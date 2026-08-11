import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";

const NODE_CLASS = "MusicVideoBuilder";
const STYLESHEET_ID = "music-video-builder-styles";

let overlay = null;
let healthController = null;

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

function updateStatus(root, text, state) {
    if (root !== overlay || !root.isConnected) {
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
    } catch (error) {
        if (controller.signal.aborted) {
            return;
        }

        console.error("[Music Video Builder] Backend health check failed.", error);
        updateStatus(root, visibleFailure, "error");
    } finally {
        if (healthController === controller) {
            healthController = null;
        }
    }
}

function closeBuilder(root) {
    if (root === overlay) {
        healthController?.abort();
        healthController = null;
        overlay = null;
    }

    root.remove();
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
                <div>
                    <p class="mvb-phase">Extension shell</p>
                    <h1 id="mvb-title">Vesper Music Video Builder</h1>
                </div>
                <button class="mvb-close" type="button">Close</button>
            </header>
            <section class="mvb-health" aria-label="Backend status">
                <span class="mvb-health-label">Backend</span>
                <span class="mvb-health-value" data-mvb-status data-state="checking" aria-live="polite">Checking…</span>
            </section>
            <ol class="mvb-stage-list" aria-label="Builder stages">
                <li>Setup</li>
                <li>Storyboard</li>
                <li>Keyframes</li>
                <li>Prompts</li>
                <li>Render</li>
            </ol>
        </section>
    `;

    const closeButton = root.querySelector(".mvb-close");
    closeButton.addEventListener("click", () => closeBuilder(root), { once: true });

    overlay = root;
    document.body.append(root);
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

        const alreadyAttached = node.widgets?.some((widget) => widget.name === "Open Builder");
        if (!alreadyAttached) {
            node.addWidget("button", "Open Builder", null, openBuilder);
        }
    },
});
