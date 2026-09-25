"""Shared text-safety helpers for learner-facing generated text (Quiz and Flashcards)."""
import re

from backend.quiz_options import strip_leading_option_label


def _clean_inline_text(value: str) -> str:
    """Collapse model/newline artifacts while keeping code-like text readable."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _looks_like_raw_chunk(option: str) -> bool:
    text = strip_leading_option_label(option)
    words = text.split()
    if len(words) > 32:
        return True
    if len(text) > 190:
        return True
    if text.count(".") >= 3 and len(words) > 22:
        return True
    return False


_FORBIDDEN_FINAL_PHRASES = ("evidence angle", "selected concept", "source-backed")
_RAW_MESSAGE_HEADER = re.compile(
    r"(?:^|\s)(?:from|to|cc|bcc|subject|date|reply-to|message-id)\s*:\s*\S+",
    flags=re.IGNORECASE,
)
_RAW_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", flags=re.IGNORECASE)


def _reject_unsafe_final_text(value: str, *, field: str) -> None:
    """Keep prompt scaffolding and copied message/evidence artifacts out of saved quizzes."""
    text = _clean_inline_text(value)
    lowered = text.lower()
    if any(phrase in lowered for phrase in _FORBIDDEN_FINAL_PHRASES):
        raise ValueError(f"{field} contains forbidden generation scaffolding.")
    if _RAW_MESSAGE_HEADER.search(text) or _RAW_EMAIL.search(text):
        raise ValueError(f"{field} contains a raw header or email address.")
    if _looks_like_raw_chunk(text):
        raise ValueError(f"{field} contains long or malformed raw evidence text.")
