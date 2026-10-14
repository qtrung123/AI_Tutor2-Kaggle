"""Real-browser tests: study goals in the desktop Study Planner (headless Chrome, mocked API).

- Materials saved before goals existed show as they are ("Due Oct 1" / "No goal set"); opening the
  planner writes nothing for them;
- Add materials asks for the goal: "Has deadline" needs a date, the optional target completion
  date can't be after the deadline, "Flexible learning" needs nothing;
- the Materials panel edits, switches and removes a goal inline; a draft survives a re-render;
- after a reload the saved goals come back;
- on an accepted plan a deadline change only brings a proposal to review: nothing is applied and
  the accepted sessions stay; a target-only change asks for nothing at all.
"Now" is pinned to Thu 2026-09-24 12:00. Runs at 1280px and 1100px. Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_drag_ui import MOCK as DRAG_MOCK
from tests.test_study_planner_calendar_ui import run_at_width

# Records every material write and answers the adaptation endpoints: preview proposes moving the
# first scheduled session (one change); apply must never be called by a goal edit.
MOCK = DRAG_MOCK + r"""
window.__goalWrites = [];
window.__adaptCalls = [];
const goalBaseFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  if (/\/materials(\/[^/]+)?$/.test(p) && method !== "GET") window.__goalWrites.push({method, body});
  const m = p.match(/^\/api\/planner\/plans\/([^/]+)\/adaptation\/(preview|apply)$/);
  if (m) {
    window.__adaptCalls.push({kind: m[2], trigger: body.trigger, confirm: Boolean(body.confirm)});
    if (m[2] === "apply") return json({plan_id: m[1], applied: false, requires_confirmation: false, change_count: 0,
      added: [], moved: [], cancelled: [], warnings: [], reasons: [], sessions: []});
    const s = P.sessions.find((x) => x.status === "scheduled");
    const to = s.scheduled_start.slice(0, 11) + "20:00:00";
    return json({plan_id: m[1], persisted: false, trigger: body.trigger, added: [], cancelled: [],
      moved: [{session_id: s.session_id, document_id: s.document_id, activity_type: s.activity_type, from_start: s.scheduled_start,
               from_end: s.scheduled_end, to_start: to, to_end: s.scheduled_start.slice(0, 11) + "20:30:00", message: "Fits the new deadline."}],
      unchanged_count: P.sessions.length - 1, change_count: 1, warnings: [], significance: "small", reasons: ["x"]});
  }
  return goalBaseFetch(input, init);
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
  && [...document.querySelectorAll(".pcal-material, .pcal-sheet-row")].every((el) => el.scrollWidth <= el.clientWidth + 1);
const material = (id) => document.querySelector(`.pcal-material[data-document-id="${id}"]`);
const sheetRow = (id) => document.querySelector(`.pcal-sheet-row[data-document-id="${id}"]`);
const rail = () => [...document.querySelectorAll(".pcal-material")].map((m) => ({title: m.querySelector(".pcal-material-title").textContent,
  goal: [...m.querySelector(".pcal-goal").classList].find((c) => c.startsWith("pcal-goal--")),
  text: m.querySelector(".pcal-goal").textContent, edit: m.querySelector(".pcal-goal-edit").textContent}));
const form = (root) => ({checked: root.querySelector(".planner-goal-choice input:checked")?.value || null,
  deadlineShown: !root.querySelector(".planner-goal-deadline").hidden, deadline: root.querySelector(".planner-goal-deadline input").value,
  target: root.querySelector(".planner-goal-target input").value,
  error: root.querySelector(".planner-goal-error").hidden ? null : root.querySelector(".planner-goal-error").textContent});
const choose = (root, goal) => { const r = root.querySelector(`.planner-goal-choice input[value="${goal}"]`); r.checked = true; r.dispatchEvent(new Event("change", {bubbles: true})); };
const setDate = (root, which, value) => { const i = root.querySelector(`.planner-goal-${which} input`); i.value = value; i.dispatchEvent(new Event("change", {bubbles: true})); };
const writes = () => window.__goalWrites.length;
const sessionsSnapshot = () => JSON.stringify(P.sessions.map((s) => [s.session_id, s.status, s.scheduled_start, s.scheduled_end]));
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  // Saved before goals existed: no goal_type / target_date at all.
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: "2026-10-01", learning_state: "new"},
                 {material_id: "m-stats", plan_id: "plan-1", document_id: "stats.pdf", deadline: null, learning_state: "new"}];
  P.availability = [0, 1, 2, 3, 4].map((day) => ({availability_id: "a" + day, start_at: "18:00", end_at: "22:00", is_recurring: true, day_of_week: day, date: null}));
  setPage("planner"); await sleep(900);
  $("pcal-materials-toggle").click(); await sleep(100);
  out.dates = Object.fromEntries(["2026-10-01", "2026-10-06", "2026-10-08", "2026-10-20"].map((key) => [key, plannerShortDate(key)]));

  // 1. Existing materials, untouched.
  out.legacy = {rail: rail(), writes: writes()};
  out.overflow.legacy = noOverflow();

  // 2. Add materials: the goal is chosen while adding.
  $("pcal-add-materials").click(); await sleep(150);
  const pbi = () => sheetRow("pbi.pdf");
  out.sheetDefault = form(pbi());
  out.overflow.sheet = noOverflow();
  choose(pbi(), "deadline");
  out.sheetDeadlineShown = form(pbi()).deadlineShown;
  pbi().querySelector(".pcal-sheet-add").click(); await sleep(100);
  out.sheetMissingDeadline = {error: form(pbi()).error, writes: writes()};
  setDate(pbi(), "deadline", "2026-10-08");
  setDate(pbi(), "target", "2026-10-10");
  out.sheetTargetMax = pbi().querySelector(".planner-goal-target input").max;
  pbi().querySelector(".pcal-sheet-add").click(); await sleep(100);
  out.sheetLateTarget = {error: form(pbi()).error, writes: writes()};
  setDate(pbi(), "target", "2026-10-06");
  pcalRenderSheet();   // a re-render keeps the draft
  out.sheetDraftKept = form(pbi());
  pbi().querySelector(".pcal-sheet-add").click(); await sleep(400);
  out.added = window.__goalWrites.at(-1);
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(600);
  $("pcal-materials-toggle").click(); await sleep(100);
  out.afterAdd = rail();

  // 3. An unconfigured material: Set goal -> must choose -> Flexible learning with a target date.
  material("stats.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  const statsEditor = () => material("stats.pdf").querySelector(".pcal-goal-editor");
  out.statsEditorOpen = form(statsEditor());
  statsEditor().querySelector(".pcal-goal-save").click(); await sleep(50);
  out.statsNoChoice = {error: form(statsEditor()).error, writes: writes(), removeShown: Boolean(statsEditor().querySelector(".pcal-goal-remove"))};
  choose(statsEditor(), "flexible");
  setDate(statsEditor(), "target", "2026-10-20");
  out.overflow.editor = noOverflow();
  statsEditor().querySelector(".pcal-goal-save").click(); await sleep(400);
  out.statsSaved = {write: window.__goalWrites.at(-1), editorClosed: !material("stats.pdf").querySelector(".pcal-goal-editor")};

  // 4. Change a legacy deadline: the editor starts from the saved state; a draft survives a re-render.
  material("mkt.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  const mktEditor = () => material("mkt.pdf").querySelector(".pcal-goal-editor");
  out.mktEditorOpen = form(mktEditor());
  setDate(mktEditor(), "deadline", "2026-10-03");
  renderPlannerWorkspace(); await sleep(50);
  out.mktDraftKept = form(mktEditor());
  mktEditor().querySelector(".pcal-goal-save").click(); await sleep(400);
  out.mktSaved = window.__goalWrites.at(-1);

  // 5. Cancel discards a draft; Remove goal clears the goal and both dates.
  material("pbi.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  const pbiEditor = () => material("pbi.pdf").querySelector(".pcal-goal-editor");
  choose(pbiEditor(), "flexible");
  pbiEditor().querySelector(".pcal-goal-cancel").click(); await sleep(50);
  out.cancelled = {writes: writes(), rail: rail().find((r) => r.title === "PowerBI")};
  material("pbi.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  pbiEditor().querySelector(".pcal-goal-remove").click(); await sleep(400);
  out.removed = window.__goalWrites.at(-1);
  out.beforeReload = rail();
  out.writesBeforeReload = writes();

  // 6. Reload: the saved goals come back from the server.
  await loadPlannerData(); await sleep(600);
  $("pcal-materials-toggle").click(); await sleep(100);
  if (!document.querySelector(".pcal-material")) { $("pcal-materials-toggle").click(); await sleep(100); }
  out.afterReload = rail();
  out.savedMaterials = P.materials.map((m) => [m.document_id, m.goal_type ?? null, m.deadline ?? null, m.target_date ?? null]);

  // 7. Accept the plan, then change goals: nothing accepted moves without the learner's say.
  await sleep(900);
  $("pcal-accept").click(); await sleep(800);
  out.confirmed = P.sessions.filter((s) => s.status === "scheduled").length;
  const accepted = sessionsSnapshot();
  material("mkt.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  setDate(mktEditor(), "target", "2026-10-02");
  mktEditor().querySelector(".pcal-goal-save").click(); await sleep(500);
  out.targetOnly = {write: window.__goalWrites.at(-1), adapt: window.__adaptCalls.length, unchanged: sessionsSnapshot() === accepted};
  material("stats.pdf").querySelector(".pcal-goal-edit").click(); await sleep(50);
  choose(statsEditor(), "deadline");
  setDate(statsEditor(), "deadline", "2026-10-02");   // before its target date (Oct 20): refused inline
  statsEditor().querySelector(".pcal-goal-save").click(); await sleep(100);
  out.liveLateTarget = {error: form(statsEditor()).error, writes: writes(), adapt: window.__adaptCalls.length};
  setDate(statsEditor(), "deadline", "2026-10-25");
  statsEditor().querySelector(".pcal-goal-save").click(); await sleep(600);
  out.deadlineOnLivePlan = {write: window.__goalWrites.at(-1), adapt: window.__adaptCalls.map((c) => [c.kind, c.trigger.kind, c.trigger.document_id || null]),
    review: document.querySelector(".pcal-review-count")?.textContent || null, unchanged: sessionsSnapshot() === accepted};
  $("pcal-review-keep")?.click(); await sleep(300);
  out.kept = {adapt: window.__adaptCalls.map((c) => c.kind), unchanged: sessionsSnapshot() === accepted,
    reviewGone: !document.querySelector(".pcal-review-count")};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class GoalAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_existing_materials_without_goals_stay_valid(self):
        legacy = self.out["legacy"]
        self.assertEqual(legacy["writes"], 0)   # nothing is invented or saved just by opening the planner
        mkt, stats = legacy["rail"]
        self.assertEqual((mkt["goal"], mkt["edit"]), ("pcal-goal--deadline", "Edit goal"))
        self.assertEqual(mkt["text"], f"Due {self.out['dates']['2026-10-01']}")
        self.assertEqual(stats, {"title": "Statistics", "goal": "pcal-goal--none", "text": "No goal set", "edit": "Set goal"})

    def test_add_material_with_a_deadline_goal(self):
        self.assertEqual(self.out["sheetDefault"], {"checked": "flexible", "deadlineShown": False, "deadline": "", "target": "", "error": None})
        self.assertTrue(self.out["sheetDeadlineShown"])
        self.assertEqual(self.out["sheetMissingDeadline"], {"error": "Pick the deadline date.", "writes": 0})
        self.assertEqual(self.out["sheetTargetMax"], "2026-10-08")
        self.assertEqual(self.out["sheetLateTarget"], {"error": "The target completion date can’t be after the deadline.", "writes": 0})
        self.assertEqual(self.out["sheetDraftKept"], {"checked": "deadline", "deadlineShown": True, "deadline": "2026-10-08",
                                                      "target": "2026-10-06", "error": None})
        self.assertEqual(self.out["added"], {"method": "POST", "body": {"document_id": "pbi.pdf", "goal_type": "deadline",
                                                                        "deadline": "2026-10-08", "target_date": "2026-10-06"}})
        added = self.out["afterAdd"][2]
        self.assertEqual((added["title"], added["goal"]), ("PowerBI", "pcal-goal--deadline"))
        dates = self.out["dates"]
        self.assertEqual(added["text"], f"Due {dates['2026-10-08']} · Target {dates['2026-10-06']}")

    def test_choose_flexible_learning_for_an_unconfigured_material(self):
        self.assertEqual(self.out["statsEditorOpen"], {"checked": None, "deadlineShown": False, "deadline": "", "target": "", "error": None})
        self.assertEqual(self.out["statsNoChoice"], {"error": "Choose a learning goal.", "writes": 1, "removeShown": False})
        self.assertEqual(self.out["statsSaved"], {"write": {"method": "PATCH", "body": {"goal_type": "flexible", "deadline": None,
                                                                                         "target_date": "2026-10-20"}}, "editorClosed": True})

    def test_change_deadline_with_a_draft_that_survives_rerender(self):
        self.assertEqual(self.out["mktEditorOpen"], {"checked": "deadline", "deadlineShown": True, "deadline": "2026-10-01",
                                                     "target": "", "error": None})
        self.assertEqual(self.out["mktDraftKept"]["deadline"], "2026-10-03")
        self.assertEqual(self.out["mktSaved"], {"method": "PATCH", "body": {"goal_type": "deadline", "deadline": "2026-10-03", "target_date": None}})

    def test_cancel_and_remove_goal(self):
        self.assertEqual(self.out["cancelled"]["writes"], 3)
        self.assertEqual(self.out["cancelled"]["rail"]["goal"], "pcal-goal--deadline")
        self.assertEqual(self.out["removed"], {"method": "PATCH", "body": {"goal_type": None, "deadline": None, "target_date": None}})

    def test_goals_are_restored_after_reload(self):
        self.assertEqual(self.out["afterReload"], self.out["beforeReload"])
        by_title = {row["title"]: row for row in self.out["afterReload"]}
        self.assertEqual(by_title["Statistics"]["goal"], "pcal-goal--flexible")
        self.assertEqual(by_title["Statistics"]["text"], f"Flexible · Target {self.out['dates']['2026-10-20']}")
        self.assertEqual((by_title["PowerBI"]["goal"], by_title["PowerBI"]["text"]), ("pcal-goal--none", "No goal set"))
        self.assertEqual(self.out["savedMaterials"], [["mkt.pdf", "deadline", "2026-10-03", None],
                                                      ["stats.pdf", "flexible", None, "2026-10-20"],
                                                      ["pbi.pdf", None, None, None]])

    def test_accepted_sessions_are_never_changed_by_a_goal_edit(self):
        self.assertGreater(self.out["confirmed"], 0)
        target_only = self.out["targetOnly"]
        self.assertEqual(target_only["write"]["body"], {"goal_type": "deadline", "deadline": "2026-10-03", "target_date": "2026-10-02"})
        self.assertEqual(target_only["adapt"], 0)   # no deadline change: nothing to re-check
        self.assertTrue(target_only["unchanged"])
        self.assertEqual(self.out["liveLateTarget"], {"error": "The target completion date can’t be after the deadline.",
                                                      "writes": 5, "adapt": 0})
        live = self.out["deadlineOnLivePlan"]
        self.assertEqual(live["write"]["body"], {"goal_type": "deadline", "deadline": "2026-10-25", "target_date": "2026-10-20"})
        self.assertEqual(live["adapt"], [["preview", "deadline_changed", "stats.pdf"]])   # a proposal, never an apply
        self.assertEqual(live["review"], "1 schedule change suggested")
        self.assertTrue(live["unchanged"])
        self.assertEqual(self.out["kept"], {"adapt": ["preview"], "unchanged": True, "reviewGone": True})

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoalsDesktopTests(GoalAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoalsNarrowDesktopTests(GoalAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
