"""Persistence for independent chat conversations and their messages (PostgreSQL or SQLite)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend import db
from config import DATABASE_PATH
from backend.auth_store import LEGACY_USER_ID, initialize_auth_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> db.Connection:
    return db.connect(DATABASE_PATH)


def initialize_conversation_store() -> None:
    initialize_auth_store()
    db.ensure_schema("conversations", DATABASE_PATH, _initialize_schema)


def _initialize_schema(connection: db.Connection) -> None:
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
    db.add_column(connection, "conversations", "owner_id", f"TEXT NOT NULL DEFAULT '{LEGACY_USER_ID}'")
    db.add_column(connection, "conversations", "document_id", "TEXT")
    connection.execute(
        """
        UPDATE conversations
        SET document_id = (
            SELECT cs.document_id FROM conversation_sources cs
            WHERE cs.conversation_id = conversations.id
            ORDER BY cs.document_id LIMIT 1
        )
        WHERE document_id IS NULL
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_id, updated_at)"
    )


def _source_ids(connection: db.Connection, conversation_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT document_id FROM conversation_sources WHERE conversation_id = ? ORDER BY document_id",
        (conversation_id,),
    ).fetchall()
    return [row["document_id"] for row in rows]


def _conversation_result(connection: db.Connection, row) -> dict:
    item = dict(row)
    document_id = item.get("document_id")
    item["document_ids"] = [document_id] if document_id else _source_ids(connection, row["id"])
    return item


def create_conversation(
    owner_id: str,
    title: str = "New conversation",
    document_ids: list[str] | None = None,
    new_conversation: bool = False,
) -> dict:
    """Create a conversation scoped to exactly one document.

    By default the document's existing conversation is returned instead (one chat per Study Session);
    new_conversation=True always starts another conversation for the same document ("New chat").
    """
    initialize_conversation_store()
    unique_document_ids = list(dict.fromkeys(document_ids or []))
    if len(unique_document_ids) != 1:
        raise ValueError("A Study Session conversation requires exactly one document.")
    document_id = unique_document_ids[0]
    existing = None if new_conversation else get_conversation_for_document(owner_id, document_id)
    if existing:
        return get_conversation(owner_id, existing["id"], include_messages=True)
    conversation_id = str(uuid4())
    timestamp = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, owner_id, document_id) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, title.strip() or "New conversation", timestamp, timestamp, owner_id, document_id),
        )
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
            _conversation_result(connection, row)
            for row in rows
        ]


def get_conversation_for_document(owner_id: str, document_id: str) -> dict | None:
    initialize_conversation_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE owner_id = ? AND document_id = ? ORDER BY created_at LIMIT 1",
            (owner_id, document_id),
        ).fetchone()
        return _conversation_result(connection, row) if row else None


def _recent_messages(connection: db.Connection, conversation_id: str, limit: int) -> list:
    """The last `limit` messages in chronological order. When none of them is a student message
    but older ones exist, the latest student message is put first, so a caller looking back for
    the previous question finds the same one it would find in the full history."""
    rows = list(reversed(connection.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()))
    if len(rows) == limit and all(row["role"] != "user" for row in rows):
        latest_user = connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if latest_user is not None:
            rows.insert(0, latest_user)
    return rows


def _citations_by_message(connection: db.Connection, conversation_id: str, message_ids: list[str] | None) -> dict:
    """All citations of the conversation's messages in one query (or of `message_ids` only), grouped
    by message and kept in insertion (id) order -- the same lists one query per message returned."""
    if message_ids is None:
        rows = connection.execute(
            "SELECT c.message_id, c.document_id, c.page, c.chunk, c.content FROM message_citations c "
            "JOIN messages m ON m.id = c.message_id WHERE m.conversation_id = ? ORDER BY c.id",
            (conversation_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT message_id, document_id, page, chunk, content FROM message_citations "
            f"WHERE message_id IN ({','.join('?' * len(message_ids))}) ORDER BY id",
            tuple(message_ids),
        ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["message_id"], []).append(
            {"document_id": row["document_id"], "page": row["page"], "chunk": row["chunk"], "content": row["content"]}
        )
    return grouped


def get_conversation(owner_id: str, conversation_id: str, include_messages: bool = True,
                     recent_messages: int | None = None, include_citations: bool = True) -> dict:
    """The owner's conversation; with include_messages, its messages oldest first, each with its
    citations. recent_messages=N keeps only the last N (see _recent_messages) and
    include_citations=False leaves citations out -- what answering a new question needs."""
    initialize_conversation_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if row is None:
            raise ValueError("Conversation not found.")
        result = _conversation_result(connection, row)
        if include_messages:
            messages = (
                connection.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
                    (conversation_id,),
                ).fetchall()
                if recent_messages is None else _recent_messages(connection, conversation_id, recent_messages)
            )
            citations = {}
            if include_citations and messages:
                citations = _citations_by_message(
                    connection, conversation_id,
                    None if recent_messages is None else [message["id"] for message in messages],
                )
            result["messages"] = [
                {**dict(message), "citations": citations.get(message["id"], [])} if include_citations else dict(message)
                for message in messages
            ]
        return result


def update_conversation_title(owner_id: str, conversation_id: str, title: str,
                              return_conversation: bool = True) -> dict | None:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
            (title.strip() or "New conversation", _now(), conversation_id, owner_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("Conversation not found.")
    return get_conversation(owner_id, conversation_id, include_messages=False) if return_conversation else None


def set_conversation_sources(owner_id: str, conversation_id: str, document_ids: list[str]) -> dict:
    unique_document_ids = list(dict.fromkeys(document_ids))
    if len(unique_document_ids) != 1:
        raise ValueError("A Study Session conversation requires exactly one document.")
    current = get_conversation(owner_id, conversation_id, include_messages=False)
    if current.get("document_id") and current["document_id"] != unique_document_ids[0]:
        raise ValueError("A Study Session conversation cannot be reassigned to another document.")
    document_id = unique_document_ids[0]
    with _connect() as connection:
        connection.execute("DELETE FROM conversation_sources WHERE conversation_id = ?", (conversation_id,))
        connection.execute(
            "INSERT INTO conversation_sources (conversation_id, document_id) VALUES (?, ?)",
            (conversation_id, document_id),
        )
        connection.execute(
            "UPDATE conversations SET document_id = ?, updated_at = ? WHERE id = ?",
            (document_id, _now(), conversation_id),
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
    timestamp = _now()
    with _connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if exists is None:
            raise ValueError("Conversation not found.")
        message = _insert_message(connection, conversation_id, role, content, grounding_status, citations, timestamp)
        connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
    return message


def _insert_message(connection: db.Connection, conversation_id: str, role: str, content: str,
                    grounding_status: str | None, citations: list[dict] | None, timestamp: str) -> dict:
    """Insert one message and its citations (one batched statement) inside the caller's unit."""
    message_id = str(uuid4())
    connection.execute(
        "INSERT INTO messages (id, conversation_id, role, content, grounding_status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, conversation_id, role, content, grounding_status, timestamp),
    )
    if citations:
        connection.executemany(
            "INSERT INTO message_citations (message_id, document_id, page, chunk, content) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    message_id,
                    citation.get("document_id") or citation.get("title", "Unknown source"),
                    str(citation.get("page", "Unknown page")),
                    str(citation.get("chunk", "")),
                    citation.get("content", ""),
                )
                for citation in citations
            ],
        )
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "grounding_status": grounding_status,
        "created_at": timestamp,
        "citations": citations or [],
    }


def _after(timestamp: str) -> str:
    """A timestamp strictly later than `timestamp`, so messages saved together keep their order."""
    now = _now()
    if now > timestamp:
        return now
    return (datetime.fromisoformat(timestamp) + timedelta(microseconds=1)).isoformat()


def add_exchange(
    owner_id: str,
    conversation_id: str,
    user_content: str,
    assistant_content: str,
    grounding_status: str | None,
    citations: list[dict] | None,
    title: str | None = None,
) -> tuple[dict, dict]:
    """Save a student message and the tutor's answer (with its citations) in one unit of work, and
    the conversation's first title when `title` is given. Same rows, timestamps order and
    ownership check as add_message() twice plus update_conversation_title(), in one transaction."""
    with _connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND owner_id = ?", (conversation_id, owner_id)
        ).fetchone()
        if exists is None:
            raise ValueError("Conversation not found.")
        user_timestamp = _now()
        user_message = _insert_message(connection, conversation_id, "user", user_content, None, None, user_timestamp)
        assistant_timestamp = _after(user_timestamp)
        assistant_message = _insert_message(connection, conversation_id, "assistant", assistant_content,
                                            grounding_status, citations, assistant_timestamp)
        if title is not None:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip() or "New conversation", assistant_timestamp, conversation_id),
            )
        else:
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (assistant_timestamp, conversation_id))
    return user_message, assistant_message


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
