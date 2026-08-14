export function promptDraftIsDirty(item, draft) {
    if (!item || !draft) {
        return false;
    }
    const savedText = item.saved_final_prompt || "";
    const hasCurrentRelay = promptDraftHasCurrentRelay(item, draft);
    return draft.text !== savedText
        || (hasCurrentRelay
            && (draft.sourceFingerprint !== item.saved_source_fingerprint
                || draft.relayFingerprint !== item.saved_relay_fingerprint));
}

export function promptDraftHasCurrentRelay(item, draft) {
    return Boolean(item
        && draft
        && typeof item.source_fingerprint === "string"
        && item.source_fingerprint
        && draft.sourceFingerprint === item.source_fingerprint
        && draft.relayFingerprint === item.source_fingerprint);
}

export function promptDraftIsStale(item, draft) {
    if (!item || !draft || !draft.dirty) {
        return false;
    }
    if (typeof draft.sourceFingerprint !== "string" || !draft.sourceFingerprint) {
        return false;
    }
    return draft.sourceFingerprint !== item.source_fingerprint;
}

export function promptDraftStatus(item, draft) {
    if (!item) {
        return "needs_gpt";
    }
    if (item.status === "error") {
        return "error";
    }
    if (draft?.dirty) {
        return promptDraftIsStale(item, draft) ? "stale" : "unsaved";
    }
    if (item.status === "current" || item.status === "stale") {
        return item.status;
    }
    return "needs_gpt";
}

export function canSavePromptDraft(item, draft) {
    if (!item || !draft || item.status === "error") {
        return false;
    }
    if (!draft.text.trim() || !promptDraftHasCurrentRelay(item, draft)) {
        return false;
    }
    return promptDraftIsDirty(item, draft);
}

export function promptDraftReadyForRender(item, draft) {
    return Boolean(item?.ready_for_render_prompt)
        && promptDraftHasCurrentRelay(item, draft)
        && !promptDraftIsDirty(item, draft);
}

export function storyboardRequestJson(request) {
    return request ? JSON.stringify(request, null, 2) : "";
}

export function canCopyStoryboardRequest(request, stale, blocked = false) {
    return Boolean(storyboardRequestJson(request)) && !stale && !blocked;
}

export function promptRelayRequestJson(request) {
    return request ? JSON.stringify(request, null, 2) : "";
}

export function promptRelayRequestIsCurrent(relay, item) {
    return Boolean(relay?.request
        && item
        && relay.request.scene_id === item.scene_id
        && relay.request.generation_method === item.generation_method
        && relay.request.request_fingerprint === item.source_fingerprint);
}

export function canCopyPromptRelayRequest(relay, item, blocked = false) {
    return Boolean(promptRelayRequestJson(relay?.request))
        && promptRelayRequestIsCurrent(relay, item)
        && !blocked;
}

export function canApplyPromptRelayResponse(relay, item, blocked = false) {
    return canCopyPromptRelayRequest(relay, item, blocked)
        && typeof relay.responseText === "string"
        && Boolean(relay.responseText.trim());
}
