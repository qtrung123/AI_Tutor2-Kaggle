"""Study Planner Phase 5A: POST /api/planner/plans/{plan_id}/adaptation/preview -- read-only,
owner/plan isolated, built from persisted data only."""

import unittest

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from backend import study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-24T12:00:00"


class StudyAdaptationApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice, self.bob = self.user("Alice"), self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.bob, "bob-doc", "Bob's notes")
        self.client = self.login("alice")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        study_planner_store.add_material(self.alice, self.plan["plan_id"], "mkt")
        for day in range(7):
            self.client.post("/api/planner/availability", json={"start_at": "18:00", "end_at": "21:00",
                                                                 "is_recurring": True, "day_of_week": day})

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def session(self, activity, start, end, minutes, owner=None, plan_id=None, document_id="mkt"):
        return study_planner_store.create_session(owner or self.alice, plan_id or self.plan["plan_id"], {
            "document_id": document_id, "activity_type": activity, "scheduled_start": start, "scheduled_end": end,
            "duration_minutes": minutes, "reason": "new_material"})

    def preview(self, trigger, plan_id=None, client=None):
        return (client or self.client).post(f"/api/planner/plans/{plan_id or self.plan['plan_id']}/adaptation/preview",
                                            json={"trigger": trigger, "utc_offset_minutes": 0, "local_now": NOW})

    def snapshot(self):
        return sorted((s["session_id"], s["status"], s["scheduled_start"], s["updated_at"])
                      for s in study_planner_store.list_sessions(self.alice))

    def test_missed_session_proposal_is_read_only(self):
        missed = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45)
        self.session("flashcards", "2026-09-25T18:00:00", "2026-09-25T18:20:00", 20)
        before = self.snapshot()
        response = self.preview({"kind": "session_missed", "session_id": missed["session_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["persisted"], body["significance"], body["unchanged_count"]), (False, "small", 1))
        self.assertEqual([(a["activity_type"], a["scheduled_start"], a["replaces_session_id"]) for a in body["added"]],
                         [("summary", "2026-09-24T18:00:00", missed["session_id"])])
        self.assertEqual(set(body), {"plan_id", "persisted", "local_now", "trigger", "added", "moved", "cancelled",
                                     "unchanged_count", "change_count", "warnings", "significance", "reasons"})
        self.assertEqual(self.snapshot(), before)   # nothing was written

    def test_quiz_completed_uses_the_persisted_quiz_result(self):
        self.add_summary(self.alice, "mkt")
        self.add_flashcards(self.alice, "mkt", 10)
        self.add_quiz(self.alice, "mkt", "q1")
        self.add_attempt(self.alice, "mkt", "q1", score=3, answered=10, completed=True,
                         completed_at="2026-09-24T10:00:00+00:00")
        body = self.preview({"kind": "quiz_completed", "document_id": "mkt"}).json()
        self.assertEqual([(a["activity_type"], a["reason_code"]) for a in body["added"]],
                         [("flashcards", "low_quiz_score"), ("quiz_retry", "low_quiz_score")])
        self.assertEqual(body["added"][1]["artifact_id"], "q1")
        self.assertLess(body["added"][0]["scheduled_start"], body["added"][1]["scheduled_start"])

    def test_owner_and_plan_isolation(self):
        missed = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45)
        bob = self.login("bob")
        self.assertEqual(self.preview({"kind": "availability_changed"}, client=bob).status_code, 404)
        bob_plan = study_planner_store.create_plan(self.bob, "Bob")
        study_planner_store.add_material(self.bob, bob_plan["plan_id"], "bob-doc")
        # Bob cannot point his own plan at Alice's session or document.
        self.assertEqual(self.preview({"kind": "session_missed", "session_id": missed["session_id"]},
                                      plan_id=bob_plan["plan_id"], client=bob).status_code, 404)
        self.assertEqual(self.preview({"kind": "quiz_completed", "document_id": "mkt"},
                                      plan_id=bob_plan["plan_id"], client=bob).status_code, 404)
        # ...nor Alice at another of her plans' sessions.
        other = study_planner_store.create_plan(self.alice, "Other")
        study_planner_store.add_material(self.alice, other["plan_id"], "mkt")
        foreign = self.session("quiz", "2026-09-24T08:00:00", "2026-09-24T08:30:00", 30, plan_id=other["plan_id"])
        self.assertEqual(self.preview({"kind": "session_missed", "session_id": foreign["session_id"]}).status_code, 404)

    def test_invalid_triggers_and_inactive_plans(self):
        future = self.session("quiz", "2026-09-25T18:00:00", "2026-09-25T18:30:00", 30)
        for trigger in ({"kind": "flashcards_reviewed"}, {"kind": "quiz_completed"},
                        {"kind": "session_missed", "session_id": future["session_id"]}):
            response = self.preview(trigger)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "invalid_trigger"), trigger)
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})
        response = self.preview({"kind": "availability_changed"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "plan_not_active"))


if __name__ == "__main__":
    unittest.main()
