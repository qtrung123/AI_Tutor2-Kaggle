"""Real-browser tests for the Flashcards UI states redesign (empty / preparing-generating /
ready deck / error+retry), the actual frontend/app.js in headless Chrome against a mocked API.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no Ollama, no
network. Complements tests/test_frontend_browser_smoke.py (which covers the Quiz/Summary/Flashcards
redesign at a high level) with the specific Flashcards states this redesign introduces.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def find_chrome():
    candidates = [os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    return next((path for path in candidates if path and Path(path).exists()), None)


MOCK = r"""
window.APP_CONFIG = { API_BASE_URL: "" };
window.__calls = [];
window.__prepareHold = null;          // when set, /api/models/*/prepare waits for it
window.__generateHold = null;         // when set, the real (non-cache_only) GET waits for it
window.__failNextGenerate = false;    // when true, the next real (non-cache_only) GET fails once
window.__regenerateHold = null;       // when set, POST /regenerate waits for it
window.__failNextRegenerate = false;  // when true, the next POST /regenerate fails once
const CARDS = [
  {flashcard_id: "c1", front: "Front 1", back: "Back 1", topic_id: "t1", topic_name: "Topic 1", is_favorite: false},
  {flashcard_id: "c2", front: "Front 2", back: "Back 2", topic_id: "t1", topic_name: "Topic 1", is_favorite: false},
  {flashcard_id: "c3", front: "Front 3", back: "Back 3", topic_id: "t1", topic_name: "Topic 1", is_favorite: false},
];
const REGENERATED_CARDS = [
  {flashcard_id: "r1", front: "Regenerated 1", back: "Regenerated Back 1", topic_id: "t1", topic_name: "Topic 1", is_favorite: false},
  {flashcard_id: "r2", front: "Regenerated 2", back: "Regenerated Back 2", topic_id: "t1", topic_name: "Topic 1", is_favorite: false},
];
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  window.__calls.push(method + " " + url);
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [
    {id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: false},
    {id: "gemma3-12b", label: "Gemma 3 12B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) {
    if (window.__prepareHold) await window.__prepareHold;
    return json({status: "ready", ready: true});
  }
  if (p === "/api/documents") return json([{id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([]);
  if (p === "/api/quiz-history") return json([]);
  if (p === "/api/quiz/lecture.pdf" && method === "GET") return json({document_id: "lecture.pdf", difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"), quiz: null, latest_attempt: null, attempt_summary: null});
  if (p.startsWith("/api/summary/lecture.pdf")) return json({status: "not_generated"});
  if (p === `/api/flashcards/lecture.pdf/regenerate` && method === "POST") {
    const body = JSON.parse(init.body);
    if (window.__regenerateHold) await window.__regenerateHold;
    if (window.__failNextRegenerate) {
      window.__failNextRegenerate = false;
      return json({detail: "Couldn't generate flashcards with the selected model. Please try again or switch models."}, 502);
    }
    return json({set_id: "s2", model_id: body.model_id, cards: REGENERATED_CARDS});
  }
  if (p.startsWith("/api/flashcards/lecture.pdf")) {
    if (u.searchParams.get("cache_only") === "true") return json({status: "not_generated", cards: []});
    if (window.__generateHold) await window.__generateHold;
    if (window.__failNextGenerate) {
      window.__failNextGenerate = false;
      return json({detail: "Couldn't generate flashcards with the selected model. Please try again or switch models."}, 502);
    }
    return json({set_id: "s1", model_id: u.searchParams.get("model_id"), cards: CARDS});
  }
  if (p === "/api/conversations" && method === "POST") return json({id: "c1", title: "New conversation", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p.startsWith("/api/conversations/")) return json({id: "c1", title: "t", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p === "/api/conversations") return json([]);
  if (p === "/api/dashboard") return json({mastery: [], materials: [], summary: {}});
  if (p === "/api/sources") return json([]);
  return json([]);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const visible = (el) => Boolean(el) && !el.hidden;
const flashcardCalls = () => window.__calls.filter((c) => c.includes("/api/flashcards/"));
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "flashcards");
  await sleep(300);

  // 1) Empty state: no flashcards yet, selected model + language shown, Generate button present.
  out.empty = {
    generatePromptVisible: visible(flashcardsGenerate), stageVisible: visible(flashcardsStage),
    errorVisible: visible(flashcardsError), loadingVisible: visible(flashcardsLoading),
    modelNote: document.querySelector("#flashcards-generate .generate-model-note")?.textContent,
    languageNote: document.getElementById("flashcards-empty-language")?.textContent,
    hasGenerateButton: Boolean(generateFlashcardsButton),
  };

  // 2) Preparing / Generating: hold model prepare so "Preparing model…" is observable, and the
  // Generate button must be disabled while busy (no duplicate actions). The bootstrap's own
  // silent "prime the selected model" call already marked it ready before __prepareHold existed,
  // so force it back to not-ready to make this specific prepare call actually hit the network.
  const qwenModel = generationModels.find((m) => m.id === "qwen-2.5-7b");
  if (qwenModel) qwenModel.ready = false;
  let releasePrepare; window.__prepareHold = new Promise((resolve) => { releasePrepare = resolve; });
  let releaseGenerate; window.__generateHold = new Promise((resolve) => { releaseGenerate = resolve; });
  const pendingFirstGenerate = loadDocumentFlashcards();
  await sleep(200);
  out.preparing = {
    loadingVisible: visible(flashcardsLoading), loadingLabel: flashcardsLoadingLabel.textContent,
    generateDisabled: generateFlashcardsButton.disabled,
    skeletonVisible: visible(flashcardsLoading) && Boolean(document.querySelector(".flashcards-skeleton-card")),
  };
  releasePrepare(); await sleep(150);
  out.generating = {
    loadingVisible: visible(flashcardsLoading), loadingLabel: flashcardsLoadingLabel.textContent,
    generateDisabled: generateFlashcardsButton.disabled,
  };
  releaseGenerate(); window.__generateHold = null; await pendingFirstGenerate; await sleep(200);

  // 3) Ready deck: large card, flip, previous/next + progress, shuffle, regenerate, model label.
  out.ready = {
    stageVisible: visible(flashcardsStage), generatePromptVisible: visible(flashcardsGenerate),
    position: flashcardPosition.textContent, front: flashcardCopy.textContent, sideLabel: flashcardSideLabel.textContent,
    modelNote: flashcardsModelNote.textContent, regenerateVisible: visible(regenerateFlashcardsButton),
    progressWidth: flashcardProgressBar.style.width,
  };
  flashcardElement.click(); await sleep(50);
  out.afterFlip = { sideLabel: flashcardSideLabel.textContent, back: flashcardCopy.textContent, flippedClass: flashcardElement.classList.contains("flipped") };
  document.getElementById("next-flashcard").click(); await sleep(20);
  document.getElementById("next-flashcard").click(); await sleep(20);
  out.afterTwoNext = { position: flashcardPosition.textContent, sideLabelResetOnNav: flashcardSideLabel.textContent };
  document.getElementById("next-flashcard").click(); await sleep(20);   // wraps 3/3 -> 1/3
  out.afterWrapNext = { position: flashcardPosition.textContent };
  document.getElementById("previous-flashcard").click(); await sleep(20);   // wraps 1/3 -> 3/3
  out.afterWrapPrevious = { position: flashcardPosition.textContent };

  document.getElementById("shuffle-flashcards").click(); await sleep(20);
  out.afterShuffle = { position: flashcardPosition.textContent };

  document.getElementById("manage-flashcards").click(); await sleep(50);
  out.manager = { open: document.querySelector(".flashcard-manager")?.classList.contains("open"),
    rows: document.querySelectorAll(".managed-flashcard").length };
  document.querySelector(".flashcard-manager-close")?.click(); await sleep(20);

  // 4) Regenerate over an already-visible deck must never swap in the full empty/skeleton state:
  // the current deck (and its front text) stays on screen, with only a small status + a disabled,
  // relabeled Regenerate button, until the new deck replaces it.
  let releaseRegenerate; window.__regenerateHold = new Promise((resolve) => { releaseRegenerate = resolve; });
  const frontBeforeRegenerate = flashcardCopy.textContent;
  const callsBeforeRegenerate = flashcardCalls().length;
  const pendingRegenerate = loadDocumentFlashcards(true);
  await sleep(150);
  out.duringRegenerate = {
    stageVisible: visible(flashcardsStage), frontUnchanged: flashcardCopy.textContent === frontBeforeRegenerate,
    position: flashcardPosition.textContent,
    fullSkeletonVisible: visible(flashcardsLoading),
    skeletonCardVisible: visible(flashcardsLoading) && Boolean(document.querySelector(".flashcards-skeleton-card")),
    generatePromptVisible: visible(flashcardsGenerate),
    statusVisible: visible(document.getElementById("flashcards-regenerate-status")),
    statusText: document.getElementById("flashcards-regenerate-status")?.textContent,
    regenerateDisabled: regenerateFlashcardsButton.disabled, regenerateLabel: regenerateFlashcardsButton.textContent,
  };
  releaseRegenerate(); window.__regenerateHold = null; await pendingRegenerate; await sleep(200);
  out.afterRegenerateSuccess = {
    regeneratePosted: flashcardCalls().slice(callsBeforeRegenerate).some((c) => c.includes("/regenerate") && c.startsWith("POST")),
    front: flashcardCopy.textContent, position: flashcardPosition.textContent, modelNote: flashcardsModelNote.textContent,
    regenerateLabel: regenerateFlashcardsButton.textContent, regenerateDisabled: regenerateFlashcardsButton.disabled,
    statusVisible: visible(document.getElementById("flashcards-regenerate-status")), stageVisible: visible(flashcardsStage),
  };

  // 5) A failed regenerate must keep the (new, just-swapped-in) deck visible with a friendly inline
  // retryable banner -- never the full-page error state, never an empty pane.
  const frontBeforeFailedRegenerate = flashcardCopy.textContent;
  window.__failNextRegenerate = true;
  await loadDocumentFlashcards(true); await sleep(200);
  out.failedRegenerate = {
    stageVisible: visible(flashcardsStage), frontUnchanged: flashcardCopy.textContent === frontBeforeFailedRegenerate,
    fullErrorVisible: visible(flashcardsError), regenerateLabel: regenerateFlashcardsButton.textContent,
    bannerVisible: visible(document.getElementById("flashcards-regenerate-error")),
    bannerMessage: document.getElementById("flashcards-regenerate-error-message")?.textContent,
  };
  document.getElementById("retry-regenerate-flashcards-button")?.click(); await sleep(200);
  out.afterFailedRegenerateRetry = {
    stageVisible: visible(flashcardsStage), front: flashcardCopy.textContent,
    bannerVisible: visible(document.getElementById("flashcards-regenerate-error")),
  };

  // 6) Error + retry: force the next real generate to fail, force a fresh generation, check the
  // friendly message (never raw/backend technical text as the primary line) + collapsed details,
  // then retry and confirm recovery.
  loadedFlashcardKey = ""; flashcardSet = null; flashcards = []; flashcardIndex = 0;
  window.__failNextGenerate = true;
  await loadDocumentFlashcards(); await sleep(200);
  out.error = {
    errorVisible: visible(flashcardsError), stageVisible: visible(flashcardsStage),
    message: document.getElementById("flashcards-error-message").textContent,
    hasRetryButton: Boolean(retryFlashcardsButton), retryVisible: visible(retryFlashcardsButton),
    detailsCollapsed: !document.querySelector(".flashcards-error-details").open,
    technical: document.getElementById("flashcards-error-technical").textContent,
  };
  retryFlashcardsButton.click(); await sleep(300);
  out.afterRetry = { stageVisible: visible(flashcardsStage), errorVisible: visible(flashcardsError), cardCount: flashcards.length };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class FlashcardUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="flashcard_ui_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")   # app.js's classic-script modules
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=40000", "--dump-dom",
             f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def test_the_app_runs_without_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_empty_state_shows_selected_model_language_and_generate_button(self):
        empty = self.out["empty"]
        self.assertTrue(empty["generatePromptVisible"])
        self.assertFalse(empty["stageVisible"]); self.assertFalse(empty["errorVisible"]); self.assertFalse(empty["loadingVisible"])
        self.assertTrue(empty["hasGenerateButton"])
        self.assertEqual(empty["modelNote"], "Model: Qwen 2.5 7B (change it in the Model selector above)")
        self.assertEqual(empty["languageNote"], "Auto")

    def test_preparing_then_generating_are_distinct_and_disable_generate(self):
        preparing, generating = self.out["preparing"], self.out["generating"]
        self.assertTrue(preparing["loadingVisible"]); self.assertTrue(preparing["skeletonVisible"])
        self.assertIn("Preparing", preparing["loadingLabel"])
        self.assertTrue(preparing["generateDisabled"])   # no duplicate actions while busy
        self.assertTrue(generating["loadingVisible"])
        self.assertIn("Generating", generating["loadingLabel"])
        self.assertNotEqual(preparing["loadingLabel"], generating["loadingLabel"])
        self.assertTrue(generating["generateDisabled"])

    def test_initial_generation_with_no_existing_deck_still_uses_the_full_skeleton_state(self):
        # Contrasts with test_regenerate_keeps_the_existing_deck_visible_...: only regenerating OVER
        # an existing deck skips the full empty/skeleton state -- an initial generation (no deck
        # yet) always uses it.
        preparing = self.out["preparing"]
        self.assertTrue(preparing["loadingVisible"])
        self.assertTrue(preparing["skeletonVisible"])

    def test_ready_deck_shows_large_card_progress_and_model_label(self):
        ready = self.out["ready"]
        self.assertTrue(ready["stageVisible"]); self.assertFalse(ready["generatePromptVisible"])
        self.assertEqual(ready["position"], "1 / 3")
        self.assertEqual(ready["front"], "Front 1")
        self.assertEqual(ready["sideLabel"], "Front")
        self.assertEqual(ready["modelNote"], "Generated by Qwen 2.5 7B")
        self.assertTrue(ready["regenerateVisible"])
        self.assertNotEqual(ready["progressWidth"], "")

    def test_flip_reveals_the_back(self):
        flip = self.out["afterFlip"]
        self.assertEqual(flip["sideLabel"], "Back")
        self.assertEqual(flip["back"], "Back 1")
        self.assertTrue(flip["flippedClass"])

    def test_navigation_and_progress_wrap_around(self):
        self.assertEqual(self.out["afterTwoNext"]["position"], "3 / 3")
        self.assertEqual(self.out["afterTwoNext"]["sideLabelResetOnNav"], "Front")   # flip resets on navigation
        self.assertEqual(self.out["afterWrapNext"]["position"], "1 / 3")
        self.assertEqual(self.out["afterWrapPrevious"]["position"], "3 / 3")

    def test_shuffle_keeps_a_valid_deck(self):
        self.assertEqual(self.out["afterShuffle"]["position"], "1 / 3")

    def test_manage_cards_still_opens_with_existing_cards_listed(self):
        manager = self.out["manager"]
        self.assertTrue(manager["open"])
        self.assertEqual(manager["rows"], 3)

    def test_regenerate_keeps_the_existing_deck_visible_with_a_small_status_not_the_skeleton(self):
        during = self.out["duringRegenerate"]
        self.assertTrue(during["stageVisible"])
        self.assertTrue(during["frontUnchanged"])   # old deck's current card is still on screen, unchanged
        self.assertEqual(during["position"], "1 / 3")   # untouched: not reset to a loading placeholder
        self.assertFalse(during["fullSkeletonVisible"]); self.assertFalse(during["skeletonCardVisible"])
        self.assertFalse(during["generatePromptVisible"])
        self.assertTrue(during["statusVisible"])
        # The model is already prepared from the earlier initial generation, so this regenerate goes
        # straight to "Regenerating flashcards…" -- still a small, subtle indicator, never the skeleton.
        self.assertIn("regenerat", during["statusText"].lower())
        self.assertTrue(during["regenerateDisabled"])   # no duplicate actions
        self.assertIn("Regenerat", during["regenerateLabel"])   # "Regenerating…"

    def test_successful_regeneration_swaps_in_the_new_deck_reset_to_card_one(self):
        after = self.out["afterRegenerateSuccess"]
        self.assertTrue(after["regeneratePosted"])
        self.assertTrue(after["stageVisible"])
        self.assertEqual(after["front"], "Regenerated 1")   # the new deck's first card, not the old one
        self.assertEqual(after["position"], "1 / 2")   # reset to card 1, new deck length reflected
        self.assertEqual(after["modelNote"], "Generated by Qwen 2.5 7B")
        self.assertEqual(after["regenerateLabel"], "Regenerate")   # button label restored
        self.assertFalse(after["regenerateDisabled"])
        self.assertFalse(after["statusVisible"])   # the subtle status is gone once done

    def test_failed_regeneration_preserves_the_old_deck_with_a_friendly_inline_banner(self):
        failed = self.out["failedRegenerate"]
        self.assertTrue(failed["stageVisible"])   # the deck is never lost on a failed regenerate
        self.assertTrue(failed["frontUnchanged"])
        self.assertFalse(failed["fullErrorVisible"])   # never the full-page error state
        self.assertTrue(failed["bannerVisible"])
        self.assertIn("try again or switch models", failed["bannerMessage"].lower())
        self.assertNotIn("ollama", failed["bannerMessage"].lower())
        self.assertEqual(failed["regenerateLabel"], "Regenerate")   # button restored so Retry/Regenerate works again

    def test_retrying_a_failed_regeneration_recovers_and_clears_the_banner(self):
        after_retry = self.out["afterFailedRegenerateRetry"]
        self.assertTrue(after_retry["stageVisible"])
        self.assertFalse(after_retry["bannerVisible"])
        self.assertEqual(after_retry["front"], "Regenerated 1")

    def test_error_state_shows_friendly_message_with_collapsed_technical_details_and_retry(self):
        error = self.out["error"]
        self.assertTrue(error["errorVisible"]); self.assertFalse(error["stageVisible"])
        self.assertTrue(error["hasRetryButton"]); self.assertTrue(error["retryVisible"])
        self.assertNotIn("Traceback", error["message"]); self.assertNotIn("ollama", error["message"].lower())
        self.assertIn("try again or switch models", error["message"].lower())
        self.assertTrue(error["detailsCollapsed"])
        self.assertTrue(error["technical"])   # still reachable, just not the primary/default view

    def test_retry_recovers_the_deck(self):
        after_retry = self.out["afterRetry"]
        self.assertTrue(after_retry["stageVisible"]); self.assertFalse(after_retry["errorVisible"])
        self.assertEqual(after_retry["cardCount"], 3)


if __name__ == "__main__":
    unittest.main()
