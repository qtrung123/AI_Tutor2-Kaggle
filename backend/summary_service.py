"""Grounded, two-call full-document summarization over existing indexed chunks."""

import json
import re
import time

from langchain_ollama import ChatOllama

from backend.indexed_document_store import get_indexed_document
from backend.model_registry import resolve_generation_model
from backend.quiz_service import get_topic_chunks
from backend.summary_store import get_compatible_summary, save_summary
from config import DEFAULT_GENERATION_MODEL

SUMMARY_VERSION = "full_document_summary_v3_structured_study_notes"
MAX_CHARS_PER_CHUNK = 1200
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_STOP_WORDS = {
    "about", "after", "also", "and", "are", "because", "been", "being", "between", "both",
    "can", "does", "each", "for", "from", "has", "have", "into", "its", "more", "only",
    "other", "over", "such", "summary", "than", "that", "the", "their", "these", "they",
    "this", "those", "through", "topic", "using", "was", "were", "which", "with", "within",
}


def _json_object(content: str) -> dict:
    cleaned = str(content).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("Summary model did not return valid JSON.")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Summary model did not return a JSON object.")
    return value


def _usage(response) -> dict:
    usage = dict(getattr(response, "usage_metadata", {}) or {})
    metadata = dict(getattr(response, "response_metadata", {}) or {})
    for source, target in (("prompt_eval_count", "input_tokens"), ("eval_count", "output_tokens"),
                           ("total_duration", "total_duration_ns")):
        if source in metadata and target not in usage:
            usage[target] = metadata[source]
    return usage


def _topic_prompt(document_id: str, topic_evidence: list[tuple[dict, list[dict]]]) -> str:
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
        sections.append(
            f"TOPIC_ID: {topic['topic_id']}\n"
            f"TOPIC_NAME: {topic.get('name') or topic['topic_id']}\n"
            f"REQUIRED_SUBTOPICS_IN_ORDER: {json.dumps(subtopics, ensure_ascii=False)}\n"
            f"EVIDENCE:\n{joined_evidence}\n"
            "END_TOPIC"
        )
    joined_sections = "\n\n".join(sections)
    return f"""Summarize every requested topic using ONLY the evidence inside that topic's own block.
Treat every TOPIC_ID ... END_TOPIC block as isolated. Never transfer facts, terminology, examples,
properties, or conclusions between topics. If a fact is not supported inside the current topic block,
omit it from that topic's overview and subsections. If evidence is limited, say so plainly.
Return exactly one summary object per requested TOPIC_ID, in the same order, with no missing or extra objects.
For each topic, write one concise overview and use only subsection headings from REQUIRED_SUBTOPICS_IN_ORDER.
Include each supported, non-redundant subtopic in that existing order. You may omit a subtopic only when its evidence
is not meaningful enough to summarize or its content would merely repeat the parent topic overview. Do not invent,
rename, or merge subtopics. Topics with no useful required subtopics must return an empty subsections list.
Avoid repeating the same fact in the topic overview and subsections.
Each subsection content must use exactly one natural format: paragraph, bullets, or a small table. Use tables only
when the evidence genuinely contains compact comparable attributes. Do not return per-topic key takeaways.
Return JSON only using this schema:
{{"topics":[{{"topic_id":"exact id","overview":"concise grounded overview","subsections":[
{{"subtopic_id":"exact id","content_type":"paragraph|bullets|table","paragraph":"", "bullets":[],
"table":{{"headers":[],"rows":[[]]}}}}]}}]}}

DOCUMENT: {document_id}

{joined_sections}"""


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
    if len(set(returned_ids)) == len(returned_ids) and set(returned_ids) == set(requested_ids):
        by_id = dict(zip(returned_ids, raw_items))
        return [by_id[item_id] for item_id in requested_ids], False
    return list(raw_items), True


def _bind_topic_responses(raw_topics, topics: list[dict]) -> tuple[list[dict], bool]:
    """Bind a structurally complete response to backend-owned topic identities and order."""
    requested_ids = [str(topic["topic_id"]) for topic in topics]
    bound, rebound = _bind_ordered_responses(raw_topics, requested_ids, "topic_id", "topic summaries")
    if any(not str(item.get("overview") or "").strip() for item in bound):
        raise ValueError("Summary model returned an incomplete or ambiguous topic summary response.")
    return bound, rebound


def _normalized_subsection_content(item: dict) -> dict:
    content_type = str(item.get("content_type") or "").strip().lower()
    if content_type == "paragraph":
        text = str(item.get("paragraph") or "").strip()
        if not text:
            raise ValueError("Structured summary paragraph content is empty.")
        return {"type": "paragraph", "text": text}
    if content_type == "bullets":
        bullets = [str(value).strip() for value in item.get("bullets") or [] if str(value).strip()]
        if not bullets:
            raise ValueError("Structured summary bullet content is empty.")
        return {"type": "bullets", "items": bullets}
    if content_type == "table":
        table = item.get("table") or {}
        headers = [str(value).strip() for value in table.get("headers") or [] if str(value).strip()]
        rows = [[str(value).strip() for value in row] for row in table.get("rows") or [] if isinstance(row, list)]
        if not headers or not rows or any(len(row) != len(headers) for row in rows):
            raise ValueError("Structured summary table must have headers and equally sized rows.")
        return {"type": "table", "headers": headers, "rows": rows}
    raise ValueError("Structured summary subsection has an unsupported content_type.")


def _bind_optional_subtopics(raw_items, expected_subtopics: list[dict]) -> tuple[list[tuple[dict, dict]], bool]:
    """Bind an optional model subset to existing subtopics without inventing or merging identities."""
    if not isinstance(raw_items, list) or len(raw_items) > len(expected_subtopics):
        raise ValueError("Summary model returned an invalid number of subsections.")
    if any(not isinstance(item, dict) for item in raw_items):
        raise ValueError("Summary model returned an incomplete or ambiguous subsections response.")
    expected_ids = [str(item["subtopic_id"]) for item in expected_subtopics]
    returned_ids = [str(item.get("subtopic_id") or "") for item in raw_items]
    if len(set(returned_ids)) == len(returned_ids) and set(returned_ids).issubset(set(expected_ids)):
        by_id = dict(zip(returned_ids, raw_items))
        return [(subtopic, by_id[subtopic_id]) for subtopic, subtopic_id in zip(expected_subtopics, expected_ids) if subtopic_id in by_id], False
    if len(raw_items) == len(expected_subtopics):
        return list(zip(expected_subtopics, raw_items)), True
    raise ValueError("Summary model returned ambiguous subtopic identities for an incomplete subsection set.")


def _content_text(content: dict) -> str:
    if content["type"] == "paragraph":
        return content["text"]
    if content["type"] == "bullets":
        return " ".join(content["items"])
    return " ".join([*content["headers"], *(cell for row in content["rows"] for cell in row)])


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


def _validated_topic_summaries(raw_topics, topics: list[dict], evidence: list[tuple[dict, list[dict]]]) -> list[dict]:
    bound, rebound_ids = _bind_topic_responses(raw_topics, topics)
    evidence_tokens = [_tokens(_evidence_text(chunks)) for _topic, chunks in evidence]
    validated = []
    for index, (topic, item) in enumerate(zip(topics, bound)):
        overview = str(item["overview"]).strip()
        expected_subtopics = list(topic.get("subtopics") or [])
        raw_subsections = item.get("subsections")
        bound_subsections, rebound_subtopics = _bind_optional_subtopics(raw_subsections, expected_subtopics)
        normalized_subsections = []
        for subtopic, subsection in bound_subsections:
            content = _normalized_subsection_content(subsection)
            normalized_subsections.append({
                "subtopic_id": str(subtopic["subtopic_id"]),
                "subtopic_name": str(subtopic.get("name") or subtopic["subtopic_id"]),
                "content": content,
            })
        structured_text = " ".join([overview, *(_content_text(section["content"]) for section in normalized_subsections)])
        generated_tokens = _tokens(structured_text)
        own_tokens = evidence_tokens[index]
        overlap = generated_tokens & own_tokens
        required_overlap = 1 if len(generated_tokens) <= 4 else 2
        if len(overlap) < required_overlap:
            raise ValueError(f"Summary for topic {topic['topic_id']} lacks meaningful lexical support from its evidence.")
        other_tokens = set().union(*(tokens for other_index, tokens in enumerate(evidence_tokens) if other_index != index))
        warnings = _grounding_warnings(
            overview, [_content_text(section["content"]) for section in normalized_subsections], own_tokens, other_tokens
        )
        if rebound_ids:
            warnings.insert(0, "model_topic_ids_overwritten_by_backend_order")
        if rebound_subtopics:
            warnings.insert(0, "model_subtopic_ids_overwritten_by_backend_order")
        validated.append({
            "topic_id": str(topic["topic_id"]),
            "topic_name": str(topic.get("name") or topic["topic_id"]),
            "overview": overview,
            "subsections": normalized_subsections,
            "grounding_warnings": list(dict.fromkeys(warnings)),
        })
    return validated


def _synthesis_prompt(document_id: str, topic_summaries: list[dict]) -> str:
    synthesis_evidence = [
        {key: item[key] for key in ("topic_id", "topic_name", "overview", "subsections")}
        for item in topic_summaries
    ]
    return f"""Using only the validated ordered study notes below, write one concise Document Overview paragraph
and 4 to 7 document-level key takeaways. Do not introduce unsupported facts. Avoid repeating the same fact
between the overview, topic notes, and final takeaways. Do not return topic-level takeaways.
Return JSON only: {{"overview":"one concise paragraph","key_takeaways":["takeaway 1","takeaway 2","takeaway 3","takeaway 4"]}}

DOCUMENT: {document_id}
ORDERED TOPIC SUMMARIES:
{json.dumps(synthesis_evidence, ensure_ascii=False)}"""


def generate_document_summary(owner_id: str, document_id: str, model_id: str | None = None, regenerate: bool = False) -> dict:
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
        cached = get_compatible_summary(identity)
        if cached:
            cached["cache_hit"] = True
            return cached

    retrieval_started = time.perf_counter()
    evidence = [(topic, get_topic_chunks(document_id, str(topic["topic_id"]), owner_id)) for topic in topics]
    if any(not chunks for _topic, chunks in evidence):
        missing = [str(topic["topic_id"]) for topic, chunks in evidence if not chunks]
        raise ValueError(f"No indexed chunks found for topic(s): {', '.join(missing)}")
    retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)

    llm = ChatOllama(model=runtime_model, temperature=0, format="json", num_ctx=32768)
    topic_started = time.perf_counter()
    topic_response = llm.invoke(_topic_prompt(document_id, evidence))
    raw_topics = _json_object(topic_response.content).get("topics")
    topic_summaries = _validated_topic_summaries(raw_topics, topics, evidence)
    topic_ms = round((time.perf_counter() - topic_started) * 1000)

    synthesis_started = time.perf_counter()
    synthesis_response = llm.invoke(_synthesis_prompt(document_id, topic_summaries))
    final_summary = _json_object(synthesis_response.content)
    if not str(final_summary.get("overview") or "").strip():
        raise ValueError("Summary model omitted the final overview.")
    final_takeaways = [str(value).strip() for value in final_summary.get("key_takeaways") or [] if str(value).strip()]
    if not 4 <= len(final_takeaways) <= 7:
        raise ValueError("Summary model must return 4 to 7 document-level key takeaways.")
    final_summary = {"overview": str(final_summary["overview"]).strip(), "key_takeaways": final_takeaways}
    synthesis_ms = round((time.perf_counter() - synthesis_started) * 1000)
    metrics = {"chunk_retrieval_ms": retrieval_ms, "topic_generation_ms": topic_ms,
               "synthesis_ms": synthesis_ms, "llm_calls": 2,
               "topic_generation_usage": _usage(topic_response), "synthesis_usage": _usage(synthesis_response)}
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
