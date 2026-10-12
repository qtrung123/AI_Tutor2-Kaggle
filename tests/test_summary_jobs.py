"""Summary generation runs as a background job so no HTTP request stays open while the LLM runs.

Root cause this covers: GET /api/summary/{id} generated synchronously (two long LLM calls), so behind
Cloudflare the request hit 524 even though the backend kept going and saved the summary. Now
POST /api/summary/generate returns 202 + job_id at once and the client polls /api/summary/jobs/{id}.
"""

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth_store, indexed_document_store, summary_jobs, summary_service, summary_store
from backend.api import summary as summary_api
from backend.api.deps import require_current_user


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5}
        self.response_metadata = {}


class FakeLlm:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        if "ORDERED TOPIC SUMMARIES" in prompt:
            return FakeResponse('{"overview":"Whole document overview.","key_takeaways":["One point","Two point","Three point","Four point"]}')
        return FakeResponse('{"topics":[{"topic_id":"topic_a","overview":"Evidence topic_a is summarized.","subsections":[]}]}')


class SummaryJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "summary_jobs.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(summary_store, "DATABASE_PATH", self.db),
            patch.object(summary_jobs, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
            patch.object(summary_jobs, "prepare_generation_model", return_value={}),
            patch.object(summary_service, "ChatOllama", FakeLlm),
            patch.object(summary_service, "resolve_generation_model", side_effect=lambda model: f"runtime-{model}"),
            patch.object(summary_service, "get_topic_chunks", side_effect=self.chunks),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-jobs@example.com", "long-password-a")
        self.bob = auth_store.create_user("Bob", "bob-jobs@example.com", "long-password-b")
        for owner in (self.alice["id"], self.bob["id"]):
            for document_id in ("lecture.pdf", "notes.pdf"):
                indexed_document_store.upsert_indexed_document(owner, document_id, {
                    "hash": f"hash-{document_id}", "chunks": 1, "path": document_id, "topic_schema_version": 3,
                    "topics": [{"topic_id": "topic_a", "name": "Topic A"}],
                })
        self.user = self.alice
        app = FastAPI()
        app.include_router(summary_api.router)
        app.dependency_overrides[require_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.gate = threading.Event()
        self.calls = []

    def tearDown(self):
        self.gate.set()   # never leave a worker blocked
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(document_id, topic_id, owner_id):
        return [{"content": f"Evidence {topic_id}", "metadata": {"owner_id": owner_id, "document_id": document_id,
                                                               "topic_id": topic_id, "chunk_id": f"{topic_id}-1", "chunk": 0}}]

    def slow_generation(self, owner_id, document_id, model_id=None, regenerate=False):
        """Real generation + persistence, held at the gate to stand in for a slow LLM."""
        self.calls.append(document_id)
        self.assertTrue(self.gate.wait(10))
        return summary_service.generate_document_summary(owner_id, document_id, model_id=model_id, regenerate=regenerate)

    def start(self, document_id="lecture.pdf", regenerate=False):
        return self.client.post("/api/summary/generate", json={"document_id": document_id, "model_id": "model-a",
                                                               "regenerate": regenerate})

    def wait_for(self, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/summary/jobs/{job_id}").json()
            if body["status"] != "processing":
                return body
            time.sleep(0.02)
        self.fail(f"job {job_id} stayed processing")

    def test_start_returns_202_at_once_and_generation_continues_after_the_response(self):
        with patch.object(summary_jobs, "generate_document_summary", side_effect=self.slow_generation):
            started = time.monotonic()
            response = self.start()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(response.status_code, 202)
            body = response.json()
            self.assertEqual((body["status"], body["document_id"]), ("processing", "lecture.pdf"))
            self.assertEqual(self.client.get(f"/api/summary/jobs/{body['job_id']}").json(),
                             {"status": "processing", "job_id": body["job_id"]})
            self.gate.set()   # the "LLM" finishes only after the start request has returned
            done = self.wait_for(body["job_id"])
        self.assertEqual(done["status"], "complete")
        self.assertEqual(done["summary"]["final_summary"]["overview"], "Whole document overview.")

    def test_completed_job_returns_the_existing_persisted_summary_format(self):
        job_id = self.start().json()["job_id"]
        done = self.wait_for(job_id)
        self.assertEqual(done["status"], "complete")
        identity = {"owner_id": self.alice["id"], "document_id": "lecture.pdf", "document_hash": "hash-lecture.pdf",
                    "topic_schema_version": 3, "summary_version": summary_service.SUMMARY_VERSION,
                    "model_id": "model-a", "runtime_model": "runtime-model-a"}
        persisted = summary_store.get_compatible_summary(identity)
        self.assertEqual(done["summary"]["summary_id"], persisted["summary_id"])
        self.assertEqual(done["summary"]["topic_summaries"], persisted["topic_summaries"])
        self.assertEqual(done["summary"]["final_summary"], persisted["final_summary"])
        self.assertFalse(done["summary"]["cache_hit"])
        # An already-completed summary is returned immediately, exactly like the cached lookup.
        again = self.start()
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["cache_hit"])
        self.assertEqual(again.json()["summary_id"], persisted["summary_id"])
        self.assertEqual(again.json(), self.client.get("/api/summary/lecture.pdf?model_id=model-a&cache_only=true").json())

    def test_failed_generation_marks_the_job_failed_with_a_safe_error(self):
        with patch.object(summary_jobs, "generate_document_summary", side_effect=RuntimeError("model crashed")):
            failed = self.wait_for(self.start().json()["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "generation_failed")
        self.assertIn("model crashed", failed["error"]["message"])
        with patch.object(summary_jobs, "generate_document_summary",
                          side_effect=ValueError("Summary model omitted the final overview.")):
            failed = self.wait_for(self.start().json()["job_id"])
        self.assertEqual(failed["error"], {"code": "invalid_summary", "message": "Summary model omitted the final overview."})

    def test_duplicate_generate_while_processing_reuses_the_job_and_runs_one_generation(self):
        with patch.object(summary_jobs, "generate_document_summary", side_effect=self.slow_generation):
            first = self.start().json()["job_id"]
            second = self.start().json()["job_id"]
            third = self.start(regenerate=True).json()["job_id"]
            self.assertEqual(first, second)
            self.assertEqual(first, third)
            self.gate.set()
            self.assertEqual(self.wait_for(first)["status"], "complete")
        self.assertEqual(self.calls, ["lecture.pdf"])

    def test_refresh_discovers_the_active_job_for_the_document(self):
        with patch.object(summary_jobs, "generate_document_summary", side_effect=self.slow_generation):
            job_id = self.start().json()["job_id"]
            active = self.client.get("/api/summary/jobs?document_id=lecture.pdf&model_id=model-a").json()
            self.assertEqual(active, {"status": "processing", "job_id": job_id, "document_id": "lecture.pdf"})
            self.assertEqual(self.client.get("/api/summary/jobs?document_id=lecture.pdf&model_id=model-b").json(), {"status": "none"})
            self.user = self.bob   # another user never sees it
            self.assertEqual(self.client.get("/api/summary/jobs?document_id=lecture.pdf&model_id=model-a").json(), {"status": "none"})
            self.assertEqual(self.client.get(f"/api/summary/jobs/{job_id}").status_code, 404)
            self.user = self.alice
            self.gate.set()
            self.wait_for(job_id)
        self.assertEqual(self.client.get("/api/summary/jobs?document_id=lecture.pdf&model_id=model-a").json(), {"status": "none"})

    def test_two_documents_run_separate_jobs(self):
        with patch.object(summary_jobs, "generate_document_summary", side_effect=self.slow_generation):
            lecture = self.start("lecture.pdf").json()["job_id"]
            notes = self.start("notes.pdf").json()["job_id"]
            self.assertNotEqual(lecture, notes)
            self.gate.set()
            self.assertEqual(self.wait_for(lecture)["summary"]["document_id"], "lecture.pdf")
            self.assertEqual(self.wait_for(notes)["summary"]["document_id"], "notes.pdf")
        self.assertEqual(sorted(self.calls), ["lecture.pdf", "notes.pdf"])

    def test_single_worker_queues_the_second_document_without_marking_it_stale(self):
        self.assertEqual(summary_jobs.MAX_SUMMARY_WORKERS, 1)
        with patch.object(summary_jobs, "generate_document_summary", side_effect=self.slow_generation):
            lecture = self.start("lecture.pdf").json()["job_id"]
            notes = self.start("notes.pdf").json()["job_id"]
            deadline = time.monotonic() + 5
            while not self.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)   # give a (wrongly) concurrent second worker time to start
            self.assertEqual(self.calls, ["lecture.pdf"])   # the first blocks; the second only waits in the queue
            queued = self.client.get(f"/api/summary/jobs/{notes}").json()
            self.assertEqual(queued, {"status": "processing", "job_id": notes})   # live, never "interrupted"
            self.assertEqual(self.client.get("/api/summary/jobs?document_id=notes.pdf&model_id=model-a").json()["job_id"], notes)
            self.gate.set()
            self.assertEqual(self.wait_for(lecture)["status"], "complete")
            self.assertEqual(self.wait_for(notes)["status"], "complete")
        self.assertEqual(self.calls, ["lecture.pdf", "notes.pdf"])   # the second ran after the first

    def test_processing_job_left_by_a_restart_is_failed_and_retryable(self):
        summary_jobs.initialize_summary_job_store()
        fingerprint = summary_jobs.summary_job_fingerprint(self.alice["id"], "lecture.pdf", "model-a")
        with closing(sqlite3.connect(self.db)) as connection, connection:   # a row from a previous process; no live worker owns it
            connection.execute(
                "INSERT INTO summary_jobs (job_id, owner_id, document_id, model_id, fingerprint, status, created_at, updated_at)"
                " VALUES ('orphan', ?, 'lecture.pdf', 'model-a', ?, 'processing', '2026-01-01', '2026-01-01')",
                (self.alice["id"], fingerprint))
        orphan = self.client.get("/api/summary/jobs/orphan").json()
        self.assertEqual((orphan["status"], orphan["error"]["code"]), ("failed", "interrupted"))
        retry = self.start()
        self.assertEqual(retry.status_code, 202)
        self.assertNotEqual(retry.json()["job_id"], "orphan")
        self.assertEqual(self.wait_for(retry.json()["job_id"])["status"], "complete")

    def test_missing_document_is_rejected_without_creating_a_job(self):
        response = self.start("absent.pdf")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
