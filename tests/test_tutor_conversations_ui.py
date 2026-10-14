"""Real-browser tests for AI Tutor multi-conversation management (headless Chrome, stateful mock of
the existing /api/conversations endpoints). Covers: the open document's chats listed (others hidden)
with the active one highlighted, switching without mixing messages, "New chat" (POST with
new_conversation=true, empty chats reused), the readable title from the first question, inline
rename (Enter/Escape/empty), delete with inline confirmation (active and inactive chats), the list
locked while an answer is pending, the collapsible list, the wide-panel rail and the phone layout.
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

from tests.test_quiz_player_ui import FRONTEND, MOCK, find_chrome
from tests.test_tutor_resize_ui import PHONE_HOST

CONVERSATION_MOCK = MOCK + r"""
window.__clock = Date.parse("2026-10-20T00:00:00Z");
const tick = () => new Date(window.__clock += 60000).toISOString();
const msgs = (question, answer, citations = []) => [{role: "user", content: question},
  {role: "assistant", content: answer, grounding_status: "supported", citations}];
window.__convs = [
  {id: "c-exam", title: "Exam prep", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], updated_at: "2026-10-13T09:00:00Z",
   messages: msgs("What is an ISR?", "An **ISR** handles interrupts.", [{title: "lecture.pdf", document_id: "lecture.pdf", page: "3", chunk: ""}])},
  {id: "c-timers", title: "Timers", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], updated_at: "2026-10-12T09:00:00Z",
   messages: msgs("Explain prescalers", "A prescaler divides the clock.")},
  {id: "c-other", title: "Other doc chat", document_id: "other.pdf", document_ids: ["other.pdf"], updated_at: "2026-10-14T09:00:00Z",
   messages: msgs("Other question", "Other answer")},
];
window.__requests = [];
window.__hold = null;
window.__newId = 0;
const summary = (c) => { const {messages, ...rest} = c; return rest; };
const defaultTitle = (message) => {
  const text = message.split(/\s+/).filter(Boolean).join(" ");
  if (text.length <= 60) return text || "New conversation";
  const head = text.slice(0, 60);
  return (head.includes(" ") ? head.slice(0, head.lastIndexOf(" ")) : head).replace(/[ ,.;:-]+$/, "") + "…";
};
const quizFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  const body = init.body ? JSON.parse(init.body) : null;
  if (!p.startsWith("/api/conversations")) {
    if (p === "/api/sources") return json([{sourceId: 1, title: "lecture.pdf", chunks: 3}]);
    return quizFetch(input, init);
  }
  window.__requests.push({method, path: p, body});
  const find = (id) => window.__convs.find((c) => c.id === id);
  if (p === "/api/conversations" && method === "GET") {
    return json([...window.__convs].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).map(summary));
  }
  if (p === "/api/conversations" && method === "POST") {
    const doc = body.document_ids[0];
    const existing = window.__convs.filter((c) => c.document_id === doc).sort((a, b) => a.id.localeCompare(b.id))[0];
    if (existing && !body.new_conversation) return json(existing);
    const created = {id: "c-new-" + (++window.__newId), title: body.title, document_id: doc, document_ids: [doc], updated_at: tick(), messages: []};
    window.__convs.push(created);
    return json(created);
  }
  const match = p.match(/^\/api\/conversations\/([^/]+)(\/messages|\/sources)?$/);
  const conv = match && find(match[1]);
  if (!conv) return json({detail: "Conversation not found."}, 404);
  if (match[2] === "/messages") {
    if (window.__hold) await window.__hold;
    const answer = "Answer about: " + body.message;
    if (conv.title === "New conversation" && !conv.messages.length) conv.title = defaultTitle(body.message);
    const user = {role: "user", content: body.message, conversation_id: conv.id};
    const assistant = {role: "assistant", content: answer, grounding_status: "supported", conversation_id: conv.id,
      citations: [{title: "lecture.pdf", document_id: "lecture.pdf", page: "7", chunk: ""}]};
    conv.messages.push(user, assistant);
    conv.updated_at = tick();
    return json({answer, grounding_status: "supported", citations: assistant.citations, model: "qwen-2.5-7b", user_message: user, assistant_message: assistant});
  }
  if (match[2] === "/sources") return json(conv);
  if (method === "GET") return json(conv);
  if (method === "PATCH") {
    if (!body.title || body.title.length > 120) return json({detail: "invalid title"}, 422);
    conv.title = body.title; conv.updated_at = tick();
    return json(summary(conv));
  }
  if (method === "DELETE") {
    window.__convs = window.__convs.filter((c) => c.id !== conv.id);
    return json({deleted: conv.id});
  }
  return json({detail: "unsupported"}, 405);
};
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
const shown = (el) => Boolean(el) && getComputedStyle(el).display !== "none" && getComputedStyle(el).visibility !== "hidden" && rect(el).width > 0;
const rows = () => [...document.querySelectorAll("#conversation-list .conversation-item")];
const rowFor = (title) => rows().find((row) => row.querySelector(".conversation-open-button strong")?.textContent === title);
const listState = () => ({
  titles: rows().map((row) => row.querySelector("strong")?.textContent ?? null),
  active: rows().filter((row) => row.classList.contains("active")).map((row) => row.querySelector("strong")?.textContent),
  ariaCurrent: rows().filter((row) => row.querySelector('[aria-current="true"]')).length,
  count: $("tutor-conversations-count").textContent, header: $("chat-conversation-title").textContent,
  activeId: activeConversation?.id || null,
});
const messageTexts = () => [...$("message-list").querySelectorAll(".message-body")].map((el) => el.textContent);
const requests = (method, pathPart = "") => window.__requests.filter((r) => r.method === method && r.path.includes(pathPart));
const toastText = () => $("toast").textContent;
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  await openStudySession("lecture.pdf", "material");
  await sleep(400);
  if (tutorOverlayQuery.matches) { $("tutor-launch-button").click(); await sleep(300); }
  out.viewport = innerWidth;
  const tutor = $("persistent-tutor");
  out.initial = {...listState(), messages: messageTexts(), newChatShown: shown($("new-conversation-button")), listShown: shown($("conversation-list")),
    listInsideTutor: tutor.contains($("conversation-list")), stacked: rect($("conversation-list")).bottom <= rect($("message-list")).top + 1,
    pageOverflow: document.documentElement.scrollWidth > innerWidth + 1};

  // Switching chats shows only that chat's messages.
  rowFor("Timers").querySelector(".conversation-open-button").click(); await sleep(250);
  out.switched = {...listState(), messages: messageTexts()};
  rowFor("Exam prep").querySelector(".conversation-open-button").click(); await sleep(250);
  out.switchedBack = {...listState(), messages: messageTexts()};

  // New chat: POST with new_conversation; an empty chat is reused on a second click.
  $("new-conversation-button").click(); await sleep(300);
  out.newChat = {...listState(), messages: messageTexts(), posts: requests("POST").filter((r) => r.path === "/api/conversations").map((r) => r.body),
    focus: document.activeElement?.id};
  $("new-conversation-button").click(); await sleep(300);
  out.newChatAgain = {posts: requests("POST").filter((r) => r.path === "/api/conversations").length, count: listState().count, toast: toastText()};

  // The first question titles the chat; the answer stays in that chat. The list is locked meanwhile.
  let release; window.__hold = new Promise((resolve) => { release = resolve; });
  const sending = sendTutorMessage("  How do   DMA transfers work when two peripherals request the bus at the same moment?  ");
  await sleep(200);
  out.locked = {newChatDisabled: $("new-conversation-button").disabled,
    otherOpenDisabled: rows().filter((row) => !row.classList.contains("active")).every((row) => row.querySelector(".conversation-open-button").disabled),
    actionsDisabled: rows().every((row) => [...row.querySelectorAll(".conversation-rename-button, .conversation-delete-button")].every((b) => b.disabled))};
  window.__hold = null; release(); await sending; await sleep(300);
  out.afterFirstQuestion = {...listState(), messages: messageTexts(), unlocked: !$("new-conversation-button").disabled};
  rowFor("Exam prep").querySelector(".conversation-open-button").click(); await sleep(250);
  out.examAfterDma = {messages: messageTexts()};

  // Rename: Escape cancels (and keeps the Tutor open), empty is refused, Enter saves.
  rowFor("Timers").querySelector(".conversation-rename-button").click(); await sleep(100);
  const input = () => document.querySelector(".conversation-rename-input");
  out.renameOpen = {focus: document.activeElement === input(), value: input()?.value};
  input().dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true})); await sleep(100);
  out.renameEscape = {editing: Boolean(input()), patches: requests("PATCH").length, tutorOpen: tutorOverlayQuery.matches ? tutorOverlayOpen() : true,
    focus: document.activeElement?.className};
  rowFor("Timers").querySelector(".conversation-rename-button").click(); await sleep(100);
  input().value = "   "; input().form.requestSubmit(); await sleep(150);
  out.renameEmpty = {editing: Boolean(input()), patches: requests("PATCH").length, toast: toastText()};
  input().value = "  Timer interrupts  "; input().form.requestSubmit(); await sleep(300);
  out.renamed = {...listState(), patch: requests("PATCH").map((r) => [r.path, r.body]), editing: Boolean(input())};
  rowFor("Timer interrupts").querySelector(".conversation-open-button").click(); await sleep(250);
  out.renamedHeader = listState().header;

  // Delete an inactive chat: confirmation first, Cancel keeps it, Delete removes it.
  const examRow = () => rowFor("Exam prep");
  examRow().querySelector(".conversation-delete-button").click(); await sleep(100);
  const confirmRow = () => document.querySelector(".conversation-item.is-confirming");
  out.confirmShown = {text: confirmRow()?.querySelector(".conversation-confirm-text")?.textContent, deletes: requests("DELETE").length,
    focus: document.activeElement?.className};
  confirmRow().querySelector(".conversation-confirm-cancel").click(); await sleep(100);
  out.confirmCancelled = {confirming: Boolean(confirmRow()), titles: listState().titles, deletes: requests("DELETE").length};
  examRow().querySelector(".conversation-delete-button").click(); await sleep(100);
  confirmRow().querySelector(".conversation-confirm-delete").click(); await sleep(300);
  out.deletedInactive = {...listState(), deletes: requests("DELETE").map((r) => r.path), messages: messageTexts()};

  // Delete the active chat: the next chat of this document opens.
  rows().find((row) => row.classList.contains("active")).querySelector(".conversation-delete-button").click(); await sleep(100);
  confirmRow().querySelector(".conversation-confirm-delete").click(); await sleep(400);
  out.deletedActive = {...listState(), messages: messageTexts()};

  // Collapsible list (narrow panels) and the remembered preference.
  $("tutor-conversations-toggle").click(); await sleep(100);
  out.collapsed = {listShown: shown($("conversation-list")), expanded: $("tutor-conversations-toggle").getAttribute("aria-expanded"),
    stored: localStorage.getItem("aiTutorChatListCollapsed"), wide: tutor.classList.contains("tutor-wide")};
  $("tutor-conversations-toggle").click(); await sleep(100);
  out.reopenedList = shown($("conversation-list"));

  // Wide panel: chats become a rail beside the messages.
  if (tutorOverlayQuery.matches) {
    $("tutor-expand-button").click(); await sleep(300);
    const list = rect($("conversation-list")), messages = rect($("message-list"));
    out.wide = {wide: tutor.classList.contains("tutor-wide"), railLeft: list.right <= messages.left + 1, sideBySide: list.top < messages.bottom && messages.top < list.bottom,
      formRight: rect($("chat-form")).left >= list.right - 1, pageOverflow: document.documentElement.scrollWidth > innerWidth + 1};
    $("tutor-expand-button").click(); await sleep(300);
    $("tutor-close-button").click(); await sleep(300);
    out.closedWhileWide = {tutorShown: shown(tutor)};
  } else {
    out.phone = {newChatShown: shown($("new-conversation-button")), listShown: shown($("conversation-list")),
      actionsShown: rows().every((row) => [...row.querySelectorAll(".conversation-rename-button, .conversation-delete-button")].every((b) => getComputedStyle(b).opacity === "1")),
      listInside: rect($("conversation-list")).right <= rect(tutor).right + 1, listMaxHeight: getComputedStyle($("conversation-list")).maxHeight,
      pageOverflow: document.documentElement.scrollWidth > innerWidth + 1};
  }
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_page(width, height):
    work = Path(tempfile.mkdtemp(prefix=f"tutor_chats_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(CONVERSATION_MOCK, encoding="utf-8")
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


DMA_TITLE = "How do DMA transfers work when two peripherals request the…"


class ConversationFlowAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_lists_only_this_documents_chats_with_the_active_one_highlighted(self):
        initial = self.out["initial"]
        self.assertTrue(initial["newChatShown"] and initial["listShown"] and initial["listInsideTutor"])
        self.assertEqual(initial["titles"], ["Exam prep", "Timers"])   # other.pdf's chat is not listed
        self.assertEqual((initial["active"], initial["ariaCurrent"], initial["count"]), (["Exam prep"], 1, "2"))
        self.assertEqual(initial["header"], "Exam prep")
        self.assertEqual(initial["messages"], ["What is an ISR?", "An ISR handles interrupts."])
        self.assertFalse(initial["pageOverflow"])

    def test_switching_never_mixes_messages(self):
        switched = self.out["switched"]
        self.assertEqual((switched["active"], switched["header"]), (["Timers"], "Timers"))
        self.assertEqual(switched["messages"], ["Explain prescalers", "A prescaler divides the clock."])
        self.assertEqual(self.out["switchedBack"]["messages"], ["What is an ISR?", "An ISR handles interrupts."])

    def test_new_chat_creates_a_separate_conversation_and_reuses_an_empty_one(self):
        new_chat = self.out["newChat"]
        self.assertEqual(new_chat["posts"][-1], {"title": "New conversation", "document_ids": ["lecture.pdf"], "new_conversation": True})
        self.assertEqual((new_chat["count"], new_chat["active"], new_chat["header"]), ("3", ["New conversation"], "New conversation"))
        self.assertEqual(len(new_chat["messages"]), 1)   # only the empty-chat hint
        self.assertNotIn("What is an ISR?", new_chat["messages"])
        self.assertEqual(new_chat["focus"], "chat-input")
        again = self.out["newChatAgain"]
        self.assertEqual((again["posts"], again["count"]), (len(new_chat["posts"]), "3"))
        self.assertIn("still empty", again["toast"])

    def test_first_question_names_the_chat_and_its_answer_stays_there(self):
        locked = self.out["locked"]
        self.assertTrue(locked["newChatDisabled"] and locked["otherOpenDisabled"] and locked["actionsDisabled"])
        after = self.out["afterFirstQuestion"]
        self.assertTrue(after["unlocked"])
        self.assertEqual((after["active"], after["header"]), ([DMA_TITLE], DMA_TITLE))
        self.assertEqual(after["titles"][0], DMA_TITLE)   # most recently updated first
        self.assertEqual(len(after["messages"]), 2)
        self.assertTrue(after["messages"][1].startswith("Answer about:"))
        self.assertFalse(any("DMA" in text for text in self.out["examAfterDma"]["messages"]))

    def test_rename_inline_with_escape_and_empty_name_handling(self):
        self.assertEqual(self.out["renameOpen"], {"focus": True, "value": "Timers"})
        escape = self.out["renameEscape"]
        self.assertEqual((escape["editing"], escape["patches"], escape["tutorOpen"]), (False, 0, True))
        self.assertIn("conversation-rename-button", escape["focus"])
        empty = self.out["renameEmpty"]
        self.assertEqual((empty["editing"], empty["patches"]), (True, 0))
        self.assertIn("Enter a name", empty["toast"])
        renamed = self.out["renamed"]
        self.assertFalse(renamed["editing"])
        self.assertEqual(renamed["patch"], [["/api/conversations/c-timers", {"title": "Timer interrupts"}]])
        self.assertIn("Timer interrupts", renamed["titles"])
        self.assertEqual(self.out["renamedHeader"], "Timer interrupts")

    def test_delete_asks_for_confirmation(self):
        shown = self.out["confirmShown"]
        self.assertIn("Delete “Exam prep”?", shown["text"])
        self.assertEqual(shown["deletes"], 0)
        self.assertIn("conversation-confirm-cancel", shown["focus"])
        cancelled = self.out["confirmCancelled"]
        self.assertFalse(cancelled["confirming"])
        self.assertIn("Exam prep", cancelled["titles"])
        self.assertEqual(cancelled["deletes"], 0)
        inactive = self.out["deletedInactive"]
        self.assertEqual(inactive["deletes"], ["/api/conversations/c-exam"])
        self.assertNotIn("Exam prep", inactive["titles"])
        self.assertEqual(inactive["active"], ["Timer interrupts"])   # the open chat is untouched
        self.assertEqual(inactive["messages"], ["Explain prescalers", "A prescaler divides the clock."])

    def test_deleting_the_active_chat_opens_the_next_one(self):
        after = self.out["deletedActive"]
        self.assertEqual(after["titles"], [DMA_TITLE])
        self.assertEqual((after["active"], after["header"], after["count"]), ([DMA_TITLE], DMA_TITLE, "1"))
        self.assertTrue(after["messages"][1].startswith("Answer about:"))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorConversationsDesktopTests(ConversationFlowAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(1440, 900)

    def test_narrow_panel_stacks_a_collapsible_list(self):
        self.assertTrue(self.out["initial"]["stacked"])
        collapsed = self.out["collapsed"]
        self.assertEqual((collapsed["listShown"], collapsed["expanded"], collapsed["stored"], collapsed["wide"]), (False, "false", "1", False))
        self.assertTrue(self.out["reopenedList"])

    def test_wide_panel_shows_chats_as_a_rail_and_still_closes(self):
        wide = self.out["wide"]
        self.assertTrue(wide["wide"] and wide["railLeft"] and wide["sideBySide"] and wide["formRight"])
        self.assertFalse(wide["pageOverflow"])
        self.assertFalse(self.out["closedWhileWide"]["tutorShown"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorConversationsPhoneTests(ConversationFlowAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(390, 844)

    def test_phone_layout(self):
        self.assertLessEqual(self.out["viewport"], 400)
        phone = self.out["phone"]
        self.assertTrue(phone["newChatShown"] and phone["listShown"] and phone["listInside"])
        self.assertEqual(phone["listMaxHeight"], "132px")
        self.assertFalse(phone["pageOverflow"])


if __name__ == "__main__":
    unittest.main()
