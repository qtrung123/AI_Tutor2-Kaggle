import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, indexed_document_store, summary_service, summary_store
from frontend_source import frontend_script_text


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

    def test_cache_only_lookup_reports_a_missing_summary_without_generating_anything(self):
        """The screen asks "does it exist?" before offering Generate: no model call, no retrieval."""
        with patch.object(summary_service, "ChatOllama") as llm,              patch.object(summary_service, "get_topic_chunks") as retrieval,              patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            missing = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a", cache_only=True)
        self.assertEqual((missing["status"], missing["cache_hit"], missing["model_id"]), ("not_generated", False, "model-a"))
        llm.assert_not_called(); retrieval.assert_not_called()

    def test_cache_only_lookup_returns_a_saved_summary_of_the_same_model_only(self):
        self.generate(model="model-a")
        with patch.object(summary_service, "ChatOllama") as llm,              patch.object(summary_service, "resolve_generation_model", side_effect=lambda model: f"runtime-{model}"):
            same = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a", cache_only=True)
            other = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-b", cache_only=True)
        self.assertTrue(same["cache_hit"]); self.assertEqual(same["final_summary"]["overview"], "Whole document overview.")
        self.assertEqual(other["status"], "not_generated")
        llm.assert_not_called()

    def save_previous_format_summary(self, document_id="lecture.pdf"):
        """A summary saved before topic batching / derived sections (format v3)."""
        return summary_store.save_summary({
            "owner_id": self.alice, "document_id": document_id, "document_hash": "hash-v1", "topic_schema_version": 3,
            "summary_version": summary_service.PREVIOUS_STRUCTURED_SUMMARY_VERSIONS[0],
            "model_id": "model-a", "runtime_model": "runtime-model-a",
        }, [{"topic_id": "topic_b", "overview": "Saved earlier.", "subsections": []}],
           {"overview": "Saved earlier overview.", "key_takeaways": ["a", "b", "c", "d"]}, {})

    def test_a_structured_documents_previous_format_summary_is_kept_without_regenerating(self):
        saved = self.save_previous_format_summary()
        with patch.object(summary_service, "ChatOllama") as llm, \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            cached = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a")
        llm.assert_not_called()
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["summary_id"], saved["summary_id"])
        # The Planner / Study Pack also still sees it as the document's summary.
        info = summary_store.get_latest_summary_info(self.alice, "lecture.pdf", "hash-v1", 3,
                                                     summary_service.accepted_summary_versions(
                                                         indexed_document_store.get_indexed_document(self.alice, "lecture.pdf")["topics"]))
        self.assertEqual(info["summary_id"], saved["summary_id"])

    def test_regenerating_saves_the_current_format_which_then_wins(self):
        self.save_previous_format_summary()
        result, _retrieval, _prompts = self.generate(model="model-a", regenerate=True)
        self.assertEqual(result["summary_version"], summary_service.SUMMARY_VERSION)
        with patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            cached = summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a", cache_only=True)
        self.assertEqual((cached["summary_id"], cached["summary_version"]), (result["summary_id"], summary_service.SUMMARY_VERSION))

    def test_an_unstructured_documents_previous_format_summary_is_not_reused(self):
        indexed_document_store.upsert_indexed_document(self.alice, "notes.pdf", {
            "hash": "hash-v1", "chunks": 2, "path": "notes.pdf", "topic_schema_version": 3,
            "topics": [{"topic_id": "topic_document_overview", "name": "Document overview"}],   # no subtopics
        })
        self.save_previous_format_summary("notes.pdf")   # v3 had no subsections for such documents
        with patch.object(summary_service, "ChatOllama") as llm, \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            missing = summary_service.generate_document_summary(self.alice, "notes.pdf", model_id="model-a", cache_only=True)
        llm.assert_not_called()
        self.assertEqual(missing["status"], "not_generated")

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
        self.assertIn("Cover all important ideas from the evidence", prompts[0])
        self.assertIn("paraphrase freely when helpful", prompts[0])
        self.assertIn("Preserve useful details, definitions, characteristics, mechanisms, and comparisons", prompts[0])
        self.assertIn("REQUIRED_SUBTOPICS_IN_ORDER", prompts[0])
        self.assertIn('"content_type":"paragraph|bullets|table"', prompts[0])
        self.assertIn("Do not return per-topic key takeaways", prompts[0])
        self.assertNotIn(self.alice, prompts[1])
        self.assertNotIn("END_TOPIC", prompts[1])
        self.assertIn("4 to 7 document-level key takeaways", prompts[1])
        self.assertEqual(result["metrics"]["llm_calls"], 2)
        self.assertEqual(result["metrics"]["topic_generation_usage"]["input_tokens"], 100)

    def test_boundary_topic_with_no_meaningful_evidence_is_excluded(self):
        document = indexed_document_store.get_indexed_document(self.alice, "lecture.pdf")
        topics = [
            {"topic_id": "usable", "name": "Usable", "boundary": {"start": {}, "end": {}}},
            {"topic_id": "empty", "name": "Empty", "boundary": {"start": {}, "end": {}}},
        ]
        indexed_document_store.upsert_indexed_document(self.alice, "lecture.pdf", {**document, "topics": topics})
        FakeLlm.responses = [
            FakeResponse('{"topics":[{"topic_id":"usable","overview":"Usable grounded evidence is summarized.","subsections":[]}]}', 20, 10),
            FakeResponse('{"overview":"Grounded document overview.","key_takeaways":["One","Two","Three","Four"]}', 10, 5),
        ]
        with patch.object(summary_service, "get_schema_topic_evidence", side_effect=lambda _d, t, _o: (
            [{"content": "Usable grounded evidence", "metadata": {"chunk_id": "c1"}}]
            if t["topic_id"] == "usable" else []
        )), patch.object(summary_service, "ChatOllama", FakeLlm), \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model"):
            result = summary_service.generate_document_summary(self.alice, "lecture.pdf", regenerate=True)
        self.assertEqual([item["topic_id"] for item in result["topic_summaries"]], ["usable"])

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

    def test_duplicate_or_unknown_topic_ids_are_rejected(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [
            (topics[0], [{"content": "eCos configurable kernel synchronization mutex."}]),
            (topics[1], [{"content": "TinyOS event-driven nesC components tasks."}]),
        ]
        raw = [
            {"topic_id": "tinyos", "overview": "eCos configurable kernel synchronization.", "subsections": []},
            {"topic_id": "tinyos", "overview": "TinyOS event-driven nesC components.", "subsections": []},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate topic summaries IDs"):
            summary_service._validated_topic_summaries(raw, topics, evidence)
        raw[0]["topic_id"] = "invented"
        with self.assertRaisesRegex(ValueError, "unknown topic summaries IDs"):
            summary_service._validated_topic_summaries(raw, topics, evidence)

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

    def test_partial_schema_is_rejected_but_low_lexical_overlap_is_a_warning(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}, {"topic_id": "tinyos", "name": "TinyOS"}]
        evidence = [(topic, [{"content": f"{topic['name']} distinct supported architecture concepts."}]) for topic in topics]
        with self.assertRaisesRegex(ValueError, "1 topic summaries for 2 requested"):
            summary_service._validated_topic_summaries(
                [{"topic_id": "ecos", "overview": "eCos supported architecture.", "subsections": []}], topics, evidence
            )
        validated = summary_service._validated_topic_summaries([
            {"topic_id": "ecos", "overview": "Coordinates concurrent work while remaining adaptable to constrained deployments.", "subsections": []},
            {"topic_id": "tinyos", "overview": "Organizes reactive operations into lightweight deferred units.", "subsections": []},
        ], topics, evidence)
        self.assertTrue(all("low_lexical_support_from_topic_evidence" in item["grounding_warnings"] for item in validated))

    def test_explicitly_unrelated_or_refusal_output_is_rejected(self):
        topics = [{"topic_id": "ecos", "name": "eCos"}]
        evidence = [(topics[0], [{"content": "eCos provides configurable kernel synchronization."}])]
        with self.assertRaisesRegex(ValueError, "empty or obviously unrelated"):
            summary_service._validated_topic_summaries([
                {"topic_id": "ecos", "overview": "This output is unrelated to the supplied material.", "subsections": []}
            ], topics, evidence)

    def test_existing_subtopics_are_authoritative_and_content_formats_are_normalized(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
            {"subtopic_id": "comparison", "name": "Kernel Comparison"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling uses priorities. Synchronization uses mutex locks. Kernel comparison covers latency and memory.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "RTOS scheduling synchronization and kernel comparison concepts.", "subsections": [
            {"subtopic_id": "comparison", "content_type": "table", "paragraph": "", "bullets": [], "table": {"headers": ["Kernel", "Latency"], "rows": [["Comparison", "Memory latency"]]}},
            {"subtopic_id": "scheduling", "content_type": "paragraph", "paragraph": "Scheduling uses priorities.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "sync", "content_type": "bullets", "paragraph": "", "bullets": ["Synchronization uses mutex locks."], "table": {"headers": [], "rows": []}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["subtopic_id"] for item in validated[0]["subsections"]], ["scheduling", "sync", "comparison"])
        self.assertEqual([item["subtopic_name"] for item in validated[0]["subsections"]], ["Scheduling", "Synchronization", "Kernel Comparison"])
        self.assertEqual([item["content"]["type"] for item in validated[0]["subsections"]], ["paragraph", "bullets", "table"])

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

    def test_empty_unsupported_and_heading_only_subtopics_are_omitted(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "unsupported", "name": "Interrupt Handling"},
            {"subtopic_id": "trivial", "name": "Memory Management"},
            {"subtopic_id": "empty", "name": "Device Drivers"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling assigns priorities to runnable tasks.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "RTOS scheduling assigns priorities to runnable tasks.", "subsections": [
            {"subtopic_id": "empty", "content_type": "bullets", "paragraph": "", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "trivial", "content_type": "paragraph", "paragraph": "Memory management overview.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "scheduling", "content_type": "paragraph", "paragraph": "Scheduling assigns priorities to runnable tasks.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "unsupported", "content_type": "paragraph", "paragraph": "Interrupt handling is not detailed in the provided evidence.", "bullets": [], "table": {"headers": [], "rows": []}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["subtopic_id"] for item in validated[0]["subsections"]], ["scheduling"])
        self.assertEqual(validated[0]["subsections"][0]["content"]["text"], "Scheduling assigns priorities to runnable tasks.")

    def test_unsupported_extra_subtopic_ids_are_ignored_without_guessing(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
        ]}]
        evidence = [(topics[0], [{"content": "RTOS scheduling priorities and synchronization mutex locks.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "RTOS scheduling priorities and synchronization.", "subsections": [
            {"subtopic_id": "invented", "content_type": "paragraph", "paragraph": "Scheduling priorities.", "bullets": [], "table": {"headers": [], "rows": []}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual(validated[0]["subsections"], [])

    def test_missing_and_extra_model_subtopics_preserve_authoritative_identity_and_order(self):
        topics = [{"topic_id": "rtos", "name": "RTOS", "subtopics": [
            {"subtopic_id": "scheduling", "name": "Scheduling"},
            {"subtopic_id": "sync", "name": "Synchronization"},
        ]}]
        evidence = [(topics[0], [{"content": "Scheduling uses priorities. Synchronization uses mutex locks.", "metadata": {}}])]
        raw = [{"topic_id": "rtos", "overview": "Scheduling and synchronization are described.", "subsections": [
            {"subtopic_id": "invented", "content_type": "paragraph", "paragraph": "Unsupported extra.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "sync", "content_type": "paragraph", "paragraph": "Synchronization uses mutex locks.", "bullets": [], "table": {"headers": [], "rows": []}},
            {"subtopic_id": "also-invented", "content_type": "paragraph", "paragraph": "Another extra.", "bullets": [], "table": {"headers": [], "rows": []}},
        ]}]
        validated = summary_service._validated_topic_summaries(raw, topics, evidence)
        self.assertEqual([item["subtopic_id"] for item in validated[0]["subsections"]], ["sync"])
        self.assertEqual(validated[0]["subsections"][0]["subtopic_name"], "Synchronization")

    @staticmethod
    def paragraph(text, **identity):
        return {**identity, "content_type": "paragraph", "paragraph": text, "bullets": [], "table": {"headers": [], "rows": []}}

    def generate_with_topics(self, topics, evidence_by_topic, topic_response):
        document = indexed_document_store.get_indexed_document(self.alice, "lecture.pdf")
        indexed_document_store.upsert_indexed_document(self.alice, "lecture.pdf", {**document, "topics": topics})
        FakeLlm.responses = [
            FakeResponse(topic_response, 500, 400),
            FakeResponse('{"overview":"Grounded document overview.","key_takeaways":["One","Two","Three","Four"]}', 100, 50),
        ]
        FakeLlm.prompts = []
        with patch.object(summary_service, "get_schema_topic_evidence", side_effect=lambda _d, t, _o: evidence_by_topic[t["topic_id"]]), \
             patch.object(summary_service, "ChatOllama", FakeLlm), \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model"):
            result = summary_service.generate_document_summary(self.alice, "lecture.pdf", regenerate=True)
        return result, list(FakeLlm.prompts)

    def test_multi_topic_document_keeps_every_topic_and_its_subsections(self):
        boundary = {"start": {}, "end": {}}
        topics = [
            {"topic_id": "topic_embedded", "name": "Section 1: Embedded Systems", "boundary": boundary,
             "subtopics": [{"subtopic_id": "sub_devices", "name": "Examples of Embedded Devices"}]},
            {"topic_id": "topic_ecos", "name": "Section 3: eCos", "boundary": boundary,
             "subtopics": [{"subtopic_id": "sub_kernel", "name": "eCos Kernel Design"}]},
            {"topic_id": "topic_tinyos", "name": "Section 4: TinyOS", "boundary": boundary, "subtopics": []},
        ]
        evidence = {
            "topic_embedded": [{"content": "Embedded systems perform a dedicated function. Devices include braking controllers.", "metadata": {"chunk_id": "c1"}}],
            "topic_ecos": [{"content": "eCos kernel design uses configurable schedulers and synchronization.", "metadata": {"chunk_id": "c2"}}],
            "topic_tinyos": [{"content": "TinyOS targets wireless sensor networks with components and a scheduler.", "metadata": {"chunk_id": "c3"}}],
        }
        response = json.dumps({"topics": [
            {"topic_id": "topic_embedded", "overview": "Embedded systems perform a dedicated function.", "subsections": [
                self.paragraph("Braking controllers are typical embedded devices.", subtopic_id="sub_devices")]},
            {"topic_id": "topic_ecos", "overview": "eCos is a configurable embedded operating system.", "subsections": [
                self.paragraph("The kernel offers configurable schedulers and synchronization.", subtopic_id="sub_kernel")]},
            {"topic_id": "topic_tinyos", "overview": "TinyOS targets wireless sensor networks.", "subsections": []},
        ]})
        result, prompts = self.generate_with_topics(topics, evidence, response)
        self.assertEqual([item["topic_id"] for item in result["topic_summaries"]], ["topic_embedded", "topic_ecos", "topic_tinyos"])
        self.assertNotIn("topic_document_overview", [item["topic_id"] for item in result["topic_summaries"]])
        self.assertEqual([[sub["subtopic_id"] for sub in item["subsections"]] for item in result["topic_summaries"]],
                         [["sub_devices"], ["sub_kernel"], []])
        # Structured documents keep the authoritative-subtopic contract: nothing is derived.
        self.assertNotIn(summary_service.DERIVE_SECTIONS_MARKER, prompts[0])
        self.assertIn('"subtopic_id":"exact id"', prompts[0])
        self.assertEqual(result["final_summary"]["overview"], "Grounded document overview.")
        self.assertEqual(len(result["final_summary"]["key_takeaways"]), 4)

    def test_unstructured_fallback_document_gets_grounded_derived_sections(self):
        topics = [{"topic_id": "topic_document_overview", "name": "Document Overview",
                   "boundary": {"start": {}, "end": {}}, "subtopics": []}]
        # No spaces between words, as pypdf extracts Embedded Systems.pdf.
        evidence = {"topic_document_overview": [
            {"content": "Embeddedsystemsaretightlycoupledtotheirenvironment.Real-timeconstraintsapply.", "metadata": {"chunk_id": "c1"}},
            {"content": "eCosisaconfigurableoperatingsystem.TinyOStargetswirelesssensornetworks.", "metadata": {"chunk_id": "c2"}},
        ]}
        response = json.dumps({"topics": [{"topic_id": "topic_document_overview",
            "overview": "The lecture covers embedded systems and two embedded operating systems.", "subsections": [
                self.paragraph("Embedded systems are tightly coupled to their environment and face real-time constraints.", heading="Embedded Systems and Real-Time Constraints"),
                self.paragraph("eCos is a configurable operating system for embedded devices.", heading="eCos"),
                self.paragraph("Invented content about quantum compilers.", heading="Quantum Compilers"),
                self.paragraph("Duplicate heading content about eCos configuration.", heading="eCos"),
                self.paragraph("Overview.", heading="TinyOS Overview"),
                self.paragraph("TinyOS targets wireless sensor networks.", heading="TinyOS for Sensor Networks"),
            ]}]})
        result, prompts = self.generate_with_topics(topics, evidence, response)
        self.assertIn(f"SUBSECTIONS: {summary_service.DERIVE_SECTIONS_MARKER}", prompts[0])
        self.assertNotIn("REQUIRED_SUBTOPICS_IN_ORDER: []", prompts[0])
        [topic] = result["topic_summaries"]
        self.assertEqual(topic["topic_id"], "topic_document_overview")
        self.assertEqual([sub["subtopic_name"] for sub in topic["subsections"]],
                         ["Embedded Systems and Real-Time Constraints", "eCos", "TinyOS for Sensor Networks"])
        self.assertEqual([sub["subtopic_id"] for sub in topic["subsections"]],
                         [f"topic_document_overview_section_{n}" for n in (1, 2, 3)])
        self.assertEqual(len(result["final_summary"]["key_takeaways"]), 4)

    def test_unstructured_fallback_without_derivable_sections_still_returns_overview(self):
        topics = [{"topic_id": "topic_document_overview", "name": "Document Overview",
                   "boundary": {"start": {}, "end": {}}, "subtopics": []}]
        evidence = {"topic_document_overview": [{"content": "A short note about interrupts.", "metadata": {"chunk_id": "c1"}}]}
        response = json.dumps({"topics": [{"topic_id": "topic_document_overview",
                                           "overview": "A short note about interrupts.", "subsections": []}]})
        result, _prompts = self.generate_with_topics(topics, evidence, response)
        self.assertEqual([(item["topic_id"], item["subsections"]) for item in result["topic_summaries"]],
                         [("topic_document_overview", [])])
        self.assertEqual(result["final_summary"]["overview"], "Grounded document overview.")

    def test_frontend_renders_structured_notes_and_one_final_takeaway_block(self):
        script = frontend_script_text()
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


SYNTHESIS_REPLY = '{"overview":"Whole document overview.","key_takeaways":["One point","Two point","Three point","Four point"]}'


def _topic_ids_in(prompt: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("TOPIC_ID:")]


def _topic_reply(topic_ids) -> str:
    return json.dumps({"topics": [{"topic_id": topic_id, "overview": f"Evidence {topic_id} is summarized.",
                                   "subsections": []} for topic_id in topic_ids]})


class ScriptedLlm:
    """Answers each topic call from a script keyed by call number (missing → a complete reply for the
    prompt's own topics) and the synthesis call with SYNTHESIS_REPLY. Records prompts and options."""
    script: dict = {}
    prompts: list = []
    options: list = []

    def __init__(self, **kwargs):
        ScriptedLlm.options.append(kwargs)

    def invoke(self, prompt):
        ScriptedLlm.prompts.append(prompt)
        if "END_TOPIC" not in prompt:
            return FakeResponse(SYNTHESIS_REPLY, 40, 12)
        reply = ScriptedLlm.script.get(sum("END_TOPIC" in item for item in ScriptedLlm.prompts), _topic_reply)
        if isinstance(reply, Exception):
            raise reply
        content = reply(_topic_ids_in(prompt)) if callable(reply) else reply
        return FakeResponse(content, 100, 30)


class SummaryTopicRecoveryTests(unittest.TestCase):
    """Summary model returned 1 topic summaries for 10 requested: batching and missing-topic recovery."""

    TOPIC_IDS = [f"topic_{index:02d}" for index in range(1, 11)]
    tearDown = SummaryFeatureTests.tearDown
    chunks = staticmethod(SummaryFeatureTests.chunks)
    generate = SummaryFeatureTests.generate

    def setUp(self):
        SummaryFeatureTests.setUp(self)
        indexed_document_store.upsert_indexed_document(self.alice, "lecture.pdf", {
            "hash": "hash-v1", "chunks": 10, "path": "lecture.pdf", "topic_schema_version": 3,
            "topics": [{"topic_id": topic_id, "name": f"Name {topic_id}"} for topic_id in self.TOPIC_IDS],
        })
        ScriptedLlm.script, ScriptedLlm.prompts, ScriptedLlm.options = {}, [], []

    def run_summary(self, script=None, regenerate=True):
        ScriptedLlm.script = script or {}
        ScriptedLlm.prompts, ScriptedLlm.options = [], []
        with patch.object(summary_service, "ChatOllama", ScriptedLlm), \
             patch.object(summary_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(summary_service, "resolve_generation_model", return_value="runtime-model-a"):
            return summary_service.generate_document_summary(self.alice, "lecture.pdf", model_id="model-a",
                                                             regenerate=regenerate)

    def topic_prompts(self):
        return [prompt for prompt in ScriptedLlm.prompts if "END_TOPIC" in prompt]

    def test_ten_topics_are_requested_in_bounded_batches_with_explicit_context_and_output_limits(self):
        result = self.run_summary()
        self.assertEqual([_topic_ids_in(prompt) for prompt in self.topic_prompts()],
                         [self.TOPIC_IDS[0:4], self.TOPIC_IDS[4:8], self.TOPIC_IDS[8:10]])
        self.assertEqual([item["topic_id"] for item in result["topic_summaries"]], self.TOPIC_IDS)
        self.assertEqual((result["metrics"]["llm_calls"], result["metrics"]["topic_retries"]), (4, 0))
        self.assertEqual(result["metrics"]["topic_generation_usage"]["input_tokens"], 300)
        options = ScriptedLlm.options[0]
        self.assertEqual(options["num_ctx"], summary_service.SUMMARY_NUM_CTX)
        self.assertEqual(options["num_predict"],
                         summary_service.SUMMARY_TOPICS_PER_CALL * summary_service.SUMMARY_OUTPUT_TOKENS_PER_TOPIC)
        # A one-topic batch of a structured document must not switch to derived (unstructured) sections.
        self.assertIn("REQUIRED_SUBTOPICS_IN_ORDER", self.topic_prompts()[2])
        self.assertNotIn(summary_service.DERIVE_SECTIONS_MARKER, self.topic_prompts()[2])

    def test_reply_with_one_of_requested_topics_keeps_it_and_retries_only_the_missing_ones(self):
        result = self.run_summary({1: _topic_reply(self.TOPIC_IDS[:1])})
        prompts = self.topic_prompts()
        self.assertEqual(_topic_ids_in(prompts[1]), self.TOPIC_IDS[1:4])   # the retry asks for the 3 missing only
        self.assertEqual([item["topic_id"] for item in result["topic_summaries"]], self.TOPIC_IDS)
        self.assertEqual((result["metrics"]["llm_calls"], result["metrics"]["topic_retries"]), (5, 1))

    def test_malformed_or_truncated_json_is_retried_for_that_batch_only(self):
        truncated = _topic_reply(self.TOPIC_IDS[4:8])[:-40]
        result = self.run_summary({1: "not json at all", 3: truncated})
        self.assertEqual([_topic_ids_in(prompt) for prompt in self.topic_prompts()], [
            self.TOPIC_IDS[0:4], self.TOPIC_IDS[0:4], self.TOPIC_IDS[4:8], self.TOPIC_IDS[4:8], self.TOPIC_IDS[8:10]])
        self.assertEqual(len(result["topic_summaries"]), 10)
        self.assertEqual(result["metrics"]["topic_retries"], 2)

    def test_runtime_error_duplicate_and_invalid_topics_are_recovered_without_guessing(self):
        duplicate = json.dumps({"topics": [
            {"topic_id": "topic_01", "overview": "Evidence topic_01 is summarized.", "subsections": []},
            {"topic_id": "topic_01", "overview": "Evidence topic_01 again.", "subsections": []},
            {"topic_id": "invented", "overview": "Invented topic.", "subsections": []},
            {"topic_id": "topic_02", "overview": "", "subsections": []},
            {"topic_id": "topic_03", "overview": "Evidence topic_03 is summarized.", "subsections": [
                {"subtopic_id": "x", "content_type": "unsupported"}]},
            {"topic_id": "topic_04", "overview": "Evidence topic_04 is summarized.", "subsections": []},
        ]})
        result = self.run_summary({1: duplicate, 3: RuntimeError("token repeat limit reached")})
        prompts = self.topic_prompts()
        self.assertEqual(_topic_ids_in(prompts[1]), ["topic_01", "topic_02"])   # topic_03's bad subsection is ignored
        self.assertEqual(_topic_ids_in(prompts[3]), self.TOPIC_IDS[4:8])
        self.assertEqual(result["topic_summaries"][0]["overview"], "Evidence topic_01 is summarized.")
        self.assertEqual(len(result["topic_summaries"]), 10)

    def test_topic_failing_validation_is_retried_alone(self):
        bad_table = json.dumps({"topics": [
            *json.loads(_topic_reply(self.TOPIC_IDS[0:3]))["topics"],
            {"topic_id": "topic_04", "overview": "Evidence topic_04 is summarized.", "subsections": [
                {"subtopic_id": "s", "content_type": "table", "table": {"headers": ["A", "B"], "rows": [["only one"]]}}]},
        ]})
        indexed = indexed_document_store.get_indexed_document(self.alice, "lecture.pdf")
        topics = indexed["topics"]
        topics[3]["subtopics"] = [{"subtopic_id": "s", "name": "Table"}]
        indexed_document_store.upsert_indexed_document(self.alice, "lecture.pdf", {**indexed, "topics": topics})
        result = self.run_summary({1: bad_table})
        self.assertEqual(_topic_ids_in(self.topic_prompts()[1]), ["topic_04"])
        self.assertEqual(len(result["topic_summaries"]), 10)

    def test_topics_still_missing_after_the_retry_fail_without_touching_the_saved_summary(self):
        saved = self.run_summary(regenerate=False)
        with self.assertRaisesRegex(ValueError, r"did not return valid notes for 3 of 10 topics after 2 attempts: "
                                                r"topic_02, topic_03, topic_04"):
            self.run_summary({1: _topic_reply(self.TOPIC_IDS[:1]), 2: _topic_reply([])})
        self.assertEqual(len(self.topic_prompts()), 2)   # bounded: no further calls, no synthesis
        identity = {"owner_id": self.alice, "document_id": "lecture.pdf", "document_hash": "hash-v1",
                    "topic_schema_version": 3, "summary_version": summary_service.SUMMARY_VERSION,
                    "model_id": "model-a", "runtime_model": "runtime-model-a"}
        self.assertEqual(summary_store.get_compatible_summary(identity)["summary_id"], saved["summary_id"])

    def test_documents_with_few_topics_still_use_a_single_topic_call(self):
        result, _retrievals, prompts = self.generate(self.bob)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(result["metrics"]["topic_calls"], 1)

    def test_prompt_plus_reserved_output_fits_the_context_and_oversized_topics_are_sampled(self):
        big_chunks = [{"content": f"Chunk {index} " + "evidence " * 130, "metadata": {"chunk_id": f"c{index}", "chunk": index}}
                      for index in range(300)]
        topics = [{"topic_id": "big", "name": "Big"}, {"topic_id": "small", "name": "Small"}]
        evidence = [(topics[0], big_chunks), (topics[1], big_chunks[:3])]
        batches, prompt_evidence, sampled = summary_service._topic_batches("lecture.pdf", evidence, False)
        budget = summary_service.SUMMARY_NUM_CTX - summary_service.SUMMARY_TOPICS_PER_CALL * summary_service.SUMMARY_OUTPUT_TOKENS_PER_TOPIC
        for batch in batches:
            prompt = summary_service._topic_prompt("lecture.pdf", [prompt_evidence[index] for index in batch], False)
            self.assertLessEqual(summary_service._token_estimate(prompt), budget)
        kept = prompt_evidence[0][1]
        self.assertEqual(sampled, 1)
        self.assertLess(len(kept), len(big_chunks))
        self.assertEqual((kept[0]["metadata"]["chunk_id"], kept[-1]["metadata"]["chunk_id"]), ("c0", "c299"))
        self.assertEqual(prompt_evidence[1][1], big_chunks[:3])


if __name__ == "__main__":
    unittest.main()
