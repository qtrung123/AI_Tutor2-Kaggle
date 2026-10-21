"""A submitted quiz completes the quiz part of the Study Planner session it was taken for
(api/quiz_attempts.quiz_submit -> study_plan_api_service.complete_quiz_from_attempt ->
study_planner_store.complete_quiz_activity), through the real API on a temporary database.

Covers single quiz sessions, Summary + Quiz and Flashcards + Quiz (with and without the first step
done), matching only the session the attempt belongs to (never by document alone, never another
document's or another user's session, never a session that was not started), repeated and failed
submissions, a resumed session and a lost session link, practice vs assessment quizzes, legacy
attempts, manual completion of other activities and Google Calendar sync. 2026-09-28 is a Monday."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin

from backend import google_calendar_sync, quiz_store, study_plan_api_service, study_planner_store
from backend import study_combined_sessions as combined
from backend.document_study_state import get_document_study_state
from backend.main import app

PASSWORD = "long-password-x"
START_AT = "2026-09-28T18:05:00"
QUESTIONS = [
    {"id": 1, "question": "Which one orders processes?", "options": ["A. scheduler", "B. pager", "C. cache", "D. disk"],
     "correct_answer": "A", "question_type": "single_choice", "topic_id": "t1"},
    {"id": 2, "question": "Which one maps pages?", "options": ["A. scheduler", "B. MMU", "C. cache", "D. disk"],
     "correct_answer": "B", "question_type": "single_choice", "topic_id": "t1"},
]
RIGHT = {"1": "A", "2": "B"}


class PlannerQuizAutoCompleteTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.addCleanup(self.stop_planner_database)
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = TestClient(app)
        login = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        for document_id in ("mkt", "stats"):
            study_planner_store.add_material(self.alice, self.plan["plan_id"], document_id)
        self.quiz = self.save_quiz("mkt")
        self.calendar_calls = []
        sync = patch.object(google_calendar_sync, "after_planner_change",
                            side_effect=lambda owner, offset=None: self.calendar_calls.append(owner))
        sync.start()
        self.addCleanup(sync.stop)

    # helpers -----------------------------------------------------------------------------------
    def save_quiz(self, document_id, owner=None, practice=False, title="Quiz"):
        plan = {"planner_version": "flashcard_written_v1" if practice else "study_units_v1"}
        return quiz_store.save_quiz(document_id, "easy", {
            "title": title, "topic_id": "document", "assessment_scope": "document", "topic_schema_version": 2,
            "assessment_plan": plan, "questions": QUESTIONS}, owner or self.alice)

    def session(self, activity="quiz", *, document_id="mkt", steps=None, artifact_id=None, hour=18, owner=None, plan=None):
        record = {"document_id": document_id, "activity_type": activity, "artifact_id": artifact_id,
                  "scheduled_start": f"2026-09-28T{hour}:00:00", "scheduled_end": f"2026-09-28T{hour}:35:00",
                  "duration_minutes": 35, "reason": "retrieval_practice"}
        if steps:
            record["steps"] = combined.new_steps(steps)
        return study_planner_store.create_session(owner or self.alice, (plan or self.plan)["plan_id"], record)["session_id"]

    def start(self, session_id):
        response = self.client.post(f"/api/planner/sessions/{session_id}/start",
                                    json={"utc_offset_minutes": 0, "local_now": START_AT})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, session_id=None, quiz=None, answers=RIGHT, document_id="mkt"):
        quiz = quiz or self.quiz
        body = {"quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document", "answers": answers}
        if session_id:
            body["planner_session_id"] = session_id
        return self.client.post(f"/api/quiz/{document_id}/submit", json=body)

    def stored(self, session_id):
        return study_planner_store.get_session(self.alice, session_id)

    # single quiz sessions ----------------------------------------------------------------------
    def test_a_single_quiz_session_completes_when_its_quiz_is_submitted(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        response = self.submit(sid)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["score"], 2)                                      # the attempt itself is unchanged
        completion = body["planner_completion"]
        self.assertEqual((completion["status"], completion["changed"], completion["session_completed"]),
                         ("completed", True, True))
        self.assertEqual((completion["session"]["status"], completion["step_index"]), ("completed", None))
        session = self.stored(sid)
        self.assertEqual(session["status"], "completed")
        self.assertTrue(session["completed_at"] and session["started_at"])
        self.assertIn("quiz", get_document_study_state(self.alice, "mkt").completed_activities)
        self.assertEqual(self.calendar_calls.count(self.alice), 2)              # start + this completion, like Complete

    def test_a_quiz_retry_session_completes_with_a_retake_of_its_quiz(self):
        self.submit()                                                          # an earlier attempt of the same quiz
        sid = self.session("quiz_retry", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        self.assertEqual(self.submit(sid).json()["planner_completion"]["status"], "completed")
        self.assertEqual(self.stored(sid)["status"], "completed")

    # combined sessions -------------------------------------------------------------------------
    def test_summary_plus_quiz_completes_the_session_after_the_summary_step(self):
        sid = self.session("summary", steps=(("summary", 15, None), ("quiz", 20, None)))
        self.start(sid)
        self.client.post(f"/api/planner/sessions/{sid}/steps/0/complete")
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["step_index"], completion["session_completed"]), ("completed", 1, True))
        session = self.stored(sid)
        self.assertEqual(session["status"], "completed")
        self.assertEqual([step["status"] for step in session["steps"]], ["completed", "completed"])
        self.assertTrue(session["completed_at"] and session["steps"][1]["completed_at"])
        self.assertEqual(session["steps"][1]["attempt_id"], completion["session"]["steps"][1]["attempt_id"])
        self.assertTrue(session["steps"][1]["attempt_id"])

    def test_flashcards_plus_quiz_with_the_first_step_open_completes_only_the_quiz_step(self):
        sid = self.session("flashcards", steps=(("flashcards", 15, None), ("quiz", 20, None)))
        self.start(sid)
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["step_index"], completion["session_completed"]), ("completed", 1, False))
        session = self.stored(sid)
        self.assertEqual(session["status"], "in_progress")                      # the parent stays open
        self.assertIsNone(session["completed_at"])
        self.assertEqual([step["status"] for step in session["steps"]], ["pending", "completed"])
        self.assertIsNone(session["steps"][0]["completed_at"])                   # the other step is untouched
        # Finishing the first step afterwards completes the session.
        last = self.client.post(f"/api/planner/sessions/{sid}/steps/0/complete").json()
        self.assertEqual((last["session_completed"], last["session"]["status"]), (True, "completed"))
        # A manual Complete before then is still refused while a step is open (unchanged rule).
        other = self.session("flashcards", steps=(("flashcards", 15, None), ("quiz", 20, None)), hour=19)
        self.start(other)
        self.submit(other)
        refused = self.client.post(f"/api/planner/sessions/{other}/complete")
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "session_steps_incomplete"))

    def test_a_combined_row_carrying_its_flashcard_set_still_completes_with_a_new_quiz(self):
        # The scheduler stores `second.artifact_id or first.artifact_id`: with no quiz yet, the
        # row names the flashcard set. Only the quiz step's own artifact decides the match.
        sid = self.session("flashcards", steps=(("flashcards", 15, "set-1"), ("quiz", 20, None)), artifact_id="set-1")
        self.start(sid)
        self.assertEqual(self.submit(sid).json()["planner_completion"]["status"], "completed")
        self.assertEqual([step["status"] for step in self.stored(sid)["steps"]], ["pending", "completed"])

    def test_a_combined_quiz_step_planned_for_a_quiz_matches_it_without_a_link(self):
        sid = self.session("flashcards", steps=(("flashcards", 15, "set-1"), ("quiz", 20, self.quiz["quiz_id"])),
                           artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        self.assertEqual(self.submit().json()["planner_completion"]["status"], "completed")

    def test_a_combined_session_without_a_quiz_step_is_never_touched(self):
        sid = self.session("summary", steps=(("summary", 20, None), ("flashcards", 15, None)))
        self.start(sid)
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "quiz_session_mismatch"))
        self.assertEqual([step["status"] for step in self.stored(sid)["steps"]], ["pending", "pending"])

    # matching the right session ----------------------------------------------------------------
    def test_only_the_named_session_completes_among_several_for_the_same_document(self):
        first = self.session("quiz", hour=18)
        second = self.session("quiz", hour=19)
        self.start(first)
        self.start(second)
        self.assertEqual(self.submit(second).json()["planner_completion"]["session_id"], second)
        self.assertEqual((self.stored(first)["status"], self.stored(second)["status"]), ("in_progress", "completed"))

    def test_the_active_combined_session_completes_only_its_own_quiz_step(self):
        # The Study Session page's step bar names the active combined session; two other started sessions
        # for the same document (another combined one, a single quiz) must stay exactly as they were.
        active = self.session("summary", steps=(("summary", 15, None), ("quiz", 20, None)), hour=18)
        other = self.session("flashcards", steps=(("flashcards", 15, None), ("quiz", 20, None)), hour=19)
        single = self.session("quiz", hour=20)
        for sid in (active, other, single):
            self.start(sid)
        self.client.post(f"/api/planner/sessions/{active}/steps/0/complete")
        completion = self.submit(active).json()["planner_completion"]
        self.assertEqual((completion["session_id"], completion["step_index"], completion["session_completed"]), (active, 1, True))
        self.assertEqual([step["status"] for step in self.stored(active)["steps"]], ["completed", "completed"])
        self.assertEqual(self.stored(active)["status"], "completed")
        self.assertEqual(([step["status"] for step in self.stored(other)["steps"]], self.stored(other)["status"]),
                         (["pending", "pending"], "in_progress"))
        self.assertEqual(self.stored(single)["status"], "in_progress")

    def test_without_a_session_link_a_document_match_alone_completes_nothing(self):
        sid = self.session("quiz")                                              # no planned quiz (artifact_id None)
        self.start(sid)
        self.assertIsNone(self.submit().json()["planner_completion"])
        self.assertEqual(self.stored(sid)["status"], "in_progress")

    def test_a_lost_link_still_completes_the_one_started_session_that_planned_this_exact_quiz(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        self.assertEqual(self.submit().json()["planner_completion"]["status"], "completed")   # e.g. another tab
        self.assertEqual(self.stored(sid)["status"], "completed")

    def test_two_started_sessions_planning_the_same_quiz_are_ambiguous_without_a_link(self):
        first = self.session("quiz", artifact_id=self.quiz["quiz_id"], hour=18)
        second = self.session("quiz", artifact_id=self.quiz["quiz_id"], hour=19)
        self.start(first)
        self.start(second)
        self.assertIsNone(self.submit().json()["planner_completion"])
        self.assertEqual({self.stored(first)["status"], self.stored(second)["status"]}, {"in_progress"})

    def test_another_documents_session_is_never_completed(self):
        sid = self.session("quiz", document_id="stats")
        self.start(sid)
        completion = self.submit(sid).json()["planner_completion"]             # a Marketing quiz
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "quiz_session_mismatch"))
        self.assertEqual(self.stored(sid)["status"], "in_progress")

    def test_a_session_planned_for_a_different_quiz_is_never_completed(self):
        other = self.save_quiz("mkt", title="Other quiz")
        sid = self.session("quiz", artifact_id=other["quiz_id"])
        self.start(sid)
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "quiz_session_mismatch"))
        self.assertEqual(self.stored(sid)["status"], "in_progress")

    def test_another_users_session_is_never_completed(self):
        bob = self.user("Bob")
        self.add_document(bob, "mkt", "Marketing")
        bob_plan = study_planner_store.create_plan(bob, "Bob's plan")
        study_planner_store.add_material(bob, bob_plan["plan_id"], "mkt")
        bob_session = self.session("quiz", owner=bob, plan=bob_plan)
        study_planner_store.start_session(bob, bob_session)
        completion = self.submit(bob_session).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "session_not_found"))
        self.assertEqual(study_planner_store.get_session(bob, bob_session)["status"], "in_progress")

    def test_a_session_that_was_not_started_is_never_completed(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])           # only opened, never started
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "session_not_started"))
        self.assertEqual(self.stored(sid)["status"], "scheduled")
        self.assertIsNone(self.submit().json()["planner_completion"])           # no lost-link match either

    def test_skipped_sessions_stay_skipped(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        self.client.post(f"/api/planner/sessions/{sid}/skip")
        completion = self.submit(sid).json()["planner_completion"]
        self.assertEqual((completion["status"], completion["code"]), ("not_applied", "session_not_started"))
        self.assertEqual(self.stored(sid)["status"], "skipped")

    # practice vs assessment --------------------------------------------------------------------
    def test_a_written_quiz_session_completes_only_with_its_practice_quiz(self):
        practice = self.save_quiz("mkt", practice=True, title="Practice")
        sid = self.session("written_quiz", artifact_id=practice["quiz_id"])
        self.start(sid)
        mismatch = self.submit(sid).json()["planner_completion"]               # the normal quiz
        self.assertEqual(mismatch["status"], "not_applied")
        self.assertEqual(self.submit(sid, quiz=practice).json()["planner_completion"]["status"], "completed")
        self.assertEqual(self.stored(sid)["status"], "completed")

    def test_practice_never_completes_an_assessment_quiz_session_or_step(self):
        practice = self.save_quiz("mkt", practice=True, title="Practice")
        single = self.session("quiz", hour=18)
        steps = self.session("flashcards", steps=(("flashcards", 15, None), ("quiz", 20, None)), hour=19)
        self.start(single)
        self.start(steps)
        for sid in (single, steps):
            self.assertEqual(self.submit(sid, quiz=practice).json()["planner_completion"]["status"], "not_applied")
        self.assertEqual(self.stored(single)["status"], "in_progress")
        self.assertEqual([s["status"] for s in self.stored(steps)["steps"]], ["pending", "pending"])

    # reliability -------------------------------------------------------------------------------
    def test_a_repeated_submission_never_counts_twice(self):
        sid = self.session("flashcards", steps=(("flashcards", 15, None), ("quiz", 20, None)))
        self.start(sid)
        first = self.submit(sid).json()
        before = self.stored(sid)
        calls = len(self.calendar_calls)
        again = self.submit(sid).json()
        self.assertNotEqual(first["attempt_id"], again["attempt_id"])          # the quiz keeps both attempts
        self.assertEqual((again["planner_completion"]["status"], again["planner_completion"]["changed"]), ("unchanged", False))
        after = self.stored(sid)
        self.assertEqual(after["steps"], before["steps"])                       # same completed_at and attempt_id
        self.assertEqual(after["steps"][1]["attempt_id"], first["attempt_id"])
        self.assertEqual(len(self.calendar_calls), calls)                       # nothing synced again

    def test_a_failed_submission_changes_nothing(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        response = self.submit(sid, answers={"1": "A"})                         # incomplete, not "Submit Anyway"
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(sid)["status"], "in_progress")
        self.assertEqual(quiz_store.get_quiz_attempt_summary(self.quiz["quiz_id"], self.alice)["attempts"], 0)

    def test_a_planner_failure_never_fails_the_saved_submission(self):
        sid = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.start(sid)
        with patch.object(study_plan_api_service, "complete_quiz_from_attempt", side_effect=RuntimeError("db down")):
            response = self.submit(sid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["planner_completion"]["status"], "error")
        self.assertEqual(quiz_store.get_quiz_attempt_summary(self.quiz["quiz_id"], self.alice)["attempts"], 1)
        self.assertEqual(self.stored(sid)["status"], "in_progress")             # can still be completed by hand
        self.assertEqual(self.client.post(f"/api/planner/sessions/{sid}/complete").json()["session"]["status"], "completed")

    def test_a_resumed_session_after_a_refresh_still_completes(self):
        sid = self.session("summary", steps=(("summary", 15, None), ("quiz", 20, None)))
        self.start(sid)
        self.client.post(f"/api/planner/sessions/{sid}/steps/0/complete")
        resumed = self.start(sid)                                               # Resume after reloading the page
        self.assertEqual((resumed["started"], resumed["tool"], resumed["current_step"]), (False, "quiz", 1))
        self.assertEqual(self.submit(sid).json()["planner_completion"]["status"], "completed")
        self.assertEqual(self.stored(sid)["status"], "completed")

    # compatibility -----------------------------------------------------------------------------
    def test_legacy_attempts_and_plain_submissions_are_unaffected(self):
        legacy = self.session("quiz", artifact_id=self.quiz["quiz_id"])
        self.add_attempt(self.alice, "mkt", self.quiz["quiz_id"], score=8, answered=10, completed=True)
        self.assertEqual(self.stored(legacy)["status"], "scheduled")             # old attempts complete nothing
        history = self.client.get("/api/quiz-history?document_id=mkt").json()
        self.assertEqual(len(history), 1)
        plain = self.submit().json()                                            # no plan involved at all
        self.assertIsNone(plain["planner_completion"])
        self.assertEqual(plain["score"], 2)

    def test_other_activities_keep_manual_completion(self):
        sid = self.session("summary")
        self.start(sid)
        self.assertIsNone(self.submit().json()["planner_completion"])
        done = self.client.post(f"/api/planner/sessions/{sid}/complete").json()
        self.assertEqual((done["changed"], done["session"]["status"]), (True, "completed"))


if __name__ == "__main__":
    unittest.main()
