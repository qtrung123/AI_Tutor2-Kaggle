"""Document chunk vectors: PostgreSQL + pgvector when DATABASE_URL is set, local Chroma otherwise.

PgVectorStore implements exactly the slice of the Chroma API the app uses (get / add_documents /
delete / similarity_search[_with_score] / metadata updates), so ingestion, RAG chat and the
Quiz/Summary/Flashcard chunk readers work unchanged on either backend. Chunks, their text,
metadata, owner and document identity live in the document_chunks table, which survives a Kaggle
reset: a fresh runtime queries stored vectors without re-embedding anything.

Search is exact cosine distance over the rows that match the owner/document filter (no ANN index):
results are never lost to post-filtering, and every query is scoped to one user's documents.
pgvector is required for PostgreSQL; there is no silent fallback to the ephemeral local Chroma.
"""

import functools
import json
import re
import threading
from datetime import datetime, timezone

from backend import db, perf_timing
from config import COLLECTION_NAME, DATABASE_PATH, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, VECTORSTORE_DIR

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN_FILTERS = {"owner_id", "document_id"}
_vector_schemas: dict[str, str] = {}
_vector_schema_lock = threading.Lock()

PGVECTOR_SETUP_SQL = "create extension if not exists vector with schema extensions;"


class VectorStoreUnavailableError(RuntimeError):
    """PostgreSQL is selected but pgvector cannot be used; the message carries the fix."""


@functools.lru_cache(maxsize=None)
def _embeddings():
    # One client per process: building one opens new HTTP clients (~0.3-0.6 s), which every Chat
    # request otherwise paid before its retrieval even started.
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=EMBEDDING_MODEL)


def _vector_literal(values) -> str:
    vector = [float(value) for value in values]
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Embedding model {EMBEDDING_MODEL} returned {len(vector)} dimensions; "
            f"expected {EMBEDDING_DIMENSIONS} (EMBEDDING_DIMENSIONS)."
        )
    return "[" + ",".join(repr(value) for value in vector) + "]"


def _parse_vector(text) -> list[float]:
    return [float(value) for value in str(text).strip("[]").split(",") if value]


def _chunk_index(vector_id: str, fallback: int) -> int:
    match = re.search(r"_(\d+)$", str(vector_id))
    return int(match.group(1)) if match else fallback


# -- schema -----------------------------------------------------------------------------------

def _ensure_pgvector(connection: db.Connection) -> str:
    """Return the schema that holds the vector extension, creating the extension if allowed."""
    def installed() -> str | None:
        row = connection.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'vector'"
        ).fetchone()
        return row[0] if row else None

    schema = installed()
    if schema is None:
        has_extensions_schema = connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'"
        ).fetchone()
        try:
            connection.executescript(
                "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions" if has_extensions_schema
                else "CREATE EXTENSION IF NOT EXISTS vector"
            )
        except Exception as error:
            raise VectorStoreUnavailableError(
                "PostgreSQL is selected but the pgvector extension is not installed and could not be "
                f"created ({type(error).__name__}). In the Supabase SQL editor run: {PGVECTOR_SETUP_SQL}"
            ) from None
        schema = installed()
    if not schema or not _IDENTIFIER.match(schema):
        raise VectorStoreUnavailableError(f"Unsupported pgvector schema name. Run: {PGVECTOR_SETUP_SQL}")
    return schema


def _initialize_schema(connection: db.Connection) -> None:
    schema = _ensure_pgvector(connection)
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS document_chunks (
            vector_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            metadata_json JSONB NOT NULL,
            embedding "{schema}".vector NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dimensions INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_document_chunks_owner_document
            ON document_chunks(owner_id, document_id, chunk_index);
        """
    )
    with _vector_schema_lock:
        _vector_schemas[db.database_url()] = schema


def initialize_vector_store() -> None:
    """Create the pgvector table (PostgreSQL only); raises VectorStoreUnavailableError with the fix."""
    if db.selected_backend() != db.POSTGRES:
        return
    from backend.auth_store import initialize_auth_store

    initialize_auth_store()
    db.ensure_schema("document_chunks", DATABASE_PATH, _initialize_schema)


def _vector_schema() -> str:
    initialize_vector_store()
    with _vector_schema_lock:
        schema = _vector_schemas.get(db.database_url())
    if schema is None:  # schema cache reset (tests) after this process already initialized
        with db.connect(DATABASE_PATH) as connection:
            schema = _ensure_pgvector(connection)
        with _vector_schema_lock:
            _vector_schemas[db.database_url()] = schema
    return schema


# -- filters ----------------------------------------------------------------------------------

def _where(filter_: dict | None) -> tuple[list[str], list]:
    """Translate the Chroma `where` subset used by the app into SQL clauses + parameters."""
    if not filter_:
        return [], []
    clauses, params = [], []
    for key, value in filter_.items():
        if key == "$and":
            for item in value:
                sub_clauses, sub_params = _where(item)
                clauses.extend(sub_clauses)
                params.extend(sub_params)
            continue
        if key.startswith("$"):
            raise ValueError(f"Unsupported vector filter operator: {key}")
        if key in _COLUMN_FILTERS:
            column = key
        elif _IDENTIFIER.match(key):
            column = f"metadata_json->>'{key}'"
        else:
            raise ValueError(f"Unsupported vector filter key: {key!r}")
        if isinstance(value, dict):
            (operator, operand), = value.items()
            if operator == "$in":
                values = [str(item) for item in operand]
                if not values:
                    clauses.append("FALSE")
                    continue
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params.extend(values)
            elif operator == "$eq":
                clauses.append(f"{column} = ?")
                params.append(str(operand))
            else:
                raise ValueError(f"Unsupported vector filter operator: {operator}")
        else:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    return clauses, params


# -- store ------------------------------------------------------------------------------------

class PgVectorStore:
    """Durable chunk store in PostgreSQL + pgvector with the Chroma calls the app relies on."""

    is_durable = True

    def __init__(self, embedding_function=None):
        self._embedding_function = embedding_function
        self._schema = _vector_schema()

    @property
    def embeddings(self):
        if self._embedding_function is None:
            self._embedding_function = _embeddings()
        return self._embedding_function

    def _connect(self) -> db.Connection:
        return db.connect(DATABASE_PATH)

    def add_documents(self, documents, ids: list[str]) -> list[str]:
        """Embed and upsert chunks; re-running with the same ids is idempotent."""
        documents = list(documents)
        if len(documents) != len(ids):
            raise ValueError("Every chunk needs exactly one id.")
        if not documents:
            return []
        with perf_timing.stage("embed"):
            vectors = self.embeddings.embed_documents([document.page_content for document in documents])
        perf_timing.count(embedded_chunks=len(documents))
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for position, (document, vector_id, vector) in enumerate(zip(documents, ids, vectors)):
            metadata = dict(document.metadata or {})
            owner_id, document_id = str(metadata.get("owner_id") or ""), str(metadata.get("document_id") or "")
            if not owner_id or not document_id:
                raise ValueError("Chunk metadata must carry owner_id and document_id.")
            rows.append((
                str(vector_id), owner_id, document_id, _chunk_index(vector_id, position), document.page_content,
                json.dumps(metadata, ensure_ascii=False, default=str), _vector_literal(vector),
                EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, now,
            ))
        schema = self._schema
        with perf_timing.stage("insert"), self._connect() as connection:
            connection.executemany(
                f"""
                INSERT INTO document_chunks (vector_id, owner_id, document_id, chunk_index, content,
                    metadata_json, embedding, embedding_model, embedding_dimensions, created_at)
                VALUES (?, ?, ?, ?, ?, ?::jsonb, ?::"{schema}".vector, ?, ?, ?)
                ON CONFLICT (vector_id) DO UPDATE SET
                    document_id = excluded.document_id, chunk_index = excluded.chunk_index,
                    content = excluded.content, metadata_json = excluded.metadata_json,
                    embedding = excluded.embedding, embedding_model = excluded.embedding_model,
                    embedding_dimensions = excluded.embedding_dimensions, created_at = excluded.created_at
                WHERE document_chunks.owner_id = excluded.owner_id
                """,
                rows,
            )
        return list(ids)

    def get(self, ids: list[str] | None = None, where: dict | None = None, limit: int | None = None,
            include: list[str] | None = None) -> dict:
        include = ["documents", "metadatas"] if include is None else list(include)
        clauses, params = _where(where)
        clauses.insert(0, "embedding_model = ?")
        params.insert(0, EMBEDDING_MODEL)
        if ids is not None:
            ids = [str(item) for item in ids]
            if not ids:
                return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}
            clauses.append(f"vector_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        embedding_column = ", embedding::text AS embedding_text" if "embeddings" in include else ""
        sql = (
            f"SELECT vector_id, content, metadata_json{embedding_column} FROM document_chunks "
            f"WHERE {' AND '.join(clauses)} ORDER BY document_id, chunk_index, vector_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return {
            "ids": [row["vector_id"] for row in rows],
            "documents": [row["content"] for row in rows] if "documents" in include else None,
            "metadatas": [dict(row["metadata_json"]) for row in rows] if "metadatas" in include else None,
            "embeddings": [_parse_vector(row["embedding_text"]) for row in rows] if "embeddings" in include else None,
        }

    def delete(self, ids: list[str] | None = None) -> None:
        ids = [str(item) for item in ids or []]
        if not ids:
            return
        with self._connect() as connection:
            connection.execute(f"DELETE FROM document_chunks WHERE vector_id IN ({','.join('?' * len(ids))})", ids)

    def update_metadatas(self, ids: list[str], metadatas: list[dict]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE document_chunks SET metadata_json = ?::jsonb WHERE vector_id = ?",
                [(json.dumps(dict(metadata), ensure_ascii=False, default=str), str(vector_id))
                 for vector_id, metadata in zip(ids, metadatas)],
            )

    def similarity_search_with_score(self, query: str, k: int = 4, filter: dict | None = None) -> list[tuple]:
        """Nearest chunks by cosine distance (lower is closer), like Chroma's scored search."""
        from langchain_core.documents import Document

        if not filter:
            raise ValueError("Vector search must be scoped by a filter (owner_id/document_id).")
        clauses, params = _where(filter)
        schema = self._schema
        query_vector = _vector_literal(self.embeddings.embed_query(query))
        sql = (
            f"SELECT content, metadata_json, embedding OPERATOR(\"{schema}\".<=>) ?::\"{schema}\".vector AS distance "
            f"FROM document_chunks WHERE embedding_model = ? AND {' AND '.join(clauses)} "
            "ORDER BY distance, vector_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, [query_vector, EMBEDDING_MODEL, *params, int(k)]).fetchall()
        return [(Document(page_content=row["content"], metadata=dict(row["metadata_json"])), float(row["distance"]))
                for row in rows]

    def similarity_search(self, query: str, k: int = 4, filter: dict | None = None) -> list:
        return [document for document, _distance in self.similarity_search_with_score(query, k=k, filter=filter)]


def get_vectorstore():
    """The chunk store for the selected backend: pgvector on PostgreSQL, local Chroma on SQLite."""
    if db.selected_backend() == db.POSTGRES:
        return PgVectorStore()
    from langchain_chroma import Chroma

    return Chroma(
        persist_directory=str(VECTORSTORE_DIR),
        collection_name=COLLECTION_NAME,
        embedding_function=_embeddings(),
    )


def update_chunk_metadata(vectorstore, ids: list[str], metadatas: list[dict]) -> None:
    """Replace stored chunk metadata in place (no re-embedding) on either backend."""
    if hasattr(vectorstore, "update_metadatas"):
        vectorstore.update_metadatas(ids, metadatas)
    else:
        vectorstore._collection.update(ids=ids, metadatas=metadatas)
