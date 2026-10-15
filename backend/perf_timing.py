"""Opt-in per-request performance diagnostics (off by default).

AI_TUTOR_PERF_TIMING=1 adds a Server-Timing header to every HTTP response and prints one line per
request:

    [perf] GET /api/dashboard status=200 total_ms=812.4 db_units=5 db_statements=6 db_wait_ms=3.1 db_ms=640.2

- total_ms: backend time until the response headers are sent (Chrome's "Waiting for server
  response" minus network/tunnel time);
- db_units / db_statements: connect() calls and SQL calls made while serving the request;
- db_wait_ms: time spent opening those units (pool acquisition, health check, or SQLite open);
- db_ms: time spent executing statements, commits and rollbacks.

The same switch adds one line per Chat / quiz-explanation model call (log_llm_call):

    [llm] call=chat wall_ms=9120.4 load_ms=3.1 prompt_eval_ms=640.2 eval_ms=8300.0 ollama_total_ms=9050.0 prompt_tokens=2150 output_tokens=240 output_tokens_per_s=28.9 done_reason=stop

Only the method, the route template (e.g. /api/flashcards/{document_id}, never the concrete path,
which can carry a document name), the status and numbers are emitted: no SQL text, parameters,
query strings, credentials or document content.
"""

import os
import time

from starlette.datastructures import Headers, MutableHeaders

from backend import db


def enabled() -> bool:
    return os.getenv("AI_TUTOR_PERF_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


_NS_DURATIONS = (("load_ms", "load_duration"), ("prompt_eval_ms", "prompt_eval_duration"),
                 ("eval_ms", "eval_duration"), ("ollama_total_ms", "total_duration"))
_COUNTS = (("prompt_tokens", "prompt_eval_count"), ("output_tokens", "eval_count"))


def llm_timing(response, elapsed_seconds: float) -> dict:
    """Model timings from a chat response's metadata: Ollama's nanosecond durations as ms and its
    token counts, each only when present (vLLM reports token counts but no per-phase durations).
    `load_ms` well above zero means the model was (re)loaded for this call."""
    metadata = getattr(response, "response_metadata", None) or {}
    timing = {"wall_ms": round(elapsed_seconds * 1000, 1)}
    for name, key in _NS_DURATIONS:
        if isinstance(metadata.get(key), (int, float)):
            timing[name] = round(metadata[key] / 1_000_000, 1)
    for name, key in _COUNTS:
        if isinstance(metadata.get(key), int):
            timing[name] = metadata[key]
    if timing.get("output_tokens") and timing.get("eval_ms"):
        timing["output_tokens_per_s"] = round(timing["output_tokens"] / (timing["eval_ms"] / 1000), 1)
    return timing


def log_llm_call(call: str, response, elapsed_seconds: float, **extra) -> None:
    """With AI_TUTOR_PERF_TIMING=1, print one [llm] line per model call: the call site, numbers and
    the backend's done reason only (no prompt, answer or document content). `extra` adds numeric
    fields such as first_token_ms for a streamed call (None values are left out); for a stream,
    `response` is its last chunk, which carries the metadata."""
    if not enabled():
        return
    timing = {**llm_timing(response, elapsed_seconds), **{k: v for k, v in extra.items() if v is not None}}
    metadata = getattr(response, "response_metadata", None) or {}
    fields = " ".join(f"{key}={value}" for key, value in timing.items())
    print(f"[llm] call={call} {fields} done_reason={metadata.get('done_reason')}")


def server_timing(total_seconds: float, stats: db.RequestStats) -> str:
    return (
        f"app;dur={total_seconds * 1000:.1f}, "
        f"db;dur={stats.db_seconds * 1000:.1f};desc=\"{stats.units} units {stats.statements} statements\", "
        f"dbwait;dur={stats.wait_seconds * 1000:.1f}"
    )


class PerfTimingMiddleware:
    """Pure ASGI middleware, so the statistics context reaches the threadpool that runs sync routes."""

    def __init__(self, app, allowed_origins: tuple[str, ...] | list[str] = ()):
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        stats, token = db.start_request_stats()
        started = time.perf_counter()
        status = {"code": 0, "total": 0.0}
        origin = Headers(scope=scope).get("origin")

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                status["total"] = time.perf_counter() - started
                headers = MutableHeaders(scope=message)
                headers.append("Server-Timing", server_timing(status["total"], stats))
                if origin and origin in self.allowed_origins:
                    # Lets the frontend origin's DevTools / PerformanceResourceTiming read the header.
                    headers.append("Timing-Allow-Origin", origin)
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            db.stop_request_stats(token)
            route = scope.get("route")
            path = getattr(route, "path", None) or "<unmatched>"
            print(
                f"[perf] {scope.get('method', '-')} {path} status={status['code']} "
                f"total_ms={status['total'] * 1000:.1f} db_units={stats.units} db_statements={stats.statements} "
                f"db_wait_ms={stats.wait_seconds * 1000:.1f} db_ms={stats.db_seconds * 1000:.1f}",
                flush=True,
            )
