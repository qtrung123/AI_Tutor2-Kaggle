"""Finished OCR and Vision results, so a page or figure is never processed twice.

Per owner, and removed with the document that produced them: a description of a deleted upload
does not outlive it. Lives in the app database, so on PostgreSQL it survives a Kaggle reset.
"""
from datetime import datetime, timezone

from backend import db
from backend.auth_store import initialize_auth_store
from config import DATABASE_PATH


def _initialize_schema(connection: db.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS visual_extraction_cache (
            owner_id TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            result_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, cache_key),
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_visual_extraction_cache_file
            ON visual_extraction_cache(owner_id, file_hash);
        """
    )


def initialize_visual_cache_store() -> None:
    """Create the table (idempotent). Called at startup by durable_setup.initialize_all_stores, so the
    table exists before lock_down_api_roles runs; get/put/delete_for_file still create it on demand."""
    initialize_auth_store()
    db.ensure_schema("visual_extraction_cache", DATABASE_PATH, _initialize_schema)


_initialize = initialize_visual_cache_store


def get(owner_id: str, cache_key: str) -> str | None:
    """The stored text ("" = nothing useful was found), or None when it was never processed."""
    _initialize()
    with db.connect(DATABASE_PATH, autocommit=True) as connection:
        row = connection.execute(
            "SELECT result_text FROM visual_extraction_cache WHERE owner_id = ? AND cache_key = ?",
            (owner_id, cache_key),
        ).fetchone()
    return row["result_text"] if row else None


def put(owner_id: str, cache_key: str, file_hash: str, text: str) -> None:
    _initialize()
    with db.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO visual_extraction_cache (owner_id, cache_key, file_hash, result_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (owner_id, cache_key) DO UPDATE SET
                file_hash = excluded.file_hash, result_text = excluded.result_text
            """,
            (owner_id, cache_key, file_hash, text, datetime.now(timezone.utc).isoformat()),
        )


def delete_for_file(owner_id: str, file_hash: str) -> None:
    _initialize()
    with db.connect(DATABASE_PATH) as connection:
        connection.execute(
            "DELETE FROM visual_extraction_cache WHERE owner_id = ? AND file_hash = ?", (owner_id, file_hash),
        )
