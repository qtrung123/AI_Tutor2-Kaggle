"""PERF-2: listing documents on remote PostgreSQL costs a fixed number of reads and no writes.

quiz_common.list_indexed_documents used to run invalidate_document_quizzes_for_topic_schema (a
5-statement write transaction) for every document on every call. It now finds the documents that
really hold rows from an older topic map in one read and invalidates only those. These tests pin
the query budget, the invalidation behaviour when a topic schema does change, and owner isolation.
Runs on tests/fake_postgres.py (no server needed).
"""

import io
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, db, indexed_document_store, quiz_common, quiz_store
from backend.mastery_service import recompute_topic_mastery
from fake_postgres import FakePsycopg


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
NOW = "2026-10-15T00:00:00+00:00"
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")


def _fake_table_columns(connection, table: str) -> set[str]:
    """The fake server is SQLite underneath and has no information_schema: read PRAGMA instead."""
    if table not in connection._columns:
        connection._columns[table] = {row[1] for row in connection._raw._db.execute(f"PRAGMA table_info({table})")}
    return set(connection._columns[table])


class DocumentListingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.backing = root / "postgres.db"
        self.fake = FakePsycopg(self.backing, reuse_connections=True)
        self.stack = ExitStack()
        for module in (auth_store, indexed_document_store, quiz_store):
            self.stack.enter_context(patch.object(module, "DATABASE_PATH", root / "local.db"))
        self.stack.enter_context(patch.object(db, "_announced", set()))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.stack.enter_context(self.fake.installed(SECRET_URL))
        self.stack.enter_context(patch.object(db, "table_columns", _fake_table_columns))
        self.addCleanup(db.close_pools)
        quiz_store.initialize_quiz_store()
        indexed_document_store.initialize_indexed_document_store()
        for user in ("alice", "bob"):
            self.sql("INSERT INTO users (id, display_name, email, password_hash, created_at) VALUES (?, ?, ?, 'x', ?)",
                     (user, user, f"{user}@example.com", NOW))

    def tearDown(self):
        db.close_pools()
        self.stack.close()
        self.temp_dir.cleanup()

    # -- helpers --------------------------------------------------------------------------------

    def sql(self, statement: str, params=()):
        with db.connect("unused.db") as connection:
            connection.execute(statement, params)

    def rows(self, statement: str, params=()) -> list[tuple]:
        raw = sqlite3.connect(self.backing)
        try:
            return raw.execute(statement, params).fetchall()
        finally:
            raw.close()

    def add_document(self, owner: str, document_id: str, version: int):
        indexed_document_store.upsert_indexed_document(owner, document_id, {
            "hash": f"hash-{document_id}", "chunks": 3, "path": f"/docs/{document_id}",
            "topic_schema_version": version, "topics": [{"topic_id": "topic_001", "name": "Intro"}],
        })

    def add_quiz(self, owner: str, document_id: str, quiz_id: str, version: int, difficulty: str = "easy"):
        self.sql(
            "INSERT INTO quizzes (quiz_id, owner_id, document_id, title, difficulty, question_count, created_at, "
            "is_active, topic_id, topic_schema_version) VALUES (?, ?, ?, 'Quiz', ?, 12, ?, 1, ?, ?)",
            (quiz_id, owner, document_id, difficulty, NOW, f"topic-{quiz_id}", version),
        )

    def add_attempt(self, owner: str, document_id: str, attempt_id: str, quiz_id: str | None, completed: bool):
        self.sql(
            "INSERT INTO quiz_attempts (attempt_id, quiz_id, document_id, difficulty, started_at, updated_at, "
            "completed, is_latest, student_id, topic_id) VALUES (?, ?, ?, 'easy', ?, ?, ?, 1, ?, ?)",
            (attempt_id, quiz_id, document_id, NOW, NOW, int(completed), owner, f"topic-{attempt_id}"),
        )

    def add_mastery(self, owner: str, document_id: str):
        recompute_topic_mastery(owner, document_id, "topic_001")  # stores a topic_mastery row

    def list_and_count(self, owner: str = "alice") -> tuple[list[dict], list[str], list[str]]:
        self.fake.statements.clear()
        self.fake.round_trips.clear()
        listed = quiz_common.list_indexed_documents(owner)
        return listed, list(self.fake.statements), list(self.fake.round_trips)

    # -- tests ----------------------------------------------------------------------------------

    def test_listing_up_to_date_documents_is_two_reads_and_no_writes(self):
        for index in range(10):
            self.add_document("alice", f"doc-{index}.pdf", 2)
            self.add_quiz("alice", f"doc-{index}.pdf", f"quiz-{index}", 2)
            self.add_attempt("alice", f"doc-{index}.pdf", f"done-{index}", f"quiz-{index}", completed=True)
        self.add_mastery("alice", "doc-0.pdf")

        listed, statements, round_trips = self.list_and_count()

        self.assertEqual(len(listed), 10)
        self.assertEqual(len(statements), 2)  # list documents + one staleness read
        self.assertFalse([s for s in statements if s.lstrip().upper().startswith(WRITE_PREFIXES)])
        self.assertEqual(round_trips, ["statement", "statement"])  # autocommit reads, no check/BEGIN/COMMIT
        # Mastery (knowledge gaps) is no longer wiped by a plain listing.
        self.assertEqual(self.rows("SELECT COUNT(*) FROM topic_mastery WHERE document_id = 'doc-0.pdf'"), [(1,)])

    def test_response_structure_is_unchanged(self):
        self.add_document("alice", "doc.pdf", 2)
        listed, _statements, _round_trips = self.list_and_count()
        self.assertEqual(listed, [{
            "id": "doc.pdf", "title": "doc.pdf", "chunks": 3, "hash": "hash-doc.pdf",
            "topic_schema_version": 2, "topics": [{"topic_id": "topic_001", "name": "Intro"}],
        }])

    def test_topic_schema_change_still_invalidates_only_that_document(self):
        self.add_document("alice", "stale.pdf", 3)
        self.add_document("alice", "fresh.pdf", 3)
        self.add_quiz("alice", "stale.pdf", "old-quiz", 2)
        self.add_quiz("alice", "stale.pdf", "new-quiz", 3, difficulty="medium")
        self.add_attempt("alice", "stale.pdf", "old-open", "old-quiz", completed=False)
        self.add_attempt("alice", "stale.pdf", "old-done", "old-quiz", completed=True)
        self.add_quiz("alice", "fresh.pdf", "fresh-quiz", 3)
        self.add_attempt("alice", "fresh.pdf", "fresh-done", "fresh-quiz", completed=True)
        self.add_mastery("alice", "stale.pdf")
        self.add_mastery("alice", "fresh.pdf")

        with patch.object(quiz_common, "invalidate_document_quizzes_for_topic_schema",
                          wraps=quiz_store.invalidate_document_quizzes_for_topic_schema) as invalidate:
            quiz_common.list_indexed_documents("alice")
        invalidate.assert_called_once_with("stale.pdf", 3, "alice")

        quizzes = dict(self.rows("SELECT quiz_id, is_active FROM quizzes"))
        self.assertEqual(quizzes, {"old-quiz": 0, "new-quiz": 1, "fresh-quiz": 1})
        attempts = dict(self.rows("SELECT attempt_id, is_latest FROM quiz_attempts"))
        self.assertEqual(attempts, {"old-done": 0, "fresh-done": 1})  # unfinished stale attempt removed
        self.assertEqual(self.rows("SELECT document_id FROM topic_mastery"), [("fresh.pdf",)])

        # Converged: the next listing reads only.
        _listed, statements, _round_trips = self.list_and_count()
        self.assertEqual(len(statements), 2)

    def test_unfinished_attempt_without_a_quiz_is_still_cleaned_up(self):
        self.add_document("alice", "doc.pdf", 2)
        self.add_attempt("alice", "doc.pdf", "orphan", None, completed=False)
        quiz_common.list_indexed_documents("alice")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM quiz_attempts"), [(0,)])

    def test_other_owners_stale_rows_neither_trigger_nor_get_touched(self):
        self.add_document("alice", "shared-name.pdf", 3)
        self.add_document("bob", "shared-name.pdf", 1)
        self.add_quiz("bob", "shared-name.pdf", "bob-quiz", 1)
        self.add_attempt("bob", "shared-name.pdf", "bob-open", "bob-quiz", completed=False)

        _listed, statements, _round_trips = self.list_and_count("alice")

        self.assertEqual(len(statements), 2)
        self.assertEqual(self.rows("SELECT is_active FROM quizzes WHERE quiz_id = 'bob-quiz'"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM quiz_attempts WHERE student_id = 'bob'"), [(1,)])
        self.assertEqual([d["id"] for d in quiz_common.list_indexed_documents("bob")], ["shared-name.pdf"])

    def test_staleness_helper_ignores_unlisted_documents(self):
        self.add_quiz("alice", "deleted.pdf", "orphan-quiz", 1)
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("alice", {}), set())
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("alice", {"other.pdf": 1}), set())
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("alice", {"deleted.pdf": 2}),
                         {"deleted.pdf"})


class SQLiteDocumentListingTests(unittest.TestCase):
    """The same staleness read runs on the local SQLite backend."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "local.db"
        self.stack = ExitStack()
        for module in (auth_store, indexed_document_store, quiz_store):
            self.stack.enter_context(patch.object(module, "DATABASE_PATH", path))
        self.stack.enter_context(patch.dict("os.environ", {"DATABASE_URL": ""}))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_schema_change_detection_on_sqlite(self):
        quiz_store.initialize_quiz_store()
        with quiz_store._connect() as connection:
            connection.execute(
                "INSERT INTO quizzes (quiz_id, owner_id, document_id, title, difficulty, question_count, created_at, "
                "is_active, topic_id, topic_schema_version) VALUES ('q', 'alice', 'doc.pdf', 'Quiz', 'easy', 12, ?, 1, 't', 1)",
                (NOW,),
            )
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("alice", {"doc.pdf": 1}), set())
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("alice", {"doc.pdf": 2}), {"doc.pdf"})
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation("bob", {"doc.pdf": 2}), set())


if __name__ == "__main__":
    unittest.main()
