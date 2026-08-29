"""Owner-scoped SQLite persistence for grounded document flashcards."""

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


def initialize_flashcard_store() -> None:
    initialize_auth_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS flashcard_sets (
                set_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, document_id TEXT NOT NULL,
                document_hash TEXT NOT NULL, topic_schema_version INTEGER NOT NULL,
                flashcard_version TEXT NOT NULL, model_id TEXT NOT NULL, runtime_model TEXT NOT NULL,
                topic_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_flashcard_set_identity ON flashcard_sets(
                owner_id, document_id, document_hash, topic_schema_version,
                flashcard_version, model_id, runtime_model, created_at DESC
            );
            CREATE TABLE IF NOT EXISTS flashcards (
                flashcard_id TEXT PRIMARY KEY, set_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                document_id TEXT NOT NULL, topic_id TEXT NOT NULL, topic_name TEXT NOT NULL,
                subtopic_id TEXT, subtopic_name TEXT, front TEXT NOT NULL, back TEXT NOT NULL,
                source_chunk_ids_json TEXT NOT NULL DEFAULT '[]', is_favorite INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (set_id) REFERENCES flashcard_sets(set_id) ON DELETE CASCADE,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_flashcards_owner_document
                ON flashcards(owner_id, document_id, position, created_at);
            """
        )


def _card(row: sqlite3.Row) -> dict:
    return {
        "flashcard_id": row["flashcard_id"], "owner_id": row["owner_id"],
        "document_id": row["document_id"], "topic_id": row["topic_id"],
        "topic_name": row["topic_name"], "subtopic_id": row["subtopic_id"],
        "subtopic_name": row["subtopic_name"], "front": row["front"], "back": row["back"],
        "source_chunk_ids": json.loads(row["source_chunk_ids_json"] or "[]"),
        "is_favorite": bool(row["is_favorite"]), "created_at": row["created_at"],
    }


def get_compatible_flashcards(identity: dict) -> dict | None:
    initialize_flashcard_store()
    with _connect() as connection:
        rows = connection.execute(
            """SELECT * FROM flashcard_sets WHERE owner_id=? AND document_id=? AND document_hash=?
               AND topic_schema_version=? AND flashcard_version=? AND model_id=? AND runtime_model=?
               ORDER BY created_at DESC""",
            (identity["owner_id"], identity["document_id"], identity["document_hash"],
             identity["topic_schema_version"], identity["flashcard_version"], identity["model_id"],
             identity["runtime_model"]),
        ).fetchall()
        target = next((row for row in rows if json.loads(row["topic_ids_json"]) == identity["topic_ids"]), None)
        if not target:
            return None
        cards = connection.execute(
            "SELECT * FROM flashcards WHERE set_id=? AND owner_id=? ORDER BY position, created_at, flashcard_id",
            (target["set_id"], identity["owner_id"]),
        ).fetchall()
    return {"set_id": target["set_id"], "created_at": target["created_at"], "cards": [_card(row) for row in cards]}


def save_flashcards(identity: dict, cards: list[dict]) -> dict:
    initialize_flashcard_store()
    set_id, created_at = str(uuid4()), datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO flashcard_sets (set_id, owner_id, document_id, document_hash,
               topic_schema_version, flashcard_version, model_id, runtime_model, topic_ids_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (set_id, identity["owner_id"], identity["document_id"], identity["document_hash"],
             identity["topic_schema_version"], identity["flashcard_version"], identity["model_id"],
             identity["runtime_model"], json.dumps(identity["topic_ids"]), created_at),
        )
        for position, card in enumerate(cards):
            card_id = card.get("flashcard_id") or str(uuid4())
            connection.execute(
                """INSERT INTO flashcards (flashcard_id, set_id, owner_id, document_id, topic_id,
                   topic_name, subtopic_id, subtopic_name, front, back, source_chunk_ids_json,
                   is_favorite, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (card_id, set_id, identity["owner_id"], identity["document_id"], card["topic_id"],
                 card["topic_name"], card.get("subtopic_id"), card.get("subtopic_name"), card["front"],
                 card["back"], json.dumps(card.get("source_chunk_ids") or []), int(card.get("is_favorite", False)),
                 position, created_at),
            )
    return {"set_id": set_id, "created_at": created_at, "cards": list_flashcards(identity["owner_id"], identity["document_id"], set_id)}


def list_flashcards(owner_id: str, document_id: str, set_id: str | None = None) -> list[dict]:
    initialize_flashcard_store()
    with _connect() as connection:
        if not set_id:
            row = connection.execute(
                "SELECT set_id FROM flashcard_sets WHERE owner_id=? AND document_id=? ORDER BY created_at DESC LIMIT 1",
                (owner_id, document_id),
            ).fetchone()
            set_id = row["set_id"] if row else None
        if not set_id:
            return []
        rows = connection.execute(
            "SELECT * FROM flashcards WHERE owner_id=? AND document_id=? AND set_id=? ORDER BY position, created_at, flashcard_id",
            (owner_id, document_id, set_id),
        ).fetchall()
    return [_card(row) for row in rows]


def add_flashcard(owner_id: str, document_id: str, set_id: str, card: dict) -> dict:
    initialize_flashcard_store()
    created_at, card_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
    with _connect() as connection:
        owned_set = connection.execute(
            "SELECT 1 FROM flashcard_sets WHERE set_id=? AND owner_id=? AND document_id=?", (set_id, owner_id, document_id)
        ).fetchone()
        if not owned_set:
            raise ValueError("Flashcard set not found.")
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM flashcards WHERE set_id=?", (set_id,)).fetchone()[0]
        connection.execute(
            """INSERT INTO flashcards (flashcard_id,set_id,owner_id,document_id,topic_id,topic_name,
               subtopic_id,subtopic_name,front,back,source_chunk_ids_json,is_favorite,position,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (card_id, set_id, owner_id, document_id, card["topic_id"], card["topic_name"], card.get("subtopic_id"),
             card.get("subtopic_name"), card["front"], card["back"], json.dumps(card.get("source_chunk_ids") or []),
             int(card.get("is_favorite", False)), position, created_at),
        )
        row = connection.execute("SELECT * FROM flashcards WHERE flashcard_id=?", (card_id,)).fetchone()
    return _card(row)


def update_flashcard(owner_id: str, document_id: str, flashcard_id: str, changes: dict) -> dict:
    initialize_flashcard_store()
    allowed = {"front", "back", "is_favorite"}
    fields = [(key, changes[key]) for key in allowed if key in changes]
    if not fields:
        raise ValueError("No flashcard changes supplied.")
    values = [int(value) if key == "is_favorite" else str(value).strip() for key, value in fields]
    if any(key in {"front", "back"} and not value for (key, _), value in zip(fields, values)):
        raise ValueError("Flashcard front and back cannot be empty.")
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE flashcards SET {', '.join(f'{key}=?' for key, _ in fields)} WHERE flashcard_id=? AND owner_id=? AND document_id=?",
            (*values, flashcard_id, owner_id, document_id),
        )
        if not cursor.rowcount:
            raise ValueError("Flashcard not found.")
        row = connection.execute("SELECT * FROM flashcards WHERE flashcard_id=?", (flashcard_id,)).fetchone()
    return _card(row)


def delete_flashcard(owner_id: str, document_id: str, flashcard_id: str) -> None:
    initialize_flashcard_store()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM flashcards WHERE flashcard_id=? AND owner_id=? AND document_id=?",
            (flashcard_id, owner_id, document_id),
        )
        if not cursor.rowcount:
            raise ValueError("Flashcard not found.")


def delete_document_flashcards(owner_id: str, document_id: str) -> None:
    initialize_flashcard_store()
    with _connect() as connection:
        connection.execute("DELETE FROM flashcards WHERE owner_id=? AND document_id=?", (owner_id, document_id))
        connection.execute("DELETE FROM flashcard_sets WHERE owner_id=? AND document_id=?", (owner_id, document_id))
