"""Deterministic building blocks of the live Quiz pipeline.

Flow (see backend.quiz_service._generate_quiz_from_units for the orchestration):

    document chunks -> excerpts ("units") -> context that fits a fixed budget
    -> call 1 writes a surplus of candidates -> validate -> add the valid ones to the pool
    -> while the pool is short of `requested_count` (and calls remain): another, smaller call
       that writes only what is missing and is told what already exists -> best N -> quiz

The requested question count is a TARGET, never a condition of success: the quiz contains
min(valid candidates, requested_count) questions and is "partial" when it is short.

Nothing here needs an LLM, so every rule can be tested without a model, and nothing depends on the
language, subject or layout of a document:

* build_study_units     -- clean excerpts of the whole document (headers/overlap removed, nothing truncated)
* select_context_units  -- which excerpts go into a call (all of them if they fit, else evenly spread)
* build_generation_prompt / QUIZ_OUTPUT_SCHEMA -- one prompt and one schema that agree with the validator
* parse_candidates      -- tolerant JSON parsing (salvages complete questions from truncated output)
* validate_candidate    -- 4 options / 1 answer + grounding in the provided context + duplicates
* select_questions      -- best-ranked candidates, returned in document order

The model never owns provenance: the excerpt (and so source_chunk_ids) of a question is derived by
the backend from where its evidence_quote is really found in the context the model was given.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
import unicodedata
from collections import Counter

QUIZ_ENGINE_VERSION = "simple_context_v7"
QUIZ_PROMPT_VERSION = "simple_context_v7_single_choice"

# --- how many questions / candidates ------------------------------------------------------------
QUIZ_ALLOWED_QUESTION_COUNTS = (12, 15, 18, 20)
QUIZ_CANDIDATE_BUFFER_RATIO = 0.2     # 12 -> 15, 15 -> 18, 18 -> 22, 20 -> 24
# Models tend to write far fewer questions than asked in one big list, so after the first call
# the pipeline keeps asking, in moderate batches, until the target is met or the calls run out.
QUIZ_MAX_CALLS = {12: 4, 15: 4, 18: 5, 20: 5}
QUIZ_DEFAULT_MAX_CALLS = 4
QUIZ_FOLLOWUP_MIN_BUFFER = 2          # a follow-up asks for the missing questions + a small surplus
QUIZ_FOLLOWUP_BUFFER_RATIO = 0.25
QUIZ_FOLLOWUP_MIN_ASK = 3
QUIZ_FOLLOWUP_MAX_ASK = 8             # ... but never a big batch: that is what models under-deliver
QUIZ_STALL_LIMIT = 2                  # stop after this many calls in a row that add no valid question
QUIZ_MIN_ITEMS_RATIO = 0.7            # the schema makes the model write at least this share of the ask
QUIZ_MAX_ITEMS_EXTRA = 3              # ... and at most `ask` + this many
QUIZ_CHARS_PER_MIN_ITEM = 500         # ... but never more than the shown text can carry: 1 question per 500 chars
QUIZ_AVOID_QUOTE_CHARS = 110          # evidence shown to a follow-up call for every existing question

# --- excerpts and context -----------------------------------------------------------------------
QUIZ_UNIT_MAX_CHARS = 1000            # an excerpt is never truncated; larger chunks are split instead
QUIZ_UNIT_MIN_CHARS = 500             # tiny neighbouring chunks are merged up to this size
QUIZ_CONTEXT_CHARS_PER_CANDIDATE = 550
QUIZ_CONTEXT_MIN_CHARS = 7000
QUIZ_CONTEXT_MAX_CHARS = 12000        # evidence budget of ONE call: bounds prompt tokens for any document
QUIZ_REPEATED_LINE_MAX_CHARS = 120    # page headers/footers are short lines repeated across pages
QUIZ_REPEATED_LINE_MIN_CHUNKS = 3
QUIZ_OVERLAP_MIN_CHARS = 30
QUIZ_OVERLAP_MAX_CHARS = 300

# --- model call ---------------------------------------------------------------------------------
# One fixed window for every call so Ollama never reloads the model between them. Worst case
# (evidence at one token per 1.5 characters: glued, bilingual text) is ~8k prompt + ~1k
# instructions + <=6.4k output, so 16384 keeps headroom up to 24 candidates.
QUIZ_NUM_CTX = 16384
QUIZ_TOKENS_PER_QUESTION = 260        # quote + question + 4 options + explanation
QUIZ_MAX_NEW_TOKENS = 6400
# The HTTP request that triggers generation is cut by the reverse proxy after 600 s
# (deployment/nginx.kaggle.conf), and the HTTP client's own timeout only measures silence between
# tokens, so a slow GPU could stream for far longer. The whole pipeline therefore has a wall-clock
# budget: every call stops streaming at its deadline and keeps every complete question written so
# far, and a call only starts if QUIZ_MIN_CALL_S is left of QUIZ_TOTAL_DEADLINE_S. When a run is
# slow, the log line of each call (tokens/s, cut, done_reason) shows it.
QUIZ_LLM_TIMEOUT_S = 300              # silence limit (e.g. a stalled model), not a total limit
QUIZ_FIRST_CALL_DEADLINE_S = 300
QUIZ_FOLLOWUP_DEADLINE_S = 150
QUIZ_TOTAL_DEADLINE_S = 570
QUIZ_MIN_CALL_S = 45

# --- grounding ----------------------------------------------------------------------------------
QUIZ_MIN_QUOTE_CHARS = 20             # letters/digits only; shorter quotes prove nothing
QUIZ_QUOTE_MIN_ALIGNMENT = 0.85       # share of the quote that must align, in order, with one excerpt
QUIZ_ALIGN_BLOCK_MIN = 5              # matching runs shorter than this are noise
QUIZ_ALIGN_BLOCKS_MAX = 5             # at most this many separate matching runs (a few small edits)
QUIZ_ALIGN_SPAN_SLACK = 20            # aligned region may exceed the quote length by this much (+30%)
QUIZ_ALIGN_PREFILTER = 0.25
QUIZ_ALIGN_CANDIDATE_UNITS = 3
QUIZ_SHINGLE_SIZE = 8
QUIZ_LINK_TOKEN_MIN_CHARS = 5         # words shorter than this match glued text by accident
QUIZ_MIN_LINK_SUPPORT = 0.25          # question + answer vs the excerpt the quote sits in
QUIZ_WEAK_LINK_SUPPORT = 0.5
QUIZ_MIN_ANSWER_ANCHOR = 0.5          # share of the answer's distinctive words found in the context
QUIZ_STRONG_ANCHOR_CHARS = 8          # ... or one word this long inside the quoted excerpt
QUIZ_COMMON_TOKEN_SHARE = 0.4         # a word present in more than this share of excerpts says nothing
QUIZ_COMMON_MIN_UNITS = 5             # ... but only judged when the context has at least this many

# --- duplicates / structure ---------------------------------------------------------------------
QUIZ_CONTENT_DUPLICATE_JACCARD = 0.6
QUIZ_QUOTE_DUPLICATE_JACCARD = 0.6
QUIZ_STEM_DUPLICATE_RATIO = 0.9
QUIZ_OPTION_DUPLICATE_RATIO = 0.94
QUIZ_OPTION_BAG_JACCARD = 0.8
QUIZ_MIN_STEM_CHARS = 12
QUIZ_MAX_STEM_CHARS = 320
QUIZ_MAX_OPTION_CHARS = 200

OPTION_LETTERS = "ABCD"

# The single schema sent to the model. It matches validate_candidate exactly: four options, one
# 0-based answer index, no question_type (single_choice is the only type), and a mandatory
# evidence_quote declared BEFORE the question, so the question is written from a sentence the
# model has already chosen rather than justified afterwards.
QUIZ_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "evidence_quote": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                    "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
                    "explanation": {"type": "string"},
                },
                "required": ["evidence_quote", "question", "options", "answer_index", "explanation"],
            },
        }
    },
    "required": ["questions"],
}


class CandidateRejected(ValueError):
    """A candidate failed a hard validation rule. `category` feeds the diagnostics counters."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category  # "structure" | "grounding" | "duplicate"


def candidate_target(question_count: int) -> int:
    """How many candidates the first call writes: the target plus a 20% surplus."""
    return question_count + math.ceil(QUIZ_CANDIDATE_BUFFER_RATIO * question_count)


def max_llm_calls(question_count: int) -> int:
    """Upper bound on model calls for one quiz: 12 -> 4, 15 -> 4, 18 -> 5, 20 -> 5."""
    return QUIZ_MAX_CALLS.get(question_count, QUIZ_DEFAULT_MAX_CALLS)


def followup_request(missing: int) -> int:
    """How many candidates a follow-up call writes: what is missing plus a small surplus, but a
    moderate batch (3-8) -- a model asked for a long list stops early, a short one it completes."""
    wanted = missing + max(QUIZ_FOLLOWUP_MIN_BUFFER, math.ceil(QUIZ_FOLLOWUP_BUFFER_RATIO * missing))
    return max(QUIZ_FOLLOWUP_MIN_ASK, min(QUIZ_FOLLOWUP_MAX_ASK, wanted))


def min_items_for(ask: int, material_chars: int | None = None) -> int:
    """The fewest questions the decoder may write: QUIZ_MIN_ITEMS_RATIO of `ask`, capped by how much
    text the call shows (one question per QUIZ_CHARS_PER_MIN_ITEM characters), so a small document
    is not forced to be padded with questions it cannot support."""
    wanted = int(ask * QUIZ_MIN_ITEMS_RATIO)
    if material_chars is not None:
        wanted = min(wanted, int(material_chars // QUIZ_CHARS_PER_MIN_ITEM))
    return max(1, wanted)


def output_schema_for(ask: int, material_chars: int | None = None) -> dict:
    """The output schema of one call. Beside the fixed shape of a question it bounds the LIST:
    at least `min_items_for(ask, material_chars)` (the decoder cannot close the array early, the
    failure seen with real models) and at most `ask` + QUIZ_MAX_ITEMS_EXTRA. Padding items that
    turn out invalid or duplicate are simply rejected by the validator."""
    schema = copy.deepcopy(QUIZ_OUTPUT_SCHEMA)
    questions = schema["properties"]["questions"]
    questions["minItems"] = min_items_for(ask, material_chars)
    questions["maxItems"] = ask + QUIZ_MAX_ITEMS_EXTRA
    return schema


def context_budget(candidates: int) -> int:
    return max(QUIZ_CONTEXT_MIN_CHARS, min(QUIZ_CONTEXT_MAX_CHARS, candidates * QUIZ_CONTEXT_CHARS_PER_CANDIDATE))


# ---------------------------------------------------------------------------------------------
# Text helpers (language independent)
# ---------------------------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "the and for with from that this which what when where does are is was were into than then its "
    "their one option answer following about between during using used use can will not has have had "
    "how why who whom whose each any all also only most more some such according document statement "
    "describes described primary main purpose best correct true false identify select choose".split()
)
_LABEL = re.compile(r"^\s*\(?[A-Da-d][\.\):]\s+")
_SCAFFOLDING = re.compile(r"\[U\d+\]|\bunit_id\b|\bevidence_quote\b", flags=re.IGNORECASE)
_GENERIC_STEM = (
    re.compile(r"\bwhich statement is supported\b", re.IGNORECASE),
    re.compile(r"\bwhat does this (chunk|segment|context|excerpt) say\b", re.IGNORECASE),
    re.compile(r"\b(provided|given) (context|evidence|excerpt)s?\b", re.IGNORECASE),
)
_GENERIC_OPTIONS = frozenset({
    "all of the above", "none of the above", "both a and b", "both b and c", "cannot be determined",
    "not enough information",
})


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _clean_inline(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def squash(text: str) -> str:
    """Letters and digits only, casefolded and stripped of accents.

    This is what makes grounding robust to PDF extraction quirks: extractors that drop spaces
    ("Determineswhichprograms..."), lose or misplace Vietnamese tone marks, or models that
    re-space/re-punctuate/de-accent a quote still compare equal. Both sides of every comparison
    go through this same function, so it never creates a mismatch of its own.
    """
    folded = unicodedata.normalize("NFKD", str(text or "")).casefold().replace("đ", "d")
    return re.sub(r"[\W_]+", "", folded)


def content_tokens(text: str, min_chars: int = 3) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(rf"\w{{{min_chars},}}", _normalize(text), flags=re.UNICODE)
        if token not in _STOPWORDS
    )


def _shingles(squashed: str, size: int = QUIZ_SHINGLE_SIZE) -> frozenset[str]:
    if len(squashed) < size:
        return frozenset()
    return frozenset(squashed[index:index + size] for index in range(len(squashed) - size + 1))


def _jaccard(left: frozenset, right: frozenset) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


# ---------------------------------------------------------------------------------------------
# Excerpts: clean text of the whole document
# ---------------------------------------------------------------------------------------------

def _strip_repeated_lines(raw_texts: list[str]) -> list[str]:
    """Drop short lines repeated across many chunks (page headers/footers, running titles).

    They carry no teachable content, eat prompt budget and invite questions about author names or
    e-mail addresses. Only short lines that occur in at least QUIZ_REPEATED_LINE_MIN_CHUNKS (and at
    least a fifth of all) chunks are removed; ordinary content is never repeated that often.
    """
    line_lists = [[_clean_inline(line) for line in str(text or "").split("\n")] for text in raw_texts]
    threshold = max(QUIZ_REPEATED_LINE_MIN_CHUNKS, math.ceil(len(raw_texts) * 0.2))
    counts: Counter = Counter()
    for lines in line_lists:
        for key in {_normalize(line) for line in lines if 0 < len(line) <= QUIZ_REPEATED_LINE_MAX_CHARS}:
            counts[key] += 1
    repeated = {key for key, count in counts.items() if count >= threshold}
    return [" ".join(line for line in lines if line and _normalize(line) not in repeated) for lines in line_lists]


def _trim_overlap(previous: str, current: str) -> str:
    """Remove the text `current` repeats from the end of `previous` (splitter chunk overlap)."""
    limit = min(len(previous), len(current), QUIZ_OVERLAP_MAX_CHARS)
    for size in range(limit, QUIZ_OVERLAP_MIN_CHARS - 1, -1):
        if previous.endswith(current[:size]):
            return current[size:].lstrip()
    return current


def _split_long(text: str, max_chars: int) -> list[str]:
    parts = []
    while len(text) > max_chars:
        cut = text.rfind(" ", max_chars // 2, max_chars)
        cut = cut if cut > 0 else max_chars
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def build_study_units(
    chunks: list[dict],
    max_chars: int = QUIZ_UNIT_MAX_CHARS,
    min_chars: int = QUIZ_UNIT_MIN_CHARS,
) -> list[dict]:
    """Turn the document's chunks into ordered, clean excerpts; nothing is truncated.

    Repeated page headers and the overlap the splitter added between neighbouring chunks are
    removed first, so an excerpt's characters are unique document text. Tiny neighbouring chunks
    are merged, oversized ones split. `source_chunk_ids` lists the chunks whose text really is
    inside the excerpt. Which excerpts reach the model is decided by select_context_units.
    """
    usable: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in chunks:
        chunk_id = str((chunk.get("metadata") or {}).get("chunk_id") or "").strip()
        if chunk_id and chunk_id not in seen and _clean_inline(chunk.get("content")):
            seen.add(chunk_id)
            usable.append((chunk_id, chunk.get("content")))
    if not usable:
        return []

    cleaned = _strip_repeated_lines([text for _chunk_id, text in usable])
    pieces: list[tuple[str, str]] = []
    previous = ""
    for (chunk_id, _raw), text in zip(usable, cleaned):
        text = _clean_inline(text)
        trimmed = _trim_overlap(previous, text) if previous else text
        previous = text
        for part in _split_long(trimmed, max_chars):
            pieces.append((chunk_id, part))
    if not pieces:
        return []

    merged: list[tuple[list[str], str]] = []
    for chunk_id, text in pieces:
        if merged and len(merged[-1][1]) < min_chars and len(merged[-1][1]) + 1 + len(text) <= max_chars:
            ids, joined = merged[-1]
            merged[-1] = (ids + ([chunk_id] if chunk_id not in ids else []), f"{joined} {text}")
        else:
            merged.append(([chunk_id], text))

    units: list[dict] = []
    for index, (chunk_ids, text) in enumerate(merged):
        squashed = squash(text)
        units.append({
            "unit_id": f"U{index + 1}",
            "index": index,
            "source_chunk_ids": chunk_ids,
            "evidence_excerpt": text,
            "char_count": len(text),
            "name": f"Excerpt {index + 1}",
            "_squashed": squashed,
            "_shingles": _shingles(squashed),
        })
    return units


def public_unit(unit: dict) -> dict:
    return {key: unit[key] for key in ("unit_id", "source_chunk_ids", "char_count")}


def select_context_units(units: list[dict], budget_chars: int, exclude_ids=frozenset()) -> list[dict]:
    """The excerpts one call sees: all of them when they fit, otherwise an even spread.

    No importance model and no coverage requirement -- a document that does not fit is simply
    sampled at regular intervals so the model is not shown only its beginning. `exclude_ids` lets
    a follow-up call prefer excerpts no call has shown yet.
    """
    pool = [unit for unit in units if unit["unit_id"] not in exclude_ids]
    total = sum(unit["char_count"] for unit in pool)
    if not pool or total <= budget_chars:
        return pool
    count = max(1, int(budget_chars // (total / len(pool))))
    picked = [pool[int((slot + 0.5) * len(pool) / count)] for slot in range(count)]
    while len(picked) > 1 and sum(unit["char_count"] for unit in picked) > budget_chars:
        picked.remove(max(picked, key=lambda unit: unit["char_count"]))
    return picked


# ---------------------------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------------------------

_DIFFICULTY_CONTRACTS = {
    "easy": "Difficulty EASY: test one explicitly stated fact, definition, purpose or step.",
    "medium": (
        "Difficulty MEDIUM: require one reasoning step (comparison, cause and effect, ordering or "
        "applying a stated rule to a short situation), not plain recall."
    ),
    "difficult": (
        "Difficulty DIFFICULT: require applying or combining facts from the SAME excerpt (diagnose, "
        "predict, choose a consequence); never plain recall."
    ),
}


def build_generation_prompt(
    scope_name: str,
    difficulty: str,
    units: list[dict],
    count: int,
    avoid_stems: list[str] | None = None,
    avoid_quotes: list[str] | None = None,
) -> str:
    """`avoid_stems` / `avoid_quotes` (same order) describe the questions that already exist: a
    follow-up call is shown each stem with the sentence it was written from, so it can pick other
    facts instead of asking the same one again."""
    excerpts = "\n\n".join(f"[{unit['unit_id']}]\n{unit['evidence_excerpt']}" for unit in units)
    avoid = ""
    if avoid_stems:
        quotes = list(avoid_quotes or [])
        lines = []
        for index, stem in enumerate(avoid_stems):
            source = _clean_inline(quotes[index])[:QUIZ_AVOID_QUOTE_CHARS] if index < len(quotes) and quotes[index] else ""
            lines.append(f'- {stem}  [source: "{source}"]' if source else f"- {stem}")
        avoid = (
            "The list below holds the questions that already exist (each with the sentence it used). "
            "Do not repeat them, do not reword them, and do not ask about the same or a nearly identical "
            "fact again; choose OTHER facts. Questions already written:\n"
            + "\n".join(lines) + "\n"
        )
    spread_rule = (
        "- The questions already written are listed below the rules; use sentences and parts of the "
        "excerpts that they did not use.\n"
        if avoid_stems else
        "- Each question tests a different fact; never ask the same fact twice, even reworded. Draw the "
        "questions from different excerpts (at most 2 per excerpt) whenever the material allows.\n"
    )
    return (
        f"Write exactly {count} {difficulty} multiple-choice revision questions about \"{scope_name}\", "
        "using ONLY the excerpts below. Every fact in a question and in its correct answer must be stated "
        "in the excerpts; never add knowledge from outside them.\n"
        'Return JSON only: {"questions":[{"evidence_quote":"...","question":"...",'
        '"options":["...","...","...","..."],"answer_index":0,"explanation":"..."}]}\n'
        "Rules:\n"
        "- First choose evidence_quote: one or two consecutive sentences (about 8-30 words) copied exactly "
        "as written from an excerpt. Do not paraphrase, translate, or fix its spelling or spacing.\n"
        "- Then write a question whose correct answer is stated in that quote. Never ask about something "
        "the quote does not say.\n"
        "- Exactly 4 options and exactly 1 correct option; answer_index is the 0-based position (0-3) of the "
        "correct option. Vary the position of the correct option.\n"
        "- The 3 wrong options must be plausible but clearly wrong according to the excerpts.\n"
        "- The 4 options must have clearly different meanings: never write two options that say the same "
        "thing in different words, and no option may be a shorter or longer version of another.\n"
        f"{spread_rule}"
        "- No 'all/none of the above' options.\n"
        f"- Write ALL {count} questions: do not stop early.\n"
        "- Write in the main language of the excerpts. Question <=25 words, each option <=15 words, "
        "explanation <=25 words. No markdown, no extra fields.\n"
        f"{_DIFFICULTY_CONTRACTS[difficulty]}\n"
        f"{avoid}"
        f"EXCERPTS:\n{excerpts}"
    )


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------

def parse_candidates(text: str, report: dict | None = None) -> list:
    """Return the list under "questions".

    If `report` is given it is filled with {"salvaged": bool, "quote_keys": int}: how many
    question objects the text started, and whether the JSON was broken (so only complete objects
    were kept) -- the evidence needed to tell "the model wrote few" from "the output was cut".

    Falls back to salvaging every COMPLETE question object from truncated or slightly malformed
    output (e.g. generation stopped at num_predict), so one cut-off never discards a whole call.
    Raises ValueError when nothing usable can be recovered.
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", str(text or "").strip(), flags=re.IGNORECASE).strip()
    if report is not None:
        report.update({"salvaged": False, "quote_keys": cleaned.count('"evidence_quote"')})
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and isinstance(data.get("questions"), list):
            return data["questions"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    if report is not None:
        report["salvaged"] = True
    marker = re.search(r'"questions"\s*:\s*\[', cleaned)
    if not marker:
        raise ValueError("Quiz JSON does not contain a questions list.")
    decoder = json.JSONDecoder()
    position, salvaged = marker.end(), []
    while True:
        start = cleaned.find("{", position)
        if start < 0:
            break
        try:
            obj, position = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            break
        salvaged.append(obj)
    if not salvaged:
        raise ValueError("Quiz JSON could not be parsed and no complete question could be salvaged.")
    return salvaged


# ---------------------------------------------------------------------------------------------
# Grounding: is the question really about the context the model was given?
# ---------------------------------------------------------------------------------------------

def _shingle_containment(quote_squashed: str, unit: dict) -> float:
    shingles = _shingles(quote_squashed)
    if not shingles:
        return 0.0
    return len(shingles & unit["_shingles"]) / len(shingles)


def _alignment(quote_squashed: str, unit_squashed: str) -> float:
    """How much of the quote lines up, IN ORDER, with one contiguous stretch of the excerpt.

    1.0 for an exact substring. Otherwise the share of the quote covered by long-enough matching
    runs: a re-typed word, a dropped word or a fixed typo costs only its own characters, so the
    tolerance does not depend on how long the quote is. Scattered matches (a quote stitched
    together from pieces of the excerpt) are rejected: few runs, all inside one short stretch.
    """
    if quote_squashed in unit_squashed:
        return 1.0
    matcher = difflib.SequenceMatcher(None, unit_squashed, quote_squashed, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size >= QUIZ_ALIGN_BLOCK_MIN]
    if not blocks or len(blocks) > QUIZ_ALIGN_BLOCKS_MAX:
        return 0.0
    span = blocks[-1].a + blocks[-1].size - blocks[0].a
    if span > len(quote_squashed) * 1.3 + QUIZ_ALIGN_SPAN_SLACK:
        return 0.0
    return sum(block.size for block in blocks) / len(quote_squashed)


def locate_evidence(quote: str, units: list[dict]) -> tuple[dict, float] | None:
    """Find the excerpt that really contains `quote` and how well (1.0 = verbatim).

    Returns None when the quote is too short to prove anything or no excerpt contains it well
    enough. Comparison is on squashed text, so PDF spacing defects, accents, punctuation and case
    never matter; small in-order edits are tolerated (see _alignment) whatever the quote's length.
    """
    quote_squashed = squash(quote)
    if len(quote_squashed) < QUIZ_MIN_QUOTE_CHARS:
        return None
    scored: list[tuple[float, dict]] = [
        (1.0, unit) for unit in units if quote_squashed in unit["_squashed"]
    ]
    if not scored:
        shortlist = sorted(
            ((_shingle_containment(quote_squashed, unit), unit) for unit in units),
            key=lambda item: -item[0],
        )[:QUIZ_ALIGN_CANDIDATE_UNITS]
        scored = [
            (_alignment(quote_squashed, unit["_squashed"]), unit)
            for containment, unit in shortlist if containment >= QUIZ_ALIGN_PREFILTER
        ]
    if not scored:
        return None
    score, unit = max(scored, key=lambda item: item[0])
    return (unit, score) if score >= QUIZ_QUOTE_MIN_ALIGNMENT else None


def _token_supported(token: str, haystack_squashed: str) -> bool:
    squashed = squash(token)
    if not squashed:
        return False
    if squashed in haystack_squashed:
        return True
    if len(squashed) > 10:  # long word or unsegmented (CJK) run: allow inflection / partial restatement
        pieces = _shingles(squashed, 5)
        return bool(pieces) and sum(piece in haystack_squashed for piece in pieces) / len(pieces) >= 0.7
    return False


def _distinctive_tokens(text: str, units: list[dict]) -> frozenset[str]:
    """Meaningful words of `text` that can actually tell one part of the context from another.

    Words present in more than QUIZ_COMMON_TOKEN_SHARE of the excerpts (function words, the
    document's own topic word) carry no evidence, in any language, so they are ignored. This
    replaces per-language stopword lists.
    """
    tokens = content_tokens(text, QUIZ_LINK_TOKEN_MIN_CHARS)
    if len(units) < QUIZ_COMMON_MIN_UNITS:
        return tokens
    limit = QUIZ_COMMON_TOKEN_SHARE * len(units)
    return frozenset(
        token for token in tokens
        if sum(squash(token) in unit["_squashed"] for unit in units) <= limit
    )


def context_support(stem: str, correct_option: str, unit: dict, units: list[dict]) -> tuple[float | None, float | None]:
    """How much of the question + correct answer is backed by the context the model was given.

    Returns (joint, answer):
    * joint  -- share of the distinctive words of stem + answer found in THE EXCERPT the quote sits
                in (is the question about that passage?);
    * answer -- share of the answer's distinctive words found ANYWHERE in the context (is the
                answer stated in the material at all?; 1.0 when a word of 8+ letters is found in that excerpt). An
                answer may legitimately name a section
                heading that lives in a neighbouring excerpt; one built from outside knowledge is
                found nowhere.
    Both compare against the document's own text -- for a bilingual document that is both
    languages -- and never against the language of the quote, so a Vietnamese question about an
    English bullet is judged by the Vietnamese translation printed next to it. Matching is on
    squashed text, so glued PDF words, accents and inflections still count. None means there was
    nothing distinctive to judge.
    """
    haystack = unit["_squashed"]
    answer_tokens = _distinctive_tokens(correct_option, units)
    all_tokens = _distinctive_tokens(stem, units) | answer_tokens
    joint = (sum(_token_supported(t, haystack) for t in all_tokens) / len(all_tokens)) if all_tokens else None
    answer = None
    if answer_tokens:
        found = [token for token in answer_tokens if any(_token_supported(token, other["_squashed"]) for other in units)]
        answer = len(found) / len(answer_tokens)
        # Natural answers carry filler words the material never uses ("because", "operations"), so
        # one long word found in the very excerpt the question quotes is enough of an anchor (a long
        # word cannot match glued text by accident). A long word that only occurs somewhere else in
        # the document does not rescue an otherwise unsupported answer.
        if any(len(squash(token)) >= QUIZ_STRONG_ANCHOR_CHARS and _token_supported(token, haystack) for token in found):
            answer = 1.0
    return joint, answer


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------

def _options_too_similar(options: list[str]) -> bool:
    """Two options that are one answer written twice.

    Compared as bags of meaningful words: identical bags (order, articles and punctuation
    changed), or -- for options of 3+ words -- bags that overlap by QUIZ_OPTION_BAG_JACCARD after
    cutting every word to its first 4 letters (moved/moves, process/processes). Deliberately
    narrow: an antonym pair ("increases ..." / "decreases ...") and nested names ("Preemptive" /
    "Non-preemptive", "I/O bound" / "CPU bound") are good distractors and are NOT flagged.
    """
    bags = [content_tokens(option, 2) for option in options]
    for left in range(len(bags)):
        for right in range(left + 1, len(bags)):
            first, second = bags[left], bags[right]
            if not first or not second:
                continue
            if first == second:
                return True
            if min(len(first), len(second)) >= 3:
                stems_first, stems_second = frozenset(t[:4] for t in first), frozenset(t[:4] for t in second)
                if _jaccard(stems_first, stems_second) >= QUIZ_OPTION_BAG_JACCARD:
                    return True
    return False


def validate_candidate(
    raw,
    units: list[dict],
    accepted: list[dict],
    difficulty: str,
    question_id: int,
    scope_topic: dict,
    generation_index: int,
    assessment_capacity: int | None = None,
) -> tuple[dict, list[str]]:
    """Validate one raw candidate against the context the model saw and the questions accepted so far.

    `units` are the excerpts the model was given. Hard rejections raise CandidateRejected; soft
    findings are returned as warnings and only lower the candidate's rank during selection.
    Provenance is derived here from the located evidence.
    """
    if not isinstance(raw, dict):
        raise CandidateRejected("structure", "Question must be a JSON object.")

    stem = _clean_inline(raw.get("question"))
    if len(stem) < QUIZ_MIN_STEM_CHARS or len(stem) > QUIZ_MAX_STEM_CHARS or any(p.search(stem) for p in _GENERIC_STEM):
        raise CandidateRejected("structure", "Question stem is empty, too short/long, or generic.")
    if _SCAFFOLDING.search(stem):
        raise CandidateRejected("structure", "Question contains generation scaffolding.")

    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or len(raw_options) != 4 or not all(isinstance(o, str) for o in raw_options):
        raise CandidateRejected("structure", "Question must contain exactly 4 options.")
    # Only an explicit "A." / "A)" / "(A)" label is stripped: the generic label stripper in
    # backend.quiz_options would also eat the article of an option such as "A process ...".
    options = [_clean_inline(_LABEL.sub("", option)) for option in raw_options]
    if any(not option for option in options):
        raise CandidateRejected("structure", "Question has an empty option.")
    for option in options:
        if (
            _normalize(option).strip(" .") in _GENERIC_OPTIONS or len(option) > QUIZ_MAX_OPTION_CHARS
            or _SCAFFOLDING.search(option)
        ):
            raise CandidateRejected("structure", "Option is generic, scaffolding, or copied raw text.")
    normalized_options = [_normalize(option).strip(" .") for option in options]
    if len(set(normalized_options)) != 4 or any(
        difflib.SequenceMatcher(None, normalized_options[a], normalized_options[b]).ratio() >= QUIZ_OPTION_DUPLICATE_RATIO
        for a in range(4) for b in range(a + 1, 4)
    ):
        raise CandidateRejected("structure", "Options are not distinct.")
    if _options_too_similar(options):
        raise CandidateRejected("structure", "Two options say the same thing in different words.")

    answer_index = raw.get("answer_index")
    if isinstance(answer_index, bool) or not isinstance(answer_index, int) or answer_index not in range(4):
        raise CandidateRejected("structure", "answer_index must be a single integer 0-3.")

    explanation = _clean_inline(raw.get("explanation"))

    quote = _clean_inline(raw.get("evidence_quote"))
    located = locate_evidence(quote, units)
    if located is None:
        raise CandidateRejected("grounding", "evidence_quote was not found in the provided context.")
    unit, alignment = located
    quote_squashed = squash(quote)
    link, answer_link = context_support(stem, options[answer_index], unit, units)
    if link is not None and link < QUIZ_MIN_LINK_SUPPORT:
        raise CandidateRejected("grounding", "The question and its answer are not supported by the context.")
    # One matching word is not an anchor: in glued PDF text a 5-letter word occurs inside other
    # words by chance, so at least half of the answer's distinctive words must be found.
    if answer_link is not None and answer_link < QUIZ_MIN_ANSWER_ANCHOR:
        raise CandidateRejected("grounding", "The correct answer is not stated in the provided context.")

    stem_key = squash(stem)
    for existing in accepted:
        existing_key = squash(existing["question"])
        if stem_key == existing_key or difflib.SequenceMatcher(None, stem_key, existing_key).ratio() >= QUIZ_STEM_DUPLICATE_RATIO:
            raise CandidateRejected("duplicate", "Question duplicates an accepted question.")
    signature = content_tokens(f"{stem} {options[answer_index]}")
    quote_shingles = _shingles(quote_squashed)
    for existing in accepted:
        meta = existing["_meta"]
        if (
            (signature and _jaccard(signature, meta["signature"]) >= QUIZ_CONTENT_DUPLICATE_JACCARD)
            or _jaccard(quote_shingles, meta["quote_shingles"]) >= QUIZ_QUOTE_DUPLICATE_JACCARD
        ):
            raise CandidateRejected("duplicate", "Question tests the same fact or concept as an accepted question.")

    warnings: list[str] = []
    answer_squashed = squash(options[answer_index])
    answer_in_evidence = len(answer_squashed) >= 4 and answer_squashed in unit["_squashed"]
    if alignment < 1.0:
        warnings.append("quote_not_verbatim")
    if answer_link is None:
        warnings.append("unverified_answer")  # numbers / very short answers: nothing to anchor, so rank lower
    if link is None:
        warnings.append("unverifiable_relevance")
    elif link < QUIZ_WEAK_LINK_SUPPORT:
        warnings.append("weak_relevance")
    if _SCAFFOLDING.search(explanation):
        warnings.append("explanation_scaffolding")
    if len(explanation.split()) < 3:
        warnings.append("short_explanation")
    if difficulty in {"medium", "difficult"} and len(stem.split()) < 6:
        warnings.append("shallow_stem")

    normalized = {
        "id": question_id,
        "question": stem,
        "options": [f"{OPTION_LETTERS[i]}. {option}" for i, option in enumerate(options)],
        "correct_answer": OPTION_LETTERS[answer_index],
        "question_type": "single_choice",
        "correct_answers": [OPTION_LETTERS[answer_index]],
        "topic_id": str(scope_topic["topic_id"]),
        "topic_name": str(scope_topic.get("name") or scope_topic["topic_id"]),
        "concept_id": unit["unit_id"],
        "concept_name": unit["name"],
        "source_subtopic_ids": [],
        "concept_origin": "study_unit",
        "concept_plan_id": QUIZ_ENGINE_VERSION,
        "assessment_capacity": int(assessment_capacity if assessment_capacity is not None else len(units)),
        "difficulty": difficulty,
        "explanation": explanation,
        "source_chunk_ids": list(unit["source_chunk_ids"]),
        "validation_outcome": "accepted_quality_warning" if warnings else "accepted",
        # Internal ranking/debug data; removed by finalize_questions before persistence.
        "_meta": {
            "index": generation_index, "unit_index": unit["index"], "signature": signature,
            "quote_shingles": quote_shingles, "quote": quote, "alignment": alignment, "link": link,
            "answer_in_evidence": answer_in_evidence,
        },
    }
    return normalized, sorted(set(warnings))


# ---------------------------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------------------------

def _rank(question: dict) -> tuple:
    meta = question["_meta"]
    return (
        question["validation_outcome"] != "accepted",
        not meta["answer_in_evidence"],
        -(meta["link"] if meta["link"] is not None else 0.0),
        -meta["alignment"],
        meta["index"],
    )


def select_questions(pool: list[dict], target: int) -> list[dict]:
    """The best `target` candidates (all of them if the pool is smaller), in document order.

    The count is a target, never a requirement: a short pool simply returns every valid
    candidate. Nothing is invented and no rule is relaxed to reach the target.
    """
    best = sorted(pool, key=_rank)[:max(0, target)]
    return sorted(best, key=lambda question: (question["_meta"]["unit_index"], question["_meta"]["index"]))


def finalize_questions(selected: list[dict]) -> tuple[list[dict], list[dict]]:
    """Renumber, strip internal data; returns (public questions, per-question evidence records)."""
    capacity = max(1, len({question["concept_id"] for question in selected}))
    questions, evidence = [], []
    for number, question in enumerate(selected, start=1):
        meta = question.pop("_meta")
        question["id"] = number
        question["assessment_capacity"] = capacity
        questions.append(question)
        evidence.append({
            "question_id": number, "unit_id": question["concept_id"], "evidence_quote": meta["quote"],
            "quote_alignment": round(meta["alignment"], 3),
            "context_support": None if meta["link"] is None else round(meta["link"], 3),
            "answer_in_evidence": meta["answer_in_evidence"],
        })
    return questions, evidence
