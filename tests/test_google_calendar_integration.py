"""Google Calendar integration (MVP, one-way planning integration) -- no real Google call is made.

Google is an in-memory fake behind httpx.MockTransport (google_calendar_service._transport): OAuth
token/revoke endpoints, calendars, the calendar list, freeBusy per calendar and events. Every secret in
this file looks like "<kind>-SECRET-..." so a single check proves none of them reaches an API response.

Clock: the learner's local now is Mon 2026-09-28 08:00 (sent explicitly); availability is weekly.
"""

import json
import logging
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import google_calendar_service, google_calendar_store, google_calendar_sync, study_planner_store
from backend.main import app
from backend.study_time import free_minutes_by_date

PASSWORD = "long-password-x"
NOW = "2026-09-28T08:00:00"                       # Monday, learner-local
UTC_NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
MON, TUE = 0, 1
BASE = "/api/integrations/google-calendar"
ENV = {
    "GOOGLE_CLIENT_ID": "client-id-123.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "client-SECRET-xyz",
    "GOOGLE_CALENDAR_REDIRECT_URI": "http://testserver/api/integrations/google-calendar/callback",
    "GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "AI_TUTOR_FRONTEND_URL": "http://localhost:3000",
}
REFRESH_TOKEN = "refresh-SECRET-1"


def _json(status, body=None):
    return httpx.Response(status, json=body if body is not None else {})


class FakeGoogle:
    """Just enough of Google's OAuth + Calendar v3 REST API, with switchable failures."""

    def __init__(self):
        self.requests = []
        self.calendars = {"primary": {"unrelated-primary-event": {"id": "unrelated-primary-event", "summary": "Dentist",
                                                                  "status": "confirmed"}}}
        self.created_calendars = 0
        self.busy = []               # the primary calendar's busy time
        self.calendar_busy = {}      # other calendar id -> busy time
        self.calendar_list = [{"id": "alice@example.com", "summary": "alice@example.com", "primary": True, "selected": True}]
        self.broken_calendars = set()   # calendar ids freeBusy reports errors for
        self.fail = set()            # "freebusy", "events", "token", "network", "calendar_list"
        self.calendar_list_calls = 0
        self.revoked = False
        self.revocations = []
        self.freebusy_bodies = []
        self.scope = " ".join(google_calendar_service.SCOPES)
        self.access_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, request.url.host, path))
        if "network" in self.fail:
            raise httpx.ConnectError("offline", request=request)
        if request.url.host == "oauth2.googleapis.com":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            if path == "/revoke":
                self.revocations.append(form["token"])
                return _json(200)
            if "token" in self.fail:
                return _json(503)
            assert form["client_secret"] == ENV["GOOGLE_CLIENT_SECRET"]
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code":
                    return _json(400, {"error": "invalid_grant"})
                return self._access({"refresh_token": REFRESH_TOKEN, "scope": self.scope})
            if self.revoked or form["refresh_token"] != REFRESH_TOKEN:
                return _json(400, {"error": "invalid_grant"})
            return self._access({})
        assert request.headers["Authorization"].startswith("Bearer access-SECRET-"), "missing access token"
        api = path[len("/calendar/v3"):]
        body = json.loads(request.content) if request.content else None
        if api == "/calendars" and method == "POST":
            self.created_calendars += 1
            calendar_id = f"study-{self.created_calendars}@group.calendar.google.com"
            self.calendars[calendar_id] = {}
            # Like Google, a calendar the app creates also shows up (selected) in the user's list.
            self.calendar_list.append({"id": calendar_id, "summary": body["summary"], "selected": True})
            return _json(200, {"id": calendar_id, "summary": body["summary"]})
        if api == "/users/me/calendarList":
            self.calendar_list_calls += 1
            if "calendar_list" in self.fail:
                return _json(403)
            assert request.url.params["fields"] == "items(id,summary,primary,selected,hidden),nextPageToken"
            return _json(200, {"items": self.calendar_list})
        if api == "/freeBusy":
            self.freebusy_bodies.append(body)
            if "freebusy" in self.fail:
                return _json(503)
            calendars = {}
            for item in body["items"]:
                if item["id"] in self.broken_calendars:
                    calendars[item["id"]] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
                    continue
                busy = self.busy if item["id"] == "primary" else self.calendar_busy.get(item["id"], [])
                calendars[item["id"]] = {"busy": [{"start": s, "end": e} for s, e in busy]}
            return _json(200, {"calendars": calendars})
        match = re.fullmatch(r"/calendars/([^/]+)(/events(?:/([^/]+))?)?", api)
        calendar_id = unquote(match[1])
        if match[2] is None:
            return _json(200 if calendar_id in self.calendars else 404, {"id": calendar_id})
        if calendar_id not in self.calendars:
            return _json(404)
        if "events" in self.fail:
            return _json(500)
        events, event_id = self.calendars[calendar_id], match[3] and unquote(match[3])
        if method == "POST":
            if body["id"] in events:
                return _json(409)
            events[body["id"]] = {**body, "status": "confirmed"}
            return _json(200, events[body["id"]])
        if event_id not in events:
            return _json(404)
        if method == "PUT":
            events[event_id] = {**body, "id": event_id}
            return _json(200, events[event_id])
        if method == "DELETE":
            if events[event_id].get("status") == "cancelled":
                return _json(410)
            events[event_id]["status"] = "cancelled"
            return httpx.Response(204)
        return _json(405)

    def _access(self, extra):
        self.access_count += 1
        return _json(200, {"access_token": f"access-SECRET-{self.access_count}", "expires_in": 3600,
                           "token_type": "Bearer", **extra})

    def live(self, calendar_id):
        return {key: event for key, event in self.calendars.get(calendar_id, {}).items() if event.get("status") != "cancelled"}

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE") and "/events" in r[2]]


class _LogCapture(logging.Handler):
    def __init__(self, records):
        super().__init__(logging.DEBUG)
        self.records = records

    def emit(self, record):
        self.records.append(self.format(record))


class GoogleCalendarTestBase(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        # Every log line (any logger, any level, with tracebacks) is kept to prove no token is logged.
        self.logged = []
        root, capture = logging.getLogger(), _LogCapture(self.logged)
        previous_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(root.removeHandler, capture)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.google = FakeGoogle()
        transport = patch.object(google_calendar_service, "_transport", httpx.MockTransport(self.google.handler))
        transport.start()
        self.addCleanup(transport.stop)
        clock = patch.object(google_calendar_sync, "_utc_now", return_value=UTC_NOW)
        clock.start()
        self.addCleanup(clock.stop)
        google_calendar_service._access_tokens.clear()
        self.addCleanup(google_calendar_service._access_tokens.clear)
        self.responses = []
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        self.add_document(self.alice, "stats", "Statistics.pdf")
        self.add_document(self.alice, "mkt", "Marketing.pdf")
        self.client = self.login("alice")

    # -- helpers ---------------------------------------------------------------------

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def call(self, method, url, client=None, **kwargs):
        response = (client or self.client).request(method, url, follow_redirects=False, **kwargs)
        self.responses.append(response)
        return response

    def start_connect(self, client=None):
        response = self.call("GET", f"{BASE}/connect", client)
        self.assertEqual(response.status_code, 303, response.text)
        return response.headers["location"], parse_qs(urlparse(response.headers["location"]).query)["state"][0]

    def callback(self, state, code="good-code", client=None, **extra):
        return self.call("GET", f"{BASE}/callback", client, params={"state": state, "code": code, **extra})

    def connect(self, client=None, utc_offset_minutes=0):
        _, state = self.start_connect(client)
        response = self.callback(state, client=client)
        self.assertEqual(response.headers["location"], "http://localhost:3000/?google_calendar=connected")
        status = self.call("GET", f"{BASE}/status", client, params={"utc_offset_minutes": utc_offset_minutes}).json()
        self.assertTrue(status["connected"])
        return status

    def weekly(self, *slots, client=None):
        for day, start, end in slots:
            response = self.call("POST", "/api/planner/availability", client, json={
                "start_at": start, "end_at": end, "is_recurring": True, "day_of_week": day})
            self.assertEqual(response.status_code, 200, response.text)

    def plan_with(self, *document_ids):
        plan = study_planner_store.create_plan(self.alice, "Exams")
        for document_id in document_ids:
            study_planner_store.add_material(self.alice, plan["plan_id"], document_id)
        return plan

    def session(self, plan, activity="summary", start="2026-09-29T18:00:00", end="2026-09-29T18:30:00",
                document_id="stats", status="scheduled"):
        created = study_planner_store.create_session(self.alice, plan["plan_id"], {
            "document_id": document_id, "activity_type": activity, "scheduled_start": start, "scheduled_end": end,
            "duration_minutes": 30, "reason": "new_material"})
        if status != "scheduled":
            created = study_planner_store.update_session(self.alice, created["session_id"], {"status": status})
        return created

    def sync(self, client=None, **body):
        response = self.call("POST", f"{BASE}/sync", client, json=body or None)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def study_calendar(self):
        return google_calendar_store.get_connection(self.alice)["study_calendar_id"]

    def links(self, owner=None):
        return {link["session_id"]: link for link in google_calendar_store.list_event_links(owner or self.alice)}

    def tearDown(self):
        # Security: no token, client secret or encryption key in any API response of any test.
        for response in self.responses:
            text = response.text + json.dumps(dict(response.headers))
            self.assertNotRegex(text, r"(refresh|access|client)-SECRET", response.request.url)
            self.assertNotIn(ENV["GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY"], text)
        # ... nor in any log line.
        for line in self.logged:
            self.assertNotRegex(line, r"(refresh|access|client)-SECRET")
            self.assertNotIn(ENV["GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY"], line)


class GoogleCalendarConnectionTests(GoogleCalendarTestBase):
    def test_status_when_disconnected(self):
        status = self.call("GET", f"{BASE}/status").json()
        self.assertEqual({k: status[k] for k in ("connected", "calendar_id", "calendar_name", "connected_at")},
                         {"connected": False, "calendar_id": None, "calendar_name": None, "connected_at": None})
        self.assertTrue(status["configured"])
        self.assertFalse(status["reconnect_required"])
        self.assertEqual(self.call("GET", f"{BASE}/status", TestClient(app)).status_code, 401)

    def test_connect_builds_the_authorization_request(self):
        location, state = self.start_connect()
        url = urlparse(location)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://accounts.google.com/o/oauth2/v2/auth")
        self.assertEqual(query["scope"].split(), ["https://www.googleapis.com/auth/calendar.freebusy",
                                                  "https://www.googleapis.com/auth/calendar.app.created",
                                                  "https://www.googleapis.com/auth/calendar.calendarlist.readonly"])
        self.assertEqual((query["access_type"], query["response_type"], query["client_id"], query["redirect_uri"]),
                         ("offline", "code", ENV["GOOGLE_CLIENT_ID"], ENV["GOOGLE_CALENDAR_REDIRECT_URI"]))
        self.assertNotIn("client_secret", query)
        self.assertGreaterEqual(len(state), 40)
        with study_planner_store._connect() as connection:   # stored hashed, bound to Alice, 10 minutes
            rows = connection.execute("SELECT * FROM google_oauth_states").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["state_hash"], state)
        self.assertEqual(rows[0]["owner_id"], self.alice)
        expires = datetime.fromisoformat(rows[0]["expires_at"]) - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=9) < expires <= timedelta(minutes=10))
        self.assertEqual(self.call("GET", f"{BASE}/connect", TestClient(app)).status_code, 401)

    def test_missing_configuration_is_a_clear_error(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_SECRET": "", "GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY": ""}):
            response = self.call("GET", f"{BASE}/connect")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "not_configured")
            self.assertIn("GOOGLE_CLIENT_SECRET", response.json()["detail"]["message"])
            self.assertIn("GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY", response.json()["detail"]["message"])
            self.assertFalse(self.call("GET", f"{BASE}/status").json()["configured"])

    def test_callback_validates_state(self):
        self.start_connect()
        for bad in ("", "forged-state"):
            response = self.callback(bad)
            self.assertEqual(response.headers["location"], "http://localhost:3000/?google_calendar=error&reason=invalid_state")
        self.assertFalse([r for r in self.google.requests if r[2] == "/token"])   # the code was never used
        self.assertIsNone(google_calendar_store.get_connection(self.alice))
        # Signed out: nothing happens either.
        _, state = self.start_connect()
        response = self.callback(state, client=TestClient(app))
        self.assertEqual(response.headers["location"], "http://localhost:3000/?google_calendar=error&reason=signed_out")

    def test_expired_and_reused_state_are_rejected(self):
        _, state = self.start_connect()
        self.assertEqual(self.callback(state).headers["location"], "http://localhost:3000/?google_calendar=connected")
        self.assertEqual(self.callback(state).headers["location"],
                         "http://localhost:3000/?google_calendar=error&reason=invalid_state")   # one-time use
        _, expired = self.start_connect()
        with study_planner_store._connect() as connection:
            connection.execute("UPDATE google_oauth_states SET expires_at=? WHERE consumed_at IS NULL",
                               ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.callback(expired).headers["location"],
                         "http://localhost:3000/?google_calendar=error&reason=invalid_state")

    def test_declined_consent_and_missing_scopes(self):
        _, state = self.start_connect()
        response = self.callback(state, code="", error="access_denied")
        self.assertEqual(response.headers["location"], "http://localhost:3000/?google_calendar=error&reason=access_denied")
        self.google.scope = google_calendar_service.SCOPE_FREEBUSY   # the user unticked one permission
        _, state = self.start_connect()
        response = self.callback(state)
        self.assertEqual(response.headers["location"], "http://localhost:3000/?google_calendar=error&reason=scope_denied")
        self.assertIsNone(google_calendar_store.get_connection(self.alice))
        self.assertEqual(self.google.revocations, [REFRESH_TOKEN])

    def test_refresh_token_is_encrypted_at_rest(self):
        status = self.connect()
        self.assertEqual((status["calendar_name"], status["calendar_id"]),
                         ("AI Tutor Study Plan", "study-1@group.calendar.google.com"))
        with study_planner_store._connect() as connection:
            row = connection.execute("SELECT * FROM google_calendar_connections").fetchone()
            dump = "\n".join("|".join(map(str, r)) for table in ("google_calendar_connections", "google_oauth_states")
                             for r in connection.execute(f"SELECT * FROM {table}").fetchall())
        self.assertNotIn(REFRESH_TOKEN, dump)
        self.assertNotIn("access-SECRET", dump)            # access tokens are never persisted
        self.assertEqual(google_calendar_service.decrypt_token(row["encrypted_refresh_token"]), REFRESH_TOKEN)
        self.assertIn(google_calendar_service.SCOPE_APP_CREATED, row["granted_scopes"])

    def test_dedicated_calendar_is_created_once_and_reused(self):
        self.connect()
        self.connect()   # reconnect while connected
        self.assertEqual(self.google.created_calendars, 1)
        plan = self.plan_with("stats")
        self.session(plan)
        self.sync()
        self.call("POST", f"{BASE}/disconnect")
        self.connect()   # after a disconnect: the calendar its events live in is reused
        self.assertEqual(self.google.created_calendars, 1)
        self.assertEqual(self.study_calendar(), "study-1@group.calendar.google.com")
        self.assertEqual(len(self.google.live("study-1@group.calendar.google.com")), 1)   # no duplicate

    def test_disconnect_revokes_and_keeps_planner_data(self):
        self.connect()
        plan = self.plan_with("stats")
        session = self.session(plan)
        self.sync()
        response = self.call("POST", f"{BASE}/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["connected"], response.json()["revoked"]), (False, True))
        self.assertEqual(self.google.revocations, [REFRESH_TOKEN])
        self.assertIsNone(google_calendar_store.get_connection(self.alice))
        self.assertEqual(study_planner_store.get_session(self.alice, session["session_id"])["status"], "scheduled")
        self.assertEqual(len(study_planner_store.list_plans(self.alice)), 1)
        self.assertFalse(self.call("GET", f"{BASE}/status").json()["connected"])
        self.assertEqual(self.call("POST", f"{BASE}/sync").status_code, 409)
        # Planner actions after a disconnect never call Google.
        before = len(self.google.requests)
        self.assertNotIn("calendar_sync", self.call("POST", f"/api/planner/sessions/{session['session_id']}/skip").json())
        self.assertEqual(len(self.google.requests), before)

    def test_user_isolation(self):
        self.connect()
        bob = self.login("bob")
        _, alice_state = self.start_connect()
        # Bob cannot finish Alice's OAuth flow, see her connection, or sync/disconnect it.
        self.assertEqual(self.callback(alice_state, client=bob).headers["location"],
                         "http://localhost:3000/?google_calendar=error&reason=invalid_state")
        self.assertFalse(self.call("GET", f"{BASE}/status", bob).json()["connected"])
        self.assertEqual(self.call("POST", f"{BASE}/sync", bob).status_code, 409)
        self.call("POST", f"{BASE}/disconnect", bob)
        self.assertTrue(self.call("GET", f"{BASE}/status").json()["connected"])
        plan = self.plan_with("stats")
        self.session(plan)
        self.sync()
        self.assertEqual(self.links(self.bob), {})
        self.assertEqual(len(self.links()), 1)
        # Bob's own planner never reads Alice's Google busy time.
        self.add_document(self.bob, "bob-doc", "Bob")
        bob_plan = study_planner_store.create_plan(self.bob, "Bob")
        study_planner_store.add_material(self.bob, bob_plan["plan_id"], "bob-doc")
        self.weekly((MON, "18:00", "20:00"), client=bob)
        calls = len(self.google.freebusy_bodies)
        self.call("POST", f"/api/planner/plans/{bob_plan['plan_id']}/preview", bob, json={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual(len(self.google.freebusy_bodies), calls)

    def test_revoked_access_requires_reconnect(self):
        self.connect()
        self.google.revoked = True
        google_calendar_service._access_tokens.clear()
        plan = self.plan_with("stats")
        self.session(plan)
        self.assertEqual(self.sync()["sync"]["status"], "reconnect_required")
        status = self.call("GET", f"{BASE}/status").json()
        self.assertEqual((status["connected"], status["reconnect_required"]), (False, True))


class GoogleBusyTimeTests(GoogleCalendarTestBase):
    def setUp(self):
        super().setUp()
        self.plan = self.call("POST", "/api/planner/plans", json={"title": "Exams"}).json()
        self.call("POST", f"/api/planner/plans/{self.plan['plan_id']}/materials", json={"document_id": "stats"})
        self.weekly((MON, "18:00", "21:00"), (TUE, "18:00", "21:00"))

    def preview(self, offset=0):
        response = self.call("POST", f"/api/planner/plans/{self.plan['plan_id']}/preview",
                             json={"utc_offset_minutes": offset, "local_now": NOW})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def overlaps(session, start, end):
        return session["scheduled_start"] < end and session["scheduled_end"] > start

    def test_busy_rows_are_subtracted_from_availability(self):
        rows = google_calendar_sync.busy_availability_rows([("2026-09-28T18:10:30", "2026-09-29T01:00:00")])
        self.assertEqual([(r["date"], r["start_at"], r["end_at"]) for r in rows],
                         [("2026-09-28", "18:10", "24:00"), ("2026-09-29", "00:00", "01:00")])   # split at midnight
        availability = [{"is_recurring": True, "day_of_week": MON, "start_at": "17:00", "end_at": "21:00"}] + rows
        free = free_minutes_by_date(datetime(2026, 9, 28).date(), datetime(2026, 9, 28).date(), availability, [])
        self.assertEqual(list(free.values())[0], [(17 * 60, 18 * 60 + 10)])
        # Busy rows alone never create availability.
        self.assertEqual(list(free_minutes_by_date(datetime(2026, 9, 29).date(), datetime(2026, 9, 29).date(), rows, []).values())[0], [])

    def test_google_busy_interval_is_subtracted_before_scheduling(self):
        manual = self.preview(offset=420)
        self.assertTrue(any(self.overlaps(s, "2026-09-28T18:00:00", "2026-09-28T20:00:00") for s in manual["sessions"]))
        self.connect(utc_offset_minutes=420)
        # Monday 18:00-20:00 at UTC+7, as Google returns it (UTC).
        self.google.busy = [("2026-09-28T11:00:00Z", "2026-09-28T13:00:00Z")]
        preview = self.preview(offset=420)
        self.assertTrue(preview["sessions"])
        for session in preview["sessions"]:
            self.assertFalse(self.overlaps(session, "2026-09-28T18:00:00", "2026-09-28T20:00:00"), session)
        self.assertEqual(preview["warnings"], [])
        self.assertEqual(self.google.freebusy_bodies[-1]["timeMin"], "2026-09-27T17:00:00Z")   # local midnight in UTC
        busy = self.call("GET", f"{BASE}/busy", params={"start": "2026-09-28", "utc_offset_minutes": 420}).json()
        self.assertEqual(busy, {"busy": [{"start": "2026-09-28T18:00:00", "end": "2026-09-28T20:00:00"}], "warning": None})

    def test_confirmed_and_moved_sessions_never_overlap_busy_time(self):
        self.connect()
        self.google.busy = [("2026-09-28T18:00:00Z", "2026-09-28T21:00:00Z"), ("2026-09-29T18:00:00Z", "2026-09-29T19:00:00Z")]
        confirmed = self.call("POST", f"/api/planner/plans/{self.plan['plan_id']}/confirm",
                              json={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        sessions = confirmed.json()["sessions"]
        self.assertTrue(sessions)
        for session in sessions:
            for start, end in self.google.busy:
                self.assertFalse(self.overlaps(session, start[:19], end[:19]), session)
        # A drag into busy time is refused with its own reason.
        moved = self.call("POST", f"/api/planner/sessions/{sessions[0]['session_id']}/reschedule",
                          json={"utc_offset_minutes": 0, "local_now": NOW, "target_start": "2026-09-29T18:00:00"})
        self.assertEqual((moved.status_code, moved.json()["detail"]["code"]), (409, "calendar_busy"))
        # "Avoid conflicts" off: Google busy time is ignored again.
        self.call("PATCH", f"{BASE}/settings", json={"avoid_conflicts": False})
        calls = len(self.google.freebusy_bodies)
        self.assertTrue(any(self.overlaps(s, "2026-09-28T18:00:00", "2026-09-28T21:00:00") for s in self.preview()["sessions"]))
        self.assertEqual(len(self.google.freebusy_bodies), calls)

    def test_disconnected_user_gets_the_old_planner_behaviour(self):
        before = self.preview()
        self.connect()
        self.google.busy = [("2026-09-28T18:00:00Z", "2026-09-28T21:00:00Z")]
        self.assertNotEqual(self.preview()["sessions"], before["sessions"])
        self.call("POST", f"{BASE}/disconnect")
        calls = len(self.google.freebusy_bodies)
        self.assertEqual(self.preview(), before)
        self.assertEqual(len(self.google.freebusy_bodies), calls)
        self.assertEqual(self.call("GET", f"{BASE}/busy", params={"start": "2026-09-28", "utc_offset_minutes": 0}).json(),
                         {"busy": [], "warning": None})

    def test_google_failure_falls_back_to_manual_availability_with_a_warning(self):
        manual = self.preview()
        self.connect()
        self.google.busy = [("2026-09-28T18:00:00Z", "2026-09-28T21:00:00Z")]
        for failure in ("freebusy", "network"):
            self.google.fail = {failure}
            preview = self.preview()
            self.assertEqual(preview["sessions"], manual["sessions"])
            self.assertEqual(preview["warnings"], [{"code": "google_calendar_unavailable"}])
            self.assertEqual(self.call("GET", f"{BASE}/busy", params={"start": "2026-09-28", "utc_offset_minutes": 0}).json(),
                             {"busy": [], "warning": "google_calendar_unavailable"})
        self.google.fail = set()
        self.google.revoked = True
        google_calendar_service._access_tokens.clear()
        self.assertEqual(self.preview()["warnings"], [{"code": "google_calendar_reconnect"}])


class GoogleConflictCalendarsTests(GoogleBusyTimeTests):
    """Busy time comes from the primary calendar plus every calendar the learner shows in Google
    Calendar (CalendarList.list), merged -- never from the app's own "AI Tutor Study Plan" calendar.
    The inherited busy-time tests run again here, with secondary calendars in the list."""

    PERSONAL, WORK = "personal-1@group.calendar.google.com", "team-work@group.calendar.google.com"

    def setUp(self):
        super().setUp()
        self.google.calendar_list += [
            {"id": self.PERSONAL, "summary": "personal", "selected": True},
            {"id": self.WORK, "summary": "Work", "selected": True},
            {"id": "hidden@group.calendar.google.com", "summary": "Old", "selected": True, "hidden": True},
            {"id": "en.vietnamese#holiday@group.v.calendar.google.com", "summary": "Holidays"},   # not shown
        ]

    def busy(self):
        response = self.call("GET", f"{BASE}/busy", params={"start": "2026-09-28", "utc_offset_minutes": 0})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def queried(self):
        return [item["id"] for item in self.google.freebusy_bodies[-1]["items"]]

    def test_primary_and_secondary_calendar_busy_time_is_included(self):
        self.connect()
        self.google.busy = [("2026-09-28T09:00:00Z", "2026-09-28T10:00:00Z")]
        self.google.calendar_busy = {self.PERSONAL: [("2026-09-29T20:00:00Z", "2026-09-29T22:00:00Z")]}   # Tue 20-22, personal
        self.assertEqual(self.busy(), {"busy": [{"start": "2026-09-28T09:00:00", "end": "2026-09-28T10:00:00"},
                                                {"start": "2026-09-29T20:00:00", "end": "2026-09-29T22:00:00"}], "warning": None})

    def test_every_shown_calendar_is_queried_in_one_request(self):
        self.connect()
        self.busy()
        # Primary by its alias (once); shown secondaries; not hidden, unselected or the study calendar.
        self.assertEqual(self.queried(), ["primary", self.PERSONAL, self.WORK])
        self.assertEqual(len(self.google.freebusy_bodies), 1)
        # Only calendar metadata and free/busy are read: no event is ever listed or fetched.
        self.assertFalse([r for r in self.google.requests if r[0] == "GET" and "/events" in r[2]])

    def test_overlapping_busy_time_is_merged(self):
        self.connect()
        self.google.busy = [("2026-09-29T20:00:00Z", "2026-09-29T22:00:00Z"), ("2026-09-30T08:00:00Z", "2026-09-30T09:00:00Z")]
        self.google.calendar_busy = {
            self.PERSONAL: [("2026-09-29T19:00:00Z", "2026-09-29T23:00:00Z"), ("2026-09-30T09:00:00Z", "2026-09-30T09:30:00Z")],
            self.WORK: [("2026-09-29T20:00:00Z", "2026-09-29T22:00:00Z")],   # the same meeting on two calendars
        }
        self.assertEqual(self.busy()["busy"], [{"start": "2026-09-29T19:00:00", "end": "2026-09-29T23:00:00"},
                                               {"start": "2026-09-30T08:00:00", "end": "2026-09-30T09:30:00"}])   # touching, joined

    def test_study_plan_calendar_never_blocks_planning(self):
        self.connect()
        study = self.study_calendar()
        stale = {"id": "old-study@group.calendar.google.com", "summary": "AI Tutor Study Plan", "selected": True}
        self.google.calendar_list.append(stale)
        self.google.calendar_busy = {study: [("2026-09-28T18:00:00Z", "2026-09-28T21:00:00Z")],
                                     stale["id"]: [("2026-09-29T18:00:00Z", "2026-09-29T21:00:00Z")]}
        self.assertEqual(self.busy()["busy"], [])
        self.assertNotIn(study, self.queried())
        self.assertNotIn(stale["id"], self.queried())
        # Planning still uses the time the synced study sessions occupy in Google.
        preview = self.preview()
        self.assertTrue(any(self.overlaps(s, "2026-09-28T18:00:00", "2026-09-28T21:00:00") for s in preview["sessions"]))

    def test_secondary_calendar_busy_time_blocks_scheduling(self):
        self.connect()
        self.google.calendar_busy = {self.PERSONAL: [("2026-09-28T18:00:00Z", "2026-09-28T20:00:00Z")]}
        preview = self.preview()
        self.assertTrue(preview["sessions"])
        for session in preview["sessions"]:
            self.assertFalse(self.overlaps(session, "2026-09-28T18:00:00", "2026-09-28T20:00:00"), session)

    def test_one_unreadable_calendar_does_not_break_the_planner(self):
        self.connect()
        self.google.busy = [("2026-09-28T09:00:00Z", "2026-09-28T10:00:00Z")]
        self.google.calendar_busy = {self.WORK: [("2026-09-29T20:00:00Z", "2026-09-29T22:00:00Z")]}
        self.google.broken_calendars = {self.PERSONAL}
        self.assertEqual(self.busy(), {"busy": [{"start": "2026-09-28T09:00:00", "end": "2026-09-28T10:00:00"},
                                                {"start": "2026-09-29T20:00:00", "end": "2026-09-29T22:00:00"}], "warning": None})
        self.assertEqual(self.preview()["warnings"], [])
        # The calendar list itself failing: the primary calendar still counts, without a warning.
        self.google.fail = {"calendar_list"}
        self.assertEqual(self.busy()["busy"], [{"start": "2026-09-28T09:00:00", "end": "2026-09-28T10:00:00"}])
        self.assertEqual(self.queried(), ["primary"])
        # The primary calendar failing is still the existing fallback: manual availability + a warning.
        self.google.fail = set()
        self.google.broken_calendars = {"primary"}
        self.assertEqual(self.busy(), {"busy": [], "warning": "google_calendar_unavailable"})

    def test_connection_without_the_calendar_list_permission_uses_the_primary_calendar(self):
        self.google.scope = " ".join(google_calendar_service.REQUIRED_SCOPES)   # older grant, or unticked
        self.connect()
        self.google.busy = [("2026-09-28T09:00:00Z", "2026-09-28T10:00:00Z")]
        self.google.calendar_busy = {self.PERSONAL: [("2026-09-29T20:00:00Z", "2026-09-29T22:00:00Z")]}
        self.assertEqual(self.busy()["busy"], [{"start": "2026-09-28T09:00:00", "end": "2026-09-28T10:00:00"}])
        self.assertEqual((self.queried(), self.google.calendar_list_calls), (["primary"], 0))

    def test_disconnected_or_not_avoiding_reads_nothing(self):
        self.assertEqual(self.busy(), {"busy": [], "warning": None})
        self.connect()
        self.call("PATCH", f"{BASE}/settings", json={"avoid_conflicts": False})
        self.assertEqual(self.busy(), {"busy": [], "warning": None})
        self.assertEqual((self.google.calendar_list_calls, self.google.freebusy_bodies), (0, []))


class GoogleSessionSyncTests(GoogleCalendarTestBase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.calendar = self.study_calendar()

    def events(self):
        return self.google.live(self.calendar)

    def test_confirmed_plan_creates_one_event_per_session(self):
        plan = self.call("POST", "/api/planner/plans", json={"title": "Exams"}).json()
        self.call("POST", f"/api/planner/plans/{plan['plan_id']}/materials", json={"document_id": "stats"})
        self.weekly((MON, "18:00", "21:00"), (TUE, "18:00", "21:00"))
        self.call("POST", f"/api/planner/plans/{plan['plan_id']}/preview", json={"utc_offset_minutes": 420, "local_now": NOW})
        self.assertEqual(self.events(), {})   # preview/ghost proposals are never synced
        response = self.call("POST", f"/api/planner/plans/{plan['plan_id']}/confirm",
                             json={"utc_offset_minutes": 420, "local_now": NOW})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["calendar_sync"], {"status": "ok", "errors": 0})
        sessions = body["sessions"]
        events = self.events()
        self.assertEqual(len(events), len(sessions))
        links = self.links()
        for session in sessions:
            event = events[links[session["session_id"]]["google_event_id"]]
            self.assertEqual(event["start"]["dateTime"], session["scheduled_start"] + "+07:00")
            self.assertEqual(event["end"]["dateTime"], session["scheduled_end"] + "+07:00")
            self.assertEqual(event["extendedProperties"]["private"],
                             {"source": "ai_tutor", "session_id": session["session_id"], "activity_type": session["activity_type"]})
            self.assertTrue(event["summary"].startswith("Statistics.pdf · "))
            self.assertIn("Created by AI Tutor Study Planner", event["description"])
            self.assertEqual(links[session["session_id"]]["sync_status"], "synced")

    def test_sync_twice_creates_no_duplicate_and_no_write(self):
        plan = self.plan_with("stats")
        self.session(plan)
        first = self.sync()["sync"]
        self.assertEqual((first["created"], first["status"]), (1, "ok"))
        writes = len(self.google.writes())
        second = self.sync()["sync"]
        self.assertEqual((second["created"], second["updated"], second["unchanged"]), (0, 0, 1))
        self.assertEqual(len(self.google.writes()), writes)   # unchanged session -> no Google write
        # Even if the link was lost after Google created the event, the deterministic id prevents a duplicate.
        google_calendar_store.delete_event_link(self.alice, next(iter(self.links())))
        self.sync()
        self.assertEqual(len(self.events()), 1)

    def test_reschedule_updates_the_same_event(self):
        plan = self.plan_with("stats")
        self.weekly((TUE, "18:00", "21:00"))
        session = self.session(plan)
        self.sync()
        event_id = self.links()[session["session_id"]]["google_event_id"]
        response = self.call("POST", f"/api/planner/sessions/{session['session_id']}/reschedule",
                             json={"utc_offset_minutes": 0, "local_now": NOW, "target_start": "2026-09-29T19:00:00"})
        self.assertEqual(response.status_code, 200, response.text)
        moved = response.json()["session"]
        self.assertEqual(response.json()["calendar_sync"]["status"], "ok")
        events = self.events()
        self.assertEqual(list(events), [event_id])
        self.assertEqual(events[event_id]["start"]["dateTime"], "2026-09-29T19:00:00+00:00")
        self.assertEqual(events[event_id]["extendedProperties"]["private"]["session_id"], moved["session_id"])
        self.assertEqual(list(self.links()), [moved["session_id"]])

    def test_skip_delete_and_removal_remove_the_linked_event(self):
        plan = self.plan_with("stats", "mkt")
        skipped, _, kept = (self.session(plan), self.session(plan, "quiz", "2026-09-29T19:00:00", "2026-09-29T19:30:00", "mkt"),
                            self.session(plan, "review", "2026-09-30T18:00:00", "2026-09-30T18:30:00"))
        self.sync()
        self.assertEqual(len(self.events()), 3)
        self.assertEqual(self.call("POST", f"/api/planner/sessions/{skipped['session_id']}/skip").json()["calendar_sync"]["status"], "ok")
        self.assertEqual(len(self.events()), 2)
        material = study_planner_store.get_plan_material(self.alice, plan["plan_id"], "mkt")
        self.call("DELETE", f"/api/planner/plans/{plan['plan_id']}/materials/{material['material_id']}")
        self.assertEqual(set(self.links()), {kept["session_id"]})
        self.call("DELETE", f"/api/planner/plans/{plan['plan_id']}")
        self.assertEqual((self.events(), self.links()), ({}, {}))

    def test_completed_session_keeps_its_event(self):
        plan = self.plan_with("stats")
        session = self.session(plan, start="2026-09-28T08:00:00", end="2026-09-28T08:30:00")
        self.sync()
        started = self.call("POST", f"/api/planner/sessions/{session['session_id']}/start",
                            json={"utc_offset_minutes": 0, "local_now": "2026-09-28T08:05:00"})
        self.assertEqual(started.status_code, 200, started.text)
        writes = len(self.google.writes())
        done = self.call("POST", f"/api/planner/sessions/{session['session_id']}/complete").json()
        self.assertEqual(done["session"]["status"], "completed")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.google.writes()), writes)
        # Archiving the plan keeps completed history on the calendar too.
        self.call("PATCH", f"/api/planner/plans/{plan['plan_id']}", json={"status": "archived"})
        self.assertEqual(len(self.events()), 1)

    def test_unrelated_google_events_are_untouched(self):
        foreign = {"id": "user-made-event", "summary": "Gym", "status": "confirmed"}
        self.google.calendars[self.calendar]["user-made-event"] = dict(foreign)
        primary_before = json.dumps(self.google.calendars["primary"], sort_keys=True)
        plan = self.plan_with("stats")
        session = self.session(plan)
        self.sync()
        self.call("POST", f"/api/planner/sessions/{session['session_id']}/skip")
        self.call("DELETE", f"/api/planner/plans/{plan['plan_id']}")
        self.sync()
        self.assertEqual(self.google.calendars[self.calendar]["user-made-event"], foreign)
        self.assertEqual(json.dumps(self.google.calendars["primary"], sort_keys=True), primary_before)
        touched = {unquote(path.rsplit("/", 1)[-1]) for method, _, path in self.google.writes() if method != "POST"}
        self.assertNotIn("user-made-event", touched)
        self.assertNotIn("unrelated-primary-event", touched)

    def test_failed_sync_never_rolls_back_the_planner_action_and_retry_fixes_it(self):
        plan = self.plan_with("stats")
        first, second = self.session(plan), self.session(plan, "flashcards", "2026-09-30T18:00:00", "2026-09-30T18:30:00")
        self.sync()
        self.google.fail = {"events"}
        response = self.call("POST", f"/api/planner/sessions/{first['session_id']}/skip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["status"], "skipped")
        self.assertEqual(response.json()["calendar_sync"], {"status": "error", "errors": 1})
        self.assertEqual(study_planner_store.get_session(self.alice, first["session_id"])["status"], "skipped")
        self.assertEqual(self.links()[first["session_id"]]["sync_status"], "error")
        third = self.session(plan, "quiz", "2026-10-01T18:00:00", "2026-10-01T18:30:00")
        failed = self.sync()
        self.assertEqual((failed["sync"]["status"], failed["sync_errors"]), ("error", 2))
        self.assertEqual(self.links()[third["session_id"]]["sync_status"], "error")
        # Retry after Google recovers: the skipped event is removed, the new one created, no error left.
        self.google.fail = set()
        retried = self.sync()
        self.assertEqual((retried["sync"]["status"], retried["sync_errors"], retried["sync"]["deleted"], retried["sync"]["updated"]),
                         ("ok", 0, 1, 1))
        self.assertEqual({e["extendedProperties"]["private"]["session_id"] for e in self.events().values()},
                         {second["session_id"], third["session_id"]})

    def test_deleted_study_calendar_is_recreated(self):
        plan = self.plan_with("stats")
        self.session(plan)
        self.sync()
        del self.google.calendars[self.calendar]
        self.session(plan, "quiz", "2026-10-01T18:00:00", "2026-10-01T18:30:00")
        self.assertEqual(self.sync()["sync"]["status"], "ok")
        self.assertEqual(self.google.created_calendars, 2)
        self.assertEqual(len(self.google.live(self.study_calendar())), 2)

    def test_every_activity_maps_to_its_calendar_title(self):
        expected = {"summary": "Statistics.pdf · Summary", "flashcards": "Statistics.pdf · Flashcards",
                    "written_quiz": "Statistics.pdf · Written Quiz", "quiz": "Statistics.pdf · Quiz",
                    "quiz_retry": "Statistics.pdf · Quiz retry", "review": "Statistics.pdf · Review"}
        plan = self.plan_with("stats")
        for index, activity in enumerate(expected):
            self.session(plan, activity, f"2026-09-29T{10 + index:02d}:00:00", f"2026-09-29T{10 + index:02d}:30:00")
        self.sync()
        titles = {e["extendedProperties"]["private"]["activity_type"]: e["summary"] for e in self.events().values()}
        self.assertEqual(titles, expected)
        for event in self.events().values():   # never quiz answers or scores
            self.assertNotRegex(event["description"], r"\d+ ?%|answer|score:")

    def test_written_quiz_session_stays_deep_linkable(self):
        self.add_flashcards(self.alice, "stats", 6)
        plan = self.plan_with("stats")
        session = self.session(plan, "written_quiz", "2026-09-28T09:00:00", "2026-09-28T09:30:00")
        self.sync()
        event = next(iter(self.events().values()))
        self.assertEqual(event["extendedProperties"]["private"],
                         {"source": "ai_tutor", "session_id": session["session_id"], "activity_type": "written_quiz"})
        started = self.call("POST", f"/api/planner/sessions/{session['session_id']}/start",
                            json={"utc_offset_minutes": 0, "local_now": "2026-09-28T09:05:00"}).json()
        self.assertEqual((started["tool"], started["artifact_available"]), ("quiz", True))
        self.assertTrue(started["session"]["artifact_id"])
        self.assertEqual(len(self.events()), 1)

    def test_quiz_and_quiz_retry_sessions_stay_deep_linkable(self):
        self.add_quiz(self.alice, "stats", "quiz-1")
        plan = self.plan_with("stats")
        quiz = self.session(plan, "quiz", "2026-09-28T09:00:00", "2026-09-28T09:30:00")
        retry = self.session(plan, "quiz_retry", "2026-09-28T10:00:00", "2026-09-28T10:30:00")
        self.sync()
        for session, local_now in ((quiz, "2026-09-28T09:05:00"), (retry, "2026-09-28T10:05:00")):
            started = self.call("POST", f"/api/planner/sessions/{session['session_id']}/start",
                                json={"utc_offset_minutes": 0, "local_now": local_now})
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual((started.json()["tool"], started.json()["artifact_available"]), ("quiz", True))
        self.assertEqual(len(self.events()), 2)   # started sessions keep their events

    def test_connection_does_not_change_learning_progress(self):
        self.add_quiz(self.alice, "stats", "quiz-1")
        self.add_attempt(self.alice, "stats", "quiz-1", score=6, answered=10, completed=True)
        url = "/api/progress/documents/stats"
        params = {"utc_offset_minutes": 0, "local_now": NOW}
        connected = self.call("GET", url, params=params)
        self.assertEqual(connected.status_code, 200, connected.text)
        self.call("POST", f"{BASE}/disconnect")
        self.assertEqual(self.call("GET", url, params=params).json(), connected.json())


if __name__ == "__main__":
    unittest.main()
