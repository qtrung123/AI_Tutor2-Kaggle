import unittest
from unittest.mock import patch

from backend import model_registry


class ModelRegistryTests(unittest.TestCase):
    def test_qwen_public_id_resolves_to_real_runtime_reference(self):
        self.assertEqual(
            model_registry.resolve_generation_model("qwen-2.5-7b"),
            "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        )

    @patch("backend.model_registry._is_installed", return_value=True)
    def test_models_return_only_public_data(self, _installed):
        model = model_registry.list_generation_models()[0]
        self.assertEqual(model["id"], "qwen-2.5-7b")
        self.assertEqual(model["label"], "Qwen 2.5 7B")
        self.assertNotIn("ollama_model", model)

    def test_unknown_public_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not an allowed"):
            model_registry.resolve_generation_model("not-a-model")

    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_prepare_pulls_only_resolved_selected_runtime_model(self, _installed, client_type):
        client = client_type.return_value.__enter__.return_value
        response = client.post.return_value
        response.raise_for_status.return_value = None
        model_registry.prepare_generation_model("qwen-2.5-7b")
        client.post.assert_called_once_with(
            "http://127.0.0.1:11434/api/pull",
            json={"name": "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M", "stream": False},
        )

