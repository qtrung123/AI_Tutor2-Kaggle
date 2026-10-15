"""Opt-in per-request performance diagnostics (off by default).

AI_TUTOR_PERF_TIMING=1 adds a Server-Timing header to every HTTP response and prints one line per
request:

    [perf] GET /api/dashboard status=200 total_ms=812.4 db_units=5 db_statements=6 db_wait_ms=3.1 db_ms=640.2

- total_ms: backend time until the response headers are sent (Chrome's "Waiting for server
  response" minus network/tunnel time);
- db_units / db_statements: connect() calls and SQL calls made while serving the request;
- db_wait_ms: time spent opening those units (pool acquisition, health check, or SQLite open);
- db_ms: time spent executing statements, commits and rollbacks.

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
