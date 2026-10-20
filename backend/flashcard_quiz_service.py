"""Flashcards -> practice quiz (short_answer / fill_blank / multi_blank / matching / mixed), built without any LLM call.

A persisted flashcard set of one document is reused directly as a practice quiz:

- short_answer: the card's front is the prompt, its back the canonical answer. Short answers are
  graded with the same deterministic normalization as fill_blank (normalize_fill_blank_answer:
  case, whitespace and surrounding punctuation). An answer too long for a safe exact match is a
  "self-check" question: the learner compares their text with the card answer after submitting
  and marks it themselves (set_self_check_result) -- never fuzzy or LLM grading.
- fill_blank: a deterministic cloze of one card, tried in this order: a document sentence stating
  the card's short answer (ground_fill_blank; skipped for Flashcards -> Practice as Quiz, whose
  blanks come only from the card's own front/back), the card's own cloze
  (quiz_units.flashcard_cloze), then one key word of the card's sentence answer that its front does
  not contain (card_keyword_cloze). A card with no safe blank becomes a short_answer question instead.
- matching: MATCHING_MIN_PAIRS-MATCHING_MAX_PAIRS cards per activity; the fronts are the prompts
  (one per line under the question text) and the backs the options, shuffled. The answer is one
  option letter per prompt, in prompt order; the activity is correct only when every pair is.
- multi_blank (Word Bank): the card's own answer sentence, shown after its front as context, with
  MULTI_BLANK_MIN_BLANKS-MULTI_BLANK_MAX_BLANKS meaningful terms blanked (build_multi_blank). Never
  a document sentence: a PDF sentence may be a header, an author line or an email address. A card
  whose answer is not a clean sentence (multi_blank_card_text) is skipped. Every blank keeps its
  answer as written plus accepted spelling variants, and the word bank adds 1-3 distractors taken
  from the other flashcards' recurring key words. Graded per blank with the fill_blank
  normalization; correct only when every blank is.
- mixed: about MIXED_FILL_SHARE fill_blank cards and MIXED_MATCHING_SHARE of the cards in matching
  activities; the rest are multi_blank where the card yields one, else short_answer.
- auto (Practice as Quiz): every card, MIXED_MATCHING_SHARE of them in matching activities and the
  rest alternating between single_choice (the card front, its back among three other cards' backs)
  and multi_blank. No short answers: a card fitting neither becomes a fill_blank, or is left out.

build_document_question_mix adds the same multi_blank and matching questions to a generated
(LLM, multiple choice) quiz, both from the document's flashcards only.

`question_count` counts CARDS: K <= N usable cards are always all covered, by K questions or fewer
(one matching activity covers several cards). Normal document quiz generation
(backend/quiz_service.py) is not involved and not changed; these quizzes are saved as ordinary quiz
artifacts (planner_version FLASHCARD_QUIZ_PLANNER_VERSION) so the Quiz Library, Quiz Player,
autosave/resume, results, review and history work unchanged, and are never mastery evidence.
"""
import math
import random
import re
import unicodedata

from backend.auth_store import LEGACY_USER_ID
from backend.document_retrieval import get_document_chunks
from backend.flashcard_store import list_flashcards
from backend.indexed_document_store import get_indexed_document
from backend.quiz_store import (
    FLASHCARD_WRITTEN_PLANNER_VERSION,
    MULTI_BLANK_QUESTION_TYPE,
    get_quiz_by_id,
    get_quiz_history_attempt,
    save_quiz,
    set_attempt_answer_correctness,
)
from backend.quiz_units import (
    FILL_BLANK_MARKER,
    QUIZ_ENGINE_VERSION,
    QUIZ_FILL_BLANK_MIN_CONTEXT_TOKENS,
    QUIZ_MAX_OPTION_CHARS,
    QUIZ_MAX_STEM_CHARS,
    CandidateRejected,
    _CLOZE_WORD,
    _SURROUNDING_PUNCTUATION,
    _cloze_key,
    _cloze_minor,
    _cloze_same_word,
    _fill_blank_answer_shape_ok,
    content_tokens,
    flashcard_cloze,
    normalize_fill_blank_answer,
    squash,
    text_language,
)

FLASHCARD_QUIZ_PLANNER_VERSION = FLASHCARD_WRITTEN_PLANNER_VERSION
FLASHCARD_QUIZ_CONCEPT_ORIGIN = "flashcard"
FLASHCARD_QUIZ_MODES = ("auto", "mixed", "short_answer", "fill_blank", "matching")
CHOICE_DISTRACTORS = 3
# A generated quiz of N questions: about N/3 multi_blank and N/6 matching activities, the rest
# multiple choice (12 -> 6 + 4 + 2).
DOCUMENT_MIX_MULTI_BLANK_SHARE = 1 / 3
DOCUMENT_MIX_MATCHING_SHARE = 1 / 6
CARD_SHARE_OF_CHOICE = 0.5   # flashcards write at most half of a generated quiz's multiple choice part
MATCHING_CARD_SHARE = 0.4    # ... and at most this share of the cards go to matching activities
# Question count of a generated quiz: AI Auto (auto_question_count) or a custom TOTAL in this range.
QUIZ_AUTO_MIN_QUESTIONS = 10
QUIZ_AUTO_MAX_QUESTIONS = 30
QUIZ_CUSTOM_MIN_QUESTIONS = 5
QUIZ_CUSTOM_MAX_QUESTIONS = 40
MATCHING_MIN_PAIRS = 3
MATCHING_MAX_PAIRS = 4   # one option letter (A-D) per card answer
MATCHING_INSTRUCTION = "Match each card with its answer."
MIXED_FILL_SHARE = 0.3
MIXED_MATCHING_SHARE = 0.3
FLASHCARD_QUIZ_DIFFICULTY = "easy"
NO_FLASHCARDS_MESSAGE = "No flashcards available for this document. Generate flashcards first."
# A short answer is graded automatically only when it is a compact term/phrase; longer answers are
# self-check (an exact normalized match of a sentence is not a fair test).
SHORT_ANSWER_AUTO_MAX_WORDS = 6
SHORT_ANSWER_AUTO_MAX_CHARS = 60
FILL_BLANK_MIN_SENTENCE_WORDS = 6
MULTI_BLANK_MIN_BLANKS = 2
MULTI_BLANK_MAX_BLANKS = 4
MULTI_BLANK_WORDS_PER_BLANK = 5     # a sentence of N words gets at most N // 5 blanks
MULTI_BLANK_MAX_DISTRACTORS = 3
MULTI_BLANK_MIN_KEY_CHARS = 4
# New multi_blank questions are answered from their word bank only. Older saved questions keep the
# modes stored with them (["type_answer", "word_bank"]); the Quiz Player shows their first mode.
MULTI_BLANK_ANSWER_MODES = ("word_bank",)
MULTI_BLANK_CONTEXT_SEPARATOR = " — "
# A card answer that is not study content: an email address, a link, an author/byline or a heading.
_EMAIL_OR_URL = re.compile(r"\S+@\S+\.\w+|https?://|www\.", re.IGNORECASE)
_BYLINE = re.compile(r"\b(?:et al|authors?|written by|presented by|lecturer|instructor|prof|professor|dr|copyright|all rights reserved|"
                     r"page \d+|slide \d+|chapter \d+|e-?mail|tel|phone)\b|©", re.IGNORECASE)
# Everyday words that say nothing about the subject: never a blank (the card's terms are).
_GENERIC_BLANK_WORDS = frozenset(
    "time times value values thing things part parts way ways kind kinds type types example examples "
    "fixed same different final first second last many much several various certain number numbers "
    "size large small high low good useful work works make makes made become becomes became given "
    "lets allow allows called known being been very just even still often always never usually "
    "người những được trong của cho với khi này đó các một nhiều".split()
)
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


class NoFlashcardsAvailable(ValueError):
    pass


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_flashcard_quiz(quiz: dict | None) -> bool:
    return str(((quiz or {}).get("assessment_plan") or {}).get("planner_version") or "") == FLASHCARD_QUIZ_PLANNER_VERSION


def short_answer_is_auto_gradable(answer: str) -> bool:
    normalized = normalize_fill_blank_answer(answer)
    return bool(normalized) and len(normalized.split()) <= SHORT_ANSWER_AUTO_MAX_WORDS and len(normalized) <= SHORT_ANSWER_AUTO_MAX_CHARS


def _fill_blank_terms(card: dict) -> list[str]:
    """The card answer as a blankable term: without a leading article first (so "The ____ ..." keeps
    the sentence's own article), then as written."""
    answer = _SURROUNDING_PUNCTUATION.sub("", _clean(card.get("back")))
    terms = []
    for term in (_LEADING_ARTICLE.sub("", answer), answer):
        if term and term not in terms and len(squash(term)) >= 3 and _fill_blank_answer_shape_ok(term):
            terms.append(term)
    return terms


def _term_pattern(term: str) -> re.Pattern:
    words = [re.escape(word) for word in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def _document_sentences(chunks: list[dict]) -> list[dict]:
    sentences = []
    for chunk in chunks:
        chunk_id = str((chunk.get("metadata") or {}).get("chunk_id") or "")
        for raw in _SENTENCE_SPLIT.split(str(chunk.get("content") or "")):
            text = _clean(raw)
            if len(text.split()) >= FILL_BLANK_MIN_SENTENCE_WORDS and len(text) <= QUIZ_MAX_STEM_CHARS:
                sentences.append({"text": text, "chunk_id": chunk_id})
    return sentences


def ground_fill_blank(card: dict, sentences: list[dict]) -> dict | None:
    """A fill_blank question for this card from ONE document sentence, or None.

    The term must come from the card answer, be written exactly once in the sentence, and the
    sentence must come from the document's own chunks (the card's source chunks are preferred, then
    the sentence sharing most words with the card prompt).
    """
    source_ids = {str(value) for value in card.get("source_chunk_ids") or []}
    prompt_tokens = content_tokens(card.get("front") or "")
    for term in _fill_blank_terms(card):
        pattern = _term_pattern(term)
        candidates = []
        for position, sentence in enumerate(sentences):
            matches = list(pattern.finditer(sentence["text"]))
            if len(matches) != 1:
                continue
            match = matches[0]
            blanked = sentence["text"][:match.start()] + FILL_BLANK_MARKER + sentence["text"][match.end():]
            if len(content_tokens(blanked.replace(FILL_BLANK_MARKER, " "))) < 3:
                continue
            rank = (sentence["chunk_id"] in source_ids, len(prompt_tokens & content_tokens(sentence["text"])), -position)
            candidates.append((rank, blanked, match.group(0), sentence))
        if candidates:
            _, blanked, written, sentence = max(candidates, key=lambda item: item[0])
            answers, seen = [], set()
            for value in (term, _clean(card.get("back")), written):
                key = normalize_fill_blank_answer(value)
                if key and key not in seen:
                    seen.add(key)
                    answers.append(value)
            return {
                "question": blanked, "correct_answer": term, "correct_answers": answers,
                "evidence_sentence": sentence["text"], "source_chunk_ids": [sentence["chunk_id"]] if sentence["chunk_id"] else [],
            }
    return None


def card_keyword_cloze(card: dict) -> dict | None:
    """The card's sentence answer with ONE key word blanked, or None. The word is the longest
    meaningful word (ties: the first) that is written once in the answer and that the front does not
    contain, so the front + the rest of the sentence point at it without giving it away."""
    front, back = _clean(card.get("front")), _clean(card.get("back"))
    words = list(_CLOZE_WORD.finditer(back))
    if ("?" in back or len(words) < FILL_BLANK_MIN_SENTENCE_WORDS or len(back) > QUIZ_MAX_STEM_CHARS
            or FILL_BLANK_MARKER in back):
        return None
    front_keys = [_cloze_key(word) for word in _CLOZE_WORD.findall(front)]
    keys = [_cloze_key(match.group()) for match in words]
    candidates = [
        match for match, key in zip(words, keys)
        if len(key) >= 4 and not key.isdigit() and not _cloze_minor(match.group()) and keys.count(key) == 1
        and not any(_cloze_same_word(key, known) for known in front_keys)
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda match: len(_cloze_key(match.group())))
    blanked = back[:best.start()] + FILL_BLANK_MARKER + back[best.end():]
    if len(content_tokens(blanked.replace(FILL_BLANK_MARKER, " "))) < 3:
        return None
    return {
        "question": f"{front} — {blanked}", "correct_answer": best.group(), "correct_answers": [best.group()],
        "evidence_sentence": back, "explanation": f"From your flashcard: {back}",
        "source_chunk_ids": [str(value) for value in card.get("source_chunk_ids") or []],
    }


def card_fill_blank(card: dict, sentences: list[dict] | None) -> dict | None:
    """The fill_blank of one card (see the module docstring for the order), or None. With
    `sentences=None` only the card's own text is used (no document sentence)."""
    grounded = ground_fill_blank(card, sentences) if sentences is not None else None
    if grounded:
        return grounded
    try:
        cloze = flashcard_cloze(card)
    except CandidateRejected:
        return card_keyword_cloze(card)
    return {
        "question": cloze["question"], "correct_answer": cloze["answer"], "correct_answers": [cloze["answer"]],
        "evidence_sentence": cloze["sentence"], "explanation": f"From your flashcard: {cloze['sentence']}",
        "source_chunk_ids": [str(value) for value in card.get("source_chunk_ids") or []],
    }


def _blank_key_ok(word: str) -> bool:
    key = _cloze_key(word)
    return len(key) >= MULTI_BLANK_MIN_KEY_CHARS and not key.isdigit() and not _cloze_minor(word)


def keyword_vocabulary(texts: list[str]) -> dict[str, tuple[int, str]]:
    """Blankable key words of these texts: key -> (occurrences, a written form, lower case preferred).
    Recurring words are the document's important terms; they rank blanks and supply distractors."""
    vocabulary: dict[str, tuple[int, str]] = {}
    for text in texts:
        for word in _CLOZE_WORD.findall(str(text or "")):
            if not _blank_key_ok(word):
                continue
            key = _cloze_key(word)
            count, written = vocabulary.get(key, (0, word))
            vocabulary[key] = (count + 1, word if word.islower() and not written.islower() else written)
    return vocabulary


def _accepted_answers(word: str) -> list[str]:
    """The blanked word as written, then spelling variants a learner may type for the same word:
    hyphen as space / joined, and without diacritics (Vietnamese typed without tone marks)."""
    values = [word]
    if "-" in word:
        values += [word.replace("-", " "), word.replace("-", "")]
    plain = "".join(char for char in unicodedata.normalize("NFKD", word) if not unicodedata.combining(char))
    values.append(plain.replace("đ", "d").replace("Đ", "D"))
    unique, seen = [], set()
    for value in values:
        key = normalize_fill_blank_answer(value)
        if key and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def multi_blank_card_text(card: dict) -> str | None:
    """The card's answer when it is a sentence worth blanking, else None (the card is skipped):
    a declarative sentence of at least FILL_BLANK_MIN_SENTENCE_WORDS words that fits a question,
    with no email address, link, byline/author/page line, and not a heading (no lower-case word,
    or Title Case throughout, or ending with a colon)."""
    back = _clean(card.get("back"))
    words = _CLOZE_WORD.findall(back)
    if ("?" in back or FILL_BLANK_MARKER in back or len(back) > QUIZ_MAX_STEM_CHARS
            or len(words) < FILL_BLANK_MIN_SENTENCE_WORDS or _EMAIL_OR_URL.search(back) or _BYLINE.search(back)
            or back.endswith(":")):
        return None
    lower = [word for word in words if word[:1].islower()]
    capitalized = [word for word in words if word[:1].isupper() and not _cloze_minor(word)]
    if not lower or len(capitalized) > len(words) / 2:
        return None
    return back


def _name_word_indexes(words: list) -> set[int]:
    """Words that look like part of a person's or an organisation's name: a capitalized word (not the
    sentence's first) next to another capitalized word. Never a blank: a name is not the concept."""
    capitalized = [match.group()[:1].isupper() and not match.group().isupper() for match in words]
    return {index for index, is_capital in enumerate(capitalized)
            if is_capital and index > 0 and ((index + 1 < len(words) and capitalized[index + 1])
                                             or (index > 1 and capitalized[index - 1]))}


def _multi_blank_from_sentence(text: str, avoid_keys: set[str], vocabulary: dict, rng: random.Random) -> dict | None:
    """Blank the most meaningful terms of `text`: blankable key words (not the sentence's first word,
    no digits, no function or everyday words, no name words) that the card front (`avoid_keys`) does
    not already give away -- longer terms first, then those the flashcards use most."""
    words = list(_CLOZE_WORD.finditer(text))
    keys = [_cloze_key(match.group()) for match in words]
    names = _name_word_indexes(words)
    # A blank must be unambiguous: written once, with no inflected twin (process/processes) left visible.
    eligible = [index for index, match in enumerate(words)
                if index > 0 and _blank_key_ok(match.group()) and not any(char.isdigit() for char in keys[index])
                and keys[index] not in _GENERIC_BLANK_WORDS and index not in names and not any(_cloze_same_word(keys[index], key) for key in avoid_keys)
                and sum(_cloze_same_word(keys[index], other) for other in keys) == 1]
    ranked = sorted(eligible, key=lambda index: (len(keys[index]) >= 6, min(3, vocabulary.get(keys[index], (0, ""))[0]),
                                                 len(keys[index]), -index), reverse=True)
    limit = min(MULTI_BLANK_MAX_BLANKS, max(MULTI_BLANK_MIN_BLANKS, len(words) // MULTI_BLANK_WORDS_PER_BLANK))
    chosen: list[int] = []
    for index in ranked:
        if len(chosen) == limit:
            break
        if all(abs(index - other) >= 2 for other in chosen):   # never two adjacent blanks
            chosen.append(index)
    while len(chosen) >= MULTI_BLANK_MIN_BLANKS:
        blanked = text
        for index in sorted(chosen, reverse=True):
            blanked = blanked[:words[index].start()] + FILL_BLANK_MARKER + blanked[words[index].end():]
        if len(content_tokens(blanked.replace(FILL_BLANK_MARKER, " "))) >= QUIZ_FILL_BLANK_MIN_CONTEXT_TOKENS:
            break
        chosen.remove(max(chosen, key=ranked.index))   # too little context left: drop the weakest blank
    if len(chosen) < MULTI_BLANK_MIN_BLANKS:
        return None
    chosen.sort()
    answers = [words[index].group() for index in chosen]
    answer_keys = [keys[index] for index in chosen]
    average = sum(len(key) for key in answer_keys) / len(answer_keys)
    # Distractors: the flashcards' other recurring key words, closest in length to the answers; never
    # a word of this sentence (or an inflection of one), so a distractor can never be a right answer,
    # and never an everyday word or a number.
    pool = sorted(
        ((count, key, written) for key, (count, written) in vocabulary.items()
         if key not in _GENERIC_BLANK_WORDS and not any(char.isdigit() for char in key)
         and not any(_cloze_same_word(key, other) for other in keys)),
        key=lambda item: (-item[0], abs(len(item[1]) - average), item[1]),
    )
    distractors = [written for _, _, written in pool[:MULTI_BLANK_MAX_DISTRACTORS]]
    if not distractors:
        return None
    word_bank = answers + distractors
    rng.shuffle(word_bank)
    if word_bank[:len(answers)] == answers:   # never the answers in blank order
        word_bank = word_bank[1:] + word_bank[:1]
    return {
        "question": blanked,
        "blanks": [{"id": number, "answer": answer, "accepted_answers": _accepted_answers(answer)}
                   for number, answer in enumerate(answers, start=1)],
        "distractors": distractors,
        "word_bank": word_bank,
    }


def multi_blank_is_valid(question: dict, sentence: str) -> bool:
    """Structural checks of a built multi_blank question against its evidence sentence."""
    blanks = question.get("blanks") or []
    answers = [normalize_fill_blank_answer(blank.get("answer")) for blank in blanks]
    accepted = {normalize_fill_blank_answer(value) for blank in blanks for value in blank.get("accepted_answers") or []}
    filled = question["question"]
    for blank in blanks:
        filled = filled.replace(FILL_BLANK_MARKER, blank["answer"], 1)
    return (
        MULTI_BLANK_MIN_BLANKS <= len(blanks) <= MULTI_BLANK_MAX_BLANKS
        and question["question"].count(FILL_BLANK_MARKER) == len(blanks)
        and len(set(answers)) == len(answers) and all(answers)
        and filled == sentence
        and 1 <= len(question.get("distractors") or []) <= MULTI_BLANK_MAX_DISTRACTORS
        and not any(normalize_fill_blank_answer(value) in accepted for value in question["distractors"])
        and sorted(question.get("word_bank") or []) == sorted([blank["answer"] for blank in blanks] + question["distractors"])
    )


def build_multi_blank(card: dict, vocabulary: dict, rng: random.Random) -> dict | None:
    """A multi_blank (Word Bank) question from this card's own answer (see the module docstring), or
    None when the card is unsuitable. The question is "<front> — <answer with blanks>": the front
    keeps the blanked sentence's meaning clear; none of its words is ever a blank."""
    text = multi_blank_card_text(card)
    front = _clean(card.get("front"))
    if not text or not front:
        return None
    front_keys = {_cloze_key(word) for word in _CLOZE_WORD.findall(front) if not _cloze_minor(word)}
    question = _multi_blank_from_sentence(text, front_keys, vocabulary, rng)
    if not question or not multi_blank_is_valid(question, text):
        return None
    return {**question, "question": f"{front}{MULTI_BLANK_CONTEXT_SEPARATOR}{question['question']}",
            "evidence_sentence": text, "explanation": f"From your flashcard: {text}",
            "source_chunk_ids": [str(value) for value in card.get("source_chunk_ids") or []]}


def card_vocabulary(cards: list[dict]) -> dict[str, tuple[int, str]]:
    """keyword_vocabulary of the flashcards themselves (the word bank's distractors), leaving out
    any card that carries metadata -- an email address, a link or an author/byline line."""
    return keyword_vocabulary([f"{card.get('front') or ''} {card.get('back') or ''}" for card in cards
                               if not any(_EMAIL_OR_URL.search(str(card.get(side) or "")) or _BYLINE.search(str(card.get(side) or ""))
                                          for side in ("front", "back"))])


def _matching_eligible(card: dict) -> bool:
    return len(_clean(card.get("front"))) <= QUIZ_MAX_OPTION_CHARS and len(_clean(card.get("back"))) <= QUIZ_MAX_OPTION_CHARS


def _matching_group_sizes(card_count: int) -> list[int]:
    """Activity sizes covering as many of `card_count` cards as possible (4s, with 3s to absorb a
    remainder): 4 -> [4], 6 -> [3, 3], 7 -> [4, 3], 9 -> [3, 3, 3]; 5 -> [4] (one card left)."""
    sizes = [MATCHING_MAX_PAIRS] * (card_count // MATCHING_MAX_PAIRS)
    remainder = card_count % MATCHING_MAX_PAIRS
    if remainder == 3:
        sizes.append(3)
    elif remainder == 2 and len(sizes) >= 1:
        sizes[-1:] = [3, 3]
    elif remainder == 1 and len(sizes) >= 2:
        sizes[-2:] = [3, 3, 3]
    return sizes


def matching_groups(cards: list[dict]) -> list[list[dict]]:
    """Split cards (in the given order) into matching activities whose fronts and backs are all
    distinct; a card that does not fit any activity is left out (it becomes a short answer)."""
    remaining = list(cards)
    groups = []
    for size in _matching_group_sizes(len(remaining)):
        group = []
        for card in list(remaining):
            if len(group) == size:
                break
            if any(normalize_fill_blank_answer(card.get(side)) == normalize_fill_blank_answer(other.get(side))
                   for other in group for side in ("front", "back")):
                continue
            group.append(card)
            remaining.remove(card)
        if len(group) >= MATCHING_MIN_PAIRS:
            groups.append(group)
    return groups


def _matching_question(group: list[dict], question_id: int, rng: random.Random) -> dict:
    """One matching activity: prompt i is group[i]'s front; option j is the back of card order[j]."""
    order = list(range(len(group)))
    rng.shuffle(order)
    if order == sorted(order):
        order = order[1:] + order[:1]   # never the identity: the answers are always shuffled
    letters = ["ABCD"[order.index(index)] for index in range(len(group))]
    prompts = "\n".join(f"{index}. {_clean(card.get('front'))}" for index, card in enumerate(group, start=1))
    sources = []
    for card in group:
        sources += [str(value) for value in card.get("source_chunk_ids") or [] if str(value) not in sources]
    return {
        **_base_question(group[0], question_id),
        "question": f"{MATCHING_INSTRUCTION}\n{prompts}",
        "options": [_clean(group[index].get("back")) for index in order],
        "question_type": "matching",
        "correct_answer": ",".join(letters),
        "correct_answers": letters,
        "explanation": "Pairs from your flashcards.",
        "source_chunk_ids": sources,
    }


def _base_question(card: dict, question_id: int) -> dict:
    return {
        "id": question_id,
        "options": [],
        "topic_id": str(card.get("topic_id") or "document"),
        "topic_name": str(card.get("topic_name") or ""),
        "concept_id": str(card.get("flashcard_id") or ""),
        "concept_name": str(card.get("subtopic_name") or card.get("topic_name") or ""),
        "source_subtopic_ids": [card["subtopic_id"]] if card.get("subtopic_id") else [],
        "concept_origin": FLASHCARD_QUIZ_CONCEPT_ORIGIN,
        "concept_plan_id": FLASHCARD_QUIZ_PLANNER_VERSION,
        "assessment_capacity": 0,
        "difficulty": FLASHCARD_QUIZ_DIFFICULTY,
        "validation_outcome": "accepted",
    }


def _short_answer_question(card: dict, question_id: int) -> dict:
    answer = _clean(card.get("back"))
    return {
        **_base_question(card, question_id),
        "question": _clean(card.get("front")),
        "question_type": "short_answer",
        "correct_answer": answer,
        "correct_answers": [answer],
        "explanation": "Answer from your flashcard.",
        "source_chunk_ids": [str(value) for value in card.get("source_chunk_ids") or []],
    }


def _fill_blank_question(card: dict, question_id: int, grounded: dict) -> dict:
    return {
        **_base_question(card, question_id),
        "question": grounded["question"],
        "question_type": "fill_blank",
        "correct_answer": grounded["correct_answer"],
        "correct_answers": grounded["correct_answers"],
        "explanation": grounded.get("explanation") or f"From the document: {grounded['evidence_sentence']}",
        "source_chunk_ids": grounded["source_chunk_ids"],
    }


def _multi_blank_question(card: dict, question_id: int, built: dict) -> dict:
    answers = [blank["answer"] for blank in built["blanks"]]
    return {
        **_base_question(card, question_id),
        "question": built["question"],
        "question_type": MULTI_BLANK_QUESTION_TYPE,
        "correct_answer": " | ".join(answers),
        "correct_answers": answers,   # one canonical answer per blank, in blank order
        "blanks": built["blanks"],
        "distractors": built["distractors"],
        "word_bank": built["word_bank"],
        "answer_modes": list(MULTI_BLANK_ANSWER_MODES),
        "explanation": built["explanation"],
        "source_chunk_ids": built["source_chunk_ids"],
    }


def card_choice(card: dict, cards: list[dict], rng: random.Random) -> dict | None:
    """A single_choice question from one card, or None: its front as the stem, its back among the
    backs of three other cards (same topic first, then closest in length). Needs three distinct
    other backs short enough for an option."""
    answer = _clean(card.get("back"))
    if not _clean(card.get("front")) or not answer or len(answer) > QUIZ_MAX_OPTION_CHARS:
        return None
    seen, pool = {normalize_fill_blank_answer(answer)}, []
    for other in cards:
        text = _clean(other.get("back"))
        key = normalize_fill_blank_answer(text)
        if other is card or not key or key in seen or len(text) > QUIZ_MAX_OPTION_CHARS:
            continue
        seen.add(key)
        pool.append(other)
    if len(pool) < CHOICE_DISTRACTORS:
        return None
    pool.sort(key=lambda other: (other.get("topic_id") != card.get("topic_id"),
                                 abs(len(_clean(other.get("back"))) - len(answer)), _clean(other.get("back"))))
    options = [answer] + [_clean(other.get("back")) for other in pool[:CHOICE_DISTRACTORS]]
    rng.shuffle(options)
    letter = "ABCD"[options.index(answer)]
    return {"question": _clean(card.get("front")), "options": options, "correct_answer": letter, "correct_answers": [letter]}


def _choice_question(card: dict, question_id: int, built: dict) -> dict:
    return {
        **_base_question(card, question_id),
        **built,
        "question_type": "single_choice",
        "explanation": f"From your flashcard: {_clean(card.get('back'))}",
        "source_chunk_ids": [str(value) for value in card.get("source_chunk_ids") or []],
    }


def _auto_written_question(card: dict, question_id: int, cards: list[dict], sentences: list[dict] | None,
                           vocabulary: dict, counts: dict, rng: random.Random) -> dict | None:
    """Auto mode, one card outside matching: whichever of single_choice / multi_blank the quiz has
    fewer of so far (the other when that one cannot be built), else a fill_blank; never a short answer."""
    def choice():
        built = card_choice(card, cards, rng)
        return _choice_question(card, question_id, built) if built else None

    def multi_blank():
        built = build_multi_blank(card, vocabulary, rng)
        return _multi_blank_question(card, question_id, built) if built else None

    builders = {"single_choice": choice, MULTI_BLANK_QUESTION_TYPE: multi_blank}
    for kind in sorted(builders, key=lambda kind: (counts.get(kind, 0), kind != MULTI_BLANK_QUESTION_TYPE)):
        question = builders[kind]()
        if question:
            return question
    cloze = card_fill_blank(card, sentences)
    return _fill_blank_question(card, question_id, cloze) if cloze else None


def _round_robin_by_topic(cards: list[dict]) -> list[dict]:
    """The cards reordered to take one card per topic in turn (topics in first-seen order), so every
    question type draws on every topic before any topic repeats."""
    by_topic: dict[str, list[dict]] = {}
    for card in cards:
        by_topic.setdefault(str(card.get("topic_id") or ""), []).append(card)
    queues, ordered = list(by_topic.values()), []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def auto_question_count(units: list[dict], cards: list[dict], topic_count: int) -> int:
    """AI Auto: the TOTAL number of question activities the material supports.

    One per distinct concept: every document excerpt (about one testable fact each), plus half the
    flashcards (they are drawn from the same text, so about half add a concept the excerpts would
    not), plus one per topic beyond the first (spread). Kept within QUIZ_AUTO_MIN_QUESTIONS..
    QUIZ_AUTO_MAX_QUESTIONS, and never above excerpts + flashcards: nothing is inflated."""
    concepts = len(units) + math.ceil(len(cards) / 2) + max(0, topic_count - 1)
    capacity = max(1, len(units) + len(cards))
    return max(min(QUIZ_AUTO_MIN_QUESTIONS, capacity), min(QUIZ_AUTO_MAX_QUESTIONS, concepts, capacity))


def _alternate_types(questions: list[dict]) -> list[dict]:
    """One question of each type in turn, so a quiz never shows a run of the same activity."""
    by_type: dict[str, list[dict]] = {}
    for question in questions:
        by_type.setdefault(question["question_type"], []).append(question)
    queues, ordered = list(by_type.values()), []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def build_document_question_mix(
    owner_id: str, document_id: str, units: list[dict], scope_topic: dict, difficulty: str,
    question_count: int, rng: random.Random, language: str | None = None, cards: list[dict] | None = None,
) -> dict:
    """Everything of a generated quiz of `question_count` TOTAL activities that the model does not
    write. Returns {"questions", "choice_target", "avoid_answers", "avoid_sentences",
    "card_answer_keys"}; ids are assigned by the caller. `units` (the quiz's document excerpts) are
    accepted for the caller's convenience: nothing here is built from document text.

    Targets: about DOCUMENT_MIX_MATCHING_SHARE matching activities (a matching activity is ONE
    question), DOCUMENT_MIX_MULTI_BLANK_SHARE multi_blank, the rest multiple choice. Every card is
    used at most once (one concept, one question) and the cards are taken one topic at a time:
      1. matching from the flashcards;
      2. multi_blank (Word Bank) from the flashcards' own answer sentences (build_multi_blank; an
         unsuitable card is skipped) -- never the same sentence twice;
      3. multiple choice from flashcards, at most CARD_SHARE_OF_CHOICE of the multiple choice part.
    `choice_target` is what is left for the model, which writes multiple choice questions from the
    document (more when the flashcards ran short). The avoid sets let the caller drop model
    questions that repeat a card answer or a flashcard multi_blank sentence. Cards in another
    language than the quiz `language` are skipped."""
    topic_id = str(scope_topic["topic_id"])
    shared = {
        "topic_id": topic_id, "topic_name": str(scope_topic.get("name") or topic_id), "source_subtopic_ids": [],
        "concept_plan_id": QUIZ_ENGINE_VERSION, "assessment_capacity": 0, "difficulty": difficulty,
        "validation_outcome": "accepted",
    }
    if cards is None:
        cards = _usable_cards(owner_id, document_id, None)
    cards = [card for card in cards
             if (topic_id == "document" or str(card.get("topic_id") or "") == topic_id)
             and (not language or text_language(f"{card.get('front') or ''} {card.get('back') or ''}") in (language, None))]
    cards = _round_robin_by_topic(rng.sample(cards, len(cards)))
    matching_target = int(question_count * DOCUMENT_MIX_MATCHING_SHARE + 0.5)
    multi_target = int(question_count * DOCUMENT_MIX_MULTI_BLANK_SHARE + 0.5)
    choice_total = question_count - matching_target - multi_target
    questions, used_cards, used_sentences = [], set(), set()

    def card_fields(card: dict) -> dict:
        return {**shared, "concept_id": f"flashcard:{card['flashcard_id']}",
                "concept_name": str(card.get("subtopic_name") or card.get("topic_name") or ""),
                "concept_origin": FLASHCARD_QUIZ_CONCEPT_ORIGIN}

    # 1. Matching, on at most MATCHING_CARD_SHARE of the cards so the other types keep cards too.
    cap = min(MATCHING_MAX_PAIRS * matching_target, max(MATCHING_MIN_PAIRS, math.ceil(len(cards) * MATCHING_CARD_SHARE)))
    # Cards whose answer cannot become a multi_blank sentence go to matching first.
    pool = sorted((card for card in cards if _matching_eligible(card)),
                  key=lambda card: multi_blank_card_text(card) is not None)[:cap]
    for group in matching_groups(pool)[:matching_target]:
        questions.append({**_matching_question(group, 0, rng), **card_fields(group[0]), "concept_name": "Flashcard matching"})
        used_cards.update(card["flashcard_id"] for card in group)

    # 2. multi_blank, from the cards' own answers; distractors from the flashcards' key words.
    vocabulary = card_vocabulary(cards)
    multi_count = 0
    for card in cards:
        if multi_count >= multi_target:
            break
        if card["flashcard_id"] in used_cards:
            continue
        built = build_multi_blank(card, vocabulary, rng)
        if not built or squash(built["evidence_sentence"]) in used_sentences:
            continue
        questions.append({**_multi_blank_question(card, 0, built), **card_fields(card)})
        used_cards.add(card["flashcard_id"])
        used_sentences.add(squash(built["evidence_sentence"]))
        multi_count += 1

    # 3. Multiple choice from flashcards (the model writes the rest, from the document).
    card_choices = 0
    for card in cards:
        if card_choices >= math.ceil(choice_total * CARD_SHARE_OF_CHOICE):
            break
        if card["flashcard_id"] in used_cards:
            continue
        built = card_choice(card, cards, rng)
        if built:
            questions.append({**_choice_question(card, 0, built), **card_fields(card)})
            used_cards.add(card["flashcard_id"])
            card_choices += 1

    used = [card for card in cards if card["flashcard_id"] in used_cards]
    return {
        "questions": questions,
        "choice_target": max(0, question_count - len(questions)),
        "avoid_answers": {normalize_fill_blank_answer(card.get("back")) for card in used} - {""},
        "avoid_sentences": used_sentences,
        # A document sentence stating a used card's answer tests that card's concept again.
        "card_answer_keys": {key for key in (squash(card.get("back")) for card in used) if len(key) >= 6},
    }


def alternate_types(questions: list[dict]) -> list[dict]:
    return _alternate_types(questions)


def usable_flashcards(owner_id: str, document_id: str) -> list[dict]:
    """The document's newest flashcard set, cards with both sides filled in (what a quiz can use)."""
    return _usable_cards(owner_id, document_id, None)


def _usable_cards(owner_id: str, document_id: str, set_id: str | None) -> list[dict]:
    return [card for card in list_flashcards(owner_id, document_id, set_id or None)
            if _clean(card.get("front")) and _clean(card.get("back"))]


def create_flashcard_written_quiz(
    owner_id: str,
    document_id: str,
    mode: str = "mixed",
    question_count: int | None = None,
    set_id: str | None = None,
    quiz_name: str | None = None,
    rng: random.Random | None = None,
    include_matching: bool = False,
    card_only_fill_blank: bool = False,
) -> dict:
    """Build, persist and return a written quiz from this owner's flashcards of this document.

    `set_id` selects that exact set (it must belong to this owner and document); without it the
    document's newest set is used. `question_count` counts cards (None = every usable card); a
    matching activity covers several cards, so the quiz may have fewer questions than cards.

    `include_matching` is set only by Flashcards -> Practice as Quiz. Without it (the Planner's
    written_quiz sessions) the quiz stays written-only: Mixed is the earlier half fill_blank /
    half short_answer split and Matching is not offered.

    `card_only_fill_blank` (also set only by Practice as Quiz) builds every fill_blank from the
    selected card's own front/back -- never from a document sentence.

    `auto` (Practice as Quiz) always includes matching; see the module docstring.
    """
    mode = str(mode or "mixed").strip().lower()
    if mode not in FLASHCARD_QUIZ_MODES or (mode == "matching" and not include_matching):
        raise ValueError("mode must be auto, mixed, short_answer, fill_blank, or matching." if include_matching
                         else "mode must be auto, mixed, short_answer, or fill_blank.")
    # A plain read (not quiz_common's lookup, which also runs topic-schema invalidation): creating
    # practice must have no side effect on the document's quizzes or mastery.
    document = get_indexed_document(owner_id, document_id)
    if not document:
        raise ValueError("Document not found.")
    cards = _usable_cards(owner_id, document_id, set_id)
    if not cards:
        raise NoFlashcardsAvailable(NO_FLASHCARDS_MESSAGE)
    count = len(cards) if question_count is None else int(question_count)
    if count < 1:
        raise ValueError("question_count must be at least 1.")
    if count > len(cards):
        raise ValueError(f"Only {len(cards)} flashcard{'s' if len(cards) != 1 else ''} available for this quiz.")
    rng = rng or random.Random()

    grounded: dict[str, dict] = {}
    sentences = None
    if mode in ("fill_blank", "mixed", "auto") and not card_only_fill_blank:
        sentences = _document_sentences(get_document_chunks(document_id, owner_id))
    if mode in ("fill_blank", "mixed"):
        for card in cards:
            question = card_fill_blank(card, sentences)
            if question:
                grounded[card["flashcard_id"]] = question

    # Which K cards: all of them, else a random sample (Fill Blank prefers cards that can be blanked).
    if count == len(cards):
        chosen = list(cards)
    else:
        if mode == "fill_blank":
            compatible = [card for card in cards if card["flashcard_id"] in grounded]
            picked = rng.sample(compatible, min(count, len(compatible)))
            picked += rng.sample([card for card in cards if card["flashcard_id"] not in grounded], count - len(picked))
        else:
            picked = rng.sample(cards, count)
        chosen_ids = {card["flashcard_id"] for card in picked}
        chosen = [card for card in cards if card["flashcard_id"] in chosen_ids]   # deck order

    groups: list[list[dict]] = []
    if mode in ("matching", "auto") or (mode == "mixed" and include_matching):
        pool = [card for card in chosen if _matching_eligible(card)]
        pool = rng.sample(pool, len(pool))
        if mode in ("mixed", "auto"):
            target = MATCHING_MAX_PAIRS * int(len(chosen) * MIXED_MATCHING_SHARE / MATCHING_MAX_PAIRS + 0.5)
            # Cards that cannot be blanked go to matching first, so fill_blank keeps its cards.
            pool = sorted(pool, key=lambda card: card["flashcard_id"] in grounded)[:target]
        groups = matching_groups(pool)
    group_of = {card["flashcard_id"]: index for index, group in enumerate(groups) for card in group}

    fill_ids: set[str] = set()
    fill_pool = [card for card in chosen if card["flashcard_id"] in grounded and card["flashcard_id"] not in group_of]
    if mode == "fill_blank":
        fill_ids = {card["flashcard_id"] for card in fill_pool}
    elif mode == "mixed":
        target = int(len(chosen) * MIXED_FILL_SHARE + 0.5) if include_matching else len(chosen) // 2
        fill_ids = {card["flashcard_id"] for card in rng.sample(fill_pool, min(target, len(fill_pool)))}

    # Mixed / Auto: the cards left for short answers become multi_blank questions where possible
    # (from the card's own answer; distractors from the flashcards' key words).
    vocabulary = {}
    if mode in ("mixed", "auto"):
        vocabulary = card_vocabulary(cards)

    # Deck order; a matching activity sits where its first card is.
    questions, self_check_ids, emitted = [], [], set()
    covered, counts = 0, {}
    for card in chosen:
        question_id = len(questions) + 1
        group_index = group_of.get(card["flashcard_id"])
        if group_index is not None:
            covered += 1
            if group_index not in emitted:
                emitted.add(group_index)
                group = sorted(groups[group_index], key=chosen.index)
                questions.append(_matching_question(group, question_id, rng))
            continue
        if mode == "auto":
            question = _auto_written_question(card, question_id, cards, sentences, vocabulary, counts, rng)
            if question:   # a card fitting no auto question type is left out
                covered += 1
                counts[question["question_type"]] = counts.get(question["question_type"], 0) + 1
                questions.append(question)
            continue
        covered += 1
        if card["flashcard_id"] in fill_ids:
            questions.append(_fill_blank_question(card, question_id, grounded[card["flashcard_id"]]))
            continue
        built = build_multi_blank(card, vocabulary, rng) if mode == "mixed" else None
        if built:
            questions.append(_multi_blank_question(card, question_id, built))
            continue
        question = _short_answer_question(card, question_id)
        if not short_answer_is_auto_gradable(question["correct_answer"]):
            self_check_ids.append(question_id)
        questions.append(question)

    if not questions:
        raise ValueError("These flashcards are too few to build a quiz. Add or generate more flashcards.")
    distribution = {}
    for question in questions:
        distribution[question["question_type"]] = distribution.get(question["question_type"], 0) + 1
    mode_label = {"auto": "Mixed", "mixed": "Mixed", "short_answer": "Short Answer", "fill_blank": "Fill in the Blank",
                  "matching": "Matching"}[mode]
    title = _clean(quiz_name)[:200] or f"{document_id} · Flashcard practice ({mode_label})"
    quiz = save_quiz(document_id, FLASHCARD_QUIZ_DIFFICULTY, {
        "title": title,
        "document_hash": document.get("hash", ""),
        "topic_id": "document",
        "topic_name": "Entire document",
        "topic_schema_version": int(document.get("topic_schema_version", 0)),
        "assessment_scope": "document",
        "assessment_plan": {
            "planner_version": FLASHCARD_QUIZ_PLANNER_VERSION,
            "source": "flashcards",
            "mode": mode,
            "flashcard_set_id": cards[0]["set_id"],
            "available_flashcards": len(cards),
            "requested_count": count,
            "actual_count": len(questions),
            "covered_cards": covered,
            "type_distribution": distribution,
            "self_check_question_ids": self_check_ids,
        },
        "questions": questions,
    }, owner_id)
    return get_quiz_by_id(quiz["quiz_id"], owner_id) or quiz


def set_self_check_result(attempt_id: str, question_id: int, is_correct: bool, owner_id: str = LEGACY_USER_ID) -> dict:
    """The learner's own verdict on one answered self-check question of a completed attempt."""
    attempt = get_quiz_history_attempt(attempt_id, owner_id)
    if not attempt:
        raise LookupError("Quiz attempt was not found.")
    quiz = get_quiz_by_id(attempt.get("quiz_id") or "", owner_id)
    plan = (quiz or {}).get("assessment_plan") or {}
    if not is_flashcard_quiz(quiz) or int(question_id) not in {int(value) for value in plan.get("self_check_question_ids") or []}:
        raise ValueError("Only self-check questions can be marked by the learner.")
    result = next((item for item in attempt["question_results"] if int(item["question_id"]) == int(question_id)), None)
    if not result or not str(result.get("selected_answer") or "").strip():
        raise ValueError("Only an answered question can be marked.")
    set_attempt_answer_correctness(attempt_id, owner_id, int(question_id), bool(is_correct))
    return get_quiz_history_attempt(attempt_id, owner_id)
