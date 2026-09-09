import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from backend.quiz_service import (
    DOCUMENT_QUIZ_BATCH_PLAN,
    DOCUMENT_QUIZ_QUESTION_COUNT,
    _assign_document_slot_types,
    _deterministic_multi_select_candidate,
    _deterministic_true_false_candidate,
    _document_slot_propositions,
    _document_slot_type_scores,
    _prepare_document_slot_blueprint,
    _run_document_v2_batch,
    _validate_v2_question,
)


DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
DOCUMENT_SCOPE = {"topic_id": "document", "name": "Entire document"}


def slot(index, topic_number, phrase, fact_count=3):
    facts = " ".join(f"{phrase} fact {word} is documented." for word in ("one", "two", "three")[:fact_count])
    return {
        "slot_id": f"S{index}", "topic_id": f"topic_{topic_number}", "topic_name": f"Topic {topic_number}",
        "concept_id": f"aconcept_{index}", "name": f"Concept {index}", "concept_plan_id": f"plan_{topic_number}",
        "source_subtopic_ids": [f"sub_{topic_number}"], "concept_origin": "structural",
        "source_chunk_ids": [f"chunk_{index}"], "assessment_capacity": 5,
        "evidence_excerpt": facts,
        "evidence_variants": [], "topic_evidence_variants": [],
    }


def fifteen_slots():
    # Vary fact counts (1, 2, or 3 sentences) so the type-assignment heuristic has real signal,
    # matching the shape real evidence would produce.
    fact_pattern = [3, 3, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    return _assign_document_slot_types([
        slot(i, ((i - 1) % 4) + 1, f"Item{i}", fact_pattern[i - 1]) for i in range(1, 16)
    ])


def candidate_for(slot, override_type=None):
    question_type = override_type or slot["question_type"]
    phrase = slot["evidence_excerpt"].split(" fact")[0]
    if question_type == "single_choice":
        return {
            "slot_id": slot["slot_id"], "question_type": question_type,
            "question": f"{phrase} fact one is documented for this concept.",
            "options": [f"{phrase} fact one", "Wrong A", "Wrong B", "Wrong C"], "correct_answers": [0],
            "explanation": f"{phrase} fact one is documented.",
        }
    if question_type == "true_false":
        return {
            "slot_id": slot["slot_id"], "question_type": question_type,
            "question": f"{phrase} fact one is documented for this concept.",
            "options": ["True", "False"], "correct_answers": [0],
            "explanation": f"{phrase} fact one is documented.",
        }
    return {
        "slot_id": slot["slot_id"], "question_type": question_type,
        "question": f"Which statements about {phrase} are documented?",
        "options": [f"{phrase} fact one", f"{phrase} fact two", "Wrong A", "Wrong B"], "correct_answers": [0, 1],
        "explanation": f"{phrase} facts one and two are documented.",
    }


class SequencedOllama:
    """Fake ChatOllama whose response is computed on demand by the test via `respond`."""

    respond = None

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        from types import SimpleNamespace
        candidates = self.__class__.respond(prompt)
        return SimpleNamespace(content=json.dumps({"questions": candidates}), response_metadata={})


def run_blueprint(slots, respond, type_alternates=None):
    SequencedOllama.respond = respond
    with patch.object(quiz_service, "ChatOllama", SequencedOllama), \
         patch.object(quiz_service, "save_quiz_validation_event"):
        return _run_document_v2_batch(
            DOCUMENT, "medium", slots, "owner", "model", DOCUMENT_QUIZ_QUESTION_COUNT, "run-id",
            fixed_type_batches=DOCUMENT_QUIZ_BATCH_PLAN, type_alternates=type_alternates,
        )


def slots_in_prompt(slots, prompt):
    return [s for s in slots if f"{s['slot_id']}|" in prompt]


class ExactBlueprintTests(unittest.TestCase):
    def test_exact_fifteen_count_and_ten_three_two_distribution(self):
        slots = fifteen_slots()
        self.assertEqual(len(slots), DOCUMENT_QUIZ_QUESTION_COUNT)
        self.assertEqual(
            Counter(s["question_type"] for s in slots),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )

        def respond(prompt):
            return [candidate_for(s) for s in slots_in_prompt(slots, prompt)]

        questions, validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(validation["hard_rejections"], 0)
        self.assertEqual(timings["llm_calls"], 4)


class RepairPreservesTypeTests(unittest.TestCase):
    def test_type_assignment_and_no_switching_survive_repair(self):
        """A malformed initial true_false batch (missing question text) must be repaired with
        true_false again, never silently switched to another type."""
        slots = fifteen_slots()
        true_false_slots = {s["slot_id"] for s in slots if s["question_type"] == "true_false"}
        attempts = {"n": 0}

        def respond(prompt):
            requested = slots_in_prompt(slots, prompt)
            is_true_false_batch = requested and requested[0]["slot_id"] in true_false_slots
            if is_true_false_batch and attempts["n"] == 0:
                attempts["n"] += 1
                return [{**candidate_for(s), "question": ""} for s in requested]
            return [candidate_for(s) for s in requested]

        questions, validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(Counter(q["question_type"] for q in questions)["true_false"], 3)
        self.assertGreater(validation["hard_rejections"], 0)
        self.assertGreater(timings["repair_llm_calls"], 0)

    def test_candidate_declaring_the_wrong_type_is_rejected_not_switched(self):
        """Even if the model tries to answer a true_false slot with a single_choice-shaped
        candidate, it must be rejected -- the slot's authoritative type never changes."""
        slots = fifteen_slots()
        true_false_slot = next(s for s in slots if s["question_type"] == "true_false")
        other_true_false = {s["slot_id"] for s in slots if s["question_type"] == "true_false"} - {true_false_slot["slot_id"]}

        def respond(prompt):
            requested = slots_in_prompt(slots, prompt)
            out = []
            for s in requested:
                if s["slot_id"] == true_false_slot["slot_id"]:
                    out.append(candidate_for(s, override_type="single_choice"))
                else:
                    out.append(candidate_for(s))
            return out

        questions, validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        by_slot = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_slot[true_false_slot["slot_id"]]["question_type"], "true_false")
        self.assertGreater(validation["hard_rejections"], 0)


class OutOfOrderBindingTests(unittest.TestCase):
    def test_out_of_order_candidates_still_bind_by_slot_id_within_a_homogeneous_batch(self):
        slots = fifteen_slots()

        def respond(prompt):
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s) for s in reversed(requested)]

        questions, validation, _timings = run_blueprint(slots, respond)
        self.assertEqual(validation["hard_rejections"], 0)
        by_slot = {q["slot_id"]: q for q in questions}
        for s in slots:
            self.assertEqual(by_slot[s["slot_id"]]["topic_id"], s["topic_id"])
            self.assertEqual(by_slot[s["slot_id"]]["concept_id"], s["concept_id"])
            self.assertEqual(by_slot[s["slot_id"]]["source_chunk_ids"], s["source_chunk_ids"])


class TrueFalseAndMultiSelectContractTests(unittest.TestCase):
    def test_true_false_fallback_is_declarative_with_canonical_options(self):
        group = slot(1, 1, "Retransmission", 3)
        group["question_type"] = "true_false"
        raw = _deterministic_true_false_candidate(group, 0)
        self.assertFalse(raw["question"].endswith("?"))
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": group}, {"S1"}, [], "medium", 1, DOCUMENT_SCOPE, 5,
        )
        self.assertEqual(normalized["question_type"], "true_false")
        self.assertEqual(normalized["options"], ["A. True", "B. False"])
        self.assertEqual(len(normalized["correct_answers"]), 1)

    def test_multi_select_fallback_has_two_or_three_correct_and_at_least_one_incorrect(self):
        group = slot(1, 1, "Retransmission", 3)
        group["question_type"] = "multi_select"
        raw = _deterministic_multi_select_candidate(group, 0, ["Sibling fact alpha. Sibling fact beta."])
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": group}, {"S1"}, [], "medium", 1, DOCUMENT_SCOPE, 5,
        )
        self.assertEqual(len(normalized["options"]), 4)
        self.assertIn(len(normalized["correct_answers"]), (2, 3))
        self.assertLess(len(normalized["correct_answers"]), len(normalized["options"]))


class FallbackProvenanceIsolationTests(unittest.TestCase):
    def test_multi_select_fallback_never_reports_sibling_chunk_ids(self):
        group = slot(1, 1, "Retransmission", 3)
        group["question_type"] = "multi_select"
        raw = _deterministic_multi_select_candidate(
            group, 0, ["Sibling fact alpha appears here. Sibling fact beta appears here too."],
        )
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": group}, {"S1"}, [], "medium", 1, DOCUMENT_SCOPE, 5,
        )
        self.assertEqual(normalized["source_chunk_ids"], ["chunk_1"])

    def test_true_false_fallback_never_reports_sibling_chunk_ids(self):
        group = slot(1, 1, "Retransmission", 3)
        group["question_type"] = "true_false"
        raw = _deterministic_true_false_candidate(group, 0)
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": group}, {"S1"}, [], "medium", 1, DOCUMENT_SCOPE, 5,
        )
        self.assertEqual(normalized["source_chunk_ids"], ["chunk_1"])


class ExactRecoveryAfterMalformedOutputTests(unittest.TestCase):
    def test_exact_fifteen_recovery_after_a_fully_malformed_initial_response(self):
        slots = fifteen_slots()

        def respond(prompt):
            requested = slots_in_prompt(slots, prompt)
            # Every initial batch comes back completely unusable (no "questions" content the
            # validator can accept); repair/fill and deterministic fallback must still recover
            # the exact count without ever reassigning a slot's type.
            return [{"slot_id": s["slot_id"], "question_type": s["question_type"]} for s in requested]

        questions, validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertGreater(validation["hard_rejections"], 0)
        self.assertGreater(timings["deterministic_fallback_count"], 0)


class GenerateQuizDocumentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "doc_blueprint.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def test_document_scope_always_uses_fixed_fifteen_regardless_of_requested_count(self):
        document = {
            "id": "doc.pdf", "title": "Doc", "hash": "hash", "topic_schema_version": 2,
            "topics": [{"topic_id": "topic_a", "name": "A"}],
        }

        def planned(topic, _chunks):
            concepts = [{
                "concept_id": f"concept_{i}", "name": f"Concept {i}",
                "source_subtopic_ids": [], "source_chunk_ids": [f"chunk_{i}"],
                "concept_origin": "derived",
            } for i in range(15)]
            return {
                "topic_id": topic["topic_id"], "topic_name": topic["name"],
                "planner_version": quiz_service.PLANNER_VERSION,
                "concept_plan_id": "plan-a", "assessment_capacity": 15,
                "allocated_questions": 0, "concepts": concepts,
            }

        def chunks(_document_id, topic_id, _owner_id):
            return [{
                "content": (
                    f"Concept {i} directly governs this documented mechanism. "
                    f"Concept {i} also affects the resulting system behavior."
                ),
                "metadata": {"chunk_id": f"chunk_{i}", "topic_id": topic_id},
            } for i in range(15)]

        captured = {}

        def fake_batch(_document, _difficulty, planned_slots, _owner, _model, question_count, _run_id, **kwargs):
            captured["question_count"] = question_count
            captured["fixed_type_batches"] = kwargs.get("fixed_type_batches")
            captured["slot_types"] = [s["question_type"] for s in planned_slots]
            questions = [{
                "id": index + 1, "slot_id": s["slot_id"], "question": f"Question {index + 1}?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "correct_answers": ["A"], "question_type": s["question_type"],
                "topic_id": s["topic_id"], "topic_name": s["topic_name"],
                "concept_id": s["concept_id"], "concept_name": s["name"],
                "assessment_capacity": s["assessment_capacity"], "difficulty": "easy",
                "explanation": "Explained.", "source_chunk_ids": s["source_chunk_ids"],
                "validation_outcome": "accepted",
            } for index, s in enumerate(planned_slots)]
            return questions, {"accepted": 15, "accepted_with_warnings": 0, "rejected": 0, "reasons": []}, {"llm_calls": 4}

        with patch.object(quiz_service, "_document_lookup", return_value={"doc.pdf": document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "get_topic_chunks", side_effect=chunks), \
             patch.object(quiz_service, "build_topic_plan", side_effect=planned), \
             patch.object(
                 quiz_service, "_plan_document_slot_types",
                 return_value=(None, {"type_planning_llm_calls": 0, "type_planning_ms": 0}),
             ), \
             patch.object(quiz_service, "_run_document_v2_batch", side_effect=fake_batch):
            result = quiz_service.generate_quiz("doc.pdf", "easy", "document", question_count=12)

        self.assertEqual(captured["question_count"], DOCUMENT_QUIZ_QUESTION_COUNT)
        self.assertEqual(captured["fixed_type_batches"], DOCUMENT_QUIZ_BATCH_PLAN)
        self.assertEqual(
            Counter(captured["slot_types"]),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(result["question_count"], 15)
        self.assertEqual(result["assessment_plan"]["target_questions"], 15)
        self.assertEqual(
            result["assessment_plan"]["type_distribution"],
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )


class SuitabilityScoringTests(unittest.TestCase):
    """Covers the deterministic multi_select/true_false suitability heuristics across
    heterogeneous evidence shapes -- not just raw sentence/fact count."""

    def test_definition_heavy_evidence_is_true_false_suitable(self):
        slot = {"evidence_excerpt": (
            "A page table is a data structure used by a virtual memory system. "
            "It maps virtual addresses to physical addresses."
        )}
        multi_select_score, true_false_score = _document_slot_type_scores(slot)
        self.assertGreaterEqual(true_false_score, 1)
        self.assertGreaterEqual(multi_select_score, 0)

    def test_list_property_heavy_evidence_scores_highest_for_multi_select(self):
        list_slot = {"evidence_excerpt": (
            "Process scheduling has multiple properties. "
            "- Fairness ensures every process gets CPU time. "
            "- Throughput measures completed processes per second. "
            "- Turnaround time measures total completion time."
        )}
        definition_slot = {"evidence_excerpt": (
            "A page table is a data structure used by a virtual memory system. "
            "It maps virtual addresses to physical addresses."
        )}
        list_ms, _list_tf = _document_slot_type_scores(list_slot)
        definition_ms, _definition_tf = _document_slot_type_scores(definition_slot)
        self.assertGreaterEqual(list_ms, 2)
        self.assertGreater(list_ms, definition_ms)

    def test_process_prose_with_multiple_clauses_is_multi_select_suitable(self):
        slot = {"evidence_excerpt": (
            "First the loader reads the header. Then it allocates memory. "
            "Finally it transfers control to the entry point."
        )}
        multi_select_score, true_false_score = _document_slot_type_scores(slot)
        self.assertGreaterEqual(multi_select_score, 2)
        self.assertGreaterEqual(true_false_score, 1)

    def test_noisy_ocr_evidence_is_unsuitable_for_both_types(self):
        slot = {"evidence_excerpt": "PAGE 47 3.2 MEMORY MANAGEMENT OVERVIEW CONTINUED FROM PREVIOUS PAGE"}
        multi_select_score, true_false_score = _document_slot_type_scores(slot)
        self.assertEqual(multi_select_score, 0)
        self.assertEqual(true_false_score, 0)
        self.assertEqual(_document_slot_propositions(slot), [])

    def test_weakly_structured_heading_like_evidence_is_unsuitable_for_both_types(self):
        for evidence in ("Interrupt Handling", "Section 3.2 Overview", "Table 4"):
            with self.subTest(evidence=evidence):
                slot = {"evidence_excerpt": evidence}
                multi_select_score, true_false_score = _document_slot_type_scores(slot)
                self.assertEqual(multi_select_score, 0)
                self.assertEqual(true_false_score, 0)

    def test_a_question_in_evidence_is_never_counted_as_a_usable_proposition(self):
        slot = {"evidence_excerpt": "Why does the scheduler preempt low priority processes?"}
        self.assertEqual(_document_slot_propositions(slot), [])

    def test_type_assignment_still_hits_exact_ten_three_two_across_mixed_quality_evidence(self):
        """Five distinct evidence shapes (definition, list, process prose, noisy OCR, weak
        heading) mixed across 15 slots must still produce exactly 10/3/2, with the noisy/weak
        slots deprioritized rather than accidentally winning a multi_select or true_false slot
        over genuinely suitable evidence."""
        shapes = [
            "Process scheduling has multiple properties. - Fairness ensures every process gets CPU time. "
            "- Throughput measures completed processes per second. - Turnaround time measures total completion time.",
            "Memory allocation has several properties. - Segmentation divides memory into logical units. "
            "- Paging divides memory into fixed-size frames. - Compaction reduces external fragmentation.",
            "A page table is a data structure used by a virtual memory system. "
            "It maps virtual addresses to physical addresses.",
            "First the loader reads the header. Then it allocates memory. "
            "Finally it transfers control to the entry point.",
            "PAGE 47 3.2 MEMORY MANAGEMENT OVERVIEW CONTINUED FROM PREVIOUS PAGE",
            "Interrupt Handling",
        ]
        slots = []
        for index in range(15):
            evidence = shapes[index % len(shapes)]
            slots.append({
                "slot_id": f"S{index + 1}", "topic_id": "topic_1", "topic_name": "Topic 1",
                "concept_id": f"aconcept_{index + 1}", "name": f"Concept {index + 1}",
                "source_chunk_ids": [f"chunk_{index + 1}"], "evidence_excerpt": evidence,
            })
        assigned = _assign_document_slot_types(slots)
        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        by_id = {s["slot_id"]: s for s in assigned}
        # The two richest list-style slots (index 0 and 1, mod 6) must win the multi_select
        # slots ahead of the noisy/weak ones (indices 4 and 5, mod 6).
        list_like_slot_ids = {s["slot_id"] for i, s in enumerate(assigned) if i % 6 in (0, 1)}
        multi_select_slot_ids = {slot_id for slot_id, s in by_id.items() if s["question_type"] == "multi_select"}
        self.assertTrue(multi_select_slot_ids.issubset(list_like_slot_ids))
        noisy_slot_ids = {s["slot_id"] for i, s in enumerate(assigned) if i % 6 in (4, 5)}
        self.assertFalse(noisy_slot_ids & multi_select_slot_ids)


class InfeasibleSlotReassignmentTests(unittest.TestCase):
    """Ranking-fallback guard: even without hard gating, the deterministic ranking must still
    prefer genuinely richer slots over a one-proposition slot like S2 by score, not just by
    index tie-break, when better-scoring alternatives exist. See TypeSwapRecoveryTests below for
    what happens when an assigned special-type slot can't actually be generated."""

    def _weak_field_slots(self):
        # S3 and S5 have >=2 propositions; every other slot (including S2) has exactly 1.
        fact_pattern = {i: 1 for i in range(1, 16)}
        fact_pattern[3] = 3
        fact_pattern[5] = 3
        return [slot(i, ((i - 1) % 4) + 1, f"Item{i}", fact_pattern[i]) for i in range(1, 16)]

    def test_preflight_never_assigns_multi_select_to_a_one_proposition_slot(self):
        assigned = _assign_document_slot_types(self._weak_field_slots())
        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        by_id = {s["slot_id"]: s for s in assigned}
        self.assertNotEqual(by_id["S2"]["question_type"], "multi_select")
        self.assertEqual({s["slot_id"] for s in assigned if s["question_type"] == "multi_select"}, {"S3", "S5"})

    def test_deterministic_fallback_is_constructible_for_both_assigned_multi_select_slots(self):
        assigned = _assign_document_slot_types(self._weak_field_slots())
        multi_select_slots = [s for s in assigned if s["question_type"] == "multi_select"]
        self.assertEqual(len(multi_select_slots), 2)
        for slot_data in multi_select_slots:
            raw = _deterministic_multi_select_candidate(slot_data, 0)
            self.assertEqual(raw["question_type"], "multi_select")
            normalized, _warnings = _validate_v2_question(
                raw, {slot_data["slot_id"]: slot_data}, {slot_data["slot_id"]}, [], "medium", 1, DOCUMENT_SCOPE, 5,
            )
            self.assertGreaterEqual(len(normalized["correct_answers"]), 2)

    def test_generation_reaches_exact_fifteen_even_when_every_llm_response_is_unusable(self):
        """The exact scenario from the bug report: force every batch to fail so every slot must
        go through deterministic fallback, and confirm it no longer stalls at 14/15."""
        slots = _assign_document_slot_types(self._weak_field_slots())

        def respond(_prompt):
            return []

        questions, validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(timings["deterministic_fallback_count"], 15)


class TypePlannerOverridesWeakHeuristicTests(unittest.TestCase):
    """A: the heuristic alone finds 0 usable multi_select/true_false slots (every slot's
    evidence looks like a bare heading), but the LLM type planner still names valid picks --
    generation must proceed using the planner's semantic judgment, not the heuristic's zero
    score. This is the real-world case the old hard proposition-feasibility gate misjudged as
    "0 multi_select and 0 true_false slots" and blocked before the model ever ran."""

    def _all_heading_slots(self):
        weak_slots = [
            {**slot(i, ((i - 1) % 4) + 1, f"Item{i}", 1), "evidence_excerpt": "Heading Only"}
            for i in range(1, 16)
        ]
        for s in weak_slots:
            self.assertEqual(_document_slot_type_scores(s), (0, 0))
        return weak_slots

    def test_planner_rescues_a_document_the_heuristic_scores_zero_everywhere(self):
        weak_slots = self._all_heading_slots()
        planner_result = {
            "multi_select": ["S1", "S2"], "true_false": ["S3", "S4", "S5"],
            "multi_select_alternates": ["S6", "S7"], "true_false_alternates": ["S8", "S9", "S10"],
        }
        with patch.object(
            quiz_service, "_plan_document_slot_types",
            return_value=(planner_result, {"type_planning_llm_calls": 1, "type_planning_ms": 5}),
        ):
            assigned, alternates, timings = _prepare_document_slot_blueprint(weak_slots, "model")

        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        by_id = {s["slot_id"]: s for s in assigned}
        self.assertEqual(by_id["S1"]["question_type"], "multi_select")
        self.assertEqual(by_id["S2"]["question_type"], "multi_select")
        self.assertEqual({by_id["S3"]["question_type"], by_id["S4"]["question_type"], by_id["S5"]["question_type"]}, {"true_false"})
        self.assertEqual(timings["type_planning_llm_calls"], 1)
        self.assertNotIn("S1", alternates["multi_select"])
        self.assertIn("S6", alternates["multi_select"])


class TypePlannerFallbackTests(unittest.TestCase):
    """B: when the type-planning call errors or returns malformed/unusable JSON, the
    deterministic ranking fallback still stamps the exact structural 10/3/2 blueprint --
    generation proceeds, no blueprint hard failure, even for weak/noisy evidence."""

    def _all_heading_slots(self):
        return [
            {**slot(i, 1, f"Item{i}", 1), "evidence_excerpt": "Heading Only"}
            for i in range(1, 16)
        ]

    def test_malformed_planner_json_falls_back_to_ranking_without_blocking(self):
        weak_slots = self._all_heading_slots()
        with patch.object(
            quiz_service, "_plan_document_slot_types",
            return_value=(None, {"type_planning_llm_calls": 1, "type_planning_ms": 3}),
        ):
            assigned, _alternates, _timings = _prepare_document_slot_blueprint(weak_slots, "model")
        self.assertEqual(len(assigned), 15)
        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )

    def test_planner_llm_error_falls_back_and_never_raises(self):
        weak_slots = self._all_heading_slots()

        class RaisingOllama:
            def __init__(self, **_kwargs):
                pass

            def invoke(self, _prompt):
                raise RuntimeError("model unavailable")

        with patch.object(quiz_service, "ChatOllama", RaisingOllama):
            assigned, _alternates, timings = _prepare_document_slot_blueprint(weak_slots, "model")
        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(timings["type_planning_llm_calls"], 1)

    def test_planner_referencing_unknown_slot_ids_is_treated_as_malformed(self):
        weak_slots = self._all_heading_slots()
        bad_result = {
            "multi_select": ["S99", "S100"], "true_false": ["S101"],
            "multi_select_alternates": [], "true_false_alternates": [],
        }
        with patch.object(
            quiz_service, "_plan_document_slot_types",
            return_value=(bad_result, {"type_planning_llm_calls": 1, "type_planning_ms": 2}),
        ):
            assigned, _alternates, _timings = _prepare_document_slot_blueprint(weak_slots, "model")
        # None of the bogus ids exist, so this behaves exactly like an empty planner result --
        # ranking fallback fills the full 10/3/2 from the real 15 slots.
        self.assertEqual(
            Counter(s["question_type"] for s in assigned),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )


class TypeSwapRecoveryTests(unittest.TestCase):
    """C, D, E: an assigned multi_select slot (S2) that repeatedly fails generation must trade
    question_type with a ranked single_choice alternate (S7) -- never its content -- so the
    final blueprint still reaches exactly 15 questions with the exact 10/3/2 distribution."""

    def _typed_slots_with_s2_multi_select(self):
        # A valid starting 10/3/2 blueprint: S1 and S2 are multi_select (S1 always succeeds, S2
        # never does), S3-S5 are true_false, and the remaining 10 (including alternate S7) are
        # single_choice.
        slots = [slot(i, ((i - 1) % 4) + 1, f"Item{i}", 3) for i in range(1, 16)]
        typed = []
        for s in slots:
            if s["slot_id"] in {"S1", "S2"}:
                typed.append({**s, "question_type": "multi_select"})
            elif s["slot_id"] in {"S3", "S4", "S5"}:
                typed.append({**s, "question_type": "true_false"})
            else:
                typed.append({**s, "question_type": "single_choice"})
        return typed

    def test_s2_multi_select_swaps_with_ranked_alternate_after_repeated_failure(self):
        typed_slots = self._typed_slots_with_s2_multi_select()
        original_s2 = next(s for s in typed_slots if s["slot_id"] == "S2")
        original_s7 = next(s for s in typed_slots if s["slot_id"] == "S7")

        def respond(prompt):
            requested = slots_in_prompt(typed_slots, prompt)
            candidates = []
            for s in requested:
                if s["slot_id"] == "S2" and s["question_type"] == "multi_select":
                    continue  # S2-as-multi_select never produces a usable candidate
                candidates.append(candidate_for(s))
            return candidates

        questions, validation, timings = run_blueprint(
            typed_slots, respond, type_alternates={"multi_select": ["S7"], "true_false": []},
        )

        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertGreaterEqual(timings["type_swap_count"], 1)

        by_id = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_id["S2"]["question_type"], "single_choice")
        self.assertEqual(by_id["S7"]["question_type"], "multi_select")

        # D: only question_type ownership changed -- content/topic/concept/source stay bound to
        # their own original slot_id, never swapped between S2 and S7.
        self.assertEqual(by_id["S2"]["topic_id"], original_s2["topic_id"])
        self.assertEqual(by_id["S2"]["concept_id"], original_s2["concept_id"])
        self.assertEqual(by_id["S2"]["source_chunk_ids"], original_s2["source_chunk_ids"])
        self.assertEqual(by_id["S7"]["topic_id"], original_s7["topic_id"])
        self.assertEqual(by_id["S7"]["concept_id"], original_s7["concept_id"])
        self.assertEqual(by_id["S7"]["source_chunk_ids"], original_s7["source_chunk_ids"])
        self.assertNotEqual(by_id["S2"]["concept_id"], by_id["S7"]["concept_id"])

    def test_swapped_slot_repair_prompt_excludes_stale_type_specific_rejection_history(self):
        """S2 fails multi_select with a type-specific structural error ("requires at least two
        correct..."), then swaps with S7. The targeted regeneration prompt for S2's NEW
        single_choice contract must not carry that stale multi_select-specific reason forward;
        content/topic/concept/source stay bound to their own slot throughout."""
        typed_slots = self._typed_slots_with_s2_multi_select()
        original_s2 = next(s for s in typed_slots if s["slot_id"] == "S2")
        original_s7 = next(s for s in typed_slots if s["slot_id"] == "S7")
        captured_prompts = []

        def respond(prompt):
            captured_prompts.append(prompt)
            requested = slots_in_prompt(typed_slots, prompt)
            candidates = []
            for s in requested:
                if s["slot_id"] == "S2" and s["question_type"] == "multi_select":
                    # Deliberately malformed: only one correct answer, which _validate_v2_question
                    # rejects with "multi_select requires at least two correct..." -- a
                    # type-specific structural reason recorded against S2.
                    bad = candidate_for(s)
                    bad["correct_answers"] = [0]
                    candidates.append(bad)
                    continue
                candidates.append(candidate_for(s))
            return candidates

        questions, _validation, timings = run_blueprint(
            typed_slots, respond, type_alternates={"multi_select": ["S7"], "true_false": []},
        )

        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertGreaterEqual(timings["type_swap_count"], 1)

        by_id = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_id["S2"]["question_type"], "single_choice")
        self.assertEqual(by_id["S7"]["question_type"], "multi_select")
        self.assertEqual(by_id["S2"]["topic_id"], original_s2["topic_id"])
        self.assertEqual(by_id["S2"]["concept_id"], original_s2["concept_id"])
        self.assertEqual(by_id["S2"]["source_chunk_ids"], original_s2["source_chunk_ids"])
        self.assertEqual(by_id["S7"]["topic_id"], original_s7["topic_id"])
        self.assertEqual(by_id["S7"]["concept_id"], original_s7["concept_id"])
        self.assertEqual(by_id["S7"]["source_chunk_ids"], original_s7["source_chunk_ids"])

        post_swap_prompts_for_s2 = [
            prompt for prompt in captured_prompts
            if "S2|" in prompt and 'question_type":"single_choice"' in prompt
        ]
        self.assertTrue(post_swap_prompts_for_s2, "expected a post-swap single_choice prompt for S2")
        for prompt in post_swap_prompts_for_s2:
            self.assertNotIn("multi_select requires", prompt.lower())

    def test_type_swap_is_bounded(self):
        typed_slots = self._typed_slots_with_s2_multi_select()

        def respond(prompt):
            requested = slots_in_prompt(typed_slots, prompt)
            candidates = []
            for s in requested:
                if s["question_type"] == "multi_select":
                    continue  # every multi_select attempt fails, no matter which slot holds it
                candidates.append(candidate_for(s))
            return candidates

        _questions, _validation, timings = run_blueprint(
            typed_slots, respond,
            type_alternates={"multi_select": ["S7", "S8", "S9", "S10"], "true_false": []},
        )
        self.assertLessEqual(timings["type_swap_count"], quiz_service.DOCUMENT_TYPE_SWAP_BUDGET)


class NoRawMultiSelectFallbackJunkTests(unittest.TestCase):
    """F: deterministic fallback text must never leak generation-scaffolding phrasing, and
    multi_select fallback must never fabricate facts or emit raw evidence fragments as options."""

    def test_deterministic_fallback_wording_has_no_forbidden_scaffolding_phrases(self):
        rich_slot = slot(1, 1, "Retransmission", 3)
        forbidden = ("documented term", "evidence angle", "selected concept", "first option matches")

        single_choice_raw = quiz_service._deterministic_grounded_candidate(rich_slot, 0)
        multi_select_raw = _deterministic_multi_select_candidate({**rich_slot, "question_type": "multi_select"}, 0)
        true_false_raw = _deterministic_true_false_candidate({**rich_slot, "question_type": "true_false"}, 0)

        for raw in (single_choice_raw, multi_select_raw, true_false_raw):
            rendered = " ".join([raw["question"], *raw["options"], raw["explanation"]]).lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, rendered)

    def test_multi_select_fallback_refuses_to_fabricate_from_thin_evidence(self):
        thin_slot = slot(1, 1, "Thin", 1)
        thin_slot["question_type"] = "multi_select"
        with self.assertRaises(ValueError):
            _deterministic_multi_select_candidate(thin_slot, 0)


class FrontendDocumentBlueprintTests(unittest.TestCase):
    def test_frontend_fixes_document_count_and_shows_type_breakdown(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("const DOCUMENT_QUIZ_QUESTION_COUNT = 15;", script)
        self.assertIn('if (selectedAssessmentScope() === "document") return DOCUMENT_QUIZ_QUESTION_COUNT;', script)
        self.assertIn("if (quizQuestionCountField) quizQuestionCountField.hidden = !topicMode;", script)
        self.assertIn("function documentQuizTypeBreakdown(quiz)", script)
        self.assertIn('`${quiz.questions.length} questions · ${parts.join(" · ")}`', script)
        self.assertIn("Multiple Choice", script)
        self.assertIn("Multiple Select", script)
        # Delete/Regenerate/Retake actions must remain untouched by the document blueprint change.
        self.assertIn('["Delete Quiz", "text-button danger-button", callbacks.remove]', script)
        self.assertIn("regenerate: regenerateAssessmentQuiz", script)
        self.assertIn("retake: resetAssessmentQuiz", script)


if __name__ == "__main__":
    unittest.main()
