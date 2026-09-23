"""Scheduler-facing contracts for the document-centric Study Planner.

Pure data structures only -- no persistence, no allocation heuristics, no LLM. The future
deterministic scheduler consumes a SchedulingContext and returns ProposedSessions; both sides
explain themselves with a SchedulingReason (a stable machine code plus a human-readable message).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from backend.study_planner_store import ACTIVITY_TYPES, SESSION_REASONS, validate_session

if TYPE_CHECKING:
    from backend.document_study_state import DocumentStudyState


# Scheduling reason codes -- why an activity is proposed / a session scheduled. The single source
# is study_planner_store.SESSION_REASONS (persisted in study_sessions.reason); never rename one once
# released. Learning-state explanations are a separate set (document_study_state.StateReason).
REASON_NEW_MATERIAL = "new_material"
REASON_DEADLINE_APPROACHING = "deadline_approaching"
REASON_REVIEW_DUE = "review_due"
REASON_LOW_QUIZ_SCORE = "low_quiz_score"
REASON_FLASHCARD_REVIEW_DUE = "flashcard_review_due"
REASON_FINAL_REVIEW = "final_review"
REASON_QUIZ_IN_PROGRESS = "quiz_in_progress"
REASON_RESCHEDULED = "rescheduled"
SCHEDULING_REASON_CODES = SESSION_REASONS


@dataclass(frozen=True)
class SchedulingReason:
    """Why an activity/session is proposed. `metadata` carries the facts behind the message (e.g.
    the quiz percentage or the deadline) for display and tests; only `code` is persisted."""
    code: str
    message: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.code not in SCHEDULING_REASON_CODES:
            raise ValueError(f"Unknown scheduling reason code: {self.code}")


@dataclass(frozen=True)
class CandidateActivity:
    """One thing the learner could do next for one document, before any time slot is chosen."""
    document_id: str
    activity_type: str
    reason: SchedulingReason
    artifact_id: str | None = None
    estimated_minutes: int | None = None
    deadline: str | None = None

    def __post_init__(self):
        if self.activity_type not in ACTIVITY_TYPES:
            raise ValueError(f"activity_type must be one of: {', '.join(ACTIVITY_TYPES)}.")
        if self.estimated_minutes is not None and (
            isinstance(self.estimated_minutes, bool) or not isinstance(self.estimated_minutes, int)
            or self.estimated_minutes <= 0
        ):
            raise ValueError("estimated_minutes must be a positive whole number.")


@dataclass(frozen=True)
class MaterialContext:
    """A plan material (study_plan_materials row) together with its current study state."""
    material: dict
    state: "DocumentStudyState"

    @property
    def document_id(self) -> str:
        return self.material["document_id"]

    @property
    def deadline(self) -> str | None:
        return self.material.get("deadline")


@dataclass(frozen=True)
class SchedulingContext:
    """Everything a scheduler run may read. `now` is a NAIVE LOCAL datetime (the planner's
    wall-clock convention). `availability` are availability_slots rows; `busy_sessions` are
    existing study_sessions rows (any plan) that already occupy time."""
    owner_id: str
    plan_id: str
    now: datetime
    materials: tuple[MaterialContext, ...]
    availability: tuple[dict, ...]
    busy_sessions: tuple[dict, ...]


@dataclass(frozen=True)
class ProposedSession:
    """A scheduler output, not yet persisted. Validated with the same rules as the store."""
    document_id: str
    activity_type: str
    scheduled_start: str
    scheduled_end: str
    duration_minutes: int
    reason: SchedulingReason
    artifact_id: str | None = None
    priority_snapshot: float | None = None

    def __post_init__(self):
        validate_session({**self.to_session_record(), "status": "scheduled"})

    def to_session_record(self) -> dict:
        """The dict study_planner_store.create_sessions accepts (reason persisted as its code)."""
        return {
            "document_id": self.document_id, "activity_type": self.activity_type,
            "artifact_id": self.artifact_id, "scheduled_start": self.scheduled_start,
            "scheduled_end": self.scheduled_end, "duration_minutes": self.duration_minutes,
            "reason": self.reason.code, "priority_snapshot": self.priority_snapshot,
        }


class Scheduler(Protocol):
    """What the future deterministic scheduler implements."""

    def propose(self, context: SchedulingContext) -> list[ProposedSession]: ...
