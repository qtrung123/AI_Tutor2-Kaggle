"""Grounded full-document summarization over existing indexed chunks: topic notes in a few bounded
calls (missing topics retried once), then one synthesis call."""

import json
import re
import time

from backend.llm_backend import ChatOllama

from backend.indexed_document_store import get_indexed_document
from backend.llm_json import parse_json_object
from backend.model_registry import resolve_generation_model
from backend.prompt_loader import load_prompt, render_prompt
from backend.document_retrieval import get_schema_topic_evidence, get_topic_chunks
from backend.summary_store import get_compatible_summary, save_summary
from config import DEFAULT_GENERATION_MODEL, GENERATION_KEEP_ALIVE

SUMMARY_VERSION = "full_document_summary_v4_derived_sections_for_unstructured_documents"
# Earlier formats a saved summary is still served in. v4 changed the notes only for documents with no
# detected structure (one topic without subtopics); topic batching changed how notes are requested,
# not their shape. So a structured document's v3 summary stays valid instead of forcing every user to
# regenerate (new LLM calls); an unstructured document's v3 summary (no subsections) does not.
PREVIOUS_STRUCTURED_SUMMARY_VERSIONS = ("full_document_summary_v3_structured_study_notes_low_value_filter",)
MAX_CHARS_PER_CHUNK = 1200
MAX_DERIVED_SECTIONS = 6
DERIVE_SECTIONS_MARKER = "DERIVE_FROM_EVIDENCE"
# Topic notes are generated a few topics per call: asked for ten topics in one JSON reply, Qwen 2.5 7B
# may close the list after the first object ("returned 1 topic summaries for 10 requested"). Each
# call's prompt plus its reserved output must fit SUMMARY_NUM_CTX, so Ollama never silently drops the
# start of the prompt (the instructions) and a runaway reply stops at num_predict.
SUMMARY_NUM_CTX = 32768
SUMMARY_TOPICS_PER_CALL = 4
SUMMARY_OUTPUT_TOKENS_PER_TOPIC = 1500
SUMMARY_TOPIC_ATTEMPTS = 2   # the first call per batch, plus one retry for only the topics it missed
_CHARS_PER_TOKEN_ESTIMATE = 2   # conservative for mixed English/Vietnamese text (see flashcard_service)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_STOP_WORDS = {
    "about", "after", "also", "and", "are", "because", "been", "being", "between", "both",
    "can", "does", "each", "for", "from", "has", "have", "into", "its", "more", "only",
    "other", "over", "such", "summary", "than", "that", "the", "their", "these", "they",
    "this", "those", "through", "topic", "using", "was", "were", "which", "with", "within",
}
_OBVIOUSLY_UNRELATED_PATTERNS = (
    "unrelated to the supplied", "unrelated to the provided", "cannot summarize the provided",
    "cannot summarize the supplied", "no information was provided", "no evidence was provided",
)
_UNSUPPORTED_SUBTOPIC_PATTERNS = (
    "not detailed in the provided evidence", "not detailed in the supplied evidence",
    "not provided in the evidence", "not supported by the evidence", "insufficient evidence",
    "no information is provided", "no details are provided", "not discussed in the evidence",
    "not mentioned in the evidence", "the evidence does not describe", "the evidence does not explain",
)
_TRIVIAL_SUBTOPIC_WORDS = {
    "concept", "concepts", "covered", "covers", "described", "describes", "details", "discussed",
    "discusses", "explained", "explains", "important", "information", "overview", "section",
}


def _usage(response) -> dict:
    usage = dict(getattr(response, "usage_metadata", {}) or {})
    metadata = dict(getattr(response, "response_metadata", {}) or {})
    for source, target in (("prompt_eval_count", "input_tokens"), ("eval_count", "output_tokens"),
                           ("total_duration", "total_duration_ns")):
        if source in metadata and target not in usage:
            usage[target] = metadata[source]
    return usage


def _derives_sections(topics: list[dict]) -> bool:
    """One topic with no extracted subtopics (e.g. the topic_document_overview fallback) means the
    document has no usable structure, so its subsections are derived from the evidence instead."""
    return len(topics) == 1 and not topics[0].get("subtopics")


def accepted_summary_versions(topics: list[dict]) -> tuple[str, ...]:
    """The summary formats that count as this document's current summary (newest format first)."""
    return (SUMMARY_VERSION,) if _derives_sections(topics) else (SUMMARY_VERSION, *PREVIOUS_STRUCTURED_SUMMARY_VERSIONS)


def _topic_prompt(document_id: str, topic_evidence: list[tuple[dict, list[dict]]], derive: bool | None = None) -> str:
    # `derive` is decided for the whole document: a one-topic batch of a structured document is not
    # an unstructured document.
    if derive is None:
        derive = _derives_sections([topic for topic, _chunks in topic_evidence])
    sections = []
    for topic, chunks in topic_evidence:
        evidence = []
        for chunk in chunks:
            metadata = chunk.get("metadata") or {}
            content = str(chunk.get("content") or "").strip()[:MAX_CHARS_PER_CHUNK]
            evidence.append(
                f"[chunk_id={metadata.get('chunk_id') or metadata.get('chunk')} "
                f"page={metadata.get('page', 'unknown')} "
                f"subtopic_id={metadata.get('subtopic_id') or 'TOPIC_LEVEL'}]\n{content}"
            )
        joined_evidence = "\n\n".join(evidence)
        subtopics = [
            {"subtopic_id": str(item["subtopic_id"]), "subtopic_name": str(item.get("name") or item["subtopic_id"])}
            for item in topic.get("subtopics") or []
        ]
        subtopic_line = (
            f"SUBSECTIONS: {DERIVE_SECTIONS_MARKER}" if derive
            else f"REQUIRED_SUBTOPICS_IN_ORDER: {json.dumps(subtopics, ensure_ascii=False)}"
        )
        sections.append(
            f"TOPIC_ID: {topic['topic_id']}\n"
            f"TOPIC_NAME: {topic.get('name') or topic['topic_id']}\n"
            f"{subtopic_line}\n"
            f"EVIDENCE:\n{joined_evidence}\n"
            "END_TOPIC"
        )
    joined_sections = "\n\n".join(sections)
    derive_instructions = "\n" + render_prompt(
        "summary/derive_sections_instructions.txt", marker=DERIVE_SECTIONS_MARKER, max_sections=MAX_DERIVED_SECTIONS,
    ) + "\n" if derive else ""
    subsection_identity = load_prompt(
        "summary/subsection_identity_derived.txt" if derive else "summary/subsection_identity_required.txt"
    )
    return render_prompt(
        "summary/topic_notes.txt",
        derive_instructions=derive_instructions,
        subsection_identity=subsection_identity,
        document_id=document_id,
        topic_blocks=joined_sections,
    )


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(str(text)) if token.lower() not in _STOP_WORDS}


def _evidence_text(chunks: list[dict]) -> str:
    return "\n".join(str(chunk.get("content") or "") for chunk in chunks)


def _bind_ordered_responses(raw_items, requested_ids: list[str], id_field: str, label: str) -> tuple[list[dict], bool]:
    """Bind a complete model list to backend-owned identities and order."""
    if not isinstance(raw_items, list) or len(raw_items) != len(requested_ids):
        raise ValueError(
            f"Summary model returned {len(raw_items) if isinstance(raw_items, list) else 0} "
            f"{label} for {len(requested_ids)} requested {label}."
        )
    if any(not isinstance(item, dict) for item in raw_items):
        raise ValueError(f"Summary model returned an incomplete or ambiguous {label} response.")
    returned_ids = [str(item.get(id_field) or "") for item in raw_items]
    if len(set(returned_ids)) != len(returned_ids):
        raise ValueError(f"Summary model returned duplicate {label} IDs.")
    unknown = sorted(set(returned_ids) - set(requested_ids))
    if unknown:
        raise ValueError(f"Summary model returned unknown {label} IDs: {', '.join(unknown)}.")
    missing = sorted(set(requested_ids) - set(returned_ids))
    if missing:
        raise ValueError(f"Summary model omitted required {label} IDs: {', '.join(missing)}.")
    by_id = dict(zip(returned_ids, raw_items))
    return [by_id[item_id] for item_id in requested_ids], False


def _bind_topic_responses(raw_topics, topics: list[dict]) -> tuple[list[dict], bool]:
    """Bind a structurally complete response to backend-owned topic identities and order."""
    requested_ids = [str(topic["topic_id"]) for topic in topics]
    bound, rebound = _bind_ordered_responses(raw_topics, requested_ids, "topic_id", "topic summaries")
    if any(not str(item.get("overview") or "").strip() for item in bound):
        raise ValueError("Summary model returned an incomplete or ambiguous topic summary response.")
    return bound, rebound


def _normalized_subsection_content(item: dict) -> dict | None:
    content_type = str(item.get("content_type") or "").strip().lower()
    if content_type == "paragraph":
        text = str(item.get("paragraph") or "").strip()
        if not text:
            return None
        return {"type": "paragraph", "text": text}
    if content_type == "bullets":
        bullets = [str(value).strip() for value in item.get("bullets") or [] if str(value).strip()]
        if not bullets:
            return None
        return {"type": "bullets", "items": bullets}
    if content_type == "table":
        table = item.get("table") or {}
        headers = [str(value).strip() for value in table.get("headers") or [] if str(value).strip()]
        rows = [[str(value).strip() for value in row] for row in table.get("rows") or [] if isinstance(row, list)]
        if not headers and not rows:
            return None
        if not headers or not rows or any(len(row) != len(headers) for row in rows):
            raise ValueError("Structured summary table must have headers and equally sized rows.")
        return {"type": "table", "headers": headers, "rows": rows}
    raise ValueError("Structured summary subsection has an unsupported content_type.")


def _bind_optional_subtopics(raw_items, expected_subtopics: list[dict]) -> tuple[list[tuple[dict, dict]], bool]:
    """Bind an optional model subset to existing subtopics without inventing or merging identities."""
    if not isinstance(raw_items, list):
        raw_items = []
    expected_ids = [str(item["subtopic_id"]) for item in expected_subtopics]
    by_id = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        subtopic_id = str(item.get("subtopic_id") or "")
        if subtopic_id in expected_ids and subtopic_id not in by_id:
            by_id[subtopic_id] = item
    bound = [(subtopic, by_id[subtopic_id]) for subtopic, subtopic_id in zip(expected_subtopics, expected_ids) if subtopic_id in by_id]
    return bound, len(bound) != len(raw_items)


def _bind_derived_sections(raw_items, evidence_text: str) -> list[tuple[dict, dict]]:
    """Accept model-derived headings for an unstructured document only when they are grounded and distinct."""
    evidence_lower = evidence_text.lower()
    bound, seen = [], set()
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        heading = re.sub(r"\s+", " ", str(item.get("heading") or "")).strip()
        heading_tokens = _tokens(heading) - _TRIVIAL_SUBTOPIC_WORDS
        # Substring match also grounds headings in PDFs extracted without spaces between words.
        if not heading_tokens or heading.casefold() in seen or not any(token in evidence_lower for token in heading_tokens):
            continue
        seen.add(heading.casefold())
        bound.append(({"name": heading}, item))
        if len(bound) == MAX_DERIVED_SECTIONS:
            break
    return bound


def _content_text(content: dict) -> str:
    if content["type"] == "paragraph":
        return content["text"]
    if content["type"] == "bullets":
        return " ".join(content["items"])
    return " ".join([*content["headers"], *(cell for row in content["rows"] for cell in row)])


def _subsection_statements(content: dict) -> list[str]:
    if content["type"] == "paragraph":
        return [content["text"]]
    if content["type"] == "bullets":
        return list(content["items"])
    return [" ".join([*content["headers"], *row]) for row in content["rows"]]


def _is_low_value_subsection(subtopic: dict, content: dict | None) -> bool:
    if not content:
        return True
    heading_tokens = _tokens(str(subtopic.get("name") or subtopic.get("subtopic_id") or ""))
    for statement in _subsection_statements(content):
        normalized = statement.strip().lower()
        if not normalized or any(pattern in normalized for pattern in _UNSUPPORTED_SUBTOPIC_PATTERNS):
            continue
        meaningful_tokens = _tokens(statement) - _TRIVIAL_SUBTOPIC_WORDS
        if meaningful_tokens and not meaningful_tokens.issubset(heading_tokens):
            return False
    return True


def _grounding_warnings(summary_text: str, takeaways: list[str], own_tokens: set[str], other_tokens: set[str]) -> list[str]:
    warnings = []
    statements = [part.strip() for part in re.split(r"(?<=[.!?])\s+", summary_text) if part.strip()] + takeaways
    for index, statement in enumerate(statements, start=1):
        statement_tokens = _tokens(statement)
        if len(statement_tokens) < 2:
            continue
        own_overlap = statement_tokens & own_tokens
        foreign_only = statement_tokens & (other_tokens - own_tokens)
        if not own_overlap:
            warnings.append(f"statement_{index}_has_no_topic_evidence_overlap")
        if len(foreign_only) >= 2 and len(foreign_only) > len(own_overlap):
            warnings.append(f"statement_{index}_resembles_other_topic_evidence")
    return list(dict.fromkeys(warnings))


def _validated_topic_summary(index: int, item: dict, topics: list[dict], evidence: list[tuple[dict, list[dict]]],
                             evidence_tokens: list[set[str]], derive: bool) -> dict:
    """Validate one topic's reply against its own evidence; the other topics' evidence (from the whole
    document, whichever call produced them) is used only to flag cross-topic contamination."""
    topic = topics[index]
    overview = str(item["overview"]).strip()
    raw_subsections = item.get("subsections")
    if derive:
        bound_subsections = _bind_derived_sections(raw_subsections, _evidence_text(evidence[index][1]))
    else:
        bound_subsections, _ = _bind_optional_subtopics(raw_subsections, list(topic.get("subtopics") or []))
    normalized_subsections = []
    for subtopic, subsection in bound_subsections:
        content = _normalized_subsection_content(subsection)
        if _is_low_value_subsection(subtopic, content):
            continue
        # Derived sections get backend-owned IDs numbered after filtering, so they stay contiguous.
        subtopic_id = str(subtopic.get("subtopic_id") or f"{topic['topic_id']}_section_{len(normalized_subsections) + 1}")
        normalized_subsections.append({
            "subtopic_id": subtopic_id,
            "subtopic_name": str(subtopic.get("name") or subtopic_id),
            "content": content,
        })
    structured_text = " ".join([overview, *(_content_text(section["content"]) for section in normalized_subsections)])
    generated_tokens = _tokens(structured_text)
    if not generated_tokens or any(pattern in structured_text.lower() for pattern in _OBVIOUSLY_UNRELATED_PATTERNS):
        raise ValueError(f"Summary for topic {topic['topic_id']} is empty or obviously unrelated to its evidence.")
    own_tokens = evidence_tokens[index]
    overlap = generated_tokens & own_tokens
    required_overlap = 1 if len(generated_tokens) <= 4 else 2
    other_tokens = set().union(*(tokens for other_index, tokens in enumerate(evidence_tokens) if other_index != index))
    warnings = _grounding_warnings(
        overview, [_content_text(section["content"]) for section in normalized_subsections], own_tokens, other_tokens
    )
    if len(overlap) < required_overlap:
        warnings.insert(0, "low_lexical_support_from_topic_evidence")
    return {
        "topic_id": str(topic["topic_id"]),
        "topic_name": str(topic.get("name") or topic["topic_id"]),
        "overview": overview,
        "subsections": normalized_subsections,
        "grounding_warnings": list(dict.fromkeys(warnings)),
    }


def _validated_topic_summaries(raw_topics, topics: list[dict], evidence: list[tuple[dict, list[dict]]]) -> list[dict]:
    bound, _ = _bind_topic_responses(raw_topics, topics)
    evidence_tokens = [_tokens(_evidence_text(chunks)) for _topic, chunks in evidence]
    derive = _derives_sections(topics)
    return [_validated_topic_summary(index, item, topics, evidence, evidence_tokens, derive)
            for index, item in enumerate(bound)]


def _usable_topic_items(raw_topics, requested_ids: list[str]) -> dict[str, dict]:
    """The reply's objects for requested topics that carry an overview, keyed by topic_id. Unlike
    _bind_topic_responses this keeps every usable topic of a partial reply: unknown IDs are ignored,
    and a topic returned twice or without an overview is treated as missing (retried, never guessed)."""
    by_id: dict[str, dict] = {}
    repeated: set[str] = set()
    for item in raw_topics if isinstance(raw_topics, list) else []:
        if not isinstance(item, dict):
            continue
        topic_id = str(item.get("topic_id") or "")
        if topic_id not in requested_ids:
            continue
        if topic_id in by_id:
            repeated.add(topic_id)
        by_id[topic_id] = item
    return {topic_id: item for topic_id, item in by_id.items()
            if topic_id not in repeated and str(item.get("overview") or "").strip()}


def _token_estimate(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN_ESTIMATE


def _fit_topic_evidence(document_id: str, topic: dict, chunks: list[dict], derive: bool, budget_tokens: int) -> list[dict]:
    """A topic whose evidence alone exceeds the prompt budget keeps evenly spaced chunks in source
    order (the whole topic stays represented) instead of letting Ollama cut the prompt's start."""
    if _token_estimate(_topic_prompt(document_id, [(topic, chunks)], derive)) <= budget_tokens:
        return chunks
    keep = len(chunks)
    while keep > 1:
        keep = max(1, keep * 3 // 4)
        selected = [chunks[round(i * (len(chunks) - 1) / max(1, keep - 1))] for i in range(keep)] if keep > 1 else chunks[:1]
        if _token_estimate(_topic_prompt(document_id, [(topic, selected)], derive)) <= budget_tokens:
            return selected
    return chunks[:1]


def _topic_batches(document_id: str, evidence: list[tuple[dict, list[dict]]], derive: bool) -> tuple[list[list[int]], list[tuple[dict, list[dict]]], int]:
    """Group topic indexes (source order) into calls of at most SUMMARY_TOPICS_PER_CALL topics whose
    prompt fits the context left after the reserved output. Returns (batches, prompt evidence,
    number of topics whose evidence had to be sampled to fit)."""
    budget = SUMMARY_NUM_CTX - SUMMARY_TOPICS_PER_CALL * SUMMARY_OUTPUT_TOKENS_PER_TOPIC
    prompt_evidence, trimmed = [], 0
    for topic, chunks in evidence:
        fitted = _fit_topic_evidence(document_id, topic, chunks, derive, budget)
        trimmed += len(fitted) != len(chunks)
        prompt_evidence.append((topic, fitted))
    batches: list[list[int]] = []
    for index in range(len(prompt_evidence)):
        current = batches[-1] if batches else None
        if current and len(current) < SUMMARY_TOPICS_PER_CALL and _token_estimate(
                _topic_prompt(document_id, [prompt_evidence[i] for i in [*current, index]], derive)) <= budget:
            current.append(index)
        else:
            batches.append([index])
    return batches, prompt_evidence, trimmed


def _add_usage(total: dict, response) -> None:
    for key, value in _usage(response).items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value


def _generate_topic_summaries(llm, document_id: str, topics: list[dict], evidence: list[tuple[dict, list[dict]]]) -> tuple[list[dict], dict]:
    """Topic notes for every topic, in source order, a batch at a time. A batch's reply keeps every
    valid topic; only the topics it missed (omitted, duplicated, malformed JSON, failed validation)
    are asked for once more. Nothing is returned unless every topic has validated notes."""
    derive = _derives_sections(topics)
    evidence_tokens = [_tokens(_evidence_text(chunks)) for _topic, chunks in evidence]
    batches, prompt_evidence, trimmed = _topic_batches(document_id, evidence, derive)
    validated: dict[int, dict] = {}
    usage: dict = {}
    calls = retries = 0
    errors: list[str] = []
    for batch in batches:
        pending = list(batch)
        for attempt in range(SUMMARY_TOPIC_ATTEMPTS):
            if not pending:
                break
            calls += 1
            retries += attempt > 0
            requested_ids = [str(topics[index]["topic_id"]) for index in pending]
            started = time.perf_counter()
            response, items, failure = None, {}, ""
            try:
                response = llm.invoke(_topic_prompt(document_id, [prompt_evidence[index] for index in pending], derive))
                _add_usage(usage, response)
                items = _usable_topic_items(parse_json_object(response.content, "Summary").get("topics"), requested_ids)
            except Exception as error:   # malformed/truncated JSON or a raised model/runtime failure
                failure = str(error)
            for index in pending:
                item = items.get(str(topics[index]["topic_id"]))
                if item is None:
                    continue
                try:
                    validated[index] = _validated_topic_summary(index, item, topics, evidence, evidence_tokens, derive)
                except ValueError as error:
                    errors.append(str(error))
            missing = [index for index in pending if index not in validated]
            metadata = dict(getattr(response, "response_metadata", {}) or {})
            print(f"[summary] topic_call={calls} attempt={attempt + 1} topics_requested={len(pending)} "
                  f"topics_valid={len(pending) - len(missing)} done_reason={metadata.get('done_reason')} "
                  f"prompt_tokens={metadata.get('prompt_eval_count')} output_tokens={metadata.get('eval_count')} "
                  f"seconds={time.perf_counter() - started:.1f}" + (f" error={failure[:200]}" if failure else ""))
            if failure:
                errors.append(failure)
            pending = missing
        if pending:
            missing_ids = ", ".join(str(topics[index]["topic_id"]) for index in pending)
            detail = f" Last error: {errors[-1]}" if errors else ""
            raise ValueError(
                f"Summary model did not return valid notes for {len(pending)} of {len(topics)} topics "
                f"after {SUMMARY_TOPIC_ATTEMPTS} attempts: {missing_ids}.{detail}"
            )
    metrics = {"topic_calls": calls, "topic_retries": retries, "topic_batches": len(batches),
               "evidence_sampled_topics": trimmed}
    return [validated[index] for index in range(len(topics))], {**metrics, "usage": usage}


def _synthesis_prompt(document_id: str, topic_summaries: list[dict]) -> str:
    synthesis_evidence = [
        {key: item[key] for key in ("topic_id", "topic_name", "overview", "subsections")}
        for item in topic_summaries
    ]
    return render_prompt(
        "summary/document_synthesis.txt",
        document_id=document_id,
        topic_summaries=json.dumps(synthesis_evidence, ensure_ascii=False),
    )


def generate_document_summary(owner_id: str, document_id: str, model_id: str | None = None, regenerate: bool = False,
                              cache_only: bool = False) -> dict:
    total_started = time.perf_counter()
    document = get_indexed_document(owner_id, document_id)
    if not document:
        raise ValueError("Document not found.")
    topics = list(document.get("topics") or [])
    if not topics:
        raise ValueError("Document has no extracted topics to summarize.")
    selected_model = model_id or DEFAULT_GENERATION_MODEL
    runtime_model = resolve_generation_model(selected_model)
    identity = {
        "owner_id": owner_id, "document_id": document_id,
        "document_hash": str(document.get("hash") or ""),
        "topic_schema_version": int(document.get("topic_schema_version", 0)),
        "summary_version": SUMMARY_VERSION, "model_id": selected_model, "runtime_model": runtime_model,
    }
    if not regenerate:
        cached = get_compatible_summary(identity, accepted_summary_versions(topics))
        if cached:
            cached["cache_hit"] = True
            return cached
        if cache_only:
            # The screen only asks "does this summary exist?": answer without generating anything.
            return {"status": "not_generated", "document_id": document_id, "model_id": selected_model, "cache_hit": False}

    retrieval_started = time.perf_counter()
    evidence = [(
        topic,
        get_schema_topic_evidence(document_id, topic, owner_id)
        if isinstance(topic.get("boundary"), dict)
        else get_topic_chunks(document_id, str(topic["topic_id"]), owner_id),
    ) for topic in topics]
    evidence = [(topic, chunks) for topic, chunks in evidence if chunks]
    topics = [topic for topic, _chunks in evidence]
    if not topics:
        raise ValueError("Document has no topics with meaningful indexed evidence.")
    retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)

    llm = ChatOllama(model=runtime_model, temperature=0, format="json", num_ctx=SUMMARY_NUM_CTX,
                     keep_alive=GENERATION_KEEP_ALIVE,
                     num_predict=SUMMARY_TOPICS_PER_CALL * SUMMARY_OUTPUT_TOKENS_PER_TOPIC)
    topic_started = time.perf_counter()
    topic_summaries, topic_metrics = _generate_topic_summaries(llm, document_id, topics, evidence)
    topic_ms = round((time.perf_counter() - topic_started) * 1000)

    synthesis_started = time.perf_counter()
    synthesis_response = llm.invoke(_synthesis_prompt(document_id, topic_summaries))
    final_summary = parse_json_object(synthesis_response.content, "Summary")
    if not str(final_summary.get("overview") or "").strip():
        raise ValueError("Summary model omitted the final overview.")
    final_takeaways = [str(value).strip() for value in final_summary.get("key_takeaways") or [] if str(value).strip()]
    if not 4 <= len(final_takeaways) <= 7:
        raise ValueError("Summary model must return 4 to 7 document-level key takeaways.")
    final_summary = {"overview": str(final_summary["overview"]).strip(), "key_takeaways": final_takeaways}
    synthesis_ms = round((time.perf_counter() - synthesis_started) * 1000)
    topic_usage = topic_metrics.pop("usage")
    metrics = {"chunk_retrieval_ms": retrieval_ms, "topic_generation_ms": topic_ms,
               "synthesis_ms": synthesis_ms, "llm_calls": topic_metrics["topic_calls"] + 1, **topic_metrics,
               "topic_generation_usage": topic_usage, "synthesis_usage": _usage(synthesis_response)}
    persistence_started = time.perf_counter()
    stored = save_summary(identity, topic_summaries, final_summary, metrics)
    persistence_ms = round((time.perf_counter() - persistence_started) * 1000)
    metrics["persistence_ms"] = persistence_ms
    metrics["total_ms"] = round((time.perf_counter() - total_started) * 1000)
    stored["metrics"] = metrics
    stored["cache_hit"] = False
    print(f"[summary] owner={owner_id} document={document_id} model={selected_model} " +
          " ".join(f"{key}={value}" for key, value in metrics.items() if key.endswith("_ms") or key == "llm_calls") +
          f" token_metrics={{'topic': {metrics['topic_generation_usage']}, 'synthesis': {metrics['synthesis_usage']}}}")
    return stored
