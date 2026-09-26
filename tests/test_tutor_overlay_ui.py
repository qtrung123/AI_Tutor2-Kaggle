"""Real-browser tests for the collapsible Study Session AI Tutor (headless Chrome against the Quiz
Player mock). Desktop (>=1024px): no permanent chat column, "Ask AI Tutor" opens a ~360px slide-over
that does not shrink the learning content, close/reopen keeps the conversation and draft, Escape
closes. Tablet (<1024px): unchanged inline tutor, no launch button.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no network.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, MOCK, find_chrome

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const width = (el) => Math.round(el.getBoundingClientRect().width);
const tutorState = () => {
  const tutor = $("persistent-tutor"), s = getComputedStyle(tutor), r = tutor.getBoundingClientRect();
  return {visible: s.visibility === "visible" && s.display !== "none" && r.right <= window.innerWidth + 1 && r.width > 0,
    width: Math.round(r.width), position: s.position, launchVisible: getComputedStyle($("tutor-launch-button")).display !== "none",
    expanded: $("tutor-launch-button").getAttribute("aria-expanded"), contentWidth: width(document.querySelector(".session-content")),
    workspaceWidth: width(document.querySelector(".session-workspace")), pageOverflow: document.documentElement.scrollWidth > window.innerWidth,
    active: document.activeElement && document.activeElement.id};
};
// Headless --dump-dom does not paint frames, so CSS transitions never finish: test end states.
const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "material");
  await sleep(400);
  out.viewport = window.innerWidth;
  out.placeholder = $("chat-input").placeholder;
  out.initial = tutorState();

  $("tutor-launch-button").click();
  await sleep(400);
  out.opened = tutorState();

  // A conversation line + an unsent draft survive close/reopen (the panel is only hidden).
  addMessage("Earlier tutor answer", "tutor");
  $("chat-input").value = "my draft";
  const messages = $("message-list").children.length;
  $("tutor-close-button").click();
  await sleep(400);
  out.closedByButton = tutorState();
  $("tutor-launch-button").click();
  await sleep(400);
  out.reopened = {...tutorState(), messagesKept: $("message-list").children.length === messages, draft: $("chat-input").value};

  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
  await sleep(400);
  out.afterEscape = tutorState();

  // Other tabs keep the full width too.
  setSessionTab("quiz"); await sleep(200);
  out.quizTab = tutorState();

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


def run_page(window_size):
    work = Path(tempfile.mkdtemp(prefix="tutor_overlay_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")   # app.js's classic-script modules
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=30000", "--dump-dom",
             f"--window-size={window_size}", f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise AssertionError("the page produced no result: " + result.stderr[-1500:])
    return json.loads(html.unescape(match.group(1)))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorOverlayDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page("1440,900")

    def test_no_script_errors_or_overflow(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        for key in ("initial", "opened", "reopened", "afterEscape", "quizTab"):
            self.assertFalse(self.out[key]["pageOverflow"], key)

    def test_no_permanent_chat_column_only_a_launch_button(self):
        initial = self.out["initial"]
        self.assertTrue(initial["launchVisible"])
        self.assertFalse(initial["visible"])
        self.assertEqual(initial["expanded"], "false")
        self.assertEqual(initial["contentWidth"], initial["workspaceWidth"])   # content uses the full workspace
        self.assertEqual(self.out["placeholder"], "Ask a question...")

    def test_launch_opens_a_slide_over_without_shrinking_content(self):
        opened = self.out["opened"]
        self.assertTrue(opened["visible"])
        self.assertEqual(opened["position"], "fixed")
        self.assertEqual(opened["width"], 360)
        self.assertEqual(opened["expanded"], "true")
        self.assertEqual(opened["contentWidth"], self.out["initial"]["contentWidth"])
        self.assertEqual(opened["active"], "chat-input")

    def test_close_and_reopen_preserve_the_conversation(self):
        self.assertFalse(self.out["closedByButton"]["visible"])
        self.assertEqual(self.out["closedByButton"]["active"], "tutor-launch-button")
        reopened = self.out["reopened"]
        self.assertTrue(reopened["visible"])
        self.assertTrue(reopened["messagesKept"])
        self.assertEqual(reopened["draft"], "my draft")

    def test_escape_closes(self):
        self.assertFalse(self.out["afterEscape"]["visible"])
        self.assertEqual(self.out["afterEscape"]["expanded"], "false")

    def test_other_tabs_keep_full_width(self):
        quiz = self.out["quizTab"]
        self.assertEqual(quiz["contentWidth"], quiz["workspaceWidth"])
        self.assertFalse(quiz["visible"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorOverlayTabletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page("900,900")

    def test_tablet_keeps_the_inline_tutor_without_a_launch_button(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        initial = self.out["initial"]
        self.assertFalse(initial["launchVisible"])
        self.assertTrue(initial["visible"])
        self.assertNotEqual(initial["position"], "fixed")
        self.assertFalse(initial["pageOverflow"])


if __name__ == "__main__":
    unittest.main()
