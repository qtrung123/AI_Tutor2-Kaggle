// Home: Continue learning, the materials list and Today's Study Plan.
// Classic script (shared global scope), loaded by index.html before app.js.

function renderDashboard() {
  if (!dashboardData) return;
  const materials = dashboardData.materials || [];
  renderSidebarRecentDocuments(materials);
  if (homeMaterialSearch) homeMaterialSearch.hidden = materials.length < HOME_SEARCH_MIN_MATERIALS;
  renderHomeDocumentCards();
  loadHomeDocumentStates(materials.map((material) => material.document_id));
}

async function loadHomeDocumentStates(documentIds) {
  const load = ++homeDocStatesLoad;
  const states = new Map();
  await Promise.all(documentIds.map(async (id) => {
    try {
      const progress = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(id)}?utc_offset_minutes=${plannerUtcOffsetMinutes()}`);
      if (progress?.learning) states.set(id, progress);
    } catch (error) {
      // no state for this document: its card simply shows fewer details
    }
  }));
  if (load !== homeDocStatesLoad) return;
  homeDocStates = states;
  renderHomeDocumentCards();
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

function renderHomeContinue(materials) {
  const panel = document.getElementById("home-continue");
  if (!panel) return;
  panel.innerHTML = "";
  panel.hidden = !materials.length;
  if (!materials.length) return;
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
  const materials = dashboardData?.materials || [];
  overviewMaterialsList.innerHTML = "";
  renderHomeContinue(materials);
  if (!materials.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No study materials yet. Upload a PDF or TXT file to start.";
    const upload = document.createElement("button");
    upload.type = "button";
    upload.className = "secondary-button empty-state-action";
    upload.textContent = "Upload material";
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

    const header = document.createElement("div");
    header.className = "home-doc-header";
    const title = document.createElement("h3");
    title.textContent = material.document_name || progress?.title || documentId;
    header.appendChild(title);
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
    actions.append(open, remove);

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
    overviewMaterialsList.innerHTML = '<div class="empty-state">Your materials could not be loaded right now.</div>';
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

// ---- Home: Today's Study Plan (read-only) ------------------------------------

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
  const upcoming = today.length ? null : remaining.find((session) => session.scheduled_start.slice(0, 10) > todayKey) || null;
  // Scheduled sessions whose time has passed: offered Reschedule / Skip, never silently dropped.
  const overdue = sessions.filter((session) => plannerIsOverdue(session, now))
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  return { today, upcoming, overdue };
}

function renderTodayPlan(sessions, progress = null) {
  if (!todayPlanPanel) return;
  todayPlanPanel.hidden = false;
  todayPlanPanel.innerHTML = "";
  const now = plannerNow();
  const heading = document.createElement("div");
  heading.className = "today-plan-heading";
  const titles = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = now.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  const title = document.createElement("h2");
  title.id = "today-plan-title";
  title.textContent = "Today’s Study Plan";
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

  if (sessions === null) { empty("Your study plan could not be loaded right now."); return; }
  const { today, upcoming, overdue } = plannerTodayAgenda(sessions, now);
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
    empty("Nothing left for today.");
    section(`Next session · ${plannerDayLabel(upcoming.scheduled_start.slice(0, 10))}`, "today-plan-upcoming")
      .appendChild(plannerSessionItem(upcoming, "div", { action: true }));
  } else if (!overdue.length) {
    empty("No upcoming study sessions. Create a plan to see what to study next.");
  }
  if (overdue.length) section("Not completed", "today-plan-overdue").appendChild(list(overdue));
}
