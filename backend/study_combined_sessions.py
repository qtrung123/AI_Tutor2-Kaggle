"""Combined study sessions: one 30-40 minute session made of two sequential activities.

Pure rules, no persistence, no LLM. A combined session pairs two consecutive steps of one document:

    Summary + Flashcards   (learn, then practice)
    Summary + Quiz         (review, then check)
    Flashcards + Quiz      (practice, then check; a quiz retry counts as the quiz)

Persistence: a combined session is ONE study_sessions row (one calendar block, one Google event)
whose `activity_type` is its first step and whose `steps_json` lists both steps in order, each with
its own minutes and progress (pending | completed). A row without steps is a single-activity
session, exactly as before; nothing older is ever rewritten. The session is complete only after
every step is (study_planner_store.complete_session_step / transition_session).

When to combine (combine_pairs, used by the scheduler and the adaptive planner alike, so both agree):
- always: a pair that is meant to happen back to back anyway (the second step has no day gap), e.g.
  reading a new summary and then practising its flashcards;
- under time pressure only (few days left for the steps still needed, or a deadline close): a pair
  across a day gap too (practice / reading followed by the quiz), so fewer, denser sessions still fit;
- never: a quiz already in progress, the final review before a deadline, a step that is not one of
  the three pairs above (e.g. a weak-topic review), or creating flashcards and a first quiz in the
  same sitting. Pairs never overlap, so combined sessions alternate with single-activity ones.

How long (combined_minutes): 35 minutes by default, split per pair (COMBINED_SPLITS); 30 when the
learner's daily study target is small or the latest quiz was strong; 40 when the latest quiz was
weak (the first, learning part gets the extra minutes). Always within 30-40.
"""

COMBINED_PAIRS = (("summary", "flashcards"), ("summary", "quiz"), ("flashcards", "quiz"))
# (first part, second part) minutes of the default 35-minute session.
COMBINED_SPLITS = {("summary", "flashcards"): (20, 15), ("summary", "quiz"): (15, 20), ("flashcards", "quiz"): (15, 20)}
COMBINED_MIN_MINUTES = 30
COMBINED_MAX_MINUTES = 40
COMBINED_SHORT_DAILY_TARGET = 60      # a daily target at or below this keeps combined sessions at 30 minutes
COMBINED_WEAK_SCORE_BELOW = 60.0      # latest quiz below this: 40 minutes, the extra on the learning part
COMBINED_STRONG_SCORE_FROM = 80.0     # latest quiz at least this: a lighter 30 minutes
COMBINED_TIGHT_DAYS_PER_STEP = 1.5    # fewer calendar days per remaining step than this = time pressure
STEP_STATUSES = ("pending", "completed")
_NOT_COMBINED_KINDS = ("finish", "final")
ACTIVITY_LABELS = {"summary": "Summary", "flashcards": "Flashcards", "quiz": "Quiz", "quiz_retry": "Quiz retry",
                   "review": "Review", "written_quiz": "Written Quiz"}


def pair_kind(activity: str) -> str:
    """The pairing role of an activity: a quiz retry pairs like a quiz."""
    return "quiz" if activity == "quiz_retry" else activity


def can_pair(first: str, second: str) -> bool:
    return (pair_kind(first), pair_kind(second)) in COMBINED_PAIRS


def combined_minutes(first: str, second: str, *, daily_target_minutes: int, latest_percentage: float | None) -> tuple[int, int]:
    """(first part, second part) minutes of a combined session (see the module docstring)."""
    first_part, second_part = COMBINED_SPLITS[(pair_kind(first), pair_kind(second))]
    if latest_percentage is not None and latest_percentage < COMBINED_WEAK_SCORE_BELOW:
        first_part += 5
    elif daily_target_minutes <= COMBINED_SHORT_DAILY_TARGET or (
            latest_percentage is not None and latest_percentage >= COMBINED_STRONG_SCORE_FROM):
        first_part -= 5
    return first_part, second_part


def under_time_pressure(days_available: int, steps_left: int, deadline_close: bool) -> bool:
    """Few calendar days for the steps still needed, or a deadline close: combine more."""
    return deadline_close or (steps_left > 1 and days_available / steps_left < COMBINED_TIGHT_DAYS_PER_STEP)


def combine_pairs(steps: list, *, tight: bool, may_pair) -> list[tuple[int, int | None]]:
    """Which steps form combined sessions: a list of (index, partner index or None) in order.
    `steps` need .activity_type, .kind, .gap_days, .earliest, .latest and .metadata; `may_pair(a, b)`
    is the caller's extra condition. Greedy from the first step, so pairs never overlap."""
    plan, index = [], 0
    while index < len(steps):
        first = steps[index]
        second = steps[index + 1] if index + 1 < len(steps) else None
        if second is not None and _pairable(first, second, tight) and may_pair(first, second):
            plan.append((index, index + 1))
            index += 2
        else:
            plan.append((index, None))
            index += 1
    return plan


def _pairable(first, second, tight: bool) -> bool:
    if first.kind in _NOT_COMBINED_KINDS or second.kind in _NOT_COMBINED_KINDS:
        return False
    if not can_pair(first.activity_type, second.activity_type):
        return False
    if second.gap_days and not tight:
        return False
    preparations = (first.metadata.get("preparation"), second.metadata.get("preparation"))
    if preparations == ("create_flashcards", "create_quiz"):
        return False   # the quiz needs the flashcards first: never both created in one sitting
    return max(first.earliest, second.earliest) <= min(first.latest, second.latest)


# ---------------------------------------------------------------------------
# Persisted steps (study_sessions.steps_json)
# ---------------------------------------------------------------------------

def new_steps(parts) -> list[dict]:
    """The persisted steps of a new combined session from (activity_type, minutes, artifact_id) parts."""
    return [{"activity_type": activity, "duration_minutes": int(minutes), "artifact_id": artifact,
             "status": "pending", "completed_at": None} for activity, minutes, artifact in parts]


def validate_steps(steps, duration_minutes: int, activity_type: str) -> None:
    """A combined session's steps: exactly two, a known pair, whole minutes adding up to the
    session's length, the first one matching the session's activity_type."""
    if not steps:
        return
    if not isinstance(steps, list) or len(steps) != 2:
        raise ValueError("A combined session has exactly two steps.")
    for step in steps:
        if not isinstance(step, dict) or step.get("status", "pending") not in STEP_STATUSES:
            raise ValueError("Each step needs an activity and a known status.")
        minutes = step.get("duration_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("Each step needs a positive whole number of minutes.")
    if not can_pair(steps[0].get("activity_type"), steps[1].get("activity_type")):
        raise ValueError("Unknown combined session steps.")
    if steps[0]["activity_type"] != activity_type:
        raise ValueError("A combined session's activity_type is its first step.")
    if sum(step["duration_minutes"] for step in steps) != duration_minutes:
        raise ValueError("A combined session's steps must add up to its length.")


def activity_key(activity_type: str, steps=None) -> str:
    """What a session or step is, for matching: "summary", or "summary+flashcards" when combined."""
    return "+".join(step["activity_type"] for step in steps) if steps else activity_type


def activity_types(activity_type: str, steps=None) -> tuple[str, ...]:
    return tuple(step["activity_type"] for step in steps) if steps else (activity_type,)


def current_step_index(steps) -> int | None:
    """The first step not completed yet; None when every step is (or there are none)."""
    for index, step in enumerate(steps or []):
        if step.get("status") != "completed":
            return index
    return None


def in_flight_activities(document_sessions) -> tuple[str, ...]:
    """The steps still to do in combined sessions that are under way (in_progress): the learner
    continues them there, so the planner never suggests them again elsewhere."""
    return tuple(step["activity_type"] for session in document_sessions if session.get("status") == "in_progress"
                 for step in session.get("steps") or () if step.get("status") != "completed")


def keeps_single_sessions(document_sessions) -> bool:
    """A document whose plan already has sessions, none of them combined (a plan accepted before
    combined sessions existed): its later suggestions stay single-activity, so they keep matching
    the sessions it has instead of replacing them."""
    sessions = list(document_sessions)
    return bool(sessions) and not any(session.get("steps") for session in sessions)


def label(activity_type: str, steps=None) -> str:
    return " + ".join(ACTIVITY_LABELS.get(activity, activity) for activity in activity_types(activity_type, steps))
