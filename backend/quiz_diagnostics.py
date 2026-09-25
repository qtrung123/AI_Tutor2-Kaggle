"""Request-scoped performance instrumentation for Quiz generation.

This module is diagnostic-only: it measures where time and LLM calls go inside
the existing Quiz generation pipeline (backend/quiz_service.py,
backend/assessment_planner.py). It never changes prompts, models, retry
limits, validation rules, or generated quiz content -- it only observes and
logs.

Every call site that reports into this module first fetches the "current"
recorder via get_current(). Outside of a generate_quiz() request (e.g. when
internal functions are unit-tested directly, or on the dead/legacy code
paths), get_current() returns a shared no-op recorder, so instrumented code
never needs an `if diagnostics:` check and behaves identically whether or not
a real run is active.

Nothing here is persisted to a database -- it is in-memory for the lifetime
of one request and is only ever printed as structured log lines, per the
"do not add persistent telemetry storage yet" requirement.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Record shapes
# ---------------------------------------------------------------------------

# Maps an LLM call's reported "stage" to the summary counter it increments.
# "generator" covers both the initial batch call and same-model repair/fill
# retries -- retries are additionally counted separately via retry_count
# (see record_retry) since a retry is a generator call made *because of* a
# prior rejection, not a distinct kind of call.
_STAGE_TO_CALL_COUNTER = {
    "planner": "planner_calls",
    "generator": "generator_calls",
    "semantic_validator": "validator_calls",
    "repair": "repair_calls",
}


@dataclass
class LlmCallRecord:
    stage: str  # "planner" | "generator" | "semantic_validator" | "repair"
    model: str
    elapsed_ms: int
    success: bool
    concept_id: str | None = None
    attempt: int = 1
    exception_type: str | None = None
    reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    started_at: str = ""


@dataclass
class RetryRecord:
    concept_id: str
    original_attempt: int
    retry_attempt: int
    reason: str
    stage: str
    elapsed_ms: int


@dataclass
class ContextRecord:
    stage: str  # "planner" | "generator" | "semantic_validator"
    num_chunks: int
    total_chars: int
    prompt_chars: int | None
    concept_id: str | None = None


@dataclass
class RetrievedChunksRecord:
    total_chunks: int
    unique_chunks: int
    evidence_chunk_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------

class QuizRunDiagnostics:
    """Accumulates timing/call/retry data for exactly one generate_quiz() call."""

    def __init__(self, request_id: str, **meta: Any):
        self.request_id = request_id
        self.meta = meta
        self._run_started = time.perf_counter()
        self.stage_ms: dict[str, float] = {}
        self.llm_calls: list[LlmCallRecord] = []
        self.retries: list[RetryRecord] = []
        self.context_records: list[ContextRecord] = []
        self.retrieved_chunks: RetrievedChunksRecord | None = None
        self.counts: dict[str, int] = {}
        self.failure: dict[str, str] | None = None

    # -- stage timing ------------------------------------------------------

    def add_stage_ms(self, name: str, ms: float) -> None:
        """Accumulate elapsed time into a named stage bucket (additive, like
        the existing timings dicts elsewhere in quiz_service.py)."""
        self.stage_ms[name] = self.stage_ms.get(name, 0) + max(0.0, ms)

    def set_stage_ms(self, name: str, ms: float) -> None:
        self.stage_ms[name] = max(0.0, ms)

    def absorb_pipeline_timings(self, timings: dict) -> None:
        """Pull the standardized stage buckets out of an existing quiz_service.py
        `timings` dict (document- or topic-scope shape) without assuming every
        key is present -- tests frequently substitute a minimal mocked dict for
        the inner generation functions, so every lookup here is defensive."""
        def get(*keys, default=0):
            for key in keys:
                if key in timings and timings[key] is not None:
                    return timings[key]
            return default

        self.add_stage_ms("retrieval_ms", get("topic_chunk_retrieval_ms"))
        self.add_stage_ms(
            "context_ms",
            get("slot_build_ms") + get("evidence_selection_ms") + get("prompt_construction_ms")
            + get("context_grouping_ms"),
        )
        self.add_stage_ms("planner_ms", get("concept_planning_ms"))
        self.add_stage_ms("allocation_ms", get("allocation_ms"))
        self.add_stage_ms(
            "generation_ms",
            get("generation_ms") + (
                0 if "generation_ms" in timings else
                get("initial_batch_generation_ms") + get("repair_generation_ms") + get("fill_generation_ms")
            ),
        )
        self.add_stage_ms("validation_ms", get("validation_ms"))
        self.add_stage_ms("retry_ms", get("repair_ms") + (
            0 if "repair_ms" in timings else get("repair_generation_ms") + get("fill_generation_ms")
        ))
        self.add_stage_ms("persistence_ms", get("persistence_ms"))
        # Take the max, not a sum: when the real generation pipeline ran, record_llm_call already
        # counted every call as it happened, and the timings dict's own "llm_calls" reports the
        # same total -- adding them would double-count. When a test replaces the whole inner
        # generation function with a mock (so record_llm_call is never invoked), counts["llm_calls"]
        # stays 0 and this timings-reported total is the only source of truth.
        self.counts["llm_calls"] = max(self.counts.get("llm_calls", 0), int(get("llm_calls")))

    # -- context / retrieval -------------------------------------------------

    def record_context(self, stage: str, chunks: list[dict] | None = None, prompt: str | None = None,
                        concept_id: str | None = None, num_chunks: int | None = None,
                        total_chars: int | None = None) -> None:
        if chunks is not None:
            num_chunks = len(chunks)
            total_chars = sum(len(str(chunk.get("content") or "")) for chunk in chunks)
        record = ContextRecord(
            stage=stage, num_chunks=num_chunks or 0, total_chars=total_chars or 0,
            prompt_chars=len(prompt) if prompt is not None else None, concept_id=concept_id,
        )
        self.context_records.append(record)
        print(
            f"[QUIZ][CONTEXT] stage={record.stage} concept={record.concept_id or '-'} "
            f"chunks={record.num_chunks} chars={record.total_chars} "
            f"prompt_chars={record.prompt_chars if record.prompt_chars is not None else 'null'}"
        )

    def record_retrieved_chunks(self, chunks: list[dict]) -> None:
        ids = [str((chunk.get("metadata") or {}).get("chunk_id") or "") for chunk in chunks]
        unique_ids = list(dict.fromkeys(chunk_id for chunk_id in ids if chunk_id))
        self.retrieved_chunks = RetrievedChunksRecord(
            total_chunks=len(chunks), unique_chunks=len(unique_ids), evidence_chunk_ids=unique_ids,
        )
        self.counts["retrieved_chunks"] = len(chunks)

    # -- LLM calls -----------------------------------------------------------

    def record_llm_call(
        self, stage: str, model: str, elapsed_ms: float, success: bool,
        concept_id: str | None = None, attempt: int = 1, exception_type: str | None = None,
        reason: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None,
    ) -> LlmCallRecord:
        record = LlmCallRecord(
            stage=stage, model=model, elapsed_ms=round(elapsed_ms), success=success,
            concept_id=concept_id, attempt=attempt, exception_type=exception_type, reason=reason,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        self.llm_calls.append(record)
        counter = _STAGE_TO_CALL_COUNTER.get(stage)
        if counter:
            self.counts[counter] = self.counts.get(counter, 0) + 1
        self.counts["llm_calls"] = self.counts.get("llm_calls", 0) + 1

        tag = {"planner": "PLANNER", "generator": "GENERATOR",
               "semantic_validator": "VALIDATOR", "repair": "GENERATOR"}.get(stage, stage.upper())
        result = "PASS" if success else "FAIL"
        lines = [f"[QUIZ][{tag}]"]
        if record.concept_id:
            lines.append(f"concept={record.concept_id}")
        lines.append(f"attempt={record.attempt}")
        lines.append(f"model={record.model}")
        lines.append(f"elapsed={record.elapsed_ms}ms")
        lines.append(f"result={result}")
        if not success and (reason or exception_type):
            lines.append(f"reason={exception_type or ''}{': ' + reason if reason else ''}")
        if input_tokens is not None or output_tokens is not None:
            lines.append(f"tokens_in={input_tokens if input_tokens is not None else 'null'}")
            lines.append(f"tokens_out={output_tokens if output_tokens is not None else 'null'}")
        print(" ".join(lines))
        return record

    # -- retries ---------------------------------------------------------------

    def record_retry(self, concept_id: str, original_attempt: int, retry_attempt: int,
                      reason: str, stage: str, elapsed_ms: float = 0) -> RetryRecord:
        record = RetryRecord(
            concept_id=concept_id, original_attempt=original_attempt, retry_attempt=retry_attempt,
            reason=reason, stage=stage, elapsed_ms=round(elapsed_ms),
        )
        self.retries.append(record)
        print(
            f"[QUIZ][RETRY] concept={concept_id} attempt={retry_attempt} "
            f"stage={stage} reason={reason[:160]}"
        )
        return record

    # -- counts / failure --------------------------------------------------

    def set_counts(self, **kwargs: int) -> None:
        for key, value in kwargs.items():
            if value is not None:
                self.counts[key] = value

    def record_failure(self, exception_type: str, message: str) -> None:
        self.failure = {"exception_type": exception_type, "message": message[:300]}

    # -- start/summary -------------------------------------------------------

    def log_start(self) -> None:
        meta = " ".join(f"{key}={value}" for key, value in self.meta.items() if value is not None)
        print(f"[QUIZ][START] request={self.request_id} {meta}".rstrip())

    def summary(self) -> dict:
        total_ms = round((time.perf_counter() - self._run_started) * 1000)
        stage = {name: round(value) for name, value in self.stage_ms.items()}

        retry_generator = sum(1 for call in self.llm_calls if call.stage in ("generator", "repair") and call.attempt > 1)
        retry_from_records = len(self.retries)

        result = {
            "total_ms": total_ms,
            "retrieval_ms": stage.get("retrieval_ms", 0),
            "context_ms": stage.get("context_ms", 0),
            "planner_ms": stage.get("planner_ms", 0),
            "allocation_ms": stage.get("allocation_ms", 0),
            "generation_ms": stage.get("generation_ms", 0),
            "validation_ms": stage.get("validation_ms", 0),
            "semantic_validation_ms": stage.get("semantic_validation_ms", 0),
            "retry_ms": stage.get("retry_ms", 0),
            "persistence_ms": stage.get("persistence_ms", 0),
            "cache_lookup_ms": stage.get("cache_lookup_ms", 0),

            "llm_calls": self.counts.get("llm_calls", 0),
            "planner_calls": self.counts.get("planner_calls", 0),
            "generator_calls": self.counts.get("generator_calls", 0),
            "validator_calls": self.counts.get("validator_calls", 0),
            "repair_calls": self.counts.get("repair_calls", 0),

            "retry_count": max(retry_from_records, retry_generator),

            "requested_questions": self.counts.get("requested_questions"),
            "generated_questions": self.counts.get("generated_questions"),
            "validated_questions": self.counts.get("validated_questions"),

            "retrieved_chunks": self.counts.get("retrieved_chunks", 0),
            "unique_chunks": self.retrieved_chunks.unique_chunks if self.retrieved_chunks else 0,
            "context_chars": sum(record.total_chars for record in self.context_records),

            "cache_hit": self.counts.get("cache_hit"),
        }
        if self.failure:
            result["failure"] = self.failure
        return result

    def log_summary(self) -> dict:
        data = self.summary()
        sink = _summary_sink.get()
        if sink is not None:
            sink.append(data)
        print(
            "[QUIZ][SUMMARY]\n"
            f"  request={self.request_id}\n"
            f"  total={data['total_ms']}ms\n"
            f"  cache_lookup={data['cache_lookup_ms']}ms cache_hit={data['cache_hit']}\n"
            f"  retrieval={data['retrieval_ms']}ms context={data['context_ms']}ms\n"
            f"  planner={data['planner_ms']}ms allocation={data['allocation_ms']}ms\n"
            f"  generation={data['generation_ms']}ms validation={data['validation_ms']}ms "
            f"semantic_validation={data['semantic_validation_ms']}ms\n"
            f"  retry={data['retry_ms']}ms persistence={data['persistence_ms']}ms\n"
            f"  llm_calls={data['llm_calls']} (planner={data['planner_calls']} "
            f"generator={data['generator_calls']} validator={data['validator_calls']} "
            f"repair={data['repair_calls']}) retries={data['retry_count']}\n"
            f"  questions requested={data['requested_questions']} generated={data['generated_questions']} "
            f"validated={data['validated_questions']}\n"
            f"  chunks retrieved={data['retrieved_chunks']} unique={data['unique_chunks']} "
            f"context_chars={data['context_chars']}\n"
            f"  json={json.dumps(data, ensure_ascii=False)}"
        )
        return data


class _NullDiagnostics:
    """No-op stand-in used whenever no generate_quiz() request is currently
    running (e.g. a unit test calling an inner function directly, or a
    dead/legacy code path). Every method mirrors QuizRunDiagnostics but does
    nothing and logs nothing, so instrumented call sites never need to check
    whether a real run is active."""

    def add_stage_ms(self, *_args, **_kwargs) -> None: ...
    def set_stage_ms(self, *_args, **_kwargs) -> None: ...

    def absorb_pipeline_timings(self, *_args, **_kwargs) -> None: ...
    def record_context(self, *_args, **_kwargs) -> None: ...
    def record_retrieved_chunks(self, *_args, **_kwargs) -> None: ...

    def record_llm_call(self, *_args, **_kwargs) -> None:
        return None

    def record_retry(self, *_args, **_kwargs) -> None:
        return None

    def set_counts(self, **_kwargs) -> None: ...
    def record_failure(self, *_args, **_kwargs) -> None: ...
    def log_start(self) -> None: ...

    def summary(self) -> dict:
        return {}

    def log_summary(self) -> dict:
        return {}


_NULL = _NullDiagnostics()
_current: ContextVar[Any] = ContextVar("quiz_diagnostics_current", default=_NULL)
# Optional in-memory collector for finished run summaries (used by the admin Quiz model benchmark,
# see backend/model_benchmark_service.py). Unset by default, so normal requests only log.
_summary_sink: ContextVar[list | None] = ContextVar("quiz_diagnostics_summary_sink", default=None)


def get_current() -> "QuizRunDiagnostics | _NullDiagnostics":
    """Fetch the active diagnostics recorder, or a no-op stand-in if no
    generate_quiz() request is currently running on this task/thread."""
    return _current.get()


@contextmanager
def start_run(request_id: str, **meta: Any):
    """Start one request-scoped diagnostics recorder. Only the public
    generate_quiz() entry point in quiz_service.py should call this."""
    diag = QuizRunDiagnostics(request_id=request_id, **meta)
    token = _current.set(diag)
    try:
        yield diag
    finally:
        _current.reset(token)


@contextmanager
def capture_summaries():
    """Collect the summary() dict of every run that finishes (success or failure) inside this block."""
    collected: list[dict] = []
    token = _summary_sink.set(collected)
    try:
        yield collected
    finally:
        _summary_sink.reset(token)
