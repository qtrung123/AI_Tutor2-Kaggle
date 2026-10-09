"""Stale topic metadata is re-extracted without re-embedding; current documents are left alone."""
import gc
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

from backend import auth_store, indexed_document_store, ingest, topic_extractor
from backend.auth_store import LEGACY_USER_ID
from backend.topic_extractor import TOPIC_SCHEMA_VERSION, TopicExtractor

EMBEDDED_PDF = Path(__file__).parents[1] / "data" / "Embedded Systems.pdf"
EMBEDDED_SECTIONS = [
    "Section 1: Embedded Systems",
    "Section 2: Characteristics of Embedded Operating Systems",
    "Section 3: eCos: Embedded Configurable Operating System",
    "Section 4: TinyOS",
]
STALE_FALLBACK_TOPICS = [{"topic_id": "topic_document_overview", "name": "Document Overview", "subtopics": []}]
RESTORED_SCHEMA_VERSION = 5


class CountingEmbeddings(Embeddings):
    def __init__(self):
        self.embedded_texts = 0

    def embed_documents(self, texts):
        self.embedded_texts += len(texts)
        return [[float(len(text) % 11), 1.0, 0.5] for text in texts]

    def embed_query(self, text):
        return [float(len(text) % 11), 1.0, 0.5]


def _no_llm_refiner(_model):
    def refine(_payload):
        raise AssertionError("structured fixtures must not need the LLM heading refiner")
    return refine


class TopicSchemaRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.embeddings = CountingEmbeddings()
        self.store = Chroma(collection_name=f"topic_refresh_{uuid4().hex}", embedding_function=self.embeddings)
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", root / "app.db"),
            patch.object(indexed_document_store, "DATABASE_PATH", root / "app.db"),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "missing.json"),
            patch.object(ingest, "DATA_DIR", root / "data"),
            patch.object(ingest, "VECTORSTORE_DIR", root / "vectorstore"),
            patch.object(ingest, "get_vectorstore", return_value=self.store),
            patch.object(ingest, "ollama_heading_refiner", _no_llm_refiner),
        ]
        for item in self.patches:
            item.start()
        topic_extractor._HIERARCHY_CACHE.clear()
        self.owner = auth_store.create_user("Kaggle", "kaggle-topics@example.com", "long-password-k")["id"]
        self.user_dir = root / "data" / "users" / self.owner
        self.user_dir.mkdir(parents=True)

    def tearDown(self):
        self.store.delete_collection()
        del self.store
        gc.collect()
        for item in reversed(self.patches):
            item.stop()
        topic_extractor._HIERARCHY_CACHE.clear()
        self.temp.cleanup()

    def index_pdf(self):
        path = self.user_dir / EMBEDDED_PDF.name
        shutil.copy2(EMBEDDED_PDF, path)
        ingest.index_files([path], self.owner)
        return path

    def make_stale(self, document_id, topics=STALE_FALLBACK_TOPICS, version=RESTORED_SCHEMA_VERSION):
        """Simulate a restored Kaggle row written by an older extractor schema."""
        info = indexed_document_store.get_indexed_document(self.owner, document_id)
        indexed_document_store.upsert_indexed_document(
            self.owner, document_id, {**info, "topic_schema_version": version, "topics": topics}
        )
        stored = self.store.get(where={"owner_id": self.owner})
        self.store._collection.update(ids=stored["ids"], metadatas=[
            {**metadata, "topic_id": topics[0]["topic_id"], "topic_name": topics[0]["name"]}
            for metadata in stored["metadatas"]
        ])

    def vectors(self):
        stored = self.store.get(where={"owner_id": self.owner}, include=["embeddings", "documents", "metadatas"])
        return stored["ids"], [list(vector) for vector in stored["embeddings"]], stored["documents"], stored["metadatas"]

    def counting_extract(self):
        return patch.object(TopicExtractor, "extract", autospec=True, side_effect=TopicExtractor.extract)

    def index_legacy_text(self):
        path = ingest.DATA_DIR / "legacy-notes.txt"
        path.write_text("1. Interrupts\nInterrupt handlers run briefly.\n2. Timers\nTimers trigger periodic work.\n", encoding="utf-8")
        ingest.index_files([path], LEGACY_USER_ID)
        return path

    def test_startup_refreshes_a_restored_stale_user_upload_without_re_embedding(self):
        self.assertEqual(TOPIC_SCHEMA_VERSION, 6)
        self.index_legacy_text()
        self.index_pdf()
        self.make_stale(EMBEDDED_PDF.name)  # restored row: schema 5, topic_document_overview, same file hash
        ids_before, embeddings_before, texts_before, _ = self.vectors()
        embedded_before = self.embeddings.embedded_texts

        with self.counting_extract() as extract, patch.object(self.store, "add_documents") as add_documents:
            ingest.main()  # the exact entry point deployment/start_kaggle.sh runs at startup

        self.assertEqual(extract.call_count, 1)
        add_documents.assert_not_called()
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        self.assertEqual(self.vectors()[:3], (ids_before, embeddings_before, texts_before))
        document = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual(document["topic_schema_version"], 6)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual({metadata["topic_name"] for metadata in self.vectors()[3]}, set(EMBEDDED_SECTIONS))
        # Owner scoping: the user upload stays the user's; the legacy scan only sees top-level data/ files.
        self.assertEqual([item["document_id"] for item in indexed_document_store.list_indexed_documents(LEGACY_USER_ID)],
                         ["legacy-notes.txt"])
        self.assertEqual([item["document_id"] for item in indexed_document_store.list_indexed_documents(self.owner)],
                         [EMBEDDED_PDF.name])

        # Second startup: hash and schema are current, so nothing is extracted, embedded or rewritten.
        metadatas_before = self.vectors()[3]
        with self.counting_extract() as extract, \
             patch.object(ingest, "refresh_topic_metadata") as refresh, \
             patch.object(ingest, "get_vectorstore", return_value=self.store) as open_chroma:
            ingest.main()
        extract.assert_not_called()
        refresh.assert_not_called()
        # Only the pre-existing legacy data/ scan opens Chroma; the topic refresh step does not.
        self.assertEqual(open_chroma.call_count, 1)
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        self.assertEqual(self.vectors()[3], metadatas_before)

    def test_refresh_never_attaches_topics_to_mismatched_vectors(self):
        self.index_pdf()
        self.make_stale(EMBEDDED_PDF.name)
        info = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        ids, embeddings, _texts, metadatas = self.vectors()
        self.store._collection.update(ids=[ids[0]], embeddings=[embeddings[0]], documents=["text that no longer matches the file"])
        extra = Path(self.temp.name) / "other.pdf"
        shutil.copy2(EMBEDDED_PDF, extra)
        extra.write_bytes(extra.read_bytes() + b"\n% changed")

        with self.counting_extract() as extract:
            self.assertIsNone(ingest.refresh_topic_metadata(self.store, self.user_dir / EMBEDDED_PDF.name, self.owner, info))
            self.assertIsNone(ingest.refresh_topic_metadata(self.store, extra, self.owner, info))  # different file hash
        extract.assert_not_called()
        self.assertEqual(self.vectors()[3], metadatas)
        stored = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual((stored["topic_schema_version"], stored["topics"]), (RESTORED_SCHEMA_VERSION, STALE_FALLBACK_TOPICS))

    def test_embedded_systems_fixture_extracts_the_four_sections(self):
        self.index_pdf()
        document = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)

    def test_stale_schema_is_re_extracted_at_startup_without_re_embedding(self):
        self.index_pdf()
        self.make_stale(EMBEDDED_PDF.name)
        ids_before, embeddings_before, texts_before, _ = self.vectors()
        embedded_before = self.embeddings.embedded_texts

        result = ingest.refresh_stale_topic_documents()

        self.assertEqual(result, {"refreshed": [f"{self.owner}/{EMBEDDED_PDF.name}"], "reindexed": [], "missing": []})
        document = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        ids_after, embeddings_after, texts_after, metadatas_after = self.vectors()
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        self.assertEqual((ids_after, embeddings_after, texts_after), (ids_before, embeddings_before, texts_before))
        self.assertNotIn("topic_document_overview", {metadata["topic_id"] for metadata in metadatas_after})
        self.assertEqual({metadata["topic_name"] for metadata in metadatas_after}, set(EMBEDDED_SECTIONS))

    def test_re_upload_of_unchanged_file_with_stale_schema_refreshes_topics_only(self):
        path = self.index_pdf()
        self.make_stale(EMBEDDED_PDF.name)
        embedded_before = self.embeddings.embedded_texts

        result = ingest.index_files([path], self.owner)

        self.assertEqual((result["new_files"], result["new_chunks"]), (0, 0))
        self.assertEqual(result["topic_refreshed_files"], [EMBEDDED_PDF.name])
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        document = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)

    def test_current_hash_and_schema_are_not_re_extracted(self):
        path = self.index_pdf()
        embedded_before = self.embeddings.embedded_texts
        with patch.object(TopicExtractor, "extract", autospec=True, side_effect=TopicExtractor.extract) as extract, \
             patch.object(ingest, "get_vectorstore", side_effect=AssertionError("startup must not open Chroma")):
            refresh = ingest.refresh_stale_topic_documents()
        with patch.object(TopicExtractor, "extract", autospec=True, side_effect=TopicExtractor.extract) as upload_extract:
            upload = ingest.index_files([path], self.owner)
        self.assertEqual(refresh, {"refreshed": [], "reindexed": [], "missing": []})
        extract.assert_not_called()
        upload_extract.assert_not_called()
        self.assertEqual(upload["skipped_files"], [EMBEDDED_PDF.name])
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)

    def test_genuinely_unstructured_document_still_falls_back_to_document_overview(self):
        path = self.user_dir / "notes.txt"
        path.write_text("ordinary prose about interrupts and timers ending with a period.", encoding="utf-8")
        ingest.index_files([path], self.owner)
        self.make_stale("notes.txt", topics=[{"topic_id": "topic_001", "name": "Old Topic", "subtopics": []}])

        ingest.refresh_stale_topic_documents()

        document = indexed_document_store.get_indexed_document(self.owner, "notes.txt")
        self.assertEqual([topic["topic_id"] for topic in document["topics"]], ["topic_document_overview"])
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)

    def test_changed_vectors_fall_back_to_a_full_re_index(self):
        self.index_pdf()
        self.make_stale(EMBEDDED_PDF.name)
        info = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        indexed_document_store.upsert_indexed_document(self.owner, EMBEDDED_PDF.name, {**info, "chunks": info["chunks"] + 1})
        embedded_before = self.embeddings.embedded_texts

        result = ingest.refresh_stale_topic_documents()

        self.assertEqual(result["reindexed"], [f"{self.owner}/{EMBEDDED_PDF.name}"])
        self.assertGreater(self.embeddings.embedded_texts, embedded_before)
        document = indexed_document_store.get_indexed_document(self.owner, EMBEDDED_PDF.name)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)


if __name__ == "__main__":
    unittest.main()
