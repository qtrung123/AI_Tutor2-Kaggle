"""Study Planner v2 HTTP API: plans, plan materials, and the read-only schedule preview."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_plan_api_service, study_planner_store
from backend.main import app
from backend.study_planner_store import SESSION_REASONS

# "Real" UTC clock for these tests: Mon 2026-09-28 01:00 UTC (08:00 at UTC+7).
UTC_NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
MONDAY_LOCAL = "2026-09-28T08:00:00"
PASSWORD = "long-password-x"


class StudyPlanApiTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        clock = patch.object(study_plan_api_service, "_utc_now", return_value=UTC_NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.alice = self.user("Alice")
        self.bob = self.user("Bob")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.add_document(self.alice, "pbi", "PowerBI")
        self.add_document(self.bob, "bob-doc", "Bob's notes")
        self.client = self.login("alice")

    def login(self, name):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": f"{name}-planner@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def create_plan(self, client=None, title="Exams"):
        response = (client or self.client).post("/api/planner/plans", json={"title": title})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add(self, plan_id, document_id, client=None, **fields):
        return (client or self.client).post(f"/api/planner/plans/{plan_id}/materials",
                                            json={"document_id": document_id, **fields})

    def weekly(self, *slots, client=None):
        for day, start, end in slots:
            response = (client or self.client).post("/api/planner/availability", json={
                "start_at": start, "end_at": end, "is_recurring": True, "day_of_week": day})
            self.assertEqual(response.status_code, 200, response.text)

    def preview(self, plan_id, client=None, **body):
        return (client or self.client).post(f"/api/planner/plans/{plan_id}/preview",
                                            json={"utc_offset_minutes": 0, "local_now": MONDAY_LOCAL, **body})

    # -- plans -----------------------------------------------------------------

    def test_plan_crud(self):
        plan = self.create_plan()
        self.assertEqual((plan["title"], plan["status"]), ("Exams", "active"))
        self.assertEqual([p["plan_id"] for p in self.client.get("/api/planner/plans").json()], [plan["plan_id"]])
        detail = self.client.get(f"/api/planner/plans/{plan['plan_id']}").json()
        self.assertEqual((detail["title"], detail["materials"]), ("Exams", []))

        updated = self.client.patch(f"/api/planner/plans/{plan['plan_id']}", json={"title": "Finals", "status": "paused"})
        self.assertEqual((updated.status_code, updated.json()["title"], updated.json()["status"]), (200, "Finals", "paused"))
        self.assertEqual(self.client.patch(f"/api/planner/plans/{plan['plan_id']}", json={"status": "bogus"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/planner/plans/{plan['plan_id']}", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/planner/plans", json={"title": ""}).status_code, 422)

        self.assertEqual(self.client.delete(f"/api/planner/plans/{plan['plan_id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/planner/plans/{plan['plan_id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/planner/plans/{plan['plan_id']}").status_code, 404)

    def test_ownership_and_isolation(self):
        plan = self.create_plan()
        material = self.add(plan["plan_id"], "mkt").json()
        bob = self.login("bob")
        base = f"/api/planner/plans/{plan['plan_id']}"
        self.assertEqual(bob.get("/api/planner/plans").json(), [])
        for method, url, body in (
            ("get", base, None), ("patch", base, {"title": "Stolen"}), ("delete", base, None),
            ("get", f"{base}/materials", None), ("post", f"{base}/materials", {"document_id": "bob-doc"}),
            ("patch", f"{base}/materials/{material['material_id']}", {"deadline": None}),
            ("delete", f"{base}/materials/{material['material_id']}", None),
            ("post", f"{base}/preview", {"utc_offset_minutes": 0}),
        ):
            response = getattr(bob, method)(url, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 404, (method, url, response.text))
        # Bob cannot add Alice's document to his own plan, nor Alice Bob's.
        bobs_plan = self.create_plan(bob)
        self.assertEqual(self.add(bobs_plan["plan_id"], "mkt", client=bob).status_code, 404)
        self.assertEqual(self.add(plan["plan_id"], "bob-doc").status_code, 404)
        self.assertEqual(self.add(plan["plan_id"], "no-such-doc").status_code, 404)
        # A material id from another plan is not addressable through this plan.
        other = self.create_plan(title="Other")
        self.assertEqual(self.client.delete(f"/api/planner/plans/{other['plan_id']}/materials/{material['material_id']}").status_code, 404)
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/planner/plans").status_code, 401)
        self.assertEqual(self.client.get(base).status_code, 200)

    # -- materials -------------------------------------------------------------

    def test_material_crud_with_optional_deadlines(self):
        plan = self.create_plan()
        base = f"/api/planner/plans/{plan['plan_id']}/materials"
        undated = self.add(plan["plan_id"], "mkt")
        dated = self.add(plan["plan_id"], "stats", deadline="2026-10-05", familiarity="somewhat_familiar")
        self.assertEqual((undated.status_code, dated.status_code), (201, 201))
        self.assertEqual((undated.json()["deadline"], undated.json()["document_title"]), (None, "Marketing"))
        self.assertEqual((dated.json()["deadline"], dated.json()["familiarity"]), ("2026-10-05", "somewhat_familiar"))
        self.assertNotIn("topic_id", dated.json())
        self.assertEqual(self.add(plan["plan_id"], "mkt").status_code, 409)                        # duplicate
        self.assertEqual(self.add(plan["plan_id"], "pbi", deadline="soon").status_code, 400)        # invalid
        self.assertEqual(self.add(plan["plan_id"], "pbi", deadline="2026-02-30").status_code, 400)  # not a date
        self.assertEqual(self.add(plan["plan_id"], "pbi", familiarity="expert").status_code, 400)
        self.assertEqual(self.add(plan["plan_id"], "pbi", topic_id="t1").status_code, 422)          # never topics
        # Only the format is validated here -- "already passed" needs the learner's local date,
        # which only preview knows. No timezone heuristic.
        self.assertEqual(self.add(plan["plan_id"], "pbi", deadline="2020-01-01").status_code, 201)

        material_id = dated.json()["material_id"]
        changed = self.client.patch(f"{base}/{material_id}", json={"deadline": "2026-10-08"}).json()
        self.assertEqual((changed["deadline"], changed["familiarity"]), ("2026-10-08", "somewhat_familiar"))
        cleared = self.client.patch(f"{base}/{material_id}", json={"deadline": None}).json()
        self.assertEqual((cleared["deadline"], cleared["familiarity"]), (None, "somewhat_familiar"))
        self.assertEqual(self.client.patch(f"{base}/{material_id}", json={"familiarity": None}).json()["familiarity"], None)
        self.assertEqual(self.client.patch(f"{base}/{material_id}", json={"deadline": "2026-13-01"}).status_code, 400)
        self.assertEqual(self.client.patch(f"{base}/{material_id}", json={"deadline": "2020-01-01"}).status_code, 200)
        self.assertEqual(self.client.patch(f"{base}/{material_id}", json={"topic_id": "t1"}).status_code, 422)
        self.assertEqual(self.client.patch(f"{base}/{material_id}", json={}).status_code, 400)

        listed = self.client.get(base).json()
        self.assertEqual([m["document_id"] for m in listed], ["mkt", "stats", "pbi"])
        self.assertEqual(self.client.delete(f"{base}/{material_id}").status_code, 200)
        self.assertEqual([m["document_id"] for m in self.client.get(base).json()], ["mkt", "pbi"])
        self.assertEqual(self.client.delete(f"{base}/{material_id}").status_code, 404)

    # -- preview -----------------------------------------------------------------

    def test_preview_errors(self):
        plan = self.create_plan()
        empty = self.preview(plan["plan_id"])
        self.assertEqual((empty.status_code, empty.json()["detail"]["code"]), (400, "no_materials"))
        self.add(plan["plan_id"], "mkt")
        self.assertEqual(self.client.post(f"/api/planner/plans/{plan['plan_id']}/preview", json={}).status_code, 422)
        for offset in (900, -780, 7, 400):  # out of range / not a whole quarter hour
            response = self.preview(plan["plan_id"], utc_offset_minutes=offset)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "invalid_utc_offset"), offset)
        for offset in (-720, -210, 0, 330, 345, 765, 840):  # real civil offsets incl. +5:30, +5:45, +12:45
            self.assertEqual(self.preview(plan["plan_id"], utc_offset_minutes=offset).status_code, 200, offset)
        for bad in ("nonsense", "2026-02-30T08:00:00", "2026-09-28T08:00:00+07:00", "2026-09-28T01:00:00Z"):
            response = self.preview(plan["plan_id"], local_now=bad)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "invalid_local_now"), bad)
        self.assertEqual(self.preview("no-such-plan").status_code, 404)

    def test_preview_does_not_persist_and_is_deterministic(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-01")
        self.weekly((0, "18:00", "22:00"), (1, "20:00", "21:00"))
        first, second = self.preview(plan["plan_id"]), self.preview(plan["plan_id"])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertFalse(first.json()["persisted"])
        self.assertTrue(first.json()["sessions"])
        self.assertEqual(study_planner_store.list_sessions(self.alice), [])
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan["plan_id"]), [])

    def test_preview_timezone_offset_is_passed_through(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "stats")
        self.add_flashcards(self.alice, "stats", 20)
        self.add_quiz(self.alice, "stats", "q1")
        # 40% at 20:30 UTC Sunday: Monday 03:30 at UTC+7 (review due Tue), Sunday at UTC (due Mon).
        self.add_attempt(self.alice, "stats", "q1", score=4, answered=10, completed=True,
                         completed_at="2026-09-27T20:30:00+00:00")
        self.weekly(*((day, "09:00", "22:00") for day in range(7)))
        at_utc = self.preview(plan["plan_id"], utc_offset_minutes=0).json()
        at_plus_7 = self.preview(plan["plan_id"], utc_offset_minutes=420).json()
        self.assertEqual(at_utc["sessions"][0]["scheduled_start"][:10], "2026-09-28")
        self.assertEqual(at_plus_7["sessions"][0]["scheduled_start"][:10], "2026-09-29")
        self.assertEqual((at_plus_7["utc_offset_minutes"], at_plus_7["local_now"]), (420, MONDAY_LOCAL))
        # Without local_now, "now" is the real UTC clock shifted by the learner's offset.
        derived = self.client.post(f"/api/planner/plans/{plan['plan_id']}/preview", json={"utc_offset_minutes": -300}).json()
        self.assertEqual(derived["local_now"], "2026-09-27T20:00:00")

    def test_preview_without_availability_is_an_at_risk_result(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-05")
        response = self.preview(plan["plan_id"])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["sessions"], [])
        self.assertIn({"code": "no_availability"}, body["warnings"])
        capacity = body["capacity"]
        self.assertEqual((capacity["status"], capacity["scheduled_minutes"], capacity["schedulable_minutes"]),
                         ("at_risk", 0, 0))
        self.assertEqual(capacity["shortfall_minutes"], capacity["required_minutes"])
        self.assertTrue(capacity["unscheduled"])

    def test_preview_insufficient_capacity(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-09-30")
        self.add(plan["plan_id"], "stats", deadline="2026-09-30")
        self.weekly((0, "18:00", "19:00"))
        response = self.preview(plan["plan_id"])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        capacity = body["capacity"]
        self.assertEqual(capacity["status"], "at_risk")
        self.assertGreater(capacity["shortfall_minutes"], 0)
        self.assertLessEqual(capacity["scheduled_minutes"], 60)
        self.assertGreater(capacity["available_minutes"], 0)
        unscheduled = capacity["unscheduled"][0]
        self.assertEqual(set(unscheduled), {"document_id", "document_title", "activity_type", "estimated_minutes",
                                            "deadline", "reason", "artifact_id"})
        self.assertEqual(body["warnings"], [])

    def test_past_deadline_is_judged_on_the_learner_local_date_at_preview(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-05")
        passed = self.add(plan["plan_id"], "stats", deadline="2026-09-27").json()
        self.weekly((0, "18:00", "22:00"))
        base = f"/api/planner/plans/{plan['plan_id']}"
        # 01:00 UTC Monday Sep 28: at UTC-5 it is still Sunday Sep 27 -> the deadline is today, which
        # passes validation; the scheduler/capacity then decide (no Sunday availability -> unscheduled).
        west = self.client.post(f"{base}/preview", json={"utc_offset_minutes": -300})
        self.assertEqual(west.status_code, 200, west.text)
        west_body = west.json()
        self.assertEqual(west_body["local_now"], "2026-09-27T20:00:00")
        self.assertNotIn("stats", {s["document_id"] for s in west_body["sessions"]})
        self.assertIn("stats", {c["document_id"] for c in west_body["capacity"]["unscheduled"]})
        self.assertEqual(west_body["capacity"]["status"], "at_risk")
        # At UTC+7 it is Monday -> Sunday's deadline has passed: a clear 400, the scheduler never runs.
        east = self.client.post(f"{base}/preview", json={"utc_offset_minutes": 420})
        self.assertEqual(east.status_code, 400)
        detail = east.json()["detail"]
        self.assertEqual((detail["code"], detail["local_date"]), ("deadline_passed", "2026-09-28"))
        self.assertEqual(detail["documents"], [{"material_id": passed["material_id"], "document_id": "stats",
                                                "document_title": "Statistics", "deadline": "2026-09-27"}])
        self.assertEqual(study_planner_store.list_sessions(self.alice), [])
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan["plan_id"]), [])
        # Clearing the deadline makes the plan previewable again.
        self.client.patch(f"{base}/materials/{passed['material_id']}", json={"deadline": None})
        self.assertEqual(self.client.post(f"{base}/preview", json={"utc_offset_minutes": 420}).status_code, 200)

    def test_golden_multi_document_preview(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-01")
        self.add(plan["plan_id"], "stats", deadline="2026-10-05")
        self.add(plan["plan_id"], "pbi", deadline="2026-10-12")
        self.weekly((0, "18:00", "22:00"), (1, "20:00", "21:00"), (3, "19:00", "22:00"), (5, "09:00", "12:00"))
        body = self.preview(plan["plan_id"], utc_offset_minutes=420).json()
        sessions = body["sessions"]
        self.assertEqual(body["capacity"]["status"], "on_track")
        self.assertLess(body["capacity"]["scheduled_minutes"], body["capacity"]["available_minutes"] / 3)
        self.assertEqual(set(sessions[0]), {"document_id", "document_title", "activity_type", "scheduled_start",
                                            "scheduled_end", "duration_minutes", "reason", "artifact_id"})
        self.assertEqual((sessions[0]["document_id"], sessions[0]["document_title"], sessions[0]["scheduled_start"]),
                         ("mkt", "Marketing", "2026-09-28T18:00:00"))
        self.assertEqual({s["document_id"] for s in sessions}, {"mkt", "stats", "pbi"})
        for session in sessions:
            self.assertIn(session["reason"]["code"], SESSION_REASONS)
            self.assertTrue(session["reason"]["message"])
            self.assertNotIn("priority_snapshot", session)
        deadlines = {"mkt": date(2026, 10, 1), "stats": date(2026, 10, 5), "pbi": date(2026, 10, 12)}
        for session in sessions:
            self.assertLess(date.fromisoformat(session["scheduled_start"][:10]), deadlines[session["document_id"]])
        self.assertEqual(body["capacity"]["unscheduled"], [])
        self.assertEqual(study_planner_store.list_sessions(self.alice), [])
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan["plan_id"]), [])

    # -- confirm -----------------------------------------------------------------

    def confirm(self, plan_id, client=None, **body):
        return (client or self.client).post(f"/api/planner/plans/{plan_id}/confirm",
                                            json={"utc_offset_minutes": 0, "local_now": MONDAY_LOCAL, **body})

    def golden_plan(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-01")
        self.add(plan["plan_id"], "stats", deadline="2026-10-05")
        self.weekly((0, "18:00", "22:00"), (1, "20:00", "21:00"), (3, "19:00", "22:00"), (5, "09:00", "12:00"))
        return plan

    def saved(self, plan_id):
        return (study_planner_store.list_sessions(self.alice, plan_id=plan_id),
                study_planner_store.list_plan_schedule_runs(self.alice, plan_id))

    def test_confirm_saves_the_server_computed_schedule_and_one_run(self):
        plan = self.golden_plan()
        preview = self.preview(plan["plan_id"], utc_offset_minutes=420).json()
        response = self.confirm(plan["plan_id"], utc_offset_minutes=420)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["persisted"])
        key = lambda s: (s["document_id"], s["activity_type"], s["scheduled_start"], s["scheduled_end"], s["duration_minutes"])
        self.assertEqual([key(s) for s in body["sessions"]], [key(s) for s in preview["sessions"]])
        self.assertEqual(body["capacity"], preview["capacity"])
        first = body["sessions"][0]
        self.assertEqual(set(first), {"session_id", "document_id", "document_title", "activity_type", "scheduled_start",
                                      "scheduled_end", "duration_minutes", "status", "reason", "artifact_id", "started_at",
                                      "completed_at", "rescheduled_from"})
        self.assertEqual((first["status"], first["document_title"]), ("scheduled", "Marketing"))
        self.assertEqual(first["reason"], {"code": "deadline_approaching", "message": "Deadline is coming up"})
        sessions, runs = self.saved(plan["plan_id"])
        self.assertEqual(len(sessions), len(preview["sessions"]))
        self.assertEqual([(r["schedule_run_id"], r["reason"]) for r in runs], [(body["schedule_run_id"], "confirm")])
        listed = self.client.get(f"/api/planner/plans/{plan['plan_id']}/sessions").json()
        self.assertEqual(listed, body["sessions"])

    def test_confirm_never_accepts_client_sessions(self):
        plan = self.golden_plan()
        forged = [{"document_id": "mkt", "activity_type": "quiz", "scheduled_start": "2026-09-28T03:00:00",
                   "scheduled_end": "2026-09-28T04:00:00", "duration_minutes": 60}]
        self.assertEqual(self.confirm(plan["plan_id"], sessions=forged).status_code, 422)
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))

    def test_duplicate_confirmation_is_a_conflict(self):
        plan = self.golden_plan()
        self.assertEqual(self.confirm(plan["plan_id"]).status_code, 201)
        before = self.saved(plan["plan_id"])
        again = self.confirm(plan["plan_id"])
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.saved(plan["plan_id"]), before)
        # The store re-checks inside the write transaction, so a racing second confirm also fails.
        with self.assertRaises(study_planner_store.PlanAlreadyConfirmedError):
            study_planner_store.confirm_plan_sessions(self.alice, plan["plan_id"], [
                {"document_id": "mkt", "activity_type": "review", "scheduled_start": "2026-09-30T18:00:00",
                 "scheduled_end": "2026-09-30T18:15:00", "duration_minutes": 15}])
        self.assertEqual(self.saved(plan["plan_id"]), before)

    def test_confirm_is_atomic(self):
        plan = self.golden_plan()
        with patch.object(study_planner_store, "_insert_schedule_run", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                study_plan_api_service.confirm_plan(self.alice, plan["plan_id"], 0, local_now=MONDAY_LOCAL)
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))  # no sessions without their run
        with self.assertRaises(ValueError):  # one invalid session -> nothing written
            study_planner_store.confirm_plan_sessions(self.alice, plan["plan_id"], [
                {"document_id": "mkt", "activity_type": "summary", "scheduled_start": "2026-09-28T18:00:00",
                 "scheduled_end": "2026-09-28T18:45:00", "duration_minutes": 45},
                {"document_id": "not-in-plan", "activity_type": "quiz", "scheduled_start": "2026-09-29T18:00:00",
                 "scheduled_end": "2026-09-29T18:30:00", "duration_minutes": 30}])
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))
        self.assertEqual(self.confirm(plan["plan_id"]).status_code, 201)  # and it still confirms afterwards

    def test_partial_at_risk_plan_can_be_confirmed(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-09-30")
        self.add(plan["plan_id"], "stats", deadline="2026-09-30")
        self.weekly((0, "18:00", "19:00"))
        preview = self.preview(plan["plan_id"]).json()
        self.assertEqual(preview["capacity"]["status"], "at_risk")
        response = self.confirm(plan["plan_id"])
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["capacity"]["status"], "at_risk")
        self.assertEqual(len(body["sessions"]), len(preview["sessions"]))
        self.assertTrue(body["capacity"]["unscheduled"])

    def test_confirm_rejects_safely_when_nothing_can_be_scheduled(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "mkt", deadline="2026-10-05")
        response = self.confirm(plan["plan_id"])  # no availability at all
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "nothing_to_schedule")
        self.assertEqual(detail["capacity"]["status"], "at_risk")
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))

    def test_confirm_shares_preview_validation(self):
        plan = self.create_plan()
        self.assertEqual(self.confirm(plan["plan_id"]).json()["detail"]["code"], "no_materials")
        self.add(plan["plan_id"], "mkt", deadline="2026-09-27")
        self.weekly((0, "18:00", "22:00"))
        self.assertEqual(self.confirm(plan["plan_id"], utc_offset_minutes=7).json()["detail"]["code"], "invalid_utc_offset")
        self.assertEqual(self.confirm(plan["plan_id"], local_now="2026-09-28T08:00:00Z").json()["detail"]["code"],
                         "invalid_local_now")
        self.assertEqual(self.confirm(plan["plan_id"]).json()["detail"]["code"], "deadline_passed")
        self.assertEqual(self.client.post(f"/api/planner/plans/{plan['plan_id']}/confirm", json={}).status_code, 422)
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))

    def test_confirm_uses_the_learner_utc_offset(self):
        plan = self.create_plan()
        self.add(plan["plan_id"], "stats")
        self.add_flashcards(self.alice, "stats", 20)
        self.add_quiz(self.alice, "stats", "q1")
        self.add_attempt(self.alice, "stats", "q1", score=4, answered=10, completed=True,
                         completed_at="2026-09-27T20:30:00+00:00")
        self.weekly(*((day, "09:00", "22:00") for day in range(7)))
        body = self.client.post(f"/api/planner/plans/{plan['plan_id']}/confirm",
                                json={"utc_offset_minutes": 420, "local_now": MONDAY_LOCAL}).json()
        self.assertEqual(body["sessions"][0]["scheduled_start"][:10], "2026-09-29")  # Monday 03:30 local -> due Tue
        self.assertEqual(body["utc_offset_minutes"], 420)

    def test_confirm_and_sessions_are_owner_scoped(self):
        plan = self.golden_plan()
        bob = self.login("bob")
        self.assertEqual(self.confirm(plan["plan_id"], client=bob).status_code, 404)
        self.assertEqual(bob.get(f"/api/planner/plans/{plan['plan_id']}/sessions").status_code, 404)
        self.assertEqual(self.saved(plan["plan_id"]), ([], []))
        self.assertEqual(self.confirm(plan["plan_id"]).status_code, 201)
        self.assertEqual(bob.get(f"/api/planner/plans/{plan['plan_id']}/sessions").status_code, 404)

    # -- archived plans ------------------------------------------------------------

    def replan(self, title="Next plan"):
        """A second plan over the same documents, deadlines and (shared) availability."""
        plan = self.create_plan(title=title)
        self.add(plan["plan_id"], "mkt", deadline="2026-10-01")
        self.add(plan["plan_id"], "stats", deadline="2026-10-05")
        return plan

    def slots(self, body):
        return [(s["document_id"], s["activity_type"], s["scheduled_start"], s["scheduled_end"]) for s in body["sessions"]]

    def test_archived_plan_no_longer_blocks_the_next_plan(self):
        old = self.golden_plan()
        fresh = self.preview(old["plan_id"]).json()               # what an unblocked plan looks like
        self.assertEqual(self.confirm(old["plan_id"]).status_code, 201)
        old_sessions = study_planner_store.list_sessions(self.alice, plan_id=old["plan_id"])

        # While the old plan is active its future sessions are real commitments.
        new = self.replan()
        blocked = self.preview(new["plan_id"]).json()
        self.assertLess(blocked["capacity"]["schedulable_minutes"], fresh["capacity"]["schedulable_minutes"])
        self.assertFalse(set(self.slots(blocked)) & set(self.slots(fresh)))

        # "Start a new plan" archives it: the same availability is fully usable again.
        self.assertEqual(self.client.patch(f"/api/planner/plans/{old['plan_id']}", json={"status": "archived"}).status_code, 200)
        unblocked = self.preview(new["plan_id"]).json()
        self.assertEqual(self.slots(unblocked), self.slots(fresh))
        self.assertEqual(unblocked["capacity"], fresh["capacity"])
        self.assertEqual(self.confirm(new["plan_id"]).status_code, 201)
        # Nothing of the archived plan was deleted or rewritten.
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=old["plan_id"]), old_sessions)

    def test_archiving_keeps_completed_history_and_in_progress_work(self):
        old = self.golden_plan()
        saved = self.confirm(old["plan_id"]).json()["sessions"]
        done, active = saved[0], saved[1]
        study_planner_store.update_session(self.alice, done["session_id"], {"status": "completed"})
        study_planner_store.update_session(self.alice, active["session_id"], {"status": "in_progress"})
        self.client.patch(f"/api/planner/plans/{old['plan_id']}", json={"status": "archived"})

        statuses = {s["session_id"]: s["status"] for s in study_planner_store.list_sessions(self.alice, plan_id=old["plan_id"])}
        self.assertEqual(len(statuses), len(saved))                       # nothing deleted
        self.assertEqual((statuses[done["session_id"]], statuses[active["session_id"]]), ("completed", "in_progress"))
        busy = {s["session_id"] for s in study_planner_store.list_busy_sessions(self.alice)}
        self.assertEqual(busy, {done["session_id"], active["session_id"]})  # scheduled ones stopped blocking

        new = self.replan()
        body = self.preview(new["plan_id"]).json()
        for kept in (done, active):  # completed/in-progress time is never double-booked
            for session in body["sessions"]:
                self.assertFalse(session["scheduled_start"] < kept["scheduled_end"]
                                 and kept["scheduled_start"] < session["scheduled_end"], (session, kept))

    def test_archived_plans_only_release_their_own_owners_time(self):
        self.weekly((0, "18:00", "22:00"), (1, "20:00", "21:00"), (3, "19:00", "22:00"), (5, "09:00", "12:00"))
        bob = self.login("bob")
        self.weekly((0, "18:00", "22:00"), client=bob)
        bobs = self.create_plan(bob, title="Bob")
        self.add(bobs["plan_id"], "bob-doc", client=bob)
        bob_confirmed = self.confirm(bobs["plan_id"], client=bob).json()["sessions"]

        old = self.replan(title="Alice old")
        alice_fresh = self.preview(old["plan_id"]).json()   # Bob's sessions never block Alice
        self.confirm(old["plan_id"])
        self.client.patch(f"/api/planner/plans/{old['plan_id']}", json={"status": "archived"})
        self.assertEqual(self.slots(self.preview(self.replan()["plan_id"]).json()), self.slots(alice_fresh))

        # Bob's active plan still blocks Bob, and his sessions are untouched by Alice's archive.
        self.assertEqual([s["session_id"] for s in study_planner_store.list_busy_sessions(self.bob)],
                         [s["session_id"] for s in bob_confirmed])
        self.assertTrue(all(s["status"] == "scheduled" for s in study_planner_store.list_sessions(self.bob)))
        bob_next = self.create_plan(bob, title="Bob next")
        self.add(bob_next["plan_id"], "bob-doc", client=bob)
        bob_preview = self.preview(bob_next["plan_id"], client=bob).json()
        taken = {(s["scheduled_start"], s["scheduled_end"]) for s in bob_confirmed}
        self.assertFalse(taken & {(s["scheduled_start"], s["scheduled_end"]) for s in bob_preview["sessions"]})

    # -- legacy ------------------------------------------------------------------

    def test_legacy_planner_routes_still_work_alongside_v2(self):
        self.create_plan()
        task = self.client.post("/api/planner/tasks", json={"title": "Old", "deadline": "2026-10-10",
                                                              "estimated_minutes": 60})
        self.assertEqual(task.status_code, 200, task.text)
        self.assertEqual([t["title"] for t in self.client.get("/api/planner/tasks").json()], ["Old"])
        self.assertEqual(self.client.get("/api/planner/blocks").status_code, 200)
        self.assertEqual(len(self.client.get("/api/planner/plans").json()), 1)


if __name__ == "__main__":
    unittest.main()
