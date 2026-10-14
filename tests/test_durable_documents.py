"""Durable documents: originals in (fake) Supabase Storage, vectors in real PostgreSQL + pgvector.

Unit tests always run. The integration scenarios need a PostgreSQL server with the pgvector
extension: set AI_TUTOR_TEST_POSTGRES_URL=postgresql://... Each test works in its own throw-away
schema (ai_tutor_test_<random>) that it creates and drops itself; Supabase Storage is replaced by
an in-memory fake, so nothing touches a real Supabase project.
"""

import hashlib
import io
import math
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import (auth_store, conversation_store, db, document_retrieval, document_storage, durable_setup,
                     flashcard_store, google_calendar_store, indexed_document_store, ingest, quiz_store, rag_service,
                     study_planner_store, summary_jobs, summary_store, vector_store)
from backend.api import documents as documents_api
from backend.main import app
from fake_supabase_storage import FakeSupabaseStorage, PROJECT_URL, SERVICE_KEY

POSTGRES_TEST_URL = os.getenv("AI_TUTOR_TEST_POSTGRES_URL", "").strip()
REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = REPO_ROOT / "data" / "Embedded Systems.pdf"
STORES = (auth_store, conversation_store, indexed_document_store, summary_store, summary_jobs,
          flashcard_store, quiz_store, study_planner_store)
PASSWORD = "correct-horse-battery"
BUCKET = "ai-tutor-documents"


def with_search_path(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


class HashingEmbeddings:
    """Deterministic 1024-d bag-of-words embeddings that count every call."""

    def __init__(self):
        self.document_calls = 0
        self.embedded_texts = 0
        self.query_calls = 0
        self.fail_next = False

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 1024
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 1024] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("embedding service unavailable")
        self.document_calls += 1
        self.embedded_texts += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._vector(text)


def _no_llm_refiner(_model):
    def refine(_payload):
        raise RuntimeError("LLM refinement is not used in tests")
    return refine


class FakeChat:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        return type("Response", (), {"content": "Grounded answer from the course material."})()


# -- unit tests (no database) -----------------------------------------------------------------

class VectorFilterTests(unittest.TestCase):
    def test_app_filters_translate_to_owner_scoped_sql(self):
        clauses, params = vector_store._where(
            {"$and": [{"owner_id": "u1"}, {"document_id": {"$in": ["a.pdf", "b.pdf"]}}, {"topic_id": "t1"}]})
        self.assertEqual(clauses, ["owner_id = ?", "document_id IN (?,?)", "metadata_json->>'topic_id' = ?"])
        self.assertEqual(params, ["u1", "a.pdf", "b.pdf", "t1"])
        self.assertEqual(vector_store._where({"owner_id": "u1"}), (["owner_id = ?"], ["u1"]))

    def test_unsupported_filters_are_rejected(self):
        for bad in ({"$or": []}, {"owner_id": {"$gt": 1}}, {"bad key'": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vector_store._where(bad)

    def test_wrong_embedding_dimensions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected 1024"):
            vector_store._vector_literal([0.1] * 768)


class StorageUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeSupabaseStorage()
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {**self.fake.env(), "AI_TUTOR_DOCUMENT_STORAGE": "supabase"}))
        self.stack.enter_context(patch.object(document_storage, "_http_transport", self.fake.transport))
        self.stack.enter_context(patch.object(document_storage, "DATA_DIR", Path(self.temp.name)))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_object_paths_are_owner_scoped_and_content_addressed(self):
        digest = "a" * 64
        self.assertEqual(document_storage.object_path("user-1", digest, ".PDF"), f"user-1/{digest}.pdf")
        for bad_hash, suffix in (("../etc", ".pdf"), (digest, ".exe")):
            with self.assertRaises(ValueError):
                document_storage.object_path("user-1", bad_hash, suffix)
        with self.assertRaisesRegex(document_storage.DocumentStorageError, "another owner"):
            document_storage.download("user-2", f"user-1/{digest}.pdf", Path(self.temp.name) / "x.pdf")
        with self.assertRaisesRegex(document_storage.DocumentStorageError, "another owner"):
            document_storage.delete("user-2", f"user-1/{digest}.pdf")

    def test_storage_mode_defaults_follow_the_database_backend(self):
        with patch.dict(os.environ, {"AI_TUTOR_DOCUMENT_STORAGE": ""}):
            with patch.object(db, "selected_backend", return_value=db.POSTGRES):
                self.assertEqual(document_storage.storage_mode(), "supabase")
            with patch.object(db, "selected_backend", return_value=db.SQLITE):
                self.assertEqual(document_storage.storage_mode(), "local")
        with patch.dict(os.environ, {"AI_TUTOR_DOCUMENT_STORAGE": "s3"}):
            with self.assertRaises(document_storage.DocumentStorageConfigurationError):
                document_storage.storage_mode()

    def test_missing_credentials_fail_clearly(self):
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": ""}):
            with self.assertRaisesRegex(document_storage.DocumentStorageConfigurationError, "SUPABASE_SERVICE_ROLE_KEY"):
                document_storage.ensure_bucket()

    def test_bucket_is_created_private_and_a_public_bucket_is_refused(self):
        self.fake.bucket_exists = False
        self.assertEqual(document_storage.ensure_bucket(), BUCKET)
        self.assertFalse(self.fake.buckets[BUCKET]["public"])
        self.fake.buckets[BUCKET]["public"] = True
        with self.assertRaisesRegex(document_storage.DocumentStorageConfigurationError, "public"):
            document_storage.ensure_bucket()

    def test_round_trip_verifies_hash_and_leaves_no_partial_file(self):
        source = Path(self.temp.name) / "notes.txt"
        source.write_bytes(b"durable bytes")
        digest = hashlib.sha256(b"durable bytes").hexdigest()
        stored = document_storage.upload("user-1", source, digest)
        target = Path(self.temp.name) / "cache" / "notes.txt"
        self.assertEqual(document_storage.download("user-1", stored, target, digest).read_bytes(), b"durable bytes")
        with self.assertRaisesRegex(document_storage.DocumentStorageError, "SHA-256"):
            document_storage.download("user-1", stored, Path(self.temp.name) / "bad.txt", "0" * 64)
        self.assertEqual([path.name for path in (Path(self.temp.name)).iterdir() if path.name.startswith(".download")], [])

    def test_auth_headers_match_the_key_format(self):
        import httpx

        seen = []
        transport = httpx.MockTransport(lambda request: seen.append(dict(request.headers)) or
                                        httpx.Response(200, json={"public": False}))
        with patch.object(document_storage, "_http_transport", transport):
            for key in ("eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig", "sb_secret_abc123"):
                with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": key}):
                    document_storage.ensure_bucket()
        self.assertEqual(seen[0]["authorization"], "Bearer eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig")
        self.assertEqual(seen[1]["apikey"], "sb_secret_abc123")
        self.assertNotIn("authorization", seen[1])

    def test_errors_never_contain_credentials(self):
        self.fake.fail_uploads = 1
        source = Path(self.temp.name) / "notes.txt"
        source.write_bytes(b"x")
        with self.assertRaises(document_storage.DocumentStorageError) as caught:
            document_storage.upload("user-1", source, hashlib.sha256(b"x").hexdigest())
        self.assertEqual(str(caught.exception), "Supabase Storage upload failed (HTTP 500).")
        self.assertNotIn(SERVICE_KEY, str(caught.exception))


# -- integration (real PostgreSQL + pgvector) ---------------------------------------------------

@unittest.skipUnless(POSTGRES_TEST_URL, "set AI_TUTOR_TEST_POSTGRES_URL (PostgreSQL with pgvector)")
class DurableDocumentTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.schema = f"ai_tutor_test_{uuid4().hex[:12]}"
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(self.drop_schema)
        self.storage = FakeSupabaseStorage()
        self.embeddings = HashingEmbeddings()
        self.stack = ExitStack()
        environment = {**self.storage.env(), "DATABASE_URL": with_search_path(POSTGRES_TEST_URL, self.schema),
                       "AI_TUTOR_DOCUMENT_STORAGE": ""}
        self.stack.enter_context(patch.dict(os.environ, environment))
        local_db = Path(self.temp.name) / "unused.db"
        for store in STORES:
            self.stack.enter_context(patch.object(store, "DATABASE_PATH", local_db))
        for module in (document_storage, ingest, documents_api):
            self.stack.enter_context(patch.object(module, "DATA_DIR", self.data_dir))
        self.stack.enter_context(patch.object(ingest, "VECTORSTORE_DIR", Path(self.temp.name) / "vectorstore"))
        self.stack.enter_context(patch.object(document_storage, "_http_transport", self.storage.transport))
        self.stack.enter_context(patch.object(vector_store, "_embeddings", lambda: self.embeddings))
        self.stack.enter_context(patch.object(ingest, "ollama_heading_refiner", _no_llm_refiner))
        self.stack.enter_context(patch.object(rag_service, "ChatOllama", FakeChat))
        self.stack.enter_context(patch.object(rag_service, "resolve_generation_model", lambda model_id=None: "test-model"))
        self.stack.enter_context(patch.object(db, "_schema_ready", set()))
        self.stack.enter_context(patch.object(db, "_announced", set()))  # each test is a "new process"
        self.stack.enter_context(patch.object(vector_store, "_vector_schemas", {}))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))

    def tearDown(self):
        self.stack.close()
        db.close_pools()
        self.temp.cleanup()

    def drop_schema(self):
        import psycopg

        db.close_pools()
        assert self.schema.startswith("ai_tutor_test_")
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")

    # -- helpers ----------------------------------------------------------------------------------

    def fresh_runtime(self):
        """A new Kaggle session: the local disk, pooled connections and caches are all gone."""
        db.close_pools()
        db._schema_ready.clear()
        vector_store._vector_schemas.clear()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.embeddings = HashingEmbeddings()

    def client_for(self, email: str) -> tuple[TestClient, str]:
        client = TestClient(app)
        response = client.post("/api/auth/signup", json={"display_name": "Learner", "email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        return client, response.json()["id"]

    def login(self, email: str) -> tuple[TestClient, str]:
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client, response.json()["id"]

    def upload(self, client: TestClient, name: str, content: bytes, content_type="application/pdf"):
        return client.post("/api/sources/upload", files=[("files", (name, content, content_type))])

    def chunk_rows(self, owner_id: str | None = None) -> list:
        with db.connect("unused.db") as connection:
            sql = "SELECT owner_id, document_id, vector_id, embedding_model, embedding_dimensions FROM document_chunks"
            params = ()
            if owner_id:
                sql += " WHERE owner_id = ?"
                params = (owner_id,)
            return connection.execute(sql + " ORDER BY vector_id", params).fetchall()

    def text_document(self, topic: str) -> bytes:
        paragraphs = [f"Section {index}: {topic} details paragraph {index}. " * 12 for index in range(1, 7)]
        return "\n\n".join(paragraphs).encode("utf-8")

    # -- scenarios --------------------------------------------------------------------------------

    def test_uploaded_pdf_survives_a_fresh_runtime_without_reupload_or_reembedding(self):
        pdf = SAMPLE_PDF.read_bytes()
        digest = hashlib.sha256(pdf).hexdigest()
        client, owner = self.client_for("alice@example.com")
        response = self.upload(client, SAMPLE_PDF.name, pdf)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["new_files"], 1)
        self.assertEqual(self.storage.objects[(BUCKET, f"{owner}/{digest}.pdf")], pdf)
        document = indexed_document_store.get_indexed_document(owner, SAMPLE_PDF.name)
        self.assertEqual((document["object_path"], document["object_bucket"]), (f"{owner}/{digest}.pdf", BUCKET))
        rows = self.chunk_rows(owner)
        self.assertEqual(len(rows), document["chunks"])
        self.assertEqual({(row["embedding_model"], row["embedding_dimensions"]) for row in rows}, {("bge-m3", 1024)})
        before_chunks = document_retrieval.get_document_chunks(SAMPLE_PDF.name, owner)
        conversation = conversation_store.create_conversation(owner, "Study", [SAMPLE_PDF.name])
        rag_service.answer_conversation_message(owner, conversation["id"], "What is an embedded system?")

        self.fresh_runtime()
        client, same_owner = self.login("ALICE@example.com")
        self.assertEqual(same_owner, owner)
        sources = client.get("/api/sources").json()
        self.assertEqual([source["title"] for source in sources], [SAMPLE_PDF.name])
        served = client.get(f"/api/sources/{SAMPLE_PDF.name}/content")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, pdf)
        self.assertEqual(served.headers["content-type"], "application/pdf")

        after_chunks = document_retrieval.get_document_chunks(SAMPLE_PDF.name, owner)
        self.assertEqual([(c["content"], c["metadata"]["page"], c["metadata"]["chunk_id"]) for c in after_chunks],
                         [(c["content"], c["metadata"]["page"], c["metadata"]["chunk_id"]) for c in before_chunks])
        answer = rag_service.answer_conversation_message(owner, conversation["id"], "What is an embedded system?")
        self.assertEqual(answer["grounding_status"], "supported")
        self.assertTrue(answer["citations"])
        self.assertEqual({citation["title"] for citation in answer["citations"]}, {SAMPLE_PDF.name})
        self.assertTrue(all(citation["page"] not in ("", "Unknown page") for citation in answer["citations"]))
        stored = conversation_store.get_conversation(owner, conversation["id"])
        self.assertEqual(len(stored["messages"]), 4)
        self.assertEqual(self.embeddings.document_calls, 0)  # nothing was re-embedded
        self.assertGreater(self.embeddings.query_calls, 0)

    def test_retrieval_ranks_the_relevant_chunk_and_stays_in_the_document(self):
        client, owner = self.client_for("alice@example.com")
        self.upload(client, "networks.txt", b"Routers forward packets between networks using routing tables. " * 30,
                    "text/plain")
        self.upload(client, "biology.txt", b"Mitochondria produce cellular energy through respiration. " * 30,
                    "text/plain")
        docs = rag_service._retrieve_conversation_docs(owner, "how do routers forward packets", ["networks.txt"])
        self.assertTrue(docs)
        self.assertEqual({doc.metadata["document_id"] for doc in docs}, {"networks.txt"})
        self.assertIn("Routers", docs[0].page_content)
        scored = vector_store.get_vectorstore().similarity_search_with_score(
            "mitochondria energy", k=2, filter={"$and": [{"owner_id": owner}, {"document_id": {"$in": ["networks.txt", "biology.txt"]}}]})
        self.assertEqual(scored[0][0].metadata["document_id"], "biology.txt")
        self.assertLessEqual(scored[0][1], scored[-1][1])

    def test_users_cannot_reach_each_others_documents(self):
        alice_client, alice = self.client_for("alice@example.com")
        self.upload(alice_client, "private.txt", self.text_document("secret routing"), "text/plain")
        bob_client, bob = self.client_for("bob@example.com")
        self.assertEqual(bob_client.get("/api/sources").json(), [])
        self.assertEqual(bob_client.get("/api/sources/private.txt/content").status_code, 404)
        self.assertEqual(bob_client.delete("/api/sources/private.txt").status_code, 404)
        self.assertEqual(rag_service._retrieve_conversation_docs(bob, "secret routing", ["private.txt"]), [])
        self.assertEqual(document_retrieval.get_document_chunks("private.txt", bob), [])
        self.assertEqual(self.chunk_rows(bob), [])
        self.assertTrue(self.chunk_rows(alice))
        self.assertTrue(all(key.startswith(f"{alice}/") for key in self.storage.object_keys(BUCKET)))
        self.assertEqual(alice_client.get("/api/sources/private.txt/content").status_code, 200)

    def test_duplicate_upload_is_idempotent(self):
        client, owner = self.client_for("alice@example.com")
        content = self.text_document("scheduling")
        self.assertEqual(self.upload(client, "sched.txt", content, "text/plain").json()["new_files"], 1)
        uploads, embedded, rows = self.storage.count("POST"), self.embeddings.embedded_texts, self.chunk_rows(owner)
        second = self.upload(client, "sched.txt", content, "text/plain").json()
        self.assertEqual((second["new_files"], second["skipped_files"]), (0, ["sched.txt"]))
        self.assertEqual((self.storage.count("POST"), self.embeddings.embedded_texts), (uploads, embedded))
        self.assertEqual(self.chunk_rows(owner), rows)

    def test_replacing_a_document_swaps_its_original_and_vectors(self):
        client, owner = self.client_for("alice@example.com")
        old, new = self.text_document("version one"), self.text_document("version two rewritten")
        self.upload(client, "notes.txt", old, "text/plain")
        old_key = f"{owner}/{hashlib.sha256(old).hexdigest()}.txt"
        self.assertIn(old_key, self.storage.object_keys(BUCKET))
        self.assertEqual(self.upload(client, "notes.txt", new, "text/plain").status_code, 200)
        new_key = f"{owner}/{hashlib.sha256(new).hexdigest()}.txt"
        self.assertEqual(self.storage.object_keys(BUCKET), {new_key})
        new_hash = hashlib.sha256(new).hexdigest()
        self.assertTrue(all(new_hash in row["vector_id"] for row in self.chunk_rows(owner)))
        self.assertEqual(indexed_document_store.get_indexed_document(owner, "notes.txt")["object_path"], new_key)
        self.fresh_runtime()
        client, _ = self.login("alice@example.com")
        self.assertEqual(client.get("/api/sources/notes.txt/content").content, new)

    def test_storage_failure_records_nothing(self):
        client, owner = self.client_for("alice@example.com")
        self.storage.fail_uploads = 1
        response = self.upload(client, "notes.txt", self.text_document("lost"), "text/plain")
        self.assertEqual(response.status_code, 503)
        self.assertIn("durable storage", response.json()["detail"])
        self.assertIsNone(indexed_document_store.get_indexed_document(owner, "notes.txt"))
        self.assertEqual(self.chunk_rows(owner), [])
        self.assertEqual(self.storage.object_keys(BUCKET), set())
        self.assertEqual(self.embeddings.embedded_texts, 0)

    def test_indexing_failure_leaves_no_orphaned_object_and_retry_succeeds(self):
        client, owner = self.client_for("alice@example.com")
        content = self.text_document("retry")
        self.embeddings.fail_next = True
        self.assertEqual(self.upload(client, "retry.txt", content, "text/plain").status_code, 500)
        self.assertEqual(self.storage.object_keys(BUCKET), set())
        self.assertIsNone(indexed_document_store.get_indexed_document(owner, "retry.txt"))
        self.assertEqual(self.chunk_rows(owner), [])
        retried = self.upload(client, "retry.txt", content, "text/plain")
        self.assertEqual(retried.json()["new_files"], 1)
        self.assertEqual(len(self.storage.object_keys(BUCKET)), 1)
        self.assertEqual(len(self.chunk_rows(owner)), indexed_document_store.get_indexed_document(owner, "retry.txt")["chunks"])

    def test_deleting_a_document_removes_its_original_vectors_and_record(self):
        client, owner = self.client_for("alice@example.com")
        self.upload(client, "gone.txt", self.text_document("temporary"), "text/plain")
        self.upload(client, "kept.txt", self.text_document("permanent"), "text/plain")
        self.assertEqual(client.delete("/api/sources/gone.txt").status_code, 200)
        self.assertEqual({row["document_id"] for row in self.chunk_rows(owner)}, {"kept.txt"})
        self.assertEqual(len(self.storage.object_keys(BUCKET)), 1)
        self.storage.fail_deletes = 1  # storage outage: the record still goes, the object is reported
        self.assertEqual(client.delete("/api/sources/kept.txt").status_code, 200)
        self.assertIsNone(indexed_document_store.get_indexed_document(owner, "kept.txt"))
        self.assertEqual(self.chunk_rows(owner), [])

    def test_missing_storage_credentials_block_uploads(self):
        client, owner = self.client_for("alice@example.com")
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": ""}):
            response = self.upload(client, "notes.txt", self.text_document("config"), "text/plain")
            self.assertEqual(response.status_code, 503)
            self.assertIn("SUPABASE_SERVICE_ROLE_KEY", response.json()["detail"])
            self.assertEqual(durable_setup.main(), 1)
        self.assertIsNone(indexed_document_store.get_indexed_document(owner, "notes.txt"))

    def test_durable_setup_initializes_everything_and_locks_out_api_roles(self):
        import psycopg

        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(durable_setup.main(), 0)
            self.assertEqual(durable_setup.main(), 0)  # idempotent
        text = output.getvalue()
        self.assertIn("[db] backend=postgres connected", text)
        self.assertIn("[vectors] backend=pgvector schema=extensions model=bge-m3 dimensions=1024", text)
        self.assertIn(f"[storage] backend=supabase bucket={BUCKET} private=true", text)
        self.assertIn("revoked anon, authenticated", text)
        for secret in (SERVICE_KEY, PROJECT_URL, POSTGRES_TEST_URL):
            self.assertNotIn(secret, text)
        with psycopg.connect(with_search_path(POSTGRES_TEST_URL, self.schema), autocommit=True) as connection:
            unlocked = connection.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind = 'r' AND NOT c.relrowsecurity", (self.schema,)).fetchall()
            self.assertEqual(unlocked, [])
            connection.execute(f"GRANT USAGE ON SCHEMA {self.schema} TO anon")
            connection.execute("SET ROLE anon")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM users")
            connection.execute("RESET ROLE")
        # The backend (table owner) is unaffected by row level security.
        auth_store.create_user("Owner", "owner@example.com", PASSWORD)

    def test_existing_relational_data_reads_after_restart(self):
        client, owner = self.client_for("alice@example.com")
        self.upload(client, "study.txt", self.text_document("planning"), "text/plain")
        plan = study_planner_store.create_plan(owner, "Finals")
        study_planner_store.add_material(owner, plan["plan_id"], "study.txt")
        self.fresh_runtime()
        self.assertEqual([item["plan_id"] for item in study_planner_store.list_plans(owner)], [plan["plan_id"]])
        self.assertEqual(google_calendar_store.get_connection(owner), None)
        self.assertEqual(len(document_retrieval.get_document_chunks("study.txt", owner)),
                         indexed_document_store.get_indexed_document(owner, "study.txt")["chunks"])


if __name__ == "__main__":
    unittest.main()
