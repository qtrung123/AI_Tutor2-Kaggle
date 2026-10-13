import hashlib
import secrets
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from backend import db
from config import ADMIN_EMAILS, AUTH_SESSION_DAYS, DATABASE_PATH, LEGACY_USER_EMAIL


LEGACY_USER_ID = "local_student"
_password_hasher = PasswordHasher()
_dummy_hash = _password_hasher.hash("not-a-real-user-password")
# Never a valid argon2 hash, so a SQLite placeholder row for a PostgreSQL user can't log in.
_LOCAL_PLACEHOLDER_PASSWORD_HASH = "!stored-in-postgres"
_postgres_schema_ready = False
_postgres_schema_lock = threading.Lock()
_mirrored_users: set[tuple[str, str]] = set()

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at);
"""

# Timestamps stay ISO-8601 UTC TEXT, as in SQLite: every value is written by utc_now().isoformat(),
# so string comparisons (expires_at > now) and API payloads behave identically on both backends.
# PostgreSQL has no COLLATE NOCASE; case-insensitive email uniqueness is an index on lower(email).
# The advisory lock serializes schema creation if two processes start at once.
_POSTGRES_SCHEMA = """
SELECT pg_advisory_xact_lock(7241001);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (lower(email));
CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at);
"""


def _connect():
    """Auth tables live in PostgreSQL when DATABASE_URL is set, otherwise in the SQLite file."""
    return db.connect(DATABASE_PATH)


def _connect_local_sqlite():
    return db.connect(DATABASE_PATH, backend=db.SQLITE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_user(row: Mapping) -> dict:
    return {key: row[key] for key in ("id", "display_name", "email", "created_at")}


def _ensure_legacy_user(connection: db.Connection) -> None:
    legacy = connection.execute("SELECT 1 FROM users WHERE id = ?", (LEGACY_USER_ID,)).fetchone()
    if not legacy:
        # The legacy account is intentionally locked until an operator explicitly sets credentials.
        connection.execute(
            "INSERT INTO users (id, display_name, email, password_hash, created_at, disabled_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                LEGACY_USER_ID,
                "Legacy Local User",
                LEGACY_USER_EMAIL.strip().lower(),
                _password_hasher.hash(secrets.token_urlsafe(48)),
                utc_now().isoformat(),
                utc_now().isoformat(),
            ),
        )


def _initialize_postgres_schema() -> None:
    """Create the PostgreSQL auth schema once per process; never drops or rebuilds tables."""
    global _postgres_schema_ready
    if _postgres_schema_ready:
        return
    with _postgres_schema_lock:
        if _postgres_schema_ready:
            return
        with _connect() as connection:
            connection.executescript(_POSTGRES_SCHEMA)
            _ensure_legacy_user(connection)
        _postgres_schema_ready = True


def initialize_auth_store() -> None:
    # The SQLite users table is always kept: stores not yet moved to PostgreSQL declare
    # FOREIGN KEY (owner_id) REFERENCES users(id) against it in the same SQLite file.
    if db.selected_backend() == db.POSTGRES:
        _initialize_postgres_schema()
        local = _connect_local_sqlite()
    else:
        local = _connect()
    with local as connection:
        connection.executescript(_SQLITE_SCHEMA)
        _ensure_legacy_user(connection)


def _mirror_user_locally(user: dict) -> None:
    """Give a PostgreSQL user a credential-less SQLite users row so local foreign keys hold.

    Needed only until every store has moved to PostgreSQL. The placeholder email is derived from
    the id, so it never collides with an older SQLite account restored from a previous session.
    """
    if db.selected_backend() != db.POSTGRES:
        return
    key = (str(DATABASE_PATH), user["id"])
    if key in _mirrored_users:
        return
    with _connect_local_sqlite() as connection:
        connection.execute(
            "INSERT INTO users (id, display_name, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                user["id"],
                user["display_name"],
                f"{user['id']}@postgres-account.invalid",
                _LOCAL_PLACEHOLDER_PASSWORD_HASH,
                user["created_at"],
            ),
        )
    _mirrored_users.add(key)


def normalize_email(email: str) -> str:
    return str(email).strip().lower()


def is_admin_email(email: str) -> bool:
    """Admin status is an env-configured allowlist, not a stored user attribute."""
    return normalize_email(email) in ADMIN_EMAILS


def create_user(display_name: str, email: str, password: str) -> dict:
    initialize_auth_store()
    user_id = str(uuid4())
    now = utc_now().isoformat()
    try:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO users (id, display_name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, display_name.strip(), normalize_email(email), _password_hasher.hash(password), now),
            )
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            user = _public_user(row)
    except db.IntegrityError as error:
        raise ValueError("An account with this email already exists.") from error
    _mirror_user_locally(user)
    return user


def authenticate_user(email: str, password: str) -> dict | None:
    initialize_auth_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE lower(email) = ?", (normalize_email(email),)
        ).fetchone()
    candidate_hash = row["password_hash"] if row else _dummy_hash
    try:
        verified = _password_hasher.verify(candidate_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        verified = False
    if not row or not verified or row["disabled_at"]:
        return None
    user = _public_user(row)
    _mirror_user_locally(user)
    return user


def create_session(user_id: str) -> tuple[str, dict]:
    initialize_auth_store()
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = utc_now()
    session = {
        "id": str(uuid4()),
        "user_id": user_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=AUTH_SESSION_DAYS)).isoformat(),
    }
    with _connect() as connection:
        connection.execute(
            "INSERT INTO auth_sessions (id, user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (session["id"], user_id, token_hash, session["created_at"], session["expires_at"]),
        )
    return raw_token, session


def get_user_for_session(raw_token: str | None) -> dict | None:
    if not raw_token:
        return None
    initialize_auth_store()
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = utc_now().isoformat()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.disabled_at IS NULL
            """,
            (token_hash, now),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash)
            )
    if not row:
        return None
    user = _public_user(row)
    _mirror_user_locally(user)
    return user


def revoke_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with _connect() as connection:
        connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now().isoformat(), token_hash),
        )


def claim_legacy_user(display_name: str, email: str, password: str) -> dict:
    """Explicit operator-only credential setup for preserved pre-authentication data."""
    initialize_auth_store()
    try:
        with _connect() as connection:
            connection.execute(
                """
                UPDATE users SET display_name = ?, email = ?, password_hash = ?, disabled_at = NULL
                WHERE id = ?
                """,
                (display_name.strip(), normalize_email(email), _password_hasher.hash(password), LEGACY_USER_ID),
            )
            row = connection.execute("SELECT * FROM users WHERE id = ?", (LEGACY_USER_ID,)).fetchone()
            return _public_user(row)
    except db.IntegrityError as error:
        raise ValueError("That email is already assigned to another account.") from error
