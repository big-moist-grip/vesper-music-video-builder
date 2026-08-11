import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";

const NODE_CLASS = "MusicVideoBuilder";
const STYLESHEET_ID = "music-video-builder-styles";
const PROJECTS_PATH = "/music-video-builder/projects";
const AUTOSAVE_DELAY_MS = 700;

const NODE_COLORS = {
    title: "#73572d",
    body: "#29231a",
};

let overlay = null;
let healthController = null;
let builderState = null;

function ensureStylesheet() {
    if (document.getElementById(STYLESHEET_ID)) {
        return;
    }

    const link = document.createElement("link");
    link.id = STYLESHEET_ID;
    link.rel = "stylesheet";
    link.href = new URL("./builder.css", import.meta.url).href;
    document.head.append(link);
}

function isActive(root) {
    return root === overlay && root.isConnected && builderState?.root === root;
}

function updateStatus(root, text, state) {
    if (!isActive(root)) {
        return;
    }

    const status = root.querySelector("[data-mvb-status]");
    status.textContent = text;
    status.dataset.state = state;
}

function hasValidHealthPayload(payload) {
    return payload !== null
        && typeof payload === "object"
        && payload.ok === true
        && payload.service === "music-video-builder"
        && payload.phase === 0;
}

function hasProjectDocument(payload) {
    return payload !== null
        && typeof payload === "object"
        && typeof payload.schema_version === "number"
        && typeof payload.project_id === "string"
        && typeof payload.name === "string"
        && typeof payload.created_at === "string"
        && typeof payload.updated_at === "string";
}

async function fetchJson(path, options = {}) {
    const response = await api.fetchApi(path, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });

    let payload = null;
    try {
        payload = await response.json();
    } catch (error) {
        throw new Error(`Request returned invalid JSON (HTTP ${response.status}).`, { cause: error });
    }

    if (!response.ok) {
        const message = payload && typeof payload.error === "string"
            ? payload.error
            : `Request failed (HTTP ${response.status}).`;
        throw new Error(message);
    }

    return payload;
}

async function closeBuilder(root) {
    if (root !== overlay || !isActive(root)) {
        root.remove();
        return;
    }
    if (builderState.closing || builderState.transitioning) {
        return;
    }

    builderState.closing = true;
    const saved = await flushCurrentProject(root);
    if (!isActive(root)) {
        return;
    }
    if (!saved) {
        builderState.closing = false;
        renderProjectState(root);
        return;
    }

    cancelAutosave(root);
    healthController?.abort();
    healthController = null;
    builderState = null;
    overlay = null;
    root.remove();
}

function saveStateLabel(state) {
    switch (state.saveState) {
        case "saved":
            return "Saved";
        case "dirty":
            return "Unsaved changes";
        case "saving":
            return "Saving…";
        case "error":
            return state.saveMessage || "Save failed — retry Save";
        default:
            return "No project open";
    }
}

function setCurrentProject(root, project) {
    if (!isActive(root)) {
        return;
    }

    cancelAutosave(root);
    builderState.currentProject = project;
    builderState.editRevision = 0;
    builderState.saveState = project ? "saved" : "empty";
    builderState.saveMessage = "";
}

function cancelAutosave(root) {
    if (!isActive(root) || builderState.autosaveTimer === null) {
        return;
    }

    window.clearTimeout(builderState.autosaveTimer);
    builderState.autosaveTimer = null;
}

function hasPendingProjectSave(state) {
    return Boolean(state.currentProject)
        && (state.saving
            || state.autosaveTimer !== null
            || state.saveState === "dirty"
            || state.saveState === "error");
}

function renderProjectState(root) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    const currentProject = state.currentProject;
    const editor = root.querySelector("[data-mvb-project-editor]");
    const emptyState = root.querySelector("[data-mvb-project-empty]");
    const nameInput = root.querySelector("[data-mvb-project-name]");
    const saveButton = root.querySelector("[data-mvb-save]");
    const projectTitle = root.querySelector("[data-mvb-current-project]");
    const saveState = root.querySelector("[data-mvb-save-state]");

    if (!currentProject) {
        editor.hidden = true;
        emptyState.hidden = false;
        nameInput.value = "";
        nameInput.disabled = true;
        saveButton.disabled = true;
        projectTitle.textContent = "No project open";
    } else {
        editor.hidden = false;
        emptyState.hidden = true;
        if (nameInput.value !== currentProject.name) {
            nameInput.value = currentProject.name;
        }
        nameInput.disabled = false;
        saveButton.disabled = state.saving || state.closing || state.transitioning;
        projectTitle.textContent = currentProject.name || "Unnamed project";
    }

    saveState.textContent = saveStateLabel(state);
    saveState.dataset.state = state.saveState;
}

function updateProjectListSummary(root) {
    if (!isActive(root)) {
        return;
    }

    const summary = root.querySelector("[data-mvb-list-summary]");
    const state = builderState;
    if (state.projectListState === "loading") {
        summary.textContent = "Loading saved projects…";
        summary.dataset.state = "loading";
        return;
    }
    if (state.projectListState === "error") {
        summary.textContent = state.projectListMessage;
        summary.dataset.state = "error";
        return;
    }

    const count = state.projects.length;
    const projectLabel = count === 1 ? "project" : "projects";
    const invalidSuffix = state.invalidProjects.length > 0
        ? ` · ${state.invalidProjects.length} invalid entry`
        : "";
    summary.textContent = count > 0
        ? `${count} saved ${projectLabel}${invalidSuffix}`
        : state.invalidProjects.length > 0
            ? `${state.invalidProjects.length} invalid project entry`
            : "No saved projects yet";
    summary.dataset.state = state.invalidProjects.length > 0 ? "warning" : "ready";
}

function formatUpdatedAt(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
        return "Updated time unavailable";
    }
    return `Updated ${new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
    }).format(date)}`;
}

function renderOpenProjectList(root) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    const list = root.querySelector("[data-mvb-project-list]");
    const listStatus = root.querySelector("[data-mvb-project-list-status]");
    const openButtons = root.querySelectorAll("[data-mvb-open-project]");
    list.replaceChildren();

    if (state.projectListState === "loading") {
        listStatus.textContent = "Loading saved projects…";
        listStatus.dataset.state = "loading";
    } else if (state.projectListState === "error") {
        listStatus.textContent = state.projectListMessage;
        listStatus.dataset.state = "error";
    } else if (state.projects.length === 0) {
        listStatus.textContent = state.invalidProjects.length > 0
            ? "No valid projects are available to open."
            : "No projects yet. Create a new project to begin.";
        listStatus.dataset.state = state.invalidProjects.length > 0 ? "error" : "empty";
    } else {
        listStatus.textContent = "Select a project to open it.";
        listStatus.dataset.state = "ready";
    }

    for (const project of state.projects) {
        const item = document.createElement("li");
        item.className = "mvb-project-list-item";

        const button = document.createElement("button");
        button.type = "button";
        button.className = "mvb-project-choice";
        button.dataset.mvbOpenProject = "";
        button.dataset.projectId = project.project_id;
        button.disabled = state.openingProject;

        const name = document.createElement("span");
        name.className = "mvb-project-choice-name";
        name.textContent = project.name;

        const updated = document.createElement("span");
        updated.className = "mvb-project-choice-meta";
        updated.textContent = formatUpdatedAt(project.updated_at);

        button.append(name, updated);
        button.addEventListener("click", () => void openProject(root, project.project_id));
        item.append(button);
        list.append(item);
    }

    if (state.invalidProjects.length > 0) {
        const invalidNotice = document.createElement("li");
        invalidNotice.className = "mvb-invalid-projects";
        invalidNotice.textContent = `${state.invalidProjects.length} project ${state.invalidProjects.length === 1 ? "entry" : "entries"} could not be read.`;
        list.append(invalidNotice);
    }

    for (const button of openButtons) {
        button.disabled = state.projectListState === "loading" || state.openingProject;
    }
}

async function loadProjectList(root) {
    if (!isActive(root)) {
        return;
    }

    builderState.projectListState = "loading";
    builderState.projectListMessage = "";
    updateProjectListSummary(root);
    renderOpenProjectList(root);

    try {
        const payload = await fetchJson(PROJECTS_PATH, { method: "GET", cache: "no-store" });
        if (!isActive(root)) {
            return;
        }
        if (!payload || !Array.isArray(payload.projects) || !Array.isArray(payload.invalid_projects)) {
            throw new Error("Project list response was invalid.");
        }
        builderState.projects = payload.projects;
        builderState.invalidProjects = payload.invalid_projects;
        builderState.projectListState = "ready";
        builderState.projectListMessage = "";
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Project list request failed.", error);
        builderState.projectListState = "error";
        builderState.projectListMessage = "Projects could not be loaded.";
    }

    updateProjectListSummary(root);
    renderOpenProjectList(root);
}

function closeProjectDialogs(root) {
    root.querySelector("[data-mvb-new-dialog]").hidden = true;
    root.querySelector("[data-mvb-open-dialog]").hidden = true;
}

function showNewProjectError(root, message) {
    const error = root.querySelector("[data-mvb-new-error]");
    error.textContent = message;
    error.hidden = false;
}

async function openNewProjectDialog(root) {
    if (!isActive(root) || builderState.transitioning || builderState.closing || builderState.newProjectBusy) {
        return;
    }

    builderState.transitioning = true;
    try {
        if (!await flushCurrentProject(root)) {
            return;
        }
        if (!isActive(root)) {
            return;
        }

        closeProjectDialogs(root);
        const dialog = root.querySelector("[data-mvb-new-dialog]");
        const input = root.querySelector("[data-mvb-new-name]");
        const error = root.querySelector("[data-mvb-new-error]");
        dialog.hidden = false;
        input.value = "";
        error.hidden = true;
        window.requestAnimationFrame(() => input.focus());
    } finally {
        if (isActive(root)) {
            builderState.transitioning = false;
            renderProjectState(root);
        }
    }
}

async function createProject(root) {
    if (!isActive(root) || builderState.newProjectBusy || builderState.transitioning || builderState.closing) {
        return;
    }

    const input = root.querySelector("[data-mvb-new-name]");
    const name = input.value.trim();
    if (!name) {
        showNewProjectError(root, "Enter a project name.");
        input.focus();
        return;
    }

    const form = root.querySelector("[data-mvb-new-form]");
    const submit = form.querySelector("[type=submit]");
    const cancel = form.querySelector("[data-mvb-cancel-new]");
    builderState.newProjectBusy = true;
    builderState.transitioning = true;
    submit.disabled = true;
    cancel.disabled = true;
    root.querySelector("[data-mvb-new-error]").hidden = true;

    try {
        if (!await flushCurrentProject(root)) {
            return;
        }
        const project = await fetchJson(PROJECTS_PATH, {
            method: "POST",
            body: JSON.stringify({ name }),
        });
        if (!isActive(root)) {
            return;
        }
        if (!hasProjectDocument(project)) {
            throw new Error("Create project response was invalid.");
        }
        setCurrentProject(root, project);
        closeProjectDialogs(root);
        renderProjectState(root);
        void loadProjectList(root);
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Project creation failed.", error);
        showNewProjectError(root, error instanceof Error ? error.message : "Project could not be created.");
    } finally {
        if (isActive(root)) {
            builderState.newProjectBusy = false;
            builderState.transitioning = false;
            submit.disabled = false;
            cancel.disabled = false;
            renderProjectState(root);
        }
    }
}

async function openProjectDialog(root) {
    if (!isActive(root) || builderState.transitioning || builderState.closing || builderState.newProjectBusy) {
        return;
    }

    builderState.transitioning = true;
    try {
        if (!await flushCurrentProject(root)) {
            return;
        }
        if (!isActive(root)) {
            return;
        }

        closeProjectDialogs(root);
        root.querySelector("[data-mvb-open-dialog]").hidden = false;
        renderOpenProjectList(root);
        void loadProjectList(root);
    } finally {
        if (isActive(root)) {
            builderState.transitioning = false;
            renderProjectState(root);
            renderOpenProjectList(root);
        }
    }
}

async function openProject(root, projectId) {
    if (!isActive(root) || builderState.openingProject || builderState.transitioning || builderState.closing) {
        return;
    }

    builderState.openingProject = true;
    builderState.transitioning = true;
    renderOpenProjectList(root);
    try {
        if (!await flushCurrentProject(root)) {
            closeProjectDialogs(root);
            return;
        }
        if (!isActive(root)) {
            return;
        }
        const project = await fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}`, {
            method: "GET",
            cache: "no-store",
        });
        if (!isActive(root)) {
            return;
        }
        if (!hasProjectDocument(project)) {
            throw new Error("Open project response was invalid.");
        }
        setCurrentProject(root, project);
        closeProjectDialogs(root);
        renderProjectState(root);
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Project open failed.", error);
        const listStatus = root.querySelector("[data-mvb-project-list-status]");
        listStatus.textContent = error instanceof Error ? error.message : "Project could not be opened.";
        listStatus.dataset.state = "error";
    } finally {
        if (isActive(root)) {
            builderState.openingProject = false;
            builderState.transitioning = false;
            renderOpenProjectList(root);
            renderProjectState(root);
        }
    }
}

function scheduleAutosave(root) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }

    cancelAutosave(root);
    const projectId = builderState.currentProject.project_id;
    const editRevision = builderState.editRevision;
    const document = { ...builderState.currentProject };
    builderState.autosaveTimer = window.setTimeout(() => {
        builderState.autosaveTimer = null;
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        if (builderState.editRevision !== editRevision) {
            scheduleAutosave(root);
            return;
        }
        void saveCurrentProject(root, true, { projectId, editRevision, document });
    }, AUTOSAVE_DELAY_MS);
}

function saveCurrentProject(root, autosave = false, requestedSave = null) {
    if (!isActive(root) || !builderState.currentProject) {
        return Promise.resolve(false);
    }
    if (builderState.saving) {
        return builderState.savePromise || Promise.resolve(false);
    }

    const currentProject = builderState.currentProject;
    const projectId = requestedSave?.projectId ?? currentProject.project_id;
    const editRevision = requestedSave?.editRevision ?? builderState.editRevision;
    if (currentProject.project_id !== projectId || builderState.editRevision !== editRevision) {
        return Promise.resolve(false);
    }

    cancelAutosave(root);
    const projectToSave = requestedSave?.document
        ? { ...requestedSave.document }
        : { ...currentProject };
    builderState.saving = true;
    builderState.saveState = "saving";
    builderState.saveMessage = "";
    renderProjectState(root);

    const savePromise = performSaveCurrentProject(root, autosave, {
        projectId,
        editRevision,
        projectToSave,
    });
    builderState.savePromise = savePromise;
    return savePromise;
}

async function performSaveCurrentProject(root, autosave, request) {
    try {
        const savedProject = await fetchJson(
            `${PROJECTS_PATH}/${encodeURIComponent(request.projectId)}`,
            {
                method: "PUT",
                body: JSON.stringify(request.projectToSave),
            },
        );
        if (!isActive(root) || builderState.currentProject?.project_id !== request.projectId) {
            return false;
        }
        if (!hasProjectDocument(savedProject)) {
            throw new Error("Save project response was invalid.");
        }

        if (builderState.editRevision === request.editRevision) {
            builderState.currentProject = savedProject;
            builderState.saveState = "saved";
            builderState.saveMessage = "";
            void loadProjectList(root);
        } else {
            builderState.saveState = "dirty";
            builderState.saveMessage = "";
        }
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== request.projectId) {
            return false;
        }
        console.error("[Music Video Builder] Project save failed.", error);
        builderState.saveState = "error";
        builderState.saveMessage = autosave
            ? "Autosave failed — retry Save"
            : "Save failed — retry Save";
        return false;
    } finally {
        if (!isActive(root)) {
            return;
        }

        const sameProject = builderState.currentProject?.project_id === request.projectId;
        const newerEdit = sameProject && builderState.editRevision !== request.editRevision;
        builderState.saving = false;
        builderState.savePromise = null;
        renderProjectState(root);
        if (newerEdit && builderState.saveState === "dirty") {
            scheduleAutosave(root);
        }
    }
}

async function flushCurrentProject(root) {
    if (!isActive(root) || !builderState.currentProject) {
        return true;
    }

    while (isActive(root) && builderState.currentProject) {
        const state = builderState;
        const hadPendingTimer = state.autosaveTimer !== null;
        cancelAutosave(root);
        if (!hadPendingTimer && !hasPendingProjectSave(state)) {
            return true;
        }

        const saved = await saveCurrentProject(root);
        if (!isActive(root) || !saved) {
            return false;
        }
    }

    return isActive(root);
}

async function checkBackend(root) {
    healthController?.abort();
    const controller = new AbortController();
    healthController = controller;
    updateStatus(root, "Checking…", "checking");

    let visibleFailure = "Connection failed";

    try {
        const response = await api.fetchApi("/music-video-builder/health", {
            cache: "no-store",
            signal: controller.signal,
        });

        if (!response.ok) {
            visibleFailure = `Unavailable (HTTP ${response.status})`;
            throw new Error(`Health endpoint returned HTTP ${response.status} ${response.statusText}.`);
        }

        let payload;
        try {
            payload = await response.json();
        } catch (error) {
            visibleFailure = "Invalid response";
            throw new Error("Health endpoint did not return valid JSON.", { cause: error });
        }

        if (!hasValidHealthPayload(payload)) {
            visibleFailure = "Invalid response";
            throw new Error(`Health endpoint returned an invalid payload: ${JSON.stringify(payload)}`);
        }

        updateStatus(root, "Connected", "connected");
        await loadProjectList(root);
    } catch (error) {
        if (controller.signal.aborted) {
            return;
        }

        console.error("[Music Video Builder] Backend health check failed.", error);
        updateStatus(root, visibleFailure, "error");
        if (isActive(root)) {
            builderState.projectListState = "error";
            builderState.projectListMessage = "Projects unavailable until the backend connects.";
            updateProjectListSummary(root);
        }
    } finally {
        if (healthController === controller) {
            healthController = null;
        }
    }
}

function openBuilder() {
    if (overlay?.isConnected) {
        overlay.querySelector(".mvb-close").focus();
        return;
    }

    healthController?.abort();
    healthController = null;
    overlay = null;

    const root = document.createElement("div");
    root.className = "mvb-overlay";
    root.innerHTML = `
        <section class="mvb-panel" role="dialog" aria-modal="true" aria-labelledby="mvb-title">
            <header class="mvb-header">
                <div class="mvb-brand">
                    <span class="mvb-brand-rule" aria-hidden="true"></span>
                    <div>
                        <p class="mvb-phase">Phase 1 · Project persistence</p>
                        <h1 id="mvb-title">Vesper Music Video Builder</h1>
                        <p class="mvb-subtitle">A local workspace for organised music-video projects.</p>
                    </div>
                </div>
                <button class="mvb-close" type="button">Close</button>
            </header>

            <main class="mvb-content">
                <section class="mvb-health" aria-label="Backend status">
                    <div>
                        <p class="mvb-eyebrow">Service connection</p>
                        <span class="mvb-health-label">Music Video Builder backend</span>
                    </div>
                    <span class="mvb-health-value" data-mvb-status data-state="checking" aria-live="polite">Checking…</span>
                </section>

                <section class="mvb-project-card" aria-labelledby="mvb-project-heading">
                    <div class="mvb-section-heading">
                        <div>
                            <p class="mvb-eyebrow">Workspace</p>
                            <h2 id="mvb-project-heading">Project persistence</h2>
                        </div>
                        <span class="mvb-save-state" data-mvb-save-state data-state="empty" aria-live="polite">No project open</span>
                    </div>

                    <div class="mvb-project-empty" data-mvb-project-empty>
                        <p class="mvb-empty-title">No project open</p>
                        <p>Create a new project or open an existing one to begin.</p>
                    </div>

                    <div class="mvb-project-editor" data-mvb-project-editor hidden>
                        <div class="mvb-current-project-heading">
                            <p class="mvb-eyebrow">Current project</p>
                            <h3 data-mvb-current-project>No project open</h3>
                        </div>
                        <label class="mvb-field">
                            <span>Project name</span>
                            <input data-mvb-project-name type="text" maxlength="200" autocomplete="off" disabled>
                        </label>
                        <button class="mvb-button mvb-button-primary" data-mvb-save type="button" disabled>Save</button>
                    </div>

                    <div class="mvb-project-actions">
                        <button class="mvb-button mvb-button-primary" data-mvb-new type="button">New Project</button>
                        <button class="mvb-button mvb-button-secondary" data-mvb-open type="button">Open Project</button>
                    </div>
                    <p class="mvb-list-summary" data-mvb-list-summary aria-live="polite">Loading saved projects…</p>
                </section>

                <section class="mvb-stages" aria-labelledby="mvb-stages-heading">
                    <div class="mvb-section-heading">
                        <div>
                            <p class="mvb-eyebrow">Pipeline</p>
                            <h2 id="mvb-stages-heading">Builder stages</h2>
                        </div>
                        <span class="mvb-stage-note">Stage navigation follows in later phases</span>
                    </div>
                    <ol class="mvb-stage-list" aria-label="Builder stages">
                        <li><span class="mvb-stage-number">01</span><span>Setup</span></li>
                        <li><span class="mvb-stage-number">02</span><span>Storyboard</span></li>
                        <li><span class="mvb-stage-number">03</span><span>Keyframes</span></li>
                        <li><span class="mvb-stage-number">04</span><span>Prompts</span></li>
                        <li><span class="mvb-stage-number">05</span><span>Render</span></li>
                    </ol>
                </section>
            </main>

            <div class="mvb-modal-backdrop" data-mvb-new-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-new-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">New workspace</p>
                            <h2 id="mvb-new-heading">Create project</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-cancel-new type="button">Cancel</button>
                    </div>
                    <form data-mvb-new-form>
                        <label class="mvb-field">
                            <span>Project name</span>
                            <input data-mvb-new-name type="text" maxlength="200" autocomplete="off" required>
                        </label>
                        <p class="mvb-dialog-error" data-mvb-new-error role="alert" hidden></p>
                        <div class="mvb-dialog-actions">
                            <button class="mvb-button mvb-button-secondary" data-mvb-cancel-new type="button">Cancel</button>
                            <button class="mvb-button mvb-button-primary" type="submit">Create Project</button>
                        </div>
                    </form>
                </section>
            </div>

            <div class="mvb-modal-backdrop" data-mvb-open-dialog hidden>
                <section class="mvb-dialog mvb-open-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-open-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">Saved workspaces</p>
                            <h2 id="mvb-open-heading">Open project</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-close-open type="button">Close</button>
                    </div>
                    <p class="mvb-dialog-status" data-mvb-project-list-status aria-live="polite">Loading saved projects…</p>
                    <ul class="mvb-project-list" data-mvb-project-list></ul>
                </section>
            </div>
        </section>
    `;

    const state = {
        root,
        currentProject: null,
        projects: [],
        invalidProjects: [],
        projectListState: "loading",
        projectListMessage: "",
        saveState: "empty",
        saveMessage: "",
        autosaveTimer: null,
        editRevision: 0,
        savePromise: null,
        saving: false,
        closing: false,
        transitioning: false,
        newProjectBusy: false,
        openingProject: false,
    };

    const closeButton = root.querySelector(".mvb-close");
    const newButton = root.querySelector("[data-mvb-new]");
    const openButton = root.querySelector("[data-mvb-open]");
    const saveButton = root.querySelector("[data-mvb-save]");
    const projectName = root.querySelector("[data-mvb-project-name]");
    const newForm = root.querySelector("[data-mvb-new-form]");
    const cancelNewButtons = root.querySelectorAll("[data-mvb-cancel-new]");
    const closeOpenButton = root.querySelector("[data-mvb-close-open]");

    closeButton.addEventListener("click", () => void closeBuilder(root));
    newButton.addEventListener("click", () => openNewProjectDialog(root));
    openButton.addEventListener("click", () => void openProjectDialog(root));
    saveButton.addEventListener("click", () => void saveCurrentProject(root));
    projectName.addEventListener("input", () => {
        if (!isActive(root) || !builderState.currentProject) {
            return;
        }
        if (builderState.currentProject.name === projectName.value) {
            return;
        }
        builderState.currentProject.name = projectName.value;
        builderState.editRevision += 1;
        builderState.saveState = "dirty";
        builderState.saveMessage = "";
        renderProjectState(root);
        scheduleAutosave(root);
    });
    projectName.addEventListener("change", () => {
        if (builderState?.saveState === "dirty") {
            scheduleAutosave(root);
        }
    });
    projectName.addEventListener("blur", () => {
        if (builderState?.saveState === "dirty") {
            scheduleAutosave(root);
        }
    });
    newForm.addEventListener("submit", (event) => {
        event.preventDefault();
        void createProject(root);
    });
    for (const button of cancelNewButtons) {
        button.addEventListener("click", () => closeProjectDialogs(root));
    }
    closeOpenButton.addEventListener("click", () => closeProjectDialogs(root));

    overlay = root;
    builderState = state;
    document.body.append(root);
    renderProjectState(root);
    updateProjectListSummary(root);
    closeButton.focus();
    void checkBackend(root);
}

ensureStylesheet();

app.registerExtension({
    name: "music-video-builder.extension-shell",
    nodeCreated(node) {
        if (node.comfyClass !== NODE_CLASS) {
            return;
        }

        node.color = NODE_COLORS.title;
        node.bgcolor = NODE_COLORS.body;

        const alreadyAttached = node.widgets?.some((widget) => widget.name === "Open Builder");
        if (!alreadyAttached) {
            node.addWidget("button", "Open Builder", null, openBuilder);
        }
    },
});
