import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.main import QuizGenerateRequest, QuizRegenerateRequest
from backend.mastery_service import calculate_mastery
from backend.quiz_service import QuizGenerationError, _generate_topic_quiz_v2


CHUNK = {
    "content": (
        "TCP acknowledgements support reliable delivery. Flow control protects receivers. "
        "Sequence numbers preserve ordering. Retransmission handles loss. Checksums detect corruption."
    ),
    "metadata": {
        "chunk_id": "canonical_chunk_1",
        "owner_id": "owner",
        "document_id": "lecture.pdf",
        "topic_id": "topic_001",
        "source": "lecture.pdf",
        "page": 1,
    },
}
DOCUMENT = {
    "id": "lecture.pdf",
    "title": "Lecture",
    "hash": "hash",
    "topic_schema_version": 2,
}
TOPIC = {"topic_id": "topic_001", "name": "Reliable transport"}


def raw_question(index: int, source_id: str = "canonical_chunk_1") -> dict:
    stems = [
        "Why do acknowledgements improve reliable transport delivery?",
        "How does flow control protect a receiving endpoint?",
        "What role do sequence numbers play in ordered communication?",
        "When packet loss occurs, how does retransmission help?",
        "How can checksums reveal corruption during transport?",
    ]
    return {
        "question": stems[index % len(stems)] + (f" Consider case {index + 1}." if index >= len(stems) else ""),
        "options": [
            f"It provides supported behavior {index + 1}",
            f"It disables receiver behavior {index + 1}",
            f"It removes ordering behavior {index + 1}",
            f"It prevents delivery behavior {index + 1}",
        ],
        "correct_answer": 0,
        "explanation": "The selected evidence directly supports the first option.",
        "concept_id": f"concept_{index % 5 + 1:03d}",
        "source_chunk_ids": [source_id],
    }


class FakeBatchModel:
    payloads = []
    models = []

    def __init__(self, **kwargs):
        self.__class__.models.append(kwargs.get("model"))

    def invoke(self, _prompt):
        return SimpleNamespace(content=json.dumps(self.__class__.payloads.pop(0)))


class QuizV2Tests(unittest.TestCase):
    def setUp(self):
        FakeBatchModel.payloads = []
        FakeBatchModel.models = []

    def run_v2(self, payloads, question_count=5):
        FakeBatchModel.payloads = list(payloads)
        saved = []
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.validate_question_semantics") as semantic,
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz", side_effect=lambda _d, _x, quiz, _o: saved.append(quiz) or quiz),
        ):
            result = _generate_topic_quiz_v2(
                DOCUMENT, TOPIC, "easy", "owner", "qwen-2.5-3b-runtime", False, question_count
            )
        semantic.assert_not_called()
        return result, saved

    def test_api_model_contract_has_one_optional_field_per_request(self):
        self.assertIn("model_id", QuizGenerateRequest.model_fields)
        self.assertIn("model_id", QuizRegenerateRequest.model_fields)
        request = QuizGenerateRequest(
            document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
            difficulty="easy", model_id="qwen-2.5-3b",
        )
        self.assertEqual(request.model_id, "qwen-2.5-3b")
        self.assertEqual(request.question_count, 5)
        for count in (3, 5, 10):
            self.assertEqual(QuizGenerateRequest(
                document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
                difficulty="easy", question_count=count,
            ).question_count, count)
        with self.assertRaises(ValueError):
            QuizGenerateRequest(
                document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
                difficulty="easy", question_count=4,
            )

    def test_requested_counts_generate_three_five_and_ten_slots(self):
        for count in (3, 5, 10):
            with self.subTest(question_count=count):
                result, saved = self.run_v2(
                    [{"questions": [raw_question(index) for index in range(count)]}], count
                )
                self.assertEqual(len(result["questions"]), count)
                self.assertEqual(result["assessment_plan"]["target_questions"], count)
                self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
                self.assertEqual(len(saved), 1)

    def test_five_questions_use_one_selected_model_call_and_no_semantic_llm(self):
        result, saved = self.run_v2([{"questions": [raw_question(index) for index in range(5)]}])
        self.assertEqual(len(result["questions"]), 5)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertFalse(result["assessment_plan"]["partial"])
        self.assertEqual(FakeBatchModel.models, ["qwen-2.5-3b-runtime"])
        self.assertEqual(len(saved), 1)
        self.assertEqual({question["source_chunk_ids"][0] for question in result["questions"]}, {"canonical_chunk_1"})

    def test_four_questions_are_saved_partial_after_one_repair(self):
        result, saved = self.run_v2([
            {"questions": [raw_question(index) for index in range(4)]},
            {"questions": []},
        ])
        self.assertEqual(len(result["questions"]), 4)
        self.assertTrue(result["assessment_plan"]["partial"])
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)

    def test_fewer_than_four_questions_fail_without_persistence(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index) for index in range(3)]},
            {"questions": []},
        ]
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz") as save,
        ):
            with self.assertRaises(QuizGenerationError) as raised:
                _generate_topic_quiz_v2(DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False)
        self.assertEqual(raised.exception.detail["valid_questions"], 3)
        save.assert_not_called()

    def test_three_question_request_requires_all_three(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index) for index in range(2)]},
            {"questions": []},
        ]
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz") as save,
        ):
            with self.assertRaises(QuizGenerationError) as raised:
                _generate_topic_quiz_v2(
                    DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False, 3
                )
        self.assertEqual(raised.exception.detail["target_questions"], 3)
        self.assertEqual(raised.exception.detail["valid_questions"], 2)
        save.assert_not_called()

    def test_ten_question_request_requires_at_least_eight(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index) for index in range(7)]},
            {"questions": []},
        ]
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz") as save,
        ):
            with self.assertRaises(QuizGenerationError) as raised:
                _generate_topic_quiz_v2(
                    DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False, 10
                )
        self.assertEqual(raised.exception.detail["target_questions"], 10)
        self.assertEqual(raised.exception.detail["valid_questions"], 7)
        save.assert_not_called()

    def test_invented_evidence_ids_are_rejected(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index, "invented_chunk") for index in range(5)]},
            {"questions": []},
        ]
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz") as save,
        ):
            with self.assertRaises(QuizGenerationError):
                _generate_topic_quiz_v2(DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False)
        save.assert_not_called()

    def test_v2_questions_preserve_mastery_concept_coverage_fields(self):
        result, _saved = self.run_v2([{"questions": [raw_question(index) for index in range(5)]}])
        answer_rows = [
            {
                "is_correct": True,
                "question_difficulty": question["difficulty"],
                "validation_outcome": question["validation_outcome"],
                "concept_id": question["concept_id"],
                "assessment_capacity": question["assessment_capacity"],
            }
            for question in result["questions"]
        ]
        mastery = calculate_mastery(answer_rows, completed_attempts=1)
        self.assertEqual(mastery["distinct_concepts_assessed"], 5)
        self.assertEqual(mastery["concept_coverage_ratio"], 1.0)
        self.assertTrue(mastery["has_sufficient_evidence"])

    def test_ten_questions_repeat_concepts_without_inflating_coverage(self):
        result, _saved = self.run_v2(
            [{"questions": [raw_question(index) for index in range(10)]}], 10
        )
        answer_rows = [{
            "is_correct": True,
            "question_difficulty": question["difficulty"],
            "validation_outcome": question["validation_outcome"],
            "concept_id": question["concept_id"],
            "assessment_capacity": question["assessment_capacity"],
        } for question in result["questions"]]
        mastery = calculate_mastery(answer_rows, completed_attempts=1)
        self.assertEqual(mastery["distinct_concepts_assessed"], 5)
        self.assertEqual(mastery["concept_coverage_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
