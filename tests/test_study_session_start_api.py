"""Study Planner Phase 4B: POST /api/planner/sessions/{session_id}/start -- exact-session,
owner-scoped scheduled -> in_progress, idempotent resume, terminal/archived rules, and which study
tool each activity opens (review resolved from the document's state)."""

import threading
import unittest

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from backend import study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
EARLY = "2026-09-24T09:00:00"   # the learner's local now: before the 10:00+ sessions below


class StudySessionStartApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = self.login("alice")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        for document_id in ("mkt", "stats"):
            study_planner_store.add_material(self.alice, self.plan["plan_id"], document_id)

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def sessions(self, *specs, plan_id=None):
        rows = [{"document_id": document_id, "activity_type": activity, "scheduled_start": f"2026-09-24T{hour}:00:00",
                 "scheduled_end": f"2026-09-24T{hour}:30:00", "duration_minutes": 30, "reason": "new_material"}
                for document_id, activity, hour in specs]
        return study_planner_store.create_sessions(self.alice, plan_id or self.plan["plan_id"], rows)

    def start(self, session_id, client=None, local_now=EARLY):
        return (client or self.client).post(f"/api/planner/sessions/{session_id}/start",
                                            json={"utc_offset_minutes": 0, "local_now": local_now})

    # -- a missed session (its time is over on the learner's clock) is not startable ----------------

    def test_missed_session_cannot_be_started(self):
        (missed,) = self.sessions(("mkt", "quiz", "10"))   # 10:00-10:30
        for now in ("2026-09-24T10:30:00", "2026-09-24T12:00:00", "2026-09-25T08:00:00"):
            with self.subTest(now=now):
                response = self.start(missed["session_id"], local_now=now)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], {"code": "session_missed", "status": "scheduled",
                                                             "message": "This session's time has passed. Reschedule it instead."})
        stored = study_planner_store.get_session(self.alice, missed["session_id"])
        self.assertEqual((stored["status"], stored["started_at"]), ("scheduled", None))

    def test_session_in_its_window_or_ahead_can_be_started(self):
        current, ahead = self.sessions(("mkt", "summary", "10"), ("stats", "summary", "11"))
        now_in_window = "2026-09-24T10:15:00"
        for session in (current, ahead):
            response = self.start(session["session_id"], local_now=now_in_window)
            self.assertEqual((response.status_code, response.json()["started"]), (200, True), response.text)

    def test_resume_of_an_in_progress_session_is_unchanged_after_its_window(self):
        (session,) = self.sessions(("mkt", "summary", "10"))
        first = self.start(session["session_id"], local_now="2026-09-24T10:10:00").json()
        later = self.start(session["session_id"], local_now="2026-09-24T18:00:00")   # well after 10:30
        self.assertEqual(later.status_code, 200, later.text)
        self.assertEqual((later.json()["started"], later.json()["session"]["started_at"]),
                         (False, first["session"]["started_at"]))

    def test_the_learner_offset_is_required(self):
        (session,) = self.sessions(("mkt", "summary", "10"))
        self.assertEqual(self.client.post(f"/api/planner/sessions/{session['session_id']}/start").status_code, 422)
        self.assertEqual(study_planner_store.get_session(self.alice, session["session_id"])["status"], "scheduled")

    def test_starts_exactly_that_session(self):
        first, second = self.sessions(("mkt", "summary", "10"), ("stats", "summary", "11"))
        response = self.start(first["session_id"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["started"], body["session"]["session_id"], body["session"]["status"]),
                         (True, first["session_id"], "in_progress"))
        self.assertEqual(body["session"]["document_title"], "Marketing")
        self.assertTrue(body["session"]["started_at"])
        self.assertEqual(study_planner_store.get_session(self.alice, second["session_id"])["status"], "scheduled")
        self.assertIsNone(study_planner_store.get_session(self.alice, second["session_id"])["started_at"])

    def test_owner_isolation(self):
        (mine,) = self.sessions(("mkt", "summary", "10"))
        bob = self.login("bob")
        self.assertEqual(self.start(mine["session_id"], client=bob).status_code, 404)
        self.assertEqual(study_planner_store.get_session(self.alice, mine["session_id"])["status"], "scheduled")
        self.assertEqual(self.start("no-such-session").status_code, 404)
        self.assertEqual(TestClient(app).post(f"/api/planner/sessions/{mine['session_id']}/start").status_code, 401)

    def test_resume_is_idempotent_and_keeps_started_at(self):
        (session,) = self.sessions(("mkt", "quiz", "10"))
        first = self.start(session["session_id"]).json()
        again = self.start(session["session_id"])
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["started"])
        self.assertEqual(again.json()["session"]["started_at"], first["session"]["started_at"])
        self.assertEqual(again.json()["session"]["status"], "in_progress")

    def test_terminal_statuses_are_rejected(self):
        rows = self.sessions(("mkt", "summary", "10"), ("mkt", "quiz", "11"), ("stats", "summary", "12"))
        for row, status in zip(rows, ("completed", "skipped", "missed")):
            study_planner_store.update_session(self.alice, row["session_id"], {"status": status})
            response = self.start(row["session_id"])
            self.assertEqual(response.status_code, 409, status)
            self.assertEqual(response.json()["detail"]["code"], "session_not_startable")
            self.assertEqual(response.json()["detail"]["status"], status)
            self.assertEqual(study_planner_store.get_session(self.alice, row["session_id"])["status"], status)

    def test_archived_plan_rule(self):
        scheduled, started = self.sessions(("mkt", "summary", "10"), ("stats", "summary", "11"))
        self.start(started["session_id"])
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})
        blocked = self.start(scheduled["session_id"])
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "plan_not_active")
        self.assertEqual(study_planner_store.get_session(self.alice, scheduled["session_id"])["status"], "scheduled")
        resumed = self.start(started["session_id"])   # an in-progress session can still be resumed
        self.assertEqual((resumed.status_code, resumed.json()["started"]), (200, False))

    def test_activity_to_tool_mapping(self):
        rows = self.sessions(("mkt", "summary", "10"), ("mkt", "flashcards", "11"), ("mkt", "quiz", "12"),
                             ("mkt", "quiz_retry", "13"))
        tools = [self.start(row["session_id"]).json()["tool"] for row in rows]
        self.assertEqual(tools, ["summary", "flashcards", "quiz", "quiz"])

    def test_missing_artifact_is_reported_not_an_error(self):
        summary, cards, quiz = self.sessions(("mkt", "summary", "10"), ("mkt", "flashcards", "11"), ("mkt", "quiz", "12"))
        for row in (summary, cards, quiz):
            response = self.start(row["session_id"])
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["artifact_available"])
        self.add_summary(self.alice, "stats")
        (with_summary,) = self.sessions(("stats", "summary", "13"))
        self.assertTrue(self.start(with_summary["session_id"]).json()["artifact_available"])

    def test_review_uses_the_document_state(self):
        # Nothing generated yet -> Flashcards (its empty state creates the set), never Overview.
        (bare,) = self.sessions(("mkt", "review", "10"))
        self.assertEqual((self.start(bare["session_id"]).json()["tool"],
                          self.start(bare["session_id"]).json()["artifact_available"]), ("flashcards", False))
        # A quiz only -> Quiz; plus a summary -> Summary; plus flashcards -> Flashcards.
        self.add_quiz(self.alice, "stats", "q1")
        (quiz_only,) = self.sessions(("stats", "review", "11"))
        self.assertEqual(self.start(quiz_only["session_id"]).json()["tool"], "quiz")
        self.add_summary(self.alice, "stats")
        (with_summary,) = self.sessions(("stats", "review", "12"))
        self.assertEqual(self.start(with_summary["session_id"]).json()["tool"], "summary")
        self.add_flashcards(self.alice, "stats", 8)
        (with_cards,) = self.sessions(("stats", "review", "13"))
        body = self.start(with_cards["session_id"]).json()
        self.assertEqual((body["tool"], body["artifact_available"]), ("flashcards", True))

    def test_concurrent_starts_write_once(self):
        (session,) = self.sessions(("mkt", "summary", "10"))
        results, barrier = [], threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(study_planner_store.start_session(self.alice, session["session_id"]))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(started for _, started in results), 1)
        self.assertEqual(len({row["started_at"] for row, _ in results}), 1)


if __name__ == "__main__":
    unittest.main()
