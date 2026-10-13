"""In-process stand-in for the psycopg module, backed by a SQLite file.

Lets unit tests drive backend/db.py's PostgreSQL path without a PostgreSQL server. It enforces
psycopg's parameter style -- %s placeholders and %% escapes, with a stray ? or bare % rejected --
returns dict rows, and raises its own IntegrityError. It does not reproduce PostgreSQL semantics
beyond what the shared SQL needs, so it checks wiring and SQL shape, not PostgreSQL itself.
"""

import sqlite3
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


class Error(Exception):
    pass


class IntegrityError(Error):
    pass


class OperationalError(Error):
    pass


def _from_pyformat(query: str) -> str:
    """Translate psycopg %s/%% style back to SQLite, rejecting what real psycopg would mishandle."""
    out: list[str] = []
    index, quote = 0, None
    while index < len(query):
        char = query[index]
        if char == "%":
            following = query[index + 1:index + 2]
            if following == "%":
                out.append("%")
            elif following == "s" and quote is None:
                out.append("?")
            else:
                raise AssertionError(f"Unescaped % in psycopg query: {query!r}")
            index += 2
            continue
        if quote is None and char in ("'", '"'):
            quote = char
        elif char == quote:
            quote = None
        elif char == "?" and quote is None:
            raise AssertionError(f"SQLite ? placeholder reached psycopg: {query!r}")
        out.append(char)
        index += 1
    return "".join(out)


def _dict_row(cursor, row):
    return {column[0]: value for column, value in zip(cursor.description, row)}


class FakePostgresConnection:
    def __init__(self, backing_path: Path, statements: list[str]):
        self._db = sqlite3.connect(backing_path)
        self._db.row_factory = _dict_row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.create_function("pg_advisory_xact_lock", 1, lambda key: None)
        self._statements = statements
        self.closed = False

    def execute(self, query, params=None):
        self._statements.append(query)
        try:
            if params is None:
                # psycopg sends parameterless text as-is; several statements are allowed.
                self._db.executescript(query)
                return self._db.cursor()
            return self._db.execute(_from_pyformat(query), tuple(params))
        except sqlite3.IntegrityError as error:
            raise IntegrityError(str(error)) from error

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()

    def close(self):
        self.closed = True
        self._db.close()


class FakePsycopg:
    """Holds the fake module plus what tests inspect: executed statements and connect() calls."""

    def __init__(self, backing_path: Path, connect_error: Exception | None = None):
        self.backing_path = Path(backing_path)
        self.connect_error = connect_error
        self.statements: list[str] = []
        self.connect_calls: list[dict] = []
        self.module = types.ModuleType("psycopg")
        self.module.Error = Error
        self.module.IntegrityError = IntegrityError
        self.module.OperationalError = OperationalError
        self.module.connect = self._connect
        self.rows = types.ModuleType("psycopg.rows")
        self.rows.dict_row = object()
        self.module.rows = self.rows

    def _connect(self, conninfo, **kwargs):
        self.connect_calls.append({"conninfo": conninfo, **kwargs})
        if self.connect_error is not None:
            raise self.connect_error
        return FakePostgresConnection(self.backing_path, self.statements)

    @contextmanager
    def installed(self, database_url: str):
        with patch.dict(sys.modules, {"psycopg": self.module, "psycopg.rows": self.rows}), \
                patch.dict("os.environ", {"DATABASE_URL": database_url}):
            yield self
