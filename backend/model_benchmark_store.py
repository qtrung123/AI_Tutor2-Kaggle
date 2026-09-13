"""Read-only access to offline Quiz model benchmark results.

Results are produced out-of-band by a benchmark script/notebook run -- never
by a live web request -- and stored as one small JSON file. This module only
reads (and, for the offline script's own use, writes) that file; it never
runs inference and is never called from the Quiz generation pipeline.
"""

import json
from pathlib import Path

from config import QUIZ_MODEL_BENCHMARK_RESULTS_PATH

_EMPTY_RESULT = {"generated_at": None, "results": []}


def load_benchmark_results() -> dict:
    """Return the last stored benchmark run, or an empty placeholder shape."""
    path = Path(QUIZ_MODEL_BENCHMARK_RESULTS_PATH)
    if not path.exists():
        return dict(_EMPTY_RESULT)
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return dict(_EMPTY_RESULT)
    if not isinstance(data, dict):
        return dict(_EMPTY_RESULT)
    return {
        "generated_at": data.get("generated_at"),
        "results": [row for row in (data.get("results") or []) if isinstance(row, dict)],
    }


def save_benchmark_results(generated_at: str, results: list[dict]) -> dict:
    """Persist one benchmark run. Called only by the offline benchmark script/notebook."""
    path = Path(QUIZ_MODEL_BENCHMARK_RESULTS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": generated_at, "results": results}
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return payload
