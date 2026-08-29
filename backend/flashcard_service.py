"""Grounded one-call flashcard generation over existing owner-scoped topic chunks."""

import json
import re

from langchain_ollama import ChatOllama

from backend.flashcard_store import get_compatible_flashcards, save_flashcards
from backend.indexed_document_store import get_indexed_document
from backend.model_registry import resolve_generation_model
from backend.quiz_service import get_topic_chunks
from config import DEFAULT_GENERATION_MODEL

FLASHCARD_VERSION = "grounded_flashcards_v1"
MAX_CHARS_PER_CHUNK = 1400


def _json_object(content: str) -> dict:
    cleaned = str(content).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("Flashcard model did not return valid JSON.")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Flashcard model did not return a JSON object.")
    return value


def _prompt(document_id: str, evidence_groups: list[tuple[dict, list[dict]]]) -> str:
    blocks = []
    for topic, chunks in evidence_groups:
        evidence = []
        for chunk in chunks:
            metadata = chunk.get("metadata") or {}
            chunk_id = str(metadata.get("chunk_id") or metadata.get("chunk") or "")
            evidence.append(
                f"[chunk_id={chunk_id} subtopic_id={metadata.get('subtopic_id') or 'TOPIC_LEVEL'}]\n"
                f"{str(chunk.get('content') or '').strip()[:MAX_CHARS_PER_CHUNK]}"
            )
        blocks.append(
            f"TOPIC_ID: {topic['topic_id']}\nTOPIC_NAME: {topic.get('name') or topic['topic_id']}\n"
            f"EVIDENCE:\n{'\n\n'.join(evidence)}\nEND_TOPIC"
        )
    return f"""Create concise study flashcards for every topic block below.
Prioritize important definitions, concepts, characteristics, mechanisms, components, and comparisons.
For each card, front is a question or term and back is a clear answer or definition.
Use ONLY evidence from the card's own TOPIC block. Never transfer facts between topics or add outside facts.
Include source_chunk_ids that directly support each card. A subtopic_id is optional and must come from evidence metadata.
Do not pad to an exact count. Avoid duplicates. Return one topic group per input block, in the same order.
Return JSON only: {{"topics":[{{"topic_id":"id","cards":[{{"front":"...","back":"...",
"subtopic_id":"optional","source_chunk_ids":["..."]}}]}}]}}

DOCUMENT: {document_id}

{chr(10).join(blocks)}"""


def _topic_maps(document: dict) -> tuple[list[dict], dict[str, dict]]:
    topics = [topic for topic in document.get("topics") or [] if topic.get("topic_id")]
    return topics, {str(topic["topic_id"]): topic for topic in topics}


def generate_flashcards(owner_id: str, document_id: str, topic_ids: list[str] | None = None,
                        model_id: str | None = None) -> dict:
    document = get_indexed_document(owner_id, document_id)
    if not document:
        raise ValueError("Document not found.")
    topics, topic_by_id = _topic_maps(document)
    selected_ids = list(dict.fromkeys(str(value) for value in (topic_ids or [topic["topic_id"] for topic in topics])))
    if not selected_ids or any(topic_id not in topic_by_id for topic_id in selected_ids):
        raise ValueError("One or more selected topics are not part of this document.")
    # Restore backend-authoritative document order regardless of query/model ordering.
    selected = [topic for topic in topics if str(topic["topic_id"]) in set(selected_ids)]
    selected_ids = [str(topic["topic_id"]) for topic in selected]
    public_model = model_id or DEFAULT_GENERATION_MODEL
    runtime_model = resolve_generation_model(public_model)
    identity = {
        "owner_id": owner_id, "document_id": document_id, "document_hash": document.get("hash") or "",
        "topic_schema_version": int(document.get("topic_schema_version") or 0),
        "flashcard_version": FLASHCARD_VERSION, "model_id": public_model,
        "runtime_model": runtime_model, "topic_ids": selected_ids,
    }
    cached = get_compatible_flashcards(identity)
    if cached:
        return {**cached, **identity, "cache_hit": True, "llm_calls": 0}

    groups = [(topic, get_topic_chunks(document_id, str(topic["topic_id"]), owner_id)) for topic in selected]
    if any(not chunks for _, chunks in groups):
        raise ValueError("One or more selected topics have no indexed evidence.")
    response = ChatOllama(model=runtime_model, temperature=0.1, format="json").invoke(_prompt(document_id, groups))
    raw_topics = _json_object(response.content).get("topics")
    if not isinstance(raw_topics, list) or len(raw_topics) != len(selected):
        raise ValueError("Flashcard model returned an incomplete topic response.")

    cards, seen = [], set()
    for position, (topic, chunks) in enumerate(groups):
        raw_group = raw_topics[position]
        if not isinstance(raw_group, dict) or not isinstance(raw_group.get("cards"), list):
            raise ValueError("Flashcard model returned malformed cards.")
        subtopics = {str(item["subtopic_id"]): item for item in topic.get("subtopics") or [] if item.get("subtopic_id")}
        chunk_ids = {str((chunk.get("metadata") or {}).get("chunk_id") or (chunk.get("metadata") or {}).get("chunk") or "") for chunk in chunks}
        for raw in raw_group["cards"]:
            if not isinstance(raw, dict):
                continue
            front, back = str(raw.get("front") or "").strip(), str(raw.get("back") or "").strip()
            source_ids = list(dict.fromkeys(str(value) for value in raw.get("source_chunk_ids") or [] if str(value) in chunk_ids))
            subtopic_id = str(raw.get("subtopic_id") or "").strip() or None
            if not front or not back or not source_ids or (subtopic_id and subtopic_id not in subtopics):
                continue
            duplicate_key = (" ".join(front.lower().split()), " ".join(back.lower().split()))
            if duplicate_key in seen:
                continue
            seen.add(duplicate_key)
            cards.append({
                "topic_id": str(topic["topic_id"]), "topic_name": str(topic.get("name") or topic["topic_id"]),
                "subtopic_id": subtopic_id,
                "subtopic_name": str(subtopics[subtopic_id].get("name") or subtopic_id) if subtopic_id else None,
                "front": front, "back": back, "source_chunk_ids": source_ids,
            })
    if not cards:
        raise ValueError("Flashcard model returned no grounded cards.")
    saved = save_flashcards(identity, cards)
    return {**saved, **identity, "cache_hit": False, "llm_calls": 1}


def authoritative_card_fields(owner_id: str, document_id: str, topic_id: str,
                              subtopic_id: str | None = None) -> dict:
    document = get_indexed_document(owner_id, document_id)
    if not document:
        raise ValueError("Document not found.")
    topic = next((item for item in document.get("topics") or [] if str(item.get("topic_id")) == topic_id), None)
    if not topic:
        raise ValueError("Topic not found.")
    subtopic = None
    if subtopic_id:
        subtopic = next((item for item in topic.get("subtopics") or [] if str(item.get("subtopic_id")) == subtopic_id), None)
        if not subtopic:
            raise ValueError("Subtopic not found.")
    return {"topic_id": topic_id, "topic_name": str(topic.get("name") or topic_id),
            "subtopic_id": subtopic_id, "subtopic_name": str(subtopic.get("name") or subtopic_id) if subtopic else None}
