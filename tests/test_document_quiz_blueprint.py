import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from backend.quiz_service import (
    DOCUMENT_QUIZ_QUESTION_COUNT,
    _deterministic_multi_select_candidate,
    _deterministic_true_false_candidate,
    _document_slot_propositions,
    _document_slot_type_scores,
    _prepare_document_slot_rankings,
    _rank_document_slots_by_type,
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
    # matching the shape real evidence would produce. No question_type is stamped here -- the
    # special-first pipeline decides each slot's final type during generation.
    fact_pattern = [3, 3, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    return [slot(i, ((i - 1) % 4) + 1, f"Item{i}", fact_pattern[i - 1]) for i in range(1, 16)]


def default_rankings(slots):
    """The pure deterministic ranking, used as type_rankings for tests that don't care which
    specific slots win the special types, just that the pipeline runs end to end."""
    return _rank_document_slots_by_type(slots)


def candidate_for(slot, question_type):
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


def generic_candidate_for(slot, question_type):
    """Candidate fabricator that works for arbitrary evidence prose (not just the "phrase fact
    N" convention `candidate_for` relies on) -- used for cross-domain fixtures."""
    concept = slot.get("name") or slot["slot_id"]
    evidence_words = re.findall(r"[A-Za-z0-9]+", str(slot.get("evidence_excerpt") or ""))[:6]
    anchor = " ".join(evidence_words) or concept
    if question_type == "single_choice":
        return {
            "slot_id": slot["slot_id"], "question_type": question_type,
            "question": f"What does the evidence say about {concept}?",
            "options": [f"{anchor} is documented", "Unrelated claim A", "Unrelated claim B", "Unrelated claim C"],
            "correct_answers": [0],
            "explanation": f"{anchor} is directly documented for {concept}.",
        }
    if question_type == "true_false":
        return {
            "slot_id": slot["slot_id"], "question_type": question_type,
            "question": f"{anchor} is documented for {concept}.",
            "options": ["True", "False"], "correct_answers": [0],
            "explanation": f"{anchor} is documented for {concept}.",
        }
    return {
        "slot_id": slot["slot_id"], "question_type": question_type,
        "question": f"Which statements about {concept} are documented?",
        "options": [f"{anchor} point one", f"{anchor} point two", "Unrelated claim A", "Unrelated claim B"],
        "correct_answers": [0, 1],
        "explanation": f"Both points are documented for {concept}.",
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


def run_blueprint(slots, respond, type_rankings=None):
    SequencedOllama.respond = respond
    with patch.object(quiz_service, "ChatOllama", SequencedOllama), \
         patch.object(quiz_service, "save_quiz_validation_event"):
        return _run_document_v2_batch(
            DOCUMENT, "medium", slots, "owner", "model", DOCUMENT_QUIZ_QUESTION_COUNT, "run-id",
            special_first=True, type_rankings=type_rankings,
        )


def slots_in_prompt(slots, prompt):
    return [s for s in slots if f"{s['slot_id']}|" in prompt]


def required_type_of(prompt):
    """Every call in the special-first pipeline asks for exactly one question_type; recover
    which one from the prompt's explicit type instruction."""
    for question_type in ("multi_select", "true_false", "single_choice"):
        if f'"question_type":"{question_type}" only' in prompt:
            return question_type
    return None


class SpecialFirstExactContractTests(unittest.TestCase):
    def test_exact_fifteen_count_and_ten_three_two_distribution_on_happy_path(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        questions, validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(validation["hard_rejections"], 0)


class SpecialFirstOrderTests(unittest.TestCase):
    """The core architectural guarantee: multi_select and true_false must be fully locked before
    any single_choice slot is even assigned, let alone generated."""

    def test_single_choice_never_generates_before_both_special_types_are_locked(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        call_order = []

        def respond(prompt):
            required_type = required_type_of(prompt)
            call_order.append(required_type)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        questions, _validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertIn("multi_select", call_order)
        self.assertIn("true_false", call_order)
        self.assertIn("single_choice", call_order)

        seen_true_false = False
        seen_single_choice = False
        for required_type in call_order:
            if required_type == "multi_select":
                self.assertFalse(seen_true_false, "multi_select requested after true_false started")
                self.assertFalse(seen_single_choice, "multi_select requested after single_choice started")
            if required_type == "true_false":
                self.assertFalse(seen_single_choice, "true_false requested after single_choice started")
                seen_true_false = True
            if required_type == "single_choice":
                seen_single_choice = True


class RankedWalkRecoveryTests(unittest.TestCase):
    """generate -> targeted repair -> next ranked content slot: a slot whose special-type
    attempt fails, live, is never left missing -- the walk moves on, and the failed slot remains
    fully available to become single_choice afterward."""

    def test_first_ranked_multi_select_candidate_fails_second_succeeds_first_becomes_single_choice(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        failing_slot_id = rankings["multi_select"][0]
        expected_ms_winner = rankings["multi_select"][1]

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            candidates = []
            for s in requested:
                if s["slot_id"] == failing_slot_id and required_type in ("multi_select", "true_false"):
                    continue  # never produces a usable special-type candidate, live or repaired
                candidates.append(candidate_for(s, required_type))
            return candidates

        questions, _validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        by_id = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_id[expected_ms_winner]["question_type"], "multi_select")
        self.assertEqual(by_id[failing_slot_id]["question_type"], "single_choice")


class OverlappingRankingsTests(unittest.TestCase):
    def test_same_top_ranked_slot_for_both_types_locks_once_and_the_other_type_moves_on(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        shared_top = rankings["multi_select"][0]
        tf_ranking = [shared_top] + [sid for sid in rankings["true_false"] if sid != shared_top]
        rankings = {**rankings, "true_false": tf_ranking}

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        questions, _validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        by_id = {q["slot_id"]: q for q in questions}
        # multi_select locks first (special types lock in that order); true_false's walk must
        # have skipped the already-locked slot and moved on to its own next ranked slot.
        self.assertEqual(by_id[shared_top]["question_type"], "multi_select")
        true_false_slot_ids = {q["slot_id"] for q in questions if q["question_type"] == "true_false"}
        self.assertEqual(len(true_false_slot_ids), 3)
        self.assertNotIn(shared_top, true_false_slot_ids)


class RejectionHistoryScopingTests(unittest.TestCase):
    def test_failed_multi_select_history_never_appears_in_that_slots_later_single_choice_prompt(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        target = rankings["multi_select"][0]
        # Keep target out of true_false's early picks so it only ever fails as multi_select and
        # falls through to single_choice (true_false locks its 3 well before reaching the tail).
        rankings = {**rankings, "true_false": [sid for sid in rankings["true_false"] if sid != target] + [target]}
        captured_prompts = []
        sc_attempts = {"count": 0}

        def respond(prompt):
            required_type = required_type_of(prompt)
            captured_prompts.append((required_type, prompt))
            requested = slots_in_prompt(slots, prompt)
            candidates = []
            for s in requested:
                if s["slot_id"] == target and required_type == "multi_select":
                    # Structurally invalid: one correct answer for multi_select -- rejected with
                    # a distinctive type-specific reason ("multi_select requires...").
                    bad = candidate_for(s, "multi_select")
                    bad["correct_answers"] = [0]
                    candidates.append(bad)
                    continue
                if s["slot_id"] == target and required_type == "single_choice" and sc_attempts["count"] == 0:
                    sc_attempts["count"] += 1
                    continue  # force a repair call so we can inspect its prompt
                candidates.append(candidate_for(s, required_type))
            return candidates

        questions, _validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        by_id = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_id[target]["question_type"], "single_choice")

        single_choice_prompts_for_target = [
            prompt for required_type, prompt in captured_prompts
            if required_type == "single_choice" and f"{target}|" in prompt
        ]
        self.assertGreaterEqual(len(single_choice_prompts_for_target), 2)
        for prompt in single_choice_prompts_for_target:
            self.assertNotIn("multi_select requires", prompt.lower())


class NoAcceptedSingleChoiceInvalidatedTests(unittest.TestCase):
    def test_no_post_hoc_type_swap_machinery_remains(self):
        self.assertFalse(hasattr(quiz_service, "DOCUMENT_TYPE_SWAP_BUDGET"))
        self.assertFalse(hasattr(quiz_service, "_is_type_specific_rejection_reason"))
        self.assertFalse(hasattr(quiz_service, "DOCUMENT_QUIZ_BATCH_PLAN"))

    def test_accepted_single_choice_questions_are_never_removed_when_special_types_struggle(self):
        """Even when both special types must fall all the way through to deterministic fallback,
        every single_choice question survives untouched -- there is no later phase that revisits
        or evicts an accepted slot."""
        slots = fifteen_slots()
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            if required_type in ("multi_select", "true_false"):
                return []  # every live special-type attempt fails; only fallback can lock them
            return [candidate_for(s, required_type) for s in requested]

        questions, _validation, timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(timings["deterministic_fallback_count"], 5)
        single_choice_slot_ids = {q["slot_id"] for q in questions if q["question_type"] == "single_choice"}
        self.assertEqual(len(single_choice_slot_ids), 10)


class RepairPreservesTypeTests(unittest.TestCase):
    def test_type_assignment_and_no_switching_survive_repair(self):
        """A malformed initial true_false attempt (missing question text) must be repaired with
        true_false again, never silently switched to another type."""
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        attempts = {"n": 0}

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            if required_type == "true_false" and attempts["n"] == 0:
                attempts["n"] += 1
                return [{**candidate_for(s, required_type), "question": ""} for s in requested]
            return [candidate_for(s, required_type) for s in requested]

        questions, validation, timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(Counter(q["question_type"] for q in questions)["true_false"], 3)
        self.assertGreater(validation["hard_rejections"], 0)
        self.assertGreater(timings["repair_llm_calls"], 0)

    def test_candidate_declaring_the_wrong_type_is_rejected_not_switched(self):
        """Even if the model answers a true_false attempt with a single_choice-shaped candidate,
        it must be rejected -- the attempt's required type never silently changes."""
        slots = fifteen_slots()
        rankings = default_rankings(slots)
        wrong_type_used = {"done": False}

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            if required_type == "true_false" and not wrong_type_used["done"]:
                wrong_type_used["done"] = True
                return [candidate_for(s, "single_choice") for s in requested]
            return [candidate_for(s, required_type) for s in requested]

        questions, validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(Counter(q["question_type"] for q in questions)["true_false"], 3)
        self.assertGreater(validation["hard_rejections"], 0)


class OutOfOrderBindingTests(unittest.TestCase):
    def test_out_of_order_candidates_still_bind_by_slot_id_within_a_homogeneous_batch(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in reversed(requested)]

        questions, validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
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
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            # Every attempt comes back completely unusable (no real question content the
            # validator can accept); repair/fill and deterministic fallback must still recover
            # the exact count without ever reassigning a slot's already-locked type.
            return [{"slot_id": s["slot_id"], "question_type": required_type} for s in requested]

        questions, validation, timings = run_blueprint(slots, respond, type_rankings=rankings)
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
            captured["type_rankings"] = kwargs.get("type_rankings")
            captured["planned_slots"] = planned_slots
            questions = [{
                "id": index + 1, "slot_id": s["slot_id"], "question": f"Question {index + 1}?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "correct_answers": ["A"],
                "question_type": (
                    "multi_select" if index < 2 else "true_false" if index < 5 else "single_choice"
                ),
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
                 quiz_service, "_plan_document_slot_rankings",
                 return_value=(None, {"type_planning_llm_calls": 0, "type_planning_ms": 0}),
             ), \
             patch.object(quiz_service, "_run_document_v2_batch", side_effect=fake_batch):
            result = quiz_service.generate_quiz("doc.pdf", "easy", "document", question_count=12)

        self.assertEqual(captured["question_count"], DOCUMENT_QUIZ_QUESTION_COUNT)
        self.assertEqual(len(captured["planned_slots"]), 15)
        # No slot is pre-stamped with a question_type before generation -- the special-first walk
        # inside _run_document_v2_batch is what decides each slot's final type.
        self.assertTrue(all("question_type" not in s for s in captured["planned_slots"]))
        self.assertIsNotNone(captured["type_rankings"])
        self.assertEqual(set(captured["type_rankings"].keys()), {"multi_select", "true_false"})
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

    def test_ranking_still_prioritizes_richer_evidence_across_mixed_quality_shapes(self):
        """Five distinct evidence shapes (definition, list, process prose, noisy OCR, weak
        heading) mixed across 15 slots must still rank the noisy/weak slots below genuinely
        suitable evidence for both special types, never ahead of it."""
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
        ranked = _rank_document_slots_by_type(slots)
        top_multi_select = set(ranked["multi_select"][:2])
        # The two richest list-style slots (index 0 and 1, mod 6) must rank ahead of the
        # noisy/weak ones (indices 4 and 5, mod 6) for multi_select.
        list_like_slot_ids = {s["slot_id"] for i, s in enumerate(slots) if i % 6 in (0, 1)}
        self.assertTrue(top_multi_select.issubset(list_like_slot_ids))
        noisy_slot_ids = {s["slot_id"] for i, s in enumerate(slots) if i % 6 in (4, 5)}
        self.assertFalse(noisy_slot_ids & top_multi_select)


class InfeasibleSlotReassignmentTests(unittest.TestCase):
    """Ranking guard: even without hard gating, the deterministic ranking must still prefer
    genuinely richer slots over a one-proposition slot like S2 by score, not just by index
    tie-break, when better-scoring alternatives exist."""

    def _weak_field_slots(self):
        # S3 and S5 have >=2 propositions; every other slot (including S2) has exactly 1.
        fact_pattern = {i: 1 for i in range(1, 16)}
        fact_pattern[3] = 3
        fact_pattern[5] = 3
        return [slot(i, ((i - 1) % 4) + 1, f"Item{i}", fact_pattern[i]) for i in range(1, 16)]

    def test_ranking_never_prefers_a_one_proposition_slot_over_a_richer_one(self):
        ranked = _rank_document_slots_by_type(self._weak_field_slots())
        self.assertEqual(set(ranked["multi_select"][:2]), {"S3", "S5"})
        self.assertNotIn("S2", ranked["multi_select"][:2])

    def test_deterministic_fallback_is_constructible_for_the_top_ranked_multi_select_slots(self):
        weak_slots = self._weak_field_slots()
        by_id = {s["slot_id"]: s for s in weak_slots}
        ranked = _rank_document_slots_by_type(weak_slots)
        for slot_id in ranked["multi_select"][:2]:
            slot_data = {**by_id[slot_id], "question_type": "multi_select"}
            raw = _deterministic_multi_select_candidate(slot_data, 0)
            self.assertEqual(raw["question_type"], "multi_select")
            normalized, _warnings = _validate_v2_question(
                raw, {slot_id: slot_data}, {slot_id}, [], "medium", 1, DOCUMENT_SCOPE, 5,
            )
            self.assertGreaterEqual(len(normalized["correct_answers"]), 2)

    def test_generation_reaches_exact_fifteen_even_when_every_llm_response_is_unusable(self):
        """The exact scenario from the bug report: force every LLM batch to fail so every slot
        must go through deterministic fallback, and confirm it no longer stalls short of 15 --
        proven directly against the special-first pipeline, with no pre-assignment step."""
        slots = self._weak_field_slots()

        def respond(_prompt):
            return []

        questions, _validation, timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(timings["deterministic_fallback_count"], 15)


class TypePlannerOverridesWeakHeuristicTests(unittest.TestCase):
    """The heuristic alone finds 0 usable multi_select/true_false slots (every slot's evidence
    looks like a bare heading), but the LLM type planner still names a real ranking -- the
    special-first walk must use the planner's semantic judgment, not the heuristic's zero score,
    to decide which slots to even attempt first. This is the real-world case the old hard
    proposition-feasibility gate misjudged as "no usable slots" and blocked before the model ever
    ran."""

    def _all_heading_slots(self):
        weak_slots = [
            {**slot(i, ((i - 1) % 4) + 1, f"Item{i}", 1), "evidence_excerpt": "Heading Only"}
            for i in range(1, 16)
        ]
        for s in weak_slots:
            self.assertEqual(_document_slot_type_scores(s), (0, 0))
        return weak_slots

    def test_planner_ranking_is_used_first_even_when_the_heuristic_scores_everything_zero(self):
        weak_slots = self._all_heading_slots()
        planner_result = {"multi_select": ["S1", "S2"], "true_false": ["S3", "S4", "S5"]}
        with patch.object(
            quiz_service, "_plan_document_slot_rankings",
            return_value=(planner_result, {"type_planning_llm_calls": 1, "type_planning_ms": 5}),
        ):
            slots, type_rankings, timings = _prepare_document_slot_rankings(weak_slots, "model")

        self.assertEqual(len(slots), 15)
        self.assertTrue(all("question_type" not in s for s in slots))
        self.assertEqual(type_rankings["multi_select"][:2], ["S1", "S2"])
        self.assertEqual(type_rankings["true_false"][:3], ["S3", "S4", "S5"])
        self.assertEqual(len(type_rankings["multi_select"]), 15)
        self.assertEqual(len(type_rankings["true_false"]), 15)
        self.assertEqual(timings["type_planning_llm_calls"], 1)

    def test_planner_ranking_actually_drives_which_slots_lock_as_special_types(self):
        # S3, S4, S5 each have exactly one proposition -> heuristic multi_select score is 0 for
        # S3/S4, yet the planner naming them must still be what the walk tries first. Only the
        # planner's picks are made to succeed -- the rest of the batched window (backfilled from
        # the deterministic ranking) deliberately fails, so which slots lock is unambiguous.
        slots = fifteen_slots()
        planner_result = {"multi_select": ["S3", "S4"], "true_false": ["S5", "S6", "S7"]}
        with patch.object(
            quiz_service, "_plan_document_slot_rankings",
            return_value=(planner_result, {"type_planning_llm_calls": 1, "type_planning_ms": 5}),
        ):
            planned_slots, type_rankings, _timings = _prepare_document_slot_rankings(slots, "model")

        planner_picks = {"S3", "S4", "S5", "S6", "S7"}

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(planned_slots, prompt)
            return [
                candidate_for(s, required_type) for s in requested
                if required_type == "single_choice" or s["slot_id"] in planner_picks
            ]

        questions, _validation, _timings = run_blueprint(planned_slots, respond, type_rankings=type_rankings)
        self.assertEqual(len(questions), 15)
        by_id = {q["slot_id"]: q for q in questions}
        self.assertEqual(by_id["S3"]["question_type"], "multi_select")
        self.assertEqual(by_id["S4"]["question_type"], "multi_select")
        self.assertEqual(
            {by_id["S5"]["question_type"], by_id["S6"]["question_type"], by_id["S7"]["question_type"]},
            {"true_false"},
        )


class TypePlannerFallbackTests(unittest.TestCase):
    """When the type-planning call errors or returns malformed/unusable JSON, the deterministic
    ranking fallback still produces a FULL ranking (every slot_id, for both types) -- generation
    proceeds; nothing here ever raises for weak/noisy evidence."""

    def _all_heading_slots(self):
        return [
            {**slot(i, 1, f"Item{i}", 1), "evidence_excerpt": "Heading Only"}
            for i in range(1, 16)
        ]

    def test_malformed_planner_json_falls_back_to_full_ranking_without_blocking(self):
        weak_slots = self._all_heading_slots()
        with patch.object(
            quiz_service, "_plan_document_slot_rankings",
            return_value=(None, {"type_planning_llm_calls": 1, "type_planning_ms": 3}),
        ):
            slots, type_rankings, _timings = _prepare_document_slot_rankings(weak_slots, "model")
        self.assertEqual(len(slots), 15)
        self.assertEqual(len(type_rankings["multi_select"]), 15)
        self.assertEqual(len(type_rankings["true_false"]), 15)
        self.assertEqual(set(type_rankings["multi_select"]), {f"S{i}" for i in range(1, 16)})

    def test_planner_llm_error_falls_back_and_never_raises(self):
        weak_slots = self._all_heading_slots()

        class RaisingOllama:
            def __init__(self, **_kwargs):
                pass

            def invoke(self, _prompt):
                raise RuntimeError("model unavailable")

        with patch.object(quiz_service, "ChatOllama", RaisingOllama):
            slots, type_rankings, timings = _prepare_document_slot_rankings(weak_slots, "model")
        self.assertEqual(len(slots), 15)
        self.assertEqual(len(type_rankings["multi_select"]), 15)
        self.assertEqual(len(type_rankings["true_false"]), 15)
        self.assertEqual(timings["type_planning_llm_calls"], 1)

    def test_planner_referencing_unknown_slot_ids_is_treated_as_malformed(self):
        weak_slots = self._all_heading_slots()
        bad_result = {"multi_select": ["S99", "S100"], "true_false": ["S101"]}
        with patch.object(
            quiz_service, "_plan_document_slot_rankings",
            return_value=(bad_result, {"type_planning_llm_calls": 1, "type_planning_ms": 2}),
        ):
            _slots, type_rankings, _timings = _prepare_document_slot_rankings(weak_slots, "model")
        # None of the bogus ids exist, so the merged ranking is exactly the deterministic one.
        self.assertEqual(set(type_rankings["multi_select"]), {f"S{i}" for i in range(1, 16)})
        self.assertNotIn("S99", type_rankings["multi_select"])


class NoRawMultiSelectFallbackJunkTests(unittest.TestCase):
    """Deterministic fallback text must never leak generation-scaffolding phrasing, and
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


class CrossDomainEvidenceRegressionTests(unittest.TestCase):
    """Generalization regression: the special-first type-assignment pipeline must be driven only
    by each slot's own evidence shape (proposition count, sentence structure, list markers) --
    never by document title, topic name, chunk id, or domain vocabulary. These fixtures stand in
    for content that looks nothing alike -- textbook definitions, tutorial steps, report prose,
    policy rules, and noisy/bilingual text -- and every one of them flows through the exact same
    _rank_document_slots_by_type / _run_document_v2_batch special-first code path as any other
    document. There is no per-fixture or per-domain branch anywhere here."""

    DEFINITION_TEXTBOOK = (
        "A binary search tree is a data structure in which every node has at most two children. "
        "Each node's left subtree contains only values less than the node's own value."
    )
    PROCESS_TUTORIAL = (
        "Building the project takes three steps. First, install the dependencies with the package "
        "manager. Next, run the build script to compile the sources. Finally, launch the compiled "
        "binary to verify the build succeeded."
    )
    RESEARCH_REPORT = (
        "The survey found that remote adoption increased by twenty percent over the prior year. "
        "Researchers attributed the growth to expanded broadband access in rural regions. "
        "The report also noted that regional disparities in adoption remained significant."
    )
    RULES_POLICY = (
        "Employees must submit expense reports within thirty days of purchase. Reimbursement "
        "requires an original receipt attached to the report. Requests submitted after the "
        "deadline will not be processed."
    )
    NOISY_OCR = "PAGE 12 4.1 QUARTERLY COMPLIANCE SUMMARY CONTINUED FROM PREVIOUS PAGE"
    BILINGUAL = (
        "Chuong nay trinh bay tong quan ve quan ly bo nho. Bo nho ao (virtual memory) cho phep "
        "he dieu hanh mo rong khong gian dia chi vuot qua dung luong RAM vat ly thuc te."
    )

    ALL_FIXTURES = (
        ("definition_textbook", DEFINITION_TEXTBOOK),
        ("process_tutorial", PROCESS_TUTORIAL),
        ("research_report", RESEARCH_REPORT),
        ("rules_policy", RULES_POLICY),
        ("noisy_ocr", NOISY_OCR),
        ("bilingual", BILINGUAL),
    )

    def _slot(self, index, topic_number, evidence):
        return {
            "slot_id": f"S{index}", "topic_id": f"topic_{topic_number}", "topic_name": f"Topic {topic_number}",
            "concept_id": f"concept_{index}", "name": f"Concept {index}",
            "source_chunk_ids": [f"chunk_{index}"], "evidence_excerpt": evidence,
        }

    def test_suitability_scoring_never_raises_across_heterogeneous_evidence_shapes(self):
        for label, evidence in self.ALL_FIXTURES:
            with self.subTest(evidence=label):
                scores = _document_slot_type_scores({"evidence_excerpt": evidence})
                self.assertIsInstance(scores, tuple)

    def test_process_tutorial_steps_score_as_multi_select_suitable(self):
        multi_select_score, true_false_score = _document_slot_type_scores(
            {"evidence_excerpt": self.PROCESS_TUTORIAL}
        )
        self.assertGreaterEqual(multi_select_score, 2)
        self.assertGreaterEqual(true_false_score, 1)

    def test_single_proposition_prose_scores_as_true_false_suitable_regardless_of_domain(self):
        for label, evidence in (
            ("definition_textbook", self.DEFINITION_TEXTBOOK),
            ("research_report", self.RESEARCH_REPORT),
            ("rules_policy", self.RULES_POLICY),
        ):
            with self.subTest(evidence=label):
                _multi_select_score, true_false_score = _document_slot_type_scores({"evidence_excerpt": evidence})
                self.assertGreaterEqual(true_false_score, 1)

    def test_bilingual_evidence_is_scored_by_structure_not_rejected_for_non_english_text(self):
        multi_select_score, true_false_score = _document_slot_type_scores({"evidence_excerpt": self.BILINGUAL})
        self.assertGreaterEqual(true_false_score, 1)
        self.assertGreaterEqual(multi_select_score, 2)

    def test_noisy_running_header_evidence_is_deprioritized_not_crashed(self):
        multi_select_score, true_false_score = _document_slot_type_scores({"evidence_excerpt": self.NOISY_OCR})
        self.assertEqual((multi_select_score, true_false_score), (0, 0))

    def test_ranking_never_prefers_the_noisy_fixture_across_a_mixed_domain_document(self):
        shapes = [evidence for _label, evidence in self.ALL_FIXTURES]
        slots = [self._slot(i + 1, (i % 4) + 1, shapes[i % len(shapes)]) for i in range(15)]
        ranked = _rank_document_slots_by_type(slots)
        noisy_index = [label for label, _evidence in self.ALL_FIXTURES].index("noisy_ocr")
        noisy_slot_ids = {f"S{i + 1}" for i in range(15) if i % len(shapes) == noisy_index}
        self.assertFalse(noisy_slot_ids & set(ranked["multi_select"][:2]))
        self.assertFalse(noisy_slot_ids & set(ranked["true_false"][:3]))

    def test_generation_reaches_exact_fifteen_via_special_first_pipeline_across_mixed_domains(self):
        """Full pipeline regression: the special-first pipeline (lock 2 multi_select + 3
        true_false by walking ranked evidence-shape scores, THEN assign single_choice to the
        rest) reaches exactly 15 questions with the exact 10/3/2 split for a document mixing
        textbook, tutorial, report, policy, noisy, and bilingual evidence -- proving there is no
        per-domain branch anywhere in the path, and that the noisy fixture is never forced into a
        special-type slot it cannot support."""
        shapes = [evidence for _label, evidence in self.ALL_FIXTURES]
        slots = [self._slot(i + 1, (i % 4) + 1, shapes[i % len(shapes)]) for i in range(15)]

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [generic_candidate_for(s, required_type) for s in requested]

        questions, _validation, _timings = run_blueprint(slots, respond)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        noisy_index = [label for label, _evidence in self.ALL_FIXTURES].index("noisy_ocr")
        noisy_slot_ids = {f"S{i + 1}" for i in range(15) if i % len(shapes) == noisy_index}
        by_id = {q["slot_id"]: q for q in questions}
        for slot_id in noisy_slot_ids:
            self.assertEqual(by_id[slot_id]["question_type"], "single_choice")


def _special_incapable_slot(index, topic_number):
    """Evidence with no extractable words at all -- genuinely incapable of every construction
    path multi_select/true_false rely on, including the deterministic term-extraction fallback
    (unlike a bare heading like "Section 3.2", which the term-extraction fallback can still turn
    into a low-quality but valid true_false statement)."""
    return {**slot(index, topic_number, f"Item{index}", 1), "evidence_excerpt": "   "}


class SpecialQuotaGateHardeningTests(unittest.TestCase):
    """Reliability/performance hardening: special candidates are batched into windows (not one
    LLM call per slot), and a hard quota gate blocks single_choice generation entirely unless
    both special-type quotas were met -- never persisting a 13/15 or 14/15 quiz."""

    def _one_ms_capable_slots(self):
        # Only S1 has >=2 propositions (multi_select-capable, live or via fallback); every other
        # slot is heading-only -- genuinely incapable of multi_select or true_false, live or via
        # deterministic fallback -- so at most 1 multi_select can ever lock.
        slots = [slot(1, 1, "Item1", 3)]
        slots += [_special_incapable_slot(i, ((i - 1) % 4) + 1) for i in range(2, 16)]
        return slots

    def _two_tf_capable_slots(self):
        # S1/S2 are multi_select-capable (locks the MS quota cleanly); S3/S4 are true_false-
        # capable; everything else is heading-only -- genuinely incapable of either special type,
        # live or via deterministic fallback -- so at most 2 true_false can ever lock.
        slots = [slot(1, 1, "Item1", 3), slot(2, 2, "Item2", 3)]
        slots += [slot(3, 3, "Item3", 1), slot(4, 4, "Item4", 1)]
        slots += [_special_incapable_slot(i, ((i - 1) % 4) + 1) for i in range(5, 16)]
        return slots

    def test_a_only_one_multi_select_locks_fails_before_any_single_choice_generation(self):
        slots = self._one_ms_capable_slots()
        call_order = []

        def respond(prompt):
            required_type = required_type_of(prompt)
            call_order.append(required_type)
            requested = slots_in_prompt(slots, prompt)
            if required_type == "multi_select":
                return [candidate_for(s, "multi_select") for s in requested if s["slot_id"] == "S1"]
            return []  # true_false and single_choice must never even be usable here

        with self.assertRaises(quiz_service.QuizGenerationError) as raised:
            run_blueprint(slots, respond)

        self.assertNotIn("single_choice", call_order)
        detail = raised.exception.detail
        self.assertEqual(detail["stage"], "blueprint")
        joined_summary = " ".join(detail["failure_summary"]).lower()
        self.assertIn("multi_select", joined_summary)
        self.assertIn("required 2", joined_summary)
        self.assertIn("locked 1", joined_summary)

    def test_b_only_two_true_false_lock_fails_before_single_choice_generation(self):
        slots = self._two_tf_capable_slots()
        call_order = []

        def respond(prompt):
            required_type = required_type_of(prompt)
            call_order.append(required_type)
            requested = slots_in_prompt(slots, prompt)
            if required_type == "multi_select":
                return [candidate_for(s, "multi_select") for s in requested if s["slot_id"] in {"S1", "S2"}]
            if required_type == "true_false":
                return [candidate_for(s, "true_false") for s in requested if s["slot_id"] in {"S3", "S4"}]
            return []  # single_choice must never even be requested

        with self.assertRaises(quiz_service.QuizGenerationError) as raised:
            run_blueprint(slots, respond)

        self.assertNotIn("single_choice", call_order)
        detail = raised.exception.detail
        self.assertEqual(detail["stage"], "blueprint")
        joined_summary = " ".join(detail["failure_summary"]).lower()
        self.assertIn("true_false", joined_summary)
        self.assertIn("required 3", joined_summary)
        self.assertIn("locked 2", joined_summary)

    def test_c_normal_happy_path_batches_special_candidates_and_hits_exact_contract(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        questions, validation, timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        self.assertEqual(
            Counter(q["question_type"] for q in questions),
            {"single_choice": 10, "true_false": 3, "multi_select": 2},
        )
        self.assertEqual(validation["hard_rejections"], 0)
        self.assertEqual(len(timings["locked_multi_select_slot_ids"]), 2)
        self.assertEqual(len(timings["locked_true_false_slot_ids"]), 3)
        single_choice_slot_ids = {q["slot_id"] for q in questions if q["question_type"] == "single_choice"}
        self.assertEqual(len(single_choice_slot_ids), 10)
        # Batched: exactly one LLM call locked each special type on the happy path, not one call
        # per candidate slot.
        self.assertEqual(timings["special_generation_llm_calls"], 2)

    def test_d_normal_happy_path_uses_approximately_five_llm_calls_not_per_slot_calls(self):
        slots = fifteen_slots()

        with patch.object(
            quiz_service, "_plan_document_slot_rankings",
            return_value=(None, {"type_planning_llm_calls": 1, "type_planning_ms": 1}),
        ):
            planned_slots, type_rankings, planning_timings = _prepare_document_slot_rankings(slots, "model")

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(planned_slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        _questions, _validation, timings = run_blueprint(planned_slots, respond, type_rankings=type_rankings)
        # 1 type-planner call + 1 multi_select batch + 1 true_false batch + 2 single_choice
        # batches == 5 total, independent of slot count -- never one call per candidate slot.
        total_calls = planning_timings["type_planning_llm_calls"] + timings["llm_calls"]
        self.assertEqual(total_calls, 5)
        self.assertEqual(timings["special_generation_llm_calls"], 2)
        self.assertEqual(timings["single_choice_generation_llm_calls"], 2)
        self.assertLess(timings["special_candidate_attempts"], DOCUMENT_QUIZ_QUESTION_COUNT)

    def test_e_original_slot_topic_concept_source_metadata_is_preserved(self):
        slots = fifteen_slots()
        rankings = default_rankings(slots)

        def respond(prompt):
            required_type = required_type_of(prompt)
            requested = slots_in_prompt(slots, prompt)
            return [candidate_for(s, required_type) for s in requested]

        questions, _validation, _timings = run_blueprint(slots, respond, type_rankings=rankings)
        self.assertEqual(len(questions), 15)
        by_id = {q["slot_id"]: q for q in questions}
        for original_slot in slots:
            question = by_id[original_slot["slot_id"]]
            self.assertEqual(question["topic_id"], original_slot["topic_id"])
            self.assertEqual(question["concept_id"], original_slot["concept_id"])
            self.assertEqual(question["source_chunk_ids"], original_slot["source_chunk_ids"])


if __name__ == "__main__":
    unittest.main()
