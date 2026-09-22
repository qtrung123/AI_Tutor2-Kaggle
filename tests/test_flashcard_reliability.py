"""Regression tests for flashcard generation reliability across models (repeat-limit aborts,
malformed output, bounded retry, no silent model fallback, unchanged cache behavior)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ollama

from backend import auth_store, flashcard_service, flashcard_store, indexed_document_store
from config import FLASHCARD_MAX_CARDS_PER_TOPIC


class FakeResponse:
    def __init__(self, content):
        self.content = content


def _cards_response(cards, topic_id="alpha"):
    return json.dumps({"topics": [{"topic_id": topic_id, "cards": cards}]})


EN_QA = {
    "front": "What does the scheduler optimize for?",
    "back": "Latency for a small number of active threads.",
    "source_chunk_ids": ["a1"],
}


class FlakyFakeLlm:
    """Records the kwargs ChatOllama(...) was constructed with on every attempt, and raises or
    returns a canned response per call -- lets a test drive the retry loop deterministically
    without a real Ollama server."""

    calls: list = []
    kwargs_calls: list = []
    responses: list = []

    def __init__(self, **kwargs):
        self.__class__.kwargs_calls.append(kwargs)

    def invoke(self, prompt):
        self.__class__.calls.append(prompt)
        item = self.__class__.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    @classmethod
    def reset(cls, responses):
        cls.calls = []
        cls.kwargs_calls = []
        cls.responses = list(responses)


class FlashcardReliabilityTests(unittest.TestCase):
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
        self.owner = auth_store.create_user("Dave", "dave-reliability@example.com", "long-password-d")["id"]
        topics = [{"topic_id": "alpha", "name": "Scheduling", "subtopics": []}]
        indexed_document_store.upsert_indexed_document(self.owner, "notes.pdf", {
            "hash": "hash-1", "chunks": 1, "path": "notes.pdf", "topic_schema_version": 2, "topics": topics,
        })

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(_document_id, _topic_id, owner_id):
        return [{"content": "Evidence content.", "metadata": {
            "owner_id": owner_id, "document_id": "notes.pdf", "topic_id": "alpha",
            "chunk_id": "a1", "chunk": 0,
        }}]

    def generate(self, responses, model_id="gemma3-12b", runtime_model="gemma3-runtime"):
        FlakyFakeLlm.reset(responses)
        with patch.object(flashcard_service, "ChatOllama", FlakyFakeLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(flashcard_service, "resolve_generation_model", return_value=runtime_model):
            return flashcard_service.generate_flashcards(
                self.owner, "notes.pdf", ["alpha"], model_id,
            )

    def test_repeat_limit_failure_then_retry_succeeds(self):
        repeat_limit_error = ollama.ResponseError("prediction aborted, token repeat limit reached")
        result = self.generate([repeat_limit_error, _cards_response([EN_QA])])
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])
        self.assertFalse(result["cache_hit"])

    def test_retry_exhausts_safely_without_crashing_or_looping_forever(self):
        repeat_limit_error = ollama.ResponseError("prediction aborted, token repeat limit reached")
        with self.assertRaises(flashcard_service.FlashcardGenerationError) as ctx:
            self.generate([repeat_limit_error, repeat_limit_error, repeat_limit_error])
        # Bounded: exactly FLASHCARD_GENERATION_RETRY_LIMIT attempts, never more (no infinite retry).
        self.assertEqual(len(FlakyFakeLlm.calls), flashcard_service.FLASHCARD_GENERATION_RETRY_LIMIT)
        self.assertEqual(ctx.exception.safe_message, flashcard_service.FlashcardGenerationError.SAFE_MESSAGE)
        self.assertIn("token repeat limit reached", ctx.exception.technical_message)
        self.assertEqual(flashcard_store.list_flashcards(self.owner, "notes.pdf"), [])

    def test_malformed_model_output_triggers_retry_and_recovers(self):
        result = self.generate(["not json at all, just garbage text", _cards_response([EN_QA])])
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])

    def test_malformed_output_exhausts_safely(self):
        with self.assertRaises(flashcard_service.FlashcardGenerationError):
            self.generate(["garbage one", "garbage two"])
        self.assertEqual(len(FlakyFakeLlm.calls), flashcard_service.FLASHCARD_GENERATION_RETRY_LIMIT)

    def test_selected_model_is_preserved_across_retries_with_no_silent_fallback(self):
        repeat_limit_error = ollama.ResponseError("prediction aborted, token repeat limit reached")
        self.generate([repeat_limit_error, _cards_response([EN_QA])], model_id="gemma3-12b",
                       runtime_model="gemma3:12b-it-q4_K_M")
        self.assertEqual(len(FlakyFakeLlm.kwargs_calls), 2)
        for kwargs in FlakyFakeLlm.kwargs_calls:
            self.assertEqual(kwargs["model"], "gemma3:12b-it-q4_K_M")
            self.assertNotIn("qwen", kwargs["model"].lower())

    def test_retry_sampling_options_change_between_attempts(self):
        repeat_limit_error = ollama.ResponseError("prediction aborted, token repeat limit reached")
        self.generate([repeat_limit_error, _cards_response([EN_QA])])
        first, second = FlakyFakeLlm.kwargs_calls
        self.assertNotEqual(
            (first["temperature"], first["repeat_penalty"]),
            (second["temperature"], second["repeat_penalty"]),
        )

    def test_cache_behavior_unchanged_after_a_retry_recovered_generation(self):
        repeat_limit_error = ollama.ResponseError("prediction aborted, token repeat limit reached")
        first = self.generate([repeat_limit_error, _cards_response([EN_QA])])
        self.assertEqual(first["llm_calls"], 2)
        with patch.object(flashcard_service, "ChatOllama") as llm, \
             patch.object(flashcard_service, "get_topic_chunks") as retrieval, \
             patch.object(flashcard_service, "resolve_generation_model", return_value="gemma3-runtime"):
            cached = flashcard_service.generate_flashcards(self.owner, "notes.pdf", ["alpha"], "gemma3-12b")
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["llm_calls"], 0)
        llm.assert_not_called()
        retrieval.assert_not_called()
        self.assertEqual(cached["cards"][0]["front"], EN_QA["front"])

    def test_output_is_capped_at_max_cards_per_topic(self):
        many_cards = [
            {"front": f"What is fact {index}?", "back": f"Fact {index} is true.", "source_chunk_ids": ["a1"]}
            for index in range(FLASHCARD_MAX_CARDS_PER_TOPIC + 5)
        ]
        result = self.generate([_cards_response(many_cards)])
        self.assertEqual(len(result["cards"]), FLASHCARD_MAX_CARDS_PER_TOPIC)


if __name__ == "__main__":
    unittest.main()
