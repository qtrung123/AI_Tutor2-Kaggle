"""Direct manipulation of study sessions (Phase 7B): validated user placements.

The deterministic scheduler stays authoritative. A learner may only *move* what the server itself
proposes or wants done:

- before a plan is confirmed, a placement names one of the scheduler's own candidates by its
  `candidate_key` (derived from the server's recomputation, never from client data) plus a start
  time; preview and confirm recompute the schedule, then apply only placements that still name a
  real candidate and fit the rules below. Confirm rejects the whole request if any placement is
  stale or invalid (nothing is saved);
- for a confirmed plan, a session is rescheduled to a chosen start, or a still-wanted activity
  (see live_candidates) is placed as a new session.

Every placed window must start on a 15-minute boundary, lie in the future, inside one of the
learner's available intervals on that date, not after the material's deadline, and not overlap
other study sessions. Durations always come from the server (the proposal or the candidate).
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from backend.study_time import free_minutes_by_date
from backend.study_scheduler import select_candidates
from backend.study_scheduler_contracts import ProposedSession

PLACEMENT_STEP_MINUTES = 15

PLACEMENT_MESSAGES = {
    "invalid_time": "Pick a time on the calendar grid.",
    "slot_in_past": "That time has already passed.",
    "outside_availability": "Pick a time inside your available hours.",
    "after_deadline": "That is after this material's deadline.",
    "slot_taken": "That time is already taken by another session.",
    "candidate_stale": "This suggestion changed. The calendar has been refreshed.",
}


class PlacementError(ValueError):
    def __init__(self, code: str):
        super().__init__(PLACEMENT_MESSAGES[code])
        self.code = code


def candidate_key(document_id: str, activity_type: str, ordinal: int) -> str:
    return f"{document_id}|{activity_type}|{ordinal}"


def _parse_start(value: str) -> datetime:
    try:
        start = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise PlacementError("invalid_time") from error
    if start.tzinfo is not None or start.second or start.microsecond or start.minute % PLACEMENT_STEP_MINUTES:
        raise PlacementError("invalid_time")
    return start


def validate_window(scheduled_start: str, duration_minutes: int, *, now: datetime, availability, deadline: str | None,
                    busy) -> tuple[str, str]:
    """(start, end) naive-local ISO strings for a valid window, else PlacementError."""
    start = _parse_start(scheduled_start)
    end = start + timedelta(minutes=duration_minutes)
    day = start.date()
    if end.date() != day and end != datetime(day.year, day.month, day.day) + timedelta(days=1):
        raise PlacementError("outside_availability")
    if start < now:
        raise PlacementError("slot_in_past")
    if deadline and day > date.fromisoformat(deadline):
        raise PlacementError("after_deadline")
    first = start.hour * 60 + start.minute
    last = first + duration_minutes
    intervals = free_minutes_by_date(day, day, list(availability), []).get(day, [])
    if not any(begin <= first and last <= finish for begin, finish in intervals):
        raise PlacementError("outside_availability")
    start_iso, end_iso = start.isoformat(), end.isoformat()
    if any(s["scheduled_start"] < end_iso and s["scheduled_end"] > start_iso for s in busy):
        raise PlacementError("slot_taken")
    return start_iso, end_iso


# ---------------------------------------------------------------------------
# Before confirmation: placements over the scheduler's own result
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    key: str
    document_id: str
    activity_type: str
    duration: int
    reason: object
    artifact_id: str | None
    priority: float | None
    start: str | None       # None: not on the calendar (unscheduled)
    end: str | None
    placed: bool = False


def keyed_candidates(result) -> list[_Entry]:
    """Every activity of one scheduler result -- proposals first, then what did not fit -- with a
    stable key per (document, activity, n-th occurrence)."""
    counts: dict = {}
    entries = []

    def key_for(document_id, activity):
        ordinal = counts.get((document_id, activity), 0)
        counts[(document_id, activity)] = ordinal + 1
        return candidate_key(document_id, activity, ordinal)

    for p in result.proposals:
        entries.append(_Entry(key_for(p.document_id, p.activity_type), p.document_id, p.activity_type,
                              p.duration_minutes, p.reason, p.artifact_id, p.priority_snapshot,
                              p.scheduled_start, p.scheduled_end))
    for c in result.capacity.unscheduled:
        entries.append(_Entry(key_for(c.document_id, c.activity_type), c.document_id, c.activity_type,
                              c.estimated_minutes, c.reason, c.artifact_id, None, None, None))
    return entries


def apply_placements(result, placements: list[dict], *, now: datetime, availability, busy, deadlines: dict):
    """(new result, entries, rejected) -- the scheduler's result with every valid placement applied.
    `placements` are [{candidate_key, scheduled_start}]; the last one per key wins. A rejected
    placement leaves its activity where the scheduler put it."""
    entries = keyed_candidates(result)
    by_key = {entry.key: entry for entry in entries}
    original = {entry.key: (entry.start, entry.end) for entry in entries}
    rejected = []
    latest = {}
    for placement in placements:
        latest[placement["candidate_key"]] = placement["scheduled_start"]
    for key, start in latest.items():
        entry = by_key.get(key)
        if entry is None:
            rejected.append({"candidate_key": key, "code": "candidate_stale", "message": PLACEMENT_MESSAGES["candidate_stale"]})
            continue
        try:
            entry.start, entry.end = validate_window(start, entry.duration, now=now, availability=availability,
                                                     deadline=deadlines.get(entry.document_id), busy=busy)
            entry.placed = True
        except PlacementError as error:
            rejected.append({"candidate_key": key, "code": error.code, "message": str(error)})
    # A placed activity may not overlap anything else on the calendar; undo placements until none do.
    changed = True
    while changed:
        changed = False
        for entry in entries:
            if not entry.placed:
                continue
            clash = any(other is not entry and other.start and other.start < entry.end and other.end > entry.start
                        for other in entries)
            if clash:
                entry.start, entry.end = original[entry.key]
                entry.placed = False
                rejected.append({"candidate_key": entry.key, "code": "slot_taken", "message": PLACEMENT_MESSAGES["slot_taken"]})
                changed = True
    scheduled = sorted((e for e in entries if e.start), key=lambda e: (e.start, e.key))
    proposals = tuple(ProposedSession(e.document_id, e.activity_type, e.start, e.end, e.duration, e.reason,
                                      artifact_id=e.artifact_id, priority_snapshot=e.priority) for e in scheduled)
    placed_keys = {e.key for e in entries if e.placed}
    # entries after the proposals are the unscheduled candidates, in the same order
    kept = [candidate for entry, candidate in zip(entries[len(result.proposals):], result.capacity.unscheduled)
            if not entry.start]
    scheduled_minutes = sum(p.duration_minutes for p in proposals)
    shortfall = sum(c.estimated_minutes or 0 for c in kept)
    capacity = replace(result.capacity, scheduled_minutes=scheduled_minutes, shortfall_minutes=shortfall,
                       required_minutes=scheduled_minutes + shortfall, unscheduled=tuple(kept),
                       status="on_track" if shortfall == 0 else "at_risk")
    new_result = replace(result, proposals=proposals, capacity=capacity)
    return new_result, {e.key: e for e in entries}, placed_keys, rejected


# ---------------------------------------------------------------------------
# After confirmation: what the planner still wants done
# ---------------------------------------------------------------------------

def live_candidates(context, plan_sessions: list[dict]) -> list[dict]:
    """The scheduler's current candidate activities for each material of a confirmed plan that no
    scheduled / in-progress session of that document and activity already covers."""
    covered: dict = {}
    for session in plan_sessions:
        if session["status"] in ("scheduled", "in_progress"):
            pair = (session["document_id"], session["activity_type"])
            covered[pair] = covered.get(pair, 0) + 1
    found = []
    for material in context.materials:
        seen: dict = {}
        for candidate in select_candidates(material, context.now, utc_offset=context.utc_offset):
            pair = (candidate.document_id, candidate.activity_type)
            ordinal = seen.get(pair, 0)
            seen[pair] = ordinal + 1
            if ordinal < covered.get(pair, 0):
                continue
            found.append({"candidate_key": candidate_key(*pair, ordinal), "candidate": candidate,
                          "title": material.state.title})
    return found
