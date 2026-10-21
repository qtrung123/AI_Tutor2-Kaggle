"""Real-browser tests (headless Chrome, mocked API):

1. Matching Quiz review: one readable card per pair at desktop and phone widths -- the card front, the
   learner's answer and the correct answer on separate labelled lines, a short status that never
   squeezes or overlaps the text, no horizontal overflow; saved results rendered the same way by the
   assessment review card.
2. Combined Study Planner sessions on the Study Session page (Summary + Quiz, Flashcards + Quiz): Start
   opens the current step, "<Step> done" moves on with a "Continue to <Next>" action, the quiz step opens
   its own quiz (never the other step's artifact) or the Create Quiz flow, submitting the quiz completes
   the session, a refresh restores the current step, and opening a tool never completes anything.

Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import MOCK as QUIZ_MOCK, find_chrome
from tests.test_study_planner_calendar_ui import run_at_width

PUBLISH = r"""
const out = {errors: [], width: window.innerWidth};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (id) => document.getElementById(id);
"""

LONG_BACK = ("Because many processes compete for a small number of CPU cores at the same time, the operating system "
             "must decide which runnable process gets the processor next and for how long.")

MATCHING_DRIVER = "{" + PUBLISH + r"""
(async () => {
  await sleep(1200);
  // The review card as the Quiz Player shows it, with long card fronts and backs.
  const item = {
    question: "Match each card with its answer.\n1. Which component of the operating system orders the runnable processes?\n"
      + "2. What guards counters shared between threads?\n3. Why is CPU scheduling needed at all in a multiprogrammed system?",
    options: ["LONG_BACK", "A semaphore (or a mutex) that only lets one thread update the counter at a time",
      "The short-term scheduler, also called the CPU scheduler"],
    selected: ["A", "B", ""],          // wrong, right, not paired
    correct: ["C", "B", "A"],
  };
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;inset:0;overflow:auto;background:#fff;z-index:9999;padding:16px;box-sizing:border-box";
  const card = document.createElement("div");
  card.className = "quiz-player-card";
  card.style.cssText = "max-width:720px;margin:0 auto";
  const list = document.createElement("div");
  list.className = "quiz-review-options";
  list.append(...matchingReviewRows(item));
  card.appendChild(list); host.appendChild(card); document.body.appendChild(host);
  await sleep(50);
  const overlap = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  out.rows = [...list.querySelectorAll(".quiz-review-option.is-matching")].map((row) => {
    const box = row.getBoundingClientRect();
    const body = row.querySelector(".quiz-matching-review-body").getBoundingClientRect();
    const tag = row.querySelector(".quiz-review-option-tag").getBoundingClientRect();
    const values = [...row.querySelectorAll(".quiz-matching-review-value")];
    return {
      state: ["is-correct", "is-wrong", "is-unpaired"].find((name) => row.classList.contains(name)),
      prompt: row.querySelector(".quiz-matching-review-prompt").textContent,
      lines: [...row.querySelectorAll(".quiz-matching-review-line")].map((line) =>
        [line.className.replace("quiz-matching-review-line ", ""), line.querySelector(".quiz-matching-review-caption").textContent,
         line.querySelector(".quiz-matching-review-value").textContent]),
      tag: row.querySelector(".quiz-review-option-tag").textContent,
      rowWidth: Math.round(box.width), rowHeight: Math.round(box.height), bodyShare: body.width / box.width,
      minValueWidth: Math.min(...values.map((value) => value.getBoundingClientRect().width)),
      tagOverlapsText: overlap(body, tag),
      overflows: row.scrollWidth > row.clientWidth + 1,
    };
  });
  out.pageOverflow = host.scrollWidth > host.clientWidth + 1;
  // A saved result in the assessment review card uses the same pair cards.
  const question = {id: 7, question: item.question, options: item.options, question_type: "matching"};
  const reviewCard = createAssessmentReviewCard(question, {question_id: 7, selected_answer: "A,B,", correct_answers: ["C", "B", "A"],
    is_correct: false}, 0, 1);
  out.assessmentCard = {heading: reviewCard.querySelector("h3").textContent,
    pairs: reviewCard.querySelectorAll(".quiz-review-option.is-matching").length,
    plainOptions: reviewCard.querySelectorAll(".review-answer-option").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
""".replace("LONG_BACK", LONG_BACK)

PLANNER_MOCK = QUIZ_MOCK + r"""
window.__calls = [];
window.__sessions = {};
const stepTool = {summary: "summary", flashcards: "flashcards", quiz: "quiz"};
const planRow = (s) => ({session_id: s.session_id, document_id: s.document_id, activity_type: s.activity_type,
  scheduled_start: s.scheduled_start, scheduled_end: s.scheduled_end, duration_minutes: s.duration_minutes, status: s.status, steps: s.steps});
const pending = (s) => s.steps.findIndex((step) => step.status !== "completed");
const plannerFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const u = new URL(typeof input === "string" ? input : input.url, "http://x");
  const p = u.pathname;
  const method = (init.method || "GET").toUpperCase();
  if (p.startsWith("/api/planner/sessions/")) window.__calls.push(method + " " + p);
  if (p.startsWith("/api/progress/documents/")) {
    const documentId = decodeURIComponent(p.split("/").pop());
    const live = Object.values(window.__sessions).filter((s) => s.document_id === documentId
      && (s.status === "in_progress" || s.status === "scheduled"));
    const next = live.sort((a, b) => (a.status === "in_progress" ? 0 : 1) - (b.status === "in_progress" ? 0 : 1))[0];
    return json({learning: {state: "new", label: "New", explanation: "Just started."}, quiz: {latest: null, attempts: [], trend: null},
      plan: {completed_sessions: 0, planned_sessions: 1, completed_minutes: 0, remaining_minutes: 35, deadline: null,
        next_session: next ? planRow(next) : null}});
  }
  const start = p.match(/^\/api\/planner\/sessions\/([^/]+)\/start$/);
  if (start && method === "POST") {
    const s = window.__sessions[start[1]];
    const started = s.status === "scheduled";
    s.status = "in_progress";
    const index = pending(s);
    const step = s.steps[index];
    // Like the backend: the current step's own artifact (the row's artifact_id is the other step's).
    return json({session: {...s}, started, tool: stepTool[step.activity_type], artifact_available: s.quizAvailable !== false,
      prerequisite: null, current_step: index, step_activity_type: step.activity_type, step_artifact_id: step.artifact_id});
  }
  const step = p.match(/^\/api\/planner\/sessions\/([^/]+)\/steps\/(\d+)\/complete$/);
  if (step && method === "POST") {
    const s = window.__sessions[step[1]];
    s.steps[Number(step[2])].status = "completed";
    const done = pending(s) < 0;
    if (done) s.status = "completed";
    return json({session: {...s}, changed: true, session_completed: done});
  }
  const submit = p.match(/^\/api\/quiz\/([^/]+)\/submit$/);
  if (submit && method === "POST") {
    const body = JSON.parse(init.body);
    window.__submittedFor = body.planner_session_id || null;
    const response = await plannerFetch(input, init);
    const attempt = await response.json();
    const s = window.__sessions[body.planner_session_id];
    if (s && s.status === "in_progress") {
      s.steps.find((item) => item.activity_type === "quiz").status = "completed";
      if (pending(s) < 0) s.status = "completed";
      attempt.planner_completion = {status: "completed", session_id: s.session_id, changed: true, session: {...s},
        session_completed: s.status === "completed"};
    }
    return json(attempt);
  }
  return plannerFetch(input, init);
};
"""

PLANNER_DRIVER = "{" + PUBLISH + r"""
const bar = () => $("planner-step-bar");
const barState = () => ({
  visible: !bar().hidden && bar().offsetParent !== null,
  status: bar().querySelector(".planner-step-bar-status")?.textContent || null,
  steps: [...bar().querySelectorAll(".pcal-step")].map((li) => li.textContent),
  buttons: [...bar().querySelectorAll(".planner-step-bar-actions button")].map((b) => b.textContent),
  tab: document.body.dataset.sessionTab,
});
const clickBar = async (label, wait = 900) => {
  [...bar().querySelectorAll(".planner-step-bar-actions button")].find((b) => b.textContent === label).click();
  await sleep(wait);
};
const steps = (first, second, firstArtifact = null) => [
  {activity_type: first, duration_minutes: 15, artifact_id: firstArtifact, status: "pending", completed_at: null},
  {activity_type: second, duration_minutes: 20, artifact_id: null, status: "pending", completed_at: null}];
const session = (id, first, firstArtifact) => ({session_id: id, plan_id: "plan-1", document_id: "lecture.pdf", document_title: "Lecture",
  activity_type: first, artifact_id: firstArtifact, status: "scheduled", scheduled_start: "2026-01-02T10:00:00",
  scheduled_end: "2026-01-02T10:35:00", duration_minutes: 35, reason: {code: "new_material", message: "New material"},
  steps: steps(first, "quiz", firstArtifact), current_step: 0});
const quizCalls = () => window.__calls.filter((call) => /\/(complete|steps)/.test(call));
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 0, 2, 10, 5, 0);
  try { sessionStorage.clear(); } catch (error) {}

  // ---- Summary + Quiz -----------------------------------------------------------------------------
  // The row's artifact_id is the summary's: the quiz did not exist when the session was planned.
  const sq = window.__sessions["s-sq"] = session("s-sq", "summary", "summary-art-1");
  // An unrelated combined session already under way for another document, remembered in this tab.
  const other = window.__sessions["s-other"] = {...session("s-other", "summary", null), document_id: "other.pdf", status: "in_progress"};
  sessionStorage.setItem("aiTutor.plannerQuizSession", JSON.stringify({session_id: "s-other", document_id: "other.pdf"}));
  await openStudySession("lecture.pdf", "overview"); await sleep(500);
  out.scheduled = barState();                                   // not started: no bar

  // Start from the planner card: the current (first) step's tool opens; nothing is completed.
  const card = plannerSessionItem({...sq}, "div", {action: true});
  document.body.appendChild(card);
  [...card.querySelectorAll("button")].find((b) => b.textContent === "Start").click();
  await sleep(1200);
  card.remove();
  out.started = {...barState(), calls: quizCalls(), sessionStatus: sq.status};
  setSessionTab("overview"); await sleep(50);
  out.otherTab = barState();

  await clickBar("Summary done");
  out.summaryDone = {...barState(), calls: quizCalls(), sessionStatus: sq.status, toast: $("toast").textContent};

  // Refresh in a new tab: no remembered session; the server's progress restores the bar and the link.
  plannerStepBarSession = null; $("planner-step-bar").hidden = true;
  try { sessionStorage.clear(); } catch (error) {}
  await loadDocumentProgress("lecture.pdf"); await sleep(300);
  out.afterRefresh = {...barState(), remembered: plannerQuizSessionFor("lecture.pdf")};

  // Continue to the quiz: the quiz step's own (missing) artifact, never the summary's id as a quiz id.
  window.__detailRequests = [];
  const detailFetch = window.fetch;
  window.fetch = async (input, init = {}) => {
    const u = new URL(typeof input === "string" ? input : input.url, "http://x");
    if (/^\/api\/quiz\/[^/]+$/.test(u.pathname)) window.__detailRequests.push(u.searchParams.get("quiz_id"));
    return detailFetch(input, init);
  };
  await clickBar("Continue to Quiz", 1200);
  out.continued = {...barState(), calls: quizCalls(), sessionStatus: sq.status, quizIdsRequested: window.__detailRequests.filter(Boolean)};

  // Take Quiz C from the library and submit it: the server completes the quiz step and the session.
  const quizCard = [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === "Quiz C");
  quizCard.querySelector(".quiz-history-actions button").click(); await sleep(400);
  for (let index = 0; index < 3; index += 1) {
    document.querySelector(".quiz-player-answer-card")?.click(); await sleep(60);
    $("quiz-player-next").click(); await sleep(250);
  }
  await sleep(900);
  out.submitted = {...barState(), submittedFor: window.__submittedFor, sessionStatus: sq.status, toast: $("toast").textContent,
    calls: quizCalls(), quizStep: sq.steps[1].status,
    other: {status: other.status, steps: other.steps.map((item) => item.status)}};
  window.fetch = detailFetch;

  // ---- Flashcards + Quiz, no quiz yet: Continue opens the Create Quiz flow ----------------------
  const fq = window.__sessions["s-fq"] = {...session("s-fq", "flashcards", "cards-art-1"), quizAvailable: false};
  delete window.__sessions["s-sq"];
  if (typeof exitQuizPlayer === "function") { try { exitQuizPlayer(); } catch (error) {} }
  const card2 = plannerSessionItem({...fq}, "div", {action: true});
  document.body.appendChild(card2);
  [...card2.querySelectorAll("button")].find((b) => b.textContent === "Start").click();
  await sleep(1200);
  card2.remove();
  out.fqStarted = barState();
  await clickBar("Flashcards done");
  out.fqDone = {...barState(), sessionStatus: fq.status};
  await clickBar("Continue to Quiz", 1200);
  out.fqQuiz = {...barState(), sessionStatus: fq.status, dialogOpen: Boolean(document.querySelector(".quiz-create-dialog.open")),
    calls: window.__calls.filter((call) => call.includes("s-fq"))};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class MatchingReviewLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desktop = run_at_width(1280, 900, mock=QUIZ_MOCK, driver=MATCHING_DRIVER)
        cls.phone = run_at_width(390, 844, mock=QUIZ_MOCK, driver=MATCHING_DRIVER)

    def test_no_script_errors(self):
        for out in (self.desktop, self.phone):
            self.assertNotIn("fatal", out, out.get("fatal"))
            self.assertEqual(out["errors"], [])

    def test_one_card_per_pair_with_labelled_answers(self):
        wrong, right, unpaired = self.desktop["rows"]
        self.assertEqual((wrong["state"], right["state"], unpaired["state"]), ("is-wrong", "is-correct", "is-unpaired"))
        self.assertEqual(wrong["prompt"], "1. Which component of the operating system orders the runnable processes?")
        self.assertEqual(wrong["lines"], [["is-wrong", "Your answer", LONG_BACK],
                                          ["is-expected", "Correct answer", "The short-term scheduler, also called the CPU scheduler"]])
        self.assertEqual(right["lines"], [["is-correct", "Your answer",
                                           "A semaphore (or a mutex) that only lets one thread update the counter at a time"]])
        self.assertEqual(unpaired["lines"], [["is-unpaired", "Your answer", "Not paired"], ["is-expected", "Correct answer", LONG_BACK]])
        self.assertEqual([row["tag"] for row in self.desktop["rows"]], ["Incorrect", "Correct", "Not paired"])

    def test_text_is_never_squeezed_overlapped_or_overflowing(self):
        for name, out in (("desktop", self.desktop), ("phone", self.phone)):
            self.assertFalse(out["pageOverflow"], name)
            for row in out["rows"]:
                self.assertFalse(row["tagOverlapsText"], (name, row))
                self.assertFalse(row["overflows"], (name, row))
                # The text column keeps most of the card: answers wrap as lines, not word by word.
                self.assertGreater(row["bodyShare"], 0.7, (name, row))
                self.assertGreater(row["minValueWidth"], 200, (name, row))
            # Even the longest pair stays a compact card.
            self.assertLess(max(row["rowHeight"] for row in out["rows"]), 230 if name == "desktop" else 420, name)

    def test_saved_results_use_the_same_pair_cards(self):
        self.assertEqual(self.desktop["assessmentCard"], {"heading": "Match each card with its answer.", "pairs": 3, "plainOptions": 0})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CombinedPlannerSessionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=PLANNER_MOCK, driver=PLANNER_DRIVER)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out, self.out.get("fatal"))
        self.assertEqual(self.out["errors"], [])

    def test_no_bar_before_the_session_starts(self):
        self.assertFalse(self.out["scheduled"]["visible"])

    def test_start_opens_the_first_step_and_completes_nothing(self):
        started = self.out["started"]
        self.assertEqual((started["visible"], started["tab"], started["sessionStatus"]), (True, "summary", "in_progress"))
        self.assertEqual(started["status"], "Step 1 of 2: Summary — mark it done when you finish.")
        self.assertEqual(started["steps"], ["1. Summary15mNow", "2. Quiz20mNext"])
        self.assertEqual(started["buttons"], ["Summary done"])          # already on its tab
        self.assertEqual(started["calls"], [])                         # opening never completes a step
        self.assertEqual(self.out["otherTab"]["buttons"], ["Open Summary", "Summary done"])

    def test_step_done_moves_on_with_a_continue_action(self):
        done = self.out["summaryDone"]
        self.assertEqual(done["calls"], ["POST /api/planner/sessions/s-sq/steps/0/complete"])
        self.assertEqual(done["sessionStatus"], "in_progress")        # the session waits for the quiz
        self.assertEqual(done["toast"], "Summary done. Next: Quiz")
        self.assertEqual(done["steps"], ["1. Summary15mDone", "2. Quiz20mNow"])
        self.assertEqual(done["buttons"], ["Continue to Quiz"])        # no manual "Quiz done" here
        self.assertEqual(done["status"], "Step 2 of 2: Quiz — submitting the quiz completes this step.")

    def test_refresh_restores_the_current_step_and_the_quiz_link(self):
        restored = self.out["afterRefresh"]
        self.assertTrue(restored["visible"])
        self.assertEqual(restored["buttons"], ["Continue to Quiz"])
        self.assertEqual(restored["remembered"], "s-sq")

    def test_continue_opens_the_quiz_step_without_the_other_steps_artifact(self):
        continued = self.out["continued"]
        self.assertEqual(continued["tab"], "quiz")
        self.assertNotIn("summary-art-1", continued["quizIdsRequested"])
        self.assertEqual(continued["calls"], ["POST /api/planner/sessions/s-sq/steps/0/complete"])   # still nothing new
        self.assertEqual(continued["sessionStatus"], "in_progress")

    def test_submitting_the_quiz_completes_the_session(self):
        submitted = self.out["submitted"]
        self.assertEqual(submitted["submittedFor"], "s-sq")
        self.assertEqual(submitted["sessionStatus"], "completed")
        self.assertFalse(submitted["visible"])
        self.assertEqual(submitted["toast"], "Study session completed. Nice work!")
        self.assertEqual(submitted["calls"], ["POST /api/planner/sessions/s-sq/steps/0/complete"])   # no extra complete call
        self.assertEqual(submitted["quizStep"], "completed")
        # The other document's session (remembered in this tab before) is never named or touched.
        self.assertEqual(submitted["other"], {"status": "in_progress", "steps": ["pending", "pending"]})

    def test_flashcards_then_quiz_without_a_quiz_opens_create_quiz(self):
        self.assertEqual((self.out["fqStarted"]["tab"], self.out["fqStarted"]["buttons"]), ("flashcards", ["Flashcards done"]))
        self.assertEqual(self.out["fqDone"]["buttons"], ["Continue to Quiz"])
        quiz = self.out["fqQuiz"]
        self.assertEqual((quiz["tab"], quiz["dialogOpen"], quiz["sessionStatus"]), ("quiz", True, "in_progress"))
        self.assertEqual(quiz["calls"], ["POST /api/planner/sessions/s-fq/start", "POST /api/planner/sessions/s-fq/steps/0/complete",
                                         "POST /api/planner/sessions/s-fq/start"])


# ---- The step bar at phone, tablet and desktop widths -----------------------------------------------

STEP_BAR_DRIVER = "{" + PUBLISH + r"""
const bar = () => $("planner-step-bar");
const measure = () => {
  bar().scrollIntoView({block: "start"});
  const box = bar().getBoundingClientRect();
  const buttons = [...bar().querySelectorAll(".planner-step-bar-actions button")];
  const rects = buttons.map((b) => b.getBoundingClientRect());
  const inside = (r) => r.left >= box.left - 1 && r.right <= box.right + 1 && r.top >= box.top - 1 && r.bottom <= box.bottom + 1;
  const overlap = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  const hit = (b, r) => { const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return el === b || b.contains(el); };
  return {
    visible: !bar().hidden && box.width > 0,
    status: bar().querySelector(".planner-step-bar-status").textContent,
    steps: [...bar().querySelectorAll(".pcal-step")].map((li) => li.textContent),
    buttons: buttons.map((b) => b.textContent),
    barInViewport: box.left >= -1 && box.right <= window.innerWidth + 1,
    barOverflows: bar().scrollWidth > bar().clientWidth + 1,
    pageOverflows: document.documentElement.scrollWidth > window.innerWidth + 1,
    buttonsInside: rects.every(inside),
    buttonsOverlap: rects.some((a, i) => rects.some((b, j) => i < j && overlap(a, b))),
    textClipped: buttons.some((b) => b.scrollWidth > b.clientWidth + 1),
    minButtonHeight: Math.min(...rects.map((r) => r.height)),
    clickable: buttons.every((b, i) => hit(b, rects[i])),
    stepsInside: [...bar().querySelectorAll(".pcal-step")].every((li) => inside(li.getBoundingClientRect())),
  };
};
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 0, 2, 10, 5, 0);
  const steps = [
    {activity_type: "flashcards", duration_minutes: 15, artifact_id: null, status: "pending", completed_at: null},
    {activity_type: "quiz", duration_minutes: 20, artifact_id: null, status: "pending", completed_at: null}];
  const s = window.__sessions["s-bar"] = {session_id: "s-bar", plan_id: "plan-1", document_id: "lecture.pdf", document_title: "Lecture",
    activity_type: "flashcards", artifact_id: null, status: "scheduled", scheduled_start: "2026-01-02T10:00:00",
    scheduled_end: "2026-01-02T10:35:00", duration_minutes: 35, reason: {code: "new_material", message: "New material"}, steps, current_step: 0};
  await openStudySession("lecture.pdf", "overview"); await sleep(400);
  const card = plannerSessionItem({...s}, "div", {action: true});
  document.body.appendChild(card);
  [...card.querySelectorAll("button")].find((b) => b.textContent === "Start").click();
  await sleep(1200);
  card.remove();
  out.onStepTab = measure();                                    // one button: "Flashcards done"
  setSessionTab("overview"); await sleep(80);
  out.otherTab = measure();                                     // "Open Flashcards" + "Flashcards done"
  [...bar().querySelectorAll("button")].find((b) => b.textContent === "Flashcards done").click();
  await sleep(1000);
  out.continue = measure();                                     // "Continue to Quiz"
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class StepBarResponsiveTests(unittest.TestCase):
    WIDTHS = ((390, 844), (768, 1024), (1280, 900))

    @classmethod
    def setUpClass(cls):
        cls.outs = {width: run_at_width(width, height, mock=PLANNER_MOCK, driver=STEP_BAR_DRIVER) for width, height in cls.WIDTHS}

    def test_no_script_errors(self):
        for width, out in self.outs.items():
            self.assertNotIn("fatal", out, (width, out.get("fatal")))
            self.assertEqual(out["errors"], [], width)

    def test_content_is_right_at_every_width(self):
        for width, out in self.outs.items():
            self.assertEqual(out["onStepTab"]["buttons"], ["Flashcards done"], width)
            self.assertEqual(out["otherTab"]["buttons"], ["Open Flashcards", "Flashcards done"], width)
            self.assertEqual(out["continue"]["buttons"], ["Continue to Quiz"], width)
            self.assertEqual(out["continue"]["steps"], ["1. Flashcards15mDone", "2. Quiz20mNow"], width)
            self.assertEqual(out["continue"]["status"], "Step 2 of 2: Quiz — submitting the quiz completes this step.", width)

    def test_no_overflow_overlap_or_clipped_controls(self):
        for width, out in self.outs.items():
            for state in ("onStepTab", "otherTab", "continue"):
                m = out[state]
                where = (width, state, m)
                self.assertTrue(m["visible"], where)
                self.assertTrue(m["barInViewport"], where)
                self.assertFalse(m["barOverflows"], where)
                self.assertFalse(m["pageOverflows"], where)
                self.assertTrue(m["buttonsInside"], where)
                self.assertTrue(m["stepsInside"], where)
                self.assertFalse(m["buttonsOverlap"], where)
                self.assertFalse(m["textClipped"], where)
                self.assertGreaterEqual(m["minButtonHeight"], 32, where)
                self.assertTrue(m["clickable"], where)


# ---- Matching in a saved attempt's history review ---------------------------------------------------

HISTORY_MOCK = QUIZ_MOCK + r"""
const historyFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/quiz-history/att-matching") {
    return json({attempt_id: "att-matching", quiz_id: "fc-match", document_id: "lecture.pdf", difficulty: "easy", completed: true,
      score: 0, total: 1, percentage: 0, completed_at: "2026-01-01T00:02:00Z",
      question_results: [{question_id: 1, question_type: "matching",
        question: "Match each card with its answer.\n1. Which component of the operating system orders the runnable processes?\n"
          + "2. What guards counters shared between threads?\n3. Why is CPU scheduling needed at all in a multiprogrammed system?",
        options: ["LONG_BACK", "A semaphore (or a mutex) that only lets one thread update the counter at a time",
          "The short-term scheduler, also called the CPU scheduler"],
        // Saved as the backend stores positional answers: joined letters, one per prompt ("" = not paired).
        selected_answer: "A,B,", selected_answers: [], correct_answer: "C,B,A", correct_answers: ["C", "B", "A"],
        is_correct: false, explanation: "Pairs from your flashcards."}]});
  }
  return historyFetch(input, init);
};
""".replace("LONG_BACK", LONG_BACK)

HISTORY_DRIVER = "{" + PUBLISH + r"""
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz"); await sleep(400);
  await showQuizHistoryDetail("att-matching"); await sleep(200);
  const detail = $("quiz-history-detail");
  detail.scrollIntoView({block: "start"});
  const overlap = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  out.heading = detail.querySelector(".quiz-review-card h3")?.textContent || null;
  out.plainOptions = detail.querySelectorAll(".review-answer-option").length;
  out.rows = [...detail.querySelectorAll(".quiz-review-option.is-matching")].map((row) => {
    const box = row.getBoundingClientRect();
    const body = row.querySelector(".quiz-matching-review-body").getBoundingClientRect();
    const tag = row.querySelector(".quiz-review-option-tag").getBoundingClientRect();
    return {
      state: ["is-correct", "is-wrong", "is-unpaired"].find((name) => row.classList.contains(name)),
      lines: [...row.querySelectorAll(".quiz-matching-review-line")].map((line) =>
        [line.querySelector(".quiz-matching-review-caption").textContent, line.querySelector(".quiz-matching-review-value").textContent]),
      tag: row.querySelector(".quiz-review-option-tag").textContent,
      bodyShare: body.width / box.width, rowHeight: Math.round(box.height),
      minValueWidth: Math.min(...[...row.querySelectorAll(".quiz-matching-review-value")].map((v) => v.getBoundingClientRect().width)),
      tagOverlapsText: overlap(body, tag), overflows: row.scrollWidth > row.clientWidth + 1,
    };
  });
  out.pageOverflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class MatchingHistoryReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outs = {width: run_at_width(width, height, mock=HISTORY_MOCK, driver=HISTORY_DRIVER)
                    for width, height in ((390, 844), (1280, 900))}

    def test_no_script_errors(self):
        for width, out in self.outs.items():
            self.assertNotIn("fatal", out, (width, out.get("fatal")))
            self.assertEqual(out["errors"], [], width)

    def test_a_saved_matching_attempt_shows_one_card_per_pair(self):
        out = self.outs[1280]
        self.assertEqual(out["heading"], "Match each card with its answer.")
        self.assertEqual(out["plainOptions"], 0)                   # never listed as multiple-choice options
        self.assertEqual([row["state"] for row in out["rows"]], ["is-wrong", "is-correct", "is-unpaired"])
        self.assertEqual(out["rows"][0]["lines"], [["Your answer", LONG_BACK],
                                                   ["Correct answer", "The short-term scheduler, also called the CPU scheduler"]])
        self.assertEqual(out["rows"][2]["lines"], [["Your answer", "Not paired"], ["Correct answer", LONG_BACK]])
        self.assertEqual([row["tag"] for row in out["rows"]], ["Incorrect", "Correct", "Not paired"])

    def test_long_answers_stay_readable_at_desktop_and_phone_widths(self):
        for width, out in self.outs.items():
            self.assertFalse(out["pageOverflow"], width)
            self.assertEqual(len(out["rows"]), 3, width)
            for row in out["rows"]:
                self.assertFalse(row["tagOverlapsText"], (width, row))
                self.assertFalse(row["overflows"], (width, row))
                self.assertGreater(row["bodyShare"], 0.7, (width, row))
                self.assertGreater(row["minValueWidth"], 180, (width, row))
                self.assertLess(row["rowHeight"], 240 if width == 1280 else 460, (width, row))


if __name__ == "__main__":
    unittest.main()
