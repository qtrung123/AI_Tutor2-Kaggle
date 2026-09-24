"""Study Planner Phase 7B: direct manipulation over HTTP.

- preview/confirm accept learner placements that only name the server's own candidates
  (candidate_key) and are validated against the recomputed schedule; confirm refuses a stale or
  invalid placement atomically;
- reschedule takes an exact target_start (calendar drag) and keeps rescheduled_from lineage;
- a confirmed plan lists the activities it still wants (candidates) and places one at a chosen time.

Clock: the learner's local "now" is sent explicitly: Mon 2026-09-28 08:00, UTC offset 0.
"""

import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-28T08:00:00"
BODY = {"utc_offset_minutes": 0, "local_now": NOW}


class PlacementApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = self.login("alice")
        self.plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        self.base = f"/api/planner/plans/{self.plan['plan_id']}"
        self.client.post(f"{self.base}/materials", json={"document_id": "mkt", "deadline": "2026-10-01"})
        self.client.post(f"{self.base}/materials", json={"document_id": "stats"})
        for day in (0, 1, 2, 3):   # Mon-Thu 18:00-22:00, every week
            self.client.post("/api/planner/availability", json={
                "start_at": "18:00", "end_at": "22:00", "is_recurring": True, "day_of_week": day})

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def preview(self, placements=None):
        response = self.client.post(f"{self.base}/preview", json={**BODY, **({"placements": placements} if placements else {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def confirm(self, placements=None, client=None):
        return (client or self.client).post(f"{self.base}/confirm", json={**BODY, **({"placements": placements} if placements else {})})

    def sessions(self):
        return study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"])

    @staticmethod
    def free_start(taken, day, duration):
        """The first 15-minute-aligned start inside 18:00-22:00 on `day` clear of `taken`."""
        start = datetime.fromisoformat(f"{day}T18:00:00")
        while start + timedelta(minutes=duration) <= datetime.fromisoformat(f"{day}T22:00:00"):
            end = start + timedelta(minutes=duration)
            if not any(s["scheduled_start"] < end.isoformat() and s["scheduled_end"] > start.isoformat() for s in taken):
                return start.isoformat()
            start += timedelta(minutes=15)
        raise AssertionError("no free start")

    # -- preview ---------------------------------------------------------------------------

    def test_preview_keys_every_candidate_and_applies_a_valid_placement(self):
        before = self.preview()
        keys = [s["candidate_key"] for s in before["sessions"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(not s["placed"] for s in before["sessions"]))
        first = before["sessions"][0]
        target = self.free_start(before["sessions"], "2026-09-30", first["duration_minutes"])
        after = self.preview([{"candidate_key": first["candidate_key"], "scheduled_start": target}])
        moved = next(s for s in after["sessions"] if s["candidate_key"] == first["candidate_key"])
        self.assertEqual((moved["scheduled_start"], moved["placed"], moved["duration_minutes"]),
                         (target, True, first["duration_minutes"]))
        self.assertEqual(after["placements"], {"applied": [first["candidate_key"]], "rejected": []})
        self.assertEqual(len(after["sessions"]), len(before["sessions"]))
        self.assertEqual(self.sessions(), [])   # preview never writes

    def test_invalid_placements_are_rejected_and_leave_the_schedule_alone(self):
        before = self.preview()
        first, second = before["sessions"][0], before["sessions"][1]
        cases = {
            "invalid_time": "2026-09-30T18:05:00",
            "slot_in_past": "2026-09-28T07:00:00",
            "outside_availability": "2026-09-30T10:00:00",
            "slot_taken": second["scheduled_start"],
        }
        for code, start in cases.items():
            with self.subTest(code=code):
                body = self.preview([{"candidate_key": first["candidate_key"], "scheduled_start": start}])
                self.assertEqual([r["code"] for r in body["placements"]["rejected"]], [code])
                self.assertEqual(body["sessions"], before["sessions"])
        mkt = next(s for s in before["sessions"] if s["document_id"] == "mkt")
        late = self.preview([{"candidate_key": mkt["candidate_key"], "scheduled_start": "2026-10-05T18:00:00"}])
        self.assertEqual(late["placements"]["rejected"][0]["code"], "after_deadline")
        stale = self.preview([{"candidate_key": "mkt|summary|9", "scheduled_start": "2026-09-30T18:00:00"}])
        self.assertEqual(stale["placements"]["rejected"][0]["code"], "candidate_stale")

    def test_client_cannot_send_sessions_or_unknown_fields(self):
        self.assertEqual(self.client.post(f"{self.base}/preview", json={**BODY, "sessions": []}).status_code, 422)
        bad = {**BODY, "placements": [{"candidate_key": "mkt|summary|0", "scheduled_start": NOW, "duration_minutes": 5}]}
        self.assertEqual(self.client.post(f"{self.base}/confirm", json=bad).status_code, 422)
        self.assertEqual(self.sessions(), [])

    # -- confirm -----------------------------------------------------------------------------

    def test_a_moved_suggestion_survives_confirm(self):
        before = self.preview()
        first = before["sessions"][0]
        target = self.free_start(before["sessions"], "2026-09-30", first["duration_minutes"])
        response = self.confirm([{"candidate_key": first["candidate_key"], "scheduled_start": target}])
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.sessions()
        self.assertEqual(len(saved), len(before["sessions"]))
        match = [s for s in saved if s["document_id"] == first["document_id"] and s["activity_type"] == first["activity_type"]
                 and s["scheduled_start"] == target]
        self.assertEqual(len(match), 1)

    def test_confirm_rejects_a_stale_or_invalid_placement_atomically(self):
        before = self.preview()
        first = before["sessions"][0]
        for placement in ({"candidate_key": first["candidate_key"], "scheduled_start": "2026-09-30T10:00:00"},
                          {"candidate_key": "stats|quiz|7", "scheduled_start": "2026-09-30T18:00:00"}):
            with self.subTest(placement=placement):
                response = self.confirm([placement])
                self.assertEqual(response.status_code, 409)
                detail = response.json()["detail"]
                self.assertEqual(detail["code"], "placement_rejected")
                self.assertEqual(len(detail["placements"]), 1)
                self.assertEqual(self.sessions(), [])
                self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, self.plan["plan_id"]), [])

    def test_an_unscheduled_candidate_can_be_placed_before_confirm(self):
        # Only this Monday evening is free: the scheduler stops at its daily target, so work is left
        # unscheduled while the learner still has free time that evening.
        for day in (0, 1, 2, 3):
            self.client.post("/api/planner/availability/remove", json={
                "start_at": "18:00", "end_at": "22:00", "is_recurring": True, "day_of_week": day})
        self.client.post("/api/planner/availability", json={
            "start_at": "18:00", "end_at": "22:00", "is_recurring": False, "date": "2026-09-28"})
        before = self.preview()
        self.assertTrue(before["capacity"]["unscheduled"])
        waiting = before["capacity"]["unscheduled"][0]
        target = self.free_start(before["sessions"], "2026-09-28", waiting["estimated_minutes"])
        after = self.preview([{"candidate_key": waiting["candidate_key"], "scheduled_start": target}])
        placed = [s for s in after["sessions"] if s["candidate_key"] == waiting["candidate_key"]]
        self.assertEqual([(s["scheduled_start"], s["placed"]) for s in placed], [(target, True)])
        self.assertNotIn(waiting["candidate_key"], [c["candidate_key"] for c in after["capacity"]["unscheduled"]])
        self.assertEqual(after["capacity"]["scheduled_minutes"],
                         before["capacity"]["scheduled_minutes"] + waiting["estimated_minutes"])
        response = self.confirm([{"candidate_key": waiting["candidate_key"], "scheduled_start": target}])
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn(target, [s["scheduled_start"] for s in self.sessions()])

    # -- confirmed plan: drag a session ---------------------------------------------------------

    def confirmed(self):
        self.assertEqual(self.confirm().status_code, 201)
        return self.sessions()

    def reschedule(self, session_id, target, client=None):
        return (client or self.client).post(f"/api/planner/sessions/{session_id}/reschedule",
                                            json={**BODY, "target_start": target})

    def test_dragging_a_confirmed_session_reschedules_it_once_with_lineage(self):
        saved = self.confirmed()
        first = saved[0]
        target = self.free_start(saved, "2026-09-30", first["duration_minutes"])
        response = self.reschedule(first["session_id"], target)
        self.assertEqual(response.status_code, 200, response.text)
        moved = response.json()["session"]
        self.assertEqual((moved["scheduled_start"], moved["rescheduled_from"], moved["status"]),
                         (target, first["session_id"], "scheduled"))
        after = self.sessions()
        self.assertEqual(len(after), len(saved) + 1)   # exactly one new row
        self.assertEqual(study_planner_store.get_session(self.alice, first["session_id"])["status"], "rescheduled")

    def test_an_invalid_drag_writes_nothing(self):
        saved = self.confirmed()
        first, second = saved[0], saved[1]
        cases = {
            "outside_availability": "2026-09-30T10:00:00",
            "slot_taken": second["scheduled_start"],
            "slot_in_past": "2026-09-28T07:00:00",
            "invalid_time": "2026-09-30T18:10:00",
        }
        for code, target in cases.items():
            with self.subTest(code=code):
                response = self.reschedule(first["session_id"], target)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertEqual(self.sessions(), saved)
        mkt = next(s for s in saved if s["document_id"] == "mkt")
        late = self.reschedule(mkt["session_id"], "2026-10-05T18:00:00")
        self.assertEqual(late.json()["detail"]["code"], "after_deadline")
        self.assertEqual(self.reschedule(first["session_id"], "2026-09-30T20:00:00", client=self.login("bob")).status_code, 404)
        self.assertEqual(self.sessions(), saved)

    def test_only_scheduled_sessions_can_be_dragged(self):
        saved = self.confirmed()
        first = saved[0]
        study_planner_store.transition_session(self.alice, first["session_id"], "skip")
        response = self.reschedule(first["session_id"], "2026-09-30T21:00:00")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "session_not_reschedulable"))

    # -- confirmed plan: place a still-wanted activity ------------------------------------------

    def test_confirmed_plan_candidates_can_be_placed_once(self):
        saved = self.confirmed()
        study_planner_store.transition_session(self.alice, saved[-1]["session_id"], "skip")   # frees a wanted activity
        listing = self.client.get(f"{self.base}/candidates", params={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual(listing.status_code, 200, listing.text)
        candidates = listing.json()
        self.assertTrue(candidates)
        wanted = candidates[0]
        self.assertEqual(set(wanted), {"candidate_key", "document_id", "document_title", "activity_type",
                                       "estimated_minutes", "deadline", "reason", "artifact_id"})
        target = self.free_start(self.sessions(), "2026-09-30", wanted["estimated_minutes"])
        place = lambda start, key=wanted["candidate_key"], client=None: (client or self.client).post(
            f"{self.base}/candidates/place", json={**BODY, "candidate_key": key, "scheduled_start": start})
        # invalid slots and foreign / unknown candidates write nothing
        before = self.sessions()
        self.assertEqual(place("2026-09-30T10:00:00").json()["detail"]["code"], "outside_availability")
        self.assertEqual(place(target, key="mkt|quiz_retry|3").json()["detail"]["code"], "candidate_stale")
        self.assertEqual(place(target, client=self.login("bob")).status_code, 404)
        self.assertEqual(self.sessions(), before)
        response = place(target)
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()["session"]
        self.assertEqual((session["document_id"], session["activity_type"], session["scheduled_start"], session["duration_minutes"]),
                         (wanted["document_id"], wanted["activity_type"], target, wanted["estimated_minutes"]))
        self.assertEqual(len(self.sessions()), len(before) + 1)
        # the same activity is now covered: placing it again is stale
        again = place(self.free_start(self.sessions(), "2026-09-29", wanted["estimated_minutes"]))
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "candidate_stale"))

    def test_archived_plan_takes_no_new_sessions(self):
        self.confirmed()
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})
        listing = self.client.get(f"{self.base}/candidates", params={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual((listing.status_code, listing.json()["detail"]["code"]), (400, "plan_not_active"))


if __name__ == "__main__":
    unittest.main()
