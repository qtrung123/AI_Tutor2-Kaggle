"""Background indexing of what a PDF shows but its text layer does not say.

An upload answers as soon as the text is indexed. Afterwards one daemon thread goes through the
document's figures, one at a time:

- a figure that is mostly text (a code screenshot, a scanned page) is read by OCR alone;
- anything else also goes to the vision model, if enabled (AI_TUTOR_VISION), at most
  VISION_MAX_IMAGES_PER_DOCUMENT per document.

Results are stored as extra chunks of the same document, with its source and page, so RAG chat
retrieves and cites them like any other chunk. Their IDs ({file_hash}_visual_{n}) are stable and
stay out of the numbered text chunks. They carry a content_type, which keeps them out of the
whole-document readers (Quiz, Summary, Flashcards, Planner: backend/document_retrieval.py).

indexed_documents.visual_status records the progress and survives a restart:
NULL = never processed, pending = to do, done = finished, ocr_only = finished with Vision switched
off, partial = Vision is on but could not describe every figure. On start-up the API server resumes
pending documents, and ocr_only and partial ones when the vision model can be used.
"""
import argparse
import queue
import threading
from pathlib import Path

from langchain_core.documents import Document

from backend import document_storage, ingest, pdf_ocr, pdf_visuals, perf_timing, vision_service, visual_cache_store
from backend.indexed_document_store import (
    get_indexed_document,
    list_documents_by_visual_status,
    set_visual_status,
)
from config import VISION_MAX_IMAGES_PER_DOCUMENT, VISUAL_ENRICHMENT_ENABLED

# A figure mostly covered by recognized text is text (a code screenshot, a scanned page): OCR is
# enough. Labelled diagrams reach neither pair of (characters, covered area) on the course slides.
TEXT_FIGURE_THRESHOLDS = ((200, 0.30), (400, 0.20))
MIN_OCR_CHUNK_CHARS = 20
MAX_VISION_FAILURES = 2
FIGURE_DPI = 150
# Chunk numbers sort after every text chunk: base + page * 1000 + kind offset + piece.
_INDEX_BASE = 1_000_000
_KIND_OFFSET = {ingest.CONTENT_TYPE_OCR: 0, ingest.CONTENT_TYPE_FIGURE: 500}
_LABELS = {ingest.CONTENT_TYPE_OCR: "[Text in a figure or scanned page]", ingest.CONTENT_TYPE_FIGURE: "[Figure description]"}

_queue: queue.Queue = queue.Queue()
_queued: set[tuple[str, str]] = set()
_state_lock = threading.Lock()
_worker: threading.Thread | None = None


def schedule(owner_id: str, document_id: str) -> None:
    """Queue a document for the background worker (no-op when disabled or already queued)."""
    global _worker
    if not VISUAL_ENRICHMENT_ENABLED:
        return
    with _state_lock:
        if (owner_id, document_id) in _queued:
            return
        _queued.add((owner_id, document_id))
        _queue.put((owner_id, document_id))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="visual-enrichment", daemon=True)
            _worker.start()


def _run_worker() -> None:
    while True:
        owner_id, document_id = _queue.get()
        with _state_lock:
            _queued.discard((owner_id, document_id))
        try:
            process_document(owner_id, document_id)
        except Exception as error:   # the status stays pending and a restart retries the document
            print(f"[visual] processing failed, will retry after a restart ({type(error).__name__})")


def resume_pending() -> int:
    """Queue unfinished documents at start-up. Those waiting for Vision need the model to be there."""
    if not VISUAL_ENRICHMENT_ENABLED:
        return 0
    documents = list_documents_by_visual_status("pending")
    waiting = [*list_documents_by_visual_status("partial"), *list_documents_by_visual_status("ocr_only")]
    if waiting and vision_service.enabled() and vision_service.model_available():
        documents += waiting
    for document in documents:
        schedule(document["owner_id"], document["document_id"])
    return len(documents)


def _local_file(info: dict) -> Path:
    if document_storage.durable_storage_enabled():
        return document_storage.ensure_local_copy(info)
    return Path(str(info.get("path") or ""))


def _still_current(owner_id: str, document_id: str, file_hash: str) -> bool:
    current = get_indexed_document(owner_id, document_id)
    return bool(current and current.get("hash") == file_hash)


def _write_chunks(vectorstore, info: dict, page: int, kind: str, text: str) -> int:
    """Store one result as chunks of its page; returns how many (0 if the document changed meanwhile).

    Under the indexing lock, so a delete or re-index cannot run between the check and the write."""
    owner_id, document_id, file_hash = info["owner_id"], info["document_id"], info["hash"]
    label = _LABELS[kind]
    pieces = ingest.split_documents([Document(page_content=text, metadata={})])
    chunks, ids = [], []
    for piece_number, piece in enumerate(pieces):
        provenance = f"{file_hash}_visual_{_INDEX_BASE + page * 1000 + _KIND_OFFSET[kind] + piece_number}"
        chunks.append(Document(page_content=f"{label}\n{piece.page_content}", metadata={
            "source": document_id, "document_id": document_id, "owner_id": owner_id, "file_hash": file_hash,
            "page": page, "page_label": str(page + 1), "content_type": kind, "chunk_id": provenance,
        }))
        ids.append(ingest.owner_vector_ids(owner_id, [provenance])[0])
    with ingest.INDEXING_LOCK:
        if not _still_current(owner_id, document_id, file_hash):
            return 0
        vectorstore.add_documents(documents=chunks, ids=ids)
    return len(chunks)


def _cached_ocr(owner_id: str, file_hash: str, digest: str, image) -> pdf_ocr.OcrResult:
    """OCR of one figure. The text area is kept with the text, to classify it again without OCR."""
    key = f"ocr-figure|{pdf_ocr.OCR_ENGINE_VERSION}|{digest}"
    stored = visual_cache_store.get(owner_id, key)
    if stored is not None:
        area, _, text = stored.partition("\n")
        return pdf_ocr.OcrResult(text, 0.0, float(area or 0))
    if not pdf_ocr.available():
        return pdf_ocr.OcrResult("", 0.0, 0.0)
    result = pdf_ocr.read_image(image)
    visual_cache_store.put(owner_id, key, file_hash, f"{result.text_area:.4f}\n{result.text}")
    return result


def _is_text_figure(result: pdf_ocr.OcrResult) -> bool:
    return any(len(result.text) >= chars and result.text_area >= area for chars, area in TEXT_FIGURE_THRESHOLDS)


def process_document(owner_id: str, document_id: str) -> dict:
    """Index the visual content of one document. Safe to repeat: finished pages are skipped."""
    timer = perf_timing.stage_timer("[visual]")
    with perf_timing.recording(timer):
        try:
            stats = _process(owner_id, document_id)
            perf_timing.outcome(stats["status"])
            return stats
        finally:
            if timer is not None:
                timer.emit()


def _process(owner_id: str, document_id: str) -> dict:
    stats = {"status": "skipped", "figures": 0, "duplicates": 0, "blank": 0, "ocr_chunks": 0,
             "vision_chunks": 0, "vision_calls": 0, "vision_cached": 0, "vision_over_limit": 0}
    info = get_indexed_document(owner_id, document_id)
    if not info or not str(document_id).lower().endswith(".pdf"):
        return stats
    file_hash = info["hash"]
    file_path = _local_file(info)
    if not file_path.is_file():
        return stats

    vectorstore = ingest.get_vectorstore()
    stored = vectorstore.get(where={"$and": [{"owner_id": owner_id}, {"document_id": document_id}]}, include=["metadatas", "documents"])
    page_text: dict[int, str] = {}
    ocr_pages, written = set(), set()
    for metadata, text in zip(stored.get("metadatas") or [], stored.get("documents") or []):
        metadata = metadata or {}
        page, kind = int(metadata.get("page", -1) or 0), metadata.get("content_type")
        if kind:
            written.add((page, kind))
            continue
        page_text[page] = page_text.get(page, "") + " " + (text or "")
        if metadata.get("extraction") == "ocr":
            ocr_pages.add(page)

    with perf_timing.stage("analyze"):
        analyses = pdf_visuals.analyze_pdf(file_path)
        regions = pdf_visuals.figure_regions(analyses)
    stats["figures"] = len(regions)

    seen: list[int] = []
    vision_ready: bool | None = None   # asked once, only when a figure needs the model
    vision_missing = False
    vision_on = vision_service.enabled()
    vision_skipped = False             # a figure could have used Vision, but it is switched off
    failures = 0
    for page in sorted(regions):
        image = pdf_visuals.render_region(file_path, page, FIGURE_DPI, regions[page])
        if pdf_visuals.is_blank(image):
            stats["blank"] += 1
            continue
        fingerprint = pdf_visuals.dhash(image)
        if pdf_visuals.is_near_duplicate(fingerprint, seen):
            stats["duplicates"] += 1
            continue
        seen.append(fingerprint)
        digest = pdf_visuals.image_digest(image)
        with perf_timing.stage("ocr"):
            ocr = _cached_ocr(owner_id, file_hash, digest, image)
        needs_vision = not _is_text_figure(ocr)

        # Text from the image: already in the document when the page was read by OCR at upload.
        if (len(ocr.text) >= MIN_OCR_CHUNK_CHARS and page not in ocr_pages
                and (page, ingest.CONTENT_TYPE_OCR) not in written):
            with perf_timing.stage("write"):
                stats["ocr_chunks"] += _write_chunks(vectorstore, info, page, ingest.CONTENT_TYPE_OCR, ocr.text)

        if not needs_vision or (page, ingest.CONTENT_TYPE_FIGURE) in written or vision_missing:
            continue
        if not vision_on:
            vision_skipped = True
            continue
        key = vision_service.cache_key(digest)
        description = visual_cache_store.get(owner_id, key)
        if description is not None:
            stats["vision_cached"] += 1
        else:
            if stats["vision_calls"] >= VISION_MAX_IMAGES_PER_DOCUMENT:
                stats["vision_over_limit"] += 1
                continue
            if vision_ready is None:
                vision_ready = vision_service.model_available()
            if not vision_ready:
                vision_missing = True
                continue
            try:
                with perf_timing.stage("vision"):
                    description = vision_service.describe_image(image, page_text.get(page, ""), ocr.text)
                stats["vision_calls"] += 1
                failures = 0
            except vision_service.VisionUnavailable as error:
                failures += 1
                print(f"[visual] vision call failed ({error})")
                if failures >= MAX_VISION_FAILURES:
                    vision_missing = True
                continue
            visual_cache_store.put(owner_id, key, file_hash, description)
        if description:
            with perf_timing.stage("write"):
                stats["vision_chunks"] += _write_chunks(vectorstore, info, page, ingest.CONTENT_TYPE_FIGURE, description)

    perf_timing.count(**{name: value for name, value in stats.items() if name != "status"})
    stats["status"] = "partial" if vision_missing or failures else "ocr_only" if vision_skipped else "done"
    with ingest.INDEXING_LOCK:
        if _still_current(owner_id, document_id, file_hash):
            set_visual_status(owner_id, document_id, file_hash, stats["status"])
    print(f"[visual] finished: {stats}")
    return stats


def backfill(owner_id: str | None = None) -> list[dict]:
    """Process PDFs indexed before this feature existed. Run while the API server is stopped."""
    results = []
    for document in list_documents_by_visual_status(None, owner_id):
        if str(document["document_id"]).lower().endswith(".pdf"):
            results.append(process_document(document["owner_id"], document["document_id"]))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Index figures of already indexed PDFs.")
    parser.add_argument("--backfill", action="store_true", help="process PDFs indexed before this feature existed")
    parser.add_argument("--owner", help="only this owner's documents")
    args = parser.parse_args()
    if args.backfill:
        print(f"Processed {len(backfill(args.owner))} document(s).")


if __name__ == "__main__":
    main()
