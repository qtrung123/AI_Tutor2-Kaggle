"""Owner-scoped SQLite persistence for the Study Planner feature (Phase 1 / MVP).

Mirrors the connection/schema-migration conventions already used by flashcard_store.py,
quiz_store.py, and conversation_store.py -- same shared DATABASE_PATH, same
_ClosingConnection pattern, same additive-migration style.
"""

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from backend.auth_store import initialize_auth_store
from config import DATABASE_PATH


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_study_planner_store() -> None:
    initialize_auth_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_tasks (
                task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                document_id TEXT, deadline TEXT NOT NULL,
                estimated_minutes INTEGER NOT NULL, remaining_minutes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_study_tasks_owner ON study_tasks(owner_id, deadline);

            CREATE TABLE IF NOT EXISTS availability_slots (
                availability_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                date TEXT, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                is_recurring INTEGER NOT NULL DEFAULT 0, day_of_week INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_availability_owner_date ON availability_slots(owner_id, date);
            CREATE INDEX IF NOT EXISTS idx_availability_owner_recurring
                ON availability_slots(owner_id, is_recurring, day_of_week);

            CREATE TABLE IF NOT EXISTS study_blocks (
                block_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT NOT NULL,
                document_id TEXT, topic_id TEXT, title TEXT NOT NULL,
                start_at TEXT NOT NULL, end_at TEXT NOT NULL, planned_minutes INTEGER NOT NULL,
                actual_minutes INTEGER, status TEXT NOT NULL DEFAULT 'suggested',
                locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id),
                FOREIGN KEY (task_id) REFERENCES study_tasks(task_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_study_blocks_owner_start ON study_blocks(owner_id, start_at);
            CREATE INDEX IF NOT EXISTS idx_study_blocks_task ON study_blocks(task_id);

            CREATE TABLE IF NOT EXISTS schedule_runs (
                schedule_run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id),
                FOREIGN KEY (task_id) REFERENCES study_tasks(task_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_runs_task ON schedule_runs(task_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS study_plan_items (
                item_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                document_id TEXT NOT NULL, topic_id TEXT NOT NULL, title TEXT NOT NULL,
                estimated_minutes INTEGER NOT NULL, remaining_minutes INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id),
                FOREIGN KEY (task_id) REFERENCES study_tasks(task_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_study_plan_items_task
                ON study_plan_items(task_id, priority, created_at);

            CREATE TABLE IF NOT EXISTS topic_progress (
                owner_id TEXT NOT NULL, document_id TEXT NOT NULL, topic_id TEXT NOT NULL,
                planned_minutes INTEGER NOT NULL DEFAULT 0, completed_minutes INTEGER NOT NULL DEFAULT 0,
                progress_percent REAL NOT NULL DEFAULT 0, last_studied_at TEXT,
                PRIMARY KEY (owner_id, document_id, topic_id),
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            """
        )
        # Additive migration: completion tracking on study_blocks (Phase 3).
        existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(study_blocks)")}
        if "completion_status" not in existing_columns:
            connection.execute(
                "ALTER TABLE study_blocks ADD COLUMN completion_status TEXT NOT NULL DEFAULT 'scheduled'"
            )
        if "completed_at" not in existing_columns:
            connection.execute("ALTER TABLE study_blocks ADD COLUMN completed_at TEXT")


# ---------------------------------------------------------------------------
# Study tasks
# ---------------------------------------------------------------------------

def _task(row: sqlite3.Row) -> dict:
    return {
        "task_id": row["task_id"], "owner_id": row["owner_id"], "title": row["title"],
        "document_id": row["document_id"], "deadline": row["deadline"],
        "estimated_minutes": row["estimated_minutes"], "remaining_minutes": row["remaining_minutes"],
        "status": row["status"], "created_at": row["created_at"],
    }


def create_task(owner_id: str, title: str, deadline: str, estimated_minutes: int,
                document_id: str | None = None) -> dict:
    initialize_study_planner_store()
    task_id, created_at = str(uuid4()), utc_now_iso()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO study_tasks (task_id, owner_id, title, document_id, deadline,
               estimated_minutes, remaining_minutes, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (task_id, owner_id, title, document_id, deadline, estimated_minutes,
             estimated_minutes, created_at),
        )
    return get_task(owner_id, task_id)


def list_tasks(owner_id: str) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM study_tasks WHERE owner_id=? ORDER BY deadline, created_at", (owner_id,),
        ).fetchall()
    return [_task(row) for row in rows]


def get_task(owner_id: str, task_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id),
        ).fetchone()
    return _task(row) if row else None


def update_task(owner_id: str, task_id: str, changes: dict) -> dict:
    initialize_study_planner_store()
    allowed = {"title", "document_id", "deadline", "estimated_minutes", "remaining_minutes", "status"}
    fields = [(key, changes[key]) for key in allowed if key in changes]
    if not fields:
        raise ValueError("No task changes supplied.")
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE study_tasks SET {', '.join(f'{key}=?' for key, _ in fields)} WHERE task_id=? AND owner_id=?",
            (*[value for _, value in fields], task_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Study task not found.")
    return get_task(owner_id, task_id)


def delete_task(owner_id: str, task_id: str) -> None:
    initialize_study_planner_store()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM study_tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Study task not found.")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _availability(row: sqlite3.Row) -> dict:
    return {
        "availability_id": row["availability_id"], "owner_id": row["owner_id"],
        "date": row["date"], "start_at": row["start_at"], "end_at": row["end_at"],
        "is_recurring": bool(row["is_recurring"]), "day_of_week": row["day_of_week"],
        "created_at": row["created_at"],
    }


def list_availability(owner_id: str) -> list[dict]:
    """All persisted availability for one owner -- both dated and recurring slots."""
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM availability_slots WHERE owner_id=? ORDER BY is_recurring, date, day_of_week, start_at",
            (owner_id,),
        ).fetchall()
    return [_availability(row) for row in rows]


def _replace_group(connection: sqlite3.Connection, owner_id: str, date: str | None,
                    day_of_week: int | None, is_recurring: bool, intervals: list[tuple[str, str]]) -> None:
    """Replace every row in one (owner, date) or (owner, recurring day_of_week) group with the
    given minimal, non-overlapping set of (start_at, end_at) HH:MM intervals."""
    if is_recurring:
        connection.execute(
            "DELETE FROM availability_slots WHERE owner_id=? AND is_recurring=1 AND day_of_week=?",
            (owner_id, day_of_week),
        )
    else:
        connection.execute(
            "DELETE FROM availability_slots WHERE owner_id=? AND is_recurring=0 AND date=?",
            (owner_id, date),
        )
    created_at = utc_now_iso()
    for start_at, end_at in intervals:
        connection.execute(
            """INSERT INTO availability_slots (availability_id, owner_id, date, start_at, end_at,
               is_recurring, day_of_week, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), owner_id, None if is_recurring else date, start_at, end_at,
             int(is_recurring), day_of_week if is_recurring else None, created_at),
        )


def add_availability(owner_id: str, start_at: str, end_at: str, date: str | None = None,
                      is_recurring: bool = False, day_of_week: int | None = None) -> list[dict]:
    """Mark [start_at, end_at) available for one date (or one recurring weekday), merging with
    whatever is already persisted for that same group so adjacent/overlapping ranges never
    duplicate (see _merge_minute_intervals in study_planner_service)."""
    from backend.study_planner_service import merge_minute_intervals, to_hhmm, to_minutes

    initialize_study_planner_store()
    if is_recurring and day_of_week is None:
        raise ValueError("day_of_week is required for recurring availability.")
    if not is_recurring and not date:
        raise ValueError("date is required for non-recurring availability.")
    new_start, new_end = to_minutes(start_at), to_minutes(end_at)
    if new_end <= new_start:
        raise ValueError("end_at must be after start_at.")
    with _connect() as connection:
        existing = connection.execute(
            "SELECT start_at, end_at FROM availability_slots WHERE owner_id=? AND is_recurring=? "
            + ("AND day_of_week=?" if is_recurring else "AND date=?"),
            (owner_id, int(is_recurring), day_of_week if is_recurring else date),
        ).fetchall()
        intervals = [(to_minutes(row["start_at"]), to_minutes(row["end_at"])) for row in existing]
        intervals.append((new_start, new_end))
        merged = merge_minute_intervals(intervals)
        _replace_group(
            connection, owner_id, date, day_of_week, is_recurring,
            [(to_hhmm(start), to_hhmm(end)) for start, end in merged],
        )
    return list_availability(owner_id)


def remove_availability(owner_id: str, start_at: str, end_at: str, date: str | None = None,
                         is_recurring: bool = False, day_of_week: int | None = None) -> list[dict]:
    """Subtract [start_at, end_at) from whatever is persisted for that date/recurring weekday --
    "erase mode": splits, shrinks, or fully removes existing slots as needed."""
    from backend.study_planner_service import subtract_minute_interval, to_hhmm, to_minutes

    initialize_study_planner_store()
    if is_recurring and day_of_week is None:
        raise ValueError("day_of_week is required for recurring availability.")
    if not is_recurring and not date:
        raise ValueError("date is required for non-recurring availability.")
    remove_start, remove_end = to_minutes(start_at), to_minutes(end_at)
    if remove_end <= remove_start:
        raise ValueError("end_at must be after start_at.")
    with _connect() as connection:
        existing = connection.execute(
            "SELECT start_at, end_at FROM availability_slots WHERE owner_id=? AND is_recurring=? "
            + ("AND day_of_week=?" if is_recurring else "AND date=?"),
            (owner_id, int(is_recurring), day_of_week if is_recurring else date),
        ).fetchall()
        intervals = [(to_minutes(row["start_at"]), to_minutes(row["end_at"])) for row in existing]
        remaining = subtract_minute_interval(intervals, remove_start, remove_end)
        _replace_group(
            connection, owner_id, date, day_of_week, is_recurring,
            [(to_hhmm(start), to_hhmm(end)) for start, end in remaining],
        )
    return list_availability(owner_id)


# ---------------------------------------------------------------------------
# Study blocks
# ---------------------------------------------------------------------------

def _block(row: sqlite3.Row) -> dict:
    return {
        "block_id": row["block_id"], "owner_id": row["owner_id"], "task_id": row["task_id"],
        "document_id": row["document_id"], "topic_id": row["topic_id"], "title": row["title"],
        "start_at": row["start_at"], "end_at": row["end_at"], "planned_minutes": row["planned_minutes"],
        "actual_minutes": row["actual_minutes"], "status": row["status"],
        "locked": bool(row["locked"]), "created_at": row["created_at"],
        "completion_status": row["completion_status"], "completed_at": row["completed_at"],
    }


def create_blocks(owner_id: str, task_id: str, blocks: list[dict]) -> list[dict]:
    """Persist a batch of freshly generated suggested blocks for one task."""
    initialize_study_planner_store()
    created_at = utc_now_iso()
    block_ids = []
    with _connect() as connection:
        for block in blocks:
            block_id = str(uuid4())
            block_ids.append(block_id)
            connection.execute(
                """INSERT INTO study_blocks (block_id, owner_id, task_id, document_id, topic_id,
                   title, start_at, end_at, planned_minutes, actual_minutes, status, locked, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'suggested', 0, ?)""",
                (block_id, owner_id, task_id, block.get("document_id"), block.get("topic_id"),
                 block["title"], block["start_at"], block["end_at"], block["planned_minutes"], created_at),
            )
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM study_blocks WHERE block_id IN ({','.join('?' * len(block_ids))})",
            block_ids,
        ).fetchall()
    by_id = {row["block_id"]: _block(row) for row in rows}
    return [by_id[block_id] for block_id in block_ids]


def list_blocks(owner_id: str, task_id: str | None = None) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        if task_id:
            rows = connection.execute(
                "SELECT * FROM study_blocks WHERE owner_id=? AND task_id=? ORDER BY start_at",
                (owner_id, task_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM study_blocks WHERE owner_id=? ORDER BY start_at", (owner_id,),
            ).fetchall()
    return [_block(row) for row in rows]


def get_block(owner_id: str, block_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_blocks WHERE block_id=? AND owner_id=?", (block_id, owner_id),
        ).fetchone()
    return _block(row) if row else None


def delete_unlocked_suggested_blocks_for_task(owner_id: str, task_id: str) -> None:
    """Clear out a task's stale suggested blocks before writing a fresh plan (used by both the
    initial generate and a later regenerate). Never touches a confirmed/completed/missed block,
    never touches a locked block (the user pinned it with a manual move/resize), and never
    touches a block whose completion_status is 'completed' even in the edge case where it is
    still technically status='suggested' and unlocked."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM study_blocks WHERE owner_id=? AND task_id=? AND status='suggested' "
            "AND locked=0 AND completion_status != 'completed'",
            (owner_id, task_id),
        )


def update_block(owner_id: str, block_id: str, changes: dict) -> dict:
    initialize_study_planner_store()
    allowed = {"start_at", "end_at", "planned_minutes", "actual_minutes", "status", "locked", "title",
               "completion_status", "completed_at"}
    fields = [(key, changes[key]) for key in allowed if key in changes]
    if not fields:
        raise ValueError("No block changes supplied.")
    values = [int(value) if key == "locked" else value for key, value in fields]
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE study_blocks SET {', '.join(f'{key}=?' for key, _ in fields)} WHERE block_id=? AND owner_id=?",
            (*values, block_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Study block not found.")
    return get_block(owner_id, block_id)


def accept_blocks_for_task(owner_id: str, task_id: str) -> list[dict]:
    """Confirm every currently-suggested block for this task. Never touches an already
    confirmed/completed/missed block."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute(
            "UPDATE study_blocks SET status='confirmed' WHERE owner_id=? AND task_id=? AND status='suggested'",
            (owner_id, task_id),
        )
    return list_blocks(owner_id, task_id)


def delete_block(owner_id: str, block_id: str) -> None:
    initialize_study_planner_store()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM study_blocks WHERE block_id=? AND owner_id=?", (block_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Study block not found.")


# ---------------------------------------------------------------------------
# Schedule runs
# ---------------------------------------------------------------------------

def record_schedule_run(owner_id: str, task_id: str, reason: str = "initial") -> dict:
    initialize_study_planner_store()
    schedule_run_id, created_at = str(uuid4()), utc_now_iso()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO schedule_runs (schedule_run_id, owner_id, task_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (schedule_run_id, owner_id, task_id, reason, created_at),
        )
    return {"schedule_run_id": schedule_run_id, "owner_id": owner_id, "task_id": task_id,
            "reason": reason, "created_at": created_at}


# ---------------------------------------------------------------------------
# Study plan items (Phase 2: document-aware planning -- one per document topic)
# ---------------------------------------------------------------------------

def _plan_item(row: sqlite3.Row) -> dict:
    return {
        "item_id": row["item_id"], "task_id": row["task_id"], "owner_id": row["owner_id"],
        "document_id": row["document_id"], "topic_id": row["topic_id"], "title": row["title"],
        "estimated_minutes": row["estimated_minutes"], "remaining_minutes": row["remaining_minutes"],
        "priority": row["priority"], "status": row["status"], "created_at": row["created_at"],
    }


def create_plan_items(owner_id: str, task_id: str, items: list[dict]) -> list[dict]:
    """Persist a freshly computed set of study plan items (one per document topic) for a task."""
    initialize_study_planner_store()
    created_at = utc_now_iso()
    item_ids = []
    with _connect() as connection:
        for item in items:
            item_id = str(uuid4())
            item_ids.append(item_id)
            connection.execute(
                """INSERT INTO study_plan_items (item_id, task_id, owner_id, document_id, topic_id,
                   title, estimated_minutes, remaining_minutes, priority, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (item_id, task_id, owner_id, item["document_id"], item["topic_id"], item["title"],
                 item["estimated_minutes"], item["remaining_minutes"], int(item.get("priority", 0)), created_at),
            )
    return list_plan_items(owner_id, task_id)


def list_plan_items(owner_id: str, task_id: str) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM study_plan_items WHERE owner_id=? AND task_id=? ORDER BY priority, created_at",
            (owner_id, task_id),
        ).fetchall()
    return [_plan_item(row) for row in rows]


def delete_plan_items_for_task(owner_id: str, task_id: str) -> None:
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM study_plan_items WHERE owner_id=? AND task_id=?", (owner_id, task_id),
        )


def sum_estimated_minutes_for_topic(owner_id: str, document_id: str, topic_id: str) -> int:
    """Total planned minutes for one topic across every study plan item ever created for it,
    regardless of which task the item belongs to."""
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            """SELECT COALESCE(SUM(estimated_minutes), 0) AS total FROM study_plan_items
               WHERE owner_id=? AND document_id=? AND topic_id=?""",
            (owner_id, document_id, topic_id),
        ).fetchone()
    return int(row["total"])


# ---------------------------------------------------------------------------
# Topic progress (Phase 3: completion tracking)
# ---------------------------------------------------------------------------

def _topic_progress(row: sqlite3.Row) -> dict:
    return {
        "owner_id": row["owner_id"], "document_id": row["document_id"], "topic_id": row["topic_id"],
        "planned_minutes": row["planned_minutes"], "completed_minutes": row["completed_minutes"],
        "progress_percent": row["progress_percent"], "last_studied_at": row["last_studied_at"],
    }


def get_topic_progress(owner_id: str, document_id: str, topic_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM topic_progress WHERE owner_id=? AND document_id=? AND topic_id=?",
            (owner_id, document_id, topic_id),
        ).fetchone()
    return _topic_progress(row) if row else None


def list_topic_progress(owner_id: str, document_id: str | None = None) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        if document_id:
            rows = connection.execute(
                "SELECT * FROM topic_progress WHERE owner_id=? AND document_id=?",
                (owner_id, document_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM topic_progress WHERE owner_id=?", (owner_id,),
            ).fetchall()
    return [_topic_progress(row) for row in rows]


def upsert_topic_progress(owner_id: str, document_id: str, topic_id: str, planned_minutes: int,
                           completed_minutes_delta: int, last_studied_at: str) -> dict:
    """Add completed_minutes_delta to this topic's running total (creating the row on first use),
    refresh planned_minutes to the latest known total, and recompute progress_percent. Both
    completed_minutes and progress_percent are capped at planned_minutes / 100% -- a block whose
    actual_minutes overshoots its planned allocation can never make a topic look more than fully
    studied."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO topic_progress
                   (owner_id, document_id, topic_id, planned_minutes, completed_minutes,
                    progress_percent, last_studied_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(owner_id, document_id, topic_id) DO UPDATE SET
                   planned_minutes=excluded.planned_minutes,
                   completed_minutes=topic_progress.completed_minutes + excluded.completed_minutes,
                   last_studied_at=excluded.last_studied_at""",
            (owner_id, document_id, topic_id, planned_minutes, max(completed_minutes_delta, 0),
             last_studied_at),
        )
        completed_minutes = connection.execute(
            "SELECT completed_minutes FROM topic_progress WHERE owner_id=? AND document_id=? AND topic_id=?",
            (owner_id, document_id, topic_id),
        ).fetchone()["completed_minutes"]
        capped_completed_minutes = min(completed_minutes, planned_minutes) if planned_minutes > 0 else 0
        progress_percent = (
            min(100.0, round(capped_completed_minutes / planned_minutes * 100, 2)) if planned_minutes > 0 else 0.0
        )
        connection.execute(
            """UPDATE topic_progress SET completed_minutes=?, progress_percent=?
               WHERE owner_id=? AND document_id=? AND topic_id=?""",
            (capped_completed_minutes, progress_percent, owner_id, document_id, topic_id),
        )
    return get_topic_progress(owner_id, document_id, topic_id)


# ---------------------------------------------------------------------------
# Development reset -- current user's planner data only
# ---------------------------------------------------------------------------

def reset_planner_data(owner_id: str) -> None:
    """Delete only this owner's Study Planner data (tasks, availability, blocks, schedule runs,
    plan items, topic progress). Never touches any other feature's tables -- documents, quiz,
    flashcards, and chat history all live in separate stores this function never opens."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute("DELETE FROM study_blocks WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM schedule_runs WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_plan_items WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_tasks WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM availability_slots WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM topic_progress WHERE owner_id=?", (owner_id,))
