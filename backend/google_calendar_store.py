"""Owner-scoped SQLite persistence for the Google Calendar integration.

Three small tables in the planner's database (the event links point at study_sessions rows, so they
live -- and are test-isolated -- together; see study_planner_store._connect):
- google_calendar_connections: one row per owner -- ENCRYPTED refresh token, granted scopes, the
  dedicated "AI Tutor Study Plan" calendar and the owner's integration toggles. Access tokens are
  never stored here (they are refreshed from the refresh token and kept in memory only).
- google_calendar_event_links: which Google event mirrors which study session (one per session).
- google_oauth_states: one-time, expiring OAuth `state` values bound to the app user (stored hashed).

Persistence only: no HTTP, no encryption, no planner decisions. Every query is scoped by owner_id.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from backend import study_planner_store
from backend.study_planner_store import utc_now_iso

CONNECTION_STATUSES = ("connected", "reconnect_required")
LINK_STATUSES = ("synced", "error")


def _connect():
    return study_planner_store._connect()


def initialize_google_calendar_store() -> None:
    study_planner_store.initialize_study_planner_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS google_calendar_connections (
                owner_id TEXT PRIMARY KEY, encrypted_refresh_token TEXT NOT NULL,
                granted_scopes TEXT NOT NULL DEFAULT '', study_calendar_id TEXT, study_calendar_name TEXT,
                status TEXT NOT NULL DEFAULT 'connected',
                avoid_conflicts INTEGER NOT NULL DEFAULT 1, sync_sessions INTEGER NOT NULL DEFAULT 1,
                utc_offset_minutes INTEGER,
                connected_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS google_calendar_event_links (
                owner_id TEXT NOT NULL, session_id TEXT NOT NULL, calendar_id TEXT NOT NULL,
                google_event_id TEXT NOT NULL, synced_hash TEXT, last_synced_at TEXT,
                sync_status TEXT NOT NULL DEFAULT 'synced', last_error TEXT,
                PRIMARY KEY (owner_id, session_id),
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS google_oauth_states (
                state_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL, consumed_at TEXT,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_google_oauth_states_owner ON google_oauth_states(owner_id);
            """
        )


# -- OAuth state ----------------------------------------------------------------

def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_state(owner_id: str, state: str, ttl: timedelta) -> None:
    initialize_google_calendar_store()
    now = datetime.now(timezone.utc)
    with _connect() as connection:
        # Expired states of anyone are useless: drop them while we are here.
        connection.execute("DELETE FROM google_oauth_states WHERE expires_at < ?", (now.isoformat(),))
        connection.execute(
            "INSERT INTO google_oauth_states (state_hash, owner_id, expires_at) VALUES (?, ?, ?)",
            (_state_hash(state), owner_id, (now + ttl).isoformat()),
        )


def consume_oauth_state(state: str, owner_id: str) -> bool:
    """Atomically mark `state` used. True only for an unexpired, never-used state that belongs to
    `owner_id`; any other state (unknown, expired, reused, another user's) is refused."""
    initialize_google_calendar_store()
    if not state:
        return False
    now = utc_now_iso()
    with _connect() as connection:
        cursor = connection.execute(
            """UPDATE google_oauth_states SET consumed_at=?
               WHERE state_hash=? AND owner_id=? AND consumed_at IS NULL AND expires_at > ?""",
            (now, _state_hash(state), owner_id, now),
        )
        return cursor.rowcount == 1


def delete_oauth_states(owner_id: str) -> None:
    initialize_google_calendar_store()
    with _connect() as connection:
        connection.execute("DELETE FROM google_oauth_states WHERE owner_id=?", (owner_id,))


# -- connections ----------------------------------------------------------------

_CONNECTION_COLUMNS = ("owner_id", "encrypted_refresh_token", "granted_scopes", "study_calendar_id",
                       "study_calendar_name", "status", "avoid_conflicts", "sync_sessions", "utc_offset_minutes",
                       "connected_at", "updated_at")


def _connection(row) -> dict:
    record = {key: row[key] for key in _CONNECTION_COLUMNS}
    record["avoid_conflicts"] = bool(record["avoid_conflicts"])
    record["sync_sessions"] = bool(record["sync_sessions"])
    return record


def get_connection(owner_id: str) -> dict | None:
    initialize_google_calendar_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM google_calendar_connections WHERE owner_id=?", (owner_id,),
        ).fetchone()
    return _connection(row) if row else None


def save_connection(owner_id: str, encrypted_refresh_token: str, granted_scopes: str) -> dict:
    """Insert or replace the owner's tokens (a reconnect keeps the calendar and the toggles)."""
    initialize_google_calendar_store()
    now = utc_now_iso()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO google_calendar_connections
                   (owner_id, encrypted_refresh_token, granted_scopes, status, connected_at, updated_at)
               VALUES (?, ?, ?, 'connected', ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET encrypted_refresh_token=excluded.encrypted_refresh_token,
                   granted_scopes=excluded.granted_scopes, status='connected',
                   connected_at=excluded.connected_at, updated_at=excluded.updated_at""",
            (owner_id, encrypted_refresh_token, granted_scopes, now, now),
        )
    return get_connection(owner_id)


_UPDATABLE = ("study_calendar_id", "study_calendar_name", "status", "avoid_conflicts", "sync_sessions",
              "utc_offset_minutes")


def update_connection(owner_id: str, changes: dict) -> dict | None:
    initialize_google_calendar_store()
    fields = {key: changes[key] for key in _UPDATABLE if key in changes}
    if "status" in fields and fields["status"] not in CONNECTION_STATUSES:
        raise ValueError("Unknown Google Calendar connection status.")
    for key in ("avoid_conflicts", "sync_sessions"):
        if key in fields:
            fields[key] = int(bool(fields[key]))
    if fields:
        fields["updated_at"] = utc_now_iso()
        with _connect() as connection:
            connection.execute(
                f"UPDATE google_calendar_connections SET {', '.join(f'{key}=?' for key in fields)} WHERE owner_id=?",
                (*fields.values(), owner_id),
            )
    return get_connection(owner_id)


def delete_connection(owner_id: str) -> None:
    initialize_google_calendar_store()
    with _connect() as connection:
        connection.execute("DELETE FROM google_calendar_connections WHERE owner_id=?", (owner_id,))


# -- event links ----------------------------------------------------------------

_LINK_COLUMNS = ("owner_id", "session_id", "calendar_id", "google_event_id", "synced_hash", "last_synced_at",
                 "sync_status", "last_error")


def list_event_links(owner_id: str) -> list[dict]:
    initialize_google_calendar_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM google_calendar_event_links WHERE owner_id=? ORDER BY session_id", (owner_id,),
        ).fetchall()
    return [{key: row[key] for key in _LINK_COLUMNS} for row in rows]


def save_event_link(owner_id: str, session_id: str, calendar_id: str, google_event_id: str, *,
                    synced_hash: str | None, sync_status: str, last_error: str | None = None) -> None:
    initialize_google_calendar_store()
    if sync_status not in LINK_STATUSES:
        raise ValueError("Unknown event link status.")
    now = utc_now_iso()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO google_calendar_event_links
                   (owner_id, session_id, calendar_id, google_event_id, synced_hash, last_synced_at, sync_status, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id, session_id) DO UPDATE SET calendar_id=excluded.calendar_id,
                   google_event_id=excluded.google_event_id, synced_hash=excluded.synced_hash,
                   last_synced_at=CASE WHEN excluded.sync_status='synced' THEN excluded.last_synced_at
                                       ELSE google_calendar_event_links.last_synced_at END,
                   sync_status=excluded.sync_status, last_error=excluded.last_error""",
            (owner_id, session_id, calendar_id, google_event_id, synced_hash,
             now if sync_status == "synced" else None, sync_status, last_error),
        )


def move_event_link(owner_id: str, from_session_id: str, to_session_id: str) -> None:
    """A rescheduled session inherits its original's event (updated in place, never duplicated)."""
    initialize_google_calendar_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE google_calendar_event_links SET session_id=?, synced_hash=NULL WHERE owner_id=? AND session_id=?",
            (to_session_id, owner_id, from_session_id),
        )


def mark_event_link_error(owner_id: str, session_id: str, message: str) -> None:
    initialize_google_calendar_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE google_calendar_event_links SET sync_status='error', last_error=? WHERE owner_id=? AND session_id=?",
            (message, owner_id, session_id),
        )


def delete_event_link(owner_id: str, session_id: str) -> None:
    initialize_google_calendar_store()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM google_calendar_event_links WHERE owner_id=? AND session_id=?", (owner_id, session_id),
        )


def link_summary(owner_id: str) -> dict:
    """{errors, last_synced_at} over the owner's links -- for the status endpoint."""
    initialize_google_calendar_store()
    with _connect() as connection:
        row = connection.execute(
            """SELECT SUM(sync_status='error') AS errors, MAX(last_synced_at) AS last_synced_at
               FROM google_calendar_event_links WHERE owner_id=?""",
            (owner_id,),
        ).fetchone()
    return {"errors": int(row["errors"] or 0), "last_synced_at": row["last_synced_at"]}


def known_calendar_ids(owner_id: str) -> list[str]:
    """Calendars this owner's events were synced into before (a reconnect may reuse one)."""
    initialize_google_calendar_store()
    with _connect() as connection:
        rows = connection.execute(
            """SELECT calendar_id FROM google_calendar_event_links WHERE owner_id=?
               GROUP BY calendar_id ORDER BY MAX(COALESCE(last_synced_at, '')) DESC""",
            (owner_id,),
        ).fetchall()
    return [row["calendar_id"] for row in rows]
