"""Study session length rules: every NEW session (scheduler suggestion, adaptation addition, queue
placement) lasts at least 20 minutes; existing sessions are never resized when moved."""

import unittest
from datetime import datetime

import test_study_adaptation as ad   # session / propose fixtures (module import: no test re-collection)
import test_study_scheduler as fx

from backend import study_plan_api_service
from backend.study_adaptation import AdaptationTrigger
from backend.study_scheduler import DEFAULT_CONFIG, estimate_duration
from backend.study_session_duration import (DEFAULT_SESSION_MINUTES, MIN_SESSION_MINUTES, SESSION_DURATION_PRESETS,
                                            SessionDurationError, at_least_minimum, validate_new_session_minutes)


def minutes_between(start, end):
    return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60)


class DurationRuleTests(unittest.TestCase):
    def test_minimum_presets_and_default(self):
        self.assertEqual(MIN_SESSION_MINUTES, 20)
        self.assertEqual(SESSION_DURATION_PRESETS, (20, 30, 40, 50))
        self.assertEqual(DEFAULT_SESSION_MINUTES, 30)
        self.assertTrue(all(preset >= MIN_SESSION_MINUTES for preset in SESSION_DURATION_PRESETS))

    def test_validate_new_session_minutes(self):
        for ok in (20, 30, 45, 50, 90):
            self.assertEqual(validate_new_session_minutes(ok), ok)
        for bad in (0, 10, 15, 19, -20, True, 20.0, "30", None):
            with self.assertRaises(SessionDurationError, msg=repr(bad)):
                validate_new_session_minutes(bad)

    def test_at_least_minimum_only_raises_short_lengths(self):
        self.assertEqual([at_least_minimum(m) for m in (5, 15, 20, 25, 50)], [20, 20, 20, 25, 50])


class GeneratedDurationTests(unittest.TestCase):
    def test_no_scheduler_estimate_is_shorter_than_the_minimum(self):
        states = [fx.make_state("d", chunks=chunks, cards=cards, questions=questions, percentage=percentage)
                  for chunks in (0, 1, 40) for cards in (0, 1, 8, 90) for questions in (1, 10)
                  for percentage in (None, 40.0)]
        for activity in DEFAULT_CONFIG.durations:
            for state in states:
                self.assertGreaterEqual(estimate_duration(activity, state), MIN_SESSION_MINUTES, activity)

    def test_previewed_sessions_are_at_least_the_minimum(self):
        materials = [fx.material(fx.make_state(doc, cards=cards)) for doc, cards in (("a", 2), ("b", 4), ("c", 0))]
        result = fx.plan_schedule(fx.context(materials, fx.weekly(*((day, "08:00", "22:00") for day in range(7)))))
        self.assertTrue(result.proposals)
        for proposal in result.proposals:
            self.assertGreaterEqual(proposal.duration_minutes, MIN_SESSION_MINUTES)
            self.assertEqual(minutes_between(proposal.scheduled_start, proposal.scheduled_end), proposal.duration_minutes)

    def test_replacement_for_a_short_missed_session_is_at_least_the_minimum(self):
        stats = fx.material(fx.make_state("stats", "Statistics"))
        sessions = [ad.session("missed", "stats", "summary", "2026-09-24T09:00:00", 15),   # end passed: not completed
                    ad.session("quiz", "stats", "quiz", "2026-09-26T18:00:00", 30)]
        result = ad.propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], sessions)
        self.assertTrue(result.added)
        for added in result.added:
            self.assertGreaterEqual(added.duration_minutes, MIN_SESSION_MINUTES)
            self.assertEqual(minutes_between(added.scheduled_start, added.scheduled_end), added.duration_minutes)

    def test_an_existing_short_session_keeps_its_length_when_moved(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing"))
        sessions = [ad.session("sat", "mkt", "flashcards", "2026-09-26T10:00:00", 15)]   # no longer available
        result = ad.propose(AdaptationTrigger("availability_changed"), [mkt], sessions)
        self.assertEqual([m.session_id for m in result.moved], ["sat"])
        self.assertEqual(minutes_between(result.moved[0].to_start, result.moved[0].to_end), 15)


class ServiceGuardTests(unittest.TestCase):
    def test_a_short_new_session_is_refused_not_resized(self):
        record = {"activity_type": "review", "duration_minutes": 15}
        with self.assertRaises(study_plan_api_service.PlanValidationError) as raised:
            study_plan_api_service._require_new_session_lengths([{"activity_type": "quiz", "duration_minutes": 30}, record])
        self.assertEqual(raised.exception.payload["code"], "session_too_short")
        self.assertEqual(record["duration_minutes"], 15)

    def test_minimum_length_sessions_pass(self):
        study_plan_api_service._require_new_session_lengths(
            [{"activity_type": "quiz", "duration_minutes": minutes} for minutes in SESSION_DURATION_PRESETS])


if __name__ == "__main__":
    unittest.main()
