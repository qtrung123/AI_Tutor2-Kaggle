import gc
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from backend import auth_store, conversation_store, indexed_document_store, quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.ingest import migrate_legacy_vector_ownership
from backend.main import QuizProgressRequest, app
from backend.rag_service import _chroma_filter
from backend.quiz_service import update_quiz_progress
from backend.mastery_service import recompute_topic_mastery


class DeterministicEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0, 0.5] for text in texts]

    def embed_query(self, text):
        return [float(len(text) % 7), 1.0, 0.5]


def sample_quiz(owner: str, quiz_id: str) -> dict:
    return {
        "quiz_id": quiz_id, "document_id": "shared.pdf", "title": "Shared",
        "difficulty": "easy", "topic_id": "topic_001", "topic_name": "Topic",
        "questions": [{
            "id": 1, "question": "Which grounded answer is correct here?",
            "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
            "topic_id": "topic_001", "topic_name": "Topic", "difficulty": "easy",
            "explanation": "Supported.", "source_chunk_ids": [f"{owner}_hash_0"],
        }],
    }


class AuthenticationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database_path = root / "auth.db"
        self.index_json = root / "indexed_files.json"
        self.patchers = [
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
            patch.object(quiz_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", self.index_json),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp_dir.cleanup()

    def user(self, suffix: str) -> dict:
        return auth_store.create_user(f"User {suffix}", f"{suffix}@example.com", "correct-horse-123")

    def test_passwords_and_raw_session_tokens_are_never_stored(self):
        user = self.user("secure")
        token, _session = auth_store.create_session(user["id"])
        connection = sqlite3.connect(self.database_path)
        try:
            password_hash = connection.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
            token_hash = connection.execute("SELECT token_hash FROM auth_sessions WHERE user_id = ?", (user["id"],)).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(password_hash, "correct-horse-123")
        self.assertTrue(password_hash.startswith("$argon2id$"))
        self.assertNotEqual(token_hash, token)
        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(auth_store.get_user_for_session(token)["id"], user["id"])

    def test_auth_routes_set_httponly_cookie_and_protect_data_routes(self):
        with TestClient(app) as client:
            unauthorized = client.get("/api/dashboard")
            self.assertEqual(unauthorized.status_code, 401)
            signup = client.post("/api/auth/signup", json={
                "display_name": "Alice", "email": "Alice@Example.com", "password": "long-password-123",
            })
            self.assertEqual(signup.status_code, 201)
            cookie = signup.headers["set-cookie"].lower()
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=lax", cookie)
            self.assertEqual(client.get("/api/auth/me").json()["email"], "alice@example.com")
            self.assertEqual(client.get("/api/dashboard").status_code, 200)
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_duplicate_email_is_case_insensitive_and_student_id_is_not_client_controlled(self):
        self.user("duplicate")
        with self.assertRaisesRegex(ValueError, "already exists"):
            auth_store.create_user("Other", "DUPLICATE@example.com", "another-password-123")
        self.assertNotIn("student_id", QuizProgressRequest.model_fields)

    def test_conversations_are_isolated_between_users(self):
        first, second = self.user("first"), self.user("second")
        conversation = conversation_store.create_conversation(first["id"], "Private", ["a.pdf"])
        self.assertEqual(len(conversation_store.list_conversations(first["id"])), 1)
        self.assertEqual(conversation_store.list_conversations(second["id"]), [])
        with self.assertRaisesRegex(ValueError, "not found"):
            conversation_store.get_conversation(second["id"], conversation["id"])
        with self.assertRaisesRegex(ValueError, "not found"):
            conversation_store.delete_conversation(second["id"], conversation["id"])
        self.assertEqual(conversation_store.get_conversation(first["id"], conversation["id"])["title"], "Private")

    def test_documents_quizzes_attempts_and_mastery_are_isolated(self):
        first, second = self.user("owner1"), self.user("owner2")
        for user in (first, second):
            indexed_document_store.upsert_indexed_document(user["id"], "shared.pdf", {
                "hash": "samehash", "chunks": 1, "path": f"/{user['id']}/shared.pdf",
                "topic_schema_version": 1, "topics": [{"topic_id": "topic_001", "name": "Topic"}],
            })
        quiz_store.save_quiz("shared.pdf", "easy", sample_quiz(first["id"], "quiz-first"), first["id"])
        quiz_store.save_quiz("shared.pdf", "easy", sample_quiz(second["id"], "quiz-second"), second["id"])
        self.assertEqual(quiz_store.get_quiz("shared.pdf", "easy", "topic_001", first["id"])["quiz_id"], "quiz-first")
        self.assertEqual(quiz_store.get_quiz("shared.pdf", "easy", "topic_001", second["id"])["quiz_id"], "quiz-second")
        update_quiz_progress("shared.pdf", "easy", "topic_001", 1, "A", first["id"])
        update_quiz_progress("shared.pdf", "easy", "topic_001", 1, "B", second["id"])
        self.assertEqual(recompute_topic_mastery(first["id"], "shared.pdf", "topic_001")["mastery_score"], 100.0)
        self.assertEqual(recompute_topic_mastery(second["id"], "shared.pdf", "topic_001")["mastery_score"], 0.0)
        self.assertEqual(len(quiz_store.list_quiz_history(student_id=first["id"])), 1)
        self.assertEqual(len(quiz_store.list_quiz_history(student_id=second["id"])), 1)
        self.assertEqual(len(indexed_document_store.list_indexed_documents(first["id"])), 1)
        indexed_document_store.delete_indexed_document(first["id"], "shared.pdf")
        self.assertEqual(indexed_document_store.list_indexed_documents(first["id"]), [])
        self.assertEqual(len(indexed_document_store.list_indexed_documents(second["id"])), 1)

    def test_chroma_owner_filter_prevents_cross_user_retrieval(self):
        store = Chroma(
            collection_name=f"auth_isolation_{uuid4().hex}",
            embedding_function=DeterministicEmbeddings(),
        )
        store.add_documents([
            Document(page_content="Alice private material", metadata={"owner_id": "alice", "document_id": "same.pdf", "topic_id": "topic_001"}),
            Document(page_content="Bob private material", metadata={"owner_id": "bob", "document_id": "same.pdf", "topic_id": "topic_001"}),
        ], ids=["alice_hash_0", "bob_hash_0"])
        alice = store.get(where=_chroma_filter("alice", ["same.pdf"], "topic_001"))
        bob = store.get(where=_chroma_filter("bob", ["same.pdf"], "topic_001"))
        store.delete_collection()
        del store
        gc.collect()
        self.assertEqual(alice["ids"], ["alice_hash_0"])
        self.assertEqual(bob["ids"], ["bob_hash_0"])

    def test_legacy_registry_and_vectors_migrate_without_changing_chunk_ids(self):
        self.index_json.write_text(json.dumps({
            "legacy.pdf": {"hash": "oldhash", "chunks": 1, "path": "legacy.pdf", "topic_schema_version": 2, "topics": []}
        }), encoding="utf-8")
        legacy_documents = indexed_document_store.list_indexed_documents(LEGACY_USER_ID)
        self.assertEqual(legacy_documents[0]["document_id"], "legacy.pdf")
        store = Chroma(
            collection_name=f"legacy_auth_{uuid4().hex}",
            embedding_function=DeterministicEmbeddings(),
        )
        store.add_documents([Document(page_content="Legacy", metadata={"source": "legacy.pdf", "chunk_id": "oldhash_0"})], ids=["oldhash_0"])
        migrated = migrate_legacy_vector_ownership(store)
        result = store.get(ids=["oldhash_0"])
        store.delete_collection()
        self.assertEqual(migrated, 1)
        self.assertEqual(result["ids"], ["oldhash_0"])
        self.assertEqual(result["metadatas"][0]["owner_id"], LEGACY_USER_ID)
        self.assertEqual(result["metadatas"][0]["document_id"], "legacy.pdf")

    def test_existing_conversations_are_backfilled_to_legacy_user(self):
        auth_store.initialize_auth_store()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO conversations VALUES ('legacy-conversation', 'Legacy', 'now', 'now')"
            )
            connection.commit()
        finally:
            connection.close()
        conversation_store.initialize_conversation_store()
        migrated = conversation_store.get_conversation(LEGACY_USER_ID, "legacy-conversation", include_messages=False)
        self.assertEqual(migrated["owner_id"], LEGACY_USER_ID)

    def test_frontend_has_auth_gate_without_client_token_storage(self):
        root = Path(__file__).parents[1]
        markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="auth-screen"', markup)
        self.assertIn('id="app-shell" hidden', markup)
        self.assertIn('id="logout-button"', markup)
        self.assertIn('id="profile-display-name"', markup)
        self.assertIn('credentials: "include"', script)
        self.assertNotIn('localStorage.setItem("auth', script)
        self.assertNotIn('localStorage.setItem("token', script)


if __name__ == "__main__":
    unittest.main()
