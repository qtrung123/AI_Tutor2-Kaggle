import json
import re
import time
from dataclasses import asdict, dataclass

from backend.llm_backend import ChatOllama
from backend.prompt_loader import render_prompt

from config import (
    QUIZ_SEMANTIC_VALIDATION_ENABLED,
    QUIZ_VALIDATION_ATTEMPTS,
    QUIZ_VALIDATION_MODEL,
    QUIZ_VALIDATION_NUM_CTX,
)


VALIDATION_PROMPT_VERSION = "semantic_grounding_v2_backend_evidence"
HARD_CRITERIA = (
    "question_supported",
    "correct_answer_supported",
    "explanation_supported",
)
QUALITY_CRITERIA = (
    "meaningful_concept",
    "distractor_quality",
    "difficulty_match",
)


@dataclass
class SemanticValidationResult:
    enabled: bool
    validator_model: str
    validator_prompt_version: str
    verdict: dict
    evidence_chunk_ids: list[str]
    hard_failures: list[str]
    quality_failures: list[str]
    latency_ms: int

    @property
    def hard_passed(self) -> bool:
        return not self.hard_failures

    @property
    def quality_passed(self) -> bool:
        return not self.quality_failures

    def to_dict(self) -> dict:
        return {**asdict(self), "hard_passed": self.hard_passed, "quality_passed": self.quality_passed}


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.IGNORECASE).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Semantic validator output must be a JSON object.")
    return value


def _prompt(question: dict, cited_chunks: list[dict], requested_difficulty: str, topic_name: str) -> str:
    context = [
        {
            "chunk_id": chunk["metadata"]["chunk_id"],
            "content": chunk["content"],
        }
        for chunk in cited_chunks
    ]
    candidate = {
        key: question[key]
        for key in ("question", "options", "correct_answer", "explanation")
    }
    return render_prompt(
        "quiz/semantic_validation.txt",
        topic_name=topic_name,
        concept=question.get('concept_name') or question.get('concept_id') or 'legacy',
        structural_lineage=json.dumps(question.get('source_subtopic_ids') or [], ensure_ascii=False),
        concept_origin=question.get('concept_origin') or 'legacy',
        requested_difficulty=requested_difficulty,
        candidate=json.dumps(candidate, ensure_ascii=False),
        cited_context=json.dumps(context, ensure_ascii=False),
    ).strip()


def validate_question_semantics(
    question: dict,
    cited_chunks: list[dict],
    requested_difficulty: str,
    topic_name: str,
    llm_factory=ChatOllama,
) -> SemanticValidationResult:
    if not QUIZ_SEMANTIC_VALIDATION_ENABLED:
        return SemanticValidationResult(False, QUIZ_VALIDATION_MODEL, VALIDATION_PROMPT_VERSION, {}, [], [], [], 0)

    cited_ids = {str(chunk["metadata"]["chunk_id"]) for chunk in cited_chunks}
    started = time.perf_counter()
    last_error = None
    for _attempt in range(max(1, QUIZ_VALIDATION_ATTEMPTS)):
        try:
            response = llm_factory(
                model=QUIZ_VALIDATION_MODEL,
                temperature=0,
                format="json",
                num_ctx=QUIZ_VALIDATION_NUM_CTX,
                num_predict=500,
            ).invoke(_prompt(question, cited_chunks, requested_difficulty, topic_name))
            verdict = _extract_json(response.content)
            evidence_ids = sorted(cited_ids)
            hard_failures = [criterion for criterion in HARD_CRITERIA if verdict.get(criterion) is not True]
            quality_failures = [criterion for criterion in QUALITY_CRITERIA if verdict.get(criterion) is not True]
            return SemanticValidationResult(
                True,
                QUIZ_VALIDATION_MODEL,
                VALIDATION_PROMPT_VERSION,
                verdict,
                evidence_ids,
                hard_failures,
                quality_failures,
                round((time.perf_counter() - started) * 1000),
            )
        except Exception as error:
            last_error = error
    raise ValueError(f"Semantic validator failed to return valid JSON: {last_error}")
