"""Phase 6A: learner progress from persisted data only -- GET /api/progress/documents/{id},
GET /api/planner/plans/{id}/progress and the dashboard's current quiz performance."""

import unittest

from fastapi.testclient import TestClient

from planner_fixtures import SCHEMA, PlannerDatabaseMixin
from backend import quiz_store, study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-24T12:00:00"
QUERY = f"?utc_offset_minutes=0&local_now={NOW}"


class StudyProgressApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice, self.bob = self.user("Alice"), self.user("Bob")
        for document_id, title in (("mkt", "Marketing"), ("stats", "Statistics"), ("pbi", "PowerBI")):
            self.add_document(self.alice, document_id, title)
        self.add_document(self.bob, "bob-doc", "Bob's notes")
        self.client = self.login("alice")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        for document_id in ("mkt", "stats", "pbi"):
            study_planner_store.add_material(self.alice, self.plan["plan_id"], document_id,
                                             deadline="2026-10-01" if document_id == "mkt" else None)

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def quiz(self, document_id, quiz_id, score, completed_at):
        self.add_quiz(self.alice, document_id, quiz_id)
        with quiz_store._connect() as connection:   # a real quiz belongs to the document's current topic map
            connection.execute("UPDATE quizzes SET topic_schema_version=? WHERE quiz_id=?", (SCHEMA, quiz_id))
        self.add_attempt(self.alice, document_id, quiz_id, score=score, answered=10, completed=True,
                         completed_at=completed_at)

    def session(self, document_id, activity, start, minutes, status="scheduled"):
        hour, minute = int(start[11:13]), int(start[14:16]) + minutes
        end = f"{start[:11]}{hour + minute // 60:02d}:{minute % 60:02d}:00"
        row = study_planner_store.create_session(self.alice, self.plan["plan_id"], {
            "document_id": document_id, "activity_type": activity, "scheduled_start": start, "scheduled_end": end,
            "duration_minutes": minutes, "reason": "new_material"})
        if status in ("in_progress", "completed"):
            study_planner_store.update_session(self.alice, row["session_id"], {"status": "in_progress"})
        if status != "scheduled" and status != "in_progress":
            study_planner_store.update_session(self.alice, row["session_id"], {"status": status})
        return row

    def document(self, document_id, client=None):
        return (client or self.client).get(f"/api/progress/documents/{document_id}{QUERY}")

    # -- per document ----------------------------------------------------------------------

    def test_document_progress_uses_real_persisted_values(self):
        self.add_summary(self.alice, "mkt")
        self.add_flashcards(self.alice, "mkt", 8)
        self.quiz("mkt", "q1", 4, "2026-09-20T10:00:00+00:00")
        self.quiz("mkt", "q2", 8, "2026-09-23T10:00:00+00:00")   # the retake is the latest score
        done = self.session("mkt", "summary", "2026-09-22T18:00:00", 30, "completed")
        running = self.session("mkt", "flashcards", "2026-09-24T11:30:00", 20, "in_progress")
        self.session("mkt", "quiz", "2026-09-25T18:00:00", 45)
        self.session("mkt", "review", "2026-09-24T09:00:00", 30)          # overdue: still planned, still remaining
        self.session("mkt", "quiz", "2026-09-21T18:00:00", 20, "skipped")
        self.session("mkt", "review", "2026-09-23T18:00:00", 30, "rescheduled")   # superseded: not counted
        self.session("mkt", "quiz_retry", "2026-09-27T18:00:00", 15, "cancelled")  # withdrawn: not counted
        self.session("stats", "summary", "2026-09-25T19:00:00", 40)        # another document: not counted
        response = self.document("mkt")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["learning"], {"state": "on_track", "label": "On track",
                                            "explanation": "Latest quiz score 8/10 (80%) is strong."})
        quiz = body["quiz"]
        self.assertEqual((quiz["latest"]["percentage"], quiz["latest"]["score"], quiz["latest"]["total"]), (80.0, 8, 10))
        self.assertEqual([a["percentage"] for a in quiz["attempts"]], [40.0, 80.0])   # oldest first
        self.assertEqual((quiz["attempt_count"], quiz["trend"]), (2, "improving"))
        self.assertEqual(body["flashcards"], {"card_count": 8})
        plan = body["plan"]
        self.assertEqual((plan["plan_id"], plan["deadline"]), (self.plan["plan_id"], "2026-10-01"))
        self.assertEqual((plan["completed_sessions"], plan["planned_sessions"]), (1, 5))
        self.assertEqual((plan["completed_minutes"], plan["remaining_minutes"]), (30, 20 + 45 + 30))
        self.assertEqual(plan["next_session"]["session_id"], running["session_id"])   # work under way comes first
        self.assertNotEqual(plan["next_session"]["session_id"], done["session_id"])

    def test_study_pack_reports_only_real_artifacts(self):
        pack = lambda: self.client.get(f"/api/progress/documents/stats{QUERY}").json()["study_pack"]
        self.assertEqual(pack(), {"summary_ready": False, "flashcard_count": 0, "quiz_count": 0})   # nothing created yet
        self.add_summary(self.alice, "stats")
        self.add_flashcards(self.alice, "stats", 7)
        self.assertEqual(pack(), {"summary_ready": True, "flashcard_count": 7, "quiz_count": 0})
        self.add_quiz(self.alice, "stats", "q-stats")
        self.assertEqual(pack(), {"summary_ready": True, "flashcard_count": 7, "quiz_count": 1})

    def test_next_session_skips_sessions_whose_time_has_passed(self):
        self.session("stats", "summary", "2026-09-24T09:00:00", 30)
        later = self.session("stats", "quiz", "2026-09-26T18:00:00", 30)
        self.assertEqual(self.document("stats").json()["plan"]["next_session"]["session_id"], later["session_id"])

    def test_empty_states_without_quiz_or_plan(self):
        body = self.document("pbi").json()
        self.assertEqual(body["learning"]["state"], "new")
        self.assertEqual(body["quiz"], {"latest": None, "attempts": [], "attempt_count": 0, "trend": None})
        self.assertEqual(body["plan"]["planned_sessions"], 0)
        self.assertIsNone(body["plan"]["next_session"])
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})
        self.assertIsNone(self.document("pbi").json()["plan"])   # not in an ACTIVE plan

    def test_a_single_attempt_has_no_trend(self):
        self.quiz("stats", "q1", 5, "2026-09-23T10:00:00+00:00")
        quiz = self.document("stats").json()["quiz"]
        self.assertEqual((quiz["attempt_count"], quiz["trend"], quiz["latest"]["percentage"]), (1, None, 50.0))

    def test_steady_and_declining_trends(self):
        self.quiz("stats", "q1", 7, "2026-09-20T10:00:00+00:00")
        self.quiz("stats", "q2", 7, "2026-09-22T10:00:00+00:00")
        self.assertEqual(self.document("stats").json()["quiz"]["trend"], "steady")
        self.quiz("stats", "q3", 3, "2026-09-23T10:00:00+00:00")
        self.assertEqual(self.document("stats").json()["quiz"]["trend"], "declining")

    # -- plan aggregate + dashboard --------------------------------------------------------

    def test_plan_progress_averages_latest_scores_of_assessed_documents_only(self):
        self.quiz("mkt", "q1", 4, "2026-09-20T10:00:00+00:00")
        self.quiz("mkt", "q2", 8, "2026-09-23T10:00:00+00:00")   # latest 80, not the average of 40 and 80
        self.quiz("stats", "q3", 6, "2026-09-22T10:00:00+00:00")
        self.session("mkt", "summary", "2026-09-22T18:00:00", 30, "completed")
        self.session("stats", "summary", "2026-09-25T18:00:00", 40)
        self.session("pbi", "summary", "2026-09-26T18:00:00", 40, "cancelled")
        response = self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/progress{QUERY}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["current_quiz_performance"],
                         {"average_percentage": 70.0, "assessed_documents": 2, "documents": 3})   # pbi excluded, not 0
        self.assertEqual((body["completed_sessions"], body["planned_sessions"]), (1, 2))
        self.assertEqual((body["completed_minutes"], body["remaining_minutes"]), (30, 40))
        by_document = {d["document_id"]: d for d in body["documents"]}
        self.assertEqual((by_document["mkt"]["latest_quiz_percentage"], by_document["pbi"]["latest_quiz_percentage"]),
                         (80.0, None))
        self.assertEqual(by_document["mkt"]["deadline"], "2026-10-01")

    def test_plan_without_quizzes_or_sessions(self):
        body = self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/progress{QUERY}").json()
        self.assertEqual(body["current_quiz_performance"],
                         {"average_percentage": None, "assessed_documents": 0, "documents": 3})
        self.assertEqual((body["completed_sessions"], body["planned_sessions"], body["next_session"]), (0, 0, None))

    def test_dashboard_reports_current_quiz_performance_not_all_time_accuracy(self):
        self.quiz("mkt", "q1", 2, "2026-09-20T10:00:00+00:00")
        self.quiz("mkt", "q2", 9, "2026-09-23T10:00:00+00:00")
        metrics = self.client.get("/api/dashboard").json()["metrics"]
        self.assertNotIn("quiz_accuracy", metrics)
        self.assertEqual(metrics["current_quiz_performance"],
                         {"average_percentage": 90.0, "assessed_documents": 1, "documents": 3})

    # -- isolation / validation ------------------------------------------------------------

    def test_owner_isolation(self):
        bob = self.login("bob")
        self.assertEqual(self.document("mkt", client=bob).status_code, 404)
        self.assertEqual(bob.get(f"/api/planner/plans/{self.plan['plan_id']}/progress{QUERY}").status_code, 404)
        self.assertEqual(self.document("bob-doc").status_code, 404)

    def test_the_learner_offset_is_required(self):
        self.assertEqual(self.client.get("/api/progress/documents/mkt").status_code, 422)
        self.assertEqual(self.client.get("/api/progress/documents/mkt?utc_offset_minutes=9999").status_code, 400)


if __name__ == "__main__":
    unittest.main()
