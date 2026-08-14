import base64
import copy
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import ProjectStorage, default_prompts_for_scenes, validate_project_document
from backend.prompt_service import (
    PromptConflictError,
    build_prompt_relay_request,
    compile_scene_prompt,
    prompt_scene_state,
    save_scene_prompt,
    validate_final_prompt,
    validate_prompt_relay_response,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT.joinpath("web", "extension.js").read_text(encoding="utf-8")
PROMPT_STATE = ROOT.joinpath("web", "prompt_state.js").read_bytes()
STYLES = ROOT.joinpath("web", "builder.css").read_text(encoding="utf-8")


class Phase7C6TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    @staticmethod
    def _reference(name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": name,
        }

    def _project(self, method="reference2video"):
        project = self.storage.create_project("Phase 7C.6")
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "Hold the line."}],
            4_000,
        )
        character_id = str(uuid.uuid4())
        location_id = str(uuid.uuid4())
        character_reference = self._reference("vesper.png")
        location_reference = self._reference("chapel.png")
        characters = [{
            "character_id": character_id,
            "name": "Vesper",
            "role": "performer",
            "appearance": "Tousled copper hair and a slim build.",
            "outfit": "Black leather jacket and dark boots.",
            "references": [character_reference],
        }]
        locations = [{
            "location_id": location_id,
            "name": "Chapel",
            "description": "Cold stone walls, reflective water, and hard white side light.",
            "references": [location_reference],
        }]
        visuals = default_visuals_for_scenes(scenes)
        visual = visuals["scenes"][0]
        visual["generation_method"] = method
        visual["reference2video"]["selected_references"] = [
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
        if method == "keyframe_i2v":
            asset_id = str(uuid.uuid4())
            stored_name = f"{asset_id}.png"
            (self.storage.project_directory(project["project_id"]) / "keyframes" / stored_name).write_bytes(b"keyframe")
            visual["keyframe_i2v"].update({
                "accepted_keyframe": {
                    "asset_id": asset_id,
                    "stored_name": stored_name,
                    "original_name": "accepted.png",
                },
                "actual_keyframe_description": "Vesper stands in a black jacket beside a flooded chapel wall.",
                "intended_keyframe_description": "A contradictory bright red coat.",
            })
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
                        "cue_count": 1,
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
        storyboard = validate_storyboard_response(project, {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [{
                "scene_id": scenes[0]["scene_id"],
                "scene_type": "performance",
                "character_ids": [character_id],
                "location_id": location_id,
                "action": "Vesper performs with controlled intensity.",
                "visual_instructions": "Cold blue-grey gothic performance staging with hard white side light.",
                "camera_direction": "A slow lateral camera drift.",
                "motion_direction": "Restrained forward movement.",
                "continuity_notes": "Keep Vesper and the chapel spatially consistent.",
                "required_references": [],
            }],
        })
        return self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )

    @staticmethod
    def _relay(project, method, description):
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id, method)
        validated = validate_prompt_relay_response(compiled, {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": compiled["source_fingerprint"],
            "enhanced_description": description,
        })
        return compiled, validated

    def _save_validated(self, project, method, compiled, validated, final_prompt=None):
        return save_scene_prompt(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            {
                "generation_method": method,
                "final_prompt": final_prompt or validated["enhanced_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": validated["request_fingerprint"],
            },
        )

    def _run_prompt_state(self, script):
        module_url = "data:text/javascript;base64," + base64.b64encode(PROMPT_STATE).decode("ascii")
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", f"const state = await import('{module_url}');\n{script}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_schema_v7_and_narrow_v6_prompt_provenance_migration(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id, "reference2video")
        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        legacy = copy.deepcopy(project)
        legacy["schema_version"] = 6
        legacy_record = legacy["prompts"]["scenes"][0]["reference2video"]
        legacy_record["final_prompt"] = "Preserved legacy final prompt."
        legacy_record["source_fingerprint"] = compiled["source_fingerprint"]
        for prompt_scene in legacy["prompts"]["scenes"]:
            for method in ("keyframe_i2v", "reference2video"):
                prompt_scene[method].pop("relay_fingerprint")
        original = json.dumps(legacy, indent=2).encode("utf-8")
        project_file.write_bytes(original)

        loaded = self.storage.load_project(project["project_id"])
        listed = self.storage.list_projects()

        self.assertEqual(loaded["schema_version"], 7)
        migrated = loaded["prompts"]["scenes"][0]["reference2video"]
        self.assertEqual(migrated["final_prompt"], "Preserved legacy final prompt.")
        self.assertEqual(migrated["source_fingerprint"], compiled["source_fingerprint"])
        self.assertEqual(migrated["relay_fingerprint"], "")
        self.assertEqual(prompt_scene_state(loaded, scene_id)["status"], "needs_gpt")
        self.assertFalse(prompt_scene_state(loaded, scene_id)["ready_for_render_prompt"])
        self.assertEqual(len(listed["projects"]), 1)
        self.assertEqual(project_file.read_bytes(), original)

    def test_ref2va_uses_exact_canonical_sections_and_machine_relationships(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id, "reference2video")
        prompt = result["deterministic_prompt"]
        sections = (
            "subject_definitions",
            "summary",
            "retention_analysis",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        )
        self.assertEqual([line[:-1] for line in prompt.splitlines() if line[:-1] in sections and line.endswith(":")], list(sections))
        for section in sections[1:]:
            self.assertIn(f"\n\n{section}:\n", prompt)
        subject_block = prompt.split("\n\nsummary:", 1)[0]
        self.assertIn("<Subject 1> is Vesper, the performer represented by <Picture 1>", subject_block)
        self.assertIn("appearance continuity", subject_block)
        self.assertIn("wardrobe continuity", subject_block)
        self.assertIn("<Subject 2> is Chapel, the location represented by <Picture 2>", subject_block)
        self.assertNotIn("Continuity details for", prompt)
        self.assertIn("<Audio 1> is the authoritative master-song scene-audio segment", subject_block)
        self.assertNotIn("fully_copy", subject_block)
        self.assertIn("summary:\n[reference generation + audio reuse] ", prompt)
        self.assertIn("<Subject 1> (appears in [Shot 1]): fully_preserved - ", prompt)
        self.assertIn("<Subject 2> (appears in [Shot 1]): fully_preserved - ", prompt)
        self.assertIn("<Audio 1>: fully_copy - <Audio 1> is reused 1:1", prompt)
        self.assertNotIn("— fully_", prompt)
        detailed = prompt.split("detailed_description:\n", 1)[1].split("\n\noverall_soundscape:", 1)[0]
        self.assertEqual(detailed.count("[Shot 1]"), 1)
        self.assertNotIn("[Shot 2]", prompt)
        self.assertEqual(validate_final_prompt(result, prompt), prompt)

    def test_ref2va_style_precedes_shot_and_enhanced_description_is_intact(self):
        project = self._project()
        enhanced = (
            "<Subject 1> holds a controlled performance stance while the camera drifts laterally through "
            "<Subject 2>; hard white side light catches the flooded stone floor without introducing a cut."
        )
        result, validated = self._relay(project, "reference2video", enhanced)
        prompt = validated["enhanced_prompt"]
        detailed = prompt.split("detailed_description:\n", 1)[1].split("\n\noverall_soundscape:", 1)[0]
        self.assertTrue(detailed.startswith("Overall visual direction: Cold blue-grey gothic performance staging"))
        self.assertIn("\n\n[Shot 1] ", detailed)
        shot_content = detailed.split("[Shot 1] ", 1)[1]
        self.assertTrue(shot_content.startswith(enhanced + "\n"))
        self.assertIn("Vesper (S1) sings: <d>[English] Hold the line.</d>", shot_content)
        self.assertEqual(shot_content.count("Hold the line."), 1)
        self.assertEqual(detailed.count("[Shot 1]"), 1)
        self.assertEqual(validate_final_prompt(result, prompt), prompt)
        saved = self._save_validated(project, "reference2video", result, validated)
        self.assertEqual(prompt_scene_state(saved, project["scenes"][0]["scene_id"])["status"], "current")

    def test_i2va_uses_exact_opening_core_fields_and_one_machine_shot(self):
        project = self._project(method="keyframe_i2v")
        enhanced = "Vesper advances slowly as the camera maintains a restrained lateral drift."
        result, validated = self._relay(project, "keyframe_i2v", enhanced)
        prompt = validated["enhanced_prompt"]
        opening = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> "
            "(from [Shot 1]) is fully referenced."
        )
        self.assertTrue(prompt.startswith(opening + "\n\nintegrated_multimodal_description: [Shot 1] "))
        self.assertNotIn(opening + "\n\n\n", prompt)
        self.assertLess(prompt.index("integrated_multimodal_description:"), prompt.index("overall_soundscape:"))
        self.assertLess(prompt.index("overall_soundscape:"), prompt.index("non_diegetic_music:"))
        self.assertIn(
            "The accepted first frame establishes the opening: "
            "Vesper stands in a black jacket beside a flooded chapel wall.",
            prompt,
        )
        self.assertNotIn("Opening state is defined exclusively", prompt)
        self.assertIn("Vesper stands in a black jacket beside a flooded chapel wall.", prompt)
        self.assertNotIn("contradictory bright red coat", prompt)
        self.assertIn(enhanced, prompt)
        self.assertEqual(prompt.count("[Shot 1]"), 2)  # opening reference plus one description wrapper
        self.assertEqual(validate_final_prompt(result, prompt), prompt)
        saved = self._save_validated(project, "keyframe_i2v", result, validated)
        self.assertEqual(prompt_scene_state(saved, project["scenes"][0]["scene_id"])["status"], "current")

    def test_mandatory_relay_provenance_controls_save_stale_and_readiness(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id, "reference2video")
        initial = prompt_scene_state(project, scene_id, storage=self.storage)
        self.assertEqual(initial["status"], "needs_gpt")
        self.assertFalse(initial["ready_for_render_prompt"])
        with self.assertRaises(PromptConflictError):
            save_scene_prompt(self.storage, project["project_id"], scene_id, {
                "generation_method": "reference2video",
                "final_prompt": compiled["deterministic_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": "",
            })

        compiled, validated = self._relay(project, "reference2video", "<Subject 1> performs within <Subject 2> as the camera drifts slowly.")
        saved = self._save_validated(project, "reference2video", compiled, validated)
        record = saved["prompts"]["scenes"][0]["reference2video"]
        self.assertEqual(record["relay_fingerprint"], compiled["source_fingerprint"])
        reopened = self.storage.load_project(project["project_id"])
        self.assertEqual(prompt_scene_state(reopened, scene_id, storage=self.storage)["status"], "current")
        self.assertTrue(prompt_scene_state(reopened, scene_id, storage=self.storage)["ready_for_render_prompt"])
        self.assertEqual(reopened["prompts"]["scenes"][0]["keyframe_i2v"]["relay_fingerprint"], "")

        changed_storyboard = copy.deepcopy(reopened["storyboard"])
        changed_storyboard["scenes"][0]["motion_direction"] = "A faster but still continuous forward movement."
        changed = self.storage.save_project(
            reopened["project_id"],
            {**reopened, "storyboard": changed_storyboard},
            allow_storyboard_change=True,
        )
        changed_state = prompt_scene_state(changed, scene_id, storage=self.storage)
        self.assertEqual(changed_state["status"], "stale")
        self.assertFalse(changed_state["ready_for_render_prompt"])
        with self.assertRaises(PromptConflictError):
            save_scene_prompt(self.storage, changed["project_id"], scene_id, {
                "generation_method": "reference2video",
                "final_prompt": record["final_prompt"],
                "source_fingerprint": changed_state["source_fingerprint"],
                "relay_fingerprint": record["relay_fingerprint"],
            })

    def test_identical_text_provenance_and_unsaved_readiness_frontend_contract(self):
        self._run_prompt_state(
            """
const fp = "a".repeat(64);
const missingProvenance = {status: "needs_gpt", source_fingerprint: fp, saved_source_fingerprint: fp, saved_relay_fingerprint: "", saved_final_prompt: "same", ready_for_render_prompt: false};
const appliedSame = {text: "same", sourceFingerprint: fp, relayFingerprint: fp, dirty: false};
appliedSame.dirty = state.promptDraftIsDirty(missingProvenance, appliedSame);
if (!appliedSame.dirty || !state.canSavePromptDraft(missingProvenance, appliedSame)) throw new Error("identical text with missing provenance must save");
const current = {...missingProvenance, status: "current", saved_relay_fingerprint: fp, ready_for_render_prompt: true};
const noOp = {text: "same", sourceFingerprint: fp, relayFingerprint: fp, dirty: false};
noOp.dirty = state.promptDraftIsDirty(current, noOp);
if (noOp.dirty || state.canSavePromptDraft(current, noOp)) throw new Error("identical current provenance must be a no-op");
if (!state.promptDraftReadyForRender(current, noOp)) throw new Error("clean current prompt must be ready");
noOp.text += " edited";
noOp.dirty = state.promptDraftIsDirty(current, noOp);
if (state.promptDraftReadyForRender(current, noOp)) throw new Error("unsaved draft must not be ready");
"""
        )

    def test_request_shaped_response_is_rejected_without_prompt_mutation(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        request = build_prompt_relay_request(project, scene_id)
        before = copy.deepcopy(project["prompts"])
        with self.assertRaisesRegex(Exception, "request, not a Prompt Director response"):
            validate_prompt_relay_response(compiled, request)
        self.assertEqual(self.storage.load_project(project["project_id"])["prompts"], before)

    def test_prompt_ui_order_naming_and_mandatory_relay_contract(self):
        render = EXTENSION[
            EXTENSION.index("function renderPromptState(root, options = {})"):
            EXTENSION.index("async function loadPromptCards(root, silent = false, options = {})")
        ]
        self.assertIn(">Final Prompts</h2>", EXTENSION)
        self.assertIn('editorLabel.textContent = "Final Prompt";', render)
        self.assertNotIn("Final H3 Prompt", EXTENSION)
        self.assertNotIn("Use Deterministic", EXTENSION)
        self.assertNotIn("Optional manual relay", EXTENSION)
        self.assertNotIn("Prompt Director · Vesper H3 Prompt Director", EXTENSION)
        self.assertLess(render.index('relayInstruction.className = "mvb-prompt-instruction"'), render.index('makeVisualButton("Generate GPT Request"'))
        self.assertLess(render.index('finalHeading.textContent = "Final Prompt"'), render.index('promptContextHeading.textContent = "Prompt Context"'))
        self.assertLess(render.index('promptContextHeading.textContent = "Prompt Context"'), render.index('mappingSummary.textContent = "Reference Mapping"'))
        self.assertLess(render.index('mappingSummary.textContent = "Reference Mapping"'), render.index('relayRequestSummary.textContent = "View Request JSON"'))
        self.assertLess(render.index('relayRequestSummary.textContent = "View Request JSON"'), render.index('deterministicSummary.textContent = "View Deterministic Prompt"'))
        self.assertIn("if (promptFinalIsVisible(item, draft))", render)
        self.assertIn('editor.readOnly = !promptDraftHasCurrentRelay(item, draft);', render)
        self.assertIn('makeVisualButton("Apply GPT Response"', render)
        self.assertIn('relay_fingerprint: draft.relayFingerprint', EXTENSION)
        self.assertIn('card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));', render)
        self.assertIn("mvb-prompt-workflow-section", STYLES)

    def test_mt46_resources_and_mt47_single_authoritative_launcher_remain(self):
        self.assertIn("function renderResourceDialogReferences", EXTENSION)
        self.assertIn("setPendingResourceReferenceIds", EXTENSION)
        launcher = EXTENSION[
            EXTENSION.index("async function openDedicatedGpt(root, director)"):
            EXTENSION.index("async function closeBuilder(root)")
        ]
        self.assertEqual(launcher.count("fetchJson(path"), 1)
        self.assertNotIn("window.open", launcher)
        self.assertNotIn("allow pop-ups", launcher)
        self.assertIn("clearGptLauncherErrors();", launcher)
        self.assertIn('openDedicatedGpt(root, "prompts")', EXTENSION)
        self.assertIn("function capturePromptViewport", EXTENSION)
        self.assertIn("window.requestAnimationFrame(apply);", EXTENSION)

    def test_schema_is_exactly_v7_and_phase8_surface_is_absent(self):
        project = self.storage.create_project("Schema v7")
        self.assertEqual(project["schema_version"], 7)
        self.assertEqual(validate_project_document(project), project)
        self.assertNotIn('data-mvb-view="render"', EXTENSION)


if __name__ == "__main__":
    unittest.main()
