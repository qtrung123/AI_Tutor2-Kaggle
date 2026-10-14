"""Real-browser tests: Study Preferences panel and manual session editing (desktop, mocked API).

- the Preferences panel shows the saved values (defaults first), validates lengths before sending,
  saves with PUT, re-runs the suggested plan, survives a reload, shows the server's refusal next to
  the field, and resets to the defaults;
- an accepted session's Edit form changes date, start time and length; a refused change shows the
  server's reason in the form and saves nothing; a saved one is marked "Set by you".
"Now" is pinned to Thu 2026-09-24 12:00. Runs at 1280px and 1100px. Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_drag_ui import MOCK as DRAG_MOCK
from tests.test_study_planner_calendar_ui import run_at_width

MOCK = DRAG_MOCK + r"""
const PREF_DEFAULTS = {daily_target_minutes: 90, break_minutes: 20, strategy: "balanced",
  durations: {summary: 45, flashcards: 20, quiz: 35, quiz_retry: 35, review: 30, written_quiz: 20, final_review: 45, light_review: 20}};
const PREF_OPTIONS = {daily_target_minutes: [60, 90, 120, 180], break_minutes: [5, 10, 15, 20, 30],
  strategy: ["balanced", "finish_early", "deadline_priority"], durations: Object.keys(PREF_DEFAULTS.durations),
  min_session_minutes: 20, max_session_minutes: 180, duration_step_minutes: 5};
Object.assign(window.__planner, {prefs: null, prefCalls: [], prefsReject: null, editCalls: []});
const prefsBaseFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  const current = () => ({...(P.prefs || PREF_DEFAULTS), durations: {...(P.prefs || PREF_DEFAULTS).durations},
    saved: Boolean(P.prefs), options: PREF_OPTIONS});
  if (p === "/api/planner/preferences") {
    P.prefCalls.push({method, body});
    if (method === "PUT") {
      if (P.prefsReject) return json({detail: P.prefsReject}, 400);
      P.prefs = {...(P.prefs || PREF_DEFAULTS), ...body, durations: {...(P.prefs || PREF_DEFAULTS).durations, ...(body.durations || {})}};
    }
    if (method === "DELETE") P.prefs = null;
    return json(current());
  }
  const m = p.match(/^\/api\/planner\/sessions\/([^/]+)\/reschedule$/);
  if (m && body && body.duration_minutes !== undefined) {
    // The Edit form with a new length: same rules as a drag, with the new length.
    P.editCalls.push(body);
    const session = P.sessions.find((s) => s.session_id === m[1]);
    if (body.duration_minutes < 20) return json({detail: {code: "session_too_short", message: "A study session must be at least 20 minutes long.", status: "scheduled"}}, 409);
    const problem = slotProblem(body.target_start, body.duration_minutes,
      P.sessions.filter((s) => s !== session && ["scheduled", "in_progress", "completed"].includes(s.status)));
    if (problem) return json({detail: {code: problem, message: MESSAGES[problem], status: session.status}}, 409);
    session.status = "rescheduled";
    const moved = {...session, session_id: session.session_id + "-e", status: "scheduled", rescheduled_from: session.session_id,
      scheduled_start: body.target_start, duration_minutes: body.duration_minutes, manual_edit: true,
      scheduled_end: isoAt(body.target_start.slice(0, 10), minutesOf(body.target_start) + body.duration_minutes)};
    P.sessions.push(moved);
    return json({session: moved, previous: {...session}, after_deadline: false});
  }
  if (m && body) P.editCalls.push(body);
  return prefsBaseFetch(input, init);
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
  && (!$("pcal-prefs-rail") || $("pcal-prefs-rail").getBoundingClientRect().right <= window.innerWidth + 1)
  && (!$("pcal-popover").hidden ? $("pcal-popover").getBoundingClientRect().right <= window.innerWidth + 1 : true);
const form = () => $("pcal-prefs-form");
const panel = () => ({open: getComputedStyle($("pcal-prefs-rail")).display !== "none",
  daily: form().querySelector("#pcal-pref-daily")?.value, pause: form().querySelector("#pcal-pref-break")?.value,
  strategy: form().querySelector('input[name="pcal-pref-strategy"]:checked')?.value,
  summary: form().querySelector('input[data-activity="summary"]')?.value,
  error: form().querySelector(".pcal-prefs-error")?.hidden === false ? form().querySelector(".pcal-prefs-error").textContent : null,
  status: form().querySelector(".pcal-prefs-status")?.textContent || "",
  invalid: [...form().querySelectorAll('[aria-invalid="true"]')].map((i) => i.dataset.activity)});
const setValue = (el, value) => { el.value = value; el.dispatchEvent(new Event("change", {bubbles: true})); el.dispatchEvent(new Event("input", {bubbles: true})); };
const editForm = () => $("pcal-popover").querySelector(".pcal-edit-form");
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: "2026-10-01", learning_state: "new"}];
  P.availability = [0, 1, 2, 3, 4].map((day) => ({availability_id: "a" + day, start_at: "18:00", end_at: "22:00", is_recurring: true, day_of_week: day, date: null}));
  setPage("planner"); await sleep(900);

  // 1. Defaults, shown from the server.
  $("pcal-prefs-toggle").click(); await sleep(150);
  out.defaults = panel();
  out.toggleExpanded = $("pcal-prefs-toggle").getAttribute("aria-expanded");
  out.overflow.panel = noOverflow();

  // 2. A too-short length is caught before anything is sent.
  setValue(form().querySelector("#pcal-pref-daily"), "120");
  setValue(form().querySelector("#pcal-pref-break"), "10");
  form().querySelector('input[name="pcal-pref-strategy"][value="finish_early"]').click();
  setValue(form().querySelector('input[data-activity="summary"]'), "15");
  form().querySelector(".pcal-prefs-save").click(); await sleep(150);
  out.tooShort = {...panel(), puts: P.prefCalls.filter((c) => c.method === "PUT").length};

  // 3. Save: one PUT with every choice; the suggested plan is recomputed.
  setValue(form().querySelector('input[data-activity="summary"]'), "60");
  const previewsBefore = P.previewBodies.length;
  form().querySelector(".pcal-prefs-save").click(); await sleep(900);
  out.saved = {...panel(), put: P.prefCalls.filter((c) => c.method === "PUT").at(-1)?.body,
    previewsAfterSave: P.previewBodies.length - previewsBefore};

  // 4. Reload: the saved values come back.
  $("pcal-prefs-toggle").click(); await sleep(100);
  await loadPlannerData(); await sleep(600);
  $("pcal-prefs-toggle").click(); await sleep(200);
  out.reloaded = panel();

  // 5. The server refuses: its reason is shown next to the field; nothing changes.
  P.prefsReject = {code: "invalid_preference", field: "durations.quiz", message: "60 minutes is longer than your daily study target (60 minutes)."};
  form().querySelector(".pcal-prefs-save").click(); await sleep(300);
  out.refused = panel();
  P.prefsReject = null;

  // 6. Reset to the defaults.
  form().querySelector(".pcal-prefs-reset").click(); await sleep(400);
  out.reset = {...panel(), deletes: P.prefCalls.filter((c) => c.method === "DELETE").length};
  $("pcal-prefs-toggle").click(); await sleep(100);

  // 7. Accept the plan, then edit an accepted session.
  await sleep(600);
  $("pcal-accept").click(); await sleep(800);
  const first = P.sessions.filter((s) => s.status === "scheduled").sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start))[0];
  const other = P.sessions.filter((s) => s.status === "scheduled" && s !== first)[0];
  out.first = {start: first.scheduled_start, minutes: first.duration_minutes};
  const block = () => document.querySelector(`.pcal-event[data-session-id="${first.session_id}"]`);
  if (!block()) { $("pcal-next").click(); await sleep(150); }
  block().click(); await sleep(150);
  out.popoverButtons = [...$("pcal-popover").querySelectorAll(".pcal-session-actions button")].map((b) => b.textContent);
  $("pcal-popover").querySelector(".planner-session-edit").click(); await sleep(150);
  out.editOpen = {date: editForm().querySelector("#pcal-edit-date").value, time: editForm().querySelector("#pcal-edit-time").value,
    minutes: editForm().querySelector("#pcal-edit-duration").value};
  out.overflow.edit = noOverflow();
  // a too-short length: refused in the form, nothing sent
  setValue(editForm().querySelector("#pcal-edit-duration"), "10");
  editForm().querySelector(".pcal-edit-save").click(); await sleep(200);
  out.editShort = {error: editForm().querySelector(".pcal-edit-error").textContent, sent: P.editCalls.length};
  // onto another session: the server's reason is shown, the form stays
  setValue(editForm().querySelector("#pcal-edit-date"), other.scheduled_start.slice(0, 10));
  setValue(editForm().querySelector("#pcal-edit-time"), other.scheduled_start.slice(11, 16));
  setValue(editForm().querySelector("#pcal-edit-duration"), "40");
  editForm().querySelector(".pcal-edit-save").click(); await sleep(400);
  out.editConflict = {error: editForm()?.querySelector(".pcal-edit-error").textContent || null, stillOpen: Boolean(editForm()),
    firstStatus: first.status};
  // outside the available hours
  setValue(editForm().querySelector("#pcal-edit-date"), "2026-09-30");
  setValue(editForm().querySelector("#pcal-edit-time"), "10:00");
  editForm().querySelector(".pcal-edit-save").click(); await sleep(400);
  out.editOutside = editForm()?.querySelector(".pcal-edit-error").textContent || null;
  // a valid change: new date, time and length
  setValue(editForm().querySelector("#pcal-edit-time"), "20:15");
  setValue(editForm().querySelector("#pcal-edit-duration"), "50");
  editForm().querySelector(".pcal-edit-save").click(); await sleep(600);
  const edited = P.sessions.find((s) => s.rescheduled_from === first.session_id && s.status === "scheduled");
  out.editSaved = {closed: $("pcal-popover").hidden, body: P.editCalls.at(-1),
    edited: edited ? {start: edited.scheduled_start, end: edited.scheduled_end, minutes: edited.duration_minutes} : null};
  const editedBlock = document.querySelector(`.pcal-event[data-session-id="${edited.session_id}"]`);
  editedBlock.click(); await sleep(150);
  out.manualNote = $("pcal-popover").querySelector(".pcal-popover-manual")?.textContent || null;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class PreferencesAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_defaults_are_shown(self):
        self.assertEqual(self.out["defaults"], {"open": True, "daily": "90", "pause": "20", "strategy": "balanced",
                                                "summary": "45", "error": None, "status": "Using the defaults.", "invalid": []})
        self.assertEqual(self.out["toggleExpanded"], "true")

    def test_too_short_lengths_are_caught_before_sending(self):
        short = self.out["tooShort"]
        self.assertEqual(short["puts"], 0)
        self.assertIn("at least 20 minutes", short["error"])
        self.assertEqual(short["invalid"], ["summary"])

    def test_save_sends_every_choice_and_refreshes_the_suggestions(self):
        saved = self.out["saved"]
        self.assertEqual({k: saved["put"][k] for k in ("daily_target_minutes", "break_minutes", "strategy")},
                         {"daily_target_minutes": 120, "break_minutes": 10, "strategy": "finish_early"})
        self.assertEqual(saved["put"]["durations"]["summary"], 60)
        self.assertEqual(saved["status"], "Saved. The suggested plan is updated.")
        self.assertGreaterEqual(saved["previewsAfterSave"], 1)

    def test_saved_values_survive_a_reload(self):
        reloaded = self.out["reloaded"]
        self.assertEqual({k: reloaded[k] for k in ("daily", "pause", "strategy", "summary")},
                         {"daily": "120", "pause": "10", "strategy": "finish_early", "summary": "60"})

    def test_server_refusal_is_shown_next_to_the_field(self):
        refused = self.out["refused"]
        self.assertIn("longer than your daily study target", refused["error"])
        self.assertEqual(refused["invalid"], ["quiz"])

    def test_reset_returns_to_the_defaults(self):
        reset = self.out["reset"]
        self.assertEqual((reset["deletes"], reset["daily"], reset["pause"], reset["strategy"], reset["summary"]),
                         (1, "90", "20", "balanced", "45"))
        self.assertEqual(reset["status"], "Back to the defaults.")

    def test_edit_form_validates_and_saves_a_manual_change(self):
        self.assertIn("Edit", self.out["popoverButtons"])
        first = self.out["first"]
        self.assertEqual(self.out["editOpen"], {"date": first["start"][:10], "time": first["start"][11:16], "minutes": str(first["minutes"])})
        self.assertEqual(self.out["editShort"], {"error": "A study session must be at least 20 minutes long.", "sent": 0})
        conflict = self.out["editConflict"]
        self.assertEqual((conflict["error"], conflict["stillOpen"], conflict["firstStatus"]),
                         ("That time is already taken by another session.", True, "scheduled"))
        self.assertEqual(self.out["editOutside"], "Pick a time inside your available hours.")
        saved = self.out["editSaved"]
        self.assertTrue(saved["closed"])
        self.assertEqual((saved["body"]["target_start"], saved["body"]["duration_minutes"]), ("2026-09-30T20:15:00", 50))
        self.assertEqual(saved["edited"], {"start": "2026-09-30T20:15:00", "end": "2026-09-30T21:05:00", "minutes": 50})
        self.assertEqual(self.out["manualNote"], "Set by you. Auto Plan and suggestions keep this time and length.")

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PreferencesDesktopTests(PreferencesAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PreferencesNarrowDesktopTests(PreferencesAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
