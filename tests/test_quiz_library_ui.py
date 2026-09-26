"""Real-browser tests for the Quiz Library default view (frontend/app.js in headless Chrome
against a mocked API). Covers only the Quiz Library redesign requested alongside the quiz
persistence fix: the landing header/copy, the card fields for Not Started / In Progress / Completed
quizzes, that switching the selected model never hides a quiz, and the "..." menu's Delete action.
Does not touch the quiz player or the (temporary, unchanged) creation flow.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no Ollama, no
network.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def find_chrome():
    candidates = [os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    return next((path for path in candidates if path and Path(path).exists()), None)


MOCK = r"""
window.APP_CONFIG = { API_BASE_URL: "" };
window.__calls = [];
window.__variants = [
  {quiz_id: "q1", title: "Not Started Quiz", topic_id: "document", topic_name: "", difficulty: "easy",
   question_count: 12, requested_count: 12, status: "complete", model_id: "qwen-2.5-7b", model_name: "Qwen2.5-7B-Instruct",
   assessment_scope: "document", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
   progress_status: "not_started", answered: 0, total: 12, score: null, percentage: null},
  {quiz_id: "q2", title: "In Progress Quiz", topic_id: "document", topic_name: "", difficulty: "medium",
   question_count: 15, requested_count: 15, status: "complete", model_id: "gemma3-12b", model_name: "Gemma 3 12B",
   assessment_scope: "document", created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T01:00:00Z",
   progress_status: "in_progress", answered: 7, total: 15, score: null, percentage: null},
];
window.__history = [{
  attempt_id: "a1", quiz_id: "q3", document_id: "lecture.pdf", title: "Completed Quiz", difficulty: "difficult",
  topic_id: "document", topic_name: "", score: 9, total: 12, percentage: 75, attempt_number: 1,
  completed_at: "2026-01-03T00:00:00Z",
}];
window.__deleted = [];
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  window.__calls.push(method + " " + url);
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [
    {id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: true},
    {id: "gemma3-12b", label: "Gemma 3 12B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) return json({status: "ready", ready: true});
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: window.__variants.length > 0, variants: window.__variants}]);
  if (p === "/api/quiz-history") return json(window.__history);
  if (p === "/api/quiz/lecture.pdf" && method === "GET") return json({document_id: "lecture.pdf", difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"), quiz: null, latest_attempt: null, attempt_summary: null});
  if (p.startsWith("/api/quizzes/") && method === "DELETE") {
    const quizId = decodeURIComponent(p.split("/").pop());
    window.__deleted.push(quizId);
    window.__variants = window.__variants.filter((v) => v.quiz_id !== quizId);
    window.__history = window.__history.filter((a) => a.quiz_id !== quizId);
    return json({deleted: quizId});
  }
  if (p.startsWith("/api/summary/lecture.pdf")) return json({status: "not_generated"});
  if (p.startsWith("/api/flashcards/lecture.pdf")) return json({status: "not_generated", cards: []});
  if (p === "/api/conversations" && method === "POST") return json({id: "c1", title: "New conversation", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p.startsWith("/api/conversations/")) return json({id: "c1", title: "t", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p === "/api/conversations") return json([]);
  if (p === "/api/dashboard") return json({mastery: [], materials: [], summary: {}});
  if (p === "/api/sources") return json([]);
  return json([]);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
window.confirm = () => true;   // headless: no real dialog, always confirm destructive actions
const cardText = (card) => card.textContent;
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(400);

  const header = document.querySelector('[data-session-pane="quiz"] .quiz-landing-header');
  out.header = {
    heading: header?.querySelector("h2")?.textContent, subtitle: header?.querySelector("p")?.textContent,
    buttonText: header?.querySelector("button")?.textContent, visible: header && !header.hidden,
  };

  const cards = () => [...document.getElementById("quiz-history-list").children];
  out.cardCount = cards().length;
  const byTitle = (title) => cards().find((card) => card.querySelector("strong")?.textContent === title);

  const notStarted = byTitle("Not Started Quiz");
  out.notStarted = notStarted ? {
    text: cardText(notStarted), hasStart: [...notStarted.querySelectorAll("button")].some((b) => b.textContent === "Start"),
    hasMenu: Boolean(notStarted.querySelector(".quiz-card-menu")),
  } : null;

  const inProgress = byTitle("In Progress Quiz");
  out.inProgress = inProgress ? {
    text: cardText(inProgress), hasResume: [...inProgress.querySelectorAll("button")].some((b) => b.textContent === "Resume"),
  } : null;

  const completed = byTitle("Completed Quiz");
  out.completed = completed ? {
    text: cardText(completed), hasReview: [...completed.querySelectorAll("button")].some((b) => b.textContent === "Review Answers"),
    hasMenu: Boolean(completed.querySelector(".quiz-card-menu")),
  } : null;

  // Switching the selected model must never hide any quiz from the library.
  const modelSelect = document.getElementById("generation-model-select");
  modelSelect.value = "gemma3-12b"; modelSelect.dispatchEvent(new Event("change", {bubbles: true})); await sleep(200);
  out.afterModelSwitch = { cardCount: cards().length, titles: cards().map((c) => c.querySelector("strong")?.textContent) };
  modelSelect.value = "qwen-2.5-7b"; modelSelect.dispatchEvent(new Event("change", {bubbles: true})); await sleep(200);

  // Delete the "Not Started" quiz via its "..." menu; the other two must remain.
  const menu = byTitle("Not Started Quiz").querySelector(".quiz-card-menu");
  menu.open = true;
  const deleteButton = [...menu.querySelectorAll("button")].find((b) => b.textContent === "Delete");
  deleteButton.click();
  await sleep(300);
  out.afterDelete = {
    cardCount: cards().length, deletedIds: window.__deleted, titles: cards().map((c) => c.querySelector("strong")?.textContent),
  };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizLibraryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_library_ui_"))
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
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=40000", "--dump-dom",
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

    def test_header_shows_the_requested_quiz_library_copy(self):
        header = self.out["header"]
        self.assertTrue(header["visible"])
        self.assertEqual(header["heading"], "Quiz")
        self.assertEqual(header["subtitle"], "Test your knowledge and track your progress")
        self.assertEqual(header["buttonText"], "+ New Quiz")

    def test_all_three_quizzes_are_listed_by_default(self):
        self.assertEqual(self.out["cardCount"], 3)

    def test_not_started_card_shows_required_fields_and_a_start_action(self):
        card = self.out["notStarted"]
        self.assertIsNotNone(card)
        self.assertIn("Not Started", card["text"])
        self.assertIn("easy", card["text"])
        self.assertIn("12 questions", card["text"])
        self.assertIn("Entire Document", card["text"])
        self.assertIn("Generated by Qwen 2.5 7B", card["text"])
        self.assertTrue(card["hasStart"])
        self.assertTrue(card["hasMenu"])

    def test_in_progress_card_shows_answered_progress_and_a_resume_action(self):
        card = self.out["inProgress"]
        self.assertIsNotNone(card)
        self.assertIn("In Progress", card["text"])
        self.assertIn("7 / 15 answered", card["text"])
        self.assertIn("Generated by Gemma 3 12B", card["text"])
        self.assertTrue(card["hasResume"])

    def test_completed_card_shows_score_and_a_review_action(self):
        card = self.out["completed"]
        self.assertIsNotNone(card)
        self.assertIn("Completed", card["text"])
        self.assertIn("75%", card["text"])
        self.assertTrue(card["hasReview"])
        self.assertTrue(card["hasMenu"])

    def test_switching_the_selected_model_never_hides_a_quiz(self):
        after = self.out["afterModelSwitch"]
        self.assertEqual(after["cardCount"], 3)
        self.assertEqual(set(after["titles"]), {"Not Started Quiz", "In Progress Quiz", "Completed Quiz"})

    def test_delete_via_the_menu_removes_only_that_quiz(self):
        after = self.out["afterDelete"]
        self.assertEqual(after["deletedIds"], ["q1"])
        self.assertEqual(after["cardCount"], 2)
        self.assertEqual(set(after["titles"]), {"In Progress Quiz", "Completed Quiz"})


if __name__ == "__main__":
    unittest.main()
