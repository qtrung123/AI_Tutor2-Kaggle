// Subjects: the user's own subject/course groups for documents -- Home's My Subjects, the optional
// subject of an upload and the Planner's subject filter. Organizational only: a document belongs to
// at most one subject, and deleting a subject only moves its documents to Unassigned.
// Classic script (shared global scope), loaded by index.html before home.js and app.js.

const SUBJECTS_API_URL = apiUrl("/api/subjects");
// The same palette the backend assigns in turn (backend/subject_store.py SUBJECT_COLORS).
const SUBJECT_COLORS = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#64748b"];
const SUBJECT_UNASSIGNED = "unassigned";
const SUBJECT_ALL = "all";

let userSubjects = [];                       // [{subject_id, name, color, document_ids, document_count}]
let homeSubjectFilter = SUBJECT_ALL;         // Home: "all" | "unassigned" | a subject_id
let homeSelectedDocuments = new Set();       // Home: documents picked for a bulk move
let plannerSubjectFilter = SUBJECT_ALL;      // Planner: display-only filter, never sent to the scheduler

function setUserSubjects(subjects) {
  userSubjects = Array.isArray(subjects) ? subjects : [];
  const known = (filter) => filter === SUBJECT_ALL || filter === SUBJECT_UNASSIGNED || Boolean(subjectById(filter));
  if (!known(homeSubjectFilter)) homeSubjectFilter = SUBJECT_ALL;
  if (!known(plannerSubjectFilter)) plannerSubjectFilter = SUBJECT_ALL;
}

function subjectById(subjectId) {
  return userSubjects.find((subject) => subject.subject_id === subjectId) || null;
}

function subjectOfDocument(documentId) {
  return userSubjects.find((subject) => (subject.document_ids || []).includes(documentId)) || null;
}

function subjectFilterMatches(filter, documentId) {
  if (filter === SUBJECT_ALL) return true;
  const subject = subjectOfDocument(documentId);
  return filter === SUBJECT_UNASSIGNED ? !subject : subject?.subject_id === filter;
}

function subjectCountText(count) {
  return `${count} document${count === 1 ? "" : "s"}`;
}

// A small colored dot + name, e.g. in a document row or a planner material.
function subjectBadge(subject, className = "") {
  const badge = document.createElement("span");
  badge.className = `subject-badge ${className}`.trim();
  if (subject) badge.style.setProperty("--subject-color", subject.color);
  else badge.classList.add("subject-badge--none");
  badge.textContent = subject ? subject.name : "Unassigned";
  return badge;
}

async function subjectRequest(url, method, body) {
  return fetchJson(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// Every change re-reads the subject list from the response and redraws what shows subjects.
function afterSubjectsChange(subjects) {
  setUserSubjects(subjects);
  homeSelectedDocuments = new Set([...homeSelectedDocuments].filter((id) => (dashboardData?.materials || []).some((m) => m.document_id === id)));
  renderHomeSubjects();
  renderHomeDocumentCards();
  if (document.body.dataset.page === "planner") renderPlanner();
}

async function refreshSubjects() {
  try { afterSubjectsChange(await fetchJson(SUBJECTS_API_URL)); }
  catch (error) { /* keep the list already shown */ }
}

async function createSubject(name, color) {
  const subject = await subjectRequest(SUBJECTS_API_URL, "POST", { name, color });
  await refreshSubjects();
  return subject;
}

async function renameSubject(subject) {
  const values = await subjectDialog({ title: "Edit subject", name: subject.name, color: subject.color, confirmLabel: "Save" });
  if (!values) return;
  try {
    await subjectRequest(`${SUBJECTS_API_URL}/${encodeURIComponent(subject.subject_id)}`, "PATCH", values);
    await refreshSubjects();
    showToast("Subject updated");
  } catch (error) {
    showToast(error.message || "Subject could not be updated");
  }
}

async function deleteSubject(subject, button) {
  const count = subject.document_count || 0;
  if (!window.confirm(`Delete subject "${subject.name}"? Its documents will be kept and moved to Unassigned`
    + `${count ? ` (${subjectCountText(count)})` : ""}. Nothing else is deleted.`)) return;
  if (button) button.disabled = true;
  try {
    const result = await subjectRequest(`${SUBJECTS_API_URL}/${encodeURIComponent(subject.subject_id)}`, "DELETE");
    if (homeSubjectFilter === subject.subject_id) homeSubjectFilter = SUBJECT_ALL;
    afterSubjectsChange(result.subjects);
    showToast("Subject deleted; its documents are now Unassigned");
  } catch (error) {
    if (button) button.disabled = false;
    showToast(error.message || "Subject could not be deleted");
  }
}

// Assign, move or (subjectId null) unassign documents.
async function moveDocumentsToSubject(documentIds, subjectId) {
  if (!documentIds.length) return false;
  try {
    const result = await subjectRequest(`${SUBJECTS_API_URL}/assign`, "POST", { document_ids: documentIds, subject_id: subjectId });
    afterSubjectsChange(result.subjects);
    const target = subjectId ? subjectById(subjectId)?.name || "subject" : "Unassigned";
    showToast(`${documentIds.length === 1 ? "Document" : `${documentIds.length} documents`} moved to ${target}`);
    return true;
  } catch (error) {
    showToast(error.message || "Documents could not be moved");
    return false;
  }
}

// A subject <select>: Unassigned, every subject, and "New subject…" (asks for a name, creates it).
function subjectSelect({ value = null, label, includeNew = true, noneLabel = "Unassigned" }) {
  const select = document.createElement("select");
  select.className = "subject-select";
  select.setAttribute("aria-label", label);
  const add = (optionValue, text) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = text;
    select.appendChild(option);
  };
  add("", noneLabel);
  userSubjects.forEach((subject) => add(subject.subject_id, subject.name));
  if (includeNew) add("__new__", "+ New subject…");
  select.value = value || "";
  return select;
}

// Resolves a select's value to a subject id (creating a new subject when asked); undefined = cancelled.
async function resolveSubjectChoice(select, previous) {
  if (select.value !== "__new__") return select.value || null;
  const values = await subjectDialog({ title: "New subject", confirmLabel: "Create" });
  if (!values) { select.value = previous || ""; return undefined; }
  try {
    return (await createSubject(values.name, values.color)).subject_id;
  } catch (error) {
    showToast(error.message || "Subject could not be created");
    select.value = previous || "";
    return undefined;
  }
}

// ---- dialogs ------------------------------------------------------------------------------------

function subjectDialogShell(titleText) {
  const dialog = document.createElement("dialog");
  dialog.className = "subject-dialog";
  const form = document.createElement("form");
  form.method = "dialog";
  form.className = "subject-dialog-form";
  const title = document.createElement("h2");
  title.className = "subject-dialog-title";
  title.id = `subject-dialog-title-${Date.now()}`;
  title.textContent = titleText;
  dialog.setAttribute("aria-labelledby", title.id);
  form.appendChild(title);
  dialog.appendChild(form);
  document.body.appendChild(dialog);
  return { dialog, form };
}

function subjectDialogActions(form, confirmLabel) {
  const actions = document.createElement("div");
  actions.className = "subject-dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "text-button subject-dialog-cancel";
  cancel.textContent = "Cancel";
  const confirm = document.createElement("button");
  confirm.type = "submit";
  confirm.className = "primary-button subject-dialog-confirm";
  confirm.textContent = confirmLabel;
  actions.append(cancel, confirm);
  form.appendChild(actions);
  return { cancel, confirm };
}

function subjectNameField(form, name) {
  const field = document.createElement("label");
  field.className = "subject-dialog-field";
  const caption = document.createElement("span");
  caption.textContent = "Subject name";
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 80;
  input.required = true;
  input.placeholder = "e.g. Biology";
  input.value = name || "";
  field.append(caption, input);
  form.appendChild(field);
  return { field, input };
}

function subjectColorField(form, color) {
  const group = document.createElement("fieldset");
  group.className = "subject-dialog-colors";
  const legend = document.createElement("legend");
  legend.textContent = "Color";
  group.appendChild(legend);
  const chosen = SUBJECT_COLORS.includes(color) ? color : SUBJECT_COLORS[userSubjects.length % SUBJECT_COLORS.length];
  SUBJECT_COLORS.forEach((value, index) => {
    const swatch = document.createElement("label");
    swatch.className = "subject-swatch";
    swatch.style.setProperty("--subject-color", value);
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "subject-color";
    radio.value = value;
    radio.checked = value === chosen;
    const text = document.createElement("span");
    text.className = "visually-hidden";
    text.textContent = `Color ${index + 1}`;
    swatch.append(radio, text);
    group.appendChild(swatch);
  });
  form.appendChild(group);
  return () => group.querySelector("input:checked")?.value || chosen;
}

function subjectDialogError(form) {
  const error = document.createElement("p");
  error.className = "subject-dialog-error";
  error.setAttribute("role", "alert");
  error.hidden = true;
  form.appendChild(error);
  return error;
}

// Shows the dialog; returns finish(result), which closes and removes it and resolves exactly once.
// Escape (the dialog's cancel event) finishes with null.
function openSubjectDialog(dialog, resolve) {
  let done = false;
  const finish = (result) => {
    if (done) return;
    done = true;
    if (dialog.open) dialog.close();
    dialog.remove();
    resolve(result);
  };
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(null); });
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  return finish;
}

// Name + color. Resolves {name, color}, or null when cancelled.
function subjectDialog({ title, name = "", color = null, confirmLabel }) {
  return new Promise((resolve) => {
    const { dialog, form } = subjectDialogShell(title);
    const { input } = subjectNameField(form, name);
    const readColor = subjectColorField(form, color);
    const error = subjectDialogError(form);
    const { cancel } = subjectDialogActions(form, confirmLabel);
    const finish = openSubjectDialog(dialog, resolve);
    cancel.addEventListener("click", () => finish(null));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim().replace(/\s+/g, " ");
      if (!value) { error.textContent = "Enter a subject name."; error.hidden = false; input.focus(); return; }
      finish({ name: value, color: readColor() });
    });
    input.focus();
  });
}

// Before an upload: an optional subject for all chosen files. Resolves {} (no subject),
// {subject_id}, {new_subject_name}, or null when cancelled. Home's current subject is preselected.
function chooseUploadSubject(files) {
  return new Promise((resolve) => {
    const count = files.length;
    const { dialog, form } = subjectDialogShell(count === 1 ? "Add 1 document" : `Add ${count} documents`);
    const names = document.createElement("p");
    names.className = "subject-dialog-files";
    names.textContent = Array.from(files, (file) => file.name).join(", ");
    form.appendChild(names);
    const field = document.createElement("label");
    field.className = "subject-dialog-field";
    const caption = document.createElement("span");
    caption.textContent = "Subject (optional)";
    const preset = subjectById(homeSubjectFilter) ? homeSubjectFilter : "";
    const select = subjectSelect({ value: preset, label: "Subject for the uploaded files", noneLabel: "No subject (Unassigned)" });
    field.append(caption, select);
    form.appendChild(field);
    const { field: nameField, input } = subjectNameField(form, "");
    nameField.hidden = true;
    const error = subjectDialogError(form);
    const { cancel, confirm } = subjectDialogActions(form, "Upload");
    input.required = false;   // only asked for when "New subject…" is chosen
    select.addEventListener("change", () => {
      nameField.hidden = select.value !== "__new__";
      if (!nameField.hidden) input.focus();
    });
    const finish = openSubjectDialog(dialog, resolve);
    cancel.addEventListener("click", () => finish(null));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (select.value === "__new__") {
        const value = input.value.trim().replace(/\s+/g, " ");
        if (!value) { error.textContent = "Enter a name for the new subject."; error.hidden = false; input.focus(); return; }
        finish({ new_subject_name: value });
      } else {
        finish(select.value ? { subject_id: select.value } : {});
      }
    });
    confirm.focus();
  });
}

async function uploadWithSubjectChoice(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const choice = await chooseUploadSubject(list);
  if (choice === null) {   // cancelled: nothing is uploaded
    sourceFileInput.value = "";
    return;
  }
  await uploadSourceFiles(list, choice);
}

// ---- Home: My Subjects ----------------------------------------------------------------------------

function homeSubjectCard({ key, name, count, color = null, subject = null }) {
  const card = document.createElement("div");
  card.className = "subject-card";
  card.dataset.subjectFilter = key;
  if (color) card.style.setProperty("--subject-color", color);
  if (key === SUBJECT_ALL) card.classList.add("subject-card--all");
  if (key === SUBJECT_UNASSIGNED) card.classList.add("subject-card--unassigned");
  const active = homeSubjectFilter === key;
  card.classList.toggle("is-active", active);
  const main = document.createElement("button");
  main.type = "button";
  main.className = "subject-card-main";
  main.setAttribute("aria-pressed", String(active));
  main.setAttribute("aria-label", `Show ${name}: ${subjectCountText(count)}`);
  const dot = document.createElement("span");
  dot.className = "subject-card-dot";
  dot.setAttribute("aria-hidden", "true");
  const title = document.createElement("strong");
  title.className = "subject-card-name";
  title.textContent = name;
  const meta = document.createElement("span");
  meta.className = "subject-card-count";
  meta.textContent = subjectCountText(count);
  main.append(dot, title, meta);
  main.addEventListener("click", () => {
    homeSubjectFilter = key;
    homeSelectedDocuments.clear();
    renderHomeSubjects();
    renderHomeDocumentCards();
  });
  card.appendChild(main);
  if (subject) {
    const actions = document.createElement("div");
    actions.className = "subject-card-actions";
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "text-button subject-card-rename";
    rename.textContent = "Edit";
    rename.setAttribute("aria-label", `Rename or recolor ${subject.name}`);
    rename.addEventListener("click", () => renameSubject(subject));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button danger-button subject-card-delete";
    remove.textContent = "Delete";
    remove.setAttribute("aria-label", `Delete subject ${subject.name}`);
    remove.addEventListener("click", () => deleteSubject(subject, remove));
    actions.append(rename, remove);
    card.appendChild(actions);
  }
  return card;
}

function renderHomeSubjects() {
  const panel = document.getElementById("home-subjects");
  if (!panel) return;
  const materials = dashboardData?.materials || [];
  panel.innerHTML = "";
  panel.hidden = !materials.length && !userSubjects.length;
  if (panel.hidden) return;

  const heading = document.createElement("div");
  heading.className = "home-subjects-heading";
  const title = document.createElement("h2");
  title.id = "home-subjects-title";
  title.textContent = "My Subjects";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "text-button home-subjects-add";
  add.textContent = "+ New subject";
  add.addEventListener("click", async () => {
    const values = await subjectDialog({ title: "New subject", confirmLabel: "Create" });
    if (!values) return;
    try {
      const subject = await createSubject(values.name, values.color);
      homeSubjectFilter = subject.subject_id;
      renderHomeSubjects();
      renderHomeDocumentCards();
      showToast("Subject created");
    } catch (error) {
      showToast(error.message || "Subject could not be created");
    }
  });
  heading.append(title, add);
  panel.appendChild(heading);

  if (!userSubjects.length) {
    const hint = document.createElement("p");
    hint.className = "home-subjects-hint";
    hint.textContent = "Group your documents by course, e.g. Biology or Physics.";
    panel.appendChild(hint);
    return;
  }
  const known = new Set(materials.map((material) => material.document_id));
  const unassigned = materials.filter((material) => !subjectOfDocument(material.document_id)).length;
  const grid = document.createElement("div");
  grid.className = "subjects-grid";
  grid.setAttribute("role", "group");
  grid.setAttribute("aria-label", "Filter materials by subject");
  grid.appendChild(homeSubjectCard({ key: SUBJECT_ALL, name: "All materials", count: materials.length }));
  userSubjects.forEach((subject) => grid.appendChild(homeSubjectCard({
    key: subject.subject_id, name: subject.name, color: subject.color, subject,
    count: (subject.document_ids || []).filter((id) => known.has(id)).length,
  })));
  grid.appendChild(homeSubjectCard({ key: SUBJECT_UNASSIGNED, name: "Unassigned", count: unassigned }));
  panel.appendChild(grid);
  const progress = document.createElement("section");
  progress.className = "subject-progress";
  progress.id = "home-subject-progress";
  progress.setAttribute("aria-labelledby", "home-subject-progress-title");
  panel.appendChild(progress);
  renderHomeSubjectProgress();
}

// ---- Home: study progress per subject ---------------------------------------------------------------
// Derived only from each document's saved Planner sessions, as /api/progress/documents/{id} already
// reports them (homeDocStates: the document's active plan, counted by backend/study_progress.py):
// one saved session row = one session, a combined session included; superseded ("rescheduled") and
// withdrawn ("cancelled") rows, drafts and preview suggestions are never counted. A subject's figure
// is the sum of its documents'; nothing here touches Summary, Flashcards, Quiz or scheduling.

const homeExpandedSubjectProgress = new Set();   // subject keys whose per-PDF rows are shown

function subjectProgressBar(completed, planned, label) {
  const bar = document.createElement("div");
  bar.className = "subject-progress-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", label);
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(planned));
  bar.setAttribute("aria-valuenow", String(completed));
  const fill = document.createElement("span");
  fill.style.width = `${planned ? Math.round((100 * completed) / planned) : 0}%`;
  bar.appendChild(fill);
  return bar;
}

function subjectSessionText(completed, planned) {
  return planned ? `${completed} of ${planned} session${planned === 1 ? "" : "s"} done` : "Not in your study plan";
}

// [{key, subject, name, completed, planned, unavailable, documents: [{document_id, name, completed, planned, available}]}]:
// every subject with documents, plus Unassigned when its documents have planned sessions (or could
// not be read). A document whose progress request failed is "unavailable" -- never counted as 0.
function homeSubjectProgressGroups(materials) {
  return groupDocumentsBySubject(materials, (material) => material.document_id).map((group) => {
    const documents = group.documents.map((material) => {
      const available = homeDocStates.has(material.document_id);
      const plan = homeDocStates.get(material.document_id)?.plan;
      return { document_id: material.document_id, name: material.document_name || material.document_id, available,
        completed: plan?.completed_sessions || 0, planned: plan?.planned_sessions || 0 };
    });
    const sum = (key) => documents.reduce((total, doc) => total + doc[key], 0);
    const unavailable = documents.filter((doc) => !doc.available).length;
    return { ...group, documents, unavailable, completed: sum("completed"), planned: sum("planned") };
  }).filter((group) => group.subject || group.planned > 0 || group.unavailable > 0);
}

// "x of y sessions done", marked when some of the documents could not be read.
function subjectGroupProgressText(group) {
  if (group.unavailable === group.documents.length) return "Progress unavailable";
  const text = subjectSessionText(group.completed, group.planned);
  return group.unavailable ? `${text} · incomplete` : text;
}

function renderHomeSubjectProgress() {
  const section = document.getElementById("home-subject-progress");
  if (!section) return;
  section.innerHTML = "";
  const groups = homeDocStatesReady ? homeSubjectProgressGroups(dashboardData?.materials || []) : [];
  section.hidden = !groups.length;
  if (section.hidden) return;
  const title = document.createElement("h3");
  title.id = "home-subject-progress-title";
  title.className = "subject-progress-title";
  title.textContent = "Current plan progress by subject";
  const hint = document.createElement("p");
  hint.className = "subject-progress-hint";
  hint.textContent = "Sessions saved in your active study plan, not your lifetime study history.";
  const failed = groups.reduce((total, group) => total + group.unavailable, 0);
  let warning = null;
  if (failed) {
    warning = document.createElement("p");
    warning.className = "subject-progress-warning";
    warning.setAttribute("role", "status");
    warning.textContent = `Progress for ${failed} document${failed === 1 ? "" : "s"} could not be loaded, so the totals marked "incomplete" leave ${failed === 1 ? "it" : "them"} out. `;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "text-button subject-progress-retry";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => { retry.disabled = true; refreshHomeDocumentStates(); });
    warning.appendChild(retry);
  }
  const list = document.createElement("ul");
  list.className = "subject-progress-list";
  groups.forEach((group) => {
    const expanded = homeExpandedSubjectProgress.has(group.key);
    const item = document.createElement("li");
    item.className = "subject-progress-item";
    item.dataset.subjectProgress = group.key;
    if (group.subject) item.style.setProperty("--subject-color", group.subject.color);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "subject-progress-toggle";
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-controls", `subject-progress-docs-${group.key}`);
    const chevron = document.createElement("span");
    chevron.className = "planner-subject-group-chevron";
    chevron.setAttribute("aria-hidden", "true");
    const dot = document.createElement("span");
    dot.className = "subject-card-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "subject-progress-name";
    name.textContent = group.name;
    const count = document.createElement("span");
    count.className = "subject-progress-count";
    count.textContent = subjectGroupProgressText(group);
    item.classList.toggle("is-incomplete", group.unavailable > 0);
    toggle.append(chevron, dot, name);
    toggle.addEventListener("click", () => {
      if (expanded) homeExpandedSubjectProgress.delete(group.key);
      else homeExpandedSubjectProgress.add(group.key);
      renderHomeSubjectProgress();
    });
    const head = document.createElement("div");
    head.className = "subject-progress-head";
    head.append(toggle, subjectProgressBar(group.completed, group.planned, `${group.name}: ${count.textContent}`), count);
    const docs = document.createElement("ul");
    docs.className = "subject-progress-docs";
    docs.id = `subject-progress-docs-${group.key}`;
    docs.hidden = !expanded;
    group.documents.forEach((doc) => {
      const row = document.createElement("li");
      row.className = "subject-progress-doc";
      row.dataset.documentId = doc.document_id;
      const docName = document.createElement("span");
      docName.className = "subject-progress-doc-name";
      docName.textContent = doc.name;
      const docCount = document.createElement("span");
      docCount.className = "subject-progress-count";
      docCount.textContent = doc.available ? subjectSessionText(doc.completed, doc.planned) : "Progress unavailable";
      row.classList.toggle("is-unavailable", !doc.available);
      row.append(docName, subjectProgressBar(doc.completed, doc.planned, `${doc.name}: ${docCount.textContent}`), docCount);
      docs.appendChild(row);
    });
    item.append(head, docs);
    list.appendChild(item);
  });
  section.append(title, hint);
  if (warning) section.appendChild(warning);
  section.appendChild(list);
}

function homeMaterialsHeading() {
  if (homeSubjectFilter === SUBJECT_UNASSIGNED) return "Unassigned";
  return subjectById(homeSubjectFilter)?.name || "Your materials";
}

// The bulk bar: with documents ticked, move them all to a subject (or Unassigned) at once.
function renderHomeBulkBar() {
  const bar = document.getElementById("home-bulk-bar");
  if (!bar) return;
  bar.innerHTML = "";
  bar.hidden = homeSelectedDocuments.size === 0;
  if (bar.hidden) return;
  const count = document.createElement("span");
  count.className = "home-bulk-count";
  count.textContent = `${homeSelectedDocuments.size} selected`;
  const select = subjectSelect({ label: "Move selected documents to", noneLabel: "Unassigned" });
  select.value = "";
  const move = document.createElement("button");
  move.type = "button";
  move.className = "primary-button home-bulk-move";
  move.textContent = "Move";
  move.addEventListener("click", async () => {
    const subjectId = await resolveSubjectChoice(select, "");
    if (subjectId === undefined) return;
    move.disabled = true;
    if (await moveDocumentsToSubject([...homeSelectedDocuments], subjectId)) homeSelectedDocuments.clear();
    renderHomeDocumentCards();
  });
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "text-button home-bulk-clear";
  clear.textContent = "Clear";
  clear.addEventListener("click", () => { homeSelectedDocuments.clear(); renderHomeDocumentCards(); });
  const label = document.createElement("span");
  label.className = "home-bulk-label";
  label.textContent = "Move to";
  bar.append(count, label, select, move, clear);
}

// Per document row: tick box (bulk move) and its subject select (assign / move / unassign).
function homeDocumentSubjectControls(documentId, title) {
  const pick = document.createElement("input");
  pick.type = "checkbox";
  pick.className = "home-doc-select";
  pick.checked = homeSelectedDocuments.has(documentId);
  pick.setAttribute("aria-label", `Select ${title}`);
  pick.addEventListener("change", () => {
    if (pick.checked) homeSelectedDocuments.add(documentId);
    else homeSelectedDocuments.delete(documentId);
    renderHomeBulkBar();
  });
  const current = subjectOfDocument(documentId);
  const select = subjectSelect({ value: current?.subject_id || "", label: `Subject of ${title}` });
  select.classList.add("home-doc-subject");
  if (current) select.style.setProperty("--subject-color", current.color);
  select.addEventListener("change", async () => {
    const subjectId = await resolveSubjectChoice(select, current?.subject_id || "");
    if (subjectId === undefined || subjectId === (current?.subject_id || null)) return;
    select.disabled = true;
    if (!(await moveDocumentsToSubject([documentId], subjectId))) renderHomeDocumentCards();
  });
  return { pick, select };
}

// ---- Planner: display-only subject filter ---------------------------------------------------------

function plannerSubjectMatches(documentId) {
  return subjectFilterMatches(plannerSubjectFilter, documentId);
}

function plannerSubjectFilterSelect(onChange) {
  const select = subjectSelect({ value: subjectById(plannerSubjectFilter) ? plannerSubjectFilter : "", label: "Show subject", includeNew: false, noneLabel: "All subjects" });
  const unassigned = document.createElement("option");
  unassigned.value = SUBJECT_UNASSIGNED;
  unassigned.textContent = "Unassigned";
  select.appendChild(unassigned);
  if (plannerSubjectFilter === SUBJECT_UNASSIGNED) select.value = SUBJECT_UNASSIGNED;
  select.classList.add("planner-subject-select");
  select.addEventListener("change", () => {
    plannerSubjectFilter = select.value || SUBJECT_ALL;
    onChange();
  });
  return select;
}

function subjectFilterCaption() {
  const caption = document.createElement("span");
  caption.className = "planner-subject-caption";
  caption.textContent = "Subject";
  return caption;
}

// The phone step flow's filter (Materials and Your plan steps): caption + select, hidden without subjects.
function plannerRenderStepSubjectFilter(holderId, onChange) {
  const holder = document.getElementById(holderId);
  if (!holder) return;
  holder.innerHTML = "";
  holder.hidden = !userSubjects.length;
  if (!holder.hidden) holder.append(subjectFilterCaption(), plannerSubjectFilterSelect(onChange));
}

// Desktop calendar: the Subject select, and -- while it narrows the view -- how many of the week's
// sessions are shown plus a one-click way back to all subjects.
function pcalRenderSubjectFilter() {
  const holder = document.getElementById("pcal-subject-filter");
  if (!holder) return;
  holder.innerHTML = "";
  holder.hidden = !userSubjects.length;
  if (holder.hidden) return;
  const label = document.createElement("label");
  label.className = "pcal-subject-filter-label";
  label.append(subjectFilterCaption(), plannerSubjectFilterSelect(() => renderPlannerWorkspace()));
  holder.appendChild(label);
  if (!pcalSubjectFilterOn()) return;
  const dates = pcalWeekDates();
  const week = pcalWeekItems(plannerDateKey(dates[0]), plannerDateKey(dates[6]));
  const shown = week.filter(pcalShownBySubject).length;
  holder.appendChild(Object.assign(document.createElement("span"), {
    className: "pcal-subject-filter-note", textContent: `Showing ${shown} of ${week.length} session${week.length === 1 ? "" : "s"} this week`,
  }));
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "text-button pcal-subject-filter-reset";
  reset.textContent = "Show all subjects";
  reset.addEventListener("click", () => { plannerSubjectFilter = SUBJECT_ALL; renderPlannerWorkspace(); });
  holder.appendChild(reset);
}

// The desktop filter hides other subjects' sessions; it pauses while a schedule change is under review
// (the review bar replaces the filter's line), so every proposed change stays visible.
function pcalSubjectFilterOn() {
  return plannerSubjectFilter !== SUBJECT_ALL && !pcalReviewActive();
}

function pcalShownBySubject(item) {
  return !pcalSubjectFilterOn() || plannerSubjectMatches(item.session?.document_id ?? item.document_id);
}

// The subject shown on a session or queue item: its subject, or "Unassigned" once the user has
// subjects at all (nothing is added for a user without subjects). Read at render time, so a moved
// document or a deleted subject shows up on the next render.
function plannerSubjectTag(documentId, className) {
  if (!userSubjects.length || !documentId) return null;
  return subjectBadge(subjectOfDocument(documentId), className);
}

// A calendar block / agenda card / material of another subject stays in place, only muted, so
// nothing about the schedule (times, drag targets, conflicts) changes with the filter.
function plannerMarkSubject(element, documentId) {
  if (!element || !documentId) return element;
  const subject = subjectOfDocument(documentId);
  if (subject) {
    element.style.setProperty("--subject-color", subject.color);
    element.dataset.subjectId = subject.subject_id;
  }
  element.classList.toggle("is-subject-muted", !plannerSubjectMatches(documentId));
  return element;
}

// ---- Planner: documents grouped by subject ----------------------------------------------------------
// The Materials step and the Add materials sheet list documents under their subject (Unassigned last).
// Grouping is presentation only: a plan material is still one document, chosen or removed one at a time.

const plannerCollapsedSubjectGroups = new Set();   // group keys the user collapsed (UI state only)

// [{key, subject, name, documents}] in subject order, Unassigned last; empty groups are left out.
function groupDocumentsBySubject(documents, idOf) {
  const groups = userSubjects.map((subject) => ({ key: subject.subject_id, subject, name: subject.name, documents: [] }));
  const unassigned = { key: SUBJECT_UNASSIGNED, subject: null, name: "Unassigned", documents: [] };
  const byKey = new Map(groups.map((group) => [group.key, group]));
  documents.forEach((doc) => (byKey.get(subjectOfDocument(idOf(doc))?.subject_id) || unassigned).documents.push(doc));
  return [...groups, unassigned].filter((group) => group.documents.length);
}

// One collapsible subject group: a header (expand/collapse, name, count, optional select-all box) and
// its rows. `selectAll` = {checked, indeterminate, label, onChange(checkbox)} or null.
function plannerSubjectGroup(group, { idPrefix, countText, rows, tag = "section", selectAll = null, action = null, onToggle }) {
  const collapsed = plannerCollapsedSubjectGroups.has(group.key);
  const section = document.createElement(tag);
  section.className = "planner-subject-group";
  section.dataset.subjectGroup = group.key;
  section.classList.toggle("is-collapsed", collapsed);
  if (group.subject) section.style.setProperty("--subject-color", group.subject.color);
  const head = document.createElement("div");
  head.className = "planner-subject-group-head";
  const bodyId = `${idPrefix}-${group.key}`;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "planner-subject-group-toggle";
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-controls", bodyId);
  const chevron = document.createElement("span");
  chevron.className = "planner-subject-group-chevron";
  chevron.setAttribute("aria-hidden", "true");
  const dot = document.createElement("span");
  dot.className = "subject-card-dot";
  dot.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "planner-subject-group-name";
  name.textContent = group.name;
  const count = document.createElement("span");
  count.className = "planner-subject-group-count";
  count.textContent = countText;
  toggle.append(chevron, dot, name, count);
  toggle.addEventListener("click", () => {
    if (collapsed) plannerCollapsedSubjectGroups.delete(group.key);
    else plannerCollapsedSubjectGroups.add(group.key);
    onToggle();
  });
  head.appendChild(toggle);
  if (selectAll) {
    const label = document.createElement("label");
    label.className = "planner-subject-group-all";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selectAll.checked;
    box.indeterminate = selectAll.indeterminate;
    box.setAttribute("aria-label", selectAll.label);
    box.addEventListener("change", () => selectAll.onChange(box));
    const text = document.createElement("span");
    text.textContent = "Select all";
    label.append(box, text);
    head.appendChild(label);
  }
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pcal-button pcal-secondary planner-subject-group-action";
    button.textContent = action.label;
    button.setAttribute("aria-label", action.ariaLabel);
    button.addEventListener("click", () => action.onClick(button));
    head.appendChild(button);
  }
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pcal-button pcal-secondary planner-subject-group-action";
    button.textContent = action.label;
    button.setAttribute("aria-label", action.ariaLabel);
    button.addEventListener("click", () => action.onClick(button));
    head.appendChild(button);
  }
  const body = document.createElement(tag === "li" ? "ul" : "div");
  body.className = "planner-subject-group-body";
  body.id = bodyId;
  body.hidden = collapsed;
  rows.forEach((row) => body.appendChild(row));
  section.append(head, body);
  return section;
}
