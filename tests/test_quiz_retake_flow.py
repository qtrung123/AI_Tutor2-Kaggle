import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.mastery_service import recompute_topic_mastery
from backend.quiz_service import load_quiz_for_retake, submit_quiz_attempt


def saved_quiz():
    return {
        "quiz_id": "same-saved-quiz",
        "document_id": "lecture.pdf",
        "title": "Lecture",
        "difficulty": "easy",
        "topic_id": "topic_1",
        "topic_name": "Topic 1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "questions": [
            {
                "id": index,
                "question": f"Question {index}?",
                "options": ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"],
                "correct_answer": correct,
                "difficulty": "easy",
                "topic_id": "topic_1",
                "topic_name": "Topic 1",
                "concept_id": f"concept_{index}",
                "assessment_capacity": 3,
                "explanation": f"Explanation {index}",
                "source_chunk_ids": [f"chunk_{index}"],
            }
            for index, correct in enumerate(("A", "B", "C"), start=1)
        ],
    }


class QuizRetakeFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "quiz.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.db)
        self.database_patch.start()
        self.legacy_patches = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp.name) / "missing-explanations.json"),
        ]
        for legacy_patch in self.legacy_patches: legacy_patch.start()
        quiz_store.initialize_quiz_store()
        quiz_store.save_quiz("lecture.pdf", "easy", saved_quiz(), LEGACY_USER_ID)

    def tearDown(self):
        self.database_patch.stop()
        for legacy_patch in reversed(self.legacy_patches): legacy_patch.stop()
        self.temp.cleanup()

    def test_submit_requires_a_complete_answer_set(self):
        with self.assertRaisesRegex(ValueError, "Every quiz question"):
            submit_quiz_attempt(
                "lecture.pdf", "easy", "topic_1", {"1": "A", "3": "C"}, LEGACY_USER_ID
            )
        self.assertEqual(quiz_store.list_quiz_history(student_id=LEGACY_USER_ID), [])

    def test_each_retake_is_new_attempt_with_scores_and_full_snapshots(self):
        first = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "D", "3": "C"}, LEGACY_USER_ID
        )
        second = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID
        )

        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual((first["attempt_number"], first["score"], first["percentage"]), (1, 1, 33.33))
        self.assertEqual((second["attempt_number"], second["score"], second["percentage"]), (2, 3, 100.0))
        self.assertEqual(second["attempt_summary"], {
            "attempts": 2, "latest_score": 100.0, "best_score": 100.0, "average_score": 66.67
        })
        self.assertEqual(second["question_results"][0]["options"], ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"])
        self.assertEqual(second["question_results"][0]["explanation"], "Explanation 1")
        self.assertEqual(second["question_results"][0]["source_chunk_ids"], ["chunk_1"])

    def test_retake_replaces_mastery_evidence_without_inflating_coverage(self):
        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "D", "3": "D"}, LEGACY_USER_ID
        )
        first_mastery = recompute_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_1")
        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID
        )
        latest_mastery = recompute_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_1")

        self.assertEqual(first_mastery["answered_questions"], 3)
        self.assertEqual(latest_mastery["answered_questions"], 3)
        self.assertEqual(latest_mastery["distinct_concepts_assessed"], 3)
        self.assertEqual(latest_mastery["concept_coverage_ratio"], 1.0)
        self.assertEqual(latest_mastery["completed_attempts"], 1)
        self.assertEqual(latest_mastery["mastery_score"], 100.0)

    def test_retake_resolves_quiz_id_and_legacy_attempt_snapshot_when_quiz_row_is_missing(self):
        first = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "B", "3": "C"}, LEGACY_USER_ID
        )
        with quiz_store._connect() as connection:
            connection.execute("DELETE FROM quizzes WHERE quiz_id = ?", ("same-saved-quiz",))

        retake = load_quiz_for_retake(first["attempt_id"], LEGACY_USER_ID)
        self.assertEqual(retake["quiz"]["quiz_id"], "same-saved-quiz")
        self.assertEqual([question["question"] for question in retake["quiz"]["questions"]], [
            "Question 1?", "Question 2?", "Question 3?"
        ])
        second = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"},
            LEGACY_USER_ID, quiz_id="same-saved-quiz",
        )
        self.assertEqual(second["quiz_id"], first["quiz_id"])
        self.assertEqual(second["attempt_number"], 2)

    def test_frontend_uses_deferred_submission_navigation_review_and_separate_regeneration(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('check.textContent = "Check Answers"', script)
        self.assertIn('button.className = "quiz-navigator-button"', script)
        self.assertIn("requestQuizSubmission()", script)
        self.assertIn("currentAttempt = await requestQuizSubmission()", script)
        self.assertIn("createAssessmentReviewCard", script)
        self.assertIn('retake.textContent = "Retake Quiz"', script)
        self.assertIn('review.textContent = "Review Answers"', script)
        self.assertIn('regenerate.textContent = "Regenerate Quiz"', script)
        self.assertIn("startHistoryQuizRetake(attempt)", script)
        self.assertIn('actions.append(retake, close)', script)
        self.assertIn("requestQuizRegeneration", script)
        deferred_selector = script[script.rfind("function selectAssessmentAnswer") :]
        self.assertNotIn("requestQuizProgress(", deferred_selector.split("function submitAssessmentQuiz", 1)[0])


if __name__ == "__main__":
    unittest.main()
