"""CHAT-PERF-4: Chatbot database round trips.

Runs conversation_store and rag_service on the fake PostgreSQL driver (tests/fake_postgres.py) and
counts units of work and SQL calls with db.RequestStats -- the numbers AI_TUTOR_PERF_TIMING=1 logs
on Kaggle (an executemany is one call, as psycopg sends it in one pipeline).

- The conversation view loads every message's citations in one query, with exactly the output of
  the previous one-query-per-message loader, whatever the conversation length.
- Answering loads only the recent history (no citations) and saves question + answer + citations
  (+ a new chat's title) in one transaction; the cost no longer grows with the conversation.
- Ownership, ordering, empty conversations, messages without citations, SSE and JSON payloads.
"""

import io
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessageChunk

from backend import auth_store, conversation_store, db, rag_service
from fake_postgres import FakePsycopg
from test_document_listing_queries import _fake_table_columns

SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
DONE_KEYS = ["answer", "assistant_message", "citations", "grounding_status", "model", "type", "user_message"]
JSON_KEYS = [key for key in DONE_KEYS if key != "type"]


def legacy_get_conversation(owner_id, conversation_id):
    """The previous loader (one citation query per message), kept as the reference output."""
    with db.connect(conversation_store.DATABASE_PATH) as connection:
        row = connection.execute("SELECT * FROM conversations WHERE id = ? AND owner_id = ?",
                                 (conversation_id, owner_id)).fetchone()
        result = conversation_store._conversation_result(connection, row)
        result["messages"] = []
        for message in connection.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
                                          (conversation_id,)).fetchall():
            citations = connection.execute(
                "SELECT document_id, page, chunk, content FROM message_citations WHERE message_id = ? ORDER BY id",
                (message["id"],)).fetchall()
            result["messages"].append({**dict(message), "citations": [dict(citation) for citation in citations]})
        return result


class FakeChat:
    def __init__(self, **options):
        self.options = options

    def stream(self, prompt):
        yield AIMessageChunk(content="Paging maps pages ")
        yield AIMessageChunk(content="to frames.", response_metadata={"done_reason": "stop"})

    def invoke(self, prompt):
        return AIMessageChunk(content="Paging maps pages to frames.", response_metadata={"done_reason": "stop"})


class ChatQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        backing = root / "postgres.db"
        # The fake runs SQL on SQLite, which cannot parse PostgreSQL's identity columns: create the
        # same tables there with the SQLite DDL first, then mark the schemas ready for the fake URL.
        without_url = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        with patch.dict(os.environ, without_url, clear=True), patch.object(auth_store, "DATABASE_PATH", backing), \
                patch.object(conversation_store, "DATABASE_PATH", backing), patch("sys.stdout", io.StringIO()):
            conversation_store.initialize_conversation_store()
        self.fake = FakePsycopg(backing, reuse_connections=True)
        self.stack = ExitStack()
        for module in (auth_store, conversation_store):
            self.stack.enter_context(patch.object(module, "DATABASE_PATH", root / "local.db"))
        self.stack.enter_context(patch.object(db, "_announced", set()))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.stack.enter_context(self.fake.installed(SECRET_URL))
        self.stack.enter_context(patch.object(db, "table_columns", _fake_table_columns))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.addCleanup(db.close_pools)
        db._schema_ready.update({("auth", db.database_url()), ("conversations", db.database_url())})
        for user in ("alice", "bob"):
            with db.connect("unused.db") as connection:
                connection.execute("INSERT INTO users (id, display_name, email, password_hash, created_at) "
                                   "VALUES (?, ?, ?, 'x', '2026-10-16T00:00:00+00:00')", (user, user, f"{user}@example.com"))
        self.doc = Document(page_content="Paging maps virtual pages to physical frames.",
                            metadata={"source": "lecture.pdf", "page": 3, "owner_id": "alice", "document_id": "lecture.pdf"})

    # -- helpers --------------------------------------------------------------------------------

    def conversation(self, owner="alice", document="lecture.pdf"):
        return conversation_store.create_conversation(owner, document_ids=[document], new_conversation=True)["id"]

    def seed(self, conversation_id, exchanges, owner="alice"):
        """`exchanges` question/answer pairs; every third answer has no citations, the others 4."""
        for index in range(exchanges):
            conversation_store.add_message(owner, conversation_id, "user", f"Question {index}")
            citations = [] if index % 3 == 2 else [
                {"document_id": "lecture.pdf", "page": str(index), "chunk": "", "content": f"c{index}-{position}"}
                for position in range(4)]
            conversation_store.add_message(owner, conversation_id, "assistant", f"Answer {index}", "supported", citations)

    def cost(self, call):
        stats, token = db.start_request_stats()
        try:
            result = call()
        finally:
            db.stop_request_stats(token)
        return result, (stats.units, stats.statements)

    def chat(self, conversation_id, question, streaming):
        with patch.object(rag_service, "ChatOllama", FakeChat), \
                patch.object(rag_service, "resolve_generation_model", return_value="qwen"), \
                patch.object(rag_service, "_retrieve_conversation_docs", return_value=[self.doc]):
            if streaming:
                return list(rag_service.stream_conversation_message("alice", conversation_id, question, "qwen-2.5-7b"))
            return rag_service.answer_conversation_message("alice", conversation_id, question, "qwen-2.5-7b")

    # -- conversation view ----------------------------------------------------------------------

    def test_long_conversation_loads_exactly_like_one_query_per_message(self):
        conversation_id = self.conversation()
        self.seed(conversation_id, 30)
        loaded, (units, statements) = self.cost(lambda: conversation_store.get_conversation("alice", conversation_id))
        reference, (_units, legacy_statements) = self.cost(lambda: legacy_get_conversation("alice", conversation_id))
        self.assertEqual(loaded, reference)                       # same messages, order, citations, keys
        self.assertEqual(len(loaded["messages"]), 60)
        self.assertEqual(sum(len(message["citations"]) for message in loaded["messages"]), 20 * 4)
        self.assertEqual((units, statements), (1, 3))              # conversation, messages, all citations
        self.assertEqual(legacy_statements, 2 + 60)                # before: one more query per message

    def test_citations_stay_with_their_message_in_insertion_order(self):
        conversation_id = self.conversation()
        self.seed(conversation_id, 5)
        messages = conversation_store.get_conversation("alice", conversation_id)["messages"]
        self.assertEqual([message["content"] for message in messages],
                         [text for index in range(5) for text in (f"Question {index}", f"Answer {index}")])
        for index, answer in enumerate(messages[1::2]):
            expected = [] if index % 3 == 2 else [f"c{index}-{position}" for position in range(4)]
            self.assertEqual([citation["content"] for citation in answer["citations"]], expected)
            self.assertEqual(list(answer["citations"][0]) if expected else [], ["document_id", "page", "chunk", "content"][:4 if expected else 0])
        self.assertTrue(all(message["citations"] == [] for message in messages[0::2]))

    def test_cost_does_not_grow_with_the_conversation(self):
        short_id, long_id = self.conversation(), self.conversation()
        self.seed(short_id, 1)
        self.seed(long_id, 40)
        self.assertEqual(self.cost(lambda: conversation_store.get_conversation("alice", short_id))[1],
                         self.cost(lambda: conversation_store.get_conversation("alice", long_id))[1])

    def test_empty_conversation_and_header_only_reads(self):
        conversation_id = self.conversation()
        loaded, cost = self.cost(lambda: conversation_store.get_conversation("alice", conversation_id))
        self.assertEqual((loaded["messages"], cost), ([], (1, 2)))   # no citation query without messages
        header, cost = self.cost(lambda: conversation_store.get_conversation("alice", conversation_id, include_messages=False))
        self.assertNotIn("messages", header)
        self.assertEqual(cost, (1, 1))

    def test_owner_isolation(self):
        alice_id, bob_id = self.conversation("alice"), self.conversation("bob")
        self.seed(alice_id, 3, owner="alice")
        self.seed(bob_id, 3, owner="bob")
        with self.assertRaises(ValueError):
            conversation_store.get_conversation("bob", alice_id)
        with self.assertRaises(ValueError):
            conversation_store.add_exchange("bob", alice_id, "q", "a", "supported", [{"document_id": "x", "content": "y"}])
        alice = conversation_store.get_conversation("alice", alice_id)
        self.assertEqual(len(alice["messages"]), 6)                 # bob's failed write added nothing
        self.assertTrue(all(message["conversation_id"] == alice_id for message in alice["messages"]))
        bob_contents = {citation["content"] for message in conversation_store.get_conversation("bob", bob_id)["messages"]
                        for citation in message["citations"]}
        alice_contents = {citation["content"] for message in alice["messages"] for citation in message["citations"]}
        self.assertEqual(len(alice_contents), 8)
        self.assertEqual(len(bob_contents), 8)
        self.assertEqual(len(conversation_store.get_conversation("bob", bob_id)["messages"]), 6)

    # -- answering ------------------------------------------------------------------------------

    def test_recent_history_is_the_tail_without_citations(self):
        conversation_id = self.conversation()
        self.seed(conversation_id, 10)
        full = conversation_store.get_conversation("alice", conversation_id)["messages"]
        recent, cost = self.cost(lambda: conversation_store.get_conversation(
            "alice", conversation_id, recent_messages=6, include_citations=False))
        self.assertEqual(recent["messages"], [{key: value for key, value in message.items() if key != "citations"}
                                              for message in full[-6:]])
        self.assertEqual(cost, (1, 2))

    def test_recent_history_keeps_the_previous_question_when_the_tail_has_none(self):
        conversation_id = self.conversation()
        self.seed(conversation_id, 1)
        for index in range(6):   # not produced by the API, but the follow-up lookup must still match
            conversation_store.add_message("alice", conversation_id, "assistant", f"Extra {index}")
        recent = conversation_store.get_conversation("alice", conversation_id, recent_messages=6,
                                                     include_citations=False)["messages"]
        self.assertEqual([message["content"] for message in recent], ["Question 0"] + [f"Extra {i}" for i in range(6)])
        self.assertEqual(rag_service._retrieval_query(recent, "why?"), "Question 0\nwhy?")

    def test_streamed_answer_costs_two_units_whatever_the_history(self):
        costs = {}
        for exchanges in (0, 30):
            conversation_id = self.conversation()
            self.seed(conversation_id, exchanges)
            events, costs[exchanges] = self.cost(lambda: self.chat(conversation_id, "What is paging?", streaming=True))
            done = events[-1]
            self.assertEqual([event["type"] for event in events], ["start", "delta", "delta", "done"])
            self.assertEqual(sorted(done), DONE_KEYS)
            self.assertEqual(sorted(done["user_message"]), sorted(done["assistant_message"]))
            saved = conversation_store.get_conversation("alice", conversation_id)
            self.assertEqual([message["content"] for message in saved["messages"][-2:]],
                             ["What is paging?", "Paging maps pages to frames."])
            self.assertEqual([message["id"] for message in saved["messages"][-2:]],
                             [done["user_message"]["id"], done["assistant_message"]["id"]])
            self.assertLess(saved["messages"][-2]["created_at"], saved["messages"][-1]["created_at"])
            self.assertEqual(saved["messages"][-1]["citations"],
                             [{"document_id": "lecture.pdf", "page": "3", "chunk": "", "content": self.doc.page_content}])
            self.assertEqual(saved["title"], "What is paging?" if exchanges == 0 else "New conversation")
            self.assertEqual(saved["updated_at"], saved["messages"][-1]["created_at"])
        self.assertEqual(costs[0], costs[30])
        self.assertEqual(costs[30], (2, 7))   # load (conversation + recent messages), one save transaction

    def test_json_answer_keeps_its_contract_and_saves_the_question_first(self):
        conversation_id = self.conversation()
        self.seed(conversation_id, 30)
        result, (units, statements) = self.cost(lambda: self.chat(conversation_id, "And the TLB?", streaming=False))
        self.assertEqual(sorted(result), JSON_KEYS)
        self.assertEqual(result["citations"], [{"sourceId": 1, "title": "lecture.pdf", "page": "3", "content": self.doc.page_content}])
        saved = conversation_store.get_conversation("alice", conversation_id)["messages"]
        self.assertEqual([message["content"] for message in saved[-2:]], ["And the TLB?", "Paging maps pages to frames."])
        self.assertEqual((units, statements), (3, 2 + 3 + 4))   # load, question, answer (+ batched citations)

    def test_json_first_message_sets_the_title_without_reading_it_back(self):
        conversation_id = self.conversation()
        _result, (units, statements) = self.cost(lambda: self.chat(conversation_id, "What is paging?", streaming=False))
        self.assertEqual(conversation_store.get_conversation("alice", conversation_id)["title"], "What is paging?")
        self.assertEqual((units, statements), (4, 2 + 3 + 1 + 4))


if __name__ == "__main__":
    unittest.main()
