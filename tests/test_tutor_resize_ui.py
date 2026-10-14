"""Real-browser tests for the resizable Study Session AI Tutor (headless Chrome against the Quiz Player
mock). Desktop (>=1024px): the open tutor is docked beside the study content with a draggable divider;
dragging, the keyboard and "Expand"/"Restore" change its width within limits (chat >=320px, content
>=420px), the preferred width is remembered in localStorage, and messages, citations, the composer
and the Sources drawer reflow without horizontal overflow while it resizes. Tablet and phone keep a
single full-width chat under the content. Skipped when Chrome is not installed.
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
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const rect = (el) => el.getBoundingClientRect();
const shown = (el) => { const s = getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden" && rect(el).width > 0; };
const stored = () => localStorage.getItem("aiTutorChatWidth");
const LONG_TOKEN = "https://example.com/" + "a".repeat(220);
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  await openStudySession("lecture.pdf", "material");
  await sleep(400);
  out.viewport = innerWidth;
  const workspace = document.querySelector(".session-workspace"), content = document.querySelector(".session-content");
  const tutor = $("persistent-tutor"), resizer = $("tutor-resizer"), expand = $("tutor-expand-button");
  const snap = () => {
    const t = rect(tutor), c = rect(content), list = $("message-list"), form = $("chat-form");
    const bubbles = [...tutor.querySelectorAll(".message")];
    return {
      tutorWidth: Math.round(t.width), contentWidth: Math.round(c.width), workspaceWidth: Math.round(rect(workspace).width),
      tutorLeft: Math.round(t.left), contentRight: Math.round(c.right), tutorBelowContent: Math.round(t.top) >= Math.round(c.bottom) - 1,
      overlap: c.right > t.left + 1 && c.bottom > t.top + 1 && t.bottom > c.top + 1 && t.right > c.left + 1,
      pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      listOverflow: list.scrollWidth > list.clientWidth + 1,
      bubblesInside: bubbles.every((b) => rect(b).right <= t.right + 1 && rect(b).left >= t.left - 1),
      formInside: rect(form).right <= t.right + 1 && rect(form).left >= t.left - 1,
      inputWidth: Math.round(rect($("chat-input")).width),
      resizerShown: shown(resizer), expandShown: shown(expand), tutorShown: shown(tutor),
      ariaNow: resizer.getAttribute("aria-valuenow"), stored: stored(),
      expanded: document.body.classList.contains("tutor-expanded"), pressed: expand.getAttribute("aria-pressed"),
    };
  };
  if (tutorOverlayQuery.matches) { $("tutor-launch-button").click(); await sleep(300); }
  for (let i = 0; i < 6; i += 1) {
    addMessage("Question " + i + ": how does " + LONG_TOKEN + " relate to the lecture?", "user");
    const row = addMessage("**Answer " + i + "** with a long unbroken reference " + LONG_TOKEN + "\n- first point\n- second point", "tutor");
    appendMessageMeta(row, "supported", [{chunk_id: "a"}, {chunk_id: "b"}]);
  }
  const messageCount = $("message-list").children.length;
  $("chat-input").value = "my draft";
  out.initial = snap();
  if (tutorOverlayQuery.matches) {
    const pointer = (target, type, x) => target.dispatchEvent(new PointerEvent(type, {bubbles: true, pointerId: 9, button: 0, isPrimary: true, clientX: x}));
    const drag = async (x) => { const r = rect(resizer); pointer(resizer, "pointerdown", r.left + r.width / 2); pointer(window, "pointermove", (r.left + x) / 2); pointer(window, "pointermove", x); pointer(window, "pointerup", x); await sleep(80); };
    const handle = rect(resizer).left + rect(resizer).width / 2;
    {
      const r = rect(resizer); pointer(resizer, "pointerdown", r.left + r.width / 2); pointer(window, "pointermove", handle - 100);
      out.duringDrag = {resizing: document.body.classList.contains("tutor-resizing"), ...snap()};
      pointer(window, "pointerup", handle - 100); await sleep(80);
    }
    out.afterWiden = snap();
    await drag(0);
    out.dragToMax = snap();
    await drag(innerWidth);
    out.dragToMin = snap();
    resizer.dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowLeft", bubbles: true})); await sleep(50);
    out.afterArrowLeft = snap();
    expand.click(); await sleep(80);
    out.expandedState = snap();
    expand.click(); await sleep(80);
    out.restoredState = snap();
    expand.click(); await sleep(80);
    await drag(handle);   // dragging leaves the expanded mode and stores the new width
    out.dragWhileExpanded = snap();
    $("tutor-close-button").click(); await sleep(200);
    out.closed = snap();
    $("tutor-launch-button").click(); await sleep(200);
    out.reopened = {...snap(), messagesKept: $("message-list").children.length === messageCount, draft: $("chat-input").value};
    setSourcesDrawerOpen(true); await sleep(100);
    const card = rect($("conversation-sources-panel")), t = rect(tutor);
    out.sourcesDrawer = {inside: card.left >= t.left - 1 && card.right <= t.right + 1, applyShown: shown($("apply-conversation-sources-button"))};
    setSourcesDrawerOpen(false); await sleep(80);
    resizer.dispatchEvent(new MouseEvent("dblclick", {bubbles: true})); await sleep(80);
    out.afterReset = snap();
  }
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

PHONE_HOST = """<!doctype html><html><body style="margin:0">
<iframe src="index.html" style="width:{width}px;height:{height}px;border:0"></iframe>
<script>window.addEventListener("message", (event) => {{ const pre = document.createElement("pre"); pre.id = "harness-out";
pre.textContent = event.data; document.body.appendChild(pre); }});</script></body></html>"""


def run_page(width, height, prelude=""):
    """Runs the driver at width x height (inside an iframe below 600px, which headless Chrome's
    window cannot reach). prelude runs before app.js, e.g. to seed a saved chat width."""
    work = Path(tempfile.mkdtemp(prefix=f"tutor_resize_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(MOCK + prelude, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        entry, window_size = work / "index.html", f"{width},{height}"
        if width < 600:
            entry, window_size = work / "phone.html", f"800,{height}"
            entry.write_text(PHONE_HOST.format(width=width, height=height), encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000", "--dump-dom",
             f"--window-size={window_size}", f"--user-data-dir={work / 'profile'}", entry.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise AssertionError("the page produced no result: " + result.stderr[-1500:])
    return json.loads(html.unescape(match.group(1)))


class LayoutAssertions:
    def assert_clean(self, state, label):
        self.assertFalse(state["pageOverflow"], label)
        self.assertFalse(state["listOverflow"], label)
        self.assertFalse(state["overlap"], label)
        self.assertTrue(state["bubblesInside"], label)
        self.assertTrue(state["formInside"], label)

    def assert_docked(self, state, label):
        """Content, 12px divider and chat fill the workspace side by side within the limits."""
        self.assert_clean(state, label)
        self.assertEqual(state["contentWidth"] + 12 + state["tutorWidth"], state["workspaceWidth"], label)
        self.assertGreaterEqual(state["tutorWidth"], 320, label)
        self.assertGreaterEqual(state["contentWidth"], 420, label)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorResizeDesktopTests(LayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(1440, 900)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_open_tutor_is_docked_beside_the_content_with_messages(self):
        initial = self.out["initial"]
        self.assert_docked(initial, "initial")
        self.assertEqual(initial["tutorWidth"], 380)
        self.assertTrue(initial["resizerShown"] and initial["expandShown"])
        self.assertEqual(initial["ariaNow"], "380")
        self.assertIsNone(initial["stored"])

    def test_dragging_the_divider_resizes_and_remembers_the_chat_width(self):
        during = self.out["duringDrag"]
        self.assertTrue(during["resizing"])
        self.assert_docked(during, "duringDrag")
        widened = self.out["afterWiden"]
        self.assert_docked(widened, "afterWiden")
        self.assertEqual(widened["tutorWidth"], 480)
        self.assertEqual((widened["stored"], widened["ariaNow"]), ("480", "480"))
        self.assertGreater(widened["inputWidth"], self.out["initial"]["inputWidth"])   # composer follows the width

    def test_width_is_limited_on_both_sides(self):
        biggest, smallest = self.out["dragToMax"], self.out["dragToMin"]
        self.assert_docked(biggest, "dragToMax")
        self.assertEqual(biggest["contentWidth"], 420)
        self.assert_docked(smallest, "dragToMin")
        self.assertEqual(smallest["tutorWidth"], 320)
        self.assertEqual(smallest["stored"], "320")
        self.assertEqual(self.out["afterArrowLeft"]["tutorWidth"], 336)

    def test_expand_and_restore(self):
        expanded, restored = self.out["expandedState"], self.out["restoredState"]
        self.assert_docked(expanded, "expanded")
        self.assertEqual(expanded["contentWidth"], 420)
        self.assertEqual((expanded["expanded"], expanded["pressed"]), (True, "true"))
        self.assertEqual(expanded["stored"], "336")   # expanding does not overwrite the preference
        self.assert_docked(restored, "restored")
        self.assertEqual((restored["tutorWidth"], restored["expanded"], restored["pressed"]), (336, False, "false"))
        dragged = self.out["dragWhileExpanded"]
        self.assertFalse(dragged["expanded"])
        self.assert_docked(dragged, "dragWhileExpanded")
        self.assertEqual(dragged["stored"], str(dragged["tutorWidth"]))

    def test_close_and_reopen_keep_conversation_draft_and_width(self):
        closed = self.out["closed"]
        self.assertFalse(closed["tutorShown"] or closed["resizerShown"])
        self.assertEqual(closed["contentWidth"], closed["workspaceWidth"])
        reopened = self.out["reopened"]
        self.assert_docked(reopened, "reopened")
        self.assertEqual(reopened["tutorWidth"], self.out["dragWhileExpanded"]["tutorWidth"])
        self.assertTrue(reopened["messagesKept"])
        self.assertEqual(reopened["draft"], "my draft")
        self.assertEqual(self.out["sourcesDrawer"], {"inside": True, "applyShown": True})

    def test_double_click_resets_to_the_default(self):
        reset = self.out["afterReset"]
        self.assertEqual((reset["tutorWidth"], reset["stored"]), (380, None))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorResizeSavedWidthTests(LayoutAssertions, unittest.TestCase):
    def test_saved_width_is_restored(self):
        out = run_page(1440, 900, 'localStorage.setItem("aiTutorChatWidth", "520");')
        self.assertEqual(out["errors"], [])
        self.assert_docked(out["initial"], "initial")
        self.assertEqual(out["initial"]["tutorWidth"], 520)

    def test_saved_width_too_wide_for_a_narrow_window_is_clamped(self):
        out = run_page(1100, 800, 'localStorage.setItem("aiTutorChatWidth", "900");')
        self.assertEqual(out["errors"], [])
        initial = out["initial"]
        self.assert_docked(initial, "initial")
        self.assertEqual(initial["contentWidth"], 420)
        self.assertEqual(initial["stored"], "900")   # the preference survives for wider windows


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorResizeTabletAndPhoneTests(LayoutAssertions, unittest.TestCase):
    def assert_full_width_under_content(self, out):
        self.assertNotIn("fatal", out)
        self.assertEqual(out["errors"], [])
        initial = out["initial"]
        self.assert_clean(initial, "initial")
        self.assertTrue(initial["tutorShown"])
        self.assertTrue(initial["tutorBelowContent"])
        self.assertEqual(initial["tutorWidth"], initial["workspaceWidth"])
        self.assertFalse(initial["resizerShown"] or initial["expandShown"])

    def test_tablet_uses_a_full_width_chat(self):
        self.assert_full_width_under_content(run_page(900, 900, 'localStorage.setItem("aiTutorChatWidth", "600");'))

    def test_phone_uses_a_full_width_chat(self):
        out = run_page(390, 844, 'localStorage.setItem("aiTutorChatWidth", "600");')
        self.assertLessEqual(out["viewport"], 400)
        self.assert_full_width_under_content(out)


if __name__ == "__main__":
    unittest.main()
