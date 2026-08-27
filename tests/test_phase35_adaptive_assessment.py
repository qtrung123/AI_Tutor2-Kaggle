import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_store
from backend.assessment_planner import allocate_document_topics, validate_and_deduplicate_concepts
from backend.main import QuizGenerateRequest, QuizRegenerateRequest
from backend.mastery_service import calculate_mastery, recompute_topic_mastery
from backend.quiz_service import update_quiz_progress
from backend import quiz_service


def concept_plan(topic_id: str, capacity: int) -> dict:
    concepts = [
        {"concept_id": f"concept_{index:03d}", "name": f"Concept {index}", "source_chunk_ids": [f"c{index}"]}
        for index in range(1, capacity + 1)
    ]
    return {
        "topic_id": topic_id,
        "topic_name": topic_id,
        "assessment_capacity": capacity,
        "allocated_questions": 0,
        "concepts": concepts,
    }


def answer(question_id: int, concept_id: str, capacity: int, correct: bool = True) -> dict:
    return {
        "question_id": question_id,
        "selected_answer": "A" if correct else "B",
        "correct_answer": "A",
        "is_correct": correct,
        "question_difficulty": "easy",
        "validation_outcome": "accepted",
        "topic_id": "topic_001",
        "topic_name": "Topic",
        "concept_id": concept_id,
        "assessment_capacity": capacity,
        "evidence_requirement_version": "concept_coverage_v1",
    }


class AdaptiveAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "phase35.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def test_document_mode_covers_all_assessable_topics_within_cap(self):
        result = allocate_document_topics([
            concept_plan("topic_a", 1), concept_plan("topic_b", 2), concept_plan("topic_c", 3)
        ], cap=6)
        allocations = {plan["topic_id"]: plan["allocated_questions"] for plan in result["topics"]}
        self.assertTrue(all(allocations[topic] >= 1 for topic in allocations))
        self.assertEqual(result["excluded_topic_ids"], [])

    def test_richer_topics_get_extras_only_after_broad_coverage(self):
        result = allocate_document_topics([
            concept_plan("topic_a", 1), concept_plan("topic_b", 4), concept_plan("topic_c", 2)
        ], cap=4)
        allocations = {plan["topic_id"]: plan["allocated_questions"] for plan in result["topics"]}
        self.assertEqual(allocations, {"topic_a": 1, "topic_b": 2, "topic_c": 1})

    def test_cap_smaller_than_topics_records_deterministic_exclusions(self):
        result = allocate_document_topics([
            concept_plan("topic_c", 1), concept_plan("topic_a", 3), concept_plan("topic_b", 2)
        ], cap=2)
        self.assertEqual(result["excluded_topic_ids"], ["topic_c"])
        self.assertEqual(result["total_questions"], 2)

    def test_planner_rejects_invalid_evidence_and_deduplicates_concepts(self):
        chunks = [{"content": "Reliable delivery", "metadata": {"chunk_id": "hash_1"}}]
        concepts = validate_and_deduplicate_concepts([
            {"name": "Reliable delivery", "source_chunk_ids": ["hash_1"]},
            {"name": "Reliable delivery!", "source_chunk_ids": ["hash_1"]},
            {"name": "Unsupported detail", "source_chunk_ids": ["other"]},
        ], chunks)
        self.assertEqual(len(concepts), 1)
        self.assertEqual(concepts[0]["source_chunk_ids"], ["hash_1"])

    def test_repeated_questions_do_not_increase_concept_coverage(self):
        result = calculate_mastery([answer(1, "concept_001", 5), answer(2, "concept_001", 5)])
        self.assertEqual(result["answered_questions"], 2)
        self.assertEqual(result["distinct_concepts_assessed"], 1)
        self.assertEqual(result["concept_coverage_ratio"], 0.2)
        self.assertFalse(result["has_sufficient_evidence"])

    def test_evidence_sufficiency_scales_with_capacity(self):
        short = calculate_mastery([answer(1, "concept_001", 1)])
        large = calculate_mastery([answer(1, "concept_001", 10), answer(2, "concept_002", 10)])
        self.assertTrue(short["has_sufficient_evidence"])
        self.assertEqual(short["required_concepts"], 1)
        self.assertFalse(large["has_sufficient_evidence"])
        self.assertEqual(large["required_concepts"], 6)

    def test_two_concept_topic_can_become_sufficiently_assessed(self):
        result = calculate_mastery([answer(1, "concept_001", 2), answer(2, "concept_002", 2)])
        self.assertTrue(result["has_sufficient_evidence"])
        self.assertEqual(result["distinct_concepts_assessed"], 2)

    def test_assessment_plan_and_question_concepts_are_persisted(self):
        quiz = {
            "quiz_id": "adaptive-quiz", "document_id": "doc.pdf", "title": "Doc",
            "difficulty": "easy", "topic_id": "document", "topic_name": "Entire document",
            "assessment_scope": "document", "topic_schema_version": 2,
            "assessment_plan": {"planner_version": "assessment_capacity_v1", "topics": [], "excluded_topic_ids": []},
            "questions": [{
                "id": 1, "question": "What does this concept provide?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "topic_id": "topic_001", "topic_name": "Topic", "concept_id": "concept_001",
                "concept_name": "Concept", "assessment_capacity": 1, "difficulty": "easy",
                "explanation": "Supported.", "source_chunk_ids": ["hash_1"], "validation_outcome": "accepted",
            }],
        }
        quiz_store.save_quiz("doc.pdf", "easy", quiz)
        loaded = quiz_store.get_quiz("doc.pdf", "easy", "document")
        self.assertEqual(loaded["assessment_scope"], "document")
        self.assertEqual(loaded["assessment_plan"]["planner_version"], "assessment_capacity_v1")
        self.assertEqual(loaded["questions"][0]["concept_id"], "concept_001")

    def test_document_attempt_recomputes_each_represented_topic(self):
        questions = []
        for index, topic_id in enumerate(("topic_a", "topic_b"), start=1):
            questions.append({
                "id": index, "question": f"Meaningful question {index} about the topic?",
                "options": ["A. Correct", "B. Wrong", "C. Other", "D. Another"],
                "correct_answer": "A", "topic_id": topic_id, "topic_name": topic_id,
                "concept_id": "concept_001", "concept_name": "Core concept",
                "assessment_capacity": 1, "difficulty": "easy", "explanation": "Supported.",
                "source_chunk_ids": [f"hash_{index}"], "validation_outcome": "accepted",
            })
        quiz_store.save_quiz("doc.pdf", "easy", {
            "quiz_id": "document-quiz", "document_id": "doc.pdf", "title": "Doc",
            "difficulty": "easy", "topic_id": "document", "topic_name": "Entire document",
            "assessment_scope": "document", "assessment_plan": {"planner_version": "assessment_capacity_v1"},
            "questions": questions,
        }, "student")
        update_quiz_progress("doc.pdf", "easy", "document", 1, "A", "student")
        completed = update_quiz_progress("doc.pdf", "easy", "document", 2, "A", "student")
        self.assertEqual(set(completed["mastery_by_topic"]), {"topic_a", "topic_b"})
        self.assertTrue(completed["mastery_by_topic"]["topic_a"]["has_sufficient_evidence"])
        self.assertTrue(completed["mastery_by_topic"]["topic_b"]["has_sufficient_evidence"])

    def test_document_generation_plans_and_generates_topic_by_topic(self):
        document = {
            "id": "doc.pdf", "title": "Doc", "hash": "hash", "topic_schema_version": 2,
            "topics": [{"topic_id": "topic_a", "name": "A"}, {"topic_id": "topic_b", "name": "B"}],
        }

        def planned(topic, chunks):
            capacity = 1 if topic["topic_id"] == "topic_a" else 2
            plan = concept_plan(topic["topic_id"], capacity) | {"topic_name": topic["name"]}
            plan["concept_plan_id"] = f"plan-{topic['topic_id']}"
            for concept in plan["concepts"]:
                concept["source_chunk_ids"] = [f"{topic['topic_id']}_1"]
            return plan

        def generated(_document, difficulty, slots, _owner, _model, requested, _run_id):
            questions = [{
                "id": index + 1, "question": f"Question for {slot['name']} case {index + 1}?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "topic_id": slot["topic_id"], "topic_name": slot["topic_name"],
                "concept_id": slot["concept_id"], "concept_name": slot["name"],
                "concept_plan_id": slot["concept_plan_id"],
                "assessment_capacity": slot["assessment_capacity"], "difficulty": difficulty,
                "explanation": "Supported.", "source_chunk_ids": slot["source_chunk_ids"],
                "validation_outcome": "accepted",
            } for index, slot in enumerate(slots)]
            return questions, {"accepted": requested, "accepted_with_warnings": 0, "rejected": 0, "reasons": []}, {"llm_calls": 1}

        def chunks(document_id, topic_id, owner_id):
            return [{"content": topic_id, "metadata": {"chunk_id": f"{topic_id}_1", "topic_id": topic_id}}]

        with patch.object(quiz_service, "_document_lookup", return_value={"doc.pdf": document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "get_topic_chunks", side_effect=chunks), \
             patch.object(quiz_service, "build_topic_plan", side_effect=planned), \
             patch.object(quiz_service, "_run_document_v2_batch", side_effect=generated) as generator, \
             patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _owner: quiz):
            result = quiz_service.generate_quiz("doc.pdf", "easy", "document")

        self.assertEqual(result["question_count"], 10)
        self.assertEqual(generator.call_count, 1)
        self.assertEqual({question["topic_id"] for question in result["questions"]}, {"topic_a", "topic_b"})
        self.assertTrue(all(question["concept_id"] for question in result["questions"]))

    def test_request_models_and_frontend_support_allowed_question_counts(self):
        self.assertIn("question_count", QuizGenerateRequest.model_fields)
        self.assertIn("question_count", QuizRegenerateRequest.model_fields)
        self.assertEqual(QuizGenerateRequest(
            document_id="doc.pdf", assessment_scope="topic", topic_id="topic_a", difficulty="easy"
        ).question_count, 10)
        frontend = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("quizQuestionCountSelect", frontend)
        self.assertIn("question_count:", frontend)
        self.assertIn('quiz-question-count-select', frontend)
        self.assertIn('quizCreateDialog?.classList.contains("open")', frontend)
        self.assertIn("!currentQuiz?.questions?.length && !createDialogOpen", frontend)

    def test_topic_generation_uses_v2_batch_pipeline(self):
        document = {
            "id": "doc.pdf", "title": "Doc", "hash": "hash", "topic_schema_version": 2,
            "topics": [{"topic_id": "topic_a", "name": "A"}],
        }
        with patch.object(quiz_service, "_document_lookup", return_value={"doc.pdf": document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "_generate_topic_quiz_v2", return_value={
                 "question_count": 10, "questions": [{"concept_id": f"concept_{index:03d}"} for index in range(1, 11)]
             }) as generator:
            result = quiz_service.generate_quiz("doc.pdf", "easy", "topic", "topic_a")

        self.assertEqual(result["question_count"], 10)
        generator.assert_called_once()
        self.assertEqual(generator.call_args.kwargs["model_id"], quiz_service.CHAT_MODEL)

    def test_dashboard_empty_state_contains_no_invented_metrics(self):
        with patch.object(quiz_service, "list_indexed_documents", return_value=[]):
            dashboard = quiz_service.build_learning_dashboard("new-student")
        self.assertEqual(dashboard["metrics"]["documents"], 0)
        self.assertEqual(dashboard["metrics"]["total_topics"], 0)
        self.assertIsNone(dashboard["metrics"]["quiz_accuracy"])
        self.assertEqual(dashboard["mastery"], [])
        self.assertIsNone(dashboard["latest_attempt"])

    def test_dashboard_uses_real_attempts_and_returns_multi_topic_mastery(self):
        document = {
            "id": "dashboard.pdf", "title": "Dashboard PDF", "chunks": 2,
            "topics": [{"topic_id": "topic_a", "name": "Topic A"}, {"topic_id": "topic_b", "name": "Topic B"}],
        }
        results = [
            {**answer(1, "concept_001", 1, True), "topic_id": "topic_a", "topic_name": "Topic A"},
            {**answer(2, "concept_001", 1, False), "topic_id": "topic_b", "topic_name": "Topic B"},
        ]
        quiz_store.save_quiz_progress("dashboard.pdf", "easy", {
            "quiz_id": "dashboard-quiz", "answers": {"1": "A", "2": "B"},
            "question_results": results, "score": 1, "answered": 2, "total": 2, "completed": True,
        }, "document", "dashboard-student")
        with patch.object(quiz_service, "list_indexed_documents", return_value=[document]):
            dashboard = quiz_service.build_learning_dashboard("dashboard-student")
        self.assertEqual(dashboard["metrics"]["documents"], 1)
        self.assertEqual(dashboard["metrics"]["topics_assessed"], 2)
        self.assertEqual(dashboard["metrics"]["quiz_accuracy"], 50.0)
        self.assertEqual({row["topic_name"] for row in dashboard["mastery"]}, {"Topic A", "Topic B"})
        self.assertEqual(set(dashboard["latest_attempt"]["represented_topic_ids"]), {"topic_a", "topic_b"})

    def test_overview_markup_uses_real_dashboard_and_mastery_containers(self):
        root = Path(__file__).parents[1]
        markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        for demo_text in (
            "Course average", "Spring semester", "Calculus II", "Database Systems",
            "Study streak", "Next deadline", "Progress Monitor Alert", "This week",
        ):
            self.assertNotIn(demo_text, markup)
        self.assertIn('id="overview-mastery-list"', markup)
        self.assertIn('id="practice-mastery-list"', markup)
        self.assertIn('id="continue-learning-list"', markup)
        self.assertIn('id="overview-materials-list"', markup)
        self.assertIn('/api/dashboard', script)
        self.assertIn("mastery_by_topic", script)


if __name__ == "__main__":
    unittest.main()
