const pageTitles = {
  overview: "Study sessions",
  session: "Study Session",
  planner: "Study Planner",
  "model-comparison": "Model Comparison"
};

const navItems = document.querySelectorAll(".nav-item");
const views = document.querySelectorAll(".view");
const pageTitle = document.getElementById("page-title");
const resetButton = document.getElementById("reset-button");
const toast = document.getElementById("toast");
const messageList = document.getElementById("message-list");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
chatInput.placeholder = "Ask a question...";
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
          <nav class="quiz-player-nav" id="quiz-player-nav" aria-label="Jump to question"></nav>
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
// Desktop (>=1024px): the AI Tutor is a slide-over opened from "Ask AI Tutor" instead of a permanent
// column, so the learning content keeps its full width. It is only hidden/shown -- never re-rendered --
// so the conversation, draft and sources survive close/reopen. Below 1024px it stays inline as before.
const tutorOverlayQuery = window.matchMedia("(min-width: 1024px)");
const tutorLaunchButton = document.getElementById("tutor-launch-button");
// Keep the source control in the Tutor footer while retaining the existing IDs/handlers.
tutorLayout.insertBefore(toggleConversationSourcesButton, conversationSourcesPanel);
setSourcesDrawerOpen(false);
const overviewMaterialsList = document.getElementById("overview-materials-list");
const sessionDocumentName = document.getElementById("session-document-name");
const sessionDocumentStatus = document.getElementById("session-document-status");
const sessionMasteryList = document.getElementById("session-mastery-list");
const sessionCoverageList = document.getElementById("session-coverage-list");
const sessionProgressState = document.getElementById("session-progress-state");
const sessionProgressQuiz = document.getElementById("session-progress-quiz");
const sessionProgressPlan = document.getElementById("session-progress-plan");
const sessionProgressPack = document.getElementById("session-progress-pack");
const sessionQuizDetails = document.getElementById("session-quiz-details");
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
const homeMaterialSearch = document.getElementById("home-material-search");
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

const ADMIN_QUIZ_MODEL_BENCHMARK_API_URL = apiUrl("/api/admin/quiz-model-benchmark");
const BENCHMARK_QUESTION_COUNTS = [12, 15, 18, 20];
let benchmarkPollTimer = null;
const benchmarkOpenRawRuns = new Set();

function formatBenchmarkPercent(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

function formatBenchmarkSeconds(value) {
  return typeof value === "number" ? `${value.toFixed(1)}s` : "—";
}

function formatBenchmarkNumber(value, digits = 1) {
  return typeof value === "number" ? value.toFixed(digits) : "—";
}

function formatBenchmarkMs(value) {
  return typeof value === "number" ? formatBenchmarkSeconds(value / 1000) : "—";
}

function benchmarkCell(row, text, tag = "td") {
  const cell = document.createElement(tag);
  cell.textContent = text;
  row.appendChild(cell);
  return cell;
}

function benchmarkTable(headers) {
  const wrap = document.createElement("div");
  wrap.className = "summary-table-wrap";
  const table = document.createElement("table");
  table.className = "summary-table";
  const headRow = document.createElement("tr");
  headers.forEach((header) => benchmarkCell(headRow, header, "th"));
  const thead = document.createElement("thead");
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");
  table.append(thead, tbody);
  wrap.appendChild(table);
  return { wrap, tbody };
}

async function loadQuizModelComparison() {
  const view = document.getElementById("model-comparison-view");
  if (!view) return;
  clearTimeout(benchmarkPollTimer);
  if (!view.querySelector(".model-comparison-panel")) {
    view.innerHTML = '<div class="empty-state">Loading benchmark results…</div>';
  }
  try {
    const data = await fetchJson(ADMIN_QUIZ_MODEL_COMPARISON_API_URL);
    renderQuizModelComparison(view, data);
    if (data.benchmark_status === "running" && document.body.dataset.page === "model-comparison") {
      benchmarkPollTimer = setTimeout(loadQuizModelComparison, 5000);
    }
  } catch (error) {
    view.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "Could not load the Quiz model comparison.";
    view.appendChild(empty);
  }
}

function renderQuizModelComparison(view, data) {
  let panel = view.querySelector(".model-comparison-panel");
  if (!panel) {
    view.innerHTML = "";
    panel = document.createElement("article");
    panel.className = "panel model-comparison-panel";
    const heading = document.createElement("div");
    heading.className = "panel-heading";
    heading.innerHTML = "<div><p>Admin only</p><h2>Quiz Model Comparison</h2></div>";
    const form = buildBenchmarkForm(data);
    const results = document.createElement("div");
    results.className = "model-comparison-results";
    panel.append(heading, form, results);
    view.appendChild(panel);
  }
  panel.querySelector(".benchmark-run-button").disabled = data.benchmark_status === "running";
  renderBenchmarkResults(panel.querySelector(".model-comparison-results"), data);
}

function buildBenchmarkForm(data) {
  const form = document.createElement("form");
  form.className = "benchmark-form";
  form.innerHTML = (
    '<fieldset class="benchmark-documents"><legend>Documents</legend><p class="muted">Loading documents…</p></fieldset>'
    + '<label>Difficulty <select name="difficulty"><option value="easy">Easy</option>'
    + '<option value="medium" selected>Medium</option><option value="difficult">Difficult</option></select></label>'
    + '<label>Questions <select name="question_count">'
    + BENCHMARK_QUESTION_COUNTS.map((count) => `<option value="${count}">${count}</option>`).join("")
    + "</select></label>"
    + `<label>Runs per model per document <input name="runs" type="number" min="1" max="${data.defaults?.max_runs || 10}" value="${data.defaults?.runs || 3}"></label>`
    + '<button class="primary-button benchmark-run-button" type="submit">Run benchmark</button>'
    + '<p class="muted benchmark-form-status" role="status"></p>'
  );
  const fieldset = form.querySelector(".benchmark-documents");
  fetchJson(DOCUMENTS_API_URL).then((documents) => {
    fieldset.querySelector("p").remove();
    if (!documents.length) {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "No indexed documents. Upload one first.";
      fieldset.appendChild(note);
    }
    documents.forEach((doc) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.name = "document_ids";
      box.value = doc.id;
      label.append(box, document.createTextNode(` ${doc.title || doc.id}`));
      fieldset.appendChild(label);
    });
  }).catch((error) => { fieldset.querySelector("p").textContent = error.message || "Could not load documents."; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = form.querySelector(".benchmark-form-status");
    const documentIds = [...form.querySelectorAll('input[name="document_ids"]:checked')].map((box) => box.value);
    if (!documentIds.length) { status.textContent = "Choose at least one document."; return; }
    const button = form.querySelector(".benchmark-run-button");
    button.disabled = true;
    status.textContent = "Starting benchmark…";
    try {
      await fetchJson(ADMIN_QUIZ_MODEL_BENCHMARK_API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          document_ids: documentIds,
          difficulty: form.elements.difficulty.value,
          question_count: Number(form.elements.question_count.value),
          runs: Number(form.elements.runs.value),
        }),
      });
      status.textContent = "";
      loadQuizModelComparison();
    } catch (error) {
      status.textContent = error.message || "Could not start the benchmark.";
      button.disabled = false;
    }
  });
  return form;
}

function renderBenchmarkResults(container, data) {
  container.innerHTML = "";
  const statusLine = document.createElement("p");
  statusLine.className = "muted";
  const progress = data.progress ? ` (${data.progress.completed}/${data.progress.total} runs)` : "";
  const statusText = {
    running: `Benchmark running${progress}…`,
    interrupted: `Last benchmark was interrupted${progress}.`,
    failed: `Last benchmark stopped early${progress}: ${data.error?.message || "unknown error"}`,
  }[data.benchmark_status];
  statusLine.textContent = statusText || (data.generated_at
    ? `Last benchmark: ${new Date(data.generated_at).toLocaleString()}${progress}`
    : "Last benchmark: not run yet");
  container.appendChild(statusLine);

  const config = data.config;
  if (config) {
    const summary = document.createElement("p");
    summary.className = "muted benchmark-config";
    const docs = (config.documents || []).map((doc) => `${doc.title} (${doc.flashcard_count} flashcards)`).join(", ");
    summary.textContent = `Same inputs for every model: ${docs} · ${config.difficulty} · ${config.question_count} questions · `
      + `${config.runs_per_model_per_document} runs/model/document · engine ${config.quiz_engine_version} · prompt ${config.quiz_prompt_version}`;
    container.appendChild(summary);
  }

  const { wrap, tbody } = benchmarkTable([
    "Model", "Runs", "Success Rate", "Final-question Rate", "Grounding", "Avg Latency", "p50", "p95",
    "Avg Retries", "Avg tokens/s", "Failures", "Questions/min",
  ]);
  (data.models || []).forEach((model) => {
    const row = document.createElement("tr");
    const nameCell = benchmarkCell(row, `${model.label} `);
    const badge = document.createElement("span");
    const isProduction = model.status === "current_production";
    badge.className = `soft-badge ${isProduction ? "production" : "candidate"}`;
    badge.textContent = isProduction ? "Current Production" : "Benchmark Candidate";
    nameCell.appendChild(badge);
    [
      formatBenchmarkNumber(model.runs, 0),
      formatBenchmarkPercent(model.success_rate),
      formatBenchmarkPercent(model.final_question_rate),
      formatBenchmarkPercent(model.grounding_rate),
      formatBenchmarkSeconds(model.avg_latency_seconds),
      formatBenchmarkSeconds(model.p50_latency_seconds),
      formatBenchmarkSeconds(model.p95_latency_seconds),
      formatBenchmarkNumber(model.avg_retries),
      formatBenchmarkNumber(model.avg_tokens_per_second),
      formatBenchmarkNumber(model.failures, 0),
      formatBenchmarkNumber(model.questions_per_minute),
    ].forEach((text) => benchmarkCell(row, text));
    if (!model.measured) row.classList.add("model-comparison-unmeasured");
    tbody.appendChild(row);
  });
  container.appendChild(wrap);

  const legend = document.createElement("p");
  legend.className = "muted benchmark-legend";
  legend.textContent = "Final-question rate = final questions / requested (failed runs count as 0). "
    + "Grounding = validated candidates / (validated + grounding rejections). "
    + "Latency = quiz pipeline time of successful measured runs; each model is pulled and loaded before its runs, "
    + "and that cold start is shown separately below, never inside latency. No overall score is computed.";
  container.appendChild(legend);

  const warmups = data.warmups || [];
  if (warmups.length) {
    const title = document.createElement("h3");
    title.className = "benchmark-subheading";
    title.textContent = "Cold start (warm-up before measured runs)";
    container.appendChild(title);
    const cold = benchmarkTable(["Model", "Prepare / pull", "Load into memory", "Ollama load", "Cold start total", "Result"]);
    warmups.forEach((warmup) => {
      const row = document.createElement("tr");
      const label = (data.models || []).find((model) => model.model_id === warmup.model_id)?.label || warmup.model_id;
      [
        label,
        `${formatBenchmarkMs(warmup.model_prepare_ms)}${warmup.pulled ? " (pulled)" : ""}`,
        formatBenchmarkMs(warmup.warm_ms),
        formatBenchmarkMs(warmup.ollama_load_ms),
        formatBenchmarkMs(warmup.cold_start_ms),
        warmup.success ? "Ready" : `Failed: ${warmup.error?.message || "unknown error"}`,
      ].forEach((text) => benchmarkCell(row, text));
      if (!warmup.success) row.classList.add("benchmark-run-failed");
      cold.tbody.appendChild(row);
    });
    container.appendChild(cold.wrap);
  }

  if (!(data.models || []).some((model) => model.measured) && data.benchmark_status !== "running") {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "No benchmark data yet. Choose documents above and run the benchmark.";
    container.appendChild(note);
  }

  (data.models || []).forEach((model) => {
    const runs = (data.runs || []).filter((run) => run.model_id === model.model_id);
    if (!runs.length) return;
    const details = document.createElement("details");
    details.className = "benchmark-raw-runs";
    details.open = benchmarkOpenRawRuns.has(model.model_id);
    details.addEventListener("toggle", () => {
      if (details.open) benchmarkOpenRawRuns.add(model.model_id);
      else benchmarkOpenRawRuns.delete(model.model_id);
    });
    const summary = document.createElement("summary");
    summary.textContent = `Raw runs — ${model.label} (${runs.length})`;
    details.appendChild(summary);
    const raw = benchmarkTable([
      "Document", "Run", "Result", "Final / Requested", "Validated", "Rejected", "Latency", "Retries",
      "LLM calls", "Tokens in / out", "tokens/s", "Load in run", "Quiz", "Error",
    ]);
    runs.forEach((run) => {
      const row = document.createElement("tr");
      [
        run.document_title || run.document_id,
        run.run_number,
        run.success ? "Success" : "Failed",
        `${run.final_count ?? "—"} / ${run.requested_count ?? "—"}`,
        run.validated_count ?? "—",
        run.rejected_count ?? "—",
        typeof run.total_ms === "number" ? formatBenchmarkSeconds(run.total_ms / 1000) : "—",
        run.retries ?? "—",
        run.llm_calls ?? "—",
        `${run.prompt_tokens ?? "—"} / ${run.generated_tokens ?? "—"}`,
        formatBenchmarkNumber(run.tokens_per_second),
        typeof run.ollama_load_ms === "number" ? `${run.ollama_load_ms} ms` : "—",
        run.quiz_id || "—",
        run.error ? `${run.error.type}: ${run.error.message}` : "",
      ].forEach((text) => benchmarkCell(row, String(text)));
      if (!run.success) row.classList.add("benchmark-run-failed");
      raw.tbody.appendChild(row);
    });
    details.appendChild(raw.wrap);
    const json = document.createElement("details");
    const jsonSummary = document.createElement("summary");
    jsonSummary.textContent = "All recorded fields (JSON)";
    const pre = document.createElement("pre");
    pre.className = "benchmark-raw-json";
    pre.textContent = JSON.stringify(runs, null, 2);
    json.append(jsonSummary, pre);
    details.appendChild(json);
    container.appendChild(details);
  });
}

// ---- Document progress (Phase 6A): learning state, quiz results, study plan -------------------

let documentProgressRequest = 0;

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
  overviewMaterialsList.querySelectorAll(".home-doc-card").forEach((row) => {
    row.hidden = Boolean(query && !row.dataset.title.includes(query));
  });
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

// ---- Home: Continue studying ------------------------------------------------------------------
// One card per document, built only from /api/progress/documents (DocumentStudyState + the active
// plan): learning state, the latest completed quiz (only when there is one -- never a fake 0%),
// what the Study Pack holds, the next planned session and the deadline. Cross-document headline
// metrics (topics mastered, overall accuracy) are intentionally not shown on Home.
const HOME_SEARCH_MIN_MATERIALS = 7;   // search only helps once the library is large
let homeDocStates = new Map();   // document id -> progress payload
let homeDocStatesLoad = 0;

// With quizId, opens exactly that quiz artifact (Quiz Library Start/Resume) -- staleness is then
// tracked by request sequence (quizId never changes with the settings selectors, so the old
// settings-key comparison would never catch a superseded request). Without quizId, keeps the
// older settings-key staleness check for callers that have no specific artifact to open.
let quizDetailRequestSeq = 0;

const QUIZ_OPTION_LETTERS = "ABCD";

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
newConversationButton.addEventListener("click", createChatConversation);
applyConversationSourcesButton.addEventListener("click", applyConversationSources);
toggleConversationSourcesButton.addEventListener("click", () => {
  setSourcesDrawerOpen(!tutorLayout.classList.contains("sources-open"));
});
closeConversationSourcesButton.addEventListener("click", closeSourcesDrawer);
sourcesDrawerBackdrop.addEventListener("click", closeSourcesDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (tutorLayout.classList.contains("sources-open")) {
    closeSourcesDrawer();
    toggleConversationSourcesButton.focus();
  } else if (tutorOverlayOpen()) {
    setTutorOverlayOpen(false);
  }
});
tutorLaunchButton?.addEventListener("click", () => setTutorOverlayOpen(!tutorOverlayOpen()));
document.getElementById("tutor-close-button")?.addEventListener("click", () => setTutorOverlayOpen(false));
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

class PlannerRequestError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const PLANNER_HISTORY_SESSION_STATUSES = ["completed", "skipped"];
const PLANNER_STATUS_LABELS = { completed: "Completed", skipped: "Skipped" };

const plannerBusySessions = new Set();   // session ids with a lifecycle request in flight

// ---- Adaptive replanning (Phase 5B2) ------------------------------------------
// The server recomputes every proposal from the trigger; the page never sends session changes.
// Small proposals are applied right away with a short note; large ones wait for the learner.

const plannerAdaptingPlans = new Set();   // plan ids with an adaptation request in flight
let plannerAdaptReview = null;   // desktop: a large proposal shown on the calendar {planId, trigger, proposal, busy, jump}
let plannerAdaptStale = null;    // desktop: Accept found a newer plan {planId, trigger}; a fresh check is offered

const PCAL_CHANGE_LABELS = { added: "New", moved: "Moved here", moving: "Moving", cancelled: "No longer needed" };
const PCAL_CHANGE_KIND_LABELS = {
  added: "Suggested new session", moved: "Suggested new time", moving: "Suggested move", cancelled: "Suggested removal",
};

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

// Each document's real study state -- learning state, Study Pack contents, latest quiz -- from
// /api/progress/documents (DocumentStudyState). Nothing here is inferred on the page.
let plannerDocStates = new Map();   // document id -> progress payload (null while loading / unavailable)
let plannerDocStatesLoad = 0;

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

// "How this plan was built": only the factors the planner really used, with this plan's data.
const PCAL_REASON_PHRASES = {
  new_material: "new material", deadline_approaching: "deadline catch-up", review_due: "spaced review",
  low_quiz_score: "practice after a low quiz score", flashcard_review_due: "flashcard review",
  final_review: "final review before a deadline", quiz_in_progress: "quiz to finish", rescheduled: "moved session",
};

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
