import base64
import copy
import io
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import (
    ProjectStorage,
    ProjectValidationError,
    default_prompts_for_scenes,
    validate_project_document,
)
from backend.prompt_service import (
    PromptConflictError,
    build_prompt_list,
    build_prompt_relay_request,
    build_prompt_source_fingerprint,
    compile_scene_prompt,
    prompt_scene_state,
    save_scene_prompt,
    validate_final_prompt,
    validate_prompt_relay_response,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes


class Phase7BTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def _run_prompt_state_script(self, script):
        source = Path(__file__).parents[1].joinpath("web", "prompt_state.js").read_bytes()
        module_url = "data:text/javascript;base64," + base64.b64encode(source).decode("ascii")
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", f"const state = await import('{module_url}');\n{script}"],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @staticmethod
    def _reference(original_name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": original_name,
        }

    def _project(self, method="reference2video", scene_type="performance", with_resources=True, instrumental=False):
        project = self.storage.create_project("Phase 7B Prompt Project")
        scenes = build_scenes(
            [] if instrumental else [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "The lyric remains verbatim."}],
            4_000,
        )
        character_id = str(uuid.uuid4())
        location_id = str(uuid.uuid4())
        character_reference = self._reference("vesper-wide.png")
        location_reference = self._reference("chapel-wide.png")
        characters = []
        locations = []
        if with_resources:
            characters = [{
                "character_id": character_id,
                "name": "Vesper",
                "role": "performer",
                "appearance": "Copper hair.",
                "outfit": "Black jacket.",
                "references": [character_reference],
            }]
            locations = [{
                "location_id": location_id,
                "name": "Chapel",
                "description": "Stone interior with warm practical light.",
                "references": [location_reference],
            }]
        visuals = default_visuals_for_scenes(scenes)
        visuals["scenes"][0]["generation_method"] = method
        if method == "reference2video" and with_resources:
            visuals["scenes"][0]["reference2video"]["selected_references"] = [
                {
                    "entity_type": "character",
                    "entity_id": character_id,
                    "reference_id": character_reference["reference_id"],
                },
                {
                    "entity_type": "location",
                    "entity_id": location_id,
                    "reference_id": location_reference["reference_id"],
                },
            ]
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "source": {
                    "master_audio": {
                        "stored_name": "master_audio.wav",
                        "original_name": "master.wav",
                        "duration_ms": 4_000,
                    },
                    "lyrics_srt": {
                        "stored_name": "lyrics.srt",
                        "original_name": "lyrics.srt",
                        "cue_count": 0 if instrumental else 1,
                    },
                },
                "scenes": scenes,
                "characters": characters,
                "locations": locations,
                "visuals": visuals,
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )
        storyboard_response = {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [{
                "scene_id": scenes[0]["scene_id"],
                "scene_type": scene_type,
                "character_ids": [character_id] if with_resources else [],
                "location_id": location_id if with_resources else None,
                "action": "Vesper moves through the frame.",
                "visual_instructions": "Warm light and restrained contrast.",
                "camera_direction": "Slow lateral tracking.",
                "motion_direction": "Measured forward movement.",
                "continuity_notes": "Keep the performer and environment consistent.",
                "required_references": [],
            }],
        }
        storyboard = validate_storyboard_response(project, storyboard_response)
        return self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )

    def _with_accepted_keyframe(self, project):
        asset_id = str(uuid.uuid4())
        stored_name = f"{asset_id}.png"
        keyframe_path = self.storage.project_directory(project["project_id"]) / "keyframes" / stored_name
        keyframe_path.write_bytes(b"not-a-real-image-for-prompt-state-test")
        visuals = copy.deepcopy(project["visuals"])
        keyframe = visuals["scenes"][0]["keyframe_i2v"]
        keyframe["keyframe_generation_prompt"] = "A restrained cinematic still."
        keyframe["intended_keyframe_description"] = "Intended opening: a bright red scarf."
        keyframe["accepted_keyframe"] = {
            "asset_id": asset_id,
            "stored_name": stored_name,
            "original_name": "accepted.png",
        }
        keyframe["actual_keyframe_description"] = "Actual opening: Vesper in a black jacket beside the chapel wall."
        return self.storage.save_project(
            project["project_id"],
            {**project, "visuals": visuals},
            allow_visuals_change=True,
        )

    def _validated_relay(self, project, method="reference2video", description="A restrained camera drift follows the performer."):
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id, method)
        validated = validate_prompt_relay_response(compiled, {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": compiled["source_fingerprint"],
            "enhanced_description": description,
        })
        return compiled, validated

    def test_new_project_uses_exact_schema_v7_prompt_state(self):
        project = self.storage.create_project("Prompt schema")
        self.assertEqual(project["schema_version"], 7)
        self.assertEqual(project["prompts"], {"scenes": []})
        self.assertEqual(validate_project_document(project), project)

    def test_prompt_records_are_exact_and_scene_aligned(self):
        project = self._project()
        valid = project["prompts"]
        invalid_documents = (
            {**project, "prompts": {"scenes": [], "extra": True}},
            {**project, "prompts": {"scenes": []}},
            {**project, "prompts": {"scenes": [{
                **valid["scenes"][0],
                "scene_id": str(uuid.uuid4()),
            }]}},
            {**project, "prompts": {"scenes": [{
                **valid["scenes"][0],
                "keyframe_i2v": {"final_prompt": "x", "source_fingerprint": "bad"},
            }]}},
        )
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_project_document(invalid)

    def test_schema_v5_normalizes_to_v7_without_mutating_file(self):
        project = self.storage.create_project("Legacy v5")
        project_file = self.projects_root / project["project_id"] / "project.json"
        legacy = {key: value for key, value in project.items() if key != "prompts"}
        legacy["schema_version"] = 5
        original = json.dumps(legacy, indent=2).encode("utf-8")
        project_file.write_bytes(original)

        loaded = self.storage.load_project(project["project_id"])

        self.assertEqual(loaded["schema_version"], 7)
        self.assertEqual(loaded["prompts"], {"scenes": []})
        self.assertEqual(project_file.read_bytes(), original)

    def test_v1_through_v4_normalization_adds_empty_prompt_state(self):
        project = self.storage.create_project("Legacy prompt shapes")
        base = {
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        v1 = {"schema_version": 1, **base}
        v2 = {**v1, "schema_version": 2, "source": {"master_audio": None, "lyrics_srt": None}, "scenes": []}
        v3 = {**v2, "schema_version": 3, "characters": [], "locations": []}
        v4 = {
            **v3,
            "schema_version": 4,
            "story_direction": {"story_brief": "", "visual_notes": ""},
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        for legacy in (v1, v2, v3, v4):
            with self.subTest(schema=legacy["schema_version"]):
                normalized = validate_project_document(legacy)
                self.assertEqual(normalized["schema_version"], 7)
                self.assertEqual(normalized["prompts"], {"scenes": []})

    def test_scene_replacement_resets_prompt_records(self):
        project = self._project()
        scene = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "Replacement"}],
            4_000,
        )
        visuals = default_visuals_for_scenes(scene)
        replaced = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "scenes": scene,
                "visuals": visuals,
                "prompts": default_prompts_for_scenes(scene),
                "storyboard": {"request_fingerprint": None, "scenes": []},
            },
            allow_visuals_change=True,
        )
        self.assertEqual(replaced["prompts"], default_prompts_for_scenes(scene))

    def test_fingerprint_is_deterministic_and_method_specific(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        first = build_prompt_source_fingerprint(project, scene_id, "reference2video")
        second = build_prompt_source_fingerprint(project, scene_id, "reference2video")
        keyframe = build_prompt_source_fingerprint(project, scene_id, "keyframe_i2v")
        self.assertEqual(first, second)
        self.assertNotEqual(first, keyframe)
        self.assertEqual(len(first), 64)

    def test_prompt_save_roundtrip_reports_current_and_ready_state(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled, validated = self._validated_relay(project)
        saved = save_scene_prompt(
            self.storage,
            project["project_id"],
            scene_id,
            {
                "generation_method": "reference2video",
                "final_prompt": validated["enhanced_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": validated["request_fingerprint"],
            },
        )
        state = prompt_scene_state(saved, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "current")
        self.assertEqual(state["saved_final_prompt"], validated["enhanced_prompt"])
        self.assertTrue(state["ready_for_render_prompt"])

    def test_prompt_state_becomes_stale_when_scene_inputs_change(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled, validated = self._validated_relay(project)
        project = save_scene_prompt(
            self.storage,
            project["project_id"],
            scene_id,
            {
                "generation_method": "reference2video",
                "final_prompt": validated["enhanced_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": validated["request_fingerprint"],
            },
        )
        changed_visuals = copy.deepcopy(project["visuals"])
        changed_visuals["scenes"][0]["reference2video"]["selected_references"] = changed_visuals["scenes"][0]["reference2video"]["selected_references"][:1]
        changed = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": changed_visuals},
            allow_visuals_change=True,
        )
        self.assertEqual(prompt_scene_state(changed, scene_id)["status"], "stale")

    def test_prompt_save_rejects_stale_source_fingerprint_without_mutating_prompt(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled, validated = self._validated_relay(project)
        project = save_scene_prompt(
            self.storage,
            project["project_id"],
            scene_id,
            {
                "generation_method": "reference2video",
                "final_prompt": validated["enhanced_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": validated["request_fingerprint"],
            },
        )
        changed_visuals = copy.deepcopy(project["visuals"])
        changed_visuals["scenes"][0]["reference2video"]["selected_references"] = changed_visuals["scenes"][0]["reference2video"]["selected_references"][:1]
        self.storage.save_project(
            project["project_id"],
            {**project, "visuals": changed_visuals},
            allow_visuals_change=True,
        )
        with self.assertRaises(PromptConflictError):
            save_scene_prompt(
                self.storage,
                project["project_id"],
                scene_id,
                {
                    "generation_method": "reference2video",
                    "final_prompt": "stale edit",
                    "source_fingerprint": compiled["source_fingerprint"],
                    "relay_fingerprint": compiled["source_fingerprint"],
                },
            )
        self.assertEqual(
            self.storage.load_project(project["project_id"])["prompts"]["scenes"][0]["reference2video"]["final_prompt"],
            validated["enhanced_prompt"],
        )

    def test_prompt_list_returns_computed_needs_gpt_status(self):
        project = self._project()
        listing = build_prompt_list(project, self.storage)
        self.assertEqual(listing["schema_version"], 7)
        self.assertEqual(listing["scenes"][0]["status"], "needs_gpt")
        self.assertFalse(listing["scenes"][0]["ready_for_render_prompt"])

    def test_h3_prompt_excludes_internal_diagnostic_metadata(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        prompt = result["deterministic_prompt"]
        self.assertNotIn(project["project_id"], prompt)
        self.assertNotIn(project["characters"][0]["character_id"], prompt)
        self.assertNotIn(project["locations"][0]["location_id"], prompt)
        self.assertNotIn(project["characters"][0]["references"][0]["reference_id"], prompt)
        self.assertNotIn("source_cue_numbers", prompt)
        self.assertNotIn("vesper-wide.png", prompt)
        self.assertNotIn("chapel-wide.png", prompt)
        self.assertNotIn("IMMUTABLE STRUCTURAL FACTS", prompt)
        self.assertIn("subject_definitions:", prompt)
        self.assertIn("<Audio 1>", prompt)

    def test_keyframe_prompt_makes_accepted_image_the_opening_authority(self):
        project = self._project(method="keyframe_i2v")
        project = self._with_accepted_keyframe(project)
        result = compile_scene_prompt(project, project["scenes"][0]["scene_id"])
        prompt = result["deterministic_prompt"]
        self.assertIn(
            "The accepted first frame establishes the opening: "
            "Actual opening: Vesper in a black jacket beside the chapel wall.",
            prompt,
        )
        self.assertNotIn("Opening state is defined exclusively", prompt)
        self.assertNotIn("Actual accepted-keyframe description", prompt)
        self.assertIn("Actual opening: Vesper in a black jacket", prompt)
        self.assertNotIn("Intended opening: a bright red scarf", prompt)
        self.assertNotIn("Forward-generation intent", prompt)
        self.assertIn("The performance continues: Vesper moves through the frame.", prompt)

    def test_lyric_policy_is_performance_only(self):
        performance = self._project(scene_type="performance")
        narrative = self._project(scene_type="narrative")
        instrumental = self._project(scene_type="performance", instrumental=True)
        performance_prompt = compile_scene_prompt(performance, performance["scenes"][0]["scene_id"])["deterministic_prompt"]
        narrative_prompt = compile_scene_prompt(narrative, narrative["scenes"][0]["scene_id"])["deterministic_prompt"]
        instrumental_prompt = compile_scene_prompt(instrumental, instrumental["scenes"][0]["scene_id"])["deterministic_prompt"]
        self.assertIn("The lyric remains verbatim.", performance_prompt)
        self.assertNotIn("The lyric remains verbatim.", narrative_prompt)
        self.assertIn("do not invent vocal words", instrumental_prompt)
        self.assertNotIn("The lyric remains verbatim.", instrumental_prompt)

    def test_custom_gpt_relay_request_and_machine_reassembly_contract(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        request = build_prompt_relay_request(project, scene_id)
        self.assertEqual(request["request_fingerprint"], result["source_fingerprint"])
        self.assertEqual(request["prompt_relay_request_version"], 1)
        self.assertIn("response_contract", request)
        response = validate_prompt_relay_response(result, {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": request["request_fingerprint"],
            "enhanced_description": "A restrained camera drift follows the performer.",
        })
        self.assertEqual(response["status"], "validated")
        self.assertIn("<Picture 1>", response["enhanced_prompt"] if result["generation_method"] == "keyframe_i2v" else result["deterministic_prompt"])
        self.assertNotIn("IMMUTABLE STRUCTURAL FACTS", response["enhanced_prompt"])

    def test_official_i2va_prompt_structure_and_performance_lyric_syntax(self):
        project = self._project(method="keyframe_i2v")
        project = self._with_accepted_keyframe(project)
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        prompt = result["deterministic_prompt"]
        self.assertIn("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.", prompt)
        self.assertIn("integrated_multimodal_description:", prompt)
        self.assertIn("overall_soundscape:", prompt)
        self.assertIn("non_diegetic_music:", prompt)
        self.assertIn("Vesper (S1) sings: <d>[English] The lyric remains verbatim.</d>", prompt)
        relay_request = build_prompt_relay_request(project, scene_id, "keyframe_i2v")
        relay_json = json.dumps(relay_request)
        self.assertNotIn("<Picture 1>", relay_json)
        self.assertNotIn("opening_reference", relay_json)
        self.assertNotIn("integrated_multimodal_description:", relay_json)
        self.assertTrue(relay_request["method_specific"]["accepted_keyframe_opening_state_is_authoritative"])

    def test_final_prompt_validation_keeps_method_contracts(self):
        keyframe = self._project(method="keyframe_i2v")
        keyframe = self._with_accepted_keyframe(keyframe)
        keyframe_result = compile_scene_prompt(keyframe, keyframe["scenes"][0]["scene_id"])
        self.assertEqual(
            validate_final_prompt(keyframe_result, keyframe_result["deterministic_prompt"]),
            keyframe_result["deterministic_prompt"],
        )
        for invalid_suffix in ("<Picture 2>", "<Subject 1>", "<Audio 1>", "<Location 1>"):
            with self.subTest(invalid_suffix=invalid_suffix):
                with self.assertRaises(ProjectValidationError):
                    validate_final_prompt(keyframe_result, keyframe_result["deterministic_prompt"] + "\n" + invalid_suffix)

        ref2v = self._project(method="reference2video")
        ref2v_result = compile_scene_prompt(ref2v, ref2v["scenes"][0]["scene_id"])
        with self.assertRaises(ProjectValidationError):
            validate_final_prompt(ref2v_result, "<Picture 1> <Picture 2> <Subject 1> <Subject 2> <Audio 2>")
        with self.assertRaises(ProjectValidationError):
            validate_final_prompt(ref2v_result, "<Picture 1> <Picture 2> <Subject 1> <Subject 2> <Audio 1> <Location 1>")
        self.assertEqual(
            validate_final_prompt(ref2v_result, ref2v_result["deterministic_prompt"]),
            ref2v_result["deterministic_prompt"],
        )

    def test_prompt_saveability_state_machine_and_first_edit_contract(self):
        self._run_prompt_state_script(
            """
const fingerprint = "a".repeat(64);
const needsGpt = {status: "needs_gpt", source_fingerprint: fingerprint, saved_source_fingerprint: null, saved_relay_fingerprint: "", saved_final_prompt: "", ready_for_render_prompt: false};
const deterministicOnly = {text: "deterministic candidate", sourceFingerprint: fingerprint, relayFingerprint: "", dirty: false};
if (state.canSavePromptDraft(needsGpt, deterministicOnly)) throw new Error("deterministic-only candidate must not be saveable");
if (state.promptDraftReadyForRender(needsGpt, deterministicOnly)) throw new Error("deterministic-only candidate must not be ready");
const applied = {text: "GPT-derived prompt", sourceFingerprint: fingerprint, relayFingerprint: fingerprint, dirty: false};
applied.dirty = state.promptDraftIsDirty(needsGpt, applied);
if (!applied.dirty || state.promptDraftStatus(needsGpt, applied) !== "unsaved") throw new Error("valid GPT apply must be unsaved");
if (!state.canSavePromptDraft(needsGpt, applied)) throw new Error("valid GPT apply must be saveable");
const stale = {status: "stale", source_fingerprint: fingerprint, saved_source_fingerprint: "b".repeat(64), saved_relay_fingerprint: "b".repeat(64), saved_final_prompt: "saved", ready_for_render_prompt: false};
const staleDraft = {text: "saved", sourceFingerprint: stale.saved_source_fingerprint, relayFingerprint: stale.saved_relay_fingerprint, dirty: false};
if (state.canSavePromptDraft(stale, staleDraft)) throw new Error("stale acknowledgement must require a fresh relay");
const current = {status: "current", source_fingerprint: fingerprint, saved_source_fingerprint: fingerprint, saved_relay_fingerprint: fingerprint, saved_final_prompt: "saved", ready_for_render_prompt: true};
const currentDraft = {text: "saved", sourceFingerprint: fingerprint, relayFingerprint: fingerprint, dirty: false};
if (state.canSavePromptDraft(current, currentDraft)) throw new Error("unchanged current prompt must not be saveable");
if (!state.promptDraftReadyForRender(current, currentDraft)) throw new Error("clean current prompt must be ready");
currentDraft.text += " edited";
currentDraft.dirty = state.promptDraftIsDirty(current, currentDraft);
if (!state.canSavePromptDraft(current, currentDraft) || state.promptDraftReadyForRender(current, currentDraft)) throw new Error("edited GPT-derived prompt must be saveable but not ready");
"""
        )

        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        handler_start = extension.index('editor.addEventListener("input"')
        handler = extension[
            handler_start:
            extension.index("finalSection.append(actions)", handler_start)
        ]
        self.assertIn("draft.dirty = promptDraftIsDirty(item, draft);", handler)
        self.assertIn("saveButton.disabled = blocked || !canSavePromptDraft(item, draft);", handler)
        self.assertNotIn("renderPromptState", handler)
        self.assertNotIn("renderProjectState", handler)
        self.assertNotIn("sourceFingerprint =", handler)

    def test_storyboard_request_copy_state_and_exact_payload_contract(self):
        self._run_prompt_state_script(
            """
const request = {request_fingerprint: "abc", story_direction: {storyboard_mode: "loose"}};
if (state.canCopyStoryboardRequest(null, false)) throw new Error("missing request must not copy");
if (!state.canCopyStoryboardRequest(request, false)) throw new Error("current request must copy");
if (state.canCopyStoryboardRequest(request, true)) throw new Error("invalidated request must not copy");
if (state.canCopyStoryboardRequest(request, false, true)) throw new Error("blocked request must not copy");
const visible = state.storyboardRequestJson(request);
if (visible !== JSON.stringify(request, null, 2)) throw new Error("visible JSON is not exact");
if (!state.canCopyStoryboardRequest(request, false)) throw new Error("regenerated request must copy");
"""
        )

        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        generate = extension[extension.index("async function generateStoryboardRequest(root)"):extension.index("async function copyStoryboardRequest(root)")]
        copy_action = extension[extension.index("async function copyStoryboardRequest(root)"):extension.index("async function validateStoryboardResponseInput(root)")]
        self.assertIn("builderState.storyboardRequest = request;", generate)
        self.assertIn("builderState.storyboardRequestStale = false;", generate)
        self.assertIn("storyboardRequestJson(builderState.storyboardRequest)", copy_action)
        self.assertIn("navigator.clipboard.writeText(requestJson)", copy_action)
        self.assertNotIn("storyboardResponseText", copy_action)
        self.assertNotIn("promptDraft", copy_action)

    def test_custom_gpt_relay_copy_and_apply_state_follow_current_request(self):
        self._run_prompt_state_script(
            """
const item = {scene_id: "scene-1", generation_method: "keyframe_i2v", source_fingerprint: "a".repeat(64)};
const relay = {request: {prompt_relay_request_version: 1, scene_id: "scene-1", generation_method: "keyframe_i2v", request_fingerprint: item.source_fingerprint}, responseText: "{\\"enhanced_description\\":\\"camera drift\\"}"};
if (!state.canCopyPromptRelayRequest(relay, item)) throw new Error("current relay request must copy");
if (!state.canApplyPromptRelayResponse(relay, item)) throw new Error("current relay response must apply");
if (state.canCopyPromptRelayRequest({...relay, request: {...relay.request, request_fingerprint: "b".repeat(64)}}, item)) throw new Error("stale relay request must not copy");
if (state.canApplyPromptRelayResponse({...relay, responseText: ""}, item)) throw new Error("empty relay response must not apply");
"""
        )
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        self.assertIn("async function generatePromptRelayRequest(root, sceneId, generationMethod)", extension)
        self.assertIn("async function copyPromptRelayRequest(root, sceneId, generationMethod)", extension)
        self.assertIn("async function applyPromptRelayResponse(root, sceneId, generationMethod)", extension)
        self.assertIn('promptPath(projectId, sceneId, "relay-request")', extension)
        self.assertIn('promptPath(projectId, sceneId, "relay-response")', extension)
        self.assertIn("draft.text = result.enhanced_prompt;", extension)
        self.assertNotIn("setCurrentProject(root, project", extension[extension.index("async function applyPromptRelayResponse"):extension.index("async function savePrompt")])
        self.assertIn("capturePromptViewport(root, sceneId)", extension[extension.index("async function generatePromptRelayRequest"):extension.index("async function savePrompt")])

    def test_frontend_exposes_prompts_without_permanent_requirements_dashboard_or_browser_storage(self):
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        styles = Path(__file__).parents[1].joinpath("web", "builder.css").read_text(encoding="utf-8")
        routes = Path(__file__).parents[1].joinpath("backend", "routes.py").read_text(encoding="utf-8")
        for marker in (
            'data-mvb-view="prompts"',
            'data-mvb-prompts',
            'data-mvb-prompt-scenes',
            'data-mvb-prompt-empty',
            'data-mvb-prompt-relay-request',
            'data-mvb-prompt-relay-response',
            'data-mvb-prompt-relay-copy',
            'data-mvb-prompt-relay-apply',
            'method: "PUT"',
            'final_prompt: draft.text',
            'source_fingerprint: draft.sourceFingerprint',
            'relay_fingerprint: draft.relayFingerprint',
            'function flushPromptDrafts(root)',
            'function hasUnsavedPromptDrafts(state)',
            'function reconcilePromptDrafts',
            'Generate GPT Request',
            'Copy Request JSON',
            'Paste GPT Response',
            'Apply GPT Response',
            'Save Prompt',
        ):
            self.assertIn(marker, extension)
        self.assertNotIn('Use Deterministic', extension)
        self.assertNotIn('Optional manual relay', extension)
        self.assertIn('dirty: false', extension)
        self.assertNotIn('data-mvb-requirements', extension)
        self.assertNotIn('data-mvb-refresh-requirements', extension)
        self.assertNotIn('Local checks', extension)
        self.assertNotIn('Required production', extension)
        self.assertNotIn('.mvb-requirements', styles)
        self.assertIn('/music-video-builder/requirements', routes)
        self.assertIn('await asyncio.to_thread(build_requirements_report)', routes)
        self.assertNotIn('void loadRequirements(root, true);', extension)
        self.assertNotIn('Ollama', extension)
        self.assertNotIn('ollama', extension)
        self.assertNotIn('localhost:11434', extension)
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertNotIn('data-mvb-view="render"', extension)

    def test_frontend_flushes_prompt_drafts_by_key_before_close_and_transition(self):
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        flush = extension[extension.index("async function flushPromptDrafts(root)"):extension.index("function scheduleAutosave(root)")]
        self.assertIn('Object.entries(builderState.promptDrafts || {})', flush)
        self.assertIn('fetchJson(promptPath(projectId, sceneId)', flush)
        self.assertIn('setCurrentProject(root, project, { reconciledPromptKey: key });', flush)
        self.assertNotIn('promptItemFor(sceneId, generationMethod)', flush)
        transition = extension[extension.index("async function closeBuilder(root)"):extension.index("function saveStateLabel")]
        self.assertIn("flushProjectTransition(root)", transition)
        self.assertIn("if (!saved)", transition)
        project_transition = extension[extension.index("async function flushProjectTransition(root)"):extension.index("async function checkBackend(root)")]
        self.assertIn("flushCurrentProject(root)", project_transition)
        self.assertIn("flushPromptDrafts(root)", project_transition)

    def test_prompts_preserve_scroll_anchor_across_state_refreshes(self):
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        self.assertIn("function capturePromptViewport(root, anchorSceneId = null)", extension)
        self.assertIn('root?.querySelector("[data-mvb-prompts]")', extension)
        self.assertIn("scroller.scrollTop", extension)
        self.assertIn("function restorePromptViewport(root, viewport)", extension)
        self.assertIn("window.requestAnimationFrame(apply);", extension)
        self.assertIn("card.dataset.mvbPromptSceneId = item.scene_id;", extension)
        self.assertIn("card.dataset.mvbPromptGenerationMethod = item.generation_method;", extension)
        render = extension[extension.index("function renderPromptState(root, options = {})"):extension.index("async function loadPromptCards(root, silent = false, options = {})")]
        self.assertIn("const promptViewport = options.promptViewport || capturePromptViewport(root, options.anchorSceneId);", render)
        self.assertIn("restorePromptViewport(root, promptViewport);", render)
        load = extension[extension.index("async function loadPromptCards(root, silent = false, options = {})"):extension.index("async function copyPrompt(root, sceneId, generationMethod)")]
        self.assertIn("const promptViewport = options.promptViewport || capturePromptViewport(root, options.anchorSceneId);", load)
        self.assertIn("renderPromptState(root, { promptViewport });", load)
        save = extension[extension.index("async function savePrompt(root, sceneId, generationMethod)"):extension.index("function closeResourceDialogs(root)")]
        self.assertIn("const promptViewport = capturePromptViewport(root, sceneId);", save)
        self.assertIn("renderProjectState(root, { promptViewport });", save)
        self.assertIn("loadPromptCards(root, true, { promptViewport });", save)
        relay = extension[extension.index("async function generatePromptRelayRequest(root, sceneId, generationMethod)"):extension.index("async function savePrompt(root, sceneId, generationMethod)")]
        self.assertIn("const promptViewport = capturePromptViewport(root, sceneId);", relay)
        self.assertIn("renderProjectState(root, { promptViewport });", relay)
        self.assertIn("async function copyPromptRelayRequest(root, sceneId, generationMethod)", relay)
        self.assertIn("async function applyPromptRelayResponse(root, sceneId, generationMethod)", relay)
        apply = extension[extension.index('async function applyPromptRelayResponse(root, sceneId, generationMethod)'):extension.index('async function savePrompt(root, sceneId, generationMethod)')]
        self.assertIn("capturePromptViewport(root, sceneId)", apply)
        input_start = extension.index('editor.addEventListener("input"')
        input_handler = extension[input_start:extension.index("finalSection.append(actions)", input_start)]
        self.assertNotIn("renderPromptState", input_handler)
        self.assertNotIn("renderProjectState", input_handler)

    def test_routes_expose_exact_prompt_preview_list_save_and_enhance_contract(self):
        routes = Path(__file__).parents[1].joinpath("backend", "routes.py").read_text(encoding="utf-8")
        for route in (
            '/projects/{project_id}/scenes/{scene_id}/prompt/preview',
            '/projects/{project_id}/prompts',
            '/projects/{project_id}/scenes/{scene_id}/prompt',
            '/projects/{project_id}/scenes/{scene_id}/prompt/relay-request',
            '/projects/{project_id}/scenes/{scene_id}/prompt/relay-response',
        ):
            self.assertIn(route, routes)
        self.assertIn("PromptConflictError", routes)
        self.assertIn("build_prompt_list", routes)
        self.assertIn("save_scene_prompt", routes)
        self.assertIn("build_prompt_relay_request", routes)
        self.assertIn("validate_prompt_relay_response", routes)
        self.assertNotIn("enhance_prompt_with_ollama", routes)

    def test_7c_product_sources_contain_no_local_lm_support_markers(self):
        root = Path(__file__).parents[1]
        product_sources = (
            root / "backend" / "prompt_service.py",
            root / "backend" / "requirements.py",
            root / "backend" / "routes.py",
            root / "web" / "extension.js",
            root / "web" / "prompt_state.js",
            root / "docs" / "ComfyUI Music Video Builder — Development Plan.md",
        )
        markers = ("ollama", "localhost:11434", "qwen3:8b", "enhance_prompt_with_ollama", "prompt/enhance")
        for path in product_sources:
            contents = path.read_text(encoding="utf-8").lower()
            for marker in markers:
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, contents)


if __name__ == "__main__":
    unittest.main()
