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
                          (PDF-artifact tolerant; refuses negative "NOT/EXCEPT" questions; a question is a
                          duplicate when it tests the same FACT, not merely the same sentence)
* followup_units        -- what a follow-up call is shown: unused text preferred, used text only if needed
* select_questions      -- best-ranked candidates, returned in document order
* validate_multiple_select_candidate -- 4 options / 2+ correct (each with its own grounded quote)

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
QUIZ_PROMPT_VERSION = "simple_context_v8_single_choice"

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
QUIZ_UNUSED_CHARS_PER_QUESTION = 150  # unused text (about one short bullet) a follow-up needs per missing question

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
QUIZ_ALIGN_BLOCKS_MAX = 8             # at most this many separate matching runs (PDF glyph drops split a quote into runs)
QUIZ_ALIGN_SPAN_SLACK = 20            # aligned region may exceed the quote length by this much (+30%)
QUIZ_ALIGN_PREFILTER = 0.25
QUIZ_ALIGN_CANDIDATE_UNITS = 3
QUIZ_SHINGLE_SIZE = 8
QUIZ_LINK_TOKEN_MIN_CHARS = 5         # words shorter than this match glued text by accident
QUIZ_MIN_LINK_SUPPORT = 0.25          # question + answer vs the excerpt the quote sits in
QUIZ_WEAK_LINK_SUPPORT = 0.5
QUIZ_MIN_ANSWER_ANCHOR = 0.5          # share of the answer's distinctive words found in the context
QUIZ_STEM_MIN_PREFIX = 6              # inflected words ("scheduling"/"schedule") match on this many leading letters ...
QUIZ_STEM_MAX_DROP = 3                # ... after dropping at most this many trailing letters (words of 7+ letters only)
QUIZ_MIN_ANSWER_MATCHES = 2           # ... and never fewer than this many (one shared word is not support)
QUIZ_COMMON_TOKEN_SHARE = 0.4         # a word present in more than this share of excerpts says nothing
QUIZ_COMMON_MIN_UNITS = 5             # ... but only judged when the context has at least this many

# --- duplicates / structure ---------------------------------------------------------------------
QUIZ_CONTENT_DUPLICATE_JACCARD = 0.6
QUIZ_QUOTE_DUPLICATE_JACCARD = 0.6
QUIZ_STEM_DUPLICATE_RATIO = 0.9
QUIZ_OPTION_DUPLICATE_RATIO = 0.94
QUIZ_OPTION_BAG_JACCARD = 0.8
QUIZ_EVIDENCE_OVERLAP_MAX = 0.5       # two questions on (mostly) the same evidence ...
QUIZ_SAME_TARGET_JACCARD = 0.5        # ... whose correct answers overlap this much test the same fact
QUIZ_SEGMENT_MAX_CHARS = 450          # a used bullet/sentence is hidden from later calls as a whole up to this size
QUIZ_MIN_REMAINING_CHARS = 100        # an excerpt with less unused text than this is not shown again
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
    """A candidate failed a hard validation rule. `category` feeds the diagnostics counters and
    `code` says which rule (quote_not_found, answer_not_in_context, duplicate_evidence, ...), so a
    real run shows WHY candidates were rejected, not only how many."""

    def __init__(self, category: str, message: str, code: str | None = None):
        super().__init__(message)
        self.category = category  # "structure" | "grounding" | "duplicate"
        self.code = code or category


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
    "describes described primary main purpose best correct true false identify select choose "
    "because therefore however instead whereas rather their there these those every other another should would could".split()
)
_LABEL = re.compile(r"^\s*\(?[A-Da-d][\.\):]\s+")
_SCAFFOLDING = re.compile(r"\[U\d+\]|\bunit_id\b|\bevidence_quote\b", flags=re.IGNORECASE)
_GENERIC_STEM = (
    re.compile(r"\bwhich statement is supported\b", re.IGNORECASE),
    re.compile(r"\bwhat does this (chunk|segment|context|excerpt) say\b", re.IGNORECASE),
    re.compile(r"\b(provided|given) (context|evidence|excerpt)s?\b", re.IGNORECASE),
)
# "Which is NOT ...", "all EXCEPT ...", "which statement is incorrect/false": the right answer is the
# one option that is NOT supported, which no deterministic check can verify (and models often mark a
# supported option as the answer). Such questions are refused instead of guessed at.
_NEGATIVE_EMPHASIS = re.compile(r"\b(?:NOT|EXCEPT|INCORRECT|FALSE)\b|\bKHÔNG\b")
_NEGATIVE_PHRASES = re.compile(
    r"\b(?:except|incorrect(?:ly)?|false|not true|not correct|least likely|least true)\b"
    r"|\bwhich\b[^?.]{0,60}\b(?:is|are|was|were|does|do|did|can|would|will)\s+not\b"
    r"|ngoại trừ|không đúng|không chính xác|\b(?:phát biểu|câu|mệnh đề|nhận định|khẳng định|nào)\s+sai\b",
    flags=re.IGNORECASE,
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
        "- The questions already written are listed below the rules; prefer sentences and parts of the "
        "excerpts that they did not use. A sentence they used may still hold a different fact: ask about "
        "it only if none of the listed questions tests that fact.\n"
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
        "- No 'all/none of the above' options. No negative questions: the question must not contain NOT, EXCEPT, "
        "incorrect or false (or their equivalent in the excerpts' language) -- never ask which option is NOT true; "
        "ask what the excerpts positively state.\n"
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


def _join_units(first: dict, second: dict) -> dict:
    """Two neighbouring excerpts read as one text (a sentence can run across the boundary between
    two chunks, and so across two excerpts). `_parts` remembers where the boundary is."""
    return {
        **first,
        "_squashed": first["_squashed"] + second["_squashed"],
        "_shingles": first["_shingles"] | second["_shingles"],
        "source_chunk_ids": list(dict.fromkeys([*first["source_chunk_ids"], *second["source_chunk_ids"]])),
        "evidence_excerpt": f"{first['evidence_excerpt']} {second['evidence_excerpt']}",
        "char_count": first["char_count"] + second["char_count"],
        "_parts": ((first["unit_id"], len(first["_squashed"])), (second["unit_id"], len(second["_squashed"]))),
    }


def _adjacent_pairs(units: list[dict]) -> list[dict]:
    ordered = sorted(units, key=lambda unit: unit["index"])
    return [_join_units(first, second) for first, second in zip(ordered, ordered[1:])
            if second["index"] == first["index"] + 1]


def _best_match(quote_squashed: str, candidates: list[dict]) -> tuple[dict, float] | None:
    scored: list[tuple[float, dict]] = [
        (1.0, unit) for unit in candidates if quote_squashed in unit["_squashed"]
    ]
    if not scored:
        shortlist = sorted(
            ((_shingle_containment(quote_squashed, unit), unit) for unit in candidates),
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


def locate_evidence(quote: str, units: list[dict]) -> tuple[dict, float] | None:
    """Find the excerpt that really contains `quote` and how well (1.0 = verbatim).

    Returns None when the quote is too short to prove anything or no excerpt contains it well
    enough. Comparison is on squashed text, so PDF spacing defects, accents, punctuation and case
    never matter; small in-order edits are tolerated (see _alignment) whatever the quote's length.
    A quote that runs across the boundary of two NEIGHBOURING excerpts (a sentence split by the
    chunker) is found in the two read as one; the result is then a joined excerpt (`_parts`)
    whose source_chunk_ids cover both. Excerpts that are not neighbours are never joined.
    """
    quote_squashed = squash(quote)
    if len(quote_squashed) < QUIZ_MIN_QUOTE_CHARS:
        return None
    return _best_match(quote_squashed, units) or _best_match(quote_squashed, _adjacent_pairs(units))


def evidence_spans(quote: str, unit: dict) -> list[tuple[str, int, int]]:
    """Where in the (squashed) excerpt(s) the quote sits: [(unit_id, start, end), ...].

    This is the fact the question was built on; the backend uses it to notice a second question
    about the same passage and to hide used passages from later calls.
    """
    quote_squashed = squash(quote)
    text = unit["_squashed"]
    start = text.find(quote_squashed)
    if start >= 0:
        end = start + len(quote_squashed)
    else:
        blocks = [block for block in difflib.SequenceMatcher(None, text, quote_squashed, autojunk=False).get_matching_blocks()
                  if block.size >= QUIZ_ALIGN_BLOCK_MIN]
        if not blocks:
            return []
        start, end = blocks[0].a, blocks[-1].a + blocks[-1].size
    parts = unit.get("_parts")
    if not parts:
        return [(unit["unit_id"], start, end)]
    spans, offset = [], 0
    for unit_id, length in parts:
        low, high = max(start, offset), min(end, offset + length)
        if high > low:
            spans.append((unit_id, low - offset, high - offset))
        offset += length
    return spans


def _token_supported(token: str, haystack_squashed: str) -> bool:
    squashed = squash(token)
    if not squashed:
        return False
    if squashed in haystack_squashed:
        return True
    if len(squashed) > 10:  # long word or unsegmented (CJK) run: allow inflection / partial restatement
        pieces = _shingles(squashed, 5)
        return bool(pieces) and sum(piece in haystack_squashed for piece in pieces) / len(pieces) >= 0.7
    if len(squashed) > QUIZ_STEM_MIN_PREFIX:
        # an inflected form of a word the material uses (scheduling/schedule, preemption/preemptive):
        # its leading letters, dropping at most a few endings, are found. Words of 6 letters or
        # fewer must match whole, so short words cannot match glued text by accident.
        prefix = squashed[:max(QUIZ_STEM_MIN_PREFIX, len(squashed) - QUIZ_STEM_MAX_DROP)]
        return prefix in haystack_squashed
    return False


def _distinctive_tokens(text: str, units: list[dict], min_chars: int = QUIZ_LINK_TOKEN_MIN_CHARS) -> frozenset[str]:
    """Meaningful words of `text` that can actually tell one part of the context from another.

    Words present in more than QUIZ_COMMON_TOKEN_SHARE of the excerpts (function words, the
    document's own topic word) carry no evidence, in any language, so they are ignored. This
    replaces per-language stopword lists.
    """
    tokens = content_tokens(text, min_chars)
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
                answer stated in the material at all?). At least half of them AND at least
                QUIZ_MIN_ANSWER_MATCHES of them (all of them for a one-word answer): a single shared
                word -- "algorithm" in an answer about the banker's algorithm -- never grounds an
                answer. An answer may legitimately name a section heading that lives in a
                neighbouring excerpt; one built from outside knowledge is found nowhere.
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
        if len(found) < min(len(answer_tokens), QUIZ_MIN_ANSWER_MATCHES):
            answer = 0.0
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


def _same_target(new_tokens, new_key: str, old_tokens, old_key: str) -> bool:
    """Do two correct answers state the same fact? (identical, largely the same words, or one
    contained in the other)"""
    if new_key and new_key == old_key:
        return True
    if new_tokens and old_tokens and _jaccard(new_tokens, old_tokens) >= QUIZ_SAME_TARGET_JACCARD:
        return True
    shorter, longer = sorted((new_key, old_key), key=len)
    return len(shorter) >= 8 and shorter in longer


def _evidence_overlap(new_spans, old_spans) -> float:
    """Largest share of the smaller of two evidence spans (same excerpt) that they have in common."""
    best = 0.0
    for new_unit, new_start, new_end in new_spans:
        for old_unit, old_start, old_end in old_spans:
            if new_unit != old_unit:
                continue
            common = min(new_end, old_end) - max(new_start, old_start)
            smaller = min(new_end - new_start, old_end - old_start)
            if common > 0 and smaller > 0:
                best = max(best, common / smaller)
    return best


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
        raise CandidateRejected("structure", "Question stem is empty, too short/long, or generic.", "stem")
    if _SCAFFOLDING.search(stem):
        raise CandidateRejected("structure", "Question contains generation scaffolding.", "scaffolding")
    if _NEGATIVE_EMPHASIS.search(stem) or _NEGATIVE_PHRASES.search(stem):
        raise CandidateRejected(
            "structure", "Negative questions (NOT / EXCEPT / incorrect / false) cannot be verified against the material.",
            "negative_polarity",
        )

    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or len(raw_options) != 4 or not all(isinstance(o, str) for o in raw_options):
        raise CandidateRejected("structure", "Question must contain exactly 4 options.", "options")
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
        raise CandidateRejected("structure", "Options are not distinct.", "options")
    if _options_too_similar(options):
        raise CandidateRejected("structure", "Two options say the same thing in different words.", "options")

    answer_index = raw.get("answer_index")
    if isinstance(answer_index, bool) or not isinstance(answer_index, int) or answer_index not in range(4):
        raise CandidateRejected("structure", "answer_index must be a single integer 0-3.")

    explanation = _clean_inline(raw.get("explanation"))

    quote = _clean_inline(raw.get("evidence_quote"))
    located = locate_evidence(quote, units)
    if located is None:
        code = "quote_too_short" if len(squash(quote)) < QUIZ_MIN_QUOTE_CHARS else "quote_not_found"
        raise CandidateRejected("grounding", "evidence_quote was not found in the provided context.", code)
    unit, alignment = located
    quote_squashed = squash(quote)
    link, answer_link = context_support(stem, options[answer_index], unit, units)
    if link is not None and link < QUIZ_MIN_LINK_SUPPORT:
        raise CandidateRejected("grounding", "The question and its answer are not supported by the context.", "question_not_supported")
    # One matching word is not an anchor: in glued PDF text a 5-letter word occurs inside other
    # words by chance, so at least half of the answer's distinctive words must be found.
    if answer_link is not None and answer_link < QUIZ_MIN_ANSWER_ANCHOR:
        raise CandidateRejected("grounding", "The correct answer is not stated in the provided context.", "answer_not_in_context")

    stem_key = squash(stem)
    for existing in accepted:
        existing_key = squash(existing["question"])
        if stem_key == existing_key or difflib.SequenceMatcher(None, stem_key, existing_key).ratio() >= QUIZ_STEM_DUPLICATE_RATIO:
            raise CandidateRejected("duplicate", "Question duplicates an accepted question.", "duplicate_stem")
    # Two questions may be built on the same sentence when they test different facts of it. They
    # are the same question when they rest on (mostly) the same evidence AND their correct answers
    # are the same fact.
    spans = evidence_spans(quote, unit)
    answer_tokens, answer_key = content_tokens(options[answer_index]), squash(options[answer_index])
    for existing in accepted:
        meta = existing["_meta"]
        if (_evidence_overlap(spans, meta.get("spans", ())) >= QUIZ_EVIDENCE_OVERLAP_MAX
                and _same_target(answer_tokens, answer_key, meta["answer_tokens"], meta["answer_key"])):
            raise CandidateRejected("duplicate", "Question tests the same fact as an accepted question on the same evidence.", "duplicate_evidence")
    signature = content_tokens(f"{stem} {options[answer_index]}")
    quote_shingles = _shingles(quote_squashed)
    for existing in accepted:
        meta = existing["_meta"]
        if signature and _jaccard(signature, meta["signature"]) >= QUIZ_CONTENT_DUPLICATE_JACCARD:
            raise CandidateRejected("duplicate", "Question tests the same fact or concept as an accepted question.", "duplicate_content")
        # the same sentence quoted again is a duplicate only when the same fact is asked (a sentence can hold several)
        if (_jaccard(quote_shingles, meta["quote_shingles"]) >= QUIZ_QUOTE_DUPLICATE_JACCARD
                and _same_target(answer_tokens, answer_key, meta["answer_tokens"], meta["answer_key"])):
            raise CandidateRejected("duplicate", "Question asks the same fact as an accepted question on the same sentence.", "duplicate_quote")

    warnings: list[str] = []
    answer_squashed = squash(options[answer_index])
    # ONE definition, the one acceptance used: the answer's distinctive words were found in the
    # material (answer_link is not None means they passed QUIZ_MIN_ANSWER_ANCHOR above), or -- for an
    # answer with no distinctive word (an acronym, a number) -- the whole answer text occurs in it.
    # (It used to be "the whole answer is a substring of the quote's excerpt", which is False for a
    # supported answer that is reordered, inflected or found in a neighbouring excerpt.)
    answer_in_evidence = answer_link is not None or (
        len(answer_squashed) >= 4 and any(answer_squashed in context["_squashed"] for context in units)
    )
    if alignment < 1.0:
        warnings.append("quote_not_verbatim")
    if not answer_in_evidence:
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
            "answer_in_evidence": answer_in_evidence, "spans": spans,
            "answer_tokens": answer_tokens, "answer_key": answer_key,
        },
    }
    return normalized, sorted(set(warnings))


# ---------------------------------------------------------------------------------------------
# Evidence already used (what a follow-up call must not be shown again)
# ---------------------------------------------------------------------------------------------

# Generic boundaries of a bullet / sentence / numbered item in extracted text (no language or
# document format is assumed; text without any of them is simply cut at the quote itself).
_SEGMENT_BREAK = re.compile(r"[○●•▪◦■□◆◇▶►➢✓✔]|(?<=[.!?])\s+|\s(?=\d{1,2}\.\s)")


def _raw_positions(text: str) -> list[int]:
    """For every character of squash(text): the index of the character of `text` it came from."""
    positions: list[int] = []
    for index, char in enumerate(text):
        positions.extend([index] * len(squash(char)))
    return positions


def segment_bounds(unit: dict, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The passages of the excerpt (raw character ranges) that the given squashed spans belong to.

    A span is widened to its whole bullet / sentence / numbered item when that is short, so the
    translation or restatement printed next to a sentence counts as the same passage. Text with
    none of those boundaries is cut at the quote itself. Overlapping passages are merged.
    Returns [] when the excerpt cannot be mapped back reliably.
    """
    text = unit["evidence_excerpt"]
    raw_of = _raw_positions(text)
    if len(raw_of) != len(unit["_squashed"]):
        return []
    breaks = sorted({
        match.start() if len(match.group()) == 1 and not match.group().isspace() else match.end()
        for match in _SEGMENT_BREAK.finditer(text)
    })
    cuts: list[list[int]] = []
    for start, end in spans:
        if not 0 <= start < end <= len(raw_of):
            continue
        low, high = raw_of[start], raw_of[end - 1] + 1
        before = max([0] + [position for position in breaks if position <= low])
        after = min([len(text)] + [position for position in breaks if position >= high])
        cuts.append([before, after] if after - before <= QUIZ_SEGMENT_MAX_CHARS else [low, high])
    cuts.sort()
    merged: list[list[int]] = []
    for cut in cuts:
        if merged and cut[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], cut[1])
        else:
            merged.append(cut)
    return [(low, high) for low, high in merged]


def unused_excerpt(unit: dict, spans: list[tuple[int, int]]) -> str:
    """The excerpt without the passages accepted questions were built on."""
    text = unit["evidence_excerpt"]
    kept, cursor = [], 0
    for low, high in segment_bounds(unit, spans):
        kept.append(text[cursor:low])
        cursor = high
    kept.append(text[cursor:])
    return _clean_inline(" ".join(kept))


def mask_used_evidence(units: list[dict], accepted: list[dict]) -> list[dict]:
    """Excerpts with the passages of the accepted questions removed; excerpts with nothing left
    worth showing are dropped. Used only to build a follow-up prompt: validation still checks
    quotes against the original excerpts."""
    spans_by_unit: dict[str, list[tuple[int, int]]] = {}
    for question in accepted:
        for unit_id, start, end in question["_meta"].get("spans", ()):
            spans_by_unit.setdefault(unit_id, []).append((start, end))
    masked = []
    for unit in units:
        spans = spans_by_unit.get(unit["unit_id"])
        text = unused_excerpt(unit, spans) if spans else unit["evidence_excerpt"]
        if len(text) >= QUIZ_MIN_REMAINING_CHARS:
            masked.append({**unit, "evidence_excerpt": text, "char_count": len(text)})
    return masked


def _least_used_first(units: list[dict], usage: Counter, budget_chars: int) -> list[dict]:
    """As many excerpts as fit the budget, the ones fewest accepted questions came from first."""
    chosen, total = [], 0
    for unit in sorted(units, key=lambda item: (usage.get(item["unit_id"], 0), item["index"])):
        if chosen and total + unit["char_count"] > budget_chars:
            continue
        chosen.append(unit)
        total += unit["char_count"]
    return sorted(chosen, key=lambda item: item["index"])


def followup_units(units: list[dict], accepted: list[dict], budget_chars: int, wanted_chars: int) -> list[dict]:
    """The evidence of a follow-up call once every excerpt has been shown: a PREFERENCE for new
    material, not a ban on used material.

    * enough unused text (`wanted_chars`) -> only that, least-used excerpts first;
    * too little left -> the whole excerpts again, least-used first. A passage that already became
      a question can still hold a different fact; the list of existing questions in the prompt and
      the duplicate checks keep the new questions different.
    """
    usage = Counter(question["concept_id"] for question in accepted)
    unused = _least_used_first(mask_used_evidence(units, accepted), usage, budget_chars)
    if sum(unit["char_count"] for unit in unused) >= wanted_chars:
        return unused
    return _least_used_first(units, usage, budget_chars)


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
            # fill_blank: the source flashcard (the persisted question has no column for it)
            **({"flashcard_id": meta["flashcard_id"]} if meta.get("flashcard_id") else {}),
        })
    return questions, evidence


# ---------------------------------------------------------------------------------------------
# Multiple select ("select all that apply")
# ---------------------------------------------------------------------------------------------
# Persisted with the existing question_type "multi_select" (the token the store, grading and Quiz
# Player already use): four "A. ..." options and correct_answers = the sorted correct LETTERS. The
# backend decides how many a quiz asks for (multiple_select_target); a separate, bounded call writes
# them. Every correct option needs its OWN evidence quote, found in the excerpts the model was shown,
# and must be stated in the excerpt that quote sits in. Grading is exact-set (no partial credit).

MULTI_SELECT_QUESTION_TYPE = "multi_select"
QUIZ_MULTI_SELECT_SHARE = 0.25
QUIZ_MULTI_SELECT_MIN_CORRECT = 2
QUIZ_MULTI_SELECT_MAX_CALLS = 2       # one call plus one retry when the pool is still short
QUIZ_MULTI_SELECT_DISTRACTOR_MIN_CHARS = 8
# A wrong option whose distinctive words occur TOGETHER in one excerpt (or two neighbouring ones) at
# least this much is treated as a statement the material supports, i.e. possibly true.
QUIZ_MULTI_SELECT_DISTRACTOR_SUPPORT = 0.75
QUIZ_MULTI_SELECT_DISTRACTOR_TOKEN_MIN_CHARS = 3

QUIZ_MULTI_SELECT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                    "correct_answers": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
                    "explanation": {"type": "string"},
                },
                "required": ["evidence_quotes", "question", "options", "correct_answers", "explanation"],
            },
        }
    },
    "required": ["questions"],
}


def multiple_select_target(question_count: int) -> int:
    """How many multiple_select questions a Normal Quiz asks for: a quarter, rounded half up
    (4 -> 1, 8 -> 2, 12 -> 3, 15 -> 4, 18 -> 5, 20 -> 5). Decided here, never by the model."""
    return max(0, math.floor(question_count * QUIZ_MULTI_SELECT_SHARE + 0.5))


def multiple_select_output_schema(ask: int) -> dict:
    schema = copy.deepcopy(QUIZ_MULTI_SELECT_OUTPUT_SCHEMA)
    schema["properties"]["questions"]["minItems"] = 1
    schema["properties"]["questions"]["maxItems"] = ask + QUIZ_MAX_ITEMS_EXTRA
    return schema


def build_multiple_select_prompt(
    scope_name: str,
    difficulty: str,
    units: list[dict],
    count: int,
    avoid_stems: list[str] | None = None,
) -> str:
    excerpts = "\n\n".join(f"[{unit['unit_id']}]\n{unit['evidence_excerpt']}" for unit in units)
    avoid = ""
    if avoid_stems:
        avoid = ("Questions that already exist (do not test the same facts):\n"
                 + "\n".join(f"- {stem}" for stem in avoid_stems) + "\n")
    return (
        f"Write exactly {count} {difficulty} multiple-select (\"select all that apply\") revision questions about "
        f"\"{scope_name}\", using ONLY the excerpts below. Every correct option must be stated in the excerpts; "
        "never add knowledge from outside them.\n"
        'Return JSON only: {"questions":[{"evidence_quotes":["...","..."],"question":"...",'
        '"options":["...","...","...","..."],"correct_answers":["...","..."],"explanation":"..."}]}\n'
        "Rules:\n"
        "- Exactly 4 options. 2 or 3 of them are correct and at least 1 is wrong.\n"
        "- correct_answers: the text of each correct option, copied exactly as written in options.\n"
        "- evidence_quotes: for EACH correct option, in the same order as correct_answers, one sentence (about "
        "8-30 words) copied exactly as written from an excerpt that states it. Do not paraphrase.\n"
        "- The wrong options must be plausible and about the same subject, but NOT stated as true by the excerpts.\n"
        "- The 4 options must have clearly different meanings; never write the same option twice in other words.\n"
        "- Ask positively, e.g. \"Which statements about X are correct?\". No negative questions: the question must "
        "not contain NOT, EXCEPT, incorrect or false (or their equivalent in the excerpts' language).\n"
        "- No 'all/none of the above' options. Each question tests different facts.\n"
        "- Write in the main language of the excerpts. Question <=25 words, each option <=15 words, "
        "explanation <=30 words. No markdown, no extra fields.\n"
        f"{_DIFFICULTY_CONTRACTS[difficulty]}\n"
        f"{avoid}"
        f"EXCERPTS:\n{excerpts}"
    )


def _statement_supported_in_context(option: str, units: list[dict]) -> bool:
    """Could this wrong option be a TRUE statement of the material? Checked against every excerpt
    the call was shown (and neighbouring pairs, like locate_evidence), not only the cited quotes.

    Only a statement-like option is judged: one with at least QUIZ_MIN_ANSWER_MATCHES distinctive
    words (_distinctive_tokens). It counts as supported when it is written in an excerpt, or when
    QUIZ_MULTI_SELECT_DISTRACTOR_SUPPORT of its distinctive words -- matched like answers are
    (_token_supported: glued text, accents, inflections) -- occur together in one SENTENCE (the
    unit a statement is asserted in). A whole excerpt is too wide: a lecture passage names most of
    its subject's terms, so every plausible same-subject distractor (exactly what the prompt asks
    for) would be refused. A distractor recombining words from different sentences is accepted;
    text without sentence punctuation stays one sentence, i.e. is judged as a whole (conservative)."""
    # Short words count here (stopwords never do): the subject of a statement is often a short
    # acronym (CPU, FCFS, SJF), and losing it would let a swapped-subject option match the other
    # subject's sentence. Extra words can only make an option look MORE supported per sentence.
    tokens = _distinctive_tokens(option, units, QUIZ_MULTI_SELECT_DISTRACTOR_TOKEN_MIN_CHARS)
    if len(tokens) < QUIZ_MIN_ANSWER_MATCHES:
        return False
    option_squashed = squash(option)
    for context in [*units, *_adjacent_pairs(units)]:
        if len(option_squashed) >= QUIZ_MULTI_SELECT_DISTRACTOR_MIN_CHARS and option_squashed in context["_squashed"]:
            return True
    for unit in units:
        for sentence in _SENTENCE_BREAK.split(unit["evidence_excerpt"]):
            sentence_squashed = squash(sentence)
            if not sentence_squashed:
                continue
            found = sum(_token_supported(token, sentence_squashed) for token in tokens)
            if found >= QUIZ_MIN_ANSWER_MATCHES and found / len(tokens) >= QUIZ_MULTI_SELECT_DISTRACTOR_SUPPORT:
                return True
    return False


# Sentence ends (followed by whitespace, so decimals and glued text never split), line breaks and
# bullet glyphs: where one asserted statement ends and the next begins.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;。！？])\s+|\n+|\s*[•▪●◦]\s*")


def _option_key(option: str) -> str:
    return _normalize(_clean_inline(_LABEL.sub("", str(option or "")))).strip(" .")


def validate_multiple_select_candidate(
    raw,
    units: list[dict],
    accepted: list[dict],
    difficulty: str,
    question_id: int,
    scope_topic: dict,
    generation_index: int,
) -> tuple[dict, list[str]]:
    """Validate one multiple_select candidate against the excerpts the model was shown and every
    question accepted so far. Hard failures raise CandidateRejected; nothing is repaired, and a
    malformed candidate is never turned into a single_choice question."""
    if not isinstance(raw, dict):
        raise CandidateRejected("structure", "Question must be a JSON object.")
    declared_type = raw.get("question_type")
    if declared_type is not None and declared_type not in (MULTI_SELECT_QUESTION_TYPE, "multiple_select"):
        raise CandidateRejected("structure", "A multiple_select candidate must not declare another question type.", "question_type")

    stem = _clean_inline(raw.get("question"))
    if len(stem) < QUIZ_MIN_STEM_CHARS or len(stem) > QUIZ_MAX_STEM_CHARS or any(p.search(stem) for p in _GENERIC_STEM):
        raise CandidateRejected("structure", "Question stem is empty, too short/long, or generic.", "stem")
    if _SCAFFOLDING.search(stem):
        raise CandidateRejected("structure", "Question contains generation scaffolding.", "scaffolding")
    if _NEGATIVE_EMPHASIS.search(stem) or _NEGATIVE_PHRASES.search(stem):
        raise CandidateRejected(
            "structure", "Negative questions (NOT / EXCEPT / incorrect / false) cannot be verified against the material.",
            "negative_polarity",
        )

    # Exactly four: the persisted shape is letters A-D (quiz_options, attempt grading, Quiz Player).
    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or len(raw_options) != len(OPTION_LETTERS) or not all(isinstance(o, str) for o in raw_options):
        raise CandidateRejected("structure", "A multiple_select question must contain exactly 4 options.", "options")
    options = [_clean_inline(_LABEL.sub("", option)) for option in raw_options]
    if any(not option for option in options):
        raise CandidateRejected("structure", "Question has an empty option.", "options")
    for option in options:
        if (
            _normalize(option).strip(" .") in _GENERIC_OPTIONS or len(option) > QUIZ_MAX_OPTION_CHARS
            or _SCAFFOLDING.search(option)
        ):
            raise CandidateRejected("structure", "Option is generic, scaffolding, or copied raw text.", "options")
    option_keys = [_option_key(option) for option in options]
    if len(set(option_keys)) != len(options) or any(
        difflib.SequenceMatcher(None, option_keys[a], option_keys[b]).ratio() >= QUIZ_OPTION_DUPLICATE_RATIO
        for a in range(len(options)) for b in range(a + 1, len(options))
    ):
        raise CandidateRejected("structure", "Options are not distinct.", "duplicate_options")
    if _options_too_similar(options):
        raise CandidateRejected("structure", "Two options say the same thing in different words.", "duplicate_options")

    raw_correct = raw.get("correct_answers")
    if not isinstance(raw_correct, list) or not all(isinstance(value, str) and value.strip() for value in raw_correct):
        raise CandidateRejected("structure", "correct_answers must be a list of option texts.", "correct_answers")
    correct_keys = [_option_key(value) for value in raw_correct]
    if len(set(correct_keys)) != len(correct_keys):
        raise CandidateRejected("structure", "correct_answers contains the same answer twice.", "duplicate_correct")
    if len(correct_keys) < QUIZ_MULTI_SELECT_MIN_CORRECT or len(correct_keys) >= len(options):
        raise CandidateRejected(
            "structure", "A multiple_select question needs at least 2 correct options and at least 1 wrong one.",
            "correct_count",
        )
    correct_indices = []
    for key in correct_keys:
        if key not in option_keys:
            raise CandidateRejected("structure", "A correct answer is not one of the options.", "correct_not_in_options")
        correct_indices.append(option_keys.index(key))

    raw_quotes = raw.get("evidence_quotes")
    if (not isinstance(raw_quotes, list) or len(raw_quotes) != len(correct_indices)
            or not all(isinstance(quote, str) for quote in raw_quotes)):
        raise CandidateRejected("structure", "evidence_quotes must hold one quote per correct answer.", "evidence_quotes")

    # Every correct statement must be stated in the excerpt its own quote is found in.
    located_units, alignments, links, spans = [], [], [], []
    quotes = [_clean_inline(quote) for quote in raw_quotes]
    for option_index, quote in zip(correct_indices, quotes):
        located = locate_evidence(quote, units)
        if located is None:
            code = "quote_too_short" if len(squash(quote)) < QUIZ_MIN_QUOTE_CHARS else "quote_not_found"
            raise CandidateRejected("grounding", "An evidence quote was not found in the provided context.", code)
        unit, alignment = located
        option = options[option_index]
        link, _ = context_support(stem, option, unit, units)
        if link is not None and link < QUIZ_MIN_LINK_SUPPORT:
            raise CandidateRejected("grounding", "The question and a correct answer are not supported by the context.", "question_not_supported")
        _, stated = context_support(stem, option, unit, [unit])
        option_squashed = squash(option)
        if stated is None:
            supported = len(option_squashed) >= 4 and option_squashed in unit["_squashed"]
        else:
            supported = stated >= QUIZ_MIN_ANSWER_ANCHOR
        if not supported:
            raise CandidateRejected("grounding", "A correct answer is not stated in its evidence.", "answer_not_in_context")
        located_units.append(unit)
        alignments.append(alignment)
        links.append(link)
        spans.extend(evidence_spans(quote, unit))

    # A wrong option written word for word in the evidence of the correct ones would also be true:
    # the question would be ambiguous.
    quotes_squashed = [squash(quote) for quote in quotes]
    for index, option in enumerate(options):
        if index in correct_indices:
            continue
        option_squashed = squash(option)
        if len(option_squashed) >= QUIZ_MULTI_SELECT_DISTRACTOR_MIN_CHARS and any(option_squashed in quote for quote in quotes_squashed):
            raise CandidateRejected("grounding", "A wrong option is stated by the evidence of the correct ones.", "ambiguous_distractor")
        if _statement_supported_in_context(option, units):
            raise CandidateRejected("grounding", "A wrong option is supported by the provided context.", "supported_distractor")

    stem_key = squash(stem)
    correct_options = [options[index] for index in correct_indices]
    signature = content_tokens(f"{stem} {' '.join(correct_options)}")
    for existing in accepted:
        existing_key = squash(existing["question"])
        if stem_key == existing_key or difflib.SequenceMatcher(None, stem_key, existing_key).ratio() >= QUIZ_STEM_DUPLICATE_RATIO:
            raise CandidateRejected("duplicate", "Question duplicates an accepted question.", "duplicate_stem")
        meta = existing["_meta"]
        if signature and _jaccard(signature, meta["signature"]) >= QUIZ_CONTENT_DUPLICATE_JACCARD:
            raise CandidateRejected("duplicate", "Question tests the same facts as an accepted question.", "duplicate_content")
        for option, quote, unit in zip(correct_options, quotes, located_units):
            if (_evidence_overlap(evidence_spans(quote, unit), meta.get("spans", ())) >= QUIZ_EVIDENCE_OVERLAP_MAX
                    and _same_target(content_tokens(option), squash(option), meta["answer_tokens"], meta["answer_key"])):
                raise CandidateRejected("duplicate", "A correct answer repeats a fact an accepted question tests on the same evidence.", "duplicate_evidence")

    explanation = _clean_inline(raw.get("explanation"))
    warnings: list[str] = []
    if min(alignments) < 1.0:
        warnings.append("quote_not_verbatim")
    if any(link is None for link in links):
        warnings.append("unverifiable_relevance")
    elif min(links) < QUIZ_WEAK_LINK_SUPPORT:
        warnings.append("weak_relevance")
    if _SCAFFOLDING.search(explanation):
        warnings.append("explanation_scaffolding")
    if len(explanation.split()) < 3:
        warnings.append("short_explanation")

    first_unit = min(located_units, key=lambda unit: unit["index"])
    letters = sorted(OPTION_LETTERS[index] for index in correct_indices)
    known_links = [link for link in links if link is not None]
    normalized = {
        "id": question_id,
        "question": stem,
        "options": [f"{OPTION_LETTERS[i]}. {option}" for i, option in enumerate(options)],
        "correct_answer": letters[0],
        "question_type": MULTI_SELECT_QUESTION_TYPE,
        "correct_answers": letters,
        "topic_id": str(scope_topic["topic_id"]),
        "topic_name": str(scope_topic.get("name") or scope_topic["topic_id"]),
        "concept_id": first_unit["unit_id"],
        "concept_name": first_unit["name"],
        "source_subtopic_ids": [],
        "concept_origin": "study_unit",
        "concept_plan_id": QUIZ_ENGINE_VERSION,
        "assessment_capacity": len(units),
        "difficulty": difficulty,
        "explanation": explanation,
        "source_chunk_ids": list(dict.fromkeys(chunk_id for unit in located_units for chunk_id in unit["source_chunk_ids"])),
        "validation_outcome": "accepted_quality_warning" if warnings else "accepted",
        "_meta": {
            "index": generation_index, "unit_index": first_unit["index"], "signature": signature,
            "quote_shingles": _shingles(squash(" ".join(quotes))), "quote": " | ".join(quotes),
            "alignment": min(alignments), "link": min(known_links) if known_links else None,
            "answer_in_evidence": True, "spans": spans,
            "answer_tokens": content_tokens(" ".join(correct_options)), "answer_key": squash(" ".join(correct_options)),
        },
    }
    return normalized, sorted(set(warnings))


# ---------------------------------------------------------------------------------------------
# Fill-in-the-blank, derived from the document's persisted flashcards (no LLM call)
# ---------------------------------------------------------------------------------------------
# A Normal Quiz fill_blank question is a cloze built deterministically from ONE usable flashcard:
# the card's own declarative sentence with ONE short key span replaced by a blank; the removed span
# is the only accepted answer. Nothing is invented (no synonyms, no outside facts) and the document
# is not re-read: the flashcard -- itself generated from the document's chunks, whose ids it keeps --
# is the source. A card that yields no safe, unambiguous cloze is skipped; the quiz then keeps
# single_choice questions instead. Grading is deterministic (normalize_fill_blank_answer).

FILL_BLANK_MARKER = "____"
QUIZ_FILL_BLANK_MAX_ANSWER_WORDS = 4
QUIZ_FILL_BLANK_MAX_ANSWER_CHARS = 40
QUIZ_FILL_BLANK_MIN_SENTENCE_WORDS = 6   # the card sentence must stand alone
QUIZ_FILL_BLANK_MIN_CONTEXT_TOKENS = 3   # meaningful words left around the blank
QUIZ_FILL_BLANK_MIN_SHARED_WITH_FRONT = 2   # a question card's back must restate its subject
FLASHCARD_CLOZE_ORIGIN = "flashcard_cloze"
_BLANK_RUN = re.compile(r"_{3,}")
_SURROUNDING_PUNCTUATION = re.compile(r"^[\s.,;:!?\"'`“”‘’«»()\[\]{}<>…]+|[\s.,;:!?\"'`“”‘’«»()\[\]{}<>…]+$")
_CLOZE_WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", flags=re.UNICODE)
_CLOZE_RUN_BREAK = re.compile(r"[,;:()\[\]\"“”]")
_CLOZE_LIST_BEFORE = re.compile(r"(?:,|\band|\bor)\s*$", flags=re.IGNORECASE)
_CLOZE_LIST_AFTER = re.compile(r"^\s*(?:,|and\b|or\b)", flags=re.IGNORECASE)


def fill_blank_target(question_count: int) -> int:
    """How many fill_blank questions a quiz aims for: 12/15 -> 2, 18/20 -> 3. A target only: the
    quiz keeps single_choice questions when fewer flashcards yield a valid cloze."""
    return max(0, question_count // 6)


def normalize_fill_blank_answer(value) -> str:
    """The ONLY comparison used to grade a fill_blank answer: Unicode-normalized, casefolded,
    inner whitespace collapsed and surrounding punctuation/quotes/brackets removed."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return _SURROUNDING_PUNCTUATION.sub("", text).strip()


def fill_blank_is_correct(selected, correct_answers: list[str]) -> bool:
    answer = normalize_fill_blank_answer(selected)
    return bool(answer) and answer in {normalize_fill_blank_answer(value) for value in correct_answers}


def _fill_blank_answer_shape_ok(answer: str) -> bool:
    words = answer.split()
    return (
        0 < len(words) <= QUIZ_FILL_BLANK_MAX_ANSWER_WORDS
        and len(answer) <= QUIZ_FILL_BLANK_MAX_ANSWER_CHARS
        and bool(re.search(r"\w", answer))
        and not _BLANK_RUN.search(answer)
    )


def _cloze_key(word: str) -> str:
    return squash(word)


def _cloze_minor(word: str) -> bool:
    """Function words and very short words: never an answer on their own."""
    key = _cloze_key(word)
    return len(key) <= 2 or _normalize(word) in _STOPWORDS


def _cloze_same_word(left: str, right: str) -> bool:
    """The same word, allowing a short inflection (use/uses, process/processes)."""
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 3 and longer.startswith(shorter) and len(longer) - len(shorter) <= 3


def _cloze_is_question(front: str) -> bool:
    return "?" in front or bool(_HINT_QUESTION_START.match(front))


def _cloze_span_from_question(front: str, back: str, words: list[re.Match]) -> tuple[int, int]:
    """Question card: the back restates the question and adds the answer. The answer span is the
    ONE contiguous run of back words that the front does not contain (function words may sit inside
    the run, never at its edges). No such run, or more than one, means no safe single blank."""
    front_keys = [_cloze_key(word) for word in _CLOZE_WORD.findall(front)]
    in_front = [any(_cloze_same_word(_cloze_key(match.group()), key) for key in front_keys) for match in words]
    shared = sum(1 for match, known in zip(words, in_front) if known and not _cloze_minor(match.group()))
    if shared < QUIZ_FILL_BLANK_MIN_SHARED_WITH_FRONT:
        raise CandidateRejected("structure", "The flashcard back does not restate its question.", "not_standalone")
    new = [index for index, match in enumerate(words) if not in_front[index] and not _cloze_minor(match.group())]
    if not new:
        raise CandidateRejected("structure", "The flashcard back adds no answer to its question.", "no_answer_span")
    runs = [[new[0]]]
    for index in new[1:]:
        previous = runs[-1][-1]
        between = words[previous + 1:index]
        gap_text = back[words[previous].end():words[index].start()]
        if all(not in_front[previous + 1 + offset] and _cloze_minor(match.group()) for offset, match in enumerate(between)) \
                and not _CLOZE_RUN_BREAK.search(gap_text):
            runs[-1].append(index)
        else:
            runs.append([index])
    if len(runs) != 1:
        raise CandidateRejected("structure", "More than one part of the flashcard answer could be the blank.", "ambiguous_span")
    return words[runs[0][0]].start(), words[runs[0][-1]].end()


def _cloze_span_from_term(front: str, back: str) -> tuple[int, int]:
    """Term card: the front names a term and the back is a sentence about it. The term, written
    exactly once in the back, is the blank."""
    term = _HINT_ARTICLE.sub("", _SURROUNDING_PUNCTUATION.sub("", front))
    if len(squash(term)) < 3 or not _fill_blank_answer_shape_ok(term):
        raise CandidateRejected("structure", "The flashcard front is neither a question nor a short term.", "front_shape")
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in term.split()) + r"(?!\w)", re.IGNORECASE)
    matches = list(pattern.finditer(back))
    if len(matches) != 1:
        raise CandidateRejected("structure", "The term is not written exactly once in the flashcard sentence.", "term_not_once")
    return matches[0].start(), matches[0].end()


def flashcard_cloze(card: dict) -> dict:
    """The deterministic cloze of one flashcard, or CandidateRejected (with a code) when the card
    has no safe, unambiguous single blank. Uses only the card's own front/back text.

    1. The back must be ONE declarative sentence that stands alone (no question mark, at least
       QUIZ_FILL_BLANK_MIN_SENTENCE_WORDS words, not over QUIZ_MAX_STEM_CHARS).
    2. The span: for a question front, the single run of back words the question does not contain
       (_cloze_span_from_question); for a short-term front, that term inside the back
       (_cloze_span_from_term).
    3. The span is a short answer (1-4 words, <= 40 characters), not only function words, written
       once (never a giveaway), not an item of a list ("X, ____ and Y"), and enough meaningful words
       remain around the blank.
    """
    front = _clean_inline(card.get("front"))
    back = _clean_inline(card.get("back"))
    if not front or not back:
        raise CandidateRejected("structure", "The flashcard is empty.", "empty_card")
    words = list(_CLOZE_WORD.finditer(back))
    if ("?" in back or len(words) < QUIZ_FILL_BLANK_MIN_SENTENCE_WORDS or len(back) > QUIZ_MAX_STEM_CHARS
            or _BLANK_RUN.search(back) or _SCAFFOLDING.search(back)):
        raise CandidateRejected("structure", "The flashcard back is not a standalone sentence.", "not_a_sentence")
    if _cloze_is_question(front):
        start, end = _cloze_span_from_question(front, back, words)
        rule = "question_answer"
    else:
        start, end = _cloze_span_from_term(front, back)
        rule = "term_in_definition"
    answer = _SURROUNDING_PUNCTUATION.sub("", back[start:end])
    if not _fill_blank_answer_shape_ok(answer):
        raise CandidateRejected("structure", "The blank would remove more than a short term.", "answer_too_long")
    if all(_cloze_minor(word) for word in _CLOZE_WORD.findall(answer)):
        raise CandidateRejected("structure", "The blank would remove only filler words.", "trivial_answer")
    before, after = back[:start], back[end:]
    if _CLOZE_LIST_BEFORE.search(before) or _CLOZE_LIST_AFTER.search(after):
        raise CandidateRejected("structure", "The blank is one item of a list; other items would fit too.", "list_ambiguity")
    rest = f"{before} {after}"
    if squash(answer) in squash(rest):
        raise CandidateRejected("structure", "The sentence already contains its answer.", "fill_blank_giveaway")
    if len(content_tokens(rest)) < QUIZ_FILL_BLANK_MIN_CONTEXT_TOKENS:
        raise CandidateRejected("structure", "Too little is left around the blank.", "too_little_context")
    question = _clean_inline(f"{before.rstrip()} {FILL_BLANK_MARKER} {after.lstrip()}").replace(f"{FILL_BLANK_MARKER} .", f"{FILL_BLANK_MARKER}.")
    question = re.sub(rf"{re.escape(FILL_BLANK_MARKER)}\s+([,.;:!])", rf"{FILL_BLANK_MARKER}\1", question)
    return {"question": question, "answer": answer, "sentence": back, "rule": rule}


def build_flashcard_fill_blank(
    card: dict,
    units: list[dict],
    accepted: list[dict],
    difficulty: str,
    scope_topic: dict,
    generation_index: int,
) -> dict:
    """One Normal Quiz fill_blank question from one flashcard, or CandidateRejected.

    Beyond flashcard_cloze: the card must belong to this quiz's material (one of its source chunks
    is among the quiz's excerpts; a card without source chunks only for a whole-document quiz), and
    the cloze must not test the same fact as a question already in the pool (single_choice,
    multi_select or an earlier cloze)."""
    cloze = flashcard_cloze(card)
    source_ids = [str(value) for value in card.get("source_chunk_ids") or [] if str(value)]
    unit = next((unit for unit in units if set(unit["source_chunk_ids"]) & set(source_ids)), None)
    if (source_ids and unit is None) or (not source_ids and str(scope_topic["topic_id"]) != "document"):
        raise CandidateRejected("grounding", "The flashcard is not about this quiz's material.", "out_of_scope")

    stem_key = squash(cloze["question"])
    answer_tokens, answer_key = content_tokens(cloze["answer"]), squash(cloze["answer"])
    signature = content_tokens(f"{cloze['question']} {cloze['answer']}")
    for existing in accepted:
        meta = existing["_meta"]
        if (difflib.SequenceMatcher(None, stem_key, squash(existing["question"])).ratio() >= QUIZ_STEM_DUPLICATE_RATIO
                or (signature and _jaccard(signature, meta["signature"]) >= QUIZ_CONTENT_DUPLICATE_JACCARD)
                or (_same_target(answer_tokens, answer_key, meta["answer_tokens"], meta["answer_key"])
                    and _jaccard(signature, meta["signature"]) >= QUIZ_SAME_TARGET_JACCARD / 2)):
            raise CandidateRejected("duplicate", "The cloze tests the same fact as a question already in the quiz.", "duplicate")

    return {
        "id": 0,
        "question": cloze["question"],
        "options": [],
        "correct_answer": cloze["answer"],
        "question_type": "fill_blank",
        # Exactly the removed span: no synonyms or alternatives are invented.
        "correct_answers": [cloze["answer"]],
        "topic_id": str(scope_topic["topic_id"]),
        "topic_name": str(scope_topic.get("name") or scope_topic["topic_id"]),
        "concept_id": unit["unit_id"] if unit else str(card.get("flashcard_id") or ""),
        "concept_name": unit["name"] if unit else str(card.get("subtopic_name") or card.get("topic_name") or ""),
        "source_subtopic_ids": [card["subtopic_id"]] if card.get("subtopic_id") else [],
        "concept_origin": FLASHCARD_CLOZE_ORIGIN,
        "concept_plan_id": QUIZ_ENGINE_VERSION,
        "assessment_capacity": len(units),
        "difficulty": difficulty,
        "explanation": f"From your flashcard: {cloze['sentence']}",
        "source_chunk_ids": source_ids,
        "validation_outcome": "accepted",
        "_meta": {
            "index": generation_index, "unit_index": unit["index"] if unit else len(units),
            "signature": signature, "quote_shingles": _shingles(squash(cloze["sentence"])),
            "quote": cloze["sentence"], "alignment": 1.0, "link": None, "answer_in_evidence": True, "spans": (),
            "answer_tokens": answer_tokens, "answer_key": answer_key,
            "flashcard_id": str(card.get("flashcard_id") or ""), "cloze_rule": cloze["rule"],
        },
    }


def select_flashcard_fill_blanks(
    cards: list[dict],
    units: list[dict],
    accepted: list[dict],
    difficulty: str,
    scope_topic: dict,
    target: int,
    first_index: int,
) -> tuple[list[dict], dict]:
    """Up to `target` fill_blank questions from the flashcards, in deck order (deterministic). A
    card that yields no valid cloze is skipped and counted under its rejection code."""
    pool: list[dict] = []
    info = {"source": "flashcards", "target": target, "available_flashcards": len(cards),
            "accepted": 0, "rejected": 0, "rejected_by": {}, "flashcard_ids": []}
    for offset, card in enumerate(cards):
        if len(pool) >= target:
            break
        try:
            question = build_flashcard_fill_blank(card, units, accepted + pool, difficulty, scope_topic, first_index + offset + 1)
        except CandidateRejected as error:
            info["rejected"] += 1
            info["rejected_by"][error.code] = info["rejected_by"].get(error.code, 0) + 1
            continue
        pool.append(question)
        info["flashcard_ids"].append(question["_meta"]["flashcard_id"])
    info["accepted"] = len(pool)
    return pool, info


# Flashcard terms (compact concept/answer terms of a document's flashcards). Used by the admin Quiz
# model benchmark to fingerprint its flashcard input; Normal Quiz fill_blank questions come from
# select_flashcard_fill_blanks above.
_HINT_QUESTION_START = re.compile(
    r"^(?:what|which|who|whom|whose|why|how|when|where|define|describe|explain|name|list|give)\b", re.IGNORECASE,
)
_HINT_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def flashcard_hint_terms(cards: list[dict]) -> list[str]:
    """Compact concept/answer terms (1-4 words) from flashcard fronts/backs, in card order.
    Questions, sentences and long answers are skipped: they are not a short, objective target."""
    terms, seen = [], set()
    for card in cards or []:
        for side in (card.get("front"), card.get("back")):
            text = _clean_inline(side)
            if not text or "?" in text or _HINT_QUESTION_START.match(text):
                continue
            term = _HINT_ARTICLE.sub("", _SURROUNDING_PUNCTUATION.sub("", text))
            key = squash(term)
            if len(key) < 3 or key in seen or not _fill_blank_answer_shape_ok(term):
                continue
            seen.add(key)
            terms.append(term)
    return terms
