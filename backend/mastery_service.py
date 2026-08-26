import math

from config import (
    MASTERY_DEVELOPING_THRESHOLD,
    MASTERY_DIFFICULTY_WEIGHTS,
    MASTERY_MASTERED_THRESHOLD,
    MASTERY_MIN_QUESTIONS,
    MASTERY_MIN_CONCEPT_COVERAGE,
    MASTERY_PROFICIENT_THRESHOLD,
    MASTERY_QUALITY_WEIGHTS,
)
from backend.quiz_store import (
    list_completed_answer_snapshots,
    list_mastery_identities,
    save_topic_mastery,
)


MASTERY_FORMULA_VERSION = "weighted_accuracy_v1"


def mastery_config() -> dict:
    return {
        "difficulty_weights": dict(MASTERY_DIFFICULTY_WEIGHTS),
        "quality_weights": dict(MASTERY_QUALITY_WEIGHTS),
        "minimum_questions_required": MASTERY_MIN_QUESTIONS,
        "minimum_concept_coverage": MASTERY_MIN_CONCEPT_COVERAGE,
        "evidence_requirement_version": "concept_coverage_v1",
        "thresholds": {
            "developing": MASTERY_DEVELOPING_THRESHOLD,
            "proficient": MASTERY_PROFICIENT_THRESHOLD,
            "mastered": MASTERY_MASTERED_THRESHOLD,
        },
    }


def calculate_mastery(answer_rows: list[dict], completed_attempts: int = 0) -> dict:
    earned_weight = 0.0
    possible_weight = 0.0
    correct_answers = 0
    for answer in answer_rows:
        difficulty = str(answer.get("question_difficulty") or "easy").lower()
        outcome = str(answer.get("validation_outcome") or "accepted")
        effective_weight = MASTERY_DIFFICULTY_WEIGHTS.get(difficulty, 1.0) * MASTERY_QUALITY_WEIGHTS.get(outcome, 1.0)
        possible_weight += effective_weight
        if bool(answer.get("is_correct")):
            correct_answers += 1
            earned_weight += effective_weight

    answered_questions = len(answer_rows)
    has_evidence = answered_questions > 0
    capacities = [int(answer.get("assessment_capacity") or 0) for answer in answer_rows]
    assessment_capacity = max(capacities, default=0)
    distinct_concepts = {
        str(answer.get("concept_id")) for answer in answer_rows if str(answer.get("concept_id") or "").strip()
    }
    distinct_concepts_assessed = len(distinct_concepts)
    if assessment_capacity > 0:
        required_concepts = min(
            assessment_capacity,
            max(1, math.ceil(assessment_capacity * MASTERY_MIN_CONCEPT_COVERAGE)),
        )
        concept_coverage_ratio = min(1.0, distinct_concepts_assessed / assessment_capacity)
        has_sufficient_evidence = distinct_concepts_assessed >= required_concepts
        minimum_questions_required = required_concepts
    else:
        # Persisted quizzes created before concept planning retain the Phase 3 rule.
        required_concepts = 0
        concept_coverage_ratio = 0.0
        has_sufficient_evidence = answered_questions >= MASTERY_MIN_QUESTIONS
        minimum_questions_required = MASTERY_MIN_QUESTIONS
    score = round(100.0 * earned_weight / possible_weight, 2) if possible_weight else 0.0
    if not has_evidence:
        level = "Not assessed"
    elif not has_sufficient_evidence:
        level = "Insufficient evidence"
    elif score >= MASTERY_MASTERED_THRESHOLD:
        level = "Mastered"
    elif score >= MASTERY_PROFICIENT_THRESHOLD:
        level = "Proficient"
    elif score >= MASTERY_DEVELOPING_THRESHOLD:
        level = "Developing"
    else:
        level = "Weak"
    return {
        "mastery_score": score,
        "mastery_level": level,
        "earned_weight": round(earned_weight, 4),
        "possible_weight": round(possible_weight, 4),
        "correct_answers": correct_answers,
        "answered_questions": answered_questions,
        "completed_attempts": completed_attempts,
        "has_evidence": has_evidence,
        "has_sufficient_evidence": has_sufficient_evidence,
        "minimum_questions_required": minimum_questions_required,
        "assessment_capacity": assessment_capacity,
        "distinct_concepts_assessed": distinct_concepts_assessed,
        "concept_coverage_ratio": round(concept_coverage_ratio, 4),
        "required_concept_coverage": MASTERY_MIN_CONCEPT_COVERAGE,
        "required_concepts": required_concepts,
        "formula_version": MASTERY_FORMULA_VERSION,
        "formula_config": mastery_config(),
    }


def recompute_topic_mastery(student_id: str, document_id: str, topic_id: str) -> dict:
    history = list_completed_answer_snapshots(student_id, document_id, topic_id)
    attempt_ids = {row["quiz_id"] for row in history}
    result = {
        "student_id": student_id,
        "document_id": document_id,
        "topic_id": topic_id,
        **calculate_mastery(history, len(attempt_ids)),
    }
    return save_topic_mastery(result)


def recompute_all_mastery(student_id: str | None = None) -> list[dict]:
    return [
        recompute_topic_mastery(identity["student_id"], identity["document_id"], identity["topic_id"])
        for identity in list_mastery_identities(student_id)
    ]
