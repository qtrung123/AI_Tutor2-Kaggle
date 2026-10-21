"""Real-browser tests (headless Chrome, mocked v2 API) for the desktop Study Planner:

- first open: the current calendar workspace renders immediately -- with a loading notice while the
  plan is still loading -- and the legacy step-by-step wizard never shows, also when loading fails
  (a retry is offered);
- a combined session (Summary + Flashcards) on the calendar: its label, its two steps with minutes
  and progress, the step actions ("Open Summary", "Summary done") and completing the first step.

Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width
from tests.test_study_planner_v2_ui import MOCK as PLANNER_MOCK

STEPS = ('[{activity_type: "summary", duration_minutes: 20, artifact_id: null, status: "pending", completed_at: null},'
         ' {activity_type: "flashcards", duration_minutes: 15, artifact_id: null, status: "pending", completed_at: null}]')

MOCK = PLANNER_MOCK + r"""
{
  const P = window.__planner;
  P.plans.push({plan_id: "plan-1", title: "Exams", status: "active"});
  P.materials.push({material_id: "m-mkt.pdf", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null, goal_type: "flexible",
    target_date: null, familiarity: null, learning_state: "new"});
  P.availability.push({availability_id: "a0", is_recurring: true, day_of_week: 0, start_at: "18:00", end_at: "21:00", date: null});
  P.sessions.push({session_id: "s0", plan_id: "plan-1", document_id: "mkt.pdf", document_title: "Marketing", activity_type: "summary",
    scheduled_start: "2026-09-28T18:00:00", scheduled_end: "2026-09-28T18:35:00", duration_minutes: 35, status: "in_progress",
    reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, started_at: "2026-09-28T18:01:00Z",
    completed_at: null, rescheduled_from: null, manual_edit: false, steps: STEPS_JSON, current_step: 0});
  P.stepCalls = [];
}
const firstOpenFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/planner/plans" && method === "GET") {
    await new Promise((resolve) => setTimeout(resolve, window.__planDelay || 0));
    if (window.__planFail) return json({detail: "Planner is unavailable"}, 500);
  }
  const step = p.match(/^\/api\/planner\/sessions\/([^/]+)\/steps\/(\d+)\/complete$/);
  if (step && method === "POST") {
    P.stepCalls.push(p);
    const session = P.sessions.find((s) => s.session_id === step[1]);
    session.steps[Number(step[2])].status = "completed";
    const next = session.steps.findIndex((s) => s.status !== "completed");
    session.current_step = next < 0 ? null : next;
    if (next < 0) session.status = "completed";
    return json({session: {...session}, changed: true, session_completed: next < 0});
  }
  return firstOpenFetch(input, init);
};
""".replace("STEPS_JSON", STEPS)

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: [], width: window.innerWidth};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const layout = () => ({wizard: visible(document.querySelector(".planner-shell")), wizardHidden: document.querySelector(".planner-shell").hidden,
  workspace: visible($("planner-workspace")), steps: [...document.querySelectorAll("[data-step-indicator]")].some(visible),
  notice: $("pcal-notice").hidden ? null : $("pcal-notice").textContent});
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 28, 18, 10, 0);   // Mon 28 Sep 2026, 18:10: the combined session is under way
  out.beforeOpen = {wizardHidden: document.querySelector(".planner-shell").hidden, workspaceHidden: $("planner-workspace").hidden};

  // 1. First open, failing load: the calendar workspace, never the wizard; a retry is offered.
  window.__planFail = true;
  setPage("planner"); await sleep(20);
  out.immediately = layout();
  await sleep(400);
  out.failed = {...layout(), retry: !!$("pcal-load-retry")};
  setPage("overview"); await sleep(50);

  // 2. Open again with a slow load: the workspace shows a loading notice at once, then the plan.
  window.__planFail = false;
  window.__planDelay = 600;
  setPage("planner"); await sleep(30);
  out.loading = layout();
  await sleep(1500);
  out.loaded = layout();

  // 3. The combined session on the calendar and in its popover.
  const block = document.querySelector('#pcal-body .pcal-event[data-session-id="s0"]');
  out.block = block ? {combined: block.classList.contains("pcal-event--combined"), meta: block.querySelector(".pcal-event-meta").textContent,
    kind: block.dataset.kind} : null;
  block.click(); await sleep(80);
  const popover = $("pcal-popover");
  const steps = () => [...popover.querySelectorAll(".pcal-step")].map((li) => li.textContent);
  out.popover = {open: visible(popover), activity: popover.querySelector(".pcal-popover-activity")?.textContent, steps: steps(),
    buttons: [...popover.querySelectorAll(".pcal-session-actions button")].map((b) => b.textContent)};
  // 4. Mark the first step done: one request; the session stays in progress with Flashcards next.
  [...popover.querySelectorAll(".pcal-session-actions button")].find((b) => b.textContent === "Summary done").click();
  await sleep(900);
  out.afterStep = {calls: window.__planner.stepCalls, toast: $("toast").textContent};
  const again = document.querySelector('#pcal-body .pcal-event[data-session-id="s0"]');
  again.click(); await sleep(80);
  out.afterStep.steps = steps();
  out.afterStep.buttons = [...$("pcal-popover").querySelectorAll(".pcal-session-actions button")].map((b) => b.textContent);
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerFirstOpenAndCombinedSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=MOCK, driver=DRIVER)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out, self.out.get("fatal"))
        self.assertEqual(self.out["errors"], [])

    def test_the_wizard_markup_starts_hidden(self):
        self.assertEqual(self.out["beforeOpen"], {"wizardHidden": True, "workspaceHidden": True})

    def test_first_open_renders_the_calendar_immediately_with_a_loading_state(self):
        for state in ("immediately", "loading"):
            self.assertEqual({key: self.out[state][key] for key in ("wizard", "wizardHidden", "workspace", "steps")},
                             {"wizard": False, "wizardHidden": True, "workspace": True, "steps": False}, state)
        self.assertEqual(self.out["loading"]["notice"], "Loading your study plan…")
        self.assertEqual((self.out["loaded"]["wizard"], self.out["loaded"]["workspace"], self.out["loaded"]["notice"]),
                         (False, True, None))

    def test_a_failed_load_keeps_the_current_interface_and_offers_a_retry(self):
        failed = self.out["failed"]
        self.assertEqual((failed["wizard"], failed["workspace"], failed["retry"]), (False, True, True))
        self.assertIn("Could not load your study plan.", failed["notice"])

    def test_combined_session_shows_both_steps_and_step_actions(self):
        self.assertEqual(self.out["block"], {"combined": True, "meta": "Summary + Flashcards · 18:00–18:35", "kind": "active"})
        popover = self.out["popover"]
        self.assertTrue(popover["open"])
        self.assertEqual(popover["activity"], "Summary + Flashcards")
        self.assertEqual(popover["steps"], ["1. Summary20mNow", "2. Flashcards15mNext"])
        self.assertEqual(popover["buttons"], ["Open Summary", "Summary done", "Skip"])   # no early Complete

    def test_completing_the_first_step_moves_on_to_the_second(self):
        after = self.out["afterStep"]
        self.assertEqual(after["calls"], ["/api/planner/sessions/s0/steps/0/complete"])
        self.assertEqual(after["toast"], "Summary done. Next: Flashcards")
        self.assertEqual(after["steps"], ["1. Summary20mDone", "2. Flashcards15mNow"])
        self.assertEqual(after["buttons"], ["Continue to Flashcards", "Flashcards done", "Skip"])


if __name__ == "__main__":
    unittest.main()
