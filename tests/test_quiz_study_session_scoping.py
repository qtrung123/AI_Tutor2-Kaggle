"""Regression coverage for document-scoped Study Session quiz lists."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_attempt_service, quiz_store


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
        quiz_attempt_service.submit_quiz_attempt(
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
        self.assertLess(detail_body.index("currentQuiz = null"), detail_body.index("requestQuizDetail(documentId, quizId)"))

    def test_the_old_quiz_progress_and_mastery_drawer_is_gone(self):
        """The old per-quiz "View Progress" drawer (Progress & Mastery) was removed; mastery lives in the
        Study Session's Progress tab and the dashboard, which do not depend on the open quiz."""
        script = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        for removed in ("renderPracticeMastery", "practice-mastery", "quizProgressTrigger", "setQuizProgressDrawerOpen",
                        "Mastery evidence is not available for this quiz.", "View Progress", "Progress &amp; Mastery"):
            self.assertNotIn(removed, script)
        self.assertIn("renderSessionProgress(documentId)", script)

    def test_switching_documents_still_rejects_stale_session_responses(self):
        """openStudySession's own stale-response guard (unrelated to quiz generation) is unchanged."""
        session_body = function_body("openStudySession")
        self.assertIn("if (activeDocumentId !== documentId) return", session_body)
        self.assertLess(session_body.index("activeDocumentId !== documentId"), session_body.index("quizDocumentSelect.value = documentId"))

    def test_an_explicit_generate_never_auto_loads_the_new_quiz_into_the_player(self):
        """Create Quiz sheet requirement: success closes the sheet and refreshes the Library: it
        never sets currentQuiz/currentAttempt or calls renderAssessmentQuiz -- the user opens the
        new quiz later with Start/Resume from its own Library card."""
        generation_body = function_body("generateAssessmentQuiz")
        self.assertEqual(generation_body.count("await requestGeneratedQuiz(generationRequest)"), 1)
        self.assertNotIn("currentQuiz = generatedQuiz", generation_body)
        # renderAssessmentQuiz() legitimately appears once, in the unrelated early-return guard for
        # an already-loaded quiz (see the "not regenerated by the shared button" test below) --
        # what matters here is that the SUCCESS path (after the generate call) never touches the
        # player state at all.
        success_path = generation_body[generation_body.index("await requestGeneratedQuiz(generationRequest)"):]
        for never in ("currentAttempt = null", "renderAssessmentQuiz()"):
            self.assertNotIn(never, success_path)
        self.assertIn("closeQuizCreateDialog({ force: true })", success_path)

    def test_an_already_loaded_quiz_is_not_regenerated_by_the_shared_button(self):
        """The same button/handler also sits inline next to an already-loaded quiz (Start/Resume
        from the Library put it there) -- that is a player-adjacent affordance, not the Create Quiz
        sheet, so it must not create a new artifact; it just re-shows what is already loaded."""
        generation_body = function_body("generateAssessmentQuiz")
        guard = generation_body.index("if (currentQuiz?.questions?.length)")
        early_return = generation_body.index("return;", guard)
        request_built = generation_body.index("selectedQuizGenerationRequest()")
        self.assertLess(guard, early_return)
        self.assertLess(early_return, request_built)
        self.assertIn("renderAssessmentQuiz()", generation_body[guard:early_return])

    def test_duplicate_submissions_and_a_generation_failure_are_handled_in_sheet(self):
        generation_body = function_body("generateAssessmentQuiz")
        self.assertIn("if (quizGenerationInFlight) return;", generation_body)
        guard_index = generation_body.index("if (quizGenerationInFlight) return;")
        self.assertLess(guard_index, generation_body.index("setQuizSheetGenerating(true)"))
        catch_index = generation_body.index("} catch (error) {")
        self.assertIn("showQuizSheetError(", generation_body[catch_index:])
        self.assertIn("setQuizSheetGenerating(false)", generation_body[catch_index:])
        # A partial result is still a success -- never routed through the error path.
        self.assertIn('generatedQuiz.status === "partial"', generation_body)
        self.assertIn("Grounded quality was prioritized", generation_body)

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
        self.assertIn("requestGeneratedQuiz(generationRequest)", generation_body)
        self.assertIn("const generationRequest = selectedQuizGenerationRequest()", regeneration_body)
        self.assertIn("quizGenerationRequestKey(generationRequest)", regeneration_body)   # regenerate still touches currentQuiz directly, so it still needs staleness protection
        for field in ("difficulty", "assessment_scope", "topic_id", "question_count", "model_id"):
            self.assertIn(f"{field}: request.{field}", regenerate_request_body)
        self.assertNotIn("selectedDifficulty()", request_body)
        self.assertNotIn("selectedQuestionCount()", request_body)


if __name__ == "__main__":
    unittest.main()
