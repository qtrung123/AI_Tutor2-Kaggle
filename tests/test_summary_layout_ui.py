"""Real-browser layout test for Study Session -> Summary (headless Chrome, mock API from
tests/test_flashcard_ui_states.py). The Summary column fills the panel instead of stopping at a fixed
760px and leaving an empty band on the right; running text keeps a readable measure (~75ch at most);
wide panels place subsections side by side (at most two columns) with tables across the full width;
the Document Overview, topics and takeaways share one left edge. Desktop (1440px) and phone (390px).
Skipped without Chrome.
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

SUMMARY = {
    "model_id": "qwen-2.5-7b",
    "final_summary": {"overview": "This document explains how operating systems schedule processes and keep concurrent programs correct. " * 3,
                      "key_takeaways": ["Schedulers trade throughput for latency.", "Round robin bounds waiting time.",
                                        "Locks prevent race conditions.", "Deadlock needs four conditions at once."]},
    "topic_summaries": [
        {"topic_id": "t1", "topic_name": "CPU scheduling", "overview": "Scheduling decides which ready process runs next on the CPU. " * 3,
         "subsections": [
             {"subtopic_id": "s1", "subtopic_name": "First come, first served",
              "content": {"type": "paragraph", "text": "Processes run in arrival order; long jobs delay short ones (the convoy effect). " * 3}},
             {"subtopic_id": "s2", "subtopic_name": "Round robin",
              "content": {"type": "paragraph", "text": "Each process gets a fixed time quantum; a small quantum improves response time. " * 3}},
             {"subtopic_id": "s3", "subtopic_name": "Comparison",
              "content": {"type": "table", "headers": ["Algorithm", "Preemptive"], "rows": [["FCFS", "No"], ["RR", "Yes"]]}},
         ]},
    ],
}

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
    await openStudySession("lecture.pdf", "summary"); await sleep(400);
    summaryLoading.hidden = true; summaryGenerate.hidden = true; summaryError.hidden = true;
    renderDocumentSummary(SUMMARY_JSON); await sleep(200);
    const panel = document.querySelector(".summary-panel"), content = document.getElementById("summary-content");
    const cs = getComputedStyle(panel), pr = panel.getBoundingClientRect();
    const innerRight = pr.right - parseFloat(cs.paddingRight), innerWidth_ = innerRight - pr.left - parseFloat(cs.paddingLeft);
    // Line length in characters: an element's width over the width of "0" in its own font.
    const chars = (el) => { const probe = document.createElement("span"); probe.style.font = getComputedStyle(el).font;
      probe.style.position = "absolute"; probe.style.visibility = "hidden"; probe.textContent = "0".repeat(20);
      document.body.appendChild(probe); const ch = probe.getBoundingClientRect().width / 20; probe.remove();
      return Math.round(el.getBoundingClientRect().width / ch); };
    const left = (selector) => Math.round(document.querySelector(selector).getBoundingClientRect().left);
    const textSubsections = [...document.querySelectorAll(".summary-subsection")].filter((el) => !el.querySelector(".summary-table-wrap"));
    out.layout = {
      unusedRight: Math.round(innerRight - content.getBoundingClientRect().right),
      overviewChars: chars(document.querySelector(".summary-overview")),
      topicOverviewChars: chars(document.querySelector(".summary-topic-overview")),
      subsectionChars: textSubsections.map((el) => chars(el.querySelector("p"))),
      subsectionColumns: new Set(textSubsections.map((el) => Math.round(el.getBoundingClientRect().left))).size,
      tableShare: Math.round(100 * document.querySelector(".summary-table-wrap").getBoundingClientRect().width / innerWidth_),
      lefts: [left(".summary-heading h2"), left(".summary-overview"), left(".summary-topic h3"), left(".summary-final-takeaways")],
      overviewFontPx: parseFloat(getComputedStyle(document.querySelector(".summary-overview")).fontSize),
      overflowX: document.documentElement.scrollWidth > innerWidth + 1,
    };
  } catch (error) { out.fatal = String(error && error.stack || error); }
  publish();
})();
""".replace("SUMMARY_JSON", json.dumps(SUMMARY))

PHONE_HOST = """<!doctype html><html><body style="margin:0"><iframe src="index.html" style="width:{w}px;height:{h}px;border:0"></iframe>
<script>addEventListener("message", (e) => {{ const p = document.createElement("pre"); p.id = "harness-out"; p.textContent = e.data;
document.body.appendChild(p); }});</script></body></html>"""


def run_at_width(width, height):
    work = Path(tempfile.mkdtemp(prefix=f"summary_layout_{width}_"))
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


class SummaryLayoutAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_content_fills_the_panel(self):
        self.assertLessEqual(self.out["layout"]["unusedRight"], 4)   # no empty band on the right
        self.assertGreaterEqual(self.out["layout"]["tableShare"], 90)   # tables use the full width
        self.assertFalse(self.out["layout"]["overflowX"])

    def test_text_keeps_a_readable_measure(self):
        layout = self.out["layout"]
        for count in [layout["overviewChars"], layout["topicOverviewChars"], *layout["subsectionChars"]]:
            self.assertLessEqual(count, 80)
        self.assertGreaterEqual(layout["overviewFontPx"], 16)

    def test_sections_share_one_left_edge(self):
        self.assertEqual(len(set(self.out["layout"]["lefts"])), 1)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SummaryLayoutDesktopTests(SummaryLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1440, 1000)

    def test_wide_panel_places_subsections_side_by_side(self):
        self.assertEqual(self.out["layout"]["subsectionColumns"], 2)
        self.assertGreaterEqual(min(self.out["layout"]["subsectionChars"]), 45)   # columns stay comfortably wide


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SummaryLayoutPhoneTests(SummaryLayoutAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 1400)

    def test_phone_uses_a_single_column(self):
        self.assertEqual(self.out["layout"]["subsectionColumns"], 1)


if __name__ == "__main__":
    unittest.main()
