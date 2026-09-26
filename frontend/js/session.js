// Study Session: document overview, summary, flashcards and document progress.
// Classic script (shared global scope), loaded by index.html before app.js.

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
    revealTutor();
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
  // Topic-level detail is secondary and only shown when real quiz evidence backs it.
  if (sessionQuizDetails) sessionQuizDetails.hidden = !(assessed.length || gaps.length || next.length);
  loadDocumentProgress(documentId);
}

function quizCoverageText(mastery) {
  // concept_coverage_ratio = distinct concepts your quizzes have assessed / concepts the topic can be assessed on.
  const ratio = Number(mastery.concept_coverage_ratio);
  if (!Number.isFinite(ratio)) return "Coverage appears after an assessment.";
  const text = `${Math.round(ratio * 100)}% of this topic's key concepts covered by your quizzes`;
  return mastery.has_sufficient_evidence === false ? `${text} · a few more questions give a reliable picture` : text;
}

async function loadDocumentProgress(documentId) {
  if (!sessionProgressState) return;
  const request = ++documentProgressRequest;
  [sessionProgressState, sessionProgressQuiz, sessionProgressPack, sessionProgressPlan].forEach((element) => {
    element.innerHTML = '<p class="empty-state">Loading…</p>';
  });
  try {
    const progress = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(documentId)}?utc_offset_minutes=${plannerUtcOffsetMinutes()}`);
    if (request !== documentProgressRequest || activeDocumentId !== documentId) return;   // switched document meanwhile
    renderDocumentProgress(progress);
  } catch (error) {
    if (request !== documentProgressRequest) return;
    [sessionProgressState, sessionProgressQuiz, sessionProgressPack, sessionProgressPlan].forEach((element) => {
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

  // Study Pack: what exists for this document. Flashcards are a card count only -- there is no
  // review data, so nothing here claims flashcard progress or mastery.
  sessionProgressPack.innerHTML = "";
  const pack = progress.study_pack || { summary_ready: false, flashcard_count: progress.flashcards?.card_count || 0, quiz_count: 0 };
  const cards = Number(pack.flashcard_count) || 0;
  const quizzes = Number(pack.quiz_count) || 0;
  const packList = document.createElement("dl");
  packList.className = "progress-pack";
  [["Summary", pack.summary_ready ? "Ready" : "Not generated yet"],
   ["Flashcards", cards ? `${cards} card${cards === 1 ? "" : "s"}` : "Not generated yet"],
   ["Quiz", quizzes ? `Ready · ${quizzes} quiz${quizzes === 1 ? "" : "zes"}` : "Not generated yet"]].forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = `progress-pack-row${value === "Not generated yet" ? " is-missing" : ""}`;
    const term = document.createElement("dt"); term.textContent = label;
    const detail = document.createElement("dd"); detail.textContent = value;
    row.append(term, detail);
    packList.appendChild(row);
  });
  sessionProgressPack.appendChild(packList);

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
