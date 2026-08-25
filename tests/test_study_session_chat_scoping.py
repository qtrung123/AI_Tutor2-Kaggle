"""Regression tests for one-document Study Session conversations."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document

from backend import auth_store, conversation_store, rag_service


class _Answer:
    content = "Grounded answer"


class _ChatModel:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, _prompt):
        return _Answer()


class StudySessionChatScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "study-session.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
        ]
        for item in self.patches:
            item.start()
        self.user = auth_store.create_user("Owner", "owner@example.com", "long-password-123")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def _answer(self, conversation_id: str, expected_document: str):
        document = Document(
            page_content=f"Only {expected_document}",
            metadata={
                "owner_id": self.user["id"],
                "document_id": expected_document,
                "source": expected_document,
                "page": 1,
                "chunk": 0,
            },
        )
        with (
            patch.object(rag_service, "resolve_generation_model", return_value="qwen3:3b"),
            patch.object(rag_service, "_retrieve_conversation_docs", return_value=[document]) as retrieve,
            patch.object(rag_service, "load_prompt_template", return_value="{context}\n{conversation_history}\n{question}"),
            patch.object(rag_service, "ChatOllama", _ChatModel),
        ):
            result = rag_service.answer_conversation_message(
                self.user["id"], conversation_id, "Explain this", "qwen-3b"
            )
        self.assertEqual(retrieve.call_args.args[2], [expected_document])
        self.assertEqual({item["title"] for item in result["citations"]}, {expected_document})

    def test_document_a_chat_only_retrieves_document_a(self):
        conversation = conversation_store.create_conversation(
            self.user["id"], "Document A", ["document-a.pdf"]
        )
        self._answer(conversation["id"], "document-a.pdf")

    def test_document_b_chat_only_retrieves_document_b(self):
        conversation = conversation_store.create_conversation(
            self.user["id"], "Document B", ["document-b.pdf"]
        )
        self._answer(conversation["id"], "document-b.pdf")

    def test_switching_a_to_b_to_a_restores_a_history(self):
        first_a = conversation_store.create_conversation(self.user["id"], "A", ["a.pdf"])
        conversation_store.add_message(self.user["id"], first_a["id"], "user", "Remember A")
        conversation_b = conversation_store.create_conversation(self.user["id"], "B", ["b.pdf"])
        conversation_store.add_message(self.user["id"], conversation_b["id"], "user", "Remember B")

        restored_a = conversation_store.create_conversation(self.user["id"], "A again", ["a.pdf"])
        self.assertEqual(restored_a["id"], first_a["id"])
        self.assertEqual([message["content"] for message in restored_a["messages"]], ["Remember A"])
        self.assertEqual(
            conversation_store.get_conversation_for_document(self.user["id"], "b.pdf")["id"],
            conversation_b["id"],
        )

    def test_conversation_document_cannot_be_reassigned(self):
        conversation = conversation_store.create_conversation(self.user["id"], "A", ["a.pdf"])
        with self.assertRaisesRegex(ValueError, "cannot be reassigned"):
            conversation_store.set_conversation_sources(
                self.user["id"], conversation["id"], ["b.pdf"]
            )

    def test_other_user_cannot_access_document_conversation(self):
        other = auth_store.create_user("Other", "other@example.com", "long-password-456")
        conversation = conversation_store.create_conversation(self.user["id"], "A", ["a.pdf"])
        self.assertIsNone(conversation_store.get_conversation_for_document(other["id"], "a.pdf"))
        with self.assertRaisesRegex(ValueError, "not found"):
            conversation_store.get_conversation(other["id"], conversation["id"])


if __name__ == "__main__":
    unittest.main()
