"""The Summary screen starts a background job and polls it instead of holding one request open.

The real functions are extracted from the frontend source and run with Node against a minimal DOM
stub, a scripted fetchJson and a manual timer queue (skipped without Node).
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
const asyncName = (name) => source.includes(`async function ${name}(`) ? `async ${extract(name)}` : extract(name);
const NAMES = ["showSummaryState", "showSummaryGenerating", "finishSummaryGeneration", "stopSummaryPolling",
               "pollSummaryJob", "loadDocumentSummary", "updateSummaryChrome"];

class Element { constructor() { this.hidden = false; this.disabled = false; this.textContent = ""; this.children = []; }
                set innerHTML(_value) { this.children = []; } }

function makeContext(routes) {
  const calls = [], timers = [], rendered = [];
  const context = vm.createContext({
    summaryContent: new Element(), summaryLoading: new Element(), summaryError: new Element(),
    summaryGenerate: new Element(), regenerateSummaryButton: new Element(),
    activeDocumentId: "doc.pdf", selectedModelId: "model-a", loadedSummaryKey: "", summaryInFlightKey: "",
    summaryPollTimer: null, summaryPollJobId: "", SUMMARY_JOB_POLL_MS: 2000, SUMMARY_JOB_MAX_POLL_ERRORS: 5,
    SUMMARY_API_BASE_URL: "/api/summary", document: { body: { dataset: { sessionTab: "summary" } } },
    ensureSelectedModelReadyWithStatus: async () => {},
    renderDocumentSummary: (summary) => { rendered.push(summary.final_summary.overview); context.summaryContent.children = [1]; },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id && timers[id - 1]) timers[id - 1].fn = null; },
    fetchJson: async (url, options = {}) => {
      const method = options.method || "GET";
      calls.push(`${method} ${url}`);
      const route = routes.find(([prefix]) => `${method} ${url}`.startsWith(prefix));
      if (!route) throw new Error(`unexpected ${method} ${url}`);
      const reply = Array.isArray(route[1]) ? (route[1].length > 1 ? route[1].shift() : route[1][0]) : route[1];
      if (reply instanceof Error) throw reply;
      return JSON.parse(JSON.stringify(reply));
    },
  });
  vm.runInContext(NAMES.map(asyncName).join("\n"), context);
  const runTimers = async () => {   // run every pending (non-cancelled) timer once
    const pending = timers.splice(0).filter((timer) => timer.fn);
    timers.push(...pending.map(() => ({ fn: null })));   // keep ids stable
    for (const timer of pending) await timer.fn();
  };
  const pendingTimers = () => timers.filter((timer) => timer.fn).length;
  const state = () => ({ calls: [...calls], rendered: [...rendered], loading: !context.summaryLoading.hidden,
    error: context.summaryError.hidden ? "" : context.summaryError.textContent, generateVisible: !context.summaryGenerate.hidden,
    inFlight: context.summaryInFlightKey, pendingTimers: pendingTimers() });
  return { context, runTimers, state };
}

const SUMMARY = { final_summary: { overview: "Done overview." }, topic_summaries: [] };
const scenarios = {
  async complete() {
    const { context, runTimers, state } = makeContext([
      ["POST /api/summary/generate", { status: "processing", job_id: "j1", document_id: "doc.pdf" }],
      ["GET /api/summary/jobs/j1", [{ status: "processing", job_id: "j1" }, { status: "complete", job_id: "j1", summary: SUMMARY }]],
    ]);
    await context.loadDocumentSummary();
    const started = state();
    await runTimers();
    const processing = state();
    await runTimers();
    return { started, processing, done: state() };
  },
  async failed() {
    const { context, runTimers, state } = makeContext([
      ["POST /api/summary/generate", { status: "processing", job_id: "j2", document_id: "doc.pdf" }],
      ["GET /api/summary/jobs/j2", { status: "failed", job_id: "j2", error: { code: "generation_failed", message: "Could not generate document summary: model crashed" } }],
    ]);
    await context.loadDocumentSummary();
    await runTimers();
    return state();
  },
  async duplicate() {
    const { context, state } = makeContext([
      ["POST /api/summary/generate", { status: "processing", job_id: "j3", document_id: "doc.pdf" }],
      ["GET /api/summary/jobs/j3", { status: "processing", job_id: "j3" }],
    ]);
    await Promise.all([context.loadDocumentSummary(), context.loadDocumentSummary()]);
    await context.loadDocumentSummary();
    await context.loadDocumentSummary(true);
    return state();
  },
  async immediate() {
    const { context, state } = makeContext([["POST /api/summary/generate", { ...SUMMARY, cache_hit: true }]]);
    await context.loadDocumentSummary();
    return state();
  },
  async resume() {
    const { context, runTimers, state } = makeContext([
      ["GET /api/summary/doc.pdf?", { status: "not_generated" }],
      ["GET /api/summary/jobs?document_id=doc.pdf", { status: "processing", job_id: "j9", document_id: "doc.pdf" }],
      ["GET /api/summary/jobs/j9", { status: "complete", job_id: "j9", summary: SUMMARY }],
    ]);
    await context.showSummaryState();
    const resumed = state();
    await runTimers();
    return { resumed, done: state() };
  },
  async leave() {
    const { context, runTimers, state } = makeContext([
      ["POST /api/summary/generate", { status: "processing", job_id: "j4", document_id: "doc.pdf" }],
      ["GET /api/summary/jobs/j4", { status: "processing", job_id: "j4" }],
    ]);
    await context.loadDocumentSummary();
    await runTimers();
    context.activeDocumentId = "other.pdf";
    await runTimers();
    return state();
  },
};
(async () => {
  const results = {};
  for (const [name, run] of Object.entries(scenarios)) results[name] = await run();
  process.stdout.write(JSON.stringify(results));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def run_scenarios():
    result = subprocess.run(
        ["node", "-e", NODE_DRIVER, json.dumps([str(path) for path in frontend_script_paths()])],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SummaryJobPollingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = run_scenarios()

    def test_generate_starts_a_job_then_polls_processing_to_complete(self):
        started, processing, done = (self.results["complete"][key] for key in ("started", "processing", "done"))
        self.assertEqual(started["calls"], ["POST /api/summary/generate"])
        self.assertTrue(started["loading"]); self.assertFalse(started["generateVisible"])
        self.assertEqual(started["pendingTimers"], 1)
        self.assertTrue(processing["loading"]); self.assertEqual(processing["rendered"], [])
        self.assertEqual(done["calls"], ["POST /api/summary/generate", "GET /api/summary/jobs/j1", "GET /api/summary/jobs/j1"])
        self.assertEqual(done["rendered"], ["Done overview."])
        self.assertFalse(done["loading"]); self.assertEqual(done["pendingTimers"], 0); self.assertEqual(done["inFlight"], "")

    def test_failed_job_stops_polling_shows_the_error_and_allows_retry(self):
        failed = self.results["failed"]
        self.assertEqual(failed["error"], "Could not generate document summary: model crashed")
        self.assertFalse(failed["loading"]); self.assertEqual(failed["pendingTimers"], 0)
        self.assertTrue(failed["generateVisible"]); self.assertEqual(failed["inFlight"], "")

    def test_clicking_generate_again_while_processing_starts_no_second_job(self):
        duplicate = self.results["duplicate"]
        self.assertEqual(duplicate["calls"], ["POST /api/summary/generate"])
        self.assertEqual(duplicate["pendingTimers"], 1)

    def test_an_already_saved_summary_renders_without_polling(self):
        immediate = self.results["immediate"]
        self.assertEqual(immediate["rendered"], ["Done overview."])
        self.assertEqual(immediate["pendingTimers"], 0); self.assertFalse(immediate["loading"])

    def test_reopening_resumes_the_running_job_instead_of_starting_another(self):
        resumed, done = self.results["resume"]["resumed"], self.results["resume"]["done"]
        self.assertTrue(resumed["loading"]); self.assertEqual(resumed["pendingTimers"], 1)
        self.assertFalse([call for call in done["calls"] if call.startswith("POST")])
        self.assertEqual(done["calls"][-1], "GET /api/summary/jobs/j9")
        self.assertEqual(done["rendered"], ["Done overview."])

    def test_leaving_stops_only_frontend_polling(self):
        leave = self.results["leave"]
        self.assertEqual(leave["calls"], ["POST /api/summary/generate", "GET /api/summary/jobs/j4"])
        self.assertEqual(leave["pendingTimers"], 0); self.assertEqual(leave["inFlight"], "")
        self.assertFalse([call for call in leave["calls"] if "cancel" in call or call.startswith("DELETE")])


if __name__ == "__main__":
    unittest.main()
