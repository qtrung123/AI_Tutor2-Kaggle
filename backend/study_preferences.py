"""Study Preferences: the learner's own scheduling choices, persisted per user (study_planner_store,
table study_preferences) and turned into the SchedulerConfig that Auto Plan, adaptation, live
candidates and rescheduling actually run with.

No saved row = the defaults below, which are exactly study_scheduler.DEFAULT_CONFIG -- so a learner
who never opens the settings keeps today's behaviour. Only choices the learner makes are stored;
nothing here ever changes an existing session.
"""

import json
from dataclasses import replace

from backend import study_planner_store
from backend.study_scheduler import DEFAULT_CONFIG, SchedulerConfig
from backend.study_session_duration import MIN_SESSION_MINUTES

DAILY_TARGET_OPTIONS = (60, 90, 120, 180)
BREAK_OPTIONS = (5, 10, 15, 20, 30)
STRATEGIES = ("balanced", "finish_early", "deadline_priority")
MAX_SESSION_MINUTES = 180
DURATION_STEP_MINUTES = 5
# Every per-activity length the scheduler uses for NEW sessions (SchedulerConfig.durations).
DURATION_KEYS = tuple(DEFAULT_CONFIG.durations)

DEFAULTS = {
    "daily_target_minutes": DEFAULT_CONFIG.daily_target_minutes,   # 90
    "break_minutes": DEFAULT_CONFIG.session_gap_minutes,           # 20
    "strategy": "balanced",
    "durations": dict(DEFAULT_CONFIG.durations),
}

# How each strategy shapes the scheduler (on top of the learner's numbers):
# - balanced: Task 2 distribution -- new material paced over the available days, one session per
#   document per day while there is room (the default);
# - finish_early: earliest-first, as soon as there is free time;
# - deadline_priority: documents with a deadline are scheduled as early as possible with deadline
#   urgency weighted higher; documents without one stay balanced.
_STRATEGY_CONFIG = {
    "balanced": {},
    "finish_early": {"spread_sessions": False},
    "deadline_priority": {"pace_deadline_materials": False, "weight_deadline": 0.55, "weight_review": 0.25,
                          "weight_performance": 0.12, "weight_workload": 0.08},
}


class PreferencesError(ValueError):
    """An invalid preference value (HTTP 400); `field` names it for the UI."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _choice(value, options: tuple, field: str, label: str):
    if isinstance(value, bool) or value not in options:
        raise PreferencesError(field, f"{label} must be one of: {', '.join(str(option) for option in options)}.")
    return value


def validate(values: dict) -> dict:
    """A complete, valid preference set: defaults filled in, every value checked."""
    merged = {**DEFAULTS, **{key: values[key] for key in DEFAULTS if key in values and values[key] is not None}}
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise PreferencesError(sorted(unknown)[0], f"Unknown preference: {sorted(unknown)[0]}.")
    daily = _choice(merged["daily_target_minutes"], DAILY_TARGET_OPTIONS, "daily_target_minutes", "Daily study target")
    _choice(merged["break_minutes"], BREAK_OPTIONS, "break_minutes", "Break between sessions")
    _choice(merged["strategy"], STRATEGIES, "strategy", "Scheduling strategy")
    durations = merged["durations"]
    if not isinstance(durations, dict):
        raise PreferencesError("durations", "durations must map each activity to minutes.")
    unknown = set(durations) - set(DURATION_KEYS)
    if unknown:
        raise PreferencesError(f"durations.{sorted(unknown)[0]}", f"Unknown activity: {sorted(unknown)[0]}.")
    lengths = {**DEFAULTS["durations"], **durations}
    for key, minutes in lengths.items():
        field = f"durations.{key}"
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise PreferencesError(field, "Session length must be a whole number of minutes.")
        if minutes < MIN_SESSION_MINUTES:
            raise PreferencesError(field, f"A study session must be at least {MIN_SESSION_MINUTES} minutes long.")
        if minutes > MAX_SESSION_MINUTES or minutes % DURATION_STEP_MINUTES:
            raise PreferencesError(field, f"Choose a length up to {MAX_SESSION_MINUTES} minutes, in steps of "
                                          f"{DURATION_STEP_MINUTES}.")
        if minutes > daily:
            raise PreferencesError(field, f"{minutes} minutes is longer than your daily study target ({daily} minutes): "
                                          "such a session could never be scheduled.")
    return {**merged, "durations": lengths}


def get_preferences(owner_id: str) -> dict:
    """The learner's effective preferences plus `saved` (False = the defaults, nothing stored)."""
    row = study_planner_store.get_study_preferences(owner_id)
    if row is None:
        return {**DEFAULTS, "durations": dict(DEFAULTS["durations"]), "saved": False, "updated_at": None}
    durations = {**DEFAULTS["durations"], **json.loads(row["durations_json"] or "{}")}
    return {"daily_target_minutes": row["daily_target_minutes"], "break_minutes": row["break_minutes"],
            "strategy": row["strategy"], "durations": durations, "saved": True, "updated_at": row["updated_at"]}


def save_preferences(owner_id: str, changes: dict) -> dict:
    """Merge `changes` into the learner's current preferences, validate the whole set and save it.
    Saving never touches sessions: it shapes the next Auto Plan / adaptation proposals only."""
    current = get_preferences(owner_id)
    base = {key: current[key] for key in DEFAULTS}
    if "durations" in changes and isinstance(changes["durations"], dict):
        changes = {**changes, "durations": {**base["durations"], **changes["durations"]}}
    values = validate({**base, **changes})
    study_planner_store.save_study_preferences(owner_id, values["daily_target_minutes"], values["break_minutes"],
                                               values["strategy"], json.dumps(values["durations"], sort_keys=True))
    return get_preferences(owner_id)


def reset_preferences(owner_id: str) -> dict:
    study_planner_store.delete_study_preferences(owner_id)
    return get_preferences(owner_id)


def config_from(preferences: dict) -> SchedulerConfig:
    """The scheduler configuration for these preferences. The learner's break applies between every
    pair of sessions (heavy ones included) -- the choice is theirs, so it is never silently raised."""
    return replace(DEFAULT_CONFIG, daily_target_minutes=preferences["daily_target_minutes"],
                   session_gap_minutes=preferences["break_minutes"], heavy_gap_minutes=preferences["break_minutes"],
                   durations={**DEFAULT_CONFIG.durations, **preferences["durations"]},
                   **_STRATEGY_CONFIG[preferences["strategy"]])


def scheduler_config(owner_id: str) -> SchedulerConfig:
    """What Auto Plan, adaptation, candidates and rescheduling run with for this learner:
    DEFAULT_CONFIG itself when nothing was ever saved."""
    preferences = get_preferences(owner_id)
    return config_from(preferences) if preferences["saved"] else DEFAULT_CONFIG


def options() -> dict:
    """The choices the settings UI offers (the single source for them)."""
    return {"daily_target_minutes": list(DAILY_TARGET_OPTIONS), "break_minutes": list(BREAK_OPTIONS),
            "strategy": list(STRATEGIES), "durations": list(DURATION_KEYS),
            "min_session_minutes": MIN_SESSION_MINUTES, "max_session_minutes": MAX_SESSION_MINUTES,
            "duration_step_minutes": DURATION_STEP_MINUTES}
