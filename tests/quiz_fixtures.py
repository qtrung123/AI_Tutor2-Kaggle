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


def flashcard(fact: int, kind: str = "term", **overrides) -> dict:
    """A persisted flashcard about fact `fact`, with the source chunk make_chunks(12, 2) puts it in.
    kind="term": front is the component, back its sentence; kind="question": a question front."""
    fact %= FACT_COUNT
    front = _NOUNS[fact].capitalize() if kind == "term" else f"What does the {_NOUNS[fact]} do during processing?"
    card = {
        "flashcard_id": f"fc-{fact}-{kind}", "set_id": "set-1", "topic_id": "t1", "topic_name": "Topic 1",
        "subtopic_id": None, "subtopic_name": None, "front": front, "back": fact_sentence(fact),
        "source_chunk_ids": [f"chunk_{fact // 2 + 1}"],
    }
    card.update(overrides)
    return card


def multi_candidate(first: int, second: int, **overrides) -> dict:
    """A valid multiple_select candidate: facts `first` and `second` are the two correct options
    (A and C), each quoted verbatim; B and D pair the same components with another fact's behaviour."""
    first %= FACT_COUNT
    second %= FACT_COUNT
    correct = [f"The {_NOUNS[first]} {_BEHAVIOURS[first]}", f"The {_NOUNS[second]} {_BEHAVIOURS[second]}"]
    wrong = [f"The {_NOUNS[first]} {_BEHAVIOURS[(first + 7) % FACT_COUNT]}",
             f"The {_NOUNS[second]} {_BEHAVIOURS[(second + 11) % FACT_COUNT]}"]
    candidate = {
        "evidence_quotes": [fact_sentence(first), fact_sentence(second)],
        "question": f"Which statements about the {_NOUNS[first]} and the {_NOUNS[second]} are correct?",
        "options": [correct[0], wrong[0], correct[1], wrong[1]],
        "correct_answers": list(correct),
        "explanation": f"The document states that the {_NOUNS[first]} {_BEHAVIOURS[first]} and the "
                       f"{_NOUNS[second]} {_BEHAVIOURS[second]}.",
    }
    candidate.update(overrides)
    return candidate


# Facts no single-choice fixture pool built from candidates(range(15)) tests, paired per chunk of
# make_chunks(12, 2): valid multiple_select payloads for the live pipeline's multiple_select call.
MULTI_FACT_PAIRS = ((16, 17), (18, 19), (22, 23))


def multi_payload(pairs=MULTI_FACT_PAIRS) -> dict:
    return {"questions": [multi_candidate(first, second) for first, second in pairs]}


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
        yield SimpleNamespace(content="", response_metadata={
            "eval_count": 123, "prompt_eval_count": 456, "done_reason": "stop", "eval_duration": 2_000_000_000,
        })

    @classmethod
    def reset(cls, payloads=None):
        cls.payloads = list(payloads or [])
        cls.prompts = []
        cls.kwargs = []
