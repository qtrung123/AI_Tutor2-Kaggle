// AI Tutor: chat, conversations, the persistent tutor overlay and the uploaded-sources panel.
// Classic script (shared global scope), loaded by index.html before app.js.

function tutorOverlayOpen() {
  return document.body.classList.contains("tutor-overlay-open");
}

function setTutorOverlayOpen(open, { focus = true } = {}) {
  const wasFocusedInside = tutorLayout.contains(document.activeElement);
  document.body.classList.toggle("tutor-overlay-open", open);
  tutorLaunchButton?.setAttribute("aria-expanded", String(open));
  if (open && focus) chatInput.focus({ preventScroll: true });
  if (!open && wasFocusedInside) tutorLaunchButton?.focus({ preventScroll: true });
}

// Callers that want the tutor visible (Overview's AI Tutor tool, "Explain" on a quiz answer).
function revealTutor(options) {
  if (tutorOverlayQuery.matches) setTutorOverlayOpen(true, options);
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
