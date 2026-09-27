"""Study Planner written_quiz (flashcard retrieval practice): scheduling rules, the exact practice quiz
a session opens (created once from the persisted flashcards and linked as its artifact_id), and that
practice -- submitting it or completing its session -- never changes the document's primary
assessment (latest score, low_quiz_score, performance, mastery, knowledge gaps)."""

import unittest
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from test_study_scheduler import GOLDEN_AVAILABILITY, MONDAY, context, make_state, material

from backend import flashcard_quiz_service, quiz_store, study_planner_store, study_progress
from backend.document_study_state import get_document_study_state
from backend.main import app
from backend.quiz_attempt_service import submit_quiz_attempt
from backend.study_scheduler import plan_schedule, select_candidates
from backend.study_scheduler_contracts import REASON_LABELS

PASSWORD = "long-password-x"
EARLY = "2026-09-24T09:00:00"


def kinds(state, deadline=None):
    return [(c.activity_type, c.reason.code) for c in select_candidates(material(state, deadline), MONDAY)]


class WrittenQuizSchedulingTests(unittest.TestCase):
    def test_new_material_with_flashcards_schedules_written_quiz_before_the_quiz(self):
        self.assertEqual(kinds(make_state("d", cards=12)), [
            ("summary", "new_material"), ("written_quiz", "retrieval_practice"), ("quiz", "new_material")])
        self.assertEqual(kinds(make_state("d", summary=True, cards=12), "2026-10-10"), [
            ("written_quiz", "retrieval_practice"), ("quiz", "new_material"), ("review", "final_review")])
        result = plan_schedule(context([material(make_state("d", cards=12), "2026-10-10")]))
        order = [p.activity_type for p in result.proposals]
        self.assertEqual(order, ["summary", "written_quiz", "quiz", "review"])
        written, quiz = (next(p for p in result.proposals if p.activity_type == kind) for kind in ("written_quiz", "quiz"))
        self.assertLess(written.scheduled_start, quiz.scheduled_start)
        self.assertEqual(written.reason.message, 'Written Quiz on "d": reinforce recall before your next assessment.')
        self.assertIsNone(written.artifact_id)   # linked to its practice quiz when the session starts

    def test_no_flashcards_never_schedules_a_written_quiz(self):
        for state in (make_state("d"), make_state("d", summary=True), make_state("d", percentage=40.0),
                      make_state("d", percentage=70.0), make_state("d", percentage=95.0), make_state("d", in_progress=True)):
            with self.subTest(reason=state.state_reason.code):
                self.assertNotIn("written_quiz", [kind for kind, _ in kinds(state, "2026-10-10")])
        result = plan_schedule(context([material(make_state("d"), "2026-10-10")]))
        self.assertNotIn("written_quiz", [p.activity_type for p in result.proposals])

    def test_low_score_flashcard_review_then_written_quiz_then_retry(self):
        self.assertEqual(kinds(make_state("d", cards=12, percentage=40.0)), [
            ("flashcards", "low_quiz_score"), ("written_quiz", "retrieval_practice"), ("quiz_retry", "low_quiz_score")])
        result = plan_schedule(context([material(make_state("d", cards=12, percentage=40.0))]))
        proposals = result.proposals
        self.assertEqual([p.activity_type for p in proposals], ["flashcards", "written_quiz", "quiz_retry"])
        self.assertLess(proposals[0].scheduled_start, proposals[1].scheduled_start)
        self.assertLess(date.fromisoformat(proposals[1].scheduled_start[:10]), date.fromisoformat(proposals[2].scheduled_start[:10]))

    def test_medium_score_reinforces_before_the_retry(self):
        self.assertEqual(kinds(make_state("d", cards=12, percentage=70.0)), [
            ("review", "review_due"), ("written_quiz", "retrieval_practice"), ("quiz_retry", "review_due")])

    def test_strong_score_has_no_immediate_written_quiz(self):
        self.assertEqual(kinds(make_state("d", cards=12, percentage=90.0)), [("review", "review_due")])
        result = plan_schedule(context([material(make_state("d", cards=12, percentage=90.0))], GOLDEN_AVAILABILITY))
        self.assertNotIn("written_quiz", [p.activity_type for p in result.proposals])

    def test_reason_is_explainable(self):
        self.assertEqual(REASON_LABELS["retrieval_practice"], "Scheduled to reinforce recall before your next assessment")


class WrittenQuizSessionApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.chunks = patch.object(flashcard_quiz_service, "get_document_chunks", return_value=[])
        self.chunks.start()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        study_planner_store.add_material(self.alice, self.plan["plan_id"], "mkt")

    def tearDown(self):
        self.client.close()
        self.chunks.stop()
        self.stop_planner_database()

    def session(self, activity, hour="10", artifact_id=None, reason="retrieval_practice"):
        (row,) = study_planner_store.create_sessions(self.alice, self.plan["plan_id"], [{
            "document_id": "mkt", "activity_type": activity, "artifact_id": artifact_id,
            "scheduled_start": f"2026-09-24T{hour}:00:00", "scheduled_end": f"2026-09-24T{hour}:30:00",
            "duration_minutes": 15, "reason": reason}])
        return row

    def start(self, session_id):
        response = self.client.post(f"/api/planner/sessions/{session_id}/start",
                                    json={"utc_offset_minutes": 0, "local_now": EARLY})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_start_links_the_exact_practice_quiz_once_and_resume_reuses_it(self):
        self.add_flashcards(self.alice, "mkt", 14)
        session = self.session("written_quiz")
        body = self.start(session["session_id"])
        self.assertEqual((body["tool"], body["artifact_available"], body["started"]), ("quiz", True, True))
        quiz_id = body["session"]["artifact_id"]
        quiz = quiz_store.get_quiz_by_id(quiz_id, self.alice)
        self.assertTrue(flashcard_quiz_service.is_flashcard_quiz(quiz))
        self.assertEqual((quiz["document_id"], len(quiz["questions"])), ("mkt", 10))   # at most 10 cards practiced
        self.assertTrue({q["question_type"] for q in quiz["questions"]} <= {"fill_blank", "short_answer"})
        # Persisted: a reload lists the session with that artifact; Resume opens the same quiz.
        stored = study_planner_store.get_session(self.alice, session["session_id"])
        self.assertEqual((stored["artifact_id"], stored["status"]), (quiz_id, "in_progress"))
        listed = self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/sessions").json()
        rows = listed if isinstance(listed, list) else listed["sessions"]
        self.assertEqual([(s["activity_type"], s["artifact_id"]) for s in rows], [("written_quiz", quiz_id)])
        again = self.start(session["session_id"])
        self.assertEqual((again["started"], again["session"]["artifact_id"]), (False, quiz_id))
        self.assertEqual(len(quiz_store.list_document_quizzes("mkt", self.alice)), 1)   # no duplicate practice quiz

    def test_without_flashcards_the_session_opens_flashcards(self):
        session = self.session("written_quiz")
        body = self.start(session["session_id"])
        self.assertEqual((body["tool"], body["artifact_available"], body["session"]["artifact_id"]), ("flashcards", False, None))
        self.assertEqual(quiz_store.list_document_quizzes("mkt", self.alice), {})

    def test_normal_quiz_sessions_keep_opening_their_normal_quiz(self):
        self.add_flashcards(self.alice, "mkt", 5)
        self.add_quiz(self.alice, "mkt", "q-normal")
        session = self.session("quiz", artifact_id="q-normal", reason="new_material")
        body = self.start(session["session_id"])
        self.assertEqual((body["tool"], body["session"]["artifact_id"]), ("quiz", "q-normal"))
        self.assertEqual(list(quiz_store.list_document_quizzes("mkt", self.alice)), ["q-normal"])   # nothing created

    def test_completing_written_practice_never_changes_the_primary_assessment(self):
        self.add_flashcards(self.alice, "mkt", 6)
        self.add_quiz(self.alice, "mkt", "q-normal")
        self.add_attempt(self.alice, "mkt", "q-normal", score=4, answered=10, completed=True,
                         completed_at="2026-09-23T10:00:00+00:00")   # normal Quiz: 40%
        before = get_document_study_state(self.alice, "mkt")
        self.assertEqual((before.quiz.latest_completed.percentage, before.state_reason.code), (40.0, "low_quiz_score"))
        mastery_before = quiz_store.list_topic_mastery(self.alice, "mkt")
        gaps_before = self.client.get("/api/knowledge-gaps/mkt").json()

        session = self.session("written_quiz")
        quiz_id = self.start(session["session_id"])["session"]["artifact_id"]
        quiz = quiz_store.get_quiz_by_id(quiz_id, self.alice)
        submitted = submit_quiz_attempt("mkt", "easy", "document",
                                        {str(q["id"]): q["correct_answer"] for q in quiz["questions"]},
                                        student_id=self.alice, quiz_id=quiz_id)
        self.assertEqual(submitted["percentage"], 100.0)
        completed = self.client.post(f"/api/planner/sessions/{session['session_id']}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["session"]["status"], "completed")

        after = get_document_study_state(self.alice, "mkt")
        self.assertEqual(after.quiz.latest_completed.percentage, 40.0)
        self.assertEqual(after.quiz.latest_completed.quiz_id, "q-normal")
        self.assertEqual(after.state_reason.code, "low_quiz_score")
        self.assertEqual(after.completed_session_count, before.completed_session_count + 1)   # the session counts
        self.assertEqual(study_progress.latest_quiz_percentage(self.alice, "mkt"), 40.0)
        self.assertEqual(quiz_store.list_topic_mastery(self.alice, "mkt"), mastery_before)
        self.assertEqual(self.client.get("/api/knowledge-gaps/mkt").json(), gaps_before)
        # current_quiz_performance (the dashboard metric) is built from the same latest percentage.
        performance = study_progress.current_quiz_performance({"mkt": study_progress.latest_quiz_percentage(self.alice, "mkt")})
        self.assertEqual(performance["average_percentage"], 40.0)
        # The next plan still treats the document as low-scoring: flashcards -> written quiz -> retry.
        self.assertEqual([c.activity_type for c in select_candidates(material(after), MONDAY)],
                         ["flashcards", "written_quiz", "quiz_retry"])


if __name__ == "__main__":
    unittest.main()
