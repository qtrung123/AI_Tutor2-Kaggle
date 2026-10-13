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
                          duplicate only when it asks essentially the same thing -- one fact may be asked
                          about again for a different aspect; bad distractors and too-short quotes are
                          repaired locally by repair_options / repair_evidence, without an LLM call)
* followup_units        -- what a follow-up call is shown: unused text preferred, used text only if needed
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

# v9: single_choice ONLY (no flashcard fill_blank in the Normal Quiz) and one target language per
# quiz -- bumped so cached v8 quizzes (mixed types / mixed languages) are never silently reused.
# v10: local option/evidence repair, aspect-aware duplicates, at most 3 calls -- cached v9 quizzes
# are regenerated rather than served as current.
QUIZ_ENGINE_VERSION = "simple_context_v10"
QUIZ_PROMPT_VERSION = "simple_context_v10_single_choice_repair"

# --- how many questions / candidates ------------------------------------------------------------
QUIZ_ALLOWED_QUESTION_COUNTS = (12, 15, 18, 20)
QUIZ_CANDIDATE_BUFFER_RATIO = 1 / 6   # 12 -> 14, 15 -> 18, 18 -> 21, 20 -> 24
# Bad distractors and too-short quotes are repaired locally (no LLM call), so a small surplus is
# enough. At most 3 calls for every count: call 2 asks for what is missing + 2, call 3 for what is
# missing + 1, and there is never a 4th call.
QUIZ_MAX_CALLS = 3
QUIZ_FOLLOWUP_EXTRA = {2: 2, 3: 1}    # call number -> surplus on top of the missing questions
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
QUIZ_STEM_PREFIX_CHARS = 5            # stems differing only by inflection ("array"/"arrays") are one question
QUIZ_ASPECT_MAX_OVERLAP = 0.2         # same answer, same statement: stems sharing at most this much meaning ask different aspects

# --- local repair (no LLM call) -------------------------------------------------------------------
QUIZ_REPAIR_MIN_OPTIONS = 3           # fewer options than this is not a 4-option question to repair
QUIZ_MAX_RAW_OPTIONS = 6              # ... more than this is not one either (5-6 are cut to 4)
QUIZ_REPAIR_MIN_ANCHOR_CHARS = 6      # a too-short quote is expanded only when this much of it is found verbatim
QUIZ_REPAIR_NOT_FOUND_SHARE = 0.5     # a quote not found is repaired when one verbatim run covers this share of it
QUIZ_REPAIR_MAX_QUOTE_CHARS = 300     # a repaired quote longer than this is cut to the words around the anchor
QUIZ_REPAIR_WINDOW_CHARS = 40         # ... squashed characters the word window grows to

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
    """How many candidates the first call writes: the target plus a small surplus (12 -> 14)."""
    return question_count + math.ceil(round(QUIZ_CANDIDATE_BUFFER_RATIO * question_count, 6))


def max_llm_calls(question_count: int) -> int:
    """Upper bound on model calls for one quiz: 3 for every count."""
    return QUIZ_MAX_CALLS


def followup_request(missing: int, call_number: int = 2) -> int:
    """How many candidates a follow-up call writes: call 2 asks for what is missing + 2, call 3 for
    what is missing + 1."""
    return max(1, missing) + QUIZ_FOLLOWUP_EXTRA.get(call_number, 1)


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


# ---------------------------------------------------------------------------------------------
# Quiz language: ONE target language per quiz -- the document's primary/source language, chosen
# deterministically before any model call (detect_quiz_language), then enforced per question.
# ---------------------------------------------------------------------------------------------

QUIZ_LANGUAGES = ("english", "vietnamese")
QUIZ_DEFAULT_LANGUAGE = "english"
# Letters that plain English text never uses; Vietnamese prose is full of them.
_VIETNAMESE_LETTERS = re.compile(r"[ăâđêôơưàáãèéìíòóõùúýĂÂĐÊÔƠƯÀÁÃÈÉÌÍÒÓÕÙÚÝẠ-ỹ]")
_LETTER_WORD = re.compile(r"[^\W\d_]{2,}", flags=re.UNICODE)
QUIZ_IDENTIFIER_MAX_CHARS = 15          # a capitalised token up to this long is an identifier, not prose
QUIZ_SEGMENT_MIN_LETTERS = 12           # a source sentence needs this many natural letters to vote
QUIZ_LABEL_MIN_LETTERS = 6              # ... a heading / topic name this many
QUIZ_SEGMENT_VIETNAMESE_LETTER_SHARE = 0.05   # share of Vietnamese-specific letters: clearly Vietnamese
QUIZ_SEGMENT_ENGLISH_LETTER_SHARE = 0.01      # ... at most this share: clearly English
QUIZ_STRUCTURE_MIN_LABELS = 2
QUIZ_STRUCTURE_WIN_SHARE = 2 / 3
QUIZ_BILINGUAL_MIN_PAIRS = 3
QUIZ_BILINGUAL_WIN_SHARE = 0.6
_SEGMENT_SPLIT = re.compile(r"(?<=[.!?])|→")
QUIZ_LANGUAGE_FIELD_MIN_WORDS = 4       # a question/explanation needs this many to be judged on its own
QUIZ_LANGUAGE_TEXT_MIN_WORDS = 6        # the whole question (stem + options + explanation)
QUIZ_VIETNAMESE_SHARE = 0.3             # share of natural words with Vietnamese letters: clearly Vietnamese
QUIZ_ENGLISH_MAX_SHARE = 0.1            # ... at most this share: clearly English
QUIZ_VIETNAMESE_MIN_SHARE = 0.2         # a Vietnamese question below this share is (mostly) another language
QUIZ_ENGLISH_MAX_VI_WORDS = 1           # an English question may carry one stray accented word (a name)

_LANGUAGE_DIRECTIVES = {
    "english": "Write ALL question text, answer options and explanations in English only.",
    "vietnamese": (
        "Write ALL question text, answer options and explanations in Vietnamese only. "
        "Preserve technical identifiers such as eCos, TinyOS, HAL, FIFO exactly."
    ),
}


def _natural_words(text: str) -> list[str]:
    """Words that carry a language signal. Acronyms and identifiers (HAL, FIFO, eCos, TinyOS) do not:
    a word in capitals or with a capital after its first letter is a technical name, not prose."""
    words = _LETTER_WORD.findall(unicodedata.normalize("NFC", str(text or "")))
    return [word for word in words if not any(char.isupper() for char in word[1:])]


def vietnamese_share(text: str) -> tuple[int, int]:
    """(natural words, natural words containing a Vietnamese-specific letter)."""
    words = _natural_words(text)
    return len(words), sum(1 for word in words if _VIETNAMESE_LETTERS.search(word))


def _is_identifier(token: str) -> bool:
    """An acronym or technical name (HAL, FIFO, eCos, TinyOS): no language evidence. A LONG token
    with capitals is glued PDF prose ("TheeCoskernelwasdesigned"), so it still counts."""
    return len(token) <= QUIZ_IDENTIFIER_MAX_CHARS and (token.isupper() or any(char.isupper() for char in token[1:]))


def text_language(text: str, min_letters: int = QUIZ_SEGMENT_MIN_LETTERS) -> str | None:
    """"english", "vietnamese" or None (too little natural text, or mixed) for one short text.

    Measured on LETTERS, not words, so glued PDF text (no spaces) is judged as well as spaced text:
    Vietnamese prose has many Vietnamese-specific letters, English none. Identifiers are ignored."""
    tokens = [token for token in _LETTER_WORD.findall(unicodedata.normalize("NFC", str(text or ""))) if not _is_identifier(token)]
    letters = sum(len(token) for token in tokens)
    if letters < min_letters:
        return None
    share = sum(len(_VIETNAMESE_LETTERS.findall(token)) for token in tokens) / letters
    if share >= QUIZ_SEGMENT_VIETNAMESE_LETTER_SHARE:
        return "vietnamese"
    if share <= QUIZ_SEGMENT_ENGLISH_LETTER_SHARE:
        return "english"
    return None


def _line_segments(line: str) -> list[dict]:
    """One line cut into sentence-sized segments: after . ! ? (even with no space after it, as in
    glued PDF text) and at an arrow. `body` marks a sentence (ends with . ! ?); a heading-like piece
    without one is not body text."""
    segments = []
    for piece in _SEGMENT_SPLIT.split(line):
        piece = piece.strip(" \t○●•▪◦■□◆◇▶►➢✓✔-")
        if piece:
            segments.append({"text": piece, "body": piece.endswith((".", "!", "?")), "language": text_language(piece)})
    return segments


def source_segments(texts: list[str]) -> list[dict]:
    """The source text in ORIGINAL order as segments (see _line_segments)."""
    return [segment for text in texts for line in str(text or "").splitlines() for segment in _line_segments(line)]


def _pair_runs(languages: list[str], pairs: Counter) -> None:
    """Pair a sequence of sentence languages: consecutive sentences in the same language are ONE
    run first (a translation may take two sentences; an untranslated sentence joins its neighbour),
    then the runs are paired two by two from the start: (source run, translation run)."""
    runs = [language for index, language in enumerate(languages) if index == 0 or language != languages[index - 1]]
    for index in range(0, len(runs) - 1, 2):
        pairs[(runs[index], runs[index + 1])] += 1


def bilingual_pairs(texts: list[str]) -> Counter:
    """(source language, translation language) pairs, in original order.

    * A line holding sentences in both languages is paired on its own ("English sentence.Vietnamese
      translation." on one PDF line).
    * Lines holding ONE sentence form a block that is paired as a whole; a block ends at every
      heading-like line and blank line, so an anomaly cannot shift the pairing of a whole document.
    Across a block, alternating EN/VI sentences look alike in both directions; what tells the source
    from the translation is which language STARTS each line / block (source first, then translation)."""
    pairs = Counter()
    block: list[str] = []

    def close_block():
        _pair_runs(block, pairs)
        block.clear()

    for text in texts:
        for line in str(text or "").splitlines():
            segments = _line_segments(line)
            languages = [segment["language"] for segment in segments if segment["body"] and segment["language"]]
            if not segments or any(not segment["body"] and segment["language"] for segment in segments):
                close_block()                                # blank line or heading: restart
            if len(set(languages)) == 2:
                close_block()
                _pair_runs(languages, pairs)
            else:
                block.extend(languages)
    close_block()
    return pairs


def _clear_winner(first: int, second: int, minimum: int, share: float) -> str | None:
    total = first + second
    if max(first, second) >= minimum and total and max(first, second) / total >= share:
        return "english" if first > second else "vietnamese"
    return None


def detect_quiz_language(texts: list[str], structure_labels: list[str] | tuple = ()) -> dict:
    """The ONE canonical language of a quiz: the document's PRIMARY/SOURCE language (deterministic,
    before any model call). Returns {"detected_language", "detection_basis", vote counters}.

    1. structural -- the document title, topic and subtopic names: with >= 2 confident labels and a
       clear (>= 2/3) winner, that language. Headings are written in the source language even when
       every paragraph is followed by its translation.
    2. bilingual_order -- body sentences in original order, a sentence directly followed by one in
       the other language is a (source -> translation) pair (bilingual_pairs). With >= 3 pairs and
       >= 60% in one direction: EN -> VI means English is the source, VI -> EN means Vietnamese.
    3. body_fallback -- one vote per confident segment (never weighted by length); a tie goes to the
       first confident segment; no evidence at all -> English.
    Identifiers and acronyms (eCos, TinyOS, HAL, FIFO) are never language evidence."""
    structural = Counter(text_language(label, QUIZ_LABEL_MIN_LETTERS) for label in structure_labels)
    segments = source_segments(texts)
    pairs = bilingual_pairs(texts)
    votes = Counter(segment["language"] for segment in segments if segment["language"])
    report = {
        "structural_en_votes": structural["english"], "structural_vi_votes": structural["vietnamese"],
        "en_to_vi_transitions": pairs[("english", "vietnamese")], "vi_to_en_transitions": pairs[("vietnamese", "english")],
        "body_en_votes": votes["english"], "body_vi_votes": votes["vietnamese"],
    }
    language = _clear_winner(
        structural["english"], structural["vietnamese"], QUIZ_STRUCTURE_MIN_LABELS, QUIZ_STRUCTURE_WIN_SHARE,
    ) if structural["english"] + structural["vietnamese"] >= QUIZ_STRUCTURE_MIN_LABELS else None
    basis = "structural"
    if language is None:
        basis = "bilingual_order"
        language = _clear_winner(report["en_to_vi_transitions"], report["vi_to_en_transitions"],
                                 QUIZ_BILINGUAL_MIN_PAIRS, QUIZ_BILINGUAL_WIN_SHARE)
    if language is None:
        basis = "body_fallback"
        if votes["english"] != votes["vietnamese"]:
            language = "english" if votes["english"] > votes["vietnamese"] else "vietnamese"
        else:
            language = next((segment["language"] for segment in segments if segment["language"]), QUIZ_DEFAULT_LANGUAGE)
    return {"detected_language": language, "detection_basis": basis, **report}


def language_mismatch(stem: str, options: list[str], explanation: str, language: str) -> bool:
    """True when a question is not written in the quiz's target language (or mixes languages).

    Judged on the natural-language text the learner reads -- stem, options and explanation together,
    plus the stem, the explanation and EACH option on its own (a field is judged alone only when it
    has QUIZ_LANGUAGE_FIELD_MIN_WORDS natural words) -- never on the copied evidence quote. Acronyms
    and identifiers are not words here, so "eCos", "TinyOS", "HAL" or "FIFO" never cause a rejection,
    and a short term-like option is neutral."""
    if language not in QUIZ_LANGUAGES:
        return False
    words, vi = vietnamese_share(" ".join([stem, *options, explanation]))
    fields = [vietnamese_share(text) for text in (stem, explanation, *options)]
    if language == "english":
        if vi > QUIZ_ENGLISH_MAX_VI_WORDS and vi / max(words, 1) > QUIZ_ENGLISH_MAX_SHARE:
            return True
        return any(n >= QUIZ_LANGUAGE_FIELD_MIN_WORDS and v / n >= QUIZ_VIETNAMESE_SHARE for n, v in fields)
    if words >= QUIZ_LANGUAGE_TEXT_MIN_WORDS and vi / words < QUIZ_VIETNAMESE_MIN_SHARE:
        return True
    return any(n >= QUIZ_LANGUAGE_FIELD_MIN_WORDS and v / n < QUIZ_ENGLISH_MAX_SHARE for n, v in fields)


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
    language: str = QUIZ_DEFAULT_LANGUAGE,
) -> str:
    """`avoid_stems` / `avoid_quotes` (same order) describe the questions that already exist: a
    follow-up call is shown each stem with the sentence it was written from, so it can pick other
    facts instead of asking the same one again. `language` is the quiz's ONE target language
    (detect_quiz_language), stated in every call -- the first one and every follow-up."""
    directive = _LANGUAGE_DIRECTIVES.get(language, _LANGUAGE_DIRECTIVES[QUIZ_DEFAULT_LANGUAGE])
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
            "Do not repeat them and do not reword them: never ask the same thing again. A listed sentence "
            "may be used again only to ask about a DIFFERENT aspect of it. Questions already written:\n"
            + "\n".join(lines) + "\n"
        )
    spread_rule = (
        "- The questions already written are listed below the rules; prefer sentences and parts of the "
        "excerpts that they did not use. A sentence they used may still hold a different fact or aspect: "
        "ask about it only if none of the listed questions asks that.\n"
        if avoid_stems else
        "- Spread the questions over the excerpts whenever the material allows. The same fact or sentence "
        "may be reused for a DIFFERENT aspect (e.g. for net.Listen(\"tcp\", \":8080\"): the function, the "
        "port, the protocol, the purpose), but never write two questions that ask essentially the same "
        "thing, even reworded.\n"
    )
    return (
        f"Write exactly {count} {difficulty} single_choice revision questions about \"{scope_name}\", "
        "using ONLY the excerpts below. Every fact in a question and in its correct answer must be stated "
        "in the excerpts; never add knowledge from outside them.\n"
        f"LANGUAGE: {directive}\n"
        'Return JSON only: {"questions":[{"evidence_quote":"...","question":"...",'
        '"options":["...","...","...","..."],"answer_index":0,"explanation":"..."}]}\n'
        "Rules:\n"
        "- First choose evidence_quote: one or two consecutive sentences (about 8-30 words) copied exactly "
        "as written from an excerpt. Do not paraphrase, translate, or fix its spelling or spacing.\n"
        "- Then write a question whose correct answer is stated in that quote. Never ask about something "
        "the quote does not say.\n"
        "- single_choice only: exactly 4 options, exactly 1 correct option; answer_index is the 0-based "
        "position (0-3) of the correct option. Vary the position of the correct option.\n"
        "- The 3 wrong options are distinct, plausible distractors that are clearly incorrect. They do NOT "
        "need to appear in the excerpts: related concepts, nearby values, opposite actions or invented "
        "alternatives are fine, as long as none of them is also correct.\n"
        "- The 4 options must have clearly different meanings: never write two options that say the same "
        "thing in different words, and no option may be a shorter or longer version of another.\n"
        f"{spread_rule}"
        "- No 'all/none of the above' options. No negative questions: the question must not contain NOT, EXCEPT, "
        "incorrect or false (or their equivalent in the excerpts' language) -- never ask which option is NOT true; "
        "ask what the excerpts positively state.\n"
        f"- Write ALL {count} questions: do not stop early.\n"
        f"- {directive} Never mix languages. Be concise: question <=20 words, each option <=10 words, "
        "explanation <=20 words. No markdown, no extra fields.\n"
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

def _bags_alike(first_option: str, second_option: str) -> bool:
    """Two options that are one answer written twice, compared as bags of meaningful words:
    identical bags (order, articles and punctuation changed), or -- for options of 3+ words -- bags
    that overlap by QUIZ_OPTION_BAG_JACCARD after cutting every word to its first 4 letters
    (moved/moves, process/processes). Deliberately narrow: an antonym pair ("increases ..." /
    "decreases ...") and nested names ("Preemptive" / "Non-preemptive", "I/O bound" / "CPU bound")
    are good distractors and are NOT flagged."""
    first, second = content_tokens(first_option, 2), content_tokens(second_option, 2)
    if not first or not second:
        return False
    if first == second:
        return True
    if min(len(first), len(second)) >= 3:
        stems_first, stems_second = frozenset(t[:4] for t in first), frozenset(t[:4] for t in second)
        return _jaccard(stems_first, stems_second) >= QUIZ_OPTION_BAG_JACCARD
    return False


def _options_alike(first: str, second: str) -> bool:
    """One answer written twice: the same text (case and punctuation aside), nearly the same
    characters with the same words up to inflection, or the same bag of meaningful words. An
    opposite ("Increases ..." / "Decreases ...") is nearly the same characters but a different word:
    a good distractor, not a repeat."""
    left, right = _normalize(first).strip(" ."), _normalize(second).strip(" .")
    if left == right or _bags_alike(first, second):
        return True
    same_words = {token[:4] for token in content_tokens(first, 2)} == {token[:4] for token in content_tokens(second, 2)}
    return same_words and difflib.SequenceMatcher(None, left, right).ratio() >= QUIZ_OPTION_DUPLICATE_RATIO


def _option_problem(option: str) -> str | None:
    if not option:
        return "empty"
    if (_normalize(option).strip(" .") in _GENERIC_OPTIONS or len(option) > QUIZ_MAX_OPTION_CHARS
            or _SCAFFOLDING.search(option)):
        return "generic"
    return None


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


def _stem_prefix_bag(stem: str) -> frozenset[str]:
    return frozenset(token[:QUIZ_STEM_PREFIX_CHARS] for token in content_tokens(stem))


def _near_duplicate_stem(stem: str, answer_tokens, answer_key: str, existing: dict) -> bool:
    """The same question asked again. Identical stems always are; nearly identical stems are only
    when the difference is inflection/wording (same meaningful words) or the answer is the same.
    "What port does net.Listen(...) use?" and "What protocol does net.Listen(...) use?" ask
    different aspects of one statement and are both kept."""
    stem_key, existing_key = squash(stem), squash(existing["question"])
    if stem_key == existing_key:
        return True
    if difflib.SequenceMatcher(None, stem_key, existing_key).ratio() < QUIZ_STEM_DUPLICATE_RATIO:
        return False
    meta = existing["_meta"]
    shorter, longer = sorted((answer_key, meta["answer_key"]), key=len)
    return (_stem_prefix_bag(stem) == _stem_prefix_bag(existing["question"])
            or _same_target(answer_tokens, answer_key, meta["answer_tokens"], meta["answer_key"])
            or bool(shorter) and shorter in longer)


_QUOTE_STATEMENT_BREAK = re.compile(r"(?<=[.!?;])\s+|[○●•▪◦■□◆◇▶►➢✓✔]")


def _pointed_statements(stem: str, answer: str, quote: str) -> frozenset[str]:
    """The statements (sentences / bullets) of its evidence quote that a question points at: those
    containing the most of the stem's meaningful words, leaving out the answer's own words. A stem that
    points at nothing in particular covers the whole quote. A sentence and its translation after an
    arrow stay ONE statement."""
    statements = [squash(part) for part in _QUOTE_STATEMENT_BREAK.split(quote) if len(squash(part)) >= 3]
    answer_prefixes = {token[:QUIZ_STEM_PREFIX_CHARS] for token in content_tokens(answer)}
    anchors = [token for token in content_tokens(stem) if token[:QUIZ_STEM_PREFIX_CHARS] not in answer_prefixes]
    hits = {statement: sum(_token_supported(token, statement) for token in anchors) for statement in statements}
    best = max(hits.values(), default=0)
    # the statement(s) the stem matches best: a word shared by every statement ("net", "server")
    # does not make a question point at all of them
    return frozenset(statement for statement, count in hits.items() if count == best) if best else frozenset(statements)


def _question_meaning(stem: str, answer: str, quote: str) -> tuple[frozenset[str], frozenset[str]]:
    """(meaning, frame) of a question: `meaning` is its stem's meaningful words without the answer's
    own words (as prefixes, so "listening"/"listen" agree); `frame` is the part of them the evidence
    quote does not contain -- the words that name WHAT is asked (function, port, variable, error ...)."""
    answer_prefixes = {token[:QUIZ_STEM_PREFIX_CHARS] for token in content_tokens(answer)}
    tokens = [token for token in content_tokens(stem) if token[:QUIZ_STEM_PREFIX_CHARS] not in answer_prefixes]
    quote_squashed = squash(quote)
    meaning = frozenset(token[:QUIZ_STEM_PREFIX_CHARS] for token in tokens)
    frame = frozenset(token[:QUIZ_STEM_PREFIX_CHARS] for token in tokens if not _token_supported(token, quote_squashed))
    return meaning, frame


def _same_proposition(stem: str, answer: str, quote: str, existing: dict) -> bool:
    """Do two questions with the same correct answer on overlapping evidence test the same
    proposition? Driven by the question meaning, not by the evidence:

    * stems pointing at DIFFERENT statements of the evidence ask different things ("Which protocol
      does the server pass?" / "... the client pass?", both "tcp");
    * on the SAME statement -- even a single code line such as ln, err := net.Listen("tcp", ":8080")
      -- they are still different when their meanings barely overlap and each names its own aspect
      with words the evidence does not contain ("Which function creates the listener?" / "Which
      call's error result is stored in err?", both net.Listen);
    * otherwise it is one question reworded ("Which port does the server listen on?" / "What is the
      server's listening port?"), including a stem that merely restates the evidence."""
    meta = existing["_meta"]
    old_answer, old_quote = meta.get("answer_text", ""), meta.get("quote", "")
    if not _pointed_statements(stem, answer, quote) & _pointed_statements(existing["question"], old_answer, old_quote):
        return False
    meaning, frame = _question_meaning(stem, answer, quote)
    old_meaning, old_frame = _question_meaning(existing["question"], old_answer, old_quote)
    different_aspects = bool(frame) and bool(old_frame) and _jaccard(meaning, old_meaning) <= QUIZ_ASPECT_MAX_OVERLAP
    return not different_aspects


# ---------------------------------------------------------------------------------------------
# Local repair (no LLM call)
# ---------------------------------------------------------------------------------------------
# Wrong options: a candidate whose stem, correct answer and evidence are fine but whose
# distractors are bad (duplicates, generic, wrong language, also correct, missing/extra) keeps its
# stem, correct answer and evidence; only the bad distractors are replaced and the question is
# validated again. Distractors need not be in the source: nearby values, other identifiers of the
# material, opposite actions and the options other candidates of the same call wrote.
# Evidence: a too-short or slightly-off quote is replaced by the exact source statement it sits in.
# Nothing here invents a fact: the correct answer and the evidence always come from the model and
# the material, and every grounding rule still runs on the result.

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w)")
_DOTTED_IDENTIFIER = re.compile(r"(?<![\w.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(\))?")
_CODE_IDENTIFIER = re.compile(r"(?<![\w.])(?:[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*|[a-z]+_\w+)(?:\(\))?(?![\w.])")
_ACRONYM = re.compile(r"(?<!\w)[A-Z]{2,6}(?!\w)")
_OPPOSITE_WORDS = (
    ("increase", "decrease"), ("increases", "decreases"), ("open", "close"), ("opens", "closes"),
    ("client", "server"), ("send", "receive"), ("sends", "receives"), ("read", "write"), ("reads", "writes"),
    ("start", "stop"), ("starts", "stops"), ("before", "after"), ("first", "last"),
    ("synchronous", "asynchronous"), ("blocking", "non-blocking"), ("static", "dynamic"), ("fixed", "variable"),
    ("enable", "disable"), ("enables", "disables"), ("connect", "disconnect"), ("accept", "reject"),
    ("accepts", "rejects"), ("input", "output"), ("minimum", "maximum"), ("more", "less"), ("higher", "lower"),
    ("faster", "slower"), ("larger", "smaller"), ("local", "remote"), ("public", "private"), ("always", "never"),
    ("upload", "download"), ("encrypt", "decrypt"), ("lock", "unlock"),
    ("tăng", "giảm"), ("gửi", "nhận"), ("đọc", "ghi"), ("trước", "sau"), ("mở", "đóng"), ("lớn", "nhỏ"), ("cao", "thấp"),
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+")
_LIST_SPLIT = re.compile(r"[○●•▪◦■□◆◇▶►➢✓✔;,]|\s(?:and|or|và|hoặc)\s", flags=re.IGNORECASE)


def _answer_kind(text: str, acronyms=frozenset()) -> str:
    """"number", "identifier", "acronym" or "text": decides which replacement distractors fit."""
    text = text.strip().strip("`")
    numbers = list(_NUMBER.finditer(text))
    if len(numbers) == 1:
        rest = text[:numbers[0].start()] + text[numbers[0].end():]
        if len(_LETTER_WORD.findall(rest)) <= 2:
            return "number"
    if _DOTTED_IDENTIFIER.fullmatch(text) or _CODE_IDENTIFIER.fullmatch(text):
        return "identifier"
    if re.fullmatch(r"[A-Za-z]{2,6}", text) and (text.isupper() or text.upper() in acronyms):
        return "acronym"
    return "text"


def _nearby_numbers(correct: str) -> list[str]:
    """0 -> -1, 1, 2 ...; 2007 -> 2005, 2008, 2010 ...; the text around the number is kept."""
    match = _NUMBER.search(correct)
    value = match.group()
    decimals = len(value.split(".", 1)[1]) if "." in value else 0
    scale = 10 ** decimals
    base = round(float(value) * scale)
    offsets = (-1, 1, 2, 3, -2, 4) if decimals == 0 and abs(base) < 10 else (-2, 1, 3, -1, 2, 4, 5)
    found = []
    for offset in offsets:
        number = base + offset
        text = f"{number / scale:.{decimals}f}" if decimals else str(number)
        found.append(correct[:match.start()] + text + correct[match.end():])
    return found


def _identifier_alternatives(correct: str, texts: list[str]) -> list[str]:
    """Other identifiers of the same shape in the material: net.Dial -> net.Listen, fmt.Println, ...
    (same package first, then in document order)."""
    bare = correct.strip("`")
    pattern = _DOTTED_IDENTIFIER if _DOTTED_IDENTIFIER.fullmatch(bare) else _CODE_IDENTIFIER
    parens = bare.endswith("()")
    package = bare.split(".", 1)[0] if "." in bare else None
    seen, found = {squash(bare)}, []
    for text in texts:
        for match in pattern.finditer(text):
            name = match.group()
            name = name[:-2] if name.endswith("()") else name
            key = squash(name)
            if len(name) < 4 or key in seen:
                continue
            seen.add(key)
            found.append(name + ("()" if parens else ""))
    return sorted(found, key=lambda name: package is None or name.split(".", 1)[0] != package)


def _acronym_alternatives(correct: str, texts: list[str]) -> list[str]:
    lower = correct.islower()
    seen, found = {correct.upper()}, []
    for text in texts:
        for token in _ACRONYM.findall(text):
            if token not in seen:
                seen.add(token)
                found.append(token.lower() if lower else token)
    return found


def _opposite_alternatives(correct: str) -> list[str]:
    """The opposite action/property: "opens the socket" -> "closes the socket"."""
    found = []
    for left, right in _OPPOSITE_WORDS:
        for source, target in ((left, right), (right, left)):
            match = re.search(rf"(?<![\w-]){re.escape(source)}(?![\w-])", correct, flags=re.IGNORECASE)
            if match:
                word = target[0].upper() + target[1:] if match.group()[0].isupper() else target
                found.append(correct[:match.start()] + word + correct[match.end():])
    return found


def _bank_alternatives(correct: str, kind: str, bank, stem: str, quote: str, acronyms) -> list[str]:
    """Options other candidates of the same call wrote, of the same kind and size, the ones sharing
    vocabulary with this question first (related concepts)."""
    words = len(correct.split())
    topic = content_tokens(f"{stem} {quote}")
    scored, seen = [], set()
    for position, item in enumerate(bank or ()):
        text = _clean_inline(_LABEL.sub("", str(item or "")))
        key = squash(text)
        if not key or key in seen or _answer_kind(text, acronyms) != kind:
            continue
        seen.add(key)
        if kind == "text" and not max(1, words // 2) <= len(text.split()) <= words * 2 + 1:
            continue
        scored.append((-len(content_tokens(text) & topic), abs(len(text.split()) - words), position, text))
    return [text for *_rank, text in sorted(scored)]


def _distractor_alternatives(correct: str, stem: str, quote: str, units: list[dict], bank, extra: list[str]) -> list[str]:
    texts = [unit["evidence_excerpt"] for unit in units] + [str(item or "") for item in bank or ()]
    acronyms = frozenset(token for text in texts for token in _ACRONYM.findall(text))
    kind = _answer_kind(correct, acronyms)
    found = list(extra)
    if kind == "number":
        found += _nearby_numbers(correct)
    elif kind == "identifier":
        found += _identifier_alternatives(correct, texts)
    elif kind == "acronym":
        found += _acronym_alternatives(correct, texts)
    found += _opposite_alternatives(correct)
    found += _bank_alternatives(correct, kind, bank, stem, quote, acronyms)
    stem_key = squash(stem)
    # an option that repeats part of the stem gives the answer away (or is nonsense): skipped
    return [text for text in found if not (len(squash(text)) >= 4 and squash(text) in stem_key)]


def _list_items(quote: str) -> list[str]:
    return [key for key in (squash(part) for part in _LIST_SPLIT.split(quote)) if len(key) >= 3]


def _is_list_item(key: str, items: list[str]) -> bool:
    """Is this (squashed) option one item of a list in the quote? The first and last items may carry
    the sentence around the list ("Robert Griesemer, Ken Thompson, and Rob Pike started the
    project": "Rob Pike" starts the last item)."""
    return len(key) >= 3 and any(item == key or (len(key) >= 4 and (item.startswith(key) or item.endswith(key))) for item in items)


def _also_correct(option: str, correct: str, stem: str, quote: str) -> bool:
    """A wrong option the evidence supports as much as the correct answer: the correct answer and
    this option are items of ONE list in a sentence of the quote, AND that sentence is what the
    question asks about -- at least half of the stem's meaningful words (other than the two
    options' own words) are stated in it outside the item names.

    "Robert Griesemer, Ken Thompson, and Rob Pike started the project." / "Who started the
    project?": the sentence answers the question for every member, so Rob Pike is also correct.
    "Go supports arrays, slices, maps. Arrays have fixed size." / "Which type has fixed size?": the
    list sentence says nothing about a fixed size, so Slices and Maps stay valid distractors."""
    key, correct_key = squash(option), squash(correct)
    if key == correct_key:
        return False
    own = {token[:QUIZ_STEM_PREFIX_CHARS] for token in content_tokens(f"{option} {correct}")}
    asked = [token for token in content_tokens(stem) if token[:QUIZ_STEM_PREFIX_CHARS] not in own]
    for sentence in _SENTENCE_BREAK.split(quote):
        items = _list_items(sentence)
        if len(items) < 2 or not (_is_list_item(key, items) and _is_list_item(correct_key, items)):
            continue
        rest = squash(sentence).replace(key, "").replace(correct_key, "")
        if not asked or sum(_token_supported(token, rest) for token in asked) * 2 >= len(asked):
            return True
    return False


def repair_options(
    options: list[str], answer_index: int, stem: str, quote: str, units: list[dict],
    bank=None, language: str | None = None,
) -> tuple[list[str], int, bool]:
    """Exactly 4 options with the correct one unchanged: (options, answer position, changed).

    Good distractors are kept in their order; a bad one (empty, generic, too long, a repeat of
    another option, in another language, or also correct according to the quote) is replaced, as is a
    missing 4th option; options beyond four are dropped (and used first as replacements). Raises
    CandidateRejected when no usable replacement exists -- code "multiple_correct" when a second
    supported option could not be replaced."""
    correct = options[answer_index]
    supported, foreign = [], []

    def usable(option: str, kept: list[str]) -> bool:
        if _option_problem(option) or any(_options_alike(option, other) for other in kept):
            return False
        if _also_correct(option, correct, stem, quote):
            supported.append(option)
            return False
        if language and language_mismatch("", [option], "", language):
            foreign.append(option)
            return False
        return True

    others = [option for position, option in enumerate(options) if position != answer_index]
    kept, slots = [correct], []
    for option in others[:3]:
        if usable(option, kept):
            slots.append(option)
            kept.append(option)
        else:
            slots.append(None)
    slots += [None] * (3 - len(slots))
    missing = [index for index, slot in enumerate(slots) if slot is None]
    if missing:
        replacements = iter(_distractor_alternatives(correct, stem, quote, units, bank, others[3:]))
        for index in missing:
            for replacement in replacements:
                replacement = _clean_inline(replacement)
                if usable(replacement, kept):
                    slots[index] = replacement
                    kept.append(replacement)
                    break
            else:
                if supported:
                    raise CandidateRejected("structure", "Another option is also correct according to the material.", "multiple_correct")
                if foreign:
                    raise CandidateRejected("structure", f"An option is not written in the quiz language ({language}).", "language_mismatch")
                raise CandidateRejected("structure", "Options are not distinct and could not be repaired.", "options")
    position = min(answer_index, 3)
    final = list(slots)
    final.insert(position, correct)
    return final, position, bool(missing) or len(options) != 4


def _statement_breaks(text: str) -> list[int]:
    """Boundaries of the bullets / sentences / code statements of an excerpt (raw positions)."""
    cuts = {0, len(text)}
    for match in re.finditer(r"[○●•▪◦■□◆◇▶►➢✓✔→]|(?<=[.!?;{}])\s+", text):
        cuts.add(match.end() if match.group().isspace() else match.start())
    return sorted(cuts)


def _statement_around(unit: dict, start: int, end: int) -> str | None:
    """The exact source text of the statement(s) around the squashed span [start, end): the
    containing sentence / bullet / code statement, widened to its neighbours (previous first, e.g. a
    heading) until it is long enough to prove something; a very long statement is cut to the words
    around the span instead."""
    text = unit["evidence_excerpt"]
    raw_of = _raw_positions(text)
    if len(raw_of) != len(unit["_squashed"]) or not 0 <= start < end <= len(raw_of):
        return None
    low, high = raw_of[start], raw_of[end - 1] + 1
    cuts = _statement_breaks(text)
    first = max(index for index, cut in enumerate(cuts) if cut <= low)
    last = min(index for index, cut in enumerate(cuts) if cut >= high)

    def piece(a: int, b: int) -> str:
        return text[cuts[a]:cuts[b]].strip(" \t○●•▪◦■□◆◇▶►➢✓✔→-")

    toggle = True
    while len(squash(piece(first, last))) < QUIZ_MIN_QUOTE_CHARS and (first > 0 or last < len(cuts) - 1):
        if (toggle and first > 0) or last == len(cuts) - 1:
            first -= 1
        else:
            last += 1
        toggle = not toggle
    result = piece(first, last)
    if len(result) > QUIZ_REPAIR_MAX_QUOTE_CHARS:
        words = [match.span() for match in re.finditer(r"\S+", text)]
        inside = [index for index, (a, b) in enumerate(words) if b > low and a < high]
        if not inside:
            return None
        left, right = inside[0], inside[-1]
        while len(squash(text[words[left][0]:words[right][1]])) < QUIZ_REPAIR_WINDOW_CHARS and (left > 0 or right < len(words) - 1):
            left, right = max(0, left - 1), min(len(words) - 1, right + 1)
        result = text[words[left][0]:words[right][1]]
    return result if len(squash(result)) >= QUIZ_MIN_QUOTE_CHARS else None


def repair_evidence(quote: str, units: list[dict], correct: str = "") -> tuple[str, tuple[dict, float]] | None:
    """A too-short or not-found quote replaced by the exact source statement it comes from.

    * too short: the quote is found verbatim (squashed) in an excerpt and widened to its statement;
    * not found: one verbatim run of the quote covering QUIZ_REPAIR_NOT_FOUND_SHARE of it is found in
      an excerpt (the model added, cut or changed a few words) and widened to its statement.
    The result is exact source text, located again like any quote, and it must itself state the
    correct answer: a generic partial overlap ("to the server") never attaches a candidate to
    unrelated source text. None when nothing reliable is found."""
    quote_squashed = squash(quote)
    if len(quote_squashed) < QUIZ_REPAIR_MIN_ANCHOR_CHARS:
        return None
    anchors: list[tuple[dict, int, int]] = []
    if len(quote_squashed) < QUIZ_MIN_QUOTE_CHARS:
        for unit in units:
            position = unit["_squashed"].find(quote_squashed)
            while position >= 0 and len(anchors) < 8:
                anchors.append((unit, position, position + len(quote_squashed)))
                position = unit["_squashed"].find(quote_squashed, position + 1)
    else:
        need = max(QUIZ_MIN_QUOTE_CHARS, math.ceil(QUIZ_REPAIR_NOT_FOUND_SHARE * len(quote_squashed)))
        for unit in units:
            match = difflib.SequenceMatcher(None, unit["_squashed"], quote_squashed, autojunk=False).find_longest_match(
                0, len(unit["_squashed"]), 0, len(quote_squashed))
            if match.size >= need:
                anchors.append((unit, match.a, match.a + match.size))
        anchors.sort(key=lambda anchor: anchor[1] - anchor[2])
    for unit, start, end in anchors:
        text = _statement_around(unit, start, end)
        if not text or not _answer_supported_by(correct, text):
            continue
        located = locate_evidence(text, units)
        if located:
            return text, located
    return None


def _answer_supported_by(answer: str, text: str) -> bool:
    """The correct answer is stated in `text`: written in it, or at least half of its meaningful
    words and never fewer than QUIZ_MIN_ANSWER_MATCHES of them are (all of a short answer;
    inflections allowed) -- one shared word ("net" of net.Listen) is not support."""
    key, text_squashed = squash(answer), squash(text)
    if key and key in text_squashed:
        return True
    tokens = content_tokens(answer)
    found = sum(_token_supported(token, text_squashed) for token in tokens)
    return bool(tokens) and found * 2 >= len(tokens) and found >= min(len(tokens), QUIZ_MIN_ANSWER_MATCHES)


def _answer_in_quote(answer: str, quote: str) -> bool:
    """The whole answer written in the quote as its own token(s): "0" in "Index starts at 0"."""
    core = _normalize(answer).strip(" .,:;()[]\"'`")
    return bool(core) and re.search(rf"(?<!\w){re.escape(core)}(?!\w)", _normalize(quote)) is not None


def validate_candidate(
    raw,
    units: list[dict],
    accepted: list[dict],
    difficulty: str,
    question_id: int,
    scope_topic: dict,
    generation_index: int,
    assessment_capacity: int | None = None,
    language: str | None = None,
    distractor_bank=None,
) -> tuple[dict, list[str]]:
    """Validate one raw candidate against the context the model saw and the questions accepted so far.

    `units` are the excerpts the model was given. Hard rejections raise CandidateRejected; soft
    findings are returned as warnings and only lower the candidate's rank during selection.
    Provenance is derived here from the located evidence. Bad distractors and a too-short /
    slightly-off quote are repaired locally (repair_options, repair_evidence); `distractor_bank` holds
    the options other candidates of the same call wrote, a source of replacement distractors.
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
    if (not isinstance(raw_options, list) or not all(isinstance(o, str) for o in raw_options)
            or not QUIZ_REPAIR_MIN_OPTIONS <= len(raw_options) <= QUIZ_MAX_RAW_OPTIONS):
        raise CandidateRejected("structure", "Question must contain exactly 4 options.", "options")
    # Only an explicit "A." / "A)" / "(A)" label is stripped: the generic label stripper in
    # backend.quiz_options would also eat the article of an option such as "A process ...".
    options = [_clean_inline(_LABEL.sub("", option)) for option in raw_options]

    answer_index = raw.get("answer_index")
    if isinstance(answer_index, bool) or not isinstance(answer_index, int) or answer_index not in range(len(options)):
        raise CandidateRejected("structure", "answer_index must be a single integer 0-3.")
    correct = options[answer_index]
    if _option_problem(correct):
        raise CandidateRejected("structure", "The correct option is empty, generic, or copied raw text.", "options")

    explanation = _clean_inline(raw.get("explanation"))
    # The whole quiz is in ONE language: a question in another one (or mixing two) is rejected and
    # the existing bounded follow-up calls replace it. A distractor in another language is repaired.
    if language and language_mismatch(stem, [correct], explanation, language):
        raise CandidateRejected("structure", f"Question is not written in the quiz language ({language}).", "language_mismatch")

    quote = _clean_inline(raw.get("evidence_quote"))
    options, answer_index, options_repaired = repair_options(options, answer_index, stem, quote, units, distractor_bank, language)

    located = locate_evidence(quote, units)
    evidence_repaired = False
    if located is None:
        code = "quote_too_short" if len(squash(quote)) < QUIZ_MIN_QUOTE_CHARS else "quote_not_found"
        repaired = repair_evidence(quote, units, correct)
        if repaired is None:
            raise CandidateRejected("grounding", "evidence_quote was not found in the provided context.", code)
        quote, located = repaired
        evidence_repaired = True
        # the widened quote may show that another option is also correct: validate the options again
        options, answer_index, changed = repair_options(options, answer_index, stem, quote, units, distractor_bank, language)
        options_repaired = options_repaired or changed
    unit, alignment = located
    quote_squashed = squash(quote)
    link, answer_link = context_support(stem, correct, unit, units)
    if link is not None and link < QUIZ_MIN_LINK_SUPPORT:
        raise CandidateRejected("grounding", "The question and its answer are not supported by the context.", "question_not_supported")
    # One matching word is not an anchor: in glued PDF text a 5-letter word occurs inside other
    # words by chance, so at least half of the answer's distinctive words must be found.
    if answer_link is not None and answer_link < QUIZ_MIN_ANSWER_ANCHOR:
        raise CandidateRejected("grounding", "The correct answer is not stated in the provided context.", "answer_not_in_context")
    if language and language_mismatch(stem, options, explanation, language):
        raise CandidateRejected("structure", f"Question is not written in the quiz language ({language}).", "language_mismatch")

    # Duplicates: the same QUESTION, not the same fact, sentence or answer. One fact / sentence may
    # be asked about several times, even with the same answer, when the questions test different
    # aspects of it (the function, the port, the protocol of one code line): a repeat is a
    # near-identical stem, the same correct answer asked with largely the same words, or the same
    # correct answer on the same evidence with a stem pointing at the same statement of it.
    answer_tokens, answer_key = content_tokens(correct), squash(correct)
    for existing in accepted:
        if _near_duplicate_stem(stem, answer_tokens, answer_key, existing):
            raise CandidateRejected("duplicate", "Question duplicates an accepted question.", "duplicate_stem")
    spans = evidence_spans(quote, unit)
    signature = content_tokens(f"{stem} {correct}")
    quote_shingles = _shingles(quote_squashed)
    for existing in accepted:
        meta = existing["_meta"]
        if not _same_target(answer_tokens, answer_key, meta["answer_tokens"], meta["answer_key"]):
            continue
        # the same answer asked with largely the same words: the same question, whatever its evidence
        if signature and _jaccard(signature, meta["signature"]) >= QUIZ_CONTENT_DUPLICATE_JACCARD:
            raise CandidateRejected("duplicate", "Question tests the same fact or concept as an accepted question.", "duplicate_content")
        # same answer + overlapping evidence is NOT enough on its own: the stems must also point at
        # the same statement of that evidence
        same_evidence = (_evidence_overlap(spans, meta.get("spans", ())) >= QUIZ_EVIDENCE_OVERLAP_MAX
                         or _jaccard(quote_shingles, meta["quote_shingles"]) >= QUIZ_QUOTE_DUPLICATE_JACCARD)
        if same_evidence and _same_proposition(stem, correct, quote, existing):
            raise CandidateRejected("duplicate", "Question tests the same fact as an accepted question on the same evidence.", "duplicate_evidence")

    warnings: list[str] = []
    answer_squashed = squash(correct)
    # ONE definition: the answer's distinctive words were found in the material (answer_link is not
    # None means they passed QUIZ_MIN_ANSWER_ANCHOR above), or -- for an answer with no distinctive
    # word (a number, an acronym) -- the answer is written as its own token in the evidence quote
    # ("0" in "Index starts at 0"), or a 4+ character answer occurs in the material.
    answer_in_evidence = answer_link is not None or _answer_in_quote(correct, quote) or (
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
    if options_repaired:
        warnings.append("options_repaired")
    if evidence_repaired:
        warnings.append("evidence_repaired")

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
            "answer_tokens": answer_tokens, "answer_key": answer_key, "answer_text": correct,
            "options_repaired": options_repaired, "evidence_repaired": evidence_repaired,
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
# Fill-in-the-blank, derived from the document's persisted flashcards (no LLM call)
# ---------------------------------------------------------------------------------------------
# NOT on the live Normal Quiz path: a generated Normal Quiz is single_choice only (engine v9). The
# cloze builder serves Flashcard Practice (flashcard_cloze) and the grading helpers below grade
# every saved fill_blank question (old Normal Quizzes and Flashcard Practice alike).
#
# A fill_blank question is a cloze built deterministically from ONE usable flashcard:
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
    the cloze must not test the same fact as a question already in the pool (single_choice
    or an earlier cloze)."""
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
# model benchmark to fingerprint its flashcard input.
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
