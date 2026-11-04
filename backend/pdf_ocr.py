"""English OCR for scanned PDF pages (RapidOCR, PP-OCRv6 tiny, CPU through ONNX Runtime).

A PDF whose every page has a text layer never reaches this module's OCR code or loads a model:
recover_scanned_pages() returns immediately. OCR is optional at runtime too: if the engine cannot
load, the document is indexed from its text layer as before.

Scanned pages are read during the upload, up to PDF_OCR_SYNC_SECONDS, so the document exists with
its text as soon as the upload answers. Pages past that budget are read by the background worker
(backend/visual_enrichment.py).
"""
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

from backend import pdf_visuals, perf_timing, visual_cache_store
from config import (
    PDF_OCR_DPI,
    PDF_OCR_ENABLED,
    PDF_OCR_MIN_PAGE_CHARS,
    PDF_OCR_MIN_SCORE,
    PDF_OCR_SYNC_SECONDS,
    PDF_OCR_THREADS,
)

OCR_ENGINE_VERSION = "ppocrv6-tiny-1"

_engine_lock = threading.Lock()
_engine = None
_engine_failed = False


@dataclass
class OcrResult:
    text: str
    confidence: float
    text_area: float   # share of the image covered by recognized text


def _load_engine():
    from rapidocr import ModelType, OCRVersion, RapidOCR

    return RapidOCR(params={
        "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.TINY,
        "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.TINY,
        "EngineConfig.onnxruntime.intra_op_num_threads": PDF_OCR_THREADS,
        "Global.log_level": "error",
    })


def available() -> bool:
    """Load the engine on first use. A failed load is reported once and not retried."""
    global _engine, _engine_failed
    if not PDF_OCR_ENABLED or _engine_failed:
        return False
    if _engine is None:
        with _engine_lock:
            if _engine is None and not _engine_failed:
                try:
                    _engine = _load_engine()
                except Exception as error:
                    _engine_failed = True
                    print(f"[ocr] engine unavailable, continuing without OCR ({type(error).__name__})")
    return _engine is not None


def _group_lines(boxes, texts: list[str]) -> list[str]:
    """Top-to-bottom lines, left-to-right inside a line."""
    items = []
    for box, text in zip(boxes, texts):
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        items.append(((min(ys) + max(ys)) / 2, min(xs), max(ys) - min(ys), text))
    if not items:
        return []
    heights = sorted(item[2] for item in items)
    tolerance = max(1.0, heights[len(heights) // 2] * 0.6)
    lines: list[list[tuple]] = []
    for item in sorted(items):
        if lines and abs(item[0] - lines[-1][-1][0]) <= tolerance:
            lines[-1].append(item)
        else:
            lines.append([item])
    return [" ".join(part[3] for part in sorted(line, key=lambda part: part[1])) for line in lines]


def read_image(image) -> OcrResult:
    """OCR one RGB PIL image. Raises when the engine is not available()."""
    import numpy as np

    with _engine_lock:   # one page at a time: bounded CPU, whatever the number of callers
        result = _engine(np.array(image))
    boxes, texts, scores = result.boxes, result.txts, result.scores
    if boxes is None or not texts:
        return OcrResult("", 0.0, 0.0)
    kept = [(box, text, score) for box, text, score in zip(boxes, texts, scores)
            if score >= PDF_OCR_MIN_SCORE and text.strip()]
    if not kept:
        return OcrResult("", 0.0, 0.0)
    area = 0.0
    for box, _text, _score in kept:
        xs, ys = [point[0] for point in box], [point[1] for point in box]
        area += (max(xs) - min(xs)) * (max(ys) - min(ys))
    return OcrResult(
        "\n".join(_group_lines([k[0] for k in kept], [k[1] for k in kept])),
        round(float(sum(k[2] for k in kept)) / len(kept), 3),
        float(min(1.0, area / max(1, image.width * image.height))),
    )


def _page_cache_key(file_hash: str, page: int) -> str:
    return f"ocr-page|{OCR_ENGINE_VERSION}|{PDF_OCR_DPI}|{file_hash}|{page}"


def _low_text_pages(documents: list, total_pages: int) -> list[int]:
    """Pages that are missing or have almost no text layer."""
    chars = {int(doc.metadata.get("page", -1)): len("".join((doc.page_content or "").split())) for doc in documents}
    return [page for page in range(total_pages) if chars.get(page, 0) < PDF_OCR_MIN_PAGE_CHARS]


def _total_pages(file_path: Path, documents: list) -> int:
    for doc in documents:
        total = doc.metadata.get("total_pages")
        if isinstance(total, int) and total > 0:
            return total
    return pdf_visuals.page_count(file_path)


def recover_scanned_pages(file_path: Path, documents: list, owner_id: str, file_hash: str) -> list:
    """Return `documents` with the text of scanned pages filled in, in page order.

    Pages with a usable text layer are returned untouched, so a normal PDF gets exactly the chunks
    (and vector IDs) it always had. Never raises: a PDF PDFium cannot read keeps its text layer.
    """
    if not PDF_OCR_ENABLED:
        return documents
    try:
        total = _total_pages(file_path, documents)
        low_text = _low_text_pages(documents, total)
        if not low_text:
            return documents
        scanned = pdf_visuals.scanned_pages(pdf_visuals.analyze_pdf(file_path, low_text))
        if not scanned or not available():
            return documents
        return _read_pages(file_path, documents, scanned, total, owner_id, file_hash)
    except Exception as error:
        print(f"[ocr] skipped, keeping the text layer ({type(error).__name__})")
        return documents


def _read_pages(file_path: Path, documents: list, scanned: list[int], total: int, owner_id: str, file_hash: str) -> list:
    by_page = {int(doc.metadata.get("page", -1)): doc for doc in documents}
    deadline = time.monotonic() + PDF_OCR_SYNC_SECONDS
    done = deferred = 0
    with perf_timing.stage("ocr"):
        for page in scanned:
            key = _page_cache_key(file_hash, page)
            text = visual_cache_store.get(owner_id, key)
            # The first page always runs, so a scan is never rejected as having no text at all.
            if text is None:
                if done and time.monotonic() >= deadline:
                    deferred += 1
                    continue
                try:
                    text = read_image(pdf_visuals.render_region(file_path, page, PDF_OCR_DPI)).text
                except Exception as error:   # this page keeps its text layer; the others still run
                    print(f"[ocr] a page could not be read ({type(error).__name__})")
                    continue
                visual_cache_store.put(owner_id, key, file_hash, text)
                done += 1
            existing = by_page.get(page)
            if len(text) <= len(existing.page_content if existing else ""):
                continue
            metadata = dict(existing.metadata) if existing else {
                "source": str(file_path), "total_pages": total, "page": page, "page_label": str(page + 1),
            }
            metadata["extraction"] = "ocr"
            by_page[page] = Document(page_content=text, metadata=metadata)
    perf_timing.count(ocr_pages=done, ocr_deferred=deferred)
    if deferred:
        print(f"[ocr] budget reached, {deferred} scanned page(s) left to background processing")
    return [by_page[page] for page in sorted(by_page)]
