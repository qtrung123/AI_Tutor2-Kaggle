"""Study Planner auto-include after a PDF upload.

Upload A -> the active plan (created if needed) gets A and its suggestions cover A; upload B, then
C -> the same plan covers A + B (+ C). Only materials are added: suggestions stay the read-only
preview until Accept Plan, an accepted plan's sessions are never touched, a duplicate upload
changes nothing, and a planner failure never fails the upload (manual retry endpoint).
"""

import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_plan_api_service, study_planner_store
from backend.api import documents as documents_api
from backend.main import app

UTC_NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
LOCAL_NOW = "2026-09-28T08:00:00"
SCHEDULE = {"utc_offset_minutes": 0, "local_now": LOCAL_NOW}


class PlannerAutoIncludeTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        for patcher in (patch.object(study_plan_api_service, "_utc_now", return_value=UTC_NOW),
                        patch.object(documents_api, "DATA_DIR", Path(self.temp.name)),
                        patch.object(documents_api, "list_uploaded_sources", return_value=[]),
                        patch.object(documents_api, "index_files", side_effect=self.fake_index_files)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alice = self.user("Alice")
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "alice-planner@example.com",
                                                             "password": "long-password-x"})
        self.assertEqual(response.status_code, 200, response.text)
        for day in range(7):
            response = self.client.post("/api/planner/availability", json={
                "start_at": "09:00", "end_at": "21:00", "is_recurring": True, "day_of_week": day})
            self.assertEqual(response.status_code, 200, response.text)

    def fake_index_files(self, paths, owner_id):
        """index_files stand-in: a new name is indexed and persisted; a known one is a duplicate."""
        known = {document["document_id"] for document in study_plan_api_service.list_indexed_documents(owner_id)}
        indexed, skipped = [], []
        for path in paths:
            if path.name in known:
                skipped.append(path.name)
            else:
                self.add_document(owner_id, path.name, path.stem.upper())
                indexed.append(path.name)
        return {"new_files": len(indexed), "new_chunks": 12 * len(indexed), "skipped_files": skipped,
                "topic_refreshed_files": [], "indexed_documents": indexed, "total_indexed_files": len(known) + len(indexed)}

    def upload(self, name):
        response = self.client.post("/api/sources/upload", files=[("files", (name, b"%PDF-1.4 x", "application/pdf"))])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def active_plans(self):
        return [plan for plan in study_planner_store.list_plans(self.alice) if plan["status"] == "active"]

    def materials(self, plan_id):
        return sorted(material["document_id"] for material in study_planner_store.list_materials(self.alice, plan_id))

    def preview_documents(self, plan_id):
        response = self.client.post(f"/api/planner/plans/{plan_id}/preview", json=SCHEDULE)
        self.assertEqual(response.status_code, 200, response.text)
        return {session["document_id"] for session in response.json()["sessions"]}

    def test_first_second_and_third_upload_extend_the_same_suggested_plan(self):
        first = self.upload("a.pdf")["planner"]
        self.assertEqual((first["status"], first["added_document_ids"]), ("updated", ["a.pdf"]))
        [plan] = self.active_plans()
        self.assertEqual((plan["plan_id"], plan["title"]), (first["plan_id"], "My study plan"))
        self.assertEqual(self.preview_documents(plan["plan_id"]), {"a.pdf"})

        second = self.upload("b.pdf")["planner"]
        self.assertEqual((second["plan_id"], second["added_document_ids"]), (plan["plan_id"], ["b.pdf"]))
        self.assertEqual(self.preview_documents(plan["plan_id"]), {"a.pdf", "b.pdf"})

        third = self.upload("c.pdf")["planner"]
        self.assertEqual((third["plan_id"], third["added_document_ids"]), (plan["plan_id"], ["c.pdf"]))
        self.assertEqual([p["plan_id"] for p in self.active_plans()], [plan["plan_id"]])
        self.assertEqual(self.materials(plan["plan_id"]), ["a.pdf", "b.pdf", "c.pdf"])
        self.assertEqual(self.preview_documents(plan["plan_id"]), {"a.pdf", "b.pdf", "c.pdf"})
        # Suggestions only: nothing is saved until Accept Plan.
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan["plan_id"]), [])
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan["plan_id"]), [])

    def test_existing_goals_and_plan_are_kept(self):
        plan = self.client.post("/api/planner/plans", json={"title": "Finals"}).json()
        self.add_document(self.alice, "old.pdf", "Old")
        material = self.client.post(f"/api/planner/plans/{plan['plan_id']}/materials", json={
            "document_id": "old.pdf", "goal_type": "deadline", "deadline": "2026-10-20"}).json()
        before = study_planner_store.get_material(self.alice, material["material_id"])

        self.assertEqual(self.upload("new.pdf")["planner"]["plan_id"], plan["plan_id"])   # the user's own plan
        self.assertEqual(study_planner_store.get_material(self.alice, material["material_id"]), before)
        self.assertEqual(study_planner_store.get_plan(self.alice, plan["plan_id"])["title"], "Finals")
        self.assertEqual(self.materials(plan["plan_id"]), ["new.pdf", "old.pdf"])

    def test_duplicate_upload_and_removed_material_change_nothing(self):
        plan_id = self.upload("a.pdf")["planner"]["plan_id"]
        self.assertEqual(self.upload("a.pdf")["planner"]["status"], "unchanged")
        material = study_planner_store.get_plan_material(self.alice, plan_id, "a.pdf")
        self.client.delete(f"/api/planner/plans/{plan_id}/materials/{material['material_id']}")
        self.assertEqual(self.upload("a.pdf")["planner"]["status"], "unchanged")   # not silently re-added
        self.assertEqual(self.materials(plan_id), [])
        self.assertEqual(len(self.active_plans()), 1)

    def test_accepted_plan_sessions_are_never_touched(self):
        plan_id = self.upload("a.pdf")["planner"]["plan_id"]
        confirmed = self.client.post(f"/api/planner/plans/{plan_id}/confirm", json=SCHEDULE)
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        before = study_planner_store.list_sessions(self.alice, plan_id=plan_id)
        self.assertTrue(before)
        runs = study_planner_store.list_plan_schedule_runs(self.alice, plan_id)

        for name in ("b.pdf", "c.pdf"):
            self.assertEqual(self.upload(name)["planner"]["added_document_ids"], [name])
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), before)
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan_id), runs)
        self.assertEqual(self.materials(plan_id), ["a.pdf", "b.pdf", "c.pdf"])
        # The new materials are offered as still-to-schedule candidates, for the learner to place.
        candidates = self.client.get(f"/api/planner/plans/{plan_id}/candidates",
                                     params={"utc_offset_minutes": 0, "local_now": LOCAL_NOW}).json()
        self.assertTrue({"b.pdf", "c.pdf"} <= {item["document_id"] for item in candidates})

    # -- accepted plan: new material becomes a proposal (adaptation trigger "materials_added") -------

    def propose_new_materials(self, plan_id):
        response = self.client.post(f"/api/planner/plans/{plan_id}/adaptation/preview",
                                    json={"trigger": {"kind": "materials_added"}, **SCHEDULE})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply_new_materials(self, plan_id):
        response = self.client.post(f"/api/planner/plans/{plan_id}/adaptation/apply",
                                    json={"trigger": {"kind": "materials_added"}, "confirm": True, **SCHEDULE})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_free_and_disjoint(self, plan_id, added):
        """Every proposed session is inside availability (09:00-21:00 daily) and overlaps nothing."""
        taken = [(s["scheduled_start"], s["scheduled_end"]) for s in study_planner_store.list_sessions(self.alice, plan_id=plan_id)
                 if s["status"] in ("scheduled", "in_progress", "completed")]
        for item in added:
            start, end = item["scheduled_start"], item["scheduled_end"]
            self.assertEqual(start[:10], end[:10])
            self.assertTrue("09:00" <= start[11:16] and end[11:16] <= "21:00", item)
            self.assertGreaterEqual(start, LOCAL_NOW)
            for other_start, other_end in taken:
                self.assertFalse(start < other_end and other_start < end, (item, other_start, other_end))
            taken.append((start, end))

    def accepted_plan_with_a_manual_edit(self):
        plan_id = self.upload("a.pdf")["planner"]["plan_id"]
        self.assertEqual(self.client.post(f"/api/planner/plans/{plan_id}/confirm", json=SCHEDULE).status_code, 201)
        first = study_planner_store.list_sessions(self.alice, plan_id=plan_id)[0]
        edited = self.client.post(f"/api/planner/sessions/{first['session_id']}/reschedule",
                                  json={**SCHEDULE, "duration_minutes": first["duration_minutes"] + 10})
        self.assertEqual(edited.status_code, 200, edited.text)
        sessions = study_planner_store.list_sessions(self.alice, plan_id=plan_id)
        self.assertTrue(any(s.get("manual_edit") for s in sessions))
        return plan_id, sessions

    def test_new_pdf_after_an_accepted_plan_is_proposed_not_accepted(self):
        plan_id, accepted = self.accepted_plan_with_a_manual_edit()
        self.assertEqual(self.upload("b.pdf")["planner"]["added_document_ids"], ["b.pdf"])

        proposal = self.propose_new_materials(plan_id)
        self.assertFalse(proposal["persisted"])
        self.assertTrue(proposal["added"])
        self.assertEqual({item["document_id"] for item in proposal["added"]}, {"b.pdf"})
        self.assertEqual((proposal["moved"], proposal["cancelled"]), ([], []))   # nothing accepted moves
        self.assert_free_and_disjoint(plan_id, proposal["added"])
        # Only a proposal: nothing is saved until the learner accepts it.
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), accepted)

        applied = self.apply_new_materials(plan_id)
        self.assertTrue(applied["applied"])
        after = study_planner_store.list_sessions(self.alice, plan_id=plan_id)
        by_id = {s["session_id"]: s for s in after}
        for session in accepted:   # accepted and hand-edited sessions: byte-for-byte unchanged
            self.assertEqual(by_id[session["session_id"]], session)
        new = [s for s in after if s["session_id"] not in {a["session_id"] for a in accepted}]
        self.assertEqual({s["document_id"] for s in new}, {"b.pdf"})
        self.assertEqual(len(new), len(proposal["added"]))

    def test_second_and_third_pdf_each_get_their_own_proposal(self):
        plan_id, _ = self.accepted_plan_with_a_manual_edit()
        self.upload("b.pdf")
        self.apply_new_materials(plan_id)
        before_c = study_planner_store.list_sessions(self.alice, plan_id=plan_id)

        self.upload("c.pdf")
        proposal = self.propose_new_materials(plan_id)
        self.assertEqual({item["document_id"] for item in proposal["added"]}, {"c.pdf"})   # B is already planned
        self.assertEqual((proposal["moved"], proposal["cancelled"]), ([], []))
        self.assert_free_and_disjoint(plan_id, proposal["added"])
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), before_c)

        self.apply_new_materials(plan_id)
        self.assertEqual(self.propose_new_materials(plan_id)["change_count"], 0)   # nothing left to propose

    def test_two_pdfs_uploaded_before_review_share_one_proposal(self):
        plan_id, _ = self.accepted_plan_with_a_manual_edit()
        self.upload("b.pdf")
        self.upload("c.pdf")
        proposal = self.propose_new_materials(plan_id)
        self.assertEqual({item["document_id"] for item in proposal["added"]}, {"b.pdf", "c.pdf"})
        self.assert_free_and_disjoint(plan_id, proposal["added"])

    def test_no_proposal_before_the_plan_is_accepted(self):
        plan_id = self.upload("a.pdf")["planner"]["plan_id"]
        self.upload("b.pdf")
        proposal = self.propose_new_materials(plan_id)   # suggestions come from the regular preview instead
        self.assertEqual(proposal["change_count"], 0)
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), [])

    def test_accepted_plan_with_no_free_time_left_gets_warnings_not_overlaps(self):
        plan_id, accepted = self.accepted_plan_with_a_manual_edit()
        for day in range(7):   # remove every free slot
            self.client.post("/api/planner/availability/remove", json={
                "start_at": "09:00", "end_at": "21:00", "is_recurring": True, "day_of_week": day})
        self.upload("b.pdf")
        proposal = self.propose_new_materials(plan_id)
        self.assertEqual(proposal["added"], [])
        self.assertTrue(any(w["code"] == "no_available_slot" for w in proposal["warnings"]))
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), accepted)

    def test_planner_failure_keeps_the_upload_and_can_be_retried(self):
        plan_id = self.upload("a.pdf")["planner"]["plan_id"]
        with patch.object(study_plan_api_service.study_planner_store, "add_material", side_effect=RuntimeError("db down")):
            body = self.upload("b.pdf")
        self.assertEqual(body["new_files"], 1)
        planner = body["planner"]
        self.assertEqual((planner["status"], planner["document_ids"]), ("failed", ["b.pdf"]))
        self.assertNotIn("db down", planner["message"])
        self.assertIn("b.pdf", {d["document_id"] for d in study_plan_api_service.list_indexed_documents(self.alice)})
        self.assertEqual(self.materials(plan_id), ["a.pdf"])

        retried = self.client.post("/api/planner/include-documents", json={"document_ids": planner["document_ids"]})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual((retried.json()["plan_id"], retried.json()["added_document_ids"]), (plan_id, ["b.pdf"]))
        self.assertEqual(self.materials(plan_id), ["a.pdf", "b.pdf"])

    def test_retry_only_accepts_the_owners_indexed_documents(self):
        bob = self.user("Bob")
        self.add_document(bob, "bob.pdf", "Bob")
        response = self.client.post("/api/planner/include-documents", json={"document_ids": ["bob.pdf", "missing.pdf"]})
        self.assertEqual(response.json(), {"status": "unchanged", "plan_id": None, "added_document_ids": []})
        self.assertEqual(self.active_plans(), [])

    def test_concurrent_uploads_share_one_active_plan(self):
        for name in ("x.pdf", "y.pdf"):
            self.add_document(self.alice, name, name)
        real_create = study_planner_store.create_plan

        def slow_create(*args, **kwargs):
            time.sleep(0.05)   # widen the find-or-create window
            return real_create(*args, **kwargs)

        results = []
        with patch.object(study_plan_api_service.study_planner_store, "create_plan", side_effect=slow_create):
            threads = [threading.Thread(target=lambda n=name: results.append(
                study_plan_api_service.include_uploaded_documents(self.alice, [n]))) for name in ("x.pdf", "y.pdf")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        [plan] = self.active_plans()
        self.assertEqual({result["plan_id"] for result in results}, {plan["plan_id"]})
        self.assertEqual(self.materials(plan["plan_id"]), ["x.pdf", "y.pdf"])


if __name__ == "__main__":
    unittest.main()
