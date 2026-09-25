"""Parse the JSON object an LLM reply is expected to contain (shared by the flashcard and summary services)."""

import json
import re


def parse_json_object(content: str, source: str) -> dict:
    """Return the JSON object in `content`, tolerating ```json fences and surrounding prose.

    `source` names the caller in error messages ("Flashcard" -> "Flashcard model did not return
    valid JSON."). Raises ValueError when there is no JSON object.
    """
    cleaned = str(content).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError(f"{source} model did not return valid JSON.")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError(f"{source} model did not return a JSON object.")
    return value
