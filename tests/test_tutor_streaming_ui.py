"""AI Tutor answer streaming in the frontend (CHAT-PERF-2).

- js/chat_stream.js under Node: SSE frames split anywhere (also inside a UTF-8 character), error and
  premature-end handling, and render batching.
- The real app in headless Chrome with a streaming fetch mock: loading row until the first text, the
  answer growing in place with batched Markdown renders, Stop, model errors and a dropped connection.
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

STREAM_JS = FRONTEND / "js" / "chat_stream.js"

NODE_SCRIPT = r"""
const { parseTutorStreamFrames, readTutorAnswerStream, createTutorStreamRenderer, TutorStreamError } = require(process.argv[2]);
const encoder = new TextEncoder();
const frame = (event) => `data: ${JSON.stringify(event)}\n\n`;
const responseOf = (byteChunks) => ({ body: new ReadableStream({ start(c) { byteChunks.forEach((b) => c.enqueue(b)); c.close(); } }) });
const splitBytes = (text, size) => { const bytes = encoder.encode(text); const out = []; for (let i = 0; i < bytes.length; i += size) out.push(bytes.slice(i, i + size)); return out; };
(async () => {
  const out = {};
  const full = frame({type: "start"}) + frame({type: "delta", text: "Phân trang "}) + ": keep-alive\n\n"
    + frame({type: "delta", text: "ánh xạ **trang**"}) + frame({type: "done", answer: "Phân trang ánh xạ **trang**", citations: [1]});
  for (const size of [1, 3, 7, 1000]) {
    const pieces = [];
    const done = await readTutorAnswerStream(responseOf(splitBytes(full, size)), (t) => pieces.push(t));
    out["split" + size] = {text: pieces.join(""), deltas: pieces.length, answer: done.answer, citations: done.citations};
  }
  out.crlf = parseTutorStreamFrames("data: {\"type\":\"delta\",\"text\":\"a\"}\r\n\r\ndata: {\"ty").events;
  out.rest = parseTutorStreamFrames("data: {\"type\":\"delta\",\"text\":\"a\"}\r\n\r\ndata: {\"ty").rest;
  const failure = async (text) => {
    const pieces = [];
    try { await readTutorAnswerStream(responseOf([encoder.encode(text)]), (t) => pieces.push(t)); return {resolved: true}; }
    catch (error) { return {isStreamError: error instanceof TutorStreamError, message: error.message, pieces}; }
  };
  out.errorEvent = await failure(frame({type: "delta", text: "par"}) + frame({type: "error", message: "model crashed"}));
  out.cutOff = await failure(frame({type: "delta", text: "par"}));
  out.cutMidFrame = await failure(frame({type: "delta", text: "par"}) + 'data: {"type":"do');
  // Renderer: many requests -> one render per tick; flush renders now; cancel drops a pending one.
  const timers = []; let renders = 0;
  const renderer = createTutorStreamRenderer(() => renders++, 80, (fn) => { timers.push(fn); return timers.length; }, () => {});
  for (let i = 0; i < 50; i++) renderer.request();
  out.timersAfterBurst = timers.length; timers.shift()();
  out.rendersAfterTick = renders;
  renderer.request(); renderer.flush(); out.rendersAfterFlush = renders;
  renderer.request(); renderer.cancel(); timers.forEach((fn) => fn()); out.rendersAfterCancel = renders;
  console.log(JSON.stringify(out));
})().catch((error) => { console.log(JSON.stringify({fatal: String(error.stack || error)})); });
"""


class ChatStreamModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is not installed")
        work = Path(tempfile.mkdtemp(prefix="chat_stream_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        (work / "run.js").write_text(NODE_SCRIPT, encoding="utf-8")
        result = subprocess.run(["node", str(work / "run.js"), str(STREAM_JS)], capture_output=True, text=True,
                                encoding="utf-8", timeout=60)
        cls.out = json.loads(result.stdout.strip().splitlines()[-1])

    def test_frames_split_anywhere_reassemble_in_order(self):
        self.assertNotIn("fatal", self.out)
        for size in (1, 3, 7, 1000):   # 1 and 3 bytes also split the Vietnamese characters
            with self.subTest(size=size):
                self.assertEqual(self.out[f"split{size}"], {"text": "Phân trang ánh xạ **trang**", "deltas": 2,
                                                            "answer": "Phân trang ánh xạ **trang**", "citations": [1]})

    def test_crlf_frames_and_unfinished_rest(self):
        self.assertEqual(self.out["crlf"], [{"type": "delta", "text": "a"}])
        self.assertEqual(self.out["rest"], 'data: {"ty')

    def test_error_event_and_a_stream_that_ends_early_reject(self):
        self.assertEqual(self.out["errorEvent"], {"isStreamError": True, "message": "model crashed", "pieces": ["par"]})
        for case in ("cutOff", "cutMidFrame"):
            with self.subTest(case=case):
                self.assertTrue(self.out[case]["isStreamError"])
                self.assertIn("connection closed", self.out[case]["message"])

    def test_renders_are_batched(self):
        self.assertEqual(self.out["timersAfterBurst"], 1)
        self.assertEqual(self.out["rendersAfterTick"], 1)
        self.assertEqual(self.out["rendersAfterFlush"], 2)
        self.assertEqual(self.out["rendersAfterCancel"], 2)


STREAM_MOCK = MOCK + r"""
window.__streams = [];
window.__chatRequests = [];
const quizPlayerFetchForStream = window.fetch;
const streamSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const streamFrame = (event) => `data: ${JSON.stringify(event)}\n\n`;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (method === "POST" && /^\/api\/conversations\/[^/]+\/messages$/.test(p)) {
    const plan = window.__streams.shift();
    window.__chatRequests.push({accept: init.headers && init.headers.Accept, message: JSON.parse(init.body).message});
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      async start(controller) {
        init.signal?.addEventListener("abort", () => controller.error(new DOMException("The user aborted a request.", "AbortError")));
        for (const [delay, text] of plan.frames) {
          await streamSleep(delay);
          if (init.signal?.aborted) return;
          controller.enqueue(encoder.encode(text));
        }
        if (!plan.hang && !init.signal?.aborted) controller.close();
      },
    });
    return new Response(body, {status: 200, headers: {"Content-Type": "text/event-stream; charset=utf-8"}});
  }
  if (p === "/api/sources") return new Response(JSON.stringify([{sourceId: 1, title: "lecture.pdf", chunks: 12}]),
    {status: 200, headers: {"Content-Type": "application/json"}});
  return quizPlayerFetchForStream(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const frame = (event) => `data: ${JSON.stringify(event)}\n\n`;
const citations = [{sourceId: 1, title: "lecture.pdf", page: "3", content: "a"}];
const done = (answer) => frame({type: "done", answer, grounding_status: "supported", citations, model: "qwen",
  user_message: {role: "user", content: "q"}, assistant_message: {role: "assistant", content: answer, grounding_status: "supported", citations}});
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "material");
  await sleep(400);
  if (tutorOverlayQuery.matches) { $("tutor-launch-button").click(); await sleep(200); }
  const list = $("message-list"), button = chatForm.querySelector("button");
  let renders = 0;
  const realAppendMarkdown = window.appendMarkdown;
  window.appendMarkdown = (container, text) => { renders++; return realAppendMarkdown(container, text); };
  const snapshot = () => ({loading: list.querySelectorAll(".loading-message").length, streaming: list.querySelectorAll(".is-streaming").length,
    lastText: [...list.querySelectorAll(".agent-row")].pop()?.querySelector(".message-body")?.textContent || "",
    button: button.textContent, inputDisabled: chatInput.disabled, sending: chatForm.classList.contains("is-sending"),
    loadingText: list.querySelector(".loading-message .message-body")?.textContent || "",
    loadingRole: list.querySelector(".loading-message")?.getAttribute("role") || "", buttonLabel: button.getAttribute("aria-label") || ""});
  const rowsWithText = (text) => [...list.querySelectorAll(".agent-row .message-body")].filter((b) => b.textContent === text).length;

  // 1. Success: 30 pieces, 10 ms apart.
  const words = Array.from({length: 30}, (_, i) => (i === 0 ? "**Paging** " : `w${i} `));
  const answer = words.join("").trim();
  window.__streams.push({frames: [[200, frame({type: "start"})], ...words.map((w) => [10, frame({type: "delta", text: w})]), [10, done(answer)]]});
  const messagesBefore = (activeConversation.messages || []).length;
  const sending = sendTutorMessage("What is paging?");
  await sleep(100); out.beforeFirstText = snapshot();
  await sleep(200); out.midStream = snapshot();
  await sending; await sleep(50);
  const last = [...list.querySelectorAll(".agent-row")].pop();
  out.afterDone = {...snapshot(), strong: last.querySelector(".message-body strong")?.textContent, text: last.querySelector(".message-body").textContent,
    badge: last.querySelector(".grounding-badge")?.textContent, pages: last.querySelector(".citation-pages")?.textContent,
    addedMessages: activeConversation.messages.length - messagesBefore, answerRows: rowsWithText(last.querySelector(".message-body").textContent), renders, deltas: words.length,
    accept: window.__chatRequests[0].accept};

  // 2. Stop: the stream never ends; the Stop button aborts it and nothing is added to the history.
  window.__streams.push({hang: true, frames: [[50, frame({type: "start"})], [20, frame({type: "delta", text: "Partial "})], [20, frame({type: "delta", text: "answer"})]]});
  const stopMessages = activeConversation.messages.length;
  const stopping = sendTutorMessage("Explain more please");
  await sleep(300);
  chatInput.value = "a second question";       // a submit while sending only stops, never sends again
  button.click();
  await stopping; await sleep(50);
  const stopped = [...list.querySelectorAll(".agent-row")].pop();
  out.stopped = {...snapshot(), note: stopped.querySelector(".message-stopped-note")?.textContent, text: stopped.querySelector(".message-body").textContent,
    addedMessages: activeConversation.messages.length - stopMessages, requests: window.__chatRequests.length, errorBubble: list.textContent.includes("I could not reach")};

  // 3. A model error after some text: the partial answer is replaced by the error message.
  window.__streams.push({frames: [[50, frame({type: "start"})], [20, frame({type: "delta", text: "Half an ans"})], [20, frame({type: "error", message: "Could not answer this conversation. Original error: model crashed"})]]});
  const errorMessages = activeConversation.messages.length;
  await sendTutorMessage("Will fail"); await sleep(50);
  out.modelError = {...snapshot(), inputValue: chatInput.value, partialShown: list.textContent.includes("Half an ans"), errorBubble: list.textContent.includes("model crashed"),
    addedMessages: activeConversation.messages.length - errorMessages};

  // 4. The connection drops before "done".
  window.__streams.push({frames: [[50, frame({type: "start"})], [20, frame({type: "delta", text: "Cut"})]]});
  await sendTutorMessage("Will drop"); await sleep(50);
  out.dropped = {...snapshot(), message: list.textContent.includes("connection closed before the answer finished")};

  // 5. Loading phases come only from real events: sent -> "start" -> first text replaces the row.
  window.__streams.push({frames: [[100, frame({type: "start"})], [300, frame({type: "delta", text: "Late"})], [10, done("Late")]]});
  const phased = sendTutorMessage("Phases?");
  await sleep(40); out.phaseSent = snapshot();
  await sleep(150); out.phaseStarted = snapshot();
  await phased; await sleep(50);
  out.phaseDone = snapshot();

  // 6. A typed question that fails goes back into the box; the error bubble is an alert.
  window.__streams.push({frames: [[50, frame({type: "start"})], [20, frame({type: "error", message: "model crashed"})]]});
  chatInput.value = "My typed question";
  chatForm.requestSubmit();
  await sleep(400);
  const errorMessage = [...list.querySelectorAll(".agent-row .message")].pop();
  out.typedError = {...snapshot(), input: chatInput.value, isError: errorMessage.classList.contains("error-message"),
    role: errorMessage.getAttribute("role"), text: errorMessage.textContent};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorStreamingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="tutor_stream_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(STREAM_MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000", "--dump-dom",
             "--window-size=1400,900", f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_loading_row_until_the_first_text_then_the_answer_grows(self):
        before, mid = self.out["beforeFirstText"], self.out["midStream"]
        self.assertEqual((before["loading"], before["streaming"], before["button"], before["inputDisabled"]), (1, 0, "Stop", True))
        self.assertEqual((mid["loading"], mid["streaming"]), (0, 1))
        self.assertTrue(mid["lastText"].startswith("Paging w1"), mid["lastText"])
        self.assertLess(len(mid["lastText"]), len(self.out["afterDone"]["text"]))

    def test_final_answer_is_markdown_with_citations_and_saved_once(self):
        done = self.out["afterDone"]
        self.assertEqual((done["streaming"], done["loading"], done["button"], done["inputDisabled"], done["sending"]), (0, 0, "Send", False, False))
        self.assertEqual(done["strong"], "Paging")
        self.assertEqual((done["badge"], done["pages"]), ("Grounded", "lecture.pdf · p. 3"))
        self.assertEqual((done["addedMessages"], done["answerRows"]), (2, 1))   # no duplicate assistant message
        self.assertIn("text/event-stream", done["accept"])

    def test_markdown_renders_are_batched_not_per_token(self):
        done = self.out["afterDone"]
        self.assertLess(done["renders"], done["deltas"] / 2, done)

    def test_stop_keeps_the_partial_text_unsaved_and_never_sends_twice(self):
        stopped = self.out["stopped"]
        self.assertEqual(stopped["note"], "Stopped. This answer was not saved.")
        self.assertIn("Partial answer", stopped["text"])
        self.assertEqual((stopped["addedMessages"], stopped["requests"], stopped["errorBubble"]), (0, 2, False))
        self.assertEqual((stopped["button"], stopped["sending"], stopped["streaming"]), ("Send", False, 0))

    def test_loading_row_reports_real_phases_and_stop_is_labelled(self):
        sent, started, done = self.out["phaseSent"], self.out["phaseStarted"], self.out["phaseDone"]
        self.assertEqual(sent["loadingText"], "Sending your question to the AI Tutor")
        self.assertEqual(sent["loadingRole"], "status")
        self.assertEqual((sent["button"], sent["buttonLabel"]), ("Stop", "Stop generating"))
        self.assertEqual(started["loadingText"], "Reading your study materials and writing the answer")
        self.assertEqual((done["loading"], done["button"], done["buttonLabel"]), (0, "Send", ""))

    def test_a_failed_typed_question_is_restored_for_retry(self):
        error = self.out["typedError"]
        self.assertEqual(error["input"], "My typed question")
        self.assertTrue(error["isError"])
        self.assertEqual(error["role"], "alert")
        self.assertIn("interrupted and was not saved", error["text"])
        self.assertIn("model crashed", error["text"])
        self.assertIn("Press Send to try again", error["text"])
        self.assertEqual((error["button"], error["sending"]), ("Send", False))

    def test_quiz_explain_failures_do_not_fill_the_input(self):
        self.assertEqual(self.out["modelError"]["inputValue"], "")

    def test_model_error_and_dropped_connection_show_an_error_and_save_nothing(self):
        error = self.out["modelError"]
        self.assertEqual((error["partialShown"], error["errorBubble"], error["addedMessages"]), (False, True, 0))
        self.assertEqual((error["button"], error["streaming"], error["loading"]), ("Send", 0, 0))
        self.assertTrue(self.out["dropped"]["message"])
        self.assertEqual(self.out["dropped"]["streaming"], 0)


if __name__ == "__main__":
    unittest.main()
