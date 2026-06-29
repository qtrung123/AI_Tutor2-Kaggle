const pageTitles = {
  overview: "Good afternoon, Thuan",
  tutor: "Learn with your AI tutor",
  practice: "Practice",
  plan: "Study Plan"
};

const CHAT_API_URL = "http://127.0.0.1:8000/api/chat";
const SOURCES_API_URL = "http://127.0.0.1:8000/api/sources";
const UPLOAD_API_URL = "http://127.0.0.1:8000/api/sources/upload";
const DELETE_SOURCE_API_URL = "http://127.0.0.1:8000/api/sources";
const DOCUMENTS_API_URL = "http://127.0.0.1:8000/api/documents";
const QUIZZES_API_URL = "http://127.0.0.1:8000/api/quizzes";
const QUIZ_API_BASE_URL = "http://127.0.0.1:8000/api/quiz";
const QUIZ_GENERATE_API_URL = "http://127.0.0.1:8000/api/quiz/generate";

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
const generateQuizButton = document.getElementById("generate-quiz-button");
const resetQuizButton = document.getElementById("reset-quiz-button");
const newQuizButton = document.getElementById("new-quiz-button");
const submitQuizButton = document.getElementById("submit-quiz-button");
const selectedDocumentLabel = document.getElementById("selected-document-label");
const quizStatusLabel = document.getElementById("quiz-status-label");
const quizQuestionCountLabel = document.getElementById("quiz-question-count-label");
const quizProgressLabel = document.getElementById("quiz-progress-label");
const quizAccuracyLabel = document.getElementById("quiz-accuracy-label");
const assessmentLoading = document.getElementById("assessment-loading");
const quizList = document.getElementById("quiz-list");
const assessmentTitle = document.getElementById("assessment-title");

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2400);
}

function setPage(page) {
  state.page = page;
  document.body.dataset.page = page;
  navItems.forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  views.forEach((view) => view.classList.toggle("active", view.id === `${page}-view`));
  pageTitle.textContent = pageTitles[page];
  showToast(`Opened ${pageTitles[page]}`);
}

function updateConfidence(value) {
  state.confidence = Math.max(0, Math.min(100, value));
  confidenceLabel.textContent = `${state.confidence}%`;
  confidenceBar.style.width = `${state.confidence}%`;
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
  const response = await fetch(CHAT_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      message: userText,
      course: "Net-centric Computing",
      topic: "Course materials"
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
    addMessage(formatBackendAnswer(data), "tutor");
    renderSources(data.citations || [], "citations");
  } catch (error) {
    loadingRow.remove();
    const detail = error.message || "Backend request failed.";
    addMessage(`I could not reach the RAG answer right now.\n\n${detail}\n\nCheck that FastAPI is running with the project virtual environment and Ollama is still running.`, "tutor");
    renderSources(
      [
        {
          sourceId: 1,
          title: "RAG request failed",
          page: "N/A",
          content: detail
        }
      ],
      "citations"
    );
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
  const accuracy = currentAttempt?.completed && attemptTotal ? Math.round((score / attemptTotal) * 100) : 0;
  const status = getSelectedQuizStatus();

  selectedDocumentLabel.textContent = selectedDocument;
  quizStatusLabel.textContent = formatQuizStatus(status);
  quizQuestionCountLabel.textContent = total ? `${total} questions: easy, medium, hard` : "Auto-sized assessment";
  quizProgressLabel.textContent = currentAttempt?.completed ? `${score} / ${attemptTotal}` : `${answered} / ${total}`;
  quizAccuracyLabel.textContent = currentAttempt?.completed ? `Accuracy ${accuracy}%` : "Not submitted";

  const hasQuiz = Boolean(currentQuiz?.questions?.length);
  if (generateQuizButton) {
    generateQuizButton.textContent = hasQuiz ? (currentAttempt?.completed ? "Review Quiz" : "Start Quiz") : "Generate Quiz";
  }
  if (submitQuizButton) {
    submitQuizButton.disabled = !hasQuiz || Boolean(currentAttempt?.completed) || answered !== total;
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

function formatQuizStatus(status) {
  if (!status?.has_quiz && !currentQuiz) {
    return "Not generated";
  }
  if (currentAttempt?.completed || status?.status === "completed") {
    return "Completed";
  }
  if (currentQuiz || status?.has_quiz) {
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
      const quizLabel = status?.has_quiz ? `quiz ready, ${status.question_count} questions` : "no quiz yet";
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
  submitQuizButton.disabled = isLoading;
}

async function requestGeneratedQuiz() {
  const response = await fetch(QUIZ_GENERATE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      document_id: quizDocumentSelect.value
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
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(documentId)}`);
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
    method: "POST"
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

async function requestQuizSubmit() {
  const response = await fetch(`${QUIZ_API_BASE_URL}/${encodeURIComponent(currentQuiz.document_id)}/submit`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      answers: quizAnswers
    })
  });

  if (!response.ok) {
    let detail = `Submit API returned ${response.status}`;
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

async function loadSelectedQuiz() {
  const documentId = quizDocumentSelect?.value;
  if (!documentId) {
    currentQuiz = null;
    currentAttempt = null;
    quizAnswers = {};
    renderAssessmentQuiz();
    return;
  }

  try {
    const detail = await requestQuizDetail(documentId);
    currentQuiz = detail.quiz || null;
    currentAttempt = detail.latest_attempt || null;
    quizAnswers = currentAttempt?.answers ? { ...currentAttempt.answers } : {};
    renderAssessmentQuiz();
  } catch (error) {
    currentQuiz = null;
    currentAttempt = null;
    quizAnswers = {};
    renderAssessmentQuiz();
  }
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
    await loadQuizStatuses();
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
    await loadQuizStatuses();
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
    ? `${currentQuiz.questions.length} questions from ${currentQuiz.document_id}`
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
    number.textContent = `Question ${question.id} · ${question.difficulty}`;

    const source = document.createElement("small");
    source.textContent = `${question.source.title}, page ${question.source.page}, chunk ${question.source.chunk ?? "N/A"}`;

    heading.append(number, source);

    const questionText = document.createElement("h3");
    questionText.textContent = question.question;

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
      if (currentAttempt?.completed) {
        button.disabled = true;
        if (optionLetter === question.correct_answer) {
          button.classList.add("correct");
        }
        if (selectedLetter === optionLetter && selectedLetter !== question.correct_answer) {
          button.classList.add("incorrect");
        }
      }
      button.addEventListener("click", () => selectAssessmentAnswer(question, option, card));
      options.appendChild(button);
    });

    const explanation = document.createElement("div");
    explanation.className = "feedback";
    if (currentAttempt?.completed) {
      const selectedLetter = quizAnswers[String(question.id)] || "";
      const isCorrect = selectedLetter === question.correct_answer;
      explanation.className = `feedback ${isCorrect ? "good" : "bad"}`;
      explanation.textContent = `${isCorrect ? "Correct." : "Not quite."} Correct answer: ${question.correct_answer}. ${question.explanation}`;
    }

    card.append(heading, questionText, options, explanation);
    quizList.appendChild(card);
  });

  updateAssessmentSummary();
}

function selectAssessmentAnswer(question, option, card) {
  if (currentAttempt?.completed) {
    return;
  }

  const selectedLetter = option.trim().charAt(0).toUpperCase();
  quizAnswers[String(question.id)] = selectedLetter;

  const buttons = card.querySelectorAll(".answer-option");
  buttons.forEach((button) => {
    button.classList.toggle("selected", button.textContent === option);
  });

  const explanation = card.querySelector(".feedback");
  explanation.className = "feedback";
  explanation.textContent = "";
  updateAssessmentSummary();
}

function resetAssessmentQuiz() {
  currentAttempt = null;
  quizAnswers = {};
  renderAssessmentQuiz();
}

async function submitAssessmentQuiz() {
  if (!currentQuiz?.questions?.length) {
    return;
  }

  const answered = Object.values(quizAnswers).filter(Boolean).length;
  if (answered !== currentQuiz.questions.length) {
    showToast("Answer every question before submitting");
    return;
  }

  setAssessmentLoading(true);
  try {
    const result = await requestQuizSubmit();
    currentAttempt = result.latest_attempt;
    quizAnswers = currentAttempt?.answers ? { ...currentAttempt.answers } : quizAnswers;
    await loadQuizStatuses();
    renderAssessmentQuiz();
    showToast(`Score saved: ${result.score}/${result.total}`);
  } catch (error) {
    showToast(error.message || "Submit failed");
  } finally {
    setAssessmentLoading(false);
    updateAssessmentSummary();
  }
}

function resetApp() {
  state.page = initialState.page;
  state.confidence = initialState.confidence;
  state.quizIndex = initialState.quizIndex;
  state.quizScore = initialState.quizScore;
  state.answered = initialState.answered;
  messageList.innerHTML = "";
  addMessage("Ask me about socket programming, Go language, data serialization, or application protocols. I will answer from your uploaded Net-centric materials.", "tutor");
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

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    chatInput.value = button.dataset.prompt;
    chatInput.focus();
  });
});

chatForm.addEventListener("submit", handleChatSubmit);
resetButton.addEventListener("click", resetApp);
uploadSourceButton.addEventListener("click", () => sourceFileInput.click());
sourceFileInput.addEventListener("change", () => uploadSourceFiles(sourceFileInput.files));
generateQuizButton.addEventListener("click", generateAssessmentQuiz);
newQuizButton.addEventListener("click", regenerateAssessmentQuiz);
submitQuizButton.addEventListener("click", submitAssessmentQuiz);
resetQuizButton.addEventListener("click", resetAssessmentQuiz);
quizDocumentSelect.addEventListener("change", loadSelectedQuiz);

document.body.dataset.page = state.page;
updateConfidence(state.confidence);
renderAssessmentQuiz();
loadUploadedSources();
loadIndexedDocuments();
