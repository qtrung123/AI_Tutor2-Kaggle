"""Quiz Generation V3: Planner-free generation, deterministic context grouping, and the
V2 candidate-pool/selection/partial-persistence architecture reused on top of it.

Each test below is labeled with the numbered scenario it covers from the Quiz V3 design:
1. Quiz generation without Planner
2. Context grouping
3. Candidate provenance
4. Multiple document/context groups
5. Duplicate removal
6. Grounding validation
7. Candidate pool selection
8. Targeted fill
9. Partial Quiz
10. Zero-valid-candidate failure
11. Requested 12 but model returns fewer than 16 candidates
12. Coverage across multiple document regions

These call _generate_quiz_v3 and _select_v3_context_groups directly (the same pattern as
tests/test_quiz_v2_candidate_pool.py for V2), so no chunk retrieval or Chroma/Ollama embedding
call is involved -- only the LLM used for question generation is mocked.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import assessment_planner, quiz_service
from backend.quiz_service import QuizGenerationError, _generate_quiz_v3, _select_v3_context_groups


DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


def make_chunks(count: int) -> list[dict]:
    return [{
        "content": f"Mechanism{i} supports reliable delivery. Mechanism{i} also protects against loss.",
        "metadata": {"chunk_id": f"chunk_{i}", "document_id": "lecture.pdf"},
    } for i in range(1, count + 1)]


def raw_v3_question(slot_id: str, index: int) -> dict:
    return {
        "slot_id": slot_id, "question_type": "single_choice",
        "question": f"How does the mechanism support reliable delivery in case {index}?",
        "options": [
            "It supports reliable delivery", "It disables receiver behavior",
            "It removes ordering behavior", "It prevents delivery behavior",
        ],
        "correct_answers": [0],
        "explanation": "The evidence directly supports reliable delivery.",
    }


def ungrounded_v3_question(slot_id: str) -> dict:
    """A structurally valid candidate that shares no vocabulary with any group's evidence."""
    return {
        "slot_id": slot_id, "question_type": "single_choice",
        "question": "What color is commonly associated with stop signs in traffic signage?",
        "options": ["Red paint pigment", "Blue dye mixture", "Green tint coating", "Yellow pigment blend"],
        "correct_answers": [0],
        "explanation": "Stop signs are conventionally painted with red pigment.",
    }


class FakeV3Model:
    payloads = []
    prompts = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        self.__class__.prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(self.__class__.payloads.pop(0)), response_metadata={})


class QuizV3NoPlannerTests(unittest.TestCase):
    def setUp(self):
        FakeV3Model.payloads = []
        FakeV3Model.prompts = []

    def run_v3(self, chunks, payloads, question_count=12, scope="topic"):
        FakeV3Model.payloads = list(payloads)
        saved = []
        with (
            patch.object(quiz_service, "ChatOllama", FakeV3Model),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: saved.append(quiz) or quiz),
        ):
            result = _generate_quiz_v3(
                document=DOCUMENT, scope=scope, scope_topic_id="topic_1" if scope == "topic" else "document",
                scope_topic_name="Topic 1" if scope == "topic" else "Entire document", chunks=chunks,
                difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )
        return result, saved

    # --- 1. Quiz generation without Planner ----------------------------------------------------
    def test_generation_never_calls_the_planner(self):
        chunks = make_chunks(12)
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(12)]
        with (
            patch.object(assessment_planner, "build_topic_plan") as build_plan,
            patch.object(assessment_planner, "_plan_seeds") as plan_seeds,
        ):
            result, saved = self.run_v3(chunks, [{"questions": candidates}])
        build_plan.assert_not_called()
        plan_seeds.assert_not_called()
        self.assertEqual(result["assessment_plan"]["generation_engine"], "v3_context_groups")
        self.assertEqual(result["assessment_plan"]["planner_version"], quiz_service.QUIZ_V3_ENGINE_VERSION)
        self.assertEqual(len(saved), 1)

    # --- 2. Context grouping ---------------------------------------------------------------------
    def test_context_grouping_produces_contiguous_ordered_groups(self):
        groups = _select_v3_context_groups(make_chunks(7))
        self.assertEqual([group["group_id"] for group in groups], ["G1", "G2", "G3"])
        self.assertEqual(groups[0]["source_chunk_ids"], ["chunk_1", "chunk_2", "chunk_3"])
        self.assertEqual(groups[1]["source_chunk_ids"], ["chunk_4", "chunk_5", "chunk_6"])
        self.assertEqual(groups[2]["source_chunk_ids"], ["chunk_7"])
        # No group's evidence exceeds the configured character cap.
        for group in groups:
            self.assertLessEqual(len(group["evidence_excerpt"]), quiz_service.QUIZ_V3_MAX_CHARS_PER_GROUP)

    def test_context_grouping_is_empty_for_no_usable_chunks(self):
        self.assertEqual(_select_v3_context_groups([]), [])
        self.assertEqual(_select_v3_context_groups([{"content": "", "metadata": {"chunk_id": "c1"}}]), [])

    # --- 3. Candidate provenance ------------------------------------------------------------------
    def test_candidate_provenance_is_backend_owned_not_model_supplied(self):
        chunks = make_chunks(6)
        forged = raw_v3_question("S1", 0)
        forged.update({"concept_id": "forged-concept", "topic_id": "forged-topic", "source_chunk_ids": ["not-a-real-chunk"]})
        result, _saved = self.run_v3(chunks, [{"questions": [forged]}], question_count=1)
        question = result["questions"][0]
        self.assertNotEqual(question["concept_id"], "forged-concept")
        self.assertEqual(question["concept_id"], "G1")
        self.assertEqual(question["topic_id"], "topic_1")
        self.assertEqual(question["source_chunk_ids"], ["chunk_1", "chunk_2", "chunk_3"])
        self.assertEqual(question["concept_origin"], "context_group")
        self.assertEqual(question["concept_plan_id"], quiz_service.QUIZ_V3_ENGINE_VERSION)

    # --- 4 / 12. Multiple document/context groups & coverage across regions ----------------------
    def test_coverage_across_multiple_document_regions(self):
        chunks = make_chunks(9)  # 3 groups of 3
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(9)]
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=9)
        represented_groups = {question["concept_id"] for question in result["questions"]}
        self.assertEqual(represented_groups, {"G1", "G2", "G3"})
        represented_chunks = {
            chunk_id for question in result["questions"] for chunk_id in question["source_chunk_ids"]
        }
        self.assertEqual(represented_chunks, {f"chunk_{i}" for i in range(1, 10)})

    # --- 5. Duplicate removal ----------------------------------------------------------------------
    def test_duplicate_candidate_is_rejected_from_pool(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(4)]
        duplicate = raw_v3_question("S5", 0)  # same stem as the first accepted candidate
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, duplicate]}], question_count=4)
        stems = [question["question"] for question in result["questions"]]
        self.assertEqual(len(stems), len(set(stems)))
        self.assertEqual(len(result["questions"]), 4)

    # --- 6. Grounding validation --------------------------------------------------------------------
    def test_ungrounded_candidate_is_rejected_from_pool(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(4)]
        ungrounded = ungrounded_v3_question("S5")
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, ungrounded]}], question_count=4)
        self.assertEqual(len(result["questions"]), 4)
        self.assertEqual(result["assessment_plan"]["validation_results"]["grounding_rejections"], 1)

    # --- 7. Candidate pool selection ------------------------------------------------------------------
    def test_selection_spreads_across_groups_instead_of_first_n(self):
        chunks = make_chunks(9)  # 3 groups
        # 9 valid candidates for a pool of 12 (9 + buffer 3) requesting only 6 -- selection must
        # not just take the first 6 (which would all come from G1/G2 given slot cycling order).
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(9)]
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=6)
        represented_groups = {question["concept_id"] for question in result["questions"]}
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(represented_groups, {"G1", "G2", "G3"})

    # --- 8. Targeted fill -------------------------------------------------------------------------
    def test_targeted_fill_tops_up_missing_candidates(self):
        chunks = make_chunks(6)
        initial = [raw_v3_question(f"S{index + 1}", index) for index in range(4)]
        fill = [raw_v3_question("S5", 4), raw_v3_question("S6", 5)]
        result, saved = self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=6)
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    # --- 9. Partial Quiz --------------------------------------------------------------------------
    def test_partial_quiz_when_fill_is_exhausted(self):
        chunks = make_chunks(6)
        initial = [raw_v3_question(f"S{index + 1}", index) for index in range(4)]
        empty = {"questions": []}
        result, saved = self.run_v3(
            chunks, [{"questions": initial}, empty, empty, empty, empty], question_count=6,
        )
        self.assertEqual(len(result["questions"]), 4)
        self.assertEqual(result["assessment_plan"]["status"], "partial")
        self.assertTrue(result["assessment_plan"]["partial"])
        self.assertEqual(result["assessment_plan"]["requested_count"], 6)
        self.assertEqual(result["assessment_plan"]["actual_count"], 4)
        self.assertEqual(result["assessment_plan"]["missing_count"], 2)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["deterministic_fallback_count"], 0)
        self.assertEqual(len(saved), 1)

    # --- 10. Zero-valid-candidate failure ------------------------------------------------------------
    def test_zero_valid_candidates_fails_without_planner_fallback(self):
        chunks = make_chunks(6)
        empty = {"questions": []}
        with (
            patch.object(assessment_planner, "build_topic_plan") as build_plan,
            self.assertRaises(QuizGenerationError) as failure,
        ):
            self.run_v3(chunks, [empty, empty, empty, empty, empty], question_count=6)
        build_plan.assert_not_called()
        self.assertEqual(failure.exception.detail["valid_questions"], 0)
        self.assertEqual(failure.exception.detail["missing_count"], 6)

    # --- 11. Requested 12 but model returns fewer than 16 candidates -------------------------------
    def test_requested_twelve_accepts_fewer_than_the_buffered_sixteen(self):
        chunks = make_chunks(15)  # 5 groups, enough for 12 distinct slots without repeats
        # Pool target for N=12 is 12 + 4 = 16 (see _v2_candidate_pool_target), but the model only
        # returns 12 -- that must be accepted outright, with no error and no forced retry to reach 16.
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(12)]
        self.assertEqual(quiz_service._v2_candidate_pool_target(12), 16)
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["actual_count"], 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["candidates_returned_total"], 12)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["desired_candidate_count"], 16)


class QuizV3PerformanceOptimizationTests(unittest.TestCase):
    """Early stopping and targeted-fill sizing on top of the V3 candidate-pool architecture --
    no architecture change, just fewer/smaller LLM calls when the pool already satisfies the
    request. Cases numbered per the optimization pass spec."""

    def setUp(self):
        FakeV3Model.payloads = []
        FakeV3Model.prompts = []

    run_v3 = QuizV3NoPlannerTests.run_v3

    # --- Case 1: pool already sufficient after the initial call -------------------------------
    def test_case1_pool_already_sufficient_skips_repair_and_fill(self):
        chunks = make_chunks(15)  # 5 context groups
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(13)]
        result, saved = self.run_v3(chunks, [{"questions": candidates}], question_count=12)
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["repair_attempt_count"], 0)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["fill_attempt_count"], 0)
        self.assertEqual(len(saved), 1)

    # --- Case 2: one targeted fill closes the gap in a single extra call ----------------------
    def test_case2_single_targeted_fill_call_closes_the_gap(self):
        chunks = make_chunks(15)  # 5 context groups
        initial = [raw_v3_question(f"S{index + 1}", index) for index in range(10)]
        # The targeted-fill round is asked for missing(2) + buffer(1) = 3 slots -- the model
        # returns all 3, but only 2 are needed to reach question_count.
        fill = [raw_v3_question("S11", 10), raw_v3_question("S12", 11), raw_v3_question("S13", 12)]
        result, saved = self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=12)
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    # --- Case 3: a fill round that only partly closes the gap still respects the hard maximum --
    def test_case3_bounded_fill_never_exceeds_configured_maximum(self):
        chunks = make_chunks(15)  # 5 context groups
        initial = [raw_v3_question(f"S{index + 1}", index) for index in range(10)]
        single_fill = [raw_v3_question("S11", 10)]
        empty = {"questions": []}
        result, saved = self.run_v3(
            chunks, [{"questions": initial}, {"questions": single_fill}, empty, empty, empty],
            question_count=12,
        )
        self.assertEqual(len(result["questions"]), 11)
        self.assertEqual(result["assessment_plan"]["status"], "partial")
        self.assertEqual(result["assessment_plan"]["missing_count"], 1)
        # Hard maximum: 1 initial + 2 repair + 2 fill, never more, regardless of how many extra
        # rounds were actually needed.
        self.assertEqual(result["assessment_plan"]["llm_calls"], 5)
        self.assertEqual(len(saved), 1)

    # --- Case 4: pool stays below target -> partial, no infinite retry -------------------------
    def test_case4_partial_quiz_when_pool_stays_below_target(self):
        chunks = make_chunks(15)  # 5 context groups
        initial = [raw_v3_question(f"S{index + 1}", index) for index in range(8)]
        empty = {"questions": []}
        result, saved = self.run_v3(
            chunks, [{"questions": initial}, empty, empty, empty, empty], question_count=12,
        )
        self.assertEqual(len(result["questions"]), 8)
        self.assertEqual(result["assessment_plan"]["status"], "partial")
        self.assertEqual(result["assessment_plan"]["actual_count"], 8)
        self.assertEqual(result["assessment_plan"]["missing_count"], 4)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 5)
        self.assertEqual(len(saved), 1)

    # --- Case 5: bounded fill prioritizes the under-represented context group -------------------
    def test_case5_fill_slot_priority_favors_underrepresented_groups(self):
        remaining = [
            {"slot_id": "S5", "concept_id": "G1"}, {"slot_id": "S6", "concept_id": "G2"},
            {"slot_id": "S7", "concept_id": "G3"}, {"slot_id": "S8", "concept_id": "G4"},
        ]
        accepted = [
            {"concept_id": "G1"}, {"concept_id": "G1"}, {"concept_id": "G1"},
            {"concept_id": "G2"}, {"concept_id": "G2"}, {"concept_id": "G3"},
        ]  # G4 has zero accepted candidates so far
        chosen = quiz_service._prioritize_v3_fill_slots(remaining, accepted, limit=2)
        self.assertEqual([slot["slot_id"] for slot in chosen], ["S8", "S7"])

    def test_case5_bounded_fill_prioritizes_the_underrepresented_group_end_to_end(self):
        chunks = make_chunks(12)  # 4 context groups
        # G4 (S4, S8) has zero accepted candidates after the initial call; G1/G2/G3 each have 2.
        initial = [
            raw_v3_question("S1", 0), raw_v3_question("S2", 1), raw_v3_question("S3", 2),
            raw_v3_question("S5", 4), raw_v3_question("S6", 5), raw_v3_question("S7", 6),
        ]
        fill = [raw_v3_question("S4", 3), raw_v3_question("S8", 7)]
        result, _saved = self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=8)
        self.assertEqual(len(result["questions"]), 8)
        first_retry = result["assessment_plan"]["timings_ms"]["missing_slots_before_each_retry"][0]
        self.assertEqual(first_retry["missing_slots"][:2], ["S4", "S8"])
        represented_groups = {question["concept_id"] for question in result["questions"]}
        self.assertIn("G4", represented_groups)

    # --- Benchmark logging: rejected/duplicate/grounding counts are in the printed timings ------
    def test_optimization_logging_reports_rejection_breakdown(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"S{index + 1}", index) for index in range(3)]
        duplicate = raw_v3_question("S4", 0)  # same stem as the first accepted candidate
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, duplicate]}], question_count=3)
        timings = result["assessment_plan"]["timings_ms"]
        self.assertIn("rejected_candidates", timings)
        self.assertIn("duplicate_candidates", timings)
        self.assertIn("grounding_rejected_candidates", timings)
        self.assertIn("structural_rejected_candidates", timings)
        self.assertIn("response_failures", timings)
        self.assertEqual(timings["duplicate_candidates"], 1)
        self.assertEqual(timings["rejected_candidates"], 1)
        self.assertEqual(timings["response_failures"], 0)
        self.assertEqual(
            timings["duplicate_candidates"] + timings["grounding_rejected_candidates"]
            + timings["structural_rejected_candidates"] + timings["response_failures"],
            timings["rejected_candidates"],
        )

    def test_response_level_failure_is_counted_separately_from_candidate_rejections(self):
        """A whole-response failure (malformed JSON, exception, timeout) never produced
        individual candidates to classify -- it must land in response_failures, not be folded
        into duplicate/grounding/structural, while still adding up to rejected_candidates."""
        chunks = make_chunks(6)
        malformed = {"not_questions": True}  # missing "questions" key -> response-level failure
        valid = [raw_v3_question(f"S{index + 1}", index) for index in range(2)]
        result, _saved = self.run_v3(chunks, [malformed, {"questions": valid}], question_count=2)
        timings = result["assessment_plan"]["timings_ms"]
        self.assertEqual(len(result["questions"]), 2)
        self.assertGreater(timings["response_failures"], 0)
        self.assertEqual(timings["duplicate_candidates"], 0)
        self.assertEqual(timings["grounding_rejected_candidates"], 0)
        self.assertEqual(timings["structural_rejected_candidates"], 0)
        self.assertEqual(
            timings["duplicate_candidates"] + timings["grounding_rejected_candidates"]
            + timings["structural_rejected_candidates"] + timings["response_failures"],
            timings["rejected_candidates"],
        )


if __name__ == "__main__":
    unittest.main()
