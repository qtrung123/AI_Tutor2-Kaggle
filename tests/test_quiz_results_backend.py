"""Quiz Results backend: the Quiz Player's "Submit Anyway" (allow_unanswered) and the completed-
attempt payload the Results/Review screen reads (backend/quiz_service.load_completed_quiz_attempt).
Real SQLite database throughout, matching tests/test_quiz_player_progress.py.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from tests.test_quiz_player_progress import DOCUMENT_ID, two_question_quiz


class QuizResultsBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db"),
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp.name) / "missing-explanations.json"),
        ]
        for active in self.patches:
            active.start()
        self.owner = "owner-1"
        quiz_a = two_question_quiz("quiz-a")
        quiz_a["title"] = "Quiz A"
        quiz_a["assessment_plan"] = {"generation_model": {"model_id": "qwen-2.5-7b", "name": "Qwen 2.5 7B"}}
        quiz_a["questions"][0]["concept_name"] = "Concept One"
        quiz_b = two_question_quiz("quiz-b")
        quiz_b["title"] = "Quiz B"
        quiz_b["assessment_plan"] = {"generation_model": {"model_id": "gemma3-12b", "name": "Gemma 3 12B"}}
        quiz_store.save_quiz(DOCUMENT_ID, "easy", quiz_a, self.owner)
        quiz_store.save_quiz(DOCUMENT_ID, "easy", quiz_b, self.owner)

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.temp.cleanup()

    def submit(self, quiz_id, answers, **kwargs):
        return quiz_service.submit_quiz_attempt(
            DOCUMENT_ID, "easy", "document", answers, self.owner, quiz_id=quiz_id, **kwargs,
        )

    def test_complete_answer_set_is_still_required_without_allow_unanswered(self):
        with self.assertRaisesRegex(ValueError, "Every quiz question"):
            self.submit("quiz-a", {"1": "A"})

    def test_submit_anyway_persists_unanswered_questions_as_not_correct(self):
        saved = self.submit("quiz-a", {"1": "A"}, allow_unanswered=True)
        self.assertTrue(saved["completed"])
        self.assertEqual((saved["score"], saved["total"], saved["answered"]), (1, 2, 1))
        self.assertEqual(saved["percentage"], 50.0)
        # The unanswered question is not an answer, but keeps its graded review row.
        self.assertEqual(saved["answers"], {"1": "A"})
        by_id = {result["question_id"]: result for result in saved["question_results"]}
        self.assertTrue(by_id[1]["is_correct"])
        self.assertFalse(by_id[2]["is_correct"])
        self.assertEqual(by_id[2]["selected_answers"], [])
        self.assertEqual(by_id[2]["selected_answer"], "")
        self.assertEqual(by_id[2]["correct_answers"], ["B"])

    def test_submit_anyway_with_nothing_answered_and_empty_values(self):
        saved = self.submit("quiz-a", {"1": "", "2": []}, allow_unanswered=True)
        self.assertEqual((saved["score"], saved["answered"]), (0, 0))
        self.assertTrue(all(result["selected_answers"] == [] for result in saved["question_results"]))

    def test_submit_anyway_still_rejects_unknown_questions_and_invalid_letters(self):
        with self.assertRaises(ValueError):
            self.submit("quiz-a", {"9": "A"}, allow_unanswered=True)
        with self.assertRaises(ValueError):
            self.submit("quiz-a", {"1": "Z"}, allow_unanswered=True)

    def test_completed_attempt_carries_its_own_quiz_metadata(self):
        saved = self.submit("quiz-a", {"1": "A", "2": "C"})
        loaded = quiz_service.load_completed_quiz_attempt(saved["attempt_id"], self.owner)
        self.assertEqual(loaded["quiz_id"], "quiz-a")
        self.assertEqual(loaded["quiz"]["title"], "Quiz A")
        self.assertEqual(loaded["quiz"]["generation_model"]["name"], "Qwen 2.5 7B")
        self.assertEqual(loaded["quiz"]["question_count"], 2)
        self.assertFalse(loaded["quiz"]["partial"])
        by_id = {result["question_id"]: result for result in loaded["question_results"]}
        self.assertEqual(by_id[1]["concept_name"], "Concept One")
        # Persisted grading is returned unchanged.
        self.assertTrue(by_id[1]["is_correct"])
        self.assertFalse(by_id[2]["is_correct"])
        self.assertEqual(by_id[2]["selected_answers"], ["C"])
        self.assertEqual(by_id[1]["explanation"], "Supported.")
        self.assertEqual(by_id[1]["source_chunk_ids"], ["hash_1"])

    def test_sibling_attempts_each_resolve_their_own_quiz(self):
        attempt_a = self.submit("quiz-a", {"1": "A", "2": "B"})
        attempt_b = self.submit("quiz-b", {"1": "B", "2": "B"})
        loaded_a = quiz_service.load_completed_quiz_attempt(attempt_a["attempt_id"], self.owner)
        loaded_b = quiz_service.load_completed_quiz_attempt(attempt_b["attempt_id"], self.owner)
        self.assertEqual((loaded_a["quiz_id"], loaded_a["quiz"]["title"], loaded_a["score"]), ("quiz-a", "Quiz A", 2))
        self.assertEqual((loaded_b["quiz_id"], loaded_b["quiz"]["title"], loaded_b["score"]), ("quiz-b", "Quiz B", 1))
        self.assertEqual(loaded_b["quiz"]["generation_model"]["name"], "Gemma 3 12B")

    def test_unknown_or_foreign_attempt_is_not_found(self):
        saved = self.submit("quiz-a", {"1": "A", "2": "B"})
        with self.assertRaises(ValueError):
            quiz_service.load_completed_quiz_attempt("does-not-exist", self.owner)
        with self.assertRaises(ValueError):
            quiz_service.load_completed_quiz_attempt(saved["attempt_id"], "someone-else")

    def test_submit_request_model_accepts_allow_unanswered(self):
        from backend.main import QuizSubmitRequest
        request = QuizSubmitRequest(difficulty="easy", topic_id="document", quiz_id="quiz-a", answers={"1": "A"}, allow_unanswered=True)
        self.assertTrue(request.allow_unanswered)
        self.assertFalse(QuizSubmitRequest(difficulty="easy", topic_id="document", answers={}).allow_unanswered)


if __name__ == "__main__":
    unittest.main()
