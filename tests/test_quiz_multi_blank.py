"""multi_blank questions (backend/flashcard_quiz_service.build_multi_blank): 2-4 blanks per grounded
sentence, accepted variants, 1-3 word-bank distractors, Mixed replacing short answers, SQLite
storage of the blank spec, autosave/resume, per-blank grading and compatibility with older types.
No LLM is called anywhere in this flow.
"""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import (
    auth_store, conversation_store, flashcard_quiz_service, flashcard_store, indexed_document_store, quiz_store,
    study_planner_store, summary_store,
)
from backend.main import app
from backend.quiz_attempt_service import _multi_blank_accepted
from backend.quiz_units import FILL_BLANK_MARKER, squash
from test_flashcard_written_quiz import CHUNKS, DOC, TOPICS, save_cards

SENTENCE = "A semaphore guards shared counters so that two threads never update them together."
VOCABULARY_TEXTS = [chunk["content"] for chunk in CHUNKS]


def fill(question: dict, answers: list[str]) -> str:
    text = question["question"]
    for answer in answers:
        text = text.replace(FILL_BLANK_MARKER, answer, 1)
    return text


class BuildMultiBlankTests(unittest.TestCase):
    def build(self, card, sentences, texts=VOCABULARY_TEXTS, seed=1):
        vocabulary = flashcard_quiz_service.keyword_vocabulary(texts)
        return flashcard_quiz_service.build_multi_blank(card, sentences, vocabulary, random.Random(seed))

    def test_document_sentence_gets_two_to_four_distinct_grounded_blanks(self):
        card = {"front": "What guards shared counters between threads?", "back": "Semaphore", "source_chunk_ids": ["c2"]}
        built = self.build(card, [{"text": SENTENCE, "chunk_id": "c2"}])
        self.assertIsNotNone(built)
        answers = [blank["answer"] for blank in built["blanks"]]
        self.assertTrue(2 <= len(answers) <= 4, answers)
        self.assertEqual(built["question"].count(FILL_BLANK_MARKER), len(answers))
        self.assertEqual(len({squash(answer) for answer in answers}), len(answers))   # different keywords
        self.assertEqual(fill(built, answers), SENTENCE)                              # the PDF sentence itself
        self.assertEqual(built["source_chunk_ids"], ["c2"])
        self.assertTrue(built["explanation"].startswith("From the document:"))
        self.assertNotIn(f"{FILL_BLANK_MARKER} {FILL_BLANK_MARKER}", built["question"])   # never adjacent

        self.assertTrue(1 <= len(built["distractors"]) <= 3)
        sentence_keys = {squash(word) for word in SENTENCE.split()}
        for distractor in built["distractors"]:
            self.assertNotIn(squash(distractor), sentence_keys)   # a distractor can never be right
        self.assertEqual(sorted(built["word_bank"]), sorted(answers + built["distractors"]))
        self.assertNotEqual(built["word_bank"][:len(answers)], answers)
        self.assertTrue(flashcard_quiz_service.multi_blank_is_valid(built, SENTENCE))

    def test_accepted_alternatives_cover_hyphens_and_missing_diacritics(self):
        self.assertEqual(flashcard_quiz_service._accepted_answers("light-dependent"),
                         ["light-dependent", "light dependent", "lightdependent"])
        self.assertEqual(flashcard_quiz_service._accepted_answers("quang hợp"), ["quang hợp", "quang hop"])
        self.assertEqual(flashcard_quiz_service._accepted_answers("đường"), ["đường", "duong"])
        self.assertEqual(flashcard_quiz_service._accepted_answers("scheduler"), ["scheduler"])

    def test_no_sentence_or_no_distractor_means_no_question(self):
        short = {"front": "What guards counters?", "back": "Semaphore", "source_chunk_ids": []}
        self.assertIsNone(self.build(short, None))   # a term, not a sentence, and no document text
        # Every key word of the only text is in the sentence itself: no distractor exists.
        card = {"front": "Q?", "back": SENTENCE, "source_chunk_ids": []}
        self.assertIsNone(self.build(card, None, texts=[SENTENCE]))

    def test_card_sentence_is_used_without_document_sentences(self):
        card = {"front": "How does a semaphore protect a counter?",
                "back": "It lets only one thread at a time enter the section that updates the shared counter value.",
                "source_chunk_ids": ["c2"]}
        built = self.build(card, None)
        self.assertIsNotNone(built)
        self.assertEqual(fill(built, [blank["answer"] for blank in built["blanks"]]), card["back"])
        self.assertTrue(built["explanation"].startswith("From your flashcard:"))

    def test_snapshot_without_spec_grades_against_its_canonical_answers(self):
        self.assertEqual(_multi_blank_accepted({"correct_answers": ["semaphore", "threads"]}), [["semaphore"], ["threads"]])


class QuizDbTestCase(unittest.TestCase):
    """A signed-up learner with the shared test document and its ten flashcards, on a temp SQLite file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        db = root / "app.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", db),
            patch.object(conversation_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(flashcard_store, "DATABASE_PATH", db),
            patch.object(quiz_store, "DATABASE_PATH", db),
            patch.object(study_planner_store, "DATABASE_PATH", db),
            patch.object(summary_store, "DATABASE_PATH", db),
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", root / "missing_quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", root / "missing_attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", root / "missing_explanations.json"),
            patch.object(flashcard_quiz_service, "get_document_chunks", return_value=CHUNKS),
            patch("backend.flashcard_service.ChatOllama", side_effect=AssertionError("LLM called")),
            patch("backend.rag_service.explain_quiz_answer", side_effect=AssertionError("LLM called")),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.client = TestClient(app)
        response = self.client.post("/api/auth/signup", json={
            "display_name": "Learner", "email": "multi-blank@example.com", "password": "long-password-123"})
        self.assertEqual(response.status_code, 201, response.text)
        self.owner = response.json()["id"]
        indexed_document_store.upsert_indexed_document(self.owner, DOC, {
            "hash": "hash-1", "chunks": 2, "path": DOC, "topic_schema_version": 2, "topics": TOPICS})
        save_cards(self.owner, DOC)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()


class MultiBlankQuizTests(QuizDbTestCase):
    def planner_mixed(self) -> dict:
        return flashcard_quiz_service.create_flashcard_written_quiz(self.owner, DOC, "mixed", 10, rng=random.Random(3))

    def multi_blanks(self, quiz):
        return [q for q in quiz["questions"] if q["question_type"] == "multi_blank"]

    def test_mixed_replaces_short_answers_with_grounded_multi_blank(self):
        quiz = self.planner_mixed()
        questions = self.multi_blanks(quiz)
        self.assertTrue(questions)
        self.assertEqual(len(quiz["questions"]), 10)   # one question per card, as before
        document_text = squash(" ".join(chunk["content"] for chunk in CHUNKS))
        for question in questions:
            self.assertTrue(2 <= len(question["blanks"]) <= 4)
            self.assertEqual(question["correct_answers"], [blank["answer"] for blank in question["blanks"]])
            self.assertEqual(question["answer_modes"], ["type_answer", "word_bank"])
            self.assertEqual(question["options"], [])
            if question["explanation"].startswith("From the document:"):
                self.assertIn(squash(fill(question, question["correct_answers"])), document_text)
                self.assertTrue(set(question["source_chunk_ids"]) <= {"c1", "c2"})
        # Only cards that yield no multi_blank stay short answers.
        self.assertEqual(quiz["assessment_plan"]["type_distribution"].get("multi_blank"), len(questions))
        # Single-type modes are unchanged.
        self.assertNotIn("multi_blank", [q["question_type"] for q in flashcard_quiz_service.create_flashcard_written_quiz(
            self.owner, DOC, "short_answer", 10)["questions"]])

    def test_practice_as_quiz_mixed_uses_only_the_cards(self):
        response = self.client.post(f"/api/flashcards/{DOC}/practice-quiz", json={"mode": "mixed", "question_count": 10})
        self.assertEqual(response.status_code, 200, response.text)
        self.mocks[11].assert_not_called()
        for question in self.multi_blanks(response.json()):
            self.assertTrue(question["explanation"].startswith("From your flashcard:"))

    def test_spec_is_stored_and_older_types_keep_their_shape(self):
        quiz = self.planner_mixed()
        stored = quiz_store.get_quiz_by_id(quiz["quiz_id"], self.owner)
        created = {q["id"]: q for q in quiz["questions"]}
        for question in stored["questions"]:
            if question["question_type"] == "multi_blank":
                for key in ("blanks", "distractors", "word_bank", "answer_modes", "correct_answers", "correct_answer"):
                    self.assertEqual(question[key], created[question["id"]][key], key)
            else:
                self.assertNotIn("blanks", question)
                self.assertNotIn("word_bank", question)

    def progress(self, quiz, answers):
        return self.client.patch(f"/api/quiz/{DOC}/progress", json={
            "difficulty": "easy", "topic_id": "document", "quiz_id": quiz["quiz_id"], "answers": answers})

    def test_autosave_resume_submit_and_review(self):
        quiz = self.planner_mixed()
        first, *_ = self.multi_blanks(quiz)
        qid = str(first["id"])
        blanks = first["correct_answers"]
        partial = [blanks[0]] + [""] * (len(blanks) - 1)
        self.assertEqual(self.progress(quiz, {qid: partial}).status_code, 200)
        detail = self.client.get(f"/api/quiz/{DOC}", params={"topic_id": "document", "difficulty": "easy", "quiz_id": quiz["quiz_id"]}).json()
        self.assertEqual(detail["latest_attempt"]["answers"][qid], partial)   # positional, empty blanks kept
        self.assertEqual(self.progress(quiz, {qid: [blanks[0]]}).status_code, 400)   # one answer per blank

        others = [q for q in quiz["questions"] if q["id"] != first["id"]]
        typed = [f"  {answer.upper()} " for answer in blanks]   # Type Answer: case and spaces are normalized
        submit = self.client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document", "allow_unanswered": True,
            "answers": {qid: typed, **({str(others[0]["id"]): ["wrong"] * len(others[0]["correct_answers"])}
                                        if others[0]["question_type"] == "multi_blank" else {})},
        })
        self.assertEqual(submit.status_code, 200, submit.text)
        results = {str(r["question_id"]): r for r in submit.json()["question_results"]}
        self.assertTrue(results[qid]["is_correct"])
        self.assertEqual(results[qid]["selected_answers"], [answer.strip() for answer in typed])
        self.assertEqual(results[qid]["correct_answers"], blanks)
        self.assertEqual(results[qid]["correct_answer"], " | ".join(blanks))
        self.assertEqual(results[qid]["question_type"], "multi_blank")

        history = self.client.get(f"/api/quiz-history/{submit.json()['attempt_id']}").json()
        stored = {str(r["question_id"]): r for r in history["question_results"]}
        self.assertEqual(stored[qid]["selected_answers"], [answer.strip() for answer in typed])
        self.assertTrue(stored[qid]["is_correct"])
        retake = self.client.get(f"/api/quiz-history/{submit.json()['attempt_id']}/retake")
        self.assertEqual(retake.status_code, 200)
        retaken = next(q for q in retake.json()["quiz"]["questions"] if str(q["id"]) == qid)
        self.assertEqual(retaken["word_bank"], first["word_bank"])

    def test_every_blank_must_be_right_and_word_bank_picks_grade_the_same(self):
        quiz = self.planner_mixed()
        first = self.multi_blanks(quiz)[0]
        blanks = first["correct_answers"]
        wrong_last = blanks[:-1] + [first["distractors"][0]]   # Word Bank: a distractor in the last blank

        def grade(answer):
            response = self.client.post(f"/api/quiz/{DOC}/submit", json={
                "quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document", "allow_unanswered": True,
                "answers": {str(first["id"]): answer}})
            self.assertEqual(response.status_code, 200, response.text)
            return next(r for r in response.json()["question_results"] if r["question_id"] == first["id"])["is_correct"]

        self.assertTrue(grade(list(blanks)))
        self.assertFalse(grade(wrong_last))
        self.assertFalse(grade(list(reversed(blanks))))   # positional
        self.assertFalse(grade(blanks[:1] + [""] * (len(blanks) - 1)))
        accepted = [blank["accepted_answers"][-1] for blank in first["blanks"]]   # an accepted variant per blank
        self.assertTrue(grade(accepted))


class AutoPracticeQuizTests(QuizDbTestCase):
    """Practice as Quiz (mode "auto", every card): multiple choice + multi_blank + matching, no short answers."""

    def auto_quiz(self) -> dict:
        response = self.client.post(f"/api/flashcards/{DOC}/practice-quiz", json={"set_id": None})   # "auto" is the default
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_every_card_becomes_a_balanced_mix_without_short_answers(self):
        quiz = self.auto_quiz()
        types = [q["question_type"] for q in quiz["questions"]]
        self.assertEqual(quiz["assessment_plan"]["mode"], "auto")
        self.assertNotIn("short_answer", types)
        self.assertLessEqual(set(types), {"single_choice", "multi_blank", "matching", "fill_blank"})
        for kind in ("single_choice", "multi_blank", "matching"):
            self.assertIn(kind, types)
        self.assertEqual(quiz["assessment_plan"]["covered_cards"], 10)   # all cards, nothing to choose
        self.assertEqual(quiz["assessment_plan"]["self_check_question_ids"], [])
        cards = {card["flashcard_id"]: card for card in flashcard_store.list_flashcards(self.owner, DOC)}
        for question in quiz["questions"]:
            if question["question_type"] != "single_choice":
                continue
            self.assertEqual(len(question["options"]), 4)
            answer = question["options"]["ABCD".index(question["correct_answer"])][3:]
            self.assertEqual(answer, cards[question["concept_id"]]["back"])          # the card's own answer is correct
            self.assertEqual(question["question"], cards[question["concept_id"]]["front"])
            self.assertEqual(len({option[3:].lower() for option in question["options"]}), 4)

    def test_a_perfect_auto_attempt_scores_full_marks(self):
        quiz = self.auto_quiz()
        answers = {str(q["id"]): (q["correct_answers"] if q["question_type"] in ("matching", "multi_blank") else q["correct_answer"])
                   for q in quiz["questions"]}
        submit = self.client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document", "answers": answers})
        self.assertEqual(submit.status_code, 200, submit.text)
        self.assertEqual(submit.json()["percentage"], 100.0)

    def test_choice_needs_three_distinct_other_answers(self):
        card = {"front": "Q?", "back": "alpha", "topic_id": "t"}
        others = [{"front": f"Q{n}?", "back": back, "topic_id": "t"} for n, back in enumerate(["beta", "Alpha", "gamma"])]
        self.assertIsNone(flashcard_quiz_service.card_choice(card, [card, *others], random.Random(1)))   # "Alpha" is the same answer
        built = flashcard_quiz_service.card_choice(card, [card, *others, {"front": "Q9?", "back": "delta"}], random.Random(1))
        self.assertEqual(sorted(built["options"]), ["alpha", "beta", "delta", "gamma"])
        self.assertEqual(built["options"]["ABCD".index(built["correct_answer"])], "alpha")


class DocumentQuestionMixTests(QuizDbTestCase):
    """Create a Quiz: multi_blank from the quiz's own excerpts, matching only when flashcards exist."""

    def units(self):
        from backend.quiz_units import build_study_units
        return build_study_units(CHUNKS)

    def mix(self, owner):
        return flashcard_quiz_service.build_document_question_mix(
            owner, DOC, self.units(), {"topic_id": "document", "name": "Entire document"}, "medium", 12, random.Random(2),
            language="english")

    def test_mix_adds_grounded_multi_blank_and_flashcard_matching(self):
        extras = self.mix(self.owner)
        types = [q["question_type"] for q in extras]
        self.assertIn("multi_blank", types)
        self.assertEqual(types.count("matching"), 2)   # 12 questions -> 2 activities
        document_text = squash(" ".join(chunk["content"] for chunk in CHUNKS))
        unit_ids = {unit["unit_id"] for unit in self.units()}
        for question in extras:
            self.assertEqual((question["topic_id"], question["difficulty"]), ("document", "medium"))
            if question["question_type"] == "multi_blank":
                self.assertIn(squash(fill(question, question["correct_answers"])), document_text)
                self.assertIn(question["concept_id"], unit_ids)

    def test_without_flashcards_the_quiz_needs_none(self):
        other = auth_store.create_user("No Cards", "no-cards@example.com", "long-password-123")["id"]
        extras = self.mix(other)
        self.assertTrue(extras)
        self.assertEqual({q["question_type"] for q in extras}, {"multi_blank"})

    def test_mix_question_types_spreads_and_renumbers(self):
        from backend.quiz_service import _mix_question_types
        choices = [{"id": n, "concept_id": f"U{n}", "question_type": "single_choice"} for n in range(1, 7)]
        extras = [{"id": 0, "concept_id": f"X{n}", "question_type": "multi_blank"} for n in range(3)]
        ordered, evidence = _mix_question_types(choices, extras, [{"question_id": n} for n in range(1, 7)])
        self.assertEqual([q["id"] for q in ordered], list(range(1, 10)))
        self.assertEqual([q["question_type"] for q in ordered].count("multi_blank"), 3)
        self.assertNotEqual([q["question_type"] for q in ordered][-3:], ["multi_blank"] * 3)   # spread, not appended
        self.assertEqual({q["assessment_capacity"] for q in ordered}, {9})
        by_concept = {q["concept_id"]: q["id"] for q in ordered}
        self.assertEqual([record["question_id"] for record in evidence], [by_concept[f"U{n}"] for n in range(1, 7)])


class GeneratedQuizMixTests(unittest.TestCase):
    def test_generated_quiz_mixes_types_and_the_model_writes_only_the_rest(self):
        from quiz_fixtures import FakeModel, candidates, make_chunks
        from test_quiz_single_choice_language import run_generate

        quiz = run_generate([{"questions": candidates(range(15))}], make_chunks(12, 2) + CHUNKS)
        types = [q["question_type"] for q in quiz["questions"]]
        blanks = types.count("multi_blank")
        self.assertGreater(blanks, 0)
        self.assertEqual(len(types), 12)
        self.assertEqual(types.count("single_choice"), 12 - blanks)
        self.assertNotIn("short_answer", types)
        self.assertEqual([q["id"] for q in quiz["questions"]], list(range(1, 13)))
        plan = quiz["assessment_plan"]
        self.assertEqual((plan["requested_count"], plan["status"]), (12, "complete"))
        self.assertEqual(plan["type_distribution"], {"single_choice": 12 - blanks, "multi_blank": blanks})
        evidence_ids = {record["question_id"] for record in plan["question_evidence"]}
        self.assertEqual(evidence_ids, {q["id"] for q in quiz["questions"] if q["question_type"] == "single_choice"})
        self.assertEqual(len(FakeModel.prompts), 1)


if __name__ == "__main__":
    unittest.main()
