"""Tests for the Quiz Custom Name feature (29/8 meeting notes).

Covers: request validation, persistence, API response shape, quiz
history/list display, quiz detail display, backward compatibility with
legacy (unnamed) quizzes, generation-content isolation, and ownership
isolation.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import quiz_service, quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.main import QuizGenerateRequest, QuizRegenerateRequest, app
from backend.quiz_service import (
    _generate_topic_quiz_v2,
    _resolve_quiz_title,
    list_completed_quiz_attempts,
)


# ---------------------------------------------------------------------------
# Fixtures shared by the "real generation" tests below (mirrors the mocking
# recipe already proven in tests/test_quiz_v2.py, kept self-contained here so
# this file does not depend on another test module's internals).
# ---------------------------------------------------------------------------

CHUNK = {
    "content": (
        "TCP acknowledgements support reliable delivery. Flow control protects receivers. "
        "Sequence numbers preserve ordering. Retransmission handles loss. Checksums detect corruption."
    ),
    "metadata": {
        "chunk_id": "canonical_chunk_1",
        "owner_id": "owner",
        "document_id": "lecture.pdf",
        "topic_id": "topic_001",
        "source": "lecture.pdf",
        "page": 1,
    },
}
DOCUMENT = {
    "id": "lecture.pdf",
    "title": "Lecture",
    "hash": "hash",
    "topic_schema_version": 2,
}
TOPIC = {"topic_id": "topic_001", "name": "Reliable transport"}


def raw_question(index: int) -> dict:
    stems = [
        "Why do acknowledgements improve reliable transport delivery?",
        "How does flow control protect a receiving endpoint?",
        "What role do sequence numbers play in ordered communication?",
        "When packet loss occurs, how does retransmission help?",
        "How can checksums reveal corruption during transport?",
    ]
    mechanisms = [
        "congestion recovery", "window scaling", "selective acknowledgement", "timeout estimation",
        "connection establishment", "receiver buffering", "segment framing", "duplicate detection",
        "loss recovery", "delivery confirmation", "sequence wraparound", "delayed acknowledgement",
        "adaptive retransmission", "ordered reassembly", "corruption detection", "flow regulation",
        "sender throttling", "round trip sampling", "state synchronization", "endpoint negotiation",
        "payload verification", "stream reconstruction", "failure recovery", "packet accounting",
        "transport coordination",
    ]
    stem = stems[index] if index < len(stems) else f"How does {mechanisms[index]} contribute to reliable communication?"
    return {
        "question": stem,
        "options": [
            f"It supports reliable communication {index + 1}",
            f"It disables receiver behavior {index + 1}",
            f"It removes ordering behavior {index + 1}",
            f"It prevents delivery behavior {index + 1}",
        ],
        "correct_answer": 0,
        "explanation": "The selected evidence directly supports the first option.",
        "slot_id": f"S{index + 1}",
    }


class FakeBatchModel:
    payloads = []

    def __init__(self, **kwargs):
        pass

    def invoke(self, prompt):
        return SimpleNamespace(
            content=json.dumps(self.__class__.payloads.pop(0)),
            response_metadata={
                "load_duration": 1, "prompt_eval_duration": 1, "eval_duration": 1,
                "prompt_eval_count": 1, "eval_count": 1,
            },
        )


def run_v2(quiz_title, question_count=10):
    """Run the real _generate_topic_quiz_v2 pipeline (and real quiz_store persistence,
    against whatever quiz_store.DATABASE_PATH the caller has patched) with only the
    LLM and concept-plan cache mocked out."""
    FakeBatchModel.payloads = [{"questions": [raw_question(index) for index in range(question_count)]}]
    with (
        patch.object(quiz_service, "get_topic_chunks", return_value=[CHUNK]),
        patch.object(quiz_service, "ChatOllama", FakeBatchModel),
        patch.object(quiz_service, "get_cached_concept_plan", return_value=None),
        patch.object(quiz_service, "save_cached_concept_plan"),
        patch.object(quiz_service, "validate_question_semantics") as semantic,
        patch.object(quiz_service, "save_quiz_validation_event"),
    ):
        result = _generate_topic_quiz_v2(
            DOCUMENT, TOPIC, "easy", "owner", "qwen-test", False, question_count, quiz_title=quiz_title,
        )
    semantic.assert_not_called()
    return result


class QuizNameRequestModelTests(unittest.TestCase):
    """Requirement 4 (API): request/response models carry the custom name."""

    def test_generate_request_requires_non_empty_quiz_name(self):
        self.assertIn("quiz_name", QuizGenerateRequest.model_fields)
        with self.assertRaises(Exception):
            QuizGenerateRequest(
                document_id="doc.pdf", assessment_scope="document", difficulty="easy",
            )

    def test_generate_request_accepts_a_provided_quiz_name(self):
        request = QuizGenerateRequest(
            document_id="doc.pdf", assessment_scope="document", difficulty="easy",
            quiz_name="Midterm Embedded Systems",
        )
        self.assertEqual(request.quiz_name, "Midterm Embedded Systems")

    def test_regenerate_request_quiz_name_is_optional(self):
        request = QuizRegenerateRequest(difficulty="easy", assessment_scope="document")
        self.assertIsNone(request.quiz_name)


class ResolveQuizTitleTests(unittest.TestCase):
    """Pure-function tests for the title-resolution rule generate_quiz() uses."""

    def test_explicit_name_wins_and_is_trimmed(self):
        self.assertEqual(_resolve_quiz_title("  Midterm Review  ", "Old Name", "fallback.pdf"), "Midterm Review")

    def test_falls_back_to_previous_title_when_no_name_given(self):
        self.assertEqual(_resolve_quiz_title(None, "Previous Custom Name", "fallback.pdf"), "Previous Custom Name")
        self.assertEqual(_resolve_quiz_title("   ", "Previous Custom Name", "fallback.pdf"), "Previous Custom Name")

    def test_falls_back_to_document_title_when_nothing_else_is_set(self):
        self.assertEqual(_resolve_quiz_title(None, None, "fallback.pdf"), "fallback.pdf")
        self.assertEqual(_resolve_quiz_title("   ", "   ", "fallback.pdf"), "fallback.pdf")


class QuizNameApiRouteTests(unittest.TestCase):
    """Requirement 2 (validation) and 4 (API) at the HTTP layer, service mocked out."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "auth.db"
        self.patchers = [patch("backend.main.generate_quiz")]
        import backend.auth_store as auth_store
        self.patchers.append(patch.object(auth_store, "DATABASE_PATH", self.database_path))
        import backend.conversation_store as conversation_store
        self.patchers.append(patch.object(conversation_store, "DATABASE_PATH", self.database_path))
        self.patchers.append(patch.object(quiz_store, "DATABASE_PATH", self.database_path))
        import backend.indexed_document_store as indexed_document_store
        self.patchers.append(patch.object(indexed_document_store, "DATABASE_PATH", self.database_path))
        self.patchers.append(
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp_dir.name) / "indexed_files.json")
        )
        self.mock_generate_quiz = None
        started = [patcher.start() for patcher in self.patchers]
        self.mock_generate_quiz = started[0]
        self.mock_generate_quiz.return_value = {
            "quiz_id": "quiz-1", "document_id": "doc.pdf", "document_hash": "hash",
            "title": "Midterm Embedded Systems", "difficulty": "easy", "topic_id": "document",
            "topic_name": "Entire document", "assessment_scope": "document", "assessment_plan": {},
            "created_at": "2026-01-01T00:00:00+00:00", "question_count": 0, "questions": [],
        }

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _signed_in_client(self):
        client = TestClient(app)
        signup = client.post("/api/auth/signup", json={
            "display_name": "Quiz Namer", "email": "namer@example.com", "password": "long-password-123",
        })
        self.assertEqual(signup.status_code, 201)
        return client

    def test_whitespace_only_quiz_name_is_rejected_before_reaching_the_service(self):
        client = self._signed_in_client()
        response = client.post("/api/quiz/generate", json={
            "document_id": "doc.pdf", "assessment_scope": "document", "difficulty": "easy",
            "quiz_name": "   ",
        })
        self.assertEqual(response.status_code, 400)
        self.mock_generate_quiz.assert_not_called()

    def test_missing_quiz_name_is_rejected_by_pydantic_validation(self):
        client = self._signed_in_client()
        response = client.post("/api/quiz/generate", json={
            "document_id": "doc.pdf", "assessment_scope": "document", "difficulty": "easy",
        })
        self.assertEqual(response.status_code, 422)
        self.mock_generate_quiz.assert_not_called()

    def test_trimmed_quiz_name_flows_from_request_to_service_and_back_in_response(self):
        client = self._signed_in_client()
        response = client.post("/api/quiz/generate", json={
            "document_id": "doc.pdf", "assessment_scope": "document", "difficulty": "easy",
            "quiz_name": "  Midterm Embedded Systems  ",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Midterm Embedded Systems")
        called_kwargs = self.mock_generate_quiz.call_args.kwargs
        self.assertEqual(called_kwargs["quiz_name"], "Midterm Embedded Systems")

    def test_regenerate_without_a_name_passes_none_to_preserve_existing_name(self):
        client = self._signed_in_client()
        response = client.post("/api/quiz/doc.pdf/regenerate", json={
            "difficulty": "easy", "assessment_scope": "document",
        })
        self.assertEqual(response.status_code, 200)
        called_kwargs = self.mock_generate_quiz.call_args.kwargs
        self.assertIsNone(called_kwargs["quiz_name"])


class QuizNameGenerationTests(unittest.TestCase):
    """Requirement 3 (persistence) and the 'must not affect generation' guardrail."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "quiz.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.legacy_patches = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp_dir.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp_dir.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp_dir.name) / "missing-explanations.json"),
        ]
        for legacy_patch in self.legacy_patches:
            legacy_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        for legacy_patch in self.legacy_patches:
            legacy_patch.stop()
        self.temp_dir.cleanup()

    def test_custom_name_is_persisted_and_returned_by_get_quiz(self):
        result = run_v2(quiz_title="Midterm Embedded Systems")
        self.assertEqual(result["title"], "Midterm Embedded Systems")

        reloaded = quiz_store.get_quiz("lecture.pdf", "easy", "topic_001", "owner")
        self.assertEqual(reloaded["title"], "Midterm Embedded Systems")
        by_id = quiz_store.get_quiz_by_id(result["quiz_id"], "owner")
        self.assertEqual(by_id["title"], "Midterm Embedded Systems")

    def test_custom_name_does_not_alter_generated_question_content(self):
        named_result = run_v2(quiz_title="Midterm Embedded Systems")
        unnamed_result = run_v2(quiz_title=None)

        def strip_identity(quiz):
            return [
                {key: value for key, value in question.items() if key not in {"id"}}
                for question in quiz["questions"]
            ]

        self.assertEqual(strip_identity(named_result), strip_identity(unnamed_result))
        self.assertNotEqual(named_result["title"], unnamed_result["title"])

    def test_legacy_quiz_without_custom_name_still_has_a_usable_title(self):
        legacy_quiz = {
            "quiz_id": "legacy-quiz", "document_id": "legacy.pdf", "title": "legacy.pdf",
            "difficulty": "easy", "topic_id": "topic_1", "topic_name": "Topic 1",
            "questions": [{
                "id": 1, "question": "Legacy question?", "options": ["A. One", "B. Two", "C. Three", "D. Four"],
                "correct_answer": "A", "difficulty": "easy", "topic_id": "topic_1", "topic_name": "Topic 1",
                "explanation": "Legacy.", "source_chunk_ids": ["chunk_1"],
            }],
        }
        quiz_store.save_quiz("legacy.pdf", "easy", legacy_quiz, LEGACY_USER_ID)
        reloaded = quiz_store.get_quiz_by_id("legacy-quiz", LEGACY_USER_ID)
        self.assertTrue(str(reloaded["title"]).strip())
        self.assertEqual(reloaded["title"], "legacy.pdf")


class QuizNameHistoryDisplayTests(unittest.TestCase):
    """Requirements 5/7: custom name in the quiz history list, with a safe legacy fallback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "quiz.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.legacy_patches = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp_dir.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp_dir.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp_dir.name) / "missing-explanations.json"),
        ]
        for legacy_patch in self.legacy_patches:
            legacy_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        for legacy_patch in self.legacy_patches:
            legacy_patch.stop()
        self.temp_dir.cleanup()

    def named_quiz(self, quiz_id, title, topic_id="topic_1"):
        return {
            "quiz_id": quiz_id, "document_id": "lecture.pdf", "title": title,
            "difficulty": "easy", "topic_id": topic_id, "topic_name": "Topic",
            "questions": [{
                "id": index, "question": f"Question {index}?",
                "options": ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"], "correct_answer": correct,
                "difficulty": "easy", "topic_id": topic_id, "topic_name": "Topic",
                "explanation": f"Explanation {index}", "source_chunk_ids": [f"chunk_{index}"],
            } for index, correct in enumerate(("A", "B", "C"), start=1)],
        }

    def test_custom_name_appears_in_quiz_history_summary(self):
        quiz_store.save_quiz("lecture.pdf", "easy", self.named_quiz("quiz-a", "Embedded Systems Midterm"), LEGACY_USER_ID)
        quiz_service.submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"},
            LEGACY_USER_ID, quiz_id="quiz-a",
        )
        summaries = list_completed_quiz_attempts(student_id=LEGACY_USER_ID)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "Embedded Systems Midterm")

    def test_legacy_attempt_with_no_matching_quiz_row_falls_back_to_untitled(self):
        # Simulates a completed attempt whose quiz row is no longer resolvable
        # (e.g. an old legacy-imported attempt) -- history must still render.
        quiz_store.save_quiz("lecture.pdf", "easy", self.named_quiz("quiz-orphan", "Temporary Name"), LEGACY_USER_ID)
        quiz_service.submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID, quiz_id="quiz-orphan",
        )
        with quiz_store._connect() as connection:
            connection.execute("DELETE FROM quizzes WHERE quiz_id = ?", ("quiz-orphan",))

        summaries = list_completed_quiz_attempts(student_id=LEGACY_USER_ID)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "Untitled Quiz")

    def test_multiple_quizzes_from_same_document_get_distinct_names(self):
        quiz_store.save_quiz("lecture.pdf", "easy", self.named_quiz("quiz-a", "Midterm Review", "topic_1"), LEGACY_USER_ID)
        quiz_store.save_quiz("lecture.pdf", "medium", self.named_quiz("quiz-b", "Final Exam Prep", "topic_2"), LEGACY_USER_ID)
        quiz_service.submit_quiz_attempt("lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID, quiz_id="quiz-a")
        quiz_service.submit_quiz_attempt("lecture.pdf", "medium", "topic_2", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID, quiz_id="quiz-b")
        titles = {summary["title"] for summary in list_completed_quiz_attempts(student_id=LEGACY_USER_ID)}
        self.assertEqual(titles, {"Midterm Review", "Final Exam Prep"})

    def test_ownership_isolation_of_quiz_titles(self):
        quiz_store.save_quiz("lecture.pdf", "easy", self.named_quiz("quiz-owner1", "Owner One's Quiz"), "owner-1")
        quiz_store.save_quiz("lecture.pdf", "easy", self.named_quiz("quiz-owner2", "Owner Two's Quiz"), "owner-2")
        titles_for_owner1 = quiz_store.get_quiz_titles(["quiz-owner1", "quiz-owner2"], "owner-1")
        self.assertEqual(titles_for_owner1, {"quiz-owner1": "Owner One's Quiz"})
        titles_for_owner2 = quiz_store.get_quiz_titles(["quiz-owner1", "quiz-owner2"], "owner-2")
        self.assertEqual(titles_for_owner2, {"quiz-owner2": "Owner Two's Quiz"})


class QuizNameFrontendWiringTests(unittest.TestCase):
    """Verifies the UI actually exposes, sends, validates, and displays the field."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.script = (root / "frontend" / "app.js").read_text(encoding="utf-8")

    def test_quiz_name_field_exists_in_the_create_quiz_form(self):
        self.assertIn('id="quiz-name-input"', self.markup)
        self.assertIn("<span>Quiz name</span>", self.markup)

    def test_request_body_includes_the_trimmed_quiz_name(self):
        self.assertIn("quiz_name: selectedQuizName()", self.script)
        self.assertIn('(quizNameInput?.value || "").trim()', self.script)

    def test_empty_name_is_rejected_client_side_before_calling_the_api(self):
        self.assertIn('if (!generationRequest.quiz_name) {', self.script)
        self.assertIn('showToast("Enter a quiz name")', self.script)

    def test_quiz_detail_title_uses_the_custom_name(self):
        self.assertIn('const quizName = (quiz.title || "").trim() || "Untitled Quiz";', self.script)

    def test_quiz_history_card_title_uses_the_custom_name(self):
        self.assertIn('title.textContent = (attempt.title || "").trim() || "Untitled Quiz";', self.script)


if __name__ == "__main__":
    unittest.main()
