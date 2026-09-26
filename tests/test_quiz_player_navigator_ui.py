"""Real-browser tests for the Quiz Player's compact question navigator (frontend/app.js in headless
Chrome against the Quiz Player mock from tests/test_quiz_player_ui.py, with 12- and 20-question
quizzes). Covers: one item per question, distinct current/answered/unanswered states, jumping to an
exact question without submitting, answers kept across jumps, autosaved position + Resume, Previous/
Next still correct, Finish/Results unchanged, and a compact layout at desktop and phone widths.

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
const navQuestions = (count) => Array.from({length: count}, (_, i) => ({
  id: i + 1, question: "N" + (i + 1) + "?", options: ["A. a", "B. b", "C. c", "D. d"], correct_answer: "A",
  question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: []}));
const navQuiz = (quizId, title, count) => ({...makeQuiz(quizId, title, "qwen-2.5-7b"), questions: navQuestions(count)});
window.__quizzes = {"nav-12": navQuiz("nav-12", "Nav 12", 12), "nav-20": navQuiz("nav-20", "Nav 20", 20)};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const cardFor = (title) => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === title);
const startButtonFor = (title) => cardFor(title)?.querySelector(".quiz-history-actions button");
const answerCards = () => [...document.querySelectorAll(".quiz-player-answer-card")];
const clickAnswer = (letter) => answerCards().find((b) => b.querySelector(".quiz-player-answer-label").textContent.trim().startsWith(letter + "."))?.click();
const items = () => [...$("quiz-player-nav").querySelectorAll(".quiz-player-nav-item")];
const jump = (n) => items()[n - 1].click();
const nav = () => items().map((b) => (b.classList.contains("is-current") ? "C" : "") + (b.classList.contains("is-answered") ? "A" : "") || "-");
const style = (b) => { const s = getComputedStyle(b); return [s.backgroundColor, s.color, s.borderColor].join("|"); };
const selectedLetter = () => answerCards().find((b) => b.classList.contains("selected"))?.querySelector(".quiz-player-answer-label").textContent.trim().charAt(0) || null;
const layout = () => {
  const n = $("quiz-player-nav"), r = n.getBoundingClientRect();
  const current = n.querySelector(".is-current").getBoundingClientRect();
  return {height: Math.round(r.height), itemHeight: Math.round(items()[0].getBoundingClientRect().height),
    scrolls: n.scrollWidth > n.clientWidth, currentVisible: current.left >= r.left - 1 && current.right <= r.right + 1,
    pageOverflow: document.documentElement.scrollWidth > window.innerWidth};
};
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(300);
  out.viewport = window.innerWidth;

  // 12-question quiz: one navigator item per question.
  startButtonFor("Nav 12").click();
  await sleep(300);
  out.opened = {labels: items().map((b) => b.textContent), nav: nav(), current: items()[0].getAttribute("aria-current"),
    aria: items()[1].getAttribute("aria-label"), position: $("quiz-player-position").textContent, layout: layout()};

  // Answer Q1, jump to Q5: exact question, no submit, position autosaved, Q1 answer kept.
  clickAnswer("A");
  jump(5);
  await sleep(700);
  out.afterJump = {position: $("quiz-player-position").textContent, nav: nav(), submitCalls: window.__submitCalls,
    body: {answers: window.__lastProgressBody.answers, index: window.__lastProgressBody.current_question_index},
    selected: selectedLetter(), previousDisabled: $("quiz-player-previous").disabled,
    styles: {current: style(items()[4]), answered: style(items()[0]), unanswered: style(items()[1])},
    aria: {answered: items()[0].getAttribute("aria-label"), current: items()[4].getAttribute("aria-current")}};

  clickAnswer("C");
  jump(1);
  await sleep(50);
  out.backToOne = {position: $("quiz-player-position").textContent, selected: selectedLetter(), nav: nav()};

  // Previous/Next stay correct after jumps.
  $("quiz-player-next").click(); await sleep(50);
  out.afterNext = {position: $("quiz-player-position").textContent, nav: nav()};
  jump(5); await sleep(50);
  $("quiz-player-previous").click(); await sleep(50);
  out.afterPrevious = {position: $("quiz-player-position").textContent};
  jump(12); await sleep(50);
  out.last = {position: $("quiz-player-position").textContent, nextLabel: $("quiz-player-next").textContent, submitCalls: window.__submitCalls};

  // Keyboard: a focused navigator item keeps focus on the (re-rendered) current item.
  items()[6].focus(); items()[6].click(); await sleep(50);
  out.focus = {position: $("quiz-player-position").textContent, activeIsCurrent: document.activeElement === $("quiz-player-nav").querySelector(".is-current")};

  // Exit (flushes) then Resume restores the exact question and the answered states.
  $("quiz-player-exit").click(); await sleep(150);
  if (!$("quiz-exit-confirm").hidden) { $("quiz-exit-confirm-exit").click(); await sleep(300); }
  out.saved = {answers: window.__attempts["nav-12"].answers, index: window.__attempts["nav-12"].current_question_index};
  startButtonFor("Nav 12").click();
  await sleep(300);
  out.resumed = {position: $("quiz-player-position").textContent, nav: nav(), answered: $("quiz-player-answered-count").textContent};

  // Answer everything via the navigator, then Finish submits once and opens Results as before.
  for (let n = 1; n <= 12; n += 1) { jump(n); await sleep(20); clickAnswer("A"); }
  await sleep(700);
  out.allAnswered = {nav: nav(), submitCalls: window.__submitCalls};
  $("quiz-player-next").click();   // Q12 is current: Finish Quiz, all answered -> no confirmation
  await sleep(400);
  out.finished = {submitCalls: window.__submitCalls, resultsVisible: !$("quiz-results-view").hidden,
    score: $("quiz-results-score").textContent, submittedAnswers: Object.keys(window.__lastSubmitBody.answers).length,
    confirmVisible: !$("quiz-finish-confirm").hidden};
  $("quiz-results-back").click();
  await sleep(300);

  // 20-question quiz: compact layout.
  startButtonFor("Nav 20").click();
  await sleep(300);
  out.twenty = {count: items().length, layout: layout()};
  jump(20); await sleep(50);
  out.twentyLast = {position: $("quiz-player-position").textContent, layout: layout()};

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


def run_page(window_size):
    work = Path(tempfile.mkdtemp(prefix="quiz_nav_"))
    try:
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
             f"--window-size={window_size}", f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise AssertionError("the page produced no result: " + result.stderr[-1500:])
    return json.loads(html.unescape(match.group(1)))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizPlayerNavigatorDesktopTests(unittest.TestCase):
    WINDOW = "1366,900"

    @classmethod
    def setUpClass(cls):
        cls.out = run_page(cls.WINDOW)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_twelve_question_quiz_renders_twelve_items_with_the_first_current(self):
        opened = self.out["opened"]
        self.assertEqual(opened["labels"], [str(n) for n in range(1, 13)])
        self.assertEqual(opened["nav"], ["C"] + ["-"] * 11)
        self.assertEqual(opened["current"], "step")
        self.assertEqual(opened["aria"], "Question 2, unanswered")
        self.assertEqual(opened["position"], "Question 1 of 12")

    def test_current_answered_and_unanswered_states_are_visually_distinct(self):
        styles = self.out["afterJump"]["styles"]
        self.assertEqual(len({styles["current"], styles["answered"], styles["unanswered"]}), 3)
        self.assertEqual(self.out["afterJump"]["aria"], {"answered": "Question 1, answered", "current": "step"})

    def test_clicking_an_item_jumps_to_that_exact_question_without_submitting(self):
        after = self.out["afterJump"]
        self.assertEqual(after["position"], "Question 5 of 12")
        self.assertEqual(after["nav"], ["A", "-", "-", "-", "C"] + ["-"] * 7)
        self.assertEqual(after["submitCalls"], 0)
        self.assertIsNone(after["selected"])
        self.assertFalse(after["previousDisabled"])
        self.assertEqual(after["body"], {"answers": {"1": "A"}, "index": 4})   # position autosaved, answers intact

    def test_saved_answers_remain_selected_after_jumping_back(self):
        back = self.out["backToOne"]
        self.assertEqual(back["position"], "Question 1 of 12")
        self.assertEqual(back["selected"], "A")
        self.assertEqual(back["nav"], ["CA", "-", "-", "-", "A"] + ["-"] * 7)

    def test_previous_and_next_stay_correct_after_jumps(self):
        self.assertEqual(self.out["afterNext"]["position"], "Question 2 of 12")
        self.assertEqual(self.out["afterNext"]["nav"][:2], ["A", "C"])
        self.assertEqual(self.out["afterPrevious"]["position"], "Question 4 of 12")
        last = self.out["last"]
        self.assertEqual(last["position"], "Question 12 of 12")
        self.assertEqual(last["nextLabel"], "Finish Quiz")
        self.assertEqual(last["submitCalls"], 0)

    def test_keyboard_focus_stays_on_the_navigator(self):
        self.assertEqual(self.out["focus"]["position"], "Question 7 of 12")
        self.assertTrue(self.out["focus"]["activeIsCurrent"])

    def test_resume_restores_current_question_and_answered_states(self):
        self.assertEqual(self.out["saved"], {"answers": {"1": "A", "5": "C"}, "index": 6})
        resumed = self.out["resumed"]
        self.assertEqual(resumed["position"], "Question 7 of 12")
        self.assertEqual(resumed["answered"], "2 answered")
        self.assertEqual(resumed["nav"], ["A", "-", "-", "-", "A", "-", "C"] + ["-"] * 5)

    def test_finish_and_results_are_unchanged(self):
        self.assertEqual(self.out["allAnswered"], {"nav": ["A"] * 11 + ["CA"], "submitCalls": 0})
        finished = self.out["finished"]
        self.assertFalse(finished["confirmVisible"])
        self.assertEqual(finished["submitCalls"], 1)
        self.assertTrue(finished["resultsVisible"])
        self.assertEqual(finished["submittedAnswers"], 12)
        self.assertEqual(finished["score"], "12 / 12")

    def test_twenty_questions_stay_compact_without_page_overflow(self):
        self.assertEqual(self.out["twenty"]["count"], 20)
        for state in (self.out["opened"]["layout"], self.out["twenty"]["layout"], self.out["twentyLast"]["layout"]):
            self.assertFalse(state["pageOverflow"])
            self.assertLessEqual(state["height"], state["itemHeight"] * 2 + 12)   # at most two compact rows
            self.assertTrue(state["currentVisible"])
        self.assertEqual(self.out["twentyLast"]["position"], "Question 20 of 20")


class QuizPlayerNavigatorPhoneTests(QuizPlayerNavigatorDesktopTests):
    WINDOW = "390,844"

    def test_twenty_questions_stay_compact_without_page_overflow(self):
        self.assertLessEqual(self.out["viewport"], 720)
        self.assertEqual(self.out["twenty"]["count"], 20)
        for state in (self.out["twenty"]["layout"], self.out["twentyLast"]["layout"]):
            self.assertFalse(state["pageOverflow"])
            self.assertLess(state["height"], state["itemHeight"] * 2)   # one row (plus scrollbar) that scrolls sideways
            self.assertTrue(state["scrolls"])
            self.assertTrue(state["currentVisible"])   # jumping to 20 keeps it in view


if __name__ == "__main__":
    unittest.main()
