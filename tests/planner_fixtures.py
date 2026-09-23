"""Shared Study Planner test fixtures: an isolated SQLite database plus helpers that seed documents
and their persisted study artifacts (summary, flashcards, quizzes, attempts) exactly as the app does."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, flashcard_store, indexed_document_store, quiz_store, study_planner_store, summary_store
from backend.flashcard_service import FLASHCARD_VERSION
from backend.summary_service import SUMMARY_VERSION

HASH, SCHEMA = "hash-1", 3


class PlannerDatabaseMixin:
    def start_planner_database(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        db = root / "planner.db"
        self.patches = [
            patch.object(module, "DATABASE_PATH", db)
            for module in (auth_store, study_planner_store, indexed_document_store, summary_store,
                           flashcard_store, quiz_store)
        ] + [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", root / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", root / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", root / "missing-explanations.json"),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "missing-indexed.json"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.stop_planner_database)
        self.clock = 0

    def stop_planner_database(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def user(self, name):
        return auth_store.create_user(name, f"{name.lower()}-planner@example.com", "long-password-x")["id"]

    def tick(self) -> str:
        self.clock += 1
        return f"2026-09-2{self.clock // 60}T10:{self.clock % 60:02d}:00+00:00"

    def add_document(self, owner, document_id, title="Doc", chunks=12):
        indexed_document_store.upsert_indexed_document(owner, document_id, {
            "display_name": title, "hash": HASH, "chunks": chunks, "path": "x",
            "topic_schema_version": SCHEMA, "topics": [{"topic_id": "t1", "name": "T1"}],
        })

    def add_summary(self, owner, document_id):
        summary_store.save_summary({
            "owner_id": owner, "document_id": document_id, "document_hash": HASH, "topic_schema_version": SCHEMA,
            "summary_version": SUMMARY_VERSION, "model_id": "m1", "runtime_model": "r1",
        }, [], {}, {})

    def add_flashcards(self, owner, document_id, count):
        return flashcard_store.save_flashcards({
            "owner_id": owner, "document_id": document_id, "document_hash": HASH, "topic_schema_version": SCHEMA,
            "flashcard_version": FLASHCARD_VERSION, "model_id": "m1", "runtime_model": "r1", "topic_ids": ["t1"],
        }, [{"topic_id": "t1", "topic_name": "T1", "front": f"Q{i}", "back": f"A{i}"} for i in range(count)])

    def add_quiz(self, owner, document_id, quiz_id, question_count=10):
        quiz_store.initialize_quiz_store()
        with quiz_store._connect() as connection:
            connection.execute(
                """INSERT INTO quizzes (quiz_id, document_id, title, difficulty, question_count, created_at, owner_id)
                   VALUES (?, ?, 'Quiz', 'easy', ?, ?, ?)""",
                (quiz_id, document_id, question_count, self.tick(), owner),
            )

    def add_attempt(self, owner, document_id, quiz_id, score=0, answered=0, completed=False, total=10,
                    completed_at=None):
        stamp = completed_at or self.tick()
        with quiz_store._connect() as connection:
            connection.execute("UPDATE quiz_attempts SET is_latest=0 WHERE quiz_id=? AND student_id=?", (quiz_id, owner))
            connection.execute(
                """INSERT INTO quiz_attempts (attempt_id, quiz_id, document_id, difficulty, started_at, updated_at,
                   completed_at, score, answered, total, completed, is_latest, student_id, percentage)
                   VALUES (?, ?, ?, 'easy', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (f"att-{quiz_id}-{stamp}", quiz_id, document_id, stamp, stamp, stamp if completed else None, score,
                 answered, total, int(completed), owner,
                 round(100.0 * score / total, 2) if completed and total else 0),
            )
