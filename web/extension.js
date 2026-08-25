import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";
import {
    canCopyStoryboardRequest,
    canApplyPromptRelayResponse,
    canCopyPromptRelayRequest,
    canSavePromptDraft,
    promptDraftHasCurrentRelay,
    promptDraftReadyForRender,
    promptDraftStatus,
    promptDraftIsDirty,
    promptRelayRequestJson,
    storyboardRequestJson,
} from "./prompt_state.js";
import { gptLaunchPath, isSuccessfulGptLaunch } from "./gpt_launcher.js";

const NODE_CLASS = "MusicVideoBuilder";
const STYLESHEET_ID = "music-video-builder-styles";
const PROJECTS_PATH = "/music-video-builder/projects";
const AUTOSAVE_DELAY_MS = 700;
const MAX_REF2VA_STILL_REFERENCES = 9;
const GPT_LAUNCH_FAILURE_MESSAGE = "Could not open the GPT. Open it manually or retry.";
const STORYBOARD_MODE_HELP = {
    loose: "Interpret the song freely; lyrics guide emotion and structure rather than dictating each shot.",
    strict: "Keep the visuals closely aligned to the lyrical content and sequence.",
    band_performance: "Build a performance-only video with no narrative storyline.",
};
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
    const storyDirection = payload?.story_direction;
    const storyboard = payload?.storyboard;
    const visuals = payload?.visuals;
    const prompts = payload?.prompts;
    return payload !== null
        && typeof payload === "object"
        && payload.schema_version === 7
        && typeof payload.project_id === "string"
        && typeof payload.name === "string"
        && typeof payload.created_at === "string"
        && typeof payload.updated_at === "string"
        && source !== null
        && typeof source === "object"
        && Object.keys(source).length === 2
        && Object.prototype.hasOwnProperty.call(source, "master_audio")
        && Object.prototype.hasOwnProperty.call(source, "lyrics_srt")
        && Array.isArray(payload.scenes)
        && Array.isArray(payload.characters)
        && Array.isArray(payload.locations)
        && storyDirection !== null
        && typeof storyDirection === "object"
        && Object.keys(storyDirection).length === 3
        && typeof storyDirection.storyboard_mode === "string"
        && Object.prototype.hasOwnProperty.call(STORYBOARD_MODE_HELP, storyDirection.storyboard_mode)
        && typeof storyDirection.story_brief === "string"
        && typeof storyDirection.visual_notes === "string"
        && storyboard !== null
        && typeof storyboard === "object"
        && Object.keys(storyboard).length === 2
        && (storyboard.request_fingerprint === null || typeof storyboard.request_fingerprint === "string")
        && Array.isArray(storyboard.scenes)
        && visuals !== null
        && typeof visuals === "object"
        && Object.keys(visuals).length === 1
        && Array.isArray(visuals.scenes)
        && visuals.scenes.length === payload.scenes.length
        && visuals.scenes.every((visualScene, index) => visualScene !== null
            && typeof visualScene === "object"
            && visualScene.scene_id === payload.scenes[index]?.scene_id
            && (visualScene.generation_method === "keyframe_i2v" || visualScene.generation_method === "reference2video"))
        && prompts !== null
        && typeof prompts === "object"
        && Object.keys(prompts).length === 1
        && Array.isArray(prompts.scenes)
        && prompts.scenes.length === payload.scenes.length
        && prompts.scenes.every((promptScene, index) => promptScene !== null
            && typeof promptScene === "object"
            && promptScene.scene_id === payload.scenes[index]?.scene_id
            && ["keyframe_i2v", "reference2video"].every((method) => {
                const record = promptScene[method];
                return record !== null
                    && typeof record === "object"
                    && Object.keys(record).length === 3
                    && typeof record.final_prompt === "string"
                    && (record.source_fingerprint === null || typeof record.source_fingerprint === "string")
                    && typeof record.relay_fingerprint === "string";
            }));
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

function clearGptLauncherErrors() {
    if (builderState.storyboardRelayMessage === GPT_LAUNCH_FAILURE_MESSAGE) {
        builderState.storyboardRelayMessage = "";
        builderState.storyboardRelayState = "ready";
    }
    if (builderState.promptMessage === GPT_LAUNCH_FAILURE_MESSAGE) {
        builderState.promptMessage = "";
        builderState.promptMessageState = "ready";
    }
}

async function openDedicatedGpt(root, director) {
    if (!isActive(root) || builderState.gptLaunchInFlight) {
        return;
    }
    const path = gptLaunchPath(director);
    const sourceView = director === "storyboard" ? "storyboard" : "prompts";
    const promptViewport = capturePromptViewport(root);
    clearGptLauncherErrors();
    builderState.gptLaunchInFlight = true;
    renderProjectState(root, { promptViewport });
    try {
        if (!path) {
            throw new Error(GPT_LAUNCH_FAILURE_MESSAGE);
        }
        const result = await fetchJson(path, {
            method: "POST",
            body: JSON.stringify({}),
        });
        if (!isSuccessfulGptLaunch(result, director)) {
            throw new Error(GPT_LAUNCH_FAILURE_MESSAGE);
        }
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] GPT launch failed.", error);
        if (builderState.activeView === sourceView) {
            if (sourceView === "storyboard") {
                setStoryboardRelayMessage(GPT_LAUNCH_FAILURE_MESSAGE, "error");
            } else {
                builderState.promptMessage = GPT_LAUNCH_FAILURE_MESSAGE;
                builderState.promptMessageState = "error";
            }
        }
    } finally {
        if (isActive(root)) {
            builderState.gptLaunchInFlight = false;
            renderProjectState(root, { promptViewport });
        }
    }
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
    const saved = await flushProjectTransition(root);
    if (!isActive(root)) {
        return;
    }
    if (!saved) {
        builderState.closing = false;
        renderProjectState(root);
        return;
    }

    cancelAutosave(root);
    clearRenderJobPoll();
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

function reconcileVisualDrafts(previousProject, nextProject, drafts, reconciledSceneId = null) {
    if (!previousProject || !nextProject || previousProject.project_id !== nextProject.project_id) {
        return {};
    }

    const nextSceneIds = new Set((nextProject.scenes || []).map((scene) => scene.scene_id));
    const preserved = {};
    for (const [sceneId, draft] of Object.entries(drafts || {})) {
        const visualScene = visualSceneFor(nextProject, sceneId);
        if (nextSceneIds.has(sceneId) && visualScene && sceneId !== reconciledSceneId && visualDraftIsDirty(visualScene, draft)) {
            preserved[sceneId] = draft;
        }
    }
    return preserved;
}

function reconcileVisualExpansion(previousProject, nextProject, expandedScenes) {
    if (!previousProject || !nextProject || previousProject.project_id !== nextProject.project_id) {
        return {};
    }

    const nextSceneIds = new Set((nextProject.scenes || []).map((scene) => scene.scene_id));
    return Object.fromEntries(
        Object.entries(expandedScenes || {}).filter(([sceneId]) => nextSceneIds.has(sceneId)),
    );
}

function promptDraftKey(sceneId, generationMethod) {
    return `${sceneId}:${generationMethod}`;
}

function reconcilePromptDrafts(previousProject, nextProject, drafts, reconciledPromptKey = null) {
    if (!previousProject || !nextProject || previousProject.project_id !== nextProject.project_id) {
        return {};
    }
    const nextSceneIds = new Set((nextProject.scenes || []).map((scene) => scene.scene_id));
    return Object.fromEntries(
        Object.entries(drafts || {}).filter(([key, draft]) => {
            const [sceneId, method] = key.split(":");
            return key !== reconciledPromptKey
                && nextSceneIds.has(sceneId)
                && ["keyframe_i2v", "reference2video"].includes(method)
                && Boolean(draft?.dirty);
        }),
    );
}

function setCurrentProject(root, project, options = {}) {
    if (!isActive(root)) {
        return;
    }

    clearRenderJobPoll();
    const previousProject = builderState.currentProject;
    const sameProject = previousProject?.project_id === project?.project_id;
    const preservedVisualDrafts = reconcileVisualDrafts(
        previousProject,
        project,
        builderState.visualDrafts,
        options.reconciledVisualSceneId || null,
    );
    const preservedVisualExpansion = reconcileVisualExpansion(
        previousProject,
        project,
        builderState.visualExpandedScenes,
    );
    const preservedPromptDrafts = reconcilePromptDrafts(
        previousProject,
        project,
        builderState.promptDrafts,
        options.reconciledPromptKey || null,
    );
    cancelAutosave(root);
    builderState.currentProject = project;
    if (!sameProject || !["keyframe_i2v", "reference2video"].includes(builderState.visualBulkMethod)) {
        builderState.visualBulkMethod = project?.visuals?.scenes?.[0]?.generation_method || "keyframe_i2v";
    }
    builderState.editRevision = 0;
    builderState.saveState = project ? "saved" : "empty";
    builderState.saveMessage = "";
    builderState.setupState = project ? "ready" : "empty";
    builderState.setupMessage = "";
    builderState.storyboardMessage = "";
    builderState.storyboardRequest = null;
    builderState.storyboardRequestStale = false;
    builderState.storyboardAppliedStale = sameProject
        ? builderState.storyboardAppliedStale
        : Boolean(project?.storyboard?.scenes?.length);
    builderState.storyboardResponseText = "";
    builderState.storyboardPreview = null;
    builderState.storyboardRelayMessage = "";
    builderState.storyboardRelayState = "ready";
    builderState.deleteConfirm = null;
    builderState.visualDrafts = preservedVisualDrafts;
    builderState.visualExpandedScenes = preservedVisualExpansion;
    builderState.visualRemoveTarget = null;
    builderState.visualMessage = "";
    builderState.visualMessageState = "ready";
    builderState.promptDrafts = preservedPromptDrafts;
    builderState.promptRelay = sameProject ? builderState.promptRelay : {};
    builderState.promptRelayExpanded = sameProject ? builderState.promptRelayExpanded : {};
    builderState.promptExpandedScenes = sameProject ? builderState.promptExpandedScenes : {};
    // Same-project updates keep the visible prompt cards until the caller's
    // prompt-card refresh replaces them, so no blank intermediate frame renders.
    builderState.promptCards = sameProject ? builderState.promptCards : [];
    builderState.promptListState = project
        ? (sameProject ? builderState.promptListState : "idle")
        : "empty";
    builderState.promptListMessage = "";
    builderState.promptMessage = "";
    builderState.promptMessageState = "ready";
    builderState.renderPreflight = null;
    builderState.renderRequirementsCache = null;
    builderState.renderLoadingMode = "runtime";
    builderState.renderState = project ? "idle" : "empty";
    builderState.renderMessage = "";
    builderState.renderMessageState = "ready";
    builderState.renderPreparingSceneId = null;
    builderState.renderJobs = {};
    builderState.renderJobsState = project ? "idle" : "empty";
    builderState.renderJobsMessage = "";
    builderState.renderJobsPollDelay = 1000;
    builderState.renderJobsPollTimer = null;
    builderState.renderJobsLoading = false;
    builderState.renderSubmittingSceneId = null;
    builderState.renderJobActionId = null;
    builderState.renderFinalizingJobId = null;
    builderState.renderBatch = sameProject ? builderState.renderBatch : null;
    builderState.renderBatchLoading = false;
    builderState.renderBatchBusy = false;
    builderState.renderBatchSelection = null;
    builderState.renderBatchConfirm = null;
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

function hasUnsavedPromptDrafts(state) {
    return Boolean(state.currentProject)
        && Object.values(state.promptDrafts || {}).some((draft) => Boolean(draft?.dirty));
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

function roleLabel(role) {
    return {
        performer: "Performer / Singer",
        band_member: "Band Member",
        extra: "Extra",
    }[role] || role;
}

function referenceUrl(projectId, kind, entityId, referenceId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}/references/${encodeURIComponent(referenceId)}`;
}

function appendDefinitionField(parent, label, value) {
    const labelElement = document.createElement("dt");
    labelElement.textContent = label;
    const valueElement = document.createElement("dd");
    valueElement.textContent = value || "—";
    parent.append(labelElement, valueElement);
}

function appendReferenceList(root, parent, kind, entity) {
    const projectId = builderState.currentProject.project_id;
    const references = document.createElement("div");
    references.className = "mvb-reference-list";
    const retainedIds = new Set(pendingResourceReferenceIds(kind));
    const retainedReferences = entity.references.filter((reference) => retainedIds.has(reference.reference_id));
    if (!retainedReferences.length) {
        const empty = document.createElement("p");
        empty.className = "mvb-reference-empty";
        empty.textContent = "No reference images.";
        references.append(empty);
    }

    for (const reference of retainedReferences) {
        const item = document.createElement("figure");
        item.className = "mvb-reference-item";

        const image = document.createElement("img");
        image.src = referenceUrl(projectId, kind, entity[`${kind === "characters" ? "character" : "location"}_id`], reference.reference_id);
        image.alt = `${entity.name} reference`;
        image.title = reference.original_name;
        image.loading = "lazy";

        const caption = document.createElement("figcaption");
        caption.textContent = reference.original_name;

        const removeButton = document.createElement("button");
        removeButton.className = "mvb-button mvb-button-small mvb-button-danger";
        removeButton.type = "button";
        removeButton.textContent = "Remove";
        removeButton.disabled = Boolean(builderState.operation);
        removeButton.addEventListener("click", () => {
            setPendingResourceReferenceIds(
                kind,
                pendingResourceReferenceIds(kind).filter((referenceId) => referenceId !== reference.reference_id),
            );
            renderResourceDialogReferences(root, kind, entity);
        });

        item.append(image, caption, removeButton);
        references.append(item);
    }
    parent.append(references);
}

function appendReferenceUpload(root, parent, kind, entity) {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";
    input.hidden = true;
    input.addEventListener("change", () => {
        const selected = [...(input.files || [])];
        if (!selected.length) {
            return;
        }
        setPendingResourceFiles(kind, [...pendingResourceFiles(kind), ...selected]);
        input.value = "";
        renderResourceDialogReferences(root, kind, entity);
    });

    const button = document.createElement("button");
    button.className = "mvb-button mvb-button-secondary mvb-button-small";
    button.type = "button";
    button.textContent = entity ? "Add Reference" : "Choose Reference Images";
    button.disabled = Boolean(builderState.operation);
    button.addEventListener("click", () => input.click());
    parent.append(button, input);
}

function appendDeleteControls(root, parent, kind, entity) {
    const entityId = entity[kind === "characters" ? "character_id" : "location_id"];
    const isPending = builderState.deleteConfirm?.kind === kind
        && builderState.deleteConfirm?.id === entityId;
    if (!isPending) {
        const deleteButton = document.createElement("button");
        deleteButton.className = "mvb-button mvb-button-danger mvb-button-small";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.disabled = Boolean(builderState.operation);
        deleteButton.addEventListener("click", () => {
            builderState.deleteConfirm = { kind, id: entityId };
            renderProjectState(root);
        });
        parent.append(deleteButton);
        return;
    }

    const confirmation = document.createElement("span");
    confirmation.className = "mvb-delete-confirmation";
    confirmation.textContent = `Delete ${entity.name}?`;
    const confirmButton = document.createElement("button");
    confirmButton.className = "mvb-button mvb-button-danger mvb-button-small";
    confirmButton.type = "button";
    confirmButton.textContent = "Confirm";
    confirmButton.addEventListener("click", () => void deleteEntity(root, kind, entityId));
    const cancelButton = document.createElement("button");
    cancelButton.className = "mvb-button mvb-button-secondary mvb-button-small";
    cancelButton.type = "button";
    cancelButton.textContent = "Cancel";
    cancelButton.addEventListener("click", () => {
        builderState.deleteConfirm = null;
        renderProjectState(root);
    });
    parent.append(confirmation, confirmButton, cancelButton);
}

function resourceDialogPrefix(kind) {
    return kind === "characters" ? "character" : "location";
}

function pendingResourceFiles(kind) {
    return kind === "characters"
        ? builderState.characterDialogFiles
        : builderState.locationDialogFiles;
}

function setPendingResourceFiles(kind, files) {
    if (kind === "characters") {
        builderState.characterDialogFiles = files;
    } else {
        builderState.locationDialogFiles = files;
    }
}

function pendingResourceReferenceIds(kind) {
    return kind === "characters"
        ? builderState.characterDialogReferenceIds
        : builderState.locationDialogReferenceIds;
}

function setPendingResourceReferenceIds(kind, referenceIds) {
    if (kind === "characters") {
        builderState.characterDialogReferenceIds = referenceIds;
    } else {
        builderState.locationDialogReferenceIds = referenceIds;
    }
}

function appendEntityPreview(root, parent, kind, entity) {
    const preview = document.createElement("div");
    preview.className = "mvb-entity-card-preview";
    const reference = entity.references[0];
    if (reference) {
        const image = document.createElement("img");
        const entityId = entity[kind === "characters" ? "character_id" : "location_id"];
        image.src = referenceUrl(builderState.currentProject.project_id, kind, entityId, reference.reference_id);
        image.alt = `${entity.name} reference preview`;
        image.loading = "lazy";
        preview.append(image);
    } else {
        preview.textContent = "No image";
        preview.classList.add("mvb-entity-card-preview-empty");
    }
    parent.append(preview);
}

function renderResourceDialogReferences(root, kind, entity = null) {
    if (!isActive(root)) {
        return;
    }
    const prefix = resourceDialogPrefix(kind);
    const container = root.querySelector(`[data-mvb-${prefix}-references]`);
    if (!container) {
        return;
    }
    container.replaceChildren();

    const heading = document.createElement("p");
    heading.className = "mvb-dialog-section-label";
    heading.textContent = "Reference images";
    container.append(heading);

    if (entity) {
        appendReferenceList(root, container, kind, entity);
    }
    appendReferenceUpload(root, container, kind, entity);

    const files = pendingResourceFiles(kind);
    const pending = document.createElement("ul");
    pending.className = "mvb-dialog-reference-pending";
    pending.hidden = files.length === 0;
    files.forEach((file, index) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = file.name;
        const removeButton = document.createElement("button");
        removeButton.className = "mvb-button mvb-button-danger mvb-button-small";
        removeButton.type = "button";
        removeButton.textContent = "Remove";
        removeButton.disabled = Boolean(builderState.operation);
        removeButton.addEventListener("click", () => {
            const next = [...pendingResourceFiles(kind)];
            next.splice(index, 1);
            setPendingResourceFiles(kind, next);
            renderResourceDialogReferences(root, kind, entity);
        });
        item.append(name, removeButton);
        pending.append(item);
    });
    container.append(pending);
}

function refreshOpenResourceDialog(root) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }
    const characterDialog = root.querySelector("[data-mvb-character-dialog]");
    const characterForm = root.querySelector("[data-mvb-character-form]");
    if (characterDialog && !characterDialog.hidden && characterForm?.dataset.entityId) {
        const character = findCharacter(builderState.currentProject, characterForm.dataset.entityId);
        if (character) {
            renderResourceDialogReferences(root, "characters", character);
        }
        return;
    }
    const locationDialog = root.querySelector("[data-mvb-location-dialog]");
    const locationForm = root.querySelector("[data-mvb-location-form]");
    if (locationDialog && !locationDialog.hidden && locationForm?.dataset.entityId) {
        const location = findLocation(builderState.currentProject, locationForm.dataset.entityId);
        if (location) {
            renderResourceDialogReferences(root, "locations", location);
        }
    }
}

function appendCharacterCard(root, character) {
    const card = document.createElement("article");
    card.className = "mvb-entity-card";

    appendEntityPreview(root, card, "characters", character);
    const heading = document.createElement("div");
    heading.className = "mvb-entity-card-heading";
    const title = document.createElement("h3");
    title.textContent = character.name;
    heading.append(title);

    const actions = document.createElement("div");
    actions.className = "mvb-entity-actions";
    const editButton = document.createElement("button");
    editButton.className = "mvb-button mvb-button-secondary mvb-button-small";
    editButton.type = "button";
    editButton.textContent = "Edit";
    editButton.disabled = Boolean(builderState.operation);
    editButton.addEventListener("click", () => openCharacterDialog(root, character));
    actions.append(editButton);
    appendDeleteControls(root, actions, "characters", character);
    card.append(heading, actions);
    return card;
}

function appendLocationCard(root, location) {
    const card = document.createElement("article");
    card.className = "mvb-entity-card";

    appendEntityPreview(root, card, "locations", location);
    const heading = document.createElement("div");
    heading.className = "mvb-entity-card-heading";
    const title = document.createElement("h3");
    title.textContent = location.name;
    heading.append(title);

    const actions = document.createElement("div");
    actions.className = "mvb-entity-actions";
    const editButton = document.createElement("button");
    editButton.className = "mvb-button mvb-button-secondary mvb-button-small";
    editButton.type = "button";
    editButton.textContent = "Edit";
    editButton.disabled = Boolean(builderState.operation);
    editButton.addEventListener("click", () => openLocationDialog(root, location));
    actions.append(editButton);
    appendDeleteControls(root, actions, "locations", location);
    card.append(heading, actions);
    return card;
}

function findCharacter(project, characterId) {
    return project.characters.find((character) => character.character_id === characterId) || null;
}

function findLocation(project, locationId) {
    return project.locations.find((location) => location.location_id === locationId) || null;
}

function storyboardStatus(project) {
    if (!project?.storyboard?.scenes?.length) {
        return "No storyboard applied";
    }
    return builderState.storyboardAppliedStale ? "Out of date" : "Applied";
}

function setStoryboardRelayMessage(message, state = "success") {
    builderState.storyboardRelayMessage = message;
    builderState.storyboardRelayState = message ? state : "ready";
}

function invalidateStoryboardRequestForDirectionEdit() {
    builderState.storyboardRequest = null;
    builderState.storyboardPreview = null;
    builderState.storyboardRequestStale = true;
    builderState.storyboardAppliedStale = Boolean(builderState.currentProject?.storyboard?.scenes?.length);
    setStoryboardRelayMessage("");
}

function appendStoryboardPreviewField(parent, label, value) {
    const field = document.createElement("div");
    field.className = "mvb-storyboard-preview-field";
    const labelElement = document.createElement("span");
    labelElement.className = "mvb-storyboard-preview-label";
    labelElement.textContent = label;
    const valueElement = document.createElement("span");
    valueElement.className = "mvb-storyboard-preview-value";
    valueElement.textContent = value || "—";
    field.append(labelElement, valueElement);
    parent.append(field);
}

function renderStoryboardPreview(root) {
    if (!isActive(root)) {
        return;
    }

    const preview = root.querySelector("[data-mvb-storyboard-preview]");
    const empty = root.querySelector("[data-mvb-storyboard-preview-empty]");
    const rows = root.querySelector("[data-mvb-storyboard-preview-rows]");
    const previewState = root.querySelector("[data-mvb-storyboard-preview-state]");
    if (!preview || !empty || !rows || !previewState) {
        return;
    }

    const project = builderState.currentProject;
    const storyboard = builderState.storyboardPreview || project?.storyboard;
    const storyboardScenes = storyboard?.scenes || [];
    rows.replaceChildren();
    preview.hidden = false;
    empty.hidden = storyboardScenes.length > 0;
    previewState.textContent = builderState.storyboardPreview
        ? "Validated preview — not yet applied"
        : storyboardScenes.length
            ? storyboardStatus(project)
            : "No applied scene allocations";
    previewState.dataset.state = builderState.storyboardPreview
        ? "preview"
        : storyboardScenes.length && builderState.storyboardAppliedStale
            ? "warning"
            : "ready";

    if (!project) {
        return;
    }

    const scenesById = new Map(project.scenes.map((scene) => [scene.scene_id, scene]));
    for (const [index, storyboardScene] of storyboardScenes.entries()) {
        const renderScene = scenesById.get(storyboardScene.scene_id);
        const card = document.createElement("article");
        card.className = "mvb-storyboard-preview-scene";

        const heading = document.createElement("div");
        heading.className = "mvb-storyboard-preview-heading";
        const title = document.createElement("h4");
        title.textContent = `Scene ${String(index + 1).padStart(2, "0")}`;
        const timeline = document.createElement("span");
        timeline.textContent = renderScene
            ? `${formatTimelineMs(renderScene.timeline_start_ms)} → ${formatTimelineMs(renderScene.timeline_end_ms)}`
            : "Timeline unavailable";
        heading.append(title, timeline);
        card.append(heading);

        const context = document.createElement("p");
        context.className = "mvb-storyboard-preview-context";
        context.textContent = renderScene
            ? renderScene.source_kind === "lyric"
                ? `Lyric · ${renderScene.lyric || "No lyric text"}`
                : "Instrumental"
            : "Current render scene unavailable";
        card.append(context);

        const fields = document.createElement("div");
        fields.className = "mvb-storyboard-preview-fields";
        appendStoryboardPreviewField(fields, "Type", storyboardScene.scene_type);
        appendStoryboardPreviewField(
            fields,
            "Characters",
            storyboardScene.character_ids
                .map((characterId) => findCharacter(project, characterId)?.name || "Unknown character")
                .join(", "),
        );
        appendStoryboardPreviewField(
            fields,
            "Location",
            storyboardScene.location_id
                ? findLocation(project, storyboardScene.location_id)?.name || "Unknown location"
                : "—",
        );
        appendStoryboardPreviewField(fields, "Action", storyboardScene.action);
        appendStoryboardPreviewField(fields, "Visual", storyboardScene.visual_instructions);
        appendStoryboardPreviewField(fields, "Camera", storyboardScene.camera_direction);
        appendStoryboardPreviewField(fields, "Motion", storyboardScene.motion_direction);
        appendStoryboardPreviewField(fields, "Continuity", storyboardScene.continuity_notes);
        appendStoryboardPreviewField(
            fields,
            "References",
            storyboardScene.required_references.map((selector) => {
                const entity = selector.entity_type === "character"
                    ? findCharacter(project, selector.entity_id)
                    : findLocation(project, selector.entity_id);
                const reference = entity?.references.find((item) => item.reference_id === selector.reference_id);
                return entity && reference ? `${entity.name} · ${reference.original_name}` : "Unknown reference";
            }).join(", "),
        );
        card.append(fields);
        rows.append(card);
    }
}

function renderStoryboardRelay(root) {
    if (!isActive(root)) {
        return;
    }

    const project = builderState.currentProject;
    const direction = project?.story_direction || {
        storyboard_mode: "loose",
        story_brief: "",
        visual_notes: "",
    };
    const brief = root.querySelector("[data-mvb-story-brief]");
    const visualNotes = root.querySelector("[data-mvb-visual-notes]");
    const storyboardMode = root.querySelector("[data-mvb-storyboard-mode]");
    const storyboardModeHelp = root.querySelector("[data-mvb-storyboard-mode-help]");
    const requestPreview = root.querySelector("[data-mvb-request-json]");
    const responseInput = root.querySelector("[data-mvb-response-json]");
    const requestButton = root.querySelector("[data-mvb-generate-request]");
    const copyButton = root.querySelector("[data-mvb-copy-request]");
    const openGptButton = root.querySelector("[data-mvb-storyboard-open-gpt]");
    const validateButton = root.querySelector("[data-mvb-validate-response]");
    const clearPasteButton = root.querySelector("[data-mvb-clear-paste]");
    const applyButton = root.querySelector("[data-mvb-apply-storyboard]");
    const clearButton = root.querySelector("[data-mvb-clear-storyboard]");
    const status = root.querySelector("[data-mvb-storyboard-relay-status]");
    if (!brief || !visualNotes || !storyboardMode || !storyboardModeHelp || !requestPreview || !responseInput || !requestButton || !copyButton || !openGptButton || !validateButton || !clearPasteButton || !applyButton || !clearButton || !status) {
        return;
    }

    if (document.activeElement !== storyboardMode && storyboardMode.value !== direction.storyboard_mode) {
        storyboardMode.value = direction.storyboard_mode;
    }
    if (document.activeElement !== brief && brief.value !== direction.story_brief) {
        brief.value = direction.story_brief;
    }
    if (document.activeElement !== visualNotes && visualNotes.value !== direction.visual_notes) {
        visualNotes.value = direction.visual_notes;
    }
    if (document.activeElement !== responseInput && responseInput.value !== builderState.storyboardResponseText) {
        responseInput.value = builderState.storyboardResponseText;
    }
    storyboardModeHelp.textContent = STORYBOARD_MODE_HELP[direction.storyboard_mode] || STORYBOARD_MODE_HELP.loose;
    const requestJson = storyboardRequestJson(builderState.storyboardRequest);
    requestPreview.value = requestJson || "Generate a request to preview its JSON.";

    const blocked = !project || builderState.closing || builderState.transitioning || Boolean(builderState.operation) || builderState.gptLaunchInFlight;
    storyboardMode.disabled = blocked;
    brief.disabled = blocked;
    visualNotes.disabled = blocked;
    requestButton.disabled = blocked || !project.scenes.length;
    copyButton.disabled = !canCopyStoryboardRequest(
        builderState.storyboardRequest,
        builderState.storyboardRequestStale,
        blocked,
    );
    openGptButton.disabled = blocked;
    validateButton.disabled = blocked || !builderState.storyboardResponseText.trim();
    clearPasteButton.disabled = !builderState.storyboardResponseText;
    applyButton.disabled = blocked || !builderState.storyboardPreview;
    clearButton.disabled = blocked || !project?.storyboard?.scenes?.length;

    const relayMessage = builderState.storyboardRelayMessage;
    const relayOperation = builderState.operation === "storyboard-request"
        ? "Generating request…"
        : builderState.operation === "storyboard-validate"
            ? "Validating response…"
            : builderState.operation === "storyboard-apply"
                ? "Applying storyboard…"
                : builderState.operation === "storyboard-clear"
                    ? "Clearing storyboard…"
                    : "";
    status.textContent = relayMessage || relayOperation || (project ? storyboardStatus(project) : "");
    status.dataset.state = relayMessage
        ? builderState.storyboardRelayState
        : builderState.storyboardPreview
            ? "preview"
            : project?.storyboard?.scenes?.length && builderState.storyboardAppliedStale
                ? "warning"
                : builderState.operation
                    ? "working"
                    : "ready";
    renderStoryboardPreview(root);
}

function renderStoryboardState(root) {
    if (!isActive(root)) {
        return;
    }

    const project = builderState.currentProject;
    const characterList = root.querySelector("[data-mvb-character-list]");
    const locationList = root.querySelector("[data-mvb-location-list]");
    const characterEmpty = root.querySelector("[data-mvb-character-empty]");
    const locationEmpty = root.querySelector("[data-mvb-location-empty]");
    const status = root.querySelector("[data-mvb-storyboard-status]");
    const addCharacter = root.querySelector("[data-mvb-add-character]");
    const addLocation = root.querySelector("[data-mvb-add-location]");
    characterList.replaceChildren();
    locationList.replaceChildren();

    if (!project) {
        characterEmpty.hidden = true;
        locationEmpty.hidden = true;
        status.textContent = "";
        renderStoryboardRelay(root);
        return;
    }

    for (const character of project.characters) {
        characterList.append(appendCharacterCard(root, character));
    }
    for (const location of project.locations) {
        locationList.append(appendLocationCard(root, location));
    }
    characterEmpty.hidden = project.characters.length > 0;
    locationEmpty.hidden = project.locations.length > 0;
    addCharacter.disabled = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    addLocation.disabled = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    const operationLabel = builderState.operation === "character"
        ? "Saving character…"
        : builderState.operation === "location"
            ? "Saving location…"
            : builderState.operation === "reference"
                ? "Saving reference…"
                : "";
    status.textContent = operationLabel || builderState.storyboardMessage || "";
    status.dataset.state = builderState.storyboardMessage ? "error" : builderState.operation ? "working" : "ready";
    renderStoryboardRelay(root);
}

const VISUAL_METHOD_LABELS = {
    keyframe_i2v: "Keyframe / Image-to-Video",
    reference2video: "Reference-to-Video",
};

function visualSceneFor(project, sceneId) {
    return project?.visuals?.scenes?.find((visualScene) => visualScene.scene_id === sceneId) || null;
}

function storyboardSceneFor(project, sceneId) {
    return project?.storyboard?.scenes?.find((storyboardScene) => storyboardScene.scene_id === sceneId) || null;
}

function visualSelectorKey(selector) {
    return `${selector.entity_type}:${selector.entity_id}:${selector.reference_id}`;
}

function visualReferenceFor(project, selector) {
    const entity = selector.entity_type === "character"
        ? findCharacter(project, selector.entity_id)
        : findLocation(project, selector.entity_id);
    const reference = entity?.references.find((item) => item.reference_id === selector.reference_id);
    return entity && reference ? { entity, reference } : null;
}

function visualAssignedReferences(project, storyboardScene) {
    if (!storyboardScene) {
        return [];
    }
    const assigned = [];
    for (const characterId of storyboardScene.character_ids) {
        const character = findCharacter(project, characterId);
        for (const reference of character?.references || []) {
            assigned.push({
                entity_type: "character",
                entity_id: character.character_id,
                reference_id: reference.reference_id,
            });
        }
    }
    if (storyboardScene.location_id) {
        const location = findLocation(project, storyboardScene.location_id);
        for (const reference of location?.references || []) {
            assigned.push({
                entity_type: "location",
                entity_id: location.location_id,
                reference_id: reference.reference_id,
            });
        }
    }
    return assigned;
}

function visualOwnerKey(selector) {
    return `${selector.entity_type}:${selector.entity_id}`;
}

function visualRequiredReferences(project, storyboardScene) {
    const required = [];
    const seenOwners = new Set();
    for (const marker of storyboardScene?.required_references || []) {
        const ownerKey = visualOwnerKey(marker);
        if (seenOwners.has(ownerKey)) {
            continue;
        }
        seenOwners.add(ownerKey);
        const entity = marker.entity_type === "character"
            ? findCharacter(project, marker.entity_id)
            : findLocation(project, marker.entity_id);
        const referenceIds = new Set();
        for (const reference of entity?.references || []) {
            referenceIds.add(reference.reference_id);
            required.push({
                entity_type: marker.entity_type,
                entity_id: marker.entity_id,
                reference_id: reference.reference_id,
            });
        }
        if (!entity || !referenceIds.has(marker.reference_id)) {
            required.push({ ...marker });
        }
    }
    return required;
}

function visualReferenceLabel(project, selector) {
    const info = visualReferenceFor(project, selector);
    return info
        ? `${info.entity.name} · ${info.reference.original_name}`
        : `${selector.entity_type} · ${selector.reference_id}`;
}

function visualDraftFor(sceneId, visualScene) {
    const existing = builderState.visualDrafts[sceneId];
    if (existing) {
        return existing;
    }
    const source = visualScene.keyframe_i2v;
    const draft = {
        keyframe_generation_prompt: source.keyframe_generation_prompt,
        intended_keyframe_description: source.intended_keyframe_description,
        actual_keyframe_description: source.actual_keyframe_description,
    };
    builderState.visualDrafts[sceneId] = draft;
    return draft;
}

function visualDraftIsDirty(visualScene, draft) {
    return Boolean(draft)
        && (draft.keyframe_generation_prompt !== visualScene.keyframe_i2v.keyframe_generation_prompt
            || draft.intended_keyframe_description !== visualScene.keyframe_i2v.intended_keyframe_description
            || draft.actual_keyframe_description !== visualScene.keyframe_i2v.actual_keyframe_description);
}

function deriveVisualReadinessClient(project, visualScene) {
    const missing = [];
    const storyboardScenes = project?.storyboard?.scenes || [];
    const storyboardScene = storyboardSceneFor(project, visualScene.scene_id);
    if (!storyboardScenes.length) {
        missing.push("Apply a storyboard before continuing.");
    } else if (builderState.storyboardAppliedStale) {
        missing.push("Storyboard is out of date.");
    } else if (!storyboardScene) {
        missing.push("Current storyboard scene allocation is missing.");
    }

    if (visualScene.generation_method === "keyframe_i2v") {
        const keyframe = visualScene.keyframe_i2v;
        if (!keyframe.keyframe_generation_prompt) {
            missing.push("Missing keyframe generation prompt");
        }
        if (!keyframe.intended_keyframe_description) {
            missing.push("Missing intended keyframe description");
        }
        if (!keyframe.accepted_keyframe) {
            missing.push("Missing accepted keyframe");
        }
        if (!keyframe.actual_keyframe_description) {
            missing.push("Missing actual image description");
        }
    } else {
        const selected = visualScene.reference2video.selected_references;
        const assigned = new Set(visualAssignedReferences(project, storyboardScene).map(visualSelectorKey));
        const selectedKeys = new Set(selected.map(visualSelectorKey));
        const required = visualRequiredReferences(project, storyboardScene);
        if (required.length > MAX_REF2VA_STILL_REFERENCES) {
            missing.push(
                `Storyboard-required references require ${required.length} images, exceeding the Reference-to-Video maximum of ${MAX_REF2VA_STILL_REFERENCES}.`,
            );
        } else if (selected.length > MAX_REF2VA_STILL_REFERENCES) {
            missing.push(`Reference-to-Video supports at most ${MAX_REF2VA_STILL_REFERENCES} still references`);
        }
        if (!selected.length) {
            missing.push("Select at least one still reference");
        }
        if (selected.some((selector) => !assigned.has(visualSelectorKey(selector)))) {
            missing.push("A selected reference is not assigned to this scene");
        }
        for (const requiredSelector of required) {
            if (!selectedKeys.has(visualSelectorKey(requiredSelector))) {
                missing.push(
                    `Required storyboard reference '${visualReferenceLabel(project, requiredSelector)}' is missing from synchronized Visuals selection.`,
                );
            }
        }
    }
    return {
        ready: missing.length === 0,
        missing: [...new Set(missing)],
    };
}

function appendVisualContextField(parent, label, value) {
    const field = document.createElement("div");
    field.className = "mvb-visual-context-field";
    const labelElement = document.createElement("span");
    labelElement.className = "mvb-visual-context-label";
    labelElement.textContent = label;
    const valueElement = document.createElement("span");
    valueElement.className = "mvb-visual-context-value";
    valueElement.textContent = value || "—";
    field.append(labelElement, valueElement);
    parent.append(field);
}

function makeVisualButton(label, className, handler, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `mvb-button mvb-button-small ${className}`;
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", handler);
    return button;
}

function visualPath(projectId, sceneId, suffix = "") {
    const tail = suffix ? `/${suffix}` : "";
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/visuals/scenes/${encodeURIComponent(sceneId)}${tail}`;
}

function visualProjectPath(projectId, suffix = "") {
    const tail = suffix ? `/${suffix}` : "";
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/visuals${tail}`;
}

function captureVisualViewport(root) {
    if (!isActive(root) || builderState.activeView !== "visuals") {
        return null;
    }
    const visuals = root.querySelector("[data-mvb-visuals]");
    const content = root.querySelector(".mvb-content");
    return {
        visuals: visuals ? { top: visuals.scrollTop, left: visuals.scrollLeft } : null,
        content: content ? { top: content.scrollTop, left: content.scrollLeft } : null,
    };
}

function restoreVisualViewport(root, viewport) {
    if (!viewport || !isActive(root) || builderState.activeView !== "visuals") {
        return;
    }
    const apply = () => {
        if (!isActive(root) || builderState.activeView !== "visuals") {
            return;
        }
        const visuals = root.querySelector("[data-mvb-visuals]");
        const content = root.querySelector(".mvb-content");
        if (visuals && viewport.visuals) {
            visuals.scrollTop = viewport.visuals.top;
            visuals.scrollLeft = viewport.visuals.left;
        }
        if (content && viewport.content) {
            content.scrollTop = viewport.content.top;
            content.scrollLeft = viewport.content.left;
        }
    };
    apply();
    window.requestAnimationFrame(apply);
}

function appendVisualTextarea(parent, label, value, rows, onInput, disabled = false) {
    const field = document.createElement("label");
    field.className = "mvb-field mvb-visual-field";
    const labelElement = document.createElement("span");
    labelElement.textContent = label;
    const textarea = document.createElement("textarea");
    textarea.rows = rows;
    textarea.maxLength = 8000;
    textarea.value = value;
    textarea.disabled = disabled;
    textarea.addEventListener("input", () => onInput(textarea.value));
    field.append(labelElement, textarea);
    parent.append(field);
    return textarea;
}

function plannedVisualReferenceMapping(project, selectedReferences) {
    const subjectNumbers = new Map();
    const mapping = [];
    for (const [index, selector] of selectedReferences.entries()) {
        const info = visualReferenceFor(project, selector);
        if (!info) {
            continue;
        }
        const ownerKey = `${selector.entity_type}:${selector.entity_id}`;
        if (!subjectNumbers.has(ownerKey)) {
            subjectNumbers.set(ownerKey, subjectNumbers.size + 1);
        }
        const subjectTag = `<Subject ${subjectNumbers.get(ownerKey)}>`;
        mapping.push({
            pictureTag: `<Picture ${index + 1}>`,
            subjectTag,
            entityName: info.entity.name,
            originalName: info.reference.original_name,
        });
    }
    return mapping;
}

function renderVisualKeyframeBranch(root, body, scene, visualScene, disabled) {
    const draft = visualDraftFor(visualScene.scene_id, visualScene);
    const branch = document.createElement("div");
    branch.className = "mvb-visual-branch";

    appendVisualTextarea(
        branch,
        "Keyframe generation prompt",
        draft.keyframe_generation_prompt,
        5,
        (value) => {
            draft.keyframe_generation_prompt = value;
            builderState.visualMessage = "";
        },
        disabled,
    );
    appendVisualTextarea(
        branch,
        "Intended keyframe description",
        draft.intended_keyframe_description,
        3,
        (value) => {
            draft.intended_keyframe_description = value;
            builderState.visualMessage = "";
        },
        disabled,
    );
    appendVisualTextarea(
        branch,
        "Actual keyframe description",
        draft.actual_keyframe_description,
        3,
        (value) => {
            draft.actual_keyframe_description = value;
            builderState.visualMessage = "";
        },
        disabled,
    );

    const accepted = visualScene.keyframe_i2v.accepted_keyframe;
    if (accepted) {
        const preview = document.createElement("figure");
        preview.className = "mvb-visual-keyframe-preview";
        const image = document.createElement("img");
        const imagePath = visualPath(builderState.currentProject.project_id, visualScene.scene_id, "keyframe/image");
        image.src = `${imagePath}?v=${encodeURIComponent(accepted.asset_id)}`;
        image.alt = `Accepted keyframe for scene ${scene.scene_id}`;
        image.loading = "lazy";
        const caption = document.createElement("figcaption");
        caption.textContent = accepted.original_name;
        preview.append(image, caption);
        branch.append(preview);
    }

    const actions = document.createElement("div");
    actions.className = "mvb-visual-actions";
    actions.append(
        makeVisualButton(
            "Save Details",
            "mvb-button-primary",
            () => void saveVisualDetails(root, visualScene.scene_id),
            disabled,
        ),
        makeVisualButton(
            "Generate Prompt",
            "mvb-button-secondary",
            () => void generateKeyframePrompt(root, visualScene.scene_id),
            disabled,
        ),
        makeVisualButton(
            "Copy Prompt",
            "mvb-button-secondary",
            () => void copyKeyframePrompt(root, visualScene.scene_id),
            disabled || !draft.keyframe_generation_prompt,
        ),
    );

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";
    fileInput.hidden = true;
    fileInput.addEventListener("change", () => {
        const file = fileInput.files?.[0];
        if (!file) {
            return;
        }
        void assignKeyframe(root, visualScene.scene_id, file).finally(() => {
            fileInput.value = "";
        });
    });
    actions.append(
        makeVisualButton(
            accepted ? "Replace Keyframe" : "Assign Keyframe",
            "mvb-button-secondary",
            () => fileInput.click(),
            disabled,
        ),
        fileInput,
    );

    if (accepted && builderState.visualRemoveTarget === visualScene.scene_id) {
        const confirmation = document.createElement("span");
        confirmation.className = "mvb-visual-remove-confirmation";
        confirmation.textContent = "Remove this accepted image?";
        actions.append(
            confirmation,
            makeVisualButton(
                "Confirm Remove",
                "mvb-button-danger",
                () => void removeKeyframe(root, visualScene.scene_id),
                disabled,
            ),
            makeVisualButton(
                "Keep",
                "mvb-button-secondary",
                () => {
                    builderState.visualRemoveTarget = null;
                    renderVisualsState(root);
                },
                disabled,
            ),
        );
    } else if (accepted) {
        actions.append(makeVisualButton(
            "Remove Keyframe",
            "mvb-button-danger",
            () => {
                builderState.visualRemoveTarget = visualScene.scene_id;
                renderVisualsState(root);
            },
            disabled,
        ));
    }
    branch.append(actions, document.createElement("div"));
    body.append(branch);
}

function renderVisualReferenceBranch(root, body, scene, visualScene, disabled) {
    const project = builderState.currentProject;
    const storyboardScene = storyboardSceneFor(project, scene.scene_id);
    const assigned = visualAssignedReferences(project, storyboardScene);
    const selected = visualScene.reference2video.selected_references;
    const selectedKeys = new Set(selected.map(visualSelectorKey));
    const required = visualRequiredReferences(project, storyboardScene);
    const requiredKeys = required.map(visualSelectorKey);
    const requiredKeySet = new Set(requiredKeys);
    const branch = document.createElement("div");
    branch.className = "mvb-visual-branch";

    const chooser = document.createElement("div");
    chooser.className = "mvb-visual-reference-chooser";
    const select = document.createElement("select");
    select.className = "mvb-visual-reference-select";
    select.setAttribute("aria-label", "Assigned reference to add");
    for (const selector of assigned) {
        if (selectedKeys.has(visualSelectorKey(selector))) {
            continue;
        }
        const info = visualReferenceFor(project, selector);
        if (!info) {
            continue;
        }
        const option = document.createElement("option");
        option.value = visualSelectorKey(selector);
        option.textContent = `${info.entity.name} · ${info.reference.original_name}${requiredKeySet.has(visualSelectorKey(selector)) ? " · REQUIRED" : ""}`;
        select.append(option);
    }
    const addButton = makeVisualButton(
        "Add Reference",
        "mvb-button-secondary",
        () => {
            const selector = assigned.find((item) => visualSelectorKey(item) === select.value);
            if (selector) {
                void saveReferenceSelection(root, scene.scene_id, [...selected, selector]);
            }
        },
        disabled || selected.length >= MAX_REF2VA_STILL_REFERENCES || !select.options.length,
    );
    if (required.length > MAX_REF2VA_STILL_REFERENCES || selected.length >= MAX_REF2VA_STILL_REFERENCES) {
        const limit = document.createElement("p");
        limit.className = "mvb-visual-missing";
        limit.textContent = required.length > MAX_REF2VA_STILL_REFERENCES
            ? `Storyboard-required references require ${required.length} images, exceeding the Reference-to-Video maximum of ${MAX_REF2VA_STILL_REFERENCES}.`
            : `Reference-to-Video supports up to ${MAX_REF2VA_STILL_REFERENCES} still references.`;
        chooser.append(limit);
    }
    chooser.append(select, addButton);
    branch.append(chooser);

    const selectedList = document.createElement("div");
    selectedList.className = "mvb-visual-selected-references";
    if (!selected.length) {
        const empty = document.createElement("p");
        empty.className = "mvb-visual-empty";
        empty.textContent = "No still references selected.";
        selectedList.append(empty);
    }
    for (const [index, selector] of selected.entries()) {
        const info = visualReferenceFor(project, selector);
        const row = document.createElement("div");
        row.className = "mvb-visual-selected-reference";
        const isRequired = requiredKeySet.has(visualSelectorKey(selector));
        if (isRequired) {
            row.classList.add("mvb-visual-selected-reference-required");
            row.dataset.required = "true";
        }
        if (!info) {
            const missingText = document.createElement("span");
            missingText.className = "mvb-visual-reference-label";
            missingText.textContent = "Reference is no longer available.";
            if (isRequired) {
                const badge = document.createElement("span");
                badge.className = "mvb-visual-required-badge";
                badge.textContent = "REQUIRED";
                badge.setAttribute("aria-label", "Required Storyboard reference");
                missingText.append(" ", badge);
            }
            row.append(missingText);
            selectedList.append(row);
            continue;
        }
        const image = document.createElement("img");
        image.src = referenceUrl(project.project_id, selector.entity_type === "character" ? "characters" : "locations", selector.entity_id, selector.reference_id);
        image.alt = `${info.entity.name} reference`;
        image.loading = "lazy";
        const text = document.createElement("span");
        text.className = "mvb-visual-reference-label";
        text.textContent = `${index + 1}. ${info.entity.name} · ${info.reference.original_name}`;
        if (isRequired) {
            const badge = document.createElement("span");
            badge.className = "mvb-visual-required-badge";
            badge.textContent = "REQUIRED";
            badge.setAttribute("aria-label", "Required Storyboard reference");
            text.append(" ", badge);
        }
        const rowActions = document.createElement("span");
        rowActions.className = "mvb-visual-row-actions";
        const previousIsRequired = index > 0 && requiredKeySet.has(visualSelectorKey(selected[index - 1]));
        const nextIsRequired = index < selected.length - 1 && requiredKeySet.has(visualSelectorKey(selected[index + 1]));
        const upBlockedByRequiredGroup = index <= required.length;
        const downBlockedByRequiredGroup = index < required.length || nextIsRequired;
        rowActions.append(
            makeVisualButton(
                "Up",
                "mvb-button-secondary",
                () => {
                    if (index === 0 || isRequired || upBlockedByRequiredGroup || previousIsRequired) return;
                    const reordered = [...selected];
                    [reordered[index - 1], reordered[index]] = [reordered[index], reordered[index - 1]];
                    void saveReferenceSelection(root, scene.scene_id, reordered);
                },
                disabled || index === 0 || isRequired || upBlockedByRequiredGroup || previousIsRequired,
            ),
            makeVisualButton(
                "Down",
                "mvb-button-secondary",
                () => {
                    if (index === selected.length - 1 || isRequired || downBlockedByRequiredGroup) return;
                    const reordered = [...selected];
                    [reordered[index], reordered[index + 1]] = [reordered[index + 1], reordered[index]];
                    void saveReferenceSelection(root, scene.scene_id, reordered);
                },
                disabled || index === selected.length - 1 || isRequired || downBlockedByRequiredGroup,
            ),
            makeVisualButton(
                "Remove",
                "mvb-button-danger",
                () => void saveReferenceSelection(root, scene.scene_id, selected.filter((_, itemIndex) => itemIndex !== index)),
                disabled || isRequired,
            ),
        );
        row.append(image, text, rowActions);
        selectedList.append(row);
    }
    branch.append(selectedList);

    const mapping = document.createElement("div");
    mapping.className = "mvb-visual-mapping";
    const mappingHeading = document.createElement("p");
    mappingHeading.className = "mvb-visual-subheading";
    mappingHeading.textContent = "Planned reference mapping";
    mapping.append(mappingHeading);
    const mappingList = document.createElement("ul");
    const planned = plannedVisualReferenceMapping(project, selected);
    if (!planned.length) {
        const empty = document.createElement("li");
        empty.textContent = "Select assigned references to preview deterministic Picture order.";
        mappingList.append(empty);
    }
    for (const item of planned) {
        const entry = document.createElement("li");
        entry.textContent = `${item.pictureTag} · ${item.entityName} · ${item.originalName}${item.subjectTag ? ` · ${item.subjectTag}` : ""}`;
        mappingList.append(entry);
    }
    mapping.append(mappingList);
    branch.append(mapping);
    body.append(branch);
}

function renderVisualSceneCard(root, scene, visualScene, index) {
    const readiness = deriveVisualReadinessClient(builderState.currentProject, visualScene);
    const card = document.createElement("details");
    card.className = "mvb-visual-card";
    const hasExpansionState = Object.prototype.hasOwnProperty.call(builderState.visualExpandedScenes, scene.scene_id);
    card.open = hasExpansionState ? Boolean(builderState.visualExpandedScenes[scene.scene_id]) : index === 0;
    card.addEventListener("toggle", () => {
        if (isActive(root)) {
            builderState.visualExpandedScenes[scene.scene_id] = card.open;
        }
    });

    const summary = document.createElement("summary");
    summary.className = "mvb-visual-summary";
    const title = document.createElement("span");
    title.className = "mvb-visual-summary-title";
    title.textContent = `Scene ${String(index + 1).padStart(2, "0")}`;
    const timing = document.createElement("span");
    timing.className = "mvb-visual-summary-timing";
    timing.textContent = `${formatTimelineMs(scene.timeline_start_ms)} → ${formatTimelineMs(scene.timeline_end_ms)} · ${formatDurationMs(scene.exact_duration_ms)}`;
    const method = document.createElement("span");
    method.className = "mvb-visual-summary-method";
    method.textContent = VISUAL_METHOD_LABELS[visualScene.generation_method];
    const readinessElement = document.createElement("span");
    readinessElement.className = "mvb-visual-readiness";
    readinessElement.dataset.state = readiness.ready ? "ready" : "blocked";
    readinessElement.textContent = readiness.ready ? "Ready" : "Needs input";
    summary.append(title, timing, method, readinessElement);
    card.append(summary);

    const body = document.createElement("div");
    body.className = "mvb-visual-card-body";
    const storyboardScene = storyboardSceneFor(builderState.currentProject, scene.scene_id);
    const context = document.createElement("div");
    context.className = "mvb-visual-context";
    appendVisualContextField(context, "Type", storyboardScene?.scene_type || "No storyboard allocation");
    appendVisualContextField(context, "Source", scene.source_kind === "lyric" ? scene.lyric : "Instrumental");
    appendVisualContextField(
        context,
        "Characters",
        storyboardScene?.character_ids.map((id) => findCharacter(builderState.currentProject, id)?.name || "Unknown character").join(", "),
    );
    appendVisualContextField(
        context,
        "Location",
        storyboardScene?.location_id ? findLocation(builderState.currentProject, storyboardScene.location_id)?.name : "—",
    );
    appendVisualContextField(context, "Action", storyboardScene?.action);
    appendVisualContextField(context, "Visual", storyboardScene?.visual_instructions);
    appendVisualContextField(context, "Camera", storyboardScene?.camera_direction);
    appendVisualContextField(context, "Motion", storyboardScene?.motion_direction);
    appendVisualContextField(context, "Continuity", storyboardScene?.continuity_notes);
    appendVisualContextField(
        context,
        "Required references",
        visualRequiredReferences(builderState.currentProject, storyboardScene).map((selector) => {
            const info = visualReferenceFor(builderState.currentProject, selector);
            return info ? `${info.entity.name} · ${info.reference.original_name}` : "Unknown reference";
        }).join(", "),
    );
    body.append(context);

    const controls = document.createElement("div");
    controls.className = "mvb-visual-method-row";
    const methodField = document.createElement("label");
    methodField.className = "mvb-field";
    const methodLabel = document.createElement("span");
    methodLabel.textContent = "Generation method";
    const methodSelect = document.createElement("select");
    methodSelect.setAttribute("aria-label", `Generation method for scene ${index + 1}`);
    for (const value of ["keyframe_i2v", "reference2video"]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = VISUAL_METHOD_LABELS[value];
        option.selected = visualScene.generation_method === value;
        methodSelect.append(option);
    }
    methodSelect.disabled = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    methodSelect.addEventListener("change", () => void changeVisualMethod(root, scene.scene_id, methodSelect.value));
    methodField.append(methodLabel, methodSelect);
    controls.append(methodField);
    body.append(controls);

    const disabled = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    if (visualScene.generation_method === "keyframe_i2v") {
        renderVisualKeyframeBranch(root, body, scene, visualScene, disabled);
    } else {
        renderVisualReferenceBranch(root, body, scene, visualScene, disabled);
    }
    if (readiness.missing.length) {
        const missing = document.createElement("p");
        missing.className = "mvb-visual-missing";
        missing.textContent = readiness.missing.join(" · ");
        body.append(missing);
    }
    card.append(body);
    return card;
}

function renderVisualsState(root, viewport = null) {
    if (!isActive(root)) {
        return;
    }
    const list = root.querySelector("[data-mvb-visual-scenes]");
    const empty = root.querySelector("[data-mvb-visual-empty]");
    const status = root.querySelector("[data-mvb-visual-status]");
    const bulkControls = root.querySelector("[data-mvb-visual-bulk]");
    const bulkMethod = root.querySelector("[data-mvb-visual-bulk-method]");
    const bulkButton = root.querySelector("[data-mvb-visual-bulk-apply]");
    if (!list || !empty || !status || !bulkControls || !bulkMethod || !bulkButton) {
        return;
    }
    const capturedViewport = viewport || captureVisualViewport(root);
    list.replaceChildren();
    const project = builderState.currentProject;
    if (!project) {
        bulkControls.hidden = true;
        empty.hidden = true;
        status.textContent = "";
        restoreVisualViewport(root, capturedViewport);
        return;
    }
    const scenes = project.scenes || [];
    bulkControls.hidden = false;
    bulkMethod.value = builderState.visualBulkMethod;
    const controlsDisabled = Boolean(builderState.operation) || builderState.transitioning || builderState.closing || !scenes.length;
    bulkMethod.disabled = controlsDisabled;
    bulkButton.disabled = controlsDisabled;
    const sceneIds = new Set(scenes.map((scene) => scene.scene_id));
    for (const sceneId of Object.keys(builderState.visualExpandedScenes)) {
        if (!sceneIds.has(sceneId)) {
            delete builderState.visualExpandedScenes[sceneId];
        }
    }
    empty.hidden = scenes.length > 0;
    if (!scenes.length) {
        status.textContent = "Build scenes before preparing Visuals.";
        status.dataset.state = "empty";
        restoreVisualViewport(root, capturedViewport);
        return;
    }
    const operationLabel = builderState.operation?.startsWith("visual") ? "Saving Visuals…" : "";
    status.textContent = builderState.visualMessage || operationLabel;
    status.dataset.state = builderState.visualMessage
        ? builderState.visualMessageState
        : operationLabel
            ? "working"
            : "ready";
    for (const [index, scene] of scenes.entries()) {
        const visualScene = visualSceneFor(project, scene.scene_id);
        if (visualScene) {
            list.append(renderVisualSceneCard(root, scene, visualScene, index));
        }
    }
    restoreVisualViewport(root, capturedViewport);
}

async function runVisualMutation(root, operation, sceneId, task, failureMessage, options = {}) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return false;
    }
    const projectId = builderState.currentProject.project_id;
    const activeView = builderState.activeView;
    const visualViewport = captureVisualViewport(root);
    builderState.operation = operation;
    builderState.visualMessage = "";
    builderState.visualMessageState = "ready";
    renderProjectState(root, { visualViewport });
    let succeeded = false;
    try {
        if (!await flushCurrentProject(root)) {
            builderState.visualMessage = "Save the current project before continuing.";
            builderState.visualMessageState = "error";
            return false;
        }
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        const project = await task(projectId, sceneId);
        if (!hasProjectDocument(project)) {
            throw new Error("Visuals response was invalid.");
        }
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        setCurrentProject(root, project, options);
        builderState.activeView = activeView;
        builderState.visualMessage = "";
        builderState.visualMessageState = "ready";
        if (project.storyboard?.scenes?.length) {
            void loadStoryboardRequest(root, true);
        }
        void loadPromptCards(root, true);
        void loadProjectList(root);
        succeeded = true;
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        console.error(`[Music Video Builder] ${operation} operation failed.`, error);
        builderState.visualMessage = error instanceof Error && error.message ? error.message : failureMessage;
        builderState.visualMessageState = "error";
        return false;
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            if (succeeded) {
                builderState.visualMessage = "";
            }
            renderProjectState(root, { visualViewport });
        }
    }
}

async function saveVisualDetails(root, sceneId) {
    const project = builderState.currentProject;
    const visualScene = visualSceneFor(project, sceneId);
    if (!visualScene) {
        return false;
    }
    const draft = visualDraftFor(sceneId, visualScene);
    return runVisualMutation(
        root,
        "visual-details",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "keyframe-details"), {
            method: "PUT",
            body: JSON.stringify({
                keyframe_generation_prompt: draft.keyframe_generation_prompt,
                intended_keyframe_description: draft.intended_keyframe_description,
                actual_keyframe_description: draft.actual_keyframe_description,
            }),
        }),
        "Keyframe details could not be saved.",
        { reconciledVisualSceneId: sceneId },
    );
}

async function persistVisualDetailsIfDirty(root, sceneId) {
    const visualScene = visualSceneFor(builderState.currentProject, sceneId);
    const draft = visualScene ? builderState.visualDrafts[sceneId] : null;
    if (!visualScene || !visualDraftIsDirty(visualScene, draft)) {
        return true;
    }
    return saveVisualDetails(root, sceneId);
}

async function changeVisualMethod(root, sceneId, generationMethod) {
    const visualScene = visualSceneFor(builderState.currentProject, sceneId);
    if (!visualScene || visualScene.generation_method === generationMethod) {
        return;
    }
    if (!(await persistVisualDetailsIfDirty(root, sceneId))) {
        return;
    }
    await runVisualMutation(
        root,
        "visual-method",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "generation-method"), {
            method: "PUT",
            body: JSON.stringify({ generation_method: generationMethod }),
        }),
        "Generation method could not be saved.",
        { reconciledVisualSceneId: sceneId },
    );
}

async function generateKeyframePrompt(root, sceneId) {
    if (!(await persistVisualDetailsIfDirty(root, sceneId))) {
        return;
    }
    await runVisualMutation(
        root,
        "visual-prompt",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "keyframe-prompt"), { method: "POST" }),
        "Keyframe prompt could not be generated.",
        { reconciledVisualSceneId: sceneId },
    );
}

async function copyKeyframePrompt(root, sceneId) {
    const visualScene = visualSceneFor(builderState.currentProject, sceneId);
    const prompt = builderState.visualDrafts[sceneId]?.keyframe_generation_prompt
        || visualScene?.keyframe_i2v.keyframe_generation_prompt;
    if (!prompt) {
        return;
    }
    try {
        if (!navigator.clipboard?.writeText) {
            throw new Error("Clipboard access is unavailable; select the visible prompt to copy it.");
        }
        await navigator.clipboard.writeText(prompt);
        builderState.visualMessage = "Keyframe prompt copied.";
        builderState.visualMessageState = "success";
    } catch (error) {
        console.error("[Music Video Builder] Keyframe prompt copy failed.", error);
        builderState.visualMessage = error instanceof Error ? error.message : "Keyframe prompt could not be copied.";
        builderState.visualMessageState = "error";
    }
    renderVisualsState(root);
}

async function assignKeyframe(root, sceneId, file) {
    if (!(await persistVisualDetailsIfDirty(root, sceneId))) {
        return;
    }
    await runVisualMutation(
        root,
        "visual-keyframe",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "keyframe"), {
            method: "POST",
            body: uploadFormData(file),
        }),
        "Keyframe could not be saved.",
        { reconciledVisualSceneId: sceneId },
    );
}

async function removeKeyframe(root, sceneId) {
    await runVisualMutation(
        root,
        "visual-remove-keyframe",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "keyframe"), { method: "DELETE" }),
        "Keyframe could not be removed.",
    );
}

async function saveReferenceSelection(root, sceneId, selectedReferences) {
    await runVisualMutation(
        root,
        "visual-references",
        sceneId,
        (projectId, currentSceneId) => fetchJson(visualPath(projectId, currentSceneId, "reference2video"), {
            method: "PUT",
            body: JSON.stringify({ selected_references: selectedReferences }),
        }),
        "Reference selection could not be saved.",
    );
}

async function applyVisualMethodToAllScenes(root) {
    if (!isActive(root) || !builderState.currentProject?.scenes?.length) {
        return;
    }
    await runVisualMutation(
        root,
        "visual-bulk-method",
        null,
        (projectId) => fetchJson(visualProjectPath(projectId, "generation-method"), {
            method: "PUT",
            body: JSON.stringify({ generation_method: builderState.visualBulkMethod }),
        }),
        "Generation method could not be applied to all scenes.",
    );
}

function promptPath(projectId, sceneId, suffix = "") {
    const tail = suffix ? `/${suffix}` : "";
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/prompt${tail}`;
}

function promptDraftFor(sceneId, generationMethod, item) {
    const key = promptDraftKey(sceneId, generationMethod);
    const existing = builderState.promptDrafts[key];
    if (existing) {
        return existing;
    }
    const draft = {
        text: item.saved_final_prompt || "",
        sourceFingerprint: item.saved_source_fingerprint || item.source_fingerprint || null,
        relayFingerprint: item.saved_relay_fingerprint || "",
        dirty: false,
    };
    builderState.promptDrafts[key] = draft;
    return draft;
}

function promptStatusLabel(status) {
    return {
        needs_gpt: "NEEDS GPT",
        current: "CURRENT",
        unsaved: "UNSAVED",
        stale: "STALE",
        error: "ERROR",
    }[status] || "NEEDS GPT";
}

function promptRelayKey(sceneId, generationMethod) {
    return `${sceneId}:${generationMethod}`;
}

function promptRelayFor(sceneId, generationMethod) {
    const key = promptRelayKey(sceneId, generationMethod);
    if (!builderState.promptRelay[key]) {
        builderState.promptRelay[key] = {
            request: null,
            responseText: "",
            responseMessage: "",
            responseMessageState: "ready",
        };
    }
    return builderState.promptRelay[key];
}

function promptRelayIsCurrent(relay, item) {
    return canCopyPromptRelayRequest(relay, item);
}

function updatePromptRelayControls(root, sceneId, generationMethod) {
    if (!isActive(root)) {
        return;
    }
    const item = promptItemFor(sceneId, generationMethod);
    const card = promptCardFor(root, sceneId);
    if (!item || !card) {
        return;
    }
    const relay = promptRelayFor(sceneId, generationMethod);
    const blocked = Boolean(builderState.operation) || builderState.transitioning || builderState.closing || builderState.gptLaunchInFlight;
    const requestCurrent = promptRelayIsCurrent(relay, item);
    const requestPreview = card.querySelector("[data-mvb-prompt-relay-request]");
    const responseInput = card.querySelector("[data-mvb-prompt-relay-response]");
    const copyButton = card.querySelector("[data-mvb-prompt-relay-copy]");
    const applyButton = card.querySelector("[data-mvb-prompt-relay-apply]");
    const relayStatus = card.querySelector("[data-mvb-prompt-relay-status]");
    if (requestPreview && document.activeElement !== requestPreview) {
        requestPreview.value = promptRelayRequestJson(relay.request) || "Generate a request to preview its JSON.";
    }
    if (responseInput && document.activeElement !== responseInput) {
        responseInput.value = relay.responseText;
    }
    if (copyButton) {
        copyButton.disabled = blocked || !canCopyPromptRelayRequest(relay, item);
    }
    if (applyButton) {
        applyButton.disabled = blocked || !canApplyPromptRelayResponse(relay, item);
    }
    if (relayStatus) {
        relayStatus.textContent = relay.responseMessage || (relay.request && !requestCurrent
            ? "Request is stale — generate a new request."
            : "");
        relayStatus.dataset.state = relay.responseMessage
            ? relay.responseMessageState
            : (relay.request && !requestCurrent ? "warning" : "ready");
    }
}

function promptCardFor(root, sceneId) {
    if (!root || !sceneId) {
        return null;
    }
    return [...root.querySelectorAll("[data-mvb-prompt-card]")]
        .find((card) => card.dataset.mvbPromptSceneId === sceneId) || null;
}

function revealPromptReviewSection(root, sceneId, generationMethod) {
    if (!isActive(root) || builderState.activeView !== "prompts") {
        return;
    }
    const editor = [...root.querySelectorAll("[data-mvb-prompt-editor]")]
        .find((candidate) => candidate.dataset.mvbPromptSceneId === sceneId
            && candidate.dataset.mvbPromptGenerationMethod === generationMethod);
    if (!editor) {
        return;
    }
    const marker = generationMethod === "reference2video"
        ? "detailed_description:"
        : "integrated_multimodal_description:";
    const markerIndex = editor.value.indexOf(marker);
    if (markerIndex < 0) {
        return;
    }
    const lineCount = editor.value.slice(0, markerIndex).split("\n").length;
    const lineHeight = Number.parseFloat(window.getComputedStyle(editor).lineHeight) || 20;
    editor.scrollTop = Math.max(0, (lineCount - 2) * lineHeight);
}

function promptScrollContainer(root) {
    return root?.querySelector("[data-mvb-prompts]") || null;
}

function capturePromptViewport(root, anchorSceneId = null) {
    if (!isActive(root) || builderState.activeView !== "prompts") {
        return null;
    }
    const scroller = promptScrollContainer(root);
    if (!scroller) {
        return null;
    }
    const scrollerRect = scroller.getBoundingClientRect();
    const focusedEditor = document.activeElement?.closest("[data-mvb-prompt-editor]");
    const focusedCard = focusedEditor?.closest("[data-mvb-prompt-card]")
        || promptCardFor(root, anchorSceneId)
        || [...root.querySelectorAll("[data-mvb-prompt-card]")].find((card) => {
            const rect = card.getBoundingClientRect();
            return rect.bottom > scrollerRect.top && rect.top < scrollerRect.bottom;
        });
    const cardRect = focusedCard?.getBoundingClientRect();
    return {
        top: scroller.scrollTop,
        left: scroller.scrollLeft,
        sceneId: focusedCard?.dataset.mvbPromptSceneId || null,
        generationMethod: focusedCard?.dataset.mvbPromptGenerationMethod || null,
        cardOffset: cardRect ? cardRect.top - scrollerRect.top : null,
        editor: focusedEditor
            ? {
                sceneId: focusedEditor.dataset.mvbPromptSceneId,
                generationMethod: focusedEditor.dataset.mvbPromptGenerationMethod,
                selectionStart: focusedEditor.selectionStart,
                selectionEnd: focusedEditor.selectionEnd,
            }
            : null,
    };
}

function restorePromptViewport(root, viewport) {
    if (!viewport || !isActive(root) || builderState.activeView !== "prompts") {
        return;
    }
    const apply = () => {
        if (!isActive(root) || builderState.activeView !== "prompts") {
            return;
        }
        const scroller = promptScrollContainer(root);
        if (!scroller) {
            return;
        }
        const card = promptCardFor(root, viewport.sceneId);
        if (card && viewport.cardOffset !== null) {
            const scrollerRect = scroller.getBoundingClientRect();
            const cardRect = card.getBoundingClientRect();
            scroller.scrollTop += cardRect.top - scrollerRect.top - viewport.cardOffset;
        } else {
            scroller.scrollTop = viewport.top;
        }
        scroller.scrollLeft = viewport.left;
        if (viewport.editor) {
            const editor = [...root.querySelectorAll("[data-mvb-prompt-editor]")]
                .find((candidate) => candidate.dataset.mvbPromptSceneId === viewport.editor.sceneId
                    && candidate.dataset.mvbPromptGenerationMethod === viewport.editor.generationMethod);
            if (editor && document.activeElement !== editor) {
                editor.focus({ preventScroll: true });
                if (typeof viewport.editor.selectionStart === "number") {
                    editor.setSelectionRange(viewport.editor.selectionStart, viewport.editor.selectionEnd);
                }
            }
        }
    };
    apply();
    window.requestAnimationFrame(apply);
}

function promptItemFor(sceneId, generationMethod) {
    return builderState.promptCards.find((item) => item.scene_id === sceneId
        && item.generation_method === generationMethod) || null;
}

function promptFinalIsVisible(item, draft) {
    return promptDraftHasCurrentRelay(item, draft)
        || item.status === "current"
        || item.status === "stale";
}

function promptFinalInstruction(item, draft) {
    if (item.status === "stale" && !promptDraftHasCurrentRelay(item, draft)) {
        return "This saved prompt is stale because the scene changed. Run the Prompt Director again before saving a current version.";
    }
    if (draft.dirty || promptDraftStatus(item, draft) === "unsaved") {
        return "Review the assembled prompt below. Edit if required, then save it for rendering.";
    }
    return "This is the saved current prompt. Edit it only if you intend to create a new unsaved revision.";
}

function promptCardsMatchStructure(root) {
    const cards = builderState.promptCards;
    if (!isActive(root) || !builderState.currentProject || !cards.length) {
        return false;
    }
    const list = root.querySelector("[data-mvb-prompt-scenes]");
    if (!list) {
        return false;
    }
    const existing = [...list.querySelectorAll(":scope > [data-mvb-prompt-card]")];
    if (existing.length !== cards.length) {
        return false;
    }
    return existing.every((card, index) => card.dataset.mvbPromptSceneId === cards[index].scene_id
        && card.dataset.mvbPromptGenerationMethod === cards[index].generation_method);
}

function buildPromptFinalSection(root, card, item, draft, blocked) {
    const finalSection = document.createElement("section");
    finalSection.className = "mvb-prompt-final mvb-prompt-workflow-section";
    finalSection.dataset.mvbPromptFinal = "";
    const finalHeading = document.createElement("h4");
    finalHeading.textContent = "Final Prompt";
    const finalInstruction = document.createElement("p");
    finalInstruction.className = "mvb-prompt-instruction mvb-prompt-final-instruction";
    finalInstruction.textContent = promptFinalInstruction(item, draft);
    finalSection.append(finalHeading, finalInstruction);
    const editorField = document.createElement("label");
    editorField.className = "mvb-field mvb-prompt-editor-field";
    const editorLabel = document.createElement("span");
    editorLabel.textContent = "Final Prompt";
    const editor = document.createElement("textarea");
    editor.rows = 12;
    editor.maxLength = 50_000;
    editor.spellcheck = false;
    editor.value = draft.text;
    editor.setAttribute("aria-label", `Final Prompt for scene ${item.sequence}`);
    editor.dataset.mvbPromptEditor = "";
    editor.dataset.mvbPromptSceneId = item.scene_id;
    editor.dataset.mvbPromptGenerationMethod = item.generation_method;
    editor.disabled = blocked || item.status === "error";
    editor.readOnly = !promptDraftHasCurrentRelay(item, draft);
    editorField.append(editorLabel, editor);
    finalSection.append(editorField);
    const actions = document.createElement("div");
    actions.className = "mvb-prompt-actions";
    const dirtyLabel = document.createElement("span");
    dirtyLabel.className = "mvb-prompt-dirty";
    dirtyLabel.dataset.mvbPromptDirty = "";
    dirtyLabel.textContent = "UNSAVED";
    dirtyLabel.classList.toggle("mvb-prompt-dirty-visible", draft.dirty);
    dirtyLabel.setAttribute("aria-hidden", String(!draft.dirty));
    actions.append(dirtyLabel);
    const copyButton = makeVisualButton("Copy", "mvb-button-secondary", () => void copyPrompt(root, item.scene_id, item.generation_method), blocked || !draft.text);
    copyButton.dataset.mvbPromptCopy = "";
    const saveButton = makeVisualButton("Save Prompt", "mvb-button-primary", () => void savePrompt(root, item.scene_id, item.generation_method), blocked || !canSavePromptDraft(item, draft));
    saveButton.dataset.mvbPromptSave = "";
    actions.append(copyButton, saveButton);
    editor.addEventListener("input", () => {
        draft.text = editor.value;
        draft.dirty = promptDraftIsDirty(item, draft);
        builderState.promptMessage = "";
        builderState.promptMessageState = "ready";
        const currentDraftStatus = promptDraftStatus(item, draft);
        card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));
        const badge = card.querySelector("[data-mvb-prompt-badge]");
        if (badge) {
            badge.dataset.state = currentDraftStatus;
            badge.textContent = promptStatusLabel(currentDraftStatus);
        }
        dirtyLabel.classList.toggle("mvb-prompt-dirty-visible", draft.dirty);
        dirtyLabel.setAttribute("aria-hidden", String(!draft.dirty));
        copyButton.disabled = blocked || !draft.text;
        saveButton.disabled = blocked || !canSavePromptDraft(item, draft);
    });
    finalSection.append(actions);
    return finalSection;
}

function updatePromptFinalSection(root, card, item, draft, blocked) {
    const visible = promptFinalIsVisible(item, draft);
    let finalSection = card.querySelector("[data-mvb-prompt-final]");
    if (!visible) {
        if (finalSection) {
            finalSection.remove();
        }
        return;
    }
    if (!finalSection) {
        finalSection = buildPromptFinalSection(root, card, item, draft, blocked);
        const body = card.querySelector(".mvb-prompt-card-body");
        if (body) {
            body.insertBefore(finalSection, body.querySelector("[data-mvb-prompt-context]") || null);
        }
        return;
    }
    const instruction = finalSection.querySelector(".mvb-prompt-final-instruction");
    if (instruction) {
        instruction.textContent = promptFinalInstruction(item, draft);
    }
    const editor = finalSection.querySelector("[data-mvb-prompt-editor]");
    if (editor) {
        editor.disabled = blocked || item.status === "error";
        editor.readOnly = !promptDraftHasCurrentRelay(item, draft);
        if (document.activeElement !== editor && editor.value !== draft.text) {
            editor.value = draft.text;
        }
    }
    const dirtyLabel = finalSection.querySelector("[data-mvb-prompt-dirty]");
    if (dirtyLabel) {
        dirtyLabel.classList.toggle("mvb-prompt-dirty-visible", draft.dirty);
        dirtyLabel.setAttribute("aria-hidden", String(!draft.dirty));
    }
    const copyButton = finalSection.querySelector("[data-mvb-prompt-copy]");
    if (copyButton) {
        copyButton.disabled = blocked || !draft.text;
    }
    const saveButton = finalSection.querySelector("[data-mvb-prompt-save]");
    if (saveButton) {
        saveButton.disabled = blocked || !canSavePromptDraft(item, draft);
    }
}

function updatePromptContextInPlace(card, item) {
    const contextSection = card.querySelector("[data-mvb-prompt-context]");
    if (!contextSection) {
        return;
    }
    const context = contextSection.querySelector(".mvb-prompt-context");
    if (context) {
        context.textContent = item.source_kind === "lyric" && item.lyric
            ? item.lyric
            : "Instrumental scene";
    }
    const mappingKey = JSON.stringify(item.reference_map?.pictures || []);
    const existingMapping = contextSection.querySelector(".mvb-prompt-mapping");
    if (existingMapping && existingMapping.dataset.mvbMappingKey !== mappingKey) {
        existingMapping.remove();
    }
    const pictures = item.reference_map?.pictures || [];
    if (pictures.length && !contextSection.querySelector(".mvb-prompt-mapping")) {
        const mapping = document.createElement("details");
        mapping.className = "mvb-prompt-mapping mvb-prompt-context-disclosure";
        mapping.dataset.mvbMappingKey = mappingKey;
        mapping.open = Boolean(builderState.promptRelayExpanded[`${promptDraftKey(item.scene_id, item.generation_method)}:mapping`]);
        mapping.addEventListener("toggle", () => {
            builderState.promptRelayExpanded[`${promptDraftKey(item.scene_id, item.generation_method)}:mapping`] = mapping.open;
        });
        const mappingSummary = document.createElement("summary");
        mappingSummary.textContent = "Reference Mapping";
        const mappingList = document.createElement("ul");
        for (const picture of pictures) {
            const entry = document.createElement("li");
            entry.textContent = `${picture.picture_tag} → ${picture.subject_tag} → ${picture.entity_name}`;
            mappingList.append(entry);
        }
        mapping.append(mappingSummary, mappingList);
        contextSection.insertBefore(mapping, contextSection.querySelector(".mvb-relay-disclosure") || null);
    }
    const deterministic = contextSection.querySelector(".mvb-prompt-deterministic");
    if (deterministic) {
        const deterministicText = deterministic.querySelector("pre");
        if (deterministicText) {
            deterministicText.textContent = item.deterministic_prompt || item.error || "No deterministic prompt is available.";
        }
    }
}

function updatePromptCardInPlace(root, card, item, blocked) {
    const draft = promptDraftFor(item.scene_id, item.generation_method, item);
    card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));
    const badge = card.querySelector("[data-mvb-prompt-badge]");
    if (badge) {
        const draftStatus = promptDraftStatus(item, draft);
        badge.dataset.state = draftStatus;
        badge.textContent = promptStatusLabel(draftStatus);
    }
    const generateButton = card.querySelector("[data-mvb-prompt-relay-generate]");
    if (generateButton) {
        generateButton.disabled = blocked || item.status === "error";
    }
    const openGptButton = card.querySelector("[data-mvb-prompt-relay-open-gpt]");
    if (openGptButton) {
        openGptButton.disabled = blocked;
    }
    updatePromptRelayControls(root, item.scene_id, item.generation_method);
    updatePromptFinalSection(root, card, item, draft, blocked);
    updatePromptContextInPlace(card, item);
}

function updatePromptCardsInPlace(root, promptViewport) {
    const list = root.querySelector("[data-mvb-prompt-scenes]");
    const empty = root.querySelector("[data-mvb-prompt-empty]");
    const status = root.querySelector("[data-mvb-prompt-status]");
    if (!list || !empty || !status) {
        return;
    }
    const cards = builderState.promptCards;
    empty.hidden = cards.length > 0;
    empty.textContent = cards.length ? "" : "Build scenes before preparing Prompts.";
    status.textContent = builderState.promptMessage || "";
    status.dataset.state = builderState.promptMessage
        ? builderState.promptMessageState
        : "ready";
    const blocked = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    for (const [index, item] of cards.entries()) {
        const card = list.children[index];
        if (card && card.dataset.mvbPromptCard !== undefined) {
            updatePromptCardInPlace(root, card, item, blocked);
        }
    }
    restorePromptViewport(root, promptViewport);
}

function renderPromptState(root, options = {}) {
    if (!isActive(root)) {
        return;
    }
    const list = root.querySelector("[data-mvb-prompt-scenes]");
    const empty = root.querySelector("[data-mvb-prompt-empty]");
    const status = root.querySelector("[data-mvb-prompt-status]");
    if (!list || !empty || !status) {
        return;
    }
    const promptViewport = options.promptViewport || capturePromptViewport(root, options.anchorSceneId);
    if (builderState.promptListState === "ready" && promptCardsMatchStructure(root)) {
        updatePromptCardsInPlace(root, promptViewport);
        return;
    }
    if (builderState.promptListState === "loading" && promptCardsMatchStructure(root)) {
        // A silent refresh is in flight; keep the current cards on screen
        // instead of flashing a loading placeholder or an empty list.
        empty.hidden = true;
        status.textContent = builderState.promptMessage || "";
        status.dataset.state = builderState.promptMessage
            ? builderState.promptMessageState
            : "ready";
        restorePromptViewport(root, promptViewport);
        return;
    }
    list.replaceChildren();
    if (!builderState.currentProject) {
        empty.hidden = true;
        status.textContent = "";
        restorePromptViewport(root, promptViewport);
        return;
    }
    if (builderState.promptListState === "loading") {
        empty.hidden = false;
        empty.textContent = "Loading prompt previews…";
        status.textContent = "";
        restorePromptViewport(root, promptViewport);
        return;
    }
    if (builderState.promptListState === "error") {
        empty.hidden = false;
        empty.textContent = builderState.promptListMessage || "Prompts could not be loaded.";
        status.textContent = builderState.promptMessage || "";
        status.dataset.state = "error";
        restorePromptViewport(root, promptViewport);
        return;
    }
    const cards = builderState.promptCards;
    empty.hidden = cards.length > 0;
    empty.textContent = cards.length ? "" : "Build scenes before preparing Prompts.";
    status.textContent = builderState.promptMessage || "";
    status.dataset.state = builderState.promptMessage
        ? builderState.promptMessageState
        : "ready";
    const blocked = Boolean(builderState.operation) || builderState.transitioning || builderState.closing;
    for (const item of cards) {
        const key = promptDraftKey(item.scene_id, item.generation_method);
        const draft = promptDraftFor(item.scene_id, item.generation_method, item);
        const card = document.createElement("details");
        card.className = "mvb-prompt-card";
        card.dataset.mvbPromptCard = "";
        card.dataset.mvbPromptSceneId = item.scene_id;
        card.dataset.mvbPromptGenerationMethod = item.generation_method;
        card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));
        card.open = Boolean(builderState.promptExpandedScenes[item.scene_id]);
        card.addEventListener("toggle", () => {
            builderState.promptExpandedScenes[item.scene_id] = card.open;
        });

        const summary = document.createElement("summary");
        summary.className = "mvb-prompt-summary";
        const sceneLabel = document.createElement("span");
        sceneLabel.className = "mvb-prompt-summary-title";
        sceneLabel.textContent = `Scene ${String(item.sequence).padStart(2, "0")}`;
        const timing = document.createElement("span");
        timing.className = "mvb-prompt-summary-timing";
        timing.textContent = `${formatTimelineMs(item.timeline_start_ms)} → ${formatTimelineMs(item.timeline_end_ms)} · ${formatDurationMs(item.exact_duration_ms)}`;
        const method = document.createElement("span");
        method.className = "mvb-prompt-summary-method";
        method.textContent = VISUAL_METHOD_LABELS[item.generation_method] || item.generation_method;
         const badge = document.createElement("span");
         badge.className = "mvb-prompt-status";
         badge.dataset.mvbPromptBadge = "";
         const draftStatus = promptDraftStatus(item, draft);
         badge.dataset.state = draftStatus;
         badge.textContent = promptStatusLabel(draftStatus);
        summary.append(sceneLabel, timing, method, badge);
        card.append(summary);

        const body = document.createElement("div");
        body.className = "mvb-prompt-card-body";
        const relay = promptRelayFor(item.scene_id, item.generation_method);
        const relayDetails = document.createElement("section");
        relayDetails.className = "mvb-prompt-relay mvb-prompt-workflow-section";
        const relayBody = document.createElement("div");
        relayBody.className = "mvb-prompt-relay-body";
        const relayInstruction = document.createElement("p");
        relayInstruction.className = "mvb-prompt-instruction";
        relayInstruction.textContent = "Generate and copy the scene request, open the Prompt Director, then paste and apply its response.";
        relayBody.append(relayInstruction);
        const relayResponseField = document.createElement("label");
        relayResponseField.className = "mvb-field mvb-prompt-relay-field";
        const relayResponseLabel = document.createElement("span");
        relayResponseLabel.textContent = "Paste GPT Response";
        const relayResponseInput = document.createElement("textarea");
        relayResponseInput.rows = 6;
        relayResponseInput.spellcheck = false;
        relayResponseInput.placeholder = "Paste the strict response JSON from Vesper H3 Prompt Director.";
        relayResponseInput.dataset.mvbPromptRelayResponse = "";
        relayResponseInput.value = relay.responseText;
        relayResponseInput.addEventListener("input", () => {
            relay.responseText = relayResponseInput.value;
            relay.responseMessage = "";
            relay.responseMessageState = "ready";
            updatePromptRelayControls(root, item.scene_id, item.generation_method);
        });
        relayResponseField.append(relayResponseLabel, relayResponseInput);
        relayBody.append(relayResponseField);
        const relayActions = document.createElement("div");
        relayActions.className = "mvb-prompt-actions mvb-prompt-relay-actions";
        const generateRelayButton = makeVisualButton("Generate GPT Request", "mvb-button-secondary", () => void generatePromptRelayRequest(root, item.scene_id, item.generation_method), blocked || item.status === "error");
        generateRelayButton.dataset.mvbPromptRelayGenerate = "";
        const copyRelayButton = makeVisualButton("Copy Request JSON", "mvb-button-secondary", () => void copyPromptRelayRequest(root, item.scene_id, item.generation_method), blocked || !canCopyPromptRelayRequest(relay, item));
        copyRelayButton.dataset.mvbPromptRelayCopy = "";
        const openGptButton = makeVisualButton("Open GPT", "mvb-button-secondary", () => void openDedicatedGpt(root, "prompts"), blocked);
        openGptButton.dataset.mvbPromptRelayOpenGpt = "";
        openGptButton.setAttribute("aria-label", "Open Vesper H3 Prompt Director");
        openGptButton.title = "Vesper H3 Prompt Director";
        const applyRelayButton = makeVisualButton("Apply GPT Response", "mvb-button-primary", () => void applyPromptRelayResponse(root, item.scene_id, item.generation_method), blocked || !canApplyPromptRelayResponse(relay, item));
        applyRelayButton.dataset.mvbPromptRelayApply = "";
        relayActions.append(generateRelayButton, copyRelayButton, openGptButton, applyRelayButton);
        relayBody.append(relayActions);
        const relayStatus = document.createElement("span");
        relayStatus.className = "mvb-prompt-relay-status";
        relayStatus.dataset.mvbPromptRelayStatus = "";
        relayStatus.setAttribute("aria-live", "polite");
        relayBody.append(relayStatus);
         relayDetails.append(relayBody);
         body.append(relayDetails);

        if (promptFinalIsVisible(item, draft)) {
            const finalSection = document.createElement("section");
            finalSection.className = "mvb-prompt-final mvb-prompt-workflow-section";
            finalSection.dataset.mvbPromptFinal = "";
            const finalHeading = document.createElement("h4");
            finalHeading.textContent = "Final Prompt";
            finalSection.append(finalHeading);
            const finalInstruction = document.createElement("p");
            finalInstruction.className = "mvb-prompt-instruction mvb-prompt-final-instruction";
            finalInstruction.textContent = promptFinalInstruction(item, draft);
            finalSection.append(finalInstruction);
            const editorField = document.createElement("label");
            editorField.className = "mvb-field mvb-prompt-editor-field";
            const editorLabel = document.createElement("span");
            editorLabel.textContent = "Final Prompt";
            const editor = document.createElement("textarea");
            editor.rows = 12;
            editor.maxLength = 50_000;
            editor.spellcheck = false;
            editor.value = draft.text;
            editor.setAttribute("aria-label", `Final Prompt for scene ${item.sequence}`);
            editor.dataset.mvbPromptEditor = "";
            editor.dataset.mvbPromptSceneId = item.scene_id;
            editor.dataset.mvbPromptGenerationMethod = item.generation_method;
            editor.disabled = blocked || item.status === "error";
            editor.readOnly = !promptDraftHasCurrentRelay(item, draft);
            editorField.append(editorLabel, editor);
            finalSection.append(editorField);
            const actions = document.createElement("div");
            actions.className = "mvb-prompt-actions";
            const dirtyLabel = document.createElement("span");
            dirtyLabel.className = "mvb-prompt-dirty";
            dirtyLabel.dataset.mvbPromptDirty = "";
            dirtyLabel.textContent = "UNSAVED";
            dirtyLabel.classList.toggle("mvb-prompt-dirty-visible", draft.dirty);
            dirtyLabel.setAttribute("aria-hidden", String(!draft.dirty));
            actions.append(dirtyLabel);
            const copyButton = makeVisualButton("Copy", "mvb-button-secondary", () => void copyPrompt(root, item.scene_id, item.generation_method), blocked || !draft.text);
            copyButton.dataset.mvbPromptCopy = "";
            const saveButton = makeVisualButton("Save Prompt", "mvb-button-primary", () => void savePrompt(root, item.scene_id, item.generation_method), blocked || !canSavePromptDraft(item, draft));
            saveButton.dataset.mvbPromptSave = "";
            actions.append(copyButton, saveButton);
            editor.addEventListener("input", () => {
                draft.text = editor.value;
                draft.dirty = promptDraftIsDirty(item, draft);
                builderState.promptMessage = "";
                builderState.promptMessageState = "ready";
                const currentDraftStatus = promptDraftStatus(item, draft);
                card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));
                badge.dataset.state = currentDraftStatus;
                badge.textContent = promptStatusLabel(currentDraftStatus);
                dirtyLabel.classList.toggle("mvb-prompt-dirty-visible", draft.dirty);
                dirtyLabel.setAttribute("aria-hidden", String(!draft.dirty));
                copyButton.disabled = blocked || !draft.text;
                saveButton.disabled = blocked || !canSavePromptDraft(item, draft);
            });
            finalSection.append(actions);
            body.append(finalSection);
        }

        const promptContextSection = document.createElement("section");
        promptContextSection.className = "mvb-prompt-support mvb-prompt-workflow-section";
        promptContextSection.dataset.mvbPromptContext = "";
        const promptContextHeading = document.createElement("h4");
        promptContextHeading.textContent = "Prompt Context";
        promptContextSection.append(promptContextHeading);
        const context = document.createElement("p");
        context.className = "mvb-prompt-context";
        context.textContent = item.source_kind === "lyric" && item.lyric
            ? item.lyric
            : "Instrumental scene";
        promptContextSection.append(context);
        if (item.generation_method === "reference2video" && item.reference_map?.pictures?.length) {
            const mapping = document.createElement("details");
            mapping.className = "mvb-prompt-mapping mvb-prompt-context-disclosure";
            mapping.open = Boolean(builderState.promptRelayExpanded[`${key}:mapping`]);
            mapping.addEventListener("toggle", () => {
                builderState.promptRelayExpanded[`${key}:mapping`] = mapping.open;
            });
            const mappingSummary = document.createElement("summary");
            mappingSummary.textContent = "Reference Mapping";
            const mappingList = document.createElement("ul");
            for (const picture of item.reference_map.pictures) {
                const entry = document.createElement("li");
                entry.textContent = `${picture.picture_tag} → ${picture.subject_tag} → ${picture.entity_name}`;
                mappingList.append(entry);
            }
            mapping.append(mappingSummary, mappingList);
            promptContextSection.append(mapping);
        }
        const relayRequestDisclosure = document.createElement("details");
        relayRequestDisclosure.className = "mvb-relay-disclosure mvb-prompt-context-disclosure";
        relayRequestDisclosure.open = Boolean(builderState.promptRelayExpanded[`${key}:request`]);
        relayRequestDisclosure.addEventListener("toggle", () => {
            builderState.promptRelayExpanded[`${key}:request`] = relayRequestDisclosure.open;
        });
        const relayRequestSummary = document.createElement("summary");
        relayRequestSummary.textContent = "View Request JSON";
        const relayRequestField = document.createElement("label");
        relayRequestField.className = "mvb-field mvb-prompt-relay-field";
        const relayRequestLabel = document.createElement("span");
        relayRequestLabel.textContent = "GPT Request JSON";
        const relayRequestInput = document.createElement("textarea");
        relayRequestInput.rows = 6;
        relayRequestInput.readOnly = true;
        relayRequestInput.spellcheck = false;
        relayRequestInput.dataset.mvbPromptRelayRequest = "";
        relayRequestInput.value = promptRelayRequestJson(relay.request) || "Generate a request to preview its JSON.";
        relayRequestField.append(relayRequestLabel, relayRequestInput);
        relayRequestDisclosure.append(relayRequestSummary, relayRequestField);
        promptContextSection.append(relayRequestDisclosure);
        const deterministic = document.createElement("details");
        deterministic.className = "mvb-prompt-deterministic mvb-prompt-context-disclosure";
        const deterministicSummary = document.createElement("summary");
        deterministicSummary.textContent = "View Deterministic Prompt";
        const deterministicText = document.createElement("pre");
        deterministicText.textContent = item.deterministic_prompt || item.error || "No deterministic prompt is available.";
        deterministic.append(deterministicSummary, deterministicText);
        promptContextSection.append(deterministic);
        body.append(promptContextSection);
        card.append(body);
        list.append(card);
        updatePromptRelayControls(root, item.scene_id, item.generation_method);
    }
    restorePromptViewport(root, promptViewport);
}

async function loadPromptCards(root, silent = false, options = {}) {
    if (!isActive(root) || !builderState.currentProject) {
        return false;
    }
    const promptViewport = options.promptViewport || capturePromptViewport(root, options.anchorSceneId);
    const projectId = builderState.currentProject.project_id;
    builderState.promptListState = "loading";
    if (!silent) {
        builderState.promptMessage = "";
    }
    renderPromptState(root, { promptViewport });
    try {
        const payload = await fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}/prompts`, {
            method: "GET",
            cache: "no-store",
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        if (!payload || payload.project_id !== projectId || !Array.isArray(payload.scenes)) {
            throw new Error("Prompt list response was invalid.");
        }
        // Refresh matching card items in place so DOM listeners that reference
        // an item keep seeing authoritative data across silent refreshes.
        const previousItems = new Map(
            builderState.promptCards.map((entry) => [promptDraftKey(entry.scene_id, entry.generation_method), entry]),
        );
        builderState.promptCards = payload.scenes.map((entry) => {
            const previous = previousItems.get(promptDraftKey(entry.scene_id, entry.generation_method));
            if (!previous) {
                return entry;
            }
            for (const staleKey of Object.keys(previous)) {
                if (!(staleKey in entry)) {
                    delete previous[staleKey];
                }
            }
            Object.assign(previous, entry);
            return previous;
        });
        for (const item of builderState.promptCards) {
            const key = promptDraftKey(item.scene_id, item.generation_method);
            if (builderState.promptDrafts[key] && !builderState.promptDrafts[key].dirty) {
                builderState.promptDrafts[key].text = item.saved_final_prompt || "";
                builderState.promptDrafts[key].sourceFingerprint = item.source_fingerprint;
                builderState.promptDrafts[key].relayFingerprint = item.saved_relay_fingerprint || "";
            }
        }
        builderState.promptListState = "ready";
        builderState.promptListMessage = "";
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        console.error("[Music Video Builder] Prompt list request failed.", error);
        builderState.promptListState = "error";
        builderState.promptListMessage = error instanceof Error ? error.message : "Prompts could not be loaded.";
        return false;
    } finally {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            renderPromptState(root, { promptViewport });
        }
    }
}

function renderPreflightPath(projectId, forceRefresh = false) {
    const suffix = forceRefresh ? "?force=1" : "";
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/preflight${suffix}`;
}

function renderScenePreparePath(projectId, sceneId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/scenes/${encodeURIComponent(sceneId)}/prepare`;
}

function renderJobsPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/jobs`;
}

function renderSceneSubmitPath(projectId, sceneId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/scenes/${encodeURIComponent(sceneId)}/submit`;
}

function renderJobCancelPath(projectId, jobId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/jobs/${encodeURIComponent(jobId)}/cancel`;
}

function renderJobRetryPath(projectId, jobId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/jobs/${encodeURIComponent(jobId)}/retry`;
}

function renderJobFinalizePath(projectId, jobId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/jobs/${encodeURIComponent(jobId)}/finalize`;
}

function productionStatusPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/production/status`;
}

function productionSetMethodPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/production/upscale-method`;
}

function scenePostprocessStartPath(projectId, sceneId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/render/postprocess/start`;
}

function scenePostprocessCancelPath(projectId, sceneId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/render/postprocess/cancel`;
}

function scenePostprocessRetryPath(projectId, sceneId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/render/postprocess/retry`;
}

function renderBatchPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch`;
}

function renderBatchPreviewPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/preview`;
}

function renderBatchStartPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/start`;
}

function renderBatchPausePath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/pause`;
}

function renderBatchResumePath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/resume`;
}

function renderBatchEndPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/end`;
}

function renderBatchRetryFailedPath(projectId) {
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/render/batch/retry-failed`;
}

const BATCH_ELIGIBLE_ACTIONS = ["POSTPROCESS_ONLY", "FINALIZE_AND_POSTPROCESS", "FINALIZE_RAW", "RENDER_PREPARED", "PREPARE_AND_RENDER"];

function batchIsExecuting(batch) {
    return Boolean(batch) && ["RUNNING", "PAUSE_REQUESTED"].includes(batch.state);
}

function batchSelectionBody(selection) {
    if (!selection) {
        return {};
    }
    return { scene_ids: [...selection.selected] };
}

function renderJobLabel(state) {
    return {
        READY_TO_SUBMIT: "READY TO SUBMIT",
        SUBMITTING: "SUBMITTING",
        QUEUED: "QUEUED",
        RUNNING: "RUNNING",
        SUCCEEDED: "SUCCEEDED",
        FAILED: "FAILED",
        CANCEL_REQUESTED: "CANCELLATION REQUESTED",
        CANCELLED: "CANCELLED",
        INTERRUPTED: "INTERRUPTED",
        UNKNOWN: "RECONCILIATION REQUIRED",
        ORPHANED: "ORPHANED",
    }[state] || "NOT QUEUED";
}

function renderJobIsActive(job) {
    return ["READY_TO_SUBMIT", "SUBMITTING", "QUEUED", "RUNNING", "CANCEL_REQUESTED", "UNKNOWN"].includes(job?.state);
}

function renderFinalizationLabel(state) {
    return {
        NOT_AVAILABLE: "NOT AVAILABLE",
        RAW_READY: "RAW READY",
        FINALIZING: "FINALIZING",
        FINALIZED: "READY",
        STALE: "STALE",
        FAILED: "FAILED",
    }[state] || "NOT AVAILABLE";
}

function renderCompletionProjection(job) {
    return job?.completion && typeof job.completion === "object" ? job.completion : null;
}

function renderCompletionProjectionIsValid(job) {
    const completion = renderCompletionProjection(job);
    const rawOutput = completion?.raw_output;
    return Boolean(completion)
        && typeof completion.state === "string"
        && typeof completion.finalization_allowed === "boolean"
        && rawOutput
        && typeof rawOutput === "object"
        && typeof rawOutput.state === "string"
        && typeof rawOutput.usable === "boolean";
}

function renderRawOutputProjection(job) {
    const completion = renderCompletionProjection(job);
    if (completion?.raw_output && typeof completion.raw_output === "object") {
        return completion.raw_output;
    }
    const finalization = job?.finalization;
    return finalization?.output_association && typeof finalization.output_association === "object"
        ? finalization.output_association
        : { state: "UNKNOWN", usable: false, failure: null };
}

function renderRawOutputAssociationState(job) {
    const state = renderRawOutputProjection(job).state;
    return typeof state === "string" ? state : "UNKNOWN";
}

function renderRawOutputAssociationLabel(state) {
    return {
        AVAILABLE: "AVAILABLE",
        MISSING: "MISSING",
        STALE: "STALE",
        UNSAFE: "UNSAFE",
        FOREIGN: "FOREIGN",
        AMBIGUOUS: "AMBIGUOUS",
        UNKNOWN: "UNKNOWN",
    }[state] || "UNKNOWN";
}

function renderFinalizationState(job) {
    return job?.finalization?.state || "NOT_AVAILABLE";
}

function renderFinalizationEligible(job) {
    const completion = renderCompletionProjection(job);
    const state = renderFinalizationState(job);
    return job?.state === "SUCCEEDED"
        && completion?.finalization_allowed === true
        && job?.finalization?.eligible === true
        && ["RAW_READY", "STALE", "FAILED"].includes(state);
}

function renderProductionSceneLabel(scene, job, state) {
    const upscaleMethod = state.currentProject?.production?.upscale_method || "none";
    const finalizationState = renderFinalizationState(job);
    const jobCurrent = Boolean(job)
        && scene.preparation_status === "current"
        && job.preparation_fingerprint === scene.preparation_fingerprint;
    const finalReady = job?.state === "SUCCEEDED"
        && renderCompletionProjection(job)?.finalization_allowed === true
        && finalizationState === "FINALIZED"
        && jobCurrent;

    if (upscaleMethod === "none") {
        return finalReady ? "READY" : "NOT READY";
    }

    const prodCap = state.renderProductionStatus?.capabilities?.[upscaleMethod];
    const prodSummary = state.renderProductionStatus?.scenes?.find((s) => s.scene_id === scene.scene_id);
    const pjob = state.postprocessJobs?.[scene.scene_id] || prodSummary?.active_job || prodSummary?.last_job;
    if (pjob) {
        if (["SUBMITTING", "SUBMITTED", "ACTIVE"].includes(pjob.state)) {
            return "UPSCALE ACTIVE";
        }
        if (pjob.state === "SUCCEEDED" && prodSummary?.status === "ready") {
            return "READY";
        }
        if (pjob.state === "FAILED") {
            return "FAILED";
        }
    }
    if (prodSummary?.status === "ready") {
        return "READY";
    }
    if (prodCap?.state === "UNAVAILABLE") {
        return "UNAVAILABLE";
    }
    return finalReady ? "NEEDS UPSCALE" : "NOT READY";
}

function clearRenderJobPoll() {
    if (!builderState || builderState.renderJobsPollTimer === null) {
        return;
    }
    window.clearTimeout(builderState.renderJobsPollTimer);
    builderState.renderJobsPollTimer = null;
}

function scheduleRenderJobPoll(root) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderJobsPollTimer !== null) {
        return;
    }
    const jobs = Object.values(builderState.renderJobs || {});
    if (!jobs.some(renderJobIsActive)) {
        builderState.renderJobsPollDelay = 1000;
        return;
    }
    const delay = builderState.renderJobsPollDelay || 1000;
    builderState.renderJobsPollTimer = window.setTimeout(() => {
        builderState.renderJobsPollTimer = null;
        void loadRenderJobs(root, true);
    }, delay);
}

function renderDiagnosticText(entry) {
    const raw = entry && typeof entry.message === "string" ? entry.message.trim() : "";
    if (!raw || (raw.startsWith("{") && raw.endsWith("}")) || raw.startsWith("[") || raw.includes("Traceback")) {
        return "Render inputs require attention.";
    }
    if (raw.includes("status_str")
        || raw.includes("node_type")
        || raw.includes("executed")
        || /['"]?\w*id['"]?\s*:\s*['"]?[a-f0-9-]{8,}['"]?/i.test(raw)
        || /['"]node[_\s]?id['"]\s*:/i.test(raw)
        || /['"]nodes['"]\s*:\s*\[/i.test(raw)
        || /['"]timestamp['"]\s*:/i.test(raw)) {
        return "Render inputs require attention.";
    }
    return raw;
}

function renderJobShellKey(scene, prompt, job, state) {
    const durableJob = job ? {
        ...job,
        progress: null,
        telemetry: null,
        updated_at: null,
        last_reconciled_at: null,
        last_poll_error: null,
    } : null;
    return JSON.stringify({
        scene,
        prompt,
        job: durableJob,
        preparing: state.renderPreparingSceneId,
        submitting: state.renderSubmittingSceneId,
        action: state.renderJobActionId,
        finalizing: state.renderFinalizingJobId,
        operation: state.operation,
        transitioning: state.transitioning,
        closing: state.closing,
        batchExecuting: batchIsExecuting(state.renderBatch),
        batchBusy: state.renderBatchBusy,
        selecting: Boolean(state.renderBatchSelection),
        selected: Boolean(state.renderBatchSelection?.selected?.has(scene.scene_id)),
    });
}

function finiteTelemetryNumber(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function renderTelemetryPresentation(job) {
    const active = Boolean(job && renderJobIsActive(job) && job.state !== "UNKNOWN");
    const progress = job?.progress;
    const telemetry = job?.telemetry;
    const telemetryCurrent = telemetry?.telemetry_stale !== true;
    const progressValue = finiteTelemetryNumber(progress?.raw_value)
        ?? finiteTelemetryNumber(telemetry?.progress_value);
    const progressMax = finiteTelemetryNumber(progress?.raw_max)
        ?? finiteTelemetryNumber(telemetry?.progress_max);
    const determinate = Boolean(
        active
        && job?.state === "RUNNING"
        && telemetryCurrent
        && progress?.kind === "telemetry"
        && progressValue !== null
        && progressMax !== null
        && progressMax > 0,
    );
    const percent = determinate
        ? Math.round(Math.max(0, Math.min(1, progressValue / progressMax)) * 100)
        : null;
    const stageLabel = telemetryCurrent && typeof progress?.stage === "string" && progress.stage
        ? progress.stage
        : telemetryCurrent && typeof telemetry?.current_stage === "string" && telemetry.current_stage
            ? telemetry.current_stage
            : telemetryCurrent && typeof telemetry?.stage === "string" && telemetry.stage
                ? telemetry.stage
                : job?.state === "QUEUED"
                    ? "Queued"
                    : "Rendering";
    return {
        active,
        mode: percent === null ? "indeterminate" : "determinate",
        percent,
        stageLabel,
    };
}

function updateRenderJobTelemetry(card, job) {
    const panel = card?.querySelector("[data-mvb-render-telemetry]");
    if (!panel) {
        return;
    }
    const status = panel.querySelector("[data-mvb-render-telemetry-status]");
    const stage = panel.querySelector("[data-mvb-render-telemetry-stage]");
    const bar = panel.querySelector("[data-mvb-render-telemetry-bar]");
    const track = panel.querySelector("[data-mvb-render-telemetry-track]");
    const { active, mode, percent, stageLabel } = renderTelemetryPresentation(job);
    panel.hidden = !active;
    panel.dataset.state = active ? mode : "idle";
    if (!active) {
        if (status) {
            status.textContent = "";
        }
        if (stage) {
            stage.textContent = "";
        }
        if (track) {
            track.dataset.mode = "idle";
            track.classList.remove("mvb-render-telemetry-track-indeterminate", "mvb-render-telemetry-track-determinate");
            track.removeAttribute("aria-valuenow");
            track.removeAttribute("aria-valuetext");
        }
        if (bar) {
            bar.style.removeProperty("width");
        }
        return;
    }
    if (status) {
        status.textContent = renderJobLabel(job?.state);
    }
    if (stage) {
        stage.textContent = percent === null ? stageLabel : `${stageLabel} · ${percent}%`;
    }
    if (bar) {
        if (percent !== null) {
            bar.style.width = `${percent}%`;
        } else {
            bar.style.removeProperty("width");
        }
    }
    if (track) {
        track.dataset.mode = mode;
        track.classList.toggle("mvb-render-telemetry-track-indeterminate", mode === "indeterminate");
        track.classList.toggle("mvb-render-telemetry-track-determinate", mode === "determinate");
        if (percent !== null) {
            track.setAttribute("aria-valuenow", String(percent));
            track.setAttribute("aria-valuetext", `${stageLabel}, ${percent}% stage progress`);
        } else {
            track.removeAttribute("aria-valuenow");
            track.setAttribute("aria-valuetext", `${stageLabel}, in progress`);
        }
    }
}

function createRenderJobTelemetry() {
    const panel = document.createElement("div");
    panel.className = "mvb-render-telemetry";
    panel.dataset.mvbRenderTelemetry = "true";
    const heading = document.createElement("div");
    heading.className = "mvb-render-telemetry-heading";
    const status = document.createElement("strong");
    status.dataset.mvbRenderTelemetryStatus = "true";
    heading.append(status);
    const stage = document.createElement("span");
    stage.className = "mvb-render-telemetry-stage";
    stage.dataset.mvbRenderTelemetryStage = "true";
    const track = document.createElement("div");
    track.className = "mvb-render-telemetry-track";
    track.dataset.mvbRenderTelemetryTrack = "true";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", "Current render stage progress");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    const bar = document.createElement("div");
    bar.className = "mvb-render-telemetry-bar";
    bar.dataset.mvbRenderTelemetryBar = "true";
    bar.setAttribute("aria-hidden", "true");
    track.append(bar);
    panel.append(heading, stage, track);
    return panel;
}

function renderPromptStateForScene(scene) {
    const item = promptItemFor(scene.scene_id, scene.generation_method);
    if (!item) {
        return {
            status: scene.prompt_status || "error",
            ready: Boolean(scene.prompt_ready),
            diagnostics: [],
        };
    }
    const draft = promptDraftFor(scene.scene_id, scene.generation_method, item);
    const status = promptDraftStatus(item, draft);
    return {
        status,
        ready: status === "current" && promptDraftReadyForRender(item, draft),
        diagnostics: status === "unsaved"
            ? [{ code: "PROMPT_UNSAVED", message: "Prompt has unsaved edits. Save the current Final Prompt before preparation." }]
            : status === "stale" && scene.prompt_status !== "stale"
                ? [{ code: "PROMPT_STALE", message: "Prompt is stale. Generate and save a fresh Prompt Director result." }]
                : [],
    };
}

function renderRenderState(root) {
    if (!isActive(root)) {
        return;
    }
    const section = root.querySelector("[data-mvb-render]");
    const status = root.querySelector("[data-mvb-render-status]");
    const list = root.querySelector("[data-mvb-render-scenes]");
    const empty = root.querySelector("[data-mvb-render-empty]");
    const refresh = root.querySelector("[data-mvb-render-refresh]");
    const rescan = root.querySelector("[data-mvb-render-rescan]");
    const batchAllButton = root.querySelector("[data-mvb-batch-all]");
    const batchSelectButton = root.querySelector("[data-mvb-batch-select]");
    const upscaleSelect = root.querySelector("[data-mvb-render-upscale-select]");
    const upscaleNote = root.querySelector("[data-mvb-render-upscale-note]");
    if (!section || !status || !list || !empty || !refresh || !rescan) {
        return;
    }

    const state = builderState;
    const preflight = state.renderPreflight;
    const batchExecuting = batchIsExecuting(state.renderBatch);
    const selectionActive = Boolean(state.renderBatchSelection);
    const upscaleMethod = state.currentProject?.production?.upscale_method || "none";
    if (upscaleSelect) {
        upscaleSelect.value = upscaleMethod;
        upscaleSelect.disabled = !state.currentProject
            || state.renderState === "loading"
            || Boolean(state.operation)
            || state.transitioning
            || state.closing
            || batchExecuting
            || state.renderBatchBusy;
    }
    const prodCap = state.renderProductionStatus?.capabilities?.[upscaleMethod];
    if (upscaleNote) {
        if (upscaleMethod === "none") {
            upscaleNote.textContent = "";
            upscaleNote.removeAttribute("data-state");
        } else if (prodCap?.state === "UNAVAILABLE") {
            upscaleNote.textContent = upscaleMethod === "rtx_vsr_fast" ? "RTX VSR unavailable on this runtime" : "SeedVR2 unavailable on this runtime";
            upscaleNote.dataset.state = "error";
        } else if (prodCap?.state === "UNQUALIFIED") {
            upscaleNote.textContent = "Qualification pending";
            upscaleNote.dataset.state = "warning";
        } else {
            upscaleNote.textContent = "";
            upscaleNote.removeAttribute("data-state");
        }
    }
    refresh.disabled = !state.currentProject || state.renderState === "loading" || Boolean(state.operation) || state.transitioning || state.closing;
    rescan.disabled = !state.currentProject || state.renderState === "loading" || Boolean(state.operation) || state.transitioning || state.closing;
    if (batchAllButton) {
        batchAllButton.disabled = !state.currentProject
            || state.renderState === "loading"
            || Boolean(state.operation)
            || state.transitioning
            || state.closing
            || batchExecuting
            || selectionActive
            || state.renderBatchBusy;
        batchAllButton.removeAttribute("title");
    }
    if (batchSelectButton) {
        batchSelectButton.disabled = !state.currentProject
            || state.renderState === "loading"
            || Boolean(state.operation)
            || state.transitioning
            || state.closing
            || batchExecuting
            || selectionActive
            || state.renderBatchBusy;
        batchSelectButton.removeAttribute("title");
    }
    if (!state.currentProject) {
        status.textContent = "Open a project to inspect render readiness.";
        status.dataset.state = "empty";
        empty.hidden = false;
        list.replaceChildren();
        renderSelectionBar(root);
        renderBatchPanel(root);
        return;
    }
    if (state.renderState === "loading") {
        status.textContent = state.renderLoadingMode === "runtime"
            ? "Inspecting runtime requirements…"
            : "Refreshing renders…";
        status.dataset.state = "working";
        empty.hidden = true;
        renderSelectionBar(root);
        renderBatchPanel(root);
        return;
    }
    if (state.renderState === "error") {
        status.textContent = state.renderMessage || "Render preflight could not be loaded.";
        status.dataset.state = "error";
        empty.hidden = true;
        list.replaceChildren();
        renderSelectionBar(root);
        renderBatchPanel(root);
        return;
    }
    if (!preflight || !Array.isArray(preflight.scenes)) {
        status.textContent = "Refresh Preflight to inspect scenes.";
        status.dataset.state = "empty";
        empty.hidden = false;
        list.replaceChildren();
        renderSelectionBar(root);
        renderBatchPanel(root);
        return;
    }

    const notReadyCount = Math.max(0, preflight.scene_count - preflight.preparation_ready_count);
    const activeJobCount = Object.values(state.renderJobs || {}).filter(renderJobIsActive).length;
    status.textContent = state.renderMessage
        || state.renderJobsMessage
        || `${preflight.preparation_ready_count} ready · ${notReadyCount} not ready${activeJobCount ? ` · ${activeJobCount} active` : ""}`;
    status.dataset.state = state.renderMessage
        ? (state.renderMessageState || "neutral")
        : state.renderJobsMessage
            ? "warning"
            : "neutral";
    empty.hidden = preflight.scenes.length !== 0;
    const content = root.querySelector(".mvb-content");
    const contentScrollTop = content?.scrollTop ?? 0;
    const existingCards = new Map(
        [...list.children]
            .filter((child) => child.dataset.mvbRenderCard)
            .map((child) => [child.dataset.mvbRenderCard, child]),
    );
    const activeSceneIds = new Set();

    for (const scene of preflight.scenes) {
        activeSceneIds.add(scene.scene_id);
        const prompt = renderPromptStateForScene(scene);
        const job = state.renderJobs?.[scene.scene_id] || null;
        const jobCurrent = Boolean(job)
            && scene.preparation_status === "current"
            && job.preparation_fingerprint === scene.preparation_fingerprint;
        const completion = renderCompletionProjection(job);
        const rawAssociation = renderRawOutputProjection(job);
        const rawAssociationState = renderRawOutputAssociationState(job);
        const rawOutputLabel = completion?.state === "SUCCEEDED" && rawAssociation.usable === true
            ? (jobCurrent ? "CURRENT" : "HISTORICAL")
            : renderRawOutputAssociationLabel(rawAssociationState);
        const blockers = [...(Array.isArray(scene.blockers) ? scene.blockers : []), ...prompt.diagnostics];
        const ready = Boolean(scene.preparation_ready) && prompt.ready;
        const executionEligible = scene.execution_eligibility?.eligible === true;
        const timingPlan = scene.timing_plan;
        const timingLabel = timingPlan && Number.isInteger(timingPlan.generated_frame_count)
            && Number.isFinite(Number(timingPlan.generated_duration_ms))
            ? `${timingPlan.generated_frame_count} frames · ${(Number(timingPlan.generated_duration_ms) / 1000).toFixed(3)} s`
            : "NOT PLANNED";
        const trimLabel = timingPlan && Number.isFinite(Number(timingPlan.trim_duration_ms))
            ? timingPlan.trim_required === true
                ? `Required · ${Math.round(Number(timingPlan.trim_duration_ms))} ms`
                : "None"
            : "NOT PLANNED";
        const card = existingCards.get(scene.scene_id) || document.createElement("article");
        const shellKey = renderJobShellKey(scene, prompt, job, state);
        if (card.dataset.mvbRenderShellKey === shellKey && card.querySelector("[data-mvb-render-telemetry]")) {
            updateRenderJobTelemetry(card, job);
            list.append(card);
            continue;
        }
        const selection = state.renderBatchSelection;
        const selectionAction = selection ? selection.actions.get(scene.scene_id) : null;
        const selectionEligible = Boolean(selection) && BATCH_ELIGIBLE_ACTIONS.includes(selectionAction);
        const isSelected = Boolean(selection) && selection.selected.has(scene.scene_id);
        card.className = "mvb-render-card";
        card.dataset.mvbRenderCard = scene.scene_id;
        card.dataset.mvbRenderShellKey = shellKey;
        card.dataset.state = ready ? "ready" : "blocked";

        const heading = document.createElement("div");
        heading.className = "mvb-render-card-heading";
        const title = document.createElement("div");
        title.className = "mvb-render-card-title";
        const name = document.createElement("h3");
        name.textContent = `Scene ${scene.sequence}`;
        title.append(name);
        const stateBadge = document.createElement("span");
        stateBadge.className = "mvb-render-readiness";
        stateBadge.dataset.state = job && renderJobIsActive(job) ? "active" : ready ? "ready" : "blocked";
        stateBadge.textContent = job && renderJobIsActive(job) ? renderJobLabel(job.state) : ready ? "INPUTS READY" : "BLOCKED";
        heading.append(title, stateBadge);

        const facts = document.createElement("div");
        facts.className = "mvb-render-facts";
        for (const [label, value] of [
            ["Duration", `${scene.duration_ms} ms`],
            ["H3 plan", timingLabel],
            ["Trim", trimLabel],
            ["Method", scene.generation_method === "keyframe_i2v" ? "Keyframe / I2V" : "Reference-to-Video"],
            ["Visuals", scene.visuals_ready ? "READY" : "BLOCKED"],
            ["Prompt", prompt.status === "current" ? "CURRENT" : promptStatusLabel(prompt.status)],
            ["Requirements", scene.requirements_ready ? "READY" : "BLOCKED"],
            ["Preparation", scene.preparation_status === "current" ? "PREPARED" : scene.preparation_status === "stale" ? "RE-PREPARE" : ready ? "READY" : "BLOCKED"],
            ["Render job", job ? `${renderJobLabel(job.state)}${job.state === "SUCCEEDED" && !jobCurrent ? " · HISTORICAL" : ""}` : "NOT QUEUED"],
            ["Raw H3", rawOutputLabel],
            ["Final scene", renderFinalizationLabel(renderFinalizationState(job))],
            ["Production scene", renderProductionSceneLabel(scene, job, state)],
        ]) {
            const fact = document.createElement("div");
            fact.className = "mvb-render-fact";
            const factLabel = document.createElement("span");
            factLabel.textContent = label;
            const factValue = document.createElement("strong");
            factValue.textContent = value;
            fact.append(factLabel, factValue);
            facts.append(fact);
        }

        const detail = document.createElement("p");
        detail.className = "mvb-render-detail";
        const finalizationState = renderFinalizationState(job);
        const prodSummary = state.renderProductionStatus?.scenes?.find((s) => s.scene_id === scene.scene_id);
        const pjob = state.postprocessJobs?.[scene.scene_id] || prodSummary?.active_job || prodSummary?.last_job;
        if (pjob && ["SUBMITTING", "SUBMITTED", "ACTIVE"].includes(pjob.state)) {
            const pStage = pjob.progress?.stage || pjob.telemetry?.stage || pjob.telemetry?.current_stage || "Upscaling video…";
            detail.textContent = pStage;
            detail.hidden = false;
        } else if (pjob?.state === "FAILED" && upscaleMethod !== "none") {
            detail.textContent = pjob.failure?.message || "Production upscale failed; retry is available.";
            detail.hidden = false;
        } else if (job?.state === "SUCCEEDED" && finalizationState === "FINALIZED") {
            detail.textContent = "Final scene ready.";
        } else if (job?.state === "SUCCEEDED" && finalizationState === "FINALIZING") {
            detail.textContent = "Finalizing the raw H3 output with authoritative scene audio…";
        } else if (job?.state === "SUCCEEDED" && finalizationState === "FAILED") {
            detail.textContent = job.finalization?.failure?.message || "Final scene finalization failed; retry is available if inputs remain current.";
        } else if (job?.state === "SUCCEEDED" && finalizationState === "STALE") {
            detail.textContent = "Historical raw render retained; final scene output is stale for current scene inputs.";
        } else if (job?.state === "SUCCEEDED" && finalizationState === "RAW_READY") {
            detail.textContent = "Raw H3 output ready for finalization.";
        } else if (job?.state === "SUCCEEDED" && completion?.finalization_allowed !== true) {
            const failureMessage = rawAssociation.failure?.message || "Raw H3 output association is not usable.";
            detail.textContent = `ComfyUI reported success, but raw H3 association is ${renderRawOutputAssociationLabel(rawAssociationState).toLowerCase()}: ${failureMessage}`;
        } else if (job?.state === "SUCCEEDED") {
            detail.textContent = rawAssociation.usable === true && jobCurrent
                ? "Raw H3 output ready."
                : rawAssociation.usable === true
                    ? "Historical raw H3 output retained."
                    : "ComfyUI reported success, but no usable output association is available.";
        } else if (job?.state === "FAILED") {
            detail.textContent = job.failure?.message || "The render job failed.";
        } else if (job?.state === "UNKNOWN") {
            detail.textContent = job.failure?.message || "Checking ComfyUI queue/history; reconcile this existing prompt before retrying.";
        } else if (job?.state === "ORPHANED") {
            detail.textContent = "Previous render is unavailable and can be retried.";
        } else if (job && renderJobIsActive(job)) {
            if (job.state === "CANCEL_REQUESTED") {
                detail.textContent = "Cancellation requested.";
            } else {
                detail.hidden = true;
            }
        } else if (blockers.length) {
            detail.textContent = renderDiagnosticText(blockers[0]);
        } else if (scene.preparation_status === "current") {
            detail.textContent = executionEligible
                ? "Prepared inputs are current and ready to render."
                : "Prepared inputs are current, but a runtime execution requirement is not ready.";
        } else if (scene.preparation_status === "stale") {
            detail.textContent = "Existing preparation is stale and must be regenerated from current scene inputs.";
        } else {
            detail.textContent = "All structural prerequisites are current; prepare inputs to create the dry workflow package.";
        }
        const telemetryPanel = createRenderJobTelemetry();

        const actions = document.createElement("div");
        actions.className = "mvb-render-actions";
        const prepareButton = document.createElement("button");
        prepareButton.className = "mvb-button mvb-button-primary mvb-button-small";
        prepareButton.type = "button";
        prepareButton.dataset.mvbRenderPrepare = scene.scene_id;
        prepareButton.textContent = scene.preparation_status === "stale" ? "Re-prepare Render Inputs" : "Prepare Render Inputs";
        prepareButton.disabled = !ready || Boolean(job && renderJobIsActive(job)) || state.renderPreparingSceneId === scene.scene_id || Boolean(state.operation) || state.transitioning || state.closing || batchExecuting;
        actions.append(prepareButton);
        const upscaleUnavailable = upscaleMethod !== "none" && prodCap?.state === "UNAVAILABLE";
        const renderButton = document.createElement("button");
        renderButton.className = "mvb-button mvb-button-primary mvb-button-small";
        renderButton.type = "button";
        renderButton.dataset.mvbRenderSubmit = scene.scene_id;
        renderButton.textContent = "Render Scene";
        renderButton.disabled = !executionEligible
            || !ready
            || Boolean(job && renderJobIsActive(job))
            || state.renderSubmittingSceneId === scene.scene_id
            || Boolean(state.operation)
            || state.transitioning
            || state.closing
            || batchExecuting;
        renderButton.title = batchExecuting
            ? "A batch render is active; manual scene renders are paused."
            : executionEligible
                ? "Submit the current project-owned preparation package to local ComfyUI."
                : (scene.execution_eligibility?.blockers?.[0]?.message || "Current scene execution requirements are not ready.");
        const retryableJob = Boolean(job
            && completion?.retry_available === true
            && ["FAILED", "CANCELLED", "INTERRUPTED", "ORPHANED"].includes(job.state));
        if (!retryableJob) {
            actions.append(renderButton);
        }
        if (job && renderJobIsActive(job) && job.state !== "UNKNOWN") {
            const cancelButton = document.createElement("button");
            cancelButton.className = "mvb-button mvb-button-secondary mvb-button-small";
            cancelButton.type = "button";
            cancelButton.dataset.mvbRenderCancel = job.job_id;
            cancelButton.textContent = job.state === "CANCEL_REQUESTED" ? "Cancellation requested" : "Cancel";
            cancelButton.disabled = job.state === "CANCEL_REQUESTED" || state.renderJobActionId === job.job_id || Boolean(state.operation) || state.transitioning || state.closing;
            actions.append(cancelButton);
        }
        if (retryableJob) {
            const retryButton = document.createElement("button");
            retryButton.className = "mvb-button mvb-button-secondary mvb-button-small";
            retryButton.type = "button";
            retryButton.dataset.mvbRenderRetry = job.job_id;
            retryButton.textContent = "Retry";
            retryButton.disabled = !executionEligible || state.renderJobActionId === job.job_id || Boolean(state.operation) || state.transitioning || state.closing || batchExecuting;
            actions.append(retryButton);
        }
        if (renderFinalizationEligible(job)) {
            const finalizeButton = document.createElement("button");
            finalizeButton.className = "mvb-button mvb-button-secondary mvb-button-small";
            finalizeButton.type = "button";
            finalizeButton.dataset.mvbRenderFinalize = job.job_id;
            finalizeButton.textContent = finalizationState === "FAILED" ? "Retry Finalization" : "Finalize Scene";
            finalizeButton.disabled = state.renderFinalizingJobId === job.job_id
                || Boolean(state.operation)
                || state.transitioning
                || state.closing
                || batchExecuting;
            actions.append(finalizeButton);
        }
        if (upscaleMethod !== "none" && finalizationState === "FINALIZED" && jobCurrent) {
            const pjobActive = Boolean(pjob && ["SUBMITTING", "SUBMITTED", "ACTIVE"].includes(pjob.state));
            const pjobFailed = Boolean(pjob && pjob.state === "FAILED");
            if (pjobActive) {
                const cancelUpscaleButton = document.createElement("button");
                cancelUpscaleButton.className = "mvb-button mvb-button-secondary mvb-button-small";
                cancelUpscaleButton.type = "button";
                cancelUpscaleButton.dataset.mvbRenderPostprocessCancel = scene.scene_id;
                cancelUpscaleButton.textContent = "Cancel Upscale";
                cancelUpscaleButton.disabled = Boolean(state.operation) || state.transitioning || state.closing;
                actions.append(cancelUpscaleButton);
            } else if (pjobFailed) {
                const retryUpscaleButton = document.createElement("button");
                retryUpscaleButton.className = "mvb-button mvb-button-secondary mvb-button-small";
                retryUpscaleButton.type = "button";
                retryUpscaleButton.dataset.mvbRenderPostprocessRetry = scene.scene_id;
                retryUpscaleButton.textContent = "Retry Upscale";
                retryUpscaleButton.disabled = Boolean(state.operation) || state.transitioning || state.closing || batchExecuting || upscaleUnavailable;
                actions.append(retryUpscaleButton);
            } else if (!pjob || pjob.state !== "SUCCEEDED" || prodSummary?.status !== "ready") {
                if (prodCap?.state !== "UNAVAILABLE") {
                    const upscaleButton = document.createElement("button");
                    upscaleButton.className = "mvb-button mvb-button-secondary mvb-button-small";
                    upscaleButton.type = "button";
                    upscaleButton.dataset.mvbRenderPostprocessStart = scene.scene_id;
                    upscaleButton.textContent = "Upscale Scene";
                    upscaleButton.disabled = Boolean(job && renderJobIsActive(job)) || Boolean(state.operation) || state.transitioning || state.closing || batchExecuting;
                    actions.append(upscaleButton);
                }
            }
        }
        const blockersList = document.createElement("ul");
        blockersList.className = "mvb-render-blockers";
        blockersList.hidden = blockers.length === 0;
        for (const blocker of blockers) {
            const item = document.createElement("li");
            item.textContent = renderDiagnosticText(blocker);
            blockersList.append(item);
        }
        const children = [];
        if (selection) {
            const choice = document.createElement("label");
            choice.className = "mvb-render-select";
            if (!selectionEligible) {
                choice.className += " mvb-render-select-disabled";
            }
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.dataset.mvbRenderSelectScene = scene.scene_id;
            checkbox.checked = isSelected;
            checkbox.disabled = !selectionEligible || state.renderBatchBusy;
            checkbox.setAttribute("aria-label", `Select Scene ${scene.sequence} for batch rendering`);
            const choiceLabel = document.createElement("span");
            choiceLabel.textContent = selectionEligible
                ? "Include in batch"
                : selectionAction === "ALREADY_COMPLETE"
                    ? "Final scene already ready"
                    : "Not ready for batch rendering";
            choice.append(checkbox, choiceLabel);
            children.push(choice);
        }
        card.replaceChildren(
            ...children,
            heading,
            facts,
            detail,
            telemetryPanel,
            blockersList,
            actions,
        );
        updateRenderJobTelemetry(card, job);
        list.append(card);
    }
    for (const [sceneId, card] of existingCards) {
        if (!activeSceneIds.has(sceneId)) {
            card.remove();
        }
    }
    if (content) {
        content.scrollTop = contentScrollTop;
    }
    renderSelectionBar(root);
    renderBatchPanel(root);
}

async function loadRenderJobs(root, silent = false) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderJobsLoading) {
        return false;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderJobsLoading = true;
    if (!silent) {
        builderState.renderJobsState = "loading";
        builderState.renderJobsMessage = "";
    }
    try {
        const payload = await fetchJson(renderJobsPath(projectId), { method: "GET", cache: "no-store" });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        if (!payload || payload.project_id !== projectId || !Array.isArray(payload.jobs)) {
            throw new Error("Render jobs response was invalid.");
        }
        if (payload.jobs.some((job) => job && typeof job.scene_id === "string" && typeof job.job_id === "string" && !renderCompletionProjectionIsValid(job))) {
            throw new Error("Render lifecycle projection was invalid; refresh the Builder backend.");
        }
        builderState.renderJobs = Object.fromEntries(
            payload.jobs
                .filter((job) => job && typeof job.scene_id === "string" && typeof job.job_id === "string")
                .map((job) => [job.scene_id, job]),
        );
        builderState.renderJobsState = "ready";
        builderState.renderJobsMessage = Array.isArray(payload.warnings) && payload.warnings.length
            ? payload.warnings[0].message || "ComfyUI status is temporarily unavailable."
            : "";
        try {
            const prodPayload = await fetchJson(productionStatusPath(projectId), { method: "GET", cache: "no-store" });
            if (prodPayload && prodPayload.project_id === projectId) {
                builderState.renderProductionStatus = prodPayload;
            }
        } catch (error) {
            // production status is non-blocking
        }
        // The backend owns one shared WebSocket telemetry session. Keep the
        // durable HTTP reconciliation cadence bounded while live volatile
        // observations are overlaid without rebuilding the card shell.
        builderState.renderJobsPollDelay = 1000;
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        console.error("[Music Video Builder] Render job status request failed.", error);
        builderState.renderJobsState = "error";
        builderState.renderJobsMessage = error instanceof Error ? error.message : "Render job status could not be loaded.";
        return false;
    } finally {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            builderState.renderJobsLoading = false;
            void loadRenderBatch(root, true);
            renderRenderState(root);
            scheduleRenderJobPoll(root);
        }
    }
}

async function submitRenderScene(root, sceneId) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderSubmittingSceneId) {
        return;
    }
    const scene = builderState.renderPreflight?.scenes?.find((entry) => entry.scene_id === sceneId);
    if (!scene || scene.execution_eligibility?.eligible !== true || scene.preparation_status !== "current") {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderSubmittingSceneId = sceneId;
    builderState.renderMessage = "Starting render…";
    builderState.renderMessageState = "working";
    renderRenderState(root);
    try {
        await fetchJson(renderSceneSubmitPath(projectId, sceneId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        builderState.renderMessage = "";
        builderState.renderMessageState = "";
        await loadRenderJobs(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Render submission could not complete.";
            builderState.renderMessageState = "error";
            await loadRenderJobs(root, true);
        }
    } finally {
        if (isActive(root)) {
            builderState.renderSubmittingSceneId = null;
            renderRenderState(root);
        }
    }
}

async function cancelRenderSceneJob(root, jobId) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderJobActionId) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderJobActionId = jobId;
    try {
        await fetchJson(renderJobCancelPath(projectId, jobId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        await loadRenderJobs(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Render cancellation could not complete.";
            builderState.renderMessageState = "error";
        }
    } finally {
        if (isActive(root)) {
            builderState.renderJobActionId = null;
            renderRenderState(root);
        }
    }
}

async function retryRenderSceneJob(root, jobId) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderJobActionId) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderJobActionId = jobId;
    try {
        await fetchJson(renderJobRetryPath(projectId, jobId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        await loadRenderJobs(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Render retry could not complete.";
            builderState.renderMessageState = "error";
            await loadRenderJobs(root, true);
        }
    } finally {
        if (isActive(root)) {
            builderState.renderJobActionId = null;
            renderRenderState(root);
        }
    }
}

async function finalizeRenderScene(root, jobId) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderFinalizingJobId) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderFinalizingJobId = jobId;
    builderState.renderMessage = "Finalizing the current raw H3 output…";
    builderState.renderMessageState = "working";
    renderRenderState(root);
    try {
        await fetchJson(renderJobFinalizePath(projectId, jobId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        builderState.renderMessage = "Final scene output validated.";
        builderState.renderMessageState = "success";
        await loadRenderJobs(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Final scene finalization could not complete.";
            builderState.renderMessageState = "error";
        }
    } finally {
        if (isActive(root)) {
            builderState.renderFinalizingJobId = null;
            renderRenderState(root);
        }
    }
}

async function setUpscaleMethod(root, method) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    try {
        const result = await fetchJson(productionSetMethodPath(projectId), {
            method: "PUT",
            body: JSON.stringify({ upscale_method: method }),
        });
        if (result?.production) {
            builderState.currentProject.production = result.production;
        }
        builderState.renderMessage = "";
        builderState.renderMessageState = "ready";
        await loadRenderJobs(root, true);
        await loadRenderPreflight(root, true);
    } catch (error) {
        console.error("[Music Video Builder] Setting upscale method failed.", error);
        builderState.renderMessage = error instanceof Error ? error.message : "Could not update upscale method.";
        builderState.renderMessageState = "error";
        renderRenderState(root);
    }
}

async function startScenePostprocess(root, sceneId) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    try {
        await fetchJson(scenePostprocessStartPath(projectId, sceneId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        await loadRenderJobs(root, true);
    } catch (error) {
        console.error("[Music Video Builder] Postprocess start failed.", error);
        builderState.renderMessage = error instanceof Error ? error.message : "Could not start post-processing.";
        builderState.renderMessageState = "error";
        renderRenderState(root);
    }
}

async function cancelScenePostprocess(root, sceneId) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    try {
        await fetchJson(scenePostprocessCancelPath(projectId, sceneId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        await loadRenderJobs(root, true);
    } catch (error) {
        console.error("[Music Video Builder] Postprocess cancel failed.", error);
        builderState.renderMessage = error instanceof Error ? error.message : "Could not cancel post-processing.";
        builderState.renderMessageState = "error";
        renderRenderState(root);
    }
}

async function retryScenePostprocess(root, sceneId) {
    if (!isActive(root) || !builderState.currentProject) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    try {
        await fetchJson(scenePostprocessRetryPath(projectId, sceneId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        await loadRenderJobs(root, true);
    } catch (error) {
        console.error("[Music Video Builder] Postprocess retry failed.", error);
        builderState.renderMessage = error instanceof Error ? error.message : "Could not retry post-processing.";
        builderState.renderMessageState = "error";
        renderRenderState(root);
    }
}

function renderBatchStageLabel(root, batch) {
    const current = batch?.current;
    if (!current || typeof current.scene_id !== "string") {
        return null;
    }
    const sequence = Number.isInteger(current.sequence) ? current.sequence : null;
    const sceneLabel = sequence ? `Scene ${sequence}` : null;
    const job = builderState.renderJobs?.[current.scene_id] || null;
    const presentation = renderTelemetryPresentation(job);
    const stage = presentation?.stageLabel || (job?.progress && typeof job.progress.label === "string" ? job.progress.label : null);
    if (current.disposition === "PREPARING") {
        return sceneLabel ? `${sceneLabel} · Preparing render inputs` : "Preparing render inputs";
    }
    if (current.disposition === "FINALIZING") {
        return sceneLabel ? `${sceneLabel} · Finalizing scene` : "Finalizing scene";
    }
    if (current.disposition === "POSTPROCESSING") {
        return sceneLabel ? `${sceneLabel} · Upscaling production scene` : "Upscaling production scene";
    }
    if (sceneLabel && stage) {
        return `${sceneLabel} · ${stage}`;
    }
    if (sceneLabel) {
        return `${sceneLabel} · Rendering`;
    }
    return stage || null;
}

function renderBatchPanel(root) {
    if (!isActive(root)) {
        return;
    }
    const panel = root.querySelector("[data-mvb-batch-panel]");
    if (!panel) {
        return;
    }
    const batch = builderState.renderBatch;
    if (!batch || typeof batch.state !== "string") {
        panel.hidden = true;
        panel.replaceChildren();
        panel.dataset.shellKey = "";
        return;
    }
    const counts = batch.counts || {};
    const attention = batch.attention && typeof batch.attention.message === "string" ? batch.attention.message : null;
    const stageLabel = renderBatchStageLabel(root, batch);
    const parts = [];
    if (Number.isInteger(counts.complete) && counts.complete > 0) parts.push(`${counts.complete} complete`);
    if (Number.isInteger(counts.failed) && counts.failed > 0) parts.push(`${counts.failed} failed`);
    if (Number.isInteger(counts.cancelled) && counts.cancelled > 0) parts.push(`${counts.cancelled} cancelled`);
    if (Number.isInteger(counts.skipped) && counts.skipped > 0) parts.push(`${counts.skipped} skipped`);
    if (Number.isInteger(counts.remaining) && counts.remaining > 0) parts.push(`${counts.remaining} remaining`);
    const shellKey = JSON.stringify([
        batch.state,
        batch.pause_requested,
        counts,
        attention,
        stageLabel,
        batch.current || null,
        batch.retryable_count,
    ]);
    if (panel.dataset.shellKey === shellKey && panel.childElementCount > 0) {
        return;
    }
    panel.dataset.shellKey = shellKey;
    panel.hidden = false;
    panel.className = "mvb-batch-panel";
    panel.dataset.state = batch.state;
    panel.replaceChildren();

    const heading = document.createElement("div");
    heading.className = "mvb-batch-heading";
    const title = document.createElement("h3");
    title.className = "mvb-batch-title";
    const status = document.createElement("span");
    status.className = "mvb-batch-status";
    if (batch.state === "RUNNING" || batch.state === "PAUSE_REQUESTED") {
        title.textContent = "Batch render";
        status.textContent = batch.state === "PAUSE_REQUESTED" ? "Pausing after current scene" : "Running";
    } else if (batch.state === "PAUSED") {
        title.textContent = "Batch paused";
        status.textContent = attention || "Paused";
        status.dataset.state = attention ? "warning" : "neutral";
    } else if (batch.state === "PAUSED_RECOVERY") {
        title.textContent = "Batch paused after restart";
        status.textContent = attention || "Resume to continue production.";
        status.dataset.state = "warning";
    } else if (batch.state === "COMPLETED") {
        title.textContent = "Batch complete";
        status.textContent = parts.length ? parts.join(" · ") : "All selected scenes are complete.";
    } else if (batch.state === "COMPLETED_WITH_ISSUES") {
        title.textContent = "Batch complete";
        status.textContent = parts.length ? parts.join(" · ") : "Completed with issues.";
        status.dataset.state = "warning";
    } else if (batch.state === "ENDED") {
        title.textContent = "Batch ended";
        status.textContent = parts.length ? parts.join(" · ") : "Ended.";
    }
    heading.append(title, status);
    panel.append(heading);

    if (Number.isInteger(counts.total) && counts.total > 0 && batch.state !== "COMPLETED" && batch.state !== "COMPLETED_WITH_ISSUES" && batch.state !== "ENDED") {
        const summary = document.createElement("p");
        summary.className = "mvb-batch-summary";
        summary.textContent = `${counts.processed || 0} of ${counts.total} scenes processed${parts.length ? ` · ${parts.join(" · ")}` : ""}`;
        panel.append(summary);
    }

    if ((batch.state === "RUNNING" || batch.state === "PAUSE_REQUESTED") && stageLabel) {
        const currentLine = document.createElement("p");
        currentLine.className = "mvb-batch-current";
        currentLine.textContent = `Current: ${stageLabel}`;
        panel.append(currentLine);
    }

    if (Number.isInteger(counts.total) && counts.total > 0) {
        const progress = document.createElement("div");
        progress.className = "mvb-batch-progress";
        const label = document.createElement("span");
        label.className = "mvb-batch-progress-label";
        label.textContent = "Scenes processed";
        const track = document.createElement("div");
        track.className = "mvb-batch-progress-track";
        track.setAttribute("role", "progressbar");
        track.setAttribute("aria-label", "Scenes processed");
        const processed = Math.max(0, Math.min(counts.total, counts.processed || 0));
        const percent = Math.round((processed / counts.total) * 100);
        track.setAttribute("aria-valuemin", "0");
        track.setAttribute("aria-valuemax", String(counts.total));
        track.setAttribute("aria-valuenow", String(processed));
        track.setAttribute("aria-valuetext", `${processed} of ${counts.total} scenes processed`);
        const bar = document.createElement("div");
        bar.className = "mvb-batch-progress-bar";
        bar.style.width = `${percent}%`;
        track.append(bar);
        const value = document.createElement("span");
        value.className = "mvb-batch-progress-value";
        value.textContent = `${processed} / ${counts.total}`;
        progress.append(label, track, value);
        panel.append(progress);
    }

    const actions = document.createElement("div");
    actions.className = "mvb-batch-actions";
    const busy = builderState.renderBatchBusy;
    if (batch.state === "RUNNING") {
        const pauseButton = document.createElement("button");
        pauseButton.className = "mvb-button mvb-button-secondary mvb-button-small";
        pauseButton.type = "button";
        pauseButton.dataset.mvbBatchPause = "true";
        pauseButton.textContent = "Pause After Current";
        pauseButton.disabled = busy;
        actions.append(pauseButton);
    } else if (batch.state === "PAUSE_REQUESTED") {
        const pausing = document.createElement("span");
        pausing.className = "mvb-batch-note";
        pausing.textContent = "Pausing after the current scene…";
        actions.append(pausing);
    } else if (batch.state === "PAUSED" || batch.state === "PAUSED_RECOVERY") {
        const resumeButton = document.createElement("button");
        resumeButton.className = "mvb-button mvb-button-primary mvb-button-small";
        resumeButton.type = "button";
        resumeButton.dataset.mvbBatchResume = "true";
        resumeButton.textContent = "Resume Batch";
        resumeButton.disabled = busy;
        actions.append(resumeButton);
        const endButton = document.createElement("button");
        endButton.className = "mvb-button mvb-button-secondary mvb-button-small";
        endButton.type = "button";
        endButton.dataset.mvbBatchEnd = "true";
        endButton.textContent = "End Batch";
        endButton.disabled = busy;
        actions.append(endButton);
    } else if ((batch.state === "COMPLETED_WITH_ISSUES" || batch.state === "ENDED") && Number.isInteger(batch.retryable_count) && batch.retryable_count > 0) {
        const retryButton = document.createElement("button");
        retryButton.className = "mvb-button mvb-button-primary mvb-button-small";
        retryButton.type = "button";
        retryButton.dataset.mvbBatchRetryFailed = "true";
        retryButton.textContent = "Retry Failed";
        retryButton.disabled = busy;
        actions.append(retryButton);
    }
    if (actions.childElementCount > 0) {
        panel.append(actions);
    }
}

async function loadRenderBatch(root, silent = false) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderBatchLoading) {
        return false;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderBatchLoading = true;
    try {
        const payload = await fetchJson(renderBatchPath(projectId), { method: "GET", cache: "no-store" });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        builderState.renderBatch = payload && typeof payload.batch === "object" ? payload.batch : null;
        renderBatchPanel(root);
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        if (!silent) {
            console.error("[Music Video Builder] Batch status request failed.", error);
        }
        return false;
    } finally {
        builderState.renderBatchLoading = false;
    }
}

function renderSelectionBar(root) {
    const bar = root.querySelector("[data-mvb-render-selection]");
    if (!bar) {
        return;
    }
    const selection = builderState.renderBatchSelection;
    if (!selection) {
        bar.hidden = true;
        bar.replaceChildren();
        return;
    }
    bar.hidden = false;
    const eligibleIds = [...selection.actions.entries()]
        .filter(([, action]) => BATCH_ELIGIBLE_ACTIONS.includes(action))
        .map(([sceneId]) => sceneId);
    const selectedCount = selection.selected.size;
    bar.replaceChildren();
    const count = document.createElement("span");
    count.className = "mvb-render-selection-count";
    count.textContent = `${selectedCount} selected`;
    const selectAll = document.createElement("button");
    selectAll.className = "mvb-button mvb-button-secondary mvb-button-small";
    selectAll.type = "button";
    selectAll.dataset.mvbBatchSelectAll = "true";
    selectAll.textContent = "Select All Ready";
    selectAll.disabled = eligibleIds.length === 0;
    const clear = document.createElement("button");
    clear.className = "mvb-button mvb-button-secondary mvb-button-small";
    clear.type = "button";
    clear.dataset.mvbBatchClear = "true";
    clear.textContent = "Clear";
    clear.disabled = selectedCount === 0;
    const renderSelected = document.createElement("button");
    renderSelected.className = "mvb-button mvb-button-primary mvb-button-small";
    renderSelected.type = "button";
    renderSelected.dataset.mvbBatchRenderSelected = "true";
    renderSelected.textContent = "Render Selected";
    renderSelected.disabled = selectedCount === 0 || builderState.renderBatchBusy;
    const cancel = document.createElement("button");
    cancel.className = "mvb-button mvb-button-secondary mvb-button-small";
    cancel.type = "button";
    cancel.dataset.mvbBatchCancelSelection = "true";
    cancel.textContent = "Cancel Selection";
    cancel.disabled = builderState.renderBatchBusy;
    bar.append(count, selectAll, clear, renderSelected, cancel);
}

async function enterBatchSelectionMode(root) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderBatchSelection || builderState.renderBatchBusy) {
        return;
    }
    if (batchIsExecuting(builderState.renderBatch)) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderBatchBusy = true;
    try {
        const payload = await fetchJson(renderBatchPreviewPath(projectId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        const actions = new Map(
            (Array.isArray(payload?.scenes) ? payload.scenes : [])
                .filter((entry) => entry && typeof entry.scene_id === "string")
                .map((entry) => [entry.scene_id, entry.action]),
        );
        builderState.renderBatchSelection = { selected: new Set(), actions };
    } catch (error) {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            const preflightScenes = Array.isArray(builderState.renderPreflight?.scenes) ? builderState.renderPreflight.scenes : [];
            const actions = new Map(
                preflightScenes.map((s) => [s.scene_id, s.preparation_ready ? "PREPARE_AND_RENDER" : "NOT_READY"]),
            );
            builderState.renderBatchSelection = { selected: new Set(), actions };
        }
    } finally {
        if (isActive(root)) {
            builderState.renderBatchBusy = false;
            renderRenderState(root);
        }
    }
}

function exitBatchSelectionMode(root) {
    builderState.renderBatchSelection = null;
    builderState.renderBatchConfirm = null;
    const dialog = root.querySelector("[data-mvb-batch-confirm]");
    if (dialog) {
        dialog.hidden = true;
    }
    renderRenderState(root);
}

async function openBatchConfirmation(root, selection) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderBatchBusy) {
        return;
    }
    if (batchIsExecuting(builderState.renderBatch)) {
        return;
    }
    const upscaleMethod = builderState.currentProject?.production?.upscale_method || "none";
    const prodCap = builderState.renderProductionStatus?.capabilities?.[upscaleMethod];
    if (upscaleMethod !== "none" && prodCap?.state === "UNAVAILABLE") {
        const methodLabel = upscaleMethod === "rtx_vsr_fast" ? "RTX VSR" : (prodCap?.label || "SeedVR2");
        builderState.renderMessage = `${methodLabel} is unavailable on this runtime. Select another upscale method or use a compatible production machine.`;
        builderState.renderMessageState = "error";
        renderRenderState(root);
        return;
    }
    const projectId = builderState.currentProject.project_id;
    const body = batchSelectionBody(selection);
    builderState.renderBatchBusy = true;
    try {
        const payload = await fetchJson(renderBatchPreviewPath(projectId), {
            method: "POST",
            body: JSON.stringify(body),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        const counts = payload?.counts || {};
        if (!Number.isInteger(counts.eligible) || counts.eligible <= 0) {
            builderState.renderMessage = "No scenes are currently ready for batch production.";
            builderState.renderMessageState = "warning";
            renderRenderState(root);
            return;
        }
        builderState.renderBatchConfirm = { body, counts };
        const dialog = root.querySelector("[data-mvb-batch-confirm]");
        const title = root.querySelector("[data-mvb-batch-confirm-title]");
        const summary = root.querySelector("[data-mvb-batch-confirm-summary]");
        const errorLine = root.querySelector("[data-mvb-batch-confirm-error]");
        if (dialog && title && summary && errorLine) {
            title.textContent = `Render ${counts.eligible} scene${counts.eligible === 1 ? "" : "s"}?`;
            const detailParts = [];
            if (Number.isInteger(counts.needs_render) && counts.needs_render > 0) {
                detailParts.push(`${counts.needs_render} require H3 rendering`);
            }
            if (Number.isInteger(counts.finalize_only) && counts.finalize_only > 0) {
                detailParts.push(`${counts.finalize_only} require finalization only`);
            }
            summary.textContent = detailParts.length ? detailParts.join(" · ") : "";
            summary.hidden = detailParts.length === 0;
            errorLine.hidden = true;
            errorLine.textContent = "";
            dialog.hidden = false;
        }
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Batch preview could not be loaded.";
            builderState.renderMessageState = "error";
            renderRenderState(root);
        }
    } finally {
        if (isActive(root)) {
            builderState.renderBatchBusy = false;
        }
    }
}

function closeBatchConfirmation(root) {
    builderState.renderBatchConfirm = null;
    const dialog = root.querySelector("[data-mvb-batch-confirm]");
    if (dialog) {
        dialog.hidden = true;
    }
}

async function startConfirmedBatch(root) {
    if (!isActive(root) || !builderState.currentProject || !builderState.renderBatchConfirm || builderState.renderBatchBusy) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    const confirm = builderState.renderBatchConfirm;
    builderState.renderBatchBusy = true;
    const startButton = root.querySelector("[data-mvb-batch-confirm-start]");
    const errorLine = root.querySelector("[data-mvb-batch-confirm-error]");
    if (startButton) {
        startButton.disabled = true;
    }
    try {
        await fetchJson(renderBatchStartPath(projectId), {
            method: "POST",
            body: JSON.stringify(confirm.body),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        closeBatchConfirmation(root);
        builderState.renderBatchSelection = null;
        builderState.renderMessage = "";
        builderState.renderMessageState = "ready";
        await loadRenderBatch(root, true);
        await loadRenderJobs(root, true);
        await loadRenderPreflight(root, true);
    } catch (error) {
        if (isActive(root) && errorLine) {
            errorLine.textContent = error instanceof Error ? error.message : "The batch could not be started.";
            errorLine.hidden = false;
        }
    } finally {
        if (isActive(root)) {
            builderState.renderBatchBusy = false;
            if (startButton) {
                startButton.disabled = false;
            }
            renderRenderState(root);
            renderBatchPanel(root);
        }
    }
}

async function batchControlAction(root, path) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderBatchBusy) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderBatchBusy = true;
    renderBatchPanel(root);
    try {
        await fetchJson(path(projectId), { method: "POST", body: JSON.stringify({}) });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        await loadRenderBatch(root, true);
        await loadRenderJobs(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "The batch control could not be completed.";
            builderState.renderMessageState = "error";
            await loadRenderBatch(root, true);
        }
    } finally {
        if (isActive(root)) {
            builderState.renderBatchBusy = false;
            renderRenderState(root);
            renderBatchPanel(root);
        }
    }
}

async function loadRenderPreflight(root, silent = false, { forceRefresh = false } = {}) {
    if (!isActive(root) || !builderState.currentProject) {
        return false;
    }
    if (builderState.renderState === "loading") {
        return false;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderLoadingMode = forceRefresh || !builderState.renderRequirementsCache ? "runtime" : "scene";
    builderState.renderState = "loading";
    if (!silent) {
        builderState.renderMessage = "";
        builderState.renderMessageState = "ready";
    }
    renderRenderState(root);
    try {
        const payload = await fetchJson(renderPreflightPath(projectId, forceRefresh), { method: "GET", cache: "no-store" });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        if (!payload || payload.project_id !== projectId || !Array.isArray(payload.scenes)) {
            throw new Error("Render preflight response was invalid.");
        }
        builderState.renderPreflight = payload;
        builderState.renderRequirementsCache = payload.requirements_cache || null;
        builderState.renderLoadingMode = "scene";
        builderState.renderState = "ready";
        void loadRenderJobs(root, true);
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        console.error("[Music Video Builder] Render preflight failed.", error);
        builderState.renderState = "error";
        builderState.renderMessage = error instanceof Error ? error.message : "Render preflight could not be loaded.";
        builderState.renderMessageState = "error";
        return false;
    } finally {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            renderRenderState(root);
        }
    }
}

async function prepareRenderScene(root, sceneId) {
    if (!isActive(root) || !builderState.currentProject || builderState.renderPreparingSceneId) {
        return;
    }
    const scene = builderState.renderPreflight?.scenes?.find((entry) => entry.scene_id === sceneId);
    const prompt = scene ? renderPromptStateForScene(scene) : null;
    if (!scene || !scene.preparation_ready || !prompt?.ready) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    builderState.renderPreparingSceneId = sceneId;
    builderState.renderMessage = "Preparing project-owned scene inputs…";
    builderState.renderMessageState = "working";
    renderRenderState(root);
    try {
        const result = await fetchJson(renderScenePreparePath(projectId, sceneId), {
            method: "POST",
            body: JSON.stringify({}),
        });
        if (result?.status === "prepared") {
            // The refreshed scene card is the durable success surface: its
            // Preparation tile becomes PREPARED without exposing internal IDs
            // or repeating timing and workflow details in the page header.
            builderState.renderMessage = "";
            builderState.renderMessageState = "ready";
        } else {
            builderState.renderMessage = "Preparation did not complete.";
            builderState.renderMessageState = "error";
        }
        await loadRenderPreflight(root, true);
    } catch (error) {
        if (isActive(root)) {
            builderState.renderMessage = error instanceof Error ? error.message : "Scene preparation could not complete.";
            builderState.renderMessageState = "error";
        }
    } finally {
        if (isActive(root)) {
            builderState.renderPreparingSceneId = null;
            renderRenderState(root);
        }
    }
}

async function copyPrompt(root, sceneId, generationMethod) {
    const promptViewport = capturePromptViewport(root, sceneId);
    const item = promptItemFor(sceneId, generationMethod);
    const draft = item ? promptDraftFor(sceneId, generationMethod, item) : null;
    if (!draft?.text) {
        return;
    }
    try {
        if (!navigator.clipboard?.writeText) {
            throw new Error("Clipboard access is unavailable; select the visible prompt to copy it.");
        }
        await navigator.clipboard.writeText(draft.text);
        builderState.promptMessage = "Prompt copied.";
        builderState.promptMessageState = "success";
    } catch (error) {
        console.error("[Music Video Builder] Prompt copy failed.", error);
        builderState.promptMessage = error instanceof Error ? error.message : "Prompt could not be copied.";
        builderState.promptMessageState = "error";
    }
    renderPromptState(root, { promptViewport });
}

async function generatePromptRelayRequest(root, sceneId, generationMethod) {
    if (!isActive(root) || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    const item = promptItemFor(sceneId, generationMethod);
    if (!item) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    const promptViewport = capturePromptViewport(root, sceneId);
    const relay = promptRelayFor(sceneId, generationMethod);
    builderState.operation = "prompt-relay-request";
    builderState.promptMessage = "";
    renderProjectState(root, { promptViewport });
    try {
        const request = await fetchJson(promptPath(projectId, sceneId, "relay-request"), {
            method: "POST",
            body: JSON.stringify({}),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        const currentItem = promptItemFor(sceneId, generationMethod);
        if (!request
            || request.prompt_relay_request_version !== 1
            || request.scene_id !== sceneId
            || request.generation_method !== generationMethod
            || request.request_fingerprint !== currentItem?.source_fingerprint) {
            // Reconcile the prompt cards with the authoritative backend state
            // so the next attempt compares against the current fingerprint.
            void loadPromptCards(root, true);
            throw new Error("Generated GPT request was invalid or stale.");
        }
        relay.request = request;
        relay.responseMessage = "Request generated. Copy it into Vesper H3 Prompt Director.";
        relay.responseMessageState = "success";
    } catch (error) {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            console.error("[Music Video Builder] Custom GPT request generation failed.", error);
            relay.responseMessage = error instanceof Error ? error.message : "GPT request could not be generated.";
            relay.responseMessageState = "error";
        }
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root, { promptViewport });
        }
    }
}

async function copyPromptRelayRequest(root, sceneId, generationMethod) {
    const promptViewport = capturePromptViewport(root, sceneId);
    const item = promptItemFor(sceneId, generationMethod);
    const relay = promptRelayFor(sceneId, generationMethod);
    if (!canCopyPromptRelayRequest(relay, item) || !isActive(root)) {
        return;
    }
    try {
        if (!navigator.clipboard?.writeText) {
            throw new Error("Clipboard access is unavailable; select the visible request JSON to copy it.");
        }
        await navigator.clipboard.writeText(promptRelayRequestJson(relay.request));
        relay.responseMessage = "REQUEST COPIED";
        relay.responseMessageState = "success";
    } catch (error) {
        console.error("[Music Video Builder] Custom GPT request copy failed.", error);
        relay.responseMessage = error instanceof Error ? error.message : "Request JSON could not be copied.";
        relay.responseMessageState = "error";
    }
    renderPromptState(root, { promptViewport });
}

async function applyPromptRelayResponse(root, sceneId, generationMethod) {
    if (!isActive(root) || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    const item = promptItemFor(sceneId, generationMethod);
    const relay = promptRelayFor(sceneId, generationMethod);
    if (!canApplyPromptRelayResponse(relay, item)) {
        return;
    }
    let response;
    try {
        response = JSON.parse(relay.responseText);
    } catch (error) {
        relay.responseMessage = "GPT response is not valid JSON. Copy the raw JSON response again without Markdown or unescaped quotation marks.";
        relay.responseMessageState = "error";
        renderPromptState(root, { promptViewport: capturePromptViewport(root, sceneId) });
        return;
    }
    const projectId = builderState.currentProject.project_id;
    const promptViewport = capturePromptViewport(root, sceneId);
    let changedCandidateApplied = false;
    builderState.operation = "prompt-relay-apply";
    builderState.promptMessage = "";
    renderProjectState(root, { promptViewport });
    try {
        const result = await fetchJson(promptPath(projectId, sceneId, "relay-response"), {
            method: "POST",
            body: JSON.stringify(response),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        if (!result || typeof result.enhanced_prompt !== "string" || result.request_fingerprint !== item.source_fingerprint) {
            throw new Error("Custom GPT response was invalid or stale.");
        }
        const currentItem = promptItemFor(sceneId, generationMethod);
        if (!currentItem || currentItem.source_fingerprint !== item.source_fingerprint) {
            throw new Error("Custom GPT response is stale. Generate a new request first.");
        }
        const draft = promptDraftFor(sceneId, generationMethod, currentItem);
        draft.text = result.enhanced_prompt;
        draft.sourceFingerprint = result.request_fingerprint;
        draft.relayFingerprint = result.request_fingerprint;
        draft.dirty = promptDraftIsDirty(currentItem, draft);
        changedCandidateApplied = draft.dirty;
        builderState.promptMessage = draft.dirty
            ? "PROMPT DIRECTOR RESPONSE APPLIED — UNSAVED. Review and save the Final Prompt."
            : "This Prompt Director response is already saved and current.";
        builderState.promptMessageState = draft.dirty ? "warning" : "ready";
        relay.responseMessage = draft.dirty
            ? "Prompt Director response applied — UNSAVED. Review and save the Final Prompt."
            : "This Prompt Director response is already saved and current.";
        relay.responseMessageState = "success";
    } catch (error) {
        if (isActive(root) && builderState.currentProject?.project_id === projectId) {
            console.error("[Music Video Builder] Custom GPT response validation failed.", error);
            relay.responseMessage = error instanceof Error ? error.message : "Custom GPT response could not be applied.";
            relay.responseMessageState = "error";
        }
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root, { promptViewport });
            if (changedCandidateApplied) {
                window.requestAnimationFrame(() => revealPromptReviewSection(root, sceneId, generationMethod));
            }
        }
    }
}

async function savePrompt(root, sceneId, generationMethod) {
    if (!isActive(root) || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    const item = promptItemFor(sceneId, generationMethod);
    const draft = item ? promptDraftFor(sceneId, generationMethod, item) : null;
    if (!item || !canSavePromptDraft(item, draft)) {
        return;
    }
    const projectId = builderState.currentProject.project_id;
    const key = promptDraftKey(sceneId, generationMethod);
    const promptViewport = capturePromptViewport(root, sceneId);
    builderState.operation = "prompt-save";
    builderState.promptMessage = "";
    renderProjectState(root, { promptViewport });
    let succeeded = false;
    try {
        const project = await fetchJson(promptPath(projectId, sceneId), {
            method: "PUT",
            body: JSON.stringify({
                generation_method: generationMethod,
                final_prompt: draft.text,
                source_fingerprint: draft.sourceFingerprint,
                relay_fingerprint: draft.relayFingerprint,
            }),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId || !hasProjectDocument(project)) {
            throw new Error("Prompt save response was invalid.");
        }
        setCurrentProject(root, project, { reconciledPromptKey: key });
        // Reconcile the visible card with the authoritative save response so
        // the badge and Final Prompt section never flash through a pre-save
        // snapshot before the silent prompt-card refresh lands.
        const savedRecord = project.prompts?.scenes
            ?.find((entry) => entry.scene_id === sceneId)
            ?.[generationMethod];
        const savedItem = promptItemFor(sceneId, generationMethod);
        if (savedRecord
            && savedItem
            && savedRecord.source_fingerprint === savedItem.source_fingerprint
            && savedRecord.relay_fingerprint === savedItem.source_fingerprint) {
            savedItem.saved_final_prompt = savedRecord.final_prompt;
            savedItem.saved_source_fingerprint = savedRecord.source_fingerprint;
            savedItem.saved_relay_fingerprint = savedRecord.relay_fingerprint;
            savedItem.status = "current";
            savedItem.source_validity = "current";
            savedItem.relay_validity = "current";
            savedItem.ready_for_render_prompt = Boolean(savedItem.visual_readiness?.ready);
        }
        builderState.activeView = "prompts";
        builderState.promptMessage = "Prompt saved.";
        builderState.promptMessageState = "success";
        succeeded = true;
        void loadProjectList(root);
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        console.error("[Music Video Builder] Prompt save failed.", error);
        builderState.promptMessage = error instanceof Error
            ? error.message
            : "Prompt could not be saved.";
        builderState.promptMessageState = "error";
        if (String(builderState.promptMessage).includes("Scene inputs changed")) {
            void loadPromptCards(root, true, { promptViewport });
        }
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            if (!succeeded) {
                builderState.promptListState = builderState.promptCards.length ? "ready" : builderState.promptListState;
            }
            renderProjectState(root, { promptViewport });
            if (succeeded) {
                void loadPromptCards(root, true, { promptViewport });
            }
        }
    }
}

function closeResourceDialogs(root) {
    root.querySelector("[data-mvb-character-dialog]").hidden = true;
    root.querySelector("[data-mvb-location-dialog]").hidden = true;
    if (builderState) {
        builderState.characterDialogFiles = [];
        builderState.locationDialogFiles = [];
        builderState.characterDialogReferenceIds = [];
        builderState.locationDialogReferenceIds = [];
    }
}

function openCharacterDialog(root, character = null) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    closeResourceDialogs(root);
    const dialog = root.querySelector("[data-mvb-character-dialog]");
    const form = root.querySelector("[data-mvb-character-form]");
    form.dataset.entityId = character?.character_id || "";
    root.querySelector("[data-mvb-character-dialog-title]").textContent = character ? "Edit Character" : "Add Character";
    root.querySelector("[data-mvb-character-submit]").textContent = character ? "Save Character" : "Add Character";
    root.querySelector("[data-mvb-character-name]").value = character?.name || "";
    root.querySelector("[data-mvb-character-role]").value = character?.role || "performer";
    root.querySelector("[data-mvb-character-appearance]").value = character?.appearance || "";
    root.querySelector("[data-mvb-character-outfit]").value = character?.outfit || "";
    root.querySelector("[data-mvb-character-error]").hidden = true;
    builderState.characterDialogFiles = [];
    builderState.characterDialogReferenceIds = (character?.references || []).map((reference) => reference.reference_id);
    renderResourceDialogReferences(root, "characters", character);
    dialog.hidden = false;
    window.requestAnimationFrame(() => root.querySelector("[data-mvb-character-name]").focus());
}

function openLocationDialog(root, location = null) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    closeResourceDialogs(root);
    const dialog = root.querySelector("[data-mvb-location-dialog]");
    const form = root.querySelector("[data-mvb-location-form]");
    form.dataset.entityId = location?.location_id || "";
    root.querySelector("[data-mvb-location-dialog-title]").textContent = location ? "Edit Location" : "Add Location";
    root.querySelector("[data-mvb-location-submit]").textContent = location ? "Save Location" : "Add Location";
    root.querySelector("[data-mvb-location-name]").value = location?.name || "";
    root.querySelector("[data-mvb-location-description]").value = location?.description || "";
    root.querySelector("[data-mvb-location-error]").hidden = true;
    builderState.locationDialogFiles = [];
    builderState.locationDialogReferenceIds = (location?.references || []).map((reference) => reference.reference_id);
    renderResourceDialogReferences(root, "locations", location);
    dialog.hidden = false;
    window.requestAnimationFrame(() => root.querySelector("[data-mvb-location-name]").focus());
}

async function runResourceMutation(root, operation, task, failureMessage) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return false;
    }
    builderState.operation = operation;
    builderState.storyboardMessage = "";
    renderProjectState(root);
    let succeeded = false;
    const activeView = builderState.activeView;
    try {
        if (!await flushCurrentProject(root)) {
            builderState.storyboardMessage = "Save the current project before continuing.";
            return false;
        }
        const project = await task();
        if (!hasProjectDocument(project)) {
            throw new Error("Resource mutation response was invalid.");
        }
        setCurrentProject(root, project);
        builderState.activeView = activeView;
        refreshOpenResourceDialog(root);
        builderState.deleteConfirm = null;
        builderState.storyboardRequestStale = true;
        builderState.storyboardAppliedStale = Boolean(project.storyboard?.scenes?.length);
        void loadProjectList(root);
        void loadPromptCards(root, true);
        succeeded = true;
        return true;
    } catch (error) {
        if (!isActive(root)) {
            return false;
        }
        console.error(`[Music Video Builder] ${operation} operation failed.`, error);
        builderState.storyboardMessage = failureMessage;
        return false;
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            if (succeeded) {
                builderState.storyboardMessage = "";
            }
            renderProjectState(root);
        }
    }
}

function findResource(project, kind, entityId) {
    return kind === "characters"
        ? findCharacter(project, entityId)
        : findLocation(project, entityId);
}

async function rollbackUploadedResourceReferences(projectId, kind, entityId, referenceIds) {
    for (const referenceId of [...referenceIds].reverse()) {
        try {
            await fetchJson(
                `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}/references/${encodeURIComponent(referenceId)}`,
                { method: "DELETE" },
            );
        } catch (error) {
            console.error("[Music Video Builder] Resource reference rollback failed.", error);
        }
    }
}

async function saveExistingResourceDraft(projectId, kind, entityId, path, payload, retainedReferenceIds, pendingFiles) {
    const currentEntity = findResource(builderState.currentProject, kind, entityId);
    if (!currentEntity) {
        throw new Error("The edited resource is no longer available.");
    }
    const knownReferenceIds = new Set(currentEntity.references.map((reference) => reference.reference_id));
    const uploadedReferenceIds = [];
    try {
        for (const file of pendingFiles) {
            const project = await uploadReferenceFile(projectId, kind, entityId, file);
            if (!hasProjectDocument(project)) {
                throw new Error("Resource reference response was invalid.");
            }
            const updatedEntity = findResource(project, kind, entityId);
            const addedReferences = updatedEntity?.references.filter(
                (reference) => !knownReferenceIds.has(reference.reference_id),
            ) || [];
            if (addedReferences.length !== 1) {
                throw new Error("Resource reference identity could not be reconciled.");
            }
            const addedReferenceId = addedReferences[0].reference_id;
            knownReferenceIds.add(addedReferenceId);
            uploadedReferenceIds.push(addedReferenceId);
        }
        const project = await fetchJson(path, {
            method: "PUT",
            body: JSON.stringify({
                ...payload,
                reference_ids: [...retainedReferenceIds, ...uploadedReferenceIds],
            }),
        });
        if (!hasProjectDocument(project)) {
            throw new Error("Resource edit response was invalid.");
        }
        return project;
    } catch (error) {
        await rollbackUploadedResourceReferences(projectId, kind, entityId, uploadedReferenceIds);
        throw error;
    }
}

async function saveCharacter(root) {
    const form = root.querySelector("[data-mvb-character-form]");
    const errorElement = root.querySelector("[data-mvb-character-error]");
    const characterId = form.dataset.entityId;
    const projectId = builderState.currentProject.project_id;
    errorElement.textContent = "";
    errorElement.hidden = true;
    const payload = {
        name: root.querySelector("[data-mvb-character-name]").value,
        role: root.querySelector("[data-mvb-character-role]").value,
        appearance: root.querySelector("[data-mvb-character-appearance]").value,
        outfit: root.querySelector("[data-mvb-character-outfit]").value,
    };
    const path = `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/characters${characterId ? `/${encodeURIComponent(characterId)}` : ""}`;
    const pendingFiles = [...builderState.characterDialogFiles];
    const retainedReferenceIds = [...builderState.characterDialogReferenceIds];
    const saved = await runResourceMutation(
        root,
        "character",
        async () => {
            if (characterId) {
                return saveExistingResourceDraft(
                    projectId,
                    "characters",
                    characterId,
                    path,
                    payload,
                    retainedReferenceIds,
                    pendingFiles,
                );
            }
            let project = await fetchJson(path, { method: characterId ? "PUT" : "POST", body: JSON.stringify(payload) });
            if (!hasProjectDocument(project)) {
                throw new Error("Character creation response was invalid.");
            }
            if (!characterId && pendingFiles.length) {
                const created = project.characters[project.characters.length - 1];
                try {
                    for (const file of pendingFiles) {
                        project = await uploadReferenceFile(projectId, "characters", created.character_id, file);
                        if (!hasProjectDocument(project)) {
                            throw new Error("Character reference response was invalid.");
                        }
                    }
                } catch (error) {
                    try {
                        await fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(created.character_id)}`, { method: "DELETE" });
                    } catch (rollbackError) {
                        console.error("[Music Video Builder] Character creation rollback failed.", rollbackError);
                    }
                    throw error;
                }
            }
            return project;
        },
        "Character could not be saved.",
    );
    if (!saved && isActive(root)) {
        errorElement.textContent = builderState.storyboardMessage || "Character could not be saved.";
        errorElement.hidden = false;
    }
    if (saved && isActive(root)) {
        closeResourceDialogs(root);
    }
}

async function saveLocation(root) {
    const form = root.querySelector("[data-mvb-location-form]");
    const errorElement = root.querySelector("[data-mvb-location-error]");
    const locationId = form.dataset.entityId;
    const projectId = builderState.currentProject.project_id;
    errorElement.textContent = "";
    errorElement.hidden = true;
    const payload = {
        name: root.querySelector("[data-mvb-location-name]").value,
        description: root.querySelector("[data-mvb-location-description]").value,
    };
    const path = `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/locations${locationId ? `/${encodeURIComponent(locationId)}` : ""}`;
    const pendingFiles = [...builderState.locationDialogFiles];
    const retainedReferenceIds = [...builderState.locationDialogReferenceIds];
    const saved = await runResourceMutation(
        root,
        "location",
        async () => {
            if (locationId) {
                return saveExistingResourceDraft(
                    projectId,
                    "locations",
                    locationId,
                    path,
                    payload,
                    retainedReferenceIds,
                    pendingFiles,
                );
            }
            let project = await fetchJson(path, { method: locationId ? "PUT" : "POST", body: JSON.stringify(payload) });
            if (!hasProjectDocument(project)) {
                throw new Error("Location creation response was invalid.");
            }
            if (!locationId && pendingFiles.length) {
                const created = project.locations[project.locations.length - 1];
                try {
                    for (const file of pendingFiles) {
                        project = await uploadReferenceFile(projectId, "locations", created.location_id, file);
                        if (!hasProjectDocument(project)) {
                            throw new Error("Location reference response was invalid.");
                        }
                    }
                } catch (error) {
                    try {
                        await fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}/locations/${encodeURIComponent(created.location_id)}`, { method: "DELETE" });
                    } catch (rollbackError) {
                        console.error("[Music Video Builder] Location creation rollback failed.", rollbackError);
                    }
                    throw error;
                }
            }
            return project;
        },
        "Location could not be saved.",
    );
    if (!saved && isActive(root)) {
        errorElement.textContent = builderState.storyboardMessage || "Location could not be saved.";
        errorElement.hidden = false;
    }
    if (saved && isActive(root)) {
        closeResourceDialogs(root);
    }
}

function uploadReferenceFile(projectId, kind, entityId, file) {
    return fetchJson(
        `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}/references`,
        { method: "POST", body: uploadFormData(file) },
    );
}

async function deleteEntity(root, kind, entityId) {
    const projectId = builderState.currentProject.project_id;
    await runResourceMutation(
        root,
        kind === "characters" ? "character" : "location",
        () => fetchJson(
            `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}`,
            { method: "DELETE" },
        ),
        kind === "characters" ? "Character could not be deleted." : "Location could not be deleted.",
    );
}

function switchView(root, view) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    if (view !== "setup" && view !== "storyboard" && view !== "visuals" && view !== "prompts" && view !== "render") {
        return;
    }
    clearGptLauncherErrors();
    builderState.activeView = view;
    builderState.deleteConfirm = null;
    renderProjectState(root);
    if (view === "prompts") {
        void loadPromptCards(root);
    }
    if (view === "render") {
        void loadPromptCards(root, true).then(() => loadRenderPreflight(root));
    }
}

function renderProjectState(root, options = {}) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
    const promptViewport = options.promptViewport
        || (state.activeView === "prompts" ? capturePromptViewport(root, options.promptAnchorSceneId) : null);
    renderMaximizeState(root);
    const currentProject = state.currentProject;
    const landingHeader = root.querySelector("[data-mvb-landing-header]");
    const projectToolbar = root.querySelector("[data-mvb-project-toolbar]");
    const landing = root.querySelector("[data-mvb-landing]");
    const viewNav = root.querySelector("[data-mvb-view-nav]");
    const setupView = root.querySelector("[data-mvb-setup-view]");
    const setup = root.querySelector("[data-mvb-setup]");
    const sceneReview = root.querySelector("[data-mvb-scene-review]");
    const storyboard = root.querySelector("[data-mvb-storyboard]");
    const visuals = root.querySelector("[data-mvb-visuals]");
    const prompts = root.querySelector("[data-mvb-prompts]");
    const render = root.querySelector("[data-mvb-render]");
    const nameInput = root.querySelector("[data-mvb-project-name]");
    const saveButton = root.querySelector("[data-mvb-save]");
    const projectsButton = root.querySelector("[data-mvb-projects]");
    const newButtons = root.querySelectorAll("[data-mvb-new]");
    const openButtons = root.querySelectorAll("[data-mvb-open]");
    const saveState = root.querySelector("[data-mvb-save-state]");
    const characterSubmit = root.querySelector("[data-mvb-character-submit]");
    const locationSubmit = root.querySelector("[data-mvb-location-submit]");

    if (!currentProject) {
        root.classList.remove("mvb-project-open");
        landingHeader.hidden = false;
        projectToolbar.hidden = true;
        landing.hidden = false;
        viewNav.hidden = true;
        setupView.hidden = true;
        storyboard.hidden = true;
        visuals.hidden = true;
        prompts.hidden = true;
        render.hidden = true;
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
        viewNav.hidden = false;
        setupView.hidden = state.activeView !== "setup";
        storyboard.hidden = state.activeView !== "storyboard";
        visuals.hidden = state.activeView !== "visuals";
        prompts.hidden = state.activeView !== "prompts";
        render.hidden = state.activeView !== "render";
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
    characterSubmit.disabled = Boolean(state.operation) || state.closing || state.transitioning;
    locationSubmit.disabled = Boolean(state.operation) || state.closing || state.transitioning;
    for (const button of root.querySelectorAll("[data-mvb-view]")) {
        const selected = currentProject && button.dataset.mvbView === state.activeView;
        button.classList.toggle("mvb-view-active", Boolean(selected));
        button.setAttribute("aria-selected", String(Boolean(selected)));
    }
    renderRecentProjectList(root);
    if (currentProject) {
        renderSetupState(root);
        renderStoryboardState(root);
        renderVisualsState(root, options.visualViewport || null);
        renderPromptState(root, { promptViewport });
        renderRenderState(root);
    } else {
        renderSceneReview(root);
        renderStoryboardState(root);
        renderVisualsState(root, options.visualViewport || null);
        renderPromptState(root, { promptViewport });
        renderRenderState(root);
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
    const invalidCount = state.invalidProjects.length;
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

function appendInvalidProjectNotice(root, list, location) {
    if (!isActive(root) || builderState.invalidProjects.length === 0) {
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

    if (builderState.invalidIgnoreMessage) {
        const errorMessage = document.createElement("p");
        errorMessage.className = "mvb-invalid-projects-error";
        errorMessage.setAttribute("role", "alert");
        errorMessage.textContent = builderState.invalidIgnoreMessage;
        item.append(message, errorMessage);
    }

    const actions = document.createElement("div");
    actions.className = "mvb-invalid-projects-actions";

    const detailsId = "mvb-invalid-project-details-" + location;
    const detailsButton = document.createElement("button");
    detailsButton.className = "mvb-button mvb-button-secondary";
    detailsButton.type = "button";
    detailsButton.textContent = "Details";
    detailsButton.setAttribute("aria-controls", detailsId);
    detailsButton.setAttribute("aria-expanded", "false");

    const addIgnoreButton = (invalidProject, parent) => {
        const ignoreButton = document.createElement("button");
        ignoreButton.className = "mvb-button mvb-button-secondary";
        ignoreButton.type = "button";
        ignoreButton.textContent = "Ignore";
        ignoreButton.disabled = builderState.invalidIgnoreBusy;
        ignoreButton.addEventListener("click", () => void ignoreInvalidProject(root, invalidProject));
        parent.append(ignoreButton);
    };

    if (count === 1) {
        addIgnoreButton(builderState.invalidProjects[0], actions);
    }

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
        if (count > 1) {
            addIgnoreButton(invalidProject, entry);
        }
        details.append(entry);
    }

    detailsButton.addEventListener("click", () => {
        const expanded = details.hidden;
        details.hidden = !expanded;
        detailsButton.textContent = expanded ? "Hide Details" : "Details";
        detailsButton.setAttribute("aria-expanded", String(expanded));
    });
    actions.prepend(detailsButton);
    if (!builderState.invalidIgnoreMessage) {
        item.append(message);
    }
    item.append(actions, details);
    list.append(item);
}

async function ignoreInvalidProject(root, invalidProject) {
    if (!isActive(root) || builderState.invalidIgnoreBusy || !invalidProject) {
        return;
    }

    const folderName = invalidProject.folder_name;
    const signature = invalidProject.signature;
    if (typeof folderName !== "string" || typeof signature !== "string") {
        builderState.invalidIgnoreMessage = "Invalid project entry could not be ignored.";
        renderRecentProjectList(root);
        renderOpenProjectList(root);
        return;
    }

    builderState.invalidIgnoreBusy = true;
    builderState.invalidIgnoreMessage = "";
    renderRecentProjectList(root);
    renderOpenProjectList(root);
    try {
        await fetchJson(`${PROJECTS_PATH}/invalid/ignore`, {
            method: "POST",
            body: JSON.stringify({ folder_name: folderName, signature }),
        });
        if (!isActive(root)) {
            return;
        }
        await loadProjectList(root);
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Invalid project Ignore failed.", error);
        builderState.invalidIgnoreMessage = error instanceof Error
            ? error.message
            : "Invalid project entry could not be ignored.";
    } finally {
        if (isActive(root)) {
            builderState.invalidIgnoreBusy = false;
            updateProjectListSummary(root);
            renderRecentProjectList(root);
            renderOpenProjectList(root);
        }
    }
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
        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "mvb-button mvb-button-danger mvb-button-small mvb-project-delete";
        deleteButton.textContent = "Delete";
        deleteButton.title = `Delete ${project.name}`;
        deleteButton.disabled = state.openingProject || state.transitioning || state.closing || state.projectDeleteBusy;
        deleteButton.addEventListener("click", (event) => {
            event.stopPropagation();
            openProjectDeleteDialog(root, project);
        });

        const row = document.createElement("div");
        row.className = "mvb-project-row";
        row.append(button, deleteButton);
        item.append(row);
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
    builderState.invalidIgnoreMessage = "";
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
    closeProjectDeleteDialog(root);
    closeStoryboardClearDialog(root);
}

function closeProjectDeleteDialog(root) {
    const dialog = root.querySelector("[data-mvb-delete-dialog]");
    if (dialog) {
        dialog.hidden = true;
    }
    if (builderState) {
        builderState.projectDeleteTarget = null;
    }
}

function openProjectDeleteDialog(root, project) {
    if (
        !isActive(root)
        || builderState.currentProject
        || builderState.transitioning
        || builderState.closing
        || builderState.operation
        || builderState.projectDeleteBusy
        || !project
    ) {
        return;
    }

    closeProjectDialogs(root);
    closeResourceDialogs(root);
    builderState.projectDeleteTarget = project;
    const dialog = root.querySelector("[data-mvb-delete-dialog]");
    const projectName = root.querySelector("[data-mvb-delete-name]");
    const error = root.querySelector("[data-mvb-delete-error]");
    projectName.textContent = project.name;
    error.textContent = "";
    error.hidden = true;
    dialog.hidden = false;
    window.requestAnimationFrame(() => root.querySelector("[data-mvb-cancel-delete]")?.focus());
}

async function deleteProject(root) {
    if (!isActive(root) || builderState.projectDeleteBusy || !builderState.projectDeleteTarget) {
        return;
    }

    const target = builderState.projectDeleteTarget;
    const error = root.querySelector("[data-mvb-delete-error]");
    const submit = root.querySelector("[data-mvb-confirm-delete]");
    const cancel = root.querySelector("[data-mvb-cancel-delete]");
    builderState.projectDeleteBusy = true;
    error.textContent = "";
    error.hidden = true;
    submit.disabled = true;
    cancel.disabled = true;
    renderRecentProjectList(root);

    try {
        const result = await fetchJson(`${PROJECTS_PATH}/${encodeURIComponent(target.project_id)}`, {
            method: "DELETE",
        });
        if (!result || result.deleted !== true || result.project_id !== target.project_id) {
            throw new Error("Project could not be deleted.");
        }
        if (!isActive(root)) {
            return;
        }
        closeProjectDeleteDialog(root);
        await loadProjectList(root);
    } catch (requestError) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Project deletion failed.", requestError);
        error.textContent = requestError instanceof Error
            ? requestError.message
            : "Project could not be deleted.";
        error.hidden = false;
    } finally {
        if (isActive(root)) {
            builderState.projectDeleteBusy = false;
            submit.disabled = false;
            cancel.disabled = false;
            renderRecentProjectList(root);
        }
    }
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
        if (!await flushProjectTransition(root)) {
            return;
        }
        if (!isActive(root)) {
            return;
        }

        closeProjectDialogs(root);
        closeResourceDialogs(root);
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
        builderState.activeView = "setup";
        setCurrentProject(root, project);
        closeProjectDialogs(root);
        renderProjectState(root);
        void loadStoryboardRequest(root, true);
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
        closeResourceDialogs(root);
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
        if (!await flushProjectTransition(root)) {
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
        builderState.activeView = "setup";
        setCurrentProject(root, project);
        closeProjectDialogs(root);
        renderProjectState(root);
        if (project.scenes?.length && project.storyboard?.scenes?.length) {
            void loadStoryboardRequest(root, true);
        }
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
        void loadPromptCards(root, true);
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

function storyboardRequestPath(projectId, suffix = "request") {
    const suffixPath = suffix ? `/${suffix}` : "";
    return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/storyboard${suffixPath}`;
}

async function loadStoryboardRequest(root, silent = false) {
    if (!isActive(root) || !builderState.currentProject?.scenes?.length) {
        return false;
    }
    const projectId = builderState.currentProject.project_id;
    try {
        const request = await fetchJson(storyboardRequestPath(projectId));
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        if (!request || typeof request !== "object" || typeof request.request_fingerprint !== "string") {
            throw new Error("Storyboard request response was invalid.");
        }
        builderState.storyboardRequest = request;
        builderState.storyboardRequestStale = false;
        builderState.storyboardAppliedStale = Boolean(builderState.currentProject.storyboard?.scenes?.length)
            && builderState.currentProject.storyboard.request_fingerprint !== request.request_fingerprint;
        if (!silent) {
            setStoryboardRelayMessage("Request refreshed.");
        }
        renderProjectState(root);
        return true;
    } catch (error) {
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return false;
        }
        console.error("[Music Video Builder] Storyboard request load failed.", error);
        if (!silent) {
            setStoryboardRelayMessage(error instanceof Error
                ? error.message
                : "Storyboard request could not be built.", "error");
            renderProjectState(root);
        }
        return false;
    }
}

async function generateStoryboardRequest(root) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    builderState.operation = "storyboard-request";
    setStoryboardRelayMessage("");
    renderProjectState(root);
    const projectId = builderState.currentProject.project_id;
    try {
        if (!await flushCurrentProject(root)) {
            setStoryboardRelayMessage("Save the current project before generating a request.", "error");
            return;
        }
        const request = await fetchJson(storyboardRequestPath(projectId));
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        if (!request || typeof request !== "object" || typeof request.request_fingerprint !== "string") {
            throw new Error("Storyboard request response was invalid.");
        }
        builderState.storyboardRequest = request;
        builderState.storyboardRequestStale = false;
        builderState.storyboardAppliedStale = Boolean(builderState.currentProject.storyboard?.scenes?.length)
            && builderState.currentProject.storyboard.request_fingerprint !== request.request_fingerprint;
        builderState.storyboardResponseText = "";
        builderState.storyboardPreview = null;
        setStoryboardRelayMessage("Request ready to copy.");
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Storyboard request generation failed.", error);
        setStoryboardRelayMessage(error instanceof Error
            ? error.message
            : "Storyboard request could not be built.", "error");
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root);
        }
    }
}

async function copyStoryboardRequest(root) {
    if (!isActive(root) || !canCopyStoryboardRequest(
        builderState.storyboardRequest,
        builderState.storyboardRequestStale,
    )) {
        return;
    }
    const requestJson = storyboardRequestJson(builderState.storyboardRequest);
    try {
        if (!navigator.clipboard?.writeText) {
            throw new Error("Clipboard access is unavailable; select the visible request JSON to copy it.");
        }
        await navigator.clipboard.writeText(requestJson);
        setStoryboardRelayMessage("REQUEST COPIED");
    } catch (error) {
        console.error("[Music Video Builder] Storyboard request copy failed.", error);
        setStoryboardRelayMessage(error instanceof Error
            ? error.message
            : "Request JSON could not be copied; select the visible payload.", "error");
    }
    renderProjectState(root);
}

async function validateStoryboardResponseInput(root) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    const responseText = builderState.storyboardResponseText;
    let response;
    try {
        response = JSON.parse(responseText);
    } catch (error) {
        console.error("[Music Video Builder] Storyboard response JSON parse failed.", error);
        builderState.storyboardPreview = null;
        setStoryboardRelayMessage("Response JSON is not valid JSON.", "error");
        renderProjectState(root);
        return;
    }

    builderState.operation = "storyboard-validate";
    setStoryboardRelayMessage("");
    renderProjectState(root);
    const projectId = builderState.currentProject.project_id;
    try {
        const preview = await fetchJson(storyboardRequestPath(projectId, "validate"), {
            method: "POST",
            body: JSON.stringify(response),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId) {
            return;
        }
        builderState.storyboardPreview = preview;
        setStoryboardRelayMessage("");
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Storyboard response validation failed.", error);
        builderState.storyboardPreview = null;
        setStoryboardRelayMessage(error instanceof Error
            ? error.message
            : "Storyboard response could not be validated.", "error");
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root);
        }
    }
}

async function applyStoryboard(root) {
    if (!isActive(root) || !builderState.currentProject || !builderState.storyboardPreview || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    let response;
    try {
        response = JSON.parse(builderState.storyboardResponseText);
    } catch (error) {
        setStoryboardRelayMessage("Response JSON is not valid JSON.", "error");
        renderProjectState(root);
        return;
    }

    builderState.operation = "storyboard-apply";
    setStoryboardRelayMessage("");
    renderProjectState(root);
    const projectId = builderState.currentProject.project_id;
    const request = builderState.storyboardRequest;
    try {
        if (!await flushCurrentProject(root)) {
            setStoryboardRelayMessage("Save the current project before applying the storyboard.", "error");
            return;
        }
        const project = await fetchJson(storyboardRequestPath(projectId, "apply"), {
            method: "POST",
            body: JSON.stringify(response),
        });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId || !hasProjectDocument(project)) {
            throw new Error("Storyboard apply response was invalid.");
        }
        setCurrentProject(root, project);
        builderState.storyboardRequest = request;
        builderState.storyboardRequestStale = false;
        builderState.storyboardAppliedStale = false;
        setStoryboardRelayMessage("Storyboard applied.");
        void loadProjectList(root);
        void loadPromptCards(root, true);
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Storyboard apply failed.", error);
        setStoryboardRelayMessage(error instanceof Error
            ? error.message
            : "Storyboard could not be applied.", "error");
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root);
        }
    }
}

function openStoryboardClearDialog(root) {
    if (!isActive(root) || !builderState.currentProject?.storyboard?.scenes?.length || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    root.querySelector("[data-mvb-clear-storyboard-dialog]").hidden = false;
    root.querySelector("[data-mvb-cancel-clear-storyboard]").focus();
}

function closeStoryboardClearDialog(root) {
    if (!isActive(root)) {
        return;
    }
    root.querySelector("[data-mvb-clear-storyboard-dialog]").hidden = true;
}

async function clearAppliedStoryboard(root) {
    if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
        return;
    }
    builderState.operation = "storyboard-clear";
    setStoryboardRelayMessage("");
    renderProjectState(root);
    const projectId = builderState.currentProject.project_id;
    try {
        if (!await flushCurrentProject(root)) {
            setStoryboardRelayMessage("Save the current project before clearing the storyboard.", "error");
            return;
        }
        const project = await fetchJson(storyboardRequestPath(projectId, ""), { method: "DELETE" });
        if (!isActive(root) || builderState.currentProject?.project_id !== projectId || !hasProjectDocument(project)) {
            throw new Error("Storyboard clear response was invalid.");
        }
        setCurrentProject(root, project);
        setStoryboardRelayMessage("Storyboard cleared.");
        closeStoryboardClearDialog(root);
        void loadPromptCards(root, true);
    } catch (error) {
        if (!isActive(root)) {
            return;
        }
        console.error("[Music Video Builder] Storyboard clear failed.", error);
        setStoryboardRelayMessage(error instanceof Error
            ? error.message
            : "Storyboard could not be cleared.", "error");
    } finally {
        if (isActive(root)) {
            builderState.operation = null;
            renderProjectState(root);
        }
    }
}

async function flushPromptDrafts(root) {
    if (!isActive(root) || !builderState.currentProject) {
        return true;
    }
    const dirtyKeys = Object.entries(builderState.promptDrafts || {})
        .filter(([, draft]) => Boolean(draft?.dirty))
        .map(([key]) => key);
    for (const key of dirtyKeys) {
        if (!isActive(root) || !builderState.currentProject) {
            return false;
        }
        const [sceneId, generationMethod] = key.split(":");
        const draft = builderState.promptDrafts[key];
        if (!draft || !["keyframe_i2v", "reference2video"].includes(generationMethod)) {
            builderState.saveState = "error";
            builderState.saveMessage = "Prompt draft could not be identified — retry Save Prompt";
            return false;
        }
        try {
            const projectId = builderState.currentProject.project_id;
            const project = await fetchJson(promptPath(projectId, sceneId), {
                method: "PUT",
                body: JSON.stringify({
                    generation_method: generationMethod,
                    final_prompt: draft.text,
                    source_fingerprint: draft.sourceFingerprint,
                    relay_fingerprint: draft.relayFingerprint,
                }),
            });
            if (!isActive(root) || builderState.currentProject?.project_id !== projectId || !hasProjectDocument(project)) {
                return false;
            }
            setCurrentProject(root, project, { reconciledPromptKey: key });
        } catch (error) {
            if (!isActive(root)) {
                return false;
            }
            const message = error instanceof Error ? error.message : "";
            const unsavableDraft = message.includes("Scene inputs changed")
                || message.includes("current Prompt Director response must be applied");
            if (unsavableDraft) {
                // A stale draft cannot be persisted against changed scene
                // inputs; keep the saved prompt untouched and continue the
                // transition instead of deadlocking on prompt readiness.
                continue;
            }
            console.error("[Music Video Builder] Prompt draft flush failed.", error);
            builderState.saveState = "error";
            builderState.saveMessage = error instanceof Error
                ? error.message
                : "Prompt draft could not be saved — retry Save Prompt";
            return false;
        }
    }
    return true;
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
        if (hadPendingTimer || hasPendingProjectSave(state)) {
            const saved = await saveCurrentProject(root);
            if (!isActive(root) || !saved) {
                return false;
            }
        }
        return true;
    }

    return isActive(root);
}

async function flushProjectTransition(root) {
    if (!await flushCurrentProject(root)) {
        return false;
    }
    if (hasUnsavedPromptDrafts(builderState)) {
        return await flushPromptDrafts(root);
    }
    return true;
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
                            <p class="mvb-phase">Phase 4 · Storyboard relay</p>
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

                <nav class="mvb-view-nav" data-mvb-view-nav role="tablist" aria-label="Project views" hidden>
                    <button class="mvb-view-button" data-mvb-view="setup" type="button" role="tab" aria-selected="true">Setup</button>
                    <button class="mvb-view-button" data-mvb-view="storyboard" type="button" role="tab" aria-selected="false">Storyboard</button>
                    <button class="mvb-view-button" data-mvb-view="visuals" type="button" role="tab" aria-selected="false">Visuals</button>
                    <button class="mvb-view-button" data-mvb-view="prompts" type="button" role="tab" aria-selected="false">Prompts</button>
                    <button class="mvb-view-button" data-mvb-view="render" type="button" role="tab" aria-selected="false">Renders</button>
                </nav>

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

                <div class="mvb-setup-view" data-mvb-setup-view hidden>
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
                </div>

                <section class="mvb-visuals" data-mvb-visuals aria-labelledby="mvb-visuals-heading" hidden>
                    <div class="mvb-visuals-heading">
                        <div>
                            <p class="mvb-eyebrow">Visuals</p>
                            <h2 id="mvb-visuals-heading">Scene visual workflow</h2>
                        </div>
                        <span class="mvb-visual-status" data-mvb-visual-status aria-live="polite"></span>
                    </div>
                    <p class="mvb-visual-intro">Prepare one generation method and its scene-local inputs for each applied storyboard scene.</p>
                    <div class="mvb-visual-bulk" data-mvb-visual-bulk>
                        <label class="mvb-field mvb-visual-bulk-field">
                            <span>Generation Method for All Scenes</span>
                            <select data-mvb-visual-bulk-method aria-label="Generation Method for All Scenes">
                                <option value="keyframe_i2v">Keyframe / Image-to-Video</option>
                                <option value="reference2video">Reference-to-Video</option>
                            </select>
                        </label>
                        <button class="mvb-button mvb-button-secondary" data-mvb-visual-bulk-apply type="button">Apply to All Scenes</button>
                    </div>
                    <p class="mvb-visual-empty" data-mvb-visual-empty hidden>Build scenes before preparing Visuals.</p>
                    <div class="mvb-visual-scenes" data-mvb-visual-scenes></div>
                </section>

                <section class="mvb-prompts" data-mvb-prompts aria-labelledby="mvb-prompts-heading" hidden>
                    <div class="mvb-prompts-heading">
                        <div>
                            <p class="mvb-eyebrow">Prompts</p>
                            <h2 id="mvb-prompts-heading">Final Prompts</h2>
                        </div>
                        <span class="mvb-prompt-status-message" data-mvb-prompt-status aria-live="polite"></span>
                    </div>
                    <p class="mvb-prompt-intro">Generate, copy, open, paste, and apply each scene request, then review and save its Final Prompt.</p>
                    <p class="mvb-prompt-empty" data-mvb-prompt-empty hidden>Build scenes before preparing Prompts.</p>
                    <div class="mvb-prompt-scenes" data-mvb-prompt-scenes></div>
                </section>

                <section class="mvb-render" data-mvb-render aria-labelledby="mvb-render-heading" hidden>
                    <header class="mvb-render-heading">
                        <h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>
                        <div class="mvb-render-upscale-control">
                            <label class="mvb-render-upscale-label" for="mvb-render-upscale-select">Upscale</label>
                            <select class="mvb-select mvb-render-upscale-select" id="mvb-render-upscale-select" data-mvb-render-upscale-select>
                                <option value="none">None</option>
                                <option value="rtx_vsr_fast">RTX VSR — Fast</option>
                                <option value="seedvr2_quality">SeedVR2 — Quality</option>
                            </select>
                            <span class="mvb-render-upscale-note" data-mvb-render-upscale-note></span>
                        </div>
                        <span class="mvb-render-status" data-mvb-render-status aria-live="polite"></span>
                        <button class="mvb-button mvb-button-primary mvb-button-small" data-mvb-batch-all type="button">Render All Ready</button>
                        <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-batch-select type="button">Select Scenes</button>
                        <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-render-refresh type="button">Refresh Preflight</button>
                        <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-render-rescan type="button">Rescan Runtime</button>
                    </header>
                    <div class="mvb-render-selection" data-mvb-render-selection hidden></div>
                    <div class="mvb-batch-panel" data-mvb-batch-panel hidden aria-live="polite"></div>
                    <p class="mvb-render-empty" data-mvb-render-empty hidden>Build scenes before preparing Render inputs.</p>
                    <div class="mvb-render-scenes" data-mvb-render-scenes></div>
                </section>

                <div class="mvb-modal-backdrop" data-mvb-batch-confirm hidden>
                    <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-batch-confirm-heading">
                        <div class="mvb-dialog-heading">
                            <div>
                                <p class="mvb-eyebrow">Batch render</p>
                                <h2 id="mvb-batch-confirm-heading" data-mvb-batch-confirm-title>Render scenes?</h2>
                            </div>
                            <button class="mvb-dialog-close" data-mvb-batch-confirm-cancel type="button">Cancel</button>
                        </div>
                        <p data-mvb-batch-confirm-summary class="mvb-dialog-note" hidden></p>
                        <p class="mvb-dialog-note">Scenes will be processed one at a time in scene order. Render inputs will be prepared automatically where needed and successful raw renders will be finalized automatically.</p>
                        <p class="mvb-dialog-error" data-mvb-batch-confirm-error role="alert" hidden></p>
                        <div class="mvb-dialog-actions">
                            <button class="mvb-button mvb-button-secondary" data-mvb-batch-confirm-cancel type="button">Cancel</button>
                            <button class="mvb-button mvb-button-primary" data-mvb-batch-confirm-start type="button">Start Batch</button>
                        </div>
                    </section>
                </div>

                <section class="mvb-storyboard" data-mvb-storyboard aria-labelledby="mvb-storyboard-heading" hidden>
                    <div class="mvb-storyboard-heading">
                        <div>
                            <p class="mvb-eyebrow">Storyboard resources</p>
                            <h2 id="mvb-storyboard-heading">Characters &amp; Locations</h2>
                        </div>
                        <span class="mvb-storyboard-status" data-mvb-storyboard-status aria-live="polite"></span>
                    </div>
                    <div class="mvb-storyboard-grid">
                        <section class="mvb-resource-panel" aria-labelledby="mvb-characters-heading">
                            <div class="mvb-resource-panel-heading">
                                <h3 id="mvb-characters-heading">Characters</h3>
                                <button class="mvb-button mvb-button-primary mvb-button-small" data-mvb-add-character type="button">Add Character</button>
                            </div>
                            <p class="mvb-resource-empty" data-mvb-character-empty>No characters yet.</p>
                            <div class="mvb-resource-list" data-mvb-character-list></div>
                        </section>
                        <section class="mvb-resource-panel" aria-labelledby="mvb-locations-heading">
                            <div class="mvb-resource-panel-heading">
                                <h3 id="mvb-locations-heading">Locations</h3>
                                <button class="mvb-button mvb-button-primary mvb-button-small" data-mvb-add-location type="button">Add Location</button>
                            </div>
                            <p class="mvb-resource-empty" data-mvb-location-empty>No locations yet.</p>
                            <div class="mvb-resource-list" data-mvb-location-list></div>
                        </section>
                    </div>
                    <section class="mvb-storyboard-relay" aria-labelledby="mvb-direction-heading">
                        <div class="mvb-relay-heading">
                            <div>
                                <p class="mvb-eyebrow">Direction</p>
                                <h3 id="mvb-direction-heading">Story direction</h3>
                            </div>
                            <span class="mvb-storyboard-relay-status" data-mvb-storyboard-relay-status aria-live="polite"></span>
                        </div>
                        <div class="mvb-direction-mode-row">
                            <label class="mvb-field mvb-storyboard-mode-field">
                                <span>Storyboard mode</span>
                                <select data-mvb-storyboard-mode aria-describedby="mvb-storyboard-mode-help">
                                    <option value="loose">Loose</option>
                                    <option value="strict">Strict</option>
                                    <option value="band_performance">Band Performance</option>
                                </select>
                            </label>
                            <p class="mvb-field-help" data-mvb-storyboard-mode-help id="mvb-storyboard-mode-help"></p>
                        </div>
                        <div class="mvb-direction-grid">
                            <label class="mvb-field">
                                <span>Story brief</span>
                                <textarea data-mvb-story-brief rows="4" maxlength="8000" placeholder="The story, emotional arc, or lyrical idea."></textarea>
                            </label>
                            <label class="mvb-field">
                                <span>Visual / directional notes</span>
                                <textarea data-mvb-visual-notes rows="4" maxlength="8000" placeholder="Overall visual language and directorial notes."></textarea>
                            </label>
                        </div>
                    </section>
                    <section class="mvb-storyboard-relay" aria-labelledby="mvb-relay-heading">
                        <div class="mvb-relay-heading">
                            <div>
                                <p class="mvb-eyebrow">ChatGPT relay</p>
                                <h3 id="mvb-relay-heading">Storyboard request</h3>
                            </div>
                            <div class="mvb-relay-actions">
                                <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-generate-request type="button">Generate / Refresh Request</button>
                                <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-copy-request type="button" disabled>Copy Request JSON</button>
                                <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-storyboard-open-gpt type="button" aria-label="Open Vesper Storyboard Director" title="Vesper Storyboard Director">Open GPT</button>
                            </div>
                        </div>
                        <details class="mvb-relay-disclosure">
                            <summary>View Request JSON</summary>
                            <label class="mvb-field mvb-relay-json-field">
                                <span>Request JSON</span>
                                <textarea data-mvb-request-json rows="8" readonly spellcheck="false">Generate a request to preview its JSON.</textarea>
                            </label>
                        </details>
                        <div class="mvb-relay-response-heading">
                            <label class="mvb-field mvb-relay-json-field">
                                <span>Paste response JSON</span>
                                <textarea data-mvb-response-json rows="10" spellcheck="false" placeholder="Paste the dedicated GPT storyboard response here."></textarea>
                            </label>
                            <div class="mvb-relay-actions mvb-relay-response-actions">
                                <button class="mvb-button mvb-button-primary mvb-button-small" data-mvb-validate-response type="button">Validate Response</button>
                                <button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-clear-paste type="button">Clear Paste</button>
                            </div>
                        </div>
                        <div class="mvb-storyboard-preview" data-mvb-storyboard-preview>
                            <div class="mvb-relay-heading">
                                <div>
                                    <p class="mvb-eyebrow">Validated preview / applied storyboard</p>
                                    <h3>Scene allocations</h3>
                                </div>
                                <span class="mvb-storyboard-preview-state" data-mvb-storyboard-preview-state aria-live="polite"></span>
                            </div>
                            <p class="mvb-relay-empty" data-mvb-storyboard-preview-empty>No applied scene allocations.</p>
                            <div class="mvb-storyboard-preview-rows" data-mvb-storyboard-preview-rows></div>
                            <div class="mvb-relay-actions mvb-relay-apply-actions">
                                <button class="mvb-button mvb-button-primary mvb-button-small" data-mvb-apply-storyboard type="button" disabled>Apply Storyboard</button>
                                <button class="mvb-button mvb-button-danger mvb-button-small" data-mvb-clear-storyboard type="button" disabled>Clear Storyboard</button>
                            </div>
                        </div>
                    </section>
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

            <div class="mvb-modal-backdrop" data-mvb-delete-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-delete-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">Delete project</p>
                            <h2 id="mvb-delete-heading">Delete project?</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-cancel-delete type="button">Cancel</button>
                    </div>
                    <p>Delete <strong data-mvb-delete-name></strong>?</p>
                    <p class="mvb-dialog-note">This removes the project-local files and cannot be undone.</p>
                    <p class="mvb-dialog-error" data-mvb-delete-error role="alert" hidden></p>
                    <div class="mvb-dialog-actions">
                        <button class="mvb-button mvb-button-secondary" data-mvb-cancel-delete type="button">Cancel</button>
                        <button class="mvb-button mvb-button-danger" data-mvb-confirm-delete type="button">Delete Project</button>
                    </div>
                </section>
            </div>

             <div class="mvb-modal-backdrop" data-mvb-character-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-character-dialog-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">Storyboard resource</p>
                            <h2 id="mvb-character-dialog-heading" data-mvb-character-dialog-title>Add Character</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-cancel-character type="button">Cancel</button>
                    </div>
                    <form data-mvb-character-form>
                        <label class="mvb-field">
                            <span>Name</span>
                            <input data-mvb-character-name type="text" maxlength="200" autocomplete="off" required>
                        </label>
                        <label class="mvb-field">
                            <span>Role</span>
                            <select data-mvb-character-role>
                                <option value="performer">Performer / Singer</option>
                                <option value="band_member">Band Member</option>
                                <option value="extra">Extra</option>
                            </select>
                        </label>
                        <label class="mvb-field">
                            <span>Appearance</span>
                            <textarea data-mvb-character-appearance rows="3" maxlength="5000"></textarea>
                        </label>
                         <label class="mvb-field">
                             <span>Outfit</span>
                             <textarea data-mvb-character-outfit rows="3" maxlength="5000"></textarea>
                         </label>
                         <div class="mvb-resource-dialog-references" data-mvb-character-references></div>
                         <p class="mvb-dialog-error" data-mvb-character-error role="alert" hidden></p>
                        <div class="mvb-dialog-actions">
                            <button class="mvb-button mvb-button-secondary" data-mvb-cancel-character type="button">Cancel</button>
                            <button class="mvb-button mvb-button-primary" data-mvb-character-submit type="submit">Add Character</button>
                        </div>
                    </form>
                </section>
            </div>

            <div class="mvb-modal-backdrop" data-mvb-location-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-location-dialog-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">Storyboard resource</p>
                            <h2 id="mvb-location-dialog-heading" data-mvb-location-dialog-title>Add Location</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-cancel-location type="button">Cancel</button>
                    </div>
                    <form data-mvb-location-form>
                        <label class="mvb-field">
                            <span>Name</span>
                            <input data-mvb-location-name type="text" maxlength="200" autocomplete="off" required>
                        </label>
                         <label class="mvb-field">
                             <span>Description</span>
                             <textarea data-mvb-location-description rows="5" maxlength="5000"></textarea>
                         </label>
                         <div class="mvb-resource-dialog-references" data-mvb-location-references></div>
                         <p class="mvb-dialog-error" data-mvb-location-error role="alert" hidden></p>
                        <div class="mvb-dialog-actions">
                            <button class="mvb-button mvb-button-secondary" data-mvb-cancel-location type="button">Cancel</button>
                            <button class="mvb-button mvb-button-primary" data-mvb-location-submit type="submit">Add Location</button>
                        </div>
                    </form>
                </section>
            </div>
            <div class="mvb-modal-backdrop" data-mvb-clear-storyboard-dialog hidden>
                <section class="mvb-dialog" role="dialog" aria-modal="true" aria-labelledby="mvb-clear-storyboard-heading">
                    <div class="mvb-dialog-heading">
                        <div>
                            <p class="mvb-eyebrow">Clear storyboard</p>
                            <h2 id="mvb-clear-storyboard-heading">Clear applied storyboard?</h2>
                        </div>
                        <button class="mvb-dialog-close" data-mvb-cancel-clear-storyboard type="button">Cancel</button>
                    </div>
                    <p class="mvb-dialog-note">This removes scene allocations but preserves source scenes, direction, Characters, Locations, and references.</p>
                    <div class="mvb-dialog-actions">
                        <button class="mvb-button mvb-button-secondary" data-mvb-cancel-clear-storyboard type="button">Cancel</button>
                        <button class="mvb-button mvb-button-danger" data-mvb-confirm-clear-storyboard type="button">Clear Storyboard</button>
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
        storyboardMessage: "",
        storyboardRequest: null,
        storyboardRequestStale: false,
        storyboardAppliedStale: false,
        storyboardResponseText: "",
        storyboardPreview: null,
         storyboardRelayMessage: "",
         storyboardRelayState: "ready",
         characterDialogFiles: [],
         locationDialogFiles: [],
         characterDialogReferenceIds: [],
         locationDialogReferenceIds: [],
         visualDrafts: {},
        visualExpandedScenes: {},
        visualBulkMethod: "keyframe_i2v",
         visualRemoveTarget: null,
         visualMessage: "",
         visualMessageState: "ready",
         promptCards: [],
         promptListState: "empty",
         promptListMessage: "",
         promptDrafts: {},
         promptExpandedScenes: {},
          promptMessage: "",
          promptMessageState: "ready",
          promptRelay: {},
          promptRelayExpanded: {},
          gptLaunchInFlight: false,
          renderPreflight: null,
          renderRequirementsCache: null,
          renderLoadingMode: "runtime",
          renderState: "empty",
           renderMessage: "",
           renderMessageState: "ready",
           renderPreparingSceneId: null,
           renderJobs: {},
           renderJobsState: "empty",
           renderJobsMessage: "",
           renderJobsPollDelay: 1000,
           renderJobsPollTimer: null,
           renderJobsLoading: false,
           renderSubmittingSceneId: null,
           renderJobActionId: null,
           renderFinalizingJobId: null,
           renderBatch: null,
           renderBatchLoading: false,
           renderBatchBusy: false,
           renderBatchSelection: null,
           renderBatchConfirm: null,
           activeView: "setup",
        deleteConfirm: null,
        newProjectBusy: false,
        openingProject: false,
        maximized: false,
        invalidIgnoreBusy: false,
        invalidIgnoreMessage: "",
        projectDeleteTarget: null,
        projectDeleteBusy: false,
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
    const cancelDeleteButtons = root.querySelectorAll("[data-mvb-cancel-delete]");
    const confirmDeleteButton = root.querySelector("[data-mvb-confirm-delete]");
    const viewButtons = root.querySelectorAll("[data-mvb-view]");
    const addCharacterButton = root.querySelector("[data-mvb-add-character]");
    const addLocationButton = root.querySelector("[data-mvb-add-location]");
    const characterForm = root.querySelector("[data-mvb-character-form]");
    const locationForm = root.querySelector("[data-mvb-location-form]");
    const cancelCharacterButtons = root.querySelectorAll("[data-mvb-cancel-character]");
    const cancelLocationButtons = root.querySelectorAll("[data-mvb-cancel-location]");
    const storyboardMode = root.querySelector("[data-mvb-storyboard-mode]");
    const storyBrief = root.querySelector("[data-mvb-story-brief]");
    const visualNotes = root.querySelector("[data-mvb-visual-notes]");
    const requestButton = root.querySelector("[data-mvb-generate-request]");
    const copyRequestButton = root.querySelector("[data-mvb-copy-request]");
    const responseInput = root.querySelector("[data-mvb-response-json]");
    const validateResponseButton = root.querySelector("[data-mvb-validate-response]");
    const clearPasteButton = root.querySelector("[data-mvb-clear-paste]");
    const applyStoryboardButton = root.querySelector("[data-mvb-apply-storyboard]");
    const clearStoryboardButton = root.querySelector("[data-mvb-clear-storyboard]");
    const cancelClearStoryboardButton = root.querySelector("[data-mvb-cancel-clear-storyboard]");
    const confirmClearStoryboardButton = root.querySelector("[data-mvb-confirm-clear-storyboard]");
    const visualBulkMethod = root.querySelector("[data-mvb-visual-bulk-method]");
    const visualBulkApply = root.querySelector("[data-mvb-visual-bulk-apply]");
    const renderRefreshButton = root.querySelector("[data-mvb-render-refresh]");
    const renderRescanButton = root.querySelector("[data-mvb-render-rescan]");
    const renderSceneList = root.querySelector("[data-mvb-render-scenes]");

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
    storyboardMode.addEventListener("change", () => {
        if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
            return;
        }
        const value = storyboardMode.value;
        if (builderState.currentProject.story_direction.storyboard_mode === value) {
            return;
        }
        builderState.currentProject.story_direction.storyboard_mode = value;
        builderState.editRevision += 1;
        builderState.saveState = "dirty";
        builderState.saveMessage = "";
        invalidateStoryboardRequestForDirectionEdit();
        renderProjectState(root);
        scheduleAutosave(root);
    });
    storyBrief.addEventListener("input", () => {
        if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
            return;
        }
        const value = storyBrief.value;
        if (builderState.currentProject.story_direction.story_brief === value) {
            return;
        }
        builderState.currentProject.story_direction.story_brief = value;
        builderState.editRevision += 1;
        builderState.saveState = "dirty";
        builderState.saveMessage = "";
        invalidateStoryboardRequestForDirectionEdit();
        renderProjectState(root);
        scheduleAutosave(root);
    });
    visualNotes.addEventListener("input", () => {
        if (!isActive(root) || !builderState.currentProject || builderState.operation || builderState.transitioning || builderState.closing) {
            return;
        }
        const value = visualNotes.value;
        if (builderState.currentProject.story_direction.visual_notes === value) {
            return;
        }
        builderState.currentProject.story_direction.visual_notes = value;
        builderState.editRevision += 1;
        builderState.saveState = "dirty";
        builderState.saveMessage = "";
        invalidateStoryboardRequestForDirectionEdit();
        renderProjectState(root);
        scheduleAutosave(root);
    });
    responseInput.addEventListener("input", () => {
        if (!isActive(root)) {
            return;
        }
        builderState.storyboardResponseText = responseInput.value;
        builderState.storyboardPreview = null;
        setStoryboardRelayMessage("");
        renderStoryboardRelay(root);
    });
    requestButton.addEventListener("click", () => void generateStoryboardRequest(root));
    copyRequestButton.addEventListener("click", () => void copyStoryboardRequest(root));
    root.querySelector("[data-mvb-storyboard-open-gpt]").addEventListener("click", () => void openDedicatedGpt(root, "storyboard"));
    validateResponseButton.addEventListener("click", () => void validateStoryboardResponseInput(root));
    clearPasteButton.addEventListener("click", () => {
        builderState.storyboardResponseText = "";
        builderState.storyboardPreview = null;
        setStoryboardRelayMessage("");
        renderStoryboardRelay(root);
    });
    applyStoryboardButton.addEventListener("click", () => void applyStoryboard(root));
    clearStoryboardButton.addEventListener("click", () => openStoryboardClearDialog(root));
    visualBulkMethod.addEventListener("change", () => {
        if (!isActive(root)) {
            return;
        }
        builderState.visualBulkMethod = visualBulkMethod.value;
    });
    visualBulkApply.addEventListener("click", () => void applyVisualMethodToAllScenes(root));
    renderRefreshButton.addEventListener("click", () => void loadRenderPreflight(root));
    renderRescanButton.addEventListener("click", () => void loadRenderPreflight(root, false, { forceRefresh: true }));
    const batchAllButton = root.querySelector("[data-mvb-batch-all]");
    const batchSelectButton = root.querySelector("[data-mvb-batch-select]");
    const renderSection = root.querySelector("[data-mvb-render]");
    const batchConfirmCancelButtons = root.querySelectorAll("[data-mvb-batch-confirm-cancel]");
    const batchConfirmStartButton = root.querySelector("[data-mvb-batch-confirm-start]");
    if (batchAllButton) {
        batchAllButton.addEventListener("click", () => void openBatchConfirmation(root, null));
    }
    if (batchSelectButton) {
        batchSelectButton.addEventListener("click", () => void enterBatchSelectionMode(root));
    }
    for (const cancelButton of batchConfirmCancelButtons) {
        cancelButton.addEventListener("click", () => closeBatchConfirmation(root));
    }
    if (batchConfirmStartButton) {
        batchConfirmStartButton.addEventListener("click", () => void startConfirmedBatch(root));
    }
    if (renderSection) {
        renderSection.addEventListener("click", (event) => {
            const selectAll = event.target.closest("[data-mvb-batch-select-all]");
            if (selectAll) {
                const selection = builderState.renderBatchSelection;
                if (selection) {
                    for (const [sceneId, action] of selection.actions) {
                        if (BATCH_ELIGIBLE_ACTIONS.includes(action)) {
                            selection.selected.add(sceneId);
                        }
                    }
                    renderRenderState(root);
                }
                return;
            }
            const clearButton = event.target.closest("[data-mvb-batch-clear]");
            if (clearButton) {
                builderState.renderBatchSelection?.selected.clear();
                renderRenderState(root);
                return;
            }
            const cancelSelection = event.target.closest("[data-mvb-batch-cancel-selection]");
            if (cancelSelection) {
                exitBatchSelectionMode(root);
                return;
            }
            const renderSelected = event.target.closest("[data-mvb-batch-render-selected]");
            if (renderSelected) {
                void openBatchConfirmation(root, builderState.renderBatchSelection);
                return;
            }
            const pauseButton = event.target.closest("[data-mvb-batch-pause]");
            if (pauseButton) {
                void batchControlAction(root, renderBatchPausePath);
                return;
            }
            const resumeButton = event.target.closest("[data-mvb-batch-resume]");
            if (resumeButton) {
                void batchControlAction(root, renderBatchResumePath);
                return;
            }
            const endButton = event.target.closest("[data-mvb-batch-end]");
            if (endButton) {
                void batchControlAction(root, renderBatchEndPath);
                return;
            }
            const retryFailedButton = event.target.closest("[data-mvb-batch-retry-failed]");
            if (retryFailedButton) {
                void batchControlAction(root, renderBatchRetryFailedPath);
            }
        });
        renderSection.addEventListener("change", (event) => {
            const upscaleSelect = event.target.closest("[data-mvb-render-upscale-select]");
            if (upscaleSelect) {
                void setUpscaleMethod(root, upscaleSelect.value);
                return;
            }
            const checkbox = event.target.closest("[data-mvb-render-select-scene]");
            if (!checkbox || !builderState.renderBatchSelection) {
                return;
            }
            const sceneId = checkbox.dataset.mvbRenderSelectScene;
            if (checkbox.checked) {
                builderState.renderBatchSelection.selected.add(sceneId);
            } else {
                builderState.renderBatchSelection.selected.delete(sceneId);
            }
            renderSelectionBar(root);
        });
    }
    renderSceneList.addEventListener("click", (event) => {
        const prepareButton = event.target.closest("[data-mvb-render-prepare]");
        if (prepareButton) {
            void prepareRenderScene(root, prepareButton.dataset.mvbRenderPrepare);
            return;
        }
        const submitButton = event.target.closest("[data-mvb-render-submit]");
        if (submitButton) {
            void submitRenderScene(root, submitButton.dataset.mvbRenderSubmit);
            return;
        }
        const cancelButton = event.target.closest("[data-mvb-render-cancel]");
        if (cancelButton) {
            void cancelRenderSceneJob(root, cancelButton.dataset.mvbRenderCancel);
            return;
        }
        const retryButton = event.target.closest("[data-mvb-render-retry]");
        if (retryButton) {
            void retryRenderSceneJob(root, retryButton.dataset.mvbRenderRetry);
            return;
        }
        const finalizeButton = event.target.closest("[data-mvb-render-finalize]");
        if (finalizeButton) {
            void finalizeRenderScene(root, finalizeButton.dataset.mvbRenderFinalize);
            return;
        }
        const postprocessStartButton = event.target.closest("[data-mvb-render-postprocess-start]");
        if (postprocessStartButton) {
            void startScenePostprocess(root, postprocessStartButton.dataset.mvbRenderPostprocessStart);
            return;
        }
        const postprocessCancelButton = event.target.closest("[data-mvb-render-postprocess-cancel]");
        if (postprocessCancelButton) {
            void cancelScenePostprocess(root, postprocessCancelButton.dataset.mvbRenderPostprocessCancel);
            return;
        }
        const postprocessRetryButton = event.target.closest("[data-mvb-render-postprocess-retry]");
        if (postprocessRetryButton) {
            void retryScenePostprocess(root, postprocessRetryButton.dataset.mvbRenderPostprocessRetry);
        }
    });
    cancelClearStoryboardButton.addEventListener("click", () => closeStoryboardClearDialog(root));
    confirmClearStoryboardButton.addEventListener("click", () => void clearAppliedStoryboard(root));
    newForm.addEventListener("submit", (event) => {
        event.preventDefault();
        void createProject(root);
    });
    for (const button of cancelNewButtons) {
        button.addEventListener("click", () => closeProjectDialogs(root));
    }
    closeOpenButton.addEventListener("click", () => closeProjectDialogs(root));
    root.querySelector("[data-mvb-dialog-new]").addEventListener("click", () => void openNewProjectDialog(root));
    for (const button of cancelDeleteButtons) {
        button.addEventListener("click", () => closeProjectDeleteDialog(root));
    }
    confirmDeleteButton.addEventListener("click", () => void deleteProject(root));
    for (const button of viewButtons) {
        button.addEventListener("click", () => switchView(root, button.dataset.mvbView));
    }
    addCharacterButton.addEventListener("click", () => openCharacterDialog(root));
    addLocationButton.addEventListener("click", () => openLocationDialog(root));
    characterForm.addEventListener("submit", (event) => {
        event.preventDefault();
        void saveCharacter(root);
    });
    locationForm.addEventListener("submit", (event) => {
        event.preventDefault();
        void saveLocation(root);
    });
    for (const button of cancelCharacterButtons) {
        button.addEventListener("click", () => closeResourceDialogs(root));
    }
    for (const button of cancelLocationButtons) {
        button.addEventListener("click", () => closeResourceDialogs(root));
    }

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
