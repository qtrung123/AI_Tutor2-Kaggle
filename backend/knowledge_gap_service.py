"""Deterministic, read-only knowledge-gap projection over topic mastery."""

from backend.quiz_store import list_topic_mastery


GAP_RULE_VERSION = "mastery_level_v1"
_SEVERITY_RANK = {"high": 0, "moderate": 1}


def mastery_to_gap(mastery: dict) -> dict | None:
    level = str(mastery.get("mastery_level") or "")
    if not mastery.get("has_evidence") or not mastery.get("has_sufficient_evidence"):
        return None
    severity = {"Weak": "high", "Developing": "moderate"}.get(level)
    if severity is None:
        return None
    return {
        "user_id": mastery["student_id"],
        "document_id": mastery["document_id"],
        "topic_id": mastery["topic_id"],
        "topic_name": mastery.get("topic_name") or mastery["topic_id"],
        "mastery_score": float(mastery.get("mastery_score") or 0),
        "mastery_level": level,
        "severity": severity,
        "assessment_capacity": int(mastery.get("assessment_capacity") or 0),
        "distinct_concepts_assessed": int(mastery.get("distinct_concepts_assessed") or 0),
        "concept_coverage_ratio": float(mastery.get("concept_coverage_ratio") or 0),
        "required_concept_coverage": float(mastery.get("required_concept_coverage") or 0),
        "answered_questions": int(mastery.get("answered_questions") or 0),
        "completed_attempts": int(mastery.get("completed_attempts") or 0),
        "has_evidence": True,
        "has_sufficient_evidence": True,
        "rule_version": GAP_RULE_VERSION,
    }


def detect_knowledge_gaps(user_id: str, document_id: str | None = None) -> list[dict]:
    gaps = [gap for row in list_topic_mastery(user_id, document_id) if (gap := mastery_to_gap(row))]
    return sorted(
        gaps,
        key=lambda gap: (
            _SEVERITY_RANK[gap["severity"]],
            gap["mastery_score"],
            gap["concept_coverage_ratio"],
            gap["topic_id"],
        ),
    )
