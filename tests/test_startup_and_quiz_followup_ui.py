"""Real-browser regression tests for two asynchronous paths (headless Chrome, existing mocks):

- Start-up reads run in parallel, so a Study Session can be opened before the chat list arrives; it
  must still open the document's existing chat (not an empty "New chat") and create nothing.
- After a successful quiz submission the results are shown at once; the Library / history / Home
  refresh and the study-plan follow-up run behind them, and a failure there is never reported as a
  failed submission.
Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import MOCK as QUIZ_MOCK, find_chrome
from tests.test_tutor_conversations_ui import CONVERSATION_MOCK
import tests.test_sidebar_navigation_ui as harness

HEADER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
"""

# The chat list answers late (after documents and Home), as on a slow connection.
SLOW_LIST_MOCK = CONVERSATION_MOCK + r"""
const listFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/conversations" && (init.method || "GET").toUpperCase() === "GET" && !window.__listServed) {
    window.__listServed = true;
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  return listFetch(input, init);
};
"""

SLOW_LIST_DRIVER = HEADER + r"""
(async () => {
  await sleep(400);   // documents are in, the chat list is not
  out.listLoadedWhenOpening = conversations.length > 0;
  await openStudySession("lecture.pdf", "overview");
  await sleep(600);
  out.session = {activeId: activeConversation?.id || null, header: $("chat-conversation-title").textContent,
    messages: [...$("message-list").querySelectorAll(".message-body")].map((el) => el.textContent),
    created: window.__requests.filter((r) => r.method === "POST" && r.path === "/api/conversations").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

QUIZ_DRIVER = HEADER + r"""
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz"); await sleep(300);
  await openQuizPlayer({document_id: "lecture.pdf", topic_id: "document", difficulty: "easy", quiz_id: "quiz-a"}); await sleep(300);
  document.querySelector("#quiz-player-answers button").click(); await sleep(700);
  // Every follow-up step fails after the submission succeeds.
  window.loadDashboard = async () => { throw new Error("dashboard is down"); };
  window.loadQuizHistory = async () => { throw new Error("history is down"); };
  window.plannerAfterQuizSubmission = async () => { throw new Error("planner is down"); };
  const toasts = [];
  new MutationObserver(() => toasts.push($("toast").textContent)).observe($("toast"), {childList: true, characterData: true, subtree: true});
  await submitQuizPlayer(); await sleep(600);
  out.quiz = {submitCalls: window.__submitCalls, resultsShown: !$("quiz-results-view").hidden, score: $("quiz-results-score").textContent,
    playerOpen: !$("quiz-player").hidden, finishBusy: $("quiz-player-next").disabled, toasts,
    unhandled: out.errors.filter((e) => e.startsWith("rejection")).length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run(mock, driver, width=1280, height=900):
    original = (harness.MOCK, harness.DRIVER)
    harness.MOCK, harness.DRIVER = mock, driver
    try:
        return harness.run_at_width(width, height)
    finally:
        harness.MOCK, harness.DRIVER = original


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SessionOpenedBeforeTheChatListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(SLOW_LIST_MOCK, SLOW_LIST_DRIVER)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_the_documents_existing_chat_opens_once_the_list_arrives(self):
        self.assertFalse(self.out["listLoadedWhenOpening"])   # the race really happened
        session = self.out["session"]
        self.assertEqual((session["activeId"], session["header"]), ("c-exam", "Exam prep"))
        self.assertEqual(session["messages"], ["What is an ISR?", "An ISR handles interrupts."])
        self.assertEqual(session["created"], 0)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizFollowUpFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(QUIZ_MOCK, QUIZ_DRIVER)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["quiz"]["unhandled"], 0)

    def test_a_successful_submission_shows_its_results_even_when_follow_ups_fail(self):
        quiz = self.out["quiz"]
        self.assertEqual(quiz["submitCalls"], 1)
        self.assertTrue(quiz["resultsShown"] and quiz["playerOpen"])
        self.assertTrue(quiz["score"])
        self.assertFalse(quiz["finishBusy"])
        self.assertFalse([text for text in quiz["toasts"] if "submit" in text.lower() or "down" in text])
