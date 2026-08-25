"""Regression coverage for the persistent Study Session AI Tutor submit contract."""

import re
import unittest
from pathlib import Path


SCRIPT = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(rf"async function {name}\([^)]*\) \{{", SCRIPT)
    if not match:
        raise AssertionError(f"{name} was not found")
    start = match.end()
    depth = 1
    for index in range(start, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start:index]
    raise AssertionError(f"{name} is not balanced")


class PersistentTutorSubmitTests(unittest.TestCase):
    def test_submit_posts_public_model_id_after_conversation_preflight(self):
        body = function_body("requestTutorAnswer")
        self.assertIn("await ensureStudySessionConversation()", body)
        self.assertIn("await ensureSelectedModelReady()", body)
        self.assertIn("/messages", body)
        self.assertIn("method: \"POST\"", body)
        self.assertIn("model_id: modelId", body)
        self.assertNotIn("ollama_model", body)

    def test_unready_model_is_prepared_and_prepare_errors_are_actionable(self):
        body = function_body("ensureSelectedModelReady")
        self.assertIn("/prepare", body)
        self.assertIn("model.ready = true", body)
        self.assertIn("could not be prepared", body)

    def test_submit_surfaces_errors_after_the_user_message_renders(self):
        body = function_body("handleChatSubmit")
        self.assertIn("addMessage(userText, \"user\")", body)
        self.assertIn('const submitButton = chatForm.querySelector("button")', body)
        self.assertIn("await requestTutorAnswer(userText)", body)
        self.assertIn("console.error", body)
        self.assertIn("RAG request failed", body)
        self.assertIn("addMessage(`I could not reach the RAG answer", body)

    def test_tab_switches_only_change_visible_panes(self):
        body = re.search(r"function setSessionTab\(tab\) \{([\s\S]*?)\n}\n", SCRIPT).group(1)
        self.assertIn("classList.toggle(\"active\"", body)
        self.assertNotIn("createChatConversation", body)
        self.assertNotIn("activeConversation = null", body)

    def test_session_conversation_is_loaded_by_document_without_reassignment(self):
        body = function_body("ensureStudySessionConversation")
        self.assertIn("conversations.find", body)
        self.assertIn("await openConversation(existing.id)", body)
        self.assertIn("document_ids: documentIds", body)
        self.assertNotIn("/sources", body)
        self.assertNotIn("method: \"PUT\"", body)


if __name__ == "__main__":
    unittest.main()
