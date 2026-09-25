"""Real-browser tests for Quiz Results + Review Answers (frontend/app.js in headless Chrome against a
mocked API). Covers: the Library's Review Answers opening the Results screen for the exact
completed attempt (never question-taking mode, never a sibling quiz), score/correct/incorrect/
unanswered counts taken from the persisted grading, Review Answers navigation, the correct/
incorrect/unanswered visual states, explanation/source rendering (including missing values),
Back to Results, Back to Quizzes, and that Start + Finish (Submit Anyway) still work and now land
on Results.

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
window.__historyDetailRequests = [];
window.__submitBodies = [];
window.__corruptNextDetail = false;
const OPTIONS = ["A. alpha", "B. beta", "C. gamma", "D. delta"];
function questions(prefix) {
  return [
    {id: 1, question: prefix + " question one?", options: OPTIONS, correct_answer: "A", question_type: "single_choice", topic_id: "document", topic_name: "Topic 1", concept_name: "Concept Alpha", difficulty: "easy", explanation: "Alpha is stated in section 1.", source_chunk_ids: ["h1", "h2"]},
    {id: 2, question: prefix + " question two?", options: OPTIONS, correct_answer: "B", question_type: "single_choice", topic_id: "document", topic_name: "Topic 1", concept_name: "", difficulty: "easy", explanation: "Beta follows from the definition.", source_chunk_ids: ["h3"]},
    {id: 3, question: prefix + " question three?", options: OPTIONS, correct_answer: "C", question_type: "single_choice", topic_id: "document", topic_name: "Topic 2", concept_name: "", difficulty: "easy", explanation: "", source_chunk_ids: ["h4"]},
    {id: 4, question: prefix + " question four?", options: OPTIONS, correct_answer: "D", question_type: "single_choice", topic_id: "document", topic_name: "", concept_name: "", difficulty: "easy", explanation: "Delta closes the list.", source_chunk_ids: []},
  ];
}
function makeQuiz(quizId, title, modelId, modelName, partial) {
  return {quiz_id: quizId, document_id: "lecture.pdf", title, difficulty: "easy", topic_id: "document", topic_name: "",
    assessment_scope: "document", generation_model: {model_id: modelId, name: modelName}, questions: questions(title),
    assessment_plan: partial ? {partial: true, requested_count: 5, actual_count: 4} : {}, created_at: "2026-01-01T00:00:00Z"};
}
window.__quizzes = {
  "quiz-x": makeQuiz("quiz-x", "Quiz X", "qwen-2.5-7b", "Qwen 2.5 7B", true),
  "quiz-y": makeQuiz("quiz-y", "Quiz Y", "gemma3-12b", "Gemma 3 12B", false),
  "quiz-z": makeQuiz("quiz-z", "Quiz Z", "qwen-2.5-7b", "Qwen 2.5 7B", false),
};
// Server-side grading, as backend/quiz_attempt_service.submit_quiz_attempt persists it (unanswered ->
// empty selection, not correct).
function grade(quiz, answers, attemptId, attemptNumber) {
  let score = 0;
  const question_results = quiz.questions.map((question) => {
    const selected = answers[String(question.id)] || "";
    const is_correct = selected === question.correct_answer;
    if (is_correct) score += 1;
    return {question_id: question.id, question: question.question, options: question.options,
      selected_answer: selected, selected_answers: selected ? [selected] : [], correct_answer: question.correct_answer,
      correct_answers: [question.correct_answer], question_type: "single_choice", is_correct,
      question_difficulty: "easy", validation_outcome: "accepted", concept_plan_id: "plan-internal-1",
      evidence_requirement_version: "concept_coverage_v1", topic_id: "document", topic_name: question.topic_name,
      explanation: question.explanation, source_chunk_ids: question.source_chunk_ids};
  });
  const total = quiz.questions.length;
  const answered = Object.values(answers).filter(Boolean);
  return {attempt_id: attemptId, quiz_id: quiz.quiz_id, document_id: "lecture.pdf", difficulty: "easy", topic_id: "document",
    completed: true, completed_at: "2026-01-02T00:00:00Z", score, total, answered: answered.length,
    percentage: Math.round((10000 * score) / total) / 100, attempt_number: attemptNumber, current_question_index: 0,
    answers: Object.fromEntries(Object.entries(answers).filter(([, value]) => value)), question_results};
}
window.__attempts = {
  "att-x1": grade(window.__quizzes["quiz-x"], {"1": "A", "2": "C", "4": "D"}, "att-x1", 1),
  "att-y1": grade(window.__quizzes["quiz-y"], {"1": "A", "2": "B", "3": "C", "4": "D"}, "att-y1", 1),
};
function summary(attempt) {
  const quiz = window.__quizzes[attempt.quiz_id];
  return {attempt_id: attempt.attempt_id, quiz_id: attempt.quiz_id, document_id: "lecture.pdf", title: quiz.title,
    difficulty: "easy", topic_id: "document", topic_name: "", score: attempt.score, total: attempt.total,
    percentage: Math.round(attempt.percentage), attempt_number: attempt.attempt_number, completed_at: attempt.completed_at};
}
function detail(attempt) {
  const quiz = window.__quizzes[attempt.quiz_id];
  const concepts = Object.fromEntries(quiz.questions.map((question) => [question.id, question.concept_name]));
  return {...attempt,
    question_results: attempt.question_results.map((result) => ({...result, concept_name: concepts[result.question_id] || ""})),
    quiz: {quiz_id: quiz.quiz_id, title: quiz.title, difficulty: quiz.difficulty, generation_model: quiz.generation_model,
      partial: Boolean(quiz.assessment_plan.partial), requested_count: quiz.assessment_plan.requested_count || null, question_count: quiz.questions.length}};
}
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [
    {id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: true},
    {id: "gemma3-12b", label: "Gemma 3 12B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) return json({status: "ready", ready: true});
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: true,
    variants: Object.values(window.__quizzes).map((quiz) => {
      const done = Object.values(window.__attempts).some((attempt) => attempt.quiz_id === quiz.quiz_id);
      return {quiz_id: quiz.quiz_id, title: quiz.title, topic_id: "document", topic_name: "", assessment_scope: "document",
        difficulty: "easy", question_count: 4, requested_count: 4, status: "complete",
        model_id: quiz.generation_model.model_id, model_name: quiz.generation_model.name,
        created_at: quiz.created_at, updated_at: quiz.created_at,
        progress_status: done ? "completed" : "not_started", answered: 0, total: 4, score: null, percentage: null};
    })}]);
  if (p === "/api/quiz-history") return json(Object.values(window.__attempts).map(summary));
  const historyMatch = p.match(/^\/api\/quiz-history\/([^/]+)$/);
  if (historyMatch) {
    const attemptId = decodeURIComponent(historyMatch[1]);
    window.__historyDetailRequests.push(attemptId);
    const attempt = window.__attempts[attemptId];
    if (!attempt) return json({detail: "Quiz history attempt was not found."}, 404);
    const payload = detail(attempt);
    if (window.__corruptNextDetail) { window.__corruptNextDetail = false; payload.quiz_id = "quiz-y"; }
    return json(payload);
  }
  const progressMatch = p.match(/^\/api\/quiz\/([^/]+)\/progress$/);
  if (progressMatch && method === "PATCH") {
    const body = JSON.parse(init.body);
    return json({attempt_id: "live-" + body.quiz_id, quiz_id: body.quiz_id, answers: body.answers || {}, completed: false,
      completed_at: null, answered: Object.keys(body.answers || {}).length, total: 4, current_question_index: body.current_question_index || 0});
  }
  const submitMatch = p.match(/^\/api\/quiz\/([^/]+)\/submit$/);
  if (submitMatch && method === "POST") {
    const body = JSON.parse(init.body);
    window.__submitBodies.push(body);
    const attemptId = "att-" + body.quiz_id + "-" + window.__submitBodies.length;
    const attempt = grade(window.__quizzes[body.quiz_id], body.answers, attemptId, 1);
    window.__attempts[attemptId] = attempt;
    return json({...attempt, attempt_summary: {attempts: 1, latest_score: attempt.percentage, best_score: attempt.percentage, average_score: attempt.percentage}});
  }
  const quizMatch = p.match(/^\/api\/quiz\/([^/]+)$/);
  if (quizMatch && method === "GET") {
    const quiz = window.__quizzes[u.searchParams.get("quiz_id")] || null;
    return json({document_id: quizMatch[1], difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"), quiz, latest_attempt: null, attempt_summary: null});
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
const pane = () => document.querySelector('[data-session-pane="quiz"]');
const playerOpen = () => pane().classList.contains("quiz-player-open");
const visible = (id) => !$(id).hidden;
const historyCard = (title) => [...document.querySelectorAll(".quiz-history-card")].find((c) => c.querySelector("strong")?.textContent === title);
const reviewButtonFor = (title) => [...historyCard(title).querySelectorAll("button")].find((b) => b.textContent === "Review Answers");
const savedStartFor = (title) => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === title)?.querySelector(".quiz-history-actions button");
const results = () => ({
  visible: visible("quiz-results-view"), questionViewHidden: $("quiz-player-question-view").hidden, reviewHidden: $("quiz-review-view").hidden,
  title: $("quiz-results-title").textContent, difficulty: $("quiz-results-difficulty").textContent,
  model: $("quiz-results-model").hidden ? "" : $("quiz-results-model").textContent,
  score: $("quiz-results-score").textContent, percentage: $("quiz-results-percentage").textContent,
  correct: $("quiz-results-correct").textContent, incorrect: $("quiz-results-incorrect").textContent,
  unanswered: $("quiz-results-unanswered").textContent, note: $("quiz-results-note").hidden ? "" : $("quiz-results-note").textContent,
});
const review = () => ({
  visible: visible("quiz-review-view"), position: $("quiz-review-position").textContent, status: $("quiz-review-status").textContent,
  question: $("quiz-review-question").textContent, unansweredNote: visible("quiz-review-unanswered-note"),
  options: [...document.querySelectorAll("#quiz-review-options .quiz-review-option")].map((row) => ({
    letter: row.dataset.letter, text: row.querySelector(".quiz-review-option-label").textContent,
    classes: ["is-correct", "is-selected", "is-wrong"].filter((c) => row.classList.contains(c)),
    tag: row.querySelector(".quiz-review-option-tag")?.textContent || "",
  })),
  explanation: $("quiz-review-explanation").textContent, explanationMissing: $("quiz-review-explanation").classList.contains("is-missing"),
  source: visible("quiz-review-source") ? $("quiz-review-source-text").textContent : null,
  previousDisabled: $("quiz-review-previous").disabled, nextLabel: $("quiz-review-next").textContent,
  playerText: $("quiz-player").textContent,
});
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(400);

  // 1) Library Review Answers on a completed quiz opens Results for that exact attempt.
  out.libraryBefore = { hasHistoryX: Boolean(historyCard("Quiz X")), hasHistoryY: Boolean(historyCard("Quiz Y")) };
  reviewButtonFor("Quiz X").click();
  await sleep(300);
  out.resultsX = { ...results(), playerOpen: playerOpen(),
    libraryHidden: getComputedStyle(document.querySelector(".assessment-shell")).display === "none",
    detailRequests: [...window.__historyDetailRequests] };

  // 2) Review Answers: one question at a time with correct / incorrect / unanswered states.
  $("quiz-results-review").click(); await sleep(50);
  out.review1 = review();
  $("quiz-review-next").click(); await sleep(50);
  out.review2 = review();
  $("quiz-review-next").click(); await sleep(50);
  out.review3 = review();
  $("quiz-review-next").click(); await sleep(50);
  out.review4 = review();
  $("quiz-review-previous").click(); await sleep(50);
  out.afterPrevious = { position: $("quiz-review-position").textContent };

  // 3) Back to Results (header button), and Next on the last question also returns to Results.
  $("quiz-review-back-results").click(); await sleep(50);
  out.afterBackToResults = { resultsVisible: visible("quiz-results-view"), reviewHidden: $("quiz-review-view").hidden };
  $("quiz-results-review").click(); await sleep(50);
  out.reviewRestartsAtFirst = $("quiz-review-position").textContent;
  for (let i = 0; i < 4; i += 1) { $("quiz-review-next").click(); await sleep(30); }
  out.afterLastNext = { resultsVisible: visible("quiz-results-view") };

  // Unrelated re-renders must not replace Results with the question view.
  renderAssessmentQuiz(); await sleep(50);
  out.afterRerender = { resultsVisible: visible("quiz-results-view"), questionViewHidden: $("quiz-player-question-view").hidden };

  // 4) Back to Quizzes returns to the Library.
  $("quiz-results-back").click(); await sleep(300);
  out.afterBackToQuizzes = { playerOpen: playerOpen(), isLanding: pane().classList.contains("quiz-landing"), hasHistoryX: Boolean(historyCard("Quiz X")) };

  // 5) Sibling isolation: Quiz Y's Results show only Quiz Y's own attempt.
  reviewButtonFor("Quiz Y").click(); await sleep(300);
  out.resultsY = { ...results(), lastDetailRequest: window.__historyDetailRequests.at(-1) };
  $("quiz-results-review").click(); await sleep(50);
  out.reviewY1 = review();
  $("quiz-review-back-results").click(); await sleep(50);
  $("quiz-results-back").click(); await sleep(300);

  // 6) No fallback: an attempt whose quiz_id does not match the card's quiz is refused.
  window.__corruptNextDetail = true;
  reviewButtonFor("Quiz X").click(); await sleep(300);
  out.mismatch = { playerOpen: playerOpen(), resultsVisible: visible("quiz-results-view") && playerOpen() };

  // 7) Start (no regression) -> answer one -> Finish -> Submit Anyway -> Results with unanswered.
  const start = savedStartFor("Quiz Z");
  out.startLabel = start && start.textContent;
  start.click(); await sleep(300);
  out.playerStarted = { playerOpen: playerOpen(), questionView: visible("quiz-player-question-view"), position: $("quiz-player-position").textContent };
  [...document.querySelectorAll(".quiz-player-answer-card")][0].click();
  await sleep(50);
  for (let i = 0; i < 3; i += 1) { $("quiz-player-next").click(); await sleep(30); }
  $("quiz-player-next").click(); await sleep(50);   // Finish Quiz -> 3 unanswered
  out.finishConfirm = $("quiz-finish-confirm-title").textContent;
  $("quiz-finish-submit-anyway").click(); await sleep(500);
  out.submitBody = window.__submitBodies.at(-1);
  out.resultsZ = results();
  $("quiz-results-review").click(); await sleep(50);
  $("quiz-review-next").click(); await sleep(50);
  out.reviewZ2 = review();
  $("quiz-review-back-results").click(); await sleep(50);
  $("quiz-results-back").click(); await sleep(300);
  out.finalLibrary = {
    playerOpen: playerOpen(),
    createHeaderVisible: getComputedStyle(document.querySelector(".quiz-landing-header")).display !== "none",
    hasHistoryZ: Boolean(historyCard("Quiz Z")),
  };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizResultsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_results_"))
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

    def test_library_review_opens_results_for_the_exact_completed_attempt(self):
        self.assertTrue(self.out["libraryBefore"]["hasHistoryX"])
        res = self.out["resultsX"]
        self.assertTrue(res["playerOpen"])
        self.assertTrue(res["libraryHidden"])
        self.assertTrue(res["visible"])
        self.assertTrue(res["questionViewHidden"])   # never question-taking mode
        self.assertTrue(res["reviewHidden"])
        self.assertEqual(res["detailRequests"], ["att-x1"])

    def test_results_show_title_meta_score_and_counts_from_persisted_grading(self):
        res = self.out["resultsX"]
        self.assertEqual(res["title"], "Quiz X")
        self.assertEqual(res["difficulty"], "easy")
        self.assertEqual(res["model"], "Generated by Qwen 2.5 7B")
        self.assertEqual(res["score"], "2 / 4")
        self.assertEqual(res["percentage"], "50%")
        self.assertEqual((res["correct"], res["incorrect"], res["unanswered"]), ("2", "1", "1"))

    def test_partial_quiz_count_is_noted(self):
        self.assertEqual(self.out["resultsX"]["note"], "This quiz has 4 of the 5 requested questions.")
        self.assertEqual(self.out["resultsY"]["note"], "")

    def test_review_correct_answer_state_with_explanation_and_source(self):
        q1 = self.out["review1"]
        self.assertTrue(q1["visible"])
        self.assertEqual(q1["position"], "Question 1 of 4")
        self.assertEqual(q1["status"], "Correct")
        self.assertEqual(q1["question"], "Quiz X question one?")
        self.assertEqual(len(q1["options"]), 4)
        self.assertEqual(q1["options"][0]["classes"], ["is-correct", "is-selected"])
        self.assertEqual(q1["options"][0]["tag"], "Your answer · Correct")
        self.assertTrue(all(not option["classes"] for option in q1["options"][1:]))
        self.assertEqual(q1["explanation"], "Alpha is stated in section 1.")
        self.assertEqual(q1["source"], "Topic: Topic 1 · Concept: Concept Alpha · Based on 2 passages from the document")
        self.assertTrue(q1["previousDisabled"])
        self.assertEqual(q1["nextLabel"], "Next")

    def test_review_incorrect_answer_marks_selected_and_correct_options(self):
        q2 = self.out["review2"]
        self.assertEqual(q2["position"], "Question 2 of 4")
        self.assertEqual(q2["status"], "Incorrect")
        by_letter = {option["letter"]: option for option in q2["options"]}
        self.assertEqual(by_letter["C"]["classes"], ["is-selected", "is-wrong"])
        self.assertEqual(by_letter["C"]["tag"], "Your answer")
        self.assertEqual(by_letter["B"]["classes"], ["is-correct"])
        self.assertEqual(by_letter["B"]["tag"], "Correct answer")
        self.assertFalse(q2["unansweredNote"])

    def test_review_unanswered_state_and_missing_explanation(self):
        q3 = self.out["review3"]
        self.assertEqual(q3["status"], "Unanswered")
        self.assertTrue(q3["unansweredNote"])
        self.assertTrue(all("is-selected" not in option["classes"] for option in q3["options"]))
        correct = [option for option in q3["options"] if "is-correct" in option["classes"]]
        self.assertEqual([(option["letter"], option["tag"]) for option in correct], [("C", "Correct answer")])
        self.assertTrue(q3["explanationMissing"])
        self.assertIn("No explanation", q3["explanation"])

    def test_review_missing_source_is_hidden_and_last_next_returns_to_results(self):
        q4 = self.out["review4"]
        self.assertEqual(q4["position"], "Question 4 of 4")
        self.assertIsNone(q4["source"])
        self.assertEqual(q4["nextLabel"], "Back to Results")
        self.assertEqual(self.out["afterPrevious"]["position"], "Question 3 of 4")
        self.assertTrue(self.out["afterLastNext"]["resultsVisible"])

    def test_review_never_exposes_internal_validation_fields(self):
        for key in ("review1", "review2", "review3", "review4"):
            text = self.out[key]["playerText"]
            for internal in ("accepted", "plan-internal-1", "concept_coverage_v1", "h1", "validation"):
                self.assertNotIn(internal, text)

    def test_back_to_results_and_rerender_keep_results(self):
        self.assertEqual(self.out["afterBackToResults"], {"resultsVisible": True, "reviewHidden": True})
        self.assertEqual(self.out["reviewRestartsAtFirst"], "Question 1 of 4")
        self.assertEqual(self.out["afterRerender"], {"resultsVisible": True, "questionViewHidden": True})

    def test_back_to_quizzes_returns_to_the_library(self):
        self.assertEqual(self.out["afterBackToQuizzes"], {"playerOpen": False, "isLanding": True, "hasHistoryX": True})

    def test_sibling_quiz_results_are_isolated(self):
        res = self.out["resultsY"]
        self.assertEqual(res["lastDetailRequest"], "att-y1")
        self.assertEqual(res["title"], "Quiz Y")
        self.assertEqual(res["model"], "Generated by Gemma 3 12B")
        self.assertEqual(res["score"], "4 / 4")
        self.assertEqual((res["correct"], res["incorrect"], res["unanswered"]), ("4", "0", "0"))
        self.assertEqual(self.out["reviewY1"]["question"], "Quiz Y question one?")

    def test_mismatched_attempt_never_falls_back_to_another_quiz(self):
        self.assertEqual(self.out["mismatch"], {"playerOpen": False, "resultsVisible": False})

    def test_start_still_opens_question_taking_mode(self):
        self.assertEqual(self.out["startLabel"], "Start")
        self.assertEqual(self.out["playerStarted"], {"playerOpen": True, "questionView": True, "position": "Question 1 of 4"})

    def test_submit_anyway_sends_exact_quiz_and_lands_on_results_with_unanswered(self):
        self.assertEqual(self.out["finishConfirm"], "3 questions unanswered")
        body = self.out["submitBody"]
        self.assertEqual(body["quiz_id"], "quiz-z")
        self.assertTrue(body["allow_unanswered"])
        self.assertEqual(body["answers"], {"1": "A"})
        res = self.out["resultsZ"]
        self.assertTrue(res["visible"])
        self.assertEqual(res["title"], "Quiz Z")
        self.assertEqual(res["score"], "1 / 4")
        self.assertEqual(res["percentage"], "25%")
        self.assertEqual((res["correct"], res["incorrect"], res["unanswered"]), ("1", "0", "3"))
        self.assertEqual(self.out["reviewZ2"]["status"], "Unanswered")

    def test_library_and_create_entry_point_after_returning(self):
        final = self.out["finalLibrary"]
        self.assertFalse(final["playerOpen"])
        self.assertTrue(final["createHeaderVisible"])
        self.assertTrue(final["hasHistoryZ"])


if __name__ == "__main__":
    unittest.main()
