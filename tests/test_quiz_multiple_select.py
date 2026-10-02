"""multiple_select ("select all that apply") as a Normal Quiz question type: the dedicated validator
(grounding of every correct option in its own evidence quote, structure, negative questions,
duplicates), exact-set grading without partial credit, persistence/autosave/resume of an unordered
selection through the real SQLite store, backward compatibility of single_choice quizzes, and the
backend-decided generation mix with its retry and single_choice fill-in. No real model is called.

The persisted type is the existing "multi_select" token (the one the store, grading and the Quiz
Player already understood); options stay A-D and correct_answers are the sorted correct letters.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quiz_fixtures import (
    MULTI_FACT_PAIRS, FakeModel, candidates, fact_sentence, flashcard, make_chunks, multi_candidate, multi_payload,
)

from backend import quiz_attempt_service, quiz_service, quiz_store
from backend.api.quiz_generation import QuizQuestion
from backend.quiz_attempt_service import option_answers_correct
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import (
    QUIZ_MIN_CALL_S, QUIZ_MULTI_SELECT_MAX_CALLS, QUIZ_MULTI_SELECT_OUTPUT_SCHEMA, QUIZ_MULTI_SELECT_RESERVE_S,
    QUIZ_TOTAL_DEADLINE_S, CandidateRejected, build_generation_prompt, build_multiple_select_prompt,
    build_study_units, fill_blank_target, multiple_select_target, validate_candidate,
    validate_multiple_select_candidate,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
SCOPE = {"topic_id": "document", "name": "Entire document"}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(12, 2))

    def validate(self, raw, accepted=None):
        return validate_multiple_select_candidate(raw, self.units, accepted or [], "easy", 1, SCOPE, 1)

    def assertRejected(self, raw, code, accepted=None):
        with self.assertRaises(CandidateRejected) as caught:
            self.validate(raw, accepted)
        self.assertEqual(caught.exception.code, code)

    def test_valid_candidate_is_accepted_in_the_persisted_shape(self):
        question, warnings = self.validate(multi_candidate(16, 17))
        self.assertEqual(question["question_type"], "multi_select")
        self.assertEqual(question["options"], [
            "A. The gateway bridges networks", "B. The gateway tracks frames",
            "C. The handshake confirms links", "D. The handshake forwards packets",
        ])
        self.assertEqual((question["correct_answers"], question["correct_answer"]), (["A", "C"], "A"))
        self.assertEqual(question["source_chunk_ids"], ["chunk_9"])
        self.assertEqual(warnings, [])
        # the API response model accepts it unchanged
        public = {key: value for key, value in question.items() if key != "_meta"}
        self.assertEqual(QuizQuestion(**public).question_type, "multi_select")

    def test_correct_answers_may_carry_option_labels_and_any_order(self):
        raw = multi_candidate(16, 17)
        raw["correct_answers"] = ["C. The handshake confirms links", "A) The gateway bridges networks"]
        raw["evidence_quotes"] = [fact_sentence(17), fact_sentence(16)]
        question, _ = self.validate(raw)
        self.assertEqual(question["correct_answers"], ["A", "C"])

    def test_one_correct_answer_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["correct_answers"], raw["evidence_quotes"] = raw["correct_answers"][:1], raw["evidence_quotes"][:1]
        self.assertRejected(raw, "correct_count")

    def test_all_options_correct_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["options"] = ["The gateway bridges networks", "The handshake confirms links",
                          "The daemon serves requests", "The register holds values"]
        raw["correct_answers"] = list(raw["options"])
        raw["evidence_quotes"] = [fact_sentence(fact) for fact in (16, 17, 18, 19)]
        self.assertRejected(raw, "correct_count")

    def test_correct_answer_not_present_in_options_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["correct_answers"] = ["The gateway bridges networks", "The router forwards packets"]
        self.assertRejected(raw, "correct_not_in_options")

    def test_duplicate_options_are_rejected(self):
        raw = multi_candidate(16, 17)
        raw["options"][3] = "the gateway bridges networks."
        self.assertRejected(raw, "duplicate_options")

    def test_duplicate_correct_answers_are_rejected(self):
        raw = multi_candidate(16, 17)
        raw["correct_answers"] = ["The gateway bridges networks", "A. the gateway bridges networks"]
        self.assertRejected(raw, "duplicate_correct")

    def test_malformed_correct_answers_and_quotes_are_rejected(self):
        for malformed in ("The gateway bridges networks", [0, 2], None, ["The gateway bridges networks", ""]):
            with self.subTest(correct_answers=malformed):
                self.assertRejected(multi_candidate(16, 17, correct_answers=malformed), "correct_answers")
        # one quote for two correct answers
        self.assertRejected(multi_candidate(16, 17, evidence_quotes=[fact_sentence(16)]), "evidence_quotes")
        # five options cannot be stored (letters A-D)
        raw = multi_candidate(16, 17)
        raw["options"].append("The gateway serves requests")
        self.assertRejected(raw, "options")
        self.assertRejected(multi_candidate(16, 17, question_type="single_choice"), "question_type")

    def test_unsupported_correct_statement_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["options"][2] = "The handshake deletes every partition"      # quoted sentence says something else
        raw["correct_answers"][1] = "The handshake deletes every partition"
        self.assertRejected(raw, "answer_not_in_context")
        outside = multi_candidate(16, 17)
        outside["evidence_quotes"][1] = "The handshake is described by an outside textbook in great detail."
        self.assertRejected(outside, "quote_not_found")

    def test_wrong_option_stated_by_the_evidence_is_rejected_as_ambiguous(self):
        raw = multi_candidate(16, 17)
        raw["options"][1] = "gateway bridges networks during processing"
        self.assertRejected(raw, "ambiguous_distractor")

    def test_exact_true_distractor_from_another_unit_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["options"][1] = "The daemon serves requests"   # fact 18: true, stated in another excerpt, not cited
        self.assertRejected(raw, "supported_distractor")

    def test_paraphrased_supported_distractor_is_rejected(self):
        raw = multi_candidate(16, 17)
        raw["options"][3] = "Sources are translated by the compiler"   # fact 5, reordered + inflected
        self.assertRejected(raw, "supported_distractor")

    def test_known_limit_short_word_inflection_is_not_detected(self):
        # _token_supported matches inflections only for words of 7+ letters: "served" vs "serves"
        # is not recognized, so this true paraphrase of fact 18 still passes (documented limitation).
        raw = multi_candidate(16, 17)
        raw["options"][3] = "Requests are served by the daemon"
        question, _ = self.validate(raw)
        self.assertEqual(question["correct_answers"], ["A", "C"])

    def test_unsupported_plausible_distractors_remain_valid(self):
        # each distractor pairs a component with a behaviour stated for a DIFFERENT component elsewhere
        question, _ = self.validate(multi_candidate(16, 17))
        self.assertEqual(question["options"][1], "B. The gateway tracks frames")
        raw = multi_candidate(16, 17)
        raw["options"][1] = "The gateway encrypts stored passwords"   # stated nowhere
        question, _ = self.validate(raw)
        self.assertEqual(question["correct_answers"], ["A", "C"])

    def test_every_fixture_pair_remains_valid(self):
        for first, second in ((8, 9), (16, 17), (18, 19), (20, 21), (22, 23)):
            with self.subTest(pair=(first, second)):
                question, _ = self.validate(multi_candidate(first, second))
                self.assertEqual(question["question_type"], "multi_select")

    def test_negative_not_except_questions_are_rejected(self):
        for stem in ("Which statements about the gateway are NOT correct?",
                     "All of these describe the gateway EXCEPT which ones?",
                     "Which statements about the gateway are incorrect?"):
            with self.subTest(stem=stem):
                self.assertRejected(multi_candidate(16, 17, question=stem), "negative_polarity")

    def test_a_fact_already_tested_on_the_same_evidence_is_a_duplicate(self):
        single, _ = validate_candidate(candidates([16])[0], self.units, [], "easy", 1, SCOPE, 1)
        self.assertRejected(multi_candidate(16, 17), "duplicate_evidence", accepted=[single])
        first, _ = self.validate(multi_candidate(16, 17))
        self.assertRejected(multi_candidate(16, 17), "duplicate_stem", accepted=[first])


class DenseLectureDistractorTests(unittest.TestCase):
    """Real lecture excerpts name most of their subject's terms in one passage. A plausible
    same-subject distractor recombines words from DIFFERENT sentences and must stay valid; one
    whose words are asserted together in one sentence is a true statement and is still refused."""

    TEXTS = [
        "CPU scheduling decides which process in the ready queue gets the CPU. First-Come First-Served (FCFS) "
        "runs processes in arrival order and is non-preemptive. FCFS can cause the convoy effect when a long "
        "process delays short ones.",
        "Shortest Job First (SJF) selects the process with the smallest next CPU burst. SJF gives the minimum "
        "average waiting time for a given set of processes. Its preemptive version is called Shortest Remaining "
        "Time First.",
        "Round Robin (RR) gives each process a fixed time quantum. When the quantum expires the process is "
        "preempted and moved to the tail of the ready queue. If the quantum is very large, Round Robin behaves like FCFS.",
        "Priority scheduling assigns a priority number to each process and runs the highest priority first. A major "
        "problem is starvation, where low priority processes may never run. Aging gradually increases the priority "
        "of processes that wait for a long time.",
    ]

    def setUp(self):
        self.units = build_study_units([
            {"content": text, "metadata": {"chunk_id": f"os_{index}", "document_id": "os.pdf"}}
            for index, text in enumerate(self.TEXTS)
        ])

    def candidate(self, **overrides):
        raw = {
            "evidence_quotes": [
                "FCFS can cause the convoy effect when a long process delays short ones.",
                "First-Come First-Served (FCFS) runs processes in arrival order and is non-preemptive.",
            ],
            "question": "Which statements about First-Come First-Served scheduling are correct?",
            "options": ["FCFS can cause the convoy effect", "FCFS always gives the minimum average waiting time",
                        "FCFS runs processes in arrival order", "FCFS uses a fixed time quantum"],
            "correct_answers": ["FCFS can cause the convoy effect", "FCFS runs processes in arrival order"],
            "explanation": "Both statements are made in the excerpt about FCFS.",
        }
        raw.update(overrides)
        return raw

    def test_same_subject_distractors_from_other_sentences_are_valid(self):
        question, _ = validate_multiple_select_candidate(self.candidate(), self.units, [], "medium", 1, SCOPE, 1)
        self.assertEqual((question["question_type"], question["correct_answers"]), ("multi_select", ["A", "C"]))

    def test_a_true_statement_in_one_sentence_is_still_refused(self):
        for distractor in ("FCFS scheduling is non-preemptive", "Round Robin gives each process a fixed time quantum"):
            with self.subTest(distractor=distractor):
                raw = self.candidate()
                raw["options"][3] = distractor
                with self.assertRaises(CandidateRejected) as caught:
                    validate_multiple_select_candidate(raw, self.units, [], "medium", 1, SCOPE, 1)
                self.assertEqual(caught.exception.code, "supported_distractor")


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


class DistributionTests(unittest.TestCase):
    def test_deterministic_backend_distribution(self):
        self.assertEqual({count: multiple_select_target(count) for count in (4, 8, 12, 15, 18, 20)},
                         {4: 1, 8: 2, 12: 3, 15: 4, 18: 5, 20: 5})
        for count in (4, 8, 12, 15, 20):
            single = count - multiple_select_target(count)
            self.assertEqual((single, multiple_select_target(count)),
                             {4: (3, 1), 8: (6, 2), 12: (9, 3), 15: (11, 4), 20: (15, 5)}[count])

    def test_the_model_never_chooses_the_type(self):
        item = QUIZ_MULTI_SELECT_OUTPUT_SCHEMA["properties"]["questions"]["items"]
        self.assertNotIn("question_type", item["properties"])
        self.assertEqual(item["required"], ["evidence_quotes", "question", "options", "correct_answers", "explanation"])
        units = build_study_units(make_chunks(4, 2))
        prompt = build_multiple_select_prompt("Lecture", "easy", units, 4)
        self.assertIn("Write exactly 4 easy multiple-select", prompt)
        self.assertIn("using ONLY the excerpts below", prompt)
        self.assertIn("2 or 3 of them are correct", prompt)
        self.assertIn("No negative questions", prompt)
        # the single-choice prompt is unchanged: still one correct option, no multiple-select wording
        single_prompt = build_generation_prompt("Lecture", "easy", units, 4)
        self.assertIn("exactly 1 correct option", single_prompt)
        self.assertNotIn("multiple-select", single_prompt)


class EngineTests(unittest.TestCase):
    def run_engine(self, payloads, question_count=12, multi_select_count=None, chunks=None):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=chunks or make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test",
                regenerate=False, question_count=question_count,
                multi_select_count=multiple_select_target(question_count) if multi_select_count is None else multi_select_count,
            )

    @staticmethod
    def multi_prompts():
        return [prompt for prompt in FakeModel.prompts if "multiple-select" in prompt]

    def test_requested_mix_is_produced(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, multi_payload()])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 9, "multi_select": 3})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))
        self.assertEqual([question["id"] for question in quiz["questions"]], list(range(1, 13)))
        self.assertTrue(all("_meta" not in question for question in quiz["questions"]))
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual((info["target"], info["calls"], info["accepted"], info["stop"]), (3, 1, 3, "target reached"))
        self.assertIn("Write exactly 4 easy multiple-select", self.multi_prompts()[0])   # target + 1, decided by the backend
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 2)
        evidence = {record["question_id"]: record for record in quiz["assessment_plan"]["question_evidence"]}
        for question in quiz["questions"]:
            if question["question_type"] == "multi_select":
                self.assertEqual(len(question["correct_answers"]), 2)
                self.assertIn(" | ", evidence[question["id"]]["evidence_quote"])   # one quote per correct option

    def test_retry_replaces_invalid_multiple_select_output(self):
        invalid = multi_candidate(16, 17)
        invalid["correct_answers"], invalid["evidence_quotes"] = invalid["correct_answers"][:1], invalid["evidence_quotes"][:1]
        quiz = self.run_engine([{"questions": candidates(range(15))}, {"questions": [invalid]}, multi_payload()])
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual((info["calls"], info["accepted"], info["rejected_by"]), (2, 3, {"correct_count": 1}))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 9, "multi_select": 3})

    def test_malformed_output_is_retried_and_never_converted(self):
        single_shaped = candidates([20])   # single-choice output where multiple_select was asked for
        quiz = self.run_engine([{"questions": candidates(range(15))}, "not json {", {"questions": single_shaped}, "unused"])
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual(len(self.multi_prompts()), QUIZ_MULTI_SELECT_MAX_CALLS)
        self.assertEqual((info["calls"], info["accepted"], len(info["errors"]), info["rejected_by"]),
                         (2, 0, 1, {"correct_answers": 1}))
        self.assertNotIn(single_shaped[0]["question"], [question["question"] for question in quiz["questions"]])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_valid_single_choice_fills_a_multiple_select_shortage(self):
        quiz = self.run_engine([{"questions": candidates(range(15))},
                                {"questions": [multi_candidate(16, 17)]}, {"questions": []}])
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual((info["calls"], info["accepted"], info["stop"]), (2, 1, "empty result"))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "multi_select": 1})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))

    def test_model_failure_is_not_retried_and_the_quiz_stays_complete(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}])   # the multiple_select call fails
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual((info["calls"], info["stop"]), (1, "model call failed"))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_total_is_preserved_with_surplus_multiple_select_when_single_choice_runs_short(self):
        pairs = ((8, 9), *MULTI_FACT_PAIRS)
        quiz = self.run_engine([{"questions": candidates(range(8))}, {"questions": []}, {"questions": []},
                                multi_payload(pairs)])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 8, "multi_select": 4})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))

    def test_disabled_step_makes_no_multiple_select_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], multi_select_count=0)
        self.assertEqual(self.multi_prompts(), [])
        self.assertNotIn("multiple_select", quiz["assessment_plan"])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})


class SlowSingleChoiceBudgetTests(unittest.TestCase):
    """A slow GPU: every single-choice call streams until its own deadline is reached. The
    multiple_select step must still get its reserved budget and keep its 3-question allocation."""

    SINGLE_CHOICE_PAYLOADS = ({"questions": candidates(range(10))}, {"questions": []}, {"questions": []})

    def run_slow_engine(self, fill_blank_cards=(), fill_blank_count=0):
        clock = [0.0]
        real_generate = quiz_service._generate_with_deadline

        def slow_single_choice(llm, prompt, deadline_s):
            if "multiple-select" in prompt:   # fast, answered whenever the step gets to run
                FakeModel.prompts.append(prompt)
                return json.dumps(multi_payload()), {}, False
            text, metadata, _cut = real_generate(llm, prompt, deadline_s)
            clock[0] += deadline_s            # the single-choice call streamed until its deadline
            return text, metadata, True

        FakeModel.reset(self.SINGLE_CHOICE_PAYLOADS)
        with (
            patch.object(quiz_service, "time", SimpleNamespace(perf_counter=lambda: clock[0])),
            patch.object(quiz_service, "_generate_with_deadline", slow_single_choice),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            quiz = _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test",
                regenerate=False, question_count=12, multi_select_count=multiple_select_target(12),
                fill_blank_count=fill_blank_count, fill_blank_cards=list(fill_blank_cards),
            )
        return quiz, clock[0]

    def test_slow_single_choice_cannot_starve_multiple_select(self):
        # 10 valid single-choice on call 1 (300 s), then empty follow-ups until the single-choice share
        # of the budget is gone; without the reserve the follow-ups ran to 570 s and multi_select got 0 calls.
        quiz, elapsed = self.run_slow_engine()
        self.assertLessEqual(elapsed, QUIZ_TOTAL_DEADLINE_S - QUIZ_MULTI_SELECT_RESERVE_S)
        info = quiz["assessment_plan"]["multiple_select"]
        self.assertEqual((info["target"], info["calls"], info["accepted"], info["stop"]), (3, 1, 3, "target reached"))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 9, "multi_select": 3})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))
        self.assertIn("time budget used up", " ".join(quiz["assessment_plan"]["generation_warnings"]))
        multi_kwargs = [kwargs for kwargs, prompt in zip(FakeModel.kwargs, FakeModel.prompts) if "multiple-select" in prompt]
        self.assertGreaterEqual(multi_kwargs[0]["client_kwargs"]["timeout"], QUIZ_MIN_CALL_S)

    def test_slow_single_choice_keeps_multiple_select_with_fill_blank(self):
        cards = [flashcard(fact) for fact in (20, 21)]
        quiz, _elapsed = self.run_slow_engine(fill_blank_cards=cards, fill_blank_count=fill_blank_target(12))
        self.assertEqual(quiz["assessment_plan"]["multiple_select"]["accepted"], 3)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"],
                         {"single_choice": 7, "multi_select": 3, "fill_blank": 2})
        self.assertEqual(len(quiz["questions"]), 12)


class LiveEntryPointTests(unittest.TestCase):
    def test_normal_quiz_asks_for_the_backend_mix_with_create_quiz_unchanged(self):
        document = {**DOCUMENT, "topics": []}
        FakeModel.reset([{"questions": candidates(range(candidate_count(15)))}, multi_payload(((16, 17), (18, 19), (20, 21), (22, 23)))])
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
            quiz = quiz_service.generate_quiz(DOCUMENT["id"], "easy", "document", question_count=15, owner_id="owner")
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "multi_select": 4})
        self.assertEqual(len(quiz["questions"]), 15)


def candidate_count(question_count: int) -> int:
    from backend.quiz_units import candidate_target
    return min(16, candidate_target(question_count))   # facts 0..15: none of them is used by the multiple_select pairs


if __name__ == "__main__":
    unittest.main()
