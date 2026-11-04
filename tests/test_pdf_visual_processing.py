"""OCR and figure indexing for PDFs: real PDFium and real SQLite/registry, generated test PDFs;
the OCR engine, the vision model and the vector store are stand-ins (no models, no downloads)."""
import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document
from PIL import Image, ImageDraw
from pypdf import PdfWriter

from backend import document_retrieval, indexed_document_store, ingest, pdf_ocr, pdf_visuals
from backend import vision_service, visual_cache_store, visual_enrichment
from backend.auth_store import LEGACY_USER_ID

OWNER = LEGACY_USER_ID


def diagram(seed: int, size=(900, 600)) -> Image.Image:
    """A distinct box-and-arrow picture, large enough to count as a figure."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index in range(5):
        x = 40 + index * 160
        y = 80 + ((seed * 37 + index * 91) % 380)
        draw.rectangle([x, y, x + 120, y + 70], outline="black", fill=(seed * 40 % 255, 120, index * 50), width=4)
        draw.line([x + 120, y + 35, x + 160, 300], fill="black", width=3)
    return image


def pdf_from_images(path: Path, pages: list[Image.Image]) -> None:
    pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:], resolution=100)


def blank_pdf(path: Path, count: int) -> None:
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as file:
        writer.write(file)


class FakeVectorStore:
    def __init__(self):
        self.rows = {}

    def add_documents(self, documents, ids):
        for document, vector_id in zip(documents, ids):
            self.rows[vector_id] = document

    def get(self, ids=None, where=None, limit=None, include=None):
        keys = list(self.rows)
        return {"ids": keys, "documents": [self.rows[k].page_content for k in keys],
                "metadatas": [dict(self.rows[k].metadata) for k in keys]}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pdf_visuals_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class FastPathTests(Base):
    def test_pdf_with_a_text_layer_never_touches_pdfium_or_ocr(self):
        documents = [Document(page_content="x" * 300, metadata={"page": page, "total_pages": 2}) for page in range(2)]
        with patch.object(pdf_visuals, "analyze_pdf") as analyze, patch.object(pdf_ocr, "available") as available:
            result = pdf_ocr.recover_scanned_pages(self.dir / "absent.pdf", documents, OWNER, "h")
        self.assertIs(result, documents)
        analyze.assert_not_called()
        available.assert_not_called()

    def test_unreadable_pdf_keeps_its_text_and_never_raises(self):
        broken = self.dir / "broken.pdf"
        broken.write_bytes(b"not a pdf")
        documents = [Document(page_content="short", metadata={"page": 0, "total_pages": 1})]
        self.assertIs(pdf_ocr.recover_scanned_pages(broken, documents, OWNER, "h"), documents)

    def test_ocr_can_be_switched_off(self):
        with patch.object(pdf_ocr, "PDF_OCR_ENABLED", False), patch.object(pdf_visuals, "analyze_pdf") as analyze:
            self.assertEqual(pdf_ocr.recover_scanned_pages(self.dir / "a.pdf", [], OWNER, "h"), [])
        analyze.assert_not_called()


class ScanRecoveryTests(Base):
    def run_recovery(self, path, documents, file_hash, read=None):
        reader = read or (lambda image: pdf_ocr.OcrResult("Recovered English text from the scanned page. " * 4, 0.9, 0.4))
        with patch.object(pdf_ocr, "available", return_value=True), patch.object(pdf_ocr, "read_image", side_effect=reader) as ocr:
            return pdf_ocr.recover_scanned_pages(path, documents, OWNER, file_hash), ocr

    def test_scanned_pdf_gets_ocr_text_with_page_metadata_in_order(self):
        path = self.dir / "scan.pdf"
        pdf_from_images(path, [diagram(1), diagram(2), diagram(3)])
        documents, _ocr = self.run_recovery(path, [], "scan-hash")
        self.assertEqual([doc.metadata["page"] for doc in documents], [0, 1, 2])
        self.assertTrue(all(doc.metadata["extraction"] == "ocr" and doc.metadata["total_pages"] == 3 for doc in documents))

    def test_mixed_pdf_only_reads_the_scanned_page(self):
        path = self.dir / "mixed.pdf"
        pdf_from_images(path, [diagram(1), diagram(2)])
        text_page = Document(page_content="A real text layer. " * 20, metadata={"page": 1, "total_pages": 2})
        documents, ocr = self.run_recovery(path, [text_page], "mixed-hash")
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual([doc.metadata["page"] for doc in documents], [0, 1])
        self.assertIs(documents[1], text_page)

    def test_short_page_without_any_image_is_not_sent_to_ocr(self):
        path = self.dir / "title.pdf"
        blank_pdf(path, 1)
        title = [Document(page_content="Thank you", metadata={"page": 0, "total_pages": 1})]
        documents, ocr = self.run_recovery(path, title, "title-hash")
        ocr.assert_not_called()
        self.assertIs(documents, title)

    def test_slide_template_image_is_not_a_scan(self):
        path = self.dir / "template.pdf"
        same = diagram(5)
        pdf_from_images(path, [same, same, same, same])
        short = [Document(page_content="Title", metadata={"page": page, "total_pages": 4}) for page in range(4)]
        analyses = pdf_visuals.analyze_pdf(path)
        # Saved from one bitmap, every page holds the same image: decoration, not four scans.
        self.assertEqual(pdf_visuals.scanned_pages(analyses), [])
        _documents, ocr = self.run_recovery(path, short, "template-hash")
        ocr.assert_not_called()

    def test_second_run_uses_the_cache(self):
        path = self.dir / "cached.pdf"
        pdf_from_images(path, [diagram(1)])
        first, ocr_first = self.run_recovery(path, [], "cached-hash")
        second, ocr_second = self.run_recovery(path, [], "cached-hash")
        self.assertEqual(ocr_first.call_count, 1)
        self.assertEqual(ocr_second.call_count, 0)
        self.assertEqual(first[0].page_content, second[0].page_content)

    def test_time_budget_defers_pages_but_always_reads_one(self):
        path = self.dir / "long.pdf"
        pdf_from_images(path, [diagram(index) for index in range(1, 4)])
        with patch.object(pdf_ocr, "PDF_OCR_SYNC_SECONDS", 0):
            documents, ocr = self.run_recovery(path, [], "long-hash")
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual([doc.metadata["page"] for doc in documents], [0])

    def test_lines_are_grouped_top_to_bottom_left_to_right(self):
        def box(x, y):
            return [[x, y], [x + 50, y], [x + 50, y + 10], [x, y + 10]]
        lines = pdf_ocr._group_lines([box(100, 50), box(0, 2), box(100, 0)], ["third", "first", "second"])
        self.assertEqual(lines, ["first second", "third"])


class FigureFilterTests(Base):
    def test_small_banner_and_repeated_images_are_filtered(self):
        box = (0.0, 0.0, 300.0, 200.0)
        page = lambda index, images: pdf_visuals.PageAnalysis(index, 612, 792, images=images)
        analyses = [
            page(0, [((0, 0, 20, 20), "icon", 30, 30)]),
            page(1, [((0, 0, 600, 20), "banner", 1200, 40)]),
            page(2, [(box, "logo", 400, 300)]),
            page(3, [(box, "logo", 400, 300)]),
            page(4, [(box, "logo", 400, 300)]),
            page(5, [(box, "real", 400, 300)]),
            page(6, []),
        ]
        self.assertEqual(list(pdf_visuals.figure_regions(analyses)), [5])

    def test_vector_diagram_counts_but_a_few_rules_and_template_shapes_do_not(self):
        shapes = [(50.0 + i * 8, 100.0 + i * 8, 90.0 + i * 8, 130.0 + i * 8) for i in range(40)]
        template = [(10.0, 10.0 + i, 600.0, 11.0 + i) for i in range(40)]
        analyses = [pdf_visuals.PageAnalysis(0, 612, 792, paths=shapes + template)]
        analyses += [pdf_visuals.PageAnalysis(page, 612, 792, paths=template) for page in range(1, 6)]
        self.assertEqual(list(pdf_visuals.figure_regions(analyses)), [0])

    def test_rotated_page_uses_the_whole_page(self):
        analysis = pdf_visuals.PageAnalysis(0, 612, 792, rotation=90, images=[((0, 0, 100, 100), "a", 300, 300)])
        self.assertIsNone(pdf_visuals.figure_regions([analysis])[0])

    def test_crop_and_blank_and_near_duplicate(self):
        path = self.dir / "one.pdf"
        pdf_from_images(path, [diagram(1)])
        analysis = pdf_visuals.analyze_pdf(path)[0]
        whole = pdf_visuals.render_region(path, 0, 72)
        cropped = pdf_visuals.render_region(path, 0, 72, (0, 0, analysis.width / 2, analysis.height / 2))
        self.assertLess(cropped.width, whole.width)
        self.assertLess(cropped.height, whole.height)
        self.assertTrue(pdf_visuals.is_blank(Image.new("RGB", (200, 200), "white")))
        self.assertFalse(pdf_visuals.is_blank(diagram(1)))
        same = pdf_visuals.dhash(diagram(1))
        self.assertTrue(pdf_visuals.is_near_duplicate(pdf_visuals.dhash(diagram(1)), [same]))
        self.assertFalse(pdf_visuals.is_near_duplicate(pdf_visuals.dhash(diagram(3)), [same]))


class EnrichmentTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "figures.pdf"
        pdf_from_images(self.path, [diagram(1), diagram(1), diagram(3)])   # page 1 repeats page 0
        self.document_id = f"{self.dir.name}.pdf"
        self.file_hash = f"hash-{self.dir.name}"
        indexed_document_store.upsert_indexed_document(OWNER, self.document_id, {
            "hash": self.file_hash, "chunks": 1, "path": str(self.path), "topic_schema_version": 1,
            "visual_status": "pending",
        })
        self.addCleanup(indexed_document_store.delete_indexed_document, OWNER, self.document_id)
        self.addCleanup(visual_cache_store.delete_for_file, OWNER, self.file_hash)
        self.store = FakeVectorStore()
        self.store.add_documents([Document(page_content="Some text", metadata={"page": 0})], [f"{self.file_hash}_0"])
        self.vision_on = True

    def run_process(self, ocr=None, vision_available=True, describe="A flowchart with five connected boxes labelled in order."):
        ocr = ocr or pdf_ocr.OcrResult("", 0.0, 0.0)
        with patch.object(ingest, "get_vectorstore", return_value=self.store), \
                patch.object(visual_enrichment.pdf_ocr, "available", return_value=True), \
                patch.object(visual_enrichment.pdf_ocr, "read_image", return_value=ocr), \
                patch.object(vision_service, "enabled", return_value=self.vision_on), \
                patch.object(vision_service, "model_available", return_value=vision_available), \
                patch.object(vision_service, "describe_image", side_effect=[describe] * 10 if isinstance(describe, str) else describe) as call:
            return visual_enrichment.process_document(OWNER, self.document_id), call

    def visual_rows(self):
        return {key: doc for key, doc in self.store.rows.items() if ingest.is_visual_vector_id(key)}

    def status(self):
        return indexed_document_store.get_indexed_document(OWNER, self.document_id)["visual_status"]

    def test_figures_become_chunks_with_source_page_and_type(self):
        stats, call = self.run_process()
        self.assertEqual(call.call_count, 2)     # the near-duplicate page is skipped
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(self.status(), "done")
        rows = self.visual_rows()
        self.assertEqual({doc.metadata["page"] for doc in rows.values()}, {0, 2})
        for key, doc in rows.items():
            self.assertEqual(doc.metadata["source"], self.document_id)
            self.assertEqual(doc.metadata["document_id"], self.document_id)
            self.assertEqual(doc.metadata["owner_id"], OWNER)
            self.assertEqual(doc.metadata["content_type"], ingest.CONTENT_TYPE_FIGURE)
            self.assertTrue(doc.page_content.startswith("[Figure description]"))
            self.assertRegex(key, r"_visual_\d+$")   # chunk order: _(\d+)$, after every text chunk
            self.assertGreaterEqual(int(re.search(r"_(\d+)$", key).group(1)), 1_000_000)

    def test_repeat_run_does_no_work(self):
        self.run_process()
        count = len(self.visual_rows())
        stats, call = self.run_process()
        call.assert_not_called()
        self.assertEqual(len(self.visual_rows()), count)
        self.assertEqual(stats["vision_calls"], 0)

    def test_results_are_reused_after_vectors_are_lost(self):
        self.run_process()
        self.store = FakeVectorStore()
        _stats, call = self.run_process()
        call.assert_not_called()
        self.assertEqual(len(self.visual_rows()), 2)

    def test_text_figure_uses_ocr_and_no_vision(self):
        text = pdf_ocr.OcrResult("int main(void) { return 0; } " * 10, 0.95, 0.4)
        stats, call = self.run_process(ocr=text)
        call.assert_not_called()
        self.assertGreater(stats["ocr_chunks"], 0)
        self.assertEqual({doc.metadata["content_type"] for doc in self.visual_rows().values()}, {ingest.CONTENT_TYPE_OCR})

    def test_page_read_by_ocr_at_upload_is_not_indexed_twice(self):
        self.store.rows = {}
        for page in (0, 2):
            self.store.add_documents([Document(page_content="scan", metadata={"page": page, "extraction": "ocr"})], [f"{self.file_hash}_{page}"])
        stats, call = self.run_process(ocr=pdf_ocr.OcrResult("scan text " * 40, 0.9, 0.4))
        call.assert_not_called()
        self.assertEqual(stats["ocr_chunks"], 0)

    def test_without_the_model_ocr_text_is_kept_and_the_document_stays_partial(self):
        stats, call = self.run_process(ocr=pdf_ocr.OcrResult("Client Server Socket bind listen", 0.9, 0.02), vision_available=False)
        call.assert_not_called()
        self.assertEqual(stats["status"], "partial")
        self.assertEqual(self.status(), "partial")
        self.assertGreater(stats["ocr_chunks"], 0)

    def test_vision_off_indexes_ocr_text_makes_no_call_and_can_be_finished_later(self):
        self.vision_on = False
        stats, call = self.run_process(ocr=pdf_ocr.OcrResult("Client Server Socket bind listen", 0.9, 0.02))
        call.assert_not_called()
        self.assertEqual(stats["status"], "ocr_only")
        self.assertEqual(self.status(), "ocr_only")
        self.assertEqual({doc.metadata["content_type"] for doc in self.visual_rows().values()}, {ingest.CONTENT_TYPE_OCR})
        self.vision_on = True
        stats, call = self.run_process(ocr=pdf_ocr.OcrResult("Client Server Socket bind listen", 0.9, 0.02))
        self.assertGreater(call.call_count, 0)
        self.assertEqual(stats["status"], "done")

    def test_vision_outage_stops_after_two_failures_and_is_not_cached(self):
        pdf_from_images(self.path, [diagram(1), diagram(3), diagram(5), diagram(7)])
        stats, call = self.run_process(describe=vision_service.VisionUnavailable("ConnectError"))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(stats["status"], "partial")
        self.assertEqual(self.visual_rows(), {})
        _stats, call = self.run_process()
        self.assertGreater(call.call_count, 0)

    def test_image_limit_is_respected(self):
        pdf_from_images(self.path, [diagram(1), diagram(3), diagram(5), diagram(7)])
        with patch.object(visual_enrichment, "VISION_MAX_IMAGES_PER_DOCUMENT", 1):
            stats, call = self.run_process()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(stats["vision_over_limit"], 3)

    def test_nothing_is_written_for_a_document_deleted_during_the_vision_call(self):
        def delete_then_answer(*_args):
            indexed_document_store.delete_indexed_document(OWNER, self.document_id)
            return "A flowchart with five connected boxes labelled in order."
        self.run_process(describe=delete_then_answer)
        self.assertEqual(self.visual_rows(), {})

    def test_deleting_a_document_clears_its_cache(self):
        visual_cache_store.put(OWNER, "cache-key", self.file_hash, "text")
        visual_cache_store.delete_for_file(OWNER, self.file_hash)
        self.assertIsNone(visual_cache_store.get(OWNER, "cache-key"))

    def test_resume_finds_pending_documents(self):
        with patch.object(visual_enrichment, "VISUAL_ENRICHMENT_ENABLED", True), \
                patch.object(visual_enrichment, "schedule") as schedule, \
                patch.object(vision_service, "model_available", return_value=False):
            visual_enrichment.resume_pending()
        self.assertIn((OWNER, self.document_id), [call.args for call in schedule.call_args_list])


class VisionModeTests(unittest.TestCase):
    def test_auto_means_only_with_a_gpu(self):
        for mode, gpu, expected in (("auto", False, False), ("auto", True, True), ("off", True, False), ("on", False, True)):
            with self.subTest(mode=mode, gpu=gpu), patch.object(vision_service, "VISION_MODE", mode), \
                    patch.object(vision_service, "_has_gpu", return_value=gpu):
                self.assertIs(vision_service.enabled(), expected)

    def test_no_nvidia_tool_means_no_gpu(self):
        vision_service._has_gpu.cache_clear()
        self.addCleanup(vision_service._has_gpu.cache_clear)
        with patch.object(vision_service.shutil, "which", return_value=None):
            self.assertFalse(vision_service._has_gpu())

    def test_resume_does_not_touch_the_model_while_vision_is_off(self):
        with patch.object(visual_enrichment, "VISUAL_ENRICHMENT_ENABLED", True), \
                patch.object(visual_enrichment, "schedule"), \
                patch.object(vision_service, "enabled", return_value=False), \
                patch.object(vision_service, "model_available") as model_check, \
                patch.object(visual_enrichment, "list_documents_by_visual_status",
                             side_effect=lambda status: [{"owner_id": OWNER, "document_id": f"{status}.pdf"}]):
            self.assertEqual(visual_enrichment.resume_pending(), 1)
        model_check.assert_not_called()


class FailureIsolationTests(Base):
    """Nothing in OCR or figure indexing can fail a normal upload."""

    def scan(self, name="s.pdf"):
        path = self.dir / name
        pdf_from_images(path, [diagram(1), diagram(2)])
        return path

    def test_engine_that_cannot_load_leaves_the_text_layer(self):
        documents = [Document(page_content="short", metadata={"page": 0, "total_pages": 2})]
        with patch.object(pdf_ocr, "_engine", None), patch.object(pdf_ocr, "_engine_failed", False), \
                patch.object(pdf_ocr, "_load_engine", side_effect=ImportError("no rapidocr")):
            self.assertIs(pdf_ocr.recover_scanned_pages(self.scan(), documents, OWNER, "engine-hash"), documents)
            self.assertFalse(pdf_ocr.available())

    def test_missing_pdfium_leaves_the_text_layer(self):
        documents = [Document(page_content="short", metadata={"page": 0, "total_pages": 1})]
        with patch.object(pdf_visuals, "analyze_pdf", side_effect=ImportError("no pypdfium2")):
            self.assertIs(pdf_ocr.recover_scanned_pages(self.scan(), documents, OWNER, "h"), documents)

    def test_one_bad_page_does_not_lose_the_others(self):
        good = pdf_ocr.OcrResult("Recovered English text from the scanned page. " * 4, 0.9, 0.4)
        with patch.object(pdf_ocr, "available", return_value=True), \
                patch.object(pdf_ocr, "read_image", side_effect=[RuntimeError("bad page"), good]):
            documents = pdf_ocr.recover_scanned_pages(self.scan(), [], OWNER, "bad-page-hash")
        self.addCleanup(visual_cache_store.delete_for_file, OWNER, "bad-page-hash")
        self.assertEqual([doc.metadata["page"] for doc in documents], [1])

    def test_cache_failure_leaves_the_text_layer(self):
        documents = [Document(page_content="short", metadata={"page": 0, "total_pages": 2})]
        with patch.object(pdf_ocr, "available", return_value=True), \
                patch.object(visual_cache_store, "get", side_effect=RuntimeError("database down")):
            self.assertIs(pdf_ocr.recover_scanned_pages(self.scan(), documents, OWNER, "h"), documents)

    def test_worker_survives_a_failing_document(self):
        calls = []

        def process(owner, document):
            calls.append(document)
            if document == "bad.pdf":
                raise RuntimeError("boom")
            raise SystemExit   # ends the worker loop for this test

        visual_enrichment._queue.put((OWNER, "bad.pdf"))
        visual_enrichment._queue.put((OWNER, "next.pdf"))
        with patch.object(visual_enrichment, "process_document", side_effect=process), self.assertRaises(SystemExit):
            visual_enrichment._run_worker()
        self.assertEqual(calls, ["bad.pdf", "next.pdf"])

    def index(self, path, schedule_side_effect=None):
        """The real index_files() with stand-ins for the embedding model and heading refiner only."""
        store = FakeVectorStore()
        store.delete = lambda ids=None: None
        with patch.object(ingest, "get_vectorstore", return_value=store), \
                patch.object(ingest, "ollama_heading_refiner", return_value=lambda _payload: []), \
                patch.object(ingest, "migrate_legacy_vector_ownership"), \
                patch.object(visual_enrichment, "schedule", side_effect=schedule_side_effect) as schedule, \
                patch.object(pdf_ocr, "recover_scanned_pages", wraps=lambda p, d, o, h: d):
            result = ingest.index_files([path], OWNER)
        self.addCleanup(indexed_document_store.delete_indexed_document, OWNER, path.name)
        return result, schedule

    def text_pdf(self):
        from langchain_core.documents import Document as Page
        path = self.dir / f"{self.dir.name}-text.pdf"
        path.write_bytes(b"%PDF-1.4 stand-in")
        text = "Virtual memory maps pages to frames. " * 60
        return path, [Page(page_content=text, metadata={"page": 0, "total_pages": 1, "source": str(path)})]

    def test_upload_does_not_wait_for_figures_and_survives_a_scheduling_failure(self):
        path, pages = self.text_pdf()
        with patch.object(ingest, "load_pdf_pages", return_value=pages):
            result, schedule = self.index(path, schedule_side_effect=RuntimeError("no thread"))
        schedule.assert_called_once()
        self.assertEqual(result["new_files"], 1)
        stored = indexed_document_store.get_indexed_document(OWNER, path.name)
        self.assertEqual(stored["visual_status"], "pending")

    def test_refresh_of_a_document_from_before_ocr_never_runs_ocr(self):
        path, pages = self.text_pdf()
        with patch.object(ingest, "load_pdf_pages", return_value=pages), \
                patch.object(pdf_ocr, "recover_scanned_pages") as recover:
            ingest.load_document_chunks(path, OWNER, "h", use_ocr=False)
        recover.assert_not_called()


class IntegrationBoundaryTests(unittest.TestCase):
    def test_whole_document_readers_ignore_figure_chunks(self):
        result = {
            "ids": ["h_0", "h_visual_1000000"],
            "documents": ["text", "[Figure description]\nA chart"],
            "metadatas": [{"page": 0, "source": "a.pdf"}, {"page": 0, "source": "a.pdf", "content_type": ingest.CONTENT_TYPE_FIGURE}],
        }
        chunks = document_retrieval._result_to_chunks(result, "a.pdf", filter_source=False)
        self.assertEqual([chunk["content"] for chunk in chunks], ["text"])

    def test_figure_chunk_cites_like_a_text_chunk(self):
        from backend.rag_service import _build_citations, format_docs

        figure = Document(page_content="[Figure description]\nThe TCP handshake.", metadata={"source": "net.pdf", "page": 4})
        self.assertEqual(_build_citations([figure])[0]["page"], "4")
        self.assertEqual(_build_citations([figure])[0]["title"], "net.pdf")
        self.assertIn("Page: 4", format_docs([figure]))

    def test_stale_topic_refresh_ignores_figure_vectors(self):
        self.assertTrue(ingest.is_visual_vector_id("owner_abc_visual_1000000"))
        self.assertFalse(ingest.is_visual_vector_id("owner_abc_12"))

    def test_vision_answer_cleanup(self):
        self.assertEqual(vision_service.clean_description(" NONE "), "")
        self.assertEqual(vision_service.clean_description("A box"), "")
        self.assertTrue(vision_service.clean_description("A bar chart of CPU usage per process."))

    def test_vision_request_shape(self):
        sent = {}

        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"message": {"content": "A bar chart of CPU usage per process."}}

        class Client:
            def __init__(self, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def post(self, url, json):
                sent.update(url=url, body=json)
                return Response()

        with patch.object(vision_service.httpx, "Client", Client):
            answer = vision_service.describe_image(diagram(1), "page text", "ocr text")
        self.assertIn("bar chart", answer)
        self.assertTrue(sent["url"].endswith("/api/chat"))
        message = sent["body"]["messages"][0]
        self.assertIn("page text", message["content"])
        Image.open(io.BytesIO(__import__("base64").b64decode(message["images"][0]))).verify()
        self.assertEqual(sent["body"]["keep_alive"], vision_service.VISION_KEEP_ALIVE)


if __name__ == "__main__":
    unittest.main()
