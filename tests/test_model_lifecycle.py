"""MODEL-RESIDENCY (lifecycle): every live Ollama call keeps Qwen 2.5 7B loaded with a consistent
window. Extends tests/test_model_residency.py (Chat, the quiz explanation, the Kaggle warmup) to the
other live callers.

Ollama reloads a model whenever a request asks for a different num_ctx than the loaded one, and
unloads it after keep_alive idle time (keep_alive=0: right after the request). These tests pin what
each live feature sends -- Chat, Quiz, Summary, Flashcards and upload-time topic refinement -- by
capturing the real ChatOllama keyword arguments (no model is run):

- one shared residency (GENERATION_KEEP_ALIVE) everywhere, never keep_alive=0 on a live path;
- num_ctx only from the shared sizes: CHAT_NUM_CTX (= the Quiz window = the first Flashcards tier)
  or the large 32768 window that Summary and big Flashcards prompts deliberately use;
- one Qwen reference for every feature (the registry's qwen-2.5-7b is CHAT_MODEL), so the same
  weights are never loaded twice;
- the Chat client is built once per model and reused.
"""

import inspect
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from backend import (
    auth_store, flashcard_service, flashcard_store, indexed_document_store, llm_backend, model_registry,
    quiz_service, rag_service, summary_service, summary_store, topic_extractor,
)
from backend.quiz_units import QUIZ_NUM_CTX


class CapturingLlm:
    """Records the constructor kwargs of every ChatOllama built, and replies from a script."""
    built: list[dict] = []
    replies: list[str] = []

    def __init__(self, **kwargs):
        CapturingLlm.built.append(kwargs)

    def invoke(self, _prompt):
        class Response:
            response_metadata = {}
            usage_metadata = {}
        response = Response()
        response.content = CapturingLlm.replies.pop(0) if CapturingLlm.replies else '{"classifications":[]}'
        return response


class SharedSettingsTests(unittest.TestCase):
    def test_flashcards_large_tier_is_the_summary_window(self):
        # (The first tier = Chat = Quiz is pinned in test_model_residency.py.)
        self.assertEqual(flashcard_service._NUM_CTX_TIERS, (QUIZ_NUM_CTX, summary_service.SUMMARY_NUM_CTX))

    def test_every_feature_resolves_to_the_same_qwen_reference(self):
        self.assertEqual(model_registry._registry()["qwen-2.5-7b"].ollama_model, config.CHAT_MODEL)

    def test_no_live_path_unloads_the_model_after_a_call(self):
        live_modules = (rag_service, quiz_service, summary_service, flashcard_service, topic_extractor)
        for module in live_modules:
            with self.subTest(module=module.__name__):
                self.assertIsNone(re.search(r"keep_alive\s*=\s*0\b", inspect.getsource(module)))


class LiveCallSettingsTests(unittest.TestCase):
    def setUp(self):
        CapturingLlm.built, CapturingLlm.replies = [], []

    def assert_resident(self, kwargs, num_ctx):
        self.assertEqual(kwargs.get("keep_alive"), config.GENERATION_KEEP_ALIVE)
        self.assertEqual(kwargs.get("num_ctx"), num_ctx)

    def test_chat_builds_one_client_per_model_and_reuses_it(self):
        with patch.object(rag_service, "ChatOllama", CapturingLlm):
            first = rag_service._chat_model("qwen-runtime")
            second = rag_service._chat_model("qwen-runtime")
        self.assertIs(first, second)
        self.assertEqual(len(CapturingLlm.built), 1)
        self.assert_resident(CapturingLlm.built[0], config.CHAT_NUM_CTX)

    def test_upload_topic_refinement_no_longer_reloads_qwen_with_a_default_window(self):
        with patch.object(llm_backend, "ChatOllama", CapturingLlm):
            topic_extractor.ollama_heading_refiner(config.CHAT_MODEL)({"candidates": []})
        self.assertEqual(CapturingLlm.built[0]["model"], config.CHAT_MODEL)
        self.assert_resident(CapturingLlm.built[0], config.CHAT_NUM_CTX)
        # Outputs are unchanged: the same deterministic options as before.
        self.assertEqual({key: CapturingLlm.built[0][key] for key in ("temperature", "num_predict", "format")},
                         {"temperature": 0, "num_predict": 500, "format": "json"})


class GenerationFeatureSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "residency.db"
        for patcher in (
            patch.object(auth_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
            patch.object(summary_store, "DATABASE_PATH", db),
            patch.object(flashcard_store, "DATABASE_PATH", db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owner = auth_store.create_user("Res", "residency@example.com", "long-password-r")["id"]
        indexed_document_store.upsert_indexed_document(self.owner, "notes.pdf", {
            "hash": "h", "chunks": 1, "path": "notes.pdf", "topic_schema_version": 2,
            "topics": [{"topic_id": "alpha", "name": "Alpha", "subtopics": []},
                       {"topic_id": "beta", "name": "Beta", "subtopics": []}],
        })
        CapturingLlm.built, CapturingLlm.replies = [], []

    @staticmethod
    def chunks(_document_id, topic_id, owner_id):
        return [{"content": f"{topic_id} evidence explains the {topic_id} concept clearly.", "metadata": {
            "owner_id": owner_id, "document_id": "notes.pdf", "topic_id": topic_id, "chunk_id": f"{topic_id}1", "chunk": 0}}]

    def test_summary_keeps_qwen_resident_with_its_large_window(self):
        CapturingLlm.replies = [
            '{"topics":[{"topic_id":"alpha","overview":"Alpha evidence explains the alpha concept.","subsections":[]},'
            '{"topic_id":"beta","overview":"Beta evidence explains the beta concept.","subsections":[]}]}',
            '{"overview":"Alpha and beta concepts.","key_takeaways":["alpha concept","beta concept","alpha evidence","beta evidence"]}',
        ]
        with patch.object(summary_service, "ChatOllama", CapturingLlm), \
             patch.object(summary_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(summary_service, "resolve_generation_model", return_value=config.CHAT_MODEL):
            summary_service.generate_document_summary(self.owner, "notes.pdf", model_id="qwen-2.5-7b", regenerate=True)
        self.assertEqual(len(CapturingLlm.built), 1)   # one client for every topic batch and the synthesis
        self.assertEqual(CapturingLlm.built[0]["keep_alive"], config.GENERATION_KEEP_ALIVE)
        self.assertEqual(CapturingLlm.built[0]["num_ctx"], summary_service.SUMMARY_NUM_CTX)

    def test_flashcards_keep_qwen_resident_on_a_shared_window_tier(self):
        CapturingLlm.replies = ['{"topics":[{"topic_id":"alpha","cards":[{"front":"What is alpha?","back":"Alpha is the alpha concept.","source_chunk_ids":["alpha1"]}]},'
                                '{"topic_id":"beta","cards":[{"front":"What is beta?","back":"Beta is the beta concept.","source_chunk_ids":["beta1"]}]}]}']
        with patch.object(flashcard_service, "ChatOllama", CapturingLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(flashcard_service, "resolve_generation_model", return_value=config.CHAT_MODEL):
            flashcard_service.generate_flashcards(self.owner, "notes.pdf", ["alpha", "beta"], "qwen-2.5-7b")
        self.assertTrue(CapturingLlm.built)
        for kwargs in CapturingLlm.built:
            self.assertEqual(kwargs["keep_alive"], config.GENERATION_KEEP_ALIVE)
            self.assertIn(kwargs["num_ctx"], flashcard_service._NUM_CTX_TIERS)
        self.assertEqual(CapturingLlm.built[0]["num_ctx"], config.CHAT_NUM_CTX)   # a normal prompt stays on the Chat window


if __name__ == "__main__":
    unittest.main()
