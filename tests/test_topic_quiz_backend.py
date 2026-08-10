import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from backend import quiz_store
from backend.quiz_service import _parse_quiz_response, _validate_quiz_batch


def sample_quiz(topic_id: str, schema_version: int = 2) -> dict:
    return {
        "quiz_id": f"quiz-{topic_id}",
        "document_id": "lecture.pdf",
        "document_hash": "hash",
        "title": "Lecture",
        "topic_id": topic_id,
        "topic_name": topic_id.replace("_", " ").title(),
        "topic_schema_version": schema_version,
        "difficulty": "easy",
        "created_at": "2026-01-01T00:00:00+00:00",
        "questions": [
            {
                "id": 1,
                "question": "Which transport behavior is described by the selected material?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"],
                "correct_answer": "A",
                "topic_id": topic_id,
                "difficulty": "easy",
                "explanation": "The cited chunk explicitly describes this behavior.",
                "source_chunk_ids": ["hash_0"],
            }
        ],
    }


class TopicQuizBackendTests(unittest.TestCase):
    def test_json_parse_failure_logs_complete_escaped_raw_response(self):
        raw = '{"questions":[{"question":"line one\nline two" "options":[]}]}'
        output = StringIO()
        with self.assertRaises(json.JSONDecodeError), redirect_stdout(output):
            _parse_quiz_response(raw, 2)
        logged = output.getvalue()
        self.assertIn("[quiz-json-parse-error] attempt=3", logged)
        self.assertIn(json.dumps(raw, ensure_ascii=False), logged)
        self.assertNotIn("line one\nline two", logged)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "quiz.db"
        self.database_patch = patch.object(quiz_store, "DATABASE_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temp_dir.cleanup()

    def test_migrated_schema_uses_non_null_topic_variant_identity(self):
        quiz_store.initialize_quiz_store()
        connection = sqlite3.connect(self.database_path)
        try:
            quiz_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(quizzes)")}
            question_columns = {row[1] for row in connection.execute("PRAGMA table_info(quiz_questions)")}
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_active_quiz_variant'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(quiz_columns["topic_id"][3], 1)
        self.assertEqual(quiz_columns["topic_id"][4], "'document'")
        self.assertIn("topic_id, difficulty", index_sql)
        self.assertTrue({"topic_id", "difficulty", "explanation", "source_chunk_ids_json"} <= question_columns)

    def test_backend_attaches_exact_generation_batch_without_model_echo(self):
        raw = {
            "questions": [{
                "question": "Which transport behavior is described by the selected topic context?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"],
                "correct_answer": "A",
                "explanation": "The cited batch chunk states the correct behavior.",
            }]
        }
        question = _validate_quiz_batch(raw, 1, 1, "easy", "topic_001", ["batch_1"])[0]
        self.assertEqual(question["source_chunk_ids"], ["batch_1"])
        question = _validate_quiz_batch(raw, 1, 1, "easy", "topic_001", ["different_batch_9"])[0]
        self.assertEqual(question["source_chunk_ids"], ["different_batch_9"])

    def test_same_document_and_difficulty_keep_topics_independent(self):
        quiz_store.save_quiz("lecture.pdf", "easy", sample_quiz("topic_001"))
        quiz_store.save_quiz("lecture.pdf", "easy", sample_quiz("topic_002"))

        first = quiz_store.get_quiz("lecture.pdf", "easy", "topic_001")
        second = quiz_store.get_quiz("lecture.pdf", "easy", "topic_002")
        self.assertEqual(first["quiz_id"], "quiz-topic_001")
        self.assertEqual(second["quiz_id"], "quiz-topic_002")
        self.assertEqual(first["questions"][0]["topic_id"], "topic_001")
        self.assertEqual(second["questions"][0]["topic_id"], "topic_002")
        self.assertEqual(first["questions"][0]["explanation"], "The cited chunk explicitly describes this behavior.")
        self.assertEqual(first["questions"][0]["source_chunk_ids"], ["hash_0"])

    def test_topic_schema_change_invalidates_old_document_quizzes(self):
        quiz_store.save_quiz("lecture.pdf", "easy", sample_quiz("topic_001", schema_version=2))
        removed = quiz_store.invalidate_document_quizzes_for_topic_schema("lecture.pdf", 3)
        self.assertEqual(removed, 1)
        self.assertIsNone(quiz_store.get_quiz("lecture.pdf", "easy", "topic_001"))


if __name__ == "__main__":
    unittest.main()
