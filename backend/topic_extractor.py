import json
import re
from dataclasses import dataclass
from typing import Callable


TOPIC_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class HeadingCandidate:
    name: str
    normalized: str
    page_index: int
    char_start: int
    level: int


def normalize_heading(value: str) -> str:
    """Normalize headings for comparison without damaging their display text."""
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(r"^[\s#>*_-]+|[\s#>*_-]+$", "", value)
    value = re.sub(
        r"^(?:chapter|section|part|unit)\s+[\divxlcdm]+\s*[:.\-–—]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _heading_level(line: str) -> int:
    numbered = re.match(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+", line)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 3)
    if re.match(r"^\s*(?:chapter|part|unit)\b", line, re.IGNORECASE):
        return 1
    if re.match(r"^\s*section\b", line, re.IGNORECASE):
        return 2
    return 1


def _looks_like_heading(line: str) -> bool:
    text = re.sub(r"\s+", " ", line).strip()
    words = text.split()
    if not text or len(text) > 120 or len(words) > 14:
        return False
    if re.fullmatch(r"(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?", text, re.IGNORECASE):
        return False
    if re.match(
        r"^(?:chapter|section|part|unit)\s+[\divxlcdm]+(?:\b|\s*[:.\-–—])",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-ZÀ-Þ]", text):
        return True
    letters = [character for character in text if character.isalpha()]
    if len(words) >= 2 and letters and text.upper() == text and len(letters) >= 4:
        return True
    title_words = sum(word[:1].isupper() for word in words if word[:1].isalpha())
    return 2 <= len(words) <= 10 and title_words >= max(2, len(words) - 1) and not text.endswith((".", ";", ","))


class TopicExtractor:
    """Extract main topics from page text, with an optional bounded LLM refiner."""

    def __init__(self, llm_refiner: Callable[[dict], list[dict]] | None = None):
        self.llm_refiner = llm_refiner

    def detect_headings(self, documents) -> list[HeadingCandidate]:
        candidates = []
        page_occurrences: dict[str, set[int]] = {}
        for page_index, document in enumerate(documents):
            content = document.page_content or ""
            offset = 0
            for raw_line in content.splitlines(keepends=True):
                line = raw_line.strip()
                normalized = normalize_heading(line)
                if normalized and _looks_like_heading(line):
                    candidate = HeadingCandidate(
                        name=re.sub(r"\s+", " ", line).strip(),
                        normalized=normalized,
                        page_index=page_index,
                        char_start=offset + max(raw_line.find(line), 0),
                        level=_heading_level(line),
                    )
                    candidates.append(candidate)
                    page_occurrences.setdefault(normalized, set()).add(page_index)
                offset += len(raw_line)

        repeated_headers = {
            normalized
            for normalized, pages in page_occurrences.items()
            if len(pages) >= 2
            and all(
                candidate.char_start <= 160
                for candidate in candidates
                if candidate.normalized == normalized
            )
        }
        # Exclude repeated top-of-page running headers. Collapse other duplicate
        # headings to their first structural occurrence.
        seen = set()
        deduplicated = []
        for candidate in candidates:
            if candidate.normalized in repeated_headers or candidate.normalized in seen:
                continue
            seen.add(candidate.normalized)
            deduplicated.append(candidate)
        return deduplicated

    def extract(self, documents) -> tuple[list[dict], list[HeadingCandidate]]:
        headings = self.detect_headings(documents)
        main_headings = [heading for heading in headings if heading.level == 1]
        selected = main_headings or headings

        if self.llm_refiner and self._needs_refinement(selected, len(documents)):
            payload = {
                "candidates": [
                    {
                        "name": heading.name,
                        "page": heading.page_index + 1,
                        "level": heading.level,
                    }
                    for heading in headings[:100]
                ],
                "page_excerpts": [
                    {
                        "page": page_index + 1,
                        "text": re.sub(r"\s+", " ", document.page_content or "")[:500],
                    }
                    for page_index, document in enumerate(documents[:30])
                ],
            }
            try:
                refined = self.llm_refiner(payload)
                selected = self._apply_refinement(refined, headings, len(documents)) or selected
            except Exception as error:
                print(f"Topic LLM refinement failed; using deterministic topics: {error}")

        if not selected:
            topic = {
                "topic_id": "topic_001",
                "name": "Document Overview",
                "subtopics": [],
                "start_page": 1,
                "end_page": max(1, len(documents)),
            }
            return [topic], []

        selected = sorted(selected, key=lambda item: (item.page_index, item.char_start))
        topics = []
        for index, heading in enumerate(selected):
            next_heading = selected[index + 1] if index + 1 < len(selected) else None
            end_page = next_heading.page_index + 1 if next_heading else max(1, len(documents))
            topics.append(
                {
                    "topic_id": f"topic_{index + 1:03d}",
                    "name": heading.name,
                    "subtopics": [],
                    "start_page": heading.page_index + 1,
                    "end_page": end_page,
                }
            )
        return topics, selected

    @staticmethod
    def _needs_refinement(headings: list[HeadingCandidate], page_count: int) -> bool:
        return not headings or (page_count >= 4 and len(headings) < 2)

    @staticmethod
    def _apply_refinement(
        refined: list[dict], headings: list[HeadingCandidate], page_count: int
    ) -> list[HeadingCandidate]:
        by_name = {heading.normalized: heading for heading in headings}
        result = []
        seen = set()
        for item in refined or []:
            normalized = normalize_heading(item.get("name", ""))
            source = by_name.get(normalized)
            if source is None and normalized:
                page_index = max(0, min(int(item.get("page", 1)) - 1, max(0, page_count - 1)))
                source = HeadingCandidate(
                    name=re.sub(r"\s+", " ", str(item.get("name", ""))).strip(),
                    normalized=normalized,
                    page_index=page_index,
                    char_start=0,
                    level=1,
                )
            if source and normalized not in seen:
                result.append(source)
                seen.add(normalized)
        return result

    @staticmethod
    def map_chunks(chunks, documents, topics: list[dict], headings: list[HeadingCandidate]) -> None:
        """Annotate chunks by largest section overlap, with page ranges as fallback."""
        topic_by_heading = {
            heading.normalized: topic
            for heading, topic in zip(headings, topics)
        }
        headings_by_page: dict[int, list[tuple[HeadingCandidate, dict]]] = {}
        for heading in sorted(headings, key=lambda item: (item.page_index, item.char_start)):
            topic = topic_by_heading.get(heading.normalized)
            if topic:
                headings_by_page.setdefault(heading.page_index, []).append((heading, topic))

        page_sections: dict[int, list[tuple[int, int, dict]]] = {}
        active_topic = topics[0]
        for page_index, document in enumerate(documents):
            page_length = len(document.page_content or "")
            cursor = 0
            sections = []
            for heading, topic in headings_by_page.get(page_index, []):
                boundary = max(0, min(heading.char_start, page_length))
                if boundary > cursor:
                    sections.append((cursor, boundary, active_topic))
                active_topic = topic
                cursor = boundary
            if cursor < page_length or not sections:
                sections.append((cursor, page_length, active_topic))
            page_sections[page_index] = sections

        for chunk in chunks:
            metadata = chunk.metadata
            page_index = int(metadata.get("page", 0) or 0)
            char_start = metadata.get("start_index")
            selected_topic = None
            if isinstance(char_start, int) and char_start >= 0:
                char_end = char_start + len(chunk.page_content)
                overlaps = [
                    (max(0, min(char_end, end) - max(char_start, start)), topic)
                    for start, end, topic in page_sections.get(page_index, [])
                ]
                if overlaps:
                    selected_topic = max(overlaps, key=lambda item: item[0])[1]
            if selected_topic is None:
                page_number = page_index + 1
                selected_topic = next(
                    (
                        topic
                        for topic in topics
                        if topic["start_page"] <= page_number <= topic["end_page"]
                    ),
                    topics[0],
                )
            metadata["topic_id"] = selected_topic["topic_id"]
            metadata["topic_name"] = selected_topic["name"]


def ollama_heading_refiner(model: str):
    """Create a refiner that sends heading candidates—not PDF contents—to Ollama."""
    from langchain_ollama import ChatOllama
    from config import OLLAMA_KEEP_ALIVE

    def refine(payload: dict) -> list[dict]:
        prompt = (
            "Select and merge the major document topics using the detected headings and short page excerpts. "
            "Prefer existing headings. If none are usable, infer concise major topic names from the excerpts. "
            "Return JSON only as an array of objects with name and 1-based page fields.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response = ChatOllama(model=model, temperature=0, num_predict=800, keep_alive=OLLAMA_KEEP_ALIVE).invoke(prompt)
        text = str(response.content).strip()
        match = re.search(r"\[[\s\S]*\]", text)
        parsed = json.loads(match.group(0) if match else text)
        return parsed if isinstance(parsed, list) else []

    return refine
