// Study Planner: plan steps, availability calendar, preview/confirm, sessions, adaptive replanning and the desktop calendar workspace.
// Classic script (shared global scope), loaded by index.html before app.js.

function plannerMondayOf(reference) {
  const date = new Date(reference);
  date.setHours(0, 0, 0, 0);
  const offset = (date.getDay() + 6) % 7; // getDay(): 0=Sun..6=Sat -> offset from Monday
  date.setDate(date.getDate() - offset);
  return date;
}

function plannerDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function plannerToMinutes(hhmm) {
  const [hour, minute] = hhmm.split(":").map(Number);
  return hour * 60 + minute;
}

function plannerEndMinutes(session) {
  // Minute the session ends, counted from its start day: one ending at 24:00 is stored as the
  // next day's 00:00 and must read as 1440, not 0.
  const end = plannerToMinutes(session.scheduled_end.slice(11, 16));
  return session.scheduled_end.slice(0, 10) > session.scheduled_start.slice(0, 10) ? end + 24 * 60 : end;
}

function plannerMinutesToLabel(minutes) {
  const hour = Math.floor(minutes / 60), minute = minutes % 60;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function plannerFormatDuration(totalMinutes) {
  const minutes = Math.max(0, Math.round(totalMinutes));
  const hours = Math.floor(minutes / 60), remainder = minutes % 60;
  if (hours && remainder) return `${hours}h ${remainder}m`;
  if (hours) return `${hours}h`;
  return `${remainder}m`;
}

function plannerFormatDateLabel(date) {
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function plannerDayLabel(dateKey) {
  const [year, month, day] = dateKey.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function plannerWeekDates(weekStart = plannerWeekStart) {
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(weekStart);
    date.setDate(date.getDate() + index);
    return date;
  });
}

function plannerNow() {
  // The browser's local clock drives "today" and "this week" (sessions are stored as naive
  // learner-local times). A single seam so tests can pin the date.
  return new Date();
}

function plannerLocalIso(date) {
  // Naive local "YYYY-MM-DDTHH:MM:SS", directly comparable with a session's scheduled_start/end.
  const time = [date.getHours(), date.getMinutes(), date.getSeconds()].map((part) => String(part).padStart(2, "0")).join(":");
  return `${plannerDateKey(date)}T${time}`;
}

function plannerIsActiveSession(session) {
  return PLANNER_ACTIVE_SESSION_STATUSES.includes(session.status);
}

function plannerUtcOffsetMinutes() {
  // The learner's own offset from UTC (e.g. 420 for UTC+7) -- the only timezone input the planner
  // API takes. "now" itself is left to the server (current UTC instant + this offset).
  return -new Date().getTimezoneOffset();
}

async function plannerRequest(url, options = {}) {
  // Like fetchJson, but keeps a structured error detail ({code, message, ...}) intact.
  const init = { ...options };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const response = await fetch(url, init);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    if (response.status === 401 && currentUser) showAuthentication();
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : detail?.message || `Request returned ${response.status}`;
    throw new PlannerRequestError(message, response.status, detail);
  }
  // Planner actions report their Google Calendar sync (only when connected); never blocking.
  if (payload?.calendar_sync) plannerNoteCalendarSync(payload.calendar_sync);
  return payload;
}

function plannerPlanUrl(suffix = "") {
  return `${PLANNER_PLANS_API_URL}/${encodeURIComponent(plannerPlan.plan_id)}${suffix}`;
}

async function loadPlannerData({ keepWeek = false } = {}) {
  const seq = ++plannerLoadSeq;   // a slower, older reload (e.g. two uploads finishing) never wins
  try {
    const [plans, availability] = await Promise.all([
      plannerRequest(PLANNER_PLANS_API_URL), plannerRequest(PLANNER_AVAILABILITY_API_URL),
    ]);
    if (seq !== plannerLoadSeq) return;
    const active = plans.filter((plan) => plan.status === "active");
    const nextPlan = active[active.length - 1] || null;
    const [detail, saved] = nextPlan
      ? await Promise.all([
        plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(nextPlan.plan_id)}`),
        plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(nextPlan.plan_id)}/sessions`),
      ])
      : [null, []];
    if (seq !== plannerLoadSeq) return;
    plannerAvailability = availability;
    plannerPlan = nextPlan;
    plannerMaterials = [];
    plannerSessions = [];
    plannerHistorySessions = [];
    plannerSessionsById = new Map();
    if (!keepWeek) plannerPlanWeekStart = null;
    if (plannerPlan) {
      const sessions = saved.map((session) => ({ ...session, plan_id: plannerPlan.plan_id }));
      plannerSessionsById = new Map(sessions.map((session) => [session.session_id, session]));
      plannerMaterials = detail.materials;
      plannerSessions = sessions.filter(plannerIsActiveSession);
      plannerHistorySessions = sessions.filter((session) => PLANNER_HISTORY_SESSION_STATUSES.includes(session.status));
    }
    if (plannerSessions.length || plannerHistorySessions.length) plannerStep = "plan";
    if (plannerPlan && plannerStep === "plan") loadPlannerPlanProgress(plannerPlan.plan_id);
    else if (plannerStep === "plan") plannerStep = "materials";
    if (!keepWeek) plannerCalWeekStart = null;
    if (plannerPlacements.length && plannerPlacementsPlanId !== plannerPlan?.plan_id) plannerPlacements = [];
    plannerPlacementsPlanId = plannerPlan?.plan_id || null;
    plannerLoaded = true;
    plannerLoadFailed = false;
    renderPlanner();
    if (plannerIsDesktop()) {
      plannerQueueAutoPreview(0);
      plannerLoadLiveCandidates();
      plannerLoadDocStates();
      plannerLoadPreferences();
    }
    plannerLoadGoogleCalendar();   // every layout: the toolbar Connect control must not need the desktop calendar
  } catch (error) {
    if (seq !== plannerLoadSeq) return;
    plannerLoadFailed = true;
    showToast(error.message || "Could not load Study Planner data");
    renderPlanner();   // the current interface says so (with a retry) instead of leaving an empty page
  }
}

// -- material added to an accepted plan ------------------------------------------------------------

async function plannerProposeNewMaterials(planId) {
  // Add Material on an accepted plan: propose sessions for the new material (adaptation trigger
  // "materials_added") in free time only, reviewed on the calendar like any suggested change --
  // accepted, hand-edited and completed sessions are never touched, nothing is saved until accepted.
  if (!planId) return;
  const seq = ++plannerNewMaterialSeq;   // a slower, older proposal never replaces a newer one
  const trigger = { kind: "materials_added" };
  let proposal = null;
  try {
    proposal = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/preview`, {
      method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    if (seq !== plannerNewMaterialSeq) return;
    // Read-only request: the accepted plan is untouched. Say so and offer a retry.
    plannerNewMaterialFailure = { planId };
    showToast("Could not suggest sessions for your new material. Try again in Study Planner.");
    if (plannerIsDesktop() && document.body.dataset.page === "planner") renderPlannerWorkspace();
    return;
  }
  if (seq !== plannerNewMaterialSeq) return;
  plannerNewMaterialFailure = null;
  if (!proposal?.change_count) {
    if (plannerIsDesktop() && document.body.dataset.page === "planner") renderPlannerWorkspace();
    return;
  }
  if (plannerIsDesktop()) plannerStartCalendarReview(planId, trigger, proposal);
  else plannerShowAdaptationReview(planId, trigger, proposal);
}

function plannerOpen() {
  // Opening the Planner tab: the current layout renders at once -- the last loaded plan, or the
  // calendar with a loading notice on the very first open -- then the plan is (re)loaded.
  plannerLoadFailed = false;
  renderPlanner();
  return loadPlannerData();
}

function plannerSetStep(step) {
  plannerStep = step;
  renderPlanner();
}

function renderPlanner() {
  if (!plannerView) return;
  // Desktop (>=1024px) plans on one calendar workspace; smaller screens keep the step-by-step flow.
  // Both start hidden in the markup: this is the only place that shows one of them, and it runs as
  // soon as the Planner opens (setPage), before any data has loaded.
  const desktop = plannerIsDesktop();
  if (plannerShell) plannerShell.hidden = desktop;
  if (plannerWorkspace) plannerWorkspace.hidden = !desktop;
  if (desktop) {
    renderPlannerWorkspace();
    return;
  }
  const current = PLANNER_STEPS.indexOf(plannerStep);
  plannerView.querySelectorAll("[data-planner-step]").forEach((section) => {
    section.hidden = section.dataset.plannerStep !== plannerStep;
  });
  plannerView.querySelectorAll("[data-step-indicator]").forEach((item) => {
    const index = PLANNER_STEPS.indexOf(item.dataset.stepIndicator);
    item.classList.toggle("active", index === current);
    item.classList.toggle("done", index < current);
    if (index === current) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  if (plannerStep === "materials") renderPlannerMaterials();
  if (plannerStep === "availability") renderPlannerCalendar();
  if (plannerStep === "preview") renderPlannerPreview();
  if (plannerStep === "plan") renderPlannerPlan();
}

// ---- Step 1: materials + optional deadlines --------------------------------

function plannerMaterialFor(documentId) {
  return plannerMaterials.find((material) => material.document_id === documentId) || null;
}

function renderPlannerMaterials() {
  if (!plannerMaterialList) return;
  plannerMaterialList.innerHTML = "";
  plannerMaterialEmpty.hidden = indexedDocuments.length > 0;
  const today = plannerDateKey(new Date());
  indexedDocuments.forEach((doc) => {
    const material = plannerMaterialFor(doc.id);
    const row = document.createElement("div");
    row.className = "planner-material-row";
    row.classList.toggle("selected", Boolean(material));
    const pick = document.createElement("label");
    pick.className = "planner-material-pick";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(material);
    checkbox.dataset.documentId = doc.id;
    checkbox.addEventListener("change", () => plannerToggleMaterial(doc.id, checkbox));
    const name = document.createElement("span");
    name.textContent = doc.title;
    pick.append(checkbox, name);
    row.appendChild(pick);
    if (material) {
      const goal = document.createElement("div");
      goal.className = "planner-material-goal";
      const summary = document.createElement("span");
      summary.className = `planner-goal-summary planner-goal-summary--${plannerMaterialGoal(material) || "none"}`;
      summary.textContent = plannerGoalSummary(material);
      const { form, read } = plannerGoalForm(plannerGoalValues(material), { today, title: doc.title });
      const save = document.createElement("button");
      save.type = "button";
      save.className = "secondary-button planner-goal-save";
      save.textContent = "Save goal";
      save.addEventListener("click", () => plannerSaveStepGoal(material, read(), save));
      goal.append(summary, form, save);
      if (plannerMaterialHasGoal(material)) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "text-button planner-goal-remove";
        remove.textContent = "Remove goal";
        remove.addEventListener("click", () => plannerSaveStepGoal(material, PLANNER_NO_GOAL, remove));
        goal.appendChild(remove);
      }
      row.appendChild(goal);
    }
    plannerMaterialList.appendChild(row);
  });
  plannerToAvailabilityButton.disabled = plannerMaterials.length === 0;
}

async function plannerEnsurePlan() {
  if (!plannerPlan) {
    plannerPlan = await plannerRequest(PLANNER_PLANS_API_URL, { method: "POST", body: { title: "My study plan" } });
  }
  return plannerPlan;
}

async function plannerToggleMaterial(documentId, checkbox) {
  checkbox.disabled = true;
  try {
    if (checkbox.checked) {
      await plannerEnsurePlan();
      const material = await plannerRequest(plannerPlanUrl("/materials"), { method: "POST", body: { document_id: documentId } });
      plannerMaterials = [...plannerMaterials, material];
    } else {
      const material = plannerMaterialFor(documentId);
      if (material) {
        await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), { method: "DELETE" });
      }
      plannerMaterials = plannerMaterials.filter((item) => item.document_id !== documentId);
    }
    plannerPreview = null;
  } catch (error) {
    showToast(error.message || "Could not update the plan");
  }
  renderPlannerMaterials();
}

async function plannerSaveStepGoal(material, values, button) {
  if (!values) return;
  button.disabled = true;
  try {
    await plannerPatchGoal(material, values);
    plannerPreview = null;
  } catch (error) {
    button.disabled = false;
    showToast(error.message || "Could not save the learning goal");
    return;
  }
  renderPlannerMaterials();
}

// ---- Study goals (one per material) -------------------------------------------
// "deadline" = finish by a required date; "flexible" = no deadline, sessions may spread over the
// available days. The target completion date is optional and only a preference (never after the
// deadline). A material saved before goals existed has goal_type null: it reads as a deadline goal
// when it already had a deadline, otherwise as "No goal set" -- nothing is invented for it.
// Saving a goal never changes accepted sessions; see plannerSuggestAdaptation.

const PLANNER_GOAL_LABELS = { deadline: "Has deadline", flexible: "Flexible learning" };
const PLANNER_NO_GOAL = { goal_type: null, deadline: null, target_date: null };
let plannerGoalFormCount = 0;

function plannerMaterialGoal(material) {
  return material?.goal_type || (material?.deadline ? "deadline" : null);
}

function plannerMaterialHasGoal(material) {
  return Boolean(material.goal_type || material.deadline || material.target_date);
}

function plannerGoalValues(material) {
  return { goal_type: plannerMaterialGoal(material), deadline: material.deadline || null, target_date: material.target_date || null };
}

function plannerShortDate(key) {
  return new Date(`${key}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function plannerGoalSummary(material) {
  const goal = plannerMaterialGoal(material);
  const parts = [goal === "deadline" ? `Due ${plannerShortDate(material.deadline)}` : goal === "flexible" ? "Flexible" : "No goal set"];
  if (material.target_date) parts.push(`Target ${plannerShortDate(material.target_date)}`);
  return parts.join(" · ");
}

function plannerGoalForm(values, { today, title, onChange = null }) {
  // The goal choice plus its two dates. `read()` validates and returns the POST/PATCH fields (or
  // null, with the problem shown inline); `values()` returns the raw, unvalidated state.
  const name = `planner-goal-${++plannerGoalFormCount}`;
  const form = document.createElement("fieldset");
  form.className = "planner-goal-form";
  const legend = document.createElement("legend");
  legend.textContent = "Learning goal";
  const choices = document.createElement("div");
  choices.className = "planner-goal-choices";
  const radios = {};
  Object.entries(PLANNER_GOAL_LABELS).forEach(([value, label]) => {
    const option = document.createElement("label");
    option.className = "planner-goal-choice";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = name;
    radio.value = value;
    radio.checked = values.goal_type === value;
    radio.setAttribute("aria-label", `${label} for ${title}`);
    const text = document.createElement("span");
    text.textContent = label;
    option.append(radio, text);
    choices.appendChild(option);
    radios[value] = radio;
  });
  const dateField = (caption, value, className) => {
    const field = document.createElement("label");
    field.className = `planner-goal-date ${className}`;
    const text = document.createElement("span");
    text.textContent = caption;
    const input = document.createElement("input");
    input.type = "date";
    input.value = value || "";
    input.setAttribute("aria-label", `${caption} for ${title}`);
    field.append(text, input);
    return { field, input };
  };
  const deadline = dateField("Deadline", values.deadline, "planner-goal-deadline");
  const target = dateField("Target completion (optional)", values.target_date, "planner-goal-target");
  const error = document.createElement("p");
  error.className = "planner-goal-error";
  error.setAttribute("role", "alert");
  error.hidden = true;
  const goal = () => Object.keys(radios).find((key) => radios[key].checked) || null;
  const raw = () => ({ goal_type: goal(), deadline: deadline.input.value || null, target_date: target.input.value || null });
  const sync = () => {
    const hasDeadline = goal() === "deadline";
    deadline.field.hidden = !hasDeadline;
    deadline.input.required = hasDeadline;
    // The picker suggests today on; a past deadline is reported by the preview (deadline_passed).
    if (!deadline.input.value || deadline.input.value >= today) deadline.input.min = today;
    else deadline.input.removeAttribute("min");
    if (hasDeadline && deadline.input.value) target.input.max = deadline.input.value;
    else target.input.removeAttribute("max");
  };
  [...Object.values(radios), deadline.input, target.input].forEach((control) => control.addEventListener("change", () => {
    sync();
    error.hidden = true;
    onChange?.(raw());
  }));
  sync();
  form.append(legend, choices, deadline.field, target.field, error);
  const read = () => {
    const { goal_type: goalType, deadline: due, target_date: targetDate } = raw();
    const result = { goal_type: goalType, deadline: goalType === "deadline" ? due : null, target_date: targetDate };
    let message = "";
    if (!goalType) message = "Choose a learning goal.";
    else if (goalType === "deadline" && !due) message = "Pick the deadline date.";
    else if (result.deadline && targetDate && targetDate > result.deadline) message = "The target completion date can’t be after the deadline.";
    error.textContent = message;
    error.hidden = !message;
    return message ? null : result;
  };
  return { form, read, values: raw };
}

async function plannerPatchGoal(material, values) {
  // One PATCH carries the whole goal, so the server validates it as a unit.
  const updated = await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), {
    method: "PATCH", body: { goal_type: values.goal_type, deadline: values.deadline, target_date: values.target_date },
  });
  plannerMaterials = plannerMaterials.map((item) => (item.material_id === updated.material_id ? { ...item, ...updated } : item));
  return updated;
}

async function plannerSuggestAdaptation(planId, trigger) {
  // A goal or deadline change never moves, adds or cancels accepted sessions by itself: the
  // planner only proposes (read-only preview) and the learner reviews it before anything changes.
  if (!planId) return null;
  let proposal = null;
  try {
    proposal = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/preview`, {
      method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    return null;   // the goal itself is saved; a suggestion is only a bonus
  }
  if (!proposal?.change_count) return proposal;
  if (plannerIsDesktop()) plannerStartCalendarReview(planId, trigger, proposal);
  else plannerShowAdaptationReview(planId, trigger, proposal);
  return proposal;
}

// ---- Step 2: availability (the existing weekly calendar) --------------------

function plannerAvailabilityCoversDate(slot, dateKey, weekday) {
  return slot.is_recurring ? slot.day_of_week === weekday : slot.date === dateKey;
}

function plannerIsMinuteAvailable(dateKey, weekday, minute) {
  return plannerAvailability.some((slot) => {
    if (!plannerAvailabilityCoversDate(slot, dateKey, weekday)) return false;
    return minute >= plannerToMinutes(slot.start_at) && minute < plannerToMinutes(slot.end_at);
  });
}

function plannerSessionAt(dateKey, minute) {
  return plannerSessions.find((session) => {
    if (session.scheduled_start.slice(0, 10) !== dateKey) return false;
    const start = plannerToMinutes(session.scheduled_start.slice(11, 16));
    const end = plannerEndMinutes(session);
    return minute < end && minute + PLANNER_CELL_MINUTES > start;
  });
}

function renderPlannerCalendar() {
  if (!plannerCalendar) return;
  const weekDates = plannerWeekDates();
  if (plannerWeekLabel) {
    plannerWeekLabel.textContent = `${plannerFormatDateLabel(weekDates[0])} – ${plannerFormatDateLabel(weekDates[6])}`;
  }
  plannerCalendar.innerHTML = "";

  const header = document.createElement("div");
  header.className = "planner-grid-header";
  header.appendChild(document.createElement("div")).className = "planner-grid-corner";
  weekDates.forEach((date) => {
    const cell = document.createElement("div");
    cell.className = "planner-grid-day-label";
    cell.textContent = `${PLANNER_DAY_LABELS[(date.getDay() + 6) % 7]} ${date.getDate()}`;
    header.appendChild(cell);
  });
  plannerCalendar.appendChild(header);

  const body = document.createElement("div");
  body.className = "planner-grid-body";
  for (let minute = PLANNER_GRID_START_MINUTE; minute < PLANNER_GRID_END_MINUTE; minute += PLANNER_CELL_MINUTES) {
    const rowLabel = document.createElement("div");
    rowLabel.className = "planner-grid-time-label";
    if (minute % 60 === 0) rowLabel.textContent = plannerMinutesToLabel(minute);
    body.appendChild(rowLabel);
    weekDates.forEach((date) => {
      const dateKey = plannerDateKey(date);
      const weekday = (date.getDay() + 6) % 7;
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "planner-cell";
      cell.dataset.date = dateKey;
      cell.dataset.weekday = String(weekday);
      cell.dataset.minute = String(minute);
      cell.setAttribute("aria-label", `${dateKey} ${plannerMinutesToLabel(minute)}`);
      cell.classList.toggle("available", plannerIsMinuteAvailable(dateKey, weekday, minute));
      const session = plannerSessionAt(dateKey, minute);
      if (session) {
        // A saved session: shown on the calendar, not paintable.
        cell.classList.add("block-confirmed");
        cell.title = `${session.document_title || session.document_id} · ${pcalActivity(session.activity_type, session.steps)}`;
        if (plannerSessionAt(dateKey, minute - PLANNER_CELL_MINUTES) !== session) {  // first cell of the session
          const label = document.createElement("span");
          label.className = "planner-cell-block-label";
          label.textContent = session.document_title || session.document_id;
          cell.appendChild(label);
        }
      } else {
        cell.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          plannerStartDrag(cell);
        });
      }
      body.appendChild(cell);
    });
  }
  plannerCalendar.appendChild(body);
}

function plannerStartDrag(cell) {
  const minute = Number(cell.dataset.minute);
  plannerDrag = {
    date: cell.dataset.date, weekday: Number(cell.dataset.weekday),
    start: minute, end: minute + PLANNER_CELL_MINUTES, mode: plannerMode,
  };
  cell.classList.add("dragging");
}

function plannerExtendDrag(cell) {
  if (!plannerDrag || cell.dataset.date !== plannerDrag.date) return;
  const minute = Number(cell.dataset.minute);
  plannerDrag.start = Math.min(plannerDrag.start, minute);
  plannerDrag.end = Math.max(plannerDrag.end, minute + PLANNER_CELL_MINUTES);
  document.querySelectorAll(`.planner-cell[data-date="${plannerDrag.date}"]`).forEach((element) => {
    const elementMinute = Number(element.dataset.minute);
    element.classList.toggle("dragging", elementMinute >= plannerDrag.start && elementMinute < plannerDrag.end);
  });
}

async function plannerFinishDrag() {
  if (!plannerDrag) return;
  const drag = plannerDrag;
  plannerDrag = null;
  document.querySelectorAll(".planner-cell.dragging").forEach((element) => element.classList.remove("dragging"));
  const payload = {
    start_at: plannerMinutesToLabel(drag.start), end_at: plannerMinutesToLabel(drag.end),
    is_recurring: Boolean(plannerRepeatWeeklyCheckbox?.checked),
  };
  if (payload.is_recurring) payload.day_of_week = drag.weekday; else payload.date = drag.date;
  const url = drag.mode === "erase" ? `${PLANNER_AVAILABILITY_API_URL}/remove` : PLANNER_AVAILABILITY_API_URL;
  try {
    plannerAvailability = await plannerRequest(url, { method: "POST", body: payload });
    plannerPreview = null;
    renderPlannerCalendar();
  } catch (error) {
    showToast(error.message || "Could not update availability");
  }
}

// ---- Steps 3 & 4: preview, confirm, saved plan ------------------------------

function plannerGroupByDay(sessions) {
  const groups = new Map();
  sessions.forEach((session) => {
    const key = session.scheduled_start.slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(session);
  });
  return [...groups.entries()];
}

function plannerSessionItem(session, tag = "li", { action = false } = {}) {
  // Narrow screens list sessions as agenda cards grouped by day (renderPlannerSessionList).
  const item = document.createElement(tag);
  item.className = `planner-session planner-session--${session.activity_type} planner-session--status-${session.status || "suggested"}${session.steps?.length ? " planner-session--combined" : ""}`;
  const time = document.createElement("span");
  time.className = "planner-session-time";
  time.textContent = `${session.scheduled_start.slice(11, 16)}–${session.scheduled_end.slice(11, 16)}`;
  const content = document.createElement("div");
  content.className = "planner-session-body";
  const title = document.createElement("strong");
  const fullTitle = session.document_title || session.document_id;
  title.textContent = plannerDisplayTitle(fullTitle);
  if (title.textContent !== fullTitle) title.title = fullTitle;
  const meta = document.createElement("span");
  meta.className = "planner-session-meta";
  const chip = document.createElement("span");
  chip.className = "planner-activity-chip";
  chip.textContent = pcalActivity(session.activity_type, session.steps);
  if (session.activity_type === "written_quiz") chip.textContent += " · Retrieval practice";
  const duration = document.createElement("span");
  duration.textContent = plannerFormatDuration(session.duration_minutes);
  meta.append(chip, duration);
  if (session.status === "in_progress") meta.prepend(Object.assign(document.createElement("span"), { className: "planner-session-live", textContent: "In progress" }));
  const reason = document.createElement("small");
  reason.className = "planner-session-reason";
  reason.textContent = session.reason?.message || "";
  content.append(title, meta, reason);
  item.append(time, content);
  if (action && session.session_id) plannerAddSessionActions(item, content, session, `${fullTitle} (${chip.textContent})`);
  return item;
}

function plannerIsOverdue(session, now = plannerNow()) {
  // A scheduled session whose end has passed on the browser's local clock (both are naive local times).
  return session.status === "scheduled" && session.scheduled_end <= plannerLocalIso(now);
}

function plannerAddSessionActions(item, content, session, name) {
  if (PLANNER_STATUS_LABELS[session.status]) {
    const badge = document.createElement("span");
    badge.className = `planner-session-status planner-session-status--${session.status}`;
    badge.textContent = PLANNER_STATUS_LABELS[session.status];
    item.classList.add("planner-session--done");
    item.appendChild(badge);
    return;
  }
  let actions = [];
  if (session.status === "in_progress") {
    actions = plannerCurrentStep(session) === null ? [["complete", "Complete", true], ["start", "Resume", false]]
      : plannerSessionActions("active", session);
  }
  else if (plannerIsOverdue(session)) {
    const note = document.createElement("small");
    note.className = "planner-session-note";
    note.textContent = "Session not completed";
    content.appendChild(note);
    item.classList.add("planner-session--overdue");
    actions = [["reschedule", "Reschedule", true], ["skip", "Skip", false]];
  } else if (session.status === "scheduled") actions = [["start", "Start", true]];
  if (!actions.length) return;
  item.classList.add("planner-session--actionable");
  const group = document.createElement("div");
  group.className = "planner-session-actions";
  actions.forEach(([kind, label, primary]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `${primary ? "primary-button" : "secondary-button"} planner-session-action planner-session-${kind}`;
    button.dataset.sessionAction = kind;
    button.dataset.sessionId = session.session_id;
    button.textContent = label;
    button.setAttribute("aria-label", `${label} ${name}`);
    button.addEventListener("click", () => plannerSessionAction(session, kind, button, group));
    group.appendChild(button);
  });
  item.appendChild(group);
}

async function plannerQuizTarget(session) {
  // The exact quiz a planned quiz / quiz_retry session is about: its own artifact (the quiz to
  // take or retake), else the document's quiz already in progress. null = the Quiz library.
  // A written_quiz session only ever opens its own flashcard practice quiz (set by the backend
  // when the session starts) -- never a normal assessment quiz.
  if (session.activity_type === "written_quiz" && !session.artifact_id) return null;
  await loadQuizStatuses();
  const variants = (quizStatuses.find((item) => item.document_id === session.document_id)?.variants || [])
    .filter((variant) => variant.quiz_id);
  const byRecent = (left, right) => new Date(right.updated_at || right.created_at || 0) - new Date(left.updated_at || left.created_at || 0);
  const variant = session.artifact_id
    ? variants.find((item) => item.quiz_id === session.artifact_id)
    : variants.filter((item) => !item.practice && item.progress_status === "in_progress").sort(byRecent)[0];   // flashcard practice is not the planned quiz
  const quizId = session.artifact_id || variant?.quiz_id;
  if (!quizId) return null;
  return { document_id: session.document_id, topic_id: variant?.topic_id || "document", difficulty: variant?.difficulty, quiz_id: quizId };
}

// The started session a quiz is being taken for: sent with the submission as planner_session_id
// (the server verifies it and completes the quiz part). Kept per tab in sessionStorage, so a refresh
// during the quiz still completes the right session.
const PLANNER_QUIZ_SESSION_KEY = "aiTutor.plannerQuizSession";

function plannerHasQuizActivity(session) {
  const activities = session.steps?.length ? session.steps.map((step) => step.activity_type) : [session.activity_type];
  return activities.some((activity) => ["quiz", "quiz_retry", "written_quiz"].includes(activity));
}

function plannerRememberQuizSession(session) {
  try {
    if (session.status === "in_progress" && plannerHasQuizActivity(session)) {
      sessionStorage.setItem(PLANNER_QUIZ_SESSION_KEY, JSON.stringify({ session_id: session.session_id, document_id: session.document_id }));
    } else sessionStorage.removeItem(PLANNER_QUIZ_SESSION_KEY);   // another session started: the old link no longer applies
  } catch (error) { /* storage unavailable: the server can still match a session that planned this exact quiz */ }
}

function plannerForgetQuizSession(sessionId) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(PLANNER_QUIZ_SESSION_KEY) || "null");
    if (saved && (!sessionId || saved.session_id === sessionId)) sessionStorage.removeItem(PLANNER_QUIZ_SESSION_KEY);
  } catch (error) { /* nothing to forget */ }
}

// The remembered session for a quiz on this document (never another document's session).
function plannerQuizSessionFor(documentId) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(PLANNER_QUIZ_SESSION_KEY) || "null");
    return saved && saved.document_id === documentId ? saved.session_id : null;
  } catch (error) { return null; }
}

// After a submitted quiz: what the server did to its planned session (planner_completion).
async function plannerAfterQuizSubmission(completion) {
  if (!completion || !["completed", "unchanged"].includes(completion.status)) return;
  if (completion.session_completed) plannerForgetQuizSession(completion.session_id);
  if (completion.status !== "completed") return;
  const session = completion.session;
  const pending = session?.steps?.find((step) => step.status !== "completed");
  showToast(pending ? `Quiz step done. Finish ${pcalActivity(pending.activity_type)} to complete the session`
    : "Study session completed. Nice work!");
  // Never surfaces as a submit error: the attempt and the session are both saved already.
  try { await plannerRefreshSchedules(); } catch (error) { /* the next Home / Planner load shows it */ }
}

async function plannerSessionAction(session, kind, button, group) {
  // One request per session at a time: a double click (or the same session in two lists) acts once.
  if (plannerBusySessions.has(session.session_id)) return;
  if (kind === "edit") {   // the learner's own date / time / length: a form, not a lifecycle call
    pcalOpenSessionEditor(session, button);
    return;
  }
  plannerBusySessions.add(session.session_id);
  const buttons = [...group.querySelectorAll("button")];
  const label = button.textContent;
  buttons.forEach((item) => { item.disabled = true; });
  button.textContent = kind === "start" ? "Opening…" : "Saving…";
  let refresh = kind !== "start";
  let skipped = null;   // after a Skip: whether it changed anything (then the plan may adapt)
  try {
    if (kind === "reschedule" && await plannerRescheduleThroughAdaptation(session)) {
      refresh = false;   // the adaptation already refreshed Home + week (or is waiting for the learner's review)
      return;
    }
    const step = kind === "step_done" ? plannerCurrentStep(session) : null;
    const url = kind === "step_done"
      ? `${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/steps/${step}/complete`
      : `${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/${kind}`;
    const body = kind === "reschedule" || kind === "start" ? { utc_offset_minutes: plannerUtcOffsetMinutes() } : undefined;
    const result = await plannerRequest(url, { method: "POST", body });
    if (kind === "start") {
      Object.assign(session, result.session);
      plannerSessions.forEach((item) => { if (item.session_id === session.session_id) Object.assign(item, result.session); });
      plannerRememberQuizSession(session);
      if (!indexedDocuments.some((item) => item.id === session.document_id)) {
        showToast("This document is no longer available");
        return;
      }
      // A missing artifact is fine: the tool's own empty state offers to generate/create it.
      if (result.tool === "quiz") {
        const quizTarget = await plannerQuizTarget(session);
        await openStudySession(session.document_id, "quiz", "", { quizTarget });
        // No quiz yet: the Quiz tab's unified Create Quiz sheet (nothing is generated until the
        // learner asks). Without flashcards it shows "Create Flashcards first" with a link to them.
        if (!quizTarget && !result.artifact_available && typeof openQuizCreateDialog === "function") openQuizCreateDialog();
        if (result.prerequisite === "flashcards") showToast("Create Flashcards first, then create the quiz");
      } else await openStudySession(session.document_id, result.tool);
    } else if (kind === "complete") {
      plannerForgetQuizSession(session.session_id);
      showToast("Session completed. Nice work!");
    }
    else if (kind === "step_done") {
      const next = plannerCurrentStep(result.session);
      showToast(result.session_completed ? "Session completed. Nice work!"
        : `${pcalActivity(session.steps[step].activity_type)} done. Next: ${pcalActivity(result.session.steps[next].activity_type)}`);
    }
    else if (kind === "skip") {
      plannerForgetQuizSession(session.session_id);
      skipped = { changed: Boolean(result.changed) };
    }
    else {
      const moved = result.session;
      showToast(`Moved to ${plannerDayLabel(moved.scheduled_start.slice(0, 10))}, ${moved.scheduled_start.slice(11, 16)}`
        + (result.after_deadline ? " (after the deadline)" : ""));
    }
  } catch (error) {
    showToast(error.message || "Could not update this session");
    refresh = refresh || error.status === 409 || error.status === 404;
  } finally {
    plannerBusySessions.delete(session.session_id);
    if (button.isConnected) {
      buttons.forEach((item) => { item.disabled = false; });
      button.textContent = kind === "start" && session.status === "in_progress" ? "Resume" : label;
    }
  }
  if (refresh) await plannerRefreshSchedules();
  if (skipped?.changed) {
    await plannerAdapt(session.plan_id, { kind: "session_skipped", session_id: session.session_id }, { prefix: "Session skipped" });
  } else if (skipped) showToast("Session skipped");
}

function plannerDocumentTitle(documentId) {
  return indexedDocuments.find((item) => item.id === documentId)?.title || documentId;
}

function plannerActivityWord(activity, steps = null) {
  return pcalActivity(activity, steps).toLowerCase();
}

function plannerShortWhen(iso) {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return `${new Date(year, month - 1, day).toLocaleDateString(undefined, { weekday: "short" })} ${iso.slice(11, 16)}`;
}

function plannerLongWhen(iso) {
  return `${plannerDayLabel(iso.slice(0, 10))}, ${iso.slice(11, 16)}`;
}

function plannerAdaptationSummary(result) {
  const parts = [
    ...result.moved.map((item) => `${plannerActivityWord(item.activity_type, item.steps)} moved to ${plannerShortWhen(item.to_start)}`),
    ...result.added.map((item) => `${plannerActivityWord(item.activity_type, item.steps)} added ${plannerShortWhen(item.scheduled_start)}`),
    ...result.cancelled.map((item) => `${plannerActivityWord(item.activity_type, item.steps)} no longer needed`),
  ];
  const shown = parts.slice(0, 3).join(", ") + (parts.length > 3 ? `, +${parts.length - 3} more` : "");
  return `Plan adjusted: ${shown}.`;
}

function plannerAdaptationReason(message) {
  // Engine messages restate the action ("Move the review session for "X" to <date>: <why>."); the
  // panel already shows what and when, so keep only the why.
  const why = (message || "").replace(/^(Move|Cancel) the .*? session for ".*?"( to \d{4}-\d{2}-\d{2} \d{2}:\d{2})?: /, "");
  return why.charAt(0).toUpperCase() + why.slice(1);
}

async function plannerAdapt(planId, trigger, { prefix = "", quietErrors = true } = {}) {
  if (!planId) return null;
  if (plannerAdaptingPlans.has(planId)) return null;   // one adaptation per plan at a time
  plannerAdaptingPlans.add(planId);
  let result = null;
  try {
    result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/apply`, {
      method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    if (!quietErrors) throw error;
    if (prefix) showToast(prefix);
    return null;
  } finally {
    plannerAdaptingPlans.delete(planId);
  }
  // A newer answer for this plan replaces any proposal still waiting on the calendar.
  if (result && plannerAdaptReview?.planId === planId) plannerAdaptReview = null;
  if (result && plannerAdaptStale?.planId === planId) plannerAdaptStale = null;
  if (result?.applied) {
    showToast(prefix ? `${prefix}. ${plannerAdaptationSummary(result)}` : plannerAdaptationSummary(result));
    await plannerRefreshSchedules();
  } else if (result?.requires_confirmation) {
    if (prefix) showToast(prefix);
    // Desktop reviews the proposal on the calendar itself; smaller screens keep the review panel.
    if (plannerIsDesktop()) plannerStartCalendarReview(planId, trigger, result, { prefix });
    else plannerShowAdaptationReview(planId, trigger, result);
  } else {
    if (prefix) showToast(prefix);
    if (result && plannerIsDesktop()) renderPlannerWorkspace();
  }
  return result;
}

async function plannerRescheduleThroughAdaptation(session) {
  // A session whose time has passed is a "missed" trigger: the planner finds its replacement and
  // shifts what depends on it. When it has nothing to change, the plain reschedule runs instead.
  if (!plannerIsOverdue(session)) return false;
  let result = null;
  try {
    result = await plannerAdapt(session.plan_id, { kind: "session_missed", session_id: session.session_id }, { quietErrors: false });
  } catch (error) {
    return false;
  }
  return Boolean(result && (result.applied || result.requires_confirmation));
}

async function plannerAdaptAfterQuiz(documentId) {
  // A completed quiz can re-shape that document's upcoming practice in any active plan holding it.
  try {
    const plans = (await plannerRequest(PLANNER_PLANS_API_URL)).filter((plan) => plan.status === "active");
    for (const plan of plans) {
      const detail = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(plan.plan_id)}`);
      if ((detail.materials || []).some((material) => material.document_id === documentId)) {
        await plannerAdapt(plan.plan_id, { kind: "quiz_completed", document_id: documentId });
      }
    }
  } catch (error) {
    // Adaptation is a bonus on top of the quiz result; the result itself is already saved.
  }
}

function plannerCloseAdaptationReview() {
  document.getElementById("adapt-review")?.remove();
  document.removeEventListener("keydown", plannerAdaptationReviewKeys);
}

function plannerAdaptationReviewKeys(event) {
  if (event.key === "Escape") document.getElementById("adapt-keep")?.click();
}

function plannerShowAdaptationReview(planId, trigger, proposal) {
  plannerCloseAdaptationReview();
  const backdrop = document.createElement("div");
  backdrop.className = "adapt-backdrop";
  backdrop.id = "adapt-review";
  const panel = document.createElement("section");
  panel.className = "adapt-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "adapt-title");
  const title = document.createElement("h2");
  title.id = "adapt-title";
  title.textContent = "Review plan changes";
  const intro = document.createElement("p");
  intro.className = "adapt-intro";
  intro.textContent = "Based on your recent study, we suggest a few changes. Nothing changes unless you accept.";
  panel.append(title, intro);

  const group = (label, items, describe) => {
    if (!items.length) return;
    const section = document.createElement("section");
    section.className = "adapt-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    const list = document.createElement("ul");
    items.forEach((item) => {
      const { when, reason } = describe(item);
      const row = document.createElement("li");
      row.className = "adapt-item";
      const name = document.createElement("strong");
      name.textContent = `${pcalActivity(item.activity_type, item.steps)} · ${plannerDocumentTitle(item.document_id)}`;
      const time = document.createElement("span");
      time.className = "adapt-when";
      time.textContent = when;
      const why = document.createElement("small");
      why.textContent = reason;
      row.append(name, time, why);
      list.appendChild(row);
    });
    section.append(heading, list);
    panel.appendChild(section);
  };
  group("New sessions", proposal.added, (item) => ({
    when: plannerLongWhen(item.scheduled_start), reason: plannerAdaptationReason(item.message) }));
  group("Moved", proposal.moved, (item) => ({
    when: `${plannerLongWhen(item.from_start)} → ${plannerLongWhen(item.to_start)}`, reason: plannerAdaptationReason(item.message) }));
  group("No longer needed", proposal.cancelled, (item) => ({
    when: `Removed from plan · was ${plannerLongWhen(item.scheduled_start)}`, reason: plannerAdaptationReason(item.message) }));
  if (proposal.warnings?.length) {
    const note = document.createElement("p");
    note.className = "adapt-note";
    note.textContent = "Some study time no longer fits before a deadline. Adding availability can help.";
    panel.appendChild(note);
  }
  const error = document.createElement("p");
  error.className = "adapt-error";
  error.setAttribute("role", "alert");
  error.hidden = true;
  const actions = document.createElement("div");
  actions.className = "adapt-actions";
  const keep = document.createElement("button");
  keep.type = "button";
  keep.id = "adapt-keep";
  keep.className = "secondary-button";
  keep.textContent = "Keep current plan";
  const accept = document.createElement("button");
  accept.type = "button";
  accept.id = "adapt-accept";
  accept.className = "primary-button";
  accept.textContent = "Accept changes";
  actions.append(keep, accept);
  panel.append(error, actions);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
  document.addEventListener("keydown", plannerAdaptationReviewKeys);

  let busy = false;
  keep.addEventListener("click", () => {
    if (busy) return;
    plannerCloseAdaptationReview();   // nothing is written
    showToast("Kept your current plan");
  });
  accept.addEventListener("click", async () => {
    if (busy) return;   // one apply per click burst
    busy = true;
    keep.disabled = accept.disabled = true;
    accept.textContent = "Applying…";
    error.hidden = true;
    try {
      const result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(planId)}/adaptation/apply`, {
        method: "POST", body: { trigger, utc_offset_minutes: plannerUtcOffsetMinutes(), confirm: true },
      });
      plannerCloseAdaptationReview();
      showToast(result.applied ? plannerAdaptationSummary(result) : "Your plan is already up to date");
      await plannerRefreshSchedules();
    } catch (failure) {
      error.textContent = failure.status === 409
        ? "Your plan changed in the meantime. Try again to use the latest version."
        : "Could not update your plan right now. Please try again.";
      error.hidden = false;
      busy = false;
      keep.disabled = accept.disabled = false;
      accept.textContent = "Accept changes";
    }
  });
  accept.focus();
}

// -- desktop: the calendar is the review surface --------------------------------
// A large proposal is drawn onto the week (new and moved-to sessions as ghosts; moving and
// no-longer-needed sessions muted in place). Nothing is written until Accept changes.

function plannerStartCalendarReview(planId, trigger, proposal, { prefix = "" } = {}) {
  plannerCloseAdaptationReview();
  plannerAdaptStale = null;
  plannerAdaptReview = { planId, trigger, proposal, busy: false, jump: true };
  const count = plannerReviewChanges().length;
  const text = `${count} schedule change${count === 1 ? "" : "s"} suggested`;
  if (state.page === "planner") {
    renderPlannerWorkspace();
    return;
  }
  // A session action elsewhere (Home) opens the calendar to show where things go; a finished quiz
  // leaves the learner where they are and the proposal waits in the Planner.
  if (trigger.kind === "quiz_completed" || trigger.kind === "materials_added") {
    showToast(`${text}. Review them in Study Planner.`);
    return;
  }
  setPage("planner");
  showToast(prefix ? `${prefix}. ${text}.` : `${text}.`);
}

function plannerReviewChanges(review = plannerAdaptReview) {
  // Every proposed change with the calendar dates it touches (a move touches two).
  if (!review) return [];
  const { added = [], moved = [], cancelled = [] } = review.proposal;
  return [
    ...added.map((item) => ({ type: "added", item, starts: [item.scheduled_start] })),
    ...moved.map((item) => ({ type: "moved", item, starts: [item.to_start, item.from_start] })),
    ...cancelled.map((item) => ({ type: "cancelled", item, starts: [item.scheduled_start] })),
  ];
}

function pcalReviewActive() {
  return Boolean(plannerAdaptReview && plannerPlan && plannerAdaptReview.planId === plannerPlan.plan_id);
}

function pcalDateOf(iso) {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function pcalReviewJump() {
  // First view of a proposal: when none of it is on the visible week, open the week of the first change.
  if (!pcalReviewActive() || !plannerAdaptReview.jump || state.page !== "planner") return;
  plannerAdaptReview.jump = false;
  const dates = pcalWeekDates();
  const fromKey = plannerDateKey(dates[0]), untilKey = plannerDateKey(dates[6]);
  const starts = plannerReviewChanges().flatMap((change) => change.starts);
  if (!starts.length || starts.some((iso) => iso.slice(0, 10) >= fromKey && iso.slice(0, 10) <= untilKey)) return;
  plannerCalWeekStart = plannerMondayOf(pcalDateOf(starts.sort()[0]));
}

function pcalReviewElsewhereText(fromKey, untilKey) {
  // "1 change next week": a pointer only -- the normal week controls get there.
  const visible = (iso) => iso.slice(0, 10) >= fromKey && iso.slice(0, 10) <= untilKey;
  const away = plannerReviewChanges().filter((change) => !change.starts.some(visible));
  if (!away.length) return "";
  const monday = plannerMondayOf(pcalDateOf(fromKey)).getTime();
  const offsets = new Set(away.map((change) =>
    Math.round((plannerMondayOf(pcalDateOf(change.starts[0])).getTime() - monday) / (7 * 86400000))));
  const count = `${away.length} change${away.length === 1 ? "" : "s"}`;
  if (offsets.size > 1) return `${count} in other weeks`;
  const [offset] = offsets;
  if (offset === 1) return `${count} next week`;
  if (offset === -1) return `${count} last week`;
  return offset > 0 ? `${count} in ${offset} weeks` : `${count} in an earlier week`;
}

function pcalRenderReviewBar(fromKey, untilKey) {
  const bar = pcal.review;
  if (!bar) return;
  const active = pcalReviewActive();
  bar.hidden = !active;
  bar.innerHTML = "";
  if (!active) return;
  const review = plannerAdaptReview;
  const count = plannerReviewChanges().length;
  const text = pcalEl("span", "pcal-review-text");
  text.appendChild(pcalEl("strong", "pcal-review-count", `${count} schedule change${count === 1 ? "" : "s"} suggested`));
  const elsewhere = pcalReviewElsewhereText(fromKey, untilKey);
  if (elsewhere) text.appendChild(pcalEl("span", "pcal-review-elsewhere", `· ${elsewhere}`));
  const actions = pcalEl("div", "pcal-review-actions");
  const keep = pcalEl("button", "pcal-button pcal-secondary", "Keep current plan");
  keep.type = "button";
  keep.id = "pcal-review-keep";
  const accept = pcalEl("button", "primary-button pcal-accept", review.busy ? "Applying…" : "Accept changes");
  accept.type = "button";
  accept.id = "pcal-review-accept";
  keep.disabled = accept.disabled = review.busy;
  keep.addEventListener("click", plannerKeepCalendarReview);
  accept.addEventListener("click", plannerAcceptCalendarReview);
  actions.append(keep, accept);
  bar.append(text, actions);
}

function plannerKeepCalendarReview() {
  if (!plannerAdaptReview || plannerAdaptReview.busy) return;
  plannerAdaptReview = null;   // nothing is written
  renderPlannerWorkspace();
  showToast("Kept your current plan");
}

async function plannerAcceptCalendarReview() {
  const review = plannerAdaptReview;
  if (!review || review.busy) return;   // one apply per click burst
  review.busy = true;
  pcalClosePopover();
  pcalRenderToolbar();
  try {
    const result = await plannerRequest(`${PLANNER_PLANS_API_URL}/${encodeURIComponent(review.planId)}/adaptation/apply`, {
      method: "POST", body: { trigger: review.trigger, utc_offset_minutes: plannerUtcOffsetMinutes(), confirm: true },
    });
    if (plannerAdaptReview === review) plannerAdaptReview = null;
    showToast(result.applied ? plannerAdaptationSummary(result) : "Your plan is already up to date");
    await plannerRefreshSchedules();
  } catch (failure) {
    if (plannerAdaptReview !== review) return;
    if (failure.status === 409) {
      // The plan changed since this proposal: drop it, show the saved schedule, offer a fresh look.
      plannerAdaptReview = null;
      plannerAdaptStale = { planId: review.planId, trigger: review.trigger };
      renderPlannerWorkspace();
      await plannerRefreshSchedules();
      return;
    }
    review.busy = false;
    pcalRenderToolbar();
    showToast("Could not update your plan right now. Please try again.");
  }
}

async function plannerRecheckAfterStale(button) {
  const stale = plannerAdaptStale;
  if (!stale) return;
  button.disabled = true;
  plannerAdaptStale = null;
  const result = await plannerAdapt(stale.planId, stale.trigger);
  if (result && !result.applied && !result.requires_confirmation) showToast("No changes needed. Your plan is up to date.");
  if (!result) pcalRenderNotice();
}

// Why a proposed change -- in the learner's words, built from the engine's own reason.
function plannerChangeWhy(type, item) {
  const message = item.message || "";
  if (type === "added") {
    const replaced = message.match(/^Replace the (missed|skipped) /);
    if (replaced) return `Replaces the ${replaced[1]} ${plannerActivityWord(item.activity_type, item.steps)} session.`;
    return PCAL_REASON_TEXT[item.reason_code] || plannerAdaptationReason(message);
  }
  if (/ no longer fits/.test(message)) {
    return / before \d{4}-\d{2}-\d{2}\.$/.test(message)
      ? "It no longer fits in your available time before the deadline."
      : "It no longer fits in your available time.";
  }
  const why = (message.match(/^(?:Move|Cancel) the .*? session for ".*?"(?: to \d{4}-\d{2}-\d{2} \d{2}:\d{2})?: (.*?)(?:, it is no longer needed)?\.$/) || [])[1];
  let text = why || "";
  const score = text.match(/^latest quiz scored ([\d.]+)%$/);
  const deadline = text.match(/^the deadline is now (\d{4}-\d{2}-\d{2})$/);
  if (score) text = `Your latest quiz scored ${score[1]}%.`;
  else if (deadline) text = `The deadline is now ${plannerDayLabel(deadline[1])}.`;
  else if (text) text = `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
  else text = plannerAdaptationReason(message);
  if (type === "cancelled") return `${text.replace(/\.$/, "")} — this session is no longer needed.`;
  return text;
}

function pcalDocumentTitle(documentId) {
  const saved = [...plannerSessionsById.values()].find((session) => session.document_id === documentId && session.document_title);
  return saved?.document_title || plannerDocumentTitle(documentId);
}

function pcalOpenChangePopover(change, session, block) {
  const { type, item } = change;
  const title = session.document_title || pcalDocumentTitle(session.document_id);
  pcalShowPopover(block, `${PCAL_CHANGE_KIND_LABELS[type]}: ${pcalActivity(session.activity_type, session.steps)} · ${title}`, (popover) => {
    const content = pcalEl("div", "pcal-popover-body");
    content.append(pcalEl("span", `pcal-kind pcal-kind--change-${type}`, PCAL_CHANGE_KIND_LABELS[type]),
      pcalEl("h4", "pcal-popover-title", title),
      pcalEl("p", "pcal-popover-activity", `${pcalActivity(session.activity_type, session.steps)} · ${plannerFormatDuration(session.duration_minutes)}`));
    const line = (caption, text) => {
      const row = pcalEl("p", "pcal-popover-when");
      row.append(pcalEl("span", "pcal-popover-caption", caption), document.createTextNode(text));
      content.appendChild(row);
    };
    const span = (start, end) => `${plannerDayLabel(start.slice(0, 10))} · ${start.slice(11, 16)}–${end.slice(11, 16)}`;
    if (type === "added") line("Proposed time", span(item.scheduled_start, item.scheduled_end));
    if (type === "moved" || type === "moving") {
      line("Proposed time", span(item.to_start, item.to_end));
      line("Currently", span(item.from_start, item.from_end));
    }
    if (type === "cancelled") line("Currently", span(session.scheduled_start, session.scheduled_end));
    const why = plannerChangeWhy(type === "moving" ? "moved" : type, item);
    if (why) {
      const reason = pcalEl("p", "pcal-popover-reason");
      reason.append(pcalEl("span", "pcal-popover-caption", "Why this change?"), document.createTextNode(` ${why}`));
      content.appendChild(reason);
    }
    content.appendChild(pcalEl("p", "pcal-popover-hint", "Nothing changes until you accept."));
    popover.appendChild(content);
  });
}

async function plannerRefreshSchedules() {
  // Home and the saved week both reflect the change; the week being viewed is kept.
  await Promise.all([loadTodayPlan(), plannerPlan ? loadPlannerData({ keepWeek: true }) : null]);
}

function renderPlannerSessionList(container, sessions, emptyText, options = {}) {
  container.innerHTML = "";
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  plannerGroupByDay(sessions).forEach(([dateKey, daySessions]) => {
    const day = document.createElement("section");
    day.className = "planner-day";
    const heading = document.createElement("h4");
    heading.textContent = plannerDayLabel(dateKey);
    const total = document.createElement("small");
    total.textContent = ` · ${plannerFormatDuration(daySessions.reduce((sum, item) => sum + item.duration_minutes, 0))}`;
    heading.appendChild(total);
    day.appendChild(heading);
    const list = document.createElement("ol");
    list.className = "planner-session-list";
    daySessions.forEach((session) => list.appendChild(plannerSessionItem(session, "li", options)));
    day.appendChild(list);
    container.appendChild(day);
  });
}

function plannerStat(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = plannerFormatDuration(value);
  wrapper.append(term, detail);
  return wrapper;
}

function plannerGoButton(label, step, className = "secondary-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", () => plannerSetStep(step));
  return button;
}

function renderPlannerPreview() {
  if (!plannerPreviewCapacity) return;
  plannerPreviewCapacity.innerHTML = "";
  const preview = plannerPreview;
  if (!preview) {
    plannerPreviewSessions.innerHTML = "";
    plannerConfirmButton.hidden = true;
    return;
  }
  const capacity = preview.capacity;
  const atRisk = capacity.status === "at_risk";
  const noAvailability = preview.warnings.some((warning) => warning.code === "no_availability");
  const box = document.createElement("div");
  box.className = `planner-capacity ${atRisk ? "at-risk" : "on-track"}`;
  const title = document.createElement("strong");
  title.textContent = atRisk ? "Not everything fits" : "Everything fits";
  const text = document.createElement("p");
  const count = preview.sessions.length;
  text.textContent = noAvailability
    ? "You haven’t selected any study time yet. Add some availability to get a plan."
    : atRisk
      ? "Your selected time can’t comfortably hold everything before the deadlines. Add study time, move a deadline, or confirm the part that fits."
      : `${plannerFormatDuration(capacity.scheduled_minutes)} of study in ${count} session${count === 1 ? "" : "s"}. The rest of your selected time stays free.`;
  box.append(title, text);
  const deadlineRisks = preview.warnings.filter((warning) => warning.code === "deadline_at_risk");
  if (deadlineRisks.length) {
    const list = document.createElement("ul");
    list.className = "planner-deadline-risks";
    deadlineRisks.forEach((risk) => {
      const item = document.createElement("li");
      const name = risk.document_title || risk.document_id;
      const due = risk.deadline ? ` before ${risk.deadline}` : "";
      const sessions = `${risk.unscheduled_count} session${risk.unscheduled_count === 1 ? "" : "s"}`;
      const final = risk.final_review_missing ? ", including the final review" : "";
      item.textContent = `${name}: ${sessions} (${plannerFormatDuration(risk.shortfall_minutes)}) don’t fit${due}${final}.`;
      list.appendChild(item);
    });
    box.appendChild(list);
  }
  if (atRisk) {
    const stats = document.createElement("dl");
    stats.className = "planner-capacity-stats";
    stats.append(
      plannerStat("Required", capacity.required_minutes),
      plannerStat("Schedulable", capacity.schedulable_minutes),
      plannerStat("Shortfall", capacity.shortfall_minutes),
    );
    const actions = document.createElement("div");
    actions.className = "planner-capacity-actions";
    actions.append(plannerGoButton("Add availability", "availability"), plannerGoButton("Adjust deadlines", "materials"));
    box.append(stats, actions);
  }
  plannerPreviewCapacity.appendChild(box);
  renderPlannerSessionList(plannerPreviewSessions, preview.sessions, "No sessions could be scheduled yet.");
  plannerConfirmButton.hidden = !count;
  plannerConfirmButton.disabled = plannerBusy;
  plannerConfirmButton.textContent = atRisk ? "Confirm partial plan" : "Looks good, confirm";
}

function plannerShowPreviewError(error) {
  plannerPreviewError.innerHTML = "";
  const message = document.createElement("p");
  message.textContent = error.message || "Could not build a preview";
  plannerPreviewError.appendChild(message);
  const documents = error.detail?.code === "deadline_passed" ? error.detail.documents || [] : [];
  if (documents.length) {
    const list = document.createElement("ul");
    documents.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = `${item.document_title || item.document_id}: ${item.deadline}`;
      list.appendChild(entry);
    });
    plannerPreviewError.append(list, plannerGoButton("Update deadlines", "materials", "text-button"));
  }
  plannerPreviewError.hidden = false;
}

async function plannerGeneratePreview() {
  if (plannerBusy || !plannerPlan) return;
  plannerBusy = true;
  plannerGeneratePreviewButton.disabled = true;
  plannerGeneratePreviewButton.textContent = "Generating…";
  plannerPreviewError.hidden = true;
  try {
    plannerPreview = await plannerRequest(plannerPlanUrl("/preview"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
  } catch (error) {
    plannerPreview = null;
    plannerShowPreviewError(error);
  } finally {
    plannerBusy = false;
    plannerGeneratePreviewButton.disabled = false;
    plannerGeneratePreviewButton.textContent = "Generate preview";
    plannerSetStep("preview");
  }
}

async function plannerConfirm() {
  if (plannerBusy || !plannerPlan || !plannerPreview?.sessions.length) return;
  plannerBusy = true;
  plannerConfirmButton.disabled = true;
  plannerConfirmButton.textContent = "Saving…";
  let alreadyConfirmed = false;
  try {
    // The server recomputes the schedule itself; only the learner's UTC offset is sent.
    const result = await plannerRequest(plannerPlanUrl("/confirm"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes() },
    });
    plannerSessions = result.sessions;
    plannerPlanWeekStart = null;
    plannerPreview = null;
    plannerStep = "plan";
    showToast("Study plan saved");
  } catch (error) {
    if (error.status === 409) alreadyConfirmed = true;
    else plannerShowPreviewError(error);
  } finally {
    plannerBusy = false;
    renderPlanner();
  }
  if (alreadyConfirmed) {
    showToast("This plan is already confirmed");
    await loadPlannerData();
  }
}

function renderPlannerPlan() {
  if (!plannerPlanSessions) return;
  const total = plannerSessions.reduce((sum, session) => sum + session.duration_minutes, 0);
  const days = new Set(plannerSessions.map((session) => session.scheduled_start.slice(0, 10))).size;
  plannerPlanSummary.textContent = plannerSessions.length
    ? `${plannerSessions.length} session${plannerSessions.length === 1 ? "" : "s"} · ${plannerFormatDuration(total)} across ${days} day${days === 1 ? "" : "s"}`
    : "No sessions saved yet.";
  if (!plannerPlanWeekStart) plannerPlanWeekStart = plannerDefaultPlanWeek(plannerSessions);
  const weekDates = plannerWeekDates(plannerPlanWeekStart);
  const fromKey = plannerDateKey(weekDates[0]);
  const untilKey = plannerDateKey(weekDates[6]);
  if (plannerPlanWeekLabel) {
    plannerPlanWeekLabel.textContent = `${plannerFormatDateLabel(weekDates[0])} – ${plannerFormatDateLabel(weekDates[6])}`;
  }
  const inWeek = (session) => {
    const key = session.scheduled_start.slice(0, 10);
    return key >= fromKey && key <= untilKey;
  };
  let emptyText = "No study sessions this week.";
  const next = plannerSessions.find((session) => session.scheduled_start.slice(0, 10) > untilKey);
  if (next) emptyText += ` Next session: ${plannerDayLabel(next.scheduled_start.slice(0, 10))}.`;
  const weekSessions = [...plannerSessions, ...plannerHistorySessions].filter(inWeek)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start));
  const anySaved = plannerSessions.length || plannerHistorySessions.length;
  renderPlannerSessionList(plannerPlanSessions, weekSessions, anySaved ? emptyText : "No sessions saved yet.", { action: true });
}

async function loadPlannerPlanProgress(planId) {
  if (!plannerPlanProgress) return;
  try {
    const progress = await plannerRequest(plannerPlanUrl(`/progress?utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    if (plannerPlan?.plan_id !== planId) return;
    const performance = progress.current_quiz_performance;
    const quizText = performance.average_percentage == null
      ? "no quiz results yet"
      : `quiz performance ${Math.round(performance.average_percentage)}% across ${performance.assessed_documents} of ${performance.documents} document${performance.documents === 1 ? "" : "s"}`;
    plannerPlanProgress.textContent = `${progress.completed_sessions} of ${progress.planned_sessions} sessions done · `
      + `${plannerFormatDuration(progress.completed_minutes)} studied, ${plannerFormatDuration(progress.remaining_minutes)} still planned · ${quizText}`;
    plannerPlanProgress.hidden = false;
  } catch (error) {
    plannerPlanProgress.hidden = true;
  }
}

function plannerDefaultPlanWeek(sessions) {
  // This week if it holds any saved session; otherwise the week of the next upcoming one.
  const thisWeek = plannerMondayOf(plannerNow());
  const thisWeekKey = plannerDateKey(thisWeek);
  const upcoming = sessions.filter((session) => session.scheduled_start.slice(0, 10) >= thisWeekKey)
    .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start))[0];
  return upcoming ? plannerMondayOf(new Date(upcoming.scheduled_start)) : thisWeek;
}

function plannerShiftPlanWeek(days) {
  const next = new Date(plannerPlanWeekStart || plannerMondayOf(plannerNow()));
  next.setDate(next.getDate() + days);
  plannerPlanWeekStart = next;
  renderPlannerPlan();
}

async function plannerLoadLiveCandidates() {
  // What a confirmed plan still wants scheduled (draggable from the queue). Server-computed only.
  if (!plannerPlan || !plannerHasLivePlan()) {
    plannerLiveCandidates = [];
    return;
  }
  const planId = plannerPlan.plan_id;
  try {
    const found = await plannerRequest(plannerPlanUrl(`/candidates?utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    if (plannerPlan?.plan_id !== planId) return;
    plannerLiveCandidates = Array.isArray(found) ? found : [];
  } catch (error) {
    plannerLiveCandidates = [];
  }
  if (plannerIsDesktop() && !pcalMove) pcalRenderQueue();
}

function plannerIsDesktop() {
  return Boolean(plannerDesktopQuery?.matches);
}

function plannerHasLivePlan() {
  // A confirmed plan with current work: the calendar shows it; nothing is previewed.
  return plannerSessions.length > 0;
}

function plannerReadyToPreview() {
  return Boolean(plannerPlan && plannerMaterials.length && plannerAvailability.length && !plannerHasLivePlan());
}

function pcalWeekDates() {
  if (!plannerCalWeekStart) plannerCalWeekStart = plannerMondayOf(plannerNow());
  return plannerWeekDates(plannerCalWeekStart);
}

function pcalEl(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function pcalSessionTimes(session) {
  return `${session.scheduled_start.slice(11, 16)}–${session.scheduled_end.slice(11, 16)}`;
}

function pcalShortDay(dateKey) {
  const [year, month, day] = dateKey.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return `${date.toLocaleDateString(undefined, { weekday: "short" })} ${day}`;
}

function pcalActivity(activity, steps = null) {
  // A combined session (two sequential steps) reads "Summary + Flashcards".
  if (steps?.length) return steps.map((step) => PLANNER_ACTIVITY_LABELS[step.activity_type] || step.activity_type).join(" + ");
  return PLANNER_ACTIVITY_LABELS[activity] || activity;
}

function plannerCurrentStep(session) {
  // The index of a combined session's first step not done yet; null when done or single-activity.
  if (!session?.steps?.length) return null;
  const index = session.steps.findIndex((step) => step.status !== "completed");
  return index < 0 ? null : index;
}

function plannerSessionActions(kind, session) {
  // The lifecycle buttons of a saved session. A combined session in progress opens its current
  // step's tool and marks that step done; the session completes with its last step.
  const actions = PCAL_SESSION_ACTIONS[kind] || [];
  const current = plannerCurrentStep(session);
  if (kind !== "active" || current === null) return actions;
  const step = pcalActivity(session.steps[current].activity_type);
  return [["start", `Open ${step}`, true], ["step_done", `${step} done`, false],
    ...actions.filter(([action]) => action !== "start" && action !== "complete")];
}

function pcalStepList(session) {
  // A combined session's two steps, in order, with their minutes and progress.
  const current = plannerCurrentStep(session);
  const list = pcalEl("ol", "pcal-steps");
  list.setAttribute("aria-label", "Session steps");
  session.steps.forEach((step, index) => {
    const done = step.status === "completed";
    const state = done ? "Done" : index === current && session.status === "in_progress" ? "Now" : index === current ? "First" : "Next";
    const item = pcalEl("li", `pcal-step${done ? " is-done" : ""}${index === current ? " is-current" : ""}`);
    item.append(pcalEl("span", "pcal-step-name", `${index + 1}. ${pcalActivity(step.activity_type)}`),
      pcalEl("span", "pcal-step-minutes", plannerFormatDuration(step.duration_minutes)),
      pcalEl("span", "pcal-step-state", state));
    list.appendChild(item);
  });
  return list;
}

function pcalMaterialTitle(material) {
  return material.document_title || plannerDocumentTitle(material.document_id);
}

// -- state changes that re-plan ------------------------------------------------

function plannerQueueAutoPreview(delay = PCAL_PREVIEW_DELAY_MS) {
  // Materials, deadlines and availability all feed the preview: any change re-runs it (debounced).
  clearTimeout(plannerPreviewTimer);
  if (!plannerReadyToPreview()) {
    plannerPreviewSeq += 1;   // drop any response still in flight
    plannerPreviewing = false;
    if (!plannerHasLivePlan()) plannerPreview = null;
    plannerPreviewFailure = null;
    if (plannerIsDesktop()) renderPlannerWorkspace();
    return;
  }
  plannerPreviewing = true;
  if (plannerIsDesktop()) renderPlannerWorkspace();
  plannerPreviewTimer = setTimeout(plannerRunPreview, delay);
}

async function plannerRunPreview() {
  if (!plannerReadyToPreview()) {
    plannerQueueAutoPreview();
    return;
  }
  const seq = ++plannerPreviewSeq;
  const planId = plannerPlan.plan_id;
  plannerPreviewing = true;
  pcalRenderToolbar();
  try {
    const result = await plannerRequest(plannerPlanUrl("/preview"), {
      method: "POST", body: plannerScheduleBody(),
    });
    if (seq !== plannerPreviewSeq || plannerPlan?.plan_id !== planId) return;
    plannerPreview = result;
    plannerPreviewFailure = null;
    plannerDropRejectedPlacements(result.placements?.rejected);
  } catch (error) {
    if (seq !== plannerPreviewSeq) return;
    plannerPreview = null;
    plannerPreviewFailure = error;
  }
  plannerPreviewing = false;
  if (plannerIsDesktop()) renderPlannerWorkspace();
}

function plannerScheduleBody() {
  // Only the learner's offset, plus their draft moves; the server recomputes and validates everything.
  const body = { utc_offset_minutes: plannerUtcOffsetMinutes() };
  if (plannerPlacements.length) body.placements = plannerPlacements.map((item) => ({ ...item }));
  return body;
}

function plannerDropRejectedPlacements(rejected) {
  if (!rejected?.length) return;
  const keys = new Set(rejected.map((item) => item.candidate_key));
  plannerPlacements = plannerPlacements.filter((item) => !keys.has(item.candidate_key));
  showToast(rejected[0].message || "That time is not available");
}

function plannerAfterAvailabilityChange() {
  if (!plannerHasLivePlan()) {
    plannerQueueAutoPreview();
    return;
  }
  // A confirmed plan: sessions that no longer fit move (existing adaptation API), once the edits settle.
  renderPlannerWorkspace();
  clearTimeout(plannerAvailabilityTimer);
  const planId = plannerPlan?.plan_id;
  plannerAvailabilityTimer = setTimeout(() => plannerAdapt(planId, { kind: "availability_changed" }), 800);
}

async function pcalAddMaterial(documentId, goal, button) {
  if (button) button.disabled = true;
  try {
    await plannerEnsurePlan();
    const body = { document_id: documentId };
    Object.entries(goal || {}).forEach(([key, value]) => { if (value) body[key] = value; });
    const material = await plannerRequest(plannerPlanUrl("/materials"), { method: "POST", body });
    plannerMaterials = [...plannerMaterials, material];
    pcalSheetGoals.delete(documentId);
    if (plannerHasLivePlan() && material.deadline) {
      await plannerSuggestAdaptation(plannerPlan.plan_id, { kind: "deadline_changed", document_id: documentId });
    } else if (plannerHasLivePlan()) {
      await plannerProposeNewMaterials(plannerPlan.plan_id);
    }
  } catch (error) {
    showToast(error.message || "Could not add this material");
  }
  plannerQueueAutoPreview();
}

async function pcalRemoveMaterial(material, button) {
  button.disabled = true;
  try {
    await plannerRequest(plannerPlanUrl(`/materials/${encodeURIComponent(material.material_id)}`), { method: "DELETE" });
    plannerMaterials = plannerMaterials.filter((item) => item.material_id !== material.material_id);
  } catch (error) {
    showToast(error.message || "Could not remove this material");
    button.disabled = false;
    return;
  }
  if (plannerHasLivePlan()) await loadPlannerData({ keepWeek: true });
  else plannerQueueAutoPreview();
}

async function pcalSaveGoal(material, values, button) {
  if (!values) return;
  button.disabled = true;
  const deadlineChanged = (values.deadline || null) !== (material.deadline || null);
  try {
    await plannerPatchGoal(material, values);
  } catch (error) {
    button.disabled = false;
    showToast(error.message || "Could not save the learning goal");
    return;
  }
  pcalGoalEditor = null;
  if (plannerHasLivePlan()) {
    renderPlannerWorkspace();
    // Accepted sessions stay as they are: a changed deadline only brings a proposal to review.
    if (deadlineChanged) await plannerSuggestAdaptation(plannerPlan.plan_id, { kind: "deadline_changed", document_id: material.document_id });
  } else plannerQueueAutoPreview();
}

async function pcalChangeAvailability(action, payload) {
  const url = action === "remove" ? `${PLANNER_AVAILABILITY_API_URL}/remove` : PLANNER_AVAILABILITY_API_URL;
  // The learner's offset lets the server refuse a dated slot in the past (it never guesses the timezone).
  const body = action === "remove" ? payload : { ...payload, utc_offset_minutes: plannerUtcOffsetMinutes() };
  plannerAvailability = await plannerRequest(url, { method: "POST", body });
}

function pcalSlotPayload(slot, dateKey) {
  return slot.is_recurring
    ? { start_at: slot.start_at, end_at: slot.end_at, is_recurring: true, day_of_week: slot.day_of_week }
    : { start_at: slot.start_at, end_at: slot.end_at, is_recurring: false, date: slot.date || dateKey };
}

async function plannerAcceptPlan() {
  if (plannerBusy || plannerPreviewing || !plannerPlan || !plannerPreview?.sessions.length || plannerHasLivePlan()) return;
  plannerBusy = true;
  clearTimeout(plannerPreviewTimer);
  pcalRenderToolbar();
  let alreadyConfirmed = false;
  try {
    // One atomic confirm; the server recomputes the same deterministic schedule from its own data.
    const result = await plannerRequest(plannerPlanUrl("/confirm"), {
      method: "POST", body: plannerScheduleBody(),
    });
    const planId = plannerPlan.plan_id;
    plannerSessions = result.sessions.map((session) => ({ ...session, plan_id: planId })).filter(plannerIsActiveSession);
    plannerPreview = null;
    plannerPreviewFailure = null;
    plannerPlacements = [];
    plannerLoadLiveCandidates();
    plannerStep = "plan";
    showToast("Study plan saved");
    loadPlannerPlanProgress(planId);
    loadTodayPlan();
  } catch (error) {
    if (error.detail?.code === "placement_rejected") {
      // A moved suggestion no longer fits: nothing was saved; show the refreshed suggestion instead.
      plannerBusy = false;
      plannerDropRejectedPlacements(error.detail.placements);
      plannerQueueAutoPreview(0);
      return;
    }
    if (error.status === 409) alreadyConfirmed = true;
    else plannerPreviewFailure = error;
  } finally {
    plannerBusy = false;
    renderPlannerWorkspace();
  }
  if (alreadyConfirmed) {
    showToast("This plan is already confirmed");
    await loadPlannerData({ keepWeek: true });
  }
}

async function pcalStartNewPlan() {
  if (!plannerPlan) return;
  try {
    // Archive the confirmed plan (its saved sessions stay in its history) and start fresh.
    await plannerRequest(plannerPlanUrl(), { method: "PATCH", body: { status: "archived" } });
    plannerPlan = null;
    plannerMaterials = [];
    plannerSessions = [];
    plannerHistorySessions = [];
    plannerPreview = null;
    plannerPlacements = [];
    plannerLiveCandidates = [];
    plannerStep = "materials";
    plannerQueueAutoPreview();
    loadTodayPlan();
  } catch (error) {
    showToast(error.message || "Could not start a new plan");
  }
}

// -- rendering -----------------------------------------------------------------

function renderPlannerWorkspace() {
  if (!plannerWorkspace || plannerWorkspace.hidden) return;
  pcalApplyView();
  pcalReviewJump();
  pcalClosePopover();
  pcalRenderMaterials();
  pcalRenderToolbar();
  pcalRenderGrid();
  pcalRenderQueue();
  pcalRenderSheet();
  pcalRenderGoogleCalendar();
}

// -- Materials / Study queue panels -------------------------------------------------
// Two icon buttons at the top right of the calendar header open these panels as popovers anchored
// under the buttons; the calendar always keeps its full width. One panel at a time; the same
// button, a click outside or Escape closes it. Panels start closed on every page load.
let pcalOpenPanel = null;   // "materials" | "queue" | "prefs" | null

function pcalSetPanel(panel, { focusToggle = false } = {}) {
  if (!plannerWorkspace) return;
  const previous = pcalOpenPanel;
  pcalOpenPanel = panel;
  if (previous === "materials" && panel !== "materials") pcalCloseSheet();
  ["materials", "queue", "prefs"].forEach((name) => {
    plannerWorkspace.classList.toggle(`is-${name}-open`, name === panel);
    document.getElementById(`pcal-${name}-toggle`)?.setAttribute("aria-expanded", String(name === panel));
  });
  if (panel === "prefs" && previous !== "prefs") pcalRenderPreferences();
  if (panel) pcalPositionPanel(panel);
  if (focusToggle && previous) document.getElementById(`pcal-${previous}-toggle`)?.focus();
}

function pcalTogglePanel(panel) {
  pcalClosePopover();
  pcalSetPanel(pcalOpenPanel === panel ? null : panel);
}

function pcalPositionPanel(panel) {
  // Right-aligned with the toolbar buttons, just below them.
  const rail = document.getElementById(`pcal-${panel}-rail`);
  const toggles = plannerWorkspace?.querySelector(".pcal-panel-toggles");
  if (!rail || !toggles) return;
  const box = plannerWorkspace.getBoundingClientRect(), anchor = toggles.getBoundingClientRect();
  const top = Math.round(anchor.bottom - box.top + 6);
  rail.style.top = `${top}px`;
  rail.style.right = `${Math.max(0, Math.round(box.right - anchor.right))}px`;
  rail.style.maxHeight = `${Math.max(160, Math.round(box.height - top - 10))}px`;
}

function pcalRenderMaterials() {
  pcal.materials.innerHTML = "";
  const today = plannerDateKey(plannerNow());
  plannerMaterials.forEach((material) => {
    const title = pcalMaterialTitle(material);
    const item = pcalEl("li", "pcal-material");
    item.dataset.documentId = material.document_id;
    const top = pcalEl("div", "pcal-material-top");
    const name = pcalEl("strong", "pcal-material-title", title);
    name.title = title;
    const remove = pcalEl("button", "pcal-material-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${title}`);
    remove.addEventListener("click", () => pcalRemoveMaterial(material, remove));
    top.append(name, remove);
    const meta = pcalEl("div", "pcal-material-meta");
    const stateKey = pcalLearningKey(material.document_id, material.learning_state || "new");
    meta.appendChild(pcalEl("span", `pcal-pill pcal-pill--${stateKey}`, PLANNER_LEARNING_STATE_LABELS[stateKey] || stateKey));
    const goal = plannerMaterialGoal(material);
    const summary = pcalEl("span", `pcal-goal pcal-goal--${goal || "none"}`, plannerGoalSummary(material));
    summary.classList.toggle("is-past", Boolean(material.deadline && material.deadline < today));
    const editing = pcalGoalEditor?.materialId === material.material_id;
    const edit = pcalEl("button", "pcal-goal-edit", goal ? "Edit goal" : "Set goal");
    edit.type = "button";
    edit.setAttribute("aria-label", `${goal ? "Edit" : "Set"} learning goal for ${title}`);
    edit.setAttribute("aria-expanded", String(editing));
    edit.addEventListener("click", () => {
      pcalGoalEditor = editing ? null : { materialId: material.material_id, values: plannerGoalValues(material) };
      pcalRenderMaterials();
      pcal.materials.querySelector(".pcal-goal-editor input")?.focus();
    });
    meta.append(summary, edit);
    item.append(top, meta);
    if (editing) item.appendChild(pcalGoalEditorFor(material, title, today));
    const pack = pcalStudyPackText(material.document_id, { compact: true });
    if (pack) item.appendChild(pcalEl("span", "pcal-material-pack", pack));
    pcal.materials.appendChild(item);
  });
  pcal.newPlan.hidden = !(plannerPlan && (plannerSessions.length || plannerHistorySessions.length));
}

async function plannerLoadDocStates() {
  const load = ++plannerDocStatesLoad;
  plannerDocStates = new Map();
  const ids = indexedDocuments.map((doc) => doc.id);
  await Promise.all(ids.map(async (id) => {
    try {
      const state = await plannerRequest(`${PROGRESS_API_BASE_URL}/${encodeURIComponent(id)}?utc_offset_minutes=${plannerUtcOffsetMinutes()}`);
      if (load === plannerDocStatesLoad && state?.learning) plannerDocStates.set(id, state);
    } catch (error) {
      // no state for this document: its details are simply not shown
    }
  }));
  if (load !== plannerDocStatesLoad || !plannerIsDesktop()) return;
  pcalRenderMaterials();
  pcalRenderSheet();
}

function pcalLearningKey(documentId, fallback = "new") {
  return plannerDocStates.get(documentId)?.learning.state || fallback;
}

function pcalStudyPackText(documentId, { compact = false } = {}) {
  // What the Study Pack actually holds; missing parts are said plainly, never assumed.
  const pack = plannerDocStates.get(documentId)?.study_pack;
  if (!pack) return "";
  const cards = pack.flashcard_count;
  if (compact) {
    const parts = [pack.summary_ready && "Summary", cards && `${cards} cards`, pack.quiz_count && "Quiz"].filter(Boolean);
    return parts.length ? parts.join(" · ") : "No study pack yet";
  }
  return [pack.summary_ready ? "Summary ready" : "No summary yet", cards ? `${cards} flashcard${cards === 1 ? "" : "s"}` : "No flashcards yet",
    pack.quiz_count ? "Quiz ready" : "No quiz yet"].join(" · ");
}

function pcalQuizText(documentId) {
  const quiz = plannerDocStates.get(documentId)?.quiz;
  if (!quiz) return "";
  return quiz.latest ? `Latest quiz ${Math.round(quiz.latest.percentage)}%` : "No quiz attempt yet";
}

// The Materials panel edits one goal at a time, inline under its material. The draft lives here so
// a re-render (document states loading, a preview finishing) never drops what is being typed.
let pcalGoalEditor = null;   // { materialId, values } | null

function pcalGoalEditorFor(material, title, today) {
  const box = pcalEl("div", "pcal-goal-editor");
  const { form, read } = plannerGoalForm(pcalGoalEditor.values, {
    today, title, onChange: (values) => { pcalGoalEditor.values = values; },
  });
  const actions = pcalEl("div", "pcal-goal-actions");
  const save = pcalEl("button", "pcal-button pcal-goal-save", "Save");
  save.type = "button";
  save.addEventListener("click", () => pcalSaveGoal(material, read(), save));
  const cancel = pcalEl("button", "pcal-text-action pcal-goal-cancel", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => {
    pcalGoalEditor = null;
    pcalRenderMaterials();
  });
  actions.append(save, cancel);
  if (plannerMaterialHasGoal(material)) {
    const remove = pcalEl("button", "pcal-text-action pcal-danger pcal-goal-remove", "Remove goal");
    remove.type = "button";
    remove.addEventListener("click", () => pcalSaveGoal(material, PLANNER_NO_GOAL, remove));
    actions.appendChild(remove);
  }
  box.append(form, actions);
  return box;
}

function pcalWeekItems(fromKey, untilKey) {
  const inWeek = (session) => {
    const key = session.scheduled_start.slice(0, 10);
    return key >= fromKey && key <= untilKey;
  };
  const items = plannerHistorySessions.filter(inWeek).map((session) => ({ kind: session.status, session }));
  if (plannerHasLivePlan()) {
    const review = pcalReviewActive() ? plannerAdaptReview.proposal : null;
    plannerSessions.filter(inWeek).forEach((session) => {
      const kind = session.status === "in_progress" ? "active" : plannerIsOverdue(session) ? "overdue" : "confirmed";
      // Under review, a session the proposal moves or drops stays where it is, marked.
      const moving = review?.moved.find((item) => item.session_id === session.session_id);
      const dropping = review?.cancelled.find((item) => item.session_id === session.session_id);
      const change = moving ? { type: "moving", item: moving } : dropping ? { type: "cancelled", item: dropping } : null;
      items.push({ kind, session, change });
    });
    if (review) {
      const ghost = (item, start, end, minutes) => ({ document_id: item.document_id, document_title: pcalDocumentTitle(item.document_id),
        activity_type: item.activity_type, scheduled_start: start, scheduled_end: end, duration_minutes: minutes, status: "proposed" });
      review.added.forEach((item) => {
        const session = ghost(item, item.scheduled_start, item.scheduled_end, item.duration_minutes);
        if (inWeek(session)) items.push({ kind: "proposed", session, change: { type: "added", item } });
      });
      review.moved.forEach((item) => {
        const minutes = plannerSessionsById.get(item.session_id)?.duration_minutes
          ?? plannerToMinutes(item.to_end.slice(11, 16)) - plannerToMinutes(item.to_start.slice(11, 16));
        const session = ghost(item, item.to_start, item.to_end, minutes);
        if (inWeek(session)) items.push({ kind: "proposed", session, change: { type: "moved", item } });
      });
    }
  } else if (plannerPreview) {
    plannerPreview.sessions.filter(inWeek).forEach((session) => items.push({ kind: "suggested", session }));
  }
  return items;
}

function pcalRenderToolbar() {
  if (!pcal.range) return;
  const dates = pcalWeekDates();
  const first = dates[0], last = dates[6];
  const sameMonth = first.getMonth() === last.getMonth();
  const startLabel = first.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const endLabel = last.toLocaleDateString(undefined, sameMonth ? { day: "numeric" } : { month: "short", day: "numeric" });
  pcal.range.textContent = `${startLabel} – ${endLabel}, ${last.getFullYear()}`;

  const live = plannerHasLivePlan();
  let status = "";
  let tone = "";
  if (live) {
    const week = pcalWeekItems(plannerDateKey(first), plannerDateKey(last))
      .filter((item) => ["confirmed", "active", "overdue"].includes(item.kind));
    const minutes = week.reduce((sum, item) => sum + item.session.duration_minutes, 0);
    status = week.length
      ? `${week.length} planned session${week.length === 1 ? "" : "s"} this week · ${plannerFormatDuration(minutes)}`
      : "No sessions this week";
  } else if (plannerPreviewing && plannerReadyToPreview()) {
    status = "Updating suggestions…";
    tone = "busy";
  } else if (plannerPreview) {
    const sessions = plannerPreview.sessions;
    const minutes = sessions.reduce((sum, session) => sum + session.duration_minutes, 0);
    status = `${sessions.length} suggested session${sessions.length === 1 ? "" : "s"} · ${plannerFormatDuration(minutes)}`;
    if (plannerPreview.capacity.status === "at_risk") {
      status += ` · ${plannerFormatDuration(plannerPreview.capacity.shortfall_minutes)} doesn’t fit`;
      tone = "warn";
    }
  }
  pcal.status.textContent = status;
  pcal.status.dataset.tone = tone;
  pcal.explain.hidden = !(live || plannerPreview?.sessions.length);
  pcal.autoPlan.hidden = live || !plannerMaterials.length;
  pcal.autoPlan.disabled = plannerBusy || !plannerReadyToPreview();
  pcal.accept.hidden = live || !plannerPreview?.sessions.length;
  pcal.accept.disabled = plannerBusy || plannerPreviewing;
  pcal.accept.textContent = plannerBusy ? "Saving…" : "Accept plan";
  pcalRenderReviewBar(plannerDateKey(first), plannerDateKey(last));
  pcal.status.parentElement.hidden = pcalReviewActive();   // the review bar takes the status line's place
  pcalRenderNotice();
}

function pcalRenderNotice() {
  // Progressive disclosure: only the next thing the learner needs, in one short line.
  const notice = pcal.notice;
  notice.innerHTML = "";
  notice.dataset.tone = "";
  let text = "";
  if (plannerLoadFailed) {
    text = "Could not load your study plan.";
    notice.dataset.tone = "warn";
    const action = pcalEl("button", "pcal-notice-action", "Try again");
    action.type = "button";
    action.id = "pcal-load-retry";
    action.addEventListener("click", () => { action.disabled = true; plannerOpen(); });
    notice.append(pcalEl("span", "", text), action);
  } else if (!plannerLoaded) {
    text = "Loading your study plan…";
    notice.dataset.tone = "busy";
    notice.appendChild(pcalEl("span", "", text));
  } else if (plannerNewMaterialFailure && plannerNewMaterialFailure.planId === plannerPlan?.plan_id) {
    text = "Could not suggest sessions for your new material. Your accepted plan is unchanged.";
    notice.dataset.tone = "warn";
    const action = pcalEl("button", "pcal-notice-action", "Try again");
    action.type = "button";
    action.id = "pcal-new-material-retry";
    action.addEventListener("click", () => { action.disabled = true; plannerProposeNewMaterials(plannerNewMaterialFailure.planId); });
    notice.append(pcalEl("span", "", text), action);
  } else if (plannerAdaptStale && plannerAdaptStale.planId === plannerPlan?.plan_id) {
    text = "Your plan changed. Review the latest schedule.";
    const action = pcalEl("button", "pcal-notice-action", "Check for new suggestions");
    action.type = "button";
    action.id = "pcal-review-recheck";
    action.addEventListener("click", () => plannerRecheckAfterStale(action));
    notice.append(pcalEl("span", "", text), action);
  } else if (plannerHasLivePlan()) text = "";
  else if (!plannerMaterials.length) {
    text = "Add the materials you want to study.";
    const action = pcalEl("button", "pcal-notice-action", "Add materials");
    action.type = "button";
    action.addEventListener("click", pcalOpenSheet);
    notice.append(pcalEl("span", "", text), action);
  } else if (!plannerAvailability.length) {
    text = "Drag on the calendar to mark when you could study.";
    notice.appendChild(pcalEl("span", "", text));
  } else if (plannerPreviewFailure && !plannerPreviewing) {
    const failure = plannerPreviewFailure;
    text = failure.message || "Could not suggest a plan";
    notice.dataset.tone = "warn";
    notice.appendChild(pcalEl("span", "", text));
    const documents = failure.detail?.code === "deadline_passed" ? failure.detail.documents || [] : [];
    documents.forEach((item) => notice.appendChild(pcalEl("span", "pcal-notice-chip", `${item.document_title || item.document_id} · ${item.deadline}`)));
    if (!documents.length) {
      const retry = pcalEl("button", "pcal-notice-action", "Try again");
      retry.type = "button";
      retry.id = "pcal-preview-retry";
      retry.addEventListener("click", () => plannerQueueAutoPreview(0));
      notice.appendChild(retry);
    }
  } else if (plannerPreview && !plannerPreviewing && !plannerPreview.sessions.length) {
    text = "Nothing fits in your available time yet. Drag to add more.";
    notice.dataset.tone = "warn";
    notice.appendChild(pcalEl("span", "", text));
  }
  notice.hidden = !text;
}

function pcalNowMinutes() {
  const now = plannerNow();
  return now.getHours() * 60 + now.getMinutes();
}

function pcalFirstOpenMinute(dateKey, step = PLANNER_CELL_MINUTES) {
  // Past days are read-only; today opens at the next grid step after "now"; later days are open.
  const todayKey = plannerDateKey(plannerNow());
  if (dateKey < todayKey) return Infinity;
  if (dateKey > todayKey) return 0;
  return Math.ceil(pcalNowMinutes() / step) * step;
}

function pcalSlotIsPast(slot, dateKey) {
  const todayKey = plannerDateKey(plannerNow());
  return dateKey < todayKey || (dateKey === todayKey && plannerToMinutes(slot.end_at) <= pcalNowMinutes());
}

function pcalAvailabilityFor(dateKey, weekday) {
  return plannerAvailability.filter((slot) => plannerAvailabilityCoversDate(slot, dateKey, weekday));
}

function pcalSubtractIntervals(start, end, blocked) {
  // [start, end) minus the blocked [from, to) ranges (any order, may overlap): the pieces left, in order.
  let free = [[start, end]];
  [...blocked].sort((left, right) => left[0] - right[0]).forEach(([from, to]) => {
    free = free.flatMap(([s, e]) => (to <= s || from >= e ? [[s, e]]
      : [...(from > s ? [[s, from]] : []), ...(to < e ? [[to, e]] : [])]));
  });
  return free;
}

function pcalEffectiveAvailabilityFor(dateKey, weekday) {
  // What the scheduler can actually use (study_time.free_minutes_by_date): each marked slot minus the
  // day's Google busy time. [{slot, start, end}] per remaining piece; with "Avoid conflicts" off (or
  // Google not connected) there is no busy time, so each slot is shown whole.
  const busy = pcalGoogleBusyFor(dateKey);
  return pcalAvailabilityFor(dateKey, weekday).flatMap((slot) =>
    pcalSubtractIntervals(plannerToMinutes(slot.start_at), plannerToMinutes(slot.end_at), busy)
      .map(([start, end]) => ({ slot, start, end })));
}

function pcalRenderGrid() {
  const dates = pcalWeekDates();
  const now = plannerNow();
  const todayKey = plannerDateKey(now);
  const fromKey = plannerDateKey(dates[0]);
  const untilKey = plannerDateKey(dates[6]);
  const items = pcalWeekItems(fromKey, untilKey);

  // Sticky header: weekday + date, then the all-day row that carries deadline markers.
  if (pcalFocusDay && (pcalFocusDay < fromKey || pcalFocusDay > untilKey)) pcalFocusDay = null;
  const columns = pcalGridColumns(dates);
  pcal.head.style.gridTemplateColumns = columns;
  pcal.body.style.gridTemplateColumns = columns;
  pcal.head.innerHTML = "";
  pcal.head.appendChild(pcalEl("div", "pcal-corner"));
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    // A day header expands its column (more room for that day's sessions); again shows all days equally.
    const head = pcalEl("button", "pcal-dayhead");
    head.type = "button";
    head.dataset.date = key;
    head.classList.toggle("is-today", key === todayKey);
    head.classList.toggle("is-past", key < todayKey);
    head.classList.toggle("is-focused", key === pcalFocusDay);
    head.setAttribute("aria-pressed", String(key === pcalFocusDay));
    head.title = key === pcalFocusDay ? "Show all days equally" : `Expand ${plannerDayLabel(key)}`;
    head.append(pcalEl("span", "pcal-dow", PLANNER_DAY_LABELS[(date.getDay() + 6) % 7]), pcalEl("span", "pcal-daynum", String(date.getDate())));
    head.addEventListener("click", () => pcalToggleFocusDay(key));
    pcal.head.appendChild(head);
  });
  pcal.head.appendChild(pcalEl("div", "pcal-allday-label"));
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const cell = pcalEl("div", "pcal-allday");
    cell.dataset.date = key;
    plannerMaterials.filter((material) => material.deadline === key).forEach((material) => {
      const marker = pcalEl("span", "pcal-deadline-marker", pcalMaterialTitle(material));
      marker.classList.toggle("is-past", key < todayKey);
      marker.title = `${pcalMaterialTitle(material)} due ${plannerDayLabel(key)}`;
      marker.setAttribute("aria-label", marker.title);
      cell.appendChild(marker);
    });
    pcal.head.appendChild(cell);
  });

  // Body: hour gutter + seven day columns; availability, sessions and "now" are positioned layers.
  pcal.body.innerHTML = "";
  const gutter = pcalEl("div", "pcal-gutter");
  for (let hour = 1; hour < 24; hour += 1) {
    const label = pcalEl("span", "pcal-hour", plannerMinutesToLabel(hour * 60));
    label.style.top = `${hour * PCAL_HOUR_PX}px`;
    gutter.appendChild(label);
  }
  pcal.body.appendChild(gutter);
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const weekday = (date.getDay() + 6) % 7;
    const column = pcalEl("div", "pcal-col");
    column.dataset.date = key;
    column.dataset.weekday = String(weekday);
    column.classList.toggle("is-today", key === todayKey);
    column.classList.toggle("is-past", key < todayKey);
    column.setAttribute("aria-label", plannerDayLabel(key));
    const dayItems = items.filter((item) => item.session.scheduled_start.slice(0, 10) === key);
    const busy = dayItems.map((item) => [plannerToMinutes(item.session.scheduled_start.slice(11, 16)),
      plannerEndMinutes(item.session)]);
    pcalEffectiveAvailabilityFor(key, weekday).forEach((piece) => column.appendChild(pcalAvailabilityBlock(piece, key, busy)));
    pcalGoogleBusyFor(key).forEach(([from, to]) => column.appendChild(pcalBusyBlock(from, to)));
    const lanes = pcalLayoutLanes(busy.map(([start, end]) => ({ start, end })));
    dayItems.forEach((item, index) => column.appendChild(pcalEventBlock(item, lanes[index])));
    if (key === todayKey) {
      const shade = pcalEl("div", "pcal-past-shade");   // the part of today that has passed
      shade.style.height = `${(now.getHours() * 60 + now.getMinutes()) * PCAL_MINUTE_PX}px`;
      shade.setAttribute("aria-hidden", "true");
      column.appendChild(shade);
      const line = pcalEl("div", "pcal-now");
      line.style.top = `${(now.getHours() * 60 + now.getMinutes()) * PCAL_MINUTE_PX}px`;
      line.setAttribute("aria-hidden", "true");
      column.appendChild(line);
    }
    column.addEventListener("pointerdown", (event) => pcalStartDrag(event, column, null));
    pcal.body.appendChild(column);
  });

  // First view of a week: scroll to the morning, or earlier if something starts earlier.
  if (pcalScrolledWeek !== fromKey) {
    pcalScrolledWeek = fromKey;
    const starts = [
      ...items.map((item) => plannerToMinutes(item.session.scheduled_start.slice(11, 16))),
      ...dates.flatMap((date) => pcalAvailabilityFor(plannerDateKey(date), (date.getDay() + 6) % 7).map((slot) => plannerToMinutes(slot.start_at))),
    ];
    const first = Math.min(8 * 60, ...starts);
    pcal.scroll.scrollTop = Math.max(0, (first - 60) * PCAL_MINUTE_PX);
  }
  pcalLoadGoogleBusy();   // no-op unless connected with "Avoid conflicts" and this week is not loaded yet
  pcalRenderAgenda();
}

function pcalPlace(element, startMinute, endMinute) {
  element.style.top = `${startMinute * PCAL_MINUTE_PX}px`;
  element.style.height = `${Math.max(12, (endMinute - startMinute) * PCAL_MINUTE_PX - 2)}px`;
}

function pcalAvailabilityBlock({ slot, start, end }, dateKey, busy = []) {
  // One effective piece of a marked slot (see pcalEffectiveAvailabilityFor); its popover edits the whole slot.
  const from = plannerMinutesToLabel(start), to = plannerMinutesToLabel(end);
  const block = pcalEl("div", "pcal-avail");
  block.dataset.start = from;
  block.dataset.end = to;
  block.dataset.recurring = String(Boolean(slot.is_recurring));
  block.tabIndex = 0;
  block.setAttribute("role", "button");
  block.setAttribute("aria-label", `Available ${from}–${to}${slot.is_recurring ? ", every week" : ""}`);
  pcalPlace(block, start, end);
  block.classList.toggle("is-short", end - start < 60);
  block.classList.toggle("is-past", pcalSlotIsPast({ end_at: to }, dateKey));
  // The label goes where no session covers it: the bottom of the window, else the top, else none.
  const free = (from, to) => !busy.some(([s, e]) => s < to && e > from);
  const room = 30;
  block.classList.add(free(end - room, end) ? "label-bottom" : free(start, start + room) ? "label-top" : "label-none");
  block.append(pcalEl("span", "pcal-avail-label", "Available"),
    pcalEl("span", "pcal-avail-time", `${from}–${to} · ${plannerFormatDuration(end - start)}`));
  block.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
    pcalStartDrag(event, block.parentElement, { slot, dateKey, block });
  });
  block.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pcalOpenAvailabilityPopover(slot, dateKey, block);
    }
  });
  return block;
}

function pcalEventBlock({ kind, session, change = null }, { lane = 0, lanes = 1 } = {}) {
  // A study session at its real start and end. The content adapts to the block's height (zoom x
  // duration): roomy = title, time · duration, activity + status; medium = title + time · duration;
  // compact = one row, title + start. The full details stay in the tooltip, aria-label and popover.
  const start = plannerToMinutes(session.scheduled_start.slice(11, 16));
  const end = plannerEndMinutes(session);
  const block = pcalEl("button", `pcal-event pcal-event--${kind} pcal-activity--${session.activity_type}${session.steps?.length ? " pcal-event--combined" : ""}`);
  block.type = "button";
  block.dataset.kind = kind;
  block.dataset.start = session.scheduled_start;
  if (session.session_id) block.dataset.sessionId = session.session_id;
  const fullTitle = plannerSessionTitle(session);
  const title = plannerDisplayTitle(fullTitle);
  const label = change ? PCAL_CHANGE_KIND_LABELS[change.type] : PCAL_KIND_LABELS[kind];
  const duration = plannerFormatDuration(session.duration_minutes || end - start);
  block.setAttribute("aria-label", `${label}: ${pcalActivity(session.activity_type, session.steps)} · ${fullTitle}, ${pcalShortDay(session.scheduled_start.slice(0, 10))} ${pcalSessionTimes(session)}`);
  pcalPlace(block, start, end);
  if (lanes > 1) {
    // Overlapping events share the column side by side; neither covers the other.
    block.classList.add("is-lane");
    block.style.left = `calc(${(lane * 100) / lanes}% + 3px)`;
    block.style.width = `calc(${100 / lanes}% - 4px)`;
    block.style.right = "auto";
  }
  // Room for text: the block's real height, less a change tag's own line on a proposed change.
  const height = (end - start) * PCAL_MINUTE_PX - 2 - (change ? 15 : 0);
  const size = height >= PCAL_EVENT_ROOMY_PX && lanes === 1 ? "roomy" : height >= PCAL_EVENT_TWO_LINE_PX ? "medium" : "compact";
  block.classList.add(`pcal-event--${size}`);
  block.classList.toggle("is-compact", size === "compact");
  // A long subject wraps onto a second (third) line only when the block has room for it.
  if (size === "roomy") block.style.setProperty("--title-lines", String(height >= 112 ? 3 : height >= 78 ? 2 : 1));
  // A side-by-side lane is narrow: its subject may wrap instead of collapsing to an ellipsis.
  else if (lanes > 1 && size === "medium") block.style.setProperty("--title-lines", String(height >= 90 ? 3 : height >= 62 ? 2 : 1));
  if (change) {
    // A proposed change: tagged, reviewable, not draggable until the learner decides.
    block.classList.add("pcal-change", `pcal-change--${change.type}`);
    block.dataset.change = change.type;
    block.title = PCAL_CHANGE_LABELS[change.type];
    // Short blocks keep the title readable: a glyph for ghosts; muted / struck styling says the rest.
    const tag = end - start < 40 ? { added: "+", moved: "→" }[change.type]
      : change.type === "added" ? `+ ${PCAL_CHANGE_LABELS.added}` : PCAL_CHANGE_LABELS[change.type];
    if (tag) block.appendChild(pcalEl("span", "pcal-event-tag", tag));
  } else {
    block.title = `${fullTitle}\n${pcalActivity(session.activity_type, session.steps)} · ${pcalSessionTimes(session)} (${duration})\n${label}`;
  }
  const meta = pcalEl("span", "pcal-event-meta");   // its text stays "Activity · 15:30–16:00"
  // The end time is its own span so a one-row or narrow block can show only the start.
  const time = pcalEl("span", "pcal-event-time", session.scheduled_start.slice(11, 16));
  time.appendChild(pcalEl("span", "pcal-event-end", `–${session.scheduled_end.slice(11, 16)}`));
  meta.append(pcalEl("span", "pcal-event-activity", pcalActivity(session.activity_type, session.steps)),
    pcalEl("span", "pcal-event-sep", " · "), time);
  const extra = pcalEl("span", "pcal-event-extra");
  extra.append(pcalEl("span", "pcal-event-duration", duration), pcalEl("span", "pcal-event-break"),
    pcalEl("span", `pcal-event-status pcal-event-status--${kind}`, label));
  block.append(pcalEl("strong", "pcal-event-title", title), meta, extra);
  const source = change ? null : pcalMoveSourceFor(kind, session, block);
  block.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
    if (source) pcalBeginMove(event, source);
  });
  block.addEventListener("click", () => {
    if (pcalSuppressClick) {
      pcalSuppressClick = false;
      return;
    }
    if (change) pcalOpenChangePopover(change, session, block);
    else pcalOpenSessionPopover(kind, session, block);
  });
  if (source) block.classList.add("is-draggable");
  return block;
}

// -- drag to mark availability --------------------------------------------------

function pcalMinuteAt(column, clientY) {
  const box = column.getBoundingClientRect();
  const minute = Math.floor((clientY - box.top) / PCAL_MINUTE_PX / PLANNER_CELL_MINUTES) * PLANNER_CELL_MINUTES;
  return Math.min(PCAL_DAY_MINUTES - PLANNER_CELL_MINUTES, Math.max(0, minute));
}

function pcalStartDrag(event, column, from) {
  if (event.button !== undefined && event.button > 0) return;
  event.preventDefault();
  pcalClosePopover();
  const anchor = pcalMinuteAt(column, event.clientY);
  const firstOpen = pcalFirstOpenMinute(column.dataset.date);
  if (anchor < firstOpen) {
    // Past time is read-only: a click on past availability still shows its details.
    if (from) pcalOpenAvailabilityPopover(from.slot, from.dateKey, from.block);
    return;
  }
  const ghost = pcalEl("div", "pcal-drag");
  column.appendChild(ghost);
  pcalDrag = { column, anchor, start: anchor, end: anchor + PLANNER_CELL_MINUTES, moved: false, from, ghost, firstOpen };
  pcalUpdateDragGhost();
}

function pcalUpdateDragGhost() {
  const drag = pcalDrag;
  pcalPlace(drag.ghost, drag.start, drag.end);
  drag.ghost.textContent = `${plannerMinutesToLabel(drag.start)}–${plannerMinutesToLabel(drag.end)}`;
  drag.ghost.hidden = Boolean(drag.from) && !drag.moved;
}

function pcalMoveDrag(event) {
  if (!pcalDrag) return;
  const minute = Math.max(pcalDrag.firstOpen, pcalMinuteAt(pcalDrag.column, event.clientY));
  if (minute !== pcalDrag.anchor) pcalDrag.moved = true;
  pcalDrag.start = Math.min(pcalDrag.anchor, minute);
  pcalDrag.end = Math.max(pcalDrag.anchor, minute) + PLANNER_CELL_MINUTES;
  pcalUpdateDragGhost();
}

async function pcalFinishDrag() {
  const drag = pcalDrag;
  pcalDrag = null;
  if (!drag) return;
  if (drag.from && !drag.moved) {   // a click on an availability block: its details
    drag.ghost.remove();
    pcalOpenAvailabilityPopover(drag.from.slot, drag.from.dateKey, drag.from.block);
    return;
  }
  const weekday = Number(drag.column.dataset.weekday);
  // New availability repeats weekly by default; the block's popover can limit it to this date.
  const payload = { start_at: plannerMinutesToLabel(drag.start), end_at: plannerMinutesToLabel(drag.end), is_recurring: true, day_of_week: weekday };
  try {
    await pcalChangeAvailability("add", payload);
  } catch (error) {
    showToast(error.message || "Could not save availability");
  }
  drag.ghost.remove();
  plannerAfterAvailabilityChange();
}

// -- popovers ---------------------------------------------------------------------

function pcalClosePopover() {
  if (!pcal.popover || pcal.popover.hidden) return;
  pcal.popover.hidden = true;
  pcal.popover.innerHTML = "";
  plannerWorkspace.querySelectorAll(".is-selected").forEach((item) => item.classList.remove("is-selected"));
}

function pcalShowPopover(anchor, label, build) {
  pcalClosePopover();
  const popover = pcal.popover;
  popover.setAttribute("aria-label", label);
  const close = pcalEl("button", "pcal-popover-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", pcalClosePopover);
  popover.appendChild(close);
  build(popover);
  popover.hidden = false;
  anchor.classList.add("is-selected");
  // Beside the block, inside the workspace: to the right when there is room, else to the left.
  const frame = plannerWorkspace.getBoundingClientRect();
  const box = anchor.getBoundingClientRect();
  const width = popover.offsetWidth, height = popover.offsetHeight;
  let left = box.right + 8 - frame.left;
  if (left + width > frame.width - 8) left = box.left - width - 8 - frame.left;
  const top = Math.min(Math.max(8, box.top - frame.top), frame.height - height - 8);
  popover.style.left = `${Math.max(8, left)}px`;
  popover.style.top = `${Math.max(8, top)}px`;
}

function pcalOpenAvailabilityPopover(slot, dateKey, block) {
  pcalShowPopover(block, "Availability", (popover) => {
    const start = plannerToMinutes(slot.start_at), end = plannerToMinutes(slot.end_at);
    popover.append(pcalEl("h4", "pcal-popover-title", "Available"),
      pcalEl("p", "pcal-popover-when", `${plannerDayLabel(dateKey)} · ${block.dataset.start}–${block.dataset.end} (${plannerFormatDuration(plannerToMinutes(block.dataset.end) - plannerToMinutes(block.dataset.start))})`));
    if (block.dataset.start !== slot.start_at || block.dataset.end !== slot.end_at) {
      // A piece of a slot that Google busy time splits: the actions below apply to the whole slot.
      popover.appendChild(pcalEl("p", "pcal-popover-note", `Part of ${slot.start_at}–${slot.end_at} (${plannerFormatDuration(end - start)}); busy time in Google Calendar is skipped.`));
    }
    if (pcalSlotIsPast(slot, dateKey)) return;   // history: shown, not editable
    const repeat = pcalEl("label", "pcal-popover-toggle");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = "pcal-repeat";
    checkbox.checked = Boolean(slot.is_recurring);
    repeat.append(checkbox, pcalEl("span", "", "Repeat weekly"));
    const remove = pcalEl("button", "pcal-button pcal-danger", "Delete");
    remove.type = "button";
    remove.id = "pcal-delete-availability";
    const actions = pcalEl("div", "pcal-popover-actions");
    actions.appendChild(remove);
    popover.append(repeat, actions);
    const run = async (steps) => {
      checkbox.disabled = remove.disabled = true;
      try {
        for (const [action, payload] of steps) await pcalChangeAvailability(action, payload);
      } catch (error) {
        showToast(error.message || "Could not update availability");
      }
      pcalClosePopover();
      plannerAfterAvailabilityChange();
    };
    const current = pcalSlotPayload(slot, dateKey);
    remove.addEventListener("click", () => run([["remove", current]]));
    checkbox.addEventListener("change", () => {
      // Weekly <-> this date only: the same time range moves between the two kinds of availability.
      const other = checkbox.checked
        ? { start_at: slot.start_at, end_at: slot.end_at, is_recurring: true, day_of_week: slot.day_of_week ?? Number(block.parentElement.dataset.weekday) }
        : { start_at: slot.start_at, end_at: slot.end_at, is_recurring: false, date: dateKey };
      run([["remove", current], ["add", other]]);
    });
  });
}

function pcalReasonText(reason) {
  if (!reason) return "";
  return PCAL_REASON_TEXT[reason.code] || reason.message || "";
}

function pcalPlanFactors() {
  const factors = [];
  const materials = plannerMaterials.map((material) => ({ material, state: plannerDocStates.get(material.document_id) }));
  const known = materials.filter((item) => item.state);
  if (known.length) {
    const counts = {};
    known.forEach(({ state }) => {
      const label = (PLANNER_LEARNING_STATE_LABELS[state.learning.state] || state.learning.state).toLowerCase();
      counts[label] = (counts[label] || 0) + 1;
    });
    factors.push(["Learning state", Object.entries(counts).map(([label, count]) => `${count} ${label}`).join(", ")]);
    const quizzed = known.filter(({ state }) => state.quiz.latest);
    factors.push(["Quiz performance", quizzed.length
      ? quizzed.map(({ material, state }) => `${pcalMaterialTitle(material)} ${Math.round(state.quiz.latest.percentage)}%`).join(", ")
      : "No quiz results yet — planning starts from new material."]);
  }
  const due = plannerMaterials.filter((material) => material.deadline);
  factors.push(["Deadlines", due.length
    ? due.map((material) => `${pcalMaterialTitle(material)} ${new Date(`${material.deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`).join(", ")
    : "None set — sessions spread over the next two weeks."]);
  const weekMinutes = pcalWeekDates().reduce((sum, date) => sum + pcalAvailabilityFor(plannerDateKey(date), (date.getDay() + 6) % 7)
    .reduce((total, slot) => total + plannerToMinutes(slot.end_at) - plannerToMinutes(slot.start_at), 0), 0);
  factors.push(["Available time", `Only the time you marked — ${plannerFormatDuration(weekMinutes)} this week.`]);
  const sessions = plannerHasLivePlan() ? plannerSessions : plannerPreview?.sessions || [];
  const reasons = {};
  sessions.forEach((session) => {
    const code = session.reason?.code;
    if (PCAL_REASON_PHRASES[code]) reasons[code] = (reasons[code] || 0) + 1;
  });
  if (Object.keys(reasons).length) {
    factors.push(["In this plan", Object.entries(reasons).map(([code, count]) => `${count} × ${PCAL_REASON_PHRASES[code]}`).join(", ")]);
  }
  return factors;
}

function pcalOpenPlanExplanation() {
  pcalShowPopover(pcal.explain, "How this plan was built", (popover) => {
    popover.appendChild(pcalEl("h4", "pcal-popover-title", "How this plan was built"));
    popover.appendChild(pcalEl("p", "pcal-popover-when", "Tutor picks each session from what your materials need, then fits it into your free time."));
    const list = pcalEl("dl", "pcal-factors");
    pcalPlanFactors().forEach(([term, detail]) => {
      const row = pcalEl("div", "pcal-factor");
      row.append(pcalEl("dt", "", term), pcalEl("dd", "", detail));
      list.appendChild(row);
    });
    popover.appendChild(list);
  });
}

function pcalWhen(iso) {
  return `${plannerDayLabel(iso.slice(0, 10))} · ${iso.slice(11, 16)}`;
}

function pcalOpenSessionPopover(kind, session, block) {
  const title = session.document_title || plannerDocumentTitle(session.document_id);
  const name = `${title} (${pcalActivity(session.activity_type, session.steps)})`;
  pcalShowPopover(block, `${pcalActivity(session.activity_type, session.steps)} · ${title}`, (popover) => {
    const holder = pcalEl("div", "pcal-popover-session");
    const content = pcalEl("div", "pcal-popover-body");
    content.append(pcalEl("span", `pcal-kind pcal-kind--${kind}`, PCAL_KIND_LABELS[kind]),
      pcalEl("h4", "pcal-popover-title", title),
      pcalEl("p", "pcal-popover-activity", pcalActivity(session.activity_type, session.steps)),
      pcalEl("p", "pcal-popover-when", `${plannerDayLabel(session.scheduled_start.slice(0, 10))} · ${pcalSessionTimes(session)} · ${plannerFormatDuration(session.duration_minutes)}`));
    const reason = pcalReasonText(session.reason);
    if (reason) {
      const why = pcalEl("p", "pcal-popover-reason");
      why.append(pcalEl("span", "pcal-popover-caption", "Why this session?"), document.createTextNode(` ${reason}`));
      content.appendChild(why);
    }
    const deadline = pcalMaterialDeadline(session.document_id);
    if (deadline && !PLANNER_HISTORY_SESSION_STATUSES.includes(session.status)) {
      content.appendChild(pcalEl("p", "pcal-popover-note", `Due ${new Date(`${deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`));
    }
    const original = session.rescheduled_from && plannerSessionsById.get(session.rescheduled_from);
    if (original) content.appendChild(pcalEl("p", "pcal-popover-note", `Moved from ${pcalWhen(original.scheduled_start)}`));
    if (session.manual_edit && !PLANNER_HISTORY_SESSION_STATUSES.includes(session.status)) {
      content.appendChild(pcalEl("p", "pcal-popover-note pcal-popover-manual", "Set by you. Auto Plan and suggestions keep this time and length."));
    }
    if (session.steps?.length) content.appendChild(pcalStepList(session));
    if (kind === "completed" && session.completed_at) {
      content.appendChild(pcalEl("p", "pcal-popover-note", `Completed ${new Date(session.completed_at).toLocaleString(undefined,
        { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`));
    }
    holder.appendChild(content);
    if (kind === "suggested") content.appendChild(pcalEl("p", "pcal-popover-hint", "Suggested. Accept plan to save it."));
    const actions = session.session_id ? plannerSessionActions(kind, session) : [];
    if (actions.length) {
      const group = pcalEl("div", "pcal-popover-actions pcal-session-actions");
      actions.forEach(([action, label, primary]) => {
        const button = pcalEl("button", `${primary ? "primary-button" : "pcal-text-action"} planner-session-action planner-session-${action}`, label);
        button.type = "button";
        button.dataset.sessionAction = action;
        button.dataset.sessionId = session.session_id;
        button.setAttribute("aria-label", `${label} ${name}`);
        // The same lifecycle call as everywhere else: one request per session at a time.
        button.addEventListener("click", () => plannerSessionAction(session, action, button, group));
        group.appendChild(button);
      });
      holder.appendChild(group);
    }
    popover.appendChild(holder);
  });
  pcal.popover.querySelector(".pcal-session-actions .primary-button")?.focus();
}

// -- Study preferences panel ----------------------------------------------------------
// The learner's own scheduling choices, saved per user (server: study_preferences). Auto Plan and
// new suggestions use them; accepted and hand-edited sessions never change because of them.

const PLANNER_STRATEGY_LABELS = {
  balanced: ["Balanced", "Spread sessions evenly over your available days."],
  finish_early: ["Finish Early", "Use the earliest free time first."],
  deadline_priority: ["Deadline Priority", "Materials with a deadline go first; the others stay balanced."],
};
const PLANNER_DURATION_LABELS = {
  summary: "Summary (new material)", flashcards: "Flashcards", quiz: "Quiz", quiz_retry: "Quiz retry",
  review: "Weak-topic review", written_quiz: "Written quiz", final_review: "Final review", light_review: "Light spaced review",
};
let plannerPreferences = null;

async function plannerLoadPreferences() {
  try {
    plannerPreferences = await plannerRequest(PLANNER_PREFERENCES_API_URL);
  } catch (error) {
    plannerPreferences = null;   // the panel says it could not load; planning keeps the server's defaults
  }
  if (pcalOpenPanel === "prefs") pcalRenderPreferences();
}

function pcalPrefsSelect(id, label, values, current, format) {
  const field = pcalEl("label", "pcal-prefs-field");
  field.htmlFor = id;
  const select = document.createElement("select");
  select.id = id;
  values.forEach((value) => select.add(new Option(format(value), String(value), false, value === current)));
  field.append(pcalEl("span", "pcal-prefs-label", label), select);
  return { field, select };
}

function pcalRenderPreferences() {
  const form = pcal.prefsForm;
  if (!form) return;
  form.innerHTML = "";
  const prefs = plannerPreferences;
  if (!prefs) {
    form.appendChild(pcalEl("p", "pcal-prefs-note", "Could not load your preferences. Try again in a moment."));
    return;
  }
  const options = prefs.options;
  const daily = pcalPrefsSelect("pcal-pref-daily", "Daily study target", options.daily_target_minutes,
    prefs.daily_target_minutes, (value) => plannerFormatDuration(value));
  const pause = pcalPrefsSelect("pcal-pref-break", "Break between sessions", options.break_minutes,
    prefs.break_minutes, (value) => `${value} min`);
  const strategy = pcalEl("fieldset", "pcal-prefs-group");
  strategy.appendChild(pcalEl("legend", "pcal-prefs-label", "Scheduling strategy"));
  options.strategy.forEach((value) => {
    const [label, hint] = PLANNER_STRATEGY_LABELS[value] || [value, ""];
    const option = pcalEl("label", "pcal-prefs-choice");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "pcal-pref-strategy";
    radio.value = value;
    radio.checked = prefs.strategy === value;
    const text = pcalEl("span", "pcal-prefs-choice-text");
    text.append(pcalEl("strong", "", label), pcalEl("small", "", hint));
    option.append(radio, text);
    strategy.appendChild(option);
  });
  const lengths = pcalEl("fieldset", "pcal-prefs-group pcal-prefs-lengths");
  lengths.appendChild(pcalEl("legend", "pcal-prefs-label", "Session length for new sessions"));
  options.durations.forEach((activity) => {
    const field = pcalEl("label", "pcal-prefs-length");
    const input = document.createElement("input");
    input.type = "number";
    input.min = String(options.min_session_minutes);
    input.max = String(options.max_session_minutes);
    input.step = String(options.duration_step_minutes);
    input.value = String(prefs.durations[activity]);
    input.dataset.activity = activity;
    input.setAttribute("aria-label", `${PLANNER_DURATION_LABELS[activity] || activity} length in minutes`);
    field.append(pcalEl("span", "", PLANNER_DURATION_LABELS[activity] || activity), input, pcalEl("span", "pcal-prefs-unit", "min"));
    lengths.appendChild(field);
  });
  const note = pcalEl("p", "pcal-prefs-note",
    "Used by Auto Plan and new suggestions. Accepted sessions and the ones you edited stay exactly as they are.");
  const error = pcalEl("p", "pcal-prefs-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  const status = pcalEl("p", "pcal-prefs-status");
  status.setAttribute("aria-live", "polite");
  status.textContent = prefs.saved ? "" : "Using the defaults.";
  const actions = pcalEl("div", "pcal-prefs-actions");
  const save = pcalEl("button", "primary-button pcal-prefs-save", "Save");
  save.type = "submit";
  const reset = pcalEl("button", "pcal-text-action pcal-prefs-reset", "Reset to defaults");
  reset.type = "button";
  reset.disabled = !prefs.saved;
  reset.addEventListener("click", () => pcalSavePreferences(null, reset, error, status));
  actions.append(save, reset);
  form.append(daily.field, pause.field, strategy, lengths, note, error, status, actions);
  form.onsubmit = (event) => {
    event.preventDefault();
    pcalSavePreferences(form, save, error, status);
  };
}

function pcalReadPreferences(form, error) {
  // Client-side checks mirror the server's; the server stays the authority.
  const options = plannerPreferences.options;
  const body = {
    daily_target_minutes: Number(form.querySelector("#pcal-pref-daily").value),
    break_minutes: Number(form.querySelector("#pcal-pref-break").value),
    strategy: form.querySelector('input[name="pcal-pref-strategy"]:checked')?.value || "balanced",
    durations: {},
  };
  for (const input of form.querySelectorAll(".pcal-prefs-length input")) {
    input.removeAttribute("aria-invalid");
    const minutes = Number(input.value);
    const label = PLANNER_DURATION_LABELS[input.dataset.activity] || input.dataset.activity;
    let problem = "";
    if (!Number.isInteger(minutes)) problem = `${label}: enter a whole number of minutes.`;
    else if (minutes < options.min_session_minutes) problem = `${label}: a study session must be at least ${options.min_session_minutes} minutes long.`;
    else if (minutes > options.max_session_minutes || minutes % options.duration_step_minutes) {
      problem = `${label}: choose up to ${options.max_session_minutes} minutes, in steps of ${options.duration_step_minutes}.`;
    } else if (minutes > body.daily_target_minutes) problem = `${label}: longer than your daily study target, so it could never be scheduled.`;
    if (problem) {
      input.setAttribute("aria-invalid", "true");
      error.textContent = problem;
      error.hidden = false;
      input.focus();
      return null;
    }
    body.durations[input.dataset.activity] = minutes;
  }
  return body;
}

async function pcalSavePreferences(form, button, error, status) {
  error.hidden = true;
  const body = form ? pcalReadPreferences(form, error) : null;
  if (form && !body) return;
  button.disabled = true;
  try {
    plannerPreferences = await plannerRequest(PLANNER_PREFERENCES_API_URL, form ? { method: "PUT", body } : { method: "DELETE" });
  } catch (failure) {
    button.disabled = false;
    error.textContent = failure.message || "Could not save your preferences.";
    error.hidden = false;
    const field = failure.detail?.field?.startsWith("durations.") ? failure.detail.field.slice(10) : null;
    if (field) pcal.prefsForm.querySelector(`input[data-activity="${field}"]`)?.setAttribute("aria-invalid", "true");
    return;
  }
  pcalRenderPreferences();
  const live = plannerHasLivePlan();
  pcal.prefsForm.querySelector(".pcal-prefs-status").textContent = form
    ? (live ? "Saved. Your accepted sessions stay as they are; new suggestions use these settings." : "Saved. The suggested plan is updated.")
    : "Back to the defaults.";
  // Not accepted yet: the suggestions are recomputed with the new settings. An accepted plan is never touched.
  if (!live) plannerQueueAutoPreview(0);
}

// -- Editing one session by hand ------------------------------------------------------
// Date, start time and length of an accepted session. The server validates the new window
// (availability, Google Calendar busy time, other sessions, the deadline, 20-minute minimum) and
// refuses with a reason -- shown here -- instead of saving a conflict. A saved edit is marked as the
// learner's own (manual_edit): Auto Plan and adaptation keep it as it is.

function pcalOpenSessionEditor(session, anchor) {
  const title = session.document_title || plannerDocumentTitle(session.document_id);
  const options = plannerPreferences?.options;
  // Opened from the session's own popover (which is replaced): anchor to its calendar block.
  const block = plannerWorkspace?.querySelector(`.pcal-event[data-session-id="${CSS.escape(session.session_id)}"]`);
  pcalShowPopover(block || anchor, `Edit ${pcalActivity(session.activity_type, session.steps)} · ${title}`, (popover) => {
    const form = pcalEl("form", "pcal-edit-form");
    form.noValidate = true;
    const field = (label, input) => {
      const wrap = pcalEl("label", "pcal-edit-field");
      wrap.append(pcalEl("span", "pcal-prefs-label", label), input);
      return wrap;
    };
    const date = document.createElement("input");
    date.type = "date";
    date.id = "pcal-edit-date";
    date.value = session.scheduled_start.slice(0, 10);
    date.min = plannerDateKey(plannerNow());
    const time = document.createElement("input");
    time.type = "time";
    time.id = "pcal-edit-time";
    time.step = "900";
    time.value = session.scheduled_start.slice(11, 16);
    const length = document.createElement("input");
    length.type = "number";
    length.id = "pcal-edit-duration";
    length.min = String(options?.min_session_minutes || 20);
    length.max = String(options?.max_session_minutes || 180);
    length.step = "5";
    length.value = String(session.duration_minutes);
    const error = pcalEl("p", "pcal-edit-error");
    error.setAttribute("role", "alert");
    error.hidden = true;
    const save = pcalEl("button", "primary-button pcal-edit-save", "Save");
    save.type = "submit";
    const cancel = pcalEl("button", "pcal-text-action pcal-edit-cancel", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", pcalClosePopover);
    const actions = pcalEl("div", "pcal-popover-actions");
    actions.append(save, cancel);
    form.append(pcalEl("h4", "pcal-popover-title", "Edit session"),
      pcalEl("p", "pcal-popover-activity", `${pcalActivity(session.activity_type, session.steps)} · ${title}`),
      field("Date", date), field("Start time", time), field("Length (minutes)", length),
      pcalEl("p", "pcal-popover-note", "Your change is kept: Auto Plan and suggestions won't move or resize this session."),
      error, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      pcalSaveSessionEdit(session, { date: date.value, time: time.value, minutes: Number(length.value) }, error, save);
    });
    popover.appendChild(form);
  });
  pcal.popover.querySelector("#pcal-edit-time")?.focus();
}

async function pcalSaveSessionEdit(session, values, error, button) {
  const show = (message) => {
    error.textContent = message;
    error.hidden = false;
  };
  error.hidden = true;
  const minimum = plannerPreferences?.options?.min_session_minutes || 20;
  if (!values.date || !values.time) return show("Choose a date and a start time.");
  if (!Number.isInteger(values.minutes) || values.minutes < minimum) return show(`A study session must be at least ${minimum} minutes long.`);
  const target = `${values.date}T${values.time}:00`;
  const body = { utc_offset_minutes: plannerUtcOffsetMinutes(), target_start: target };
  if (values.minutes !== session.duration_minutes) body.duration_minutes = values.minutes;
  if (target === session.scheduled_start && body.duration_minutes === undefined) {
    pcalClosePopover();
    return null;
  }
  if (plannerBusySessions.has(session.session_id)) return null;
  plannerBusySessions.add(session.session_id);
  button.disabled = true;
  try {
    const result = await plannerRequest(`${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/reschedule`,
      { method: "POST", body });
    const moved = { ...result.session, plan_id: session.plan_id };
    plannerSessionsById.set(session.session_id, { ...session, ...(result.previous || {}) });
    plannerSessionsById.set(moved.session_id, moved);
    plannerSessions = [...plannerSessions.filter((item) => item.session_id !== session.session_id), moved];
    pcalClosePopover();
    renderPlannerWorkspace();
    loadTodayPlan();
    showToast("Session updated");
  } catch (failure) {
    // Nothing was saved: say why, keep the form so the learner can adjust it.
    button.disabled = false;
    show(failure.message || "Could not save this change.");
    if (failure.status === 404 || failure.detail?.code === "session_not_reschedulable") await loadPlannerData({ keepWeek: true });
  } finally {
    plannerBusySessions.delete(session.session_id);
  }
  return null;
}

// -- add materials sheet ------------------------------------------------------------

function pcalOpenSheet() {
  pcalClosePopover();
  pcal.sheet.hidden = false;
  pcal.addMaterials.setAttribute("aria-expanded", "true");
  pcalRenderSheet();
  pcal.sheet.querySelector(".pcal-sheet-add, #pcal-sheet-close")?.focus();
}

// Goal drafts in the Add materials sheet, per document, kept while the sheet stays open.
const pcalSheetGoals = new Map();

function pcalCloseSheet() {
  if (!pcal.sheet || pcal.sheet.hidden) return;
  pcal.sheet.hidden = true;
  pcalSheetGoals.clear();
  pcal.addMaterials.setAttribute("aria-expanded", "false");
}

function pcalRenderSheet() {
  if (!pcal.sheet || pcal.sheet.hidden) return;
  pcal.sheetList.innerHTML = "";
  const chosen = new Set(plannerMaterials.map((material) => material.document_id));
  const available = indexedDocuments.filter((doc) => !chosen.has(doc.id));
  if (!indexedDocuments.length) {
    pcal.sheetList.appendChild(pcalEl("li", "pcal-sheet-empty", "Upload a document first, then add it here."));
    return;
  }
  if (!available.length) {
    pcal.sheetList.appendChild(pcalEl("li", "pcal-sheet-empty", "All your documents are in this plan."));
    return;
  }
  const today = plannerDateKey(plannerNow());
  available.forEach((doc) => {
    const row = pcalEl("li", "pcal-sheet-row");
    row.dataset.documentId = doc.id;
    const name = pcalEl("strong", "pcal-sheet-title", doc.title);
    name.title = doc.title;
    const facts = pcalEl("div", "pcal-sheet-facts");
    if (plannerDocStates.has(doc.id)) {
      const stateKey = pcalLearningKey(doc.id);
      const line = pcalEl("span", "pcal-sheet-state");
      line.append(pcalEl("span", `pcal-pill pcal-pill--${stateKey}`, PLANNER_LEARNING_STATE_LABELS[stateKey] || stateKey),
        pcalEl("span", "pcal-sheet-quiz", pcalQuizText(doc.id)));
      facts.append(line, pcalEl("span", "pcal-sheet-pack", pcalStudyPackText(doc.id)));
    }
    // Every new material starts with a goal choice; "Flexible learning" is preselected because it is
    // what a material without a deadline already means.
    const draft = pcalSheetGoals.get(doc.id) || { goal_type: "flexible", deadline: null, target_date: null };
    const { form, read } = plannerGoalForm(draft, { today, title: doc.title, onChange: (values) => pcalSheetGoals.set(doc.id, values) });
    const add = pcalEl("button", "pcal-button pcal-sheet-add", "Add");
    add.type = "button";
    add.setAttribute("aria-label", `Add ${doc.title}`);
    add.addEventListener("click", () => {
      const goal = read();
      if (goal) pcalAddMaterial(doc.id, goal, add);
    });
    row.append(name, facts, form, add);
    pcal.sheetList.appendChild(row);
  });
}

// -- study queue ----------------------------------------------------------------------

function pcalQueueItem(entry, kind) {
  // One activity the scheduler still wants that has no calendar slot. Dragging it onto the
  // calendar places it (a server-validated placement); it has no lifecycle actions of its own.
  const title = entry.document_title || plannerDocumentTitle(entry.document_id);
  const item = pcalEl("li", "pcal-queue-item pcal-queue-item--unscheduled");
  item.append(pcalEl("strong", "pcal-queue-title", title),
    pcalEl("span", "pcal-queue-meta", `${pcalActivity(entry.activity_type, entry.steps)} · ${plannerFormatDuration(entry.estimated_minutes || 0)}`));
  if (entry.reason?.message) item.appendChild(pcalEl("small", "pcal-queue-reason", entry.reason.message));
  const due = entry.deadline
    ? `Due ${new Date(`${entry.deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · ` : "";
  item.appendChild(pcalEl("span", "pcal-queue-need", `${due}Needs a time slot`));
  if (entry.candidate_key && entry.estimated_minutes) {
    item.classList.add("is-draggable");
    item.dataset.candidateKey = entry.candidate_key;
    item.setAttribute("aria-label", `${pcalActivity(entry.activity_type, entry.steps)} · ${title}, needs a time slot`);
    item.addEventListener("pointerdown", (event) => pcalBeginMove(event, {
      kind: kind === "live" ? "candidate" : "unscheduled", key: entry.candidate_key, element: item,
      documentId: entry.document_id, duration: entry.estimated_minutes, deadline: entry.deadline,
      title, activity: entry.activity_type, grabMinutes: 0,
    }));
  }
  return item;
}

function pcalRenderQueue() {
  // The queue holds only work without a calendar slot: scheduled, suggested and past sessions are
  // on the calendar, and their actions live in its popovers (and on Home).
  pcal.queue.innerHTML = "";
  const waiting = plannerHasLivePlan()
    ? plannerLiveCandidates.map((candidate) => pcalQueueItem(candidate, "live"))
    : (plannerPreview?.capacity.unscheduled || []).map((entry) => pcalQueueItem(entry, "suggested"));
  waiting.forEach((item) => pcal.queue.appendChild(item));
  pcal.queueCount.textContent = waiting.length ? String(waiting.length) : "";
  if (waiting.length) return;
  const empty = pcalEl("li", "pcal-queue-empty");
  const live = plannerHasLivePlan();
  if (!live && !plannerMaterials.length) empty.appendChild(pcalEl("span", "", "Add materials to get started."));
  else if (!live && !plannerAvailability.length) empty.appendChild(pcalEl("span", "", "Mark some free time to see what fits."));
  else if (!live && plannerPreviewing) empty.appendChild(pcalEl("span", "", "Finding the best times…"));
  else empty.append(pcalEl("strong", "", "All caught up"),
    pcalEl("span", "", "Everything that needs attention is already on your calendar."));
  pcal.queue.appendChild(empty);
}

function pcalShiftWeek(days) {
  const next = new Date(pcalWeekDates()[0]);
  next.setDate(next.getDate() + days);
  plannerCalWeekStart = next;
  renderPlannerWorkspace();
}

// -- direct manipulation: drag a session / queue item to a time (Phase 7B) ------------------
// The page only proposes a start; the server validates every drop (availability, overlap,
// deadline, status) and stays authoritative. An obviously invalid drop never sends a request.

function pcalMoveSourceFor(kind, session, element) {
  const grab = { element, documentId: session.document_id, duration: session.duration_minutes,
    title: session.document_title || plannerDocumentTitle(session.document_id), activity: session.activity_type };
  if (kind === "suggested" && session.candidate_key) return { ...grab, kind: "suggested", key: session.candidate_key, session };
  if ((kind === "confirmed" || kind === "overdue") && session.session_id && session.status === "scheduled") {
    return { ...grab, kind: "session", session };
  }
  return null;
}

function pcalBeginMove(event, source) {
  if ((event.button !== undefined && event.button > 0) || pcalMoveBusy) return;
  event.preventDefault();
  let grabMinutes = source.grabMinutes;
  if (grabMinutes === undefined) {
    const box = source.element.getBoundingClientRect();
    grabMinutes = Math.max(0, Math.round((event.clientY - box.top) / PCAL_MINUTE_PX));
  }
  pcalMove = { source, grabMinutes, x: event.clientX, y: event.clientY, active: false, target: null };
}

function pcalMaterialDeadline(documentId) {
  return plannerMaterials.find((material) => material.document_id === documentId)?.deadline || null;
}

function pcalOccupied(dateKey, source) {
  // Everything already on that day's calendar, except the item being moved.
  const same = (session) => session.scheduled_start.slice(0, 10) === dateKey;
  if (plannerHasLivePlan()) {
    return [...plannerSessions, ...plannerHistorySessions.filter((session) => session.status === "completed")]
      .filter((session) => same(session) && session !== source.session);
  }
  return [...(plannerPreview?.sessions || []), ...plannerHistorySessions.filter((session) => session.status === "completed")]
    .filter((session) => same(session) && !(source.key && session.candidate_key === source.key));
}

function pcalCheckTarget(dateKey, weekday, start, source) {
  // The same rules the server applies, so an obviously invalid drop sends nothing.
  const end = start + source.duration;
  const now = plannerNow();
  const todayKey = plannerDateKey(now);
  if (dateKey < todayKey || (dateKey === todayKey && start < now.getHours() * 60 + now.getMinutes())) return "That time has already passed.";
  const deadline = source.deadline || pcalMaterialDeadline(source.documentId);
  if (deadline && dateKey > deadline) return "That is after this material’s deadline.";
  const windows = pcalAvailabilityFor(dateKey, weekday)
    .map((slot) => [plannerToMinutes(slot.start_at), plannerToMinutes(slot.end_at)])
    .sort((left, right) => left[0] - right[0])
    .reduce((merged, [from, to]) => {
      const last = merged[merged.length - 1];
      if (last && from <= last[1]) last[1] = Math.max(last[1], to);
      else merged.push([from, to]);
      return merged;
    }, []);
  if (!windows.some(([from, to]) => from <= start && end <= to)) return "Pick a time inside your available hours.";
  if (pcalGoogleBusyFor(dateKey).some(([from, to]) => from < end && to > start)) return "That time is busy in your Google Calendar.";
  const clash = pcalOccupied(dateKey, source).some((session) => {
    const from = plannerToMinutes(session.scheduled_start.slice(11, 16));
    const to = plannerEndMinutes(session);
    return from < end && to > start;
  });
  return clash ? "That time is already taken by another session." : null;
}

function pcalMoveElements() {
  let preview = document.getElementById("pcal-drop");
  if (!preview) {
    preview = pcalEl("div", "pcal-drop");
    preview.id = "pcal-drop";
    preview.setAttribute("aria-hidden", "true");
    preview.append(pcalEl("strong", "pcal-drop-title"), pcalEl("span", "pcal-drop-time"));
    pcal.scroll.appendChild(preview);
  }
  let chip = document.getElementById("pcal-drag-chip");
  if (!chip) {
    chip = pcalEl("div", "pcal-drag-chip");
    chip.id = "pcal-drag-chip";
    chip.setAttribute("aria-hidden", "true");
    document.body.appendChild(chip);
  }
  return { preview, chip };
}

function pcalUpdateMove(event) {
  const move = pcalMove;
  if (!move.active) {
    if (Math.hypot(event.clientX - move.x, event.clientY - move.y) < 5) return;
    move.active = true;
    pcalClosePopover();
    plannerWorkspace.classList.add("is-moving");
    move.source.element.classList.add("is-moving-source");
  }
  const { preview, chip } = pcalMoveElements();
  const scrollBox = pcal.scroll.getBoundingClientRect();
  const column = [...pcal.body.querySelectorAll(".pcal-col")].find((col) => {
    const box = col.getBoundingClientRect();
    return event.clientX >= box.left && event.clientX < box.right;
  });
  const overGrid = column && event.clientY >= scrollBox.top && event.clientY <= scrollBox.bottom;
  const label = `${pcalActivity(move.source.activity)} · ${move.source.title}`;
  if (!overGrid) {
    move.target = null;
    preview.hidden = true;
    chip.hidden = false;
    chip.textContent = label;
    chip.style.left = `${event.clientX + 12}px`;
    chip.style.top = `${event.clientY + 8}px`;
    return;
  }
  chip.hidden = true;
  const colBox = column.getBoundingClientRect();
  const raw = (event.clientY - colBox.top) / PCAL_MINUTE_PX - move.grabMinutes;
  const start = Math.min(PCAL_DAY_MINUTES - move.source.duration,
    Math.max(0, Math.round(raw / PCAL_SNAP_MINUTES) * PCAL_SNAP_MINUTES));
  const dateKey = column.dataset.date;
  const problem = pcalCheckTarget(dateKey, Number(column.dataset.weekday), start, move.source);
  move.target = { dateKey, start, problem };
  preview.hidden = false;
  preview.classList.toggle("is-invalid", Boolean(problem));
  preview.style.left = `${pcal.body.offsetLeft + column.offsetLeft + 2}px`;
  preview.style.width = `${column.offsetWidth - 4}px`;
  preview.style.top = `${pcal.body.offsetTop + start * PCAL_MINUTE_PX}px`;
  preview.style.height = `${Math.max(12, move.source.duration * PCAL_MINUTE_PX - 2)}px`;
  preview.querySelector(".pcal-drop-title").textContent = move.source.title;
  preview.querySelector(".pcal-drop-time").textContent = `${plannerMinutesToLabel(start)}–${plannerMinutesToLabel(start + move.source.duration)}`;
}

function pcalClearMoveVisuals(source) {
  plannerWorkspace.classList.remove("is-moving");
  source?.element?.classList.remove("is-moving-source");
  document.getElementById("pcal-drop")?.remove();
  document.getElementById("pcal-drag-chip")?.remove();
}

function pcalCancelMove() {
  const move = pcalMove;
  pcalMove = null;
  pcalClearMoveVisuals(move?.source);
}

function pcalBounce(source, message) {
  // An invalid drop: nothing is written; the item settles back where it was.
  if (message) showToast(message);
  const selector = source.kind === "session" ? `.pcal-event[data-session-id="${source.session.session_id}"]`
    : source.key ? `[data-candidate-key="${source.key}"]` : null;
  const element = selector ? plannerWorkspace.querySelector(selector) : null;
  if (!element) return;
  element.classList.remove("is-returning");
  void element.offsetWidth;
  element.classList.add("is-returning");
}

async function pcalEndMove(event) {
  const move = pcalMove;
  pcalMove = null;
  if (!move?.active) {
    pcalClearMoveVisuals(move?.source);
    return;   // a plain click: the popover opens as before
  }
  pcalSuppressClick = true;
  setTimeout(() => { pcalSuppressClick = false; }, 0);
  const { source, target } = move;
  if (!target) {
    pcalClearMoveVisuals(source);
    return;
  }
  if (target.problem) {
    pcalClearMoveVisuals(source);
    pcalBounce(source, target.problem);
    return;
  }
  const startIso = `${target.dateKey}T${plannerMinutesToLabel(target.start)}:00`;
  if (source.kind === "session" && source.session.scheduled_start === startIso) {
    pcalClearMoveVisuals(source);
    return;
  }
  document.getElementById("pcal-drop")?.classList.add("is-saving");
  try {
    if (source.kind === "session") await pcalRescheduleTo(source.session, startIso);
    else if (source.kind === "candidate") await pcalPlaceCandidate(source.key, startIso);
    else pcalSetPlacement(source.key, startIso);
  } finally {
    pcalClearMoveVisuals(source);
  }
}

function pcalSetPlacement(key, startIso) {
  // Before Accept: a draft move of one of the server's own suggestions. The preview re-runs with
  // it and the server applies it only if it is (still) valid; Accept sends the same moves.
  plannerPlacements = [...plannerPlacements.filter((item) => item.candidate_key !== key), { candidate_key: key, scheduled_start: startIso }];
  plannerQueueAutoPreview(0);
}

async function pcalRescheduleTo(session, startIso) {
  if (pcalMoveBusy || plannerBusySessions.has(session.session_id)) return;
  pcalMoveBusy = true;
  plannerBusySessions.add(session.session_id);
  try {
    const result = await plannerRequest(`${PLANNER_SESSIONS_API_URL}/${encodeURIComponent(session.session_id)}/reschedule`, {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes(), target_start: startIso },
    });
    const moved = { ...result.session, plan_id: session.plan_id };
    plannerSessionsById.set(session.session_id, { ...session, ...(result.previous || {}) });
    plannerSessionsById.set(moved.session_id, moved);
    plannerSessions = [...plannerSessions.filter((item) => item.session_id !== session.session_id), moved];
    renderPlannerWorkspace();
    loadTodayPlan();
  } catch (error) {
    renderPlannerWorkspace();
    pcalBounce({ kind: "session", session }, error.message || "Could not move this session");
    if (error.status === 404 || error.detail?.code === "session_not_reschedulable") await loadPlannerData({ keepWeek: true });
  } finally {
    pcalMoveBusy = false;
    plannerBusySessions.delete(session.session_id);
  }
}

async function pcalPlaceCandidate(key, startIso) {
  if (pcalMoveBusy || !plannerPlan) return;
  pcalMoveBusy = true;
  try {
    const result = await plannerRequest(plannerPlanUrl("/candidates/place"), {
      method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes(), candidate_key: key, scheduled_start: startIso },
    });
    plannerSessions = [...plannerSessions, { ...result.session, plan_id: plannerPlan.plan_id }];
    plannerLiveCandidates = plannerLiveCandidates.filter((candidate) => candidate.candidate_key !== key);
    renderPlannerWorkspace();
    loadTodayPlan();
    plannerLoadLiveCandidates();
  } catch (error) {
    pcalBounce({ kind: "candidate", key }, error.message || "Could not add this session");
    if (error.detail?.code === "candidate_stale" || error.detail?.code === "stale_plan") await loadPlannerData({ keepWeek: true });
  } finally {
    pcalMoveBusy = false;
  }
}

// -- Google Calendar (one-way: Google busy time in, confirmed study sessions out) ------------------
// The backend owns OAuth and every token; the page only sees connection status, the two toggles,
// and anonymous busy blocks ("Busy" -- never an event's name).

function plannerGoogleUrl(suffix) {
  return `${GOOGLE_CALENDAR_API_URL}${suffix}`;
}

function plannerGoogleNavigate(url) {
  // The OAuth flow is a full-page redirect through the backend. A single seam so tests can observe it.
  window.location.assign(url);
}

function plannerGoogleAvoiding() {
  return Boolean(plannerGoogle?.connected && plannerGoogle.avoid_conflicts);
}

function plannerHandleGoogleReturn() {
  // Back from Google's consent screen (?google_calendar=connected|error&reason=...): open the Planner.
  const params = new URLSearchParams(window.location.search);
  const outcome = params.get("google_calendar");
  if (!outcome) return;
  const reason = params.get("reason");
  params.delete("google_calendar");
  params.delete("reason");
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
  setPage("planner");
  showToast(outcome === "connected" ? "Google Calendar connected"
    : PCAL_GOOGLE_RETURN_ERRORS[reason] || "Could not connect Google Calendar");
}

async function plannerLoadGoogleCalendar() {
  try {
    const status = await plannerRequest(plannerGoogleUrl(`/status?utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    plannerGoogle = status && typeof status === "object" && "connected" in status ? status : null;
  } catch (error) {
    plannerGoogle = null;   // unknown: the settings section stays hidden and the Planner works as before
  }
  plannerGoogleStatusFailed = !plannerGoogle;   // the toolbar slot shows an error + Retry instead of vanishing
  if (plannerGoogle) plannerGoogleSyncFailed = plannerGoogle.sync_errors > 0;
  plannerGoogleBusyWeek = null;
  plannerGoogleWarning = plannerGoogle?.reconnect_required ? "google_calendar_reconnect" : null;
  if (!plannerGoogleAvoiding()) plannerGoogleBusy = [];
  pcalRenderGoogleCalendar();
  if (plannerWorkspace && !plannerWorkspace.hidden) pcalRenderGrid();   // also loads this week's busy blocks
}

async function pcalLoadGoogleBusy() {
  if (!plannerGoogleAvoiding()) return;
  const weekKey = plannerDateKey(pcalWeekDates()[0]);
  if (plannerGoogleBusyWeek === weekKey) return;
  plannerGoogleBusyWeek = weekKey;
  let busy = [];
  let warning = null;
  try {
    const result = await plannerRequest(plannerGoogleUrl(
      `/busy?start=${weekKey}&days=7&utc_offset_minutes=${plannerUtcOffsetMinutes()}`));
    busy = result.busy || [];
    warning = result.warning || null;
  } catch (error) {
    warning = "google_calendar_unavailable";
  }
  if (plannerGoogleBusyWeek !== weekKey) return;   // the learner moved on to another week meanwhile
  plannerGoogleBusy = busy;
  plannerGoogleWarning = warning;
  pcalRenderGoogleCalendar();
  if (plannerWorkspace && !plannerWorkspace.hidden) pcalRenderGrid();
}

function pcalGoogleBusyFor(dateKey) {
  // [startMinute, endMinute] of each busy block on that day (a block across midnight is cut at it).
  if (!plannerGoogleAvoiding()) return [];
  return plannerGoogleBusy.flatMap(({ start, end }) => {
    if (start.slice(0, 10) > dateKey || end.slice(0, 10) < dateKey) return [];
    const from = start.slice(0, 10) < dateKey ? 0 : plannerToMinutes(start.slice(11, 16));
    // A partial last minute still blocks, as in google_calendar_sync.busy_availability_rows.
    const to = end.slice(0, 10) > dateKey ? PCAL_DAY_MINUTES
      : Math.min(PCAL_DAY_MINUTES, plannerToMinutes(end.slice(11, 16)) + (/[1-9]/.test(end.slice(17)) ? 1 : 0));
    return to > from ? [[from, to]] : [];
  });
}

function pcalBusyBlock(start, end) {
  // Read-only: not a button, not draggable, no popover; it only blocks placement.
  const block = pcalEl("div", "pcal-busy");
  block.dataset.start = plannerMinutesToLabel(start);
  block.dataset.end = plannerMinutesToLabel(end);
  block.setAttribute("aria-label", `Busy in Google Calendar ${plannerMinutesToLabel(start)}–${plannerMinutesToLabel(end)}`);
  pcalPlace(block, start, end);
  block.appendChild(pcalEl("span", "pcal-busy-label", "Busy"));
  return block;
}

function plannerNoteCalendarSync(outcome) {
  plannerGoogleSyncFailed = outcome.status === "error";
  if (outcome.status === "reconnect_required") {
    plannerLoadGoogleCalendar();
    return;
  }
  pcalRenderGoogleCalendar();
}

function pcalGoogleWarningCode() {
  const previewWarning = (plannerPreview?.warnings || []).find((warning) => PCAL_GOOGLE_WARNINGS[warning.code]);
  return plannerGoogleWarning || previewWarning?.code || null;
}

function pcalGoogleButton(id, text, onClick, className = "pcal-button pcal-secondary") {
  const button = pcalEl("button", className, text);
  button.type = "button";
  button.id = id;
  button.addEventListener("click", () => onClick(button));
  return button;
}

function pcalGoogleToggle(id, text, checked, field) {
  const label = pcalEl("label", "pcal-gcal-toggle");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.checked = checked;
  input.addEventListener("change", () => pcalGoogleSetting(field, input));
  label.append(input, pcalEl("span", "", text));
  return label;
}

// The always-visible toolbar control (desktop calendar toolbar and the step-by-step layout's header):
// the full settings stay in the Materials panel, which is a closed popover by default.
function plannerRenderGoogleCta() {
  document.querySelectorAll(".planner-gcal-cta").forEach((slot) => {
    slot.innerHTML = "";
    slot.hidden = !plannerGoogle && !plannerGoogleStatusFailed;   // hidden only before the first status answer
    if (!plannerGoogle) {
      if (!plannerGoogleStatusFailed) return;
      // Status unknown: never guess "configured" or start OAuth; offer a retry of the status request.
      const unavailable = pcalEl("span", "planner-gcal-unavailable", "Google Calendar unavailable");
      unavailable.setAttribute("role", "status");
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "pcal-button planner-gcal-retry";
      retry.textContent = "Retry";
      retry.addEventListener("click", () => plannerLoadGoogleCalendar());
      slot.append(unavailable, retry);
      return;
    }
    if (plannerGoogle.connected && !plannerGoogle.reconnect_required) {
      const state = pcalEl("span", "planner-gcal-connected", "✓ Google Calendar connected");
      state.setAttribute("role", "status");
      slot.appendChild(state);
      return;
    }
    const connect = document.createElement("button");
    connect.type = "button";
    connect.className = "pcal-button planner-gcal-connect";
    connect.textContent = plannerGoogle.reconnect_required ? "Reconnect Google Calendar" : "Connect Google Calendar";
    connect.disabled = !plannerGoogle.configured;
    if (!plannerGoogle.configured) connect.title = "Google Calendar is not set up on this server yet.";
    connect.addEventListener("click", () => plannerGoogleNavigate(plannerGoogleUrl("/connect")));
    slot.appendChild(connect);
  });
}

function pcalRenderGoogleCalendar() {
  plannerRenderGoogleCta();
  const box = pcal.gcal;
  if (!box) return;
  box.innerHTML = "";
  box.hidden = !plannerGoogle;
  if (!plannerGoogle) return;
  const title = pcalEl("h3", "pcal-rail-title", "Google Calendar");
  title.id = "pcal-gcal-title";
  box.appendChild(title);
  const warning = pcalGoogleWarningCode();
  if (plannerGoogle.connected) {
    box.appendChild(pcalEl("p", "pcal-gcal-state", "✓ Connected"));
    box.append(pcalGoogleToggle("pcal-gcal-avoid", "Avoid conflicts", plannerGoogle.avoid_conflicts, "avoid_conflicts"),
      pcalGoogleToggle("pcal-gcal-sync", "Sync confirmed study sessions", plannerGoogle.sync_sessions, "sync_sessions"));
    if (plannerGoogleSyncFailed) {
      const failed = pcalEl("p", "pcal-gcal-error", "Calendar sync failed");
      failed.id = "pcal-gcal-sync-error";
      failed.setAttribute("role", "status");
      box.appendChild(failed);
    }
    if (warning) box.appendChild(pcalEl("p", "pcal-gcal-hint is-warn", PCAL_GOOGLE_WARNINGS[warning]));
    const actions = pcalEl("div", "pcal-gcal-actions");
    actions.append(pcalGoogleButton("pcal-gcal-sync-now", "Sync now", pcalGoogleSyncNow),
      pcalGoogleButton("pcal-gcal-disconnect", "Disconnect", pcalGoogleDisconnect));
    box.appendChild(actions);
    return;
  }
  if (plannerGoogle.reconnect_required) box.appendChild(pcalEl("p", "pcal-gcal-hint is-warn", PCAL_GOOGLE_WARNINGS.google_calendar_reconnect));
  else if (!plannerGoogle.configured) box.appendChild(pcalEl("p", "pcal-gcal-hint", "Not set up on this server yet."));
  else box.appendChild(pcalEl("p", "pcal-gcal-hint", "Avoid your busy times and add study sessions to your calendar."));
  const connect = pcalGoogleButton("pcal-gcal-connect", plannerGoogle.reconnect_required ? "Reconnect" : "Connect",
    () => plannerGoogleNavigate(plannerGoogleUrl("/connect")), "pcal-button");
  connect.disabled = !plannerGoogle.configured;
  box.appendChild(connect);
}

function pcalAfterGoogleBusyChange() {
  // Busy time feeds the server-side scheduler: refresh the blocks and, before confirmation, the suggestions.
  plannerGoogleBusy = [];
  plannerGoogleBusyWeek = null;
  plannerGoogleWarning = null;
  renderPlannerWorkspace();
  if (!plannerHasLivePlan()) plannerQueueAutoPreview(0);
}

async function pcalGoogleSetting(field, input) {
  input.disabled = true;
  try {
    plannerGoogle = await plannerRequest(plannerGoogleUrl("/settings"), { method: "PATCH", body: { [field]: input.checked } });
    if (field === "avoid_conflicts") pcalAfterGoogleBusyChange();
    else {
      plannerGoogleSyncFailed = plannerGoogle.sync_errors > 0;
      pcalRenderGoogleCalendar();
    }
  } catch (error) {
    input.checked = !input.checked;
    input.disabled = false;
    showToast(error.message || "Could not update Google Calendar settings");
  }
}

async function pcalGoogleSyncNow(button) {
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    const result = await plannerRequest(plannerGoogleUrl("/sync"), { method: "POST", body: { utc_offset_minutes: plannerUtcOffsetMinutes() } });
    plannerGoogle = result;
    plannerGoogleSyncFailed = result.sync.status !== "ok" || result.sync_errors > 0;
    showToast(plannerGoogleSyncFailed ? "Calendar sync failed" : "Google Calendar is up to date");
    if (result.sync.status === "reconnect_required") await plannerLoadGoogleCalendar();
  } catch (error) {
    plannerGoogleSyncFailed = true;
    showToast(error.message || "Calendar sync failed");
  }
  pcalRenderGoogleCalendar();
}

async function pcalGoogleDisconnect(button) {
  button.disabled = true;
  try {
    plannerGoogle = await plannerRequest(plannerGoogleUrl("/disconnect"), { method: "POST" });
    plannerGoogleSyncFailed = false;
    showToast("Google Calendar disconnected");
    pcalAfterGoogleBusyChange();
  } catch (error) {
    button.disabled = false;
    showToast(error.message || "Could not disconnect Google Calendar");
  }
}

// -- readable sessions: labels, lanes, week/agenda view, zoom ---------------------------------
// Presentation only: stored document names, ids and session times are never changed.

function plannerDisplayTitle(raw) {
  // A readable label from a stored document name: "Lecture_03-Embedded_Systems.pdf" ->
  // "Lecture 03 Embedded Systems". Names already written with spaces keep their hyphens.
  const name = String(raw || "").trim();
  let label = name.replace(/\.(pdf|docx?|pptx?|txt|md)$/i, "").replace(/_+/g, " ");
  if (!/\s/.test(name)) label = label.replace(/-+/g, " ");
  label = label.replace(/\s+/g, " ").trim();
  return label || name;
}

function plannerSessionTitle(session) {
  return session.document_title || plannerDocumentTitle(session.document_id);
}

function plannerValidateNewSessionMinutes(minutes) {
  // The rule every NEW session follows (backend/study_session_duration.py): a whole number of
  // minutes, at least the minimum. Returns an error message, or "" when valid. Existing sessions are
  // never checked or resized.
  if (!Number.isInteger(minutes)) return "Choose a session length in whole minutes.";
  if (minutes < PLANNER_MIN_SESSION_MINUTES) return `A study session must be at least ${PLANNER_MIN_SESSION_MINUTES} minutes long.`;
  return "";
}

function pcalLayoutLanes(ranges) {
  // Side-by-side lanes for events that overlap in time, each keeping its real start and end:
  // [{start, end}] -> [{lane, lanes}] in input order. Events that overlap nothing use the full width.
  const result = new Array(ranges.length);
  const order = ranges.map((range, index) => ({ ...range, index }))
    .sort((left, right) => left.start - right.start || right.end - left.end);
  let cluster = [];
  let laneEnds = [];
  let clusterEnd = -Infinity;
  const close = () => {
    cluster.forEach((item) => { result[item.index].lanes = laneEnds.length; });
    cluster = [];
    laneEnds = [];
  };
  order.forEach((item) => {
    if (item.start >= clusterEnd) {
      close();
      clusterEnd = -Infinity;
    }
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= item.start);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(item.end);
    } else laneEnds[lane] = item.end;
    result[item.index] = { lane, lanes: 1 };
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.end);
  });
  close();
  return result;
}

function pcalGridColumns(dates) {
  // Hour gutter + seven days; an expanded day takes about half the width, the others stay visible.
  return `52px ${dates.map((date) => (plannerDateKey(date) === pcalFocusDay ? "minmax(0,4fr)" : "minmax(0,1fr)")).join(" ")}`;
}

function pcalToggleFocusDay(dateKey) {
  pcalFocusDay = pcalFocusDay === dateKey ? null : dateKey;
  pcalClosePopover();
  const top = pcal.scroll.scrollTop;
  pcalRenderGrid();
  pcal.scroll.scrollTop = top;
  pcal.head.querySelector(`.pcal-dayhead[data-date="${dateKey}"]`)?.focus();
}

function pcalStorage(key, value) {
  // Per-viewer conveniences only (view, zoom); the page works the same without storage.
  try {
    if (value === undefined) return window.localStorage.getItem(key);
    window.localStorage.setItem(key, value);
  } catch (error) {
    // storage unavailable (private window, blocked site data)
  }
  return null;
}

function pcalSetHourPx(px, { save = true } = {}) {
  PCAL_HOUR_PX = px;
  PCAL_MINUTE_PX = px / 60;
  plannerWorkspace?.style.setProperty("--pcal-hour", `${px}px`);
  if (save) pcalStorage("planner.hourPx", String(px));
}

function pcalRestorePreferences() {
  const view = pcalStorage("planner.view");
  if (view === "week" || view === "agenda") pcalView = view;
  const hour = Number(pcalStorage("planner.hourPx"));
  pcalSetHourPx(PCAL_ZOOM_LEVELS.includes(hour) ? hour : PCAL_HOUR_PX, { save: false });
}

function pcalZoom(step) {
  // Taller or shorter hours; the time at the top of the grid stays in place.
  const current = PCAL_ZOOM_LEVELS.indexOf(PCAL_HOUR_PX);
  const from = current === -1 ? PCAL_ZOOM_LEVELS.indexOf(PCAL_DEFAULT_HOUR_PX) : current;
  const index = Math.min(PCAL_ZOOM_LEVELS.length - 1, Math.max(0, from + step));
  if (PCAL_ZOOM_LEVELS[index] === PCAL_HOUR_PX) return;
  const topMinute = pcal.scroll.scrollTop / PCAL_MINUTE_PX;
  pcalSetHourPx(PCAL_ZOOM_LEVELS[index]);
  pcalClosePopover();
  pcalApplyView();
  pcalRenderGrid();
  pcal.scroll.scrollTop = topMinute * PCAL_MINUTE_PX;
}

function pcalSetView(view) {
  if (view === pcalView) return;
  pcalView = view;
  pcalStorage("planner.view", view);
  pcalClosePopover();
  pcalApplyView();
  if (view === "week") {
    pcalScrolledWeek = null;   // the grid was hidden: anchor its scroll again
    pcalRenderGrid();
  } else pcalRenderAgenda();
}

function pcalApplyView() {
  if (!pcal.agenda) return;
  const agenda = pcalView === "agenda";
  pcal.scroll.hidden = agenda;
  pcal.agenda.hidden = !agenda;
  plannerWorkspace.classList.toggle("is-agenda", agenda);
  pcal.viewWeek.setAttribute("aria-pressed", String(!agenda));
  pcal.viewAgenda.setAttribute("aria-pressed", String(agenda));
  pcal.zoomIn.disabled = PCAL_HOUR_PX >= PCAL_ZOOM_LEVELS[PCAL_ZOOM_LEVELS.length - 1];
  pcal.zoomOut.disabled = PCAL_HOUR_PX <= PCAL_ZOOM_LEVELS[0];
  pcal.zoomIn.parentElement.hidden = agenda;
}

function pcalItemOrder(left, right) {
  return left.session.scheduled_start.localeCompare(right.session.scheduled_start)
    || left.session.scheduled_end.localeCompare(right.session.scheduled_end);
}

function pcalRenderAgenda() {
  // The visible week as readable cards, grouped by day: subject, activity, time, duration, status
  // and the same actions as the calendar popover.
  const box = pcal.agenda;
  if (!box || pcalView !== "agenda" || plannerWorkspace.hidden) return;
  const dates = pcalWeekDates();
  const fromKey = plannerDateKey(dates[0]), untilKey = plannerDateKey(dates[6]);
  const todayKey = plannerDateKey(plannerNow());
  const items = pcalWeekItems(fromKey, untilKey).sort(pcalItemOrder);
  box.innerHTML = "";
  if (!items.length) {
    const empty = pcalEl("div", "pcal-agenda-empty");
    empty.appendChild(pcalEl("strong", "", "No study sessions this week."));
    const next = plannerSessions.filter((session) => session.scheduled_start.slice(0, 10) > untilKey)
      .sort((left, right) => left.scheduled_start.localeCompare(right.scheduled_start))[0];
    if (next) empty.appendChild(pcalEl("span", "", `Next session: ${plannerDayLabel(next.scheduled_start.slice(0, 10))}, ${next.scheduled_start.slice(11, 16)}.`));
    box.appendChild(empty);
    return;
  }
  dates.forEach((date) => {
    const key = plannerDateKey(date);
    const dayItems = items.filter((item) => item.session.scheduled_start.slice(0, 10) === key);
    if (!dayItems.length) return;
    const day = pcalEl("section", "pcal-agenda-day");
    day.dataset.date = key;
    day.classList.toggle("is-today", key === todayKey);
    day.classList.toggle("is-past", key < todayKey);
    const heading = pcalEl("h3", "pcal-agenda-date");
    heading.appendChild(pcalEl("span", "pcal-agenda-day-label", plannerDayLabel(key)));
    if (key === todayKey) heading.appendChild(pcalEl("span", "pcal-agenda-today", "Today"));
    const counted = dayItems.filter((item) => !["moving", "cancelled"].includes(item.change?.type));
    const minutes = counted.reduce((sum, item) => sum + (item.session.duration_minutes || 0), 0);
    heading.appendChild(pcalEl("span", "pcal-agenda-total",
      `${counted.length} session${counted.length === 1 ? "" : "s"} · ${plannerFormatDuration(minutes)}`));
    const list = pcalEl("ol", "pcal-agenda-list");
    dayItems.forEach((item) => list.appendChild(pcalAgendaCard(item)));
    day.append(heading, list);
    box.appendChild(day);
  });
}

function pcalAgendaCard({ kind, session, change = null }) {
  const fullTitle = plannerSessionTitle(session);
  const title = plannerDisplayTitle(fullTitle);
  const activity = pcalActivity(session.activity_type, session.steps);
  const card = pcalEl("li", `pcal-agenda-card pcal-agenda-card--${kind} pcal-activity--${session.activity_type}${session.steps?.length ? " pcal-agenda-card--combined" : ""}`);
  card.dataset.kind = kind;
  card.dataset.start = session.scheduled_start;
  if (session.session_id) card.dataset.sessionId = session.session_id;
  if (change) card.classList.add("pcal-change", `pcal-change--${change.type}`);

  const time = pcalEl("div", "pcal-agenda-time");
  time.append(pcalEl("strong", "pcal-agenda-start", session.scheduled_start.slice(11, 16)),
    pcalEl("span", "pcal-agenda-end", `to ${session.scheduled_end.slice(11, 16)}`),
    pcalEl("span", "pcal-agenda-duration", plannerFormatDuration(session.duration_minutes || 0)));

  const body = pcalEl("div", "pcal-agenda-body");
  const top = pcalEl("div", "pcal-agenda-top");
  const heading = pcalEl("h4", "pcal-agenda-title", title);
  if (title !== fullTitle) heading.title = fullTitle;
  top.append(heading, pcalEl("span", `pcal-agenda-status pcal-kind pcal-kind--${kind}`,
    change ? PCAL_CHANGE_KIND_LABELS[change.type] : PCAL_KIND_LABELS[kind]));
  const topic = pcalEl("p", "pcal-agenda-topic");
  topic.appendChild(pcalEl("span", "pcal-agenda-activity", activity));
  const reason = change ? plannerChangeWhy(change.type === "moving" ? "moved" : change.type, change.item) : pcalReasonText(session.reason);
  if (reason) topic.appendChild(pcalEl("span", "pcal-agenda-reason", reason));
  body.append(top, topic);
  const deadline = pcalMaterialDeadline(session.document_id);
  if (deadline && !PLANNER_HISTORY_SESSION_STATUSES.includes(session.status)) {
    body.appendChild(pcalEl("p", "pcal-agenda-note",
      `Due ${new Date(`${deadline}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`));
  }
  if (kind === "suggested") body.appendChild(pcalEl("p", "pcal-agenda-note", "Suggested. Accept plan to save it."));
  if (kind === "overdue") body.appendChild(pcalEl("p", "pcal-agenda-note pcal-agenda-note--warn", "Session not completed"));

  const actions = pcalEl("div", "pcal-agenda-actions pcal-session-actions");
  const name = `${fullTitle} (${activity})`;
  if (session.steps?.length && !change) body.appendChild(pcalStepList(session));
  (session.session_id && !change ? plannerSessionActions(kind, session) : []).forEach(([action, label, primary]) => {
    const button = pcalEl("button", `${primary ? "primary-button" : "pcal-text-action"} planner-session-action planner-session-${action}`, label);
    button.type = "button";
    button.dataset.sessionAction = action;
    button.dataset.sessionId = session.session_id;
    button.setAttribute("aria-label", `${label} ${name}`);
    // The same lifecycle call as the calendar popover and Home: one request per session at a time.
    button.addEventListener("click", () => plannerSessionAction(session, action, button, actions));
    actions.appendChild(button);
  });
  const details = pcalEl("button", "pcal-text-action pcal-agenda-details", "Details");
  details.type = "button";
  details.setAttribute("aria-label", `Details for ${name}`);
  details.addEventListener("click", () => (change ? pcalOpenChangePopover(change, session, card) : pcalOpenSessionPopover(kind, session, card)));
  actions.appendChild(details);
  body.appendChild(actions);
  card.append(time, body);
  return card;
}
