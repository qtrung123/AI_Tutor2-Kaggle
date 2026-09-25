"""Quiz attempt lifecycle: resume, autosave, submit/grade, retake, history, results and explanations.

Quiz generation stays in backend/quiz_service.py; persistence stays in backend/quiz_store.py.
"""
import re
from uuid import uuid4

from backend.auth_store import LEGACY_USER_ID
from backend.mastery_service import recompute_topic_mastery
from backend.quiz_common import QUIZ_DIFFICULTIES, _document_lookup
from backend.quiz_options import OPTION_LETTERS
from backend.quiz_store import (
    get_latest_attempt,
    get_latest_completed_attempt_for_quiz,
    get_quiz,
    get_quiz_attempt_summary,
    get_quiz_by_id,
    get_quiz_explanation,
    get_quiz_history_attempt,
    get_quiz_titles,
    list_quiz_history as load_quiz_history,
    quiz_cache_key,
    reset_quiz_progress,
    save_quiz_explanation,
    save_quiz_progress,
    utc_now_iso,
)
from backend.quiz_units import fill_blank_is_correct, normalize_fill_blank_answer
from backend.rag_service import explain_quiz_answer


def _question_type(question: dict) -> str:
    return str(question.get("question_type") or "single_choice")


def _fill_blank_correct_answers(question: dict) -> list[str]:
    """A fill_blank question's accepted answers, canonical first, text kept as written."""
    values = question.get("correct_answers")
    if not isinstance(values, list) or not values:
        values = [question.get("correct_answer", "")]
    canonical = str(question.get("correct_answer") or "").strip()
    ordered = ([canonical] if canonical else []) + [str(value).strip() for value in values]
    unique, seen = [], set()
    for value in ordered:
        key = normalize_fill_blank_answer(value)
        if key and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _correct_answers(question: dict) -> list[str]:
    values = question.get("correct_answers")
    if not isinstance(values, list) or not values:
        values = [question.get("correct_answer", "")]
    return list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))


def _fill_blank_answer(value) -> str:
    """A learner's fill_blank answer as saved: the text itself (trimmed, inner whitespace collapsed).
    Case and punctuation are kept for display; grading normalizes (normalize_fill_blank_answer)."""
    if isinstance(value, list) and len(value) <= 1:
        value = value[0] if value else ""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("A fill-in-the-blank answer must be text.")
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > 200:
        raise ValueError("A fill-in-the-blank answer must be at most 200 characters.")
    return text


def _saved_answer(question: dict, value):
    """Normalize one submitted/autosaved answer for its question type (None = unanswered)."""
    if _question_type(question) == "fill_blank":
        return _fill_blank_answer(value) or None
    if value in (None, "", []):
        return None
    selected = _selected_answers(value)
    return selected if _question_type(question) == "multi_select" else selected[0]


def _selected_answers(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    normalized = list(dict.fromkeys(str(item).strip().upper() for item in values if str(item).strip()))
    if not normalized or any(item not in OPTION_LETTERS for item in normalized):
        raise ValueError("Selected answers must contain only A, B, C, or D.")
    return sorted(normalized)


def load_quiz_with_attempt(
    document_id: str, difficulty: str, topic_id: str, student_id: str = LEGACY_USER_ID,
    quiz_id: str | None = None,
) -> dict:
    """Return a saved quiz plus its latest attempt.

    With `quiz_id`, resolves exactly that quiz artifact via get_quiz_by_id -- never a different one
    from the same slot, and never falls back to "the newest quiz in this slot" if that exact id is
    missing/not owned (the Quiz Library's Start/Resume must open precisely the quiz the user
    clicked). Without a quiz_id, keeps the older slot lookup (get_quiz's "newest compatible-ish
    quiz here") for callers that have no specific artifact to ask for.
    """
    known_documents = _document_lookup(student_id)
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")

    if quiz_id:
        quiz = get_quiz_by_id(quiz_id, student_id)
        if quiz and quiz.get("document_id") != document_id:
            quiz = None
    else:
        quiz = get_quiz(document_id, difficulty, topic_id, student_id)
    # Scoped to this exact quiz_id so "latest_attempt" can never belong to a different sibling
    # quiz sharing the same (document, topic, difficulty) slot.
    latest_attempt = get_latest_attempt(
        document_id, difficulty, topic_id, student_id, quiz_id=quiz.get("quiz_id") if quiz else None
    )
    return {
        "document_id": document_id,
        "difficulty": difficulty,
        "topic_id": topic_id,
        "quiz": quiz,
        "latest_attempt": latest_attempt,
        "attempt_summary": get_quiz_attempt_summary(quiz["quiz_id"], student_id) if quiz else None,
    }


def _quiz_from_attempt_snapshot(attempt: dict) -> dict | None:
    results = list(attempt.get("question_results") or [])
    if not results or any(
        len(result.get("options") or []) != 4 and result.get("question_type") != "fill_blank" for result in results
    ):
        return None
    topic_id = str(attempt.get("topic_id") or "document")
    questions = [{
        "id": int(result["question_id"]), "question": result.get("question", ""),
        "options": list(result.get("options") or []), "correct_answer": result.get("correct_answer", ""),
        "question_type": result.get("question_type") or "single_choice",
        "correct_answers": list(result.get("correct_answers") or ([result.get("correct_answer")] if result.get("correct_answer") else [])),
        "topic_id": result.get("topic_id") or topic_id, "topic_name": result.get("topic_name", ""),
        "concept_id": result.get("concept_id", ""), "concept_name": "",
        "source_subtopic_ids": list(result.get("source_subtopic_ids") or []),
        "concept_origin": result.get("concept_origin", ""),
        "concept_plan_id": result.get("concept_plan_id", ""),
        "assessment_capacity": int(result.get("assessment_capacity") or 0),
        "difficulty": result.get("question_difficulty") or attempt.get("difficulty", "easy"),
        "explanation": result.get("explanation", ""),
        "source_chunk_ids": list(result.get("source_chunk_ids") or []),
        "validation_outcome": result.get("validation_outcome", "accepted"),
    } for result in results]
    return {
        "quiz_id": attempt.get("quiz_id"), "document_id": attempt.get("document_id"),
        "title": attempt.get("document_id"), "difficulty": attempt.get("difficulty", "easy"),
        "topic_id": topic_id,
        "topic_name": next((question["topic_name"] for question in questions if question["topic_name"]), ""),
        "assessment_scope": "document" if topic_id == "document" else "topic",
        "assessment_plan": {"planner_version": "persisted_attempt_snapshot", "total_questions": len(questions)},
        "question_count": len(questions),
        "created_at": attempt.get("started_at") or attempt.get("completed_at") or utc_now_iso(),
        "questions": questions,
    }


def load_quiz_for_retake(attempt_id: str, student_id: str = LEGACY_USER_ID) -> dict:
    """Resolve the exact persisted quiz behind a completed attempt."""
    attempt = get_quiz_history_attempt(attempt_id, student_id)
    if not attempt:
        raise ValueError("Completed quiz attempt was not found.")
    quiz_id = str(attempt.get("quiz_id") or "")
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else None
    if not quiz or not quiz.get("questions"):
        quiz = _quiz_from_attempt_snapshot(attempt)
    if not quiz or not quiz.get("questions"):
        raise ValueError("The persisted quiz questions are no longer available.")
    return {"quiz": quiz, "source_attempt_id": attempt_id, "attempt_summary": get_quiz_attempt_summary(quiz["quiz_id"], student_id)}


def update_quiz_progress(
    document_id: str,
    difficulty: str,
    topic_id: str,
    student_id: str = LEGACY_USER_ID,
    quiz_id: str | None = None,
    question_id: int | None = None,
    selected_answer: str | list[str] | None = None,
    current_question_index: int | None = None,
    answers: dict | None = None,
) -> dict:
    """Autosave the learner's in-progress answers and current position for one exact quiz.

    `answers`, when given, is the Quiz Player's full local answer snapshot and REPLACES the saved
    answers (so a cleared multi-select is cleared server-side too, and a debounced save can never
    drop an answer made just before navigating). `question_id`/`selected_answer` still merge one
    answer into the saved set for older callers.

    With `quiz_id`, resolves exactly that quiz artifact via get_quiz_by_id -- never a different one
    from the same (document, topic, difficulty) slot, and never falls back to "the newest quiz in
    this slot" (see load_quiz_with_attempt for the same rule applied to reads). Without a quiz_id,
    keeps the older slot lookup for callers that have no specific artifact to save against.

    This never grades or completes the attempt -- it only mirrors the Quiz Player's local answer/
    position state so Resume can restore it. Grading and completion happen once, explicitly, in
    submit_quiz_attempt.
    """
    difficulty = difficulty.lower().strip()
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else get_quiz(document_id, difficulty, topic_id, student_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    if quiz.get("document_id") != document_id or quiz.get("difficulty") != difficulty or quiz.get("topic_id") != topic_id:
        raise ValueError("The submitted quiz identity does not match its persisted questions.")

    questions = quiz.get("questions", [])
    previous = get_latest_attempt(
        document_id, difficulty, topic_id, student_id, quiz_id=quiz.get("quiz_id"), live_only=True,
    ) or {}
    if previous.get("completed"):
        raise ValueError("This quiz attempt is already completed. Retake it to answer again.")
    questions_by_id = {str(question.get("id")): question for question in questions}
    if answers is not None:
        snapshot = {}
        for key, value in answers.items():
            question = questions_by_id.get(str(key))
            if not question:
                raise ValueError("question_id was not found in this quiz.")
            saved_answer = _saved_answer(question, value)
            if saved_answer is not None:
                snapshot[str(key)] = saved_answer
        answers = snapshot
    else:
        answers = {str(key): value for key, value in (previous.get("answers") or {}).items()}

    if question_id is not None:
        target_question = next(
            (question for question in questions if int(question.get("id")) == question_id),
            None,
        )
        if not target_question:
            raise ValueError("question_id was not found in this quiz.")
        saved_answer = _saved_answer(target_question, selected_answer)
        if saved_answer is None:
            answers.pop(str(question_id), None)
        else:
            answers[str(question_id)] = saved_answer

    total = len(questions)
    now = utc_now_iso()
    position = int(previous.get("current_question_index") or 0) if current_question_index is None else int(current_question_index)
    position = max(0, min(position, max(total - 1, 0)))
    progress = {
        "quiz_id": quiz["quiz_id"],
        "started_at": previous.get("started_at") or now,
        "completed_at": None,
        "completed": False,
        "answered": len(answers),
        "total": total,
        "current_question_index": position,
        "answers": answers,
    }
    return save_quiz_progress(document_id, difficulty, progress, topic_id, student_id)


def submit_quiz_attempt(
    document_id: str,
    difficulty: str,
    topic_id: str,
    answers: dict[str, str | list[str]],
    student_id: str = LEGACY_USER_ID,
    quiz_id: str | None = None,
    allow_unanswered: bool = False,
) -> dict:
    """Grade a complete answer set once and append a new attempt for the saved quiz.

    With `allow_unanswered` (the Quiz Player's "Submit Anyway"), questions left without an answer
    are persisted with an empty selection and graded as not correct, so Results/Review can show
    them as unanswered. Without it, every question must be answered exactly once.
    """
    difficulty = difficulty.lower().strip()
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else get_quiz(document_id, difficulty, topic_id, student_id)
    if not quiz and quiz_id:
        source_attempt = get_latest_completed_attempt_for_quiz(quiz_id, student_id)
        quiz = _quiz_from_attempt_snapshot(source_attempt) if source_attempt else None
    if not quiz:
        raise ValueError("The persisted quiz questions are no longer available.")
    if quiz.get("document_id") != document_id or quiz.get("difficulty") != difficulty or quiz.get("topic_id") != topic_id:
        raise ValueError("The submitted quiz identity does not match its persisted questions.")
    questions = list(quiz.get("questions") or [])
    questions_by_id = {str(question.get("id")): question for question in questions}
    normalized_answers = {}
    for key, value in answers.items():
        question = questions_by_id.get(str(key))
        if question is not None and _question_type(question) == "fill_blank":
            text = _fill_blank_answer(value)
            if text:
                normalized_answers[str(key)] = [text]
            elif not allow_unanswered:
                raise ValueError("Every quiz question must be answered exactly once before submission.")
        elif not (allow_unanswered and value in (None, "", [])):
            normalized_answers[str(key)] = _selected_answers(value)
    expected_ids = {str(question.get("id")) for question in questions}
    if allow_unanswered:
        if not set(normalized_answers) <= expected_ids:
            raise ValueError("A submitted answer does not belong to this quiz.")
    elif set(normalized_answers) != expected_ids:
        raise ValueError("Every quiz question must be answered exactly once before submission.")
    for question in questions:
        if str(question.get("id")) not in normalized_answers:
            continue
        selected = normalized_answers[str(question.get("id"))]
        question_type = _question_type(question)
        if question_type == "fill_blank":
            continue
        valid_letters = set("ABCD"[:len(question.get("options") or [])])
        if any(answer not in valid_letters for answer in selected):
            raise ValueError("A selected answer does not exist for its question.")
        if question_type == "multi_select" and len(selected) < 2:
            raise ValueError("multi_select questions require at least two selected answers.")
        if question_type != "multi_select" and len(selected) != 1:
            raise ValueError(f"{question_type} questions require exactly one selected answer.")
    results = []
    for question in questions:
        question_id = str(question.get("id"))
        selected_answers = normalized_answers.get(question_id, [])
        if _question_type(question) == "fill_blank":
            # Deterministic: normalized exact match against the explicitly stored accepted answers.
            correct_answers = _fill_blank_correct_answers(question)
            is_correct = bool(selected_answers) and fill_blank_is_correct(selected_answers[0], correct_answers)
        else:
            correct_answers = sorted(_correct_answers(question))
            is_correct = selected_answers == correct_answers
        results.append({
            "question_id": int(question_id),
            "question": question.get("question", ""),
            "options": list(question.get("options", [])),
            "selected_answer": selected_answers[0] if selected_answers else "",
            "selected_answers": selected_answers,
            "correct_answer": correct_answers[0],
            "correct_answers": correct_answers,
            "question_type": _question_type(question),
            "is_correct": is_correct,
            "question_difficulty": question.get("difficulty", difficulty),
            "validation_outcome": question.get("validation_outcome", "accepted"),
            "topic_id": question.get("topic_id", topic_id),
            "topic_name": question.get("topic_name", ""),
            "concept_id": question.get("concept_id", ""),
            "source_subtopic_ids": list(question.get("source_subtopic_ids") or []),
            "concept_origin": question.get("concept_origin", ""),
            "concept_plan_id": question.get("concept_plan_id", ""),
            "assessment_capacity": int(question.get("assessment_capacity") or 0),
            "evidence_requirement_version": "concept_coverage_v1",
            "explanation": question.get("explanation", ""),
            "source_chunk_ids": list(question.get("source_chunk_ids") or []),
        })
    score = sum(int(result["is_correct"]) for result in results)
    total = len(results)
    summary = get_quiz_attempt_summary(quiz["quiz_id"], student_id)
    now = utc_now_iso()
    saved = save_quiz_progress(document_id, difficulty, {
        "attempt_id": str(uuid4()),
        "quiz_id": quiz["quiz_id"],
        "started_at": now,
        "completed_at": now,
        "submitted_at": now,
        "score": score,
        "answered": len(normalized_answers),
        "total": total,
        "completed": True,
        "attempt_number": int(summary["attempts"]) + 1,
        "percentage": round(100.0 * score / total, 2) if total else 0,
        "answers": {key: (value if len(value) > 1 else value[0]) for key, value in normalized_answers.items()},
        "question_results": results,
    }, topic_id, student_id)
    represented_topics = sorted({str(result["topic_id"]) for result in results if result.get("topic_id")})
    saved["mastery_by_topic"] = {
        represented_topic: recompute_topic_mastery(student_id, document_id, represented_topic)
        for represented_topic in represented_topics
    }
    if len(represented_topics) == 1:
        saved["mastery"] = saved["mastery_by_topic"][represented_topics[0]]
    saved["attempt_summary"] = get_quiz_attempt_summary(quiz["quiz_id"], student_id)
    return saved


def list_completed_quiz_attempts(
    document_id: str | None = None,
    difficulty: str | None = None,
    student_id: str = LEGACY_USER_ID,
) -> list[dict]:
    """Return compact summaries for the Quiz History UI."""
    attempts = load_quiz_history(document_id, difficulty, student_id)
    titles = get_quiz_titles([attempt.get("quiz_id") for attempt in attempts], student_id)
    summaries = []
    for attempt in attempts:
        total = int(attempt.get("total", 0))
        score = int(attempt.get("score", 0))
        summaries.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "quiz_id": attempt.get("quiz_id"),
                "document_id": attempt.get("document_id"),
                "title": titles.get(attempt.get("quiz_id")) or "Untitled Quiz",
                "difficulty": attempt.get("difficulty", "medium"),
                "topic_id": attempt.get("topic_id") or "document",
                "topic_name": next((result.get("topic_name") for result in attempt.get("question_results", []) if result.get("topic_name")), ""),
                "score": score,
                "total": total,
                "percentage": round((score / total) * 100) if total else 0,
                "attempt_number": attempt.get("attempt_number", 0),
                "completed_at": attempt.get("completed_at") or attempt.get("submitted_at"),
            }
        )
    return summaries


def load_completed_quiz_attempt(attempt_id: str, student_id: str = LEGACY_USER_ID) -> dict:
    """Return one completed attempt with question snapshots for review.

    Also attaches display metadata of that attempt's OWN quiz (looked up by its exact quiz_id,
    never a sibling in the same slot) for the Results screen: title, difficulty, generating model,
    partial-count info, and each question's concept name. The persisted grading in
    question_results is returned unchanged.
    """
    attempt = get_quiz_history_attempt(attempt_id, student_id)
    if not attempt:
        raise ValueError("Quiz history attempt was not found.")
    quiz = get_quiz_by_id(attempt["quiz_id"], student_id) if attempt.get("quiz_id") else None
    if quiz and quiz.get("quiz_id") == attempt.get("quiz_id"):
        plan = quiz.get("assessment_plan") or {}
        attempt["quiz"] = {
            "quiz_id": quiz["quiz_id"],
            "title": quiz.get("title") or "",
            "difficulty": quiz.get("difficulty") or attempt.get("difficulty"),
            "generation_model": quiz.get("generation_model") or plan.get("generation_model"),
            "partial": bool(plan.get("partial")),
            "requested_count": plan.get("requested_count") or plan.get("target_questions"),
            "question_count": len(quiz.get("questions") or []),
        }
        concept_names = {
            str(question.get("id")): question.get("concept_name") or ""
            for question in quiz.get("questions") or []
        }
        for result in attempt.get("question_results") or []:
            result.setdefault("concept_name", concept_names.get(str(result.get("question_id")), ""))
    return attempt


def clear_quiz_progress(
    document_id: str, difficulty: str, topic_id: str, student_id: str = LEGACY_USER_ID,
    quiz_id: str | None = None,
) -> dict:
    """Reset current answers for one saved quiz.

    With `quiz_id`, resolves and resets exactly that quiz artifact -- a sibling quiz sharing the
    same (document, topic, difficulty) slot keeps its own progress untouched. Without it, keeps the
    older slot-wide behavior for callers that have no quiz_id to give.
    """
    quiz = get_quiz_by_id(quiz_id, student_id) if quiz_id else get_quiz(document_id, difficulty, topic_id, student_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    reset_quiz_progress(document_id, difficulty, topic_id, student_id, quiz_id=quiz.get("quiz_id") if quiz_id else None)
    return {"student_id": student_id, "document_id": document_id, "topic_id": topic_id, "difficulty": difficulty, "reset": True}


def explain_quiz_question(
    document_id: str,
    difficulty: str,
    topic_id: str,
    question_id: int,
    owner_id: str = LEGACY_USER_ID,
) -> dict:
    """Return a cached or on-demand RAG explanation for one answered question."""
    quiz = get_quiz(document_id, difficulty, topic_id, owner_id)
    if not quiz:
        raise ValueError("Quiz has not been generated for this document and difficulty.")
    question = next(
        (item for item in quiz.get("questions", []) if int(item.get("id")) == question_id),
        None,
    )
    if not question:
        raise ValueError("question_id was not found in this quiz.")
    if question.get("explanation"):
        return {
            "document_id": document_id,
            "topic_id": topic_id,
            "difficulty": difficulty,
            "question_id": question_id,
            "explanation": question["explanation"],
            "source_chunk_ids": question.get("source_chunk_ids", []),
            "cache_hit": True,
        }
    explanation_key = f"{quiz_cache_key(document_id, difficulty, topic_id, owner_id)}::{question_id}"
    cached = get_quiz_explanation(explanation_key, owner_id)
    if cached:
        return {**cached, "cache_hit": True}

    result = explain_quiz_answer(
        owner_id=owner_id,
        document_id=document_id,
        question=question["question"],
        options=question["options"],
        correct_answer=(_fill_blank_correct_answers(question) if _question_type(question) == "fill_blank"
                        else _correct_answers(question)),
    )
    saved = {
        "document_id": document_id,
        "difficulty": difficulty,
        "question_id": question_id,
        **result,
        "created_at": utc_now_iso(),
    }
    save_quiz_explanation(explanation_key, saved, owner_id)
    return {**saved, "cache_hit": False}
