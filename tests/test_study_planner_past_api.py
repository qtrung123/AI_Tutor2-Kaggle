"""Study Planner: the past is read-only on the server too.

Availability sent with the learner's UTC offset (as the desktop calendar does) may not start in the
learner's past; a missed session can only be moved to a future start. "Now" is pinned: Thu
2026-09-24 14:00 local (offset 0); 2026-09-24 is a Thursday (weekday 3).
"""

import unittest

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-24T14:00:00"


class PastIsReadOnlyApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    def add(self, **slot):
        return self.client.post("/api/planner/availability", json={"utc_offset_minutes": 0, "local_now": NOW, **slot})

    def test_dated_availability_in_the_past_is_refused(self):
        cases = {
            "previous day": {"date": "2026-09-23", "start_at": "18:00", "end_at": "20:00"},
            "today before now": {"date": "2026-09-24", "start_at": "10:00", "end_at": "12:00"},
            "today straddling now": {"date": "2026-09-24", "start_at": "13:30", "end_at": "15:00"},
        }
        for name, slot in cases.items():
            with self.subTest(name=name):
                response = self.add(**slot)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"]["code"], "availability_in_past")
        self.assertEqual(study_planner_store.list_availability(self.alice), [])

    def test_future_availability_is_accepted(self):
        self.assertEqual(self.add(date="2026-09-24", start_at="14:00", end_at="16:00").status_code, 200)
        self.assertEqual(self.add(date="2026-09-25", start_at="09:00", end_at="10:00").status_code, 200)
        # a weekly rule has no date: it only applies from now on, whichever weekday
        self.assertEqual(self.add(is_recurring=True, day_of_week=1, start_at="18:00", end_at="20:00").status_code, 200)
        self.assertEqual(len(study_planner_store.list_availability(self.alice)), 3)

    def test_without_an_offset_the_request_is_unchanged(self):
        # the step flow (phone/tablet) sends no offset: its behavior is not changed here
        response = self.client.post("/api/planner/availability", json={"date": "2026-09-01", "start_at": "10:00", "end_at": "11:00"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_missed_session_moves_only_to_the_future(self):
        plan = study_planner_store.create_plan(self.alice, "Exams")
        study_planner_store.add_material(self.alice, plan["plan_id"], "mkt")
        self.add(is_recurring=True, day_of_week=3, start_at="09:00", end_at="22:00")
        missed = study_planner_store.create_session(self.alice, plan["plan_id"], {
            "document_id": "mkt", "activity_type": "summary", "scheduled_start": "2026-09-24T10:00:00",
            "scheduled_end": "2026-09-24T10:30:00", "duration_minutes": 30, "reason": "new_material"})
        move = lambda target: self.client.post(f"/api/planner/sessions/{missed['session_id']}/reschedule",
                                               json={"utc_offset_minutes": 0, "local_now": NOW, "target_start": target})
        past = move("2026-09-24T11:00:00")
        self.assertEqual((past.status_code, past.json()["detail"]["code"]), (409, "slot_in_past"))
        self.assertEqual(study_planner_store.get_session(self.alice, missed["session_id"])["status"], "scheduled")
        future = move("2026-09-24T15:00:00")
        self.assertEqual(future.status_code, 200, future.text)
        self.assertEqual(future.json()["session"]["rescheduled_from"], missed["session_id"])


if __name__ == "__main__":
    unittest.main()
