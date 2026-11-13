"""The test run never reads the developer's git-ignored .env.local (see tests/conftest.py).

A local model setup there (for example OLLAMA_GENERATION_MODELS=qwen-2.5-3b) once broke the model
registry tests and the collection of test_quiz_model_benchmark.py. Real environment variables still
apply; only that one file is skipped, and any other dotenv file still loads normally.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dotenv

import config
from conftest import ENV_LOCAL_PATH

DEFAULT_CHAT_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"


class EnvLocalIsolationTests(unittest.TestCase):
    def test_the_skipped_file_is_the_repositorys_env_local(self):
        self.assertEqual(ENV_LOCAL_PATH, (Path(config.BASE_DIR) / ".env.local").resolve())

    def test_loading_env_local_is_a_no_op_during_tests(self):
        key = "AI_TUTOR_ISOLATION_PROBE"
        with tempfile.TemporaryDirectory() as directory:   # a stand-in, so the real .env.local is never read
            fake_env_local = Path(directory) / ".env.local"
            fake_env_local.write_text(f"{key}=from-a-file\n", encoding="utf-8")
            with patch("conftest.ENV_LOCAL_PATH", fake_env_local.resolve()), patch.dict(os.environ):
                os.environ.pop(key, None)
                self.assertFalse(dotenv.load_dotenv(fake_env_local, override=False))
                self.assertNotIn(key, os.environ)

    def test_any_other_dotenv_file_still_loads(self):
        key = "AI_TUTOR_ISOLATION_PROBE"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop(key, None)
            other = Path(directory) / "other.env"
            other.write_text(f"{key}=loaded\n", encoding="utf-8")
            self.assertTrue(dotenv.load_dotenv(other, override=False))
            self.assertEqual(os.environ[key], "loaded")

    def test_model_settings_come_from_the_real_environment_or_the_committed_defaults(self):
        self.assertEqual(config.CHAT_MODEL, os.environ.get("OLLAMA_CHAT_MODEL", DEFAULT_CHAT_MODEL))
        self.assertEqual(config.DEFAULT_GENERATION_MODEL, os.environ.get("OLLAMA_DEFAULT_GENERATION_MODEL", "qwen-2.5-7b"))
        if "OLLAMA_GENERATION_MODELS" not in os.environ and config.LLM_BACKEND != "vllm":
            self.assertEqual(config.GENERATION_MODELS, ("qwen-2.5-7b", "gemma3-12b"))


if __name__ == "__main__":
    unittest.main()
