// Shared client state (loaded after js/planner.js: plannerWeekStart uses plannerMondayOf).
// Classic script (shared global scope), loaded by index.html before app.js.

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
