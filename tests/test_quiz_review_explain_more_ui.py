"""Real-browser tests for Quiz Review's "Explain more" (frontend/app.js in headless Chrome against the
mocked API of test_quiz_results_ui). Covers: the saved explanation stays as it was, nothing is sent
to the AI Tutor until the button is clicked, a click sends exactly one message to the existing
document-scoped tutor conversation with this question's context only, and the message shape for
multiple select, fill in the blank and matching questions.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no Ollama, no
network.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, find_chrome
from tests.test_quiz_results_ui import MOCK as RESULTS_MOCK

MOCK = RESULTS_MOCK + r"""
window.__tutorMessages = [];
window.__tutorHold = null;   // when set, the tutor reply waits for it
const resultsFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (/^\/api\/conversations\/[^/]+\/messages$/.test(p) && method === "POST") {
    const body = JSON.parse(init.body);
    window.__tutorMessages.push(body.message);
    if (window.__tutorHold) await window.__tutorHold;
    return json({answer: "Because alpha is defined in section 1.", grounding_status: "grounded", citations: [],
      user_message: {role: "user", content: body.message}, assistant_message: {role: "assistant", content: "ok"}});
  }
  return resultsFetch(input, init);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const historyCard = (title) => [...document.querySelectorAll(".quiz-history-card")].find((c) => c.querySelector("strong")?.textContent === title);
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(400);
  [...historyCard("Quiz X").querySelectorAll("button")].find((b) => b.textContent === "Review Answers").click();
  await sleep(300);
  $("quiz-results-review").click(); await sleep(50);
  $("quiz-review-next").click(); await sleep(50);   // question 2: answered C, correct B
  out.beforeClick = {
    button: $("quiz-review-explain-more")?.textContent, buttonVisible: Boolean($("quiz-review-explain-more")?.getClientRects().length),
    explanation: $("quiz-review-explanation").textContent, tutorMessages: window.__tutorMessages.length,
  };
  let release; window.__tutorHold = new Promise((resolve) => { release = resolve; });
  $("quiz-review-explain-more").click();
  await sleep(100);
  out.whileSending = { label: $("quiz-review-explain-more").textContent, disabled: $("quiz-review-explain-more").disabled };
  release(); await sleep(400);
  out.afterClick = {
    messages: [...window.__tutorMessages], label: $("quiz-review-explain-more").textContent,
    disabled: $("quiz-review-explain-more").disabled, explanation: $("quiz-review-explanation").textContent,
    reviewVisible: !$("quiz-review-view").hidden, position: $("quiz-review-position").textContent,
    tutorAnswer: [...document.querySelectorAll("#message-list .message")].map((m) => m.textContent).join(" | "),
  };

  // Message shape for the other question types (same builder, crafted Review items).
  const base = {options: [], selected: [], correct: [], unanswered: false, isCorrect: false, explanation: "", topicName: "", conceptName: ""};
  out.multi = quizReviewExplainMessage({...base, questionType: "multi_select", question: "Which are prime?",
    options: ["A. 2", "B. 4", "C. 5", "D. 9"], selected: ["A", "B"], correct: ["A", "C"]}, {});
  out.fill = quizReviewExplainMessage({...base, questionType: "fill_blank", question: "The capital of France is ____.",
    selected: ["Lyon"], correct: ["Paris"], explanation: "Paris is the capital."}, {});
  out.matching = quizReviewExplainMessage({...base, questionType: "matching", question: "Match each term.\n1. CPU\n2. RAM",
    options: ["A. memory", "B. processor"], selected: ["A", "B"], correct: ["B", "A"]}, {});
  out.unanswered = quizReviewExplainMessage({...base, questionType: "short_answer", question: "Define latency.",
    unanswered: true, correct: ["delay before transfer"]}, {});

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizReviewExplainMoreUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_explain_more_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000", "--dump-dom",
             f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_the_app_runs_without_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_nothing_is_sent_until_the_button_is_clicked_and_the_explanation_is_unchanged(self):
        before = self.out["beforeClick"]
        self.assertEqual(before["button"], "Explain more")
        self.assertTrue(before["buttonVisible"])
        self.assertEqual(before["tutorMessages"], 0)
        self.assertEqual(before["explanation"], "Beta follows from the definition.")
        self.assertEqual(self.out["afterClick"]["explanation"], "Beta follows from the definition.")

    def test_a_click_sends_one_message_with_this_questions_context_to_the_existing_tutor(self):
        after = self.out["afterClick"]
        self.assertEqual(len(after["messages"]), 1)
        message = after["messages"][0]
        self.assertIn('"Lecture" (lecture.pdf)', message)
        self.assertIn("Question type: Multiple Choice", message)
        self.assertIn("Question: Quiz X question two?", message)
        self.assertIn("My answer: C. gamma", message)
        self.assertIn("Correct answer: B. beta", message)
        self.assertIn("I answered it incorrectly", message)
        self.assertIn("Saved explanation: Beta follows from the definition.", message)
        self.assertIn('topic "Topic 1"', message)
        self.assertIn("Why the correct answer is correct", message)
        self.assertIn("Why my answer is wrong", message)
        self.assertIn("underlying concept", message)
        self.assertNotIn("question one", message)   # only this question, nothing else
        self.assertIn("Because alpha is defined in section 1.", after["tutorAnswer"])
        self.assertTrue(after["reviewVisible"])
        self.assertEqual(after["position"], "Question 2 of 4")

    def test_the_button_is_busy_only_while_sending(self):
        self.assertEqual(self.out["whileSending"], {"label": "Explaining…", "disabled": True})
        self.assertEqual(self.out["afterClick"]["label"], "Explain more")
        self.assertFalse(self.out["afterClick"]["disabled"])

    def test_message_shape_for_other_question_types(self):
        self.assertIn("Question type: Multiple Select", self.out["multi"])
        self.assertIn("My answer: A. 2; B. 4", self.out["multi"])
        self.assertIn("Correct answers: A. 2; C. 5", self.out["multi"])
        self.assertIn('My answer: "Lyon"', self.out["fill"])
        self.assertIn('Correct answer: "Paris"', self.out["fill"])
        self.assertIn("Question: Match each term.", self.out["matching"])
        self.assertIn("1. CPU → A. memory", self.out["matching"])
        self.assertIn("Correct pairs:\n  1. CPU → B. processor\n  2. RAM → A. memory", self.out["matching"])
        self.assertIn("My answer: (no answer)", self.out["unanswered"])
        self.assertIn("I did not answer it", self.out["unanswered"])


if __name__ == "__main__":
    unittest.main()
