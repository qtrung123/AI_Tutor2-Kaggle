"""Read-only summary of the configured database, for checking persistence across Kaggle restarts.

    python -m deployment.db_status                      # backend + row count per table
    python -m deployment.db_status --email you@x.com    # ...plus that account's user_id and its rows

Run it before and after restarting the Kaggle session: the user_id and the counts must match.
Only SELECTs; never writes, drops or truncates. Never prints DATABASE_URL or credentials.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import db  # noqa: E402
from config import DATABASE_PATH  # noqa: E402

# table -> column holding the owning user's id
OWNED_TABLES = {
    "auth_sessions": "user_id",
    "conversations": "owner_id",
    "indexed_documents": "owner_id",
    "document_chunks": "owner_id",
    "document_summaries": "owner_id",
    "summary_jobs": "owner_id",
    "flashcard_sets": "owner_id",
    "flashcards": "owner_id",
    "quizzes": "owner_id",
    "quiz_attempts": "student_id",
    "topic_mastery": "student_id",
    "study_plans": "owner_id",
    "study_sessions": "owner_id",
    "availability_slots": "owner_id",
    "google_calendar_connections": "owner_id",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", help="also show this account's user_id and its own row counts")
    args = parser.parse_args()
    try:
        with db.connect(DATABASE_PATH) as connection:
            existing = set()
            for table in ("users", *OWNED_TABLES):
                if db.table_columns(connection, table):
                    existing.add(table)
            print(f"[db-status] backend={connection.backend}")
            if "users" not in existing:
                print("[db-status] no tables yet (start the backend once to create them)")
                return 0
            user_id = None
            if args.email:
                row = connection.execute(
                    "SELECT id FROM users WHERE lower(email) = ?", (args.email.strip().lower(),)
                ).fetchone()
                user_id = row["id"] if row else None
                print(f"[db-status] user_id={user_id or 'NOT FOUND'}")
            print(f"[db-status] {'table':<28} {'all rows':>9}" + (f" {'this user':>10}" if user_id else ""))
            for table in ("users", *OWNED_TABLES):
                if table not in existing:
                    print(f"[db-status] {table:<28} {'(missing)':>9}")
                    continue
                total = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                line = f"[db-status] {table:<28} {total:>9}"
                if user_id:
                    column = OWNED_TABLES.get(table, "id")
                    mine = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,)).fetchone()[0]
                    line += f" {mine:>10}"
                print(line)
            if user_id and "indexed_documents" in existing:
                has_vectors = "document_chunks" in existing
                rows = connection.execute(
                    "SELECT d.document_id, d.chunk_count, d.object_path, "
                    + ("(SELECT COUNT(*) FROM document_chunks c WHERE c.owner_id = d.owner_id "
                       "AND c.document_id = d.document_id)" if has_vectors else "0")
                    + " AS vectors FROM indexed_documents d WHERE d.owner_id = ? ORDER BY d.document_id",
                    (user_id,),
                ).fetchall() if "object_path" in db.table_columns(connection, "indexed_documents") else []
                for row in rows:
                    print(f"[db-status] document={row['document_id']} chunks={row['chunk_count']} "
                          f"vectors={row['vectors']} original_stored={'yes' if row['object_path'] else 'no'}")
            connection.rollback()
    except db.DatabaseError as error:
        print(str(error) if str(error).startswith("[db]") else f"[db] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
