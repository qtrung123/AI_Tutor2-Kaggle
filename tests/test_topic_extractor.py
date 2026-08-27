import unittest
import gc
from pathlib import Path
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend.topic_extractor import TopicExtractor, normalize_heading
from backend.ingest import add_topic_metadata, clean_documents, delete_stale_source_vectors, load_single_file, split_documents
from backend.rag_service import _chroma_filter
from backend.quiz_service import _result_to_chunks
from config import CHUNK_OVERLAP, CHUNK_SIZE


class DeterministicEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(text) % 11), 1.0, 0.5] for text in texts]

    def embed_query(self, text):
        return [float(len(text) % 11), 1.0, 0.5]


class TopicExtractorTests(unittest.TestCase):
    def _extract_text(self, text):
        return TopicExtractor().extract([Document(page_content=text, metadata={"page": 0})])[0]

    def test_embedded_systems_golden_hierarchy(self):
        path = Path(__file__).parents[1] / "data" / "Embedded Systems.pdf"
        documents = clean_documents(load_single_file(path))
        topics, _ = TopicExtractor().extract(documents)
        self.assertEqual([topic["name"] for topic in topics], [
            "Section 1: Embedded Systems",
            "Section 2: Characteristics of Embedded Operating Systems",
            "Section 3: eCos: Embedded Configurable Operating System",
            "Section 4: TinyOS",
        ])
        self.assertIn("Examples of Embedded Devices", [sub["name"] for sub in topics[0]["subtopics"]])
        self.assertIn("Wireless Sensor Network Topology", [sub["name"] for sub in topics[3]["subtopics"]])

    def test_chapter_section_family_uses_chapters_as_topics(self):
        topics = self._extract_text(
            "Chapter 1: Foundations\n" + "intro body " * 20 + "\nSection 1: Terms\n" + "terms " * 20
            + "\nChapter 2: Design\n" + "design " * 20 + "\nSection 2: Patterns\n" + "patterns " * 20
        )
        self.assertEqual([topic["name"] for topic in topics], ["Chapter 1: Foundations", "Chapter 2: Design"])
        self.assertEqual([sub["name"] for topic in topics for sub in topic["subtopics"]], ["Section 1: Terms", "Section 2: Patterns"])

    def test_unit_module_family_uses_units_as_topics(self):
        topics = self._extract_text(
            "Unit 1: Basics\n" + "body " * 30 + "\nModule 1: Vocabulary\n" + "body " * 30
            + "\nUnit 2: Practice\n" + "body " * 30 + "\nModule 2: Exercises\n" + "body " * 30
        )
        self.assertEqual([topic["name"] for topic in topics], ["Unit 1: Basics", "Unit 2: Practice"])

    def test_dotted_numbering_infers_depth_one_topics(self):
        topics = self._extract_text(
            "1 Overview\n" + "body " * 30 + "\n1.1 Definitions\n" + "body " * 30
            + "\n2 Architecture\n" + "body " * 30 + "\n2.1 Components\n" + "body " * 30
        )
        self.assertEqual([topic["name"] for topic in topics], ["1 Overview", "2 Architecture"])
        self.assertEqual([len(topic["subtopics"]) for topic in topics], [1, 1])

    def test_unnumbered_headings_use_content_span(self):
        topics = self._extract_text(
            "Course Overview\n" + "overview content " * 20 + "\nCore Principles\n" + "principle content " * 20
        )
        self.assertEqual([topic["name"] for topic in topics], ["Course Overview", "Core Principles"])

    def test_structural_ids_are_stable_when_unrelated_heading_is_appended(self):
        first = self._extract_text("1 Alpha\n" + "a " * 80 + "\n2 Beta\n" + "b " * 80)
        second = self._extract_text("1 Alpha\n" + "a " * 80 + "\n2 Beta\n" + "b " * 80 + "\n3 Gamma\n" + "c " * 80)
        self.assertEqual([topic["topic_id"] for topic in first], [topic["topic_id"] for topic in second[:2]])

    def test_boundaries_are_half_open_and_chunk_metadata_has_hierarchy(self):
        document = Document(page_content="1 Alpha\n" + "a " * 100 + "\n1.1 Detail\n" + "d " * 100 + "\n2 Beta\n" + "b " * 100, metadata={"page": 0})
        chunks = RecursiveCharacterTextSplitter(chunk_size=100, chunk_overlap=0, add_start_index=True).split_documents([document])
        topics = add_topic_metadata([document], chunks, [f"id_{i}" for i in range(len(chunks))], TopicExtractor())
        self.assertEqual(topics[0]["boundary"]["end"], topics[1]["boundary"]["start"])
        self.assertEqual(topics[0]["boundary"]["interval"], "half-open")
        detail = next(chunk for chunk in chunks if "d d d" in chunk.page_content)
        self.assertEqual(detail.metadata["topic_id"], topics[0]["topic_id"])
        self.assertTrue(detail.metadata["subtopic_id"].startswith("subtopic_"))
        self.assertTrue({"subtopic_name", "heading_path", "structure_confidence"} <= detail.metadata.keys())
    def test_owner_vector_id_is_distinct_from_canonical_provenance_id(self):
        chunks = _result_to_chunks({
            "ids": ["user-123_hash_0"], "documents": ["Grounded content"],
            "metadatas": [{"chunk_id": "hash_0", "document_id": "lecture.pdf", "owner_id": "user-123"}],
        }, "lecture.pdf", False)
        self.assertEqual(chunks[0]["metadata"]["vector_id"], "user-123_hash_0")
        self.assertEqual(chunks[0]["metadata"]["chunk_id"], "hash_0")

    def test_800_150_chunk_crossing_heading_uses_largest_character_overlap(self):
        page = Document(
            page_content=(
                "1. First Topic\n"
                + ("alpha " * 54)
                + "\n2. Second Topic\n"
                + ("beta " * 80)
                + "\n"
                + ("tail " * 100)
            ),
            metadata={"page": 0, "source": "crossing.pdf"},
        )
        chunks = split_documents([page])
        baseline_chunks = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
        ).split_documents([page])
        crossing = next(
            chunk
            for chunk in chunks
            if "1. First Topic" in chunk.page_content and "2. Second Topic" in chunk.page_content
        )
        topics = add_topic_metadata(
            [page], chunks, [f"cross_{index}" for index in range(len(chunks))], TopicExtractor()
        )
        second_heading_offset = page.page_content.index("2. Second Topic")
        chunk_start = crossing.metadata["start_index"]
        chunk_end = chunk_start + len(crossing.page_content)
        first_overlap = second_heading_offset - chunk_start
        second_overlap = chunk_end - second_heading_offset

        self.assertEqual((CHUNK_SIZE, CHUNK_OVERLAP), (800, 150))
        self.assertEqual(
            [chunk.page_content for chunk in chunks],
            [chunk.page_content for chunk in baseline_chunks],
        )
        self.assertGreater(second_overlap, first_overlap)
        self.assertEqual(crossing.metadata["topic_id"], topics[1]["topic_id"])
        self.assertEqual(crossing.metadata["topic_name"], "2. Second Topic")

    def test_multiple_topics_on_one_pdf_page_map_by_heading_offsets(self):
        page = Document(
            page_content=(
                "1. Networking Basics\n"
                + "Packets and links introduce networking. " * 12
                + "\n2. Transport Protocols\n"
                + "TCP provides reliable transport and UDP uses datagrams. " * 12
            ),
            metadata={"page": 0, "source": "lecture.pdf"},
        )
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=240, chunk_overlap=30, add_start_index=True
        )
        chunks = splitter.split_documents([page])
        extractor = TopicExtractor()
        ids = [f"abc123_{index}" for index in range(len(chunks))]
        topics = add_topic_metadata([page], chunks, ids, extractor=extractor)

        self.assertEqual([topic["name"] for topic in topics], ["1. Networking Basics", "2. Transport Protocols"])
        self.assertTrue(all(chunk.metadata["page"] == 0 for chunk in chunks))
        self.assertEqual({chunk.metadata["topic_id"] for chunk in chunks}, {topic["topic_id"] for topic in topics})
        self.assertTrue(all(topic["topic_id"].startswith("topic_") for topic in topics))
        self.assertEqual([chunk.metadata["chunk_id"] for chunk in chunks], ids)
        transport_chunks = [chunk for chunk in chunks if "TCP provides" in chunk.page_content]
        self.assertTrue(transport_chunks)
        self.assertEqual(transport_chunks[-1].metadata["topic_name"], "2. Transport Protocols")

    def test_repeated_headers_page_numbers_and_duplicate_headings_are_deduplicated(self):
        pages = [
            Document(page_content="COURSE HANDBOOK\n1\n1. Introduction\nFirst content", metadata={"page": 0}),
            Document(page_content="COURSE HANDBOOK\n2\n1. Introduction\nRepeated title", metadata={"page": 1}),
            Document(page_content="COURSE HANDBOOK\n3\n2. Architecture\nMore content", metadata={"page": 2}),
        ]
        headings = TopicExtractor().detect_headings(pages)
        normalized = [heading.normalized for heading in headings]

        self.assertNotIn(normalize_heading("COURSE HANDBOOK"), normalized)
        self.assertNotIn(normalize_heading("1. Introduction"), normalized)
        self.assertNotIn(normalize_heading("1"), normalized)

    def test_fallback_topic_for_unstructured_document(self):
        pages = [Document(page_content="ordinary prose ending with a period.", metadata={"page": 0})]
        topics, headings = TopicExtractor().extract(pages)
        self.assertEqual(headings, [])
        self.assertEqual(topics[0]["topic_id"], "topic_document_overview")
        self.assertEqual(topics[0]["start_page"], 1)

    def test_topic_metadata_is_stored_and_filterable_in_chroma(self):
        documents = [
            Document(
                page_content="TCP server socket",
                metadata={
                    "source": "lecture.pdf",
                    "document_id": "lecture.pdf",
                    "owner_id": "user-a",
                    "page": 6,
                    "chunk_id": "hash_0",
                    "topic_id": "topic_001",
                    "topic_name": "TCP Server",
                },
            ),
            Document(
                page_content="UDP datagram socket",
                metadata={
                    "source": "lecture.pdf",
                    "document_id": "lecture.pdf",
                    "owner_id": "user-a",
                    "page": 7,
                    "chunk_id": "hash_1",
                    "topic_id": "topic_002",
                    "topic_name": "UDP Client",
                },
            ),
        ]
        store = Chroma(
            collection_name=f"topic_metadata_test_{uuid4().hex}",
            embedding_function=DeterministicEmbeddings(),
        )
        store.add_documents(documents, ids=["hash_0", "hash_1"])
        result = store.get(where={"topic_id": "topic_002"})
        store.delete_collection()
        del store
        gc.collect()

        self.assertEqual(result["ids"], ["hash_1"])
        self.assertEqual(result["metadatas"][0]["chunk_id"], "hash_1")
        self.assertEqual(result["metadatas"][0]["topic_name"], "UDP Client")
        self.assertEqual(
            _chroma_filter("user-a", ["lecture.pdf"], "topic_002"),
            {"$and": [{"owner_id": "user-a"}, {"document_id": "lecture.pdf"}, {"topic_id": "topic_002"}]},
        )

    def test_schema_reindex_upserts_current_ids_and_removes_all_stale_source_vectors(self):
        store = Chroma(
            collection_name=f"schema_reindex_test_{uuid4().hex}",
            embedding_function=DeterministicEmbeddings(),
        )
        old_documents = [
            Document(page_content=f"old {index}", metadata={"source": "lecture.pdf", "document_id": "lecture.pdf", "owner_id": "user-a"})
            for index in range(3)
        ]
        store.add_documents(old_documents, ids=["samehash_0", "samehash_1", "orphan_99"])
        replacement = [
            Document(
                page_content=f"new {index}",
                metadata={
                    "source": "lecture.pdf",
                    "document_id": "lecture.pdf",
                    "owner_id": "user-a",
                    "chunk_id": f"samehash_{index}",
                    "topic_id": "topic_001",
                    "topic_name": "Replacement",
                },
            )
            for index in range(2)
        ]
        current_ids = ["samehash_0", "samehash_1"]
        store.add_documents(replacement, ids=current_ids)
        stale_ids = delete_stale_source_vectors(store, "user-a", "lecture.pdf", current_ids)
        result = store.get(where={"source": "lecture.pdf"})
        store.delete_collection()

        self.assertEqual(stale_ids, ["orphan_99"])
        self.assertEqual(sorted(result["ids"]), current_ids)
        self.assertEqual(len(result["ids"]), len(set(result["ids"])))
        self.assertTrue(all(item["topic_id"] == "topic_001" for item in result["metadatas"]))


if __name__ == "__main__":
    unittest.main()
