"""Deterministic adaptive replanning for Study Planner v2 (Phase 5A): PROPOSALS ONLY.

Given what just happened (a trigger) and the persisted plan, propose the smallest set of changes
to the plan's FUTURE sessions. Nothing is written here: the result lists sessions to add, move or
cancel, how many stay unchanged, warnings, human-readable reasons and a small|large significance.

Rules
- Only future sessions (status 'scheduled', starting at/after now) of the affected documents can
  change. Completed / in-progress / skipped / missed / rescheduled rows and anything already
  under way are history: they are never touched and they keep occupying their time.
- The steps a document still needs come from the scheduler itself (study_scheduler._build_steps,
  i.e. the same state-based rules: low score -> flashcards + quiz_retry, moderate -> review +
  later retry, strong -> review pushed out, final review before the deadline). Existing sessions
  are matched to those steps first; only what no longer fits is moved, only what is missing is
  added, and only what is truly obsolete is cancelled.
- New/moved sessions stay inside the learner's availability, never overlap busy time (padded by
  the session gap), respect the daily comfortable capacity, heavy-session limits, per-document
  daily limits and day spacing between a document's steps, and never land after a deadline: a
  step that no longer fits before it becomes a warning instead.
- Deterministic: same input, same output (sorted iteration, no clock reads, no randomness).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backend.study_planner_service import free_minutes_by_date, subtract_minute_interval, to_minutes
from backend.study_scheduler import DEFAULT_CONFIG, SchedulerConfig, _build_steps, _Step, resolve_clock
from backend.study_scheduler_contracts import REASON_FINAL_REVIEW, REASON_RESCHEDULED, MaterialContext

TRIGGER_KINDS = ("quiz_completed", "session_missed", "session_skipped", "availability_changed", "deadline_changed")
_ACTIVITY_NAMES = {"summary": "summary", "flashcards": "flashcards", "quiz": "quiz", "review": "review",
                   "quiz_retry": "quiz retry"}


@dataclass(frozen=True)
class AdaptationConfig:
    # small = at most this many changes, all within at most this many documents; otherwise large.
    small_max_changes: int = 2
    small_max_documents: int = 1
    # After a quiz, a review/retry session is kept only if it is due within this many days of the
    # step's new earliest date (so a low score pulls practice EARLIER, a strong one pushes it out).
    review_keep_tolerance_days: int = 1
    scheduler: SchedulerConfig = DEFAULT_CONFIG


DEFAULT_ADAPTATION_CONFIG = AdaptationConfig()


@dataclass(frozen=True)
class AdaptationTrigger:
    kind: str
    document_id: str | None = None   # quiz_completed / deadline_changed
    session_id: str | None = None    # session_missed / session_skipped

    def __post_init__(self):
        if self.kind not in TRIGGER_KINDS:
            raise ValueError(f"Unknown adaptation trigger: {self.kind}.")
        if self.kind in ("quiz_completed", "deadline_changed") and not self.document_id:
            raise ValueError(f"{self.kind} needs a document_id.")
        if self.kind in ("session_missed", "session_skipped") and not self.session_id:
            raise ValueError(f"{self.kind} needs a session_id.")


@dataclass(frozen=True)
class AdaptationContext:
    """Everything a proposal may read. `now` is naive learner-local (the planner convention).
    `plan_sessions`: every session of THIS plan (any status). `busy_sessions`: the owner's sessions
    that occupy time (study_planner_store.list_busy_sessions, all plans)."""
    trigger: AdaptationTrigger
    now: datetime
    materials: tuple[MaterialContext, ...]
    plan_sessions: tuple[dict, ...]
    availability: tuple[dict, ...]
    busy_sessions: tuple[dict, ...]
    utc_offset: timedelta | None = None


@dataclass(frozen=True)
class AddedSession:
    document_id: str
    activity_type: str
    scheduled_start: str
    scheduled_end: str
    duration_minutes: int
    reason_code: str
    message: str
    artifact_id: str | None = None
    replaces_session_id: str | None = None


@dataclass(frozen=True)
class MovedSession:
    session_id: str
    document_id: str
    activity_type: str
    from_start: str
    from_end: str
    to_start: str
    to_end: str
    message: str


@dataclass(frozen=True)
class CancelledSession:
    session_id: str
    document_id: str
    activity_type: str
    scheduled_start: str
    message: str


@dataclass(frozen=True)
class AdaptationResult:
    trigger: AdaptationTrigger
    added: tuple[AddedSession, ...]
    moved: tuple[MovedSession, ...]
    cancelled: tuple[CancelledSession, ...]
    unchanged_count: int
    warnings: tuple[dict, ...]
    significance: str   # small | large
    reasons: tuple[str, ...]

    @property
    def change_count(self) -> int:
        return len(self.added) + len(self.moved) + len(self.cancelled)

    def to_dict(self) -> dict:
        def rows(items):
            return [dict(item.__dict__) for item in items]
        return {
            "trigger": {key: value for key, value in self.trigger.__dict__.items() if value is not None},
            "added": rows(self.added), "moved": rows(self.moved), "cancelled": rows(self.cancelled),
            "unchanged_count": self.unchanged_count, "change_count": self.change_count,
            "warnings": [dict(w) for w in self.warnings], "significance": self.significance,
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# Time occupancy (availability minus busy time), capacity and spacing
# ---------------------------------------------------------------------------

def _day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _minute(value: str) -> int:
    return to_minutes(value[11:16])


def _at(day: date, minute: int) -> str:
    return datetime(day.year, day.month, day.day, minute // 60, minute % 60).isoformat()


class _Calendar:
    """The owner's occupied time while a proposal is being built. Sessions are keyed by id so a
    session being moved or cancelled can be released first."""

    def __init__(self, context: AdaptationContext, now: datetime, config: SchedulerConfig):
        self.now, self.config = now, config
        self.availability = list(context.availability)
        # A scheduled session whose time is over (missed) neither blocks time nor uses the day's budget.
        nowiso = now.isoformat()
        self.sessions = {s["session_id"]: s for s in context.busy_sessions
                         if not (s["status"] == "scheduled" and s["scheduled_end"] <= nowiso)}
        self._placed = 0

    def release(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def occupy(self, document_id: str, activity: str, start: str, end: str, duration: int) -> None:
        self._placed += 1
        self.sessions[f"__proposed_{self._placed}"] = {
            "session_id": f"__proposed_{self._placed}", "document_id": document_id, "activity_type": activity,
            "scheduled_start": start, "scheduled_end": end, "duration_minutes": duration,
        }

    def _on(self, day: date) -> list[dict]:
        return [s for s in self.sessions.values() if _day(s["scheduled_start"]) == day]

    def within_availability(self, start: str, end: str) -> bool:
        day = _day(start)
        windows = free_minutes_by_date(day, day, self.availability, [])[day]
        return any(a <= _minute(start) and _minute(end) <= b for a, b in windows)

    def overlaps(self, start: str, end: str, ignore: str | None = None) -> bool:
        return any(s["session_id"] != ignore and s["scheduled_start"] < end and s["scheduled_end"] > start
                   for s in self.sessions.values())

    def find_on(self, day: date, document_id: str, activity: str, duration: int,
                not_before: str | None = None) -> tuple[str, str] | None:
        """A slot on `day` (starting at/after `not_before`) honouring availability, busy time
        (+gap), the daily comfortable target, heavy-session and per-document limits -- or None."""
        config, sessions = self.config, self._on(day)
        if sum(s["duration_minutes"] for s in sessions) + duration > config.daily_target_minutes:
            return None
        heavy = activity in config.heavy_activities
        if heavy and sum(s["activity_type"] in config.heavy_activities for s in sessions) >= config.max_heavy_sessions_per_day:
            return None
        if sum(s["document_id"] == document_id for s in sessions) >= config.max_sessions_per_document_per_day:
            return None
        free = free_minutes_by_date(day, day, self.availability, [], now=self.now if day == self.now.date() else None)[day]
        for s in sessions:
            gap = config.heavy_gap_minutes if heavy and s["activity_type"] in config.heavy_activities else config.session_gap_minutes
            free = subtract_minute_interval(free, _minute(s["scheduled_start"]) - gap, _minute(s["scheduled_end"]) + gap)
        if not_before is not None and _day(not_before) == day:
            free = subtract_minute_interval(free, 0, _minute(not_before))
        align = config.slot_alignment_minutes
        for start, end in free:
            candidate = -(-start // align) * align
            if candidate + duration <= end:
                return _at(day, candidate), _at(day, candidate + duration)
        return None


# ---------------------------------------------------------------------------
# Reconciling one document
# ---------------------------------------------------------------------------

@dataclass
class _Outcome:
    added: list = field(default_factory=list)
    moved: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def _label(activity: str) -> str:
    return _ACTIVITY_NAMES.get(activity, activity)


def _matches(step: _Step, session: dict) -> bool:
    if step.activity_type != session["activity_type"]:
        return False
    if step.activity_type == "review":   # a final review and a spaced review are different steps
        return (step.kind == "final") == (session.get("reason") == REASON_FINAL_REVIEW)
    return True


def _search_days(ready: date, latest: date, preferred: date | None) -> list[date]:
    """Days to try: from the preferred day (the session's own day, when moving) forward, then back
    towards `ready`; otherwise simply ready..latest."""
    if latest < ready:
        return []
    days = [ready + timedelta(days=offset) for offset in range((latest - ready).days + 1)]
    if preferred is None or not ready <= preferred <= latest:
        return days
    return [d for d in days if d >= preferred] + [d for d in reversed(days) if d < preferred]


def _reconcile_document(material: MaterialContext, steps: list[_Step], future: list[dict], calendar: _Calendar,
                        outcome: _Outcome, *, can_add, cancel_unmatched: bool, strict_review_window: bool,
                        prefer_same_day: bool, replaces: dict | None, why: str,
                        config: AdaptationConfig) -> tuple[set, set]:
    """Walk the document's needed steps in order, reusing its existing future sessions. Returns
    (kept session ids, touched session ids)."""
    title = material.state.title
    deadline = date.fromisoformat(material.deadline) if material.deadline else None
    unused = sorted(future, key=lambda s: (s["scheduled_start"], s["session_id"]))
    kept, touched = set(), set()
    last_date, last_end = None, None

    for step in steps:
        ready = step.earliest if last_date is None else max(step.earliest, last_date + timedelta(days=step.gap_days))
        if last_end is not None and ready < _day(last_end):
            ready = _day(last_end)
        latest = step.latest
        match = next((s for s in unused if _matches(step, s)), None)
        if match is not None:
            unused.remove(match)
            day = _day(match["scheduled_start"])
            keep_latest = latest if deadline else max(latest, day)
            if strict_review_window and step.kind == "review":
                keep_latest = min(keep_latest, ready + timedelta(days=config.review_keep_tolerance_days))
            valid = (ready <= day <= keep_latest and (last_end is None or match["scheduled_start"] >= last_end)
                     and calendar.within_availability(match["scheduled_start"], match["scheduled_end"])
                     and not calendar.overlaps(match["scheduled_start"], match["scheduled_end"], ignore=match["session_id"]))
            if valid:
                kept.add(match["session_id"])
                last_date, last_end = day, match["scheduled_end"]
                continue
            calendar.release(match["session_id"])
            slot = _place(calendar, material.document_id, step.activity_type, match["duration_minutes"], ready, latest,
                          day if prefer_same_day else None, last_end)
            touched.add(match["session_id"])
            if slot is None:
                message = (f'The {_label(step.activity_type)} session for "{title}" on {match["scheduled_start"][:16].replace("T", " ")} '
                           f"no longer fits" + (f" before {deadline.isoformat()}" if deadline else "") + ".")
                outcome.cancelled.append(CancelledSession(match["session_id"], material.document_id, step.activity_type,
                                                          match["scheduled_start"], message))
                outcome.warnings.append({"code": "no_slot_before_deadline" if deadline else "no_available_slot",
                                         "document_id": material.document_id, "activity_type": step.activity_type})
                outcome.reasons.append(message)
                continue
            message = f'Move the {_label(step.activity_type)} session for "{title}" to {slot[0][:16].replace("T", " ")}: {why}.'
            outcome.moved.append(MovedSession(match["session_id"], material.document_id, step.activity_type,
                                              match["scheduled_start"], match["scheduled_end"], slot[0], slot[1], message))
            outcome.reasons.append(message)
            calendar.occupy(material.document_id, step.activity_type, *slot, match["duration_minutes"])
            last_date, last_end = _day(slot[0]), slot[1]
            continue
        if not can_add(step):
            continue
        slot = _place(calendar, material.document_id, step.activity_type, step.duration, ready, latest, None, last_end)
        if slot is None:
            outcome.warnings.append({"code": "no_slot_before_deadline" if deadline else "no_available_slot",
                                     "document_id": material.document_id, "activity_type": step.activity_type})
            outcome.reasons.append(f'No time left to add a {_label(step.activity_type)} session for "{title}"'
                                   + (f" before {deadline.isoformat()}" if deadline else "") + ".")
            continue
        replaced = replaces if replaces and replaces["activity_type"] == step.activity_type else None
        reason_code = REASON_RESCHEDULED if replaced else step.reason_code
        message = (f'Replace the {"skipped" if replaced and replaced.get("status") == "skipped" else "missed"} '
                   f'{_label(step.activity_type)} session for "{title}".' if replaced else step.message)
        outcome.added.append(AddedSession(material.document_id, step.activity_type, slot[0], slot[1], step.duration,
                                          reason_code, message, step.artifact_id,
                                          replaced["session_id"] if replaced else None))
        outcome.reasons.append(f"Add: {message}")
        calendar.occupy(material.document_id, step.activity_type, *slot, step.duration)
        last_date, last_end = _day(slot[0]), slot[1]

    for session in unused:
        if cancel_unmatched:
            calendar.release(session["session_id"])
            touched.add(session["session_id"])
            message = f'Cancel the {_label(session["activity_type"])} session for "{title}": {why}, it is no longer needed.'
            outcome.cancelled.append(CancelledSession(session["session_id"], material.document_id,
                                                      session["activity_type"], session["scheduled_start"], message))
            outcome.reasons.append(message)
        else:
            kept.add(session["session_id"])
    return kept, touched


def _place(calendar: _Calendar, document_id: str, activity: str, duration: int, ready: date, latest: date,
           preferred: date | None, after: str | None) -> tuple[str, str] | None:
    """The first usable slot for a step between `ready` and `latest` (never before today, never
    before the document's previous step ends)."""
    for day in _search_days(max(ready, calendar.now.date()), latest, preferred):
        slot = calendar.find_on(day, document_id, activity, duration, not_before=after)
        if slot:
            return slot
    return None


def _session_as_step(session: dict, deadline: date | None, horizon_end: date, today: date) -> _Step:
    latest = max(today, deadline - timedelta(days=1)) if deadline else max(horizon_end, _day(session["scheduled_start"]))
    return _Step(session["activity_type"], session.get("reason") or "", "", session["duration_minutes"],
                 "final" if session.get("reason") == REASON_FINAL_REVIEW else "planned", today, latest)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def propose_adaptation(context: AdaptationContext, config: AdaptationConfig = DEFAULT_ADAPTATION_CONFIG) -> AdaptationResult:
    now, utc_offset = resolve_clock(context.now, context.utc_offset)
    now = now.replace(second=0, microsecond=0)
    today, nowiso = now.date(), now.isoformat()
    scheduler = config.scheduler
    horizon_end = today + timedelta(days=scheduler.default_horizon_days)
    trigger = context.trigger
    materials = {m.document_id: m for m in context.materials}
    sessions = {s["session_id"]: s for s in context.plan_sessions}
    future = sorted((s for s in context.plan_sessions if s["status"] == "scheduled" and s["scheduled_start"] >= nowiso),
                    key=lambda s: (s["scheduled_start"], s["session_id"]))
    by_document: dict[str, list] = {}
    for session in future:
        by_document.setdefault(session["document_id"], []).append(session)

    calendar = _Calendar(context, now, scheduler)
    outcome = _Outcome()
    touched: set = set()

    def steps_for(material: MaterialContext) -> list[_Step]:
        return _build_steps(material, today, min(horizon_end, today + timedelta(days=scheduler.max_horizon_days)),
                            utc_offset, scheduler)

    def document_of(document_id: str) -> MaterialContext:
        if document_id not in materials:
            raise ValueError("That document is not part of this study plan.")
        return materials[document_id]

    if trigger.kind == "quiz_completed":
        material = document_of(trigger.document_id)
        result = material.state.quiz.latest_completed
        if result is None:
            outcome.warnings.append({"code": "no_completed_quiz", "document_id": material.document_id})
        else:
            why = f"latest quiz scored {result.percentage:g}%"
            _, changed = _reconcile_document(material, steps_for(material), by_document.get(material.document_id, []),
                                             calendar, outcome, can_add=lambda step: True, cancel_unmatched=True,
                                             strict_review_window=True, prefer_same_day=False, replaces=None, why=why,
                                             config=config)
            touched |= changed

    elif trigger.kind in ("session_missed", "session_skipped"):
        session = sessions.get(trigger.session_id)
        if session is None:
            raise ValueError("That session is not part of this study plan.")
        if trigger.kind == "session_skipped" and session["status"] != "skipped":
            raise ValueError("That session has not been skipped.")
        if trigger.kind == "session_missed" and not (
                session["status"] == "missed" or (session["status"] == "scheduled" and session["scheduled_end"] <= nowiso)):
            raise ValueError("That session is not missed: it is not a scheduled session whose time has passed.")
        material = document_of(session["document_id"])
        steps = steps_for(material)
        label = "skipped" if trigger.kind == "session_skipped" else "missed"
        if not any(step.activity_type == session["activity_type"] for step in steps):
            outcome.reasons.append(f'No replacement for the {label} {_label(session["activity_type"])} session of '
                                   f'"{material.state.title}": it is no longer needed.')
        else:
            added_once = []

            def can_add(step, activity=session["activity_type"]):
                if step.activity_type != activity or added_once:
                    return False
                added_once.append(step)
                return True

            _, changed = _reconcile_document(material, steps, by_document.get(material.document_id, []), calendar,
                                             outcome, can_add=can_add, cancel_unmatched=False,
                                             strict_review_window=False, prefer_same_day=False, replaces=session,
                                             why=f"to make room after the {label} session", config=config)
            touched |= changed
            if not added_once:
                outcome.reasons.append(f'The {label} {_label(session["activity_type"])} session of "{material.state.title}" '
                                       "is already covered by a later session.")

    elif trigger.kind == "deadline_changed":
        material = document_of(trigger.document_id)
        deadline = material.deadline
        if deadline and date.fromisoformat(deadline) < today:
            outcome.warnings.append({"code": "deadline_passed", "document_id": material.document_id, "deadline": deadline})
        else:
            why = f"the deadline is now {deadline}" if deadline else "the deadline was removed"
            _, changed = _reconcile_document(material, steps_for(material), by_document.get(material.document_id, []),
                                             calendar, outcome, can_add=lambda step: True, cancel_unmatched=True,
                                             strict_review_window=False, prefer_same_day=True, replaces=None, why=why,
                                             config=config)
            touched |= changed

    else:  # availability_changed: move only the sessions that no longer fit; nothing is added or cancelled lightly
        for document_id in sorted(by_document):
            material = materials.get(document_id)
            if material is None:
                continue
            deadline = date.fromisoformat(material.deadline) if material.deadline else None
            items = by_document[document_id]
            invalid = [s for s in items if not calendar.within_availability(s["scheduled_start"], s["scheduled_end"])
                       or calendar.overlaps(s["scheduled_start"], s["scheduled_end"], ignore=s["session_id"])]
            if not invalid:
                continue
            steps = [_session_as_step(s, deadline, horizon_end, today) for s in items]
            _, changed = _reconcile_document(material, steps, items, calendar, outcome, can_add=lambda step: False,
                                             cancel_unmatched=False, strict_review_window=False, prefer_same_day=True,
                                             replaces=None, why="its time is no longer available", config=config)
            touched |= changed

    documents = ({a.document_id for a in outcome.added} | {m.document_id for m in outcome.moved}
                 | {c.document_id for c in outcome.cancelled})
    changes = len(outcome.added) + len(outcome.moved) + len(outcome.cancelled)
    significance = ("small" if changes <= config.small_max_changes and len(documents) <= config.small_max_documents
                    else "large")
    if not changes and not outcome.reasons:
        outcome.reasons.append("No changes needed: the plan still fits.")
    return AdaptationResult(
        trigger=trigger, added=tuple(outcome.added), moved=tuple(outcome.moved), cancelled=tuple(outcome.cancelled),
        unchanged_count=sum(1 for s in future if s["session_id"] not in touched),
        warnings=tuple(outcome.warnings), significance=significance, reasons=tuple(outcome.reasons),
    )
