"""Tests for the Quiz Model Comparison backend.

Covers: the admin allowlist gate, the benchmark results store, the
model-comparison service (Qwen vs Gemma roster + production/candidate status),
and the API routes (comparison readable by any signed-in user, benchmark start
admin-only). The Model Comparison page itself was removed from the frontend; the
last test class checks that no navigation or page code for it remains.
The benchmark runner itself is covered by tests/test_quiz_model_benchmark.py.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth_store, model_benchmark_store, model_comparison_service
from backend.auth_store import is_admin_email
from backend.main import app
from backend.model_comparison_service import QUIZ_BENCHMARK_MODELS, get_quiz_model_comparison
from frontend_source import frontend_script_text


class IsAdminEmailTests(unittest.TestCase):
    def test_configured_email_is_admin_case_and_whitespace_insensitive(self):
        with patch.object(auth_store, "ADMIN_EMAILS", frozenset({"admin@example.com"})):
            self.assertTrue(is_admin_email("  Admin@Example.com  "))
            self.assertFalse(is_admin_email("student@example.com"))

    def test_empty_allowlist_admits_nobody(self):
        with patch.object(auth_store, "ADMIN_EMAILS", frozenset()):
            self.assertFalse(is_admin_email("anyone@example.com"))


class ModelBenchmarkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_path = Path(self.temp_dir.name) / "quiz_model_benchmark_results.json"
        self.patcher = patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_missing_file_returns_empty_placeholder(self):
        self.assertEqual(model_benchmark_store.load_benchmark_results(), {"generated_at": None, "results": []})

    def test_malformed_json_falls_back_to_empty_placeholder(self):
        self.results_path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(model_benchmark_store.load_benchmark_results(), {"generated_at": None, "results": []})

    def test_save_then_load_roundtrip(self):
        results = [{"model_id": "qwen3-8b", "success_rate": 0.92}]
        model_benchmark_store.save_benchmark_results("2026-09-20T00:00:00+00:00", results)
        loaded = model_benchmark_store.load_benchmark_results()
        self.assertEqual(loaded["generated_at"], "2026-09-20T00:00:00+00:00")
        self.assertEqual(loaded["results"], results)


class ModelComparisonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_path = Path(self.temp_dir.name) / "quiz_model_benchmark_results.json"
        self.patcher = patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_full_roster_is_always_qwen_and_gemma_even_with_no_benchmark_data(self):
        comparison = get_quiz_model_comparison()
        self.assertIsNone(comparison["generated_at"])
        self.assertIsNone(comparison["benchmark_status"])
        self.assertEqual(comparison["runs"], [])
        self.assertEqual(
            [model["model_id"] for model in comparison["models"]], ["qwen-2.5-7b", "gemma3-12b"],
        )
        self.assertTrue(all(model["measured"] is False for model in comparison["models"]))
        self.assertTrue(all(
            model[field] is None for model in comparison["models"]
            for field in model_comparison_service.METRIC_FIELDS
        ))

    def test_measured_model_reports_stored_metrics_others_stay_unmeasured(self):
        model_benchmark_store.save_benchmark_results(
            "2026-09-20T12:00:00+00:00",
            [{
                "model_id": "qwen-2.5-7b", "success_rate": 0.95, "final_question_rate": 0.9,
                "grounding_rate": 0.88, "avg_latency_seconds": 12.4, "avg_retries": 0.6,
                "p95_latency_seconds": 20.1, "questions_per_minute": 4.2,
            }],
        )
        comparison = get_quiz_model_comparison()
        self.assertEqual(comparison["generated_at"], "2026-09-20T12:00:00+00:00")
        by_id = {model["model_id"]: model for model in comparison["models"]}
        self.assertTrue(by_id["qwen-2.5-7b"]["measured"])
        self.assertEqual(by_id["qwen-2.5-7b"]["success_rate"], 0.95)
        self.assertEqual(by_id["qwen-2.5-7b"]["p95_latency_seconds"], 20.1)
        self.assertIsNone(by_id["qwen-2.5-7b"]["avg_tokens_per_second"])   # unavailable -> null
        self.assertFalse(by_id["gemma3-12b"]["measured"])

    def test_current_production_status_follows_configured_quiz_default(self):
        # The status follows whatever the quiz default is; pin it so the test does not depend on it.
        with patch.object(model_comparison_service, "QUIZ_DEFAULT_GENERATION_MODEL", "gemma3-12b"):
            comparison = get_quiz_model_comparison()
        by_id = {model["model_id"]: model["status"] for model in comparison["models"]}
        self.assertEqual(by_id["gemma3-12b"], "current_production")
        self.assertEqual(by_id["qwen-2.5-7b"], "benchmark_candidate")

        with patch.object(model_comparison_service, "QUIZ_DEFAULT_GENERATION_MODEL", "qwen-2.5-7b"):
            comparison = get_quiz_model_comparison()
        by_id = {model["model_id"]: model["status"] for model in comparison["models"]}
        self.assertEqual(by_id["qwen-2.5-7b"], "current_production")
        self.assertEqual(by_id["gemma3-12b"], "benchmark_candidate")

    def test_roster_is_exactly_qwen_vs_gemma_with_no_combined_score(self):
        self.assertEqual([entry["model_id"] for entry in QUIZ_BENCHMARK_MODELS], ["qwen-2.5-7b", "gemma3-12b"])
        # Both benchmark models are active generation models (the benchmark can still run them).
        from backend.model_registry import resolve_generation_model
        for entry in QUIZ_BENCHMARK_MODELS:
            self.assertTrue(resolve_generation_model(entry["model_id"]))
        for forbidden in ("quality_score", "overall_score", "winner", "vram_mb"):
            self.assertNotIn(forbidden, model_comparison_service.METRIC_FIELDS)


class AdminRouteAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database_path = root / "auth.db"
        self.results_path = root / "quiz_model_benchmark_results.json"

        import backend.conversation_store as conversation_store
        import backend.indexed_document_store as indexed_document_store
        from backend import quiz_store

        self.patchers = [
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
            patch.object(quiz_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(auth_store, "ADMIN_EMAILS", frozenset({"admin@example.com"})),
            patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _signup(self, client, email, display_name="Person"):
        response = client.post("/api/auth/signup", json={
            "display_name": display_name, "email": email, "password": "long-password-123",
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_unauthenticated_request_is_rejected(self):
        with TestClient(app) as client:
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 401)

    def test_non_admin_user_can_read_the_comparison(self):
        with TestClient(app) as client:
            self._signup(client, "student@example.com")
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([model["model_id"] for model in body["models"]], ["qwen-2.5-7b", "gemma3-12b"])
        self.assertEqual(body, get_quiz_model_comparison())   # same payload an admin gets

    def test_unauthenticated_benchmark_start_is_rejected(self):
        with TestClient(app) as client:
            response = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": ["doc-1"]})
        self.assertEqual(response.status_code, 401)

    def test_non_admin_cannot_start_a_benchmark(self):
        with patch("backend.main.start_benchmark") as start:
            with TestClient(app) as client:
                self._signup(client, "student@example.com")
                response = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": ["doc-1"]})
        self.assertEqual(response.status_code, 403)
        start.assert_not_called()

    def test_admin_can_start_a_benchmark(self):
        with patch("backend.main.start_benchmark") as start:
            with TestClient(app) as client:
                admin = self._signup(client, "admin@example.com")
                response = client.post("/api/admin/quiz-model-benchmark", json={"document_ids": ["doc-1"]})
        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with(admin["id"], ["doc-1"], "medium", 12, start.call_args.args[4])

    def test_admin_user_receives_full_roster(self):
        with TestClient(app) as client:
            self._signup(client, "admin@example.com")
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["generated_at"])
        self.assertEqual(len(body["models"]), 2)
        self.assertTrue(all(model["measured"] is False for model in body["models"]))

    def test_signup_login_and_me_all_report_is_admin_flag(self):
        with TestClient(app) as client:
            signup_body = self._signup(client, "admin@example.com")
            self.assertTrue(signup_body["is_admin"])
            me_body = client.get("/api/auth/me").json()
            self.assertTrue(me_body["is_admin"])

        with TestClient(app) as client:
            student_body = self._signup(client, "student2@example.com")
            self.assertFalse(student_body["is_admin"])
            login_body = client.post("/api/auth/login", json={
                "email": "student2@example.com", "password": "long-password-123",
            }).json()
            self.assertFalse(login_body["is_admin"])


class FrontendModelComparisonRemovedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.styles = (root / "frontend" / "styles.css").read_text(encoding="utf-8")
        cls.script = frontend_script_text()

    def test_no_sidebar_item_or_view(self):
        for marker in ('id="admin-model-comparison-nav"', 'data-page="model-comparison"',
                       'id="model-comparison-view"', "Model Comparison"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.markup)

    def test_no_page_code_or_api_calls(self):
        for marker in ('"model-comparison"', "admin-model-comparison-nav", "loadQuizModelComparison",
                       "renderQuizModelComparison", "buildBenchmarkForm", "ADMIN_QUIZ_MODEL_COMPARISON_API_URL",
                       "ADMIN_QUIZ_MODEL_BENCHMARK_API_URL", "/api/admin/quiz-model"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.script)

    def test_no_page_only_styles(self):
        for marker in (".model-comparison-", ".benchmark-", ".soft-badge.production", ".soft-badge.candidate"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.styles)


if __name__ == "__main__":
    unittest.main()
