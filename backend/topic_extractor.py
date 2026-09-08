import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

TOPIC_SCHEMA_VERSION = 5
_HIERARCHY_CACHE: dict[tuple[str, int], tuple[list[dict], list["HeadingCandidate"]]] = {}
_HIGH_CONFIDENCE = 0.85

@dataclass(frozen=True)
class HeadingCandidate:
    name: str
    normalized: str
    page_index: int
    char_start: int
    level: int
    family: str = "unnumbered"
    number_path: tuple[int, ...] = ()
    document_offset: int = 0
    confidence: float = 0.0

_LABEL_RANK = {"part": 0, "chapter": 1, "unit": 1, "module": 2, "section": 3}

def _display_heading(value: str) -> str:
    """Repair common extraction joins without document-specific vocabulary."""
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(
        r"^(part|chapter|unit|module|section)\s*([0-9ivxlcdm]+)\s*([:.\-]?)\s*",
        lambda m: f"{m.group(1).title()} {m.group(2)}{m.group(3)} ", value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"([a-z]{2,})([A-Z])(?=[a-z])", r"\1 \2", value)
    value = re.sub(r"([A-Z]{2})([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"(?i)([a-z]{5,})(of|and|the)(?=\s|[A-Z]|$)", r"\1 \2", value)
    return re.sub(r"\s+", " ", value).strip()

def normalize_heading(value: str) -> str:
    value = _display_heading(value)
    value = re.sub(r"^[\s#>*_-]+|[\s#>*_-]+$", "", value)
    value = re.sub(
        r"^(?:chapter|section|part|unit|module)\s+[\divxlcdm]+\s*[:.\-–—]?\s*",
        "", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().casefold()

def _structure(line: str) -> tuple[str, tuple[int, ...], int]:
    text = _display_heading(line)
    labeled = re.match(
        r"^\s*(part|chapter|unit|module|section)\s*([0-9]+|[ivxlcdm]+)(?:\b|\s*[:.\-])",
        text, re.IGNORECASE,
    )
    if labeled:
        family, raw = labeled.group(1).lower(), labeled.group(2)
        return family, ((int(raw),) if raw.isdigit() else ()), _LABEL_RANK[family] + 1
    numbered = re.match(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+", text)
    if numbered:
        path = tuple(int(part) for part in numbered.group(1).split("."))
        return "dotted", path, len(path)
    return "unnumbered", (), 1

def _heading_level(line: str) -> int:
    return min(_structure(line)[2], 4)

def _looks_like_heading(line: str) -> bool:
    text = _display_heading(line)
    words = text.split()
    if not text or len(text) > 120 or len(words) > 14:
        return False
    if re.fullmatch(r"(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?", text, re.IGNORECASE):
        return False
    if _structure(text)[0] != "unnumbered":
        return True
    if any(len(word) > 32 for word in words):
        return False
    letters = [character for character in text if character.isalpha()]
    if len(words) >= 2 and letters and text.upper() == text and len(letters) >= 4:
        return True
    title_words = sum(word[:1].isupper() for word in words if word[:1].isalpha())
    return 2 <= len(words) <= 10 and title_words >= max(2, len(words) - 1) and not text.endswith((".", ";", ","))

def _stable_id(kind: str, parent: str, candidate: HeadingCandidate, occurrence: int) -> str:
    identity = "|".join((parent, candidate.family, ".".join(map(str, candidate.number_path)), candidate.normalized, str(occurrence)))
    return f"{kind}_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12]}"

def _location(candidate: HeadingCandidate) -> dict:
    return {"page": candidate.page_index + 1, "page_index": candidate.page_index, "char_offset": candidate.char_start, "document_offset": candidate.document_offset}

class TopicExtractor:
    """Deterministic structural hierarchy extraction with bounded LLM fallback."""

    def __init__(self, llm_refiner: Callable[[dict], list[dict]] | None = None):
        self.llm_refiner = llm_refiner

    def detect_headings(self, documents) -> list[HeadingCandidate]:
        candidates, page_occurrences = [], {}
        document_base = 0
        for page_index, document in enumerate(documents):
            content, offset = document.page_content or "", 0
            for raw_line in content.splitlines(keepends=True):
                raw_name = raw_line.strip()
                name, normalized = _display_heading(raw_name), normalize_heading(raw_name)
                if normalized and _looks_like_heading(raw_name):
                    family, number_path, level = _structure(name)
                    local_start = offset + max(raw_line.find(raw_name), 0)
                    candidate = HeadingCandidate(name, normalized, page_index, local_start, level, family, number_path, document_base + local_start)
                    candidates.append(candidate)
                    page_occurrences.setdefault(normalized, set()).add(page_index)
                offset += len(raw_line)
            document_base += len(content) + 1
        repeated_headers = {
            normalized for normalized, pages in page_occurrences.items()
            if len(pages) >= 2 and all(
                c.char_start <= 160 or c.char_start >= max(0, len(documents[c.page_index].page_content or "") - 160)
                for c in candidates if c.normalized == normalized
            )
        }
        seen, result = set(), []
        for candidate in candidates:
            key = (candidate.normalized, candidate.page_index, candidate.char_start)
            if candidate.normalized in repeated_headers or key in seen:
                continue
            seen.add(key)
            result.append(candidate)
        return result

    @staticmethod
    def _select_major_candidates(headings: list[HeadingCandidate], document_length: int) -> tuple[list[HeadingCandidate], float]:
        structured = [heading for heading in headings if heading.family != "unnumbered"]
        if structured:
            labeled = [heading for heading in structured if heading.family != "dotted"]
            if labeled:
                top_rank = min(_LABEL_RANK[heading.family] for heading in labeled)
                selected = [heading for heading in labeled if _LABEL_RANK[heading.family] == top_rank]
                coherent = len(selected) >= 2 and len({heading.family for heading in selected}) == 1
                return selected, 0.97 if coherent else 0.78
            minimum_depth = min(len(heading.number_path) for heading in structured)
            selected = [heading for heading in structured if len(heading.number_path) == minimum_depth]
            roots = [heading.number_path[0] for heading in selected if heading.number_path]
            coherent = len(selected) >= 2 and roots == sorted(roots) and len(roots) == len(set(roots))
            return selected, 0.96 if coherent else 0.76
        if not headings:
            return [], 0.0
        ordered = sorted(headings, key=lambda item: item.document_offset)
        spans = [(ordered[i + 1].document_offset if i + 1 < len(ordered) else document_length) - h.document_offset for i, h in enumerate(ordered)]
        meaningful = [heading for heading, span in zip(ordered, spans) if span >= 120]
        return (meaningful or ordered), (0.68 if meaningful else 0.52)

    def extract(self, documents) -> tuple[list[dict], list[HeadingCandidate]]:
        document_hash = next((str(doc.metadata.get("file_hash") or "") for doc in documents if doc.metadata.get("file_hash")), "")
        cache_key = (document_hash, TOPIC_SCHEMA_VERSION) if document_hash else None
        if cache_key and cache_key in _HIERARCHY_CACHE:
            return deepcopy(_HIERARCHY_CACHE[cache_key])
        headings = sorted(self.detect_headings(documents), key=lambda item: item.document_offset)
        document_length = sum(len(document.page_content or "") + 1 for document in documents)
        selected, confidence = self._select_major_candidates(headings, document_length)
        llm_assignments = None
        if confidence < _HIGH_CONFIDENCE and self.llm_refiner and self._needs_refinement(headings, len(documents)):
            payload = {"candidates": [self._candidate_payload(index, h) for index, h in enumerate(headings[:100])]}
            try:
                llm_assignments = self._validate_refinement(self.llm_refiner(payload), headings[:100])
                selected = [heading for heading, role, _parent in llm_assignments if role == "major"]
                confidence = 0.74
            except Exception as error:
                print(f"Topic LLM refinement failed; using deterministic topics: {error}")
        if not selected:
            end = self._document_end(documents, document_length)
            result = ([{"topic_id": "topic_document_overview", "name": "Document Overview", "subtopics": [], "start_page": 1, "end_page": max(1, len(documents)), "boundary": {"start": {"page": 1, "page_index": 0, "char_offset": 0, "document_offset": 0}, "end": end, "interval": "half-open"}, "structure_confidence": 0.35}], headings)
            if cache_key:
                _HIERARCHY_CACHE[cache_key] = deepcopy(result)
            return result

        selected = sorted(selected, key=lambda item: item.document_offset)
        selected_offsets, topic_occurrences, topics = {h.document_offset for h in selected}, {}, []
        for index, heading in enumerate(selected):
            end_heading = selected[index + 1] if index + 1 < len(selected) else None
            end_offset = end_heading.document_offset if end_heading else document_length
            if llm_assignments is None:
                subordinate = [h for h in headings if heading.document_offset < h.document_offset < end_offset and h.document_offset not in selected_offsets]
            else:
                subordinate = [h for h, role, parent in llm_assignments if role == "subtopic" and parent is heading]
            occurrence = topic_occurrences.get(heading.normalized, 0) + 1
            topic_occurrences[heading.normalized] = occurrence
            topic_id = _stable_id("topic", "document", heading, occurrence)
            end_location = _location(end_heading) if end_heading else self._document_end(documents, document_length)
            subtopics, sub_occurrences = [], {}
            for sub_index, subheading in enumerate(subordinate):
                sub_end = subordinate[sub_index + 1] if sub_index + 1 < len(subordinate) else end_heading
                sub_occurrence = sub_occurrences.get(subheading.normalized, 0) + 1
                sub_occurrences[subheading.normalized] = sub_occurrence
                subtopics.append({"subtopic_id": _stable_id("subtopic", topic_id, subheading, sub_occurrence), "name": subheading.name, "start_page": subheading.page_index + 1, "end_page": (sub_end.page_index + 1) if sub_end else max(1, len(documents)), "boundary": {"start": _location(subheading), "end": _location(sub_end) if sub_end else end_location, "interval": "half-open"}, "structure_confidence": round(max(0.5, confidence - 0.08), 2)})
            topics.append({"topic_id": topic_id, "name": heading.name, "subtopics": subtopics, "start_page": heading.page_index + 1, "end_page": end_location["page"], "boundary": {"start": _location(heading), "end": end_location, "interval": "half-open"}, "structure_confidence": confidence})
        result = (topics, headings)
        if cache_key:
            if len(_HIERARCHY_CACHE) >= 128:
                _HIERARCHY_CACHE.pop(next(iter(_HIERARCHY_CACHE)))
            _HIERARCHY_CACHE[cache_key] = deepcopy(result)
        return result

    @staticmethod
    def _document_end(documents, document_length: int) -> dict:
        return {"page": max(1, len(documents)), "page_index": max(0, len(documents) - 1), "char_offset": len(documents[-1].page_content or "") if documents else 0, "document_offset": document_length}

    @staticmethod
    def _needs_refinement(headings: list[HeadingCandidate], page_count: int) -> bool:
        return bool(headings) and page_count >= 1

    @staticmethod
    def _candidate_payload(index: int, heading: HeadingCandidate) -> dict:
        return {"id": f"h{index}", "heading": heading.name, "page": heading.page_index + 1,
                "numbering_depth": len(heading.number_path), "structure": heading.family}

    @staticmethod
    def _validate_refinement(refined: list[dict], headings: list[HeadingCandidate]):
        if not isinstance(refined, list) or len(refined) != len(headings):
            raise ValueError("refiner must classify every candidate exactly once")
        assignments, seen, current_major, current_major_id = [], set(), None, None
        for index, item in enumerate(refined):
            expected_id = f"h{index}"
            if not isinstance(item, dict) or item.get("id") != expected_id or expected_id in seen:
                raise ValueError("candidate IDs must be unique and preserve document order")
            heading = headings[index]
            if item.get("heading") != heading.name:
                raise ValueError("headings may not be invented or renamed")
            role, parent_id, parent_heading = item.get("role"), item.get("parent_id"), item.get("parent_heading")
            if role == "major" and parent_id in (None, "") and parent_heading in (None, ""):
                current_major, current_major_id = heading, expected_id
            elif (role == "subtopic" and current_major is not None
                  and parent_id == current_major_id and parent_heading == current_major.name):
                pass
            else:
                raise ValueError("invalid role or parent hierarchy")
            assignments.append((heading, role, None if role == "major" else current_major))
            seen.add(expected_id)
        if not any(role == "major" for _heading, role, _parent in assignments):
            raise ValueError("at least one major topic is required")
        return assignments

    @staticmethod
    def map_chunks(chunks, documents, topics: list[dict], headings: list[HeadingCandidate]) -> None:
        """Annotate chunks by maximum overlap with half-open structural spans."""
        page_bases, base = [], 0
        for document in documents:
            page_bases.append(base)
            base += len(document.page_content or "") + 1
        topic_spans = []
        for topic in topics:
            boundary = topic["boundary"]
            subs = [(s["boundary"]["start"]["document_offset"], s["boundary"]["end"]["document_offset"], s) for s in topic.get("subtopics", [])]
            topic_spans.append((boundary["start"]["document_offset"], boundary["end"]["document_offset"], topic, subs))
        for chunk in chunks:
            metadata = chunk.metadata
            source_page = int(metadata.get("page", 0) or 0)
            page_index = next((i for i, doc in enumerate(documents) if int(doc.metadata.get("page", i) or 0) == source_page), min(source_page, max(0, len(documents) - 1)))
            char_start = metadata.get("start_index") if isinstance(metadata.get("start_index"), int) else 0
            start, end = page_bases[page_index] + max(0, char_start), page_bases[page_index] + max(0, char_start) + len(chunk.page_content)
            _, _, topic, subs = max(topic_spans, key=lambda span: max(0, min(end, span[1]) - max(start, span[0])))
            overlaps = [(max(0, min(end, e) - max(start, s)), sub) for s, e, sub in subs]
            best = max(overlaps, key=lambda item: item[0]) if overlaps else (0, None)
            subtopic = best[1] if best[0] > 0 else None
            metadata.update({"topic_id": topic["topic_id"], "topic_name": topic["name"], "subtopic_id": subtopic["subtopic_id"] if subtopic else "", "subtopic_name": subtopic["name"] if subtopic else "", "heading_path": json.dumps([topic["name"]] + ([subtopic["name"]] if subtopic else []), ensure_ascii=False), "structure_confidence": float(subtopic.get("structure_confidence") if subtopic else topic.get("structure_confidence", 0.0))})

def ollama_heading_refiner(model: str):
    """Create a bounded classifier that cannot alter detected candidates."""
    from langchain_ollama import ChatOllama
    def refine(payload: dict) -> list[dict]:
        prompt = (
            "Classify each ordered candidate as role major or subtopic. Copy id and heading exactly. "
            "For a major use parent_id and parent_heading null; for a subtopic copy the id and exact "
            "heading text of its preceding major parent. "
            "Do not omit, reorder, invent, or rename anything. Return JSON only as "
            '{"classifications":[{"id":"h0","heading":"exact text","role":"major|subtopic",'
            '"parent_id":null|"hN","parent_heading":null|"exact parent text"}]}.\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        response = ChatOllama(model=model, temperature=0, num_predict=500, format="json").invoke(prompt)
        text = str(response.content).strip()
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = parsed.get("classifications", [])
        return parsed if isinstance(parsed, list) else []
    return refine
