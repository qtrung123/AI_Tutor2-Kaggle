"""Flashcards -> practice quiz (short_answer / fill_blank / matching / mixed), built without any LLM call.

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
- mixed: about MIXED_FILL_SHARE fill_blank cards and MIXED_MATCHING_SHARE of the cards in matching
  activities, the rest short_answer.

`question_count` counts CARDS: K <= N usable cards are always all covered, by K questions or fewer
(one matching activity covers several cards). Normal document quiz generation
(backend/quiz_service.py) is not involved and not changed; these quizzes are saved as ordinary quiz
artifacts (planner_version FLASHCARD_QUIZ_PLANNER_VERSION) so the Quiz Library, Quiz Player,
autosave/resume, results, review and history work unchanged, and are never mastery evidence.
"""
import random
import re

from backend.auth_store import LEGACY_USER_ID
from backend.document_retrieval import get_document_chunks
from backend.flashcard_store import list_flashcards
from backend.indexed_document_store import get_indexed_document
from backend.quiz_store import (
    FLASHCARD_WRITTEN_PLANNER_VERSION,
    get_quiz_by_id,
    get_quiz_history_attempt,
    save_quiz,
    set_attempt_answer_correctness,
)
from backend.quiz_units import (
    FILL_BLANK_MARKER,
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
)

FLASHCARD_QUIZ_PLANNER_VERSION = FLASHCARD_WRITTEN_PLANNER_VERSION
FLASHCARD_QUIZ_CONCEPT_ORIGIN = "flashcard"
FLASHCARD_QUIZ_MODES = ("mixed", "short_answer", "fill_blank", "matching")
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
    """
    mode = str(mode or "mixed").strip().lower()
    if mode not in FLASHCARD_QUIZ_MODES or (mode == "matching" and not include_matching):
        raise ValueError("mode must be mixed, short_answer, fill_blank, or matching." if include_matching
                         else "mode must be mixed, short_answer, or fill_blank.")
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
    if mode in ("fill_blank", "mixed"):
        sentences = None if card_only_fill_blank else _document_sentences(get_document_chunks(document_id, owner_id))
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
    if mode == "matching" or (mode == "mixed" and include_matching):
        pool = [card for card in chosen if _matching_eligible(card)]
        pool = rng.sample(pool, len(pool))
        if mode == "mixed":
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

    # Deck order; a matching activity sits where its first card is.
    questions, self_check_ids, emitted = [], [], set()
    for card in chosen:
        question_id = len(questions) + 1
        group_index = group_of.get(card["flashcard_id"])
        if group_index is not None:
            if group_index not in emitted:
                emitted.add(group_index)
                group = sorted(groups[group_index], key=chosen.index)
                questions.append(_matching_question(group, question_id, rng))
            continue
        if card["flashcard_id"] in fill_ids:
            questions.append(_fill_blank_question(card, question_id, grounded[card["flashcard_id"]]))
            continue
        question = _short_answer_question(card, question_id)
        if not short_answer_is_auto_gradable(question["correct_answer"]):
            self_check_ids.append(question_id)
        questions.append(question)

    distribution = {}
    for question in questions:
        distribution[question["question_type"]] = distribution.get(question["question_type"], 0) + 1
    mode_label = {"mixed": "Mixed", "short_answer": "Short Answer", "fill_blank": "Fill in the Blank", "matching": "Matching"}[mode]
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
            "covered_cards": len(chosen),
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
