"""OCR-only benchmark of PDF processing, on real files. Not a pytest test.

    python tests/benchmark_pdf_visuals.py --pdf-dir data --figures-pdf "data/Lecture 4 - Socket Programming - TCP.pdf"
        --scan-pdf "data/Lecture 5 - Socket Programming - UDP.pdf" --query "three-way handshake SYN ACK"

Writes only to a temporary directory: the app's database, vectors and uploads are not touched.
Needs Ollama with the embedding model for --figures-pdf. Vision is off: it takes minutes per image on a CPU.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="ai_tutor_bench_"))
os.environ["AI_TUTOR_DATA_DIR"] = str(ROOT / "data")
os.environ["AI_TUTOR_DATABASE_PATH"] = str(ROOT / "data" / "bench.db")
os.environ["AI_TUTOR_VECTORSTORE_DIR"] = str(ROOT / "vectorstore")
os.environ["AI_TUTOR_INDEXED_FILES_PATH"] = str(ROOT / "indexed_files.json")
os.environ["AI_TUTOR_VISUAL_ENRICHMENT"] = "0"   # process_document is called directly below
os.environ["AI_TUTOR_VISION"] = "off"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import ingest, pdf_ocr, pdf_visuals, rag_service, visual_enrichment  # noqa: E402
from backend.auth_store import LEGACY_USER_ID as OWNER  # noqa: E402


def timed(function, repeat=3):
    results, times = None, []
    for _ in range(repeat):
        started = time.perf_counter()
        results = function()
        times.append(time.perf_counter() - started)
    return results, round(statistics.median(times), 3)


def digest(chunks) -> str:
    return hashlib.sha256("\x00".join(chunk.page_content for chunk in chunks).encode()).hexdigest()[:16]


def bench_parse(pdf_dir: Path) -> list[dict]:
    """Before = the text extraction and split alone; after = the same plus scan detection and OCR."""
    rows = []
    for path in sorted(pdf_dir.glob("*.pdf")):
        file_hash = ingest.calculate_file_hash(path)
        before_chunks, before = timed(lambda: ingest.split_documents(ingest.clean_documents(ingest.load_single_file(path))))
        (_docs, after_cold), cold = timed(lambda: ingest.load_document_chunks(path, OWNER, file_hash), repeat=1)
        (_docs, after_chunks), cached = timed(lambda: ingest.load_document_chunks(path, OWNER, file_hash))
        ocr_pages = sorted({c.metadata["page"] for c in after_chunks if c.metadata.get("extraction") == "ocr"})
        analyses, analyze = timed(lambda: pdf_visuals.analyze_pdf(path))
        regions = pdf_visuals.figure_regions(analyses)
        rows.append({
            "file": path.name, "pages": len(analyses), "before_s": before, "after_first_s": cold, "after_cached_s": cached,
            "ocr_pages": ocr_pages, "unchanged_chunks": digest(before_chunks) == digest(after_cold),
            "chunks_before": len(before_chunks), "chunks_after": len(after_cold),
            "background_analyze_s": analyze, "figure_pages": sorted(regions),
        })
        print(json.dumps(rows[-1]), flush=True)
    return rows


def bench_scan(text_pdf: Path, workdir: Path) -> dict:
    """Rasterize a text PDF into a scan: its own text layer is the reference for OCR accuracy."""
    pages = [pdf_visuals.render_region(text_pdf, page, 150) for page in range(pdf_visuals.page_count(text_pdf))]
    scan = workdir / "synthetic-scan.pdf"
    pages[0].save(scan, format="PDF", save_all=True, append_images=pages[1:], resolution=150)
    reference = ingest.clean_documents(ingest.load_single_file(text_pdf))
    if not pdf_ocr.available():
        return {"error": "OCR engine unavailable"}
    pypdf_only, pypdf_s = timed(lambda: ingest.clean_documents(ingest.load_single_file(scan)), repeat=1)
    file_hash = ingest.calculate_file_hash(scan)
    (documents, chunks), ocr_s = timed(lambda: ingest.load_document_chunks(scan, OWNER, file_hash), repeat=1)
    _, cached_s = timed(lambda: ingest.load_document_chunks(scan, OWNER, file_hash), repeat=1)

    def normalized(text):
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def words(text):
        return re.findall(r"[a-z0-9]+", text.lower())

    truth = " ".join(doc.page_content for doc in reference)
    read = " ".join(doc.page_content for doc in documents)
    found = set(words(read))
    return {
        "pages": len(pages), "text_pages_without_ocr": len(pypdf_only), "text_pages_with_ocr": len(documents),
        "chunks": len(chunks), "parse_without_ocr_s": pypdf_s, "parse_with_ocr_s": ocr_s, "parse_cached_s": cached_s,
        "seconds_per_page": round(ocr_s / max(1, len(pages)), 2),
        "char_similarity": round(difflib.SequenceMatcher(None, normalized(truth), normalized(read), autojunk=False).ratio(), 3),
        "word_recall": round(sum(word in found for word in words(truth)) / max(1, len(words(truth))), 3),
        "pages_cited": sorted(doc.metadata["page"] for doc in documents)[:3],
    }


def bench_end_to_end(path: Path, query: str, workdir: Path) -> dict:
    uploads = workdir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    copy = uploads / path.name
    shutil.copy(path, copy)
    # Heading refinement needs the chat model and has nothing to do with this feature.
    ingest.ollama_heading_refiner = lambda _model: (lambda _payload: [])

    started = time.perf_counter()
    ingest.index_files([copy], OWNER)
    upload_s = round(time.perf_counter() - started, 2)

    started = time.perf_counter()
    first = visual_enrichment.process_document(OWNER, path.name)
    enrich_s = round(time.perf_counter() - started, 2)

    started = time.perf_counter()
    repeat = visual_enrichment.process_document(OWNER, path.name)
    repeat_s = round(time.perf_counter() - started, 2)

    store = ingest.get_vectorstore()
    visual = store.get(where={"$and": [{"owner_id": OWNER}, {"document_id": path.name}]}, include=["documents", "metadatas"])
    samples = [{"page": meta.get("page"), "type": meta.get("content_type"), "text": text[:400]}
               for meta, text in zip(visual["metadatas"], visual["documents"]) if meta.get("content_type")]
    docs = rag_service._retrieve_conversation_docs(OWNER, query, [path.name])
    return {
        "upload_index_s": upload_s, "background_s": enrich_s, "first_run": first, "repeat_run_s": repeat_s,
        "vision_calls": first["vision_calls"] + repeat["vision_calls"],
        "visual_chunks": samples,
        "retrieval": [{"page": doc.metadata.get("page"), "type": doc.metadata.get("content_type", "text"),
                       "citation_page": rag_service._build_citations([doc])[0]["page"], "text": doc.page_content[:120]} for doc in docs],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-dir", type=Path, default=Path("data"))
    parser.add_argument("--scan-pdf", type=Path)
    parser.add_argument("--figures-pdf", type=Path)
    parser.add_argument("--query", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"parse": bench_parse(args.pdf_dir)}
    if args.scan_pdf:
        report["scan"] = bench_scan(args.scan_pdf, ROOT)
        print(json.dumps(report["scan"]), flush=True)
    if args.figures_pdf:
        report["end_to_end"] = bench_end_to_end(args.figures_pdf, args.query, ROOT)
    text = json.dumps(report, indent=1, ensure_ascii=False)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
