"""Duration rules for NEW study sessions (scheduler output, adaptation additions, queue placements).

Existing sessions are never re-validated or resized: a saved 15-minute session keeps its 15 minutes
when it is moved, rescheduled, completed or shown. Only a session that is about to be created must
last at least MIN_SESSION_MINUTES. The presets and the default are the choices a learner is offered
when picking a length by hand (mirrored in frontend/app.js).
"""

MIN_SESSION_MINUTES = 20
SESSION_DURATION_PRESETS = (20, 30, 40, 50)
DEFAULT_SESSION_MINUTES = 30


class SessionDurationError(ValueError):
    """A new session shorter than the minimum, or not a whole number of minutes."""

    code = "session_too_short"


def at_least_minimum(minutes: int) -> int:
    """A computed (AI-generated) length raised to the minimum; longer lengths are kept as they are."""
    return max(MIN_SESSION_MINUTES, int(minutes))


def validate_new_session_minutes(minutes) -> int:
    """The length of a session about to be created, or SessionDurationError."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise SessionDurationError("Session length must be a whole number of minutes.")
    if minutes < MIN_SESSION_MINUTES:
        raise SessionDurationError(f"A study session must be at least {MIN_SESSION_MINUTES} minutes long.")
    return minutes
