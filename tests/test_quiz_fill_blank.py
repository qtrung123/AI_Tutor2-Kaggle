"""Fill-in-the-blank questions of the Normal Quiz, derived DETERMINISTICALLY from the document's
persisted flashcards (no LLM call): the cloze algorithm and its rejection rules, the persisted
question shape, selection inside the live pipeline with single_choice fill-in, deterministic grading,
and persistence/resume/submit through the real SQLite store. Only exact (normalized) matches against
the answer stored with the question are correct -- no fuzzy matching and no invented synonyms.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, flashcard, make_chunks, multi_payload, raw_candidate

from backend import quiz_attempt_service, quiz_service, quiz_store
from backend.api.quiz_generation import QuizQuestion
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import (
    FILL_BLANK_MARKER, CandidateRejected, build_flashcard_fill_blank, build_study_units, fill_blank_is_correct,
    fill_blank_target, flashcard_cloze, flashcard_hint_terms, normalize_fill_blank_answer, select_flashcard_fill_blanks,
    validate_candidate,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
SCOPE = {"topic_id": "document", "name": "Entire document"}
# Facts 15, 20 and 21 are tested by none of the single-choice (candidates(range(15))) or
# multiple_select (multi_payload()) fixtures, so their clozes are not duplicates.
FREE_FACTS = (20, 21, 15)


def card(front: str, back: str, **extra) -> dict:
    return {"flashcard_id": "fc", "front": front, "back": back, **extra}


class TargetAndApiTests(unittest.TestCase):
    def test_target_is_a_small_share_of_the_quiz(self):
        self.assertEqual({n: fill_blank_target(n) for n in (12, 15, 18, 20)}, {12: 2, 15: 2, 18: 3, 20: 3})

    def test_api_model_accepts_fill_blank_questions(self):
        question = QuizQuestion(id=1, question=f"The {FILL_BLANK_MARKER} runs tasks.", options=[], correct_answer="thread",
                                question_type="fill_blank", correct_answers=["thread"], topic_id="document",
                                difficulty="easy", explanation="e", source_chunk_ids=["h1"])
        self.assertEqual(question.question_type, "fill_blank")

    def test_benchmark_flashcard_terms_are_unchanged(self):
        cards = [{"front": "What does the encoder do?", "back": "It encodes symbols during processing of every frame."},
                 {"front": "Encoder", "back": "encodes symbols"}, {"front": "The hypervisor.", "back": "Isolates machines"}]
        self.assertEqual(flashcard_hint_terms(cards), ["Encoder", "encodes symbols", "hypervisor", "Isolates machines"])


class ClozeTests(unittest.TestCase):
    def assertCloze(self, front, back, question, answer):
        cloze = flashcard_cloze(card(front, back))
        self.assertEqual((cloze["question"], cloze["answer"]), (question, answer))
        self.assertEqual(cloze["sentence"], back)

    def assertRejected(self, front, back, code):
        with self.assertRaises(CandidateRejected) as caught:
            flashcard_cloze(card(front, back))
        self.assertEqual(caught.exception.code, code)

    def test_question_card_blanks_the_one_span_its_answer_adds(self):
        self.assertCloze("What does Round Robin scheduling use?", "Round Robin scheduling uses a fixed time quantum.",
                         f"Round Robin scheduling uses a {FILL_BLANK_MARKER}.", "fixed time quantum")
        self.assertCloze("What does virtual memory use disk space as?", "Virtual memory uses disk space as an extension of RAM.",
                         f"Virtual memory uses disk space as an {FILL_BLANK_MARKER}.", "extension of RAM")
        self.assertCloze("What is the size of a page in this system?", "In this system the size of a page is 4096 bytes.",
                         f"In this system the size of a page is {FILL_BLANK_MARKER}.", "4096 bytes")

    def test_term_card_blanks_the_term_in_its_definition(self):
        self.assertCloze("Virtual memory", "Virtual memory uses disk space as an extension of RAM.",
                         f"{FILL_BLANK_MARKER} uses disk space as an extension of RAM.", "Virtual memory")
        self.assertCloze("The semaphore", "A semaphore guards shared counters between threads.",
                         f"A {FILL_BLANK_MARKER} guards shared counters between threads.", "semaphore")

    def test_back_that_is_not_a_standalone_sentence_is_rejected(self):
        self.assertRejected("What does RR scheduling use?", "A fixed time quantum.", "not_a_sentence")
        self.assertRejected("Quantum", "Is the quantum fixed for every process in RR?", "not_a_sentence")
        self.assertRejected("What does RR use?", "Each process receives a fixed slice of time.", "not_standalone")

    def test_no_safe_single_span_is_rejected(self):
        self.assertRejected("Which component orders processes during processing in the kernel?",
                            "The component orders processes during processing in the kernel.", "no_answer_span")
        self.assertRejected("What does the scheduler do during processing?",
                            "The scheduler quickly orders processes during processing and memory pages.", "ambiguous_span")
        self.assertRejected("Deadlock", "A situation where processes wait forever for each other.", "term_not_once")
        self.assertRejected("Cache", "The cache keeps copies so that the cache answers quickly.", "term_not_once")
        self.assertRejected("This front is a long statement and not a term", "This front is a long statement here.", "front_shape")

    def test_broad_trivial_or_meaningless_blanks_are_rejected(self):
        self.assertRejected("What does the scheduler do during processing?",
                            "The scheduler orders runnable user kernel batch processes during processing.", "answer_too_long")
        self.assertRejected("into", "The scheduler moves processes into memory during processing.", "trivial_answer")
        self.assertRejected("Paging", "Paging is it, as it is, so it is.", "too_little_context")

    def test_list_items_are_rejected_because_other_items_would_fit(self):
        self.assertRejected("SRTF", "Preemptive algorithms include Round Robin, SRTF and priority scheduling.", "list_ambiguity")
        self.assertRejected("Which algorithms are preemptive?",
                            "Preemptive algorithms include Round Robin, SRTF and priority scheduling.", "ambiguous_span")


class BuildQuestionTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(12, 2))

    def build(self, source, accepted=None, scope=SCOPE):
        return build_flashcard_fill_blank(source, self.units, accepted or [], "medium", scope, 1)

    def test_persisted_shape_keeps_the_flashcard_answer_and_linkage(self):
        question = self.build(flashcard(20))
        self.assertEqual(question["question"], f"The {FILL_BLANK_MARKER} encodes symbols during processing.")
        self.assertEqual((question["question_type"], question["options"]), ("fill_blank", []))
        self.assertEqual((question["correct_answer"], question["correct_answers"]), ("encoder", ["encoder"]))   # no synonyms
        self.assertEqual(question["source_chunk_ids"], ["chunk_11"])
        self.assertEqual(question["concept_id"], next(u["unit_id"] for u in self.units if "chunk_11" in u["source_chunk_ids"]))
        self.assertEqual(question["concept_origin"], "flashcard_cloze")   # an assessment question: not 'flashcard' practice
        self.assertEqual((question["topic_id"], question["difficulty"]), ("document", "medium"))
        self.assertEqual(question["explanation"], "From your flashcard: The encoder encodes symbols during processing.")
        self.assertEqual(question["_meta"]["flashcard_id"], "fc-20-term")
        question = self.build(card("What does the hypervisor isolate on a shared server?",
                                   "The hypervisor isolates guest virtual machines on a shared server.",
                                   flashcard_id="fc-q", source_chunk_ids=["chunk_11"]))
        self.assertEqual((question["question"], question["correct_answer"]),
                         (f"The hypervisor isolates {FILL_BLANK_MARKER} on a shared server.", "guest virtual machines"))
        with self.assertRaises(CandidateRejected) as caught:   # "The hypervisor ____ during processing." says too little
            self.build(flashcard(21, "question"))
        self.assertEqual(caught.exception.code, "too_little_context")

    def test_card_outside_the_quiz_material_is_rejected(self):
        with self.assertRaises(CandidateRejected) as caught:
            self.build(flashcard(20, source_chunk_ids=["chunk_from_another_topic"]))
        self.assertEqual(caught.exception.code, "out_of_scope")
        with self.assertRaises(CandidateRejected) as caught:
            self.build(flashcard(20, source_chunk_ids=[]), scope={"topic_id": "t1", "name": "Topic 1"})
        self.assertEqual(caught.exception.code, "out_of_scope")
        self.assertEqual(self.build(flashcard(20, source_chunk_ids=[]))["source_chunk_ids"], [])   # whole document: allowed

    def test_cloze_duplicating_a_question_in_the_quiz_is_rejected(self):
        single, _ = validate_candidate(raw_candidate(13), self.units, [], "easy", 1, SCOPE, 1)
        with self.assertRaises(CandidateRejected) as caught:
            self.build(flashcard(13), accepted=[single])   # the same fact as a single_choice question
        self.assertEqual(caught.exception.code, "duplicate")
        first = self.build(flashcard(20))
        with self.assertRaises(CandidateRejected):
            self.build(flashcard(20, "question"), accepted=[first])

    def test_selection_is_deterministic_in_deck_order(self):
        deck = [card("Deadlock", "A situation where processes wait forever."), flashcard(21), flashcard(20), flashcard(15)]
        first, info = select_flashcard_fill_blanks(deck, self.units, [], "easy", SCOPE, 2, 0)
        second, _ = select_flashcard_fill_blanks(deck, self.units, [], "easy", SCOPE, 2, 0)
        self.assertEqual([q["question"] for q in first], [q["question"] for q in second])
        self.assertEqual([q["correct_answer"] for q in first], ["hypervisor", "encoder"])
        self.assertEqual((info["accepted"], info["rejected"], info["rejected_by"], info["flashcard_ids"]),
                         (2, 1, {"term_not_once": 1}, ["fc-21-term", "fc-20-term"]))


class GradingTests(unittest.TestCase):
    def test_trim_case_whitespace_and_terminal_punctuation_are_normalized(self):
        for typed in ("Scheduler", "  scheduler  ", "SCHEDULER.", "\"scheduler\"", "(scheduler)", "scheduler!"):
            self.assertTrue(fill_blank_is_correct(typed, ["scheduler"]), typed)
        self.assertEqual(normalize_fill_blank_answer("  Fixed   Time quantum; "), "fixed time quantum")
        self.assertTrue(fill_blank_is_correct("fixed  time  QUANTUM.", ["fixed time quantum"]))

    def test_no_fuzzy_matching(self):
        for typed in ("schedulr", "the scheduler", "scheduler process", "", "   "):
            self.assertFalse(fill_blank_is_correct(typed, ["scheduler"]), typed)

    def test_no_automatic_synonyms(self):
        self.assertFalse(fill_blank_is_correct("CPU", ["central processing unit"]))

    def test_inner_punctuation_is_kept(self):
        self.assertTrue(fill_blank_is_correct("C++", ["c++"]))
        self.assertFalse(fill_blank_is_correct("C", ["C++"]))


class EngineTests(unittest.TestCase):
    def run_engine(self, payloads, cards, fill_blank_count=2, multi_select_count=0):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=12, fill_blank_count=fill_blank_count, fill_blank_cards=list(cards),
                multi_select_count=multi_select_count,
            )

    def test_flashcard_clozes_replace_single_choice_without_any_model_call(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], [flashcard(fact) for fact in FREE_FACTS])
        self.assertEqual(len(FakeModel.prompts), 1)                     # the candidate call only
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 1)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10, "fill_blank": 2})
        self.assertEqual((len(quiz["questions"]), quiz["assessment_plan"]["status"]), (12, "complete"))
        self.assertEqual([q["id"] for q in quiz["questions"]], list(range(1, 13)))
        self.assertTrue(all("_meta" not in q for q in quiz["questions"]))
        info = quiz["assessment_plan"]["fill_blank"]
        self.assertEqual((info["source"], info["target"], info["accepted"], info["flashcard_set_id"]), ("flashcards", 2, 2, "set-1"))
        self.assertEqual(info["flashcard_ids"], ["fc-20-term", "fc-21-term"])
        linked = {record["flashcard_id"] for record in quiz["assessment_plan"]["question_evidence"] if "flashcard_id" in record}
        self.assertEqual(linked, {"fc-20-term", "fc-21-term"})

    def test_ineligible_flashcards_fall_back_to_single_choice(self):
        deck = [card("Deadlock", "A situation where processes wait forever."), flashcard(13), flashcard(20)]
        quiz = self.run_engine([{"questions": candidates(range(15))}], deck)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 11, "fill_blank": 1})
        self.assertEqual(quiz["assessment_plan"]["fill_blank"]["rejected_by"], {"term_not_once": 1, "duplicate": 1})
        self.assertEqual(len(quiz["questions"]), 12)

    def test_no_flashcards_means_an_unchanged_quiz(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}], [])
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})

    def test_full_normal_quiz_mix(self):
        quiz = self.run_engine([{"questions": candidates(range(15))}, multi_payload()],
                               [flashcard(fact) for fact in FREE_FACTS], multi_select_count=3)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 7, "multi_select": 3, "fill_blank": 2})
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 2)       # candidates + multiple_select, none for fill_blank


class LiveEntryPointTests(unittest.TestCase):
    """generate_quiz reads the document's CURRENT persisted flashcards (never generates them)."""

    def generate(self, cards):
        document = {**DOCUMENT, "topics": []}
        FakeModel.reset([{"questions": candidates(range(15))}, {"questions": []}])   # candidates, multiple_select (empty)
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

    def test_persisted_flashcards_become_fill_blank_questions(self):
        cards = [card("Empty", "", flashcard_id="fc-empty"), flashcard(20), flashcard(21)]
        quiz, listing = self.generate(cards)
        listing.assert_called_once_with("owner", DOCUMENT["id"], "set-1")
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 10, "fill_blank": 2})
        self.assertEqual(quiz["assessment_plan"]["fill_blank"]["available_flashcards"], 2)   # the empty card is not usable
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 2)
        self.assertEqual(len(quiz["questions"]), 12)

    def test_no_flashcards_makes_no_fill_blank_questions(self):
        quiz, _ = self.generate([])
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 2)   # candidates + multiple_select
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})


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
        return quiz_attempt_service.update_quiz_progress("lecture.pdf", "easy", "document", self.owner, quiz_id="quiz-f",
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
        attempt = quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document",
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
        attempt = quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": "A", "2": "", "3": "semaphore"},
                                                   self.owner, quiz_id="quiz-f", allow_unanswered=True)
        results = {result["question_id"]: result for result in attempt["question_results"]}
        self.assertEqual((results[2]["is_correct"], results[2]["selected_answers"]), (False, []))
        with self.assertRaises(ValueError):
            quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document", {"1": "A", "2": " ", "3": "x"},
                                             self.owner, quiz_id="quiz-f")

    def test_retake_snapshot_keeps_fill_blank_questions(self):
        attempt = quiz_attempt_service.submit_quiz_attempt("lecture.pdf", "easy", "document",
                                                   {"1": "B", "2": "Scheduler", "3": "semaphore"}, self.owner, quiz_id="quiz-f")
        snapshot = quiz_attempt_service._quiz_from_attempt_snapshot(quiz_store.get_quiz_history_attempt(attempt["attempt_id"], self.owner))
        self.assertEqual([question["question_type"] for question in snapshot["questions"]], ["single_choice", "fill_blank", "fill_blank"])


if __name__ == "__main__":
    unittest.main()
