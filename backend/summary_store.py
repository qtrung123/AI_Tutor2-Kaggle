"""Owner-scoped SQLite persistence for generated document summaries."""

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from backend.auth_store import initialize_auth_store
from config import DATABASE_PATH


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


def initialize_summary_store() -> None:
    initialize_auth_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS document_summaries (
                summary_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                document_hash TEXT NOT NULL,
                topic_schema_version INTEGER NOT NULL,
                summary_version TEXT NOT NULL,
                model_id TEXT NOT NULL,
                runtime_model TEXT NOT NULL,
                topic_summaries_json TEXT NOT NULL,
                final_summary_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_document_summary_version
                ON document_summaries(owner_id, document_id, version_number);
            CREATE INDEX IF NOT EXISTS idx_document_summary_identity
                ON document_summaries(
                    owner_id, document_id, document_hash, topic_schema_version,
                    summary_version, model_id, version_number DESC
                );
            """
        )


def _decode(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    return {
        "summary_id": row["summary_id"], "owner_id": row["owner_id"],
        "document_id": row["document_id"], "version_number": row["version_number"],
        "document_hash": row["document_hash"],
        "topic_schema_version": row["topic_schema_version"],
        "summary_version": row["summary_version"], "model": row["model_id"],
        "runtime_model": row["runtime_model"],
        "topic_summaries": json.loads(row["topic_summaries_json"]),
        "final_summary": json.loads(row["final_summary_json"]),
        "metrics": json.loads(row["metrics_json"] or "{}"), "created_at": row["created_at"],
    }


def get_compatible_summary(identity: dict) -> dict | None:
    initialize_summary_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM document_summaries
            WHERE owner_id=? AND document_id=? AND document_hash=?
              AND topic_schema_version=? AND summary_version=? AND model_id=? AND runtime_model=?
            ORDER BY version_number DESC LIMIT 1
            """,
            (identity["owner_id"], identity["document_id"], identity["document_hash"],
             identity["topic_schema_version"], identity["summary_version"], identity["model_id"],
             identity["runtime_model"]),
        ).fetchone()
    return _decode(row)


def save_summary(identity: dict, topic_summaries: list[dict], final_summary: dict, metrics: dict) -> dict:
    initialize_summary_store()
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        version_number = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM document_summaries WHERE owner_id=? AND document_id=?",
            (identity["owner_id"], identity["document_id"]),
        ).fetchone()[0]
        summary_id = str(uuid4())
        connection.execute(
            """INSERT INTO document_summaries (
                summary_id, owner_id, document_id, version_number, document_hash,
                topic_schema_version, summary_version, model_id, runtime_model,
                topic_summaries_json, final_summary_json, metrics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (summary_id, identity["owner_id"], identity["document_id"], version_number,
             identity["document_hash"], identity["topic_schema_version"], identity["summary_version"],
             identity["model_id"], identity["runtime_model"],
             json.dumps(topic_summaries, ensure_ascii=False), json.dumps(final_summary, ensure_ascii=False),
             json.dumps(metrics, ensure_ascii=False), created_at),
        )
        row = connection.execute("SELECT * FROM document_summaries WHERE summary_id=?", (summary_id,)).fetchone()
    return _decode(row)


def delete_document_summaries(owner_id: str, document_id: str) -> None:
    initialize_summary_store()
    with _connect() as connection:
        connection.execute("DELETE FROM document_summaries WHERE owner_id=? AND document_id=?", (owner_id, document_id))
