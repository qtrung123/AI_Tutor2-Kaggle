"""Shared Chroma document-chunk retrieval used by Quiz, Summary, Flashcards and the Study Planner."""
import re
from pathlib import Path

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

from backend.assessment_planner import resolve_topic_evidence
from backend.auth_store import LEGACY_USER_ID
from backend.ingest import migrate_legacy_vector_ownership
from config import COLLECTION_NAME, EMBEDDING_MODEL, VECTORSTORE_DIR


def _load_vectorstore() -> Chroma:
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    store = Chroma(
        persist_directory=str(VECTORSTORE_DIR),
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
    )
    migrate_legacy_vector_ownership(store)
    return store


def _source_matches(source: str, document_id: str) -> bool:
    return Path(str(source)).name == document_id or str(source) == document_id


def _chunk_index_from_id(raw_id: str, fallback: int) -> int:
    match = re.search(r"_(\d+)$", str(raw_id))
    if match:
        return int(match.group(1))
    return fallback


def _result_to_chunks(result: dict, document_id: str, filter_source: bool) -> list[dict]:
    chunks = []
    ids = result.get("ids", []) or []
    documents = result.get("documents", []) or []
    metadatas = result.get("metadatas", []) or []

    for fallback_index, (raw_id, content, metadata) in enumerate(zip(ids, documents, metadatas)):
        metadata = metadata or {}
        if filter_source and not _source_matches(metadata.get("source", ""), document_id):
            continue

        chunk_index = _chunk_index_from_id(raw_id, fallback_index)
        chunks.append(
            {
                "content": content,
                "metadata": {
                    **metadata,
                    "chunk": chunk_index,
                    # vector_id is internal; chunk_id is the canonical persisted provenance ID.
                    "vector_id": str(raw_id),
                    "chunk_id": metadata.get("chunk_id"),
                },
            }
        )

    return sorted(chunks, key=lambda item: int((item.get("metadata") or {}).get("chunk", 0)))


def get_topic_chunks(document_id: str, topic_id: str, owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """
    Load all chunks for the selected document.

    Quiz generation intentionally does not use semantic top-k retrieval. It
    fetches the selected document's chunks and samples from the whole ordered
    list so the quiz can cover beginning, middle, and end material.
    """
    vectorstore = _load_vectorstore()

    try:
        result = vectorstore.get(
            where={"$and": [{"owner_id": owner_id}, {"document_id": document_id}, {"topic_id": topic_id}]}
        )
        chunks = _result_to_chunks(result, document_id, filter_source=False)
        if chunks:
            return chunks
    except Exception:
        pass

    result = vectorstore.get(where={"owner_id": owner_id}, limit=10000)
    return [
        chunk for chunk in _result_to_chunks(result, document_id, filter_source=True)
        if chunk["metadata"].get("owner_id") == owner_id
        and chunk["metadata"].get("document_id", chunk["metadata"].get("source")) == document_id
        and chunk["metadata"].get("topic_id") == topic_id
    ]


def get_document_chunks(document_id: str, owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """Load ordered document chunks for boundary-overlap evidence membership."""
    vectorstore = _load_vectorstore()
    try:
        result = vectorstore.get(where={"$and": [{"owner_id": owner_id}, {"document_id": document_id}]})
        chunks = _result_to_chunks(result, document_id, filter_source=False)
        if chunks:
            return chunks
    except Exception:
        pass
    result = vectorstore.get(where={"owner_id": owner_id}, limit=10000)
    return [
        chunk for chunk in _result_to_chunks(result, document_id, filter_source=True)
        if chunk["metadata"].get("owner_id") == owner_id
        and chunk["metadata"].get("document_id", chunk["metadata"].get("source")) == document_id
    ]


def get_schema_topic_evidence(document_id: str, topic: dict, owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """Retrieve owner/document-scoped evidence and isolate it to one schema boundary."""
    if not isinstance(topic.get("boundary"), dict):
        return get_topic_chunks(document_id, str(topic["topic_id"]), owner_id)
    return resolve_topic_evidence(topic, get_document_chunks(document_id, owner_id))
