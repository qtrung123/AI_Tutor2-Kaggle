import json
import re
import time
from dataclasses import asdict, dataclass

from langchain_ollama import ChatOllama

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
    return f"""
You are a cautious reviewer of one multiple-choice question. Your judgment is a runtime quality signal, not ground truth.
Use only the cited context. Do not repair the question and do not use outside knowledge.

Selected topic: {topic_name}
Requested difficulty: {requested_difficulty}
Candidate: {json.dumps(candidate, ensure_ascii=False)}
Cited context: {json.dumps(context, ensure_ascii=False)}

Return JSON only with this exact shape:
{{
  "question_supported": true,
  "correct_answer_supported": true,
  "explanation_supported": true,
  "meaningful_concept": true,
  "distractor_quality": true,
  "difficulty_match": true,
  "detected_difficulty": "easy",
  "reasons": []
}}

Hard grounding criteria:
- question_supported: the context establishes the premise and contains enough information to answer.
- correct_answer_supported: the declared correct option is directly supported.
- explanation_supported: every factual claim in the explanation is supported.

Quality criteria:
- meaningful_concept: assesses educationally meaningful topic knowledge, not incidental trivia unless the context makes that detail important.
- distractor_quality: all distractors are plausible, same-domain, and clearly incorrect from the context without invented factual claims.
- difficulty_match: the cognitive task reasonably matches {requested_difficulty}; allow normal judgment variation.

Evidence identity is owned and attached by the backend. Judge semantic support from the cited context.
""".strip()


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
