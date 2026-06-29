import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config import DATA_DIR, GENERATED_QUIZZES_PATH, QUIZ_ATTEMPTS_PATH


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
    """Load all generated quizzes keyed by document_id."""
    return _read_json(GENERATED_QUIZZES_PATH, {})


def save_quizzes(quizzes: dict) -> None:
    """Persist all generated quizzes."""
    _write_json(GENERATED_QUIZZES_PATH, quizzes)


def get_quiz(document_id: str) -> dict | None:
    """Return the saved quiz for a document, if it exists."""
    return load_quizzes().get(document_id)


def save_quiz(document_id: str, quiz: dict) -> dict:
    """Store one generated quiz by document_id."""
    quizzes = load_quizzes()
    quizzes[document_id] = quiz
    save_quizzes(quizzes)
    return quiz


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


def get_latest_attempt(document_id: str) -> dict | None:
    """Return the newest saved attempt for a document, if any."""
    return (load_attempts().get(document_id) or {}).get("latest_attempt")


def save_quiz_attempt(document_id: str, attempt: dict) -> dict:
    """Save a completed quiz attempt and make it the latest result."""
    attempts = load_attempts()
    document_attempts = attempts.setdefault(
        document_id,
        {
            "document_id": document_id,
            "latest_attempt": None,
            "history": [],
        },
    )

    saved_attempt = {
        **attempt,
        "attempt_id": attempt.get("attempt_id") or str(uuid4()),
        "submitted_at": attempt.get("submitted_at") or utc_now_iso(),
    }
    document_attempts["latest_attempt"] = saved_attempt
    document_attempts.setdefault("history", []).append(saved_attempt)
    save_attempts(attempts)
    return saved_attempt


def delete_document_quiz_data(document_id: str) -> None:
    """Delete generated quiz and attempts for a removed document."""
    quizzes = load_quizzes()
    if document_id in quizzes:
        del quizzes[document_id]
        save_quizzes(quizzes)

    attempts = load_attempts()
    if document_id in attempts:
        del attempts[document_id]
        save_attempts(attempts)


def delete_document_attempts(document_id: str) -> None:
    """Delete saved attempts for a document while keeping its generated quiz."""
    attempts = load_attempts()
    if document_id in attempts:
        del attempts[document_id]
        save_attempts(attempts)
