"""Study Planner study goals per material: "Has deadline" (a required hard deadline) or "Flexible
learning" (no deadline), plus an optional target completion date that is only a preference.

Goals live on the existing study_plan_materials rows (goal_type + target_date next to deadline) and
go through the existing materials API. Materials saved before goals existed stay valid untouched,
and editing a goal never changes sessions that were already accepted.
"""

import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_plan_api_service, study_planner_store
from backend.main import app

UTC_NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
MONDAY_LOCAL = "2026-09-28T08:00:00"
PASSWORD = "long-password-x"


class StudyGoalStoreTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")

    def add(self, document_id="mkt", **fields):
        return study_planner_store.add_material(self.alice, self.plan["plan_id"], document_id, **fields)

    def update(self, material, **changes):
        return study_planner_store.update_material(self.alice, material["material_id"], changes)

    def goal(self, material):
        return {key: material[key] for key in ("goal_type", "deadline", "target_date", "learning_goal")}

    def test_deadline_goal_with_target_date(self):
        material = self.add(goal_type="deadline", deadline="2026-10-10", target_date="2026-10-05")
        self.assertEqual(self.goal(material), {"goal_type": "deadline", "deadline": "2026-10-10",
                                               "target_date": "2026-10-05", "learning_goal": "deadline"})

    def test_flexible_goal_has_no_deadline_but_may_have_a_target(self):
        material = self.add(goal_type="flexible", target_date="2026-11-01")
        self.assertEqual(self.goal(material), {"goal_type": "flexible", "deadline": None,
                                               "target_date": "2026-11-01", "learning_goal": "flexible"})

    def test_invalid_goals_are_refused_and_nothing_is_written(self):
        for fields, message in (
            ({"goal_type": "deadline"}, "needs a deadline"),
            ({"goal_type": "flexible", "deadline": "2026-10-10"}, "Flexible learning has no deadline"),
            ({"goal_type": "deadline", "deadline": "2026-10-10", "target_date": "2026-10-11"}, "cannot be later"),
            ({"deadline": "2026-10-10", "target_date": "2026-10-11"}, "cannot be later"),
            ({"goal_type": "someday"}, "goal_type must be one of"),
            ({"goal_type": "flexible", "target_date": "soon"}, "target_date must be an ISO date"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                self.add(**fields)
        self.assertEqual(study_planner_store.list_materials(self.alice, self.plan["plan_id"]), [])

    def test_target_equal_to_deadline_is_allowed(self):
        material = self.add(goal_type="deadline", deadline="2026-10-10", target_date="2026-10-10")
        self.assertEqual(material["target_date"], "2026-10-10")

    def test_change_deadline_switch_goals_and_remove_goal(self):
        material = self.add(goal_type="deadline", deadline="2026-10-10", target_date="2026-10-05")
        moved = self.update(material, deadline="2026-10-20")
        self.assertEqual((moved["goal_type"], moved["deadline"], moved["target_date"]), ("deadline", "2026-10-20", "2026-10-05"))
        # Choosing flexible removes the hard deadline; the target preference stays.
        flexible = self.update(material, goal_type="flexible")
        self.assertEqual(self.goal(flexible), {"goal_type": "flexible", "deadline": None,
                                               "target_date": "2026-10-05", "learning_goal": "flexible"})
        back = self.update(material, goal_type="deadline", deadline="2026-10-15")
        self.assertEqual((back["goal_type"], back["deadline"]), ("deadline", "2026-10-15"))
        # Removing just the deadline of a configured goal makes it flexible.
        cleared = self.update(material, deadline=None)
        self.assertEqual((cleared["goal_type"], cleared["deadline"]), ("flexible", None))
        # Setting a deadline on a flexible goal makes it a deadline goal.
        dated = self.update(material, deadline="2026-10-12")
        self.assertEqual((dated["goal_type"], dated["deadline"]), ("deadline", "2026-10-12"))
        removed = self.update(material, goal_type=None, deadline=None, target_date=None)
        self.assertEqual(self.goal(removed), {"goal_type": None, "deadline": None, "target_date": None, "learning_goal": None})

    def test_edit_target_date(self):
        material = self.add(goal_type="deadline", deadline="2026-10-10")
        self.assertEqual(self.update(material, target_date="2026-10-08")["target_date"], "2026-10-08")
        self.assertEqual(self.update(material, target_date="2026-10-01")["target_date"], "2026-10-01")
        self.assertIsNone(self.update(material, target_date=None)["target_date"])

    def test_updates_are_validated_against_the_saved_goal(self):
        material = self.add(goal_type="deadline", deadline="2026-10-10", target_date="2026-10-08")
        with self.assertRaisesRegex(ValueError, "cannot be later"):
            self.update(material, deadline="2026-10-05")        # would put the target after the deadline
        with self.assertRaisesRegex(ValueError, "cannot be later"):
            self.update(material, target_date="2026-10-11")
        with self.assertRaisesRegex(ValueError, "needs a deadline"):
            self.update(material, goal_type="deadline", deadline=None)
        with self.assertRaisesRegex(ValueError, "Flexible learning has no deadline"):
            self.update(material, goal_type="flexible", deadline="2026-10-10")
        saved = study_planner_store.get_material(self.alice, material["material_id"])
        self.assertEqual((saved["goal_type"], saved["deadline"], saved["target_date"]), ("deadline", "2026-10-10", "2026-10-08"))

    def test_unconfigured_materials_stay_valid_and_unchanged(self):
        undated = self.add("mkt")
        dated = self.add("stats", deadline="2026-10-10")
        self.assertEqual(self.goal(undated), {"goal_type": None, "deadline": None, "target_date": None, "learning_goal": None})
        # Reads as a deadline goal for display, but nothing is stored for it.
        self.assertEqual(self.goal(dated), {"goal_type": None, "deadline": "2026-10-10", "target_date": None,
                                            "learning_goal": "deadline"})
        # Deadline-only edits behave exactly as before goals existed: goal_type stays unset.
        self.assertEqual(self.goal(self.update(dated, deadline=None))["goal_type"], None)
        self.assertEqual(self.goal(self.update(undated, deadline="2026-10-20"))["goal_type"], None)
        self.assertEqual(self.update(undated, familiarity="reviewing")["deadline"], "2026-10-20")

    def test_materials_saved_before_goals_existed_are_migrated_additively(self):
        dated = self.add("mkt", deadline="2026-10-10")
        undated = self.add("stats")
        # Rebuild the pre-goals table shape: the goal columns did not exist yet.
        raw = sqlite3.connect(study_planner_store.DATABASE_PATH)
        try:
            raw.execute("ALTER TABLE study_plan_materials DROP COLUMN goal_type")
            raw.execute("ALTER TABLE study_plan_materials DROP COLUMN target_date")
            raw.commit()
            columns = {row[1] for row in raw.execute("PRAGMA table_info(study_plan_materials)")}
        finally:
            raw.close()
        self.assertNotIn("goal_type", columns)
        materials = {m["document_id"]: m for m in study_planner_store.list_materials(self.alice, self.plan["plan_id"])}
        self.assertEqual(self.goal(materials["mkt"]), {"goal_type": None, "deadline": "2026-10-10", "target_date": None,
                                                       "learning_goal": "deadline"})
        self.assertEqual(self.goal(materials["stats"]), {"goal_type": None, "deadline": None, "target_date": None,
                                                         "learning_goal": None})
        self.assertEqual((materials["mkt"]["material_id"], materials["stats"]["material_id"]),
                         (dated["material_id"], undated["material_id"]))


class StudyGoalApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=UTC_NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        self.base = f"/api/planner/plans/{self.plan['plan_id']}"

    def add(self, document_id, **fields):
        return self.client.post(f"{self.base}/materials", json={"document_id": document_id, **fields})

    def patch(self, material, **body):
        return self.client.patch(f"{self.base}/materials/{material['material_id']}", json=body)

    def listed(self):
        """A fresh read, as after a page reload."""
        return {m["document_id"]: m for m in self.client.get(f"{self.base}/materials").json()}

    def test_create_deadline_goal_and_flexible_goal(self):
        deadline = self.add("mkt", goal_type="deadline", deadline="2026-10-10", target_date="2026-10-06")
        self.assertEqual(deadline.status_code, 201, deadline.text)
        flexible = self.add("stats", goal_type="flexible")
        self.assertEqual(flexible.status_code, 201, flexible.text)
        materials = self.listed()
        self.assertEqual({k: materials["mkt"][k] for k in ("goal_type", "deadline", "target_date", "learning_goal", "document_title")},
                         {"goal_type": "deadline", "deadline": "2026-10-10", "target_date": "2026-10-06",
                          "learning_goal": "deadline", "document_title": "Marketing"})
        self.assertEqual({k: materials["stats"][k] for k in ("goal_type", "deadline", "target_date", "learning_goal")},
                         {"goal_type": "flexible", "deadline": None, "target_date": None, "learning_goal": "flexible"})
        detail = self.client.get(self.base).json()
        self.assertEqual({m["document_id"]: m["goal_type"] for m in detail["materials"]}, {"mkt": "deadline", "stats": "flexible"})

    def test_invalid_goals_are_rejected_with_400(self):
        for body in ({"goal_type": "deadline"}, {"goal_type": "flexible", "deadline": "2026-10-10"},
                     {"goal_type": "deadline", "deadline": "2026-10-10", "target_date": "2026-10-12"},
                     {"goal_type": "later"}, {"goal_type": "flexible", "target_date": "13/10/2026"}):
            with self.subTest(body=body):
                self.assertEqual(self.add("mkt", **body).status_code, 400)
        self.assertEqual(self.listed(), {})
        self.assertEqual(self.add("mkt", goal="deadline").status_code, 422)   # unknown fields stay forbidden

    def test_change_remove_and_edit_goal_then_reload(self):
        material = self.add("mkt", goal_type="deadline", deadline="2026-10-10").json()
        changed = self.patch(material, goal_type="deadline", deadline="2026-10-14", target_date="2026-10-12")
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((changed.json()["deadline"], changed.json()["target_date"]), ("2026-10-14", "2026-10-12"))
        self.assertEqual(self.patch(material, target_date="2026-10-15").status_code, 400)
        self.assertEqual(self.patch(material, target_date="2026-10-09").json()["target_date"], "2026-10-09")
        flexible = self.patch(material, goal_type="flexible", deadline=None, target_date="2026-10-09").json()
        self.assertEqual((flexible["goal_type"], flexible["deadline"]), ("flexible", None))
        reloaded = self.listed()["mkt"]
        self.assertEqual((reloaded["goal_type"], reloaded["deadline"], reloaded["target_date"]), ("flexible", None, "2026-10-09"))
        removed = self.patch(material, goal_type=None, deadline=None, target_date=None).json()
        self.assertEqual((removed["goal_type"], removed["deadline"], removed["target_date"], removed["learning_goal"]),
                         (None, None, None, None))
        self.assertIsNone(self.listed()["mkt"]["goal_type"])

    def test_existing_clients_without_goals_keep_working(self):
        legacy = self.add("mkt", deadline="2026-10-10").json()
        self.assertEqual((legacy["goal_type"], legacy["learning_goal"]), (None, "deadline"))
        cleared = self.patch(legacy, deadline=None).json()
        self.assertEqual((cleared["goal_type"], cleared["deadline"], cleared["learning_goal"]), (None, None, None))
        undated = self.add("stats").json()
        self.assertEqual((undated["goal_type"], undated["deadline"], undated["target_date"]), (None, None, None))

    def test_target_date_is_a_preference_not_a_preview_constraint(self):
        self.add("mkt", goal_type="flexible", target_date="2026-09-01")   # already past: still fine
        self.client.post("/api/planner/availability", json={"start_at": "18:00", "end_at": "21:00", "is_recurring": True, "day_of_week": 0})
        preview = self.client.post(f"{self.base}/preview", json={"utc_offset_minutes": 0, "local_now": MONDAY_LOCAL})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["sessions"])

    def test_goal_changes_never_touch_accepted_sessions(self):
        mkt = self.add("mkt", goal_type="deadline", deadline="2026-10-05").json()
        stats = self.add("stats", goal_type="flexible").json()
        for day in (0, 1, 2, 3):
            self.client.post("/api/planner/availability", json={"start_at": "18:00", "end_at": "21:00", "is_recurring": True, "day_of_week": day})
        confirmed = self.client.post(f"{self.base}/confirm", json={"utc_offset_minutes": 0, "local_now": MONDAY_LOCAL})
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        before = study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"])
        self.assertTrue(before)
        runs_before = study_planner_store.list_plan_schedule_runs(self.alice, self.plan["plan_id"])
        with patch("backend.google_calendar_sync.after_planner_change") as calendar_sync:
            self.assertEqual(self.patch(mkt, goal_type="deadline", deadline="2026-09-30", target_date="2026-09-29").status_code, 200)
            self.assertEqual(self.patch(mkt, goal_type="flexible", deadline=None).status_code, 200)
            self.assertEqual(self.patch(stats, goal_type="deadline", deadline="2026-10-01").status_code, 200)
            self.assertEqual(self.patch(stats, goal_type=None, deadline=None, target_date=None).status_code, 200)
        calendar_sync.assert_not_called()   # nothing changed on the calendar either
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"]), before)
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, self.plan["plan_id"]), runs_before)


if __name__ == "__main__":
    unittest.main()
