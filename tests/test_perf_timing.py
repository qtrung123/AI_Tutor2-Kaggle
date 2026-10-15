"""PERF-4: opt-in per-request diagnostics (backend/perf_timing.py + db.RequestStats).

Off by default and silent; when on, every response carries Server-Timing with the request's
database units, statements, wait and execution time, and one [perf] line is printed that names
only the route template -- never the concrete path, SQL text, parameters or credentials.
"""

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import db, perf_timing
from fake_postgres import FakePsycopg


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
SERVER_TIMING = re.compile(
    r'^app;dur=(?P<app>[\d.]+), db;dur=(?P<db>[\d.]+);desc="(?P<units>\d+) units (?P<statements>\d+) statements", '
    r"dbwait;dur=(?P<wait>[\d.]+)$"
)


def without_database_url():
    environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    return patch.dict(os.environ, environment, clear=True)


class PerfTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "perf.db"
        self.addCleanup(self.temp_dir.cleanup)
        self.announced = patch.object(db, "_announced", set())
        self.announced.start()
        self.addCleanup(self.announced.stop)

    def make_app(self) -> FastAPI:
        app = FastAPI()
        path = self.path

        @app.get("/api/items/{document_id}")
        def read_item(document_id: str) -> dict:   # sync route: runs in the threadpool
            with db.connect(path) as connection:
                connection.executescript("CREATE TABLE IF NOT EXISTS t (v TEXT);")
                connection.execute("INSERT INTO t (v) VALUES (?)", ("secret-parameter",))
            with db.connect(path, autocommit=True) as connection:
                connection.execute("SELECT v FROM t WHERE v = ?", ("secret-parameter",)).fetchall()
            return {"ok": True}

        app.add_middleware(perf_timing.PerfTimingMiddleware, allowed_origins=["https://tutor.example"])
        return app

    def test_disabled_by_default_and_counts_nothing(self):
        with patch.dict(os.environ, {"AI_TUTOR_PERF_TIMING": ""}):
            self.assertFalse(perf_timing.enabled())
        for value in ("1", "true", "ON"):
            with patch.dict(os.environ, {"AI_TUTOR_PERF_TIMING": value}):
                self.assertTrue(perf_timing.enabled())
        self.assertIsNone(db._request_stats.get())
        with without_database_url(), redirect_stdout(io.StringIO()):
            with db.connect(self.path, autocommit=True) as connection:
                connection.execute("SELECT 1").fetchone()
        self.assertIsNone(db._request_stats.get())

    def test_main_app_does_not_install_the_middleware_unless_enabled(self):
        if perf_timing.enabled():
            self.skipTest("AI_TUTOR_PERF_TIMING is set in this environment")
        from backend.main import app
        self.assertNotIn(perf_timing.PerfTimingMiddleware, [item.cls for item in app.user_middleware])

    def test_server_timing_header_and_sanitized_log_line(self):
        output = io.StringIO()
        with without_database_url(), redirect_stdout(output):
            response = TestClient(self.make_app()).get(
                "/api/items/Private Lecture Notes.pdf?token=abc", headers={"Origin": "https://tutor.example"})
        self.assertEqual(response.status_code, 200)
        timing = SERVER_TIMING.match(response.headers["server-timing"])
        self.assertIsNotNone(timing, response.headers["server-timing"])
        self.assertEqual((timing["units"], timing["statements"]), ("2", "3"))
        self.assertGreaterEqual(float(timing["app"]), float(timing["db"]))
        self.assertEqual(response.headers["timing-allow-origin"], "https://tutor.example")

        line = next(item for item in output.getvalue().splitlines() if item.startswith("[perf]"))
        self.assertRegex(line, r"^\[perf\] GET /api/items/\{document_id\} status=200 total_ms=[\d.]+ "
                               r"db_units=2 db_statements=3 db_wait_ms=[\d.]+ db_ms=[\d.]+$")
        for secret in ("secret-parameter", "Private", "Lecture", "token", "abc", "SELECT", "INSERT"):
            self.assertNotIn(secret, line)
            self.assertNotIn(secret, response.headers["server-timing"])
        self.assertIsNone(db._request_stats.get())   # the request's context was reset

    def test_unlisted_origin_gets_no_timing_allow_origin(self):
        with without_database_url(), redirect_stdout(io.StringIO()):
            response = TestClient(self.make_app()).get("/api/items/x", headers={"Origin": "https://evil.example"})
        self.assertIn("server-timing", response.headers)
        self.assertNotIn("timing-allow-origin", response.headers)

    def test_unmatched_route_is_logged_without_its_path(self):
        output = io.StringIO()
        with without_database_url(), redirect_stdout(output):
            response = TestClient(self.make_app()).get("/no/such/Private.pdf")
        self.assertEqual(response.status_code, 404)
        line = next(item for item in output.getvalue().splitlines() if item.startswith("[perf]"))
        self.assertIn("GET <unmatched> status=404", line)
        self.assertNotIn("Private", line)

    def test_postgres_pool_wait_and_statements_are_measured_without_credentials(self):
        fake = FakePsycopg(Path(self.temp_dir.name) / "postgres.db", reuse_connections=True)
        output = io.StringIO()
        with fake.installed(SECRET_URL), redirect_stdout(output):
            response = TestClient(self.make_app()).get("/api/items/doc")
            db.close_pools()
        timing = SERVER_TIMING.match(response.headers["server-timing"])
        self.assertEqual((timing["units"], timing["statements"]), ("2", "3"))
        rendered = output.getvalue() + response.headers["server-timing"]
        for part in ("s3cr3t-pw", "alice", "db.private-host.example", "postgresql://"):
            self.assertNotIn(part, rendered)


if __name__ == "__main__":
    unittest.main()
