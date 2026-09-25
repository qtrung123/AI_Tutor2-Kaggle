"""Tests for backend/quiz_diagnostics.py and its wiring into the live Quiz pipeline.

These tests prove two things at once, matching the instrumentation brief:
1. The diagnostics module itself records timing/call/retry data correctly in
   isolation (no quiz pipeline involved).
2. Wiring it into quiz_service.py / assessment_planner.py does not change
   Quiz generation behavior, output, or error handling -- only observes it.

No new tokenizer, no new database table, no change to prompts/models/retry
limits/validation rules is exercised or asserted here.
"""

import json
import tempfile
import time
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quiz_fixtures import candidates, make_chunks, spread_facts

from backend import assessment_planner, quiz_diagnostics, quiz_service, quiz_store
from backend.quiz_service import _run_document_single_choice_batch


DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
DOCUMENT_SCOPE = {"topic_id": "document", "name": "Entire document"}


def make_slot(index: int, phrase: str) -> dict:
    facts = f"{phrase} fact one is documented. {phrase} fact two is documented."
    return {
        "slot_id": f"S{index}", "topic_id": "topic_1", "topic_name": "Topic 1",
        "concept_id": f"aconcept_{index}", "name": f"Concept {index}", "concept_plan_id": "plan_1",
        "source_subtopic_ids": ["sub_1"], "concept_origin": "structural",
        "source_chunk_ids": [f"chunk_{index}"], "assessment_capacity": 5,
        "evidence_excerpt": facts, "evidence_variants": [], "topic_evidence_variants": [],
    }


def good_candidate(slot: dict) -> dict:
    phrase = slot["evidence_excerpt"].split(" fact")[0]
    return {
        "slot_id": slot["slot_id"],
        "question": f"{phrase} fact one is documented for this concept.",
        "options": [f"{phrase} fact one", "Wrong A", "Wrong B", "Wrong C"], "correct_answer": 0,
        "explanation": f"{phrase} fact one is documented.",
    }


class SequencedOllama:
    """Fake ChatOllama whose response is computed on demand by the test via `respond`.
    Mirrors the fake already used in tests/test_document_quiz_blueprint.py."""

    respond = None
    response_metadata_factory = None  # optional: () -> dict, defaults to {}

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        import json
        candidates = self.__class__.respond(prompt)
        metadata = self.__class__.response_metadata_factory() if self.__class__.response_metadata_factory else {}
        return SimpleNamespace(content=json.dumps({"questions": candidates}), response_metadata=metadata)


class RaisingOllama:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, _prompt):
        raise RuntimeError("model unavailable")


def run_sc_batch(sc_slots, respond, response_metadata_factory=None):
    """Drive _run_document_single_choice_batch in isolation, inside a diagnostics run,
    and return (accepted_count, sc_timings, diag)."""
    SequencedOllama.respond = respond
    SequencedOllama.response_metadata_factory = response_metadata_factory
    accepted_by_slot: dict = {}
    accepted_stems: list = []
    remaining_slot_ids = {s["slot_id"] for s in sc_slots}
    rejection_reasons_by_slot: dict = {}
    results = {
        "accepted": 0, "accepted_with_warnings": 0, "rejected": 0,
        "hard_rejections": 0, "quality_warnings": 0, "reasons": [],
    }
    slot_positions = {s["slot_id"]: index for index, s in enumerate(sc_slots)}
    with quiz_diagnostics.start_run(request_id="test-request") as diag, \
         patch.object(quiz_service, "ChatOllama", SequencedOllama), \
         patch.object(quiz_service, "save_quiz_validation_event"):
        accepted_count, sc_timings = _run_document_single_choice_batch(
            DOCUMENT, "medium", sc_slots, "owner", "model", "run-id",
            accepted_by_slot, accepted_stems, remaining_slot_ids, rejection_reasons_by_slot,
            results, slot_positions, sc_slots,
        )
    return accepted_count, sc_timings, diag


@contextmanager
def capture_diagnostics():
    """Spy on quiz_diagnostics.start_run so a test can inspect the QuizRunDiagnostics
    object a real generate_quiz() call created, after the call returns."""
    captured = []
    original_start_run = quiz_diagnostics.start_run

    @contextmanager
    def spy(*args, **kwargs):
        with original_start_run(*args, **kwargs) as diag:
            captured.append(diag)
            yield diag

    with patch.object(quiz_diagnostics, "start_run", spy):
        yield captured


class DiagnosticsUnitTests(unittest.TestCase):
    """The diagnostics module in isolation -- no quiz pipeline involved."""

    def test_summary_records_total_latency(self):
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r1")
        time.sleep(0.001)
        summary = diag.summary()
        self.assertIn("total_ms", summary)
        self.assertGreaterEqual(summary["total_ms"], 0)

    def test_llm_call_counters_are_correct(self):
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r2")
        diag.record_llm_call(stage="planner", model="m", elapsed_ms=10, success=True)
        diag.record_llm_call(stage="planner", model="m", elapsed_ms=10, success=True)
        diag.record_llm_call(stage="generator", model="m", elapsed_ms=10, success=True)
        diag.record_llm_call(stage="generator", model="m", elapsed_ms=10, success=True)
        diag.record_llm_call(stage="generator", model="m", elapsed_ms=10, success=True)
        diag.record_llm_call(stage="repair", model="m", elapsed_ms=10, success=True)
        summary = diag.summary()
        self.assertEqual(summary["llm_calls"], 6)
        self.assertEqual(summary["planner_calls"], 2)
        self.assertEqual(summary["generator_calls"], 3)
        self.assertEqual(summary["repair_calls"], 1)
        self.assertEqual(summary["validator_calls"], 0)

    def test_retries_are_counted_correctly(self):
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r3")
        diag.record_retry(concept_id="S1", original_attempt=1, retry_attempt=1, reason="rejected", stage="validation")
        diag.record_retry(concept_id="S2", original_attempt=1, retry_attempt=1, reason="rejected", stage="validation")
        diag.record_retry(concept_id="S1", original_attempt=1, retry_attempt=2, reason="still rejected", stage="validation")
        summary = diag.summary()
        self.assertEqual(summary["retry_count"], 3)
        self.assertEqual(len(diag.retries), 3)

    def test_failed_llm_call_is_recorded_with_exception_type(self):
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r4")
        record = diag.record_llm_call(
            stage="generator", model="m", elapsed_ms=5, success=False,
            exception_type="RuntimeError", reason="model unavailable",
        )
        self.assertFalse(record.success)
        self.assertEqual(record.exception_type, "RuntimeError")
        summary = diag.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["generator_calls"], 1)

    def test_missing_token_metadata_records_null_not_a_fake_number(self):
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r5")
        record = diag.record_llm_call(
            stage="planner", model="m", elapsed_ms=5, success=True,
            input_tokens=None, output_tokens=None,
        )
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.output_tokens)
        # Must not raise, and must not invent a numeric token count anywhere in the summary.
        summary = diag.summary()
        self.assertNotIn("token_count", summary)

    def test_null_diagnostics_is_a_safe_noop_outside_a_run(self):
        diag = quiz_diagnostics.get_current()
        self.assertIsInstance(diag, quiz_diagnostics._NullDiagnostics)
        diag.record_llm_call(stage="planner", model="m", elapsed_ms=1, success=True)
        diag.record_retry(concept_id="S1", original_attempt=1, retry_attempt=1, reason="x", stage="validation")
        diag.record_context(stage="planner", num_chunks=1, total_chars=1, prompt=None)
        diag.absorb_pipeline_timings({"llm_calls": 1})
        self.assertEqual(diag.summary(), {})
        self.assertEqual(diag.log_summary(), {})

    def test_absorb_pipeline_timings_is_defensive_against_minimal_mocked_dicts(self):
        """Existing tests mock inner generation functions with minimal timings dicts
        (e.g. {"llm_calls": 1, "rejection_reasons_by_slot": {}}) -- absorb must not KeyError."""
        diag = quiz_diagnostics.QuizRunDiagnostics(request_id="r6")
        diag.absorb_pipeline_timings({"llm_calls": 1, "rejection_reasons_by_slot": {}})
        summary = diag.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["retrieval_ms"], 0)
        self.assertEqual(summary["generation_ms"], 0)

    def test_context_var_is_isolated_between_runs(self):
        with quiz_diagnostics.start_run(request_id="outer") as outer_diag:
            self.assertIs(quiz_diagnostics.get_current(), outer_diag)
        # After the context exits, get_current() must fall back to the null recorder again.
        self.assertIsInstance(quiz_diagnostics.get_current(), quiz_diagnostics._NullDiagnostics)


class SingleChoiceBatchDiagnosticsTests(unittest.TestCase):
    """Drives the real, live _run_document_single_choice_batch with a faked ChatOllama,
    the same way tests/test_document_quiz_blueprint.py does, and inspects what the
    diagnostics recorder captured."""

    def test_clean_generation_records_one_generator_call_and_no_retries(self):
        slots = [make_slot(i, f"Item{i}") for i in range(1, 4)]

        def respond(prompt):
            return [good_candidate(s) for s in slots if f"{s['slot_id']}|" in prompt]

        accepted_count, _timings, diag = run_sc_batch(slots, respond)

        self.assertEqual(accepted_count, 3)
        self.assertEqual(len(diag.llm_calls), 1)
        self.assertEqual(diag.llm_calls[0].stage, "generator")
        self.assertTrue(diag.llm_calls[0].success)
        self.assertEqual(diag.retries, [])
        summary = diag.summary()
        self.assertEqual(summary["generator_calls"], 1)
        self.assertEqual(summary["repair_calls"], 0)
        self.assertEqual(summary["retry_count"], 0)

    def test_one_failing_slot_triggers_a_repair_call_and_a_retry_record(self):
        slots = [make_slot(i, f"Item{i}") for i in range(1, 4)]
        failing_slot_id = slots[0]["slot_id"]
        attempts = {"count": 0}

        def respond(prompt):
            attempts["count"] += 1
            requested = [s for s in slots if f"{s['slot_id']}|" in prompt]
            return [
                good_candidate(s) for s in requested
                if not (s["slot_id"] == failing_slot_id and attempts["count"] == 1)
            ]

        accepted_count, _timings, diag = run_sc_batch(slots, respond)

        self.assertEqual(accepted_count, 3)  # the failing slot recovers via repair
        stages = [call.stage for call in diag.llm_calls]
        self.assertIn("generator", stages)
        self.assertIn("repair", stages)
        self.assertGreaterEqual(len(diag.retries), 1)
        self.assertTrue(any(retry.concept_id == failing_slot_id for retry in diag.retries))
        summary = diag.summary()
        self.assertGreaterEqual(summary["retry_count"], 1)
        self.assertGreaterEqual(summary["repair_calls"], 1)

    def test_missing_response_metadata_does_not_break_generation_or_instrumentation(self):
        """SequencedOllama returns response_metadata={} by default (no load/prompt-eval keys
        at all) -- exactly like the existing test fixtures in test_document_quiz_blueprint.py.
        Generation must still succeed, and token fields must be recorded as null, not guessed."""
        slots = [make_slot(1, "Item1")]

        def respond(prompt):
            return [good_candidate(s) for s in slots if f"{s['slot_id']}|" in prompt]

        accepted_count, _timings, diag = run_sc_batch(slots, respond, response_metadata_factory=dict)

        self.assertEqual(accepted_count, 1)
        self.assertEqual(len(diag.llm_calls), 1)
        self.assertIsNone(diag.llm_calls[0].input_tokens)
        self.assertIsNone(diag.llm_calls[0].output_tokens)

    def test_a_raising_llm_call_is_recorded_as_a_failure_and_generation_still_fails_closed(self):
        """Matches the existing repo convention (test_document_quiz_blueprint.py's
        TypePlannerFallbackTests) for a raising fake model. Document-scope quizzes have no
        deterministic fallback, so every call raising means the slot stays unfilled --
        exactly the pre-existing behavior; this test only adds an assertion about what
        diagnostics captured on top of that unchanged behavior."""
        slots = [make_slot(1, "Item1")]
        with quiz_diagnostics.start_run(request_id="test-raising") as diag, \
             patch.object(quiz_service, "ChatOllama", RaisingOllama), \
             patch.object(quiz_service, "save_quiz_validation_event"):
            accepted_by_slot: dict = {}
            accepted_stems: list = []
            remaining_slot_ids = {s["slot_id"] for s in slots}
            rejection_reasons_by_slot: dict = {}
            results = {
                "accepted": 0, "accepted_with_warnings": 0, "rejected": 0,
                "hard_rejections": 0, "quality_warnings": 0, "reasons": [],
            }
            accepted_count, _sc_timings = _run_document_single_choice_batch(
                DOCUMENT, "medium", slots, "owner", "model", "run-id",
                accepted_by_slot, accepted_stems, remaining_slot_ids, rejection_reasons_by_slot,
                results, {slots[0]["slot_id"]: 0}, slots,
            )

        self.assertEqual(accepted_count, 0)  # unchanged existing behavior: no fallback, slot stays missing
        self.assertGreaterEqual(len(diag.llm_calls), 1)
        self.assertTrue(all(not call.success for call in diag.llm_calls))
        self.assertTrue(all(call.exception_type == "RuntimeError" for call in diag.llm_calls))


class PlannerDiagnosticsTests(unittest.TestCase):
    """Drives assessment_planner._plan_seeds() directly with a faked ChatOllama."""

    def _seeds(self):
        return [{
            "subtopic_id": "sub1", "name": "Sub 1",
            "chunks": [{"content": "Reliable delivery is documented here.", "metadata": {"chunk_id": "c1"}}],
        }]

    def test_planner_call_is_recorded_on_success(self):
        class FakePlanner:
            def __init__(self, **_kwargs):
                pass

            def invoke(self, _prompt):
                import json
                return SimpleNamespace(
                    content=json.dumps({"concepts": [{"name": "Reliable delivery", "source_subtopic_ids": ["sub1"], "source_chunk_ids": ["c1"]}]}),
                    response_metadata={"prompt_eval_count": 42, "eval_count": 7},
                )

        with quiz_diagnostics.start_run(request_id="planner-test") as diag, \
             patch.object(assessment_planner, "ChatOllama", FakePlanner):
            concepts = assessment_planner._plan_seeds("Topic Name", self._seeds())

        self.assertEqual(len(concepts), 1)
        planner_calls = [call for call in diag.llm_calls if call.stage == "planner"]
        self.assertEqual(len(planner_calls), 1)
        self.assertTrue(planner_calls[0].success)
        self.assertEqual(planner_calls[0].input_tokens, 42)
        self.assertEqual(planner_calls[0].output_tokens, 7)
        self.assertEqual(diag.summary()["planner_calls"], 1)

    def test_planner_failure_is_recorded_and_still_raises(self):
        with quiz_diagnostics.start_run(request_id="planner-fail-test") as diag, \
             patch.object(assessment_planner, "ChatOllama", RaisingOllama):
            with self.assertRaises(RuntimeError):
                assessment_planner._plan_seeds("Topic Name", self._seeds())

        planner_calls = [call for call in diag.llm_calls if call.stage == "planner"]
        self.assertEqual(len(planner_calls), 1)
        self.assertFalse(planner_calls[0].success)
        self.assertEqual(planner_calls[0].exception_type, "RuntimeError")

    def test_build_topic_plan_fallback_is_unaffected_by_a_planner_failure(self):
        """build_topic_plan() already catches _plan_seeds() exceptions and falls back to
        deterministic concepts -- this must remain true unchanged with diagnostics wired in."""
        topic = {
            "topic_id": "topic_1", "name": "Topic 1", "boundary": {"start": {}, "end": {}},
            "subtopics": [{"subtopic_id": "sub1", "name": "Sub 1", "boundary": {"start": {}, "end": {}}}],
        }
        chunks = [{
            "content": "Reliable delivery is documented here.",
            "metadata": {"chunk_id": "c1", "start_index": 0, "page": 1},
        }]
        with quiz_diagnostics.start_run(request_id="fallback-test"), \
             patch.object(assessment_planner, "ChatOllama", RaisingOllama), \
             patch.object(assessment_planner, "build_structural_seeds", return_value=[{
                 "subtopic_id": "sub1", "name": "Sub 1", "chunks": chunks, "source_chunk_ids": ["c1"], "order": 0,
             }]):
            plan = assessment_planner.build_topic_plan(topic, chunks)

        self.assertTrue(plan["concepts"])  # deterministic fallback still produced usable concepts


class _FakeV3DiagModel:
    """Minimal ChatOllama stand-in for the live Study Units pipeline diagnostics tests."""
    payloads = []

    def __init__(self, **_kwargs):
        pass

    def stream(self, prompt):
        response = self.invoke(prompt)
        yield SimpleNamespace(content=response.content, response_metadata=getattr(response, "response_metadata", {}))

    def invoke(self, _prompt):
        return SimpleNamespace(content=json.dumps(self.__class__.payloads.pop(0)), response_metadata={})


def _v3_diag_chunks(count: int) -> list[dict]:
    return make_chunks(count, facts_per_chunk=2, prefix="chunk")


def _v3_diag_candidates(count: int) -> list[dict]:
    return candidates(spread_facts(count))


class GenerateQuizWrapperBehaviorTests(unittest.TestCase):
    """The public generate_quiz() entry point, through the real Study Units (no
    Planner) document-scope pipeline with only chunk retrieval and the LLM mocked out,
    proving the instrumentation wrapper changes nothing about the returned quiz."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "diagnostics.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def _document(self, document_id: str) -> dict:
        return {
            "id": document_id, "title": "Doc", "hash": "hash", "topic_schema_version": 2,
            "topics": [{"topic_id": "topic_a", "name": "A"}],
        }

    @contextmanager
    def _mocked_pipeline(self, document, candidate_count: int = 12):
        _FakeV3DiagModel.payloads = [{"questions": _v3_diag_candidates(candidate_count)}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(quiz_service, "_document_lookup", return_value={document["id"]: document}))
            stack.enter_context(patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"))
            stack.enter_context(patch.object(quiz_service, "get_document_chunks", return_value=_v3_diag_chunks(12)))
            stack.enter_context(patch.object(quiz_service, "ChatOllama", _FakeV3DiagModel))
            stack.enter_context(patch.object(quiz_service, "save_quiz_validation_event"))
            yield

    def test_wrapper_output_matches_calling_the_internal_function_directly(self):
        """Same mocked pipeline, two different document_ids (so caching can't hide a
        difference), called once through the public wrapper and once through the internal
        function it wraps -- content must be identical aside from run-unique ids/timestamps."""
        doc_a, doc_b = self._document("doc-a.pdf"), self._document("doc-b.pdf")

        with self._mocked_pipeline(doc_a):
            via_wrapper = quiz_service.generate_quiz(doc_a["id"], "easy", "document", question_count=12)

        with self._mocked_pipeline(doc_b):
            via_internal = quiz_service._generate_quiz(doc_b["id"], "easy", "document", question_count=12)

        def strip(quiz):
            return [
                {key: value for key, value in question.items() if key not in ("id",)}
                for question in quiz["questions"]
            ]

        self.assertEqual(strip(via_wrapper), strip(via_internal))
        self.assertEqual(via_wrapper["question_count"], via_internal["question_count"])
        self.assertEqual(
            via_wrapper["assessment_plan"]["type_distribution"],
            via_internal["assessment_plan"]["type_distribution"],
        )

    def test_wrapper_records_a_diagnostics_summary_with_expected_counts(self):
        document = self._document("doc-c.pdf")
        with capture_diagnostics() as captured, self._mocked_pipeline(document):
            result = quiz_service.generate_quiz(document["id"], "easy", "document", question_count=12)

        self.assertEqual(len(captured), 1)
        summary = captured[0].summary()
        self.assertEqual(result["question_count"], 12)
        self.assertGreaterEqual(summary["total_ms"], 0)
        self.assertEqual(summary["requested_questions"], 12)
        self.assertEqual(summary["generated_questions"], 12)
        self.assertEqual(summary["validated_questions"], 12)
        # One initial call already supplies all 12 valid candidates -- no repair/fill needed.
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["cache_hit"], False)

    def test_cache_hit_path_still_logs_a_summary(self):
        document = self._document("doc-d.pdf")
        with self._mocked_pipeline(document):
            quiz_service.generate_quiz(document["id"], "easy", "document", question_count=12)

        with capture_diagnostics() as captured, self._mocked_pipeline(document):
            second = quiz_service.generate_quiz(document["id"], "easy", "document", question_count=12)

        summary = captured[0].summary()
        self.assertEqual(summary["cache_hit"], True)
        self.assertEqual(second["question_count"], 12)

    def test_failure_path_is_recorded_and_the_original_error_still_propagates(self):
        document = self._document("doc-e.pdf")
        # The Study Units pipeline only fails the whole request closed when the candidate pool is
        # completely empty (spec case E) -- a non-empty pool below question_count is persisted
        # as a partial quiz instead of raising. An empty initial response (and the single
        # bounded top-up after it) keeps the pool empty, so this still exercises the real
        # failure/diagnostics path.
        _FakeV3DiagModel.payloads = [{"questions": []}]

        with capture_diagnostics() as captured, \
             patch.object(quiz_service, "_document_lookup", return_value={document["id"]: document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "get_document_chunks", return_value=_v3_diag_chunks(12)), \
             patch.object(quiz_service, "ChatOllama", _FakeV3DiagModel), \
             patch.object(quiz_service, "save_quiz_validation_event"):
            with self.assertRaises(quiz_service.QuizGenerationError):
                quiz_service.generate_quiz(document["id"], "easy", "document", question_count=12)

        self.assertEqual(len(captured), 1)
        summary = captured[0].summary()
        self.assertIn("failure", summary)
        self.assertEqual(summary["failure"]["exception_type"], "QuizGenerationError")
        # One initial call plus the single bounded top-up: the hard ceiling of the live pipeline.
        self.assertEqual(summary["llm_calls"], 2)


if __name__ == "__main__":
    unittest.main()
