import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import ProjectStorage, default_prompts_for_scenes
from backend.prompt_service import (
    PromptCompilationError,
    PromptConflictError,
    build_prompt_relay_request,
    compile_scene_prompt,
    prompt_scene_state,
    save_scene_prompt,
    validate_prompt_relay_response,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
STYLES = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")


class Phase7C8TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")

    @staticmethod
    def _reference(name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": name,
        }

    def _project(self, method="reference2video"):
        project = self.storage.create_project("Phase 7C.8")
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "No god in the house."}],
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
            "description": "Cold stone walls and hard white side light.",
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
                "actual_keyframe_description": "Vesper stands beside the flooded chapel wall.",
                "intended_keyframe_description": "A discarded intended opening.",
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
                "visual_instructions": "Cold gothic performance staging.",
                "camera_direction": "A slow lateral camera drift.",
                "motion_direction": "Restrained forward movement.",
                "continuity_notes": "Keep Vesper and the chapel consistent.",
                "required_references": [],
            }],
        })
        return self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )

    @staticmethod
    def _response(compiled, description):
        return {
            "prompt_relay_response_version": 1,
            "scene_id": compiled["scene_id"],
            "request_fingerprint": compiled["source_fingerprint"],
            "enhanced_description": description,
        }

    def _save_current_prompt(self, project):
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        validated = validate_prompt_relay_response(
            compiled,
            self._response(
                compiled,
                "Controlled breath, restrained mouth movement, and tightening through the jaw carry the performance.",
            ),
        )
        return save_scene_prompt(
            self.storage,
            project["project_id"],
            scene_id,
            {
                "generation_method": compiled["generation_method"],
                "final_prompt": validated["enhanced_prompt"],
                "source_fingerprint": compiled["source_fingerprint"],
                "relay_fingerprint": validated["request_fingerprint"],
            },
        )

    @staticmethod
    def _function_body(source, name, next_name):
        start = source.index(name)
        end = source.index(next_name, start)
        return source[start:end]

    def test_relay_actions_follow_textarea_as_one_ordered_wrapping_group(self):
        render = self._function_body(
            EXTENSION,
            "function renderPromptState(root, options = {})",
            "async function loadPromptCards(root, silent = false, options = {})",
        )
        relay = render[render.index('const relayDetails = document.createElement("section")'):render.index("body.append(relayDetails);")]
        paste = relay.index("relayBody.append(relayResponseField);")
        action_group = relay.index('relayActions.className = "mvb-prompt-actions mvb-prompt-relay-actions";')
        generate = relay.index('makeVisualButton("Generate GPT Request"')
        copy_request = relay.index('makeVisualButton("Copy Request JSON"')
        open_gpt = relay.index('makeVisualButton("Open GPT"')
        apply_response = relay.index('makeVisualButton("Apply GPT Response"')
        append_group = relay.index("relayActions.append(generateRelayButton, copyRelayButton, openGptButton, applyRelayButton);")
        self.assertLess(paste, action_group)
        self.assertLess(action_group, generate)
        self.assertLess(generate, copy_request)
        self.assertLess(copy_request, open_gpt)
        self.assertLess(open_gpt, apply_response)
        self.assertLess(apply_response, append_group)
        self.assertNotIn("relayFooter", relay)
        self.assertIn("flex-wrap: wrap;", STYLES[STYLES.index(".mvb-prompt-actions {"):STYLES.index("}", STYLES.index(".mvb-prompt-actions {"))])
        action_style = STYLES[STYLES.index(".mvb-prompt-relay-actions {"):STYLES.index(".mvb-prompt-relay-actions .mvb-button")]
        self.assertNotIn("margin-left", action_style)
        self.assertNotIn("justify-content: space-between", action_style)

    def test_instruction_uses_full_available_width_and_progressive_disclosures_remain(self):
        instruction_style = STYLES[STYLES.index(".mvb-prompt-instruction {"):STYLES.index("}", STYLES.index(".mvb-prompt-instruction {"))]
        self.assertNotIn("max-width", instruction_style)
        self.assertIn(
            'relayInstruction.textContent = "Generate and copy the scene request, open the Prompt Director, then paste and apply its response.";',
            EXTENSION,
        )
        for marker in (
            "if (promptFinalIsVisible(item, draft))",
            'mapping = document.createElement("details")',
            'relayRequestDisclosure = document.createElement("details")',
            'deterministic = document.createElement("details")',
        ):
            self.assertIn(marker, EXTENSION)

    def test_prompt_director_requests_make_lyric_and_json_ownership_explicit(self):
        for method in ("keyframe_i2v", "reference2video"):
            project = self._project(method)
            request = build_prompt_relay_request(project, project["scenes"][0]["scene_id"])
            constraints = request["constraints"]
            self.assertEqual(request["creative_context"]["lyric_context"], "No god in the house.")
            self.assertTrue(constraints["lyric_text_is_machine_owned"])
            self.assertTrue(constraints["lyric_context_is_read_only"])
            self.assertTrue(constraints["do_not_repeat_or_quote_lyric_text"])
            self.assertTrue(constraints["response_must_be_strict_json"])
            self.assertTrue(constraints["json_string_values_must_escape_embedded_double_quotes"])

    def test_valid_delivery_prose_keeps_exact_lyric_machine_owned_for_both_methods(self):
        description = "Controlled breath and restrained mouth movement carry the vocal intensity."
        for method in ("keyframe_i2v", "reference2video"):
            project = self._project(method)
            compiled = compile_scene_prompt(project, project["scenes"][0]["scene_id"])
            validated = validate_prompt_relay_response(compiled, self._response(compiled, description))
            self.assertIn(description, validated["enhanced_prompt"])
            self.assertEqual(validated["enhanced_prompt"].count("No god in the house."), 1)
            self.assertIn("<d>[English] No god in the house.</d>", validated["enhanced_prompt"])

    def test_exact_lyric_repetition_and_malformed_embedded_quotes_are_rejected(self):
        project = self._project("keyframe_i2v")
        compiled = compile_scene_prompt(project, project["scenes"][0]["scene_id"])
        with self.assertRaisesRegex(PromptCompilationError, "machine-owned lyric text"):
            validate_prompt_relay_response(
                compiled,
                self._response(compiled, 'He sings "No god in the house." with minimal head movement.'),
            )
        malformed = (
            '{"prompt_relay_response_version":1,'
            f'"scene_id":"{compiled["scene_id"]}",'
            f'"request_fingerprint":"{compiled["source_fingerprint"]}",'
            '"enhanced_description":"He sings "No god in the house." quietly."}'
        )
        with self.assertRaisesRegex(PromptCompilationError, "unescaped quotation marks"):
            validate_prompt_relay_response(compiled, malformed)
        self.assertIn("without Markdown or unescaped quotation marks", EXTENSION)
        self.assertNotIn("JSON5", EXTENSION)
        self.assertNotIn("eval(", EXTENSION)

    def test_incomplete_prompt_records_save_reopen_without_render_readiness_or_synthesis(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        record = project["prompts"]["scenes"][0]["reference2video"]
        self.assertEqual(record, {"final_prompt": "", "source_fingerprint": None, "relay_fingerprint": ""})
        saved = self.storage.save_project(project["project_id"], copy.deepcopy(project))
        reopened = self.storage.load_project(project["project_id"])
        self.assertEqual(reopened["prompts"], saved["prompts"])
        self.assertEqual(reopened["prompts"]["scenes"][0]["reference2video"], record)
        state = prompt_scene_state(reopened, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "needs_gpt")
        self.assertFalse(state["ready_for_render_prompt"])
        self.assertEqual(state["saved_final_prompt"], "")

    def test_multiple_incomplete_scenes_are_valid_durable_project_state(self):
        project = self.storage.create_project("Multiple incomplete scenes")
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "First."},
                {"cue_number": 2, "start_ms": 4_000, "end_ms": 8_000, "text": "Second."},
            ],
            8_000,
        )
        candidate = {
            **project,
            "source": {
                "master_audio": {
                    "stored_name": "master_audio.wav",
                    "original_name": "master.wav",
                    "duration_ms": 8_000,
                },
                "lyrics_srt": {
                    "stored_name": "lyrics.srt",
                    "original_name": "lyrics.srt",
                    "cue_count": 2,
                },
            },
            "scenes": scenes,
            "visuals": default_visuals_for_scenes(scenes),
            "prompts": default_prompts_for_scenes(scenes),
        }
        saved = self.storage.save_project(project["project_id"], candidate, allow_visuals_change=True)
        reopened = self.storage.load_project(project["project_id"])
        self.assertEqual(len(reopened["prompts"]["scenes"]), 2)
        self.assertEqual(reopened["prompts"], saved["prompts"])
        self.assertTrue(all(
            method["final_prompt"] == "" and method["relay_fingerprint"] == ""
            for prompt_scene in reopened["prompts"]["scenes"]
            for method in (prompt_scene["keyframe_i2v"], prompt_scene["reference2video"])
        ))

    def test_explicit_prompt_save_stays_strict_while_project_save_accepts_incomplete_state(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        with self.assertRaisesRegex(PromptCompilationError, "Final prompt cannot be empty"):
            save_scene_prompt(
                self.storage,
                project["project_id"],
                scene_id,
                {
                    "generation_method": compiled["generation_method"],
                    "final_prompt": "",
                    "source_fingerprint": compiled["source_fingerprint"],
                    "relay_fingerprint": compiled["source_fingerprint"],
                },
            )
        self.assertEqual(self.storage.save_project(project["project_id"], project)["project_id"], project["project_id"])

    def test_character_and_location_edits_make_current_prompt_stale_without_losing_text(self):
        for entity_field, detail_field, replacement in (
            ("characters", "appearance", "Silver hair and a sharper silhouette."),
            ("locations", "description", "Flooded stone under severe amber light."),
        ):
            project = self._save_current_prompt(self._project())
            scene_id = project["scenes"][0]["scene_id"]
            original_prompt = project["prompts"]["scenes"][0]["reference2video"]["final_prompt"]
            old_compiled = compile_scene_prompt(project, scene_id)
            old_response = self._response(old_compiled, "A measured camera drift follows the performance.")
            candidate = copy.deepcopy(project)
            candidate[entity_field][0][detail_field] = replacement
            changed = self.storage.save_project(project["project_id"], candidate)
            stale = prompt_scene_state(changed, scene_id, storage=self.storage)
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(stale["saved_final_prompt"], original_prompt)
            self.assertNotEqual(stale["source_fingerprint"], old_compiled["source_fingerprint"])
            with self.assertRaisesRegex(PromptConflictError, "stale"):
                validate_prompt_relay_response(compile_scene_prompt(changed, scene_id), old_response)
            reopened = self.storage.load_project(changed["project_id"])
            self.assertEqual(
                reopened["prompts"]["scenes"][0]["reference2video"]["final_prompt"],
                original_prompt,
            )

    def test_visual_edit_makes_current_prompt_stale_and_project_remains_saveable(self):
        project = self._save_current_prompt(self._project("keyframe_i2v"))
        scene_id = project["scenes"][0]["scene_id"]
        original_prompt = project["prompts"]["scenes"][0]["keyframe_i2v"]["final_prompt"]
        visuals = copy.deepcopy(project["visuals"])
        visuals["scenes"][0]["keyframe_i2v"]["actual_keyframe_description"] = "Vesper now faces the chapel window."
        changed = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": visuals},
            allow_visuals_change=True,
        )
        self.assertEqual(prompt_scene_state(changed, scene_id, storage=self.storage)["status"], "stale")
        self.assertEqual(changed["prompts"]["scenes"][0]["keyframe_i2v"]["final_prompt"], original_prompt)
        saved_again = self.storage.save_project(changed["project_id"], changed)
        self.assertEqual(saved_again["prompts"]["scenes"][0]["keyframe_i2v"]["final_prompt"], original_prompt)

    def test_project_mutation_flush_is_separate_from_prompt_transition_flush(self):
        project_flush = self._function_body(
            EXTENSION,
            "async function flushCurrentProject(root)",
            "async function flushProjectTransition(root)",
        )
        transition_flush = self._function_body(
            EXTENSION,
            "async function flushProjectTransition(root)",
            "async function checkBackend(root)",
        )
        self.assertNotIn("flushPromptDrafts", project_flush)
        self.assertIn("flushCurrentProject(root)", transition_flush)
        self.assertIn("flushPromptDrafts(root)", transition_flush)
        visual_mutation = self._function_body(
            EXTENSION,
            "async function runVisualMutation(root, operation, sceneId, task, failureMessage, options = {})",
            "async function saveVisualDetails",
        )
        resource_mutation = self._function_body(
            EXTENSION,
            "async function runResourceMutation(root, operation, task, failureMessage)",
            "async function saveCharacter(root)",
        )
        self.assertIn("flushCurrentProject(root)", visual_mutation)
        self.assertIn("flushCurrentProject(root)", resource_mutation)
        self.assertNotIn("flushProjectTransition", visual_mutation)
        self.assertNotIn("flushProjectTransition", resource_mutation)
        close_builder = self._function_body(EXTENSION, "async function closeBuilder(root)", "function saveStateLabel")
        open_project = self._function_body(EXTENSION, "async function openProject(root, projectId)", "function uploadFormData")
        self.assertIn("flushProjectTransition(root)", close_builder)
        self.assertIn("flushProjectTransition(root)", open_project)


if __name__ == "__main__":
    unittest.main()
