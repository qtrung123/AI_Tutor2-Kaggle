"""Deterministic Study Planner scheduler (MVP): SchedulingContext -> ScheduleResult.

Pure and LLM-free. Same input, same output. Flow:
  1. per document, turn its DocumentStudyState into an ordered queue of steps (candidate
     activities with earliest/latest dates and minimum spacing between them);
  2. pace each document's steps (new material, recall practice, quiz follow-ups) evenly over its
     usable days (up to the final review window, the soft target date, or a week for flexible
     learning), staggered across documents;
  3. walk the horizon day by day; on each day repeatedly pick the highest-priority ready step --
     ready = due, at or past its pace day, and not a second session of that document that day
     while later days remain -- and place it in the learner's free availability (Google busy time
     and accepted sessions already removed), within a comfortable daily budget. A document that
     would run short of time before its deadline ignores pacing and is placed as early as possible;
  4. report whatever did not fit as a capacity shortfall instead of packing time harder. Should
     pacing ever leave more unscheduled than earliest-first placement, the latter is returned.

Consecutive steps of a document may form ONE combined 30-40 minute session of two sequential
activities (Summary + Flashcards, Summary + Quiz, Flashcards + Quiz) -- see study_combined_sessions
for when and how long. A combined step is placed like any other step.

Every tunable number lives in SchedulerConfig.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from backend import study_combined_sessions as combined
from backend.study_session_duration import MIN_SESSION_MINUTES, at_least_minimum
from backend.study_time import at_minute, end_minute, free_minutes_by_date, subtract_minute_interval, to_minutes
from backend.study_scheduler_contracts import (
    REASON_DEADLINE_APPROACHING, REASON_FINAL_REVIEW, REASON_LOW_QUIZ_SCORE, REASON_NEW_MATERIAL,
    REASON_QUIZ_IN_PROGRESS, REASON_RETRIEVAL_PRACTICE, REASON_REVIEW_DUE, CandidateActivity, CapacityReport, MaterialContext,
    ProposedSession, ScheduleResult, SchedulingContext, SchedulingReason,
)


@dataclass(frozen=True)
class SchedulerConfig:
    # Base priority = weighted sum of four [0, 1] dimensions (see base_priority). The only weights.
    weight_deadline: float = 0.35
    weight_review: float = 0.35
    weight_performance: float = 0.20
    weight_workload: float = 0.10
    urgency_horizon_days: int = 14        # deadline urgency falls linearly to 0 at this distance
    # Guardrails, added on top of the base score (see _guardrails); they never replace it.
    # Fairness: nearest deadline wins, but nothing starves.
    same_day_penalty: float = 0.15        # per session a document already has that day
    starvation_days: int = 3              # a document idle this long gets a boost
    starvation_boost: float = 0.15
    # Feasibility: a deadlined document whose remaining minutes need at least this share of the
    # comfortable capacity left before its latest study day...
    pressure_threshold: float = 0.5
    pressure_boost: float = 0.5
    # ...or whose remaining steps need (because of day gaps) every usable day it has left.
    critical_boost: float = 1.0
    # Today is the last day with room for the material's next step before its deadline: it goes
    # ahead of a material that can still wait (a later deadline never takes an urgent one's last day).
    last_chance_boost: float = 1.0
    # Review urgency per step kind (review steps also gain per overdue day).
    review_urgency: dict = field(default_factory=lambda: {
        "learn": 0.3, "finish": 0.9, "review": 0.7, "final": 1.0,
    })
    review_urgency_per_overdue_day: float = 0.1
    # Performance need by the reader's state reason.
    performance_need: dict = field(default_factory=lambda: {
        "low_quiz_score": 1.0, "moderate_quiz_score": 0.6, "quiz_in_progress": 0.5, "study_started": 0.5,
        "no_study_activity": 0.5, "strong_quiz_score": 0.2, "marked_completed": 0.1,
    })
    # Spaced review: (latest quiz percentage strictly below, days until next review).
    review_intervals: tuple = ((50.0, 1), (75.0, 2), (85.0, 3), (math.inf, 5))
    low_score_retry_gap_days: int = 1     # low score: flashcards, then quiz_retry this many days later
    # A low score strictly below this is very weak: relearn the foundations (re-read the summary,
    # practice flashcards) before any reassessment.
    foundational_relearn_below: float = 40.0
    moderate_retry_gap_days: int = 2      # moderate score: review, then quiz_retry this many days later
    quiz_gap_after_learning_days: int = 1  # the first quiz never lands the same day as new material
    # Flexible material (no deadline): a review of the key points this many days after the first
    # quiz, so the week ends Summary -> Flashcards -> Quiz -> Review. A deadline's final review
    # plays that role instead.
    post_quiz_review_gap_days: int = 2
    final_review_window_days: int = 2     # final review within this many days before the deadline
    # A strong score (>= 80%) gets a second, lighter spaced review this many intervals after the first.
    second_review_interval_factor: int = 2
    weak_topics_in_message: int = 3       # weak topic names quoted in a session message
    # A quiz/quiz_retry inside the final-review window already is final retrieval: a separate final
    # review is only added on a LATER day, and is dropped (satisfied) when no later slot exists.
    retrieval_activities: tuple = ("quiz", "quiz_retry")
    deadline_approaching_days: int = 3    # new material this close to its deadline says so
    default_horizon_days: int = 14        # horizon for plans without deadlines
    max_horizon_days: int = 42
    # Length of a NEW session per activity, in minutes -- the single source for these numbers
    # (Study Preferences will override this dict). "review" is a weak-topic / spaced review;
    # "final_review" the review right before a deadline; "light_review" the lighter spaced
    # repetition after a strong score. Never below MIN_SESSION_MINUTES (20).
    # Existing sessions keep their own saved length: they are never resized from here.
    durations: dict = field(default_factory=lambda: {
        "summary": 45, "flashcards": 20, "quiz": 35, "quiz_retry": 35, "review": 30,
        "written_quiz": 20, "final_review": 45, "light_review": 20,
    })
    # Daily shape: availability is permission, not obligation.
    daily_target_minutes: int = 90
    # Break between two consecutive study sessions (also kept around accepted sessions).
    session_gap_minutes: int = 20
    heavy_gap_minutes: int = 30           # longer break between two heavy sessions on the same day
    heavy_activities: tuple = ("summary", "quiz", "quiz_retry")
    max_heavy_sessions_per_day: int = 2
    max_sessions_per_document_per_day: int = 2
    slot_alignment_minutes: int = 5
    # Distribution (see _pace_days): new material is paced evenly over the usable days before its
    # pacing end instead of filling the earliest free slots. The end is the day before the final
    # review window (hard deadline), the target completion date when sooner (soft), or this many
    # days for flexible learning. Materials are staggered so they do not all land on the same days.
    spread_sessions: bool = True
    # False ("Deadline Priority" in Study Preferences): documents with a hard deadline are not paced
    # -- they go as early as possible -- while flexible ones stay balanced.
    pace_deadline_materials: bool = True
    flexible_spread_days: int = 7
    # A target completion date (soft) adds this share of deadline-style urgency as it approaches.
    target_urgency_weight: float = 0.5
    # Combined sessions (study_combined_sessions): two sequential activities in one 30-40 minute
    # session, alternating with single-activity sessions. The engine's base rules are single-activity
    # (False); the Study Planner itself runs with it switched on (study_preferences.PLANNER_CONFIG).
    combine_sessions: bool = False
    # When the final review window has no usable day at all, it moves to the last usable day
    # before it, if that day is at most this many days before the deadline (never on/after it).
    final_review_fallback_days: int = 4


DEFAULT_CONFIG = SchedulerConfig()

_ACTIVITY_LABELS = {
    "summary": "read the summary", "flashcards": "practice the flashcards", "quiz": "take a quiz",
    "review": "review the key points", "quiz_retry": "retake a quiz",
    "written_quiz": "practice recall with a written quiz from your flashcards",
}


@dataclass
class _Step:
    activity_type: str
    reason_code: str
    message: str
    duration: int
    kind: str                 # learn | finish | review | final
    earliest: date
    latest: date
    gap_days: int = 0         # minimum days after the previous placed step of the same document
    artifact_id: str | None = None
    metadata: dict = field(default_factory=dict)   # facts behind the step (score, weak topics, preparation)
    # A combined step: its two activities in order, as (activity_type, minutes, artifact_id); () = single.
    parts: tuple = ()

    @property
    def activities(self) -> tuple[str, ...]:
        return tuple(part[0] for part in self.parts) or (self.activity_type,)

    @property
    def key(self) -> str:
        """What the step is, for matching sessions and candidates ("summary+flashcards" when combined)."""
        return "+".join(self.activities)

    def steps(self) -> tuple:
        """The persisted steps of a combined session made from this step (() when single)."""
        return tuple(combined.new_steps(self.parts)) if self.parts else ()


@dataclass
class _DocumentPlan:
    context: MaterialContext
    deadline: date | None
    steps: list
    index: int = 0
    last_date: date | None = None
    last_activity: str | None = None
    sessions_by_day: dict = field(default_factory=dict)
    target: date | None = None          # soft target completion date (study goal), never a constraint
    pace: dict = field(default_factory=dict)   # step index -> earliest preferred day (learn steps)
    spread_end: date | None = None      # last day the material's steps are meant to spread over

    @property
    def current(self) -> _Step | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def remaining_minutes(self) -> int:
        return sum(step.duration for step in self.steps[self.index:])

    def ready_date(self, config: "SchedulerConfig") -> date:
        step = self.current
        if self.last_date is None:
            return step.earliest
        gap = step.gap_days
        if step.kind == "final" and self.last_activity in config.retrieval_activities:
            gap = max(gap, 1)  # never a same-day final review right after a quiz
        return max(step.earliest, self.last_date + timedelta(days=gap))

    def final_satisfied_by_retrieval(self, config: "SchedulerConfig") -> bool:
        """The pending final review is covered by a quiz/quiz_retry already placed in its window."""
        step = self.current
        return (step is not None and step.kind == "final" and self.last_activity in config.retrieval_activities
                and self.last_date is not None and self.last_date >= step.earliest)


# ---------------------------------------------------------------------------
# Durations and spacing
# ---------------------------------------------------------------------------

def estimate_duration(activity_type: str, state=None, config: SchedulerConfig = DEFAULT_CONFIG,
                      final: bool = False, variant: str | None = None) -> int:
    """Minutes for a NEW session of this activity (config.durations; `final` = the final review
    before a deadline, `variant` another named length such as "light_review"), never below the
    minimum session length. The same for every document: `state` is accepted for callers' convenience."""
    return at_least_minimum(config.durations["final_review" if final else variant or activity_type])


def review_interval_days(percentage: float, config: SchedulerConfig = DEFAULT_CONFIG) -> int:
    for below, days in config.review_intervals:
        if percentage < below:
            return days
    return config.review_intervals[-1][1]


def resolve_clock(now: datetime, utc_offset: timedelta | None) -> tuple[datetime, timedelta | None]:
    """(planner-local naive now, learner UTC offset or None). An explicit `utc_offset` wins; else a
    timezone-aware `now` supplies its own offset; a naive `now` without an offset yields None --
    the learner's timezone is then UNKNOWN and is never inferred from the server clock/timezone."""
    if now.tzinfo is not None:
        offset = utc_offset if utc_offset is not None else now.utcoffset()
        return now.astimezone(timezone(offset)).replace(tzinfo=None), offset
    return now, utc_offset


def local_date(timestamp: str | None, utc_offset: timedelta | None, fallback: date) -> date:
    """Planner-calendar date of a stored artifact timestamp. Artifact timestamps stay aware UTC (a
    naive one is read as UTC, like document_study_state does) and are shifted by the learner's
    `utc_offset` when it is known. When it is unknown (None), the neutral fallback is the UTC
    calendar date itself -- the storage frame, explicitly NOT a guess at the learner's timezone,
    and at most one day away from the learner's own date."""
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if utc_offset is None:
        return parsed.astimezone(timezone.utc).date()
    return parsed.astimezone(timezone(utc_offset)).date()


# ---------------------------------------------------------------------------
# 1. Candidate selection per document
# ---------------------------------------------------------------------------

def _build_steps(material: MaterialContext, today: date, horizon_end: date, utc_offset: timedelta | None,
                 config: SchedulerConfig) -> list[_Step]:
    """The ordered steps one document still needs, from its persisted study state only (never an
    invented score or topic). Learning states, first match wins:

    - marked completed           -> only the final review before a deadline;
    - quiz in progress           -> finish that quiz;
    - quiz attempted, < 40%      -> relearn the foundations first: re-read the summary (generated
                                    when missing), then flashcard review + flashcard recall practice
                                    (or create flashcards when there is no set), then a quiz retry;
    - quiz attempted, 40-59%     -> weak-topic review, flashcard recall practice (with flashcards),
                                    then a quiz retry on a later day;
    - quiz attempted, 60-79%     -> review of the incorrect topics, practice, then reassessment;
    - quiz attempted, >= 80%     -> lighter spaced repetition (a weak-topic review instead when
                                    topic mastery still shows a clearly weak topic);
    - quiz created, not attempted -> read the summary / practice the flashcards not studied yet,
                                    then the initial quiz;
    - no quiz yet                -> learn the material first (summary, then flashcards), then create
                                    a first quiz in the Quiz tab and take it;
    and, for a flexible material, a review of the key points a few days after that first quiz.

    A generated summary or flashcard set is not the same as having studied it: reading / practicing
    it stays a step until the learner completed a planner session of that activity. A step never
    assumes a resource that does not exist: a missing summary / flashcard set / quiz is an explicit
    preparation step ("generate", "create") whose session opens that tool's create state -- the
    planner itself never generates anything, and a new quiz (built from the flashcards, which come
    first) is only ever created in the Quiz tab. Weak-topic reviews open whatever the document has
    (see study_plan_api_service._session_tool). Recall practice uses the flashcards themselves: the
    planner no longer schedules new written (short answer) quizzes."""
    state = material.state
    title = state.title
    deadline = date.fromisoformat(material.deadline) if material.deadline else None
    if deadline is not None and deadline < today:
        return []  # deadline already passed: nothing meaningful to plan
    latest = max(today, deadline - timedelta(days=1)) if deadline else horizon_end
    final_start = max(today, deadline - timedelta(days=config.final_review_window_days)) if deadline else None
    code = state.state_reason.code
    quiz = state.quiz

    def step(activity, reason, message, kind, earliest=today, gap=0, artifact=None, metadata=None,
             variant=None) -> _Step:
        return _Step(activity, reason, message, estimate_duration(activity, state, config, variant=variant), kind,
                     min(max(earliest, today), latest), latest, gap, artifact, dict(metadata or {}))

    def learn_reason() -> str:
        close = deadline is not None and (deadline - today).days <= config.deadline_approaching_days
        return REASON_DEADLINE_APPROACHING if close else REASON_NEW_MATERIAL

    def learn_message(text) -> str:
        if learn_reason() == REASON_DEADLINE_APPROACHING:
            return f'"{title}" is due {deadline.isoformat()}: {text}.'
        return f'Start "{title}": {text}.'

    def review_due(percentage: float) -> date:
        base = local_date(quiz.latest_completed.completed_at, utc_offset, today)
        return base + timedelta(days=review_interval_days(percentage, config))

    # Recall practice is only possible from a persisted, usable flashcard set.
    has_cards = bool(state.flashcards.available and state.flashcards.card_count)
    done = set(state.completed_activities)
    weak = tuple(state.weak_topics)
    weak_names = [topic.name for topic in weak[:config.weak_topics_in_message]]
    topics_text = ", ".join(weak_names)

    def recall_practice(earliest=today, gap=0, metadata=None) -> _Step:
        return step("flashcards", REASON_RETRIEVAL_PRACTICE,
                    f'Practice "{title}" with your flashcards: reinforce recall before your next assessment.',
                    "review", earliest=earliest, gap=gap, metadata=metadata)

    def learn_first(stage: dict, create_cards: bool) -> list[_Step]:
        """Summary, then flashcards -- each a preparation step when missing, a reading / practice
        step when it exists but no planner session of it was completed yet."""
        learn = []
        if not state.summary.available:
            learn.append(step("summary", learn_reason(), learn_message("generate the summary and read it"), "learn",
                              metadata={**stage, "preparation": "generate_summary"}))
        elif "summary" not in done:
            learn.append(step("summary", learn_reason(), learn_message("read the summary"), "learn", metadata=stage))
        if has_cards:
            if "flashcards" not in done:
                learn.append(step("flashcards", learn_reason(), learn_message("practice the flashcards"), "learn",
                                  metadata=stage))
        elif create_cards:
            learn.append(step("flashcards", learn_reason(), learn_message("create flashcards and practice them"),
                              "learn", metadata={**stage, "preparation": "create_flashcards"}))
        return learn

    def review_after_quiz(stage: dict) -> list[_Step]:
        if deadline is not None:
            return []   # the final review before the deadline follows instead
        return [step("review", REASON_REVIEW_DUE, f'Review the key points of "{title}" after your quiz.', "review",
                     gap=config.post_quiz_review_gap_days, metadata=stage)]

    steps: list[_Step] = []
    if code == "marked_completed":
        pass  # only the final review below, when a deadline is coming up
    elif code == "quiz_in_progress":
        steps.append(step("quiz", REASON_QUIZ_IN_PROGRESS, f'Finish the quiz in progress for "{title}".',
                          "finish", artifact=quiz.latest_quiz_id if quiz.latest_quiz_status == "in_progress" else None,
                          metadata={"learning_stage": "quiz_in_progress"}))
    elif code in ("no_study_activity", "study_started") and quiz.quiz_count and quiz.latest_quiz_status == "not_started":
        # Quiz created but not attempted: study what is not studied yet, then take that quiz (no new
        # flashcards are required -- the quiz already exists).
        stage = {"learning_stage": "quiz_not_attempted"}
        steps.extend(learn_first(stage, create_cards=False))
        steps.append(step("quiz", learn_reason(), learn_message("take the quiz you created"), "learn",
                          gap=config.quiz_gap_after_learning_days if steps else 0, artifact=quiz.latest_quiz_id,
                          metadata=stage))
        steps.extend(review_after_quiz(stage))
    elif code in ("no_study_activity", "study_started"):
        # No quiz to take yet: summary -> flashcards (a new quiz needs them) -> quiz -> review.
        stage = {"learning_stage": "no_quiz"}
        steps.extend(learn_first(stage, create_cards=True))
        steps.append(step("quiz", learn_reason(), learn_message("create a first quiz in the Quiz tab and take it"),
                          "learn", gap=config.quiz_gap_after_learning_days if steps else 0,
                          metadata={**stage, "preparation": "create_quiz"}))
        steps.extend(review_after_quiz(stage))
    elif code in ("low_quiz_score", "moderate_quiz_score", "strong_quiz_score"):
        result = quiz.latest_completed
        due = review_due(result.percentage)
        facts = {"percentage": result.percentage, "quiz_id": result.quiz_id,
                 "weak_topics": [{"topic_id": t.topic_id, "name": t.name, "severity": t.severity} for t in weak]}
        scored = f'Latest quiz on "{title}" scored {result.percentage:g}%'
        if code == "low_quiz_score" and result.percentage < config.foundational_relearn_below:
            # Very weak: relearn the foundations from the summary, then flashcards (the weak-topic
            # review of an existing set plus recall practice, or a new set to practice), then a retry.
            stage = {**facts, "learning_stage": "very_low_score"}
            on = f" on {topics_text}" if weak_names else ""
            if state.summary.available:
                steps.append(step("summary", REASON_LOW_QUIZ_SCORE,
                                  f"{scored}: relearn the foundations -- re-read the summary{on}.", "review",
                                  earliest=due, metadata=stage))
            else:
                steps.append(step("summary", REASON_LOW_QUIZ_SCORE,
                                  f"{scored}: relearn the foundations -- generate the summary and read it{on}.",
                                  "review", earliest=due, metadata={**stage, "preparation": "generate_summary"}))
            if has_cards:
                focus = f"practice your weak topics ({topics_text})" if weak_names else "practice the key points"
                steps.append(step("review", REASON_LOW_QUIZ_SCORE, f"{scored}: {focus} with your flashcards.",
                                  "review", metadata=stage))
                steps.append(recall_practice(metadata=stage))
            else:
                steps.append(step("flashcards", REASON_LOW_QUIZ_SCORE,
                                  f"{scored}: create flashcards and practice the key points.", "review",
                                  metadata={**stage, "preparation": "create_flashcards"}))
            steps.append(step("quiz_retry", REASON_LOW_QUIZ_SCORE, f"{scored}: retake a quiz.", "review",
                              gap=config.low_score_retry_gap_days, artifact=result.quiz_id, metadata=stage))
        elif code == "low_quiz_score":
            # Below 60%: weak-topic review first, recall practice, then a retry on a later day.
            focus = f"review your weak topics ({topics_text})" if weak_names else "review the questions you missed"
            stage = {**facts, "learning_stage": "low_score"}
            steps.append(step("review", REASON_LOW_QUIZ_SCORE, f"{scored}: {focus}.", "review", earliest=due,
                              metadata=stage))
            if has_cards:
                steps.append(recall_practice(metadata=stage))
            steps.append(step("quiz_retry", REASON_LOW_QUIZ_SCORE, f"{scored}: retake a quiz.", "review",
                              gap=config.low_score_retry_gap_days, artifact=result.quiz_id, metadata=stage))
        elif code == "moderate_quiz_score":
            # 60-79%: review the incorrect topics, practice, then reassess. The final review
            # subsumes a review that would land inside its window anyway.
            stage = {**facts, "learning_stage": "moderate_score"}
            if final_start is None or due < final_start:
                focus = f"review the topics you got wrong ({topics_text})" if weak_names else "review the questions you got wrong"
                steps.append(step("review", REASON_REVIEW_DUE, f"{scored}: {focus}.", "review", earliest=due,
                                  metadata=stage))
            practice_gap = 1 if has_cards and config.moderate_retry_gap_days > 1 else 0
            if has_cards:
                steps.append(recall_practice(earliest=due, gap=practice_gap, metadata=stage))
            steps.append(step("quiz_retry", REASON_REVIEW_DUE, f'{scored}: check "{title}" again with a quiz.', "review",
                              earliest=due, gap=config.moderate_retry_gap_days - practice_gap, artifact=result.quiz_id,
                              metadata=stage))
        else:
            # >= 80%: lighter spaced repetition -- unless topic mastery still shows a clearly weak topic.
            high = [t.name for t in weak if t.severity == "high"][:config.weak_topics_in_message]
            stage = {**facts, "learning_stage": "strong_score"}
            if final_start is not None and due >= final_start:
                pass   # the final review covers it
            elif high:
                steps.append(step("review", REASON_REVIEW_DUE, f'{scored}, but {", ".join(high)} is still weak: review it.',
                                  "review", earliest=due, metadata=stage))
            else:
                steps.append(step("review", REASON_REVIEW_DUE, f'Spaced review of "{title}" ({scored.split(" scored ")[1]} last time).',
                                  "review", earliest=due, metadata=stage, variant="light_review"))
                second = due + timedelta(days=review_interval_days(result.percentage, config)
                                         * config.second_review_interval_factor)
                if second <= latest and (final_start is None or second < final_start):
                    steps.append(step("review", REASON_REVIEW_DUE, f'Spaced review of "{title}" again.', "review",
                                      earliest=second, metadata=stage, variant="light_review"))

    if deadline is not None:
        steps.append(_Step("review", REASON_FINAL_REVIEW, f'Final review of "{title}" before {deadline.isoformat()}.',
                           estimate_duration("review", state, config, final=True), "final", final_start, latest))
    return _combine_steps(steps, state, today, deadline, horizon_end, config)


def _combine_steps(steps: list[_Step], state, today: date, deadline: date | None, horizon_end: date,
                   config: SchedulerConfig) -> list[_Step]:
    """Turn suitable pairs of consecutive steps into combined steps (study_combined_sessions): back
    to back pairs always, pairs across a day gap only when the material is short of days (the days
    up to the day before its deadline, or its flexible spread window, per step still needed) or its
    deadline is close. The rest stay single steps, so the two kinds alternate."""
    if not config.combine_sessions or len(steps) < 2:
        return steps
    end = deadline - timedelta(days=1) if deadline else min(horizon_end, today + timedelta(days=config.flexible_spread_days - 1))
    days = max(1, (end - today).days + 1)
    close = deadline is not None and (deadline - today).days <= config.deadline_approaching_days
    tight = combined.under_time_pressure(days, len(steps), close)
    latest = state.quiz.latest_completed
    percentage = latest.percentage if latest is not None else None
    result = []
    for index, partner in combined.combine_pairs(steps, tight=tight, may_pair=lambda first, second: True):
        first = steps[index]
        if partner is None:
            result.append(first)
            continue
        second = steps[partner]
        first_minutes, second_minutes = combined.combined_minutes(
            first.activity_type, second.activity_type, daily_target_minutes=config.daily_target_minutes,
            latest_percentage=percentage)
        then = _ACTIVITY_LABELS[second.activity_type]
        if second.metadata.get("preparation") == "create_quiz":
            then = "create a quiz in the Quiz tab and take it"
        metadata = {**second.metadata, **first.metadata, "combined": True,
                    "step_preparations": [first.metadata.get("preparation"), second.metadata.get("preparation")]}
        result.append(_Step(
            first.activity_type, first.reason_code, f"{first.message.rstrip('.')}, then {then}.",
            first_minutes + second_minutes, first.kind, max(first.earliest, second.earliest),
            min(first.latest, second.latest), first.gap_days, second.artifact_id or first.artifact_id, metadata,
            parts=((first.activity_type, first_minutes, first.artifact_id),
                   (second.activity_type, second_minutes, second.artifact_id)),
        ))
    return result


def is_heavy(activities, config: SchedulerConfig = DEFAULT_CONFIG) -> bool:
    """A heavy session: any of its activities (both, when combined) is a heavy one."""
    return any(activity in config.heavy_activities for activity in activities)


def _horizon_end(context: SchedulingContext, today: date, config: SchedulerConfig) -> date:
    deadlines = [date.fromisoformat(m.deadline) for m in context.materials if m.deadline]
    end = today + timedelta(days=config.default_horizon_days)
    if deadlines:
        end = max(max(deadlines) - timedelta(days=1), today)
        if any(not m.deadline for m in context.materials):
            end = max(end, today + timedelta(days=config.default_horizon_days))
    return min(end, today + timedelta(days=config.max_horizon_days))


def _reason(step: _Step) -> SchedulingReason:
    return SchedulingReason(step.reason_code, step.message, dict(step.metadata))


def select_candidates(material: MaterialContext, now: datetime, config: SchedulerConfig = DEFAULT_CONFIG,
                      utc_offset: timedelta | None = None) -> list[CandidateActivity]:
    """The ordered activities the engine wants for one document right now (before any time slot)."""
    now, utc_offset = resolve_clock(now, utc_offset)
    today = now.date()
    steps = _build_steps(material, today, today + timedelta(days=config.default_horizon_days), utc_offset, config)
    return [
        CandidateActivity(material.document_id, s.activity_type, _reason(s), artifact_id=s.artifact_id,
                          estimated_minutes=s.duration, deadline=material.deadline, steps=s.steps())
        for s in steps
    ]


# ---------------------------------------------------------------------------
# 2. Priority
# ---------------------------------------------------------------------------

def base_priority(deadline_urgency: float, review_urgency: float, performance_need: float, workload: float,
                  config: SchedulerConfig = DEFAULT_CONFIG) -> float:
    """The base score: 35% deadline urgency, 35% review urgency, 20% performance need, 10% workload
    (weights from SchedulerConfig), each dimension in [0, 1]."""
    return (config.weight_deadline * deadline_urgency + config.weight_review * review_urgency
            + config.weight_performance * performance_need + config.weight_workload * workload)


def _guardrails(plan: _DocumentPlan, day: date, capacity: dict, config: SchedulerConfig) -> float:
    """Fairness and feasibility adjustments added on top of the base score."""
    adjustment = -config.same_day_penalty * plan.sessions_by_day.get(day, 0)
    if plan.last_date is None or (day - plan.last_date).days >= config.starvation_days:
        adjustment += config.starvation_boost
    if plan.deadline is not None:
        minutes_left, days_left = _capacity_until(day, plan.steps[-1].latest, capacity)
        if not minutes_left or plan.remaining_minutes / minutes_left >= config.pressure_threshold:
            adjustment += config.pressure_boost
        days_needed = 1 + sum(step.gap_days for step in plan.steps[plan.index + 1:])
        if days_left <= days_needed:
            adjustment += config.critical_boost
        if _last_chance(plan.current, day, capacity):
            adjustment += config.last_chance_boost
    return adjustment


def _last_chance(step: _Step, day: date, capacity: dict) -> bool:
    """No later day up to the step's latest (the day before the deadline) has room for it."""
    current = day + timedelta(days=1)
    while current <= step.latest:
        if capacity.get(current, 0) >= step.duration:
            return False
        current += timedelta(days=1)
    return True


def _priority(plan: _DocumentPlan, day: date, max_remaining: int, capacity: dict,
              config: SchedulerConfig) -> float:
    step = plan.current
    deadline_urgency = 0.0
    if plan.deadline is not None:
        deadline_urgency = max(0.0, min(1.0, 1 - (plan.deadline - day).days / config.urgency_horizon_days))
    if plan.target is not None:   # soft: a share of the urgency a deadline on that date would give
        target_urgency = max(0.0, min(1.0, 1 - (plan.target - day).days / config.urgency_horizon_days))
        deadline_urgency = max(deadline_urgency, config.target_urgency_weight * target_urgency)
    review_urgency = config.review_urgency[step.kind]
    if step.kind == "review":
        overdue = max(0, (day - step.earliest).days)
        review_urgency = min(1.0, review_urgency + config.review_urgency_per_overdue_day * overdue)
    performance = config.performance_need.get(plan.context.state.state_reason.code, 0.5)
    workload = plan.remaining_minutes / max_remaining if max_remaining else 0.0
    return (base_priority(deadline_urgency, review_urgency, performance, workload, config)
            + _guardrails(plan, day, capacity, config))


def _capacity_until(day: date, latest: date, capacity: dict) -> tuple[int, int]:
    """(comfortable minutes, days with any comfortable minutes) from `day` through `latest`;
    `capacity` already reflects what is used today."""
    minutes, days, current = 0, 0, day
    while current <= latest:
        minutes += capacity.get(current, 0)
        days += int(capacity.get(current, 0) > 0)
        current += timedelta(days=1)
    return minutes, days


# ---------------------------------------------------------------------------
# 3. Allocation
# ---------------------------------------------------------------------------

def _free_by_date(context: SchedulingContext, now: datetime, end: date, config: SchedulerConfig):
    """Availability minus busy sessions (padded by the session gap), never in the past."""
    free = free_minutes_by_date(now.date(), end, list(context.availability), [], now=now)
    gap = config.session_gap_minutes
    for session in context.busy_sessions:
        day = date.fromisoformat(session["scheduled_start"][:10])
        if day in free:
            start = to_minutes(session["scheduled_start"][11:16])
            finish = end_minute(session["scheduled_start"], session["scheduled_end"])
            free[day] = subtract_minute_interval(free[day], start - gap, finish + gap)
    return free


def _find_slot(intervals: list, start_from: int, duration: int, align: int) -> int | None:
    for start, end in intervals:
        candidate = -(-max(start, start_from) // align) * align
        if candidate + duration <= end:
            return candidate
    return None


_at = at_minute  # a session ending at 24:00 is stored as the next day's 00:00


def find_next_slot(context: SchedulingContext, duration: int, latest: date, earliest: datetime | None = None,
                   config: SchedulerConfig = DEFAULT_CONFIG) -> tuple[str, str] | None:
    """The first window of `duration` minutes, from max(now, earliest) through `latest` (inclusive),
    inside the learner's availability and clear of busy sessions (padded by the session gap, as
    in plan_schedule). Returns naive-local (start, end) ISO strings, or None."""
    now, _ = resolve_clock(context.now, context.utc_offset)
    start_at = max(now, earliest) if earliest else now
    if latest < start_at.date():
        return None
    free = _free_by_date(context, start_at, latest, config)
    for day in sorted(free):
        minute = _find_slot(free[day], 0, duration, config.slot_alignment_minutes)
        if minute is not None:
            return _at(day, minute), _at(day, minute + duration)
    return None


def target_date(material: MaterialContext, today: date) -> date | None:
    """The study goal's soft target completion date, when it is still ahead (a passed or malformed
    one is simply ignored -- it is a preference, never a constraint)."""
    value = material.material.get("target_date")
    try:
        target = date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return target if target is not None and target >= today else None


def _widen_empty_final_windows(plans: list, usable: list, today: date, config: SchedulerConfig) -> None:
    """A final review window (the last days before a deadline) with no usable day at all -- e.g. a
    weekend without availability -- starts on the last usable day before it instead, when that day
    is at most final_review_fallback_days before the deadline (so a free Friday still counts).
    Never on or after the deadline."""
    for plan in plans:
        final = plan.steps[-1] if plan.steps and plan.steps[-1].kind == "final" else None
        if final is None or any(final.earliest <= d <= final.latest for d in usable):
            continue
        floor = max(today, plan.deadline - timedelta(days=config.final_review_fallback_days))
        earlier = [d for d in usable if floor <= d < final.earliest]
        if earlier:
            final.earliest = earlier[-1]


def spread_end(deadline: date | None, target: date | None, steps: list, today: date, horizon_end: date,
               config: SchedulerConfig) -> date | None:
    """The last day a document's steps should spread over (shared by the initial plan and the
    adaptation proposals): the day before its final review window for a hard deadline, the soft
    target completion date when sooner, or flexible_spread_days for flexible learning. None when
    that day is already behind us (nothing left to spread)."""
    final = steps[-1] if steps and steps[-1].kind == "final" else None
    if deadline is not None:
        end = (final.earliest - timedelta(days=1)) if final else steps[-1].latest if steps else today
    else:
        end = min(horizon_end, today + timedelta(days=config.flexible_spread_days - 1))
    if target is not None:
        end = min(end, target)
    return end if end >= today else None


_PACED_KINDS = ("learn", "review")   # "finish" (a quiz in progress) and "final" keep their own dates


def _pace_days(plans: list, usable: list, today: date, horizon_end: date, config: SchedulerConfig) -> None:
    """Spread each material's steps evenly over its usable days.

    The pacing end is the day before the final review window for a hard deadline, the soft target
    completion date when it comes sooner, or flexible_spread_days for flexible learning -- extended,
    for flexible learning only, until it holds one usable day per step when availability is sparse.
    Step i of k gets the usable day at position round(i * N / k + offset) of the N usable days from
    the first step's earliest day, where the offset (rank / number of materials of one interval, at
    most `rank` days) keeps several materials from all landing on the same days without leaving one
    waiting long to start. New material, recall practice and quiz follow-ups are all paced (a pace
    never comes before a review's spaced-repetition due date); an in-progress quiz and the final
    review are not. A pace is a preference: _allocate lifts it when the material runs short of time."""
    ranked = sorted(plans, key=lambda p: (p.deadline or date.max, p.target or date.max, p.context.document_id))
    for rank, plan in enumerate(ranked):
        plan.spread_end = spread_end(plan.deadline, plan.target, plan.steps, today, horizon_end, config)
        if plan.spread_end is None or not _balanced(plan, config):
            continue
        paced = [index for index, step in enumerate(plan.steps) if step.kind in _PACED_KINDS]
        if not paced:
            continue
        start = max(today, plan.steps[paced[0]].earliest)
        days = [d for d in usable if start <= d <= plan.spread_end]
        if plan.deadline is None and plan.target is None and len(days) < len(paced):
            days = [d for d in usable if start <= d <= horizon_end][:len(paced)]
            plan.spread_end = max(plan.spread_end, days[-1]) if days else plan.spread_end
        if len(days) < 2:
            continue
        interval = len(days) / len(paced)
        # No material waits more than `rank` days to start; a due quiz-result review starts on its due date.
        due_review = plan.steps[paced[0]].reason_code in (REASON_LOW_QUIZ_SCORE, REASON_REVIEW_DUE)
        offset = 0 if due_review else min(rank / len(ranked) * interval, rank)
        for position, index in enumerate(paced):
            day = days[min(len(days) - 1, int(position * interval + offset + 0.5))]
            plan.pace[index] = max(day, plan.steps[index].earliest)


def _balanced(plan: _DocumentPlan, config: SchedulerConfig) -> bool:
    """Whether this document is paced and spread (all of them, except deadlined ones under the
    Deadline Priority strategy)."""
    return plan.deadline is None or config.pace_deadline_materials


def _short_of_time(plan: _DocumentPlan, day: date, capacity: dict, config: SchedulerConfig) -> bool:
    """True when waiting for a later day could cost the material its deadline (or target): the
    pacing preferences then give way and the material is scheduled as early as before."""
    end = plan.deadline - timedelta(days=1) if plan.deadline else plan.target
    if end is None:
        return False
    minutes_left, days_left = _capacity_until(day, end, capacity)
    if not minutes_left or plan.remaining_minutes / minutes_left >= config.pressure_threshold:
        return True
    # One day per remaining step, plus the extra empty days longer gaps force between them. Exactly
    # enough days still allows one session a day; _last_chance keeps any of them from being lost.
    remaining = plan.steps[plan.index:]
    days_needed = len(remaining) + sum(max(0, step.gap_days - 1) for step in remaining[1:])
    return days_left < days_needed


def _spread_allows(plan: _DocumentPlan, day: date, usable: list) -> bool:
    """Prefer one session per material per day: a second one the same day only when the material
    has fewer usable days left in its spread window than steps still to place."""
    if not plan.sessions_by_day.get(day):
        return True
    end = plan.spread_end or plan.steps[-1].latest
    days_after = sum(1 for d in usable if day < d <= end)
    return days_after < len(plan.steps) - plan.index


def deadline_risks(result: ScheduleResult) -> list[dict]:
    """One entry per material whose activities do not all fit (nearest deadline first, flexible
    materials last): how many sessions and minutes are missing, whether the final review is among
    them, and the last session that did fit. Derived from the result itself, so it stays true after
    the learner's placements; nothing is dropped silently."""
    missing: dict = {}
    for candidate in result.capacity.unscheduled:
        entry = missing.setdefault(candidate.document_id, {
            "document_id": candidate.document_id, "deadline": candidate.deadline, "unscheduled_count": 0,
            "shortfall_minutes": 0, "final_review_missing": False, "activities": [],
        })
        entry["unscheduled_count"] += 1
        entry["shortfall_minutes"] += candidate.estimated_minutes or 0
        entry["final_review_missing"] |= candidate.reason.code == REASON_FINAL_REVIEW
        entry["activities"].append(candidate.activity_type)
    for entry in missing.values():
        placed = [p for p in result.proposals if p.document_id == entry["document_id"]]
        entry["scheduled_count"] = len(placed)
        entry["last_scheduled_start"] = max((p.scheduled_start for p in placed), default=None)
    return sorted(missing.values(), key=lambda e: (e["deadline"] or "9999-12-31", e["document_id"]))


def plan_schedule(context: SchedulingContext, config: SchedulerConfig = DEFAULT_CONFIG) -> ScheduleResult:
    """Paced allocation (sessions spread over the usable days) when config.spread_sessions; if that
    would leave more work unscheduled than the plain earliest-first allocation, the latter is used,
    so spreading never costs the learner a session."""
    if not config.spread_sessions:
        return _allocate(context, config, spread=False)
    paced = _allocate(context, config, spread=True)
    if paced.capacity.shortfall_minutes:
        packed = _allocate(context, config, spread=False)
        if packed.capacity.shortfall_minutes < paced.capacity.shortfall_minutes:
            return packed
    return paced


def _allocate(context: SchedulingContext, config: SchedulerConfig, spread: bool) -> ScheduleResult:
    now, utc_offset = resolve_clock(context.now, context.utc_offset)
    today = now.date()
    end = _horizon_end(context, today, config)
    plans = [
        _DocumentPlan(material, date.fromisoformat(material.deadline) if material.deadline else None,
                      _build_steps(material, today, end, utc_offset, config), target=target_date(material, today))
        for material in context.materials
    ]
    free = _free_by_date(context, now, end, config)
    daily_capacity = {
        day: min(config.daily_target_minutes, sum(finish - start for start, finish in intervals))
        for day, intervals in free.items()
    }
    # Usable = a day with at least one session's worth of comfortable free time.
    usable = sorted(day for day, minutes in daily_capacity.items() if minutes >= MIN_SESSION_MINUTES)
    _widen_empty_final_windows(plans, usable, today, config)
    if spread:
        _pace_days(plans, usable, today, end, config)
    proposals: list[ProposedSession] = []
    unscheduled: list[tuple[_DocumentPlan, _Step]] = []

    day = today
    while day <= end:
        for plan in plans:  # steps whose last possible day has passed cannot be placed any more
            while plan.current and plan.current.latest < day:
                if not plan.final_satisfied_by_retrieval(config):
                    unscheduled.append((plan, plan.current))
                plan.index += 1
        cursor, used, heavy_count, last_heavy = None, 0, 0, False
        while True:
            remaining_capacity = {**daily_capacity, day: daily_capacity.get(day, 0) - used}
            ready = [plan for plan in plans if plan.current and plan.ready_date(config) <= day
                     and plan.sessions_by_day.get(day, 0) < config.max_sessions_per_document_per_day]
            if spread:   # paced and spread out, unless the material would run short of time
                ready = [plan for plan in ready
                         if not _balanced(plan, config) or _short_of_time(plan, day, remaining_capacity, config)
                         or (day >= plan.pace.get(plan.index, day) and _spread_allows(plan, day, usable))]
            max_remaining = max((plan.remaining_minutes for plan in plans), default=0)
            scored = sorted(
                ((_priority(plan, day, max_remaining, remaining_capacity, config), plan)
                 for plan in ready),
                key=lambda item: (-item[0], item[1].deadline or date.max, item[1].context.document_id),
            )
            placed = False
            for score, plan in scored:
                step = plan.current
                heavy = is_heavy(step.activities, config)
                if used + step.duration > config.daily_target_minutes:
                    continue
                if heavy and heavy_count >= config.max_heavy_sessions_per_day:
                    continue
                gap = config.heavy_gap_minutes if (heavy and last_heavy) else config.session_gap_minutes
                start = _find_slot(free.get(day, []), 0 if cursor is None else cursor + gap,
                                   step.duration, config.slot_alignment_minutes)
                if start is None:
                    continue
                proposals.append(ProposedSession(
                    plan.context.document_id, step.activity_type, _at(day, start), _at(day, start + step.duration),
                    step.duration, _reason(step), artifact_id=step.artifact_id, priority_snapshot=round(score, 4),
                    steps=step.steps(),
                ))
                cursor, used, last_heavy = start + step.duration, used + step.duration, heavy
                heavy_count += int(heavy)
                plan.sessions_by_day[day] = plan.sessions_by_day.get(day, 0) + 1
                plan.last_date, plan.last_activity = day, step.activities[-1]
                plan.index += 1
                placed = True
                break
            if not placed:
                break
        day += timedelta(days=1)
    for plan in plans:
        while plan.current:
            if not plan.final_satisfied_by_retrieval(config):
                unscheduled.append((plan, plan.current))
            plan.index += 1

    scheduled = sum(p.duration_minutes for p in proposals)
    shortfall = sum(step.duration for _, step in unscheduled)
    capacity = CapacityReport(
        required_minutes=scheduled + shortfall, scheduled_minutes=scheduled, shortfall_minutes=shortfall,
        schedulable_minutes=sum(daily_capacity.values()),
        available_minutes=sum(end_ - start for intervals in free.values() for start, end_ in intervals),
        status="on_track" if shortfall == 0 else "at_risk",
        unscheduled=tuple(
            CandidateActivity(plan.context.document_id, step.activity_type, _reason(step),
                              artifact_id=step.artifact_id, estimated_minutes=step.duration,
                              deadline=plan.context.deadline, steps=step.steps())
            for plan, step in unscheduled
        ),
    )
    return ScheduleResult(proposals=tuple(proposals), capacity=capacity)
