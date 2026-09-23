"""Quiz Player progress persistence: every live progress operation is scoped to an exact quiz_id.

Root cause this covers: backend/quiz_service.update_quiz_progress and clear_quiz_progress used to
resolve "the quiz" via the (document, topic, difficulty) SLOT only (backend/quiz_store.get_quiz's
"newest quiz here" lookup) -- once several quizzes can share that slot (see
tests/test_quiz_persistence.py), autosaving or resetting one quiz's progress could silently read or
clobber a sibling quiz's in-progress attempt. Real SQLite database throughout (nothing in the
persistence layer is patched), matching tests/test_quiz_persistence.py's style.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service, quiz_store

DOCUMENT_ID = "lecture.pdf"


def two_question_quiz(quiz_id: str) -> dict:
    return {
        "quiz_id": quiz_id,
        "document_id": DOCUMENT_ID,
        "title": "Quiz",
        "difficulty": "easy",
        "topic_id": "document",
        "topic_name": "Entire document",
        "questions": [
            {
                "id": 1, "question": "Question one?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
                "topic_id": "document", "difficulty": "easy", "explanation": "Supported.",
                "source_chunk_ids": ["hash_1"], "validation_outcome": "accepted",
            },
            {
                "id": 2, "question": "Question two?",
                "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "B",
                "topic_id": "document", "difficulty": "easy", "explanation": "Supported.",
                "source_chunk_ids": ["hash_2"], "validation_outcome": "accepted",
            },
        ],
    }


class QuizPlayerProgressScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = "owner-1"
        quiz_store.save_quiz(DOCUMENT_ID, "easy", two_question_quiz("quiz-a"), self.owner)
        quiz_store.save_quiz(DOCUMENT_ID, "easy", two_question_quiz("quiz-b"), self.owner)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def answer(self, quiz_id, question_id, letter, index=None):
        return quiz_service.update_quiz_progress(
            DOCUMENT_ID, "easy", "document", self.owner,
            quiz_id=quiz_id, question_id=question_id, selected_answer=letter,
            current_question_index=index,
        )

    # 1/2. Autosaving one quiz's answer never bleeds into a sibling quiz sharing the same slot.
    def test_progress_is_isolated_between_sibling_quizzes(self):
        self.answer("quiz-a", 1, "A", index=0)
        self.answer("quiz-a", 2, "A", index=1)
        self.answer("quiz-b", 1, "B", index=0)

        attempt_a = quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a")
        attempt_b = quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-b")
        self.assertEqual(attempt_a["answers"], {"1": "A", "2": "A"})
        self.assertEqual(attempt_b["answers"], {"1": "B"})
        self.assertEqual(attempt_a["answered"], 2)
        self.assertEqual(attempt_b["answered"], 1)

    # 3/9. current_question_index persists per quiz_id and is what Resume restores.
    def test_current_question_index_persists_per_quiz_and_resume_restores_it(self):
        self.answer("quiz-a", 1, "A", index=0)
        self.answer("quiz-a", 2, "B", index=1)
        self.answer("quiz-b", 1, "A", index=0)

        attempt_a = quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a")
        attempt_b = quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-b")
        self.assertEqual(attempt_a["current_question_index"], 1)
        self.assertEqual(attempt_b["current_question_index"], 0)

    # Regression from the task: Qwen Quiz A answered 4/12, Gemma Quiz B answered 7/12, same
    # document/scope/difficulty -- Resume A restores only A's answers, Resume B restores only B's.
    def test_resume_a_then_resume_b_restores_each_ones_own_answers_only(self):
        quiz_store.save_quiz(DOCUMENT_ID, "easy", two_question_quiz("quiz-qwen"), self.owner)
        quiz_store.save_quiz(DOCUMENT_ID, "easy", two_question_quiz("quiz-gemma"), self.owner)

        self.answer("quiz-qwen", 1, "A", index=0)
        self.answer("quiz-gemma", 1, "B", index=0)
        self.answer("quiz-gemma", 2, "C", index=1)

        with patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT_ID: {"id": DOCUMENT_ID}}):
            resumed_qwen = quiz_service.load_quiz_with_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-qwen")
            resumed_gemma = quiz_service.load_quiz_with_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-gemma")

        self.assertEqual(resumed_qwen["latest_attempt"]["answers"], {"1": "A"})
        self.assertEqual(resumed_gemma["latest_attempt"]["answers"], {"1": "B", "2": "C"})

    # Autosaving never grades or completes -- only an explicit submit does (see section D/G).
    def test_autosave_never_completes_or_grades_the_attempt(self):
        saved = self.answer("quiz-a", 1, "A", index=0)
        saved = self.answer("quiz-a", 2, "A", index=1)
        self.assertFalse(saved["completed"])
        self.assertIsNone(saved["completed_at"])

    # 13. An unknown/foreign quiz_id must never resolve to another quiz.
    def test_unknown_quiz_id_never_resolves_to_another_quiz(self):
        with self.assertRaises(ValueError):
            self.answer("does-not-exist", 1, "A", index=0)

    def test_reset_progress_scoped_to_quiz_id_never_touches_a_sibling(self):
        before = self.answer("quiz-a", 1, "A", index=0)
        self.answer("quiz-b", 1, "B", index=0)

        quiz_service.clear_quiz_progress(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a")

        # The old attempt row is demoted (is_latest = 0) by the reset, so the next autosave for
        # quiz-a starts a brand-new attempt_id rather than continuing the reset one.
        after = self.answer("quiz-a", 1, "B", index=0)
        self.assertNotEqual(after["attempt_id"], before["attempt_id"])
        self.assertEqual(after["answers"], {"1": "B"})
        # quiz-b's own in-progress attempt is completely untouched throughout.
        attempt_b = quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-b")
        self.assertEqual(attempt_b["answers"], {"1": "B"})

    def test_reset_progress_with_unknown_quiz_id_raises(self):
        with self.assertRaises(ValueError):
            quiz_service.clear_quiz_progress(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="does-not-exist")

    # An attempt that already completed cannot be edited via autosave -- Retake starts a fresh
    # attempt_id instead (see submit_quiz_attempt, which always saves under a new attempt_id).
    def test_autosave_after_completion_is_rejected(self):
        quiz_service.submit_quiz_attempt(
            DOCUMENT_ID, "easy", "document", {"1": "A", "2": "B"}, self.owner, quiz_id="quiz-a",
        )
        with self.assertRaises(ValueError):
            self.answer("quiz-a", 1, "B", index=0)

    # The Quiz Player sends its full local snapshot: it replaces the saved answers (so a later
    # navigation save can never drop an earlier answer) and carries the position in the same call.
    def test_answer_snapshot_replaces_saved_answers_and_position_together(self):
        self.answer("quiz-a", 1, "A", index=0)
        saved = quiz_service.update_quiz_progress(
            DOCUMENT_ID, "easy", "document", self.owner,
            quiz_id="quiz-a", answers={"2": "c"}, current_question_index=1,
        )
        self.assertEqual(saved["answers"], {"2": "C"})
        self.assertEqual(saved["answered"], 1)
        self.assertEqual(saved["current_question_index"], 1)
        # quiz-b (same slot) never saw any of it.
        self.assertIsNone(quiz_store.get_latest_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-b"))

    def test_answer_snapshot_skips_cleared_answers_and_rejects_unknown_questions(self):
        saved = quiz_service.update_quiz_progress(
            DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a", answers={"1": "A", "2": []},
        )
        self.assertEqual(saved["answers"], {"1": "A"})
        with self.assertRaises(ValueError):
            quiz_service.update_quiz_progress(
                DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a", answers={"99": "A"},
            )

    def test_position_only_save_keeps_answers_and_is_clamped(self):
        self.answer("quiz-a", 1, "A", index=0)
        saved = quiz_service.update_quiz_progress(
            DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a", current_question_index=50,
        )
        self.assertEqual(saved["answers"], {"1": "A"})
        self.assertEqual(saved["current_question_index"], 1)

    # Retake semantics: a completed attempt stays in history; the same quiz_id then gets a fresh
    # in-progress attempt once its progress is reset.
    def test_completed_attempt_stays_historical_and_retake_starts_fresh_for_same_quiz_id(self):
        completed = quiz_service.submit_quiz_attempt(
            DOCUMENT_ID, "easy", "document", {"1": "A", "2": "B"}, self.owner, quiz_id="quiz-a",
        )
        quiz_service.clear_quiz_progress(DOCUMENT_ID, "easy", "document", self.owner, quiz_id="quiz-a")
        retake = self.answer("quiz-a", 1, "C", index=0)
        self.assertNotEqual(retake["attempt_id"], completed["attempt_id"])
        self.assertEqual(retake["quiz_id"], "quiz-a")
        self.assertFalse(retake["completed"])
        history = quiz_store.list_quiz_history(student_id=self.owner)
        self.assertTrue(any(item.get("attempt_id") == completed["attempt_id"] for item in history))

    def test_progress_request_model_accepts_the_player_snapshot(self):
        from backend.main import QuizProgressRequest
        request = QuizProgressRequest(
            difficulty="easy", topic_id="document", quiz_id="quiz-a",
            answers={"1": "A", "2": ["B", "C"]}, current_question_index=1,
        )
        self.assertEqual(request.answers, {"1": "A", "2": ["B", "C"]})


if __name__ == "__main__":
    unittest.main()
