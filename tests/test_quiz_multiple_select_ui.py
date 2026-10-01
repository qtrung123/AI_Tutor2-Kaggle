"""Real-browser tests for multiple_select ("select all that apply") in the focused Quiz Player and
Review (frontend/app.js in headless Chrome against a mocked API): single_choice keeps its radios,
multiple_select renders checkboxes with the "Select all that apply" hint, several options can be
selected and unselected without auto-advancing, Previous/Next keep the selections, autosave and
submit send an array, Resume restores a saved selection, and Review distinguishes selected-correct,
selected-wrong and missed-correct options.

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

MOCK = r"""
window.APP_CONFIG = { API_BASE_URL: "" };
window.__progressBodies = [];
window.__submitBodies = [];
const OPTIONS = ["A. alpha", "B. beta", "C. gamma", "D. delta"];
function makeQuiz(quizId, title) {
  const base = {options: OPTIONS, topic_id: "document", topic_name: "", difficulty: "easy", explanation: "Stated in the notes.", source_chunk_ids: ["h1"]};
  return {quiz_id: quizId, document_id: "lecture.pdf", title, difficulty: "easy", topic_id: "document", topic_name: "",
    assessment_scope: "document", generation_model: {model_id: "qwen-2.5-7b", name: "Qwen 2.5 7B"}, assessment_plan: {},
    created_at: "2026-01-01T00:00:00Z", questions: [
      {...base, id: 1, question: title + " single?", correct_answer: "A", correct_answers: ["A"], question_type: "single_choice"},
      {...base, id: 2, question: title + " which are correct?", correct_answer: "A", correct_answers: ["A", "C"], question_type: "multi_select"},
      {...base, id: 3, question: title + " which also apply?", correct_answer: "B", correct_answers: ["B", "D"], question_type: "multi_select"},
    ]};
}
window.__quizzes = {"quiz-mix": makeQuiz("quiz-mix", "Mix"), "quiz-resume": makeQuiz("quiz-resume", "Resumable")};
window.__latest = {"quiz-resume": {attempt_id: "live-resume", quiz_id: "quiz-resume", completed: false, completed_at: null,
  answers: {"1": "B", "2": ["A", "C"]}, answered: 2, total: 3, current_question_index: 1}};
// Server-side exact-set grading, as backend/quiz_attempt_service.submit_quiz_attempt persists it.
function grade(quiz, answers, attemptId) {
  let score = 0;
  const question_results = quiz.questions.map((question) => {
    const raw = answers[String(question.id)];
    const selected = (Array.isArray(raw) ? raw : (raw ? [raw] : [])).slice().sort();
    const correct = question.correct_answers.slice().sort();
    const is_correct = selected.length > 0 && selected.join() === correct.join();
    if (is_correct) score += 1;
    return {question_id: question.id, question: question.question, options: question.options,
      selected_answer: selected[0] || "", selected_answers: selected, correct_answer: correct[0], correct_answers: correct,
      question_type: question.question_type, is_correct, question_difficulty: "easy", validation_outcome: "accepted",
      topic_id: "document", topic_name: "", explanation: question.explanation, source_chunk_ids: question.source_chunk_ids};
  });
  const total = quiz.questions.length;
  return {attempt_id: attemptId, quiz_id: quiz.quiz_id, document_id: "lecture.pdf", difficulty: "easy", topic_id: "document",
    completed: true, completed_at: "2026-01-02T00:00:00Z", score, total, answered: Object.keys(answers).length,
    percentage: Math.round((10000 * score) / total) / 100, attempt_number: 1, current_question_index: 0, answers, question_results};
}
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [{id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) return json({status: "ready", ready: true});
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: true,
    variants: Object.values(window.__quizzes).map((quiz) => ({quiz_id: quiz.quiz_id, title: quiz.title, topic_id: "document",
      topic_name: "", assessment_scope: "document", difficulty: "easy", question_count: 3, requested_count: 3, status: "complete",
      model_id: "qwen-2.5-7b", model_name: "Qwen 2.5 7B", created_at: quiz.created_at, updated_at: quiz.created_at,
      progress_status: window.__latest[quiz.quiz_id] ? "in_progress" : "not_started",
      answered: window.__latest[quiz.quiz_id]?.answered || 0, total: 3, score: null, percentage: null}))}]);
  if (p === "/api/quiz-history") return json([]);
  const progressMatch = p.match(/^\/api\/quiz\/([^/]+)\/progress$/);
  if (progressMatch && method === "PATCH") {
    const body = JSON.parse(init.body);
    window.__progressBodies.push(body);
    return json({attempt_id: "live-" + body.quiz_id, quiz_id: body.quiz_id, answers: body.answers || {}, completed: false,
      completed_at: null, answered: Object.keys(body.answers || {}).length, total: 3, current_question_index: body.current_question_index || 0});
  }
  const submitMatch = p.match(/^\/api\/quiz\/([^/]+)\/submit$/);
  if (submitMatch && method === "POST") {
    const body = JSON.parse(init.body);
    window.__submitBodies.push(body);
    const attempt = grade(window.__quizzes[body.quiz_id], body.answers, "att-" + window.__submitBodies.length);
    return json({...attempt, attempt_summary: {attempts: 1, latest_score: attempt.percentage, best_score: attempt.percentage, average_score: attempt.percentage}});
  }
  const quizMatch = p.match(/^\/api\/quiz\/([^/]+)$/);
  if (quizMatch && method === "GET") {
    const quizId = u.searchParams.get("quiz_id");
    return json({document_id: quizMatch[1], difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"),
      quiz: window.__quizzes[quizId] || null, latest_attempt: window.__latest[quizId] || null, attempt_summary: null});
  }
  if (p.startsWith("/api/summary/")) return json({status: "not_generated"});
  if (p.startsWith("/api/flashcards/")) return json({status: "not_generated", cards: []});
  if (p === "/api/conversations" && method === "POST") return json({id: "c1", title: "New conversation", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p.startsWith("/api/conversations/")) return json({id: "c1", title: "t", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p === "/api/conversations") return json([]);
  if (p === "/api/dashboard") return json({mastery: [], materials: [], summary: {}});
  return json([]);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const cards = () => [...document.querySelectorAll("#quiz-player-answers .quiz-player-answer-card")];
const startFor = (title) => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === title)?.querySelector(".quiz-history-actions button");
const player = () => ({
  position: $("quiz-player-position").textContent, question: $("quiz-player-question").textContent,
  hintVisible: !$("quiz-player-select-hint").hidden, hint: $("quiz-player-select-hint").textContent,
  roles: cards().map((card) => card.getAttribute("role")),
  checked: cards().map((card) => card.getAttribute("aria-checked") === "true"),
  multiClass: cards().every((card) => card.classList.contains("is-multi")),
  indicatorRadius: cards().length ? getComputedStyle(cards()[0].querySelector(".quiz-player-answer-indicator")).borderRadius : "",
});
const review = () => ({
  position: $("quiz-review-position").textContent, status: $("quiz-review-status").textContent,
  options: [...document.querySelectorAll("#quiz-review-options .quiz-review-option")].map((row) => ({
    letter: row.dataset.letter,
    classes: ["is-correct", "is-selected", "is-wrong", "is-missed"].filter((c) => row.classList.contains(c)),
    tag: row.querySelector(".quiz-review-option-tag")?.textContent || "",
  })),
});
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(400);

  // Start: question 1 is single_choice (radios, no hint).
  out.startLabel = startFor("Mix")?.textContent;
  startFor("Mix").click(); await sleep(300);
  out.q1 = player();
  cards()[0].click(); await sleep(50);
  out.q1Selected = player();

  // Question 2 is multiple_select: checkboxes + hint; several options; unselect; no auto-advance.
  $("quiz-player-next").click(); await sleep(50);
  out.q2 = player();
  cards()[0].click(); await sleep(50);
  out.q2AfterFirst = player();
  cards()[2].click(); await sleep(50);
  cards()[3].click(); await sleep(50);
  out.q2AfterThree = player();
  cards()[3].click(); await sleep(50);   // unselect D
  out.q2Final = player();
  await sleep(900);                      // autosave debounce
  out.autosaveBody = window.__progressBodies.at(-1);

  // Previous / Next keep both kinds of selections.
  $("quiz-player-previous").click(); await sleep(50);
  out.backToQ1 = player();
  $("quiz-player-next").click(); await sleep(50);
  out.returnToQ2 = player();

  // Question 3: select B (correct) and C (wrong), miss D -> Finish.
  $("quiz-player-next").click(); await sleep(50);
  cards()[1].click(); await sleep(30);
  cards()[2].click(); await sleep(30);
  out.q3 = player();
  $("quiz-player-next").click(); await sleep(600);   // Finish Quiz: everything answered -> submits
  out.submitBody = window.__submitBodies.at(-1);
  out.score = $("quiz-results-score").textContent;

  // Review.
  $("quiz-results-review").click(); await sleep(50);
  out.review1 = review();
  $("quiz-review-next").click(); await sleep(50);
  out.review2 = review();
  $("quiz-review-next").click(); await sleep(50);
  out.review3 = review();
  $("quiz-review-back-results").click(); await sleep(50);
  $("quiz-results-back").click(); await sleep(400);

  // Resume restores the saved multiple selection at the saved position.
  out.resumeLabel = startFor("Resumable")?.textContent;
  startFor("Resumable").click(); await sleep(400);
  out.resumed = player();
  $("quiz-player-previous").click(); await sleep(50);
  out.resumedQ1 = player();

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizMultipleSelectUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_multi_select_"))
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

    def test_single_choice_renders_radios_without_the_hint(self):
        q1 = self.out["q1"]
        self.assertEqual(self.out["startLabel"], "Start")
        self.assertEqual(q1["position"], "Question 1 of 3")
        self.assertEqual(q1["roles"], ["radio"] * 4)
        self.assertFalse(q1["hintVisible"])
        self.assertFalse(q1["multiClass"])
        self.assertEqual(q1["indicatorRadius"], "50%")
        self.assertEqual(self.out["q1Selected"]["checked"], [True, False, False, False])

    def test_multiple_select_renders_checkboxes_with_the_hint(self):
        q2 = self.out["q2"]
        self.assertEqual(q2["position"], "Question 2 of 3")
        self.assertEqual(q2["roles"], ["checkbox"] * 4)
        self.assertTrue(q2["hintVisible"])
        self.assertEqual(q2["hint"], "Select all that apply")
        self.assertTrue(q2["multiClass"])
        self.assertNotEqual(q2["indicatorRadius"], "50%")   # square checkbox indicator

    def test_multiple_options_can_be_selected_and_unselected_without_auto_advance(self):
        self.assertEqual(self.out["q2AfterFirst"]["checked"], [True, False, False, False])
        self.assertEqual(self.out["q2AfterFirst"]["position"], "Question 2 of 3")   # no auto-advance
        self.assertEqual(self.out["q2AfterThree"]["checked"], [True, False, True, True])
        self.assertEqual(self.out["q2Final"]["checked"], [True, False, True, False])
        self.assertEqual(self.out["autosaveBody"]["answers"], {"1": "A", "2": ["A", "C"]})

    def test_previous_and_next_preserve_selections(self):
        self.assertEqual(self.out["backToQ1"]["checked"], [True, False, False, False])
        self.assertEqual(self.out["returnToQ2"]["checked"], [True, False, True, False])
        self.assertTrue(self.out["returnToQ2"]["hintVisible"])

    def test_submit_sends_arrays_and_grades_exact_sets(self):
        self.assertEqual(self.out["q3"]["checked"], [False, True, True, False])
        self.assertEqual(self.out["submitBody"]["answers"], {"1": "A", "2": ["A", "C"], "3": ["B", "C"]})
        self.assertEqual(self.out["score"], "2 / 3")

    def test_review_keeps_single_choice_states(self):
        r1 = self.out["review1"]
        self.assertEqual(r1["status"], "Correct")
        self.assertEqual(r1["options"][0], {"letter": "A", "classes": ["is-correct", "is-selected"], "tag": "Your answer · Correct"})
        self.assertTrue(all(option["classes"] == [] and option["tag"] == "" for option in r1["options"][1:]))

    def test_review_marks_selected_correct_options(self):
        r2 = self.out["review2"]
        self.assertEqual(r2["status"], "Correct")
        by_letter = {option["letter"]: option for option in r2["options"]}
        for letter in ("A", "C"):
            self.assertEqual(by_letter[letter]["classes"], ["is-correct", "is-selected"])
            self.assertEqual(by_letter[letter]["tag"], "Your answer · Correct")
        for letter in ("B", "D"):
            self.assertEqual((by_letter[letter]["classes"], by_letter[letter]["tag"]), ([], ""))

    def test_review_distinguishes_wrong_selections_and_missed_correct_options(self):
        r3 = self.out["review3"]
        self.assertEqual(r3["status"], "Incorrect")
        by_letter = {option["letter"]: option for option in r3["options"]}
        self.assertEqual(by_letter["B"], {"letter": "B", "classes": ["is-correct", "is-selected"], "tag": "Your answer · Correct"})
        self.assertEqual(by_letter["C"], {"letter": "C", "classes": ["is-selected", "is-wrong"], "tag": "Your answer · Incorrect"})
        self.assertEqual(by_letter["D"], {"letter": "D", "classes": ["is-correct", "is-missed"], "tag": "Correct answer · Missed"})
        self.assertEqual(by_letter["A"], {"letter": "A", "classes": [], "tag": ""})

    def test_resume_restores_multiple_selections(self):
        self.assertEqual(self.out["resumeLabel"], "Resume")
        resumed = self.out["resumed"]
        self.assertEqual(resumed["position"], "Question 2 of 3")
        self.assertEqual(resumed["roles"], ["checkbox"] * 4)
        self.assertEqual(resumed["checked"], [True, False, True, False])
        self.assertEqual(self.out["resumedQ1"]["checked"], [False, True, False, False])


if __name__ == "__main__":
    unittest.main()
