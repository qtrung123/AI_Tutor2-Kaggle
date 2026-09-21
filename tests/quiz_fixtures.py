"""Shared fixtures for the live Quiz ("Study Units") tests.

Twenty-four genuinely distinct facts, each with its own vocabulary, so a pool of candidates can be
built without tripping the duplicate checks on questions that only differ by a case number.
Every candidate quotes the sentence of its own fact verbatim, exactly as the real prompt asks.
"""

import json
from types import SimpleNamespace

_NOUNS = [
    "mutex", "scheduler", "paging", "socket", "router", "compiler", "firmware", "bootloader",
    "interrupt", "pipeline", "cache", "kernel", "thread", "semaphore", "allocator", "checksum",
    "gateway", "handshake", "daemon", "register", "encoder", "hypervisor", "watchdog", "bitmap",
]
_BEHAVIOURS = [
    "reserves resources", "orders processes", "maps addresses", "connects endpoints",
    "forwards packets", "translates sources", "stores instructions", "loads images",
    "signals events", "overlaps stages", "keeps copies", "controls hardware",
    "runs tasks", "guards counters", "assigns blocks", "verifies bytes",
    "bridges networks", "confirms links", "serves requests", "holds values",
    "encodes symbols", "isolates machines", "resets stalls", "tracks frames",
]
FACT_COUNT = len(_NOUNS)


def fact_sentence(index: int) -> str:
    return f"The {_NOUNS[index]} {_BEHAVIOURS[index]} during processing."


def make_chunks(count: int, facts_per_chunk: int = 2, prefix: str = "chunk", pad: bool = True) -> list[dict]:
    """`count` ordered chunks; chunk j carries facts j*facts_per_chunk ... (wrapping).

    With `pad` every chunk gets a distinct neutral background paragraph so it is long enough to be
    its own study unit (short chunks are merged into their neighbours by design).
    """
    chunks = []
    for chunk_index in range(count):
        sentences = [
            fact_sentence((chunk_index * facts_per_chunk + offset) % FACT_COUNT)
            for offset in range(facts_per_chunk)
        ]
        text = " ".join(sentences)
        if pad:
            text += f" Background note {chunk_index + 1}: " + "additional surrounding context " * 15
        chunks.append({
            "content": text,
            "metadata": {"chunk_id": f"{prefix}_{chunk_index + 1}", "document_id": "lecture.pdf"},
        })
    return chunks


def spread_facts(count: int) -> list[int]:
    """Facts spread over the whole of make_chunks(12, 2): one per chunk first, then second facts."""
    order = list(range(0, FACT_COUNT, 2)) + list(range(1, FACT_COUNT, 2))
    return order[:count]


def raw_candidate(fact: int, answer_index: int = 0, **overrides) -> dict:
    """A valid model candidate testing `fact` (0..23)."""
    fact %= FACT_COUNT
    correct = _BEHAVIOURS[fact]
    distractors = [_BEHAVIOURS[(fact + step) % FACT_COUNT] for step in (5, 9, 13)]
    options = list(distractors)
    options.insert(answer_index, correct)
    candidate = {
        "question": f"What does the {_NOUNS[fact]} do?",
        "options": options,
        "answer_index": answer_index,
        "evidence_quote": fact_sentence(fact),
        "explanation": f"The document states that the {_NOUNS[fact]} {correct}.",
    }
    candidate.update(overrides)
    return candidate


def candidates(facts) -> list[dict]:
    return [raw_candidate(fact, answer_index=fact % 4) for fact in facts]


class FakeModel:
    """Stands in for ChatOllama: returns queued payloads and records every prompt/kwargs."""

    payloads: list = []
    prompts: list = []
    kwargs: list = []
    pieces: int = 4

    def __init__(self, **kwargs):
        self.__class__.kwargs.append(kwargs)

    def invoke(self, prompt):
        self.__class__.prompts.append(prompt)
        payload = self.__class__.payloads.pop(0)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(content=content, response_metadata={})

    def stream(self, prompt):
        """Yield the answer in pieces, like ChatOllama.stream (the last piece carries the metadata)."""
        content = self.invoke(prompt).content
        size = max(1, len(content) // self.__class__.pieces)
        for start in range(0, len(content), size):
            yield SimpleNamespace(content=content[start:start + size], response_metadata={})
        yield SimpleNamespace(content="", response_metadata={"eval_count": 123, "prompt_eval_count": 456})

    @classmethod
    def reset(cls, payloads=None):
        cls.payloads = list(payloads or [])
        cls.prompts = []
        cls.kwargs = []
