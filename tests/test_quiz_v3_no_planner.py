"""Quiz Generation V3: Planner-free generation with NO Question Blueprint / slot allocation.

The model is asked to write N questions directly from the provided context-group evidence,
tagging each with the group_id its evidence came from. A context group may contribute any
number of accepted candidates -- there is no "this slot was already used" rejection. Backend
provenance (source_chunk_ids, concept identity) is always assigned from the group record, never
trusted from the model. question_type is controlled by the system (single_choice-only for
document scope, matching the pre-Planner-removal contract; mixed types preserved for topic
scope), not left to the model's free choice.

Each test below is labeled with the scenario it covers:
1. Quiz generation without Planner/Blueprint/slot planning
2. Context grouping
3. Candidate provenance
4. No slot-based rejection (a group may contribute multiple candidates)
5. Unknown group_id is still rejected (provenance validation is not "slot allocation")
6. Coverage across multiple document regions
7. Duplicate removal (exact stem)
8. Content/semantic duplicate rejection (Q1/Q9-style same-concept questions)
9. Grounding validation
10. Ambiguous-option rejection (Q6-style "clock" vs "clock interrupt")
11. Configured question type is respected (no unrequested True/False for document scope)
12. Topic scope keeps its existing mixed-type design
13. multi_select answer consistency (correct_answer == correct_answers[0], always sorted)
14. Candidate pool selection (coverage-aware, not first-N)
15. Targeted fill
16. Partial Quiz
17. Zero-valid-candidate failure
18. Requested 12 but model returns fewer than the buffered 16
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import assessment_planner, quiz_service
from backend.quiz_service import QuizGenerationError, _generate_quiz_v3, _select_v3_context_groups


DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


# Every chunk carries the full fact vocabulary so a candidate can ground against ANY group
# regardless of which distinct fact (see _V3_FACT_VARIANTS) it happens to test -- this keeps
# fixtures simple while still letting genuinely distinct questions have low content overlap.
_SHARED_EVIDENCE = (
    "This mechanism supports reliable delivery and protects against packet loss. "
    "It prevents duplicate delivery and preserves delivery order. "
    "It detects corrupted data and confirms successful delivery. "
    "It retransmits lost data automatically and avoids overwhelming the receiver. "
    "It limits the transmission rate and tracks acknowledgements from the receiver. "
    "It recovers from timeouts by retrying and ensures data integrity. "
    "It buffers incoming data before processing and sequences incoming packets in order. "
    "A timeout triggers a retransmission and checksums validate data to detect errors."
)

# Sixteen genuinely distinct facts (low mutual content overlap) so a pool of up to 16 candidates
# (question_count=12's buffered target) can be built without tripping the content-duplicate
# check on candidates that are only "different" by an incrementing case number.
_V3_FACT_VARIANTS = [
    ("Why does the mechanism support reliable delivery?", "It supports reliable delivery"),
    ("How does the mechanism protect against packet loss?", "It protects against packet loss"),
    ("What prevents duplicate delivery in this mechanism?", "It prevents duplicate delivery"),
    ("Why does the mechanism preserve delivery order?", "It preserves delivery order"),
    ("How does the mechanism detect corrupted data?", "It detects corrupted data"),
    ("What confirms successful delivery in this mechanism?", "It confirms successful delivery"),
    ("Why does the mechanism retransmit lost data?", "It retransmits lost data automatically"),
    ("How does the mechanism avoid overwhelming the receiver?", "It avoids overwhelming the receiver"),
    ("What limits the transmission rate in this mechanism?", "It limits the transmission rate"),
    ("Why does the mechanism track acknowledgements?", "It tracks acknowledgements from the receiver"),
    ("How does the mechanism recover from timeouts?", "It recovers from timeouts by retrying"),
    ("What ensures data integrity in this mechanism?", "It ensures data integrity"),
    ("Why does the mechanism buffer incoming data?", "It buffers incoming data before processing"),
    ("How does the mechanism sequence incoming packets?", "It sequences incoming packets in order"),
    ("What triggers a retransmission in this mechanism?", "A timeout triggers a retransmission"),
    ("Why does the mechanism validate checksums?", "Checksums validate data to detect errors"),
]


def make_chunks(count: int) -> list[dict]:
    return [{
        "content": f"Chunk{i}: {_SHARED_EVIDENCE}",
        "metadata": {"chunk_id": f"chunk_{i}", "document_id": "lecture.pdf"},
    } for i in range(1, count + 1)]


def raw_v3_question(
    group_id: str, index: int, question_type: str = "single_choice",
    correct_answers: list[int] | None = None, options: list[str] | None = None, stem: str | None = None,
) -> dict:
    variant_stem, variant_answer = _V3_FACT_VARIANTS[index % len(_V3_FACT_VARIANTS)]
    return {
        "group_id": group_id, "question_type": question_type,
        "question": stem or variant_stem,
        "options": options or [
            variant_answer, "It disables receiver behavior",
            "It removes ordering behavior", "It prevents delivery entirely",
        ],
        "correct_answers": correct_answers if correct_answers is not None else [0],
        "explanation": f"The evidence directly states that {variant_answer[0].lower()}{variant_answer[1:]}.",
    }


def ungrounded_v3_question(group_id: str) -> dict:
    """A structurally valid candidate that shares no vocabulary with any group's evidence."""
    return {
        "group_id": group_id, "question_type": "single_choice",
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

    # --- 1. Quiz generation without Planner/Blueprint/slot planning -----------------------------
    def test_generation_never_calls_the_planner(self):
        chunks = make_chunks(12)
        candidates = [raw_v3_question(f"G{(index % 4) + 1}", index) for index in range(12)]
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

    def test_prompt_never_mentions_slots_or_a_fixed_blueprint(self):
        """The V3 prompt must not instruct the model to map one question to one pre-allocated
        slot -- that instruction (and the matching rejection) is exactly what caused real runs to
        reject valid candidates with "Question repeats or exceeds its planned slot_id"."""
        groups = _select_v3_context_groups(make_chunks(6))
        prompt = quiz_service._build_v3_prompt("lecture.pdf", {"topic_id": "t", "name": "T"}, "easy", groups, 4, [], False)
        self.assertNotIn("slot", prompt.lower())
        self.assertIn("group_id", prompt)
        self.assertIn("may write more than one question from the same group", prompt)

    # --- 2. Context grouping ---------------------------------------------------------------------
    def test_context_grouping_produces_contiguous_ordered_groups(self):
        groups = _select_v3_context_groups(make_chunks(7))
        self.assertEqual([group["group_id"] for group in groups], ["G1", "G2", "G3"])
        self.assertEqual(groups[0]["source_chunk_ids"], ["chunk_1", "chunk_2", "chunk_3"])
        self.assertEqual(groups[1]["source_chunk_ids"], ["chunk_4", "chunk_5", "chunk_6"])
        self.assertEqual(groups[2]["source_chunk_ids"], ["chunk_7"])
        for group in groups:
            self.assertLessEqual(len(group["evidence_excerpt"]), quiz_service.QUIZ_V3_MAX_CHARS_PER_GROUP)

    def test_context_grouping_is_empty_for_no_usable_chunks(self):
        self.assertEqual(_select_v3_context_groups([]), [])
        self.assertEqual(_select_v3_context_groups([{"content": "", "metadata": {"chunk_id": "c1"}}]), [])

    # --- 3. Candidate provenance ------------------------------------------------------------------
    def test_candidate_provenance_is_backend_owned_not_model_supplied(self):
        chunks = make_chunks(6)
        forged = raw_v3_question("G1", 0)
        forged.update({
            "concept_id": "forged-concept", "topic_id": "forged-topic",
            "source_chunk_ids": ["not-a-real-chunk"],
        })
        result, _saved = self.run_v3(chunks, [{"questions": [forged]}], question_count=1)
        question = result["questions"][0]
        self.assertEqual(len(result["questions"]), 1)
        self.assertNotEqual(question["concept_id"], "forged-concept")
        self.assertEqual(question["concept_id"], "G1")
        self.assertEqual(question["source_chunk_ids"], ["chunk_1", "chunk_2", "chunk_3"])
        self.assertEqual(question["concept_origin"], "context_group")
        self.assertEqual(question["concept_plan_id"], quiz_service.QUIZ_V3_ENGINE_VERSION)
        self.assertEqual(question["topic_id"], "topic_1")

    # --- 4. No slot-based rejection ---------------------------------------------------------------
    def test_reusing_the_same_context_group_is_not_rejected(self):
        """A context group may contribute any number of accepted candidates -- there is no
        pre-allocated one-question-per-group slot to exhaust."""
        chunks = make_chunks(6)  # 2 groups
        candidates = [
            raw_v3_question("G1", 0, stem="Why does mechanism one support reliable delivery in case 1?"),
            raw_v3_question("G1", 1, stem="Why does mechanism one protect against loss in case 2?"),
            raw_v3_question("G1", 2, stem="Why does mechanism one avoid data corruption in case 3?"),
        ]
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=3)
        self.assertEqual(len(result["questions"]), 3)
        self.assertTrue(all(question["concept_id"] == "G1" for question in result["questions"]))
        for reason in result["assessment_plan"]["generation_warnings"]:
            self.assertNotIn("slot", reason.lower())

    # --- 5. Unknown group_id is still rejected ----------------------------------------------------
    def test_unknown_group_id_is_rejected(self):
        chunks = make_chunks(6)  # groups G1, G2 only
        candidates = [raw_v3_question(f"G{index + 1}", index) for index in range(2)]
        bogus = raw_v3_question("G99", 2)
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, bogus]}], question_count=2)
        self.assertEqual(len(result["questions"]), 2)
        self.assertIn(
            "Question has an unknown group_id.",
            "".join(result["assessment_plan"]["generation_warnings"]),
        )

    # --- 6. Coverage across multiple document regions ----------------------------------------------
    def test_coverage_across_multiple_document_regions(self):
        chunks = make_chunks(9)  # 3 groups of 3
        candidates = [raw_v3_question(f"G{(index % 3) + 1}", index) for index in range(9)]
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=9)
        represented_groups = {question["concept_id"] for question in result["questions"]}
        self.assertEqual(represented_groups, {"G1", "G2", "G3"})
        represented_chunks = {
            chunk_id for question in result["questions"] for chunk_id in question["source_chunk_ids"]
        }
        self.assertEqual(represented_chunks, {f"chunk_{i}" for i in range(1, 10)})

    # --- 7. Duplicate removal (exact stem) ----------------------------------------------------------
    def test_exact_duplicate_candidate_is_rejected_from_pool(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(4)]
        duplicate = raw_v3_question("G1", 0)  # identical stem to the first accepted candidate
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, duplicate]}], question_count=4)
        stems = [question["question"] for question in result["questions"]]
        self.assertEqual(len(stems), len(set(stems)))
        self.assertEqual(len(result["questions"]), 4)

    # --- 8. Content/semantic duplicate rejection (Q1/Q9-style) --------------------------------------
    def test_content_signature_flags_same_concept_but_flags_distinct_questions_separately(self):
        sig_a = quiz_service._v3_content_signature(
            "Why does long term scheduling determine the degree of multiprogramming in this system?",
            "It determines the degree of multiprogramming",
        )
        sig_same_concept = quiz_service._v3_content_signature(
            "How is the degree of multiprogramming in this system determined by long term scheduling?",
            "It is determined by long term scheduling",
        )
        sig_distinct = quiz_service._v3_content_signature(
            "Why does flow control protect a receiving endpoint from being overwhelmed?",
            "It prevents the receiver from being overwhelmed with data",
        )
        self.assertTrue(quiz_service._is_v3_content_duplicate(sig_same_concept, [sig_a]))
        self.assertFalse(quiz_service._is_v3_content_duplicate(sig_distinct, [sig_a]))

    def test_q1_q9_style_duplicate_concept_is_rejected_end_to_end(self):
        """Two differently-worded questions that test the same underlying fact (the reported
        "Q1 and Q9 both ask about long-term scheduling" failure) must not both be accepted, even
        though their exact stems are different enough to pass the near-duplicate-stem check."""
        chunks = make_chunks(6)
        first = raw_v3_question(
            "G1", 0,
            stem="Why does long term scheduling determine the degree of multiprogramming in this system?",
            options=["It determines the degree of multiprogramming", "It disables receivers",
                     "It removes ordering", "It prevents delivery"],
        )
        same_concept_again = raw_v3_question(
            "G1", 1,
            stem="How is the degree of multiprogramming in this system determined by long term scheduling?",
            options=["It is determined by long term scheduling", "It disables receivers",
                     "It removes ordering", "It prevents delivery"],
        )
        genuinely_different = raw_v3_question(
            "G2", 2,
            stem="Why does flow control protect a receiving endpoint from being overwhelmed?",
            options=["It prevents the receiver from being overwhelmed", "It disables receivers",
                     "It removes ordering", "It prevents delivery"],
        )
        result, _saved = self.run_v3(
            chunks, [{"questions": [first, same_concept_again, genuinely_different]}], question_count=3,
        )
        self.assertEqual(len(result["questions"]), 2)
        stems = {question["question"] for question in result["questions"]}
        self.assertIn(first["question"], stems)
        self.assertNotIn(same_concept_again["question"], stems)
        self.assertIn(genuinely_different["question"], stems)
        self.assertEqual(result["assessment_plan"]["validation_results"]["duplicate_rejections"], 1)

    # --- 9. Grounding validation --------------------------------------------------------------------
    def test_ungrounded_candidate_is_rejected_from_pool(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(4)]
        ungrounded = ungrounded_v3_question("G1")
        result, _saved = self.run_v3(chunks, [{"questions": [*candidates, ungrounded]}], question_count=4)
        self.assertEqual(len(result["questions"]), 4)
        self.assertEqual(result["assessment_plan"]["validation_results"]["grounding_rejections"], 1)

    # --- 10. Ambiguous-option rejection (Q6-style) --------------------------------------------------
    def test_ambiguous_option_containment_is_rejected(self):
        chunks = make_chunks(6)
        ambiguous = raw_v3_question(
            "G1", 0,
            stem="RoundRobin (RR) uses preemption based on what mechanism?",
            options=["Clock", "Timer", "Counter", "Clock interrupt"],
            correct_answers=[3],
        )
        with self.assertRaises(QuizGenerationError):
            self.run_v3(chunks, [{"questions": [ambiguous]}], question_count=1)

    def test_non_ambiguous_options_are_accepted(self):
        chunks = make_chunks(6)
        clean = raw_v3_question(
            "G1", 0,
            stem="RoundRobin (RR) uses preemption based on what mechanism?",
            options=["Timer expiration", "Manual override", "Disk access", "Network signal"],
            correct_answers=[0],
        )
        result, _saved = self.run_v3(chunks, [{"questions": [clean]}], question_count=1)
        self.assertEqual(len(result["questions"]), 1)

    # --- 11. Configured question type is respected (document scope = single_choice only) -----------
    def test_document_scope_rejects_unrequested_true_false_and_multi_select(self):
        chunks = make_chunks(9)  # 3 groups
        single = raw_v3_question("G1", 0)
        unrequested_true_false = {
            "group_id": "G2", "question_type": "true_false",
            "question": "This mechanism supports reliable delivery.",
            "options": ["True", "False"], "correct_answers": [0],
            "explanation": "The evidence supports this.",
        }
        unrequested_multi_select = {
            "group_id": "G3", "question_type": "multi_select",
            "question": "Which of these does the mechanism support?",
            "options": ["Reliable delivery", "Loss protection", "Encryption", "Compression"],
            "correct_answers": [0, 1], "explanation": "The evidence supports both.",
        }
        result, _saved = self.run_v3(
            chunks, [{"questions": [single, unrequested_true_false, unrequested_multi_select]}],
            question_count=1, scope="document",
        )
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["question_type"], "single_choice")
        self.assertTrue(all(question["question_type"] == "single_choice" for question in result["questions"]))
        reasons = "".join(result["assessment_plan"]["generation_warnings"])
        self.assertIn("configured for", reasons)

    def test_document_scope_prompt_requests_single_choice_only(self):
        groups = _select_v3_context_groups(make_chunks(6))
        prompt = quiz_service._build_v3_prompt(
            "lecture.pdf", {"topic_id": "document", "name": "Entire document"}, "easy", groups, 4, [], False,
            required_question_type="single_choice",
        )
        self.assertIn('"question_type":"single_choice" only', prompt)

    # --- 12. Topic scope keeps its existing mixed-type design ---------------------------------------
    def test_topic_scope_still_allows_mixed_types(self):
        chunks = make_chunks(9)
        single = raw_v3_question("G1", 0)  # fact: supports reliable delivery
        true_false = {
            "group_id": "G2", "question_type": "true_false",
            "question": "This mechanism ensures data integrity throughout delivery.",
            "options": ["True", "False"], "correct_answers": [0],
            "explanation": "The evidence states that it ensures data integrity.",
        }
        multi_select = {
            "group_id": "G3", "question_type": "multi_select",
            "question": "Which of these does the mechanism do while processing packets?",
            "options": ["Buffers incoming data", "Sequences incoming packets", "Encrypts data", "Compresses data"],
            "correct_answers": [0, 1],
            "explanation": "It buffers incoming data and sequences incoming packets in order.",
        }
        result, _saved = self.run_v3(
            chunks, [{"questions": [single, true_false, multi_select]}], question_count=3, scope="topic",
        )
        self.assertEqual(len(result["questions"]), 3)
        self.assertEqual(
            {question["question_type"] for question in result["questions"]},
            {"single_choice", "true_false", "multi_select"},
        )

    # --- 13. multi_select answer consistency ---------------------------------------------------------
    def test_multi_select_correct_answer_matches_first_sorted_correct_answers(self):
        chunks = make_chunks(6)
        out_of_order = {
            "group_id": "G1", "question_type": "multi_select",
            "question": "Which of these does the mechanism support?",
            "options": ["Reliable delivery", "Loss protection", "Encryption", "Compression"],
            "correct_answers": [2, 0],  # model lists them out of ascending order
            "explanation": "The evidence supports both reliable delivery and loss protection.",
        }
        result, _saved = self.run_v3(chunks, [{"questions": [out_of_order]}], question_count=1, scope="topic")
        question = result["questions"][0]
        self.assertEqual(question["correct_answers"], ["A", "C"])
        self.assertEqual(question["correct_answer"], question["correct_answers"][0])
        self.assertEqual(question["correct_answer"], "A")

    # --- 14. Candidate pool selection (coverage-aware) -----------------------------------------------
    def test_selection_spreads_across_groups_instead_of_first_n(self):
        chunks = make_chunks(9)  # 3 groups
        candidates = [
            raw_v3_question(group_id, index)
            for index, group_id in enumerate(["G1", "G1", "G1", "G2", "G2", "G2", "G3", "G3", "G3"])
        ]
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=6)
        represented_groups = {question["concept_id"] for question in result["questions"]}
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(represented_groups, {"G1", "G2", "G3"})

    # --- 15. Targeted fill -------------------------------------------------------------------------
    def test_targeted_fill_tops_up_missing_candidates(self):
        chunks = make_chunks(6)
        initial = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(4)]
        fill = [
            raw_v3_question("G1", 4),
            raw_v3_question("G2", 5),
        ]
        result, saved = self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=6)
        self.assertEqual(len(result["questions"]), 6)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    # --- 16. Partial Quiz --------------------------------------------------------------------------
    def test_partial_quiz_when_fill_is_exhausted(self):
        chunks = make_chunks(6)
        initial = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(4)]
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

    # --- 17. Zero-valid-candidate failure ------------------------------------------------------------
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

    # --- 18. Requested 12 but model returns fewer than the buffered 16 -------------------------------
    def test_requested_twelve_accepts_fewer_than_the_buffered_sixteen(self):
        chunks = make_chunks(15)  # 5 groups, enough for 12 distinct stems without repeats
        candidates = [
            raw_v3_question(f"G{(index % 5) + 1}", index)
            for index in range(12)
        ]
        self.assertEqual(quiz_service._v2_candidate_pool_target(12), 16)
        result, _saved = self.run_v3(chunks, [{"questions": candidates}], question_count=12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["actual_count"], 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["candidates_returned_total"], 12)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["desired_candidate_count"], 16)


class QuizV3PerformanceOptimizationTests(unittest.TestCase):
    """Early stopping and targeted-fill sizing on top of the V3 candidate-pool architecture."""

    def setUp(self):
        FakeV3Model.payloads = []
        FakeV3Model.prompts = []

    run_v3 = QuizV3NoPlannerTests.run_v3

    def test_case1_pool_already_sufficient_skips_repair_and_fill(self):
        chunks = make_chunks(15)  # 5 context groups
        candidates = [
            raw_v3_question(f"G{(index % 5) + 1}", index)
            for index in range(13)
        ]
        result, saved = self.run_v3(chunks, [{"questions": candidates}], question_count=12)
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["repair_attempt_count"], 0)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["fill_attempt_count"], 0)
        self.assertEqual(len(saved), 1)

    def test_case2_single_targeted_fill_call_closes_the_gap(self):
        chunks = make_chunks(15)
        initial = [
            raw_v3_question(f"G{(index % 5) + 1}", index)
            for index in range(10)
        ]
        fill = [
            raw_v3_question("G1", 10),
            raw_v3_question("G2", 11),
            raw_v3_question("G3", 12),
        ]
        result, saved = self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=12)
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(result["assessment_plan"]["status"], "complete")
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    def test_case3_bounded_fill_never_exceeds_configured_maximum(self):
        chunks = make_chunks(15)
        initial = [
            raw_v3_question(f"G{(index % 5) + 1}", index)
            for index in range(10)
        ]
        single_fill = [raw_v3_question("G1", 10)]
        empty = {"questions": []}
        result, saved = self.run_v3(
            chunks, [{"questions": initial}, {"questions": single_fill}, empty, empty, empty],
            question_count=12,
        )
        self.assertEqual(len(result["questions"]), 11)
        self.assertEqual(result["assessment_plan"]["status"], "partial")
        self.assertEqual(result["assessment_plan"]["missing_count"], 1)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 5)
        self.assertEqual(len(saved), 1)

    def test_case4_partial_quiz_when_pool_stays_below_target(self):
        chunks = make_chunks(15)
        initial = [
            raw_v3_question(f"G{(index % 5) + 1}", index)
            for index in range(8)
        ]
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

    def test_every_round_offers_the_full_set_of_context_groups(self):
        """Repair/fill rounds must not restrict which context groups' evidence the model can see
        -- there is no per-group targeting left to reintroduce a blueprint-like restriction.
        Diversity is enforced by validation and selection, not by narrowing the evidence offered."""
        chunks = make_chunks(15)  # 5 groups
        initial = [
            raw_v3_question("G1", index)
            for index in range(10)
        ]
        fill = [raw_v3_question(f"G{(index % 5) + 1}", 10 + index) for index in range(2)]
        self.run_v3(chunks, [{"questions": initial}, {"questions": fill}], question_count=12)
        repair_prompt = FakeV3Model.prompts[1]
        for group_id in ("G1", "G2", "G3", "G4", "G5"):
            self.assertIn(f"{group_id}|", repair_prompt)

    def test_optimization_logging_reports_rejection_breakdown(self):
        chunks = make_chunks(6)
        candidates = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(3)]
        duplicate = raw_v3_question("G1", 0)  # same stem as the first accepted candidate
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
        chunks = make_chunks(6)
        malformed = {"not_questions": True}  # missing "questions" key -> response-level failure
        valid = [raw_v3_question(f"G{(index % 2) + 1}", index) for index in range(2)]
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
