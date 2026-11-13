"""Real-browser layout test for the Home dashboard (headless Chrome, mock API from
tests/test_subject_management_ui.py). Home is fluid: it starts ~28px after the sidebar and grows with the
viewport instead of a 1120px column centered between two empty bands; on wide screens Today's Schedule /
Upcoming take ~60-65% and Study progress ~35-40% side by side; tablet and phone stack the cards in one
column without horizontal overflow. Runs at 1920, 1440, 900 and 390px. Skipped without Chrome.
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
from tests.test_subject_management_ui import MOCK

DRIVER = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = {errors: []};
  window.addEventListener("error", (e) => out.errors.push(String(e.message)));
  const publish = () => { const text = JSON.stringify(out);
    if (window.parent !== window) { window.parent.postMessage(text, "*"); return; }
    const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = text; document.body.appendChild(pre); };
  try {
    await sleep(1500);
    plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
    window.__planner.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
    const s = (id, doc, title, start, end) => ({session_id: id, plan_id: "plan-1", document_id: doc, document_title: title, activity_type: "summary",
      scheduled_start: start, scheduled_end: end, duration_minutes: 30, status: "scheduled", reason: {code: "new_material", message: "New material to learn"}, artifact_id: null});
    window.__planner.sessionsByPlan = {"plan-1": [s("a", "mkt.pdf", "Marketing", "2026-09-24T14:00:00", "2026-09-24T14:30:00"),
                                                  s("b", "stats.pdf", "Statistics", "2026-09-25T18:00:00", "2026-09-25T18:30:00")]};
    setPage("overview"); await loadTodayPlan(); await sleep(1000);
    const r = (el) => el.getBoundingClientRect();
    const home = document.querySelector(".home-page"), dash = document.querySelector(".home-dashboard");
    const today = document.getElementById("today-plan"), upcoming = document.getElementById("home-upcoming"), progress = document.getElementById("home-progress");
    const phone = innerWidth <= 720, viewport = document.documentElement.clientWidth;
    out.layout = {phone,
      gapLeft: Math.round(r(home).left - (phone ? 0 : r(document.getElementById("app-sidebar")).right)),
      gapRight: Math.round(viewport - r(home).right),
      mainShare: Math.round(100 * r(today).width / r(dash).width), sideShare: Math.round(100 * r(progress).width / r(dash).width),
      sideBySide: Math.abs(r(today).top - r(progress).top) < 2 && r(progress).left > r(today).right,
      upcomingUnderToday: r(upcoming).top >= r(today).bottom && Math.abs(r(upcoming).left - r(today).left) < 2,
      overflowX: document.documentElement.scrollWidth > innerWidth + 1};
  } catch (error) { out.fatal = String(error && error.stack || error); }
  publish();
})();
"""

PHONE_HOST = """<!doctype html><html><body style="margin:0"><iframe src="index.html" style="width:{w}px;height:{h}px;border:0"></iframe>
<script>addEventListener("message", (e) => {{ const p = document.createElement("pre"); p.id = "harness-out"; p.textContent = e.data;
document.body.appendChild(p); }});</script></body></html>"""


def run_at_width(width, height):
    work = Path(tempfile.mkdtemp(prefix=f"home_layout_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        entry, size = work / "index.html", f"{width},{height}"
        if width < 600:   # headless Chrome's window cannot be that narrow: a phone-width iframe instead
            entry = work / "phone.html"
            entry.write_text(PHONE_HOST.format(w=width, h=height), encoding="utf-8")
            size = "1280,900"
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=30000", "--dump-dom",
             f"--window-size={size}", f"--user-data-dir={work / 'profile'}", entry.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        return json.loads(html.unescape(match.group(1)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


class HomeLayoutAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_no_horizontal_overflow(self):
        self.assertFalse(self.out["layout"]["overflowX"])


class WideHomeLayoutAssertions(HomeLayoutAssertions):
    def test_dashboard_starts_right_after_the_sidebar_and_fills_the_width(self):
        layout = self.out["layout"]
        self.assertGreaterEqual(layout["gapLeft"], 24)
        self.assertLessEqual(layout["gapLeft"], 32)   # no empty band between the sidebar and Today's Schedule
        self.assertLessEqual(layout["gapRight"], 32)   # nor right of Study progress

    def test_schedule_and_progress_share_the_width(self):
        layout = self.out["layout"]
        self.assertTrue(layout["sideBySide"])
        self.assertTrue(layout["upcomingUnderToday"])
        self.assertTrue(60 <= layout["mainShare"] <= 65, layout["mainShare"])
        self.assertTrue(35 <= layout["sideShare"] <= 40, layout["sideShare"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class HomeLayout1920Tests(WideHomeLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1920, 1080)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class HomeLayout1440Tests(WideHomeLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1440, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class HomeLayoutTabletTests(HomeLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(900, 1000)

    def test_cards_stack_in_one_column(self):
        layout = self.out["layout"]
        self.assertFalse(layout["sideBySide"])
        self.assertEqual((layout["mainShare"], layout["sideShare"]), (100, 100))
        self.assertLessEqual(layout["gapRight"], 32)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class HomeLayoutPhoneTests(HomeLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 1400)

    def test_cards_stack_in_one_column(self):
        layout = self.out["layout"]
        self.assertFalse(layout["sideBySide"])
        self.assertEqual((layout["mainShare"], layout["sideShare"]), (100, 100))


if __name__ == "__main__":
    unittest.main()
