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

SUMMARY_VERSION = "full_document_summary_v1"
MAX_CHARS_PER_CHUNK = 1200


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
            evidence.append(f"[chunk_id={metadata.get('chunk_id') or metadata.get('chunk')} page={metadata.get('page', 'unknown')}]\n{content}")
        sections.append(f"TOPIC_ID: {topic['topic_id']}\nTOPIC_NAME: {topic.get('name') or topic['topic_id']}\nEVIDENCE:\n" + "\n\n".join(evidence))
    joined_sections = "\n\n---\n\n".join(sections)
    return f"""Summarize every topic below using only its supplied evidence. Do not add outside facts.
Preserve the exact topic IDs and order. If evidence is limited, say so plainly.
Return JSON only: {{"topics":[{{"topic_id":"exact id","summary":"grounded summary","key_takeaways":["grounded takeaway"]}}]}}

DOCUMENT: {document_id}

{joined_sections}"""


def _synthesis_prompt(document_id: str, topic_summaries: list[dict]) -> str:
    return f"""Create a concise full-document overview using only the supplied topic summaries.
Do not introduce facts that are absent below. Preserve the document's progression and emphasize its main themes.
Return JSON only: {{"overview":"final overview","key_takeaways":["document-level takeaway"]}}

DOCUMENT: {document_id}
ORDERED TOPIC SUMMARIES:
{json.dumps(topic_summaries, ensure_ascii=False)}"""


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
    raw_topics = _json_object(topic_response.content).get("topics") or []
    by_id = {str(item.get("topic_id")): item for item in raw_topics if isinstance(item, dict)}
    topic_summaries = []
    for topic in topics:
        item = by_id.get(str(topic["topic_id"]))
        if not item or not str(item.get("summary") or "").strip():
            raise ValueError(f"Summary model omitted topic {topic['topic_id']}.")
        topic_summaries.append({"topic_id": str(topic["topic_id"]), "topic_name": str(topic.get("name") or topic["topic_id"]),
                                "summary": str(item["summary"]).strip(),
                                "key_takeaways": [str(value).strip() for value in item.get("key_takeaways") or [] if str(value).strip()]})
    topic_ms = round((time.perf_counter() - topic_started) * 1000)

    synthesis_started = time.perf_counter()
    synthesis_response = llm.invoke(_synthesis_prompt(document_id, topic_summaries))
    final_summary = _json_object(synthesis_response.content)
    if not str(final_summary.get("overview") or "").strip():
        raise ValueError("Summary model omitted the final overview.")
    final_summary = {"overview": str(final_summary["overview"]).strip(),
                     "key_takeaways": [str(value).strip() for value in final_summary.get("key_takeaways") or [] if str(value).strip()]}
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
