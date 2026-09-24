"""Fill-in-the-blank questions (safe, objective subset): schema/prompt, deterministic validation
against the original document excerpts, deterministic grading, the bounded generation step with a
retry on malformed output, and persistence/resume/submit through the real SQLite store. Free-text
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
    fill_blank_is_correct, fill_blank_target, normalize_fill_blank_answer, validate_candidate,
    validate_fill_blank_candidate,
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


class EngineTests(unittest.TestCase):
    def run_engine(self, payloads, fill_blank_count=2):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=12, fill_blank_count=fill_blank_count,
            )

    def test_malformed_fill_blank_output_is_retried_once(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, "not json at all {",
                                {"questions": [fill_candidate(20), fill_candidate(21)]}])
        plan = quiz["assessment_plan"]
        types = [question["question_type"] for question in quiz["questions"]]
        self.assertEqual(len(types), 12)
        self.assertEqual(types.count("fill_blank"), 2)
        self.assertEqual(plan["type_distribution"], {"single_choice": 10, "fill_blank": 2})
        self.assertEqual((plan["fill_blank"]["calls"], plan["fill_blank"]["accepted"]), (2, 2))
        self.assertEqual(len(plan["fill_blank"]["errors"]), 1)
        self.assertEqual([question["id"] for question in quiz["questions"]], list(range(1, 13)))
        self.assertTrue(all("_meta" not in question for question in quiz["questions"]))
        self.assertIn("fill-in-the-blank", FakeModel.prompts[1])
        fills = [question for question in quiz["questions"] if question["question_type"] == "fill_blank"]
        self.assertEqual({question["correct_answer"] for question in fills}, {"encoder", "hypervisor"})

    def test_invalid_candidates_trigger_the_retry_too(self):
        bad = fill_candidate(20, sentence=fact_sentence(20))   # no blank
        quiz = self.run_engine([{"questions": candidates(range(15))}, {"questions": [bad]},
                                {"questions": [fill_candidate(21)]}])
        self.assertEqual(quiz["assessment_plan"]["fill_blank"]["rejected_by"], {"fill_blank_marker": 1})
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "fill_blank": 1})

    def test_no_valid_fill_blank_keeps_a_complete_multiple_choice_quiz(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, "broken", "still broken"])
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})
        self.assertEqual(quiz["assessment_plan"]["status"], "complete")

    def test_at_most_two_fill_blank_calls(self):
        self.run_engine([{"questions": candidates(range(15))}, "broken", "broken", "never used"])
        self.assertEqual(len([p for p in FakeModel.prompts if "fill-in-the-blank" in p]), quiz_units.QUIZ_FILL_BLANK_MAX_CALLS)

    def test_disabled_step_makes_no_fill_blank_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], fill_blank_count=0)
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        self.assertEqual(len(FakeModel.prompts), 1)


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
