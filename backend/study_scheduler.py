"""Deterministic Study Planner scheduler (MVP): SchedulingContext -> ScheduleResult.

Pure and LLM-free. Same input, same output. Flow:
  1. per document, turn its DocumentStudyState into an ordered queue of steps (candidate
     activities with earliest/latest dates and minimum spacing between them);
  2. walk the horizon day by day; on each day repeatedly pick the highest-priority ready step
     and place it in the learner's free availability, within a comfortable daily budget;
  3. report whatever did not fit as a capacity shortfall instead of packing time harder.

Every tunable number lives in SchedulerConfig.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from backend.study_time import free_minutes_by_date, subtract_minute_interval, to_minutes
from backend.study_scheduler_contracts import (
    REASON_DEADLINE_APPROACHING, REASON_FINAL_REVIEW, REASON_LOW_QUIZ_SCORE, REASON_NEW_MATERIAL,
    REASON_QUIZ_IN_PROGRESS, REASON_REVIEW_DUE, CandidateActivity, CapacityReport, MaterialContext,
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
    moderate_retry_gap_days: int = 2      # moderate score: review, then quiz_retry this many days later
    quiz_gap_after_learning_days: int = 1  # the first quiz never lands the same day as new material
    final_review_window_days: int = 2     # final review within this many days before the deadline
    # A quiz/quiz_retry inside the final-review window already is final retrieval: a separate final
    # review is only added on a LATER day, and is dropped (satisfied) when no later slot exists.
    retrieval_activities: tuple = ("quiz", "quiz_retry")
    deadline_approaching_days: int = 3    # new material this close to its deadline says so
    default_horizon_days: int = 14        # horizon for plans without deadlines
    max_horizon_days: int = 42
    # Durations: (min, max, default when the artifact size is unknown), minutes.
    durations: dict = field(default_factory=lambda: {
        "summary": (30, 50, 40), "flashcards": (15, 30, 20), "quiz": (20, 40, 30),
        "review": (10, 20, 15), "quiz_retry": (20, 30, 25),
    })
    summary_base_minutes: int = 20
    summary_minutes_per_chunk: float = 2.0
    flashcard_minutes_per_card: float = 1.0
    review_minutes_per_card: float = 0.5
    quiz_minutes_per_question: float = 2.0
    duration_rounding_minutes: int = 5
    # Daily shape: availability is permission, not obligation.
    max_block_minutes: int = 50
    daily_target_minutes: int = 90
    session_gap_minutes: int = 10
    heavy_gap_minutes: int = 30           # between two heavy sessions on the same day
    heavy_activities: tuple = ("summary", "quiz", "quiz_retry")
    max_heavy_sessions_per_day: int = 2
    max_sessions_per_document_per_day: int = 2
    slot_alignment_minutes: int = 5


DEFAULT_CONFIG = SchedulerConfig()

_ACTIVITY_LABELS = {
    "summary": "read the summary", "flashcards": "practice the flashcards", "quiz": "take a quiz",
    "review": "review the key points", "quiz_retry": "retake a quiz",
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


@dataclass
class _DocumentPlan:
    context: MaterialContext
    deadline: date | None
    steps: list
    index: int = 0
    last_date: date | None = None
    last_activity: str | None = None
    sessions_by_day: dict = field(default_factory=dict)

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

def _round_clamp(minutes: float, bounds: tuple, rounding: int) -> int:
    low, high, _ = bounds
    rounded = int(rounding * math.floor(minutes / rounding + 0.5))
    return max(low, min(high, rounded))


def estimate_duration(activity_type: str, state, config: SchedulerConfig = DEFAULT_CONFIG) -> int:
    """Activity-appropriate minutes, scaled by the artifact size when known (chunks, cards, questions)."""
    bounds, rounding = config.durations[activity_type], config.duration_rounding_minutes
    cards, questions = state.flashcards.card_count, state.quiz.latest_quiz_question_count
    if activity_type == "summary" and state.chunk_count:
        value = config.summary_base_minutes + config.summary_minutes_per_chunk * state.chunk_count
    elif activity_type == "flashcards" and cards:
        value = cards * config.flashcard_minutes_per_card
    elif activity_type == "review" and cards:
        value = cards * config.review_minutes_per_card
    elif activity_type == "quiz" and questions:
        value = questions * config.quiz_minutes_per_question
    elif activity_type == "quiz_retry" and (state.quiz.latest_completed or questions):
        total = state.quiz.latest_completed.total if state.quiz.latest_completed else questions
        value = total * config.quiz_minutes_per_question
    else:
        return bounds[2]
    return min(_round_clamp(value, bounds, rounding), config.max_block_minutes)


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
    state = material.state
    title = state.title
    deadline = date.fromisoformat(material.deadline) if material.deadline else None
    if deadline is not None and deadline < today:
        return []  # deadline already passed: nothing meaningful to plan
    latest = max(today, deadline - timedelta(days=1)) if deadline else horizon_end
    final_start = max(today, deadline - timedelta(days=config.final_review_window_days)) if deadline else None
    code = state.state_reason.code
    quiz = state.quiz

    def step(activity, reason, message, kind, earliest=today, gap=0, artifact=None) -> _Step:
        return _Step(activity, reason, message, estimate_duration(activity, state, config), kind,
                     min(max(earliest, today), latest), latest, gap, artifact)

    def learn_reason() -> str:
        close = deadline is not None and (deadline - today).days <= config.deadline_approaching_days
        return REASON_DEADLINE_APPROACHING if close else REASON_NEW_MATERIAL

    def learn_message(activity) -> str:
        if learn_reason() == REASON_DEADLINE_APPROACHING:
            return f'"{title}" is due {deadline.isoformat()}: {_ACTIVITY_LABELS[activity]}.'
        return f'Start "{title}": {_ACTIVITY_LABELS[activity]}.'

    def review_due(percentage: float) -> date:
        base = local_date(quiz.latest_completed.completed_at, utc_offset, today)
        return base + timedelta(days=review_interval_days(percentage, config))

    steps: list[_Step] = []
    if code == "marked_completed":
        pass  # only the final review below, when a deadline is coming up
    elif code == "quiz_in_progress":
        steps.append(step("quiz", REASON_QUIZ_IN_PROGRESS, f'Finish the quiz in progress for "{title}".',
                          "finish", artifact=quiz.latest_quiz_id if quiz.latest_quiz_status == "in_progress" else None))
    elif code in ("no_study_activity", "study_started"):
        if not state.summary.available:
            steps.append(step("summary", learn_reason(), learn_message("summary"), "learn",
                              artifact=state.summary.summary_id))
        if not state.flashcards.available:
            steps.append(step("flashcards", learn_reason(), learn_message("flashcards"), "learn"))
        steps.append(step("quiz", learn_reason(), learn_message("quiz"), "learn",
                          gap=config.quiz_gap_after_learning_days if steps else 0,
                          artifact=quiz.latest_quiz_id if quiz.latest_quiz_status == "not_started" else None))
    elif code == "low_quiz_score":
        result = quiz.latest_completed
        low_message = f'Latest quiz on "{title}" scored {result.percentage:g}%'
        steps.append(step("flashcards", REASON_LOW_QUIZ_SCORE, f"{low_message}: practice the flashcards.", "review",
                          earliest=review_due(result.percentage), artifact=state.flashcards.set_id))
        steps.append(step("quiz_retry", REASON_LOW_QUIZ_SCORE, f"{low_message}: retake a quiz.", "review",
                          gap=config.low_score_retry_gap_days, artifact=result.quiz_id))
    elif code in ("moderate_quiz_score", "strong_quiz_score"):
        result = quiz.latest_completed
        due = review_due(result.percentage)
        # The final review subsumes a spaced review that would land inside its window anyway.
        if final_start is None or due < final_start:
            steps.append(step("review", REASON_REVIEW_DUE, f'Spaced review of "{title}".', "review", earliest=due))
        if code == "moderate_quiz_score":
            steps.append(step("quiz_retry", REASON_REVIEW_DUE, f'Check "{title}" again with a quiz.', "review",
                              earliest=due, gap=config.moderate_retry_gap_days, artifact=result.quiz_id))

    if deadline is not None:
        steps.append(_Step("review", REASON_FINAL_REVIEW, f'Final review of "{title}" before {deadline.isoformat()}.',
                           estimate_duration("review", state, config), "final", final_start, latest))
    return steps


def _horizon_end(context: SchedulingContext, today: date, config: SchedulerConfig) -> date:
    deadlines = [date.fromisoformat(m.deadline) for m in context.materials if m.deadline]
    end = today + timedelta(days=config.default_horizon_days)
    if deadlines:
        end = max(max(deadlines) - timedelta(days=1), today)
        if any(not m.deadline for m in context.materials):
            end = max(end, today + timedelta(days=config.default_horizon_days))
    return min(end, today + timedelta(days=config.max_horizon_days))


def _reason(step: _Step) -> SchedulingReason:
    return SchedulingReason(step.reason_code, step.message)


def select_candidates(material: MaterialContext, now: datetime, config: SchedulerConfig = DEFAULT_CONFIG,
                      utc_offset: timedelta | None = None) -> list[CandidateActivity]:
    """The ordered activities the engine wants for one document right now (before any time slot)."""
    now, utc_offset = resolve_clock(now, utc_offset)
    today = now.date()
    steps = _build_steps(material, today, today + timedelta(days=config.default_horizon_days), utc_offset, config)
    return [
        CandidateActivity(material.document_id, s.activity_type, _reason(s), artifact_id=s.artifact_id,
                          estimated_minutes=s.duration, deadline=material.deadline)
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
    return adjustment


def _priority(plan: _DocumentPlan, day: date, max_remaining: int, capacity: dict,
              config: SchedulerConfig) -> float:
    step = plan.current
    deadline_urgency = 0.0
    if plan.deadline is not None:
        deadline_urgency = max(0.0, min(1.0, 1 - (plan.deadline - day).days / config.urgency_horizon_days))
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
            finish = to_minutes(session["scheduled_end"][11:16])
            free[day] = subtract_minute_interval(free[day], start - gap, finish + gap)
    return free


def _find_slot(intervals: list, start_from: int, duration: int, align: int) -> int | None:
    for start, end in intervals:
        candidate = -(-max(start, start_from) // align) * align
        if candidate + duration <= end:
            return candidate
    return None


def _at(day: date, minute: int) -> str:
    return datetime(day.year, day.month, day.day, minute // 60, minute % 60).isoformat()


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


def plan_schedule(context: SchedulingContext, config: SchedulerConfig = DEFAULT_CONFIG) -> ScheduleResult:
    now, utc_offset = resolve_clock(context.now, context.utc_offset)
    today = now.date()
    end = _horizon_end(context, today, config)
    plans = [
        _DocumentPlan(material, date.fromisoformat(material.deadline) if material.deadline else None,
                      _build_steps(material, today, end, utc_offset, config))
        for material in context.materials
    ]
    free = _free_by_date(context, now, end, config)
    daily_capacity = {
        day: min(config.daily_target_minutes, sum(finish - start for start, finish in intervals))
        for day, intervals in free.items()
    }
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
            ready = [plan for plan in plans if plan.current and plan.ready_date(config) <= day
                     and plan.sessions_by_day.get(day, 0) < config.max_sessions_per_document_per_day]
            max_remaining = max((plan.remaining_minutes for plan in plans), default=0)
            scored = sorted(
                ((_priority(plan, day, max_remaining, {**daily_capacity, day: daily_capacity.get(day, 0) - used},
                            config), plan)
                 for plan in ready),
                key=lambda item: (-item[0], item[1].deadline or date.max, item[1].context.document_id),
            )
            placed = False
            for score, plan in scored:
                step = plan.current
                is_heavy = step.activity_type in config.heavy_activities
                if used + step.duration > config.daily_target_minutes:
                    continue
                if is_heavy and heavy_count >= config.max_heavy_sessions_per_day:
                    continue
                gap = config.heavy_gap_minutes if (is_heavy and last_heavy) else config.session_gap_minutes
                start = _find_slot(free.get(day, []), 0 if cursor is None else cursor + gap,
                                   step.duration, config.slot_alignment_minutes)
                if start is None:
                    continue
                proposals.append(ProposedSession(
                    plan.context.document_id, step.activity_type, _at(day, start), _at(day, start + step.duration),
                    step.duration, _reason(step), artifact_id=step.artifact_id, priority_snapshot=round(score, 4),
                ))
                cursor, used, last_heavy = start + step.duration, used + step.duration, is_heavy
                heavy_count += int(is_heavy)
                plan.sessions_by_day[day] = plan.sessions_by_day.get(day, 0) + 1
                plan.last_date, plan.last_activity = day, step.activity_type
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
                              deadline=plan.context.deadline)
            for plan, step in unscheduled
        ),
    )
    return ScheduleResult(proposals=tuple(proposals), capacity=capacity)
