"""Study Planner materials are chosen explicitly -- an upload never auto-includes a document.

Upload A, B, C -> no plan, material, session or Google Calendar change; only the documents the
learner adds through Add Material are previewed and accepted. On an accepted plan, Add Material
becomes a reviewable proposal (adaptation trigger "materials_added"): accepted and hand-edited
sessions are never touched.
"""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import google_calendar_sync, study_plan_api_service, study_planner_store
from backend.api import documents as documents_api
from backend.main import app

UTC_NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
LOCAL_NOW = "2026-09-28T08:00:00"
SCHEDULE = {"utc_offset_minutes": 0, "local_now": LOCAL_NOW}


class PlannerMaterialSelectionTests(PlannerDatabaseMixin, unittest.TestCase):
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

    def create_plan(self):
        response = self.client.post("/api/planner/plans", json={"title": "My study plan"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["plan_id"]

    def add_material(self, plan_id, document_id, **goal):
        response = self.client.post(f"/api/planner/plans/{plan_id}/materials", json={"document_id": document_id, **goal})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def active_plans(self):
        return [plan for plan in study_planner_store.list_plans(self.alice) if plan["status"] == "active"]

    def materials(self, plan_id):
        return sorted(material["document_id"] for material in study_planner_store.list_materials(self.alice, plan_id))

    def preview(self, plan_id):
        response = self.client.post(f"/api/planner/plans/{plan_id}/preview", json=SCHEDULE)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def confirm(self, plan_id):
        response = self.client.post(f"/api/planner/plans/{plan_id}/confirm", json=SCHEDULE)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # -- uploading never plans ----------------------------------------------------------------------

    def test_upload_creates_no_plan_material_session_or_calendar_change(self):
        with patch.object(google_calendar_sync, "after_planner_change") as calendar:
            for name in ("a.pdf", "b.pdf", "c.pdf"):
                body = self.upload(name)
                self.assertEqual(body["new_files"], 1)
                self.assertNotIn("planner", body)
        self.assertEqual(self.active_plans(), [])
        self.assertEqual(study_planner_store.list_sessions(self.alice), [])
        calendar.assert_not_called()

    def test_upload_leaves_an_existing_plan_and_its_accepted_sessions_alone(self):
        self.add_document(self.alice, "old.pdf", "Old")
        plan_id = self.create_plan()
        material = self.add_material(plan_id, "old.pdf", goal_type="deadline", deadline="2026-10-20")
        self.confirm(plan_id)
        sessions = study_planner_store.list_sessions(self.alice, plan_id=plan_id)
        runs = study_planner_store.list_plan_schedule_runs(self.alice, plan_id)
        before = study_planner_store.get_material(self.alice, material["material_id"])

        with patch.object(google_calendar_sync, "after_planner_change") as calendar:
            self.upload("new.pdf")
        calendar.assert_not_called()
        self.assertEqual(self.materials(plan_id), ["old.pdf"])   # not silently added
        self.assertEqual(study_planner_store.get_material(self.alice, material["material_id"]), before)
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), sessions)
        self.assertEqual(study_planner_store.list_plan_schedule_runs(self.alice, plan_id), runs)
        self.assertEqual([plan["plan_id"] for plan in self.active_plans()], [plan_id])

    def test_the_auto_include_endpoint_is_gone(self):
        self.add_document(self.alice, "a.pdf", "A")
        response = self.client.post("/api/planner/include-documents", json={"document_ids": ["a.pdf"]})
        self.assertIn(response.status_code, (404, 405))
        self.assertEqual(self.active_plans(), [])

    # -- only the selected materials are planned --------------------------------------------------

    def test_generate_uses_only_the_selected_materials(self):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.upload(name)
        plan_id = self.create_plan()
        self.add_material(plan_id, "a.pdf")
        self.add_material(plan_id, "c.pdf")
        self.assertEqual({s["document_id"] for s in self.preview(plan_id)["sessions"]}, {"a.pdf", "c.pdf"})
        confirmed = self.confirm(plan_id)
        self.assertEqual({s["document_id"] for s in confirmed["sessions"]}, {"a.pdf", "c.pdf"})

    def test_removed_material_leaves_the_preview(self):
        for name in ("a.pdf", "b.pdf"):
            self.upload(name)
        plan_id = self.create_plan()
        self.add_material(plan_id, "a.pdf")
        material = self.add_material(plan_id, "b.pdf")
        self.client.delete(f"/api/planner/plans/{plan_id}/materials/{material['material_id']}")
        self.assertEqual({s["document_id"] for s in self.preview(plan_id)["sessions"]}, {"a.pdf"})

    def test_a_flexible_document_gets_a_meaningful_week(self):
        # Summary -> Flashcards -> Quiz -> Review on different days, not one lone quiz. Reading the
        # summary and practising the flashcards share one combined session.
        self.upload("a.pdf")
        plan_id = self.create_plan()
        self.add_material(plan_id, "a.pdf")
        sessions = sorted(self.preview(plan_id)["sessions"], key=lambda s: s["scheduled_start"])
        activities = [step["activity_type"] for s in sessions for step in (s["steps"] or [s])]
        self.assertEqual(activities, ["summary", "flashcards", "quiz", "review"])
        self.assertEqual([len(s["steps"]) for s in sessions], [2, 0, 0])
        self.assertEqual(len({s["scheduled_start"][:10] for s in sessions}), 3)
        self.assertLessEqual(sessions[-1]["scheduled_start"][:10], "2026-10-04")   # within the week
        self.assertNotIn("written_quiz", {s["activity_type"] for s in sessions})

    # -- accepted plan: Add Material becomes a proposal (adaptation trigger "materials_added") -----

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
        self.upload("a.pdf")
        plan_id = self.create_plan()
        self.add_material(plan_id, "a.pdf")
        self.confirm(plan_id)
        first = study_planner_store.list_sessions(self.alice, plan_id=plan_id)[0]
        edited = self.client.post(f"/api/planner/sessions/{first['session_id']}/reschedule",
                                  json={**SCHEDULE, "duration_minutes": first["duration_minutes"] + 10})
        self.assertEqual(edited.status_code, 200, edited.text)
        sessions = study_planner_store.list_sessions(self.alice, plan_id=plan_id)
        self.assertTrue(any(s.get("manual_edit") for s in sessions))
        return plan_id, sessions

    def test_uploaded_pdf_is_not_proposed_until_it_is_added(self):
        plan_id, accepted = self.accepted_plan_with_a_manual_edit()
        self.upload("b.pdf")
        self.assertEqual(self.propose_new_materials(plan_id)["change_count"], 0)
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), accepted)

    def test_added_material_on_an_accepted_plan_is_proposed_not_accepted(self):
        plan_id, accepted = self.accepted_plan_with_a_manual_edit()
        self.upload("b.pdf")
        self.add_material(plan_id, "b.pdf")

        proposal = self.propose_new_materials(plan_id)
        self.assertFalse(proposal["persisted"])
        self.assertEqual({item["document_id"] for item in proposal["added"]}, {"b.pdf"})
        self.assertEqual((proposal["moved"], proposal["cancelled"]), ([], []))   # nothing accepted moves
        self.assert_free_and_disjoint(plan_id, proposal["added"])
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
        self.assertEqual(self.propose_new_materials(plan_id)["change_count"], 0)   # never proposed twice

    def test_no_free_time_left_gets_warnings_not_overlaps(self):
        plan_id, accepted = self.accepted_plan_with_a_manual_edit()
        for day in range(7):   # remove every free slot
            self.client.post("/api/planner/availability/remove", json={
                "start_at": "09:00", "end_at": "21:00", "is_recurring": True, "day_of_week": day})
        self.upload("b.pdf")
        self.add_material(plan_id, "b.pdf")
        proposal = self.propose_new_materials(plan_id)
        self.assertEqual(proposal["added"], [])
        self.assertTrue(any(w["code"] == "no_available_slot" for w in proposal["warnings"]))
        self.assertEqual(study_planner_store.list_sessions(self.alice, plan_id=plan_id), accepted)


if __name__ == "__main__":
    unittest.main()
