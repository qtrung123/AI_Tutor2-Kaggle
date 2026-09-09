import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.main import QuizGenerateRequest, QuizRegenerateRequest
from backend.quiz_service import (
    _build_v2_prompt,
    _deterministic_grounded_candidate,
    _validate_v2_question,
)


GROUP = {
    "slot_id": "S1", "topic_id": "topic_1", "topic_name": "Transport",
    "concept_id": "reliability", "name": "Reliability", "source_chunk_ids": ["chunk_1"],
    "assessment_capacity": 3,
    "evidence_excerpt": "Acknowledgements confirm delivery while sequence numbers preserve ordering.",
}
TOPIC = {"topic_id": "topic_1", "name": "Transport"}


class QuizCountAndContentPolicyTests(unittest.TestCase):
    def test_only_twelve_and_fifteen_are_allowed_and_frontend_defaults_to_twelve(self):
        for model in (QuizGenerateRequest, QuizRegenerateRequest):
            required = {"difficulty": "easy", "assessment_scope": "document"}
            if model is QuizGenerateRequest:
                required["document_id"] = "lecture.pdf"
            self.assertEqual(model(**required).question_count, 12)
            for count in (12, 15):
                self.assertEqual(model(**required, question_count=count).question_count, count)
            for count in (10, 20, 25):
                with self.assertRaises(ValidationError):
                    model(**required, question_count=count)
        frontend = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("[12, 15].includes(value) ? value : 12", frontend)
        self.assertIn("[12, 15].forEach((count)", frontend)

    def test_initial_prompt_requires_evidence_led_mixed_types_without_ratio(self):
        prompt = _build_v2_prompt("lecture.pdf", TOPIC, "easy", [GROUP], 1, [], False)
        for question_type in ("single_choice", "true_false", "multi_select"):
            self.assertIn(question_type, prompt)
        self.assertIn("from what its evidence can assess naturally", prompt)
        self.assertIn("with no fixed ratio", prompt)
        self.assertIn("another valid question_type or a different same-topic angle", prompt)

    def test_true_false_rejects_either_or_interrogative_stems(self):
        base = {
            "slot_id": "S1", "question_type": "true_false",
            "options": ["True", "False"], "correct_answers": [0],
            "explanation": "The evidence states hardware performs this translation.",
        }
        with self.assertRaises(ValueError):
            _validate_v2_question(
                {**base, "question": "Is address translation handled by the hardware or by software?"},
                {"S1": GROUP}, {"S1"}, [], "medium", 1, TOPIC, 3,
            )
        question, _warnings = _validate_v2_question(
            {**base, "question": "Address translation is handled by hardware in this system."},
            {"S1": GROUP}, {"S1"}, [], "medium", 1, TOPIC, 3,
        )
        self.assertEqual(question["question_type"], "true_false")

    def test_final_question_rejects_scaffolding_headers_emails_and_raw_evidence(self):
        base = {
            "slot_id": "S1", "question_type": "single_choice",
            "question": "How do acknowledgements confirm reliable delivery?",
            "options": ["They report receipt", "They encrypt data", "They route packets", "They compress payloads"],
            "correct_answers": [0], "explanation": "Acknowledgements report successful receipt to the sender.",
        }
        bad_values = (
            "Which evidence angle explains reliable delivery?",
            "Subject: private notes about delivery mechanisms",
            "Contact lecturer@example.edu about reliable delivery details",
            " ".join(["copied"] * 33),
        )
        for bad_value in bad_values:
            with self.subTest(value=bad_value[:30]), self.assertRaises(ValueError):
                _validate_v2_question(
                    {**base, "question": bad_value}, {"S1": GROUP}, {"S1"}, [],
                    "easy", 1, TOPIC, 3,
                )

    def test_deterministic_fallback_does_not_emit_forbidden_scaffolding(self):
        candidate = _deterministic_grounded_candidate(
            GROUP, 0,
            ["Retransmission recovers losses. Checksums identify corruption. Flow control protects receivers."],
        )
        rendered = " ".join([candidate["question"], *candidate["options"], candidate["explanation"]]).lower()
        for phrase in ("evidence angle", "selected concept", "source-backed"):
            self.assertNotIn(phrase, rendered)


if __name__ == "__main__":
    unittest.main()
