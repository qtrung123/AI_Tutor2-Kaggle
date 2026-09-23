"""Real-browser tests for the document-centric Study Planner flow (headless Chrome, mocked API):
Materials -> Deadlines -> Availability -> Preview -> Adjust -> Preview -> Confirm -> saved plan.
Runs the same driver at desktop (1280px) and phone (390px). Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_sidebar_navigation_ui import MOCK as SIDEBAR_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

# An in-page model of the v2 planner API. Preview/confirm compute sessions from the saved
# availability so "adjust availability -> preview again" really changes the result.
MOCK = SIDEBAR_MOCK + r"""
window.__planner = {plans: [], materials: [], availability: [], sessions: [], calls: [], previewBodies: [], confirmBodies: []};
const LABELS = {new_material: "New material to learn", deadline_approaching: "Deadline is coming up"};
function plannerMinutes(hhmm) { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; }
function plannerSchedule() {
  const P = window.__planner;
  const available = P.availability.reduce((sum, slot) => sum + plannerMinutes(slot.end_at) - plannerMinutes(slot.start_at), 0);
  const titles = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"};
  const docs = P.materials.map((m) => m.document_id);
  const all = [
    [docs[0], "summary", "2026-09-28T18:00:00", "2026-09-28T18:45:00", 45, "deadline_approaching", "\"Marketing\" is due 2026-10-01: read the summary."],
    [docs[1], "summary", "2026-09-28T18:55:00", "2026-09-28T19:40:00", 45, "new_material", "Start \"Statistics\": read the summary."],
    [docs[0], "quiz", "2026-09-29T20:00:00", "2026-09-29T20:30:00", 30, "deadline_approaching", "\"Marketing\" is due 2026-10-01: take a quiz."],
  ].filter((row) => row[0]);
  const fits = available === 0 ? 0 : available < 120 ? 1 : all.length;
  const toSession = (row) => ({document_id: row[0], document_title: titles[row[0]], activity_type: row[1], scheduled_start: row[2],
    scheduled_end: row[3], duration_minutes: row[4], reason: {code: row[5], message: row[6]}, artifact_id: null});
  const sessions = all.slice(0, fits).map(toSession);
  const required = all.reduce((sum, row) => sum + row[4], 0);
  const scheduled = sessions.reduce((sum, s) => sum + s.duration_minutes, 0);
  return {sessions, capacity: {status: scheduled < required ? "at_risk" : "on_track", required_minutes: required,
    scheduled_minutes: scheduled, schedulable_minutes: Math.min(available, 90), available_minutes: available,
    shortfall_minutes: required - scheduled, unscheduled: all.slice(fits).map((row) => ({document_id: row[0], activity_type: row[1],
      estimated_minutes: row[4], deadline: null, reason: {code: row[5], message: row[6]}, artifact_id: null}))},
    warnings: available ? [] : [{code: "no_availability"}]};
}
const sidebarFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  const body = init.body ? JSON.parse(init.body) : null;
  if (p.startsWith("/api/planner")) P.calls.push(method + " " + p);
  if (p === "/api/documents") return json([
    {id: "mkt.pdf", title: "Marketing", chunks: 12, topics: [{topic_id: "t1", name: "Branding"}], topic_schema_version: 2},
    {id: "stats.pdf", title: "Statistics", chunks: 12, topics: [{topic_id: "t2", name: "Regression"}], topic_schema_version: 2},
    {id: "pbi.pdf", title: "PowerBI", chunks: 12, topics: [], topic_schema_version: 2}]);
  if (p === "/api/planner/availability" && method === "GET") return json(P.availability);
  if (p === "/api/planner/availability" && method === "POST") { P.availability.push({...body, availability_id: "a" + P.availability.length}); return json(P.availability); }
  if (p === "/api/planner/availability/remove") { P.availability = []; return json(P.availability); }
  if (p === "/api/planner/plans" && method === "GET") return json(P.plans);
  if (p === "/api/planner/plans" && method === "POST") { const plan = {plan_id: "plan-" + (P.plans.length + 1), title: body.title, status: "active"}; P.plans.push(plan); return json(plan, 201); }
  let m = p.match(/^\/api\/planner\/plans\/([^/]+)$/);
  if (m && method === "GET") return json({...P.plans.find((x) => x.plan_id === m[1]), materials: P.materials});
  if (m && method === "PATCH") { const plan = P.plans.find((x) => x.plan_id === m[1]); Object.assign(plan, body); return json(plan); }
  m = p.match(/^\/api\/planner\/plans\/([^/]+)\/materials$/);
  if (m && method === "POST") { const mat = {material_id: "m-" + body.document_id, plan_id: m[1], document_id: body.document_id, deadline: null, familiarity: null, learning_state: "new"}; P.materials.push(mat); return json(mat, 201); }
  m = p.match(/^\/api\/planner\/plans\/([^/]+)\/materials\/([^/]+)$/);
  if (m && method === "PATCH") { const mat = P.materials.find((x) => x.material_id === m[2]); Object.assign(mat, body); return json(mat); }
  if (m && method === "DELETE") { P.materials = P.materials.filter((x) => x.material_id !== m[2]); return json({deleted: m[2]}); }
  m = p.match(/^\/api\/planner\/plans\/([^/]+)\/(preview|confirm|sessions)$/);
  if (m && m[2] === "sessions") return json(P.sessions);
  if (m && (m[2] === "preview" || m[2] === "confirm")) {
    (m[2] === "preview" ? P.previewBodies : P.confirmBodies).push(body);
    const past = P.materials.filter((x) => x.deadline && x.deadline < "2026-09-28");
    if (past.length) return json({detail: {code: "deadline_passed", message: "Some deadlines have already passed. Update or clear them, then preview again.",
      local_date: "2026-09-28", documents: past.map((x) => ({material_id: x.material_id, document_id: x.document_id,
        document_title: {"stats.pdf": "Statistics", "mkt.pdf": "Marketing"}[x.document_id], deadline: x.deadline}))}}, 400);
    const result = plannerSchedule();
    if (m[2] === "preview") return json({plan_id: m[1], persisted: false, ...result});
    await new Promise((resolve) => setTimeout(resolve, 300));
    if (P.sessions.length) return json({detail: "This plan is already confirmed."}, 409);
    P.sessions = result.sessions.map((s, i) => ({...s, session_id: "s" + i, status: "scheduled",
      reason: {code: s.reason.code, message: LABELS[s.reason.code]}}));
    return json({plan_id: m[1], persisted: true, schedule_run_id: "run-1", sessions: P.sessions, capacity: result.capacity, warnings: []}, 201);
  }
  return sidebarFetch(input, init);
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
const view = () => document.getElementById("planner-view");
const q = (selector) => view().querySelector(selector);
const visibleStep = () => [...view().querySelectorAll("[data-planner-step]")].find((s) => !s.hidden)?.dataset.plannerStep;
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && q(".planner-shell").scrollWidth <= q(".planner-shell").clientWidth + 1;
const recordOverflow = (label) => { out.overflow[label] = noOverflow(); };
const materialRow = (title) => [...view().querySelectorAll(".planner-material-row")].find((row) => row.textContent.includes(title));
const setDeadline = async (title, value) => {
  const input = materialRow(title).querySelector('input[type="date"]');
  input.value = value; input.dispatchEvent(new Event("change", {bubbles: true})); await sleep(200);
};
const cell = (weekday, hhmm) => [...view().querySelectorAll(`.planner-cell[data-weekday="${weekday}"]`)]
  .find((c) => c.getAttribute("aria-label").endsWith(hhmm));
const paint = async (fromCell, toCell) => {
  fromCell.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true}));
  if (toCell) {
    toCell.scrollIntoView({block: "center", inline: "nearest"});
    const box = toCell.getBoundingClientRect();
    q("#planner-calendar").dispatchEvent(new PointerEvent("pointermove", {bubbles: true, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2}));
  }
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  await sleep(250);
};
const previewState = () => ({
  step: visibleStep(),
  capacity: q(".planner-capacity")?.className || null,
  capacityTitle: q(".planner-capacity strong")?.textContent || null,
  stats: Object.fromEntries([...view().querySelectorAll(".planner-capacity-stats div")].map((d) => [d.querySelector("dt").textContent, d.querySelector("dd").textContent])),
  days: [...q("#planner-preview-sessions").querySelectorAll(".planner-day h4")].map((h) => h.childNodes[0].textContent),
  sessions: [...q("#planner-preview-sessions").querySelectorAll(".planner-session")].map((li) => ({
    time: li.querySelector(".planner-session-time").textContent, title: li.querySelector("strong").textContent,
    activity: li.querySelector(".planner-activity-chip").textContent, reason: li.querySelector(".planner-session-reason").textContent,
    text: li.textContent})),
  confirmHidden: q("#planner-confirm-button").hidden,
  confirmText: q("#planner-confirm-button").textContent,
  errorHidden: q("#planner-preview-error").hidden,
  errorText: q("#planner-preview-error").textContent,
});
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);   // pin "today" before the mocked Sep 28/29 sessions
  setPage("planner"); await sleep(400);
  const P = window.__planner;
  out.start = {step: visibleStep(), rows: view().querySelectorAll(".planner-material-row").length,
    nextDisabled: q("#planner-to-availability").disabled, selects: view().querySelectorAll("select").length,
    mentionsTopic: /topic/i.test(view().innerHTML), stepLabels: [...view().querySelectorAll("[data-step-indicator]")].map((li) => li.textContent)};
  recordOverflow("materials");

  // Materials: two documents, one deadline each -- one of them already past.
  materialRow("Marketing").querySelector('input[type="checkbox"]').click(); await sleep(300);
  materialRow("Statistics").querySelector('input[type="checkbox"]').click(); await sleep(300);
  await setDeadline("Marketing", "2026-10-01");
  await setDeadline("Statistics", "2026-01-01");
  out.materials = {selected: view().querySelectorAll(".planner-material-row.selected").length, plans: P.plans.length,
    materials: P.materials.map((m) => [m.document_id, m.deadline]), deadlineInputs: view().querySelectorAll('.planner-material-row input[type="date"]').length,
    nextDisabled: q("#planner-to-availability").disabled};
  recordOverflow("materialsSelected");

  // Availability: one 30-minute cell (Monday 18:00).
  q("#planner-to-availability").click(); await sleep(300);
  out.availability = {step: visibleStep(), hint: q('[data-planner-step="availability"] .planner-hint').textContent,
    repeatChecked: q("#planner-repeat-weekly").checked, cells: view().querySelectorAll(".planner-cell").length};
  await paint(cell(0, "18:00"));
  out.availability.saved = P.availability.map((slot) => [slot.day_of_week, slot.start_at, slot.end_at, slot.is_recurring]);
  out.availability.paintedCells = view().querySelectorAll('.planner-cell.available[data-weekday="0"]').length;
  recordOverflow("availability");

  // Preview with a past deadline -> clear error, nothing scheduled.
  q("#planner-generate-preview").click(); await sleep(400);
  out.pastDeadline = previewState();
  q("#planner-preview-error button").click(); await sleep(200);
  out.afterFixLink = visibleStep();
  await setDeadline("Statistics", "");
  out.clearedDeadline = P.materials.find((m) => m.document_id === "stats.pdf").deadline;

  // Preview with too little availability -> at_risk, partial confirm offered.
  q("#planner-to-availability").click(); await sleep(200);
  q("#planner-generate-preview").click(); await sleep(400);
  out.atRisk = previewState();
  recordOverflow("previewAtRisk");

  // Adjust: add availability (drag Monday 18:30 -> 21:30), then preview again.
  [...view().querySelectorAll(".planner-capacity-actions button")].find((b) => b.textContent === "Add availability").click(); await sleep(200);
  out.adjustStep = visibleStep();
  await paint(cell(0, "18:30"), cell(0, "21:30"));
  out.afterAdjust = P.availability.map((slot) => [slot.day_of_week, slot.start_at, slot.end_at]);
  q("#planner-generate-preview").click(); await sleep(400);
  out.onTrack = previewState();
  recordOverflow("previewOnTrack");

  // Confirm (double-click must submit once), then the saved plan comes from the server.
  q("#planner-confirm-button").click(); q("#planner-confirm-button").click(); await sleep(100);
  out.confirming = {disabled: q("#planner-confirm-button").disabled, text: q("#planner-confirm-button").textContent};
  await sleep(800);
  out.confirmed = {step: visibleStep(), confirmCalls: P.confirmBodies.length,
    summary: q("#planner-plan-summary").textContent,
    sessions: [...q("#planner-plan-sessions").querySelectorAll(".planner-session")].map((li) => li.textContent),
    days: q("#planner-plan-sessions").querySelectorAll(".planner-day").length};
  recordOverflow("plan");

  // Reload: the saved plan is what the page shows.
  await loadPlannerData(); await sleep(300);
  out.reloaded = {step: visibleStep(), sessions: q("#planner-plan-sessions").querySelectorAll(".planner-session").length};
  q("#planner-new-plan").click(); await sleep(300);
  out.newPlan = {step: visibleStep(), archived: P.plans[0].status};

  out.previewBodies = P.previewBodies;
  out.confirmBodies = P.confirmBodies;
  out.expectedOffset = -new Date().getTimezoneOffset();
  out.calls = P.calls;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_at_width(width, height):
    original = (sidebar_harness.MOCK, sidebar_harness.DRIVER)
    sidebar_harness.MOCK, sidebar_harness.DRIVER = MOCK, DRIVER
    try:
        return sidebar_harness.run_at_width(width, height)
    finally:
        sidebar_harness.MOCK, sidebar_harness.DRIVER = original


class PlannerFlowAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_materials_step_is_document_only_without_topic_controls(self):
        start = self.out["start"]
        self.assertEqual((start["step"], start["rows"], start["nextDisabled"]), ("materials", 3, True))
        self.assertEqual(start["selects"], 0)
        self.assertFalse(start["mentionsTopic"])
        self.assertEqual(start["stepLabels"], ["1Materials", "2Availability", "3Preview", "4Plan"])
        materials = self.out["materials"]
        self.assertEqual((materials["selected"], materials["plans"], materials["deadlineInputs"]), (2, 1, 2))
        self.assertEqual(materials["materials"], [["mkt.pdf", "2026-10-01"], ["stats.pdf", "2026-01-01"]])
        self.assertFalse(materials["nextDisabled"])

    def test_availability_reuses_the_weekly_calendar(self):
        availability = self.out["availability"]
        self.assertEqual(availability["step"], "availability")
        self.assertEqual(availability["hint"], "Select when you could study. We won’t necessarily fill all of this time.")
        self.assertTrue(availability["repeatChecked"])
        self.assertEqual(availability["cells"], 34 * 7)
        self.assertEqual(availability["saved"], [[0, "18:00", "18:30", True]])

    def test_past_deadline_shows_a_clear_error_and_links_back(self):
        past = self.out["pastDeadline"]
        self.assertEqual(past["step"], "preview")
        self.assertFalse(past["errorHidden"])
        self.assertIn("Statistics: 2026-01-01", past["errorText"])
        self.assertEqual(past["sessions"], [])
        self.assertEqual(self.out["afterFixLink"], "materials")
        self.assertIsNone(self.out["clearedDeadline"])

    def test_at_risk_preview_shows_capacity_and_partial_confirm(self):
        at_risk = self.out["atRisk"]
        self.assertTrue(at_risk["errorHidden"])
        self.assertIn("at-risk", at_risk["capacity"])
        self.assertEqual(at_risk["capacityTitle"], "Not everything fits")
        self.assertEqual(at_risk["stats"], {"Required": "2h", "Schedulable": "30m", "Shortfall": "1h 15m"})
        self.assertEqual(len(at_risk["sessions"]), 1)
        self.assertEqual((at_risk["confirmHidden"], at_risk["confirmText"]), (False, "Confirm partial plan"))

    def test_preview_adjust_preview(self):
        self.assertEqual(self.out["adjustStep"], "availability")
        self.assertEqual(self.out["afterAdjust"], [[0, "18:00", "18:30"], [0, "18:30", "22:00"]])
        on_track = self.out["onTrack"]
        self.assertEqual(on_track["capacityTitle"], "Everything fits")
        self.assertEqual(on_track["stats"], {})
        self.assertEqual(len(on_track["days"]), 2)  # grouped by day
        first = on_track["sessions"][0]
        self.assertEqual((first["time"], first["title"], first["activity"]), ("18:00–18:45", "Marketing", "Summary"))
        self.assertEqual(first["reason"], '"Marketing" is due 2026-10-01: read the summary.')
        self.assertIn("45m", first["text"])
        self.assertEqual([s["activity"] for s in on_track["sessions"]], ["Summary", "Summary", "Quiz"])
        for session in on_track["sessions"]:
            self.assertNotRegex(session["text"].lower(), r"priority|score")
        self.assertEqual(on_track["confirmText"], "Looks good, confirm")

    def test_preview_sends_only_the_learner_utc_offset(self):
        self.assertEqual(len(self.out["previewBodies"]), 3)
        for body in self.out["previewBodies"] + self.out["confirmBodies"]:
            self.assertEqual(body, {"utc_offset_minutes": self.out["expectedOffset"]})

    def test_confirm_submits_once_and_shows_the_saved_plan(self):
        self.assertEqual(self.out["confirming"], {"disabled": True, "text": "Saving…"})
        confirmed = self.out["confirmed"]
        self.assertEqual((confirmed["step"], confirmed["confirmCalls"], confirmed["days"]), ("plan", 1, 2))
        self.assertEqual(confirmed["summary"], "3 sessions · 2h across 2 days")
        self.assertIn("New material to learn", confirmed["sessions"][1])   # server's saved reason label
        self.assertEqual(self.out["reloaded"], {"step": "plan", "sessions": 3})
        self.assertEqual(self.out["newPlan"], {"step": "materials", "archived": "archived"})

    def test_never_calls_legacy_task_or_topic_endpoints(self):
        for call in self.out["calls"]:
            self.assertNotRegex(call, r"/api/planner/(tasks|blocks|topic-progress)")

    def test_no_horizontal_overflow(self):
        self.assertTrue(self.out["overflow"])
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerFlowDesktopTests(PlannerFlowAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerFlowPhoneTests(PlannerFlowAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_runs_at_phone_width(self):
        self.assertEqual(self.out["width"], 390)


if __name__ == "__main__":
    unittest.main()
