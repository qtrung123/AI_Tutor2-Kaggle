"""In-process stand-ins for psycopg and psycopg_pool, backed by a SQLite file.

Lets unit tests drive backend/db.py's PostgreSQL path without a PostgreSQL server. The fake
enforces psycopg's calling conventions -- %s / %(name)s placeholders and %% escapes, with a stray
? or :name rejected, no Python bools as parameters (the schema stores INTEGER 0/1 flags) -- applies
the row factory db.py passes in, and raises its own IntegrityError. It checks wiring and SQL shape,
not PostgreSQL semantics: tests/test_postgres_persistence.py runs against a real server for that.
"""

import re
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


class PoolTimeout(Error):
    pass


def _from_pyformat(query: str, named: bool) -> str:
    """Translate psycopg %s / %(name)s style back to SQLite, rejecting what psycopg would mishandle."""
    out: list[str] = []
    index, quote = 0, None
    while index < len(query):
        char = query[index]
        if char == "%":
            following = query[index + 1:index + 2]
            if following == "%":
                out.append("%")
                index += 2
                continue
            if quote is None and following == "s" and not named:
                out.append("?")
                index += 2
                continue
            match = re.match(r"%\((\w+)\)s", query[index:]) if quote is None and named else None
            if match:
                out.append(f":{match.group(1)}")
                index += len(match.group(0))
                continue
            raise AssertionError(f"Unescaped % in psycopg query: {query!r}")
        if quote is None and char in ("'", '"'):
            quote = char
        elif char == quote:
            quote = None
        elif quote is None and char == "?":
            raise AssertionError(f"SQLite ? placeholder reached psycopg: {query!r}")
        elif quote is None and char == ":" and re.match(r":[A-Za-z_]", query[index:]) and query[index - 1:index] != ":":
            raise AssertionError(f"SQLite :name placeholder reached psycopg: {query!r}")
        out.append(char)
        index += 1
    return "".join(out)


def _check_params(params) -> None:
    values = params.values() if isinstance(params, dict) else params
    for value in values:
        if isinstance(value, bool):
            raise AssertionError("Python bool sent to PostgreSQL; the schema stores INTEGER 0/1 flags.")


class _Column:
    def __init__(self, name: str):
        self.name = name


class FakeCursor:
    def __init__(self, sqlite_cursor, row_factory):
        self._cursor = sqlite_cursor
        self.rowcount = sqlite_cursor.rowcount
        self.description = [_Column(item[0]) for item in sqlite_cursor.description or ()] or None
        self._make_row = row_factory(self) if row_factory and self.description else (lambda values: values)

    def fetchone(self):
        values = self._cursor.fetchone()
        return None if values is None else self._make_row(values)

    def fetchall(self):
        return [self._make_row(values) for values in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    def executemany(self, query, seq_of_params):
        raise NotImplementedError  # replaced per call by FakePostgresConnection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Adapters:
    def __init__(self):
        self.loaders: dict[str, object] = {}

    def register_loader(self, type_name, loader):
        self.loaders[type_name] = loader


class FakePostgresConnection:
    def __init__(self, backing_path: Path, statements: list[str], row_factory):
        self._db = sqlite3.connect(backing_path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.create_function("pg_advisory_xact_lock", 1, lambda key: None)
        self._statements = statements
        self._row_factory = row_factory
        self.adapters = _Adapters()
        self.closed = False

    def _run(self, query, params):
        self._statements.append(query)
        try:
            if params is None:
                # psycopg sends parameterless text as-is; several statements are allowed.
                # SQLite lacks PostgreSQL's ADD COLUMN IF NOT EXISTS; callers check existence first.
                self._db.executescript(query.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN"))
                return FakeCursor(self._db.execute("SELECT 1 WHERE 0"), None)
            _check_params(params)
            named = isinstance(params, dict)
            return FakeCursor(self._db.execute(_from_pyformat(query, named), params), self._row_factory)
        except sqlite3.IntegrityError as error:
            raise IntegrityError(str(error)) from error

    def execute(self, query, params=None):
        return self._run(query, params)

    def cursor(self):
        connection = self
        cursor = FakeCursor(self._db.execute("SELECT 1 WHERE 0"), None)

        def executemany(query, seq_of_params):
            for params in seq_of_params:
                connection._run(query, params)

        cursor.executemany = executemany
        return cursor

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()

    def close(self):
        self.closed = True
        self._db.close()


class FakePsycopg:
    """The fake modules plus what tests inspect: executed statements, connect() and pool calls."""

    def __init__(self, backing_path: Path, connect_error: Exception | None = None):
        self.backing_path = Path(backing_path)
        self.connect_error = connect_error
        self.statements: list[str] = []
        self.connect_calls: list[dict] = []
        self.pools: list["FakePsycopg._Pool"] = []

        fake = self
        self.module = types.ModuleType("psycopg")
        self.module.Error = Error
        self.module.IntegrityError = IntegrityError
        self.module.OperationalError = OperationalError
        self.module.connect = self._connect
        self.types = types.ModuleType("psycopg.types")
        self.numeric = types.ModuleType("psycopg.types.numeric")
        self.numeric.FloatLoader = object()
        self.module.types = self.types
        self.types.numeric = self.numeric

        class _Pool:
            check_connection = staticmethod(lambda connection: None)

            def __init__(self, conninfo, kwargs=None, configure=None, **options):
                self.conninfo, self.kwargs, self.configure, self.options = conninfo, kwargs or {}, configure, options
                self.open_connections: list[FakePostgresConnection] = []
                self.closed = False
                fake.pools.append(self)

            def open(self, wait=False, timeout=None):
                if fake.connect_error is not None:
                    raise PoolTimeout("pool initialization incomplete after 20.0 sec")

            def getconn(self, timeout=None):
                connection = fake._connect(self.conninfo, **self.kwargs)
                if self.configure:
                    self.configure(connection)
                self.open_connections.append(connection)
                return connection

            def putconn(self, connection):
                connection.rollback()
                connection.close()

            def close(self):
                self.closed = True

        self.pool_module = types.ModuleType("psycopg_pool")
        self.pool_module.ConnectionPool = _Pool
        self.pool_module.PoolTimeout = PoolTimeout

    def _connect(self, conninfo, **kwargs):
        self.connect_calls.append({"conninfo": conninfo, **kwargs})
        if self.connect_error is not None:
            raise self.connect_error
        return FakePostgresConnection(self.backing_path, self.statements, kwargs.get("row_factory"))

    @contextmanager
    def installed(self, database_url: str):
        from backend import db

        modules = {"psycopg": self.module, "psycopg.types": self.types,
                   "psycopg.types.numeric": self.numeric, "psycopg_pool": self.pool_module}
        with patch.dict(sys.modules, modules), patch.dict("os.environ", {"DATABASE_URL": database_url}), \
                patch.object(db, "_pools", {}), patch.object(db, "_schema_ready", set()):
            yield self
