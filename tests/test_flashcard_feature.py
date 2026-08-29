import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, flashcard_service, flashcard_store, indexed_document_store


class FakeResponse:
    content = '{"topics":[{"topic_id":"wrong","cards":[{"front":"What is beta?","back":"Beta is the second concept.","source_chunk_ids":["b1"]}]},{"topic_id":"also-wrong","cards":[{"front":"What is alpha?","back":"Alpha is the first concept.","source_chunk_ids":["a1"]},{"front":"What is alpha?","back":"Alpha is the first concept.","source_chunk_ids":["a1"]}]}]}'


class FakeLlm:
    calls = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        self.calls.append(prompt)
        return FakeResponse()


class FlashcardFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cards.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(flashcard_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-cards@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-cards@example.com", "long-password-b")["id"]
        topics = [
            {"topic_id": "beta", "name": "Beta", "subtopics": []},
            {"topic_id": "alpha", "name": "Alpha", "subtopics": []},
        ]
        for owner in (self.alice, self.bob):
            indexed_document_store.upsert_indexed_document(owner, "notes.pdf", {
                "hash": "hash-1", "chunks": 2, "path": "notes.pdf", "topic_schema_version": 2, "topics": topics,
            })

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(_document_id, topic_id, owner_id):
        chunk_id = "b1" if topic_id == "beta" else "a1"
        return [{"content": f"{topic_id} evidence for {owner_id}", "metadata": {
            "owner_id": owner_id, "document_id": "notes.pdf", "topic_id": topic_id,
            "chunk_id": chunk_id, "chunk": 0,
        }}]

    def generate(self, owner=None):
        FakeLlm.calls = []
        with patch.object(flashcard_service, "ChatOllama", FakeLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks) as retrieval, \
             patch.object(flashcard_service, "resolve_generation_model", return_value="runtime-model"):
            result = flashcard_service.generate_flashcards(owner or self.alice, "notes.pdf", ["alpha", "beta"], "model-a")
        return result, retrieval.call_args_list

    def test_one_call_authoritative_order_identity_and_exact_deduplication(self):
        result, retrievals = self.generate()
        self.assertEqual(len(FakeLlm.calls), 1)
        self.assertEqual(result["llm_calls"], 1)
        self.assertEqual([card["topic_id"] for card in result["cards"]], ["beta", "alpha"])
        self.assertEqual([card["topic_name"] for card in result["cards"]], ["Beta", "Alpha"])
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual([call.args for call in retrievals], [
            ("notes.pdf", "beta", self.alice), ("notes.pdf", "alpha", self.alice),
        ])
        prompt = FakeLlm.calls[0]
        self.assertLess(prompt.index("TOPIC_ID: beta"), prompt.index("TOPIC_ID: alpha"))
        self.assertIn("Use ONLY evidence from the card's own TOPIC block", prompt)

    def test_compatible_cache_hit_uses_zero_calls_and_is_owner_scoped(self):
        first, _ = self.generate()
        with patch.object(flashcard_service, "ChatOllama") as llm, \
             patch.object(flashcard_service, "get_topic_chunks") as retrieval, \
             patch.object(flashcard_service, "resolve_generation_model", return_value="runtime-model"):
            cached = flashcard_service.generate_flashcards(self.alice, "notes.pdf", model_id="model-a")
        self.assertTrue(cached["cache_hit"]); self.assertEqual(cached["llm_calls"], 0)
        llm.assert_not_called(); retrieval.assert_not_called()
        self.assertTrue(all(card["owner_id"] == self.alice for card in first["cards"]))
        self.assertIsNone(flashcard_store.get_compatible_flashcards({
            "owner_id": self.bob, "document_id": "notes.pdf", "document_hash": "hash-1",
            "topic_schema_version": 2, "flashcard_version": flashcard_service.FLASHCARD_VERSION,
            "model_id": "model-a", "runtime_model": "runtime-model", "topic_ids": ["beta", "alpha"],
        }))

    def test_card_crud_is_owner_scoped_and_uses_authoritative_topic_name(self):
        generated, _ = self.generate()
        fields = flashcard_service.authoritative_card_fields(self.alice, "notes.pdf", "alpha")
        card = flashcard_store.add_flashcard(self.alice, "notes.pdf", generated["set_id"], {
            **fields, "front": "Manual front", "back": "Manual back", "source_chunk_ids": [],
        })
        updated = flashcard_store.update_flashcard(self.alice, "notes.pdf", card["flashcard_id"], {
            "front": "Edited front", "back": "Edited back", "is_favorite": True,
        })
        self.assertEqual((updated["front"], updated["back"]), ("Edited front", "Edited back"))
        self.assertTrue(updated["is_favorite"]); self.assertEqual(updated["topic_name"], "Alpha")
        persisted = flashcard_store.list_flashcards(self.alice, "notes.pdf", generated["set_id"])
        self.assertEqual(next(item for item in persisted if item["flashcard_id"] == card["flashcard_id"]), updated)
        with self.assertRaisesRegex(ValueError, "not found"):
            flashcard_store.update_flashcard(self.bob, "notes.pdf", card["flashcard_id"], {"front": "stolen"})
        flashcard_store.delete_flashcard(self.alice, "notes.pdf", card["flashcard_id"])
        self.assertNotIn(card["flashcard_id"], {
            item["flashcard_id"] for item in flashcard_store.list_flashcards(self.alice, "notes.pdf", generated["set_id"])
        })

    def test_cache_identity_bypasses_stale_document_schema_model_and_version(self):
        generated, _ = self.generate()
        base = {
            "owner_id": self.alice, "document_id": "notes.pdf", "document_hash": "hash-1",
            "topic_schema_version": 2, "flashcard_version": flashcard_service.FLASHCARD_VERSION,
            "model_id": "model-a", "runtime_model": "runtime-model", "topic_ids": ["beta", "alpha"],
        }
        self.assertEqual(flashcard_store.get_compatible_flashcards(base)["set_id"], generated["set_id"])
        variants = [
            {"document_hash": "hash-2"}, {"topic_schema_version": 3},
            {"model_id": "model-b"}, {"runtime_model": "runtime-model-b"},
            {"flashcard_version": "grounded_flashcards_v2"}, {"topic_ids": ["beta"]},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(flashcard_store.get_compatible_flashcards({**base, **change}))

    def test_cards_without_same_topic_chunk_provenance_are_discarded(self):
        class UngroundedResponse:
            content = '{"topics":[{"cards":[{"front":"Bad","back":"Wrong","source_chunk_ids":["a1"]}]},{"cards":[{"front":"Good","back":"Right","source_chunk_ids":["a1"]}]}]}'
        with patch.object(FakeLlm, "invoke", return_value=UngroundedResponse()), \
             patch.object(flashcard_service, "ChatOllama", FakeLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(flashcard_service, "resolve_generation_model", return_value="other-runtime"):
            result = flashcard_service.generate_flashcards(self.alice, "notes.pdf", model_id="model-a")
        self.assertEqual([(card["topic_id"], card["front"]) for card in result["cards"]], [("alpha", "Good")])


if __name__ == "__main__":
    unittest.main()
