"""UI-QUICK-WINS: upload/indexing status built only from the real upload response, and the frontend
server's no-store headers that make a redeploy load the new JavaScript.

- The real app in headless Chrome with a held upload request: busy -> indexed -> result / error.
- frontend/server.js under Node: index.html, app.js and js/*.js are all served with no-store.
"""

import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, MOCK, find_chrome

UPLOAD_MOCK = MOCK + r"""
window.__uploads = [];      // queued responses: {status, body}
window.__uploadHold = null; // when set, the upload request waits for it
const quizPlayerFetchForUpload = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const p = new URL(url, "http://x").pathname;
  if (p === "/api/sources/upload") {
    if (window.__uploadHold) await window.__uploadHold;
    const next = window.__uploads.shift();
    return new Response(JSON.stringify(next.body), {status: next.status, headers: {"Content-Type": "application/json"}});
  }
  return quizPlayerFetchForUpload(input, init);
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
const status = () => ({text: $("upload-status").textContent, state: $("upload-status").dataset.state || "",
  role: $("upload-status").getAttribute("role"), busy: $("upload-source-button").getAttribute("aria-busy"),
  disabled: $("upload-source-button").disabled});
const pdf = (name) => new File(["%PDF-1.4"], name, {type: "application/pdf"});
(async () => {
  await sleep(1500);
  out.initial = status();

  // 1. One new file: busy while the request runs, then the real counts from the response.
  let release; window.__uploadHold = new Promise((resolve) => { release = resolve; });
  window.__uploads.push({status: 200, body: {new_files: 1, new_chunks: 12, skipped_files: [], total_indexed_files: 2, sources: [], planner: {status: "unchanged"}}});
  const first = uploadSourceFiles([pdf("week1.pdf")]);
  await sleep(100); out.during = status();
  window.__uploadHold = null; release(); await first; await sleep(50);
  out.indexed = status();

  // 2. Two files, one already in the library.
  window.__uploads.push({status: 200, body: {new_files: 1, new_chunks: 1, skipped_files: ["old.pdf"], total_indexed_files: 3, sources: [], planner: {status: "unchanged"}}});
  const second = uploadSourceFiles([pdf("week2.pdf"), pdf("old.pdf")]);
  out.duringTwo = status();   // set synchronously, before the request is awaited
  await second; await sleep(50);
  out.partlySkipped = status();

  // 3. Only duplicates.
  window.__uploads.push({status: 200, body: {new_files: 0, new_chunks: 0, skipped_files: ["old.pdf"], total_indexed_files: 3, sources: [], planner: {status: "unchanged"}}});
  await uploadSourceFiles([pdf("old.pdf")]); await sleep(50);
  out.allSkipped = status();

  // 4. A backend error keeps its detail.
  window.__uploads.push({status: 503, body: {detail: "The file could not be saved to durable storage."}});
  await uploadSourceFiles([pdf("bad.pdf")]); await sleep(50);
  out.failed = status();
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class UploadStatusPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="ui_quick_wins_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(UPLOAD_MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=30000", "--dump-dom",
             f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_status_is_a_live_region(self):
        self.assertEqual(self.out["initial"]["role"], "status")

    def test_busy_state_names_the_file_without_a_made_up_percentage(self):
        during = self.out["during"]
        self.assertEqual(during["state"], "busy")
        self.assertIn('Uploading and indexing "week1.pdf"', during["text"])
        self.assertNotIn("%", during["text"])
        self.assertEqual((during["busy"], during["disabled"]), ("true", True))
        self.assertIn("Uploading and indexing 2 files", self.out["duringTwo"]["text"])

    def test_result_comes_from_the_upload_response(self):
        indexed = self.out["indexed"]
        self.assertEqual((indexed["text"], indexed["state"]), ("1 file indexed (12 chunks).", "done"))
        self.assertEqual((indexed["busy"], indexed["disabled"]), (None, False))
        self.assertEqual(self.out["partlySkipped"]["text"], "1 file indexed (1 chunk); already in your library: old.pdf.")
        self.assertEqual(self.out["allSkipped"]["text"], "Already in your library: old.pdf.")

    def test_error_keeps_the_backend_detail(self):
        failed = self.out["failed"]
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["text"], "Upload failed: The file could not be saved to durable storage.")
        self.assertEqual((failed["busy"], failed["disabled"]), (None, False))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class FrontendServerCacheTests(unittest.TestCase):
    """The deployed frontend (deployment/start_kaggle.sh runs `node server.js` behind nginx) must
    never let a browser or proxy keep an old page or script after a redeploy."""

    def test_page_and_scripts_are_served_no_store(self):
        port = free_port()
        server = subprocess.Popen(["node", str(FRONTEND / "server.js")], cwd=FRONTEND,
                                  env={**os.environ, "FRONTEND_PORT": str(port), "FRONTEND_HOST": "127.0.0.1", "API_BASE_URL": ""},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.terminate)
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        for path in ("/", "/index.html", "/app.js", "/js/tutor.js", "/js/quiz.js", "/styles.css", "/app-config.js"):
            with self.subTest(path=path), urllib.request.urlopen(base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main()
