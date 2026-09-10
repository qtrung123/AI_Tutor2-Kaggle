import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, study_planner_service, study_planner_store

# Well before any availability window used in these fixtures (earliest is 18:00), so it never
# truncates a test's intended availability via the past-time guard unless the test says so.
EARLY_MORNING = datetime(2026, 9, 14, 6, 0)


class StudyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-planner@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-planner@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_task_crud_is_owner_scoped(self):
        task = study_planner_service.create_task(self.alice, "Exam prep", "2026-09-20", 120)
        self.assertEqual(task["remaining_minutes"], 120)
        self.assertEqual(task["estimated_minutes"], 120)
        self.assertEqual(task["status"], "active")

        self.assertIsNone(study_planner_store.get_task(self.bob, task["task_id"]))
        self.assertEqual([item["task_id"] for item in study_planner_store.list_tasks(self.alice)], [task["task_id"]])
        self.assertEqual(study_planner_store.list_tasks(self.bob), [])

        with self.assertRaises(ValueError):
            study_planner_store.update_task(self.bob, task["task_id"], {"title": "Stolen"})
        updated = study_planner_store.update_task(self.alice, task["task_id"], {"title": "Renamed"})
        self.assertEqual(updated["title"], "Renamed")

        with self.assertRaises(ValueError):
            study_planner_store.delete_task(self.bob, task["task_id"])
        study_planner_store.delete_task(self.alice, task["task_id"])
        self.assertIsNone(study_planner_store.get_task(self.alice, task["task_id"]))

    def test_availability_persists_merges_and_erases_correctly(self):
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        study_planner_store.add_availability(self.alice, "19:00", "20:00", date="2026-09-14")  # adjacent -> merges
        slots = study_planner_store.list_availability(self.alice)
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0]["start_at"], slots[0]["end_at"]), ("18:00", "20:00"))
        self.assertFalse(slots[0]["is_recurring"])

        study_planner_store.remove_availability(self.alice, "18:30", "19:00", date="2026-09-14")
        remaining = sorted((s["start_at"], s["end_at"]) for s in study_planner_store.list_availability(self.alice))
        self.assertEqual(remaining, [("18:00", "18:30"), ("19:00", "20:00")])

        # Owner isolation: Bob's calendar is untouched.
        self.assertEqual(study_planner_store.list_availability(self.bob), [])

        with self.assertRaises(ValueError):
            study_planner_store.add_availability(self.alice, "19:00", "18:00", date="2026-09-14")

    def test_recurring_weekly_availability_is_kept_separate_from_dated(self):
        study_planner_store.add_availability(self.alice, "18:00", "19:00", is_recurring=True, day_of_week=0)
        study_planner_store.add_availability(self.alice, "20:00", "21:00", date="2026-09-14")
        slots = study_planner_store.list_availability(self.alice)
        recurring = [slot for slot in slots if slot["is_recurring"]]
        dated = [slot for slot in slots if not slot["is_recurring"]]
        self.assertEqual(len(recurring), 1)
        self.assertEqual(recurring[0]["day_of_week"], 0)
        self.assertEqual(len(dated), 1)
        self.assertEqual(dated[0]["date"], "2026-09-14")

    def test_scheduler_never_uses_time_outside_availability(self):
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-18", "remaining_minutes": 180}
        availability = [{"is_recurring": False, "date": "2026-09-15", "start_at": "18:00", "end_at": "19:00"}]
        result = study_planner_service.compute_schedule(task, availability, [], now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        for block in result["blocks"]:
            self.assertEqual(block["start_at"][:10], "2026-09-15")
            self.assertGreaterEqual(block["start_at"][11:16], "18:00")
            self.assertLessEqual(block["end_at"][11:16], "19:00")

    def test_scheduler_never_schedules_after_deadline(self):
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-15", "remaining_minutes": 999999}
        availability = [
            {"is_recurring": False, "date": "2026-09-15", "start_at": "18:00", "end_at": "19:00"},
            {"is_recurring": False, "date": "2026-09-20", "start_at": "09:00", "end_at": "12:00"},
        ]
        result = study_planner_service.compute_schedule(task, availability, [], now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        for block in result["blocks"]:
            self.assertLessEqual(block["start_at"][:10], "2026-09-15")

    def test_scheduler_avoids_confirmed_blocks(self):
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-14", "remaining_minutes": 60}
        availability = [{"is_recurring": False, "date": "2026-09-14", "start_at": "18:00", "end_at": "20:00"}]
        existing = [{
            "start_at": "2026-09-14T18:00:00", "end_at": "2026-09-14T19:00:00",
            "status": "confirmed", "locked": False,
        }]
        result = study_planner_service.compute_schedule(task, availability, existing, now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        for block in result["blocks"]:
            self.assertGreaterEqual(block["start_at"][11:16], "19:00")

    def test_240_required_300_available_produces_a_complete_plan(self):
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-18", "remaining_minutes": 240}
        availability = [
            {"is_recurring": False, "date": "2026-09-14", "start_at": "18:00", "end_at": "20:30"},
            {"is_recurring": False, "date": "2026-09-15", "start_at": "18:00", "end_at": "20:30"},
        ]
        result = study_planner_service.compute_schedule(task, availability, [], now=EARLY_MORNING)
        self.assertEqual(result["status"], "on_track")
        self.assertEqual(result["available_minutes"], 300)
        self.assertEqual(result["shortage_minutes"], 0)
        self.assertEqual(result["study_buffer"], 60)
        self.assertEqual(sum(block["planned_minutes"] for block in result["blocks"]), 240)

    def test_insufficient_time_returns_schedule_risk_with_shortage(self):
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-14", "remaining_minutes": 240}
        availability = [{"is_recurring": False, "date": "2026-09-14", "start_at": "18:00", "end_at": "19:00"}]
        result = study_planner_service.compute_schedule(task, availability, [], now=EARLY_MORNING)
        self.assertEqual(result["status"], "schedule_risk")
        self.assertEqual(result["available_minutes"], 60)
        self.assertEqual(result["shortage_minutes"], 180)
        self.assertEqual(result["study_buffer"], -180)
        # Still schedules whatever fits inside real availability -- never fabricates time.
        self.assertEqual(sum(block["planned_minutes"] for block in result["blocks"]), 60)

    def test_suggested_blocks_can_be_accepted(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        self.assertTrue(all(block["status"] == "suggested" for block in result["blocks"]))
        accepted = study_planner_service.accept_plan(self.alice, task["task_id"])
        self.assertTrue(accepted)
        self.assertTrue(all(block["status"] == "confirmed" for block in accepted))

    def test_manual_edit_marks_block_locked(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        block = result["blocks"][0]
        self.assertFalse(block["locked"])
        edited = study_planner_service.edit_block(
            self.alice, block["block_id"], start_at="2026-09-14T18:15:00", end_at="2026-09-14T19:00:00",
        )
        self.assertTrue(edited["locked"])
        self.assertEqual(edited["start_at"][11:16], "18:15")
        self.assertEqual(edited["planned_minutes"], 45)

    def test_locked_and_confirmed_blocks_remain_protected_from_regeneration(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        first = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertEqual(len(first["blocks"]), 2)

        locked_block = study_planner_service.edit_block(
            self.alice, first["blocks"][0]["block_id"],
            start_at=first["blocks"][0]["start_at"], end_at=first["blocks"][0]["end_at"],
        )
        self.assertTrue(locked_block["locked"])
        study_planner_store.update_block(self.alice, first["blocks"][1]["block_id"], {"status": "confirmed"})

        study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        remaining_ids = {block["block_id"] for block in study_planner_store.list_blocks(self.alice, task["task_id"])}
        self.assertIn(locked_block["block_id"], remaining_ids)
        self.assertIn(first["blocks"][1]["block_id"], remaining_ids)

        still_locked = study_planner_store.get_block(self.alice, locked_block["block_id"])
        self.assertEqual(still_locked["start_at"], locked_block["start_at"])
        self.assertEqual(still_locked["end_at"], locked_block["end_at"])
        self.assertTrue(still_locked["locked"])
        confirmed = study_planner_store.get_block(self.alice, first["blocks"][1]["block_id"])
        self.assertEqual(confirmed["status"], "confirmed")

    def test_edit_block_is_owner_scoped(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        block = result["blocks"][0]
        with self.assertRaises(ValueError):
            study_planner_service.edit_block(self.bob, block["block_id"], start_at="2026-09-14T18:00:00")

    def test_edit_block_rejects_a_range_outside_availability(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        block = result["blocks"][0]
        with self.assertRaisesRegex(ValueError, "outside your selected availability"):
            study_planner_service.edit_block(
                self.alice, block["block_id"], start_at="2026-09-14T20:00:00", end_at="2026-09-14T20:30:00",
            )

    def test_edit_block_rejects_an_overlap_with_a_confirmed_block(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        first_block, second_block = result["blocks"]
        study_planner_store.update_block(self.alice, first_block["block_id"], {"status": "confirmed"})
        with self.assertRaisesRegex(ValueError, "overlaps another study block"):
            study_planner_service.edit_block(
                self.alice, second_block["block_id"],
                start_at=first_block["start_at"], end_at=first_block["end_at"],
            )

    def test_fully_past_availability_today_is_never_scheduled(self):
        # current local time = 17:30, availability today = 08:00-10:00: entirely elapsed.
        now = datetime(2026, 9, 14, 17, 30)
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-14", "remaining_minutes": 60}
        availability = [{"is_recurring": False, "date": "2026-09-14", "start_at": "08:00", "end_at": "10:00"}]
        result = study_planner_service.compute_schedule(task, availability, [], now=now)
        self.assertEqual(result["blocks"], [])
        self.assertEqual(result["available_minutes"], 0)
        self.assertEqual(result["status"], "schedule_risk")

    def test_partially_past_availability_today_only_uses_the_future_grid_aligned_portion(self):
        # current local time = 18:20, availability = 18:00-20:00: only 18:30-20:00 is usable,
        # aligned up to the next 30-minute planner grid boundary (never down into the past).
        now = datetime(2026, 9, 14, 18, 20)
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-14", "remaining_minutes": 60}
        availability = [{"is_recurring": False, "date": "2026-09-14", "start_at": "18:00", "end_at": "20:00"}]
        result = study_planner_service.compute_schedule(task, availability, [], now=now)
        self.assertEqual(result["available_minutes"], 90)  # 18:30-20:00
        self.assertTrue(result["blocks"])
        for block in result["blocks"]:
            self.assertGreaterEqual(block["start_at"][11:16], "18:30")

    def test_past_availability_on_a_later_deadline_day_is_unaffected(self):
        # The past-time guard only applies to "today" -- future days keep their full availability
        # even when generation happens later in the current day.
        now = datetime(2026, 9, 14, 23, 0)
        task = {"title": "Prep", "document_id": None, "deadline": "2026-09-15", "remaining_minutes": 60}
        availability = [{"is_recurring": False, "date": "2026-09-15", "start_at": "08:00", "end_at": "09:00"}]
        result = study_planner_service.compute_schedule(task, availability, [], now=now)
        self.assertEqual(result["available_minutes"], 60)
        self.assertEqual(result["status"], "on_track")


class StudyPlannerFrontendTests(unittest.TestCase):
    def test_study_planner_ui_exists(self):
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        self.assertIn('data-page="planner"', html)
        self.assertIn('id="planner-view"', html)
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="planner-calendar"', script)
        self.assertIn("Generate Study Plan", script)
        self.assertIn("Accept Plan", script)

    def test_user_can_select_and_remove_availability_cells(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("function plannerStartDrag", script)
        self.assertIn("function plannerExtendDrag", script)
        self.assertIn("function plannerFinishDrag", script)
        self.assertIn('id="planner-mode-erase"', script)
        self.assertIn("`${PLANNER_AVAILABILITY_API_URL}/remove`", script)

    def test_block_editor_has_editable_start_and_end_with_save_delete_cancel(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="planner-block-editor-start" type="time"', script)
        self.assertIn('id="planner-block-editor-end" type="time"', script)
        self.assertIn('id="planner-block-editor-save"', script)
        self.assertIn('id="planner-block-editor-delete"', script)
        self.assertIn('id="planner-block-editor-cancel"', script)
        # A real editor, not the old window.prompt shortcut.
        self.assertNotIn("window.prompt(", script)
        self.assertIn("start_at: `${dateKey}T${startTime}:00`, end_at: `${dateKey}T${endTime}:00`", script)


if __name__ == "__main__":
    unittest.main()
