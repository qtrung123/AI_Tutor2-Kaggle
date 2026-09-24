const pageTitles = {
  overview: "Study sessions",
  session: "Study Session",
  planner: "Study Planner",
  "model-comparison": "Model Comparison"
};

const API_BASE_URL = (
  window.APP_CONFIG?.API_BASE_URL ||
  localStorage.getItem("API_BASE_URL") ||
  ""
).replace(/\/$/, "");
const apiUrl = (path) => `${API_BASE_URL}${path}`;
const originalFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => originalFetch(input, { credentials: "include", ...init });
const AUTH_ME_API_URL = apiUrl("/api/auth/me");
const AUTH_LOGIN_API_URL = apiUrl("/api/auth/login");
const AUTH_SIGNUP_API_URL = apiUrl("/api/auth/signup");
const AUTH_LOGOUT_API_URL = apiUrl("/api/auth/logout");
const CONVERSATIONS_API_URL = apiUrl("/api/conversations");
const SOURCES_API_URL = apiUrl("/api/sources");
const UPLOAD_API_URL = apiUrl("/api/sources/upload");
const DELETE_SOURCE_API_URL = apiUrl("/api/sources");
const DOCUMENTS_API_URL = apiUrl("/api/documents");
const QUIZZES_API_URL = apiUrl("/api/quizzes");
const QUIZ_API_BASE_URL = apiUrl("/api/quiz");
const QUIZ_GENERATE_API_URL = apiUrl("/api/quiz/generate");
const QUIZ_HISTORY_API_URL = apiUrl("/api/quiz-history");
const DASHBOARD_API_URL = apiUrl("/api/dashboard");
const KNOWLEDGE_GAPS_API_URL = apiUrl("/api/knowledge-gaps");
const RECOMMENDATIONS_API_URL = apiUrl("/api/recommendations");
const MODELS_API_URL = apiUrl("/api/models");
const SUMMARY_API_BASE_URL = apiUrl("/api/summary");
const FLASHCARDS_API_BASE_URL = apiUrl("/api/flashcards");
const PLANNER_AVAILABILITY_API_URL = apiUrl("/api/planner/availability");
const PLANNER_PLANS_API_URL = apiUrl("/api/planner/plans");
const PLANNER_SESSIONS_API_URL = apiUrl("/api/planner/sessions");
const ADMIN_QUIZ_MODEL_COMPARISON_API_URL = apiUrl("/api/admin/quiz-model-comparison");

const initialState = {
  page: "overview",
  confidence: 64,
  quizIndex: 0,
  quizScore: 0,
  answered: false
};

const state = { ...initialState };
let uploadedSources = [];
let indexedDocuments = [];
let quizStatuses = [];
let currentQuiz = null;
let quizAnswers = {};
let currentAttempt = null;
let quizAttemptSummary = null;
let quizExplanations = {};
let quizHistory = [];
// Quizzes whose generation request is still running (the request outlives the screen it was started on).
const pendingQuizGenerations = new Map();
let pendingQuizSequence = 0;
let quizHistoryDifficultyFilter = "all";
let quizQuestionIndex = 0;
let quizExplanationPending = false;
// True only while the focused Quiz Player (Start/Resume from the Library) is open -- gates the
// renderAssessmentQuiz() dispatcher and hides the Library/Create UI (see CSS .quiz-player-open).
let quizPlayerOpen = false;
let quizAutosaveTimer = null;
let quizAutosaveSeq = 0;
let quizAutosaveDirty = false;
let quizAutosaveInFlight = null;
let quizDetachedSave = null;
// Results/Review inside the focused player. Non-null while a completed attempt is shown; it
// takes precedence over question-taking in renderQuizPlayer, so an unrelated re-render never
// swaps Results for the Loading/question view.
let quizResultState = null;   // { loading } | { result, view: "results" | "review", reviewIndex }
let quizResultRequestSeq = 0;
// Study Session Overview: read-only statuses of this session's saved artifacts for the selected
// model (never generates anything -- cache_only reads, same as the Summary/Flashcards tabs).
let sessionOverviewStatus = { key: "", summary: "loading", flashcards: "loading", flashcardCount: 0 };   // the latest detached (session-switch) save, for tests/diagnostics
// True only between a Finish Quiz click and its outcome: blocks a duplicate submission, matching
// the quizGenerationInFlight guard used by the Create Quiz sheet.
let quizSubmitInFlight = false;
let conversations = [];
let dashboardData = null;
let knowledgeGaps = [];
let recommendations = [];
let currentUser = null;
let authMode = "login";
let activeConversation = null;
let generationModels = [];
let selectedModelId = localStorage.getItem("aiTutorModelId") || "";
// Visible "Preparing…/Ready/error" state for the selected model, shared by the selector and every
// generation surface (Quiz, Summary, Flashcards): "unknown" | "preparing" | "ready" | "error".
let modelReadyState = "unknown";
let modelReadyMessage = "";
let activeDocumentId = "";
let loadedSummaryKey = "";
let summaryInFlightKey = "";
let flashcardsInFlightKey = "";
let flashcardSet = null;
let flashcards = [];
let flashcardIndex = 0;
let flashcardFlipped = false;
let flashcardTopicFilter = "all";
let loadedFlashcardKey = "";
let flashcardLanguage = localStorage.getItem("aiTutorFlashcardLanguage") || "auto";

let plannerAvailability = [];
let plannerMode = "available";
let plannerWeekStart = plannerMondayOf(new Date());
let plannerDrag = null;
// Study Planner v2 (document-centric): one active plan at a time.
let plannerPlan = null;          // the plan being built, or already confirmed
let plannerMaterials = [];       // its documents (with optional deadlines)
let plannerSessions = [];        // its saved sessions (the confirmed plan)
let plannerPlanWeekStart = null; // week shown on the saved plan; null = pick a sensible default
let plannerHistorySessions = []; // its completed/skipped sessions (shown on the week, never re-planned)
let plannerPreview = null;       // the latest read-only preview result
let plannerStep = "materials";   // materials | availability | preview | plan
let plannerBusy = false;         // a preview/confirm request is in flight (no double submit)

const navItems = document.querySelectorAll(".nav-item");
const views = document.querySelectorAll(".view");
const pageTitle = document.getElementById("page-title");
const resetButton = document.getElementById("reset-button");
const toast = document.getElementById("toast");
const messageList = document.getElementById("message-list");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
chatInput.placeholder = "Ask AI assistant...";
const confidenceLabel = document.getElementById("confidence-label");
const confidenceBar = document.getElementById("confidence-bar");
const confidencePill = document.getElementById("confidence-pill");
const sourceList = document.getElementById("source-list");
const sourceFileInput = document.getElementById("source-file-input");
const uploadSourceButton = document.getElementById("upload-source-button");
const uploadStatus = document.getElementById("upload-status");
// Create Quiz sheet: replaces the old always-editable inline form's markup with an iOS-style sheet
// layout (segmented Difficulty/Question-count controls, a read-only Model summary, and in-sheet
// Generating/Error states). Element ids are unchanged on purpose -- every id-based binding below,
// and every other function that reads these controls, keeps working against the new markup.
(() => {
  const panel = document.querySelector('[data-session-pane="quiz"] .assessment-control');
  if (!panel) return;
  panel.innerHTML = `<div class="assessment-form quiz-sheet-form">
    <label class="quiz-sheet-field quiz-sheet-field--name"><span>Quiz name</span><input id="quiz-name-input" type="text" maxlength="200" placeholder="e.g. Midterm Embedded Systems"></label>
    <label class="quiz-sheet-field quiz-sheet-field--scope"><span>Scope</span><select id="quiz-scope-select" class="quiz-sheet-scope-select" disabled><option value="document" selected>Entire document</option></select></label>
    <label class="quiz-sheet-field quiz-sheet-field--difficulty"><span>Difficulty</span><select id="quiz-difficulty-select" class="visually-hidden"><option value="easy">Easy</option><option value="medium">Medium</option><option value="difficult">Difficult</option></select><div class="quiz-segmented" id="quiz-difficulty-segmented" role="radiogroup" aria-label="Difficulty"></div></label>
    <select id="quiz-document-select" class="visually-hidden"></select>
    <div class="quiz-sheet-model-summary"><div class="quiz-sheet-model-row"><span class="quiz-sheet-model-label">Model</span><span class="generate-model-name quiz-sheet-model-name"></span><span class="quiz-sheet-model-badge" id="quiz-sheet-model-badge" hidden></span></div><p class="quiz-sheet-model-hint">Change the model using the Study Session selector above.</p></div>
    <div class="quiz-sheet-generating" id="quiz-sheet-generating" hidden><span class="quiz-sheet-spinner" aria-hidden="true"></span><p>Generating quiz…</p></div>
    <div class="quiz-sheet-error" id="quiz-sheet-error" hidden><p id="quiz-sheet-error-message"></p><button class="secondary-button" id="quiz-sheet-retry-button" type="button">Retry</button><details class="quiz-sheet-error-details"><summary>Technical details</summary><p id="quiz-sheet-error-technical"></p></details></div>
    <button class="primary-button" id="generate-quiz-button" type="button">Generate Quiz</button>
  </div>`;
})();

// Quiz Player: a focused, iOS-inspired overlay for taking a quiz, entirely separate from the
// Quiz Library/Create Quiz sheet markup above. It is injected once here and toggled purely via the
// .quiz-player-open class on the Quiz tab's session-pane (see CSS): when open, that class hides
// .assessment-shell (Library, history, the old inline quiz UI) and shows only this element.
(() => {
  const pane = document.querySelector('[data-session-pane="quiz"]');
  if (!pane || document.getElementById("quiz-player")) return;
  pane.insertAdjacentHTML("beforeend", `
    <div class="quiz-player" id="quiz-player" hidden>
      <div class="quiz-player-question-view" id="quiz-player-question-view">
        <header class="quiz-player-header">
          <button class="quiz-player-exit" id="quiz-player-exit" type="button">← Exit Quiz</button>
          <div class="quiz-player-heading">
            <h2 id="quiz-player-title"></h2>
            <div class="quiz-player-meta">
              <span class="quiz-player-badge" id="quiz-player-difficulty"></span>
              <span class="quiz-player-model" id="quiz-player-model" hidden></span>
            </div>
          </div>
        </header>
        <div class="quiz-player-progress">
          <div class="quiz-player-progress-row">
            <span id="quiz-player-position"></span>
            <span id="quiz-player-answered-count"></span>
          </div>
          <div class="quiz-player-progress-track"><span id="quiz-player-progress-bar"></span></div>
          <span class="quiz-player-save-status" id="quiz-player-save-status" hidden></span>
        </div>
        <article class="quiz-player-card" id="quiz-player-card">
          <p class="quiz-player-question" id="quiz-player-question"></p>
          <div class="quiz-player-answers" id="quiz-player-answers"></div>
        </article>
        <footer class="quiz-player-footer">
          <button class="secondary-button quiz-player-prev" id="quiz-player-previous" type="button">Previous</button>
          <button class="primary-button quiz-player-next" id="quiz-player-next" type="button">Next</button>
        </footer>
      </div>
      <div class="quiz-player-loading" id="quiz-results-loading" hidden>Loading results…</div>
      <div class="quiz-results-view" id="quiz-results-view" hidden>
        <header class="quiz-results-header">
          <span class="quiz-results-eyebrow">Quiz completed</span>
          <h2 id="quiz-results-title"></h2>
          <div class="quiz-results-meta">
            <span class="quiz-player-badge" id="quiz-results-difficulty"></span>
            <span class="quiz-player-model" id="quiz-results-model" hidden></span>
          </div>
        </header>
        <section class="quiz-results-score-card" aria-label="Score">
          <div class="quiz-results-score">
            <strong id="quiz-results-score"></strong>
            <span id="quiz-results-percentage"></span>
          </div>
          <div class="quiz-results-stats">
            <div class="quiz-results-stat is-correct"><strong id="quiz-results-correct"></strong><span>Correct</span></div>
            <div class="quiz-results-stat is-incorrect"><strong id="quiz-results-incorrect"></strong><span>Incorrect</span></div>
            <div class="quiz-results-stat is-unanswered"><strong id="quiz-results-unanswered"></strong><span>Unanswered</span></div>
          </div>
          <p class="quiz-results-note" id="quiz-results-note" hidden></p>
        </section>
        <div class="quiz-results-actions">
          <button class="primary-button" id="quiz-results-review" type="button">Review Answers</button>
          <button class="secondary-button" id="quiz-results-back" type="button">Back to Quizzes</button>
        </div>
      </div>
      <div class="quiz-review-view" id="quiz-review-view" hidden>
        <header class="quiz-player-header">
          <button class="quiz-player-exit" id="quiz-review-back-results" type="button">← Back to Results</button>
          <div class="quiz-player-heading"><h2 id="quiz-review-title"></h2></div>
        </header>
        <div class="quiz-player-progress">
          <div class="quiz-player-progress-row">
            <span id="quiz-review-position"></span>
            <span class="quiz-review-status" id="quiz-review-status"></span>
          </div>
          <div class="quiz-player-progress-track"><span id="quiz-review-progress-bar"></span></div>
        </div>
        <article class="quiz-player-card quiz-review-card">
          <p class="quiz-player-question" id="quiz-review-question"></p>
          <p class="quiz-review-unanswered-note" id="quiz-review-unanswered-note" hidden>You didn't answer this question.</p>
          <div class="quiz-review-options" id="quiz-review-options"></div>
          <section class="quiz-review-section">
            <h4>Explanation</h4>
            <p id="quiz-review-explanation"></p>
          </section>
          <section class="quiz-review-section" id="quiz-review-source" hidden>
            <h4>Source</h4>
            <p id="quiz-review-source-text"></p>
          </section>
        </article>
        <footer class="quiz-player-footer">
          <button class="secondary-button" id="quiz-review-previous" type="button">Previous</button>
          <button class="primary-button" id="quiz-review-next" type="button">Next</button>
        </footer>
      </div>
    </div>
    <div class="quiz-player-modal-backdrop" id="quiz-exit-confirm" hidden>
      <div class="quiz-player-modal">
        <h3 id="quiz-exit-confirm-title">Your progress is saved</h3>
        <p id="quiz-exit-confirm-detail"></p>
        <div class="quiz-player-modal-actions">
          <button class="text-button" id="quiz-exit-confirm-continue" type="button">Continue Quiz</button>
          <button class="primary-button" id="quiz-exit-confirm-exit" type="button">Exit</button>
        </div>
      </div>
    </div>
    <div class="quiz-player-modal-backdrop" id="quiz-finish-confirm" hidden>
      <div class="quiz-player-modal">
        <h3 id="quiz-finish-confirm-title"></h3>
        <p>You can review them before submitting.</p>
        <div class="quiz-player-modal-actions">
          <button class="text-button" id="quiz-finish-review" type="button">Review Unanswered</button>
          <button class="primary-button" id="quiz-finish-submit-anyway" type="button">Submit Anyway</button>
        </div>
      </div>
    </div>
  `);
})();

const quizNameInput = document.getElementById("quiz-name-input");
const quizDocumentSelect = document.getElementById("quiz-document-select");
const quizScopeSelect = document.getElementById("quiz-scope-select");
const quizDifficultySelect = document.getElementById("quiz-difficulty-select");
let quizQuestionCountSelect = document.getElementById("quiz-question-count-select");
let quizQuestionCountField = document.getElementById("quiz-question-count-field");
const generateQuizButton = document.getElementById("generate-quiz-button");
const resetQuizButton = document.getElementById("reset-quiz-button");
resetQuizButton.textContent = "Retake Quiz";
const newQuizButton = document.getElementById("new-quiz-button");
const deleteQuizButton = document.getElementById("delete-quiz-button");
const reviewQuizButton = document.createElement("button");
reviewQuizButton.className = "text-button";
reviewQuizButton.type = "button";
reviewQuizButton.textContent = "Review Answers";
reviewQuizButton.hidden = true;
resetQuizButton.before(reviewQuizButton);
const backToQuizzesButton = document.createElement("button");
backToQuizzesButton.className = "text-button quiz-back-button";
backToQuizzesButton.type = "button";
backToQuizzesButton.textContent = "← Back to Quizzes";
reviewQuizButton.before(backToQuizzesButton);
const quizProgressLabel = document.getElementById("quiz-progress-label");
const quizAccuracyLabel = document.getElementById("quiz-accuracy-label");
const quizProgressBar = document.getElementById("quiz-progress-bar");
const assessmentLoading = document.getElementById("assessment-loading");
const quizList = document.getElementById("quiz-list");
const assessmentTitle = document.getElementById("assessment-title");
const quizHistoryList = document.getElementById("quiz-history-list");
const quizHistoryDetail = document.getElementById("quiz-history-detail");
const refreshQuizHistoryButton = document.getElementById("refresh-quiz-history-button");
const quizPopoverPanels = document.querySelectorAll(".collapsible-panel");
const conversationList = document.getElementById("conversation-list");
const newConversationButton = document.getElementById("new-conversation-button");
const conversationSourceList = document.getElementById("conversation-source-list");
const applyConversationSourcesButton = document.getElementById("apply-conversation-sources-button");
const chatConversationTitle = document.getElementById("chat-conversation-title");
const chatSourceSummary = document.getElementById("chat-source-summary");
const tutorLayout = document.getElementById("persistent-tutor");
const toggleConversationSourcesButton = document.getElementById("toggle-conversation-sources-button");
const closeConversationSourcesButton = document.getElementById("close-conversation-sources-button");
const sourcesDrawerBackdrop = document.getElementById("sources-drawer-backdrop");
const conversationSourcesPanel = document.getElementById("conversation-sources-panel");
// Keep the source control in the Tutor footer while retaining the existing IDs/handlers.
tutorLayout.insertBefore(toggleConversationSourcesButton, conversationSourcesPanel);
setSourcesDrawerOpen(false);
const overviewKpis = document.getElementById("overview-kpis");
const overviewMaterialsList = document.getElementById("overview-materials-list");
const sessionDocumentName = document.getElementById("session-document-name");
const sessionDocumentStatus = document.getElementById("session-document-status");
const sessionMasteryList = document.getElementById("session-mastery-list");
const sessionCoverageList = document.getElementById("session-coverage-list");
const sessionProgressState = document.getElementById("session-progress-state");
const sessionProgressQuiz = document.getElementById("session-progress-quiz");
const sessionProgressPlan = document.getElementById("session-progress-plan");
const PROGRESS_API_BASE_URL = apiUrl("/api/progress/documents");
const sessionKnowledgeGapsList = document.getElementById("session-knowledge-gaps-list");
const sessionRecommendationsList = document.getElementById("session-recommendations-list");
const assessmentControl = document.querySelector('[data-session-pane="quiz"] .assessment-control');
let quizCreateDialog = null;
// True only between a Generate Quiz click and its outcome: blocks a duplicate submission and
// blocks Cancel/backdrop/Escape from dismissing the sheet mid-generation.
let quizGenerationInFlight = false;
const originalContentFrame = document.getElementById("original-content-frame");
const originalContentFileName = document.getElementById("original-content-file-name");
const originalContentOpen = document.getElementById("original-content-open");
const originalContentEmpty = document.getElementById("original-content-empty");
const summaryPane = document.querySelector('[data-session-pane="summary"]');
summaryPane?.classList.remove("placeholder-pane");
if (summaryPane) summaryPane.innerHTML = `<article class="panel summary-panel"><div class="summary-heading"><div><p class="eyebrow">Summary</p><h2>Document Overview</h2></div><button class="secondary-button" id="regenerate-summary-button" type="button">Regenerate Summary</button></div><div id="summary-generate" class="generate-prompt" hidden><h3>Generate a summary</h3><p>No summary has been generated for this document with the selected model yet.</p><p class="generate-model-note">Model: <span class="generate-model-name"></span> (change it in the Model selector above)</p><button class="primary-button" id="generate-summary-button" type="button">Generate Summary</button></div><div id="summary-loading" class="empty-state" hidden>Generating a grounded summary…</div><div id="summary-error" class="empty-state" hidden></div><div id="summary-content"></div></article>`;
const summaryLoading = document.getElementById("summary-loading");
const summaryGenerate = document.getElementById("summary-generate");
const generateSummaryButton = document.getElementById("generate-summary-button");
const summaryError = document.getElementById("summary-error");
const summaryContent = document.getElementById("summary-content");
const regenerateSummaryButton = document.getElementById("regenerate-summary-button");
const flashcardsPane = document.querySelector('[data-session-pane="flashcards"]');
flashcardsPane?.classList.remove("placeholder-pane");
if (flashcardsPane) flashcardsPane.innerHTML = `<article class="panel flashcards-panel"><div class="flashcards-toolbar"><div><p class="eyebrow">Flashcards</p><h2>Study Cards</h2></div><div class="flashcards-actions"><label>Filter Topics<select id="flashcard-topic-filter"><option value="all">All topics</option></select></label><label>Flashcard language<select id="flashcard-language-select"><option value="auto">Auto</option><option value="english">English</option><option value="vietnamese">Vietnamese</option></select></label><button class="secondary-button" id="shuffle-flashcards" type="button">Shuffle</button><button class="secondary-button" id="regenerate-flashcards-button" type="button" hidden>Regenerate</button><button class="secondary-button" id="manage-flashcards" type="button">Manage Cards</button></div></div><div id="flashcards-generate" class="generate-prompt flashcards-empty-state" hidden><div class="flashcards-empty-icon" aria-hidden="true">🗂️</div><h3>No flashcards yet</h3><p>Generate grounded study cards for this document with the selected model and language.</p><p class="generate-model-note">Model: <span class="generate-model-name"></span> (change it in the Model selector above)</p><p class="flashcards-language-note">Language: <span id="flashcards-empty-language"></span></p><button class="primary-button" id="generate-flashcards-button" type="button">Generate Flashcards</button></div><div id="flashcards-loading" class="empty-state flashcards-loading-state" hidden><div class="flashcards-skeleton-card" aria-hidden="true"><span class="flashcards-skeleton-line long"></span><span class="flashcards-skeleton-line short"></span></div><p class="flashcards-loading-label" id="flashcards-loading-label">Preparing model…</p></div><div id="flashcards-error" class="empty-state flashcards-error-state" hidden><div class="flashcards-error-icon" aria-hidden="true">!</div><h3>Couldn't load flashcards</h3><p id="flashcards-error-message"></p><button class="primary-button" id="retry-flashcards-button" type="button">Retry</button><details class="flashcards-error-details"><summary>Technical details</summary><p id="flashcards-error-technical"></p></details></div><div id="flashcards-filter-empty" class="flashcards-filter-empty" hidden>No cards are available for this topic.</div><div id="flashcards-stage" hidden><div class="flashcards-regenerate-status" id="flashcards-regenerate-status" hidden>Regenerating…</div><div class="flashcards-regenerate-error" id="flashcards-regenerate-error" hidden><span id="flashcards-regenerate-error-message"></span><button class="text-button" id="retry-regenerate-flashcards-button" type="button">Retry</button></div><div class="flashcards-stage-meta"><span class="flashcard-topic-title" id="flashcard-topic-title"></span><span class="flashcards-model-note" id="flashcards-model-note"></span></div><button class="flashcard" id="flashcard" type="button" aria-label="Flip flashcard"><span class="flashcard-side-label" id="flashcard-side-label">Front</span><span class="flashcard-copy" id="flashcard-copy"></span><span class="flashcard-flip-hint">Tap to flip</span></button><div class="flashcard-progress-track"><span class="flashcard-progress-bar" id="flashcard-progress-bar"></span></div><div class="flashcard-navigation"><button class="secondary-button" id="previous-flashcard" type="button">Previous</button><span id="flashcard-position">0 / 0</span><button class="secondary-button" id="next-flashcard" type="button">Next</button><button class="favorite-button" id="favorite-flashcard" type="button" aria-label="Favorite card">☆</button></div></div></article>`;
const flashcardsLoading = document.getElementById("flashcards-loading");
const flashcardsLoadingLabel = document.getElementById("flashcards-loading-label");
const flashcardsGenerate = document.getElementById("flashcards-generate");
const generateFlashcardsButton = document.getElementById("generate-flashcards-button");
const regenerateFlashcardsButton = document.getElementById("regenerate-flashcards-button");
const flashcardsError = document.getElementById("flashcards-error");
const flashcardsErrorMessage = document.getElementById("flashcards-error-message");
const flashcardsErrorTechnical = document.getElementById("flashcards-error-technical");
const retryFlashcardsButton = document.getElementById("retry-flashcards-button");
const flashcardsFilterEmpty = document.getElementById("flashcards-filter-empty");
const flashcardsStage = document.getElementById("flashcards-stage");
const flashcardsModelNote = document.getElementById("flashcards-model-note");
const flashcardsRegenerateStatus = document.getElementById("flashcards-regenerate-status");
const flashcardsRegenerateError = document.getElementById("flashcards-regenerate-error");
const flashcardsRegenerateErrorMessage = document.getElementById("flashcards-regenerate-error-message");
const retryRegenerateFlashcardsButton = document.getElementById("retry-regenerate-flashcards-button");
const flashcardTopicSelect = document.getElementById("flashcard-topic-filter");
const flashcardLanguageSelect = document.getElementById("flashcard-language-select");
if (flashcardLanguageSelect) flashcardLanguageSelect.value = flashcardLanguage;
const flashcardElement = document.getElementById("flashcard");
const flashcardCopy = document.getElementById("flashcard-copy");
const flashcardSideLabel = document.getElementById("flashcard-side-label");
const flashcardTopicTitle = document.getElementById("flashcard-topic-title");
const flashcardPosition = document.getElementById("flashcard-position");
const flashcardProgressBar = document.getElementById("flashcard-progress-bar");
const favoriteFlashcardButton = document.getElementById("favorite-flashcard");
let flashcardManager = null;

const FLASHCARD_LANGUAGE_LABELS = { auto: "Auto", english: "English", vietnamese: "Vietnamese" };

const plannerView = document.getElementById("planner-view");
if (plannerView) {
  plannerView.innerHTML = `<div class="planner-shell">
  <div class="planner-toolbar"><div><p class="eyebrow">Study Planner</p><h2>Plan your study time</h2><p class="planner-lead">Choose documents, add a deadline if there is one, and mark when you could study. We’ll suggest a balanced plan.</p></div></div>
  <ol class="planner-steps" aria-label="Planner steps"><li data-step-indicator="materials"><span>1</span>Materials</li><li data-step-indicator="availability"><span>2</span>Availability</li><li data-step-indicator="preview"><span>3</span>Preview</li><li data-step-indicator="plan"><span>4</span>Plan</li></ol>
  <section class="panel planner-step" data-planner-step="materials">
    <div class="planner-step-heading"><p class="eyebrow">Step 1</p><h3>What do you want to study?</h3><p class="planner-hint">Pick documents and, if there is one, a deadline for each.</p></div>
    <div id="planner-material-list" class="planner-material-list"></div>
    <p id="planner-material-empty" class="empty-state" hidden>Upload a document first, then come back to plan study time for it.</p>
    <div class="planner-step-actions"><button class="primary-button" id="planner-to-availability" type="button">Next: availability</button></div>
  </section>
  <section class="panel planner-step" data-planner-step="availability" hidden>
    <div class="planner-step-heading"><p class="eyebrow">Step 2</p><h3>When could you study?</h3><p class="planner-hint">Select when you could study. We won’t necessarily fill all of this time.</p></div>
    <div class="planner-calendar-toolbar"><div class="planner-week-nav"><button class="secondary-button" id="planner-prev-week" type="button" aria-label="Previous week">‹</button><strong id="planner-week-label">This week</strong><button class="secondary-button" id="planner-next-week" type="button" aria-label="Next week">›</button></div><div class="planner-calendar-actions"><label class="planner-repeat-toggle"><input type="checkbox" id="planner-repeat-weekly" checked>Repeat weekly</label><div class="planner-mode-toggle"><button class="secondary-button active" id="planner-mode-available" type="button">Mark available</button><button class="secondary-button" id="planner-mode-erase" type="button">Erase</button></div></div></div>
    <div id="planner-calendar" class="planner-calendar"></div>
    <div class="planner-legend"><span class="planner-legend-item"><i class="planner-swatch planner-swatch-available"></i>Available</span><span class="planner-legend-item"><i class="planner-swatch planner-swatch-confirmed"></i>Planned session</span></div>
    <div class="planner-step-actions"><button class="text-button" data-planner-go="materials" type="button">Back</button><button class="primary-button" id="planner-generate-preview" type="button">Generate preview</button></div>
  </section>
  <section class="panel planner-step" data-planner-step="preview" hidden>
    <div class="planner-step-heading"><p class="eyebrow">Step 3</p><h3>Suggested plan</h3><p class="planner-hint">Nothing is saved until you confirm.</p></div>
    <div id="planner-preview-error" class="planner-alert" role="alert" hidden></div>
    <div id="planner-preview-capacity"></div>
    <div id="planner-preview-sessions" class="planner-day-list"></div>
    <div class="planner-step-actions"><button class="text-button" data-planner-go="materials" type="button">Adjust deadlines</button><button class="secondary-button" data-planner-go="availability" type="button">Adjust availability</button><button class="primary-button" id="planner-confirm-button" type="button">Looks good, confirm</button></div>
  </section>
  <section class="panel planner-step" data-planner-step="plan" hidden>
    <div class="planner-step-heading"><p class="eyebrow">Your plan</p><h3>Saved study sessions</h3><p class="planner-hint" id="planner-plan-summary"></p><p class="planner-hint planner-plan-progress" id="planner-plan-progress" hidden></p></div>
    <div class="planner-calendar-toolbar"><div class="planner-week-nav"><button class="secondary-button" id="planner-plan-prev-week" type="button" aria-label="Previous week">‹</button><strong id="planner-plan-week-label">This week</strong><button class="secondary-button" id="planner-plan-next-week" type="button" aria-label="Next week">›</button></div></div>
    <div id="planner-plan-sessions" class="planner-day-list"></div>
    <div class="planner-step-actions"><button class="secondary-button" id="planner-new-plan" type="button">Start a new plan</button></div>
  </section>
</div>
<div class="pcal" id="planner-workspace" hidden>
  <aside class="pcal-rail pcal-materials" aria-labelledby="pcal-materials-title">
    <h3 class="pcal-rail-title" id="pcal-materials-title">Materials</h3>
    <ul id="pcal-material-list" class="pcal-material-list"></ul>
    <button class="pcal-add-button" id="pcal-add-materials" type="button" aria-haspopup="dialog">+ Add materials</button>
    <button class="text-button pcal-new-plan" id="pcal-new-plan" type="button" hidden>Start a new plan</button>
  </aside>
  <section class="pcal-main" aria-label="Study calendar">
    <div class="pcal-toolbar">
      <div class="pcal-toolbar-row">
        <button class="pcal-button" id="pcal-today" type="button">Today</button>
        <div class="pcal-nav"><button class="pcal-icon-button" id="pcal-prev" type="button" aria-label="Previous week">‹</button><button class="pcal-icon-button" id="pcal-next" type="button" aria-label="Next week">›</button></div>
        <h2 class="pcal-range" id="pcal-range"></h2>
        <span class="pcal-view-pill">Week</span>
        <div class="pcal-actions"><button class="pcal-button pcal-secondary" id="pcal-auto-plan" type="button">Auto Plan</button><button class="primary-button pcal-accept" id="pcal-accept" type="button">Accept plan</button></div>
      </div>
      <div class="pcal-status-line"><span class="pcal-status" id="pcal-status" aria-live="polite"></span><button class="pcal-explain" id="pcal-explain" type="button" aria-haspopup="dialog" hidden>How this plan was built</button></div>
      <div class="pcal-review" id="pcal-review" role="region" aria-label="Suggested schedule changes" hidden></div>
    </div>
    <div class="pcal-notice" id="pcal-notice" role="status" hidden></div>
    <div class="pcal-scroll" id="pcal-scroll">
      <div class="pcal-head" id="pcal-head"></div>
      <div class="pcal-body" id="pcal-body"></div>
    </div>
  </section>
  <aside class="pcal-rail pcal-queue" aria-labelledby="pcal-queue-title">
    <div class="pcal-queue-head"><h3 class="pcal-rail-title" id="pcal-queue-title">Study queue</h3><span class="pcal-queue-count" id="pcal-queue-count"></span></div>
    <ol class="pcal-queue-list" id="pcal-queue"></ol>
  </aside>
  <section class="pcal-sheet" id="pcal-sheet" role="dialog" aria-labelledby="pcal-sheet-title" hidden>
    <div class="pcal-sheet-head"><h3 id="pcal-sheet-title">Add materials</h3><button class="pcal-icon-button" id="pcal-sheet-close" type="button" aria-label="Close">×</button></div>
    <ul class="pcal-sheet-list" id="pcal-sheet-list"></ul>
  </section>
  <div class="pcal-popover" id="pcal-popover" role="dialog" hidden></div>
</div>`;
}
const plannerMaterialList = document.getElementById("planner-material-list");
const plannerMaterialEmpty = document.getElementById("planner-material-empty");
const plannerToAvailabilityButton = document.getElementById("planner-to-availability");
const plannerWeekLabel = document.getElementById("planner-week-label");
const plannerPrevWeekButton = document.getElementById("planner-prev-week");
const plannerNextWeekButton = document.getElementById("planner-next-week");
const plannerRepeatWeeklyCheckbox = document.getElementById("planner-repeat-weekly");
const plannerModeAvailableButton = document.getElementById("planner-mode-available");
const plannerModeEraseButton = document.getElementById("planner-mode-erase");
const plannerCalendar = document.getElementById("planner-calendar");
const plannerGeneratePreviewButton = document.getElementById("planner-generate-preview");
const plannerPreviewError = document.getElementById("planner-preview-error");
const plannerPreviewCapacity = document.getElementById("planner-preview-capacity");
const plannerPreviewSessions = document.getElementById("planner-preview-sessions");
const plannerConfirmButton = document.getElementById("planner-confirm-button");
const plannerPlanSummary = document.getElementById("planner-plan-summary");
const plannerPlanProgress = document.getElementById("planner-plan-progress");
const plannerPlanSessions = document.getElementById("planner-plan-sessions");
const plannerNewPlanButton = document.getElementById("planner-new-plan");
const plannerPlanWeekLabel = document.getElementById("planner-plan-week-label");
const plannerPlanPrevWeekButton = document.getElementById("planner-plan-prev-week");
const plannerPlanNextWeekButton = document.getElementById("planner-plan-next-week");
const plannerShell = plannerView?.querySelector(".planner-shell");
const plannerWorkspace = document.getElementById("planner-workspace");
const pcal = {
  materials: document.getElementById("pcal-material-list"),
  addMaterials: document.getElementById("pcal-add-materials"),
  newPlan: document.getElementById("pcal-new-plan"),
  today: document.getElementById("pcal-today"),
  prev: document.getElementById("pcal-prev"),
  next: document.getElementById("pcal-next"),
  range: document.getElementById("pcal-range"),
  status: document.getElementById("pcal-status"),
  explain: document.getElementById("pcal-explain"),
  review: document.getElementById("pcal-review"),
  autoPlan: document.getElementById("pcal-auto-plan"),
  accept: document.getElementById("pcal-accept"),
  notice: document.getElementById("pcal-notice"),
  scroll: document.getElementById("pcal-scroll"),
  head: document.getElementById("pcal-head"),
  body: document.getElementById("pcal-body"),
  queue: document.getElementById("pcal-queue"),
  queueCount: document.getElementById("pcal-queue-count"),
  sheet: document.getElementById("pcal-sheet"),
  sheetList: document.getElementById("pcal-sheet-list"),
  sheetClose: document.getElementById("pcal-sheet-close"),
  popover: document.getElementById("pcal-popover"),
};
const todayPlanPanel = document.getElementById("today-plan");

const authScreen = document.getElementById("auth-screen");
const appShell = document.getElementById("app-shell");
const authForm = document.getElementById("auth-form");
const authTitle = document.getElementById("auth-title");
const authCopy = document.getElementById("auth-copy");
const authNameField = document.getElementById("auth-name-field");
const authDisplayName = document.getElementById("auth-display-name");
const authEmail = document.getElementById("auth-email");
const authPassword = document.getElementById("auth-password");
const authError = document.getElementById("auth-error");
const authSubmit = document.getElementById("auth-submit");
const authSwitch = document.getElementById("auth-switch");
const logoutButton = document.getElementById("logout-button");
const profileDisplayName = document.getElementById("profile-display-name");
const profileEmail = document.getElementById("profile-email");
const profileAvatar = document.getElementById("profile-avatar");
const homeGreeting = document.getElementById("home-greeting");
const sessionSearchInput = document.getElementById("session-search-input");
const sessionSortSelect = document.getElementById("session-sort-select");
const sidebarRecentDocuments = document.getElementById("sidebar-recent-documents");

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  authTitle.textContent = signup ? "Create your account" : "Sign in";
  authCopy.textContent = signup
    ? "Create a private learning workspace for your own materials and progress."
    : "Access your materials, conversations, quizzes, and mastery.";
  authNameField.hidden = !signup;
  authDisplayName.required = signup;
  authPassword.autocomplete = signup ? "new-password" : "current-password";
  authSubmit.textContent = signup ? "Sign up" : "Sign in";
  authSwitch.textContent = signup ? "Already have an account? Sign in" : "Create an account";
  authError.textContent = "";
}

function showAuthenticatedShell(user) {
  currentUser = user;
  authScreen.hidden = true;
  appShell.hidden = false;
  profileDisplayName.textContent = user.display_name;
  profileEmail.textContent = user.email;
  profileAvatar.textContent = (user.display_name || user.email || "U").trim().charAt(0).toUpperCase();
  if (homeGreeting) homeGreeting.textContent = `Hello, ${user.display_name || "there"}!`;
  document.getElementById("admin-model-comparison-nav")?.toggleAttribute("hidden", !user.is_admin);
}

function showAuthentication() {
  currentUser = null;
  appShell.hidden = true;
  authScreen.hidden = false;
  document.getElementById("admin-model-comparison-nav")?.setAttribute("hidden", "");
  authPassword.value = "";
  setAuthMode("login");
}

async function handleAuthentication(event) {
  event.preventDefault();
  authSubmit.disabled = true;
  authError.textContent = "";
  const signup = authMode === "signup";
  try {
    const user = await fetchJson(signup ? AUTH_SIGNUP_API_URL : AUTH_LOGIN_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(signup ? { display_name: authDisplayName.value.trim() } : {}),
        email: authEmail.value.trim(),
        password: authPassword.value
      })
    });
    showAuthenticatedShell(user);
    await initializeApplication();
  } catch (error) {
    authError.textContent = error.message || "Authentication failed.";
  } finally {
    authSubmit.disabled = false;
  }
}

async function signOut() {
  try {
    await fetchJson(AUTH_LOGOUT_API_URL, { method: "POST" });
  } catch (error) {
    // Clear the local view even when the already-expired session cannot be revoked again.
  }
  uploadedSources = [];
  indexedDocuments = [];
  quizStatuses = [];
  conversations = [];
  activeConversation = null;
  currentQuiz = null;
  currentAttempt = null;
  dashboardData = null;
  knowledgeGaps = [];
  recommendations = [];
  showAuthentication();
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2400);
}

function setPage(page) {
  if (page !== "session") closeSourcesDrawer();
  state.page = page;
  document.body.dataset.page = page;
  navItems.forEach((item, index) => item.classList.toggle("active", item.dataset.page === page && (page !== "overview" || index === 0)));
  navItems.forEach((item) => {
    if (item.classList.contains("active")) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  syncSidebarRecentActive();
  views.forEach((view) => view.classList.toggle("active", view.id === `${page}-view`));
  pageTitle.textContent = pageTitles[page];
  if (pageTitles[page]) showToast(`Opened ${pageTitles[page]}`);
  if (page === "planner") loadPlannerData();
  if (page === "overview") loadTodayPlan();
  if (page === "model-comparison") loadQuizModelComparison();
}

async function loadGenerationModels() {
  try {
    const data = await fetchJson(MODELS_API_URL);
    generationModels = data.models || [];
    if (!generationModels.some((model) => model.id === selectedModelId)) selectedModelId = generationModels.find((model) => model.default)?.id || generationModels[0]?.id || "";
    renderModelSelector();
    if (currentQuiz?.questions?.length) assessmentTitle.textContent = assessmentTitleText(currentQuiz);   // labels of the models are known now
    // The restored/default selection may not be pulled yet (e.g. a lazy model chosen last session):
    // prepare it now rather than waiting for the user to touch the selector or click Generate.
    ensureSelectedModelReadyWithStatus().catch(() => {});
  } catch (error) { generationModels = []; }
}

// The Study Session's "Model:" selector is the ONLY place the model is chosen. Quiz, Summary,
// Flashcards and later AI features all use the selected model; none has a selector of its own.
function modelLabel(modelId, fallbackName) {
  return generationModels.find((model) => model.id === modelId)?.label || fallbackName || modelId || "";
}

// Says which model a Generate button will use (the Generate screens have no selector of their own).
function updateGenerateModelNotes() {
  document.querySelectorAll(".generate-model-name").forEach((element) => { element.textContent = modelLabel(selectedModelId); });
}

function renderModelSelector() {
  const header = document.querySelector(".session-header");
  if (!header || !generationModels.length) return;
  let select = document.getElementById("generation-model-select");
  if (!select) {
    const label = document.createElement("label"); label.className = "generation-model-control"; label.textContent = "Model:";
    select = document.createElement("select"); select.id = "generation-model-select";
    label.appendChild(select);
    const badge = document.createElement("span"); badge.id = "model-ready-badge"; badge.className = "model-ready-badge"; badge.hidden = true;
    label.appendChild(badge);
    header.appendChild(label);
    select.addEventListener("change", () => setSelectedModel(select.value));
  }
  select.innerHTML = "";
  generationModels.forEach((model) => select.add(new Option(model.label, model.id, false, model.id === selectedModelId)));
  updateGenerateModelNotes();
  renderModelReadyState();
}

// Every generation surface (Quiz, Summary, Flashcards) is gated on this same visible state: while
// preparing, their Generate buttons are disabled so preparation time never hides inside a
// "Generating…" state (model preparation happens BEFORE generation, and is timed separately -
// see backend/model_registry.py's prepare_generation_model / model_prepare_ms).
function renderModelReadyState() {
  const badge = document.getElementById("model-ready-badge");
  if (badge) {
    badge.hidden = modelReadyState === "unknown";
    badge.className = `model-ready-badge model-ready-${modelReadyState}`;
    badge.textContent = modelReadyState === "preparing" ? "Preparing…"
      : modelReadyState === "ready" ? "Ready"
      : modelReadyState === "error" ? (modelReadyMessage || "Could not prepare model")
      : "";
    if (modelReadyState === "error") badge.title = modelReadyMessage || "Could not prepare model";
  }
  renderQuizSheetModelSummary();
  if (document.body?.dataset?.sessionTab === "overview") renderSessionOverview();
  [generateQuizButton, generateSummaryButton, regenerateSummaryButton, generateFlashcardsButton, regenerateFlashcardsButton]
    .filter(Boolean)
    .forEach((button) => { button.disabled = modelReadyState === "preparing"; });
}

// Create Quiz sheet's Model summary: names the Study Session's selected model (via the shared
// .generate-model-name class, see updateGenerateModelNotes) and mirrors the same Ready/Preparing/
// Error state as the header badge above -- never a second model picker.
function renderQuizSheetModelSummary() {
  const badge = document.getElementById("quiz-sheet-model-badge");
  if (!badge) return;
  badge.hidden = modelReadyState === "unknown";
  badge.className = `quiz-sheet-model-badge quiz-sheet-model-badge--${modelReadyState}`;
  badge.textContent = modelReadyState === "preparing" ? "Preparing…"
    : modelReadyState === "ready" ? "Ready"
    : modelReadyState === "error" ? "Error"
    : "";
  if (modelReadyState === "error") badge.title = modelReadyMessage || "Could not prepare model";
}

// Wraps ensureSelectedModelReady() (the actual prepare/pull contract, shared with the AI Tutor
// chat) with the visible Preparing…/Ready/error state and the Generate-button disable gate.
async function ensureSelectedModelReadyWithStatus() {
  modelReadyState = "preparing"; modelReadyMessage = ""; renderModelReadyState();
  try {
    const modelId = await ensureSelectedModelReady();
    modelReadyState = "ready"; modelReadyMessage = ""; renderModelReadyState();
    return modelId;
  } catch (error) {
    modelReadyState = "error"; modelReadyMessage = error.message || "Model could not be prepared.";
    renderModelReadyState();
    throw error;
  }
}

async function setSelectedModel(modelId) {
  selectedModelId = modelId; localStorage.setItem("aiTutorModelId", selectedModelId);
  const select = document.getElementById("generation-model-select");
  if (select) select.value = selectedModelId;
  updateGenerateModelNotes();
  // Changing the model never generates anything: saved states are re-read and the screens
  // re-evaluated for the new model; a saved quiz keeps the model that made it.
  updateDifficultyOptions();
  if (currentQuiz?.questions?.length) assessmentTitle.textContent = assessmentTitleText(currentQuiz);
  const tab = document.body.dataset.sessionTab;
  if (tab === "summary") showSummaryState();
  if (tab === "flashcards") showFlashcardsState();
  if (tab === "overview") loadSessionOverview();
  try { await ensureSelectedModelReadyWithStatus(); showToast("Model ready"); }
  catch (error) { showToast(error.message || "Model could not be prepared."); }
}

function formatBenchmarkPercent(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

function formatBenchmarkSeconds(value) {
  return typeof value === "number" ? `${value.toFixed(1)}s` : "—";
}

function formatBenchmarkNumber(value, digits = 1) {
  return typeof value === "number" ? value.toFixed(digits) : "—";
}

function formatBenchmarkVram(value) {
  return typeof value === "number" ? `${Math.round(value).toLocaleString()} MB` : "—";
}

async function loadQuizModelComparison() {
  const view = document.getElementById("model-comparison-view");
  if (!view) return;
  view.innerHTML = '<div class="empty-state">Loading benchmark results…</div>';
  try {
    const data = await fetchJson(ADMIN_QUIZ_MODEL_COMPARISON_API_URL);
    renderQuizModelComparison(view, data);
  } catch (error) {
    view.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "Could not load the Quiz model comparison.";
    view.appendChild(empty);
  }
}

function renderQuizModelComparison(view, data) {
  view.innerHTML = "";
  const panel = document.createElement("article");
  panel.className = "panel model-comparison-panel";

  const heading = document.createElement("div");
  heading.className = "panel-heading";
  heading.innerHTML = "<div><p>Admin only</p><h2>Quiz Model Comparison</h2></div>";
  panel.appendChild(heading);

  const lastRun = document.createElement("p");
  lastRun.className = "muted";
  lastRun.textContent = data.generated_at
    ? `Last benchmark: ${new Date(data.generated_at).toLocaleString()}`
    : "Last benchmark: not run yet";
  panel.appendChild(lastRun);

  const wrap = document.createElement("div");
  wrap.className = "summary-table-wrap";
  const table = document.createElement("table");
  table.className = "summary-table";
  const thead = document.createElement("thead");
  thead.innerHTML = (
    "<tr><th>Model</th><th>Success Rate</th><th>Valid Question Rate</th><th>Grounding</th>"
    + "<th>Avg Latency</th><th>Avg Retry</th><th>VRAM</th><th>Questions/min</th></tr>"
  );
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  (data.models || []).forEach((model) => {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.appendChild(document.createTextNode(`${model.label} `));
    const badge = document.createElement("span");
    const isProduction = model.status === "current_production";
    badge.className = `soft-badge ${isProduction ? "production" : "candidate"}`;
    badge.textContent = isProduction ? "Current Production" : "Benchmark Candidate";
    nameCell.appendChild(badge);
    row.appendChild(nameCell);

    [
      formatBenchmarkPercent(model.success_rate),
      formatBenchmarkPercent(model.valid_question_rate),
      formatBenchmarkPercent(model.grounding_rate),
      formatBenchmarkSeconds(model.avg_latency_seconds),
      formatBenchmarkNumber(model.avg_retries),
      formatBenchmarkVram(model.vram_mb),
      formatBenchmarkNumber(model.questions_per_minute),
    ].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });

    if (!model.measured) row.classList.add("model-comparison-unmeasured");
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);

  if (!(data.models || []).some((model) => model.measured)) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "No benchmark data yet. Run the offline Quiz model benchmark script to populate this table.";
    panel.appendChild(note);
  }

  view.appendChild(panel);
}

function setSessionTab(tab) {
  document.body.dataset.sessionTab = tab;
  document.querySelectorAll(".session-tab").forEach((button) => button.classList.toggle("active", button.dataset.sessionTab === tab));
  document.querySelectorAll(".session-pane").forEach((pane) => pane.classList.toggle("active", pane.dataset.sessionPane === tab));
  document.getElementById("persistent-tutor").hidden = false;
  if (tab === "summary") showSummaryState();
  if (tab === "flashcards") showFlashcardsState();
  if (tab === "quiz") renderQuizHistory();
  if (tab === "overview") loadSessionOverview();
}

// ---- Study Session Overview ---------------------------------------------------------------------

function sessionOverviewKey() {
  return `${activeDocumentId}:${selectedModelId}:${flashcardLanguage}`;
}

async function loadSessionOverview() {
  if (!activeDocumentId) return;
  const key = sessionOverviewKey();
  if (sessionOverviewStatus.key !== key) sessionOverviewStatus = { key, summary: "loading", flashcards: "loading", flashcardCount: 0 };
  renderSessionOverview();
  const documentId = activeDocumentId;
  const [summary, cards] = await Promise.allSettled([
    fetchJson(`${SUMMARY_API_BASE_URL}/${encodeURIComponent(documentId)}?model_id=${encodeURIComponent(selectedModelId)}&cache_only=true`),
    fetchJson(flashcardsUrl("&cache_only=true")),
  ]);
  if (sessionOverviewKey() !== key) return;   // switched document/model meanwhile
  sessionOverviewStatus.summary = summary.status === "rejected" ? "error"
    : (summary.value?.status !== "not_generated" && summary.value?.final_summary ? "generated" : "not_generated");
  const cardCount = cards.status === "fulfilled" && cards.value?.status !== "not_generated" ? (cards.value?.cards || []).length : 0;
  sessionOverviewStatus.flashcards = cards.status === "rejected" ? "error" : (cardCount ? "generated" : "not_generated");
  sessionOverviewStatus.flashcardCount = cardCount;
  renderSessionOverview();
}

function sessionOverviewQuizState(documentId) {
  const variants = (quizStatuses.find((item) => item.document_id === documentId)?.variants || [])
    .filter((variant) => variant.quiz_id);
  const byRecent = (left, right) => new Date(right.updated_at || right.created_at || 0) - new Date(left.updated_at || left.created_at || 0);
  const inProgress = variants.filter((variant) => variant.progress_status === "in_progress").sort(byRecent);
  const attempts = quizHistory.filter((attempt) => attempt.document_id === documentId)
    .sort((left, right) => new Date(right.completed_at || 0) - new Date(left.completed_at || 0));
  const completedQuizIds = new Set(attempts.map((attempt) => attempt.quiz_id).filter(Boolean));
  return { variants, inProgress, latest: attempts[0] || null, completedCount: completedQuizIds.size };
}

function renderSessionOverview() {
  const root = document.getElementById("session-overview");
  const documentItem = indexedDocuments.find((item) => item.id === activeDocumentId);
  if (!root || !documentItem) return;
  const status = sessionOverviewStatus;
  const quiz = sessionOverviewQuizState(activeDocumentId);
  const readiness = { preparing: "Preparing…", ready: "Ready", error: "Unavailable" }[modelReadyState] || "";
  const topicCount = documentItem.topics?.length || 0;
  root.innerHTML = `
    <header class="overview-header">
      <p class="eyebrow">Overview</p>
      <h2 class="overview-title"></h2>
      <p class="overview-context">
        <span class="overview-model"></span>
        <span class="overview-model-state overview-model-state--${modelReadyState}"${readiness ? "" : " hidden"}>${readiness}</span>
        <span class="overview-topics">${topicCount} topic${topicCount === 1 ? "" : "s"}</span>
      </p>
    </header>
    <section class="overview-section overview-continue" hidden>
      <h3>Continue studying</h3>
      <div class="overview-continue-card">
        <div class="overview-continue-copy">
          <span class="overview-continue-label">Quiz in progress</span>
          <strong class="overview-continue-title"></strong>
          <small class="overview-continue-detail"></small>
          <div class="overview-continue-track"><span></span></div>
        </div>
        <button class="primary-button overview-resume-button" type="button">Resume Quiz</button>
      </div>
    </section>
    <section class="overview-section">
      <h3>Study tools</h3>
      <div class="overview-tools"></div>
    </section>
    <section class="overview-section">
      <h3>Progress snapshot</h3>
      <div class="overview-stats"></div>
    </section>`;
  root.querySelector(".overview-title").textContent = documentItem.title;
  root.querySelector(".overview-model").textContent = `Model: ${modelLabel(selectedModelId) || "—"}`;

  const resume = quiz.inProgress[0];
  if (resume) {
    const answered = Number(resume.answered) || 0;
    const total = Number(resume.total || resume.question_count) || 0;
    root.querySelector(".overview-continue").hidden = false;
    root.querySelector(".overview-continue-title").textContent = (resume.title || "").trim() || "Untitled Quiz";
    root.querySelector(".overview-continue-detail").textContent = `${answered} / ${total} answered`
      + (quiz.inProgress.length > 1 ? ` · ${quiz.inProgress.length - 1} more in progress` : "");
    root.querySelector(".overview-continue-track span").style.width = `${total ? Math.round((answered / total) * 100) : 0}%`;
    root.querySelector(".overview-resume-button").addEventListener("click", () => resumeQuizFromOverview(resume));
  }

  const summaryText = { loading: "Checking…", generated: "Generated", not_generated: "Not generated", error: "Couldn't check" }[status.summary];
  const flashcardText = status.flashcards === "generated" ? `${status.flashcardCount} card${status.flashcardCount === 1 ? "" : "s"}`
    : { loading: "Checking…", not_generated: "Not generated", error: "Couldn't check" }[status.flashcards];
  const quizParts = [
    quiz.variants.length ? `${quiz.variants.length} quiz${quiz.variants.length === 1 ? "" : "zes"}` : "No quizzes yet",
    quiz.inProgress.length ? `${quiz.inProgress.length} in progress` : "",
    quiz.completedCount ? `${quiz.completedCount} completed` : "",
  ].filter(Boolean);
  const tools = [
    { tab: "tutor", icon: "💬", name: "AI Tutor", detail: "Ask about this document", state: "neutral" },
    { tab: "summary", icon: "📄", name: "Summary", detail: summaryText, state: status.summary },
    { tab: "flashcards", icon: "🗂", name: "Flashcards", detail: flashcardText, state: status.flashcards },
    { tab: "quiz", icon: "✓", name: "Quiz", detail: quizParts.join(" · "), state: quiz.variants.length ? "generated" : "not_generated" },
  ];
  const toolList = root.querySelector(".overview-tools");
  tools.forEach((tool) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `overview-tool overview-tool--${tool.state}`;
    button.dataset.overviewTool = tool.tab;
    const icon = document.createElement("span"); icon.className = "overview-tool-icon"; icon.setAttribute("aria-hidden", "true"); icon.textContent = tool.icon;
    const copy = document.createElement("span"); copy.className = "overview-tool-copy";
    const name = document.createElement("strong"); name.textContent = tool.name;
    const detail = document.createElement("small"); detail.className = "overview-tool-status"; detail.textContent = tool.detail;
    copy.append(name, detail);
    const chevron = document.createElement("span"); chevron.className = "overview-tool-chevron"; chevron.setAttribute("aria-hidden", "true"); chevron.textContent = "›";
    button.append(icon, copy, chevron);
    button.addEventListener("click", () => openOverviewTool(tool.tab));
    toolList.appendChild(button);
  });

  const latest = quiz.latest;
  const stats = [
    ["Quizzes completed", String(quiz.completedCount)],
    ["In progress", String(quiz.inProgress.length)],
    ["Latest score", latest ? `${latest.score} / ${latest.total}` : "—", latest ? ((latest.title || "").trim() || "Untitled Quiz") : "No completed quiz yet"],
    ["Flashcards", status.flashcards === "generated" ? String(status.flashcardCount) : "—", status.flashcards === "loading" ? "Checking…" : ""],
    ["Summary", { generated: "Available", not_generated: "Not yet", error: "—", loading: "…" }[status.summary]],
  ];
  const statList = root.querySelector(".overview-stats");
  stats.forEach(([label, value, note]) => {
    const tile = document.createElement("div");
    tile.className = "overview-stat";
    const strong = document.createElement("strong"); strong.textContent = value;
    const span = document.createElement("span"); span.textContent = label;
    tile.append(strong, span);
    if (note) { const small = document.createElement("small"); small.textContent = note; tile.appendChild(small); }
    statList.appendChild(tile);
  });
}

function openOverviewTool(tab) {
  if (tab === "tutor") {
    // The AI Tutor lives in the persistent side panel next to every tab: land on the material
    // with the chat focused (same as the Library's AI Tutor quick action).
    setSessionTab("material");
    chatInput?.focus();
    return;
  }
  setSessionTab(tab);
}

async function resumeQuizFromOverview(variant) {
  setSessionTab("quiz");
  try {
    await openQuizPlayer({ document_id: activeDocumentId, topic_id: variant.topic_id, difficulty: variant.difficulty, quiz_id: variant.quiz_id });
  } catch (error) {
    showToast(error.message || "Could not open this quiz");
  }
}

function visibleFlashcards() {
  return flashcardTopicFilter === "all" ? flashcards : flashcards.filter((card) => card.topic_id === flashcardTopicFilter);
}

function renderCurrentFlashcard() {
  const cards = visibleFlashcards();
  if (!cards.length) { flashcardsStage.hidden = true; flashcardsFilterEmpty.hidden = false; return; }
  flashcardsFilterEmpty.hidden = true; flashcardsStage.hidden = false;
  flashcardIndex = Math.min(flashcardIndex, cards.length - 1);
  const card = cards[flashcardIndex];
  flashcardElement.classList.toggle("flipped", flashcardFlipped);
  flashcardSideLabel.textContent = flashcardFlipped ? "Back" : "Front";
  flashcardCopy.textContent = flashcardFlipped ? card.back : card.front;
  flashcardTopicTitle.textContent = card.subtopic_name ? `${card.topic_name} · ${card.subtopic_name}` : card.topic_name;
  flashcardPosition.textContent = `${flashcardIndex + 1} / ${cards.length}`;
  if (flashcardProgressBar) flashcardProgressBar.style.width = `${((flashcardIndex + 1) / cards.length) * 100}%`;
  favoriteFlashcardButton.textContent = card.is_favorite ? "★" : "☆";
  favoriteFlashcardButton.classList.toggle("active", card.is_favorite);
}

function renderFlashcardTopicFilter() {
  const selected = flashcardTopicFilter;
  flashcardTopicSelect.innerHTML = ""; flashcardTopicSelect.add(new Option("All topics", "all"));
  const seen = new Set();
  flashcards.forEach((card) => { if (!seen.has(card.topic_id)) { seen.add(card.topic_id); flashcardTopicSelect.add(new Option(card.topic_name, card.topic_id)); } });
  flashcardTopicFilter = seen.has(selected) ? selected : "all"; flashcardTopicSelect.value = flashcardTopicFilter;
}

function flashcardsKey() {
  return `${activeDocumentId}:${selectedModelId}:${flashcardLanguage}`;
}

function flashcardsUrl(extra = "") {
  return `${FLASHCARDS_API_BASE_URL}/${encodeURIComponent(activeDocumentId)}?model_id=${encodeURIComponent(selectedModelId)}&language=${encodeURIComponent(flashcardLanguage)}${extra}`;
}

// Keeps the friendly failure text as the only thing shown by default; whatever the backend sent
// (already sanitized -- never raw Ollama/model text, see backend/flashcard_service.py's
// FlashcardGenerationError) is still reachable through the collapsed "Technical details" toggle.
function showFlashcardsError(message) {
  const safeMessage = message || "Couldn't generate flashcards with the selected model. Please try again or switch models.";
  flashcardsErrorMessage.textContent = safeMessage;
  flashcardsErrorTechnical.textContent = safeMessage;
  flashcardsError.hidden = false;
}

function setFlashcardsBusy(isBusy) {
  [generateFlashcardsButton, regenerateFlashcardsButton].filter(Boolean).forEach((button) => { button.disabled = isBusy; });
}

function resetRegenerateButton() {
  if (regenerateFlashcardsButton) regenerateFlashcardsButton.textContent = "Regenerate";
}

// A regenerate error is shown as a small inline banner ON the still-visible deck, never as the
// full-page error state -- the existing deck must never disappear just because a regenerate failed.
function showRegenerateError(message) {
  if (!flashcardsRegenerateError) return;
  flashcardsRegenerateErrorMessage.textContent = message || "Couldn't regenerate flashcards with the selected model. Please try again or switch models.";
  flashcardsRegenerateError.hidden = false;
}

function updateFlashcardsEmptyLanguageNote() {
  const note = document.getElementById("flashcards-empty-language");
  if (note) note.textContent = FLASHCARD_LANGUAGE_LABELS[flashcardLanguage] || flashcardLanguage;
}

function applyFlashcardSet(set, key) {
  flashcardSet = set; flashcards = set.cards || []; flashcardIndex = 0; flashcardFlipped = false; loadedFlashcardKey = key;
  flashcardsGenerate.hidden = true;
  if (regenerateFlashcardsButton) regenerateFlashcardsButton.hidden = false;
  if (flashcardsModelNote) flashcardsModelNote.textContent = set.model_id ? `Generated by ${modelLabel(set.model_id)}` : "";
  if (flashcardsRegenerateError) flashcardsRegenerateError.hidden = true;
  if (flashcardsRegenerateStatus) flashcardsRegenerateStatus.hidden = true;
  renderFlashcardTopicFilter(); renderCurrentFlashcard();
}

// Opening the tab (or changing model/language) only LOOKS for saved cards; nothing is generated.
async function showFlashcardsState() {
  if (!activeDocumentId || !flashcardsPane) return;
  const requestDocumentId = activeDocumentId, key = flashcardsKey();
  updateFlashcardsEmptyLanguageNote();
  flashcardsLoading.hidden = flashcardsInFlightKey !== key;   // a generation of these very cards may still be running
  if (loadedFlashcardKey === key && flashcards.length) { flashcardsGenerate.hidden = true; renderCurrentFlashcard(); return; }
  flashcards = []; flashcardSet = null; loadedFlashcardKey = "";
  if (regenerateFlashcardsButton) regenerateFlashcardsButton.hidden = true;
  if (flashcardsRegenerateError) flashcardsRegenerateError.hidden = true;
  if (flashcardsRegenerateStatus) flashcardsRegenerateStatus.hidden = true;
  if (flashcardsInFlightKey === key) { flashcardsError.hidden = true; flashcardsStage.hidden = true; flashcardsFilterEmpty.hidden = true; flashcardsGenerate.hidden = true; return; }
  flashcardsError.hidden = true; flashcardsStage.hidden = true; flashcardsFilterEmpty.hidden = true; flashcardsGenerate.hidden = true;
  let saved = null;
  try { saved = await fetchJson(flashcardsUrl("&cache_only=true")); } catch (error) { saved = null; }
  if (activeDocumentId !== requestDocumentId || key !== flashcardsKey()) return;
  if (saved && saved.status !== "not_generated" && (saved.cards || []).length) applyFlashcardSet(saved, key);
  else flashcardsGenerate.hidden = false;
}

// Only the Generate/Regenerate buttons (or a saved-set reload) reach this: it is the one place
// that generates. `regenerate` bypasses the cache and always requests a fresh set.
//
// Regenerating over an already-visible deck never uses the full empty/skeleton state: the current
// deck stays on screen (a small status replaces it only above the card), and a failure leaves that
// same deck in place with an inline retryable banner instead of the full-page error state. Only an
// initial generation (no deck yet) uses the full Preparing/Generating skeleton and full error state.
async function loadDocumentFlashcards(regenerate = false) {
  if (!activeDocumentId || !flashcardsPane) return;
  const requestDocumentId = activeDocumentId, key = flashcardsKey();
  if (!regenerate && loadedFlashcardKey === key && flashcards.length) { renderCurrentFlashcard(); return; }
  const hasVisibleDeck = regenerate && flashcards.length > 0;
  setFlashcardsBusy(true);
  if (hasVisibleDeck) {
    regenerateFlashcardsButton.textContent = "Regenerating…";
    flashcardsRegenerateError.hidden = true;
    flashcardsRegenerateStatus.textContent = "Preparing model…";
    flashcardsRegenerateStatus.hidden = false;
  } else {
    flashcardsLoadingLabel.textContent = "Preparing model…";
    flashcardsLoading.hidden = false; flashcardsError.hidden = true; flashcardsStage.hidden = true;
    flashcardsFilterEmpty.hidden = true; flashcardsGenerate.hidden = true;
  }
  // Model preparation happens BEFORE generation timing starts, not inside the "Generating…" state,
  // but it is still shown here so Preparing/Generating are two visibly distinct sub-states.
  try { await ensureSelectedModelReadyWithStatus(); }
  catch (error) {
    if (activeDocumentId === requestDocumentId) {
      if (hasVisibleDeck) {
        flashcardsRegenerateStatus.hidden = true;
        showRegenerateError(error.message || "Model could not be prepared.");
      } else {
        flashcardsLoading.hidden = true;
        showFlashcardsError(error.message || "Model could not be prepared.");
        if (!flashcards.length) flashcardsGenerate.hidden = false;
      }
    }
    resetRegenerateButton(); setFlashcardsBusy(false);
    return;
  }
  if (activeDocumentId !== requestDocumentId || key !== flashcardsKey()) { resetRegenerateButton(); setFlashcardsBusy(false); return; }   // another document/model/language is on screen now
  // ensureSelectedModelReadyWithStatus() just re-enabled every Generate/Regenerate button via
  // renderModelReadyState() (model is "ready" now) -- re-assert busy for the generation call itself.
  setFlashcardsBusy(true);
  if (hasVisibleDeck) flashcardsRegenerateStatus.textContent = "Regenerating flashcards…";
  else flashcardsLoadingLabel.textContent = "Generating flashcards…";
  flashcardsInFlightKey = key;
  let generated = false;
  try {
    const url = regenerate ? `${FLASHCARDS_API_BASE_URL}/${encodeURIComponent(activeDocumentId)}/regenerate` : flashcardsUrl();
    const set = await fetchJson(url, regenerate ? {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: selectedModelId || null, language: flashcardLanguage }),
    } : {});
    if (activeDocumentId !== requestDocumentId || key !== flashcardsKey()) return;   // another document/model/language is on screen now
    applyFlashcardSet(set, key); generated = true;   // resets to card 1 and updates the model/progress labels
  } catch (error) {
    if (activeDocumentId === requestDocumentId) {
      if (hasVisibleDeck) showRegenerateError(error.message || "Could not regenerate flashcards.");
      else showFlashcardsError(error.message || "Could not load flashcards.");
    }
  } finally {
    if (flashcardsInFlightKey === key) flashcardsInFlightKey = "";
    resetRegenerateButton(); setFlashcardsBusy(false);
    if (activeDocumentId === requestDocumentId) {
      if (hasVisibleDeck) { flashcardsRegenerateStatus.hidden = true; }
      else { flashcardsLoading.hidden = true; if (!generated && !flashcards.length) flashcardsGenerate.hidden = false; }
    }
  }
}

async function patchFlashcard(card, changes) {
  const updated = await fetchJson(`${FLASHCARDS_API_BASE_URL}/${encodeURIComponent(activeDocumentId)}/cards/${encodeURIComponent(card.flashcard_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) });
  const index = flashcards.findIndex((item) => item.flashcard_id === updated.flashcard_id); if (index >= 0) flashcards[index] = updated;
  return updated;
}

function closeFlashcardManager() { flashcardManager?.classList.remove("open"); }

function openFlashcardManager() {
  if (!flashcardSet) return;
  if (!flashcardManager) {
    flashcardManager = document.createElement("div"); flashcardManager.className = "flashcard-manager";
    flashcardManager.innerHTML = `<section class="flashcard-manager-card" role="dialog" aria-modal="true"><div class="flashcard-manager-heading"><div><p class="eyebrow">Flashcards</p><h2>Manage Cards</h2></div><button class="text-button flashcard-manager-close" type="button">Close</button></div><form id="add-flashcard-form" class="add-flashcard-form"><select id="add-flashcard-topic" required></select><input id="add-flashcard-front" placeholder="Front: question or term" required maxlength="1000"><textarea id="add-flashcard-back" placeholder="Back: answer or definition" required maxlength="4000"></textarea><button class="primary-button" type="submit">Add Card</button></form><div id="flashcard-manager-list" class="flashcard-manager-list"></div></section>`;
    flashcardManager.querySelector(".flashcard-manager-close").addEventListener("click", closeFlashcardManager);
    flashcardManager.addEventListener("click", (event) => { if (event.target === flashcardManager) closeFlashcardManager(); });
    flashcardManager.querySelector("#add-flashcard-form").addEventListener("submit", addManagedFlashcard);
    document.body.appendChild(flashcardManager);
  }
  const topicSelect = flashcardManager.querySelector("#add-flashcard-topic"); topicSelect.innerHTML = "";
  const topics = indexedDocuments.find((item) => item.id === activeDocumentId)?.topics || [];
  topics.forEach((topic) => topicSelect.add(new Option(topic.name, topic.topic_id)));
  renderFlashcardManagerList(); flashcardManager.classList.add("open");
}

function renderFlashcardManagerList() {
  const list = flashcardManager.querySelector("#flashcard-manager-list"); list.innerHTML = "";
  flashcards.forEach((card) => {
    const row = document.createElement("article"); row.className = "managed-flashcard";
    const badge = document.createElement("span"); badge.className = "topic-badge"; badge.textContent = card.topic_name;
    const front = document.createElement("input"); front.value = card.front; front.setAttribute("aria-label", "Card front");
    const back = document.createElement("textarea"); back.value = card.back; back.setAttribute("aria-label", "Card back");
    const actions = document.createElement("div"); actions.className = "managed-flashcard-actions";
    const save = document.createElement("button"); save.className = "secondary-button"; save.type = "button"; save.textContent = "Save";
    save.addEventListener("click", async () => { try { await patchFlashcard(card, { front: front.value, back: back.value }); showToast("Card saved"); renderCurrentFlashcard(); } catch (error) { showToast(error.message); } });
    const remove = document.createElement("button"); remove.className = "text-button danger-button"; remove.type = "button"; remove.textContent = "Delete";
    remove.addEventListener("click", async () => { try { await fetchJson(`${FLASHCARDS_API_BASE_URL}/${encodeURIComponent(activeDocumentId)}/cards/${encodeURIComponent(card.flashcard_id)}`, { method: "DELETE" }); flashcards = flashcards.filter((item) => item.flashcard_id !== card.flashcard_id); renderFlashcardManagerList(); renderFlashcardTopicFilter(); renderCurrentFlashcard(); } catch (error) { showToast(error.message); } });
    actions.append(save, remove); row.append(badge, front, back, actions); list.appendChild(row);
  });
}

async function addManagedFlashcard(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const card = await fetchJson(`${FLASHCARDS_API_BASE_URL}/${encodeURIComponent(activeDocumentId)}/cards`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ set_id: flashcardSet.set_id, topic_id: form.querySelector("#add-flashcard-topic").value, front: form.querySelector("#add-flashcard-front").value, back: form.querySelector("#add-flashcard-back").value }) });
    flashcards.push(card); form.reset(); renderFlashcardManagerList(); renderFlashcardTopicFilter(); renderCurrentFlashcard();
  } catch (error) { showToast(error.message); }
}

function appendTakeaways(parent, takeaways, title = "Summary & Key Takeaways") {
  if (!takeaways?.length) return;
  const heading = document.createElement("h3"); heading.textContent = title; parent.appendChild(heading);
  const list = document.createElement("ul"); list.className = "summary-takeaways";
  takeaways.forEach((value) => { const item = document.createElement("li"); item.textContent = value; list.appendChild(item); });
  parent.appendChild(list);
}

function renderSummaryContent(content) {
  if (!content) return null;
  if (content.type === "paragraph") {
    const paragraph = document.createElement("p"); paragraph.textContent = content.text || ""; return paragraph;
  }
  if (content.type === "bullets") {
    const list = document.createElement("ul"); list.className = "summary-subsection-bullets";
    (content.items || []).forEach((value) => { const item = document.createElement("li"); item.textContent = value; list.appendChild(item); });
    return list;
  }
  if (content.type === "table") {
    const wrapper = document.createElement("div"); wrapper.className = "summary-table-wrap";
    const table = document.createElement("table"); table.className = "summary-table";
    const head = document.createElement("thead"); const headRow = document.createElement("tr");
    (content.headers || []).forEach((value) => { const cell = document.createElement("th"); cell.textContent = value; headRow.appendChild(cell); });
    head.appendChild(headRow); table.appendChild(head);
    const body = document.createElement("tbody");
    (content.rows || []).forEach((row) => { const tableRow = document.createElement("tr"); row.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; tableRow.appendChild(cell); }); body.appendChild(tableRow); });
    table.appendChild(body); wrapper.appendChild(table); return wrapper;
  }
  return null;
}

function renderDocumentSummary(summary) {
  summaryContent.innerHTML = "";
  const generatedBy = modelLabel(summary.model_id || selectedModelId);
  if (generatedBy) { const badge = document.createElement("p"); badge.className = "artifact-model"; badge.textContent = `Generated by ${generatedBy}`; summaryContent.appendChild(badge); }
  const overview = document.createElement("p"); overview.className = "summary-overview";
  overview.textContent = summary.final_summary?.overview || "No overview was returned."; summaryContent.appendChild(overview);
  (summary.topic_summaries || []).forEach((topic) => {
    const section = document.createElement("section"); section.className = "summary-topic";
    const heading = document.createElement("h3"); heading.textContent = topic.topic_name || topic.topic_id; section.appendChild(heading);
    const copy = document.createElement("p"); copy.className = "summary-topic-overview"; copy.textContent = topic.overview; section.appendChild(copy);
    (topic.subsections || []).forEach((subtopic) => {
      const subsection = document.createElement("section"); subsection.className = "summary-subsection";
      const subheading = document.createElement("h4"); subheading.textContent = subtopic.subtopic_name || subtopic.subtopic_id; subsection.appendChild(subheading);
      const content = renderSummaryContent(subtopic.content); if (content) subsection.appendChild(content); section.appendChild(subsection);
    });
    summaryContent.appendChild(section);
  });
  const takeaways = document.createElement("section"); takeaways.className = "summary-final-takeaways";
  appendTakeaways(takeaways, summary.final_summary?.key_takeaways); if (takeaways.children.length) summaryContent.appendChild(takeaways);
}

function updateSummaryChrome(key) {
  const has = loadedSummaryKey === key && summaryContent.children.length > 0;
  summaryGenerate.hidden = has || !summaryLoading.hidden;
  regenerateSummaryButton.hidden = !has;
}

// Opening the tab (or changing model) only LOOKS for a saved summary; nothing is generated.
async function showSummaryState() {
  if (!activeDocumentId || !summaryContent) return;
  const requestDocumentId = activeDocumentId, key = `${requestDocumentId}:${selectedModelId}`;
  summaryError.hidden = true;
  summaryLoading.hidden = summaryInFlightKey !== key;   // a generation of this very summary may still be running
  if (loadedSummaryKey === key && summaryContent.children.length) { summaryContent.hidden = false; updateSummaryChrome(key); return; }
  if (summaryInFlightKey === key) { summaryContent.innerHTML = ""; summaryGenerate.hidden = true; regenerateSummaryButton.hidden = true; return; }
  summaryContent.innerHTML = ""; loadedSummaryKey = ""; summaryGenerate.hidden = true; regenerateSummaryButton.hidden = true;
  let saved = null;
  try { saved = await fetchJson(`${SUMMARY_API_BASE_URL}/${encodeURIComponent(requestDocumentId)}?model_id=${encodeURIComponent(selectedModelId)}&cache_only=true`); } catch (error) { saved = null; }
  if (activeDocumentId !== requestDocumentId || key !== `${activeDocumentId}:${selectedModelId}`) return;
  if (saved && saved.status !== "not_generated" && saved.final_summary) { renderDocumentSummary(saved); loadedSummaryKey = key; summaryContent.hidden = false; }
  updateSummaryChrome(key);
}

// Only the Generate / Regenerate buttons reach this: it is the one place that generates.
async function loadDocumentSummary(regenerate = false) {
  if (!activeDocumentId || !summaryContent) return;
  const requestDocumentId = activeDocumentId;
  const key = `${requestDocumentId}:${selectedModelId}`;
  if (!regenerate && loadedSummaryKey === key && summaryContent.children.length) return;
  // Model preparation happens BEFORE generation timing starts, not inside the "Generating…" state.
  try { await ensureSelectedModelReadyWithStatus(); }
  catch (error) {
    if (activeDocumentId === requestDocumentId) { summaryError.textContent = error.message || "Model could not be prepared."; summaryError.hidden = false; }
    return;
  }
  if (activeDocumentId !== requestDocumentId || key !== `${activeDocumentId}:${selectedModelId}`) return;   // another document/model is on screen now
  summaryLoading.hidden = false; summaryError.hidden = true; summaryContent.hidden = true; summaryGenerate.hidden = true;
  regenerateSummaryButton.disabled = true; summaryInFlightKey = key;
  try {
    const url = `${SUMMARY_API_BASE_URL}/${encodeURIComponent(requestDocumentId)}`;
    const summary = await fetchJson(regenerate ? `${url}/regenerate` : `${url}?model_id=${encodeURIComponent(selectedModelId)}`, regenerate ? {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_id: selectedModelId || null })
    } : {});
    if (activeDocumentId !== requestDocumentId || key !== `${activeDocumentId}:${selectedModelId}`) return;   // another document/model is on screen now
    renderDocumentSummary(summary); loadedSummaryKey = key;
  } catch (error) {
    if (activeDocumentId === requestDocumentId) { summaryError.textContent = error.message || "Could not load summary."; summaryError.hidden = false; }
  } finally {
    if (summaryInFlightKey === key) summaryInFlightKey = "";
    if (activeDocumentId === requestDocumentId) { summaryLoading.hidden = true; summaryContent.hidden = false; regenerateSummaryButton.disabled = false; updateSummaryChrome(`${activeDocumentId}:${selectedModelId}`); }
  }
}

async function openStudySession(documentId, tab = "overview", topicId = "", { quizTarget } = {}) {
  // quizTarget (Quiz tab only): open that exact quiz in the focused Quiz Player -- the same entry
  // as the Quiz library's Start/Resume -- or, when null, land on the Quiz library. Either way the
  // default quiz is not pre-loaded, so the older inline quiz view is never shown on the way in.
  const documentItem = indexedDocuments.find((item) => item.id === documentId);
  if (!documentItem) return;
  activeDocumentId = documentId;
  if (quizPlayerOpen || quizAutosaveDirty || quizAutosaveInFlight) {
    // Leaving the session mid-quiz: persist the newest snapshot to the old quiz (captured before
    // the reset below) and dismiss the player.
    detachQuizAutosave();
    setQuizPlayerVisible(false);
  }
  currentQuiz = null;
  currentAttempt = null;
  quizAttemptSummary = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  quizHistory = [];
  if (quizHistoryDetail) quizHistoryDetail.hidden = true;
  renderAssessmentQuiz();
  renderQuizHistory();
  loadedSummaryKey = "";
  loadedFlashcardKey = ""; flashcardSet = null; flashcards = []; flashcardIndex = 0; flashcardTopicFilter = "all";
  if (summaryContent) summaryContent.innerHTML = "";
  const sessionBreadcrumb = document.getElementById("session-home-button");
  if (sessionBreadcrumb) sessionBreadcrumb.textContent = `Home > ${documentItem.title}`;
  try {
    await ensureStudySessionConversation();
  } catch (error) {
    console.error("Could not prepare the Study Session conversation", error);
    showToast(error.message || "Could not scope the tutor to this document");
  }
  if (activeDocumentId !== documentId) return;
  sessionDocumentName.textContent = documentItem.title;
  const matchingMaterial = (dashboardData?.materials || []).find((item) => item.document_id === documentId);
  const assessed = matchingMaterial?.assessed_topic_count || 0;
  sessionDocumentStatus.textContent = `${documentItem.topics?.length || 0} extracted topics · ${assessed} assessed`;
  const contentUrl = apiUrl(`/api/sources/${encodeURIComponent(documentId)}/content`);
  originalContentFileName.textContent = documentItem.title;
  originalContentOpen.href = contentUrl;
  originalContentFrame.src = `${contentUrl}#view=FitH`;
  originalContentFrame.hidden = false;
  originalContentEmpty.hidden = true;
  if (quizDocumentSelect) quizDocumentSelect.value = documentId;
  quizScopeSelect.value = "document";   // a quiz always covers the whole document
  flashcardTopicFilter = topicId && topicId !== "document" ? topicId : "all";
  const toQuizPlayer = quizTarget !== undefined;
  await Promise.all([toQuizPlayer ? null : loadSelectedQuiz(), loadQuizHistory(documentId)]);
  renderSessionProgress(documentId);
  setPage("session");
  setSessionTab(tab);
  if (toQuizPlayer && quizTarget) {
    try {
      await openQuizPlayer(quizTarget);
    } catch (error) {
      showToast(error.message || "Could not open this quiz");
    }
  }
}

function renderSessionProgress(documentId) {
  const assessed = (dashboardData?.mastery || []).filter((item) => item.document_id === documentId && item.mastery_level !== "Not assessed");
  renderMasteryList(sessionMasteryList, assessed, { emptyText: "Complete a quiz to see how each topic is going." });
  sessionCoverageList.innerHTML = "";
  if (!assessed.length) sessionCoverageList.innerHTML = '<div class="empty-state">Shows how much of each topic your quizzes have covered.</div>';
  assessed.forEach((item) => {
    const card = document.createElement("div");
    card.className = "mastery-card";
    const name = document.createElement("strong");
    name.textContent = item.topic_name || item.topic_id;
    const coverage = document.createElement("span");
    coverage.textContent = quizCoverageText(item);
    card.append(name, coverage);
    sessionCoverageList.appendChild(card);
  });
  const gaps = knowledgeGaps.filter((item) => item.document_id === documentId);
  sessionKnowledgeGapsList.innerHTML = "";
  if (!gaps.length) sessionKnowledgeGapsList.innerHTML = '<div class="empty-state">Nothing needs extra attention right now.</div>';
  gaps.forEach((gap) => { const item = document.createElement("div"); item.className = "knowledge-gap-row"; item.textContent = `${gap.topic_name || gap.topic_id} · ${gap.reason || "Needs more practice"}`; sessionKnowledgeGapsList.appendChild(item); });
  const next = recommendations.filter((item) => item.document_id === documentId);
  sessionRecommendationsList.innerHTML = "";
  if (!next.length) sessionRecommendationsList.innerHTML = '<div class="empty-state">Suggestions appear after a few quiz answers.</div>';
  next.forEach((recommendation) => { const button = document.createElement("button"); button.className = "continue-item"; button.type = "button"; button.textContent = recommendation.action || recommendation.topic_name; button.addEventListener("click", () => openStudySession(documentId, "quiz", recommendation.topic_id)); sessionRecommendationsList.appendChild(button); });
  loadDocumentProgress(documentId);
}

function quizCoverageText(mastery) {
  // concept_coverage_ratio = distinct concepts your quizzes have assessed / concepts the topic can be assessed on.
  const ratio = Number(mastery.concept_coverage_ratio);
  if (!Number.isFinite(ratio)) return "Coverage appears after an assessment.";
  const text = `${Math.round(ratio * 100)}% of this topic's key concepts covered by your quizzes`;
  return mastery.has_sufficient_evidence === false ? `${text} · a few more questions give a reliable picture` : text;
}

// ---- Document progress (Phase 6A): learning state, quiz results, study plan -------------------

let documentProgressRequest = 0;

async function loadDocumentProgress(documentId) {
  if (!sessionProgressState) return;
  const request = ++documentProgressRequest;
  [sessionProgressState, sessionProgressQuiz, sessionProgressPlan].forEach((element) => {
    element.innerHTML = '<p class="empty-state">Loading…</p>';
  });
  try {
    const progress = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(documentId)}?utc_offset_minutes=${plannerUtcOffsetMinutes()}`);
    if (request !== documentProgressRequest || activeDocumentId !== documentId) return;   // switched document meanwhile
    renderDocumentProgress(progress);
  } catch (error) {
    if (request !== documentProgressRequest) return;
    [sessionProgressState, sessionProgressQuiz, sessionProgressPlan].forEach((element) => {
      element.innerHTML = '<p class="empty-state">Progress could not be loaded right now.</p>';
    });
  }
}

function progressLine(parent, text, className = "") {
  const line = document.createElement("p");
  if (className) line.className = className;
  line.textContent = text;
  parent.appendChild(line);
  return line;
}

function progressDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function renderDocumentProgress(progress) {
  sessionProgressState.innerHTML = "";
  const badge = progressLine(sessionProgressState, progress.learning.label, `progress-state progress-state--${progress.learning.state}`);
  badge.setAttribute("data-state", progress.learning.state);
  progressLine(sessionProgressState, progress.learning.explanation, "progress-note");
  if (progress.flashcards.card_count) {
    progressLine(sessionProgressState, `${progress.flashcards.card_count} flashcard${progress.flashcards.card_count === 1 ? "" : "s"} ready`, "progress-note");
  }

  sessionProgressQuiz.innerHTML = "";
  const quiz = progress.quiz;
  if (!quiz.latest) {
    progressLine(sessionProgressQuiz, "No quiz results yet. Take a quiz to see your score here.", "empty-state");
  } else {
    progressLine(sessionProgressQuiz, `${Math.round(quiz.latest.percentage)}%`, "progress-figure");
    progressLine(sessionProgressQuiz, `${quiz.latest.score}/${quiz.latest.total} correct · ${progressDate(quiz.latest.completed_at)}`, "progress-note");
    const previous = quiz.attempts.slice(0, -1);
    if (quiz.trend) {
      const change = Math.round(quiz.attempts[quiz.attempts.length - 1].percentage - previous[previous.length - 1].percentage);
      const trendText = {
        improving: `Up ${change} points from your previous attempt`,
        declining: `Down ${Math.abs(change)} points from your previous attempt`,
        steady: "About the same as your previous attempt",
      }[quiz.trend];
      progressLine(sessionProgressQuiz, trendText, `progress-trend progress-trend--${quiz.trend}`);
      const list = document.createElement("ul");
      list.className = "progress-attempts";
      previous.slice(-4).reverse().forEach((attempt) => {
        const item = document.createElement("li");
        item.textContent = `${Math.round(attempt.percentage)}% · ${progressDate(attempt.completed_at)}`;
        list.appendChild(item);
      });
      const label = progressLine(sessionProgressQuiz, "Earlier attempts", "progress-subhead");
      label.after(list);
    } else {
      progressLine(sessionProgressQuiz, "One completed attempt so far. A later attempt will show how your score changes.", "progress-note");
    }
  }

  sessionProgressPlan.innerHTML = "";
  const plan = progress.plan;
  if (!plan) {
    progressLine(sessionProgressPlan, "Not in an active study plan. Add it in Study Planner to schedule sessions.", "empty-state");
    return;
  }
  progressLine(sessionProgressPlan, `${plan.completed_sessions} of ${plan.planned_sessions} session${plan.planned_sessions === 1 ? "" : "s"} done`, "progress-figure progress-figure--small");
  progressLine(sessionProgressPlan, `${plannerFormatDuration(plan.completed_minutes)} studied · ${plannerFormatDuration(plan.remaining_minutes)} still planned`, "progress-note");
  progressLine(sessionProgressPlan, plan.next_session
    ? `Next: ${PLANNER_ACTIVITY_LABELS[plan.next_session.activity_type] || plan.next_session.activity_type} · ${plannerDayLabel(plan.next_session.scheduled_start.slice(0, 10))}, ${plan.next_session.scheduled_start.slice(11, 16)}${plan.next_session.status === "in_progress" ? " (in progress)" : ""}`
    : "No upcoming session for this document.", "progress-note");
  if (plan.deadline) progressLine(sessionProgressPlan, `Deadline: ${plannerDayLabel(plan.deadline)}`, "progress-note");
}

function setSourcesDrawerOpen(isOpen) {
  tutorLayout.classList.toggle("sources-open", isOpen);
  toggleConversationSourcesButton.setAttribute("aria-expanded", String(isOpen));
  conversationSourcesPanel.setAttribute("aria-hidden", String(!isOpen));
  if (isOpen) {
    renderConversationSources();
    conversationSourcesPanel.querySelector("input, button")?.focus({ preventScroll: true });
  }
}

function closeSourcesDrawer() {
  setSourcesDrawerOpen(false);
}

function updateConfidence(value) {
  state.confidence = Math.max(0, Math.min(100, value));
  if (confidenceLabel) confidenceLabel.textContent = `${state.confidence}%`;
  if (confidenceBar) confidenceBar.style.width = `${state.confidence}%`;
  confidencePill.textContent = Math.max(1, Math.round(state.confidence / 20));
}

function appendInlineMarkdown(parent, text) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (!part) {
      return;
    }

    if (part.startsWith("**") && part.endsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = part.slice(2, -2);
      parent.appendChild(strong);
      return;
    }

    parent.appendChild(document.createTextNode(part));
  });
}

function appendFormattedText(container, text) {
  const blocks = String(text || "")
    .replace(/\r\n/g, "\n")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean);

  blocks.forEach((block) => {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    const isList = lines.every((line) => /^[-*]\s+/.test(line));

    if (isList) {
      const list = document.createElement("ul");
      lines.forEach((line) => {
        const item = document.createElement("li");
        appendInlineMarkdown(item, line.replace(/^[-*]\s+/, ""));
        list.appendChild(item);
      });
      container.appendChild(list);
      return;
    }

    lines.forEach((line) => {
      const paragraph = document.createElement("p");
      appendInlineMarkdown(paragraph, line);
      container.appendChild(paragraph);
    });
  });
}

function addMessage(text, type, isLoading = false) {
  const message = document.createElement("div");
  message.className = `message ${type}-message`;
  if (isLoading) {
    message.classList.add("loading-message");
  }

  const body = document.createElement("div");
  body.className = "message-body";
  appendFormattedText(body, text);
  message.appendChild(body);

  if (type === "tutor") {
    const row = document.createElement("div");
    row.className = "agent-row";

    const avatar = document.createElement("div");
    avatar.className = "agent-avatar";
    avatar.textContent = "AI";

    row.append(avatar, message);
    messageList.appendChild(row);
    messageList.scrollTop = messageList.scrollHeight;
    return row;
  } else {
    messageList.appendChild(message);
  }

  messageList.scrollTop = messageList.scrollHeight;
  return message;
}

function appendMessageMeta(messageElement, status, citations = []) {
  const bubble = messageElement.classList?.contains("agent-row")
    ? messageElement.querySelector(".message")
    : messageElement;
  if (!bubble || (!status && !citations.length)) return;

  const meta = document.createElement("div");
  meta.className = "message-grounding";
  if (status) {
    const badge = document.createElement("span");
    badge.className = `grounding-badge ${status}`;
    badge.textContent = status === "supported" ? "Grounded" : "Insufficient context";
    meta.appendChild(badge);
  }
  if (citations.length) {
    const cite = document.createElement("span");
    cite.className = "citation-count";
    cite.textContent = `${citations.length} citation${citations.length === 1 ? "" : "s"}`;
    meta.appendChild(cite);
  }
  bubble.appendChild(meta);
}

function renderSources(sources, mode = "uploaded") {
  if (!sourceList) {
    return;
  }

  sourceList.innerHTML = "";

  if (!sources.length) {
    const empty = document.createElement("button");
    empty.className = "source-card";
    empty.type = "button";

    const title = document.createElement("strong");
    title.textContent = mode === "citations" ? "No retrieved sources" : "No uploaded files";

    const meta = document.createElement("span");
    meta.textContent = mode === "citations" ? "Ask another question to retrieve context." : "Upload PDF or TXT material to start.";

    empty.append(title, meta);
    sourceList.appendChild(empty);
    return;
  }

  sources.forEach((source, index) => {
    const card = document.createElement(mode === "citations" ? "button" : "article");
    card.className = `source-card ${mode === "uploaded" ? "source-item" : ""}`;
    if (mode === "citations") {
      card.type = "button";
    }

    const title = document.createElement("strong");
    title.textContent = mode === "citations" ? `[${source.sourceId || index + 1}] ${source.title || "Unknown source"}` : source.title;

    const meta = document.createElement("span");
    meta.textContent = mode === "citations" ? `Page ${source.page || "Unknown page"}` : `${source.chunks} chunks indexed`;

    if (mode === "uploaded") {
      const header = document.createElement("div");
      header.className = "source-card-header";

      const titleBlock = document.createElement("div");
      titleBlock.className = "source-title";
      titleBlock.append(title, meta);

      const deleteButton = document.createElement("button");
      deleteButton.className = "delete-source-button";
      deleteButton.type = "button";
      deleteButton.textContent = "Delete";
      deleteButton.addEventListener("click", () => deleteUploadedSource(source, deleteButton));

      header.append(titleBlock, deleteButton);
      card.appendChild(header);
    } else {
      card.append(title, meta);
    }

    if (mode === "citations") {
      const detail = document.createElement("div");
      detail.className = "source-detail";
      appendFormattedText(detail, source.content || "No retrieved content preview is available for this source.");
      card.appendChild(detail);

      card.addEventListener("click", () => {
        sourceList.querySelectorAll(".source-card").forEach((item) => item.classList.remove("active"));
        card.classList.add("active");
      });
    }

    sourceList.appendChild(card);
  });
}

async function deleteUploadedSource(source, button) {
  const title = source.title || "";
  if (!title) {
    return;
  }
  if (!window.confirm(
    `Delete "${title}"? This permanently removes the document, its quizzes, attempts, summaries, `
    + "flashcards, and progress data. This cannot be undone."
  )) {
    return;
  }

  button.disabled = true;
  button.textContent = "Deleting";

  try {
    const response = await fetch(`${DELETE_SOURCE_API_URL}/${encodeURIComponent(title)}`, {
      method: "DELETE"
    });

    if (!response.ok) {
      let detail = `Delete API returned ${response.status}`;
      try {
        const errorData = await response.json();
        detail = errorData.detail || detail;
      } catch (error) {
        // Keep the HTTP status when the backend does not return JSON.
      }
      throw new Error(detail);
    }

    const data = await response.json();
    uploadedSources = data.sources || [];
    renderSources(uploadedSources, "uploaded");
    renderConversationSources();
    await loadIndexedDocuments();
    await loadDashboard();

    if (currentQuiz?.document_id === data.deleted) {
      currentQuiz = null;
      currentAttempt = null;
      quizAnswers = {};
      renderAssessmentQuiz();
    }

    uploadStatus.textContent = `Deleted ${data.deleted}`;
    showToast("Document deleted");
  } catch (error) {
    button.disabled = false;
    button.textContent = "Delete";
    uploadStatus.textContent = error.message || "Delete failed.";
    showToast("Delete failed");
  }
}

function formatBackendAnswer(data) {
  return data.answer || "I could not generate an answer from the current materials.";
}

function getFallbackAnswer() {
  return "Mock response: Start by identifying the key concept from your Net-centric material, then compare it with one short example.";
}

async function requestTutorAnswer(userText) {
  const conversation = await ensureStudySessionConversation();
  const modelId = await ensureSelectedModelReady();

  const response = await fetch(`${CONVERSATIONS_API_URL}/${conversation.id}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      message: userText,
      // This is the registry's safe public ID (for example qwen-2.5-7b),
      // never the Ollama/Hugging Face runtime reference.
      model_id: modelId
    })
  });

  if (!response.ok) {
    let detail = `Chat API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = errorData.detail || detail;
    } catch (error) {
      // Keep the HTTP status when the backend does not return JSON.
    }
    throw new Error(detail);
  }

  return response.json();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `Request returned ${response.status}`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (error) {
      // Keep the HTTP status for non-JSON responses.
    }
    if (response.status === 401 && currentUser) showAuthentication();
    throw new Error(detail);
  }
  return response.json();
}

function renderConversationSources() {
  conversationSourceList.innerHTML = "";
  if (!uploadedSources.length) {
    conversationSourceList.innerHTML = '<p class="muted">No materials yet. Open Materials to upload one.</p>';
    return;
  }
  const selected = new Set(activeConversation?.document_ids || []);
  const citationsByTitle = new Map();
  (activeConversation?.messages || []).forEach((message) => {
    (message.citations || []).forEach((citation) => {
      const title = citation.title || citation.document_id || "Unknown source";
      if (!citationsByTitle.has(title)) citationsByTitle.set(title, []);
      citationsByTitle.get(title).push(citation);
    });
  });
  uploadedSources.forEach((source) => {
    const label = document.createElement("label");
    label.className = "conversation-source-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = source.title;
    checkbox.checked = selected.has(source.title);
    const text = document.createElement("span");
    text.innerHTML = `<strong></strong><small></small><span class="source-citation-details"></span>`;
    text.querySelector("strong").textContent = source.title;
    text.querySelector("small").textContent = `${source.chunks} chunks`;
    const details = text.querySelector(".source-citation-details");
    const citations = citationsByTitle.get(source.title) || [];
    const unique = [...new Set(citations.map((citation) => {
      const page = citation.page ? `Page ${citation.page}` : "Page unavailable";
      const chunk = citation.chunk ?? citation.chunk_id;
      return chunk !== undefined && chunk !== null ? `${page} · Chunk ${chunk}` : page;
    }))];
    details.textContent = unique.length ? unique.join(" · ") : "Available to this session";
    label.append(checkbox, text);
    conversationSourceList.appendChild(label);
  });
}

function renderConversationList() {
  conversationList.innerHTML = "";
  if (!conversations.length) {
    conversationList.innerHTML = '<p class="muted">No conversations yet.</p>';
    return;
  }
  conversations.forEach((conversation) => {
    const row = document.createElement("div");
    row.className = `conversation-item ${conversation.id === activeConversation?.id ? "active" : ""}`;
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "conversation-open-button";
    const title = document.createElement("strong");
    title.textContent = conversation.title;
    const meta = document.createElement("span");
    meta.textContent = `${conversation.document_ids?.length || 0} source(s)`;
    openButton.append(title, meta);
    openButton.addEventListener("click", () => openConversation(conversation.id));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "conversation-delete-button";
    deleteButton.textContent = "×";
    deleteButton.title = "Delete conversation";
    deleteButton.addEventListener("click", () => deleteChatConversation(conversation.id));
    row.append(openButton, deleteButton);
    conversationList.appendChild(row);
  });
}

function renderConversationMessages() {
  messageList.innerHTML = "";
  const messages = activeConversation?.messages || [];
  if (!messages.length) {
    addMessage("Ask a question about the selected materials. This conversation and its answers will be saved automatically.", "tutor");
    return;
  }
  messages.forEach((message) => {
    const element = addMessage(message.content, message.role === "user" ? "user" : "tutor");
    if (message.role === "assistant") {
      appendMessageMeta(element, message.grounding_status, message.citations || []);
    }
  });
}

function updateConversationHeader() {
  chatConversationTitle.textContent = activeConversation?.title || "Current session";
  const count = activeConversation?.document_ids?.length || 0;
  chatSourceSummary.textContent = count ? "Grounded to selected material" : "Select material to ground answers";
  toggleConversationSourcesButton.textContent = `Sources (${count})`;
}

async function openConversation(conversationId) {
  try {
    activeConversation = await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`);
    if (currentUser) localStorage.setItem(`activeConversationId:${currentUser.id}`, conversationId);
    renderConversationList();
    renderConversationSources();
    renderConversationMessages();
    updateConversationHeader();
  } catch (error) {
    showToast(error.message || "Could not open conversation");
  }
}

async function createChatConversation() {
  try {
    const documentItem = getActiveStudySessionDocument();
    const documentIds = [documentItem.id];
    const created = await fetchJson(CONVERSATIONS_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "New conversation", document_ids: documentIds })
    });
    conversations.unshift(created);
    await openConversation(created.id);
    showToast("New conversation created");
  } catch (error) {
    showToast(error.message || "Could not create conversation");
  }
}

async function deleteChatConversation(conversationId) {
  try {
    await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`, { method: "DELETE" });
    conversations = conversations.filter((item) => item.id !== conversationId);
    if (activeConversation?.id === conversationId) {
      activeConversation = null;
      if (conversations.length) await openConversation(conversations[0].id);
      else await createChatConversation();
    }
    renderConversationList();
  } catch (error) {
    showToast(error.message || "Could not delete conversation");
  }
}

async function loadConversations() {
  conversations = await fetchJson(CONVERSATIONS_API_URL);
  if (!conversations.length) {
    activeConversation = null;
    renderConversationList();
    renderConversationMessages();
    updateConversationHeader();
    return;
  }
  const savedId = currentUser ? localStorage.getItem(`activeConversationId:${currentUser.id}`) : null;
  const initial = conversations.some((item) => item.id === savedId) ? savedId : conversations[0].id;
  await openConversation(initial);
}

async function applyConversationSources() {
  if (!activeConversation?.id) return;
  const documentIds = Array.from(conversationSourceList.querySelectorAll("input:checked"), (input) => input.value);
  try {
    activeConversation = await fetchJson(`${CONVERSATIONS_API_URL}/${activeConversation.id}/sources`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds })
    });
    conversations = await fetchJson(CONVERSATIONS_API_URL);
    renderConversationList();
    renderConversationSources();
    updateConversationHeader();
    closeSourcesDrawer();
    showToast("Conversation sources updated");
  } catch (error) {
    showToast(error.message || "Could not update sources");
  }
}

async function sendTutorMessage(userText) {
  addMessage(userText, "user");
  chatInput.value = "";
  chatInput.disabled = true;
  const submitButton = chatForm.querySelector("button");
  submitButton.disabled = true;
  chatForm.classList.add("is-sending");
  submitButton.textContent = "Sending";
  const loadingRow = addMessage("Tutoring is retrieving your course materials and asking the local model...", "tutor", true);

  try {
    const data = await requestTutorAnswer(userText);
    loadingRow.remove();
    const answerElement = addMessage(formatBackendAnswer(data), "tutor");
    appendMessageMeta(answerElement, data.grounding_status, data.citations || []);
    activeConversation.messages = [...(activeConversation.messages || []), data.user_message, data.assistant_message];
    conversations = await fetchJson(CONVERSATIONS_API_URL);
    activeConversation.title = conversations.find((item) => item.id === activeConversation.id)?.title || activeConversation.title;
    renderConversationList();
    updateConversationHeader();
  } catch (error) {
    loadingRow.remove();
    const detail = error.message || "Backend request failed.";
    console.error("AI Tutor message submission failed", error);
    addMessage(`I could not reach the RAG answer right now.\n\n${detail}\n\nCheck that FastAPI is running with the project virtual environment and Ollama is still running.`, "tutor");
    showToast("RAG request failed");
  } finally {
    chatInput.disabled = false;
    submitButton.disabled = false;
    chatForm.classList.remove("is-sending");
    submitButton.textContent = "Send";
    chatInput.focus();
  }
}

async function handleChatSubmit(event) {
  event.preventDefault();
  const userText = chatInput.value.trim();
  if (!userText) {
    showToast("Type a question first");
    return;
  }
  if (chatForm.classList.contains("is-sending")) return;
  await sendTutorMessage(userText);
}

async function loadUploadedSources() {
  if (!sourceList) {
    return;
  }

  try {
    const response = await fetch(SOURCES_API_URL);
    if (!response.ok) {
      throw new Error(`Sources API returned ${response.status}`);
    }

    uploadedSources = await response.json();
    renderSources(uploadedSources, "uploaded");
    renderConversationSources();
  } catch (error) {
    renderSources(
      [
        {
          title: "Sources unavailable",
          chunks: "Start the FastAPI backend to load uploaded files."
        }
      ],
      "uploaded"
    );
  }
}

async function uploadSourceFiles(files) {
  if (!files.length) {
    return;
  }

  const formData = new FormData();
  Array.from(files).forEach((file) => formData.append("files", file));

  uploadSourceButton.disabled = true;
  sourceFileInput.disabled = true;
  uploadStatus.textContent = `Indexing ${files.length} file${files.length === 1 ? "" : "s"}...`;

  try {
    const response = await fetch(UPLOAD_API_URL, {
      method: "POST",
      body: formData
    });

    if (!response.ok) {
      let detail = `Upload API returned ${response.status}`;
      try {
        const errorData = await response.json();
        detail = errorData.detail || detail;
      } catch (error) {
        // Keep the HTTP status when the backend does not return JSON.
      }
      throw new Error(detail);
    }

    const data = await response.json();
    uploadedSources = data.sources || [];
    renderSources(uploadedSources, "uploaded");
    renderConversationSources();
    await loadIndexedDocuments();
    await loadDashboard();

    uploadStatus.textContent = `${data.new_files} new file(s), ${data.new_chunks} new chunk(s) indexed`;
    showToast(data.skipped_files?.length ? "Some files were already indexed" : "Material uploaded");
  } catch (error) {
    const detail = error.message || "Upload failed.";
    uploadStatus.textContent = detail;
    showToast("Upload failed");
  } finally {
    uploadSourceButton.disabled = false;
    sourceFileInput.disabled = false;
    sourceFileInput.value = "";
  }
}

function updateAssessmentSummary() {
  const total = currentQuiz?.questions?.length || 0;
  const answered = Object.values(quizAnswers).filter(Boolean).length;
  const percentage = total ? Math.round((answered / total) * 100) : 0;
  quizProgressLabel.textContent = `${answered} / ${total} answered`;
  quizAccuracyLabel.textContent = currentAttempt?.completed
    ? `Attempt ${currentAttempt.attempt_number}: ${currentAttempt.score}/${currentAttempt.total} · ${Math.round(currentAttempt.percentage)}%`
    : (answered ? `${answered} of ${total} selected` : "Not started");
  if (quizProgressBar) {
    quizProgressBar.style.width = `${percentage}%`;
    quizProgressBar.parentElement.setAttribute("aria-valuenow", String(percentage));
  }

  const hasQuiz = Boolean(currentQuiz?.questions?.length);
  if (generateQuizButton) {
    generateQuizButton.textContent = hasQuiz ? (currentAttempt?.completed ? "Review Quiz" : "Start Quiz") : "Generate Quiz";
  }
  if (resetQuizButton) {
    resetQuizButton.disabled = !hasQuiz;
  }
  reviewQuizButton.hidden = !currentAttempt?.completed;
  if (newQuizButton) {
    newQuizButton.disabled = !hasQuiz;
  }
  if (deleteQuizButton) {
    deleteQuizButton.disabled = !hasQuiz;
  }
}

function getSelectedQuizStatus() {
  const documentId = quizDocumentSelect?.value;
  return quizStatuses.find((item) => item.document_id === documentId);
}

function selectedDifficulty() {
  return quizDifficultySelect?.value || "easy";
}

function selectedQuestionCount() {
  const value = Number(quizQuestionCountSelect?.value || 12);
  return [12, 15, 18, 20].includes(value) ? value : 12;
}

// A quiz always covers the whole document (there is no topic to choose); "document" is the id of that slot.
function selectedTopicId() {
  return "document";
}

function selectedQuizName() {
  return (quizNameInput?.value || "").trim();
}

function selectedQuizGenerationRequest() {
  const assessmentScope = selectedAssessmentScope();
  return {
    document_id: quizDocumentSelect?.value || "",
    assessment_scope: assessmentScope,
    topic_id: null,
    difficulty: selectedDifficulty(),
    question_count: selectedQuestionCount(),
    quiz_name: selectedQuizName(),
    model_id: selectedModelId || "qwen-2.5-7b"
  };
}

function quizGenerationRequestKey(request) {
  return [request.document_id, request.assessment_scope, request.topic_id || "document",
    request.difficulty, request.question_count, request.model_id].join("::");
}

function getActiveStudySessionDocument() {
  const documentItem = indexedDocuments.find((item) => item.id === activeDocumentId);
  if (!documentItem) {
    throw new Error("Open a Study Session before asking the AI Tutor.");
  }
  return documentItem;
}

async function ensureStudySessionConversation() {
  const documentItem = getActiveStudySessionDocument();
  const documentIds = [documentItem.id];
  const existing = conversations.find((conversation) =>
    conversation.document_id === documentItem.id ||
    (conversation.document_ids?.length === 1 && conversation.document_ids[0] === documentItem.id)
  );

  if (existing) {
    if (activeConversation?.id !== existing.id) await openConversation(existing.id);
    return activeConversation;
  }

  if (!activeConversation?.id || activeConversation.document_id !== documentItem.id) {
    const created = await fetchJson(CONVERSATIONS_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "New conversation", document_ids: documentIds })
    });
    conversations.unshift(created);
    await openConversation(created.id);
    return activeConversation;
  }
  return activeConversation;
}

async function ensureSelectedModelReady() {
  if (!generationModels.length) await loadGenerationModels();
  const model = generationModels.find((item) => item.id === selectedModelId);
  if (!model || !selectedModelId) {
    throw new Error("No available AI model is selected. Refresh the page and try again.");
  }
  if (!model.ready) {
    try {
      await fetchJson(`${MODELS_API_URL}/${encodeURIComponent(model.id)}/prepare`, { method: "POST" });
      model.ready = true;
    } catch (error) {
      throw new Error(error.message || `The selected model (${model.label}) could not be prepared.`);
    }
  }
  return model.id;
}

function masteryLevelClass(level) {
  return String(level || "Not assessed").toLowerCase().replace(/[^a-z]+/g, "-").replace(/^-|-$/g, "");
}

function createMasteryCard(mastery, options = {}) {
  const row = document.createElement("article");
  row.className = `mastery-row level-${masteryLevelClass(mastery.mastery_level)}`;
  const heading = document.createElement("div");
  heading.className = "mastery-row-heading";
  const titleBlock = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = mastery.topic_name || mastery.topic_id;
  const documentName = document.createElement("span");
  documentName.textContent = options.showDocument && mastery.document_name ? mastery.document_name : "";
  titleBlock.append(title, documentName);
  const result = document.createElement("div");
  result.className = "mastery-result";
  const score = mastery.has_evidence ? `${Math.round(Number(mastery.mastery_score || 0))}%` : "—";
  const scoreElement = document.createElement("b");
  scoreElement.textContent = score;
  const level = document.createElement("span");
  level.className = "mastery-level";
  level.textContent = mastery.mastery_level || "Not assessed";
  result.append(scoreElement, level);
  heading.append(titleBlock, result);

  const track = document.createElement("div");
  track.className = "progress-track mastery-score-track";
  const fill = document.createElement("span");
  fill.style.width = mastery.has_evidence ? `${Math.max(0, Math.min(100, Number(mastery.mastery_score || 0)))}%` : "0%";
  track.appendChild(fill);
  const capacity = Number(mastery.assessment_capacity || 0);
  const assessed = Number(mastery.distinct_concepts_assessed || 0);
  const meta = document.createElement("small");
  const coverage = capacity ? `Concept coverage: ${assessed} / ${capacity}` : "Concept coverage not available";
  meta.textContent = `${coverage} · ${Number(mastery.answered_questions || 0)} answered · ${Number(mastery.completed_attempts || 0)} completed quizzes`;
  row.append(heading, track, meta);
  return row;
}

function renderMasteryList(container, masteries, options = {}) {
  if (!container) return;
  container.innerHTML = "";
  if (!masteries.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = options.emptyText || "No extracted topics are available yet.";
    container.appendChild(empty);
    return;
  }
  masteries.forEach((mastery) => container.appendChild(createMasteryCard(mastery, options)));
}

function applySessionLibraryFilters() {
  const query = (sessionSearchInput?.value || "").trim().toLowerCase();
  const rows = [...overviewMaterialsList.querySelectorAll(".subject-card")];
  rows.forEach((row) => { row.hidden = Boolean(query && !row.dataset.title.includes(query)); });
  const sortMode = sessionSortSelect?.value || "name";
  rows.sort((a, b) => sortMode === "topics"
    ? Number(b.dataset.topicCount || 0) - Number(a.dataset.topicCount || 0)
    : a.dataset.title.localeCompare(b.dataset.title)
  ).forEach((row) => overviewMaterialsList.appendChild(row));
}

function renderSidebarRecentDocuments(materials) {
  if (!sidebarRecentDocuments) return;
  sidebarRecentDocuments.innerHTML = "";
  if (!materials.length) {
    sidebarRecentDocuments.innerHTML = '<span class="sidebar-empty">No documents yet</span>';
    return;
  }
  materials.slice(0, 5).forEach((material) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sidebar-recent-item";
    button.dataset.documentId = material.document_id;
    button.textContent = material.document_name;
    button.title = material.document_name;
    button.addEventListener("click", () => openStudySession(material.document_id));
    sidebarRecentDocuments.appendChild(button);
  });
  syncSidebarRecentActive();
}

// The open study session's entry in Recent is highlighted (only while a session is shown).
function syncSidebarRecentActive() {
  sidebarRecentDocuments?.querySelectorAll(".sidebar-recent-item").forEach((button) => {
    const active = document.body?.dataset?.page === "session" && button.dataset.documentId === activeDocumentId;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

// ---- Mobile navigation drawer ------------------------------------------------------------------
// At phone width the sidebar becomes an off-canvas drawer (see .sidebar-open in styles.css). It
// closes via the backdrop, the close button, Escape, or selecting any navigation inside it.
const sidebarMenuButton = document.getElementById("sidebar-menu-button");
const sidebarBackdrop = document.getElementById("sidebar-backdrop");

function setSidebarOpen(open) {
  if (!sidebarMenuButton) return;
  const wasOpen = document.body.classList.contains("sidebar-open");
  document.body.classList.toggle("sidebar-open", open);
  sidebarMenuButton.setAttribute("aria-expanded", String(open));
  if (sidebarBackdrop) sidebarBackdrop.hidden = !open;
  if (open) document.getElementById("sidebar-close-button")?.focus();
  else if (wasOpen && document.getElementById("app-sidebar")?.contains(document.activeElement)) sidebarMenuButton.focus();
}

function renderDashboard() {
  if (!dashboardData) return;
  const metrics = dashboardData.metrics || {};
  const documentCount = Number(metrics.documents || 0);
  const totalTopics = Number(metrics.total_topics || 0);
  const assessedTopics = Number(metrics.topics_assessed || 0);
  const masteredTopics = Number(metrics.topics_mastered || 0);
  const performance = metrics.current_quiz_performance || {};
  const assessedDocuments = Number(performance.assessed_documents || 0);
  const kpis = [
    ["Learning materials", String(documentCount), documentCount ? "Ready to study" : "Upload a PDF or TXT file"],
    ["Quiz performance", performance.average_percentage == null ? "—" : `${Math.round(performance.average_percentage)}%`,
      assessedDocuments ? `Latest quiz across ${assessedDocuments} document${assessedDocuments === 1 ? "" : "s"}` : "No quiz results yet"],
    ["Topics mastered", `${masteredTopics} / ${totalTopics}`, totalTopics ? `${assessedTopics} of ${totalTopics} assessed so far` : "No topics yet"],
  ];
  overviewKpis.innerHTML = "";
  kpis.forEach(([label, value, note]) => {
    const card = document.createElement("article");
    card.className = "stat-card";
    const labelElement = document.createElement("span"); labelElement.textContent = label;
    const valueElement = document.createElement("strong"); valueElement.textContent = value;
    const noteElement = document.createElement("small"); noteElement.textContent = note;
    card.append(labelElement, valueElement, noteElement);
    overviewKpis.appendChild(card);
  });

  const materials = dashboardData.materials || [];
  renderSubjectCards(dashboardData.subjects || []);
  renderSidebarRecentDocuments(materials);
  applySessionLibraryFilters();
}

function subjectQuickActionButton(label, onClick, title) {
  const button = document.createElement("button");
  button.className = "text-button subject-quick-action";
  button.type = "button";
  button.textContent = label;
  if (title) button.title = title;
  button.addEventListener("click", (event) => { event.stopPropagation(); onClick(); });
  return button;
}

function renderSubjectCards(subjects) {
  overviewMaterialsList.innerHTML = "";
  if (!subjects.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No indexed documents yet.";
    overviewMaterialsList.appendChild(empty);
    return;
  }
  subjects.forEach((subject) => {
    const card = document.createElement("article");
    card.className = "subject-card";
    card.dataset.title = (subject.subject_name || "").toLowerCase();
    card.dataset.topicCount = String(subject.topic_count || 0);

    const header = document.createElement("div");
    header.className = "subject-card-header";
    const title = document.createElement("strong");
    title.textContent = subject.subject_name;
    const meta = document.createElement("span");
    const documentCount = subject.document_count || 0;
    const topicCount = subject.topic_count || 0;
    meta.textContent = `${documentCount} document${documentCount === 1 ? "" : "s"} · ${topicCount} topic${topicCount === 1 ? "" : "s"}`;
    header.append(title, meta);

    const progress = document.createElement("div");
    progress.className = "subject-progress";
    if (subject.progress_percent == null) {
      progress.textContent = "No assessed topics yet";
    } else {
      const percent = Math.round(subject.progress_percent);
      const track = document.createElement("div"); track.className = "subject-progress-track";
      const bar = document.createElement("span"); bar.className = "subject-progress-bar"; bar.style.width = `${percent}%`;
      track.appendChild(bar);
      const label = document.createElement("small"); label.textContent = `${percent}% assessed`;
      progress.append(track, label);
    }

    const primaryDocumentId = subject.primary_document_id;
    const primaryMaterial = (dashboardData.materials || []).find((item) => item.document_id === primaryDocumentId);
    const primaryDocumentLabel = primaryMaterial ? primaryMaterial.document_name : primaryDocumentId;
    // Quiz/Flashcards/AI Tutor are intentionally document-scoped -- they open the existing,
    // unmodified per-document session for this subject's primary (most recently updated)
    // document. There is no subject-wide generation; the tooltip makes that scoping explicit
    // when a subject groups more than one document.
    const actions = document.createElement("div");
    actions.className = "subject-quick-actions";
    actions.append(
      subjectQuickActionButton("Quiz", () => openStudySession(primaryDocumentId, "quiz"),
        `Opens Quiz for ${primaryDocumentLabel}`),
      subjectQuickActionButton("Flashcards", () => openStudySession(primaryDocumentId, "flashcards"),
        `Opens Flashcards for ${primaryDocumentLabel}`),
      subjectQuickActionButton("AI Tutor", () => openStudySession(primaryDocumentId, "material"),
        `Opens AI Tutor for ${primaryDocumentLabel}`),
      subjectQuickActionButton("Study Planner", () => setPage("planner")),
    );

    const documentsList = document.createElement("div");
    documentsList.className = "subject-documents";
    (subject.document_ids || []).forEach((documentId) => {
      const material = (dashboardData.materials || []).find((item) => item.document_id === documentId);
      const row = document.createElement("div");
      row.className = "subject-document-row";
      const label = document.createElement("span");
      label.textContent = material ? material.document_name : documentId;
      const openButton = document.createElement("button");
      openButton.className = "text-button"; openButton.type = "button"; openButton.textContent = "Open →";
      openButton.addEventListener("click", (event) => { event.stopPropagation(); openStudySession(documentId); });
      const deleteButton = document.createElement("button");
      deleteButton.className = "text-button danger-button"; deleteButton.type = "button"; deleteButton.textContent = "Delete";
      deleteButton.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteUploadedSource({ title: documentId }, deleteButton);
      });
      row.append(label, openButton, deleteButton);
      documentsList.appendChild(row);
    });

    card.append(header, progress, actions, documentsList);
    card.addEventListener("click", (event) => {
      if (!event.target.closest("button") && primaryDocumentId) openStudySession(primaryDocumentId);
    });
    overviewMaterialsList.appendChild(card);
  });
}

async function loadDashboard() {
  try {
    dashboardData = await fetchJson(DASHBOARD_API_URL);
    renderDashboard();
  } catch (error) {
    dashboardData = null;
    overviewKpis.innerHTML = '<div class="empty-state">Dashboard data is unavailable.</div>';
  }
}

async function loadKnowledgeGaps() {
  try {
    knowledgeGaps = await fetchJson(KNOWLEDGE_GAPS_API_URL);
  } catch (error) {
    knowledgeGaps = [];
  }
  if (activeDocumentId) renderSessionProgress(activeDocumentId);
}

async function loadRecommendations() {
  try { recommendations = await fetchJson(RECOMMENDATIONS_API_URL); }
  catch (error) { recommendations = []; }
  if (activeDocumentId) renderSessionProgress(activeDocumentId);
}

function selectedAssessmentScope() {
  return "document";
}

function currentQuizKey() {
  return quizGenerationRequestKey(selectedQuizGenerationRequest());
}

// A saved quiz counts as "saved" for the current form only if it was made with the same model and
// number of questions. (A quiz saved before models were recorded has neither: it stays "saved".)
function variantMatchesSettings(variant) {
  const modelMatches = !variant.model_id && !variant.model_name || !selectedModelId || variant.model_id === selectedModelId;
  const countMatches = !variant.requested_count || variant.requested_count === selectedQuestionCount();
  return modelMatches && countMatches;
}

function updateDifficultyOptions() {
  if (!quizDifficultySelect) {
    return;
  }
  const savedLevels = (getSelectedQuizStatus()?.variants || [])
    .filter((variant) => variant.topic_id === selectedTopicId() && variantMatchesSettings(variant))
    .map((variant) => variant.difficulty);
  Array.from(quizDifficultySelect.options).forEach((option) => {
    const label = option.value.charAt(0).toUpperCase() + option.value.slice(1);
    option.textContent = savedLevels.includes(option.value) ? `${label} (saved)` : label;
  });
  renderQuizDifficultySegments();
}

// The Create Quiz sheet's visible Difficulty control: segmented buttons that mirror the real
// (now visually hidden) #quiz-difficulty-select -- same options/labels/"(saved)" hints, same value,
// same change event, so every existing difficulty-driven behavior keeps working unchanged.
function renderQuizDifficultySegments() {
  const container = document.getElementById("quiz-difficulty-segmented");
  if (!container || !quizDifficultySelect) return;
  container.innerHTML = "";
  Array.from(quizDifficultySelect.options).forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quiz-segmented-option";
    button.textContent = option.textContent;
    button.setAttribute("role", "radio");
    const selected = option.value === quizDifficultySelect.value;
    button.setAttribute("aria-checked", String(selected));
    button.classList.toggle("active", selected);
    button.addEventListener("click", () => {
      if (quizDifficultySelect.value === option.value) return;
      quizDifficultySelect.value = option.value;
      renderQuizDifficultySegments();
      quizDifficultySelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    container.appendChild(button);
  });
}

function formatQuizStatus(status) {
  const levelIsSaved = (status?.variants || []).some(
    (variant) => variant.topic_id === selectedTopicId() && variant.difficulty === selectedDifficulty() && variantMatchesSettings(variant)
  );
  if (!levelIsSaved && !currentQuiz) {
    return "Not generated";
  }
  if (currentAttempt?.completed) {
    return "Completed";
  }
  if (currentQuiz || levelIsSaved) {
    return "Ready";
  }
  return "Not generated";
}

async function loadQuizStatuses() {
  try {
    const response = await fetch(QUIZZES_API_URL);
    if (!response.ok) {
      throw new Error(`Quiz status API returned ${response.status}`);
    }
    quizStatuses = await response.json();
  } catch (error) {
    quizStatuses = [];
  }
}

async function loadIndexedDocuments() {
  if (!quizDocumentSelect) {
    return;
  }

  try {
    const response = await fetch(DOCUMENTS_API_URL);
    if (!response.ok) {
      throw new Error(`Documents API returned ${response.status}`);
    }

    indexedDocuments = await response.json();
    pcalRenderSheet();   // the planner's add-materials sheet may already be open
    await loadQuizStatuses();
    quizDocumentSelect.innerHTML = "";

    if (!indexedDocuments.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No indexed documents found";
      quizDocumentSelect.appendChild(option);
      updateAssessmentSummary();
      return;
    }

    indexedDocuments.forEach((documentItem) => {
      const option = document.createElement("option");
      option.value = documentItem.id;
      const status = quizStatuses.find((item) => item.document_id === documentItem.id);
      const levels = [...new Set((status?.variants || []).map((variant) => variant.difficulty))];
      const quizLabel = levels.length ? `saved: ${levels.join(", ")}` : "no quiz yet";
      option.textContent = `${documentItem.title} (${documentItem.chunks} chunks, ${quizLabel})`;
      quizDocumentSelect.appendChild(option);
    });

    await loadSelectedQuiz();
  } catch (error) {
    quizDocumentSelect.innerHTML = "";
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Could not load documents";
    quizDocumentSelect.appendChild(option);
    showToast("Could not load indexed documents");
    updateAssessmentSummary();
  }
}

async function handleQuizDocumentChange() {
  const documentId = quizDocumentSelect?.value || "";
  currentQuiz = null;
  currentAttempt = null;
  quizAttemptSummary = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  quizHistory = [];
  renderAssessmentQuiz();
  renderQuizHistory();
  await Promise.all([loadSelectedQuiz(), loadQuizHistory(documentId)]);
}

function setAssessmentLoading(isLoading) {
  assessmentLoading.classList.toggle("show", isLoading);
  generateQuizButton.disabled = isLoading;
  newQuizButton.disabled = isLoading;
  resetQuizButton.disabled = isLoading;
  if (deleteQuizButton) deleteQuizButton.disabled = isLoading;
  [quizNameInput, quizDocumentSelect, quizScopeSelect, quizDifficultySelect,
    quizQuestionCountSelect].forEach((control) => {
    if (control) control.disabled = isLoading;
  });
}

async function requestGeneratedQuiz(request) {
  // An explicit Generate/Create Quiz click always creates a new quiz artifact -- it must never be
  // silently answered with an older compatible quiz just because the settings match one.
  request = { ...request, regenerate: true };
  const response = await fetch(QUIZ_GENERATE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(request)
  });

  if (!response.ok) {
    let detail = `Quiz API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = typeof errorData.detail === "object"
        ? errorData.detail.message || JSON.stringify(errorData.detail)
        : errorData.detail || detail;
    } catch (error) {
      // Keep the status message if the backend does not return JSON.
    }
    throw new Error(detail);
  }

  return response.json();
}

// With quizId, the backend loads exactly that quiz artifact -- never a different (e.g. newer)
// quiz sharing the same document/topic/difficulty slot. Required now that several quizzes can
// share one slot (see backend/quiz_service.load_quiz_with_attempt).
async function requestQuizDetail(documentId, quizId) {
  const query = new URLSearchParams({ difficulty: selectedDifficulty(), topic_id: selectedTopicId() });
  if (quizId) query.set("quiz_id", quizId);
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(documentId)}?${query}`);
  if (!response.ok) {
    let detail = `Quiz detail API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = errorData.detail || detail;
    } catch (error) {
      // Keep the status message if the backend does not return JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

async function requestQuizRegeneration(documentId, request) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(documentId)}/regenerate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      difficulty: request.difficulty,
      assessment_scope: request.assessment_scope,
      topic_id: request.topic_id,
      question_count: request.question_count,
      model_id: request.model_id
    })
  });
  if (!response.ok) {
    let detail = `Regenerate API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = typeof errorData.detail === "object"
        ? errorData.detail.message || JSON.stringify(errorData.detail)
        : errorData.detail || detail;
    } catch (error) {
      // Keep the status message if the backend does not return JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

// `payload` carries quiz_id explicitly (never omitted for a live caller -- see
// backend/quiz_service.update_quiz_progress, which never falls back to "the newest quiz in this
// slot" once quiz_id is given) plus whichever of question_id/selected_answer/current_question_index
// changed.
async function requestQuizProgress(payload, quiz = currentQuiz) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(quiz.document_id)}/progress`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      difficulty: quiz.difficulty,
      topic_id: quiz.topic_id,
      quiz_id: quiz.quiz_id,
      ...payload
    })
  });

  if (!response.ok) {
    let detail = `Progress API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = errorData.detail || detail;
    } catch (error) {
      // Keep the status message if the backend does not return JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

async function requestQuizSubmission({ allowUnanswered = false } = {}) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      difficulty: currentQuiz.difficulty,
      topic_id: currentQuiz.topic_id,
      quiz_id: currentQuiz.quiz_id,
      answers: quizAnswers,
      ...(allowUnanswered ? { allow_unanswered: true } : {})
    })
  });
  if (!response.ok) {
    let detail = `Submit API returned ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (error) { /* Keep HTTP status. */ }
    throw new Error(detail);
  }
  return response.json();
}

async function requestQuizProgressReset() {
  const query = new URLSearchParams({ difficulty: currentQuiz.difficulty, topic_id: currentQuiz.topic_id });
  if (currentQuiz.quiz_id) query.set("quiz_id", currentQuiz.quiz_id);
  const response = await fetch(
    `${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/progress?${query}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    throw new Error(`Reset progress API returned ${response.status}`);
  }
  return response.json();
}

// ---- Quiz Player autosave -------------------------------------------------------------------
// Local state (quizAnswers/quizQuestionIndex) is always the source of truth for the UI -- autosave
// only mirrors it to the backend so Resume can restore it later. A failed or superseded autosave
// therefore never touches quizAnswers/quizQuestionIndex, only the subtle Saving/Saved/error status.

function setQuizPlayerSaveStatus(status) {
  const el = document.getElementById("quiz-player-save-status");
  if (!el) return;
  if (status === "saving") { el.hidden = false; el.textContent = "Saving…"; el.className = "quiz-player-save-status"; }
  else if (status === "saved") { el.hidden = false; el.textContent = "Saved"; el.className = "quiz-player-save-status is-saved"; }
  else if (status === "error") { el.hidden = false; el.textContent = "Could not save — will retry"; el.className = "quiz-player-save-status is-error"; }
  else { el.hidden = true; }
}

// Debounced (~500ms) so rapid answer changes or navigation do not fire one request per click.
// Every save sends the FULL local snapshot (all answers + position), so a later navigation save
// can never drop an answer made just before it, and only one request is in flight at a time, so
// an older snapshot can never land on the server after a newer one.
function scheduleQuizAutosave() {
  if (!currentQuiz?.quiz_id) return;
  quizAutosaveDirty = true;
  setQuizPlayerSaveStatus("saving");
  if (quizAutosaveTimer) clearTimeout(quizAutosaveTimer);
  quizAutosaveTimer = setTimeout(() => { flushQuizAutosave().catch(() => {}); }, 500);
}

// Resolves once everything changed so far is saved; rejects if the latest save failed (the local
// state is kept and a retry is scheduled). Exit and Finish await this before leaving/submitting.
async function flushQuizAutosave() {
  if (quizAutosaveTimer) { clearTimeout(quizAutosaveTimer); quizAutosaveTimer = null; }
  while (quizAutosaveInFlight) {
    try { await quizAutosaveInFlight; } catch (error) { /* retried below while still dirty */ }
  }
  if (!quizAutosaveDirty || !currentQuiz?.quiz_id) return;
  const quizId = currentQuiz.quiz_id;
  const seq = ++quizAutosaveSeq;
  quizAutosaveDirty = false;
  const request = requestQuizProgress({ answers: { ...quizAnswers }, current_question_index: quizQuestionIndex });
  quizAutosaveInFlight = request;
  let saved;
  try {
    saved = await request;
  } catch (error) {
    if (quizAutosaveInFlight === request) quizAutosaveInFlight = null;
    if (currentQuiz?.quiz_id === quizId && seq === quizAutosaveSeq) {
      quizAutosaveDirty = true;
      setQuizPlayerSaveStatus("error");
      if (quizPlayerOpen && !quizAutosaveTimer) {
        quizAutosaveTimer = setTimeout(() => { flushQuizAutosave().catch(() => {}); }, 4000);
      }
    }
    throw error;
  }
  if (quizAutosaveInFlight === request) quizAutosaveInFlight = null;
  if (currentQuiz?.quiz_id !== quizId || seq !== quizAutosaveSeq) return;   // exited/reset meanwhile
  // Local answers/position stay the source of truth -- only attempt metadata comes from the server.
  currentAttempt = saved;
  if (quizAutosaveDirty) return flushQuizAutosave();
  setQuizPlayerSaveStatus("saved");
}

// Leaving the Quiz Player without awaiting (document/session switch, or Exit after a failed save):
// captures the newest local snapshot BEFORE the player state is reset, then persists it to that
// snapshot's own quiz. It queues behind any in-flight save (so the newest snapshot lands last),
// retries a few times since the learner has already moved on, and never touches the new
// session's state. resetQuizAutosave() makes any older in-flight response stale.
function detachQuizAutosave() {
  const pending = quizAutosaveDirty || Boolean(quizAutosaveInFlight);
  const snapshot = pending && currentQuiz?.quiz_id && !currentAttempt?.completed ? {
    quiz: {
      document_id: currentQuiz.document_id, difficulty: currentQuiz.difficulty,
      topic_id: currentQuiz.topic_id, quiz_id: currentQuiz.quiz_id,
    },
    payload: { answers: { ...quizAnswers }, current_question_index: quizQuestionIndex },
  } : null;
  resetQuizAutosave();
  if (!snapshot) return Promise.resolve();
  quizDetachedSave = persistDetachedQuizSnapshot(snapshot).catch(() => {});
  return quizDetachedSave;
}

async function persistDetachedQuizSnapshot(snapshot, maxAttempts = 3) {
  for (let attempt = 1; ; attempt += 1) {
    while (quizAutosaveInFlight) {
      try { await quizAutosaveInFlight; } catch (error) { /* this snapshot supersedes it */ }
    }
    const request = requestQuizProgress(snapshot.payload, snapshot.quiz);
    quizAutosaveInFlight = request;
    try {
      await request;
      return;
    } catch (error) {
      if (attempt >= maxAttempts) throw error;
    } finally {
      if (quizAutosaveInFlight === request) quizAutosaveInFlight = null;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
  }
}

function resetQuizAutosave() {
  if (quizAutosaveTimer) clearTimeout(quizAutosaveTimer);
  quizAutosaveTimer = null;
  quizAutosaveDirty = false;
  quizAutosaveSeq += 1;
  setQuizPlayerSaveStatus(null);
}

async function requestQuizExplanation(questionId) {
  const response = await fetch(
    `${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/questions/${questionId}/explain`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        difficulty: currentQuiz.difficulty,
        topic_id: currentQuiz.topic_id
      })
    }
  );
  if (!response.ok) {
    let detail = `Explain API returned ${response.status}`;
    try {
      const errorData = await response.json();
      detail = errorData.detail || detail;
    } catch (error) {
      // Keep the status message when the backend response is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

async function loadQuizHistory(documentId = quizDocumentSelect?.value || activeDocumentId || "") {
  const requestDocumentId = documentId;
  quizHistory = [];
  renderQuizHistory();
  if (!requestDocumentId) return;
  try {
    const query = new URLSearchParams({ document_id: requestDocumentId });
    const response = await fetch(`${QUIZ_HISTORY_API_URL}?${query}`);
    if (!response.ok) {
      throw new Error(`Quiz history API returned ${response.status}`);
    }
    const history = await response.json();
    if ((quizDocumentSelect?.value || "") !== requestDocumentId) return;
    quizHistory = history.filter((attempt) => attempt.document_id === requestDocumentId);
  } catch (error) {
    if ((quizDocumentSelect?.value || "") !== requestDocumentId) return;
    quizHistory = [];
  }
  renderQuizHistory();
}

async function requestQuizHistoryDetail(attemptId) {
  const response = await fetch(`${QUIZ_HISTORY_API_URL}/${encodeURIComponent(attemptId)}`);
  if (!response.ok) {
    throw new Error(`Quiz history detail API returned ${response.status}`);
  }
  return response.json();
}

async function requestQuizForRetake(attemptId) {
  const response = await fetch(`${QUIZ_HISTORY_API_URL}/${encodeURIComponent(attemptId)}/retake`);
  if (!response.ok) {
    let detail = `Retake API returned ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (error) { /* Keep HTTP status. */ }
    throw new Error(detail);
  }
  return response.json();
}

// What the Quizzes screen lists for the open document: completed quizzes (attempts), plus quizzes that
// are still being generated and saved quizzes (also partial ones) not attempted yet -- a quiz must not
// look "missing" only because nobody has completed it.
function quizListState(groups) {
  const passesFilters = (difficulty) => quizHistoryDifficultyFilter === "all" || difficulty === quizHistoryDifficultyFilter;
  const activeDocument = quizDocumentSelect?.value || activeDocumentId;
  const attemptedQuizIds = new Set(groups.map((group) => group.quizId));
  const savedVariants = (quizStatuses.find((item) => item.document_id === activeDocument)?.variants || [])
    .filter((variant) => variant.quiz_id && variant.topic_id === "document" && !attemptedQuizIds.has(variant.quiz_id));
  const pending = [...pendingQuizGenerations.values()].filter((item) => item.documentId === activeDocument);
  const visibleGroups = groups.filter(({ latest }) => passesFilters(latest.difficulty));
  const visibleSaved = savedVariants.filter((variant) => passesFilters(variant.difficulty));
  const visiblePending = pending.filter((item) => passesFilters(item.difficulty));
  let emptyMessage = "";
  if (!groups.length && !savedVariants.length && !pending.length) emptyMessage = "No quizzes yet. Create one to get started.";
  else if (!visibleGroups.length && !visibleSaved.length && !visiblePending.length) emptyMessage = "No quizzes match these filters.";
  return { visibleGroups, visibleSaved, visiblePending, emptyMessage };
}

function renderQuizHistory() {
  if (!quizHistoryList) {
    return;
  }
  quizHistoryList.innerHTML = "";
  const groups = Object.values(quizHistory.reduce((byQuiz, attempt) => {
    const key = attempt.quiz_id || `legacy:${attempt.document_id}:${attempt.topic_id}:${attempt.difficulty}`;
    if (!byQuiz[key]) byQuiz[key] = { quizId: key, attempts: [] };
    byQuiz[key].attempts.push(attempt);
    return byQuiz;
  }, {})).map((group) => {
    group.attempts.sort((left, right) => new Date(right.completed_at || 0) - new Date(left.completed_at || 0));
    group.latest = group.attempts[0];
    group.best = Math.max(...group.attempts.map((attempt) => Number(attempt.percentage || 0)));
    group.average = group.attempts.reduce((sum, attempt) => sum + Number(attempt.percentage || 0), 0) / group.attempts.length;
    return group;
  }).sort((left, right) => new Date(right.latest.completed_at || 0) - new Date(left.latest.completed_at || 0));

  let filters = quizHistoryList.parentElement?.querySelector(".quiz-history-filters");
  if (!filters) {
    filters = document.createElement("div");
    filters.className = "quiz-history-filters";
    const makeFilter = (labelText, values, onChange) => {
      const label = document.createElement("label");
      label.append(document.createTextNode(labelText));
      const select = document.createElement("select");
      values.forEach(([value, text]) => select.add(new Option(text, value)));
      select.addEventListener("change", () => onChange(select.value));
      label.appendChild(select);
      return { label, select };
    };
    const difficulty = makeFilter("Difficulty", [["all", "All"], ["easy", "Easy"], ["medium", "Medium"], ["difficult", "Difficult"]], (value) => {
      quizHistoryDifficultyFilter = value;
      renderQuizHistory();
    });
    filters.append(difficulty.label);
    quizHistoryList.before(filters);
  }
  const filterSelects = filters.querySelectorAll("select");
  filterSelects[0].value = quizHistoryDifficultyFilter;

  const { visibleGroups, visibleSaved, visiblePending, emptyMessage } = quizListState(groups);
  if (emptyMessage) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = emptyMessage;
    quizHistoryList.appendChild(empty);
    // The true empty state (no quizzes at all, as opposed to "no filters match") also gets its
    // own Create Quiz entry point, alongside the landing header's "+ New Quiz".
    if (emptyMessage.startsWith("No quizzes yet")) {
      const create = document.createElement("button");
      create.className = "primary-button quiz-empty-create-button";
      create.type = "button";
      create.textContent = "Create Quiz";
      create.addEventListener("click", openQuizCreateDialog);
      quizHistoryList.appendChild(create);
    }
    return;
  }
  visiblePending.forEach((pending) => quizHistoryList.appendChild(createPendingQuizCard(pending)));
  visibleSaved.forEach((variant) => quizHistoryList.appendChild(createSavedQuizCard(variant)));

  visibleGroups.forEach((group) => {
    const attempt = group.latest;
    const card = document.createElement("article");
    card.className = "quiz-history-card";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = (attempt.title || "").trim() || "Untitled Quiz";
    const scopeName = attempt.topic_id === "document" ? "Entire Document" : (attempt.topic_name || attempt.topic_id || "Topic");
    const meta = document.createElement("small");
    meta.className = "quiz-history-meta";
    meta.textContent = `${scopeName} · ${attempt.difficulty}`;
    const date = document.createElement("small");
    date.textContent = attempt.completed_at ? `Latest activity ${new Date(attempt.completed_at).toLocaleString()}` : "Activity time unavailable";
    const status = document.createElement("span");
    status.className = "quiz-history-scope";
    status.textContent = "Completed";
    const scope = document.createElement("small");
    scope.textContent = `${attempt.total} questions · ${group.attempts.length} attempt${group.attempts.length === 1 ? "" : "s"}`;
    info.append(title, meta, status, scope, date);

    const score = document.createElement("div");
    score.className = "quiz-history-score";
    score.innerHTML = `<span>Latest <strong>${Math.round(attempt.percentage)}%</strong></span><span>Best ${Math.round(group.best)}%</span><span>Average ${Math.round(group.average)}%</span>`;
    const actions = document.createElement("div");
    actions.className = "quiz-history-actions";
    const retake = document.createElement("button");
    retake.className = "text-button";
    retake.type = "button";
    retake.textContent = "Retake Quiz";
    retake.addEventListener("click", () => startHistoryQuizRetake(attempt));
    const review = document.createElement("button");
    review.className = "text-button";
    review.type = "button";
    review.textContent = "Review Answers";
    review.addEventListener("click", () => openQuizResults(attempt));
    const regenerate = document.createElement("button");
    regenerate.className = "text-button";
    regenerate.type = "button";
    regenerate.textContent = "Regenerate Quiz";
    regenerate.addEventListener("click", () => regenerateHistoryQuiz(attempt));
    // Older quizzes made per topic can still be retaken/reviewed, but new quizzes are whole-document only.
    if ((attempt.topic_id || "document") === "document") actions.append(retake, review, regenerate);
    else actions.append(retake, review);
    if (attempt.quiz_id) actions.appendChild(createQuizCardMenu(attempt.quiz_id, (attempt.title || "").trim() || "Untitled Quiz"));
    const history = document.createElement("details");
    history.className = "quiz-attempt-history";
    const historySummary = document.createElement("summary");
    historySummary.textContent = `Attempt history (${group.attempts.length})`;
    history.appendChild(historySummary);
    group.attempts.forEach((pastAttempt) => {
      const row = document.createElement("button");
      row.className = "quiz-attempt-history-row";
      row.type = "button";
      const activity = pastAttempt.completed_at ? new Date(pastAttempt.completed_at).toLocaleString() : "time unavailable";
      row.textContent = `Attempt ${pastAttempt.attempt_number} · ${Math.round(pastAttempt.percentage)}% · ${activity}`;
      row.addEventListener("click", () => openQuizResults(pastAttempt));
      history.appendChild(row);
    });
    card.append(info, score, actions, history);
    quizHistoryList.appendChild(card);
  });
}

function createQuizStatusCard(className, titleText, metaText, statusText, detailText) {
  const card = document.createElement("article");
  card.className = `quiz-history-card ${className}`;
  const info = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = titleText;
  const meta = document.createElement("small");
  meta.className = "quiz-history-meta";
  meta.textContent = metaText;
  const status = document.createElement("span");
  status.className = "quiz-history-scope";
  status.textContent = statusText;
  const detail = document.createElement("small");
  detail.textContent = detailText;
  info.append(title, meta, status, detail);
  card.appendChild(info);
  return { card, info };
}

function createPendingQuizCard(pending) {
  return createQuizStatusCard(
    "quiz-pending-card", pending.name || "Untitled Quiz", `Entire Document · ${pending.difficulty}`,
    `Generating ${pending.count} questions…`, `Using ${modelLabel(pending.modelId)} · this quiz appears here as soon as it is ready`,
  ).card;
}

// "..." menu: Delete only, for now -- Rename has no backend support yet (see backend/main.py,
// there is no quiz-title PATCH endpoint), so it is intentionally left out rather than half-built.
function createQuizCardMenu(quizId, titleText) {
  const menu = document.createElement("details");
  menu.className = "quiz-card-menu";
  const summary = document.createElement("summary");
  summary.setAttribute("aria-label", `More actions for ${titleText}`);
  summary.textContent = "⋯";
  const list = document.createElement("div");
  list.className = "quiz-card-menu-list";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "text-button danger-button";
  remove.textContent = "Delete";
  remove.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!window.confirm(`Delete "${titleText}"? Its questions, attempts, and answers cannot be recovered.`)) return;
    try {
      await fetchJson(`${QUIZZES_API_URL}/${encodeURIComponent(quizId)}`, { method: "DELETE" });
      if (currentQuiz?.quiz_id === quizId) { currentQuiz = null; currentAttempt = null; renderAssessmentQuiz(); }
      await loadQuizStatuses();
      await loadQuizHistory();
      await loadDashboard();
      showToast("Quiz deleted");
    } catch (error) { showToast(error.message || "Could not delete quiz"); }
  });
  list.appendChild(remove);
  menu.append(summary, list);
  return menu;
}

function formatQuizCardTimestamp(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

// The Quiz Library card for a quiz with no completed attempt yet -- Not Started or In Progress.
// (A quiz with a completed attempt is represented by its history group instead; see
// quizListState's attemptedQuizIds exclusion.)
function createSavedQuizCard(variant) {
  const partial = variant.status === "partial";
  const counts = partial ? `${variant.question_count}/${variant.requested_count} questions (partial)` : `${variant.question_count} questions`;
  const model = variant.model_name ? modelLabel(variant.model_id, variant.model_name) : "";
  const scopeName = (variant.assessment_scope || variant.topic_id) === "document" || variant.topic_id === "document"
    ? "Entire Document" : (variant.topic_name || variant.topic_id || "Topic");
  const inProgress = variant.progress_status === "in_progress";
  const statusLabel = inProgress ? "In Progress" : "Not Started";
  const progressText = inProgress ? `${variant.answered || 0} / ${variant.total || variant.question_count} answered` : "";
  const updated = formatQuizCardTimestamp(variant.updated_at || variant.created_at);
  const detailParts = [counts, progressText, model ? `Generated by ${model}` : "Saved quiz", updated].filter(Boolean);
  const { card } = createQuizStatusCard(
    "quiz-saved-card", (variant.title || "").trim() || "Untitled Quiz", `${scopeName} · ${variant.difficulty}`,
    statusLabel, detailParts.join(" · "),
  );
  const actions = document.createElement("div");
  actions.className = "quiz-history-actions";
  const start = document.createElement("button");
  start.className = "text-button";
  start.type = "button";
  start.textContent = inProgress ? "Resume" : "Start";
  start.addEventListener("click", async () => {
    try { await openQuizPlayer({ document_id: activeDocumentId, topic_id: variant.topic_id, difficulty: variant.difficulty, quiz_id: variant.quiz_id }); }
    catch (error) { showToast(error.message || "Could not open this quiz"); }
  });
  actions.appendChild(start);
  if (variant.quiz_id) actions.appendChild(createQuizCardMenu(variant.quiz_id, (variant.title || "").trim() || "Untitled Quiz"));
  card.appendChild(actions);
  return card;
}

function registerPendingQuiz(request, fallbackName = "") {
  const id = ++pendingQuizSequence;
  pendingQuizGenerations.set(id, {
    documentId: request.document_id, name: request.quiz_name || fallbackName, difficulty: request.difficulty,
    count: request.question_count, modelId: request.model_id,
  });
  renderQuizHistory();
  return id;
}

// The request is over (saved, failed or superseded): drop the "generating" card and re-read what
// the backend has saved, whether or not the user is still looking at that quiz.
async function finishPendingQuiz(id) {
  pendingQuizGenerations.delete(id);
  renderQuizHistory();
  await loadQuizStatuses();
  updateDifficultyOptions();
  renderQuizHistory();
}

async function showQuizHistoryDetail(attemptId) {
  quizHistoryDetail.hidden = false;
  quizHistoryDetail.textContent = "Loading attempt...";
  try {
    const attempt = await requestQuizHistoryDetail(attemptId);
    quizQuestionIndex = 0;
    const questions = (attempt.question_results || []).map((result) => ({
      id: result.question_id,
      question: result.question || `Question ${result.question_id}`,
      options: result.options || [],
      explanation: result.explanation || "",
    }));
    renderCompletedQuizReview(quizHistoryDetail, attempt, questions, {
      back: backToQuizzes,
      retake: () => startHistoryQuizRetake(attempt),
      regenerate: () => regenerateHistoryQuiz(attempt),
    });
    return;
    quizHistoryDetail.innerHTML = "";
    const heading = document.createElement("div");
    heading.className = "quiz-history-detail-heading";
    const title = document.createElement("h3");
    title.textContent = `${attempt.document_id} · ${attempt.difficulty} · ${attempt.score}/${attempt.total}`;
    const actions = document.createElement("div");
    actions.className = "quiz-history-detail-actions";
    const retake = document.createElement("button");
    retake.className = "primary-button";
    retake.type = "button";
    retake.textContent = "Retake Quiz";
    retake.addEventListener("click", () => startHistoryQuizRetake(attempt));
    const close = document.createElement("button");
    close.className = "text-button";
    close.type = "button";
    close.textContent = "← Back to Quizzes";
    close.addEventListener("click", backToQuizzes);
    actions.append(retake, close);
    heading.append(title, actions);
    quizHistoryDetail.appendChild(heading);

    (attempt.question_results || []).forEach((result, index) => {
      const item = document.createElement("article");
      item.className = `quiz-history-question ${result.is_correct ? "correct" : "incorrect"}`;
      const question = document.createElement("strong");
      question.textContent = `${index + 1}. ${result.question || `Question ${result.question_id}`}`;
      const options = document.createElement("div");
      options.className = "review-answer-list";
      (result.options || []).forEach((option) => {
        const letter = option.trim().charAt(0).toUpperCase();
        const row = document.createElement("div");
        row.className = "review-answer-option";
        const correctAnswers = result.correct_answers || [result.correct_answer];
        const selectedAnswers = result.selected_answers || [result.selected_answer];
        if (correctAnswers.includes(letter)) row.classList.add("correct");
        if (selectedAnswers.includes(letter)) row.classList.add("selected");
        if (selectedAnswers.includes(letter) && !correctAnswers.includes(letter)) row.classList.add("incorrect");
        row.textContent = option;
        options.appendChild(row);
      });
      const explanation = document.createElement("p");
      explanation.className = "review-explanation";
      explanation.textContent = result.explanation ? `Explanation: ${result.explanation}` : "Explanation unavailable.";
      item.append(question, options, explanation);
      quizHistoryDetail.appendChild(item);
    });
  } catch (error) {
    quizHistoryDetail.textContent = error.message || "Could not load this attempt.";
  }
}

// With quizId, opens exactly that quiz artifact (Quiz Library Start/Resume) -- staleness is then
// tracked by request sequence (quizId never changes with the settings selectors, so the old
// settings-key comparison would never catch a superseded request). Without quizId, keeps the
// older settings-key staleness check for callers that have no specific artifact to open.
let quizDetailRequestSeq = 0;

async function loadSelectedQuiz(quizId) {
  const documentId = quizDocumentSelect?.value;
  const requestedQuizKey = currentQuizKey();
  const requestSeq = ++quizDetailRequestSeq;
  updateDifficultyOptions();
  currentQuiz = null;
  currentAttempt = null;
  quizAttemptSummary = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  renderAssessmentQuiz();
  if (!documentId) {
    return;
  }

  try {
    const detail = await requestQuizDetail(documentId, quizId);
    const stale = quizId ? requestSeq !== quizDetailRequestSeq : requestedQuizKey !== currentQuizKey();
    if (stale) {
      return;
    }
    currentQuiz = detail.quiz || null;
    currentAttempt = detail.latest_attempt || null;
    quizAttemptSummary = detail.attempt_summary || null;
    quizExplanations = {};
    // The Quiz Player resumes an in-progress attempt exactly where it was left: its saved answers
    // and saved current_question_index. The older inline view keeps its completed-only restore.
    const resumeInProgress = quizPlayerOpen && currentAttempt && !currentAttempt.completed;
    quizAnswers = (currentAttempt?.completed || resumeInProgress) && currentAttempt.answers ? { ...currentAttempt.answers } : {};
    if (resumeInProgress) {
      const lastIndex = Math.max((currentQuiz?.questions?.length || 1) - 1, 0);
      quizQuestionIndex = Math.max(0, Math.min(Number(currentAttempt.current_question_index) || 0, lastIndex));
    } else {
      const firstUnansweredIndex = currentQuiz?.questions?.findIndex(
        (question) => !quizAnswers[String(question.id)]
      );
      quizQuestionIndex = firstUnansweredIndex >= 0 ? firstUnansweredIndex : 0;
    }
    renderAssessmentQuiz();
  } catch (error) {
    currentQuiz = null;
    currentAttempt = null;
    quizAttemptSummary = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    renderAssessmentQuiz();
  }
}

async function handleQuizDifficultyChange() {
  currentQuiz = null;
  currentAttempt = null;
  quizAttemptSummary = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  renderAssessmentQuiz();
  await loadSelectedQuiz();
}

// An explicit Generate/Create Quiz submission always produces a brand-new quiz artifact (its own
// quiz_id) -- it must never silently hand back whatever happens to already be loaded in
// currentQuiz (that was the old behavior; see requestGeneratedQuiz's regenerate:true and
// backend/quiz_service.py's explicit_new_quiz handling).
// Two distinct entry points share this one button/handler:
//  - The Create Quiz sheet (currentQuiz empty -- the only way to reach the sheet at all, since the
//    "+ New Quiz" header button and the Library's empty-state button are both hidden/absent
//    whenever a quiz is loaded): fills the form, clicks Generate, always gets a brand-new quiz
//    artifact (its own quiz_id, see requestGeneratedQuiz's regenerate:true) -- it is never auto-
//    loaded into the player; the sheet closes, the Library refreshes, and the user opens it later
//    with Start/Resume from its card.
//  - The inline form next to an already-loaded quiz (Start/Resume from the Library put it there):
//    a player-adjacent affordance, unchanged from before -- clicking it just re-shows what is
//    already loaded rather than generating anything.
async function generateAssessmentQuiz() {
  if (quizGenerationInFlight) return;   // no duplicate submissions
  if (!quizDocumentSelect.value) {
    showToast("Choose an indexed document first");
    return;
  }
  if (currentQuiz?.questions?.length) {
    renderAssessmentQuiz();
    showToast(quizSettingsMismatch(currentQuiz) || (currentAttempt?.completed ? "Reviewing saved quiz" : "Quiz ready"));
    return;
  }

  const generationRequest = selectedQuizGenerationRequest();
  if (!generationRequest.quiz_name) {
    showToast("Enter a quiz name");
    quizNameInput?.focus();
    return;
  }
  hideQuizSheetError();
  setQuizSheetGenerating(true);   // set synchronously, before any await, so a rapid double-click is blocked

  // Model preparation happens BEFORE generation timing starts: a lazily-pulled model is fetched
  // here, not inside the "Generating…" state below (see model_prepare_ms).
  try { await ensureSelectedModelReadyWithStatus(); }
  catch (error) {
    showToast(error.message || "Model could not be prepared.");
    setQuizSheetGenerating(false);
    return;
  }

  const pendingId = registerPendingQuiz(generationRequest);
  setAssessmentLoading(true);

  try {
    const generatedQuiz = await requestGeneratedQuiz(generationRequest);
    if (quizNameInput) quizNameInput.value = "";
    closeQuizCreateDialog({ force: true });
    // A partial result (fewer grounded questions than requested) is still a success -- grounding
    // quality was prioritized over hitting the exact requested count.
    showToast(generatedQuiz.status === "partial"
      ? `Quiz created with ${generatedQuiz.actual_count ?? generatedQuiz.question_count} questions. Grounded quality was prioritized.`
      : "Quiz created");
  } catch (error) {
    showQuizSheetError(error.message || "Assessment Agent could not generate a quiz.");
  } finally {
    setQuizSheetGenerating(false);
    setAssessmentLoading(false);
    finishPendingQuiz(pendingId);
  }
}

async function regenerateAssessmentQuiz() {
  if (!quizDocumentSelect.value) {
    showToast("Choose an indexed document first");
    return;
  }
  try { await ensureSelectedModelReadyWithStatus(); }
  catch (error) { showToast(error.message || "Model could not be prepared."); return; }

  const generationRequest = selectedQuizGenerationRequest();
  const requestedQuizKey = quizGenerationRequestKey(generationRequest);
  const pendingId = registerPendingQuiz(generationRequest, currentQuiz?.title || "");
  setAssessmentLoading(true);
  quizList.innerHTML = "";
  assessmentTitle.textContent = "Regenerating assessment";

  try {
    const regeneratedQuiz = await requestQuizRegeneration(generationRequest.document_id, generationRequest);
    if (requestedQuizKey !== currentQuizKey()) return;
    currentQuiz = regeneratedQuiz;
    currentAttempt = null;
    quizAttemptSummary = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    await loadQuizStatuses();
    updateDifficultyOptions();
    renderAssessmentQuiz();
    showToast("Quiz regenerated");
  } catch (error) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "Assessment Agent could not regenerate a quiz.";
    quizList.innerHTML = "";
    quizList.appendChild(empty);
    showToast("Regenerate failed");
  } finally {
    setAssessmentLoading(false);
    updateAssessmentSummary();
    finishPendingQuiz(pendingId);
  }
}

async function deleteAssessmentQuiz() {
  const quizId = currentQuiz?.quiz_id;
  if (!quizId) return;
  if (!window.confirm("Delete this quiz? Its questions, attempts, and answers cannot be recovered.")) return;
  try {
    await fetchJson(`${QUIZZES_API_URL}/${encodeURIComponent(quizId)}`, { method: "DELETE" });
    backToQuizzes();
    await loadQuizStatuses();
    await loadDashboard();
    showToast("Quiz deleted");
  } catch (error) {
    showToast(error.message || "Could not delete quiz");
  }
}

// Opens the exact quiz artifact the user picked (Start/Resume/Regenerate-from-history) -- when
// attempt.quiz_id is given, that quiz_id is what gets loaded, never "whatever quiz is newest in
// this document/topic/difficulty slot" (several quizzes can share a slot).
async function selectHistoryQuizVariant(attempt) {
  if ((attempt.topic_id || "document") !== "document") {
    throw new Error("Quizzes are no longer created per topic. Create a quiz for the whole document instead.");
  }
  quizDocumentSelect.value = attempt.document_id;
  quizDifficultySelect.value = attempt.difficulty || "easy";
  quizScopeSelect.value = "document";
  await loadSelectedQuiz(attempt.quiz_id);
  if (!currentQuiz?.questions?.length || (attempt.quiz_id && currentQuiz.quiz_id !== attempt.quiz_id)) {
    throw new Error("The saved quiz is no longer available. Regenerate it to create new questions.");
  }
}

async function startHistoryQuizRetake(attempt) {
  try {
    const detail = await requestQuizForRetake(attempt.attempt_id);
    currentQuiz = detail.quiz;
    quizAttemptSummary = detail.attempt_summary || null;
    currentAttempt = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    quizHistoryDetail.hidden = true;
    renderAssessmentQuiz();
    showToast("Retake started with the same saved questions");
  } catch (error) {
    showToast(error.message || "Could not start this retake");
  }
}

async function regenerateHistoryQuiz(attempt) {
  try {
    await selectHistoryQuizVariant(attempt);
    quizHistoryDetail.hidden = true;
    await regenerateAssessmentQuiz();
  } catch (error) {
    showToast(error.message || "Could not regenerate this quiz");
  }
}

function renderAssessmentQuizLegacy() {
  const quizPane = document.querySelector('[data-session-pane="quiz"]');
  quizPane?.classList.toggle("quiz-active", Boolean(currentQuiz?.questions?.length));
  quizPane?.classList.toggle("quiz-landing", !currentQuiz?.questions?.length);
  updateQuizLandingLayout();
  quizList.innerHTML = "";
  assessmentTitle.textContent = currentQuiz
    ? `${currentQuiz.questions.length} ${currentQuiz.difficulty} questions from ${currentQuiz.document_id}`
    : "Assessment Agent";

  if (!currentQuiz?.questions?.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Choose a document, then generate or start its saved quiz.";
    quizList.appendChild(empty);
    updateAssessmentSummary();
    return;
  }

  quizQuestionIndex = Math.max(0, Math.min(quizQuestionIndex, currentQuiz.questions.length - 1));
  const question = currentQuiz.questions[quizQuestionIndex];
  {
    const card = document.createElement("article");
    card.className = "quiz-question-card";
    card.dataset.questionId = question.id;

    const heading = document.createElement("div");
    heading.className = "quiz-question-heading";

    const number = document.createElement("span");
    number.textContent = `Question ${quizQuestionIndex + 1} of ${currentQuiz.questions.length}`;
    heading.appendChild(number);

    const questionText = document.createElement("h3");
    questionText.textContent = question.question;
    const questionResult = currentAttempt?.question_results?.find(
      (result) => result.question_id === question.id
    );

    const options = document.createElement("div");
    options.className = "answer-list";

    question.options.forEach((option) => {
      const button = document.createElement("button");
      button.className = "answer-option";
      button.type = "button";
      button.textContent = option;
      const selectedLetter = quizAnswers[String(question.id)] || quizAnswers[question.id];
      const optionLetter = option.trim().charAt(0).toUpperCase();
      if (selectedLetter === optionLetter) {
        button.classList.add("selected");
      }
      if (questionResult) {
        button.disabled = true;
        if (optionLetter === questionResult.correct_answer) {
          button.classList.add("correct");
        }
        if (selectedLetter === optionLetter && !questionResult.is_correct) {
          button.classList.add("incorrect");
        }
      }
      button.addEventListener("click", () => selectAssessmentAnswer(question, option, card));
      options.appendChild(button);
    });

    const feedback = document.createElement("div");
    feedback.className = "feedback";
    if (questionResult) {
      feedback.className = `feedback ${questionResult.is_correct ? "good" : "bad"}`;
      feedback.textContent = questionResult.is_correct ? "Correct." : "Incorrect.";
    }

    const explainButton = document.createElement("button");
    explainButton.className = "text-button explain-button";
    explainButton.type = "button";
    explainButton.textContent = "Explain";
    explainButton.hidden = !questionResult;
    explainButton.addEventListener("click", () => explainAssessmentQuestion(question, explainButton));

    const explanation = document.createElement("div");
    explanation.className = "quiz-explanation";
    explanation.hidden = !quizExplanations[String(question.id)];
    explanation.textContent = quizExplanations[String(question.id)] || "";

    const navigation = document.createElement("div");
    navigation.className = "quiz-navigation";

    const previousButton = document.createElement("button");
    previousButton.className = "secondary-button quiz-nav-button";
    previousButton.type = "button";
    previousButton.textContent = "Previous";
    previousButton.disabled = quizQuestionIndex === 0;
    previousButton.addEventListener("click", () => moveQuizQuestion(-1));

    const nextButton = document.createElement("button");
    nextButton.className = "primary-button quiz-nav-button";
    nextButton.type = "button";
    nextButton.textContent = quizQuestionIndex === currentQuiz.questions.length - 1 ? "Finish" : "Next";
    nextButton.disabled = !questionResult;
    nextButton.addEventListener("click", () => moveQuizQuestion(1));

    navigation.append(previousButton, nextButton);
    card.append(heading, questionText, options, feedback, explainButton, explanation, navigation);
    quizList.appendChild(card);
  }

  updateAssessmentSummary();
}

function moveQuizQuestion(direction) {
  if (!currentQuiz?.questions?.length) {
    return;
  }
  const nextIndex = quizQuestionIndex + direction;
  if (nextIndex >= currentQuiz.questions.length) {
    showToast(currentAttempt?.completed
      ? `Quiz completed: ${currentAttempt.score}/${currentAttempt.total}`
      : "Answer every question to finish the quiz");
    return;
  }
  quizQuestionIndex = Math.max(0, nextIndex);
  renderAssessmentQuiz();
}

async function resetAssessmentQuiz() {
  if (!currentQuiz?.questions?.length) {
    return;
  }
  try {
    await requestQuizProgressReset();
    currentAttempt = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    renderAssessmentQuiz();
    showToast("Quiz progress reset");
  } catch (error) {
    showToast(error.message || "Could not reset quiz progress");
  }
}

async function explainAssessmentQuestion(question, button) {
  const selectedAnswer = quizAnswers[String(question.id)];
  if (!selectedAnswer || button.disabled) {
    return;
  }
  const card = button.closest(".quiz-question-card");
  const explanation = card.querySelector(".quiz-explanation");
  button.disabled = true;
  button.textContent = "Explaining...";
  explanation.hidden = false;
  explanation.textContent = "Generating a short explanation from the selected lecture...";
  try {
    const result = await requestQuizExplanation(question.id);
    quizExplanations[String(question.id)] = result.explanation;
    explanation.textContent = result.explanation;
    button.textContent = result.cache_hit ? "Explanation loaded" : "Explained";
  } catch (error) {
    explanation.textContent = error.message || "Could not generate an explanation.";
    button.disabled = false;
    button.textContent = "Try Explain Again";
  }
}

function createAssessmentReviewCard(question, result, index, total) {
  const card = document.createElement("article");
  card.className = `quiz-question-card quiz-review-card ${result.is_correct ? "correct" : "incorrect"}`;
  const heading = document.createElement("div");
  heading.className = "quiz-question-heading";
  const number = document.createElement("span");
  number.textContent = `Question ${index + 1}/${total}`;
  heading.appendChild(number);
  const questionText = document.createElement("h3");
  questionText.textContent = question.question;
  const options = document.createElement("div");
  options.className = "review-answer-list";
  question.options.forEach((option) => {
    const letter = option.trim().charAt(0).toUpperCase();
    const row = document.createElement("div");
    row.className = "review-answer-option";
    const correctAnswers = result.correct_answers || [result.correct_answer];
    const selectedAnswers = result.selected_answers || [result.selected_answer];
    if (correctAnswers.includes(letter)) row.classList.add("correct");
    if (selectedAnswers.includes(letter)) row.classList.add("selected");
    if (selectedAnswers.includes(letter) && !correctAnswers.includes(letter)) row.classList.add("incorrect");
    row.textContent = option;
    options.appendChild(row);
  });
  card.append(heading, questionText, options);
  return card;
}

function reviewedAnswerText(question, answerLetter) {
  if (!answerLetter) return "no answer";
  const indexedOption = question.options[answerLetter.toUpperCase().charCodeAt(0) - 65];
  return question.options.find((option) => option.trim().charAt(0).toUpperCase() === answerLetter) || indexedOption || answerLetter;
}

function reviewedAnswersText(question, answerLetters) {
  const values = Array.isArray(answerLetters) ? answerLetters : [answerLetters];
  return values.filter(Boolean).map((letter) => reviewedAnswerText(question, letter)).join(", ") || "no answer";
}

async function explainReviewedQuestion(question, result, button) {
  if (quizExplanationPending || chatForm.classList.contains("is-sending")) return;
  quizExplanationPending = true;
  button.disabled = true;
  button.textContent = "Explaining...";
  tutorLayout.hidden = false;
  tutorLayout.classList.add("quiz-explanation-open");
  document.getElementById("session-tutor-toggle")?.setAttribute("aria-expanded", "true");
  if (window.matchMedia("(max-width: 1050px)").matches) {
    tutorLayout.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const correctAnswer = reviewedAnswersText(question, result.correct_answers || result.correct_answer);
  const selectedValues = result.selected_answers || result.selected_answer;
  const selectedContext = selectedValues ? ` I answered ${reviewedAnswersText(question, selectedValues)}.` : "";
  const message = `Explain why the correct answer is ${correctAnswer} for this question: ${question.question}.${selectedContext} Give a concise explanation grounded in the current document.`;
  try {
    await sendTutorMessage(message);
  } finally {
    quizExplanationPending = false;
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "Explain Answer";
    }
  }
}

function renderCompletedQuizReview(container, attempt, questions, callbacks) {
  const results = attempt.question_results || [];
  const total = questions.length;
  quizQuestionIndex = Math.max(0, Math.min(quizQuestionIndex, total - 1));
  const correctCount = results.filter((result) => result.is_correct).length;
  const score = Number.isFinite(Number(attempt.score)) ? Number(attempt.score) : correctCount;
  const percentage = Number.isFinite(Number(attempt.percentage))
    ? Math.round(Number(attempt.percentage))
    : Math.round((score / Math.max(total, 1)) * 100);

  container.innerHTML = "";
  const summary = document.createElement("section");
  summary.className = "quiz-review-summary";
  const metrics = document.createElement("div");
  metrics.className = "quiz-review-summary-metrics";
  metrics.innerHTML = `<strong>${score}/${total}</strong><span>${percentage}%</span><span>${correctCount} correct</span><span>${total - correctCount} incorrect</span>`;
  const actions = document.createElement("div");
  actions.className = "quiz-review-summary-actions";
  [
    ["← Back to Quizzes", "text-button", callbacks.back],
    ["Retake Quiz", "primary-button", callbacks.retake],
    ["Regenerate Quiz", "text-button", callbacks.regenerate],
    ["Delete Quiz", "text-button danger-button", callbacks.remove],
  ].forEach(([label, className, handler]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", handler);
    actions.appendChild(button);
  });
  summary.append(metrics, actions);

  const navigator = document.createElement("nav");
  navigator.className = "quiz-review-navigator";
  navigator.setAttribute("aria-label", "Reviewed questions");
  questions.forEach((question, index) => {
    const result = results.find((item) => Number(item.question_id) === Number(question.id));
    const button = document.createElement("button");
    button.type = "button";
    button.className = `quiz-review-nav-button ${result?.is_correct ? "correct" : "incorrect"}`;
    button.classList.toggle("current", index === quizQuestionIndex);
    button.textContent = String(index + 1);
    button.setAttribute("aria-label", `Question ${index + 1}: ${result?.is_correct ? "correct" : "incorrect"}`);
    button.setAttribute("aria-current", index === quizQuestionIndex ? "true" : "false");
    button.addEventListener("click", () => {
      quizQuestionIndex = index;
      renderCompletedQuizReview(container, attempt, questions, callbacks);
    });
    navigator.appendChild(button);
  });

  const question = questions[quizQuestionIndex];
  const result = results.find((item) => Number(item.question_id) === Number(question?.id));
  const navigation = document.createElement("div");
  navigation.className = "quiz-review-controls";
  [["Previous", -1, quizQuestionIndex === 0]].forEach(([label, direction, disabled]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button quiz-nav-button";
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", () => {
      quizQuestionIndex += direction;
      renderCompletedQuizReview(container, attempt, questions, callbacks);
    });
    navigation.appendChild(button);
  });
  const explain = document.createElement("button");
  explain.type = "button";
  explain.className = "primary-button quiz-explain-answer";
  explain.textContent = quizExplanationPending ? "Explaining..." : "Explain Answer";
  explain.disabled = quizExplanationPending || !question || !result;
  explain.addEventListener("click", () => explainReviewedQuestion(question, result, explain));
  navigation.appendChild(explain);
  [["Next", 1, quizQuestionIndex === total - 1]].forEach(([label, direction, disabled]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button quiz-nav-button";
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", () => {
      quizQuestionIndex += direction;
      renderCompletedQuizReview(container, attempt, questions, callbacks);
    });
    navigation.appendChild(button);
  });
  container.append(summary, navigator);
  if (question && result) container.appendChild(createAssessmentReviewCard(question, result, quizQuestionIndex, total));
  container.appendChild(navigation);
}

function backToQuizzes() {
  currentQuiz = null;
  currentAttempt = null;
  quizAttemptSummary = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  if (quizHistoryDetail) quizHistoryDetail.hidden = true;
  renderAssessmentQuiz();
  renderQuizHistory();
}

function renderAttemptSummary() {
  if (!quizAttemptSummary?.attempts) return null;
  const summary = document.createElement("div");
  summary.className = "quiz-attempt-summary";
  summary.innerHTML = `<strong>Attempts: ${quizAttemptSummary.attempts}</strong><span>Latest score: ${Math.round(quizAttemptSummary.latest_score)}%</span><span>Best score: ${Math.round(quizAttemptSummary.best_score)}%</span><span>Average score: ${Math.round(quizAttemptSummary.average_score)}%</span>`;
  return summary;
}

function quizTypeLabel(questionType) {
  return { single_choice: "Multiple Choice", true_false: "True/False", multi_select: "Multiple Select" }[questionType]
    || questionType;
}

function documentQuizTypeBreakdown(quiz) {
  if (quiz.assessment_plan?.type_distribution) return quiz.assessment_plan.type_distribution;
  const counts = {};
  (quiz.questions || []).forEach((question) => {
    const questionType = question.question_type || "single_choice";
    counts[questionType] = (counts[questionType] || 0) + 1;
  });
  return counts;
}

function quizPartialSuffix(quiz) {
  const plan = quiz?.assessment_plan;
  if (!plan?.partial) return "";
  const requested = plan.requested_count ?? plan.target_questions ?? quiz.questions.length;
  const actual = plan.actual_count ?? quiz.questions.length;
  return ` · ${actual}/${requested} questions generated`;
}

// "Generated by <model>": the name comes from the backend (the model that really ran the quiz) and is
// shown with the Study Session's label for it; it never follows the current selector. Quizzes saved
// before this field existed have none and show nothing.
function quizModelInfo(quiz) {
  return quiz?.generation_model || quiz?.assessment_plan?.generation_model || null;
}

function quizModelSuffix(quiz) {
  const info = quizModelInfo(quiz);
  const name = info ? modelLabel(info.model_id, info.name) : "";
  return name ? ` · Generated by ${name}` : "";
}

// A saved quiz belongs to the settings it was made with (model, number of questions). If the
// screen's current settings differ, say so: generating over it never silently relabels or replaces it.
function quizSettingsMismatch(quiz, request = selectedQuizGenerationRequest()) {
  const info = quizModelInfo(quiz);
  const requested = Number(quiz?.requested_count ?? quiz?.assessment_plan?.requested_count ?? 0);
  const modelDiffers = Boolean(info?.model_id && request.model_id && info.model_id !== request.model_id);
  const countDiffers = Boolean(requested && request.question_count && requested !== Number(request.question_count));
  if (!modelDiffers && !countDiffers) return "";
  const made = [info ? `generated by ${modelLabel(info.model_id, info.name)}` : "", requested ? `${requested} questions` : ""].filter(Boolean).join(", ");
  return `This saved quiz was ${made}. Your current settings are ${modelLabel(request.model_id)}, ${request.question_count} questions. Use Regenerate Quiz to create a new one with them.`;
}

function assessmentTitleText(quiz) {
  if (!quiz?.questions?.length) return "Assessment Agent";
  const quizName = (quiz.title || "").trim() || "Untitled Quiz";
  const mismatch = quizSettingsMismatch(quiz);
  const partialSuffix = quizPartialSuffix(quiz) + quizModelSuffix(quiz)
    + (mismatch ? " · Current settings differ: press Regenerate Quiz to use them" : "");
  if (quiz.assessment_scope === "document") {
    const distribution = documentQuizTypeBreakdown(quiz);
    const parts = ["single_choice", "true_false", "multi_select"]
      .filter((questionType) => distribution[questionType])
      .map((questionType) => `${distribution[questionType]} ${quizTypeLabel(questionType)}`);
    return `${quizName} · ${quiz.questions.length} questions · ${parts.join(" · ")}` + partialSuffix;
  }
  return `${quizName} · ${quiz.questions.length} ${quiz.difficulty} questions from ${quiz.document_id}` + partialSuffix;
}

// ---- Quiz Player: open/close -------------------------------------------------------------------

// Opens the focused Quiz Player for the exact quiz_id behind a Start/Resume click -- reuses
// selectHistoryQuizVariant (document/difficulty/scope selection + loadSelectedQuiz(quiz_id), which
// never falls back to a different quiz) so Start/Resume keep the same "exact artifact" guarantee
// the Library already relies on elsewhere.
function setQuizPlayerVisible(open) {
  quizPlayerOpen = open;
  quizResultState = null;
  quizResultRequestSeq += 1;   // a still-loading Results request can no longer land
  document.querySelector('[data-session-pane="quiz"]')?.classList.toggle("quiz-player-open", open);
  document.getElementById("quiz-player").hidden = !open;
  showQuizPlayerView("question");
  document.getElementById("quiz-exit-confirm").hidden = true;
  document.getElementById("quiz-finish-confirm").hidden = true;
}

async function openQuizPlayer(target) {
  resetQuizAutosave();
  setQuizPlayerVisible(true);
  try {
    await selectHistoryQuizVariant(target);
  } catch (error) {
    closeQuizPlayerToLibrary();
    throw error;
  }
}

async function closeQuizPlayerToLibrary() {
  detachQuizAutosave();   // Exit after a failed save still keeps retrying the newest snapshot
  setQuizPlayerVisible(false);
  backToQuizzes();
  await loadQuizStatuses();
  updateDifficultyOptions();
  renderQuizHistory();
}

// ---- Quiz Player: rendering ---------------------------------------------------------------------

function renderQuizPlayer() {
  if (!currentQuiz?.questions?.length) {
    // loadSelectedQuiz resets state and renders once before its fetch resolves -- show a light
    // loading state rather than a blank/broken player in that brief window.
    if (quizResultState) { renderQuizResultState(); return; }
    showQuizPlayerView("question");
    document.getElementById("quiz-player-title").textContent = "Loading…";
    document.getElementById("quiz-player-question").textContent = "";
    document.getElementById("quiz-player-answers").innerHTML = "";
    document.getElementById("quiz-player-position").textContent = "";
    document.getElementById("quiz-player-answered-count").textContent = "";
    document.getElementById("quiz-player-difficulty").textContent = "";
    document.getElementById("quiz-player-model").hidden = true;
    return;
  }
  if (quizResultState) { renderQuizResultState(); return; }
  // Start on a quiz whose latest attempt is already completed opens its Results, never the
  // question-taking view.
  if (currentAttempt?.completed) {
    showQuizResults(buildQuizResult(currentAttempt, currentQuiz));
    return;
  }
  renderQuizPlayerQuestion();
}

function renderQuizPlayerQuestion() {
  showQuizPlayerView("question");

  const total = currentQuiz.questions.length;
  quizQuestionIndex = Math.max(0, Math.min(quizQuestionIndex, total - 1));
  const question = currentQuiz.questions[quizQuestionIndex];
  const answeredCount = Object.keys(quizAnswers).length;

  document.getElementById("quiz-player-title").textContent = (currentQuiz.title || "").trim() || "Untitled Quiz";
  document.getElementById("quiz-player-difficulty").textContent = currentQuiz.difficulty || "";
  const modelInfo = quizModelInfo(currentQuiz);
  const modelEl = document.getElementById("quiz-player-model");
  modelEl.textContent = modelInfo ? `Generated by ${modelLabel(modelInfo.model_id, modelInfo.name)}` : "";
  modelEl.hidden = !modelInfo;
  document.getElementById("quiz-player-position").textContent = `Question ${quizQuestionIndex + 1} of ${total}`;
  document.getElementById("quiz-player-answered-count").textContent = `${answeredCount} answered`;
  document.getElementById("quiz-player-progress-bar").style.width = `${Math.round(((quizQuestionIndex + 1) / total) * 100)}%`;
  document.getElementById("quiz-player-question").textContent = question.question;

  const isMultiSelect = question.question_type === "multi_select";
  const selected = quizAnswers[String(question.id)];
  const answersEl = document.getElementById("quiz-player-answers");
  answersEl.innerHTML = "";
  (question.options || []).forEach((option) => {
    const letter = option.trim().charAt(0).toUpperCase();
    const isSelected = Array.isArray(selected) ? selected.includes(letter) : selected === letter;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `quiz-player-answer-card${isSelected ? " selected" : ""}`;
    button.setAttribute("role", isMultiSelect ? "checkbox" : "radio");
    button.setAttribute("aria-checked", String(isSelected));
    const indicator = document.createElement("span");
    indicator.className = "quiz-player-answer-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "quiz-player-answer-label";
    label.textContent = option;
    button.append(indicator, label);
    button.addEventListener("click", () => selectQuizPlayerAnswer(question, letter, isMultiSelect));
    answersEl.appendChild(button);
  });

  // Previous/Next never require an answer -- only Finish Quiz (the last question's Next) checks
  // for unanswered questions, and only as a dismissable confirmation (see handleQuizPlayerFinish).
  document.getElementById("quiz-player-previous").disabled = quizQuestionIndex === 0;
  const nextButton = document.getElementById("quiz-player-next");
  const isLast = quizQuestionIndex === total - 1;
  nextButton.textContent = isLast ? (quizSubmitInFlight ? "Submitting…" : "Finish Quiz") : "Next";
  nextButton.disabled = isLast && quizSubmitInFlight;
  nextButton.onclick = isLast ? handleQuizPlayerFinish : () => moveQuizPlayerQuestion(1);
}


// ---- Quiz Results + Review Answers ------------------------------------------------------------------
// Built only from the persisted, server-graded attempt (question_results: is_correct, selected and
// correct answers) -- never re-graded client-side. The quiz, when available, only contributes
// display metadata (title, model, question order, concept names).

function showQuizPlayerView(view) {
  document.getElementById("quiz-player-question-view").hidden = view !== "question";
  document.getElementById("quiz-results-loading").hidden = view !== "loading";
  document.getElementById("quiz-results-view").hidden = view !== "results";
  document.getElementById("quiz-review-view").hidden = view !== "review";
}

const QUIZ_OPTION_LETTERS = "ABCD";

function quizResultLetters(values) {
  return [...new Set((values || []).map((value) => String(value || "").trim().toUpperCase()).filter(Boolean))].sort();
}

function buildQuizResult(attempt, quiz, fallback = {}) {
  const questions = quiz?.questions || [];
  const questionsById = new Map(questions.map((question) => [String(question.id), question]));
  const order = questions.map((question) => String(question.id));
  const results = [...(attempt?.question_results || [])];
  if (order.length) {
    const position = (result) => { const index = order.indexOf(String(result.question_id)); return index < 0 ? order.length : index; };
    results.sort((left, right) => position(left) - position(right));
  }
  const items = results.map((result) => {
    const question = questionsById.get(String(result.question_id)) || {};
    const selected = quizResultLetters(result.selected_answers?.length ? result.selected_answers : [result.selected_answer]);
    const correct = quizResultLetters(result.correct_answers?.length ? result.correct_answers : [result.correct_answer]);
    const unanswered = selected.length === 0;
    return {
      questionId: result.question_id,
      question: result.question || question.question || `Question ${result.question_id}`,
      options: result.options?.length ? result.options : (question.options || []),
      selected,
      correct,
      unanswered,
      isCorrect: !unanswered && Boolean(result.is_correct),
      explanation: String(result.explanation || question.explanation || "").trim(),
      topicName: result.topic_name || question.topic_name || "",
      conceptName: result.concept_name || question.concept_name || "",
      sourceCount: (result.source_chunk_ids?.length ? result.source_chunk_ids : (question.source_chunk_ids || [])).length,
    };
  });
  const total = Number(attempt?.total) || items.length;
  const score = Number(attempt?.score) || 0;
  const correctCount = items.length ? items.filter((item) => item.isCorrect).length : score;
  const unansweredCount = items.filter((item) => item.unanswered).length;
  const plan = quiz?.assessment_plan || {};
  const partial = Boolean(quiz?.partial ?? plan.partial);
  const requested = quiz?.requested_count ?? plan.requested_count ?? plan.target_questions ?? null;
  const modelInfo = quizModelInfo(quiz);
  return {
    attemptId: attempt?.attempt_id,
    quizId: attempt?.quiz_id,
    title: (quiz?.title || fallback.title || "").trim() || "Untitled Quiz",
    difficulty: attempt?.difficulty || quiz?.difficulty || fallback.difficulty || "",
    model: modelInfo ? modelLabel(modelInfo.model_id, modelInfo.name) : "",
    score,
    total,
    percentage: Math.round(attempt?.percentage ?? (total ? (100 * score) / total : 0)),
    correctCount,
    unansweredCount,
    incorrectCount: Math.max(0, total - correctCount - unansweredCount),
    partialNote: partial && requested && requested > total ? `This quiz has ${total} of the ${requested} requested questions.` : "",
    items,
  };
}

function showQuizResults(result) {
  quizResultState = { result, view: "results", reviewIndex: 0 };
  renderQuizResultState();
}

// Library "Review Answers" (and attempt-history rows): loads that exact completed attempt by its
// attempt_id -- whose quiz metadata the backend resolves by the attempt's own quiz_id -- and opens
// Results in the focused player. A mismatched quiz_id is an error, never a sibling fallback.
async function openQuizResults(summary) {
  resetQuizAutosave();
  setQuizPlayerVisible(true);
  quizResultState = { loading: true };
  const requestSeq = ++quizResultRequestSeq;
  renderQuizResultState();
  try {
    const attempt = await requestQuizHistoryDetail(summary.attempt_id);
    if (requestSeq !== quizResultRequestSeq || !quizPlayerOpen) return;
    if (!attempt?.completed || attempt.attempt_id !== summary.attempt_id || (summary.quiz_id && attempt.quiz_id !== summary.quiz_id)) {
      throw new Error("These results are no longer available.");
    }
    showQuizResults(buildQuizResult(attempt, attempt.quiz || null, summary));
  } catch (error) {
    if (requestSeq !== quizResultRequestSeq) return;
    closeQuizPlayerToLibrary();
    showToast(error.message || "Could not open these results");
  }
}

function renderQuizResultState() {
  if (!quizResultState) return;
  if (quizResultState.loading) { showQuizPlayerView("loading"); return; }
  if (quizResultState.view === "review") renderQuizReview();
  else renderQuizResults();
}

function renderQuizResults() {
  const { result } = quizResultState;
  showQuizPlayerView("results");
  document.getElementById("quiz-results-title").textContent = result.title;
  const difficulty = document.getElementById("quiz-results-difficulty");
  difficulty.textContent = result.difficulty;
  difficulty.hidden = !result.difficulty;
  const model = document.getElementById("quiz-results-model");
  model.textContent = result.model ? `Generated by ${result.model}` : "";
  model.hidden = !result.model;
  document.getElementById("quiz-results-score").textContent = `${result.score} / ${result.total}`;
  document.getElementById("quiz-results-percentage").textContent = `${result.percentage}%`;
  document.getElementById("quiz-results-correct").textContent = String(result.correctCount);
  document.getElementById("quiz-results-incorrect").textContent = String(result.incorrectCount);
  document.getElementById("quiz-results-unanswered").textContent = String(result.unansweredCount);
  const note = document.getElementById("quiz-results-note");
  note.textContent = result.partialNote;
  note.hidden = !result.partialNote;
  document.getElementById("quiz-results-review").hidden = !result.items.length;
}

function openQuizReview(index = 0) {
  if (!quizResultState?.result?.items.length) return;
  quizResultState.view = "review";
  quizResultState.reviewIndex = Math.max(0, Math.min(index, quizResultState.result.items.length - 1));
  renderQuizReview();
}

function renderQuizReview() {
  const { result } = quizResultState;
  const total = result.items.length;
  const index = Math.max(0, Math.min(quizResultState.reviewIndex, total - 1));
  const item = result.items[index];
  showQuizPlayerView("review");
  document.getElementById("quiz-review-title").textContent = result.title;
  document.getElementById("quiz-review-position").textContent = `Question ${index + 1} of ${total}`;
  document.getElementById("quiz-review-progress-bar").style.width = `${Math.round(((index + 1) / total) * 100)}%`;
  const status = document.getElementById("quiz-review-status");
  const state = item.unanswered ? "unanswered" : (item.isCorrect ? "correct" : "incorrect");
  status.textContent = { correct: "Correct", incorrect: "Incorrect", unanswered: "Unanswered" }[state];
  status.className = `quiz-review-status is-${state}`;
  document.getElementById("quiz-review-question").textContent = item.question;
  document.getElementById("quiz-review-unanswered-note").hidden = !item.unanswered;

  const options = document.getElementById("quiz-review-options");
  options.innerHTML = "";
  item.options.forEach((option, optionIndex) => {
    const letter = QUIZ_OPTION_LETTERS[optionIndex];
    const isSelected = item.selected.includes(letter);
    const isCorrect = item.correct.includes(letter);
    const row = document.createElement("div");
    row.className = "quiz-review-option";
    row.dataset.letter = letter;
    if (isCorrect) row.classList.add("is-correct");
    if (isSelected) row.classList.add("is-selected");
    if (isSelected && !isCorrect) row.classList.add("is-wrong");
    const indicator = document.createElement("span");
    indicator.className = "quiz-review-option-indicator";
    indicator.setAttribute("aria-hidden", "true");
    indicator.textContent = isCorrect ? "✓" : (isSelected ? "✕" : "");
    const label = document.createElement("span");
    label.className = "quiz-review-option-label";
    label.textContent = option;
    row.append(indicator, label);
    const tagText = isSelected && isCorrect ? "Your answer · Correct" : (isSelected ? "Your answer" : (isCorrect ? "Correct answer" : ""));
    if (tagText) {
      const tag = document.createElement("span");
      tag.className = "quiz-review-option-tag";
      tag.textContent = tagText;
      row.appendChild(tag);
    }
    options.appendChild(row);
  });

  const explanation = document.getElementById("quiz-review-explanation");
  explanation.textContent = item.explanation || "No explanation was saved for this question.";
  explanation.classList.toggle("is-missing", !item.explanation);
  const sourceParts = [
    item.topicName ? `Topic: ${item.topicName}` : "",
    item.conceptName && item.conceptName !== item.topicName ? `Concept: ${item.conceptName}` : "",
    item.sourceCount ? `Based on ${item.sourceCount} passage${item.sourceCount === 1 ? "" : "s"} from the document` : "",
  ].filter(Boolean);
  document.getElementById("quiz-review-source").hidden = !sourceParts.length;
  document.getElementById("quiz-review-source-text").textContent = sourceParts.join(" · ");

  document.getElementById("quiz-review-previous").disabled = index === 0;
  document.getElementById("quiz-review-next").textContent = index === total - 1 ? "Back to Results" : "Next";
}

function moveQuizReview(direction) {
  if (!quizResultState?.result) return;
  const lastIndex = quizResultState.result.items.length - 1;
  if (direction > 0 && quizResultState.reviewIndex >= lastIndex) {
    quizResultState.view = "results";
    renderQuizResultState();
    return;
  }
  quizResultState.reviewIndex = Math.max(0, Math.min(lastIndex, quizResultState.reviewIndex + direction));
  renderQuizReview();
}

// ---- Quiz Player: answering and navigation -------------------------------------------------------

function selectQuizPlayerAnswer(question, letter, isMultiSelect) {
  const key = String(question.id);
  if (isMultiSelect) {
    const selected = new Set(Array.isArray(quizAnswers[key]) ? quizAnswers[key] : []);
    selected.has(letter) ? selected.delete(letter) : selected.add(letter);
    if (selected.size) quizAnswers[key] = [...selected].sort();
    else delete quizAnswers[key];
  } else {
    quizAnswers[key] = letter;
  }
  renderQuizPlayerQuestion();
  scheduleQuizAutosave();
}

function moveQuizPlayerQuestion(direction) {
  if (!currentQuiz?.questions?.length) return;
  const total = currentQuiz.questions.length;
  quizQuestionIndex = Math.max(0, Math.min(total - 1, quizQuestionIndex + direction));
  renderQuizPlayerQuestion();
  scheduleQuizAutosave();
}

// ---- Quiz Player: exit ---------------------------------------------------------------------------

// Flushes any pending autosave first so "Your progress is saved" is actually true; if that save
// fails the dialog says so, and Continue Quiz keeps the unsaved local answers intact.
async function handleQuizPlayerExit() {
  if (!currentQuiz?.questions?.length || currentAttempt?.completed) {
    closeQuizPlayerToLibrary();
    return;
  }
  let saveFailed = false;
  try { await flushQuizAutosave(); } catch (error) { saveFailed = true; }
  const answeredCount = Object.keys(quizAnswers).length;
  if (answeredCount === 0 && !saveFailed) {
    closeQuizPlayerToLibrary();
    return;
  }
  document.getElementById("quiz-exit-confirm-title").textContent =
    saveFailed ? "Your latest answers are not saved yet" : "Your progress is saved";
  document.getElementById("quiz-exit-confirm-detail").textContent =
    `${answeredCount} of ${currentQuiz.questions.length} answered`;
  document.getElementById("quiz-exit-confirm").hidden = false;
}

// ---- Quiz Player: finish ---------------------------------------------------------------------------

function handleQuizPlayerFinish() {
  if (quizSubmitInFlight) return;
  const total = currentQuiz.questions.length;
  const unanswered = total - Object.keys(quizAnswers).length;
  if (unanswered <= 0) {
    submitQuizPlayer();
    return;
  }
  document.getElementById("quiz-finish-confirm-title").textContent =
    `${unanswered} question${unanswered === 1 ? "" : "s"} unanswered`;
  document.getElementById("quiz-finish-confirm").hidden = false;
}

async function submitQuizPlayer() {
  if (quizSubmitInFlight) return;   // no duplicate submissions
  quizSubmitInFlight = true;
  setQuizPlayerFinishBusy(true);
  try {
    // The submission itself carries every local answer, so a pending autosave is dropped and an
    // in-flight one is waited out -- it must not land on the attempt after it is completed.
    if (quizAutosaveTimer) { clearTimeout(quizAutosaveTimer); quizAutosaveTimer = null; }
    quizAutosaveDirty = false;
    if (quizAutosaveInFlight) { try { await quizAutosaveInFlight; } catch (error) { /* submit covers it */ } }
    currentAttempt = await requestQuizSubmission({ allowUnanswered: true });
    resetQuizAutosave();
    quizQuestionIndex = 0;
    quizAttemptSummary = currentAttempt.attempt_summary;
    await loadQuizStatuses();
    updateDifficultyOptions();
    await loadQuizHistory();
    await loadDashboard();
    renderQuizHistory();
    showQuizResults(buildQuizResult(currentAttempt, currentQuiz));
    plannerAdaptAfterQuiz(currentQuiz.document_id);
  } catch (error) {
    // Local answers are untouched -- the learner can simply press Finish Quiz again.
    showToast(error.message || "Could not submit quiz");
  } finally {
    quizSubmitInFlight = false;
    setQuizPlayerFinishBusy(false);
  }
}

function setQuizPlayerFinishBusy(busy) {
  const submitAnyway = document.getElementById("quiz-finish-submit-anyway");
  if (submitAnyway) submitAnyway.disabled = busy;
  const nextButton = document.getElementById("quiz-player-next");
  if (!nextButton || !currentQuiz?.questions?.length || currentAttempt?.completed) return;
  if (quizQuestionIndex !== currentQuiz.questions.length - 1) return;
  nextButton.disabled = busy;
  nextButton.textContent = busy ? "Submitting…" : "Finish Quiz";
}

function renderAssessmentQuiz() {
  if (quizPlayerOpen) {
    renderQuizPlayer();
    return;
  }
  const quizPane = document.querySelector('[data-session-pane="quiz"]');
  const hasQuiz = Boolean(currentQuiz?.questions?.length);
  quizPane?.classList.toggle("quiz-active", hasQuiz);
  quizPane?.classList.toggle("quiz-landing", !hasQuiz);
  updateQuizLandingLayout();
  quizList.innerHTML = "";
  assessmentTitle.textContent = assessmentTitleText(currentQuiz);
  if (!hasQuiz) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Choose a document, then generate or start its saved quiz.";
    quizList.appendChild(empty);
    updateAssessmentSummary();
    return;
  }

  if (currentAttempt?.completed) {
    renderCompletedQuizReview(quizList, currentAttempt, currentQuiz.questions, {
      back: backToQuizzes,
      retake: resetAssessmentQuiz,
      regenerate: regenerateAssessmentQuiz,
      remove: deleteAssessmentQuiz,
    });
    updateAssessmentSummary();
    return;
  }

  const attemptSummary = renderAttemptSummary();
  if (attemptSummary) quizList.appendChild(attemptSummary);
  if (currentAttempt?.completed) {
    const reviewHeading = document.createElement("div");
    reviewHeading.className = "quiz-review-heading";
    reviewHeading.innerHTML = `<h3>Review · Attempt ${currentAttempt.attempt_number}</h3><strong>${currentAttempt.score}/${currentAttempt.total} · ${Math.round(currentAttempt.percentage)}%</strong>`;
    const retake = document.createElement("button");
    retake.className = "primary-button";
    retake.type = "button";
    retake.textContent = "Retake Quiz";
    retake.addEventListener("click", resetAssessmentQuiz);
    reviewHeading.appendChild(retake);
    quizList.appendChild(reviewHeading);
    currentQuiz.questions.forEach((question, index) => {
      const result = currentAttempt.question_results.find((item) => Number(item.question_id) === Number(question.id));
      if (result) quizList.appendChild(createAssessmentReviewCard(question, result, index));
    });
    updateAssessmentSummary();
    return;
  }

  quizQuestionIndex = Math.max(0, Math.min(quizQuestionIndex, currentQuiz.questions.length - 1));
  const question = currentQuiz.questions[quizQuestionIndex];
  const card = document.createElement("article");
  card.className = "quiz-question-card";
  const heading = document.createElement("div");
  heading.className = "quiz-question-heading";
  const number = document.createElement("span");
  number.textContent = `Question ${quizQuestionIndex + 1} of ${currentQuiz.questions.length}`;
  const count = document.createElement("small");
  count.textContent = `${Object.keys(quizAnswers).length} answered`;
  heading.append(number, count);
  const questionText = document.createElement("h3");
  questionText.textContent = question.question;
  const isMultiSelect = question.question_type === "multi_select";
  let multiSelectHelper = null;
  if (isMultiSelect) {
    multiSelectHelper = document.createElement("p");
    multiSelectHelper.className = "quiz-multiselect-helper";
    multiSelectHelper.textContent = "Select all that apply.";
  }
  const options = document.createElement("div");
  options.className = "answer-list";
  question.options.forEach((option) => {
    const button = document.createElement("button");
    button.className = `answer-option ${isMultiSelect ? "answer-option--checkbox" : "answer-option--radio"}`;
    button.type = "button";
    button.setAttribute("role", isMultiSelect ? "checkbox" : "radio");
    const indicator = document.createElement("span");
    indicator.className = "answer-option-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "answer-option-label";
    label.textContent = option;
    button.append(indicator, label);
    const letter = option.trim().charAt(0).toUpperCase();
    const selected = quizAnswers[String(question.id)];
    const isSelected = Array.isArray(selected) ? selected.includes(letter) : selected === letter;
    button.classList.toggle("selected", isSelected);
    button.setAttribute("aria-checked", String(isSelected));
    button.addEventListener("click", () => selectAssessmentAnswer(question, option));
    options.appendChild(button);
  });
  const navigator = document.createElement("div");
  navigator.className = "quiz-question-navigator";
  currentQuiz.questions.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(index + 1);
    button.className = "quiz-navigator-button";
    button.classList.toggle("current", index === quizQuestionIndex);
    button.classList.toggle("answered", Boolean(quizAnswers[String(item.id)]));
    button.addEventListener("click", () => { quizQuestionIndex = index; renderAssessmentQuiz(); });
    navigator.appendChild(button);
  });
  const navigation = document.createElement("div");
  navigation.className = "quiz-navigation";
  const previous = document.createElement("button");
  previous.className = "secondary-button quiz-nav-button";
  previous.type = "button";
  previous.textContent = "Previous";
  previous.disabled = quizQuestionIndex === 0;
  previous.addEventListener("click", () => moveQuizQuestion(-1));
  const next = document.createElement("button");
  next.className = "secondary-button quiz-nav-button";
  next.type = "button";
  next.textContent = "Next";
  next.disabled = quizQuestionIndex === currentQuiz.questions.length - 1;
  next.addEventListener("click", () => moveQuizQuestion(1));
  const check = document.createElement("button");
  check.className = "primary-button quiz-check-button";
  check.type = "button";
  check.textContent = "Check Answers";
  check.disabled = Object.keys(quizAnswers).length !== currentQuiz.questions.length;
  check.addEventListener("click", submitAssessmentQuiz);
  navigation.append(previous, next, check);
  card.append(heading, questionText, ...(multiSelectHelper ? [multiSelectHelper] : []), options, navigator, navigation);
  quizList.appendChild(card);
  updateAssessmentSummary();
}

function moveQuizQuestionLegacy(direction) {
  quizQuestionIndex = Math.max(0, Math.min(currentQuiz.questions.length - 1, quizQuestionIndex + direction));
  renderAssessmentQuiz();
}

function selectAssessmentAnswer(question, option) {
  if (currentAttempt?.completed) return;
  const key = String(question.id);
  const letter = option.trim().charAt(0).toUpperCase();
  if (question.question_type === "multi_select") {
    const selected = new Set(Array.isArray(quizAnswers[key]) ? quizAnswers[key] : []);
    selected.has(letter) ? selected.delete(letter) : selected.add(letter);
    if (selected.size) quizAnswers[key] = [...selected].sort();
    else delete quizAnswers[key];
  } else {
    quizAnswers[key] = letter;
  }
  renderAssessmentQuiz();
}

async function submitAssessmentQuiz(event) {
  const button = event?.currentTarget;
  if (Object.keys(quizAnswers).length !== currentQuiz.questions.length) return;
  if (button) { button.disabled = true; button.textContent = "Checking..."; }
  try {
    currentAttempt = await requestQuizSubmission();
    quizQuestionIndex = 0;
    quizAttemptSummary = currentAttempt.attempt_summary;
    await loadQuizHistory();
    await loadDashboard();
    renderAssessmentQuiz();
    showToast(`Attempt ${currentAttempt.attempt_number}: ${currentAttempt.score}/${currentAttempt.total}`);
    plannerAdaptAfterQuiz(currentQuiz.document_id);
  } catch (error) {
    renderAssessmentQuiz();
    showToast(error.message || "Could not submit quiz");
  }
}

function resetAssessmentQuiz() {
  if (!currentQuiz?.questions?.length) return;
  currentAttempt = null;
  quizAnswers = {};
  quizExplanations = {};
  quizQuestionIndex = 0;
  renderAssessmentQuiz();
  showToast("Retake started with the same questions");
}

function resetApp() {
  state.page = initialState.page;
  state.confidence = initialState.confidence;
  state.quizIndex = initialState.quizIndex;
  state.quizScore = initialState.quizScore;
  state.answered = initialState.answered;
  messageList.innerHTML = "";
  renderConversationMessages();
  renderSources(uploadedSources, "uploaded");
  updateConfidence(initialState.confidence);
  currentQuiz = null;
  currentAttempt = null;
  quizAnswers = {};
  quizQuestionIndex = 0;
  renderAssessmentQuiz();
  setPage(initialState.page);
  showToast("Tutoring reset");
}

navItems.forEach((item) => {
  item.addEventListener("click", () => {
    if (item.dataset.page) setPage(item.dataset.page);
  });
});

sidebarMenuButton?.addEventListener("click", () => setSidebarOpen(!document.body.classList.contains("sidebar-open")));
sidebarBackdrop?.addEventListener("click", () => setSidebarOpen(false));
document.getElementById("sidebar-close-button")?.addEventListener("click", () => setSidebarOpen(false));
document.getElementById("app-sidebar")?.addEventListener("click", (event) => {
  if (event.target.closest("button.nav-item, .sidebar-recent-item, #upload-source-button, #logout-button")) setSidebarOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) setSidebarOpen(false);
});
window.matchMedia?.("(min-width: 721px)").addEventListener?.("change", (event) => { if (event.matches) setSidebarOpen(false); });

document.getElementById("session-home-button")?.addEventListener("click", () => setPage("overview"));
regenerateSummaryButton?.addEventListener("click", () => loadDocumentSummary(true));
generateSummaryButton?.addEventListener("click", () => loadDocumentSummary());
flashcardElement?.addEventListener("click", () => { flashcardFlipped = !flashcardFlipped; renderCurrentFlashcard(); });
document.getElementById("previous-flashcard")?.addEventListener("click", () => { const cards = visibleFlashcards(); if (cards.length) { flashcardIndex = (flashcardIndex - 1 + cards.length) % cards.length; flashcardFlipped = false; renderCurrentFlashcard(); } });
document.getElementById("next-flashcard")?.addEventListener("click", () => { const cards = visibleFlashcards(); if (cards.length) { flashcardIndex = (flashcardIndex + 1) % cards.length; flashcardFlipped = false; renderCurrentFlashcard(); } });
flashcardTopicSelect?.addEventListener("change", () => { flashcardTopicFilter = flashcardTopicSelect.value; flashcardIndex = 0; flashcardFlipped = false; renderCurrentFlashcard(); });
flashcardLanguageSelect?.addEventListener("change", () => {
  flashcardLanguage = flashcardLanguageSelect.value;
  localStorage.setItem("aiTutorFlashcardLanguage", flashcardLanguage);
  loadedFlashcardKey = "";
  showFlashcardsState();
});
generateFlashcardsButton?.addEventListener("click", () => loadDocumentFlashcards());
regenerateFlashcardsButton?.addEventListener("click", () => loadDocumentFlashcards(true));
retryFlashcardsButton?.addEventListener("click", () => loadDocumentFlashcards(flashcards.length > 0));
retryRegenerateFlashcardsButton?.addEventListener("click", () => loadDocumentFlashcards(true));
document.getElementById("shuffle-flashcards")?.addEventListener("click", () => { for (let index = flashcards.length - 1; index > 0; index -= 1) { const swap = Math.floor(Math.random() * (index + 1)); [flashcards[index], flashcards[swap]] = [flashcards[swap], flashcards[index]]; } flashcardIndex = 0; flashcardFlipped = false; renderCurrentFlashcard(); });
document.getElementById("manage-flashcards")?.addEventListener("click", openFlashcardManager);
favoriteFlashcardButton?.addEventListener("click", async () => { const card = visibleFlashcards()[flashcardIndex]; if (!card) return; try { await patchFlashcard(card, { is_favorite: !card.is_favorite }); renderCurrentFlashcard(); } catch (error) { showToast(error.message); } });
document.querySelectorAll(".session-tab, [data-session-tab]").forEach((button) => {
  button.addEventListener("click", () => setSessionTab(button.dataset.sessionTab));
});
document.getElementById("session-tutor-toggle")?.addEventListener("click", () => {
  const panel = document.getElementById("persistent-tutor");
  panel.hidden = !panel.hidden;
  document.getElementById("session-tutor-toggle").setAttribute("aria-expanded", String(!panel.hidden));
});

document.querySelectorAll("[data-page-target]").forEach((button) => {
  button.addEventListener("click", () => setPage(button.dataset.pageTarget));
});

chatForm.addEventListener("submit", handleChatSubmit);
resetButton.addEventListener("click", resetApp);
uploadSourceButton.addEventListener("click", () => sourceFileInput.click());
sourceFileInput.addEventListener("change", () => uploadSourceFiles(sourceFileInput.files));
sessionSearchInput?.addEventListener("input", applySessionLibraryFilters);
sessionSortSelect?.addEventListener("change", applySessionLibraryFilters);
newConversationButton.addEventListener("click", createChatConversation);
applyConversationSourcesButton.addEventListener("click", applyConversationSources);
toggleConversationSourcesButton.addEventListener("click", () => {
  setSourcesDrawerOpen(!tutorLayout.classList.contains("sources-open"));
});
closeConversationSourcesButton.addEventListener("click", closeSourcesDrawer);
sourcesDrawerBackdrop.addEventListener("click", closeSourcesDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && tutorLayout.classList.contains("sources-open")) {
    closeSourcesDrawer();
    toggleConversationSourcesButton.focus();
  }
});
generateQuizButton.addEventListener("click", generateAssessmentQuiz);
document.getElementById("quiz-sheet-retry-button")?.addEventListener("click", () => generateAssessmentQuiz());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && quizCreateDialog?.classList.contains("open")) closeQuizCreateDialog();
});
newQuizButton.addEventListener("click", regenerateAssessmentQuiz);
resetQuizButton.addEventListener("click", resetAssessmentQuiz);
deleteQuizButton?.addEventListener("click", deleteAssessmentQuiz);
reviewQuizButton.addEventListener("click", () => {
  if (currentAttempt?.completed) renderAssessmentQuiz();
});
backToQuizzesButton.addEventListener("click", backToQuizzes);
quizDocumentSelect.addEventListener("change", handleQuizDocumentChange);
quizScopeSelect.addEventListener("change", loadSelectedQuiz);

document.getElementById("quiz-player-exit")?.addEventListener("click", handleQuizPlayerExit);
document.getElementById("quiz-player-previous")?.addEventListener("click", () => moveQuizPlayerQuestion(-1));
document.getElementById("quiz-exit-confirm-continue")?.addEventListener("click", () => {
  document.getElementById("quiz-exit-confirm").hidden = true;
});
document.getElementById("quiz-exit-confirm-exit")?.addEventListener("click", () => {
  document.getElementById("quiz-exit-confirm").hidden = true;
  closeQuizPlayerToLibrary();
});
document.getElementById("quiz-finish-review")?.addEventListener("click", () => {
  document.getElementById("quiz-finish-confirm").hidden = true;
  const firstUnanswered = currentQuiz.questions.findIndex((question) => !quizAnswers[String(question.id)]);
  quizQuestionIndex = firstUnanswered >= 0 ? firstUnanswered : 0;
  renderQuizPlayerQuestion();
  scheduleQuizAutosave();
});
document.getElementById("quiz-finish-submit-anyway")?.addEventListener("click", () => {
  document.getElementById("quiz-finish-confirm").hidden = true;
  submitQuizPlayer();
});
document.getElementById("quiz-results-back")?.addEventListener("click", closeQuizPlayerToLibrary);
document.getElementById("quiz-results-review")?.addEventListener("click", () => openQuizReview(0));
document.getElementById("quiz-review-back-results")?.addEventListener("click", () => {
  if (!quizResultState?.result) return;
  quizResultState.view = "results";
  renderQuizResultState();
});
document.getElementById("quiz-review-previous")?.addEventListener("click", () => moveQuizReview(-1));
document.getElementById("quiz-review-next")?.addEventListener("click", () => moveQuizReview(1));

authForm.addEventListener("submit", handleAuthentication);
authSwitch.addEventListener("click", () => setAuthMode(authMode === "login" ? "signup" : "login"));
logoutButton.addEventListener("click", signOut);
quizDifficultySelect.addEventListener("change", handleQuizDifficultyChange);
refreshQuizHistoryButton.addEventListener("click", loadQuizHistory);
quizPopoverPanels.forEach((panel) => {
  panel.addEventListener("toggle", () => {
    if (!panel.open) {
      return;
    }
    quizPopoverPanels.forEach((otherPanel) => {
      if (otherPanel !== panel) {
        otherPanel.removeAttribute("open");
      }
    });
  });
});
document.addEventListener("click", (event) => {
  if (![...quizPopoverPanels].some((panel) => panel.contains(event.target))) {
    quizPopoverPanels.forEach((panel) => panel.removeAttribute("open"));
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    quizPopoverPanels.forEach((panel) => panel.removeAttribute("open"));
  }
});

document.body.dataset.page = state.page;
updateConfidence(state.confidence);
renderAssessmentQuiz();
async function initializeChatWorkspace() {
  await loadUploadedSources();
  try {
    await loadConversations();
    renderDashboard();
  } catch (error) {
    showToast(error.message || "Could not load conversations");
  }
}

function updateQuizLandingLayout() {
  const pane = document.querySelector('[data-session-pane="quiz"]');
  if (!pane) return;
  ensureQuizQuestionCountSelect();
  let header = pane.querySelector(".quiz-landing-header");
  if (!header) {
    header = document.createElement("div");
    header.className = "quiz-landing-header";
    header.innerHTML = '<div><h2>Quiz</h2><p>Test your knowledge and track your progress</p></div>';
    const create = document.createElement("button");
    create.className = "primary-button";
    create.type = "button";
    create.textContent = "+ New Quiz";
    // Temporary: reuses the existing creation dialog/flow. A dedicated New Quiz modal is a
    // separate, later piece of work.
    create.addEventListener("click", openQuizCreateDialog);
    header.appendChild(create);
    pane.insertBefore(header, pane.firstChild);
  }
  header.hidden = Boolean(currentQuiz?.questions?.length);
  const historyTitle = pane.querySelector(".quiz-history-panel h2");
  if (historyTitle) historyTitle.textContent = "Quiz Library";
  const createDialogOpen = Boolean(quizCreateDialog?.classList.contains("open"));
  if (assessmentControl) {
    assessmentControl.hidden = !currentQuiz?.questions?.length && !createDialogOpen;
  }
}

// The question-count select is part of the quiz form whenever it is shown (also next to a saved
// quiz, where "Regenerate Quiz" reads it), so it is built once with the screen, not with the dialog.
function ensureQuizQuestionCountSelect() {
  if (quizQuestionCountSelect || !generateQuizButton) return;
  const label = document.createElement("label");
  label.id = "quiz-question-count-field";
  const caption = document.createElement("span");
  caption.textContent = "Number of questions";
  quizQuestionCountSelect = document.createElement("select");
  quizQuestionCountSelect.id = "quiz-question-count-select";
  quizQuestionCountSelect.className = "visually-hidden";
  [12, 15, 18, 20].forEach((count) => {
    const option = document.createElement("option");
    option.value = String(count);
    option.textContent = String(count);
    option.selected = count === 12;
    quizQuestionCountSelect.appendChild(option);
  });
  const segmented = document.createElement("div");
  segmented.className = "quiz-segmented";
  segmented.id = "quiz-count-segmented";
  segmented.setAttribute("role", "radiogroup");
  segmented.setAttribute("aria-label", "Number of questions");
  // Changing the settings never changes a saved quiz: only the "saved" marks and the hint update.
  quizQuestionCountSelect.addEventListener("change", () => {
    updateDifficultyOptions();
    renderQuizCountSegments();
    if (currentQuiz?.questions?.length) assessmentTitle.textContent = assessmentTitleText(currentQuiz);
  });
  label.append(caption, quizQuestionCountSelect, segmented);
  generateQuizButton.before(label);
  quizQuestionCountField = label;
  renderQuizCountSegments();
}

// The Create Quiz sheet's visible Question count control: segmented buttons mirroring the real
// (visually hidden) #quiz-question-count-select.
function renderQuizCountSegments() {
  const container = document.getElementById("quiz-count-segmented");
  if (!container || !quizQuestionCountSelect) return;
  container.innerHTML = "";
  Array.from(quizQuestionCountSelect.options).forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quiz-segmented-option";
    button.textContent = option.value;
    button.setAttribute("role", "radio");
    const selected = option.value === quizQuestionCountSelect.value;
    button.setAttribute("aria-checked", String(selected));
    button.classList.toggle("active", selected);
    button.addEventListener("click", () => {
      if (quizQuestionCountSelect.value === option.value) return;
      quizQuestionCountSelect.value = option.value;
      renderQuizCountSegments();
      quizQuestionCountSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    container.appendChild(button);
  });
}

// Keeps the friendly failure text as the only thing shown by default; whatever the backend sent is
// still reachable through the collapsed "Technical details" toggle, never as the primary line.
function showQuizSheetError(message) {
  const container = document.getElementById("quiz-sheet-error");
  if (!container) return;
  const messageElement = document.getElementById("quiz-sheet-error-message");
  const technicalElement = document.getElementById("quiz-sheet-error-technical");
  if (messageElement) messageElement.textContent = "Couldn't create the quiz. Please try again or switch models.";
  if (technicalElement) technicalElement.textContent = message || "Unknown error.";
  container.hidden = false;
}

function hideQuizSheetError() {
  const container = document.getElementById("quiz-sheet-error");
  if (container) container.hidden = true;
}

// isGenerating toggles the in-sheet "Generating…" indicator, disables Cancel (so a mid-generation
// click can never discard the request), and gates duplicate-submission/close-while-generating via
// quizGenerationInFlight.
function setQuizSheetGenerating(isGenerating) {
  quizGenerationInFlight = isGenerating;
  const generating = document.getElementById("quiz-sheet-generating");
  if (generating) generating.hidden = !isGenerating;
  const cancelButton = quizCreateDialog?.querySelector(".quiz-dialog-cancel");
  if (cancelButton) cancelButton.disabled = isGenerating;
}

function openQuizCreateDialog() {
  if (!assessmentControl) return;
  if (quizNameInput) quizNameInput.value = "";
  hideQuizSheetError();
  setQuizSheetGenerating(false);
  ensureQuizQuestionCountSelect();
  renderQuizDifficultySegments();
  renderQuizCountSegments();
  renderQuizSheetModelSummary();
  if (!quizCreateDialog) {
    quizCreateDialog = document.createElement("div");
    quizCreateDialog.className = "quiz-create-dialog";
    quizCreateDialog.innerHTML = '<div class="quiz-create-dialog-card" role="dialog" aria-modal="true" aria-labelledby="quiz-create-title"><div class="quiz-dialog-heading"><div><h2 id="quiz-create-title">Create a Quiz</h2><p>Choose how you want to assess this material.</p></div><button class="text-button quiz-dialog-cancel" type="button">Cancel</button></div><div class="quiz-dialog-fields"></div></div>';
    quizCreateDialog.querySelector(".quiz-dialog-cancel").addEventListener("click", () => closeQuizCreateDialog());
    quizCreateDialog.addEventListener("click", (event) => { if (event.target === quizCreateDialog) closeQuizCreateDialog(); });
    document.body.appendChild(quizCreateDialog);
  }
  quizCreateDialog.classList.add("open");
  quizCreateDialog.querySelector(".quiz-dialog-fields").appendChild(assessmentControl);
  assessmentControl.hidden = false;
}

// Cancel / backdrop click / Escape all funnel through here: while a generation is in flight this is
// a safe no-op (never interrupts the request); otherwise it closes with no side effects -- nothing
// is sent, nothing already entered is discarded beyond the dialog simply closing.
function closeQuizCreateDialog(options = {}) {
  const force = Boolean(options && options.force);
  if (quizGenerationInFlight && !force) return;
  if (!quizCreateDialog || !assessmentControl) return;
  const shell = document.querySelector('[data-session-pane="quiz"] .assessment-shell');
  shell?.insertBefore(assessmentControl, shell.firstChild);
  assessmentControl.hidden = true;
  quizCreateDialog.classList.remove("open");
}

// ---------------------------------------------------------------------------
// Study Planner v2 -- document-centric plan creation:
// Materials (+ optional deadlines) -> Availability -> Preview -> Adjust / Confirm -> saved plan.
// Reuses the weekly availability calendar and /api/planner/availability. No topics, no scores.
// ---------------------------------------------------------------------------

const PLANNER_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PLANNER_GRID_START_MINUTE = 6 * 60;
const PLANNER_GRID_END_MINUTE = 23 * 60;
const PLANNER_CELL_MINUTES = 30;
const PLANNER_STEPS = ["materials", "availability", "preview", "plan"];
const PLANNER_ACTIVE_SESSION_STATUSES = ["scheduled", "in_progress"];
const PLANNER_ACTIVITY_LABELS = {
  summary: "Summary", flashcards: "Flashcards", quiz: "Quiz", review: "Review", quiz_retry: "Quiz retry",
};

function plannerMondayOf(reference) {
  const date = new Date(reference);
  date.setHours(0, 0, 0, 0);
  const offset = (date.getDay() + 6) % 7; // getDay(): 0=Sun..6=Sat -> offset from Monday
  date.setDate(date.getDate() - offset);
  return date;
}

function plannerDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function plannerToMinutes(hhmm) {
  const [hour, minute] = hhmm.split(":").map(Number);
  return hour * 60 + minute;
}

function plannerMinutesToLabel(minutes) {
  const hour = Math.floor(minutes / 60), minute = minutes % 60;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function plannerFormatDuration(totalMinutes) {
  const minutes = Math.max(0, Math.round(totalMinutes));
  const hours = Math.floor(minutes / 60), remainder = minutes % 60;
  if (hours && remainder) return `${hours}h ${remainder}m`;
  if (hours) return `${hours}h`;
  return `${remainder}m`;
}

function plannerFormatDateLabel(date) {
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function plannerDayLabel(dateKey) {
  const [year, month, day] = dateKey.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function plannerWeekDates(weekStart = plannerWeekStart) {
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(weekStart);
    date.setDate(date.getDate() + index);
    return date;
  });
}

function plannerNow() {
  // The browser's local clock drives "today" and "this week" (sessions are stored as naive
  // learner-local times). A single seam so tests can pin the date.
  return new Date();
}

function plannerLocalIso(date) {
  // Naive local "YYYY-MM-DDTHH:MM:SS", directly comparable with a session's scheduled_start/end.
  const time = [date.getHours(), date.getMinutes(), date.getSeconds()].map((part) => String(part).padStart(2, "0")).join(":");
  return `${plannerDateKey(date)}T${time}`;
}

function plannerIsActiveSession(session) {
  return PLANNER_ACTIVE_SESSION_STATUSES.includes(session.status);
}

function plannerUtcOffsetMinutes() {
  // The learner's own offset from UTC (e.g. 420 for UTC+7) -- the only timezone input the planner
  // API takes. "now" itself is left to the server (current UTC instant + this offset).
  return -new Date().getTimezoneOffset();
}

class PlannerRequestError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function plannerRequest(url, options = {}) {
  // Like fetchJson, but keeps a structured error detail ({code, message, ...}) intact.
  const init = { ...options };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const response = await fetch(url, init);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    if (response.status === 401 && currentUser) showAuthentication();
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : detail?.message || `Request returned ${response.status}`;
    throw new PlannerRequestError(message, response.status, detail);
  }
  return payload;
}

function plannerPlanUrl(suffix = "") {
  return `${PLANNER_PLANS_API_URL}/${encodeURIComponent(plannerPlan.plan_id)}${suffix}`;
}

async function loadPlannerData({ keepWeek = false } = {}) {
  try {
    const [plans, availability] = await Promise.all([
      plannerRequest(PLANNER_PLANS_API_URL), plannerRequest(PLANNER_AVAILABILITY_API_URL),
    ]);
    plannerAvailability = availability;
    const active = plans.filter((plan) => plan.status === "active");
    plannerPlan = active[active.length - 1] || null;
    plannerMaterials = [];
    plannerSessions = [];
    plannerHistorySessions = [];
    plannerSessionsById = new Map();
    if (!keepWeek) plannerPlanWeekStart = null;
    if (plannerPlan) {
      const [detail, saved] = await Promise.all([plannerRequest(plannerPlanUrl()), plannerRequest(plannerPlanUrl("/sessions"))]);
      const sessions = saved.map((session) => ({ ...session, plan_id: plannerPlan.plan_id }));
      plannerSessionsById = new Map(sessions.map((session) => [session.session_id, session]));
      plannerMaterials = detail.materials;
      plannerSessions = sessions.filter(plannerIsActiveSession);
      plannerHistorySessions = sessions.filter((session) => PLANNER_HISTORY_SESSION_STATUSES.includes(session.status));
    }
    if (plannerSessions.length || plannerHistorySessions.length) plannerStep = "plan";
    if (plannerPlan && plannerStep === "plan") loadPlannerPlanProgress(plannerPlan.plan_id);
    else if (plannerStep === "plan") plannerStep = "materials";
    if (!keepWeek) plannerCalWeekStart = null;
    if (plannerPlacements.length && plannerPlacementsPlanId !== plannerPlan?.plan_id) plannerPlacements = [];
    plannerPlacementsPlanId = plannerPlan?.plan_id || null;
    renderPlanner();
    if (plannerIsDesktop()) {
      plannerQueueAutoPreview(0);
      plannerLoadLiveCandidates();
      plannerLoadDocStates();
    }
  } catch (error) {
    showToast(error.message || "Could not load Study Planner data");
  }
}

function plannerSetStep(step) {
  plannerStep = step;
  renderPlanner();
}

function renderPlanner() {
  if (!plannerView) return;
  // Desktop (>=1024px) plans on one calendar workspace; smaller screens keep the step-by-step flow.
  const desktop = plannerIsDesktop();
  if (plannerShell) plannerShell.hidden = desktop;
  if (plannerWorkspace) plannerWorkspace.hidden = !desktop;
  if (desktop) {
    renderPlannerWorkspace();
    return;
  }
  const current = PLANNER_STEPS.indexOf(plannerStep);
  plannerView.querySelectorAll("[data-planner-step]").forEach((section) => {
    section.hidden = section.dataset.plannerStep !== plannerStep;
  });
  plannerView.querySelectorAll("[data-step-indicator]").forEach((item) => {
    const index = PLANNER_STEPS.indexOf(item.dataset.stepIndicator);
    item.classList.toggle("active", index === current);
    item.classList.toggle("done", index < current);
    if (index === current) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  if (plannerStep === "materials") renderPlannerMaterials();
  if (plannerStep === "availability") renderPlannerCalendar();
  if (plannerStep === "preview") renderPlannerPreview();
  if (plannerStep === "plan") renderPlannerPlan();
}

// ---- Step 1: materials + optional deadlines --------------------------------

function plannerMaterialFor(documentId) {
  return plannerMaterials.find((material) => material.document_id === documentId) || null;
}

function renderPlannerMaterials() {
  if (!plannerMaterialList) return;
  plannerMaterialList.innerHTML = "";
  plannerMaterialEmpty.hidden = indexedDocuments.length > 0;
  const today = plannerDateKey(new Date());
  indexedDocuments.forEach((doc) => {
    const material = plannerMaterialFor(doc.id);
    const row = document.createElement("div");
    row.className = "planner-material-row";
    row.classList.toggle("selected", Boolean(material));
    const pick = document.createElement("label");
    pick.className = "planner-material-pick";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(material);
    checkbox.dataset.documentId = doc.id;
    checkbox.addEventListener("change", () => plannerToggleMaterial(doc.id, checkbox));
    const name = document.createElement("span");
    name.textContent = doc.title;
    pick.append(checkbox, name);
    row.appendChild(pick);
    if (material) {
      const deadline = document.createElement("label");
      deadline.className = "planner-material-deadline";
      const caption = document.createElement("span");
      caption.textContent = "Deadline (optional)";
      const input = document.createElement("input");
      input.type = "date";
      input.min = today;
      input.value = material.deadline || "";
      input.setAttribute("aria-label", `Deadline for ${doc.title}`);
      input.addEventListener("change", () => plannerSetDeadline(material, input));
      deadline.append(caption, input);
      row.appendChild(deadline);
    }
    plannerMaterialList.appendChild(row);
  });
  plannerToAvailabilityButton.disabled = plannerMaterials.length === 0;
}

async function plannerEnsurePlan() {
  if (!plannerPlan) {
    plannerPlan = await plannerRequest(PLANNER_PLANS_API_URL, { method: "POST", body: { title: "My study plan" } });
  }
  return plannerPlan;
}

async function plannerToggleMaterial(documentId, checkbox) {
  checkbox.disabled = true;
  try {
    if (checkbox.checked) {
      await plannerEnsurePlan();
      const material = await plannerRequest(plannerPlanUrl("/materials"), { method: "POST", body: { document_id: documentId } });
      plannerMaterials = [...plannerMaterials, material];
    } else {
      const material = plannerMaterialFor(documentId);
      if (material) {
        await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), { method: "DELETE" });
      }
      plannerMaterials = plannerMaterials.filter((item) => item.document_id !== documentId);
    }
    plannerPreview = null;
  } catch (error) {
    showToast(error.message || "Could not update the plan");
  }
  renderPlannerMaterials();
}

async function plannerSetDeadline(material, input) {
  try {
    const updated = await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), {
      method: "PATCH", body: { deadline: input.value || null },
    });
    plannerMaterials = plannerMaterials.map((item) => (item.material_id === updated.material_id ? updated : item));
    plannerPreview = null;
  } catch (error) {
    input.value = material.deadline || "";
    showToast(error.message || "Could not save the deadline");
  }
}

// ---- Step 2: availability (the existing weekly calendar) --------------------

function plannerAvailabilityCoversDate(slot, dateKey, weekday) {
  return slot.is_recurring ? slot.day_of_week === weekday : slot.date === dateKey;
}

function plannerIsMinuteAvailable(dateKey, weekday, minute) {
  return plannerAvailability.some((slot) => {
    if (!plannerAvailabilityCoversDate(slot, dateKey, weekday)) return false;
    return minute >= plannerToMinutes(slot.start_at) && minute < plannerToMinutes(slot.end_at);
  });
}

function plannerSessionAt(dateKey, minute) {
  return plannerSessions.find((session) => {
    if (session.scheduled_start.slice(0, 10) !== dateKey) return false;
    const start = plannerToMinutes(session.scheduled_start.slice(11, 16));
    const end = plannerToMinutes(session.scheduled_end.slice(11, 16));
    return minute < end && minute + PLANNER_CELL_MINUTES > start;
  });
}

function renderPlannerCalendar() {
  if (!plannerCalendar) return;
  const weekDates = plannerWeekDates();
  if (plannerWeekLabel) {
    plannerWeekLabel.textContent = `${plannerFormatDateLabel(weekDates[0])} – ${plannerFormatDateLabel(weekDates[6])}`;
  }
  plannerCalendar.innerHTML = "";

  const header = document.createElement("div");
  header.className = "planner-grid-header";
  header.appendChild(document.createElement("div")).className = "planner-grid-corner";
  weekDates.forEach((date) => {
    const cell = document.createElement("div");
    cell.className = "planner-grid-day-label";
    cell.textContent = `${PLANNER_DAY_LABELS[(date.getDay() + 6) % 7]} ${date.getDate()}`;
    header.appendChild(cell);
  });
  plannerCalendar.appendChild(header);

  const body = document.createElement("div");
  body.className = "planner-grid-body";
  for (let minute = PLANNER_GRID_START_MINUTE; minute < PLANNER_GRID_END_MINUTE; minute += PLANNER_CELL_MINUTES) {
    const rowLabel = document.createElement("div");
    rowLabel.className = "planner-grid-time-label";
    if (minute % 60 === 0) rowLabel.textContent = plannerMinutesToLabel(minute);
    body.appendChild(rowLabel);
    weekDates.forEach((date) => {
      const dateKey = plannerDateKey(date);
      const weekday = (date.getDay() + 6) % 7;
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "planner-cell";
      cell.dataset.date = dateKey;
      cell.dataset.weekday = String(weekday);
      cell.dataset.minute = String(minute);
      cell.setAttribute("aria-label", `${dateKey} ${plannerMinutesToLabel(minute)}`);
      cell.classList.toggle("available", plannerIsMinuteAvailable(dateKey, weekday, minute));
      const session = plannerSessionAt(dateKey, minute);
      if (session) {
        // A saved session: shown on the calendar, not paintable.
        cell.classList.add("block-confirmed");
        cell.title = `${session.document_title || session.document_id} · ${PLANNER_ACTIVITY_LABELS[session.activity_type] || session.activity_type}`;
        if (plannerSessionAt(dateKey, minute - PLANNER_CELL_MINUTES) !== session) {  // first cell of the session
          const label = document.createElement("span");
          label.className = "planner-cell-block-label";
          label.textContent = session.document_title || session.document_id;
          cell.appendChild(label);
        }
      } else {
        cell.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          plannerStartDrag(cell);
        });
      }
      body.appendChild(cell);
    });
  }
  plannerCalendar.appendChild(body);
}

function plannerStartDrag(cell) {
  const minute = Number(cell.dataset.minute);
  plannerDrag = {
    date: cell.dataset.date, weekday: Number(cell.dataset.weekday),
    start: minute, end: minute + PLANNER_CELL_MINUTES, mode: plannerMode,
  };
  cell.classList.add("dragging");
}

function plannerExtendDrag(cell) {
  if (!plannerDrag || cell.dataset.date !== plannerDrag.date) return;
  const minute = Number(cell.dataset.minute);
  plannerDrag.start = Math.min(plannerDrag.start, minute);
  plannerDrag.end = Math.max(plannerDrag.end, minute + PLANNER_CELL_MINUTES);
  document.querySelectorAll(`.planner-cell[data-date="${plannerDrag.date}"]`).forEach((element) => {
    const elementMinute = Number(element.dataset.minute);
    element.classList.toggle("dragging", elementMinute >= plannerDrag.start && elementMinute < plannerDrag.end);
  });
}

async function plannerFinishDrag() {
  if (!plannerDrag) return;
  const drag = plannerDrag;
  plannerDrag = null;
  document.querySelectorAll(".planner-cell.dragging").forEach((element) => element.classList.remove("dragging"));
  const payload = {
    start_at: plannerMinutesToLabel(drag.start), end_at: plannerMinutesToLabel(drag.end),
    is_recurring: Boolean(plannerRepeatWeeklyCheckbox?.checked),
  };
  if (payload.is_recurring) payload.day_of_week = drag.weekday; else payload.date = drag.date;
  const url = drag.mode === "erase" ? `${PLANNER_AVAILABILITY_API_URL}/remove` : PLANNER_AVAILABILITY_API_URL;
  try {
    plannerAvailability = await plannerRequest(url, { method: "POST", body: payload });
    plannerPreview = null;
    renderPlannerCalendar();
  } catch (error) {
    showToast(error.message || "Could not update availability");
  }
}

// ---- Steps 3 & 4: preview, confirm, saved plan ------------------------------

function plannerGroupByDay(sessions) {
  const groups = new Map();
  sessions.forEach((session) => {
    const key = session.scheduled_start.slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(session);
  });
  return [...groups.entries()];
}

function plannerSessionItem(session, tag = "li", { action = false } = {}) {
  const item = document.createElement(tag);
  item.className = `planner-session planner-session--${session.activity_type}`;
  const time = document.createElement("span");
  time.className = "planner-session-time";
  time.textContent = `${session.scheduled_start.slice(11, 16)}–${session.scheduled_end.slice(11, 16)}`;
  const content = document.createElement("div");
  content.className = "planner-session-body";
  const title = document.createElement("strong");
  title.textContent = session.document_title || session.document_id;
  const meta = document.createElement("span");
  meta.className = "planner-session-meta";
  const chip = document.createElement("span");
  chip.className = "planner-activity-chip";
  chip.textContent = PLANNER_ACTIVITY_LABELS[session.activity_type] || session.activity_type;
  const duration = document.createElement("span");
  duration.textContent = plannerFormatDuration(session.duration_minutes);
  meta.append(chip, duration);
  const reason = document.createElement("small");
  reason.className = "planner-session-reason";
  reason.textContent = session.reason?.message || "";
  content.append(title, meta, reason);
  item.append(time, content);
  if (action && session.session_id) plannerAddSessionActions(item, content, session, `${title.textContent} (${chip.textContent})`);
  return item;
}

const PLANNER_HISTORY_SESSION_STATUSES = ["completed", "skipped"];
const PLANNER_STATUS_LABELS = { completed: "Completed", skipped: "Skipped" };

function plannerIsOverdue(session, now = plannerNow()) {
  // A scheduled session whose end has passed on the browser's local clock (both are naive local times).
  return session.status === "scheduled" && session.scheduled_end <= plannerLocalIso(now);
}

function plannerAddSessionActions(item, content, session, name) {
  if (PLANNER_STATUS_LABELS[session.status]) {
    const badge = document.createElement("span");
    badge.className = `planner-session-status planner-session-status--${session.status}`;
    badge.textContent = PLANNER_STATUS_LABELS[session.status];
    item.classList.add("planner-session--done");
    item.appendChild(badge);
    return;
  }
  let actions = [];
  if (session.status === "in_progress") actions = [["complete", "Complete", true], ["start", "Resume", false]];
  else if (plannerIsOverdue(session)) {
    const note = document.createElement("small");
    note.className = "planner-session-note";
    note.textContent = "Session not completed";
    content.appendChild(note);
    item.classList.add("planner-session--overdue");
    actions = [["reschedule", "Reschedule", true], ["skip", "Skip", false]];
  } else if (session.status === "scheduled") actions = [["start", "Start", true]];
  if (!actions.length) return;
  item.classList.add("planner-session--actionable");
  const group = document.createElement("div");
  group.className = "planner-session-actions";
  actions.forEach(([kind, label, primary]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `${primary ? "primary-button" : "secondary-button"} planner-session-action planner-session-${kind}`;
    button.dataset.sessionAction = kind;
    button.dataset.sessionId = session.session_id;
    button.textContent = label;
    button.setAttribute("aria-label", `${label} ${name}`);
    button.addEventListener("click", () => plannerSessionAction(session, kind, button, group));
    group.appendChild(button);
  });
  item.appendChild(group);
}

async function plannerQuizTarget(session) {
  // The exact quiz a planned quiz / quiz_retry session is about: its own artifact (the quiz to
  // take or retake), else the document's quiz already in progress. null = the Quiz library.
  await loadQuizStatuses();
  const variants = (quizStatuses.find((item) => item.document_id === session.document_id)?.variants || [])
    .filter((variant) => variant.quiz_id);
  const byRecent = (left, right) => new Date(right.updated_at || right.created_at || 0) - new Date(left.updated_at || left.created_at || 0);
  const variant = session.artifact_id
    ? variants.find((item) => item.quiz_id === session.artifact_id)
    : variants.filter((item) => item.progress_status === "in_progress").sort(byRecent)[0];
  const quizId = session.artifact_id || variant?.quiz_id;
  if (!quizId) return null;
  return { document_id: session.document_id, topic_id: variant?.topic_id || "document", difficulty: variant?.difficulty, quiz_id: quizId };
}

const plannerBusySessions = new Set();   // session ids with a lifecycle request in flight

async function plannerSessionAction(session, kind, button, group) {
  // One request per session at a time: a double click (or the same session in two lists) acts once.
  if (plannerBusySessions.has(session.session_id)) return;
  plannerBusySessions.add(session.session_id);
  const buttons = [...group.querySelectorAll("button")];
  const label = button.textContent;
  buttons.forEach((item) => { item.disabled = true; });
  button.textContent = kind === "start" ? "Opening…" : "Saving…";
  let refresh = kind !== "start";
  let skipped = null;   // after a Skip: whether it changed anything (then the plan may adapt)
  try {
    if (kind === "reschedule" && await plannerRescheduleThroughAdaptation(session)) {
      refresh = false;   // the adaptation already refreshed Home + week (or is waiting for the learner's review)
      return;
    }
    const url = `${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/${kind}`;
    const body = kind === "reschedule" || kind === "start" ? { utc_offset_minutes: plannerUtcOffsetMinutes() } : undefined;
    const result = await plannerRequest(url, { method: "POST", body });
    if (kind === "start") {
      Object.assign(session, result.session);
      plannerSessions.forEach((item) => { if (item.session_id === session.session_id) Object.assign(item, result.session); });
      if (!indexedDocuments.some((item) => item.id === session.document_id)) {
        showToast("This document is no longer available");
        return;
      }
      // A missing artifact is fine: the tool's own empty state offers to generate/create it.
      if (result.tool === "quiz") await openStudySession(session.document_id, "quiz", "", { quizTarget: await plannerQuizTarget(session) });
      else await openStudySession(session.document_id, result.tool);
    } else if (kind === "complete") showToast("Session completed. Nice work!");
    else if (kind === "skip") skipped = { changed: Boolean(result.changed) };
    else {
      const moved = result.session;
      showToast(`Moved to ${plannerDayLabel(moved.scheduled_start.slice(0, 10))}, ${moved.scheduled_start.slice(11, 16)}`
        + (result.after_deadline ? " (after the deadline)" : ""));
    }
  } catch (error) {
    showToast(error.message || "Could not update this session");
    refresh = refresh || error.status === 409 || error.status === 404;
  } finally {
    plannerBusySessions.delete(session.session_id);
    if (button.isConnected) {
      buttons.forEach((item) => { item.disabled = false; });
      button.textContent = kind === "start" && session.status === "in_progress" ? "Resume" : label;
    }
  }
  if (refresh) await plannerRefreshSchedules();
  if (skipped?.changed) {
    await plannerAdapt(session.plan_id, { kind: "session_skipped", session_id: session.session_id }, { prefix: "Session skipped" });
  } else if (skipped) showToast("Session skipped");
}

// ---- Adaptive replanning (Phase 5B2) ------------------------------------------
// The server recomputes every proposal from the trigger; the page never sends session changes.
// Small proposals are applied right away with a short note; large ones wait for the learner.

const plannerAdaptingPlans = new Set();   // plan ids with an adaptation request in flight
let plannerAdaptReview = null;   // desktop: a large proposal shown on the calendar {planId, trigger, proposal, busy, jump}
let plannerAdaptStale = null;    // desktop: Accept found a newer plan {planId, trigger}; a fresh check is offered

function plannerDocumentTitle(documentId) {
  return indexedDocuments.find((item) => item.id === documentId)?.title || documentId;
}

function plannerActivityWord(activity) {
  return (PLANNER_ACTIVITY_LABELS[activity] || activity).toLowerCase();
}

function plannerShortWhen(iso) {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return `${new Date(year, month - 1, day).toLocaleDateString(undefined, { weekday: "short" })} ${iso.slice(11, 16)}`;
}

function plannerLongWhen(iso) {
  return `${plannerDayLabel(iso.slice(0, 10))}, ${iso.slice(11, 16)}`;
}

function plannerAdaptationSummary(result) {
  const parts = [
    ...result.moved.map((item) => `${plannerActivityWord(item.activity_type)} moved to ${plannerShortWhen(item.to_start)}`),
    ...result.added.map((item) => `${plannerActivityWord(item.activity_type)} added ${plannerShortWhen(item.scheduled_start)}`),
    ...result.cancelled.map((item) => `${plannerActivityWord(item.activity_type)} no longer needed`),
  ];
  const shown = parts.slice(0, 3).join(", ") + (parts.length > 3 ? `, +${parts.length - 3} more` : "");
  return `Plan adjusted: ${shown}.`;
}

function plannerAdaptationReason(message) {
  // Engine messages restate the action ("Move the review session for "X" to <date>: <why>."); the
  // panel already shows what and when, so keep only the why.
  const why = (message || "").replace(/^(Move|Cancel) the .*? session for ".*?"( to \d{4}-\d{2}-\d{2} \d{2}:\d{2})?: /, "");
  return why.charAt(0).toUpperCase() + why.slice(1);
}

async function plannerAdapt(planId, trigger, { prefix = "", quietErrors = true } = {}) {
  if (!planId) return null;
  if (plannerAdaptingPlans.has(planId)) return null;   // one adaptation per plan at a time
  plannerAdaptingPlans.add(planId);
  let result = null;
  try {
    result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/apply`, {
      method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    if (!quietErrors) throw error;
    if (prefix) showToast(prefix);
    return null;
  } finally {
    plannerAdaptingPlans.delete(planId);
  }
  // A newer answer for this plan replaces any proposal still waiting on the calendar.
  if (result && plannerAdaptReview?.planId === planId) plannerAdaptReview = null;
  if (result && plannerAdaptStale?.planId === planId) plannerAdaptStale = null;
  if (result?.applied) {
    showToast(prefix ? `${prefix}. ${plannerAdaptationSummary(result)}` : plannerAdaptationSummary(result));
    await plannerRefreshSchedules();
  } else if (result?.requires_confirmation) {
    if (prefix) showToast(prefix);
    // Desktop reviews the proposal on the calendar itself; smaller screens keep the review panel.
    if (plannerIsDesktop()) plannerStartCalendarReview(planId, trigger, result, { prefix });
    else plannerShowAdaptationReview(planId, trigger, result);
  } else {
    if (prefix) showToast(prefix);
    if (result && plannerIsDesktop()) renderPlannerWorkspace();
  }
  return result;
}

async function plannerRescheduleThroughAdaptation(session) {
  // A session whose time has passed is a "missed" trigger: the planner finds its replacement and
  // shifts what depends on it. When it has nothing to change, the plain reschedule runs instead.
  if (!plannerIsOverdue(session)) return false;
  let result = null;
  try {
    result = await plannerAdapt(session.plan_id, { kind: "session_missed", session_id: session.session_id }, { quietErrors: false });
  } catch (error) {
    return false;
  }
  return Boolean(result && (result.applied || result.requires_confirmation));
}

async function plannerAdaptAfterQuiz(documentId) {
  // A completed quiz can re-shape that document's upcoming practice in any active plan holding it.
  try {
    const plans = (await plannerRequest(PLANNER_PLANS_API_URL)).filter((plan) => plan.status === "active");
    for (const plan of plans) {
      const detail = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(plan.plan_id)}`);
      if ((detail.materials || []).some((material) => material.document_id === documentId)) {
        await plannerAdapt(plan.plan_id, { kind: "quiz_completed", document_id: documentId });
      }
    }
  } catch (error) {
    // Adaptation is a bonus on top of the quiz result; the result itself is already saved.
  }
}

function plannerCloseAdaptationReview() {
  document.getElementById("adapt-review")?.remove();
  document.removeEventListener("keydown", plannerAdaptationReviewKeys);
}

function plannerAdaptationReviewKeys(event) {
  if (event.key === "Escape") document.getElementById("adapt-keep")?.click();
}

function plannerShowAdaptationReview(planId, trigger, proposal) {
  plannerCloseAdaptationReview();
  const backdrop = document.createElement("div");
  backdrop.className = "adapt-backdrop";
  backdrop.id = "adapt-review";
  const panel = document.createElement("section");
  panel.className = "adapt-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "adapt-title");
  const title = document.createElement("h2");
  title.id = "adapt-title";
  title.textContent = "Review plan changes";
  const intro = document.createElement("p");
  intro.className = "adapt-intro";
  intro.textContent = "Based on your recent study, we suggest a few changes. Nothing changes unless you accept.";
  panel.append(title, intro);

  const group = (label, items, describe) => {
    if (!items.length) return;
    const section = document.createElement("section");
    section.className = "adapt-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    const list = document.createElement("ul");
    items.forEach((item) => {
      const { when, reason } = describe(item);
      const row = document.createElement("li");
      row.className = "adapt-item";
      const name = document.createElement("strong");
      name.textContent = `${PLANNER_ACTIVITY_LABELS[item.activity_type] || item.activity_type} · ${plannerDocumentTitle(item.document_id)}`;
      const time = document.createElement("span");
      time.className = "adapt-when";
      time.textContent = when;
      const why = document.createElement("small");
      why.textContent = reason;
      row.append(name, time, why);
      list.appendChild(row);
    });
    section.append(heading, list);
    panel.appendChild(section);
  };
  group("New sessions", proposal.added, (item) => ({
    when: plannerLongWhen(item.scheduled_start), reason: plannerAdaptationReason(item.message) }));
  group("Moved", proposal.moved, (item) => ({
    when: `${plannerLongWhen(item.from_start)} → ${plannerLongWhen(item.to_start)}`, reason: plannerAdaptationReason(item.message) }));
  group("No longer needed", proposal.cancelled, (item) => ({
    when: `Removed from plan · was ${plannerLongWhen(item.scheduled_start)}`, reason: plannerAdaptationReason(item.message) }));
  if (proposal.warnings?.length) {
    const note = document.createElement("p");
    note.className = "adapt-note";
    note.textContent = "Some study time no longer fits before a deadline. Adding availability can help.";
    panel.appendChild(note);
  }
  const error = document.createElement("p");
  error.className = "adapt-error";
  error.setAttribute("role", "alert");
  error.hidden = true;
  const actions = document.createElement("div");
  actions.className = "adapt-actions";
  const keep = document.createElement("button");
  keep.type = "button";
  keep.id = "adapt-keep";
  keep.className = "secondary-button";
  keep.textContent = "Keep current plan";
  const accept = document.createElement("button");
  accept.type = "button";
  accept.id = "adapt-accept";
  accept.className = "primary-button";
  accept.textContent = "Accept changes";
  actions.append(keep, accept);
  panel.append(error, actions);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
  document.addEventListener("keydown", plannerAdaptationReviewKeys);

  let busy = false;
  keep.addEventListener("click", () => {
    if (busy) return;
    plannerCloseAdaptationReview();   // nothing is written
    showToast("Kept your current plan");
  });
  accept.addEventListener("click", async () => {
    if (busy) return;   // one apply per click burst
    busy = true;
    keep.disabled = accept.disabled = true;
    accept.textContent = "Applying…";
    error.hidden = true;
    try {
      const result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/apply`, {
        method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes(), confirm: true },
      });
      plannerCloseAdaptationReview();
      showToast(result.applied ? plannerAdaptationSummary(result) : "Your plan is already up to date");
      await plannerRefreshSchedules();
    } catch (failure) {
      error.textContent = failure.status === 409
        ? "Your plan changed in the meantime. Try again to use the latest version."
        : "Could not update your plan right now. Please try again.";
      error.hidden = false;
      busy = false;
      keep.disabled = accept.disabled = false;
      accept.textContent = "Accept changes";
    }
  });
  accept.focus();
}

// -- desktop: the calendar is the review surface --------------------------------
// A large proposal is drawn onto the week (new and moved-to sessions as ghosts; moving and
// no-longer-needed sessions muted in place). Nothing is written until Accept changes.

function plannerStartCalendarReview(planId, trigger, proposal, { prefix = "" } = {}) {
  plannerCloseAdaptationReview();
  plannerAdaptStale = null;
  plannerAdaptReview = { planId, trigger, proposal, busy: false, jump: true };
  const count = plannerReviewChanges().length;
  const text = `${count} schedule change${count === 1 ? "" : "s"} suggested`;
  if (state.page === "planner") {
    renderPlannerWorkspace();
    return;
  }
  // A session action elsewhere (Home) opens the calendar to show where things go; a finished quiz
  // leaves the learner where they are and the proposal waits in the Planner.
  if (trigger.kind === "quiz_completed") {
    showToast(`${text}. Review them in Study Planner.`);
    return;
  }
  setPage("planner");
  showToast(prefix ? `${prefix}. ${text}.` : `${text}.`);
}

function plannerReviewChanges(review = plannerAdaptReview) {
  // Every proposed change with the calendar dates it touches (a move touches two).
  if (!review) return [];
  const { added = [], moved = [], cancelled = [] } = review.proposal;
  return [
    ...added.map((item) => ({ type: "added", item, starts: [item.scheduled_start] })),
    ...moved.map((item) => ({ type: "moved", item, starts: [item.to_start, item.from_start] })),
    ...cancelled.map((item) => ({ type: "cancelled", item, starts: [item.scheduled_start] })),
  ];
}

function pcalReviewActive() {
  return Boolean(plannerAdaptReview && plannerPlan && plannerAdaptReview.planId === plannerPlan.plan_id);
}

function pcalDateOf(iso) {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function pcalReviewJump() {
  // First view of a proposal: when none of it is on the visible week, open the week of the first change.
  if (!pcalReviewActive() || !plannerAdaptReview.jump || state.page !== "planner") return;
  plannerAdaptReview.jump = false;
  const dates = pcalWeekDates();
  const fromKey = plannerDateKey(dates[0]), untilKey = plannerDateKey(dates[6]);
  const starts = plannerReviewChanges().flatMap((change) => change.starts);
  if (!starts.length || starts.some((iso) => iso.slice(0, 10) >= fromKey && iso.slice(0, 10) <= untilKey)) return;
  plannerCalWeekStart = plannerMondayOf(pcalDateOf(starts.sort()[0]));
}

function pcalReviewElsewhereText(fromKey, untilKey) {
  // "1 change next week": a pointer only -- the normal week controls get there.
  const visible = (iso) => iso.slice(0, 10) >= fromKey && iso.slice(0, 10) <= untilKey;
  const away = plannerReviewChanges().filter((change) => !change.starts.some(visible));
  if (!away.length) return "";
  const monday = plannerMondayOf(pcalDateOf(fromKey)).getTime();
  const offsets = new Set(away.map((change) =>
    Math.round((plannerMondayOf(pcalDateOf(change.starts[0])).getTime() - monday) / (7 * 86400000))));
  const count = `${away.length} change${away.length === 1 ? "" : "s"}`;
  if (offsets.size > 1) return `${count} in other weeks`;
  const [offset] = offsets;
  if (offset === 1) return `${count} next week`;
  if (offset === -1) return `${count} last week`;
  return offset > 0 ? `${count} in ${offset} weeks` : `${count} in an earlier week`;
}

function pcalRenderReviewBar(fromKey, untilKey) {
  const bar = pcal.review;
  if (!bar) return;
  const active = pcalReviewActive();
  bar.hidden = !active;
  bar.innerHTML = "";
  if (!active) return;
  const review = plannerAdaptReview;
  const count = plannerReviewChanges().length;
  const text = pcalEl("span", "pcal-review-text");
  text.appendChild(pcalEl("strong", "pcal-review-count", `${count} schedule change${count === 1 ? "" : "s"} suggested`));
  const elsewhere = pcalReviewElsewhereText(fromKey, untilKey);
  if (elsewhere) text.appendChild(pcalEl("span", "pcal-review-elsewhere", `· ${elsewhere}`));
  const actions = pcalEl("div", "pcal-review-actions");
  const keep = pcalEl("button", "pcal-button pcal-secondary", "Keep current plan");
  keep.type = "button";
  keep.id = "pcal-review-keep";
  const accept = pcalEl("button", "primary-button pcal-accept", review.busy ? "Applying…" : "Accept changes");
  accept.type = "button";
  accept.id = "pcal-review-accept";
  keep.disabled = accept.disabled = review.busy;
  keep.addEventListener("click", plannerKeepCalendarReview);
  accept.addEventListener("click", plannerAcceptCalendarReview);
  actions.append(keep, accept);
  bar.append(text, actions);
}

function plannerKeepCalendarReview() {
  if (!plannerAdaptReview || plannerAdaptReview.busy) return;
  plannerAdaptReview = null;   // nothing is written
  renderPlannerWorkspace();
  showToast("Kept your current plan");
}

async function plannerAcceptCalendarReview() {
  const review = plannerAdaptReview;
  if (!review || review.busy) return;   // one apply per click burst
  review.busy = true;
  pcalClosePopover();
  pcalRenderToolbar();
  try {
    const result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(review.planId)}/adaptation/apply`, {
      method: "POST", body: { trigger: review.trigger, utc_offset_minutes: plannerUtcOffsetMinutes(), confirm: true },
    });
    if (plannerAdaptReview === review) plannerAdaptReview = null;
    showToast(result.applied ? plannerAdaptationSummary(result) : "Your plan is already up to date");
    await plannerRefreshSchedules();
  } catch (failure) {
    if (plannerAdaptReview !== review) return;
    if (failure.status === 409) {
      // The plan changed since this proposal: drop it, show the saved schedule, offer a fresh look.
      plannerAdaptReview = null;
      plannerAdaptStale = { planId: review.planId, trigger: review.trigger };
      renderPlannerWorkspace();
      await plannerRefreshSchedules();
      return;
    }
    review.busy = false;
    pcalRenderToolbar();
    showToast("Could not update your plan right now. Please try again.");
  }
}

async function plannerRecheckAfterStale(button) {
  const stale = plannerAdaptStale;
  if (!stale) return;
  button.disabled = true;
  plannerAdaptStale = null;
  const result = await plannerAdapt(stale.planId, stale.trigger);
  if (result && !result.applied && !result.requires_confirmation) showToast("No changes needed. Your plan is up to date.");
  if (!result) pcalRenderNotice();
}

// Why a proposed change -- in the learner's words, built from the engine's own reason.
function plannerChangeWhy(type, item) {
  const message = item.message || "";
  if (type === "added") {
    const replaced = message.match(/^Replace the (missed|skipped) /);
    if (replaced) return `Replaces the ${replaced[1]} ${plannerActivityWord(item.activity_type)} session.`;
    return PCAL_REASON_TEXT[item.reason_code] || plannerAdaptationReason(message);
  }
  if (/ no longer fits/.test(message)) {
    return / before \d{4}-\d{2}-\d{2}\.$/.test(message)
      ? "It no longer fits in your available time before the deadline."
      : "It no longer fits in your available time.";
  }
  const why = (message.match(/^(?:Move|Cancel) the .*? session for ".*?"(?: to \d{4}-\d{2}-\d{2} \d{2}:\d{2})?: (.*?)(?:, it is no longer needed)?\.$/) || [])[1];
  let text = why || "";
  const score = text.match(/^latest quiz scored ([\d.]+)%$/);
  const deadline = text.match(/^the deadline is now (\d{4}-\d{2}-\d{2})$/);
  if (score) text = `Your latest quiz scored ${score[1]}%.`;
  else if (deadline) text = `The deadline is now ${plannerDayLabel(deadline[1])}.`;
  else if (text) text = `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
  else text = plannerAdaptationReason(message);
  if (type === "cancelled") return `${text.replace(/\.$/, "")} — this session is no longer needed.`;
  return text;
}

const PCAL_CHANGE_LABELS = { added: "New", moved: "Moved here", moving: "Moving", cancelled: "No longer needed" };
const PCAL_CHANGE_KIND_LABELS = {
  added: "Suggested new session", moved: "Suggested new time", moving: "Suggested move", cancelled: "Suggested removal",
};

function pcalDocumentTitle(documentId) {
  const saved = [...plannerSessionsById.values()].find((session) => session.document_id === documentId && session.document_title);
  return saved?.document_title || plannerDocumentTitle(documentId);
}

function pcalOpenChangePopover(change, session, block) {
  const { type, item } = change;
  const title = session.document_title || pcalDocumentTitle(session.document_id);
  pcalShowPopover(block, `${PCAL_CHANGE_KIND_LABELS[type]}: ${pcalActivity(session.activity_type)} · ${title}`, (popover) => {
    const content = pcalEl("div", "pcal-popover-body");
    content.append(pcalEl("span", `pcal-kind pcal-kind--change-${type}`, PCAL_CHANGE_KIND_LABELS[type]),
      pcalEl("h4", "pcal-popover-title", title),
      pcalEl("p", "pcal-popover-activity", `${pcalActivity(session.activity_type)} · ${plannerFormatDuration(session.duration_minutes)}`));
    const line = (caption, text) => {
      const row = pcalEl("p", "pcal-popover-when");
      row.append(pcalEl("span", "pcal-popover-caption", caption), document.createTextNode(text));
      content.appendChild(row);
    };
    const span = (start, end) => `${plannerDayLabel(start.slice(0, 10))} · ${start.slice(11, 16)}–${end.slice(11, 16)}`;
    if (type === "added") line("Proposed time", span(item.scheduled_start, item.scheduled_end));
    if (type === "moved" || type === "moving") {
      line("Proposed time", span(item.to_start, item.to_end));
      line("Currently", span(item.from_start, item.from_end));
    }
    if (type === "cancelled") line("Currently", span(session.scheduled_start, session.scheduled_end));
    const why = plannerChangeWhy(type === "moving" ? "moved" : type, item);
    if (why) {
      const reason = pcalEl("p", "pcal-popover-reason");
      reason.append(pcalEl("span", "pcal-popover-caption", "Why this change?"), document.createTextNode(` ${why}`));
      content.appendChild(reason);
    }
    content.appendChild(pcalEl("p", "pcal-popover-hint", "Nothing changes until you accept."));
    popover.appendChild(content);
  });
}

async function plannerRefreshSchedules() {
  // Home and the saved week both reflect the change; the week being viewed is kept.
  await Promise.all([loadTodayPlan(), plannerPlan ? loadPlannerData({ keepWeek: true }) : null]);
}

function renderPlannerSessionList(container, sessions, emptyText, options = {}) {
  container.innerHTML = "";
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  plannerGroupByDay(sessions).forEach(([dateKey, daySessions]) => {
    const day = document.createElement("section");
    day.className = "planner-day";
    const heading = document.createElement("h4");
    heading.textContent = plannerDayLabel(dateKey);
    const total = document.createElement("small");
    total.textContent = ` · ${plannerFormatDuration(daySessions.reduce((sum, item) => sum + item.duration_minutes, 0))}`;
    heading.appendChild(total);
    day.appendChild(heading);
    const list = document.createElement("ol");
    list.className = "planner-session-list";
    daySessions.forEach((session) => list.appendChild(plannerSessionItem(session, "li", options)));
    day.appendChild(list);
    container.appendChild(day);
  });
}

function plannerStat(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = plannerFormatDuration(value);
  wrapper.append(term, detail);
  return wrapper;
}

function plannerGoButton(label, step, className = "secondary-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", () => plannerSetStep(step));
  return button;
}

function renderPlannerPreview() {
  if (!plannerPreviewCapacity) return;
  plannerPreviewCapacity.innerHTML = "";
  const preview = plannerPreview;
  if (!preview) {
    plannerPreviewSessions.innerHTML = "";
    plannerConfirmButton.hidden = true;
    return;
  }
  const capacity = preview.capacity;
  const atRisk = capacity.status === "at_risk";
  const noAvailability = preview.warnings.some((warning) => warning.code === "no_availability");
  const box = document.createElement("div");
  box.className = `planner-capacity ${atRisk ? "at-risk" : "on-track"}`;
  const title = document.createElement("strong");
  title.textContent = atRisk ? "Not everything fits" : "Everything fits";
  const text = document.createElement("p");
  const count = preview.sessions.length;
  text.textContent = noAvailability
    ? "You haven’t selected any study time yet. Add some availability to get a plan."
    : atRisk
      ? "Your selected time can’t comfortably hold everything before the deadlines. Add study time, move a deadline, or confirm the part that fits."
      : `${plannerFormatDuration(capacity.scheduled_minutes)} of study in ${count} session${count === 1 ? "" : "s"}. The rest of your selected time stays free.`;
  box.append(title, text);
  if (atRisk) {
    const stats = document.createElement("dl");
    stats.className = "planner-capacity-stats";
    stats.append(
      plannerStat("Required", capacity.required_minutes),
      plannerStat("Schedulable", capacity.schedulable_minutes),
      plannerStat("Shortfall", capacity.shortfall_minutes),
    );
    const actions = document.createElement("div");
    actions.className = "planner-capacity-actions";
    actions.append(plannerGoButton("Add availability", "availability"), plannerGoButton("Adjust deadlines", "materials"));
    box.append(stats, actions);
  }
  plannerPreviewCapacity.appendChild(box);
  renderPlannerSessionList(plannerPreviewSessions, preview.sessions, "No sessions could be scheduled yet.");
  plannerConfirmButton.hidden = !count;
  plannerConfirmButton.disabled = plannerBusy;
  plannerConfirmButton.textContent = atRisk ? "Confirm partial plan" : "Looks good, confirm";
}

function plannerShowPreviewError(error) {
  plannerPreviewError.innerHTML = "";
  const message = document.createElement("p");
  message.textContent = error.message || "Could not build a preview";
  plannerPreviewError.appendChild(message);
  const documents = error.detail?.code === "deadline_passed" ? error.detail.documents || [] : [];
  if (documents.length) {
    const list = document.createElement("ul");
    documents.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = `${item.document_title || item.document_id}: ${item.deadline}`;
      list.appendChild(entry);
    });
    plannerPreviewError.append(list, plannerGoButton("Update deadlines", "materials", "text-button"));
  }
  plannerPreviewError.hidden = false;
}

async function plannerGeneratePreview() {
  if (plannerBusy || !plannerPlan) return;
  plannerBusy = true;
  plannerGeneratePreviewButton.disabled = true;
  plannerGeneratePreviewButton.textContent = "Generating…";
  plannerPreviewError.hidden = true;
  try {
    plannerPreview = await plannerRequest(plannerPlanUrl("/preview"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    plannerPreview = null;
    plannerShowPreviewError(error);
  } finally {
    plannerBusy = false;
    plannerGeneratePreviewButton.disabled = false;
    plannerGeneratePreviewButton.textContent = "Generate preview";
    plannerSetStep("preview");
  }
}

async function plannerConfirm() {
  if (plannerBusy || !plannerPlan || !plannerPreview?.sessions.length) return;
  plannerBusy = true;
  plannerConfirmButton.disabled = true;
  plannerConfirmButton.textContent = "Saving…";
  let alreadyConfirmed = false;
  try {
    // The server recomputes the schedule itself; only the learner's UTC offset is sent.
    const result = await plannerRequest(plannerPlanUrl("/confirm"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
    plannerSessions = result.sessions;
    plannerPlanWeekStart = null;
    plannerPreview = null;
    plannerStep = "plan";
    showToast("Study plan saved");
  } catch (error) {
    if (error.status === 409) alreadyConfirmed = true;
    else plannerShowPreviewError(error);
  } finally {
    plannerBusy = false;
    renderPlanner();
  }
  if (alreadyConfirmed) {
    showToast("This plan is already confirmed");
    await loadPlannerData();
  }
}

function renderPlannerPlan() {
  if (!plannerPlanSessions) return;
  const total = plannerSessions.reduce((sum, session) => sum + session.duration_minutes, 0);
  const days = new Set(plannerSessions.map((session) => session.scheduled_start.slice(0, 10))).size;
  plannerPlanSummary.textContent = plannerSessions.length
    ? `${plannerSessions.length} session${plannerSessions.length === 1 ? "" : "s"} · ${plannerFormatDuration(total)} across ${days} day${days === 1 ? "" : "s"}`
    : "No sessions saved yet.";
  if (!plannerPlanWeekStart) plannerPlanWeekStart = plannerDefaultPlanWeek(plannerSessions);
  const weekDates = plannerWeekDates(plannerPlanWeekStart);
  const fromKey = plannerDateKey(weekDates[0]);
  const untilKey = plannerDateKey(weekDates[6]);
  if (plannerPlanWeekLabel) {
    plannerPlanWeekLabel.textContent = `${plannerFormatDateLabel(weekDates[0])} – ${plannerFormatDateLabel(weekDates[6])}`;
  }
  const inWeek = (session) => {
    const key = session.scheduled_start.slice(0, 10);
    return key >= fromKey && key <= untilKey;
  };
  let emptyText = "No study sessions this week.";
  const next = plannerSessions.find((session) => session.scheduled_start.slice(0, 10) > untilKey);
  if (next) emptyText += ` Next session: ${plannerDayLabel(next.scheduled_start.slice(0, 10))}.`;
  const weekSessions = [...plannerSessions, ...plannerHistorySessions].filter(inWeek)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  const anySaved = plannerSessions.length || plannerHistorySessions.length;
  renderPlannerSessionList(plannerPlanSessions, weekSessions, anySaved ? emptyText : "No sessions saved yet.", { action: true });
}

async function loadPlannerPlanProgress(planId) {
  if (!plannerPlanProgress) return;
  try {
    const progress = await plannerRequest(plannerPlanUrl(`/progress?utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    if (plannerPlan?.plan_id !== planId) return;
    const performance = progress.current_quiz_performance;
    const quizText = performance.average_percentage == null
      ? "no quiz results yet"
      : `quiz performance ${Math.round(performance.average_percentage)}% across ${performance.assessed_documents} of ${performance.documents} document${performance.documents === 1 ? "" : "s"}`;
    plannerPlanProgress.textContent = `${progress.completed_sessions} of ${progress.planned_sessions} sessions done · `
      + `${plannerFormatDuration(progress.completed_minutes)} studied, ${plannerFormatDuration(progress.remaining_minutes)} still planned · ${quizText}`;
    plannerPlanProgress.hidden = false;
  } catch (error) {
    plannerPlanProgress.hidden = true;
  }
}

function plannerDefaultPlanWeek(sessions) {
  // This week if it holds any saved session; otherwise the week of the next upcoming one.
  const thisWeek = plannerMondayOf(plannerNow());
  const thisWeekKey = plannerDateKey(thisWeek);
  const upcoming = sessions.filter((session) => session.scheduled_start.slice(0, 10) >= thisWeekKey)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start))[0];
  return upcoming ? plannerMondayOf(new Date(upcoming.scheduled_start)) : thisWeek;
}

function plannerShiftPlanWeek(days) {
  const next = new Date(plannerPlanWeekStart || plannerMondayOf(plannerNow()));
  next.setDate(next.getDate() + days);
  plannerPlanWeekStart = next;
  renderPlannerPlan();
}

// ---- Desktop: calendar-first planning workspace (Phase 7A) --------------------
// One week calendar is the whole flow: add materials, drag availability, and the existing preview
// endpoint fills the week with suggested (ghost) sessions; Accept plan confirms them in place.
// Same plan state and APIs as the step flow above, which stays the small-screen experience.

const PCAL_HOUR_PX = 48;
const PCAL_MINUTE_PX = PCAL_HOUR_PX / 60;
const PCAL_DAY_MINUTES = 24 * 60;
const PCAL_PREVIEW_DELAY_MS = 450;
const PLANNER_LEARNING_STATE_LABELS = {
  new: "New", learning: "Learning", needs_review: "Needs review", on_track: "On track", completed: "Completed",
};
const PCAL_KIND_LABELS = {
  suggested: "Suggested", confirmed: "Planned", active: "In progress", overdue: "Not completed",
  completed: "Completed", skipped: "Skipped",
};
const plannerDesktopQuery = window.matchMedia ? window.matchMedia("(min-width: 1024px)") : null;

let plannerCalWeekStart = null;   // Monday of the week on the calendar; null = this week
let plannerPreviewing = false;    // a (debounced) preview is pending or in flight
let plannerPreviewFailure = null; // the last preview error (e.g. a deadline already passed)
let plannerPreviewTimer = null;
let plannerPreviewSeq = 0;        // only the newest preview response is shown
let plannerAvailabilityTimer = null;
let pcalDrag = null;
let pcalScrolledWeek = null;      // the week whose initial scroll position was already set
let plannerPlacements = [];       // draft moves before Accept: [{candidate_key, scheduled_start}]
let plannerLiveCandidates = [];   // a confirmed plan's still-wanted activities (server-computed)
let pcalMove = null;              // a session / queue item being dragged onto the calendar
let pcalMoveBusy = false;         // a drop request is in flight (one at a time)
let pcalSuppressClick = false;    // the click that ends a drag must not open a popover
const PCAL_SNAP_MINUTES = 15;

let plannerPlacementsPlanId = null;
let plannerSessionsById = new Map();   // every saved session of the plan (lineage for "Moved from")

async function plannerLoadLiveCandidates() {
  // What a confirmed plan still wants scheduled (draggable from the queue). Server-computed only.
  if (!plannerPlan || !plannerHasLivePlan()) {
    plannerLiveCandidates = [];
    return;
  }
  const planId = plannerPlan.plan_id;
  try {
    const found = await plannerRequest(plannerPlanUrl(`/candidates?utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    if (plannerPlan?.plan_id !== planId) return;
    plannerLiveCandidates = Array.isArray(found) ? found : [];
  } catch (error) {
    plannerLiveCandidates = [];
  }
  if (plannerIsDesktop() && !pcalMove) pcalRenderQueue();
}

function plannerIsDesktop() {
  return Boolean(plannerDesktopQuery?.matches);
}

function plannerHasLivePlan() {
  // A confirmed plan with current work: the calendar shows it; nothing is previewed.
  return plannerSessions.length > 0;
}

function plannerReadyToPreview() {
  return Boolean(plannerPlan && plannerMaterials.length && plannerAvailability.length && !plannerHasLivePlan());
}

function pcalWeekDates() {
  if (!plannerCalWeekStart) plannerCalWeekStart = plannerMondayOf(plannerNow());
  return plannerWeekDates(plannerCalWeekStart);
}

function pcalEl(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function pcalSessionTimes(session) {
  return `${session.scheduled_start.slice(11, 16)}–${session.scheduled_end.slice(11, 16)}`;
}

function pcalShortDay(dateKey) {
  const [year, month, day] = dateKey.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return `${date.toLocaleDateString(undefined, { weekday: "short" })} ${day}`;
}

function pcalActivity(activity) {
  return PLANNER_ACTIVITY_LABELS[activity] || activity;
}

function pcalMaterialTitle(material) {
  return material.document_title || plannerDocumentTitle(material.document_id);
}

// -- state changes that re-plan ------------------------------------------------

function plannerQueueAutoPreview(delay = PCAL_PREVIEW_DELAY_MS) {
  // Materials, deadlines and availability all feed the preview: any change re-runs it (debounced).
  clearTimeout(plannerPreviewTimer);
  if (!plannerReadyToPreview()) {
    plannerPreviewSeq += 1;   // drop any response still in flight
    plannerPreviewing = false;
    if (!plannerHasLivePlan()) plannerPreview = null;
    plannerPreviewFailure = null;
    if (plannerIsDesktop()) renderPlannerWorkspace();
    return;
  }
  plannerPreviewing = true;
  if (plannerIsDesktop()) renderPlannerWorkspace();
  plannerPreviewTimer = setTimeout(plannerRunPreview, delay);
}

async function plannerRunPreview() {
  if (!plannerReadyToPreview()) {
    plannerQueueAutoPreview();
    return;
  }
  const seq = ++plannerPreviewSeq;
  const planId = plannerPlan.plan_id;
  plannerPreviewing = true;
  pcalRenderToolbar();
  try {
    const result = await plannerRequest(plannerPlanUrl("/preview"), {
      method: "POST", body: plannerScheduleBody(),
    });
    if (seq !== plannerPreviewSeq || plannerPlan?.plan_id !== planId) return;
    plannerPreview = result;
    plannerPreviewFailure = null;
    plannerDropRejectedPlacements(result.placements?.rejected);
  } catch (error) {
    if (seq !== plannerPreviewSeq) return;
    plannerPreview = null;
    plannerPreviewFailure = error;
  }
  plannerPreviewing = false;
  if (plannerIsDesktop()) renderPlannerWorkspace();
}

function plannerScheduleBody() {
  // Only the learner's offset, plus their draft moves; the server recomputes and validates everything.
  const body = { utc_offset_minutes: plannerUtcOffsetMinutes() };
  if (plannerPlacements.length) body.placements = plannerPlacements.map((item) => ({ ...item }));
  return body;
}

function plannerDropRejectedPlacements(rejected) {
  if (!rejected?.length) return;
  const keys = new Set(rejected.map((item) => item.candidate_key));
  plannerPlacements = plannerPlacements.filter((item) => !keys.has(item.candidate_key));
  showToast(rejected[0].message || "That time is not available");
}

function plannerAfterAvailabilityChange() {
  if (!plannerHasLivePlan()) {
    plannerQueueAutoPreview();
    return;
  }
  // A confirmed plan: sessions that no longer fit move (existing adaptation API), once the edits settle.
  renderPlannerWorkspace();
  clearTimeout(plannerAvailabilityTimer);
  const planId = plannerPlan?.plan_id;
  plannerAvailabilityTimer = setTimeout(() => plannerAdapt(planId, { kind: "availability_changed" }), 800);
}

async function pcalAddMaterial(documentId, deadline, button) {
  if (button) button.disabled = true;
  try {
    await plannerEnsurePlan();
    const body = { document_id: documentId };
    if (deadline) body.deadline = deadline;
    const material = await plannerRequest(plannerPlanUrl("/materials"), { method: "POST", body });
    plannerMaterials = [...plannerMaterials, material];
    if (plannerHasLivePlan() && material.deadline) {
      await plannerAdapt(plannerPlan.plan_id, { kind: "deadline_changed", document_id: documentId });
    }
  } catch (error) {
    showToast(error.message || "Could not add this material");
  }
  plannerQueueAutoPreview();
}

async function pcalRemoveMaterial(material, button) {
  button.disabled = true;
  try {
    await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), { method: "DELETE" });
    plannerMaterials = plannerMaterials.filter((item) => item.material_id !== material.material_id);
  } catch (error) {
    showToast(error.message || "Could not remove this material");
    button.disabled = false;
    return;
  }
  if (plannerHasLivePlan()) await loadPlannerData({ keepWeek: true });
  else plannerQueueAutoPreview();
}

async function pcalSetDeadline(material, input) {
  try {
    const updated = await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), {
      method: "PATCH", body: { deadline: input.value || null },
    });
    plannerMaterials = plannerMaterials.map((item) => (item.material_id === updated.material_id ? { ...item, ...updated } : item));
  } catch (error) {
    input.value = material.deadline || "";
    showToast(error.message || "Could not save the deadline");
    return;
  }
  if (plannerHasLivePlan()) {
    renderPlannerWorkspace();
    await plannerAdapt(plannerPlan.plan_id, { kind: "deadline_changed", document_id: material.document_id });
  } else plannerQueueAutoPreview();
}

async function pcalChangeAvailability(action, payload) {
  const url = action === "remove" ? `${PLANNER_AVAILABILITY_API_URL}/remove` : PLANNER_AVAILABILITY_API_URL;
  // The learner's offset lets the server refuse a dated slot in the past (it never guesses the timezone).
  const body = action === "remove" ? payload : { ...payload, utc_offset_minutes: plannerUtcOffsetMinutes() };
  plannerAvailability = await plannerRequest(url, { method: "POST", body });
}

function pcalSlotPayload(slot, dateKey) {
  return slot.is_recurring
    ? { start_at: slot.start_at, end_at: slot.end_at, is_recurring: true, day_of_week: slot.day_of_week }
    : { start_at: slot.start_at, end_at: slot.end_at, is_recurring: false, date: slot.date || dateKey };
}

async function plannerAcceptPlan() {
  if (plannerBusy || plannerPreviewing || !plannerPlan || !plannerPreview?.sessions.length || plannerHasLivePlan()) return;
  plannerBusy = true;
  clearTimeout(plannerPreviewTimer);
  pcalRenderToolbar();
  let alreadyConfirmed = false;
  try {
    // One atomic confirm; the server recomputes the same deterministic schedule from its own data.
    const result = await plannerRequest(plannerPlanUrl("/confirm"), {
      method: "POST", body: plannerScheduleBody(),
    });
    const planId = plannerPlan.plan_id;
    plannerSessions = result.sessions.map((session) => ({ ...session, plan_id: planId })).filter(plannerIsActiveSession);
    plannerPreview = null;
    plannerPreviewFailure = null;
    plannerPlacements = [];
    plannerLoadLiveCandidates();
    plannerStep = "plan";
    showToast("Study plan saved");
    loadPlannerPlanProgress(planId);
    loadTodayPlan();
  } catch (error) {
    if (error.detail?.code === "placement_rejected") {
      // A moved suggestion no longer fits: nothing was saved; show the refreshed suggestion instead.
      plannerBusy = false;
      plannerDropRejectedPlacements(error.detail.placements);
      plannerQueueAutoPreview(0);
      return;
    }
    if (error.status === 409) alreadyConfirmed = true;
    else plannerPreviewFailure = error;
  } finally {
    plannerBusy = false;
    renderPlannerWorkspace();
  }
  if (alreadyConfirmed) {
    showToast("This plan is already confirmed");
    await loadPlannerData({ keepWeek: true });
  }
}

async function pcalStartNewPlan() {
  if (!plannerPlan) return;
  try {
    // Archive the confirmed plan (its saved sessions stay in its history) and start fresh.
    await plannerRequest(plannerPlanUrl(), { method: "PATCH", body: { status: "archived" } });
    plannerPlan = null;
    plannerMaterials = [];
    plannerSessions = [];
    plannerHistorySessions = [];
    plannerPreview = null;
    plannerPlacements = [];
    plannerLiveCandidates = [];
    plannerStep = "materials";
    plannerQueueAutoPreview();
    loadTodayPlan();
  } catch (error) {
    showToast(error.message || "Could not start a new plan");
  }
}

// -- rendering -----------------------------------------------------------------

function renderPlannerWorkspace() {
  if (!plannerWorkspace || plannerWorkspace.hidden) return;
  pcalReviewJump();
  pcalClosePopover();
  pcalRenderMaterials();
  pcalRenderToolbar();
  pcalRenderGrid();
  pcalRenderQueue();
  pcalRenderSheet();
}

function pcalRenderMaterials() {
  pcal.materials.innerHTML = "";
  const today = plannerDateKey(plannerNow());
  plannerMaterials.forEach((material) => {
    const title = pcalMaterialTitle(material);
    const item = pcalEl("li", "pcal-material");
    item.dataset.documentId = material.document_id;
    const top = pcalEl("div", "pcal-material-top");
    const name = pcalEl("strong", "pcal-material-title", title);
    name.title = title;
    const remove = pcalEl("button", "pcal-material-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${title}`);
    remove.addEventListener("click", () => pcalRemoveMaterial(material, remove));
    top.append(name, remove);
    const meta = pcalEl("div", "pcal-material-meta");
    const stateKey = pcalLearningKey(material.document_id, material.learning_state || "new");
    meta.appendChild(pcalEl("span", `pcal-pill pcal-pill--${stateKey}`, PLANNER_LEARNING_STATE_LABELS[stateKey] || stateKey));
    const { control: deadline, input } = pcalDeadlineControl(material.deadline || "", `Deadline for ${title}`, today);
    input.addEventListener("change", () => pcalSetDeadline(material, input));
    meta.appendChild(deadline);
    item.append(top, meta);
    const pack = pcalStudyPackText(material.document_id, { compact: true });
    if (pack) item.appendChild(pcalEl("span", "pcal-material-pack", pack));
    pcal.materials.appendChild(item);
  });
  pcal.newPlan.hidden = !(plannerPlan && (plannerSessions.length || plannerHistorySessions.length));
}

// Each document's real study state -- learning state, Study Pack contents, latest quiz -- from
// /api/progress/documents (DocumentStudyState). Nothing here is inferred on the page.
let plannerDocStates = new Map();   // document id -> progress payload (null while loading / unavailable)
let plannerDocStatesLoad = 0;

async function plannerLoadDocStates() {
  const load = ++plannerDocStatesLoad;
  plannerDocStates = new Map();
  const ids = indexedDocuments.map((doc) => doc.id);
  await Promise.all(ids.map(async (id) => {
    try {
      const state = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(id)}?utc_offset_minutes=${plannerUtcOffsetMinutes()}`);
      if (load === plannerDocStatesLoad && state?.learning) plannerDocStates.set(id, state);
    } catch (error) {
      // no state for this document: its details are simply not shown
    }
  }));
  if (load !== plannerDocStatesLoad || !plannerIsDesktop()) return;
  pcalRenderMaterials();
  pcalRenderSheet();
}

function pcalLearningKey(documentId, fallback = "new") {
  return plannerDocStates.get(documentId)?.learning.state || fallback;
}

function pcalStudyPackText(documentId, { compact = false } = {}) {
  // What the Study Pack actually holds; missing parts are said plainly, never assumed.
  const pack = plannerDocStates.get(documentId)?.study_pack;
  if (!pack) return "";
  const cards = pack.flashcard_count;
  if (compact) {
    const parts = [pack.summary_ready && "Summary", cards && `${cards} cards`, pack.quiz_count && "Quiz"].filter(Boolean);
    return parts.length ? parts.join(" · ") : "No study pack yet";
  }
  return [pack.summary_ready ? "Summary ready" : "No summary yet", cards ? `${cards} flashcard${cards === 1 ? "" : "s"}` : "No flashcards yet",
    pack.quiz_count ? "Quiz ready" : "No quiz yet"].join(" · ");
}

function pcalQuizText(documentId) {
  const quiz = plannerDocStates.get(documentId)?.quiz;
  if (!quiz) return "";
  return quiz.latest ? `Latest quiz ${Math.round(quiz.latest.percentage)}%` : "No quiz attempt yet";
}

function pcalDeadlineControl(value, label, today) {
  // A quiet text button ("Add deadline" / "Due Oct 2") over the native date picker.
  const control = pcalEl("span", "pcal-deadline");
  const button = pcalEl("button", "pcal-deadline-button");
  button.type = "button";
  const input = document.createElement("input");
  input.type = "date";
  input.value = value;
  input.className = "pcal-deadline-input";
  input.setAttribute("aria-label", label);
  const refresh = () => {
    const set = Boolean(input.value);
    button.textContent = set
      ? `Due ${new Date(`${input.value}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
      : "Add deadline";
    control.classList.toggle("is-set", set);
    control.classList.toggle("is-past", set && input.value < today);
  };
  button.setAttribute("aria-label", label);
  button.addEventListener("click", () => {
    try {
      input.showPicker();
    } catch (error) {
      input.focus();
    }
  });
  refresh();
  control.append(button, input);
  return { control, input, refresh };
}

function pcalWeekItems(fromKey, untilKey) {
  const inWeek = (session) => {
    const key = session.scheduled_start.slice(0, 10);
    return key >= fromKey && key <= untilKey;
  };
  const items = plannerHistorySessions.filter(inWeek).map((session) => ({ kind: session.status, session }));
  if (plannerHasLivePlan()) {
    const review = pcalReviewActive() ? plannerAdaptReview.proposal : null;
    plannerSessions.filter(inWeek).forEach((session) => {
      const kind = session.status === "in_progress" ? "active" : plannerIsOverdue(session) ? "overdue" : "confirmed";
      // Under review, a session the proposal moves or drops stays where it is, marked.
      const moving = review?.moved.find((item) => item.session_id === session.session_id);
      const dropping = review?.cancelled.find((item) => item.session_id === session.session_id);
      const change = moving ? { type: "moving", item: moving } : dropping ? { type: "cancelled", item: dropping } : null;
      items.push({ kind, session, change });
    });
    if (review) {
      const ghost = (item, start, end, minutes) => ({ document_id: item.document_id, document_title: pcalDocumentTitle(item.document_id),
        activity_type: item.activity_type, scheduled_start: start, scheduled_end: end, duration_minutes: minutes, status: "proposed" });
      review.added.forEach((item) => {
        const session = ghost(item, item.scheduled_start, item.scheduled_end, item.duration_minutes);
        if (inWeek(session)) items.push({ kind: "proposed", session, change: { type: "added", item } });
      });
      review.moved.forEach((item) => {
        const minutes = plannerSessionsById.get(item.session_id)?.duration_minutes
          ?? plannerToMinutes(item.to_end.slice(11, 16)) - plannerToMinutes(item.to_start.slice(11, 16));
        const session = ghost(item, item.to_start, item.to_end, minutes);
        if (inWeek(session)) items.push({ kind: "proposed", session, change: { type: "moved", item } });
      });
    }
  } else if (plannerPreview) {
    plannerPreview.sessions.filter(inWeek).forEach((session) => items.push({ kind: "suggested", session }));
  }
  return items;
}

function pcalRenderToolbar() {
  if (!pcal.range) return;
  const dates = pcalWeekDates();
  const first = dates[0], last = dates[6];
  const sameMonth = first.getMonth() === last.getMonth();
  const startLabel = first.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const endLabel = last.toLocaleDateString(undefined, sameMonth ? { day: "numeric" } : { month: "short", day: "numeric" });
  pcal.range.textContent = `${startLabel} – ${endLabel}, ${last.getFullYear()}`;

  const live = plannerHasLivePlan();
  let status = "";
  let tone = "";
  if (live) {
    const week = pcalWeekItems(plannerDateKey(first), plannerDateKey(last))
      .filter((item) => ["confirmed", "active", "overdue"].includes(item.kind));
    const minutes = week.reduce((sum, item) => sum + item.session.duration_minutes, 0);
    status = week.length
      ? `${week.length} planned session${week.length === 1 ? "" : "s"} this week · ${plannerFormatDuration(minutes)}`
      : "No sessions this week";
  } else if (plannerPreviewing && plannerReadyToPreview()) {
    status = "Updating suggestions…";
    tone = "busy";
  } else if (plannerPreview) {
    const sessions = plannerPreview.sessions;
    const minutes = sessions.reduce((sum, session) => sum + session.duration_minutes, 0);
    status = `${sessions.length} suggested session${sessions.length === 1 ? "" : "s"} · ${plannerFormatDuration(minutes)}`;
    if (plannerPreview.capacity.status === "at_risk") {
      status += ` · ${plannerFormatDuration(plannerPreview.capacity.shortfall_minutes)} doesn’t fit`;
      tone = "warn";
    }
  }
  pcal.status.textContent = status;
  pcal.status.dataset.tone = tone;
  pcal.explain.hidden = !(live || plannerPreview?.sessions.length);
  pcal.autoPlan.hidden = live || !plannerMaterials.length;
  pcal.autoPlan.disabled = plannerBusy || !plannerReadyToPreview();
  pcal.accept.hidden = live || !plannerPreview?.sessions.length;
  pcal.accept.disabled = plannerBusy || plannerPreviewing;
  pcal.accept.textContent = plannerBusy ? "Saving…" : "Accept plan";
  pcalRenderReviewBar(plannerDateKey(first), plannerDateKey(last));
  pcal.status.parentElement.hidden = pcalReviewActive();   // the review bar takes the status line's place
  pcalRenderNotice();
}

function pcalRenderNotice() {
  // Progressive disclosure: only the next thing the learner needs, in one short line.
  const notice = pcal.notice;
  notice.innerHTML = "";
  notice.dataset.tone = "";
  let text = "";
  if (plannerAdaptStale && plannerAdaptStale.planId === plannerPlan?.plan_id) {
    text = "Your plan changed. Review the latest schedule.";
    const action = pcalEl("button", "pcal-notice-action", "Check for new suggestions");
    action.type = "button";
    action.id = "pcal-review-recheck";
    action.addEventListener("click", () => plannerRecheckAfterStale(action));
    notice.append(pcalEl("span", "", text), action);
  } else if (plannerHasLivePlan()) text = "";
  else if (!plannerMaterials.length) {
    text = "Add the materials you want to study.";
    const action = pcalEl("button", "pcal-notice-action", "Add materials");
    action.type = "button";
    action.addEventListener("click", pcalOpenSheet);
    notice.append(pcalEl("span", "", text), action);
  } else if (!plannerAvailability.length) {
    text = "Drag on the calendar to mark when you could study.";
    notice.appendChild(pcalEl("span", "", text));
  } else if (plannerPreviewFailure && !plannerPreviewing) {
    const failure = plannerPreviewFailure;
    text = failure.message || "Could not suggest a plan";
    notice.dataset.tone = "warn";
    notice.appendChild(pcalEl("span", "", text));
    const documents = failure.detail?.code === "deadline_passed" ? failure.detail.documents || [] : [];
    documents.forEach((item) => notice.appendChild(pcalEl("span", "pcal-notice-chip", `${item.document_title || item.document_id} · ${item.deadline}`)));
  } else if (plannerPreview && !plannerPreviewing && !plannerPreview.sessions.length) {
    text = "Nothing fits in your available time yet. Drag to add more.";
    notice.dataset.tone = "warn";
    notice.appendChild(pcalEl("span", "", text));
  }
  notice.hidden = !text;
}

function pcalNowMinutes() {
  const now = plannerNow();
  return now.getHours() * 60 + now.getMinutes();
}

function pcalFirstOpenMinute(dateKey, step = PLANNER_CELL_MINUTES) {
  // Past days are read-only; today opens at the next grid step after "now"; later days are open.
  const todayKey = plannerDateKey(plannerNow());
  if (dateKey < todayKey) return Infinity;
  if (dateKey > todayKey) return 0;
  return Math.ceil(pcalNowMinutes() / step) * step;
}

function pcalSlotIsPast(slot, dateKey) {
  const todayKey = plannerDateKey(plannerNow());
  return dateKey < todayKey || (dateKey === todayKey && plannerToMinutes(slot.end_at) <= pcalNowMinutes());
}

function pcalAvailabilityFor(dateKey, weekday) {
  return plannerAvailability.filter((slot) => plannerAvailabilityCoversDate(slot, dateKey, weekday));
}

function pcalRenderGrid() {
  const dates = pcalWeekDates();
  const now = plannerNow();
  const todayKey = plannerDateKey(now);
  const fromKey = plannerDateKey(dates[0]);
  const untilKey = plannerDateKey(dates[6]);
  const items = pcalWeekItems(fromKey, untilKey);

  // Sticky header: weekday + date, then the all-day row that carries deadline markers.
  pcal.head.innerHTML = "";
  pcal.head.appendChild(pcalEl("div", "pcal-corner"));
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const head = pcalEl("div", "pcal-dayhead");
    head.classList.toggle("is-today", key === todayKey);
    head.classList.toggle("is-past", key < todayKey);
    head.append(pcalEl("span", "pcal-dow", PLANNER_DAY_LABELS[(date.getDay() + 6) % 7]), pcalEl("span", "pcal-daynum", String(date.getDate())));
    pcal.head.appendChild(head);
  });
  pcal.head.appendChild(pcalEl("div", "pcal-allday-label"));
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const cell = pcalEl("div", "pcal-allday");
    cell.dataset.date = key;
    plannerMaterials.filter((material) => material.deadline === key).forEach((material) => {
      const marker = pcalEl("span", "pcal-deadline-marker", pcalMaterialTitle(material));
      marker.classList.toggle("is-past", key < todayKey);
      marker.title = `${pcalMaterialTitle(material)} due ${plannerDayLabel(key)}`;
      marker.setAttribute("aria-label", marker.title);
      cell.appendChild(marker);
    });
    pcal.head.appendChild(cell);
  });

  // Body: hour gutter + seven day columns; availability, sessions and "now" are positioned layers.
  pcal.body.innerHTML = "";
  const gutter = pcalEl("div", "pcal-gutter");
  for (let hour = 1; hour < 24; hour += 1) {
    const label = pcalEl("span", "pcal-hour", plannerMinutesToLabel(hour * 60));
    label.style.top = `${hour * PCAL_HOUR_PX}px`;
    gutter.appendChild(label);
  }
  pcal.body.appendChild(gutter);
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const weekday = (date.getDay() + 6) % 7;
    const column = pcalEl("div", "pcal-col");
    column.dataset.date = key;
    column.dataset.weekday = String(weekday);
    column.classList.toggle("is-today", key === todayKey);
    column.classList.toggle("is-past", key < todayKey);
    column.setAttribute("aria-label", plannerDayLabel(key));
    const dayItems = items.filter((item) => item.session.scheduled_start.slice(0, 10) === key);
    const busy = dayItems.map((item) => [plannerToMinutes(item.session.scheduled_start.slice(11, 16)),
      plannerToMinutes(item.session.scheduled_end.slice(11, 16))]);
    pcalAvailabilityFor(key, weekday).forEach((slot) => column.appendChild(pcalAvailabilityBlock(slot, key, busy)));
    dayItems.forEach((item) => column.appendChild(pcalEventBlock(item)));
    if (key === todayKey) {
      const shade = pcalEl("div", "pcal-past-shade");   // the part of today that has passed
      shade.style.height = `${(now.getHours() * 60 + now.getMinutes()) * PCAL_MINUTE_PX}px`;
      shade.setAttribute("aria-hidden", "true");
      column.appendChild(shade);
      const line = pcalEl("div", "pcal-now");
      line.style.top = `${(now.getHours() * 60 + now.getMinutes()) * PCAL_MINUTE_PX}px`;
      line.setAttribute("aria-hidden", "true");
      column.appendChild(line);
    }
    column.addEventListener("pointerdown", (event) => pcalStartDrag(event, column, null));
    pcal.body.appendChild(column);
  });

  // First view of a week: scroll to the morning, or earlier if something starts earlier.
  if (pcalScrolledWeek !== fromKey) {
    pcalScrolledWeek = fromKey;
    const starts = [
      ...items.map((item) => plannerToMinutes(item.session.scheduled_start.slice(11, 16))),
      ...dates.flatMap((date) => pcalAvailabilityFor(plannerDateKey(date), (date.getDay() + 6) % 7).map((slot) => plannerToMinutes(slot.start_at))),
    ];
    const first = Math.min(8 * 60, ...starts);
    pcal.scroll.scrollTop = Math.max(0, (first - 60) * PCAL_MINUTE_PX);
  }
}

function pcalPlace(element, startMinute, endMinute) {
  element.style.top = `${startMinute * PCAL_MINUTE_PX}px`;
  element.style.height = `${Math.max(12, (endMinute - startMinute) * PCAL_MINUTE_PX - 2)}px`;
}

function pcalAvailabilityBlock(slot, dateKey, busy = []) {
  const start = plannerToMinutes(slot.start_at), end = plannerToMinutes(slot.end_at);
  const block = pcalEl("div", "pcal-avail");
  block.dataset.start = slot.start_at;
  block.dataset.end = slot.end_at;
  block.dataset.recurring = String(Boolean(slot.is_recurring));
  block.tabIndex = 0;
  block.setAttribute("role", "button");
  block.setAttribute("aria-label", `Available ${slot.start_at}–${slot.end_at}${slot.is_recurring ? ", every week" : ""}`);
  pcalPlace(block, start, end);
  block.classList.toggle("is-short", end - start < 60);
  block.classList.toggle("is-past", pcalSlotIsPast(slot, dateKey));
  // The label goes where no session covers it: the bottom of the window, else the top, else none.
  const free = (from, to) => !busy.some(([s, e]) => s < to && e > from);
  const room = 30;
  block.classList.add(free(end - room, end) ? "label-bottom" : free(start, start + room) ? "label-top" : "label-none");
  block.append(pcalEl("span", "pcal-avail-label", "Available"),
    pcalEl("span", "pcal-avail-time", `${slot.start_at}–${slot.end_at} · ${plannerFormatDuration(end - start)}`));
  block.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
    pcalStartDrag(event, block.parentElement, { slot, dateKey, block });
  });
  block.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pcalOpenAvailabilityPopover(slot, dateKey, block);
    }
  });
  return block;
}

function pcalEventBlock({ kind, session, change = null }) {
  const start = plannerToMinutes(session.scheduled_start.slice(11, 16));
  const end = plannerToMinutes(session.scheduled_end.slice(11, 16));
  const block = pcalEl("button", `pcal-event pcal-event--${kind} pcal-activity--${session.activity_type}`);
  block.type = "button";
  block.dataset.kind = kind;
  block.dataset.start = session.scheduled_start;
  if (session.session_id) block.dataset.sessionId = session.session_id;
  const title = session.document_title || plannerDocumentTitle(session.document_id);
  const label = change ? PCAL_CHANGE_KIND_LABELS[change.type] : PCAL_KIND_LABELS[kind];
  block.setAttribute("aria-label", `${label}: ${pcalActivity(session.activity_type)} · ${title}, ${pcalShortDay(session.scheduled_start.slice(0, 10))} ${pcalSessionTimes(session)}`);
  pcalPlace(block, start, end);
  block.classList.toggle("is-compact", end - start < 40);
  if (change) {
    // A proposed change: tagged, reviewable, not draggable until the learner decides.
    block.classList.add("pcal-change", `pcal-change--${change.type}`);
    block.dataset.change = change.type;
    block.title = PCAL_CHANGE_LABELS[change.type];
    // Short blocks keep the title readable: a glyph for ghosts; muted / struck styling says the rest.
    const tag = end - start < 40 ? { added: "+", moved: "→" }[change.type]
      : change.type === "added" ? `+ ${PCAL_CHANGE_LABELS.added}` : PCAL_CHANGE_LABELS[change.type];
    if (tag) block.appendChild(pcalEl("span", "pcal-event-tag", tag));
  }
  block.append(pcalEl("strong", "pcal-event-title", title),
    pcalEl("span", "pcal-event-meta", `${pcalActivity(session.activity_type)} · ${pcalSessionTimes(session)}`));
  const source = change ? null : pcalMoveSourceFor(kind, session, block);
  block.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
    if (source) pcalBeginMove(event, source);
  });
  block.addEventListener("click", () => {
    if (pcalSuppressClick) {
      pcalSuppressClick = false;
      return;
    }
    if (change) pcalOpenChangePopover(change, session, block);
    else pcalOpenSessionPopover(kind, session, block);
  });
  if (source) block.classList.add("is-draggable");
  return block;
}

// -- drag to mark availability --------------------------------------------------

function pcalMinuteAt(column, clientY) {
  const box = column.getBoundingClientRect();
  const minute = Math.floor((clientY - box.top) / PCAL_MINUTE_PX / PLANNER_CELL_MINUTES) * PLANNER_CELL_MINUTES;
  return Math.min(PCAL_DAY_MINUTES - PLANNER_CELL_MINUTES, Math.max(0, minute));
}

function pcalStartDrag(event, column, from) {
  if (event.button !== undefined && event.button > 0) return;
  event.preventDefault();
  pcalClosePopover();
  const anchor = pcalMinuteAt(column, event.clientY);
  const firstOpen = pcalFirstOpenMinute(column.dataset.date);
  if (anchor < firstOpen) {
    // Past time is read-only: a click on past availability still shows its details.
    if (from) pcalOpenAvailabilityPopover(from.slot, from.dateKey, from.block);
    return;
  }
  const ghost = pcalEl("div", "pcal-drag");
  column.appendChild(ghost);
  pcalDrag = { column, anchor, start: anchor, end: anchor + PLANNER_CELL_MINUTES, moved: false, from, ghost, firstOpen };
  pcalUpdateDragGhost();
}

function pcalUpdateDragGhost() {
  const drag = pcalDrag;
  pcalPlace(drag.ghost, drag.start, drag.end);
  drag.ghost.textContent = `${plannerMinutesToLabel(drag.start)}–${plannerMinutesToLabel(drag.end)}`;
  drag.ghost.hidden = Boolean(drag.from) && !drag.moved;
}

function pcalMoveDrag(event) {
  if (!pcalDrag) return;
  const minute = Math.max(pcalDrag.firstOpen, pcalMinuteAt(pcalDrag.column, event.clientY));
  if (minute !== pcalDrag.anchor) pcalDrag.moved = true;
  pcalDrag.start = Math.min(pcalDrag.anchor, minute);
  pcalDrag.end = Math.max(pcalDrag.anchor, minute) + PLANNER_CELL_MINUTES;
  pcalUpdateDragGhost();
}

async function pcalFinishDrag() {
  const drag = pcalDrag;
  pcalDrag = null;
  if (!drag) return;
  if (drag.from && !drag.moved) {   // a click on an availability block: its details
    drag.ghost.remove();
    pcalOpenAvailabilityPopover(drag.from.slot, drag.from.dateKey, drag.from.block);
    return;
  }
  const weekday = Number(drag.column.dataset.weekday);
  // New availability repeats weekly by default; the block's popover can limit it to this date.
  const payload = { start_at: plannerMinutesToLabel(drag.start), end_at: plannerMinutesToLabel(drag.end), is_recurring: true, day_of_week: weekday };
  try {
    await pcalChangeAvailability("add", payload);
  } catch (error) {
    showToast(error.message || "Could not save availability");
  }
  drag.ghost.remove();
  plannerAfterAvailabilityChange();
}

// -- popovers ---------------------------------------------------------------------

function pcalClosePopover() {
  if (!pcal.popover || pcal.popover.hidden) return;
  pcal.popover.hidden = true;
  pcal.popover.innerHTML = "";
  plannerWorkspace.querySelectorAll(".is-selected").forEach((item) => item.classList.remove("is-selected"));
}

function pcalShowPopover(anchor, label, build) {
  pcalClosePopover();
  const popover = pcal.popover;
  popover.setAttribute("aria-label", label);
  const close = pcalEl("button", "pcal-popover-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", pcalClosePopover);
  popover.appendChild(close);
  build(popover);
  popover.hidden = false;
  anchor.classList.add("is-selected");
  // Beside the block, inside the workspace: to the right when there is room, else to the left.
  const frame = plannerWorkspace.getBoundingClientRect();
  const box = anchor.getBoundingClientRect();
  const width = popover.offsetWidth, height = popover.offsetHeight;
  let left = box.right + 8 - frame.left;
  if (left + width > frame.width - 8) left = box.left - width - 8 - frame.left;
  const top = Math.min(Math.max(8, box.top - frame.top), frame.height - height - 8);
  popover.style.left = `${Math.max(8, left)}px`;
  popover.style.top = `${Math.max(8, top)}px`;
}

function pcalOpenAvailabilityPopover(slot, dateKey, block) {
  pcalShowPopover(block, "Availability", (popover) => {
    const start = plannerToMinutes(slot.start_at), end = plannerToMinutes(slot.end_at);
    popover.append(pcalEl("h4", "pcal-popover-title", "Available"),
      pcalEl("p", "pcal-popover-when", `${plannerDayLabel(dateKey)} · ${slot.start_at}–${slot.end_at} (${plannerFormatDuration(end - start)})`));
    if (pcalSlotIsPast(slot, dateKey)) return;   // history: shown, not editable
    const repeat = pcalEl("label", "pcal-popover-toggle");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = "pcal-repeat";
    checkbox.checked = Boolean(slot.is_recurring);
    repeat.append(checkbox, pcalEl("span", "", "Repeat weekly"));
    const remove = pcalEl("button", "pcal-button pcal-danger", "Delete");
    remove.type = "button";
    remove.id = "pcal-delete-availability";
    const actions = pcalEl("div", "pcal-popover-actions");
    actions.appendChild(remove);
    popover.append(repeat, actions);
    const run = async (steps) => {
      checkbox.disabled = remove.disabled = true;
      try {
        for (const [action, payload] of steps) await pcalChangeAvailability(action, payload);
      } catch (error) {
        showToast(error.message || "Could not update availability");
      }
      pcalClosePopover();
      plannerAfterAvailabilityChange();
    };
    const current = pcalSlotPayload(slot, dateKey);
    remove.addEventListener("click", () => run([["remove", current]]));
    checkbox.addEventListener("change", () => {
      // Weekly <-> this date only: the same time range moves between the two kinds of availability.
      const other = checkbox.checked
        ? { start_at: slot.start_at, end_at: slot.end_at, is_recurring: true, day_of_week: slot.day_of_week ?? Number(block.parentElement.dataset.weekday) }
        : { start_at: slot.start_at, end_at: slot.end_at, is_recurring: false, date: dateKey };
      run([["remove", current], ["add", other]]);
    });
  });
}

// The calendar popover's actions by state -- the primary action first; terminal or history-changing
// ones stay secondary. Only what the lifecycle API allows for that state (Home keeps its own set).
const PCAL_SESSION_ACTIONS = {
  confirmed: [["start", "Start", true], ["reschedule", "Reschedule", false], ["skip", "Skip", false]],
  active: [["start", "Resume", true], ["complete", "Complete session", false], ["skip", "Skip", false]],
  overdue: [["reschedule", "Reschedule", true], ["skip", "Skip", false]],
};

// The scheduler's own reason code, in plain words (its message when the code is unknown).
const PCAL_REASON_TEXT = {
  new_material: "Build understanding of new material.",
  deadline_approaching: "The deadline is coming up.",
  review_due: "This material is due for review.",
  low_quiz_score: "Your latest quiz shows this needs more practice.",
  flashcard_review_due: "Flashcard review is due.",
  final_review: "Final retrieval practice before the deadline.",
  quiz_in_progress: "Continue the quiz already in progress.",
  rescheduled: "Moved from an earlier study session.",
};

function pcalReasonText(reason) {
  if (!reason) return "";
  return PCAL_REASON_TEXT[reason.code] || reason.message || "";
}

// "How this plan was built": only the factors the planner really used, with this plan's data.
const PCAL_REASON_PHRASES = {
  new_material: "new material", deadline_approaching: "deadline catch-up", review_due: "spaced review",
  low_quiz_score: "practice after a low quiz score", flashcard_review_due: "flashcard review",
  final_review: "final review before a deadline", quiz_in_progress: "quiz to finish", rescheduled: "moved session",
};

function pcalPlanFactors() {
  const factors = [];
  const materials = plannerMaterials.map((material) => ({ material, state: plannerDocStates.get(material.document_id) }));
  const known = materials.filter((item) => item.state);
  if (known.length) {
    const counts = {};
    known.forEach(({ state }) => {
      const label = (PLANNER_LEARNING_STATE_LABELS[state.learning.state] || state.learning.state).toLowerCase();
      counts[label] = (counts[label] || 0) + 1;
    });
    factors.push(["Learning state", Object.entries(counts).map(([label, count]) => `${count} ${label}`).join(", ")]);
    const quizzed = known.filter(({ state }) => state.quiz.latest);
    factors.push(["Quiz performance", quizzed.length
      ? quizzed.map(({ material, state }) => `${pcalMaterialTitle(material)} ${Math.round(state.quiz.latest.percentage)}%`).join(", ")
      : "No quiz results yet — planning starts from new material."]);
  }
  const due = plannerMaterials.filter((material) => material.deadline);
  factors.push(["Deadlines", due.length
    ? due.map((material) => `${pcalMaterialTitle(material)} ${new Date(`${material.deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`).join(", ")
    : "None set — sessions spread over the next two weeks."]);
  const weekMinutes = pcalWeekDates().reduce((sum, date) => sum + pcalAvailabilityFor(plannerDateKey(date), (date.getDay() + 6) % 7)
    .reduce((total, slot) => total + plannerToMinutes(slot.end_at) - plannerToMinutes(slot.start_at), 0), 0);
  factors.push(["Available time", `Only the time you marked — ${plannerFormatDuration(weekMinutes)} this week.`]);
  const sessions = plannerHasLivePlan() ? plannerSessions : plannerPreview?.sessions || [];
  const reasons = {};
  sessions.forEach((session) => {
    const code = session.reason?.code;
    if (PCAL_REASON_PHRASES[code]) reasons[code] = (reasons[code] || 0) + 1;
  });
  if (Object.keys(reasons).length) {
    factors.push(["In this plan", Object.entries(reasons).map(([code, count]) => `${count} × ${PCAL_REASON_PHRASES[code]}`).join(", ")]);
  }
  return factors;
}

function pcalOpenPlanExplanation() {
  pcalShowPopover(pcal.explain, "How this plan was built", (popover) => {
    popover.appendChild(pcalEl("h4", "pcal-popover-title", "How this plan was built"));
    popover.appendChild(pcalEl("p", "pcal-popover-when", "Tutor picks each session from what your materials need, then fits it into your free time."));
    const list = pcalEl("dl", "pcal-factors");
    pcalPlanFactors().forEach(([term, detail]) => {
      const row = pcalEl("div", "pcal-factor");
      row.append(pcalEl("dt", "", term), pcalEl("dd", "", detail));
      list.appendChild(row);
    });
    popover.appendChild(list);
  });
}

function pcalWhen(iso) {
  return `${plannerDayLabel(iso.slice(0, 10))} · ${iso.slice(11, 16)}`;
}

function pcalOpenSessionPopover(kind, session, block) {
  const title = session.document_title || plannerDocumentTitle(session.document_id);
  const name = `${title} (${pcalActivity(session.activity_type)})`;
  pcalShowPopover(block, `${pcalActivity(session.activity_type)} · ${title}`, (popover) => {
    const holder = pcalEl("div", "pcal-popover-session");
    const content = pcalEl("div", "pcal-popover-body");
    content.append(pcalEl("span", `pcal-kind pcal-kind--${kind}`, PCAL_KIND_LABELS[kind]),
      pcalEl("h4", "pcal-popover-title", title),
      pcalEl("p", "pcal-popover-activity", pcalActivity(session.activity_type)),
      pcalEl("p", "pcal-popover-when", `${plannerDayLabel(session.scheduled_start.slice(0, 10))} · ${pcalSessionTimes(session)} · ${plannerFormatDuration(session.duration_minutes)}`));
    const reason = pcalReasonText(session.reason);
    if (reason) {
      const why = pcalEl("p", "pcal-popover-reason");
      why.append(pcalEl("span", "pcal-popover-caption", "Why this session?"), document.createTextNode(` ${reason}`));
      content.appendChild(why);
    }
    const deadline = pcalMaterialDeadline(session.document_id);
    if (deadline && !PLANNER_HISTORY_SESSION_STATUSES.includes(session.status)) {
      content.appendChild(pcalEl("p", "pcal-popover-note", `Due ${new Date(`${deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`));
    }
    const original = session.rescheduled_from && plannerSessionsById.get(session.rescheduled_from);
    if (original) content.appendChild(pcalEl("p", "pcal-popover-note", `Moved from ${pcalWhen(original.scheduled_start)}`));
    if (kind === "completed" && session.completed_at) {
      content.appendChild(pcalEl("p", "pcal-popover-note", `Completed ${new Date(session.completed_at).toLocaleString(undefined,
        { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`));
    }
    holder.appendChild(content);
    if (kind === "suggested") content.appendChild(pcalEl("p", "pcal-popover-hint", "Suggested. Accept plan to save it."));
    const actions = session.session_id ? PCAL_SESSION_ACTIONS[kind] || [] : [];
    if (actions.length) {
      const group = pcalEl("div", "pcal-popover-actions pcal-session-actions");
      actions.forEach(([action, label, primary]) => {
        const button = pcalEl("button", `${primary ? "primary-button" : "pcal-text-action"} planner-session-action planner-session-${action}`, label);
        button.type = "button";
        button.dataset.sessionAction = action;
        button.dataset.sessionId = session.session_id;
        button.setAttribute("aria-label", `${label} ${name}`);
        // The same lifecycle call as everywhere else: one request per session at a time.
        button.addEventListener("click", () => plannerSessionAction(session, action, button, group));
        group.appendChild(button);
      });
      holder.appendChild(group);
    }
    popover.appendChild(holder);
  });
  pcal.popover.querySelector(".pcal-session-actions .primary-button")?.focus();
}

// -- add materials sheet ------------------------------------------------------------

function pcalOpenSheet() {
  pcalClosePopover();
  pcal.sheet.hidden = false;
  pcal.addMaterials.setAttribute("aria-expanded", "true");
  pcalRenderSheet();
  pcal.sheet.querySelector(".pcal-sheet-add, #pcal-sheet-close")?.focus();
}

function pcalCloseSheet() {
  if (!pcal.sheet || pcal.sheet.hidden) return;
  pcal.sheet.hidden = true;
  pcal.addMaterials.setAttribute("aria-expanded", "false");
}

function pcalRenderSheet() {
  if (!pcal.sheet || pcal.sheet.hidden) return;
  pcal.sheetList.innerHTML = "";
  const chosen = new Set(plannerMaterials.map((material) => material.document_id));
  const available = indexedDocuments.filter((doc) => !chosen.has(doc.id));
  if (!indexedDocuments.length) {
    pcal.sheetList.appendChild(pcalEl("li", "pcal-sheet-empty", "Upload a document first, then add it here."));
    return;
  }
  if (!available.length) {
    pcal.sheetList.appendChild(pcalEl("li", "pcal-sheet-empty", "All your documents are in this plan."));
    return;
  }
  const today = plannerDateKey(plannerNow());
  available.forEach((doc) => {
    const row = pcalEl("li", "pcal-sheet-row");
    row.dataset.documentId = doc.id;
    const name = pcalEl("strong", "pcal-sheet-title", doc.title);
    name.title = doc.title;
    const facts = pcalEl("div", "pcal-sheet-facts");
    if (plannerDocStates.has(doc.id)) {
      const stateKey = pcalLearningKey(doc.id);
      const line = pcalEl("span", "pcal-sheet-state");
      line.append(pcalEl("span", `pcal-pill pcal-pill--${stateKey}`, PLANNER_LEARNING_STATE_LABELS[stateKey] || stateKey),
        pcalEl("span", "pcal-sheet-quiz", pcalQuizText(doc.id)));
      facts.append(line, pcalEl("span", "pcal-sheet-pack", pcalStudyPackText(doc.id)));
    }
    const { control: deadline, input, refresh } = pcalDeadlineControl("", `Deadline for ${doc.title} (optional)`, today);
    input.min = today;
    input.addEventListener("change", refresh);
    const add = pcalEl("button", "pcal-button pcal-sheet-add", "Add");
    add.type = "button";
    add.setAttribute("aria-label", `Add ${doc.title}`);
    add.addEventListener("click", () => pcalAddMaterial(doc.id, input.value || null, add));
    row.append(name, facts, deadline, add);
    pcal.sheetList.appendChild(row);
  });
}

// -- study queue ----------------------------------------------------------------------

function pcalQueueItem(entry, kind) {
  // One activity the scheduler still wants that has no calendar slot. Dragging it onto the
  // calendar places it (a server-validated placement); it has no lifecycle actions of its own.
  const title = entry.document_title || plannerDocumentTitle(entry.document_id);
  const item = pcalEl("li", "pcal-queue-item pcal-queue-item--unscheduled");
  item.append(pcalEl("strong", "pcal-queue-title", title),
    pcalEl("span", "pcal-queue-meta", `${pcalActivity(entry.activity_type)} · ${plannerFormatDuration(entry.estimated_minutes || 0)}`));
  if (entry.reason?.message) item.appendChild(pcalEl("small", "pcal-queue-reason", entry.reason.message));
  const due = entry.deadline
    ? `Due ${new Date(`${entry.deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · ` : "";
  item.appendChild(pcalEl("span", "pcal-queue-need", `${due}Needs a time slot`));
  if (entry.candidate_key && entry.estimated_minutes) {
    item.classList.add("is-draggable");
    item.dataset.candidateKey = entry.candidate_key;
    item.setAttribute("aria-label", `${pcalActivity(entry.activity_type)} · ${title}, needs a time slot`);
    item.addEventListener("pointerdown", (event) => pcalBeginMove(event, {
      kind: kind === "live" ? "candidate" : "unscheduled", key: entry.candidate_key, element: item,
      documentId: entry.document_id, duration: entry.estimated_minutes, deadline: entry.deadline,
      title, activity: entry.activity_type, grabMinutes: 0,
    }));
  }
  return item;
}

function pcalRenderQueue() {
  // The queue holds only work without a calendar slot: scheduled, suggested and past sessions are
  // on the calendar, and their actions live in its popovers (and on Home).
  pcal.queue.innerHTML = "";
  const waiting = plannerHasLivePlan()
    ? plannerLiveCandidates.map((candidate) => pcalQueueItem(candidate, "live"))
    : (plannerPreview?.capacity.unscheduled || []).map((entry) => pcalQueueItem(entry, "suggested"));
  waiting.forEach((item) => pcal.queue.appendChild(item));
  pcal.queueCount.textContent = waiting.length ? String(waiting.length) : "";
  if (waiting.length) return;
  const empty = pcalEl("li", "pcal-queue-empty");
  const live = plannerHasLivePlan();
  if (!live && !plannerMaterials.length) empty.appendChild(pcalEl("span", "", "Add materials to get started."));
  else if (!live && !plannerAvailability.length) empty.appendChild(pcalEl("span", "", "Mark some free time to see what fits."));
  else if (!live && plannerPreviewing) empty.appendChild(pcalEl("span", "", "Finding the best times…"));
  else empty.append(pcalEl("strong", "", "All caught up"),
    pcalEl("span", "", "Everything that needs attention is already on your calendar."));
  pcal.queue.appendChild(empty);
}

function pcalShiftWeek(days) {
  const next = new Date(pcalWeekDates()[0]);
  next.setDate(next.getDate() + days);
  plannerCalWeekStart = next;
  renderPlannerWorkspace();
}

setInterval(() => {
  // Keep the current-time line honest while the week is open.
  if (!plannerWorkspace || plannerWorkspace.hidden || document.body.dataset.page !== "planner" || pcalDrag) return;
  const line = pcal.body.querySelector(".pcal-now");
  const now = plannerNow();
  const passed = `${(now.getHours() * 60 + now.getMinutes()) * PCAL_MINUTE_PX}px`;
  if (line) line.style.top = passed;
  const shade = pcal.body.querySelector(".pcal-past-shade");
  if (shade) shade.style.height = passed;
}, 60 * 1000);

// -- direct manipulation: drag a session / queue item to a time (Phase 7B) ------------------
// The page only proposes a start; the server validates every drop (availability, overlap,
// deadline, status) and stays authoritative. An obviously invalid drop never sends a request.

function pcalMoveSourceFor(kind, session, element) {
  const grab = { element, documentId: session.document_id, duration: session.duration_minutes,
    title: session.document_title || plannerDocumentTitle(session.document_id), activity: session.activity_type };
  if (kind === "suggested" && session.candidate_key) return { ...grab, kind: "suggested", key: session.candidate_key, session };
  if ((kind === "confirmed" || kind === "overdue") && session.session_id && session.status === "scheduled") {
    return { ...grab, kind: "session", session };
  }
  return null;
}

function pcalBeginMove(event, source) {
  if ((event.button !== undefined && event.button > 0) || pcalMoveBusy) return;
  event.preventDefault();
  let grabMinutes = source.grabMinutes;
  if (grabMinutes === undefined) {
    const box = source.element.getBoundingClientRect();
    grabMinutes = Math.max(0, Math.round((event.clientY - box.top) / PCAL_MINUTE_PX));
  }
  pcalMove = { source, grabMinutes, x: event.clientX, y: event.clientY, active: false, target: null };
}

function pcalMaterialDeadline(documentId) {
  return plannerMaterials.find((material) => material.document_id === documentId)?.deadline || null;
}

function pcalOccupied(dateKey, source) {
  // Everything already on that day's calendar, except the item being moved.
  const same = (session) => session.scheduled_start.slice(0, 10) === dateKey;
  if (plannerHasLivePlan()) {
    return [...plannerSessions, ...plannerHistorySessions.filter((session) => session.status === "completed")]
      .filter((session) => same(session) && session !== source.session);
  }
  return [...(plannerPreview?.sessions || []), ...plannerHistorySessions.filter((session) => session.status === "completed")]
    .filter((session) => same(session) && !(source.key && session.candidate_key === source.key));
}

function pcalCheckTarget(dateKey, weekday, start, source) {
  // The same rules the server applies, so an obviously invalid drop sends nothing.
  const end = start + source.duration;
  const now = plannerNow();
  const todayKey = plannerDateKey(now);
  if (dateKey < todayKey || (dateKey === todayKey && start < now.getHours() * 60 + now.getMinutes())) return "That time has already passed.";
  const deadline = source.deadline || pcalMaterialDeadline(source.documentId);
  if (deadline && dateKey > deadline) return "That is after this material’s deadline.";
  const windows = pcalAvailabilityFor(dateKey, weekday)
    .map((slot) => [plannerToMinutes(slot.start_at), plannerToMinutes(slot.end_at)])
    .sort((left, right) => left[0] - right[0])
    .reduce((merged, [from, to]) => {
      const last = merged[merged.length - 1];
      if (last && from <= last[1]) last[1] = Math.max(last[1], to);
      else merged.push([from, to]);
      return merged;
    }, []);
  if (!windows.some(([from, to]) => from <= start && end <= to)) return "Pick a time inside your available hours.";
  const clash = pcalOccupied(dateKey, source).some((session) => {
    const from = plannerToMinutes(session.scheduled_start.slice(11, 16));
    const to = plannerToMinutes(session.scheduled_end.slice(11, 16));
    return from < end && to > start;
  });
  return clash ? "That time is already taken by another session." : null;
}

function pcalMoveElements() {
  let preview = document.getElementById("pcal-drop");
  if (!preview) {
    preview = pcalEl("div", "pcal-drop");
    preview.id = "pcal-drop";
    preview.setAttribute("aria-hidden", "true");
    preview.append(pcalEl("strong", "pcal-drop-title"), pcalEl("span", "pcal-drop-time"));
    pcal.scroll.appendChild(preview);
  }
  let chip = document.getElementById("pcal-drag-chip");
  if (!chip) {
    chip = pcalEl("div", "pcal-drag-chip");
    chip.id = "pcal-drag-chip";
    chip.setAttribute("aria-hidden", "true");
    document.body.appendChild(chip);
  }
  return { preview, chip };
}

function pcalUpdateMove(event) {
  const move = pcalMove;
  if (!move.active) {
    if (Math.hypot(event.clientX - move.x, event.clientY - move.y) < 5) return;
    move.active = true;
    pcalClosePopover();
    plannerWorkspace.classList.add("is-moving");
    move.source.element.classList.add("is-moving-source");
  }
  const { preview, chip } = pcalMoveElements();
  const scrollBox = pcal.scroll.getBoundingClientRect();
  const column = [...pcal.body.querySelectorAll(".pcal-col")].find((col) => {
    const box = col.getBoundingClientRect();
    return event.clientX >= box.left && event.clientX < box.right;
  });
  const overGrid = column && event.clientY >= scrollBox.top && event.clientY <= scrollBox.bottom;
  const label = `${pcalActivity(move.source.activity)} · ${move.source.title}`;
  if (!overGrid) {
    move.target = null;
    preview.hidden = true;
    chip.hidden = false;
    chip.textContent = label;
    chip.style.left = `${event.clientX + 12}px`;
    chip.style.top = `${event.clientY + 8}px`;
    return;
  }
  chip.hidden = true;
  const colBox = column.getBoundingClientRect();
  const raw = (event.clientY - colBox.top) / PCAL_MINUTE_PX - move.grabMinutes;
  const start = Math.min(PCAL_DAY_MINUTES - move.source.duration,
    Math.max(0, Math.round(raw / PCAL_SNAP_MINUTES) * PCAL_SNAP_MINUTES));
  const dateKey = column.dataset.date;
  const problem = pcalCheckTarget(dateKey, Number(column.dataset.weekday), start, move.source);
  move.target = { dateKey, start, problem };
  preview.hidden = false;
  preview.classList.toggle("is-invalid", Boolean(problem));
  preview.style.left = `${pcal.body.offsetLeft + column.offsetLeft + 2}px`;
  preview.style.width = `${column.offsetWidth - 4}px`;
  preview.style.top = `${pcal.body.offsetTop + start * PCAL_MINUTE_PX}px`;
  preview.style.height = `${Math.max(12, move.source.duration * PCAL_MINUTE_PX - 2)}px`;
  preview.querySelector(".pcal-drop-title").textContent = move.source.title;
  preview.querySelector(".pcal-drop-time").textContent = `${plannerMinutesToLabel(start)}–${plannerMinutesToLabel(start + move.source.duration)}`;
}

function pcalClearMoveVisuals(source) {
  plannerWorkspace.classList.remove("is-moving");
  source?.element?.classList.remove("is-moving-source");
  document.getElementById("pcal-drop")?.remove();
  document.getElementById("pcal-drag-chip")?.remove();
}

function pcalCancelMove() {
  const move = pcalMove;
  pcalMove = null;
  pcalClearMoveVisuals(move?.source);
}

function pcalBounce(source, message) {
  // An invalid drop: nothing is written; the item settles back where it was.
  if (message) showToast(message);
  const selector = source.kind === "session" ? `.pcal-event[data-session-id="${source.session.session_id}"]`
    : source.key ? `[data-candidate-key="${source.key}"]` : null;
  const element = selector ? plannerWorkspace.querySelector(selector) : null;
  if (!element) return;
  element.classList.remove("is-returning");
  void element.offsetWidth;
  element.classList.add("is-returning");
}

async function pcalEndMove(event) {
  const move = pcalMove;
  pcalMove = null;
  if (!move?.active) {
    pcalClearMoveVisuals(move?.source);
    return;   // a plain click: the popover opens as before
  }
  pcalSuppressClick = true;
  setTimeout(() => { pcalSuppressClick = false; }, 0);
  const { source, target } = move;
  if (!target) {
    pcalClearMoveVisuals(source);
    return;
  }
  if (target.problem) {
    pcalClearMoveVisuals(source);
    pcalBounce(source, target.problem);
    return;
  }
  const startIso = `${target.dateKey}T${plannerMinutesToLabel(target.start)}:00`;
  if (source.kind === "session" && source.session.scheduled_start === startIso) {
    pcalClearMoveVisuals(source);
    return;
  }
  document.getElementById("pcal-drop")?.classList.add("is-saving");
  try {
    if (source.kind === "session") await pcalRescheduleTo(source.session, startIso);
    else if (source.kind === "candidate") await pcalPlaceCandidate(source.key, startIso);
    else pcalSetPlacement(source.key, startIso);
  } finally {
    pcalClearMoveVisuals(source);
  }
}

function pcalSetPlacement(key, startIso) {
  // Before Accept: a draft move of one of the server's own suggestions. The preview re-runs with
  // it and the server applies it only if it is (still) valid; Accept sends the same moves.
  plannerPlacements = [...plannerPlacements.filter((item) => item.candidate_key !== key), { candidate_key: key, scheduled_start: startIso }];
  plannerQueueAutoPreview(0);
}

async function pcalRescheduleTo(session, startIso) {
  if (pcalMoveBusy || plannerBusySessions.has(session.session_id)) return;
  pcalMoveBusy = true;
  plannerBusySessions.add(session.session_id);
  try {
    const result = await plannerRequest(`${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/reschedule`, {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes(), target_start: startIso },
    });
    const moved = { ...result.session, plan_id: session.plan_id };
    plannerSessionsById.set(session.session_id, { ...session, ...(result.previous || {}) });
    plannerSessionsById.set(moved.session_id, moved);
    plannerSessions = [...plannerSessions.filter((item) => item.session_id !== session.session_id), moved];
    renderPlannerWorkspace();
    loadTodayPlan();
  } catch (error) {
    renderPlannerWorkspace();
    pcalBounce({ kind: "session", session }, error.message || "Could not move this session");
    if (error.status === 404 || error.detail?.code === "session_not_reschedulable") await loadPlannerData({ keepWeek: true });
  } finally {
    pcalMoveBusy = false;
    plannerBusySessions.delete(session.session_id);
  }
}

async function pcalPlaceCandidate(key, startIso) {
  if (pcalMoveBusy || !plannerPlan) return;
  pcalMoveBusy = true;
  try {
    const result = await plannerRequest(plannerPlanUrl("/candidates/place"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes(), candidate_key: key, scheduled_start: startIso },
    });
    plannerSessions = [...plannerSessions, { ...result.session, plan_id: plannerPlan.plan_id }];
    plannerLiveCandidates = plannerLiveCandidates.filter((candidate) => candidate.candidate_key !== key);
    renderPlannerWorkspace();
    loadTodayPlan();
    plannerLoadLiveCandidates();
  } catch (error) {
    pcalBounce({ kind: "candidate", key }, error.message || "Could not add this session");
    if (error.detail?.code === "candidate_stale" || error.detail?.code === "stale_plan") await loadPlannerData({ keepWeek: true });
  } finally {
    pcalMoveBusy = false;
  }
}

// ---- Home: Today's Study Plan (read-only) ------------------------------------

async function loadTodayPlan() {
  if (!todayPlanPanel) return;
  try {
    // Only active plans are current work: an archived plan's leftover scheduled sessions are not shown.
    const plans = (await plannerRequest(PLANNER_PLANS_API_URL)).filter((plan) => plan.status === "active");
    const lists = await Promise.all(plans.map((plan) => plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(plan.plan_id)}/sessions`)
      .then((sessions) => sessions.map((session) => ({ ...session, plan_id: plan.plan_id })))));
    const latestPlan = plans[plans.length - 1];
    const progress = latestPlan
      ? await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(latestPlan.plan_id)}/progress?utc_offset_minutes=${plannerUtcOffsetMinutes()}`).catch(() => null)
      : null;
    renderTodayPlan(lists.flat().filter(plannerIsActiveSession), progress);
  } catch (error) {
    renderTodayPlan(null);
  }
}

function plannerTodayAgenda(sessions, now = plannerNow()) {
  // Remaining = in progress, or not yet over by the browser's local clock.
  const nowIso = plannerLocalIso(now);
  const todayKey = plannerDateKey(now);
  const remaining = sessions
    .filter((session) => session.status === "in_progress" || session.scheduled_end > nowIso)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  const today = remaining.filter((session) => session.scheduled_start.slice(0, 10) === todayKey);
  const upcoming = today.length ? null : remaining.find((session) => session.scheduled_start.slice(0, 10) > todayKey) || null;
  // Scheduled sessions whose time has passed: offered Reschedule / Skip, never silently dropped.
  const overdue = sessions.filter((session) => plannerIsOverdue(session, now))
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  return { today, upcoming, overdue };
}

function renderTodayPlan(sessions, progress = null) {
  if (!todayPlanPanel) return;
  todayPlanPanel.hidden = false;
  todayPlanPanel.innerHTML = "";
  const now = plannerNow();
  const heading = document.createElement("div");
  heading.className = "today-plan-heading";
  const titles = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = now.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  const title = document.createElement("h2");
  title.id = "today-plan-title";
  title.textContent = "Today’s Study Plan";
  titles.append(eyebrow, title);
  const viewAll = document.createElement("button");
  viewAll.type = "button";
  viewAll.className = "text-button today-plan-view-all";
  viewAll.textContent = "View full schedule";
  viewAll.addEventListener("click", () => setPage("planner"));
  heading.append(titles, viewAll);
  todayPlanPanel.appendChild(heading);
  if (progress?.planned_sessions) {
    const line = document.createElement("p");
    line.className = "today-plan-progress";
    line.textContent = `${progress.completed_sessions} of ${progress.planned_sessions} sessions done · `
      + `${plannerFormatDuration(progress.completed_minutes)} studied · ${plannerFormatDuration(progress.remaining_minutes)} to go`;
    todayPlanPanel.appendChild(line);
  }

  const section = (label, className) => {
    const wrapper = document.createElement("section");
    wrapper.className = `today-plan-section ${className}`;
    const caption = document.createElement("h3");
    caption.textContent = label;
    wrapper.appendChild(caption);
    todayPlanPanel.appendChild(wrapper);
    return wrapper;
  };
  const empty = (text) => {
    const message = document.createElement("p");
    message.className = "empty-state today-plan-empty";
    message.textContent = text;
    todayPlanPanel.appendChild(message);
  };

  if (sessions === null) { empty("Your study plan could not be loaded right now."); return; }
  const { today, upcoming, overdue } = plannerTodayAgenda(sessions, now);
  const list = (items) => {
    const element = document.createElement("ol");
    element.className = "planner-session-list";
    items.forEach((session) => element.appendChild(plannerSessionItem(session, "li", { action: true })));
    return element;
  };
  if (today.length) {
    section("Next up", "today-plan-next").appendChild(plannerSessionItem(today[0], "div", { action: true }));
    if (today.length > 1) section("Later today", "today-plan-later").appendChild(list(today.slice(1)));
  } else if (upcoming) {
    empty("Nothing left for today.");
    section(`Next session · ${plannerDayLabel(upcoming.scheduled_start.slice(0, 10))}`, "today-plan-upcoming")
      .appendChild(plannerSessionItem(upcoming, "div", { action: true }));
  } else if (!overdue.length) {
    empty("No upcoming study sessions. Create a plan to see what to study next.");
  }
  if (overdue.length) section("Not completed", "today-plan-overdue").appendChild(list(overdue));
}

plannerView?.querySelectorAll("[data-planner-go]").forEach((button) => {
  button.addEventListener("click", () => plannerSetStep(button.dataset.plannerGo));
});
plannerToAvailabilityButton?.addEventListener("click", () => plannerSetStep("availability"));
plannerGeneratePreviewButton?.addEventListener("click", plannerGeneratePreview);
plannerConfirmButton?.addEventListener("click", plannerConfirm);
plannerNewPlanButton?.addEventListener("click", async () => {
  if (!plannerPlan) return;
  try {
    // Archive the confirmed plan (its saved sessions stay on the calendar) and start fresh.
    await plannerRequest(plannerPlanUrl(), { method: "PATCH", body: { status: "archived" } });
    plannerPlan = null;
    plannerMaterials = [];
    plannerSessions = [];
    plannerPreview = null;
    plannerSetStep("materials");
  } catch (error) {
    showToast(error.message || "Could not start a new plan");
  }
});
plannerPlanPrevWeekButton?.addEventListener("click", () => plannerShiftPlanWeek(-7));
plannerPlanNextWeekButton?.addEventListener("click", () => plannerShiftPlanWeek(7));
plannerPrevWeekButton?.addEventListener("click", () => {
  plannerWeekStart.setDate(plannerWeekStart.getDate() - 7);
  renderPlannerCalendar();
});
plannerNextWeekButton?.addEventListener("click", () => {
  plannerWeekStart.setDate(plannerWeekStart.getDate() + 7);
  renderPlannerCalendar();
});
plannerModeAvailableButton?.addEventListener("click", () => {
  plannerMode = "available";
  plannerModeAvailableButton.classList.add("active");
  plannerModeEraseButton?.classList.remove("active");
});
plannerModeEraseButton?.addEventListener("click", () => {
  plannerMode = "erase";
  plannerModeEraseButton.classList.add("active");
  plannerModeAvailableButton?.classList.remove("active");
});
// Pointer events so painting availability works with a mouse and with touch (small screens).
plannerCalendar?.addEventListener("pointermove", (event) => {
  if (!plannerDrag) return;
  const cell = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".planner-cell");
  if (cell) plannerExtendDrag(cell);
});
document.addEventListener("pointerup", () => {
  if (plannerDrag) plannerFinishDrag();
});
document.addEventListener("pointercancel", () => {
  if (plannerDrag) plannerFinishDrag();
});
// Desktop calendar workspace.
pcal.scroll?.addEventListener("scroll", () => {
  pcal.head.classList.toggle("is-scrolled", pcal.scroll.scrollTop > 2);
}, { passive: true });
pcal.today?.addEventListener("click", () => {
  plannerCalWeekStart = plannerMondayOf(plannerNow());
  renderPlannerWorkspace();
});
pcal.prev?.addEventListener("click", () => pcalShiftWeek(-7));
pcal.next?.addEventListener("click", () => pcalShiftWeek(7));
pcal.autoPlan?.addEventListener("click", () => plannerQueueAutoPreview(0));
pcal.explain?.addEventListener("click", () => (pcal.popover.hidden ? pcalOpenPlanExplanation() : pcalClosePopover()));
pcal.accept?.addEventListener("click", plannerAcceptPlan);
pcal.addMaterials?.addEventListener("click", () => (pcal.sheet.hidden ? pcalOpenSheet() : pcalCloseSheet()));
pcal.sheetClose?.addEventListener("click", pcalCloseSheet);
pcal.newPlan?.addEventListener("click", pcalStartNewPlan);
document.addEventListener("pointermove", (event) => {
  if (pcalDrag) pcalMoveDrag(event);
  if (pcalMove) pcalUpdateMove(event);
});
document.addEventListener("pointerup", (event) => {
  if (pcalDrag) pcalFinishDrag();
  if (pcalMove) pcalEndMove(event);
});
document.addEventListener("pointercancel", () => {
  if (pcalDrag) pcalFinishDrag();
  if (pcalMove) pcalCancelMove();
});
document.addEventListener("pointerdown", (event) => {
  // Click-away closes the popover and the add-materials sheet.
  if (!plannerWorkspace || plannerWorkspace.hidden) return;
  if (!pcal.popover.hidden && !pcal.popover.contains(event.target) && !event.target.closest?.(".pcal-event, .pcal-avail")) pcalClosePopover();
  if (!pcal.sheet.hidden && !pcal.sheet.contains(event.target) && !pcal.addMaterials.contains(event.target)) pcalCloseSheet();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !plannerWorkspace || plannerWorkspace.hidden) return;
  pcalClosePopover();
  pcalCloseSheet();
});
plannerDesktopQuery?.addEventListener?.("change", () => {
  if (document.body.dataset.page !== "planner") return;
  renderPlanner();
  if (plannerIsDesktop()) plannerQueueAutoPreview(0);
});

async function initializeApplication() {
  await initializeChatWorkspace();
  await Promise.all([loadIndexedDocuments(), loadQuizHistory(), loadDashboard(), loadKnowledgeGaps(), loadRecommendations(), loadGenerationModels(), loadTodayPlan()]);
}

async function bootstrapAuthentication() {
  try {
    const user = await fetchJson(AUTH_ME_API_URL);
    showAuthenticatedShell(user);
    await initializeApplication();
  } catch (error) {
    showAuthentication();
  }
}

setAuthMode("login");
bootstrapAuthentication();
