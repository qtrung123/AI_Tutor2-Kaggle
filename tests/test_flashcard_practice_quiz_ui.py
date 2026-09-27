"""Real-browser tests for Flashcards -> "Practice as Quiz" (headless Chrome against the Quiz Player
mock): the sheet (Type / Questions / Start Quiz), the "No flashcards available" state, the request
sent for the exact set on screen, opening the created quiz in the focused Quiz Player, text input for
short_answer and fill_blank, autosave + resume of typed answers, Results/Review showing the typed and
the canonical answer, and learner-marked self-check questions. Backend behavior is covered by
tests/test_flashcard_written_quiz.py; the mock mirrors its normalized exact-match grading.

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
const CARDS = [
  {flashcard_id: "f1", set_id: "set-1", topic_id: "t1", topic_name: "Topic 1", front: "Which component orders processes?", back: "The scheduler"},
  {flashcard_id: "f2", set_id: "set-1", topic_id: "t1", topic_name: "Topic 1", front: "What guards counters?", back: "semaphore"},
  {flashcard_id: "f3", set_id: "set-1", topic_id: "t1", topic_name: "Topic 1", front: "Why do processes need a scheduler?", back: "Because many processes compete for a few CPU cores at once."},
];
const written = (id, type, text, answers) => ({id, question: text, options: [], correct_answer: answers[0], correct_answers: answers,
  question_type: type, topic_id: "t1", topic_name: "Topic 1", difficulty: "easy", explanation: "Answer from your flashcard.", source_chunk_ids: []});
window.__practiceBodies = [];
window.__selfCheckBodies = [];
window.__library = () => Object.values(window.__quizzes).map((quiz) => {
  const attempt = window.__attempts[quiz.quiz_id];
  const completed = Boolean(attempt && attempt.completed);
  const answered = attempt ? Object.keys(attempt.answers || {}).length : 0;
  const model = quiz.generation_model || {};
  return {quiz_id: quiz.quiz_id, title: quiz.title, topic_id: "document", topic_name: "", assessment_scope: "document",
    difficulty: quiz.difficulty, question_count: quiz.questions.length, requested_count: quiz.questions.length, status: "complete",
    model_id: model.model_id || null, model_name: model.name || null, created_at: quiz.created_at,
    updated_at: (attempt && attempt.updated_at) || quiz.created_at,
    progress_status: completed ? "completed" : (answered > 0 ? "in_progress" : "not_started"),
    answered, total: quiz.questions.length, score: completed ? attempt.score : null, percentage: completed ? attempt.percentage : null};
});
const normalizeText = (value) => String(value || "").normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim()
  .replace(/^[\s.,;:!?"'`()\[\]{}]+|[\s.,;:!?"'`()\[\]{}]+$/g, "");
const quizFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (p === "/api/flashcards/lecture.pdf" && method === "GET") {
    return json({set_id: "set-1", model_id: "qwen-2.5-7b", cache_hit: true, cards: CARDS});
  }
  if (p === "/api/flashcards/lecture.pdf/practice-quiz" && method === "POST") {
    window.__practiceBodies.push(JSON.parse(init.body));
    const quiz = {quiz_id: "fc-quiz", document_id: "lecture.pdf", title: "Lecture · Flashcard practice (Mixed)", difficulty: "easy",
      topic_id: "document", topic_name: "Entire document", assessment_scope: "document", created_at: "2026-01-02T00:00:00Z",
      assessment_plan: {planner_version: "flashcard_written_v1", source: "flashcards", self_check_question_ids: [3],
        type_distribution: {short_answer: 2, fill_blank: 1}},
      questions: [written(1, "short_answer", CARDS[0].front, ["The scheduler"]),
        written(2, "fill_blank", "A ____ guards shared counters.", ["semaphore"]),
        written(3, "short_answer", CARDS[2].front, [CARDS[2].back])]};
    window.__quizzes["fc-quiz"] = quiz;
    return json(quiz);
  }
  if (/^\/api\/quiz\/[^/]+\/submit$/.test(p)) {
    const body = JSON.parse(init.body);
    window.__submitCalls += 1;
    window.__lastSubmitBody = body;
    const quiz = window.__quizzes[body.quiz_id];
    let score = 0;
    const question_results = quiz.questions.map((q) => {
      const selected = String(body.answers[String(q.id)] || "").trim();
      const correct = Boolean(selected) && q.correct_answers.some((answer) => normalizeText(answer) === normalizeText(selected));
      if (correct) score += 1;
      return {question_id: q.id, question: q.question, options: [], question_type: q.question_type, selected_answer: selected,
        selected_answers: selected ? [selected] : [], correct_answer: q.correct_answer, correct_answers: q.correct_answers,
        is_correct: correct, explanation: q.explanation, topic_name: "Topic 1", source_chunk_ids: []};
    });
    const total = quiz.questions.length;
    const attempt = {attempt_id: "att-fc-1", quiz_id: body.quiz_id, answers: body.answers, question_results, completed: true,
      completed_at: "2026-01-02T00:02:00Z", score, total, percentage: Math.round(100 * score / total), current_question_index: 0,
      updated_at: "2026-01-02T00:02:00Z", attempt_number: 1};
    window.__attempts[body.quiz_id] = attempt;
    return json(attempt);
  }
  const selfCheck = p.match(/^\/api\/quiz-history\/([^/]+)\/self-check$/);
  if (selfCheck && method === "POST") {
    const body = JSON.parse(init.body);
    window.__selfCheckBodies.push({attempt: selfCheck[1], ...body});
    const attempt = window.__attempts["fc-quiz"];
    attempt.question_results = attempt.question_results.map((r) => r.question_id === body.question_id ? {...r, is_correct: body.is_correct} : r);
    attempt.score = attempt.question_results.filter((r) => r.is_correct).length;
    attempt.percentage = Math.round(100 * attempt.score / attempt.total);
    return json({...attempt, quiz: {quiz_id: "fc-quiz", title: window.__quizzes["fc-quiz"].title, source: "flashcards", self_check_question_ids: [3]}});
  }
  return quizFetch(input, init);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const dialog = () => document.querySelector(".flashcard-quiz-dialog");
const segments = (id) => [...document.querySelectorAll(`#${id} .quiz-segmented-option`)].map((b) =>
  ({label: b.textContent, active: b.classList.contains("active"), disabled: b.disabled}));
const input = () => $("quiz-player-fill-input");
const type = (text) => { input().focus(); input().value = text; input().dispatchEvent(new Event("input", {bubbles: true})); };
const resumeButton = () => [...document.querySelectorAll(".quiz-saved-card")]
  .find((c) => c.querySelector("strong")?.textContent.includes("Flashcard practice"))?.querySelector(".quiz-history-actions button");
const reviewRows = () => [...document.querySelectorAll("#quiz-review-options .quiz-review-option")].map((row) => row.textContent);
(async () => {
  await sleep(1500);

  // No flashcards: the sheet says so and cannot start.
  await openStudySession("notes.pdf", "flashcards");
  await sleep(400);
  $("practice-flashcards-quiz").click();
  await sleep(50);
  out.empty = {open: dialog().classList.contains("open"), emptyVisible: !$("flashcard-quiz-empty").hidden,
    emptyText: $("flashcard-quiz-empty").textContent, fieldsHidden: $("flashcard-quiz-fields").hidden, startDisabled: $("flashcard-quiz-start").disabled};
  dialog().querySelector(".quiz-dialog-cancel").click();
  await sleep(50);
  out.emptyClosed = !dialog().classList.contains("open");

  // With the three cards on screen.
  await openStudySession("lecture.pdf", "flashcards");
  await sleep(400);
  out.buttonLabel = $("practice-flashcards-quiz").textContent;
  $("practice-flashcards-quiz").click();
  await sleep(50);
  out.sheet = {title: $("flashcard-quiz-title").textContent, emptyHidden: $("flashcard-quiz-empty").hidden,
    modes: segments("flashcard-quiz-mode"), counts: segments("flashcard-quiz-count"), start: $("flashcard-quiz-start").textContent};
  $("flashcard-quiz-start").click();
  await sleep(600);
  out.request = window.__practiceBodies;
  out.opened = {dialogClosed: !dialog().classList.contains("open"), playerVisible: !$("quiz-player").hidden,
    title: $("quiz-player-title").textContent, position: $("quiz-player-position").textContent,
    question: $("quiz-player-question").textContent, hasInput: Boolean(input()),
    caption: document.querySelector(".quiz-player-fill-caption")?.textContent, maxLength: input()?.maxLength,
    optionCards: document.querySelectorAll(".quiz-player-answer-card").length};

  type("the Scheduler!");
  $("quiz-player-next").click(); await sleep(50);
  out.fillCaption = document.querySelector(".quiz-player-fill-caption")?.textContent;
  type("Semaphore");
  $("quiz-player-next").click(); await sleep(50);
  type("Lots of processes want the CPU");
  await sleep(700);
  out.autosave = window.__lastProgressBody.answers;

  // Exit and Resume restore the typed text.
  $("quiz-player-exit").click(); await sleep(200);
  if (!$("quiz-exit-confirm").hidden) { $("quiz-exit-confirm-exit").click(); await sleep(300); }
  resumeButton().click();
  await sleep(400);
  out.resumed = {position: $("quiz-player-position").textContent, value: input()?.value};

  $("quiz-player-next").click();   // Finish Quiz on the last question, all answered
  await sleep(400);
  out.results = {visible: !$("quiz-results-view").hidden, score: $("quiz-results-score").textContent, submitted: window.__lastSubmitBody.answers};
  $("quiz-results-review").click(); await sleep(100);
  out.review1 = {status: $("quiz-review-status").textContent, rows: reviewRows(), selfCheck: Boolean(document.querySelector(".quiz-review-self-check"))};
  $("quiz-review-next").click(); $("quiz-review-next").click(); await sleep(50);
  out.review3 = {status: $("quiz-review-status").textContent, rows: reviewRows(),
    buttons: [...document.querySelectorAll(".quiz-review-self-check button")].map((b) => ({label: b.textContent, disabled: b.disabled}))};
  document.querySelector(".quiz-self-check-right").click();
  await sleep(300);
  out.marked = {status: $("quiz-review-status").textContent, body: window.__selfCheckBodies,
    rightDisabled: document.querySelector(".quiz-self-check-right")?.disabled};
  $("quiz-review-next").click(); await sleep(50);   // last -> back to Results
  out.markedScore = $("quiz-results-score").textContent;
  out.overflow = document.documentElement.scrollWidth > window.innerWidth;

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class FlashcardPracticeQuizUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="flashcard_quiz_"))
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

    def test_no_flashcards_state(self):
        self.assertEqual(self.out["empty"], {
            "open": True, "emptyVisible": True, "fieldsHidden": True, "startDisabled": True,
            "emptyText": "No flashcards available. Generate flashcards for this document first.",
        })
        self.assertTrue(self.out["emptyClosed"])

    def test_sheet_offers_type_and_count(self):
        self.assertEqual(self.out["buttonLabel"], "Practice as Quiz")
        sheet = self.out["sheet"]
        self.assertEqual(sheet["title"], "Practice as Quiz")
        self.assertTrue(sheet["emptyHidden"])
        self.assertEqual([m["label"] for m in sheet["modes"]], ["Mixed", "Short Answer", "Fill Blank"])
        self.assertTrue(sheet["modes"][0]["active"])
        # Three cards: 5 and 10 are unavailable, "All" is chosen.
        self.assertEqual(sheet["counts"], [
            {"label": "5", "active": False, "disabled": True}, {"label": "10", "active": False, "disabled": True},
            {"label": "All (3)", "active": True, "disabled": False}])
        self.assertEqual(sheet["start"], "Start Quiz")

    def test_start_uses_the_set_on_screen_and_opens_the_player(self):
        self.assertEqual(self.out["request"], [{"mode": "mixed", "question_count": None, "set_id": "set-1"}])
        opened = self.out["opened"]
        self.assertTrue(opened["dialogClosed"])
        self.assertTrue(opened["playerVisible"])
        self.assertEqual(opened["title"], "Lecture · Flashcard practice (Mixed)")
        self.assertEqual((opened["position"], opened["question"]), ("Question 1 of 3", "Which component orders processes?"))
        self.assertTrue(opened["hasInput"])
        self.assertEqual(opened["optionCards"], 0)
        self.assertEqual((opened["caption"], opened["maxLength"]), ("Type your answer", 1000))
        self.assertEqual(self.out["fillCaption"], "Fill in the blank")

    def test_typed_answers_autosave_and_resume(self):
        self.assertEqual(self.out["autosave"], {"1": "the Scheduler!", "2": "Semaphore", "3": "Lots of processes want the CPU"})
        self.assertEqual(self.out["resumed"], {"position": "Question 3 of 3", "value": "Lots of processes want the CPU"})

    def test_results_and_review_show_typed_and_canonical_answers(self):
        self.assertTrue(self.out["results"]["visible"])
        self.assertEqual(self.out["results"]["score"], "2 / 3")
        self.assertEqual(self.out["review1"], {"status": "Correct", "selfCheck": False,
                                               "rows": ["Your answerthe Scheduler!", "Correct answerThe scheduler"]})
        review3 = self.out["review3"]
        self.assertEqual(review3["status"], "Incorrect · Self-check")
        self.assertEqual(review3["rows"], ["Your answerLots of processes want the CPU",
                                           "Correct answerBecause many processes compete for a few CPU cores at once."])
        self.assertEqual(review3["buttons"], [{"label": "I got it right", "disabled": False}, {"label": "I missed it", "disabled": True}])

    def test_learner_can_mark_a_self_check_answer(self):
        marked = self.out["marked"]
        self.assertEqual(marked["body"], [{"attempt": "att-fc-1", "question_id": 3, "is_correct": True}])
        self.assertEqual(marked["status"], "Correct · Self-check")
        self.assertTrue(marked["rightDisabled"])
        self.assertEqual(self.out["markedScore"], "3 / 3")



class PracticeIsolationFrontendTests(unittest.TestCase):
    """Flashcard practice quizzes (variant/attempt `practice: true`) never count as the document's
    assessment in the UI: no "(saved)" hint, not the planned quiz to resume, not the latest score."""

    @classmethod
    def setUpClass(cls):
        from frontend_source import frontend_script_text
        cls.script = frontend_script_text()

    def test_saved_hints_ignore_practice_variants(self):
        self.assertIn(".filter((variant) => !variant.practice && variant.topic_id === selectedTopicId() && variantMatchesSettings(variant))", self.script)
        self.assertIn("(status?.variants || []).filter((variant) => !variant.practice).map((variant) => variant.difficulty)", self.script)

    def test_planner_resume_ignores_practice(self):
        self.assertIn('variants.filter((item) => !item.practice && item.progress_status === "in_progress")', self.script)

    def test_session_overview_latest_score_ignores_practice(self):
        self.assertIn("latest: attempts.find((attempt) => !attempt.practice) || null", self.script)


if __name__ == "__main__":
    unittest.main()
