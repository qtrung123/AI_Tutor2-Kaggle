import json
import math
import re
from pathlib import Path

from langchain_chroma import Chroma
from langchain_ollama import ChatOllama, OllamaEmbeddings

from backend.quiz_store import (
    delete_document_attempts,
    get_latest_attempt,
    get_quiz,
    load_attempts,
    load_quizzes,
    save_quiz,
    save_quiz_attempt,
    utc_now_iso,
)
from config import (
    CHAT_MODEL,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    INDEXED_FILES_PATH,
    QUIZ_PROMPT_PATH,
    VECTORSTORE_DIR,
)


OPTION_LETTERS = {"A", "B", "C", "D"}
DIFFICULTY_ORDER = ["easy", "medium", "hard"]
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

# The local model gets a bounded amount of context per batch. Quiz generation
# still covers the full lecture by sampling different windows across the whole
# ordered chunk list instead of taking only the first or most similar chunks.
MAX_CHARS_PER_CHUNK = 900
MAX_CHUNKS_PER_BATCH = 8
MAX_QUESTIONS_PER_BATCH = 8


def clear_quiz_cache(document_id: str | None = None) -> None:
    """
    Kept for backwards-compatible imports.

    Generated quizzes are now persisted in data/generated_quizzes.json, so there
    is no RAM cache to clear.
    """
    return None


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
    return [
        {
            "id": file_name,
            "title": file_name,
            "chunks": int(info.get("chunks", 0)),
            "hash": str(info.get("hash", "")),
        }
        for file_name, info in indexed_files.items()
    ]


def _document_lookup() -> dict[str, dict]:
    return {document["id"]: document for document in list_indexed_documents()}


def determine_question_count(chunk_count: int) -> int:
    """
    Pick a quiz size from indexed lecture length.

    Rule for presentation:
    - short lecture, up to 8 chunks: 10 questions
    - medium lecture, 9-20 chunks: 15 questions
    - longer lecture, 21-40 chunks: 20 questions
    - long lecture, 41-70 chunks: 30 questions
    - very long lecture, more than 70 chunks: 40 questions maximum
    """
    if chunk_count <= 8:
        return 10
    if chunk_count <= 20:
        return 15
    if chunk_count <= 40:
        return 20
    if chunk_count <= 70:
        return 30
    return 40


def difficulty_distribution(question_count: int) -> dict[str, int]:
    """
    Split questions into 30% easy, 40% medium, 30% hard.

    Remainder questions are assigned to medium first because it is the core
    understanding level, then easy/hard as needed.
    """
    counts = {
        "easy": math.floor(question_count * 0.3),
        "medium": math.floor(question_count * 0.4),
        "hard": math.floor(question_count * 0.3),
    }

    for difficulty in ["medium", "easy", "hard"]:
        if sum(counts.values()) < question_count:
            counts[difficulty] += 1

    return counts


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
                },
            }
        )

    return sorted(chunks, key=lambda item: int((item.get("metadata") or {}).get("chunk", 0)))


def get_document_chunks(document_id: str) -> list[dict]:
    """
    Load all chunks for the selected document.

    Quiz generation intentionally does not use semantic top-k retrieval. It
    fetches the selected document's chunks and samples from the whole ordered
    list so the quiz can cover beginning, middle, and end material.
    """
    vectorstore = _load_vectorstore()

    try:
        result = vectorstore.get(where={"source": document_id})
        chunks = _result_to_chunks(result, document_id, filter_source=False)
        if chunks:
            return chunks
    except Exception:
        pass

    result = vectorstore.get(limit=10000)
    return _result_to_chunks(result, document_id, filter_source=True)


def _select_even_chunks(chunks: list[dict], target_count: int, window_index: int, window_count: int) -> list[dict]:
    """
    Pick a small window from across the whole lecture.

    Each generation batch receives a different lecture segment. Inside that
    segment, chunks are sampled evenly so the prompt does not collapse to only
    the first few chunks.
    """
    if not chunks:
        return []

    target_count = max(1, min(target_count, len(chunks)))
    window_count = max(1, window_count)
    start = math.floor(len(chunks) * window_index / window_count)
    end = math.floor(len(chunks) * (window_index + 1) / window_count)
    window = chunks[start:end] or chunks

    if len(window) <= target_count:
        return window

    selected = []
    last_index = len(window) - 1
    for index in range(target_count):
        position = round(index * last_index / max(1, target_count - 1))
        selected.append(window[position])
    return selected


def _format_context(chunks: list[dict]) -> str:
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk["metadata"]
        source = Path(str(metadata.get("source", "Unknown source"))).name
        page = str(metadata.get("page", "Unknown page"))
        chunk_number = str(metadata.get("chunk", index - 1))
        content = str(chunk["content"]).strip()
        if len(content) > MAX_CHARS_PER_CHUNK:
            content = f"{content[:MAX_CHARS_PER_CHUNK].rstrip()}..."
        parts.append(
            f"[Chunk {chunk_number}]\n"
            f"Source: {source}\n"
            f"Page: {page}\n"
            f"Content:\n{content}"
        )
    return "\n\n".join(parts)


def load_quiz_prompt_template() -> str:
    with open(QUIZ_PROMPT_PATH, "r", encoding="utf-8") as file:
        return file.read()


def _build_prompt(document_id: str, num_questions: int, difficulty: str, chunks: list[dict]) -> str:
    prompt_template = load_quiz_prompt_template()
    return (
        prompt_template
        .replace("{document_id}", document_id)
        .replace("{difficulty}", difficulty)
        .replace("{num_questions}", str(num_questions))
        .replace("{context}", _format_context(chunks))
    )


def _build_retry_prompt(
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    error: Exception,
) -> str:
    """
    Ask the model for a corrected batch after JSON or quality validation fails.

    The retry prompt is shorter and repeats the exact failure so the model can
    repair structure/quality without getting distracted by the full rubric.
    """
    return f"""
Return valid JSON only. Regenerate the quiz batch for this document.

DOCUMENT: {document_id}
DIFFICULTY: {difficulty}
NUMBER OF QUESTIONS: {num_questions}

The previous answer was rejected for this reason:
{error}

Hard requirements:
- exactly {num_questions} questions
- each question must be specific to a concept in the context
- do not start questions with "According to"
- do not ask "which statement is supported/correct/true"
- no raw chunk text as options
- no generic distractors about unrelated material, missing information, or impossible explanation
- options must be short, plausible, same-topic choices
- explanation must say why the correct answer is right and why each wrong option is wrong
- every question difficulty must be "{difficulty}"
- return JSON only, no markdown

JSON shape:
{{
  "document_id": "{document_id}",
  "questions": [
    {{
      "id": 1,
      "question": "Specific concept question",
      "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
      "correct_answer": "A",
      "explanation": {{
        "correct_answer_text": "A. ...",
        "why_correct": "...",
        "why_others_wrong": {{"A": "This is correct.", "B": "...", "C": "...", "D": "..."}},
        "short_feedback": "..."
      }},
      "difficulty": "{difficulty}",
      "source": {{"title": "{document_id}", "page": "Unknown", "chunk": 0}}
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


def _normalize_explanation(raw_explanation) -> str:
    if isinstance(raw_explanation, dict):
        parts = []
        for key in ["correct_answer_text", "why_correct", "short_feedback"]:
            value = str(raw_explanation.get(key, "")).strip()
            if value:
                parts.append(value)

        why_others_wrong = raw_explanation.get("why_others_wrong")
        if isinstance(why_others_wrong, dict):
            wrong_parts = [
                f"{letter}: {text}"
                for letter, text in why_others_wrong.items()
                if str(text).strip()
            ]
            if wrong_parts:
                parts.append("Why the other options are not correct: " + " ".join(wrong_parts))

        return _clean_inline_text(" ".join(parts))

    return _clean_inline_text(str(raw_explanation or ""))


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


def _validate_question_quality(question: dict, options: list[str], explanation: str) -> None:
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

    lowered_explanation = explanation.lower()
    if len(explanation.split()) < 18:
        raise ValueError("Explanation is too short.")
    if "grounded in the selected lecture chunk" in lowered_explanation:
        raise ValueError("Explanation is generic fallback text.")


def _validate_quiz_batch(
    data: dict,
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    start_id: int,
) -> list[dict]:
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Quiz JSON does not contain a questions list.")

    default_page = "Unknown page"
    default_chunk = 0
    if chunks:
        default_metadata = chunks[0].get("metadata") or {}
        default_page = str(default_metadata.get("page", default_page))
        default_chunk = int(default_metadata.get("chunk", default_chunk))

    normalized_questions = []
    for offset, question in enumerate(questions[:num_questions]):
        options = _normalize_options(question.get("options", []))
        if len(options) != 4:
            raise ValueError(f"Question {offset + 1} does not have exactly 4 options.")

        correct_answer = _normalize_correct_answer(question.get("correct_answer", ""), options)
        source = question.get("source") or {}
        page = str(source.get("page", default_page)).strip() or default_page
        chunk_number = source.get("chunk", default_chunk)
        try:
            chunk_number = int(chunk_number)
        except (TypeError, ValueError):
            chunk_number = default_chunk

        raw_difficulty = str(question.get("difficulty", difficulty)).lower().strip()
        if raw_difficulty not in DIFFICULTY_ORDER:
            raw_difficulty = difficulty

        normalized_questions.append(
            {
                "id": start_id + offset,
                "question": _clean_inline_text(question.get("question", "")),
                "options": options,
                "correct_answer": correct_answer,
                "explanation": _normalize_explanation(question.get("explanation", "")),
                "difficulty": raw_difficulty,
                "source": {
                    "title": document_id,
                    "page": page,
                    "chunk": chunk_number,
                },
            }
        )

        _validate_question_quality(
            question=normalized_questions[-1],
            options=options,
            explanation=normalized_questions[-1]["explanation"],
        )

    if len(normalized_questions) != num_questions:
        raise ValueError(f"Expected {num_questions} questions, got {len(normalized_questions)}.")

    return normalized_questions


def _generate_quiz_batch(
    document_id: str,
    num_questions: int,
    difficulty: str,
    chunks: list[dict],
    start_id: int,
) -> list[dict]:
    llm = ChatOllama(
        model=CHAT_MODEL,
        temperature=0,
        format="json",
        num_predict=max(1200, num_questions * 260),
    )

    try:
        prompt = _build_prompt(document_id, num_questions, difficulty, chunks)
        response = llm.invoke(prompt)
        parsed = _extract_json(response.content)
        return _validate_quiz_batch(parsed, document_id, num_questions, difficulty, chunks, start_id)
    except Exception as first_error:
        retry_prompt = _build_retry_prompt(document_id, num_questions, difficulty, chunks, first_error)
        response = llm.invoke(retry_prompt)
        try:
            parsed = _extract_json(response.content)
            return _validate_quiz_batch(parsed, document_id, num_questions, difficulty, chunks, start_id)
        except Exception as retry_error:
            raise ValueError(
                "Quiz generation failed quality validation after one retry. "
                "No low-quality fallback quiz was saved. "
                f"First error: {first_error}. Retry error: {retry_error}"
            ) from retry_error


def _batch_plan(distribution: dict[str, int]) -> list[tuple[str, int]]:
    plan = []
    for difficulty in DIFFICULTY_ORDER:
        remaining = distribution.get(difficulty, 0)
        while remaining > 0:
            batch_size = min(MAX_QUESTIONS_PER_BATCH, remaining)
            plan.append((difficulty, batch_size))
            remaining -= batch_size
    return plan


def generate_quiz(document_id: str, regenerate: bool = False) -> dict:
    """
    Generate or load the persistent quiz for one indexed document.

    If a quiz already exists, it is returned as-is so reloads or future visits do
    not create a different quiz. Passing regenerate=True intentionally replaces
    the saved quiz.
    """
    known_documents = _document_lookup()
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")

    if not regenerate:
        saved_quiz = get_quiz(document_id)
        if saved_quiz:
            return saved_quiz

    document = known_documents[document_id]
    chunk_count = int(document.get("chunks", 0))
    question_count = determine_question_count(chunk_count)
    distribution = difficulty_distribution(question_count)
    chunks = get_document_chunks(document_id)
    if not chunks:
        raise ValueError("No chunks were found for the selected document.")

    plan = _batch_plan(distribution)
    questions = []
    next_id = 1

    for batch_index, (difficulty, batch_size) in enumerate(plan):
        context_chunks = _select_even_chunks(
            chunks=chunks,
            target_count=MAX_CHUNKS_PER_BATCH,
            window_index=batch_index,
            window_count=len(plan),
        )
        batch_questions = _generate_quiz_batch(
            document_id=document_id,
            num_questions=batch_size,
            difficulty=difficulty,
            chunks=context_chunks,
            start_id=next_id,
        )
        questions.extend(batch_questions)
        next_id += len(batch_questions)

    quiz = {
        "document_id": document_id,
        "document_hash": document.get("hash", ""),
        "title": document.get("title", document_id),
        "question_count": len(questions),
        "difficulty_distribution": distribution,
        "created_at": utc_now_iso(),
        "questions": questions,
    }
    if regenerate:
        delete_document_attempts(document_id)
    return save_quiz(document_id, quiz)


def load_quiz_with_attempt(document_id: str) -> dict:
    """Return a saved quiz plus the latest attempt for the Practice page."""
    known_documents = _document_lookup()
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")

    return {
        "document_id": document_id,
        "quiz": get_quiz(document_id),
        "latest_attempt": get_latest_attempt(document_id),
    }


def list_quiz_statuses() -> list[dict]:
    """Return quiz/status information for every indexed document."""
    quizzes = load_quizzes()
    attempts = load_attempts()
    statuses = []

    for document in list_indexed_documents():
        document_id = document["id"]
        quiz = quizzes.get(document_id)
        latest_attempt = (attempts.get(document_id) or {}).get("latest_attempt")
        status = "not_generated"
        if quiz and latest_attempt:
            status = "completed" if latest_attempt.get("completed") else "in_progress"
        elif quiz:
            status = "not_started"

        statuses.append(
            {
                "document_id": document_id,
                "title": document["title"],
                "chunks": document["chunks"],
                "has_quiz": bool(quiz),
                "question_count": int((quiz or {}).get("question_count", 0)),
                "status": status,
                "latest_attempt": latest_attempt,
            }
        )

    return statuses


def submit_quiz_attempt(document_id: str, answers: dict) -> dict:
    """
    Score and persist a completed quiz submission.

    answers maps question id to the selected answer letter. The backend is the
    source of truth for scoring so frontend reloads and retakes stay consistent.
    """
    quiz = get_quiz(document_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document.")

    normalized_answers = {
        str(question_id): str(answer).strip().upper()[:1]
        for question_id, answer in (answers or {}).items()
    }
    question_results = []
    score = 0

    for question in quiz.get("questions", []):
        question_id = str(question.get("id"))
        selected_answer = normalized_answers.get(question_id, "")
        correct_answer = str(question.get("correct_answer", "")).upper()
        is_correct = selected_answer == correct_answer
        if is_correct:
            score += 1

        question_results.append(
            {
                "question_id": int(question.get("id")),
                "selected_answer": selected_answer,
                "correct_answer": correct_answer,
                "is_correct": is_correct,
            }
        )

    total = len(quiz.get("questions", []))
    attempt = {
        "document_id": document_id,
        "score": score,
        "total": total,
        "completed": True,
        "answers": normalized_answers,
        "question_results": question_results,
    }
    saved_attempt = save_quiz_attempt(document_id, attempt)

    return {
        "document_id": document_id,
        "score": score,
        "total": total,
        "completed": True,
        "submitted_at": saved_attempt["submitted_at"],
        "latest_attempt": saved_attempt,
    }
