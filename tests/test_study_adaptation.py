"""Study Planner Phase 5A: deterministic adaptive replanning PROPOSALS (no DB writes).

Pure engine tests: learner-local now is Thu 2026-09-24 12:00 (UTC offset 0). Unless a test says
otherwise the learner is available every day 18:00-21:00."""

import random
import unittest
from datetime import datetime, timedelta

import test_study_scheduler as fx   # make_state / material / weekly fixtures (module import: no test re-collection)

from backend.study_adaptation import AdaptationConfig, AdaptationContext, AdaptationTrigger, propose_adaptation

NOW = datetime(2026, 9, 24, 12, 0)
EVENINGS = fx.weekly(*((day, "18:00", "21:00") for day in range(7)))
QUIZ_THIS_MORNING = "2026-09-24T10:00:00+00:00"


def session(session_id, document_id, activity, start, minutes, status="scheduled", reason="new_material", **extra):
    begin = datetime.fromisoformat(start)
    return {"session_id": session_id, "document_id": document_id, "activity_type": activity,
            "scheduled_start": begin.isoformat(), "scheduled_end": (begin + timedelta(minutes=minutes)).isoformat(),
            "duration_minutes": minutes, "status": status, "reason": reason, "artifact_id": None, **extra}


def busy_of(sessions):
    """What list_busy_sessions would return (active plan): in_progress/completed/scheduled from today."""
    return tuple(s for s in sessions if s["status"] in ("scheduled", "in_progress", "completed")
                 and s["scheduled_start"][:10] >= NOW.date().isoformat())


def propose(trigger, materials, sessions, availability=EVENINGS, other_busy=(), now=NOW, config=None):
    context = AdaptationContext(trigger=trigger, now=now, utc_offset=timedelta(0), materials=tuple(materials),
                                plan_sessions=tuple(sessions), availability=tuple(availability),
                                busy_sessions=busy_of(sessions) + tuple(other_busy))
    return propose_adaptation(context, config) if config else propose_adaptation(context)


def quiz_done(document_id):
    return AdaptationTrigger("quiz_completed", document_id=document_id)


class AdaptationAssertions:
    def assert_rules(self, result, sessions, materials, other_busy=()):
        """Invariants: proposals never touch history, never overlap, stay in availability/capacity
        and before deadlines."""
        history = {s["session_id"] for s in sessions if s["status"] != "scheduled" or s["scheduled_start"] < NOW.isoformat()}
        touched = {m.session_id for m in result.moved} | {c.session_id for c in result.cancelled}
        self.assertFalse(touched & history, "history must never change")
        cancelled = {c.session_id for c in result.cancelled}
        moved = {m.session_id: m for m in result.moved}
        final = [(s["document_id"], s["activity_type"], moved[s["session_id"]].to_start, moved[s["session_id"]].to_end)
                 if s["session_id"] in moved else (s["document_id"], s["activity_type"], s["scheduled_start"], s["scheduled_end"])
                 for s in list(busy_of(sessions)) + list(other_busy)
                 if s["session_id"] not in cancelled and not (s["status"] == "scheduled" and s["scheduled_end"] <= NOW.isoformat())]
        final += [(a.document_id, a.activity_type, a.scheduled_start, a.scheduled_end) for a in result.added]
        final.sort(key=lambda row: row[2])
        for before, after in zip(final, final[1:]):
            self.assertLessEqual(before[3], after[2], f"overlap: {before} / {after}")
        deadlines = {m.document_id: m.deadline for m in materials}
        by_day = {}
        for document_id, activity, start, end in final:
            by_day[start[:10]] = by_day.get(start[:10], 0) + (datetime.fromisoformat(end) - datetime.fromisoformat(start)).seconds // 60
        for item in list(result.added) + list(result.moved):
            start = getattr(item, "scheduled_start", None) or item.to_start
            self.assertGreaterEqual(start, NOW.isoformat())
            if deadlines.get(item.document_id):
                self.assertLess(start[:10], deadlines[item.document_id], "never on/after the deadline")
            self.assertLessEqual(by_day[start[:10]], 90, "daily comfortable capacity")
        self.assertIn(result.significance, ("small", "large"))
        self.assertTrue(result.reasons)


class QuizTriggerTests(AdaptationAssertions, unittest.TestCase):
    def test_low_score_pulls_practice_earlier_and_adds_one_retry(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=40,
                                        completed_at=QUIZ_THIS_MORNING))
        sessions = [session("done", "mkt", "quiz", "2026-09-24T10:00:00", 30, status="completed"),
                    session("cards", "mkt", "flashcards", "2026-09-28T18:00:00", 10, reason="review_due")]
        result = propose(quiz_done("mkt"), [mkt], sessions)
        self.assertEqual([(m.session_id, m.to_start) for m in result.moved], [("cards", "2026-09-25T18:00:00")])
        self.assertEqual([(a.activity_type, a.scheduled_start, a.reason_code) for a in result.added],
                         [("quiz_retry", "2026-09-26T18:00:00", "low_quiz_score")])
        self.assertEqual(result.added[0].artifact_id, "q-mkt")
        self.assertEqual((result.cancelled, result.significance, result.unchanged_count), ((), "small", 0))
        self.assert_rules(result, sessions, [mkt])

    def test_low_score_keeps_good_sessions_and_cancels_a_duplicate_retry(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=40,
                                        completed_at=QUIZ_THIS_MORNING))
        sessions = [session("cards", "mkt", "flashcards", "2026-09-25T18:00:00", 10, reason="low_quiz_score"),
                    session("retry1", "mkt", "quiz_retry", "2026-09-26T18:00:00", 20, reason="low_quiz_score"),
                    session("retry2", "mkt", "quiz_retry", "2026-09-27T18:00:00", 20, reason="low_quiz_score")]
        result = propose(quiz_done("mkt"), [mkt], sessions)
        self.assertEqual((result.added, result.moved), ((), ()))
        self.assertEqual([c.session_id for c in result.cancelled], ["retry2"])
        self.assertEqual(result.unchanged_count, 2)
        # Applying the proposal and asking again changes nothing more: no duplicate retries.
        applied = [s for s in sessions if s["session_id"] != "retry2"]
        again = propose(quiz_done("mkt"), [mkt], applied)
        self.assertEqual((again.added, again.moved, again.cancelled, again.unchanged_count), ((), (), (), 2))

    def test_high_score_pushes_review_out_and_drops_the_retry(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=90,
                                        completed_at=QUIZ_THIS_MORNING))
        sessions = [session("review", "mkt", "review", "2026-09-25T18:00:00", 10, reason="review_due"),
                    session("retry", "mkt", "quiz_retry", "2026-09-27T18:00:00", 20, reason="review_due")]
        result = propose(quiz_done("mkt"), [mkt], sessions)
        self.assertEqual([(m.session_id, m.to_start) for m in result.moved], [("review", "2026-09-29T18:00:00")])
        self.assertGreaterEqual(result.moved[0].to_start[:10], "2026-09-26")   # never the next day
        self.assertEqual([c.session_id for c in result.cancelled], ["retry"])
        self.assertEqual(result.added, ())
        self.assert_rules(result, sessions, [mkt])

    def test_medium_score_reviews_then_retries_later(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=70,
                                        completed_at=QUIZ_THIS_MORNING))
        result = propose(quiz_done("mkt"), [mkt], [])
        # 70% -> review in 2 days, then a retry 2 days after that review.
        self.assertEqual([(a.activity_type, a.scheduled_start) for a in result.added],
                         [("review", "2026-09-26T18:00:00"), ("quiz_retry", "2026-09-28T18:00:00")])
        self.assertEqual((result.moved, result.cancelled), ((), ()))

    def test_review_never_moves_past_the_deadline(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=90,
                                        completed_at=QUIZ_THIS_MORNING), deadline="2026-09-28")
        sessions = [session("final", "mkt", "review", "2026-09-27T18:00:00", 10, reason="final_review")]
        result = propose(quiz_done("mkt"), [mkt], sessions)
        # The spaced review (due 09-29) is past the deadline: the final review covers it instead.
        self.assertEqual((result.added, result.moved, result.cancelled, result.unchanged_count), ((), (), (), 1))
        self.assert_rules(result, sessions, [mkt])

    def test_other_documents_are_untouched(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=40,
                                        completed_at=QUIZ_THIS_MORNING))
        stats = fx.material(fx.make_state("stats", "Statistics"))
        sessions = [session("s1", "stats", "summary", "2026-09-25T18:00:00", 40),
                    session("s2", "stats", "flashcards", "2026-09-26T18:00:00", 20)]
        result = propose(quiz_done("mkt"), [mkt, stats], sessions)
        self.assertEqual({a.document_id for a in result.added}, {"mkt"})
        self.assertEqual((result.moved, result.cancelled, result.unchanged_count), ((), (), 2))
        self.assert_rules(result, sessions, [mkt, stats])

    def test_no_completed_quiz_is_a_warning(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing"))
        result = propose(quiz_done("mkt"), [mkt], [])
        self.assertEqual([w["code"] for w in result.warnings], ["no_completed_quiz"])
        self.assertEqual(result.change_count, 0)


class MissedAndSkippedTests(AdaptationAssertions, unittest.TestCase):
    def plan(self):
        stats = fx.material(fx.make_state("stats", "Statistics"))
        sessions = [session("missed", "stats", "summary", "2026-09-24T09:00:00", 40),   # end passed: not completed
                    session("cards", "stats", "flashcards", "2026-09-25T18:00:00", 20),
                    session("quiz", "stats", "quiz", "2026-09-26T18:00:00", 30)]
        return stats, sessions

    def test_missed_session_gets_one_replacement_and_nothing_else_moves(self):
        stats, sessions = self.plan()
        result = propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], sessions)
        self.assertEqual([(a.activity_type, a.scheduled_start, a.reason_code, a.replaces_session_id) for a in result.added],
                         [("summary", "2026-09-24T18:00:00", "rescheduled", "missed")])
        self.assertIn("missed", result.added[0].message)
        self.assertEqual((result.moved, result.cancelled, result.unchanged_count, result.significance), ((), (), 2, "small"))
        self.assert_rules(result, sessions, [stats])

    def test_no_duplicate_replacement(self):
        stats, sessions = self.plan()
        sessions[0]["status"] = "rescheduled"
        sessions.append(session("moved", "stats", "summary", "2026-09-24T19:00:00", 40, rescheduled_from="missed"))
        sessions.append(session("missed2", "stats", "summary", "2026-09-23T09:00:00", 40))
        result = propose(AdaptationTrigger("session_missed", session_id="missed2"), [stats], sessions)
        self.assertEqual(result.change_count, 0)
        self.assertTrue(any("already covered" in reason for reason in result.reasons))

    def test_replacement_that_would_land_after_the_next_step_shifts_it(self):
        stats, sessions = self.plan()
        sessions[1]["scheduled_start"], sessions[1]["scheduled_end"] = "2026-09-24T18:00:00", "2026-09-24T18:20:00"
        # Only 90 comfortable minutes a day: today holds flashcards (20) + a 60-minute other-plan session.
        other = (session("other", "pbi", "summary", "2026-09-24T19:30:00", 60, status="completed"),)
        result = propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], sessions, other_busy=other)
        self.assertEqual(result.added[0].scheduled_start[:10], "2026-09-25")
        self.assertEqual([m.session_id for m in result.moved], ["cards"])   # flashcards now follow the summary
        self.assertGreater(result.moved[0].to_start, result.added[0].scheduled_end)
        self.assert_rules(result, sessions, [stats], other)

    def test_no_replacement_when_no_longer_needed(self):
        stats = fx.material(fx.make_state("stats", "Statistics", summary=True))
        sessions = [session("missed", "stats", "summary", "2026-09-24T09:00:00", 40)]
        result = propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], sessions)
        self.assertEqual(result.change_count, 0)
        self.assertIn("no longer needed", result.reasons[0])

    def test_skipped_session_replacement(self):
        stats = fx.material(fx.make_state("stats", "Statistics", summary=True, cards=8))
        sessions = [session("skipped", "stats", "quiz", "2026-09-25T18:00:00", 30, status="skipped")]
        result = propose(AdaptationTrigger("session_skipped", session_id="skipped"), [stats], sessions)
        self.assertEqual([(a.activity_type, a.scheduled_start, a.replaces_session_id) for a in result.added],
                         [("quiz", "2026-09-24T18:00:00", "skipped")])
        self.assertIn("skipped", result.added[0].message)

    def test_no_slot_before_the_deadline_is_a_warning_not_a_late_session(self):
        stats = fx.material(fx.make_state("stats", "Statistics", summary=True, cards=8), deadline="2026-09-25")
        sessions = [session("skipped", "stats", "quiz", "2026-09-24T09:00:00", 30, status="skipped")]
        mondays = fx.weekly((0, "18:00", "21:00"))
        result = propose(AdaptationTrigger("session_skipped", session_id="skipped"), [stats], sessions, availability=mondays)
        self.assertEqual(result.added, ())
        self.assertEqual([w["code"] for w in result.warnings], ["no_slot_before_deadline"])

    def test_trigger_must_match_the_session_state(self):
        stats, sessions = self.plan()
        with self.assertRaises(ValueError):
            propose(AdaptationTrigger("session_missed", session_id="cards"), [stats], sessions)   # still ahead
        with self.assertRaises(ValueError):
            propose(AdaptationTrigger("session_skipped", session_id="missed"), [stats], sessions)
        with self.assertRaises(ValueError):
            propose(AdaptationTrigger("session_missed", session_id="nope"), [stats], sessions)


class AvailabilityAndDeadlineTests(AdaptationAssertions, unittest.TestCase):
    def test_removed_availability_moves_only_the_affected_session_same_day(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing"))
        sessions = [session("sat", "mkt", "summary", "2026-09-26T10:00:00", 40),     # Saturday morning: no longer available
                    session("fri", "mkt", "flashcards", "2026-09-25T18:00:00", 20),
                    session("sun", "mkt", "quiz", "2026-09-27T18:00:00", 30)]
        result = propose(AdaptationTrigger("availability_changed"), [mkt], sessions)
        self.assertEqual([(m.session_id, m.from_start, m.to_start) for m in result.moved],
                         [("sat", "2026-09-26T10:00:00", "2026-09-26T18:00:00")])
        self.assertEqual((result.added, result.cancelled, result.unchanged_count), ((), (), 2))
        self.assert_rules(result, sessions, [mkt])

    def test_no_time_left_before_the_deadline_cancels_with_a_warning(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing"), deadline="2026-09-27")
        sessions = [session("fri", "mkt", "summary", "2026-09-25T10:00:00", 40)]
        mondays = fx.weekly((0, "18:00", "21:00"))
        result = propose(AdaptationTrigger("availability_changed"), [mkt], sessions, availability=mondays)
        self.assertEqual([c.session_id for c in result.cancelled], ["fri"])
        self.assertEqual([w["code"] for w in result.warnings], ["no_slot_before_deadline"])

    def test_nothing_to_do_when_everything_still_fits(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing"))
        sessions = [session("fri", "mkt", "flashcards", "2026-09-25T18:00:00", 20)]
        result = propose(AdaptationTrigger("availability_changed"), [mkt], sessions)
        self.assertEqual((result.change_count, result.unchanged_count, result.significance), (0, 1, "small"))
        self.assertEqual(result.reasons, ("No changes needed: the plan still fits.",))

    def test_earlier_deadline_moves_late_sessions_inside_it(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10), deadline="2026-09-28")
        sessions = [session("quiz", "mkt", "quiz", "2026-09-29T18:00:00", 30),
                    session("final", "mkt", "review", "2026-10-04T18:00:00", 10, reason="final_review")]
        result = propose(AdaptationTrigger("deadline_changed", document_id="mkt"), [mkt], sessions)
        moved = {m.session_id: m.to_start for m in result.moved}
        self.assertEqual(moved, {"quiz": "2026-09-24T18:00:00", "final": "2026-09-26T18:00:00"})
        self.assertEqual(result.significance, "small")
        self.assert_rules(result, sessions, [mkt])

    def test_new_deadline_adds_a_final_review(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10), deadline="2026-09-30")
        sessions = [session("quiz", "mkt", "quiz", "2026-09-25T18:00:00", 30)]
        result = propose(AdaptationTrigger("deadline_changed", document_id="mkt"), [mkt], sessions)
        self.assertEqual([(a.activity_type, a.reason_code, a.scheduled_start) for a in result.added],
                         [("review", "final_review", "2026-09-28T18:00:00")])
        self.assertEqual((result.moved, result.cancelled, result.unchanged_count), ((), (), 1))


class RulesTests(AdaptationAssertions, unittest.TestCase):
    def test_history_is_preserved_and_busy_time_respected(self):
        stats = fx.material(fx.make_state("stats", "Statistics"))
        sessions = [session("missed", "stats", "summary", "2026-09-24T09:00:00", 40),
                    session("running", "stats", "flashcards", "2026-09-24T11:30:00", 5, status="in_progress"),
                    session("done", "pbi", "flashcards", "2026-09-24T18:00:00", 5, status="completed")]
        other = (session("x", "pbi", "quiz", "2026-09-24T18:30:00", 30),)   # another active plan
        result = propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], sessions, other_busy=other)
        # 40 of today's 90 comfortable minutes are used (+45 for the summary); after 18:30-19:00 + the 30-minute heavy gap.
        self.assertEqual(result.added[0].scheduled_start, "2026-09-24T19:30:00")
        # One more minute of load and the summary no longer fits today's budget.
        heavier = [dict(s, duration_minutes=11, scheduled_end="2026-09-24T18:11:00") if s["session_id"] == "done" else s
                   for s in sessions]
        later = propose(AdaptationTrigger("session_missed", session_id="missed"), [stats], heavier, other_busy=other)
        self.assertEqual(later.added[0].scheduled_start[:10], "2026-09-25")
        self.assert_rules(result, sessions, [stats], other)

    def test_significance_is_configurable(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=40,
                                        completed_at=QUIZ_THIS_MORNING))
        sessions = [session("cards", "mkt", "flashcards", "2026-09-28T18:00:00", 10, reason="review_due")]
        self.assertEqual(propose(quiz_done("mkt"), [mkt], sessions).significance, "small")
        strict = AdaptationConfig(small_max_changes=1)
        self.assertEqual(propose(quiz_done("mkt"), [mkt], sessions, config=strict).significance, "large")

    def test_deterministic_regardless_of_input_order(self):
        mkt = fx.material(fx.make_state("mkt", "Marketing", summary=True, cards=10, percentage=40,
                                        completed_at=QUIZ_THIS_MORNING), deadline="2026-10-02")
        stats = fx.material(fx.make_state("stats", "Statistics"))
        sessions = [session(f"s{i}", doc, activity, f"2026-09-{25 + i}T18:00:00", minutes)
                    for i, (doc, activity, minutes) in enumerate([("mkt", "flashcards", 10), ("stats", "summary", 40),
                                                                  ("mkt", "quiz_retry", 20), ("stats", "quiz", 30)])]
        expected = propose(quiz_done("mkt"), [mkt, stats], sessions).to_dict()
        for seed in range(5):
            shuffled = sessions[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(propose(quiz_done("mkt"), [stats, mkt], shuffled).to_dict(), expected)

    def test_unknown_trigger_rejected(self):
        with self.assertRaises(ValueError):
            AdaptationTrigger("flashcards_reviewed", document_id="mkt")
        with self.assertRaises(ValueError):
            AdaptationTrigger("quiz_completed")


if __name__ == "__main__":
    unittest.main()
