import unittest
import gc
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend.topic_extractor import TopicExtractor, normalize_heading
from backend.ingest import add_topic_metadata, delete_stale_source_vectors, split_documents
from backend.rag_service import _chroma_filter
from config import CHUNK_OVERLAP, CHUNK_SIZE


class DeterministicEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(text) % 11), 1.0, 0.5] for text in texts]

    def embed_query(self, text):
        return [float(len(text) % 11), 1.0, 0.5]


class TopicExtractorTests(unittest.TestCase):
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
        self.assertIn("topic_001", {chunk.metadata["topic_id"] for chunk in chunks})
        self.assertIn("topic_002", {chunk.metadata["topic_id"] for chunk in chunks})
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
        self.assertEqual(topics[0]["topic_id"], "topic_001")
        self.assertEqual(topics[0]["start_page"], 1)

    def test_topic_metadata_is_stored_and_filterable_in_chroma(self):
        documents = [
            Document(
                page_content="TCP server socket",
                metadata={
                    "source": "lecture.pdf",
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
            _chroma_filter(["lecture.pdf"], "topic_002"),
            {"$and": [{"source": "lecture.pdf"}, {"topic_id": "topic_002"}]},
        )

    def test_schema_reindex_upserts_current_ids_and_removes_all_stale_source_vectors(self):
        store = Chroma(
            collection_name=f"schema_reindex_test_{uuid4().hex}",
            embedding_function=DeterministicEmbeddings(),
        )
        old_documents = [
            Document(page_content=f"old {index}", metadata={"source": "lecture.pdf"})
            for index in range(3)
        ]
        store.add_documents(old_documents, ids=["samehash_0", "samehash_1", "orphan_99"])
        replacement = [
            Document(
                page_content=f"new {index}",
                metadata={
                    "source": "lecture.pdf",
                    "chunk_id": f"samehash_{index}",
                    "topic_id": "topic_001",
                    "topic_name": "Replacement",
                },
            )
            for index in range(2)
        ]
        current_ids = ["samehash_0", "samehash_1"]
        store.add_documents(replacement, ids=current_ids)
        stale_ids = delete_stale_source_vectors(store, "lecture.pdf", current_ids)
        result = store.get(where={"source": "lecture.pdf"})
        store.delete_collection()

        self.assertEqual(stale_ids, ["orphan_99"])
        self.assertEqual(sorted(result["ids"]), current_ids)
        self.assertEqual(len(result["ids"]), len(set(result["ids"])))
        self.assertTrue(all(item["topic_id"] == "topic_001" for item in result["metadatas"]))


if __name__ == "__main__":
    unittest.main()
