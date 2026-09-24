"""Study Planner Phase 5B1: POST /api/planner/plans/{plan_id}/adaptation/apply -- the server
recomputes the proposal and applies it atomically; large proposals need confirm=true; the new
'cancelled' status keeps withdrawn recommendations as history without blocking time.

Learner-local now: Thu 2026-09-24 12:00 (offset 0); available every day 18:00-21:00 unless noted."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from planner_fixtures import PlannerDatabaseMixin
from backend import study_plan_api_service, study_planner_store
from backend.main import app

PASSWORD = "long-password-x"
NOW = "2026-09-24T12:00:00"


class StudyAdaptationApplyTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice, self.bob = self.user("Alice"), self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.add_document(self.bob, "bob-doc", "Bob's notes")
        self.client = self.login("alice")
        self.plan = study_planner_store.create_plan(self.alice, "Exams")
        for document_id in ("mkt", "stats"):
            study_planner_store.add_material(self.alice, self.plan["plan_id"], document_id)
        self.evenings()

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def evenings(self, start="18:00", end="21:00"):
        for day in range(7):
            self.client.post("/api/planner/availability", json={"start_at": start, "end_at": end,
                                                                 "is_recurring": True, "day_of_week": day})

    def session(self, activity, start, end, minutes, document_id="mkt", reason="new_material", status=None):
        row = study_planner_store.create_session(self.alice, self.plan["plan_id"], {
            "document_id": document_id, "activity_type": activity, "scheduled_start": start, "scheduled_end": end,
            "duration_minutes": minutes, "reason": reason})
        if status:
            row = study_planner_store.update_session(self.alice, row["session_id"], {"status": status})
        return row

    def apply(self, trigger, confirm=False, client=None, plan_id=None, **extra):
        return (client or self.client).post(
            f"/api/planner/plans/{plan_id or self.plan['plan_id']}/adaptation/apply",
            json={"trigger": trigger, "utc_offset_minutes": 0, "local_now": NOW, "confirm": confirm, **extra})

    def rows(self):
        return {s["session_id"]: s for s in study_planner_store.list_sessions(self.alice)}

    def snapshot(self):
        return sorted((s["session_id"], s["status"], s["scheduled_start"], s["updated_at"]) for s in self.rows().values())

    def assert_no_overlap(self):
        busy = sorted(study_planner_store.list_busy_sessions(self.alice), key=lambda s: s["scheduled_start"])
        for before, after in zip(busy, busy[1:]):
            self.assertLessEqual(before["scheduled_end"], after["scheduled_start"], (before, after))

    def high_score_plan(self):
        """mkt scored 90%: a review tomorrow is too early and the retry is no longer needed."""
        self.add_summary(self.alice, "mkt")
        self.add_flashcards(self.alice, "mkt", 10)
        self.add_quiz(self.alice, "mkt", "q1")
        self.add_attempt(self.alice, "mkt", "q1", score=9, answered=10, completed=True,
                         completed_at="2026-09-24T10:00:00+00:00")
        done = self.session("quiz", "2026-09-24T10:00:00", "2026-09-24T10:30:00", 30, status="in_progress")
        done = study_planner_store.update_session(self.alice, done["session_id"], {"status": "completed"})
        review = self.session("review", "2026-09-25T18:00:00", "2026-09-25T18:10:00", 10, reason="review_due")
        retry = self.session("quiz_retry", "2026-09-27T18:00:00", "2026-09-27T18:20:00", 20, reason="review_due")
        return done, review, retry

    # -- small apply, lineage, cancellation history ---------------------------------------------

    def test_small_proposal_applies_with_move_lineage_and_cancellation_history(self):
        done, review, retry = self.high_score_plan()
        other = self.session("summary", "2026-09-26T18:00:00", "2026-09-26T18:45:00", 45, document_id="stats")
        before_done, before_other = self.rows()[done["session_id"]], self.rows()[other["session_id"]]
        response = self.apply({"kind": "quiz_completed", "document_id": "mkt"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["applied"], body["significance"], body["requires_confirmation"]), (True, "small", False))
        self.assertEqual([(m["session_id"], m["to_start"]) for m in body["moved"]],
                         [(review["session_id"], "2026-09-29T18:00:00")])
        self.assertEqual([c["session_id"] for c in body["cancelled"]], [retry["session_id"]])
        rows = self.rows()
        # Move lineage: the original is kept as 'rescheduled'; the replacement points back to it.
        new_id = body["moved"][0]["new_session_id"]
        self.assertEqual(rows[review["session_id"]]["status"], "rescheduled")
        self.assertEqual((rows[new_id]["status"], rows[new_id]["rescheduled_from"], rows[new_id]["scheduled_start"],
                          rows[new_id]["reason"], rows[new_id]["activity_type"]),
                         ("scheduled", review["session_id"], "2026-09-29T18:00:00", "review_due", "review"))
        # Cancellation history: the row stays, only its status changes.
        self.assertEqual((rows[retry["session_id"]]["status"], rows[retry["session_id"]]["scheduled_start"]),
                         ("cancelled", "2026-09-27T18:00:00"))
        # History and unrelated sessions are untouched.
        self.assertEqual(rows[done["session_id"]], before_done)
        self.assertEqual(rows[other["session_id"]], before_other)
        self.assertEqual({s["session_id"] for s in body["sessions"]},
                         {review["session_id"], new_id, retry["session_id"]})
        self.assert_no_overlap()

    def test_duplicate_apply_creates_no_extra_changes(self):
        self.high_score_plan()
        self.assertTrue(self.apply({"kind": "quiz_completed", "document_id": "mkt"}).json()["applied"])
        before = self.snapshot()
        again = self.apply({"kind": "quiz_completed", "document_id": "mkt"}).json()
        self.assertEqual((again["applied"], again["change_count"]), (False, 0))
        self.assertEqual(self.snapshot(), before)

    def test_missed_replacement_is_applied_once(self):
        missed = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45)
        body = self.apply({"kind": "session_missed", "session_id": missed["session_id"]}).json()
        self.assertTrue(body["applied"])
        rows = self.rows()
        replacement = rows[body["added"][0]["session_id"]]
        self.assertEqual((replacement["scheduled_start"], replacement["rescheduled_from"], replacement["reason"]),
                         ("2026-09-24T18:00:00", missed["session_id"], "rescheduled"))
        self.assertEqual(rows[missed["session_id"]]["status"], "rescheduled")   # no longer "not completed"
        before = self.snapshot()
        again = self.apply({"kind": "session_missed", "session_id": missed["session_id"]}).json()
        self.assertEqual((again["applied"], again["change_count"]), (False, 0))
        self.assertEqual(self.snapshot(), before)

    # -- large needs confirmation ------------------------------------------------------------------

    def test_large_proposal_requires_confirmation(self):
        for document_id, day in (("mkt", "26"), ("stats", "27")):
            self.session("summary", f"2026-09-{day}T10:00:00", f"2026-09-{day}T10:45:00", 45, document_id=document_id)
        self.session("flashcards", "2026-09-26T11:00:00", "2026-09-26T11:20:00", 20, document_id="stats")
        before = self.snapshot()
        proposal = self.apply({"kind": "availability_changed"}).json()
        self.assertEqual((proposal["applied"], proposal["significance"], proposal["requires_confirmation"]),
                         (False, "large", True))
        self.assertEqual(len(proposal["moved"]), 3)
        self.assertEqual(self.snapshot(), before)   # nothing written without confirm
        confirmed = self.apply({"kind": "availability_changed"}, confirm=True).json()
        self.assertTrue(confirmed["applied"])
        self.assertEqual(sum(s["status"] == "rescheduled" for s in self.rows().values()), 3)
        self.assert_no_overlap()

    # -- deadline --------------------------------------------------------------------------------

    def test_deadline_apply_keeps_everything_before_the_deadline(self):
        self.add_summary(self.alice, "mkt")
        self.add_flashcards(self.alice, "mkt", 10)
        quiz = self.session("quiz", "2026-09-29T18:00:00", "2026-09-29T18:30:00", 30)
        material = study_planner_store.get_plan_material(self.alice, self.plan["plan_id"], "mkt")
        study_planner_store.update_material(self.alice, material["material_id"], {"deadline": "2026-09-28"})
        body = self.apply({"kind": "deadline_changed", "document_id": "mkt"}).json()
        self.assertTrue(body["applied"])
        active = [s for s in self.rows().values() if s["status"] == "scheduled" and s["document_id"] == "mkt"]
        self.assertTrue(active)
        self.assertTrue(all(s["scheduled_start"][:10] < "2026-09-28" for s in active), active)
        self.assertEqual(self.rows()[quiz["session_id"]]["status"], "rescheduled")
        self.assert_no_overlap()

    # -- stale state, atomicity ------------------------------------------------------------------

    def test_stale_state_is_a_conflict_with_no_writes(self):
        self.high_score_plan()
        real = study_plan_api_service._compute_adaptation
        unrelated = self.session("summary", "2026-09-30T18:00:00", "2026-09-30T18:45:00", 45, document_id="stats")

        def compute_then_change(*args, **kwargs):
            result = real(*args, **kwargs)
            study_planner_store.update_session(self.alice, unrelated["session_id"], {"scheduled_start": "2026-09-30T19:00:00",
                                                                                     "scheduled_end": "2026-09-30T19:45:00"})
            return result

        with patch.object(study_plan_api_service, "_compute_adaptation", side_effect=compute_then_change):
            response = self.apply({"kind": "quiz_completed", "document_id": "mkt"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "stale_plan"))
        # Zero partial writes: nothing rescheduled or cancelled, no replacement inserted.
        self.assertEqual({s["status"] for s in self.rows().values()}, {"completed", "scheduled"})
        self.assertEqual(len(self.rows()), 4)

    def test_failure_mid_apply_rolls_everything_back(self):
        self.high_score_plan()
        before = self.snapshot()
        calls = {"n": 0}
        real_uuid = study_planner_store.uuid4

        def failing_uuid():
            calls["n"] += 1
            if calls["n"] == 1:
                return real_uuid()
            raise RuntimeError("disk full")

        # The move writes 'rescheduled' + inserts a replacement; cancelling comes after. Make the
        # second row id fail: nothing at all may persist.
        with patch.object(study_planner_store, "uuid4", side_effect=failing_uuid):
            with self.assertRaises(RuntimeError):
                study_planner_store.apply_adaptation_changes(
                    self.alice, self.plan["plan_id"],
                    study_planner_store.plan_sessions_fingerprint(study_planner_store.list_sessions(self.alice, plan_id=self.plan["plan_id"])),
                    NOW, added=[{"document_id": "stats", "activity_type": "quiz", "scheduled_start": "2026-09-30T18:00:00",
                                 "scheduled_end": "2026-09-30T18:30:00", "duration_minutes": 30, "reason": "new_material"}],
                    moved=[(s["session_id"], "2026-09-29T18:00:00", "2026-09-29T18:10:00")
                           for s in self.rows().values() if s["activity_type"] == "review"],
                    cancelled=[], replaced=[])
        self.assertEqual(self.snapshot(), before)

    # -- isolation, inactive plan, client-supplied changes ---------------------------------------

    def test_ownership_and_plan_isolation(self):
        missed = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45)
        before = self.snapshot()
        bob = self.login("bob")
        self.assertEqual(self.apply({"kind": "availability_changed"}, client=bob).status_code, 404)
        bob_plan = study_planner_store.create_plan(self.bob, "Bob")
        study_planner_store.add_material(self.bob, bob_plan["plan_id"], "bob-doc")
        self.assertEqual(self.apply({"kind": "session_missed", "session_id": missed["session_id"]},
                                    client=bob, plan_id=bob_plan["plan_id"]).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_inactive_plan_is_rejected(self):
        missed = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45)
        study_planner_store.update_plan(self.alice, self.plan["plan_id"], {"status": "archived"})
        response = self.apply({"kind": "session_missed", "session_id": missed["session_id"]})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "plan_not_active"))
        self.assertEqual(self.rows()[missed["session_id"]]["status"], "scheduled")

    def test_client_supplied_changes_are_refused(self):
        response = self.apply({"kind": "availability_changed"},
                              added=[{"document_id": "mkt", "activity_type": "quiz", "scheduled_start": "2026-09-25T18:00:00"}])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.rows(), {})

    # -- the cancelled status --------------------------------------------------------------------

    def test_cancelled_is_terminal_and_frees_its_time(self):
        _, _, retry = self.high_score_plan()
        self.apply({"kind": "quiz_completed", "document_id": "mkt"})
        self.assertEqual(self.rows()[retry["session_id"]]["status"], "cancelled")
        for action in ("start", "complete", "skip"):
            response = self.client.post(f"/api/planner/sessions/{retry['session_id']}/{action}",
                                        json={"utc_offset_minutes": 0, "local_now": NOW})
            self.assertEqual(response.status_code, 409, action)
        response = self.client.post(f"/api/planner/sessions/{retry['session_id']}/reschedule",
                                    json={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "session_not_reschedulable"))
        # Not busy time: another session can take exactly that window.
        self.assertNotIn(retry["session_id"], {s["session_id"] for s in study_planner_store.list_busy_sessions(self.alice)})
        other = self.session("summary", "2026-09-24T09:00:00", "2026-09-24T09:20:00", 20, document_id="stats")
        _, moved = study_planner_store.reschedule_session(self.alice, other["session_id"], "2026-09-27T18:00:00",
                                                          "2026-09-27T18:20:00")
        self.assertEqual(moved["scheduled_start"], "2026-09-27T18:00:00")
        # ...and it is not active plan work.
        self.assertNotIn("cancelled", study_planner_store.ACTIVE_SESSION_STATUSES)
        self.assertEqual(study_planner_store.count_active_plan_sessions(self.alice, self.plan["plan_id"]),
                         sum(s["status"] == "scheduled" for s in self.rows().values()))


if __name__ == "__main__":
    unittest.main()
