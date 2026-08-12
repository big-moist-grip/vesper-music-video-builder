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
    if (!status) {
        return;
    }

    const isError = state === "error";
    status.hidden = !isError;
    status.textContent = isError ? text : "";
    status.dataset.state = state;
}

function renderMaximizeState(root) {
    if (!isActive(root)) {
        return;
    }

    const button = root.querySelector("[data-mvb-maximize]");
    const maximized = Boolean(builderState.maximized);
    root.classList.toggle("mvb-overlay-maximized", maximized);
    button.textContent = maximized ? "Restore" : "Maximise";
    button.setAttribute("aria-label", maximized ? "Restore compact builder" : "Maximise builder");
    button.setAttribute("aria-pressed", String(maximized));
}

function toggleMaximize(root) {
    if (!isActive(root)) {
        return;
    }

    builderState.maximized = !builderState.maximized;
    renderMaximizeState(root);
}

function hasValidHealthPayload(payload) {
    return payload !== null
        && typeof payload === "object"
        && payload.ok === true
        && payload.service === "music-video-builder"
        && payload.phase === 0;
}

function hasProjectDocument(payload) {
    const source = payload?.source;
    return payload !== null
        && typeof payload === "object"
        && payload.schema_version === 2
        && typeof payload.project_id === "string"
        && typeof payload.name === "string"
        && typeof payload.created_at === "string"
        && typeof payload.updated_at === "string"
        && source !== null
        && typeof source === "object"
        && Object.keys(source).length === 2
        && Object.prototype.hasOwnProperty.call(source, "master_audio")
        && Object.prototype.hasOwnProperty.call(source, "lyrics_srt")
        && Array.isArray(payload.scenes);
}

async function fetchJson(path, options = {}) {
    const isMultipart = typeof FormData !== "undefined" && options.body instanceof FormData;
    const headers = { ...(options.headers || {}) };
    if (!isMultipart && !Object.keys(headers).some((key) => key.toLowerCase() === "content-type")) {
        headers["Content-Type"] = "application/json";
    }
    const response = await api.fetchApi(path, {
        ...options,
        headers,
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
    if (builderState.closing || builderState.transitioning || builderState.operation) {
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
            return "";
        case "dirty":
            return "Unsaved";
        case "saving":
            return "Saving…";
        case "error":
            return state.saveMessage || "Save failed — retry Save";
        default:
            return "";
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
    builderState.setupState = project ? "ready" : "empty";
    builderState.setupMessage = "";
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

function formatDurationMs(durationMs) {
    const totalSeconds = Math.floor(durationMs / 1000);
    const milliseconds = durationMs % 1000;
    const seconds = totalSeconds % 60;
    const totalMinutes = Math.floor(totalSeconds / 60);
    const minutes = totalMinutes % 60;
    const hours = Math.floor(totalMinutes / 60);
    const base = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
    return hours > 0 ? `${hours}:${base}` : base;
}

function formatTimelineMs(value) {
    return formatDurationMs(value);
}

function renderSceneReview(root) {
    if (!isActive(root)) {
        return;
    }

    const scenes = builderState.currentProject?.scenes || [];
    const empty = root.querySelector("[data-mvb-scenes-empty]");
    const table = root.querySelector("[data-mvb-scene-table]");
    const rows = root.querySelector("[data-mvb-scene-rows]");
    const summary = root.querySelector("[data-mvb-scene-summary]");
    rows.replaceChildren();
    table.hidden = scenes.length === 0;
    empty.hidden = scenes.length > 0;
    summary.textContent = scenes.length > 0
        ? `${scenes.length} scene${scenes.length === 1 ? "" : "s"} · read-only timing review`
        : "No scenes built yet.";

    for (const [index, scene] of scenes.entries()) {
        const row = document.createElement("tr");
        const values = [
            String(index + 1).padStart(2, "0"),
            scene.source_kind === "lyric" ? "Lyric" : "Instrumental",
            formatTimelineMs(scene.timeline_start_ms),
            formatTimelineMs(scene.timeline_end_ms),
            formatDurationMs(scene.exact_duration_ms),
            `${scene.split_index} / ${scene.split_count}`,
            Array.isArray(scene.source_cue_numbers) && scene.source_cue_numbers.length > 0
                ? scene.source_cue_numbers.join(", ")
                : "—",
            scene.lyric || "—",
        ];
        for (const [cellIndex, value] of values.entries()) {
            const cell = document.createElement(cellIndex === 0 ? "th" : "td");
            if (cellIndex === 0) {
                cell.scope = "row";
            }
            cell.textContent = value;
            row.append(cell);
        }
        rows.append(row);
    }
}

function renderSetupState(root) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    const project = state.currentProject;
    const operationActive = Boolean(state.operation);
    const transitionActive = state.transitioning || state.closing;
    const controlsBlocked = !project || operationActive || transitionActive;
    const audio = project?.source?.master_audio;
    const lyrics = project?.source?.lyrics_srt;
    const audioInput = root.querySelector("[data-mvb-audio-file]");
    const audioButton = root.querySelector("[data-mvb-import-audio]");
    const audioMeta = root.querySelector("[data-mvb-audio-meta]");
    const audioStatus = root.querySelector("[data-mvb-audio-status]");
    const srtInput = root.querySelector("[data-mvb-srt-file]");
    const srtButton = root.querySelector("[data-mvb-import-srt]");
    const srtMeta = root.querySelector("[data-mvb-srt-meta]");
    const srtStatus = root.querySelector("[data-mvb-srt-status]");
    const buildButton = root.querySelector("[data-mvb-build-scenes]");
    const setupStatus = root.querySelector("[data-mvb-setup-status]");

    audioInput.disabled = controlsBlocked;
    audioButton.disabled = controlsBlocked || !audioInput.files?.length;
    srtInput.disabled = controlsBlocked;
    srtButton.disabled = controlsBlocked || !srtInput.files?.length;
    buildButton.disabled = controlsBlocked || !audio || !lyrics;
    buildButton.textContent = project?.scenes?.length ? "Rebuild Scenes" : "Build Scenes";

    audioMeta.textContent = audio
        ? `${audio.original_name} · ${formatDurationMs(audio.duration_ms)}`
        : "No master audio imported.";
    srtMeta.textContent = lyrics
        ? `${lyrics.original_name} · ${lyrics.cue_count} cue${lyrics.cue_count === 1 ? "" : "s"}`
        : "No lyrics SRT imported.";

    const operationLabel = state.operation === "audio"
        ? "Importing master audio…"
        : state.operation === "srt"
            ? "Importing lyrics SRT…"
            : state.operation === "scenes"
                ? "Building scenes…"
                : "";
    audioStatus.textContent = state.operation === "audio" ? operationLabel : audio ? "Imported" : "Waiting for source";
    srtStatus.textContent = state.operation === "srt" ? operationLabel : lyrics ? "Imported" : "Waiting for source";
    audioStatus.dataset.state = state.operation === "audio" ? "working" : audio ? "success" : "empty";
    srtStatus.dataset.state = state.operation === "srt" ? "working" : lyrics ? "success" : "empty";
    setupStatus.textContent = operationLabel || state.setupMessage || "";
    setupStatus.dataset.state = state.operation ? "working" : state.setupState;
    renderSceneReview(root);
}

function renderProjectState(root) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    renderMaximizeState(root);
    const currentProject = state.currentProject;
    const landingHeader = root.querySelector("[data-mvb-landing-header]");
    const projectToolbar = root.querySelector("[data-mvb-project-toolbar]");
    const landing = root.querySelector("[data-mvb-landing]");
    const setup = root.querySelector("[data-mvb-setup]");
    const sceneReview = root.querySelector("[data-mvb-scene-review]");
    const nameInput = root.querySelector("[data-mvb-project-name]");
    const saveButton = root.querySelector("[data-mvb-save]");
    const projectsButton = root.querySelector("[data-mvb-projects]");
    const newButtons = root.querySelectorAll("[data-mvb-new]");
    const openButtons = root.querySelectorAll("[data-mvb-open]");
    const saveState = root.querySelector("[data-mvb-save-state]");

    if (!currentProject) {
        root.classList.remove("mvb-project-open");
        landingHeader.hidden = false;
        projectToolbar.hidden = true;
        landing.hidden = false;
        setup.hidden = true;
        sceneReview.hidden = true;
        nameInput.value = "";
        nameInput.disabled = true;
        saveButton.disabled = true;
        projectsButton.disabled = true;
    } else {
        root.classList.add("mvb-project-open");
        landingHeader.hidden = true;
        projectToolbar.hidden = false;
        landing.hidden = true;
        setup.hidden = false;
        sceneReview.hidden = false;
        if (nameInput.value !== currentProject.name) {
            nameInput.value = currentProject.name;
        }
        nameInput.disabled = Boolean(state.operation) || state.closing || state.transitioning;
        saveButton.disabled = state.saving || state.closing || state.transitioning || Boolean(state.operation);
        projectsButton.disabled = state.closing || state.transitioning || Boolean(state.operation) || state.newProjectBusy;
    }

    for (const button of newButtons) {
        button.disabled = state.closing || state.transitioning || Boolean(state.operation) || state.newProjectBusy;
    }
    for (const button of openButtons) {
        button.disabled = state.closing || state.transitioning || Boolean(state.operation) || state.newProjectBusy;
    }
    const saveLabel = saveStateLabel(state);
    saveState.textContent = saveLabel;
    saveState.hidden = !saveLabel;
    saveState.dataset.state = state.saveState;
    renderRecentProjectList(root);
    if (currentProject) {
        renderSetupState(root);
    } else {
        renderSceneReview(root);
    }
}

function updateProjectListSummary(root) {
    if (!isActive(root)) {
        return;
    }

    const summary = root.querySelector("[data-mvb-list-summary]");
    if (!summary) {
        return;
    }
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
    const invalidCount = state.invalidProjectsDismissed ? 0 : state.invalidProjects.length;
    const projectLabel = count === 1 ? "project" : "projects";
    const invalidSuffix = invalidCount > 0
        ? " · " + invalidCount + " invalid " + (invalidCount === 1 ? "entry" : "entries")
        : "";
    summary.textContent = count > 0
        ? `${count} saved ${projectLabel}${invalidSuffix}`
        : invalidCount > 0
            ? invalidCount + " invalid project " + (invalidCount === 1 ? "entry" : "entries")
            : "No saved projects yet";
    summary.dataset.state = invalidCount > 0 ? "warning" : "ready";
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

function recentProjects(projects) {
    return [...projects]
        .sort((left, right) => {
            const leftTime = Date.parse(left.updated_at);
            const rightTime = Date.parse(right.updated_at);
            const leftValid = Number.isFinite(leftTime);
            const rightValid = Number.isFinite(rightTime);
            if (leftValid && rightValid && leftTime !== rightTime) {
                return rightTime - leftTime;
            }
            if (leftValid !== rightValid) {
                return leftValid ? -1 : 1;
            }
            const nameOrder = String(left.name).localeCompare(String(right.name));
            if (nameOrder !== 0) {
                return nameOrder;
            }
            return String(left.project_id).localeCompare(String(right.project_id));
        })
        .slice(0, 5);
}

function invalidProjectsSignature(projects) {
    return JSON.stringify(
        projects
            .map((project) => [
                typeof project?.folder_name === "string" ? project.folder_name : "",
                typeof project?.error === "string" ? project.error : "",
            ])
            .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))),
    );
}

function appendInvalidProjectNotice(root, list, location) {
    if (!isActive(root) || builderState.invalidProjects.length === 0 || builderState.invalidProjectsDismissed) {
        return;
    }

    const count = builderState.invalidProjects.length;
    const item = document.createElement("li");
    item.className = "mvb-invalid-projects";
    item.setAttribute("data-mvb-invalid-warning", "");
    item.setAttribute("role", "status");

    const message = document.createElement("p");
    message.className = "mvb-invalid-projects-message";
    message.textContent = String(count) + " project " + (count === 1 ? "entry" : "entries") + " could not be read.";

    const actions = document.createElement("div");
    actions.className = "mvb-invalid-projects-actions";

    const detailsId = "mvb-invalid-project-details-" + location;
    const detailsButton = document.createElement("button");
    detailsButton.className = "mvb-button mvb-button-secondary";
    detailsButton.type = "button";
    detailsButton.textContent = "Details";
    detailsButton.setAttribute("aria-controls", detailsId);
    detailsButton.setAttribute("aria-expanded", "false");

    const dismissButton = document.createElement("button");
    dismissButton.className = "mvb-button mvb-button-secondary";
    dismissButton.type = "button";
    dismissButton.textContent = "Dismiss";

    const details = document.createElement("div");
    details.className = "mvb-invalid-project-details";
    details.id = detailsId;
    details.hidden = true;

    for (const invalidProject of builderState.invalidProjects) {
        const entry = document.createElement("article");
        entry.className = "mvb-invalid-project-entry";

        const folderLabel = document.createElement("span");
        folderLabel.className = "mvb-invalid-project-label";
        folderLabel.textContent = "Folder";

        const folderValue = document.createElement("span");
        folderValue.className = "mvb-invalid-project-value";
        folderValue.textContent = typeof invalidProject.folder_name === "string"
            ? invalidProject.folder_name
            : "Unknown project entry";

        const problemLabel = document.createElement("span");
        problemLabel.className = "mvb-invalid-project-label";
        problemLabel.textContent = "Problem";

        const problemValue = document.createElement("span");
        problemValue.className = "mvb-invalid-project-value";
        problemValue.textContent = typeof invalidProject.error === "string"
            ? invalidProject.error
            : "Project entry could not be read.";

        entry.append(folderLabel, folderValue, problemLabel, problemValue);
        details.append(entry);
    }

    detailsButton.addEventListener("click", () => {
        const expanded = details.hidden;
        details.hidden = !expanded;
        detailsButton.textContent = expanded ? "Hide Details" : "Details";
        detailsButton.setAttribute("aria-expanded", String(expanded));
    });
    dismissButton.addEventListener("click", () => {
        if (!isActive(root)) {
            return;
        }
        builderState.invalidProjectsDismissed = true;
        renderProjectState(root);
        renderOpenProjectList(root);
    });

    actions.append(detailsButton, dismissButton);
    item.append(message, actions, details);
    list.append(item);
}

function renderRecentProjectList(root) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    const list = root.querySelector("[data-mvb-recent-project-list]");
    const listStatus = root.querySelector("[data-mvb-landing-status]");
    list.replaceChildren();

    if (state.projectListState === "loading") {
        listStatus.textContent = "Loading recent projects…";
        listStatus.dataset.state = "loading";
        return;
    }
    if (state.projectListState === "error") {
        listStatus.textContent = state.projectListMessage || "Projects could not be loaded.";
        listStatus.dataset.state = "error";
        return;
    }

    const projects = recentProjects(state.projects);
    if (projects.length === 0) {
        listStatus.textContent = state.invalidProjects.length > 0
            ? "No valid projects are available."
            : "No projects yet. Create a project to begin.";
        listStatus.dataset.state = state.invalidProjects.length > 0 ? "error" : "empty";
    } else {
        listStatus.textContent = "Open a recent project or use Open Project to see the complete list.";
        listStatus.dataset.state = "ready";
    }

    for (const project of projects) {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mvb-project-choice";
        button.dataset.projectId = project.project_id;
        button.disabled = state.openingProject || state.transitioning || state.closing;

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

    appendInvalidProjectNotice(root, list, "landing");
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

    appendInvalidProjectNotice(root, list, "open");

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
    renderRecentProjectList(root);
    renderOpenProjectList(root);

    try {
        const payload = await fetchJson(PROJECTS_PATH, { method: "GET", cache: "no-store" });
        if (!isActive(root)) {
            return;
        }
        if (!payload || !Array.isArray(payload.projects) || !Array.isArray(payload.invalid_projects)) {
            throw new Error("Project list response was invalid.");
        }
        const invalidSignature = invalidProjectsSignature(payload.invalid_projects);
        if (invalidSignature !== builderState.invalidProjectsSignature) {
            builderState.invalidProjectsDismissed = false;
            builderState.invalidProjectsSignature = invalidSignature;
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
    renderRecentProjectList(root);
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
    if (!isActive(root) || builderState.transitioning || builderState.closing || builderState.newProjectBusy || builderState.operation) {
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
    if (!isActive(root) || builderState.newProjectBusy || builderState.transitioning || builderState.closing || builderState.operation) {
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
    if (!isActive(root) || builderState.transitioning || builderState.closing || builderState.newProjectBusy || builderState.operation) {
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
    if (!isActive(root) || builderState.openingProject || builderState.transitioning || builderState.closing || builderState.operation) {
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
        builderState.projectListState = "error";
        builderState.projectListMessage = error instanceof Error ? error.message : "Project could not be opened.";
        const listStatus = root.querySelector("[data-mvb-project-list-status]");
        listStatus.textContent = builderState.projectListMessage;
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

function uploadFormData(file) {
    const formData = new FormData();
    formData.append("file", file, file.name);
    return formData;
}

async function runProjectMutation(root, operation, task, successMessage) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return false;
    }

    builderState.operation = operation;
    builderState.setupState = "working";
    builderState.setupMessage = "";
    renderProjectState(root);
    let succeeded = false;
    try {
        if (!await flushCurrentProject(root)) {
            builderState.setupState = "error";
            builderState.setupMessage = "Save the current project before continuing.";
            return false;
        }
        if (!isActive(root)) {
            return false;
        }

        const project = await task();
        if (!hasProjectDocument(project)) {
            throw new Error("Project mutation response was invalid.");
        }
        setCurrentProject(root, project);
        builderState.setupState = "success";
        builderState.setupMessage = successMessage;
        succeeded = true;
        void loadProjectList(root);
        return true;
    } catch (error) {
        if (!isActive(root)) {
            return false;
        }
        console.error(`[Music Video Builder] ${operation} operation failed.`, error);
        builderState.setupState = "error";
        builderState.setupMessage = operation === "audio"
            ? "Audio import failed."
            : operation === "srt"
                ? "SRT import failed."
                : "Scene build failed.";
        return false;
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            if (!succeeded && builderState.setupState === "working") {
                builderState.setupState = "error";
            }
            renderProjectState(root);
        }
    }
}

async function importMasterAudio(root) {
    const input = root.querySelector("[data-mvb-audio-file]");
    const file = input.files?.[0];
    if (!file) {
        builderState.setupState = "error";
        builderState.setupMessage = "Choose a master audio file first.";
        renderProjectState(root);
        return;
    }

    const imported = await runProjectMutation(
        root,
        "audio",
        () => fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(builderState.currentProject.project_id)}/source/audio`, {
            method: "POST",
            body: uploadFormData(file),
        }),
        "Master audio imported.",
    );
    if (imported && isActive(root)) {
        input.value = "";
        renderSetupState(root);
    }
}

async function importLyricsSrt(root) {
    const input = root.querySelector("[data-mvb-srt-file]");
    const file = input.files?.[0];
    if (!file) {
        builderState.setupState = "error";
        builderState.setupMessage = "Choose an SRT file first.";
        renderProjectState(root);
        return;
    }

    const imported = await runProjectMutation(
        root,
        "srt",
        () => fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(builderState.currentProject.project_id)}/source/srt`, {
            method: "POST",
            body: uploadFormData(file),
        }),
        "Lyrics SRT imported.",
    );
    if (imported && isActive(root)) {
        input.value = "";
        renderSetupState(root);
    }
}

async function buildProjectScenes(root) {
    await runProjectMutation(
        root,
        "scenes",
        () => fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(builderState.currentProject.project_id)}/scenes/build`, {
            method: "POST",
            body: JSON.stringify({}),
        }),
        "Scenes built.",
    );
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

    let visibleFailure = "Backend unavailable — project operations may not work.";

    try {
        const response = await api.fetchApi("/music-video-builder/health", {
            cache: "no-store",
            signal: controller.signal,
        });

        if (!response.ok) {
            throw new Error(`Health endpoint returned HTTP ${response.status} ${response.statusText}.`);
        }

        let payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error("Health endpoint did not return valid JSON.", { cause: error });
        }

        if (!hasValidHealthPayload(payload)) {
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
        <section class="mvb-panel" role="dialog" aria-modal="true" aria-label="Vesper Music Video Builder">
            <header class="mvb-header">
                <div class="mvb-header-main">
                    <div class="mvb-brand" data-mvb-landing-header>
                        <span class="mvb-brand-rule" aria-hidden="true"></span>
                        <div>
                            <p class="mvb-phase">Phase 2 · Audio, lyrics, and scenes</p>
                            <h1 id="mvb-title">Vesper Music Video Builder</h1>
                            <p class="mvb-subtitle">A local project space for organised music-video work.</p>
                        </div>
                    </div>
                    <div class="mvb-project-toolbar" data-mvb-project-toolbar hidden>
                        <label class="mvb-project-name-field">
                            <span class="mvb-sr-only">Project name</span>
                            <input data-mvb-project-name type="text" maxlength="200" autocomplete="off" aria-label="Project name" disabled>
                        </label>
                        <div class="mvb-project-toolbar-actions">
                            <span class="mvb-save-state" data-mvb-save-state data-state="empty" aria-live="polite" hidden></span>
                            <button class="mvb-button mvb-button-secondary" data-mvb-save type="button" disabled>Save</button>
                            <button class="mvb-button mvb-button-secondary" data-mvb-projects type="button">Projects</button>
                        </div>
                    </div>
                </div>
                <div class="mvb-header-actions">
                    <button class="mvb-maximize" data-mvb-maximize type="button" aria-pressed="false">Maximise</button>
                    <button class="mvb-close" type="button">Close</button>
                </div>
            </header>

            <main class="mvb-content">
                <div class="mvb-health-banner" data-mvb-health-banner data-mvb-status data-state="checking" role="status" aria-live="polite" hidden></div>

                <section class="mvb-landing" data-mvb-landing aria-labelledby="mvb-landing-heading" hidden>
                    <div class="mvb-landing-heading">
                        <div>
                            <p class="mvb-eyebrow">Project landing</p>
                            <h2 id="mvb-landing-heading">Recent Projects</h2>
                            <p class="mvb-landing-subtitle">Open a saved project or create a new project to begin.</p>
                        </div>
                        <div class="mvb-project-actions mvb-landing-actions">
                            <button class="mvb-button mvb-button-primary" data-mvb-new type="button">New Project</button>
                            <button class="mvb-button mvb-button-secondary" data-mvb-open type="button">Open Project</button>
                        </div>
                    </div>
                    <p class="mvb-landing-status" data-mvb-landing-status aria-live="polite">Loading recent projects…</p>
                    <ul class="mvb-project-list mvb-recent-project-list" data-mvb-recent-project-list></ul>
                </section>

                <section class="mvb-setup" data-mvb-setup aria-labelledby="mvb-setup-heading">
                    <div class="mvb-setup-heading">
                        <div>
                            <p class="mvb-eyebrow">Setup</p>
                            <h2 id="mvb-setup-heading">Sources</h2>
                        </div>
                        <span class="mvb-setup-status" data-mvb-setup-status data-state="empty" aria-live="polite"></span>
                    </div>

                    <div class="mvb-source-grid">
                        <article class="mvb-source-card">
                            <div class="mvb-source-heading">
                                <div>
                                    <p class="mvb-eyebrow">01 · Audio</p>
                                    <h3>Master Audio Track</h3>
                                </div>
                                <span class="mvb-source-status" data-mvb-audio-status data-state="empty">Waiting for source</span>
                            </div>
                            <p class="mvb-source-meta" data-mvb-audio-meta>No master audio imported.</p>
                            <label class="mvb-file-field">
                                <span>Select audio file</span>
                                <input data-mvb-audio-file type="file" accept=".wav,.mp3,.flac,.m4a,.aac,.ogg,.opus,audio/*" disabled>
                            </label>
                            <button class="mvb-button mvb-button-secondary" data-mvb-import-audio type="button" disabled>Import Master Audio</button>
                        </article>

                        <article class="mvb-source-card">
                            <div class="mvb-source-heading">
                                <div>
                                    <p class="mvb-eyebrow">02 · Lyrics</p>
                                    <h3>Lyrics SRT</h3>
                                </div>
                                <span class="mvb-source-status" data-mvb-srt-status data-state="empty">Waiting for source</span>
                            </div>
                            <p class="mvb-source-meta" data-mvb-srt-meta>No lyrics SRT imported.</p>
                            <label class="mvb-file-field">
                                <span>Select SRT file</span>
                                <input data-mvb-srt-file type="file" accept=".srt,text/plain" disabled>
                            </label>
                            <button class="mvb-button mvb-button-secondary" data-mvb-import-srt type="button" disabled>Import SRT</button>
                        </article>
                    </div>

                    <div class="mvb-scene-build">
                        <div>
                            <p class="mvb-eyebrow">03 · Scenes</p>
                            <p>Build the timing review from the imported sources.</p>
                        </div>
                        <button class="mvb-button mvb-button-primary" data-mvb-build-scenes type="button" disabled>Build Scenes</button>
                    </div>
                </section>

                <section class="mvb-scene-review" data-mvb-scene-review aria-labelledby="mvb-scene-review-heading" hidden>
                    <div class="mvb-scene-review-heading">
                        <div>
                            <p class="mvb-eyebrow">Timing Review</p>
                            <h2 id="mvb-scene-review-heading">Scenes</h2>
                        </div>
                        <span class="mvb-scene-summary" data-mvb-scene-summary>No scenes built yet.</span>
                    </div>
                    <div class="mvb-scenes-empty" data-mvb-scenes-empty>
                        <p>Build scenes after both sources are imported.</p>
                    </div>
                    <div class="mvb-scene-table-wrap" data-mvb-scene-table hidden>
                        <table class="mvb-scene-table">
                            <thead>
                                <tr>
                                    <th scope="col">#</th>
                                    <th scope="col">Type</th>
                                    <th scope="col">Start</th>
                                    <th scope="col">End</th>
                                    <th scope="col">Duration</th>
                                    <th scope="col">Split</th>
                                    <th scope="col">Cue IDs</th>
                                    <th scope="col">Lyric</th>
                                </tr>
                            </thead>
                            <tbody data-mvb-scene-rows></tbody>
                        </table>
                    </div>
                </section>
            </main>

            <div class="mvb-modal-backdrop" data-mvb-new-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-new-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">New project</p>
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
                            <p class="mvb-eyebrow">Projects</p>
                            <h2 id="mvb-open-heading">Projects</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-close-open type="button">Close</button>
                    </div>
                    <p class="mvb-dialog-status" data-mvb-project-list-status aria-live="polite">Loading saved projects…</p>
                    <ul class="mvb-project-list" data-mvb-project-list></ul>
                    <div class="mvb-dialog-actions">
                        <button class="mvb-button mvb-button-primary" data-mvb-dialog-new type="button">New Project</button>
                    </div>
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
        operation: null,
        setupState: "empty",
        setupMessage: "",
        newProjectBusy: false,
        openingProject: false,
        maximized: false,
        invalidProjectsDismissed: false,
        invalidProjectsSignature: "",
    };

    const closeButton = root.querySelector(".mvb-close");
    const maximizeButton = root.querySelector("[data-mvb-maximize]");
    const projectsButton = root.querySelector("[data-mvb-projects]");
    const newButtons = root.querySelectorAll("[data-mvb-new]");
    const openButtons = root.querySelectorAll("[data-mvb-open]");
    const saveButton = root.querySelector("[data-mvb-save]");
    const projectName = root.querySelector("[data-mvb-project-name]");
    const audioFile = root.querySelector("[data-mvb-audio-file]");
    const importAudioButton = root.querySelector("[data-mvb-import-audio]");
    const srtFile = root.querySelector("[data-mvb-srt-file]");
    const importSrtButton = root.querySelector("[data-mvb-import-srt]");
    const buildScenesButton = root.querySelector("[data-mvb-build-scenes]");
    const newForm = root.querySelector("[data-mvb-new-form]");
    const cancelNewButtons = root.querySelectorAll("[data-mvb-cancel-new]");
    const closeOpenButton = root.querySelector("[data-mvb-close-open]");

    closeButton.addEventListener("click", () => void closeBuilder(root));
    maximizeButton.addEventListener("click", () => toggleMaximize(root));
    projectsButton.addEventListener("click", () => void openProjectDialog(root));
    for (const button of newButtons) {
        button.addEventListener("click", () => void openNewProjectDialog(root));
    }
    for (const button of openButtons) {
        button.addEventListener("click", () => void openProjectDialog(root));
    }
    saveButton.addEventListener("click", () => void saveCurrentProject(root));
    audioFile.addEventListener("change", () => renderSetupState(root));
    importAudioButton.addEventListener("click", () => void importMasterAudio(root));
    srtFile.addEventListener("change", () => renderSetupState(root));
    importSrtButton.addEventListener("click", () => void importLyricsSrt(root));
    buildScenesButton.addEventListener("click", () => void buildProjectScenes(root));
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
    root.querySelector("[data-mvb-dialog-new]").addEventListener("click", () => void openNewProjectDialog(root));

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
