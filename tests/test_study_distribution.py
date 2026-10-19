"""Study Planner session distribution: sessions spread over the learner's available days instead of
filling the earliest free slots (study_scheduler._pace_days / _spread_allows), with hard deadlines,
soft target dates, Google Calendar busy time and accepted sessions respected.

Each scenario is compared with the earliest-first allocation (SchedulerConfig(spread_sessions=False)),
which is what the planner did before. "Now" is Mon 2026-09-28 08:00.
"""

import unittest
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

from test_study_scheduler import (
    DEFAULT_CONFIG, MONDAY, ScheduleAssertions, context, day_of, make_state, material, weekly,
)

from backend.study_scheduler import plan_schedule

PACKED = replace(DEFAULT_CONFIG, spread_sessions=False)
EVENINGS = weekly(*((d, "19:00", "21:00") for d in range(7)))          # 2h every evening
WEEKDAYS = weekly(*((d, "19:00", "21:00") for d in range(5)))          # Mon-Fri evenings


def goal(state, deadline=None, target=None):
    item = material(state, deadline)
    item.material["target_date"] = target
    return item


def google_busy(*days, start="19:00", end="21:00"):
    """Google Calendar busy rows as google_calendar_sync.planner_availability adds them."""
    return tuple({"busy": True, "date": day, "start_at": start, "end_at": end, "is_recurring": False,
                  "day_of_week": None} for day in days)


def days_used(result, document_id=None):
    return sorted({day_of(p.scheduled_start) for p in result.proposals
                   if document_id is None or p.document_id == document_id})


def per_document_day(result):
    return Counter((p.document_id, p.scheduled_start[:10]) for p in result.proposals)


class DistributionTests(ScheduleAssertions, unittest.TestCase):
    def schedule(self, materials, availability=EVENINGS, busy=()):
        ctx = context(materials, availability, busy=busy)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        return result, plan_schedule(ctx, PACKED)

    # -- without deadlines -------------------------------------------------------------------

    def test_flexible_materials_spread_over_the_week_instead_of_the_first_days(self):
        materials = [goal(make_state(doc)) for doc in ("mkt", "stats", "pbi")]
        spread, packed = self.schedule(materials)
        self.assertEqual(len(days_used(packed)), 6)              # earliest-first: Mon-Sat, up to 3 a day
        self.assertGreaterEqual(len(days_used(spread)), 7)       # spread: every evening of the week
        self.assertEqual(spread.capacity.scheduled_minutes, packed.capacity.scheduled_minutes)  # same work
        self.assertEqual(spread.capacity.status, "on_track")
        self.assertLessEqual(max(Counter(p.scheduled_start[:10] for p in spread.proposals).values()), 2)
        self.assertEqual(max(per_document_day(spread).values()), 1)   # one session per material per day
        for doc in ("mkt", "stats", "pbi"):
            self.assertEqual(len(days_used(spread, doc)), 4, doc)   # summary, flashcards, quiz, review

    def test_learning_order_and_spacing_rules_still_hold(self):
        spread, _ = self.schedule([goal(make_state(doc)) for doc in ("mkt", "stats", "pbi")])
        sessions = defaultdict(list)
        for p in spread.proposals:
            sessions[p.document_id].append(p)
        for doc, items in sessions.items():
            self.assertEqual([p.activity_type for p in items], ["summary", "flashcards", "quiz", "review"], doc)
            days = [day_of(p.scheduled_start) for p in items]
            self.assertEqual(days, sorted(set(days)), doc)       # each on a later day than the one before

    # -- with deadlines ------------------------------------------------------------------------

    def test_a_far_deadline_is_paced_instead_of_crammed_into_the_first_days(self):
        spread, packed = self.schedule([goal(make_state("mkt"), "2026-10-08")])
        self.assertEqual(days_used(packed), [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 6)])
        learn = [day_of(p.scheduled_start) for p in spread.proposals if p.reason.code != "final_review"]
        self.assertEqual(learn[0], MONDAY.date())                 # starts right away
        self.assertEqual(len(set(learn)), 3)
        self.assertGreaterEqual((learn[-1] - learn[0]).days, 4)   # ...and keeps going over the week
        final = spread.proposals[-1]
        self.assertEqual(final.reason.code, "final_review")
        self.assertLess(learn[-1], day_of(final.scheduled_start))
        self.assertLess(day_of(final.scheduled_start), date(2026, 10, 8))

    def test_never_after_a_hard_deadline(self):
        for availability in (EVENINGS, WEEKDAYS, weekly((0, "18:00", "22:00"), (3, "18:00", "22:00"))):
            materials = [goal(make_state("a"), "2026-10-02"), goal(make_state("b"), "2026-10-06"),
                         goal(make_state("c", cards=20, percentage=45.0), "2026-10-09"), goal(make_state("d"))]
            with self.subTest(availability=len(availability)):
                spread, _ = self.schedule(materials, availability)
                deadlines = {"a": date(2026, 10, 2), "b": date(2026, 10, 6), "c": date(2026, 10, 9)}
                for p in spread.proposals:
                    if p.document_id in deadlines:
                        self.assertLess(day_of(p.scheduled_start), deadlines[p.document_id], p)

    def test_nearer_deadline_comes_first_without_clustering_at_the_deadline(self):
        spread, _ = self.schedule([goal(make_state("near"), "2026-10-05"), goal(make_state("far"), "2026-10-16")])
        self.assertEqual(spread.proposals[0].document_id, "near")
        near, far = days_used(spread, "near"), days_used(spread, "far")
        self.assertEqual(near[0], MONDAY.date())
        self.assertLess(near[-1], far[-1])
        # near: 4 sessions on 4 different days, not bunched into the last days before Oct 5
        self.assertEqual(len(near), 4)
        self.assertLessEqual(sum(d >= date(2026, 10, 3) for d in near), 1)
        self.assertGreaterEqual((far[-1] - far[0]).days, 7)        # far is paced over its longer runway

    def test_final_review_moves_to_a_free_day_when_the_last_days_have_no_time(self):
        # Deadline Mon Oct 5; the two days before it are a weekend without availability.
        spread, packed = self.schedule([goal(make_state("mkt"), "2026-10-05")], WEEKDAYS)
        self.assertEqual(spread.capacity.status, "on_track")
        final = next(p for p in spread.proposals if p.reason.code == "final_review")
        self.assertEqual(day_of(final.scheduled_start), date(2026, 10, 2))   # the Friday before
        self.assertEqual(packed.capacity.status, "on_track")                 # the old allocation too now

    # -- soft target date -------------------------------------------------------------------------

    def test_target_date_pulls_a_flexible_material_forward(self):
        with_target, _ = self.schedule([goal(make_state("mkt"), target="2026-10-01"), goal(make_state("stats"))])
        without, _ = self.schedule([goal(make_state("mkt")), goal(make_state("stats"))])
        def learned_by(result):   # the last learning step (the spaced review after the quiz follows it)
            return max(day_of(p.scheduled_start) for p in result.proposals
                       if p.document_id == "mkt" and p.activity_type != "review")
        self.assertLessEqual(learned_by(with_target), date(2026, 10, 1))
        self.assertGreater(learned_by(without), date(2026, 10, 1))
        self.assertEqual(len(days_used(with_target, "mkt")), 4)   # still on different days

    def test_target_date_with_a_deadline_finishes_learning_by_the_target(self):
        spread, _ = self.schedule([goal(make_state("mkt"), "2026-10-12", target="2026-10-04")])
        learn = [day_of(p.scheduled_start) for p in spread.proposals if p.reason.code != "final_review"]
        self.assertLessEqual(learn[-1], date(2026, 10, 4))
        self.assertEqual(len(set(learn)), len(learn))
        final = spread.proposals[-1]
        self.assertEqual((final.reason.code, day_of(final.scheduled_start) < date(2026, 10, 12)), ("final_review", True))

    def test_target_date_is_soft(self):
        # A target that cannot be met (and one already past) never causes a shortfall.
        tight, _ = self.schedule([goal(make_state("mkt"), target="2026-09-28")], weekly((0, "19:00", "20:00"), (2, "19:00", "21:00")))
        self.assertEqual(tight.capacity.status, "on_track")
        self.assertGreater(days_used(tight, "mkt")[-1], date(2026, 9, 28))
        past, _ = self.schedule([goal(make_state("mkt"), target="2026-09-01")])
        plain, _ = self.schedule([goal(make_state("mkt"))])
        self.assertEqual(past, plain)

    # -- availability, Google Calendar and accepted sessions --------------------------------------

    def test_only_available_days_are_used_and_spread_across(self):
        mwf = weekly(*((d, "18:00", "21:00") for d in (0, 2, 4)))
        spread, packed = self.schedule([goal(make_state(doc)) for doc in ("mkt", "stats")], mwf)
        self.assertTrue(all(d.weekday() in (0, 2, 4) for d in days_used(spread)))
        self.assertGreater(len(days_used(spread)), len(days_used(packed)))
        self.assertEqual(max(per_document_day(spread).values()), 1)

    def test_google_calendar_busy_days_are_skipped_and_the_rest_stays_spread(self):
        busy = google_busy("2026-09-28", "2026-09-29")
        materials = [goal(make_state(doc)) for doc in ("mkt", "stats", "pbi")]
        spread, packed = self.schedule(materials, EVENINGS + busy)
        used = days_used(spread)
        self.assertNotIn(date(2026, 9, 28), used)
        self.assertNotIn(date(2026, 9, 29), used)
        self.assertGreater(len(used), len(days_used(packed)))
        self.assertEqual(spread.capacity.status, "on_track")
        # A partly busy evening keeps its free part: nothing overlaps 19:30-20:30 on Wednesday.
        partial, _ = self.schedule(materials, EVENINGS + google_busy("2026-09-30", start="19:30", end="20:30"))
        for p in partial.proposals:
            if p.scheduled_start[:10] == "2026-09-30":
                self.assertFalse(p.scheduled_start[11:16] < "20:30" and p.scheduled_end[11:16] > "19:30", p)

    def test_accepted_sessions_are_kept_and_worked_around(self):
        accepted = [{"scheduled_start": f"2026-09-{d}T19:00:00", "scheduled_end": f"2026-09-{d}T20:30:00", "status": "scheduled"}
                    for d in (28, 29, 30)]
        spread, _ = self.schedule([goal(make_state("mkt")), goal(make_state("stats"))], busy=accepted)
        # assert_well_formed already proved no overlap with the accepted sessions; they also stay spread.
        self.assertEqual(max(per_document_day(spread).values()), 1)
        self.assertEqual(spread.capacity.status, "on_track")

    # -- not enough time ---------------------------------------------------------------------------

    def test_insufficient_time_is_reported_not_forced(self):
        materials = [goal(make_state("a"), "2026-10-01"), goal(make_state("b"), "2026-10-01")]
        spread, packed = self.schedule(materials, weekly((0, "19:00", "20:00"), (1, "19:00", "20:00")))
        self.assertEqual(spread.capacity.status, "at_risk")
        self.assertGreater(spread.capacity.shortfall_minutes, 0)
        self.assertEqual(sum(c.estimated_minutes for c in spread.capacity.unscheduled), spread.capacity.shortfall_minutes)
        self.assertLessEqual(spread.capacity.shortfall_minutes, packed.capacity.shortfall_minutes)
        for p in spread.proposals:
            self.assertLess(day_of(p.scheduled_start), date(2026, 10, 1))

    def test_spreading_never_schedules_less_than_earliest_first(self):
        scenarios = [
            ([goal(make_state("a"), "2026-09-30"), goal(make_state("b"), "2026-10-02"), goal(make_state("c"))], WEEKDAYS),
            ([goal(make_state(f"d{i}"), "2026-10-04") for i in range(4)], EVENINGS),
            ([goal(make_state("a", cards=20, percentage=40.0), "2026-10-03"), goal(make_state("b"), target="2026-09-30")],
             weekly((1, "19:00", "20:00"), (3, "19:00", "20:00"))),
        ]
        for materials, availability in scenarios:
            with self.subTest(materials=[m.document_id for m in materials]):
                spread, packed = self.schedule(materials, availability)
                self.assertLessEqual(spread.capacity.shortfall_minutes, packed.capacity.shortfall_minutes)

    # -- recall practice and quiz follow-ups are paced too (P1.1) ----------------------------------
    # Before, only new-material steps were paced: recall practice or a low-score follow-up landed on
    # the first free day(s), so a material with a summary and flashcards (an uploaded PDF) was done
    # in 2 consecutive days and several such materials all started on day one.

    def gaps(self, result, document_id):
        days = days_used(result, document_id)
        return [(b - a).days for a, b in zip(days, days[1:])]

    def test_one_flexible_document_is_spaced_over_the_week(self):
        spread, _ = self.schedule([goal(make_state("mkt", summary=True, cards=10))])   # before: one lone quiz
        self.assertEqual(days_used(spread, "mkt"), [date(2026, 9, 28), date(2026, 9, 30), date(2026, 10, 2),
                                                    date(2026, 10, 4)])
        self.assertEqual([p.activity_type for p in spread.proposals], ["summary", "flashcards", "quiz", "review"])
        plain, _ = self.schedule([goal(make_state("mkt"))])           # summary -> flashcards -> quiz -> review
        self.assertTrue(all(gap >= 2 for gap in self.gaps(plain, "mkt")))

    def test_three_flexible_documents_start_on_different_days_and_stay_spaced(self):
        docs = ("mkt", "stats", "pbi")
        spread, _ = self.schedule([goal(make_state(doc, summary=True, cards=10)) for doc in docs])
        firsts = [days_used(spread, doc)[0] for doc in docs]
        self.assertEqual(min(firsts), MONDAY.date())
        self.assertGreater(max(firsts), MONDAY.date())                # staggered, not all on Monday
        self.assertLessEqual(max(firsts), MONDAY.date() + timedelta(days=2))
        for doc in docs:
            self.assertEqual(len(days_used(spread, doc)), 4, doc)     # one session per day per material
            self.assertGreaterEqual(sum(self.gaps(spread, doc)), 5, doc)   # spread over the week
        self.assertLessEqual(max(Counter(p.scheduled_start[:10] for p in spread.proposals).values()), 2)
        self.assertGreaterEqual(len(days_used(spread)), 7)

    def test_low_score_follow_ups_are_spaced_but_the_review_stays_due(self):
        spread, _ = self.schedule([goal(make_state("mkt", cards=10, percentage=40.0))])   # before: Mon, Tue, Wed
        self.assertEqual(days_used(spread, "mkt")[0], MONDAY.date())  # review on its due date
        self.assertTrue(all(gap >= 2 for gap in self.gaps(spread, "mkt")))
        self.assertEqual([p.activity_type for p in spread.proposals], ["review", "flashcards", "quiz_retry"])

    def test_documents_with_different_deadlines_all_start_early_and_finish_in_time(self):
        deadlines = {"a": date(2026, 10, 3), "b": date(2026, 10, 9), "c": date(2026, 10, 14)}
        materials = [goal(make_state("a"), "2026-10-03"), goal(make_state("b", cards=10), "2026-10-09"),
                     goal(make_state("c", summary=True, cards=10), "2026-10-14")]
        spread, _ = self.schedule(materials)
        self.assertEqual(spread.capacity.status, "on_track")
        for doc, deadline in deadlines.items():
            used = days_used(spread, doc)
            self.assertLessEqual(used[0], MONDAY.date() + timedelta(days=2), doc)   # nobody starved
            self.assertLess(used[-1], deadline, doc)
        self.assertEqual(max(per_document_day(spread).values()), 1)
        self.assertGreaterEqual(self.gaps(spread, "c")[0], 3)         # a far deadline is paced, not crammed

    def test_limited_available_days_avoid_same_day_doubles(self):
        tue_fri = weekly((1, "19:00", "21:00"), (4, "19:00", "21:00"))
        spread, packed = self.schedule([goal(make_state(doc, cards=10)) for doc in ("mkt", "stats")], tue_fri)
        # before: summary + recall practice on the same Tuesday evening
        self.assertEqual(max(per_document_day(spread).values()), 1)
        self.assertTrue(all(d.weekday() in (1, 4) for d in days_used(spread)))
        self.assertEqual(spread.capacity.scheduled_minutes, packed.capacity.scheduled_minutes)

    def test_accepted_sessions_stay_put_and_new_ones_spread_around_them(self):
        accepted = [{"scheduled_start": f"2026-09-{d}T19:00:00", "scheduled_end": f"2026-09-{d}T19:45:00",
                     "status": "scheduled"} for d in (28, 30)]
        materials = [goal(make_state(doc, summary=True, cards=10)) for doc in ("mkt", "stats")]
        ctx = context(materials, EVENINGS, busy=accepted)
        spread, _ = self.schedule(materials, busy=accepted)
        self.assertEqual(ctx.busy_sessions, tuple(accepted))          # inputs only, never rescheduled
        for doc in ("mkt", "stats"):
            self.assertEqual(len(days_used(spread, doc)), 4, doc)     # four sessions on four days
            self.assertGreaterEqual(sum(self.gaps(spread, doc)), 5, doc)
        self.assertEqual(spread.capacity.status, "on_track")

    def test_late_night_availability_until_midnight(self):
        late = weekly(*((d, "22:00", "24:00") for d in range(7)))
        ctx = context([goal(make_state(doc, cards=10)) for doc in ("mkt", "stats")], late,
                      now=MONDAY.replace(hour=22, minute=30))
        spread = plan_schedule(ctx)
        self.assert_well_formed(spread, ctx)
        for p in spread.proposals:
            self.assertGreaterEqual(p.scheduled_start[11:16], "22:30" if p.scheduled_start[:10] == "2026-09-28" else "22:00")
            self.assertTrue(p.scheduled_end[11:16] <= "23:59" or p.scheduled_end[11:] == "00:00:00", p)
        self.assertEqual(days_used(spread)[0], MONDAY.date())         # tonight's free 90 minutes still count
        self.assertEqual(max(per_document_day(spread).values()), 1)
        for doc in ("mkt", "stats"):
            self.assertGreaterEqual(sum(self.gaps(spread, doc)), 5, doc)

    def test_finish_early_and_deadline_priority_are_unchanged(self):
        from backend.study_preferences import DEFAULTS, config_from
        materials = [goal(make_state("mkt", summary=True, cards=10), "2026-10-09"),
                     goal(make_state("stats", summary=True, cards=10))]
        ctx = context(materials, EVENINGS)
        early = plan_schedule(ctx, config_from({**DEFAULTS, "strategy": "finish_early"}))
        self.assertEqual(early, plan_schedule(ctx, replace(config_from(DEFAULTS), spread_sessions=False)))
        self.assertEqual(self.gaps(early, "mkt")[0], 1)               # as soon as possible
        priority = plan_schedule(ctx, config_from({**DEFAULTS, "strategy": "deadline_priority"}))
        self.assertEqual(days_used(priority, "mkt")[:2], [date(2026, 9, 28), date(2026, 9, 29)])   # deadlined: early
        self.assertGreaterEqual(sum(self.gaps(priority, "stats")), 5)                           # flexible: balanced

    def test_deterministic(self):
        materials = [goal(make_state("a"), "2026-10-06", target="2026-10-03"), goal(make_state("b"))]
        ctx = context(materials, EVENINGS, busy=())
        self.assertEqual(plan_schedule(ctx), plan_schedule(ctx))


if __name__ == "__main__":
    unittest.main()
