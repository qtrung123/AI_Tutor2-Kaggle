"""Regression coverage for document-scoped Study Session quiz lists."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store


SCRIPT = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^)]*\) \{{", SCRIPT)
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

    def test_two_new_documents_without_attempts_render_as_unassessed(self):
        """A and B both use the generic null-attempt path; no document special case is allowed."""
        mastery_body = function_body("renderPracticeMastery")

        self.assertIn("currentAttempt?.mastery_by_topic || {}", mastery_body)
        self.assertIn("currentAttempt?.mastery", mastery_body)
        self.assertNotRegex(mastery_body, r"[AaBb]\.pdf|document_[ab]")

    def test_assessed_document_keeps_attempt_mastery_precedence(self):
        mastery_body = function_body("renderPracticeMastery")

        attempt_rows = mastery_body.index("currentAttempt?.mastery_by_topic")
        legacy_attempt_row = mastery_body.index("currentAttempt?.mastery)")
        dashboard_fallback = mastery_body.index("dashboardData.mastery || []")
        self.assertLess(attempt_rows, legacy_attempt_row)
        self.assertLess(legacy_attempt_row, dashboard_fallback)

    def test_switching_c_to_a_to_b_rejects_stale_quiz_and_session_responses(self):
        generation_body = function_body("generateAssessmentQuiz")
        session_body = function_body("openStudySession")

        self.assertIn("const requestedQuizKey = quizGenerationRequestKey(generationRequest)", generation_body)
        self.assertGreaterEqual(generation_body.count("requestedQuizKey !== currentQuizKey()"), 2)
        self.assertLess(generation_body.index("requestedQuizKey !== currentQuizKey()"), generation_body.index("currentQuiz = generatedQuiz"))
        self.assertIn("if (activeDocumentId !== documentId) return", session_body)
        self.assertLess(session_body.index("activeDocumentId !== documentId"), session_body.index("quizDocumentSelect.value = documentId"))

    def test_first_generated_quiz_renders_immediately_with_null_attempt(self):
        generation_body = function_body("generateAssessmentQuiz")
        mastery_body = function_body("renderPracticeMastery")

        self.assertEqual(generation_body.count("await requestGeneratedQuiz(generationRequest)"), 1)
        quiz_assignment = generation_body.index("currentQuiz = generatedQuiz")
        null_attempt = generation_body.index("currentAttempt = null", quiz_assignment)
        immediate_render = generation_body.index("renderAssessmentQuiz()", null_attempt)
        self.assertLess(quiz_assignment, null_attempt)
        self.assertLess(null_attempt, immediate_render)
        self.assertIn("currentAttempt?.mastery_by_topic || {}", mastery_body)
        self.assertIn("Mastery evidence is not available for this quiz.", mastery_body)

    def test_post_persistence_render_failure_is_not_reported_as_generation_failure(self):
        generation_body = function_body("generateAssessmentQuiz")

        persisted_guard = generation_body.index("if (generatedQuiz)")
        saved_message = generation_body.index("Quiz was generated and saved")
        clear_failed_generation = generation_body.index("currentQuiz = null", persisted_guard)
        generation_failed_message = generation_body.index("Quiz generation failed")
        self.assertLess(persisted_guard, saved_message)
        self.assertLess(saved_message, clear_failed_generation)
        self.assertLess(clear_failed_generation, generation_failed_message)
        self.assertIn("return;", generation_body[persisted_guard:clear_failed_generation])

    def test_generation_and_regeneration_send_one_immutable_ui_configuration(self):
        selection_body = function_body("selectedQuizGenerationRequest")
        request_body = function_body("requestGeneratedQuiz")
        generation_body = function_body("generateAssessmentQuiz")
        regeneration_body = function_body("regenerateAssessmentQuiz")
        regenerate_request_body = function_body("requestQuizRegeneration")

        for field in ("document_id", "assessment_scope", "topic_id", "difficulty", "question_count", "model_id"):
            self.assertIn(field, selection_body)
        self.assertIn("body: JSON.stringify(request)", request_body)
        self.assertIn("const generationRequest = selectedQuizGenerationRequest()", generation_body)
        self.assertIn("quizGenerationRequestKey(generationRequest)", generation_body)
        self.assertIn("requestGeneratedQuiz(generationRequest)", generation_body)
        self.assertIn("const generationRequest = selectedQuizGenerationRequest()", regeneration_body)
        for field in ("difficulty", "assessment_scope", "topic_id", "question_count", "model_id"):
            self.assertIn(f"{field}: request.{field}", regenerate_request_body)
        self.assertNotIn("selectedDifficulty()", request_body)
        self.assertNotIn("selectedQuestionCount()", request_body)


if __name__ == "__main__":
    unittest.main()
