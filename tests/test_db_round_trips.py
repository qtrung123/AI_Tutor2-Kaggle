"""Round-trip budget of the shared database layer on PostgreSQL (PERF-1).

Supabase is remote, so every server round trip costs network latency. These tests pin the
optimizations in backend/db.py and their safety rails: the pool health check is skipped only for a
connection returned healthy moments ago, autocommit units send no BEGIN/COMMIT, an autocommit
connection never goes back to the pool in autocommit, and the per-request session lookup writes
last_seen_at at most once a minute.
"""

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import auth_store, db
from fake_postgres import FakePsycopg, TransactionStatus


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
PASSWORD = "correct-horse-123"


class PostgresRoundTripTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.backing = root / "postgres.db"
        self.fake = FakePsycopg(self.backing, reuse_connections=True)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_announced", set()))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.stack.enter_context(self.fake.installed(SECRET_URL))
        with db.connect("unused.db") as connection:
            connection.executescript("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY);")
        self.fake.round_trips.clear()

    def tearDown(self):
        db.close_pools()  # closes idle pooled connections so the backing file can be removed
        self.stack.close()
        self.temp_dir.cleanup()

    def rows(self) -> list[str]:
        raw = sqlite3.connect(self.backing)
        try:
            return [row[0] for row in raw.execute("SELECT id FROM t ORDER BY id")]
        finally:
            raw.close()


class HealthCheckTests(PostgresRoundTripTestBase):
    def test_recently_returned_connection_skips_the_server_health_check(self):
        with db.connect("unused.db") as connection:
            connection.execute("SELECT id FROM t").fetchall()
        self.assertNotIn("check", self.fake.round_trips)
        self.assertEqual(len(self.fake.pools[0].open_connections), 1)  # the same connection was reused

    def test_connection_idle_longer_than_the_window_is_checked(self):
        idle = self.fake.pools[0].idle[-1]
        setattr(idle, db._RELEASED_AT, db.time.monotonic() - db._RECHECK_IDLE_SECONDS - 1)
        with db.connect("unused.db") as connection:
            connection.execute("SELECT id FROM t").fetchall()
        self.assertEqual(self.fake.round_trips[0], "check")

    def test_a_fresh_connection_is_always_checked(self):
        while self.fake.pools[0].idle:
            self.fake.pools[0].idle.pop().close()
        with db.connect("unused.db") as connection:
            connection.execute("SELECT id FROM t").fetchall()
        self.assertEqual(self.fake.round_trips[0], "check")

    def test_suspect_connections_are_checked_even_when_recent(self):
        def raw(**state):
            values = {"closed": False, "broken": False, "status": TransactionStatus.IDLE, **state}
            return SimpleNamespace(closed=values["closed"], broken=values["broken"],
                                   info=SimpleNamespace(transaction_status=values["status"]),
                                   **{db._RELEASED_AT: db.time.monotonic()})

        with patch.dict("sys.modules", {"psycopg.pq": self.fake.pq}):
            self.assertTrue(db._released_recently(raw()))
            self.assertFalse(db._released_recently(raw(broken=True)))
            self.assertFalse(db._released_recently(raw(closed=True)))
            self.assertFalse(db._released_recently(raw(status=TransactionStatus.INERROR)))
            self.assertFalse(db._released_recently(SimpleNamespace()))  # never returned before


class AutocommitTests(PostgresRoundTripTestBase):
    def test_default_unit_is_one_transaction(self):
        with db.connect("unused.db") as connection:
            connection.execute("SELECT id FROM t").fetchall()
        self.assertEqual(self.fake.round_trips, ["BEGIN", "statement", "COMMIT"])

    def test_autocommit_read_sends_only_the_statement(self):
        with db.connect("unused.db", autocommit=True) as connection:
            self.assertTrue(connection.autocommit)
            connection.execute("SELECT id FROM t").fetchall()
        self.assertEqual(self.fake.round_trips, ["statement"])

    def test_connection_goes_back_to_the_pool_transactional(self):
        with db.connect("unused.db", autocommit=True) as connection:
            connection.execute("INSERT INTO t (id) VALUES (?)", ("kept",))
        raw = self.fake.pools[0].idle[-1]
        self.assertFalse(raw.autocommit)
        # The next default unit on that same connection is still atomic.
        with self.assertRaises(db.IntegrityError):
            with db.connect("unused.db") as connection:
                connection.execute("INSERT INTO t (id) VALUES (?)", ("rolled-back",))
                connection.execute("INSERT INTO t (id) VALUES (?)", ("kept",))
        self.assertEqual(self.rows(), ["kept"])
        self.assertEqual(len(self.fake.pools[0].open_connections), 1)

    def test_connection_that_cannot_leave_autocommit_is_discarded(self):
        connection = db.connect("unused.db", autocommit=True)
        raw = connection._raw
        raw.execute("BEGIN")  # a stray manual BEGIN leaves the connection mid-transaction
        raw._in_transaction = True
        connection.close()
        self.assertTrue(raw.closed)
        self.assertNotIn(raw, self.fake.pools[0].idle)

    def test_begin_immediate_is_refused_in_autocommit_on_both_backends(self):
        with db.connect("unused.db", autocommit=True) as connection:
            with self.assertRaises(db.DatabaseError):
                connection.begin_immediate()
        environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        with patch.dict(os.environ, environment, clear=True):
            with db.connect(Path(self.temp_dir.name) / "local.db", autocommit=True) as connection:
                self.assertEqual(connection.backend, db.SQLITE)
                with self.assertRaises(db.DatabaseError):
                    connection.begin_immediate()


class SessionLookupTests(PostgresRoundTripTestBase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(auth_store, "DATABASE_PATH", Path(self.temp_dir.name) / "local.db"))
        self.user = auth_store.create_user("Ada", "ada@example.com", PASSWORD)
        self.token, _session = auth_store.create_session(self.user["id"])
        self.other = auth_store.create_user("Bob", "bob@example.com", PASSWORD)
        self.other_token, _session = auth_store.create_session(self.other["id"])

    def last_seen(self, token: str) -> str | None:
        raw = sqlite3.connect(self.backing)
        try:
            token_hash = auth_store.hashlib.sha256(token.encode("utf-8")).hexdigest()
            return raw.execute("SELECT last_seen_at FROM auth_sessions WHERE token_hash = ?", (token_hash,)).fetchone()[0]
        finally:
            raw.close()

    def test_steady_state_lookup_is_one_round_trip_and_writes_nothing(self):
        self.assertEqual(auth_store.get_user_for_session(self.token), self.user)  # first touch
        first_seen = self.last_seen(self.token)
        self.assertIsNotNone(first_seen)
        self.fake.round_trips.clear()
        self.fake.statements.clear()
        self.assertEqual(auth_store.get_user_for_session(self.token), self.user)
        self.assertEqual(self.fake.round_trips, ["statement"])
        self.assertFalse(any(statement.startswith("UPDATE") for statement in self.fake.statements))
        self.assertEqual(self.last_seen(self.token), first_seen)

    def test_last_seen_is_refreshed_once_the_interval_has_passed(self):
        auth_store.get_user_for_session(self.token)
        first_seen = self.last_seen(self.token)
        later = auth_store.utc_now() + timedelta(seconds=61)
        with patch.object(auth_store, "utc_now", return_value=later):
            self.assertEqual(auth_store.get_user_for_session(self.token), self.user)
        self.assertEqual(self.last_seen(self.token), later.isoformat())
        self.assertNotEqual(first_seen, later.isoformat())

    def test_revocation_and_user_isolation_still_apply_immediately(self):
        self.assertEqual(auth_store.get_user_for_session(self.token)["id"], self.user["id"])
        self.assertEqual(auth_store.get_user_for_session(self.other_token)["id"], self.other["id"])
        auth_store.revoke_session(self.token)
        self.assertIsNone(auth_store.get_user_for_session(self.token))
        self.assertEqual(auth_store.get_user_for_session(self.other_token)["id"], self.other["id"])

    def test_touch_interval_parsing(self):
        now = auth_store.utc_now()
        self.assertTrue(auth_store._session_touch_due(None, now))
        self.assertTrue(auth_store._session_touch_due("not-a-timestamp", now))
        self.assertFalse(auth_store._session_touch_due((now - timedelta(seconds=5)).isoformat(), now))
        self.assertTrue(auth_store._session_touch_due((now - timedelta(seconds=60)).isoformat(), now))
        self.assertFalse(auth_store._session_touch_due((now - timedelta(seconds=5)).replace(tzinfo=None).isoformat(), now))


if __name__ == "__main__":
    unittest.main()
