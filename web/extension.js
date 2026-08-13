import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";

const NODE_CLASS = "MusicVideoBuilder";
const STYLESHEET_ID = "music-video-builder-styles";
const PROJECTS_PATH = "/music-video-builder/projects";
const AUTOSAVE_DELAY_MS = 700;
const MAX_REF2VA_STILL_REFERENCES = 9;
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
    return payload !== null
        && typeof payload === "object"
        && payload.schema_version === 5
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
            && (visualScene.generation_method === "keyframe_i2v" || visualScene.generation_method === "reference2video"));
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

function setCurrentProject(root, project, options = {}) {
    if (!isActive(root)) {
        return;
    }

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
    builderState.storyboardRequestStale = Boolean(project?.storyboard?.scenes?.length);
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
    if (!entity.references.length) {
        const empty = document.createElement("p");
        empty.className = "mvb-reference-empty";
        empty.textContent = "No reference images.";
        references.append(empty);
    }

    for (const reference of entity.references) {
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
        removeButton.addEventListener("click", () => void removeReference(root, kind, entity, reference));

        item.append(image, caption, removeButton);
        references.append(item);
    }
    parent.append(references);
}

function appendReferenceUpload(root, parent, kind, entity) {
    const entityId = entity[kind === "characters" ? "character_id" : "location_id"];
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";
    input.hidden = true;
    input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (!file) {
            return;
        }
        void uploadReference(root, kind, entityId, file).finally(() => {
            input.value = "";
        });
    });

    const button = document.createElement("button");
    button.className = "mvb-button mvb-button-secondary mvb-button-small";
    button.type = "button";
    button.textContent = "Add Reference";
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

function appendCharacterCard(root, character) {
    const card = document.createElement("article");
    card.className = "mvb-entity-card";

    const heading = document.createElement("div");
    heading.className = "mvb-entity-card-heading";
    const title = document.createElement("h3");
    title.textContent = character.name;
    const role = document.createElement("span");
    role.className = "mvb-entity-role";
    role.textContent = roleLabel(character.role);
    heading.append(title, role);

    const fields = document.createElement("dl");
    fields.className = "mvb-entity-fields";
    appendDefinitionField(fields, "Appearance", character.appearance);
    appendDefinitionField(fields, "Outfit", character.outfit);

    const referencesHeading = document.createElement("p");
    referencesHeading.className = "mvb-entity-section-label";
    referencesHeading.textContent = `References · ${character.references.length}`;
    appendReferenceList(root, card, "characters", character);

    const actions = document.createElement("div");
    actions.className = "mvb-entity-actions";
    const editButton = document.createElement("button");
    editButton.className = "mvb-button mvb-button-secondary mvb-button-small";
    editButton.type = "button";
    editButton.textContent = "Edit";
    editButton.disabled = Boolean(builderState.operation);
    editButton.addEventListener("click", () => openCharacterDialog(root, character));
    actions.append(editButton);
    appendReferenceUpload(root, actions, "characters", character);
    appendDeleteControls(root, actions, "characters", character);

    const references = card.querySelector(".mvb-reference-list");
    card.insertBefore(heading, card.firstChild);
    card.insertBefore(fields, references);
    card.insertBefore(referencesHeading, references);
    card.append(actions);
    return card;
}

function appendLocationCard(root, location) {
    const card = document.createElement("article");
    card.className = "mvb-entity-card";

    const heading = document.createElement("div");
    heading.className = "mvb-entity-card-heading";
    const title = document.createElement("h3");
    title.textContent = location.name;
    heading.append(title);

    const fields = document.createElement("dl");
    fields.className = "mvb-entity-fields";
    appendDefinitionField(fields, "Description", location.description);

    const referencesHeading = document.createElement("p");
    referencesHeading.className = "mvb-entity-section-label";
    referencesHeading.textContent = `References · ${location.references.length}`;
    appendReferenceList(root, card, "locations", location);

    const actions = document.createElement("div");
    actions.className = "mvb-entity-actions";
    const editButton = document.createElement("button");
    editButton.className = "mvb-button mvb-button-secondary mvb-button-small";
    editButton.type = "button";
    editButton.textContent = "Edit";
    editButton.disabled = Boolean(builderState.operation);
    editButton.addEventListener("click", () => openLocationDialog(root, location));
    actions.append(editButton);
    appendReferenceUpload(root, actions, "locations", location);
    appendDeleteControls(root, actions, "locations", location);

    const references = card.querySelector(".mvb-reference-list");
    card.insertBefore(heading, card.firstChild);
    card.insertBefore(fields, references);
    card.insertBefore(referencesHeading, references);
    card.append(actions);
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
    return builderState.storyboardRequestStale ? "Out of date" : "Applied";
}

function setStoryboardRelayMessage(message, state = "success") {
    builderState.storyboardRelayMessage = message;
    builderState.storyboardRelayState = message ? state : "ready";
}

function invalidateStoryboardRequestForDirectionEdit() {
    builderState.storyboardRequest = null;
    builderState.storyboardPreview = null;
    builderState.storyboardRequestStale = Boolean(builderState.currentProject?.storyboard?.scenes?.length);
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
        : storyboardScenes.length && builderState.storyboardRequestStale
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
    const validateButton = root.querySelector("[data-mvb-validate-response]");
    const clearPasteButton = root.querySelector("[data-mvb-clear-paste]");
    const applyButton = root.querySelector("[data-mvb-apply-storyboard]");
    const clearButton = root.querySelector("[data-mvb-clear-storyboard]");
    const status = root.querySelector("[data-mvb-storyboard-relay-status]");
    if (!brief || !visualNotes || !storyboardMode || !storyboardModeHelp || !requestPreview || !responseInput || !requestButton || !copyButton || !validateButton || !clearPasteButton || !applyButton || !clearButton || !status) {
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
    requestPreview.value = builderState.storyboardRequest
        ? JSON.stringify(builderState.storyboardRequest, null, 2)
        : "Generate a request to preview its JSON.";

    const blocked = !project || builderState.closing || builderState.transitioning || Boolean(builderState.operation);
    storyboardMode.disabled = blocked;
    brief.disabled = blocked;
    visualNotes.disabled = blocked;
    requestButton.disabled = blocked || !project.scenes.length;
    copyButton.disabled = !builderState.storyboardRequest || builderState.storyboardRequestStale || blocked;
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
            : project?.storyboard?.scenes?.length && builderState.storyboardRequestStale
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
    } else if (builderState.storyboardRequestStale) {
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
        if (selected.length > MAX_REF2VA_STILL_REFERENCES) {
            missing.push(`Reference-to-Video supports at most ${MAX_REF2VA_STILL_REFERENCES} still references`);
        }
        if (!selected.length) {
            missing.push("Select at least one still reference");
        }
        if (selected.some((selector) => !assigned.has(visualSelectorKey(selector)))) {
            missing.push("A selected reference is not assigned to this scene");
        }
        for (const required of storyboardScene?.required_references || []) {
            if (!selectedKeys.has(visualSelectorKey(required))) {
                missing.push("A required storyboard reference is not selected");
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
        option.textContent = `${info.entity.name} · ${info.reference.original_name}`;
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
    if (selected.length >= MAX_REF2VA_STILL_REFERENCES) {
        const limit = document.createElement("p");
        limit.className = "mvb-visual-missing";
        limit.textContent = `Reference-to-Video supports up to ${MAX_REF2VA_STILL_REFERENCES} still references.`;
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
        if (!info) {
            row.textContent = "Reference is no longer available.";
            selectedList.append(row);
            continue;
        }
        const image = document.createElement("img");
        image.src = referenceUrl(project.project_id, selector.entity_type === "character" ? "characters" : "locations", selector.entity_id, selector.reference_id);
        image.alt = `${info.entity.name} reference`;
        image.loading = "lazy";
        const text = document.createElement("span");
        text.textContent = `${index + 1}. ${info.entity.name} · ${info.reference.original_name}`;
        const rowActions = document.createElement("span");
        rowActions.className = "mvb-visual-row-actions";
        rowActions.append(
            makeVisualButton(
                "Up",
                "mvb-button-secondary",
                () => {
                    if (index === 0) return;
                    const reordered = [...selected];
                    [reordered[index - 1], reordered[index]] = [reordered[index], reordered[index - 1]];
                    void saveReferenceSelection(root, scene.scene_id, reordered);
                },
                disabled || index === 0,
            ),
            makeVisualButton(
                "Down",
                "mvb-button-secondary",
                () => {
                    if (index === selected.length - 1) return;
                    const reordered = [...selected];
                    [reordered[index], reordered[index + 1]] = [reordered[index + 1], reordered[index]];
                    void saveReferenceSelection(root, scene.scene_id, reordered);
                },
                disabled || index === selected.length - 1,
            ),
            makeVisualButton(
                "Remove",
                "mvb-button-danger",
                () => void saveReferenceSelection(root, scene.scene_id, selected.filter((_, itemIndex) => itemIndex !== index)),
                disabled,
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
        storyboardScene?.required_references.map((selector) => {
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

function closeResourceDialogs(root) {
    root.querySelector("[data-mvb-character-dialog]").hidden = true;
    root.querySelector("[data-mvb-location-dialog]").hidden = true;
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
        builderState.deleteConfirm = null;
        void loadProjectList(root);
        if (project.storyboard?.scenes?.length) {
            void loadStoryboardRequest(root, true);
        }
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
    const saved = await runResourceMutation(
        root,
        "character",
        () => fetchJson(path, { method: characterId ? "PUT" : "POST", body: JSON.stringify(payload) }),
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
    const saved = await runResourceMutation(
        root,
        "location",
        () => fetchJson(path, { method: locationId ? "PUT" : "POST", body: JSON.stringify(payload) }),
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

async function uploadReference(root, kind, entityId, file) {
    const projectId = builderState.currentProject.project_id;
    await runResourceMutation(
        root,
        "reference",
        () => fetchJson(
            `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}/references`,
            { method: "POST", body: uploadFormData(file) },
        ),
        "Reference image is invalid or could not be saved.",
    );
}

async function removeReference(root, kind, entity, reference) {
    const entityId = entity[kind === "characters" ? "character_id" : "location_id"];
    const projectId = builderState.currentProject.project_id;
    await runResourceMutation(
        root,
        "reference",
        () => fetchJson(
            `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(entityId)}/references/${encodeURIComponent(reference.reference_id)}`,
            { method: "DELETE" },
        ),
        "Reference could not be removed.",
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
    if (view !== "setup" && view !== "storyboard" && view !== "visuals") {
        return;
    }
    builderState.activeView = view;
    builderState.deleteConfirm = null;
    renderProjectState(root);
}

function renderProjectState(root, options = {}) {
    if (!isActive(root)) {
        return;
    }

    const state = builderState;
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
    } else {
        renderSceneReview(root);
        renderStoryboardState(root);
        renderVisualsState(root, options.visualViewport || null);
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
        if (!await flushCurrentProject(root)) {
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
        builderState.storyboardRequestStale = Boolean(builderState.currentProject.storyboard?.scenes?.length)
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
        builderState.storyboardRequestStale = Boolean(builderState.currentProject.storyboard?.scenes?.length)
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
    if (!isActive(root) || !builderState.storyboardRequest) {
        return;
    }
    const requestJson = JSON.stringify(builderState.storyboardRequest, null, 2);
    try {
        if (!navigator.clipboard?.writeText) {
            throw new Error("Clipboard access is unavailable; select the visible request JSON to copy it.");
        }
        await navigator.clipboard.writeText(requestJson);
        setStoryboardRelayMessage("Request JSON copied.");
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
        setStoryboardRelayMessage("Storyboard applied.");
        void loadProjectList(root);
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
                            </div>
                        </div>
                        <label class="mvb-field mvb-relay-json-field">
                            <span>Request JSON</span>
                            <textarea data-mvb-request-json rows="8" readonly spellcheck="false">Generate a request to preview its JSON.</textarea>
                        </label>
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
        storyboardResponseText: "",
        storyboardPreview: null,
        storyboardRelayMessage: "",
        storyboardRelayState: "ready",
        visualDrafts: {},
        visualExpandedScenes: {},
        visualBulkMethod: "keyframe_i2v",
        visualRemoveTarget: null,
        visualMessage: "",
        visualMessageState: "ready",
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
