"""Admin-only Quiz model comparison.

Merges the fixed roster of the models covered by the Quiz model benchmark (Qwen 2.5 7B vs
Gemma 3 12B, see backend/model_benchmark_service.py) with the latest stored benchmark: its config,
status/progress, per-model aggregates and raw runs. This module never runs a benchmark and never
calls an LLM; it reports each model's production/candidate status relative to the current
QUIZ_DEFAULT_GENERATION_MODEL. There is deliberately no combined or "winner" score.
"""

from backend.model_benchmark_service import BENCHMARK_MODELS, DEFAULT_RUNS, MAX_RUNS, current_benchmark_state
from config import QUIZ_DEFAULT_GENERATION_MODEL

QUIZ_BENCHMARK_MODELS = BENCHMARK_MODELS

# Measured, objective fields only -- no subjective/AI-judged quality score.
METRIC_FIELDS = (
    "runs",
    "failures",
    "success_rate",
    "final_question_rate",
    "grounding_rate",
    "avg_latency_seconds",
    "p50_latency_seconds",
    "p95_latency_seconds",
    "avg_retries",
    "avg_tokens_per_second",
    "questions_per_minute",
    # Cold start (warm-up before the measured runs), reported separately from latency.
    "model_prepare_ms",
    "warm_ms",
    "cold_start_ms",
)


def get_quiz_model_comparison() -> dict:
    """Return the benchmarked Quiz models plus the latest stored benchmark."""
    stored = current_benchmark_state()
    measured_by_id = {
        str(row["model_id"]): row for row in stored["results"] if row.get("model_id")
    }

    models = []
    for entry in QUIZ_BENCHMARK_MODELS:
        measured = measured_by_id.get(entry["model_id"])
        row = {
            "model_id": entry["model_id"],
            "label": entry["label"],
            "status": (
                "current_production"
                if entry["model_id"] == QUIZ_DEFAULT_GENERATION_MODEL
                else "benchmark_candidate"
            ),
            "measured": measured is not None,
        }
        for field in METRIC_FIELDS:
            row[field] = (measured or {}).get(field)
        models.append(row)

    return {
        "generated_at": stored["generated_at"],
        "benchmark_status": stored.get("status"),
        "started_at": stored.get("started_at"),
        "finished_at": stored.get("finished_at"),
        "progress": stored.get("progress"),
        "config": stored.get("config"),
        "error": stored.get("error"),
        "defaults": {"runs": DEFAULT_RUNS, "max_runs": MAX_RUNS},
        "models": models,
        "warmups": stored.get("warmups") or [],
        "runs": stored.get("runs") or [],
    }
