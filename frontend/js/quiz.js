// Quiz: library, Create Quiz sheet, Quiz Player (autosave, navigation, finish) and Results/Review.
// Classic script (shared global scope), loaded by index.html before app.js.

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
      if (result.question_type === "fill_blank") {
        options.append(...fillBlankReviewRows(result.selected_answer || "", result.correct_answers || [result.correct_answer], result.is_correct, "review-answer-option"));
      }
      (result.question_type === "fill_blank" ? [] : result.options || []).forEach((option) => {
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
  if (isFillBlankQuestion(question)) {
    options.append(...fillBlankReviewRows(result.selected_answer || "", result.correct_answers || [result.correct_answer], result.is_correct, "review-answer-option"));
  }
  (isFillBlankQuestion(question) ? [] : question.options).forEach((option) => {
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
  revealTutor({ focus: false });
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
  return { single_choice: "Multiple Choice", true_false: "True/False", multi_select: "Multiple Select", fill_blank: "Fill in the Blank" }[questionType]
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
    const parts = ["single_choice", "true_false", "multi_select", "fill_blank"]
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
    document.getElementById("quiz-player-nav").innerHTML = "";
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
  renderQuizPlayerNav();

  const isMultiSelect = question.question_type === "multi_select";
  const selected = quizAnswers[String(question.id)];
  const answersEl = document.getElementById("quiz-player-answers");
  answersEl.innerHTML = "";
  if (isFillBlankQuestion(question)) answersEl.appendChild(createQuizPlayerFillBlank(question, selected));
  (isFillBlankQuestion(question) ? [] : question.options || []).forEach((option) => {
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

// Compact question navigator: one button per question (current / answered / unanswered), built from
// the same local quizAnswers + quizQuestionIndex the rest of the player uses -- a jump is just
// navigation (like Previous/Next), never an answer or a submit.
function renderQuizPlayerNav() {
  const nav = document.getElementById("quiz-player-nav");
  const hadFocus = nav.contains(document.activeElement);
  nav.innerHTML = "";
  let currentButton = null;
  currentQuiz.questions.forEach((question, index) => {
    const answered = Boolean(quizAnswers[String(question.id)]);
    const isCurrent = index === quizQuestionIndex;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `quiz-player-nav-item${answered ? " is-answered" : ""}${isCurrent ? " is-current" : ""}`;
    button.textContent = String(index + 1);
    button.dataset.index = String(index);
    button.setAttribute("aria-label", `Question ${index + 1}, ${answered ? "answered" : "unanswered"}`);
    if (isCurrent) { button.setAttribute("aria-current", "step"); currentButton = button; }
    button.addEventListener("click", () => jumpToQuizPlayerQuestion(index));
    nav.appendChild(button);
  });
  if (!currentButton) return;
  // Keep keyboard focus on the navigator across the re-render, and keep the current item visible
  // when the row scrolls horizontally (narrow screens) -- without scrolling the page itself.
  if (hadFocus) currentButton.focus({ preventScroll: true });
  const left = currentButton.offsetLeft - nav.offsetLeft;
  if (left < nav.scrollLeft) nav.scrollLeft = left;
  else if (left + currentButton.offsetWidth > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = left + currentButton.offsetWidth - nav.clientWidth;
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
    const questionType = result.question_type || question.question_type || "single_choice";
    const fillBlank = questionType === "fill_blank";
    // fill_blank answers are text, shown as written (never upper-cased or sorted like option letters).
    const selected = fillBlank
      ? [String((result.selected_answers?.length ? result.selected_answers[0] : result.selected_answer) || "").trim()].filter(Boolean)
      : quizResultLetters(result.selected_answers?.length ? result.selected_answers : [result.selected_answer]);
    const correct = fillBlank
      ? (result.correct_answers?.length ? result.correct_answers : [result.correct_answer]).map((value) => String(value || "").trim()).filter(Boolean)
      : quizResultLetters(result.correct_answers?.length ? result.correct_answers : [result.correct_answer]);
    const unanswered = selected.length === 0;
    return {
      questionId: result.question_id,
      questionType,
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
  if (item.questionType === "fill_blank") options.append(...fillBlankReviewRows(item.selected[0] || "", item.correct, item.isCorrect));
  (item.questionType === "fill_blank" ? [] : item.options).forEach((option, optionIndex) => {
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

function isFillBlankQuestion(question) {
  return question?.question_type === "fill_blank";
}

// Fill-in-the-blank: a plain text field. Typing only updates local state, the answered count and the
// navigator (no full re-render, so focus and caret stay put) and autosaves like any other answer;
// a blank/whitespace-only field counts as unanswered.
function createQuizPlayerFillBlank(question, value) {
  const wrapper = document.createElement("label");
  wrapper.className = "quiz-player-fill-blank";
  const caption = document.createElement("span");
  caption.className = "quiz-player-fill-caption";
  caption.textContent = "Fill in the blank";
  const input = document.createElement("input");
  input.type = "text";
  input.id = "quiz-player-fill-input";
  input.className = "quiz-player-fill-input";
  input.maxLength = 200;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Type your answer";
  input.value = typeof value === "string" ? value : "";
  input.addEventListener("input", () => updateQuizPlayerFillBlank(question, input.value));
  wrapper.append(caption, input);
  return wrapper;
}

function updateQuizPlayerFillBlank(question, value) {
  const key = String(question.id);
  if (value.trim()) quizAnswers[key] = value;
  else delete quizAnswers[key];
  document.getElementById("quiz-player-answered-count").textContent = `${Object.keys(quizAnswers).length} answered`;
  renderQuizPlayerNav();
  scheduleQuizAutosave();
}

// Review rows for a fill_blank result: the learner's text and the stored accepted answer(s).
function fillBlankReviewRows(selectedText, correctAnswers, isCorrect, rowClass = "quiz-review-option") {
  const rows = [];
  const row = (label, text, classes) => {
    const element = document.createElement("div");
    element.className = [rowClass, "is-fill-blank", ...classes].join(" ");
    const tag = document.createElement("span");
    tag.className = `${rowClass}-tag`;
    tag.textContent = label;
    const body = document.createElement("span");
    body.className = `${rowClass}-label`;
    body.textContent = text;
    element.append(tag, body);
    rows.push(element);
  };
  row("Your answer", selectedText || "No answer", selectedText ? ["is-selected", isCorrect ? "is-correct" : "is-wrong"] : []);
  const [canonical, ...alternatives] = correctAnswers;
  row("Correct answer", alternatives.length ? `${canonical} (also accepted: ${alternatives.join(", ")})` : (canonical || ""), ["is-correct"]);
  return rows;
}

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

function jumpToQuizPlayerQuestion(index) {
  if (!currentQuiz?.questions?.length || index === quizQuestionIndex) return;
  quizQuestionIndex = Math.max(0, Math.min(currentQuiz.questions.length - 1, index));
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
  if (isFillBlankQuestion(question)) {
    // Older inline view: a plain text field committed on change (the focused player autosaves per keystroke).
    const field = document.createElement("input");
    field.type = "text";
    field.className = "quiz-player-fill-input";
    field.maxLength = 200;
    field.placeholder = "Type your answer";
    field.setAttribute("aria-label", "Your answer");
    field.value = typeof quizAnswers[String(question.id)] === "string" ? quizAnswers[String(question.id)] : "";
    field.addEventListener("change", () => {
      if (currentAttempt?.completed) return;
      if (field.value.trim()) quizAnswers[String(question.id)] = field.value.trim();
      else delete quizAnswers[String(question.id)];
      renderAssessmentQuiz();
    });
    options.appendChild(field);
  }
  (isFillBlankQuestion(question) ? [] : question.options).forEach((option) => {
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
