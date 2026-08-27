import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from backend.assessment_planner import (
    PLANNER_VERSION,
    allocate_document_topics,
    build_structural_seeds,
    build_topic_plan,
    validate_and_deduplicate_concepts,
)
from backend.mastery_service import recompute_topic_mastery
from backend.quiz_service import _select_v2_evidence_groups, submit_quiz_attempt


TOPIC = {
    "topic_id": "topic_alpha",
    "name": "Alpha",
    "subtopics": [
        {"subtopic_id": "sub_a", "name": "Broad Systems"},
        {"subtopic_id": "sub_b", "name": "Small Details"},
    ],
}


def chunk(chunk_id, subtopic_id, content, topic_id="topic_alpha"):
    return {"content": content, "metadata": {
        "chunk_id": chunk_id, "topic_id": topic_id, "subtopic_id": subtopic_id,
        "subtopic_name": subtopic_id, "heading_path": "[]", "structure_confidence": 0.9,
    }}


CHUNKS = [
    chunk("c1", "sub_a", "Scheduling selects work. Priorities affect selection."),
    chunk("c2", "sub_a", "Synchronization protects shared state."),
    chunk("c3", "sub_b", "Small details support configuration."),
    chunk("c4", "", "Topic introduction provides context."),
]


def question(quiz_id, plan_id, concept_id="aconcept_one"):
    return {
        "quiz_id": quiz_id, "document_id": "doc.pdf", "title": "Doc", "difficulty": "easy",
        "topic_id": "topic_alpha", "topic_name": "Alpha", "assessment_scope": "topic",
        "assessment_plan": {"planner_version": PLANNER_VERSION, "concept_plan_id": plan_id},
        "questions": [{
            "id": 1, "question": "Which behavior does the evidence support?",
            "options": ["A. Scheduling", "B. Nothing", "C. Removal", "D. Failure"],
            "correct_answer": "A", "difficulty": "easy", "topic_id": "topic_alpha",
            "topic_name": "Alpha", "concept_id": concept_id, "concept_name": "Scheduling",
            "source_subtopic_ids": ["sub_a"], "concept_origin": "derived",
            "concept_plan_id": plan_id, "assessment_capacity": 1,
            "explanation": "The evidence supports scheduling.", "source_chunk_ids": ["c1"],
        }],
    }


class TopicV2Phase2Tests(unittest.TestCase):
    def test_structural_seeds_preserve_order_and_reject_foreign_provenance(self):
        seeds = build_structural_seeds(TOPIC, CHUNKS)
        self.assertEqual([seed["subtopic_id"] for seed in seeds], ["sub_a", "sub_b", ""])
        with self.assertRaisesRegex(ValueError, "outside selected topic"):
            build_structural_seeds(TOPIC, CHUNKS + [chunk("bad", "sub_a", "bad", "other")])
        with self.assertRaisesRegex(ValueError, "invalid subtopic"):
            build_structural_seeds(TOPIC, CHUNKS + [chunk("bad", "invented", "bad")])

    def test_split_merge_structural_and_derived_concepts_have_stable_ids(self):
        raw = [
            {"name": "Scheduling", "source_subtopic_ids": ["sub_a"], "source_chunk_ids": ["c1"]},
            {"name": "Synchronization", "source_subtopic_ids": ["sub_a"], "source_chunk_ids": ["c2"]},
            {"name": "Configuration overview", "source_subtopic_ids": ["sub_a", "sub_b"], "source_chunk_ids": ["c1", "c3"]},
            {"name": "Topic context", "source_subtopic_ids": [], "source_chunk_ids": ["c4"]},
        ]
        first = validate_and_deduplicate_concepts(raw, CHUNKS, TOPIC)
        second = validate_and_deduplicate_concepts(raw, CHUNKS, TOPIC)
        self.assertEqual([item["concept_id"] for item in first], [item["concept_id"] for item in second])
        self.assertTrue(all(item["concept_id"].startswith("aconcept_") for item in first))
        origins = {item["name"]: item["concept_origin"] for item in first}
        self.assertEqual(origins, {
            "Scheduling": "derived", "Synchronization": "derived",
            "Configuration overview": "refined", "Topic context": "derived",
        })

    def test_structural_origin_and_invented_lineage_rejection(self):
        concepts = validate_and_deduplicate_concepts([
            {"name": "Broad Systems", "source_subtopic_ids": ["sub_a"], "source_chunk_ids": ["c1", "c2"]},
            {"name": "Invented", "source_subtopic_ids": ["missing"], "source_chunk_ids": ["c1"]},
            {"name": "Wrong lineage", "source_subtopic_ids": ["sub_b"], "source_chunk_ids": ["c1"]},
            {"name": "Missing evidence", "source_subtopic_ids": ["sub_a"], "source_chunk_ids": []},
        ], CHUNKS, TOPIC)
        self.assertEqual(len(concepts), 1)
        self.assertEqual(concepts[0]["concept_origin"], "structural")

    def test_stable_plan_id_and_topic_v2_groups_use_shared_plan(self):
        planned = [
            {"name": "Scheduling", "source_subtopic_ids": ["sub_a"], "source_chunk_ids": ["c1"]},
            {"name": "Topic context", "source_subtopic_ids": [], "source_chunk_ids": ["c4"]},
        ]
        with patch("backend.assessment_planner._plan_seeds", return_value=planned):
            first = build_topic_plan(TOPIC, CHUNKS)
            second = build_topic_plan(TOPIC, CHUNKS)
        self.assertEqual(first["concept_plan_id"], second["concept_plan_id"])
        self.assertTrue(first["concept_plan_id"].startswith("conceptplan_"))
        groups = _select_v2_evidence_groups(TOPIC, CHUNKS, 5, first)
        self.assertEqual([group["concept_id"] for group in groups], [item["concept_id"] for item in first["concepts"]])
        self.assertEqual(groups[0]["source_subtopic_ids"], ["sub_a"])

    def test_document_requested_count_caps_topic_first_allocation(self):
        plans = []
        for name, capacity in (("a", 5), ("b", 4), ("c", 3), ("d", 2)):
            plans.append({
                "topic_id": name, "assessment_capacity": capacity,
                "concepts": [{"concept_id": f"{name}{index}"} for index in range(capacity)],
            })
        allocated = allocate_document_topics(plans, cap=10)
        counts = {plan["topic_id"]: plan["allocated_questions"] for plan in allocated["topics"]}
        self.assertEqual(sum(counts.values()), 10)
        self.assertTrue(all(count >= 1 for count in counts.values()))

    def test_document_generation_uses_requested_ten_question_cap(self):
        topics = [{"topic_id": name, "name": name, "subtopics": []} for name in ("a", "b", "c", "d")]
        document = {"id": "doc.pdf", "title": "Doc", "hash": "hash", "topic_schema_version": 3, "topics": topics}
        def chunks(_document_id, topic_id, _owner):
            return [chunk(f"{topic_id}-chunk", "", f"Evidence for {topic_id}", topic_id)]
        def plan(topic, _chunks):
            concepts = [{
                "concept_id": f"aconcept_{topic['topic_id']}_{index}", "name": f"Concept {index}",
                "source_subtopic_ids": [], "source_chunk_ids": [f"{topic['topic_id']}-chunk"],
                "concept_origin": "derived",
            } for index in range(4)]
            return {"topic_id": topic["topic_id"], "topic_name": topic["name"], "planner_version": PLANNER_VERSION,
                    "concept_plan_id": f"plan-{topic['topic_id']}", "assessment_capacity": 4,
                    "allocated_questions": 0, "concepts": concepts}
        def generated(**kwargs):
            return [{"id": kwargs["start_id"], "question": "Which supported concept applies?",
                     "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                     "topic_id": kwargs["topic_id"], "topic_name": kwargs["topic_name"],
                     "concept_id": kwargs["concept_id"], "concept_name": kwargs["concept_name"],
                     "assessment_capacity": kwargs["assessment_capacity"], "difficulty": "easy",
                     "explanation": "The evidence supports this.",
                     "source_chunk_ids": [kwargs["chunks"][0]["metadata"]["chunk_id"]]}]
        for requested in (10, 15, 20, 25):
            with self.subTest(question_count=requested), \
                 patch.object(quiz_service, "_document_lookup", return_value={"doc.pdf": document}), \
                 patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
                 patch.object(quiz_service, "get_quiz", return_value=None), \
                 patch.object(quiz_service, "get_topic_chunks", side_effect=chunks), \
                 patch.object(quiz_service, "build_topic_plan", side_effect=plan), \
                 patch.object(quiz_service, "_generate_quiz_batch", side_effect=generated), \
                 patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, value, _o: value):
                result = quiz_service.generate_quiz("doc.pdf", "easy", "document", question_count=requested)
            self.assertEqual(result["question_count"], requested)
            self.assertEqual(result["assessment_plan"]["target_questions"], requested)
            represented = {question["topic_id"] for question in result["questions"]}
            self.assertEqual(represented, {"a", "b", "c", "d"})
            if requested > 16:
                self.assertLess(len({question["concept_id"] for question in result["questions"]}), requested)

    def test_document_partial_generation_fails_without_persistence(self):
        topic = {"topic_id": "a", "name": "A", "subtopics": []}
        document = {"id": "doc.pdf", "title": "Doc", "hash": "hash", "topic_schema_version": 3, "topics": [topic]}
        evidence = chunk("a-chunk", "", "Grounded evidence for A", "a")
        concept = {
            "concept_id": "aconcept_a", "name": "Concept A", "source_subtopic_ids": [],
            "source_chunk_ids": ["a-chunk"], "concept_origin": "derived",
        }
        plan = {
            "topic_id": "a", "topic_name": "A", "planner_version": PLANNER_VERSION,
            "concept_plan_id": "plan-a", "assessment_capacity": 1,
            "allocated_questions": 0, "concepts": [concept],
        }
        calls = 0

        def generated(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 10:
                raise ValueError("bounded repair exhausted")
            return [{
                "id": kwargs["start_id"], "question": f"Grounded question {calls}?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "topic_id": "a", "topic_name": "A", "concept_id": "aconcept_a",
                "concept_name": "Concept A", "assessment_capacity": 1, "difficulty": "easy",
                "explanation": "The evidence supports this.", "source_chunk_ids": ["a-chunk"],
            }]

        with patch.object(quiz_service, "_document_lookup", return_value={"doc.pdf": document}), \
             patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"), \
             patch.object(quiz_service, "get_quiz", return_value=None), \
             patch.object(quiz_service, "get_topic_chunks", return_value=[evidence]), \
             patch.object(quiz_service, "build_topic_plan", return_value=plan), \
             patch.object(quiz_service, "resolve_concept_evidence", return_value=[evidence]), \
             patch.object(quiz_service, "_generate_quiz_batch", side_effect=generated), \
             patch.object(quiz_service, "save_quiz") as save:
            with self.assertRaises(quiz_service.QuizGenerationError) as raised:
                quiz_service.generate_quiz("doc.pdf", "easy", "document", question_count=10)
        self.assertEqual(raised.exception.detail["requested_count"], 10)
        self.assertEqual(raised.exception.detail["valid_count"], 9)
        self.assertEqual(raised.exception.detail["missing_count"], 1)
        self.assertTrue(raised.exception.detail["failure_summary"])
        save.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "phase2.db")
        self.db_patch.start()
        self.legacy = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp.name) / "none1"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp.name) / "none2"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp.name) / "none3"),
        ]
        for item in self.legacy: item.start()

    def tearDown(self):
        for item in reversed(self.legacy): item.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def test_lineage_round_trip_retake_and_incompatible_plan_isolation(self):
        quiz_store.save_quiz("doc.pdf", "easy", question("quiz-old", "plan-old"))
        submit_quiz_attempt("doc.pdf", "easy", "topic_alpha", {"1": "A"}, quiz_id="quiz-old")
        quiz_store.save_quiz("doc.pdf", "easy", question("quiz-new", "plan-new", "aconcept_new"))
        first = submit_quiz_attempt("doc.pdf", "easy", "topic_alpha", {"1": "B"}, quiz_id="quiz-new")
        second = submit_quiz_attempt("doc.pdf", "easy", "topic_alpha", {"1": "A"}, quiz_id="quiz-new")
        self.assertEqual(second["question_results"][0]["source_subtopic_ids"], ["sub_a"])
        self.assertEqual(second["question_results"][0]["concept_origin"], "derived")
        self.assertEqual(second["question_results"][0]["concept_plan_id"], "plan-new")
        mastery = recompute_topic_mastery("local_student", "doc.pdf", "topic_alpha")
        self.assertEqual(mastery["answered_questions"], 1)
        self.assertEqual(mastery["distinct_concepts_assessed"], 1)
        self.assertEqual(mastery["mastery_score"], 100.0)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    def test_legacy_answers_without_plan_remain_readable(self):
        legacy = question("legacy-quiz", "")
        legacy["questions"][0].pop("concept_plan_id")
        legacy["questions"][0].pop("source_subtopic_ids")
        legacy["questions"][0].pop("concept_origin")
        quiz_store.save_quiz("doc.pdf", "easy", legacy)
        saved = submit_quiz_attempt("doc.pdf", "easy", "topic_alpha", {"1": "A"}, quiz_id="legacy-quiz")
        self.assertEqual(saved["question_results"][0]["concept_plan_id"], "")
        self.assertEqual(recompute_topic_mastery("local_student", "doc.pdf", "topic_alpha")["answered_questions"], 1)


if __name__ == "__main__":
    unittest.main()
