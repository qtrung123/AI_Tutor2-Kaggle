"""AI Tutor answer streaming (CHAT-PERF-2): the chat API streams Server-Sent Events when asked
(Accept: text/event-stream) and keeps its JSON answer otherwise; nothing is saved until the model
has finished, and a stopped, failed or abandoned stream saves nothing."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessageChunk

from backend import auth_store, conversation_store, rag_service
from backend.main import app

PASSWORD = "long-password-123"
PIECES = ["Paging maps ", "virtual pages ", "to **frames** (lecture.pdf: 3)."]


class FakeStreamingModel:
    """ChatOllama stand-in: .stream() yields PIECES then a metadata-only chunk, like langchain_ollama.
    Records the options, the prompt, and whether the stream was closed early."""
    instances = []
    fail_after = None   # raise after this many pieces

    def __init__(self, **options):
        self.options, self.prompt, self.closed, self.finished = options, None, False, False
        FakeStreamingModel.instances.append(self)

    def stream(self, prompt):
        self.prompt = prompt
        try:
            for index, piece in enumerate(PIECES):
                if FakeStreamingModel.fail_after is not None and index == FakeStreamingModel.fail_after:
                    raise RuntimeError("model crashed")
                yield AIMessageChunk(content=piece)
            yield AIMessageChunk(content="", response_metadata={"eval_count": 12, "done_reason": "stop"})
            self.finished = True
        except GeneratorExit:
            self.closed = True
            raise

    def invoke(self, prompt):
        self.prompt = prompt
        return AIMessageChunk(content="".join(PIECES))


class StreamingTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "chat.db"
        doc = Document(page_content="Paging maps virtual pages to physical frames.",
                       metadata={"source": "lecture.pdf", "page": 3, "owner_id": None, "document_id": "lecture.pdf"})
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", database),
            patch.object(conversation_store, "DATABASE_PATH", database),
            patch.object(rag_service, "ChatOllama", FakeStreamingModel),
            patch.object(rag_service, "resolve_generation_model", return_value="qwen-runtime"),
            patch.object(rag_service, "_retrieve_conversation_docs", side_effect=lambda owner, *_: [self.doc_for(owner)]),
            patch("backend.api.conversations.prepare_generation_model", return_value={"ready": True}),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.stop)
        self.doc = doc
        FakeStreamingModel.instances, FakeStreamingModel.fail_after = [], None
        self.user = auth_store.create_user("Owner", "owner-stream@example.com", PASSWORD)
        self.conversation = conversation_store.create_conversation(self.user["id"], "New conversation", ["lecture.pdf"])

    def stop(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def doc_for(self, owner):
        return Document(page_content=self.doc.page_content, metadata={**self.doc.metadata, "owner_id": owner})

    def saved(self):
        return [(m["role"], m["content"]) for m in
                conversation_store.get_conversation(self.user["id"], self.conversation["id"])["messages"]]


class StreamGeneratorTests(StreamingTestBase):
    def events(self, **kwargs):
        return rag_service.stream_conversation_message(self.user["id"], self.conversation["id"], "  What is paging?  ",
                                                       "qwen-2.5-7b", **kwargs)

    def test_streams_the_answer_in_order_and_saves_it_only_at_the_end(self):
        events = self.events()
        self.assertEqual(next(events), {"type": "start", "model": "qwen-runtime"})
        deltas = [next(events) for _ in PIECES]
        self.assertEqual([e["text"] for e in deltas], PIECES)
        self.assertEqual(self.saved(), [])                       # nothing saved while it streams
        done = next(events)
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["answer"], "".join(PIECES).strip())
        self.assertEqual((done["grounding_status"], [c["title"] for c in done["citations"]]), ("supported", ["lecture.pdf"]))
        self.assertEqual(done["user_message"]["content"], "What is paging?")
        self.assertEqual(self.saved(), [("user", "What is paging?"), ("assistant", done["answer"])])
        stored = conversation_store.get_conversation(self.user["id"], self.conversation["id"])
        self.assertEqual(stored["title"], "What is paging?")
        self.assertEqual([c["page"] for c in stored["messages"][1]["citations"]], ["3"])
        self.assertEqual(list(events), [])

    def test_same_model_options_prompt_and_payload_as_the_json_answer(self):
        streamed = list(self.events())[-1]
        stream_model = FakeStreamingModel.instances[-1]
        other = conversation_store.create_conversation(self.user["id"], "Other", ["lecture.pdf"], new_conversation=True)
        plain = rag_service.answer_conversation_message(self.user["id"], other["id"], "  What is paging?  ", "qwen-2.5-7b")
        plain_model = FakeStreamingModel.instances[-1]
        self.assertEqual(stream_model.options, plain_model.options)
        self.assertEqual(stream_model.prompt, plain_model.prompt)
        self.assertEqual({k: streamed[k] for k in ("answer", "model", "grounding_status", "citations")},
                         {k: plain[k] for k in ("answer", "model", "grounding_status", "citations")})
        self.assertEqual(set(streamed) - {"type"}, set(plain))

    def test_closing_early_stops_the_model_and_saves_nothing(self):
        events = self.events()
        next(events), next(events)            # start + first piece
        events.close()                        # the client went away
        self.assertTrue(FakeStreamingModel.instances[-1].closed)
        self.assertFalse(FakeStreamingModel.instances[-1].finished)
        self.assertEqual(self.saved(), [])

    def test_cancel_flag_stops_at_the_next_piece_and_saves_nothing(self):
        cancelled = threading.Event()
        events = self.events(cancelled=cancelled)
        next(events), next(events)
        cancelled.set()
        self.assertEqual(list(events), [])
        self.assertTrue(FakeStreamingModel.instances[-1].closed)
        self.assertEqual(self.saved(), [])

    def test_model_error_saves_nothing(self):
        FakeStreamingModel.fail_after = 1
        events = self.events()
        next(events), next(events)
        with self.assertRaisesRegex(RuntimeError, "model crashed"):
            next(events)
        self.assertEqual(self.saved(), [])

    def test_unknown_conversation_fails_before_any_event(self):
        with self.assertRaises(ValueError):
            rag_service.stream_conversation_message(self.user["id"], "missing", "Hi", "qwen-2.5-7b")

    def test_fixed_answer_without_relevant_chunks_is_streamed_and_saved(self):
        with patch.object(rag_service, "_retrieve_conversation_docs", return_value=[]):
            events = list(self.events())
        self.assertEqual([e["type"] for e in events], ["start", "delta", "done"])
        self.assertEqual(events[1]["text"], rag_service.NO_CONTEXT_ANSWER)
        self.assertEqual((events[-1]["grounding_status"], events[-1]["citations"]), ("insufficient_context", []))
        self.assertEqual(FakeStreamingModel.instances, [])        # no model call
        self.assertEqual(len(self.saved()), 2)


def sse_events(text: str) -> list[dict]:
    return [json.loads(frame[len("data: "):]) for frame in text.split("\n\n") if frame.startswith("data: ")]


class StreamingApiTests(StreamingTestBase):
    def client(self):
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": "owner-stream@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def post(self, client, accept="text/event-stream", conversation_id=None, message="What is paging?"):
        return client.post(f"/api/conversations/{conversation_id or self.conversation['id']}/messages",
                           json={"message": message, "model_id": "qwen-2.5-7b"}, headers={"Accept": accept})

    def test_event_stream_response(self):
        response = self.post(self.client())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        events = sse_events(response.text)
        self.assertEqual([e["type"] for e in events], ["start", "delta", "delta", "delta", "done"])
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "".join(PIECES))
        self.assertEqual(events[-1]["assistant_message"]["content"], events[-1]["answer"])
        self.assertEqual(len(self.saved()), 2)                     # exactly one user + one assistant message

    def test_json_answer_is_unchanged_without_the_stream_accept_header(self):
        for accept in ("application/json", "*/*"):
            with self.subTest(accept=accept):
                response = self.post(self.client(), accept=accept)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertEqual(set(response.json()), {"user_message", "assistant_message", "answer", "model",
                                                        "grounding_status", "citations"})

    def test_model_error_becomes_an_error_event_and_saves_nothing(self):
        FakeStreamingModel.fail_after = 2
        response = self.post(self.client())
        self.assertEqual(response.status_code, 200)                # headers were already sent
        events = sse_events(response.text)
        self.assertEqual([e["type"] for e in events], ["start", "delta", "delta", "error"])
        self.assertIn("model crashed", events[-1]["message"])
        self.assertEqual(self.saved(), [])

    def test_authentication_and_unknown_conversation_fail_before_streaming(self):
        anonymous = TestClient(app).post(f"/api/conversations/{self.conversation['id']}/messages",
                                         json={"message": "Hi"}, headers={"Accept": "text/event-stream"})
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.headers["content-type"], "application/json")
        missing = self.post(self.client(), conversation_id="missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["content-type"], "application/json")
        self.assertEqual(self.post(self.client(), message="   ").status_code, 400)
        self.assertEqual(FakeStreamingModel.instances, [])

    def test_client_disconnect_closes_the_model_stream_and_saves_nothing(self):
        from backend.api.conversations import _event_stream

        async def disconnect_after_two_frames():
            cancelled = threading.Event()
            events = rag_service.stream_conversation_message(self.user["id"], self.conversation["id"], "Hi",
                                                             "qwen-2.5-7b", cancelled)
            frames = _event_stream(events, cancelled)
            received = [await frames.__anext__(), await frames.__anext__()]
            await frames.aclose()             # what Starlette does when the client goes away
            return received, cancelled.is_set()

        received, cancelled = asyncio.run(disconnect_after_two_frames())
        self.assertEqual([sse_events(frame)[0]["type"] for frame in received], ["start", "delta"])
        self.assertTrue(cancelled)
        self.assertTrue(FakeStreamingModel.instances[-1].closed)
        self.assertEqual(self.saved(), [])

    def test_another_users_conversation_is_not_streamed(self):
        auth_store.create_user("Other", "other-stream@example.com", PASSWORD)
        other = TestClient(app)
        other.post("/api/auth/login", json={"email": "other-stream@example.com", "password": PASSWORD})
        response = self.post(other)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.saved(), [])


if __name__ == "__main__":
    unittest.main()
