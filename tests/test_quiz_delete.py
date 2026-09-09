import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.quiz_service import delete_quiz, submit_quiz_attempt


def saved_quiz(quiz_id, topic_id, topic_name):
    return {
        "quiz_id": quiz_id,
        "document_id": "lecture.pdf",
        "title": "Lecture",
        "difficulty": "easy",
        "topic_id": topic_id,
        "topic_name": topic_name,
        "created_at": "2026-01-01T00:00:00+00:00",
        "questions": [
            {
                "id": index,
                "question": f"Question {index}?",
                "options": ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"],
                "correct_answer": correct,
                "difficulty": "easy",
                "topic_id": topic_id,
                "topic_name": topic_name,
                "concept_id": f"concept_{topic_id}_{index}",
                "assessment_capacity": 3,
                "explanation": f"Explanation {index}",
                "source_chunk_ids": [f"chunk_{index}"],
            }
            for index, correct in enumerate(("A", "B", "C"), start=1)
        ],
    }


class QuizDeleteTests(unittest.TestCase):
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
        for legacy_patch in self.legacy_patches:
            legacy_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        for legacy_patch in self.legacy_patches:
            legacy_patch.stop()
        self.temp.cleanup()

    def test_delete_removes_quiz_data_and_recomputes_only_its_own_topic_mastery(self):
        quiz_store.save_quiz("lecture.pdf", "easy", saved_quiz("quiz-a", "topic_1", "Topic 1"), LEGACY_USER_ID)
        quiz_store.save_quiz("lecture.pdf", "easy", saved_quiz("quiz-b", "topic_2", "Topic 2"), LEGACY_USER_ID)

        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"},
            LEGACY_USER_ID, quiz_id="quiz-a",
        )
        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_2", {"1": "A", "2": "B", "3": "C"},
            LEGACY_USER_ID, quiz_id="quiz-b",
        )
        mastery_before_topic_2 = quiz_store.get_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_2")
        self.assertTrue(mastery_before_topic_2["has_evidence"])

        result = delete_quiz("quiz-a", LEGACY_USER_ID)

        self.assertEqual(result["recomputed_topic_ids"], ["topic_1"])
        self.assertIsNone(quiz_store.get_quiz_by_id("quiz-a", LEGACY_USER_ID))
        with quiz_store._connect() as connection:
            attempts_left = connection.execute(
                "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id = ?", ("quiz-a",)
            ).fetchone()[0]
            answers_left = connection.execute(
                "SELECT COUNT(*) FROM quiz_attempt_answers a JOIN quiz_attempts t ON t.attempt_id = a.attempt_id "
                "WHERE t.quiz_id = ?", ("quiz-a",)
            ).fetchone()[0]
        self.assertEqual(attempts_left, 0)
        self.assertEqual(answers_left, 0)

        mastery_topic_1 = quiz_store.get_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_1")
        self.assertFalse(mastery_topic_1["has_evidence"])

        self.assertIsNotNone(quiz_store.get_quiz_by_id("quiz-b", LEGACY_USER_ID))
        mastery_topic_2 = quiz_store.get_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_2")
        self.assertEqual(mastery_topic_2, mastery_before_topic_2)

    def test_delete_without_completed_attempts_skips_mastery_recompute(self):
        quiz_store.save_quiz("lecture.pdf", "easy", saved_quiz("quiz-c", "topic_3", "Topic 3"), LEGACY_USER_ID)
        result = delete_quiz("quiz-c", LEGACY_USER_ID)
        self.assertEqual(result["recomputed_topic_ids"], [])
        self.assertIsNone(quiz_store.get_quiz_by_id("quiz-c", LEGACY_USER_ID))

    def test_delete_missing_quiz_raises(self):
        with self.assertRaises(ValueError):
            delete_quiz("does-not-exist", LEGACY_USER_ID)


class QuizDeleteFrontendTests(unittest.TestCase):
    def test_frontend_has_confirmed_delete_action_separate_from_regenerate(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("async function deleteAssessmentQuiz()", script)
        self.assertIn("window.confirm(", script)
        self.assertIn('method: "DELETE"', script)
        self.assertIn("QUIZZES_API_URL}/${encodeURIComponent(quizId)}", script)
        self.assertIn('["Delete Quiz", "text-button danger-button", callbacks.remove]', script)
        self.assertIn("remove: deleteAssessmentQuiz", script)
        self.assertNotIn("regenerate: deleteAssessmentQuiz", script)


if __name__ == "__main__":
    unittest.main()
