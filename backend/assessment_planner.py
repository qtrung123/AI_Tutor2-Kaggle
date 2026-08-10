import json
import re
import difflib

from langchain_ollama import ChatOllama

from config import (
    ASSESSMENT_PLANNER_CHUNKS_PER_BATCH,
    AUTO_QUIZ_MAX_DOCUMENT,
    AUTO_QUIZ_MAX_PER_TOPIC,
    CHAT_MODEL,
)


PLANNER_VERSION = "assessment_capacity_v1"


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(value).lower())).strip()


def _chunk_id(chunk: dict) -> str:
    return str((chunk.get("metadata") or {}).get("chunk_id") or "")


def _planner_context(chunks: list[dict]) -> str:
    parts = []
    for chunk in chunks:
        chunk_id = _chunk_id(chunk)
        content = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip()[:1200]
        if chunk_id and content:
            parts.append(f"[Chunk ID: {chunk_id}]\n{content}")
    return "\n\n".join(parts)


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def validate_and_deduplicate_concepts(raw_concepts: list, chunks: list[dict]) -> list[dict]:
    """Keep grounded, distinct concepts and assign stable topic-local IDs."""
    allowed = {_chunk_id(chunk) for chunk in chunks if _chunk_id(chunk)}
    concepts = []
    seen = set()
    for raw in raw_concepts or []:
        if not isinstance(raw, dict):
            continue
        name = re.sub(r"\s+", " ", str(raw.get("name") or raw.get("concept_name") or "")).strip()
        normalized = _key(name)
        evidence = list(dict.fromkeys(
            str(value).strip() for value in (raw.get("source_chunk_ids") or [])
            if str(value).strip() in allowed
        ))
        duplicate = normalized in seen or any(
            difflib.SequenceMatcher(None, normalized, existing).ratio() >= 0.88
            for existing in seen
        )
        if not normalized or duplicate or not evidence:
            continue
        seen.add(normalized)
        concepts.append({"name": name, "source_chunk_ids": evidence})
    return [
        {**concept, "concept_id": f"concept_{index:03d}"}
        for index, concept in enumerate(concepts[:max(0, AUTO_QUIZ_MAX_PER_TOPIC)], start=1)
    ]


def _plan_batch(topic_name: str, chunks: list[dict]) -> list[dict]:
    prompt = f"""
Identify distinct, educationally meaningful assessable concepts for the topic below.
Do not create questions. Do not include incidental trivia unless it is educationally important.
Merge redundant ideas. Every concept must cite one or more exact Chunk IDs that directly support it.
Return JSON only: {{"concepts":[{{"name":"concise concept label","source_chunk_ids":["exact id"]}}]}}

TOPIC: {topic_name}
CONTEXT:
{_planner_context(chunks)}
""".strip()
    response = ChatOllama(model=CHAT_MODEL, temperature=0, format="json", num_ctx=8192).invoke(prompt)
    data = _parse_json(response.content)
    return data.get("concepts") if isinstance(data.get("concepts"), list) else []


def build_topic_plan(topic: dict, chunks: list[dict]) -> dict:
    usable = [chunk for chunk in chunks if _chunk_id(chunk) and str(chunk.get("content") or "").strip()]
    raw_concepts = []
    batch_size = max(1, ASSESSMENT_PLANNER_CHUNKS_PER_BATCH)
    for start in range(0, len(usable), batch_size):
        try:
            raw_concepts.extend(_plan_batch(str(topic.get("name") or topic.get("topic_id")), usable[start:start + batch_size]))
        except Exception as error:
            print(f"[assessment-planner] discarded invalid planning batch: {error}")
    concepts = validate_and_deduplicate_concepts(raw_concepts, usable)
    return {
        "topic_id": str(topic["topic_id"]),
        "topic_name": str(topic.get("name") or topic["topic_id"]),
        "assessment_capacity": len(concepts),
        "allocated_questions": 0,
        "concepts": concepts,
    }


def allocate_document_topics(topic_plans: list[dict], cap: int = AUTO_QUIZ_MAX_DOCUMENT) -> dict:
    """Cover every assessable topic once before allocating richer-topic extras."""
    valid = [plan for plan in topic_plans if int(plan.get("assessment_capacity", 0)) > 0]
    cap = max(0, int(cap))
    if cap < len(valid):
        included = sorted(
            valid,
            key=lambda plan: (-int(plan["assessment_capacity"]), str(plan["topic_id"])),
        )[:cap]
        included_ids = {plan["topic_id"] for plan in included}
        excluded = [plan["topic_id"] for plan in valid if plan["topic_id"] not in included_ids]
    else:
        included = list(valid)
        excluded = []

    allocations = {plan["topic_id"]: 1 for plan in included}
    remaining = cap - len(included)
    while remaining > 0:
        candidates = [
            plan for plan in included
            if allocations[plan["topic_id"]] < int(plan["assessment_capacity"])
        ]
        if not candidates:
            break
        candidates.sort(key=lambda plan: (
            -(int(plan["assessment_capacity"]) - allocations[plan["topic_id"]]),
            str(plan["topic_id"]),
        ))
        allocations[candidates[0]["topic_id"]] += 1
        remaining -= 1

    planned = []
    for plan in topic_plans:
        allocation = allocations.get(plan["topic_id"], 0)
        planned.append({
            **plan,
            "allocated_questions": allocation,
            "selected_concepts": list(plan.get("concepts") or [])[:allocation],
        })
    return {
        "topics": planned,
        "total_questions": sum(allocations.values()),
        "excluded_topic_ids": excluded,
    }
