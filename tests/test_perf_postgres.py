"""PERF-1..4 against a REAL PostgreSQL (skipped unless AI_TUTOR_TEST_POSTGRES_URL is set).

Re-runs the PERF equivalence and safety tests on a throwaway schema, so the new SQL (UNION
staleness read, window-function batches, autocommit units, pooled-connection health check) is
checked by PostgreSQL itself, not only by tests/fake_postgres.py. Never point this at production:
each test creates and drops its own ai_tutor_test_* schema, but it still needs CREATE SCHEMA rights.

    AI_TUTOR_TEST_POSTGRES_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \\
        python -m pytest tests/test_perf_postgres.py
"""

import io
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from uuid import uuid4

from backend import auth_store, db, indexed_document_store, quiz_common, quiz_store, study_progress
from backend.quiz_service import build_learning_dashboard
import test_dashboard_queries  # module import: keeps pytest from re-collecting its SQLite tests here
from test_postgres_persistence import POSTGRES_TEST_URL, with_search_path


class PostgresSchemaMixin:
    """Point every store at a fresh schema of the test server for the duration of one test."""

    def use_postgres_schema(self, stack: ExitStack):
        import psycopg

        self.schema = f"ai_tutor_test_{uuid4().hex[:12]}"
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(self.drop_schema)
        stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": with_search_path(POSTGRES_TEST_URL, self.schema)}))
        stack.enter_context(patch.object(db, "_pools", {}))
        stack.enter_context(patch.object(db, "_schema_ready", set()))
        stack.enter_context(patch.object(db, "_announced", set()))
        stack.enter_context(patch("sys.stdout", io.StringIO()))

    def drop_schema(self):
        import psycopg

        db.close_pools()
        assert self.schema.startswith("ai_tutor_test_")  # only ever the schema this test created
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")


@unittest.skipUnless(POSTGRES_TEST_URL, "set AI_TUTOR_TEST_POSTGRES_URL to run against a real PostgreSQL")
class PostgresBatchedReadTests(PostgresSchemaMixin, test_dashboard_queries.BatchedReadEquivalenceTests):
    """Every PERF-3 batched read equals the per-item read it replaced -- on PostgreSQL."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.use_postgres_schema(stack)
        super().setUp()

    def test_batches_ran_on_postgres(self):
        with db.connect("unused.db") as connection:
            self.assertEqual(connection.backend, db.POSTGRES)


@unittest.skipUnless(POSTGRES_TEST_URL, "set AI_TUTOR_TEST_POSTGRES_URL to run against a real PostgreSQL")
class PostgresPerfSafetyTests(PostgresSchemaMixin, unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.use_postgres_schema(stack)
        quiz_store.initialize_quiz_store()
        indexed_document_store.initialize_indexed_document_store()
        self.alice = auth_store.create_user("Alice", "alice-perf@example.com", "long-password-x")["id"]
        self.bob = auth_store.create_user("Bob", "bob-perf@example.com", "long-password-x")["id"]

    def sql(self, statement, params=()):
        with db.connect("unused.db") as connection:
            return connection.execute(statement, params).fetchall() if statement.lstrip().upper().startswith("SELECT") \
                else connection.execute(statement, params)

    def add_document(self, owner, document_id, version):
        indexed_document_store.upsert_indexed_document(owner, document_id, {
            "hash": "h", "chunks": 3, "path": "x", "topic_schema_version": version,
            "topics": [{"topic_id": "t1", "name": "T1"}]})

    def add_quiz(self, owner, document_id, quiz_id, version, difficulty="easy"):
        self.sql("INSERT INTO quizzes (quiz_id, owner_id, document_id, title, difficulty, question_count, created_at, "
                 "topic_id, topic_schema_version) VALUES (?, ?, ?, 'Q', ?, 3, '2026-10-15T00:00:00+00:00', ?, ?)",
                 (quiz_id, owner, document_id, difficulty, quiz_id, version))

    def add_attempt(self, owner, document_id, attempt_id, quiz_id, completed):
        self.sql("INSERT INTO quiz_attempts (attempt_id, quiz_id, document_id, difficulty, started_at, updated_at, "
                 "completed, is_latest, student_id, topic_id) VALUES (?, ?, ?, 'easy', '2026-10-15T00:00:00+00:00', "
                 "'2026-10-15T00:00:00+00:00', ?, 1, ?, ?)", (attempt_id, quiz_id, document_id, int(completed), owner, attempt_id))

    # -- PERF-1 --------------------------------------------------------------------------------

    def test_autocommit_connection_returns_to_the_pool_transactional_and_is_reused_unchecked(self):
        self.sql("CREATE TABLE perf_t (id TEXT PRIMARY KEY)")
        with db.connect("unused.db", autocommit=True) as connection:
            connection.execute("INSERT INTO perf_t (id) VALUES (?)", ("kept",))
            raw = connection._raw
        self.assertFalse(raw.autocommit)
        self.assertTrue(db._released_recently(raw))   # real psycopg attributes: closed/broken/info
        with self.assertRaises(db.IntegrityError):
            with db.connect("unused.db") as connection:
                connection.execute("INSERT INTO perf_t (id) VALUES (?)", ("rolled-back",))
                connection.execute("INSERT INTO perf_t (id) VALUES (?)", ("kept",))
        self.assertEqual([row[0] for row in self.sql("SELECT id FROM perf_t ORDER BY id")], ["kept"])

    def test_session_lookup_is_one_statement_and_still_isolated(self):
        alice_token, _ = auth_store.create_session(self.alice)
        bob_token, _ = auth_store.create_session(self.bob)
        auth_store.get_user_for_session(alice_token)          # first touch writes last_seen_at
        stats, token = db.start_request_stats()
        try:
            self.assertEqual(auth_store.get_user_for_session(alice_token)["id"], self.alice)
        finally:
            db.stop_request_stats(token)
        self.assertEqual((stats.units, stats.statements), (1, 1))
        auth_store.revoke_session(alice_token)
        self.assertIsNone(auth_store.get_user_for_session(alice_token))
        self.assertEqual(auth_store.get_user_for_session(bob_token)["id"], self.bob)

    # -- PERF-2 --------------------------------------------------------------------------------

    def test_listing_reads_only_and_invalidates_only_changed_schema_per_owner(self):
        self.add_document(self.alice, "stale.pdf", 3)
        self.add_document(self.alice, "fresh.pdf", 3)
        self.add_document(self.bob, "stale.pdf", 1)
        self.add_quiz(self.alice, "stale.pdf", "old", 2)
        self.add_attempt(self.alice, "stale.pdf", "old-open", "old", completed=False)
        self.add_attempt(self.alice, "stale.pdf", "old-done", "old", completed=True)
        self.add_quiz(self.alice, "fresh.pdf", "fresh", 3)
        self.add_quiz(self.bob, "stale.pdf", "bob-old", 0)
        self.assertEqual(quiz_store.documents_needing_topic_schema_invalidation(
            self.alice, {"stale.pdf": 3, "fresh.pdf": 3}), {"stale.pdf"})

        quiz_common.list_indexed_documents(self.alice)
        quizzes = dict(self.sql("SELECT quiz_id, is_active FROM quizzes"))
        self.assertEqual(quizzes, {"old": 0, "fresh": 1, "bob-old": 1})   # bob untouched
        self.assertEqual(dict(self.sql("SELECT attempt_id, is_latest FROM quiz_attempts")), {"old-done": 0})

        stats, token = db.start_request_stats()
        try:
            listed = quiz_common.list_indexed_documents(self.alice)
        finally:
            db.stop_request_stats(token)
        self.assertEqual(sorted(document["id"] for document in listed), ["fresh.pdf", "stale.pdf"])
        self.assertEqual((stats.units, stats.statements), (2, 2))

    # -- PERF-3/4 ------------------------------------------------------------------------------

    def test_dashboard_reads_have_a_fixed_cost_on_postgres(self):
        for index in range(4):
            self.add_document(self.alice, f"d{index}.pdf", 3)
            self.add_quiz(self.alice, f"d{index}.pdf", f"q{index}", 3)
        stats, token = db.start_request_stats()
        try:
            dashboard = build_learning_dashboard(self.alice)
            study_progress.latest_quiz_percentages(self.alice, [row["document_id"] for row in dashboard["materials"]])
        finally:
            db.stop_request_stats(token)
        self.assertEqual((stats.units, stats.statements), (5, 6))
        self.assertGreater(stats.db_seconds, 0)


if __name__ == "__main__":
    unittest.main()
