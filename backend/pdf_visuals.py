"""Find the pages of a PDF that need OCR or Vision AI, and render them (pypdfium2 + Pillow).

Pure analysis: no database, no model calls. Everything here is cheap compared to a model call, and
its job is to make sure as few pages as possible reach one: decorations, logos and slide templates
are dropped from the page objects alone, before anything is rendered.

PDFium is not thread-safe, and uploads and the background enrichment worker can both be reading a
PDF at the same moment, so every use of it goes through _PDFIUM_LOCK.
"""
import hashlib
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_PDFIUM_LOCK = threading.RLock()

# A meaningful embedded image: not a bullet, an icon or a thin decorative rule.
MIN_IMAGE_PIXELS = 100           # on each side of the embedded bitmap
MIN_IMAGE_COVERAGE = 0.02        # of the page area
MAX_IMAGE_ASPECT = 8.0           # wider or taller than this is a banner or a divider
# An image or vector shape on this many pages (and this share of them) belongs to the template.
REPEAT_MIN_PAGES = 3
REPEAT_MIN_SHARE = 0.3
# Vector drawings: a real diagram has many shapes spread over a good part of the page. Lines of a
# simple table or underlines rarely reach both.
MIN_DIAGRAM_PATHS = 30
MIN_DIAGRAM_COVERAGE = 0.10
# Whether a page with almost no text layer is a scan.
SCAN_MIN_IMAGE_COVERAGE = 0.5
OUTLINED_TEXT_MIN_PATHS = 400    # text exported as curves has no text layer either
# Crop to the figure unless it fills most of the page anyway.
WHOLE_PAGE_COVERAGE = 0.6
CROP_MARGIN = 0.03               # of the page's longer side, so labels next to a figure survive
BLANK_STDDEV = 6.0               # grayscale standard deviation of an empty or single-colour render
DHASH_SIZE = 16
NEAR_DUPLICATE_BITS = 6          # of 256: slide-animation steps of one figure


Box = tuple[float, float, float, float]   # left, bottom, right, top in PDF points


@dataclass
class PageAnalysis:
    page: int                                       # 0-based, the same as Document.metadata["page"]
    width: float
    height: float
    rotation: int = 0
    origin: tuple[float, float] = (0.0, 0.0)        # the page box's left/bottom corner
    images: list[tuple[Box, str, int, int]] = field(default_factory=list)   # box, digest, px width, px height
    paths: list[Box] = field(default_factory=list)


def _coverage(boxes: list[Box], width: float, height: float) -> float:
    """Share of the page taken by the bounding box of `boxes`."""
    if not boxes or width <= 0 or height <= 0:
        return 0.0
    union = _union(boxes)
    return min(1.0, max(0.0, (union[2] - union[0]) * (union[3] - union[1]) / (width * height)))


def _union(boxes: list[Box]) -> Box:
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def page_count(file_path: Path) -> int:
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            return len(document)
        finally:
            document.close()


def analyze_pdf(file_path: Path, pages: list[int] | None = None) -> list[PageAnalysis]:
    """Read the image and vector objects of `pages` (every page by default), without rendering."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    analyses = []
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            for index in (range(len(document)) if pages is None else pages):
                if not 0 <= index < len(document):
                    continue
                page = document[index]
                try:
                    width, height = page.get_size()
                    left, bottom, _right, _top = page.get_bbox()
                    analysis = PageAnalysis(index, width, height, page.get_rotation(), (left, bottom))
                    for item in page.get_objects(max_depth=3):
                        try:
                            if item.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
                                metadata = item.get_metadata()
                                digest = hashlib.blake2b(bytes(item.get_data(decode_simple=False)), digest_size=12).hexdigest()
                                analysis.images.append((tuple(item.get_bounds()), digest, metadata.width, metadata.height))
                            elif item.type == pdfium_raw.FPDF_PAGEOBJ_PATH:
                                analysis.paths.append(tuple(item.get_bounds()))
                        except Exception:   # one malformed object does not hide the rest of the page
                            continue
                    analyses.append(analysis)
                finally:
                    page.close()
        finally:
            document.close()
    return analyses


def _repeated(counts: Counter, total_pages: int) -> set:
    threshold = max(REPEAT_MIN_PAGES, REPEAT_MIN_SHARE * total_pages)
    return {key for key, pages in counts.items() if pages >= threshold}


def _repeated_images(analyses: list[PageAnalysis]) -> set[str]:
    return _repeated(Counter(digest for a in analyses for digest in {i[1] for i in a.images}), len(analyses))


def is_scanned_page(analysis: PageAnalysis, repeated_images: set[str]) -> bool:
    """For a page with almost no text layer: is the visible content really a picture of a page?"""
    images = [box for box, digest, _w, _h in analysis.images if digest not in repeated_images]
    return (_coverage(images, analysis.width, analysis.height) >= SCAN_MIN_IMAGE_COVERAGE
            or len(analysis.paths) >= OUTLINED_TEXT_MIN_PATHS)


def scanned_pages(analyses: list[PageAnalysis]) -> list[int]:
    repeated = _repeated_images(analyses)
    return [analysis.page for analysis in analyses if is_scanned_page(analysis, repeated)]


def _meaningful_image(box: Box, pixel_width: int, pixel_height: int, analysis: PageAnalysis) -> bool:
    width, height = box[2] - box[0], box[3] - box[1]
    if pixel_width < MIN_IMAGE_PIXELS or pixel_height < MIN_IMAGE_PIXELS or width <= 0 or height <= 0:
        return False
    if max(width / height, height / width) > MAX_IMAGE_ASPECT:
        return False
    return width * height / (analysis.width * analysis.height) >= MIN_IMAGE_COVERAGE


def figure_regions(analyses: list[PageAnalysis]) -> dict[int, Box | None]:
    """Pages holding a meaningful figure -> the region to look at (None = the whole page).

    Pass every page of the document: whether something is template decoration can only be told by
    how often it repeats."""
    repeated_images = _repeated_images(analyses)
    path_counts = Counter(key for a in analyses for key in {_path_key(box) for box in a.paths})
    repeated_paths = _repeated(path_counts, len(analyses))
    regions: dict[int, Box | None] = {}
    for analysis in analyses:
        if analysis.width <= 0 or analysis.height <= 0:
            continue
        boxes = [box for box, digest, w, h in analysis.images
                 if digest not in repeated_images and _meaningful_image(box, w, h, analysis)]
        paths = [box for box in analysis.paths if _path_key(box) not in repeated_paths]
        if len(paths) >= MIN_DIAGRAM_PATHS and _coverage(paths, analysis.width, analysis.height) >= MIN_DIAGRAM_COVERAGE:
            boxes.extend(paths)
        if not boxes:
            continue
        rotated = analysis.rotation != 0   # object bounds are not in the rotated page's coordinates
        whole = rotated or _coverage(boxes, analysis.width, analysis.height) >= WHOLE_PAGE_COVERAGE
        regions[analysis.page] = None if whole else _union(boxes)
    return regions


def _path_key(box: Box) -> tuple:
    return tuple(round(value) for value in box)


def render_region(file_path: Path, analysis_page: int, dpi: int, region: Box | None = None, max_pixels: int = 5_000_000):
    """Render one page (cropped to `region`) to an RGB PIL image; huge pages are scaled down."""
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            page = document[analysis_page]
            try:
                width, height = page.get_size()
                left, bottom, _right, _top = page.get_bbox()
                scale = dpi / 72
                scale = min(scale, (max_pixels / max(1.0, width * height)) ** 0.5)
                image = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
        finally:
            document.close()
    if region is None:
        return image
    margin = CROP_MARGIN * max(width, height)
    crop = (
        max(0, (region[0] - left - margin) * scale),
        max(0, (height - (region[3] - bottom) - margin) * scale),
        min(image.width, (region[2] - left + margin) * scale),
        min(image.height, (height - (region[1] - bottom) + margin) * scale),
    )
    if crop[2] - crop[0] < 1 or crop[3] - crop[1] < 1:
        return image
    return image.crop(tuple(int(value) for value in crop))


def is_blank(image) -> bool:
    from PIL import ImageStat

    return ImageStat.Stat(image.convert("L")).stddev[0] < BLANK_STDDEV


def image_digest(image) -> str:
    return hashlib.sha256(image.tobytes() + repr(image.size).encode()).hexdigest()


def dhash(image, size: int = DHASH_SIZE) -> int:
    """Perceptual hash: which of two neighbouring pixels is brighter, on a tiny grayscale copy."""
    from PIL import Image

    pixels = image.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR).tobytes()
    bits = 0
    for row in range(size):
        for column in range(size):
            bits = (bits << 1) | (pixels[row * (size + 1) + column] > pixels[row * (size + 1) + column + 1])
    return bits


def is_near_duplicate(fingerprint: int, seen: list[int]) -> bool:
    return any((fingerprint ^ other).bit_count() <= NEAR_DUPLICATE_BITS for other in seen)
