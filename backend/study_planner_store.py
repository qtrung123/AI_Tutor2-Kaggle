"""Owner-scoped SQLite persistence for the Study Planner feature (Phase 1 / MVP).

Mirrors the connection/schema-migration conventions already used by flashcard_store.py,
quiz_store.py, and conversation_store.py -- same shared DATABASE_PATH, same
_ClosingConnection pattern, same additive-migration style.
"""

import sqlite3
from datetime import date as date_type, datetime, timezone
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


def _add_column_if_missing(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Additive-migration helper: ALTER TABLE ... ADD COLUMN, tolerant of two requests racing to
    apply the same migration on a fresh database (each of loadPlannerData's parallel GETs calls
    initialize_study_planner_store independently). The PRAGMA table_info check above already
    avoids this in the common case, but does not make the check-then-ALTER atomic, so a
    'duplicate column' error from a concurrent winner is swallowed here rather than surfacing as
    a 500 to the user."""
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as error:
        if "duplicate column name" not in str(error):
            raise


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
            _add_column_if_missing(
                connection, "study_blocks", "completion_status", "TEXT NOT NULL DEFAULT 'scheduled'"
            )
        if "completed_at" not in existing_columns:
            _add_column_if_missing(connection, "study_blocks", "completed_at", "TEXT")
        # Additive migration: learning context on study_blocks (AI Study Planning Agent).
        if "objective" not in existing_columns:
            _add_column_if_missing(connection, "study_blocks", "objective", "TEXT")
        if "study_goal" not in existing_columns:
            _add_column_if_missing(connection, "study_blocks", "study_goal", "TEXT")
        # Additive migration: optional topic-level linking on study_tasks (in addition to
        # document-level linking) so a task can scope its generated plan to a single topic.
        existing_task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(study_tasks)")}
        if "topic_id" not in existing_task_columns:
            _add_column_if_missing(connection, "study_tasks", "topic_id", "TEXT")

        # Document-centric planner (Study Planner v2). The legacy task/topic tables above stay
        # untouched but are no longer used by new code. No topic_id anywhere below: a plan is a
        # set of documents (study packs), and sessions are scheduled per document + activity.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_plans (
                plan_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_study_plans_owner ON study_plans(owner_id, created_at);

            CREATE TABLE IF NOT EXISTS study_plan_materials (
                material_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                document_id TEXT NOT NULL, deadline TEXT, familiarity TEXT,
                learning_state TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (plan_id, document_id),
                FOREIGN KEY (owner_id) REFERENCES users(id),
                FOREIGN KEY (plan_id) REFERENCES study_plans(plan_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_study_plan_materials_owner_document
                ON study_plan_materials(owner_id, document_id);

            CREATE TABLE IF NOT EXISTS study_sessions (
                session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                document_id TEXT NOT NULL, activity_type TEXT NOT NULL, artifact_id TEXT,
                scheduled_start TEXT NOT NULL, scheduled_end TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled',
                reason TEXT, priority_snapshot REAL,
                started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id),
                FOREIGN KEY (plan_id) REFERENCES study_plans(plan_id) ON DELETE CASCADE,
                FOREIGN KEY (plan_id, document_id)
                    REFERENCES study_plan_materials(plan_id, document_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_study_sessions_owner_start ON study_sessions(owner_id, scheduled_start);
            CREATE INDEX IF NOT EXISTS idx_study_sessions_plan ON study_sessions(plan_id, document_id, scheduled_start);
            """
        )
        # Additive migration: a rescheduled session points at the session it replaces.
        _add_column_if_missing(connection, "study_sessions", "rescheduled_from", "TEXT")
        _migrate_schedule_runs_for_plans(connection)


def _migrate_schedule_runs_for_plans(connection: sqlite3.Connection) -> None:
    """Let schedule_runs log runs for a study plan as well as a legacy task: rebuild it once with
    a nullable task_id plus a plan_id (SQLite cannot drop NOT NULL in place). Existing rows are
    copied verbatim. BEGIN IMMEDIATE + a re-check inside the transaction makes two requests racing
    on a fresh database safe -- the loser sees the already-rebuilt table and does nothing."""
    def needs_rebuild() -> bool:
        return "plan_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(schedule_runs)")}

    if not needs_rebuild():
        return
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if needs_rebuild():
            connection.execute(
                """CREATE TABLE schedule_runs_v2 (
                       schedule_run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT,
                       plan_id TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL,
                       CHECK (task_id IS NOT NULL OR plan_id IS NOT NULL),
                       FOREIGN KEY (owner_id) REFERENCES users(id),
                       FOREIGN KEY (task_id) REFERENCES study_tasks(task_id) ON DELETE CASCADE,
                       FOREIGN KEY (plan_id) REFERENCES study_plans(plan_id) ON DELETE CASCADE
                   )"""
            )
            connection.execute(
                """INSERT INTO schedule_runs_v2 (schedule_run_id, owner_id, task_id, plan_id, reason, created_at)
                   SELECT schedule_run_id, owner_id, task_id, NULL, reason, created_at FROM schedule_runs"""
            )
            connection.execute("DROP TABLE schedule_runs")
            connection.execute("ALTER TABLE schedule_runs_v2 RENAME TO schedule_runs")
            connection.execute("CREATE INDEX idx_schedule_runs_task ON schedule_runs(task_id, created_at DESC)")
            connection.execute("CREATE INDEX idx_schedule_runs_plan ON schedule_runs(plan_id, created_at DESC)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


# ---------------------------------------------------------------------------
# Study tasks
# ---------------------------------------------------------------------------

def _task(row: sqlite3.Row) -> dict:
    return {
        "task_id": row["task_id"], "owner_id": row["owner_id"], "title": row["title"],
        "document_id": row["document_id"], "topic_id": row["topic_id"], "deadline": row["deadline"],
        "estimated_minutes": row["estimated_minutes"], "remaining_minutes": row["remaining_minutes"],
        "status": row["status"], "created_at": row["created_at"],
    }


def create_task(owner_id: str, title: str, deadline: str, estimated_minutes: int,
                document_id: str | None = None, topic_id: str | None = None) -> dict:
    initialize_study_planner_store()
    task_id, created_at = str(uuid4()), utc_now_iso()
    with _connect() as connection:
        connection.execute(
            """INSERT INTO study_tasks (task_id, owner_id, title, document_id, topic_id, deadline,
               estimated_minutes, remaining_minutes, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (task_id, owner_id, title, document_id, topic_id, deadline, estimated_minutes,
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
    allowed = {"title", "document_id", "topic_id", "deadline", "estimated_minutes", "remaining_minutes", "status"}
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
        "objective": row["objective"], "study_goal": row["study_goal"],
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
                   title, start_at, end_at, planned_minutes, actual_minutes, status, locked, created_at,
                   objective, study_goal)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'suggested', 0, ?, ?, ?)""",
                (block_id, owner_id, task_id, block.get("document_id"), block.get("topic_id"),
                 block["title"], block["start_at"], block["end_at"], block["planned_minutes"], created_at,
                 block.get("objective"), block.get("study_goal")),
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


def decrement_plan_item_remaining_minutes(owner_id: str, task_id: str, document_id: str, topic_id: str,
                                           minutes: int) -> dict | None:
    """Roll a completed block's actual_minutes off its matching study_plan_item's
    remaining_minutes, floored at 0 -- mirrors _decrement_task_remaining_minutes at the task
    level, but keeps the per-topic figure that ensure_study_plan_items/regeneration reads in
    sync too, so a regenerate after completion never re-schedules minutes already done. Returns
    the updated item, or None when no plan item matches (e.g. a plain task-level block)."""
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            """SELECT * FROM study_plan_items
               WHERE owner_id=? AND task_id=? AND document_id=? AND topic_id=?""",
            (owner_id, task_id, document_id, topic_id),
        ).fetchone()
        if not row:
            return None
        new_remaining = max(0, int(row["remaining_minutes"]) - int(minutes))
        connection.execute(
            "UPDATE study_plan_items SET remaining_minutes=? WHERE item_id=?",
            (new_remaining, row["item_id"]),
        )
        updated = connection.execute(
            "SELECT * FROM study_plan_items WHERE item_id=?", (row["item_id"],),
        ).fetchone()
    return _plan_item(updated)


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
# Document-centric planner: study plans, plan materials, study sessions
# ---------------------------------------------------------------------------

PLAN_STATUSES = ("active", "paused", "completed", "archived")
FAMILIARITY_LEVELS = ("new_to_me", "somewhat_familiar", "reviewing")
LEARNING_STATES = ("new", "learning", "needs_review", "on_track", "completed")
ACTIVITY_TYPES = ("summary", "flashcards", "quiz", "review", "quiz_retry")
# skipped = the learner chose to skip it; missed = its time passed; rescheduled = moved, with a
# replacement row pointing back via rescheduled_from; cancelled = the adaptive planner withdrew a
# future recommendation that is no longer needed/valid. All four are terminal history: never
# deleted, never busy time, never active work.
SESSION_STATUSES = ("scheduled", "in_progress", "completed", "skipped", "missed", "rescheduled", "cancelled")
# Why a session was scheduled -- stable codes persisted in study_sessions.reason (the UI renders and
# translates them). Scheduling semantics only; learning/performance state lives in DocumentStudyState.
SESSION_REASONS = (
    "new_material", "deadline_approaching", "review_due", "low_quiz_score", "flashcard_review_due",
    "final_review", "quiz_in_progress", "rescheduled",
)


def _require_choice(value, allowed: tuple[str, ...], field: str, optional: bool = False) -> None:
    if value is None and optional:
        return
    if value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}.")


def _require_iso_date(value: str | None, field: str) -> None:
    if value is None:
        return
    try:
        date_type.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD).") from error


def _session_span_minutes(scheduled_start: str, scheduled_end: str) -> int:
    """Validate a session's naive-local ISO datetimes (the planner's wall-clock convention, see
    study_planner_service.compute_schedule) and return the span in minutes."""
    try:
        start, end = datetime.fromisoformat(scheduled_start), datetime.fromisoformat(scheduled_end)
    except (TypeError, ValueError) as error:
        raise ValueError("scheduled_start/scheduled_end must be ISO datetimes.") from error
    if end <= start:
        raise ValueError("scheduled_end must be after scheduled_start.")
    return int((end - start).total_seconds() // 60)


def validate_session(session: dict) -> None:
    _require_choice(session.get("activity_type"), ACTIVITY_TYPES, "activity_type")
    _require_choice(session.get("status"), SESSION_STATUSES, "status")
    _require_choice(session.get("reason"), SESSION_REASONS, "reason", optional=True)
    span = _session_span_minutes(session["scheduled_start"], session["scheduled_end"])
    duration = session.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 < duration <= span:
        raise ValueError("duration_minutes must be a positive whole number that fits the scheduled window.")


def _plan(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in ("plan_id", "owner_id", "title", "status", "created_at", "updated_at")}


def _material(row: sqlite3.Row) -> dict:
    return {
        key: row[key]
        for key in ("material_id", "plan_id", "owner_id", "document_id", "deadline", "familiarity",
                    "learning_state", "created_at", "updated_at")
    }


def _session(row: sqlite3.Row) -> dict:
    return {
        key: row[key]
        for key in ("session_id", "owner_id", "plan_id", "document_id", "activity_type", "artifact_id",
                    "scheduled_start", "scheduled_end", "duration_minutes", "status", "reason",
                    "priority_snapshot", "started_at", "completed_at", "rescheduled_from", "created_at",
                    "updated_at")
    }


def _update_row(table: str, key_column: str, key: str, owner_id: str, fields: dict, not_found: str) -> None:
    fields = {**fields, "updated_at": utc_now_iso()}
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE {table} SET {', '.join(f'{column}=?' for column in fields)} WHERE {key_column}=? AND owner_id=?",
            (*fields.values(), key, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError(not_found)


# -- Study plans -------------------------------------------------------------

def create_plan(owner_id: str, title: str, status: str = "active") -> dict:
    initialize_study_planner_store()
    title = str(title or "").strip()
    if not title:
        raise ValueError("Plan title is required.")
    _require_choice(status, PLAN_STATUSES, "status")
    plan_id, now = str(uuid4()), utc_now_iso()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO study_plans (plan_id, owner_id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, owner_id, title, status, now, now),
        )
    return get_plan(owner_id, plan_id)


def get_plan(owner_id: str, plan_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_plans WHERE plan_id=? AND owner_id=?", (plan_id, owner_id),
        ).fetchone()
    return _plan(row) if row else None


def list_plans(owner_id: str) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM study_plans WHERE owner_id=? ORDER BY created_at, plan_id", (owner_id,),
        ).fetchall()
    return [_plan(row) for row in rows]


def update_plan(owner_id: str, plan_id: str, changes: dict) -> dict:
    initialize_study_planner_store()
    fields = {key: changes[key] for key in ("title", "status") if key in changes}
    if not fields:
        raise ValueError("No plan changes supplied.")
    if "title" in fields:
        fields["title"] = str(fields["title"] or "").strip()
        if not fields["title"]:
            raise ValueError("Plan title is required.")
    if "status" in fields:
        _require_choice(fields["status"], PLAN_STATUSES, "status")
    _update_row("study_plans", "plan_id", plan_id, owner_id, fields, "Study plan not found.")
    return get_plan(owner_id, plan_id)


def delete_plan(owner_id: str, plan_id: str) -> None:
    """Deletes the plan and (via ON DELETE CASCADE) its materials, sessions, and schedule runs."""
    initialize_study_planner_store()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM study_plans WHERE plan_id=? AND owner_id=?", (plan_id, owner_id))
        if not cursor.rowcount:
            raise ValueError("Study plan not found.")


# -- Plan materials (one row per document in a plan) -------------------------

def add_material(owner_id: str, plan_id: str, document_id: str, deadline: str | None = None,
                 familiarity: str | None = None, learning_state: str = "new") -> dict:
    """Add one document to a plan. Does not check that the document exists -- callers go through
    study_planner_service.add_plan_material, which validates document ownership."""
    initialize_study_planner_store()
    if not get_plan(owner_id, plan_id):
        raise ValueError("Study plan not found.")
    if not document_id:
        raise ValueError("document_id is required.")
    _require_iso_date(deadline, "deadline")
    _require_choice(familiarity, FAMILIARITY_LEVELS, "familiarity", optional=True)
    _require_choice(learning_state, LEARNING_STATES, "learning_state")
    material_id, now = str(uuid4()), utc_now_iso()
    try:
        with _connect() as connection:
            connection.execute(
                """INSERT INTO study_plan_materials (material_id, plan_id, owner_id, document_id, deadline,
                   familiarity, learning_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (material_id, plan_id, owner_id, document_id, deadline, familiarity, learning_state, now, now),
            )
    except sqlite3.IntegrityError as error:
        raise ValueError("This document is already part of the study plan.") from error
    return get_material(owner_id, material_id)


def get_material(owner_id: str, material_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_plan_materials WHERE material_id=? AND owner_id=?", (material_id, owner_id),
        ).fetchone()
    return _material(row) if row else None


def get_plan_material(owner_id: str, plan_id: str, document_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_plan_materials WHERE plan_id=? AND document_id=? AND owner_id=?",
            (plan_id, document_id, owner_id),
        ).fetchone()
    return _material(row) if row else None


def list_materials(owner_id: str, plan_id: str) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM study_plan_materials WHERE owner_id=? AND plan_id=? ORDER BY created_at, material_id",
            (owner_id, plan_id),
        ).fetchall()
    return [_material(row) for row in rows]


def update_material(owner_id: str, material_id: str, changes: dict) -> dict:
    """`deadline` and `familiarity` may be set to None to clear them."""
    initialize_study_planner_store()
    fields = {key: changes[key] for key in ("deadline", "familiarity", "learning_state") if key in changes}
    if not fields:
        raise ValueError("No material changes supplied.")
    if "deadline" in fields:
        _require_iso_date(fields["deadline"], "deadline")
    if "familiarity" in fields:
        _require_choice(fields["familiarity"], FAMILIARITY_LEVELS, "familiarity", optional=True)
    if "learning_state" in fields:
        _require_choice(fields["learning_state"], LEARNING_STATES, "learning_state")
    _update_row("study_plan_materials", "material_id", material_id, owner_id, fields, "Plan material not found.")
    return get_material(owner_id, material_id)


def remove_material(owner_id: str, material_id: str) -> None:
    """Removes the document from its plan, and (via ON DELETE CASCADE) its sessions in that plan."""
    initialize_study_planner_store()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM study_plan_materials WHERE material_id=? AND owner_id=?", (material_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Plan material not found.")


def delete_document_plan_data(owner_id: str, document_id: str) -> None:
    """Called when a document is deleted: drops it from every plan of this owner (sessions cascade)."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM study_plan_materials WHERE owner_id=? AND document_id=?", (owner_id, document_id),
        )


# -- Study sessions ----------------------------------------------------------

ACTIVE_SESSION_STATUSES = ("scheduled", "in_progress")


class PlanAlreadyConfirmedError(ValueError):
    """The plan already has active (scheduled/in_progress) sessions."""


def _session_rows(owner_id: str, plan_id: str, sessions: list[dict]) -> list[tuple]:
    """Validate every session (activity/status/reason/window, and that its document is a material
    of this plan) and build the insert rows -- before anything is written."""
    if not get_plan(owner_id, plan_id):
        raise ValueError("Study plan not found.")
    material_documents = {material["document_id"] for material in list_materials(owner_id, plan_id)}
    now = utc_now_iso()
    rows = []
    for session in sessions:
        session = {"status": "scheduled", **session}
        validate_session(session)
        if session.get("document_id") not in material_documents:
            raise ValueError("Session document is not part of this study plan.")
        rows.append((
            str(uuid4()), owner_id, plan_id, session["document_id"], session["activity_type"],
            session.get("artifact_id"), session["scheduled_start"], session["scheduled_end"],
            session["duration_minutes"], session["status"], session.get("reason"),
            session.get("priority_snapshot"), now, now,
        ))
    return rows


_INSERT_SESSION_SQL = """INSERT INTO study_sessions (session_id, owner_id, plan_id, document_id, activity_type,
    artifact_id, scheduled_start, scheduled_end, duration_minutes, status, reason, priority_snapshot,
    created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def create_sessions(owner_id: str, plan_id: str, sessions: list[dict]) -> list[dict]:
    """Persist a batch of sessions for one plan atomically (all or nothing). Each session's
    document must already be a material of this plan. Returned in input order."""
    initialize_study_planner_store()
    rows = _session_rows(owner_id, plan_id, sessions)
    with _connect() as connection:
        connection.executemany(_INSERT_SESSION_SQL, rows)
    return [get_session(owner_id, row[0]) for row in rows]


def list_busy_sessions(owner_id: str, start_from: str | None = None) -> list[dict]:
    """The owner's sessions that occupy time for scheduling, across all plans:
    - scheduled   -> busy only while its plan is ACTIVE (an archived/paused plan's future plan is
                     not a commitment any more; the rows are kept, never deleted);
    - in_progress -> always busy (never silently drop work that is under way);
    - completed   -> always busy (history).
    skipped/missed/rescheduled/cancelled never block."""
    initialize_study_planner_store()
    clauses, params = ["s.owner_id=?"], [owner_id]
    if start_from:
        clauses.append("s.scheduled_start >= ?")
        params.append(start_from)
    with _connect() as connection:
        rows = connection.execute(
            f"""SELECT s.* FROM study_sessions s
                JOIN study_plans p ON p.plan_id = s.plan_id AND p.owner_id = s.owner_id
                WHERE {' AND '.join(clauses)}
                  AND (s.status IN ('in_progress', 'completed') OR (s.status = 'scheduled' AND p.status = 'active'))
                ORDER BY s.scheduled_start, s.session_id""",
            params,
        ).fetchall()
    return [_session(row) for row in rows]


def count_active_plan_sessions(owner_id: str, plan_id: str) -> int:
    initialize_study_planner_store()
    with _connect() as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM study_sessions WHERE owner_id=? AND plan_id=? "
            f"AND status IN ({','.join('?' * len(ACTIVE_SESSION_STATUSES))})",
            (owner_id, plan_id, *ACTIVE_SESSION_STATUSES),
        ).fetchone()[0]


def _insert_schedule_run(connection: sqlite3.Connection, owner_id: str, plan_id: str, reason: str) -> dict:
    schedule_run_id, created_at = str(uuid4()), utc_now_iso()
    connection.execute(
        "INSERT INTO schedule_runs (schedule_run_id, owner_id, plan_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (schedule_run_id, owner_id, plan_id, reason, created_at),
    )
    return {"schedule_run_id": schedule_run_id, "owner_id": owner_id, "plan_id": plan_id,
            "reason": reason, "created_at": created_at}


def confirm_plan_sessions(owner_id: str, plan_id: str, sessions: list[dict], reason: str = "confirm") -> dict:
    """Confirm a plan: save all its sessions plus one schedule_run in ONE transaction, or nothing.
    BEGIN IMMEDIATE takes the write lock before re-checking that the plan has no active sessions,
    so two concurrent confirmations can never both succeed (the loser gets PlanAlreadyConfirmedError)."""
    initialize_study_planner_store()
    if not sessions:
        raise ValueError("No sessions to confirm.")
    rows = _session_rows(owner_id, plan_id, sessions)
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            f"SELECT COUNT(*) FROM study_sessions WHERE owner_id=? AND plan_id=? "
            f"AND status IN ({','.join('?' * len(ACTIVE_SESSION_STATUSES))})",
            (owner_id, plan_id, *ACTIVE_SESSION_STATUSES),
        ).fetchone()[0]
        if active:
            raise PlanAlreadyConfirmedError("This plan already has scheduled sessions.")
        connection.executemany(_INSERT_SESSION_SQL, rows)
        run = _insert_schedule_run(connection, owner_id, plan_id, reason)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"sessions": [get_session(owner_id, row[0]) for row in rows], "schedule_run": run}


class SessionTransitionError(ValueError):
    """A lifecycle action the session's current state does not allow. code is machine-readable:
    session_not_<action>able (wrong status), session_not_started (complete before start),
    plan_not_active (a still-scheduled session of an archived/paused plan) or slot_taken."""

    def __init__(self, code: str, message: str, status: str):
        super().__init__(message)
        self.code, self.status = code, status


# action -> (target status, statuses it may move from). Reaching the target again is a no-op.
_TRANSITIONS = {
    "start": ("in_progress", ("scheduled",)),
    "complete": ("completed", ("in_progress",)),
    "skip": ("skipped", ("scheduled", "in_progress")),
}


_REFUSALS = {"start": ("session_not_startable", "started"), "complete": ("session_not_completable", "completed"),
             "skip": ("session_not_skippable", "skipped")}


def _locked_session(connection: sqlite3.Connection, owner_id: str, session_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT s.*, p.status AS plan_status FROM study_sessions s
           JOIN study_plans p ON p.plan_id = s.plan_id
           WHERE s.session_id=? AND s.owner_id=?""",
        (session_id, owner_id),
    ).fetchone()
    if not row:
        raise ValueError("Study session not found.")
    return row


def transition_session(owner_id: str, session_id: str, action: str) -> tuple[dict, bool]:
    """Apply one lifecycle action to exactly one of the owner's sessions. Returns (session, changed):
    a session already in the action's target status is returned unchanged (changed=False), so
    repeated Start/Complete/Skip are safe. started_at and completed_at are stamped once, never
    overwritten. A *scheduled* session can only be acted on while its plan is active; work already
    under way (in_progress) can always be resumed, completed or skipped. Re-check and write share
    one BEGIN IMMEDIATE transaction, so concurrent requests cannot both write."""
    target, sources = _TRANSITIONS[action]
    initialize_study_planner_store()
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _locked_session(connection, owner_id, session_id)
        status, changed = row["status"], False
        if status != target:
            if status not in sources:
                if action == "complete" and status == "scheduled":
                    raise SessionTransitionError("session_not_started", "Start this session before completing it.", status)
                code, verb = _REFUSALS[action]
                raise SessionTransitionError(code, f"A {status} session cannot be {verb}.", status)
            if status == "scheduled" and row["plan_status"] != "active":
                raise SessionTransitionError("plan_not_active", "This session belongs to a plan that is no longer active.", status)
            now = utc_now_iso()
            stamps = {"in_progress": ", started_at=COALESCE(started_at, :now)",
                      "completed": ", completed_at=COALESCE(completed_at, :now)"}.get(target, "")
            connection.execute(
                f"UPDATE study_sessions SET status=:target, updated_at=:now{stamps} "
                "WHERE session_id=:id AND owner_id=:owner AND status=:status",
                {"target": target, "now": now, "id": session_id, "owner": owner_id, "status": status},
            )
            changed = True
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_session(owner_id, session_id), changed


def start_session(owner_id: str, session_id: str) -> tuple[dict, bool]:
    """scheduled -> in_progress (see transition_session); an in_progress session is a resume."""
    return transition_session(owner_id, session_id, "start")


RESCHEDULABLE_STATUSES = ("scheduled", "missed")


def reschedule_session(owner_id: str, session_id: str, scheduled_start: str, scheduled_end: str) -> tuple[dict, dict]:
    """Move one session to a new window. The original row is kept as history (status
    'rescheduled', which never blocks time) and a new 'scheduled' session carries the same
    plan/document/activity/artifact/reason/priority plus rescheduled_from=<original id>. Refuses a
    window that overlaps time already occupied (see list_busy_sessions). One transaction."""
    initialize_study_planner_store()
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _locked_session(connection, owner_id, session_id)
        if row["status"] not in RESCHEDULABLE_STATUSES:
            raise SessionTransitionError("session_not_reschedulable", f"A {row['status']} session cannot be rescheduled.",
                                         row["status"])
        if row["plan_status"] != "active":
            raise SessionTransitionError("plan_not_active", "This session belongs to a plan that is no longer active.",
                                         row["status"])
        duration = row["duration_minutes"]
        validate_session({"activity_type": row["activity_type"], "status": "scheduled", "reason": row["reason"],
                          "scheduled_start": scheduled_start, "scheduled_end": scheduled_end,
                          "duration_minutes": duration})
        overlap = connection.execute(
            """SELECT 1 FROM study_sessions s JOIN study_plans p ON p.plan_id = s.plan_id
               WHERE s.owner_id=? AND s.session_id<>? AND s.scheduled_start < ? AND s.scheduled_end > ?
                 AND (s.status IN ('in_progress', 'completed') OR (s.status = 'scheduled' AND p.status = 'active'))
               LIMIT 1""",
            (owner_id, session_id, scheduled_end, scheduled_start),
        ).fetchone()
        if overlap:
            raise SessionTransitionError("slot_taken", "That time is already taken by another session.", row["status"])
        now, new_id = utc_now_iso(), str(uuid4())
        connection.execute(
            "UPDATE study_sessions SET status='rescheduled', updated_at=? WHERE session_id=? AND owner_id=?",
            (now, session_id, owner_id),
        )
        connection.execute(
            """INSERT INTO study_sessions (session_id, owner_id, plan_id, document_id, activity_type, artifact_id,
                   scheduled_start, scheduled_end, duration_minutes, status, reason, priority_snapshot,
                   rescheduled_from, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)""",
            (new_id, owner_id, row["plan_id"], row["document_id"], row["activity_type"], row["artifact_id"],
             scheduled_start, scheduled_end, duration, row["reason"], row["priority_snapshot"], session_id, now, now),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_session(owner_id, session_id), get_session(owner_id, new_id)


def plan_sessions_fingerprint(sessions: list[dict]) -> tuple:
    """What an adaptation proposal was computed from: every session of the plan, with its
    state. Any difference at apply time means the proposal is stale."""
    return tuple(sorted((s["session_id"], s["status"], s["scheduled_start"], s["scheduled_end"], s["updated_at"])
                        for s in sessions))


def apply_adaptation_changes(owner_id: str, plan_id: str, fingerprint: tuple, now_local: str, *,
                             added: list[dict], moved: list[tuple[str, str, str]], cancelled: list[str],
                             replaced: list[str]) -> dict:
    """Apply one server-computed adaptation proposal atomically (all or nothing):
    - added:     new 'scheduled' rows (a replacement carries rescheduled_from);
    - moved:     (session_id, new_start, new_end) -> original becomes 'rescheduled' + a replacement
                 row with rescheduled_from=<original>;
    - cancelled: session ids -> 'cancelled' (kept as history);
    - replaced:  missed originals an added replacement stands in for -> 'rescheduled'.
    Inside one BEGIN IMMEDIATE transaction it re-checks that the plan is still active, that its
    sessions are exactly what the proposal saw (`fingerprint`), that every moved/cancelled row is a
    future 'scheduled' session and that no new window overlaps busy time. Any failure raises
    SessionTransitionError and nothing is written."""
    initialize_study_planner_store()
    if not (added or moved or cancelled):
        raise ValueError("No adaptation changes to apply.")
    material_documents = {m["document_id"] for m in list_materials(owner_id, plan_id)}
    for record in added:
        validate_session({**record, "status": "scheduled"})
        if record["document_id"] not in material_documents:
            raise ValueError("Session document is not part of this study plan.")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = connection.execute("SELECT status FROM study_plans WHERE plan_id=? AND owner_id=?",
                                  (plan_id, owner_id)).fetchone()
        if not plan:
            raise ValueError("Study plan not found.")
        if plan["status"] != "active":
            raise SessionTransitionError("plan_not_active", "Only an active plan can be adapted.", plan["status"])
        rows = {row["session_id"]: row for row in connection.execute(
            "SELECT * FROM study_sessions WHERE owner_id=? AND plan_id=?", (owner_id, plan_id)).fetchall()}
        if plan_sessions_fingerprint([dict(row) for row in rows.values()]) != fingerprint:
            raise SessionTransitionError("stale_plan", "The plan changed since this proposal was made. Try again.", "")
        touched = [session_id for session_id, _, _ in moved] + list(cancelled)
        for session_id in touched:
            row = rows.get(session_id)
            if not row or row["status"] != "scheduled" or row["scheduled_start"] < now_local:
                raise SessionTransitionError("session_not_changeable", "Only future scheduled sessions can change.",
                                             row["status"] if row else "")
        for session_id in replaced:
            row = rows.get(session_id)
            if not row or row["status"] not in ("scheduled", "missed") or row["scheduled_end"] > now_local:
                raise SessionTransitionError("session_not_changeable", "Only a missed session can be replaced.",
                                             row["status"] if row else "")
        windows = [(r["scheduled_start"], r["scheduled_end"]) for r in added] + [(a, b) for _, a, b in moved]
        for index, (start, end) in enumerate(windows):
            if any(s < end and e > start for s, e in windows[:index]):
                raise SessionTransitionError("slot_taken", "Two proposed sessions overlap.", "")
            placeholders = ",".join("?" * len(touched)) or "''"
            overlap = connection.execute(
                f"""SELECT 1 FROM study_sessions s JOIN study_plans p ON p.plan_id = s.plan_id
                    WHERE s.owner_id=? AND s.session_id NOT IN ({placeholders})
                      AND s.scheduled_start < ? AND s.scheduled_end > ?
                      AND (s.status IN ('in_progress', 'completed') OR (s.status = 'scheduled' AND p.status = 'active'))
                    LIMIT 1""",
                (owner_id, *touched, end, start),
            ).fetchone()
            if overlap:
                raise SessionTransitionError("slot_taken", "A proposed time is already taken by another session.", "")

        now = utc_now_iso()

        def insert(record: dict) -> str:
            new_id = str(uuid4())
            connection.execute(
                """INSERT INTO study_sessions (session_id, owner_id, plan_id, document_id, activity_type, artifact_id,
                       scheduled_start, scheduled_end, duration_minutes, status, reason, priority_snapshot,
                       rescheduled_from, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)""",
                (new_id, owner_id, plan_id, record["document_id"], record["activity_type"], record.get("artifact_id"),
                 record["scheduled_start"], record["scheduled_end"], record["duration_minutes"], record.get("reason"),
                 record.get("priority_snapshot"), record.get("rescheduled_from"), now, now),
            )
            return new_id

        def set_status(session_id: str, status: str, expected: tuple) -> None:
            cursor = connection.execute(
                f"UPDATE study_sessions SET status=?, updated_at=? WHERE session_id=? AND owner_id=? "
                f"AND status IN ({','.join('?' * len(expected))})",
                (status, now, session_id, owner_id, *expected),
            )
            if cursor.rowcount != 1:
                raise SessionTransitionError("stale_plan", "The plan changed since this proposal was made. Try again.", "")

        added_ids = [insert(record) for record in added]
        moved_ids = {}
        for session_id, start, end in moved:
            row = rows[session_id]
            set_status(session_id, "rescheduled", ("scheduled",))
            moved_ids[session_id] = insert({
                "document_id": row["document_id"], "activity_type": row["activity_type"], "artifact_id": row["artifact_id"],
                "scheduled_start": start, "scheduled_end": end, "duration_minutes": row["duration_minutes"],
                "reason": row["reason"], "priority_snapshot": row["priority_snapshot"], "rescheduled_from": session_id,
            })
        for session_id in cancelled:
            set_status(session_id, "cancelled", ("scheduled",))
        for session_id in replaced:
            set_status(session_id, "rescheduled", ("scheduled", "missed"))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"added": added_ids, "moved": moved_ids}


def create_session(owner_id: str, plan_id: str, session: dict) -> dict:
    return create_sessions(owner_id, plan_id, [session])[0]


def get_session(owner_id: str, session_id: str) -> dict | None:
    initialize_study_planner_store()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM study_sessions WHERE session_id=? AND owner_id=?", (session_id, owner_id),
        ).fetchone()
    return _session(row) if row else None


def list_sessions(owner_id: str, plan_id: str | None = None, document_id: str | None = None,
                  statuses: list[str] | tuple[str, ...] | None = None,
                  start_from: str | None = None, start_before: str | None = None) -> list[dict]:
    """Owner's sessions ordered by scheduled_start, optionally narrowed to one plan, one document,
    a set of statuses, and/or a [start_from, start_before) window on scheduled_start (ISO strings
    in the same naive-local format, so string comparison is chronological)."""
    initialize_study_planner_store()
    clauses, params = ["owner_id=?"], [owner_id]
    for column, value in (("plan_id", plan_id), ("document_id", document_id)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if statuses:
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if start_from:
        clauses.append("scheduled_start >= ?")
        params.append(start_from)
    if start_before:
        clauses.append("scheduled_start < ?")
        params.append(start_before)
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM study_sessions WHERE {' AND '.join(clauses)} ORDER BY scheduled_start, session_id",
            params,
        ).fetchall()
    return [_session(row) for row in rows]


def update_session(owner_id: str, session_id: str, changes: dict) -> dict:
    """Update schedule/status fields. The merged record is re-validated as a whole. Moving to
    in_progress stamps started_at (once); moving to completed stamps completed_at."""
    initialize_study_planner_store()
    allowed = ("scheduled_start", "scheduled_end", "duration_minutes", "status", "reason",
               "priority_snapshot", "artifact_id")
    fields = {key: changes[key] for key in allowed if key in changes}
    if not fields:
        raise ValueError("No session changes supplied.")
    current = get_session(owner_id, session_id)
    if not current:
        raise ValueError("Study session not found.")
    validate_session({**current, **fields})
    status = fields.get("status")
    if status == "in_progress" and not current["started_at"]:
        fields["started_at"] = utc_now_iso()
    if status == "completed":
        fields["completed_at"] = utc_now_iso()
    _update_row("study_sessions", "session_id", session_id, owner_id, fields, "Study session not found.")
    return get_session(owner_id, session_id)


def delete_session(owner_id: str, session_id: str) -> None:
    initialize_study_planner_store()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM study_sessions WHERE session_id=? AND owner_id=?", (session_id, owner_id),
        )
        if not cursor.rowcount:
            raise ValueError("Study session not found.")


def delete_plan_sessions(owner_id: str, plan_id: str, statuses: tuple[str, ...] = ("scheduled",)) -> int:
    """Bulk-clear a plan's sessions in the given statuses (default: only still-scheduled ones, so
    completed/in-progress history is never touched) -- the hook a later regenerate uses."""
    initialize_study_planner_store()
    for status in statuses:
        _require_choice(status, SESSION_STATUSES, "status")
    with _connect() as connection:
        cursor = connection.execute(
            f"DELETE FROM study_sessions WHERE owner_id=? AND plan_id=? AND status IN ({','.join('?' * len(statuses))})",
            (owner_id, plan_id, *statuses),
        )
    return cursor.rowcount


def record_plan_schedule_run(owner_id: str, plan_id: str, reason: str = "initial") -> dict:
    initialize_study_planner_store()
    if not get_plan(owner_id, plan_id):
        raise ValueError("Study plan not found.")
    with _connect() as connection:
        return _insert_schedule_run(connection, owner_id, plan_id, reason)


def list_plan_schedule_runs(owner_id: str, plan_id: str) -> list[dict]:
    initialize_study_planner_store()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM schedule_runs WHERE owner_id=? AND plan_id=? ORDER BY created_at DESC",
            (owner_id, plan_id),
        ).fetchall()
    return [{key: row[key] for key in ("schedule_run_id", "owner_id", "plan_id", "reason", "created_at")} for row in rows]


# ---------------------------------------------------------------------------
# Development reset -- current user's planner data only
# ---------------------------------------------------------------------------

def reset_planner_data(owner_id: str) -> None:
    """Delete only this owner's Study Planner data (tasks, availability, blocks, schedule runs,
    plan items, topic progress). Never touches any other feature's tables -- documents, quiz,
    flashcards, and chat history all live in separate stores this function never opens."""
    initialize_study_planner_store()
    with _connect() as connection:
        connection.execute("DELETE FROM study_sessions WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_plan_materials WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_plans WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_blocks WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM schedule_runs WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_plan_items WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM study_tasks WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM availability_slots WHERE owner_id=?", (owner_id,))
        connection.execute("DELETE FROM topic_progress WHERE owner_id=?", (owner_id,))
