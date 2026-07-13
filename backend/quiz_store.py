import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config import DATA_DIR, GENERATED_QUIZZES_PATH, QUIZ_ATTEMPTS_PATH, QUIZ_EXPLANATIONS_PATH


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for generated quizzes and submissions."""
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default

    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def load_quizzes() -> dict:
    """Load generated quizzes keyed by ``document_id::difficulty``."""
    return _read_json(GENERATED_QUIZZES_PATH, {})


def save_quizzes(quizzes: dict) -> None:
    """Persist all generated quizzes."""
    _write_json(GENERATED_QUIZZES_PATH, quizzes)


def quiz_cache_key(document_id: str, difficulty: str) -> str:
    """Build the stable persistent cache key for one quiz variant."""
    return f"{document_id}::{difficulty}"


def get_quiz(document_id: str, difficulty: str) -> dict | None:
    """Return the saved quiz for one document and difficulty."""
    quizzes = load_quizzes()
    cached_quiz = quizzes.get(quiz_cache_key(document_id, difficulty))
    if cached_quiz:
        return cached_quiz

    # Backward-compatible reads for older nested and one-quiz-per-document data.
    document_quizzes = quizzes.get(document_id) or {}
    if "questions" in document_quizzes:  # Legacy one-quiz-per-document data.
        legacy_difficulty = document_quizzes.get("difficulty", "medium")
        return document_quizzes if legacy_difficulty == difficulty else None
    return document_quizzes.get(difficulty)


def save_quiz(document_id: str, difficulty: str, quiz: dict) -> dict:
    """Store one generated quiz by document and difficulty."""
    quizzes = load_quizzes()
    quizzes[quiz_cache_key(document_id, difficulty)] = quiz
    save_quizzes(quizzes)
    return quiz


def list_document_quizzes(document_id: str) -> dict[str, dict]:
    """Return all saved difficulty variants for a document, including legacy data."""
    quizzes = load_quizzes()
    variants = {
        difficulty: quiz
        for difficulty in ("easy", "medium", "difficult")
        if (quiz := quizzes.get(quiz_cache_key(document_id, difficulty)))
    }

    legacy = quizzes.get(document_id) or {}
    if "questions" in legacy:
        variants.setdefault(legacy.get("difficulty", "medium"), legacy)
    else:
        for difficulty in ("easy", "medium", "difficult"):
            if difficulty in legacy:
                variants.setdefault(difficulty, legacy[difficulty])
    return variants


def load_attempts() -> dict:
    """
    Load quiz attempts keyed by document_id.

    Each entry contains latest_attempt plus history so the UI can show the most
    recent result while still preserving previous submissions.
    """
    return _read_json(QUIZ_ATTEMPTS_PATH, {})


def save_attempts(attempts: dict) -> None:
    """Persist quiz attempts."""
    _write_json(QUIZ_ATTEMPTS_PATH, attempts)


def get_latest_attempt(document_id: str, difficulty: str) -> dict | None:
    """Return the newest attempt for one document and difficulty."""
    document_attempts = load_attempts().get(document_id) or {}
    if "latest_attempt" in document_attempts:  # Legacy attempt data.
        legacy_difficulty = (document_attempts.get("latest_attempt") or {}).get("difficulty", "medium")
        return document_attempts.get("latest_attempt") if legacy_difficulty == difficulty else None
    return (document_attempts.get(difficulty) or {}).get("latest_attempt")


def save_quiz_progress(document_id: str, difficulty: str, progress: dict) -> dict:
    """Upsert current quiz progress and archive it once when it becomes complete."""
    attempts = load_attempts()
    document_attempts = attempts.setdefault(document_id, {})
    if "latest_attempt" in document_attempts:
        legacy_difficulty = (document_attempts.get("latest_attempt") or {}).get("difficulty", "medium")
        document_attempts = {legacy_difficulty: document_attempts}
        attempts[document_id] = document_attempts
    level_attempts = document_attempts.setdefault(
        difficulty,
        {"document_id": document_id, "difficulty": difficulty, "latest_attempt": None, "history": []},
    )
    previous = level_attempts.get("latest_attempt") or {}
    saved_progress = {
        **progress,
        "attempt_id": previous.get("attempt_id") or str(uuid4()),
        "updated_at": utc_now_iso(),
    }
    if saved_progress.get("completed") and not previous.get("completed"):
        saved_progress["submitted_at"] = utc_now_iso()
        level_attempts.setdefault("history", []).append(deepcopy(saved_progress))
    level_attempts["latest_attempt"] = saved_progress
    save_attempts(attempts)
    return saved_progress


def reset_quiz_progress(document_id: str, difficulty: str) -> None:
    """Clear current progress while preserving completed history."""
    attempts = load_attempts()
    level_attempts = (attempts.get(document_id) or {}).get(difficulty)
    if level_attempts:
        level_attempts["latest_attempt"] = None
        save_attempts(attempts)


def load_quiz_explanations() -> dict:
    return _read_json(QUIZ_EXPLANATIONS_PATH, {})


def get_quiz_explanation(cache_key: str) -> dict | None:
    return load_quiz_explanations().get(cache_key)


def save_quiz_explanation(cache_key: str, explanation: dict) -> dict:
    explanations = load_quiz_explanations()
    explanations[cache_key] = explanation
    _write_json(QUIZ_EXPLANATIONS_PATH, explanations)
    return explanation


def delete_document_quiz_data(document_id: str) -> None:
    """Delete generated quiz and attempts for a removed document."""
    quizzes = load_quizzes()
    keys_to_delete = [
        key for key in quizzes
        if key == document_id or key.startswith(f"{document_id}::")
    ]
    if keys_to_delete:
        for key in keys_to_delete:
            del quizzes[key]
        save_quizzes(quizzes)

    attempts = load_attempts()
    if document_id in attempts:
        del attempts[document_id]
        save_attempts(attempts)

    explanations = load_quiz_explanations()
    explanation_keys = [key for key in explanations if key.startswith(f"{document_id}::")]
    if explanation_keys:
        for key in explanation_keys:
            del explanations[key]
        _write_json(QUIZ_EXPLANATIONS_PATH, explanations)


def delete_document_attempts(document_id: str, difficulty: str) -> None:
    """Clear active progress for regeneration while preserving completed history."""
    attempts = load_attempts()
    document_attempts = attempts.get(document_id) or {}
    if "latest_attempt" in document_attempts:
        document_attempts["latest_attempt"] = None
        save_attempts(attempts)
    elif difficulty in document_attempts:
        document_attempts[difficulty]["latest_attempt"] = None
        save_attempts(attempts)

    explanations = load_quiz_explanations()
    prefix = f"{quiz_cache_key(document_id, difficulty)}::"
    explanation_keys = [key for key in explanations if key.startswith(prefix)]
    if explanation_keys:
        for key in explanation_keys:
            del explanations[key]
        _write_json(QUIZ_EXPLANATIONS_PATH, explanations)


def list_quiz_history(
    document_id: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    """Return completed attempts across documents and difficulty levels."""
    attempts = load_attempts()
    history = []
    for current_document_id, document_attempts in attempts.items():
        if document_id and current_document_id != document_id:
            continue
        if "history" in document_attempts:  # Legacy one-level format.
            levels = {
                (document_attempts.get("difficulty") or "medium"): document_attempts
            }
        else:
            levels = document_attempts
        for current_difficulty, level_attempts in levels.items():
            if difficulty and current_difficulty != difficulty:
                continue
            for attempt in (level_attempts or {}).get("history", []):
                history.append(attempt)
    return sorted(
        history,
        key=lambda attempt: str(attempt.get("completed_at") or attempt.get("submitted_at") or ""),
        reverse=True,
    )


def get_quiz_history_attempt(attempt_id: str) -> dict | None:
    """Find one immutable completed attempt by id."""
    return next(
        (attempt for attempt in list_quiz_history() if attempt.get("attempt_id") == attempt_id),
        None,
    )
