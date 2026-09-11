"""Overview-only subject/course grouping, derived from EXISTING document metadata.

No new persisted entity: this module never writes to the database. It is a pure, read-only
aggregation over the same per-document rows build_learning_dashboard already computes (document
name, topic count, assessed-topic count), grouped into subject-level cards for the Overview UI.
Quiz generation, flashcards, the RAG pipeline, document extraction, and the Study Planner are
untouched -- this only reorganizes how already-computed document metadata is presented.
"""

import re

_EXTENSION_PATTERN = re.compile(r"\.(pdf|docx?|pptx?|txt|md)$", re.IGNORECASE)
_SEPARATOR_PATTERN = re.compile(r"\s*[-–—:|]\s*")
_TRAILING_UNIT_PATTERN = re.compile(
    r"\s*\b(chapter|lecture|lesson|week|part|unit|module|section)\s*\d*\s*$", re.IGNORECASE
)
_TRAILING_NUMBER_PATTERN = re.compile(r"\s+\d+\s*$")


def derive_subject_name(document_name: str) -> str:
    """Best-effort subject/course name parsed out of a document's existing display name.

    Strips a trailing file extension, then either the part after a '<subject> - <chapter>'
    style separator, or a trailing 'Chapter 3' / 'Week 2' / lone-number suffix -- so
    'Biology - Chapter 1.pdf' and 'Biology - Chapter 2.pdf' (or 'Biology Week 1.pdf' /
    'Biology Week 2.pdf') both resolve to the subject 'Biology'. A name with no recognizable
    separator or suffix is returned as-is and becomes a subject of its own.
    """
    name = (document_name or "").strip()
    name = _EXTENSION_PATTERN.sub("", name).strip()
    if not name:
        return (document_name or "").strip() or "Untitled"

    parts = _SEPARATOR_PATTERN.split(name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return parts[0].strip()

    stripped = _TRAILING_UNIT_PATTERN.sub("", name).strip()
    stripped = _TRAILING_NUMBER_PATTERN.sub("", stripped).strip()
    return stripped or name


def group_documents_into_subjects(materials: list[dict]) -> list[dict]:
    """Group the dashboard's already-computed per-document rows into subject-level overview
    cards. `materials` is the same list build_learning_dashboard returns under "materials"
    (document_id, document_name, topic_count, assessed_topic_count) -- this only aggregates it,
    it never recomputes mastery or topic data itself.

    Returns one dict per subject: subject_id, subject_name, document_count, document_ids,
    primary_document_id (the first/most-recent document in that subject), topic_count,
    assessed_topic_count, and progress_percent (None when the subject has no topics yet).
    Sorted by subject_name for a stable, deterministic overview order.
    """
    subjects: dict[str, dict] = {}
    order: list[str] = []
    for material in materials:
        document_name = material.get("document_name") or material.get("document_id") or ""
        subject_name = derive_subject_name(document_name)
        key = subject_name.lower()
        if key not in subjects:
            subjects[key] = {
                "subject_id": key, "subject_name": subject_name,
                "document_ids": [], "document_count": 0,
                "topic_count": 0, "assessed_topic_count": 0,
            }
            order.append(key)
        entry = subjects[key]
        entry["document_ids"].append(material.get("document_id"))
        entry["document_count"] += 1
        entry["topic_count"] += int(material.get("topic_count") or 0)
        entry["assessed_topic_count"] += int(material.get("assessed_topic_count") or 0)

    result = []
    for key in order:
        entry = subjects[key]
        topic_count = entry["topic_count"]
        progress_percent = (
            round(100 * entry["assessed_topic_count"] / topic_count, 2) if topic_count > 0 else None
        )
        result.append({
            **entry,
            "primary_document_id": entry["document_ids"][0] if entry["document_ids"] else None,
            "progress_percent": progress_percent,
        })
    result.sort(key=lambda subject: subject["subject_name"].lower())
    return result
