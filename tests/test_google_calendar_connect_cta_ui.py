"""Real-browser tests for the always-visible Google Calendar control in the Planner toolbar.

The detailed Google Calendar settings live in the Materials panel, a popover that is closed by
default, so the Connect action must also be reachable from the top toolbar: the desktop calendar
toolbar (1280px) and the step-by-step layout's header (390px). Runs with zero plans, materials and
sessions. Skipped when Chrome is not installed.
"""

import unittest

import tests.test_google_calendar_planner_ui as google_ui
from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }   // narrow widths run in an iframe
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const slotId = () => (plannerIsDesktop() ? "pcal-gcal-cta" : "planner-gcal-cta");
const control = () => {
  const slot = $(slotId());
  const button = slot.querySelector("button");
  const rect = slot.getBoundingClientRect();
  return {slot: slotId(), shown: visible(slot), text: slot.textContent.trim(), button: button ? [button.textContent, button.disabled, visible(button)] : null,
    inToolbar: !!slot.closest(".pcal-toolbar-row, .planner-toolbar"), inMaterials: !!slot.closest("#pcal-materials-rail"),
    onScreen: rect.width > 0 && rect.height > 0 && rect.right <= window.innerWidth + 1,
    togglesOnScreen: !plannerIsDesktop() || $("pcal-queue-toggle").getBoundingClientRect().right <= window.innerWidth + 1};
};
(async () => {
  await sleep(1500);
  const G = window.__google;
  const P = window.__planner;
  let navigatedTo = null;
  plannerGoogleNavigate = (url) => { navigatedTo = url; };

  setPage("planner"); await sleep(900);
  out.empty = {plans: P.plans.length, materials: P.materials.length, sessions: P.sessions.length};
  out.materialsOpen = $("planner-workspace").classList.contains("is-materials-open");
  out.disconnected = control();
  $(slotId()).querySelector("button").click();
  out.connectUrl = navigatedTo;

  G.connected = true;
  await plannerLoadGoogleCalendar(); await sleep(400);
  out.connected = control();
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


FAILING_STATUS_MOCK = google_ui.MOCK + r"""
window.__statusDown = true;
const statusAwareFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, "http://x");
  if (window.__statusDown && url.pathname.endsWith("/api/integrations/google-calendar/status")) {
    window.__google.calls.push("GET " + url.pathname + " (503)");
    return new Response(JSON.stringify({detail: "unavailable"}), {status: 503, headers: {"Content-Type": "application/json"}});
  }
  return statusAwareFetch(input, init);
};
"""

FAILING_STATUS_DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const slot = () => ({shown: visible($("pcal-gcal-cta")), text: $("pcal-gcal-cta").textContent.trim(),
  buttons: [...$("pcal-gcal-cta").querySelectorAll("button")].map((b) => [b.textContent, b.disabled, visible(b)])});
(async () => {
  await sleep(1500);
  let navigatedTo = null;
  plannerGoogleNavigate = (url) => { navigatedTo = url; };
  setPage("planner"); await sleep(900);
  out.failed = slot();
  out.statusCallsBeforeRetry = window.__google.calls.filter((c) => c.includes("/status")).length;
  window.__statusDown = false;
  [...$("pcal-gcal-cta").querySelectorAll("button")].find((b) => b.textContent === "Retry").click();
  await sleep(600);
  out.afterRetry = slot();
  out.statusCallsAfterRetry = window.__google.calls.filter((c) => c.includes("/status")).length;
  out.navigatedTo = navigatedTo;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarStatusFailureCtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=FAILING_STATUS_MOCK, driver=FAILING_STATUS_DRIVER)

    def test_failed_status_shows_a_visible_error_and_retry_instead_of_hiding(self):
        self.assertEqual(self.out["errors"], [])
        failed = self.out["failed"]
        self.assertTrue(failed["shown"])
        self.assertIn("Google Calendar unavailable", failed["text"])
        # No guessed Connect button while the status is unknown, and OAuth is never started.
        self.assertEqual(failed["buttons"], [["Retry", False, True]])
        self.assertIsNone(self.out["navigatedTo"])

    def test_retry_reloads_the_status_and_restores_connect(self):
        self.assertEqual(self.out["statusCallsAfterRetry"], self.out["statusCallsBeforeRetry"] + 1)
        after = self.out["afterRetry"]
        self.assertTrue(after["shown"])
        self.assertEqual(after["buttons"], [["Connect Google Calendar", False, True]])


RECONNECT_MOCK = google_ui.MOCK + r"""
const reconnectFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, "http://x");
  if (url.pathname.endsWith("/api/integrations/google-calendar/status")) {
    const status = await (await reconnectFetch(input, init)).json();
    return new Response(JSON.stringify({...status, configured: true, connected: true, reconnect_required: true}),
      {status: 200, headers: {"Content-Type": "application/json"}});
  }
  return reconnectFetch(input, init);
};
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarReconnectCtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=RECONNECT_MOCK, driver=DRIVER)

    def test_reconnect_required_shows_reconnect_through_the_existing_oauth_flow(self):
        self.assertEqual(self.out["errors"], [])
        state = self.out["disconnected"]   # DRIVER's first snapshot, taken with the mocked status as-is
        self.assertEqual(state["slot"], "pcal-gcal-cta")
        self.assertTrue(state["shown"] and state["inToolbar"])
        self.assertEqual(state["button"], ["Reconnect Google Calendar", False, True])
        self.assertNotIn("Google Calendar connected", state["text"])
        self.assertEqual(self.out["connectUrl"], "/api/integrations/google-calendar/connect")


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarConnectCtaDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=google_ui.MOCK, driver=DRIVER)

    def test_no_script_errors(self):
        self.assertEqual(self.out["errors"], [])

    def test_disconnected_toolbar_shows_connect_google_calendar_with_nothing_planned(self):
        self.assertEqual(self.out["empty"], {"plans": 0, "materials": 0, "sessions": 0})
        self.assertFalse(self.out["materialsOpen"])  # visible without opening the Materials panel
        state = self.out["disconnected"]
        self.assertEqual(state["slot"], "pcal-gcal-cta")
        self.assertTrue(state["shown"] and state["inToolbar"] and state["onScreen"])
        self.assertFalse(state["inMaterials"])
        self.assertEqual(state["button"], ["Connect Google Calendar", False, True])

    def test_clicking_connect_uses_the_existing_oauth_route(self):
        self.assertEqual(self.out["connectUrl"], "/api/integrations/google-calendar/connect")

    def test_connected_state_replaces_the_connect_cta(self):
        state = self.out["connected"]
        self.assertTrue(state["shown"])
        self.assertIsNone(state["button"])
        self.assertEqual(state["text"], "✓ Google Calendar connected")


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarConnectCtaNarrowDesktopTests(unittest.TestCase):
    """The calendar toolbar row does not wrap: the control must still fit at a narrow desktop width."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, mock=google_ui.MOCK, driver=DRIVER)

    def test_connect_control_fits_the_toolbar(self):
        self.assertEqual(self.out["errors"], [])
        state = self.out["disconnected"]
        self.assertEqual(state["slot"], "pcal-gcal-cta")
        self.assertTrue(state["shown"] and state["inToolbar"] and state["onScreen"])
        self.assertTrue(state["togglesOnScreen"])  # the Materials / Study queue toggles are not pushed off
        self.assertEqual(state["button"], ["Connect Google Calendar", False, True])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarConnectCtaMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844, mock=google_ui.MOCK, driver=DRIVER)

    def test_no_script_errors(self):
        self.assertEqual(self.out["errors"], [])

    def test_mobile_header_has_an_accessible_connect_control(self):
        state = self.out["disconnected"]
        self.assertEqual(state["slot"], "planner-gcal-cta")
        self.assertTrue(state["shown"] and state["inToolbar"] and state["onScreen"])
        self.assertEqual(state["button"], ["Connect Google Calendar", False, True])
        self.assertEqual(self.out["connectUrl"], "/api/integrations/google-calendar/connect")

    def test_mobile_connected_state(self):
        self.assertEqual(self.out["connected"]["text"], "✓ Google Calendar connected")
        self.assertIsNone(self.out["connected"]["button"])


if __name__ == "__main__":
    unittest.main()
