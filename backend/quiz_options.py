"""Canonical quiz-option labels shared by generation and persistence."""

import re


_LEADING_OPTION_LABEL = re.compile(
    r"^\s*[ABCD](?:\s*[.):]\s*|\s*-\s*|\s+)(?=\S)",
    flags=re.IGNORECASE,
)


def strip_leading_option_label(value: object) -> str:
    """Strip exactly one model-supplied A/B/C/D label from option text."""
    text = str(value or "").strip()
    return _LEADING_OPTION_LABEL.sub("", text, count=1).strip()


def canonicalize_option(value: object, letter: str) -> str:
    """Return one backend-canonical label followed by the untouched answer body."""
    body = strip_leading_option_label(value)
    return f"{letter}. {body}"
