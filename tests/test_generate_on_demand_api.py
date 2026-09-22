"""The endpoints behind the Summary/Flashcards "Generate" screens: a lookup never generates."""

import unittest
from unittest.mock import patch

from backend import main

USER = {"id": "user-1"}


class OnDemandEndpointTests(unittest.TestCase):
    def test_summary_lookup_is_cache_only_and_plain_get_still_generates(self):
        with patch.object(main, "generate_document_summary", return_value={"status": "not_generated"}) as service, \
             patch.object(main, "prepare_generation_model") as prepare:
            main.summary_detail("doc.pdf", model_id="qwen-2.5-7b", cache_only=True, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b", cache_only=True)
            prepare.assert_not_called()   # a pure existence check never prepares/pulls anything
        with patch.object(main, "generate_document_summary", return_value={}) as service, \
             patch.object(main, "prepare_generation_model") as prepare:
            main.summary_detail("doc.pdf", model_id="qwen-2.5-7b", current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b")   # the Generate button's request
            prepare.assert_called_once_with("qwen-2.5-7b")

    def test_flashcards_lookup_is_cache_only_and_plain_get_still_generates(self):
        with patch.object(main, "generate_flashcards", return_value={"status": "not_generated"}) as service, \
             patch.object(main, "prepare_generation_model") as prepare:
            main.flashcards_detail("doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", cache_only=True, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", cache_only=True)
            prepare.assert_not_called()   # a pure existence check never prepares/pulls anything
        with patch.object(main, "generate_flashcards", return_value={}) as service, \
             patch.object(main, "prepare_generation_model") as prepare:
            main.flashcards_detail("doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto")
            prepare.assert_called_once_with("qwen-2.5-7b")

    def test_regenerate_summary_is_still_an_explicit_generation(self):
        request = main.SummaryGenerateRequest(model_id="qwen-2.5-7b")
        with patch.object(main, "generate_document_summary", return_value={}) as service, \
             patch.object(main, "prepare_generation_model") as prepare:
            main.summary_regenerate("doc.pdf", request, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b", regenerate=True)
            prepare.assert_called_once_with("qwen-2.5-7b")

    def test_a_lazily_pulled_model_is_prepared_before_generation_and_never_silently_swapped(self):
        """Selecting a lazy model (Gemma/GLM/DeepSeek) and asking to generate prepares exactly that
        model - never Qwen - and a failed preparation aborts the request instead of generating
        anyway with a different model."""
        for endpoint_call in (
            lambda: main.summary_detail("doc.pdf", model_id="gemma3-12b", current_user=USER),
            lambda: main.flashcards_detail("doc.pdf", topic_ids=None, model_id="glm4-9b", language="auto", current_user=USER),
        ):
            with patch.object(main, "generate_document_summary", return_value={}), \
                 patch.object(main, "generate_flashcards", return_value={}), \
                 patch.object(main, "prepare_generation_model", side_effect=ValueError("boom")) as prepare:
                with self.assertRaises(Exception):
                    endpoint_call()
                self.assertTrue(prepare.called)


if __name__ == "__main__":
    unittest.main()
