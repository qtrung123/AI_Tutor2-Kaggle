"""Study Planner adaptive learning from quiz progress (Task 4).

Learning states, from persisted data only:
- no quiz yet               -> learn first (summary / flashcards / recall), then create a first quiz;
- quiz created, not taken   -> the initial quiz;
- quiz < 60%                -> weak-topic review, practice, quiz retry;
- quiz 60-79%               -> review of the incorrect topics, then reassessment;
- quiz >= 80%               -> lighter spaced repetition.
Weak topics come only from persisted topic mastery; missing resources become explicit preparation
steps; adaptation proposals are balanced like the initial plan. "Now" is Mon 2026-09-28 08:00
(scheduler) / Thu 2026-09-24 12:00 (adaptation engine).
"""

import unittest
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin
from test_study_scheduler import (
    DEFAULT_CONFIG, MONDAY, ScheduleAssertions, context, day_of, make_state, material, weekly,
)

from backend import quiz_store, study_plan_api_service, study_planner_store
from backend.document_study_state import QuizState, WeakTopic, derive_learning_state, get_document_study_state
from backend.main import app
from backend.mastery_service import calculate_mastery
from backend.study_adaptation import AdaptationContext, AdaptationTrigger, propose_adaptation
from backend.study_scheduler import plan_schedule, select_candidates

EVENINGS = weekly(*((d, "19:00", "21:00") for d in range(7)))
ADAPT_EVENINGS = weekly(*((d, "18:00", "21:00") for d in range(7)))


def with_quiz_created(state, quiz_id="q-new", questions=10):
    """A document whose quiz exists but was never attempted (no answers)."""
    quiz = QuizState(quiz_count=1, latest_quiz_id=quiz_id, latest_quiz_status="not_started",
                     latest_quiz_question_count=questions)
    learning_state, reason = derive_learning_state(state.summary, state.flashcards, quiz, 0, None)
    return replace(state, quiz=quiz, learning_state=learning_state, state_reason=reason)


def weak(*topics):
    return tuple(WeakTopic(f"t-{name.lower()}", name, severity, score) for name, severity, score in topics)


def candidates(state, deadline=None):
    return select_candidates(material(state, deadline), MONDAY)


def plan(state):
    return [(c.activity_type, c.reason.code, c.estimated_minutes) for c in candidates(state)]


class LearningStateTests(ScheduleAssertions, unittest.TestCase):
    # -- no quiz created ---------------------------------------------------------------------------

    def test_no_quiz_learns_first_and_never_assumes_a_quiz_exists(self):
        new = candidates(make_state("d"))
        self.assertEqual([(c.activity_type, c.reason.metadata.get("preparation")) for c in new],
                         [("summary", "generate_summary"), ("flashcards", "create_flashcards"), ("quiz", "create_quiz")])
        quiz = new[-1]
        self.assertIsNone(quiz.artifact_id)
        self.assertIn("create a first quiz", quiz.reason.message)
        self.assertEqual({c.reason.metadata["learning_stage"] for c in new}, {"no_quiz"})
        # Summary and flashcards already there: recall practice, then a first quiz to create.
        ready = candidates(make_state("d", summary=True, cards=12))
        self.assertEqual([(c.activity_type, c.reason.metadata.get("preparation")) for c in ready],
                         [("written_quiz", None), ("quiz", "create_quiz")])

    def test_no_quiz_schedules_learning_before_the_quiz(self):
        ctx = context([material(make_state("d"), "2026-10-09")], EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        kinds = [p.activity_type for p in result.proposals]
        self.assertEqual(kinds[:3], ["summary", "flashcards", "quiz"])
        self.assertLess(day_of(result.proposals[1].scheduled_start), day_of(result.proposals[2].scheduled_start))

    # -- quiz created, not attempted ----------------------------------------------------------------

    def test_quiz_created_but_not_attempted_gets_the_initial_quiz(self):
        state = with_quiz_created(make_state("d", summary=True))
        self.assertEqual(state.state_reason.code, "study_started")
        first = candidates(state)
        self.assertEqual([(c.activity_type, c.artifact_id, c.estimated_minutes) for c in first], [("quiz", "q-new", 35)])
        self.assertEqual(first[0].reason.metadata["learning_stage"], "quiz_not_attempted")
        self.assertIn("take the quiz you created", first[0].reason.message)
        # Without a summary it is generated first; the quiz follows on a later day.
        no_summary = candidates(with_quiz_created(make_state("d")))
        self.assertEqual([(c.activity_type, c.artifact_id) for c in no_summary], [("summary", None), ("quiz", "q-new")])
        ctx = context([material(with_quiz_created(make_state("d")))], EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertLess(day_of(result.proposals[0].scheduled_start), day_of(result.proposals[1].scheduled_start))

    # -- scored quizzes ------------------------------------------------------------------------------

    def test_low_score_prioritizes_weak_topic_review_and_retry(self):
        state = replace(make_state("d", cards=20, percentage=40.0),
                        weak_topics=weak(("Regression", "high", 30.0), ("Sampling", "moderate", 55.0)))
        steps = candidates(state)
        self.assertEqual([(c.activity_type, c.reason.code, c.estimated_minutes) for c in steps],
                         [("review", "low_quiz_score", 30), ("written_quiz", "retrieval_practice", 20),
                          ("quiz_retry", "low_quiz_score", 35)])
        self.assertIn("Regression, Sampling", steps[0].reason.message)
        self.assertEqual([t["name"] for t in steps[0].reason.metadata["weak_topics"]], ["Regression", "Sampling"])
        self.assertEqual(steps[0].reason.metadata["percentage"], 40.0)
        self.assertEqual(steps[-1].artifact_id, "q-d")

    # -- very weak score (< 40%): foundational relearning before reassessment (P1.2) ----------------
    # Before, 17% got the same review -> written quiz -> retry as 59% (or only review -> retry
    # without flashcards): never back to the summary before being assessed again.

    def test_very_low_score_relearns_from_the_summary_before_practice_and_retry(self):
        state = replace(make_state("d", summary=True, cards=20, percentage=17.0),
                        weak_topics=weak(("Regression", "high", 10.0), ("Sampling", "high", 20.0)))
        steps = candidates(state)
        self.assertEqual([(c.activity_type, c.reason.code, c.estimated_minutes) for c in steps],
                         [("summary", "low_quiz_score", 45), ("review", "low_quiz_score", 30),
                          ("written_quiz", "retrieval_practice", 20), ("quiz_retry", "low_quiz_score", 35)])
        self.assertIn("re-read the summary on Regression, Sampling", steps[0].reason.message)
        self.assertNotIn("preparation", steps[0].reason.metadata)        # the summary already exists
        self.assertEqual({c.reason.metadata["learning_stage"] for c in steps}, {"very_low_score"})
        self.assertEqual(steps[-1].artifact_id, "q-d")

    def test_very_low_score_without_resources_prepares_them_explicitly(self):
        steps = candidates(make_state("d", percentage=17.0))   # no summary, no flashcards
        self.assertEqual([(c.activity_type, c.reason.metadata.get("preparation")) for c in steps],
                         [("summary", "generate_summary"), ("flashcards", "create_flashcards"), ("quiz_retry", None)])
        self.assertNotIn("written_quiz", [c.activity_type for c in steps])   # needs a persisted set
        only_summary = candidates(make_state("d", summary=True, percentage=17.0))
        self.assertEqual([(c.activity_type, c.reason.metadata.get("preparation")) for c in only_summary],
                         [("summary", None), ("flashcards", "create_flashcards"), ("quiz_retry", None)])
        # Never a duplicate: one summary, one retry, no separate review that would reopen the summary.
        self.assertEqual(Counter(c.activity_type for c in only_summary)["summary"], 1)
        self.assertNotIn("review", [c.activity_type for c in only_summary])

    def test_very_low_score_threshold_is_40(self):
        for percentage, stage, first in ((0.0, "very_low_score", "summary"), (39.9, "very_low_score", "summary"),
                                         (40.0, "low_score", "review"), (59.9, "low_score", "review")):
            with self.subTest(percentage=percentage):
                head = candidates(make_state("d", summary=True, cards=10, percentage=percentage))[0]
                self.assertEqual((head.activity_type, head.reason.metadata["learning_stage"]), (first, stage))

    def test_very_low_score_schedules_relearning_first_spaced_and_before_the_deadline(self):
        ctx = context([material(make_state("d", summary=True, cards=10, percentage=17.0), "2026-10-09")], EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual(result.capacity.status, "on_track")
        kinds = [p.activity_type for p in result.proposals]
        self.assertEqual(kinds[:4], ["summary", "review", "written_quiz", "quiz_retry"])
        days = [day_of(p.scheduled_start) for p in result.proposals]
        self.assertEqual(days[0], MONDAY.date())                     # due 1 day after yesterday's quiz
        self.assertEqual(days, sorted(set(days)))                    # one per day, in order
        for day in days:
            self.assertLess(day, date(2026, 10, 9))
        # Accepted sessions / Google busy time are worked around exactly as before.
        busy = [{"scheduled_start": "2026-09-28T19:00:00", "scheduled_end": "2026-09-28T20:30:00", "status": "scheduled"}]
        around = context([material(make_state("d", summary=True, cards=10, percentage=17.0))], EVENINGS, busy=busy)
        moved = plan_schedule(around)
        self.assert_well_formed(moved, around)
        self.assertEqual([p.activity_type for p in moved.proposals],
                         ["summary", "review", "written_quiz", "quiz_retry"])

    def test_every_learning_state_has_its_own_sequence(self):
        sequences = {
            "new": (make_state("d"), ["summary", "flashcards", "quiz"]),
            "summary, no quiz": (make_state("d", summary=True), ["flashcards", "quiz"]),
            "summary + cards, no quiz": (make_state("d", summary=True, cards=10), ["written_quiz", "quiz"]),
            "quiz created": (with_quiz_created(make_state("d", summary=True)), ["quiz"]),
            "17%": (make_state("d", summary=True, cards=10, percentage=17.0),
                    ["summary", "review", "written_quiz", "quiz_retry"]),
            "45%": (make_state("d", cards=10, percentage=45.0), ["review", "written_quiz", "quiz_retry"]),
            "70%": (make_state("d", cards=10, percentage=70.0), ["review", "written_quiz", "quiz_retry"]),
            "90%": (make_state("d", percentage=90.0), ["review", "review"]),
        }
        for name, (state, expected) in sequences.items():
            with self.subTest(state=name):
                self.assertEqual([c.activity_type for c in candidates(state)], expected)

    def test_moderate_score_reviews_incorrect_topics_then_reassesses(self):
        state = replace(make_state("d", cards=20, percentage=70.0), weak_topics=weak(("Probability", "moderate", 62.0)))
        steps = candidates(state)
        self.assertEqual([(c.activity_type, c.reason.code, c.estimated_minutes) for c in steps],
                         [("review", "review_due", 30), ("written_quiz", "retrieval_practice", 20),
                          ("quiz_retry", "review_due", 35)])
        self.assertIn("topics you got wrong (Probability)", steps[0].reason.message)
        self.assertEqual(steps[0].reason.metadata["learning_stage"], "moderate_score")

    def test_strong_score_gets_lighter_spaced_repetition(self):
        steps = candidates(make_state("d", cards=20, percentage=90.0))
        self.assertEqual([(c.activity_type, c.estimated_minutes) for c in steps], [("review", 20), ("review", 20)])
        self.assertNotIn("quiz_retry", [c.activity_type for c in steps])
        ctx = context([material(make_state("d", cards=20, percentage=90.0))], EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        days = [day_of(p.scheduled_start) for p in result.proposals]
        self.assertGreaterEqual(days[0], MONDAY.date() + timedelta(days=4))     # spaced, not tomorrow
        self.assertGreater(days[1], days[0] + timedelta(days=5))                # repetition further out

    def test_strong_score_with_a_clearly_weak_topic_still_reviews_it(self):
        state = replace(make_state("d", percentage=85.0), weak_topics=weak(("Ethics", "high", 35.0)))
        steps = candidates(state)
        self.assertEqual([(c.activity_type, c.estimated_minutes) for c in steps], [("review", 30)])
        self.assertIn("Ethics is still weak", steps[0].reason.message)

    def test_score_thresholds_are_60_and_80(self):
        for percentage, stage in ((59.9, "low_score"), (60.0, "moderate_score"), (79.9, "moderate_score"),
                                  (80.0, "strong_score")):
            with self.subTest(percentage=percentage):
                first = candidates(make_state("d", percentage=percentage))[0]
                self.assertEqual(first.reason.metadata["learning_stage"], stage)

    def test_weak_topics_are_never_invented(self):
        for percentage in (40.0, 70.0):
            with self.subTest(percentage=percentage):
                first = candidates(make_state("d", percentage=percentage))[0]
                self.assertEqual(first.reason.metadata["weak_topics"], [])
                self.assertNotIn("(", first.reason.message.split(":", 1)[1])   # no topic list quoted

    # -- missing resources ---------------------------------------------------------------------------

    def test_never_asks_for_resources_that_do_not_exist(self):
        for state in (make_state("d", percentage=40.0), make_state("d", percentage=70.0), make_state("d", percentage=95.0)):
            with self.subTest(percentage=state.quiz.latest_completed.percentage):
                kinds = [c.activity_type for c in candidates(state)]
                self.assertNotIn("flashcards", kinds)      # no set to practice
                self.assertNotIn("written_quiz", kinds)    # needs flashcards
        with_cards = [c.activity_type for c in candidates(make_state("d", cards=10, percentage=40.0))]
        self.assertIn("written_quiz", with_cards)

    # -- multiple documents and deadlines ------------------------------------------------------------

    def test_multiple_documents_each_follow_their_own_state(self):
        materials = [material(make_state("new")), material(with_quiz_created(make_state("taken", summary=True))),
                     material(make_state("low", cards=10, percentage=45.0)), material(make_state("mid", percentage=65.0)),
                     material(make_state("top", percentage=92.0))]
        ctx = context(materials, EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        first = {}
        for p in result.proposals:
            first.setdefault(p.document_id, (p.activity_type, p.reason.metadata.get("learning_stage")))
        self.assertEqual(first, {"new": ("summary", "no_quiz"), "taken": ("quiz", "quiz_not_attempted"),
                                 "low": ("review", "low_score"), "mid": ("review", "moderate_score"),
                                 "top": ("review", "strong_score")})
        self.assertEqual(max(Counter((p.document_id, p.scheduled_start[:10]) for p in result.proposals).values()), 1)
        self.assertEqual(plan_schedule(ctx), result)   # deterministic

    def test_low_score_before_a_close_deadline(self):
        ctx = context([material(make_state("d", cards=10, percentage=40.0), "2026-10-02")], EVENINGS)
        result = plan_schedule(ctx)
        self.assert_well_formed(result, ctx)
        self.assertEqual(result.capacity.status, "on_track")
        self.assertEqual([p.activity_type for p in result.proposals][:3], ["review", "written_quiz", "quiz_retry"])
        for p in result.proposals:
            self.assertLess(day_of(p.scheduled_start), date(2026, 10, 2))
        # No time at all before the deadline: reported, never forced.
        tight = context([material(make_state("d", cards=10, percentage=40.0), "2026-09-29")], weekly((0, "19:00", "19:30")))
        short = plan_schedule(tight)
        self.assertEqual(short.capacity.status, "at_risk")
        self.assertIn("quiz_retry", [c.activity_type for c in short.capacity.unscheduled])


class PersistedMasteryTests(PlannerDatabaseMixin, unittest.TestCase):
    """Weak topics come from the topic mastery the quiz pipeline already saves."""

    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "stats", "Statistics")

    def save_mastery(self, topic_id, correct, total):
        rows = [{"is_correct": index < correct, "question_difficulty": "easy"} for index in range(total)]
        quiz_store.save_topic_mastery({"student_id": self.alice, "document_id": "stats", "topic_id": topic_id,
                                       **calculate_mastery(rows, completed_attempts=1)})

    def test_weak_topics_come_from_saved_mastery_and_only_after_a_quiz(self):
        self.save_mastery("regression", 1, 8)    # Weak
        self.save_mastery("means", 8, 8)         # Mastered: not a gap
        self.save_mastery("tiny", 0, 1)          # not enough evidence: never reported
        self.assertEqual(get_document_study_state(self.alice, "stats").weak_topics, ())   # no completed quiz yet
        self.add_quiz(self.alice, "stats", "q1")
        self.add_attempt(self.alice, "stats", "q1", score=4, answered=10, completed=True,
                         completed_at="2026-09-27T19:00:00+00:00")
        state = get_document_study_state(self.alice, "stats")
        self.assertEqual([(t.topic_id, t.severity) for t in state.weak_topics], [("regression", "high")])
        review = select_candidates(material(state), MONDAY)[0]
        self.assertEqual((review.activity_type, review.reason.code), ("review", "low_quiz_score"))
        self.assertIn("regression", review.reason.message)


def session(session_id, document_id, activity, start, minutes, status="scheduled", reason="new_material"):
    begin = datetime.fromisoformat(start)
    return {"session_id": session_id, "document_id": document_id, "activity_type": activity,
            "scheduled_start": begin.isoformat(), "scheduled_end": (begin + timedelta(minutes=minutes)).isoformat(),
            "duration_minutes": minutes, "status": status, "reason": reason, "artifact_id": None}


class BalancedAdaptationTests(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 12, 0)
    QUIZ_THIS_MORNING = "2026-09-24T10:00:00+00:00"

    def propose(self, trigger, materials, sessions, availability=ADAPT_EVENINGS, config=None):
        busy = tuple(s for s in sessions if s["status"] in ("scheduled", "in_progress", "completed")
                     and s["scheduled_start"][:10] >= self.NOW.date().isoformat())
        ctx = AdaptationContext(trigger=trigger, now=self.NOW, utc_offset=timedelta(0), materials=tuple(materials),
                                plan_sessions=tuple(sessions), availability=tuple(availability), busy_sessions=busy)
        return propose_adaptation(ctx, config) if config else propose_adaptation(ctx)

    def test_after_a_low_quiz_the_new_sessions_spread_over_different_days(self):
        state = make_state("mkt", cards=10, percentage=40.0, completed_at=self.QUIZ_THIS_MORNING)
        result = self.propose(AdaptationTrigger("quiz_completed", document_id="mkt"), [material(state)], [])
        added = [(a.activity_type, a.scheduled_start[:10], a.duration_minutes) for a in result.added]
        self.assertEqual(added, [("review", "2026-09-25", 30), ("written_quiz", "2026-09-26", 20),
                                 ("quiz_retry", "2026-09-27", 35)])

    def test_after_a_strong_quiz_only_light_spaced_reviews_are_added(self):
        state = make_state("mkt", cards=10, percentage=90.0, completed_at=self.QUIZ_THIS_MORNING)
        planned = [session("retry", "mkt", "quiz_retry", "2026-09-25T18:00:00", 35, reason="low_quiz_score")]
        result = self.propose(AdaptationTrigger("quiz_completed", document_id="mkt"), [material(state)], planned)
        self.assertEqual([c.session_id for c in result.cancelled], ["retry"])        # no longer needed
        self.assertEqual({(a.activity_type, a.duration_minutes) for a in result.added}, {("review", 20)})
        self.assertGreaterEqual(min(a.scheduled_start for a in result.added), "2026-09-29")

    def test_new_material_after_a_deadline_change_is_paced_not_crammed(self):
        state = make_state("mkt")
        mat = material(state, "2026-10-10")
        result = self.propose(AdaptationTrigger("deadline_changed", document_id="mkt"), [mat], [])
        learn = [day_of(a.scheduled_start) for a in result.added if a.reason_code != "final_review"]
        self.assertEqual(len(set(learn)), 3)
        self.assertGreaterEqual((learn[-1] - learn[0]).days, 4)
        self.assertTrue(all(day_of(a.scheduled_start) < date(2026, 10, 10) for a in result.added))

    def test_target_date_paces_adaptation_sooner(self):
        mat = material(make_state("mkt"))
        mat.material["target_date"] = "2026-09-27"
        result = self.propose(AdaptationTrigger("deadline_changed", document_id="mkt"), [mat], [])
        self.assertTrue(result.added)
        self.assertLessEqual(max(a.scheduled_start[:10] for a in result.added), "2026-09-28")

    def test_missed_session_replacement_avoids_a_day_the_document_already_studies(self):
        mat = material(make_state("stats", summary=True, cards=8))
        sessions = [session("missed", "stats", "written_quiz", "2026-09-24T09:00:00", 20),
                    session("other", "stats", "quiz", "2026-09-27T18:00:00", 35)]
        result = self.propose(AdaptationTrigger("session_missed", session_id="missed"), [mat],
                              sessions + [session("busy", "stats", "review", "2026-09-24T18:00:00", 20, status="completed")])
        self.assertEqual([(a.activity_type, a.scheduled_start[:10]) for a in result.added], [("written_quiz", "2026-09-25")])
        self.assertEqual((result.moved, result.cancelled), ((), ()))     # the planned quiz stays put

    def test_accepted_sessions_keep_their_length_and_google_busy_time_is_respected(self):
        state = make_state("mkt", cards=10, percentage=40.0, completed_at=self.QUIZ_THIS_MORNING)
        planned = [session("cards", "mkt", "flashcards", "2026-09-25T18:00:00", 15, reason="low_quiz_score")]
        google = ({"busy": True, "date": "2026-09-26", "start_at": "18:00", "end_at": "21:00", "is_recurring": False,
                   "day_of_week": None},)
        result = self.propose(AdaptationTrigger("quiz_completed", document_id="mkt"), [material(state)], planned,
                              availability=ADAPT_EVENINGS + google)
        self.assertEqual((result.moved, result.cancelled), ((), ()))      # the 15-minute practice is the weak-topic review
        self.assertEqual(result.unchanged_count, 1)
        self.assertNotIn("2026-09-26", [a.scheduled_start[:10] for a in result.added])
        for a in result.added:
            self.assertGreater(a.scheduled_start, "2026-09-25T18:15:00")


class AdaptationApiTests(PlannerDatabaseMixin, unittest.TestCase):
    """End to end: the preview after a real persisted quiz attempt names the saved weak topic."""

    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.client = TestClient(app)
        self.assertEqual(self.client.post("/api/auth/login", json={"email": "alice-planner@example.com",
                                                                   "password": "long-password-x"}).status_code, 200)
        for day in range(7):
            self.client.post("/api/planner/availability", json={"start_at": "18:00", "end_at": "21:00",
                                                                "is_recurring": True, "day_of_week": day})
        self.plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/materials", json={"document_id": "mkt"})

    def test_quiz_completed_preview_uses_saved_results_and_mastery(self):
        self.add_summary(self.alice, "mkt")
        self.add_quiz(self.alice, "mkt", "q1")
        self.add_attempt(self.alice, "mkt", "q1", score=5, answered=10, completed=True,
                         completed_at="2026-09-24T04:00:00+00:00")
        rows = [{"is_correct": False, "question_difficulty": "easy"} for _ in range(6)]
        quiz_store.save_topic_mastery({"student_id": self.alice, "document_id": "mkt", "topic_id": "pricing",
                                       **calculate_mastery(rows, completed_attempts=1)})
        body = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/adaptation/preview", json={
            "trigger": {"kind": "quiz_completed", "document_id": "mkt"}, "utc_offset_minutes": 0,
            "local_now": "2026-09-24T12:00:00"}).json()
        self.assertEqual([(a["activity_type"], a["duration_minutes"]) for a in body["added"]],
                         [("review", 30), ("quiz_retry", 35)])   # no flashcards exist: no flashcard practice
        self.assertIn("pricing", body["added"][0]["message"])
        self.assertIn("50%", body["added"][0]["message"])
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"]), [])   # preview only


if __name__ == "__main__":
    unittest.main()
