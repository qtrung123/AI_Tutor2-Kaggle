const pageTitles = {
  overview: "Study sessions",
  session: "Study Session"
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
const RECOMMENDATIONS_OVERVIEW_LIMIT = Number(window.APP_CONFIG?.RECOMMENDATIONS_OVERVIEW_LIMIT || 4);

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
let quizHistoryDifficultyFilter = "all";
let quizHistoryScopeFilter = "all";
let quizQuestionIndex = 0;
let conversations = [];
let dashboardData = null;
let knowledgeGaps = [];
let recommendations = [];
let currentUser = null;
let authMode = "login";
let activeConversation = null;
let generationModels = [];
let selectedModelId = localStorage.getItem("aiTutorModelId") || "";
let activeDocumentId = "";

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
const quizDocumentSelect = document.getElementById("quiz-document-select");
const quizTopicSelect = document.getElementById("quiz-topic-select");
const quizScopeSelect = document.getElementById("quiz-scope-select");
const quizTopicField = document.getElementById("quiz-topic-field");
const quizDifficultySelect = document.getElementById("quiz-difficulty-select");
let quizQuestionCountSelect = document.getElementById("quiz-question-count-select");
const generateQuizButton = document.getElementById("generate-quiz-button");
const resetQuizButton = document.getElementById("reset-quiz-button");
resetQuizButton.textContent = "Retake Quiz";
const newQuizButton = document.getElementById("new-quiz-button");
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
const overviewMasteryList = document.getElementById("overview-mastery-list");
const continueLearningList = document.getElementById("continue-learning-list");
const overviewMaterialsList = document.getElementById("overview-materials-list");
const overviewKnowledgeGapsList = document.getElementById("overview-knowledge-gaps-list");
const overviewRecommendationsList = document.getElementById("overview-recommendations-list");
const learningStatusTitle = document.getElementById("learning-status-title");
const learningStatusCopy = document.getElementById("learning-status-copy");
const learningStatusAction = document.getElementById("learning-status-action");
const sidebarDocumentCount = document.getElementById("sidebar-document-count");
const sidebarTopicProgress = document.getElementById("sidebar-topic-progress");
const sidebarTopicStatus = document.getElementById("sidebar-topic-status");
const practiceMasteryPanel = document.getElementById("practice-mastery-panel");
const practiceMasteryList = document.getElementById("practice-mastery-list");
const sessionDocumentName = document.getElementById("session-document-name");
const sessionDocumentStatus = document.getElementById("session-document-status");
const sessionMaterialDetails = document.getElementById("session-material-details");
const sessionTopicList = document.getElementById("session-topic-list");
const sessionMasteryList = document.getElementById("session-mastery-list");
const sessionCoverageList = document.getElementById("session-coverage-list");
const sessionKnowledgeGapsList = document.getElementById("session-knowledge-gaps-list");
const sessionRecommendationsList = document.getElementById("session-recommendations-list");
const assessmentControl = document.querySelector('[data-session-pane="quiz"] .assessment-control');
let quizCreateDialog = null;
const originalContentFrame = document.getElementById("original-content-frame");
const originalContentFileName = document.getElementById("original-content-file-name");
const originalContentOpen = document.getElementById("original-content-open");
const originalContentEmpty = document.getElementById("original-content-empty");
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
}

function showAuthentication() {
  currentUser = null;
  appShell.hidden = true;
  authScreen.hidden = false;
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
  views.forEach((view) => view.classList.toggle("active", view.id === `${page}-view`));
  pageTitle.textContent = pageTitles[page];
  if (pageTitles[page]) showToast(`Opened ${pageTitles[page]}`);
}

async function loadGenerationModels() {
  try {
    const data = await fetchJson(MODELS_API_URL);
    generationModels = data.models || [];
    if (!generationModels.some((model) => model.id === selectedModelId)) selectedModelId = generationModels.find((model) => model.default)?.id || generationModels[0]?.id || "";
    renderModelSelector();
  } catch (error) { generationModels = []; }
}

function renderModelSelector() {
  const header = document.querySelector(".session-header");
  if (!header || !generationModels.length) return;
  let select = document.getElementById("generation-model-select");
  if (!select) {
    const label = document.createElement("label"); label.className = "generation-model-control"; label.textContent = "Model:";
    select = document.createElement("select"); select.id = "generation-model-select"; label.appendChild(select); header.appendChild(label);
    select.addEventListener("change", async () => {
      selectedModelId = select.value; localStorage.setItem("aiTutorModelId", selectedModelId);
      select.disabled = true;
      try { await fetchJson(`${MODELS_API_URL}/${encodeURIComponent(selectedModelId)}/prepare`, { method: "POST" }); showToast("Model ready"); }
      catch (error) { showToast(error.message || "Model is still preparing"); }
      finally { select.disabled = false; }
    });
  }
  select.innerHTML = "";
  generationModels.forEach((model) => select.add(new Option(model.label, model.id, false, model.id === selectedModelId)));
}

function setSessionTab(tab) {
  document.querySelectorAll(".session-tab").forEach((button) => button.classList.toggle("active", button.dataset.sessionTab === tab));
  document.querySelectorAll(".session-pane").forEach((pane) => pane.classList.toggle("active", pane.dataset.sessionPane === tab));
  document.getElementById("persistent-tutor").hidden = false;
}

async function openStudySession(documentId, tab = "material", topicId = "") {
  const documentItem = indexedDocuments.find((item) => item.id === documentId);
  if (!documentItem) return;
  activeDocumentId = documentId;
  const sessionBreadcrumb = document.getElementById("session-home-button");
  if (sessionBreadcrumb) sessionBreadcrumb.textContent = `Home > ${documentItem.title}`;
  try {
    await ensureStudySessionConversation();
  } catch (error) {
    console.error("Could not prepare the Study Session conversation", error);
    showToast(error.message || "Could not scope the tutor to this document");
  }
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
  sessionMaterialDetails.innerHTML = `<dl><div><dt>Document</dt><dd>${documentItem.title}</dd></div><div><dt>Indexed content</dt><dd>${documentItem.chunks || 0} chunks available for AI Tutor and quizzes</dd></div></dl>`;
  sessionTopicList.innerHTML = "";
  (documentItem.topics || []).forEach((topic) => { const item = document.createElement("div"); item.className = "mastery-card"; item.innerHTML = `<strong>${topic.name}</strong><span>Ready for assessment</span>`; sessionTopicList.appendChild(item); });
  if (!documentItem.topics?.length) sessionTopicList.innerHTML = '<div class="empty-state">No extracted topics are available yet.</div>';
  if (quizDocumentSelect) quizDocumentSelect.value = documentId;
  updateTopicOptions();
  if (topicId && topicId !== "document") { quizScopeSelect.value = "topic"; quizTopicSelect.value = topicId; }
  else quizScopeSelect.value = "document";
  updateAssessmentScope();
  await loadSelectedQuiz();
  renderSessionProgress(documentId);
  setPage("session");
  setSessionTab(tab);
}

function renderSessionProgress(documentId) {
  const assessed = (dashboardData?.mastery || []).filter((item) => item.document_id === documentId && item.mastery_level !== "Not assessed");
  renderMasteryList(sessionMasteryList, assessed, { emptyText: "No assessed topics yet. Use Quiz to begin building mastery." });
  sessionCoverageList.innerHTML = "";
  if (!assessed.length) sessionCoverageList.innerHTML = '<div class="empty-state">Concept coverage appears after an assessment.</div>';
  assessed.forEach((item) => { const card = document.createElement("div"); card.className = "mastery-card"; card.innerHTML = `<strong>${item.topic_name || item.topic_id}</strong><span>${item.concept_coverage || "Coverage is pending"}</span>`; sessionCoverageList.appendChild(card); });
  const gaps = knowledgeGaps.filter((item) => item.document_id === documentId);
  sessionKnowledgeGapsList.innerHTML = "";
  if (!gaps.length) sessionKnowledgeGapsList.innerHTML = '<div class="empty-state">No reliable knowledge gaps detected.</div>';
  gaps.forEach((gap) => { const item = document.createElement("div"); item.className = "knowledge-gap-row"; item.textContent = `${gap.topic_name || gap.topic_id} · ${gap.reason || "Needs more practice"}`; sessionKnowledgeGapsList.appendChild(item); });
  const next = recommendations.filter((item) => item.document_id === documentId);
  sessionRecommendationsList.innerHTML = "";
  if (!next.length) sessionRecommendationsList.innerHTML = '<div class="empty-state">Recommendations will appear as learning evidence grows.</div>';
  next.forEach((recommendation) => { const button = document.createElement("button"); button.className = "continue-item"; button.type = "button"; button.textContent = recommendation.action || recommendation.topic_name; button.addEventListener("click", () => openStudySession(documentId, "quiz", recommendation.topic_id)); sessionRecommendationsList.appendChild(button); });
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

async function handleChatSubmit(event) {
  event.preventDefault();
  const userText = chatInput.value.trim();
  if (!userText) {
    showToast("Type a question first");
    return;
  }

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
}

function getSelectedQuizStatus() {
  const documentId = quizDocumentSelect?.value;
  return quizStatuses.find((item) => item.document_id === documentId);
}

function selectedDifficulty() {
  return quizDifficultySelect?.value || "easy";
}

function selectedQuestionCount() {
  const value = Number(quizQuestionCountSelect?.value || 10);
  return [10, 15, 20, 25].includes(value) ? value : 10;
}

function selectedTopicId() {
  return selectedAssessmentScope() === "document" ? "document" : (quizTopicSelect?.value || "");
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

async function openPracticeContext(documentId, topicId = "") {
  setPage("practice");
  if (documentId && quizDocumentSelect) quizDocumentSelect.value = documentId;
  updateTopicOptions();
  if (topicId && topicId !== "document") {
    quizScopeSelect.value = "topic";
    quizTopicSelect.value = topicId;
  } else {
    quizScopeSelect.value = "document";
  }
  updateAssessmentScope();
  await loadSelectedQuiz();
}

function dashboardAction(label, page, className = "continue-item", onClick = null) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick || (() => setPage(page)));
  return button;
}

function applySessionLibraryFilters() {
  const query = (sessionSearchInput?.value || "").trim().toLowerCase();
  const rows = [...overviewMaterialsList.querySelectorAll(".study-session-card")];
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
    button.textContent = material.document_name;
    button.addEventListener("click", () => openStudySession(material.document_id));
    sidebarRecentDocuments.appendChild(button);
  });
}

function renderDashboard() {
  if (!dashboardData) return;
  const metrics = dashboardData.metrics || {};
  const documentCount = Number(metrics.documents || 0);
  const totalTopics = Number(metrics.total_topics || 0);
  const assessedTopics = Number(metrics.topics_assessed || 0);
  const masteredTopics = Number(metrics.topics_mastered || 0);
  const completedAnswers = Number(metrics.answered_questions || 0);
  const kpis = [
    ["Learning materials", String(documentCount), `${documentCount} active learning material${documentCount === 1 ? "" : "s"}`],
    ["Topics assessed", `${assessedTopics} / ${totalTopics}`, totalTopics ? "Across all learning materials" : "No extracted topics yet"],
    ["Overall accuracy", metrics.quiz_accuracy == null ? "—" : `${Math.round(metrics.quiz_accuracy)}%`, `${completedAnswers} completed answer${completedAnswers === 1 ? "" : "s"}`],
    ["Topics mastered", `${masteredTopics} / ${totalTopics}`, totalTopics ? "Across all learning materials" : "No extracted topics yet"],
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

  sidebarDocumentCount.textContent = metrics.documents ? `${metrics.documents} indexed document${metrics.documents === 1 ? "" : "s"}` : "No materials";
  const topicPercent = metrics.total_topics ? Math.round(100 * metrics.topics_assessed / metrics.total_topics) : 0;
  sidebarTopicProgress.style.width = `${topicPercent}%`;
  sidebarTopicStatus.textContent = metrics.total_topics ? `${metrics.topics_assessed} of ${metrics.total_topics} topics assessed` : "Add a document to begin";
  const overviewMasteries = (dashboardData.mastery || [])
    .filter((mastery) => mastery.mastery_level !== "Not assessed")
    .slice(0, 8);
  renderMasteryList(overviewMasteryList, overviewMasteries, {
    showDocument: true,
    emptyText: "No assessed topics yet. Complete a quiz to see mastery progress.",
  });

  const latest = dashboardData.latest_attempt;
  if (latest) {
    learningStatusTitle.textContent = `Continue with ${latest.document_id}`;
    learningStatusCopy.textContent = `Latest completed quiz: ${latest.score}/${latest.total} at ${latest.difficulty} difficulty.`;
    learningStatusAction.textContent = "Continue practice";
    learningStatusAction.onclick = () => openPracticeContext(latest.document_id, latest.topic_id);
  } else if (metrics.documents) {
    learningStatusTitle.textContent = "Your materials are ready";
    learningStatusCopy.textContent = "Generate a grounded assessment to begin collecting mastery evidence.";
    learningStatusAction.textContent = "Start practicing";
    learningStatusAction.onclick = () => setPage("practice");
  } else {
    learningStatusTitle.textContent = "Add learning material to begin";
    learningStatusCopy.textContent = "Upload and index a PDF or TXT document before using quizzes and mastery.";
    learningStatusAction.textContent = "Open materials";
    learningStatusAction.onclick = () => setPage("materials");
  }

  continueLearningList.innerHTML = "";
  if (latest) {
    const practice = dashboardAction(
      `Practice · ${latest.document_id}`,
      "practice",
      "continue-item",
      () => openPracticeContext(latest.document_id, latest.topic_id)
    );
    continueLearningList.appendChild(practice);
  }
  const recentConversation = conversations[0];
  if (recentConversation) continueLearningList.appendChild(dashboardAction(`AI Tutor · ${recentConversation.title || "Recent conversation"}`, "tutor"));
  if (!continueLearningList.children.length) {
    continueLearningList.appendChild(dashboardAction(metrics.documents ? "Start a grounded quiz" : "Add your first material", metrics.documents ? "practice" : "materials"));
  }

  overviewMaterialsList.innerHTML = "";
  const materials = dashboardData.materials || [];
  if (!materials.length) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No indexed documents yet."; overviewMaterialsList.appendChild(empty);
  } else {
    materials.forEach((material) => {
      const row = document.createElement("article"); row.className = "study-session-card";
      row.dataset.title = (material.document_name || "").toLowerCase();
      row.dataset.topicCount = String(material.topic_count || 0);
      const copy = document.createElement("div"); const title = document.createElement("strong"); title.textContent = material.document_name;
      const meta = document.createElement("span"); meta.textContent = `${material.topic_count} topics · ${material.assessed_topic_count} assessed`;
      const gapCount = knowledgeGaps.filter((gap) => gap.document_id === material.document_id).length;
      const gap = document.createElement("small"); gap.textContent = `${gapCount} knowledge gap${gapCount === 1 ? "" : "s"}`;
      copy.append(title, meta, gap); const action = document.createElement("button"); action.className = "text-button"; action.type = "button"; action.textContent = "Open session →";
      action.addEventListener("click", () => openStudySession(material.document_id));
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) openStudySession(material.document_id); });
      row.append(copy, action); overviewMaterialsList.appendChild(row);
    });
  }
  renderSidebarRecentDocuments(materials);
  applySessionLibraryFilters();
  renderRecommendations();
}

async function loadDashboard() {
  try {
    dashboardData = await fetchJson(DASHBOARD_API_URL);
    renderDashboard();
    renderPracticeMastery();
  } catch (error) {
    dashboardData = null;
    learningStatusTitle.textContent = "Dashboard unavailable";
    learningStatusCopy.textContent = "The current learning state could not be loaded.";
    overviewKpis.innerHTML = '<div class="empty-state">Dashboard data is unavailable.</div>';
  }
}

function renderKnowledgeGaps() {
  overviewKnowledgeGapsList.innerHTML = "";
  if (!knowledgeGaps.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No reliable knowledge gaps detected. Unassessed topics are not classified as gaps.";
    overviewKnowledgeGapsList.appendChild(empty);
    return;
  }
  knowledgeGaps.forEach((gap) => {
    const row = document.createElement("article");
    row.className = `knowledge-gap-row severity-${gap.severity}`;
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = gap.topic_name || gap.topic_id;
    const detail = document.createElement("span");
    detail.textContent = `${gap.document_id} · ${Math.round(gap.mastery_score)}% mastery · ${Math.round(100 * gap.concept_coverage_ratio)}% concept coverage`;
    const evidence = document.createElement("small");
    evidence.textContent = `${gap.distinct_concepts_assessed}/${gap.assessment_capacity} concepts · ${gap.answered_questions} answers · sufficient evidence`;
    copy.append(title, detail, evidence);
    const severity = document.createElement("span");
    severity.className = "gap-severity";
    severity.textContent = gap.severity === "high" ? "High priority" : "Moderate";
    row.append(copy, severity);
    overviewKnowledgeGapsList.appendChild(row);
  });
}

async function loadKnowledgeGaps() {
  try {
    knowledgeGaps = await fetchJson(KNOWLEDGE_GAPS_API_URL);
  } catch (error) {
    knowledgeGaps = [];
  }
  renderKnowledgeGaps();
  renderDashboard();
  if (activeDocumentId) renderSessionProgress(activeDocumentId);
}

function renderRecommendations() {
  overviewRecommendationsList.innerHTML = "";
  if (!recommendations.length) {
    const empty = document.createElement("div"); empty.className = "empty-state";
    if (dashboardData && !(dashboardData.metrics?.documents || 0)) {
      empty.textContent = "Add learning material to receive grounded next actions.";
      const action = document.createElement("button"); action.type = "button"; action.className = "text-button"; action.textContent = "Open materials";
      action.addEventListener("click", () => setPage("materials"));
      overviewRecommendationsList.append(empty, action);
    } else {
      empty.textContent = "No priority learning actions right now.";
      overviewRecommendationsList.appendChild(empty);
    }
    return;
  }
  recommendations.slice(0, RECOMMENDATIONS_OVERVIEW_LIMIT).forEach((recommendation) => {
    const row = document.createElement("article"); row.className = `recommendation-row priority-${recommendation.priority}`;
    const copy = document.createElement("div");
    const badge = document.createElement("span"); badge.className = "recommendation-priority";
    badge.textContent = recommendation.priority === "high" ? "High" : recommendation.priority === "moderate" ? "Moderate" : recommendation.priority === "needs_more_evidence" ? "Needs more evidence" : "Not assessed";
    const title = document.createElement("strong"); title.textContent = recommendation.topic_name;
    const metrics = document.createElement("span"); metrics.textContent = `${recommendation.document_name} · Mastery ${Math.round(recommendation.mastery_score)}% · Coverage ${recommendation.distinct_concepts_assessed}/${recommendation.assessment_capacity}`;
    const reason = document.createElement("small"); reason.textContent = recommendation.reason_text;
    copy.append(badge, title, metrics, reason);
    const action = document.createElement("button"); action.type = "button"; action.className = "secondary-button"; action.textContent = recommendation.primary_action.label;
    action.addEventListener("click", () => openPracticeContext(recommendation.document_id, recommendation.topic_id));
    row.append(copy, action); overviewRecommendationsList.appendChild(row);
  });
}

async function loadRecommendations() {
  try { recommendations = await fetchJson(RECOMMENDATIONS_API_URL); }
  catch (error) { recommendations = []; }
  renderRecommendations();
  if (activeDocumentId) renderSessionProgress(activeDocumentId);
}

function selectedAssessmentScope() {
  return quizScopeSelect?.value || "topic";
}

function currentQuizKey() {
  return `${quizDocumentSelect?.value || ""}::${selectedTopicId()}::${selectedDifficulty()}`;
}

function updateDifficultyOptions() {
  if (!quizDifficultySelect) {
    return;
  }
  const savedLevels = (getSelectedQuizStatus()?.variants || [])
    .filter((variant) => variant.topic_id === selectedTopicId())
    .map((variant) => variant.difficulty);
  Array.from(quizDifficultySelect.options).forEach((option) => {
    const label = option.value.charAt(0).toUpperCase() + option.value.slice(1);
    option.textContent = savedLevels.includes(option.value) ? `${label} (saved)` : label;
  });
}

function formatQuizStatus(status) {
  const levelIsSaved = (status?.variants || []).some(
    (variant) => variant.topic_id === selectedTopicId() && variant.difficulty === selectedDifficulty()
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

    updateTopicOptions();
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

function updateTopicOptions() {
  if (!quizTopicSelect) return;
  const selectedDocument = indexedDocuments.find((item) => item.id === quizDocumentSelect?.value);
  const topics = selectedDocument?.topics || [];
  quizTopicSelect.innerHTML = "";
  if (!topics.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No extracted topics available";
    quizTopicSelect.appendChild(option);
    return;
  }
  topics.forEach((topic) => {
    const option = document.createElement("option");
    option.value = topic.topic_id;
    option.textContent = topic.name;
    quizTopicSelect.appendChild(option);
  });
}

function updateAssessmentScope() {
  const topicMode = selectedAssessmentScope() === "topic";
  if (quizTopicField) quizTopicField.hidden = !topicMode;
}

async function handleQuizDocumentChange() {
  updateTopicOptions();
  updateAssessmentScope();
  await loadSelectedQuiz();
}

function setAssessmentLoading(isLoading) {
  assessmentLoading.classList.toggle("show", isLoading);
  generateQuizButton.disabled = isLoading;
  newQuizButton.disabled = isLoading;
  resetQuizButton.disabled = isLoading;
}

async function requestGeneratedQuiz() {
  const response = await fetch(QUIZ_GENERATE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      document_id: quizDocumentSelect.value,
      assessment_scope: selectedAssessmentScope(),
      topic_id: selectedAssessmentScope() === "topic" ? selectedTopicId() : null,
      difficulty: selectedDifficulty(),
      question_count: selectedQuestionCount(),
      model_id: selectedModelId || null
    })
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

async function requestQuizDetail(documentId) {
  const query = new URLSearchParams({ difficulty: selectedDifficulty(), topic_id: selectedTopicId() });
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

async function requestQuizRegeneration(documentId) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(documentId)}/regenerate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      difficulty: selectedDifficulty(),
      assessment_scope: selectedAssessmentScope(),
      topic_id: selectedAssessmentScope() === "topic" ? selectedTopicId() : null,
      question_count: currentQuiz?.assessment_plan?.target_questions || selectedQuestionCount(),
      model_id: selectedModelId || null
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

async function requestQuizProgress(questionId, selectedAnswer) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/progress`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      difficulty: currentQuiz.difficulty,
      topic_id: currentQuiz.topic_id,
      question_id: questionId,
      selected_answer: selectedAnswer
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

async function requestQuizSubmission() {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      difficulty: currentQuiz.difficulty,
      topic_id: currentQuiz.topic_id,
      quiz_id: currentQuiz.quiz_id,
      answers: quizAnswers
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
  const response = await fetch(
    `${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/progress?${query}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    throw new Error(`Reset progress API returned ${response.status}`);
  }
  return response.json();
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

async function loadQuizHistory() {
  try {
    const response = await fetch(QUIZ_HISTORY_API_URL);
    if (!response.ok) {
      throw new Error(`Quiz history API returned ${response.status}`);
    }
    quizHistory = await response.json();
  } catch (error) {
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
    const scope = makeFilter("Scope", [["all", "All"], ["topic", "Topic"], ["document", "Entire Document"]], (value) => {
      quizHistoryScopeFilter = value;
      renderQuizHistory();
    });
    filters.append(difficulty.label, scope.label);
    quizHistoryList.before(filters);
  }
  const filterSelects = filters.querySelectorAll("select");
  filterSelects[0].value = quizHistoryDifficultyFilter;
  filterSelects[1].value = quizHistoryScopeFilter;

  const visibleGroups = groups.filter(({ latest }) => {
    const scope = latest.topic_id === "document" ? "document" : "topic";
    return (quizHistoryDifficultyFilter === "all" || latest.difficulty === quizHistoryDifficultyFilter)
      && (quizHistoryScopeFilter === "all" || scope === quizHistoryScopeFilter);
  });
  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No completed quizzes yet.";
    quizHistoryList.appendChild(empty);
    return;
  }
  if (!visibleGroups.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No quizzes match these filters.";
    quizHistoryList.appendChild(empty);
    return;
  }

  visibleGroups.forEach((group) => {
    const attempt = group.latest;
    const card = document.createElement("article");
    card.className = "quiz-history-card";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    const scopeName = attempt.topic_id === "document" ? "Entire Document" : (attempt.topic_name || attempt.topic_id || "Topic");
    title.textContent = `${scopeName} · ${attempt.difficulty}`;
    const date = document.createElement("small");
    date.textContent = attempt.completed_at ? `Latest activity ${new Date(attempt.completed_at).toLocaleString()}` : "Activity time unavailable";
    const scope = document.createElement("span");
    scope.className = "quiz-history-scope";
    scope.textContent = `${attempt.total} questions · ${group.attempts.length} attempt${group.attempts.length === 1 ? "" : "s"}`;
    info.append(title, scope, date);

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
    review.addEventListener("click", () => showQuizHistoryDetail(attempt.attempt_id));
    const regenerate = document.createElement("button");
    regenerate.className = "text-button";
    regenerate.type = "button";
    regenerate.textContent = "Regenerate Quiz";
    regenerate.addEventListener("click", () => regenerateHistoryQuiz(attempt));
    actions.append(retake, review, regenerate);
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
      row.addEventListener("click", () => showQuizHistoryDetail(pastAttempt.attempt_id));
      history.appendChild(row);
    });
    card.append(info, score, actions, history);
    quizHistoryList.appendChild(card);
  });
}

async function showQuizHistoryDetail(attemptId) {
  quizHistoryDetail.hidden = false;
  quizHistoryDetail.textContent = "Loading attempt...";
  try {
    const attempt = await requestQuizHistoryDetail(attemptId);
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
        if (letter === result.correct_answer) row.classList.add("correct");
        if (letter === result.selected_answer) row.classList.add("selected");
        if (letter === result.selected_answer && !result.is_correct) row.classList.add("incorrect");
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

async function loadSelectedQuiz() {
  const documentId = quizDocumentSelect?.value;
  const requestedQuizKey = currentQuizKey();
  updateDifficultyOptions();
  if (!documentId || !selectedTopicId()) {
    currentQuiz = null;
    currentAttempt = null;
    quizAttemptSummary = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    renderAssessmentQuiz();
    return;
  }

  try {
    const detail = await requestQuizDetail(documentId);
    if (requestedQuizKey !== currentQuizKey()) {
      return;
    }
    currentQuiz = detail.quiz || null;
    currentAttempt = detail.latest_attempt || null;
    quizAttemptSummary = detail.attempt_summary || null;
    quizExplanations = {};
    quizAnswers = currentAttempt?.completed && currentAttempt.answers ? { ...currentAttempt.answers } : {};
    const firstUnansweredIndex = currentQuiz?.questions?.findIndex(
      (question) => !quizAnswers[String(question.id)]
    );
    quizQuestionIndex = firstUnansweredIndex >= 0 ? firstUnansweredIndex : 0;
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

async function generateAssessmentQuiz() {
  if (!quizDocumentSelect.value) {
    showToast("Choose an indexed document first");
    return;
  }
  if (selectedAssessmentScope() === "topic" && !selectedTopicId()) {
    showToast("Choose an extracted topic first");
    return;
  }

  if (currentQuiz?.questions?.length) {
    renderAssessmentQuiz();
    showToast(currentAttempt?.completed ? "Reviewing saved quiz" : "Quiz ready");
    return;
  }

  setAssessmentLoading(true);
  quizList.innerHTML = "";
  assessmentTitle.textContent = "Generating assessment";

  try {
    currentQuiz = await requestGeneratedQuiz();
    currentAttempt = null;
    quizAttemptSummary = null;
    quizAnswers = {};
    quizExplanations = {};
    quizQuestionIndex = 0;
    await loadQuizStatuses();
    updateDifficultyOptions();
    closeQuizCreateDialog();
    renderAssessmentQuiz();
    showToast(currentQuiz.assessment_plan?.partial
      ? `Quiz ready with ${currentQuiz.questions.length}/${currentQuiz.assessment_plan?.target_questions || selectedQuestionCount()} grounded questions`
      : "Quiz ready");
  } catch (error) {
    currentQuiz = null;
    quizAnswers = {};
    quizList.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "Assessment Agent could not generate a quiz.";
    quizList.appendChild(empty);
    assessmentTitle.textContent = "Assessment Agent";
    showToast(error.message || "Quiz generation failed");
  } finally {
    setAssessmentLoading(false);
    updateAssessmentSummary();
  }
}

async function regenerateAssessmentQuiz() {
  if (!quizDocumentSelect.value) {
    showToast("Choose an indexed document first");
    return;
  }
  if (!selectedTopicId()) {
    showToast("Choose an extracted topic first");
    return;
  }

  setAssessmentLoading(true);
  quizList.innerHTML = "";
  assessmentTitle.textContent = "Regenerating assessment";

  try {
    currentQuiz = await requestQuizRegeneration(quizDocumentSelect.value);
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
  }
}

function renderPracticeMastery() {
  if (!practiceMasteryPanel || !practiceMasteryList) return;
  if (!currentAttempt?.completed) {
    practiceMasteryPanel.hidden = true;
    practiceMasteryList.innerHTML = "";
    return;
  }
  const topicNames = Object.fromEntries((currentQuiz?.questions || []).map((question) => [question.topic_id, question.topic_name || question.topic_id]));
  let masteries = Object.values(currentAttempt.mastery_by_topic || {});
  if (!masteries.length && currentAttempt.mastery) masteries = [currentAttempt.mastery];
  if (!masteries.length && dashboardData) {
    const represented = new Set((currentQuiz?.questions || []).map((question) => question.topic_id));
    masteries = (dashboardData.mastery || []).filter((mastery) => mastery.document_id === currentQuiz.document_id && represented.has(mastery.topic_id));
  }
  masteries = masteries.map((mastery) => ({ ...mastery, topic_name: mastery.topic_name || topicNames[mastery.topic_id] || mastery.topic_id }));
  practiceMasteryPanel.hidden = !masteries.length;
  renderMasteryList(practiceMasteryList, masteries, { emptyText: "Mastery evidence is not available for this quiz." });
}

async function selectHistoryQuizVariant(attempt) {
  const topicId = attempt.topic_id || "document";
  quizDocumentSelect.value = attempt.document_id;
  updateTopicOptions();
  quizDifficultySelect.value = attempt.difficulty || "easy";
  if (topicId === "document") {
    quizScopeSelect.value = "document";
  } else {
    quizScopeSelect.value = "topic";
    if ([...quizTopicSelect.options].some((option) => option.value === topicId)) quizTopicSelect.value = topicId;
  }
  updateAssessmentScope();
  await loadSelectedQuiz();
  if (!currentQuiz?.questions?.length) throw new Error("The saved quiz is no longer available. Regenerate it to create new questions.");
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
    renderPracticeMastery();
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
  renderPracticeMastery();
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

async function selectAssessmentAnswer(question, option, card) {
  if (currentAttempt?.question_results?.some((result) => result.question_id === question.id)) {
    return;
  }

  const selectedLetter = option.trim().charAt(0).toUpperCase();
  quizAnswers[String(question.id)] = selectedLetter;

  const buttons = card.querySelectorAll(".answer-option");
  buttons.forEach((button) => {
    const buttonLetter = button.textContent.trim().charAt(0).toUpperCase();
    button.disabled = true;
    button.classList.toggle("correct", buttonLetter === question.correct_answer);
    button.classList.toggle("incorrect", buttonLetter === selectedLetter && selectedLetter !== question.correct_answer);
    button.classList.toggle("selected", buttonLetter === selectedLetter);
  });

  const feedback = card.querySelector(".feedback");
  const isCorrect = selectedLetter === question.correct_answer;
  feedback.className = `feedback ${isCorrect ? "good" : "bad"}`;
  feedback.textContent = isCorrect ? "Correct." : "Incorrect.";
  card.querySelector(".explain-button").hidden = false;
  updateAssessmentSummary();

  try {
    currentAttempt = await requestQuizProgress(question.id, selectedLetter);
    quizAnswers = { ...currentAttempt.answers };
    renderAssessmentQuiz();
    if (currentAttempt.completed) {
      await loadQuizHistory();
      await loadDashboard();
      renderPracticeMastery();
      showToast(`Quiz completed: ${currentAttempt.score}/${currentAttempt.total}`);
    }
  } catch (error) {
    delete quizAnswers[String(question.id)];
    renderAssessmentQuiz();
    showToast(error.message || "Could not save answer");
  }
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

function createAssessmentReviewCard(question, result, index) {
  const card = document.createElement("article");
  card.className = `quiz-question-card quiz-review-card ${result.is_correct ? "correct" : "incorrect"}`;
  const heading = document.createElement("div");
  heading.className = "quiz-question-heading";
  const number = document.createElement("span");
  number.textContent = `Question ${index + 1} of ${currentQuiz.questions.length}`;
  const outcome = document.createElement("small");
  outcome.textContent = result.is_correct ? "Correct" : "Incorrect";
  heading.append(number, outcome);
  const questionText = document.createElement("h3");
  questionText.textContent = question.question;
  const options = document.createElement("div");
  options.className = "review-answer-list";
  question.options.forEach((option) => {
    const letter = option.trim().charAt(0).toUpperCase();
    const row = document.createElement("div");
    row.className = "review-answer-option";
    if (letter === result.correct_answer) row.classList.add("correct");
    if (letter === result.selected_answer) row.classList.add("selected");
    if (letter === result.selected_answer && !result.is_correct) row.classList.add("incorrect");
    row.textContent = option;
    options.appendChild(row);
  });
  const explanation = document.createElement("div");
  explanation.className = "quiz-explanation";
  explanation.textContent = result.explanation || question.explanation || "Explanation unavailable.";
  card.append(heading, questionText, options, explanation);
  return card;
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

function renderAssessmentQuiz() {
  const quizPane = document.querySelector('[data-session-pane="quiz"]');
  const hasQuiz = Boolean(currentQuiz?.questions?.length);
  quizPane?.classList.toggle("quiz-active", hasQuiz);
  quizPane?.classList.toggle("quiz-landing", !hasQuiz);
  updateQuizLandingLayout();
  quizList.innerHTML = "";
  assessmentTitle.textContent = hasQuiz
    ? `${currentQuiz.questions.length} ${currentQuiz.difficulty} questions from ${currentQuiz.document_id}`
    : "Assessment Agent";
  if (!hasQuiz) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Choose a document, then generate or start its saved quiz.";
    quizList.appendChild(empty);
    renderPracticeMastery();
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
    renderPracticeMastery();
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
  const options = document.createElement("div");
  options.className = "answer-list";
  question.options.forEach((option) => {
    const button = document.createElement("button");
    button.className = "answer-option";
    button.type = "button";
    button.textContent = option;
    const letter = option.trim().charAt(0).toUpperCase();
    button.classList.toggle("selected", quizAnswers[String(question.id)] === letter);
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
  card.append(heading, questionText, options, navigator, navigation);
  quizList.appendChild(card);
  updateAssessmentSummary();
  renderPracticeMastery();
}

function moveQuizQuestionLegacy(direction) {
  quizQuestionIndex = Math.max(0, Math.min(currentQuiz.questions.length - 1, quizQuestionIndex + direction));
  renderAssessmentQuiz();
}

function selectAssessmentAnswer(question, option) {
  if (currentAttempt?.completed) return;
  quizAnswers[String(question.id)] = option.trim().charAt(0).toUpperCase();
  renderAssessmentQuiz();
}

async function submitAssessmentQuiz(event) {
  const button = event?.currentTarget;
  if (Object.keys(quizAnswers).length !== currentQuiz.questions.length) return;
  if (button) { button.disabled = true; button.textContent = "Checking..."; }
  try {
    currentAttempt = await requestQuizSubmission();
    quizAttemptSummary = currentAttempt.attempt_summary;
    await loadQuizHistory();
    await loadDashboard();
    renderAssessmentQuiz();
    showToast(`Attempt ${currentAttempt.attempt_number}: ${currentAttempt.score}/${currentAttempt.total}`);
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

document.getElementById("session-home-button")?.addEventListener("click", () => setPage("overview"));
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
newQuizButton.addEventListener("click", regenerateAssessmentQuiz);
resetQuizButton.addEventListener("click", resetAssessmentQuiz);
reviewQuizButton.addEventListener("click", () => {
  if (currentAttempt?.completed) renderAssessmentQuiz();
});
backToQuizzesButton.addEventListener("click", backToQuizzes);
quizDocumentSelect.addEventListener("change", handleQuizDocumentChange);
quizTopicSelect.addEventListener("change", loadSelectedQuiz);
quizScopeSelect.addEventListener("change", async () => {
  updateAssessmentScope();
  await loadSelectedQuiz();
});

authForm.addEventListener("submit", handleAuthentication);
authSwitch.addEventListener("click", () => setAuthMode(authMode === "login" ? "signup" : "login"));
logoutButton.addEventListener("click", signOut);
quizDifficultySelect.addEventListener("change", handleQuizDifficultyChange);
refreshQuizHistoryButton.addEventListener("click", loadQuizHistory);
document.getElementById("overview-practice-action")?.addEventListener("click", () => setPage("practice"));
document.getElementById("overview-materials-action")?.addEventListener("click", () => setPage("materials"));
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
  let header = pane.querySelector(".quiz-landing-header");
  if (!header) {
    header = document.createElement("div");
    header.className = "quiz-landing-header";
    header.innerHTML = '<div><h2>Quiz</h2><p>Test your understanding of this material.</p></div>';
    const create = document.createElement("button");
    create.className = "primary-button";
    create.type = "button";
    create.textContent = "+ Create Quiz";
    create.addEventListener("click", openQuizCreateDialog);
    header.appendChild(create);
    pane.insertBefore(header, pane.firstChild);
  }
  header.hidden = Boolean(currentQuiz?.questions?.length);
  const historyTitle = pane.querySelector(".quiz-history-panel h2");
  if (historyTitle) historyTitle.textContent = "Your Quizzes";
  const createDialogOpen = Boolean(quizCreateDialog?.classList.contains("open"));
  if (assessmentControl) {
    assessmentControl.hidden = !currentQuiz?.questions?.length && !createDialogOpen;
  }
}

function openQuizCreateDialog() {
  if (!assessmentControl) return;
  if (!quizQuestionCountSelect) {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = "Number of questions";
    quizQuestionCountSelect = document.createElement("select");
    quizQuestionCountSelect.id = "quiz-question-count-select";
    [10, 15, 20, 25].forEach((count) => {
      const option = document.createElement("option");
      option.value = String(count);
      option.textContent = String(count);
      option.selected = count === 10;
      quizQuestionCountSelect.appendChild(option);
    });
    label.append(caption, quizQuestionCountSelect);
    generateQuizButton.before(label);
  }
  if (!quizCreateDialog) {
    quizCreateDialog = document.createElement("div");
    quizCreateDialog.className = "quiz-create-dialog";
    quizCreateDialog.innerHTML = '<div class="quiz-create-dialog-card" role="dialog" aria-modal="true" aria-labelledby="quiz-create-title"><div class="quiz-dialog-heading"><div><h2 id="quiz-create-title">Create a Quiz</h2><p>Choose how you want to assess this material.</p></div><button class="text-button quiz-dialog-cancel" type="button">Cancel</button></div><div class="quiz-dialog-fields"></div></div>';
    quizCreateDialog.querySelector(".quiz-dialog-cancel").addEventListener("click", closeQuizCreateDialog);
    quizCreateDialog.addEventListener("click", (event) => { if (event.target === quizCreateDialog) closeQuizCreateDialog(); });
    document.body.appendChild(quizCreateDialog);
  }
  quizCreateDialog.classList.add("open");
  quizCreateDialog.querySelector(".quiz-dialog-fields").appendChild(assessmentControl);
  assessmentControl.hidden = false;
}

function closeQuizCreateDialog() {
  if (!quizCreateDialog || !assessmentControl) return;
  const shell = document.querySelector('[data-session-pane="quiz"] .assessment-shell');
  shell?.insertBefore(assessmentControl, shell.firstChild);
  assessmentControl.hidden = true;
  quizCreateDialog.classList.remove("open");
}

async function initializeApplication() {
  await initializeChatWorkspace();
  await Promise.all([loadIndexedDocuments(), loadQuizHistory(), loadDashboard(), loadKnowledgeGaps(), loadRecommendations(), loadGenerationModels()]);
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
