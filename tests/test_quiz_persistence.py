"""Quiz persistence: every generated Quiz is its own permanent artifact identified by quiz_id.

Root cause this covers: backend/quiz_store.py's _insert_quiz used to deactivate (is_active=0)
every OTHER quiz sharing the same (owner, document, topic, difficulty) slot whenever a new one was
saved -- so generating a quiz with a different model (or a plain re-run) silently hid the earlier
quiz from list_document_quizzes/list_quiz_statuses ("Your Quizzes"/Quiz History), even though the
row itself, its questions and its attempts were never deleted. Real SQLite database throughout
(nothing in the persistence layer is patched), matching tests/test_quiz_generation_model.py's style.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, make_chunks

from backend import quiz_attempt_service, quiz_service, quiz_store
from backend.api import quiz_generation as quiz_generation_api

QWEN = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
GEMMA = "gemma3:12b-it-q4_K_M"
DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


class QuizPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = "owner-1"

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def generate(self, model_reference, regenerate=False, difficulty="easy", question_count=12, quiz_name="Benchmark"):
        FakeModel.reset([{"questions": candidates(range(15))}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
        ):
            return quiz_service.generate_quiz(
                DOCUMENT["id"], difficulty, "document", question_count=question_count,
                quiz_name=quiz_name, model_id=model_reference, regenerate=regenerate, owner_id=self.owner,
            )

    def variants(self):
        """What the Quiz Library reads: list_quiz_statuses' per-document variants, unfiltered by
        any "currently selected model" -- that concept does not exist at this layer."""
        with patch.object(quiz_service, "list_indexed_documents",
                          return_value=[{"id": DOCUMENT["id"], "title": "Lecture", "chunks": 12}]):
            (status,) = [item for item in quiz_service.list_quiz_statuses(self.owner) if item["document_id"] == DOCUMENT["id"]]
        return status["variants"]

    # 1. Generate Qwen quiz -> 1 quiz exists
    def test_generating_one_quiz_creates_exactly_one_artifact(self):
        quiz = self.generate(QWEN)
        self.assertEqual([v["quiz_id"] for v in self.variants()], [quiz["quiz_id"]])

    # 2. Generate Gemma with identical settings -> both remain
    def test_generating_a_different_model_with_identical_settings_preserves_both(self):
        qwen_quiz = self.generate(QWEN)
        gemma_quiz = self.generate(GEMMA, regenerate=True)
        self.assertEqual({v["quiz_id"] for v in self.variants()}, {qwen_quiz["quiz_id"], gemma_quiz["quiz_id"]})
        # Neither row was ever deactivated by the other's insert.
        self.assertIsNotNone(quiz_store.get_quiz_by_id(qwen_quiz["quiz_id"], self.owner))
        self.assertIsNotNone(quiz_store.get_quiz_by_id(gemma_quiz["quiz_id"], self.owner))

    # 3. Generate Qwen again identical settings -> all 3 remain with unique quiz_id
    def test_generating_qwen_again_creates_a_third_distinct_quiz_all_three_remain(self):
        first = self.generate(QWEN)
        second = self.generate(GEMMA, regenerate=True)
        third = self.generate(QWEN, regenerate=True)
        ids = [first["quiz_id"], second["quiz_id"], third["quiz_id"]]
        self.assertEqual(len(set(ids)), 3, "every generation must get its own unique quiz_id")
        self.assertEqual({v["quiz_id"] for v in self.variants()}, set(ids))

    # 4. Switch selected model -> all 3 remain visible
    def test_the_variant_listing_has_no_selected_model_parameter_to_filter_by(self):
        ids = {self.generate(model, regenerate=bool(index))["quiz_id"]
               for index, model in enumerate((QWEN, GEMMA, QWEN))}
        # list_quiz_statuses/list_document_quizzes take only an owner_id -- there is no "selected
        # model" argument anywhere in this call chain, so switching it client-side cannot hide
        # anything server-side.
        self.assertEqual({v["quiz_id"] for v in self.variants()}, ids)
        self.assertEqual(len(ids), 3)

    # 5. Delete one quiz -> only that quiz is removed
    def test_deleting_one_quiz_removes_only_that_quiz(self):
        first = self.generate(QWEN)
        second = self.generate(GEMMA, regenerate=True)
        quiz_service.delete_quiz(first["quiz_id"], self.owner)
        self.assertEqual({v["quiz_id"] for v in self.variants()}, {second["quiz_id"]})
        self.assertIsNone(quiz_store.get_quiz_by_id(first["quiz_id"], self.owner))
        self.assertIsNotNone(quiz_store.get_quiz_by_id(second["quiz_id"], self.owner))

    # 6. Existing progress/result belongs to the correct quiz_id
    def test_existing_progress_stays_attached_to_its_own_quiz_id(self):
        first = self.generate(QWEN)
        answers = {str(question["id"]): question["correct_answer"] for question in first["questions"]}
        quiz_attempt_service.submit_quiz_attempt(
            DOCUMENT["id"], "easy", "document", answers, student_id=self.owner, quiz_id=first["quiz_id"],
        )
        second = self.generate(GEMMA, regenerate=True)   # a sibling quiz at the same slot

        summary_first = quiz_store.get_quiz_attempt_summary(first["quiz_id"], self.owner)
        summary_second = quiz_store.get_quiz_attempt_summary(second["quiz_id"], self.owner)
        self.assertEqual(summary_first["attempts"], 1)
        self.assertEqual(summary_first["latest_score"], 100)
        self.assertEqual(summary_second["attempts"], 0, "generating a sibling quiz must not attach the old attempt to it")

        by_id = {variant["quiz_id"]: variant for variant in self.variants()}
        self.assertEqual(by_id[first["quiz_id"]]["progress_status"], "completed")
        self.assertEqual(by_id[first["quiz_id"]]["score"], 12)
        self.assertEqual(by_id[second["quiz_id"]]["progress_status"], "not_started")
        self.assertIsNone(by_id[second["quiz_id"]]["score"])

    # 7. Quiz list ordered newest first
    def test_quiz_list_is_ordered_newest_first(self):
        ids = [self.generate(QWEN, regenerate=bool(index))["quiz_id"] for index in range(3)]
        self.assertEqual([variant["quiz_id"] for variant in self.variants()], list(reversed(ids)))


class OpeningAQuizByIdTests(unittest.TestCase):
    """Root cause: Start/Resume used to load "the quiz" via the slot only (document, topic,
    difficulty) -- backend/quiz_store.get_quiz's "newest quiz here" lookup. Once several quizzes
    share a slot, clicking the OLDER Library card would silently open the NEWER quiz instead. Every
    artifact action must resolve the exact quiz_id clicked, with no fallback to "latest in slot".
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = "owner-3"

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def generate(self, model_reference, regenerate=False):
        FakeModel.reset([{"questions": candidates(range(15))}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
        ):
            return quiz_service.generate_quiz(
                DOCUMENT["id"], "easy", "document", question_count=12,
                quiz_name="Benchmark", model_id=model_reference, regenerate=regenerate, owner_id=self.owner,
            )

    def open_by_id(self, quiz_id):
        with patch.object(quiz_attempt_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}):
            return quiz_attempt_service.load_quiz_with_attempt(DOCUMENT["id"], "easy", "document", self.owner, quiz_id=quiz_id)

    def test_clicking_the_qwen_card_then_the_gemma_card_opens_each_ones_own_quiz_id(self):
        qwen_quiz = self.generate(QWEN)
        gemma_quiz = self.generate(GEMMA, regenerate=True)   # a newer quiz at the same slot

        opened_qwen = self.open_by_id(qwen_quiz["quiz_id"])
        self.assertEqual(opened_qwen["quiz"]["quiz_id"], qwen_quiz["quiz_id"])
        self.assertEqual(opened_qwen["quiz"]["generation_model"], qwen_quiz["generation_model"])

        opened_gemma = self.open_by_id(gemma_quiz["quiz_id"])
        self.assertEqual(opened_gemma["quiz"]["quiz_id"], gemma_quiz["quiz_id"])
        self.assertEqual(opened_gemma["quiz"]["generation_model"], gemma_quiz["generation_model"])
        self.assertNotEqual(opened_qwen["quiz"]["generation_model"], opened_gemma["quiz"]["generation_model"])

    def test_an_unknown_quiz_id_never_falls_back_to_the_latest_quiz_in_the_slot(self):
        self.generate(QWEN)
        self.generate(GEMMA, regenerate=True)   # this would be "the latest in the slot"
        result = self.open_by_id("does-not-exist")
        self.assertIsNone(result["quiz"])

    def test_the_api_route_accepts_a_quiz_id_query_parameter(self):
        qwen_quiz = self.generate(QWEN)
        self.generate(GEMMA, regenerate=True)
        with patch.object(quiz_attempt_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}):
            result = quiz_generation_api.quiz_detail(DOCUMENT["id"], topic_id="document", difficulty="easy",
                                      quiz_id=qwen_quiz["quiz_id"], current_user={"id": self.owner})
        self.assertEqual(result["quiz"]["quiz_id"], qwen_quiz["quiz_id"])


class ExplicitGenerateAlwaysCreatesANewQuizTests(unittest.TestCase):
    """Root cause: an explicit Generate/Create Quiz submission went through the same
    "reuse-if-compatible" cache-hit branch as a passive read, so re-submitting identical settings
    silently returned an older quiz instead of creating a new artifact. The live frontend now always
    sends regenerate=true for this action (see requestGeneratedQuiz); this exercises that exact
    request shape end to end through backend/api/quiz_generation.py's QuizGenerateRequest/quiz_generate route.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.owner = {"id": "owner-4"}

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def generate_via_route(self, model_id, regenerate=True, quiz_name="Benchmark"):
        FakeModel.reset([{"questions": candidates(range(15))}])
        request = quiz_generation_api.QuizGenerateRequest(
            document_id=DOCUMENT["id"], assessment_scope="document", difficulty="easy",
            question_count=12, model_id=model_id, quiz_name=quiz_name, regenerate=regenerate,
        )
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_generation_api, "prepare_generation_model"),
            patch.object(quiz_generation_api, "resolve_generation_model", side_effect=lambda value: value),
        ):
            return quiz_generation_api.quiz_generate(request, current_user=self.owner)

    def test_qwen_gemma_qwen_again_all_three_remain_with_unique_quiz_id(self):
        quiz_a = self.generate_via_route(QWEN)
        quiz_b = self.generate_via_route(GEMMA)
        quiz_c = self.generate_via_route(QWEN)
        ids = {quiz_a.quiz_id, quiz_b.quiz_id, quiz_c.quiz_id}
        self.assertEqual(len(ids), 3, "Qwen Easy 12 -> A, Gemma Easy 12 -> B, Qwen Easy 12 again -> C, all unique")
        with patch.object(quiz_service, "list_indexed_documents",
                          return_value=[{"id": DOCUMENT["id"], "title": "Lecture", "chunks": 12}]):
            (status,) = [item for item in quiz_service.list_quiz_statuses(self.owner["id"]) if item["document_id"] == DOCUMENT["id"]]
        self.assertEqual({v["quiz_id"] for v in status["variants"]}, ids)

    def test_regenerate_defaults_to_false_so_other_callers_keep_the_reuse_if_compatible_cache(self):
        omitted = quiz_generation_api.QuizGenerateRequest(
            document_id=DOCUMENT["id"], assessment_scope="document", difficulty="easy",
            question_count=12, model_id=QWEN, quiz_name="Benchmark",
        )
        self.assertFalse(omitted.regenerate)


if __name__ == "__main__":
    unittest.main()
