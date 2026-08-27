import difflib
import hashlib
import json
import re

from langchain_ollama import ChatOllama

from config import (
    AUTO_QUIZ_MAX_DOCUMENT,
    AUTO_QUIZ_MAX_PER_TOPIC,
    CHAT_MODEL,
    STRUCTURAL_EVIDENCE_MIN_CHUNK_RATIO,
    STRUCTURAL_EVIDENCE_MIN_OVERLAP_CHARS,
    STRUCTURAL_EVIDENCE_MIN_SPAN_RATIO,
    STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS,
)

PLANNER_VERSION = "hierarchy_concepts_v2"


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(value).lower())).strip()


def _chunk_id(chunk: dict) -> str:
    return str((chunk.get("metadata") or {}).get("chunk_id") or "").strip()


def _subtopic_id(chunk: dict) -> str:
    return str((chunk.get("metadata") or {}).get("subtopic_id") or "").strip()


def _compact(value: str) -> str:
    return re.sub(r"[^\w]", "", str(value), flags=re.UNICODE).casefold()


def _clip_to_boundary(chunk: dict, boundary: dict) -> tuple[dict | None, int]:
    """Return a copied canonical chunk clipped to one page-local boundary overlap."""
    metadata = dict(chunk.get("metadata") or {})
    content = str(chunk.get("content") or "")
    page = int(metadata.get("page", 0) or 0)
    chunk_start = int(metadata.get("start_index", 0) or 0)
    chunk_end = chunk_start + len(content)
    start = boundary.get("start") or {}
    end = boundary.get("end") or {}
    start_page = int(start.get("page_index", int(start.get("page", 1) or 1) - 1))
    end_page = int(end.get("page_index", int(end.get("page", 1) or 1) - 1))
    if page < start_page or page > end_page:
        return None, 0
    lower = int(start.get("char_offset", 0) or 0) if page == start_page else 0
    upper = int(end.get("char_offset", chunk_end) or 0) if page == end_page else chunk_end
    overlap_start, overlap_end = max(chunk_start, lower), min(chunk_end, upper)
    if overlap_end <= overlap_start:
        return None, 0
    relative_start, relative_end = overlap_start - chunk_start, overlap_end - chunk_start
    clipped = {
        "content": content[relative_start:relative_end],
        "metadata": {
            **metadata,
            "evidence_char_start": overlap_start,
            "evidence_char_end": overlap_end,
            "evidence_is_clipped": relative_start > 0 or relative_end < len(content),
        },
    }
    return clipped, overlap_end - overlap_start


def _clip_to_topic_gaps(chunk: dict, topic_boundary: dict, subtopic_boundaries: list[dict]) -> tuple[dict | None, int]:
    """Clip a chunk to top-level material not owned by any structural subtopic."""
    clipped, overlap = _clip_to_boundary(chunk, topic_boundary)
    if not clipped:
        return None, 0
    metadata = clipped["metadata"]
    page = int(metadata.get("page", 0) or 0)
    segments = [(int(metadata["evidence_char_start"]), int(metadata["evidence_char_end"]))]
    for boundary in subtopic_boundaries:
        start, end = boundary.get("start") or {}, boundary.get("end") or {}
        start_page = int(start.get("page_index", int(start.get("page", 1) or 1) - 1))
        end_page = int(end.get("page_index", int(end.get("page", 1) or 1) - 1))
        if page < start_page or page > end_page:
            continue
        lower = int(start.get("char_offset", 0) or 0) if page == start_page else 0
        upper = int(end.get("char_offset", 10**12) or 0) if page == end_page else 10**12
        remaining = []
        for left, right in segments:
            if upper <= left or lower >= right:
                remaining.append((left, right))
            else:
                if left < lower:
                    remaining.append((left, lower))
                if upper < right:
                    remaining.append((upper, right))
        segments = remaining
    original = str(chunk.get("content") or "")
    chunk_start = int((chunk.get("metadata") or {}).get("start_index", 0) or 0)
    parts = [original[left - chunk_start:right - chunk_start] for left, right in segments if right > left]
    text = "\n".join(part for part in parts if part)
    if not text:
        return None, 0
    return {"content": text, "metadata": {**metadata, "evidence_is_clipped": True}}, sum(len(part) for part in parts)


def _legacy_primary_seeds(topic: dict, chunks: list[dict]) -> list[dict]:
    """Compatibility path for documents without persisted structural boundaries."""
    topic_id = str(topic.get("topic_id") or "").strip()
    allowed = {str(item.get("subtopic_id") or "").strip(): item for item in topic.get("subtopics") or []}
    grouped, order, seen = {}, [], set()
    for chunk in chunks:
        metadata, chunk_id = chunk.get("metadata") or {}, _chunk_id(chunk)
        if not chunk_id or chunk_id in seen or not str(chunk.get("content") or "").strip():
            continue
        if str(metadata.get("topic_id") or "").strip() not in {"", topic_id}:
            raise ValueError(f"Chunk {chunk_id} is outside selected topic {topic_id}.")
        subtopic_id = _subtopic_id(chunk)
        if subtopic_id and subtopic_id not in allowed:
            raise ValueError(f"Chunk {chunk_id} cites invalid subtopic {subtopic_id}.")
        seen.add(chunk_id)
        if subtopic_id not in grouped:
            grouped[subtopic_id], order = [], [*order, subtopic_id]
        grouped[subtopic_id].append(chunk)
    return _seeds_from_groups(topic, grouped, order)


def _seeds_from_groups(topic: dict, grouped: dict[str, list[dict]], order: list[str]) -> list[dict]:
    topic_id = str(topic.get("topic_id") or "").strip()
    subtopics = {str(item.get("subtopic_id") or "").strip(): item for item in topic.get("subtopics") or []}
    seeds = []
    for index, subtopic_id in enumerate(order):
        group = grouped.get(subtopic_id) or []
        if not group:
            continue
        subtopic = subtopics.get(subtopic_id) or {}
        seeds.append({
            "seed_id": subtopic_id or f"{topic_id}:topic-evidence:{index + 1}",
            "subtopic_id": subtopic_id,
            "name": str(subtopic.get("name") or topic.get("name") or topic_id),
            "heading_path": (group[0].get("metadata") or {}).get("heading_path", ""),
            "structure_confidence": float((group[0].get("metadata") or {}).get("structure_confidence") or 0),
            "source_chunk_ids": list(dict.fromkeys(_chunk_id(chunk) for chunk in group)),
            "chunks": group,
            "order": index,
        })
    return seeds


def build_structural_seeds(topic: dict, chunks: list[dict]) -> list[dict]:
    """Build multi-membership seeds from meaningful clipped boundary overlaps."""
    if not isinstance(topic.get("boundary"), dict):
        return _legacy_primary_seeds(topic, chunks)

    subtopics = [item for item in topic.get("subtopics") or [] if isinstance(item.get("boundary"), dict)]
    grouped: dict[str, list[dict]] = {}
    used_memberships: set[tuple[str, str]] = set()
    for subtopic in subtopics:
        subtopic_id = str(subtopic.get("subtopic_id") or "").strip()
        boundary = subtopic["boundary"]
        span = max(1, int(boundary["end"].get("document_offset", 0)) - int(boundary["start"].get("document_offset", 0)))
        candidates = []
        for chunk in chunks:
            clipped, overlap = _clip_to_boundary(chunk, boundary)
            if clipped and _chunk_id(chunk):
                candidates.append((chunk, clipped, overlap))
        for chunk, clipped, overlap in candidates:
            content_length = max(1, len(str(chunk.get("content") or "")))
            heading_present = _compact(subtopic.get("name", "")) in _compact(clipped["content"])
            heading_with_body = heading_present and overlap >= len(str(subtopic.get("name") or "")) + 16
            only_chunk = len(candidates) == 1 and overlap >= STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS
            ratio_evidence = overlap >= STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS and (
                overlap / content_length >= STRUCTURAL_EVIDENCE_MIN_CHUNK_RATIO
                or overlap / span >= STRUCTURAL_EVIDENCE_MIN_SPAN_RATIO
            )
            meaningful = (
                overlap >= STRUCTURAL_EVIDENCE_MIN_OVERLAP_CHARS
                or ratio_evidence
                or heading_with_body
                or only_chunk
            )
            if not meaningful:
                continue
            clipped["metadata"]["evidence_subtopic_id"] = subtopic_id
            membership = (subtopic_id, _chunk_id(chunk))
            if membership not in used_memberships:
                grouped.setdefault(subtopic_id, []).append(clipped)
                used_memberships.add(membership)

    # Preserve topic-level evidence only when its primary assignment is this
    # topic with no subtopic, clipped to the top-level interval. Do not duplicate
    # a chunk already used by one of this topic's structural seeds.
    topic_id = str(topic.get("topic_id") or "").strip()
    used_chunk_ids = {chunk_id for _, chunk_id in used_memberships}
    topic_group = []
    for chunk in chunks:
        metadata, chunk_id = chunk.get("metadata") or {}, _chunk_id(chunk)
        if not chunk_id or chunk_id in used_chunk_ids:
            continue
        if str(metadata.get("topic_id") or "").strip() != topic_id or _subtopic_id(chunk):
            continue
        clipped, overlap = _clip_to_topic_gaps(
            chunk, topic["boundary"], [subtopic["boundary"] for subtopic in subtopics]
        )
        if clipped and overlap >= STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS:
            clipped["metadata"]["evidence_subtopic_id"] = ""
            topic_group.append(clipped)
    if topic_group:
        grouped[""] = topic_group

    structural_order = [str(item.get("subtopic_id") or "") for item in subtopics]
    order = ([""] if "" in grouped else []) + structural_order
    return _seeds_from_groups(topic, grouped, order)


def resolve_concept_evidence(topic: dict, chunks: list[dict], concept: dict) -> list[dict]:
    """Resolve canonical IDs to decontaminated clipped evidence fragments."""
    seeds = build_structural_seeds(topic, chunks)
    cited_subtopics = set(concept.get("source_subtopic_ids") or [])
    allowed_seed_ids = cited_subtopics or {""}
    fragments: dict[str, list[dict]] = {}
    for seed in seeds:
        if seed["subtopic_id"] not in allowed_seed_ids:
            continue
        for chunk in seed["chunks"]:
            if _chunk_id(chunk) in set(concept.get("source_chunk_ids") or []):
                fragments.setdefault(_chunk_id(chunk), []).append(chunk)
    resolved = []
    for chunk_id in concept.get("source_chunk_ids") or []:
        parts = fragments.get(chunk_id) or []
        if not parts:
            continue
        resolved.append({
            "content": "\n".join(str(part.get("content") or "").strip() for part in parts if str(part.get("content") or "").strip()),
            "metadata": {**(parts[0].get("metadata") or {}), "chunk_id": chunk_id},
        })
    return resolved


def _planner_context(seeds: list[dict]) -> str:
    parts = []
    for seed in seeds:
        parts.append(f"[Structural seed: {seed['subtopic_id'] or 'TOPIC_LEVEL'} | {seed['name']}]")
        for chunk in seed["chunks"]:
            content = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip()[:1200]
            parts.append(f"[Chunk ID: {_chunk_id(chunk)}]\n{content}")
    return "\n\n".join(parts)


def _parse_json(text: str) -> dict:
    cleaned = str(text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def _plan_seeds(topic_name: str, seeds: list[dict]) -> list[dict]:
    seed_ids = [seed["subtopic_id"] for seed in seeds if seed["subtopic_id"]]
    prompt = f"""
Identify distinct, educationally meaningful assessment concepts for this topic.
Structural subtopics are seeds, not mandatory one-to-one concepts. You may split a broad seed,
merge small or redundant seeds, or derive concepts from TOPIC_LEVEL evidence.
Every concept must cite exact supplied Chunk IDs and only applicable structural subtopic IDs.
Return only name, source_subtopic_ids, and source_chunk_ids for each concept.
Return JSON only: {{"concepts":[{{"name":"label","source_subtopic_ids":[],"source_chunk_ids":["exact id"]}}]}}

TOPIC: {topic_name}
ALLOWED SUBTOPIC IDS: {json.dumps(seed_ids, ensure_ascii=False)}
CONTEXT:
{_planner_context(seeds)}
""".strip()
    response = ChatOllama(model=CHAT_MODEL, temperature=0, format="json", num_ctx=8192).invoke(prompt)
    data = _parse_json(response.content)
    return data.get("concepts") if isinstance(data.get("concepts"), list) else []


def _fallback_concepts(seeds: list[dict]) -> list[dict]:
    concepts = []
    for seed in seeds:
        if seed["subtopic_id"]:
            concepts.append({"name": seed["name"], "source_subtopic_ids": [seed["subtopic_id"]], "source_chunk_ids": list(seed["source_chunk_ids"])})
        else:
            for chunk in seed["chunks"]:
                content = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip()
                excerpts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", content) if len(part.split()) >= 3]
                if not excerpts:
                    words = content.split()
                    size = max(8, min(35, len(words) // max(1, min(AUTO_QUIZ_MAX_PER_TOPIC, 5))))
                    excerpts = [" ".join(words[start:start + size]) for start in range(0, len(words), size) if words[start:start + size]]
                for excerpt in excerpts:
                    label = " ".join(re.findall(r"[\w'/-]+", excerpt, flags=re.UNICODE)[:8])
                    concepts.append({"name": f"{seed['name']}: {label or 'topic evidence'}", "source_subtopic_ids": [], "source_chunk_ids": [_chunk_id(chunk)]})
    return concepts


def _concept_id(topic_id: str, name: str, subtopic_ids: list[str], chunk_ids: list[str]) -> str:
    identity = json.dumps([PLANNER_VERSION, topic_id, _key(name), sorted(subtopic_ids), sorted(chunk_ids)], ensure_ascii=False)
    return f"aconcept_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def validate_and_deduplicate_concepts(raw_concepts: list, chunks: list[dict], topic: dict | None = None, seeds: list[dict] | None = None) -> list[dict]:
    """Validate evidence and structural lineage, then assign backend-owned IDs."""
    topic = topic or {"topic_id": "legacy-topic", "subtopics": []}
    seeds = seeds if seeds is not None else build_structural_seeds(topic, chunks)
    allowed_chunks = {_chunk_id(chunk): chunk for chunk in chunks if _chunk_id(chunk)}
    allowed_subtopics = {seed["subtopic_id"] for seed in seeds if seed["subtopic_id"]}
    memberships_by_chunk: dict[str, set[str]] = {}
    for seed in seeds:
        for chunk_id in seed.get("source_chunk_ids") or []:
            memberships_by_chunk.setdefault(chunk_id, set()).add(seed["subtopic_id"])
    concepts, seen_names = [], []
    for raw in raw_concepts or []:
        if not isinstance(raw, dict):
            continue
        name = re.sub(r"\s+", " ", str(raw.get("name") or raw.get("concept_name") or "")).strip()
        normalized = _key(name)
        subtopic_ids = list(dict.fromkeys(str(value).strip() for value in (raw.get("source_subtopic_ids") or []) if str(value).strip()))
        chunk_ids = list(dict.fromkeys(str(value).strip() for value in (raw.get("source_chunk_ids") or []) if str(value).strip()))
        if not normalized or not chunk_ids:
            continue
        if any(value not in allowed_subtopics for value in subtopic_ids) or any(value not in allowed_chunks for value in chunk_ids):
            continue
        if not all(
            bool(memberships_by_chunk.get(value, set()).intersection(subtopic_ids))
            if subtopic_ids else "" in memberships_by_chunk.get(value, set())
            for value in chunk_ids
        ):
            continue
        if subtopic_ids and any(
            not any(subtopic_id in memberships_by_chunk.get(chunk_id, set()) for chunk_id in chunk_ids)
            for subtopic_id in subtopic_ids
        ):
            continue
        if any(normalized == old or difflib.SequenceMatcher(None, normalized, old).ratio() >= 0.88 for old in seen_names):
            continue
        seen_names.append(normalized)
        concepts.append({"name": name, "source_subtopic_ids": subtopic_ids, "source_chunk_ids": chunk_ids})

    single_seed_counts = {}
    for concept in concepts:
        ids = concept["source_subtopic_ids"]
        if len(ids) == 1:
            single_seed_counts[ids[0]] = single_seed_counts.get(ids[0], 0) + 1
    subtopic_names = {seed["subtopic_id"]: _key(seed["name"]) for seed in seeds if seed["subtopic_id"]}
    for concept in concepts:
        source_subtopics = concept["source_subtopic_ids"]
        if not source_subtopics or (len(source_subtopics) == 1 and single_seed_counts[source_subtopics[0]] > 1):
            origin = "derived"
        elif len(source_subtopics) > 1:
            origin = "refined"
        else:
            similarity = difflib.SequenceMatcher(None, _key(concept["name"]), subtopic_names[source_subtopics[0]]).ratio()
            origin = "structural" if similarity >= 0.88 else "refined"
        concept["concept_origin"] = origin
        concept["concept_id"] = _concept_id(str(topic.get("topic_id") or ""), concept["name"], source_subtopics, concept["source_chunk_ids"])
    chunk_order = {_chunk_id(chunk): index for index, chunk in enumerate(chunks)}
    concepts.sort(key=lambda concept: min(chunk_order.get(chunk_id, 10**9) for chunk_id in concept["source_chunk_ids"]))
    return concepts[:max(0, AUTO_QUIZ_MAX_PER_TOPIC)]


def _concept_plan_id(topic_id: str, concepts: list[dict]) -> str:
    identity = json.dumps([PLANNER_VERSION, topic_id, [concept["concept_id"] for concept in concepts]])
    return f"conceptplan_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def build_topic_plan(topic: dict, chunks: list[dict]) -> dict:
    seeds = build_structural_seeds(topic, chunks)
    raw_concepts = []
    if seeds and any(seed["subtopic_id"] for seed in seeds):
        try:
            raw_concepts = _plan_seeds(str(topic.get("name") or topic.get("topic_id")), seeds)
        except Exception as error:
            print(f"[assessment-planner] using structural fallback: {error}")
    concepts = validate_and_deduplicate_concepts(raw_concepts, chunks, topic, seeds)
    if not concepts:
        concepts = validate_and_deduplicate_concepts(_fallback_concepts(seeds), chunks, topic, seeds)
    plan_id = _concept_plan_id(str(topic["topic_id"]), concepts)
    return {
        "topic_id": str(topic["topic_id"]), "topic_name": str(topic.get("name") or topic["topic_id"]),
        "planner_version": PLANNER_VERSION, "concept_plan_id": plan_id,
        "assessment_capacity": len(concepts), "allocated_questions": 0,
        "concepts": concepts, "structural_seed_count": len(seeds),
    }


def allocate_document_topics(topic_plans: list[dict], cap: int = AUTO_QUIZ_MAX_DOCUMENT) -> dict:
    """Cover every assessable top-level topic before allocating extras."""
    valid = [plan for plan in topic_plans if int(plan.get("assessment_capacity", 0)) > 0]
    cap = max(0, int(cap))
    if cap < len(valid):
        included = sorted(valid, key=lambda plan: (-int(plan["assessment_capacity"]), str(plan["topic_id"])))[:cap]
        included_ids = {plan["topic_id"] for plan in included}
        excluded = [plan["topic_id"] for plan in valid if plan["topic_id"] not in included_ids]
    else:
        included, excluded = list(valid), []
    allocations = {plan["topic_id"]: 1 for plan in included}
    remaining = cap - len(included)
    while remaining > 0:
        candidates = [plan for plan in included if allocations[plan["topic_id"]] < int(plan["assessment_capacity"])]
        if not candidates:
            break
        candidates.sort(key=lambda plan: (-(int(plan["assessment_capacity"]) - allocations[plan["topic_id"]]), str(plan["topic_id"])))
        allocations[candidates[0]["topic_id"]] += 1
        remaining -= 1
    planned = []
    for plan in topic_plans:
        allocation = allocations.get(plan["topic_id"], 0)
        planned.append({**plan, "allocated_questions": allocation, "selected_concepts": list(plan.get("concepts") or [])[:allocation]})
    return {"topics": planned, "total_questions": sum(allocations.values()), "excluded_topic_ids": excluded}
