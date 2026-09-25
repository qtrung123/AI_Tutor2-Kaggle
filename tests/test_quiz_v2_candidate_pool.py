"""Quiz Generation V2: candidate pool, selection, and partial-persistence behavior.

These tests exercise the architecture described in the Quiz Generation V2 design: question
quality, evidence grounding, and concept coverage outrank raw question count. A candidate pool
below the requested count is persisted as a partial quiz instead of failing the whole request
closed, and duplicate/invalid candidates never reach the final quiz. Each test below is labeled
with the spec case it covers (A-J).
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import quiz_store
from backend.quiz_legacy_v2 import _generate_topic_quiz_v2
from backend.quiz_service import QuizGenerationError


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
DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
TOPIC = {"topic_id": "topic_001", "name": "Reliable transport"}


def raw_question(index: int) -> dict:
    """One structurally valid, distinct candidate bound to slot S{index + 1}."""
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
    ]
    stem = stems[index] if index < len(stems) else f"How does {mechanisms[index % len(mechanisms)]} contribute to reliable communication {index}?"
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
    prompts = []

    def __init__(self, **kwargs):
        pass

    def invoke(self, prompt):
        self.__class__.prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(self.__class__.payloads.pop(0)), response_metadata={})


class QuizV2CandidatePoolTests(unittest.TestCase):
    def setUp(self):
        FakeBatchModel.payloads = []
        FakeBatchModel.prompts = []

    def run_v2(self, payloads, question_count=12):
        FakeBatchModel.payloads = list(payloads)
        saved = []
        with (
            patch("backend.document_retrieval.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_legacy_v2.ChatOllama", FakeBatchModel),
            patch("backend.quiz_legacy_v2.get_cached_concept_plan", return_value=None),
            patch("backend.quiz_legacy_v2.save_cached_concept_plan"),
            patch("backend.quiz_legacy_v2.save_quiz_validation_event"),
            patch("backend.quiz_legacy_v2.save_quiz", side_effect=lambda _d, _x, quiz, _o: saved.append(quiz) or quiz),
        ):
            result = _generate_topic_quiz_v2(
                DOCUMENT, TOPIC, "easy", "owner", "qwen-2.5-3b-runtime", False, question_count,
            )
        return result, saved

    # --- Case A: requested=12, valid=12 -> complete -------------------------------------------
    def test_case_a_full_pool_on_first_call_is_complete(self):
        result, saved = self.run_v2([{"questions": [raw_question(index) for index in range(12)]}])
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(plan["status"], "complete")
        self.assertFalse(plan["partial"])
        self.assertEqual(plan["requested_count"], 12)
        self.assertEqual(plan["actual_count"], 12)
        self.assertEqual(plan["missing_count"], 0)
        self.assertEqual(len(saved), 1)

    # --- Case B: requested=12, valid=10, targeted fill produces 2 -> complete ------------------
    def test_case_b_targeted_fill_tops_up_to_complete(self):
        result, saved = self.run_v2([
            {"questions": [raw_question(index) for index in range(10)]},
            {"questions": [raw_question(10), raw_question(11)]},
        ])
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(plan["status"], "complete")
        self.assertFalse(plan["partial"])
        self.assertEqual(plan["actual_count"], 12)
        self.assertEqual(plan["missing_count"], 0)
        self.assertEqual(plan["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    # --- Case C: requested=12, valid=10, fill produces 0 -> partial 10/12 ----------------------
    def test_case_c_fill_exhausted_persists_partial_ten_of_twelve(self):
        result, saved = self.run_v2([
            {"questions": [raw_question(index) for index in range(10)]},
            {"questions": []}, {"questions": []}, {"questions": []}, {"questions": []},
        ])
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(plan["status"], "partial")
        self.assertTrue(plan["partial"])
        self.assertEqual(plan["requested_count"], 12)
        self.assertEqual(plan["actual_count"], 10)
        self.assertEqual(plan["missing_count"], 2)
        # No fabricated deterministic filler is used to reach the requested count.
        self.assertEqual(plan["timings_ms"]["deterministic_fallback_count"], 0)
        self.assertEqual(len(saved), 1)

    # --- Case D: requested=12, valid=1 -> partial 1/12 -----------------------------------------
    def test_case_d_single_valid_candidate_persists_partial_one_of_twelve(self):
        result, saved = self.run_v2([
            {"questions": [raw_question(0)]},
            {"questions": []}, {"questions": []}, {"questions": []}, {"questions": []},
        ])
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(plan["status"], "partial")
        self.assertEqual(plan["requested_count"], 12)
        self.assertEqual(plan["actual_count"], 1)
        self.assertEqual(plan["missing_count"], 11)
        self.assertEqual(len(saved), 1)

    # --- Case E: requested=12, valid=0 -> failure, no quiz created -----------------------------
    def test_case_e_zero_valid_candidates_fails_without_persisting(self):
        with (
            patch("backend.document_retrieval.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_legacy_v2.ChatOllama", FakeBatchModel),
            patch("backend.quiz_legacy_v2.get_cached_concept_plan", return_value=None),
            patch("backend.quiz_legacy_v2.save_cached_concept_plan"),
            patch("backend.quiz_legacy_v2.save_quiz_validation_event"),
            patch("backend.quiz_legacy_v2.save_quiz") as save_quiz_mock,
        ):
            FakeBatchModel.payloads = [{"questions": []}]
            with self.assertRaises(QuizGenerationError) as failure:
                _generate_topic_quiz_v2(DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False, 12)
            save_quiz_mock.assert_not_called()
        self.assertEqual(failure.exception.detail["valid_questions"], 0)
        self.assertEqual(failure.exception.detail["requested_count"], 12)
        self.assertEqual(failure.exception.detail["missing_count"], 12)

    # --- Case F: duplicate candidates are removed from the pool --------------------------------
    def test_case_f_duplicate_candidate_is_excluded_from_the_pool(self):
        candidates = [raw_question(index) for index in range(10)]
        duplicate = raw_question(0)
        duplicate["slot_id"] = "S11"  # same question text, different slot -- still a duplicate.
        result, _saved = self.run_v2([{"questions": [*candidates, duplicate]}])
        questions = [question["question"] for question in result["questions"]]
        self.assertEqual(len(questions), len(set(questions)))
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(result["assessment_plan"]["status"], "partial")

    # --- Case G: invalid candidates are removed from the pool -----------------------------------
    def test_case_g_structurally_invalid_candidate_is_excluded_from_the_pool(self):
        candidates = [raw_question(index) for index in range(10)]
        invalid = raw_question(10)
        invalid["options"] = invalid["options"][:3]  # single_choice requires exactly 4 options.
        result, _saved = self.run_v2([{"questions": [*candidates, invalid]}])
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(result["assessment_plan"]["validation_results"]["hard_rejections"], 1)
        self.assertEqual(result["assessment_plan"]["status"], "partial")

    # --- Case H: partial quiz persists correctly -------------------------------------------------
    def test_case_h_partial_quiz_round_trips_through_the_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            with (
                patch.object(quiz_store, "DATABASE_PATH", temp_path / "quiz.db"),
                patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", temp_path / "missing-quizzes.json"),
                patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", temp_path / "missing-attempts.json"),
                patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", temp_path / "missing-explanations.json"),
                patch("backend.document_retrieval.get_topic_chunks", return_value=[CHUNK]),
                patch("backend.quiz_legacy_v2.ChatOllama", FakeBatchModel),
                patch("backend.quiz_legacy_v2.get_cached_concept_plan", return_value=None),
                patch("backend.quiz_legacy_v2.save_cached_concept_plan"),
                patch("backend.quiz_legacy_v2.save_quiz_validation_event"),
            ):
                quiz_store.initialize_quiz_store()
                FakeBatchModel.payloads = [
                    {"questions": [raw_question(index) for index in range(10)]},
                    {"questions": []}, {"questions": []}, {"questions": []}, {"questions": []},
                ]
                result = _generate_topic_quiz_v2(DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False, 12)
                restored = quiz_store.get_quiz_by_id(result["quiz_id"], "owner")
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored["questions"]), 10)
        self.assertEqual(restored["question_count"], 10)
        self.assertEqual(restored["assessment_plan"]["status"], "partial")
        self.assertEqual(restored["assessment_plan"]["requested_count"], 12)
        self.assertEqual(restored["assessment_plan"]["actual_count"], 10)
        self.assertEqual(restored["assessment_plan"]["missing_count"], 2)

    # --- Case I: frontend surfaces actual_count/requested_count for a partial quiz -------------
    def test_case_i_frontend_displays_actual_and_requested_counts(self):
        frontend = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("function quizPartialSuffix(quiz)", frontend)
        self.assertIn("plan.actual_count", frontend)
        self.assertIn("plan.requested_count", frontend)
        self.assertIn("questions generated", frontend)
        self.assertIn("assessmentTitleText(quiz)", frontend)

    # --- Case J: existing full-quiz behavior remains intact -------------------------------------
    def test_case_j_complete_quiz_keeps_legacy_fields_consistent(self):
        result, _saved = self.run_v2([{"questions": [raw_question(index) for index in range(12)]}])
        plan = result["assessment_plan"]
        # Legacy fields (pre-dating V2 partial support) must still be present and correct so
        # existing consumers of a *complete* quiz see no behavior change.
        self.assertEqual(plan["target_questions"], 12)
        self.assertEqual(plan["total_questions"], 12)
        self.assertFalse(plan["partial"])
        self.assertEqual(result["question_count"], 12)
        self.assertEqual(len(result["questions"]), 12)


if __name__ == "__main__":
    unittest.main()
