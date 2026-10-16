"""PDF-PERF-2: opt-in stage timing for upload/indexing (backend/perf_timing.StageTimer).

Off by default (no lines, shared no-op stage context). With AI_TUTOR_PERF_TIMING=1 the real upload
route and the real index_files() -- with stand-ins for storage, the embedding model, PostgreSQL and
the Qwen heading refiner -- print one [ingest] line per file and one [upload] line per request,
holding only stage times and counts: never file names, document/user ids or content.
"""

import io
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import document_storage, ingest, perf_timing, topic_extractor, vector_store
from backend.api import documents as documents_api
from backend.api.deps import require_current_user

OWNER_ID = "owner-secret-7f3a"
FILE_NAME = "secret-lecture-name.txt"
CONTENT_MARKER = "CONFIDENTIALPHRASE"
TEXT = "\n".join([
    "Memory Paging Basics",
    f"Paging splits memory into fixed size frames. {CONTENT_MARKER} " + "Pages map to frames. " * 40,
    "Virtual Address Translation",
    "The page table translates virtual pages to physical frames. " * 40,
])
EMBED_SECONDS, STORAGE_SECONDS, REFINE_SECONDS = 0.05, 0.03, 0.02
LINE = re.compile(r"^\[(ingest|upload)\] .*$", re.M)


def timing_env(enabled: bool):
    environment = {key: value for key, value in os.environ.items() if key != "AI_TUTOR_PERF_TIMING"}
    if enabled:
        environment["AI_TUTOR_PERF_TIMING"] = "1"
    return patch.dict(os.environ, environment, clear=True)


def fields(line: str) -> dict:
    return dict(part.split("=", 1) for part in line.split()[1:])


class FakeEmbeddings:
    def embed_documents(self, texts):
        time.sleep(EMBED_SECONDS)
        return [[0.0] * vector_store.EMBEDDING_DIMENSIONS for _ in texts]


class FakeCursorResult:
    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.inserted = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        self.inserted += len(list(rows))

    def execute(self, sql, params=()):
        return FakeCursorResult()


def fake_pgvector_store():
    """The real PgVectorStore.add_documents/get/delete, over a fake connection and embedder."""
    store = vector_store.PgVectorStore.__new__(vector_store.PgVectorStore)
    store._embedding_function = FakeEmbeddings()
    store._schema = "extensions"
    store.connection = FakeConnection()
    store._connect = lambda: store.connection
    return store


def fake_refiner_factory(_model):
    def refine(payload):
        time.sleep(REFINE_SECONDS)
        return []   # rejected by validation -> deterministic topics, like a truncated Qwen reply
    return refine


class IngestTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = fake_pgvector_store()
        self.upload_error = None
        self.indexed_registry = {}
        topic_extractor._HIERARCHY_CACHE.clear()

        def storage_upload(owner_id, path, file_hash):
            time.sleep(STORAGE_SECONDS)
            if self.upload_error:
                raise self.upload_error
            return f"{owner_id}/{file_hash}.txt"

        for patcher in (
            patch.object(documents_api, "DATA_DIR", root / "data"),
            patch.object(documents_api, "include_planner_documents", return_value={"status": "unchanged"}),
            patch.object(documents_api, "list_uploaded_sources", return_value=[]),
            patch.object(ingest, "DATA_DIR", root / "data"),
            patch.object(ingest, "VECTORSTORE_DIR", root / "vectors"),
            patch.object(ingest, "get_vectorstore", return_value=self.store),
            patch.object(ingest, "load_indexed_files", side_effect=lambda owner_id: dict(self.indexed_registry)),
            patch.object(ingest, "has_source_vectors", return_value=True),
            patch.object(ingest, "upsert_indexed_document"),
            patch.object(ingest, "ollama_heading_refiner", side_effect=fake_refiner_factory),
            patch.object(document_storage, "durable_storage_enabled", return_value=True),
            patch.object(document_storage, "upload", side_effect=storage_upload),
            patch.object(document_storage, "bucket_name", return_value="bucket"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(documents_api.router)
        app.dependency_overrides[require_current_user] = lambda: {"id": OWNER_ID}
        self.client = TestClient(app)

    def upload(self, enabled: bool):
        output = io.StringIO()
        with timing_env(enabled), redirect_stdout(output):
            response = self.client.post("/api/sources/upload",
                                        files=[("files", (FILE_NAME, TEXT.encode("utf-8"), "text/plain"))])
        return response, output.getvalue()

    def test_disabled_by_default_prints_nothing_and_costs_a_shared_no_op(self):
        with timing_env(False):
            self.assertFalse(perf_timing.enabled())
            self.assertIsNone(perf_timing.stage_timer("[ingest]"))
            self.assertIs(perf_timing.stage("embed"), perf_timing.stage("insert"))   # one shared nullcontext
            perf_timing.count(chunks=3)
            perf_timing.outcome("indexed")
        response, output = self.upload(enabled=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(LINE.findall(output), [])
        self.assertNotIn("[llm]", output)

    def test_enabled_upload_logs_every_stage_with_counts(self):
        response, output = self.upload(enabled=True)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(sorted(body), ["new_chunks", "new_files", "planner", "skipped_files", "sources", "total_indexed_files"])
        lines = [match.group(0) for match in LINE.finditer(output)]
        self.assertEqual([line.split()[0] for line in lines], ["[ingest]", "[upload]"])   # per file, then per request
        ingest_line, upload_line = fields(lines[0]), fields(lines[1])

        self.assertEqual(ingest_line["outcome"], "indexed")
        self.assertEqual((ingest_line["file"], ingest_line["files"], ingest_line["pages"]), ("1", "1", "1"))
        self.assertEqual(int(ingest_line["chunks"]), body["new_chunks"])
        self.assertEqual(int(ingest_line["embedded_chunks"]), body["new_chunks"])
        self.assertEqual(self.store.connection.inserted, body["new_chunks"])
        for stage in ("hash", "registry_lookup", "storage", "parse", "split", "topics", "refine", "vectors",
                      "embed", "insert", "stale_cleanup", "metadata"):
            self.assertIn(f"{stage}_ms", ingest_line, stage)
        self.assertEqual((ingest_line["refine_calls"], ingest_line["refine_candidates"]), ("1", "2"))
        self.assertNotIn("refine_ok", ingest_line)   # the stand-in reply is rejected, as a capped one would be

        self.assertEqual(upload_line["outcome"], "ok")
        self.assertEqual((upload_line["files"], upload_line["bytes"]), ("1", str(len(TEXT.encode("utf-8")))))
        self.assertEqual((int(upload_line["new_files"]), int(upload_line["new_chunks"]), upload_line["skipped_files"]),
                         (1, body["new_chunks"], "0"))
        for stage in ("read_write", "index", "planner", "sources"):
            self.assertIn(f"{stage}_ms", upload_line, stage)

    def test_stage_times_are_measured_and_nest_consistently(self):
        _response, output = self.upload(enabled=True)
        ingest_line, upload_line = (dict((k, float(v)) for k, v in fields(m.group(0)).items() if k.endswith("_ms"))
                                    for m in LINE.finditer(output))
        self.assertGreaterEqual(ingest_line["embed_ms"], EMBED_SECONDS * 1000 * 0.9)
        self.assertGreaterEqual(ingest_line["storage_ms"], STORAGE_SECONDS * 1000 * 0.9)
        self.assertGreaterEqual(ingest_line["refine_ms"], REFINE_SECONDS * 1000 * 0.9)
        self.assertGreaterEqual(ingest_line["topics_ms"], ingest_line["refine_ms"])                 # topics includes refine
        self.assertGreaterEqual(ingest_line["vectors_ms"], ingest_line["embed_ms"] + ingest_line["insert_ms"])
        stages = sum(value for key, value in ingest_line.items() if key not in {"total_ms", "refine_ms", "embed_ms", "insert_ms"})
        self.assertGreaterEqual(ingest_line["total_ms"], stages)
        self.assertGreaterEqual(upload_line["index_ms"], ingest_line["total_ms"])
        self.assertGreaterEqual(upload_line["total_ms"], upload_line["index_ms"] + upload_line["planner_ms"])

    def test_timing_lines_never_carry_names_ids_or_content(self):
        _response, output = self.upload(enabled=True)
        timing = "\n".join(line for line in output.splitlines() if line.startswith(("[ingest]", "[upload]", "[llm]")))
        self.assertTrue(timing)
        for secret in (FILE_NAME, "secret-lecture-name", OWNER_ID, CONTENT_MARKER, "Memory Paging", "bucket"):
            self.assertNotIn(secret, timing)
        for line in timing.splitlines():
            for key, value in fields(line).items():
                self.assertRegex(key, r"^[a-z_]+$")
                self.assertRegex(value, r"^(\d+(\.\d+)?|[a-z_]+)$", line)   # numbers or a fixed outcome word

    def test_failed_storage_upload_is_logged_as_error_and_contract_unchanged(self):
        self.upload_error = document_storage.DocumentStorageError("storage down")
        response, output = self.upload(enabled=True)
        self.assertEqual(response.status_code, 503)
        lines = [fields(match.group(0)) for match in LINE.finditer(output)]
        self.assertEqual([line["outcome"] for line in lines], ["error", "error"])
        self.assertIn("storage_ms", lines[0])
        self.assertNotIn("embed_ms", lines[0])

    def test_duplicate_upload_is_logged_as_skipped_without_embedding(self):
        file_hash = ingest.hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
        self.indexed_registry = {FILE_NAME: {"hash": file_hash, "chunks": 3, "object_path": "stored",
                                             "topic_schema_version": topic_extractor.TOPIC_SCHEMA_VERSION}}
        response, output = self.upload(enabled=True)
        self.assertEqual(response.status_code, 200, response.text)
        ingest_line, upload_line = (fields(match.group(0)) for match in LINE.finditer(output))
        self.assertEqual(ingest_line["outcome"], "skipped")
        self.assertNotIn("embed_ms", ingest_line)
        self.assertNotIn("storage_ms", ingest_line)
        self.assertEqual((upload_line["new_files"], upload_line["skipped_files"]), ("0", "1"))


class StageTimerTests(unittest.TestCase):
    def test_repeated_stages_add_up_and_counts_sum(self):
        timer = perf_timing.StageTimer("[ingest]")
        with perf_timing.recording(timer):
            for _ in range(2):
                with perf_timing.stage("embed"):
                    time.sleep(0.01)
            perf_timing.count(chunks=2)
            perf_timing.count(chunks=3)
            perf_timing.outcome("indexed")
        line = fields(timer.line())
        self.assertGreaterEqual(float(line["embed_ms"]), 18)
        self.assertEqual((line["chunks"], line["outcome"]), ("5", "indexed"))
        with perf_timing.stage("embed"):   # outside recording(): no target, nothing recorded
            pass
        self.assertEqual(timer.fields["chunks"], 5)

    def test_recording_none_is_a_no_op(self):
        with perf_timing.recording(None) as timer:
            self.assertIsNone(timer)
            self.assertIsInstance(perf_timing.stage("x"), type(perf_timing._NO_STAGE))

    def test_refiner_reports_a_separate_llm_line_with_reload_and_done_reason(self):
        class Reply:
            content = '{"classifications": []}'
            response_metadata = {"load_duration": 3_200_000_000, "eval_count": 500, "eval_duration": 20_000_000_000,
                                 "done_reason": "length"}

        class FakeChat:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def invoke(self, prompt):
                return Reply()

        output = io.StringIO()
        with timing_env(True), redirect_stdout(output), patch("backend.llm_backend.ChatOllama", FakeChat):
            refine = topic_extractor.ollama_heading_refiner("qwen")
            self.assertEqual(refine({"candidates": [{"id": "h0", "heading": "Secret Heading Text"}]}), [])
        line = output.getvalue().strip()
        self.assertTrue(line.startswith("[llm] call=topic_refine "), line)
        self.assertIn("load_ms=3200.0", line)
        self.assertIn("done_reason=length", line)
        self.assertNotIn("Secret Heading", line)


if __name__ == "__main__":
    unittest.main()
