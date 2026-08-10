import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from backend.quiz_validation import SemanticValidationResult


def chunk(chunk_id):
    return {"content": f"Evidence for {chunk_id}.", "metadata": {"chunk_id": chunk_id}}


def verdict(hard=True):
    return SemanticValidationResult(
        True, "judge", "semantic_grounding_v2_backend_evidence", {}, ["owned"],
        [] if hard else ["question_supported"], [], 1,
    )


class _Response:
    def __init__(self, content):
        self.content = content


class QuizLatencyOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp_dir.name) / "quiz.db")
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_plan_cache_reuses_evidence_and_invalidates_on_hash_or_schema(self):
        payload = {"planned_topics": [], "excluded_topic_ids": [], "topic_chunks": {"t": [chunk("c1")]}}
        quiz_store.save_assessment_plan_cache("alice", "doc", "t", 2, "planner", "hash-a", payload)
        self.assertEqual(quiz_store.get_assessment_plan_cache("alice", "doc", "t", 2, "planner", "hash-a"), payload)
        self.assertIsNone(quiz_store.get_assessment_plan_cache("alice", "doc", "t", 2, "planner", "hash-b"))
        self.assertIsNone(quiz_store.get_assessment_plan_cache("alice", "doc", "t", 3, "planner", "hash-a"))
        self.assertIsNone(quiz_store.get_assessment_plan_cache("bob", "doc", "t", 2, "planner", "hash-a"))

    def test_regenerate_uses_fast_cached_plan_path(self):
        document = {"id": "doc", "hash": "hash", "topic_schema_version": 2,
                    "topics": [{"topic_id": "t", "name": "Topic"}]}
        plan = {"topic_id": "t", "topic_name": "Topic", "assessment_capacity": 1,
                "allocated_questions": 0,
                "concepts": [{"concept_id": "c1", "name": "Concept", "source_chunk_ids": ["source"]}]}

        def generated(_doc, difficulty, items, start_id, *_args):
            item = items[0]
            return ([{"id": start_id, "question": "Which evidence-backed statement correctly describes this concept in the topic?",
                      "options": ["A. Yes", "B. No", "C. Maybe", "D. Never"], "correct_answer": "A",
                      "explanation": "Evidence supports Yes.", "difficulty": difficulty,
                      "topic_id": "t", "topic_name": "Topic", "concept_id": "c1", "concept_name": "Concept",
                      "assessment_capacity": 1, "source_chunk_ids": ["source"], "validation_outcome": "accepted"}], [])

        with patch.object(quiz_service, "_document_lookup", return_value={"doc": document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "get_topic_chunks", return_value=[chunk("source")]) as retrieve, \
             patch.object(quiz_service, "build_topic_plan", return_value=plan) as planner, \
             patch.object(quiz_service, "_generate_concept_batch", side_effect=generated), \
             patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _owner: quiz), \
             patch.object(quiz_service, "delete_document_attempts"):
            first = quiz_service.generate_quiz("doc", "easy", "topic", "t", owner_id="alice")
            second = quiz_service.generate_quiz("doc", "easy", "topic", "t", regenerate=True, owner_id="alice")
        self.assertFalse(first["performance_metrics"]["assessment_plan_cache_hit"])
        self.assertTrue(second["performance_metrics"]["assessment_plan_cache_hit"])
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(planner.call_count, 1)

    def test_generation_and_validation_are_batched_with_backend_provenance(self):
        items = [{
            "concept": {"concept_id": f"c{i}", "name": f"Concept {i}"},
            "evidence_chunks": [chunk(f"source-{i}")], "topic_id": "topic",
            "topic_name": "Topic", "assessment_capacity": 4,
        } for i in range(4)]
        calls = []

        class Generator:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            def invoke(self, _prompt):
                return _Response(json.dumps({"questions": [{
                    "concept_id": f"c{i}", "question": f"Which statement correctly describes the evidence-backed concept number {i} in this topic?",
                    "options": ["A. Yes", "B. No", "C. Maybe", "D. Never"],
                    "correct_answer": "A", "explanation": "Evidence supports Yes.",
                } for i in range(4)]}))

        metrics = {"llm_calls": 0, "generation_ms": 0, "validation_ms": 0,
                   "validation_llm_calls": 0, "retry_llm_calls": 0}
        with patch.object(quiz_service, "ChatOllama", Generator), \
             patch.object(quiz_service, "validate_questions_semantics", side_effect=lambda rows: [verdict() for _ in rows]) as validate, \
             patch.object(quiz_service, "_is_duplicate_question", return_value=False), \
             patch.object(quiz_service, "save_quiz_validation_event"):
            questions, errors = quiz_service._generate_concept_batch(
                "doc", "easy", items, 1, [], "run", 1, "hash", 2, "alice", metrics)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual([q["source_chunk_ids"] for q in questions], [[f"source-{i}"] for i in range(4)])
        self.assertEqual(metrics["llm_calls"], 2)

    def test_only_failed_item_is_regenerated(self):
        items = [{
            "concept": {"concept_id": f"c{i}", "name": f"Concept {i}"},
            "evidence_chunks": [chunk(f"source-{i}")], "topic_id": "topic",
            "topic_name": "Topic", "assessment_capacity": 2,
        } for i in range(2)]
        prompts = []

        class Generator:
            count = 0
            def __init__(self, **_kwargs): pass
            def invoke(self, prompt):
                prompts.append(prompt)
                Generator.count += 1
                ids = ["c0", "c1"] if Generator.count == 1 else ["c1"]
                return _Response(json.dumps({"questions": [{
                    "concept_id": cid, "question": f"Which statement correctly describes the evidence-backed concept {cid} in this topic?", "options": ["A. Yes", "B. No", "C. Maybe", "D. Never"],
                    "correct_answer": "A", "explanation": "Evidence supports Yes.",
                } for cid in ids]}))

        validation_calls = 0
        def validate(rows):
            nonlocal validation_calls
            validation_calls += 1
            return [verdict(not (validation_calls == 1 and index == 1)) for index, _ in enumerate(rows)]

        metrics = {"llm_calls": 0, "generation_ms": 0, "validation_ms": 0,
                   "validation_llm_calls": 0, "retry_llm_calls": 0}
        with patch.object(quiz_service, "ChatOllama", Generator), \
             patch.object(quiz_service, "validate_questions_semantics", side_effect=validate), \
             patch.object(quiz_service, "_is_duplicate_question", return_value=False), \
             patch.object(quiz_service, "save_quiz_validation_event"):
            questions, errors = quiz_service._generate_concept_batch(
                "doc", "easy", items, 1, [], "run", 1, "hash", 2, "alice", metrics)
        self.assertEqual(errors, [])
        self.assertEqual({q["concept_id"] for q in questions}, {"c0", "c1"})
        self.assertIn('"concept_id": "c1"', prompts[1])
        self.assertNotIn('"concept_id": "c0"', prompts[1])
        self.assertEqual(metrics["retry_llm_calls"], 1)
