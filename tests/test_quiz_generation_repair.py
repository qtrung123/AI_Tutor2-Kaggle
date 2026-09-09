import unittest

from backend.quiz_service import _build_v2_prompt, _validate_v2_question


GROUP = {
    "slot_id": "S1", "topic_id": "topic_1", "topic_name": "Transport",
    "concept_id": "reliability", "name": "Reliability", "source_chunk_ids": ["chunk_1"],
    "assessment_capacity": 3,
    "evidence_excerpt": "Address translation is handled by hardware in modern systems using a memory management unit.",
}
TOPIC = {"topic_id": "topic_1", "name": "Transport"}


def true_false_question(options):
    return {
        "slot_id": "S1", "question_type": "true_false",
        "question": "The memory management unit performs address translation using hardware support.",
        "options": options, "correct_answers": [0],
        "explanation": "The evidence states hardware performs this translation.",
    }


class TrueFalseLabelNormalizationTests(unittest.TestCase):
    """Reproduces the runtime failure: models punctuate True/False ("True.", "'False'") and the
    exact-match validator rejected them as a content failure, exhausting bounded repair for an
    otherwise-correct question."""

    def test_trailing_punctuation_and_quotes_are_normalized_not_rejected(self):
        for noisy_options in (
            ["True.", "False."],
            ["'True'", "“False”"],
            ["true", "FALSE"],
        ):
            with self.subTest(options=noisy_options):
                question, warnings = _validate_v2_question(
                    true_false_question(noisy_options), {"S1": GROUP}, {"S1"}, [], "medium", 1, TOPIC, 3,
                )
                self.assertEqual(question["options"], ["A. True", "B. False"])
                self.assertIn("true_false_label_normalized", warnings)

    def test_clean_true_false_options_are_not_flagged_as_normalized(self):
        question, warnings = _validate_v2_question(
            true_false_question(["True", "False"]), {"S1": GROUP}, {"S1"}, [], "medium", 1, TOPIC, 3,
        )
        self.assertEqual(question["options"], ["A. True", "B. False"])
        self.assertNotIn("true_false_label_normalized", warnings)

    def test_genuinely_wrong_true_false_options_still_rejected(self):
        with self.assertRaises(ValueError):
            _validate_v2_question(
                true_false_question(["Yes", "No"]), {"S1": GROUP}, {"S1"}, [], "medium", 1, TOPIC, 3,
            )


class TargetedRepairPromptTests(unittest.TestCase):
    """A structural rejection (wrong option/answer count, bad question_type, ...) should ask the
    model to fix that exact detail, not to invent a different question angle."""

    def test_structural_rejection_asks_for_a_targeted_fix_not_a_new_angle(self):
        prompt = _build_v2_prompt(
            "lecture.pdf", TOPIC, "easy", [{"slot_id": "S1", "evidence_excerpt": GROUP["evidence_excerpt"]}], 1,
            [], True,
            rejection_reasons_by_slot={"S1": ["true_false question must contain exactly 2 options."]},
            retry_attempt_by_slot={"S1": 1},
        )
        self.assertIn("only fix that exact structural problem", prompt)
        self.assertNotIn("diversification attempt", prompt)

    def test_content_rejection_still_asks_for_a_different_angle(self):
        prompt = _build_v2_prompt(
            "lecture.pdf", TOPIC, "easy", [{"slot_id": "S1", "evidence_excerpt": GROUP["evidence_excerpt"]}], 1,
            ["Which mechanism supports delivery?"], True,
            rejection_reasons_by_slot={"S1": ["Question duplicates an accepted question."]},
            retry_attempt_by_slot={"S1": 1},
        )
        self.assertIn("diversification attempt", prompt)
        self.assertNotIn("only fix that exact structural problem", prompt)


if __name__ == "__main__":
    unittest.main()
