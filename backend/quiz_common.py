"""Quiz definitions shared by generation, attempts and the model benchmark.

Holds the supported difficulty set and the indexed-document lookup that every Quiz entry point
validates against. Kept separate from backend/quiz_service.py (generation) and
backend/quiz_attempt_service.py (attempt lifecycle) so neither service imports the other.
"""
from backend.auth_store import LEGACY_USER_ID
from backend.indexed_document_store import list_indexed_documents as load_owned_documents
from backend.quiz_store import documents_needing_topic_schema_invalidation, invalidate_document_quizzes_for_topic_schema


QUIZ_DIFFICULTIES = {"easy", "medium", "difficult"}


def _load_indexed_files(owner_id: str) -> dict:
    return {document["document_id"]: document for document in load_owned_documents(owner_id)}


def list_indexed_documents(owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """
    Return documents available for quiz generation.

    The hash is included internally so generated quizzes can remember which
    exact uploaded file version they belong to.

    Quizzes built for an older topic map are invalidated here, but only for the documents that
    still have such rows: one read finds them, so a plain listing performs no writes.
    """
    indexed_files = _load_indexed_files(owner_id)
    versions = {file_name: int(info.get("topic_schema_version", 0)) for file_name, info in indexed_files.items()}
    for file_name in sorted(documents_needing_topic_schema_invalidation(owner_id, versions)):
        invalidate_document_quizzes_for_topic_schema(file_name, versions[file_name], owner_id)
    return [
        {
            "id": file_name,
            "title": file_name,
            "chunks": int(info.get("chunks", 0)),
            "hash": str(info.get("hash", "")),
            "topic_schema_version": int(info.get("topic_schema_version", 0)),
            "topics": list(info.get("topics") or []),
        }
        for file_name, info in indexed_files.items()
    ]


def _document_lookup(owner_id: str) -> dict[str, dict]:
    return {document["id"]: document for document in list_indexed_documents(owner_id)}
