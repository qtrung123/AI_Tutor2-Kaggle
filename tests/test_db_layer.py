import io
import os
import sqlite3
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend import db
from fake_postgres import FakePsycopg, OperationalError


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
SECRET_PARTS = ("s3cr3t-pw", "alice", "db.private-host.example", "postgresql://")


def without_database_url():
    environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    return patch.dict(os.environ, environment, clear=True)


class BackendSelectionTests(unittest.TestCase):
    def test_no_database_url_selects_sqlite(self):
        with without_database_url():
            self.assertEqual(db.selected_backend(), db.SQLITE)

    def test_blank_database_url_selects_sqlite(self):
        with patch.dict(os.environ, {"DATABASE_URL": "   "}):
            self.assertEqual(db.selected_backend(), db.SQLITE)

    def test_postgres_urls_select_postgres(self):
        for url in (SECRET_URL, "postgres://u:p@h/d", "POSTGRESQL://u:p@h/d"):
            with self.subTest(url=url.split("://")[0]), patch.dict(os.environ, {"DATABASE_URL": url}):
                self.assertEqual(db.selected_backend(), db.POSTGRES)

    def test_unsupported_url_is_rejected_without_echoing_it(self):
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://alice:s3cr3t-pw@db.private-host.example/tutor"}):
            with self.assertRaises(db.DatabaseConfigurationError) as caught:
                db.selected_backend()
        for part in ("s3cr3t-pw", "alice", "db.private-host.example"):
            self.assertNotIn(part, str(caught.exception))


class PlaceholderTranslationTests(unittest.TestCase):
    def test_question_marks_become_psycopg_placeholders(self):
        self.assertEqual(
            db.to_pyformat("SELECT * FROM users WHERE id = ? AND email = ?"),
            "SELECT * FROM users WHERE id = %s AND email = %s",
        )

    def test_quoted_text_and_comments_are_not_rewritten(self):
        sql = "SELECT 'why?', \"odd?col\" FROM t -- really?\nWHERE a = ? /* b = ? */"
        self.assertEqual(
            db.to_pyformat(sql),
            "SELECT 'why?', \"odd?col\" FROM t -- really?\nWHERE a = %s /* b = ? */",
        )

    def test_doubled_quotes_stay_inside_the_literal(self):
        self.assertEqual(db.to_pyformat("SELECT 'it''s ?' WHERE x = ?"), "SELECT 'it''s ?' WHERE x = %s")

    def test_percent_signs_are_escaped_everywhere(self):
        self.assertEqual(
            db.to_pyformat("SELECT 100 % 7, 'a%b' WHERE name LIKE ?"),
            "SELECT 100 %% 7, 'a%%b' WHERE name LIKE %s",
        )

    def test_unterminated_quote_is_rejected(self):
        with self.assertRaises(ValueError):
            db.to_pyformat("SELECT 'oops WHERE a = ?")


class SQLiteConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "app.db"
        self.env = without_database_url()
        self.env.start()
        with db.connect(self.path) as connection:
            connection.executescript(
                "CREATE TABLE parent (id TEXT PRIMARY KEY);"
                "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES parent(id));"
            )

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def rows(self, table):
        raw = sqlite3.connect(self.path)
        try:
            return [row[0] for row in raw.execute(f"SELECT id FROM {table}")]
        finally:
            raw.close()

    def test_commits_on_success_and_returns_named_rows(self):
        with db.connect(self.path) as connection:
            self.assertEqual(connection.backend, db.SQLITE)
            connection.execute("INSERT INTO parent (id) VALUES (?)", ("p1",))
            self.assertEqual(connection.execute("SELECT id FROM parent").fetchone()["id"], "p1")
        self.assertEqual(self.rows("parent"), ["p1"])

    def test_rolls_back_and_closes_on_failure(self):
        with self.assertRaises(RuntimeError):
            with db.connect(self.path) as connection:
                connection.execute("INSERT INTO parent (id) VALUES (?)", ("p1",))
                raise RuntimeError("boom")
        self.assertEqual(self.rows("parent"), [])
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_constraint_violations_raise_the_shared_integrity_error(self):
        with self.assertRaises(db.IntegrityError):
            with db.connect(self.path) as connection:
                connection.execute("INSERT INTO child (id, parent_id) VALUES (?, ?)", ("c1", "missing"))
        self.assertEqual(self.rows("child"), [])


class PostgresConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backing = Path(self.temp_dir.name) / "postgres.db"
        self.announced = patch.object(db, "_announced", set())
        self.announced.start()

    def tearDown(self):
        self.announced.stop()
        self.temp_dir.cleanup()

    def test_connects_with_dict_rows_timeout_and_announces_without_credentials(self):
        fake = FakePsycopg(self.backing)
        output = io.StringIO()
        with fake.installed(SECRET_URL), redirect_stdout(output):
            for _ in range(2):
                with db.connect("unused.db") as connection:
                    self.assertEqual(connection.backend, db.POSTGRES)
                    connection.executescript("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, label TEXT);")
                    connection.execute("INSERT INTO t (id, label) VALUES (?, ?)", (f"r{_}", "50% ?"))
        self.assertEqual(output.getvalue(), "[db] backend=postgres connected\n")
        self.assertIs(fake.connect_calls[0]["row_factory"], fake.rows.dict_row)
        self.assertEqual(fake.connect_calls[0]["connect_timeout"], 10)
        self.assertIn("INSERT INTO t (id, label) VALUES (%s, %s)", fake.statements)
        raw = sqlite3.connect(self.backing)
        try:
            self.assertEqual(raw.execute("SELECT label FROM t ORDER BY id").fetchall(), [("50% ?",), ("50% ?",)])
        finally:
            raw.close()

    def test_rolls_back_postgres_work_on_failure(self):
        fake = FakePsycopg(self.backing)
        with fake.installed(SECRET_URL), redirect_stdout(io.StringIO()):
            with db.connect("unused.db") as connection:
                connection.executescript("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY);")
            with self.assertRaises(db.IntegrityError):
                with db.connect("unused.db") as connection:
                    connection.execute("INSERT INTO t (id) VALUES (?)", ("a",))
                    connection.execute("INSERT INTO t (id) VALUES (?)", ("a",))
        raw = sqlite3.connect(self.backing)
        try:
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        finally:
            raw.close()

    def test_connection_failure_never_exposes_credentials(self):
        libpq_message = (
            'connection to server at "db.private-host.example" (10.0.0.5), port 5432 failed: '
            'FATAL: password authentication failed for user "alice"'
        )
        fake = FakePsycopg(self.backing, connect_error=OperationalError(libpq_message))
        output = io.StringIO()
        with fake.installed(SECRET_URL), redirect_stdout(output):
            with self.assertRaises(db.DatabaseConnectionError) as caught:
                with db.connect("unused.db"):
                    pass
        rendered = "".join(traceback.format_exception(caught.exception)) + output.getvalue()
        self.assertIn("backend=postgres connection failed (OperationalError)", rendered)
        for part in SECRET_PARTS:
            self.assertNotIn(part, rendered)

    def test_missing_driver_is_reported_without_the_url(self):
        with patch.dict(sys.modules, {"psycopg": None}), patch.dict(os.environ, {"DATABASE_URL": SECRET_URL}):
            with self.assertRaises(db.DatabaseConfigurationError) as caught:
                with db.connect("unused.db"):
                    pass
        self.assertIn("psycopg is not installed", str(caught.exception))
        for part in SECRET_PARTS:
            self.assertNotIn(part, str(caught.exception))

    def test_sqlite_selection_is_announced_once_and_forced_sqlite_is_silent(self):
        output = io.StringIO()
        with without_database_url(), redirect_stdout(output):
            path = Path(self.temp_dir.name) / "local.db"
            with db.connect(path, backend=db.SQLITE):
                pass
            self.assertEqual(output.getvalue(), "")
            for _ in range(2):
                with db.connect(path):
                    pass
        self.assertEqual(output.getvalue(), "[db] backend=sqlite\n")


if __name__ == "__main__":
    unittest.main()
