import unittest

from backend.quiz_service import (
    _deterministic_grounded_candidate,
    _looks_like_raw_chunk,
    _validate_v2_question,
)


def slot(evidence, slot_id="S1"):
    return {
        "slot_id": slot_id, "topic_id": "topic_1", "topic_name": "Transport",
        "concept_id": "reliability", "name": "Reliability", "source_chunk_ids": ["chunk_1"],
        "assessment_capacity": 3, "evidence_excerpt": evidence,
    }


TOPIC = {"topic_id": "topic_1", "name": "Transport"}


class DeterministicFallbackSanitizationTests(unittest.TestCase):
    """Reproduces the runtime failure: deterministic fallback copied raw evidence (long run-on
    sentences, message headers, emails) straight into options, which the SAME validator then
    rejected, leaving the slot missing after both repair and fallback were exhausted."""

    def test_fallback_with_raw_email_and_header_evidence_still_validates(self):
        # Pre-fix: propositions[0] was the raw "Reply-To: ...@example.com ..." sentence, which
        # _deterministic_grounded_candidate picked as the correct option and the validator then
        # rejected with "Option contains a raw header or email address."
        evidence = (
            "Reply-To: sender@example.com regarding the previous meeting notes. "
            "Acknowledgements confirm reliable delivery. Sequence numbers preserve ordering. "
            "Flow control protects receivers from being overwhelmed."
        )
        raw = _deterministic_grounded_candidate(slot(evidence), 0)
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": slot(evidence)}, {"S1"}, [], "easy", 1, TOPIC, 3,
        )
        rendered = " ".join([normalized["question"], *normalized["options"], normalized["explanation"]])
        self.assertNotIn("@", rendered)
        self.assertNotIn("Reply-To:", rendered)

    def test_fallback_with_long_run_on_evidence_produces_short_options(self):
        # Pre-fix: the 34-word run-on sentence was used verbatim as an option and the validator
        # rejected it with "Option contains long or malformed raw evidence text."
        long_sentence = (
            "Retransmission recovers losses when acknowledgements are delayed beyond the "
            "estimated round trip time and the sender's timer expires before a duplicate "
            "acknowledgement confirms that the original segment actually arrived at the receiver."
        )
        evidence = (
            f"{long_sentence} Checksums detect corruption. Flow control regulates senders. "
            "Sequence numbers order segments."
        )
        raw = _deterministic_grounded_candidate(slot(evidence), 0)
        for option in raw["options"]:
            self.assertFalse(_looks_like_raw_chunk(option), option)
            self.assertLessEqual(len(option.split()), 14)
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": slot(evidence)}, {"S1"}, [], "easy", 1, TOPIC, 3,
        )
        self.assertEqual(len(normalized["options"]), 4)

    def test_fallback_still_grounded_and_structurally_valid_single_choice(self):
        evidence = "Alpha controls delivery. Alpha preserves ordering. Alpha detects corruption. Alpha regulates flow."
        raw = _deterministic_grounded_candidate(slot(evidence), 0)
        self.assertEqual(raw["question_type"], "single_choice")
        normalized, _warnings = _validate_v2_question(
            raw, {"S1": slot(evidence)}, {"S1"}, [], "easy", 1, TOPIC, 3,
        )
        self.assertEqual(normalized["question_type"], "single_choice")
        self.assertEqual(len(normalized["options"]), 4)
        self.assertEqual(normalized["correct_answers"], ["A"])
        self.assertEqual(normalized["concept_id"], "reliability")
        self.assertEqual(normalized["source_chunk_ids"], ["chunk_1"])

    def test_fallback_rejects_evidence_that_is_entirely_header_and_email_noise(self):
        evidence = "From: a@example.com To: b@example.com Subject: hi Reply-To: c@example.com"
        with self.assertRaises(ValueError):
            _deterministic_grounded_candidate(slot(evidence), 0)


if __name__ == "__main__":
    unittest.main()
