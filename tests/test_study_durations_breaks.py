"""Study Planner activity durations and study breaks (scheduler rules only).

New sessions get activity-specific lengths from one place (SchedulerConfig.durations): summary 45,
flashcards 20, quiz 35, weak-topic review 30, final review 45 -- never below 20 minutes -- with a
20-minute break between consecutive sessions (also around accepted ones). A session never goes into a
window too short for it; what does not fit is reported. Existing sessions keep their own length.
"Now" is Mon 2026-09-28 08:00.
"""

import unittest
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin
from test_study_scheduler import (
    DEFAULT_CONFIG, ScheduleAssertions, context, day_of, make_state, material, minute_of, weekly,
)

from backend import study_plan_api_service, study_planner_store
from backend.main import app
from backend.study_scheduler import estimate_duration, plan_schedule
from backend.study_session_duration import MIN_SESSION_MINUTES

EVENINGS = weekly(*((d, "19:00", "21:00") for d in range(7)))
EXPECTED = {"summary": 45, "flashcards": 20, "quiz": 35, "review": 30, "final_review": 45}


def google_busy(day, start, end):
    return {"busy": True, "date": day, "start_at": start, "end_at": end, "is_recurring": False, "day_of_week": None}


def by_day(result):
    days = defaultdict(list)
    for p in result.proposals:
        days[p.scheduled_start[:10]].append(p)
    return {key: sorted(items, key=lambda p: p.scheduled_start) for key, items in days.items()}


class DurationAndBreakTests(ScheduleAssertions, unittest.TestCase):
    def schedule(self, materials, availability=EVENINGS, busy=(), config=DEFAULT_CONFIG):
        ctx = context(materials, availability, busy=busy)
        result = plan_schedule(ctx, config)
        if config is DEFAULT_CONFIG:
            self.assert_well_formed(result, ctx)
        return result

    # -- durations -------------------------------------------------------------------------------

    def test_activity_specific_durations(self):
        self.assertEqual({key: estimate_duration(key) for key in ("summary", "flashcards", "quiz", "review")},
                         {key: EXPECTED[key] for key in ("summary", "flashcards", "quiz", "review")})
        self.assertEqual(estimate_duration("review", final=True), 45)
        new = self.schedule([material(make_state("mkt"), "2026-10-09")])
        lengths = {("final_review" if p.reason.code == "final_review" else p.activity_type): p.duration_minutes
                   for p in new.proposals}
        self.assertEqual(lengths, {"summary": 45, "flashcards": 20, "quiz": 35, "final_review": 45})
        # A moderate score brings a weak-topic (spaced) review and a retry quiz.
        weak = self.schedule([material(make_state("stats", summary=True, cards=20, percentage=60.0))])
        self.assertEqual({p.activity_type: p.duration_minutes for p in weak.proposals},
                         {"review": 30, "flashcards": 20, "quiz_retry": 35})

    def test_durations_do_not_depend_on_document_size(self):
        small, large = make_state("s", chunks=2, cards=4), make_state("l", chunks=200, cards=300)
        for activity in ("summary", "flashcards", "quiz", "review"):
            self.assertEqual(estimate_duration(activity, small), estimate_duration(activity, large), activity)

    def test_durations_are_configurable_in_one_place_and_never_below_the_minimum(self):
        config = replace(DEFAULT_CONFIG, durations={**DEFAULT_CONFIG.durations, "summary": 60, "flashcards": 10})
        self.assertEqual(estimate_duration("summary", config=config), 60)
        self.assertEqual(estimate_duration("flashcards", config=config), MIN_SESSION_MINUTES)
        result = self.schedule([material(make_state("mkt"))], weekly((0, "18:00", "21:00"), (2, "18:00", "21:00"),
                                                                     (4, "18:00", "21:00")), config=config)
        lengths = {p.activity_type: p.duration_minutes for p in result.proposals}
        self.assertEqual((lengths["summary"], lengths["flashcards"]), (60, 20))

    # -- breaks ------------------------------------------------------------------------------------

    def test_twenty_minute_break_between_consecutive_sessions(self):
        self.assertEqual(DEFAULT_CONFIG.session_gap_minutes, 20)
        result = self.schedule([material(make_state(doc), "2026-09-30") for doc in ("a", "b")],
                               weekly((0, "18:00", "22:00"), (1, "18:00", "22:00")))
        busy_days = [items for items in by_day(result).values() if len(items) > 1]
        self.assertTrue(busy_days)
        for items in busy_days:
            for before, after in zip(items, items[1:]):
                self.assertGreaterEqual(minute_of(after.scheduled_start) - minute_of(before.scheduled_end), 20)
            self.assertLessEqual(sum(p.duration_minutes for p in items), DEFAULT_CONFIG.daily_target_minutes)

    def test_break_is_kept_around_accepted_sessions(self):
        accepted = [{"scheduled_start": "2026-09-28T19:00:00", "scheduled_end": "2026-09-28T19:30:00", "status": "scheduled"}]
        result = self.schedule([material(make_state("mkt"), "2026-09-30")], weekly((0, "18:00", "21:00")), busy=accepted)
        monday = [p for p in result.proposals if p.scheduled_start.startswith("2026-09-28")]
        self.assertTrue(monday)
        for p in monday:
            ends_before = minute_of(p.scheduled_end) <= 19 * 60 - 20
            starts_after = minute_of(p.scheduled_start) >= 19 * 60 + 30 + 20
            self.assertTrue(ends_before or starts_after, p)

    # -- narrow windows and capacity -------------------------------------------------------------

    def test_a_session_never_goes_into_a_window_too_short_for_it(self):
        # 30-minute evenings: a 20-minute flashcards session fits, a 45-minute summary never does.
        result = self.schedule([material(make_state("mkt"), "2026-10-05")], weekly(*((d, "19:00", "19:30") for d in range(7))))
        self.assertNotIn("summary", [p.activity_type for p in result.proposals])
        self.assertEqual(result.capacity.status, "at_risk")
        self.assertIn(("summary", 45), [(c.activity_type, c.estimated_minutes) for c in result.capacity.unscheduled])
        for p in result.proposals:
            self.assertLessEqual(p.duration_minutes, 30)
        # Exactly 45 minutes is enough for the summary.
        exact = self.schedule([material(make_state("mkt"))], weekly((0, "19:00", "19:45")))
        self.assertEqual([(p.activity_type, p.scheduled_start, p.scheduled_end) for p in exact.proposals][0],
                         ("summary", "2026-09-28T19:00:00", "2026-09-28T19:45:00"))

    def test_google_calendar_busy_time_splits_a_window(self):
        # 19:00-21:00 with a Google event 19:40-20:00: the 45-minute summary only fits after it.
        availability = weekly((0, "19:00", "21:00")) + (google_busy("2026-09-28", "19:40", "20:00"),)
        result = self.schedule([material(make_state("mkt"))], availability)
        summary = next(p for p in result.proposals if p.activity_type == "summary")
        self.assertEqual((summary.scheduled_start, summary.scheduled_end), ("2026-09-28T20:00:00", "2026-09-28T20:45:00"))
        for p in result.proposals:
            if p.scheduled_start.startswith("2026-09-28"):
                self.assertFalse(p.scheduled_start[11:16] < "20:00" and p.scheduled_end[11:16] > "19:40", p)

    def test_deadlines_hold_and_shortfall_is_reported_with_longer_sessions(self):
        materials = [material(make_state("a"), "2026-10-01"), material(make_state("b"), "2026-10-01")]
        result = self.schedule(materials, weekly((0, "19:00", "20:30"), (1, "19:00", "20:30"), (2, "19:00", "20:30")))
        self.assertEqual(result.capacity.status, "at_risk")
        self.assertEqual(sum(c.estimated_minutes for c in result.capacity.unscheduled), result.capacity.shortfall_minutes)
        for p in result.proposals:
            self.assertLess(day_of(p.scheduled_start), date(2026, 10, 1))
        for items in by_day(result).values():
            self.assertLessEqual(sum(p.duration_minutes for p in items), DEFAULT_CONFIG.daily_target_minutes)

    # -- Task 2 distribution with longer sessions -------------------------------------------------

    def test_balanced_distribution_still_holds(self):
        result = self.schedule([material(make_state(doc)) for doc in ("mkt", "stats", "pbi")])
        self.assertEqual(result.capacity.status, "on_track")
        self.assertGreaterEqual(len({p.scheduled_start[:10] for p in result.proposals}), 7)
        self.assertEqual(max(Counter((p.document_id, p.scheduled_start[:10]) for p in result.proposals).values()), 1)
        far = self.schedule([material(make_state("mkt"), "2026-10-08")])
        learn = [day_of(p.scheduled_start) for p in far.proposals if p.reason.code != "final_review"]
        self.assertEqual(len(set(learn)), 3)
        self.assertGreaterEqual((learn[-1] - learn[0]).days, 4)


class AcceptedSessionPreservationTests(PlannerDatabaseMixin, unittest.TestCase):
    """Accepted sessions keep their own (user-defined) length and time; new plans work around them."""

    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        for doc, title in (("mkt", "Marketing"), ("stats", "Statistics")):
            self.add_document(self.alice, doc, title)
        self.client = TestClient(app)
        self.assertEqual(self.client.post("/api/auth/login", json={"email": "alice-planner@example.com",
                                                                   "password": "long-password-x"}).status_code, 200)
        for day in range(7):
            self.client.post("/api/planner/availability", json={"start_at": "19:00", "end_at": "21:00",
                                                                "is_recurring": True, "day_of_week": day})

    def test_accepted_custom_lengths_are_never_resized_or_overlapped(self):
        first = self.client.post("/api/planner/plans", json={"title": "Old"}).json()
        study_planner_store.add_material(self.alice, first["plan_id"], "mkt")
        # Accepted earlier with the old short lengths / a hand-picked time.
        accepted = study_planner_store.confirm_plan_sessions(self.alice, first["plan_id"], [
            {"document_id": "mkt", "activity_type": "summary", "scheduled_start": "2026-09-28T19:00:00",
             "scheduled_end": "2026-09-28T19:25:00", "duration_minutes": 25},
            {"document_id": "mkt", "activity_type": "quiz", "scheduled_start": "2026-09-29T19:10:00",
             "scheduled_end": "2026-09-29T19:25:00", "duration_minutes": 15},
        ])["sessions"]
        second = self.client.post("/api/planner/plans", json={"title": "New"}).json()
        self.client.post(f"/api/planner/plans/{second['plan_id']}/materials", json={"document_id": "stats"})
        body = {"utc_offset_minutes": 0, "local_now": "2026-09-28T08:00:00"}
        confirmed = self.client.post(f"/api/planner/plans/{second['plan_id']}/confirm", json=body)
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        after = study_planner_store.list_sessions(self.alice, plan_id=first["plan_id"])
        self.assertEqual([(s["scheduled_start"], s["scheduled_end"], s["duration_minutes"], s["status"]) for s in after],
                         [(s["scheduled_start"], s["scheduled_end"], s["duration_minutes"], s["status"]) for s in accepted])
        new = confirmed.json()["sessions"]
        self.assertTrue(new)
        for session in new:
            for old in accepted:
                if session["scheduled_start"][:10] != old["scheduled_start"][:10]:
                    continue
                gap_after = minute_of(session["scheduled_start"]) - minute_of(old["scheduled_end"])
                gap_before = minute_of(old["scheduled_start"]) - minute_of(session["scheduled_end"])
                self.assertTrue(gap_after >= 20 or gap_before >= 20, (session, old))
        self.assertEqual({s["activity_type"]: s["duration_minutes"] for s in new if s["activity_type"] != "review"},
                         {"summary": 45, "flashcards": 20, "quiz": 35})
        # Moving an accepted session keeps its own length.
        moved = self.client.post(f"/api/planner/sessions/{accepted[1]['session_id']}/reschedule",
                                 json={"target_start": "2026-10-02T20:00:00", "utc_offset_minutes": 0,
                                       "local_now": "2026-09-28T08:00:00"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["session"]["duration_minutes"], 15)


if __name__ == "__main__":
    unittest.main()
