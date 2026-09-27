"""Flashcards -> written practice quiz (short_answer / fill_blank), built without any LLM call.

A persisted flashcard set of one document is reused directly as a written quiz:

- short_answer: the card's front is the prompt, its back the canonical answer. Short answers are
  graded with the same deterministic normalization as fill_blank (normalize_fill_blank_answer:
  case, whitespace and surrounding punctuation). An answer too long for a safe exact match is a
  "self-check" question: the learner compares their text with the card answer after submitting
  and marks it themselves (set_self_check_result) -- never fuzzy or LLM grading.
- fill_blank: only for a card whose answer is a short term that is WRITTEN in the original
  document. A document sentence stating that term becomes the question with the term blanked out;
  the flashcard answer is the accepted answer. The flashcard text is never used as evidence. A card
  that cannot be grounded this way becomes a short_answer question instead -- never multiple choice.

K <= N usable cards always yields exactly K written questions. Normal document quiz generation
(backend/quiz_service.py) is not involved and not changed; these quizzes are saved as ordinary quiz
artifacts (planner_version FLASHCARD_QUIZ_PLANNER_VERSION) so the Quiz Library, Quiz Player,
autosave/resume, results, review and history work unchanged.
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
    QUIZ_MAX_STEM_CHARS,
    _SURROUNDING_PUNCTUATION,
    _fill_blank_answer_shape_ok,
    content_tokens,
    normalize_fill_blank_answer,
    squash,
)

FLASHCARD_QUIZ_PLANNER_VERSION = FLASHCARD_WRITTEN_PLANNER_VERSION
FLASHCARD_QUIZ_CONCEPT_ORIGIN = "flashcard"
FLASHCARD_QUIZ_MODES = ("mixed", "short_answer", "fill_blank")
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
        "explanation": f"From the document: {grounded['evidence_sentence']}",
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
) -> dict:
    """Build, persist and return a written quiz from this owner's flashcards of this document.

    `set_id` selects that exact set (it must belong to this owner and document); without it the
    document's newest set is used. `question_count=None` means every usable card.
    """
    mode = str(mode or "mixed").strip().lower()
    if mode not in FLASHCARD_QUIZ_MODES:
        raise ValueError("mode must be mixed, short_answer, or fill_blank.")
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
    if mode != "short_answer":
        sentences = _document_sentences(get_document_chunks(document_id, owner_id))
        for card in cards:
            question = ground_fill_blank(card, sentences)
            if question:
                grounded[card["flashcard_id"]] = question

    compatible = [card for card in cards if card["flashcard_id"] in grounded]
    others = [card for card in cards if card["flashcard_id"] not in grounded]
    fill_target = {"short_answer": 0, "fill_blank": count, "mixed": count // 2}[mode]
    fill_cards = rng.sample(compatible, min(fill_target, len(compatible)))
    fill_ids = {card["flashcard_id"] for card in fill_cards}
    # Short-answer slots take the cards that cannot be blanked first, then any blankable card left over.
    short_pool = rng.sample(others, len(others)) + rng.sample(
        [card for card in compatible if card["flashcard_id"] not in fill_ids], len(compatible) - len(fill_cards))
    chosen_ids = fill_ids | {card["flashcard_id"] for card in short_pool[:count - len(fill_cards)]}
    ordered = [card for card in cards if card["flashcard_id"] in chosen_ids]   # deck order

    questions, self_check_ids = [], []
    for question_id, card in enumerate(ordered, start=1):
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
    mode_label = {"mixed": "Mixed", "short_answer": "Short Answer", "fill_blank": "Fill in the Blank"}[mode]
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
