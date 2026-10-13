import hashlib
import secrets
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
_POSTGRES_SCHEMA = """
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


def _connect() -> db.Connection:
    """Auth tables live in PostgreSQL when DATABASE_URL is set, otherwise in the SQLite file."""
    return db.connect(DATABASE_PATH)


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


def _initialize_schema(connection: db.Connection) -> None:
    connection.executescript(_POSTGRES_SCHEMA if connection.backend == db.POSTGRES else _SQLITE_SCHEMA)
    _ensure_legacy_user(connection)


def initialize_auth_store() -> None:
    db.ensure_schema("auth", DATABASE_PATH, _initialize_schema)


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
            return _public_user(row)
    except db.IntegrityError as error:
        raise ValueError("An account with this email already exists.") from error


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
    return _public_user(row)


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
    return _public_user(row) if row else None


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
