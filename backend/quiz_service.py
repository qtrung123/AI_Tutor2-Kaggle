import json
import re
import time
from collections import Counter
from uuid import uuid4

from langchain_ollama import ChatOllama

from backend.quiz_store import (
    delete_quiz as _delete_quiz_row,
    get_latest_attempt,
    get_latest_completed_attempt_for_quiz,
    get_quiz,
    get_quiz_by_id,
    get_quiz_explanation,
    get_quiz_attempt_summary,
    list_document_quizzes,
    list_completed_answer_snapshots,
    list_quiz_history as load_quiz_history,
    get_quiz_titles,
    get_quiz_history_attempt,
    invalidate_document_quizzes_for_topic_schema,
    quiz_cache_key,
    save_quiz,
    save_quiz_explanation,
    save_quiz_progress,
    reset_quiz_progress,
    save_quiz_validation_event,
    utc_now_iso,
)
from backend.quiz_units import (
    QUIZ_ALLOWED_QUESTION_COUNTS,
    QUIZ_ENGINE_VERSION,
    QUIZ_FIRST_CALL_DEADLINE_S,
    QUIZ_FOLLOWUP_DEADLINE_S,
    QUIZ_LLM_TIMEOUT_S,
    QUIZ_MAX_ITEMS_EXTRA,
    QUIZ_MAX_NEW_TOKENS,
    QUIZ_MIN_CALL_S,
    QUIZ_NUM_CTX,
    QUIZ_PROMPT_VERSION,
    QUIZ_STALL_LIMIT,
    QUIZ_TOKENS_PER_QUESTION,
    QUIZ_UNUSED_CHARS_PER_QUESTION,
    QUIZ_TOTAL_DEADLINE_S,
    QUIZ_FILL_BLANK_MAX_CALLS,
    build_fill_blank_prompt,
    build_generation_prompt,
    flashcard_hint_terms,
    ground_fill_blank_hints,
    build_study_units,
    fill_blank_is_correct,
    fill_blank_output_schema,
    fill_blank_target,
    normalize_fill_blank_answer,
    validate_fill_blank_candidate,
    candidate_target,
    context_budget,
    finalize_questions,
    followup_request,
    followup_units,
    max_llm_calls,
    output_schema_for,
    parse_candidates,
    public_unit,
    select_context_units,
    select_questions,
    validate_candidate,
    _rank as _rank_question,
)
from backend.flashcard_store import get_latest_flashcard_set_info, list_flashcards
from backend.model_registry import describe_generation_model
from backend.mastery_service import calculate_mastery, recompute_topic_mastery
from backend.rag_service import explain_quiz_answer
from backend import quiz_diagnostics
from backend.auth_store import LEGACY_USER_ID
from backend.indexed_document_store import list_indexed_documents as load_owned_documents
from backend.document_retrieval import get_document_chunks, get_schema_topic_evidence
from config import CHAT_MODEL

QUIZ_V2_ALLOWED_QUESTION_COUNTS = set(QUIZ_ALLOWED_QUESTION_COUNTS)
QUIZ_GENERATION_KEEP_ALIVE = "5m"

# Live Quiz path ("Study Units", see backend/quiz_units.py and _generate_quiz_from_units): no
# Planner, concept extraction, Question Blueprint, slots, or repair/fill loops. At most TWO LLM
# calls per request: one candidate-generation call and, only if the validated pool is short of
# question_count, one bounded top-up. The V2 Planner-based engines (_generate_topic_quiz_v2,
# _run_document_single_choice_quiz in backend/quiz_legacy_v2.py, built on
# backend/assessment_planner.py) are kept for comparison and are not on the live path.


class QuizGenerationError(ValueError):
    """Structured topic-quiz failure that is safe to expose through the API."""

    def __init__(
        self, message: str, *, stage: str, valid_questions: int = 0,
        target_questions: int = 12, failure_summary: list[str] | None = None,
        missing_slots: list[str] | None = None,
        rejection_reasons_by_slot: dict[str, list[str]] | None = None,
    ):
        super().__init__(message)
        missing_questions = max(0, target_questions - valid_questions)
        self.detail = {
            "code": "quiz_v2_generation_failed",
            "message": message,
            "stage": stage,
            "valid_questions": valid_questions,
            "target_questions": target_questions,
            "requested_count": target_questions,
            "valid_count": valid_questions,
            "missing_count": missing_questions,
            "missing_slots": list(missing_slots or []),
            "rejection_reasons_by_slot": {
                slot_id: list(reasons)
                for slot_id, reasons in (rejection_reasons_by_slot or {}).items()
            },
            "failure_summary": list(failure_summary or []),
        }


OPTION_LETTERS = {"A", "B", "C", "D"}


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
QUIZ_DIFFICULTIES = {"easy", "medium", "difficult"}
def _load_indexed_files(owner_id: str) -> dict:
    return {document["document_id"]: document for document in load_owned_documents(owner_id)}


def list_indexed_documents(owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """
    Return documents available for quiz generation.

    The hash is included internally so generated quizzes can remember which
    exact uploaded file version they belong to.
    """
    indexed_files = _load_indexed_files(owner_id)
    for file_name, info in indexed_files.items():
        invalidate_document_quizzes_for_topic_schema(
            file_name, int(info.get("topic_schema_version", 0)), owner_id
        )
    return [
        {
            "id": file_name,
            "title": file_name,
            "chunks": int(info.get("chunks", 0)),
            "hash": str(info.get("hash", "")),
            "topic_schema_version": int(info.get("topic_schema_version", 0)),
            "topics": list(info.get("topics") or []),
        }
        for file_name, info in indexed_files.items()
    ]


def _document_lookup(owner_id: str) -> dict[str, dict]:
    return {document["id"]: document for document in list_indexed_documents(owner_id)}


def _generate_with_deadline(llm, prompt: str, deadline_s: float) -> tuple[str, dict, bool]:
    """Stream the model's answer, stopping at a wall-clock deadline.

    Returns (text, response_metadata, cut). When the deadline is reached the text written so far
    is kept -- parse_candidates salvages every complete question in it -- so a slow GPU costs some
    candidates instead of the whole request (and never outlives the reverse proxy's time limit).
    """
    started = time.perf_counter()
    parts: list[str] = []
    metadata: dict = {}
    cut = False
    stream = llm.stream(prompt)
    try:
        for chunk in stream:
            parts.append(str(getattr(chunk, "content", "") or ""))
            metadata.update(getattr(chunk, "response_metadata", None) or {})
            if time.perf_counter() - started > deadline_s:
                cut = True
                break
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return "".join(parts), metadata, cut


def _generate_quiz_from_units(
    document: dict,
    scope: str,
    scope_topic_id: str,
    scope_topic_name: str,
    chunks: list[dict],
    difficulty: str,
    owner_id: str,
    model_id: str,
    regenerate: bool,
    question_count: int = 12,
    quiz_title: str | None = None,
    retrieval_ms: int = 0,
    fill_blank_count: int = 0,
    fill_blank_hints: list[str] | None = None,
) -> dict:
    """Live Quiz generation: one simple pipeline for ANY document.

    With `fill_blank_count` > 0 AND `fill_blank_hints` (flashcard terms) of which at least one is
    written in the document's excerpts, a bounded fill_blank step runs after the multiple-choice pool
    (see _generate_fill_blank_pool): up to that many validated fill_blank questions replace the
    lowest-ranked multiple-choice ones, so the total question count is unchanged. Without grounded
    hints no fill_blank call is made. The live entry point asks for fill_blank_target(count) with
    the document's persisted flashcards as hints.

        chunks -> excerpts -> context that fits a fixed budget
        -> call 1 writes a surplus of candidates (12 -> 15, 15 -> 18, 18 -> 22, 20 -> 24)
        -> parse -> validate -> the valid ones join the pool -> enough? stop
        -> otherwise another call (at most 4 for 12/15 questions, 5 for 18/20) that writes a
           moderate batch of only what is missing, is shown every existing question with the
           sentence it used, and prefers excerpts no call has shown -> repeat
        -> best `question_count` candidates -> quiz

    `question_count` is a target, not a condition of success: the quiz holds
    min(valid candidates, question_count) questions and is "partial" when the calls run out first
    (or the time budget does, or two calls in a row add nothing). Only when there is not a single
    valid candidate does generation fail. No rule is relaxed and nothing is invented to reach the
    target; every question keeps its evidence in the context the model was shown. No Planner,
    topics, slots, coverage requirement or per-question calls. Provenance (excerpt and
    source_chunk_ids) is derived by the backend from where the model's evidence_quote is found.
    """
    diag = quiz_diagnostics.get_current()
    total_started = time.perf_counter()
    timings = {"topic_chunk_retrieval_ms": retrieval_ms}
    llm_calls = 0
    generation_run_id = str(uuid4())
    scope_topic = {"topic_id": scope_topic_id, "name": scope_topic_name}
    scope_label = scope_topic_name if scope == "topic" else str(document.get("title") or document["id"])

    stage_started = time.perf_counter()
    diag.record_retrieved_chunks(chunks)
    units = build_study_units(chunks)
    timings["context_grouping_ms"] = round((time.perf_counter() - stage_started) * 1000)
    timings["context_group_count"] = len(units)
    if not units:
        raise QuizGenerationError(
            "No usable document context was found for a grounded quiz.",
            stage="context_grouping",
            target_questions=question_count,
        )
    units_by_id = {unit["unit_id"]: unit for unit in units}
    pool_target = candidate_target(question_count)
    max_calls = max_llm_calls(question_count)
    # The model every call below is sent to (ChatOllama(model=model_id)); stored with the quiz.
    generation_model = describe_generation_model(model_id)

    accepted: list[dict] = []
    shown_ids: set[str] = set()
    validation_results = {
        "accepted": 0, "accepted_with_warnings": 0, "rejected": 0,
        "hard_rejections": 0, "quality_warnings": 0, "reasons": [],
        # rejected == duplicate_rejections + grounding_rejections + structural_rejections
        #           + response_failures (a whole-call failure is not a per-candidate outcome).
        "duplicate_rejections": 0, "grounding_rejections": 0, "structural_rejections": 0,
        "response_failures": 0,
        # which rule rejected the candidates (quote_not_found, answer_not_in_context, duplicate_evidence, ...)
        "rejection_codes": {},
    }
    generation_ms = validation_ms = model_load_ms = prompt_eval_ms = 0
    token_generation_ms = model_invocation_ms = initial_generation_ms = followup_generation_ms = 0
    candidates_requested_total = candidates_returned_total = 0
    candidate_counter = 0
    call_log: list[dict] = []
    stalled_calls = 0
    stop_reason = ""

    for call_number in range(1, max_calls + 1):
        first_call = call_number == 1
        elapsed_s = time.perf_counter() - total_started
        remaining_s = QUIZ_TOTAL_DEADLINE_S - elapsed_s
        if first_call:
            requested = pool_target
            shown_units = select_context_units(units, context_budget(requested))
            avoid_stems = avoid_quotes = None
            deadline_s = min(QUIZ_FIRST_CALL_DEADLINE_S, remaining_s)
        else:
            missing = question_count - len(accepted)
            if missing <= 0:
                break
            if stalled_calls >= QUIZ_STALL_LIMIT:
                stop_reason = f"stopped after {stalled_calls} calls in a row added no valid question"
                break
            if remaining_s < QUIZ_MIN_CALL_S:
                stop_reason = "time budget used up"
                break
            requested = followup_request(missing)
            budget = context_budget(requested)
            # Prefer excerpts no call has shown. Once every excerpt was shown, prefer the text no
            # accepted question was built on (least-used excerpts first) and only fall back to the
            # used passages when too little is left for what is missing: a passage can hold a further,
            # different fact. Repeats are stopped by the duplicate checks and the stall guard.
            shown_units = select_context_units(units, budget, exclude_ids=shown_ids)
            if not shown_units:
                shown_units = followup_units(units, accepted, budget, QUIZ_UNUSED_CHARS_PER_QUESTION * missing)
            avoid_stems = [question["question"] for question in accepted]
            avoid_quotes = [question["_meta"]["quote"] for question in accepted]
            deadline_s = min(QUIZ_FOLLOWUP_DEADLINE_S, remaining_s)
            print(f"[quiz-units-followup] call={call_number}/{max_calls} missing={missing} asking={requested}")
            diag.record_retry(
                concept_id="pool", original_attempt=1, retry_attempt=call_number - 1,
                reason=f"{len(accepted)}/{question_count} valid questions after call {call_number - 1}",
                stage="validation",
            )
        shown_ids.update(unit["unit_id"] for unit in shown_units)
        # Grounding is judged against everything the model has been shown so far.
        context_units = [unit for unit in units if unit["unit_id"] in shown_ids]
        prompt = build_generation_prompt(scope_label, difficulty, shown_units, requested, avoid_stems, avoid_quotes)
        material_chars = sum(unit["char_count"] for unit in shown_units)
        output_schema = output_schema_for(requested, material_chars)
        num_predict = min(QUIZ_MAX_NEW_TOKENS, max(600, (requested + QUIZ_MAX_ITEMS_EXTRA) * QUIZ_TOKENS_PER_QUESTION))
        unit_ids = ",".join(unit["unit_id"] for unit in shown_units)[:120]
        diag.record_context(
            stage="generator" if first_call else "repair",
            num_chunks=len(shown_units),
            total_chars=sum(unit["char_count"] for unit in shown_units),
            prompt=prompt,
            concept_id=unit_ids,
        )
        stage_started = time.perf_counter()
        llm_calls += 1
        candidates_requested_total += requested
        # One fixed context window for every call so Ollama never reloads the model between them;
        # the evidence per call is bounded by context_budget whatever the document size.
        temperature = 0.1 if first_call else 0.25
        llm = ChatOllama(
            model=model_id,
            reasoning=False,
            temperature=temperature,
            format=output_schema,
            num_ctx=QUIZ_NUM_CTX,
            num_predict=num_predict,
            keep_alive=QUIZ_GENERATION_KEEP_ALIVE,
            client_kwargs={"timeout": min(QUIZ_LLM_TIMEOUT_S, max(30, deadline_s))},
        )
        invocation_started = time.perf_counter()
        call_invocation_ms = 0
        metadata: dict = {}
        parse_report: dict = {}
        cut = False
        error_text = ""
        try:
            response_text, metadata, cut = _generate_with_deadline(llm, prompt, deadline_s)
            call_invocation_ms = round((time.perf_counter() - invocation_started) * 1000)
            if cut:
                validation_results["reasons"].append(f"call {call_number}: time limit reached; kept the complete questions written so far")
                timings["deadline_hit"] = True
            model_load_ms += round(float(metadata.get("load_duration") or 0) / 1_000_000)
            prompt_eval_ms += round(float(metadata.get("prompt_eval_duration") or 0) / 1_000_000)
            token_generation_ms += round(float(metadata.get("eval_duration") or 0) / 1_000_000)
            candidates = parse_candidates(response_text, parse_report)
            candidates_returned_total += len(candidates)
            diag.record_llm_call(
                stage="generator" if first_call else "repair", model=model_id,
                elapsed_ms=call_invocation_ms, success=True, concept_id=unit_ids,
                attempt=call_number,
                input_tokens=int(metadata["prompt_eval_count"]) if "prompt_eval_count" in metadata else None,
                output_tokens=int(metadata["eval_count"]) if "eval_count" in metadata else None,
            )
        except Exception as error:
            call_invocation_ms = call_invocation_ms or round((time.perf_counter() - invocation_started) * 1000)
            candidates = []
            error_text = f"{type(error).__name__}: {error}"[:200]
            validation_results["rejected"] += requested
            validation_results["response_failures"] += requested
            validation_results["reasons"].append(f"response: {error}")
            diag.record_llm_call(
                stage="generator" if first_call else "repair", model=model_id,
                elapsed_ms=call_invocation_ms, success=False, concept_id=unit_ids,
                attempt=call_number, exception_type=type(error).__name__, reason=str(error)[:200],
            )
        elapsed = round((time.perf_counter() - stage_started) * 1000)
        generation_ms += elapsed
        model_invocation_ms += call_invocation_ms
        if first_call:
            initial_generation_ms += elapsed
        else:
            followup_generation_ms += elapsed

        validation_started = time.perf_counter()
        accepted_before, rejected_here, codes_here = len(accepted), 0, {}
        for raw in candidates:
            candidate_counter += 1
            try:
                normalized, warnings = validate_candidate(
                    raw, context_units, accepted, difficulty, len(accepted) + 1, scope_topic, candidate_counter,
                )
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                category = getattr(error, "category", "structure")
                validation_results["rejected"] += 1
                rejected_here += 1
                validation_results["hard_rejections"] += 1
                validation_results["reasons"].append(str(error))
                bucket = category if category in {"duplicate", "grounding"} else "structural"
                validation_results[f"{bucket}_rejections"] += 1
                code = getattr(error, "code", None) or bucket
                codes_here[code] = codes_here.get(code, 0) + 1
                validation_results["rejection_codes"][code] = validation_results["rejection_codes"].get(code, 0) + 1
                print(f"[quiz-units-validation] call={call_number} discarded candidate ({bucket}): {error}")
                continue
            accepted.append(normalized)
            validation_results["accepted_with_warnings" if warnings else "accepted"] += 1
            validation_results["quality_warnings"] += len(warnings)
            save_quiz_validation_event({
                "generation_run_id": generation_run_id,
                "owner_id": owner_id,
                "document_id": document["id"],
                "document_hash": document.get("hash", ""),
                "topic_id": scope_topic_id,
                "topic_schema_version": int(document.get("topic_schema_version", 0)),
                "difficulty": difficulty,
                "batch_index": 1,
                "generation_attempt": call_number,
                "candidate_index": len(accepted),
                "generator_model": model_id,
                "generation_prompt_version": QUIZ_PROMPT_VERSION,
                "validator_model": "deterministic-context-grounding",
                "validator_prompt_version": QUIZ_ENGINE_VERSION,
                "candidate_question": {key: value for key, value in normalized.items() if key != "_meta"},
                "cited_chunk_ids": normalized["source_chunk_ids"],
                "evidence_chunk_ids": normalized["source_chunk_ids"],
                "hard_passed": True,
                "quality_passed": not warnings,
                "accepted": True,
                "outcome": normalized["validation_outcome"],
                "verdict": {"mode": "deterministic", "warnings": warnings},
                "rejection_reasons": warnings,
                "latency_ms": 0,
            })
        validation_ms += round((time.perf_counter() - validation_started) * 1000)
        added = len(accepted) - accepted_before
        stalled_calls = 0 if added else stalled_calls + 1

        # One line per call: the evidence for "the model wrote few" versus "the output was cut".
        eval_count = metadata.get("eval_count")
        eval_seconds = float(metadata.get("eval_duration") or 0) / 1e9
        call_record = {
            "call": call_number, "asked": requested, "min_items": output_schema["properties"]["questions"]["minItems"],
            "returned": len(candidates), "valid_added": added,
            "rejected": rejected_here, "rejected_by": codes_here,
            "excerpts": len(shown_units), "material_chars": material_chars, "prompt_chars": len(prompt),
            "num_predict": num_predict, "temperature": temperature, "num_ctx": QUIZ_NUM_CTX,
            "keep_alive": QUIZ_GENERATION_KEEP_ALIVE,
            "prompt_tokens": metadata.get("prompt_eval_count"), "generated_tokens": eval_count,
            "done_reason": metadata.get("done_reason"), "cut": cut,
            "salvaged": parse_report.get("salvaged"), "quote_keys": parse_report.get("quote_keys"),
            "elapsed_ms": call_invocation_ms,
            "tokens_per_s": round(eval_count / eval_seconds, 1) if eval_count and eval_seconds else None,
            "error": error_text or None,
        }
        call_log.append(call_record)
        print(f"[quiz-units-call] {json.dumps(call_record)}")
    else:
        stop_reason = stop_reason or "call limit reached"
    if len(accepted) < question_count and stop_reason:
        validation_results["reasons"].append(f"more questions were not generated: {stop_reason}")

    followups = max(0, llm_calls - 1)
    timings["generation_ms"] = generation_ms
    timings["model_load_ms"] = model_load_ms
    timings["prompt_eval_ms"] = prompt_eval_ms
    timings["token_generation_ms"] = token_generation_ms
    timings["model_invocation_ms"] = model_invocation_ms
    timings["initial_generation_ms"] = initial_generation_ms
    timings["repair_generation_ms"] = followup_generation_ms
    timings["fill_generation_ms"] = 0
    timings["validation_ms"] = validation_ms
    timings["repair_ms"] = followup_generation_ms
    timings["repair_llm_calls"] = followups
    timings["repair_attempt_count"] = 0
    timings["fill_attempt_count"] = followups
    timings["missing_slots_before_each_retry"] = call_log[1:]
    timings["final_fill_llm_calls"] = followups
    timings["deterministic_fallback_count"] = 0
    timings["candidate_pool_size"] = len(accepted)  # valid candidates, pre-selection
    timings["desired_candidate_count"] = pool_target
    timings["candidates_requested_total"] = candidates_requested_total
    timings["candidates_returned_total"] = candidates_returned_total
    timings["rejected_candidates"] = validation_results["rejected"]
    timings["duplicate_candidates"] = validation_results["duplicate_rejections"]
    timings["grounding_rejected_candidates"] = validation_results["grounding_rejections"]
    timings["structural_rejected_candidates"] = validation_results["structural_rejections"]
    timings["response_failures"] = validation_results["response_failures"]
    timings["total_quiz_generation_ms"] = round((time.perf_counter() - total_started) * 1000)

    # The ONLY failure: not a single valid question.
    if not accepted:
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000)
        timings["total_request_ms"] = timings["total_ms"]
        print(f"[quiz-units-timing] {json.dumps({**timings, 'llm_calls': llm_calls})}")
        diag.absorb_pipeline_timings({**timings, "llm_calls": llm_calls})
        summary = list(dict.fromkeys(validation_results["reasons"]))[-10:]
        if not summary:
            summary = [f"No valid candidate was returned for any of the {question_count} requested questions."]
        raise QuizGenerationError(
            f"Quiz generation requested {question_count} questions but produced 0 valid questions.",
            stage="validation",
            valid_questions=0,
            target_questions=question_count,
            failure_summary=summary,
        )

    fill_pool, fill_info = [], None
    grounded_hints = ground_fill_blank_hints(fill_blank_hints or [], units) if fill_blank_count > 0 else []
    if grounded_hints:
        fill_pool, fill_info = _generate_fill_blank_pool(
            model_id=model_id, scope_label=scope_label, difficulty=difficulty,
            units=units, accepted=accepted, hints=grounded_hints,
            scope_topic=scope_topic, target=min(fill_blank_count, len(grounded_hints)), first_index=candidate_counter,
            deadline_s=QUIZ_TOTAL_DEADLINE_S - (time.perf_counter() - total_started),
        )
        llm_calls += fill_info["calls"]

    # final_count = min(valid candidates, requested_count). Short is "partial", never a failure.
    # Fill_blank questions REPLACE multiple-choice ones, so the requested total is unchanged.
    fill_selected = _select_fill_blank(fill_pool, fill_blank_count) if fill_pool else []
    combined = select_questions(accepted, question_count - len(fill_selected)) + fill_selected
    combined.sort(key=lambda question: (question["_meta"]["unit_index"], question["_meta"]["index"]))
    selected, question_evidence = finalize_questions(combined)
    actual_count = len(selected)
    missing_count = max(0, question_count - actual_count)
    status = "complete" if actual_count >= question_count else "partial"
    shown_chars = sum(units_by_id[uid]["char_count"] for uid in shown_ids)
    document_chars = sum(unit["char_count"] for unit in units)
    timings["selected_count"] = actual_count

    quiz = {
        "quiz_id": str(uuid4()),
        "document_id": document["id"],
        "document_hash": document.get("hash", ""),
        "title": quiz_title or document.get("title", document["id"]),
        "difficulty": difficulty,
        "topic_id": scope_topic_id,
        "topic_name": scope_topic_name,
        "assessment_scope": scope,
        "assessment_plan": {
            "planner_version": QUIZ_ENGINE_VERSION,
            "generation_engine": "context_candidates",
            "scope": scope,
            "context_group_count": len(shown_ids),
            "study_units": [public_unit(units_by_id[uid]) for uid in sorted(shown_ids, key=lambda uid: units_by_id[uid]["index"])],
            "context": {
                "excerpts_total": len(units), "excerpts_shown": len(shown_ids),
                "chars_total": document_chars, "chars_shown": shown_chars,
                "share_of_document_shown": round(shown_chars / document_chars, 3) if document_chars else 0.0,
            },
            "excluded_topic_ids": [],
            "target_questions": question_count,
            "total_questions": actual_count,
            "status": status,
            "requested_count": question_count,
            "actual_count": actual_count,
            "missing_count": missing_count,
            "candidate_pool_size": len(accepted),
            "partial": status == "partial",
            "type_distribution": dict(Counter(question["question_type"] for question in selected)),
            **({"fill_blank": fill_info} if fill_info else {}),
            "generation_warnings": validation_results["reasons"],
            "validation_results": validation_results,
            "question_evidence": question_evidence,
            "llm_calls": llm_calls,
            "max_llm_calls": max_calls,
            "calls": call_log,
            "generation_model": generation_model,
        },
        "generation_model": generation_model,
        "topic_schema_version": int(document.get("topic_schema_version", 0)),
        "question_count": actual_count,
        "created_at": utc_now_iso(),
        "questions": selected,
    }
    # Regenerating creates a brand-new quiz artifact (its own quiz_id) alongside the previous one --
    # it never replaces it, so the previous quiz's own attempts/progress are left untouched here.
    persistence_started = time.perf_counter()
    saved = save_quiz(document["id"], difficulty, quiz, owner_id)
    timings["persistence_ms"] = round((time.perf_counter() - persistence_started) * 1000)
    timings["total_ms"] = round((time.perf_counter() - total_started) * 1000)
    timings["total_request_ms"] = timings["total_ms"]
    saved["assessment_plan"]["timings_ms"] = timings
    print(f"[quiz-units-timing] {json.dumps({**timings, 'llm_calls': llm_calls, 'questions': actual_count})}")
    diag.absorb_pipeline_timings({**timings, "llm_calls": llm_calls})
    return saved


def _flashcard_coverage_hints(document: dict, owner_id: str) -> list[str]:
    """Compact terms from the document's current persisted flashcards (coverage hints only; see
    quiz_units.flashcard_hint_terms). No flashcards, or any read problem -> no hints."""
    try:
        from backend.flashcard_service import FLASHCARD_VERSION   # flashcard_service imports this module
        info = get_latest_flashcard_set_info(
            owner_id, document["id"], str(document.get("hash") or ""), int(document.get("topic_schema_version") or 0),
            FLASHCARD_VERSION,
        )
        cards = list_flashcards(owner_id, document["id"], info["set_id"]) if info else []
    except Exception as error:
        print(f"[quiz-units-fill-blank] flashcard hints unavailable: {type(error).__name__}: {error}")
        return []
    return flashcard_hint_terms(cards)


def _fill_blank_context(units: list[dict], hints: list[dict], used: list[dict], budget_chars: int) -> list[dict]:
    """Excerpts for the fill_blank call: those stating a hinted term first, then the least-used rest,
    within the call's evidence budget."""
    hinted_ids = {unit_id for hint in hints for unit_id in hint["unit_ids"]}
    ordered = [unit for unit in units if unit["unit_id"] in hinted_ids]
    ordered += [unit for unit in followup_units(units, used, budget_chars, QUIZ_UNUSED_CHARS_PER_QUESTION)
                if unit["unit_id"] not in hinted_ids]
    shown, total = [], 0
    for unit in ordered:
        if shown and total + unit["char_count"] > budget_chars:
            break
        shown.append(unit)
        total += unit["char_count"]
    return shown


def _generate_fill_blank_pool(
    *, model_id: str, scope_label: str, difficulty: str, units: list[dict], accepted: list[dict],
    scope_topic: dict, target: int, first_index: int, deadline_s: float, hints: list[dict],
) -> tuple[list[dict], dict]:
    """The bounded fill_blank step of the live pipeline, run after the multiple-choice pool exists
    and only when there are grounded flashcard hints (the reason to ask for fill_blank at all).

    The call is shown the excerpts that state the hinted terms and told to prefer them. It stops
    after the first call that parses and yields a valid candidate, and after a valid EMPTY answer
    ({"questions": []}); exactly ONE retry follows only malformed output (unparseable) or output
    whose candidates were all invalid. A transport/model failure is not retried. Every candidate
    goes through validate_fill_blank_candidate against the document excerpts (a hint never
    authorizes a question). Never raises: without a valid candidate the quiz stays multiple-choice.
    """
    diag = quiz_diagnostics.get_current()
    started = time.perf_counter()
    pool: list[dict] = []
    hint_keys = {hint["key"] for hint in hints}
    info = {"target": target, "hint_terms": len(hints), "calls": 0, "accepted": 0, "hinted_accepted": 0,
            "rejected": 0, "rejected_by": {}, "errors": [], "stop": ""}
    index = first_index
    for call_number in range(1, QUIZ_FILL_BLANK_MAX_CALLS + 1):
        remaining_s = deadline_s - (time.perf_counter() - started)
        if remaining_s < QUIZ_MIN_CALL_S:
            info["stop"] = "time budget used up"
            break
        ask = target + 1
        shown = _fill_blank_context(units, hints, accepted + pool, context_budget(ask))
        prompt = build_fill_blank_prompt(scope_label, difficulty, shown, ask, [q["question"] for q in accepted + pool],
                                         [hint["term"] for hint in hints])
        llm = ChatOllama(
            model=model_id, reasoning=False, temperature=0.1 if call_number == 1 else 0.3,
            format=fill_blank_output_schema(ask), num_ctx=QUIZ_NUM_CTX,
            num_predict=min(QUIZ_MAX_NEW_TOKENS, max(600, (ask + QUIZ_MAX_ITEMS_EXTRA) * QUIZ_TOKENS_PER_QUESTION)),
            keep_alive=QUIZ_GENERATION_KEEP_ALIVE,
            client_kwargs={"timeout": min(QUIZ_LLM_TIMEOUT_S, max(30, remaining_s))},
        )
        info["calls"] += 1
        invocation_started = time.perf_counter()
        try:
            response_text, _metadata, _cut = _generate_with_deadline(llm, prompt, min(QUIZ_FOLLOWUP_DEADLINE_S, remaining_s))
        except Exception as error:   # the model/runtime failed: no retry, no extra latency
            info["errors"].append(f"call {call_number}: {type(error).__name__}: {error}"[:200])
            info["stop"] = "model call failed"
            diag.record_llm_call(stage="fill_blank", model=model_id, success=False, attempt=call_number,
                                 elapsed_ms=(time.perf_counter() - invocation_started) * 1000,
                                 exception_type=type(error).__name__, reason=str(error)[:200])
            break
        diag.record_llm_call(stage="fill_blank", model=model_id, success=True, attempt=call_number,
                             elapsed_ms=(time.perf_counter() - invocation_started) * 1000)
        try:
            candidates = parse_candidates(response_text)
        except ValueError as error:   # malformed output: retry once
            info["errors"].append(f"call {call_number}: malformed output: {error}"[:200])
            info["stop"] = "malformed output"
            continue
        if not candidates:            # a valid, empty answer: the excerpts hold nothing suitable
            info["stop"] = "empty result"
            break
        added = 0
        for raw in candidates:
            index += 1
            try:
                question, _warnings = validate_fill_blank_candidate(
                    raw, shown, accepted + pool, difficulty, 0, scope_topic, index,
                )
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                code = getattr(error, "code", None) or "structure"
                info["rejected"] += 1
                info["rejected_by"][code] = info["rejected_by"].get(code, 0) + 1
                continue
            question["_meta"]["hinted"] = question["_meta"]["answer_key"] in hint_keys
            pool.append(question)
            added += 1
        if added:
            info["stop"] = "valid candidates"
            break
        info["stop"] = "all candidates invalid"   # invalid output: retry once
    info["accepted"] = len(pool)
    info["hinted_accepted"] = sum(bool(question["_meta"].get("hinted")) for question in pool)
    print(f"[quiz-units-fill-blank] {json.dumps(info)}")
    return pool, info


def _select_fill_blank(pool: list[dict], target: int) -> list[dict]:
    """Flashcard-covered (hinted) candidates first, then the usual quality ranking."""
    return sorted(pool, key=lambda question: (not question["_meta"].get("hinted"), _rank_question(question)))[:max(0, target)]


def _resolve_quiz_title(quiz_name: str | None, previous_title: str | None, fallback: str) -> str:
    """Pick the quiz's display name without ever touching generation input.

    An explicit custom name always wins (trimmed). Otherwise a regeneration
    keeps whatever name the quiz already had, so regenerating a custom-named
    quiz cannot silently rename it. A brand-new quiz with no name falls back
    to the document's own title.
    """
    trimmed_name = str(quiz_name or "").strip()
    if trimmed_name:
        return trimmed_name
    trimmed_previous = str(previous_title or "").strip()
    return trimmed_previous or fallback


def _saved_quiz_uses_model(quiz: dict, model_id: str) -> bool:
    """A saved quiz answers a request only if the same model generated it.

    A quiz saved before models were recorded has no `generation_model`; it stays reusable rather
    than being replaced only because its model is unknown.
    """
    saved = quiz.get("generation_model") or (quiz.get("assessment_plan") or {}).get("generation_model")
    if not saved:
        return True
    requested = describe_generation_model(model_id)
    return (saved.get("name"), saved.get("quantization")) == (requested["name"], requested["quantization"])


def _quiz_variant_status(quiz: dict, owner_id: str = LEGACY_USER_ID) -> dict:
    """What the Quiz Library needs to know about one saved quiz artifact (no questions).

    Every field here is scoped to this exact quiz_id -- in particular the progress/status fields
    are read from the latest attempt for THIS quiz specifically, so a sibling quiz sharing the same
    (document, topic, difficulty) slot never bleeds its progress into this one.
    """
    plan = quiz.get("assessment_plan") or {}
    model = quiz.get("generation_model") or plan.get("generation_model") or {}
    requested = int(plan.get("requested_count") or plan.get("target_questions") or quiz.get("question_count") or 0)
    actual = int(quiz.get("question_count") or 0)
    attempt = get_latest_attempt(
        quiz["document_id"], quiz["difficulty"], quiz["topic_id"], owner_id, quiz_id=quiz.get("quiz_id")
    )
    completed = bool(attempt and attempt.get("completed"))
    in_progress = bool(attempt and not completed and int(attempt.get("answered") or 0) > 0)
    progress_status = "completed" if completed else "in_progress" if in_progress else "not_started"
    return {
        "quiz_id": quiz.get("quiz_id"),
        "title": quiz.get("title") or "",
        "topic_id": quiz["topic_id"],
        "topic_name": quiz.get("topic_name") or "",
        "assessment_scope": quiz.get("assessment_scope") or ("document" if quiz["topic_id"] == "document" else "topic"),
        "difficulty": quiz["difficulty"],
        "question_count": actual,
        "requested_count": requested,
        "status": "complete" if actual >= requested else "partial",
        "model_id": model.get("model_id"),
        "model_name": model.get("name"),
        "created_at": quiz.get("created_at"),
        "updated_at": (attempt.get("updated_at") if attempt else None) or quiz.get("created_at"),
        "progress_status": progress_status,
        "answered": int(attempt.get("answered") or 0) if attempt else 0,
        "total": int(attempt.get("total") or 0) if attempt and attempt.get("total") else actual,
        "score": int(attempt.get("score") or 0) if completed else None,
        "percentage": float(attempt.get("percentage") or 0) if completed else None,
    }


def _with_count_fields(quiz: dict, requested: int) -> dict:
    """Expose requested_count / actual_count / status at the top level of the quiz."""
    plan = quiz.get("assessment_plan") or {}
    requested_count = int(plan.get("requested_count") or plan.get("target_questions") or requested)
    actual_count = len(quiz.get("questions") or [])
    quiz["requested_count"] = requested_count
    quiz["actual_count"] = actual_count
    quiz["status"] = "complete" if actual_count >= requested_count else "partial"
    return quiz


def generate_quiz(
    document_id: str,
    difficulty: str,
    assessment_scope: str,
    topic_id: str | None = None,
    regenerate: bool = False,
    owner_id: str = LEGACY_USER_ID,
    model_id: str = CHAT_MODEL,
    question_count: int = 12,
    quiz_name: str | None = None,
) -> dict:
    """
    Public entry point for Quiz generation.

    This is a thin instrumentation wrapper around _generate_quiz() -- it does not
    change generation behavior, prompts, models, retries, validation, or output.
    It only opens a request-scoped performance diagnostics recorder (see
    backend/quiz_diagnostics.py) and logs a [QUIZ][SUMMARY] line before
    returning/raising exactly what _generate_quiz() returned/raised.
    """
    request_id = str(uuid4())
    with quiz_diagnostics.start_run(
        request_id=request_id, document_id=document_id, assessment_scope=assessment_scope,
        topic_id=topic_id, difficulty=difficulty, question_count=question_count, model_id=model_id,
    ) as diag:
        diag.log_start()
        try:
            result = _generate_quiz(
                document_id, difficulty, assessment_scope, topic_id, regenerate,
                owner_id, model_id, question_count, quiz_name,
            )
            _with_count_fields(result, question_count)
            questions = result.get("questions") or []
            diag.set_counts(
                requested_questions=question_count,
                generated_questions=len(questions),
                validated_questions=len(questions),
            )
            return result
        except Exception as error:
            diag.record_failure(type(error).__name__, str(error))
            raise
        finally:
            diag.log_summary()


def _generate_quiz(
    document_id: str,
    difficulty: str,
    assessment_scope: str,
    topic_id: str | None = None,
    regenerate: bool = False,
    owner_id: str = LEGACY_USER_ID,
    model_id: str = CHAT_MODEL,
    question_count: int = 12,
    quiz_name: str | None = None,
) -> dict:
    """
    Generate or load the persistent quiz for one indexed document.

    If a quiz already exists, it is returned as-is so reloads or future visits do
    not create a different quiz. Passing regenerate=True intentionally replaces
    the saved quiz.
    """
    diag = quiz_diagnostics.get_current()
    known_documents = _document_lookup(owner_id)
    if document_id not in known_documents:
        raise ValueError("document_id was not found in indexed documents.")
    difficulty = difficulty.lower().strip()
    if difficulty not in QUIZ_DIFFICULTIES:
        raise ValueError("difficulty must be easy, medium, or difficult.")
    print(f"[quiz-service] difficulty={difficulty}")
    document = known_documents[document_id]
    assessment_scope = str(assessment_scope).lower().strip()
    if assessment_scope not in {"topic", "document"}:
        raise ValueError("assessment_scope must be topic or document.")
    if question_count not in QUIZ_V2_ALLOWED_QUESTION_COUNTS:
        raise ValueError("question_count must be one of 12, 15, 18 or 20.")
    topic_lookup = {topic.get("topic_id"): topic for topic in document.get("topics", [])}
    if assessment_scope == "topic" and topic_id not in topic_lookup:
        raise ValueError("topic_id was not found in the selected document.")
    scope_topic_id = str(topic_id) if assessment_scope == "topic" else "document"
    topic_schema_version = int(document.get("topic_schema_version", 0))
    invalidate_document_quizzes_for_topic_schema(document_id, topic_schema_version, owner_id)
    cache_lookup_started = time.perf_counter()
    cache_key = quiz_cache_key(document_id, difficulty, scope_topic_id, owner_id)
    saved_quiz = get_quiz(document_id, difficulty, scope_topic_id, owner_id)
    diag.set_stage_ms("cache_lookup_ms", (time.perf_counter() - cache_lookup_started) * 1000)

    if not regenerate:
        saved_plan = (saved_quiz or {}).get("assessment_plan") or {}
        saved_planner = str(saved_plan.get("planner_version") or "legacy")
        saved_requested = int(saved_plan.get("requested_count") or saved_plan.get("target_questions") or 0)
        saved_questions = list((saved_quiz or {}).get("questions") or [])
        if (
            saved_quiz and saved_questions and saved_requested == question_count and saved_planner == QUIZ_ENGINE_VERSION
            and _saved_quiz_uses_model(saved_quiz, model_id)
        ):
            print(f"[quiz-cache] key={cache_key} HIT")
            diag.set_counts(cache_hit=True, requested_questions=question_count,
                             generated_questions=len(saved_questions), validated_questions=len(saved_questions))
            return saved_quiz
        if saved_quiz:
            print(f"[quiz-cache] key={cache_key} MISS (planner/count/model compatibility)")
        else:
            print(f"[quiz-cache] key={cache_key} MISS")
        diag.set_counts(cache_hit=False)
    else:
        print(f"[quiz-cache] key={cache_key} MISS (regenerate)")
        diag.set_counts(cache_hit=False)

    quiz_title = _resolve_quiz_title(
        quiz_name, (saved_quiz or {}).get("title"), document.get("title", document_id)
    )

    # Live path ("Study Units", see backend/quiz_units.py): no Planner, concept extraction,
    # Question Blueprint, slots or repair/fill loops, for both scopes. Retrieval is unchanged --
    # get_schema_topic_evidence for a topic quiz, get_document_chunks for a whole-document quiz --
    # and the chunks become excerpts in _generate_quiz_from_units (one call, at most one top-up).
    #
    # The V2 Planner-based engines in backend/quiz_legacy_v2.py (_generate_topic_quiz_v2 for topic
    # scope, _run_document_single_choice_quiz for document scope) are kept for comparison; they are
    # not invoked from here.
    retrieval_started = time.perf_counter()
    if assessment_scope == "topic":
        topic = topic_lookup[topic_id]
        chunks = get_schema_topic_evidence(document_id, topic, owner_id)
        scope_topic_name = str(topic.get("name") or topic_id)
    else:
        chunks = get_document_chunks(document_id, owner_id)
        scope_topic_name = "Entire document"
    retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)

    return _generate_quiz_from_units(
        fill_blank_count=fill_blank_target(question_count),
        fill_blank_hints=_flashcard_coverage_hints(document, owner_id),
        document=document,
        scope=assessment_scope,
        scope_topic_id=scope_topic_id,
        scope_topic_name=scope_topic_name,
        chunks=chunks,
        difficulty=difficulty,
        owner_id=owner_id,
        model_id=model_id,
        regenerate=regenerate,
        question_count=question_count,
        quiz_title=quiz_title,
        retrieval_ms=retrieval_ms,
    )


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


def list_quiz_statuses(owner_id: str = LEGACY_USER_ID) -> list[dict]:
    """Return quiz/status information for every indexed document."""
    statuses = []

    for document in list_indexed_documents(owner_id):
        document_id = document["id"]
        document_quizzes = list_document_quizzes(document_id, owner_id)
        variants = [_quiz_variant_status(quiz, owner_id) for quiz in document_quizzes.values()]

        statuses.append(
            {
                "document_id": document_id,
                "title": document["title"],
                "chunks": document["chunks"],
                "has_quiz": bool(variants),
                "variants": variants,
            }
        )

    return statuses


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


def build_learning_dashboard(student_id: str = LEGACY_USER_ID) -> dict:
    """Aggregate real indexed-document, attempt, and mastery state for Overview."""
    documents = list_indexed_documents(student_id)
    mastery_rows = []
    for document in documents:
        for topic in document.get("topics", []):
            history = list_completed_answer_snapshots(student_id, document["id"], topic["topic_id"])
            mastery = {
                "student_id": student_id,
                "document_id": document["id"],
                "topic_id": topic["topic_id"],
                **calculate_mastery(history, len({row["quiz_id"] for row in history})),
            }
            mastery_rows.append({
                **mastery,
                "topic_name": topic.get("name") or topic["topic_id"],
                "document_name": document.get("title") or document["id"],
            })

    attempts = load_quiz_history(student_id=student_id)
    answer_rows = [result for attempt in attempts for result in attempt.get("question_results", [])]
    answered_questions = len(answer_rows)
    assessed = [mastery for mastery in mastery_rows if mastery.get("has_evidence")]
    latest_attempt = attempts[0] if attempts else None
    latest_summary = None
    if latest_attempt:
        represented_topics = sorted({
            str(result.get("topic_id")) for result in latest_attempt.get("question_results", [])
            if result.get("topic_id") and result.get("topic_id") != "document"
        })
        latest_summary = {
            "attempt_id": latest_attempt.get("attempt_id"),
            "document_id": latest_attempt.get("document_id"),
            "topic_id": latest_attempt.get("topic_id"),
            "difficulty": latest_attempt.get("difficulty"),
            "score": latest_attempt.get("score", 0),
            "total": latest_attempt.get("total", 0),
            "completed_at": latest_attempt.get("completed_at") or latest_attempt.get("submitted_at"),
            "represented_topic_ids": represented_topics,
        }

    mastery_by_document = {}
    for mastery in mastery_rows:
        mastery_by_document.setdefault(mastery["document_id"], []).append(mastery)
    material_rows = []
    for document in documents:
        rows = mastery_by_document.get(document["id"], [])
        material_rows.append({
            "document_id": document["id"],
            "document_name": document.get("title") or document["id"],
            "topic_count": len(document.get("topics", [])),
            "assessed_topic_count": sum(int(bool(row.get("has_evidence"))) for row in rows),
            "indexed": True,
        })

    return {
        "student_id": student_id,
        "metrics": {
            "documents": len(documents),
            "topics_assessed": len(assessed),
            "total_topics": len(mastery_rows),
            "answered_questions": answered_questions,
            "topics_mastered": sum(row.get("mastery_level") == "Mastered" for row in mastery_rows),
        },
        "mastery": sorted(
            mastery_rows,
            key=lambda row: (not row.get("has_evidence"), -int(row.get("answered_questions", 0)), row["document_name"], row["topic_name"]),
        ),
        "latest_attempt": latest_summary,
        "materials": material_rows,
    }


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


def delete_quiz(quiz_id: str, owner_id: str = LEGACY_USER_ID) -> dict:
    """
    Permanently delete one quiz along with its own questions, attempts, and answers.

    The source document and its topic hierarchy are never touched. If the
    deleted quiz had completed attempts, mastery for the topics those answers
    fed is recomputed from whatever attempts remain; topics untouched by this
    quiz keep their existing mastery unchanged.
    """
    result = _delete_quiz_row(quiz_id, owner_id)
    if result is None:
        raise ValueError("Quiz was not found.")
    document_id = result["document_id"]
    recomputed = [
        recompute_topic_mastery(owner_id, document_id, topic_id)
        for topic_id in result["affected_topic_ids"]
    ]
    return {
        "quiz_id": quiz_id,
        "document_id": document_id,
        "deleted": True,
        "recomputed_topic_ids": result["affected_topic_ids"],
        "mastery": recomputed,
    }


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
