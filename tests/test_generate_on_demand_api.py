"""The endpoints behind the Summary/Flashcards "Generate" screens: a lookup never generates."""

import unittest
from unittest.mock import patch

from backend import main

USER = {"id": "user-1"}


class OnDemandEndpointTests(unittest.TestCase):
    def test_summary_lookup_is_cache_only_and_plain_get_still_generates(self):
        with patch.object(main, "generate_document_summary", return_value={"status": "not_generated"}) as service:
            main.summary_detail("doc.pdf", model_id="qwen-2.5-7b", cache_only=True, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b", cache_only=True)
        with patch.object(main, "generate_document_summary", return_value={}) as service:
            main.summary_detail("doc.pdf", model_id="qwen-2.5-7b", current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b")   # the Generate button's request

    def test_flashcards_lookup_is_cache_only_and_plain_get_still_generates(self):
        with patch.object(main, "generate_flashcards", return_value={"status": "not_generated"}) as service:
            main.flashcards_detail("doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", cache_only=True, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", cache_only=True)
        with patch.object(main, "generate_flashcards", return_value={}) as service:
            main.flashcards_detail("doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto", current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", topic_ids=None, model_id="qwen-2.5-7b", language="auto")

    def test_regenerate_summary_is_still_an_explicit_generation(self):
        request = main.SummaryGenerateRequest(model_id="qwen-2.5-7b")
        with patch.object(main, "generate_document_summary", return_value={}) as service:
            main.summary_regenerate("doc.pdf", request, current_user=USER)
            service.assert_called_once_with("user-1", "doc.pdf", model_id="qwen-2.5-7b", regenerate=True)


if __name__ == "__main__":
    unittest.main()
