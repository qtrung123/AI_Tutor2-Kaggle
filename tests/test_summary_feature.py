import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, indexed_document_store, summary_service, summary_store


class FakeResponse:
    def __init__(self, content, input_tokens, output_tokens):
        self.content = content
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        self.response_metadata = {}


class FakeLlm:
    responses = []
    prompts = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class SummaryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "summary.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(summary_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-summary@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-summary@example.com", "long-password-b")["id"]
        for owner in (self.alice, self.bob):
            indexed_document_store.upsert_indexed_document(owner, "lecture.pdf", {
                "hash": "hash-v1", "chunks": 2, "path": "lecture.pdf", "topic_schema_version": 3,
                "topics": [{"topic_id": "topic_b", "name": "Second in source"},
                           {"topic_id": "topic_a", "name": "First alphabetically"}],
            })

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(_document_id, topic_id, owner_id):
        return [{"content": f"Evidence {topic_id} for {owner_id}",
                 "metadata": {"owner_id": owner_id, "document_id": "lecture.pdf",
                              "topic_id": topic_id, "chunk_id": f"{topic_id}-1", "chunk": 0}}]

    def generate(self, owner=None, model="model-a", regenerate=False):
        FakeLlm.responses = [
            FakeResponse('{"topics":[{"topic_id":"topic_b","summary":"B summary","key_takeaways":["B"]},'
                         '{"topic_id":"topic_a","summary":"A summary","key_takeaways":[]}]}', 100, 30),
            FakeResponse('{"overview":"Whole document","key_takeaways":["Overall"]}', 40, 12),
        ]
        FakeLlm.prompts = []
        with patch.object(summary_service, "ChatOllama", FakeLlm), \
             patch.object(summary_service, "get_topic_chunks", side_effect=self.chunks) as retrieval, \
             patch.object(summary_service, "resolve_generation_model", return_value=f"runtime-{model}"):
            result = summary_service.generate_document_summary(
                owner or self.alice, "lecture.pdf", model_id=model, regenerate=regenerate
            )
        return result, retrieval.call_args_list, list(FakeLlm.prompts)

    def test_normal_path_uses_two_calls_and_preserves_topic_order_and_scope(self):
        result, retrievals, prompts = self.generate()
        self.assertEqual(len(prompts), 2)
        self.assertEqual([item["topic_id"] for item in result["topic_summaries"]], ["topic_b", "topic_a"])
        self.assertEqual([call.args for call in retrievals], [
            ("lecture.pdf", "topic_b", self.alice), ("lecture.pdf", "topic_a", self.alice)
        ])
        self.assertLess(prompts[0].index("TOPIC_ID: topic_b"), prompts[0].index("TOPIC_ID: topic_a"))
        self.assertEqual(result["metrics"]["llm_calls"], 2)
        self.assertEqual(result["metrics"]["topic_generation_usage"]["input_tokens"], 100)

    def test_cache_reuse_regeneration_and_model_staleness(self):
        first, _retrievals, _prompts = self.generate()
        with patch.object(summary_service, "get_topic_chunks") as retrieval, \
             patch.object(summary_service, "ChatOllama") as llm, \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            cached = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a")
        self.assertTrue(cached["cache_hit"])
        retrieval.assert_not_called(); llm.assert_not_called()
        regenerated, _retrievals, prompts = self.generate(regenerate=True)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(regenerated["version_number"], first["version_number"] + 1)
        changed_model, _retrievals, prompts = self.generate(model="model-b")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(changed_model["model"], "model-b")

    def test_store_is_owner_scoped_and_schema_contains_compatibility_identity(self):
        alice, _retrievals, _prompts = self.generate(self.alice)
        bob, _retrievals, _prompts = self.generate(self.bob)
        self.assertNotEqual(alice["summary_id"], bob["summary_id"])
        identity = {"owner_id": self.alice, "document_id": "lecture.pdf", "document_hash": "hash-v1",
                    "topic_schema_version": 3, "summary_version": summary_service.SUMMARY_VERSION,
                    "model_id": "model-a", "runtime_model": "runtime-model-a"}
        self.assertEqual(summary_store.get_compatible_summary(identity)["owner_id"], self.alice)
        for changed in (
            {"document_hash": "hash-v2"}, {"topic_schema_version": 4},
            {"summary_version": "new-prompt-version"}, {"runtime_model": "new-runtime-model"},
        ):
            self.assertIsNone(summary_store.get_compatible_summary({**identity, **changed}))
        connection = sqlite3.connect(self.db)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(document_summaries)")}
        finally:
            connection.close()
        self.assertTrue({"owner_id", "document_id", "document_hash", "topic_schema_version",
                         "summary_version", "model_id", "topic_summaries_json", "final_summary_json"} <= columns)


if __name__ == "__main__":
    unittest.main()
