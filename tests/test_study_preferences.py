"""Study Preferences (per user, persisted) and manual scheduling control (Task 5).

- GET/PUT/DELETE /api/planner/preferences: defaults until saved, validation, reload, per-user isolation;
- the saved daily target, break, strategy and activity lengths are what Auto Plan, adaptation and
  live candidates actually use -- saving never changes an existing session;
- POST /api/planner/sessions/{id}/reschedule with target_start / duration_minutes: the learner's own
  date, time and length, validated against other sessions, Google Calendar busy time, availability
  and the deadline; a refused change writes nothing; a saved one is kept by adaptation.
"Now" is Mon 2026-09-28 08:00 (UTC offset 0).
"""

import unittest
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin
from test_study_scheduler import make_state, material, minute_of, weekly

from backend import google_calendar_sync, study_plan_api_service, study_planner_store, study_preferences
from backend.main import app
from backend.study_adaptation import AdaptationConfig, AdaptationContext, AdaptationTrigger, propose_adaptation
from backend.study_scheduler import DEFAULT_CONFIG

UTC_NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
LOCAL_NOW = "2026-09-28T08:00:00"
BODY = {"utc_offset_minutes": 0, "local_now": LOCAL_NOW}


class PlannerApiCase(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=UTC_NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        for doc, title in (("mkt", "Marketing"), ("stats", "Statistics"), ("pbi", "PowerBI")):
            self.add_document(self.alice, doc, title)
        self.client = self.login("alice")

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": "long-password-x"})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def evenings(self, days=range(7), start="18:00", end="22:00"):
        for day in days:
            self.client.post("/api/planner/availability", json={"start_at": start, "end_at": end,
                                                                "is_recurring": True, "day_of_week": day})

    def plan(self, *materials):
        plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        for document_id, deadline in materials:
            body = {"document_id": document_id, **({"deadline": deadline} if deadline else {})}
            self.assertEqual(self.client.post(f"/api/planner/plans/{plan['plan_id']}/materials", json=body).status_code, 201)
        return plan

    def preview(self, plan):
        response = self.client.post(f"/api/planner/plans/{plan['plan_id']}/preview", json=BODY)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def put(self, client=None, **body):
        return (client or self.client).put("/api/planner/preferences", json=body)


class PreferencesApiTests(PlannerApiCase):
    def test_defaults_until_saved(self):
        prefs = self.client.get("/api/planner/preferences").json()
        self.assertEqual({k: prefs[k] for k in ("daily_target_minutes", "break_minutes", "strategy", "saved")},
                         {"daily_target_minutes": 90, "break_minutes": 20, "strategy": "balanced", "saved": False})
        self.assertEqual(prefs["durations"], {"summary": 45, "flashcards": 20, "quiz": 35, "quiz_retry": 35, "review": 30,
                                              "written_quiz": 20, "final_review": 45, "light_review": 20})
        self.assertEqual(prefs["options"]["daily_target_minutes"], [60, 90, 120, 180])
        self.assertEqual(prefs["options"]["break_minutes"], [5, 10, 15, 20, 30])
        self.assertEqual(prefs["options"]["strategy"], ["balanced", "finish_early", "deadline_priority"])
        self.assertIs(study_preferences.scheduler_config(self.alice), DEFAULT_CONFIG)   # behaviour unchanged

    def test_save_reload_merge_and_reset(self):
        saved = self.put(daily_target_minutes=120, break_minutes=10, strategy="finish_early",
                         durations={"summary": 60, "quiz": 40})
        self.assertEqual(saved.status_code, 200, saved.text)
        # A fresh session (as after a reload / a new login) reads the same values from the database.
        again = self.login("alice").get("/api/planner/preferences").json()
        self.assertEqual({k: again[k] for k in ("daily_target_minutes", "break_minutes", "strategy", "saved")},
                         {"daily_target_minutes": 120, "break_minutes": 10, "strategy": "finish_early", "saved": True})
        self.assertEqual((again["durations"]["summary"], again["durations"]["quiz"], again["durations"]["flashcards"]), (60, 40, 20))
        # Only the fields sent change; durations merge per activity.
        merged = self.put(durations={"flashcards": 25}).json()
        self.assertEqual((merged["daily_target_minutes"], merged["durations"]["summary"], merged["durations"]["flashcards"]),
                         (120, 60, 25))
        reset = self.client.delete("/api/planner/preferences").json()
        self.assertEqual((reset["saved"], reset["daily_target_minutes"], reset["durations"]["summary"]), (False, 90, 45))

    def test_invalid_values_are_refused_with_the_field_and_nothing_is_saved(self):
        cases = [({"daily_target_minutes": 75}, "daily_target_minutes"), ({"break_minutes": 25}, "break_minutes"),
                 ({"strategy": "cram"}, "strategy"), ({"durations": {"summary": 15}}, "durations.summary"),
                 ({"durations": {"quiz": 42}}, "durations.quiz"), ({"durations": {"juggling": 30}}, "durations.juggling"),
                 ({"daily_target_minutes": 60, "durations": {"final_review": 75}}, "durations.final_review")]
        for body, field in cases:
            with self.subTest(body=body):
                response = self.put(**body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual((response.json()["detail"]["code"], response.json()["detail"]["field"]), ("invalid_preference", field))
        self.assertIn("at least 20 minutes", self.put(durations={"summary": 15}).json()["detail"]["message"])
        self.assertEqual(self.put(colour="blue").status_code, 422)
        self.assertFalse(self.client.get("/api/planner/preferences").json()["saved"])

    def test_preferences_are_per_user(self):
        self.put(daily_target_minutes=180, durations={"summary": 90})
        bob = self.login("bob").get("/api/planner/preferences").json()
        self.assertEqual((bob["daily_target_minutes"], bob["durations"]["summary"], bob["saved"]), (90, 45, False))
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/planner/preferences").status_code, 401)


class PreferencesAppliedTests(PlannerApiCase):
    def day_sessions(self, sessions):
        by_day = defaultdict(list)
        for s in sessions:
            by_day[s["scheduled_start"][:10]].append(s)
        return {day: sorted(items, key=lambda s: s["scheduled_start"]) for day, items in by_day.items()}

    def test_auto_plan_uses_the_saved_lengths_break_and_daily_target(self):
        self.evenings()
        plan = self.plan(("mkt", "2026-10-02"), ("stats", "2026-10-02"))
        self.put(daily_target_minutes=120, break_minutes=10, durations={"summary": 50, "flashcards": 25, "quiz": 40})
        sessions = self.preview(plan)["sessions"]
        lengths = {s["activity_type"]: s["duration_minutes"] for s in sessions if s["reason"]["code"] != "final_review"}
        self.assertEqual((lengths["summary"], lengths["flashcards"], lengths["quiz"]), (50, 25, 40))
        days = self.day_sessions(sessions)
        self.assertTrue(any(sum(s["duration_minutes"] for s in items) > 90 for items in days.values()))   # 120 a day
        for items in days.values():
            self.assertLessEqual(sum(s["duration_minutes"] for s in items), 120)
            gaps = [minute_of(b["scheduled_start"]) - minute_of(a["scheduled_end"]) for a, b in zip(items, items[1:])]
            self.assertTrue(all(gap >= 10 for gap in gaps), gaps)
        all_gaps = [minute_of(b["scheduled_start"]) - minute_of(a["scheduled_end"])
                    for items in days.values() for a, b in zip(items, items[1:])]
        self.assertIn(10, all_gaps)   # the learner's 10-minute break, not the 20-minute default

    def test_strategy_changes_the_distribution(self):
        self.evenings()
        plan = self.plan(("mkt", None), ("stats", None), ("pbi", None))
        balanced = self.preview(plan)["sessions"]
        self.put(strategy="finish_early")
        early = self.preview(plan)["sessions"]
        self.assertGreater(len({s["scheduled_start"][:10] for s in balanced}), len({s["scheduled_start"][:10] for s in early}))
        self.assertLess(max(s["scheduled_start"] for s in early), max(s["scheduled_start"] for s in balanced))

    def test_deadline_priority_puts_deadlined_material_first(self):
        self.evenings()
        plan = self.plan(("mkt", "2026-10-09"), ("stats", None))
        balanced = [s for s in self.preview(plan)["sessions"] if s["document_id"] == "mkt" and s["reason"]["code"] != "final_review"]
        self.put(strategy="deadline_priority")
        priority = [s for s in self.preview(plan)["sessions"] if s["document_id"] == "mkt" and s["reason"]["code"] != "final_review"]
        self.assertLess(max(s["scheduled_start"] for s in priority), max(s["scheduled_start"] for s in balanced))
        self.assertTrue(all(s["scheduled_start"] < "2026-10-09" for s in priority))

    def test_saving_preferences_never_changes_accepted_sessions(self):
        self.evenings()
        plan = self.plan(("mkt", None))
        confirmed = self.client.post(f"/api/planner/plans/{plan['plan_id']}/confirm", json=BODY)
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        before = study_planner_store.list_sessions(self.alice, plan_id=plan["plan_id"])
        self.put(daily_target_minutes=60, break_minutes=5, strategy="finish_early", durations={"summary": 30})
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan["plan_id"]), before)

    def test_adaptation_and_live_candidates_use_the_saved_lengths(self):
        self.evenings()
        plan = self.plan(("mkt", None))
        self.client.post(f"/api/planner/plans/{plan['plan_id']}/confirm", json=BODY)
        self.put(durations={"review": 40, "quiz_retry": 25})
        self.add_quiz(self.alice, "mkt", "q1")
        self.add_attempt(self.alice, "mkt", "q1", score=4, answered=10, completed=True, completed_at="2026-09-28T07:00:00+00:00")
        proposal = self.client.post(f"/api/planner/plans/{plan['plan_id']}/adaptation/preview", json={
            "trigger": {"kind": "quiz_completed", "document_id": "mkt"}, **BODY}).json()
        # The planned flashcards session already counts as the weak-topic review (kept with its own
        # length); what is added uses the learner's lengths.
        added = {a["activity_type"]: a["duration_minutes"] for a in proposal["added"]}
        self.assertEqual(added["quiz_retry"], 25)
        candidates = self.client.get(f"/api/planner/plans/{plan['plan_id']}/candidates",
                                     params={"utc_offset_minutes": 0, "local_now": LOCAL_NOW}).json()
        lengths = {c["activity_type"]: c["estimated_minutes"] for c in candidates}
        self.assertEqual(lengths.get("review"), 40)


class ManualEditTests(PlannerApiCase):
    def setUp(self):
        super().setUp()
        self.evenings(start="18:00", end="22:00")
        self.plan_row = self.plan(("mkt", "2026-10-09"), ("stats", None))
        confirmed = self.client.post(f"/api/planner/plans/{self.plan_row['plan_id']}/confirm", json=BODY)
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        self.sessions = sorted(confirmed.json()["sessions"], key=lambda s: s["scheduled_start"])

    def edit(self, session, **body):
        return self.client.post(f"/api/planner/sessions/{session['session_id']}/reschedule", json={**BODY, **body})

    def saved(self):
        return study_planner_store.list_sessions(self.alice, plan_id=self.plan_row["plan_id"])

    def test_change_date_time_and_length(self):
        first = self.sessions[0]
        response = self.edit(first, target_start="2026-10-01T20:15:00", duration_minutes=60)
        self.assertEqual(response.status_code, 200, response.text)
        moved = response.json()["session"]
        self.assertEqual((moved["scheduled_start"], moved["scheduled_end"], moved["duration_minutes"], moved["manual_edit"]),
                         ("2026-10-01T20:15:00", "2026-10-01T21:15:00", 60, True))
        self.assertEqual(study_planner_store.get_session(self.alice, first["session_id"])["status"], "rescheduled")
        # Length only: same start, new length.
        resized = self.edit(moved, duration_minutes=30).json()["session"]
        self.assertEqual((resized["scheduled_start"], resized["duration_minutes"], resized["manual_edit"]),
                         ("2026-10-01T20:15:00", 30, True))
        # A plain drag is the learner's choice too.
        dragged = self.edit(self.sessions[1], target_start="2026-10-03T18:00:00").json()["session"]
        self.assertEqual((dragged["duration_minutes"], dragged["manual_edit"]), (self.sessions[1]["duration_minutes"], True))

    def test_conflicting_changes_are_refused_with_a_clear_reason_and_write_nothing(self):
        first, second = self.sessions[0], self.sessions[1]
        before = self.saved()
        busy = [{"busy": True, "date": "2026-10-03", "start_at": "18:00", "end_at": "20:00", "is_recurring": False,
                 "day_of_week": None}]

        def with_google(owner_id, availability, now, offset):
            return list(availability) + busy, []

        cases = [
            ({"target_start": second["scheduled_start"]}, "slot_taken", "already taken by another session"),
            ({"target_start": "2026-10-03T18:30:00"}, "calendar_busy", "busy in your Google Calendar"),
            ({"target_start": "2026-10-12T18:00:00"}, "after_deadline", "after this material's deadline"),
            ({"target_start": "2026-10-01T10:00:00"}, "outside_availability", "inside your available hours"),
            ({"target_start": "2026-09-27T18:00:00"}, "slot_in_past", "already passed"),
            ({"duration_minutes": 15}, "session_too_short", "at least 20 minutes"),
            ({"duration_minutes": 300}, "session_too_long", "at most 180 minutes"),
            ({"target_start": "2026-10-01T21:30:00", "duration_minutes": 60}, "outside_availability", "available hours"),
        ]
        mkt_first = next(s for s in self.sessions if s["document_id"] == "mkt")
        with patch.object(google_calendar_sync, "planner_availability", side_effect=with_google):
            for body, code, text in cases:
                with self.subTest(code=code, body=body):
                    target = mkt_first if code == "after_deadline" else first
                    response = self.edit(target, **body)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["detail"]["code"], code)
                    self.assertIn(text, response.json()["detail"]["message"])
        self.assertEqual(self.saved(), before)

    def test_adaptation_never_moves_or_cancels_a_hand_edited_session(self):
        quiz = next(s for s in self.sessions if s["document_id"] == "mkt" and s["activity_type"] == "quiz")
        edited = self.edit(quiz, target_start="2026-10-07T21:00:00", duration_minutes=50).json()["session"]
        # A deadline change would normally re-plan Marketing's sessions.
        self.client.patch(f"/api/planner/plans/{self.plan_row['plan_id']}/materials/"
                          f"{study_planner_store.get_plan_material(self.alice, self.plan_row['plan_id'], 'mkt')['material_id']}",
                          json={"deadline": "2026-10-05"})
        proposal = self.client.post(f"/api/planner/plans/{self.plan_row['plan_id']}/adaptation/apply", json={
            "trigger": {"kind": "deadline_changed", "document_id": "mkt"}, "confirm": True, **BODY}).json()
        touched = {m["session_id"] for m in proposal["moved"]} | {c["session_id"] for c in proposal["cancelled"]}
        self.assertNotIn(edited["session_id"], touched)
        kept = study_planner_store.get_session(self.alice, edited["session_id"])
        self.assertEqual((kept["status"], kept["scheduled_start"], kept["duration_minutes"]),
                         ("scheduled", "2026-10-07T21:00:00", 50))
        self.assertIn("manual_session_conflict", [w["code"] for w in proposal["warnings"]])   # now after the deadline


class ManualSessionEngineTests(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 12, 0)
    EVENINGS = weekly(*((d, "18:00", "21:00") for d in range(7)))

    def session(self, session_id, activity, start, minutes, manual=False, reason="new_material"):
        begin = datetime.fromisoformat(start)
        return {"session_id": session_id, "document_id": "mkt", "activity_type": activity, "scheduled_start": start,
                "scheduled_end": (begin + timedelta(minutes=minutes)).isoformat(), "duration_minutes": minutes,
                "status": "scheduled", "reason": reason, "artifact_id": None, "manual_edit": manual}

    def propose(self, trigger, state, sessions, availability=EVENINGS, deadline=None):
        ctx = AdaptationContext(trigger=trigger, now=self.NOW, utc_offset=timedelta(0),
                                materials=(material(state, deadline),), plan_sessions=tuple(sessions),
                                availability=tuple(availability), busy_sessions=tuple(sessions))
        return propose_adaptation(ctx)

    def test_an_unneeded_manual_session_is_kept_not_cancelled(self):
        state = make_state("mkt", cards=10, percentage=95.0, completed_at="2026-09-24T10:00:00+00:00")
        sessions = [self.session("mine", "quiz_retry", "2026-09-25T18:00:00", 25, manual=True),
                    self.session("auto", "quiz_retry", "2026-09-26T18:00:00", 25)]
        result = propose_adaptation(AdaptationContext(
            trigger=AdaptationTrigger("quiz_completed", document_id="mkt"), now=self.NOW, utc_offset=timedelta(0),
            materials=(material(state),), plan_sessions=tuple(sessions), availability=self.EVENINGS,
            busy_sessions=tuple(sessions)))
        self.assertEqual([c.session_id for c in result.cancelled], ["auto"])

    def test_availability_change_warns_about_a_manual_session_instead_of_moving_it(self):
        sessions = [self.session("mine", "summary", "2026-09-25T18:00:00", 45, manual=True),
                    self.session("auto", "flashcards", "2026-09-26T18:00:00", 20)]
        later = weekly(*((d, "19:00", "21:00") for d in range(7)))   # 18:00 is no longer available
        result = self.propose(AdaptationTrigger("availability_changed"), make_state("mkt"), sessions, availability=later)
        self.assertEqual([m.session_id for m in result.moved], ["auto"])
        self.assertEqual([(w["code"], w["session_id"]) for w in result.warnings], [("manual_session_conflict", "mine")])
        self.assertIn("stays as it is", " ".join(result.reasons))


if __name__ == "__main__":
    unittest.main()
