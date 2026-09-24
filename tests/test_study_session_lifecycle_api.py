"""Study Planner Phase 4C: Complete / Skip / Reschedule on exactly one owned session.

Clock: the learner's local "now" is sent explicitly (Thu 2026-09-24 12:00, UTC offset 0), the same
way the preview/confirm endpoints take it. 2026-09-24 is a Thursday (weekday 3)."""

import unittest

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from backend import study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-24T12:00:00"
MON, THU, FRI = 0, 3, 4


class StudySessionLifecycleApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = self.login("alice")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        study_planner_store.add_material(self.alice, self.plan["plan_id"], "mkt")
        study_planner_store.add_material(self.alice, self.plan["plan_id"], "stats")

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def session(self, document_id="mkt", start="2026-09-24T10:00:00", end="2026-09-24T10:30:00",
                activity="summary", minutes=30, reason="deadline_approaching", artifact_id=None):
        return study_planner_store.create_session(self.alice, self.plan["plan_id"], {
            "document_id": document_id, "activity_type": activity, "scheduled_start": start, "scheduled_end": end,
            "duration_minutes": minutes, "reason": reason, "artifact_id": artifact_id, "priority_snapshot": 0.7})

    def weekly(self, day, start, end):
        response = self.client.post("/api/planner/availability", json={
            "start_at": start, "end_at": end, "is_recurring": True, "day_of_week": day})
        self.assertEqual(response.status_code, 200, response.text)

    def act(self, action, session_id, client=None, local_now=NOW):
        # Start takes the learner's clock (a missed session cannot be started); the others take no body.
        body = {"utc_offset_minutes": 0, "local_now": local_now} if action == "start" else None
        return (client or self.client).post(f"/api/planner/sessions/{session_id}/{action}", json=body)

    def reschedule(self, session_id, client=None, local_now=NOW):
        return (client or self.client).post(f"/api/planner/sessions/{session_id}/reschedule",
                                            json={"utc_offset_minutes": 0, "local_now": local_now})

    def stored(self, session_id):
        return study_planner_store.get_session(self.alice, session_id)

    def archive(self):
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})

    # -- ownership ------------------------------------------------------------------

    def test_owner_isolation_for_every_action(self):
        mine = self.session()
        bob = self.login("bob")
        for action in ("complete", "skip"):
            self.assertEqual(self.act(action, mine["session_id"], client=bob).status_code, 404)
            self.assertEqual(self.act(action, "missing").status_code, 404)
        self.assertEqual(self.reschedule(mine["session_id"], client=bob).status_code, 404)
        self.assertEqual(self.reschedule("missing").status_code, 404)
        self.assertEqual(self.stored(mine["session_id"])["status"], "scheduled")

    # -- complete -------------------------------------------------------------------

    def test_complete_lifecycle_timestamps_and_idempotency(self):
        session, other = self.session(), self.session("stats", "2026-09-24T14:00:00", "2026-09-24T14:30:00")
        early = self.act("complete", session["session_id"])
        self.assertEqual((early.status_code, early.json()["detail"]["code"]), (409, "session_not_started"))
        started = self.act("start", session["session_id"], local_now="2026-09-24T10:05:00").json()["session"]   # during its window
        done = self.act("complete", session["session_id"])
        self.assertEqual(done.status_code, 200, done.text)
        body = done.json()
        self.assertEqual((body["changed"], body["session"]["status"]), (True, "completed"))
        self.assertEqual(body["session"]["started_at"], started["started_at"])
        self.assertTrue(body["session"]["completed_at"])
        again = self.act("complete", session["session_id"]).json()
        self.assertEqual((again["changed"], again["session"]["completed_at"]), (False, body["session"]["completed_at"]))
        self.assertEqual(self.stored(other["session_id"])["status"], "scheduled")   # exactly one session
        # A completed session is history: it cannot be skipped, restarted or rescheduled.
        for response in (self.act("skip", session["session_id"]), self.act("start", session["session_id"]),
                         self.reschedule(session["session_id"])):
            self.assertEqual(response.status_code, 409)

    def test_skipped_session_cannot_be_completed(self):
        session = self.session()
        self.act("skip", session["session_id"])
        response = self.act("complete", session["session_id"])
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "session_not_completable"))

    # -- skip -----------------------------------------------------------------------

    def test_skip_keeps_history_and_is_idempotent(self):
        scheduled, started = self.session(), self.session("stats", "2026-09-24T14:00:00", "2026-09-24T14:30:00")
        self.act("start", started["session_id"])
        for row in (scheduled, started):
            body = self.act("skip", row["session_id"]).json()
            self.assertEqual((body["changed"], body["session"]["status"]), (True, "skipped"))
            self.assertFalse(self.act("skip", row["session_id"]).json()["changed"])
        self.assertEqual({s["status"] for s in study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"])},
                         {"skipped"})   # kept, not deleted

    # -- archived plans -------------------------------------------------------------

    def test_archived_plan_rules(self):
        scheduled, started = self.session(), self.session("stats", "2026-09-24T14:00:00", "2026-09-24T14:30:00")
        self.act("start", started["session_id"])
        self.weekly(THU, "18:00", "20:00")
        self.archive()
        for response in (self.act("skip", scheduled["session_id"]), self.reschedule(scheduled["session_id"])):
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "plan_not_active"))
        self.assertEqual(self.stored(scheduled["session_id"])["status"], "scheduled")
        self.assertEqual(self.act("complete", started["session_id"]).json()["session"]["status"], "completed")

    # -- reschedule -----------------------------------------------------------------

    def test_reschedule_moves_to_the_next_usable_slot_without_overlap(self):
        overdue = self.session(artifact_id="quiz-7")
        busy = self.session("stats", "2026-09-24T18:00:00", "2026-09-24T18:45:00")
        self.weekly(THU, "18:00", "20:00")
        response = self.reschedule(overdue["session_id"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        moved = body["session"]
        # 18:00-18:45 is busy (+10 min gap) -> 18:55.
        self.assertEqual((moved["scheduled_start"], moved["scheduled_end"], moved["duration_minutes"]),
                         ("2026-09-24T18:55:00", "2026-09-24T19:25:00", 30))
        self.assertEqual((moved["status"], moved["rescheduled_from"], body["after_deadline"]),
                         ("scheduled", overdue["session_id"], False))
        # Lineage: same document / activity / artifact / reason.
        self.assertEqual((moved["document_id"], moved["activity_type"], moved["artifact_id"], moved["reason"]["code"]),
                         ("mkt", "summary", "quiz-7", "deadline_approaching"))
        self.assertEqual(body["previous"]["status"], "rescheduled")
        self.assertEqual(self.stored(overdue["session_id"])["scheduled_start"], "2026-09-24T10:00:00")   # history kept
        self.assertEqual(self.stored(busy["session_id"])["status"], "scheduled")
        new = self.stored(moved["session_id"])
        self.assertEqual(new["priority_snapshot"], 0.7)
        # The replacement is current work: it can be started; the old row cannot be moved again.
        self.assertEqual(self.act("start", moved["session_id"]).status_code, 200)
        again = self.reschedule(overdue["session_id"])
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "session_not_reschedulable"))

    def test_reschedule_never_lands_in_the_past_or_before_a_future_session_ends(self):
        future = self.session(start="2026-09-24T18:00:00", end="2026-09-24T18:30:00")
        self.weekly(THU, "09:00", "20:00")
        moved = self.reschedule(future["session_id"]).json()["session"]
        self.assertGreaterEqual(moved["scheduled_start"], "2026-09-24T18:30:00")
        overdue = self.session("stats")
        moved = self.reschedule(overdue["session_id"], local_now="2026-09-24T12:07:00").json()["session"]
        self.assertGreaterEqual(moved["scheduled_start"], "2026-09-24T12:30:00")   # the current half hour is spent

    def test_deadline_rule(self):
        study_planner_store.update_material(self.alice, study_planner_store.get_plan_material(
            self.alice, self.plan["plan_id"], "mkt")["material_id"], {"deadline": "2026-09-26"})
        first = self.session()
        self.weekly(MON, "18:00", "19:00")
        self.weekly(FRI, "18:00", "19:00")
        body = self.reschedule(first["session_id"]).json()
        self.assertEqual((body["session"]["scheduled_start"], body["after_deadline"]), ("2026-09-25T18:00:00", False))
        # Friday is now taken (with its gap); nothing else fits by Saturday -> after the deadline, flagged.
        second = self.session(start="2026-09-24T10:40:00", end="2026-09-24T11:10:00")
        body = self.reschedule(second["session_id"]).json()
        self.assertEqual((body["session"]["scheduled_start"], body["after_deadline"]), ("2026-09-28T18:00:00", True))

    def test_no_available_slot_is_a_clear_conflict(self):
        session = self.session()
        response = self.reschedule(session["session_id"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "no_available_slot")
        self.assertIn("Add availability", response.json()["detail"]["message"])
        self.assertEqual(self.stored(session["session_id"])["status"], "scheduled")
        self.assertEqual(len(study_planner_store.list_sessions(self.alice)), 1)

    def test_in_progress_session_is_not_rescheduled(self):
        session = self.session()
        self.act("start", session["session_id"], local_now="2026-09-24T10:05:00")   # started during its window
        self.weekly(THU, "18:00", "20:00")
        response = self.reschedule(session["session_id"])
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "session_not_reschedulable"))

    def test_store_refuses_an_overlapping_window(self):
        session = self.session()
        self.session("stats", "2026-09-24T18:00:00", "2026-09-24T18:45:00")
        with self.assertRaises(study_planner_store.SessionTransitionError) as caught:
            study_planner_store.reschedule_session(self.alice, session["session_id"], "2026-09-24T18:30:00",
                                                   "2026-09-24T19:00:00")
        self.assertEqual(caught.exception.code, "slot_taken")
        self.assertEqual(self.stored(session["session_id"])["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
