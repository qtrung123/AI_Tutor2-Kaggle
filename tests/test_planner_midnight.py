"""Study Planner: availability that runs to midnight (24:00) -- Auto Plan regression.

A window ending at 24:00 is minute 1440, so a session that fills it to the end is stored as the
next day's 00:00. Building that datetime with hour=minute // 60 raised "hour must be in 0..23" as
soon as enough sessions (e.g. a second material added through Add Material) filled a late-evening
window to its end; reading such a session's end back as minute 0 also turned its busy interval
inside out, so later sessions could overlap it.
"""

import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

import test_study_scheduler as fx   # module import: keeps pytest from re-collecting its tests here
from test_study_adaptation import session as make_session

from backend import study_plan_api_service, study_planner_service, study_planner_store
from backend.main import app
from backend.study_adaptation import _Calendar, AdaptationContext, AdaptationTrigger
from backend.study_scheduler import DEFAULT_CONFIG, find_next_slot, plan_schedule
from backend.study_time import at_minute, end_minute, free_minutes_by_date, to_minutes

MONDAY = datetime(2026, 9, 28, 8, 0)
DEADLINE = "2026-10-10"


def span(start: str, end: str) -> tuple[date, int, int]:
    """(start day, start minute, end minute counted from the start day's midnight)."""
    return date.fromisoformat(start[:10]), int(start[11:13]) * 60 + int(start[14:16]), end_minute(start, end)


def materials(count: int):
    return [fx.material(fx.make_state(f"d{index}", summary=index % 2 == 0, cards=10 * (index % 2)), DEADLINE)
            for index in range(count)]


class LateNightAssertions:
    def assert_inside_window_and_disjoint(self, sessions, window_start: int, busy=()):
        by_day = defaultdict(list)
        for start, end, duration in sessions:
            day, first, last = span(start, end)
            self.assertGreaterEqual(first, window_start, start)
            self.assertLessEqual(last, 24 * 60, (start, end))         # never past the 24:00 boundary
            self.assertEqual(last - first, duration, (start, end))
            self.assertLessEqual(end[:10], (day + timedelta(days=1)).isoformat())
            by_day[day].append((first, last))
        for intervals in by_day.values():
            intervals.sort()
            for (_, before_end), (after_start, _) in zip(intervals, intervals[1:]):
                self.assertGreaterEqual(after_start - before_end, DEFAULT_CONFIG.session_gap_minutes)
        for start, end, _ in sessions:
            for other in busy:
                self.assertFalse(start < other["scheduled_end"] and other["scheduled_start"] < end, (start, end, other))


class MidnightHelperTests(unittest.TestCase):
    def test_minute_1440_is_the_next_days_midnight(self):
        monday = date(2026, 9, 28)
        self.assertEqual(at_minute(monday, 22 * 60 + 45), "2026-09-28T22:45:00")
        self.assertEqual(at_minute(monday, 24 * 60), "2026-09-29T00:00:00")
        self.assertEqual(at_minute(date(2026, 12, 31), 24 * 60), "2027-01-01T00:00:00")

    def test_end_minute_counts_from_the_start_day(self):
        self.assertEqual(end_minute("2026-09-28T22:00:00", "2026-09-28T23:15:00"), 23 * 60 + 15)
        self.assertEqual(end_minute("2026-09-28T23:15:00", "2026-09-29T00:00:00"), 24 * 60)

    def test_a_block_ending_at_midnight_still_blocks_its_evening(self):
        block = {"status": "confirmed", "start_at": "2026-09-28T23:00:00", "end_at": "2026-09-29T00:00:00"}
        free = free_minutes_by_date(date(2026, 9, 28), date(2026, 9, 28), list(fx.every_day("22:00", "24:00")), [block])
        self.assertEqual(free[date(2026, 9, 28)], [(22 * 60, 23 * 60)])


class AutoPlanLateNightTests(LateNightAssertions, unittest.TestCase):
    def test_multiple_documents_fill_a_late_window_to_midnight(self):
        reached_midnight = False
        for window_start in ("22:00", "23:00", "23:15"):
            for count in (1, 2, 3):
                with self.subTest(window=window_start, documents=count):
                    result = plan_schedule(fx.context(materials(count), fx.every_day(window_start, "24:00"), now=MONDAY))
                    self.assertTrue(result.proposals)
                    sessions = [(p.scheduled_start, p.scheduled_end, p.duration_minutes) for p in result.proposals]
                    self.assert_inside_window_and_disjoint(sessions, to_minutes(window_start))
                    reached_midnight |= any(end.endswith("T00:00:00") for _, end, _ in sessions)
        self.assertTrue(reached_midnight, "no schedule exercised a session ending at 24:00")

    def test_busy_session_ending_at_midnight_is_not_overlapped(self):
        busy = (make_session("other", "elsewhere", "review", "2026-09-28T23:15:00", 45),
                make_session("other-2", "elsewhere", "review", "2026-09-29T23:15:00", 45))
        self.assertEqual(busy[0]["scheduled_end"], "2026-09-29T00:00:00")
        result = plan_schedule(fx.context(materials(2), fx.every_day("22:00", "24:00"), now=MONDAY, busy=busy))
        sessions = [(p.scheduled_start, p.scheduled_end, p.duration_minutes) for p in result.proposals]
        self.assertTrue(sessions)
        self.assert_inside_window_and_disjoint(sessions, 22 * 60, busy)

    def test_find_next_slot_can_end_at_midnight(self):
        ctx = fx.context(materials(1), fx.every_day("23:15", "24:00"), now=MONDAY)
        self.assertEqual(find_next_slot(ctx, 45, date(2026, 9, 28)), ("2026-09-28T23:15:00", "2026-09-29T00:00:00"))

    def test_legacy_task_scheduler_can_end_at_midnight(self):
        task = {"title": "Essay", "deadline": "2026-09-28", "remaining_minutes": 60}
        result = study_planner_service.compute_schedule(task, list(fx.every_day("23:00", "24:00")), [], now=MONDAY)
        self.assertEqual([(b["start_at"], b["end_at"]) for b in result["blocks"]],
                         [("2026-09-28T23:00:00", "2026-09-29T00:00:00")])


class AdaptationLateNightTests(unittest.TestCase):
    def calendar(self, busy):
        context = AdaptationContext(trigger=AdaptationTrigger("quiz_completed", document_id="d0"), now=MONDAY,
                                    utc_offset=timedelta(0), materials=(), plan_sessions=tuple(busy),
                                    availability=fx.every_day("22:00", "24:00"), busy_sessions=tuple(busy))
        return _Calendar(context, MONDAY, DEFAULT_CONFIG)

    def test_slot_ending_at_midnight_and_busy_until_midnight(self):
        empty = self.calendar(())
        self.assertEqual(empty.find_on(date(2026, 9, 28), "d0", "review", 30, not_before="2026-09-28T23:30:00"),
                         ("2026-09-28T23:30:00", "2026-09-29T00:00:00"))
        self.assertTrue(empty.within_availability("2026-09-28T23:30:00", "2026-09-29T00:00:00"))

        late = make_session("late", "d1", "review", "2026-09-28T23:15:00", 45)
        calendar = self.calendar((late,))
        start, end = calendar.find_on(date(2026, 9, 28), "d0", "review", 30)
        self.assertLessEqual(end, "2026-09-28T22:55:00")    # ends a session gap before 23:15
        self.assertIsNone(calendar.find_on(date(2026, 9, 28), "d0", "review", 30, not_before="2026-09-28T22:30:00"))


class AutoPlanApiLateNightTests(PlannerDatabaseMixin, LateNightAssertions, unittest.TestCase):
    """Reproduction from the report: plan one document, Add Material a second, Auto Plan again."""

    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        self.add_document(self.alice, "first", "First")
        self.add_document(self.alice, "second", "Second")
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com",
                                                             "password": "long-password-x"})
        self.assertEqual(response.status_code, 200, response.text)
        for day in range(7):
            response = self.client.post("/api/planner/availability", json={
                "start_at": "23:15", "end_at": "24:00", "is_recurring": True, "day_of_week": day})
            self.assertEqual(response.status_code, 200, response.text)

    def auto_plan(self, plan_id, action):
        return self.client.post(f"/api/planner/plans/{plan_id}/{action}",
                                json={"utc_offset_minutes": 0, "local_now": "2026-09-28T08:00:00"})

    def test_second_material_auto_plans_into_late_night_availability(self):
        plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        base = f"/api/planner/plans/{plan['plan_id']}/materials"
        self.assertEqual(self.client.post(base, json={"document_id": "first", "deadline": DEADLINE}).status_code, 201)
        self.assertEqual(self.auto_plan(plan["plan_id"], "preview").status_code, 200)
        self.assertEqual(self.client.post(base, json={"document_id": "second", "deadline": DEADLINE}).status_code, 201)

        preview = self.auto_plan(plan["plan_id"], "preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        confirmed = self.auto_plan(plan["plan_id"], "confirm")
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        saved = study_planner_store.list_sessions(self.alice, plan_id=plan["plan_id"])
        self.assertEqual({s["document_id"] for s in saved}, {"first", "second"})
        self.assertTrue(any(s["scheduled_end"].endswith("T00:00:00") for s in saved))
        self.assert_inside_window_and_disjoint(
            [(s["scheduled_start"], s["scheduled_end"], s["duration_minutes"]) for s in saved], 23 * 60 + 15)


if __name__ == "__main__":
    unittest.main()
