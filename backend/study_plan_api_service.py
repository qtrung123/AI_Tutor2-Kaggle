"""Study Planner v2 (document-centric) API orchestration: plan/material operations with the
validation the HTTP layer needs, and a read-only schedule preview.

The preview builds a SchedulingContext, runs the deterministic scheduler and serializes the
result without persisting anything; confirm recomputes the same schedule server-side and saves it.
"""

from datetime import date, datetime, timedelta, timezone

from backend import study_adaptation, study_planner_service, study_planner_store, study_progress
from backend.document_study_state import get_document_study_state
from backend.indexed_document_store import list_indexed_documents
from backend.study_scheduler import DEFAULT_CONFIG, find_next_slot, plan_schedule
from backend.study_scheduler_contracts import REASON_LABELS, SchedulingContext

# Real civil UTC offsets: UTC-12:00 .. UTC+14:00, always a whole number of quarter hours
# (e.g. +05:30, +05:45, +12:45).
MIN_UTC_OFFSET_MINUTES, MAX_UTC_OFFSET_MINUTES, UTC_OFFSET_STEP_MINUTES = -12 * 60, 14 * 60, 15


class PlanNotFoundError(Exception):
    """Unknown or foreign plan / material / document (HTTP 404)."""


class PlanConflictError(Exception):
    """The request conflicts with existing state, e.g. a duplicate material (HTTP 409)."""


class PlanValidationError(ValueError):
    """A request that cannot be served as-is, with a machine-readable payload (HTTP 400)."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.payload = {"code": code, "message": message, **details}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_plan(owner_id: str, plan_id: str) -> dict:
    plan = study_planner_store.get_plan(owner_id, plan_id)
    if not plan:
        raise PlanNotFoundError("Study plan not found.")
    return plan


def _require_material(owner_id: str, plan_id: str, material_id: str) -> dict:
    _require_plan(owner_id, plan_id)
    material = study_planner_store.get_material(owner_id, material_id)
    if not material or material["plan_id"] != plan_id:
        raise PlanNotFoundError("Plan material not found.")
    return material


def _validate_deadline(deadline: str | None) -> None:
    """Format only. Whether a deadline has passed depends on the learner's local date, which is
    known only at preview time -- so that check lives in preview_plan, not here."""
    if deadline is None:
        return
    try:
        date.fromisoformat(str(deadline))
    except ValueError as error:
        raise ValueError("deadline must be an ISO date (YYYY-MM-DD).") from error


def _validate_utc_offset(utc_offset_minutes: int) -> timedelta:
    minutes = int(utc_offset_minutes)
    if not MIN_UTC_OFFSET_MINUTES <= minutes <= MAX_UTC_OFFSET_MINUTES or minutes % UTC_OFFSET_STEP_MINUTES:
        raise PlanValidationError(
            "invalid_utc_offset",
            "utc_offset_minutes must be a real UTC offset: -720..840 in steps of 15 minutes.",
            utc_offset_minutes=minutes,
        )
    return timedelta(minutes=minutes)


def _document_titles(owner_id: str) -> dict[str, str]:
    return {doc["document_id"]: doc["display_name"] or doc["document_id"] for doc in list_indexed_documents(owner_id)}


def _with_titles(owner_id: str, materials: list[dict]) -> list[dict]:
    titles = _document_titles(owner_id)
    return [{**material, "document_title": titles.get(material["document_id"])} for material in materials]


# -- plans --------------------------------------------------------------------

def get_plan_detail(owner_id: str, plan_id: str) -> dict:
    plan = _require_plan(owner_id, plan_id)
    return {**plan, "materials": list_plan_materials(owner_id, plan_id)}


def update_plan(owner_id: str, plan_id: str, changes: dict) -> dict:
    _require_plan(owner_id, plan_id)
    return study_planner_store.update_plan(owner_id, plan_id, changes)


def delete_plan(owner_id: str, plan_id: str) -> None:
    _require_plan(owner_id, plan_id)
    study_planner_store.delete_plan(owner_id, plan_id)


# -- materials ----------------------------------------------------------------

def list_plan_materials(owner_id: str, plan_id: str) -> list[dict]:
    _require_plan(owner_id, plan_id)
    return _with_titles(owner_id, study_planner_store.list_materials(owner_id, plan_id))


def add_plan_material(owner_id: str, plan_id: str, document_id: str, deadline: str | None = None,
                      familiarity: str | None = None) -> dict:
    _require_plan(owner_id, plan_id)
    _validate_deadline(deadline)
    try:
        material = study_planner_service.add_plan_material(owner_id, plan_id, document_id, deadline, familiarity)
    except ValueError as error:
        if str(error) == "Document not found.":
            raise PlanNotFoundError(str(error)) from error
        if "already part of the study plan" in str(error):
            raise PlanConflictError(str(error)) from error
        raise
    return _with_titles(owner_id, [material])[0]


def update_plan_material(owner_id: str, plan_id: str, material_id: str, changes: dict) -> dict:
    """`changes` holds only the fields the caller sent; an explicit None clears that field."""
    _require_material(owner_id, plan_id, material_id)
    if "deadline" in changes:
        _validate_deadline(changes["deadline"])
    material = study_planner_store.update_material(owner_id, material_id, changes)
    return _with_titles(owner_id, [material])[0]


def remove_plan_material(owner_id: str, plan_id: str, material_id: str) -> None:
    _require_material(owner_id, plan_id, material_id)
    study_planner_store.remove_material(owner_id, material_id)


# -- preview ------------------------------------------------------------------

def _reason(reason) -> dict:
    return {"code": reason.code, "message": reason.message}


def _parse_local_now(local_now: str | None, offset: timedelta) -> datetime:
    """Optional NAIVE learner-local ISO datetime override. utc_offset_minutes is the only timezone
    input, so an offset-bearing value is rejected rather than silently reconciled."""
    if local_now is None:
        return (_utc_now() + offset).replace(tzinfo=None, microsecond=0)
    try:
        parsed = datetime.fromisoformat(str(local_now))
    except ValueError as error:
        raise PlanValidationError("invalid_local_now", "local_now must be an ISO datetime.",
                                  local_now=local_now) from error
    if parsed.tzinfo is not None:
        raise PlanValidationError(
            "invalid_local_now",
            "local_now must be a naive local datetime without a timezone; send the offset in utc_offset_minutes.",
            local_now=local_now,
        )
    return parsed


def _schedule_plan(owner_id: str, plan_id: str, utc_offset_minutes: int, local_now: str | None):
    """Shared by preview and confirm: validate the learner time context and the plan, then run the
    deterministic scheduler. Returns (local_now, offset, result, warnings, titles)."""
    offset = _validate_utc_offset(utc_offset_minutes)
    local_now = _parse_local_now(local_now, offset)
    materials = study_planner_store.list_materials(owner_id, plan_id)
    if not materials:
        raise PlanValidationError("no_materials", "Add at least one document to the plan before previewing.")
    # Strictly before the learner's local date; a deadline of "today" passes validation and the
    # scheduler/capacity report decides what can still fit.
    past = [m for m in materials if m["deadline"] and date.fromisoformat(m["deadline"]) < local_now.date()]
    if past:
        titles = _document_titles(owner_id)
        raise PlanValidationError(
            "deadline_passed",
            "Some deadlines have already passed. Update or clear them, then preview again.",
            local_date=local_now.date().isoformat(),
            documents=[{"material_id": m["material_id"], "document_id": m["document_id"],
                        "document_title": titles.get(m["document_id"]), "deadline": m["deadline"]} for m in past],
        )

    context = study_planner_service.build_scheduling_context(owner_id, plan_id, now=local_now, utc_offset=offset)
    result = plan_schedule(context)

    titles = {material.document_id: material.state.title for material in context.materials}
    warnings = []
    if not context.availability:
        warnings.append({"code": "no_availability"})
    for material in materials:
        if material["document_id"] not in titles:
            warnings.append({"code": "document_missing", "document_id": material["document_id"]})
    return local_now, offset, result, warnings, titles


def _capacity(capacity, titles: dict) -> dict:
    return {
        "status": capacity.status,
        "required_minutes": capacity.required_minutes,
        "scheduled_minutes": capacity.scheduled_minutes,
        "schedulable_minutes": capacity.schedulable_minutes,
        "available_minutes": capacity.available_minutes,
        "shortfall_minutes": capacity.shortfall_minutes,
        "unscheduled": [
            {
                "document_id": c.document_id, "document_title": titles.get(c.document_id),
                "activity_type": c.activity_type, "estimated_minutes": c.estimated_minutes,
                "deadline": c.deadline, "reason": _reason(c.reason), "artifact_id": c.artifact_id,
            }
            for c in capacity.unscheduled
        ],
    }


def _saved_session(session: dict, titles: dict) -> dict:
    """A persisted study session for the UI (reason code + its label; never the priority score)."""
    code = session["reason"]
    return {
        "session_id": session["session_id"], "document_id": session["document_id"],
        "document_title": titles.get(session["document_id"]), "activity_type": session["activity_type"],
        "scheduled_start": session["scheduled_start"], "scheduled_end": session["scheduled_end"],
        "duration_minutes": session["duration_minutes"], "status": session["status"],
        "reason": {"code": code, "message": REASON_LABELS.get(code, "")} if code else None,
        "artifact_id": session["artifact_id"], "started_at": session["started_at"],
        "completed_at": session["completed_at"], "rescheduled_from": session["rescheduled_from"],
    }


def preview_plan(owner_id: str, plan_id: str, utc_offset_minutes: int, local_now: str | None = None) -> dict:
    """Run the deterministic scheduler for one plan WITHOUT saving anything.

    The learner's timezone comes only from `utc_offset_minutes` (required, a real civil offset).
    `local_now` is an optional naive local wall-clock override (deterministic/tests); when omitted,
    planner-local now is the current UTC instant shifted by that offset -- never the server's
    timezone. Insufficient capacity is a normal result (status "at_risk"); a deadline already past
    on the learner's local date is a validation error (PlanValidationError "deadline_passed").
    """
    _require_plan(owner_id, plan_id)
    local_now, offset, result, warnings, titles = _schedule_plan(owner_id, plan_id, utc_offset_minutes, local_now)
    return {
        "plan_id": plan_id,
        "persisted": False,
        "local_now": local_now.isoformat(),
        "utc_offset_minutes": int(offset.total_seconds() // 60),
        "sessions": [
            {
                "document_id": p.document_id, "document_title": titles.get(p.document_id),
                "activity_type": p.activity_type, "scheduled_start": p.scheduled_start,
                "scheduled_end": p.scheduled_end, "duration_minutes": p.duration_minutes,
                "reason": _reason(p.reason), "artifact_id": p.artifact_id,
            }
            for p in result.proposals
        ],
        "capacity": _capacity(result.capacity, titles),
        "warnings": warnings,
    }


def confirm_plan(owner_id: str, plan_id: str, utc_offset_minutes: int, local_now: str | None = None) -> dict:
    """Recompute the schedule server-side (the client never sends sessions) and save it: every
    proposed session plus one schedule_run, atomically. A partial (at_risk) plan may be confirmed
    as long as something can be scheduled; a plan that already has active sessions is a conflict."""
    _require_plan(owner_id, plan_id)
    if study_planner_store.count_active_plan_sessions(owner_id, plan_id):
        raise PlanConflictError("This plan is already confirmed.")
    local_now, offset, result, warnings, titles = _schedule_plan(owner_id, plan_id, utc_offset_minutes, local_now)
    if not result.proposals:
        raise PlanValidationError(
            "nothing_to_schedule",
            "Nothing can be scheduled with the current availability and deadlines.",
            capacity=_capacity(result.capacity, titles),
        )
    try:
        saved = study_planner_store.confirm_plan_sessions(
            owner_id, plan_id, [p.to_session_record() for p in result.proposals], reason="confirm",
        )
    except study_planner_store.PlanAlreadyConfirmedError as error:
        raise PlanConflictError("This plan is already confirmed.") from error
    return {
        "plan_id": plan_id,
        "persisted": True,
        "schedule_run_id": saved["schedule_run"]["schedule_run_id"],
        "local_now": local_now.isoformat(),
        "utc_offset_minutes": int(offset.total_seconds() // 60),
        "sessions": [_saved_session(session, titles) for session in saved["sessions"]],
        "capacity": _capacity(result.capacity, titles),
        "warnings": warnings,
    }


class SessionConflictError(Exception):
    """A lifecycle action the session cannot take in its current state (HTTP 409, machine-readable payload)."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.payload = {"code": code, "message": message, **details}


# Which study tool each activity opens. review is resolved from the document's state.
ACTIVITY_TOOLS = {"summary": "summary", "flashcards": "flashcards", "quiz": "quiz", "quiz_retry": "quiz"}


def _session_tool(owner_id: str, session: dict) -> tuple[str, bool]:
    """(tool, artifact_available) for a session. review prefers the flashcards (spaced review of
    the key points), then the summary, then a quiz the learner already has; with none of them it
    opens Flashcards, whose empty state offers to create the set."""
    state = get_document_study_state(owner_id, session["document_id"])
    has_summary = bool(state and state.summary.available)
    has_cards = bool(state and state.flashcards.available and state.flashcards.card_count)
    has_quiz = bool(state and state.quiz.quiz_count)
    activity = session["activity_type"]
    if activity == "review":
        if has_cards:
            return "flashcards", True
        if has_summary:
            return "summary", True
        if has_quiz:
            return "quiz", True
        return "flashcards", False
    tool = ACTIVITY_TOOLS[activity]
    return tool, {"summary": has_summary, "flashcards": has_cards, "quiz": has_quiz}[tool]


def _transition(owner_id: str, session_id: str, action: str) -> tuple[dict, bool]:
    try:
        return study_planner_store.transition_session(owner_id, session_id, action)
    except study_planner_store.SessionTransitionError as error:
        raise SessionConflictError(error.code, str(error), status=error.status) from error
    except ValueError as error:
        raise PlanNotFoundError(str(error)) from error


def start_session(owner_id: str, session_id: str) -> dict:
    """Start (or resume) one session and say which tool to open for it."""
    session, started = _transition(owner_id, session_id, "start")
    tool, artifact_available = _session_tool(owner_id, session)
    return {"session": _saved_session(session, _document_titles(owner_id)), "started": started,
            "tool": tool, "artifact_available": artifact_available}


def complete_session(owner_id: str, session_id: str) -> dict:
    """in_progress -> completed (completed_at stamped once); completing again is a no-op."""
    session, changed = _transition(owner_id, session_id, "complete")
    return {"session": _saved_session(session, _document_titles(owner_id)), "changed": changed}


def skip_session(owner_id: str, session_id: str) -> dict:
    """scheduled/in_progress -> skipped. The row is kept as history; skipping again is a no-op."""
    session, changed = _transition(owner_id, session_id, "skip")
    return {"session": _saved_session(session, _document_titles(owner_id)), "changed": changed}


def reschedule_session(owner_id: str, session_id: str, utc_offset_minutes: int, local_now: str | None = None) -> dict:
    """Move one session to the first usable slot: inside the learner's availability, clear of busy
    sessions, from now (or, for a session still ahead, after its current end) up to the material's
    deadline. Only when nothing fits by the deadline -- or the deadline has already passed -- is a
    slot after it used (after_deadline=true), within the default planning horizon. Nothing else in
    the plan moves."""
    session = study_planner_store.get_session(owner_id, session_id)
    if not session:
        raise PlanNotFoundError("Study session not found.")
    offset = _validate_utc_offset(utc_offset_minutes)
    now = _parse_local_now(local_now, offset)
    material = study_planner_store.get_plan_material(owner_id, session["plan_id"], session["document_id"])
    deadline = date.fromisoformat(material["deadline"]) if material and material["deadline"] else None
    context = SchedulingContext(
        owner_id=owner_id, plan_id=session["plan_id"], now=now, utc_offset=offset, materials=(),
        availability=tuple(study_planner_store.list_availability(owner_id)),
        busy_sessions=tuple(s for s in study_planner_store.list_busy_sessions(owner_id, start_from=now.date().isoformat())
                            if s["session_id"] != session_id),
    )
    scheduled_end = datetime.fromisoformat(session["scheduled_end"])
    earliest = scheduled_end if scheduled_end > now else None
    horizon = now.date() + timedelta(days=DEFAULT_CONFIG.default_horizon_days)
    slot, after_deadline = None, False
    if deadline is None or deadline >= now.date():
        slot = find_next_slot(context, session["duration_minutes"], deadline or horizon, earliest)
    if slot is None and deadline is not None:
        slot = find_next_slot(context, session["duration_minutes"], max(horizon, deadline), earliest)
        after_deadline = slot is not None
    if slot is None:
        raise SessionConflictError(
            "no_available_slot",
            "No free study time is left" + (" before the deadline" if deadline else "")
            + ". Add availability, then try again.", status=session["status"])
    try:
        previous, moved = study_planner_store.reschedule_session(owner_id, session_id, *slot)
    except study_planner_store.SessionTransitionError as error:
        raise SessionConflictError(error.code, str(error), status=error.status) from error
    titles = _document_titles(owner_id)
    return {"session": _saved_session(moved, titles), "previous": _saved_session(previous, titles),
            "after_deadline": after_deadline}


def _compute_adaptation(owner_id: str, plan_id: str, trigger: dict, utc_offset_minutes: int,
                        local_now: str | None):
    """(result, local now, the plan sessions it was computed from) -- persisted state only."""
    plan = _require_plan(owner_id, plan_id)
    if plan["status"] != "active":
        raise PlanValidationError("plan_not_active", "Only an active plan can be adapted.")
    offset = _validate_utc_offset(utc_offset_minutes)
    now = _parse_local_now(local_now, offset)
    try:
        parsed = study_adaptation.AdaptationTrigger(**trigger)
    except (TypeError, ValueError) as error:
        raise PlanValidationError("invalid_trigger", str(error)) from error
    context = study_planner_service.build_scheduling_context(owner_id, plan_id, now=now, utc_offset=offset)
    plan_sessions = study_planner_store.list_sessions(owner_id, plan_id=plan_id)
    # Exact plan isolation: a trigger may only name this plan's own documents and sessions.
    if parsed.document_id and parsed.document_id not in {m.document_id for m in context.materials}:
        raise PlanNotFoundError("That document is not part of this study plan.")
    if parsed.session_id and parsed.session_id not in {s["session_id"] for s in plan_sessions}:
        raise PlanNotFoundError("That session is not part of this study plan.")
    try:
        result = study_adaptation.propose_adaptation(study_adaptation.AdaptationContext(
            trigger=parsed, now=now, utc_offset=offset, materials=context.materials,
            plan_sessions=tuple(plan_sessions), availability=context.availability,
            busy_sessions=context.busy_sessions,
        ))
    except ValueError as error:
        raise PlanValidationError("invalid_trigger", str(error)) from error
    return result, now, plan_sessions


def propose_adaptation(owner_id: str, plan_id: str, trigger: dict, utc_offset_minutes: int,
                       local_now: str | None = None) -> dict:
    """Read-only adaptive replanning proposal for one active plan (Phase 5A): what should change in
    its future sessions after `trigger`. Reads persisted state only and never writes."""
    result, now, _ = _compute_adaptation(owner_id, plan_id, trigger, utc_offset_minutes, local_now)
    return {"plan_id": plan_id, "persisted": False, "local_now": now.isoformat(), **result.to_dict()}


def apply_adaptation(owner_id: str, plan_id: str, trigger: dict, utc_offset_minutes: int,
                     local_now: str | None = None, confirm: bool = False) -> dict:
    """Recompute the proposal for `trigger` server-side (the client never sends changes) and apply
    it atomically. A large proposal is only applied with confirm=true; otherwise -- and when there
    is nothing to change -- it is returned with applied=false and nothing is written."""
    result, now, plan_sessions = _compute_adaptation(owner_id, plan_id, trigger, utc_offset_minutes, local_now)
    proposal = result.to_dict()
    base = {"plan_id": plan_id, "local_now": now.isoformat(), **proposal}
    if not result.change_count:
        return {**base, "applied": False, "requires_confirmation": False, "sessions": []}
    if result.significance == "large" and not confirm:
        return {**base, "applied": False, "requires_confirmation": True, "sessions": []}
    by_id = {s["session_id"]: s for s in plan_sessions}
    added = [{"document_id": a.document_id, "activity_type": a.activity_type, "scheduled_start": a.scheduled_start,
              "scheduled_end": a.scheduled_end, "duration_minutes": a.duration_minutes, "reason": a.reason_code,
              "artifact_id": a.artifact_id, "rescheduled_from": a.replaces_session_id} for a in result.added]
    replaced = sorted({a.replaces_session_id for a in result.added if a.replaces_session_id
                       and by_id[a.replaces_session_id]["status"] in ("scheduled", "missed")})
    try:
        written = study_planner_store.apply_adaptation_changes(
            owner_id, plan_id, study_planner_store.plan_sessions_fingerprint(plan_sessions), now.isoformat(),
            added=added, moved=[(m.session_id, m.to_start, m.to_end) for m in result.moved],
            cancelled=[c.session_id for c in result.cancelled], replaced=replaced,
        )
    except study_planner_store.SessionTransitionError as error:
        raise SessionConflictError(error.code, str(error), status=error.status) from error
    for row, new_id in zip(proposal["added"], written["added"]):
        row["session_id"] = new_id
    for row in proposal["moved"]:
        row["new_session_id"] = written["moved"][row["session_id"]]
    affected = (written["added"] + [m.session_id for m in result.moved] + list(written["moved"].values())
                + [c.session_id for c in result.cancelled] + replaced)
    titles = _document_titles(owner_id)
    refreshed = sorted((study_planner_store.get_session(owner_id, session_id) for session_id in affected),
                       key=lambda s: (s["scheduled_start"], s["session_id"]))
    return {**base, **proposal, "applied": True, "requires_confirmation": False,
            "sessions": [_saved_session(s, titles) for s in refreshed]}


def document_progress(owner_id: str, document_id: str, utc_offset_minutes: int, local_now: str | None = None) -> dict:
    """Read-only learner progress for one owned document (Phase 6A)."""
    now = _parse_local_now(local_now, _validate_utc_offset(utc_offset_minutes))
    progress = study_progress.document_progress(owner_id, document_id, now)
    if progress is None:
        raise PlanNotFoundError("Document not found.")
    return progress


def plan_progress(owner_id: str, plan_id: str, utc_offset_minutes: int, local_now: str | None = None) -> dict:
    """Read-only aggregate progress for one owned plan (Phase 6A)."""
    now = _parse_local_now(local_now, _validate_utc_offset(utc_offset_minutes))
    progress = study_progress.plan_progress(owner_id, plan_id, now)
    if progress is None:
        raise PlanNotFoundError("Study plan not found.")
    return progress


def list_plan_sessions(owner_id: str, plan_id: str) -> list[dict]:
    """The plan's persisted sessions (the confirmed weekly plan), in time order."""
    _require_plan(owner_id, plan_id)
    titles = _document_titles(owner_id)
    return [_saved_session(s, titles) for s in study_planner_store.list_sessions(owner_id, plan_id=plan_id)]
