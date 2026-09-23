"""Real-browser tests for sidebar + navigation (headless Chrome, mocked API reused from
tests/test_session_overview_ui.py). The same driver runs at desktop (1280px) and phone (390px)
width: desktop keeps the sidebar in place with active states and Recent-session navigation; phone
turns it into a drawer (menu button, backdrop/close/Escape/navigation all close it) with no
horizontal overflow. Skipped when Chrome is not installed.
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
from tests.test_session_overview_ui import MOCK as OVERVIEW_MOCK

MOCK = OVERVIEW_MOCK + r"""
const overviewFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/dashboard") return json({mastery: [], summary: {}, metrics: {documents: 1},
    materials: [{document_id: "lecture.pdf", document_name: "Lecture", topic_count: 1}]});
  return overviewFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: [], width: window.innerWidth};
// Phone runs inside a 390px iframe (headless Chrome's window cannot be that narrow): the result is
// posted to the parent page, which prints it for --dump-dom.
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const sidebar = () => $("app-sidebar");
const visible = (el) => { const s = getComputedStyle(el); const b = el.getBoundingClientRect(); return s.display !== "none" && s.visibility !== "hidden" && b.width > 0 && b.right > 0 && b.left < innerWidth; };
const state = () => ({
  open: document.body.classList.contains("sidebar-open"),
  expanded: $("sidebar-menu-button").getAttribute("aria-expanded"),
  sidebarVisible: visible(sidebar()),
  backdropHidden: $("sidebar-backdrop").hidden,
});
const overflow = () => document.documentElement.scrollWidth > window.innerWidth + 1;
const activeNav = () => [...document.querySelectorAll(".nav-item.active")].map((b) => b.dataset.page);
const recent = () => [...document.querySelectorAll(".sidebar-recent-item")];
(async () => {
  await sleep(1500);
  // CSS transitions do not advance under headless virtual time: assert end states directly.
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  setPage("overview"); await sleep(100);
  out.home = {
    activeNav: activeNav(), ariaCurrent: document.querySelector(".nav-item.active")?.getAttribute("aria-current"),
    menuButtonVisible: visible($("sidebar-menu-button")), sidebarVisible: visible(sidebar()),
    recent: recent().map((b) => b.textContent), overflow: overflow(),
    brandColor: getComputedStyle(sidebar().querySelector(".brand span")).color,
    libraryItemVisible: visible(document.querySelector(".nav-menu .library-item")),
    newSessionReachable: Boolean($("upload-source-button")), logoutReachable: Boolean($("logout-button")),
  };
  if (innerWidth <= 720) {
    // Phone: drawer opens from the menu button, closes via backdrop, close button and Escape.
    out.closed = state();
    $("sidebar-menu-button").click(); await sleep(300);
    out.opened = { ...state(), focus: document.activeElement?.id, newSessionVisible: visible($("upload-source-button")), logoutVisible: visible($("logout-button")) };
    $("sidebar-backdrop").click(); await sleep(300);
    out.afterBackdrop = state();
    $("sidebar-menu-button").click(); await sleep(300);
    $("sidebar-close-button").click(); await sleep(300);
    out.afterClose = { ...state(), focus: document.activeElement?.id };
    $("sidebar-menu-button").click(); await sleep(300);
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(300);
    out.afterEscape = state();
    // Selecting Study Planner closes it and navigates.
    $("sidebar-menu-button").click(); await sleep(300);
    document.querySelector('.nav-item[data-page="planner"]').click(); await sleep(400);
    out.afterNavSelect = { ...state(), page: document.body.dataset.page };
    // Selecting a Recent session closes it and opens that session.
    $("sidebar-menu-button").click(); await sleep(300);
    recent()[0].click(); await sleep(800);
  } else {
    document.querySelector('.nav-item[data-page="planner"]').click(); await sleep(400);
    out.afterNavSelect = { page: document.body.dataset.page, activeNav: activeNav() };
    recent()[0].click(); await sleep(800);
  }
  out.session = {
    ...state(), page: document.body.dataset.page, tab: document.body.dataset.sessionTab, activeDocumentId,
    activeNav: activeNav(), recentActive: recent().filter((b) => b.classList.contains("active")).map((b) => b.dataset.documentId),
    recentAria: recent()[0].getAttribute("aria-current"), overflow: overflow(),
    contentWidth: Math.round(document.querySelector(".main-content").getBoundingClientRect().width),
    viewportWidth: document.documentElement.clientWidth,
  };
  // Home returns to the library and clears the Recent highlight.
  $("session-home-button").click(); await sleep(300);
  out.backHome = { page: document.body.dataset.page, activeNav: activeNav(), recentActive: recent().filter((b) => b.classList.contains("active")).length };
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


PHONE_HOST = """<!doctype html><html><body style="margin:0">
<iframe src="index.html" style="width:{width}px;height:{height}px;border:0"></iframe>
<script>window.addEventListener("message", (event) => {{ const pre = document.createElement("pre"); pre.id = "harness-out";
pre.textContent = event.data; document.body.appendChild(pre); }});</script></body></html>"""


def run_at_width(width, height):
    """Desktop: the page itself at a real window size. Phone: the page inside a width-px iframe."""
    work = Path(tempfile.mkdtemp(prefix=f"sidebar_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        entry = work / "index.html"
        if width < 600:
            entry = work / "phone.html"
            entry.write_text(PHONE_HOST.format(width=width, height=height), encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000",
             "--window-size=1280,900", "--dump-dom", f"--user-data-dir={work / 'profile'}", entry.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        return json.loads(html.unescape(match.group(1)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SidebarDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_sidebar_is_in_place_without_a_menu_button(self):
        home = self.out["home"]
        self.assertTrue(home["sidebarVisible"])
        self.assertFalse(home["menuButtonVisible"])
        self.assertFalse(home["overflow"])
        self.assertTrue(home["newSessionReachable"] and home["logoutReachable"])
        self.assertFalse(home["libraryItemVisible"])   # non-interactive duplicate of Home is hidden
        self.assertNotEqual(home["brandColor"], "rgb(244, 255, 247)")   # readable on the white sidebar

    def test_active_nav_state_follows_the_page(self):
        home = self.out["home"]
        self.assertEqual(home["activeNav"], ["overview"])
        self.assertEqual(home["ariaCurrent"], "page")
        self.assertEqual(self.out["afterNavSelect"], {"page": "planner", "activeNav": ["planner"]})

    def test_recent_session_opens_and_is_highlighted(self):
        self.assertEqual(self.out["home"]["recent"], ["Lecture"])
        session = self.out["session"]
        self.assertEqual((session["page"], session["tab"], session["activeDocumentId"]), ("session", "overview", "lecture.pdf"))
        self.assertEqual(session["activeNav"], [])
        self.assertEqual(session["recentActive"], ["lecture.pdf"])
        self.assertEqual(session["recentAria"], "page")
        self.assertEqual(self.out["backHome"], {"page": "overview", "activeNav": ["overview"], "recentActive": 0})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SidebarMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_no_script_errors_and_phone_width(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertLessEqual(self.out["width"], 720)

    def test_sidebar_is_a_closed_drawer_behind_a_menu_button(self):
        home = self.out["home"]
        self.assertTrue(home["menuButtonVisible"])
        self.assertFalse(home["sidebarVisible"])
        self.assertFalse(home["overflow"])
        self.assertEqual(self.out["closed"], {"open": False, "expanded": "false", "sidebarVisible": False, "backdropHidden": True})

    def test_menu_opens_the_drawer_with_new_session_and_sign_out(self):
        opened = self.out["opened"]
        self.assertEqual((opened["open"], opened["expanded"], opened["sidebarVisible"], opened["backdropHidden"]), (True, "true", True, False))
        self.assertEqual(opened["focus"], "sidebar-close-button")
        self.assertTrue(opened["newSessionVisible"])
        self.assertTrue(opened["logoutVisible"])

    def test_backdrop_close_button_and_escape_close_the_drawer(self):
        closed = {"open": False, "expanded": "false", "sidebarVisible": False, "backdropHidden": True}
        self.assertEqual(self.out["afterBackdrop"], closed)
        self.assertEqual({k: v for k, v in self.out["afterClose"].items() if k != "focus"}, closed)
        self.assertEqual(self.out["afterClose"]["focus"], "sidebar-menu-button")
        self.assertEqual(self.out["afterEscape"], closed)

    def test_selecting_navigation_closes_the_drawer(self):
        nav = self.out["afterNavSelect"]
        self.assertEqual((nav["open"], nav["page"]), (False, "planner"))
        session = self.out["session"]
        self.assertFalse(session["open"])
        self.assertEqual((session["page"], session["activeDocumentId"]), ("session", "lecture.pdf"))

    def test_session_uses_full_width_without_overflow(self):
        session = self.out["session"]
        self.assertFalse(session["overflow"])
        self.assertGreaterEqual(session["contentWidth"], session["viewportWidth"] - 2)   # sidebar takes no column


if __name__ == "__main__":
    unittest.main()
