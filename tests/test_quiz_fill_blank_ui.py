"""Real-browser tests for fill-in-the-blank questions in the focused Quiz Player (headless Chrome
against the Quiz Player mock with a mixed MCQ + fill_blank quiz): the text input, autosave of the
typed text, the navigator marking only non-empty answers, Resume restoring the text, Finish, and
Results/Review showing the learner's answer next to the stored correct answer(s). Grading itself is
backend-authoritative (tests/test_quiz_fill_blank.py); the mock mirrors its normalized exact match.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no network.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, MOCK as QUIZ_MOCK, find_chrome

MOCK = QUIZ_MOCK + r"""
const mcq = (id, text, correct) => ({id, question: text, options: ["A. a", "B. b", "C. c", "D. d"], correct_answer: correct,
  correct_answers: [correct], question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: ["h"]});
const fill = (id, text, answers) => ({id, question: text, options: [], correct_answer: answers[0], correct_answers: answers,
  question_type: "fill_blank", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "The document names it.", source_chunk_ids: ["h"]});
window.__quizzes = {"mixed": {...makeQuiz("mixed", "Mixed quiz", "qwen-2.5-7b"), questions: [
  mcq(1, "Q1?", "A"), fill(2, "The ____ orders processes.", ["Scheduler", "task scheduler"]),
  fill(3, "A ____ guards counters.", ["semaphore"]), mcq(4, "Q4?", "B")]}};
const normalizeFill = (value) => String(value || "").normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim()
  .replace(/^[\s.,;:!?"'`()\[\]{}]+|[\s.,;:!?"'`()\[\]{}]+$/g, "");
const baseFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const p = new URL(url, "http://x").pathname;
  if (/^\/api\/quiz\/[^/]+\/submit$/.test(p)) {
    const body = JSON.parse(init.body);
    window.__submitCalls += 1;
    window.__lastSubmitBody = body;
    const quiz = window.__quizzes[body.quiz_id];
    let score = 0;
    const question_results = quiz.questions.map((q) => {
      const raw = body.answers[String(q.id)] || "";
      const selected = q.question_type === "fill_blank" ? String(raw).trim() : raw;
      const correct = q.question_type === "fill_blank"
        ? Boolean(selected) && q.correct_answers.some((answer) => normalizeFill(answer) === normalizeFill(selected))
        : selected === q.correct_answer;
      if (correct) score += 1;
      return {question_id: q.id, question: q.question, options: q.options, question_type: q.question_type,
        selected_answer: selected, selected_answers: selected ? [selected] : [], correct_answer: q.correct_answer,
        correct_answers: q.correct_answers, is_correct: correct, explanation: q.explanation, topic_name: "", source_chunk_ids: ["h"]};
    });
    const total = quiz.questions.length;
    const attempt = {attempt_id: "att-mixed-1", quiz_id: body.quiz_id, answers: body.answers, question_results, completed: true,
      completed_at: "2026-01-01T00:02:00Z", score, total, percentage: Math.round(100 * score / total), current_question_index: 0,
      updated_at: "2026-01-01T00:02:00Z", attempt_number: 1};
    window.__attempts[body.quiz_id] = attempt;
    return json(attempt);
  }
  return baseFetch(input, init);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const startButton = () => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === "Mixed quiz")
  ?.querySelector(".quiz-history-actions button");
const nav = () => [...document.querySelectorAll("#quiz-player-nav .quiz-player-nav-item")].map((b) =>
  (b.classList.contains("is-current") ? "C" : "") + (b.classList.contains("is-answered") ? "A" : "") || "-");
const input = () => $("quiz-player-fill-input");
const type = (text) => { input().focus(); input().value = text; input().dispatchEvent(new Event("input", {bubbles: true})); };
const clickAnswer = (letter) => [...document.querySelectorAll(".quiz-player-answer-card")]
  .find((b) => b.textContent.trim().startsWith(letter + "."))?.click();
const reviewRows = () => [...document.querySelectorAll("#quiz-review-options .quiz-review-option")].map((row) =>
  ({text: row.textContent, classes: [...row.classList].filter((c) => c.startsWith("is-")).sort()}));
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(300);
  startButton().click();
  await sleep(300);
  clickAnswer("A");
  document.querySelectorAll("#quiz-player-nav .quiz-player-nav-item")[1].click();
  await sleep(50);
  out.fillView = {position: $("quiz-player-position").textContent, question: $("quiz-player-question").textContent,
    hasInput: Boolean(input()), optionCards: document.querySelectorAll(".quiz-player-answer-card").length,
    placeholder: input()?.placeholder, nav: nav()};

  type("  task   scheduler ");
  out.typed = {nav: nav(), answered: $("quiz-player-answered-count").textContent, focused: document.activeElement === input()};
  await sleep(700);
  out.autosave = {answers: window.__lastProgressBody.answers, index: window.__lastProgressBody.current_question_index};
  type("   ");
  out.cleared = {nav: nav(), answered: $("quiz-player-answered-count").textContent};
  type("Scheduler");
  await sleep(700);

  // Previous/Next keep the typed text; Exit + Resume restore it and the position.
  $("quiz-player-next").click(); await sleep(50);
  type("semafore");
  $("quiz-player-previous").click(); await sleep(50);
  out.afterPrevious = {value: input().value, position: $("quiz-player-position").textContent};
  $("quiz-player-exit").click(); await sleep(200);
  if (!$("quiz-exit-confirm").hidden) { $("quiz-exit-confirm-exit").click(); await sleep(300); }
  out.saved = {answers: window.__attempts["mixed"].answers, index: window.__attempts["mixed"].current_question_index};
  startButton().click();
  await sleep(300);
  out.resumed = {position: $("quiz-player-position").textContent, value: input()?.value, nav: nav()};

  // Finish: Q4 unanswered first -> confirmation (unchanged), then answer it and submit.
  document.querySelectorAll("#quiz-player-nav .quiz-player-nav-item")[3].click(); await sleep(50);
  clickAnswer("B"); await sleep(700);
  $("quiz-player-next").click();   // Finish Quiz, all four answered
  await sleep(400);
  out.results = {visible: !$("quiz-results-view").hidden, score: $("quiz-results-score").textContent,
    correct: $("quiz-results-correct").textContent, incorrect: $("quiz-results-incorrect").textContent,
    submitted: window.__lastSubmitBody.answers, submitCalls: window.__submitCalls};
  $("quiz-results-review").click(); await sleep(100);
  $("quiz-review-next").click(); await sleep(50);
  out.reviewCorrect = {status: $("quiz-review-status").textContent, rows: reviewRows(), question: $("quiz-review-question").textContent};
  $("quiz-review-next").click(); await sleep(50);
  out.reviewWrong = {status: $("quiz-review-status").textContent, rows: reviewRows()};
  $("quiz-review-next").click(); await sleep(50);
  out.reviewMcq = {rows: document.querySelectorAll("#quiz-review-options .quiz-review-option").length};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth;

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class FillBlankPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_fill_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000", "--dump-dom",
             "--window-size=1440,900", f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_no_script_errors_or_overflow(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertFalse(self.out["overflow"])

    def test_fill_blank_question_shows_a_text_input_not_options(self):
        view = self.out["fillView"]
        self.assertEqual(view["position"], "Question 2 of 4")
        self.assertEqual(view["question"], "The ____ orders processes.")
        self.assertTrue(view["hasInput"])
        self.assertEqual(view["optionCards"], 0)
        self.assertEqual(view["placeholder"], "Type your answer")
        self.assertEqual(view["nav"], ["A", "C", "-", "-"])

    def test_typing_marks_the_navigator_and_autosaves_without_losing_focus(self):
        self.assertEqual(self.out["typed"], {"nav": ["A", "CA", "-", "-"], "answered": "2 answered", "focused": True})
        self.assertEqual(self.out["autosave"], {"answers": {"1": "A", "2": "  task   scheduler "}, "index": 1})
        self.assertEqual(self.out["cleared"], {"nav": ["A", "C", "-", "-"], "answered": "1 answered"})   # whitespace = unanswered

    def test_previous_next_and_resume_keep_the_typed_text(self):
        self.assertEqual(self.out["afterPrevious"], {"value": "Scheduler", "position": "Question 2 of 4"})
        self.assertEqual(self.out["saved"]["answers"], {"1": "A", "2": "Scheduler", "3": "semafore"})
        self.assertEqual(self.out["resumed"], {"position": "Question 2 of 4", "value": "Scheduler", "nav": ["A", "CA", "A", "-"]})

    def test_submit_scores_normally(self):
        results = self.out["results"]
        self.assertTrue(results["visible"])
        self.assertEqual((results["score"], results["correct"], results["incorrect"]), ("3 / 4", "3", "1"))
        self.assertEqual(results["submitted"], {"1": "A", "2": "Scheduler", "3": "semafore", "4": "B"})
        self.assertEqual(results["submitCalls"], 1)

    def test_review_shows_learner_and_correct_answer(self):
        correct = self.out["reviewCorrect"]
        self.assertEqual(correct["status"], "Correct")
        self.assertEqual(correct["rows"][0], {"text": "Your answerScheduler", "classes": ["is-correct", "is-fill-blank", "is-selected"]})
        self.assertEqual(correct["rows"][1]["text"], "Correct answerScheduler (also accepted: task scheduler)")
        wrong = self.out["reviewWrong"]
        self.assertEqual(wrong["status"], "Incorrect")
        self.assertEqual(wrong["rows"][0], {"text": "Your answersemafore", "classes": ["is-fill-blank", "is-selected", "is-wrong"]})
        self.assertEqual(wrong["rows"][1]["text"], "Correct answersemaphore")
        self.assertEqual(self.out["reviewMcq"]["rows"], 4)   # multiple-choice review unchanged


if __name__ == "__main__":
    unittest.main()
