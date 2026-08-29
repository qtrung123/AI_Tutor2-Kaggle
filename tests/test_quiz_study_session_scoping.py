"""Regression coverage for document-scoped Study Session quiz lists."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store


SCRIPT = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(rf"async function {name}\([^)]*\) \{{", SCRIPT)
    if not match:
        raise AssertionError(f"{name} was not found")
    start, depth = match.end(), 1
    for index in range(start, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start:index]
    raise AssertionError(f"{name} is not balanced")


def saved_quiz(document_id: str, quiz_id: str) -> dict:
    return {
        "quiz_id": quiz_id,
        "document_id": document_id,
        "difficulty": "easy",
        "topic_id": "topic_1",
        "topic_name": "Topic 1",
        "questions": [{
            "id": 1,
            "question": "Question?",
            "options": ["A. One", "B. Two", "C. Three", "D. Four"],
            "correct_answer": "A",
            "difficulty": "easy",
            "topic_id": "topic_1",
            "topic_name": "Topic 1",
            "concept_id": "concept_1",
            "source_chunk_ids": ["chunk_1"],
        }],
    }


class QuizStudySessionScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.db_patch.start()
        self.legacy_patches = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp.name) / "missing-explanations.json"),
        ]
        for item in self.legacy_patches:
            item.start()
        quiz_store.initialize_quiz_store()

    def tearDown(self):
        for item in reversed(self.legacy_patches):
            item.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def test_a_to_b_to_a_lists_only_persisted_quizzes_for_current_document(self):
        owner_id = "owner-1"
        quiz_store.save_quiz("A.pdf", "easy", saved_quiz("A.pdf", "quiz-a"), owner_id)
        quiz_service.submit_quiz_attempt(
            "A.pdf", "easy", "topic_1", {"1": "A"}, owner_id, quiz_id="quiz-a"
        )

        first_a = quiz_store.list_quiz_history(document_id="A.pdf", student_id=owner_id)
        document_b = quiz_store.list_quiz_history(document_id="B.pdf", student_id=owner_id)
        restored_a = quiz_store.list_quiz_history(document_id="A.pdf", student_id=owner_id)

        self.assertEqual([attempt["quiz_id"] for attempt in first_a], ["quiz-a"])
        self.assertEqual(document_b, [])
        self.assertEqual([attempt["quiz_id"] for attempt in restored_a], ["quiz-a"])

    def test_frontend_clears_stale_state_and_requests_current_document_history(self):
        switch_body = function_body("openStudySession")
        history_body = function_body("loadQuizHistory")
        detail_body = function_body("loadSelectedQuiz")

        self.assertLess(switch_body.index("currentQuiz = null"), switch_body.index("loadSelectedQuiz()"))
        self.assertLess(switch_body.index("quizHistory = []"), switch_body.index("loadQuizHistory(documentId)"))
        self.assertIn("new URLSearchParams({ document_id: requestDocumentId })", history_body)
        self.assertIn("documentId = quizDocumentSelect?.value || activeDocumentId", SCRIPT)
        self.assertIn("attempt.document_id === requestDocumentId", history_body)
        self.assertIn("!== requestDocumentId) return", history_body)
        self.assertLess(detail_body.index("currentQuiz = null"), detail_body.index("requestQuizDetail(documentId)"))


if __name__ == "__main__":
    unittest.main()
