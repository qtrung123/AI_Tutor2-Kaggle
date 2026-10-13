"""Background Summary generation jobs: start -> return quickly -> generate in a worker -> poll.

A Summary generation runs two long LLM calls, longer than Cloudflare keeps one HTTP request open
(524). The job table only tracks generation STATE; the finished summary itself is persisted by the
existing generate_document_summary/save_summary path and referenced here by summary_id.

Single-process deployment (Kaggle): a bounded in-process thread pool runs the jobs, and the set of
job ids this process is actually running decides staleness - a "processing" row that no live worker
owns (e.g. the Kaggle session restarted mid-generation) is marked failed instead of staying stuck.
"""

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from backend import db
from backend.auth_store import initialize_auth_store
from backend.indexed_document_store import get_indexed_document
from backend.model_registry import prepare_generation_model
from backend.summary_service import SUMMARY_VERSION, generate_document_summary
from config import DATABASE_PATH, DEFAULT_GENERATION_MODEL

MAX_SUMMARY_WORKERS = 1   # one GPU on Kaggle: Summary generations queue instead of competing
MAX_ERROR_MESSAGE_CHARS = 500

_executor = ThreadPoolExecutor(max_workers=MAX_SUMMARY_WORKERS, thread_name_prefix="summary-job")
_lock = threading.Lock()
_live_jobs: set[str] = set()


def _connect() -> db.Connection:
    return db.connect(DATABASE_PATH)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_jobs (
    job_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    regenerate INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    summary_id TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_jobs_one_processing
    ON summary_jobs(owner_id, fingerprint) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_summary_jobs_document
    ON summary_jobs(owner_id, document_id, created_at DESC);
"""


def initialize_summary_job_store() -> None:
    initialize_auth_store()
    db.ensure_schema("summary_jobs", DATABASE_PATH, lambda connection: connection.executescript(_SCHEMA))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(row) -> dict | None:
    return dict(row) if row else None


def summary_job_fingerprint(owner_id: str, document_id: str, model_id: str | None) -> str:
    """Identity of "this document's summary, as the current generator would produce it"."""
    document = get_indexed_document(owner_id, document_id)
    if not document:
        raise ValueError("Document not found.")
    return "|".join((document_id, model_id or DEFAULT_GENERATION_MODEL, str(document.get("hash") or ""),
                     str(int(document.get("topic_schema_version", 0))), SUMMARY_VERSION))


def _update(job_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{name}=?" for name in fields)
    with _connect() as connection:
        connection.execute(f"UPDATE summary_jobs SET {assignments} WHERE job_id=?", (*fields.values(), job_id))


def _fail_if_orphaned(job: dict | None) -> dict | None:
    """A processing job no live worker in this process owns can never finish: fail it (retryable)."""
    if job and job["status"] == "processing" and job["job_id"] not in _live_jobs:
        _update(job["job_id"], status="failed", error_code="interrupted",
                error_message="Summary generation was interrupted (the server restarted). Please try again.")
        print(f"[summary-job] failed job_id={job['job_id']} seconds=0 error=interrupted")
        with _connect() as connection:
            job = _decode(connection.execute("SELECT * FROM summary_jobs WHERE job_id=?", (job["job_id"],)).fetchone())
    return job


def _processing_job(connection: db.Connection, owner_id: str, fingerprint: str) -> dict | None:
    return _decode(connection.execute(
        "SELECT * FROM summary_jobs WHERE owner_id=? AND fingerprint=? AND status='processing'",
        (owner_id, fingerprint),
    ).fetchone())


def get_summary_job(owner_id: str, job_id: str) -> dict | None:
    """Owner-scoped job lookup: another user's job id is reported as missing."""
    initialize_summary_job_store()
    with _lock:
        with _connect() as connection:
            job = _decode(connection.execute(
                "SELECT * FROM summary_jobs WHERE job_id=? AND owner_id=?", (job_id, owner_id)).fetchone())
        return _fail_if_orphaned(job)


def find_active_summary_job(owner_id: str, document_id: str, model_id: str | None) -> dict | None:
    """The live processing job for this document's current summary identity, if any (refresh/reopen)."""
    initialize_summary_job_store()
    fingerprint = summary_job_fingerprint(owner_id, document_id, model_id)
    with _lock:
        with _connect() as connection:
            job = _processing_job(connection, owner_id, fingerprint)
        job = _fail_if_orphaned(job)
    return job if job and job["status"] == "processing" else None


def start_summary_job(owner_id: str, document_id: str, model_id: str | None, regenerate: bool = False) -> tuple[dict, bool]:
    """Create one processing job and launch it, or return the one already processing.

    Returns (job, created). Never waits for the LLM.
    """
    initialize_summary_job_store()
    selected_model = model_id or DEFAULT_GENERATION_MODEL
    fingerprint = summary_job_fingerprint(owner_id, document_id, selected_model)
    with _lock:
        with _connect() as connection:
            existing = _processing_job(connection, owner_id, fingerprint)
        existing = _fail_if_orphaned(existing)
        if existing and existing["status"] == "processing":
            return existing, False
        job_id, now = str(uuid4()), _now()
        with _connect() as connection:
            connection.execute(
                """INSERT INTO summary_jobs (job_id, owner_id, document_id, model_id, fingerprint, regenerate,
                       status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)""",
                (job_id, owner_id, document_id, selected_model, fingerprint, int(bool(regenerate)), now, now),
            )
            job = _decode(connection.execute("SELECT * FROM summary_jobs WHERE job_id=?", (job_id,)).fetchone())
        _live_jobs.add(job_id)
    print(f"[summary-job] queued job_id={job_id} document_id={document_id}")
    try:
        _executor.submit(_run_summary_job, job_id, owner_id, document_id, selected_model, bool(regenerate))
    except Exception as error:   # executor unavailable: never leave the row processing
        _finish_failed(job_id, time.perf_counter(), error)
        raise
    return job, True


def _error_details(error: Exception) -> tuple[str, str]:
    message = str(error).strip()
    if isinstance(error, ValueError):
        code = "document_not_found" if message == "Document not found." else "invalid_summary"
    else:
        code, message = "generation_failed", f"Could not generate document summary: {message or type(error).__name__}"
    return code, (message or "Could not generate document summary.")[:MAX_ERROR_MESSAGE_CHARS]


def _finish_failed(job_id: str, started: float, error: Exception) -> None:
    code, message = _error_details(error)
    try:
        _update(job_id, status="failed", error_code=code, error_message=message)
    finally:
        _live_jobs.discard(job_id)
    print(f"[summary-job] failed job_id={job_id} seconds={time.perf_counter() - started:.1f} error={code}")


def _run_summary_job(job_id: str, owner_id: str, document_id: str, model_id: str, regenerate: bool) -> None:
    started = time.perf_counter()
    print(f"[summary-job] started job_id={job_id}")
    try:
        prepare_generation_model(model_id)
        stored = generate_document_summary(owner_id, document_id, model_id=model_id, regenerate=regenerate)
        _update(job_id, status="complete", summary_id=stored.get("summary_id"))
        _live_jobs.discard(job_id)
        print(f"[summary-job] completed job_id={job_id} seconds={time.perf_counter() - started:.1f}")
    except Exception as error:
        traceback.print_exc()
        _finish_failed(job_id, started, error)
