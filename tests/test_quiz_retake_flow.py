import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.mastery_service import recompute_topic_mastery
from backend.quiz_service import load_quiz_for_retake, submit_quiz_attempt


def saved_quiz():
    return {
        "quiz_id": "same-saved-quiz",
        "document_id": "lecture.pdf",
        "title": "Lecture",
        "difficulty": "easy",
        "topic_id": "topic_1",
        "topic_name": "Topic 1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "questions": [
            {
                "id": index,
                "question": f"Question {index}?",
                "options": ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"],
                "correct_answer": correct,
                "difficulty": "easy",
                "topic_id": "topic_1",
                "topic_name": "Topic 1",
                "concept_id": f"concept_{index}",
                "assessment_capacity": 3,
                "explanation": f"Explanation {index}",
                "source_chunk_ids": [f"chunk_{index}"],
            }
            for index, correct in enumerate(("A", "B", "C"), start=1)
        ],
    }


class QuizRetakeFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "quiz.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.db)
        self.database_patch.start()
        self.legacy_patches = [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", Path(self.temp.name) / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", Path(self.temp.name) / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", Path(self.temp.name) / "missing-explanations.json"),
        ]
        for legacy_patch in self.legacy_patches: legacy_patch.start()
        quiz_store.initialize_quiz_store()
        quiz_store.save_quiz("lecture.pdf", "easy", saved_quiz(), LEGACY_USER_ID)

    def tearDown(self):
        self.database_patch.stop()
        for legacy_patch in reversed(self.legacy_patches): legacy_patch.stop()
        self.temp.cleanup()

    def test_submit_requires_a_complete_answer_set(self):
        with self.assertRaisesRegex(ValueError, "Every quiz question"):
            submit_quiz_attempt(
                "lecture.pdf", "easy", "topic_1", {"1": "A", "3": "C"}, LEGACY_USER_ID
            )
        self.assertEqual(quiz_store.list_quiz_history(student_id=LEGACY_USER_ID), [])

    def test_each_retake_is_new_attempt_with_scores_and_full_snapshots(self):
        first = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "D", "3": "C"}, LEGACY_USER_ID
        )
        second = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID
        )

        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual((first["attempt_number"], first["score"], first["percentage"]), (1, 1, 33.33))
        self.assertEqual((second["attempt_number"], second["score"], second["percentage"]), (2, 3, 100.0))
        self.assertEqual(second["attempt_summary"], {
            "attempts": 2, "latest_score": 100.0, "best_score": 100.0, "average_score": 66.67
        })
        self.assertEqual(second["question_results"][0]["options"], ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"])
        self.assertEqual(second["question_results"][0]["explanation"], "Explanation 1")
        self.assertEqual(second["question_results"][0]["source_chunk_ids"], ["chunk_1"])

    def test_retake_replaces_mastery_evidence_without_inflating_coverage(self):
        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "D", "3": "D"}, LEGACY_USER_ID
        )
        first_mastery = recompute_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_1")
        submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"}, LEGACY_USER_ID
        )
        latest_mastery = recompute_topic_mastery(LEGACY_USER_ID, "lecture.pdf", "topic_1")

        self.assertEqual(first_mastery["answered_questions"], 3)
        self.assertEqual(latest_mastery["answered_questions"], 3)
        self.assertEqual(latest_mastery["distinct_concepts_assessed"], 3)
        self.assertEqual(latest_mastery["concept_coverage_ratio"], 1.0)
        self.assertEqual(latest_mastery["completed_attempts"], 1)
        self.assertEqual(latest_mastery["mastery_score"], 100.0)

    def test_retake_resolves_quiz_id_and_legacy_attempt_snapshot_when_quiz_row_is_missing(self):
        first = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "D", "2": "B", "3": "C"}, LEGACY_USER_ID
        )
        with quiz_store._connect() as connection:
            connection.execute("DELETE FROM quizzes WHERE quiz_id = ?", ("same-saved-quiz",))

        retake = load_quiz_for_retake(first["attempt_id"], LEGACY_USER_ID)
        self.assertEqual(retake["quiz"]["quiz_id"], "same-saved-quiz")
        self.assertEqual([question["question"] for question in retake["quiz"]["questions"]], [
            "Question 1?", "Question 2?", "Question 3?"
        ])
        second = submit_quiz_attempt(
            "lecture.pdf", "easy", "topic_1", {"1": "A", "2": "B", "3": "C"},
            LEGACY_USER_ID, quiz_id="same-saved-quiz",
        )
        self.assertEqual(second["quiz_id"], first["quiz_id"])
        self.assertEqual(second["attempt_number"], 2)

    def test_document_batch_quiz_survives_invalidation_submit_reload_review_and_retake(self):
        quiz = saved_quiz()
        quiz.update({
            "quiz_id": "document-batch-10",
            "document_id": "Embedded Systems.pdf",
            "title": "Embedded Systems",
            "topic_id": "document",
            "topic_name": "Entire document",
            "assessment_scope": "document",
            "topic_schema_version": 2,
            "assessment_plan": {
                "planner_version": "assessment_capacity_v1",
                "scope": "document",
                "target_questions": 10,
                "total_questions": 10,
            },
            "questions": [
                {
                    "id": index,
                    "question": f"Grounded embedded-systems question {index}?",
                    "options": ["A. Alpha", "B. Beta", "C. Gamma", "D. Delta"],
                    "correct_answer": "A",
                    "difficulty": "easy",
                    "topic_id": f"topic_{1 + (index - 1) // 5}",
                    "topic_name": f"Topic {1 + (index - 1) // 5}",
                    "concept_id": f"concept_{index}",
                    "concept_name": f"Concept {index}",
                    "concept_plan_id": f"plan_{1 + (index - 1) // 5}",
                    "source_subtopic_ids": [f"subtopic_{index}"],
                    "concept_origin": "structural",
                    "assessment_capacity": 5,
                    "explanation": f"Explanation {index}",
                    "source_chunk_ids": [f"chunk_{index}"],
                    "validation_outcome": "accepted",
                }
                for index in range(1, 11)
            ],
        })
        quiz["questions"][0]["options"] = [
            "A. Alpha", "b) Beta", "C: Gamma", "d - Delta",
        ]
        document = {
            "id": "Embedded Systems.pdf", "title": "Embedded Systems", "hash": "embedded-hash",
            "topic_schema_version": 2,
            "topics": [{"topic_id": "topic_1", "name": "Topic 1"}, {"topic_id": "topic_2", "name": "Topic 2"}],
        }
        topic_plans = {
            topic_id: {
                "topic_id": topic_id, "topic_name": f"Topic {topic_id[-1]}",
                "planner_version": "assessment_capacity_v1", "concept_plan_id": f"plan_{topic_id[-1]}",
                "assessment_capacity": 5,
                "concepts": [{
                    "concept_id": f"concept_{offset + index}", "name": f"Concept {offset + index}",
                    "source_chunk_ids": [f"chunk_{offset + index}"], "source_subtopic_ids": [f"subtopic_{offset + index}"],
                    "concept_origin": "structural",
                } for index in range(1, 6)],
            }
            for topic_id, offset in (("topic_1", 0), ("topic_2", 5))
        }

        def generated_batch(_document, _difficulty, _slots, _owner, _model, _count, _run_id):
            return quiz["questions"], {
                "accepted": 10, "accepted_with_warnings": 0, "rejected": 0, "reasons": [],
            }, {"llm_calls": 1}

        with patch.object(quiz_service, "_document_lookup", return_value={document["id"]: document}), \
             patch.object(quiz_service, "get_topic_chunks", return_value=[{
                 "content": "Grounded embedded systems evidence", "metadata": {"chunk_id": "chunk_1"},
             }]), \
             patch.object(quiz_service, "build_topic_plan", side_effect=lambda topic, _chunks: topic_plans[topic["topic_id"]]), \
             patch.object(quiz_service, "resolve_concept_evidence", return_value=[{
                 "content": "Grounded embedded systems evidence", "metadata": {"chunk_id": "chunk_1"},
             }]), \
             patch.object(quiz_service, "_run_document_v2_batch", side_effect=generated_batch), \
             patch.object(quiz_service, "uuid4", return_value="document-batch-10"):
            generated = quiz_service.generate_quiz(
                "Embedded Systems.pdf", "easy", "document", owner_id=LEGACY_USER_ID, question_count=10,
            )

        with quiz_store._connect() as connection:
            quiz_row = connection.execute(
                "SELECT quiz_id, question_count FROM quizzes WHERE quiz_id = ?", (generated["quiz_id"],)
            ).fetchone()
            question_rows = connection.execute(
                "SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = ?", (generated["quiz_id"],)
            ).fetchone()[0]
        self.assertEqual((quiz_row["quiz_id"], quiz_row["question_count"]), ("document-batch-10", 10))
        self.assertEqual(question_rows, 10)

        # A topic-map refresh may happen after the slow batch response has
        # rendered but before Check Answers. It must not erase that quiz id.
        self.assertEqual(
            quiz_store.invalidate_document_quizzes_for_topic_schema(
                "Embedded Systems.pdf", 3, LEGACY_USER_ID
            ),
            1,
        )
        self.assertIsNone(quiz_store.get_quiz("Embedded Systems.pdf", "easy", "document", LEGACY_USER_ID))
        persisted = quiz_store.get_quiz_by_id("document-batch-10", LEGACY_USER_ID)
        self.assertEqual(len(persisted["questions"]), 10)
        self.assertEqual(persisted["questions"][0]["options"], [
            "A. Alpha", "B. Beta", "C. Gamma", "D. Delta",
        ])

        answers = {str(index): "A" for index in range(1, 11)}
        completed = submit_quiz_attempt(
            "Embedded Systems.pdf", "easy", "document", answers,
            LEGACY_USER_ID, quiz_id="document-batch-10",
        )
        self.assertEqual(completed["quiz_id"], "document-batch-10")
        self.assertEqual((completed["answered"], completed["total"]), (10, 10))
        self.assertEqual(len(completed["question_results"]), 10)

        review = quiz_store.get_quiz_history_attempt(completed["attempt_id"], LEGACY_USER_ID)
        self.assertEqual(len(review["question_results"]), 10)
        self.assertEqual(review["question_results"][0]["options"], persisted["questions"][0]["options"])
        retake = load_quiz_for_retake(completed["attempt_id"], LEGACY_USER_ID)
        self.assertEqual(retake["quiz"]["quiz_id"], completed["quiz_id"])
        self.assertEqual(len(retake["quiz"]["questions"]), 10)
        self.assertEqual(retake["quiz"]["questions"][0]["options"], persisted["questions"][0]["options"])
        second = submit_quiz_attempt(
            "Embedded Systems.pdf", "easy", "document", answers,
            LEGACY_USER_ID, quiz_id=retake["quiz"]["quiz_id"],
        )
        self.assertEqual(second["attempt_number"], 2)
        self.assertEqual(len(second["question_results"]), 10)

    def test_frontend_uses_deferred_submission_navigation_review_and_separate_regeneration(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('check.textContent = "Check Answers"', script)
        self.assertIn('button.className = "quiz-navigator-button"', script)
        self.assertIn("requestQuizSubmission()", script)
        self.assertIn("currentAttempt = await requestQuizSubmission()", script)
        self.assertIn("createAssessmentReviewCard", script)
        self.assertIn('retake.textContent = "Retake Quiz"', script)
        self.assertIn('review.textContent = "Review Answers"', script)
        self.assertIn('regenerate.textContent = "Regenerate Quiz"', script)
        self.assertIn("startHistoryQuizRetake(attempt)", script)
        self.assertIn('actions.append(retake, close)', script)
        self.assertIn("requestQuizRegeneration", script)
        self.assertIn('backToQuizzesButton.textContent = "← Back to Quizzes"', script)
        self.assertIn('close.textContent = "← Back to Quizzes"', script)
        self.assertIn("const groups = Object.values(quizHistory.reduce", script)
        self.assertIn('historySummary.textContent = `Attempt history (${group.attempts.length})`', script)
        self.assertIn('makeFilter("Difficulty"', script)
        self.assertIn('makeFilter("Scope"', script)
        self.assertIn("group.average", script)
        self.assertNotIn("— Your answer", script)
        self.assertNotIn("— Correct answer", script)
        self.assertNotIn("Source citation unavailable.", script)
        self.assertNotIn("source_chunk_ids.join", script)
        deferred_selector = script[script.rfind("function selectAssessmentAnswer") :]
        self.assertNotIn("requestQuizProgress(", deferred_selector.split("function submitAssessmentQuiz", 1)[0])


if __name__ == "__main__":
    unittest.main()
