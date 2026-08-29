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
            FakeResponse('{"topics":[{"topic_id":"topic_b","overview":"Evidence topic_b is summarized.","subsections":[]},'
                         '{"topic_id":"topic_a","overview":"Evidence topic_a is summarized.","subsections":[]}]}', 100, 30),
            FakeResponse('{"overview":"Whole document overview.","key_takeaways":["First grounded point","Second grounded point","Third grounded point","Fourth grounded point"]}', 40, 12),
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
        self.assertEqual(prompts[0].count("\nEND_TOPIC"), 2)
        self.assertIn("using ONLY the evidence inside that topic's own block", prompts[0])
        self.assertIn("Never transfer facts", prompts[0])
        self.assertIn("REQUIRED_SUBTOPICS_IN_ORDER", prompts[0])
        self.assertIn('"content_type":"paragraph|bullets|table"', prompts[0])
        self.assertIn("Do not return per-topic key takeaways", prompts[0])
        self.assertNotIn(self.alice, prompts[1])
        self.assertNotIn("END_TOPIC", prompts[1])
        self.assertIn("4 to 7 document-level key takeaways", prompts[1])
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

    def test_cross_topic_contamination_is_flagged_without_extra_calls(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [
            (topics[0], [{"content": "eCos provides a configurable real-time kernel with mutex synchronization."}]),
            (topics[1], [{"content": "TinyOS uses event-driven components, nesC modules, and deferred tasks."}]),
        ]
        raw = [
            {"topic_id": "ecos", "overview": "eCos provides a configurable real-time kernel. TinyOS uses event-driven nesC components.", "subsections": []},
            {"topic_id": "tinyos", "overview": "TinyOS uses event-driven nesC modules and deferred tasks.", "subsections": []},
        ]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertIn("statement_2_resembles_other_topic_evidence", validated[0]["grounding_warnings"])
        self.assertEqual(validated[1]["grounding_warnings"], [])

    def test_backend_overwrites_wrong_or_repeated_ids_for_complete_ordered_response(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [
            (topics[0], [{"content": "eCos configurable kernel synchronization mutex."}]),
            (topics[1], [{"content": "TinyOS event-driven nesC components tasks."}]),
        ]
        raw = [
            {"topic_id": "tinyos", "overview": "eCos configurable kernel synchronization.", "subsections": []},
            {"topic_id": "tinyos", "overview": "TinyOS event-driven nesC components.", "subsections": []},
        ]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["topic_id"] for item in validated], ["ecos", "tinyos"])
        self.assertEqual(len({item["topic_id"] for item in validated}), len(topics))
        self.assertTrue(all("model_topic_ids_overwritten_by_backend_order" in item["grounding_warnings"] for item in validated))

    def test_valid_ids_are_mapped_then_returned_in_authoritative_order(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [
            (topics[0], [{"content": "eCos configurable kernel synchronization mutex."}]),
            (topics[1], [{"content": "TinyOS event-driven nesC components tasks."}]),
        ]
        raw = [
            {"topic_id": "tinyos", "overview": "TinyOS event-driven nesC components.", "subsections": []},
            {"topic_id": "ecos", "overview": "eCos configurable kernel synchronization.", "subsections": []},
        ]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["topic_id"] for item in validated], ["ecos", "tinyos"])
        self.assertTrue(validated[0]["overview"].startswith("eCos"))

    def test_partial_or_unsupported_topic_responses_are_rejected_without_guessing(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [(topic, [{"content": f"{topic['name']} distinct supported architecture concepts."}]) for topic in topics]
        with self.assertRaisesRegex(ValueError, "1 topic summaries for 2 requested"):
            summary_service._validated_topic_summaries(
                [{"topic_id": "ecos", "overview": "eCos supported architecture.", "subsections": []}], topics, evidence
            )
        with self.assertRaisesRegex(ValueError, "lacks meaningful lexical support"):
            summary_service._validated_topic_summaries([
                {"topic_id": "ecos", "overview": "Unrelated pineapple astronomy vocabulary.", "subsections": []},
                {"topic_id": "tinyos", "overview": "TinyOS supported architecture.", "subsections": []},
            ], topics, evidence)

    def test_existing_subtopics_are_authoritative_and_content_formats_are_normalized(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
            {"subtopic_id": "comparison", "name": "Kernel Comparison"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling uses priorities. Synchronization uses mutex locks. Kernel comparison covers latency and memory.", "metadata": {}}])]
        raw = [{"topic_id": "wrong", "overview": "RTOS scheduling synchronization and kernel comparison concepts.", "subsections": [
            {"subtopic_id": "wrong-1", "content_type": "paragraph", "paragraph": "Scheduling uses priorities.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "wrong-2", "content_type": "bullets", "paragraph": "", "bullets": ["Synchronization uses mutex locks."], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "wrong-3", "content_type": "table", "paragraph": "", "bullets": [], "table": {"headers": ["Kernel", "Latency"], "rows": [["Comparison", "Memory latency"]]}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["subtopic_id"] for item in validated[0]["subsections"]], ["scheduling", "sync", "comparison"])
        self.assertEqual([item["subtopic_name"] for item in validated[0]["subsections"]], ["Scheduling", "Synchronization", "Kernel Comparison"])
        self.assertEqual([item["content"]["type"] for item in validated[0]["subsections"]], ["paragraph", "bullets", "table"])
        self.assertIn("model_subtopic_ids_overwritten_by_backend_order", validated[0]["grounding_warnings"])

    def test_subtopics_may_be_omitted_but_rendered_subset_keeps_backend_order(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
            {"subtopic_id": "memory", "name": "Memory"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling priorities and memory allocation are described.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "RTOS scheduling priorities and memory allocation.", "subsections": [
            {"subtopic_id": "memory", "content_type": "paragraph", "paragraph": "Memory allocation is described.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "scheduling", "content_type": "bullets", "paragraph": "", "bullets": ["Scheduling uses priorities."], "table": {"headers": [], "rows": []}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["subtopic_id"] for item in validated[0]["subsections"]], ["scheduling", "memory"])
        self.assertNotIn("model_subtopic_ids_overwritten_by_backend_order", validated[0]["grounding_warnings"])

    def test_incomplete_subtopic_set_with_wrong_ids_is_not_guessed(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling priorities and synchronization mutex locks.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "RTOS scheduling priorities and synchronization.", "subsections": [
            {"subtopic_id": "invented", "content_type": "paragraph", "paragraph": "Scheduling priorities.", "bullets": [], "table": {"headers": [], "rows": []}},
        ]}]
        with self.assertRaisesRegex(ValueError, "ambiguous subtopic identities"):
            summary_service._validated_topic_summaries(raw, topics, evidence)

    def test_frontend_renders_structured_notes_and_one_final_takeaway_block(self):
        script = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "frontend" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("function renderSummaryContent(content)", script)
        self.assertIn('content.type === "paragraph"', script)
        self.assertIn('content.type === "bullets"', script)
        self.assertIn('content.type === "table"', script)
        self.assertIn("topic.subsections || []", script)
        self.assertIn('title = "Summary & Key Takeaways"', script)
        self.assertNotIn("topic.key_takeaways", script)
        self.assertIn(".summary-table-wrap", styles)
        self.assertIn("overflow-x:auto", styles)

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
