import json
import sqlite3
from datetime import datetime, timezone

from backend.auth_store import LEGACY_USER_ID, initialize_auth_store
from config import DATABASE_PATH, INDEXED_FILES_PATH


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_indexed_document_store() -> None:
    initialize_auth_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS indexed_documents (
                owner_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                storage_path TEXT NOT NULL,
                topic_schema_version INTEGER NOT NULL,
                topics_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, document_id),
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_indexed_documents_owner
                ON indexed_documents(owner_id, updated_at);
            CREATE TABLE IF NOT EXISTS index_registry_migrations (
                migration_key TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            );
            """
        )
        migrated = connection.execute(
            "SELECT 1 FROM index_registry_migrations WHERE migration_key = 'indexed_files_json_v1'"
        ).fetchone()
        if not migrated:
            legacy = {}
            if INDEXED_FILES_PATH.exists():
                try:
                    legacy = json.loads(INDEXED_FILES_PATH.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    legacy = {}
            now = _now()
            for document_id, info in legacy.items():
                if not isinstance(info, dict):
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO indexed_documents (
                        owner_id, document_id, display_name, file_hash, chunk_count,
                        storage_path, topic_schema_version, topics_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        LEGACY_USER_ID, document_id, document_id, str(info.get("hash", "")),
                        int(info.get("chunks", 0)), str(info.get("path", document_id)),
                        int(info.get("topic_schema_version", 0)),
                        json.dumps(info.get("topics") or [], ensure_ascii=False), now, now,
                    ),
                )
            connection.execute(
                "INSERT INTO index_registry_migrations (migration_key, completed_at) VALUES ('indexed_files_json_v1', ?)",
                (now,),
            )


def list_indexed_documents(owner_id: str) -> list[dict]:
    initialize_indexed_document_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM indexed_documents WHERE owner_id = ? ORDER BY updated_at DESC, document_id",
            (owner_id,),
        ).fetchall()
    return [
        {
            "owner_id": row["owner_id"], "document_id": row["document_id"],
            "display_name": row["display_name"], "hash": row["file_hash"],
            "chunks": row["chunk_count"], "path": row["storage_path"],
            "topic_schema_version": row["topic_schema_version"],
            "topics": json.loads(row["topics_json"] or "[]"),
        }
        for row in rows
    ]


def get_indexed_document(owner_id: str, document_id: str) -> dict | None:
    return next((item for item in list_indexed_documents(owner_id) if item["document_id"] == document_id), None)


def upsert_indexed_document(owner_id: str, document_id: str, info: dict) -> dict:
    initialize_indexed_document_store()
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO indexed_documents (
                owner_id, document_id, display_name, file_hash, chunk_count, storage_path,
                topic_schema_version, topics_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, document_id) DO UPDATE SET
                display_name=excluded.display_name, file_hash=excluded.file_hash,
                chunk_count=excluded.chunk_count, storage_path=excluded.storage_path,
                topic_schema_version=excluded.topic_schema_version,
                topics_json=excluded.topics_json, updated_at=excluded.updated_at
            """,
            (
                owner_id, document_id, info.get("display_name") or document_id,
                str(info.get("hash", "")), int(info.get("chunks", 0)), str(info.get("path", "")),
                int(info.get("topic_schema_version", 0)),
                json.dumps(info.get("topics") or [], ensure_ascii=False), now, now,
            ),
        )
    return get_indexed_document(owner_id, document_id)


def delete_indexed_document(owner_id: str, document_id: str) -> dict | None:
    document = get_indexed_document(owner_id, document_id)
    if not document:
        return None
    with _connect() as connection:
        connection.execute(
            "DELETE FROM indexed_documents WHERE owner_id = ? AND document_id = ?", (owner_id, document_id)
        )
    return document
