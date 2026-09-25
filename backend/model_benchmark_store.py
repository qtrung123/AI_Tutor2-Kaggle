"""Storage for Quiz model benchmark results.

One JSON file holds the latest benchmark: its config, every raw run and the
per-model aggregates. It is written by backend/model_benchmark_service.py
(after every finished run, so progress survives a reload) and read by the
admin Model Comparison page. This module never runs inference and is never
called from the Quiz generation pipeline.
"""

import json
import os
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
    """Persist aggregate rows only (the benchmark service itself saves full state via save_benchmark_state)."""
    path = Path(QUIZ_MODEL_BENCHMARK_RESULTS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": generated_at, "results": results}
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return payload


def load_benchmark_state() -> dict:
    """Return the full stored benchmark (config, status, progress, raw runs, aggregates)."""
    path = Path(QUIZ_MODEL_BENCHMARK_RESULTS_PATH)
    data: dict = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
    return {
        **data,
        "generated_at": data.get("generated_at"),
        "results": [row for row in (data.get("results") or []) if isinstance(row, dict)],
        "runs": [row for row in (data.get("runs") or []) if isinstance(row, dict)],
        "status": data.get("status"),
        "config": data.get("config") if isinstance(data.get("config"), dict) else None,
        "progress": data.get("progress") if isinstance(data.get("progress"), dict) else None,
    }


def save_benchmark_state(state: dict) -> dict:
    """Persist the whole benchmark state atomically (write a temp file, then replace)."""
    path = Path(QUIZ_MODEL_BENCHMARK_RESULTS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return state
