"""Shared database layer for every relational store.

DATABASE_URL (postgresql://... or postgres://...) selects PostgreSQL. Without it, each store keeps
using the local SQLite file it passes in (AI_TUTOR_DATABASE_PATH by default), exactly as before.

Stores keep writing SQLite-flavoured SQL; on PostgreSQL this module adapts it:
- ``?`` and ``:name`` placeholders become psycopg's ``%s`` / ``%(name)s``; quoted text and
  comments are left alone and every literal ``%`` is escaped;
- Python bools are sent as 0/1, because the schema keeps SQLite's INTEGER 0/1 flags;
- rows behave like sqlite3.Row (index or case-insensitive name, keys(), dict(row));
- NUMERIC results (AVG, ROUND, 100.0 * x) come back as float, as they do from SQLite;
- executescript() and add_column() map AUTOINCREMENT and REAL to PostgreSQL types.

``connect()`` returns a connection that is also a context manager with sqlite3's contract: commit
on success, roll back on error, then close. PostgreSQL connections come from a small pool that
health-checks a connection before handing it out, so nothing stale is reused. Schema DDL runs once
per process (ensure_schema) under an advisory lock. DATABASE_URL and driver connection errors
(which can name the host and user) are never printed.
"""

import atexit
import logging
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any


POSTGRES = "postgres"
SQLITE = "sqlite"
_POSTGRES_SCHEMES = {"postgres", "postgresql"}
_POSTGRES_CONNECT_TIMEOUT_SECONDS = 10
_POOL_MAX_SIZE = max(1, int(os.getenv("AI_TUTOR_DB_POOL_MAX_SIZE", "5")))
# Transaction-scoped advisory lock keys (arbitrary constants, unique to this app).
_SCHEMA_LOCK_KEY = 7241001
_WRITE_LOCK_KEY = 7241002

_announced: set[str] = set()
_announce_lock = threading.Lock()
_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()
_schema_ready: set[tuple[str, str]] = set()
_schema_lock = threading.RLock()


class DatabaseError(Exception):
    """Base class for errors raised by this module."""


class DatabaseConfigurationError(DatabaseError):
    """DATABASE_URL is unusable, or the driver it needs is missing."""


class DatabaseConnectionError(DatabaseError):
    """The configured database could not be reached."""


class IntegrityError(DatabaseError):
    """A constraint (unique, foreign key, check, not null) rejected a write, on either backend."""


class _SQLiteIntegrityError(IntegrityError, sqlite3.IntegrityError):
    """On SQLite the shared error is also a sqlite3.IntegrityError, so existing handlers still match."""


def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def selected_backend() -> str:
    """POSTGRES when DATABASE_URL is set, SQLITE otherwise. Never echoes the URL."""
    url = database_url()
    if not url:
        return SQLITE
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme in _POSTGRES_SCHEMES:
        return POSTGRES
    raise DatabaseConfigurationError("DATABASE_URL must start with postgresql:// or postgres://")


# -- SQL rewriting ----------------------------------------------------------------------------

def _segments(sql: str) -> Iterable[tuple[bool, str]]:
    """Split SQL into (is_code, text) pieces; quoted text and comments are not code."""
    index, start, length = 0, 0, len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"'):
            end = index + 1
            while True:
                end = sql.find(char, end)
                if end == -1:
                    raise ValueError("Unterminated quoted text in SQL.")
                if end + 1 < length and sql[end + 1] == char:  # '' or "" escape
                    end += 2
                    continue
                break
            end += 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unterminated comment in SQL.")
            end += 2
        else:
            index += 1
            continue
        if start < index:
            yield True, sql[start:index]
        yield False, sql[index:end]
        index = start = end
    if start < length:
        yield True, sql[start:]


_NAMED_PARAMETER = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def to_pyformat(sql: str, named: bool = False) -> str:
    """Rewrite SQLite-style SQL for psycopg.

    ``?`` -> ``%s`` (or, with ``named``, ``:name`` -> ``%(name)s``) outside quoted text and
    comments; every ``%`` is escaped as ``%%`` because psycopg reads it as a placeholder marker
    even inside literals. Dollar-quoted strings and PostgreSQL's jsonb ``?`` operators are not
    supported: shared SQL must stay plain SQLite-compatible SQL.
    """
    out = []
    for is_code, text in _segments(sql):
        text = text.replace("%", "%%")
        if is_code:
            text = _NAMED_PARAMETER.sub(r"%(\1)s", text) if named else text.replace("?", "%s")
        out.append(text)
    return "".join(out)


_DDL_REWRITES = (
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b"), "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    # PostgreSQL REAL is 4-byte; SQLite REAL is an 8-byte double.
    (re.compile(r"\bREAL\b"), "DOUBLE PRECISION"),
)


def postgres_ddl(sql: str) -> str:
    """Map SQLite column types in DDL to their PostgreSQL equivalents (outside quoted text)."""
    out = []
    for is_code, text in _segments(sql):
        if is_code:
            if re.search(r"\bCOLLATE\s+NOCASE\b", text, re.IGNORECASE):
                raise ValueError("COLLATE NOCASE has no PostgreSQL equivalent; use a lower() index.")
            for pattern, replacement in _DDL_REWRITES:
                text = pattern.sub(replacement, text)
        out.append(text)
    return "".join(out)


# -- rows and parameters ----------------------------------------------------------------------

class Row:
    """A result row with sqlite3.Row's interface, so stores read both backends the same way."""

    __slots__ = ("_keys", "_values", "_positions")

    def __init__(self, keys: Iterable[str], values: Iterable[Any]):
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._positions: dict[str, int] = {}
        for position, key in enumerate(self._keys):
            self._positions.setdefault(key.lower(), position)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._positions[key.lower()]]
        except KeyError:
            raise IndexError("No item with that key") from None

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other) -> bool:
        return isinstance(other, Row) and self._keys == other._keys and self._values == other._values

    def __hash__(self) -> int:
        return hash((self._keys, self._values))

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._keys, self._values))!r})"


def _row_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: Row(names, values)


def _adapt_value(value):
    return int(value) if isinstance(value, bool) else value


def _adapt_params(params):
    if isinstance(params, Mapping):
        return {key: _adapt_value(value) for key, value in params.items()}
    return tuple(_adapt_value(value) for value in params)


# -- connections ------------------------------------------------------------------------------

class Connection:
    """One unit of database work. Use ``with connect(path) as connection:`` (commit on success,
    roll back on error, close), or call commit()/rollback()/close() explicitly."""

    def __init__(self, raw: Any, backend: str, integrity_errors: tuple[type[BaseException], ...],
                 release: Callable[[Any], None]):
        self._raw = raw
        self.backend = backend
        self._integrity_errors = integrity_errors
        self._integrity_error = _SQLiteIntegrityError if backend == SQLITE else IntegrityError
        self._release = release
        self._closed = False
        self._columns: dict[str, set[str]] = {}  # table_columns() cache for this unit of work

    def _sql(self, sql: str, params) -> str:
        if self.backend != POSTGRES:
            return sql
        return to_pyformat(sql, named=isinstance(params, Mapping))

    def execute(self, sql: str, params: Any = ()) -> Any:
        try:
            if self.backend == POSTGRES:
                return self._raw.execute(self._sql(sql, params), _adapt_params(params))
            return self._raw.execute(sql, params)
        except self._integrity_errors as error:
            raise self._integrity_error("Database constraint violated.") from error

    def executemany(self, sql: str, seq_of_params: Iterable[Any]) -> None:
        rows = list(seq_of_params)
        try:
            if self.backend == POSTGRES:
                if rows:
                    with self._raw.cursor() as cursor:
                        cursor.executemany(self._sql(sql, rows[0]), [_adapt_params(row) for row in rows])
            else:
                self._raw.executemany(sql, rows)
        except self._integrity_errors as error:
            raise self._integrity_error("Database constraint violated.") from error

    def executescript(self, sql: str) -> None:
        """Run several parameterless DDL statements. On SQLite this commits first (sqlite3's own
        behaviour); on PostgreSQL it runs inside the current transaction."""
        try:
            if self.backend == POSTGRES:
                # Without parameters psycopg sends the text as-is and allows several statements.
                self._raw.execute(postgres_ddl(sql))
            else:
                self._raw.executescript(sql)
        except self._integrity_errors as error:
            raise self._integrity_error("Database constraint violated.") from error

    def begin_immediate(self) -> None:
        """Start a write transaction that concurrent writers must wait for.

        SQLite: BEGIN IMMEDIATE takes the database-wide write lock. PostgreSQL: a transaction-scoped
        advisory lock gives the same serialization; it is released at commit or rollback."""
        if self.backend == POSTGRES:
            self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
        else:
            self._raw.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release(self._raw)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                try:
                    self.rollback()
                except Exception:
                    pass  # the original error is the one worth surfacing
        finally:
            self.close()
        return False


def _announce(key: str, message: str) -> None:
    with _announce_lock:
        if key in _announced:
            return
        _announced.add(key)
    print(message, flush=True)


def _sqlite_connection(path: Path) -> Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, timeout=15)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 15000")
    return Connection(raw, SQLITE, (sqlite3.IntegrityError,), lambda connection: connection.close())


_SQLSTATE_HINTS = {
    "28P01": "authentication failed: check the user and password",
    "28000": "authentication rejected: check the user and pg_hba/SSL settings",
    "3D000": "database does not exist",
    "53300": "too many connections",
    "57P03": "server is starting up or shutting down",
}
# libpq connection errors carry no SQLSTATE; these fixed phrases map them to a category. Only the
# category is ever printed -- never the message, which names the host and user.
_MESSAGE_HINTS = (
    ("password authentication failed", _SQLSTATE_HINTS["28P01"]),
    ("database \"", _SQLSTATE_HINTS["3D000"]),
    ("too many", _SQLSTATE_HINTS["53300"]),
    ("ssl", "SSL negotiation failed: check sslmode"),
    ("tenant or user not found", "pooler rejected the user: check the pooler user name (user.project-ref)"),
    ("fatal", "server rejected the connection: check user, database and SSL settings"),
)


def _connection_failed(error: BaseException, detail: str = "") -> DatabaseConnectionError:
    # libpq messages can contain the host, port and user: report only the error type and a category.
    return DatabaseConnectionError(f"[db] backend=postgres connection failed ({type(error).__name__}{detail})")


def _diagnose_connect_failure(url: str) -> str:
    """One direct connection attempt, reduced to a SQLSTATE/category hint (never the message)."""
    import psycopg

    try:
        psycopg.connect(url, connect_timeout=_POSTGRES_CONNECT_TIMEOUT_SECONDS).close()
    except Exception as error:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate:
            return f"; sqlstate={sqlstate}" + (f": {_SQLSTATE_HINTS[sqlstate]}" if sqlstate in _SQLSTATE_HINTS else "")
        message = str(error).lower()
        if "does not exist" in message and "database" not in message:
            return "; server rejected the connection: check user, database and SSL settings"
        for phrase, hint in _MESSAGE_HINTS:
            if phrase in message:
                return f"; {hint}"
        return "; server unreachable: check host, port, network/IPv4 access and sslmode"
    return ""


def _configure_postgres_connection(raw) -> None:
    from psycopg.types.numeric import FloatLoader

    raw.adapters.register_loader("numeric", FloatLoader)


def _create_pool(url: str):
    try:
        import psycopg  # noqa: F401  (imported for the clear error below)
        from psycopg_pool import ConnectionPool
    except ImportError:
        raise DatabaseConfigurationError(
            "DATABASE_URL selects PostgreSQL but psycopg/psycopg-pool are not installed "
            "(pip install -r requirements.txt)."
        ) from None
    # The pool logs failed connection attempts with libpq's message (host, user); keep it quiet.
    driver_logger = logging.getLogger("psycopg")
    driver_logger.propagate = False
    if not driver_logger.handlers:
        driver_logger.addHandler(logging.NullHandler())
    pool = ConnectionPool(
        url,
        kwargs={
            "row_factory": _row_factory,
            "connect_timeout": _POSTGRES_CONNECT_TIMEOUT_SECONDS,
            # Server-side prepared statements break behind transaction poolers (Supabase/PgBouncer).
            "prepare_threshold": None,
        },
        configure=_configure_postgres_connection,
        check=ConnectionPool.check_connection,
        min_size=1,
        max_size=_POOL_MAX_SIZE,
        timeout=15,
        max_idle=300,
        max_lifetime=1800,
        open=False,
        name="ai-tutor-db",
    )
    try:
        pool.open(wait=True, timeout=20)
    except Exception as error:
        try:
            pool.close()
        except Exception:
            pass
        try:
            detail = _diagnose_connect_failure(url)
        except Exception:
            detail = ""
        raise _connection_failed(error, detail) from None
    return pool


def _postgres_connection(url: str) -> Connection:
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            pool = _pools[url] = _create_pool(url)
    try:
        raw = pool.getconn()
    except Exception as error:
        raise _connection_failed(error) from None

    import psycopg

    return Connection(raw, POSTGRES, (psycopg.IntegrityError,), pool.putconn)


def connect(sqlite_path: Path | str, backend: str | None = None) -> Connection:
    """Open the selected database. ``sqlite_path`` is used when SQLite is selected; ``backend``
    forces one backend."""
    selected = backend or selected_backend()
    if selected == POSTGRES:
        connection = _postgres_connection(database_url())
    else:
        connection = _sqlite_connection(Path(sqlite_path))
    if backend is None:
        _announce(selected, "[db] backend=postgres connected" if selected == POSTGRES else "[db] backend=sqlite")
    return connection


def close_pools() -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


atexit.register(close_pools)


# -- schema helpers ---------------------------------------------------------------------------

def ensure_schema(name: str, sqlite_path: Path | str, initializer: Callable[[Connection], None]) -> None:
    """Run a store's idempotent schema initializer.

    SQLite: every call, exactly as the stores always did (each test uses a fresh file).
    PostgreSQL: once per process per database, in one transaction holding an advisory lock so
    two processes starting together never race on DDL. Never drops or rebuilds tables.
    """
    if selected_backend() != POSTGRES:
        with connect(sqlite_path) as connection:
            initializer(connection)
        return
    key = (name, database_url())
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        with connect(sqlite_path) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(?)", (_SCHEMA_LOCK_KEY,))
            initializer(connection)
        _schema_ready.add(key)


def table_columns(connection: Connection, table: str) -> set[str]:
    """Column names of ``table``, read once per connection (one round trip per table, not per column)."""
    if table not in connection._columns:
        if connection.backend == POSTGRES:
            rows = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            ).fetchall()
            connection._columns[table] = {row[0] for row in rows}
        else:
            connection._columns[table] = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    return set(connection._columns[table])


def add_column(connection: Connection, table: str, column: str, definition: str) -> bool:
    """Additive migration: add ``column`` if missing. Returns True when it was added.

    Safe when two requests race on a fresh database: PostgreSQL uses ADD COLUMN IF NOT EXISTS;
    on SQLite a concurrent winner's 'duplicate column' error is swallowed."""
    if column in table_columns(connection, table):
        return False
    if table in connection._columns:
        connection._columns[table].add(column)
    if connection.backend == POSTGRES:
        connection.executescript(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        return True
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as error:
        if "duplicate column name" not in str(error):
            raise
        return False
    return True


def verify_connection() -> str:
    """Open the selected database once and run a trivial query. Used by deployment scripts."""
    from config import DATABASE_PATH

    with connect(DATABASE_PATH) as connection:
        connection.execute("SELECT 1").fetchone()
        return connection.backend


if __name__ == "__main__":
    # python -m backend.db  ->  prints "[db] backend=..." and exits 0, or a sanitized error and 1.
    try:
        verify_connection()
    except DatabaseError as error:
        print(str(error) if str(error).startswith("[db]") else f"[db] {error}", file=sys.stderr, flush=True)
        sys.exit(1)
