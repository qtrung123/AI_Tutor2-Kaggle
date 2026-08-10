"""SQLite persistence for independent chat conversations and their messages."""

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from config import DATABASE_PATH
from backend.auth_store import LEGACY_USER_ID, initialize_auth_store


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite file handles when a transaction context exits."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize_conversation_store() -> None:
    initialize_auth_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_sources (
                conversation_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                PRIMARY KEY (conversation_id, document_id),
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                grounding_status TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS message_citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                page TEXT,
                chunk TEXT,
                content TEXT,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, created_at);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
        if "owner_id" not in columns:
            connection.execute(
                f"ALTER TABLE conversations ADD COLUMN owner_id TEXT NOT NULL DEFAULT '{LEGACY_USER_ID}'"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_id, updated_at)"
        )


def _source_ids(connection: sqlite3.Connection, conversation_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT document_id FROM conversation_sources WHERE conversation_id = ? ORDER BY document_id",
        (conversation_id,),
    ).fetchall()
    return [row["document_id"] for row in rows]


def create_conversation(owner_id: str, title: str = "New conversation", document_ids: list[str] | None = None) -> dict:
    initialize_conversation_store()
    conversation_id = str(uuid4())
    timestamp = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, owner_id) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, title.strip() or "New conversation", timestamp, timestamp, owner_id),
        )
        for document_id in dict.fromkeys(document_ids or []):
            connection.execute(
                "INSERT INTO conversation_sources (conversation_id, document_id) VALUES (?, ?)",
                (conversation_id, document_id),
            )
    return get_conversation(owner_id, conversation_id, include_messages=True)


def list_conversations(owner_id: str) -> list[dict]:
    initialize_conversation_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM conversations WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,)
        ).fetchall()
        return [
            {**dict(row), "document_ids": _source_ids(connection, row["id"])}
            for row in rows
        ]


def get_conversation(owner_id: str, conversation_id: str, include_messages: bool = True) -> dict:
    initialize_conversation_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if row is None:
            raise ValueError("Conversation not found.")
        result = {**dict(row), "document_ids": _source_ids(connection, conversation_id)}
        if include_messages:
            messages = connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
            result["messages"] = []
            for message in messages:
                item = dict(message)
                citations = connection.execute(
                    "SELECT document_id, page, chunk, content FROM message_citations WHERE message_id = ? ORDER BY id",
                    (message["id"],),
                ).fetchall()
                item["citations"] = [dict(citation) for citation in citations]
                result["messages"].append(item)
        return result


def update_conversation_title(owner_id: str, conversation_id: str, title: str) -> dict:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
            (title.strip() or "New conversation", _now(), conversation_id, owner_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("Conversation not found.")
    return get_conversation(owner_id, conversation_id, include_messages=False)


def set_conversation_sources(owner_id: str, conversation_id: str, document_ids: list[str]) -> dict:
    get_conversation(owner_id, conversation_id, include_messages=False)
    with _connect() as connection:
        connection.execute("DELETE FROM conversation_sources WHERE conversation_id = ?", (conversation_id,))
        for document_id in dict.fromkeys(document_ids):
            connection.execute(
                "INSERT INTO conversation_sources (conversation_id, document_id) VALUES (?, ?)",
                (conversation_id, document_id),
            )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conversation_id)
        )
    return get_conversation(owner_id, conversation_id, include_messages=True)


def add_message(
    owner_id: str,
    conversation_id: str,
    role: str,
    content: str,
    grounding_status: str | None = None,
    citations: list[dict] | None = None,
) -> dict:
    message_id = str(uuid4())
    timestamp = _now()
    with _connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if exists is None:
            raise ValueError("Conversation not found.")
        connection.execute(
            "INSERT INTO messages (id, conversation_id, role, content, grounding_status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, content, grounding_status, timestamp),
        )
        for citation in citations or []:
            connection.execute(
                "INSERT INTO message_citations (message_id, document_id, page, chunk, content) VALUES (?, ?, ?, ?, ?)",
                (
                    message_id,
                    citation.get("document_id") or citation.get("title", "Unknown source"),
                    str(citation.get("page", "Unknown page")),
                    str(citation.get("chunk", "")),
                    citation.get("content", ""),
                ),
            )
        connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "grounding_status": grounding_status,
        "created_at": timestamp,
        "citations": citations or [],
    }


def delete_conversation(owner_id: str, conversation_id: str) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        )
        if cursor.rowcount == 0:
            raise ValueError("Conversation not found.")


def remove_source_from_conversations(owner_id: str, document_id: str) -> None:
    initialize_conversation_store()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM conversation_sources WHERE document_id = ? AND conversation_id IN (SELECT id FROM conversations WHERE owner_id = ?)",
            (document_id, owner_id),
        )
