"""Document-centric Study Planner persistence (plans, plan materials, study sessions)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, study_planner_service, study_planner_store as store


def session(document_id, activity="summary", start="2026-09-24T18:00:00", end="2026-09-24T19:00:00",
            duration=60, **extra):
    return {"document_id": document_id, "activity_type": activity, "scheduled_start": start,
            "scheduled_end": end, "duration_minutes": duration, **extra}


class StudyPlanPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-plans@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-plans@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def plan_with_docs(self, owner, *document_ids):
        plan = store.create_plan(owner, "Finals")
        for document_id in document_ids:
            store.add_material(owner, plan["plan_id"], document_id)
        return plan

    # -- plans ---------------------------------------------------------------

    def test_create_read_update_delete_plan(self):
        plan = store.create_plan(self.alice, "  Midterms  ")
        self.assertEqual((plan["title"], plan["status"]), ("Midterms", "active"))
        self.assertEqual(store.get_plan(self.alice, plan["plan_id"]), plan)
        self.assertEqual([p["plan_id"] for p in store.list_plans(self.alice)], [plan["plan_id"]])

        updated = store.update_plan(self.alice, plan["plan_id"], {"title": "Finals", "status": "paused"})
        self.assertEqual((updated["title"], updated["status"]), ("Finals", "paused"))
        self.assertGreaterEqual(updated["updated_at"], plan["updated_at"])

        for bad in ({"status": "bogus"}, {"title": "  "}, {}):
            with self.assertRaises(ValueError):
                store.update_plan(self.alice, plan["plan_id"], bad)
        with self.assertRaises(ValueError):
            store.create_plan(self.alice, "")

        store.delete_plan(self.alice, plan["plan_id"])
        self.assertIsNone(store.get_plan(self.alice, plan["plan_id"]))

    def test_plans_are_owner_isolated(self):
        plan = self.plan_with_docs(self.alice, "doc-a")
        store.create_sessions(self.alice, plan["plan_id"], [session("doc-a")])

        self.assertIsNone(store.get_plan(self.bob, plan["plan_id"]))
        self.assertEqual(store.list_plans(self.bob), [])
        self.assertEqual(store.list_materials(self.bob, plan["plan_id"]), [])
        self.assertEqual(store.list_sessions(self.bob), [])
        with self.assertRaises(ValueError):
            store.update_plan(self.bob, plan["plan_id"], {"title": "Stolen"})
        with self.assertRaises(ValueError):
            store.add_material(self.bob, plan["plan_id"], "doc-b")
        with self.assertRaises(ValueError):
            store.create_sessions(self.bob, plan["plan_id"], [session("doc-a")])
        with self.assertRaises(ValueError):
            store.delete_plan(self.bob, plan["plan_id"])

        material = store.list_materials(self.alice, plan["plan_id"])[0]
        sess = store.list_sessions(self.alice)[0]
        self.assertIsNone(store.get_material(self.bob, material["material_id"]))
        self.assertIsNone(store.get_session(self.bob, sess["session_id"]))
        with self.assertRaises(ValueError):
            store.update_material(self.bob, material["material_id"], {"learning_state": "completed"})
        with self.assertRaises(ValueError):
            store.update_session(self.bob, sess["session_id"], {"status": "completed"})
        with self.assertRaises(ValueError):
            store.delete_session(self.bob, sess["session_id"])
        self.assertEqual(store.delete_plan_sessions(self.bob, plan["plan_id"]), 0)
        self.assertEqual(len(store.list_sessions(self.alice)), 1)

        store.reset_planner_data(self.bob)
        self.assertIsNotNone(store.get_plan(self.alice, plan["plan_id"]))
        store.reset_planner_data(self.alice)
        self.assertEqual((store.list_plans(self.alice), store.list_sessions(self.alice)), ([], []))

    # -- materials -----------------------------------------------------------

    def test_multiple_materials_with_optional_deadlines_and_familiarity(self):
        plan = store.create_plan(self.alice, "Finals")
        first = store.add_material(self.alice, plan["plan_id"], "doc-a")
        second = store.add_material(self.alice, plan["plan_id"], "doc-b", deadline="2026-10-01",
                                    familiarity="somewhat_familiar", learning_state="learning")
        self.assertEqual((first["deadline"], first["familiarity"], first["learning_state"]),
                         (None, None, "new"))
        self.assertEqual((second["deadline"], second["familiarity"], second["learning_state"]),
                         ("2026-10-01", "somewhat_familiar", "learning"))
        self.assertEqual([m["document_id"] for m in store.list_materials(self.alice, plan["plan_id"])],
                         ["doc-a", "doc-b"])
        self.assertNotIn("topic_id", first)

        with self.assertRaises(ValueError):
            store.add_material(self.alice, plan["plan_id"], "doc-a")  # duplicate document in one plan
        other_plan = store.create_plan(self.alice, "Other")
        store.add_material(self.alice, other_plan["plan_id"], "doc-a")  # same document, another plan: fine

        updated = store.update_material(self.alice, second["material_id"],
                                        {"deadline": None, "familiarity": None, "learning_state": "needs_review"})
        self.assertEqual((updated["deadline"], updated["familiarity"], updated["learning_state"]),
                         (None, None, "needs_review"))
        self.assertEqual(store.get_plan_material(self.alice, plan["plan_id"], "doc-b")["material_id"],
                         second["material_id"])

    def test_material_field_validation(self):
        plan = store.create_plan(self.alice, "Finals")
        for kwargs in ({"deadline": "next week"}, {"familiarity": "familiar"}, {"learning_state": "not_started"}):
            with self.assertRaises(ValueError):
                store.add_material(self.alice, plan["plan_id"], "doc-a", **kwargs)
        material = store.add_material(self.alice, plan["plan_id"], "doc-a")
        for changes in ({"deadline": "2026-13-01"}, {"learning_state": None}, {}):
            with self.assertRaises(ValueError):
                store.update_material(self.alice, material["material_id"], changes)

    def test_service_add_plan_material_requires_an_owned_document(self):
        plan = store.create_plan(self.alice, "Finals")
        with patch.object(study_planner_service, "get_indexed_document", return_value=None):
            with self.assertRaises(ValueError):
                study_planner_service.add_plan_material(self.alice, plan["plan_id"], "doc-x")
        with patch.object(study_planner_service, "get_indexed_document", return_value={"document_id": "doc-a"}) as lookup:
            material = study_planner_service.add_plan_material(self.alice, plan["plan_id"], "doc-a", deadline="")
        lookup.assert_called_once_with(self.alice, "doc-a")
        self.assertIsNone(material["deadline"])

    # -- sessions ------------------------------------------------------------

    def test_sessions_are_scoped_to_plan_and_document(self):
        plan = self.plan_with_docs(self.alice, "doc-a", "doc-b")
        other = self.plan_with_docs(self.alice, "doc-a")
        created = store.create_sessions(self.alice, plan["plan_id"], [
            session("doc-a", "summary", reason="Start with the summary", priority_snapshot=2.5),
            session("doc-b", "quiz", "2026-09-25T18:00:00", "2026-09-25T18:30:00", 30, artifact_id="quiz-1"),
            session("doc-a", "flashcards", "2026-09-26T18:00:00", "2026-09-26T18:45:00", 45),
        ])
        store.create_session(self.alice, other["plan_id"], session("doc-a", "review"))
        self.assertEqual([s["activity_type"] for s in created], ["summary", "quiz", "flashcards"])
        self.assertEqual((created[0]["status"], created[0]["reason"], created[0]["priority_snapshot"]),
                         ("scheduled", "Start with the summary", 2.5))
        self.assertEqual(created[1]["artifact_id"], "quiz-1")
        self.assertNotIn("topic_id", created[0])

        self.assertEqual(len(store.list_sessions(self.alice, plan_id=plan["plan_id"])), 3)
        self.assertEqual([s["activity_type"] for s in store.list_sessions(self.alice, plan_id=plan["plan_id"], document_id="doc-a")],
                         ["summary", "flashcards"])
        self.assertEqual(len(store.list_sessions(self.alice, document_id="doc-a")), 3)
        window = store.list_sessions(self.alice, plan_id=plan["plan_id"],
                                     start_from="2026-09-25T00:00:00", start_before="2026-09-26T00:00:00")
        self.assertEqual([s["activity_type"] for s in window], ["quiz"])

        with self.assertRaises(ValueError):  # document not a material of this plan
            store.create_sessions(self.alice, other["plan_id"], [session("doc-b")])

        # Removing a material cascades only its own sessions in that plan.
        store.remove_material(self.alice, store.get_plan_material(self.alice, plan["plan_id"], "doc-a")["material_id"])
        self.assertEqual([s["document_id"] for s in store.list_sessions(self.alice, plan_id=plan["plan_id"])], ["doc-b"])
        self.assertEqual(len(store.list_sessions(self.alice, plan_id=other["plan_id"])), 1)
        # Deleting a plan cascades its materials and sessions.
        store.delete_plan(self.alice, plan["plan_id"])
        self.assertEqual(store.list_sessions(self.alice, plan_id=plan["plan_id"]), [])
        self.assertEqual(store.list_materials(self.alice, plan["plan_id"]), [])

    def test_schema_enforces_unique_material_and_composite_session_fk(self):
        """Constraint-level proof, bypassing the store's Python checks: UNIQUE(plan_id, document_id)
        on materials, and study_sessions(plan_id, document_id) -> study_plan_materials with cascade."""
        store.initialize_study_planner_store()
        with store._connect() as connection:
            unique_indexes = [
                [col["name"] for col in connection.execute(f"PRAGMA index_info('{index['name']}')")]
                for index in connection.execute("PRAGMA index_list(study_plan_materials)") if index["unique"]
            ]
            self.assertIn(["plan_id", "document_id"], unique_indexes)
            fks = {}
            for fk in connection.execute("PRAGMA foreign_key_list(study_sessions)"):
                fks.setdefault(fk["id"], []).append((fk["table"], fk["from"], fk["to"], fk["on_delete"]))
            self.assertIn(
                [("study_plan_materials", "plan_id", "plan_id", "CASCADE"),
                 ("study_plan_materials", "document_id", "document_id", "CASCADE")],
                list(fks.values()),
            )

        plan = self.plan_with_docs(self.alice, "doc-a")
        other = self.plan_with_docs(self.alice, "doc-a")  # same document in a different plan: allowed
        now = "2026-09-24T00:00:00"

        def insert_material(plan_id, document_id):
            with store._connect() as connection:
                connection.execute(
                    "INSERT INTO study_plan_materials (material_id, plan_id, owner_id, document_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (f"m-{plan_id}-{document_id}-raw", plan_id, self.alice, document_id, now, now),
                )

        def insert_session(session_id, plan_id, document_id):
            with store._connect() as connection:
                connection.execute(
                    """INSERT INTO study_sessions (session_id, owner_id, plan_id, document_id, activity_type,
                       scheduled_start, scheduled_end, duration_minutes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'summary', '2026-09-24T18:00:00', '2026-09-24T19:00:00', 60, ?, ?)""",
                    (session_id, self.alice, plan_id, document_id, now, now),
                )

        with self.assertRaises(sqlite3.IntegrityError):  # duplicate document in one plan
            insert_material(plan["plan_id"], "doc-a")
        with self.assertRaises(sqlite3.IntegrityError):  # document not a material of that plan
            insert_session("s-bad", plan["plan_id"], "doc-b")
        insert_session("s-a", plan["plan_id"], "doc-a")
        insert_session("s-other", other["plan_id"], "doc-a")

        with store._connect() as connection:  # deleting the material cascades only its own plan's session
            connection.execute("DELETE FROM study_plan_materials WHERE plan_id=? AND document_id='doc-a'", (plan["plan_id"],))
        self.assertEqual([s["session_id"] for s in store.list_sessions(self.alice)], ["s-other"])

    def test_session_activity_status_and_window_validation(self):
        plan = self.plan_with_docs(self.alice, "doc-a")
        bad_sessions = [
            session("doc-a", "reading"),
            session("doc-a", status="done"),
            session("doc-a", end="2026-09-24T18:00:00"),
            session("doc-a", duration=0),
            session("doc-a", duration=61),
            session("doc-a", duration=30.5),
            session("doc-a", start="tomorrow"),
        ]
        for bad in bad_sessions:
            with self.assertRaises(ValueError, msg=bad):
                store.create_sessions(self.alice, plan["plan_id"], [bad])
        # A batch with one bad session persists nothing.
        with self.assertRaises(ValueError):
            store.create_sessions(self.alice, plan["plan_id"], [session("doc-a"), session("doc-a", "reading")])
        self.assertEqual(store.list_sessions(self.alice), [])

        for activity in store.ACTIVITY_TYPES:
            store.create_session(self.alice, plan["plan_id"], session("doc-a", activity))
        sess = store.list_sessions(self.alice)[0]
        for status in store.SESSION_STATUSES:
            self.assertEqual(store.update_session(self.alice, sess["session_id"], {"status": status})["status"], status)
        for changes in ({"status": "paused"}, {"duration_minutes": 90}, {"scheduled_end": "2026-09-24T17:00:00"}, {}):
            with self.assertRaises(ValueError):
                store.update_session(self.alice, sess["session_id"], changes)

    def test_session_lifecycle_timestamps_and_bulk_clear(self):
        plan = self.plan_with_docs(self.alice, "doc-a")
        first, second, third = store.create_sessions(self.alice, plan["plan_id"], [
            session("doc-a"), session("doc-a", "quiz"), session("doc-a", "review"),
        ])
        self.assertIsNone(first["started_at"])
        started = store.update_session(self.alice, first["session_id"], {"status": "in_progress"})
        self.assertIsNotNone(started["started_at"])
        done = store.update_session(self.alice, first["session_id"], {"status": "completed"})
        self.assertEqual(done["started_at"], started["started_at"])
        self.assertIsNotNone(done["completed_at"])

        moved = store.update_session(self.alice, second["session_id"], {
            "scheduled_start": "2026-09-27T09:00:00", "scheduled_end": "2026-09-27T09:30:00",
            "duration_minutes": 30, "status": "rescheduled", "reason": "Moved by user",
        })
        self.assertEqual((moved["scheduled_start"], moved["duration_minutes"], moved["status"]),
                         ("2026-09-27T09:00:00", 30, "rescheduled"))

        self.assertEqual(store.delete_plan_sessions(self.alice, plan["plan_id"]), 1)  # only third was 'scheduled'
        remaining = {s["session_id"] for s in store.list_sessions(self.alice, plan_id=plan["plan_id"])}
        self.assertEqual(remaining, {first["session_id"], second["session_id"]})
        with self.assertRaises(ValueError):
            store.delete_plan_sessions(self.alice, plan["plan_id"], statuses=("bogus",))
        store.delete_session(self.alice, second["session_id"])
        self.assertIsNone(store.get_session(self.alice, second["session_id"]))
        self.assertIsNone(store.get_session(self.alice, third["session_id"]))

    def test_deleting_a_document_removes_it_from_every_plan(self):
        first = self.plan_with_docs(self.alice, "doc-a", "doc-b")
        second = self.plan_with_docs(self.alice, "doc-a")
        bobs = self.plan_with_docs(self.bob, "doc-a")
        store.create_session(self.alice, first["plan_id"], session("doc-a"))
        store.create_session(self.bob, bobs["plan_id"], session("doc-a"))

        store.delete_document_plan_data(self.alice, "doc-a")
        self.assertEqual([m["document_id"] for m in store.list_materials(self.alice, first["plan_id"])], ["doc-b"])
        self.assertEqual(store.list_materials(self.alice, second["plan_id"]), [])
        self.assertEqual(store.list_sessions(self.alice), [])
        self.assertEqual(len(store.list_sessions(self.bob)), 1)

    # -- reused pieces -------------------------------------------------------

    def test_plan_schedule_runs_reuse_schedule_runs_and_cascade(self):
        plan = store.create_plan(self.alice, "Finals")
        store.record_plan_schedule_run(self.alice, plan["plan_id"], reason="initial")
        store.record_plan_schedule_run(self.alice, plan["plan_id"], reason="regenerate")
        self.assertEqual(len(store.list_plan_schedule_runs(self.alice, plan["plan_id"])), 2)
        self.assertEqual(store.list_plan_schedule_runs(self.bob, plan["plan_id"]), [])
        with self.assertRaises(ValueError):
            store.record_plan_schedule_run(self.bob, plan["plan_id"])
        # Legacy task-level runs still work in the same table.
        task = store.create_task(self.alice, "Legacy", "2026-10-01", 60)
        store.record_schedule_run(self.alice, task["task_id"])
        store.delete_plan(self.alice, plan["plan_id"])
        self.assertEqual(store.list_plan_schedule_runs(self.alice, plan["plan_id"]), [])
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0], 1)

    def test_availability_still_works_alongside_plans(self):
        store.create_plan(self.alice, "Finals")
        store.add_availability(self.alice, "18:00", "20:00", date="2026-09-24")
        store.add_availability(self.alice, "09:00", "10:00", is_recurring=True, day_of_week=5)
        store.remove_availability(self.alice, "18:30", "19:00", date="2026-09-24")
        slots = sorted((s["date"], s["start_at"], s["end_at"]) for s in store.list_availability(self.alice)
                       if not s["is_recurring"])
        self.assertEqual(slots, [("2026-09-24", "18:00", "18:30"), ("2026-09-24", "19:00", "20:00")])
        self.assertEqual(store.list_availability(self.bob), [])


class LegacyScheduleRunsMigrationTests(unittest.TestCase):
    """An existing database created before plans existed: schedule_runs.task_id was NOT NULL."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "legacy.db"
        self.patches = [patch.object(auth_store, "DATABASE_PATH", self.db), patch.object(store, "DATABASE_PATH", self.db)]
        for item in self.patches:
            item.start()
        self.owner = auth_store.create_user("Old", "old-planner@example.com", "long-password-o")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_rebuild_keeps_existing_runs_and_task_cascade(self):
        with sqlite3.connect(self.db) as connection:
            connection.executescript("""
                CREATE TABLE study_tasks (
                    task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL, document_id TEXT,
                    deadline TEXT NOT NULL, estimated_minutes INTEGER NOT NULL, remaining_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
                CREATE TABLE schedule_runs (
                    schedule_run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES study_tasks(task_id) ON DELETE CASCADE);
                CREATE INDEX idx_schedule_runs_task ON schedule_runs(task_id, created_at DESC);
            """)
            connection.execute("INSERT INTO study_tasks VALUES ('t1', ?, 'Old', NULL, '2026-10-01', 60, 60, 'active', 'x')",
                               (self.owner,))
            connection.execute("INSERT INTO schedule_runs VALUES ('r1', ?, 't1', 'initial', 'x')", (self.owner,))
        connection.close()

        store.initialize_study_planner_store()
        store.initialize_study_planner_store()  # idempotent
        with store._connect() as connection:
            rows = connection.execute("SELECT schedule_run_id, task_id, plan_id FROM schedule_runs").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("r1", "t1", None)])
            with self.assertRaises(sqlite3.IntegrityError):  # a run must reference a task or a plan
                connection.execute("INSERT INTO schedule_runs (schedule_run_id, owner_id, reason, created_at) "
                                   "VALUES ('r2', ?, 'x', 'x')", (self.owner,))
        store.delete_task(self.owner, "t1")
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
