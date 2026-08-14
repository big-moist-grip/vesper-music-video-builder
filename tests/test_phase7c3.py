import base64
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT.joinpath("web", "extension.js").read_text(encoding="utf-8")
STYLES = ROOT.joinpath("web", "builder.css").read_text(encoding="utf-8")
PROMPT_STATE = ROOT.joinpath("web", "prompt_state.js").read_bytes()
GPT_LAUNCHER = ROOT.joinpath("web", "gpt_launcher.js").read_text(encoding="utf-8")


class Phase7C3FrontendContractTestCase(unittest.TestCase):
    def _run_prompt_state_script(self, script):
        module_url = "data:text/javascript;base64," + base64.b64encode(PROMPT_STATE).decode("ascii")
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "--eval",
                f"const state = await import('{module_url}');\n{script}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @staticmethod
    def _function_body(name, next_name):
        start = EXTENSION.index(name)
        end = EXTENSION.index(next_name, start)
        return EXTENSION[start:end]

    def test_gpt_apply_reassembles_active_draft_without_persisting(self):
        handler = self._function_body(
            "async function applyPromptRelayResponse(root, sceneId, generationMethod)",
            "async function savePrompt(root, sceneId, generationMethod)",
        )
        for marker in (
            'promptPath(projectId, sceneId, "relay-response")',
            'builderState.currentProject?.project_id !== projectId',
            'currentItem.source_fingerprint !== item.source_fingerprint',
            "const draft = promptDraftFor(sceneId, generationMethod, currentItem);",
            "draft.text = result.enhanced_prompt;",
            "draft.dirty = promptDraftIsDirty(currentItem, draft);",
            'builderState.promptMessage = draft.dirty',
            'draft.relayFingerprint = result.request_fingerprint;',
            'renderProjectState(root, { promptViewport });',
        ):
            self.assertIn(marker, handler)
        self.assertNotIn("setCurrentProject(root, project", handler)
        self.assertNotIn('method: "PUT"', handler)

    def test_gpt_apply_has_truthful_changed_and_identical_feedback(self):
        handler = self._function_body(
            "async function applyPromptRelayResponse(root, sceneId, generationMethod)",
            "async function savePrompt(root, sceneId, generationMethod)",
        )
        self.assertIn('"PROMPT DIRECTOR RESPONSE APPLIED — UNSAVED. Review and save the Final Prompt."', handler)
        self.assertIn('"This Prompt Director response is already saved and current."', handler)
        self.assertIn('"Prompt Director response applied — UNSAVED. Review and save the Final Prompt."', handler)
        self.assertIn('relay.responseMessageState = "success";', handler)

    def test_prompt_status_and_saveability_cover_current_unsaved_stale_missing(self):
        self._run_prompt_state_script(
            """
const fingerprint = "a".repeat(64);
const base = {deterministic_prompt: "deterministic", saved_final_prompt: "saved", source_fingerprint: fingerprint, saved_source_fingerprint: fingerprint, saved_relay_fingerprint: fingerprint};
for (const status of ["current", "stale", "error"]) {
    const item = {...base, status};
    if (state.promptDraftStatus(item, {dirty: false}) !== status) throw new Error(status + " status failed");
}
if (state.promptDraftStatus({...base, status: "current"}, {dirty: true}) !== "unsaved") throw new Error("dirty status failed");
if (state.promptDraftStatus(null, null) !== "needs_gpt") throw new Error("needs GPT status failed");
if (!state.canSavePromptDraft({...base, status: "current"}, {text: "changed", sourceFingerprint: fingerprint, relayFingerprint: fingerprint})) throw new Error("changed current relay draft should save");
if (state.canSavePromptDraft({...base, status: "error"}, {text: "changed", sourceFingerprint: fingerprint, relayFingerprint: fingerprint})) throw new Error("error draft should not save");
"""
        )
        prompt_state = self._function_body("function renderPromptState(root, options = {})", "async function loadPromptCards")
        self.assertIn("promptDraftStatus(item, draft)", prompt_state)
        self.assertIn("saveButton.disabled = blocked || !canSavePromptDraft(item, draft);", prompt_state)

    def test_manual_editor_updates_status_badge_and_preserves_local_draft(self):
        render = self._function_body("function renderPromptState(root, options = {})", "async function loadPromptCards")
        for marker in (
            "draft.text = editor.value;",
            "draft.dirty = promptDraftIsDirty(item, draft);",
            "badge.dataset.state = currentDraftStatus;",
            "badge.textContent = promptStatusLabel(currentDraftStatus);",
            "editor.value = draft.text;",
        ):
            self.assertIn(marker, render)
        self.assertIn("revealPromptReviewSection(root, sceneId, generationMethod)", EXTENSION)
        self.assertIn("integrated_multimodal_description:", EXTENSION)
        self.assertIn("detailed_description:", EXTENSION)

    def test_storyboard_and_prompt_keep_generate_copy_open_as_distinct_actions(self):
        self.assertIn('storyboard: "https://chatgpt.com/g/g-6a7c2ee4c49c81919ff52e70d0e6f58a-storyboard-director"', GPT_LAUNCHER)
        self.assertIn('prompts: "https://chatgpt.com/g/g-6a7dd81828588191955a0ef1ac196da7-prompt-director"', GPT_LAUNCHER)
        storyboard = self._function_body("function renderStoryboardRelay(root)", "function renderStoryboardState(root)")
        prompt = self._function_body("function renderPromptState(root, options = {})", "async function loadPromptCards")
        for marker in ("Generate / Refresh Request", "Copy Request JSON", "Open GPT"):
            self.assertIn(marker, EXTENSION)
        for marker in ("Generate GPT Request", "Copy Request JSON", "Open GPT"):
            self.assertIn(marker, prompt)
        self.assertIn("[data-mvb-generate-request]", storyboard)
        self.assertIn("[data-mvb-copy-request]", storyboard)
        self.assertIn("data-mvb-storyboard-open-gpt", EXTENSION)
        self.assertIn("openGptButton.dataset.mvbPromptRelayOpenGpt = \"\";", EXTENSION)
        self.assertIn("async function openDedicatedGpt(root, director)", EXTENSION)
        self.assertIn("const result = await fetchJson(path", EXTENSION)
        self.assertIn("isSuccessfulGptLaunch(result, director)", EXTENSION)
        self.assertNotIn("window.open", EXTENSION)
        self.assertNotIn('document.createElement("a")', EXTENSION.split("async function closeBuilder", 1)[0].split("function openDedicatedGpt", 1)[1])
        self.assertIn("Could not open the GPT. Open it manually or retry.", EXTENSION)
        self.assertNotIn("allow pop-ups", EXTENSION)

    def test_request_copy_feedback_and_secondary_json_disclosures_are_visible(self):
        storyboard_copy = self._function_body("async function copyStoryboardRequest(root)", "async function validateStoryboardResponseInput(root)")
        prompt_copy = self._function_body("async function copyPromptRelayRequest(root, sceneId, generationMethod)", "async function applyPromptRelayResponse")
        self.assertIn('setStoryboardRelayMessage("REQUEST COPIED")', storyboard_copy)
        self.assertIn('relay.responseMessage = "REQUEST COPIED";', prompt_copy)
        self.assertIn('class="mvb-relay-disclosure"', EXTENSION)
        self.assertIn('textContent = "View Request JSON"', EXTENSION)
        self.assertIn('relayResponseLabel.textContent = "Paste GPT Response";', EXTENSION)
        self.assertIn('relayResponseLabel.textContent = "Paste GPT Response";', EXTENSION)

    def test_resource_cards_are_compact_and_dialogs_own_detail_and_references(self):
        character_card = self._function_body("function appendCharacterCard(root, character)", "function appendLocationCard(root, location)")
        location_card = self._function_body("function appendLocationCard(root, location)", "function findCharacter(project, characterId)")
        for card in (character_card, location_card):
            self.assertIn("appendEntityPreview(root, card", card)
            self.assertIn('textContent = "Edit"', card)
            self.assertNotIn("appendDefinitionField", card)
            self.assertNotIn("appendReferenceList", card)
            self.assertNotIn("appendReferenceUpload", card)
        for marker in (
            "renderResourceDialogReferences(root, \"characters\", character);",
            "renderResourceDialogReferences(root, \"locations\", location);",
            'data-mvb-character-references',
            'data-mvb-location-references',
            'data-mvb-character-submit',
            'data-mvb-location-submit',
        ):
            self.assertIn(marker, EXTENSION)

    def test_add_dialog_supports_reference_selection_before_entity_creation(self):
        helper = self._function_body("function renderResourceDialogReferences(root, kind, entity = null)", "function refreshOpenResourceDialog")
        upload = self._function_body("function appendReferenceUpload(root, parent, kind, entity)", "function appendDeleteControls")
        for marker in (
            'pendingResourceFiles(kind)',
        ):
            self.assertIn(marker, helper)
        for marker in (
            'input.multiple = true;',
            'button.textContent = entity ? "Add Reference" : "Choose Reference Images";',
            "setPendingResourceFiles(kind, [...pendingResourceFiles(kind), ...selected]);",
        ):
            self.assertIn(marker, upload)
        self.assertIn("const pendingFiles = [...builderState.characterDialogFiles];", EXTENSION)
        self.assertIn("const pendingFiles = [...builderState.locationDialogFiles];", EXTENSION)

    def test_new_resource_reference_uploads_have_authoritative_validation_and_rollback(self):
        character_save = self._function_body("async function saveCharacter(root)", "async function saveLocation(root)")
        location_save = self._function_body("async function saveLocation(root)", "function uploadReferenceFile(projectId, kind, entityId, file)")
        for source, entity_id in ((character_save, "created.character_id"), (location_save, "created.location_id")):
            self.assertIn("uploadReferenceFile(projectId", source)
            self.assertIn("hasProjectDocument(project)", source)
            self.assertIn("method: \"DELETE\"", source)
            self.assertIn(entity_id, source)
        mutation = self._function_body("async function runResourceMutation(root, operation, task, failureMessage)", "async function saveCharacter(root)")
        self.assertIn("flushCurrentProject(root)", mutation)
        self.assertIn('"Save the current project before continuing."', mutation)

    def test_modal_errors_are_visible_and_values_remain_on_failure(self):
        for function_name, error_marker, field_markers in (
            ("async function saveCharacter(root)", "data-mvb-character-error", ("character-name", "character-appearance", "character-outfit")),
            ("async function saveLocation(root)", "data-mvb-location-error", ("location-name", "location-description")),
        ):
            start = EXTENSION.index(function_name)
            end = EXTENSION.index("async function ", start + len(function_name))
            source = EXTENSION[start:end]
            self.assertIn(error_marker, EXTENSION)
            self.assertIn("errorElement.textContent = builderState.storyboardMessage", source)
            self.assertIn("errorElement.hidden = false", source)
            for field in field_markers:
                self.assertIn(f'data-mvb-{field}', EXTENSION)
        self.assertIn("if (!saved && isActive(root))", EXTENSION)
        self.assertIn("if (saved && isActive(root))", EXTENSION)

    def test_visuals_and_prompt_mutations_preserve_session_viewport_without_browser_storage(self):
        for marker in (
            "captureVisualViewport(root)",
            "restoreVisualViewport(root, viewport)",
            "capturePromptViewport(root, sceneId)",
            "restorePromptViewport(root, promptViewport)",
        ):
            self.assertIn(marker, EXTENSION)
        self.assertNotIn("localStorage", EXTENSION)
        self.assertNotIn("sessionStorage", EXTENSION)

    def test_phase7c3_keeps_product_architecture_and_protected_phase6_contract(self):
        self.assertNotIn('data-mvb-view="render"', EXTENSION)
        self.assertNotIn('data-mvb-view="prompts"', EXTENSION.split('data-mvb-view="prompts"', 1)[0])
        self.assertNotIn("H3 execution", EXTENSION)
        self.assertNotIn("fetch(\"https://api.openai.com", EXTENSION)
        self.assertNotIn("Ollama", EXTENSION)
        self.assertNotIn("ollama", EXTENSION)
        for protected in (
            ROOT.joinpath("backend", "workflows.py"),
            ROOT.joinpath("workflows", "h3_music_video_i2v_api.json"),
            ROOT.joinpath("workflows", "h3_music_video_ref2va_api.json"),
        ):
            self.assertTrue(protected.exists(), protected)

    def test_resource_card_layout_is_intrinsic_and_responsive(self):
        self.assertIn(".mvb-entity-card {", STYLES)
        card_start = STYLES.index(".mvb-entity-card {")
        card_end = STYLES.index("}", card_start) + 1
        card_css = STYLES[card_start:card_end]
        self.assertIn("grid-template-columns", card_css)
        self.assertNotIn("height:", card_css)
        self.assertNotIn("max-height:", card_css)
        self.assertIn(".mvb-entity-card-preview", STYLES)


if __name__ == "__main__":
    unittest.main()
