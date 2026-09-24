"""Real-browser tests: the desktop Study Planner explains its plan from real state, with no extra
setup step (headless Chrome; the placement-aware planner mock plus the document progress endpoint).

- Add materials and the rail show each document's learning state, Study Pack contents and latest
  quiz -- missing parts said plainly, nothing invented;
- a suggested session's popover says why (the scheduler's reason in plain words) and the deadline;
- "How this plan was built" lists only the factors that have data.
"Now" is pinned to Thu 2026-09-24 12:00. Runs at 1280px and 1100px. Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_drag_ui import MOCK as DRAG_MOCK
from tests.test_study_planner_calendar_ui import run_at_width

# Document progress as /api/progress/documents/{id} returns it (DocumentStudyState underneath).
MOCK = DRAG_MOCK + r"""
window.__progress = {
  "mkt.pdf": {document_id: "mkt.pdf", title: "Marketing", learning: {state: "new", label: "Not started", explanation: "Not studied yet."},
    quiz: {latest: null, attempts: [], attempt_count: 0, trend: null}, flashcards: {card_count: 18},
    study_pack: {summary_ready: true, flashcard_count: 18, quiz_count: 1}, plan: null},
  "stats.pdf": {document_id: "stats.pdf", title: "Statistics", learning: {state: "needs_review", label: "Needs review", explanation: "x"},
    quiz: {latest: {score: 5, total: 12, percentage: 41.67, completed_at: "2026-09-20T10:00:00Z"}, attempts: [], attempt_count: 1, trend: null},
    flashcards: {card_count: 0}, study_pack: {summary_ready: true, flashcard_count: 0, quiz_count: 2}, plan: null},
  "pbi.pdf": {document_id: "pbi.pdf", title: "PowerBI", learning: {state: "on_track", label: "On track", explanation: "x"},
    quiz: {latest: {score: 11, total: 12, percentage: 88, completed_at: "2026-09-21T10:00:00Z"}, attempts: [], attempt_count: 1, trend: null},
    flashcards: {card_count: 12}, study_pack: {summary_ready: false, flashcard_count: 12, quiz_count: 1}, plan: null},
};
window.__progressCalls = 0;
const dragFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const m = p.match(/^\/api\/progress\/documents\/([^/]+)$/);
  if (m) { window.__progressCalls += 1; const found = window.__progress[decodeURIComponent(m[1])];
    return found ? json(found) : json({detail: "Document not found."}, 404); }
  return dragFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: [], width: window.innerWidth, overflow: {}};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const P = window.__planner;
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("planner-workspace").getBoundingClientRect().bottom <= window.innerHeight + 1
  && (!$("pcal-popover").hidden ? $("pcal-popover").getBoundingClientRect().right <= window.innerWidth + 1 : true);
const rail = () => [...document.querySelectorAll(".pcal-material")].map((m) => ({title: m.querySelector(".pcal-material-title").textContent,
  pill: m.querySelector(".pcal-pill").textContent, due: m.querySelector(".pcal-deadline-button").textContent,
  pack: m.querySelector(".pcal-material-pack")?.textContent || null}));
const sheet = () => [...document.querySelectorAll(".pcal-sheet-row")].map((r) => ({title: r.querySelector(".pcal-sheet-title").textContent,
  pill: r.querySelector(".pcal-pill")?.textContent || null, quiz: r.querySelector(".pcal-sheet-quiz")?.textContent || null,
  pack: r.querySelector(".pcal-sheet-pack")?.textContent || null, deadline: r.querySelector(".pcal-deadline-button").textContent}));
const factors = () => Object.fromEntries([...$("pcal-popover").querySelectorAll(".pcal-factor")].map((f) => [f.querySelector("dt").textContent, f.querySelector("dd").textContent]));
const ghost = (title, activity) => [...document.querySelectorAll(".pcal-event--suggested")].find((e) => e.querySelector(".pcal-event-title").textContent === title
  && e.querySelector(".pcal-event-meta").textContent.startsWith(activity));
const popover = () => ({pill: $("pcal-popover").querySelector(".pcal-kind")?.textContent, title: $("pcal-popover").querySelector(".pcal-popover-title")?.textContent,
  activity: $("pcal-popover").querySelector(".pcal-popover-activity")?.textContent, when: $("pcal-popover").querySelector(".pcal-popover-when")?.textContent,
  reason: $("pcal-popover").querySelector(".pcal-popover-reason")?.textContent,
  notes: [...$("pcal-popover").querySelectorAll(".pcal-popover-note")].map((n) => n.textContent),
  hint: $("pcal-popover").querySelector(".pcal-popover-hint")?.textContent || null, text: $("pcal-popover").textContent});
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: "2026-10-01", learning_state: "new"}];
  P.availability = [0, 1, 2].map((day) => ({availability_id: "a" + day, start_at: "18:00", end_at: "22:00", is_recurring: true, day_of_week: day, date: null}));
  setPage("planner"); await sleep(900);

  // 1. The rail: real learning state, deadline and one short Study Pack line.
  out.railFirst = rail();
  out.explainShown = !$("pcal-explain").hidden;
  $("pcal-explain").click(); await sleep(100);
  out.firstFactors = factors();
  out.firstExplainText = $("pcal-popover").textContent;
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);

  // 2. Add materials: each document's real state before choosing it.
  $("pcal-add-materials").click(); await sleep(150);
  out.sheet = sheet();
  out.overflow.sheet = noOverflow();
  document.querySelector('.pcal-sheet-row[data-document-id="stats.pdf"] .pcal-sheet-add').click(); await sleep(400);
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(900);
  out.railAfter = rail();

  // 3. A suggested session explains itself (next week holds the suggestions).
  $("pcal-next").click(); await sleep(100);
  $("pcal-scroll").scrollTop = 17 * 48;
  ghost("Marketing", "Summary").click(); await sleep(100);
  out.deadlineGhost = popover();
  ghost("Statistics", "Summary").click(); await sleep(100);
  out.newGhost = popover();
  out.overflow.ghost = noOverflow();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);

  // 4. How this plan was built, now with a quiz result in play.
  $("pcal-explain").click(); await sleep(100);
  out.factors = factors();
  out.explainText = $("pcal-popover").textContent;
  out.overflow.explain = noOverflow();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);

  // 5. The low-friction flow is unchanged: suggestions appeared by themselves; Accept confirms once.
  out.previewCalls = P.previewBodies.length;
  $("pcal-accept").click(); await sleep(700);
  out.accepted = {confirms: P.confirmBodies.length, kinds: [...document.querySelectorAll(".pcal-event")].map((e) => e.dataset.kind)};
  out.stillExplained = !$("pcal-explain").hidden;
  out.progressCalls = window.__progressCalls;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class ExplainAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_rail_shows_real_state_compactly(self):
        first = self.out["railFirst"]
        self.assertEqual(len(first), 1)
        self.assertEqual({k: first[0][k] for k in ("title", "pill", "pack")}, {"title": "Marketing", "pill": "New", "pack": "Summary · 18 cards · Quiz"})
        self.assertRegex(first[0]["due"], r"^Due \D*1\D")   # Oct 1 in the browser's locale
        self.assertEqual(self.out["railAfter"][1], {"title": "Statistics", "pill": "Needs review", "due": "Add deadline", "pack": "Summary · Quiz"})

    def test_add_materials_shows_learning_state_study_pack_and_quiz_honestly(self):
        sheet = {row["title"]: row for row in self.out["sheet"]}
        self.assertEqual(sheet["Statistics"], {"title": "Statistics", "pill": "Needs review", "quiz": "Latest quiz 42%",
                                               "pack": "Summary ready · No flashcards yet · Quiz ready", "deadline": "Add deadline"})
        self.assertEqual(sheet["PowerBI"], {"title": "PowerBI", "pill": "On track", "quiz": "Latest quiz 88%",
                                            "pack": "No summary yet · 12 flashcards · Quiz ready", "deadline": "Add deadline"})
        self.assertNotIn("Marketing", sheet)   # already in the plan

    def test_first_explanation_says_there_are_no_quiz_results(self):
        self.assertTrue(self.out["explainShown"])
        factors = self.out["firstFactors"]
        self.assertEqual(factors["Learning state"], "1 new")
        self.assertEqual(factors["Quiz performance"], "No quiz results yet — planning starts from new material.")
        self.assertRegex(factors["Deadlines"], r"^Marketing \D*1\D")   # Oct 1 in the browser's locale
        self.assertEqual(factors["Available time"], "Only the time you marked — 12h this week.")

    def test_explanation_uses_only_real_factors(self):
        factors = self.out["factors"]
        self.assertEqual(list(factors), ["Learning state", "Quiz performance", "Deadlines", "Available time", "In this plan"])
        self.assertEqual(factors["Learning state"], "1 new, 1 needs review")
        self.assertEqual(factors["Quiz performance"], "Statistics 42%")
        self.assertEqual(factors["In this plan"], "2 × deadline catch-up, 1 × new material")
        for text in (self.out["explainText"], self.out["firstExplainText"]):
            self.assertNotRegex(text, r"\d+\s*/\s*\d+|weight|priority|score|35|0\.\d|new_material|deadline_approaching")

    def test_suggested_session_explains_why(self):
        new = self.out["newGhost"]
        self.assertEqual((new["pill"], new["title"], new["activity"]), ("Suggested", "Statistics", "Summary"))
        self.assertRegex(new["when"], r"28\D.* · 18:55–19:40 · 45m$")
        self.assertEqual(new["reason"], "Why this session? Build understanding of new material.")
        self.assertEqual(new["notes"], [])              # no deadline set for Statistics
        self.assertEqual(new["hint"], "Suggested. Accept plan to save it.")
        deadline = self.out["deadlineGhost"]
        self.assertEqual(deadline["reason"], "Why this session? The deadline is coming up.")
        self.assertEqual(len(deadline["notes"]), 1)
        self.assertRegex(deadline["notes"][0], r"^Due \D*1\D")
        for popover in (new, deadline):
            self.assertNotRegex(popover["text"], r"new_material|deadline_approaching|priority")

    def test_flow_is_unchanged(self):
        self.assertGreaterEqual(self.out["previewCalls"], 2)   # suggestions appeared without a generate step
        self.assertEqual(self.out["accepted"]["confirms"], 1)
        self.assertTrue(all(kind == "confirmed" for kind in self.out["accepted"]["kinds"]))
        self.assertTrue(self.out["stillExplained"])
        self.assertGreaterEqual(self.out["progressCalls"], 3)

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ExplainDesktopTests(ExplainAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ExplainNarrowDesktopTests(ExplainAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
