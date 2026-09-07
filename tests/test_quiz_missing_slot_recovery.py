import json
import unittest
from unittest.mock import patch

from backend import quiz_service


def candidate(slot_id, stem):
    return {
        "slot_id": slot_id,
        "question": stem,
        "options": ["Alpha fact", "Beta fact", "Gamma fact", "Delta fact"],
        "correct_answer": 0,
        "explanation": "The cited evidence directly supports Alpha fact.",
    }


def slot(slot_id, topic_id, evidence, source_id, alternatives=(), topic_alternatives=()):
    return {
        "slot_id": slot_id, "topic_id": topic_id, "topic_name": topic_id,
        "concept_id": f"concept-{slot_id}", "name": f"Concept {slot_id}",
        "source_chunk_ids": [source_id], "source_subtopic_ids": [],
        "concept_origin": "structural", "concept_plan_id": "plan",
        "assessment_capacity": 2, "evidence_excerpt": evidence,
        "evidence_variants": [
            {"evidence_excerpt": text, "source_chunk_ids": [chunk_id]}
            for text, chunk_id in alternatives
        ],
        "topic_evidence_variants": [
            {"evidence_excerpt": text, "source_chunk_ids": [chunk_id]}
            for text, chunk_id in topic_alternatives
        ],
    }


class FakeResponse:
    response_metadata = {}

    def __init__(self, questions):
        self.content = json.dumps({"questions": questions})


class SequencedOllama:
    responses = []
    prompts = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return FakeResponse(self.responses.pop(0))


class MissingSlotRecoveryTests(unittest.TestCase):
    def run_batch(self, slots, responses, difficulty="easy"):
        SequencedOllama.responses = list(responses)
        SequencedOllama.prompts = []
        with patch.object(quiz_service, "ChatOllama", SequencedOllama), \
             patch.object(quiz_service, "save_quiz_validation_event"):
            result = quiz_service._run_document_v2_batch(
                {"id": "doc.pdf", "hash": "hash"}, difficulty, slots,
                "owner", "model", len(slots), "run",
            )
        return result, list(SequencedOllama.prompts)

    def test_duplicate_slot_diversifies_angle_and_evidence_to_exact_count(self):
        first = "How does Alpha fact operate in this documented mechanism?"
        second = "Why does secondary behavior change under the documented condition?"
        slots = [
            slot("S1", "topic-a", "alpha original evidence", "a1"),
            slot("S2", "topic-a", "beta original evidence", "b1",
                 alternatives=(("beta alternate evidence", "b2"),)),
        ]
        responses = [
            [candidate("S1", first), candidate("S2", "TBD?")],
            [candidate("S2", first)],
            [candidate("S2", first)],
            [candidate("S2", second)],
        ]
        (questions, _results, timings), prompts = self.run_batch(slots, responses)
        self.assertEqual(len(questions), 2)
        self.assertEqual([q["question"] for q in questions], [first, second])
        self.assertEqual(len(set(q["question"] for q in questions)), 2)
        self.assertIn(first, prompts[1])
        self.assertIn("different question angle", prompts[1])
        self.assertIn("beta alternate evidence", prompts[2])
        self.assertEqual(timings["candidate_stems_by_attempt"][1]["stems"], [first])

    def test_exhausted_concept_evidence_replans_only_missing_slot_without_topic_leakage(self):
        accepted = "How does the accepted mechanism behave in its documented state?"
        replacement = "How should unused topic evidence affect the described outcome?"
        slots = [
            slot("S1", "topic-a", "accepted evidence", "a1"),
            slot("S2", "topic-a", "exhausted evidence", "a2", alternatives=(),
                 topic_alternatives=(("unused topic-a evidence", "a3"),)),
        ]
        duplicate = candidate("S2", accepted)
        responses = [
            [candidate("S1", accepted), candidate("S2", "placeholder...")],
            [duplicate], [duplicate], [duplicate], [candidate("S2", replacement)],
        ]
        (questions, _results, _timings), prompts = self.run_batch(slots, responses)
        self.assertEqual([q["slot_id"] for q in questions], ["S1", "S2"])
        self.assertEqual(questions[1]["source_chunk_ids"], ["a3"])
        self.assertTrue(all("topic-b" not in prompt for prompt in prompts))
        self.assertEqual(sum("S1|" in prompt for prompt in prompts), 1)
        self.assertIn("unused topic-a evidence", prompts[-1])

    def test_medium_does_not_run_easy_only_validation(self):
        group = slot("S1", "topic-a", "Evidence supports Alpha fact.", "a1")
        raw = candidate("S1", "Which mechanism is not changed by the documented relationship?")
        normalized, warnings = quiz_service._validate_v2_question(
            raw, {"S1": group}, {"S1"}, [], "medium", 1,
            {"topic_id": "document", "name": "Entire document"}, 1,
        )
        self.assertEqual(normalized["difficulty"], "medium")
        self.assertNotIn("negative_or_trick_stem", warnings)

    def test_bounded_exhaustion_returns_missing_slot_diagnostics(self):
        first = "How does Alpha fact operate in this documented mechanism?"
        slots = [slot("S1", "topic-a", "alpha", "a1"), slot("S2", "topic-a", "beta", "a2")]
        duplicate = candidate("S2", first)
        responses = [[candidate("S1", first), candidate("S2", "TBD?")], [duplicate], [duplicate], [duplicate], [duplicate]]
        (questions, results, timings), _prompts = self.run_batch(slots, responses)
        self.assertEqual(len(questions), 1)
        self.assertIn("S2", timings["rejection_reasons_by_slot"])
        self.assertGreaterEqual(results["hard_rejections"], 5)
        self.assertEqual(timings["llm_calls"], 5)


if __name__ == "__main__":
    unittest.main()
