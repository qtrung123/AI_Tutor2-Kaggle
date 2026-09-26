"""Quiz / Summary / Flashcards screens: one model selector, settings-aware "saved" state, nothing
generated until the user asks, generating and partial quizzes stay visible.

The pure and small state functions of frontend/app.js are extracted and executed with Node against
stubbed globals (no browser), so these are behaviour tests, not string checks; a few wiring checks
read the source for things that cannot run without a DOM.
"""

import json
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from frontend_source import FRONTEND, frontend_script_paths, frontend_script_text

# app.js and its classic-script modules (frontend/js/), in index.html load order.
SCRIPT_PATHS = [str(path) for path in frontend_script_paths()]
SOURCE = frontend_script_text()

DRIVER = textwrap.dedent("""
    const fs = require("fs"), vm = require("vm");
    const source = JSON.parse(process.argv[1]).map((path) => fs.readFileSync(path, "utf8")).join("\\n");
    function extract(name) {
      for (const prefix of [`async function ${name}(`, `function ${name}(`]) {
        const start = source.indexOf(prefix);
        if (start < 0) continue;
        let depth = 0;
        for (let i = source.indexOf("{", start); i < source.length; i++) {
          if (source[i] === "{") depth++;
          if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
        }
      }
      throw new Error(`missing function ${name}`);
    }
    const names = JSON.parse(process.argv[2]);
    class El {
      constructor(hidden = false) { this.hidden = hidden; this.children = []; this.disabled = false; this.textContent = ""; }
      set innerHTML(value) { this.children = []; }
      get innerHTML() { return ""; }
    }
    const context = vm.createContext({ El, calls: [], console });
    vm.runInContext(names.map(extract).join("\\n"), context);
    vm.runInContext(process.argv[3], context);          // scenario setup (stubs)
    Promise.resolve(vm.runInContext(`(async () => { ${process.argv[4]} })()`, context))
      .then((result) => process.stdout.write(JSON.stringify(result)))
      .catch((error) => { console.error(error); process.exit(1); });
""")

# ready:true so ensureSelectedModelReady() short-circuits without a network call in scenarios that
# do not care about model preparation - readiness gating itself is covered separately below.
MODELS = 'generationModels = [{id:"qwen-2.5-7b",label:"Qwen 2.5 7B",ready:true},{id:"deepseek-r1-14b",label:"DeepSeek R1 Distill Qwen 14B",ready:true}];'
# Stubs every scenario that reaches ensureSelectedModelReadyWithStatus()/renderModelReadyState()
# needs: a document with a no-op getElementById, and every Generate button renderModelReadyState()
# disables/enables while a model is preparing.
MODEL_READY_DOM_STUBS = """
    var document = { getElementById: () => null };
    var generateQuizButton = new El(), generateSummaryButton = new El(),
        regenerateSummaryButton = new El(true), generateFlashcardsButton = new El(),
        regenerateFlashcardsButton = new El(true);
"""
MODEL_READY_FUNCTIONS = ["ensureSelectedModelReady", "ensureSelectedModelReadyWithStatus", "renderModelReadyState",
                         "renderQuizSheetModelSummary"]


def run_node(functions, setup, scenario):
    result = subprocess.run(
        ["node", "-e", DRIVER, json.dumps(SCRIPT_PATHS), json.dumps(functions), setup, scenario],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


QWEN = {"model_id": "qwen-2.5-7b", "name": "Qwen2.5-7B-Instruct", "quantization": "Q4_K_M"}
DEEPSEEK = {"model_id": "deepseek-r1-14b", "name": "DeepSeek-R1-Distill-Qwen-14B", "quantization": "Q4_K_M"}
QUIZ = {
    "title": "Midterm", "difficulty": "easy", "assessment_scope": "document", "document_id": "lecture.pdf",
    "questions": [{"question_type": "single_choice"}] * 12, "requested_count": 12,
    "assessment_plan": {"type_distribution": {"single_choice": 12}, "requested_count": 12},
}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ModelAndSettingsTests(unittest.TestCase):
    FUNCTIONS = ["quizTypeLabel", "documentQuizTypeBreakdown", "quizPartialSuffix", "modelLabel", "quizModelInfo",
                 "quizModelSuffix", "quizSettingsMismatch", "assessmentTitleText", "variantMatchesSettings"]

    def scenario(self, cases):
        setup = MODELS + """
            var selectedModelId = "qwen-2.5-7b", selectedCount = 12;
            function selectedQuestionCount() { return selectedCount; }
            function selectedQuizGenerationRequest() { return { model_id: selectedModelId, question_count: selectedCount }; }
        """
        body = f"""
            const out = [];
            for (const c of {json.dumps(cases)}) {{
              selectedModelId = c.model; selectedCount = c.count;
              out.push({{ title: assessmentTitleText(c.quiz), notice: quizSettingsMismatch(c.quiz),
                          saved: c.variant ? variantMatchesSettings(c.variant) : null }});
            }}
            return out;
        """
        return run_node(self.FUNCTIONS, setup, body)

    def test_a_quiz_keeps_the_model_that_made_it_when_the_current_model_changes(self):
        qwen_quiz = {**QUIZ, "generation_model": QWEN}
        same, other = self.scenario([
            {"model": "qwen-2.5-7b", "count": 12, "quiz": qwen_quiz},
            {"model": "deepseek-r1-14b", "count": 12, "quiz": qwen_quiz},
        ])
        for result in (same, other):
            self.assertIn("Generated by Qwen 2.5 7B", result["title"])
            self.assertNotIn("DeepSeek", result["title"].split("Current settings")[0])
        self.assertEqual(same["notice"], "")
        self.assertIn("Current settings differ", other["title"])
        self.assertEqual(
            other["notice"],
            "This saved quiz was generated by Qwen 2.5 7B, 12 questions. Your current settings are "
            "DeepSeek R1 Distill Qwen 14B, 12 questions. Use Regenerate Quiz to create a new one with them.",
        )

    def test_a_deepseek_quiz_says_deepseek_and_the_old_label_never_changes_with_the_selector(self):
        quiz = {**QUIZ, "generation_model": DEEPSEEK}
        (as_deepseek, as_qwen) = self.scenario([
            {"model": "deepseek-r1-14b", "count": 12, "quiz": quiz},
            {"model": "qwen-2.5-7b", "count": 12, "quiz": quiz},
        ])
        self.assertIn("Generated by DeepSeek R1 Distill Qwen 14B", as_deepseek["title"])
        self.assertIn("Generated by DeepSeek R1 Distill Qwen 14B", as_qwen["title"])
        self.assertNotIn("Generated by Qwen", as_qwen["title"])

    def test_a_different_number_of_questions_is_also_a_different_setting(self):
        (result,) = self.scenario([{"model": "qwen-2.5-7b", "count": 15, "quiz": {**QUIZ, "generation_model": QWEN}}])
        self.assertIn("12 questions", result["notice"])
        self.assertIn("15 questions", result["notice"])

    def test_an_old_quiz_without_a_recorded_model_shows_no_model(self):
        (result,) = self.scenario([{"model": "deepseek-r1-14b", "count": 12, "quiz": QUIZ}])
        self.assertNotIn("Generated by", result["title"])
        self.assertEqual(result["notice"], "")

    def test_a_partial_quiz_is_shown_like_any_other_with_its_counts(self):
        partial = {**QUIZ, "generation_model": QWEN, "questions": [{"question_type": "single_choice"}] * 9,
                   "assessment_plan": {"partial": True, "requested_count": 12, "actual_count": 9, "type_distribution": {"single_choice": 9}}}
        (result,) = self.scenario([{"model": "qwen-2.5-7b", "count": 12, "quiz": partial}])
        self.assertIn("9 questions", result["title"])
        self.assertIn("9/12 questions generated", result["title"])
        self.assertEqual(result["notice"], "")                     # requested 12 == selected 12: same settings

    def test_saved_means_the_same_model_and_number_of_questions_not_just_any_saved_quiz(self):
        qwen_easy_12 = {"model_id": "qwen-2.5-7b", "model_name": "Qwen2.5-7B-Instruct", "requested_count": 12, "difficulty": "easy"}
        rows = self.scenario([
            {"model": "qwen-2.5-7b", "count": 12, "quiz": QUIZ, "variant": qwen_easy_12},        # Qwen + Easy + 12: saved
            {"model": "deepseek-r1-14b", "count": 12, "quiz": QUIZ, "variant": qwen_easy_12},    # DeepSeek + Easy + 12: not saved
            {"model": "qwen-2.5-7b", "count": 15, "quiz": QUIZ, "variant": qwen_easy_12},        # Qwen + Easy + 15: not saved
            {"model": "deepseek-r1-14b", "count": 12, "quiz": QUIZ,                              # a quiz saved before models were recorded
             "variant": {"model_id": None, "model_name": None, "requested_count": 0, "difficulty": "easy"}},
        ])
        self.assertEqual([row["saved"] for row in rows], [True, False, False, True])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class QuizzesScreenStateTests(unittest.TestCase):
    def state(self, statuses, pending, groups=(), difficulty="all", scope="all"):
        setup = f"""
            var quizStatuses = {json.dumps(statuses)};
            var pendingQuizGenerations = new Map({json.dumps([[i + 1, item] for i, item in enumerate(pending)])});
            var quizDocumentSelect = {{ value: "lecture.pdf" }}, activeDocumentId = "lecture.pdf";
            var quizHistoryDifficultyFilter = {json.dumps(difficulty)}, quizHistoryScopeFilter = {json.dumps(scope)};
        """
        return run_node(["quizListState"], setup, f"return quizListState({json.dumps(list(groups))});")

    VARIANT = {"quiz_id": "q1", "title": "Midterm", "topic_id": "document", "topic_name": "", "difficulty": "easy",
               "question_count": 12, "requested_count": 12, "status": "complete", "model_id": "qwen-2.5-7b"}
    PARTIAL = {**VARIANT, "quiz_id": "q2", "difficulty": "medium", "question_count": 9, "status": "partial"}
    PENDING = {"documentId": "lecture.pdf", "name": "Final", "difficulty": "easy", "topicId": "document", "count": 12, "modelId": "qwen-2.5-7b"}

    def test_a_quiz_that_is_still_generating_is_listed_instead_of_no_completed_quizzes(self):
        result = self.state([{"document_id": "lecture.pdf", "variants": []}], [self.PENDING])
        self.assertEqual(result["emptyMessage"], "")
        self.assertEqual(len(result["visiblePending"]), 1)

    def test_a_pending_quiz_of_another_document_is_not_shown_here(self):
        other = {**self.PENDING, "documentId": "other.pdf"}
        result = self.state([{"document_id": "lecture.pdf", "variants": []}], [other])
        self.assertEqual(result["emptyMessage"], "No quizzes yet. Create one to get started.")

    def test_saved_quizzes_are_listed_even_when_nothing_was_completed_and_partial_ones_too(self):
        result = self.state([{"document_id": "lecture.pdf", "variants": [self.VARIANT, self.PARTIAL]}], [])
        self.assertEqual(result["emptyMessage"], "")
        self.assertEqual([(v["quiz_id"], v["status"], v["question_count"]) for v in result["visibleSaved"]],
                         [("q1", "complete", 12), ("q2", "partial", 9)])

    def test_a_quiz_with_attempts_is_shown_once_as_a_completed_quiz(self):
        group = {"quizId": "q1", "latest": {"difficulty": "easy", "topic_id": "document"}, "attempts": [{}]}
        result = self.state([{"document_id": "lecture.pdf", "variants": [self.VARIANT, self.PARTIAL]}], [], [group])
        self.assertEqual([v["quiz_id"] for v in result["visibleSaved"]], ["q2"])
        self.assertEqual(len(result["visibleGroups"]), 1)

    def test_only_when_there_is_nothing_at_all_the_screen_says_no_quizzes_yet(self):
        self.assertEqual(self.state([], [])["emptyMessage"], "No quizzes yet. Create one to get started.")
        self.assertEqual(self.state([{"document_id": "lecture.pdf", "variants": [self.VARIANT]}], [], difficulty="difficult")["emptyMessage"],
                         "No quizzes match these filters.")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SummaryAndFlashcardsWaitForGenerateTests(unittest.TestCase):
    SUMMARY_FUNCTIONS = ["updateSummaryChrome", "showSummaryState", "loadDocumentSummary"] + MODEL_READY_FUNCTIONS
    SUMMARY_SETUP = MODELS + MODEL_READY_DOM_STUBS + """
        var activeDocumentId = "doc.pdf", selectedModelId = "qwen-2.5-7b", loadedSummaryKey = "", summaryInFlightKey = "";
        var SUMMARY_API_BASE_URL = "/api/summary";
        var summaryContent = new El(), summaryLoading = new El(true), summaryGenerate = new El(true), summaryError = new El(true);
        var regenerateSummaryButton = new El(true), saved = {}, rendered = [];
        function renderDocumentSummary(summary) { summaryContent.children.push(summary); rendered.push(summary.model_id); }
        async function fetchJson(url) { calls.push(url); return url.includes("cache_only=true") ? (saved[selectedModelId] || { status: "not_generated" }) : { final_summary: { overview: "x" }, model_id: selectedModelId }; }
    """

    def test_opening_summary_only_looks_for_a_saved_one_and_shows_the_generate_prompt(self):
        result = run_node(self.SUMMARY_FUNCTIONS, self.SUMMARY_SETUP, """
            await showSummaryState();
            return { calls, prompt: !summaryGenerate.hidden, regenerate: !regenerateSummaryButton.hidden, rendered };
        """)
        self.assertEqual(result["calls"], ["/api/summary/doc.pdf?model_id=qwen-2.5-7b&cache_only=true"])
        self.assertTrue(result["prompt"])
        self.assertFalse(result["regenerate"])
        self.assertEqual(result["rendered"], [])

    def test_generating_starts_only_when_the_user_presses_generate(self):
        result = run_node(self.SUMMARY_FUNCTIONS, self.SUMMARY_SETUP, """
            await showSummaryState();
            const before = calls.length;
            await loadDocumentSummary();                   // what the Generate button calls
            return { before, calls, prompt: !summaryGenerate.hidden, regenerate: !regenerateSummaryButton.hidden, rendered, key: loadedSummaryKey };
        """)
        self.assertEqual(result["before"], 1)
        self.assertEqual(result["calls"][1], "/api/summary/doc.pdf?model_id=qwen-2.5-7b")   # the one generating request
        self.assertFalse(result["prompt"])
        self.assertTrue(result["regenerate"])
        self.assertEqual(result["rendered"], ["qwen-2.5-7b"])
        self.assertEqual(result["key"], "doc.pdf:qwen-2.5-7b")

    def test_a_saved_summary_is_shown_without_generating(self):
        result = run_node(self.SUMMARY_FUNCTIONS, self.SUMMARY_SETUP, """
            saved["qwen-2.5-7b"] = { final_summary: { overview: "kept" }, model_id: "qwen-2.5-7b" };
            await showSummaryState();
            return { calls, prompt: !summaryGenerate.hidden, regenerate: !regenerateSummaryButton.hidden, rendered };
        """)
        self.assertEqual(result["calls"], ["/api/summary/doc.pdf?model_id=qwen-2.5-7b&cache_only=true"])
        self.assertEqual((result["prompt"], result["regenerate"], result["rendered"]), (False, True, ["qwen-2.5-7b"]))

    def test_changing_the_model_shows_the_generate_prompt_for_that_model_and_generates_nothing(self):
        result = run_node(self.SUMMARY_FUNCTIONS, self.SUMMARY_SETUP, """
            saved["qwen-2.5-7b"] = { final_summary: { overview: "qwen" }, model_id: "qwen-2.5-7b" };
            await showSummaryState();
            selectedModelId = "deepseek-r1-14b";           // the Study Session selector changed
            await showSummaryState();
            const afterSwitch = { prompt: !summaryGenerate.hidden, shown: summaryContent.children.length };
            selectedModelId = "qwen-2.5-7b";
            await showSummaryState();                       // back: Qwen's saved summary again
            return { calls, afterSwitch, back: { prompt: !summaryGenerate.hidden, shown: summaryContent.children.length } };
        """)
        self.assertTrue(all("cache_only=true" in url for url in result["calls"]), result["calls"])   # never a generating request
        self.assertIn("model_id=deepseek-r1-14b&cache_only=true", result["calls"][1])
        self.assertEqual(result["afterSwitch"], {"prompt": True, "shown": 0})
        self.assertEqual(result["back"], {"prompt": False, "shown": 1})

    FLASHCARD_FUNCTIONS = ["flashcardsKey", "flashcardsUrl", "applyFlashcardSet", "showFlashcardsState", "loadDocumentFlashcards",
                           "setFlashcardsBusy", "showFlashcardsError", "updateFlashcardsEmptyLanguageNote", "modelLabel",
                           "resetRegenerateButton", "showRegenerateError"] + MODEL_READY_FUNCTIONS
    FLASHCARD_SETUP = MODELS + MODEL_READY_DOM_STUBS + """
        var activeDocumentId = "doc.pdf", selectedModelId = "qwen-2.5-7b", flashcardLanguage = "auto", loadedFlashcardKey = "";
        var flashcardsInFlightKey = "", FLASHCARDS_API_BASE_URL = "/api/flashcards", flashcardsPane = {};
        var flashcardsLoading = new El(true), flashcardsError = new El(true), flashcardsStage = new El(true), flashcardsGenerate = new El(true);
        var flashcardsLoadingLabel = new El(), flashcardsErrorMessage = new El(), flashcardsErrorTechnical = new El(), flashcardsFilterEmpty = new El(true);
        var flashcardsModelNote = new El();
        var flashcardsRegenerateStatus = new El(true), flashcardsRegenerateError = new El(true), flashcardsRegenerateErrorMessage = new El();
        var flashcards = [], flashcardSet = null, flashcardIndex = 0, flashcardFlipped = false, shown = [], saved = {};
        function renderFlashcardTopicFilter() {}
        function renderCurrentFlashcard() { shown.push(flashcards.length); }
        async function fetchJson(url) { calls.push(url); return url.includes("cache_only=true") ? (saved[selectedModelId + ":" + flashcardLanguage] || { status: "not_generated", cards: [] }) : { cards: [{ front: "a" }, { front: "b" }], model_id: selectedModelId }; }
    """

    def test_opening_flashcards_only_looks_for_saved_cards_then_generate_creates_them(self):
        result = run_node(self.FLASHCARD_FUNCTIONS, self.FLASHCARD_SETUP, """
            await showFlashcardsState();
            const opened = { calls: [...calls], prompt: !flashcardsGenerate.hidden, cards: flashcards.length };
            await loadDocumentFlashcards();                 // what the Generate button calls
            return { opened, calls, prompt: !flashcardsGenerate.hidden, cards: flashcards.length, key: loadedFlashcardKey };
        """)
        self.assertEqual(result["opened"], {"calls": ["/api/flashcards/doc.pdf?model_id=qwen-2.5-7b&language=auto&cache_only=true"],
                                            "prompt": True, "cards": 0})
        self.assertEqual(result["calls"][1], "/api/flashcards/doc.pdf?model_id=qwen-2.5-7b&language=auto")
        self.assertEqual((result["prompt"], result["cards"], result["key"]), (False, 2, "doc.pdf:qwen-2.5-7b:auto"))

    def test_changing_model_or_language_never_generates_flashcards(self):
        result = run_node(self.FLASHCARD_FUNCTIONS, self.FLASHCARD_SETUP, """
            saved["qwen-2.5-7b:auto"] = { cards: [{ front: "kept" }], model_id: "qwen-2.5-7b" };   // saved for language=auto only
            await showFlashcardsState();
            selectedModelId = "deepseek-r1-14b"; await showFlashcardsState();
            const deepseek = { prompt: !flashcardsGenerate.hidden, cards: flashcards.length };
            selectedModelId = "qwen-2.5-7b"; flashcardLanguage = "vietnamese"; loadedFlashcardKey = ""; await showFlashcardsState();
            return { calls, deepseek, vietnamese: { prompt: !flashcardsGenerate.hidden, cards: flashcards.length } };
        """)
        self.assertTrue(all("cache_only=true" in url for url in result["calls"]), result["calls"])
        self.assertEqual(result["deepseek"], {"prompt": True, "cards": 0})
        self.assertEqual(result["vietnamese"], {"prompt": True, "cards": 0})   # Qwen's saved set is for language=auto only


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ModelPreparationGatingTests(unittest.TestCase):
    """select model -> if not ready, prepare it -> UI shows Preparing... -> Ready -> only then are
    the Quiz/Summary/Flashcards Generate buttons enabled. The prepare/pull itself is timed and
    reported separately (model_prepare_ms, see backend/model_registry.py) from any generation call -
    these tests only cover the frontend gating, not backend timing."""

    def test_generate_buttons_disable_while_preparing_and_enable_once_ready(self):
        setup = MODEL_READY_DOM_STUBS + """
            generationModels = [{id: "qwen-2.5-7b", label: "Qwen 2.5 7B", ready: false}];
            var selectedModelId = "qwen-2.5-7b", MODELS_API_URL = "/api/models";
            async function fetchJson(url) { calls.push(url); return { status: "ready", ready: true }; }
        """
        result = run_node(MODEL_READY_FUNCTIONS, setup, """
            const promise = ensureSelectedModelReadyWithStatus();
            // Synchronous up to the first await inside ensureSelectedModelReady(): the Preparing
            // state and the button-disable gate are already applied before any network call resolves.
            const duringPrepare = {
                quiz: generateQuizButton.disabled, summary: generateSummaryButton.disabled,
                regenerate: regenerateSummaryButton.disabled, flashcards: generateFlashcardsButton.disabled,
                regenerateFlashcards: regenerateFlashcardsButton.disabled,
            };
            const modelId = await promise;
            const afterReady = {
                quiz: generateQuizButton.disabled, summary: generateSummaryButton.disabled,
                regenerate: regenerateSummaryButton.disabled, flashcards: generateFlashcardsButton.disabled,
                regenerateFlashcards: regenerateFlashcardsButton.disabled,
            };
            return { duringPrepare, afterReady, modelId, ready: generationModels[0].ready, calls };
        """)
        self.assertEqual(result["duringPrepare"], {"quiz": True, "summary": True, "regenerate": True, "flashcards": True, "regenerateFlashcards": True})
        self.assertEqual(result["afterReady"], {"quiz": False, "summary": False, "regenerate": False, "flashcards": False, "regenerateFlashcards": False})
        self.assertEqual(result["modelId"], "qwen-2.5-7b")
        self.assertTrue(result["ready"])   # generationModels' cached ready flag is updated too
        self.assertEqual(result["calls"], ["/api/models/qwen-2.5-7b/prepare"])

    def test_an_already_ready_model_never_disables_the_buttons(self):
        setup = MODEL_READY_DOM_STUBS + """
            generationModels = [{id: "qwen-2.5-7b", label: "Qwen 2.5 7B", ready: true}];
            var selectedModelId = "qwen-2.5-7b", MODELS_API_URL = "/api/models";
            async function fetchJson(url) { calls.push(url); return { status: "ready", ready: true }; }
        """
        result = run_node(MODEL_READY_FUNCTIONS, setup, """
            await ensureSelectedModelReadyWithStatus();
            return { quiz: generateQuizButton.disabled, calls };
        """)
        self.assertFalse(result["quiz"])
        self.assertEqual(result["calls"], [])   # no network call at all: already ready

    def test_a_preparation_failure_re_enables_buttons_and_reports_a_clear_error(self):
        """No silent fallback in the UI either: a failed prepare re-enables the buttons (so the user
        can retry) and the visible state carries the real error message, not a generic one."""
        setup = MODEL_READY_DOM_STUBS + """
            generationModels = [{id: "gemma3-12b", label: "Gemma 3 12B", ready: false}];
            var selectedModelId = "gemma3-12b", MODELS_API_URL = "/api/models";
            async function fetchJson(_url) { throw new Error("Could not prepare model: pull failed"); }
        """
        result = run_node(MODEL_READY_FUNCTIONS, setup, """
            let error = null;
            try { await ensureSelectedModelReadyWithStatus(); } catch (e) { error = e.message; }
            return { error, quizDisabled: generateQuizButton.disabled, ready: generationModels[0].ready };
        """)
        self.assertIn("pull failed", result["error"])
        self.assertFalse(result["quizDisabled"])   # not stuck disabled after a failure: the user can retry
        self.assertFalse(result["ready"])          # never marked ready on failure


class WiringTests(unittest.TestCase):
    @staticmethod
    def body(name):
        start = SOURCE.index(f"function {name}(")
        return SOURCE[start:SOURCE.index("\n}\n", start)]

    def test_there_is_a_single_model_selector_and_it_belongs_to_the_study_session(self):
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertEqual(SOURCE.count('select.id = "generation-model-select"'), 1)             # created once, in the Study Session header
        self.assertIn('document.querySelector(".session-header")', SOURCE)
        self.assertNotIn("<select", "".join(re.findall(r'<[^>]*model[^>]*>', html)))            # no model <select> in the static markup either
        self.assertEqual(re.findall(r'id="([^"]*model[^"]*)"', html), ["admin-model-comparison-nav", "model-comparison-view"])   # (admin page only)
        for extra in ("quiz-model-select", "quizModelSelect", '"Quiz model"', "modelSelects", 'class="generation-model-select"',
                      "generate-model-control"):
            self.assertNotIn(extra, SOURCE)
        # Summary / Flashcards only NAME the model that Generate will use
        self.assertEqual(SOURCE.count('<span class="generate-model-name"></span>'), 2)
        self.assertNotRegex(SOURCE, r"<select[^>]*model")
        self.assertIn('model_id: selectedModelId || "qwen-2.5-7b"', SOURCE)
        self.assertNotIn("qwen3", SOURCE.lower())

    def test_the_create_quiz_form_has_no_topic_and_only_four_fields(self):
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        start = html.index('<div class="assessment-form">')
        form = html[start:html.index("</article>", start)]
        self.assertEqual(re.findall(r"<span>([^<]+)</span>", form), ["Quiz name", "Assessment scope", "Difficulty"])   # + "Number of questions" below
        scope = form.split('id="quiz-scope-select"')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="([^"]+)">', scope), ["document"])
        for topic in ("quiz-topic", "Topic"):
            self.assertNotIn(topic, form)
        self.assertIn('caption.textContent = "Number of questions"', SOURCE)
        self.assertIn("[12, 15, 18, 20].forEach", SOURCE)
        self.assertLess(SOURCE.index('caption.textContent = "Number of questions"'), SOURCE.index("generateQuizButton.before(label)"))
        for removed in ("quizTopicSelect", "quizTopicField", "updateTopicOptions", "updateAssessmentScope", "Choose an extracted topic",
                        "quizHistoryScopeFilter", 'makeFilter("Scope"'):
            self.assertNotIn(removed, SOURCE)
        self.assertIn("topic_id: null,", self.body("selectedQuizGenerationRequest"))
        self.assertIn('return "document";', self.body("selectedTopicId"))

    def test_changing_the_model_never_generates_anything(self):
        body = self.body("setSelectedModel")
        for generating in ("loadDocumentSummary", "loadDocumentFlashcards", "requestGeneratedQuiz", "requestQuizRegeneration"):
            self.assertNotIn(generating, body)
        self.assertIn("showSummaryState()", body)
        self.assertIn("showFlashcardsState()", body)

    def test_tabs_and_language_only_look_for_saved_content(self):
        tab = self.body("setSessionTab")
        self.assertIn("showSummaryState()", tab)
        self.assertIn("showFlashcardsState()", tab)
        self.assertNotIn("loadDocumentSummary", tab)
        self.assertNotIn("loadDocumentFlashcards", tab)
        self.assertIn("generateSummaryButton?.addEventListener(\"click\", () => loadDocumentSummary())", SOURCE)
        self.assertIn("generateFlashcardsButton?.addEventListener(\"click\", () => loadDocumentFlashcards())", SOURCE)
        language = SOURCE[SOURCE.index("flashcardLanguageSelect?.addEventListener"):]
        self.assertIn("showFlashcardsState();", language[:language.index("});")])
        self.assertNotIn("loadDocumentFlashcards", language[:language.index("});")])

    def test_generation_registers_a_pending_quiz_so_it_stays_visible_when_the_user_goes_back(self):
        for name in ("generateAssessmentQuiz", "regenerateAssessmentQuiz"):
            body = self.body(name) if name == "generateAssessmentQuiz" else SOURCE[SOURCE.index(f"async function {name}("):SOURCE.index("async function deleteAssessmentQuiz")]
            self.assertIn("registerPendingQuiz(", body)
            self.assertIn("finishPendingQuiz(pendingId)", body)
        self.assertNotIn("No completed quizzes yet", SOURCE)
        self.assertIn('if (tab === "quiz") renderQuizHistory();', SOURCE)
        self.assertIn("createPendingQuizCard", self.body("renderQuizHistory"))
        self.assertIn("createSavedQuizCard", self.body("renderQuizHistory"))

    def test_the_old_progress_ui_is_gone_but_the_new_progress_tab_remains(self):
        for removed in ("View Progress", "Progress &amp; Mastery", "quiz-progress-drawer", "Mastery evidence is not available"):
            self.assertNotIn(removed, SOURCE)
        self.assertNotIn("practice-mastery", (FRONTEND / "index.html").read_text(encoding="utf-8"))
        self.assertIn('data-session-pane="progress"', (FRONTEND / "index.html").read_text(encoding="utf-8"))
        self.assertIn("renderSessionProgress", SOURCE)


if __name__ == "__main__":
    unittest.main()
