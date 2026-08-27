import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.main import QuizGenerateRequest, QuizRegenerateRequest
from backend.mastery_service import calculate_mastery
from backend.quiz_options import strip_leading_option_label
from backend.quiz_service import (
    QuizGenerationError, _generate_topic_quiz_v2, _run_document_v2_batch, _validate_quiz_batch,
    _validate_v2_question,
)


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
    mechanisms = [
        "congestion recovery", "window scaling", "selective acknowledgement", "timeout estimation",
        "connection establishment", "receiver buffering", "segment framing", "duplicate detection",
        "loss recovery", "delivery confirmation", "sequence wraparound", "delayed acknowledgement",
        "adaptive retransmission", "ordered reassembly", "corruption detection", "flow regulation",
        "sender throttling", "round trip sampling", "state synchronization", "endpoint negotiation",
        "payload verification", "stream reconstruction", "failure recovery", "packet accounting",
        "transport coordination",
    ]
    stem = stems[index] if index < len(stems) else f"How does {mechanisms[index]} contribute to reliable communication?"
    return {
        "question": stem,
        "options": [
            f"It supports reliable communication {index + 1}",
            f"It disables receiver behavior {index + 1}",
            f"It removes ordering behavior {index + 1}",
            f"It prevents delivery behavior {index + 1}",
        ],
        "correct_answer": 0,
        "explanation": "The selected evidence directly supports the first option.",
        "slot_id": f"S{index + 1}",
    }


class FakeBatchModel:
    payloads = []
    models = []
    configurations = []
    prompts = []

    def __init__(self, **kwargs):
        self.__class__.models.append(kwargs.get("model"))
        self.__class__.configurations.append(kwargs)

    def invoke(self, prompt):
        self.__class__.prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(self.__class__.payloads.pop(0)), response_metadata={
            "load_duration": 2_000_000, "prompt_eval_duration": 3_000_000,
            "eval_duration": 4_000_000, "prompt_eval_count": 500, "eval_count": 300,
        })


class QuizV2Tests(unittest.TestCase):
    def setUp(self):
        FakeBatchModel.payloads = []
        FakeBatchModel.models = []
        FakeBatchModel.configurations = []
        FakeBatchModel.prompts = []

    def run_v2(self, payloads, question_count=10):
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

    def run_document_batch(self, payloads, question_count=10):
        FakeBatchModel.payloads = list(payloads)
        slots = []
        for index in range(question_count):
            topic_number = index % 4 + 1
            slots.append({
                "slot_id": f"S{index + 1}", "topic_id": f"topic_{topic_number}",
                "topic_name": f"Topic {topic_number}", "concept_id": f"aconcept_{index % 5}",
                "name": f"Concept {index % 5}", "concept_plan_id": f"plan_{topic_number}",
                "source_subtopic_ids": [f"sub_{topic_number}"], "concept_origin": "structural",
                "source_chunk_ids": [f"chunk_{topic_number}"], "assessment_capacity": 5,
                "evidence_excerpt": f"Grounded evidence supports reliable communication for topic {topic_number} and slot {index + 1}.",
            })
        with (
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
        ):
            questions, validation, timings = _run_document_v2_batch(
                {**DOCUMENT, "id": "lecture.pdf"}, "easy", slots, "owner",
                "qwen-2.5-3b-runtime", question_count, "run-id",
            )
        return questions, validation, timings, slots

    def validate_easy_candidate(self, stem, options, correct_answer, explanation, evidence):
        group = {
            "slot_id": "S1", "topic_id": "topic_001", "topic_name": "Scheduling",
            "concept_id": "concept_scheduler", "name": "Scheduler", "concept_plan_id": "plan_1",
            "source_subtopic_ids": [], "concept_origin": "structural", "source_chunk_ids": ["chunk_1"],
            "assessment_capacity": 1, "evidence_excerpt": evidence,
        }
        return _validate_v2_question(
            {"slot_id": "S1", "question": stem, "options": options,
             "correct_answer": correct_answer, "explanation": explanation},
            {"S1": group}, {"S1"}, [], "easy", 1, TOPIC, 1,
        )

    def test_api_model_contract_has_one_optional_field_per_request(self):
        self.assertIn("model_id", QuizGenerateRequest.model_fields)
        self.assertIn("model_id", QuizRegenerateRequest.model_fields)
        request = QuizGenerateRequest(
            document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
            difficulty="easy", model_id="qwen-2.5-3b",
        )
        self.assertEqual(request.model_id, "qwen-2.5-3b")
        self.assertEqual(request.question_count, 10)
        for count in (10, 15, 20, 25):
            self.assertEqual(QuizGenerateRequest(
                document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
                difficulty="easy", question_count=count,
            ).question_count, count)
        with self.assertRaises(ValueError):
            QuizGenerateRequest(
                document_id="lecture.pdf", assessment_scope="topic", topic_id="topic_001",
                difficulty="easy", question_count=5,
            )
        frontend = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("[10, 15, 20, 25].includes(value)", frontend)
        self.assertIn("[10, 15, 20, 25].forEach((count)", frontend)
        self.assertNotIn("[3, 5, 10]", frontend)

    def test_requested_counts_generate_exact_supported_slots(self):
        for count in (10, 15, 20, 25):
            with self.subTest(question_count=count):
                result, saved = self.run_v2(
                    [{"questions": [raw_question(index) for index in range(count)]}], count
                )
                self.assertEqual(len(result["questions"]), count)
                self.assertEqual(result["assessment_plan"]["target_questions"], count)
                self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
                self.assertEqual(len(saved), 1)

    def test_ten_questions_use_one_selected_model_call_and_no_semantic_llm(self):
        result, saved = self.run_v2([{"questions": [raw_question(index) for index in range(10)]}])
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertFalse(result["assessment_plan"]["partial"])
        self.assertEqual(FakeBatchModel.models, ["qwen-2.5-3b-runtime"])
        self.assertEqual(len(saved), 1)
        self.assertEqual({question["source_chunk_ids"][0] for question in result["questions"]}, {"canonical_chunk_1"})
        self.assertEqual(FakeBatchModel.configurations[0]["keep_alive"], "10m")
        self.assertEqual(FakeBatchModel.configurations[0]["num_ctx"], 8192)
        self.assertEqual(FakeBatchModel.configurations[0]["num_predict"], 1500)
        self.assertNotIn("source_chunk_ids", FakeBatchModel.prompts[0])
        self.assertIn('"slot_id":"S1"', FakeBatchModel.prompts[0])
        self.assertLess(len(FakeBatchModel.prompts[0]), 3400)
        timings = result["assessment_plan"]["timings_ms"]
        self.assertEqual(timings["model_load_ms"], 2)
        self.assertEqual(timings["prompt_eval_ms"], 3)
        self.assertEqual(timings["token_generation_ms"], 4)

    def test_option_prefixes_are_stripped_once_without_damaging_normal_words(self):
        cases = {
            "A. Real-time operation": "Real-time operation",
            "B) No kernel": "No kernel",
            "C: No processes": "No processes",
            "D- Deterministic timing": "Deterministic timing",
            "A real-time operation": "real-time operation",
            "b. lowercase label": "lowercase label",
            "c) lowercase parenthesis": "lowercase parenthesis",
            "d - lowercase dash": "lowercase dash",
            "Application software": "Application software",
            "Database transaction": "Database transaction",
        }
        self.assertEqual({value: strip_leading_option_label(value) for value in cases}, cases)

    def test_topic_document_and_legacy_generation_canonicalize_model_option_labels(self):
        expected = ["A. Reliable delivery", "B. No kernel", "C. No processes", "D. Application software"]
        prefixed = ["A. Reliable delivery", "b) No kernel", "C: No processes", "Application software"]
        topic_questions = [raw_question(index) for index in range(10)]
        topic_questions[0]["options"] = prefixed
        topic, _saved = self.run_v2([{"questions": topic_questions}])
        self.assertEqual(topic["questions"][0]["options"], expected)

        document_questions = [raw_question(index) for index in range(10)]
        document_questions[0]["options"] = ["A Reliable communication", "B - No kernel", "c. No processes", "Application software"]
        document, _validation, _timings, _slots = self.run_document_batch([{"questions": document_questions}])
        self.assertEqual(document[0]["options"], ["A. Reliable communication", *expected[1:]])

        legacy = _validate_quiz_batch(
            {"questions": [{
                "question": "Which option describes supported real-time system behavior?",
                "options": prefixed,
                "correct_answer": "A",
                "explanation": "The evidence supports real-time operation in this system.",
            }]},
            1, 1, source_chunk_ids=["canonical_chunk_1"],
        )
        self.assertEqual(legacy[0]["options"], expected)

    def test_easy_quality_warns_for_two_tinyos_scheduler_answers(self):
        _question, warnings = self.validate_easy_candidate(
            "How does the TinyOS scheduler run queued tasks?",
            ["Runs tasks in FIFO order", "Does not preempt running tasks", "Uses timed priorities", "Runs every task concurrently"],
            0, "TinyOS runs queued tasks in FIFO order.",
            "The TinyOS scheduler runs queued tasks in FIFO order and does not preempt a running task.",
        )
        self.assertIn("distractor_similar_to_evidence", warnings)

    def test_easy_quality_rejects_negative_and_partial_patterns(self):
        cases = [
            (
                "Which scheduler behavior is NOT used by TinyOS?",
                ["Runs queued tasks", "Uses FIFO order", "Avoids task preemption", "Runs one task"], 1,
                "TinyOS uses FIFO order for queued tasks.",
                "TinyOS runs queued tasks in FIFO order without task preemption.", "negative or trick",
            ),
            (
                "What are the four objectives of the scheduler?",
                ["Low latency", "High throughput", "Fair execution", "Small memory use"], 0,
                "The objectives include low latency, throughput, fairness, and small memory use.",
                "The four objectives are low latency, high throughput, fair execution, and small memory use.", "partial answer",
            ),
        ]
        for stem, options, answer, explanation, evidence, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.validate_easy_candidate(stem, options, answer, explanation, evidence)

    def test_easy_quality_heuristics_are_warnings_not_rejections(self):
        _question, warnings = self.validate_easy_candidate(
            "How does the TinyOS scheduler order queued tasks?",
            ["Uses deterministic selection", "Linux always uses round-robin scheduling", "Uses random ordering", "Uses deadline ordering"], 0,
            "The scheduler selects work predictably.",
            "The TinyOS scheduler processes queued tasks using FIFO order.",
        )
        self.assertIn("insufficient_lexical_support", warnings)
        self.assertIn("outside_world_distractor", warnings)
        self.assertIn("unsupported_absolute_term", warnings)

    def test_clean_easy_recall_question_is_accepted(self):
        question, warnings = self.validate_easy_candidate(
            "How does the TinyOS scheduler order queued tasks?",
            ["Uses FIFO order", "Uses random ordering", "Uses deadline ordering", "Uses reverse arrival order"], 0,
            "TinyOS uses FIFO order for queued tasks.",
            "The TinyOS scheduler orders queued tasks using FIFO order.",
        )
        self.assertEqual(question["correct_answer"], "A")
        self.assertNotIn("difficulty_mismatch", warnings)

    def test_easy_quality_rejection_tracks_one_targeted_repair_call(self):
        initial = [raw_question(index) for index in range(10)]
        initial[0]["question"] = "Which behavior is NOT supported by the evidence?"
        questions, validation, timings, _slots = self.run_document_batch([
            {"questions": initial}, {"questions": [raw_question(0)]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual(validation["hard_rejections"], 1)
        self.assertEqual(validation["quality_warnings"], 0)
        self.assertEqual(timings["repair_llm_calls"], 1)
        self.assertIn("EASY QUALITY:", FakeBatchModel.prompts[1])

    def test_easy_quality_warnings_do_not_trigger_repair(self):
        initial = [raw_question(index) for index in range(10)]
        initial[0]["options"][1] = "Linux always uses round-robin scheduling"
        questions, validation, timings, _slots = self.run_document_batch([{"questions": initial}])
        self.assertEqual(len(questions), 10)
        self.assertGreaterEqual(validation["quality_warnings"], 2)
        self.assertEqual(validation["hard_rejections"], 0)
        self.assertEqual(timings["repair_llm_calls"], 0)

    def test_partial_initial_generation_repairs_only_missing_slot(self):
        result, saved = self.run_v2([
            {"questions": [raw_question(index) for index in range(9)]},
            {"questions": [raw_question(9)]},
        ])
        self.assertEqual(len(result["questions"]), 10)
        self.assertFalse(result["assessment_plan"]["partial"])
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(saved), 1)
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[1])
        self.assertIn("S10|", FakeBatchModel.prompts[1])
        self.assertNotIn("S9|", FakeBatchModel.prompts[1])

    def test_repair_failure_reports_counts_and_does_not_persist(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index) for index in range(8)]},
            {"questions": []},
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
        self.assertEqual(raised.exception.detail["requested_count"], 10)
        self.assertEqual(raised.exception.detail["valid_count"], 8)
        self.assertEqual(raised.exception.detail["missing_count"], 2)
        self.assertTrue(raised.exception.detail["failure_summary"])
        save.assert_not_called()

    def test_invented_slot_is_rejected_and_only_missing_slot_is_repaired(self):
        questions = [raw_question(index) for index in range(10)]
        questions[0]["slot_id"] = "invented_slot"
        FakeBatchModel.payloads = [{"questions": questions}, {"questions": [raw_question(0)]}]
        with (
            patch("backend.quiz_service.get_topic_chunks", return_value=[CHUNK]),
            patch("backend.quiz_service.ChatOllama", FakeBatchModel),
            patch("backend.quiz_service.save_quiz_validation_event"),
            patch("backend.quiz_service.save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            result = _generate_topic_quiz_v2(DOCUMENT, TOPIC, "easy", "owner", "qwen-3b", False)
        self.assertEqual(len(result["questions"]), 10)
        self.assertFalse(result["assessment_plan"]["partial"])
        self.assertEqual(len(FakeBatchModel.configurations), 2)
        self.assertEqual(FakeBatchModel.configurations[1]["num_predict"], 520)
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[1])

    def test_duplicate_repair_is_rejected_then_only_missing_slot_is_retried(self):
        first = [raw_question(index) for index in range(9)]
        duplicate = raw_question(0)
        duplicate["slot_id"] = "S10"
        repaired = raw_question(9)
        FakeBatchModel.payloads = [
            {"questions": first}, {"questions": [duplicate]}, {"questions": [repaired]},
        ]
        result, saved = self.run_v2(FakeBatchModel.payloads)
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(len({question["question"] for question in result["questions"]}), 10)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 3)
        self.assertEqual(len(saved), 1)

    def test_v2_questions_preserve_mastery_concept_coverage_fields(self):
        result, _saved = self.run_v2([{"questions": [raw_question(index) for index in range(10)]}])
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

    def test_document_supported_counts_use_one_initial_batch_call(self):
        for count in (10, 15, 20, 25):
            with self.subTest(question_count=count):
                FakeBatchModel.models = []
                FakeBatchModel.configurations = []
                FakeBatchModel.prompts = []
                questions, validation, timings, _slots = self.run_document_batch(
                    [{"questions": [raw_question(index) for index in range(count)]}], count
                )
                self.assertEqual(len(questions), count)
                self.assertEqual(validation["accepted"], count)
                self.assertEqual(timings["llm_calls"], 1)
                self.assertEqual(timings["prompt_tokens"], 500)
                self.assertEqual(timings["output_tokens"], 300)
                self.assertIn("Topic 1", FakeBatchModel.prompts[0])

    def test_document_batch_repairs_only_invalid_slot_and_owns_metadata(self):
        initial = [raw_question(index) for index in range(9)]
        duplicate = raw_question(0)
        duplicate["slot_id"] = "S10"
        duplicate["topic_id"] = "invented-topic"
        repaired = raw_question(9)
        questions, validation, timings, slots = self.run_document_batch([
            {"questions": initial + [duplicate]}, {"questions": [repaired]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual(len({question["question"] for question in questions}), 10)
        self.assertEqual(timings["llm_calls"], 2)
        self.assertGreaterEqual(timings["repair_ms"], 0)
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[1])
        self.assertIn("S10|Topic 2|", FakeBatchModel.prompts[1])
        self.assertNotIn("S9|Topic 1|", FakeBatchModel.prompts[1])
        repaired_question = questions[-1]
        self.assertEqual(repaired_question["topic_id"], slots[-1]["topic_id"])
        self.assertEqual(repaired_question["concept_id"], slots[-1]["concept_id"])
        self.assertEqual(repaired_question["concept_plan_id"], slots[-1]["concept_plan_id"])
        self.assertEqual(repaired_question["source_chunk_ids"], slots[-1]["source_chunk_ids"])
        self.assertGreaterEqual(validation["rejected"], 1)


if __name__ == "__main__":
    unittest.main()
