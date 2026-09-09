import json
import unittest
from unittest.mock import patch

from backend import quiz_service
from backend.quiz_service import _run_document_v2_batch, _validate_v2_question


DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


def slot(slot_id, topic_number, evidence_phrase):
    return {
        "slot_id": slot_id, "topic_id": f"topic_{topic_number}", "topic_name": f"Topic {topic_number}",
        "concept_id": f"aconcept_{slot_id}", "name": f"Concept {slot_id}", "concept_plan_id": f"plan_{topic_number}",
        "source_subtopic_ids": [f"sub_{topic_number}"], "concept_origin": "structural",
        "source_chunk_ids": [f"chunk_{slot_id}"], "assessment_capacity": 5,
        "evidence_excerpt": f"{evidence_phrase} is documented for {slot_id} under topic {topic_number}.",
    }


def candidate(slot_id, evidence_phrase, question_type="single_choice", options=None, correct_answers=None):
    return {
        "slot_id": slot_id,
        "question": f"{evidence_phrase} directly governs the mechanism described for {slot_id}.",
        "question_type": question_type,
        "options": options or [f"{evidence_phrase} governs it", "Unrelated fact one", "Unrelated fact two", "Unrelated fact three"],
        "correct_answers": correct_answers if correct_answers is not None else [0],
        "explanation": f"The evidence for {slot_id} directly supports {evidence_phrase}.",
    }


class SequencedOllama:
    responses = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, _prompt):
        from types import SimpleNamespace
        return SimpleNamespace(
            content=json.dumps({"questions": self.__class__.responses.pop(0)}),
            response_metadata={},
        )


def run_batch(slots, responses, question_count=None):
    SequencedOllama.responses = list(responses)
    with patch.object(quiz_service, "ChatOllama", SequencedOllama), \
         patch.object(quiz_service, "save_quiz_validation_event"):
        return _run_document_v2_batch(
            DOCUMENT, "medium", slots, "owner", "model", question_count or len(slots), "run-id",
        )


class CrossSlotBindingTests(unittest.TestCase):
    """Regression coverage for the mis-binding bug: candidates returned out of order, or with
    another slot's chunk evidence implied, must never end up attached to the wrong slot."""

    def test_candidates_returned_out_of_order_still_bind_by_their_own_slot_id(self):
        slots = [
            slot("S1", 1, "Page-table lookup"),
            slot("S2", 2, "Address translation"),
            slot("S3", 3, "Interrupt handling"),
        ]
        # Deliberately reversed order relative to the slots list.
        reversed_candidates = [
            candidate("S3", "Interrupt handling"),
            candidate("S2", "Address translation"),
            candidate("S1", "Page-table lookup"),
        ]
        (questions, validation, _timings) = run_batch(slots, [reversed_candidates])
        self.assertEqual(validation["hard_rejections"], 0)
        by_slot = {question["slot_id"]: question for question in questions}
        self.assertEqual(len(by_slot), 3)
        self.assertIn("Page-table lookup", by_slot["S1"]["question"])
        self.assertIn("Address translation", by_slot["S2"]["question"])
        self.assertIn("Interrupt handling", by_slot["S3"]["question"])
        # Metadata must come from each question's OWN slot, never a neighbor's, regardless of
        # the order the model happened to answer in.
        self.assertEqual(by_slot["S1"]["topic_id"], "topic_1")
        self.assertEqual(by_slot["S2"]["topic_id"], "topic_2")
        self.assertEqual(by_slot["S3"]["topic_id"], "topic_3")
        self.assertEqual(by_slot["S1"]["concept_id"], "aconcept_S1")
        self.assertEqual(by_slot["S2"]["concept_id"], "aconcept_S2")
        self.assertEqual(by_slot["S3"]["concept_id"], "aconcept_S3")

    def test_evidence_and_chunk_ids_are_never_reused_from_another_slot(self):
        slots = [
            slot("S1", 1, "Swapping and process scheduling"),
            slot("S2", 2, "Three-level page table structure"),
            slot("S3", 3, "Page replacement policy"),
        ]
        reversed_candidates = [
            candidate("S3", "Page replacement policy"),
            candidate("S2", "Three-level page table structure"),
            candidate("S1", "Swapping and process scheduling"),
        ]
        (questions, validation, _timings) = run_batch(slots, [reversed_candidates])
        self.assertEqual(validation["hard_rejections"], 0)
        by_slot = {question["slot_id"]: question for question in questions}
        self.assertEqual(by_slot["S1"]["source_chunk_ids"], ["chunk_S1"])
        self.assertEqual(by_slot["S2"]["source_chunk_ids"], ["chunk_S2"])
        self.assertEqual(by_slot["S3"]["source_chunk_ids"], ["chunk_S3"])
        for slot_id in ("S1", "S2", "S3"):
            others = {"chunk_S1", "chunk_S2", "chunk_S3"} - {f"chunk_{slot_id}"}
            self.assertFalse(set(by_slot[slot_id]["source_chunk_ids"]) & others)


class SlotIdContractTests(unittest.TestCase):
    """_validate_v2_question must reject missing/duplicate/unknown slot_id, never guess."""

    def setUp(self):
        self.group = slot("S1", 1, "Reliable delivery")
        self.groups_by_id = {"S1": self.group}
        self.base = candidate("S1", "Reliable delivery")

    def test_missing_slot_id_is_rejected(self):
        raw = {**self.base}
        del raw["slot_id"]
        raw.pop("concept_id", None)
        with self.assertRaisesRegex(ValueError, "unknown slot_id"):
            _validate_v2_question(raw, self.groups_by_id, {"S1"}, [], "medium", 1, {"topic_id": "document", "name": "Doc"}, 3)

    def test_unknown_slot_id_is_rejected(self):
        raw = {**self.base, "slot_id": "invented_slot"}
        with self.assertRaisesRegex(ValueError, "unknown slot_id"):
            _validate_v2_question(raw, self.groups_by_id, {"S1"}, [], "medium", 1, {"topic_id": "document", "name": "Doc"}, 3)

    def test_duplicate_slot_id_reuse_is_rejected(self):
        raw = {**self.base}
        # S1 already consumed (not in remaining_slot_ids) even though it exists in groups_by_id.
        with self.assertRaisesRegex(ValueError, "repeats or exceeds"):
            _validate_v2_question(raw, self.groups_by_id, set(), [], "medium", 1, {"topic_id": "document", "name": "Doc"}, 3)


class ExactCountReliabilityTests(unittest.TestCase):
    """The stricter slot/grounding/true_false rules must not regress exact-count recovery."""

    def test_exact_twelve_question_recovery_still_succeeds_after_a_bad_initial_batch(self):
        slots = [slot(f"S{i}", (i % 4) + 1, f"Concept fact {i}") for i in range(1, 13)]
        good_batch = [candidate(f"S{i}", f"Concept fact {i}") for i in range(1, 13)]
        # First response is entirely malformed (missing question text) to force a repair round.
        bad_batch = [{**candidate(f"S{i}", f"Concept fact {i}"), "question": ""} for i in range(1, 13)]
        (questions, validation, _timings) = run_batch(slots, [bad_batch, good_batch], question_count=12)
        self.assertEqual(len(questions), 12)
        self.assertEqual({q["slot_id"] for q in questions}, {f"S{i}" for i in range(1, 13)})
        self.assertGreater(validation["hard_rejections"], 0)


if __name__ == "__main__":
    unittest.main()
