"""Real-browser tests for the Flashcards toolbar and keyboard study (headless Chrome, mock API from
tests/test_flashcard_ui_states.py): Filter and Shuffle stay visible while Language, Regenerate and Manage
Cards sit in a "More" menu (closed by a choice, Escape or a click elsewhere); Left/Right move between
cards and Space flips, but never while typing, in a field or a dialog, and Space on a focused control is
left to that control. Skipped without Chrome.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_flashcard_ui_states import FRONTEND, MOCK, find_chrome

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const shown = (el) => Boolean(el) && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
const more = () => $("flashcards-more");
const card = () => ({position: $("flashcard-position").textContent, side: $("flashcard-side-label").textContent});
const press = (key, target = document.body) => {
  const event = new KeyboardEvent("keydown", {key, bubbles: true, cancelable: true});
  target.dispatchEvent(event);
  return event.defaultPrevented;
};
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "flashcards");
  await loadDocumentFlashcards(); await sleep(300);

  out.closed = {filter: shown($("flashcard-topic-filter")), shuffle: shown($("shuffle-flashcards")), more: shown(more().querySelector("summary")),
    language: shown($("flashcard-language-select")), regenerate: shown($("regenerate-flashcards-button")), manage: shown($("manage-flashcards"))};
  more().querySelector("summary").click(); await sleep(50);
  out.opened = {open: more().open, language: shown($("flashcard-language-select")), regenerate: shown($("regenerate-flashcards-button")),
    manage: shown($("manage-flashcards")), overflow: document.documentElement.scrollWidth > innerWidth + 1};
  press("ArrowRight");   // the menu is open: no card change
  out.keysWhileMenuOpen = card();
  $("flashcard-language-select").focus();
  press("Escape", $("flashcard-language-select"));
  out.escape = {open: more().open, focusOnToggle: document.activeElement === more().querySelector("summary")};
  more().querySelector("summary").click(); await sleep(50);
  document.querySelector(".flashcards-panel h2").click(); await sleep(50);
  out.clickOutside = {open: more().open};

  // Keyboard study.
  document.activeElement?.blur();
  out.start = card();
  out.rightPrevented = press("ArrowRight"); out.afterRight = card();
  press("ArrowRight"); out.afterRightTwice = card();
  press("ArrowLeft"); out.afterLeft = card();
  out.spacePrevented = press(" "); out.afterSpace = card();
  press(" "); out.afterSpaceTwice = card();
  // Not while typing or in a field, and Space on a focused button is left to the button.
  $("chat-input").focus(); press("ArrowRight", $("chat-input")); press(" ", $("chat-input"));
  out.whileTyping = card();
  $("flashcard-topic-filter").focus(); press("ArrowRight", $("flashcard-topic-filter"));
  out.inSelect = card();
  $("flashcard").focus(); out.spaceOnButtonPrevented = press(" ", $("flashcard"));
  out.spaceOnButton = card();
  // Not on another tab.
  setSessionTab("summary"); await sleep(50); press("ArrowRight"); setSessionTab("flashcards"); await sleep(50);
  out.otherTab = card();
  out.shortcuts = {previous: $("previous-flashcard").getAttribute("aria-keyshortcuts"), next: $("next-flashcard").getAttribute("aria-keyshortcuts"),
    flip: $("flashcard").getAttribute("aria-keyshortcuts")};
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class FlashcardToolbarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="flashcard_toolbar_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=40000", "--dump-dom",
             "--window-size=1280,900", f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_secondary_actions_are_grouped_under_more(self):
        self.assertEqual(self.out["closed"], {"filter": True, "shuffle": True, "more": True,
                                              "language": False, "regenerate": False, "manage": False})
        self.assertEqual(self.out["opened"], {"open": True, "language": True, "regenerate": True, "manage": True, "overflow": False})

    def test_more_menu_closes_on_escape_and_outside_click(self):
        self.assertEqual(self.out["keysWhileMenuOpen"], {"position": "1 / 3", "side": "Front"})
        self.assertEqual(self.out["escape"], {"open": False, "focusOnToggle": True})
        self.assertFalse(self.out["clickOutside"]["open"])

    def test_left_right_navigate_and_space_flips(self):
        self.assertEqual(self.out["start"], {"position": "1 / 3", "side": "Front"})
        self.assertTrue(self.out["rightPrevented"])
        self.assertEqual(self.out["afterRight"], {"position": "2 / 3", "side": "Front"})
        self.assertEqual(self.out["afterRightTwice"]["position"], "3 / 3")
        self.assertEqual(self.out["afterLeft"]["position"], "2 / 3")
        self.assertTrue(self.out["spacePrevented"])   # no page scroll
        self.assertEqual(self.out["afterSpace"], {"position": "2 / 3", "side": "Back"})
        self.assertEqual(self.out["afterSpaceTwice"]["side"], "Front")

    def test_shortcuts_never_interfere_with_typing_fields_or_focused_controls(self):
        self.assertEqual(self.out["whileTyping"], {"position": "2 / 3", "side": "Front"})
        self.assertEqual(self.out["inSelect"], {"position": "2 / 3", "side": "Front"})
        self.assertFalse(self.out["spaceOnButtonPrevented"])   # the focused button handles Space itself
        self.assertEqual(self.out["spaceOnButton"], {"position": "2 / 3", "side": "Front"})
        self.assertEqual(self.out["otherTab"], {"position": "2 / 3", "side": "Front"})

    def test_shortcuts_are_announced(self):
        self.assertEqual(self.out["shortcuts"], {"previous": "ArrowLeft", "next": "ArrowRight", "flip": "Space"})
