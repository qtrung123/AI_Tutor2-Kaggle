"""DocumentStudyState: a read-only snapshot of what one owner has already done with one document
(study pack), built only from data the app already persists, plus a small deterministic
product-level learning state derived from it. Consumed by the future Study Planner scheduler.

Deliberately conservative: a signal the app does not persist (e.g. flashcard review/mastery) is
reported as unavailable, never guessed, and "completed" is only ever reported when the learner
explicitly marked the plan material completed. No topic-level signals, no LLM.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from backend import quiz_store, study_planner_store
from backend.flashcard_service import FLASHCARD_VERSION
from backend.flashcard_store import get_latest_flashcard_set_info
from backend.indexed_document_store import get_indexed_document
from backend.summary_service import SUMMARY_VERSION
from backend.summary_store import get_latest_summary_info

# MVP learning-state thresholds on the latest completed quiz percentage (0-100) -- the only place
# these numbers live: below NEEDS_REVIEW_BELOW_PERCENT -> needs_review; at/above
# ON_TRACK_FROM_PERCENT -> on_track; in between -> learning.
NEEDS_REVIEW_BELOW_PERCENT = 60.0
ON_TRACK_FROM_PERCENT = 80.0

# Why a learning state was derived. Descriptive only -- never persisted as a session reason
# (scheduling reasons are study_scheduler_contracts.SchedulingReason).
STATE_REASON_NO_STUDY_ACTIVITY = "no_study_activity"
STATE_REASON_STUDY_STARTED = "study_started"
STATE_REASON_QUIZ_IN_PROGRESS = "quiz_in_progress"
STATE_REASON_LOW_QUIZ_SCORE = "low_quiz_score"
STATE_REASON_MODERATE_QUIZ_SCORE = "moderate_quiz_score"
STATE_REASON_STRONG_QUIZ_SCORE = "strong_quiz_score"
STATE_REASON_MARKED_COMPLETED = "marked_completed"
STATE_REASON_CODES = (
    STATE_REASON_NO_STUDY_ACTIVITY, STATE_REASON_STUDY_STARTED, STATE_REASON_QUIZ_IN_PROGRESS,
    STATE_REASON_LOW_QUIZ_SCORE, STATE_REASON_MODERATE_QUIZ_SCORE, STATE_REASON_STRONG_QUIZ_SCORE,
    STATE_REASON_MARKED_COMPLETED,
)


@dataclass(frozen=True)
class StateReason:
    """Why derive_learning_state chose a state; `metadata` carries the facts it used."""
    code: str
    message: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.code not in STATE_REASON_CODES:
            raise ValueError(f"Unknown state reason code: {self.code}")


@dataclass(frozen=True)
class SummaryState:
    available: bool
    summary_id: str | None = None
    model_id: str | None = None
    generated_at: str | None = None


@dataclass(frozen=True)
class FlashcardState:
    available: bool
    set_id: str | None = None
    card_count: int = 0
    generated_at: str | None = None
    # Flashcard reviews are not persisted anywhere yet, so review/mastery is always unavailable.
    review_data_available: bool = False
    review_stats: dict | None = None


@dataclass(frozen=True)
class QuizAttemptResult:
    attempt_id: str
    quiz_id: str
    score: int
    total: int
    percentage: float
    completed_at: str | None


@dataclass(frozen=True)
class QuizState:
    quiz_count: int = 0
    latest_quiz_id: str | None = None
    latest_quiz_title: str | None = None
    latest_quiz_question_count: int | None = None
    latest_quiz_status: str | None = None  # not_started | in_progress | completed (that quiz's own attempt)
    in_progress_count: int = 0
    completed_attempt_count: int = 0
    latest_completed: QuizAttemptResult | None = None
    last_activity_at: str | None = None


@dataclass(frozen=True)
class DocumentStudyState:
    owner_id: str
    document_id: str
    title: str
    chunk_count: int
    summary: SummaryState
    flashcards: FlashcardState
    quiz: QuizState
    completed_session_count: int
    last_session_at: str | None
    last_activity_at: str | None
    plan_id: str | None
    planner_learning_state: str | None  # stored StudyPlanMaterial.learning_state, when plan_id given
    learning_state: str  # derived: new | learning | needs_review | on_track | completed
    state_reason: StateReason

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _latest_timestamp(*values: str | None) -> str | None:
    """The chronologically latest of the given ISO timestamps (unparsable/None ignored)."""
    parsed = [(stamp, value) for value in values if (stamp := _parse_timestamp(value))]
    return max(parsed)[1] if parsed else None


def _quiz_state(owner_id: str, document_id: str) -> QuizState:
    activity = quiz_store.get_document_quiz_activity(document_id, owner_id)
    quizzes = activity["quizzes"]
    # A completed attempt with no gradable questions is no evidence either way -- ignore it.
    completed = [row for row in activity["completed_attempts"] if int(row["total"] or 0) > 0]

    def status(quiz: dict) -> str:
        # Same rule as the Quiz Library (quiz_service._quiz_variant_status).
        attempt = quiz["latest_attempt"]
        if attempt and attempt["completed"]:
            return "completed"
        if attempt and int(attempt["answered"] or 0) > 0:
            return "in_progress"
        return "not_started"

    latest_completed = None
    if completed:
        row = completed[0]
        latest_completed = QuizAttemptResult(
            attempt_id=row["attempt_id"], quiz_id=row["quiz_id"], score=int(row["score"] or 0),
            total=int(row["total"] or 0), percentage=float(row["percentage"] or 0),
            completed_at=row["completed_at"],
        )
    latest = quizzes[0] if quizzes else None
    return QuizState(
        quiz_count=len(quizzes),
        latest_quiz_id=latest["quiz_id"] if latest else None,
        latest_quiz_title=(latest["title"] or None) if latest else None,
        latest_quiz_question_count=int(latest["question_count"] or 0) if latest else None,
        latest_quiz_status=status(latest) if latest else None,
        in_progress_count=sum(1 for quiz in quizzes if status(quiz) == "in_progress"),
        completed_attempt_count=len(completed),
        latest_completed=latest_completed,
        last_activity_at=_latest_timestamp(
            *(quiz["latest_attempt"]["updated_at"] for quiz in quizzes if quiz["latest_attempt"]),
            *(row["completed_at"] for row in completed),
        ),
    )


def derive_learning_state(summary: SummaryState, flashcards: FlashcardState, quiz: QuizState,
                          completed_session_count: int, planner_learning_state: str | None) -> tuple[str, StateReason]:
    """Deterministic, first-match-wins rules (pure function):
    1. material explicitly marked completed           -> completed
    2. latest completed quiz  < NEEDS_REVIEW_BELOW_PERCENT -> needs_review
       latest completed quiz >= ON_TRACK_FROM_PERCENT      -> on_track
       otherwise                                      -> learning
    3. a quiz in progress                             -> learning
    4. any summary / flashcards / quiz / completed planner session -> learning
    5. nothing at all                                 -> new
    """
    if planner_learning_state == "completed":
        return "completed", StateReason(STATE_REASON_MARKED_COMPLETED, "You marked this document as completed.")
    result = quiz.latest_completed
    if result is not None:
        metadata = {"percentage": result.percentage, "score": result.score, "total": result.total,
                    "quiz_id": result.quiz_id, "completed_at": result.completed_at}
        score_text = f"{result.score}/{result.total} ({result.percentage:g}%)"
        if result.percentage < NEEDS_REVIEW_BELOW_PERCENT:
            return "needs_review", StateReason(STATE_REASON_LOW_QUIZ_SCORE, f"Latest quiz score {score_text} is low.", metadata)
        if result.percentage >= ON_TRACK_FROM_PERCENT:
            return "on_track", StateReason(STATE_REASON_STRONG_QUIZ_SCORE, f"Latest quiz score {score_text} is strong.", metadata)
        return "learning", StateReason(STATE_REASON_MODERATE_QUIZ_SCORE, f"Latest quiz score {score_text}; keep practicing.", metadata)
    if quiz.in_progress_count:
        return "learning", StateReason(STATE_REASON_QUIZ_IN_PROGRESS, "A quiz is in progress.",
                                  {"quiz_id": quiz.latest_quiz_id if quiz.latest_quiz_status == "in_progress" else None})
    evidence = [
        name for name, present in (
            ("summary", summary.available), ("flashcards", flashcards.available),
            ("quiz", quiz.quiz_count > 0), ("study_session", completed_session_count > 0),
        ) if present
    ]
    if evidence:
        return "learning", StateReason(STATE_REASON_STUDY_STARTED, "Study started; no completed quiz yet.", {"evidence": evidence})
    return "new", StateReason(STATE_REASON_NO_STUDY_ACTIVITY, "Not studied yet.")


def get_document_study_state(owner_id: str, document_id: str, plan_id: str | None = None) -> DocumentStudyState | None:
    """Snapshot for one of the owner's documents, or None when the owner has no such document.
    With `plan_id`, also reads that plan's stored learning_state for the document (None when the
    document is not a material of that owner's plan)."""
    document = get_indexed_document(owner_id, document_id)
    if not document:
        return None
    document_hash = str(document.get("hash") or "")
    schema_version = int(document.get("topic_schema_version") or 0)

    summary_row = get_latest_summary_info(owner_id, document_id, document_hash, schema_version, SUMMARY_VERSION)
    summary = SummaryState(
        available=True, summary_id=summary_row["summary_id"], model_id=summary_row["model_id"],
        generated_at=summary_row["created_at"],
    ) if summary_row else SummaryState(available=False)

    card_row = get_latest_flashcard_set_info(owner_id, document_id, document_hash, schema_version, FLASHCARD_VERSION)
    card_count = int(card_row["card_count"]) if card_row else 0
    flashcards = FlashcardState(
        available=card_count > 0, set_id=card_row["set_id"] if card_row else None,
        card_count=card_count, generated_at=card_row["created_at"] if card_row else None,
    )

    quiz = _quiz_state(owner_id, document_id)

    completed_sessions = study_planner_store.list_sessions(owner_id, document_id=document_id, statuses=("completed",))
    last_session_at = _latest_timestamp(*(session["completed_at"] for session in completed_sessions))

    material = study_planner_store.get_plan_material(owner_id, plan_id, document_id) if plan_id else None
    planner_learning_state = material["learning_state"] if material else None

    learning_state, state_reason = derive_learning_state(
        summary, flashcards, quiz, len(completed_sessions), planner_learning_state,
    )
    return DocumentStudyState(
        owner_id=owner_id, document_id=document_id,
        title=str(document.get("display_name") or document_id), chunk_count=int(document.get("chunks") or 0),
        summary=summary, flashcards=flashcards, quiz=quiz,
        completed_session_count=len(completed_sessions), last_session_at=last_session_at,
        last_activity_at=_latest_timestamp(last_session_at, quiz.last_activity_at),
        plan_id=plan_id if material else None, planner_learning_state=planner_learning_state,
        learning_state=learning_state, state_reason=state_reason,
    )
