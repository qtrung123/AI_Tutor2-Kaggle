"""Real-browser tests for the Create Quiz sheet/modal (frontend/app.js in headless Chrome against a
mocked API). Covers only the Create Quiz sheet UX: entry points, fields, the Study Session model
summary (never a second model picker), safe dismissal, duplicate-submission blocking, in-sheet
generating/error states, and that success never auto-starts the Quiz Player. Does not touch the
quiz player itself or Flashcards/Summary/Overview/Sidebar.

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
window.__prepareHold = null;      // when set, /api/models/*/prepare waits for it
window.__generateHold = null;     // when set, POST /api/quiz/generate waits for it
window.__generateStatus = "complete";   // "complete" | "partial" | "fail"
window.__libraryVariants = [];    // stateful: reflects what "success closes sheet, refreshes Library,
                                   // new quiz appears at the top" actually needs to observe
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  window.__calls.push(method + " " + url);
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [
    {id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: false},
    {id: "gemma3-12b", label: "Gemma 3 12B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) {
    if (window.__prepareHold) await window.__prepareHold;
    return json({status: "ready", ready: true});
  }
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: window.__libraryVariants.length > 0, variants: window.__libraryVariants}]);
  if (p === "/api/quiz-history") return json([]);
  if (p === "/api/quiz/lecture.pdf" && method === "GET") return json({document_id: "lecture.pdf", difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"), quiz: null, latest_attempt: null, attempt_summary: null});
  if (p === "/api/quiz/generate" && method === "POST") {
    if (window.__generateHold) await window.__generateHold;
    const body = JSON.parse(init.body);
    window.__lastGenerateBody = body;
    if (window.__generateStatus === "fail") {
      return json({detail: "prediction aborted, token repeat limit reached (raw ollama text)"}, 500);
    }
    const total = window.__generateStatus === "partial" ? 8 : body.question_count;
    const quizId = "q-" + window.__calls.length;
    window.__libraryVariants.unshift({
      quiz_id: quizId, title: body.quiz_name, topic_id: "document", topic_name: "", difficulty: body.difficulty,
      question_count: total, requested_count: body.question_count,
      status: window.__generateStatus === "partial" ? "partial" : "complete",
      model_id: body.model_id, model_name: body.model_id, assessment_scope: body.assessment_scope,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
      progress_status: "not_started", answered: 0, total: total, score: null, percentage: null,
    });
    return json({
      document_id: body.document_id, quiz_id: quizId, document_hash: "h", title: body.quiz_name,
      question_count: total, requested_count: body.question_count, actual_count: total,
      status: window.__generateStatus === "partial" ? "partial" : "complete",
      difficulty: body.difficulty, topic_id: "document", topic_name: "", assessment_scope: body.assessment_scope,
      assessment_plan: {}, generation_model: {model_id: body.model_id, name: body.model_id, quantization: "Q4_K_M"},
      created_at: "2026-01-01T00:00:00Z",
      questions: Array.from({length: total}, (_, i) => ({id: i + 1, question: "Q" + (i + 1), options: ["A. a", "B. b", "C. c", "D. d"],
        correct_answer: "A", question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: body.difficulty,
        explanation: "e", source_chunk_ids: []})),
    });
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
const dialogOpen = () => Boolean(document.querySelector(".quiz-create-dialog")?.classList.contains("open"));
const generateCalls = () => window.__calls.filter((c) => c.startsWith("POST /api/quiz/generate"));
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(300);

  // 1) Quiz Library remains the default view; the sheet is closed.
  out.landing = {
    dialogOpen: dialogOpen(), headerVisible: !document.querySelector(".quiz-landing-header")?.hidden,
    isLanding: document.querySelector('[data-session-pane="quiz"]').classList.contains("quiz-landing"),
    emptyCreateButton: Boolean(document.querySelector(".quiz-empty-create-button")),
  };

  // 2) "+ New Quiz" opens the sheet with the expected fields.
  document.querySelector(".quiz-landing-header button").click();
  await sleep(200);
  out.opened = {
    dialogOpen: dialogOpen(),
    hasNameInput: Boolean(quizNameInput), nameValue: quizNameInput.value,
    scopeDisabled: document.getElementById("quiz-scope-select").disabled,
    scopeText: document.getElementById("quiz-scope-select").options[0].text,
    difficultyOptions: [...document.querySelectorAll("#quiz-difficulty-segmented .quiz-segmented-option")].map((b) => b.textContent),
    countOptions: [...document.querySelectorAll("#quiz-count-segmented .quiz-segmented-option")].map((b) => b.textContent),
    noSecondModelSelect: document.querySelectorAll(".quiz-dialog-fields select:not(.visually-hidden):not(#quiz-scope-select)").length,
    modelName: document.querySelector(".quiz-sheet-model-name")?.textContent,
    modelHint: document.querySelector(".quiz-sheet-model-hint")?.textContent,
  };

  // 3) Cancel closes the sheet with no side effects.
  const callsBeforeCancel = window.__calls.length;
  document.querySelector(".quiz-dialog-cancel").click();
  await sleep(100);
  out.afterCancel = { dialogOpen: dialogOpen(), newCalls: window.__calls.length - callsBeforeCancel };

  // 4) Reopen; Generate is disabled while the model is preparing, and the model badge says so.
  document.querySelector(".quiz-landing-header button").click();
  await sleep(100);
  quizNameInput.value = "Midterm";
  // Pick non-default Difficulty/Question count via the segmented buttons, to prove the sheet
  // submits what was actually selected, not just the defaults.
  [...document.querySelectorAll("#quiz-difficulty-segmented .quiz-segmented-option")].find((b) => b.textContent === "Medium").click();
  [...document.querySelectorAll("#quiz-count-segmented .quiz-segmented-option")].find((b) => b.textContent === "15").click();
  // The bootstrap's own silent "prime the selected model" call already marked it ready before
  // __prepareHold existed, so force it back to not-ready to make this prepare call actually hit
  // the network (same technique as the Flashcards/Summary generation-state tests).
  const qwenModel = generationModels.find((m) => m.id === "qwen-2.5-7b");
  if (qwenModel) qwenModel.ready = false;
  let releasePrepare; window.__prepareHold = new Promise((resolve) => { releasePrepare = resolve; });
  let releaseGenerate; window.__generateHold = new Promise((resolve) => { releaseGenerate = resolve; });
  const genButton = document.getElementById("generate-quiz-button");
  // Call the handler directly (exactly what a real click invokes) so the duplicate-submission
  // guard below is exercised regardless of whether the disabled attribute alone would also have
  // blocked a second real click.
  const firstCall = generateAssessmentQuiz();
  await sleep(150);
  out.preparing = {
    generateDisabled: genButton.disabled, badgeText: document.getElementById("quiz-sheet-model-badge").textContent,
    badgeHidden: document.getElementById("quiz-sheet-model-badge").hidden,
    generatingIndicatorHiddenDuringPrepare: document.getElementById("quiz-sheet-generating").hidden,
  };

  // 5) A duplicate invocation while preparing/generating (what a rapid double-click would trigger)
  // must not send a second request.
  const duplicateCall = generateAssessmentQuiz();
  releasePrepare();
  await sleep(150);
  out.generating = {
    generateDisabled: genButton.disabled, cancelDisabled: document.querySelector(".quiz-dialog-cancel").disabled,
    generatingIndicatorVisible: !document.getElementById("quiz-sheet-generating").hidden,
    generateCallsWhileHeld: generateCalls().length,
  };
  // Cancel/backdrop/Escape must be a no-op while generating.
  document.querySelector(".quiz-dialog-cancel").click();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  await sleep(50);
  out.blockedWhileGenerating = { dialogOpen: dialogOpen() };

  releaseGenerate(); await Promise.all([firstCall, duplicateCall]); await sleep(300);
  out.afterSuccess = {
    dialogOpen: dialogOpen(), generateCalls: generateCalls().length,
    requestBody: window.__lastGenerateBody,
    isLanding: document.querySelector('[data-session-pane="quiz"]').classList.contains("quiz-landing"),
    playerHasQuiz: Boolean(currentQuiz), toast: document.getElementById("toast").textContent,
    libraryCardCount: document.getElementById("quiz-history-list").children.length,
  };

  // 6) Partial generation is treated as success (sheet closes, subtle message, no error state).
  window.__generateStatus = "partial";
  document.querySelector(".quiz-landing-header button").click(); await sleep(100);
  quizNameInput.value = "Partial Quiz";
  document.getElementById("generate-quiz-button").click();
  await sleep(300);
  out.afterPartial = {
    dialogOpen: dialogOpen(), toast: document.getElementById("toast").textContent,
    errorVisible: !document.getElementById("quiz-sheet-error").hidden,
  };

  // 7) A generation failure shows a friendly, retryable, in-sheet error -- never the raw text as
  // the primary message -- and the sheet stays open.
  window.__generateStatus = "fail";
  document.querySelector(".quiz-landing-header button").click(); await sleep(100);
  quizNameInput.value = "Failing Quiz";
  document.getElementById("generate-quiz-button").click();
  await sleep(300);
  out.afterFailure = {
    dialogOpen: dialogOpen(), errorVisible: !document.getElementById("quiz-sheet-error").hidden,
    message: document.getElementById("quiz-sheet-error-message").textContent,
    technical: document.getElementById("quiz-sheet-error-technical").textContent,
    hasRetryButton: Boolean(document.getElementById("quiz-sheet-retry-button")),
  };

  // Retry (now succeeding) recovers and closes the sheet.
  window.__generateStatus = "complete";
  const callsBeforeRetry = generateCalls().length;
  document.getElementById("quiz-sheet-retry-button").click();
  await sleep(300);
  out.afterRetry = { dialogOpen: dialogOpen(), newGenerateCalls: generateCalls().length - callsBeforeRetry };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CreateQuizSheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="create_quiz_sheet_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
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

    def test_quiz_library_is_the_default_view_with_the_sheet_closed(self):
        landing = self.out["landing"]
        self.assertFalse(landing["dialogOpen"])
        self.assertTrue(landing["headerVisible"])
        self.assertTrue(landing["isLanding"])
        self.assertTrue(landing["emptyCreateButton"])   # empty state also offers a Create Quiz button

    def test_new_quiz_opens_the_sheet_with_the_expected_fields_and_no_second_model_picker(self):
        opened = self.out["opened"]
        self.assertTrue(opened["dialogOpen"])
        self.assertTrue(opened["hasNameInput"])
        self.assertEqual(opened["nameValue"], "")
        self.assertTrue(opened["scopeDisabled"])
        self.assertEqual(opened["scopeText"], "Entire document")
        self.assertEqual(opened["difficultyOptions"], ["Easy", "Medium", "Difficult"])
        self.assertEqual(opened["countOptions"], ["12", "15", "18", "20"])
        self.assertEqual(opened["noSecondModelSelect"], 0)
        self.assertEqual(opened["modelName"], "Qwen 2.5 7B")
        self.assertIn("Study Session selector", opened["modelHint"])

    def test_cancel_closes_the_sheet_with_no_side_effects(self):
        after_cancel = self.out["afterCancel"]
        self.assertFalse(after_cancel["dialogOpen"])
        self.assertEqual(after_cancel["newCalls"], 0)

    def test_generate_is_disabled_and_model_shows_preparing_while_the_model_prepares(self):
        preparing = self.out["preparing"]
        self.assertTrue(preparing["generateDisabled"])
        self.assertIn("Preparing", preparing["badgeText"])
        self.assertFalse(preparing["badgeHidden"])

    def test_duplicate_clicks_never_send_more_than_one_generate_request(self):
        generating = self.out["generating"]
        self.assertTrue(generating["generateDisabled"])
        self.assertTrue(generating["cancelDisabled"])
        self.assertTrue(generating["generatingIndicatorVisible"])
        self.assertEqual(generating["generateCallsWhileHeld"], 1)   # the one real request, still held -- never a second
        blocked = self.out["blockedWhileGenerating"]
        self.assertTrue(blocked["dialogOpen"])   # Cancel/Escape were both no-ops while generating

        after_success = self.out["afterSuccess"]
        self.assertEqual(after_success["generateCalls"], 1)   # exactly one POST despite two overlapping invocations

    def test_generate_submits_the_selected_name_difficulty_count_scope_and_model(self):
        body = self.out["afterSuccess"]["requestBody"]
        self.assertEqual(body["quiz_name"], "Midterm")
        self.assertEqual(body["difficulty"], "medium")
        self.assertEqual(body["question_count"], 15)
        self.assertEqual(body["assessment_scope"], "document")
        self.assertEqual(body["model_id"], "qwen-2.5-7b")
        self.assertTrue(body["regenerate"])   # explicit Generate always creates a new artifact

    def test_success_closes_the_sheet_refreshes_the_library_and_never_auto_starts_the_player(self):
        after_success = self.out["afterSuccess"]
        self.assertFalse(after_success["dialogOpen"])
        self.assertTrue(after_success["isLanding"])
        self.assertFalse(after_success["playerHasQuiz"])
        self.assertEqual(after_success["toast"], "Quiz created")
        self.assertEqual(after_success["libraryCardCount"], 1)

    def test_partial_generation_is_treated_as_success_with_a_subtle_message(self):
        after_partial = self.out["afterPartial"]
        self.assertFalse(after_partial["dialogOpen"])
        self.assertFalse(after_partial["errorVisible"])
        self.assertIn("8 questions", after_partial["toast"])
        self.assertIn("Grounded quality was prioritized", after_partial["toast"])

    def test_failure_shows_a_friendly_retryable_error_never_the_raw_text_as_the_primary_message(self):
        after_failure = self.out["afterFailure"]
        self.assertTrue(after_failure["dialogOpen"])   # the sheet stays open so the user can retry
        self.assertTrue(after_failure["errorVisible"])
        self.assertTrue(after_failure["hasRetryButton"])
        self.assertNotIn("ollama", after_failure["message"].lower())
        self.assertNotIn("token repeat limit", after_failure["message"])
        self.assertIn("try again or switch models", after_failure["message"].lower())
        self.assertIn("token repeat limit", after_failure["technical"])   # still reachable, just not primary

    def test_retry_recovers_and_closes_the_sheet(self):
        after_retry = self.out["afterRetry"]
        self.assertFalse(after_retry["dialogOpen"])
        self.assertEqual(after_retry["newGenerateCalls"], 1)


if __name__ == "__main__":
    unittest.main()
