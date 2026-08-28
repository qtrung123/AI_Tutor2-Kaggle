import difflib
import json
import math
import re
import time
from pathlib import Path
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_ollama import ChatOllama, OllamaEmbeddings

from backend.quiz_store import (
    delete_document_attempts,
    get_latest_attempt,
    get_latest_completed_attempt_for_quiz,
    get_quiz,
    get_quiz_by_id,
    get_quiz_explanation,
    get_quiz_attempt_summary,
    list_document_quizzes,
    list_completed_answer_snapshots,
    list_quiz_history as load_quiz_history,
    get_quiz_history_attempt,
    invalidate_document_quizzes_for_topic_schema,
    quiz_cache_key,
    save_quiz,
    save_quiz_explanation,
    get_cached_concept_plan,
    save_cached_concept_plan,
    save_quiz_progress,
    reset_quiz_progress,
    save_quiz_validation_event,
    utc_now_iso,
)
from backend.quiz_validation import validate_question_semantics
from backend.assessment_planner import (
    CONCEPT_PLANNER_PROMPT_VERSION,
    PLANNER_VERSION,
    allocate_document_topics,
    build_topic_plan,
    is_valid_concept_plan,
    resolve_concept_evidence,
    planner_input_fingerprint,
)
from backend.quiz_options import canonicalize_option, strip_leading_option_label
from backend.mastery_service import calculate_mastery, recompute_topic_mastery
from backend.rag_service import explain_quiz_answer
from backend.auth_store import LEGACY_USER_ID
from backend.indexed_document_store import list_indexed_documents as load_owned_documents
from backend.ingest import migrate_legacy_vector_ownership
from config import (
    CHAT_MODEL,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    INDEXED_FILES_PATH,
    QUIZ_PROMPT_PATH,
    QUIZ_GENERATION_RETRY_LIMIT,
    QUIZ_QUALITY_RETRY_LIMIT,
    VECTORSTORE_DIR,
)

GENERATION_PROMPT_VERSION = "topic_mcq_v2_backend_evidence"
QUIZ_V2_PROMPT_VERSION = "topic_mcq_v2_slot_batch_fast"
QUIZ_V2_ALLOWED_QUESTION_COUNTS = {10, 15, 20, 25}
QUIZ_V2_DISTINCT_CONCEPT_LIMIT = 5


class QuizGenerationError(ValueError):
    """Structured topic-quiz failure that is safe to expose through the API."""

    def __init__(
        self, message: str, *, stage: str, valid_questions: int = 0,
        target_questions: int = 10, failure_summary: list[str] | None = None,
    ):
        super().__init__(message)
        missing_questions = max(0, target_questions - valid_questions)
        self.detail = {
            "code": "quiz_v2_generation_failed",
            "message": message,
            "stage": stage,
            "valid_questions": valid_questions,
            "target_questions": target_questions,
            "requested_count": target_questions,
            "valid_count": valid_questions,
            "missing_count": missing_questions,
            "failure_summary": list(failure_summary or []),
        }


OPTION_LETTERS = {"A", "B", "C", "D"}
QUIZ_DIFFICULTIES = {"easy", "medium", "difficult"}
GENERIC_QUESTION_PATTERNS = [
    r"\bwhich statement is supported\b",
    r"\bwhich (option|statement) is (correct|true)\b",
    r"\baccording to (the )?(lecture|document|context|chunk)\b",
    r"\bwhat does this (chunk|segment|context) say\b",
    r"\bprovided context\b",
]
GENERIC_OPTION_PATTERNS = [
    r"\bunrelated to the selected material\b",
    r"\bno technical concept is involved\b",
    r"\bcannot be explained from the document\b",
    r"\bdoes not contain enough information\b",
    r"\bthe lecture does not mention\b",
    r"\bthe document does not mention\b",
]

# Every quiz-generation batch receives every indexed chunk from the selected
# lecture. Individual chunk text is still bounded so one unusually large chunk
# cannot dominate the prompt.
MAX_CHARS_PER_CHUNK = 900
MAX_QUESTIONS_PER_BATCH = 8
QUIZ_CONTEXT_WINDOWS = (4096, 8192, 16384, 32768)


def _load_indexed_files(owner_id: str) -> dict:
    return {document["document_id"]: document for document in load_owned_documents(owner_id)}


def list_indexed_documents(owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """
    Return documents available for quiz generation.

    The hash is included internally so generated quizzes can remember which
    exact uploaded file version they belong to.
    """
    indexed_files = _load_indexed_files(owner_id)
    for file_name, info in indexed_files.items():
        invalidate_document_quizzes_for_topic_schema(
            file_name, int(info.get("topic_schema_version", 0)), owner_id
        )
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


def _get_or_build_topic_plan(document: dict, topic: dict, chunks: list[dict], owner_id: str) -> tuple[dict, dict]:
    """Return one validated plan using an exact persisted planning-input identity."""
    fingerprint = planner_input_fingerprint(topic, chunks)
    identity = {
        "owner_id": owner_id,
        "document_id": str(document["id"]),
        "document_hash": str(document.get("hash") or ""),
        "topic_schema_version": int(document.get("topic_schema_version", 0)),
        "topic_id": str(topic["topic_id"]),
        "planner_version": PLANNER_VERSION,
        "planner_input_fingerprint": fingerprint,
        "planner_prompt_version": CONCEPT_PLANNER_PROMPT_VERSION,
        "planner_model": CHAT_MODEL,
    }
    lookup_started = time.perf_counter()
    cached = get_cached_concept_plan(identity)
    lookup_ms = round((time.perf_counter() - lookup_started) * 1000)
    try:
        cached_is_valid = bool(cached) and is_valid_concept_plan(cached, topic, chunks)
    except (KeyError, TypeError, ValueError):
        cached_is_valid = False
    if cached_is_valid:
        print(
            f"[concept-plan-cache] HIT document={document['id']} topic={topic['topic_id']} "
            f"fingerprint={fingerprint[:12]} lookup_ms={lookup_ms}"
        )
        return cached, {"cache_hit": True, "lookup_ms": lookup_ms, "build_ms": 0}

    build_started = time.perf_counter()
    plan = build_topic_plan(topic, chunks)
    build_ms = round((time.perf_counter() - build_started) * 1000)
    try:
        cacheable = is_valid_concept_plan(plan, topic, chunks)
    except (KeyError, TypeError, ValueError):
        cacheable = False
    if cacheable:
        save_cached_concept_plan(identity, plan)
    print(
        f"[concept-plan-cache] MISS document={document['id']} topic={topic['topic_id']} "
        f"fingerprint={fingerprint[:12]} lookup_ms={lookup_ms} build_ms={build_ms} "
        f"stored={str(cacheable).lower()}"
    )
    return plan, {"cache_hit": False, "lookup_ms": lookup_ms, "build_ms": build_ms}


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


def _format_context(chunks: list[dict]) -> str:
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk["metadata"]
        source = Path(str(metadata.get("source", "Unknown source"))).name
        page = str(metadata.get("page", "Unknown page"))
        chunk_number = str(metadata.get("chunk_id") or metadata.get("chunk", index - 1))
        content = str(chunk["content"]).strip()
        if len(content) > MAX_CHARS_PER_CHUNK:
            content = f"{content[:MAX_CHARS_PER_CHUNK].rstrip()}..."
        parts.append(
            f"[Chunk ID: {chunk_number}]\n"
            f"Source: {source}\n"
            f"Page: {page}\n"
            f"Content:\n{content}"
        )
    return "\n\n".join(parts)


def load_quiz_prompt_template() -> str:
    with open(QUIZ_PROMPT_PATH, "r", encoding="utf-8") as file:
        return file.read()


def _difficulty_instructions(difficulty: str) -> str:
    instructions = {
        "easy": """EASY
- Test one directly stated fact, definition, purpose, syntax element, or explicit step.
- The learner should answer by recognizing or recalling one piece of information.
- Do not require combining separate chunks or inferring an unstated consequence.""",
        "medium": """MEDIUM
- Require understanding through comparison, cause and effect, ordering, code interpretation, or applying one stated rule to a short situation.
- Each question must require at least one reasoning step beyond copying a sentence.
- Prefer stems such as "Why...", "How would...", "What happens when...", "Which outcome...", or "Which option best explains the relationship...".
- Do not ask direct who/when questions, isolated definitions, simple syntax recognition, or "Which ... is a characteristic of ...?" questions.""",
        "difficult": """DIFFICULT
- Require analysis, diagnosis, prediction, or application in a concrete scenario.
- Combine at least two related facts, rules, examples, or code behaviors from the context whenever the material supports it.
- Prefer predicting code behavior, choosing a design consequence, diagnosing an error, or distinguishing subtly plausible alternatives.
- Do not ask names, dates, direct definitions, direct syntax recognition, or any question answerable by copying one sentence.""",
    }
    return instructions[difficulty]


def _format_avoid_questions(questions: list[str]) -> str:
    if not questions:
        return "(None yet.)"
    return "\n".join(f"- {question}" for question in questions)


def _build_prompt(
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    topic_id: str = "document",
    topic_name: str = "Whole document",
    avoid_questions: list[str] | None = None,
) -> str:
    print(f"[quiz-prompt] difficulty={difficulty}")
    prompt_template = load_quiz_prompt_template()
    prompt = (
        prompt_template
        .replace("{document_id}", document_id)
        .replace("{num_questions}", str(num_questions))
        .replace("{difficulty}", difficulty)
        .replace("{topic_id}", topic_id)
        .replace("{topic_name}", topic_name)
        .replace("{difficulty_instructions}", _difficulty_instructions(difficulty))
        .replace("{avoid_questions}", _format_avoid_questions(avoid_questions or []))
        .replace("{context}", _format_context(chunks))
    )
    return prompt


def _build_retry_prompt(
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    error: Exception,
    topic_id: str = "document",
    topic_name: str = "Whole document",
    avoid_questions: list[str] | None = None,
) -> str:
    """
    Ask the model for a corrected batch after JSON or quality validation fails.

    The retry prompt is shorter and repeats the exact failure so the model can
    repair structure/quality without getting distracted by the full rubric.
    """
    return f"""
Return valid JSON only. Regenerate the quiz batch for this document.

DOCUMENT: {document_id}
TOPIC: {topic_name} ({topic_id})
NUMBER OF QUESTIONS: {num_questions}
QUIZ DIFFICULTY: {difficulty}

The previous answer was rejected for this reason:
{error}

REPAIR INSTRUCTION:
- Completely discard every rejected question named in the error above.
- Do not reword or paraphrase a rejected question.
- Choose a different concept from the context for its replacement.
- A repeated rejected question makes the entire response invalid.

Hard requirements:
- exactly {num_questions} questions
- each question must be specific to a concept in the context
- do not start questions with "According to"
- do not ask "which statement is supported/correct/true"
- no raw chunk text as options
- no generic distractors about unrelated material or missing information
- options must be short, plausible, same-topic choices
- distractors must not assert facts unsupported by the context
- include a grounded explanation for every question
- source provenance is attached by the backend; do not return source IDs
- all questions must follow the requested difficulty level
- no two questions in this response may test the same fact, concept, or correct-answer relationship, even if reworded
- do not repeat or paraphrase any previously used question listed below
- escape double quotes inside JSON strings as \\\"
- encode line breaks inside strings as \\n and close every string, array, and object
- return JSON only, no markdown

DIFFICULTY CONTRACT:
{_difficulty_instructions(difficulty)}

PREVIOUSLY USED QUESTIONS:
{_format_avoid_questions(avoid_questions or [])}

JSON shape:
{{
  "questions": [
    {{
      "question": "Specific concept question",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "correct_answer": "A"
      ,"explanation": "Grounded explanation"
    }}
  ]
}}

COURSE MATERIAL CONTEXT:
{_format_context(chunks)}
""".strip()


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def _parse_quiz_response(text: str, attempt_index: int) -> dict:
    """Parse quiz JSON and safely log the complete escaped response on failure."""
    raw_response = str(text)
    try:
        return _extract_json(raw_response)
    except json.JSONDecodeError as error:
        print(
            f"[quiz-json-parse-error] attempt={attempt_index + 1}, "
            f"error={error}, raw_response_json="
            f"{json.dumps(raw_response, ensure_ascii=False)}"
        )
        raise


def _normalize_correct_answer(raw_answer, options: list[str]) -> str:
    answer = str(raw_answer or "").strip().upper()
    if answer in OPTION_LETTERS:
        return answer

    match = re.search(r"\b([ABCD])\b", answer)
    if match:
        return match.group(1)

    normalized_answer = re.sub(r"^[ABCD][\.\)]\s*", "", answer).strip()
    for index, option in enumerate(options):
        option_text = re.sub(r"^[ABCD][\.\)]\s*", "", str(option).upper()).strip()
        if normalized_answer and normalized_answer == option_text:
            return "ABCD"[index]
        if normalized_answer and normalized_answer in option_text:
            return "ABCD"[index]

    raise ValueError("invalid correct_answer")


def _normalize_options(raw_options) -> list[str]:
    if isinstance(raw_options, dict):
        options = []
        for letter in ["A", "B", "C", "D"]:
            value = str(raw_options.get(letter, "")).strip()
            if not value:
                raise ValueError("missing option text")
            options.append(canonicalize_option(value, letter))
        return options

    if isinstance(raw_options, list):
        options = []
        for index, option in enumerate(raw_options[:4]):
            letter = "ABCD"[index]
            options.append(canonicalize_option(option, letter))
        return options

    raise ValueError("options must be a list or object")


def _clean_inline_text(value: str) -> str:
    """Collapse model/newline artifacts while keeping code-like text readable."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _option_text(option: str) -> str:
    return strip_leading_option_label(option)


def _looks_like_raw_chunk(option: str) -> bool:
    text = _option_text(option)
    words = text.split()
    if len(words) > 32:
        return True
    if len(text) > 190:
        return True
    if text.count(".") >= 3 and len(words) > 22:
        return True
    return False


def _validate_question_quality(
    question: dict,
    options: list[str],
) -> None:
    """
    Reject low-value model output before it reaches persistent quiz storage.

    This is intentionally strict: returning a clear generation error is better
    than saving generic fallback questions that are poor for study.
    """
    question_text = _clean_inline_text(question.get("question", ""))
    lowered_question = question_text.lower()
    if len(question_text.split()) < 6:
        raise ValueError("Question is too short to be specific.")
    if any(re.search(pattern, lowered_question) for pattern in GENERIC_QUESTION_PATTERNS):
        raise ValueError(f"Question is too generic: {question_text}")
    option_bodies = [_option_text(option) for option in options]
    if len({body.lower() for body in option_bodies}) != 4:
        raise ValueError("Question options are not distinct.")

    for option in option_bodies:
        lowered_option = option.lower()
        if any(re.search(pattern, lowered_option) for pattern in GENERIC_OPTION_PATTERNS):
            raise ValueError(f"Option is generic: {option}")
        if lowered_option in {"all of the above", "none of the above", "cannot be determined"}:
            raise ValueError(f"Option is forbidden: {option}")
        if _looks_like_raw_chunk(option):
            raise ValueError(f"Option looks like copied raw chunk text: {option[:80]}")


def _validate_difficulty_quality(question_text: str, difficulty: str) -> None:
    """Reject obvious recall questions when a higher cognitive level was requested."""
    lowered = question_text.lower().strip()
    direct_recall_patterns = [
        r"^(who|when)\b",
        r"\bofficial announcement date\b",
        r"^what (is|was) the (type|name|date|definition|syntax)\b",
        r"^which of the following is (a )?(valid|correct)\b",
        r"^which (of the following )?.*\bcharacteristic(s)? of\b",
    ]
    if difficulty in {"medium", "difficult"} and any(
        re.search(pattern, lowered) for pattern in direct_recall_patterns
    ):
        raise ValueError(f"Question is direct recall and does not match {difficulty}: {question_text}")

    if difficulty == "difficult" and len(question_text.split()) < 9:
        raise ValueError(f"Difficult question is too shallow or underspecified: {question_text}")


def _normalize_question_key(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9\s]", "", str(text).lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _is_duplicate_question(question_text: str, existing_questions: list[str]) -> bool:
    """
    Catch exact and near-duplicate questions the prompt alone failed to prevent
    (e.g. the same question reworded with a different option list). 0.85 was
    picked to reject obvious rewordings while still allowing distinct questions
    that happen to share common exam phrasing.
    """
    candidate_key = _normalize_question_key(question_text)
    for existing in existing_questions:
        existing_key = _normalize_question_key(existing)
        if not existing_key:
            continue
        if candidate_key == existing_key:
            return True
        if difflib.SequenceMatcher(None, candidate_key, existing_key).ratio() >= 0.85:
            return True
    return False


def _validate_quiz_batch(
    data: dict,
    num_questions: int,
    start_id: int,
    difficulty: str = "easy",
    topic_id: str = "document",
    source_chunk_ids: list[str] | None = None,
    topic_name: str = "Whole document",
    concept_id: str = "",
    concept_name: str = "",
    assessment_capacity: int = 0,
) -> list[dict]:
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Quiz JSON does not contain a questions list.")

    normalized_questions = []
    for offset, question in enumerate(questions[:num_questions]):
        options = _normalize_options(question.get("options", []))
        if len(options) != 4:
            raise ValueError(f"Question {offset + 1} does not have exactly 4 options.")

        correct_answer = _normalize_correct_answer(question.get("correct_answer", ""), options)
        explanation = _clean_inline_text(question.get("explanation", ""))
        if not explanation:
            raise ValueError(f"Question {offset + 1} is missing an explanation.")
        attached_source_ids = list(dict.fromkeys(
            str(value).strip() for value in (source_chunk_ids or []) if str(value).strip()
        ))
        if not attached_source_ids:
            raise ValueError(f"Question {offset + 1} generation batch has no canonical evidence chunk IDs.")
        normalized_questions.append(
            {
                "id": start_id + offset,
                "question": _clean_inline_text(question.get("question", "")),
                "options": options,
                "correct_answer": correct_answer,
                "topic_id": topic_id,
                "topic_name": topic_name,
                "concept_id": concept_id,
                "concept_name": concept_name,
                "assessment_capacity": int(assessment_capacity),
                "difficulty": difficulty,
                "explanation": explanation,
                "source_chunk_ids": attached_source_ids,
            }
        )

        _validate_question_quality(
            question=normalized_questions[-1],
            options=options,
        )
        _validate_difficulty_quality(normalized_questions[-1]["question"], difficulty)

    if len(normalized_questions) != num_questions:
        raise ValueError(f"Expected {num_questions} questions, got {len(normalized_questions)}.")

    return normalized_questions


def _quiz_output_budget(missing_count: int, attempt_index: int) -> int:
    """Reserve enough output tokens to finish the JSON, with extra room on repair attempts."""
    return max(500, missing_count * 220) + (attempt_index * 120)


def _generate_quiz_batch(
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    start_id: int,
    topic_id: str = "document",
    topic_name: str = "Whole document",
    avoid_questions: list[str] | None = None,
    generation_run_id: str = "",
    batch_index: int = 0,
    document_hash: str = "",
    topic_schema_version: int = 0,
    concept_id: str = "",
    concept_name: str = "",
    assessment_capacity: int = 0,
    owner_id: str = LEGACY_USER_ID,
    model_id: str = CHAT_MODEL,
) -> list[dict]:
    accepted_questions: list[dict] = []
    generation_errors: list[str] = []
    base_avoid_questions = list(avoid_questions or [])
    batch_chunk_ids = {
        str(chunk.get("metadata", {}).get("chunk_id", ""))
        for chunk in chunks
        if chunk.get("metadata", {}).get("chunk_id")
    }
    chunks_by_id = {
        str(chunk["metadata"]["chunk_id"]): chunk
        for chunk in chunks
        if chunk.get("metadata", {}).get("chunk_id")
    }
    if not batch_chunk_ids or len(chunks_by_id) != len(chunks):
        raise ValueError("Generation batch contains missing or duplicate canonical chunk provenance IDs.")
    quality_rejections_used = 0

    retry_limit = max(1, QUIZ_GENERATION_RETRY_LIMIT)
    for attempt_index in range(retry_limit):
        missing_count = num_questions - len(accepted_questions)
        if missing_count <= 0:
            break

        current_avoid_questions = base_avoid_questions + [
            question["question"] for question in accepted_questions
        ]
        if attempt_index == 0:
            prompt = _build_prompt(
                document_id=document_id,
                num_questions=missing_count,
                difficulty=difficulty,
                chunks=chunks,
                topic_id=topic_id,
                topic_name=f"{topic_name} — target concept: {concept_name}" if concept_name else topic_name,
                avoid_questions=current_avoid_questions,
            )
        else:
            prompt = _build_retry_prompt(
                document_id=document_id,
                num_questions=missing_count,
                difficulty=difficulty,
                chunks=chunks,
                error=ValueError("; ".join(generation_errors[-5:])),
                topic_id=topic_id,
                topic_name=f"{topic_name} — target concept: {concept_name}" if concept_name else topic_name,
                avoid_questions=current_avoid_questions,
            )

        # Truncated JSON cannot be partially parsed safely. Reserve enough room
        # for every requested question and increase it after a malformed reply.
        num_predict = _quiz_output_budget(missing_count, attempt_index)
        estimated_required_tokens = math.ceil(len(prompt) / 3) + num_predict + 512
        num_ctx = next(
            (window for window in QUIZ_CONTEXT_WINDOWS if window >= estimated_required_tokens),
            None,
        )
        if num_ctx is None:
            raise ValueError(
                "The full lecture context is too large for the model's 32768-token context window. "
                f"Estimated required tokens: {estimated_required_tokens}."
            )

        temperatures = (
            {"easy": 0.1, "medium": 0.2, "difficult": 0.3},
            {"easy": 0.45, "medium": 0.65, "difficult": 0.75},
            {"easy": 0.6, "medium": 0.75, "difficult": 0.85},
        )
        llm = ChatOllama(
            model=model_id,
            temperature=temperatures[min(attempt_index, len(temperatures) - 1)][difficulty],
            format="json",
            num_ctx=num_ctx,
            num_predict=num_predict,
        )
        print(
            f"[quiz-llm] attempt={attempt_index + 1}, difficulty={difficulty}, "
            f"missing_questions={missing_count}, chunks={len(chunks)}, "
            f"num_ctx={num_ctx}, num_predict={num_predict}"
        )

        try:
            response = llm.invoke(prompt)
            done_reason = (getattr(response, "response_metadata", {}) or {}).get(
                "done_reason", "unknown"
            )
            print(
                f"[quiz-json] response_chars={len(str(response.content))}, "
                f"done_reason={done_reason}"
            )
            parsed = _parse_quiz_response(response.content, attempt_index)
            raw_questions = parsed.get("questions")
            if not isinstance(raw_questions, list):
                raise ValueError("Quiz JSON does not contain a questions list.")

            for candidate_index, raw_question in enumerate(raw_questions):
                if len(accepted_questions) >= num_questions:
                    break
                try:
                    normalized = _validate_quiz_batch(
                        {"questions": [raw_question]},
                        1,
                        start_id + len(accepted_questions),
                        difficulty,
                        topic_id,
                        sorted(batch_chunk_ids),
                        topic_name,
                        concept_id,
                        concept_name,
                        assessment_capacity,
                    )[0]
                    cited_chunks = [chunks_by_id[chunk_id] for chunk_id in normalized["source_chunk_ids"]]
                    semantic = validate_question_semantics(
                        normalized, cited_chunks, difficulty, topic_name
                    )
                    failures = semantic.hard_failures + semantic.quality_failures
                    event = {
                        "generation_run_id": generation_run_id,
                        "owner_id": owner_id,
                        "document_id": document_id,
                        "document_hash": document_hash,
                        "topic_id": topic_id,
                        "topic_schema_version": topic_schema_version,
                        "difficulty": difficulty,
                        "batch_index": batch_index,
                        "generation_attempt": attempt_index + 1,
                        "candidate_index": candidate_index,
                        "generator_model": CHAT_MODEL,
                        "generation_prompt_version": GENERATION_PROMPT_VERSION,
                        "validator_model": semantic.validator_model,
                        "validator_prompt_version": semantic.validator_prompt_version,
                        "candidate_question": normalized,
                        "cited_chunk_ids": normalized["source_chunk_ids"],
                        "evidence_chunk_ids": semantic.evidence_chunk_ids,
                        "hard_passed": semantic.hard_passed,
                        "quality_passed": semantic.quality_passed,
                        "verdict": semantic.verdict,
                        "rejection_reasons": failures,
                        "latency_ms": semantic.latency_ms,
                    }
                    if not semantic.hard_passed:
                        save_quiz_validation_event({**event, "accepted": False, "outcome": "rejected_hard"})
                        raise ValueError(
                            "Semantic grounding failed: " + ", ".join(semantic.hard_failures)
                        )
                    if not semantic.quality_passed and quality_rejections_used < max(0, QUIZ_QUALITY_RETRY_LIMIT):
                        quality_rejections_used += 1
                        save_quiz_validation_event({**event, "accepted": False, "outcome": "rejected_quality_retry"})
                        raise ValueError(
                            "Semantic quality check requested one retry: "
                            + ", ".join(semantic.quality_failures)
                        )
                    existing_questions = base_avoid_questions + [
                        question["question"] for question in accepted_questions
                    ]
                    if _is_duplicate_question(normalized["question"], existing_questions):
                        raise ValueError(
                            f"Question duplicates an existing question: {normalized['question']}"
                        )
                    outcome = "accepted" if semantic.quality_passed else "accepted_quality_warning"
                    normalized["validation_outcome"] = outcome
                    save_quiz_validation_event({**event, "accepted": True, "outcome": outcome})
                    accepted_questions.append(normalized)
                except Exception as question_error:
                    generation_errors.append(str(question_error))
                    print(f"[quiz-validation] discarded question: {question_error}")
        except Exception as response_error:
            generation_errors.append(str(response_error))
            print(f"[quiz-validation] response rejected: {response_error}")

    if len(accepted_questions) != num_questions:
        raise ValueError(
            f"Quiz generation produced {len(accepted_questions)}/{num_questions} valid questions "
            f"after {retry_limit} attempts. "
            f"Errors: {'; '.join(generation_errors[-5:])}"
        )

    return accepted_questions


def _v2_excerpt(content: str, slot: int, total_slots: int, limit: int = 280) -> str:
    """Choose a deterministic local window without changing canonical chunk provenance."""
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(text) <= limit:
        return text
    max_start = max(0, len(text) - limit)
    start = round(max_start * slot / max(1, total_slots - 1))
    return text[start:start + limit].strip()


def _select_v2_evidence_groups(
    topic: dict, chunks: list[dict], target: int = QUIZ_V2_DISTINCT_CONCEPT_LIMIT,
    topic_plan: dict | None = None,
) -> list[dict]:
    """Create stable concept slots from already owner/document/topic-scoped chunks."""
    usable = []
    seen_ids = set()
    for chunk in chunks:
        chunk_id = str((chunk.get("metadata") or {}).get("chunk_id") or "").strip()
        if not chunk_id or chunk_id in seen_ids or not str(chunk.get("content") or "").strip():
            continue
        seen_ids.add(chunk_id)
        usable.append(chunk)
    if not usable:
        return []

    topic_plan = topic_plan or build_topic_plan(topic, usable)
    planned_groups = []
    for slot, concept in enumerate(list(topic_plan.get("concepts") or [])[:max(1, target)]):
        evidence_chunks = resolve_concept_evidence(topic, usable, concept)
        if not evidence_chunks:
            continue
        excerpts = [
            _v2_excerpt(chunk.get("content", ""), index, len(evidence_chunks))
            for index, chunk in enumerate(evidence_chunks)
        ]
        planned_groups.append({
            "slot_id": f"S{slot + 1}", **concept,
            "concept_plan_id": topic_plan["concept_plan_id"],
            "evidence_excerpt": " ".join(excerpts)[:560],
        })
    return planned_groups

    topic_name = str(topic.get("name") or topic.get("topic_id") or "Topic")
    candidates = []
    for chunk in usable:
        content = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip()
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\s*[;•]\s*", content) if len(part.split()) >= 3]
        if not sentences:
            sentences = [_v2_excerpt(content, 0, 1)]
        candidates.extend((chunk, sentence[:280]) for sentence in sentences)
    if len(candidates) < target:
        # Add deterministic windows for compact chunks that contain several facts
        # but little punctuation. Provenance remains the canonical parent chunk.
        for chunk in usable:
            words = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip().split()
            window_size = max(12, min(45, math.ceil(len(words) / max(1, target))))
            for start in range(0, len(words), window_size):
                excerpt = " ".join(words[start:start + window_size]).strip()
                if len(excerpt.split()) >= 5 and all(excerpt != item[1] for item in candidates):
                    candidates.append((chunk, excerpt))
                if len(candidates) >= target:
                    break
            if len(candidates) >= target:
                break

    group_count = min(max(1, target), len(candidates))
    groups = []
    for slot in range(group_count):
        candidate_index = round((len(candidates) - 1) * slot / max(1, group_count - 1)) if len(candidates) > 1 else 0
        chunk, excerpt = candidates[candidate_index]
        chunk_id = str(chunk["metadata"]["chunk_id"])
        label_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", excerpt)[:8]
        label = " ".join(label_words) or f"focus {slot + 1}"
        groups.append({
            "slot_id": f"S{slot + 1}",
            "concept_id": f"concept_{slot + 1:03d}",
            "name": f"{topic_name}: {label}",
            "source_chunk_ids": [chunk_id],
            "evidence_excerpt": excerpt,
        })
    return groups


def _build_v2_prompt(
    document_id: str,
    topic: dict,
    difficulty: str,
    groups: list[dict],
    count: int,
    accepted_stems: list[str],
    repair: bool,
) -> str:
    evidence = "\n".join(
        f"{group['slot_id']}|{group.get('topic_name', '')}|{group['evidence_excerpt']}"
        if group.get("topic_name") else f"{group['slot_id']}|{group['evidence_excerpt']}"
        for group in groups
    )
    avoid = " | ".join(stem[:100] for stem in accepted_stems) if repair else ""
    repair_line = f"Avoid: {avoid}\n" if avoid else ""
    easy_contract = (
        "EASY QUALITY: Ask direct recall/comprehension about one explicit evidence fact. "
        "Exactly one option must fully answer the stem; every distractor must conflict with or be unsupported by this slot evidence. "
        "Do not rely on outside-world plausibility. Do not use NOT, EXCEPT, false, incorrect, least, or double negatives. "
        "If several evidence facts are true, do not make them competing options; the correct option must contain the complete requested set. "
        "The correct option must match the stem in scope and grammar.\n"
        if difficulty == "easy" else ""
    )
    return (
        f"Write exactly {count} {difficulty} MCQs for {topic.get('name') or topic.get('topic_id')}.\n"
        'JSON only: {"questions":[{"slot_id":"S1","question":"...","options":["...","...","...","..."],"correct_answer":0,"explanation":"..."}]}\n'
        "Use each evidence slot exactly once. Use only its evidence. Four distinct options; correct_answer is 0-3. "
        "Question <=18 words, each option <=10 words, explanation <=16 words. No markdown or extra fields.\n"
        f"{easy_contract}{repair_line}EVIDENCE:\n{evidence}"
    )


_EASY_NEGATIVE_STEM = re.compile(
    r"\b(?:not|except|false|incorrect|least|never)\b|\b(?:no|not|never)\b.{0,28}\b(?:without|not|never)\b",
    flags=re.IGNORECASE,
)
_EASY_TEMPLATE_STEM = re.compile(r"\.{3}|\b(?:tbd|placeholder|insert|option|answer)\b|[_]{2,}", flags=re.IGNORECASE)
_EASY_ABSOLUTES = {"always", "never", "only", "all"}
_EASY_EXTERNAL_ENTITIES = {"linux", "windows", "freertos", "vxworks", "zephyr", "unix", "android", "macos"}
_EASY_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "which", "what", "when", "where", "does",
    "are", "is", "was", "were", "into", "than", "then", "its", "their", "one", "option", "answer",
    "evidence", "context", "because", "directly", "selected",
}


def _easy_token(value: str) -> str:
    token = value.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def _easy_content_tokens(value: str) -> set[str]:
    return {
        _easy_token(token) for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", str(value))
        if len(token) >= 3 and token.lower() not in _EASY_STOPWORDS
    }


def _validate_easy_v2_quality(
    stem: str, options: list[str], answer_index: int, explanation: str, evidence: str,
) -> list[str]:
    """Hard-reject structural Easy defects and warn on fallible heuristics."""
    warnings: list[str] = []
    lowered_stem = stem.lower().strip()
    if _EASY_NEGATIVE_STEM.search(lowered_stem):
        warnings.append("negative_or_trick_stem")
    if (
        not stem.endswith("?") or _EASY_TEMPLATE_STEM.search(stem)
        or re.search(r"\b(?:of|for|and|or|the|a|an|to|is|are)\s*\?$", lowered_stem)
    ):
        raise ValueError("Easy quality: incomplete or template-like stem.")

    correct = options[answer_index]
    explicit_count = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\b", lowered_stem)
    asks_for_set = bool(re.search(r"\b(?:objectives|components|steps|goals|features|elements|requirements)\b", lowered_stem))
    answer_parts = [part for part in re.split(r"\s*(?:,|;|/|\band\b)\s*", correct, flags=re.IGNORECASE) if part]
    number_words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    if explicit_count and asks_for_set:
        requested = number_words.get(explicit_count.group(1), int(explicit_count.group(1)) if explicit_count.group(1).isdigit() else 0)
        if requested > 1 and len(answer_parts) < requested:
            warnings.append("partial_multi_part_answer")
    elif asks_for_set and re.search(r"\b(?:what|which)\b.*\b(?:are|include)\b", lowered_stem) and len(answer_parts) < 2:
        warnings.append("singular_answer_for_set_stem")

    evidence_lower = evidence.lower()
    evidence_tokens = _easy_content_tokens(evidence)
    correct_tokens = _easy_content_tokens(correct)
    explanation_tokens = _easy_content_tokens(explanation)
    if not (correct_tokens & evidence_tokens) and not (
        correct_tokens & explanation_tokens and explanation_tokens & evidence_tokens
    ):
        warnings.append("insufficient_lexical_support")

    for index, option in enumerate(options):
        option_lower = option.lower()
        raw_option_tokens = set(re.findall(r"[a-z0-9]+", option_lower))
        unsupported_absolute = next((term for term in _EASY_ABSOLUTES if term in raw_option_tokens and term not in evidence_lower), None)
        if unsupported_absolute:
            warnings.append("unsupported_absolute_term")
        unsupported_entity = next((term for term in _EASY_EXTERNAL_ENTITIES if term in option_lower and term not in evidence_lower), None)
        if unsupported_entity:
            warnings.append("outside_world_distractor")
        if index == answer_index:
            continue
        option_tokens = _easy_content_tokens(option)
        if len(option_tokens) >= 2 and len(option_tokens & evidence_tokens) / len(option_tokens) >= 0.8:
            warnings.append("distractor_similar_to_evidence")
    return list(dict.fromkeys(warnings))


def _validate_v2_question(
    raw: dict,
    groups_by_id: dict[str, dict],
    remaining_slot_ids: set[str],
    accepted_stems: list[str],
    difficulty: str,
    question_id: int,
    topic: dict,
    assessment_capacity: int,
) -> tuple[dict, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError("Question must be a JSON object.")
    stem = _clean_inline_text(raw.get("question", ""))
    if len(stem.split()) < 4 or any(re.search(pattern, stem.lower()) for pattern in GENERIC_QUESTION_PATTERNS):
        raise ValueError("Question stem is empty, generic, or unusable.")
    candidate_key = _normalize_question_key(stem)
    if any(candidate_key == _normalize_question_key(existing) for existing in accepted_stems):
        raise ValueError("Question duplicates an accepted question.")
    near_duplicate_stem = any(
        difflib.SequenceMatcher(None, candidate_key, _normalize_question_key(existing)).ratio() >= 0.94
        for existing in accepted_stems
    )

    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or len(raw_options) != 4:
        raise ValueError("Question must contain exactly 4 options.")
    option_bodies = [strip_leading_option_label(_clean_inline_text(option)) for option in raw_options]
    if len({option.lower() for option in option_bodies}) != 4:
        raise ValueError("Question options must be distinct.")
    normalized_options = [_normalize_question_key(option) for option in option_bodies]
    near_duplicate_options = any(
        difflib.SequenceMatcher(None, normalized_options[left], normalized_options[right]).ratio() >= 0.94
        for left in range(4) for right in range(left + 1, 4)
    )

    answer_index = raw.get("correct_answer")
    if isinstance(answer_index, bool) or not isinstance(answer_index, int) or answer_index not in range(4):
        raise ValueError("correct_answer must be an integer from 0 to 3.")
    slot_id = str(raw.get("slot_id") or raw.get("concept_id") or "").strip()
    if slot_id not in groups_by_id:
        raise ValueError("Question has an unknown slot_id.")
    group = groups_by_id[slot_id]
    concept_id = group["concept_id"]
    source_ids = list(group["source_chunk_ids"])
    if slot_id not in remaining_slot_ids:
        raise ValueError("Question repeats or exceeds its planned slot_id.")

    warnings = []
    if near_duplicate_stem:
        warnings.append("near_duplicate_question")
    if near_duplicate_options:
        warnings.append("near_duplicate_options")
    if any(not option for option in option_bodies):
        warnings.append("empty_option")
    explanation = _clean_inline_text(raw.get("explanation", ""))
    if difficulty == "easy":
        warnings.extend(_validate_easy_v2_quality(
            stem, option_bodies, answer_index, explanation, str(group.get("evidence_excerpt") or "")
        ))
    if len(explanation.split()) < 4:
        warnings.append("explanation_quality")
    if len(stem.split()) < 6:
        warnings.append("meaningful_concept_quality")
    try:
        _validate_difficulty_quality(stem, difficulty)
    except ValueError:
        warnings.append("difficulty_mismatch")
    for option in option_bodies:
        lowered = option.lower()
        if (
            any(re.search(pattern, lowered) for pattern in GENERIC_OPTION_PATTERNS)
            or lowered in {"all of the above", "none of the above", "cannot be determined"}
            or _looks_like_raw_chunk(option)
        ):
            warnings.append("mediocre_distractors")
            break

    authoritative_topic_id = str(group.get("topic_id") or topic["topic_id"])
    authoritative_topic_name = str(group.get("topic_name") or topic.get("name") or authoritative_topic_id)
    authoritative_capacity = int(group.get("assessment_capacity", assessment_capacity))
    normalized = {
        "id": question_id,
        "question": stem,
        "options": [canonicalize_option(option, "ABCD"[index]) for index, option in enumerate(option_bodies)],
        "correct_answer": "ABCD"[answer_index],
        "topic_id": authoritative_topic_id,
        "topic_name": authoritative_topic_name,
        "concept_id": concept_id,
        "concept_name": group["name"],
        "source_subtopic_ids": list(group.get("source_subtopic_ids") or []),
        "concept_origin": str(group.get("concept_origin") or ""),
        "concept_plan_id": str(group.get("concept_plan_id") or ""),
        "assessment_capacity": authoritative_capacity,
        "difficulty": difficulty,
        "explanation": explanation,
        "source_chunk_ids": source_ids,
        "validation_outcome": "accepted_quality_warning" if warnings else "accepted",
    }
    return normalized, sorted(set(warnings))


def _generate_topic_quiz_v2(
    document: dict,
    topic: dict,
    difficulty: str,
    owner_id: str,
    model_id: str,
    regenerate: bool,
    question_count: int = 10,
) -> dict:
    total_started = time.perf_counter()
    timings = {}
    llm_calls = 0
    generation_run_id = str(uuid4())

    stage_started = time.perf_counter()
    chunks = (
        get_document_chunks(document["id"], owner_id)
        if isinstance(topic.get("boundary"), dict)
        else get_topic_chunks(document["id"], str(topic["topic_id"]), owner_id)
    )
    topic_plan, plan_cache_timing = _get_or_build_topic_plan(document, topic, chunks, owner_id)
    timings["concept_plan_cache_hit"] = plan_cache_timing["cache_hit"]
    timings["concept_plan_cache_lookup_ms"] = plan_cache_timing["lookup_ms"]
    timings["concept_planning_ms"] = plan_cache_timing["build_ms"]
    groups = _select_v2_evidence_groups(topic, chunks, QUIZ_V2_DISTINCT_CONCEPT_LIMIT, topic_plan)
    timings["evidence_selection_ms"] = round((time.perf_counter() - stage_started) * 1000)
    if not groups:
        raise QuizGenerationError(
            "The topic does not contain canonical evidence for a grounded quiz.",
            stage="evidence_selection",
            target_questions=question_count,
        )

    planned_slots = [
        {**groups[index % len(groups)], "slot_id": f"S{index + 1}"}
        for index in range(question_count)
    ]
    groups_by_id = {slot["slot_id"]: slot for slot in planned_slots}
    remaining_slot_ids = set(groups_by_id)
    accepted = []
    accepted_stems = []
    validation_results = {
        "accepted": 0, "accepted_with_warnings": 0, "rejected": 0,
        "hard_rejections": 0, "quality_warnings": 0, "reasons": [],
    }
    generation_ms = 0
    validation_ms = 0
    repair_ms = 0
    model_load_ms = 0
    prompt_eval_ms = 0
    token_generation_ms = 0

    # One initial batch, up to two targeted repairs, then up to two bounded fills.
    generation_phases = ["initial", "repair", "repair", "fill", "fill"]
    repair_attempt_count = 0
    fill_attempt_count = 0
    final_fill_llm_calls = 0
    missing_slots_before_each_retry = []
    for attempt_index, phase in enumerate(generation_phases):
        call_started = time.perf_counter()
        missing = question_count - len(accepted)
        if missing <= 0:
            break
        available_groups = [slot for slot in planned_slots if slot["slot_id"] in remaining_slot_ids]
        if phase != "initial":
            retry_attempt = repair_attempt_count + 1 if phase == "repair" else fill_attempt_count + 1
            retry_state = {
                "phase": phase, "attempt": retry_attempt,
                "missing_slots": [slot["slot_id"] for slot in available_groups],
            }
            missing_slots_before_each_retry.append(retry_state)
            print(f"[quiz-v2-retry] {json.dumps(retry_state)}")
        if phase == "repair":
            repair_attempt_count += 1
        elif phase == "fill":
            fill_attempt_count += 1
        prompt = _build_v2_prompt(
            document["id"], topic, difficulty, available_groups, missing, accepted_stems, phase != "initial"
        )
        stage_started = time.perf_counter()
        llm_calls += 1
        if phase == "fill":
            final_fill_llm_calls += 1
        output_schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                            "slot_id": {"type": "string"},
                            "correct_answer": {"type": "integer", "minimum": 0, "maximum": 3},
                            "explanation": {"type": "string"},
                        },
                        "required": ["slot_id", "question", "options", "correct_answer", "explanation"],
                    },
                }
            },
            "required": ["questions"],
        }
        llm = ChatOllama(
            model=model_id,
            temperature=0.1 if phase == "initial" else 0.25,
            format=output_schema,
            num_ctx=16384 if missing > 15 else 8192,
            num_predict=min(4800, max(520, missing * 150)),
            keep_alive="10m",
            client_kwargs={"timeout": 270},
        )
        print(
            f"[quiz-v2-llm] attempt={attempt_index + 1}, phase={phase}, model={model_id}, "
            f"requested={missing}, evidence_groups={len(available_groups)}"
        )
        try:
            response = llm.invoke(prompt)
            metadata = getattr(response, "response_metadata", {}) or {}
            call_model_load_ms = round(float(metadata.get("load_duration") or 0) / 1_000_000)
            call_prompt_eval_ms = round(float(metadata.get("prompt_eval_duration") or 0) / 1_000_000)
            call_token_generation_ms = round(float(metadata.get("eval_duration") or 0) / 1_000_000)
            model_load_ms += call_model_load_ms
            prompt_eval_ms += call_prompt_eval_ms
            token_generation_ms += call_token_generation_ms
            print(
                f"[quiz-v2-ollama] attempt={attempt_index + 1}, model_load_ms={call_model_load_ms}, "
                f"prompt_eval_ms={call_prompt_eval_ms}, token_generation_ms={call_token_generation_ms}, "
                f"prompt_tokens={metadata.get('prompt_eval_count', 0)}, generated_tokens={metadata.get('eval_count', 0)}"
            )
            data = _extract_json(str(response.content))
            candidates = data.get("questions") if isinstance(data, dict) else None
            if not isinstance(candidates, list):
                raise ValueError("Quiz JSON does not contain a questions list.")
        except Exception as error:
            candidates = []
            validation_results["rejected"] += missing
            validation_results["reasons"].append(f"response: {error}")
        elapsed = round((time.perf_counter() - stage_started) * 1000)
        generation_ms += elapsed

        validation_started = time.perf_counter()
        for candidate_index, raw in enumerate(candidates):
            if len(accepted) >= question_count:
                break
            try:
                normalized, warnings = _validate_v2_question(
                    raw,
                    groups_by_id,
                    remaining_slot_ids,
                    accepted_stems,
                    difficulty,
                    len(accepted) + 1,
                    topic,
                    int(topic_plan["assessment_capacity"]),
                )
                accepted.append(normalized)
                accepted_stems.append(normalized["question"])
                remaining_slot_ids.remove(str(raw.get("slot_id") or raw.get("concept_id")))
                key = "accepted_with_warnings" if warnings else "accepted"
                validation_results[key] += 1
                validation_results["quality_warnings"] += len(warnings)
                save_quiz_validation_event({
                    "generation_run_id": generation_run_id,
                    "owner_id": owner_id,
                    "document_id": document["id"],
                    "document_hash": document.get("hash", ""),
                    "topic_id": topic["topic_id"],
                    "topic_schema_version": int(document.get("topic_schema_version", 0)),
                    "difficulty": difficulty,
                    "batch_index": 1,
                    "generation_attempt": attempt_index + 1,
                    "candidate_index": candidate_index,
                    "generator_model": model_id,
                    "generation_prompt_version": QUIZ_V2_PROMPT_VERSION,
                    "validator_model": "deterministic-v2",
                    "validator_prompt_version": "no-semantic-llm-v2",
                    "candidate_question": normalized,
                    "cited_chunk_ids": normalized["source_chunk_ids"],
                    "evidence_chunk_ids": normalized["source_chunk_ids"],
                    "hard_passed": True,
                    "quality_passed": not warnings,
                    "accepted": True,
                    "outcome": normalized["validation_outcome"],
                    "verdict": {"mode": "deterministic", "warnings": warnings},
                    "rejection_reasons": warnings,
                    "latency_ms": 0,
                })
            except Exception as error:
                validation_results["rejected"] += 1
                validation_results["hard_rejections"] += 1
                validation_results["reasons"].append(str(error))
                print(f"[quiz-v2-validation] discarded candidate: {error}")
        validation_ms += round((time.perf_counter() - validation_started) * 1000)
        if phase != "initial":
            repair_ms += round((time.perf_counter() - call_started) * 1000)

    timings["generation_ms"] = generation_ms
    timings["model_load_ms"] = model_load_ms
    timings["prompt_eval_ms"] = prompt_eval_ms
    timings["token_generation_ms"] = token_generation_ms
    timings["validation_ms"] = validation_ms
    timings["repair_ms"] = repair_ms
    timings["repair_llm_calls"] = repair_attempt_count + fill_attempt_count
    timings["repair_attempt_count"] = repair_attempt_count
    timings["fill_attempt_count"] = fill_attempt_count
    timings["missing_slots_before_each_retry"] = missing_slots_before_each_retry
    timings["final_fill_llm_calls"] = final_fill_llm_calls
    if len(accepted) != question_count:
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000)
        timings["total_request_ms"] = timings["total_ms"]
        print(f"[quiz-v2-timing] {json.dumps({**timings, 'llm_calls': llm_calls})}")
        missing = question_count - len(accepted)
        summary = list(dict.fromkeys(validation_results["reasons"]))[-10:]
        if not summary:
            summary = [f"No valid candidate was returned for {missing} remaining question slots."]
        raise QuizGenerationError(
            f"Quiz generation requested {question_count} questions but produced {len(accepted)} valid questions; "
            f"{missing} questions are still missing after bounded targeted repair.",
            stage="validation",
            valid_questions=len(accepted),
            target_questions=question_count,
            failure_summary=summary,
        )

    partial = False
    quiz = {
        "quiz_id": str(uuid4()),
        "document_id": document["id"],
        "document_hash": document.get("hash", ""),
        "title": document.get("title", document["id"]),
        "difficulty": difficulty,
        "topic_id": str(topic["topic_id"]),
        "topic_name": str(topic.get("name") or topic["topic_id"]),
        "assessment_scope": "topic",
        "assessment_plan": {
            "planner_version": PLANNER_VERSION,
            "scope": "topic",
            "topics": [{
                "topic_id": str(topic["topic_id"]),
                "topic_name": str(topic.get("name") or topic["topic_id"]),
                "concept_plan_id": topic_plan["concept_plan_id"],
                "assessment_capacity": int(topic_plan["assessment_capacity"]),
                "allocated_questions": question_count,
                "selected_concepts": [
                    {key: group[key] for key in (
                        "concept_id", "name", "source_subtopic_ids", "source_chunk_ids",
                        "concept_origin", "concept_plan_id",
                    )}
                    for group in groups
                ],
            }],
            "excluded_topic_ids": [],
            "target_questions": question_count,
            "total_questions": len(accepted),
            "partial": partial,
            "generation_warnings": validation_results["reasons"],
            "validation_results": validation_results,
            "llm_calls": llm_calls,
        },
        "topic_schema_version": int(document.get("topic_schema_version", 0)),
        "question_count": len(accepted),
        "created_at": utc_now_iso(),
        "questions": accepted,
    }
    if regenerate:
        delete_document_attempts(document["id"], difficulty, str(topic["topic_id"]), owner_id)
    persistence_started = time.perf_counter()
    saved = save_quiz(document["id"], difficulty, quiz, owner_id)
    timings["persistence_ms"] = round((time.perf_counter() - persistence_started) * 1000)
    timings["total_ms"] = round((time.perf_counter() - total_started) * 1000)
    timings["total_request_ms"] = timings["total_ms"]
    saved["assessment_plan"]["timings_ms"] = timings
    print(f"[quiz-v2-timing] {json.dumps({**timings, 'llm_calls': llm_calls, 'questions': len(accepted)})}")
    return saved


def _document_v2_slots(
    planned_topics: list[dict], topic_lookup: dict[str, dict], topic_chunks: dict[str, list[dict]],
) -> list[dict]:
    """Materialize allocated document slots with backend-owned hierarchy and evidence."""
    slots = []
    evidence_cache: dict[tuple[str, str], list[dict]] = {}
    for topic_plan in planned_topics:
        topic_id = str(topic_plan["topic_id"])
        topic = topic_lookup[topic_id]
        chunks = topic_chunks.get(topic_id, [])
        for concept in topic_plan.get("selected_concepts") or []:
            cache_key = (topic_id, str(concept["concept_id"]))
            if cache_key not in evidence_cache:
                evidence_cache[cache_key] = resolve_concept_evidence(topic, chunks, concept)
            evidence_chunks = evidence_cache[cache_key]
            if not evidence_chunks:
                continue
            excerpts = [
                _v2_excerpt(chunk.get("content", ""), index, len(evidence_chunks))
                for index, chunk in enumerate(evidence_chunks)
            ]
            slots.append({
                **concept,
                "slot_id": f"S{len(slots) + 1}",
                "topic_id": topic_id,
                "topic_name": str(topic_plan.get("topic_name") or topic.get("name") or topic_id),
                "concept_plan_id": str(topic_plan["concept_plan_id"]),
                "assessment_capacity": int(topic_plan["assessment_capacity"]),
                "evidence_excerpt": " ".join(excerpts)[:560],
            })
    return slots


def _document_batch_output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                        "slot_id": {"type": "string"},
                        "correct_answer": {"type": "integer", "minimum": 0, "maximum": 3},
                        "explanation": {"type": "string"},
                    },
                    "required": ["slot_id", "question", "options", "correct_answer", "explanation"],
                },
            }
        },
        "required": ["questions"],
    }


def _run_document_v2_batch(
    document: dict,
    difficulty: str,
    planned_slots: list[dict],
    owner_id: str,
    model_id: str,
    question_count: int,
    generation_run_id: str,
) -> tuple[list[dict], dict, dict]:
    """Generate document slots in bounded batches, then repair only missing slots."""
    authoritative_slots = planned_slots[:question_count]
    groups_by_id = {slot["slot_id"]: slot for slot in authoritative_slots}
    slot_positions = {slot["slot_id"]: index for index, slot in enumerate(authoritative_slots)}
    remaining_slot_ids = set(groups_by_id)
    accepted_by_slot: dict[str, dict] = {}
    accepted_stems: list[str] = []
    results = {
        "accepted": 0, "accepted_with_warnings": 0, "rejected": 0,
        "hard_rejections": 0, "quality_warnings": 0, "reasons": [],
    }
    timings = {
        "prompt_construction_ms": 0, "initial_batch_generation_ms": 0,
        "validation_ms": 0, "repair_ms": 0, "model_load_ms": 0,
        "prompt_eval_ms": 0, "token_generation_ms": 0,
        "prompt_tokens": 0, "output_tokens": 0,
        "initial_batch_count": 0, "initial_batches": [],
    }
    llm_calls = 0
    repair_llm_calls = 0
    repair_attempt_count = 0
    fill_attempt_count = 0
    final_fill_llm_calls = 0
    missing_slots_before_each_retry = []
    document_scope = {"topic_id": "document", "name": "Entire document"}

    def run_generation_call(call_slots: list[dict], phase: str, batch_index: int) -> None:
        nonlocal llm_calls, repair_llm_calls, repair_attempt_count, fill_attempt_count, final_fill_llm_calls
        attempt_started = time.perf_counter()
        requested = len(call_slots)
        call_slot_ids = {slot["slot_id"] for slot in call_slots}
        if not requested:
            return
        prompt_started = time.perf_counter()
        prompt = _build_v2_prompt(
            document["id"], document_scope, difficulty, call_slots, requested,
            accepted_stems, phase != "initial",
        )
        timings["prompt_construction_ms"] += round((time.perf_counter() - prompt_started) * 1000)
        generation_started = time.perf_counter()
        llm_calls += 1
        if phase != "initial":
            repair_llm_calls += 1
        if phase == "repair":
            repair_attempt_count += 1
        elif phase == "fill":
            fill_attempt_count += 1
        if phase == "fill":
            final_fill_llm_calls += 1
        llm = ChatOllama(
            model=model_id,
            temperature=0.1 if phase == "initial" else 0.25,
            format=_document_batch_output_schema(),
            num_ctx=16384 if requested > 15 else 8192,
            num_predict=min(4800, max(520, requested * 150)),
            keep_alive="10m",
            client_kwargs={"timeout": 270},
        )
        print(
            f"[quiz-document-llm] attempt={llm_calls}, phase={phase}, batch={batch_index}, "
            f"model={model_id}, requested={requested}, slots={len(call_slots)}"
        )
        try:
            response = llm.invoke(prompt)
            metadata = getattr(response, "response_metadata", {}) or {}
            model_load_ms = round(float(metadata.get("load_duration") or 0) / 1_000_000)
            prompt_eval_ms = round(float(metadata.get("prompt_eval_duration") or 0) / 1_000_000)
            token_generation_ms = round(float(metadata.get("eval_duration") or 0) / 1_000_000)
            prompt_tokens = int(metadata.get("prompt_eval_count") or 0)
            output_tokens = int(metadata.get("eval_count") or 0)
            timings["model_load_ms"] += model_load_ms
            timings["prompt_eval_ms"] += prompt_eval_ms
            timings["token_generation_ms"] += token_generation_ms
            timings["prompt_tokens"] += prompt_tokens
            timings["output_tokens"] += output_tokens
            data = _extract_json(str(response.content))
            candidates = data.get("questions") if isinstance(data, dict) else None
            if not isinstance(candidates, list):
                raise ValueError("Quiz JSON does not contain a questions list.")
            print(
                f"[quiz-document-ollama] attempt={llm_calls}, phase={phase}, batch={batch_index}, "
                f"returned={len(candidates)}, "
                f"model_load_ms={model_load_ms}, prompt_eval_ms={prompt_eval_ms}, "
                f"token_generation_ms={token_generation_ms}, prompt_tokens={prompt_tokens}, "
                f"output_tokens={output_tokens}"
            )
        except Exception as error:
            candidates = []
            results["rejected"] += requested
            results["reasons"].append(f"response: {error}")
        generation_elapsed = round((time.perf_counter() - generation_started) * 1000)
        if phase == "initial":
            timings["initial_batch_generation_ms"] += generation_elapsed

        validation_started = time.perf_counter()
        accepted_before = len(accepted_by_slot)
        for candidate_index, raw in enumerate(candidates):
            if len(accepted_by_slot) >= question_count:
                break
            try:
                slot_id = str(raw.get("slot_id") or raw.get("concept_id") or "").strip() if isinstance(raw, dict) else ""
                if slot_id not in call_slot_ids:
                    raise ValueError("Question uses a slot_id outside its requested batch.")
                slot = groups_by_id.get(slot_id) or {}
                normalized, warnings = _validate_v2_question(
                    raw, groups_by_id, remaining_slot_ids, accepted_stems, difficulty,
                    slot_positions.get(slot_id, len(accepted_by_slot)) + 1,
                    document_scope, int(slot.get("assessment_capacity", 0)),
                )
                accepted_by_slot[slot_id] = normalized
                accepted_stems.append(normalized["question"])
                remaining_slot_ids.remove(slot_id)
                results["accepted_with_warnings" if warnings else "accepted"] += 1
                results["quality_warnings"] += len(warnings)
                save_quiz_validation_event({
                    "generation_run_id": generation_run_id,
                    "owner_id": owner_id,
                    "document_id": document["id"],
                    "document_hash": document.get("hash", ""),
                    "topic_id": normalized["topic_id"],
                    "topic_schema_version": int(document.get("topic_schema_version", 0)),
                    "difficulty": difficulty,
                    "batch_index": batch_index,
                    "generation_attempt": llm_calls,
                    "candidate_index": candidate_index,
                    "generator_model": model_id,
                    "generation_prompt_version": QUIZ_V2_PROMPT_VERSION,
                    "validator_model": "deterministic-v2",
                    "validator_prompt_version": "no-semantic-llm-v2",
                    "candidate_question": normalized,
                    "cited_chunk_ids": normalized["source_chunk_ids"],
                    "evidence_chunk_ids": normalized["source_chunk_ids"],
                    "hard_passed": True,
                    "quality_passed": not warnings,
                    "accepted": True,
                    "outcome": normalized["validation_outcome"],
                    "verdict": {"mode": "deterministic", "warnings": warnings},
                    "rejection_reasons": warnings,
                    "latency_ms": 0,
                })
            except Exception as error:
                results["rejected"] += 1
                results["hard_rejections"] += 1
                results["reasons"].append(str(error))
                print(f"[quiz-document-validation] discarded candidate: {error}")
        validation_elapsed = round((time.perf_counter() - validation_started) * 1000)
        timings["validation_ms"] += validation_elapsed
        call_elapsed = round((time.perf_counter() - attempt_started) * 1000)
        if phase != "initial":
            timings["repair_ms"] += round((time.perf_counter() - attempt_started) * 1000)
        batch_timing = {
            "batch_index": batch_index, "phase": phase, "requested": requested,
            "returned": len(candidates), "accepted": len(accepted_by_slot) - accepted_before,
            "generation_ms": generation_elapsed, "validation_ms": validation_elapsed,
            "total_ms": call_elapsed,
        }
        if phase == "initial":
            timings["initial_batches"].append(batch_timing)
        print(f"[quiz-document-batch-timing] {json.dumps(batch_timing)}")

    initial_batches = [authoritative_slots[index:index + 10] for index in range(0, len(authoritative_slots), 10)]
    timings["initial_batch_count"] = len(initial_batches)
    for batch_index, batch_slots in enumerate(initial_batches, start=1):
        run_generation_call(batch_slots, "initial", batch_index)

    next_batch_index = len(initial_batches) + 1
    for phase, retry_limit in (("repair", 2), ("fill", 2)):
        for retry_index in range(1, retry_limit + 1):
            missing_slots = [slot for slot in authoritative_slots if slot["slot_id"] in remaining_slot_ids]
            if not missing_slots:
                break
            retry_state = {
                "phase": phase, "attempt": retry_index,
                "missing_slots": [slot["slot_id"] for slot in missing_slots],
            }
            missing_slots_before_each_retry.append(retry_state)
            print(f"[quiz-document-retry] {json.dumps(retry_state)}")
            run_generation_call(missing_slots, phase, next_batch_index)
            next_batch_index += 1

    timings["llm_calls"] = llm_calls
    timings["repair_llm_calls"] = repair_llm_calls
    timings["repair_attempt_count"] = repair_attempt_count
    timings["fill_attempt_count"] = fill_attempt_count
    timings["missing_slots_before_each_retry"] = missing_slots_before_each_retry
    timings["final_fill_llm_calls"] = final_fill_llm_calls
    accepted = [accepted_by_slot[slot["slot_id"]] for slot in authoritative_slots if slot["slot_id"] in accepted_by_slot]
    for question_id, question in enumerate(accepted, start=1):
        question["id"] = question_id
    print(f"[quiz-document-generation-timing] {json.dumps({
        'initial_batch_count': len(initial_batches),
        'initial_batch_generation_ms': timings['initial_batch_generation_ms'],
        'validation_ms': timings['validation_ms'],
        'repair_ms': timings['repair_ms'],
        'llm_calls': llm_calls,
        'repair_llm_calls': repair_llm_calls,
        'repair_attempt_count': repair_attempt_count,
        'fill_attempt_count': fill_attempt_count,
        'missing_slots_before_each_retry': missing_slots_before_each_retry,
        'final_fill_llm_calls': final_fill_llm_calls,
        'accepted': len(accepted),
    })}")
    return accepted, results, timings


def generate_quiz(
    document_id: str,
    difficulty: str,
    assessment_scope: str,
    topic_id: str | None = None,
    regenerate: bool = False,
    owner_id: str = LEGACY_USER_ID,
    model_id: str = CHAT_MODEL,
    question_count: int = 10,
) -> dict:
    """
    Generate or load the persistent quiz for one indexed document.

    If a quiz already exists, it is returned as-is so reloads or future visits do
    not create a different quiz. Passing regenerate=True intentionally replaces
    the saved quiz.
    """
    request_started = time.perf_counter()
    known_documents = _document_lookup(owner_id)
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    difficulty = difficulty.lower().strip()
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")
    if question_count not in QUIZ_V2_ALLOWED_QUESTION_COUNTS:
        raise ValueError("question_count must be one of 10, 15, 20, or 25.")
    print(f"[quiz-service] difficulty={difficulty}")
    document = known_documents[document_id]
    assessment_scope = str(assessment_scope).lower().strip()
    if assessment_scope not in {"topic", "document"}:
        raise ValueError("assessment_scope must be topic or document.")
    topic_lookup = {topic.get("topic_id"): topic for topic in document.get("topics", [])}
    if assessment_scope == "topic" and topic_id not in topic_lookup:
        raise ValueError("topic_id was not found in the selected document.")
    scope_topic_id = str(topic_id) if assessment_scope == "topic" else "document"
    topic_schema_version = int(document.get("topic_schema_version", 0))
    invalidate_document_quizzes_for_topic_schema(document_id, topic_schema_version, owner_id)
    cache_key = quiz_cache_key(document_id, difficulty, scope_topic_id, owner_id)

    if not regenerate:
        saved_quiz = get_quiz(document_id, difficulty, scope_topic_id, owner_id)
        saved_target = int((saved_quiz or {}).get("assessment_plan", {}).get(
            "target_questions", (saved_quiz or {}).get("question_count", 10)
        ))
        saved_planner = str((saved_quiz or {}).get("assessment_plan", {}).get("planner_version") or "legacy")
        saved_questions = list((saved_quiz or {}).get("questions") or [])
        saved_exact = int((saved_quiz or {}).get("question_count", 0)) == question_count == len(saved_questions)
        if saved_quiz and saved_exact and saved_target == question_count and saved_planner == PLANNER_VERSION:
            print(f"[quiz-cache] key={cache_key} HIT")
            return saved_quiz
        if saved_quiz:
            print(f"[quiz-cache] key={cache_key} MISS (planner/count compatibility)")
        else:
            print(f"[quiz-cache] key={cache_key} MISS")
    else:
        print(f"[quiz-cache] key={cache_key} MISS (regenerate)")

    if assessment_scope == "topic":
        return _generate_topic_quiz_v2(
            document=document,
            topic=topic_lookup[topic_id],
            difficulty=difficulty,
            owner_id=owner_id,
            model_id=model_id,
            regenerate=regenerate,
            question_count=question_count,
        )

    timings: dict[str, object] = {}
    topics_to_plan = list(document.get("topics") or [])
    topic_plans = []
    topic_chunks = {}
    structural_document_chunks = None
    retrieval_started = time.perf_counter()
    if any(isinstance(topic.get("boundary"), dict) for topic in topics_to_plan):
        structural_document_chunks = get_document_chunks(document_id, owner_id)
    timings["topic_chunk_retrieval_ms"] = round((time.perf_counter() - retrieval_started) * 1000)
    planning_by_topic = {}
    plan_cache_lookup_by_topic = {}
    plan_cache_hits = 0
    plan_cache_misses = 0
    for topic in topics_to_plan:
        retrieval_started = time.perf_counter()
        current_chunks = (
            structural_document_chunks
            if isinstance(topic.get("boundary"), dict)
            else get_topic_chunks(document_id, str(topic["topic_id"]), owner_id)
        )
        if structural_document_chunks is None:
            timings["topic_chunk_retrieval_ms"] += round((time.perf_counter() - retrieval_started) * 1000)
        if not current_chunks:
            continue
        topic_chunks[str(topic["topic_id"])] = current_chunks
        plan, plan_cache_timing = _get_or_build_topic_plan(document, topic, current_chunks, owner_id)
        topic_plans.append(plan)
        topic_id_value = str(topic["topic_id"])
        planning_by_topic[topic_id_value] = int(plan_cache_timing["build_ms"])
        plan_cache_lookup_by_topic[topic_id_value] = int(plan_cache_timing["lookup_ms"])
        if plan_cache_timing["cache_hit"]:
            plan_cache_hits += 1
        else:
            plan_cache_misses += 1
    timings["concept_planning_by_topic_ms"] = planning_by_topic
    timings["concept_planning_ms"] = sum(planning_by_topic.values())
    timings["concept_plan_cache_lookup_by_topic_ms"] = plan_cache_lookup_by_topic
    timings["concept_plan_cache_lookup_ms"] = sum(plan_cache_lookup_by_topic.values())
    timings["concept_plan_cache_hits"] = plan_cache_hits
    timings["concept_plan_cache_misses"] = plan_cache_misses

    allocation_started = time.perf_counter()
    allocation = allocate_document_topics(topic_plans, cap=question_count)
    planned_topics = allocation["topics"]
    excluded_topic_ids = allocation["excluded_topic_ids"]
    timings["allocation_ms"] = round((time.perf_counter() - allocation_started) * 1000)
    if not any(plan["allocated_questions"] for plan in planned_topics):
        raise ValueError("No grounded assessable concepts were found for the selected assessment scope.")

    generation_run_id = str(uuid4())
    slot_started = time.perf_counter()
    planned_slots = _document_v2_slots(planned_topics, topic_lookup, topic_chunks)
    timings["slot_build_ms"] = round((time.perf_counter() - slot_started) * 1000)
    print(
        f"[quiz-document-plan] requested={question_count}, planned_slots={len(planned_slots)}, "
        f"topics={json.dumps({plan['topic_id']: plan['allocated_questions'] for plan in planned_topics})}"
    )
    questions, validation_results, batch_timings = _run_document_v2_batch(
        document, difficulty, planned_slots, owner_id, model_id, question_count, generation_run_id,
    )
    timings.update(batch_timings)

    if len(questions) != question_count:
        missing = question_count - len(questions)
        summary = list(dict.fromkeys(validation_results["reasons"]))[-10:]
        if not summary:
            summary = [f"No grounded evidence or valid question was available for {missing} remaining slots."]
        timings["total_request_ms"] = round((time.perf_counter() - request_started) * 1000)
        print(f"[quiz-document-timing] {json.dumps({**timings, 'valid_questions': len(questions)})}")
        raise QuizGenerationError(
            f"Quiz generation requested {question_count} questions but produced {len(questions)} valid questions; "
            f"{missing} questions are still missing after bounded targeted repair.",
            stage="generation",
            valid_questions=len(questions),
            target_questions=question_count,
            failure_summary=summary,
        )

    quiz = {
        "quiz_id": str(uuid4()),
        "document_id": document_id,
        "document_hash": document.get("hash", ""),
        "title": document.get("title", document_id),
        "difficulty": difficulty,
        "topic_id": scope_topic_id,
        "topic_name": "Entire document",
        "assessment_scope": "document",
        "assessment_plan": {
            "planner_version": PLANNER_VERSION,
            "scope": "document",
            "topics": planned_topics,
            "excluded_topic_ids": excluded_topic_ids,
            "target_questions": question_count,
            "total_questions": len(questions),
            "generation_warnings": validation_results["reasons"],
            "validation_results": validation_results,
            "llm_calls": timings["llm_calls"],
        },
        "topic_schema_version": topic_schema_version,
        "question_count": len(questions),
        "created_at": utc_now_iso(),
        "questions": questions,
    }
    if regenerate:
        delete_document_attempts(document_id, difficulty, scope_topic_id, owner_id)
    print(f"[quiz-save] cache_key={cache_key}")
    persistence_started = time.perf_counter()
    saved = save_quiz(document_id, difficulty, quiz, owner_id)
    timings["persistence_ms"] = round((time.perf_counter() - persistence_started) * 1000)
    timings["total_request_ms"] = round((time.perf_counter() - request_started) * 1000)
    saved["assessment_plan"]["timings_ms"] = timings
    print(f"[quiz-document-timing] {json.dumps({**timings, 'valid_questions': len(questions)})}")
    return saved


def load_quiz_with_attempt(
    document_id: str, difficulty: str, topic_id: str, student_id: str = LEGACY_USER_ID
) -> dict:
    """Return a saved quiz plus the latest attempt for the Practice page."""
    known_documents = _document_lookup(student_id)
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")

    quiz = get_quiz(document_id, difficulty, topic_id, student_id)
    return {
        "document_id": document_id,
        "difficulty": difficulty,
        "topic_id": topic_id,
        "quiz": quiz,
        "latest_attempt": get_latest_attempt(document_id, difficulty, topic_id, student_id),
        "attempt_summary": get_quiz_attempt_summary(quiz["quiz_id"], student_id) if quiz else None,
    }


def _quiz_from_attempt_snapshot(attempt: dict) -> dict | None:
    results = list(attempt.get("question_results") or [])
    if not results or any(len(result.get("options") or []) != 4 for result in results):
        return None
    topic_id = str(attempt.get("topic_id") or "document")
    questions = [{
        "id": int(result["question_id"]), "question": result.get("question", ""),
        "options": list(result.get("options") or []), "correct_answer": result.get("correct_answer", ""),
        "topic_id": result.get("topic_id") or topic_id, "topic_name": result.get("topic_name", ""),
        "concept_id": result.get("concept_id", ""), "concept_name": "",
        "source_subtopic_ids": list(result.get("source_subtopic_ids") or []),
        "concept_origin": result.get("concept_origin", ""),
        "concept_plan_id": result.get("concept_plan_id", ""),
        "assessment_capacity": int(result.get("assessment_capacity") or 0),
        "difficulty": result.get("question_difficulty") or attempt.get("difficulty", "easy"),
        "explanation": result.get("explanation", ""),
        "source_chunk_ids": list(result.get("source_chunk_ids") or []),
        "validation_outcome": result.get("validation_outcome", "accepted"),
    } for result in results]
    return {
        "quiz_id": attempt.get("quiz_id"), "document_id": attempt.get("document_id"),
        "title": attempt.get("document_id"), "difficulty": attempt.get("difficulty", "easy"),
        "topic_id": topic_id,
        "topic_name": next((question["topic_name"] for question in questions if question["topic_name"]), ""),
        "assessment_scope": "document" if topic_id == "document" else "topic",
        "assessment_plan": {"planner_version": "persisted_attempt_snapshot", "total_questions": len(questions)},
        "question_count": len(questions),
        "created_at": attempt.get("started_at") or attempt.get("completed_at") or utc_now_iso(),
        "questions": questions,
    }


def load_quiz_for_retake(attempt_id: str, student_id: str = LEGACY_USER_ID) -> dict:
    """Resolve the exact persisted quiz behind a completed attempt."""
    attempt = get_quiz_history_attempt(attempt_id, student_id)
    if not attempt:
        raise ValueError("Completed quiz attempt was not found.")
    quiz_id = str(attempt.get("quiz_id") or "")
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else None
    if not quiz or not quiz.get("questions"):
        quiz = _quiz_from_attempt_snapshot(attempt)
    if not quiz or not quiz.get("questions"):
        raise ValueError("The persisted quiz questions are no longer available.")
    return {"quiz": quiz, "source_attempt_id": attempt_id, "attempt_summary": get_quiz_attempt_summary(quiz["quiz_id"], student_id)}


def list_quiz_statuses(owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """Return quiz/status information for every indexed document."""
    statuses = []

    for document in list_indexed_documents(owner_id):
        document_id = document["id"]
        document_quizzes = list_document_quizzes(document_id, owner_id)
        variants = [
            {
                "topic_id": quiz["topic_id"],
                "difficulty": quiz["difficulty"],
                "question_count": int(quiz.get("question_count", 0)),
            }
            for quiz in document_quizzes.values()
        ]

        statuses.append(
            {
                "document_id": document_id,
                "title": document["title"],
                "chunks": document["chunks"],
                "has_quiz": bool(variants),
                "variants": variants,
            }
        )

    return statuses


def update_quiz_progress(
    document_id: str,
    difficulty: str,
    topic_id: str,
    question_id: int,
    selected_answer: str,
    student_id: str = LEGACY_USER_ID,
) -> dict:
    """Check one answer and persist the learner's current progress immediately."""
    difficulty = difficulty.lower().strip()
    quiz = get_quiz(document_id, difficulty, topic_id, student_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    selected_answer = selected_answer.strip().upper()
    if selected_answer not in OPTION_LETTERS:
        raise ValueError("selected_answer must be A, B, C, or D.")

    questions = quiz.get("questions", [])
    target_question = next(
        (question for question in questions if int(question.get("id")) == question_id),
        None,
    )
    if not target_question:
        raise ValueError("question_id was not found in this quiz.")

    previous = get_latest_attempt(document_id, difficulty, topic_id, student_id) or {}
    answers = {str(key): value for key, value in (previous.get("answers") or {}).items()}
    answers[str(question_id)] = selected_answer
    results = []
    score = 0
    for question in questions:
        current_id = str(question.get("id"))
        if current_id not in answers:
            continue
        correct_answer = str(question.get("correct_answer", "")).upper()
        is_correct = answers[current_id] == correct_answer
        score += int(is_correct)
        results.append(
            {
                "question_id": int(question.get("id")),
                "question": question.get("question", ""),
                "options": list(question.get("options", [])),
                "selected_answer": answers[current_id],
                "correct_answer": correct_answer,
                "is_correct": is_correct,
                "question_difficulty": question.get("difficulty", difficulty),
                "validation_outcome": question.get("validation_outcome", "accepted"),
                "topic_id": question.get("topic_id", topic_id),
                "topic_name": question.get("topic_name", ""),
                "concept_id": question.get("concept_id", ""),
                "source_subtopic_ids": list(question.get("source_subtopic_ids") or []),
                "concept_origin": question.get("concept_origin", ""),
                "concept_plan_id": question.get("concept_plan_id", ""),
                "assessment_capacity": int(question.get("assessment_capacity") or 0),
                "evidence_requirement_version": "concept_coverage_v1",
                "explanation": question.get("explanation", ""),
                "source_chunk_ids": list(question.get("source_chunk_ids") or []),
            }
        )

    total = len(questions)
    now = utc_now_iso()
    quiz_id = quiz.get("quiz_id") or (
        f"{quiz_cache_key(document_id, difficulty, topic_id, student_id)}::{quiz.get('created_at', 'legacy')}"
    )
    progress = {
        "quiz_id": quiz_id,
        "document_id": document_id,
        "difficulty": difficulty,
        "topic_id": topic_id,
        "student_id": student_id,
        "started_at": previous.get("started_at") or now,
        "completed_at": now if len(results) == total else None,
        "score": score,
        "answered": len(results),
        "total": total,
        "completed": len(results) == total,
        "answers": answers,
        "question_results": results,
    }
    saved = save_quiz_progress(document_id, difficulty, progress, topic_id, student_id)
    if saved["completed"]:
        represented_topics = sorted({
            str(result.get("topic_id")) for result in results if result.get("topic_id")
        })
        saved["mastery_by_topic"] = {
            represented_topic: recompute_topic_mastery(student_id, document_id, represented_topic)
            for represented_topic in represented_topics
        }
        if len(represented_topics) == 1:
            saved["mastery"] = saved["mastery_by_topic"][represented_topics[0]]
    return saved


def submit_quiz_attempt(
    document_id: str,
    difficulty: str,
    topic_id: str,
    answers: dict[str, str],
    student_id: str = LEGACY_USER_ID,
    quiz_id: str | None = None,
) -> dict:
    """Grade a complete answer set once and append a new attempt for the saved quiz."""
    difficulty = difficulty.lower().strip()
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else get_quiz(document_id, difficulty, topic_id, student_id)
    if not quiz and quiz_id:
        source_attempt = get_latest_completed_attempt_for_quiz(quiz_id, student_id)
        quiz = _quiz_from_attempt_snapshot(source_attempt) if source_attempt else None
    if not quiz:
        raise ValueError("The persisted quiz questions are no longer available.")
    if quiz.get("document_id") != document_id or quiz.get("difficulty") != difficulty or quiz.get("topic_id") != topic_id:
        raise ValueError("The submitted quiz identity does not match its persisted questions.")
    normalized_answers = {str(key): str(value).strip().upper() for key, value in answers.items()}
    questions = list(quiz.get("questions") or [])
    expected_ids = {str(question.get("id")) for question in questions}
    if set(normalized_answers) != expected_ids:
        raise ValueError("Every quiz question must be answered exactly once before submission.")
    if any(answer not in OPTION_LETTERS for answer in normalized_answers.values()):
        raise ValueError("Every selected answer must be A, B, C, or D.")

    results = []
    for question in questions:
        question_id = str(question.get("id"))
        correct_answer = str(question.get("correct_answer", "")).upper()
        results.append({
            "question_id": int(question_id),
            "question": question.get("question", ""),
            "options": list(question.get("options", [])),
            "selected_answer": normalized_answers[question_id],
            "correct_answer": correct_answer,
            "is_correct": normalized_answers[question_id] == correct_answer,
            "question_difficulty": question.get("difficulty", difficulty),
            "validation_outcome": question.get("validation_outcome", "accepted"),
            "topic_id": question.get("topic_id", topic_id),
            "topic_name": question.get("topic_name", ""),
            "concept_id": question.get("concept_id", ""),
            "source_subtopic_ids": list(question.get("source_subtopic_ids") or []),
            "concept_origin": question.get("concept_origin", ""),
            "concept_plan_id": question.get("concept_plan_id", ""),
            "assessment_capacity": int(question.get("assessment_capacity") or 0),
            "evidence_requirement_version": "concept_coverage_v1",
            "explanation": question.get("explanation", ""),
            "source_chunk_ids": list(question.get("source_chunk_ids") or []),
        })
    score = sum(int(result["is_correct"]) for result in results)
    total = len(results)
    summary = get_quiz_attempt_summary(quiz["quiz_id"], student_id)
    now = utc_now_iso()
    saved = save_quiz_progress(document_id, difficulty, {
        "attempt_id": str(uuid4()),
        "quiz_id": quiz["quiz_id"],
        "started_at": now,
        "completed_at": now,
        "submitted_at": now,
        "score": score,
        "answered": total,
        "total": total,
        "completed": True,
        "attempt_number": int(summary["attempts"]) + 1,
        "percentage": round(100.0 * score / total, 2) if total else 0,
        "answers": normalized_answers,
        "question_results": results,
    }, topic_id, student_id)
    represented_topics = sorted({str(result["topic_id"]) for result in results if result.get("topic_id")})
    saved["mastery_by_topic"] = {
        represented_topic: recompute_topic_mastery(student_id, document_id, represented_topic)
        for represented_topic in represented_topics
    }
    if len(represented_topics) == 1:
        saved["mastery"] = saved["mastery_by_topic"][represented_topics[0]]
    saved["attempt_summary"] = get_quiz_attempt_summary(quiz["quiz_id"], student_id)
    return saved


def list_completed_quiz_attempts(
    document_id: str | None = None,
    difficulty: str | None = None,
    student_id: str = LEGACY_USER_ID,
) -> list[dict]:
    """Return compact summaries for the Quiz History UI."""
    summaries = []
    for attempt in load_quiz_history(document_id, difficulty, student_id):
        total = int(attempt.get("total", 0))
        score = int(attempt.get("score", 0))
        summaries.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "quiz_id": attempt.get("quiz_id"),
                "document_id": attempt.get("document_id"),
                "difficulty": attempt.get("difficulty", "medium"),
                "topic_id": attempt.get("topic_id") or "document",
                "topic_name": next((result.get("topic_name") for result in attempt.get("question_results", []) if result.get("topic_name")), ""),
                "score": score,
                "total": total,
                "percentage": round((score / total) * 100) if total else 0,
                "attempt_number": attempt.get("attempt_number", 0),
                "completed_at": attempt.get("completed_at") or attempt.get("submitted_at"),
            }
        )
    return summaries


def build_learning_dashboard(student_id: str = LEGACY_USER_ID) -> dict:
    """Aggregate real indexed-document, attempt, and mastery state for Overview."""
    documents = list_indexed_documents(student_id)
    mastery_rows = []
    for document in documents:
        for topic in document.get("topics", []):
            history = list_completed_answer_snapshots(student_id, document["id"], topic["topic_id"])
            mastery = {
                "student_id": student_id,
                "document_id": document["id"],
                "topic_id": topic["topic_id"],
                **calculate_mastery(history, len({row["quiz_id"] for row in history})),
            }
            mastery_rows.append({
                **mastery,
                "topic_name": topic.get("name") or topic["topic_id"],
                "document_name": document.get("title") or document["id"],
            })

    attempts = load_quiz_history(student_id=student_id)
    answer_rows = [result for attempt in attempts for result in attempt.get("question_results", [])]
    correct_answers = sum(int(bool(result.get("is_correct"))) for result in answer_rows)
    answered_questions = len(answer_rows)
    assessed = [mastery for mastery in mastery_rows if mastery.get("has_evidence")]
    latest_attempt = attempts[0] if attempts else None
    latest_summary = None
    if latest_attempt:
        represented_topics = sorted({
            str(result.get("topic_id")) for result in latest_attempt.get("question_results", [])
            if result.get("topic_id") and result.get("topic_id") != "document"
        })
        latest_summary = {
            "attempt_id": latest_attempt.get("attempt_id"),
            "document_id": latest_attempt.get("document_id"),
            "topic_id": latest_attempt.get("topic_id"),
            "difficulty": latest_attempt.get("difficulty"),
            "score": latest_attempt.get("score", 0),
            "total": latest_attempt.get("total", 0),
            "completed_at": latest_attempt.get("completed_at") or latest_attempt.get("submitted_at"),
            "represented_topic_ids": represented_topics,
        }

    mastery_by_document = {}
    for mastery in mastery_rows:
        mastery_by_document.setdefault(mastery["document_id"], []).append(mastery)
    material_rows = []
    for document in documents:
        rows = mastery_by_document.get(document["id"], [])
        material_rows.append({
            "document_id": document["id"],
            "document_name": document.get("title") or document["id"],
            "topic_count": len(document.get("topics", [])),
            "assessed_topic_count": sum(int(bool(row.get("has_evidence"))) for row in rows),
            "indexed": True,
        })

    return {
        "student_id": student_id,
        "metrics": {
            "documents": len(documents),
            "topics_assessed": len(assessed),
            "total_topics": len(mastery_rows),
            "quiz_accuracy": round(100 * correct_answers / answered_questions, 2) if answered_questions else None,
            "answered_questions": answered_questions,
            "topics_mastered": sum(row.get("mastery_level") == "Mastered" for row in mastery_rows),
        },
        "mastery": sorted(
            mastery_rows,
            key=lambda row: (not row.get("has_evidence"), -int(row.get("answered_questions", 0)), row["document_name"], row["topic_name"]),
        ),
        "latest_attempt": latest_summary,
        "materials": material_rows,
    }


def load_completed_quiz_attempt(attempt_id: str, student_id: str = LEGACY_USER_ID) -> dict:
    """Return one completed attempt with question snapshots for review."""
    attempt = get_quiz_history_attempt(attempt_id, student_id)
    if not attempt:
        raise ValueError("Quiz history attempt was not found.")
    return attempt


def clear_quiz_progress(
    document_id: str, difficulty: str, topic_id: str, student_id: str = LEGACY_USER_ID
) -> dict:
    """Reset current answers for one saved quiz."""
    if not get_quiz(document_id, difficulty, topic_id, student_id):
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    reset_quiz_progress(document_id, difficulty, topic_id, student_id)
    return {"student_id": student_id, "document_id": document_id, "topic_id": topic_id, "difficulty": difficulty, "reset": True}


def explain_quiz_question(
    document_id: str,
    difficulty: str,
    topic_id: str,
    question_id: int,
    owner_id: str = LEGACY_USER_ID,
) -> dict:
    """Return a cached or on-demand RAG explanation for one answered question."""
    quiz = get_quiz(document_id, difficulty, topic_id, owner_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    question = next(
        (item for item in quiz.get("questions", []) if int(item.get("id")) == question_id),
        None,
    )
    if not question:
        raise ValueError("question_id was not found in this quiz.")
    if question.get("explanation"):
        return {
            "document_id": document_id,
            "topic_id": topic_id,
            "difficulty": difficulty,
            "question_id": question_id,
            "explanation": question["explanation"],
            "source_chunk_ids": question.get("source_chunk_ids", []),
            "cache_hit": True,
        }
    explanation_key = f"{quiz_cache_key(document_id, difficulty, topic_id, owner_id)}::{question_id}"
    cached = get_quiz_explanation(explanation_key, owner_id)
    if cached:
        return {**cached, "cache_hit": True}

    result = explain_quiz_answer(
        owner_id=owner_id,
        document_id=document_id,
        question=question["question"],
        options=question["options"],
        correct_answer=question["correct_answer"],
    )
    saved = {
        "document_id": document_id,
        "difficulty": difficulty,
        "question_id": question_id,
        **result,
        "created_at": utc_now_iso(),
    }
    save_quiz_explanation(explanation_key, saved, owner_id)
    return {**saved, "cache_hit": False}
