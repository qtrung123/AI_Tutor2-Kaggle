"""Every Quiz records the model that really generated it.

The name comes from the runtime reference the backend sent to Ollama (never from the client), is
stored with the quiz (inside its persisted assessment plan), is returned by the API and survives a
regeneration with another model: the superseded quiz keeps the model it was made with.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, make_chunks

from backend import model_registry, quiz_service
from backend.main import QuizGenerateResponse
from backend.model_registry import describe_generation_model
from backend.quiz_service import _generate_quiz_from_units

QWEN = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
DEEPSEEK = "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M"
QWEN_INFO = {"model_id": "qwen-2.5-7b", "name": "Qwen2.5-7B-Instruct", "quantization": "Q4_K_M"}
DEEPSEEK_INFO = {"model_id": "deepseek-r1-14b", "name": "DeepSeek-R1-Distill-Qwen-14B", "quantization": "Q4_K_M"}
DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


class DescribeGenerationModelTests(unittest.TestCase):
    def test_the_two_compared_models(self):
        self.assertEqual(describe_generation_model(QWEN), QWEN_INFO)
        self.assertEqual(describe_generation_model(DEEPSEEK), DEEPSEEK_INFO)

    def test_the_runtime_reference_is_not_exposed(self):
        for reference in (QWEN, DEEPSEEK):
            self.assertNotIn("hf.co", str(describe_generation_model(reference)))

    def test_unregistered_and_untagged_references(self):
        self.assertEqual(describe_generation_model("hf.co/someone/Other-Model-GGUF:Q8_0"),
                         {"model_id": None, "name": "Other-Model", "quantization": "Q8_0"})
        self.assertEqual(describe_generation_model("qwen-test"), {"model_id": None, "name": "qwen-test", "quantization": None})
        self.assertEqual(describe_generation_model("hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M")["name"], "Qwen3-8B")  # legacy quizzes

    def test_every_offered_model_is_described_consistently_with_the_registry(self):
        for public_id in ("qwen-2.5-7b", "deepseek-r1-14b"):
            with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "deepseek-r1-14b")):
                reference = model_registry.resolve_generation_model(public_id)
            self.assertEqual(describe_generation_model(reference)["model_id"], public_id)


class QuizRecordsItsModelTests(unittest.TestCase):
    def run_engine(self, model_reference):
        FakeModel.reset([{"questions": candidates(range(15))}])
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id=model_reference,
                regenerate=False, question_count=12,
            )

    def test_the_stored_model_is_the_one_sent_to_ollama(self):
        for reference, expected in ((QWEN, QWEN_INFO), (DEEPSEEK, DEEPSEEK_INFO)):
            with self.subTest(reference=reference):
                quiz = self.run_engine(reference)
                self.assertEqual({call["model"] for call in FakeModel.kwargs}, {reference})   # what really ran
                self.assertEqual(quiz["generation_model"], expected)
                self.assertEqual(quiz["assessment_plan"]["generation_model"], expected)

    def test_the_api_response_carries_the_model(self):
        quiz = self.run_engine(DEEPSEEK)
        quiz["requested_count"], quiz["actual_count"], quiz["status"] = 12, 12, "complete"
        self.assertEqual(QuizGenerateResponse(**quiz).generation_model, DEEPSEEK_INFO)
        self.assertIsNone(QuizGenerateResponse(**{**quiz, "generation_model": None}).generation_model)


class ModelSurvivesRegenerationTests(unittest.TestCase):
    """Real SQLite database: nothing in the persistence layer is patched."""

    def setUp(self):
        from backend import quiz_store
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db")
        self.patch.start()
        self.store = quiz_store

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
                DOCUMENT["id"], "easy", "document", question_count=12, quiz_name="Benchmark",
                model_id=model_reference, regenerate=regenerate,
            )

    def test_the_model_is_saved_reloaded_and_returned_by_every_read_path(self):
        first = self.generate(QWEN)
        self.assertEqual(first["generation_model"], QWEN_INFO)
        active = self.store.get_quiz(DOCUMENT["id"], "easy", "document")
        self.assertEqual(active["generation_model"], QWEN_INFO)
        self.assertEqual(active["assessment_plan"]["generation_model"], QWEN_INFO)
        self.assertEqual(self.store.get_quiz_by_id(first["quiz_id"])["generation_model"], QWEN_INFO)
        with patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}):
            self.assertEqual(quiz_service.load_quiz_with_attempt(DOCUMENT["id"], "easy", "document")["quiz"]["generation_model"], QWEN_INFO)
        self.assertEqual(self.generate(QWEN)["generation_model"], QWEN_INFO)          # cache hit keeps it
        self.assertEqual(len(FakeModel.prompts), 0)

    def test_regenerating_with_another_model_makes_a_new_quiz_and_keeps_the_old_models_name(self):
        qwen_quiz = self.generate(QWEN)
        deepseek_quiz = self.generate(DEEPSEEK, regenerate=True)

        self.assertNotEqual(qwen_quiz["quiz_id"], deepseek_quiz["quiz_id"])
        self.assertEqual(deepseek_quiz["generation_model"], DEEPSEEK_INFO)
        self.assertEqual({call["model"] for call in FakeModel.kwargs}, {DEEPSEEK})
        active = self.store.get_quiz(DOCUMENT["id"], "easy", "document")
        self.assertEqual((active["quiz_id"], active["generation_model"]), (deepseek_quiz["quiz_id"], DEEPSEEK_INFO))
        old = self.store.get_quiz_by_id(qwen_quiz["quiz_id"])                          # superseded, still stored
        self.assertIsNotNone(old)
        self.assertEqual(old["generation_model"], QWEN_INFO)
        self.assertEqual(old["assessment_plan"]["generation_model"], QWEN_INFO)
        with self.store._connect() as connection:
            rows = dict(connection.execute("SELECT quiz_id, is_active FROM quizzes").fetchall())
        # (the store also imports legacy quizzes of the real data folder, so look at ours only)
        self.assertEqual((rows[qwen_quiz["quiz_id"]], rows[deepseek_quiz["quiz_id"]]), (0, 1))

    def test_generating_with_deepseek_over_a_saved_qwen_quiz_returns_the_qwen_quiz_labelled_qwen(self):
        qwen_quiz = self.generate(QWEN)
        again = self.generate(DEEPSEEK)                                   # no regenerate: served from the cache
        self.assertEqual(again["quiz_id"], qwen_quiz["quiz_id"])
        self.assertEqual(len(FakeModel.prompts), 0)                       # no model ran at all
        self.assertEqual(again["generation_model"], QWEN_INFO)            # never DeepSeek's name
        self.assertEqual(QuizGenerateResponse(**again).generation_model, QWEN_INFO)   # what the API returns
        self.assertEqual(self.store.get_quiz(DOCUMENT["id"], "easy", "document")["generation_model"], QWEN_INFO)

    def test_switching_back_and_forth_keeps_each_quizs_own_model(self):
        ids = []
        for reference in (QWEN, DEEPSEEK, QWEN):
            ids.append(self.generate(reference, regenerate=bool(ids))["quiz_id"])
        names = [self.store.get_quiz_by_id(quiz_id)["generation_model"]["name"] for quiz_id in ids]
        self.assertEqual(names, ["Qwen2.5-7B-Instruct", "DeepSeek-R1-Distill-Qwen-14B", "Qwen2.5-7B-Instruct"])

    def test_a_quiz_saved_before_this_field_existed_has_no_model(self):
        quiz = self.generate(QWEN)
        with self.store._connect() as connection:
            import json
            plan = json.loads(connection.execute(
                "SELECT assessment_plan_json FROM quizzes WHERE quiz_id = ?", (quiz["quiz_id"],)).fetchone()[0])
            plan.pop("generation_model")
            connection.execute("UPDATE quizzes SET assessment_plan_json = ? WHERE quiz_id = ?", (json.dumps(plan), quiz["quiz_id"]))
        self.assertIsNone(self.store.get_quiz_by_id(quiz["quiz_id"])["generation_model"])


class FrontendShowsTheModelTests(unittest.TestCase):
    def test_the_quiz_title_shows_the_backend_supplied_model_name(self):
        frontend = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("quiz?.generation_model || quiz?.assessment_plan?.generation_model", frontend)
        self.assertIn("` · Generated by: ${model.name}`", frontend)
        self.assertIn("quizPartialSuffix(quiz) + quizModelSuffix(quiz)", frontend)
        self.assertIn("assessmentTitle.textContent = assessmentTitleText(currentQuiz)", frontend)


if __name__ == "__main__":
    unittest.main()
