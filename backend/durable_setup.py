"""Startup check and setup for durable deployments (run by deployment/start_kaggle.sh).

    python -m backend.durable_setup

On PostgreSQL it: verifies the connection, creates every store's tables (idempotent, never drops),
ensures pgvector and the document_chunks table, checks the private Supabase Storage bucket, and
locks the app's tables away from Supabase's Data API roles. Prints one status line per part and
exits non-zero with a clear message when durable storage is misconfigured. Never prints
DATABASE_URL, SUPABASE_URL or any key.
"""

import sys

from backend import db, document_storage, vector_store
from config import DATABASE_PATH

# Every table the app creates. Only these are locked down; other tables are never touched.
APP_TABLES = (
    "users", "auth_sessions",
    "conversations", "conversation_sources", "messages", "message_citations",
    "indexed_documents", "index_registry_migrations", "document_chunks", "subjects",
    "document_summaries", "summary_jobs",
    "flashcard_sets", "flashcards",
    "app_metadata", "quizzes", "quiz_questions", "quiz_options", "quiz_attempts", "quiz_attempt_answers",
    "quiz_explanations", "quiz_validation_events", "concept_plan_cache", "topic_mastery",
    "study_tasks", "availability_slots", "study_blocks", "schedule_runs", "study_plan_items", "topic_progress",
    "study_plans", "study_plan_materials", "study_sessions", "study_preferences",
    "google_calendar_connections", "google_calendar_event_links", "google_oauth_states",
)
_API_ROLES = ("anon", "authenticated")


def initialize_all_stores() -> None:
    from backend import (auth_store, conversation_store, flashcard_store, google_calendar_store,
                         indexed_document_store, quiz_store, study_planner_store, subject_store, summary_jobs,
                         summary_store)

    auth_store.initialize_auth_store()
    conversation_store.initialize_conversation_store()
    indexed_document_store.initialize_indexed_document_store()
    subject_store.initialize_subject_store()   # created at startup, so lock_down_api_roles covers it
    summary_store.initialize_summary_store()
    summary_jobs.initialize_summary_job_store()
    flashcard_store.initialize_flashcard_store()
    quiz_store.initialize_quiz_store()
    study_planner_store.initialize_study_planner_store()
    google_calendar_store.initialize_google_calendar_store()
    vector_store.initialize_vector_store()


def lock_down_api_roles(connection: db.Connection) -> tuple[int, list[str]]:
    """Enable row level security (no policies) on the app's tables and revoke Supabase's API roles.

    The backend connects as the tables' owner, which bypasses RLS, so the app is unaffected; the
    anon/authenticated roles behind Supabase's Data API can no longer read or write these tables.
    Idempotent. Returns (tables locked, API roles revoked)."""
    present = {row[0] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    ).fetchall()}
    tables = [table for table in APP_TABLES if table in present]
    roles = [row[0] for row in connection.execute(
        f"SELECT rolname FROM pg_roles WHERE rolname IN ({','.join('?' * len(_API_ROLES))}) ORDER BY rolname",
        _API_ROLES,
    ).fetchall()]
    for table in tables:
        connection.executescript(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        if roles:
            connection.executescript(f"REVOKE ALL ON TABLE {table} FROM {', '.join(roles)}")
    return len(tables), roles


def run() -> None:
    if db.selected_backend() != db.POSTGRES:
        print("[setup] backend=sqlite (local development: data stays on this machine)", flush=True)
        return
    db.verify_connection()
    initialize_all_stores()
    print(f"[vectors] backend=pgvector schema={vector_store._vector_schema()} "
          f"model={vector_store.EMBEDDING_MODEL} dimensions={vector_store.EMBEDDING_DIMENSIONS}", flush=True)
    if document_storage.durable_storage_enabled():
        bucket = document_storage.ensure_bucket()
        print(f"[storage] backend=supabase bucket={bucket} private=true", flush=True)
    else:
        print("[storage] WARNING: AI_TUTOR_DOCUMENT_STORAGE=local: original uploads stay on this machine only",
              flush=True)
    with db.connect(DATABASE_PATH) as connection:
        locked, roles = lock_down_api_roles(connection)
    revoked = f"; revoked {', '.join(roles)}" if roles else ""
    print(f"[security] row level security enabled on {locked} app tables{revoked}", flush=True)


def main() -> int:
    try:
        run()
    except (db.DatabaseError, vector_store.VectorStoreUnavailableError,
            document_storage.DocumentStorageError) as error:
        message = str(error)
        print(message if message.startswith("[") else f"[setup] {message}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
