"""SQLite persistence for generated quizzes, progress, history, and explanations."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config import (
    DATABASE_PATH,
    LEGACY_GENERATED_QUIZZES_PATH,
    LEGACY_QUIZ_ATTEMPTS_PATH,
    LEGACY_QUIZ_EXPLANATIONS_PATH,
)


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection and also release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def _read_legacy_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def initialize_quiz_store() -> None:
    """Create quiz tables and import the old JSON files once."""
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                document_hash TEXT,
                title TEXT NOT NULL,
                difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'difficult')),
                question_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_quiz_variant
                ON quizzes(document_id, difficulty) WHERE is_active = 1;
            CREATE INDEX IF NOT EXISTS idx_quizzes_document
                ON quizzes(document_id, difficulty, created_at);

            CREATE TABLE IF NOT EXISTS quiz_questions (
                quiz_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                question TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                PRIMARY KEY (quiz_id, question_id),
                FOREIGN KEY (quiz_id) REFERENCES quizzes(quiz_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS quiz_options (
                quiz_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                option_letter TEXT NOT NULL,
                option_text TEXT NOT NULL,
                PRIMARY KEY (quiz_id, question_id, option_letter),
                FOREIGN KEY (quiz_id, question_id)
                    REFERENCES quiz_questions(quiz_id, question_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS quiz_attempts (
                attempt_id TEXT PRIMARY KEY,
                quiz_id TEXT,
                document_id TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                submitted_at TEXT,
                updated_at TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                answered INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
                is_latest INTEGER NOT NULL DEFAULT 1 CHECK (is_latest IN (0, 1))
            );

            CREATE INDEX IF NOT EXISTS idx_attempt_variant
                ON quiz_attempts(document_id, difficulty, is_latest, updated_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_latest_attempt_variant
                ON quiz_attempts(document_id, difficulty) WHERE is_latest = 1;
            CREATE INDEX IF NOT EXISTS idx_attempt_history
                ON quiz_attempts(completed, completed_at, submitted_at);

            CREATE TABLE IF NOT EXISTS quiz_attempt_answers (
                attempt_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                question TEXT,
                options_json TEXT NOT NULL DEFAULT '[]',
                selected_answer TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                PRIMARY KEY (attempt_id, question_id),
                FOREIGN KEY (attempt_id) REFERENCES quiz_attempts(attempt_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS quiz_explanations (
                cache_key TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_explanations_variant
                ON quiz_explanations(document_id, difficulty, question_id);
            """
        )
        migrated = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'quiz_json_migrated_v1'"
        ).fetchone()
        if migrated is None:
            _migrate_legacy_json(connection)
            connection.execute(
                "INSERT INTO app_metadata (key, value) VALUES ('quiz_json_migrated_v1', ?)",
                (utc_now_iso(),),
            )


def quiz_cache_key(document_id: str, difficulty: str) -> str:
    return f"{document_id}::{difficulty}"


def _normalize_options(options) -> list[str]:
    if isinstance(options, dict):
        return [f"{letter}. {options.get(letter, '')}" for letter in "ABCD"]
    return [str(option) for option in (options or [])][:4]


def _insert_quiz(connection: sqlite3.Connection, document_id: str, difficulty: str, quiz: dict) -> dict:
    quiz_id = str(quiz.get("quiz_id") or uuid4())
    questions = list(quiz.get("questions") or [])
    stored = {
        **quiz,
        "quiz_id": quiz_id,
        "document_id": document_id,
        "difficulty": difficulty,
        "question_count": len(questions),
        "created_at": quiz.get("created_at") or utc_now_iso(),
    }
    connection.execute(
        "UPDATE quizzes SET is_active = 0 WHERE document_id = ? AND difficulty = ?",
        (document_id, difficulty),
    )
    connection.execute("DELETE FROM quizzes WHERE quiz_id = ?", (quiz_id,))
    connection.execute(
        """
        INSERT INTO quizzes (
            quiz_id, document_id, document_hash, title, difficulty,
            question_count, created_at, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            quiz_id,
            document_id,
            stored.get("document_hash", ""),
            stored.get("title") or document_id,
            difficulty,
            len(questions),
            stored["created_at"],
        ),
    )
    for position, question in enumerate(questions, start=1):
        question_id = int(question.get("id", position))
        connection.execute(
            """
            INSERT INTO quiz_questions (
                quiz_id, question_id, position, question, correct_answer
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                quiz_id,
                question_id,
                position,
                str(question.get("question", "")),
                str(question.get("correct_answer", "")).upper(),
            ),
        )
        for option_index, option in enumerate(_normalize_options(question.get("options")), start=0):
            letter = "ABCD"[option_index]
            connection.execute(
                """
                INSERT INTO quiz_options (quiz_id, question_id, option_letter, option_text)
                VALUES (?, ?, ?, ?)
                """,
                (quiz_id, question_id, letter, option),
            )
    return stored


def _row_to_quiz(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    question_rows = connection.execute(
        """
        SELECT question_id, question, correct_answer
        FROM quiz_questions WHERE quiz_id = ? ORDER BY position
        """,
        (row["quiz_id"],),
    ).fetchall()
    questions = []
    for question_row in question_rows:
        options = connection.execute(
            """
            SELECT option_text FROM quiz_options
            WHERE quiz_id = ? AND question_id = ? ORDER BY option_letter
            """,
            (row["quiz_id"], question_row["question_id"]),
        ).fetchall()
        questions.append(
            {
                "id": question_row["question_id"],
                "question": question_row["question"],
                "options": [option["option_text"] for option in options],
                "correct_answer": question_row["correct_answer"],
            }
        )
    return {
        "quiz_id": row["quiz_id"],
        "document_id": row["document_id"],
        "document_hash": row["document_hash"] or "",
        "title": row["title"],
        "difficulty": row["difficulty"],
        "question_count": row["question_count"],
        "created_at": row["created_at"],
        "questions": questions,
    }


def get_quiz(document_id: str, difficulty: str) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM quizzes
            WHERE document_id = ? AND difficulty = ? AND is_active = 1
            """,
            (document_id, difficulty),
        ).fetchone()
        return _row_to_quiz(connection, row) if row else None


def save_quiz(document_id: str, difficulty: str, quiz: dict) -> dict:
    initialize_quiz_store()
    with _connect() as connection:
        return _insert_quiz(connection, document_id, difficulty, quiz)


def list_document_quizzes(document_id: str) -> dict[str, dict]:
    initialize_quiz_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM quizzes WHERE document_id = ? AND is_active = 1",
            (document_id,),
        ).fetchall()
        return {row["difficulty"]: _row_to_quiz(connection, row) for row in rows}


def _row_to_attempt(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    answer_rows = connection.execute(
        """
        SELECT * FROM quiz_attempt_answers
        WHERE attempt_id = ? ORDER BY question_id
        """,
        (row["attempt_id"],),
    ).fetchall()
    answers = {str(answer["question_id"]): answer["selected_answer"] for answer in answer_rows}
    question_results = [
        {
            "question_id": answer["question_id"],
            "question": answer["question"] or "",
            "options": json.loads(answer["options_json"] or "[]"),
            "selected_answer": answer["selected_answer"],
            "correct_answer": answer["correct_answer"],
            "is_correct": bool(answer["is_correct"]),
        }
        for answer in answer_rows
    ]
    result = {
        "attempt_id": row["attempt_id"],
        "quiz_id": row["quiz_id"],
        "document_id": row["document_id"],
        "difficulty": row["difficulty"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "updated_at": row["updated_at"],
        "score": row["score"],
        "answered": row["answered"],
        "total": row["total"],
        "completed": bool(row["completed"]),
        "answers": answers,
        "question_results": question_results,
    }
    if row["submitted_at"]:
        result["submitted_at"] = row["submitted_at"]
    return result


def get_latest_attempt(document_id: str, difficulty: str) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM quiz_attempts
            WHERE document_id = ? AND difficulty = ? AND is_latest = 1
            ORDER BY updated_at DESC LIMIT 1
            """,
            (document_id, difficulty),
        ).fetchone()
        return _row_to_attempt(connection, row) if row else None


def _save_attempt_row(
    connection: sqlite3.Connection,
    document_id: str,
    difficulty: str,
    progress: dict,
    previous: dict | None,
    is_latest: bool,
) -> str:
    now = utc_now_iso()
    attempt_id = str(progress.get("attempt_id") or (previous or {}).get("attempt_id") or uuid4())
    completed = bool(progress.get("completed"))
    submitted_at = progress.get("submitted_at") or (previous or {}).get("submitted_at")
    if completed and not (previous or {}).get("completed") and not submitted_at:
        submitted_at = now
    results = list(progress.get("question_results") or [])
    answers = {str(key): value for key, value in (progress.get("answers") or {}).items()}
    if not results:
        results = [
            {
                "question_id": int(question_id),
                "selected_answer": selected_answer,
                "correct_answer": "",
                "is_correct": False,
            }
            for question_id, selected_answer in answers.items()
        ]
    connection.execute(
        """
        INSERT INTO quiz_attempts (
            attempt_id, quiz_id, document_id, difficulty, started_at,
            completed_at, submitted_at, updated_at, score, answered,
            total, completed, is_latest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id) DO UPDATE SET
            quiz_id = excluded.quiz_id,
            completed_at = excluded.completed_at,
            submitted_at = excluded.submitted_at,
            updated_at = excluded.updated_at,
            score = excluded.score,
            answered = excluded.answered,
            total = excluded.total,
            completed = excluded.completed,
            is_latest = excluded.is_latest
        """,
        (
            attempt_id,
            progress.get("quiz_id"),
            document_id,
            difficulty,
            progress.get("started_at") or (previous or {}).get("started_at") or now,
            progress.get("completed_at"),
            submitted_at,
            progress.get("updated_at") or now,
            int(progress.get("score", 0)),
            int(progress.get("answered", len(results))),
            int(progress.get("total", 0)),
            int(completed),
            int(is_latest),
        ),
    )
    connection.execute("DELETE FROM quiz_attempt_answers WHERE attempt_id = ?", (attempt_id,))
    for result in results:
        question_id = int(result.get("question_id"))
        selected_answer = str(result.get("selected_answer") or answers.get(str(question_id), ""))
        connection.execute(
            """
            INSERT INTO quiz_attempt_answers (
                attempt_id, question_id, question, options_json,
                selected_answer, correct_answer, is_correct
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                question_id,
                result.get("question", ""),
                json.dumps(result.get("options") or [], ensure_ascii=False),
                selected_answer,
                str(result.get("correct_answer", "")),
                int(bool(result.get("is_correct"))),
            ),
        )
    return attempt_id


def save_quiz_progress(document_id: str, difficulty: str, progress: dict) -> dict:
    initialize_quiz_store()
    with _connect() as connection:
        previous_row = connection.execute(
            """
            SELECT * FROM quiz_attempts
            WHERE document_id = ? AND difficulty = ? AND is_latest = 1
            ORDER BY updated_at DESC LIMIT 1
            """,
            (document_id, difficulty),
        ).fetchone()
        previous = _row_to_attempt(connection, previous_row) if previous_row else None
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE document_id = ? AND difficulty = ?",
            (document_id, difficulty),
        )
        attempt_id = _save_attempt_row(
            connection, document_id, difficulty, progress, previous, is_latest=True
        )
        saved_row = connection.execute(
            "SELECT * FROM quiz_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _row_to_attempt(connection, saved_row)


def reset_quiz_progress(document_id: str, difficulty: str) -> None:
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE document_id = ? AND difficulty = ?",
            (document_id, difficulty),
        )


def get_quiz_explanation(cache_key: str) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM quiz_explanations WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None


def save_quiz_explanation(cache_key: str, explanation: dict) -> dict:
    initialize_quiz_store()
    parts = cache_key.rsplit("::", 2)
    document_id = parts[0] if len(parts) == 3 else ""
    difficulty = parts[1] if len(parts) == 3 else "medium"
    question_id = int(parts[-1]) if parts and str(parts[-1]).isdigit() else 0
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO quiz_explanations (
                cache_key, document_id, difficulty, question_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                cache_key,
                document_id,
                difficulty,
                question_id,
                json.dumps(explanation, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
    return explanation


def delete_document_quiz_data(document_id: str) -> None:
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute("DELETE FROM quizzes WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM quiz_attempts WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM quiz_explanations WHERE document_id = ?", (document_id,))


def delete_document_attempts(document_id: str, difficulty: str) -> None:
    """Reset active progress before regeneration while keeping completed history."""
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE document_id = ? AND difficulty = ?",
            (document_id, difficulty),
        )
        connection.execute(
            "DELETE FROM quiz_explanations WHERE document_id = ? AND difficulty = ?",
            (document_id, difficulty),
        )


def list_quiz_history(
    document_id: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    initialize_quiz_store()
    clauses = ["completed = 1"]
    parameters: list[str] = []
    if document_id:
        clauses.append("document_id = ?")
        parameters.append(document_id)
    if difficulty:
        clauses.append("difficulty = ?")
        parameters.append(difficulty)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM quiz_attempts WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(completed_at, submitted_at, updated_at) DESC
            """,
            parameters,
        ).fetchall()
        return [_row_to_attempt(connection, row) for row in rows]


def get_quiz_history_attempt(attempt_id: str) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM quiz_attempts WHERE attempt_id = ? AND completed = 1",
            (attempt_id,),
        ).fetchone()
        return _row_to_attempt(connection, row) if row else None


def _iter_legacy_quizzes(data: dict):
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if "questions" in value:
            if "::" in key:
                document_id, key_difficulty = key.rsplit("::", 1)
            else:
                document_id, key_difficulty = key, value.get("difficulty", "medium")
            yield document_id, value.get("difficulty", key_difficulty), value
            continue
        for difficulty in ("easy", "medium", "difficult"):
            quiz = value.get(difficulty)
            if isinstance(quiz, dict) and "questions" in quiz:
                yield key, difficulty, quiz


def _migrate_legacy_json(connection: sqlite3.Connection) -> None:
    """Import legacy quiz JSON files without deleting the original backups."""
    quiz_count = attempt_count = explanation_count = 0
    for document_id, difficulty, quiz in _iter_legacy_quizzes(
        _read_legacy_json(LEGACY_GENERATED_QUIZZES_PATH)
    ):
        if difficulty not in {"easy", "medium", "difficult"}:
            continue
        _insert_quiz(connection, document_id, difficulty, quiz)
        quiz_count += 1

    attempts_data = _read_legacy_json(LEGACY_QUIZ_ATTEMPTS_PATH)
    for document_id, document_attempts in attempts_data.items():
        if not isinstance(document_attempts, dict):
            continue
        if "latest_attempt" in document_attempts:
            difficulty = (document_attempts.get("latest_attempt") or {}).get("difficulty", "medium")
            levels = {difficulty: document_attempts}
        else:
            levels = document_attempts
        for difficulty, level_data in levels.items():
            if not isinstance(level_data, dict):
                continue
            latest = level_data.get("latest_attempt")
            latest_id = (latest or {}).get("attempt_id")
            for attempt in level_data.get("history") or []:
                if not isinstance(attempt, dict):
                    continue
                _save_attempt_row(
                    connection,
                    document_id,
                    difficulty,
                    attempt,
                    None,
                    is_latest=attempt.get("attempt_id") == latest_id,
                )
                attempt_count += 1
            if isinstance(latest, dict) and latest_id not in {
                attempt.get("attempt_id") for attempt in (level_data.get("history") or [])
                if isinstance(attempt, dict)
            }:
                _save_attempt_row(
                    connection, document_id, difficulty, latest, None, is_latest=True
                )
                attempt_count += 1

    for cache_key, explanation in _read_legacy_json(LEGACY_QUIZ_EXPLANATIONS_PATH).items():
        if not isinstance(explanation, dict):
            continue
        parts = cache_key.rsplit("::", 2)
        document_id = parts[0] if len(parts) == 3 else ""
        difficulty = parts[1] if len(parts) == 3 else "medium"
        question_id = int(parts[-1]) if str(parts[-1]).isdigit() else 0
        connection.execute(
            """
            INSERT OR REPLACE INTO quiz_explanations (
                cache_key, document_id, difficulty, question_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                document_id,
                difficulty,
                question_id,
                json.dumps(explanation, ensure_ascii=False),
                explanation.get("created_at") or utc_now_iso(),
            ),
        )
        explanation_count += 1

    if quiz_count or attempt_count or explanation_count:
        print(
            "[quiz-migration] imported "
            f"quizzes={quiz_count}, attempts={attempt_count}, explanations={explanation_count}"
        )
