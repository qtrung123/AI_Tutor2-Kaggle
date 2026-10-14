"""Real-browser integration tests across the AI Tutor UI tasks (headless Chrome, stateful conversation
mock from tests/test_tutor_conversations_ui.py): the last chat of a document is reopened after a reload;
resizing or collapsing the main sidebar while the docked Tutor is open never overflows; a slow earlier
"open chat" response cannot replace the chat picked last; a reply that arrives after switching chats
stays out of the open chat (it is shown in its own chat); a new chat survives switching away and back;
re-rendering the chat list never scrolls the page on a phone; long Markdown answers fit on a phone.
Skipped when Chrome is not installed.
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
from tests.test_tutor_conversations_ui import CONVERSATION_MOCK
from tests.test_tutor_resize_ui import PHONE_HOST

# Delays per request path (ms) on top of the conversation mock, plus "last chat" from a previous visit.
INTEGRATION_MOCK = CONVERSATION_MOCK + r"""
window.__delays = {};
const conversationFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const delay = window.__delays[p];
  if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
  return conversationFetch(input, init);
};
localStorage.setItem("activeConversationId:u1", "c-other");
localStorage.setItem("activeConversationId:u1:lecture.pdf", "c-timers");
"""

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
const activeTitle = () => document.querySelector("#conversation-list .conversation-item.active strong")?.textContent || null;
const messageTexts = () => [...$("message-list").querySelectorAll(".message-body")].map((el) => el.textContent);
const overflow = () => document.documentElement.scrollWidth > innerWidth + 1;
const docked = () => {
  const content = rect(document.querySelector(".session-content")), tutor = rect($("persistent-tutor"));
  return {content: Math.round(content.width), tutor: Math.round(tutor.width), overlap: content.right > tutor.left + 1, overflow: overflow(),
    tutorInside: tutor.right <= innerWidth + 1};
};
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  out.viewport = innerWidth;
  out.beforeSession = {activeId: activeConversation?.id || null};   // global "last chat" (another document)
  await openStudySession("lecture.pdf", "overview");
  await sleep(400);
  out.restored = {active: activeTitle(), activeId: activeConversation?.id, messages: messageTexts()};
  const desktop = tutorOverlayQuery.matches;
  if (desktop) { $("tutor-launch-button").click(); await sleep(300); }

  if (desktop) {
    // Resize and collapse the main sidebar while the docked Tutor is open (also while expanded).
    const handle = $("sidebar-resizer"), left = rect($("app-sidebar")).left;
    const pointer = (target, type, x) => target.dispatchEvent(new PointerEvent(type, {bubbles: true, pointerId: 3, button: 0, isPrimary: true, clientX: x}));
    pointer(handle, "pointerdown", left + rect($("app-sidebar")).width); pointer(window, "pointermove", left + 420); pointer(window, "pointerup", left + 420); await sleep(150);
    out.sidebarWide = {sidebar: Math.round(rect($("app-sidebar")).width), ...docked()};
    $("tutor-expand-button").click(); await sleep(150);
    out.sidebarWideTutorExpanded = {...docked(), wideRail: $("persistent-tutor").classList.contains("tutor-wide")};
    $("tutor-expand-button").click(); await sleep(150);
    $("sidebar-collapse-button").click(); await sleep(150);
    out.sidebarCollapsed = {sidebar: Math.round(rect($("app-sidebar")).width), ...docked()};
    $("sidebar-collapse-button").click(); await sleep(150);
    handle.dispatchEvent(new MouseEvent("dblclick", {bubbles: true})); await sleep(150);
  }

  // A slow earlier open must not replace the chat picked last.
  window.__delays["/api/conversations/c-exam"] = 500;
  openConversation("c-exam");
  await sleep(30);
  openConversation("c-timers");
  await sleep(900);
  window.__delays = {};
  out.race = {active: activeTitle(), activeId: activeConversation?.id, messages: messageTexts(), header: $("chat-conversation-title").textContent};

  // New chat, switch away and back: it is still listed and still empty.
  $("new-conversation-button").click(); await sleep(300);
  const newId = activeConversation.id;
  await openConversation("c-timers"); await sleep(100);
  await openConversation(newId); await sleep(100);
  out.newChatRoundTrip = {activeId: activeConversation.id === newId, listed: document.querySelectorAll("#conversation-list .conversation-item").length,
    messages: messageTexts().length, hint: Boolean(document.querySelector(".conversation-empty-hint"))};

  // A reply that arrives after switching chats stays out of the open chat.
  await openConversation("c-timers"); await sleep(100);
  let release; window.__hold = new Promise((resolve) => { release = resolve; });
  const pending = sendTutorMessage("Late question about timers");
  await sleep(150);
  await openConversation(newId); await sleep(100);   // switched while the answer is pending
  window.__hold = null; release(); await pending; await sleep(300);
  out.lateReply = {activeId: activeConversation.id === newId, messages: messageTexts(), formEnabled: !$("chat-input").disabled};
  await openConversation("c-timers"); await sleep(150);
  out.lateReplyInItsChat = messageTexts().slice(-2);

  if (!desktop) {
    // Re-rendering the chat list (opening a session, sending) never scrolls the page.
    document.scrollingElement.scrollTop = 0; await sleep(50);
    renderConversationList(); await ensureStudySessionConversation(); await sleep(100);
    out.pageScroll = document.scrollingElement.scrollTop;
    // Long Markdown answer on a phone.
    window.__hold = null;
    await sendTutorMessage("Long " + "answer ".repeat(5) + "https://example.com/" + "y".repeat(200));
    await sleep(200);
    out.phoneLong = {overflow: overflow(), listOverflow: $("message-list").scrollWidth > $("message-list").clientWidth + 1};
  }
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_page(width, height):
    work = Path(tempfile.mkdtemp(prefix=f"tutor_integration_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(INTEGRATION_MOCK, encoding="utf-8")
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


class IntegrationAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_reload_reopens_the_documents_last_chat(self):
        self.assertEqual(self.out["beforeSession"]["activeId"], "c-other")
        restored = self.out["restored"]
        self.assertEqual((restored["active"], restored["activeId"]), ("Timers", "c-timers"))
        self.assertEqual(restored["messages"], ["Explain prescalers", "A prescaler divides the clock."])

    def test_slow_earlier_open_cannot_replace_the_latest_choice(self):
        race = self.out["race"]
        self.assertEqual((race["activeId"], race["active"], race["header"]), ("c-timers", "Timers", "Timers"))
        self.assertEqual(race["messages"], ["Explain prescalers", "A prescaler divides the clock."])

    def test_new_chat_survives_switching_away_and_back(self):
        self.assertEqual(self.out["newChatRoundTrip"], {"activeId": True, "listed": 3, "messages": 1, "hint": True})

    def test_late_reply_never_lands_in_the_open_chat(self):
        late = self.out["lateReply"]
        self.assertTrue(late["activeId"] and late["formEnabled"])
        self.assertFalse(any("Late question" in text for text in late["messages"]))
        self.assertEqual(self.out["lateReplyInItsChat"], ["Late question about timers", "Answer about: Late question about timers"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorIntegrationDesktopTests(IntegrationAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(1440, 900)

    def test_sidebar_resize_and_collapse_with_the_tutor_open(self):
        for key in ("sidebarWide", "sidebarWideTutorExpanded", "sidebarCollapsed"):
            state = self.out[key]
            self.assertFalse(state["overflow"] or state["overlap"], key)
            self.assertTrue(state["tutorInside"], key)
            self.assertGreaterEqual(state["content"], 420, key)
            self.assertGreaterEqual(state["tutor"], 320, key)
        self.assertEqual(self.out["sidebarWide"]["sidebar"], 420)
        self.assertEqual(self.out["sidebarCollapsed"]["sidebar"], 68)
        # The chat-list rail follows the panel's real width (>= 620px), not just the Expand button.
        expanded = self.out["sidebarWideTutorExpanded"]
        self.assertEqual(expanded["wideRail"], expanded["tutor"] >= 620)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorIntegrationPhoneTests(IntegrationAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(390, 844)

    def test_chat_list_rendering_does_not_scroll_the_page(self):
        self.assertEqual(self.out["pageScroll"], 0)

    def test_long_answer_fits_on_a_phone(self):
        self.assertEqual(self.out["phoneLong"], {"overflow": False, "listOverflow": False})


if __name__ == "__main__":
    unittest.main()
