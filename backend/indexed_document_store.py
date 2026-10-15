import json
from datetime import datetime, timezone

from backend import db
from backend.auth_store import LEGACY_USER_ID, initialize_auth_store
from config import DATABASE_PATH, INDEXED_FILES_PATH


def _connect(autocommit: bool = False) -> db.Connection:
    return db.connect(DATABASE_PATH, autocommit=autocommit)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_indexed_document_store() -> None:
    initialize_auth_store()
    db.ensure_schema("indexed_documents", DATABASE_PATH, _initialize_schema)


def _initialize_schema(connection: db.Connection) -> None:
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
    # Durable original upload (Supabase Storage); NULL for documents kept only on local disk.
    db.add_column(connection, "indexed_documents", "object_path", "TEXT")
    db.add_column(connection, "indexed_documents", "object_bucket", "TEXT")
    # The pre-authentication indexed_files.json registry is a local file: import it into the local
    # SQLite database only. The shared PostgreSQL database never ingests stale local state.
    if connection.backend != db.SQLITE:
        return
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
                INSERT INTO indexed_documents (
                    owner_id, document_id, display_name, file_hash, chunk_count,
                    storage_path, topic_schema_version, topics_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
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
    with _connect(autocommit=True) as connection:  # one read: no BEGIN/COMMIT round trips
        rows = connection.execute(
            "SELECT * FROM indexed_documents WHERE owner_id = ? ORDER BY updated_at DESC, document_id",
            (owner_id,),
        ).fetchall()
    return [_row_to_document(row) for row in rows]


def _row_to_document(row) -> dict:
    return {
        "owner_id": row["owner_id"], "document_id": row["document_id"],
        "display_name": row["display_name"], "hash": row["file_hash"],
        "chunks": row["chunk_count"], "path": row["storage_path"],
        "topic_schema_version": row["topic_schema_version"],
        "topics": json.loads(row["topics_json"] or "[]"),
        "object_path": row["object_path"], "object_bucket": row["object_bucket"],
    }


def list_stale_topic_documents(current_topic_schema_version: int) -> list[dict]:
    """Every owner's documents whose stored topics predate the current topic extractor schema."""
    initialize_indexed_document_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM indexed_documents WHERE topic_schema_version < ? ORDER BY owner_id, document_id",
            (int(current_topic_schema_version),),
        ).fetchall()
    return [_row_to_document(row) for row in rows]


def get_indexed_document(owner_id: str, document_id: str) -> dict | None:
    initialize_indexed_document_store()
    with _connect(autocommit=True) as connection:  # one keyed read instead of the owner's whole list
        row = connection.execute(
            "SELECT * FROM indexed_documents WHERE owner_id = ? AND document_id = ?", (owner_id, document_id),
        ).fetchone()
    return _row_to_document(row) if row else None


def upsert_indexed_document(owner_id: str, document_id: str, info: dict) -> dict:
    initialize_indexed_document_store()
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO indexed_documents (
                owner_id, document_id, display_name, file_hash, chunk_count, storage_path,
                topic_schema_version, topics_json, created_at, updated_at, object_path, object_bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, document_id) DO UPDATE SET
                display_name=excluded.display_name, file_hash=excluded.file_hash,
                chunk_count=excluded.chunk_count, storage_path=excluded.storage_path,
                topic_schema_version=excluded.topic_schema_version,
                topics_json=excluded.topics_json, updated_at=excluded.updated_at,
                object_path=COALESCE(excluded.object_path, indexed_documents.object_path),
                object_bucket=COALESCE(excluded.object_bucket, indexed_documents.object_bucket)
            """,
            (
                owner_id, document_id, info.get("display_name") or document_id,
                str(info.get("hash", "")), int(info.get("chunks", 0)), str(info.get("path", "")),
                int(info.get("topic_schema_version", 0)),
                json.dumps(info.get("topics") or [], ensure_ascii=False), now, now,
                info.get("object_path") or None, info.get("object_bucket") or None,
            ),
        )
    return get_indexed_document(owner_id, document_id)


def is_object_referenced(owner_id: str, object_path: str) -> bool:
    """Whether any of the owner's registered documents still points at this stored object."""
    initialize_indexed_document_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM indexed_documents WHERE owner_id = ? AND object_path = ? LIMIT 1", (owner_id, object_path)
        ).fetchone()
    return row is not None


def delete_indexed_document(owner_id: str, document_id: str) -> dict | None:
    document = get_indexed_document(owner_id, document_id)
    if not document:
        return None
    with _connect() as connection:
        connection.execute(
            "DELETE FROM indexed_documents WHERE owner_id = ? AND document_id = ?", (owner_id, document_id)
        )
    return document
