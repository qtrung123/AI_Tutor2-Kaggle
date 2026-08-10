"""Deterministic next-action projection over documents, mastery, and Phase 4 gaps."""

from backend.indexed_document_store import list_indexed_documents
from backend.knowledge_gap_service import mastery_to_gap
from backend.mastery_service import calculate_mastery
from backend.quiz_store import list_completed_answer_snapshots


RECOMMENDATION_RULE_VERSION = "learning_actions_v1"
_CATEGORY_RANK = {
    "weak_mastery": 0,
    "developing_mastery": 1,
    "insufficient_evidence": 2,
    "not_assessed": 3,
}


def _reason(row: dict, reason_code: str) -> str:
    score = round(float(row["mastery_score"]), 1)
    concepts = int(row["distinct_concepts_assessed"])
    capacity = int(row["assessment_capacity"])
    if reason_code == "weak_mastery":
        return f"Mastery is {score}% with sufficient evidence across {concepts} of {capacity} assessable concepts."
    if reason_code == "developing_mastery":
        return f"Mastery is {score}% with sufficient evidence across {concepts} of {capacity} assessable concepts."
    if reason_code == "insufficient_evidence":
        return f"Assessment evidence covers {concepts} of {capacity} assessable concepts, below the required coverage."
    return "This topic has no completed assessment evidence yet."


def mastery_to_recommendation(row: dict) -> dict | None:
    level = row["mastery_level"]
    gap = mastery_to_gap(row)
    if gap:
        reason_code = "weak_mastery" if gap["severity"] == "high" else "developing_mastery"
        priority = gap["severity"]
        recommendation_type = "review_topic" if gap["severity"] == "high" else "practice_topic"
        action_label = "Practice topic again"
        is_gap = True
    elif row["has_evidence"] and not row["has_sufficient_evidence"]:
        reason_code, priority, recommendation_type = "insufficient_evidence", "needs_more_evidence", "continue_assessment"
        action_label, is_gap = "Continue assessment", False
    elif not row["has_evidence"]:
        reason_code, priority, recommendation_type = "not_assessed", "not_assessed", "start_assessment"
        action_label, is_gap = "Start assessment", False
    else:
        return None
    return {
        "user_id": row["student_id"],
        "document_id": row["document_id"],
        "document_name": row["document_name"],
        "topic_id": row["topic_id"],
        "topic_name": row["topic_name"],
        "recommendation_type": recommendation_type,
        "priority": priority,
        "reason_code": reason_code,
        "reason_text": _reason(row, reason_code),
        "mastery_score": row["mastery_score"],
        "mastery_level": level,
        "concept_coverage_ratio": row["concept_coverage_ratio"],
        "distinct_concepts_assessed": row["distinct_concepts_assessed"],
        "assessment_capacity": row["assessment_capacity"],
        "answered_questions": row["answered_questions"],
        "completed_attempts": row["completed_attempts"],
        "has_evidence": row["has_evidence"],
        "has_sufficient_evidence": row["has_sufficient_evidence"],
        "is_knowledge_gap": is_gap,
        "primary_action": {
            "type": "practice_topic",
            "label": action_label,
            "navigation_context": {
                "page": "practice", "assessment_scope": "topic",
                "document_id": row["document_id"], "topic_id": row["topic_id"],
            },
        },
        "secondary_actions": [],
        "rule_version": RECOMMENDATION_RULE_VERSION,
    }


def generate_recommendations(user_id: str, document_id: str | None = None) -> list[dict]:
    recommendations = []
    for document in list_indexed_documents(user_id):
        if document_id is not None and document["document_id"] != document_id:
            continue
        for topic in document.get("topics") or []:
            topic_id = str(topic.get("topic_id") or "").strip()
            if not topic_id:
                continue
            history = list_completed_answer_snapshots(user_id, document["document_id"], topic_id)
            row = {
                "student_id": user_id,
                "document_id": document["document_id"],
                "document_name": document.get("display_name") or document["document_id"],
                "topic_id": topic_id,
                "topic_name": topic.get("name") or topic_id,
                **calculate_mastery(history, len({item["attempt_id"] for item in history})),
            }
            recommendation = mastery_to_recommendation(row)
            if recommendation:
                recommendations.append(recommendation)
    return sorted(
        recommendations,
        key=lambda item: (
            _CATEGORY_RANK[item["reason_code"]],
            item["mastery_score"] if item["has_evidence"] else 0,
            item["concept_coverage_ratio"],
            item["document_id"],
            item["topic_id"],
        ),
    )
