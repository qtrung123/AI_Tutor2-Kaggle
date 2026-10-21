"""Combined study sessions (backend/study_combined_sessions): one 30-40 minute session of two
sequential activities -- Summary + Flashcards, Summary + Quiz, Flashcards + Quiz -- interleaved with
single-activity sessions, adapted to time pressure and quiz performance, with per-step progress.

Scheduler rules are tested on pure inputs; persistence, the session lifecycle (start opens the
current step's existing tool, complete only after both steps), live candidates, adaptation and the
Google Calendar event title through the store and the API. 2026-09-28 is a Monday."""

import json
import unittest
from dataclasses import replace
from datetime import datetime

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from test_study_scheduler import context, every_day, make_state, material

from backend import google_calendar_sync, study_adaptation, study_combined_sessions as combined
from backend import study_placement, study_planner_store
from backend.document_study_state import QuizState, get_document_study_state
from backend.main import app
from backend.study_preferences import PLANNER_CONFIG
from backend.study_scheduler import DEFAULT_CONFIG, plan_schedule, select_candidates

MONDAY = datetime(2026, 9, 28, 8, 0)
PASSWORD = "long-password-x"
NOW = "2026-09-28T08:00:00"
QUIZ_NOT_STARTED = QuizState(quiz_count=1, latest_quiz_id="q-d", latest_quiz_status="not_started", latest_quiz_question_count=10)


def kinds(result):
    return [combined.activity_key(p.activity_type, p.steps) for p in result.proposals]


class CombinedRulesTests(unittest.TestCase):
    def test_minutes_stay_within_30_to_40_and_adapt(self):
        for first, second in (("summary", "flashcards"), ("summary", "quiz"), ("flashcards", "quiz_retry")):
            normal = combined.combined_minutes(first, second, daily_target_minutes=90, latest_percentage=None)
            weak = combined.combined_minutes(first, second, daily_target_minutes=90, latest_percentage=45.0)
            strong = combined.combined_minutes(first, second, daily_target_minutes=90, latest_percentage=90.0)
            short_day = combined.combined_minutes(first, second, daily_target_minutes=60, latest_percentage=None)
            self.assertEqual((sum(normal), sum(weak), sum(strong), sum(short_day)), (35, 40, 30, 30))
            self.assertGreater(weak[0], normal[0])                    # the extra minutes go to the learning part
            self.assertEqual(weak[1], normal[1])
            for parts in (normal, weak, strong, short_day):
                self.assertTrue(all(minutes >= 10 for minutes in parts))

    def test_only_the_three_pairs_combine(self):
        self.assertTrue(combined.can_pair("summary", "flashcards"))
        self.assertTrue(combined.can_pair("summary", "quiz"))
        self.assertTrue(combined.can_pair("flashcards", "quiz_retry"))
        for first, second in (("flashcards", "summary"), ("quiz", "flashcards"), ("review", "quiz"), ("summary", "review"),
                              ("quiz", "quiz")):
            self.assertFalse(combined.can_pair(first, second), (first, second))

    def test_step_validation(self):
        steps = combined.new_steps((("summary", 20, None), ("flashcards", 15, None)))
        combined.validate_steps(steps, 35, "summary")
        for bad, minutes, activity in ((steps[:1], 20, "summary"), (steps, 30, "summary"), (steps, 35, "flashcards"),
                                       ([{**steps[0], "status": "half"}, steps[1]], 35, "summary")):
            with self.assertRaises(ValueError):
                combined.validate_steps(bad, minutes, activity)
        self.assertEqual(combined.current_step_index(steps), 0)
        self.assertEqual(combined.current_step_index([{**steps[0], "status": "completed"}, steps[1]]), 1)
        self.assertIsNone(combined.current_step_index([{**step, "status": "completed"} for step in steps]))
        self.assertEqual(combined.label("summary", steps), "Summary + Flashcards")


class CombinedSchedulingTests(unittest.TestCase):
    def test_new_material_interleaves_a_combined_session_with_single_ones(self):
        # Summary and flashcards exist but were never studied, no quiz yet: summary -> flashcards ->
        # quiz (a day later) -> review. Reading + practice become one session; the quiz and the
        # review stay single sessions on later days.
        result = plan_schedule(context([material(make_state("d", summary=True, cards=10))], every_day(), now=MONDAY), PLANNER_CONFIG)
        self.assertEqual(kinds(result), ["summary+flashcards", "quiz", "review"])
        first, quiz, review = result.proposals
        self.assertEqual(first.duration_minutes, 35)
        self.assertEqual([(s["activity_type"], s["duration_minutes"], s["status"]) for s in first.steps],
                         [("summary", 20, "pending"), ("flashcards", 15, "pending")])
        self.assertIn("then practice the flashcards", first.reason.message)
        self.assertEqual((quiz.steps, review.steps), ((), ()))
        self.assertLess(first.scheduled_start[:10], quiz.scheduled_start[:10])   # the quiz is still a day later

    def test_time_pressure_combines_practice_with_the_quiz(self):
        # Quiz created, not attempted, flashcards not practiced; deadline in two days.
        state = replace(make_state("d", summary=True, cards=10), completed_activities=("summary",), quiz=QUIZ_NOT_STARTED)
        result = plan_schedule(context([material(state, "2026-09-30")], every_day(), now=MONDAY), PLANNER_CONFIG)
        self.assertEqual(kinds(result)[0], "flashcards+quiz")
        practice = result.proposals[0]
        self.assertEqual(practice.artifact_id, "q-d")                 # the quiz to take, not a new one
        self.assertEqual([s["artifact_id"] for s in practice.steps], [None, "q-d"])
        self.assertTrue(30 <= practice.duration_minutes <= 40)
        # Without time pressure (deadline far away) the quiz keeps its own later day.
        relaxed = plan_schedule(context([material(state, "2026-10-20")], every_day(), now=MONDAY), PLANNER_CONFIG)
        self.assertEqual(kinds(relaxed)[:2], ["flashcards", "quiz"])

    def test_summary_plus_quiz_under_pressure(self):
        state = replace(make_state("d", summary=True, cards=10), completed_activities=("flashcards",), quiz=QUIZ_NOT_STARTED)
        result = plan_schedule(context([material(state, "2026-09-30")], every_day(), now=MONDAY), PLANNER_CONFIG)
        self.assertEqual(kinds(result)[0], "summary+quiz")
        self.assertEqual([s["duration_minutes"] for s in result.proposals[0].steps], [15, 20])

    def test_quiz_performance_shapes_the_combined_length(self):
        weak = make_state("w", summary=True, cards=10, percentage=45)    # review -> practice -> retry
        result = plan_schedule(context([material(weak, "2026-09-30")], every_day(), now=MONDAY), PLANNER_CONFIG)
        combos = [p for p in result.proposals if p.steps]
        self.assertTrue(combos)
        self.assertEqual({combined.activity_key(p.activity_type, p.steps) for p in combos}, {"flashcards+quiz_retry"})
        self.assertEqual({p.duration_minutes for p in combos}, {40})

    def test_never_combined(self):
        # A quiz in progress and the final review stay single sessions.
        for state in (make_state("p", summary=True, cards=10, in_progress=True),
                      make_state("c", summary=True, cards=10, planner_state="completed")):
            result = plan_schedule(context([material(state, "2026-10-02")], every_day(), now=MONDAY), PLANNER_CONFIG)
            self.assertTrue(result.proposals)
            self.assertFalse(any(p.steps for p in result.proposals), kinds(result))
        # Creating flashcards and a first quiz never share one sitting (the quiz needs the cards).
        bare = make_state("n", summary=True, cards=0)
        self.assertNotIn("flashcards+quiz", [c.key for c in select_candidates(material(bare, "2026-09-30"), MONDAY, PLANNER_CONFIG)])

    def test_switched_off_means_single_sessions_only(self):
        # The scheduler's base rules (DEFAULT_CONFIG) are single-activity; the planner switches combining on.
        self.assertEqual((DEFAULT_CONFIG.combine_sessions, PLANNER_CONFIG.combine_sessions), (False, True))
        result = plan_schedule(context([material(make_state("d", summary=True, cards=10))], every_day(), now=MONDAY))
        self.assertEqual(kinds(result), ["summary", "flashcards", "quiz", "review"])

    def test_heavy_limits_and_no_overlap_hold_with_combined_sessions(self):
        materials = [material(make_state(f"d{i}", summary=True, cards=10), "2026-10-01") for i in range(4)]
        result = plan_schedule(context(materials, every_day("18:00", "22:00"), now=MONDAY), PLANNER_CONFIG)
        by_day = {}
        for p in result.proposals:
            by_day.setdefault(p.scheduled_start[:10], []).append(p)
        for sessions in by_day.values():
            sessions.sort(key=lambda p: p.scheduled_start)
            self.assertLessEqual(sum(p.duration_minutes for p in sessions), DEFAULT_CONFIG.daily_target_minutes)
            heavy = [p for p in sessions if any(a in DEFAULT_CONFIG.heavy_activities
                                                for a in combined.activity_types(p.activity_type, p.steps))]
            self.assertLessEqual(len(heavy), DEFAULT_CONFIG.max_heavy_sessions_per_day)
            for before, after in zip(sessions, sessions[1:]):
                self.assertLessEqual(before.scheduled_end, after.scheduled_start)


class CombinedSessionApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_summary(self.alice, "mkt")
        self.add_flashcards(self.alice, "mkt", 6)
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        study_planner_store.add_material(self.alice, self.plan["plan_id"], "mkt")
        for day in range(7):
            self.client.post("/api/planner/availability", json={
                "start_at": "18:00", "end_at": "21:00", "is_recurring": True, "day_of_week": day})

    def body(self, **extra):
        return {"utc_offset_minutes": 0, "local_now": NOW, **extra}

    def confirm(self):
        response = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/confirm", json=self.body())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["sessions"]

    def combined_session(self):
        return next(s for s in self.confirm() if s["steps"])

    def start(self, session_id, local_now="2026-09-28T18:05:00"):
        return self.client.post(f"/api/planner/sessions/{session_id}/start", json=self.body(local_now=local_now))

    def step_done(self, session_id, index):
        return self.client.post(f"/api/planner/sessions/{session_id}/steps/{index}/complete")

    def test_preview_and_confirm_persist_one_block_with_two_steps(self):
        preview = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/preview", json=self.body()).json()
        first = preview["sessions"][0]
        self.assertEqual([s["activity_type"] for s in first["steps"]], ["summary", "flashcards"])
        self.assertTrue(first["candidate_key"].startswith("mkt|summary+flashcards|"))
        session = self.combined_session()
        self.assertEqual((session["activity_type"], session["duration_minutes"], session["current_step"]), ("summary", 35, 0))
        self.assertEqual([(s["activity_type"], s["status"]) for s in session["steps"]],
                         [("summary", "pending"), ("flashcards", "pending")])
        stored = study_planner_store.get_session(self.alice, session["session_id"])
        self.assertEqual(sum(s["duration_minutes"] for s in stored["steps"]), stored["duration_minutes"])
        # Single-activity sessions are unchanged: no steps.
        singles = [s for s in study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"]) if not s["steps"]]
        self.assertTrue(singles)
        sessions = sorted(study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"]),
                          key=lambda s: s["scheduled_start"])
        for before, after in zip(sessions, sessions[1:]):
            self.assertLessEqual(before["scheduled_end"], after["scheduled_start"])   # never overlapping

    def test_steps_open_their_own_tool_in_order_and_complete_the_session(self):
        session = self.combined_session()
        sid = session["session_id"]
        early = self.step_done(sid, 0)
        self.assertEqual((early.status_code, early.json()["detail"]["code"]), (409, "session_not_started"))
        started = self.start(sid).json()
        self.assertEqual((started["tool"], started["current_step"], started["step_activity_type"]), ("summary", 0, "summary"))
        refused = self.client.post(f"/api/planner/sessions/{sid}/complete")
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "session_steps_incomplete"))
        out_of_order = self.step_done(sid, 1)
        self.assertEqual((out_of_order.status_code, out_of_order.json()["detail"]["code"]), (409, "step_out_of_order"))
        first = self.step_done(sid, 0).json()
        self.assertEqual((first["changed"], first["session_completed"], first["session"]["current_step"]), (True, False, 1))
        self.assertEqual(first["session"]["status"], "in_progress")
        self.assertFalse(self.step_done(sid, 0).json()["changed"])                        # idempotent
        resumed = self.start(sid).json()
        self.assertEqual((resumed["tool"], resumed["current_step"], resumed["started"]), ("flashcards", 1, False))
        last = self.step_done(sid, 1).json()
        self.assertEqual((last["session_completed"], last["session"]["status"], last["session"]["current_step"]),
                         (True, "completed", None))
        self.assertTrue(last["session"]["completed_at"])
        self.assertTrue(all(step["completed_at"] for step in last["session"]["steps"]))
        state = get_document_study_state(self.alice, "mkt")
        self.assertEqual(set(state.completed_activities), {"summary", "flashcards"})   # both count as studied
        self.assertEqual(self.client.post(f"/api/planner/sessions/{sid}/complete").json()["changed"], False)

    def test_a_quiz_step_without_a_quiz_opens_the_create_flow_and_generates_nothing(self):
        record = {"document_id": "mkt", "activity_type": "flashcards", "scheduled_start": "2026-09-28T18:00:00",
                  "scheduled_end": "2026-09-28T18:35:00", "duration_minutes": 35, "reason": "retrieval_practice",
                  "steps": combined.new_steps((("flashcards", 15, None), ("quiz", 20, None)))}
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        self.assertEqual(self.start(session["session_id"]).json()["tool"], "flashcards")
        self.step_done(session["session_id"], 0)
        quiz_step = self.start(session["session_id"]).json()
        self.assertEqual((quiz_step["tool"], quiz_step["artifact_available"], quiz_step["prerequisite"]), ("quiz", False, None))

    def combined_record(self, first, row_artifact, first_artifact, quiz_artifact):
        return {"document_id": "mkt", "activity_type": first, "artifact_id": row_artifact,
                "scheduled_start": "2026-09-28T18:00:00", "scheduled_end": "2026-09-28T18:35:00", "duration_minutes": 35,
                "reason": "retrieval_practice",
                "steps": combined.new_steps(((first, 15, first_artifact), ("quiz", 20, quiz_artifact)))}

    def test_the_quiz_step_never_opens_the_other_steps_artifact_as_its_quiz(self):
        # Planned before the quiz existed: the row's artifact_id is the flashcard set's, the quiz step has none.
        record = self.combined_record("flashcards", "cards-set-1", "cards-set-1", None)
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        sid = session["session_id"]
        first = self.start(sid).json()
        self.assertEqual((first["step_activity_type"], first["step_artifact_id"]), ("flashcards", "cards-set-1"))
        self.step_done(sid, 0)
        quiz_step = self.start(sid).json()
        self.assertEqual((quiz_step["tool"], quiz_step["current_step"], quiz_step["step_activity_type"]), ("quiz", 1, "quiz"))
        self.assertIsNone(quiz_step["step_artifact_id"])
        self.assertEqual(quiz_step["session"]["status"], "in_progress")   # opening the quiz completes nothing

    def test_the_quiz_step_keeps_its_own_planned_quiz(self):
        record = self.combined_record("summary", "quiz-1", None, "quiz-1")
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        self.start(session["session_id"])
        self.step_done(session["session_id"], 0)
        self.assertEqual(self.start(session["session_id"]).json()["step_artifact_id"], "quiz-1")

    def test_document_progress_reports_the_running_sessions_steps(self):
        # The Study Session page restores the current step after a refresh from this row.
        record = self.combined_record("summary", None, None, None)
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        self.start(session["session_id"])
        self.step_done(session["session_id"], 0)
        progress = self.client.get("/api/progress/documents/mkt?utc_offset_minutes=0&local_now=2026-09-28T18:10:00").json()
        running = progress["plan"]["next_session"]
        self.assertEqual((running["session_id"], running["status"]), (session["session_id"], "in_progress"))
        self.assertEqual([(step["activity_type"], step["status"]) for step in running["steps"]],
                         [("summary", "completed"), ("quiz", "pending")])

    def test_single_sessions_keep_the_old_lifecycle(self):
        record = {"document_id": "mkt", "activity_type": "summary", "scheduled_start": "2026-09-28T18:00:00",
                  "scheduled_end": "2026-09-28T18:45:00", "duration_minutes": 45, "reason": "new_material"}
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        self.assertEqual((session["steps"], study_planner_store.get_session(self.alice, session["session_id"])["steps"]), ([], []))
        self.assertEqual(self.start(session["session_id"]).json()["current_step"], None)
        refused = self.step_done(session["session_id"], 0)
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "session_not_combined"))
        done = self.client.post(f"/api/planner/sessions/{session['session_id']}/complete").json()
        self.assertEqual(done["session"]["status"], "completed")

    def test_older_rows_without_the_column_value_read_as_single(self):
        record = {"document_id": "mkt", "activity_type": "quiz", "scheduled_start": "2026-09-29T18:00:00",
                  "scheduled_end": "2026-09-29T18:35:00", "duration_minutes": 35, "reason": "new_material"}
        session = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        with study_planner_store._connect() as connection:
            row = connection.execute("SELECT steps_json FROM study_sessions WHERE session_id=?", (session["session_id"],)).fetchone()
        self.assertIsNone(row["steps_json"])

    def candidates(self):
        return self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/candidates",
                               params={"utc_offset_minutes": 0, "local_now": "2026-09-28T18:10:00"}).json()

    def test_a_finished_step_counts_once_and_is_never_planned_again(self):
        session = self.combined_session()
        sid = session["session_id"]
        self.start(sid)
        self.step_done(sid, 0)
        # Refresh / resume: the saved progress, continuing at the first step not done.
        reloaded = next(s for s in self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/sessions").json()
                        if s["session_id"] == sid)
        self.assertEqual(([s["status"] for s in reloaded["steps"]], reloaded["current_step"], reloaded["status"]),
                         (["completed", "pending"], 1, "in_progress"))
        self.assertEqual(self.start(sid).json()["tool"], "flashcards")   # never the summary again
        state = get_document_study_state(self.alice, "mkt")
        self.assertEqual((state.completed_activities, state.completed_session_count), (("summary",), 0))
        self.assertNotEqual(state.state_reason.code, "no_study_activity")   # the finished step is study activity
        # Neither the finished summary nor the flashcards still to do in this session is suggested elsewhere.
        self.assertFalse({"summary", "flashcards"} & {c["activity_type"] for c in self.candidates()}, self.candidates())
        proposal = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/adaptation/preview", json={
            "trigger": {"kind": "materials_added"}, "utc_offset_minutes": 0, "local_now": "2026-09-28T18:10:00"}).json()
        self.assertEqual(proposal["added"], [])
        # Skipping the rest keeps the finished step: it still counts, once; the session is not completed.
        skipped = self.client.post(f"/api/planner/sessions/{sid}/skip").json()["session"]
        self.assertEqual((skipped["status"], [s["status"] for s in skipped["steps"]]), ("skipped", ["completed", "pending"]))
        self.assertEqual(get_document_study_state(self.alice, "mkt").completed_activities, ("summary",))

    def test_reschedule_keeps_steps_and_progress(self):
        session = self.combined_session()
        response = self.client.post(f"/api/planner/sessions/{session['session_id']}/reschedule",
                                    json=self.body(target_start="2026-09-29T19:00:00", duration_minutes=40))
        self.assertEqual(response.status_code, 200, response.text)
        moved = response.json()["session"]
        self.assertEqual([s["activity_type"] for s in moved["steps"]], ["summary", "flashcards"])
        self.assertEqual((moved["duration_minutes"], sum(s["duration_minutes"] for s in moved["steps"])), (40, 40))

    def test_live_candidates_and_adaptation_see_the_combined_session(self):
        self.confirm()
        live = self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/candidates",
                               params={"utc_offset_minutes": 0, "local_now": NOW}).json()
        self.assertNotIn("summary+flashcards", [c["candidate_key"].split("|")[1] for c in live])   # already planned
        self.assertNotIn("summary", [c["activity_type"] for c in live])
        sessions = study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"])
        candidates = study_placement.live_candidates(
            context([material(get_document_study_state(self.alice, "mkt"))], every_day(), now=MONDAY), sessions,
            PLANNER_CONFIG)
        self.assertEqual(candidates, [])
        proposal = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/adaptation/preview", json={
            "trigger": {"kind": "availability_changed"}, "utc_offset_minutes": 0, "local_now": NOW}).json()
        self.assertEqual((proposal["added"], proposal["cancelled"]), ([], []))

    def test_an_older_single_activity_plan_keeps_single_suggestions(self):
        # Accepted before combined sessions existed: a summary session only. The flashcards practice
        # still wanted is suggested on its own, and a missed summary gets a single replacement.
        record = {"document_id": "mkt", "activity_type": "summary", "scheduled_start": "2026-09-27T18:00:00",
                  "scheduled_end": "2026-09-27T18:45:00", "duration_minutes": 45, "reason": "new_material"}
        missed = study_planner_store.create_session(self.alice, self.plan["plan_id"], record)
        live = self.client.get(f"/api/planner/plans/{self.plan['plan_id']}/candidates",
                               params={"utc_offset_minutes": 0, "local_now": NOW}).json()
        self.assertTrue(live)
        self.assertEqual([c["steps"] for c in live], [[] for _ in live])
        proposal = self.client.post(f"/api/planner/plans/{self.plan['plan_id']}/adaptation/preview", json={
            "trigger": {"kind": "session_missed", "session_id": missed["session_id"]}, "utc_offset_minutes": 0,
            "local_now": NOW}).json()
        self.assertEqual([(a["activity_type"], a["steps"], a["replaces_session_id"]) for a in proposal["added"]],
                         [("summary", [], missed["session_id"])])

    def test_matching_rules(self):
        steps = combined.new_steps((("summary", 20, None), ("flashcards", 15, None)))
        session = {"activity_type": "summary", "steps": steps, "reason": "new_material"}
        step = next(s for s in study_adaptation._build_steps(
            material(make_state("d", summary=True, cards=10)), MONDAY.date(), MONDAY.date().replace(day=30), None,
            PLANNER_CONFIG) if s.parts)
        self.assertTrue(study_adaptation._matches(step, session))
        self.assertFalse(study_adaptation._matches(step, {"activity_type": "summary", "steps": [], "reason": "new_material"}))

    def test_google_event_title_names_both_steps(self):
        session = self.combined_session()
        stored = study_planner_store.get_session(self.alice, session["session_id"])
        body = google_calendar_sync.event_body(stored, "Marketing", google_calendar_sync.timedelta(0))
        self.assertEqual(body["summary"], "Marketing · Summary + Flashcards")
        self.assertEqual(body["extendedProperties"]["private"]["session_id"], stored["session_id"])   # one event per session
        single = {**stored, "steps": []}
        self.assertEqual(google_calendar_sync.event_body(single, "Marketing", google_calendar_sync.timedelta(0))["summary"],
                         "Marketing · Summary")
        json.dumps(body)   # serializable as sent to Google


if __name__ == "__main__":
    unittest.main()
