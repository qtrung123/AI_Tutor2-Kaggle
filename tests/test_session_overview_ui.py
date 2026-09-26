"""Real-browser tests for the Study Session Overview tab (headless Chrome, mocked API reused from
tests/test_quiz_results_ui.py). Covers the empty state, statuses from real saved state, Resume by
exact quiz_id, navigation to each tool, and that Overview never triggers any generation.
Skipped when Chrome is not installed.
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

# Wraps the Results mock: summary/flashcards saved state, one in-progress quiz, and a call log.
MOCK = RESULTS_MOCK + r"""
window.__calls = [];
window.__summaryReady = false;
window.__flashcardCount = 0;
window.__inProgress = {};
window.__allQuizzes = window.__quizzes; window.__allAttempts = window.__attempts;
window.__attempts["att-y1"].completed_at = "2026-01-03T00:00:00Z";
const resultsFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  window.__calls.push(method + " " + url);
  const p = new URL(url, "http://x").pathname;
  if (p.startsWith("/api/summary/")) return json(window.__summaryReady ? {status: "ready", final_summary: {overview: "Overview text.", topics: []}} : {status: "not_generated"});
  if (p.startsWith("/api/flashcards/")) return json(window.__flashcardCount
    ? {status: "ready", cards: Array.from({length: window.__flashcardCount}, (_, i) => ({id: i + 1, front: "F" + i, back: "B" + i, topic_id: "t1", topic_name: "Topic 1"}))}
    : {status: "not_generated", cards: []});
  if (p === "/api/quizzes") {
    const data = await (await resultsFetch(input, init)).json();
    (data[0]?.variants || []).forEach((variant) => {
      const progress = window.__inProgress[variant.quiz_id];
      if (progress) Object.assign(variant, {progress_status: "in_progress", answered: progress, total: 4, updated_at: "2026-01-04T00:00:00Z"});
    });
    return json(data);
  }
  return resultsFetch(input, init);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const root = () => document.getElementById("session-overview");
const tool = (name) => root().querySelector(`[data-overview-tool="${name}"]`);
const snapshot = () => ({
  tab: document.body.dataset.sessionTab,
  paneActive: document.querySelector('[data-session-pane="overview"]').classList.contains("active"),
  title: root().querySelector(".overview-title")?.textContent,
  model: root().querySelector(".overview-model")?.textContent,
  continueHidden: root().querySelector(".overview-continue")?.hidden,
  continueTitle: root().querySelector(".overview-continue-title")?.textContent,
  continueDetail: root().querySelector(".overview-continue-detail")?.textContent,
  tools: Object.fromEntries([...root().querySelectorAll(".overview-tool")].map((b) => [b.dataset.overviewTool, b.querySelector(".overview-tool-status").textContent])),
  stats: Object.fromEntries([...root().querySelectorAll(".overview-stat")].map((t) => [t.querySelector("span").textContent, t.querySelector("strong").textContent])),
  noOverflow: root().scrollWidth <= root().clientWidth + 1,
});
(async () => {
  await sleep(1500);
  // 1) Empty state: nothing generated, no quizzes.
  window.__quizzes = {}; window.__attempts = {};
  await loadQuizStatuses();
  await openStudySession("lecture.pdf");
  await sleep(300);
  out.empty = snapshot();

  // 2) Real saved state: summary, 12 flashcards, 3 quizzes (2 completed, 1 in progress).
  window.__quizzes = window.__allQuizzes; window.__attempts = window.__allAttempts;
  window.__summaryReady = true; window.__flashcardCount = 12; window.__inProgress = {"quiz-z": 2};
  await loadQuizStatuses();
  await openStudySession("lecture.pdf");
  await sleep(300);
  out.populated = snapshot();

  // 3) Each tool navigates to its existing tab.
  out.nav = {};
  for (const name of ["summary", "flashcards", "quiz", "tutor"]) {
    setSessionTab("overview"); await sleep(150);
    tool(name).click(); await sleep(150);
    out.nav[name] = { tab: document.body.dataset.sessionTab, chatFocused: document.activeElement?.id === "chat-input" };
  }

  // 4) Resume opens the Quiz Player for the exact in-progress quiz_id.
  setSessionTab("overview"); await sleep(200);
  root().querySelector(".overview-resume-button").click();
  await sleep(400);
  out.resume = {
    tab: document.body.dataset.sessionTab,
    playerOpen: document.querySelector('[data-session-pane="quiz"]').classList.contains("quiz-player-open"),
    playerTitle: document.getElementById("quiz-player-title").textContent,
    quizRequest: window.__calls.filter((c) => c.startsWith("GET /api/quiz/lecture.pdf?")).at(-1) || "",
  };
  out.calls = window.__calls;

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SessionOverviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="session_overview_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")   # app.js's classic-script modules
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

    def test_overview_is_the_session_landing_tab_with_an_honest_empty_state(self):
        empty = self.out["empty"]
        self.assertEqual(empty["tab"], "overview")
        self.assertTrue(empty["paneActive"])
        self.assertEqual(empty["title"], "Lecture")
        self.assertEqual(empty["model"], "Model: Qwen 2.5 7B")
        self.assertTrue(empty["continueHidden"])   # nothing in progress -> no invented recommendation
        self.assertEqual(empty["tools"], {
            "tutor": "Ask about this document", "summary": "Not generated",
            "flashcards": "Not generated", "quiz": "No quizzes yet",
        })
        self.assertEqual(empty["stats"], {
            "Quizzes completed": "0", "In progress": "0", "Latest score": "—", "Flashcards": "—", "Summary": "Not yet",
        })

    def test_statuses_come_from_real_saved_state(self):
        populated = self.out["populated"]
        self.assertEqual(populated["tools"]["summary"], "Generated")
        self.assertEqual(populated["tools"]["flashcards"], "12 cards")
        self.assertEqual(populated["tools"]["quiz"], "3 quizzes · 1 in progress · 2 completed")
        self.assertEqual(populated["stats"], {
            "Quizzes completed": "2", "In progress": "1", "Latest score": "4 / 4", "Flashcards": "12", "Summary": "Available",
        })
        self.assertTrue(populated["noOverflow"])

    def test_continue_studying_shows_the_in_progress_quiz(self):
        populated = self.out["populated"]
        self.assertFalse(populated["continueHidden"])
        self.assertEqual(populated["continueTitle"], "Quiz Z")
        self.assertEqual(populated["continueDetail"], "2 / 4 answered")

    def test_resume_opens_the_exact_quiz_id_in_the_player(self):
        resume = self.out["resume"]
        self.assertEqual(resume["tab"], "quiz")
        self.assertTrue(resume["playerOpen"])
        self.assertEqual(resume["playerTitle"], "Quiz Z")
        self.assertIn("quiz_id=quiz-z", resume["quizRequest"])

    def test_each_tool_navigates_to_its_existing_tab(self):
        nav = self.out["nav"]
        self.assertEqual(nav["summary"]["tab"], "summary")
        self.assertEqual(nav["flashcards"]["tab"], "flashcards")
        self.assertEqual(nav["quiz"]["tab"], "quiz")
        self.assertEqual(nav["tutor"], {"tab": "material", "chatFocused": True})

    def test_overview_never_triggers_generation(self):
        calls = self.out["calls"]
        self.assertFalse([c for c in calls if c.startswith("POST ") and re.search(r"/api/(summary|flashcards|quiz/generate)", c)])
        reads = [c for c in calls if re.match(r"GET /api/(summary|flashcards)/", c)]
        self.assertTrue(reads)
        self.assertTrue(all("cache_only=true" in c for c in reads), reads)


if __name__ == "__main__":
    unittest.main()
