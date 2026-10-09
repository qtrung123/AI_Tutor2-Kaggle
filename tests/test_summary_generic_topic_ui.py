"""The Summary UI shows a generic single-topic document's overview once.

Root cause this covers: for a document without real topics the backend returns one generic topic
(topic_document_overview / "Document Overview") whose overview repeats final_summary.overview, and
renderDocumentSummary rendered both the final overview and that topic's heading + overview.

The real functions are extracted from the frontend source and run with Node against a minimal DOM
stub (skipped without Node).
"""

import json
import shutil
import subprocess
import unittest

from frontend_source import frontend_script_paths

NODE_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const source = JSON.parse(process.argv[1]).map((path) => fs.readFileSync(path, "utf8")).join("\n");
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`missing function ${name}`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated function ${name}`);
}
class Element {
  constructor(tag) { this.tagName = tag; this.className = ""; this.textContent = ""; this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
  set innerHTML(value) { this.children = []; }
}
const document = { createElement: (tag) => new Element(tag) };
const context = vm.createContext({ document, summaryContent: new Element("div"), selectedModelId: "qwen-2.5-7b",
                                   modelLabel: () => "Qwen 2.5 7B" });
const names = ["isSingleGenericSummaryTopic", "renderDocumentSummary", "renderSummaryContent", "appendTakeaways"];
vm.runInContext(names.map(extract).join("\n"), context);
vm.runInContext(`renderDocumentSummary(${process.argv[2]})`, context);
// Flatten the rendered tree to [tag, className, text] rows in document order.
const rows = [];
(function walk(node) { node.children.forEach((child) => { rows.push([child.tagName, child.className, child.textContent]); walk(child); }); })(context.summaryContent);
process.stdout.write(JSON.stringify(rows));
"""

OVERVIEW = "This document explains process scheduling and synchronization."
TAKEAWAYS = ["Schedulers trade throughput for latency.", "Locks prevent races."]


def render(summary):
    result = subprocess.run(
        ["node", "-e", NODE_DRIVER, json.dumps([str(path) for path in frontend_script_paths()]), json.dumps(summary)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def summary_with(topics):
    return {"model_id": "qwen-2.5-7b", "topic_summaries": topics,
            "final_summary": {"overview": OVERVIEW, "key_takeaways": TAKEAWAYS}}


def texts(rows, tag=None, class_name=None):
    return [text for row_tag, row_class, text in rows
            if (tag is None or row_tag == tag) and (class_name is None or row_class == class_name)]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class GenericSummaryTopicUiTests(unittest.TestCase):
    def assert_generic_topic_rendered_once(self, topic):
        rows = render(summary_with([topic]))
        self.assertEqual(texts(rows).count(OVERVIEW), 1)
        self.assertEqual(texts(rows, class_name="summary-overview"), [OVERVIEW])
        self.assertEqual(texts(rows, class_name="summary-topic-overview"), [])
        self.assertNotIn("Document Overview", texts(rows, tag="h3"))
        self.assertEqual(texts(rows, tag="li"), TAKEAWAYS)
        return rows

    def test_single_generic_document_overview_is_rendered_only_once(self):
        rows = self.assert_generic_topic_rendered_once(
            {"topic_id": "topic_document_overview", "topic_name": "Document Overview", "overview": OVERVIEW, "subsections": []})
        self.assertEqual(texts(rows, class_name="summary-topic"), [])

    def test_generic_topic_is_detected_by_id_or_by_name(self):
        self.assert_generic_topic_rendered_once({"topic_id": "topic_document_overview", "topic_name": "Overview", "overview": OVERVIEW})
        self.assert_generic_topic_rendered_once({"topic_id": "topic_1", "topic_name": "Document Overview", "overview": OVERVIEW})

    def test_generic_topic_keeps_its_subsection_notes(self):
        rows = self.assert_generic_topic_rendered_once({
            "topic_id": "topic_document_overview", "topic_name": "Document Overview", "overview": OVERVIEW,
            "subsections": [{"subtopic_id": "sync", "subtopic_name": "Synchronization",
                             "content": {"type": "paragraph", "text": "Mutexes serialize access."}}],
        })
        self.assertEqual(texts(rows, tag="h4"), ["Synchronization"])

    def test_multiple_real_topics_are_all_rendered(self):
        topics = [
            {"topic_id": "topic_scheduling", "topic_name": "Scheduling", "overview": "Round robin and priorities.", "subsections": []},
            {"topic_id": "topic_sync", "topic_name": "Synchronization", "overview": "Locks and semaphores.", "subsections": []},
        ]
        rows = render(summary_with(topics))
        self.assertEqual(texts(rows, class_name="summary-overview"), [OVERVIEW])
        self.assertEqual(texts(rows, tag="h3"), ["Scheduling", "Synchronization", "Summary & Key Takeaways"])
        self.assertEqual(texts(rows, class_name="summary-topic-overview"), ["Round robin and priorities.", "Locks and semaphores."])
        self.assertEqual(texts(rows, tag="li"), TAKEAWAYS)

    def test_a_single_real_topic_is_still_rendered(self):
        rows = render(summary_with([{"topic_id": "topic_sync", "topic_name": "Synchronization", "overview": "Locks.", "subsections": []}]))
        self.assertIn("Synchronization", texts(rows, tag="h3"))
        self.assertEqual(texts(rows, class_name="summary-topic-overview"), ["Locks."])


if __name__ == "__main__":
    unittest.main()
