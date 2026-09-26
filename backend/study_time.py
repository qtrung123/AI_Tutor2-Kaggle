"""Pure minute-of-day interval math shared by the whole Study Planner.

Availability parsing, interval merge/subtract and per-day free time. Used by the legacy planner
service/store and by the Planner v2 scheduler, adaptation and placement modules. No persistence
and no scheduling decisions here.
"""
from datetime import date, datetime, timedelta

# The planner's availability grid granularity (also used by the frontend calendar). Kept as its
# own constant, distinct from MIN_BLOCK_MINUTES, even though both are currently 30 -- one is a
# grid-alignment size, the other a scheduling-policy minimum.
GRID_ALIGNMENT_MINUTES = 30


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
        # A block is busy if it's confirmed/locked (Phase 1 rule, unchanged) OR -- Phase 3 --
        # already completed, regardless of its (possibly still 'suggested', never-accepted)
        # status/locked flags. A completed block is never touched by regeneration's cleanup, so
        # it must also never be treated as free time a regenerated block can be placed into.
        is_busy = (
            block["status"] in {"confirmed", "completed"} or block.get("locked")
            or block.get("completion_status") == "completed"
        )
        if not is_busy:
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
