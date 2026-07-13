const pageTitles = {
  overview: "Good afternoon, Thuan",
  tutor: "Learn with your AI tutor",
  materials: "Course Materials",
  practice: "Practice",
  plan: "Study Plan"
};

const CONVERSATIONS_API_URL = "http://127.0.0.1:8000/api/conversations";
const SOURCES_API_URL = "http://127.0.0.1:8000/api/sources";
const UPLOAD_API_URL = "http://127.0.0.1:8000/api/sources/upload";
const DELETE_SOURCE_API_URL = "http://127.0.0.1:8000/api/sources";
const DOCUMENTS_API_URL = "http://127.0.0.1:8000/api/documents";
const QUIZZES_API_URL = "http://127.0.0.1:8000/api/quizzes";
const QUIZ_API_BASE_URL = "http://127.0.0.1:8000/api/quiz";
const QUIZ_GENERATE_API_URL = "http://127.0.0.1:8000/api/quiz/generate";
const QUIZ_HISTORY_API_URL = "http://127.0.0.1:8000/api/quiz-history";

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
let quizExplanations = {};
let quizHistory = [];
let conversations = [];
let activeConversation = null;

const navItems = document.querySelectorAll(".nav-item");
const views = document.querySelectorAll(".view");
const pageTitle = document.getElementById("page-title");
const resetButton = document.getElementById("reset-button");
const toast = document.getElementById("toast");
const messageList = document.getElementById("message-list");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const confidenceLabel = document.getElementById("confidence-label");
const confidenceBar = document.getElementById("confidence-bar");
const confidencePill = document.getElementById("confidence-pill");
const sourceList = document.getElementById("source-list");
const sourceFileInput = document.getElementById("source-file-input");
const uploadSourceButton = document.getElementById("upload-source-button");
const uploadStatus = document.getElementById("upload-status");
const quizDocumentSelect = document.getElementById("quiz-document-select");
const quizQuestionCountSelect = document.getElementById("quiz-question-count-select");
const quizDifficultySelect = document.getElementById("quiz-difficulty-select");
const generateQuizButton = document.getElementById("generate-quiz-button");
const resetQuizButton = document.getElementById("reset-quiz-button");
const newQuizButton = document.getElementById("new-quiz-button");
const selectedDocumentLabel = document.getElementById("selected-document-label");
const quizStatusLabel = document.getElementById("quiz-status-label");
const quizQuestionCountLabel = document.getElementById("quiz-question-count-label");
const quizProgressLabel = document.getElementById("quiz-progress-label");
const quizAccuracyLabel = document.getElementById("quiz-accuracy-label");
const assessmentLoading = document.getElementById("assessment-loading");
const quizList = document.getElementById("quiz-list");
const assessmentTitle = document.getElementById("assessment-title");
const quizHistoryList = document.getElementById("quiz-history-list");
const quizHistoryDetail = document.getElementById("quiz-history-detail");
const refreshQuizHistoryButton = document.getElementById("refresh-quiz-history-button");
const conversationList = document.getElementById("conversation-list");
const newConversationButton = document.getElementById("new-conversation-button");
const conversationSourceList = document.getElementById("conversation-source-list");
const applyConversationSourcesButton = document.getElementById("apply-conversation-sources-button");
const chatConversationTitle = document.getElementById("chat-conversation-title");
const chatSourceSummary = document.getElementById("chat-source-summary");
const tutorLayout = document.querySelector(".tutor-layout");
const toggleConversationSourcesButton = document.getElementById("toggle-conversation-sources-button");
const closeConversationSourcesButton = document.getElementById("close-conversation-sources-button");
const sourcesDrawerBackdrop = document.getElementById("sources-drawer-backdrop");
const conversationSourcesPanel = document.getElementById("conversation-sources-panel");

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2400);
}

function setPage(page) {
  if (page !== "tutor") closeSourcesDrawer();
  state.page = page;
  document.body.dataset.page = page;
  navItems.forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  views.forEach((view) => view.classList.toggle("active", view.id === `${page}-view`));
  pageTitle.textContent = pageTitles[page];
  showToast(`Opened ${pageTitles[page]}`);
}

function setSourcesDrawerOpen(isOpen) {
  tutorLayout.classList.toggle("sources-open", isOpen);
  toggleConversationSourcesButton.setAttribute("aria-expanded", String(isOpen));
  conversationSourcesPanel.setAttribute("aria-hidden", String(!isOpen));
  if (isOpen) {
    renderConversationSources();
    closeConversationSourcesButton.focus();
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
  citations.forEach((citation) => {
    const cite = document.createElement("span");
    const title = citation.title || citation.document_id || "Unknown source";
    cite.textContent = `${title} · p.${citation.page || "?"}`;
    meta.appendChild(cite);
  });
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
  if (!activeConversation?.id) {
    throw new Error("Create or select a conversation first.");
  }
  const response = await fetch(`${CONVERSATIONS_API_URL}/${activeConversation.id}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      message: userText
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
  uploadedSources.forEach((source) => {
    const label = document.createElement("label");
    label.className = "conversation-source-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = source.title;
    checkbox.checked = selected.has(source.title);
    const text = document.createElement("span");
    text.innerHTML = `<strong></strong><small></small>`;
    text.querySelector("strong").textContent = source.title;
    text.querySelector("small").textContent = `${source.chunks} chunks`;
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
  chatConversationTitle.textContent = activeConversation?.title || "New conversation";
  const count = activeConversation?.document_ids?.length || 0;
  chatSourceSummary.textContent = count ? `Grounded in ${count} selected document(s)` : "No source selected";
  toggleConversationSourcesButton.textContent = count ? `Sources (${count})` : "Choose sources";
}

async function openConversation(conversationId) {
  try {
    activeConversation = await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`);
    localStorage.setItem("activeConversationId", conversationId);
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
    const documentIds = uploadedSources.map((source) => source.title);
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
    await createChatConversation();
    return;
  }
  const savedId = localStorage.getItem("activeConversationId");
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
  chatForm.querySelector("button[type='submit']").disabled = true;
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
    addMessage(`I could not reach the RAG answer right now.\n\n${detail}\n\nCheck that FastAPI is running with the project virtual environment and Ollama is still running.`, "tutor");
    showToast("RAG request failed");
  } finally {
    chatInput.disabled = false;
    chatForm.querySelector("button[type='submit']").disabled = false;
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
  const selectedDocument = quizDocumentSelect?.selectedOptions[0]?.textContent || "None";
  const total = currentQuiz?.questions?.length || 0;
  const answered = Object.values(quizAnswers).filter(Boolean).length;
  const score = currentAttempt?.score ?? 0;
  const attemptTotal = currentAttempt?.total ?? total;
  const accuracy = answered ? Math.round((score / answered) * 100) : 0;
  const status = getSelectedQuizStatus();

  selectedDocumentLabel.textContent = selectedDocument;
  quizStatusLabel.textContent = formatQuizStatus(status);
  const requestedCount = Number(quizQuestionCountSelect?.value || 10);
  quizQuestionCountLabel.textContent = total ? `${total} multiple-choice questions` : `${requestedCount}-question assessment`;
  quizProgressLabel.textContent = `${answered} / ${total}`;
  quizAccuracyLabel.textContent = answered ? `Score ${score}/${answered} · Accuracy ${accuracy}%` : "Not started";

  const hasQuiz = Boolean(currentQuiz?.questions?.length);
  if (generateQuizButton) {
    generateQuizButton.textContent = hasQuiz ? (currentAttempt?.completed ? "Review Quiz" : "Start Quiz") : "Generate Quiz";
  }
  if (resetQuizButton) {
    resetQuizButton.disabled = !hasQuiz;
  }
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

function currentQuizKey() {
  return `${quizDocumentSelect?.value || ""}::${selectedDifficulty()}`;
}

function updateDifficultyOptions() {
  if (!quizDifficultySelect) {
    return;
  }
  const savedLevels = getSelectedQuizStatus()?.available_difficulties || [];
  Array.from(quizDifficultySelect.options).forEach((option) => {
    const label = option.value.charAt(0).toUpperCase() + option.value.slice(1);
    option.textContent = savedLevels.includes(option.value) ? `${label} (saved)` : label;
  });
}

function formatQuizStatus(status) {
  const levelIsSaved = status?.available_difficulties?.includes(selectedDifficulty());
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
      const levels = status?.available_difficulties || [];
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
      question_count: Number(quizQuestionCountSelect.value),
      difficulty: selectedDifficulty()
    })
  });

  if (!response.ok) {
    let detail = `Quiz API returned ${response.status}`;
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

async function requestQuizDetail(documentId) {
  const query = new URLSearchParams({ difficulty: selectedDifficulty() });
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
      question_count: Number(quizQuestionCountSelect.value),
      difficulty: selectedDifficulty()
    })
  });
  if (!response.ok) {
    let detail = `Regenerate API returned ${response.status}`;
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

async function requestQuizProgress(questionId, selectedAnswer) {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/progress`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      difficulty: currentQuiz.difficulty,
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

async function requestQuizProgressReset() {
  const query = new URLSearchParams({ difficulty: currentQuiz.difficulty });
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
        difficulty: currentQuiz.difficulty
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

function renderQuizHistory() {
  if (!quizHistoryList) {
    return;
  }
  quizHistoryList.innerHTML = "";
  if (!quizHistory.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No completed quizzes yet.";
    quizHistoryList.appendChild(empty);
    return;
  }

  quizHistory.forEach((attempt) => {
    const card = document.createElement("article");
    card.className = "quiz-history-card";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = `${attempt.document_id} · ${attempt.difficulty}`;
    const date = document.createElement("small");
    date.textContent = attempt.completed_at
      ? new Date(attempt.completed_at).toLocaleString()
      : "Completion time unavailable";
    info.append(title, date);

    const score = document.createElement("div");
    score.className = "quiz-history-score";
    score.innerHTML = `<strong>${attempt.score}/${attempt.total}</strong><span>${attempt.percentage}%</span>`;
    const review = document.createElement("button");
    review.className = "text-button";
    review.type = "button";
    review.textContent = "Review";
    review.addEventListener("click", () => showQuizHistoryDetail(attempt.attempt_id));
    card.append(info, score, review);
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
    const close = document.createElement("button");
    close.className = "text-button";
    close.type = "button";
    close.textContent = "Close Review";
    close.addEventListener("click", () => { quizHistoryDetail.hidden = true; });
    heading.append(title, close);
    quizHistoryDetail.appendChild(heading);

    (attempt.question_results || []).forEach((result) => {
      const item = document.createElement("article");
      item.className = `quiz-history-question ${result.is_correct ? "correct" : "incorrect"}`;
      const question = document.createElement("strong");
      question.textContent = result.question || `Question ${result.question_id}`;
      const answer = document.createElement("p");
      answer.textContent = `Your answer: ${result.selected_answer} · Correct answer: ${result.correct_answer}`;
      item.append(question, answer);
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
  if (!documentId) {
    currentQuiz = null;
    currentAttempt = null;
    quizAnswers = {};
    quizExplanations = {};
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
    quizExplanations = {};
    if (currentQuiz?.question_count && quizQuestionCountSelect) {
      quizQuestionCountSelect.value = String(currentQuiz.question_count);
    }
    quizAnswers = currentAttempt?.answers ? { ...currentAttempt.answers } : {};
    renderAssessmentQuiz();
  } catch (error) {
    currentQuiz = null;
    currentAttempt = null;
    quizAnswers = {};
    quizExplanations = {};
    renderAssessmentQuiz();
  }
}

async function handleQuizDifficultyChange() {
  currentQuiz = null;
  currentAttempt = null;
  quizAnswers = {};
  quizExplanations = {};
  renderAssessmentQuiz();
  await loadSelectedQuiz();
}

async function generateAssessmentQuiz() {
  if (!quizDocumentSelect.value) {
    showToast("Choose an indexed document first");
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
    quizAnswers = {};
    quizExplanations = {};
    await loadQuizStatuses();
    updateDifficultyOptions();
    renderAssessmentQuiz();
    showToast("Quiz ready");
  } catch (error) {
    currentQuiz = null;
    quizAnswers = {};
    quizList.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "Assessment Agent could not generate a quiz.";
    quizList.appendChild(empty);
    assessmentTitle.textContent = "Assessment Agent";
    showToast("Quiz generation failed");
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

  setAssessmentLoading(true);
  quizList.innerHTML = "";
  assessmentTitle.textContent = "Regenerating assessment";

  try {
    currentQuiz = await requestQuizRegeneration(quizDocumentSelect.value);
    currentAttempt = null;
    quizAnswers = {};
    quizExplanations = {};
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

function renderAssessmentQuiz() {
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

  currentQuiz.questions.forEach((question) => {
    const card = document.createElement("article");
    card.className = "quiz-question-card";
    card.dataset.questionId = question.id;

    const heading = document.createElement("div");
    heading.className = "quiz-question-heading";

    const number = document.createElement("span");
    number.textContent = `Question ${question.id}`;
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
      feedback.textContent = `${questionResult.is_correct ? "Correct." : "Incorrect."} Correct answer: ${questionResult.correct_answer}.`;
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

    card.append(heading, questionText, options, feedback, explainButton, explanation);
    quizList.appendChild(card);
  });

  updateAssessmentSummary();
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
  feedback.textContent = isCorrect ? "Correct." : `Incorrect. Correct answer: ${question.correct_answer}.`;
  card.querySelector(".explain-button").hidden = false;
  updateAssessmentSummary();

  try {
    currentAttempt = await requestQuizProgress(question.id, selectedLetter);
    quizAnswers = { ...currentAttempt.answers };
    renderAssessmentQuiz();
    if (currentAttempt.completed) {
      await loadQuizHistory();
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
  renderAssessmentQuiz();
  setPage(initialState.page);
  showToast("Tutoring reset");
}

navItems.forEach((item) => {
  item.addEventListener("click", () => setPage(item.dataset.page));
});

document.querySelectorAll("[data-page-target]").forEach((button) => {
  button.addEventListener("click", () => setPage(button.dataset.pageTarget));
});

chatForm.addEventListener("submit", handleChatSubmit);
resetButton.addEventListener("click", resetApp);
uploadSourceButton.addEventListener("click", () => sourceFileInput.click());
sourceFileInput.addEventListener("change", () => uploadSourceFiles(sourceFileInput.files));
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
quizDocumentSelect.addEventListener("change", loadSelectedQuiz);
quizQuestionCountSelect.addEventListener("change", updateAssessmentSummary);
quizDifficultySelect.addEventListener("change", handleQuizDifficultyChange);
refreshQuizHistoryButton.addEventListener("click", loadQuizHistory);

document.body.dataset.page = state.page;
updateConfidence(state.confidence);
renderAssessmentQuiz();
async function initializeChatWorkspace() {
  await loadUploadedSources();
  try {
    await loadConversations();
  } catch (error) {
    showToast(error.message || "Could not load conversations");
  }
}

initializeChatWorkspace();
loadIndexedDocuments();
loadQuizHistory();
