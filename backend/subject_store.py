"""User-owned subjects/courses that group uploaded documents, e.g. 2 PDFs in "Biology" and 1 in
"Physics".

Organizational only: a subject is a name and a color. A document belongs to at most one subject
through indexed_documents.subject_id (NULL = Unassigned). Nothing here touches a document's file,
vectors, Chat, Summary, Flashcards, Quiz or Planner data -- deleting a subject only unassigns its
documents. Every read and write is scoped by owner_id, so one user can never see, assign to or
delete another user's subject.
"""

import re
import uuid
from datetime import datetime, timezone

from backend import db, indexed_document_store
from backend.auth_store import initialize_auth_store
from backend.indexed_document_store import initialize_indexed_document_store

MAX_SUBJECT_NAME_LENGTH = 80
# The design system's subject palette (frontend --subject-* tokens); a new subject without an
# explicit color takes the next one in turn.
SUBJECT_COLORS = ("#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#64748b")
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


class SubjectNotFound(LookupError):
    """The subject does not exist for this owner."""


class SubjectNameConflict(ValueError):
    """The owner already has a subject with this name (case-insensitive)."""


class DocumentNotFound(LookupError):
    """At least one document does not exist for this owner."""


def _database_path():
    # Subjects are read and written together with indexed_documents.subject_id, so they always live
    # in the document registry's database.
    return indexed_document_store.DATABASE_PATH


def _connect(autocommit: bool = False) -> db.Connection:
    return db.connect(_database_path(), autocommit=autocommit)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_subject_store() -> None:
    initialize_auth_store()
    initialize_indexed_document_store()   # adds indexed_documents.subject_id on legacy databases
    db.ensure_schema("subjects", _database_path(), _initialize_schema)


def _initialize_schema(connection: db.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            owner_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, subject_id),
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_owner_name
            ON subjects(owner_id, name_key);
        CREATE INDEX IF NOT EXISTS idx_indexed_documents_subject
            ON indexed_documents(owner_id, subject_id);
        """
    )


def normalize_subject_name(name: str | None) -> str:
    """Trimmed, single-spaced name; ValueError when empty or too long."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("Subject name is required.")
    if len(cleaned) > MAX_SUBJECT_NAME_LENGTH:
        raise ValueError(f"Subject name must be at most {MAX_SUBJECT_NAME_LENGTH} characters.")
    return cleaned


def normalize_subject_color(color: str | None) -> str | None:
    """Lower-case #rrggbb, or None when not given; ValueError for anything else."""
    if color is None or str(color).strip() == "":
        return None
    value = str(color).strip()
    if not _COLOR_PATTERN.match(value):
        raise ValueError("Subject color must be a hex color like #4f46e5.")
    return value.lower()


def _row_to_subject(row, document_ids: list[str]) -> dict:
    return {
        "subject_id": row["subject_id"], "name": row["name"], "color": row["color"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "document_ids": document_ids, "document_count": len(document_ids),
    }


def _subject_row(connection: db.Connection, owner_id: str, subject_id: str):
    return connection.execute(
        "SELECT * FROM subjects WHERE owner_id = ? AND subject_id = ?", (owner_id, subject_id),
    ).fetchone()


def _document_ids_by_subject(connection: db.Connection, owner_id: str) -> dict[str, list[str]]:
    rows = connection.execute(
        "SELECT subject_id, document_id FROM indexed_documents "
        "WHERE owner_id = ? AND subject_id IS NOT NULL ORDER BY updated_at DESC, document_id",
        (owner_id,),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["subject_id"], []).append(row["document_id"])
    return grouped


def list_subjects(owner_id: str) -> list[dict]:
    """The owner's subjects, sorted by name, each with its document ids (newest first)."""
    initialize_subject_store()
    with _connect(autocommit=True) as connection:
        rows = connection.execute(
            "SELECT * FROM subjects WHERE owner_id = ? ORDER BY name_key, created_at", (owner_id,),
        ).fetchall()
        documents = _document_ids_by_subject(connection, owner_id)
    return [_row_to_subject(row, documents.get(row["subject_id"], [])) for row in rows]


def get_subject(owner_id: str, subject_id: str) -> dict:
    initialize_subject_store()
    with _connect(autocommit=True) as connection:
        row = _subject_row(connection, owner_id, subject_id)
        if not row:
            raise SubjectNotFound("Subject not found.")
        documents = _document_ids_by_subject(connection, owner_id)
    return _row_to_subject(row, documents.get(subject_id, []))


def subject_exists(owner_id: str, subject_id: str) -> bool:
    initialize_subject_store()
    with _connect(autocommit=True) as connection:
        return _subject_row(connection, owner_id, subject_id) is not None


def find_subject_by_name(owner_id: str, name: str) -> dict | None:
    initialize_subject_store()
    key = normalize_subject_name(name).lower()
    with _connect(autocommit=True) as connection:
        row = connection.execute(
            "SELECT subject_id FROM subjects WHERE owner_id = ? AND name_key = ?", (owner_id, key),
        ).fetchone()
    return get_subject(owner_id, row["subject_id"]) if row else None


def create_subject(owner_id: str, name: str, color: str | None = None) -> dict:
    initialize_subject_store()
    cleaned = normalize_subject_name(name)
    chosen = normalize_subject_color(color)
    now = _now()
    subject_id = uuid.uuid4().hex
    with _connect() as connection:
        if connection.execute(
            "SELECT 1 FROM subjects WHERE owner_id = ? AND name_key = ?", (owner_id, cleaned.lower()),
        ).fetchone():
            raise SubjectNameConflict(f'You already have a subject named "{cleaned}".')
        if chosen is None:
            count = connection.execute("SELECT COUNT(*) FROM subjects WHERE owner_id = ?", (owner_id,)).fetchone()[0]
            chosen = SUBJECT_COLORS[int(count) % len(SUBJECT_COLORS)]
        try:
            connection.execute(
                "INSERT INTO subjects (owner_id, subject_id, name, name_key, color, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner_id, subject_id, cleaned, cleaned.lower(), chosen, now, now),
            )
        except db.IntegrityError as error:   # a concurrent create with the same name won
            raise SubjectNameConflict(f'You already have a subject named "{cleaned}".') from error
    return get_subject(owner_id, subject_id)


def get_or_create_subject(owner_id: str, name: str) -> dict:
    """The owner's subject with this name (case-insensitive), created when there is none."""
    existing = find_subject_by_name(owner_id, name)
    if existing:
        return existing
    try:
        return create_subject(owner_id, name)
    except SubjectNameConflict:
        return find_subject_by_name(owner_id, name)


def update_subject(owner_id: str, subject_id: str, name: str | None = None, color: str | None = None) -> dict:
    """Rename and/or recolor; fields left as None keep their value."""
    initialize_subject_store()
    cleaned = normalize_subject_name(name) if name is not None else None
    chosen = normalize_subject_color(color)
    with _connect() as connection:
        row = _subject_row(connection, owner_id, subject_id)
        if not row:
            raise SubjectNotFound("Subject not found.")
        if cleaned is not None and connection.execute(
            "SELECT 1 FROM subjects WHERE owner_id = ? AND name_key = ? AND subject_id <> ?",
            (owner_id, cleaned.lower(), subject_id),
        ).fetchone():
            raise SubjectNameConflict(f'You already have a subject named "{cleaned}".')
        new_name = cleaned if cleaned is not None else row["name"]
        try:
            connection.execute(
                "UPDATE subjects SET name = ?, name_key = ?, color = ?, updated_at = ? "
                "WHERE owner_id = ? AND subject_id = ?",
                (new_name, new_name.lower(), chosen or row["color"], _now(), owner_id, subject_id),
            )
        except db.IntegrityError as error:
            raise SubjectNameConflict(f'You already have a subject named "{new_name}".') from error
    return get_subject(owner_id, subject_id)


def delete_subject(owner_id: str, subject_id: str) -> dict:
    """Delete the subject and unassign its documents, in one transaction. The documents
    themselves (files, vectors and every feature's data) are left exactly as they are."""
    initialize_subject_store()
    with _connect() as connection:
        connection.begin_immediate()   # serialized with assign_documents: no document is left pointing here
        if not _subject_row(connection, owner_id, subject_id):
            raise SubjectNotFound("Subject not found.")
        unassigned = [row["document_id"] for row in connection.execute(
            "SELECT document_id FROM indexed_documents WHERE owner_id = ? AND subject_id = ? ORDER BY document_id",
            (owner_id, subject_id),
        ).fetchall()]
        connection.execute(
            "UPDATE indexed_documents SET subject_id = NULL WHERE owner_id = ? AND subject_id = ?",
            (owner_id, subject_id),
        )
        connection.execute("DELETE FROM subjects WHERE owner_id = ? AND subject_id = ?", (owner_id, subject_id))
    return {"deleted": subject_id, "unassigned_document_ids": unassigned}


def assign_documents(owner_id: str, document_ids: list[str], subject_id: str | None,
                     ignore_missing: bool = False, only_unassigned: bool = False) -> list[str]:
    """Assign, move (a document has one subject, so assigning replaces the old one) or, with
    subject_id None, unassign the owner's documents. All or nothing: an unknown subject or (unless
    ignore_missing) an unknown document changes nothing. Returns the document ids updated.

    only_unassigned (uploads): a document that already has a subject keeps it -- re-uploading a
    file never silently moves it; only an explicit assign does.

    Only indexed_documents.subject_id changes -- never updated_at, so the materials order and
    every feature keyed on the document are untouched."""
    initialize_subject_store()
    wanted = list(dict.fromkeys(str(document_id) for document_id in document_ids if str(document_id)))
    if not wanted:
        return []
    with _connect() as connection:
        connection.begin_immediate()   # a concurrent subject delete cannot interleave with this check
        if subject_id is not None and not _subject_row(connection, owner_id, subject_id):
            raise SubjectNotFound("Subject not found.")
        placeholders = ", ".join("?" for _ in wanted)
        current = {row["document_id"]: row["subject_id"] for row in connection.execute(
            f"SELECT document_id, subject_id FROM indexed_documents WHERE owner_id = ? AND document_id IN ({placeholders})",
            (owner_id, *wanted),
        ).fetchall()}
        missing = [document_id for document_id in wanted if document_id not in current]
        if missing and not ignore_missing:
            raise DocumentNotFound(f"Document not found: {', '.join(missing)}")
        updated = [document_id for document_id in wanted if document_id in current
                   and not (only_unassigned and current[document_id] is not None)]
        if updated:
            placeholders = ", ".join("?" for _ in updated)
            connection.execute(
                f"UPDATE indexed_documents SET subject_id = ? WHERE owner_id = ? AND document_id IN ({placeholders})",
                (subject_id, owner_id, *updated),
            )
        if subject_id is not None and updated:
            connection.execute(
                "UPDATE subjects SET updated_at = ? WHERE owner_id = ? AND subject_id = ?",
                (_now(), owner_id, subject_id),
            )
    return updated


def subject_ids_by_document(owner_id: str) -> dict[str, str]:
    """document_id -> subject_id for the owner's assigned documents (Unassigned ones left out)."""
    initialize_subject_store()
    with _connect(autocommit=True) as connection:
        rows = connection.execute(
            "SELECT document_id, subject_id FROM indexed_documents WHERE owner_id = ? AND subject_id IS NOT NULL",
            (owner_id,),
        ).fetchall()
    return {row["document_id"]: row["subject_id"] for row in rows}
