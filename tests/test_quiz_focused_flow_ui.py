"""Real-browser tests for the focused Quiz tab flow (frontend/app.js in headless Chrome against the
mocked API of test_quiz_results_ui). Covers: the Quiz tab opening on the Library without loading a
default quiz into the old stacked inline view, Start showing the quiz's actual question-type mix,
Library Retake opening the focused player (Create Quiz/Library hidden), Library Regenerate staying
in the Library, and Results showing the type summary.

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
// Quiz Z is a mixed quiz: its plan says nothing about types, its questions do.
window.__quizzes["quiz-z"].questions[1].question_type = "true_false";
window.__quizzes["quiz-z"].questions[2].question_type = "fill_blank";
window.__quizzes["quiz-z"].questions[2].correct_answers = ["gamma"];
window.__requests = [];
const resultsFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const u = new URL(url, "http://x");
  const p = u.pathname;
  window.__requests.push(method + " " + p + u.search);
  const retake = p.match(/^\/api\/quiz-history\/([^/]+)\/retake$/);
  if (retake) {
    const attempt = window.__attempts[decodeURIComponent(retake[1])];
    return json({quiz: window.__quizzes[attempt.quiz_id], attempt_summary: {attempts: 1, latest_score: 100, best_score: 100, average_score: 100}});
  }
  if (/^\/api\/quiz\/[^/]+\/regenerate$/.test(p) && method === "POST") {
    return json(makeQuiz("quiz-y2", "Quiz Y", "qwen-2.5-7b", "Qwen 2.5 7B", false));
  }
  const quizMatch = p.match(/^\/api\/quiz\/([^/]+)$/);
  if (quizMatch && method === "GET" && !u.searchParams.get("quiz_id")) {   // the document's default quiz
    return json({document_id: quizMatch[1], quiz: window.__quizzes["quiz-x"], latest_attempt: null, attempt_summary: null});
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
const pane = () => document.querySelector('[data-session-pane="quiz"]');
const shown = (el) => Boolean(el) && el.getClientRects().length > 0;
const historyCard = (title) => [...document.querySelectorAll(".quiz-history-card")].find((c) => c.querySelector("strong")?.textContent === title);
const buttonOn = (card, text) => [...card.querySelectorAll("button")].find((b) => b.textContent === text);
const savedCard = (title) => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === title);
const flowState = () => ({
  playerOpen: pane().classList.contains("quiz-player-open"), isLanding: pane().classList.contains("quiz-landing"),
  libraryShown: shown(document.querySelector(".quiz-history-panel")),
  createFormShown: shown(document.querySelector('[data-session-pane="quiz"] .assessment-control')),
  legacyInline: $("quiz-list").querySelectorAll(".quiz-question-card, .quiz-completed-review").length > 0 || pane().classList.contains("quiz-active"),
  quizId: currentQuiz?.quiz_id || null, questionView: !$("quiz-player-question-view").hidden,
  position: $("quiz-player-position").textContent, types: $("quiz-player-types").textContent,
});
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(400);
  out.landing = { ...flowState(), defaultQuizRequested: window.__requests.some((r) => /^GET \/api\/quiz\/lecture\.pdf(\?(?!.*quiz_id=)|$)/.test(r)) };

  savedCard("Quiz Z").querySelector(".quiz-history-actions button").click();
  await sleep(300);
  out.startZ = flowState();
  $("quiz-player-exit").click(); await sleep(300);
  out.afterExit = flowState();

  buttonOn(historyCard("Quiz Y"), "Retake Quiz").click();
  await sleep(300);
  out.retakeY = flowState();
  $("quiz-player-exit").click(); await sleep(300);
  out.afterRetakeExit = flowState();

  const before = window.__requests.length;
  buttonOn(historyCard("Quiz Y"), "Regenerate Quiz").click();
  await sleep(500);
  out.regenerateY = { ...flowState(), regenerateCalls: window.__requests.slice(before).filter((r) => r.includes("/regenerate")).length,
    toast: $("toast").textContent };

  buttonOn(historyCard("Quiz Y"), "Review Answers").click();
  await sleep(300);
  out.resultsY = { playerOpen: flowState().playerOpen, types: $("quiz-results-types").hidden ? "" : $("quiz-results-types").textContent };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizFocusedFlowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_focused_"))
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

    def test_quiz_tab_opens_on_the_library_without_loading_a_default_quiz(self):
        landing = self.out["landing"]
        self.assertTrue(landing["isLanding"])
        self.assertTrue(landing["libraryShown"])
        self.assertFalse(landing["createFormShown"])
        self.assertFalse(landing["legacyInline"])
        self.assertIsNone(landing["quizId"])
        self.assertFalse(landing["defaultQuizRequested"])

    def test_start_gives_the_player_the_main_space_and_shows_the_actual_type_mix(self):
        start = self.out["startZ"]
        self.assertTrue(start["playerOpen"])
        self.assertFalse(start["libraryShown"])
        self.assertFalse(start["createFormShown"])
        self.assertEqual(start["quizId"], "quiz-z")
        self.assertEqual(start["types"], "2 Multiple Choice · 1 True/False · 1 Fill in the Blank")
        self.assertFalse(self.out["afterExit"]["playerOpen"])
        self.assertTrue(self.out["afterExit"]["libraryShown"])

    def test_library_retake_opens_the_focused_player_at_question_one(self):
        retake = self.out["retakeY"]
        self.assertTrue(retake["playerOpen"])
        self.assertTrue(retake["questionView"])
        self.assertFalse(retake["libraryShown"])
        self.assertFalse(retake["createFormShown"])
        self.assertEqual(retake["quizId"], "quiz-y")
        self.assertEqual(retake["position"], "Question 1 of 4")
        self.assertEqual(retake["types"], "Multiple Choice")
        after = self.out["afterRetakeExit"]
        self.assertFalse(after["playerOpen"])
        self.assertTrue(after["libraryShown"])
        self.assertFalse(after["legacyInline"])

    def test_library_regenerate_stays_in_the_library(self):
        regenerate = self.out["regenerateY"]
        self.assertEqual(regenerate["regenerateCalls"], 1)
        self.assertFalse(regenerate["playerOpen"])
        self.assertTrue(regenerate["libraryShown"])
        self.assertFalse(regenerate["legacyInline"])
        self.assertIsNone(regenerate["quizId"])
        self.assertEqual(regenerate["toast"], "Quiz regenerated")

    def test_results_show_the_type_summary(self):
        self.assertTrue(self.out["resultsY"]["playerOpen"])
        self.assertEqual(self.out["resultsY"]["types"], "Multiple Choice")


if __name__ == "__main__":
    unittest.main()
