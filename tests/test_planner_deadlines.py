"""Study Planner deadlines and feasibility (P1.3): hard deadlines are never passed, an urgent
material keeps its last usable day, sessions stay spread before a deadline when the time allows,
and what cannot fit is reported per material (study_scheduler.deadline_risks) instead of being
dropped silently. "Now" is Mon 2026-09-28 08:00.
"""

import unittest
from datetime import date

from test_study_scheduler import MONDAY, ScheduleAssertions, context, day_of, make_state, material, weekly

from backend.study_preferences import DEFAULTS, config_from
from backend.study_scheduler import deadline_risks, plan_schedule

EVENINGS = weekly(*((d, "19:00", "21:00") for d in range(7)))
ONE_HOUR = weekly(*((d, "19:00", "20:00") for d in range(7)))
WEEKDAYS = weekly(*((d, "19:00", "21:00") for d in range(5)))
STRATEGIES = ("balanced", "finish_early", "deadline_priority")


def days_of(result, document_id):
    return [day_of(p.scheduled_start) for p in result.proposals if p.document_id == document_id]


class DeadlineTests(ScheduleAssertions, unittest.TestCase):
    def schedule(self, materials, availability=EVENINGS, busy=(), config=None):
        ctx = context(materials, availability, busy=busy)
        result = plan_schedule(ctx, config) if config else plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        return result

    # -- near deadlines ----------------------------------------------------------------------------

    def test_near_deadline_fits_everything_before_it(self):
        result = self.schedule([material(make_state("a"), "2026-10-01")])
        self.assertEqual(result.capacity.status, "on_track")
        self.assertEqual(deadline_risks(result), [])
        self.assertEqual([p.activity_type for p in result.proposals], ["summary", "flashcards", "quiz", "review"])
        self.assertEqual(result.proposals[-1].reason.code, "final_review")
        self.assertTrue(all(d < date(2026, 10, 1) for d in days_of(result, "a")))

    def test_deadline_today_is_reported_not_forced(self):
        result = self.schedule([material(make_state("a"), "2026-09-28")])
        self.assertTrue(all(d <= date(2026, 9, 28) for d in days_of(result, "a")))
        [risk] = deadline_risks(result)
        self.assertEqual((risk["document_id"], risk["deadline"], risk["final_review_missing"]), ("a", "2026-09-28", True))
        self.assertEqual(risk["shortfall_minutes"], result.capacity.shortfall_minutes)
        self.assertEqual(risk["unscheduled_count"], len(result.capacity.unscheduled))

    def test_exactly_enough_days_keeps_one_session_a_day(self):
        # Very weak score, deadline Mon Oct 5, weekday evenings only: 5 sessions, 5 usable days.
        # Before: two of them were crammed onto Monday although Tue-Fri had room.
        result = self.schedule([material(make_state("a", cards=10, percentage=17.0), "2026-10-05")], WEEKDAYS)
        self.assertEqual(result.capacity.status, "on_track")
        days = days_of(result, "a")
        self.assertEqual(days, sorted(set(days)))
        self.assertEqual(days[-1], date(2026, 10, 2))        # final review on the Friday before

    # -- multiple deadlines ------------------------------------------------------------------------

    def test_multiple_deadlines_urgent_first_later_ones_not_starved_or_crammed(self):
        deadlines = {"a": date(2026, 10, 2), "b": date(2026, 10, 4), "c": date(2026, 10, 12)}
        result = self.schedule([material(make_state(doc), d.isoformat()) for doc, d in deadlines.items()])
        self.assertEqual(result.capacity.status, "on_track")
        self.assertEqual(result.proposals[0].document_id, "a")
        for doc, deadline in deadlines.items():
            days = days_of(result, doc)
            self.assertEqual(len(days), 4, doc)                         # 3 learning steps + final review
            self.assertLess(days[-1], deadline, doc)
            self.assertLessEqual((days[0] - MONDAY.date()).days, 4, doc)   # starts within the first days
        c = days_of(result, "c")
        self.assertGreaterEqual((c[-2] - c[0]).days, 4)                  # spread, not bunched before Oct 12
        self.assertLessEqual(sum(d >= date(2026, 10, 9) for d in c), 1)  # only the final review near the end

    # -- insufficient capacity --------------------------------------------------------------------

    def test_urgent_materials_keep_their_last_day(self):
        # One hour a day; a and b are due Thu Oct 1 (last study day Wed), c a day later.
        # Before: c took Wednesday, so a and b were left with a summary each.
        materials = [material(make_state("a"), "2026-10-01"), material(make_state("b"), "2026-10-01"),
                     material(make_state("c"), "2026-10-02")]
        result = self.schedule(materials, ONE_HOUR)
        self.assertEqual(result.capacity.status, "at_risk")
        self.assertEqual([p.activity_type for p in result.proposals if p.document_id in ("a", "b")],
                         ["summary", "summary", "flashcards", "flashcards"])
        self.assertEqual(days_of(result, "c"), [date(2026, 10, 1)])     # c still gets its first session
        risks = deadline_risks(result)
        self.assertEqual([r["document_id"] for r in risks], ["a", "b", "c"])   # nearest deadline first
        self.assertEqual(sum(r["shortfall_minutes"] for r in risks), result.capacity.shortfall_minutes)
        self.assertTrue(all(r["final_review_missing"] for r in risks))
        self.assertEqual({r["document_id"]: r["scheduled_count"] for r in risks}, {"a": 2, "b": 2, "c": 1})

    def test_sparse_days_go_to_the_material_that_cannot_wait(self):
        # Mon and Fri, one hour each; a is due Mon Oct 5 (Fri is its last day), b Tue Oct 6.
        materials = [material(make_state("a"), "2026-10-05"), material(make_state("b"), "2026-10-06")]
        result = self.schedule(materials, weekly((0, "19:00", "20:00"), (4, "19:00", "20:00")))
        self.assertEqual(days_of(result, "a"), [date(2026, 9, 28), date(2026, 10, 2)])
        self.assertEqual(days_of(result, "b"), [date(2026, 10, 5)])
        self.assertEqual({r["document_id"] for r in deadline_risks(result)}, {"a", "b"})

    def test_nothing_is_dropped_silently(self):
        materials = [material(make_state("a"), "2026-09-30"), material(make_state("b", percentage=20.0), "2026-10-01"),
                     material(make_state("c"))]
        result = self.schedule(materials, weekly((0, "19:00", "19:45"), (1, "19:00", "19:45")))
        risks = deadline_risks(result)
        self.assertEqual(sum(r["unscheduled_count"] for r in risks), len(result.capacity.unscheduled))
        self.assertEqual(result.capacity.required_minutes,
                         result.capacity.scheduled_minutes + sum(r["shortfall_minutes"] for r in risks))

    # -- accepted sessions, Google Calendar, preferences ------------------------------------------

    def test_accepted_sessions_and_google_busy_time_are_kept(self):
        accepted = [{"scheduled_start": f"2026-09-{d}T19:00:00", "scheduled_end": f"2026-09-{d}T20:20:00",
                     "status": "scheduled"} for d in (28, 29, 30)]
        google = ({"busy": True, "date": "2026-10-01", "start_at": "19:00", "end_at": "20:00", "is_recurring": False,
                   "day_of_week": None},)
        materials = [material(make_state("a"), "2026-10-02"), material(make_state("b"), "2026-10-06")]
        result = self.schedule(materials, EVENINGS + google, busy=accepted)   # no overlap: assert_well_formed
        for p in result.proposals:
            if p.scheduled_start[:10] == "2026-10-01":
                self.assertGreaterEqual(p.scheduled_start[11:16], "20:00", p)
        self.assertTrue(all(d < date(2026, 10, 2) for d in days_of(result, "a")))
        self.assertTrue(all(d < date(2026, 10, 6) for d in days_of(result, "b")))
        self.assertEqual({r["document_id"] for r in deadline_risks(result)},
                         {c.document_id for c in result.capacity.unscheduled})

    def test_never_after_a_hard_deadline_under_any_strategy(self):
        scenarios = [
            ([material(make_state("a"), "2026-09-29"), material(make_state("b", cards=10, percentage=17.0), "2026-10-02"),
              material(make_state("c"), "2026-10-03"), material(make_state("d"))], ONE_HOUR),
            ([material(make_state("a", summary=True, cards=10), "2026-10-05"), material(make_state("b"), "2026-10-05")],
             WEEKDAYS),
            ([material(make_state(f"d{i}"), "2026-10-04") for i in range(4)], EVENINGS),
        ]
        for strategy in STRATEGIES:
            config = config_from({**DEFAULTS, "strategy": strategy, "break_minutes": 10})
            for materials, availability in scenarios:
                with self.subTest(strategy=strategy, docs=[m.document_id for m in materials]):
                    result = plan_schedule(context(materials, availability), config)
                    for p in result.proposals:
                        deadline = next(m.deadline for m in materials if m.document_id == p.document_id)
                        if deadline:
                            self.assertLess(day_of(p.scheduled_start), date.fromisoformat(deadline), p)
                    self.assertEqual(sum(r["shortfall_minutes"] for r in deadline_risks(result)),
                                     result.capacity.shortfall_minutes)

    def test_deadline_priority_still_front_loads_deadlined_materials(self):
        materials = [material(make_state("a"), "2026-10-09"), material(make_state("b"))]
        result = plan_schedule(context(materials, EVENINGS), config_from({**DEFAULTS, "strategy": "deadline_priority"}))
        # As soon as possible (unchanged): learning done Mon-Tue (summary + flashcards in one combined
        # session on Monday), final review just before Oct 9.
        self.assertEqual(days_of(result, "a"), [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 7)])


if __name__ == "__main__":
    unittest.main()
