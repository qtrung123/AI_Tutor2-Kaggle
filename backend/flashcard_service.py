"""Grounded one-call flashcard generation over existing owner-scoped topic chunks."""

import json
import re

from langchain_ollama import ChatOllama

from backend.flashcard_store import get_compatible_flashcards, save_flashcards
from backend.indexed_document_store import get_indexed_document
from backend.model_registry import resolve_generation_model
# Read-only reuse of already-proven, generic (non-document-specific) text-quality helpers --
# quiz_service.py itself is not modified by the flashcard fix.
from backend.quiz_service import _clean_inline_text, _reject_unsafe_final_text, get_topic_chunks
from config import DEFAULT_GENERATION_MODEL

# v2: fixes the English-front/Vietnamese-back bilingual bug, the whitespace-corruption bug, and
# adds language-aware generation -- bumped so any pre-existing v1 cache (including malformed
# cards) is never reused; see FLASHCARD_LANGUAGES / generate_flashcards.
FLASHCARD_VERSION = "grounded_flashcards_v2"
MAX_CHARS_PER_CHUNK = 1400
FLASHCARD_LANGUAGES = {"auto", "english", "vietnamese"}
DEFAULT_FLASHCARD_LANGUAGE = "auto"

# Vietnamese-specific letters/diacritics that never appear in plain English text -- a
# conservative, generic (non-document-specific) language signal used to catch a card whose Front
# and Back were written in different languages (the reported translation-pair bug) and to keep a
# generated set in one consistent study language.
_VIETNAMESE_CHARS = re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯẠ-ỹ]")
_MIN_WORDS_FOR_LANGUAGE_SIGNAL = 3

_URL_LIKE = re.compile(r"^[a-z][a-z0-9+.-]*://|^www\.", re.IGNORECASE)
_IDENTIFIER_SIGNAL = re.compile(r"[0-9_]|[a-z][A-Z]")
_MIN_CORRUPTION_LENGTH = 24


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


def _looks_vietnamese(text: str) -> bool:
    return bool(_VIETNAMESE_CHARS.search(text or ""))


def _side_language(text: str) -> str | None:
    """Best-effort language signal for one card side; None when too short to judge -- a short
    technical term/acronym answer (FIFO, HAL, eCos) carries no language signal and must never be
    penalized."""
    if len((text or "").split()) < _MIN_WORDS_FOR_LANGUAGE_SIGNAL:
        return None
    return "vietnamese" if _looks_vietnamese(text) else "english"


def _card_language(front: str, back: str) -> str | None:
    return _side_language(front) or _side_language(back)


def _is_language_consistent_card(front: str, back: str) -> bool:
    """A card whose Front and Back read as different languages is invalid -- e.g. an English
    Front with a Vietnamese Back that is really just its translation (the reported bilingual
    bug). Short sides are exempt via _side_language's word-count floor."""
    front_language, back_language = _side_language(front), _side_language(back)
    return not (front_language and back_language and front_language != back_language)


def _keep_majority_language(cards: list[dict]) -> list[dict]:
    """Auto mode: infer the set's one canonical language from the majority of cards that carry a
    language signal, then drop any card whose detected language disagrees -- a generated set
    must never mix languages between cards. A card with no signal (e.g. purely technical-term
    content) is always kept."""
    languages = [_card_language(card["front"], card["back"]) for card in cards]
    votes: dict[str, int] = {}
    for language in languages:
        if language:
            votes[language] = votes.get(language, 0) + 1
    if not votes:
        return cards
    majority = max(votes, key=votes.get)
    return [card for card, language in zip(cards, languages) if not language or language == majority]


def _looks_corrupted_no_whitespace(text: str) -> bool:
    """Generic guard against a long natural-language string whose inter-word whitespace was lost
    in transit (the reported flashcard whitespace-corruption bug) -- never fires on short terms,
    acronyms, identifiers, URLs, or code-like tokens (digits, underscores, camelCase, ALL-CAPS).
    Detects the symptom generically; never attempts to reconstruct the missing spaces."""
    stripped = (text or "").strip()
    if len(stripped) < _MIN_CORRUPTION_LENGTH or re.search(r"\s", stripped):
        return False
    if _URL_LIKE.match(stripped) or _IDENTIFIER_SIGNAL.search(stripped):
        return False
    return not stripped.isupper()


def _is_well_formed_card_text(front: str, back: str) -> bool:
    """Structural/quality guard applied before persistence. Never repairs text -- a failing card
    is dropped (and the whole set retried once), not patched."""
    if not front or not back:
        return False
    if " ".join(front.lower().split()) == " ".join(back.lower().split()):
        return False  # Back must not be a no-op restatement of Front
    if _looks_corrupted_no_whitespace(front) or _looks_corrupted_no_whitespace(back):
        return False
    try:
        _reject_unsafe_final_text(front, field="Front")
        _reject_unsafe_final_text(back, field="Back")
    except ValueError:
        return False
    return True


_LANGUAGE_INSTRUCTIONS = {
    "english": (
        "Write every card -- front and back -- in English only, even if some evidence is "
        "written in another language."
    ),
    "vietnamese": (
        "Write every card -- front and back -- in Vietnamese only, even if some evidence is "
        "written in another language."
    ),
    "auto": (
        "The evidence may contain more than one language. Infer the single canonical language "
        "of this document (the language most of its structure and content is written in) and "
        "write EVERY card, front and back, in that one language consistently. Never mix "
        "languages within one card, and never switch languages between cards in the same set."
    ),
}

_BILINGUAL_INSTRUCTIONS = (
    "Some evidence may repeat the same sentence twice, once in one language and again "
    "immediately after in a translation of it. Treat a pair like this as ONE single fact, not "
    "two separate facts, and never make one language's sentence the front and its translation "
    "into another language the back -- a translation is not a real question/answer "
    "relationship."
)


def _prompt(document_id: str, evidence_groups: list[tuple[dict, list[dict]]], language: str) -> str:
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
    return f"""Create concise, recall-oriented study flashcards for every topic block below.
Flashcards test KNOWLEDGE, not translation. Prefer a question-style front ("What is...", "When
does...", "Why does...") whose back is a clear, concise answer grounded in the evidence. A
statement-style front is fine too, but front and back must never be two versions of the same
sentence -- the back must add real information (an answer, a definition, a consequence), never
just restate or translate the front.
{_BILINGUAL_INSTRUCTIONS}
{_LANGUAGE_INSTRUCTIONS[language]}
Preserve precise technical terms and identifiers exactly as written regardless of the study
language (for example eCos, TinyOS, HAL, FIFO, mutex, semaphore, FPGA, ASIC, API).
Prioritize important definitions, concepts, characteristics, mechanisms, components, and comparisons.
Use ONLY evidence from the card's own TOPIC block. Never transfer facts between topics or add outside facts.
Include source_chunk_ids that directly support each card. A subtopic_id is optional and must come from evidence metadata.
Do not pad to an exact count. Avoid duplicates or near-duplicates. Never copy a raw header, footer, page number, or email address into a card. Return one topic group per input block, in the same order.
Return JSON only: {{"topics":[{{"topic_id":"id","cards":[{{"front":"...","back":"...",
"subtopic_id":"optional","source_chunk_ids":["..."]}}]}}]}}

DOCUMENT: {document_id}

{chr(10).join(blocks)}"""


def _topic_maps(document: dict) -> tuple[list[dict], dict[str, dict]]:
    topics = [topic for topic in document.get("topics") or [] if topic.get("topic_id")]
    return topics, {str(topic["topic_id"]): topic for topic in topics}


def generate_flashcards(owner_id: str, document_id: str, topic_ids: list[str] | None = None,
                        model_id: str | None = None, language: str | None = None) -> dict:
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
    requested_language = str(language or DEFAULT_FLASHCARD_LANGUAGE).strip().lower()
    if requested_language not in FLASHCARD_LANGUAGES:
        raise ValueError("language must be one of auto, english, or vietnamese.")
    identity = {
        "owner_id": owner_id, "document_id": document_id, "document_hash": document.get("hash") or "",
        "topic_schema_version": int(document.get("topic_schema_version") or 0),
        "flashcard_version": FLASHCARD_VERSION, "model_id": public_model,
        "runtime_model": runtime_model, "topic_ids": selected_ids,
        "flashcard_language": requested_language,
    }
    cached = get_compatible_flashcards(identity)
    if cached:
        return {**cached, **identity, "cache_hit": True, "llm_calls": 0}

    groups = [(topic, get_topic_chunks(document_id, str(topic["topic_id"]), owner_id)) for topic in selected]
    if any(not chunks for _, chunks in groups):
        raise ValueError("One or more selected topics have no indexed evidence.")

    llm_calls = 0
    cards: list[dict] = []
    missing_topic_ids: list[str] = list(selected_ids)
    # One bounded retry: a set that comes back with zero valid cards -- for the whole set, or for
    # any individual selected topic -- after quality/language filtering is regenerated once
    # rather than persisted as a partial set or failing outright on a single bad sample.
    for _attempt in range(2):
        llm_calls += 1
        response = ChatOllama(model=runtime_model, temperature=0.1, format="json").invoke(
            _prompt(document_id, groups, requested_language)
        )
        raw_topics = _json_object(response.content).get("topics")
        if not isinstance(raw_topics, list) or len(raw_topics) != len(selected):
            continue

        candidate_cards: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for position, (topic, chunks) in enumerate(groups):
            raw_group = raw_topics[position]
            if not isinstance(raw_group, dict) or not isinstance(raw_group.get("cards"), list):
                continue
            subtopics = {str(item["subtopic_id"]): item for item in topic.get("subtopics") or [] if item.get("subtopic_id")}
            chunk_ids = {str((chunk.get("metadata") or {}).get("chunk_id") or (chunk.get("metadata") or {}).get("chunk") or "") for chunk in chunks}
            for raw in raw_group["cards"]:
                if not isinstance(raw, dict):
                    continue
                front = _clean_inline_text(raw.get("front") or "")
                back = _clean_inline_text(raw.get("back") or "")
                source_ids = list(dict.fromkeys(str(value) for value in raw.get("source_chunk_ids") or [] if str(value) in chunk_ids))
                subtopic_id = str(raw.get("subtopic_id") or "").strip() or None
                if not source_ids or (subtopic_id and subtopic_id not in subtopics):
                    continue
                if not _is_well_formed_card_text(front, back) or not _is_language_consistent_card(front, back):
                    continue
                if requested_language != "auto":
                    observed = _card_language(front, back)
                    if observed and observed != requested_language:
                        continue
                duplicate_key = (" ".join(front.lower().split()), " ".join(back.lower().split()))
                if duplicate_key in seen:
                    continue
                seen.add(duplicate_key)
                candidate_cards.append({
                    "topic_id": str(topic["topic_id"]), "topic_name": str(topic.get("name") or topic["topic_id"]),
                    "subtopic_id": subtopic_id,
                    "subtopic_name": str(subtopics[subtopic_id].get("name") or subtopic_id) if subtopic_id else None,
                    "front": front, "back": back, "source_chunk_ids": source_ids,
                })

        if requested_language == "auto" and candidate_cards:
            candidate_cards = _keep_majority_language(candidate_cards)

        covered_topic_ids = {card["topic_id"] for card in candidate_cards}
        missing_topic_ids = [topic_id for topic_id in selected_ids if topic_id not in covered_topic_ids]
        # Never persist a set missing an entire selected topic (e.g. 13 generated cards
        # collapsing to 1-3 after language/quality filtering) -- every selected topic must have
        # at least one valid card, not an arbitrary fixed count per topic.
        if candidate_cards and not missing_topic_ids:
            cards = candidate_cards
            break

    if not cards:
        raise ValueError(
            "Flashcard generation could not produce at least one valid card for every selected "
            f"topic after {llm_calls} attempt(s); missing topic_id(s): "
            f"{', '.join(missing_topic_ids) if missing_topic_ids else 'all'}."
        )
    saved = save_flashcards(identity, cards)
    return {**saved, **identity, "cache_hit": False, "llm_calls": llm_calls}


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
