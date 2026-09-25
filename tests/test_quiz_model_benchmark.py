"""Tests for the admin Quiz model benchmark (Qwen 2.5 7B vs Gemma 3 12B).

Models are always mocked: the end-to-end tests run the real live Quiz pipeline with the shared
FakeModel standing in for ChatOllama; model preparation (pulls) and warm-up (loads) are patched out.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quiz_fixtures import FakeModel, candidates, make_chunks

from backend import auth_store, model_benchmark_service, model_benchmark_store, quiz_diagnostics, quiz_service, quiz_store
from backend import flashcard_store
from backend.main import app
from backend.model_benchmark_service import (
    BenchmarkInputChanged, aggregate_runs, prepare_benchmark, run_benchmark,
)
from backend.model_comparison_service import get_quiz_model_comparison
from backend import model_registry
from backend.model_registry import resolve_generation_model
from backend.quiz_units import QUIZ_ENGINE_VERSION, QUIZ_NUM_CTX, QUIZ_PROMPT_VERSION, candidate_target

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash-1", "topic_schema_version": 2, "topics": [], "chunks": 12}
QWEN_REF = resolve_generation_model("qwen-2.5-7b")
GEMMA_REF = resolve_generation_model("gemma3-12b")


class _TempStores(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patchers = [
            patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", root / "bench.json"),
            patch.object(quiz_store, "DATABASE_PATH", root / "app.db"),
            patch.object(flashcard_store, "DATABASE_PATH", root / "app.db"),
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(model_benchmark_service, "_document_lookup", return_value={DOCUMENT["id"]: DOCUMENT}),
            patch.object(model_benchmark_service, "prepare_generation_model", side_effect=self.fake_prepare),
            patch.object(model_benchmark_service, "warm_generation_model", side_effect=self.fake_warm),
        ]
        self.events = []
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def fake_prepare(self, model_id):
        self.events.append(("prepare", model_id))
        return {"model_id": model_id, "model_prepare_ms": 7, "prepared": True}

    def fake_warm(self, model_id, num_ctx, keep_alive):
        self.events.append(("warm", model_id, num_ctx, keep_alive))
        return {"model_id": model_id, "warm_ms": 999_000, "ollama_load_ms": 998_000}


class EndToEndBenchmarkTests(_TempStores):
    """The real generate_quiz pipeline, one FakeModel payload per call."""

    def run_benchmark(self, payloads, runs=2):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
        ):
            return run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, runs)

    def test_both_models_get_identical_inputs_and_every_run_is_forced_fresh(self):
        valid = {"questions": candidates(range(candidate_target(12)))}
        # Order is model -> document -> run: qwen r1, qwen r2, gemma r1, gemma r2 (queue empty -> fails).
        state = self.run_benchmark([valid, valid, valid])

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["progress"], {"completed": 4, "total": 4})
        runs = state["runs"]
        self.assertEqual([(run["model_id"], run["run_number"]) for run in runs], [
            ("qwen-2.5-7b", 1), ("qwen-2.5-7b", 2), ("gemma3-12b", 1), ("gemma3-12b", 2),
        ])
        # No silent fallback: each call went to exactly the requested model.
        self.assertEqual([kwargs["model"] for kwargs in FakeModel.kwargs[:3]], [QWEN_REF, QWEN_REF, GEMMA_REF])
        self.assertTrue(all(kwargs["model"] == GEMMA_REF for kwargs in FakeModel.kwargs[3:]))

        # Identical document + flashcard snapshot + config for every run of both models.
        identity = {
            (run["document_id"], run["document_hash"], run["flashcard_count"], run["flashcard_hash"],
             run["difficulty"], run["question_count"], run["quiz_engine_version"], run["quiz_prompt_version"])
            for run in runs
        }
        self.assertEqual(identity, {(
            "lecture.pdf", "hash-1", 0, state["config"]["documents"][0]["flashcard_hash"], "easy", 12,
            QUIZ_ENGINE_VERSION, QUIZ_PROMPT_VERSION,
        )})

        qwen_first = runs[0]
        self.assertTrue(qwen_first["success"])
        self.assertIsNone(qwen_first["error"])
        self.assertEqual((qwen_first["model_ref"], qwen_first["quantization"]), (QWEN_REF, "Q4_K_M"))
        self.assertEqual(runs[2]["model_ref"], GEMMA_REF)
        self.assertEqual(qwen_first["generated_by"]["model_id"], "qwen-2.5-7b")
        self.assertIs(qwen_first["cache_hit"], False)
        self.assertEqual(qwen_first["ollama_load_ms"], 0)        # warm model: nothing loaded inside the run
        self.assertNotIn("model_prepare_ms", qwen_first)        # cold start lives on the warm-up, not the run
        self.assertEqual(qwen_first["benchmark_id"], state["benchmark_id"])
        self.assertEqual((qwen_first["llm_calls"], qwen_first["retries"]), (1, 0))
        self.assertEqual((qwen_first["prompt_tokens"], qwen_first["generated_tokens"]), (456, 123))
        self.assertEqual(qwen_first["tokens_per_second"], 61.5)
        self.assertEqual((qwen_first["requested_count"], qwen_first["final_count"]), (12, 12))
        self.assertEqual(qwen_first["validated_count"], candidate_target(12))
        self.assertEqual(qwen_first["rejected_count"], 0)
        self.assertEqual(qwen_first["rejection_reasons"], {})
        self.assertEqual(qwen_first["question_type_distribution"], {"single_choice": 12})
        for key in ("retrieval_ms", "generation_ms", "total_ms"):
            self.assertIsInstance(qwen_first[key], int)
        self.assertEqual(qwen_first["generation_config"]["calls"], [
            {"call": 1, "temperature": 0.1, "num_ctx": QUIZ_NUM_CTX, "num_predict": FakeModel.kwargs[0]["num_predict"],
             "keep_alive": FakeModel.kwargs[0]["keep_alive"]},
        ])
        self.assertIsNone(qwen_first["generation_config"]["seed"])   # unset -> null, never invented
        # regenerate=True: the second identical request is a brand-new quiz, not a cache hit.
        self.assertNotEqual(runs[0]["quiz_id"], runs[1]["quiz_id"])
        self.assertIs(runs[1]["cache_hit"], False)

        # A failed run is recorded (with its diagnostics) and the benchmark still finished.
        failed = runs[3]
        self.assertFalse(failed["success"])
        self.assertEqual(failed["error"]["type"], "QuizGenerationError")
        self.assertIsNone(failed["quiz_id"])
        self.assertIsInstance(failed["total_ms"], int)
        self.assertGreaterEqual(failed["llm_calls"], 1)
        self.assertIsNone(failed["tokens_per_second"])

        by_model = {row["model_id"]: row for row in state["results"]}
        self.assertEqual((by_model["qwen-2.5-7b"]["success_rate"], by_model["qwen-2.5-7b"]["failures"]), (1.0, 0))
        self.assertEqual((by_model["gemma3-12b"]["success_rate"], by_model["gemma3-12b"]["failures"]), (0.5, 1))
        self.assertEqual(by_model["gemma3-12b"]["final_question_rate"], 0.5)
        self.assertEqual(by_model["qwen-2.5-7b"]["grounding_rate"], 1.0)
        self.assertEqual(by_model["qwen-2.5-7b"]["avg_tokens_per_second"], 61.5)

    def test_warm_up_is_measured_separately_and_never_inside_latency(self):
        valid = {"questions": candidates(range(candidate_target(12)))}
        state = self.run_benchmark([valid, valid], runs=1)
        # Each model: pull check, then load with the Quiz num_ctx, before its first measured call.
        self.assertEqual(self.events, [
            ("prepare", "qwen-2.5-7b"), ("warm", "qwen-2.5-7b", QUIZ_NUM_CTX, quiz_service.QUIZ_GENERATION_KEEP_ALIVE),
            ("prepare", "gemma3-12b"), ("warm", "gemma3-12b", QUIZ_NUM_CTX, quiz_service.QUIZ_GENERATION_KEEP_ALIVE),
        ])
        self.assertEqual([warmup["model_id"] for warmup in state["warmups"]], ["qwen-2.5-7b", "gemma3-12b"])
        warmup = state["warmups"][0]
        self.assertEqual((warmup["model_prepare_ms"], warmup["pulled"], warmup["warm_ms"], warmup["ollama_load_ms"]),
                         (7, True, 999_000, 998_000))
        self.assertEqual(warmup["cold_start_ms"], 999_007)
        for row in state["results"]:
            # The 999 s warm-up is kept, separately, and is absent from every latency figure.
            self.assertEqual((row["model_prepare_ms"], row["warm_ms"], row["cold_start_ms"]), (7, 999_000, 999_007))
            for key in ("avg_latency_seconds", "p50_latency_seconds", "p95_latency_seconds"):
                self.assertLess(row[key], 999)
        self.assertEqual(len(model_benchmark_store.load_benchmark_state()["warmups"]), 2)
        self.assertEqual(len(get_quiz_model_comparison()["warmups"]), 2)

    def test_benchmark_quizzes_are_kept_by_id_but_hidden_from_learner_surfaces(self):
        valid = {"questions": candidates(range(candidate_target(12)))}
        state = self.run_benchmark([valid, valid], runs=1)
        quiz_ids = [run["quiz_id"] for run in state["runs"]]
        self.assertTrue(all(quiz_ids))
        for quiz_id in quiz_ids:   # evidence for Model Comparison stays loadable by exact id
            stored = quiz_store.get_quiz_by_id(quiz_id, "owner-1")
            self.assertEqual(stored["benchmark_id"], state["benchmark_id"])
            self.assertTrue(stored["title"].startswith("[Benchmark]"))
        self.assertEqual(quiz_store.list_document_quizzes(DOCUMENT["id"], "owner-1"), {})
        self.assertIsNone(quiz_store.get_quiz(DOCUMENT["id"], "easy", "document", "owner-1"))
        self.assertEqual(quiz_store.get_document_quiz_activity(DOCUMENT["id"], "owner-1")["quizzes"], [])
        with patch.object(quiz_service, "list_indexed_documents", return_value=[DOCUMENT]):
            (library_entry,) = quiz_service.list_quiz_statuses("owner-1")
        self.assertEqual(library_entry["variants"], [])

        # A learner's normal request is never served a benchmark quiz: it generates its own, unmarked.
        FakeModel.reset([valid])
        with (
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
        ):
            learner = quiz_service.generate_quiz(
                DOCUMENT["id"], "easy", "document", owner_id="owner-1", model_id=QWEN_REF,
                question_count=12, quiz_name="Mine",
            )
        self.assertNotIn(learner["quiz_id"], quiz_ids)
        self.assertIsNone(learner["benchmark_id"])
        self.assertEqual(list(quiz_store.list_document_quizzes(DOCUMENT["id"], "owner-1")), [learner["quiz_id"]])

    def test_results_persist_and_reach_the_comparison_page(self):
        valid = {"questions": candidates(range(candidate_target(12)))}
        self.run_benchmark([valid, valid], runs=1)
        stored = model_benchmark_store.load_benchmark_state()
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(len(stored["runs"]), 2)
        comparison = get_quiz_model_comparison()
        self.assertEqual(comparison["benchmark_status"], "completed")
        self.assertIsNotNone(comparison["generated_at"])
        self.assertTrue(all(model["measured"] for model in comparison["models"]))
        self.assertEqual(len(comparison["runs"]), 2)
        self.assertEqual(comparison["config"]["runs_per_model_per_document"], 1)


def _fake_quiz(model_id="qwen-2.5-7b", cache_hit=False):
    def generate(**kwargs):
        with quiz_diagnostics.start_run("fake") as diag:
            diag.set_counts(cache_hit=cache_hit)
            diag.log_summary()
        generate.calls.append(kwargs)
        return {
            "quiz_id": f"quiz-{len(generate.calls)}", "question_count": 12, "questions": [],
            "benchmark_id": quiz_store._benchmark_id.get(),
            "generation_model": {"model_id": model_id(kwargs) if callable(model_id) else model_id},
            "assessment_plan": {"requested_count": 12, "calls": [], "validation_results": {}},
        }
    generate.calls = []
    return generate


def _generated_by_requested(kwargs):
    return "qwen-2.5-7b" if kwargs["model_id"] == QWEN_REF else "gemma3-12b"


class GuardTests(_TempStores):
    def test_every_run_passes_regenerate_true(self):
        fake = _fake_quiz(_generated_by_requested)
        with patch.object(model_benchmark_service, "generate_quiz", fake):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "medium", 15, 3)
        self.assertEqual(len(fake.calls), 6)
        self.assertTrue(all(call["regenerate"] is True for call in fake.calls))
        self.assertTrue(all(call["question_count"] == 15 and call["difficulty"] == "medium" for call in fake.calls))
        self.assertTrue(all(run["success"] for run in state["runs"]))

    def test_a_quiz_from_another_model_is_recorded_as_a_failure(self):
        with patch.object(model_benchmark_service, "generate_quiz", _fake_quiz("qwen-2.5-7b")):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 1)
        gemma = [run for run in state["runs"] if run["model_id"] == "gemma3-12b"][0]
        self.assertFalse(gemma["success"])
        self.assertEqual(gemma["error"]["type"], "BenchmarkModelMismatch")
        self.assertEqual(gemma["quiz_id"], "quiz-2")   # still recorded for inspection

    def test_a_failed_warm_up_fails_that_models_runs_without_generating_and_the_other_model_continues(self):
        fake = _fake_quiz(_generated_by_requested)

        def warm(model_id, num_ctx, keep_alive):
            if model_id == "qwen-2.5-7b":
                raise RuntimeError("load failed")
            return {"model_id": model_id, "warm_ms": 5, "ollama_load_ms": None}

        with (
            patch.object(model_benchmark_service, "warm_generation_model", side_effect=warm),
            patch.object(model_benchmark_service, "generate_quiz", fake),
        ):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 2)
        qwen_warmup, gemma_warmup = state["warmups"]
        self.assertFalse(qwen_warmup["success"])
        self.assertEqual(qwen_warmup["error"]["message"], "load failed")
        self.assertEqual(qwen_warmup["model_prepare_ms"], 7)   # what was measured is never discarded
        self.assertIsNone(gemma_warmup["ollama_load_ms"])      # unavailable -> null
        self.assertEqual([call["model_id"] for call in fake.calls], [GEMMA_REF, GEMMA_REF])
        qwen_runs = [run for run in state["runs"] if run["model_id"] == "qwen-2.5-7b"]
        self.assertTrue(all(run["error"]["type"] == "BenchmarkWarmupFailed" for run in qwen_runs))
        self.assertTrue(all(run["success"] for run in state["runs"] if run["model_id"] == "gemma3-12b"))

    def test_a_quiz_saved_without_the_benchmark_marker_is_a_failure(self):
        fake = _fake_quiz(_generated_by_requested)

        def unmarked(**kwargs):
            return {**fake(**kwargs), "benchmark_id": None}

        with patch.object(model_benchmark_service, "generate_quiz", side_effect=unmarked):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 1)
        self.assertTrue(all(not run["success"] and run["quiz_id"] for run in state["runs"]))

    def test_a_cache_hit_is_recorded_as_a_failure(self):
        with patch.object(model_benchmark_service, "generate_quiz", _fake_quiz(_generated_by_requested, cache_hit=True)):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 1)
        self.assertTrue(all(run["error"]["type"] == "BenchmarkCacheHit" for run in state["runs"]))

    def test_a_changed_flashcard_snapshot_fails_runs_but_the_benchmark_continues(self):
        current = {"hash": "cards-A"}

        def snapshot(_document, _owner):
            return {"set_id": "set-1", "flashcard_count": 3, "flashcard_hash": current["hash"],
                    "hint_term_count": 2, "hint_terms_hash": "h"}

        fake = _fake_quiz(_generated_by_requested)

        def generate(**kwargs):
            current["hash"] = "cards-B"   # flashcards edited while the first run was generating
            return fake(**kwargs)

        with (
            patch.object(model_benchmark_service, "flashcard_snapshot", side_effect=snapshot),
            patch.object(model_benchmark_service, "generate_quiz", side_effect=generate),
        ):
            state = run_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 2)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["runs"]), 4)
        self.assertEqual(len(fake.calls), 1)   # later runs refuse to generate on different inputs
        self.assertTrue(all(run["error"]["type"] == BenchmarkInputChanged.__name__ for run in state["runs"]))
        self.assertTrue(all(run["flashcard_hash"] == "cards-A" and run["flashcard_count"] == 3 for run in state["runs"]))

    def test_invalid_requests_are_refused_before_anything_runs(self):
        for args in (
            ([], "easy", 12, 3), (["missing.pdf"], "easy", 12, 3), ([DOCUMENT["id"]], "hard", 12, 3),
            ([DOCUMENT["id"]], "easy", 13, 3), ([DOCUMENT["id"]], "easy", 12, 0), ([DOCUMENT["id"]], "easy", 12, 11),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                prepare_benchmark("owner-1", *args)
        with patch.object(model_benchmark_service, "resolve_generation_model", side_effect=ValueError("not allowed")):
            with self.assertRaises(ValueError):
                prepare_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 3)
        self.assertIsNone(model_benchmark_store.load_benchmark_state()["status"])

    def test_a_running_benchmark_without_a_worker_reads_as_interrupted(self):
        prepare_benchmark("owner-1", [DOCUMENT["id"]], "easy", 12, 3)
        self.assertEqual(get_quiz_model_comparison()["benchmark_status"], "interrupted")


class WarmGenerationModelTests(unittest.TestCase):
    def test_loads_the_resolved_model_with_the_given_num_ctx_and_reports_load_time(self):
        posted = []

        class FakeClient:
            def __init__(self, **_kwargs): ...
            def __enter__(self): return self
            def __exit__(self, *_args): return False

            def post(self, url, json):
                posted.append((url, json))
                return type("Response", (), {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {"done": True, "load_duration": 2_500_000_000},
                })()

        with patch.object(model_registry.httpx, "Client", FakeClient):
            result = model_registry.warm_generation_model("gemma3-12b", 16384, "5m")
        ((url, body),) = posted
        self.assertTrue(url.endswith("/api/generate"))
        self.assertEqual(body, {"model": GEMMA_REF, "prompt": "", "stream": False, "keep_alive": "5m",
                                "options": {"num_ctx": 16384}})
        self.assertEqual(result["ollama_load_ms"], 2500)
        self.assertIsInstance(result["warm_ms"], int)


class AggregateTests(unittest.TestCase):
    def test_rates_and_latency_percentiles(self):
        runs = [
            {"model_id": "qwen-2.5-7b", "success": True, "requested_count": 12, "final_count": 12, "total_ms": 10000,
             "validated_count": 15, "grounding_rejections": 5, "retries": 0, "tokens_per_second": 40.0},
            {"model_id": "qwen-2.5-7b", "success": True, "requested_count": 12, "final_count": 6, "total_ms": 20000,
             "validated_count": 6, "grounding_rejections": 4, "retries": 2, "tokens_per_second": 20.0},
            {"model_id": "qwen-2.5-7b", "success": True, "requested_count": 12, "final_count": 12, "total_ms": 30000,
             "validated_count": 12, "grounding_rejections": 0, "retries": 1, "tokens_per_second": None},
            {"model_id": "qwen-2.5-7b", "success": False, "requested_count": 12, "final_count": None, "total_ms": 5000,
             "retries": 3},
        ]
        (row,) = aggregate_runs(runs)
        self.assertEqual((row["runs"], row["failures"], row["success_rate"]), (4, 1, 0.75))
        self.assertEqual(row["final_question_rate"], round(30 / 48, 3))
        self.assertEqual(row["grounding_rate"], round(33 / 42, 3))
        self.assertEqual((row["avg_latency_seconds"], row["p50_latency_seconds"], row["p95_latency_seconds"]), (20.0, 20.0, 29.0))
        self.assertEqual(row["avg_retries"], 1.5)
        self.assertEqual(row["avg_tokens_per_second"], 30.0)
        self.assertEqual(row["questions_per_minute"], 30.0)   # 30 questions in 60 s of successful runs

    def test_a_model_whose_runs_all_failed_has_null_latency_and_throughput(self):
        (row,) = aggregate_runs([{"model_id": "gemma3-12b", "success": False, "requested_count": 12}])
        self.assertEqual((row["success_rate"], row["final_question_rate"]), (0.0, 0.0))
        for key in ("grounding_rate", "avg_latency_seconds", "p50_latency_seconds", "p95_latency_seconds",
                    "avg_retries", "avg_tokens_per_second", "questions_per_minute"):
            self.assertIsNone(row[key])


class BenchmarkRouteTests(_TempStores):
    def setUp(self):
        super().setUp()
        import backend.conversation_store as conversation_store
        import backend.indexed_document_store as indexed_document_store
        root = Path(self.temp.name)
        for patcher in (
            patch.object(auth_store, "DATABASE_PATH", root / "app.db"),
            patch.object(conversation_store, "DATABASE_PATH", root / "app.db"),
            patch.object(indexed_document_store, "DATABASE_PATH", root / "app.db"),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(auth_store, "ADMIN_EMAILS", frozenset({"admin@example.com"})),
            patch.object(model_benchmark_service, "execute_benchmark"),   # never actually runs here
        ):
            patcher.start()
            self.patchers.append(patcher)

    def _signup(self, client, email):
        response = client.post("/api/auth/signup", json={
            "display_name": "Person", "email": email, "password": "long-password-123",
        })
        self.assertEqual(response.status_code, 201)

    def test_non_admin_cannot_start_a_benchmark(self):
        with TestClient(app) as client:
            self._signup(client, "student@example.com")
            response = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": [DOCUMENT["id"]]})
        self.assertEqual(response.status_code, 403)

    def test_admin_starts_a_benchmark_with_default_three_runs(self):
        with TestClient(app) as client:
            self._signup(client, "admin@example.com")
            response = client.post("/api/admin/quiz-model-benchmark", json={
                "document_ids": [DOCUMENT["id"]], "difficulty": "easy", "question_count": 12,
            })
        self.assertEqual(response.status_code, 202)
        config = response.json()["config"]
        self.assertEqual(config["runs_per_model_per_document"], 3)
        self.assertEqual([model["model_id"] for model in config["models"]], ["qwen-2.5-7b", "gemma3-12b"])
        self.assertEqual(response.json()["progress"], {"completed": 0, "total": 6})

    def test_invalid_request_is_400_and_a_second_start_is_409(self):
        with TestClient(app) as client:
            self._signup(client, "admin@example.com")
            bad = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": [DOCUMENT["id"]], "question_count": 13})
            with patch.object(model_benchmark_service, "is_benchmark_running", return_value=True):
                busy = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": [DOCUMENT["id"]]})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(busy.status_code, 409)


if __name__ == "__main__":
    unittest.main()
