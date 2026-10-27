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

function pcalRenderSubjectFilter() {
  const holder = document.getElementById("pcal-subject-filter");
  if (!holder) return;
  holder.innerHTML = "";
  holder.hidden = !userSubjects.length;
  if (holder.hidden) return;
  holder.append(subjectFilterCaption(), plannerSubjectFilterSelect(() => renderPlannerWorkspace()));
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
