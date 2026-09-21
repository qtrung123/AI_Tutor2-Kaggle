"""Live Quiz pipeline: chunks -> excerpts -> context -> call 1 writes surplus candidates -> validate
-> pool -> enough? stop, else another (moderate, told what exists) call, at most 4 (12/15) or 5 (18/20).

The requested count is a TARGET, never a condition of success: final_count = min(valid, requested).

A. Sizing: 12/15/18/20, candidate surplus, context budget, API and UI accept the four counts
B. Excerpts: whole document, nothing truncated, headers/overlap removed
C. Context selection: everything that fits, otherwise an even spread; follow-ups prefer unseen text
D. Prompt / schema / validator agree (4 options, 1 answer, single_choice, quote first)
E. Parsing tolerant of truncated output
F. Grounding: evidence_quote in the provided context (glue, accents, bilingual, small edits)
G. Validation: structure, support, duplicates, relaxed rules
H. Selection: best N in document order
I. Engine: counts, call budget per count, follow-up sizing, partial results, failure only at 0 valid
J. Cache versioning and reuse
K. API metadata: requested_count / actual_count / status
"""

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from quiz_fixtures import FACT_COUNT, FakeModel, candidates, fact_sentence, make_chunks, raw_candidate

from backend import quiz_service, quiz_units
from backend.main import QuizGenerateRequest, QuizGenerateResponse, QuizRegenerateRequest
from backend.quiz_service import QuizGenerationError, _generate_quiz_from_units
from backend.quiz_units import (
    CandidateRejected, build_generation_prompt, build_study_units, candidate_target, context_budget,
    context_support, locate_evidence, parse_candidates, select_context_units, select_questions, squash,
    followup_request, max_llm_calls, min_items_for, output_schema_for, validate_candidate,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
SCOPE = {"topic_id": "document", "name": "Entire document"}


def validate(raw, units, accepted=None, difficulty="easy"):
    return validate_candidate(raw, units, accepted or [], difficulty, len(accepted or []) + 1, SCOPE, 1)


def marked_chunks(count: int, size: int = 700) -> list[dict]:
    """Chunks with a unique marker so tests can see exactly which text reached an excerpt/prompt."""
    return [
        {"content": f"Marker{index:04d} " + ("filler words here " * (size // 18)), "metadata": {"chunk_id": f"c{index}"}}
        for index in range(1, count + 1)
    ]


# ---------------------------------------------------------------------------------------------
# A. Sizing
# ---------------------------------------------------------------------------------------------
class SizingTests(unittest.TestCase):
    def test_the_four_question_counts_and_their_candidate_surplus(self):
        self.assertEqual(quiz_units.QUIZ_ALLOWED_QUESTION_COUNTS, (12, 15, 18, 20))
        self.assertEqual([candidate_target(n) for n in (12, 15, 18, 20)], [15, 18, 22, 24])
        self.assertEqual(quiz_service.QUIZ_V2_ALLOWED_QUESTION_COUNTS, {12, 15, 18, 20})
        self.assertLessEqual(candidate_target(20), quiz_units.QUIZ_OUTPUT_SCHEMA["properties"]["questions"]["maxItems"])

    def test_the_call_budget_depends_on_the_requested_count(self):
        self.assertEqual({n: max_llm_calls(n) for n in (12, 15, 18, 20)}, {12: 4, 15: 4, 18: 5, 20: 5})

    def test_a_follow_up_writes_a_moderate_batch_of_what_is_missing(self):
        self.assertEqual([followup_request(m) for m in range(1, 15)], [3, 4, 5, 6, 7, 8, 8, 8, 8, 8, 8, 8, 8, 8])
        for missing in range(1, 21):
            ask = followup_request(missing)
            self.assertTrue(quiz_units.QUIZ_FOLLOWUP_MIN_ASK <= ask <= quiz_units.QUIZ_FOLLOWUP_MAX_ASK)
            self.assertLess(ask, candidate_target(12))  # never the size of the first call

    def test_the_schema_of_every_call_bounds_the_list_so_the_decoder_cannot_stop_early(self):
        for ask in (3, 8, 15, 18, 22, 24):
            questions = output_schema_for(ask)["properties"]["questions"]
            self.assertEqual(questions["minItems"], max(1, int(ask * quiz_units.QUIZ_MIN_ITEMS_RATIO)))
            self.assertEqual(questions["maxItems"], ask + quiz_units.QUIZ_MAX_ITEMS_EXTRA)
            self.assertLess(questions["minItems"], ask + 1)
            item = questions["items"]
            self.assertEqual((item["properties"]["options"]["minItems"], item["properties"]["options"]["maxItems"]), (4, 4))
        self.assertNotIn("minItems", quiz_units.QUIZ_OUTPUT_SCHEMA["properties"]["questions"])  # base schema untouched

    def test_min_items_follows_the_amount_of_text_shown_and_never_exceeds_the_ratio(self):
        ratio, per = quiz_units.QUIZ_MIN_ITEMS_RATIO, quiz_units.QUIZ_CHARS_PER_MIN_ITEM
        self.assertEqual(per, 500)
        for ask in (3, 8, 15, 18, 22, 24):
            self.assertEqual(min_items_for(ask), max(1, int(ask * ratio)))                  # no material info: ratio only
            self.assertEqual(min_items_for(ask, 10 ** 6), max(1, int(ask * ratio)))          # plenty of text: ratio only
            previous = 0
            for chars in range(0, 13000, 250):
                value = min_items_for(ask, chars)
                self.assertTrue(1 <= value <= max(1, int(ask * ratio)))                      # never forces more than the ratio
                self.assertTrue(value == 1 or value <= chars // per)                          # never more than 1 per 500 chars
                self.assertGreaterEqual(value, previous)                                     # monotonic in the text shown
                previous = value
        self.assertEqual([min_items_for(24, c) for c in (0, 499, 500, 5264, 7133, 8000, 12000)], [1, 1, 1, 10, 14, 16, 16])
        self.assertEqual(output_schema_for(24, 5264)["properties"]["questions"]["minItems"], 10)
        self.assertEqual(output_schema_for(24, 5264)["properties"]["questions"]["maxItems"], 27)   # maxItems is not affected

    def test_the_context_budget_grows_with_the_request_but_is_bounded(self):
        budgets = [context_budget(candidate_target(n)) for n in (12, 15, 18, 20)]
        self.assertEqual(budgets, sorted(budgets))
        self.assertTrue(all(quiz_units.QUIZ_CONTEXT_MIN_CHARS <= b <= quiz_units.QUIZ_CONTEXT_MAX_CHARS for b in budgets))
        self.assertEqual(context_budget(1), quiz_units.QUIZ_CONTEXT_MIN_CHARS)
        self.assertEqual(context_budget(500), quiz_units.QUIZ_CONTEXT_MAX_CHARS)

    def test_token_budget_covers_the_largest_request_inside_the_context_window(self):
        pool = candidate_target(20)
        new_tokens = min(quiz_units.QUIZ_MAX_NEW_TOKENS, pool * quiz_units.QUIZ_TOKENS_PER_QUESTION)
        self.assertEqual(new_tokens, pool * quiz_units.QUIZ_TOKENS_PER_QUESTION)  # nothing is cut off
        worst_prompt_tokens = quiz_units.QUIZ_CONTEXT_MAX_CHARS / 1.5 + 1000
        self.assertLess(worst_prompt_tokens + quiz_units.QUIZ_MAX_NEW_TOKENS, quiz_units.QUIZ_NUM_CTX)

    def test_the_whole_pipeline_has_a_wall_clock_budget_inside_the_reverse_proxy_limit(self):
        proxy_limit = 600  # deployment/nginx.kaggle.conf: proxy_read_timeout for /api/
        self.assertLess(quiz_units.QUIZ_TOTAL_DEADLINE_S, proxy_limit)
        self.assertLess(quiz_units.QUIZ_FIRST_CALL_DEADLINE_S, quiz_units.QUIZ_TOTAL_DEADLINE_S)
        self.assertLessEqual(quiz_units.QUIZ_FOLLOWUP_DEADLINE_S, quiz_units.QUIZ_FIRST_CALL_DEADLINE_S)
        self.assertGreaterEqual(quiz_units.QUIZ_TOTAL_DEADLINE_S - quiz_units.QUIZ_FIRST_CALL_DEADLINE_S,
                                quiz_units.QUIZ_MIN_CALL_S)
        self.assertLessEqual(quiz_units.QUIZ_LLM_TIMEOUT_S, quiz_units.QUIZ_FIRST_CALL_DEADLINE_S)

    def test_the_api_accepts_exactly_12_15_18_and_20(self):
        required = dict(document_id="d.pdf", assessment_scope="document", difficulty="easy", quiz_name="Quiz")
        for count in (12, 15, 18, 20):
            self.assertEqual(QuizGenerateRequest(**required, question_count=count).question_count, count)
            self.assertEqual(QuizRegenerateRequest(assessment_scope="document", difficulty="easy",
                                                   question_count=count).question_count, count)
        for bad in (0, 10, 11, 13, 16, 19, 21, 24, 30):
            with self.assertRaises(ValidationError):
                QuizGenerateRequest(**required, question_count=bad)
        self.assertEqual(QuizGenerateRequest(**required).question_count, 12)

    def test_the_ui_offers_the_four_counts(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("[12, 15, 18, 20].includes(value) ? value : 12", frontend)
        self.assertIn("[12, 15, 18, 20].forEach((count)", frontend)

    def test_the_service_rejects_other_counts(self):
        with patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}):
            for bad in (10, 16, 25):
                with self.assertRaises(ValueError):
                    quiz_service._generate_quiz(DOCUMENT["id"], "easy", "document", question_count=bad)


# ---------------------------------------------------------------------------------------------
# B. Excerpts
# ---------------------------------------------------------------------------------------------
class ExcerptTests(unittest.TestCase):
    def test_excerpts_cover_the_whole_document_in_order_and_nothing_is_truncated(self):
        chunks = marked_chunks(40)
        units = build_study_units(chunks)
        flat = [chunk_id for unit in units for chunk_id in unit["source_chunk_ids"]]
        self.assertEqual(flat, [f"c{index}" for index in range(1, 41)])
        joined = " ".join(unit["evidence_excerpt"] for unit in units)
        for index in range(1, 41):
            self.assertIn(f"Marker{index:04d}", joined)
        self.assertTrue(all(unit["char_count"] <= quiz_units.QUIZ_UNIT_MAX_CHARS for unit in units))
        self.assertEqual(squash(joined), squash(" ".join(chunk["content"] for chunk in chunks)))

    def test_excerpt_count_scales_with_the_document(self):
        self.assertEqual([len(build_study_units(marked_chunks(n))) for n in (10, 40, 160, 640)], [10, 40, 160, 640])

    def test_tiny_neighbouring_chunks_are_merged_and_oversized_ones_are_split(self):
        tiny = [{"content": f"Short chunk number {i} says something.", "metadata": {"chunk_id": f"t{i}"}} for i in range(1, 9)]
        units = build_study_units(tiny)
        self.assertLess(len(units), 8)
        self.assertEqual([c for u in units for c in u["source_chunk_ids"]], [f"t{i}" for i in range(1, 9)])
        parts = build_study_units([{"content": "word " * 700, "metadata": {"chunk_id": "big"}}])
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(part["char_count"] <= quiz_units.QUIZ_UNIT_MAX_CHARS for part in parts))
        self.assertEqual(squash(" ".join(p["evidence_excerpt"] for p in parts)), squash("word " * 700))

    def test_repeated_page_headers_are_removed_but_real_content_is_kept(self):
        chunks = [{
            "content": f"Author Name  team@example.com\nSection heading {i}\n"
                       f"Unique teaching sentence number {i} explains the topic well. " + "detail " * 60,
            "metadata": {"chunk_id": f"h{i}"}} for i in range(1, 9)]
        text = " ".join(unit["evidence_excerpt"] for unit in build_study_units(chunks))
        self.assertNotIn("team@example.com", text)
        for i in range(1, 9):
            self.assertIn(f"Unique teaching sentence number {i}", text)
            self.assertIn(f"Section heading {i}", text)

    def test_header_removal_needs_repetition_so_a_small_document_keeps_all_lines(self):
        chunks = [{"content": "Title line\nBody text one. " + "x " * 300, "metadata": {"chunk_id": f"s{i}"}} for i in (1, 2)]
        self.assertEqual(" ".join(u["evidence_excerpt"] for u in build_study_units(chunks)).count("Title line"), 2)

    def test_splitter_overlap_between_neighbouring_chunks_is_not_repeated(self):
        first = "Alpha section explains scheduling. " + "middle words " * 40 + "The shared overlap sentence appears at the seam of two chunks."
        second = "The shared overlap sentence appears at the seam of two chunks. And then the next part continues here."
        units = build_study_units([{"content": first, "metadata": {"chunk_id": "o1"}},
                                   {"content": second, "metadata": {"chunk_id": "o2"}}])
        text = " ".join(unit["evidence_excerpt"] for unit in units)
        self.assertEqual(text.count("shared overlap sentence"), 1)
        self.assertIn("the next part continues here", text)

    def test_missing_ids_blank_text_and_duplicates_are_dropped(self):
        chunks = [
            {"content": "kept text one", "metadata": {"chunk_id": "a"}},
            {"content": "kept text one again", "metadata": {"chunk_id": "a"}},
            {"content": "   ", "metadata": {"chunk_id": "b"}},
            {"content": "no id", "metadata": {}},
        ]
        self.assertEqual([unit["source_chunk_ids"] for unit in build_study_units(chunks)], [["a"]])
        self.assertEqual(build_study_units([]), [])


# ---------------------------------------------------------------------------------------------
# C. Context selection
# ---------------------------------------------------------------------------------------------
class ContextSelectionTests(unittest.TestCase):
    def test_everything_that_fits_is_shown(self):
        units = build_study_units(marked_chunks(10))
        self.assertEqual(select_context_units(units, 12000), units)

    def test_a_large_document_is_sampled_evenly_within_the_budget(self):
        units = build_study_units(marked_chunks(600))
        for candidates_wanted in (15, 18, 22, 24):
            budget = context_budget(candidates_wanted)
            picked = select_context_units(units, budget)
            self.assertLessEqual(sum(u["char_count"] for u in picked), budget)
            self.assertGreater(len(picked), 8)
            indexes = [u["index"] for u in picked]
            self.assertEqual(indexes, sorted(indexes))
            self.assertLess(indexes[0], len(units) * 0.1)      # the beginning is not the only thing shown
            self.assertGreater(indexes[-1], len(units) * 0.9)  # ... and neither is the end missing
            gaps = [b - a for a, b in zip(indexes, indexes[1:])]
            self.assertLessEqual(max(gaps) - min(gaps), 1)     # evenly spaced

    def test_a_bigger_request_sees_more_text(self):
        units = build_study_units(marked_chunks(400))
        sizes = [len(select_context_units(units, context_budget(candidate_target(n)))) for n in (12, 15, 18, 20)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[-1])

    def test_excluded_excerpts_are_skipped_so_a_top_up_can_see_new_text(self):
        units = build_study_units(marked_chunks(60))
        first = select_context_units(units, 8000)
        seen = {u["unit_id"] for u in first}
        second = select_context_units(units, 8000, exclude_ids=seen)
        self.assertTrue(second)
        self.assertFalse({u["unit_id"] for u in second} & seen)
        self.assertEqual(select_context_units(units, 8000, exclude_ids={u["unit_id"] for u in units}), [])

    def test_selection_is_deterministic(self):
        units = build_study_units(marked_chunks(120))
        self.assertEqual(select_context_units(units, 9000), select_context_units(units, 9000))


# ---------------------------------------------------------------------------------------------
# D. Prompt / schema / validator agreement
# ---------------------------------------------------------------------------------------------
class PromptSchemaValidatorAgreementTests(unittest.TestCase):
    def test_schema_matches_the_validator_contract(self):
        item = quiz_units.QUIZ_OUTPUT_SCHEMA["properties"]["questions"]["items"]
        self.assertEqual((item["properties"]["options"]["minItems"], item["properties"]["options"]["maxItems"]), (4, 4))
        self.assertEqual((item["properties"]["answer_index"]["minimum"], item["properties"]["answer_index"]["maximum"]), (0, 3))
        self.assertNotIn("question_type", item["properties"])
        self.assertNotIn("unit_id", item["properties"])
        self.assertEqual(set(item["required"]), set(item["properties"]))

    def test_the_quote_is_declared_before_the_question(self):
        order = list(quiz_units.QUIZ_OUTPUT_SCHEMA["properties"]["questions"]["items"]["properties"])
        self.assertLess(order.index("evidence_quote"), order.index("question"))
        prompt = build_generation_prompt("L", "easy", build_study_units(make_chunks(2)), 5)
        self.assertLess(prompt.index('"evidence_quote"'), prompt.index('"question"'))

    def test_prompt_demands_document_only_knowledge_single_choice_and_has_no_pipeline_jargon(self):
        units = build_study_units(make_chunks(12))
        prompt = build_generation_prompt("Lecture", "medium", units, 22)
        lowered = prompt.lower()
        self.assertIn("Write exactly 22 medium", prompt)
        self.assertIn("never add knowledge from outside them", lowered)
        self.assertIn("exactly 4 options", lowered)
        for forbidden in ("true_false", "multi_select", "true/false", "slot", "blueprint", "planner", "topic", "coverage"):
            self.assertNotIn(forbidden, lowered)
        for unit in units:
            self.assertIn(unit["evidence_excerpt"], prompt)

    def test_a_follow_up_prompt_lists_every_existing_question_with_the_sentence_it_used(self):
        stems = ["What does the mutex do?", "What does the socket do?"]
        quotes = [fact_sentence(0), fact_sentence(3) + "   extra   spaces " + "x" * 300]
        prompt = build_generation_prompt("Lecture", "easy", build_study_units(make_chunks(4)), 5, stems, quotes)
        self.assertIn(f'- What does the mutex do?  [source: "{fact_sentence(0)}"]', prompt)
        self.assertIn('- What does the socket do?  [source: "', prompt)
        listed = prompt[prompt.index("Questions already written"):prompt.index("EXCERPTS:")]
        self.assertLess(max(len(line) for line in listed.splitlines()), 200)        # quotes are shortened
        self.assertIn("The list below holds the questions that already exist", prompt)
        self.assertIn("do not ask about the same or a nearly identical fact again; choose OTHER facts", prompt)
        self.assertIn("The questions already written are listed below the rules", prompt)
        self.assertNotIn("questions above", prompt)                                  # the list is BELOW the rules
        self.assertLess(prompt.index("The questions already written are listed below the rules"), prompt.index("Questions already written:"))
        self.assertLess(prompt.index("Questions already written:"), prompt.index("EXCERPTS:"))
        self.assertNotIn("at most 2 per excerpt", prompt)                            # first-call rule does not apply
        plain = build_generation_prompt("Lecture", "easy", build_study_units(make_chunks(4)), 5, ["Only a stem?"])
        self.assertIn("- Only a stem?", plain)

    def test_the_prompt_asks_for_every_question_and_for_options_with_different_meanings(self):
        first = build_generation_prompt("Lecture", "easy", build_study_units(make_chunks(4)), 15)
        self.assertIn("Write ALL 15 questions: do not stop early", first)
        self.assertIn("clearly different meanings", first)
        self.assertIn("at most 2 per excerpt", first)
        self.assertNotIn("Questions already written", first)


# ---------------------------------------------------------------------------------------------
# E. Parsing
# ---------------------------------------------------------------------------------------------
class ParseTests(unittest.TestCase):
    def test_valid_and_fenced_output(self):
        payload = json.dumps({"questions": [raw_candidate(0)]})
        self.assertEqual(len(parse_candidates(payload)), 1)
        self.assertEqual(len(parse_candidates(f"```json\n{payload}\n```")), 1)

    def test_truncated_output_salvages_complete_questions(self):
        payload = json.dumps({"questions": [raw_candidate(0), raw_candidate(1), raw_candidate(2)]})
        cut = payload[: payload.rindex('{"question"') + 30]
        salvaged = parse_candidates(cut)
        self.assertEqual(len(salvaged), 2)
        self.assertEqual(salvaged[1]["question"], raw_candidate(1)["question"])

    def test_the_parse_report_tells_a_short_answer_from_a_broken_one(self):
        full, report = json.dumps({"questions": candidates(range(4))}), {}
        self.assertEqual(len(parse_candidates(full, report)), 4)
        self.assertEqual(report, {"salvaged": False, "quote_keys": 4})
        cut = full[: full.rindex('"evidence_quote"') + 30]
        report = {}
        self.assertEqual(len(parse_candidates(cut, report)), 3)
        self.assertEqual(report, {"salvaged": True, "quote_keys": 4})   # 4 started, 3 complete

    def test_unparseable_output_raises(self):
        for text in ("I cannot help with that.", '{"questions": [{"question": "cut off'):
            with self.assertRaises(ValueError):
                parse_candidates(text)


# ---------------------------------------------------------------------------------------------
# F. Grounding: evidence_quote in the provided context
# ---------------------------------------------------------------------------------------------
class QuoteGroundingTests(unittest.TestCase):
    """Deliberately glued PDF text (no spaces between words)."""

    GLUED = (
        "Determineswhichprogramsareadmittedtothesystemforprocessing.Controlsthedegreeofmultiprogramming."
        "Moreprocessesleadtoasmallerpercentageoftimeeachprocessisexecuted.Partoftheswappingfunction."
        "Swappingindecisionsarebasedonthenecessitytomanagethedegreeofmultiprogramming.Theschedulerselects"
        "thehighestpriorityprocessthatisreadytorunontheprocessor."
    )

    def setUp(self):
        self.units = build_study_units([{"content": self.GLUED, "metadata": {"chunk_id": "g1"}}])

    def accepted(self, quote):
        return locate_evidence(quote, self.units) is not None

    def test_spacing_case_and_punctuation_never_matter(self):
        self.assertTrue(self.accepted("Determines which programs are admitted to the system for processing."))
        self.assertTrue(self.accepted("CONTROLS THE DEGREE OF MULTIPROGRAMMING"))
        self.assertTrue(self.accepted("More processes lead to a smaller percentage of time each process is executed"))

    def test_small_edits_are_tolerated_regardless_of_quote_length(self):
        base = "Swapping-in decisions are based on the necessity to manage the degree of multiprogramming."
        self.assertTrue(self.accepted(base.replace("necessity", "need")))
        self.assertTrue(self.accepted(base.replace("manage ", "")))
        self.assertTrue(self.accepted("Swapping in decisions are based on the necessity to manage the degree of"))
        self.assertTrue(self.accepted("Controls the degree of multiprogramming."))

    def test_short_quotes_get_no_extra_leniency(self):
        self.assertTrue(self.accepted("Part of the swapping function"))
        self.assertTrue(self.accepted("Part of the swapping functon"))
        self.assertFalse(self.accepted("Part of the swapping routine"))
        self.assertFalse(self.accepted("Part of the swap"))

    def test_accents_are_ignored_on_both_sides(self):
        units = build_study_units([{
            "content": "Hệ điều hành quản lý bộ nhớ ảo bằng cách ánh xạ trang vào khung trang vật lý.",
            "metadata": {"chunk_id": "v1"}}])
        for quote in (
            "Hệ điều hành quản lý bộ nhớ ảo bằng cách ánh xạ trang vào khung trang vật lý",
            "He dieu hanh quan ly bo nho ao bang cach anh xa trang vao khung trang vat ly",
            "HỆ ĐIỀU HÀNH QUẢN LÝ BỘ NHỚ ẢO BẰNG CÁCH ÁNH XẠ TRANG",
        ):
            self.assertIsNotNone(locate_evidence(quote, units), quote)
        self.assertIsNone(locate_evidence("He dieu hanh quan ly bo nho vat ly bang cach ve do thi", units))

    def test_paraphrase_reordering_stitching_and_invention_are_rejected(self):
        self.assertFalse(self.accepted("The multiprogramming degree is managed through decisions about swapping in processes."))
        self.assertFalse(self.accepted("the degree of multiprogramming is controlled by which programs are admitted to the system"))
        self.assertFalse(self.accepted(
            "Determines which programs are admitted to the system for the highest priority process that is ready"))
        self.assertFalse(self.accepted("Virtual memory maps pages into frames using a page table entry."))
        self.assertFalse(self.accepted("short"))
        self.assertFalse(self.accepted(""))

    def test_edits_beyond_the_tolerance_are_rejected(self):
        base = "More processes lead to a smaller percentage of time each process is executed."
        self.assertFalse(self.accepted(base.replace("smaller", "tiny").replace("percentage", "share").replace("executed", "run")))
        self.assertFalse(self.accepted("Governs the degree of multiprogramming."))

    def test_only_the_provided_context_counts_as_evidence(self):
        units = build_study_units(make_chunks(16, 1))
        shown, unseen = units[:8], units[8:]
        self.assertIsNotNone(locate_evidence(fact_sentence(3), shown))
        self.assertIsNone(locate_evidence(fact_sentence(12), shown))
        self.assertIsNotNone(locate_evidence(fact_sentence(12), unseen))


# ---------------------------------------------------------------------------------------------
# G. Validation
# ---------------------------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(16, facts_per_chunk=1))

    def rejected(self, raw, category, accepted=None, units=None):
        with self.assertRaises(CandidateRejected) as context:
            validate(raw, units or self.units, accepted)
        self.assertEqual(context.exception.category, category, str(context.exception))

    def test_valid_candidate_is_single_choice_with_four_labeled_options(self):
        question, warnings = validate(raw_candidate(3, answer_index=2), self.units)
        self.assertEqual(question["question_type"], "single_choice")
        self.assertEqual((question["correct_answer"], question["correct_answers"]), ("C", ["C"]))
        self.assertEqual([option[:3] for option in question["options"]], ["A. ", "B. ", "C. ", "D. "])
        self.assertEqual(warnings, [])

    def test_only_four_options_are_accepted(self):
        good = raw_candidate(0)
        for options in (good["options"][:2], good["options"][:3], good["options"] + ["A fifth option here"], ["True", "False"]):
            self.rejected({**good, "options": options}, "structure")
        for options in ("A. B. C. D.", None, [1, 2, 3, 4]):
            self.rejected({**good, "options": options}, "structure")

    def test_exactly_one_integer_answer_in_range(self):
        good = raw_candidate(0)
        for bad in (4, -1, True, "0", [0], [0, 1], None, 1.0):
            self.rejected({**good, "answer_index": bad}, "structure")
        self.rejected({key: value for key, value in good.items() if key != "answer_index"} | {"correct_answers": [0, 1]}, "structure")

    def test_options_must_be_distinct_and_not_generic(self):
        good = raw_candidate(0)
        self.rejected({**good, "options": [good["options"][0]] * 2 + good["options"][2:]}, "structure")
        self.rejected({**good, "options": good["options"][:3] + ["None of the above"]}, "structure")
        self.rejected({**good, "options": [good["options"][0], "x" * 250, "orders tasks", "maps names"]}, "structure")

    def test_two_options_that_say_the_same_thing_are_rejected(self):
        good = raw_candidate(0)
        base = "reserves the shared resources for one thread"
        for twin in (
            "reserves shared resources for one thread",             # article dropped
            "for one thread reserves the shared resources",         # reordered
            "reserved the shared resources for one thread",         # inflected
            "Reserves the shared resources for one thread.",        # punctuation and case
        ):
            self.rejected({**good, "options": [base, twin, "orders tasks", "maps names"]}, "structure")
        self.rejected({**good, "options": ["Priority", "The priority", "orders tasks", "maps names"]}, "structure")

    def test_good_distractors_that_only_look_alike_are_kept(self):
        good = raw_candidate(0)
        for options in (
            ["Increases the execution time of short jobs", "Decreases the execution time of short jobs", "Has no effect", "Is random"],
            ["Non-preemptive", "Preemptive", "Clock driven", "Feedback based"],
            ["CPU-bound processes", "I/O-bound processes", "Both equally", "Neither"],
            ["Scheduler", "Scheduling", "Dispatcher", "Loader"],
        ):
            raw = {**good, "options": options, "answer_index": 0}
            self.assertTrue(quiz_units._options_too_similar(options) is False, options)

    def test_option_labels_are_stripped_but_articles_are_kept(self):
        raw = {**raw_candidate(0), "question": "What does the mutex reserve?",
               "options": ["A. reserves resources", "B) orders tasks", "(C) maps names", "A process runs code"]}
        question, _ = validate(raw, self.units)
        self.assertEqual([question["options"][i] for i in (0, 1, 3)],
                         ["A. reserves resources", "B. orders tasks", "D. A process runs code"])

    def test_the_quote_is_required_and_must_exist_in_the_context(self):
        good = raw_candidate(0)
        for quote in ("", "short", "The moon orbits the earth every twenty seven days."):
            self.rejected({**good, "evidence_quote": quote}, "grounding")
        self.rejected({key: value for key, value in good.items() if key != "evidence_quote"}, "grounding")

    def test_a_real_quote_must_support_the_question_and_its_answer(self):
        self.rejected(raw_candidate(5, evidence_quote=fact_sentence(3)), "grounding")  # other topic's sentence
        invented = raw_candidate(5, options=["totally invented claim", "reserves resources", "orders processes", "maps addresses"],
                                 question="What does the compiler do?")
        self.rejected(invented, "grounding")

    def test_an_answer_that_appears_nowhere_in_the_context_is_rejected(self):
        raw = raw_candidate(5, question="What does the compiler translate during processing?",
                            options=["Human written code", "orders processes", "maps addresses", "connects endpoints"])
        self.rejected(raw, "grounding")

    def test_knowledge_from_outside_the_document_is_not_accepted_as_an_answer(self):
        # the sentence is real and the question is about it, but the answer comes from general knowledge
        raw = raw_candidate(5, question="Which optimisation level does the compiler use by default?",
                            options=["Level three aggressive", "Level zero minimal", "Level two balanced", "Level one basic"])
        self.rejected(raw, "grounding")

    def test_partial_support_is_accepted_with_a_ranking_warning(self):
        raw = raw_candidate(0, question="Which duty of the mutex matters while running programs today?",
                            options=["It reserves resources", "It orders processes", "It maps addresses", "It connects endpoints"])
        question, warnings = validate(raw, self.units)
        self.assertIn("weak_relevance", warnings)
        self.assertEqual(question["validation_outcome"], "accepted_quality_warning")

    def test_a_tolerated_quote_edit_is_accepted_but_flagged(self):
        sentence = fact_sentence(4).replace("The ", "").replace("during", "while")
        question, warnings = validate(raw_candidate(4, evidence_quote=sentence), self.units)
        self.assertIn("quote_not_verbatim", warnings)
        self.assertEqual(question["source_chunk_ids"], self.units[4]["source_chunk_ids"])

    def test_provenance_comes_from_where_the_quote_is_found(self):
        question, _ = validate(raw_candidate(10), self.units)
        self.assertEqual(question["source_chunk_ids"], ["chunk_11"])
        self.assertEqual(question["concept_id"], self.units[10]["unit_id"])

    def test_duplicate_stem_same_evidence_and_same_concept_are_rejected(self):
        first, _ = validate(raw_candidate(0), self.units)
        self.rejected(raw_candidate(0), "duplicate", [first])
        self.rejected(raw_candidate(0, question="Explain what the mutex does in this document?"), "duplicate", [first])
        same_quote = raw_candidate(1, evidence_quote=fact_sentence(0), question="What does the mutex reserve?",
                                   options=["reserves resources", "orders processes", "maps addresses", "connects endpoints"])
        self.rejected(same_quote, "duplicate", [first])

    def test_distinct_facts_are_all_accepted(self):
        accepted = []
        for fact in range(16):
            question, _ = validate(raw_candidate(fact), self.units, accepted)
            accepted.append(question)
        self.assertEqual(len(accepted), 16)

    def test_junk_candidates_are_rejected(self):
        for raw in ("text", None, 5, [], {}):
            with self.assertRaises(CandidateRejected):
                validate(raw, self.units)

    def test_pipeline_scaffolding_in_the_stem_or_options_is_rejected_but_only_warned_in_the_explanation(self):
        self.rejected(raw_candidate(0, question="According to [U3], what does the mutex do?"), "structure")
        self.rejected(raw_candidate(0, options=["reserves resources [U2]", "orders tasks", "maps names", "connects ports"]), "structure")
        question, warnings = validate(raw_candidate(0, explanation="According to the study unit (unit_id U2), the mutex reserves."), self.units)
        self.assertIn("explanation_scaffolding", warnings)

    def test_numeric_or_very_short_answers_cannot_be_anchored_and_are_only_ranked_lower(self):
        raw = raw_candidate(0, options=["4", "8", "16", "32"], question="How many mutex locks does the kernel reserve?")
        question, warnings = validate(raw, self.units)
        self.assertIn("unverified_answer", warnings)
        self.assertEqual(question["validation_outcome"], "accepted_quality_warning")

    def test_one_accidental_matching_word_in_glued_text_is_not_an_anchor(self):
        chunks = make_chunks(8, 2) + [{"content": "Thebackstackframesareglued.Nothingelsehere. " + "context words " * 40,
                                       "metadata": {"chunk_id": "glue"}}]
        units = build_study_units(chunks)
        raw = raw_candidate(0, question="What does the mutex reserve?",
                            options=["The wireless antenna stack", "orders tasks", "maps names", "connects ports"])
        self.rejected(raw, "grounding", units=units)  # "stack" sits inside "backstackframes"; the rest occurs nowhere
        half = raw_candidate(0, question="What does the mutex reserve?", options=["It reserves antenna", "orders tasks", "maps names", "connects ports"])
        self.assertEqual(validate(half, units)[0]["correct_answer"], "A")


def bilingual_units():
    """Six excerpts in the style of the real PDFs: an English bullet, then its Vietnamese translation."""
    pairs = [
        ("Favors CPU-bound processes over I/O-bound processes.", "Ưu tiên tiến trình hướng CPU hơn tiến trình hướng I/O."),
        ("A non-preemptive policy.", "Chính sách không giành quyền điều phối."),
        ("Each process is given a time slice (quantum) before being preempted.",
         "Mỗi tiến trình được cấp một khoảng thời gian (quanta) trước khi bị giành quyền."),
        ("TCP uses a three-way handshake to establish a connection.", "TCP dùng bắt tay ba bước để thiết lập kết nối."),
        ("The page table maps virtual addresses to physical frames.", "Bảng trang ánh xạ địa chỉ ảo sang khung vật lý."),
        ("A heading called Quantum Computing appears in this unit.", "Một tiêu đề có tên Điện toán lượng tử xuất hiện."),
    ]
    return build_study_units([
        {"content": f"{en} → {vi} Note {i}: " + "background text here " * 30, "metadata": {"chunk_id": f"b{i}"}}
        for i, (en, vi) in enumerate(pairs, start=1)])


class RelaxedValidationTests(unittest.TestCase):
    """Bilingual documents, glued PDFs and similar technical terms must not lose valid questions."""

    def setUp(self):
        self.units = bilingual_units()

    def cand(self, quote, question, options, answer_index=0, explanation="The document states this directly."):
        return {"evidence_quote": quote, "question": question, "options": options,
                "answer_index": answer_index, "explanation": explanation}

    def test_similar_technical_terms_as_options_are_fine(self):
        cases = [
            self.cand("A non-preemptive policy.", "Is this scheduling policy preemptive or non-preemptive?",
                      ["Non-preemptive", "Preemptive", "Clock driven", "Feedback based"]),
            self.cand("Favors CPU-bound processes over I/O-bound processes.", "Which kind of process does this policy favor?",
                      ["CPU-bound processes", "I/O-bound processes", "Both equally", "Neither kind"]),
            self.cand("Favors CPU-bound processes over I/O-bound processes.", "Which kind of process does this policy favor?",
                      ["CPU bound", "I/O bound", "Both", "Neither"]),
            self.cand("Each process is given a time slice (quantum) before being preempted.",
                      "What is each process given before being preempted?", ["time slice (quantum)", "time slice", "priority boost", "memory page"]),
        ]
        for raw in cases:
            question, warnings = validate_candidate(raw, self.units, [], "easy", 1, SCOPE, 1)
            self.assertEqual(question["question_type"], "single_choice", raw["options"])
            self.assertEqual(warnings, [], raw["options"])

    def test_vietnamese_question_about_an_english_bullet_is_judged_by_the_translation(self):
        raw = self.cand("TCP uses a three-way handshake to establish a connection.",
                        "TCP thiết lập kết nối bằng cách nào?", ["Bắt tay ba bước", "Gửi gói rác", "Tắt máy chủ", "Xóa bảng"])
        self.assertEqual(validate(raw, self.units)[0]["source_chunk_ids"], ["b4"])
        raw = self.cand("Each process is given a time slice (quantum) before being preempted.",
                        "Mỗi tiến trình được cấp gì trước khi bị giành quyền điều phối?",
                        ["Một khoảng thời gian (quanta)", "Một mức ưu tiên", "Một trang bộ nhớ", "Một tỷ lệ phản hồi"])
        self.assertEqual(validate(raw, self.units)[0]["correct_answer"], "A")

    def test_the_language_of_the_question_is_never_compared_with_the_language_of_the_quote(self):
        for question, options in (
            ("What does the page table map to physical frames?", ["Virtual addresses", "Network packets", "Disk sectors", "User names"]),
            ("Bảng trang ánh xạ cái gì sang khung vật lý?", ["Địa chỉ ảo", "Gói tin mạng", "Cung đĩa", "Tên người dùng"]),
        ):
            for quote in ("The page table maps virtual addresses to physical frames.", "Bảng trang ánh xạ địa chỉ ảo sang khung vật lý."):
                self.assertEqual(validate(self.cand(quote, question, options), self.units)[0]["source_chunk_ids"], ["b5"], (question, quote))

    def test_a_language_that_appears_nowhere_in_the_context_cannot_be_verified_and_is_rejected(self):
        raw = raw_candidate(0, question="Cơ chế nào giữ tài nguyên cho tiến trình?", options=["Khóa loại trừ", "Bộ nhớ đệm", "Bảng trang", "Ngắt đồng hồ"])
        with self.assertRaises(CandidateRejected):
            validate(raw, build_study_units(make_chunks(8, 1)))

    def test_the_answer_may_come_from_another_excerpt_but_not_from_nowhere(self):
        raw = self.cand("A non-preemptive policy.", "What kind of policy is described in this bullet?",
                        ["Time slice quantum", "Network packets", "Disk sectors", "User names"])
        self.assertEqual(validate(raw, self.units)[0]["correct_answer"], "A")
        nowhere = self.cand("The page table maps virtual addresses to physical frames.", "What does the page table map to physical frames?",
                            ["Bananas and zebras", "Virtual addresses", "Disk sectors", "User names"])
        with self.assertRaises(CandidateRejected):
            validate(nowhere, self.units)

    def test_unrelated_question_with_a_real_quote_is_rejected_in_any_language(self):
        for question, options in (
            ("What does a compiler translate into machine code?", ["Source programs", "Network packets", "Disk blocks", "Interrupt vectors"]),
            ("Trình biên dịch dịch mã nguồn thành gì?", ["Mã máy", "Gói tin mạng", "Khối đĩa", "Vectơ ngắt"]),
        ):
            with self.assertRaises(CandidateRejected):
                validate(self.cand("The page table maps virtual addresses to physical frames.", question, options), self.units)

    def test_one_long_word_of_the_material_anchors_an_answer_padded_with_filler_words(self):
        units = build_study_units([{"content": "Nonblocking, so there are few explicit synchronization primitives. " + "context words " * 40,
                                    "metadata": {"chunk_id": "n1"}}])
        raw = self.cand("Nonblocking, so there are few explicit synchronization primitives.",
                        "Why does this kernel need so few synchronisation primitives?",
                        ["Because its operations are nonblocking", "Because it has no scheduler", "Because tasks never start", "Because memory is unlimited"])
        self.assertEqual(validate(raw, units)[0]["correct_answer"], "A")  # "because"/"operations" are not in the text, "nonblocking" is
        made_up = self.cand("Nonblocking, so there are few explicit synchronization primitives.",
                            "Why does this kernel need so few synchronisation primitives?",
                            ["Because its stack overflows", "Because it has no scheduler", "Because tasks never start", "Because memory is unlimited"])
        with self.assertRaises(CandidateRejected):
            validate(made_up, units)

    def test_a_long_word_from_another_excerpt_does_not_rescue_an_invented_answer(self):
        units = build_study_units([
            {"content": "Nonblocking, so there are few explicit synchronization primitives. " + "context words " * 40, "metadata": {"chunk_id": "n1"}},
            {"content": "Wireless sensor networks are mentioned only here. " + "other words " * 45, "metadata": {"chunk_id": "n2"}},
        ])
        raw = self.cand("Nonblocking, so there are few explicit synchronization primitives.",
                        "Why does this kernel need so few synchronisation primitives?",
                        ["Because wireless flooding happens", "Because it has no scheduler", "Because tasks never start", "Because memory is unlimited"])
        with self.assertRaises(CandidateRejected):
            validate(raw, units)  # "wireless" is a real word of the document, but not of the excerpt being quoted

    def test_words_found_in_most_excerpts_carry_no_evidence(self):
        chunks = [{"content": f"Fact number {i} is really important for this course, {word} explained. " + "context words " * 40,
                   "metadata": {"chunk_id": f"w{i}"}} for i, word in enumerate(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"], 1)]
        units = build_study_units(chunks)
        self.assertEqual(context_support("Is this really important?", "really important", units[0], units), (None, None))
        self.assertEqual(context_support("Is charlie really important?", "charlie", units[2], units), (1.0, 1.0))
        raw = self.cand("Fact number 1 is really important for this course, alpha explained.", "Is this really important?",
                        ["Really important", "Never needed", "Sometimes odd", "Rarely used"])
        self.assertIn("unverifiable_relevance", validate(raw, units)[1])


# ---------------------------------------------------------------------------------------------
# H. Selection
# ---------------------------------------------------------------------------------------------
class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(12, facts_per_chunk=2))

    def pool(self, facts, weak=()):
        accepted = []
        for index, fact in enumerate(facts):
            question, _ = validate_candidate(raw_candidate(fact), self.units, accepted, "easy", index + 1, SCOPE, index + 1)
            if fact in weak:
                question["validation_outcome"] = "accepted_quality_warning"
            accepted.append(question)
        return accepted

    def test_the_best_candidates_are_kept_and_returned_in_document_order(self):
        pool = self.pool(list(range(23, -1, -1)))          # generated in reverse document order
        chosen = select_questions(pool, 12)
        self.assertEqual(len(chosen), 12)
        self.assertEqual([q["_meta"]["unit_index"] for q in chosen], sorted(q["_meta"]["unit_index"] for q in chosen))

    def test_quality_beats_position_when_there_are_more_candidates_than_needed(self):
        pool = self.pool(list(range(15)), weak={0, 1, 2})
        chosen = select_questions(pool, 12)
        self.assertEqual({q["validation_outcome"] for q in chosen}, {"accepted"})
        self.assertEqual(len(select_questions(pool, 15)), 15)          # ... and a big enough target keeps everything

    def test_a_short_pool_returns_every_valid_candidate(self):
        for size in (1, 5, 11):
            self.assertEqual(len(select_questions(self.pool(list(range(size))), 12)), size)
        self.assertEqual(select_questions([], 12), [])

    def test_selection_never_exceeds_the_target(self):
        self.assertEqual(len(select_questions(self.pool(list(range(24))), 20)), 20)


# ---------------------------------------------------------------------------------------------
# I. Engine
# ---------------------------------------------------------------------------------------------
class EngineTests(unittest.TestCase):
    def setUp(self):
        FakeModel.reset()

    def run_engine(self, payloads, chunks=None, question_count=12, scope="document", difficulty="easy"):
        FakeModel.reset(payloads)
        saved = []
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: saved.append(quiz) or quiz),
        ):
            result = _generate_quiz_from_units(
                document=DOCUMENT, scope=scope,
                scope_topic_id="topic_1" if scope == "topic" else "document",
                scope_topic_name="Topic 1" if scope == "topic" else "Entire document",
                chunks=chunks if chunks is not None else make_chunks(12, 2),
                difficulty=difficulty, owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )
        return result, saved

    def assert_valid_quiz(self, quiz, count):
        self.assertEqual(len(quiz["questions"]), count)
        self.assertEqual([q["id"] for q in quiz["questions"]], list(range(1, count + 1)))
        for question in quiz["questions"]:
            self.assertEqual(question["question_type"], "single_choice")
            self.assertEqual(len(question["options"]), 4)
            self.assertEqual(len(question["correct_answers"]), 1)
            self.assertEqual(question["correct_answer"], question["correct_answers"][0])
            self.assertIn(question["correct_answer"], "ABCD")
            self.assertTrue(question["source_chunk_ids"])
            self.assertNotIn("_meta", question)
        self.assertEqual(len({q["question"] for q in quiz["questions"]}), count)

    # --- the four counts ---------------------------------------------------------------------
    def test_each_requested_count_writes_its_surplus_in_one_call_and_returns_exactly_the_target(self):
        for requested, pool in ((12, 15), (15, 18), (18, 22), (20, 24)):
            with self.subTest(requested=requested):
                result, saved = self.run_engine([{"questions": candidates(range(pool))}], question_count=requested)
                self.assert_valid_quiz(result, requested)
                plan = result["assessment_plan"]
                self.assertIn(f"Write exactly {pool} easy", FakeModel.prompts[0])
                self.assertEqual(len(FakeModel.prompts), 1)
                self.assertEqual(plan["llm_calls"], 1)
                self.assertEqual((plan["requested_count"], plan["actual_count"], plan["missing_count"]), (requested, requested, 0))
                self.assertEqual((plan["status"], plan["partial"]), ("complete", False))
                self.assertEqual(plan["candidate_pool_size"], pool)
                self.assertEqual(len(saved), 1)

    def test_surplus_valid_candidates_are_trimmed_to_the_request(self):
        result, _ = self.run_engine([{"questions": candidates(range(24))}], question_count=12)
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["candidate_pool_size"], 24)

    def test_invalid_candidates_in_the_surplus_do_not_cost_a_second_call(self):
        good = candidates(range(12))
        bad = [raw_candidate(0, evidence_quote="a sentence that is not in the document at all"),
               raw_candidate(4, options=["True", "False"], answer_index=0),
               {**raw_candidate(6), "answer_index": [0, 1]}]
        result, _ = self.run_engine([{"questions": good[:4] + bad[:1] + good[4:8] + bad[1:] + good[8:]}])
        self.assert_valid_quiz(result, 12)
        results = result["assessment_plan"]["validation_results"]
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)
        self.assertEqual((results["rejected"], results["grounding_rejections"], results["structural_rejections"]), (3, 1, 2))

    # --- shortage: partial, never a failure ---------------------------------------------------
    def test_a_short_pool_returns_what_is_valid_as_a_partial_quiz(self):
        for requested, valid in ((18, 16), (20, 17), (12, 11), (15, 1)):
            with self.subTest(requested=requested, valid=valid):
                first = candidates(range(valid))
                result, saved = self.run_engine([{"questions": first}, {"questions": []}], question_count=requested)
                self.assert_valid_quiz(result, valid)
                plan = result["assessment_plan"]
                self.assertEqual((plan["requested_count"], plan["actual_count"], plan["status"], plan["partial"]),
                                 (requested, valid, "partial", True))
                self.assertEqual(plan["missing_count"], requested - valid)
                self.assertEqual(result["question_count"], valid)
                self.assertEqual(len(saved), 1)  # persisted, not failed

    def test_a_follow_up_writes_a_moderate_batch_of_only_what_is_missing(self):
        result, _ = self.run_engine([{"questions": candidates(range(9))}, {"questions": candidates(range(9, 15))}])
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        second = FakeModel.prompts[1]
        self.assertIn(f"Write exactly {followup_request(3)} easy", second)         # 3 missing + a small surplus, not 15
        self.assertEqual(followup_request(3), 5)
        self.assertEqual(second.count("[source:"), 9)                              # every existing question is listed with its sentence
        self.assertIn(f'- What does the mutex do?  [source: "{fact_sentence(0)}"]', second)

    def test_the_pipeline_keeps_asking_in_small_batches_until_the_target_is_met_then_stops(self):
        # 20 questions: the model delivers only 6, 4, 5 and 5 valid questions per call
        payloads = [{"questions": candidates(range(0, 6))}, {"questions": candidates(range(6, 10))},
                    {"questions": candidates(range(10, 15))}, {"questions": candidates(range(15, 20))},
                    {"questions": candidates(range(20, 24))}]
        result, _ = self.run_engine(payloads, question_count=20)
        self.assert_valid_quiz(result, 20)
        plan = result["assessment_plan"]
        self.assertEqual((plan["status"], plan["llm_calls"], len(FakeModel.prompts)), ("complete", 4, 4))
        self.assertEqual(len(FakeModel.payloads), 1)                               # the 5th call was never made: enough
        asks = [int(re.search(r"Write exactly (\d+)", prompt).group(1)) for prompt in FakeModel.prompts]
        self.assertEqual(asks, [24, 8, 8, 7])                                     # 14, 10 and 5 missing -> 8, 8 and 7
        self.assertEqual([call["valid_added"] for call in plan["calls"]], [6, 4, 5, 5])
        self.assertEqual([len(prompt.split("[source:")) - 1 for prompt in FakeModel.prompts], [0, 6, 10, 15])

    def test_the_call_ceiling_per_requested_count(self):
        for requested, ceiling in ((12, 4), (15, 4), (18, 5), (20, 5)):
            with self.subTest(requested=requested):
                # the model adds exactly one new valid question per call, forever
                payloads = [{"questions": candidates([call])} for call in range(10)]
                result, _ = self.run_engine(payloads, question_count=requested)
                plan = result["assessment_plan"]
                self.assertEqual(len(FakeModel.prompts), ceiling)
                self.assertEqual((plan["llm_calls"], plan["max_llm_calls"]), (ceiling, ceiling))
                self.assert_valid_quiz(result, ceiling)                            # everything valid is returned, nothing invented
                self.assertEqual((plan["status"], plan["requested_count"], plan["actual_count"]), ("partial", requested, ceiling))
                self.assertTrue(any("call limit reached" in reason for reason in plan["generation_warnings"]))

    def test_a_short_first_call_is_completed_by_follow_ups_across_requests(self):
        for requested in (12, 15, 18, 20):
            with self.subTest(requested=requested):
                first = candidates(range(requested - 4))
                second = candidates(range(requested - 4, requested + 2))
                result, _ = self.run_engine([{"questions": first}, {"questions": second}], question_count=requested)
                self.assert_valid_quiz(result, requested)
                self.assertEqual(result["assessment_plan"]["status"], "complete")
                self.assertEqual(result["assessment_plan"]["llm_calls"], 2)

    def test_two_calls_in_a_row_that_add_nothing_stop_the_loop(self):
        payloads = [{"questions": candidates(range(3))}, {"questions": []},
                    {"questions": [raw_candidate(5, evidence_quote="a sentence that is nowhere in the document")]},
                    {"questions": candidates(range(3, 9))}]
        result, _ = self.run_engine(payloads, question_count=20)
        plan = result["assessment_plan"]
        self.assertEqual((len(FakeModel.prompts), plan["llm_calls"]), (3, 3))     # 4th call never made
        self.assertEqual(len(FakeModel.payloads), 1)
        self.assert_valid_quiz(result, 3)
        self.assertEqual(plan["status"], "partial")
        self.assertTrue(any("added no valid question" in reason for reason in plan["generation_warnings"]))

    def test_a_call_that_adds_nothing_does_not_stop_the_loop_if_the_next_one_does(self):
        payloads = [{"questions": candidates(range(6))}, {"questions": []}, {"questions": candidates(range(6, 14))}]
        result, _ = self.run_engine(payloads)
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 3)

    def test_no_further_call_when_the_target_is_met(self):
        self.run_engine([{"questions": candidates(range(12))}, {"questions": candidates([12])}])
        self.assertEqual(len(FakeModel.prompts), 1)

    def test_follow_ups_avoid_excerpts_the_accepted_questions_already_came_from(self):
        result, _ = self.run_engine([{"questions": candidates(range(6))}, {"questions": candidates(range(6, 12))}])
        self.assert_valid_quiz(result, 12)
        first = {int(m) for m in re.findall(r"\[U(\d+)\]", FakeModel.prompts[0])}
        second = {int(m) for m in re.findall(r"\[U(\d+)\]", FakeModel.prompts[1])}
        self.assertEqual(first, set(range(1, 13)))          # a small document is shown whole in call 1 ...
        self.assertFalse(second & {1, 2, 3})                # ... facts 0-5 live in U1-U3, which a follow-up skips
        self.assertTrue(second)

    def test_every_call_is_logged_with_the_evidence_needed_to_diagnose_a_short_answer(self):
        result, _ = self.run_engine([{"questions": candidates(range(4))}, {"questions": candidates(range(4, 7))}, {"questions": []}],
                                    question_count=12)
        calls = result["assessment_plan"]["calls"]
        self.assertEqual([call["call"] for call in calls], [1, 2, 3, 4][:len(calls)])
        first = calls[0]
        self.assertEqual((first["asked"], first["returned"], first["valid_added"], first["rejected"]), (15, 4, 4, 0))
        self.assertEqual(first["min_items"], int(15 * quiz_units.QUIZ_MIN_ITEMS_RATIO))
        self.assertEqual((first["generated_tokens"], first["prompt_tokens"], first["done_reason"]), (123, 456, "stop"))
        self.assertEqual((first["cut"], first["salvaged"], first["quote_keys"], first["error"]), (False, False, 4, None))
        self.assertEqual(first["num_predict"], min(quiz_units.QUIZ_MAX_NEW_TOKENS, (15 + quiz_units.QUIZ_MAX_ITEMS_EXTRA) * quiz_units.QUIZ_TOKENS_PER_QUESTION))
        self.assertGreater(first["tokens_per_s"], 0)
        self.assertEqual(calls[1]["asked"], followup_request(8))

    def test_a_failed_call_is_logged_as_an_error_not_as_rejections(self):
        result, _ = self.run_engine(["not json at all", {"questions": candidates(range(14))}])
        calls = result["assessment_plan"]["calls"]
        self.assertIn("ValueError", calls[0]["error"])
        self.assertEqual((calls[0]["returned"], calls[0]["valid_added"], calls[0]["rejected"]), (0, 0, 0))
        self.assertEqual(result["assessment_plan"]["validation_results"]["response_failures"], candidate_target(12))

    def test_nothing_is_invented_and_no_rule_is_relaxed_to_reach_the_target(self):
        result, _ = self.run_engine([{"questions": candidates(range(7)) + [raw_candidate(8, evidence_quote="invented sentence about nothing")]},
                                     {"questions": [raw_candidate(9, options=["reserves", "orders"])]},
                                     {"questions": [raw_candidate(9, options=["reserves", "orders"])]}], question_count=18)
        self.assert_valid_quiz(result, 7)
        quotes = {record["evidence_quote"] for record in result["assessment_plan"]["question_evidence"]}
        self.assertTrue(all(quote in " ".join(c["content"] for c in make_chunks(12, 2)) for quote in quotes))

    def test_options_that_repeat_each_other_are_rejected_inside_the_pipeline(self):
        twin = raw_candidate(0, options=["reserves the shared resources", "reserves shared resources", "orders tasks", "maps names"])
        result, _ = self.run_engine([{"questions": [twin] + candidates(range(1, 14))}])
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["validation_results"]["structural_rejections"], 1)

    # --- failure only when there is no valid question ------------------------------------------
    def test_zero_valid_candidates_is_the_only_failure(self):
        with self.assertRaises(QuizGenerationError) as context:
            self.run_engine([{"questions": [raw_candidate(0, evidence_quote="invented text that is not in the file")]}, {"questions": []},
                             {"questions": candidates(range(6))}])
        self.assertEqual(context.exception.detail["stage"], "validation")
        self.assertEqual(len(FakeModel.prompts), 2)   # two calls in a row added nothing: stop, do not burn the rest

    def test_one_valid_question_is_enough_to_return_a_quiz(self):
        result, _ = self.run_engine([{"questions": [raw_candidate(0)]}, {"questions": []}, {"questions": []}], question_count=20)
        self.assert_valid_quiz(result, 1)
        self.assertEqual(result["assessment_plan"]["status"], "partial")

    def test_no_context_fails_before_any_llm_call(self):
        with self.assertRaises(QuizGenerationError) as context:
            self.run_engine([], chunks=[])
        self.assertEqual(context.exception.detail["stage"], "context_grouping")
        self.assertEqual(FakeModel.prompts, [])

    def test_a_failed_first_call_can_still_recover_with_a_follow_up(self):
        result, _ = self.run_engine(["not json at all", {"questions": candidates(range(14))}])
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(result["assessment_plan"]["validation_results"]["response_failures"], candidate_target(12))
        self.assertIn(f"Write exactly {followup_request(12)} easy", FakeModel.prompts[1])

    def test_truncated_output_is_salvaged_without_a_second_call(self):
        payload = json.dumps({"questions": candidates(range(15))})
        cut = payload[: payload.rindex('{"question"') + 30]
        result, _ = self.run_engine([cut])
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 1)

    # --- context and model settings ---------------------------------------------------------------
    def test_follow_ups_show_text_no_earlier_call_showed(self):
        with self.assertRaises(QuizGenerationError):
            self.run_engine([{"questions": []}, {"questions": []}], chunks=make_chunks(60, 1))
        first = {int(m) for m in re.findall(r"\[U(\d+)\]", FakeModel.prompts[0])}
        second = {int(m) for m in re.findall(r"\[U(\d+)\]", FakeModel.prompts[1])}
        self.assertTrue(first and second)
        self.assertFalse(first & second)

    def test_grounding_is_judged_against_the_context_the_model_was_given(self):
        chunks = make_chunks(24, 1)  # excerpt i holds fact i, and the request does not fit in one call
        result, _ = self.run_engine([{"questions": candidates(range(24))}], chunks=chunks)
        shown = {int(m) for m in re.findall(r"\[U(\d+)\]", FakeModel.prompts[0])}
        self.assertTrue(0 < len(shown) < 24)
        results = result["assessment_plan"]["validation_results"]
        self.assertEqual(results["grounding_rejections"], 24 - len(shown))  # quotes from unseen excerpts are not evidence
        self.assertEqual(results["accepted"] + results["accepted_with_warnings"], len(shown))
        shown_chunks = {f"chunk_{n}" for n in shown}
        for question in result["questions"]:
            self.assertTrue(set(question["source_chunk_ids"]) <= shown_chunks)

    def test_prompt_size_is_bounded_for_any_document_and_grows_with_the_request(self):
        sizes = []
        for requested in (12, 20):
            with self.assertRaises(QuizGenerationError):
                self.run_engine([{"questions": []}, {"questions": []}], chunks=marked_chunks(900), question_count=requested)
            evidence = FakeModel.prompts[0][FakeModel.prompts[0].index("EXCERPTS:"):]
            self.assertLess(len(evidence), quiz_units.QUIZ_CONTEXT_MAX_CHARS + 1500)
            sizes.append(len(evidence))
        self.assertLess(sizes[0], sizes[1])

    def test_model_settings_follow_the_request(self):
        result, _ = self.run_engine([{"questions": candidates(range(5))}, {"questions": candidates(range(5, 10))}], question_count=20)
        self.last_plan = result["assessment_plan"]
        first, second = FakeModel.kwargs[:2]
        self.assertEqual({first["num_ctx"], second["num_ctx"]}, {quiz_units.QUIZ_NUM_CTX})
        extra = quiz_units.QUIZ_MAX_ITEMS_EXTRA
        self.assertEqual(first["num_predict"], min(quiz_units.QUIZ_MAX_NEW_TOKENS, (24 + extra) * quiz_units.QUIZ_TOKENS_PER_QUESTION))
        self.assertEqual(second["num_predict"], (followup_request(15) + extra) * quiz_units.QUIZ_TOKENS_PER_QUESTION)
        self.assertLess(second["num_predict"], first["num_predict"])
        self.assertLessEqual(first["client_kwargs"]["timeout"], quiz_units.QUIZ_LLM_TIMEOUT_S)
        calls = self.last_plan["calls"]
        self.assertEqual(first["format"], output_schema_for(24, calls[0]["material_chars"]))
        self.assertEqual(second["format"], output_schema_for(followup_request(15), calls[1]["material_chars"]))
        for call, kwargs in zip(calls[:2], (first, second)):
            self.assertEqual(call["min_items"], kwargs["format"]["properties"]["questions"]["minItems"])
            self.assertLessEqual(call["min_items"], int(call["asked"] * quiz_units.QUIZ_MIN_ITEMS_RATIO))
            self.assertLessEqual(call["min_items"], call["material_chars"] // quiz_units.QUIZ_CHARS_PER_MIN_ITEM)

    def test_a_small_document_is_not_forced_to_write_more_questions_than_it_can_carry(self):
        small = make_chunks(3, 2, pad=False)
        result, _ = self.run_engine([{"questions": candidates(range(4))}, {"questions": []}, {"questions": []}], chunks=small)
        first = result["assessment_plan"]["calls"][0]
        self.assertLess(first["material_chars"], 2000)
        self.assertEqual(first["asked"], 15)
        self.assertEqual(first["min_items"], max(1, first["material_chars"] // quiz_units.QUIZ_CHARS_PER_MIN_ITEM))
        self.assertLess(first["min_items"], int(15 * quiz_units.QUIZ_MIN_ITEMS_RATIO))
        self.assertEqual(FakeModel.kwargs[0]["format"]["properties"]["questions"]["maxItems"], 18)
        self.assertEqual(result["assessment_plan"]["status"], "partial")     # still returns what is valid

    def test_a_large_document_keeps_the_ratio_based_minimum(self):
        result, _ = self.run_engine([{"questions": candidates(range(15))}], chunks=make_chunks(40, 2))
        first = result["assessment_plan"]["calls"][0]
        self.assertGreaterEqual(first["material_chars"], 5000)
        self.assertEqual(first["min_items"], 10)

    def test_topic_scope_uses_the_same_pipeline(self):
        result, _ = self.run_engine([{"questions": candidates(range(15))}], scope="topic")
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_scope"], "topic")

    def test_true_false_and_multi_select_shaped_output_never_reaches_the_quiz(self):
        good = candidates(range(2, 17))
        result, _ = self.run_engine([{"questions": [raw_candidate(0, options=["True", "False"]), {**raw_candidate(1), "answer_index": [0, 1]}] + good}])
        self.assert_valid_quiz(result, 12)
        self.assertEqual(result["assessment_plan"]["validation_results"]["structural_rejections"], 2)

    def test_no_topic_coverage_or_planner_metadata_is_produced(self):
        result, _ = self.run_engine([{"questions": candidates(range(15))}])
        plan = result["assessment_plan"]
        for forbidden in ("coverage", "regions_covered", "units_covered", "concept_plan_id"):
            self.assertNotIn(forbidden, plan)
        self.assertEqual(plan["planner_version"], quiz_units.QUIZ_ENGINE_VERSION)
        self.assertEqual(plan["generation_engine"], "context_candidates")

    def test_evidence_is_recorded_for_every_question(self):
        result, _ = self.run_engine([{"questions": candidates(range(15))}])
        records = result["assessment_plan"]["question_evidence"]
        self.assertEqual(len(records), 12)
        self.assertTrue(all(r["evidence_quote"] and r["quote_alignment"] >= 0.85 for r in records))


# ---------------------------------------------------------------------------------------------
# J / K. Cache and API metadata
# ---------------------------------------------------------------------------------------------
class CacheAndMetadataTests(unittest.TestCase):
    def saved_quiz(self, planner_version, requested=12, actual=12):
        return {
            "quiz_id": "old", "document_id": DOCUMENT["id"], "question_count": actual,
            "assessment_plan": {"planner_version": planner_version, "target_questions": requested,
                                "requested_count": requested, "actual_count": actual},
            "questions": [{"id": index} for index in range(1, actual + 1)],
        }

    def call(self, saved, count=12, payload=None, regenerate=False, public=True):
        FakeModel.reset([{"questions": payload if payload is not None else candidates(range(candidate_target(count)))}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"),
            patch.object(quiz_service, "get_quiz", return_value=saved),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
            patch.object(quiz_service, "delete_document_attempts"),
        ):
            fn = quiz_service.generate_quiz if public else quiz_service._generate_quiz
            return fn(DOCUMENT["id"], "easy", "document", question_count=count, regenerate=regenerate)

    def test_a_quiz_saved_by_an_older_engine_never_hides_the_new_pipeline(self):
        for old in ("simple_context_v7_old", "study_units_v6", "study_units_v5", "study_units_v4",
                    "context_group_v3_no_planner", "legacy", ""):
            result = self.call(self.saved_quiz(old))
            self.assertEqual(result["assessment_plan"]["planner_version"], quiz_units.QUIZ_ENGINE_VERSION, old)
            self.assertEqual(len(FakeModel.prompts), 1)

    def test_a_quiz_from_the_current_engine_is_reused_even_when_it_is_partial(self):
        for requested, actual in ((12, 12), (18, 16), (20, 17)):
            result = self.call(self.saved_quiz(quiz_units.QUIZ_ENGINE_VERSION, requested, actual), count=requested)
            self.assertEqual(result["quiz_id"], "old")
            self.assertEqual(FakeModel.prompts, [])
            self.assertEqual((result["requested_count"], result["actual_count"]), (requested, actual))

    def test_a_different_requested_count_or_regeneration_generates_again(self):
        saved = self.saved_quiz(quiz_units.QUIZ_ENGINE_VERSION, 12, 12)
        self.call(saved, count=18)
        self.assertEqual(len(FakeModel.prompts), 1)
        self.call(saved, count=12, regenerate=True)
        self.assertEqual(len(FakeModel.prompts), 1)

    def test_engine_version_is_distinct_from_every_earlier_engine(self):
        self.assertNotIn(quiz_units.QUIZ_ENGINE_VERSION, {"study_units_v6", "study_units_v5", "study_units_v4", "context_group_v3_no_planner", "legacy"})

    def test_metadata_reflects_a_complete_quiz(self):
        result = self.call(None, count=18)
        self.assertEqual((result["requested_count"], result["actual_count"], result["status"]), (18, 18, "complete"))
        self.assertEqual(result["question_count"], 18)

    def test_metadata_reflects_a_partial_quiz(self):
        result = self.call(None, count=20, payload=candidates(range(17)))
        self.assertEqual((result["requested_count"], result["actual_count"], result["status"]), (20, 17, "partial"))
        self.assertEqual(result["question_count"], 17)
        plan = result["assessment_plan"]
        self.assertEqual((plan["requested_count"], plan["actual_count"], plan["status"]), (20, 17, "partial"))

    def test_the_api_response_model_carries_the_three_fields(self):
        result = self.call(None, count=18, payload=candidates(range(16)))
        response = QuizGenerateResponse(**result)
        self.assertEqual((response.requested_count, response.actual_count, response.status), (18, 16, "partial"))
        self.assertEqual(len(response.questions), 16)


# ---------------------------------------------------------------------------------------------
# L. Wall-clock budget
# ---------------------------------------------------------------------------------------------
class DeadlineTests(unittest.TestCase):
    def setUp(self):
        FakeModel.reset()

    def run_engine(self, payloads, question_count=12):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )

    def test_streaming_stops_at_the_deadline_and_keeps_what_was_written(self):
        full = json.dumps({"questions": candidates(range(4))})
        FakeModel.reset([{"questions": candidates(range(4))}])
        text, metadata, cut = quiz_service._generate_with_deadline(FakeModel(), "prompt", 0)
        self.assertTrue(cut)
        self.assertTrue(full.startswith(text) and 0 < len(text) < len(full))
        FakeModel.reset([{"questions": candidates(range(4))}])
        text, metadata, cut = quiz_service._generate_with_deadline(FakeModel(), "prompt", 3600)
        self.assertFalse(cut)
        self.assertEqual(text, full)
        self.assertEqual(metadata["eval_count"], 123)

    def test_a_cut_first_call_keeps_its_complete_questions_and_a_follow_up_finishes_the_job(self):
        with patch.object(quiz_service, "QUIZ_FIRST_CALL_DEADLINE_S", 0):
            result = self.run_engine([{"questions": candidates(range(15))}, {"questions": candidates(range(3, 20))}])
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(plan["llm_calls"], 2)
        self.assertTrue(plan["timings_ms"]["deadline_hit"])
        self.assertTrue(plan["calls"][0]["cut"])
        self.assertTrue(any("time limit reached" in reason for reason in plan["generation_warnings"]))
        self.assertEqual(plan["status"], "complete")

    def test_follow_ups_are_skipped_when_the_time_budget_is_used_up(self):
        with patch.object(quiz_service, "QUIZ_MIN_CALL_S", 10 ** 6):
            result = self.run_engine([{"questions": candidates(range(6))}, {"questions": candidates(range(6, 20))}])
        self.assertEqual(len(FakeModel.prompts), 1)
        self.assertEqual(result["assessment_plan"]["status"], "partial")
        self.assertEqual(len(result["questions"]), 6)
        self.assertTrue(any("time budget used up" in reason for reason in result["assessment_plan"]["generation_warnings"]))

    def test_a_call_cut_before_any_complete_question_fails_cleanly(self):
        FakeModel.pieces = 400
        try:
            with patch.object(quiz_service, "QUIZ_FIRST_CALL_DEADLINE_S", 0), patch.object(quiz_service, "QUIZ_MIN_CALL_S", 10 ** 6):
                with self.assertRaises(QuizGenerationError):
                    self.run_engine([{"questions": candidates(range(15))}])
        finally:
            FakeModel.pieces = 4

    def test_a_later_call_has_its_own_deadline_and_cannot_use_more_than_the_time_left(self):
        seen = []
        original = quiz_service._generate_with_deadline

        def spy(llm, prompt, deadline_s):
            seen.append(deadline_s)
            return original(llm, prompt, deadline_s)

        with patch.object(quiz_service, "_generate_with_deadline", spy):
            self.run_engine([{"questions": candidates(range(6))}, {"questions": candidates(range(6, 14))}])
        self.assertEqual(len(seen), 2)
        self.assertLessEqual(seen[0], quiz_units.QUIZ_FIRST_CALL_DEADLINE_S)
        self.assertLessEqual(seen[1], quiz_units.QUIZ_FOLLOWUP_DEADLINE_S)


# ---------------------------------------------------------------------------------------------
# M. Real database round trip (nothing patched in the persistence layer)
# ---------------------------------------------------------------------------------------------
class PersistenceRoundTripTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from backend import quiz_store
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.quiz_store = quiz_store

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def generate(self, count, payload):
        FakeModel.reset([{"questions": payload}, {"questions": []}, {"questions": []}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
        ):
            return quiz_service.generate_quiz(DOCUMENT["id"], "easy", "document", question_count=count, quiz_name="Round trip")

    def test_a_partial_quiz_is_saved_reloaded_and_reused(self):
        first = self.generate(18, candidates(range(16)))
        self.assertEqual((first["requested_count"], first["actual_count"], first["status"]), (18, 16, "partial"))
        stored = self.quiz_store.get_quiz(DOCUMENT["id"], "easy", "document")
        self.assertEqual(len(stored["questions"]), 16)
        plan = stored["assessment_plan"]
        self.assertEqual((plan["requested_count"], plan["actual_count"], plan["status"], plan["partial"]), (18, 16, "partial", True))
        self.assertEqual(plan["planner_version"], quiz_units.QUIZ_ENGINE_VERSION)
        for question in stored["questions"]:
            self.assertEqual(question["question_type"], "single_choice")
            self.assertEqual(len(question["options"]), 4)
            self.assertEqual([option[:2] for option in question["options"]], ["A.", "B.", "C.", "D."])
            self.assertIn(question["correct_answer"], "ABCD")
            self.assertEqual(question["correct_answers"], [question["correct_answer"]])
            self.assertTrue(question["source_chunk_ids"])
        self.assertEqual(len(FakeModel.prompts), 3)  # call 1, then two follow-ups that added nothing (stall guard)
        again = self.generate(18, candidates(range(16)))            # the same request is served from the database
        self.assertEqual(again["quiz_id"], first["quiz_id"])
        self.assertEqual(len(FakeModel.prompts), 0)
        self.assertEqual((again["requested_count"], again["actual_count"], again["status"]), (18, 16, "partial"))

    def test_every_supported_count_round_trips_as_complete(self):
        for count in (12, 15, 18, 20):
            quiz = self.generate(count, candidates(range(candidate_target(count))))
            stored = self.quiz_store.get_quiz(DOCUMENT["id"], "easy", "document")
            self.assertEqual((quiz["status"], len(stored["questions"])), ("complete", count), count)
            self.assertEqual(stored["assessment_plan"]["requested_count"], count)

    def test_validation_events_are_written_for_accepted_candidates(self):
        self.generate(12, candidates(range(15)))
        with self.quiz_store._connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM quiz_validation_events").fetchone()[0]
        self.assertEqual(rows, 15)


if __name__ == "__main__":
    unittest.main()
