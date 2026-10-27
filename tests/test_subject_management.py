"""SUBJECT-MANAGEMENT: user-owned subjects that group uploaded documents.

Store and API: CRUD, ownership, the legacy-database migration, assign/move/unassign, deleting a
subject (documents only become Unassigned; nothing else changes), upload with a subject, and the
dashboard payload Home and the Planner read. Frontend source checks cover Home and the Planner.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import auth_store, conversation_store, indexed_document_store, subject_store
from backend.api import documents as documents_api
from backend.api import subjects as subjects_api
from backend.api.deps import require_current_user
from frontend_source import FRONTEND, frontend_script_text


class SubjectStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database_path = root / "subjects.db"
        for patcher in (
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alice = auth_store.create_user("Alice", "alice@example.com", "correct-horse-123")["id"]
        self.bob = auth_store.create_user("Bob", "bob@example.com", "correct-horse-123")["id"]

    def add_document(self, owner_id: str, document_id: str, topics=None) -> dict:
        return indexed_document_store.upsert_indexed_document(owner_id, document_id, {
            "hash": f"hash-{document_id}", "chunks": 3, "path": f"/data/{document_id}",
            "topic_schema_version": 5, "topics": topics or [{"topic_id": "t1", "name": "Cells"}],
        })

    def subject_of(self, owner_id: str, document_id: str):
        return indexed_document_store.get_indexed_document(owner_id, document_id)["subject_id"]


class SubjectStoreTests(SubjectStoreTestCase):
    def test_crud_with_counts_names_and_colors(self):
        biology = subject_store.create_subject(self.alice, "  Biology   101 ")
        physics = subject_store.create_subject(self.alice, "Physics", "#10B981")
        self.assertEqual(biology["name"], "Biology 101")
        self.assertIn(biology["color"], subject_store.SUBJECT_COLORS)
        self.assertEqual(physics["color"], "#10b981")
        self.assertTrue(biology["created_at"] and biology["updated_at"])

        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.add_document(self.alice, name)
        subject_store.assign_documents(self.alice, ["a.pdf", "b.pdf"], biology["subject_id"])
        subject_store.assign_documents(self.alice, ["c.pdf"], physics["subject_id"])
        listed = {subject["name"]: subject for subject in subject_store.list_subjects(self.alice)}
        self.assertEqual(listed["Biology 101"]["document_count"], 2)
        self.assertEqual(sorted(listed["Biology 101"]["document_ids"]), ["a.pdf", "b.pdf"])
        self.assertEqual(listed["Physics"]["document_ids"], ["c.pdf"])

        renamed = subject_store.update_subject(self.alice, physics["subject_id"], name="Physics II", color="#ef4444")
        self.assertEqual((renamed["name"], renamed["color"], renamed["document_count"]), ("Physics II", "#ef4444", 1))
        recolored = subject_store.update_subject(self.alice, physics["subject_id"], color="#8b5cf6")
        self.assertEqual((recolored["name"], recolored["color"]), ("Physics II", "#8b5cf6"))

    def test_names_are_validated_and_unique_per_owner_case_insensitively(self):
        subject_store.create_subject(self.alice, "Biology")
        with self.assertRaises(subject_store.SubjectNameConflict):
            subject_store.create_subject(self.alice, "biology")
        other = subject_store.create_subject(self.alice, "Chemistry")
        with self.assertRaises(subject_store.SubjectNameConflict):
            subject_store.update_subject(self.alice, other["subject_id"], name="BIOLOGY")
        subject_store.update_subject(self.alice, other["subject_id"], name="chemistry")   # own name, new case
        subject_store.create_subject(self.bob, "Biology")   # another user's namespace
        for bad in ("", "   ", "x" * (subject_store.MAX_SUBJECT_NAME_LENGTH + 1)):
            with self.assertRaises(ValueError):
                subject_store.create_subject(self.alice, bad)
        with self.assertRaises(ValueError):
            subject_store.create_subject(self.alice, "Art", "red")

    def test_get_or_create_reuses_an_existing_name(self):
        first = subject_store.get_or_create_subject(self.alice, "History")
        again = subject_store.get_or_create_subject(self.alice, " history ")
        self.assertEqual(first["subject_id"], again["subject_id"])
        self.assertEqual(len(subject_store.list_subjects(self.alice)), 1)

    def test_assign_move_and_unassign_single_and_multiple_documents(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        physics = subject_store.create_subject(self.alice, "Physics")["subject_id"]
        for name in ("a.pdf", "b.pdf"):
            self.add_document(self.alice, name)
        self.assertEqual(subject_store.assign_documents(self.alice, ["a.pdf", "b.pdf", "a.pdf"], biology), ["a.pdf", "b.pdf"])
        subject_store.assign_documents(self.alice, ["b.pdf"], physics)   # move: at most one subject each
        self.assertEqual((self.subject_of(self.alice, "a.pdf"), self.subject_of(self.alice, "b.pdf")), (biology, physics))
        subject_store.assign_documents(self.alice, ["a.pdf", "b.pdf"], None)
        self.assertEqual((self.subject_of(self.alice, "a.pdf"), self.subject_of(self.alice, "b.pdf")), (None, None))

    def test_assignment_is_all_or_nothing(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        self.add_document(self.alice, "a.pdf")
        with self.assertRaises(subject_store.DocumentNotFound):
            subject_store.assign_documents(self.alice, ["a.pdf", "missing.pdf"], biology)
        self.assertIsNone(self.subject_of(self.alice, "a.pdf"))
        with self.assertRaises(subject_store.SubjectNotFound):
            subject_store.assign_documents(self.alice, ["a.pdf"], "no-such-subject")
        self.assertEqual(subject_store.assign_documents(self.alice, ["a.pdf", "missing.pdf"], biology, ignore_missing=True), ["a.pdf"])

    def test_ownership_is_enforced_for_every_operation(self):
        bobs = subject_store.create_subject(self.bob, "Bob's subject")["subject_id"]
        alices = subject_store.create_subject(self.alice, "Alice's subject")["subject_id"]
        self.add_document(self.alice, "shared.pdf")
        self.add_document(self.bob, "shared.pdf")
        self.assertEqual([subject["name"] for subject in subject_store.list_subjects(self.alice)], ["Alice's subject"])
        with self.assertRaises(subject_store.SubjectNotFound):
            subject_store.get_subject(self.alice, bobs)
        with self.assertRaises(subject_store.SubjectNotFound):
            subject_store.update_subject(self.alice, bobs, name="Taken over")
        with self.assertRaises(subject_store.SubjectNotFound):
            subject_store.delete_subject(self.alice, bobs)
        with self.assertRaises(subject_store.SubjectNotFound):   # Alice cannot file into Bob's subject
            subject_store.assign_documents(self.alice, ["shared.pdf"], bobs)
        subject_store.assign_documents(self.alice, ["shared.pdf"], alices)
        self.assertIsNone(self.subject_of(self.bob, "shared.pdf"))   # same name, Bob's row untouched
        self.assertEqual(subject_store.get_subject(self.bob, bobs)["name"], "Bob's subject")

    def test_reindexing_a_document_keeps_its_subject_and_order(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        self.add_document(self.alice, "a.pdf")
        before = indexed_document_store.get_indexed_document(self.alice, "a.pdf")
        subject_store.assign_documents(self.alice, ["a.pdf"], biology)
        self.assertEqual(indexed_document_store.list_indexed_documents(self.alice)[0]["subject_id"], biology)
        self.add_document(self.alice, "a.pdf", topics=[{"topic_id": "t2", "name": "Genetics"}])   # topic refresh / re-index
        after = indexed_document_store.get_indexed_document(self.alice, "a.pdf")
        self.assertEqual(after["subject_id"], biology)
        self.assertEqual(after["topics"], [{"topic_id": "t2", "name": "Genetics"}])
        self.assertEqual(before["hash"], after["hash"])

    def test_deleting_a_subject_only_unassigns_its_documents(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        physics = subject_store.create_subject(self.alice, "Physics")["subject_id"]
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.add_document(self.alice, name)
        subject_store.assign_documents(self.alice, ["a.pdf", "b.pdf"], biology)
        subject_store.assign_documents(self.alice, ["c.pdf"], physics)
        conversation = conversation_store.create_conversation(self.alice, "Chat", ["a.pdf"])

        def snapshot():   # every row of every table, minus the subjects table and the subject link
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            try:
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('subjects', 'sqlite_sequence')")]
                return {table: sorted(tuple((key, row[key]) for key in row.keys() if key != "subject_id")
                                      for row in connection.execute(f"SELECT * FROM {table}")) for table in tables}
            finally:
                connection.close()

        before = snapshot()
        result = subject_store.delete_subject(self.alice, biology)
        self.assertEqual(result, {"deleted": biology, "unassigned_document_ids": ["a.pdf", "b.pdf"]})
        self.assertEqual(snapshot(), before)   # documents, chat and every other row are unchanged
        self.assertEqual([self.subject_of(self.alice, name) for name in ("a.pdf", "b.pdf", "c.pdf")], [None, None, physics])
        self.assertEqual(conversation_store.get_conversation(self.alice, conversation["id"], include_messages=False)["id"],
                         conversation["id"])
        self.assertEqual([subject["subject_id"] for subject in subject_store.list_subjects(self.alice)], [physics])

    def test_deleting_a_document_removes_it_from_its_subject(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        self.add_document(self.alice, "a.pdf")
        subject_store.assign_documents(self.alice, ["a.pdf"], biology)
        indexed_document_store.delete_indexed_document(self.alice, "a.pdf")
        self.assertEqual(subject_store.get_subject(self.alice, biology)["document_count"], 0)


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_a_registry_without_subject_column_is_migrated_and_stays_unassigned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with patch.object(auth_store, "DATABASE_PATH", path), \
                 patch.object(conversation_store, "DATABASE_PATH", path), \
                 patch.object(indexed_document_store, "DATABASE_PATH", path), \
                 patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(directory) / "none.json"):
                owner = auth_store.create_user("Legacy", "legacy@example.com", "correct-horse-123")["id"]
                other = auth_store.create_user("Other", "other@example.com", "correct-horse-123")["id"]
                # The registry exactly as it was before subjects existed.
                connection = sqlite3.connect(path)
                connection.executescript(
                    """
                    CREATE TABLE indexed_documents (
                        owner_id TEXT NOT NULL, document_id TEXT NOT NULL, display_name TEXT NOT NULL,
                        file_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL, storage_path TEXT NOT NULL,
                        topic_schema_version INTEGER NOT NULL, topics_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, object_path TEXT, object_bucket TEXT,
                        PRIMARY KEY (owner_id, document_id));
                    """
                )
                for user in (owner, other):
                    connection.execute(
                        "INSERT INTO indexed_documents VALUES (?, 'old.pdf', 'old.pdf', 'h', 4, '/x', 5, '[]', 't', 't', NULL, NULL)",
                        (user,),
                    )
                connection.commit()
                connection.close()
                conversation_store.create_conversation(owner, "Old chat", ["old.pdf"])   # feature data next to the registry

                def snapshot():   # every row of every table, minus what the migration adds
                    db_connection = sqlite3.connect(path)
                    db_connection.row_factory = sqlite3.Row
                    try:
                        tables = [row[0] for row in db_connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name NOT IN ('subjects', 'sqlite_sequence', 'index_registry_migrations')")]
                        return {table: sorted(tuple((key, row[key]) for key in row.keys() if key != "subject_id")
                                              for row in db_connection.execute(f"SELECT * FROM {table}")) for table in tables}
                    finally:
                        db_connection.close()

                before = snapshot()
                self.assertEqual(subject_store.list_subjects(owner), [])
                self.assertEqual(snapshot(), before)   # documents, ownership and chat rows unchanged
                document = indexed_document_store.get_indexed_document(owner, "old.pdf")
                self.assertIsNone(document["subject_id"])
                self.assertEqual((document["chunks"], document["hash"]), (4, "h"))
                subject = subject_store.create_subject(owner, "Old course")
                subject_store.assign_documents(owner, ["old.pdf"], subject["subject_id"])
                self.assertEqual(subject_store.get_subject(owner, subject["subject_id"])["document_ids"], ["old.pdf"])
                subject_store.initialize_subject_store()   # idempotent on a migrated database
                self.assertEqual(indexed_document_store.get_indexed_document(owner, "old.pdf")["subject_id"], subject["subject_id"])
                self.assertIsNone(indexed_document_store.get_indexed_document(other, "old.pdf")["subject_id"])   # other owner


class SubjectApiTests(SubjectStoreTestCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(subjects_api.router)
        app.dependency_overrides[require_current_user] = self.current_user
        self.client = TestClient(app)

    @staticmethod
    def current_user(request: Request) -> dict:
        return {"id": request.headers["x-user"]}

    def call(self, method, url, user=None, **kwargs):
        return self.client.request(method, url, headers={"x-user": user or self.alice}, **kwargs)

    def test_crud_and_assignment_routes(self):
        created = self.call("POST", "/api/subjects", json={"name": "Biology", "color": "#0ea5e9"})
        self.assertEqual(created.status_code, 201, created.text)
        subject_id = created.json()["subject_id"]
        self.assertEqual(self.call("POST", "/api/subjects", json={"name": "BIOLOGY"}).status_code, 409)
        self.assertEqual(self.call("POST", "/api/subjects", json={"name": " "}).status_code, 400)
        self.assertEqual(self.call("POST", "/api/subjects", json={"name": "Art", "color": "blue"}).status_code, 400)

        self.add_document(self.alice, "a.pdf")
        self.add_document(self.alice, "b.pdf")
        assigned = self.call("POST", "/api/subjects/assign", json={"document_ids": ["a.pdf", "b.pdf"], "subject_id": subject_id})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["subjects"][0]["document_count"], 2)
        missing = self.call("POST", "/api/subjects/assign", json={"document_ids": ["nope.pdf"], "subject_id": subject_id})
        self.assertEqual(missing.status_code, 404)
        unassigned = self.call("POST", "/api/subjects/assign", json={"document_ids": ["b.pdf"], "subject_id": None})
        self.assertEqual(unassigned.json()["subjects"][0]["document_ids"], ["a.pdf"])

        renamed = self.call("PATCH", f"/api/subjects/{subject_id}", json={"name": "Biology I"})
        self.assertEqual(renamed.json()["name"], "Biology I")
        listed = self.call("GET", "/api/subjects").json()
        self.assertEqual([(item["name"], item["document_count"]) for item in listed], [("Biology I", 1)])

        deleted = self.call("DELETE", f"/api/subjects/{subject_id}")
        self.assertEqual(deleted.json()["unassigned_document_ids"], ["a.pdf"])
        self.assertEqual(deleted.json()["subjects"], [])
        self.assertIsNotNone(indexed_document_store.get_indexed_document(self.alice, "a.pdf"))
        self.assertEqual(self.call("DELETE", f"/api/subjects/{subject_id}").status_code, 404)

    def test_other_users_subjects_are_404(self):
        bobs = self.call("POST", "/api/subjects", user=self.bob, json={"name": "Private"}).json()["subject_id"]
        self.add_document(self.alice, "a.pdf")
        self.assertEqual(self.call("GET", "/api/subjects").json(), [])
        self.assertEqual(self.call("PATCH", f"/api/subjects/{bobs}", json={"name": "Mine"}).status_code, 404)
        self.assertEqual(self.call("DELETE", f"/api/subjects/{bobs}").status_code, 404)
        self.assertEqual(self.call("POST", "/api/subjects/assign", json={"document_ids": ["a.pdf"], "subject_id": bobs}).status_code, 404)
        self.add_document(self.bob, "b.pdf")   # Bob's document is invisible to Alice
        self.assertEqual(self.call("POST", "/api/subjects/assign", json={"document_ids": ["b.pdf"], "subject_id": None}).status_code, 404)
        self.assertEqual(self.call("GET", "/api/subjects", user=self.bob).json()[0]["name"], "Private")

    def test_main_app_serves_the_subject_routes(self):
        from backend.main import app

        paths = {(path, method.upper()) for path, item in app.openapi()["paths"].items() for method in item}
        for expected in (("/api/subjects", "GET"), ("/api/subjects", "POST"), ("/api/subjects/{subject_id}", "PATCH"),
                         ("/api/subjects/{subject_id}", "DELETE"), ("/api/subjects/assign", "POST")):
            self.assertIn(expected, paths)


class UploadWithSubjectTests(SubjectStoreTestCase):
    def setUp(self):
        super().setUp()
        self.data_dir = Path(self.temp.name) / "data"
        self.index_calls = []
        for patcher in (
            patch.object(documents_api, "DATA_DIR", self.data_dir),
            patch.object(documents_api, "index_files", side_effect=self.fake_index_files),
            patch.object(documents_api, "list_uploaded_sources", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(documents_api.router)
        app.dependency_overrides[require_current_user] = self.current_user
        self.client = TestClient(app)

    def current_user(self, request: Request) -> dict:
        return {"id": request.headers.get("x-user", self.alice)}

    def fake_index_files(self, paths, owner_id):
        """Parsing, topics and vectors are index_files()'s job and unchanged: this stand-in only
        registers the documents like it does."""
        self.index_calls.append((owner_id, [path.name for path in paths]))
        for path in paths:
            self.add_document(owner_id, path.name)
        return {"new_files": len(paths), "new_chunks": 3 * len(paths), "skipped_files": [],
                "topic_refreshed_files": [], "indexed_documents": [path.name for path in paths],
                "total_indexed_files": len(paths)}

    def upload(self, names, data=None, user=None):
        return self.client.post("/api/sources/upload", headers={"x-user": user or self.alice}, data=data or {},
                                files=[("files", (name, b"%PDF-1.4", "application/pdf")) for name in names])

    def test_upload_without_subject_keeps_the_original_contract(self):
        response = self.upload(["one.pdf"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"new_files": 1, "new_chunks": 3, "skipped_files": [], "total_indexed_files": 1,
                                           "sources": []})
        self.assertIsNone(self.subject_of(self.alice, "one.pdf"))
        self.assertEqual(subject_store.list_subjects(self.alice), [])

    def test_multiple_files_into_an_existing_subject(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        response = self.upload(["w1.pdf", "w2.pdf"], {"subject_id": biology})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["subject_id"], response.json()["subject_document_ids"]), (biology, ["w1.pdf", "w2.pdf"]))
        self.assertEqual(self.index_calls, [(self.alice, ["w1.pdf", "w2.pdf"])])
        self.assertEqual(sorted(subject_store.get_subject(self.alice, biology)["document_ids"]), ["w1.pdf", "w2.pdf"])

    def test_upload_creates_a_new_subject_or_reuses_one_with_the_same_name(self):
        first = self.upload(["a.pdf"], {"new_subject_name": "Physics"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.upload(["b.pdf"], {"new_subject_name": "physics"})
        self.assertEqual(first.json()["subject_id"], second.json()["subject_id"])
        subjects = subject_store.list_subjects(self.alice)
        self.assertEqual([(item["name"], sorted(item["document_ids"])) for item in subjects], [("Physics", ["a.pdf", "b.pdf"])])

    def test_invalid_subject_choices_are_rejected_before_anything_is_written(self):
        bobs = subject_store.create_subject(self.bob, "Bob only")["subject_id"]
        mine = subject_store.create_subject(self.alice, "Mine")["subject_id"]
        for data, status in (({"subject_id": bobs}, 404), ({"subject_id": "missing"}, 404),
                             ({"new_subject_name": "x" * 200}, 400),
                             ({"subject_id": mine, "new_subject_name": "Other"}, 400)):
            with self.subTest(data=data):
                self.assertEqual(self.upload(["a.pdf"], data).status_code, status)
        self.assertEqual(self.index_calls, [])
        self.assertFalse((self.data_dir / "users").exists())

    def test_reuploading_a_document_never_moves_it_out_of_its_subject(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        physics = subject_store.create_subject(self.alice, "Physics")["subject_id"]
        self.add_document(self.alice, "filed.pdf")
        self.add_document(self.alice, "loose.pdf")
        subject_store.assign_documents(self.alice, ["filed.pdf"], biology)
        response = self.upload(["filed.pdf", "loose.pdf", "new.pdf"], {"subject_id": physics})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["subject_document_ids"], ["loose.pdf", "new.pdf"])   # unassigned + new ones only
        self.assertEqual(body["subject_kept_document_ids"], ["filed.pdf"])
        self.assertEqual(self.subject_of(self.alice, "filed.pdf"), biology)
        self.assertEqual(self.subject_of(self.alice, "loose.pdf"), physics)

    def test_subject_deleted_while_indexing_keeps_the_upload(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]

        def index_then_lose_subject(paths, owner_id):
            result = self.fake_index_files(paths, owner_id)
            subject_store.delete_subject(owner_id, biology)
            return result

        with patch.object(documents_api, "index_files", side_effect=index_then_lose_subject):
            response = self.upload(["a.pdf"], {"subject_id": biology})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("subject_id", response.json())
        self.assertIsNone(self.subject_of(self.alice, "a.pdf"))

    def test_partial_indexing_failure_assigns_nothing(self):
        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]

        def index_first_then_fail(paths, owner_id):
            self.add_document(owner_id, paths[0].name)   # the first file made it into the registry
            raise RuntimeError("embedding model missing")

        with patch.object(documents_api, "index_files", side_effect=index_first_then_fail):
            response = self.upload(["a.pdf", "b.pdf"], {"subject_id": biology})
            failed_new = self.upload(["c.pdf"], {"new_subject_name": "Never"})
        self.assertEqual((response.status_code, failed_new.status_code), (500, 500))
        self.assertIsNone(self.subject_of(self.alice, "a.pdf"))   # stays Unassigned; nothing half-filed
        self.assertEqual([item["name"] for item in subject_store.list_subjects(self.alice)], ["Biology"])

    def test_failed_indexing_creates_no_subject(self):
        with patch.object(documents_api, "index_files", side_effect=RuntimeError("embedding model missing")):
            response = self.upload(["a.pdf"], {"new_subject_name": "Never"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(subject_store.list_subjects(self.alice), [])


class DashboardSubjectPayloadTests(SubjectStoreTestCase):
    def test_dashboard_lists_user_subjects_and_each_materials_subject(self):
        from backend.api import learning

        biology = subject_store.create_subject(self.alice, "Biology")["subject_id"]
        for name in ("a.pdf", "b.pdf"):
            self.add_document(self.alice, name)
        subject_store.assign_documents(self.alice, ["a.pdf"], biology)
        dashboard = {"materials": [{"document_id": "a.pdf", "document_name": "a.pdf", "topic_count": 1, "assessed_topic_count": 0},
                                   {"document_id": "b.pdf", "document_name": "b.pdf", "topic_count": 1, "assessed_topic_count": 0}],
                     "metrics": {}}
        with patch.object(learning, "build_learning_dashboard", return_value=dashboard), \
             patch.object(learning.study_progress, "latest_quiz_percentages", return_value={}), \
             patch.object(learning.study_progress, "current_quiz_performance", return_value=None):
            result = learning.learning_dashboard({"id": self.alice})
        self.assertEqual({row["document_id"]: row["subject_id"] for row in result["materials"]}, {"a.pdf": biology, "b.pdf": None})
        self.assertEqual([(item["name"], item["document_ids"]) for item in result["user_subjects"]], [("Biology", ["a.pdf"])])
        self.assertIn("subjects", result)   # the name-derived grouping is still served


class SubjectProgressCountingTests(unittest.TestCase):
    """Home's per-subject progress sums each document's plan figures from study_progress; these are
    the counting rules it relies on."""

    def test_saved_sessions_count_once_and_history_is_left_out(self):
        from backend import study_progress

        def row(session_id, status, steps=None):
            return {"session_id": session_id, "document_id": "a.pdf", "activity_type": "summary", "status": status,
                    "scheduled_start": "2026-09-25T10:00:00", "scheduled_end": "2026-09-25T10:35:00",
                    "duration_minutes": 35, "steps": steps or []}

        combined = [{"activity_type": "summary", "minutes": 20, "status": "completed"},
                    {"activity_type": "flashcards", "minutes": 15, "status": "completed"}]
        totals = study_progress._session_totals([
            row("s1", "completed", combined),   # one combined session = one session, not two steps
            row("s2", "scheduled"), row("s3", "missed"),
            row("s4", "rescheduled"), row("s5", "cancelled"),   # superseded / withdrawn history
        ], "2026-09-24T09:00:00")
        self.assertEqual((totals["completed_sessions"], totals["planned_sessions"]), (1, 3))

    def test_home_sums_document_figures_per_subject(self):
        source = frontend_script_text()
        self.assertIn("function homeSubjectProgressGroups", source)
        self.assertIn("plan?.completed_sessions", source)
        self.assertIn("plan?.planned_sessions", source)


class SubjectFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = frontend_script_text()
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    def test_api_endpoints_are_declared(self):
        self.assertIn('SUBJECTS_API_URL = apiUrl("/api/subjects")', self.source)
        self.assertIn("/assign", self.source)

    def test_home_shows_my_subjects_unassigned_search_and_management(self):
        self.assertIn('id="home-subjects"', self.html)
        for needle in ("function renderHomeSubjects", "Unassigned", "My Subjects", "function renameSubject",
                       "function deleteSubject", "function moveDocumentsToSubject", "homeSubjectFilter"):
            self.assertIn(needle, self.source)
        self.assertIn("subjects-grid", self.styles)
        # Search still filters the document cards, now by title and subject name.
        self.assertIn("row.dataset.subject", self.source)

    def test_deleting_a_subject_says_documents_are_kept(self):
        self.assertIn("Its documents will be kept and moved to Unassigned", self.source)

    def test_home_cards_keep_open_and_delete(self):
        self.assertIn("openStudySession(documentId)", self.source)
        self.assertIn("deleteUploadedSource({ title: documentId }, remove)", self.source)

    def test_upload_offers_an_optional_subject(self):
        for needle in ('formData.append("subject_id"', 'formData.append("new_subject_name"', "function chooseUploadSubject"):
            self.assertIn(needle, self.source)

    def test_planner_filters_by_subject_without_touching_scheduling(self):
        self.assertIn('id="pcal-subject-filter"', self.source)
        self.assertIn("function plannerSubjectMatches", self.source)
        self.assertIn("is-subject-muted", self.source)
        self.assertIn("is-subject-muted", self.styles)
        # The filter is display-only: the scheduling inputs never read it.
        planner = (FRONTEND / "js" / "planner.js").read_text(encoding="utf-8")
        for function in ("function plannerRunPreview", "function plannerAcceptPlan", "function pcalWeekItems"):
            body = planner.split(function, 1)[1].split("\nfunction ", 1)[0].split("\nasync function ", 1)[0]
            self.assertNotIn("plannerSubjectFilter", body, function)


if __name__ == "__main__":
    unittest.main()
