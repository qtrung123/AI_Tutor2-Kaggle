"""Deterministic Study Planner scheduler (Phase 2 MVP)."""

import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from planner_fixtures import PlannerDatabaseMixin

from backend import study_planner_service, study_planner_store
from backend.document_study_state import (
    DocumentStudyState, FlashcardState, QuizAttemptResult, QuizState, SummaryState, derive_learning_state,
)
from backend.study_planner_store import SESSION_REASONS
from backend.study_scheduler import (
    DEFAULT_CONFIG, base_priority, estimate_duration, local_date, plan_schedule,
    review_interval_days, select_candidates,
)
from backend.study_scheduler_contracts import MaterialContext, SchedulingContext

MONDAY = datetime(2026, 9, 28, 8, 0)
GOLDEN_AVAILABILITY = (  # Mon 18-22, Tue 20-21, Thu 19-22, Sat 09-12, every week
    (0, "18:00", "22:00"), (1, "20:00", "21:00"), (3, "19:00", "22:00"), (5, "09:00", "12:00"),
)
YESTERDAY_EVENING = "2026-09-27T19:00:00+00:00"


def weekly(*slots):
    return tuple({"is_recurring": True, "day_of_week": d, "start_at": a, "end_at": b, "date": None} for d, a, b in slots)


def every_day(start="09:00", end="22:00"):
    return weekly(*((d, start, end) for d in range(7)))


def make_state(doc, title=None, chunks=12, summary=False, cards=0, percentage=None, completed_at=YESTERDAY_EVENING,
               in_progress=False, questions=10, planner_state=None):
    summary_state = SummaryState(summary, f"sum-{doc}" if summary else None)
    flashcards = FlashcardState(bool(cards), f"set-{doc}" if cards else None, cards)
    quiz = QuizState()
    if percentage is not None:
        quiz = QuizState(quiz_count=1, latest_quiz_id=f"q-{doc}", latest_quiz_status="completed",
                         latest_quiz_question_count=questions, completed_attempt_count=1,
                         latest_completed=QuizAttemptResult(f"a-{doc}", f"q-{doc}", round(percentage / 10), 10,
                                                            percentage, completed_at))
    elif in_progress:
        quiz = QuizState(quiz_count=1, latest_quiz_id=f"q-{doc}", latest_quiz_status="in_progress",
                         latest_quiz_question_count=questions, in_progress_count=1)
    learning_state, reason = derive_learning_state(summary_state, flashcards, quiz, 0, planner_state)
    return DocumentStudyState("u", doc, title or doc, chunks, summary_state, flashcards, quiz, 0, None, None,
                              None, planner_state, learning_state, reason)


def material(state, deadline=None):
    return MaterialContext({"document_id": state.document_id, "deadline": deadline}, state)


UTC = timedelta(0)


def context(materials, availability=GOLDEN_AVAILABILITY, now=MONDAY, busy=(), utc_offset=UTC):
    if availability and isinstance(availability[0], tuple):
        availability = weekly(*availability)
    return SchedulingContext("u", "p", now, tuple(materials), tuple(availability), tuple(busy), utc_offset)


def day_of(value: str) -> date:
    return date.fromisoformat(value[:10])


def minute_of(value: str) -> int:
    return int(value[11:13]) * 60 + int(value[14:16])


class ScheduleAssertions:
    def assert_well_formed(self, result, ctx):
        """Invariants every schedule must satisfy."""
        by_day = defaultdict(list)
        for proposal in result.proposals:
            self.assertIn(proposal.reason.code, SESSION_REASONS)
            self.assertTrue(proposal.reason.message)
            self.assertGreaterEqual(datetime.fromisoformat(proposal.scheduled_start), ctx.now)
            self.assertLessEqual(proposal.duration_minutes, DEFAULT_CONFIG.max_block_minutes)
            low, high, _ = DEFAULT_CONFIG.durations[proposal.activity_type]
            self.assertTrue(low <= proposal.duration_minutes <= high, proposal)
            by_day[proposal.scheduled_start[:10]].append(proposal)
        for sessions in by_day.values():
            sessions.sort(key=lambda p: p.scheduled_start)
            self.assertLessEqual(sum(p.duration_minutes for p in sessions), DEFAULT_CONFIG.daily_target_minutes)
            for before, after in zip(sessions, sessions[1:]):
                self.assertGreaterEqual(minute_of(after.scheduled_start) - minute_of(before.scheduled_end),
                                        DEFAULT_CONFIG.session_gap_minutes)
        for busy in ctx.busy_sessions:
            for proposal in result.proposals:
                self.assertFalse(proposal.scheduled_start < busy["scheduled_end"]
                                 and busy["scheduled_start"] < proposal.scheduled_end, (proposal, busy))
        capacity = result.capacity
        self.assertEqual(capacity.required_minutes, capacity.scheduled_minutes + capacity.shortfall_minutes)
        self.assertEqual(capacity.scheduled_minutes, sum(p.duration_minutes for p in result.proposals))
        self.assertLessEqual(capacity.scheduled_minutes, capacity.schedulable_minutes)
        self.assertLessEqual(capacity.schedulable_minutes, capacity.available_minutes)
        retrieval_days = {(p.document_id, p.scheduled_start[:10]) for p in result.proposals
                          if p.activity_type in DEFAULT_CONFIG.retrieval_activities}
        for p in result.proposals:
            if p.reason.code == "final_review":
                self.assertNotIn((p.document_id, p.scheduled_start[:10]), retrieval_days, p)


class SchedulerUnitTests(ScheduleAssertions, unittest.TestCase):

    # -- 1. candidate selection ------------------------------------------------

    def kinds(self, state, deadline=None):
        return [(c.activity_type, c.reason.code) for c in select_candidates(material(state, deadline), MONDAY)]

    def test_candidate_selection_per_state(self):
        self.assertEqual(self.kinds(make_state("d")), [
            ("summary", "new_material"), ("flashcards", "new_material"), ("quiz", "new_material")])
        self.assertEqual(self.kinds(make_state("d"), "2026-10-01"), [
            ("summary", "deadline_approaching"), ("flashcards", "deadline_approaching"),
            ("quiz", "deadline_approaching"), ("review", "final_review")])
        self.assertEqual(self.kinds(make_state("d", summary=True)), [("flashcards", "new_material"), ("quiz", "new_material")])
        self.assertEqual(self.kinds(make_state("d", summary=True, cards=10)), [("quiz", "new_material")])
        self.assertEqual(self.kinds(make_state("d", in_progress=True)), [("quiz", "quiz_in_progress")])
        self.assertEqual(self.kinds(make_state("d", percentage=40.0)), [
            ("flashcards", "low_quiz_score"), ("quiz_retry", "low_quiz_score")])
        self.assertEqual(self.kinds(make_state("d", percentage=70.0)), [("review", "review_due"), ("quiz_retry", "review_due")])
        self.assertEqual(self.kinds(make_state("d", percentage=95.0)), [("review", "review_due")])
        self.assertEqual(self.kinds(make_state("d", percentage=95.0, planner_state="completed")), [])
        self.assertEqual(self.kinds(make_state("d", planner_state="completed"), "2026-10-10"), [("review", "final_review")])
        self.assertEqual(self.kinds(make_state("d"), "2026-09-20"), [])  # deadline already passed

    def test_candidates_reference_artifacts_only_when_they_exist(self):
        new = select_candidates(material(make_state("d")), MONDAY)
        self.assertTrue(all(c.artifact_id is None for c in new))
        in_progress = select_candidates(material(make_state("d", in_progress=True)), MONDAY)
        self.assertEqual(in_progress[0].artifact_id, "q-d")
        low = select_candidates(material(make_state("d", cards=12, percentage=40.0)), MONDAY)
        self.assertEqual([c.artifact_id for c in low], ["set-d", "q-d"])

    # -- 3/4. spaced review and durations -------------------------------------

    def test_review_intervals(self):
        cases = {0: 1, 49.9: 1, 50: 2, 74.9: 2, 75: 3, 84.9: 3, 85: 5, 100: 5}
        for percentage, days in cases.items():
            self.assertEqual(review_interval_days(percentage), days, percentage)

    def test_durations_are_activity_appropriate_and_scale_with_artifact_size(self):
        unknown = make_state("d", chunks=0)
        defaults = {activity: estimate_duration(activity, unknown) for activity in DEFAULT_CONFIG.durations}
        self.assertEqual(defaults, {"summary": 40, "flashcards": 20, "quiz": 30, "review": 15, "quiz_retry": 25})
        self.assertEqual(estimate_duration("summary", make_state("d", chunks=4)), 30)
        self.assertEqual(estimate_duration("summary", make_state("d", chunks=40)), 50)
        self.assertEqual(estimate_duration("flashcards", make_state("d", cards=8)), 15)
        self.assertEqual(estimate_duration("flashcards", make_state("d", cards=24)), 25)
        self.assertEqual(estimate_duration("flashcards", make_state("d", cards=90)), 30)
        self.assertEqual(estimate_duration("review", make_state("d", cards=30)), 15)
        self.assertEqual(estimate_duration("quiz", make_state("d", in_progress=True, questions=5)), 20)
        self.assertEqual(estimate_duration("quiz", make_state("d", in_progress=True, questions=15)), 30)
        self.assertEqual(estimate_duration("quiz", make_state("d", in_progress=True, questions=40)), 40)
        self.assertEqual(estimate_duration("quiz_retry", make_state("d", percentage=40.0)), 20)

    def test_review_never_lands_after_deadline_and_final_review_precedes_it(self):  # noqa: D102
        state = make_state("d", percentage=95.0)  # next spaced review would be 5 days out
        result = plan_schedule(context([material(state, "2026-10-01")], every_day()))
        self.assertTrue(result.proposals)
        for proposal in result.proposals:
            self.assertLess(day_of(proposal.scheduled_start), date(2026, 10, 1))
        self.assertEqual([p.reason.code for p in result.proposals], ["final_review"])  # subsumes the late review

    def test_low_score_pulls_review_closer_than_high_score(self):
        low = make_state("low", cards=20, percentage=40.0)
        high = make_state("high", cards=20, percentage=95.0)
        result = plan_schedule(context([material(low), material(high)], every_day()))
        first = {doc: min(day_of(p.scheduled_start) for p in result.proposals if p.document_id == doc)
                 for doc in ("low", "high")}
        self.assertEqual(first["low"], MONDAY.date())                 # 1 day after yesterday's quiz
        self.assertEqual(first["high"], date(2026, 10, 2))            # 5 days after, never next-day
        retry = next(p for p in result.proposals if p.activity_type == "quiz_retry")
        self.assertGreater(day_of(retry.scheduled_start), first["low"])

    # -- 5/6. capacity -----------------------------------------------------------

    def test_availability_is_permission_not_obligation(self):
        materials = [material(make_state(f"d{i}"), None) for i in range(4)]
        ctx = context(materials, every_day())
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual(result.capacity.status, "on_track")
        self.assertLess(result.capacity.scheduled_minutes, result.capacity.available_minutes / 5)
        heavy_by_day = defaultdict(list)
        per_doc_day = defaultdict(int)
        for p in result.proposals:
            per_doc_day[(p.document_id, p.scheduled_start[:10])] += 1
            if p.activity_type in DEFAULT_CONFIG.heavy_activities:
                heavy_by_day[p.scheduled_start[:10]].append(p)
        self.assertLessEqual(max(per_doc_day.values()), DEFAULT_CONFIG.max_sessions_per_document_per_day)
        for sessions in heavy_by_day.values():
            self.assertLessEqual(len(sessions), DEFAULT_CONFIG.max_heavy_sessions_per_day)

    def test_insufficient_capacity_reports_shortfall_instead_of_packing(self):
        materials = [material(make_state("a"), "2026-09-30"), material(make_state("b"), "2026-09-30")]
        ctx = context(materials, weekly((0, "18:00", "19:00")))  # a single hour before both deadlines
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        capacity = result.capacity
        self.assertEqual(capacity.status, "at_risk")
        self.assertGreater(capacity.shortfall_minutes, 0)
        self.assertLessEqual(capacity.scheduled_minutes, 60)
        self.assertEqual(sum(c.estimated_minutes for c in capacity.unscheduled), capacity.shortfall_minutes)
        self.assertTrue(all(c.reason.code in SESSION_REASONS for c in capacity.unscheduled))

    def test_never_overlaps_busy_time_or_schedules_in_the_past(self):
        now = datetime(2026, 9, 28, 19, 10)
        busy = [{"scheduled_start": "2026-09-28T19:30:00", "scheduled_end": "2026-09-28T20:00:00", "status": "scheduled"}]
        ctx = context([material(make_state("a")), material(make_state("b", summary=True))],
                      weekly((0, "18:00", "22:00")), now=now, busy=busy)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        monday = [p for p in result.proposals if day_of(p.scheduled_start) == now.date()]
        self.assertTrue(monday)
        self.assertGreaterEqual(min(minute_of(p.scheduled_start) for p in monday), 20 * 60 + 10)

    # -- 2. priority -------------------------------------------------------------

    def test_nearest_deadline_gets_more_attention_without_starving_later_ones(self):
        materials = [material(make_state("near"), "2026-10-02"), material(make_state("mid"), "2026-10-09"),
                     material(make_state("far"), "2026-10-23")]
        ctx = context(materials, weekly(*((d, "19:00", "21:00") for d in range(5))))
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual(result.proposals[0].document_id, "near")
        first_week = [p for p in result.proposals if day_of(p.scheduled_start) < MONDAY.date() + timedelta(days=7)]
        minutes = defaultdict(int)
        for p in first_week:
            minutes[p.document_id] += p.duration_minutes
        self.assertEqual(set(minutes), {"near", "mid", "far"})  # nobody starves
        self.assertGreater(minutes["near"], minutes["far"])
        self.assertEqual(result.capacity.status, "on_track")

    def test_engine_is_deterministic(self):
        materials = [material(make_state("a"), "2026-10-05"), material(make_state("b", percentage=55.0), "2026-10-08")]
        ctx = context(materials)
        self.assertEqual(plan_schedule(ctx), plan_schedule(ctx))


class FinalRetrievalTests(ScheduleAssertions, unittest.TestCase):
    def test_quiz_on_last_usable_day_satisfies_final_retrieval(self):
        # Deadline Wed; only Mon and Tue are usable, so the quiz lands Tue -- the last usable day.
        ctx = context([material(make_state("d"), "2026-09-30")], weekly((0, "18:00", "22:00"), (1, "18:00", "22:00")))
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual([(p.activity_type, day_of(p.scheduled_start)) for p in result.proposals],
                         [("summary", date(2026, 9, 28)), ("flashcards", date(2026, 9, 28)), ("quiz", date(2026, 9, 29))])
        self.assertEqual((result.capacity.status, result.capacity.shortfall_minutes), ("on_track", 0))

    def test_retry_on_last_usable_day_satisfies_final_retrieval(self):
        ctx = context([material(make_state("d", cards=20, percentage=40.0), "2026-09-30")],
                      weekly((0, "18:00", "22:00"), (1, "18:00", "22:00")))
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual([p.activity_type for p in result.proposals], ["flashcards", "quiz_retry"])
        self.assertEqual(result.capacity.status, "on_track")

    def test_separate_final_review_only_on_a_later_day(self):
        ctx = context([material(make_state("d"), "2026-10-02")], every_day())
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        quiz = next(p for p in result.proposals if p.activity_type == "quiz")
        final = next(p for p in result.proposals if p.reason.code == "final_review")
        self.assertGreater(day_of(final.scheduled_start), day_of(quiz.scheduled_start))
        self.assertLess(day_of(final.scheduled_start), date(2026, 10, 2))
        self.assertEqual(result.proposals[-1], final)

    def test_final_review_is_still_required_when_the_quiz_was_long_before_the_window(self):
        # A high score leaves only the final review, and no quiz happens inside its window:
        # an unplaceable final review is a real shortfall, not silently "satisfied".
        ctx = context([material(make_state("d", percentage=95.0), "2026-10-05")], weekly((0, "18:00", "19:00")))
        result = plan_schedule(ctx)
        self.assertEqual(result.capacity.status, "at_risk")
        self.assertIn("final_review", [c.reason.code for c in result.capacity.unscheduled])


def fake_server_timezone(hours):
    """A datetime class whose "server local" timezone is UTC+hours -- any code path that consults
    the server timezone (astimezone() with no argument, naive now()) would see it."""
    zone = timezone(timedelta(hours=hours))

    class ServerDatetime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(tz or zone)

        @classmethod
        def now(cls, tz=None):
            real = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
            return real.astimezone(tz) if tz else real.astimezone(zone).replace(tzinfo=None)

    return ServerDatetime


class TimezoneTests(unittest.TestCase):
    LOW = dict(cards=20, percentage=40.0)  # 1-day review interval: first session = completion date + 1

    def first_session_day(self, completed_at, now=MONDAY, utc_offset=UTC):
        state = make_state("d", completed_at=completed_at, **self.LOW)
        result = plan_schedule(context([material(state)], every_day(), now=now, utc_offset=utc_offset))
        return day_of(result.proposals[0].scheduled_start)

    def test_local_date_uses_the_learner_offset_not_utc_truncation(self):
        stamp = "2026-09-27T20:30:00+00:00"  # Sep 27 in UTC, but already Sep 28 at UTC+7
        self.assertEqual(local_date(stamp, timedelta(hours=7), MONDAY.date()), date(2026, 9, 28))
        self.assertEqual(local_date(stamp, UTC, MONDAY.date()), date(2026, 9, 27))
        self.assertEqual(local_date("2026-09-28T02:00:00+00:00", timedelta(hours=-5), MONDAY.date()), date(2026, 9, 27))
        self.assertEqual(local_date("2026-09-27T20:30:00", timedelta(hours=7), MONDAY.date()), date(2026, 9, 28))
        self.assertEqual(local_date("garbage", UTC, MONDAY.date()), MONDAY.date())
        self.assertEqual(local_date(stamp, None, MONDAY.date()), date(2026, 9, 27))  # neutral UTC frame

    def test_utc_plus_7_midnight_boundary(self):
        # 20:30 UTC Sep 27 is 03:30 Mon Sep 28 at UTC+7 -> review due Tue; at UTC it is Sunday -> due Mon.
        self.assertEqual(self.first_session_day("2026-09-27T20:30:00+00:00", utc_offset=timedelta(hours=7)),
                         date(2026, 9, 29))
        self.assertEqual(self.first_session_day("2026-09-27T20:30:00+00:00", utc_offset=UTC), date(2026, 9, 28))

    def test_utc_minus_5_midnight_boundary(self):
        # 03:00 UTC Sep 28 is 22:00 Sun Sep 27 at UTC-5 -> review due Mon; at UTC it is Monday -> due Tue.
        self.assertEqual(self.first_session_day("2026-09-28T03:00:00+00:00", utc_offset=timedelta(hours=-5)),
                         date(2026, 9, 28))
        self.assertEqual(self.first_session_day("2026-09-28T03:00:00+00:00", utc_offset=UTC), date(2026, 9, 29))

    def test_server_timezone_never_affects_an_explicit_offset(self):
        state = make_state("d", completed_at="2026-09-27T20:30:00+00:00", **self.LOW)
        ctx = context([material(state), material(make_state("n"), "2026-10-02")], every_day(),
                      utc_offset=timedelta(hours=7))
        results = []
        for server_hours in (-11, 0, 13):
            with patch("backend.study_scheduler.datetime", fake_server_timezone(server_hours)):
                results.append(plan_schedule(ctx))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(day_of(results[0].proposals[0].scheduled_start), date(2026, 9, 28))

    def test_aware_now_supplies_its_own_offset(self):
        stamp = "2026-09-27T20:30:00+00:00"
        aware_now = datetime(2026, 9, 28, 8, 0, tzinfo=timezone(timedelta(hours=7)))
        self.assertEqual(self.first_session_day(stamp, now=aware_now, utc_offset=None), date(2026, 9, 29))
        naive = plan_schedule(context([material(make_state("d", completed_at=stamp, **self.LOW))], every_day(),
                                      utc_offset=timedelta(hours=7)))
        aware = plan_schedule(context([material(make_state("d", completed_at=stamp, **self.LOW))], every_day(),
                                      now=aware_now, utc_offset=None))
        self.assertEqual(naive, aware)  # proposals stay naive planner-local times

    def test_naive_now_without_offset_never_fabricates_a_learner_timezone(self):
        stamp = "2026-09-27T20:30:00+00:00"
        results = []
        for server_hours in (-11, 7, 13):
            with patch("backend.study_scheduler.datetime", fake_server_timezone(server_hours)):
                results.append(self.first_session_day(stamp, utc_offset=None))
        self.assertEqual(results, [date(2026, 9, 28)] * 3)  # neutral UTC calendar, whatever the server zone

    def test_scheduling_context_offset_is_explicit_only(self):
        with patch("backend.study_planner_service.datetime", fake_server_timezone(7)),                 patch.object(study_planner_store, "get_plan", return_value={"plan_id": "p"}),                 patch.object(study_planner_store, "list_materials", return_value=[]),                 patch.object(study_planner_store, "list_availability", return_value=[]),                 patch.object(study_planner_store, "list_sessions", return_value=[]):
            implicit = study_planner_service.build_scheduling_context("u", "p", now=MONDAY)
            explicit = study_planner_service.build_scheduling_context("u", "p", now=MONDAY,
                                                                      utc_offset=timedelta(hours=-5))
        self.assertIsNone(implicit.utc_offset)
        self.assertEqual(explicit.utc_offset, timedelta(hours=-5))

    def test_scheduler_never_converts_with_the_server_timezone(self):
        import inspect
        import re

        import backend.study_scheduler as scheduler
        self.assertIsNone(re.search(r"\.astimezone\(\s*\)", inspect.getsource(scheduler)))

class PriorityAndCapacityTests(ScheduleAssertions, unittest.TestCase):
    def test_base_priority_weights_are_the_centralized_35_35_20_10(self):
        config = DEFAULT_CONFIG
        self.assertEqual((config.weight_deadline, config.weight_review, config.weight_performance, config.weight_workload),
                         (0.35, 0.35, 0.20, 0.10))
        self.assertAlmostEqual(base_priority(1, 1, 1, 1), 1.0)
        for index, weight in enumerate((0.35, 0.35, 0.20, 0.10)):
            unit = [0, 0, 0, 0]
            unit[index] = 1
            self.assertAlmostEqual(base_priority(*unit), weight)

    def test_risk_uses_comfortable_capacity_not_raw_availability(self):
        # Deadline tomorrow: only today is usable. 13h of raw availability, but a comfortable day
        # is 90 min and the quiz may not share the day with new material.
        ctx = context([material(make_state("d"), "2026-09-29")], weekly((0, "09:00", "22:00")))
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        capacity = result.capacity
        self.assertGreater(capacity.available_minutes, capacity.required_minutes)   # raw would say "fine"
        self.assertEqual(capacity.schedulable_minutes, DEFAULT_CONFIG.daily_target_minutes)
        self.assertEqual(capacity.status, "at_risk")
        # The quiz cannot fit, so nothing counts as final retrieval: the final review is short too.
        self.assertEqual([(c.activity_type, c.reason.code) for c in capacity.unscheduled],
                         [("quiz", "deadline_approaching"), ("review", "final_review")])

    def test_busy_time_reduces_schedulable_capacity(self):
        busy = [{"scheduled_start": "2026-09-28T18:00:00", "scheduled_end": "2026-09-28T21:30:00", "status": "scheduled"}]
        ctx = context([material(make_state("d"), "2026-09-29")], weekly((0, "18:00", "22:00")), busy=busy)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual(result.capacity.schedulable_minutes, 20)  # 21:40-22:00 after the padded busy block
        self.assertEqual(result.capacity.status, "at_risk")


class GoldenScenarioTests(ScheduleAssertions, PlannerDatabaseMixin, unittest.TestCase):
    """Marketing +3d, Statistics +7d, PowerBI +14d; Mon 18-22, Tue 20-21, Thu 19-22, Sat 09-12."""

    MARKETING, STATISTICS, POWERBI = "2026-10-01", "2026-10-05", "2026-10-12"

    def setUp(self):
        self.start_planner_database()
        self.owner = self.user("Golden")
        self.plan = study_planner_store.create_plan(self.owner, "Exams")
        for day, start, end in GOLDEN_AVAILABILITY:
            study_planner_store.add_availability(self.owner, start, end, is_recurring=True, day_of_week=day)

    def add_material(self, document_id, title, deadline):
        self.add_document(self.owner, document_id, title)
        study_planner_store.add_material(self.owner, self.plan["plan_id"], document_id, deadline=deadline)

    def schedule(self):
        ctx = study_planner_service.build_scheduling_context(self.owner, self.plan["plan_id"], now=MONDAY,
                                                             utc_offset=UTC)
        result = plan_schedule(ctx)
        self.sessions = defaultdict(list)
        for proposal in result.proposals:
            self.sessions[proposal.document_id].append(proposal)
        # Proposals are valid session records (persisted here only to prove that).
        saved = study_planner_store.create_sessions(self.owner, self.plan["plan_id"],
                                                    [p.to_session_record() for p in result.proposals])
        self.assertEqual(len(saved), len(result.proposals))
        self.assert_well_formed(result, ctx)
        return result

    def minutes_before(self, document_id, day):
        return sum(p.duration_minutes for p in self.sessions[document_id] if day_of(p.scheduled_start) < day)

    def assert_final_retrieval_before_deadlines(self, deadlines, expected_final_kind):
        """The last session before each deadline is final retrieval: a separate final review on a
        later day than any quiz, or the quiz/quiz_retry itself when no later slot existed."""
        for document_id, deadline in deadlines.items():
            sessions = self.sessions[document_id]
            last = sessions[-1]
            window_start = date.fromisoformat(deadline) - timedelta(days=DEFAULT_CONFIG.final_review_window_days)
            self.assertGreaterEqual(day_of(last.scheduled_start), window_start, document_id)
            kind = "final_review" if last.reason.code == "final_review" else last.activity_type
            self.assertEqual(kind, expected_final_kind[document_id], document_id)
            self.assertEqual(sum(p.reason.code == "final_review" for p in sessions), int(kind == "final_review"))
            for p in sessions:
                self.assertLess(day_of(p.scheduled_start), date.fromisoformat(deadline))

    def test_golden_scenario_with_quiz_history(self):
        self.add_material("mkt", "Marketing", self.MARKETING)                     # brand new
        self.add_material("stats", "Statistics", self.STATISTICS)                 # low score yesterday
        self.add_summary(self.owner, "stats")
        self.add_flashcards(self.owner, "stats", 20)
        self.add_quiz(self.owner, "stats", "q-stats")
        self.add_attempt(self.owner, "stats", "q-stats", score=4, answered=10, completed=True,
                         completed_at=YESTERDAY_EVENING)
        self.add_material("pbi", "PowerBI", self.POWERBI)                         # high score yesterday
        self.add_summary(self.owner, "pbi")
        self.add_flashcards(self.owner, "pbi", 30)
        self.add_quiz(self.owner, "pbi", "q-pbi")
        self.add_attempt(self.owner, "pbi", "q-pbi", score=9, answered=10, completed=True,
                         completed_at=YESTERDAY_EVENING)
        result = self.schedule()

        self.assertEqual(result.capacity.status, "on_track")
        self.assertLess(result.capacity.scheduled_minutes, result.capacity.available_minutes / 3)  # not all filled
        # Marketing gets the most attention before its deadline, and everything it needs.
        thursday = date(2026, 10, 1)
        self.assertGreater(self.minutes_before("mkt", thursday), self.minutes_before("stats", thursday))
        self.assertGreater(self.minutes_before("mkt", thursday), self.minutes_before("pbi", thursday))
        # Its quiz lands on the last usable day before Thursday (Wed has no availability), so it is
        # the final retrieval -- no extra same-day final review.
        self.assertEqual([p.activity_type for p in self.sessions["mkt"]], ["summary", "flashcards", "quiz"])
        self.assertEqual(day_of(self.sessions["mkt"][-1].scheduled_start), date(2026, 9, 29))
        # Multiple documents interleaved on the same day.
        monday_docs = {p.document_id for docs in self.sessions.values() for p in docs if day_of(p.scheduled_start) == MONDAY.date()}
        self.assertEqual(monday_docs, {"mkt", "stats"})
        # Low score pulls review closer: Statistics starts today with flashcards, then a retry on a later day.
        stats = self.sessions["stats"]
        self.assertEqual((stats[0].activity_type, stats[0].reason.code, day_of(stats[0].scheduled_start)),
                         ("flashcards", "low_quiz_score", MONDAY.date()))
        self.assertEqual(stats[1].activity_type, "quiz_retry")
        self.assertGreater(day_of(stats[1].scheduled_start), day_of(stats[0].scheduled_start))
        # High score avoids unnecessary next-day repetition; its spaced review comes days later.
        pbi = self.sessions["pbi"]
        self.assertGreaterEqual(day_of(pbi[0].scheduled_start), MONDAY.date() + timedelta(days=4))
        self.assertEqual(pbi[0].reason.code, "review_due")
        self.assert_final_retrieval_before_deadlines(
            {"mkt": self.MARKETING, "stats": self.STATISTICS, "pbi": self.POWERBI},
            {"mkt": "quiz", "stats": "final_review", "pbi": "final_review"},
        )

    def test_golden_scenario_all_new_material(self):
        for document_id, title, deadline in (("mkt", "Marketing", self.MARKETING), ("stats", "Statistics", self.STATISTICS),
                                             ("pbi", "PowerBI", self.POWERBI)):
            self.add_material(document_id, title, deadline)
        result = self.schedule()

        self.assertEqual(result.capacity.status, "on_track")
        self.assertLess(result.capacity.scheduled_minutes, result.capacity.available_minutes / 3)
        self.assertEqual(self.sessions["mkt"][0].scheduled_start[:10], MONDAY.date().isoformat())
        thursday = date(2026, 10, 1)
        self.assertGreater(self.minutes_before("mkt", thursday), self.minutes_before("pbi", thursday))
        # Statistics starts early enough to spread over several days before its final review.
        stats_days = {day_of(p.scheduled_start) for p in self.sessions["stats"]}
        self.assertLessEqual(min(stats_days), date(2026, 10, 1))
        self.assertGreaterEqual(len(stats_days), 2)
        # PowerBI gets light early attention within the first week, capacity permitting.
        self.assertLess(day_of(self.sessions["pbi"][0].scheduled_start), MONDAY.date() + timedelta(days=7))
        # Quiz never on the same day as that document's new summary.
        for sessions in self.sessions.values():
            summary_day = day_of(next(p for p in sessions if p.activity_type == "summary").scheduled_start)
            quiz_day = day_of(next(p for p in sessions if p.activity_type == "quiz").scheduled_start)
            self.assertGreater(quiz_day, summary_day)
        # Interleaved: documents' study periods overlap rather than running strictly one after another.
        spans = {doc: (day_of(s[0].scheduled_start), day_of(s[-1].scheduled_start)) for doc, s in self.sessions.items()}
        self.assertLessEqual(spans["pbi"][0], spans["stats"][1])
        self.assertLessEqual(spans["stats"][0], spans["mkt"][1] + timedelta(days=2))
        self.assert_final_retrieval_before_deadlines(
            {"mkt": self.MARKETING, "stats": self.STATISTICS, "pbi": self.POWERBI},
            {"mkt": "quiz", "stats": "quiz", "pbi": "final_review"},
        )


if __name__ == "__main__":
    unittest.main()
