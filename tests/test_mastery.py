import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_store
from backend.mastery_service import calculate_mastery, recompute_topic_mastery
from backend.quiz_service import update_quiz_progress


def answer(correct: bool, difficulty: str = "easy", outcome: str = "accepted", question_id: int = 1):
    return {
        "question_id": question_id,
        "question": f"Question {question_id}",
        "options": ["A", "B", "C", "D"],
        "selected_answer": "A" if correct else "B",
        "correct_answer": "A",
        "is_correct": correct,
        "question_difficulty": difficulty,
        "validation_outcome": outcome,
    }


class MasteryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "mastery.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def save_attempt(self, student: str, topic: str, results: list[dict], completed: bool = True):
        progress = {
            "quiz_id": f"quiz-{topic}",
            "answers": {str(item["question_id"]): item["selected_answer"] for item in results},
            "question_results": results,
            "score": sum(item["is_correct"] for item in results),
            "answered": len(results),
            "total": len(results) if completed else len(results) + 1,
            "completed": completed,
        }
        return quiz_store.save_quiz_progress(
            "mastery.pdf", "easy", progress, topic, student
        )

    def test_zero_attempts_are_not_assessed(self):
        result = recompute_topic_mastery("student-a", "mastery.pdf", "topic-zero")
        self.assertEqual(result["mastery_score"], 0)
        self.assertEqual(result["mastery_level"], "Not assessed")
        self.assertFalse(result["has_evidence"])
        self.assertFalse(result["has_sufficient_evidence"])
        self.assertEqual(result["minimum_questions_required"], 3)

    def test_one_question_has_numeric_score_but_insufficient_evidence(self):
        self.save_attempt("student-a", "topic-one", [answer(True)])
        result = recompute_topic_mastery("student-a", "mastery.pdf", "topic-one")
        self.assertEqual(result["mastery_score"], 100)
        self.assertEqual(result["mastery_level"], "Insufficient evidence")
        self.assertTrue(result["has_evidence"])
        self.assertFalse(result["has_sufficient_evidence"])

    def test_sufficient_evidence_uses_weighted_accuracy_and_level(self):
        results = [
            answer(True, "easy", question_id=1),
            answer(True, "medium", question_id=2),
            answer(False, "difficult", question_id=3),
        ]
        self.save_attempt("student-a", "topic-three", results)
        mastery = recompute_topic_mastery("student-a", "mastery.pdf", "topic-three")
        self.assertEqual(mastery["mastery_score"], 55.56)
        self.assertEqual(mastery["mastery_level"], "Developing")
        self.assertTrue(mastery["has_sufficient_evidence"])

    def test_quality_warning_reduces_effective_weight(self):
        result = calculate_mastery([
            answer(True, "easy", "accepted", 1),
            answer(False, "difficult", "accepted_quality_warning", 2),
        ], completed_attempts=1)
        self.assertEqual(result["earned_weight"], 1.0)
        self.assertEqual(result["possible_weight"], 2.5)
        self.assertEqual(result["mastery_score"], 40.0)

    def test_rebuild_uses_historical_attempt_snapshots(self):
        self.save_attempt("student-a", "topic-rebuild", [
            answer(True, "difficult", "accepted_quality_warning", 1),
            answer(False, "medium", "accepted", 2),
            answer(True, "easy", "accepted", 3),
        ])
        original = recompute_topic_mastery("student-a", "mastery.pdf", "topic-rebuild")
        with quiz_store._connect() as connection:
            connection.execute("DELETE FROM topic_mastery")
        rebuilt = recompute_topic_mastery("student-a", "mastery.pdf", "topic-rebuild")
        self.assertEqual(rebuilt["mastery_score"], original["mastery_score"])
        self.assertEqual(rebuilt["possible_weight"], original["possible_weight"])
        self.assertEqual(rebuilt["formula_version"], "weighted_accuracy_v1")

    def test_students_and_topics_are_isolated_and_incomplete_attempts_are_ignored(self):
        self.save_attempt("student-a", "topic-a", [answer(True, question_id=i) for i in range(1, 4)])
        self.save_attempt("student-b", "topic-a", [answer(False, question_id=i) for i in range(1, 4)])
        self.save_attempt("student-a", "topic-b", [answer(False, question_id=i) for i in range(1, 4)])
        quiz_store.reset_quiz_progress("mastery.pdf", "easy", "topic-a", "student-a")
        self.save_attempt("student-a", "topic-a", [answer(False, question_id=9)], completed=False)
        self.assertEqual(recompute_topic_mastery("student-a", "mastery.pdf", "topic-a")["mastery_score"], 100)
        self.assertEqual(recompute_topic_mastery("student-b", "mastery.pdf", "topic-a")["mastery_score"], 0)
        self.assertEqual(recompute_topic_mastery("student-a", "mastery.pdf", "topic-b")["mastery_score"], 0)

    def test_migrated_schema_preserves_raw_inputs_and_student_identity(self):
        quiz_store.initialize_quiz_store()
        connection = sqlite3.connect(self.database_path)
        try:
            attempts = {row[1]: row for row in connection.execute("PRAGMA table_info(quiz_attempts)")}
            answers = {row[1]: row for row in connection.execute("PRAGMA table_info(quiz_attempt_answers)")}
            mastery = {row[1] for row in connection.execute("PRAGMA table_info(topic_mastery)")}
            latest_index = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_latest_attempt_variant'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(attempts["student_id"][4], "'local_student'")
        self.assertEqual(answers["validation_outcome"][4], "'accepted'")
        self.assertIn("question_difficulty", answers)
        self.assertIn("has_sufficient_evidence", mastery)
        self.assertIn("student_id, document_id, topic_id, difficulty", latest_index)

    def test_editing_completed_attempt_recomputes_cached_mastery(self):
        quiz_store.save_quiz("mastery.pdf", "easy", {
            "quiz_id": "editable-quiz",
            "document_id": "mastery.pdf",
            "title": "Mastery",
            "topic_id": "topic-edit",
            "topic_name": "Editable",
            "difficulty": "easy",
            "questions": [{
                "id": 1,
                "question": "Which answer is correct?",
                "options": ["A. Correct", "B. Incorrect", "C. Other", "D. Other"],
                "correct_answer": "A",
                "topic_id": "topic-edit",
                "difficulty": "easy",
                "explanation": "A is correct.",
                "source_chunk_ids": ["hash_1"],
                "validation_outcome": "accepted",
            }],
        }, "student-edit")
        first = update_quiz_progress(
            "mastery.pdf", "easy", "topic-edit", 1, "A", "student-edit"
        )
        edited = update_quiz_progress(
            "mastery.pdf", "easy", "topic-edit", 1, "B", "student-edit"
        )
        cached = quiz_store.get_topic_mastery("student-edit", "mastery.pdf", "topic-edit")

        self.assertTrue(first["completed"])
        self.assertEqual(first["mastery"]["mastery_score"], 100.0)
        self.assertTrue(edited["completed"])
        self.assertEqual(edited["mastery"]["mastery_score"], 0.0)
        self.assertEqual(cached["mastery_score"], 0.0)

    def test_topic_schema_invalidation_removes_document_mastery_only(self):
        recompute_topic_mastery("student-a", "stale.pdf", "topic_001")
        recompute_topic_mastery("student-a", "stale.pdf", "topic_002")
        recompute_topic_mastery("student-a", "keep.pdf", "topic_001")

        quiz_store.invalidate_document_quizzes_for_topic_schema("stale.pdf", 3, "student-a")

        self.assertIsNone(quiz_store.get_topic_mastery("student-a", "stale.pdf", "topic_001"))
        self.assertIsNone(quiz_store.get_topic_mastery("student-a", "stale.pdf", "topic_002"))
        self.assertIsNotNone(quiz_store.get_topic_mastery("student-a", "keep.pdf", "topic_001"))


if __name__ == "__main__":
    unittest.main()
