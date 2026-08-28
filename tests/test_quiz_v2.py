import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.main import QuizGenerateRequest, QuizRegenerateRequest
from backend import quiz_store
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
            patch("backend.quiz_service.get_cached_concept_plan", return_value=None),
            patch("backend.quiz_service.save_cached_concept_plan"),
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

    def test_easy_quality_rejects_negative_and_warns_for_partial_set_answer(self):
        with self.assertRaisesRegex(ValueError, "negative or trick"):
            self.validate_easy_candidate(
                "Which scheduler behavior is NOT used by TinyOS?",
                ["Runs queued tasks", "Uses FIFO order", "Avoids task preemption", "Runs one task"], 1,
                "TinyOS uses FIFO order for queued tasks.",
                "TinyOS runs queued tasks in FIFO order without task preemption.",
            )
        _question, warnings = self.validate_easy_candidate(
            "What are the four objectives of the scheduler?",
            ["Low latency", "High throughput", "Fair execution", "Small memory use"], 0,
            "The objectives include low latency, throughput, fairness, and small memory use.",
            "The four objectives are low latency, high throughput, fair execution, and small memory use.",
        )
        self.assertIn("partial_multi_part_answer", warnings)

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
            {"questions": [raw_question(0)]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual(validation["hard_rejections"], 1)
        self.assertEqual(validation["quality_warnings"], 0)
        self.assertEqual(timings["repair_llm_calls"], 1)
        self.assertEqual(timings["repair_attempt_count"], 1)
        self.assertEqual(timings["fill_attempt_count"], 0)
        self.assertEqual(len(FakeBatchModel.payloads), 1)
        self.assertIn("EASY QUALITY:", FakeBatchModel.prompts[1])

    def test_easy_quality_warnings_do_not_trigger_repair(self):
        initial = [raw_question(index) for index in range(10)]
        initial[0]["options"][1] = "Linux always uses round-robin scheduling"
        questions, validation, timings, _slots = self.run_document_batch([{"questions": initial}])
        self.assertEqual(len(questions), 10)
        self.assertGreaterEqual(validation["quality_warnings"], 2)
        self.assertEqual(validation["hard_rejections"], 0)
        self.assertEqual(timings["repair_llm_calls"], 0)

    def test_twenty_easy_questions_with_several_warnings_need_no_repair(self):
        initial = [raw_question(index) for index in range(20)]
        for index in (1, 7, 13, 19):
            initial[index]["options"][1] = f"Linux always uses round-robin scheduling {index}"
        questions, validation, timings, _slots = self.run_document_batch([
            {"questions": initial[:10]}, {"questions": initial[10:]},
        ], 20)
        self.assertEqual(len(questions), 20)
        self.assertGreaterEqual(validation["quality_warnings"], 8)
        self.assertEqual(validation["hard_rejections"], 0)
        self.assertEqual(timings["repair_llm_calls"], 0)

    def test_twenty_five_easy_questions_with_warnings_return_exact_count(self):
        initial = [raw_question(index) for index in range(25)]
        for index in (2, 8, 14, 20, 24):
            initial[index]["options"][1] = f"Linux only uses round-robin scheduling {index}"
        questions, validation, timings, _slots = self.run_document_batch([
            {"questions": initial[:10]}, {"questions": initial[10:20]}, {"questions": initial[20:]},
        ], 25)
        self.assertEqual(len(questions), 25)
        self.assertGreater(validation["quality_warnings"], 0)
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

    def test_malformed_item_is_completed_by_targeted_repair(self):
        initial = [raw_question(index) for index in range(10)]
        initial[4]["question"] = "Incomplete and?"
        result, saved = self.run_v2([
            {"questions": initial}, {"questions": [raw_question(4)]},
        ])
        self.assertEqual(len(result["questions"]), 10)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["repair_llm_calls"], 1)
        self.assertEqual(result["assessment_plan"]["timings_ms"]["final_fill_llm_calls"], 0)
        self.assertEqual(len(saved), 1)

    def test_final_fill_completes_slots_left_after_targeted_repair(self):
        initial = [raw_question(index) for index in range(9)]
        malformed_repair_1 = raw_question(9)
        malformed_repair_1["question"] = "Still incomplete and?"
        malformed_repair_2 = raw_question(9)
        malformed_repair_2["question"] = "Still incomplete or?"
        result, saved = self.run_v2([
            {"questions": initial}, {"questions": [malformed_repair_1]},
            {"questions": [malformed_repair_2]},
            {"questions": [raw_question(9)]},
        ])
        self.assertEqual(len(result["questions"]), 10)
        timings = result["assessment_plan"]["timings_ms"]
        self.assertEqual(timings["repair_llm_calls"], 3)
        self.assertEqual(timings["repair_attempt_count"], 2)
        self.assertEqual(timings["fill_attempt_count"], 1)
        self.assertEqual(timings["final_fill_llm_calls"], 1)
        self.assertEqual(len(saved), 1)

    def test_repair_failure_reports_counts_and_does_not_persist(self):
        FakeBatchModel.payloads = [
            {"questions": [raw_question(index) for index in range(8)]},
            {"questions": []}, {"questions": []},
            {"questions": []}, {"questions": []},
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
        self.assertEqual(len(FakeBatchModel.configurations), 5)
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

    def test_document_supported_counts_use_bounded_initial_batches(self):
        for count in (10, 15, 20, 25):
            with self.subTest(question_count=count):
                FakeBatchModel.models = []
                FakeBatchModel.configurations = []
                FakeBatchModel.prompts = []
                payloads = [
                    {"questions": [raw_question(index) for index in range(start, min(start + 10, count))]}
                    for start in range(0, count, 10)
                ]
                questions, validation, timings, _slots = self.run_document_batch(
                    payloads, count
                )
                expected_sizes = [min(10, count - start) for start in range(0, count, 10)]
                self.assertEqual(len(questions), count)
                self.assertEqual([question["id"] for question in questions], list(range(1, count + 1)))
                self.assertEqual(validation["accepted"], count)
                self.assertEqual(timings["llm_calls"], len(expected_sizes))
                self.assertEqual(timings["repair_llm_calls"], 0)
                self.assertEqual(timings["repair_attempt_count"], 0)
                self.assertEqual(timings["fill_attempt_count"], 0)
                self.assertEqual(timings["initial_batch_count"], len(expected_sizes))
                self.assertEqual(
                    [batch["requested"] for batch in timings["initial_batches"]], expected_sizes
                )
                self.assertEqual(timings["prompt_tokens"], 500 * len(expected_sizes))
                self.assertEqual(timings["output_tokens"], 300 * len(expected_sizes))
                self.assertEqual(
                    [configuration["num_predict"] for configuration in FakeBatchModel.configurations],
                    [max(520, size * 150) for size in expected_sizes],
                )
                self.assertIn("Topic 1", FakeBatchModel.prompts[0])

    def test_partial_document_batch_repairs_only_missing_slot_and_owns_metadata(self):
        first_batch = [raw_question(index) for index in range(10)]
        second_batch = [raw_question(index) for index in range(10, 14)]
        duplicate = raw_question(10)
        duplicate["slot_id"] = "S15"
        duplicate["topic_id"] = "invented-topic"
        repaired = raw_question(14)
        questions, validation, timings, slots = self.run_document_batch([
            {"questions": first_batch}, {"questions": second_batch + [duplicate]},
            {"questions": [repaired]},
        ], 15)
        self.assertEqual(len(questions), 15)
        self.assertEqual([question["id"] for question in questions], list(range(1, 16)))
        self.assertEqual(len({question["question"] for question in questions}), 15)
        self.assertEqual(timings["llm_calls"], 3)
        self.assertEqual(timings["repair_llm_calls"], 1)
        self.assertEqual(timings["final_fill_llm_calls"], 0)
        self.assertGreaterEqual(timings["repair_ms"], 0)
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[2])
        self.assertIn("S15|Topic 3|", FakeBatchModel.prompts[2])
        self.assertNotIn("S14|Topic 2|", FakeBatchModel.prompts[2])
        repaired_question = questions[-1]
        self.assertEqual(repaired_question["topic_id"], slots[-1]["topic_id"])
        self.assertEqual(repaired_question["concept_id"], slots[-1]["concept_id"])
        self.assertEqual(repaired_question["concept_plan_id"], slots[-1]["concept_plan_id"])
        self.assertEqual(repaired_question["source_chunk_ids"], slots[-1]["source_chunk_ids"])
        self.assertGreaterEqual(validation["rejected"], 1)

    def test_document_final_fill_runs_only_after_targeted_repair(self):
        initial = [raw_question(index) for index in range(9)]
        malformed_repair_1 = raw_question(9)
        malformed_repair_1["question"] = "Still incomplete and?"
        malformed_repair_2 = raw_question(9)
        malformed_repair_2["question"] = "Still incomplete or?"
        questions, validation, timings, _slots = self.run_document_batch([
            {"questions": initial}, {"questions": [malformed_repair_1]},
            {"questions": [malformed_repair_2]},
            {"questions": [raw_question(9)]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual([question["id"] for question in questions], list(range(1, 11)))
        self.assertEqual(timings["llm_calls"], 4)
        self.assertEqual(timings["repair_llm_calls"], 3)
        self.assertEqual(timings["repair_attempt_count"], 2)
        self.assertEqual(timings["fill_attempt_count"], 1)
        self.assertEqual(timings["final_fill_llm_calls"], 1)
        self.assertGreaterEqual(validation["hard_rejections"], 1)
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[1])
        self.assertIn("Write exactly 1", FakeBatchModel.prompts[2])

    def test_second_targeted_repair_completes_partial_first_repair(self):
        initial = [raw_question(index) for index in range(8)]
        questions, _validation, timings, _slots = self.run_document_batch([
            {"questions": initial}, {"questions": [raw_question(8)]},
            {"questions": [raw_question(9)]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual(timings["repair_attempt_count"], 2)
        self.assertEqual(timings["fill_attempt_count"], 0)
        self.assertEqual(timings["llm_calls"], 3)
        self.assertEqual(
            [retry["missing_slots"] for retry in timings["missing_slots_before_each_retry"]],
            [["S9", "S10"], ["S10"]],
        )

    def test_fill_can_use_two_attempts_and_stops_at_exact_count(self):
        initial = [raw_question(index) for index in range(9)]
        questions, _validation, timings, _slots = self.run_document_batch([
            {"questions": initial}, {"questions": []}, {"questions": []},
            {"questions": []}, {"questions": [raw_question(9)]},
        ])
        self.assertEqual(len(questions), 10)
        self.assertEqual(timings["repair_attempt_count"], 2)
        self.assertEqual(timings["fill_attempt_count"], 2)
        self.assertEqual(timings["llm_calls"], 5)
        self.assertEqual(len(FakeBatchModel.payloads), 0)

    def test_document_retry_calls_never_exceed_two_repairs_and_two_fills(self):
        initial = [raw_question(index) for index in range(8)]
        questions, _validation, timings, _slots = self.run_document_batch([
            {"questions": initial}, {"questions": []}, {"questions": []},
            {"questions": []}, {"questions": []},
            {"questions": [raw_question(8), raw_question(9)]},
        ])
        self.assertEqual(len(questions), 8)
        self.assertEqual(timings["repair_attempt_count"], 2)
        self.assertEqual(timings["fill_attempt_count"], 2)
        self.assertEqual(timings["llm_calls"], 5)
        self.assertEqual(len(FakeBatchModel.payloads), 1)

    def test_chunked_document_questions_persist_once_without_duplicates(self):
        questions, _validation, _timings, _slots = self.run_document_batch([
            {"questions": [raw_question(index) for index in range(10)]},
            {"questions": [raw_question(index) for index in range(10, 15)]},
        ], 15)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            with (
                patch.object(quiz_store, "DATABASE_PATH", temp_path / "quiz.db"),
                patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", temp_path / "missing-quizzes.json"),
                patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", temp_path / "missing-attempts.json"),
                patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", temp_path / "missing-explanations.json"),
            ):
                quiz_store.initialize_quiz_store()
                quiz_store.save_quiz("lecture.pdf", "easy", {
                    "quiz_id": "chunked-document-quiz", "document_id": "lecture.pdf",
                    "document_hash": "hash", "title": "Lecture", "difficulty": "easy",
                    "topic_id": "document", "topic_name": "Entire document",
                    "assessment_scope": "document", "topic_schema_version": 2,
                    "assessment_plan": {"planner_version": "hierarchy_concepts_v2"},
                    "questions": questions,
                }, "owner")
                restored = quiz_store.get_quiz_by_id("chunked-document-quiz", "owner")
        self.assertIsNotNone(restored)
        self.assertEqual(len(restored["questions"]), 15)
        self.assertEqual(len({question["id"] for question in restored["questions"]}), 15)


if __name__ == "__main__":
    unittest.main()
