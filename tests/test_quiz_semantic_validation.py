import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import quiz_store
from backend.quiz_service import _generate_quiz_batch
from backend.quiz_validation import SemanticValidationResult, validate_question_semantics


CHUNK = {
    "content": "TCP uses acknowledgements to support reliable delivery.",
    "metadata": {"chunk_id": "hash_1", "source": "lecture.pdf", "page": 0},
}
RAW_QUESTION = {
    "questions": [{
        "question": "How do acknowledgements contribute to the TCP behavior described here?",
        "options": [
            "A. They support reliable delivery",
            "B. They make delivery intentionally unreliable",
            "C. They replace transport delivery with connection naming",
            "D. They prevent delivery confirmation from being used",
        ],
        "correct_answer": "A",
        "explanation": "The cited material connects acknowledgements with reliable delivery.",
        "source_chunk_ids": ["hash_1"],
    }]
}


class FakeJudge:
    verdict = {}

    def __init__(self, **_kwargs):
        pass

    def invoke(self, _prompt):
        return SimpleNamespace(content=json.dumps(self.verdict))


class FakeGenerator:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, _prompt):
        return SimpleNamespace(content=json.dumps(RAW_QUESTION), response_metadata={"done_reason": "stop"})


def result(hard=(), quality=()):
    verdict = {name: name not in set(hard) for name in (
        "question_supported", "correct_answer_supported", "explanation_supported"
    )}
    verdict.update({name: name not in set(quality) for name in (
        "meaningful_concept", "distractor_quality", "difficulty_match"
    )})
    verdict.update({"evidence_chunk_ids": ["hash_1"], "reasons": list(hard) + list(quality)})
    return SemanticValidationResult(
        True, "judge", "semantic_grounding_v1", verdict, ["hash_1"], list(hard), list(quality), 3
    )


class QuizSemanticValidationTests(unittest.TestCase):
    def test_accepted_semantic_verdict(self):
        FakeJudge.verdict = {
            "question_supported": True,
            "correct_answer_supported": True,
            "explanation_supported": True,
            "meaningful_concept": True,
            "distractor_quality": True,
            "difficulty_match": True,
            "detected_difficulty": "easy",
            "evidence_chunk_ids": ["hash_1"],
            "reasons": [],
        }
        verdict = validate_question_semantics(
            RAW_QUESTION["questions"][0], [CHUNK], "easy", "TCP", llm_factory=FakeJudge
        )
        self.assertTrue(verdict.hard_passed)
        self.assertTrue(verdict.quality_passed)

    def test_invalid_validator_evidence_is_a_hard_failure(self):
        FakeJudge.verdict = {
            "question_supported": True,
            "correct_answer_supported": True,
            "explanation_supported": True,
            "meaningful_concept": True,
            "distractor_quality": True,
            "difficulty_match": True,
            "evidence_chunk_ids": ["not_cited_9"],
            "reasons": [],
        }
        verdict = validate_question_semantics(
            RAW_QUESTION["questions"][0], [CHUNK], "easy", "TCP", llm_factory=FakeJudge
        )
        self.assertIn("invalid_evidence_ids:not_cited_9", verdict.hard_failures)

    def test_hard_rejection_retries_then_accepts(self):
        events = []
        verdicts = [result(hard=("correct_answer_supported",)), result()]
        with patch("backend.quiz_service.ChatOllama", FakeGenerator), \
             patch("backend.quiz_service.validate_question_semantics", side_effect=verdicts), \
             patch("backend.quiz_service.save_quiz_validation_event", side_effect=lambda event: events.append(event)):
            questions = _generate_quiz_batch(
                "lecture.pdf", 1, "easy", [CHUNK], 1, "topic_001", "TCP", [], "run-hard", 1, "doc-hash", 2
            )
        self.assertEqual(len(questions), 1)
        self.assertEqual(events[0]["outcome"], "rejected_hard")
        self.assertIn("correct_answer_supported", events[0]["rejection_reasons"])
        self.assertEqual(events[1]["outcome"], "accepted")

    def test_quality_failure_has_one_retry_then_accepts_warning(self):
        events = []
        verdicts = [result(quality=("meaningful_concept",)), result(quality=("meaningful_concept",))]
        with patch("backend.quiz_service.ChatOllama", FakeGenerator), \
             patch("backend.quiz_service.validate_question_semantics", side_effect=verdicts), \
             patch("backend.quiz_service.save_quiz_validation_event", side_effect=lambda event: events.append(event)):
            questions = _generate_quiz_batch(
                "lecture.pdf", 1, "easy", [CHUNK], 1, "topic_001", "TCP", [], "run-quality", 1, "doc-hash", 2
            )
        self.assertEqual(len(questions), 1)
        self.assertEqual([event["outcome"] for event in events], ["rejected_quality_retry", "accepted_quality_warning"])

    def test_validation_log_persists_research_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            quiz_store, "DATABASE_PATH", Path(directory) / "quiz.db"
        ):
            quiz_store.save_quiz_validation_event({
                "generation_run_id": "run-1", "document_id": "lecture.pdf",
                "document_hash": "doc-hash", "topic_id": "topic_001",
                "topic_schema_version": 2, "difficulty": "easy", "batch_index": 1,
                "generation_attempt": 1, "candidate_index": 0,
                "generator_model": "generator", "generation_prompt_version": "topic_mcq_v1",
                "validator_model": "judge", "validator_prompt_version": "semantic_grounding_v1",
                "candidate_question": RAW_QUESTION["questions"][0], "cited_chunk_ids": ["hash_1"],
                "evidence_chunk_ids": ["hash_1"], "hard_passed": True, "quality_passed": True,
                "accepted": True, "outcome": "accepted", "verdict": {"question_supported": True},
                "rejection_reasons": [], "latency_ms": 4,
            })
            rows = quiz_store.list_quiz_validation_events("run-1")
        self.assertEqual(rows[0]["generator_model"], "generator")
        self.assertEqual(rows[0]["topic_schema_version"], 2)
        self.assertEqual(json.loads(rows[0]["evidence_chunk_ids_json"]), ["hash_1"])


if __name__ == "__main__":
    unittest.main()
