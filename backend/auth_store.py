import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from config import AUTH_SESSION_DAYS, DATABASE_PATH, LEGACY_USER_EMAIL


LEGACY_USER_ID = "local_student"
_password_hasher = PasswordHasher()
_dummy_hash = _password_hasher.hash("not-a-real-user-password")


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_user(row: sqlite3.Row | dict) -> dict:
    return {key: row[key] for key in ("id", "display_name", "email", "created_at")}


def initialize_auth_store() -> None:
    with _connect() as connection:
        connection.executescript(
            """
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
        )
        legacy = connection.execute("SELECT 1 FROM users WHERE id = ?", (LEGACY_USER_ID,)).fetchone()
        if not legacy:
            # The legacy account is intentionally locked until an operator explicitly sets credentials.
            connection.execute(
                "INSERT INTO users (id, display_name, email, password_hash, created_at, disabled_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    LEGACY_USER_ID,
                    "Legacy Local User",
                    LEGACY_USER_EMAIL.strip().lower(),
                    _password_hasher.hash(secrets.token_urlsafe(48)),
                    utc_now().isoformat(),
                    utc_now().isoformat(),
                ),
            )


def normalize_email(email: str) -> str:
    return str(email).strip().lower()


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
    except sqlite3.IntegrityError as error:
        raise ValueError("An account with this email already exists.") from error


def authenticate_user(email: str, password: str) -> dict | None:
    initialize_auth_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (normalize_email(email),)
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
    except sqlite3.IntegrityError as error:
        raise ValueError("That email is already assigned to another account.") from error
