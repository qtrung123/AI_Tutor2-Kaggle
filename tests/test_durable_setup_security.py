"""Supabase Data API lock-down (backend.durable_setup) without a database server.

- Every table the backend's schema initialization creates is in durable_setup.APP_TABLES, so a new
  table can never ship unlocked (anon/authenticated would otherwise read and write it through
  Supabase's Data API). The real-PostgreSQL check lives in test_durable_documents.py and only runs
  with AI_TUTOR_TEST_POSTGRES_URL; this one always runs.
- lock_down_api_roles only enables row level security and revokes the API roles: idempotent, and it
  never deletes, updates or drops anything.
- Re-running the schema initialization keeps existing rows untouched.
"""

import re
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from backend import (auth_store, conversation_store, durable_setup, flashcard_store, google_calendar_store,
                     indexed_document_store, quiz_store, study_planner_store, study_preferences, summary_jobs,
                     summary_store, visual_cache_store)

STORES = (auth_store, conversation_store, indexed_document_store, summary_store, summary_jobs, flashcard_store,
          quiz_store, study_planner_store, google_calendar_store, visual_cache_store)
# Created only on PostgreSQL (pgvector), so absent from a SQLite initialization.
POSTGRES_ONLY_TABLES = {"document_chunks"}


class LocalDatabase:
    """Every store pointed at one throw-away SQLite file (legacy JSON migrations disabled)."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.path = root / "app.db"
        self.stack = ExitStack()
        for store in STORES:
            if hasattr(store, "DATABASE_PATH"):
                self.stack.enter_context(patch.object(store, "DATABASE_PATH", self.path))
        self.stack.enter_context(patch.object(durable_setup, "DATABASE_PATH", self.path))
        self.stack.enter_context(patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "missing.json"))
        for name in ("LEGACY_GENERATED_QUIZZES_PATH", "LEGACY_QUIZ_ATTEMPTS_PATH", "LEGACY_QUIZ_EXPLANATIONS_PATH"):
            self.stack.enter_context(patch.object(quiz_store, name, root / f"missing-{name}.json"))

    def tables(self) -> set[str]:
        with closing(sqlite3.connect(self.path)) as connection:   # closed, so the temp dir can be removed on Windows
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return {row[0] for row in rows}

    def close(self):
        self.stack.close()
        self.temp.cleanup()


class AppTablesCoverageTests(unittest.TestCase):
    def setUp(self):
        self.database = LocalDatabase()
        self.addCleanup(self.database.close)

    def test_every_initialized_table_is_locked_down(self):
        durable_setup.initialize_all_stores()
        created = self.database.tables()
        self.assertIn("study_preferences", created)
        self.assertEqual(sorted(created - set(durable_setup.APP_TABLES)), [],
                         "add new tables to durable_setup.APP_TABLES so Supabase's Data API cannot reach them")
        self.assertEqual(sorted(set(durable_setup.APP_TABLES) - created - POSTGRES_ONLY_TABLES), [],
                         "APP_TABLES lists a table no store creates")

    def test_every_table_in_the_source_is_locked_down(self):
        """Also catches a table whose store is not initialized by durable_setup.initialize_all_stores."""
        backend = Path(__file__).resolve().parents[1] / "backend"
        declared = set()
        for source in backend.rglob("*.py"):
            declared |= {name.lower() for name in
                         re.findall(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", source.read_text(encoding="utf-8"))}
        # Temporary tables of a SQLite-only rebuild (e.g. schedule_runs_v2) are renamed in place.
        self.assertEqual(sorted(declared - set(durable_setup.APP_TABLES)), [])

    def test_visual_extraction_cache_is_created_at_startup_and_locked_down(self):
        """Regression: OCR/Vision text of private uploads was created lazily and missing from APP_TABLES,
        so on Supabase anon/authenticated could reach it through the Data API."""
        self.assertIn("visual_extraction_cache", durable_setup.APP_TABLES)
        durable_setup.initialize_all_stores()   # a fresh database: no upload has touched the cache yet
        created = self.database.tables()
        self.assertIn("visual_extraction_cache", created)
        with closing(sqlite3.connect(self.database.path)) as connection:   # owner_id references users(id)
            foreign_keys = connection.execute("PRAGMA foreign_key_list(visual_extraction_cache)").fetchall()
        self.assertEqual([(row[2], row[3], row[4]) for row in foreign_keys], [("users", "owner_id", "id")])
        connection = RecordingConnection(created)
        locked, roles = durable_setup.lock_down_api_roles(connection)
        self.assertEqual(roles, ["anon", "authenticated"])
        self.assertIn("ALTER TABLE visual_extraction_cache ENABLE ROW LEVEL SECURITY", connection.statements)
        self.assertIn("REVOKE ALL ON TABLE visual_extraction_cache FROM anon, authenticated", connection.statements)

    def test_every_table_created_at_startup_is_locked_and_revoked(self):
        durable_setup.initialize_all_stores()
        created = self.database.tables()
        connection = RecordingConnection(created)
        locked, _ = durable_setup.lock_down_api_roles(connection)
        rls = {m.group(1) for s in connection.statements
               if (m := re.fullmatch(r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY", s))}
        revoked = {m.group(1) for s in connection.statements
                   if (m := re.fullmatch(r"REVOKE ALL ON TABLE (\w+) FROM anon, authenticated", s))}
        self.assertEqual((rls, revoked, locked), (created, created, len(created)))

    def test_reinitializing_keeps_existing_rows(self):
        durable_setup.initialize_all_stores()
        owner = auth_store.create_user("Alice", "alice-security@example.com", "long-password-x")["id"]
        plan = study_planner_store.create_plan(owner, "Exams")
        study_preferences.save_preferences(owner, {"daily_target_minutes": 120, "durations": {"summary": 60}})
        durable_setup.initialize_all_stores()
        durable_setup.initialize_all_stores()
        self.assertEqual([p["plan_id"] for p in study_planner_store.list_plans(owner)], [plan["plan_id"]])
        saved = study_preferences.get_preferences(owner)
        self.assertEqual((saved["saved"], saved["daily_target_minutes"], saved["durations"]["summary"]), (True, 120, 60))


class RecordingConnection:
    """Answers lock_down_api_roles' two catalog queries and records every statement."""

    def __init__(self, present, roles=("anon", "authenticated")):
        self.present, self.roles = list(present), list(roles)
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        if "information_schema.tables" in sql:
            return Result([(name,) for name in self.present])
        if "pg_roles" in sql:
            return Result([(role,) for role in self.roles if role in params])
        raise AssertionError(f"unexpected query: {sql}")

    def executescript(self, sql):
        self.statements.append(sql)


class LockDownStatementTests(unittest.TestCase):
    def test_lock_down_only_enables_rls_and_revokes_api_roles_and_is_idempotent(self):
        present = [*durable_setup.APP_TABLES, "someone_elses_table"]
        first, second = RecordingConnection(present), RecordingConnection(present)
        self.assertEqual(durable_setup.lock_down_api_roles(first), (len(durable_setup.APP_TABLES), ["anon", "authenticated"]))
        self.assertEqual(durable_setup.lock_down_api_roles(second), (len(durable_setup.APP_TABLES), ["anon", "authenticated"]))
        self.assertEqual(first.statements, second.statements)   # same statements every run
        writes = [s for s in first.statements if s.startswith(("ALTER", "REVOKE"))]
        self.assertIn("ALTER TABLE study_preferences ENABLE ROW LEVEL SECURITY", writes)
        self.assertIn("REVOKE ALL ON TABLE study_preferences FROM anon, authenticated", writes)
        for statement in first.statements:
            self.assertTrue(
                statement.startswith("SELECT ")
                or re.fullmatch(r"ALTER TABLE \w+ ENABLE ROW LEVEL SECURITY", statement)
                or re.fullmatch(r"REVOKE ALL ON TABLE \w+ FROM anon, authenticated", statement), statement)
            self.assertNotRegex(statement.upper(), r"\b(DELETE|UPDATE|INSERT|DROP|TRUNCATE)\b")
        self.assertFalse(any("someone_elses_table" in statement for statement in first.statements))

    def test_startup_creates_every_store_before_locking_down(self):
        """lock_down_api_roles only sees tables that exist, so every store must be created first."""
        order = []

        class FakeConnection:
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        with patch.object(durable_setup.db, "selected_backend", return_value=durable_setup.db.POSTGRES), \
             patch.object(durable_setup.db, "verify_connection"), \
             patch.object(durable_setup.db, "connect", return_value=FakeConnection()), \
             patch.object(durable_setup.document_storage, "durable_storage_enabled", return_value=False), \
             patch.object(durable_setup.vector_store, "_vector_schema", return_value="public"), \
             patch.object(durable_setup, "initialize_all_stores", side_effect=lambda: order.append("create")), \
             patch.object(durable_setup, "lock_down_api_roles",
                          side_effect=lambda connection: order.append("lock") or (0, [])), \
             patch("builtins.print"):
            durable_setup.run()
        self.assertEqual(order, ["create", "lock"])

    def test_without_api_roles_only_row_level_security_is_enabled(self):
        connection = RecordingConnection(["users", "study_preferences"], roles=())
        self.assertEqual(durable_setup.lock_down_api_roles(connection), (2, []))
        self.assertFalse(any(statement.startswith("REVOKE") for statement in connection.statements))


if __name__ == "__main__":
    unittest.main()
