// Home is a learning dashboard: Quick actions, Continue learning, Today's Schedule, Upcoming study
// sessions and Study progress. The document list itself lives in the library view (All Documents and
// each subject's view, js/subjects.js); its rows are still built here (renderHomeDocumentCards).
// Classic script (shared global scope), loaded by index.html before app.js.

function renderDashboard() {
  if (!dashboardData) return;
  const materials = dashboardData.materials || [];
  setUserSubjects(dashboardData.user_subjects);
  renderSidebarRecentDocuments(materials);
  // Search helps once the library is large, or once it is organized into subjects (it matches subject names too).
  if (homeMaterialSearch) homeMaterialSearch.hidden = materials.length < HOME_SEARCH_MIN_MATERIALS && !userSubjects.length;
  renderSidebarSubjects();
  renderLibrary();
  renderHomeContinue(materials);
  renderHomeProgress();
  // Subjects can arrive after the desktop calendar first rendered (a reload on the Planner page).
  if (document.body.dataset.page === "planner" && plannerIsDesktop()) renderPlannerWorkspace();
  loadHomeDocumentStates(materials.map((material) => material.document_id));
}

// Re-reads every document's progress (Home cards and per-subject progress), e.g. after a planned
// session changed status.
function refreshHomeDocumentStates() {
  if (dashboardData) loadHomeDocumentStates((dashboardData.materials || []).map((material) => material.document_id));
}

const PROGRESS_BATCH_SIZE = 40;

// Every document's progress in one request (GET /api/progress/documents?document_ids=...). A backend
// without the batch route (or a failed batch) falls back to one request per document.
async function fetchDocumentProgressMap(documentIds) {
  const states = new Map();
  if (!documentIds.length) return states;
  const offset = `utc_offset_minutes=${plannerUtcOffsetMinutes()}`;
  try {
    // Chunks keep each URL short enough for proxies (nginx / Cloudflare in the Kaggle deployment).
    const chunks = [];
    for (let index = 0; index < documentIds.length; index += PROGRESS_BATCH_SIZE) chunks.push(documentIds.slice(index, index + PROGRESS_BATCH_SIZE));
    const batches = await Promise.all(chunks.map((chunk) => plannerRequest(
      `${PROGRESS_API_BASE_URL}?${offset}&${chunk.map((id) => `document_ids=${encodeURIComponent(id)}`).join("&")}`)));
    if (batches.every((batch) => batch?.documents && typeof batch.documents === "object")) {
      batches.forEach((batch) => Object.entries(batch.documents).forEach(([id, progress]) => {
        if (progress?.learning) states.set(id, progress);
      }));
      return states;
    }
  } catch (error) {
    // older backend: per-document requests below
  }
  await Promise.all(documentIds.map(async (id) => {
    try {
      const progress = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(id)}?${offset}`);
      if (progress?.learning) states.set(id, progress);
    } catch (error) {
      // no state for this document: its card simply shows fewer details
    }
  }));
  return states;
}

async function loadHomeDocumentStates(documentIds) {
  const load = ++homeDocStatesLoad;
  const states = await fetchDocumentProgressMap(documentIds);
  if (load !== homeDocStatesLoad) return;
  homeDocStates = states;
  homeDocStatesReady = true;
  renderHomeContinue(dashboardData?.materials || []);
  renderHomeDocumentCards();
  renderHomeProgress();
  renderLibraryActivity();
}

// What a document's Study Pack holds right now; missing parts are said plainly, never assumed.
function studyPackText(pack) {
  if (!pack) return "";
  const cards = Number(pack.flashcard_count) || 0;
  const quizzes = Number(pack.quiz_count) || 0;
  const parts = [pack.summary_ready && "Summary ready", cards && `${cards} flashcard${cards === 1 ? "" : "s"}`,
    quizzes && `${quizzes} quiz${quizzes === 1 ? "" : "zes"}`].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Not generated yet";
}

function nextSessionText(session) {
  if (!session) return "";
  const activity = pcalActivity(session.activity_type, session.steps);
  if (session.status === "in_progress") return `${activity} · in progress`;
  return `${activity} · ${plannerDayLabel(session.scheduled_start.slice(0, 10))}, ${session.scheduled_start.slice(11, 16)}`;
}

function homeDocumentFact(label, value) {
  const row = document.createElement("div");
  row.className = "home-doc-fact";
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  row.append(term, detail);
  return row;
}

// The active plans' sessions as Today's Study Plan last loaded them (full rows: plan, artifact, steps).
let homePlanSessions = [];

// Continue Learning: the one thing to pick up now, chosen from real state only -- a planned session
// already in progress, else the soonest planned session, else the document with the most recent quiz,
// else the first material. A planned session goes through the Planner's own Start/Resume lifecycle
// (status, steps, the right tool and quiz); anything else just opens the document.
function homeContinueTarget(materials) {
  const entries = materials.map((material) => ({ material, progress: homeDocStates.get(material.document_id) }));
  const next = (entry) => entry.progress?.plan?.next_session || null;
  const live = entries.find((entry) => next(entry)?.status === "in_progress");
  if (live) return { ...live, session: next(live), detail: nextSessionText(next(live)), action: "Resume" };
  const planned = entries.filter((entry) => next(entry)?.status === "scheduled")
    .sort((a, b) => next(a).scheduled_start.localeCompare(next(b).scheduled_start))[0];
  if (planned) return { ...planned, session: next(planned), detail: `Next session · ${nextSessionText(next(planned))}`, action: "Start" };
  const latestQuiz = (entry) => entry.progress?.quiz?.latest?.completed_at || "";
  const studied = entries.filter(latestQuiz).sort((a, b) => latestQuiz(b).localeCompare(latestQuiz(a)))[0];
  if (studied) {
    const latest = studied.progress.quiz.latest;
    return { ...studied, detail: `Latest quiz ${Math.round(latest.percentage)}% · ${progressDate(latest.completed_at)}`, tab: "overview", action: "Continue" };
  }
  return { ...entries[0], detail: "Not started yet", tab: "overview", action: "Start studying" };
}

// True while Today's Study Plan features a session under "Next up" (or "Next session · <day>"): that
// session is the next thing to study, so the Continue card does not show it a second time.
let homePlanFeaturesSession = false;

function renderHomeContinue(materials) {
  const panel = document.getElementById("home-continue");
  if (!panel) return;
  panel.innerHTML = "";
  panel.hidden = !materials.length || homePlanFeaturesSession;
  if (panel.hidden) return;
  const target = homeContinueTarget(materials);
  const documentId = target.material.document_id;
  const title = target.material.document_name || target.progress?.title || documentId;

  const copy = document.createElement("div");
  copy.className = "home-continue-copy";
  const label = document.createElement("p");
  label.className = "home-continue-label";
  label.id = "home-continue-heading";
  label.textContent = "Continue learning";
  const name = document.createElement("h3");
  name.className = "home-continue-title";
  name.textContent = title;
  const detail = document.createElement("p");
  detail.className = "home-continue-detail";
  detail.textContent = target.detail;
  copy.append(label, name, detail);
  if (target.progress?.learning) {
    const state = document.createElement("span");
    state.className = `progress-state progress-state--${target.progress.learning.state} home-continue-state`;
    state.textContent = target.progress.learning.label;
    name.appendChild(state);
  }

  const open = document.createElement("button");
  open.type = "button";
  open.className = "primary-button home-continue-open";
  open.textContent = target.action;
  open.setAttribute("aria-label", `${target.action}: ${title}`);
  open.addEventListener("click", async () => {
    if (!target.session) { openStudySession(documentId, target.tab); return; }
    // The same session object the Today list acts on, so its busy guard and status stay shared.
    const session = homePlanSessions.find((item) => item.session_id === target.session.session_id) || target.session;
    await plannerSessionAction(session, "start", open, panel);
    target.session.status = session.status;   // e.g. scheduled -> in_progress: the button now says Resume
    renderHomeContinue(materials);
  });
  panel.append(copy, open);
}

function renderHomeDocumentCards() {
  const allMaterials = dashboardData?.materials || [];
  overviewMaterialsList.innerHTML = "";
  // The library's view: All Documents, one subject, or Unassigned.
  const materials = allMaterials.filter((material) => subjectFilterMatches(homeSubjectFilter, material.document_id));
  renderHomeBulkBar();
  if (allMaterials.length && !materials.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = homeSubjectFilter === SUBJECT_UNASSIGNED
      ? "Every document is in a subject."
      : "No documents in this subject yet. Move one here with its Subject menu, or upload new files into it.";
    overviewMaterialsList.appendChild(empty);
    return;
  }
  if (!materials.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No documents yet. Upload a PDF or TXT file to start.";
    const upload = document.createElement("button");
    upload.type = "button";
    upload.className = "secondary-button empty-state-action";
    upload.textContent = "Upload document";
    upload.addEventListener("click", () => document.getElementById("upload-source-button")?.click());
    empty.appendChild(upload);
    overviewMaterialsList.appendChild(empty);
    return;
  }
  // Documents with a planned session come first (soonest first); the rest keep their order.
  const nextStart = (id) => homeDocStates.get(id)?.plan?.next_session?.scheduled_start || "￿";
  const ordered = materials.map((material, index) => ({ material, index }))
    .sort((a, b) => nextStart(a.material.document_id).localeCompare(nextStart(b.material.document_id)) || a.index - b.index);
  ordered.forEach(({ material }) => {
    const documentId = material.document_id;
    const progress = homeDocStates.get(documentId);
    const card = document.createElement("article");
    card.className = "home-doc-card";
    card.dataset.documentId = documentId;
    card.dataset.title = (material.document_name || documentId).toLowerCase();
    const subject = subjectOfDocument(documentId);
    card.dataset.subject = (subject?.name || "").toLowerCase();
    if (subject) card.style.setProperty("--subject-color", subject.color);

    const header = document.createElement("div");
    header.className = "home-doc-header";
    const title = document.createElement("h3");
    title.textContent = material.document_name || progress?.title || documentId;
    const subjectControls = homeDocumentSubjectControls(documentId, title.textContent);
    header.append(subjectControls.pick, title);
    if (progress) {
      const state = document.createElement("span");
      state.className = `progress-state progress-state--${progress.learning.state} home-doc-state`;
      state.textContent = progress.learning.label;
      header.appendChild(state);
    }

    const facts = document.createElement("dl");
    facts.className = "home-doc-facts";
    const latest = progress?.quiz?.latest;
    if (latest) {
      facts.appendChild(homeDocumentFact("Latest quiz", `${Math.round(latest.percentage)}% · ${latest.score}/${latest.total} · ${progressDate(latest.completed_at)}`));
    }
    if (progress?.study_pack) facts.appendChild(homeDocumentFact("Study Pack", studyPackText(progress.study_pack)));
    if (progress?.plan?.next_session) facts.appendChild(homeDocumentFact("Next session", nextSessionText(progress.plan.next_session)));
    if (progress?.plan?.deadline) facts.appendChild(homeDocumentFact("Deadline", plannerDayLabel(progress.plan.deadline)));

    const actions = document.createElement("div");
    actions.className = "home-doc-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "primary-button home-doc-open";
    open.textContent = progress && progress.learning.state !== "new" ? "Continue" : "Open";
    open.setAttribute("aria-label", `${open.textContent} ${title.textContent}`);
    open.addEventListener("click", () => openStudySession(documentId));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button danger-button home-doc-delete";
    remove.textContent = "Delete";
    remove.setAttribute("aria-label", `Delete ${title.textContent}`);
    remove.addEventListener("click", () => deleteUploadedSource({ title: documentId }, remove));
    actions.append(subjectControls.select, open, remove);

    card.append(header);
    if (facts.children.length) card.append(facts);
    card.append(actions);
    overviewMaterialsList.appendChild(card);
  });
  applySessionLibraryFilters();
}

async function loadDashboard() {
  try {
    dashboardData = await fetchJson(DASHBOARD_API_URL);
    renderDashboard();
  } catch (error) {
    dashboardData = null;
    const continuePanel = document.getElementById("home-continue");
    if (continuePanel) continuePanel.hidden = true;
    overviewMaterialsList.innerHTML = '<div class="empty-state">Your documents could not be loaded right now.</div>';
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

// ---- Home: Today's Schedule and Upcoming study sessions ------------------------------------

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
    homePlanSessions = lists.flat();
    renderTodayPlan(homePlanSessions.filter(plannerIsActiveSession), progress);
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
  // After today: listed under Upcoming study sessions (the first one is "the next session" once today is done).
  const later = remaining.filter((session) => session.scheduled_start.slice(0, 10) > todayKey);
  const upcoming = today.length ? null : later[0] || null;
  // Scheduled sessions whose time has passed: offered Reschedule / Skip, never silently dropped.
  const overdue = sessions.filter((session) => plannerIsOverdue(session, now))
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  return { today, upcoming, later, overdue };
}

function renderTodayPlan(sessions, progress = null) {
  if (!todayPlanPanel) return;
  todayPlanPanel.hidden = false;
  todayPlanPanel.innerHTML = "";
  // Nothing planned: the panel becomes a compact prompt and Continue learning leads.
  todayPlanPanel.classList.remove("today-plan--idle");
  const now = plannerNow();
  const heading = document.createElement("div");
  heading.className = "today-plan-heading";
  const titles = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = now.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
  const title = document.createElement("h2");
  title.id = "today-plan-title";
  title.textContent = "Today’s Schedule";
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

  const features = (value) => {
    if (homePlanFeaturesSession === value) return;
    homePlanFeaturesSession = value;
    renderHomeContinue(dashboardData?.materials || []);   // show or hide the Continue card to match
  };
  renderHomeUpcoming(sessions, now);
  renderLibraryActivity();   // a subject's view lists its next sessions from the same rows
  if (sessions === null) { features(false); empty("Your study plan could not be loaded right now."); return; }
  const { today, upcoming, overdue } = plannerTodayAgenda(sessions, now);
  features(Boolean(today.length || upcoming));
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
    empty("Nothing left for today.");   // the next session leads Upcoming study sessions below
  } else if (!overdue.length) {
    todayPlanPanel.classList.add("today-plan--idle");
    empty("No upcoming study sessions. Create a plan to see what to study next.");
  }
  if (overdue.length) section("Not completed", "today-plan-overdue").appendChild(list(overdue));
}

// The plan's remaining work, soonest first: sessions in progress or not yet over (local clock).
function homeRemainingSessions(now = plannerNow()) {
  const nowIso = plannerLocalIso(now);
  return homePlanSessions.filter(plannerIsActiveSession)
    .filter((session) => session.status === "in_progress" || session.scheduled_end > nowIso)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
}

function homeDayHeading(dateKey, now = plannerNow()) {
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  if (dateKey === plannerDateKey(now)) return "Today";
  if (dateKey === plannerDateKey(tomorrow)) return `Tomorrow · ${plannerDayLabel(dateKey)}`;
  return plannerDayLabel(dateKey);
}

// Sessions grouped under their day, each row with the Planner's own Start/Resume/Reschedule actions.
function homeSessionDayList(sessions, now = plannerNow()) {
  const days = document.createElement("ol");
  days.className = "home-session-days";
  const byDay = new Map();
  sessions.forEach((session) => {
    const key = session.scheduled_start.slice(0, 10);
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(session);
  });
  byDay.forEach((items, key) => {
    const day = document.createElement("li");
    day.className = "home-session-day";
    day.dataset.day = key;
    const heading = document.createElement("h3");
    heading.textContent = homeDayHeading(key, now);
    const list = document.createElement("ol");
    list.className = "planner-session-list";
    items.forEach((session) => list.appendChild(plannerSessionItem(session, "li", { action: true })));
    day.append(heading, list);
    days.appendChild(day);
  });
  return days;
}

// Upcoming study sessions: the planned sessions after today (Today's Schedule holds today's), so no
// session is listed twice on Home. Hidden when nothing is planned after today.
const HOME_UPCOMING_LIMIT = 6;

function renderHomeUpcoming(sessions, now = plannerNow()) {
  const panel = document.getElementById("home-upcoming");
  if (!panel) return;
  panel.innerHTML = "";
  const later = sessions ? plannerTodayAgenda(sessions, now).later : [];
  panel.hidden = !later.length;
  if (panel.hidden) return;
  const heading = document.createElement("div");
  heading.className = "today-plan-heading";
  const title = document.createElement("h2");
  title.id = "home-upcoming-title";
  title.textContent = "Upcoming study sessions";
  heading.appendChild(title);
  panel.append(heading, homeSessionDayList(later.slice(0, HOME_UPCOMING_LIMIT), now));
  if (later.length > HOME_UPCOMING_LIMIT) {
    const more = document.createElement("p");
    more.className = "home-upcoming-more";
    more.textContent = `+${later.length - HOME_UPCOMING_LIMIT} more in the Study Planner`;
    panel.appendChild(more);
  }
}

// ---- Home: Study progress -------------------------------------------------------------------------
// Where the documents stand (their learning state, from /api/progress/documents) and the active plan's
// sessions per subject (js/subjects.js renderHomeSubjectProgress). Nothing is estimated or invented.

const LEARNING_STATE_ORDER = ["learning", "needs_review", "on_track", "completed", "new"];

// [[state, label, count]] for the given documents whose progress is known, in a fixed order.
function learningStateCounts(documentIds) {
  const counts = new Map();
  documentIds.forEach((id) => {
    const learning = homeDocStates.get(id)?.learning;
    if (!learning) return;
    const entry = counts.get(learning.state) || [learning.state, learning.label, 0];
    entry[2] += 1;
    counts.set(learning.state, entry);
  });
  const rank = (state) => (LEARNING_STATE_ORDER.indexOf(state) + 1) || LEARNING_STATE_ORDER.length + 1;
  return [...counts.values()].sort((left, right) => rank(left[0]) - rank(right[0]));
}

function renderHomeProgress() {
  const states = document.getElementById("home-progress-states");
  const empty = document.getElementById("home-progress-empty");
  if (!states || !empty) return;
  const materials = dashboardData?.materials || [];
  states.innerHTML = "";
  if (homeDocStatesReady) {
    learningStateCounts(materials.map((material) => material.document_id)).forEach(([state, label, count]) => {
      const chip = document.createElement("span");
      chip.className = `home-progress-state home-progress-state--${state}`;
      chip.dataset.state = state;
      const number = document.createElement("strong");
      number.textContent = String(count);
      chip.append(number, document.createTextNode(` ${label}`));
      states.appendChild(chip);
    });
  }
  renderHomeSubjectProgress();
  empty.hidden = !document.getElementById("home-subject-progress")?.hidden;
  empty.textContent = !materials.length ? "Upload a document to start tracking your progress."
    : !homeDocStatesReady ? "Loading progress…"
    : "No study plan yet. Plan study sessions to see your progress by subject.";
}
