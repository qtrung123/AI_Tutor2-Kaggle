"""Fill-in-the-blank questions (safe, objective subset): schema/prompt, deterministic validation
against the original document excerpts, deterministic grading, the bounded generation step with a
retry only on malformed/invalid output, persisted flashcards as coverage hints only (never evidence),
and persistence/resume/submit through the real SQLite store. Free-text
short-answer grading is deliberately not supported: only exact (normalized) matches against answers
explicitly stored with the question are correct.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, fact_sentence, make_chunks, raw_candidate

from backend import quiz_service, quiz_store, quiz_units
from backend.main import QuizQuestion
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import (
    FILL_BLANK_MARKER, QUIZ_FILL_BLANK_OUTPUT_SCHEMA, CandidateRejected, build_fill_blank_prompt, build_study_units,
    fill_blank_is_correct, fill_blank_target, flashcard_hint_terms, ground_fill_blank_hints,
    normalize_fill_blank_answer, validate_candidate, validate_fill_blank_candidate,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
SCOPE = {"topic_id": "document", "name": "Entire document"}


def fill_candidate(fact: int, **overrides) -> dict:
    """A valid fill_blank candidate for fact `fact` of the shared fixtures: blank out its noun."""
    noun = fact_sentence(fact).split()[1]
    candidate = {
        "evidence_quote": fact_sentence(fact),
        "sentence": fact_sentence(fact).replace(noun, FILL_BLANK_MARKER, 1),
        "answer": noun,
        "accepted_answers": [],
        "explanation": f"The document names the {noun} here.",
    }
    candidate.update(overrides)
    return candidate


class SchemaAndPromptTests(unittest.TestCase):
    def test_schema_requires_quote_sentence_answer_and_explanation(self):
        item = QUIZ_FILL_BLANK_OUTPUT_SCHEMA["properties"]["questions"]["items"]
        self.assertEqual(item["required"], ["evidence_quote", "sentence", "answer", "explanation"])
        self.assertNotIn("options", item["properties"])

    def test_prompt_grounds_in_excerpts_and_asks_for_short_objective_answers(self):
        units = build_study_units(make_chunks(4, 2))
        prompt = build_fill_blank_prompt("Lecture", "easy", units, 3)
        self.assertIn("using ONLY the excerpts below", prompt)
        self.assertIn(FILL_BLANK_MARKER, prompt)
        self.assertIn("1-4 words", prompt)
        self.assertIn(units[0]["evidence_excerpt"][:40], prompt)

    def test_target_is_a_small_share_of_the_quiz(self):
        self.assertEqual({n: fill_blank_target(n) for n in (12, 15, 18, 20)}, {12: 2, 15: 2, 18: 3, 20: 3})

    def test_api_model_accepts_fill_blank_questions(self):
        question = QuizQuestion(id=1, question=f"The {FILL_BLANK_MARKER} runs tasks.", options=[], correct_answer="thread",
                                question_type="fill_blank", correct_answers=["thread"], topic_id="document",
                                difficulty="easy", explanation="e", source_chunk_ids=["h1"])
        self.assertEqual(question.question_type, "fill_blank")


class GradingTests(unittest.TestCase):
    def test_trim_case_and_surrounding_punctuation_are_normalized(self):
        for typed in ("Scheduler", "  scheduler  ", "SCHEDULER.", "\"scheduler\"", "(scheduler)", "scheduler!"):
            self.assertTrue(fill_blank_is_correct(typed, ["scheduler"]), typed)
        self.assertEqual(normalize_fill_blank_answer("  Task   Scheduler; "), "task scheduler")

    def test_no_fuzzy_matching(self):
        for typed in ("schedulr", "the scheduler", "scheduler process", "", "   "):
            self.assertFalse(fill_blank_is_correct(typed, ["scheduler"]), typed)

    def test_synonyms_only_when_explicitly_stored(self):
        self.assertFalse(fill_blank_is_correct("CPU", ["central processing unit"]))
        self.assertTrue(fill_blank_is_correct("CPU", ["central processing unit", "CPU"]))

    def test_inner_punctuation_is_kept(self):
        self.assertTrue(fill_blank_is_correct("C++", ["c++"]))
        self.assertFalse(fill_blank_is_correct("C", ["C++"]))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(6, 2))

    def validate(self, raw, accepted=None):
        return validate_fill_blank_candidate(raw, self.units, accepted or [], "easy", 1, SCOPE, 1)

    def assertRejected(self, raw, code, accepted=None):
        with self.assertRaises(CandidateRejected) as caught:
            self.validate(raw, accepted)
        self.assertEqual(caught.exception.code, code)

    def test_a_grounded_candidate_is_normalized(self):
        question, warnings = self.validate(fill_candidate(3))
        self.assertEqual(question["question_type"], "fill_blank")
        self.assertEqual(question["question"], f"The {FILL_BLANK_MARKER} connects endpoints during processing.")
        self.assertEqual((question["correct_answer"], question["correct_answers"], question["options"]), ("socket", ["socket"], []))
        self.assertTrue(question["source_chunk_ids"])
        self.assertEqual(warnings, [])

    def test_blank_marker_must_appear_exactly_once(self):
        self.assertRejected(fill_candidate(3, sentence=fact_sentence(3)), "fill_blank_marker")
        self.assertRejected(fill_candidate(3, sentence="The ____ connects ____ during processing."), "fill_blank_marker")
        question, _ = self.validate(fill_candidate(3, sentence="The ________ connects endpoints during processing."))
        self.assertEqual(question["question"].count(FILL_BLANK_MARKER), 1)   # longer runs normalize to one marker

    def test_answer_must_be_short_and_objective(self):
        self.assertRejected(fill_candidate(3, answer="socket that connects the two endpoints"), "fill_blank_answer")
        self.assertRejected(fill_candidate(3, answer="..."), "fill_blank_answer")

    def test_sentence_must_not_give_the_answer_away(self):
        self.assertRejected(fill_candidate(3, sentence=f"The socket {FILL_BLANK_MARKER} endpoints during processing.", answer="socket"),
                            "fill_blank_giveaway")

    def test_grounding_in_the_original_document(self):
        self.assertRejected(fill_candidate(3, evidence_quote="The socket is invented by an outside textbook entirely."), "quote_not_found")
        self.assertRejected(fill_candidate(3, answer="router"), "answer_not_in_context")
        # the answer is in the quote, but the completed sentence is not what the document says
        self.assertRejected(fill_candidate(3, sentence=f"The {FILL_BLANK_MARKER} deletes every file on the disk forever."),
                            "question_not_supported")

    def test_alternatives_are_kept_only_when_written_in_the_material(self):
        question, _ = self.validate(fill_candidate(3, accepted_answers=["Socket", "router", "made-up synonym"]))
        # "Socket" is the same answer, "router" is in the material, "made-up synonym" is not
        self.assertEqual(question["correct_answers"], ["socket", "router"])

    def test_duplicate_of_an_accepted_question_is_rejected(self):
        first, _ = self.validate(fill_candidate(3))
        self.assertRejected(fill_candidate(3), "duplicate_stem", accepted=[first])
        mcq, _ = validate_candidate(raw_candidate(3), self.units, [], "easy", 1, SCOPE, 1)
        self.validate(fill_candidate(3), accepted=[mcq])   # a different fact of the same sentence is fine


class FlashcardHintTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(12, 2))

    def test_compact_terms_come_from_flashcards_and_questions_are_skipped(self):
        cards = [{"front": "What does the encoder do?", "back": "It encodes symbols during processing of every frame."},
                 {"front": "Encoder", "back": "encodes symbols"}, {"front": "The hypervisor.", "back": "Isolates machines"}]
        self.assertEqual(flashcard_hint_terms(cards), ["Encoder", "encodes symbols", "hypervisor", "Isolates machines"])

    def test_ungrounded_flashcard_terms_are_ignored(self):
        grounded = ground_fill_blank_hints(["encoder", "quantum teleporter", "hypervisor"], self.units)
        self.assertEqual([hint["term"] for hint in grounded], ["encoder", "hypervisor"])
        self.assertTrue(all(hint["unit_ids"] for hint in grounded))

    def test_flashcard_text_alone_cannot_authorize_a_question(self):
        # the flashcard's own wording is not in the document: a candidate quoting it is rejected
        flashcard_back = "The quantum teleporter moves qubits between distant laboratories."
        with self.assertRaises(CandidateRejected) as caught:
            validate_fill_blank_candidate({"evidence_quote": flashcard_back, "answer": "teleporter",
                                           "sentence": "The quantum ____ moves qubits between distant laboratories.",
                                           "explanation": "From the flashcard."}, self.units, [], "easy", 1, SCOPE, 1)
        self.assertEqual(caught.exception.code, "quote_not_found")


class EngineTests(unittest.TestCase):
    def run_engine(self, payloads, hints=("encoder", "hypervisor"), fill_blank_count=2):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=12, fill_blank_count=fill_blank_count, fill_blank_hints=list(hints),
            )

    @staticmethod
    def fill_prompts():
        return [prompt for prompt in FakeModel.prompts if "fill-in-the-blank" in prompt]

    def test_flashcard_covered_concept_is_preferred_when_grounded(self):
        # three valid candidates for two slots: the two flashcard-covered terms win over "watchdog"
        quiz = self.run_engine([{"questions": candidates(range(15))},
                                {"questions": [fill_candidate(22), fill_candidate(20), fill_candidate(21)]}])
        fills = [q for q in quiz["questions"] if q["question_type"] == "fill_blank"]
        self.assertEqual({q["correct_answer"] for q in fills}, {"encoder", "hypervisor"})
        info = quiz["assessment_plan"]["fill_blank"]
        self.assertEqual((info["hint_terms"], info["accepted"], info["hinted_accepted"], info["calls"]), (2, 3, 2, 1))
        self.assertIn("Preferred terms to blank out", self.fill_prompts()[0])
        self.assertIn("encoder; hypervisor", self.fill_prompts()[0])

    def test_total_quiz_count_is_unchanged(self):
        quiz = self.run_engine([{"questions": candidates(range(15))},
                                {"questions": [fill_candidate(20), fill_candidate(21)]}])
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10, "fill_blank": 2})
        self.assertEqual(quiz["assessment_plan"]["status"], "complete")
        self.assertEqual([q["id"] for q in quiz["questions"]], list(range(1, 13)))
        self.assertTrue(all("_meta" not in q for q in quiz["questions"]))

    def test_no_flashcards_means_no_fill_blank_call_and_a_normal_quiz(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], hints=())
        self.assertEqual(len(FakeModel.prompts), 1)
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_only_ungrounded_flashcards_make_no_fill_blank_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], hints=("quantum teleporter",))
        self.assertEqual((len(self.fill_prompts()), len(quiz["questions"])), (0, 12))

    def test_valid_empty_response_is_not_retried(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, {"questions": []}, "never used"])
        self.assertEqual(len(self.fill_prompts()), 1)
        info = quiz["assessment_plan"]["fill_blank"]
        self.assertEqual((info["calls"], info["stop"]), (1, "empty result"))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_malformed_response_is_retried_once(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, "not json at all {",
                                {"questions": [fill_candidate(20), fill_candidate(21)]}])
        info = quiz["assessment_plan"]["fill_blank"]
        self.assertEqual((info["calls"], info["accepted"], len(info["errors"])), (2, 2, 1))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10, "fill_blank": 2})

    def test_all_invalid_candidates_are_retried_once(self):
        bad = fill_candidate(20, sentence=fact_sentence(20))   # no blank
        quiz = self.run_engine([{"questions": candidates(range(15))}, {"questions": [bad]},
                                {"questions": [fill_candidate(21)]}])
        info = quiz["assessment_plan"]["fill_blank"]
        self.assertEqual((info["calls"], info["rejected_by"]), (2, {"fill_blank_marker": 1}))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "fill_blank": 1})

    def test_at_most_one_retry_then_mcq_only_with_the_requested_count(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, "broken", "broken", "never used"])
        self.assertEqual(len(self.fill_prompts()), quiz_units.QUIZ_FILL_BLANK_MAX_CALLS)
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_a_partial_valid_result_is_not_retried(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, {"questions": [fill_candidate(20)]}, "never used"])
        self.assertEqual(len(self.fill_prompts()), 1)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "fill_blank": 1})

    def test_disabled_step_makes_no_fill_blank_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], fill_blank_count=0)
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        self.assertEqual(len(FakeModel.prompts), 1)


class LiveEntryPointTests(unittest.TestCase):
    """generate_quiz reads the document's CURRENT persisted flashcards as hints (never generates them)."""

    def generate(self, cards):
        document = {**DOCUMENT, "topics": []}
        FakeModel.reset([{"questions": candidates(range(15))}, {"questions": [fill_candidate(20), fill_candidate(21)]}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: document}),
            patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"),
            patch.object(quiz_service, "get_quiz", return_value=None),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
            patch.object(quiz_service, "get_latest_flashcard_set_info", return_value={"set_id": "set-1"} if cards else None),
            patch.object(quiz_service, "list_flashcards", return_value=cards) as listing,
        ):
            quiz = quiz_service.generate_quiz(DOCUMENT["id"], "easy", "document", question_count=12, owner_id="owner")
        return quiz, listing

    def test_persisted_flashcards_become_hints(self):
        quiz, listing = self.generate([{"front": "Encoder", "back": "encodes symbols"}, {"front": "Hypervisor", "back": "x"}])
        listing.assert_called_once_with("owner", DOCUMENT["id"], "set-1")
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10, "fill_blank": 2})
        self.assertEqual(len(quiz["questions"]), 12)

    def test_no_flashcards_keeps_the_normal_single_call_quiz(self):
        quiz, _ = self.generate([])
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})
        self.assertEqual(len(quiz["questions"]), 12)


def mixed_quiz(quiz_id: str) -> dict:
    base = {"topic_id": "document", "difficulty": "easy", "explanation": "Supported.", "source_chunk_ids": ["h1"],
            "validation_outcome": "accepted"}
    return {
        "quiz_id": quiz_id, "document_id": "lecture.pdf", "title": "Mixed", "difficulty": "easy", "topic_id": "document",
        "topic_name": "Entire document",
        "questions": [
            {**base, "id": 1, "question": "Question one?", "options": ["A. One", "B. Two", "C. Three", "D. Four"],
             "correct_answer": "A", "question_type": "single_choice", "correct_answers": ["A"]},
            {**base, "id": 2, "question": f"The {FILL_BLANK_MARKER} orders processes.", "options": [],
             "correct_answer": "Scheduler", "question_type": "fill_blank", "correct_answers": ["Scheduler", "task scheduler"]},
            {**base, "id": 3, "question": f"A {FILL_BLANK_MARKER} guards counters.", "options": [],
             "correct_answer": "semaphore", "question_type": "fill_blank", "correct_answers": ["semaphore"]},
        ],
    }


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = "owner-1"
        quiz_store.save_quiz("lecture.pdf", "easy", mixed_quiz("quiz-f"), self.owner)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def save(self, answers, index=0):
        return quiz_service.update_quiz_progress("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-f",
                                                 answers=answers, current_question_index=index)

    def test_stored_fill_blank_keeps_its_text_answer_and_accepted_answers(self):
        question = quiz_store.get_quiz_by_id("quiz-f", self.owner)["questions"][1]
        self.assertEqual((question["question_type"], question["options"]), ("fill_blank", []))
        self.assertEqual(question["correct_answer"], "Scheduler")             # never upper-cased like a letter
        self.assertEqual(question["correct_answers"], ["Scheduler", "task scheduler"])

    def test_autosave_and_resume_restore_text_answers_and_position(self):
        self.save({"1": "A", "2": "  task   scheduler ", "3": "   "}, index=1)
        attempt = quiz_store.get_latest_attempt("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-f")
        self.assertEqual(attempt["answers"], {"1": "A", "2": "task scheduler"})   # blank text = unanswered
        self.assertEqual((attempt["answered"], attempt["current_question_index"]), (2, 1))

    def test_non_text_fill_blank_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            self.save({"2": ["A", "B"]})

    def test_submit_grades_deterministically_and_keeps_the_learner_text(self):
        attempt = quiz_service.submit_quiz_attempt("lecture.pdf", "easy", "document",
                                                   {"1": "A", "2": "scheduler.", "3": "semafore"}, self.owner, quiz_id="quiz-f")
        results = {result["question_id"]: result for result in attempt["question_results"]}
        self.assertEqual((attempt["score"], attempt["total"]), (2, 3))
        self.assertTrue(results[2]["is_correct"])
        self.assertEqual(results[2]["selected_answer"], "scheduler.")
        self.assertEqual(results[2]["correct_answers"], ["Scheduler", "task scheduler"])
        self.assertFalse(results[3]["is_correct"])                              # no fuzzy match
        self.assertEqual((results[3]["selected_answer"], results[3]["correct_answer"]), ("semafore", "semaphore"))
        reloaded = quiz_store.get_quiz_history_attempt(attempt["attempt_id"], self.owner)
        stored = {result["question_id"]: result for result in reloaded["question_results"]}
        self.assertEqual((stored[3]["selected_answer"], stored[3]["question_type"]), ("semafore", "fill_blank"))

    def test_unanswered_fill_blank_can_be_submitted_anyway(self):
        attempt = quiz_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": "A", "2": "", "3": "semaphore"},
                                                   self.owner, quiz_id="quiz-f", allow_unanswered=True)
        results = {result["question_id"]: result for result in attempt["question_results"]}
        self.assertEqual((results[2]["is_correct"], results[2]["selected_answers"]), (False, []))
        with self.assertRaises(ValueError):
            quiz_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": "A", "2": " ", "3": "x"},
                                             self.owner, quiz_id="quiz-f")

    def test_retake_snapshot_keeps_fill_blank_questions(self):
        attempt = quiz_service.submit_quiz_attempt("lecture.pdf", "easy", "document",
                                                   {"1": "B", "2": "Scheduler", "3": "semaphore"}, self.owner, quiz_id="quiz-f")
        snapshot = quiz_service._quiz_from_attempt_snapshot(quiz_store.get_quiz_history_attempt(attempt["attempt_id"], self.owner))
        self.assertEqual([question["question_type"] for question in snapshot["questions"]], ["single_choice", "fill_blank", "fill_blank"])


if __name__ == "__main__":
    unittest.main()
