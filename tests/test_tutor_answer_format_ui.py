"""Real-browser tests for AI Tutor answer formatting (headless Chrome against the Quiz Player mock,
with the chat endpoint mocked). Answers go through the real sendTutorMessage path and are rendered
from a safe Markdown subset: headings, emphasis, lists, quotes, code, tables, links and citation
chips; raw HTML and javascript: links stay inert text; plain text still reads as paragraphs; the
grounding status and citation summary form a separate footer that opens the Sources drawer; long
answers, URLs, code and tables never widen the chat (desktop and phone). Skipped without Chrome.
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

CHAT_MOCK = MOCK + r"""
window.__answers = [];
window.__xss = 0;
const quizPlayerFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (method === "POST" && /^\/api\/conversations\/[^/]+\/messages$/.test(p)) {
    const reply = window.__answers.shift();
    const message = JSON.parse(init.body).message;
    return json({answer: reply.answer, grounding_status: reply.status, citations: reply.citations, model: "qwen-2.5-7b",
      user_message: {role: "user", content: message}, assistant_message: {role: "assistant", content: reply.answer,
      grounding_status: reply.status, citations: reply.citations}});
  }
  if (p === "/api/sources") return json([{sourceId: 1, title: "lecture.pdf", chunks: 12}]);
  return quizPlayerFetch(input, init);
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
const tags = (root, selector) => [...root.querySelectorAll(selector)];
const citations = [{sourceId: 1, title: "lecture.pdf", page: "5", content: "a"}, {sourceId: 2, title: "lecture.pdf", page: "2", content: "b"},
  {sourceId: 3, title: "lecture.pdf", page: "5", content: "c"}];
const MARKDOWN = [
  "Interrupts let hardware **pause** the CPU (source: lecture.pdf, page 5).",
  "",
  "### How priority works",
  "1. The controller compares *levels*.",
  "2. The `NVIC` picks the highest:",
  "   - nested interrupts preempt",
  "   - equal levels wait",
  "",
  "> Keep ISRs short.",
  "",
  "```c",
  "void ISR(void) { flag = 1; }",
  "```",
  "",
  "| Term | Meaning |",
  "| --- | --- |",
  "| ISR | Handler |",
  "",
  "---",
  "See [the datasheet](https://example.com/ds) or https://example.com/notes.",
].join("\n");
const XSS = [
  "<img src=x onerror=\"window.__xss=1\"> <script>window.__xss=2</script>",
  "[click me](javascript:window.__xss=3) and [data](data:text/html,<b>x</b>)",
  "<a href=\"javascript:window.__xss=4\">raw link</a> **<b>bold html</b>**",
  "```html",
  "<iframe src=\"javascript:window.__xss=5\"></iframe>",
  "```",
].join("\n");
const PLAIN = "Polling checks a flag in a loop.\nInterrupts react only when needed.\n\nThis saves power because the CPU can sleep.";
const LONG = Array.from({length: 30}, (_, i) => "Paragraph " + i + " explains timers in detail with enough words to wrap across several lines of the chat panel.").join("\n\n")
  + "\n\nReference: https://example.com/" + "x".repeat(260) + "\n\n```\n" + "const value = " + "veryLongIdentifier".repeat(20) + ";\n```\n\n"
  + "| A | B | C | D | E | F | G |\n|---|---|---|---|---|---|---|\n| " + Array(7).fill("wide cell content here").join(" | ") + " |";
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  await openStudySession("lecture.pdf", "material");
  await sleep(400);
  if (tutorOverlayQuery.matches) { $("tutor-launch-button").click(); await sleep(200); }
  const tutor = $("persistent-tutor"), list = $("message-list");
  const send = async (answer, status = "supported", cites = citations) => {
    window.__answers.push({answer, status, citations: cites});
    await sendTutorMessage("question");
    await sleep(100);
    const rows = list.querySelectorAll(".agent-row");
    return rows[rows.length - 1];
  };
  const layout = (row) => {
    const t = rect(tutor), bubble = row.querySelector(".message");
    return {pageOverflow: document.documentElement.scrollWidth > innerWidth + 1, listOverflow: list.scrollWidth > list.clientWidth + 1,
      bubbleInside: rect(bubble).right <= t.right + 1 && rect(bubble).left >= t.left - 1,
      blocksInside: tags(bubble, ".message-body > *").every((el) => rect(el).right <= rect(bubble).right + 1)};
  };

  // 1. Markdown rendering through the real chat path.
  const md = await send(MARKDOWN);
  const body = md.querySelector(".message-body");
  out.markdown = {
    blocks: [...body.children].map((el) => el.tagName.toLowerCase() + (el.className ? "." + el.className : "")),
    heading: body.querySelector("h3")?.textContent, strong: body.querySelector("p strong")?.textContent, em: body.querySelector("em")?.textContent,
    inlineCode: body.querySelector("li code")?.textContent, olItems: tags(body, "ol > li").length, nestedItems: tags(body, "ol li ul > li").map((li) => li.textContent),
    quote: body.querySelector("blockquote")?.textContent.trim(), codeBlock: body.querySelector("pre code")?.textContent, codeLanguage: body.querySelector("pre")?.dataset.language,
    tableHead: tags(body, "th").map((th) => th.textContent), tableCells: tags(body, "td").map((td) => td.textContent),
    links: tags(body, "a").map((a) => ({href: a.getAttribute("href"), text: a.textContent, target: a.target, rel: a.rel})),
    citationChips: tags(body, ".inline-citation").map((chip) => chip.textContent),
    headingSize: parseFloat(getComputedStyle(body.querySelector("h3")).fontSize), paragraphSize: parseFloat(getComputedStyle(body.querySelector("p")).fontSize),
    lineHeight: parseFloat(getComputedStyle(body.querySelector("p")).lineHeight) / parseFloat(getComputedStyle(body.querySelector("p")).fontSize),
    preOverflowX: getComputedStyle(body.querySelector("pre")).overflowX,
  };

  const levels = document.createElement("div");
  appendMarkdown(levels, ["## Overview", "### Detail", "```python", "# not a heading", "```"].join("\n"));
  out.headingLevels = tags(levels, ".md-heading").map((h) => h.tagName.toLowerCase() + ":" + h.textContent);

  // 2. Grounding footer is separate from the answer and its citation summary opens the Sources drawer.
  const meta = md.querySelector(".message-grounding");
  out.footer = {
    insideBody: Boolean(body.querySelector(".message-grounding")), afterBody: meta?.previousElementSibling === body,
    badge: meta?.querySelector(".grounding-badge")?.textContent, buttonTag: meta?.querySelector(".citation-count")?.tagName,
    count: meta?.querySelector(".citation-count-label")?.textContent, pages: meta?.querySelector(".citation-pages")?.textContent,
    borderTop: getComputedStyle(meta).borderTopStyle, background: getComputedStyle(meta).backgroundColor,
    bubbleBackground: getComputedStyle(md.querySelector(".message")).backgroundColor,
  };
  meta.querySelector(".citation-count").click(); await sleep(150);
  out.drawer = {open: tutor.classList.contains("sources-open"), details: tags(tutor, ".source-citation-details").map((el) => el.textContent)};
  setSourcesDrawerOpen(false); await sleep(80);

  // 3. XSS: model HTML is shown as text and unsafe links are never created.
  const xss = await send(XSS);
  await sleep(200);
  out.xss = {flag: window.__xss, dangerous: tags(list, "img, script, iframe, object, embed, b").length,
    hrefs: tags(list, "a").map((a) => a.getAttribute("href")), text: xss.querySelector(".message-body").textContent,
    handlers: tags(list, "*").filter((el) => [...el.attributes].some((attr) => attr.name.startsWith("on"))).length};

  // 4. Plain text keeps reading as paragraphs with its line breaks.
  const plain = await send(PLAIN, "insufficient_context", []);
  const plainBody = plain.querySelector(".message-body");
  out.plain = {blocks: [...plainBody.children].map((el) => el.tagName.toLowerCase()), breaks: plainBody.querySelectorAll("br").length,
    text: plainBody.textContent, badge: plain.querySelector(".grounding-badge")?.textContent, citationButton: Boolean(plain.querySelector(".citation-count"))};

  // 5. A long answer with a long URL, long code and a wide table stays inside the chat.
  const long = await send(LONG);
  const pre = long.querySelector("pre"), table = long.querySelector(".md-table-wrap");
  out.long = {...layout(long), preScrolls: pre.scrollWidth > pre.clientWidth, tableScrolls: table.scrollWidth > table.clientWidth,
    answerTopVisible: Math.abs(rect(long).top - rect(list).top) <= 16, listScrollTop: list.scrollTop};
  out.markdownLayout = layout(md);

  // 6. Saved history renders with the same formatting and footer.
  activeConversation.messages = [{role: "user", content: "q"}, {role: "assistant", content: "**Saved** answer\n- one\n- two", grounding_status: "supported", citations}];
  renderConversationMessages(); await sleep(80);
  const saved = list.querySelector(".agent-row");
  out.history = {strong: saved.querySelector("strong")?.textContent, items: tags(saved, "li").length, footer: Boolean(saved.querySelector(".message-grounding .citation-count"))};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_page(width, height):
    work = Path(tempfile.mkdtemp(prefix=f"tutor_format_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(CHAT_MOCK, encoding="utf-8")
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


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorAnswerFormatDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(1440, 900)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_markdown_blocks_and_inline_formatting(self):
        md = self.out["markdown"]
        self.assertEqual(md["blocks"], ["p", "h3.md-heading", "ol", "blockquote", "pre", "div.md-table-wrap", "hr", "p"])
        self.assertEqual((md["heading"], md["strong"], md["em"], md["inlineCode"]), ("How priority works", "pause", "levels", "NVIC"))
        self.assertEqual(md["olItems"], 2)
        self.assertEqual(md["nestedItems"], ["nested interrupts preempt", "equal levels wait"])
        self.assertEqual(md["quote"], "Keep ISRs short.")
        self.assertEqual((md["codeBlock"], md["codeLanguage"]), ("void ISR(void) { flag = 1; }", "c"))
        self.assertEqual((md["tableHead"], md["tableCells"]), (["Term", "Meaning"], ["ISR", "Handler"]))
        self.assertEqual(md["links"], [
            {"href": "https://example.com/ds", "text": "the datasheet", "target": "_blank", "rel": "noopener noreferrer nofollow"},
            {"href": "https://example.com/notes", "text": "https://example.com/notes", "target": "_blank", "rel": "noopener noreferrer nofollow"},
        ])
        self.assertEqual(md["citationChips"], ["(source: lecture.pdf, page 5)"])
        # The answer's top heading level maps to h3; a "#" line inside code is not a heading.
        self.assertEqual(self.out["headingLevels"], ["h3:Overview", "h4:Detail"])

    def test_reading_typography(self):
        md = self.out["markdown"]
        self.assertGreater(md["headingSize"], md["paragraphSize"])
        self.assertGreaterEqual(md["paragraphSize"], 14)
        self.assertGreaterEqual(md["lineHeight"], 1.55)
        self.assertEqual(md["preOverflowX"], "auto")

    def test_grounding_footer_is_separate_and_opens_sources(self):
        footer = self.out["footer"]
        self.assertFalse(footer["insideBody"])
        self.assertTrue(footer["afterBody"])
        self.assertEqual(footer["badge"], "Grounded")
        self.assertEqual(footer["buttonTag"], "BUTTON")
        self.assertEqual((footer["count"], footer["pages"]), ("3 citations", "lecture.pdf · p. 2, 5"))
        self.assertEqual(footer["borderTop"], "solid")
        self.assertNotEqual(footer["background"], footer["bubbleBackground"])
        self.assertTrue(self.out["drawer"]["open"])
        self.assertIn("Page 5 · Page 2", " ".join(self.out["drawer"]["details"]))   # existing per-page source listing

    def test_model_html_and_unsafe_links_stay_inert(self):
        xss = self.out["xss"]
        self.assertEqual(xss["flag"], 0)
        self.assertEqual(xss["dangerous"], 0)
        self.assertEqual(xss["handlers"], 0)
        self.assertTrue(all(href.startswith(("http://", "https://")) for href in xss["hrefs"]), xss["hrefs"])
        for literal in ('<img src=x onerror="window.__xss=1">', "<script>window.__xss=2</script>", "[click me](javascript:window.__xss=3)",
                        "<b>bold html</b>", '<iframe src="javascript:window.__xss=5"></iframe>'):
            self.assertIn(literal, xss["text"])

    def test_plain_text_fallback(self):
        plain = self.out["plain"]
        self.assertEqual(plain["blocks"], ["p", "p"])
        self.assertEqual(plain["breaks"], 1)
        self.assertEqual(plain["text"], "Polling checks a flag in a loop.Interrupts react only when needed.This saves power because the CPU can sleep.")
        self.assertEqual(plain["badge"], "Insufficient context")
        self.assertFalse(plain["citationButton"])

    def test_long_answers_do_not_overflow(self):
        for key in ("long", "markdownLayout"):
            state = self.out[key]
            self.assertFalse(state["pageOverflow"], key)
            self.assertFalse(state["listOverflow"], key)
            self.assertTrue(state["bubbleInside"], key)
            self.assertTrue(state["blocksInside"], key)
        self.assertTrue(self.out["long"]["preScrolls"])     # long code scrolls inside its block
        self.assertTrue(self.out["long"]["tableScrolls"])   # wide tables scroll inside their wrapper
        self.assertTrue(self.out["long"]["answerTopVisible"])   # a long new answer opens at its start

    def test_saved_history_uses_the_same_rendering(self):
        self.assertEqual(self.out["history"], {"strong": "Saved", "items": 2, "footer": True})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TutorAnswerFormatPhoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(390, 844)

    def test_phone_renders_without_overflow(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        for key in ("long", "markdownLayout"):
            state = self.out[key]
            self.assertFalse(state["pageOverflow"], key)
            self.assertFalse(state["listOverflow"], key)
            self.assertTrue(state["bubbleInside"], key)
            self.assertTrue(state["blocksInside"], key)
        self.assertEqual(self.out["xss"]["flag"], 0)
        self.assertEqual(self.out["markdown"]["olItems"], 2)


if __name__ == "__main__":
    unittest.main()
