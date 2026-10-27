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

from backend.study_combined_sessions import activity_key, activity_types, in_flight_activities, keeps_single_sessions
from backend.study_time import free_minutes_by_date, to_hhmm
from backend.study_scheduler import DEFAULT_CONFIG, select_candidates
from backend.study_scheduler_contracts import REASON_FINAL_REVIEW, ProposedSession
from backend.study_session_duration import MIN_SESSION_MINUTES

PLACEMENT_STEP_MINUTES = 15

PLACEMENT_MESSAGES = {
    "invalid_time": "Pick a time on the calendar grid.",
    "slot_in_past": "That time has already passed.",
    "outside_availability": "Pick a time inside your available hours.",
    "after_deadline": "That is after this material's deadline.",
    "slot_taken": "That time is already taken by another session.",
    "calendar_busy": "That time is busy in your Google Calendar.",
    "candidate_stale": "This suggestion changed. The calendar has been refreshed.",
}


class PlacementError(ValueError):
    def __init__(self, code: str):
        super().__init__(PLACEMENT_MESSAGES[code])
        self.code = code


def candidate_key(document_id: str, activity_type: str, ordinal: int) -> str:
    """`activity_type` is the activity key: "summary", or "summary+flashcards" for a combined session."""
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
        # Inside the learner's own hours but blocked by Google Calendar busy time: say so.
        own = free_minutes_by_date(day, day, [slot for slot in availability if not slot.get("busy")], []).get(day, [])
        inside_own = any(begin <= first and last <= finish for begin, finish in own)
        raise PlacementError("calendar_busy" if inside_own else "outside_availability")
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
    steps: tuple = ()       # a combined session's two steps


def keyed_candidates(result) -> list[_Entry]:
    """Every activity of one scheduler result -- proposals first, then what did not fit -- with a
    stable key per (document, activity -- both activities for a combined session --, n-th occurrence)."""
    counts: dict = {}
    entries = []

    def key_for(document_id, activity):
        ordinal = counts.get((document_id, activity), 0)
        counts[(document_id, activity)] = ordinal + 1
        return candidate_key(document_id, activity, ordinal)

    for p in result.proposals:
        entries.append(_Entry(key_for(p.document_id, p.key), p.document_id, p.activity_type,
                              p.duration_minutes, p.reason, p.artifact_id, p.priority_snapshot,
                              p.scheduled_start, p.scheduled_end, steps=p.steps))
    for c in result.capacity.unscheduled:
        entries.append(_Entry(key_for(c.document_id, c.key), c.document_id, c.activity_type,
                              c.estimated_minutes, c.reason, c.artifact_id, None, None, None, steps=c.steps))
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
                                      artifact_id=e.artifact_id, priority_snapshot=e.priority, steps=e.steps)
                      for e in scheduled)
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

def planning_material(material, document_sessions):
    """The material as the planner should see it: a summary / flashcards step still to do in a
    combined session under way counts as done (it is being done there), so it is never suggested
    or scheduled twice. Learning progress itself (DocumentStudyState) is unchanged."""
    pending = {activity for activity in in_flight_activities(document_sessions) if activity in ("summary", "flashcards")}
    if not pending:
        return material
    state = replace(material.state, completed_activities=tuple(sorted(set(material.state.completed_activities) | pending)))
    return replace(material, state=state)


def queue_reason(duration: int, deadline: str | None, *, now: datetime, availability, busy,
                 horizon_days: int) -> dict:
    """Why a still-wanted activity has no session yet, for the Study Queue. Checks the same rules a
    drag onto the calendar is validated against (15-minute grid, inside availability and Google free
    time, not past, not after the deadline, not overlapping a session):
    - a valid window exists -> the plan's own pacing held it back (daily study target, heavy sessions
      per day, spacing between steps); the learner may still drag it there;
    - none exists -> there is genuinely no room before the deadline (or in the planning horizon)."""
    today = now.date()
    last = date.fromisoformat(deadline) if deadline else today + timedelta(days=horizon_days)
    taken: dict = {}
    for session in busy:
        same_day = session["scheduled_end"][:10] == session["scheduled_start"][:10]
        taken.setdefault(session["scheduled_start"][:10], []).append(
            (session["scheduled_start"][11:16], session["scheduled_end"][11:16] if same_day else "24:00"))
    day = today
    while day <= last:
        for begin, finish in free_minutes_by_date(day, day, list(availability), [], now=now).get(day, []):
            start = -(-begin // PLACEMENT_STEP_MINUTES) * PLACEMENT_STEP_MINUTES
            while start + duration <= finish:
                window_start, window_end = to_hhmm(start), to_hhmm(start + duration)
                if not any(b_start < window_end and b_end > window_start for b_start, b_end in taken.get(day.isoformat(), [])):
                    return {"code": "held_by_pacing", "message": (
                        "Free time is left, but your daily study target and the spacing between study steps "
                        "kept it out of the plan. Drag it onto a free slot to add it.")}
                start += PLACEMENT_STEP_MINUTES
        day += timedelta(days=1)
    if deadline:
        return {"code": "no_room_before_deadline", "message": (
            f"No free {duration}-minute slot before the deadline ({deadline}). "
            "Add available time or move another session.")}
    return {"code": "no_room", "message": (
        f"No free {duration}-minute slot in the next {horizon_days} days. Add available time to fit it in.")}


def _final_window_start(deadline: date, availability, rules) -> date:
    """The first day of a deadline's final review window, widened like the scheduler's
    _widen_empty_final_windows: a window without a usable day starts on the last usable day before
    it, at most final_review_fallback_days before the deadline. Usable = at least one session's
    worth of available time that day (the learner's availability, not the sessions now in it)."""
    start = deadline - timedelta(days=rules.final_review_window_days)
    floor = deadline - timedelta(days=rules.final_review_fallback_days)
    free = free_minutes_by_date(min(start, floor), deadline - timedelta(days=1),
                                [slot for slot in availability if not slot.get("busy")], [])
    usable = sorted(day for day, intervals in free.items()
                    if min(rules.daily_target_minutes, sum(end - begin for begin, end in intervals)) >= MIN_SESSION_MINUTES)
    if any(day >= start for day in usable):
        return start
    earlier = [day for day in usable if floor <= day < start]
    return earlier[-1] if earlier else start


def _final_review_covered(document_sessions: list[dict], deadline: str | None, availability, config) -> bool:
    """The scheduler's own rule (_DocumentPlan.final_satisfied_by_retrieval): a quiz / quiz retry
    placed inside the final review window, as the document's last session before the deadline,
    already is the final review, so the plan never schedules a separate one. Without this the queue
    listed that review as "needs a time slot" right after Accept Plan although nothing was missing."""
    if not deadline:
        return False
    rules = config or DEFAULT_CONFIG
    window_start = _final_window_start(date.fromisoformat(deadline), availability, rules).isoformat()
    kept = sorted((s for s in document_sessions
                   if s["status"] in ("scheduled", "in_progress", "completed") and s["scheduled_start"][:10] < deadline),
                  key=lambda s: s["scheduled_start"])
    if not kept:
        return False
    last = kept[-1]
    return (last["scheduled_start"][:10] >= window_start
            and activity_types(last["activity_type"], last.get("steps"))[-1] in rules.retrieval_activities)


def live_candidates(context, plan_sessions: list[dict], config=None) -> list[dict]:
    """The scheduler's current candidate activities for each material of a confirmed plan that no
    scheduled / in-progress session of that document and activity already covers (a combined
    session covers a combined candidate of the same two activities). A final review that a quiz in
    its window already covers is left out, exactly as the scheduler leaves it out."""
    covered: dict = {}
    for session in plan_sessions:
        if session["status"] in ("scheduled", "in_progress"):
            # An older plan's Written Quiz covers today's flashcard recall practice.
            activity = "flashcards" if session["activity_type"] == "written_quiz" else session["activity_type"]
            keys = [activity_key(activity, session.get("steps"))]
            if session["status"] == "in_progress" and session.get("steps"):
                keys = list(in_flight_activities([session]))   # what is left of a combined session under way
            for key in keys:
                pair = (session["document_id"], key)
                covered[pair] = covered.get(pair, 0) + 1
    found = []
    for material in context.materials:
        seen: dict = {}
        rules = config
        own = [s for s in plan_sessions if s["document_id"] == material.document_id]
        if config is not None and keeps_single_sessions(own):
            rules = replace(config, combine_sessions=False)   # an older plan keeps single-activity suggestions
        final_covered = _final_review_covered(own, material.deadline, context.availability, rules)
        material = planning_material(material, own)
        for candidate in select_candidates(material, context.now, utc_offset=context.utc_offset,
                                           **({"config": rules} if rules is not None else {})):
            if final_covered and candidate.reason.code == REASON_FINAL_REVIEW:
                continue
            pair = (candidate.document_id, candidate.key)
            ordinal = seen.get(pair, 0)
            seen[pair] = ordinal + 1
            if ordinal < covered.get(pair, 0):
                continue
            found.append({"candidate_key": candidate_key(*pair, ordinal), "candidate": candidate,
                          "title": material.state.title})
    return found
