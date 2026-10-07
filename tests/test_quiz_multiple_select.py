"""Normal Quiz is single-choice only: the live pipeline makes no multiple_select call, reserves no
generation time for one, and a 12-question quiz holds 12 single_choice questions. No real model is
called.

Quizzes saved before this change may still hold "multi_select" questions, so their exact-set
grading, persistence/autosave/resume of an unordered selection through the real SQLite store and
backward compatibility of single_choice quizzes stay covered here.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, make_chunks

from backend import quiz_attempt_service, quiz_service, quiz_store, quiz_units
from backend.quiz_attempt_service import option_answers_correct
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import QUIZ_TOTAL_DEADLINE_S, candidate_target

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


class ScoringTests(unittest.TestCase):
    def test_exact_set_grading(self):
        correct = ["A", "B"]
        self.assertTrue(option_answers_correct(["A", "B"], correct))
        self.assertTrue(option_answers_correct(["B", "A"], correct))      # order never matters
        self.assertFalse(option_answers_correct(["A"], correct))          # missing a correct option
        self.assertFalse(option_answers_correct(["A", "B", "C"], correct))  # an extra wrong option
        self.assertFalse(option_answers_correct([], correct))             # empty selection
        self.assertTrue(option_answers_correct(["C"], ["C"]))             # single_choice unchanged
        self.assertFalse(option_answers_correct(["D"], ["C"]))


def mixed_quiz(quiz_id: str = "quiz-m") -> dict:
    base = {"topic_id": "document", "difficulty": "easy", "explanation": "Supported.", "source_chunk_ids": ["h1"],
            "validation_outcome": "accepted"}
    options = ["A. One", "B. Two", "C. Three", "D. Four"]
    return {
        "quiz_id": quiz_id, "document_id": "lecture.pdf", "title": "Mixed", "difficulty": "easy", "topic_id": "document",
        "topic_name": "Entire document",
        "questions": [
            {**base, "id": 1, "question": "Single?", "options": options, "correct_answer": "A",
             "question_type": "single_choice", "correct_answers": ["A"]},
            {**base, "id": 2, "question": "Which are correct?", "options": options, "correct_answer": "A",
             "question_type": "multi_select", "correct_answers": ["A", "C"]},
            {**base, "id": 3, "question": "Which also apply?", "options": options, "correct_answer": "B",
             "question_type": "multi_select", "correct_answers": ["B", "D"]},
        ],
    }


class PersistenceAndGradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = "owner-1"
        quiz_store.save_quiz("lecture.pdf", "easy", mixed_quiz(), self.owner)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def autosave(self, answers, index=0):
        return quiz_attempt_service.update_quiz_progress("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-m",
                                                         answers=answers, current_question_index=index)

    def submit(self, answers, **kwargs):
        return quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document", answers, self.owner,
                                                        quiz_id="quiz-m", **kwargs)

    def test_multiple_select_question_save_and_load(self):
        question = quiz_store.get_quiz_by_id("quiz-m", self.owner)["questions"][1]
        self.assertEqual(question["question_type"], "multi_select")
        self.assertEqual(question["correct_answers"], ["A", "C"])
        self.assertEqual(question["options"], ["A. One", "B. Two", "C. Three", "D. Four"])

    def test_submitted_selection_is_saved_and_reloaded_as_an_array(self):
        attempt = self.submit({"1": "A", "2": ["C", "A"], "3": ["B"]})
        self.assertEqual(attempt["score"], 2)
        results = {result["question_id"]: result for result in attempt["question_results"]}
        self.assertTrue(results[2]["is_correct"])
        self.assertFalse(results[3]["is_correct"])                  # missing D: no partial credit
        reloaded = quiz_store.get_quiz_history_attempt(attempt["attempt_id"], self.owner)
        self.assertEqual(reloaded["answers"]["2"], ["A", "C"])
        self.assertEqual(reloaded["answers"]["3"], ["B"])
        stored = {result["question_id"]: result for result in reloaded["question_results"]}
        self.assertEqual((stored[2]["selected_answers"], stored[2]["correct_answers"]), (["A", "C"], ["A", "C"]))
        self.assertEqual((stored[3]["question_type"], stored[3]["is_correct"]), ("multi_select", False))

    def test_extra_and_empty_selections_grade_incorrect(self):
        attempt = self.submit({"1": "A", "2": ["A", "B", "C"], "3": []}, allow_unanswered=True)
        results = {result["question_id"]: result for result in attempt["question_results"]}
        self.assertFalse(results[2]["is_correct"])
        self.assertEqual((results[3]["is_correct"], results[3]["selected_answers"]), (False, []))
        self.assertEqual((attempt["score"], attempt["percentage"]), (1, 33.33))   # one point per question

    def test_autosave_preserves_the_array(self):
        self.autosave({"1": "B", "2": ["C", "A"], "3": ["D"]}, index=1)
        attempt = quiz_store.get_latest_attempt("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-m")
        self.assertEqual(attempt["answers"], {"1": "B", "2": ["A", "C"], "3": ["D"]})
        self.assertEqual(attempt["answered"], 3)

    def test_cleared_selection_is_removed_on_the_next_autosave(self):
        self.autosave({"2": ["A", "C"]})
        self.autosave({"2": []})
        attempt = quiz_store.get_latest_attempt("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-m")
        self.assertEqual(attempt["answers"], {})

    def test_resume_restores_multiple_selections_and_position(self):
        self.autosave({"1": "A", "2": ["C", "A"]}, index=1)
        with patch.object(quiz_attempt_service, "_document_lookup", return_value={"lecture.pdf": DOCUMENT}):
            loaded = quiz_attempt_service.load_quiz_with_attempt("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-m")
        latest = loaded["latest_attempt"]
        self.assertFalse(latest["completed"])
        self.assertEqual(latest["answers"], {"1": "A", "2": ["A", "C"]})
        self.assertEqual(latest["current_question_index"], 1)
        # resuming and submitting grades the restored selection
        attempt = self.submit({**latest["answers"], "3": ["B", "D"]})
        self.assertEqual(attempt["score"], 3)

    def test_retake_snapshot_keeps_multiple_select_questions(self):
        attempt = self.submit({"1": "A", "2": ["A", "C"], "3": ["B", "D"]})
        snapshot = quiz_attempt_service._quiz_from_attempt_snapshot(quiz_store.get_quiz_history_attempt(attempt["attempt_id"], self.owner))
        self.assertEqual([question["question_type"] for question in snapshot["questions"]],
                         ["single_choice", "multi_select", "multi_select"])
        self.assertEqual(snapshot["questions"][1]["correct_answers"], ["A", "C"])

    def test_old_single_choice_quizzes_remain_compatible(self):
        # the shape of a quiz saved before question types existed: no question_type / correct_answers
        legacy = mixed_quiz("quiz-old")
        legacy["questions"] = [{key: value for key, value in question.items() if key not in ("question_type", "correct_answers")}
                               for question in legacy["questions"][:1]]
        quiz_store.save_quiz("lecture.pdf", "easy", legacy, self.owner)
        question = quiz_store.get_quiz_by_id("quiz-old", self.owner)["questions"][0]
        self.assertEqual((question["question_type"], question["correct_answers"]), ("single_choice", ["A"]))
        quiz_attempt_service.update_quiz_progress("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-old",
                                                  answers={"1": "B"})
        saved = quiz_store.get_latest_attempt("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-old")
        self.assertEqual(saved["answers"], {"1": "B"})                 # a single answer stays a letter, not a list
        attempt = quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": "A"}, self.owner,
                                                           quiz_id="quiz-old")
        self.assertEqual((attempt["score"], attempt["answers"]), (1, {"1": "A"}))
        with self.assertRaisesRegex(ValueError, "exactly one selected answer"):
            quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": ["A", "B"]}, self.owner,
                                                     quiz_id="quiz-old")


class SingleChoiceOnlyEngineTests(unittest.TestCase):
    def run_engine(self, payloads, question_count=12):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test",
                regenerate=False, question_count=question_count,
            )

    def test_twelve_question_quiz_is_twelve_single_choice_from_one_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(len(FakeModel.prompts), 1)
        self.assertNotIn("multiple_select", quiz["assessment_plan"])
        self.assertTrue(all("multiple-select" not in prompt for prompt in FakeModel.prompts))

    def test_single_choice_shortage_is_partial_not_filled_by_another_type(self):
        quiz = self.run_engine([{"questions": candidates(range(8))}, {"questions": []}, {"questions": []}])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 8})
        self.assertEqual(quiz["assessment_plan"]["status"], "partial")
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 3)   # call 1, then two follow-ups (stall guard)
        self.assertTrue(all("multiple-select" not in prompt for prompt in FakeModel.prompts))

    def test_multiple_select_generation_helpers_are_gone(self):
        for name in ("multiple_select_target", "build_multiple_select_prompt", "validate_multiple_select_candidate",
                     "QUIZ_MULTI_SELECT_RESERVE_S", "QUIZ_MULTI_SELECT_MAX_CALLS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(quiz_units, name))
        self.assertFalse(hasattr(quiz_service, "_generate_multiple_select_pool"))


class NoReservedBudgetTests(unittest.TestCase):
    """A slow GPU: every single-choice call streams until its own deadline. With no multiple_select
    step, nothing is held back -- the single-choice calls may use the whole QUIZ_TOTAL_DEADLINE_S."""

    def test_single_choice_calls_get_the_whole_deadline(self):
        clock = [0.0]
        deadlines = []
        real_generate = quiz_service._generate_with_deadline

        def slow(llm, prompt, deadline_s):
            text, metadata, _cut = real_generate(llm, prompt, deadline_s)
            deadlines.append((clock[0], deadline_s))
            clock[0] += deadline_s
            return text, metadata, True

        FakeModel.reset([{"questions": candidates(range(10))}, {"questions": []}, {"questions": []}])
        with (
            patch.object(quiz_service, "time", SimpleNamespace(perf_counter=lambda: clock[0])),
            patch.object(quiz_service, "_generate_with_deadline", slow),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            quiz = _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test",
                regenerate=False, question_count=12,
            )
        # every call after the first is budgeted against the full total deadline, not a reduced one
        for started, deadline_s in deadlines[1:]:
            self.assertLessEqual(started + deadline_s, QUIZ_TOTAL_DEADLINE_S)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10})
        self.assertNotIn("multiple_select", quiz["assessment_plan"])


class LiveEntryPointTests(unittest.TestCase):
    def test_normal_quiz_is_single_choice_only_with_create_quiz_unchanged(self):
        document = {**DOCUMENT, "topics": []}
        FakeModel.reset([{"questions": candidates(range(min(16, candidate_target(12))))}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: document}),
            patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"),
            patch.object(quiz_service, "get_quiz", return_value=None),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
            patch.object(quiz_service, "_usable_flashcards", return_value=[]),
        ):
            # the same arguments Create Quiz sends today: no question-type field exists
            quiz = quiz_service.generate_quiz(DOCUMENT["id"], "easy", "document", question_count=12, owner_id="owner")
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(len(FakeModel.prompts), 1)


if __name__ == "__main__":
    unittest.main()
