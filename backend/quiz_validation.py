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
    OLLAMA_KEEP_ALIVE,
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
    return validate_questions_semantics(
        [(question, cited_chunks, requested_difficulty, topic_name)], llm_factory
    )[0]


def _batch_prompt(items: list[tuple[dict, list[dict], str, str]]) -> str:
    candidates = []
    for index, (question, chunks, difficulty, topic_name) in enumerate(items):
        candidates.append({
            "item_id": str(index), "topic_name": topic_name,
            "requested_difficulty": difficulty,
            "candidate": {key: question[key] for key in ("question", "options", "correct_answer", "explanation")},
            "cited_context": [{"chunk_id": c["metadata"]["chunk_id"], "content": c["content"]} for c in chunks],
        })
    return f"""
You are a cautious reviewer of multiple independent multiple-choice questions.
Use only each item's own cited_context. Do not repair questions or use outside knowledge.
Return one independent verdict for every item_id, in the same order.
Evidence identity is backend-owned.

ITEMS: {json.dumps(candidates, ensure_ascii=False)}

Return JSON only: {{"results":[{{"item_id":"0","question_supported":true,
"correct_answer_supported":true,"explanation_supported":true,"meaningful_concept":true,
"distractor_quality":true,"difficulty_match":true,"detected_difficulty":"easy","reasons":[]}}]}}

Apply all criteria independently: question, correct answer, and explanation must be grounded;
the concept must be meaningful; distractors must be plausible, same-domain, and clearly wrong;
and the cognitive task must match the requested difficulty.
""".strip()


def validate_questions_semantics(
    items: list[tuple[dict, list[dict], str, str]], llm_factory=ChatOllama,
) -> list[SemanticValidationResult]:
    if not QUIZ_SEMANTIC_VALIDATION_ENABLED:
        return [SemanticValidationResult(False, QUIZ_VALIDATION_MODEL, VALIDATION_PROMPT_VERSION, {}, [], [], [], 0) for _ in items]
    if not items:
        return []

    started = time.perf_counter()
    last_error = None
    for _attempt in range(max(1, QUIZ_VALIDATION_ATTEMPTS)):
        try:
            response = llm_factory(
                model=QUIZ_VALIDATION_MODEL,
                temperature=0,
                format="json",
                num_ctx=QUIZ_VALIDATION_NUM_CTX,
                num_predict=max(500, len(items) * 300), keep_alive=OLLAMA_KEEP_ALIVE,
            ).invoke(_batch_prompt(items))
            payload = _extract_json(response.content)
            verdicts = payload.get("results")
            if len(items) == 1 and not isinstance(verdicts, list):
                verdicts = [{**payload, "item_id": "0"}]
            if not isinstance(verdicts, list) or len(verdicts) != len(items):
                raise ValueError("Semantic validator did not return one verdict per item.")
            latency = round((time.perf_counter() - started) * 1000)
            results = []
            for index, ((_question, chunks, _difficulty, _topic), verdict) in enumerate(zip(items, verdicts)):
                if str(verdict.get("item_id")) != str(index):
                    raise ValueError("Semantic validator item IDs are missing or out of order.")
                results.append(SemanticValidationResult(
                    True, QUIZ_VALIDATION_MODEL, VALIDATION_PROMPT_VERSION, verdict,
                    sorted({str(c["metadata"]["chunk_id"]) for c in chunks}),
                    [c for c in HARD_CRITERIA if verdict.get(c) is not True],
                    [c for c in QUALITY_CRITERIA if verdict.get(c) is not True], latency,
                ))
            return results
        except Exception as error:
            last_error = error
    raise ValueError(f"Semantic validator failed to return valid JSON: {last_error}")
