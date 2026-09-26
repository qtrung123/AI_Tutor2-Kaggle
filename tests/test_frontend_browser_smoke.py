"""Real-browser smoke test: the actual frontend/app.js in headless Chrome against a mocked API.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no Ollama, no network.
It walks the flows of the Quiz / Summary / Flashcards redesign end to end.
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
window.__hold = null;          // when set, POST /api/quiz/generate waits for it
const QUESTIONS = Array.from({length: 12}, (_, i) => ({id: i + 1, question: "Q" + (i + 1), options: ["A. a", "B. b", "C. c", "D. d"],
  correct_answer: "A", question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: []}));
const QWEN = {model_id: "qwen-2.5-7b", name: "Qwen2.5-7B-Instruct", quantization: "Q4_K_M"};
window.__variants = [{quiz_id: "q1", title: "Midterm", topic_id: "document", topic_name: "", difficulty: "easy", question_count: 12,
  requested_count: 12, status: "complete", model_id: "qwen-2.5-7b", model_name: "Qwen2.5-7B-Instruct"}];
window.__savedQuiz = () => ({quiz_id: "q1", document_id: "lecture.pdf", title: "Midterm", difficulty: "easy", topic_id: "document",
  topic_name: "", assessment_scope: "document", question_count: 12, requested_count: 12, actual_count: 12, status: "complete",
  assessment_plan: {requested_count: 12, actual_count: 12, type_distribution: {single_choice: 12}, generation_model: QWEN},
  generation_model: QWEN, created_at: "2026-01-01T00:00:00Z", questions: QUESTIONS});
window.__summaries = {};       // model -> summary
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
    {id: "deepseek-r1-14b", label: "DeepSeek R1 Distill Qwen 14B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) return json({status: "ready", ready: true});
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: window.__variants.length > 0, variants: window.__variants}]);
  if (p === "/api/quiz-history") return json([]);
  if (p === "/api/quiz/lecture.pdf" && method === "GET") {
    const topic = u.searchParams.get("topic_id"), difficulty = u.searchParams.get("difficulty");
    const has = window.__variants.some((v) => v.topic_id === topic && v.difficulty === difficulty);
    return json({document_id: "lecture.pdf", difficulty, topic_id: topic, quiz: has ? window.__savedQuiz() : null, latest_attempt: null, attempt_summary: null});
  }
  if (p === "/api/quiz/generate") {
    if (window.__hold) await window.__hold;
    const body = JSON.parse(init.body);
    const quiz = {...window.__savedQuiz(), quiz_id: "q2", title: body.quiz_name, difficulty: body.difficulty,
      generation_model: {model_id: body.model_id, name: body.model_id, quantization: "Q4_K_M"}};
    window.__variants.push({quiz_id: "q2", title: body.quiz_name, topic_id: "document", topic_name: "", difficulty: body.difficulty, question_count: 9,
      requested_count: body.question_count, status: "partial", model_id: body.model_id, model_name: body.model_id});
    return json({...quiz, question_count: 9, actual_count: 9, questions: QUESTIONS.slice(0, 9),
      assessment_plan: {...quiz.assessment_plan, partial: true, actual_count: 9, requested_count: body.question_count}});
  }
  if (p.startsWith("/api/summary/lecture.pdf")) {
    const model = u.searchParams.get("model_id");
    if (u.searchParams.get("cache_only") === "true") return json(window.__summaries[model] || {status: "not_generated", model_id: model});
    window.__summaries[model] = {final_summary: {overview: "Overview by " + model, key_takeaways: []}, topic_summaries: [], model_id: model, cache_hit: false};
    return json(window.__summaries[model]);
  }
  if (p.startsWith("/api/flashcards/lecture.pdf")) {
    if (u.searchParams.get("cache_only") === "true") return json({status: "not_generated", cards: []});
    return json({set_id: "s1", cards: [{flashcard_id: "c1", front: "F", back: "B", topic_id: "t1", topic_name: "Topic 1", is_favorite: false}]});
  }
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
const visible = (el) => Boolean(el) && !el.hidden;
const summaryCalls = () => window.__calls.filter((c) => c.includes("/api/summary/"));
(async () => {
  await sleep(1500);
  const modelSelectors = () => [...document.querySelectorAll("select")].filter((s) => /model/i.test(s.id + " " + s.className));
  out.bootstrap = {appShellVisible: !document.getElementById("app-shell").hidden,
    modelSelectors: modelSelectors().map((s) => s.id || s.className),
    headerOptions: [...document.getElementById("generation-model-select").options].map((o) => o.text), selected: selectedModelId};
  await openStudySession("lecture.pdf", "summary");
  await sleep(300);
  out.summaryOpened = {prompt: visible(summaryGenerate), regenerate: visible(regenerateSummaryButton), content: summaryContent.children.length,
    calls: summaryCalls(), selectsInSummary: document.querySelectorAll('[data-session-pane="summary"] select').length,
    note: document.querySelector("#summary-generate .generate-model-note").textContent};
  generateSummaryButton.click(); await sleep(300);
  out.summaryGenerated = {prompt: visible(summaryGenerate), regenerate: visible(regenerateSummaryButton), text: summaryContent.textContent, calls: summaryCalls()};
  // change model in the header selector while on Summary
  const header = document.getElementById("generation-model-select");
  header.value = "deepseek-r1-14b"; header.dispatchEvent(new Event("change", {bubbles: true})); await sleep(400);
  out.afterModelChange = {selected: selectedModelId, prompt: visible(summaryGenerate), content: summaryContent.children.length,
    newCalls: summaryCalls().slice(out.summaryGenerated.calls.length), note: document.querySelector("#summary-generate .generate-model-note").textContent};
  // Flashcards
  setSessionTab("flashcards"); await sleep(300);
  out.flashcardsOpened = {prompt: visible(flashcardsGenerate), stage: visible(flashcardsStage), selectsInFlashcards: document.querySelectorAll("#flashcards-generate select").length,
    note: document.querySelector("#flashcards-generate .generate-model-note").textContent, calls: window.__calls.filter((c) => c.includes("/api/flashcards/"))};
  generateFlashcardsButton.click(); await sleep(300);
  out.flashcardsGenerated = {prompt: visible(flashcardsGenerate), stage: visible(flashcardsStage), calls: window.__calls.filter((c) => c.includes("/api/flashcards/")).length};
  // Quiz: saved marks follow model + count
  header.value = "qwen-2.5-7b"; header.dispatchEvent(new Event("change", {bubbles: true})); await sleep(300);
  setSessionTab("quiz"); await sleep(300);
  const easy = () => [...quizDifficultySelect.options].find((o) => o.value === "easy").textContent;
  out.quizSaved = {qwen12: null, quizOnScreen: Boolean(currentQuiz), title: assessmentTitle.textContent};
  quizScopeSelect.value = "document"; await loadSelectedQuiz(); await sleep(200);
  out.quizSaved.qwen12 = easy(); out.quizSaved.title = assessmentTitle.textContent;
  header.value = "deepseek-r1-14b"; header.dispatchEvent(new Event("change", {bubbles: true})); await sleep(300);
  out.quizSaved.deepseek12 = easy(); out.quizSaved.titleAfterSwitch = assessmentTitle.textContent;
  header.value = "qwen-2.5-7b"; header.dispatchEvent(new Event("change", {bubbles: true})); await sleep(200);
  quizQuestionCountSelect.value = "15"; quizQuestionCountSelect.dispatchEvent(new Event("change")); await sleep(100);
  out.quizSaved.qwen15 = easy();
  quizQuestionCountSelect.value = "12"; quizQuestionCountSelect.dispatchEvent(new Event("change"));
  out.quizForm = {labels: [...document.querySelectorAll(".assessment-form label span")].map((s) => s.textContent),
                  selects: [...document.querySelectorAll(".assessment-form select")].map((s) => s.id),
                  scopeOptions: [...quizScopeSelect.options].map((o) => o.textContent),
                  topicField: Boolean(document.getElementById("quiz-topic-field") || document.getElementById("quiz-topic-select"))};
  // Generating quiz stays visible after going back
  backToQuizzes(); await sleep(100);
  out.landingSaved = quizHistoryList.textContent;
  let release; window.__hold = new Promise((resolve) => { release = resolve; });
  quizDifficultySelect.value = "medium"; quizNameInput.value = "Benchmark quiz";
  currentQuiz = null; renderAssessmentQuiz();
  const pending = generateAssessmentQuiz(); await sleep(200);
  backToQuizzes(); setSessionTab("summary"); setSessionTab("quiz"); await sleep(100);
  out.whileGenerating = {history: quizHistoryList.textContent, noCompletedMsg: quizHistoryList.textContent.includes("No completed quizzes")};
  release(); await pending; await sleep(400);
  backToQuizzes(); await sleep(100);
  out.afterGenerated = {history: quizHistoryList.textContent, pendingLeft: pendingQuizGenerations.size};
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="ui_smoke_"))
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
        self.assertTrue(self.out["bootstrap"]["appShellVisible"])

    def test_one_model_selector_in_the_study_session_header(self):
        self.assertEqual(self.out["bootstrap"]["modelSelectors"], ["generation-model-select"])       # exactly one, in the header
        self.assertEqual(self.out["bootstrap"]["headerOptions"], ["Qwen 2.5 7B", "DeepSeek R1 Distill Qwen 14B"])
        self.assertEqual(self.out["bootstrap"]["selected"], "qwen-2.5-7b")
        self.assertEqual(self.out["quizForm"]["labels"], ["Quiz name", "Scope", "Difficulty", "Number of questions"])
        self.assertEqual(self.out["quizForm"]["selects"], ["quiz-scope-select", "quiz-difficulty-select", "quiz-document-select", "quiz-question-count-select"])
        self.assertEqual(self.out["quizForm"]["scopeOptions"], ["Entire document"])
        self.assertFalse(self.out["quizForm"]["topicField"])                                         # no Topic in the quiz
        self.assertEqual(self.out["summaryOpened"]["selectsInSummary"], 0)                           # no selector on Summary ...
        self.assertEqual(self.out["flashcardsOpened"]["selectsInFlashcards"], 0)                     # ... nor on Flashcards
        self.assertEqual(self.out["summaryOpened"]["note"], "Model: Qwen 2.5 7B (change it in the Model selector above)")

    def test_summary_waits_for_generate_and_a_model_change_generates_nothing(self):
        opened, generated, switched = self.out["summaryOpened"], self.out["summaryGenerated"], self.out["afterModelChange"]
        self.assertEqual(opened["calls"], ["GET /api/summary/lecture.pdf?model_id=qwen-2.5-7b&cache_only=true"])
        self.assertTrue(opened["prompt"]); self.assertFalse(opened["regenerate"]); self.assertEqual(opened["content"], 0)
        self.assertEqual(generated["calls"][1], "GET /api/summary/lecture.pdf?model_id=qwen-2.5-7b")     # only after the click
        self.assertFalse(generated["prompt"]); self.assertTrue(generated["regenerate"])
        self.assertIn("Generated by Qwen 2.5 7B", generated["text"])
        self.assertEqual(switched["newCalls"], ["GET /api/summary/lecture.pdf?model_id=deepseek-r1-14b&cache_only=true"])
        self.assertTrue(switched["prompt"]); self.assertEqual(switched["content"], 0)
        self.assertIn("DeepSeek R1 Distill Qwen 14B", switched["note"])                                # the note follows the one selector

    def test_flashcards_wait_for_generate(self):
        opened, generated = self.out["flashcardsOpened"], self.out["flashcardsGenerated"]
        self.assertEqual(opened["calls"], ["GET /api/flashcards/lecture.pdf?model_id=deepseek-r1-14b&language=auto&cache_only=true"])
        self.assertTrue(opened["prompt"]); self.assertFalse(opened["stage"])
        self.assertEqual(generated["calls"], 2)
        self.assertFalse(generated["prompt"]); self.assertTrue(generated["stage"])

    def test_saved_follows_model_and_count_and_the_old_quiz_keeps_its_model(self):
        saved = self.out["quizSaved"]
        self.assertEqual(saved["qwen12"], "Easy (saved)")
        self.assertEqual(saved["deepseek12"], "Easy")            # DeepSeek + Easy + 12 is not saved
        self.assertEqual(saved["qwen15"], "Easy")                # Qwen + Easy + 15 is not saved
        self.assertIn("Generated by Qwen 2.5 7B", saved["title"])
        self.assertIn("Generated by Qwen 2.5 7B", saved["titleAfterSwitch"])
        self.assertNotIn("Generated by DeepSeek", saved["titleAfterSwitch"])
        self.assertIn("Current settings differ", saved["titleAfterSwitch"])

    def test_a_generating_quiz_stays_visible_and_a_partial_quiz_is_listed_when_done(self):
        self.assertIn("Not Started", self.out["landingSaved"])
        during = self.out["whileGenerating"]
        self.assertFalse(during["noCompletedMsg"])
        self.assertIn("Generating 12 questions", during["history"])
        after = self.out["afterGenerated"]
        self.assertIn("9/12 questions (partial)", after["history"])
        self.assertNotIn("Generating", after["history"])
        self.assertEqual(after["pendingLeft"], 0)


if __name__ == "__main__":
    unittest.main()
