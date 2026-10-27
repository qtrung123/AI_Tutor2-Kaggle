// AI Tutor: chat, conversations, the persistent tutor overlay and the uploaded-sources panel.
// Classic script (shared global scope), loaded by index.html before app.js.

function tutorOverlayOpen() {
  return document.body.classList.contains("tutor-overlay-open");
}

function setTutorOverlayOpen(open, { focus = true } = {}) {
  const wasFocusedInside = tutorLayout.contains(document.activeElement);
  document.body.classList.toggle("tutor-overlay-open", open);
  tutorLaunchButton?.setAttribute("aria-expanded", String(open));
  if (open) syncTutorResizerValue();   // the docked width is only measurable once the tutor is shown
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
  // Tutor answers are model output: render the safe Markdown subset. User and status text keep the
  // simpler formatter (both build DOM nodes with textContent only).
  if (type === "tutor" && !isLoading) appendMarkdown(body, text);
  else appendFormattedText(body, text);
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

// "lecture.pdf · p. 3, 5" per cited document, pages de-duplicated and in numeric order.
function citationSummaries(citations) {
  const pagesByTitle = new Map();
  citations.forEach((citation) => {
    const title = citation.title || citation.document_id || "Unknown source";
    if (!pagesByTitle.has(title)) pagesByTitle.set(title, new Set());
    const page = String(citation.page ?? "").trim();
    if (page && page !== "Unknown page") pagesByTitle.get(title).add(page);
  });
  return [...pagesByTitle].map(([title, pages]) => {
    const sorted = [...pages].sort((a, b) => (Number(a) - Number(b)) || a.localeCompare(b));
    return { title, pages: sorted, label: sorted.length ? `${title} · p. ${sorted.join(", ")}` : title };
  });
}

// Grounding status and citations sit in a footer under the answer, separate from its text. The
// citation summary opens the conversation's Sources drawer, which lists the cited pages per material.
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
    const summaries = citationSummaries(citations);
    const cite = document.createElement("button");
    cite.type = "button";
    cite.className = "citation-count";
    cite.setAttribute("aria-controls", "conversation-sources-panel");
    const count = document.createElement("span");
    count.className = "citation-count-label";
    count.textContent = `${citations.length} citation${citations.length === 1 ? "" : "s"}`;
    const pages = document.createElement("span");
    pages.className = "citation-pages";
    pages.textContent = summaries.map((summary) => summary.label).join(" · ");
    cite.title = `Show sources: ${pages.textContent}`;
    cite.append(count, pages);
    cite.addEventListener("click", () => setSourcesDrawerOpen(true));
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

    setUploadStatus(`Deleted ${data.deleted}`, "done");
    showToast("Document deleted");
  } catch (error) {
    button.disabled = false;
    button.textContent = "Delete";
    setUploadStatus(error.message || "Delete failed.", "error");
    showToast("Delete failed");
  }
}

function formatBackendAnswer(data) {
  return data.answer || "I could not generate an answer from the current materials.";
}

// The answer streams (Server-Sent Events) when the backend supports it: onDelta receives each piece
// as it is generated. A plain JSON reply (an older backend) is still accepted as the full answer.
async function requestTutorAnswer(userText, options = {}) {
  const { signal, onDelta = () => {}, onStart } = options;
  const conversation = await ensureStudySessionConversation();
  const modelId = await ensureSelectedModelReady();

  const response = await fetch(`${CONVERSATIONS_API_URL}/${conversation.id}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: `${TUTOR_STREAM_MEDIA_TYPE}, application/json`
    },
    body: JSON.stringify({
      message: userText,
      // This is the registry's safe public ID (for example qwen-2.5-7b),
      // never the Ollama/Hugging Face runtime reference.
      model_id: modelId
    }),
    signal
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

  if ((response.headers.get("content-type") || "").includes(TUTOR_STREAM_MEDIA_TYPE)) {
    return readTutorAnswerStream(response, onDelta, onStart);
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

// ---- Chats (conversations) of the open Study Session ----------------------------------------------
// The Tutor panel lists the chats of the current document only (every conversation is scoped to one
// document), newest first. A row opens its chat; rename edits the title inline and delete asks for
// an inline confirmation. While an answer is being generated the list is locked so a reply can never
// land in a different chat.
let conversationEditingId = null;          // row showing the rename field
let conversationConfirmDeleteId = null;    // row asking to confirm deletion
let conversationFocusAfterRender = null;   // [conversation id, selector] to focus after re-rendering

function conversationDocumentId(conversation) {
  if (!conversation) return null;
  return conversation.document_id || (conversation.document_ids?.length === 1 ? conversation.document_ids[0] : null);
}

function sessionConversations() {
  return activeDocumentId ? conversations.filter((item) => conversationDocumentId(item) === activeDocumentId) : conversations;
}

function conversationsLocked() {
  return chatForm.classList.contains("is-sending");
}

function formatConversationTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { day: "numeric", month: "short" });
}

function conversationActionButton(className, label, text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.setAttribute("aria-label", label);
  button.title = label;
  button.disabled = conversationsLocked();
  button.addEventListener("click", onClick);
  return button;
}

function renderConversationRename(row, conversation) {
  const form = document.createElement("form");
  form.className = "conversation-rename-form";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "conversation-rename-input";
  input.maxLength = 120;
  input.value = conversation.title || "";
  input.setAttribute("aria-label", "Chat name");
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();   // Escape cancels the rename only, not the whole Tutor panel
    cancelConversationEdit(conversation.id);
  });
  const save = document.createElement("button");
  save.type = "submit";
  save.className = "conversation-rename-save";
  save.textContent = "Save";
  const cancel = conversationActionButton("conversation-rename-cancel", "Cancel renaming", "Cancel", () => cancelConversationEdit(conversation.id));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    renameChatConversation(conversation.id, input.value);
  });
  form.append(input, save, cancel);
  row.appendChild(form);
}

function renderConversationDeleteConfirm(row, conversation) {
  const prompt = document.createElement("p");
  prompt.className = "conversation-confirm-text";
  prompt.textContent = `Delete “${conversation.title}”? Its messages will be removed.`;
  const actions = document.createElement("div");
  actions.className = "conversation-confirm-actions";
  const confirm = conversationActionButton("conversation-confirm-delete", `Delete chat ${conversation.title}`, "Delete", () => deleteChatConversation(conversation.id));
  const cancel = conversationActionButton("conversation-confirm-cancel", "Keep this chat", "Cancel", () => {
    conversationConfirmDeleteId = null;
    conversationFocusAfterRender = [conversation.id, ".conversation-delete-button"];
    renderConversationList();
  });
  actions.append(confirm, cancel);
  row.classList.add("is-confirming");
  row.append(prompt, actions);
}

function renderConversationList() {
  conversationList.innerHTML = "";
  const items = sessionConversations();
  const count = document.getElementById("tutor-conversations-count");
  if (count) count.textContent = String(items.length);
  if (newConversationButton) newConversationButton.disabled = conversationsLocked();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty muted";
    empty.textContent = "No chats yet. Ask a question to start one.";
    conversationList.appendChild(empty);
    return;
  }
  items.forEach((conversation) => {
    const isActive = conversation.id === activeConversation?.id;
    const row = document.createElement("div");
    row.className = `conversation-item${isActive ? " active" : ""}`;
    row.setAttribute("role", "listitem");
    row.dataset.conversationId = conversation.id;
    if (conversation.id === conversationEditingId) {
      renderConversationRename(row, conversation);
    } else if (conversation.id === conversationConfirmDeleteId) {
      renderConversationDeleteConfirm(row, conversation);
    } else {
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "conversation-open-button";
      openButton.disabled = conversationsLocked() && !isActive;
      if (isActive) openButton.setAttribute("aria-current", "true");
      const title = document.createElement("strong");
      title.textContent = conversation.title;
      const meta = document.createElement("span");
      meta.textContent = formatConversationTime(conversation.updated_at);
      openButton.append(title, meta);
      openButton.addEventListener("click", () => {
        if (!isActive) openConversation(conversation.id);
      });
      const rename = conversationActionButton("conversation-rename-button", `Rename chat ${conversation.title}`, "✎", () => {
        conversationEditingId = conversation.id;
        conversationConfirmDeleteId = null;
        conversationFocusAfterRender = [conversation.id, ".conversation-rename-input"];
        renderConversationList();
      });
      const remove = conversationActionButton("conversation-delete-button", `Delete chat ${conversation.title}`, "×", () => {
        conversationConfirmDeleteId = conversation.id;
        conversationEditingId = null;
        conversationFocusAfterRender = [conversation.id, ".conversation-confirm-cancel"];
        renderConversationList();
      });
      row.append(openButton, rename, remove);
    }
    conversationList.appendChild(row);
  });
  if (conversationFocusAfterRender) {
    const [id, selector] = conversationFocusAfterRender;
    conversationFocusAfterRender = null;
    const target = conversationList.querySelector(`[data-conversation-id="${CSS.escape(id)}"] ${selector}`);
    target?.focus({ preventScroll: true });
    if (target?.select) target.select();
  }
  // Keep the active chat visible by scrolling the list only (scrollIntoView would also scroll the
  // page on tablet/phone, where the Tutor sits inline below the content).
  const activeRow = conversationList.querySelector(".conversation-item.active");
  if (activeRow) {
    const listBox = conversationList.getBoundingClientRect();
    const rowBox = activeRow.getBoundingClientRect();
    if (rowBox.top < listBox.top) conversationList.scrollTop -= listBox.top - rowBox.top;
    else if (rowBox.bottom > listBox.bottom) conversationList.scrollTop += rowBox.bottom - listBox.bottom;
  }
}

function cancelConversationEdit(conversationId) {
  conversationEditingId = null;
  conversationFocusAfterRender = [conversationId, ".conversation-rename-button"];
  renderConversationList();
}

function setConversationListCollapsed(collapsed, persist = true) {
  const section = document.getElementById("tutor-conversations");
  const toggle = document.getElementById("tutor-conversations-toggle");
  section?.classList.toggle("is-collapsed", collapsed);
  toggle?.setAttribute("aria-expanded", String(!collapsed));
  if (persist) writeLocalPref("aiTutorChatListCollapsed", collapsed ? "1" : "0");
}

function renderConversationMessages() {
  messageList.innerHTML = "";
  const messages = activeConversation?.messages || [];
  if (!messages.length) {
    const hint = addMessage("Ask a question about the selected materials. This conversation and its answers will be saved automatically.", "tutor");
    hint.classList.add("conversation-empty-hint");   // removed once the first question is sent
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

function rememberConversation(conversation) {
  if (!currentUser || !conversation) return;
  writeLocalPref(`activeConversationId:${currentUser.id}`, conversation.id);
  const documentId = conversationDocumentId(conversation);
  if (documentId) writeLocalPref(`activeConversationId:${currentUser.id}:${documentId}`, conversation.id);
}

// Only the most recent open wins: a slower earlier response (quick chat or document switches) must
// not replace the chat the learner picked last.
let openConversationSeq = 0;

async function openConversation(conversationId) {
  const seq = ++openConversationSeq;
  try {
    const conversation = await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`);
    if (seq !== openConversationSeq) return;
    activeConversation = conversation;
    rememberConversation(activeConversation);
    conversationEditingId = null;
    conversationConfirmDeleteId = null;
    renderConversationList();
    renderConversationSources();
    renderConversationMessages();
    updateConversationHeader();
  } catch (error) {
    if (seq === openConversationSeq) showToast(error.message || "Could not open conversation");
  }
}

// "New chat": another conversation for the open document. An untouched empty chat is reused instead
// of piling up blank ones.
async function createChatConversation() {
  if (conversationsLocked()) return;
  try {
    const documentItem = getActiveStudySessionDocument();
    if (activeConversation && conversationDocumentId(activeConversation) === documentItem.id && !(activeConversation.messages || []).length) {
      chatInput.focus({ preventScroll: true });
      showToast("This chat is still empty. Ask your first question.");
      return;
    }
    const created = await fetchJson(CONVERSATIONS_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "New conversation", document_ids: [documentItem.id], new_conversation: true })
    });
    conversations = [created, ...conversations.filter((item) => item.id !== created.id)];
    await openConversation(created.id);
    chatInput.focus({ preventScroll: true });
    showToast("New chat started");
  } catch (error) {
    showToast(error.message || "Could not create conversation");
  }
}

async function renameChatConversation(conversationId, rawTitle) {
  const title = String(rawTitle || "").trim();
  const current = conversations.find((item) => item.id === conversationId);
  if (!title) {
    showToast("Enter a name for this chat");
    return;
  }
  if (current && title === current.title) {
    cancelConversationEdit(conversationId);
    return;
  }
  try {
    const updated = await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.slice(0, 120) })
    });
    conversations = conversations.map((item) => (item.id === conversationId ? { ...item, ...updated, messages: item.messages } : item));
    if (activeConversation?.id === conversationId) activeConversation.title = updated.title;
    conversationEditingId = null;
    conversationFocusAfterRender = [conversationId, ".conversation-open-button"];
    renderConversationList();
    updateConversationHeader();
    showToast("Chat renamed");
  } catch (error) {
    showToast(error.message || "Could not rename this chat");
  }
}

async function deleteChatConversation(conversationId) {
  if (conversationsLocked()) return;
  try {
    await fetchJson(`${CONVERSATIONS_API_URL}/${conversationId}`, { method: "DELETE" });
    conversations = conversations.filter((item) => item.id !== conversationId);
    conversationConfirmDeleteId = null;
    if (activeConversation?.id === conversationId) {
      activeConversation = null;
      const next = sessionConversations()[0];
      if (next) await openConversation(next.id);
      else if (activeDocumentId) await ensureStudySessionConversation();
      else { renderConversationMessages(); updateConversationHeader(); }
    }
    renderConversationList();
    newConversationButton?.focus({ preventScroll: true });
    showToast("Chat deleted");
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

const TUTOR_LOADING_SENDING = "Sending your question to the AI Tutor";
const TUTOR_LOADING_READING = "Reading your study materials and writing the answer";
let tutorAnswerController = null;   // AbortController of the answer being streamed (Stop cancels it)

function tutorMessageBody(row) {
  return row.querySelector(".message-body");
}

function tutorScrolledToEnd() {
  return messageList.scrollHeight - messageList.scrollTop - messageList.clientHeight < 24;
}

async function sendTutorMessage(userText, { restoreOnError = false } = {}) {
  messageList.querySelector(".conversation-empty-hint")?.remove();
  addMessage(userText, "user");
  chatInput.value = "";
  chatInput.disabled = true;
  const submitButton = chatForm.querySelector("button");
  chatForm.classList.add("is-sending");
  // While the answer is generated the Send button becomes Stop (see handleChatSubmit).
  submitButton.textContent = "Stop";
  submitButton.setAttribute("aria-label", "Stop generating");
  renderConversationList();
  // The loading row reports only real progress: sent -> the backend's "start" event (it accepted the
  // question and is reading the materials) -> the first piece of the answer replaces the row.
  const loadingRow = addMessage(TUTOR_LOADING_SENDING, "tutor", true);
  loadingRow.querySelector(".message")?.setAttribute("role", "status");
  const onStart = () => {
    const body = loadingRow.isConnected && tutorMessageBody(loadingRow);
    if (body) body.textContent = TUTOR_LOADING_READING;
  };
  const controller = new AbortController();
  tutorAnswerController = controller;
  // The loading row stays until the first piece of text; then the answer grows in place, rendered
  // as Markdown at most every TUTOR_STREAM_RENDER_MS (never once per token).
  let streamRow = null;
  let streamed = "";
  const renderer = createTutorStreamRenderer(() => {
    const follow = tutorScrolledToEnd();
    const body = tutorMessageBody(streamRow);
    body.replaceChildren();
    appendMarkdown(body, streamed);
    if (follow) messageList.scrollTop = messageList.scrollHeight;
  });
  const onDelta = (text) => {
    if (!streamRow) {
      loadingRow.remove();
      streamRow = addMessage("", "tutor");
      streamRow.classList.add("is-streaming");
    }
    streamed += text;
    renderer.request();
  };

  try {
    const data = await requestTutorAnswer(userText, { signal: controller.signal, onDelta, onStart });
    renderer.cancel();
    loadingRow.remove();
    const repliedIn = data.assistant_message?.conversation_id;
    if (repliedIn && activeConversation?.id !== repliedIn) {
      streamRow?.remove();
      conversations = await fetchJson(CONVERSATIONS_API_URL);
      return;
    }
    let answerElement = streamRow;
    if (answerElement) {
      // The saved answer is authoritative (trimmed by the backend): render it once more, in full.
      answerElement.classList.remove("is-streaming");
      const body = tutorMessageBody(answerElement);
      body.replaceChildren();
      appendMarkdown(body, formatBackendAnswer(data));
    } else {
      answerElement = addMessage(formatBackendAnswer(data), "tutor");
    }
    appendMessageMeta(answerElement, data.grounding_status, data.citations || []);
    if (answerElement.offsetHeight > messageList.clientHeight) {
      messageList.scrollTop += answerElement.getBoundingClientRect().top - messageList.getBoundingClientRect().top - 8;
    }
    activeConversation.messages = [...(activeConversation.messages || []), data.user_message, data.assistant_message];
    conversations = await fetchJson(CONVERSATIONS_API_URL);
    activeConversation.title = conversations.find((item) => item.id === activeConversation.id)?.title || activeConversation.title;
    renderConversationList();
    updateConversationHeader();
  } catch (error) {
    renderer.cancel();
    loadingRow.remove();
    if (error?.name === "AbortError") {
      // Stopped by the learner: keep what was generated, marked as not saved (the backend saved nothing).
      if (streamRow) {
        renderer.flush();
        streamRow.classList.remove("is-streaming");
        const note = document.createElement("p");
        note.className = "message-stopped-note";
        note.textContent = "Stopped. This answer was not saved.";
        streamRow.querySelector(".message")?.appendChild(note);
      }
      showToast("Answer stopped");
      return;
    }
    streamRow?.remove();   // a partial answer that failed is never shown as if it were complete
    const detail = error.message || "Backend request failed.";
    console.error("AI Tutor message submission failed", error);
    // Nothing was saved, so a typed question goes back into the box: Send asks it again. (Quiz
    // "Explain" prompts are long generated messages and are not put back.)
    const restored = restoreOnError && !chatInput.value.trim();
    if (restored) chatInput.value = userText;
    const lead = error instanceof TutorStreamError ? "The answer was interrupted and was not saved." : "I could not reach the AI Tutor right now.";
    const errorRow = addMessage(`${lead}\n\n${detail}${restored ? "\n\nYour question is back in the message box. Press Send to try again." : ""}`, "tutor");
    errorRow.querySelector(".message")?.classList.add("error-message");
    errorRow.querySelector(".message")?.setAttribute("role", "alert");
    showToast("AI Tutor request failed");
  } finally {
    if (tutorAnswerController === controller) tutorAnswerController = null;
    chatInput.disabled = false;
    chatForm.classList.remove("is-sending");
    submitButton.textContent = "Send";
    submitButton.removeAttribute("aria-label");
    renderConversationList();
    chatInput.focus();
  }
}

async function handleChatSubmit(event) {
  event.preventDefault();
  if (chatForm.classList.contains("is-sending")) {   // the button reads Stop while an answer streams
    tutorAnswerController?.abort();
    return;
  }
  const userText = chatInput.value.trim();
  if (!userText) {
    showToast("Type a question first");
    return;
  }
  await sendTutorMessage(userText, { restoreOnError: true });
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

// state: "busy" (request running), "done" or "error"; styled via data-state on #upload-status.
function setUploadStatus(text, state) {
  uploadStatus.textContent = text;
  uploadStatus.dataset.state = state;
}

// Built only from the upload response: files newly indexed, their chunks, and files skipped
// because the same content was already indexed.
function uploadResultText(data) {
  const parts = [];
  if (data.new_files) parts.push(`${data.new_files} file${data.new_files === 1 ? "" : "s"} indexed (${data.new_chunks} chunk${data.new_chunks === 1 ? "" : "s"})`);
  const skipped = data.skipped_files || [];
  if (skipped.length) parts.push(`already in your library: ${skipped.join(", ")}`);
  // A re-uploaded file that already has a subject is never moved by an upload.
  const kept = data.subject_kept_document_ids || [];
  if (kept.length) parts.push(`kept in their current subject: ${kept.join(", ")} (use its Subject menu on Home to move it)`);
  const text = parts.join("; ") || "No new content was indexed";
  return text.charAt(0).toUpperCase() + text.slice(1) + ".";
}

// `subject` (optional, from chooseUploadSubject): {subject_id} or {new_subject_name} groups every
// uploaded file in that subject; without one the files stay Unassigned.
async function uploadSourceFiles(files, subject = null) {
  if (!files.length) {
    return;
  }

  const formData = new FormData();
  Array.from(files).forEach((file) => formData.append("files", file));
  if (subject?.subject_id) formData.append("subject_id", subject.subject_id);
  if (subject?.new_subject_name) formData.append("new_subject_name", subject.new_subject_name);

  uploadSourceButton.disabled = true;
  uploadSourceButton.setAttribute("aria-busy", "true");
  sourceFileInput.disabled = true;
  // One request uploads AND indexes (no progress is reported), so the status names the files and
  // the phase only; it never shows a made-up percentage.
  const names = Array.from(files, (file) => file.name);
  const what = names.length === 1 ? `"${names[0]}"` : `${names.length} files`;
  setUploadStatus(`Uploading and indexing ${what}. Large PDFs can take a few minutes; keep this tab open.`, "busy");

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
    setUploadStatus("Indexed. Updating your library...", "busy");
    uploadedSources = data.sources || [];
    renderSources(uploadedSources, "uploaded");
    renderConversationSources();
    await loadIndexedDocuments();
    await loadDashboard();

    setUploadStatus(uploadResultText(data), "done");
    // An upload never changes the Study Planner: materials join a plan only through Add Material.
    showToast(data.skipped_files?.length ? "Some files were already indexed" : "Material uploaded");
  } catch (error) {
    const detail = error.message || "Upload failed.";
    setUploadStatus(`Upload failed: ${detail}`, "error");
    showToast("Upload failed");
  } finally {
    uploadSourceButton.disabled = false;
    uploadSourceButton.removeAttribute("aria-busy");
    sourceFileInput.disabled = false;
    sourceFileInput.value = "";
  }
}
