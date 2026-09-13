"""Admin-only Quiz model comparison.

Merges the fixed roster of the 3 models covered by the Quiz model benchmark
with whatever offline benchmark results are currently stored. This module
never runs a benchmark and never calls an LLM -- it only reads
backend.model_benchmark_store and reports each model's production/candidate
status relative to the current QUIZ_DEFAULT_GENERATION_MODEL.
"""

from backend.model_benchmark_store import load_benchmark_results
from config import QUIZ_DEFAULT_GENERATION_MODEL

# The 3 models covered by the Quiz model benchmark (see benchmarks/ once the
# offline script exists). Kept as a fixed roster so the comparison table
# always shows all 3, even for a model that has not been measured yet.
QUIZ_BENCHMARK_MODELS = (
    {"model_id": "qwen3-8b", "label": "Qwen3 8B"},
    {"model_id": "qwen-2.5-7b", "label": "Qwen2.5 7B Instruct"},
    {"model_id": "qwen-2.5-3b", "label": "Qwen2.5 3B Instruct"},
)

# Measured, objective fields only -- no subjective/AI-judged quality score.
METRIC_FIELDS = (
    "success_rate",
    "valid_question_rate",
    "grounding_rate",
    "avg_latency_seconds",
    "avg_retries",
    "vram_mb",
    "questions_per_minute",
)


def get_quiz_model_comparison() -> dict:
    """Return the roster of benchmarked Quiz models plus their stored metrics."""
    stored = load_benchmark_results()
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

    return {"generated_at": stored["generated_at"], "models": models}
