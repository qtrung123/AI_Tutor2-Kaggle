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
from backend.auth_store import LEGACY_USER_ID, initialize_auth_store

LEGACY_TOPIC_ID = "document"


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
    initialize_auth_store()
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

            CREATE TABLE IF NOT EXISTS quiz_validation_events (
                validation_id TEXT PRIMARY KEY,
                generation_run_id TEXT NOT NULL,
                quiz_id TEXT,
                document_id TEXT NOT NULL,
                document_hash TEXT,
                topic_id TEXT NOT NULL,
                topic_schema_version INTEGER NOT NULL,
                difficulty TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                generation_attempt INTEGER NOT NULL,
                candidate_index INTEGER NOT NULL,
                generator_model TEXT NOT NULL,
                generation_prompt_version TEXT NOT NULL,
                validator_model TEXT NOT NULL,
                validator_prompt_version TEXT NOT NULL,
                candidate_question_json TEXT NOT NULL,
                cited_chunk_ids_json TEXT NOT NULL,
                evidence_chunk_ids_json TEXT NOT NULL,
                hard_passed INTEGER NOT NULL,
                quality_passed INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                verdict_json TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                latency_ms INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quiz_validation_run
                ON quiz_validation_events(generation_run_id, batch_index, generation_attempt);

            CREATE TABLE IF NOT EXISTS topic_mastery (
                student_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                mastery_score REAL NOT NULL,
                mastery_level TEXT NOT NULL,
                earned_weight REAL NOT NULL,
                possible_weight REAL NOT NULL,
                correct_answers INTEGER NOT NULL,
                answered_questions INTEGER NOT NULL,
                completed_attempts INTEGER NOT NULL,
                has_evidence INTEGER NOT NULL,
                has_sufficient_evidence INTEGER NOT NULL,
                minimum_questions_required INTEGER NOT NULL,
                formula_version TEXT NOT NULL,
                formula_config_json TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                PRIMARY KEY (student_id, document_id, topic_id)
            );
            """
        )
        migrations = {
            "quizzes": {
                "owner_id": f"TEXT NOT NULL DEFAULT '{LEGACY_USER_ID}'",
                "topic_id": f"TEXT NOT NULL DEFAULT '{LEGACY_TOPIC_ID}'",
                "topic_name": "TEXT NOT NULL DEFAULT 'Whole document'",
                "topic_schema_version": "INTEGER NOT NULL DEFAULT 0",
                "assessment_scope": "TEXT NOT NULL DEFAULT 'topic'",
                "assessment_plan_json": "TEXT NOT NULL DEFAULT '{}'",
                "planner_version": "TEXT NOT NULL DEFAULT 'legacy'",
            },
            "quiz_questions": {
                "topic_id": f"TEXT NOT NULL DEFAULT '{LEGACY_TOPIC_ID}'",
                "difficulty": "TEXT NOT NULL DEFAULT 'easy'",
                "explanation": "TEXT NOT NULL DEFAULT ''",
                "source_chunk_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "validation_outcome": "TEXT NOT NULL DEFAULT 'accepted'",
                "topic_name": "TEXT NOT NULL DEFAULT ''",
                "concept_id": "TEXT NOT NULL DEFAULT ''",
                "concept_name": "TEXT NOT NULL DEFAULT ''",
                "assessment_capacity": "INTEGER NOT NULL DEFAULT 0",
                "source_subtopic_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "concept_origin": "TEXT NOT NULL DEFAULT ''",
                "concept_plan_id": "TEXT NOT NULL DEFAULT ''",
            },
            "quiz_attempts": {
                "topic_id": f"TEXT NOT NULL DEFAULT '{LEGACY_TOPIC_ID}'",
                "student_id": "TEXT NOT NULL DEFAULT 'local_student'",
                "attempt_number": "INTEGER NOT NULL DEFAULT 0",
                "percentage": "REAL NOT NULL DEFAULT 0",
            },
            "quiz_attempt_answers": {
                "question_difficulty": "TEXT NOT NULL DEFAULT 'easy'",
                "validation_outcome": "TEXT NOT NULL DEFAULT 'accepted'",
                "topic_id": f"TEXT NOT NULL DEFAULT '{LEGACY_TOPIC_ID}'",
                "topic_name": "TEXT NOT NULL DEFAULT ''",
                "concept_id": "TEXT NOT NULL DEFAULT ''",
                "assessment_capacity": "INTEGER NOT NULL DEFAULT 0",
                "evidence_requirement_version": "TEXT NOT NULL DEFAULT 'question_count_v1'",
                "explanation": "TEXT NOT NULL DEFAULT ''",
                "source_chunk_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_subtopic_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "concept_origin": "TEXT NOT NULL DEFAULT ''",
                "concept_plan_id": "TEXT NOT NULL DEFAULT ''",
            },
            "topic_mastery": {
                "assessment_capacity": "INTEGER NOT NULL DEFAULT 0",
                "distinct_concepts_assessed": "INTEGER NOT NULL DEFAULT 0",
                "concept_coverage_ratio": "REAL NOT NULL DEFAULT 0",
                "required_concept_coverage": "REAL NOT NULL DEFAULT 0",
                "required_concepts": "INTEGER NOT NULL DEFAULT 0",
            },
            "quiz_explanations": {
                "owner_id": f"TEXT NOT NULL DEFAULT '{LEGACY_USER_ID}'",
            },
            "quiz_validation_events": {
                "owner_id": f"TEXT NOT NULL DEFAULT '{LEGACY_USER_ID}'",
            },
        }
        added_columns = set()
        for table, columns in migrations.items():
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    added_columns.add((table, column))
        if ("quiz_attempt_answers", "question_difficulty") in added_columns:
            connection.execute(
                """
                UPDATE quiz_attempt_answers
                SET question_difficulty = COALESCE(
                    (SELECT difficulty FROM quiz_attempts
                     WHERE quiz_attempts.attempt_id = quiz_attempt_answers.attempt_id),
                    'easy'
                )
                """
            )
        if ("quiz_attempt_answers", "topic_id") in added_columns:
            connection.execute(
                """
                UPDATE quiz_attempt_answers
                SET topic_id = COALESCE(
                    (SELECT topic_id FROM quiz_attempts
                     WHERE quiz_attempts.attempt_id = quiz_attempt_answers.attempt_id),
                    'document'
                )
                """
            )
        if ("quiz_attempts", "attempt_number") in added_columns:
            connection.execute(
                """
                UPDATE quiz_attempts AS current
                SET attempt_number = (
                    SELECT COUNT(*) FROM quiz_attempts AS earlier
                    WHERE earlier.student_id = current.student_id
                      AND earlier.quiz_id = current.quiz_id
                      AND earlier.completed = 1
                      AND (COALESCE(earlier.completed_at, earlier.submitted_at, earlier.updated_at) <
                           COALESCE(current.completed_at, current.submitted_at, current.updated_at)
                           OR (COALESCE(earlier.completed_at, earlier.submitted_at, earlier.updated_at) =
                               COALESCE(current.completed_at, current.submitted_at, current.updated_at)
                               AND earlier.attempt_id <= current.attempt_id))
                )
                WHERE current.completed = 1
                """
            )
        if ("quiz_attempts", "percentage") in added_columns:
            connection.execute(
                "UPDATE quiz_attempts SET percentage = CASE WHEN total > 0 THEN ROUND(100.0 * score / total, 2) ELSE 0 END"
            )
        connection.executescript(
            """
            DROP INDEX IF EXISTS idx_active_quiz_variant;
            CREATE UNIQUE INDEX idx_active_quiz_variant
                ON quizzes(owner_id, document_id, topic_id, difficulty) WHERE is_active = 1;
            DROP INDEX IF EXISTS idx_latest_attempt_variant;
            CREATE UNIQUE INDEX idx_latest_attempt_variant
                ON quiz_attempts(student_id, document_id, topic_id, difficulty) WHERE is_latest = 1;
            DROP INDEX IF EXISTS idx_attempt_variant;
            CREATE INDEX idx_attempt_variant
                ON quiz_attempts(student_id, document_id, topic_id, difficulty, is_latest, updated_at);
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


def quiz_cache_key(document_id: str, difficulty: str, topic_id: str = LEGACY_TOPIC_ID, owner_id: str = LEGACY_USER_ID) -> str:
    return f"{owner_id}::{document_id}::{topic_id}::{difficulty}"


def _normalize_options(options) -> list[str]:
    if isinstance(options, dict):
        return [f"{letter}. {options.get(letter, '')}" for letter in "ABCD"]
    return [str(option) for option in (options or [])][:4]


def _insert_quiz(connection: sqlite3.Connection, document_id: str, difficulty: str, quiz: dict, owner_id: str = LEGACY_USER_ID) -> dict:
    quiz_id = str(quiz.get("quiz_id") or uuid4())
    questions = list(quiz.get("questions") or [])
    stored = {
        **quiz,
        "quiz_id": quiz_id,
        "document_id": document_id,
        "difficulty": difficulty,
        "owner_id": owner_id,
        "topic_id": str(quiz.get("topic_id") or LEGACY_TOPIC_ID),
        "question_count": len(questions),
        "created_at": quiz.get("created_at") or utc_now_iso(),
    }
    connection.execute(
        "UPDATE quizzes SET is_active = 0 WHERE owner_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ?",
        (owner_id, document_id, stored["topic_id"], difficulty),
    )
    connection.execute("DELETE FROM quizzes WHERE quiz_id = ?", (quiz_id,))
    connection.execute(
        """
        INSERT INTO quizzes (
            quiz_id, document_id, document_hash, title, difficulty,
            question_count, created_at, is_active, topic_id, topic_name, topic_schema_version,
            assessment_scope, assessment_plan_json, planner_version, owner_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quiz_id,
            document_id,
            stored.get("document_hash", ""),
            stored.get("title") or document_id,
            difficulty,
            len(questions),
            stored["created_at"],
            stored["topic_id"],
            stored.get("topic_name", ""),
            int(stored.get("topic_schema_version", 0)),
            stored.get("assessment_scope", "topic"),
            json.dumps(stored.get("assessment_plan") or {}, ensure_ascii=False),
            str((stored.get("assessment_plan") or {}).get("planner_version") or "legacy"),
            owner_id,
        ),
    )
    for position, question in enumerate(questions, start=1):
        question_id = int(question.get("id", position))
        connection.execute(
            """
            INSERT INTO quiz_questions (
                quiz_id, question_id, position, question, correct_answer,
                topic_id, difficulty, explanation, source_chunk_ids_json, validation_outcome
                , topic_name, concept_id, concept_name, assessment_capacity,
                source_subtopic_ids_json, concept_origin, concept_plan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quiz_id,
                question_id,
                position,
                str(question.get("question", "")),
                str(question.get("correct_answer", "")).upper(),
                str(question.get("topic_id") or stored["topic_id"]),
                str(question.get("difficulty") or difficulty),
                str(question.get("explanation", "")),
                json.dumps(question.get("source_chunk_ids") or [], ensure_ascii=False),
                str(question.get("validation_outcome") or "accepted"),
                str(question.get("topic_name") or stored.get("topic_name", "")),
                str(question.get("concept_id") or ""),
                str(question.get("concept_name") or ""),
                int(question.get("assessment_capacity") or 0),
                json.dumps(question.get("source_subtopic_ids") or [], ensure_ascii=False),
                str(question.get("concept_origin") or ""),
                str(question.get("concept_plan_id") or ""),
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
        SELECT question_id, question, correct_answer, topic_id, difficulty,
               explanation, source_chunk_ids_json, validation_outcome,
               topic_name, concept_id, concept_name, assessment_capacity,
               source_subtopic_ids_json, concept_origin, concept_plan_id
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
                "topic_id": question_row["topic_id"],
                "difficulty": question_row["difficulty"],
                "explanation": question_row["explanation"],
                "source_chunk_ids": json.loads(question_row["source_chunk_ids_json"] or "[]"),
                "validation_outcome": question_row["validation_outcome"],
                "topic_name": question_row["topic_name"],
                "concept_id": question_row["concept_id"],
                "concept_name": question_row["concept_name"],
                "assessment_capacity": question_row["assessment_capacity"],
                "source_subtopic_ids": json.loads(question_row["source_subtopic_ids_json"] or "[]"),
                "concept_origin": question_row["concept_origin"],
                "concept_plan_id": question_row["concept_plan_id"],
            }
        )
    return {
        "quiz_id": row["quiz_id"],
        "document_id": row["document_id"],
        "owner_id": row["owner_id"],
        "document_hash": row["document_hash"] or "",
        "title": row["title"],
        "difficulty": row["difficulty"],
        "topic_id": row["topic_id"],
        "topic_name": row["topic_name"],
        "topic_schema_version": row["topic_schema_version"],
        "question_count": row["question_count"],
        "assessment_scope": (
            "document" if row["topic_id"] == LEGACY_TOPIC_ID and row["planner_version"] == "legacy"
            else row["assessment_scope"]
        ),
        "assessment_plan": json.loads(row["assessment_plan_json"] or "{}"),
        "created_at": row["created_at"],
        "questions": questions,
    }


def get_quiz(document_id: str, difficulty: str, topic_id: str = LEGACY_TOPIC_ID, owner_id: str = LEGACY_USER_ID) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM quizzes
            WHERE owner_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ? AND is_active = 1
            """,
            (owner_id, document_id, topic_id, difficulty),
        ).fetchone()
        return _row_to_quiz(connection, row) if row else None


def get_quiz_by_id(quiz_id: str, owner_id: str = LEGACY_USER_ID) -> dict | None:
    """Load an active or historical persisted quiz by its immutable id."""
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM quizzes WHERE quiz_id = ? AND owner_id = ?",
            (quiz_id, owner_id),
        ).fetchone()
        return _row_to_quiz(connection, row) if row else None


def save_quiz(document_id: str, difficulty: str, quiz: dict, owner_id: str = LEGACY_USER_ID) -> dict:
    initialize_quiz_store()
    with _connect() as connection:
        return _insert_quiz(connection, document_id, difficulty, quiz, owner_id)


def list_document_quizzes(document_id: str, owner_id: str = LEGACY_USER_ID) -> dict[str, dict]:
    initialize_quiz_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM quizzes WHERE owner_id = ? AND document_id = ? AND is_active = 1",
            (owner_id, document_id),
        ).fetchall()
        return {
            f"{row['topic_id']}::{row['difficulty']}": _row_to_quiz(connection, row)
            for row in rows
        }


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
            "question_difficulty": answer["question_difficulty"],
            "validation_outcome": answer["validation_outcome"],
            "topic_id": answer["topic_id"],
            "topic_name": answer["topic_name"],
            "concept_id": answer["concept_id"],
            "assessment_capacity": answer["assessment_capacity"],
            "source_subtopic_ids": json.loads(answer["source_subtopic_ids_json"] or "[]"),
            "concept_origin": answer["concept_origin"],
            "concept_plan_id": answer["concept_plan_id"],
            "evidence_requirement_version": answer["evidence_requirement_version"],
            "explanation": answer["explanation"],
            "source_chunk_ids": json.loads(answer["source_chunk_ids_json"] or "[]"),
        }
        for answer in answer_rows
    ]
    result = {
        "attempt_id": row["attempt_id"],
        "quiz_id": row["quiz_id"],
        "document_id": row["document_id"],
        "difficulty": row["difficulty"],
        "topic_id": row["topic_id"],
        "student_id": row["student_id"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "updated_at": row["updated_at"],
        "score": row["score"],
        "answered": row["answered"],
        "total": row["total"],
        "completed": bool(row["completed"]),
        "attempt_number": int(row["attempt_number"]),
        "percentage": float(row["percentage"]),
        "answers": answers,
        "question_results": question_results,
    }
    if row["submitted_at"]:
        result["submitted_at"] = row["submitted_at"]
    return result


def get_latest_attempt(
    document_id: str, difficulty: str, topic_id: str = LEGACY_TOPIC_ID,
    student_id: str = "local_student",
) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM quiz_attempts
            WHERE student_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ? AND is_latest = 1
            ORDER BY updated_at DESC LIMIT 1
            """,
            (student_id, document_id, topic_id, difficulty),
        ).fetchone()
        return _row_to_attempt(connection, row) if row else None


def _save_attempt_row(
    connection: sqlite3.Connection,
    document_id: str,
    difficulty: str,
    topic_id: str,
    student_id: str,
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
            total, completed, is_latest, topic_id, student_id, attempt_number, percentage
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id) DO UPDATE SET
            quiz_id = excluded.quiz_id,
            completed_at = excluded.completed_at,
            submitted_at = excluded.submitted_at,
            updated_at = excluded.updated_at,
            score = excluded.score,
            answered = excluded.answered,
            total = excluded.total,
            completed = excluded.completed,
            is_latest = excluded.is_latest,
            attempt_number = excluded.attempt_number,
            percentage = excluded.percentage
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
            topic_id,
            student_id,
            int(progress.get("attempt_number", 0)),
            float(progress.get("percentage", 0)),
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
                , question_difficulty, validation_outcome, topic_id, topic_name,
                concept_id, assessment_capacity, evidence_requirement_version,
                explanation, source_chunk_ids_json, source_subtopic_ids_json,
                concept_origin, concept_plan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                question_id,
                result.get("question", ""),
                json.dumps(result.get("options") or [], ensure_ascii=False),
                selected_answer,
                str(result.get("correct_answer", "")),
                int(bool(result.get("is_correct"))),
                str(result.get("question_difficulty") or difficulty),
                str(result.get("validation_outcome") or "accepted"),
                str(result.get("topic_id") or topic_id),
                str(result.get("topic_name") or ""),
                str(result.get("concept_id") or ""),
                int(result.get("assessment_capacity") or 0),
                str(result.get("evidence_requirement_version") or "concept_coverage_v1"),
                str(result.get("explanation") or ""),
                json.dumps(result.get("source_chunk_ids") or [], ensure_ascii=False),
                json.dumps(result.get("source_subtopic_ids") or [], ensure_ascii=False),
                str(result.get("concept_origin") or ""),
                str(result.get("concept_plan_id") or ""),
            ),
        )
    return attempt_id


def save_quiz_progress(
    document_id: str, difficulty: str, progress: dict, topic_id: str = LEGACY_TOPIC_ID,
    student_id: str = "local_student",
) -> dict:
    initialize_quiz_store()
    with _connect() as connection:
        previous_row = connection.execute(
            """
            SELECT * FROM quiz_attempts
            WHERE student_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ? AND is_latest = 1
            ORDER BY updated_at DESC LIMIT 1
            """,
            (student_id, document_id, topic_id, difficulty),
        ).fetchone()
        previous = _row_to_attempt(connection, previous_row) if previous_row else None
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE student_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ?",
            (student_id, document_id, topic_id, difficulty),
        )
        attempt_id = _save_attempt_row(
            connection, document_id, difficulty, topic_id, student_id, progress, previous, is_latest=True
        )
        saved_row = connection.execute(
            "SELECT * FROM quiz_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _row_to_attempt(connection, saved_row)


def reset_quiz_progress(
    document_id: str, difficulty: str, topic_id: str = LEGACY_TOPIC_ID,
    student_id: str = "local_student",
) -> None:
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE student_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ?",
            (student_id, document_id, topic_id, difficulty),
        )


def get_quiz_explanation(cache_key: str, owner_id: str = LEGACY_USER_ID) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM quiz_explanations WHERE cache_key = ? AND owner_id = ?", (cache_key, owner_id)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None


def save_quiz_explanation(cache_key: str, explanation: dict, owner_id: str = LEGACY_USER_ID) -> dict:
    initialize_quiz_store()
    document_id = str(explanation.get("document_id") or "")
    difficulty = str(explanation.get("difficulty") or "medium")
    question_id = int(explanation.get("question_id") or 0)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO quiz_explanations (
                cache_key, document_id, difficulty, question_id, payload_json, created_at, owner_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                owner_id,
            ),
        )
    return explanation


def delete_document_quiz_data(document_id: str, owner_id: str = LEGACY_USER_ID) -> None:
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute("DELETE FROM quizzes WHERE owner_id = ? AND document_id = ?", (owner_id, document_id))
        connection.execute("DELETE FROM quiz_attempts WHERE student_id = ? AND document_id = ?", (owner_id, document_id))
        connection.execute("DELETE FROM quiz_explanations WHERE owner_id = ? AND document_id = ?", (owner_id, document_id))
        connection.execute("DELETE FROM topic_mastery WHERE student_id = ? AND document_id = ?", (owner_id, document_id))
        connection.execute("DELETE FROM quiz_validation_events WHERE owner_id = ? AND document_id = ?", (owner_id, document_id))


def delete_document_attempts(
    document_id: str, difficulty: str, topic_id: str = LEGACY_TOPIC_ID, owner_id: str = LEGACY_USER_ID
) -> None:
    """Reset active progress before regeneration while keeping completed history."""
    initialize_quiz_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE quiz_attempts SET is_latest = 0 WHERE student_id = ? AND document_id = ? AND topic_id = ? AND difficulty = ?",
            (owner_id, document_id, topic_id, difficulty),
        )
        connection.execute(
            "DELETE FROM quiz_explanations WHERE owner_id = ? AND document_id = ? AND difficulty = ?",
            (owner_id, document_id, difficulty),
        )


def invalidate_document_quizzes_for_topic_schema(
    document_id: str, topic_schema_version: int, owner_id: str = LEGACY_USER_ID
) -> int:
    """Deactivate quizzes created for an older topic map without erasing history.

    Quiz ids are immutable references held by an open browser and by completed
    attempts.  Physically deleting a stale quiz cascades into ``quiz_questions``
    and makes those references impossible to submit, review, or retake.  An
    inactive row is excluded from variant/cache lookups while remaining
    available through the exact-id historical path.
    """
    initialize_quiz_store()
    with _connect() as connection:
        quiz_ids = [
            row["quiz_id"]
            for row in connection.execute(
                "SELECT quiz_id FROM quizzes WHERE owner_id = ? AND document_id = ? AND topic_schema_version != ?",
                (owner_id, document_id, int(topic_schema_version)),
            ).fetchall()
        ]
        connection.execute(
            """
            DELETE FROM quiz_attempts
            WHERE student_id = ? AND document_id = ? AND completed = 0
              AND (quiz_id IS NULL OR quiz_id IN (
                  SELECT quiz_id FROM quizzes WHERE owner_id = ? AND document_id = ? AND topic_schema_version != ?
              ))
            """,
            (owner_id, document_id, owner_id, document_id, int(topic_schema_version)),
        )
        connection.execute(
            """
            UPDATE quiz_attempts SET is_latest = 0
            WHERE student_id = ? AND document_id = ? AND completed = 1 AND quiz_id IN (
                SELECT quiz_id FROM quizzes WHERE owner_id = ? AND document_id = ? AND topic_schema_version != ?
            )
            """,
            (owner_id, document_id, owner_id, document_id, int(topic_schema_version)),
        )
        connection.execute(
            "UPDATE quizzes SET is_active = 0 WHERE owner_id = ? AND document_id = ? AND topic_schema_version != ?",
            (owner_id, document_id, int(topic_schema_version)),
        )
        connection.execute("DELETE FROM topic_mastery WHERE student_id = ? AND document_id = ?", (owner_id, document_id))
        return len(quiz_ids)


def save_quiz_validation_event(event: dict) -> dict:
    """Append one automated validation judgment for later evaluation."""
    initialize_quiz_store()
    stored = {
        **event,
        "validation_id": str(event.get("validation_id") or uuid4()),
        "created_at": event.get("created_at") or utc_now_iso(),
    }
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO quiz_validation_events (
                validation_id, generation_run_id, quiz_id, document_id, document_hash,
                topic_id, topic_schema_version, difficulty, batch_index,
                generation_attempt, candidate_index, generator_model,
                generation_prompt_version, validator_model, validator_prompt_version,
                candidate_question_json, cited_chunk_ids_json, evidence_chunk_ids_json,
                hard_passed, quality_passed, accepted, outcome, verdict_json,
                rejection_reasons_json, latency_ms, created_at, owner_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored["validation_id"], stored["generation_run_id"], stored.get("quiz_id"),
                stored["document_id"], stored.get("document_hash", ""), stored["topic_id"],
                int(stored.get("topic_schema_version", 0)), stored["difficulty"],
                int(stored.get("batch_index", 0)), int(stored.get("generation_attempt", 0)),
                int(stored.get("candidate_index", 0)), stored["generator_model"],
                stored["generation_prompt_version"], stored["validator_model"],
                stored["validator_prompt_version"],
                json.dumps(stored.get("candidate_question") or {}, ensure_ascii=False),
                json.dumps(stored.get("cited_chunk_ids") or [], ensure_ascii=False),
                json.dumps(stored.get("evidence_chunk_ids") or [], ensure_ascii=False),
                int(bool(stored.get("hard_passed"))), int(bool(stored.get("quality_passed"))),
                int(bool(stored.get("accepted"))), stored["outcome"],
                json.dumps(stored.get("verdict") or {}, ensure_ascii=False),
                json.dumps(stored.get("rejection_reasons") or [], ensure_ascii=False),
                stored.get("latency_ms"), stored["created_at"],
                stored.get("owner_id", LEGACY_USER_ID),
            ),
        )
    return stored


def list_quiz_validation_events(generation_run_id: str | None = None, owner_id: str = LEGACY_USER_ID) -> list[dict]:
    initialize_quiz_store()
    with _connect() as connection:
        if generation_run_id:
            rows = connection.execute(
                "SELECT * FROM quiz_validation_events WHERE owner_id = ? AND generation_run_id = ? ORDER BY created_at",
                (owner_id, generation_run_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM quiz_validation_events WHERE owner_id = ? ORDER BY created_at", (owner_id,)
            ).fetchall()
        return [dict(row) for row in rows]


def list_completed_answer_snapshots(
    student_id: str, document_id: str, topic_id: str
) -> list[dict]:
    initialize_quiz_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            WITH ranked AS (
                SELECT a.attempt_id, t.quiz_id, a.question_id, a.is_correct,
                       a.question_difficulty, a.validation_outcome, a.topic_id,
                       a.topic_name, a.concept_id, a.assessment_capacity,
                       a.evidence_requirement_version, a.concept_plan_id,
                       COALESCE(t.submitted_at, t.completed_at) AS evidence_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.quiz_id, a.question_id
                           ORDER BY COALESCE(t.submitted_at, t.completed_at) DESC, t.attempt_id DESC
                       ) AS response_rank
                FROM quiz_attempt_answers a
                JOIN quiz_attempts t ON t.attempt_id = a.attempt_id
                WHERE t.student_id = ? AND t.document_id = ? AND a.topic_id = ?
                  AND t.completed = 1 AND t.submitted_at IS NOT NULL
            )
            SELECT * FROM ranked WHERE response_rank = 1
            ORDER BY quiz_id, question_id
            """,
            (student_id, document_id, topic_id),
        ).fetchall()
        snapshots = [dict(row) for row in rows]
        planned = [row for row in snapshots if str(row.get("concept_plan_id") or "").strip()]
        if not planned:
            return snapshots
        latest_plan_id = max(planned, key=lambda row: (str(row.get("evidence_at") or ""), str(row.get("attempt_id") or "")))["concept_plan_id"]
        return [row for row in snapshots if row.get("concept_plan_id") == latest_plan_id]


def get_quiz_attempt_summary(quiz_id: str, student_id: str) -> dict:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COALESCE((SELECT percentage FROM quiz_attempts latest
                             WHERE latest.quiz_id = ? AND latest.student_id = ? AND latest.completed = 1
                             ORDER BY COALESCE(latest.completed_at, latest.submitted_at) DESC, latest.attempt_id DESC LIMIT 1), 0) AS latest_score,
                   COALESCE(MAX(percentage), 0) AS best_score,
                   COALESCE(ROUND(AVG(CASE WHEN total > 0 THEN 100.0 * score / total ELSE 0 END), 2), 0) AS average_score
            FROM quiz_attempts
            WHERE quiz_id = ? AND student_id = ? AND completed = 1
            """,
            (quiz_id, student_id, quiz_id, student_id),
        ).fetchone()
        return dict(row)


def save_topic_mastery(result: dict) -> dict:
    initialize_quiz_store()
    stored = {**result, "computed_at": result.get("computed_at") or utc_now_iso()}
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO topic_mastery (
                student_id, document_id, topic_id, mastery_score, mastery_level,
                earned_weight, possible_weight, correct_answers, answered_questions,
                completed_attempts, has_evidence, has_sufficient_evidence,
                minimum_questions_required, formula_version, formula_config_json, computed_at,
                assessment_capacity, distinct_concepts_assessed, concept_coverage_ratio,
                required_concept_coverage, required_concepts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, document_id, topic_id) DO UPDATE SET
                mastery_score=excluded.mastery_score, mastery_level=excluded.mastery_level,
                earned_weight=excluded.earned_weight, possible_weight=excluded.possible_weight,
                correct_answers=excluded.correct_answers, answered_questions=excluded.answered_questions,
                completed_attempts=excluded.completed_attempts, has_evidence=excluded.has_evidence,
                has_sufficient_evidence=excluded.has_sufficient_evidence,
                minimum_questions_required=excluded.minimum_questions_required,
                formula_version=excluded.formula_version,
                formula_config_json=excluded.formula_config_json, computed_at=excluded.computed_at,
                assessment_capacity=excluded.assessment_capacity,
                distinct_concepts_assessed=excluded.distinct_concepts_assessed,
                concept_coverage_ratio=excluded.concept_coverage_ratio,
                required_concept_coverage=excluded.required_concept_coverage,
                required_concepts=excluded.required_concepts
            """,
            (
                stored["student_id"], stored["document_id"], stored["topic_id"],
                stored["mastery_score"], stored["mastery_level"], stored["earned_weight"],
                stored["possible_weight"], stored["correct_answers"], stored["answered_questions"],
                stored["completed_attempts"], int(stored["has_evidence"]),
                int(stored["has_sufficient_evidence"]), stored["minimum_questions_required"],
                stored["formula_version"], json.dumps(stored["formula_config"], ensure_ascii=False),
                stored["computed_at"],
                stored.get("assessment_capacity", 0), stored.get("distinct_concepts_assessed", 0),
                stored.get("concept_coverage_ratio", 0), stored.get("required_concept_coverage", 0),
                stored.get("required_concepts", 0),
            ),
        )
    return stored


def get_topic_mastery(student_id: str, document_id: str, topic_id: str) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM topic_mastery WHERE student_id = ? AND document_id = ? AND topic_id = ?",
            (student_id, document_id, topic_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["has_evidence"] = bool(result["has_evidence"])
        result["has_sufficient_evidence"] = bool(result["has_sufficient_evidence"])
        result["formula_config"] = json.loads(result.pop("formula_config_json"))
        return result


def list_topic_mastery(student_id: str, document_id: str | None = None) -> list[dict]:
    """Return only one student's cached mastery, with names from immutable answer snapshots."""
    initialize_quiz_store()
    clauses = ["m.student_id = ?"]
    parameters: list[str] = [student_id]
    if document_id is not None:
        clauses.append("m.document_id = ?")
        parameters.append(document_id)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT m.*,
                   COALESCE((
                       SELECT NULLIF(a.topic_name, '')
                       FROM quiz_attempt_answers a
                       JOIN quiz_attempts t ON t.attempt_id = a.attempt_id
                       WHERE t.student_id = m.student_id
                         AND t.document_id = m.document_id
                         AND a.topic_id = m.topic_id
                       ORDER BY COALESCE(t.submitted_at, t.completed_at) DESC, a.question_id
                       LIMIT 1
                   ), m.topic_id) AS topic_name
            FROM topic_mastery m
            WHERE {' AND '.join(clauses)}
            """,
            parameters,
        ).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        result["has_evidence"] = bool(result["has_evidence"])
        result["has_sufficient_evidence"] = bool(result["has_sufficient_evidence"])
        result["formula_config"] = json.loads(result.pop("formula_config_json"))
        results.append(result)
    return results


def list_mastery_identities(student_id: str | None = None) -> list[dict]:
    initialize_quiz_store()
    with _connect() as connection:
        if student_id:
            rows = connection.execute(
                "SELECT DISTINCT t.student_id, t.document_id, a.topic_id FROM quiz_attempts t JOIN quiz_attempt_answers a ON a.attempt_id=t.attempt_id WHERE t.completed = 1 AND t.student_id = ?",
                (student_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT DISTINCT t.student_id, t.document_id, a.topic_id FROM quiz_attempts t JOIN quiz_attempt_answers a ON a.attempt_id=t.attempt_id WHERE t.completed = 1"
            ).fetchall()
        return [dict(row) for row in rows]


def list_quiz_history(
    document_id: str | None = None,
    difficulty: str | None = None,
    student_id: str | None = None,
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
    if student_id:
        clauses.append("student_id = ?")
        parameters.append(student_id)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM quiz_attempts WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(completed_at, submitted_at, updated_at) DESC
            """,
            parameters,
        ).fetchall()
        return [_row_to_attempt(connection, row) for row in rows]


def get_quiz_history_attempt(attempt_id: str, student_id: str = LEGACY_USER_ID) -> dict | None:
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM quiz_attempts WHERE attempt_id = ? AND student_id = ? AND completed = 1",
            (attempt_id, student_id),
        ).fetchone()
        return _row_to_attempt(connection, row) if row else None


def get_latest_completed_attempt_for_quiz(quiz_id: str, student_id: str = LEGACY_USER_ID) -> dict | None:
    """Return a persisted answer snapshot usable when a migrated quiz row is absent."""
    initialize_quiz_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM quiz_attempts
            WHERE quiz_id = ? AND student_id = ? AND completed = 1
            ORDER BY COALESCE(completed_at, submitted_at, updated_at) DESC, attempt_id DESC
            LIMIT 1
            """,
            (quiz_id, student_id),
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
                    LEGACY_TOPIC_ID,
                    "local_student",
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
                    connection, document_id, difficulty, LEGACY_TOPIC_ID, "local_student", latest, None, is_latest=True
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
