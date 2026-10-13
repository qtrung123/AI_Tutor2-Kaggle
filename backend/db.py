"""Shared database connection layer.

DATABASE_URL (postgresql://... or postgres://...) selects PostgreSQL. Without it, callers keep using
the local SQLite file they pass in (AI_TUTOR_DATABASE_PATH by default), exactly as before.

SQL is written once in SQLite style (``?`` placeholders); on PostgreSQL it is rewritten to psycopg's
``%s`` style by a small scanner that leaves quoted text and comments alone. Each ``connect()`` opens
a fresh connection, commits on success, rolls back on failure and always closes it, so no global
connection can go stale.

Only auth_store uses this module so far; the other stores still open SQLite directly.
DATABASE_URL and driver error messages (which can name the host and user) are never printed.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


POSTGRES = "postgres"
SQLITE = "sqlite"
_POSTGRES_SCHEMES = {"postgres", "postgresql"}
_POSTGRES_CONNECT_TIMEOUT_SECONDS = 10

_announced: set[str] = set()
_announce_lock = threading.Lock()


class DatabaseError(Exception):
    """Base class for errors raised by this module."""


class DatabaseConfigurationError(DatabaseError):
    """DATABASE_URL is unusable, or the driver it needs is missing."""


class DatabaseConnectionError(DatabaseError):
    """The configured database could not be reached."""


class IntegrityError(DatabaseError):
    """A constraint (unique, foreign key, check, not null) rejected a write, on either backend."""


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


def to_pyformat(sql: str) -> str:
    """Rewrite SQLite-style SQL for psycopg: ``?`` -> ``%s`` and ``%`` -> ``%%``.

    ``?`` inside single-quoted strings, double-quoted identifiers and comments is left as is.
    psycopg treats every ``%`` in a parameterized query as a placeholder marker (even inside
    literals), so all of them are escaped. Dollar-quoted strings and PostgreSQL's jsonb ``?``
    operators are not supported -- shared SQL must stay plain SQLite-compatible SQL.
    """
    out: list[str] = []
    index, length = 0, len(sql)
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
            out.append(sql[index:end + 1].replace("%", "%%"))
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            out.append(sql[index:end].replace("%", "%%"))
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unterminated comment in SQL.")
            out.append(sql[index:end + 2].replace("%", "%%"))
            index = end + 2
        elif char == "?":
            out.append("%s")
            index += 1
        elif char == "%":
            out.append("%%")
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


class Connection:
    """Thin wrapper giving both backends the same execute()/executescript() surface.

    Rows support ``row["column"]`` on both backends (sqlite3.Row / psycopg dict_row).
    """

    def __init__(self, raw: Any, backend: str, integrity_errors: tuple[type[BaseException], ...]):
        self._raw = raw
        self.backend = backend
        self._integrity_errors = integrity_errors

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.backend == POSTGRES:
            sql = to_pyformat(sql)
        try:
            return self._raw.execute(sql, tuple(params))
        except self._integrity_errors as error:
            raise IntegrityError("Database constraint violated.") from error

    def executescript(self, sql: str) -> None:
        """Run several parameterless statements (schema DDL) inside the current transaction."""
        try:
            if self.backend == POSTGRES:
                # Without parameters psycopg sends the text as-is and allows several statements.
                self._raw.execute(sql)
            else:
                self._raw.executescript(sql)
        except self._integrity_errors as error:
            raise IntegrityError("Database constraint violated.") from error


def _announce(message_key: str, message: str) -> None:
    with _announce_lock:
        if message_key in _announced:
            return
        _announced.add(message_key)
    print(message, flush=True)


def _open_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, timeout=15)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 15000")
    return raw


def _open_postgres(url: str) -> tuple[Any, tuple[type[BaseException], ...]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise DatabaseConfigurationError(
            "DATABASE_URL selects PostgreSQL but psycopg is not installed (pip install -r requirements.txt)."
        ) from None
    try:
        raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=_POSTGRES_CONNECT_TIMEOUT_SECONDS)
    except Exception as error:
        # libpq messages can contain the host, port and user: report only the error type.
        raise DatabaseConnectionError(
            f"[db] backend=postgres connection failed ({type(error).__name__})"
        ) from None
    return raw, (psycopg.IntegrityError,)


@contextmanager
def connect(sqlite_path: Path | str, backend: str | None = None) -> Iterator[Connection]:
    """Open the selected database for one unit of work: commit on success, roll back on error, close.

    ``sqlite_path`` is used when SQLite is selected. ``backend`` forces one backend (used by
    auth_store to keep SQLite users rows that not-yet-migrated stores reference).
    """
    selected = backend or selected_backend()
    if selected == POSTGRES:
        raw, integrity_errors = _open_postgres(database_url())
    else:
        raw, integrity_errors = _open_sqlite(Path(sqlite_path)), (sqlite3.IntegrityError,)
    if backend is None:
        _announce(selected, "[db] backend=postgres connected" if selected == POSTGRES else "[db] backend=sqlite")
    try:
        yield Connection(raw, selected, integrity_errors)
        raw.commit()
    except BaseException:
        try:
            raw.rollback()
        except Exception:
            pass  # the original error is the one worth surfacing
        raise
    finally:
        raw.close()
