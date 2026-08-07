import difflib
import json
import math
import re
from pathlib import Path
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_ollama import ChatOllama, OllamaEmbeddings

from backend.quiz_store import (
    delete_document_attempts,
    get_latest_attempt,
    get_quiz,
    get_quiz_explanation,
    list_document_quizzes,
    list_quiz_history as load_quiz_history,
    get_quiz_history_attempt,
    invalidate_document_quizzes_for_topic_schema,
    quiz_cache_key,
    save_quiz,
    save_quiz_explanation,
    save_quiz_progress,
    reset_quiz_progress,
    save_quiz_validation_event,
    utc_now_iso,
)
from backend.quiz_validation import validate_question_semantics
from backend.mastery_service import recompute_topic_mastery
from backend.rag_service import explain_quiz_answer
from config import (
    CHAT_MODEL,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    INDEXED_FILES_PATH,
    QUIZ_PROMPT_PATH,
    QUIZ_QUALITY_RETRY_LIMIT,
    VECTORSTORE_DIR,
)

GENERATION_PROMPT_VERSION = "topic_mcq_v1"


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
MAX_BATCH_GENERATION_ATTEMPTS = 8
QUIZ_CONTEXT_WINDOWS = (4096, 8192, 16384, 32768)


def _load_indexed_files() -> dict:
    if not INDEXED_FILES_PATH.exists():
        return {}

    with open(INDEXED_FILES_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def list_indexed_documents() -> list[dict]:
    """
    Return documents available for quiz generation.

    The hash is included internally so generated quizzes can remember which
    exact uploaded file version they belong to.
    """
    indexed_files = _load_indexed_files()
    for file_name, info in indexed_files.items():
        invalidate_document_quizzes_for_topic_schema(
            file_name, int(info.get("topic_schema_version", 0))
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


def _document_lookup() -> dict[str, dict]:
    return {document["id"]: document for document in list_indexed_documents()}


def _load_vectorstore() -> Chroma:
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    return Chroma(
        persist_directory=str(VECTORSTORE_DIR),
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
    )


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
                    "chunk_id": metadata.get("chunk_id") or str(raw_id),
                },
            }
        )

    return sorted(chunks, key=lambda item: int((item.get("metadata") or {}).get("chunk", 0)))


def get_topic_chunks(document_id: str, topic_id: str) -> list[dict]:
    """
    Load all chunks for the selected document.

    Quiz generation intentionally does not use semantic top-k retrieval. It
    fetches the selected document's chunks and samples from the whole ordered
    list so the quiz can cover beginning, middle, and end material.
    """
    vectorstore = _load_vectorstore()

    try:
        result = vectorstore.get(
            where={"$and": [{"source": document_id}, {"topic_id": topic_id}]}
        )
        chunks = _result_to_chunks(result, document_id, filter_source=False)
        if chunks:
            return chunks
    except Exception:
        pass

    result = vectorstore.get(limit=10000)
    return [
        chunk for chunk in _result_to_chunks(result, document_id, filter_source=True)
        if chunk["metadata"].get("topic_id") == topic_id
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
- include a grounded explanation and non-empty source_chunk_ids for every question
- every source_chunk_id must exactly match a Chunk ID in this batch context
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
      ,"explanation": "Grounded explanation",
      "source_chunk_ids": ["exact context Chunk ID"]
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
            options.append(f"{letter}. {value}")
        return options

    if isinstance(raw_options, list):
        options = []
        for index, option in enumerate(raw_options[:4]):
            value = str(option).strip()
            letter = "ABCD"[index]
            if not re.match(r"^[ABCD][\.\)]\s*", value, flags=re.IGNORECASE):
                value = f"{letter}. {value}"
            options.append(value)
        return options

    raise ValueError("options must be a list or object")


def _clean_inline_text(value: str) -> str:
    """Collapse model/newline artifacts while keeping code-like text readable."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _option_text(option: str) -> str:
    return re.sub(r"^[ABCD][\.\)]\s*", "", str(option).strip(), flags=re.IGNORECASE)


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
    allowed_chunk_ids: set[str] | None = None,
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
        raw_source_ids = question.get("source_chunk_ids")
        if not isinstance(raw_source_ids, list):
            raise ValueError(f"Question {offset + 1} source_chunk_ids must be a list.")
        source_chunk_ids = list(dict.fromkeys(str(value).strip() for value in raw_source_ids if str(value).strip()))
        if not source_chunk_ids:
            raise ValueError(f"Question {offset + 1} must cite at least one source chunk.")
        allowed = allowed_chunk_ids or set()
        invalid_ids = sorted(set(source_chunk_ids) - allowed)
        if invalid_ids:
            raise ValueError(
                f"Question {offset + 1} cites chunks outside its generation batch: {invalid_ids}"
            )
        normalized_questions.append(
            {
                "id": start_id + offset,
                "question": _clean_inline_text(question.get("question", "")),
                "options": options,
                "correct_answer": correct_answer,
                "topic_id": topic_id,
                "difficulty": difficulty,
                "explanation": explanation,
                "source_chunk_ids": source_chunk_ids,
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
    quality_rejections_used = 0

    for attempt_index in range(MAX_BATCH_GENERATION_ATTEMPTS):
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
                topic_name=topic_name,
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
                topic_name=topic_name,
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
            model=CHAT_MODEL,
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
            parsed = _extract_json(response.content)
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
                        batch_chunk_ids,
                    )[0]
                    cited_chunks = [chunks_by_id[chunk_id] for chunk_id in normalized["source_chunk_ids"]]
                    semantic = validate_question_semantics(
                        normalized, cited_chunks, difficulty, topic_name
                    )
                    failures = semantic.hard_failures + semantic.quality_failures
                    event = {
                        "generation_run_id": generation_run_id,
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
            f"after {MAX_BATCH_GENERATION_ATTEMPTS} attempts. "
            f"Errors: {'; '.join(generation_errors[-5:])}"
        )

    return accepted_questions


def _batch_plan(question_count: int) -> list[int]:
    plan = []
    remaining = question_count
    while remaining > 0:
        batch_size = min(MAX_QUESTIONS_PER_BATCH, remaining)
        plan.append(batch_size)
        remaining -= batch_size
    return plan


def generate_quiz(
    document_id: str,
    topic_id: str,
    question_count: int,
    difficulty: str,
    regenerate: bool = False,
) -> dict:
    """
    Generate or load the persistent quiz for one indexed document.

    If a quiz already exists, it is returned as-is so reloads or future visits do
    not create a different quiz. Passing regenerate=True intentionally replaces
    the saved quiz.
    """
    known_documents = _document_lookup()
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    if not 1 <= question_count <= 40:
        raise ValueError("question_count must be between 1 and 40.")
    difficulty = difficulty.lower().strip()
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")
    print(f"[quiz-service] difficulty={difficulty}")
    document = known_documents[document_id]
    topic_lookup = {topic.get("topic_id"): topic for topic in document.get("topics", [])}
    if topic_id not in topic_lookup:
        raise ValueError("topic_id was not found in the selected document.")
    topic = topic_lookup[topic_id]
    topic_schema_version = int(document.get("topic_schema_version", 0))
    invalidate_document_quizzes_for_topic_schema(document_id, topic_schema_version)
    cache_key = quiz_cache_key(document_id, difficulty, topic_id)

    if not regenerate:
        saved_quiz = get_quiz(document_id, difficulty, topic_id)
        if saved_quiz:
            print(f"[quiz-cache] key={cache_key} HIT")
            return saved_quiz
        print(f"[quiz-cache] key={cache_key} MISS")
    else:
        print(f"[quiz-cache] key={cache_key} MISS (regenerate)")

    # Cross-level duplicates are temporarily accepted. Do not inject saved
    # questions into the prompt or reject new questions by text similarity.
    avoid_questions: list[str] = []
    chunks = get_topic_chunks(document_id, topic_id)
    if not chunks:
        raise ValueError("No chunks were found for the selected document and topic.")

    plan = _batch_plan(question_count)
    generation_run_id = str(uuid4())
    questions = []
    next_id = 1

    for batch_index, batch_size in enumerate(plan):
        print(
            f"[quiz-context] batch={batch_index + 1}/{len(plan)}, "
            f"chunks={len(chunks)} (full document)"
        )
        batch_questions = _generate_quiz_batch(
            document_id=document_id,
            num_questions=batch_size,
            difficulty=difficulty,
            chunks=chunks,
            start_id=next_id,
            topic_id=topic_id,
            topic_name=topic.get("name", topic_id),
            avoid_questions=avoid_questions,
            generation_run_id=generation_run_id,
            batch_index=batch_index + 1,
            document_hash=document.get("hash", ""),
            topic_schema_version=topic_schema_version,
        )
        questions.extend(batch_questions)
        avoid_questions.extend(question["question"] for question in batch_questions)
        next_id += len(batch_questions)

    quiz = {
        "quiz_id": str(uuid4()),
        "document_id": document_id,
        "document_hash": document.get("hash", ""),
        "title": document.get("title", document_id),
        "difficulty": difficulty,
        "topic_id": topic_id,
        "topic_name": topic.get("name", topic_id),
        "topic_schema_version": topic_schema_version,
        "question_count": len(questions),
        "created_at": utc_now_iso(),
        "questions": questions,
    }
    if regenerate:
        delete_document_attempts(document_id, difficulty, topic_id)
    print(f"[quiz-save] cache_key={cache_key}")
    return save_quiz(document_id, difficulty, quiz)


def load_quiz_with_attempt(
    document_id: str, difficulty: str, topic_id: str, student_id: str = "local_student"
) -> dict:
    """Return a saved quiz plus the latest attempt for the Practice page."""
    known_documents = _document_lookup()
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")

    return {
        "document_id": document_id,
        "difficulty": difficulty,
        "topic_id": topic_id,
        "quiz": get_quiz(document_id, difficulty, topic_id),
        "latest_attempt": get_latest_attempt(document_id, difficulty, topic_id, student_id),
    }


def list_quiz_statuses() -> list[dict]:
    """Return quiz/status information for every indexed document."""
    statuses = []

    for document in list_indexed_documents():
        document_id = document["id"]
        document_quizzes = list_document_quizzes(document_id)
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
    student_id: str = "local_student",
) -> dict:
    """Check one answer and persist the learner's current progress immediately."""
    difficulty = difficulty.lower().strip()
    quiz = get_quiz(document_id, difficulty, topic_id)
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
            }
        )

    total = len(questions)
    now = utc_now_iso()
    quiz_id = quiz.get("quiz_id") or (
        f"{quiz_cache_key(document_id, difficulty, topic_id)}::{quiz.get('created_at', 'legacy')}"
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
        saved["mastery"] = recompute_topic_mastery(student_id, document_id, topic_id)
    return saved


def list_completed_quiz_attempts(
    document_id: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    """Return compact summaries for the Quiz History UI."""
    summaries = []
    for attempt in load_quiz_history(document_id, difficulty):
        total = int(attempt.get("total", 0))
        score = int(attempt.get("score", 0))
        summaries.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "quiz_id": attempt.get("quiz_id"),
                "document_id": attempt.get("document_id"),
                "difficulty": attempt.get("difficulty", "medium"),
                "score": score,
                "total": total,
                "percentage": round((score / total) * 100) if total else 0,
                "completed_at": attempt.get("completed_at") or attempt.get("submitted_at"),
            }
        )
    return summaries


def load_completed_quiz_attempt(attempt_id: str) -> dict:
    """Return one completed attempt with question snapshots for review."""
    attempt = get_quiz_history_attempt(attempt_id)
    if not attempt:
        raise ValueError("Quiz history attempt was not found.")
    return attempt


def clear_quiz_progress(
    document_id: str, difficulty: str, topic_id: str, student_id: str = "local_student"
) -> dict:
    """Reset current answers for one saved quiz."""
    if not get_quiz(document_id, difficulty, topic_id):
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    reset_quiz_progress(document_id, difficulty, topic_id, student_id)
    return {"student_id": student_id, "document_id": document_id, "topic_id": topic_id, "difficulty": difficulty, "reset": True}


def explain_quiz_question(
    document_id: str,
    difficulty: str,
    topic_id: str,
    question_id: int,
) -> dict:
    """Return a cached or on-demand RAG explanation for one answered question."""
    quiz = get_quiz(document_id, difficulty, topic_id)
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
    explanation_key = f"{quiz_cache_key(document_id, difficulty, topic_id)}::{question_id}"
    cached = get_quiz_explanation(explanation_key)
    if cached:
        return {**cached, "cache_hit": True}

    result = explain_quiz_answer(
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
    save_quiz_explanation(explanation_key, saved)
    return {**saved, "cache_hit": False}
