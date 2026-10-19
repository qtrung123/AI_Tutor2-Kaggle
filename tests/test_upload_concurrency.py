"""PERF-4: PDF indexing runs in the threadpool, so the event loop keeps serving other requests.

The real upload route on one event loop (httpx.ASGITransport), with index_files() replaced by a
stand-in that blocks in its worker thread until the test releases it. A control case runs the
same work inline on the event loop (the previous behaviour) and shows the test detects blocking.
Also: success/failure contracts, per-request owners, and one indexing run at a time.
"""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from backend import document_storage
from backend.api import documents as documents_api
from backend.api.deps import require_current_user

PDF = b"%PDF-1.4 test"


async def inline(func, *args):
    """The previous behaviour: the blocking work runs on the event loop itself."""
    return func(*args)


class UploadConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.events = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.hold = True
        self.active = 0
        self.max_active = 0
        self.counter_lock = threading.Lock()
        self.index_calls = []
        self.index_error = None
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

        @app.get("/api/ping")
        async def ping():
            return {"ok": True}

        @app.get("/api/sync-ping")
        def sync_ping():
            return {"ok": True}

        @app.get("/api/stream")
        async def stream():   # a Chat-like SSE stream
            async def frames():
                for index in range(3):
                    await asyncio.sleep(0.01)
                    yield f"data: {index}\n\n"
            return StreamingResponse(frames(), media_type="text/event-stream")

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        self.release.set()
        await self.client.aclose()

    @staticmethod
    def current_user(request: Request) -> dict:
        return {"id": request.headers.get("x-test-user", "alice")}

    def fake_index_files(self, paths, owner_id):
        with self.counter_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.index_calls.append((owner_id, [path.name for path in paths], [path.read_bytes() for path in paths],
                                 [path.parent.name for path in paths]))
        self.started.set()
        try:
            if self.hold:
                self.release.wait(timeout=2)   # bounded, so the inline control case cannot hang
            else:
                time.sleep(0.05)
            if self.index_error:
                raise self.index_error
            self.events.append("index_done")
            return {"new_files": len(paths), "new_chunks": 7 * len(paths), "skipped_files": [],
                    "topic_refreshed_files": [], "indexed_documents": [path.name for path in paths],
                    "total_indexed_files": len(paths)}
        finally:
            with self.counter_lock:
                self.active -= 1

    def upload(self, name="lecture.pdf", user="alice", content=PDF):
        return self.client.post("/api/sources/upload", headers={"x-test-user": user},
                                files=[("files", (name, content, "application/pdf"))])

    async def wait_until_indexing(self):
        for _ in range(500):
            if self.started.is_set():
                return
            await asyncio.sleep(0.01)
        self.fail("indexing never started")

    async def requests_during_indexing(self):
        upload = asyncio.create_task(self.upload())
        await self.wait_until_indexing()
        ping = await self.client.get("/api/ping")
        sync_ping = await self.client.get("/api/sync-ping")
        stream = await self.client.get("/api/stream")
        self.events.append("requests_done")
        self.release.set()
        response = await upload
        return response, ping, sync_ping, stream

    # -- responsiveness -------------------------------------------------------------------------

    async def test_other_requests_complete_while_indexing_blocks_a_worker_thread(self):
        response, ping, sync_ping, stream = await self.requests_during_indexing()
        self.assertEqual(self.events, ["requests_done", "index_done"])   # served while indexing was still blocked
        self.assertEqual((ping.status_code, sync_ping.status_code, stream.status_code), (200, 200, 200))
        self.assertEqual(stream.text, "data: 0\n\ndata: 1\n\ndata: 2\n\n")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_control_inline_indexing_would_block_every_request(self):
        with patch.object(documents_api, "run_in_threadpool", inline):
            response, ping, _sync_ping, _stream = await self.requests_during_indexing()
        self.assertEqual(self.events, ["index_done", "requests_done"])   # nothing ran until indexing ended
        self.assertEqual((ping.status_code, response.status_code), (200, 200))

    # -- contract -------------------------------------------------------------------------------

    async def test_success_returns_only_after_indexing_with_the_same_contract(self):
        self.hold = False
        response = await self.upload("week1.pdf", user="alice")
        self.assertEqual(response.status_code, 200, response.text)
        # An upload never adds the document to the Study Planner (no "planner" outcome either).
        self.assertEqual(response.json(), {"new_files": 1, "new_chunks": 7, "skipped_files": [], "total_indexed_files": 1,
                                           "sources": []})
        self.assertEqual(self.events, ["index_done"])
        self.assertEqual(self.index_calls, [("alice", ["week1.pdf"], [PDF], ["alice"])])
        self.assertEqual((self.data_dir / "users" / "alice" / "week1.pdf").read_bytes(), PDF)

    async def test_failures_keep_their_status_codes_and_release_the_lock(self):
        self.hold = False
        for error, status in ((document_storage.DocumentStorageError("storage down"), 503),
                              (RuntimeError("embedding model missing"), 500)):
            with self.subTest(status=status):
                self.index_error = error
                response = await self.upload()
                self.assertEqual(response.status_code, status)
                self.assertIn(str(error), response.json()["detail"])
        self.index_error = None
        response = await self.upload()   # the lock was released by the failures
        self.assertEqual(response.status_code, 200, response.text)

    async def test_unsupported_file_is_rejected_before_anything_is_written_or_indexed(self):
        response = await self.client.post("/api/sources/upload", files=[
            ("files", ("ok.pdf", PDF, "application/pdf")), ("files", ("notes.docx", b"x", "application/octet-stream"))])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.index_calls, [])
        self.assertFalse((self.data_dir / "users").exists())

    # -- concurrency ----------------------------------------------------------------------------

    async def test_concurrent_uploads_index_one_at_a_time_with_their_own_owner(self):
        first = asyncio.create_task(self.upload("same.pdf", user="alice", content=b"%PDF alice"))
        await self.wait_until_indexing()
        second = asyncio.create_task(self.upload("same.pdf", user="bob", content=b"%PDF bob"))
        third = asyncio.create_task(self.upload("same.pdf", user="alice", content=b"%PDF alice v2"))
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.index_calls), 1)   # the others wait for the lock, in worker threads
        ping = await self.client.get("/api/ping")      # ...and the event loop is still free
        self.assertEqual(ping.status_code, 200)
        self.release.set()
        responses = await asyncio.gather(first, second, third)
        self.assertEqual([response.status_code for response in responses], [200, 200, 200])
        self.assertEqual(self.max_active, 1)
        owners = sorted((owner, content[0], folder[0]) for owner, _names, content, folder in self.index_calls)
        self.assertEqual(owners, [("alice", b"%PDF alice", "alice"), ("alice", b"%PDF alice v2", "alice"),
                                  ("bob", b"%PDF bob", "bob")])   # each indexed exactly the bytes it uploaded

    async def test_delete_waits_for_a_running_index(self):
        deleted_at = []

        def fake_delete(document_id, owner_id):
            deleted_at.append(list(self.events))
            raise ValueError("Document was not found in indexed files.")

        with patch.object(documents_api, "delete_indexed_file", side_effect=fake_delete):
            upload = asyncio.create_task(self.upload("lecture.pdf"))
            await self.wait_until_indexing()
            delete = asyncio.create_task(self.client.delete("/api/sources/lecture.pdf"))
            await asyncio.sleep(0.2)
            self.assertEqual(deleted_at, [])   # still waiting for the index of the same document
            self.release.set()
            upload_response, delete_response = await asyncio.gather(upload, delete)
        self.assertEqual((upload_response.status_code, delete_response.status_code), (200, 404))
        self.assertEqual(deleted_at, [["index_done"]])


if __name__ == "__main__":
    unittest.main()
