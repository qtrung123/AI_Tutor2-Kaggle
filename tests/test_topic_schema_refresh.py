"""Stale topic metadata is re-extracted without re-embedding; current documents are left alone."""
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from backend import auth_store, indexed_document_store, ingest, topic_extractor
from backend.auth_store import LEGACY_USER_ID
from backend.topic_extractor import TOPIC_SCHEMA_VERSION, TopicExtractor

PDF_NAME = "Embedded Systems.pdf"
EMBEDDED_SECTIONS = [
    "Section 1: Embedded Systems",
    "Section 2: Characteristics of Embedded Operating Systems",
    "Section 3: eCos: Embedded Configurable Operating System",
    "Section 4: TinyOS",
]
STALE_FALLBACK_TOPICS = [{"topic_id": "topic_document_overview", "name": "Document Overview", "subtopics": []}]
RESTORED_SCHEMA_VERSION = 5
RealPyPDFLoader = ingest.PyPDFLoader


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
            patch.object(ingest, "PyPDFLoader", fixture_loader(STRUCTURED_PAGES, PADDED_LAYOUT_PAGES)),
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
        path = self.user_dir / PDF_NAME
        path.write_bytes(b"%PDF-1.4 synthetic embedded systems fixture")
        ingest.index_files([path], self.owner)
        return path

    def test_unchanged_file_is_reindexed_when_its_vectors_are_gone(self):
        """PostgreSQL keeps the registry across a Kaggle reset but the local Chroma store is wiped:
        uploading the same file again must rebuild its vectors under the same document id."""
        path = self.index_pdf()
        before = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        skipped = ingest.index_files([path], self.owner)
        self.assertEqual(skipped["skipped_files"], [PDF_NAME])

        self.store.delete(ids=self.store.get(where={"owner_id": self.owner})["ids"])
        result = ingest.index_files([path], self.owner)

        self.assertEqual((result["new_files"], result["skipped_files"]), (1, []))
        self.assertEqual(len(self.store.get(where={"owner_id": self.owner})["ids"]), before["chunks"])
        after = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual((after["document_id"], after["hash"], after["topics"]),
                         (before["document_id"], before["hash"], before["topics"]))

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
        self.assertEqual(TOPIC_SCHEMA_VERSION, 7)
        self.index_legacy_text()
        self.index_pdf()
        self.make_stale(PDF_NAME)  # restored row: schema 5, topic_document_overview, same file hash
        ids_before, embeddings_before, texts_before, _ = self.vectors()
        embedded_before = self.embeddings.embedded_texts

        with self.counting_extract() as extract, patch.object(self.store, "add_documents") as add_documents:
            ingest.main()  # the exact entry point deployment/start_kaggle.sh runs at startup

        self.assertEqual(extract.call_count, 1)
        add_documents.assert_not_called()
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        self.assertEqual(self.vectors()[:3], (ids_before, embeddings_before, texts_before))
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual({metadata["topic_name"] for metadata in self.vectors()[3]}, set(EMBEDDED_SECTIONS))
        # Owner scoping: the user upload stays the user's; the legacy scan only sees top-level data/ files.
        self.assertEqual([item["document_id"] for item in indexed_document_store.list_indexed_documents(LEGACY_USER_ID)],
                         ["legacy-notes.txt"])
        self.assertEqual([item["document_id"] for item in indexed_document_store.list_indexed_documents(self.owner)],
                         [PDF_NAME])

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
        self.make_stale(PDF_NAME)
        info = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        ids, embeddings, _texts, metadatas = self.vectors()
        self.store._collection.update(ids=[ids[0]], embeddings=[embeddings[0]], documents=["text that no longer matches the file"])
        extra = Path(self.temp.name) / "other.pdf"
        extra.write_bytes(b"%PDF-1.4 a different file")

        with self.counting_extract() as extract:
            self.assertIsNone(ingest.refresh_topic_metadata(self.store, self.user_dir / PDF_NAME, self.owner, info))
            self.assertIsNone(ingest.refresh_topic_metadata(self.store, extra, self.owner, info))  # different file hash
        extract.assert_not_called()
        self.assertEqual(self.vectors()[3], metadatas)
        stored = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual((stored["topic_schema_version"], stored["topics"]), (RESTORED_SCHEMA_VERSION, STALE_FALLBACK_TOPICS))

    def test_embedded_systems_fixture_extracts_the_four_sections(self):
        self.index_pdf()
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)

    def test_stale_schema_is_re_extracted_at_startup_without_re_embedding(self):
        self.index_pdf()
        self.make_stale(PDF_NAME)
        ids_before, embeddings_before, texts_before, _ = self.vectors()
        embedded_before = self.embeddings.embedded_texts

        result = ingest.refresh_stale_topic_documents()

        self.assertEqual(result, {"refreshed": [f"{self.owner}/{PDF_NAME}"], "reindexed": [], "missing": []})
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        ids_after, embeddings_after, texts_after, metadatas_after = self.vectors()
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        self.assertEqual((ids_after, embeddings_after, texts_after), (ids_before, embeddings_before, texts_before))
        self.assertNotIn("topic_document_overview", {metadata["topic_id"] for metadata in metadatas_after})
        self.assertEqual({metadata["topic_name"] for metadata in metadatas_after}, set(EMBEDDED_SECTIONS))

    def test_re_upload_of_unchanged_file_with_stale_schema_refreshes_topics_only(self):
        path = self.index_pdf()
        self.make_stale(PDF_NAME)
        embedded_before = self.embeddings.embedded_texts

        result = ingest.index_files([path], self.owner)

        self.assertEqual((result["new_files"], result["new_chunks"]), (0, 0))
        self.assertEqual(result["topic_refreshed_files"], [PDF_NAME])
        self.assertEqual(self.embeddings.embedded_texts, embedded_before)
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
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
        self.assertEqual(upload["skipped_files"], [PDF_NAME])
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
        self.make_stale(PDF_NAME)
        info = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        indexed_document_store.upsert_indexed_document(self.owner, PDF_NAME, {**info, "chunks": info["chunks"] + 1})
        embedded_before = self.embeddings.embedded_texts

        result = ingest.refresh_stale_topic_documents()

        self.assertEqual(result["reindexed"], [f"{self.owner}/{PDF_NAME}"])
        self.assertGreater(self.embeddings.embedded_texts, embedded_before)
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)

    def test_schema_v6_document_overview_row_is_reconsidered_once(self):
        self.index_pdf()
        self.make_stale(PDF_NAME, version=6)  # what 6e56b05 could stamp on a collapsed-text extraction

        first = ingest.refresh_stale_topic_documents()
        second = ingest.refresh_stale_topic_documents()

        self.assertEqual(first["refreshed"], [f"{self.owner}/{PDF_NAME}"])
        self.assertEqual(second, {"refreshed": [], "reindexed": [], "missing": []})
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)

    def test_loader_change_refuses_in_place_refresh_and_fully_re_indexes(self):
        # Production state: a runtime whose plain extraction collapsed lines indexed the PDF as one overview topic.
        with patch.object(ingest, "PyPDFLoader", fixture_loader(COLLAPSED_PAGES, COLLAPSED_PAGES)):
            path = self.index_pdf()
        self.make_stale(PDF_NAME, version=6)
        info = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual(info["topics"], STALE_FALLBACK_TOPICS)
        topic_extractor._HIERARCHY_CACHE.clear()  # a new Kaggle runtime starts with an empty process cache
        embedded_before = self.embeddings.embedded_texts

        # Same runtime pypdf after the fix: plain still collapses, but layout extraction recovers the lines,
        # so the fresh split no longer matches the stored vectors.
        with patch.object(ingest, "PyPDFLoader", fixture_loader(COLLAPSED_PAGES, PADDED_LAYOUT_PAGES)):
            with self.counting_extract() as extract:
                self.assertIsNone(ingest.refresh_topic_metadata(self.store, path, self.owner, info))
            extract.assert_not_called()
            self.assertEqual(self.vectors()[3][0]["topic_id"], "topic_document_overview")
            result = ingest.refresh_stale_topic_documents()

        self.assertEqual(result, {"refreshed": [], "reindexed": [f"{self.owner}/{PDF_NAME}"], "missing": []})
        self.assertGreater(self.embeddings.embedded_texts, embedded_before)
        document = indexed_document_store.get_indexed_document(self.owner, PDF_NAME)
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        self.assertEqual([topic["name"] for topic in document["topics"]], EMBEDDED_SECTIONS)
        ids, _embeddings, _texts, metadatas = self.vectors()
        self.assertEqual(len(ids), document["chunks"])  # no vectors from the collapsed split survive
        self.assertEqual({metadata["topic_name"] for metadata in metadatas}, set(EMBEDDED_SECTIONS))

    def test_unstructured_pdf_stays_document_overview_and_becomes_current(self):
        path = self.user_dir / "prose.pdf"
        path.write_bytes(b"%PDF-1.4 unstructured fixture")
        with patch.object(ingest, "PyPDFLoader", fixture_loader([PROSE_PAGE], [PROSE_PAGE])):
            ingest.index_files([path], self.owner)
            self.make_stale("prose.pdf", version=6)

            ingest.refresh_stale_topic_documents()
            document = indexed_document_store.get_indexed_document(self.owner, "prose.pdf")
            with self.counting_extract() as extract:
                again = ingest.refresh_stale_topic_documents()
                upload = ingest.index_files([path], self.owner)

        self.assertEqual([topic["topic_id"] for topic in document["topics"]], ["topic_document_overview"])
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        self.assertEqual(again, {"refreshed": [], "reindexed": [], "missing": []})
        self.assertEqual(upload["skipped_files"], ["prose.pdf"])
        extract.assert_not_called()


    def test_layout_with_only_numbered_diagram_labels_is_rejected(self):
        path = self.user_dir / "diagrams.pdf"
        path.write_bytes(b"%PDF-1.4 diagram fixture")
        self.assertTrue(TopicExtractor().detect_headings(pages(DIAGRAM_LAYOUT_PAGES)))  # layout yields candidates

        with patch.object(ingest, "PyPDFLoader", fixture_loader(collapse(DIAGRAM_LAYOUT_PAGES), DIAGRAM_LAYOUT_PAGES)):
            ingest.index_files([path], self.owner)

        document = indexed_document_store.get_indexed_document(self.owner, "diagrams.pdf")
        self.assertEqual([topic["topic_id"] for topic in document["topics"]], ["topic_document_overview"])
        self.assertEqual(document["topic_schema_version"], TOPIC_SCHEMA_VERSION)
        stored_text = "\n".join(self.vectors()[2])
        self.assertNotIn("\n1 Request\n", stored_text)  # plain extraction (labels inline) was kept
        self.assertIn("1 Request 2 Reply", stored_text)

    def test_layout_with_tiny_ascending_numbered_labels_is_rejected(self):
        # TCP 2022 shape: unique, ascending "1 Request" / "3 Connect" labels score 0.96 structure confidence,
        # but "1 Request" governs only a few characters, so it is a label, not a section.
        layout = pages(TCP_STYLE_LAYOUT_PAGES)
        extractor = TopicExtractor()
        headings = sorted(extractor.detect_headings(layout), key=lambda heading: heading.document_offset)
        selected, confidence = extractor._select_major_candidates(headings, sum(len(d.page_content) + 1 for d in layout))
        self.assertEqual([heading.name for heading in selected], ["1 Request", "3 Connect"])
        self.assertGreaterEqual(confidence, topic_extractor._HIGH_CONFIDENCE)
        path = self.user_dir / "tcp.pdf"
        path.write_bytes(b"%PDF-1.4 tcp diagram fixture")

        with patch.object(ingest, "PyPDFLoader", fixture_loader(collapse(TCP_STYLE_LAYOUT_PAGES), TCP_STYLE_LAYOUT_PAGES)):
            ingest.index_files([path], self.owner)

        document = indexed_document_store.get_indexed_document(self.owner, "tcp.pdf")
        self.assertEqual([topic["topic_id"] for topic in document["topics"]], ["topic_document_overview"])
        self.assertNotIn("\n3 Connect\n", "\n".join(self.vectors()[2]))  # plain extraction was kept


def _section_page(section, subtopics, sentences=2):
    def prose(subject):
        return " ".join(f"this part explains how {subject.lower()} behaves on small devices with limited memory "
                        f"and strict timing, and why designers care about it in practice." for _ in range(sentences))
    lines = [section, prose(section)]
    for subtopic in subtopics:
        lines += [subtopic, prose(subtopic)]
    return "\n".join(lines)


# Synthetic stand-in for Embedded Systems.pdf (data/ is git-ignored): one labelled section per page.
STRUCTURED_PAGES = [
    _section_page(EMBEDDED_SECTIONS[0], ["Embedded System Organization", "Examples of Embedded Devices"]),
    _section_page(EMBEDDED_SECTIONS[1], [], sentences=3),
    _section_page(EMBEDDED_SECTIONS[2], ["eCos Kernel Design", "eCos Thread Synchronization"]),
    _section_page(EMBEDDED_SECTIONS[3], ["TinyOS Components", "TinyOS Scheduler"]),
]


def collapse(page_texts):
    """What a runtime whose plain extraction loses line structure returns: one line per page."""
    return [text.replace("\n", " ") for text in page_texts]


def pad(page_texts):
    """Layout-mode output: positional padding inside and around lines, plus blank spacer lines."""
    return ["\n\n".join(f"    {line.replace(' ', '  ')}   " for line in text.splitlines()) for text in page_texts]


COLLAPSED_PAGES = collapse(STRUCTURED_PAGES)
PADDED_LAYOUT_PAGES = pad(STRUCTURED_PAGES)
PROSE = ("the client sends a request to the server and waits for the reply while the server accepts the "
         "connection and answers it before both sides close the socket in an orderly way.")
PROSE_PAGE = ("ordinary prose about interrupts and timers that continues across the page without any "
              "heading lines, so neither extraction mode can recover structure from it.")
# Numbered sequence-diagram steps that restart per diagram, plus figure labels.
DIAGRAM_LAYOUT_PAGES = [
    f"{PROSE}\n1 Request\n2 Reply\nFigure 1\n{PROSE}",
    f"{PROSE}\n1 Connect\n2 Accept\n3 Close\nFigure 2\n{PROSE}",
]
# Socket Programming TCP 2022 shape: a tiny "1 Request" label, then "3 Connect" before the bulk of the text.
TCP_STYLE_LAYOUT_PAGES = [
    f"{PROSE}\n1 Request\nclient\n3 Connect\n{PROSE}",
    f"{PROSE} {PROSE}",
    f"{PROSE} {PROSE}",
]


def pages(page_texts, source="fixture.pdf"):
    return [Document(page_content=text, metadata={"source": source, "page": index}) for index, text in enumerate(page_texts)]


def fixture_loader(plain_pages, layout_pages):
    """Stand-in for PyPDFLoader returning fixed page text per extraction mode."""
    class Loader:
        def __init__(self, file_path, extraction_mode="plain", **_kwargs):
            self.file_path, self.mode = str(file_path), extraction_mode

        def load(self):
            return pages(layout_pages if self.mode == "layout" else plain_pages, self.file_path)
    return Loader


class PdfLoaderTests(unittest.TestCase):
    def setUp(self):
        topic_extractor._HIERARCHY_CACHE.clear()

    def load(self, plain_pages, layout_pages):
        with patch.object(ingest, "PyPDFLoader", fixture_loader(plain_pages, layout_pages)):
            return ingest.load_single_file(Path("fixture.pdf"))

    def topic_names(self, documents):
        return [topic["name"] for topic in TopicExtractor().extract(documents)[0]]

    def test_structured_plain_extraction_is_used_unchanged(self):
        documents = self.load(STRUCTURED_PAGES, PADDED_LAYOUT_PAGES)
        self.assertEqual([doc.page_content for doc in documents], STRUCTURED_PAGES)
        self.assertEqual(self.topic_names(documents), EMBEDDED_SECTIONS)

    def test_collapsed_plain_extraction_is_recovered_by_layout_extraction(self):
        collapsed = self.load(COLLAPSED_PAGES, COLLAPSED_PAGES)
        self.assertEqual(TopicExtractor().detect_headings(collapsed), [])

        documents = self.load(COLLAPSED_PAGES, PADDED_LAYOUT_PAGES)

        self.assertEqual([doc.page_content for doc in documents], STRUCTURED_PAGES)  # padding normalized away
        self.assertEqual(self.topic_names(documents), EMBEDDED_SECTIONS)

    def test_layout_that_loses_text_is_rejected(self):
        truncated = [text[: len(text) // 2] for text in STRUCTURED_PAGES]
        self.assertTrue(ingest._has_reliable_structure(pages(truncated)))  # rejected for text loss alone
        self.assertEqual([doc.page_content for doc in self.load(COLLAPSED_PAGES, truncated)], COLLAPSED_PAGES)


REAL_PDF_DIR = Path(__file__).parents[1] / "data"
EMBEDDED_PDF = REAL_PDF_DIR / "Embedded Systems.pdf"
TCP_2022_PDF = REAL_PDF_DIR / "Socket Programming TCP 2022.pdf"


class RealPdfIntegrationTests(unittest.TestCase):
    """Optional checks against local lecture PDFs; data/ is git-ignored, so these skip on a fresh clone."""

    def setUp(self):
        topic_extractor._HIERARCHY_CACHE.clear()

    def layout(self, path):
        return ingest.clean_documents(ingest._normalize_layout_text(RealPyPDFLoader(
            str(path), extraction_mode="layout", extraction_kwargs={"layout_mode_space_vertically": False}).load()))

    @unittest.skipUnless(EMBEDDED_PDF.is_file(), "local Embedded Systems.pdf not present")
    def test_embedded_systems_pdf(self):
        documents = ingest.load_single_file(EMBEDDED_PDF)
        self.assertEqual([topic["name"] for topic in TopicExtractor().extract(documents)[0]], EMBEDDED_SECTIONS)
        self.assertTrue(ingest._has_reliable_structure(self.layout(EMBEDDED_PDF)))

    @unittest.skipUnless(TCP_2022_PDF.is_file(), "local Socket Programming TCP 2022.pdf not present")
    def test_tcp_2022_pdf_layout_labels_are_not_reliable_structure(self):
        plain = ingest.clean_documents(RealPyPDFLoader(str(TCP_2022_PDF), extraction_mode="plain").load())
        documents = ingest.load_single_file(TCP_2022_PDF)
        self.assertEqual([doc.page_content for doc in documents], [doc.page_content for doc in plain])
        self.assertFalse(ingest._has_reliable_structure(self.layout(TCP_2022_PDF)))


if __name__ == "__main__":
    unittest.main()
