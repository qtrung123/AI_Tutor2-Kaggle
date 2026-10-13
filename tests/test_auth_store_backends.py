import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from backend import auth_store, db
from fake_postgres import FakePsycopg


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
PASSWORD = "correct-horse-123"


def query(path: Path, sql: str, params=()) -> list[tuple]:
    raw = sqlite3.connect(path)
    try:
        return raw.execute(sql, params).fetchall()
    finally:
        raw.close()


class AuthStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.local_db = root / "data" / "conversations.db"
        self.stdout = io.StringIO()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(auth_store, "DATABASE_PATH", self.local_db))
        self.stack.enter_context(patch.object(auth_store, "_postgres_schema_ready", False))
        self.stack.enter_context(patch.object(auth_store, "_mirrored_users", set()))
        self.stack.enter_context(patch.object(db, "_announced", set()))
        self.stack.enter_context(patch("sys.stdout", self.stdout))

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def assert_full_auth_flow(self) -> dict:
        user = auth_store.create_user("  Ada  ", "Ada@Example.com", PASSWORD)
        self.assertEqual(str(UUID(user["id"])), user["id"])
        self.assertEqual(user["display_name"], "Ada")
        self.assertEqual(user["email"], "ada@example.com")
        self.assertIsNone(auth_store.authenticate_user("ada@example.com", "wrong-password"))
        self.assertEqual(auth_store.authenticate_user("  ADA@example.COM ", PASSWORD), user)
        token, session = auth_store.create_session(user["id"])
        self.assertEqual(session["user_id"], user["id"])
        self.assertEqual(auth_store.get_user_for_session(token), user)
        self.assertIsNone(auth_store.get_user_for_session("not-a-token"))
        auth_store.revoke_session(token)
        self.assertIsNone(auth_store.get_user_for_session(token))
        with self.assertRaisesRegex(ValueError, "already exists"):
            auth_store.create_user("Other", "aDa@EXAMPLE.com ", "another-password-1")
        return user


class SQLiteAuthStoreTests(AuthStoreTestBase):
    def setUp(self):
        super().setUp()
        environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        # Any attempt to load the PostgreSQL driver would raise ImportError.
        self.stack.enter_context(patch.dict(sys.modules, {"psycopg": None}))

    def test_auth_behavior_is_unchanged_without_database_url(self):
        user = self.assert_full_auth_flow()
        self.assertEqual(
            query(self.local_db, "SELECT id, email FROM users WHERE id = ?", (user["id"],)),
            [(user["id"], "ada@example.com")],
        )
        legacy = query(self.local_db, "SELECT disabled_at FROM users WHERE id = ?", (auth_store.LEGACY_USER_ID,))
        self.assertIsNotNone(legacy[0][0])
        self.assertEqual(self.stdout.getvalue(), "[db] backend=sqlite\n")

    def test_existing_user_lookup_returns_the_stored_user_id(self):
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        for _ in range(2):
            self.assertEqual(auth_store.authenticate_user("ada@example.com", PASSWORD)["id"], user["id"])

    def test_claim_legacy_user_still_rejects_taken_emails(self):
        auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        with self.assertRaisesRegex(ValueError, "already assigned"):
            auth_store.claim_legacy_user("Legacy", "ADA@example.com", PASSWORD)
        claimed = auth_store.claim_legacy_user("Legacy", "legacy@example.com", PASSWORD)
        self.assertEqual(claimed["id"], auth_store.LEGACY_USER_ID)
        self.assertEqual(auth_store.authenticate_user("legacy@example.com", PASSWORD)["id"], auth_store.LEGACY_USER_ID)


class PostgresAuthStoreTests(AuthStoreTestBase):
    def setUp(self):
        super().setUp()
        self.postgres_db = Path(self.temp_dir.name) / "postgres.db"
        self.fake = FakePsycopg(self.postgres_db)
        self.stack.enter_context(self.fake.installed(SECRET_URL))

    def simulate_kaggle_restart(self):
        """New process on a fresh Kaggle disk: in-process state and the local SQLite file are gone."""
        auth_store._postgres_schema_ready = False
        auth_store._mirrored_users.clear()
        self.local_db.unlink()

    def test_auth_flow_runs_on_postgres(self):
        user = self.assert_full_auth_flow()
        stored = query(self.postgres_db, "SELECT id, email, password_hash FROM users WHERE id = ?", (user["id"],))
        self.assertEqual(stored[0][:2], (user["id"], "ada@example.com"))
        self.assertTrue(stored[0][2].startswith("$argon2"))
        self.assertEqual(len(query(self.postgres_db, "SELECT 1 FROM auth_sessions WHERE user_id = ?", (user["id"],))), 1)
        self.assertEqual(query(self.local_db, "SELECT 1 FROM auth_sessions"), [])

    def test_local_sqlite_keeps_only_a_credential_less_placeholder_row(self):
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        local = query(self.local_db, "SELECT email, password_hash FROM users WHERE id = ?", (user["id"],))
        self.assertEqual(local, [(f"{user['id']}@postgres-account.invalid", "!stored-in-postgres")])
        self.assertEqual(query(self.local_db, "SELECT 1 FROM users WHERE email = 'ada@example.com'"), [])

    def test_user_id_survives_a_kaggle_restart(self):
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        token, _session = auth_store.create_session(user["id"])
        self.simulate_kaggle_restart()
        self.assertEqual(auth_store.get_user_for_session(token)["id"], user["id"])
        self.assertEqual(auth_store.authenticate_user("ADA@example.com", PASSWORD)["id"], user["id"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            auth_store.create_user("Ada again", "ada@example.com", PASSWORD)
        self.assertEqual(query(self.postgres_db, "SELECT COUNT(*) FROM users WHERE lower(email) = 'ada@example.com'"), [(1,)])

    def test_local_foreign_keys_accept_postgres_users_after_a_restart(self):
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        token, _session = auth_store.create_session(user["id"])
        self.simulate_kaggle_restart()
        auth_store.get_user_for_session(token)
        raw = sqlite3.connect(self.local_db)
        try:
            raw.execute("PRAGMA foreign_keys = ON")
            raw.execute("CREATE TABLE owned (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id))")
            raw.execute("INSERT INTO owned (id, owner_id) VALUES ('x', ?)", (user["id"],))
            raw.commit()
        finally:
            raw.close()

    def test_schema_initialization_is_idempotent_and_never_destructive(self):
        auth_store.initialize_auth_store()
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        auth_store.initialize_auth_store()
        self.assertEqual(sum("CREATE TABLE" in statement for statement in self.fake.statements), 1)
        self.simulate_kaggle_restart()
        auth_store.initialize_auth_store()
        self.assertEqual(sum("CREATE TABLE" in statement for statement in self.fake.statements), 2)
        self.assertEqual(auth_store.authenticate_user("ada@example.com", PASSWORD)["id"], user["id"])
        self.assertEqual(
            query(self.postgres_db, "SELECT COUNT(*) FROM users WHERE id = ?", (auth_store.LEGACY_USER_ID,)), [(1,)]
        )
        for statement in self.fake.statements:
            self.assertNotRegex(statement.upper(), r"\b(DROP|ALTER|TRUNCATE)\b")
        ddl = [line.strip() for line in auth_store._POSTGRES_SCHEMA.splitlines() if line.strip().startswith("CREATE")]
        self.assertTrue(ddl)
        for line in ddl:
            self.assertIn("IF NOT EXISTS", line)

    def test_email_uniqueness_is_case_insensitive_in_the_postgres_schema(self):
        self.assertNotIn("NOCASE", auth_store._POSTGRES_SCHEMA)
        self.assertIn("ON users (lower(email))", auth_store._POSTGRES_SCHEMA)
        auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        # Bypass Python-side normalization: the index itself must reject a case variant.
        raw = sqlite3.connect(self.postgres_db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO users (id, display_name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("other", "Other", "ADA@EXAMPLE.COM", "x", "2026-01-01T00:00:00+00:00"),
                )
        finally:
            raw.close()

    def test_sessions_cascade_with_their_user(self):
        user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        auth_store.create_session(user["id"])
        raw = sqlite3.connect(self.postgres_db)
        try:
            raw.execute("PRAGMA foreign_keys = ON")
            raw.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        finally:
            raw.close()

    def test_postgres_receives_only_psycopg_placeholders(self):
        self.assert_full_auth_flow()
        for statement in self.fake.statements:
            self.assertIsNone(re.search(r"\?(?=(?:[^']*'[^']*')*[^']*$)", statement), statement)

    def test_credentials_are_never_exposed_in_logs(self):
        self.assert_full_auth_flow()
        output = self.stdout.getvalue()
        self.assertEqual(output, "[db] backend=postgres connected\n")
        for part in ("s3cr3t-pw", "alice", "db.private-host.example", "postgresql://"):
            self.assertNotIn(part, output)
        self.assertEqual({call["conninfo"] for call in self.fake.connect_calls}, {SECRET_URL})


if __name__ == "__main__":
    unittest.main()
