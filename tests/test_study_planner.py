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


class StudyPlannerDocumentAwareTests(unittest.TestCase):
    """Phase 2: document-aware study plan items, deterministic workload allocation, and
    topic_id-tagged study blocks. Reuses compute_schedule (Phase 1) completely unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-doc-planner@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-doc-planner@example.com", "long-password-b")["id"]
        self.document = {
            "id": "notes.pdf",
            "topics": [
                {"topic_id": "topic-a", "name": "Topic A", "subtopics": [{"subtopic_id": "s1"}, {"subtopic_id": "s2"}]},
                {"topic_id": "topic-b", "name": "Topic B", "subtopics": [{"subtopic_id": "s3"}]},
            ],
        }

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def _chunks_for(_document_id, topic_id, _owner_id):
        # Content kept tiny so its (optional) size contribution stays negligible -- topic-a's
        # workload score (2 subtopics + 2 chunks = 4) is a clean 2x topic-b's (1 + 1 = 2), so a
        # 120-minute allocation lands on exactly 80/40 -- both comfortably above the scheduler's
        # 30-minute minimum block, keeping this fixture about workload allocation, not about the
        # (unchanged, Phase 1) minimum-block edge case.
        if topic_id == "topic-a":
            return [{"content": "x" * 10} for _ in range(2)]
        return [{"content": "y" * 10}]

    def _document_patches(self):
        return (
            patch.object(study_planner_service, "get_indexed_document", return_value=self.document),
            patch.object(study_planner_service, "get_topic_chunks", side_effect=self._chunks_for),
        )

    def test_document_task_creates_study_plan_items(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120, document_id="notes.pdf")
        first, second = self._document_patches()
        with first, second:
            items = study_planner_service.ensure_study_plan_items(self.alice, task)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["topic_id"] for item in items}, {"topic-a", "topic-b"})
        self.assertTrue(all(item["document_id"] == "notes.pdf" for item in items))
        self.assertTrue(all(item["task_id"] == task["task_id"] for item in items))
        stored = study_planner_store.list_plan_items(self.alice, task["task_id"])
        self.assertEqual(len(stored), 2)

    def test_workload_allocation_sums_exactly_to_task_estimated_minutes(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 137, document_id="notes.pdf")
        first, second = self._document_patches()
        with first, second:
            items = study_planner_service.ensure_study_plan_items(self.alice, task)
        self.assertEqual(sum(item["estimated_minutes"] for item in items), 137)
        self.assertEqual(sum(item["remaining_minutes"] for item in items), 137)

    def test_topic_proportions_are_deterministic(self):
        task_one = study_planner_service.create_task(self.alice, "Prep 1", "2026-09-20", 240, document_id="notes.pdf")
        task_two = study_planner_service.create_task(self.alice, "Prep 2", "2026-09-21", 240, document_id="notes.pdf")
        first, second = self._document_patches()
        with first, second:
            items_one = study_planner_service.ensure_study_plan_items(self.alice, task_one)
        third, fourth = self._document_patches()
        with third, fourth:
            items_two = study_planner_service.ensure_study_plan_items(self.alice, task_two)
        minutes_one = {item["topic_id"]: item["estimated_minutes"] for item in items_one}
        minutes_two = {item["topic_id"]: item["estimated_minutes"] for item in items_two}
        self.assertEqual(minutes_one, minutes_two)
        self.assertGreater(minutes_one["topic-a"], minutes_one["topic-b"])

    def test_generated_study_blocks_contain_topic_id(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-14", 120, document_id="notes.pdf")
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        first, second = self._document_patches()
        with first, second:
            result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        self.assertTrue(all(block["topic_id"] for block in result["blocks"]))
        self.assertTrue(all(block["document_id"] == "notes.pdf" for block in result["blocks"]))
        self.assertEqual({block["topic_id"] for block in result["blocks"]}, {"topic-a", "topic-b"})

    def test_non_document_tasks_still_work_unchanged(self):
        task = study_planner_service.create_task(self.alice, "Plain task", "2026-09-14", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertTrue(result["blocks"])
        self.assertIsNone(result["blocks"][0]["topic_id"])
        self.assertEqual(study_planner_store.list_plan_items(self.alice, task["task_id"]), [])

    def test_study_plan_items_are_owner_isolated(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120, document_id="notes.pdf")
        first, second = self._document_patches()
        with first, second:
            study_planner_service.ensure_study_plan_items(self.alice, task)
        self.assertEqual(study_planner_store.list_plan_items(self.bob, task["task_id"]), [])


class StudyPlannerCompletionTests(unittest.TestCase):
    """Phase 3: study block completion tracking, topic_progress accumulation, and the read-only
    quiz-mastery extension point. Reuses the Phase 1/2 store/service primitives directly rather
    than the scheduler, so these tests stay independent of allocate_blocks' block-splitting
    behavior."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-completion@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-completion@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _block(self, owner_id, task_id, start_at="2026-09-14T18:00:00", end_at="2026-09-14T18:30:00",
               planned_minutes=30, document_id="notes.pdf", topic_id="topic-a", title="Session"):
        block, = study_planner_store.create_blocks(owner_id, task_id, [{
            "document_id": document_id, "topic_id": topic_id, "title": title,
            "start_at": start_at, "end_at": end_at, "planned_minutes": planned_minutes,
        }])
        return block

    def test_complete_block_updates_topic_progress(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 50, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 50, "remaining_minutes": 50, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"])
        self.assertIsNone(study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a"))

        updated = study_planner_service.complete_block(self.alice, block["block_id"])
        self.assertEqual(updated["completion_status"], "completed")
        self.assertIsNotNone(updated["completed_at"])
        self.assertEqual(updated["actual_minutes"], 30)

        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["planned_minutes"], 50)
        self.assertEqual(progress["completed_minutes"], 30)
        self.assertEqual(progress["progress_percent"], 60.0)
        self.assertIsNotNone(progress["last_studied_at"])

    def test_complete_block_accepts_an_explicit_actual_minutes_override(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 50, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 50, "remaining_minutes": 50, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"], planned_minutes=30)
        updated = study_planner_service.complete_block(self.alice, block["block_id"], actual_minutes=45)
        self.assertEqual(updated["actual_minutes"], 45)
        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["completed_minutes"], 45)

    def test_complete_block_is_owner_scoped(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        block = self._block(self.alice, task["task_id"])
        with self.assertRaises(ValueError):
            study_planner_service.complete_block(self.bob, block["block_id"])
        self.assertEqual(
            study_planner_store.get_block(self.alice, block["block_id"])["completion_status"], "scheduled",
        )
        self.assertIsNone(study_planner_store.get_topic_progress(self.bob, "notes.pdf", "topic-a"))
        self.assertIsNone(study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a"))

    def test_multiple_completed_blocks_accumulate_progress(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 80, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 80, "remaining_minutes": 80, "priority": 0,
        }])
        first = self._block(self.alice, task["task_id"], start_at="2026-09-14T18:00:00", end_at="2026-09-14T18:30:00")
        second = self._block(self.alice, task["task_id"], start_at="2026-09-15T18:00:00", end_at="2026-09-15T18:30:00")

        study_planner_service.complete_block(self.alice, first["block_id"])
        study_planner_service.complete_block(self.alice, second["block_id"])

        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["completed_minutes"], 60)
        self.assertEqual(progress["planned_minutes"], 80)
        self.assertEqual(progress["progress_percent"], 75.0)

    def test_completing_an_already_completed_block_is_rejected_and_never_double_counted(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 30, "remaining_minutes": 30, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"])
        study_planner_service.complete_block(self.alice, block["block_id"])
        with self.assertRaises(ValueError):
            study_planner_service.complete_block(self.alice, block["block_id"])
        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["completed_minutes"], 30)

    def test_completed_minutes_and_progress_percent_are_capped_at_planned_minutes(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 30, "remaining_minutes": 30, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"], planned_minutes=30)
        study_planner_service.complete_block(self.alice, block["block_id"], actual_minutes=90)

        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["progress_percent"], 100.0)
        self.assertLessEqual(progress["completed_minutes"], progress["planned_minutes"])
        self.assertEqual(progress["completed_minutes"], 30)

    def test_completed_block_actual_minutes_cannot_be_modified(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 30, "remaining_minutes": 30, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"], planned_minutes=30)
        study_planner_service.complete_block(self.alice, block["block_id"], actual_minutes=30)

        with self.assertRaisesRegex(ValueError, "completed study block"):
            study_planner_service.update_block_actual_minutes(self.alice, block["block_id"], 45)

        unchanged = study_planner_store.get_block(self.alice, block["block_id"])
        self.assertEqual(unchanged["actual_minutes"], 30)
        progress = study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(progress["completed_minutes"], 30)

    def test_actual_minutes_can_still_be_edited_before_completion(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        block = self._block(self.alice, task["task_id"], planned_minutes=30)
        updated = study_planner_service.update_block_actual_minutes(self.alice, block["block_id"], 20)
        self.assertEqual(updated["actual_minutes"], 20)
        self.assertEqual(updated["completion_status"], "scheduled")

    def test_skipped_block_does_not_increase_progress(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 30, "remaining_minutes": 30, "priority": 0,
        }])
        block = self._block(self.alice, task["task_id"])
        updated = study_planner_service.skip_block(self.alice, block["block_id"])
        self.assertEqual(updated["completion_status"], "skipped")
        self.assertIsNone(study_planner_store.get_topic_progress(self.alice, "notes.pdf", "topic-a"))

    def test_non_document_task_blocks_still_complete_without_topic_progress(self):
        task = study_planner_service.create_task(self.alice, "Plain task", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        block = result["blocks"][0]
        self.assertIsNone(block["document_id"])
        self.assertIsNone(block["topic_id"])

        updated = study_planner_service.complete_block(self.alice, block["block_id"])
        self.assertEqual(updated["completion_status"], "completed")
        self.assertEqual(study_planner_store.list_topic_progress(self.alice), [])

    def test_topic_mastery_extension_point_reads_from_existing_quiz_store(self):
        from backend import quiz_store

        with patch.object(
            quiz_store, "get_topic_mastery",
            return_value={"mastery_score": 0.75, "student_id": self.alice, "document_id": "notes.pdf",
                          "topic_id": "topic-a"},
        ) as mocked:
            mastery = study_planner_service.get_topic_mastery_for_planning(self.alice, "notes.pdf", "topic-a")
        mocked.assert_called_once_with(self.alice, "notes.pdf", "topic-a")
        self.assertEqual(mastery, {
            "owner_id": self.alice, "document_id": "notes.pdf", "topic_id": "topic-a", "mastery_score": 0.75,
        })

    def test_topic_mastery_extension_point_returns_none_when_no_mastery_recorded(self):
        from backend import quiz_store

        with patch.object(quiz_store, "get_topic_mastery", return_value=None):
            mastery = study_planner_service.get_topic_mastery_for_planning(self.alice, "notes.pdf", "topic-a")
        self.assertIsNone(mastery)


class StudyPlannerTaskLifecycleTests(unittest.TestCase):
    """A task's status automatically flips active -> completed once every one of its blocks has
    completion_status == 'completed'. Purely a status flip layered on the existing planner flow;
    never touches the scheduler."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-lifecycle@example.com", "long-password-a")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_task_becomes_completed_after_all_blocks_finish(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertEqual(len(result["blocks"]), 1)
        study_planner_service.complete_block(self.alice, result["blocks"][0]["block_id"])
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["status"], "completed")

    def test_incomplete_blocks_keep_task_active(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        result = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertEqual(len(result["blocks"]), 2)
        study_planner_service.complete_block(self.alice, result["blocks"][0]["block_id"])
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["status"], "active")

    def test_task_with_no_blocks_is_never_auto_completed(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_service._maybe_complete_task(self.alice, task["task_id"])
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["status"], "active")


class StudyPlannerTaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-progress@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-progress@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_task_progress_reflects_completed_and_planned_minutes(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120, document_id="notes.pdf")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [
            {"document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
             "estimated_minutes": 60, "remaining_minutes": 60, "priority": 0},
            {"document_id": "notes.pdf", "topic_id": "topic-b", "title": "Study Topic B",
             "estimated_minutes": 60, "remaining_minutes": 60, "priority": 1},
        ])
        blocks = study_planner_store.create_blocks(self.alice, task["task_id"], [
            {"document_id": "notes.pdf", "topic_id": "topic-a", "title": "Session A",
             "start_at": "2026-09-14T18:00:00", "end_at": "2026-09-14T19:00:00", "planned_minutes": 60},
            {"document_id": "notes.pdf", "topic_id": "topic-b", "title": "Session B",
             "start_at": "2026-09-15T18:00:00", "end_at": "2026-09-15T19:00:00", "planned_minutes": 60},
        ])
        study_planner_service.complete_block(self.alice, blocks[0]["block_id"])

        progress = study_planner_service.get_task_progress(self.alice, task["task_id"])
        self.assertEqual(progress["planned_minutes"], 120)
        self.assertEqual(progress["completed_minutes"], 60)
        self.assertEqual(progress["progress_percent"], 50.0)
        self.assertEqual(progress["topic_count"], 2)
        self.assertEqual(progress["completed_topic_count"], 1)

    def test_task_progress_for_non_document_task_has_no_topic_counts(self):
        task = study_planner_service.create_task(self.alice, "Plain", "2026-09-20", 30)
        progress = study_planner_service.get_task_progress(self.alice, task["task_id"])
        self.assertIsNone(progress["topic_count"])
        self.assertIsNone(progress["completed_topic_count"])
        self.assertEqual(progress["planned_minutes"], 0)
        self.assertEqual(progress["progress_percent"], 0.0)

    def test_task_progress_is_owner_scoped(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30)
        with self.assertRaises(ValueError):
            study_planner_service.get_task_progress(self.bob, task["task_id"])


class StudyPlannerRegenerateTests(unittest.TestCase):
    """Regenerate is orchestration-only: it reuses the unchanged scheduler/allocation/
    availability logic (via generate_schedule), and only decides which already-persisted blocks
    get cleared first."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-regen@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-regen@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_regenerate_removes_stale_unlocked_suggested_blocks(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        first = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        old_block_id = first["blocks"][0]["block_id"]

        second = study_planner_service.regenerate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        remaining_ids = {b["block_id"] for b in study_planner_store.list_blocks(self.alice, task["task_id"])}
        self.assertNotIn(old_block_id, remaining_ids)
        self.assertTrue(second["blocks"])

    def test_regenerate_preserves_completed_blocks(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        accepted = study_planner_service.accept_plan(self.alice, task["task_id"])
        completed_block_id = accepted[0]["block_id"]
        study_planner_service.complete_block(self.alice, completed_block_id)

        study_planner_service.regenerate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        remaining_ids = {b["block_id"] for b in study_planner_store.list_blocks(self.alice, task["task_id"])}
        self.assertIn(completed_block_id, remaining_ids)
        still = study_planner_store.get_block(self.alice, completed_block_id)
        self.assertEqual(still["completion_status"], "completed")

    def test_regenerate_preserves_locked_blocks(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        first = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        block = first["blocks"][0]
        locked = study_planner_service.edit_block(
            self.alice, block["block_id"], start_at=block["start_at"], end_at=block["end_at"],
        )
        self.assertTrue(locked["locked"])

        study_planner_service.regenerate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        remaining_ids = {b["block_id"] for b in study_planner_store.list_blocks(self.alice, task["task_id"])}
        self.assertIn(locked["block_id"], remaining_ids)
        self.assertTrue(study_planner_store.get_block(self.alice, locked["block_id"])["locked"])

    def test_regenerate_is_owner_scoped(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60)
        with self.assertRaises(ValueError):
            study_planner_service.regenerate_schedule(self.bob, task["task_id"])

    def test_regenerate_does_not_overlap_a_completed_suggested_block(self):
        # Regression test: a block completed while still status='suggested' (never accepted)
        # must both survive regeneration AND keep its time slot free of any new block --
        # free_minutes_by_date must treat completion_status='completed' as busy on its own,
        # independent of the (unchanged) status/locked fields.
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-14")
        first = study_planner_service.generate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        self.assertEqual(len(first["blocks"]), 2)
        completed_block = first["blocks"][0]
        study_planner_service.complete_block(self.alice, completed_block["block_id"])
        still = study_planner_store.get_block(self.alice, completed_block["block_id"])
        self.assertEqual(still["status"], "suggested")
        self.assertEqual(still["completion_status"], "completed")

        study_planner_service.regenerate_schedule(self.alice, task["task_id"], now=EARLY_MORNING)
        remaining = study_planner_store.list_blocks(self.alice, task["task_id"])
        ids = {block["block_id"] for block in remaining}
        self.assertIn(completed_block["block_id"], ids)

        completed_start = datetime.fromisoformat(still["start_at"])
        completed_end = datetime.fromisoformat(still["end_at"])
        for block in remaining:
            if block["block_id"] == completed_block["block_id"]:
                continue
            block_start = datetime.fromisoformat(block["start_at"])
            block_end = datetime.fromisoformat(block["end_at"])
            overlaps = block_start < completed_end and completed_start < block_end
            self.assertFalse(overlaps, f"Regenerated block {block} overlaps completed block {still}")


class StudyPlannerRemainingMinutesTests(unittest.TestCase):
    """Completing a block rolls its actual_minutes off the parent task's remaining_minutes,
    floored at 0. topic_progress accumulation (covered by StudyPlannerCompletionTests) is
    untouched by this."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-remaining@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-remaining@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def _block(owner_id, task_id, start_at, end_at, planned_minutes):
        block, = study_planner_store.create_blocks(owner_id, task_id, [{
            "document_id": None, "topic_id": None, "title": "Session",
            "start_at": start_at, "end_at": end_at, "planned_minutes": planned_minutes,
        }])
        return block

    def test_completing_a_block_decrements_remaining_minutes(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        block = self._block(self.alice, task["task_id"], "2026-09-14T18:00:00", "2026-09-14T18:40:00", 40)
        study_planner_service.complete_block(self.alice, block["block_id"])
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["remaining_minutes"], 80)

    def test_multiple_completed_blocks_accumulate_remaining_minutes_reduction(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 120)
        first = self._block(self.alice, task["task_id"], "2026-09-14T18:00:00", "2026-09-14T18:40:00", 40)
        second = self._block(self.alice, task["task_id"], "2026-09-15T18:00:00", "2026-09-15T18:50:00", 50)
        study_planner_service.complete_block(self.alice, first["block_id"])
        study_planner_service.complete_block(self.alice, second["block_id"])
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["remaining_minutes"], 30)

    def test_remaining_minutes_never_goes_negative(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 30)
        block = self._block(self.alice, task["task_id"], "2026-09-14T18:00:00", "2026-09-14T18:30:00", 30)
        study_planner_service.complete_block(self.alice, block["block_id"], actual_minutes=90)
        self.assertEqual(study_planner_store.get_task(self.alice, task["task_id"])["remaining_minutes"], 0)

    def test_remaining_minutes_decrement_is_owner_scoped(self):
        alice_task = study_planner_service.create_task(self.alice, "Alice Prep", "2026-09-20", 60)
        bob_task = study_planner_service.create_task(self.bob, "Bob Prep", "2026-09-20", 60)
        alice_block = self._block(self.alice, alice_task["task_id"], "2026-09-14T18:00:00", "2026-09-14T18:30:00", 30)
        study_planner_service.complete_block(self.alice, alice_block["block_id"])
        self.assertEqual(study_planner_store.get_task(self.bob, bob_task["task_id"])["remaining_minutes"], 60)
        self.assertEqual(study_planner_store.get_task(self.alice, alice_task["task_id"])["remaining_minutes"], 30)


class StudyPlannerResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "planner.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(study_planner_store, "DATABASE_PATH", self.db),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-reset@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-reset@example.com", "long-password-b")["id"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_reset_deletes_only_current_users_planner_data(self):
        task = study_planner_service.create_task(self.alice, "Prep", "2026-09-20", 60, document_id="notes.pdf")
        study_planner_store.add_availability(self.alice, "18:00", "19:00", date="2026-09-14")
        study_planner_store.create_plan_items(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Study Topic A",
            "estimated_minutes": 60, "remaining_minutes": 60, "priority": 0,
        }])
        study_planner_store.create_blocks(self.alice, task["task_id"], [{
            "document_id": "notes.pdf", "topic_id": "topic-a", "title": "Session",
            "start_at": "2026-09-14T18:00:00", "end_at": "2026-09-14T19:00:00", "planned_minutes": 60,
        }])
        study_planner_store.upsert_topic_progress(
            self.alice, "notes.pdf", "topic-a", planned_minutes=60,
            completed_minutes_delta=30, last_studied_at="2026-09-14T18:30:00",
        )

        study_planner_service.create_task(self.bob, "Bob Prep", "2026-09-20", 30)
        study_planner_store.add_availability(self.bob, "18:00", "19:00", date="2026-09-14")

        study_planner_store.reset_planner_data(self.alice)

        self.assertEqual(study_planner_store.list_tasks(self.alice), [])
        self.assertEqual(study_planner_store.list_availability(self.alice), [])
        self.assertEqual(study_planner_store.list_blocks(self.alice), [])
        self.assertEqual(study_planner_store.list_plan_items(self.alice, task["task_id"]), [])
        self.assertEqual(study_planner_store.list_topic_progress(self.alice), [])

        # Bob's planner data is untouched.
        self.assertEqual(len(study_planner_store.list_tasks(self.bob)), 1)
        self.assertEqual(len(study_planner_store.list_availability(self.bob)), 1)

        # The user account itself is untouched -- reset only clears planner tables (verified by
        # being able to create a fresh task for the same owner without a foreign-key failure).
        new_task = study_planner_service.create_task(self.alice, "New Prep", "2026-09-25", 30)
        self.assertEqual(new_task["owner_id"], self.alice)

    def test_reset_never_touches_a_different_owners_data(self):
        study_planner_service.create_task(self.bob, "Bob Prep", "2026-09-20", 30)
        study_planner_store.reset_planner_data(self.alice)
        self.assertEqual(len(study_planner_store.list_tasks(self.bob)), 1)


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

    def test_block_editor_has_complete_button_and_progress_indicator(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="planner-block-editor-complete"', script)
        self.assertIn('id="planner-block-editor-progress"', script)
        self.assertIn('id="planner-block-editor-completed"', script)
        self.assertIn("function plannerCompleteBlock", script)
        self.assertIn("/complete", script)
        self.assertIn("block-completed", script)

    def test_task_cards_show_progress_and_regenerate_action(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("function plannerComputeTaskProgress", script)
        self.assertIn("planner-task-progress", script)
        self.assertIn("Regenerate Study Plan", script)
        self.assertIn("/regenerate", script)


if __name__ == "__main__":
    unittest.main()
