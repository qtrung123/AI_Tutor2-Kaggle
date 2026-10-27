"""SNAPSHOT-VERIFY: the Kaggle state snapshot (SQLite + Chroma under data/ and vectorstore/) is
complete, consistent and restorable -- verified only on temporary directories, with the Kaggle CLI
replaced by a local "remote" folder (no network, no real Dataset, no real user data).

The snapshot itself is taken with the notebook's own `_copy_live_tree` (read from
kaggle_run_ollama_cloudflare.ipynb), then saved/restored through deployment/kaggle_persist.py:

- a write in flight (rollback journal) or an un-checkpointed WAL while the tree is copied must not
  leave stale -journal / -wal files in the snapshot: SQLite would replay them over the restored
  database and silently roll back everything written after them (integrity_check stays "ok");
- a snapshot saved before this fix (already carrying such files) restores the backed-up data;
- corrupted, incomplete or missing snapshots are refused with a clear reason, never restored;
- an old (pre-Subject) snapshot restores and migrates without changing any existing row.
"""

import contextlib
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planner_fixtures import PlannerDatabaseMixin

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "deployment"))
import kaggle_persist  # noqa: E402

from backend import conversation_store, indexed_document_store, study_planner_store, subject_store  # noqa: E402


def notebook_snapshot_functions() -> dict:
    """`_is_sqlite_file` and `_copy_live_tree` exactly as the Kaggle notebook defines them. SQLite
    connections are closed on leaving `with` here (the notebook relies on Linux, where a file can be
    replaced while still open; Windows cannot)."""
    notebook = json.loads((ROOT / "kaggle_run_ollama_cloudflare.ipynb").read_text(encoding="utf-8"))
    source = next("".join(cell["source"]) for cell in notebook["cells"] if "def _copy_live_tree" in "".join(cell["source"]))
    code = source[source.index("def _is_sqlite_file"):source.index("def save_state_snapshot")]

    class _Closing:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, *exc):
            self.connection.close()
            return False

    shim = type("sqlite3_shim", (), {"connect": staticmethod(lambda *a, **k: _Closing(sqlite3.connect(*a, **k)))})
    namespace = {"shutil": shutil, "sqlite3": shim, "Path": Path}
    exec(compile(code, "notebook-cell", "exec"), namespace)
    return namespace


class FakeKaggle:
    """The `kaggle datasets` CLI over a local folder: version/create copy the staging dir up,
    download copies the latest version down (as --unzip would)."""

    def __init__(self, remote: Path):
        self.remote = remote
        self.calls = []

    def __call__(self, args):
        self.calls.append(args[:2])
        if args[:2] == ["datasets", "download"]:
            if not self.remote.exists():
                return 1, "", "404 - Not Found"
            shutil.copytree(self.remote, Path(args[args.index("-p") + 1]), dirs_exist_ok=True)
            return 0, "", ""
        if args[:2] in (["datasets", "version"], ["datasets", "create"]):
            if self.remote.exists():
                shutil.rmtree(self.remote)
            shutil.copytree(Path(args[args.index("-p") + 1]), self.remote,
                            ignore=shutil.ignore_patterns("dataset-metadata.json"))
            return 0, "Dataset version is being created", ""
        raise AssertionError(f"unexpected kaggle call {args}")


def rows(path: Path, table: str = "t") -> tuple:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT COUNT(*), SUM(v LIKE 'T1-%') FROM {table}").fetchone()
    finally:
        connection.close()


class SnapshotRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="snapshot_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.live = self.root / "live"
        (self.live / "data").mkdir(parents=True)
        (self.live / "vectorstore").mkdir()
        self.kaggle = FakeKaggle(self.root / "remote")
        self.copy_live_tree = notebook_snapshot_functions()["_copy_live_tree"]
        self.run_patch = patch.object(kaggle_persist, "_run", self.kaggle)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def live_database(self, mode: str) -> sqlite3.Connection:
        writer = sqlite3.connect(self.live / "data" / "app.db", isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute(f"PRAGMA journal_mode={mode}")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        for index in range(200):
            writer.execute("INSERT INTO t (v) VALUES (?)", (f"T0-{index}" * 20,))
        vectors = sqlite3.connect(self.live / "vectorstore" / "chroma.sqlite3")
        vectors.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY)")
        vectors.commit()
        vectors.close()
        return writer

    def snapshot(self, between=None) -> Path:
        """The notebook's save: copy the live tree (writes may happen meanwhile), then upload."""
        staging = self.root / "staging"
        original_copytree = shutil.copytree

        def copytree_then_write(source, target, *args, **kwargs):
            result = original_copytree(source, target, *args, **kwargs)
            if between and Path(source).name == "data":
                between()
            return result

        with patch.object(shutil, "copytree", copytree_then_write):
            for name in ("data", "vectorstore"):
                self.copy_live_tree(self.live / name, staging / name)
        kaggle_persist.save_dataset("me/state", staging, title="AI Tutor state", message="test")
        return staging

    def restore(self) -> tuple[bool, str, Path]:
        target = self.root / f"restore_{len(list(self.root.glob('restore_*')))}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = kaggle_persist.restore_dataset("me/state", target)
        return ok, out.getvalue(), target

    def assert_write_in_flight_restores_the_backed_up_data(self, mode: str):
        writer = self.live_database(mode)
        if mode == "DELETE":   # a large write in flight spills pages: a hot journal on disk
            writer.execute("PRAGMA cache_size=5")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE t SET v = 'in-flight-' || hex(randomblob(300))")

        def later_writes():
            if mode == "DELETE":
                writer.execute("COMMIT")
            for index in range(200):
                writer.execute("UPDATE t SET v = ? WHERE id = ?", (f"T1-{index}", index + 1))
            if mode == "WAL":
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        staging = self.snapshot(later_writes)
        self.assertEqual([p.name for p in (staging / "data").iterdir()], ["app.db"])   # no stale sidecar uploaded
        ok, log, restored = self.restore()
        self.assertTrue(ok, log)
        self.assertEqual(rows(restored / "data" / "app.db"), (200, 200))   # every write up to the backup

    def test_rollback_journal_write_in_flight_while_copying(self):
        self.assert_write_in_flight_restores_the_backed_up_data("DELETE")

    def test_uncheckpointed_wal_while_copying(self):
        self.assert_write_in_flight_restores_the_backed_up_data("WAL")
        self.assertTrue((self.live / "data" / "app.db-wal").exists())   # the live database is never touched

    def test_a_snapshot_saved_before_the_fix_drops_its_stale_journal_on_restore(self):
        writer = self.live_database("DELETE")
        writer.execute("UPDATE t SET v = 'T1-' || id")
        # The remote holds an old snapshot: the backed-up database plus the stale journal it carried.
        remote = self.root / "remote" / "data"
        remote.mkdir(parents=True)
        source = sqlite3.connect(self.live / "data" / "app.db")
        backup = sqlite3.connect(remote / "app.db")
        source.backup(backup)
        backup.close()
        source.close()
        shutil.copytree(self.live / "vectorstore", self.root / "remote" / "vectorstore")
        stale = sqlite3.connect(self.root / "stale.db", isolation_level=None)
        stale.execute("PRAGMA cache_size=5")
        stale.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        stale.executemany("INSERT INTO t (v) VALUES (?)", [("x" * 400,)] * 200)
        stale.execute("BEGIN IMMEDIATE")
        stale.execute("UPDATE t SET v = hex(randomblob(300))")
        shutil.copy(self.root / "stale.db-journal", remote / "app.db-journal")
        stale.execute("ROLLBACK")
        stale.close()
        ok, log, restored = self.restore()
        self.assertTrue(ok, log)
        self.assertIn("Removed stale SQLite journal from the snapshot: data/app.db-journal", log)
        self.assertEqual(rows(restored / "data" / "app.db"), (200, 200))

    def test_corrupted_incomplete_and_missing_snapshots_are_refused_with_a_reason(self):
        self.live_database("DELETE")
        self.snapshot()
        database = self.root / "remote" / "data" / "app.db"
        data = database.read_bytes()
        database.write_bytes(data[:100] + b"\x00" * 4096 + data[4196:])   # damaged pages
        ok, log, _ = self.restore()
        self.assertFalse(ok)
        self.assertIn("is not usable and was NOT restored", log)
        self.assertIn("data/app.db", log)

        shutil.rmtree(self.root / "remote" / "data")   # vectorstore only: incomplete state
        ok, log, _ = self.restore()
        self.assertFalse(ok)
        self.assertIn("data/ has no SQLite database (incomplete state snapshot)", log)

        shutil.rmtree(self.root / "remote")   # a never-saved Dataset
        ok, log, _ = self.restore()
        self.assertFalse(ok)
        self.assertIn("No previous version", log)

    def test_saving_refuses_a_corrupted_or_empty_staging_copy(self):
        staging = self.root / "staging"
        (staging / "data").mkdir(parents=True)
        (staging / "data" / "app.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
        with self.assertRaisesRegex(RuntimeError, "Refusing to save me/state: data/app.db"):
            kaggle_persist.save_dataset("me/state", staging, title="AI Tutor state", message="test")
        (staging / "data" / "app.db").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete state snapshot"):
            kaggle_persist.save_dataset("me/state", staging, title="AI Tutor state", message="test")
        self.assertEqual(self.kaggle.calls, [])   # nothing was uploaded

    def test_non_state_cache_dataset_is_unaffected(self):
        cache = self.root / "cache"
        (cache / "pip").mkdir(parents=True)
        (cache / "pip" / "wheel.whl").write_bytes(b"PK")
        kaggle_persist.save_dataset("me/cache", cache, title="AI Tutor build cache", message="test")
        self.assertEqual(self.kaggle.calls, [["datasets", "version"]])


class LegacySnapshotMigrationTests(PlannerDatabaseMixin, unittest.TestCase):
    """A snapshot saved before Subject Management restores and migrates with every row intact."""

    def setUp(self):
        self.start_planner_database()
        self.database = Path(study_planner_store.DATABASE_PATH)
        conversation_patch = patch.object(conversation_store, "DATABASE_PATH", self.database)
        conversation_patch.start()
        self.addCleanup(conversation_patch.stop)
        self.alice, self.bob = self.user("Alice"), self.user("Bob")
        for owner in (self.alice, self.bob):
            self.add_document(owner, "lecture.pdf", "Lecture")
            self.add_summary(owner, "lecture.pdf")
            self.add_flashcards(owner, "lecture.pdf", 3)
            self.add_quiz(owner, "lecture.pdf", f"quiz-{owner[:6]}")
            self.add_attempt(owner, "lecture.pdf", f"quiz-{owner[:6]}", score=7, answered=10, completed=True)
            conversation_store.create_conversation(owner, "Chat", ["lecture.pdf"])
            plan = study_planner_store.create_plan(owner, "Exams")
            study_planner_store.add_material(owner, plan["plan_id"], "lecture.pdf", deadline="2026-10-20")
            study_planner_store.create_session(owner, plan["plan_id"], {
                "document_id": "lecture.pdf", "activity_type": "summary", "scheduled_start": "2026-10-01T18:00:00",
                "scheduled_end": "2026-10-01T18:45:00", "duration_minutes": 45, "reason": "new_material"})
        subject_store.list_subjects(self.alice)   # make sure the current schema exists, then roll it back
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            DROP INDEX IF EXISTS idx_indexed_documents_subject;
            ALTER TABLE indexed_documents DROP COLUMN subject_id;
            DROP TABLE subjects;
        """)
        connection.close()

    def table_rows(self, path: Path) -> dict:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('subjects', 'sqlite_sequence')")]
            return {table: sorted(tuple((key, row[key]) for key in row.keys() if key != "subject_id")
                                  for row in connection.execute(f"SELECT * FROM {table}")) for table in tables}
        finally:
            connection.close()

    def test_pre_subject_snapshot_restores_and_migrates_every_row_intact(self):
        before = self.table_rows(self.database)
        for table in ("users", "indexed_documents", "conversations", "document_summaries", "flashcard_sets",
                      "quizzes", "quiz_attempts", "study_plans", "study_plan_materials", "study_sessions"):
            self.assertTrue(before.get(table), table)   # every feature really has data
        work = Path(tempfile.mkdtemp(prefix="legacy_snapshot_"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        live = work / "live" / "data"
        live.mkdir(parents=True)
        shutil.copy(self.database, live / "conversations.db")
        staging = work / "staging"
        notebook_snapshot_functions()["_copy_live_tree"](live, staging / "data")
        with patch.object(kaggle_persist, "_run", FakeKaggle(work / "remote")):
            kaggle_persist.save_dataset("me/state", staging, title="AI Tutor state", message="test")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(kaggle_persist.restore_dataset("me/state", work / "restored"))
        restored = work / "restored" / "data" / "conversations.db"
        modules = [module for module in sys.modules.values()
                   if getattr(module, "__name__", "").startswith("backend.") and hasattr(module, "DATABASE_PATH")]
        with contextlib.ExitStack() as stack:
            for module in modules:
                stack.enter_context(patch.object(module, "DATABASE_PATH", restored))
            self.assertEqual(subject_store.list_subjects(self.alice), [])   # runs the Subject migration
            documents = indexed_document_store.list_indexed_documents(self.alice)
            self.assertEqual([(d["document_id"], d["subject_id"]) for d in documents], [("lecture.pdf", None)])
            self.assertEqual(len(conversation_store.list_conversations(self.bob)), 1)
            self.assertEqual(len(study_planner_store.list_sessions(self.bob)), 1)
        self.assertEqual(self.table_rows(restored), before)   # no existing row changed, nothing lost
        columns = {row[1] for row in sqlite3.connect(restored).execute("PRAGMA table_info(indexed_documents)")}
        self.assertIn("subject_id", columns)


if __name__ == "__main__":
    unittest.main()
