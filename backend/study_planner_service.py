"""Study Planner business logic (Phase 1 / MVP): interval math, the deterministic scheduler,
and the Study Buffer calculation. Persistence lives in study_planner_store.py.

Deliberately LLM-free -- the scheduler only ever places blocks inside user-selected
availability, never after the deadline, and never overlapping an already-busy block.
"""

from datetime import date, datetime, timedelta

from backend import study_planner_store

MIN_BLOCK_MINUTES = 30
PREFERRED_BLOCK_MINUTES = 60
MAX_BLOCK_MINUTES = 90
MAX_ESTIMATED_MINUTES = 100_000
# The planner's availability grid granularity (also used by the frontend calendar). Kept as its
# own constant, distinct from MIN_BLOCK_MINUTES, even though both are currently 30 -- one is a
# grid-alignment size, the other a scheduling-policy minimum.
GRID_ALIGNMENT_MINUTES = 30


# ---------------------------------------------------------------------------
# Minute-of-day interval math (shared with study_planner_store's availability merge/subtract)
# ---------------------------------------------------------------------------

def to_minutes(hhmm: str) -> int:
    hours, minutes = str(hhmm).split(":")
    return int(hours) * 60 + int(minutes)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def merge_minute_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of possibly-overlapping/adjacent (start, end) minute-of-day intervals into the
    minimal, non-overlapping, sorted set."""
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_minute_interval(
    intervals: list[tuple[int, int]], remove_start: int, remove_end: int,
) -> list[tuple[int, int]]:
    """Remove [remove_start, remove_end) from a set of (start, end) intervals -- splits an
    interval in two, shrinks one edge, or drops it entirely, as needed."""
    result = []
    for start, end in intervals:
        if remove_end <= start or remove_start >= end:
            result.append((start, end))
            continue
        if remove_start > start:
            result.append((start, remove_start))
        if remove_end < end:
            result.append((remove_end, end))
    return result


def _datetime_minute_of_day(value: str) -> int:
    return to_minutes(value[11:16])


def _align_up_to_grid(minute: int, grid: int = GRID_ALIGNMENT_MINUTES) -> int:
    """Round a minute-of-day UP to the next planner grid boundary (e.g. 18:20 -> 18:30), never
    down -- a partially elapsed grid cell is never usable study time."""
    return ((minute + grid - 1) // grid) * grid


def free_minutes_by_date(
    start_date: date, end_date: date, availability: list[dict], existing_blocks: list[dict],
    now: datetime | None = None,
) -> dict[date, list[tuple[int, int]]]:
    """For each date in [start_date, end_date] inclusive: available minutes (dated slots plus
    expanded recurring-weekday slots) minus busy minutes from any confirmed/completed/locked
    study block -- never fabricates time outside what the user actually selected.

    When `now` is given and falls on one of these dates, that date's already-elapsed time (up
    to the next 30-minute grid boundary at or after `now`) is also excluded -- the scheduler
    must never place a block in the past, whether the day is fully or only partially elapsed.
    """
    dated: dict[str, list[tuple[int, int]]] = {}
    recurring_by_weekday: dict[int, list[tuple[int, int]]] = {}
    for slot in availability:
        if slot.get("is_recurring"):
            recurring_by_weekday.setdefault(int(slot["day_of_week"]), []).append(
                (to_minutes(slot["start_at"]), to_minutes(slot["end_at"]))
            )
        else:
            dated.setdefault(slot["date"], []).append(
                (to_minutes(slot["start_at"]), to_minutes(slot["end_at"]))
            )

    busy_by_date: dict[str, list[tuple[int, int]]] = {}
    for block in existing_blocks:
        if block["status"] not in {"confirmed", "completed"} and not block.get("locked"):
            continue
        busy_by_date.setdefault(block["start_at"][:10], []).append(
            (_datetime_minute_of_day(block["start_at"]), _datetime_minute_of_day(block["end_at"]))
        )

    result: dict[date, list[tuple[int, int]]] = {}
    current = start_date
    while current <= end_date:
        key = current.isoformat()
        day_intervals = list(dated.get(key, []))
        day_intervals.extend(recurring_by_weekday.get(current.weekday(), []))
        free = merge_minute_intervals(day_intervals)
        for busy_start, busy_end in merge_minute_intervals(busy_by_date.get(key, [])):
            free = subtract_minute_interval(free, busy_start, busy_end)
        if now is not None and current == now.date():
            elapsed_boundary = _align_up_to_grid(now.hour * 60 + now.minute)
            free = subtract_minute_interval(free, 0, elapsed_boundary)
        result[current] = free
        current += timedelta(days=1)
    return result


def total_free_minutes(free_by_date: dict[date, list[tuple[int, int]]]) -> int:
    return sum(end - start for intervals in free_by_date.values() for start, end in intervals)


def _flatten_intervals(free_by_date: dict[date, list[tuple[int, int]]]) -> list[tuple[date, int, int]]:
    flattened = []
    for day in sorted(free_by_date):
        for start, end in free_by_date[day]:
            if end > start:
                flattened.append((day, start, end))
    return flattened


def allocate_blocks(
    remaining_minutes: int, free_by_date: dict[date, list[tuple[int, int]]],
) -> tuple[list[dict], int]:
    """Deterministically carve `remaining_minutes` of study time out of the given free
    intervals. Round-robins across intervals/days (one block per interval per pass) so blocks
    spread across days when practical instead of stacking into one long same-day session.
    Returns (allocations, shortage_minutes); shortage_minutes > 0 means the available time could
    not cover the full request -- callers must report that, never invent time outside it.
    """
    intervals = _flatten_intervals(free_by_date)
    cursors = [start for _, start, _ in intervals]
    ends = [end for _, _, end in intervals]
    days = [day for day, _, _ in intervals]
    allocations: list[dict] = []
    remaining = remaining_minutes
    progressed = True
    while remaining > 0 and progressed:
        progressed = False
        for index in range(len(intervals)):
            if remaining <= 0:
                break
            free_here = ends[index] - cursors[index]
            if free_here < MIN_BLOCK_MINUTES:
                continue
            block_len = min(PREFERRED_BLOCK_MINUTES, remaining, free_here, MAX_BLOCK_MINUTES)
            leftover_after = remaining - block_len
            # Absorb a too-small remainder into this block rather than leaving an unusable
            # sub-30-minute sliver for a later pass, as long as it still respects the max.
            if 0 < leftover_after < MIN_BLOCK_MINUTES and block_len + leftover_after <= min(MAX_BLOCK_MINUTES, free_here):
                block_len += leftover_after
            block_len = max(MIN_BLOCK_MINUTES, min(block_len, MAX_BLOCK_MINUTES, free_here, remaining))
            block_start = cursors[index]
            block_end = block_start + block_len
            allocations.append({"date": days[index], "start_minute": block_start, "end_minute": block_end,
                                 "planned_minutes": block_len})
            cursors[index] = block_end
            remaining -= block_len
            progressed = True
    return allocations, max(0, remaining)


def _combine(day: date, minute_of_day: int) -> str:
    hour, minute = divmod(minute_of_day, 60)
    return datetime(day.year, day.month, day.day, hour, minute).isoformat()


def compute_schedule(task: dict, availability: list[dict], existing_blocks: list[dict],
                      now: datetime | None = None) -> dict:
    """Pure scheduling computation (no persistence) -- used directly by generate_schedule and
    by tests. Returns a structured result with status "on_track" or "schedule_risk".

    `now` is a NAIVE LOCAL datetime (the same wall-clock frame every planner time is stored in --
    see study_planner_store/frontend for the contract) representing "the current moment" for the
    purpose of excluding already-elapsed availability today. It should come from the caller's
    own local clock (the browser, ultimately) rather than being assumed from the server's -- a
    server running in a different timezone (e.g. Kaggle) must never decide what is "already
    past" for the user. Defaults to the server's local clock only when the caller has none.
    """
    now = now or datetime.now()
    today = now.date()
    deadline = date.fromisoformat(task["deadline"])
    required_minutes = int(task["remaining_minutes"])
    if deadline < today:
        return {
            "status": "schedule_risk", "available_minutes": 0, "required_minutes": required_minutes,
            "study_buffer": -required_minutes, "shortage_minutes": required_minutes, "blocks": [],
        }
    free_by_date = free_minutes_by_date(today, deadline, availability, existing_blocks, now=now)
    available_minutes = total_free_minutes(free_by_date)
    allocations, shortage = allocate_blocks(required_minutes, free_by_date)
    study_buffer = available_minutes - required_minutes
    blocks = [
        {
            "title": f"{task['title']} - Session {index}",
            "document_id": task.get("document_id"), "topic_id": None,
            "start_at": _combine(allocation["date"], allocation["start_minute"]),
            "end_at": _combine(allocation["date"], allocation["end_minute"]),
            "planned_minutes": allocation["planned_minutes"],
        }
        for index, allocation in enumerate(allocations, start=1)
    ]
    return {
        "status": "schedule_risk" if shortage > 0 else "on_track",
        "available_minutes": available_minutes, "required_minutes": required_minutes,
        "study_buffer": study_buffer, "shortage_minutes": shortage, "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Service entry points (persistence + validation)
# ---------------------------------------------------------------------------

def create_task(owner_id: str, title: str, deadline: str, estimated_minutes: int,
                 document_id: str | None = None) -> dict:
    title = str(title or "").strip()
    if not title:
        raise ValueError("Task title is required.")
    try:
        date.fromisoformat(deadline)
    except ValueError as error:
        raise ValueError("deadline must be an ISO date (YYYY-MM-DD).") from error
    estimated_minutes = int(estimated_minutes)
    if not 0 < estimated_minutes <= MAX_ESTIMATED_MINUTES:
        raise ValueError("estimated_minutes must be a positive number of minutes.")
    return study_planner_store.create_task(
        owner_id, title, deadline, estimated_minutes, document_id=document_id or None,
    )


def generate_schedule(owner_id: str, task_id: str, now: datetime | None = None) -> dict:
    task = study_planner_store.get_task(owner_id, task_id)
    if not task:
        raise ValueError("Study task not found.")
    availability = study_planner_store.list_availability(owner_id)
    existing_blocks = study_planner_store.list_blocks(owner_id)  # shared calendar, all tasks
    result = compute_schedule(task, availability, existing_blocks, now=now)
    # Replace this task's still-unlocked suggested blocks with the fresh plan. Confirmed,
    # completed, missed, and locked blocks are never silently touched.
    study_planner_store.delete_unlocked_suggested_blocks_for_task(owner_id, task_id)
    saved_blocks = (
        study_planner_store.create_blocks(owner_id, task_id, result["blocks"])
        if result["blocks"] else []
    )
    study_planner_store.record_schedule_run(owner_id, task_id, reason="initial")
    return {**result, "blocks": saved_blocks}


def accept_plan(owner_id: str, task_id: str) -> list[dict]:
    task = study_planner_store.get_task(owner_id, task_id)
    if not task:
        raise ValueError("Study task not found.")
    return study_planner_store.accept_blocks_for_task(owner_id, task_id)


def edit_block(owner_id: str, block_id: str, start_at: str | None = None,
                end_at: str | None = None) -> dict:
    """A user-initiated move/resize (both start and end may change, so duration can change too).
    Always marks the block locked so future schedule regeneration for this task never silently
    overwrites it. Validates end > start, that the new range stays inside the owner's selected
    availability, and that it does not overlap another busy (confirmed/completed/locked) block.
    """
    block = study_planner_store.get_block(owner_id, block_id)
    if not block:
        raise ValueError("Study block not found.")
    if block["status"] in {"completed", "missed"}:
        raise ValueError("Cannot edit a completed or missed study block.")
    new_start = start_at or block["start_at"]
    new_end = end_at or block["end_at"]
    start_dt = datetime.fromisoformat(new_start)
    end_dt = datetime.fromisoformat(new_end)
    if end_dt <= start_dt:
        raise ValueError("end_at must be after start_at.")
    if start_dt.date() != end_dt.date():
        raise ValueError("A study block must start and end on the same day.")

    availability = study_planner_store.list_availability(owner_id)
    day_free = free_minutes_by_date(start_dt.date(), start_dt.date(), availability, [])[start_dt.date()]
    start_minute = start_dt.hour * 60 + start_dt.minute
    end_minute = end_dt.hour * 60 + end_dt.minute
    if not any(free_start <= start_minute and end_minute <= free_end for free_start, free_end in day_free):
        raise ValueError("The requested time is outside your selected availability.")

    for other in study_planner_store.list_blocks(owner_id):
        if other["block_id"] == block_id:
            continue
        if other["status"] not in {"confirmed", "completed"} and not other["locked"]:
            continue
        other_start = datetime.fromisoformat(other["start_at"])
        other_end = datetime.fromisoformat(other["end_at"])
        if start_dt < other_end and other_start < end_dt:
            raise ValueError("This time overlaps another study block.")

    changes = {
        "locked": True, "start_at": new_start, "end_at": new_end,
        "planned_minutes": int((end_dt - start_dt).total_seconds() // 60),
    }
    return study_planner_store.update_block(owner_id, block_id, changes)


def study_buffer_status(study_buffer: int) -> str:
    return "on_track" if study_buffer >= 0 else "schedule_risk"
