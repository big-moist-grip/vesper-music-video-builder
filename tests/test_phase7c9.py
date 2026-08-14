import base64
import copy
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import ProjectStorage, default_prompts_for_scenes
from backend.prompt_service import (
    PromptCompilationError,
    PromptConflictError,
    build_prompt_list,
    build_prompt_relay_request,
    build_prompt_source_fingerprint,
    compile_scene_prompt,
    derive_prompt_status,
    prompt_scene_state,
    save_scene_prompt,
    validate_final_prompt,
    validate_prompt_relay_response,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
PROMPT_STATE = (ROOT / "web" / "prompt_state.js").read_bytes()
PROMPT_SERVICE = (ROOT / "backend" / "prompt_service.py").read_text(encoding="utf-8")

I2VA_FORBIDDEN_MARKERS = (
    "One continuous shot begins from the accepted first frame",
    "Opening state is defined exclusively by the accepted keyframe",
    "The target duration is exactly",
    "The accepted keyframe is <Picture 1>",
    "Actual accepted-keyframe description:",
    "Continuity context for",
    "Forward-generation intent",
    "Treat the supplied lyric as audio performance context",
    "machine-mapped ownership",
    "opening_state_rule",
    "source fingerprint",
)


class Phase7C9TestCase(unittest.TestCase):
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

    def _project(self, method="keyframe_i2v", scene_count=1):
        project = self.storage.create_project("Phase 7C.9")
        cues = [
            {"cue_number": index + 1, "start_ms": index * 4_000, "end_ms": (index + 1) * 4_000, "text": "No god in the house."}
            for index in range(scene_count)
        ]
        scenes = build_scenes(cues, scene_count * 4_000)
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
        echo_reference = self._reference("echo.png")
        if scene_count > 1:
            characters.append({
                "character_id": str(uuid.uuid4()),
                "name": "Echo",
                "role": "band_member",
                "appearance": "A hooded silhouette.",
                "outfit": "Grey robes.",
                "references": [echo_reference],
            })
        locations = [{
            "location_id": location_id,
            "name": "Chapel",
            "description": "Cold stone walls, reflective water, and hard white side light.",
            "references": [location_reference],
        }]
        visuals = default_visuals_for_scenes(scenes)
        for index, visual in enumerate(visuals["scenes"]):
            visual["generation_method"] = method
            if index == 0:
                scene_character_selector = {
                    "entity_type": "character",
                    "entity_id": character_id,
                    "reference_id": character_reference["reference_id"],
                }
            else:
                scene_character_selector = {
                    "entity_type": "character",
                    "entity_id": characters[1]["character_id"],
                    "reference_id": echo_reference["reference_id"],
                }
            visual["reference2video"]["selected_references"] = [
                scene_character_selector,
                {
                    "entity_type": "location",
                    "entity_id": location_id,
                    "reference_id": location_reference["reference_id"],
                },
            ]
        if method == "keyframe_i2v":
            for visual in visuals["scenes"]:
                asset_id = str(uuid.uuid4())
                stored_name = f"{asset_id}.png"
                (self.storage.project_directory(project["project_id"]) / "keyframes" / stored_name).write_bytes(b"keyframe")
                visual["keyframe_i2v"].update({
                    "keyframe_generation_prompt": "A restrained cinematic still.",
                    "intended_keyframe_description": "A discarded intended opening.",
                    "actual_keyframe_description": "Vesper stands beside the flooded chapel wall in a black jacket.",
                    "accepted_keyframe": {
                        "asset_id": asset_id,
                        "stored_name": stored_name,
                        "original_name": "accepted.png",
                    },
                })
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "source": {
                    "master_audio": {
                        "stored_name": "master_audio.wav",
                        "original_name": "master.wav",
                        "duration_ms": scene_count * 4_000,
                    },
                    "lyrics_srt": {
                        "stored_name": "lyrics.srt",
                        "original_name": "lyrics.srt",
                        "cue_count": scene_count,
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
        storyboard_scenes = [
            {
                "scene_id": scene["scene_id"],
                "scene_type": "performance",
                "character_ids": [character_id] if index == 0 else [characters[1]["character_id"]],
                "location_id": location_id,
                "action": "Vesper performs with controlled intensity.",
                "visual_instructions": "Cold gothic performance staging.",
                "camera_direction": "A slow lateral camera drift.",
                "motion_direction": "Restrained forward movement.",
                "continuity_notes": "Keep Vesper and the chapel consistent.",
                "required_references": [],
            }
            for index, scene in enumerate(scenes)
        ]
        storyboard = validate_storyboard_response(project, {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": storyboard_scenes,
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

    def _save_current_prompt(self, project, scene_index=0, description=None):
        scene_id = project["scenes"][scene_index]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        validated = validate_prompt_relay_response(
            compiled,
            self._response(
                compiled,
                description
                or "Controlled breath, restrained mouth movement, and tightening through the jaw carry the performance.",
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

    # ------------------------------------------------------------------
    # I2VA cleanup (round items 1-14)
    # ------------------------------------------------------------------

    def test_i2va_prompts_contain_no_internal_compiler_boilerplate(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        deterministic = compile_scene_prompt(project, scene_id)["deterministic_prompt"]
        compiled = compile_scene_prompt(project, scene_id)
        validated = validate_prompt_relay_response(
            compiled,
            self._response(compiled, "A restrained push toward the performer holds the frame without a cut."),
        )
        for prompt in (deterministic, validated["enhanced_prompt"]):
            for marker in I2VA_FORBIDDEN_MARKERS:
                with self.subTest(marker=marker, kind="deterministic" if prompt == deterministic else "enhanced"):
                    self.assertNotIn(marker, prompt)

    def test_i2va_keeps_official_opening_exact_blank_line_and_field_order(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        validated = validate_prompt_relay_response(
            compiled,
            self._response(compiled, "A restrained push toward the performer holds the frame without a cut."),
        )
        for prompt in (compiled["deterministic_prompt"], validated["enhanced_prompt"]):
            opening = (
                "For the target video, at 0.00 seconds into the target video, <Picture 1> "
                "(from [Shot 1]) is fully referenced."
            )
            self.assertTrue(prompt.startswith(opening + "\n\nintegrated_multimodal_description: [Shot 1] "))
            self.assertNotIn(opening + "\n\n\n", prompt)
            self.assertLess(prompt.index("integrated_multimodal_description:"), prompt.index("overall_soundscape:"))
            self.assertLess(prompt.index("overall_soundscape:"), prompt.index("non_diegetic_music:"))

    def test_i2va_natural_opening_facts_machine_lyric_and_gpt_prose(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        enhanced = "He subtly firms his shoulders and raises his chin while the camera pushes in without a cut."
        validated = validate_prompt_relay_response(compiled, self._response(compiled, enhanced))
        prompt = validated["enhanced_prompt"]
        self.assertIn(
            "The accepted first frame establishes the opening: "
            "Vesper stands beside the flooded chapel wall in a black jacket.",
            prompt,
        )
        self.assertIn("Vesper remains consistent with the opening image", prompt)
        self.assertIn("The Chapel environment remains consistent with the opening image", prompt)
        self.assertEqual(prompt.count("No god in the house."), 1)
        self.assertIn("Vesper (S1) sings: <d>[English] No god in the house.</d>", prompt)
        self.assertIn(enhanced, prompt)
        integrated = prompt.split("integrated_multimodal_description: ", 1)[1].split("\n\noverall_soundscape:", 1)[0]
        self.assertEqual(integrated.count("[Shot 1]"), 1)
        self.assertEqual(prompt.count("[Shot 1]"), 2)

    def test_i2va_canonical_validator_roundtrip_and_boilerplate_rejection(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        compiled = compile_scene_prompt(project, scene_id)
        self.assertEqual(validate_final_prompt(compiled, compiled["deterministic_prompt"]), compiled["deterministic_prompt"])
        validated = validate_prompt_relay_response(
            compiled,
            self._response(compiled, "A restrained push toward the performer holds the frame without a cut."),
        )
        self.assertEqual(validate_final_prompt(compiled, validated["enhanced_prompt"]), validated["enhanced_prompt"])
        with self.assertRaises(PromptCompilationError):
            validate_final_prompt(
                compiled,
                validated["enhanced_prompt"] + "\nOpening state is defined exclusively by the accepted keyframe and its actual description.",
            )
        stripped = validated["enhanced_prompt"].replace(
            "The accepted first frame establishes the opening: ",
            "The visible opening is: ",
        )
        with self.assertRaises(PromptCompilationError):
            validate_final_prompt(compiled, stripped)

    # ------------------------------------------------------------------
    # Unified freshness (round items 15-27)
    # ------------------------------------------------------------------

    def test_one_authoritative_fingerprint_oracle_feeds_every_consumer(self):
        self.assertEqual(PROMPT_SERVICE.count("def _prompt_source_basis("), 1)
        self.assertEqual(PROMPT_SERVICE.count("def build_prompt_source_fingerprint("), 1)
        basis_call = "build_prompt_source_fingerprint(project, scene[\"scene_id\"], method)"
        self.assertIn(basis_call, PROMPT_SERVICE)
        # status, relay request, relay response validation, and save all derive
        # from the same compiled source fingerprint.
        self.assertIn('"source_fingerprint": build_prompt_source_fingerprint(project, scene["scene_id"], method)', PROMPT_SERVICE)
        self.assertIn('"request_fingerprint": result["source_fingerprint"]', PROMPT_SERVICE)
        self.assertIn('if document["request_fingerprint"] != result["source_fingerprint"]:', PROMPT_SERVICE)
        self.assertIn('if client_fingerprint != result["source_fingerprint"]:', PROMPT_SERVICE)
        # The frontend derives badge, request freshness, save eligibility, and
        # render readiness from the same item.source_fingerprint authority.
        self.assertIn("draft.sourceFingerprint !== item.source_fingerprint", PROMPT_STATE.decode("utf-8"))
        self.assertIn("relay.request.request_fingerprint === item.source_fingerprint", PROMPT_STATE.decode("utf-8"))
        self.assertIn("draft.relayFingerprint === item.source_fingerprint", PROMPT_STATE.decode("utf-8"))

    def test_status_is_derived_from_one_validity_pair(self):
        self.assertEqual(derive_prompt_status("", "current", "current"), "needs_gpt")
        self.assertEqual(derive_prompt_status("prompt", "current", "current"), "current")
        self.assertEqual(derive_prompt_status("prompt", "stale", "current"), "stale")
        self.assertEqual(derive_prompt_status("prompt", "missing", "current"), "stale")
        self.assertEqual(derive_prompt_status("prompt", "current", "missing"), "needs_gpt")
        self.assertEqual(derive_prompt_status("prompt", "current", "stale"), "needs_gpt")
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        state = prompt_scene_state(project, scene_id, storage=self.storage)
        self.assertEqual(state["source_validity"], "missing")
        self.assertEqual(state["relay_validity"], "missing")
        self.assertEqual(state["status"], "needs_gpt")

    def test_current_ref2va_character_edit_becomes_stale(self):
        project = self._save_current_prompt(self._project("reference2video"))
        scene_id = project["scenes"][0]["scene_id"]
        original = project["prompts"]["scenes"][0]["reference2video"]["final_prompt"]
        candidate = copy.deepcopy(project)
        candidate["characters"][0]["appearance"] = "Silver hair and a sharper silhouette."
        changed = self.storage.save_project(project["project_id"], candidate)
        state = prompt_scene_state(changed, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["source_validity"], "stale")
        self.assertEqual(state["saved_final_prompt"], original)
        self.assertFalse(state["ready_for_render_prompt"])

    def test_current_i2v_character_edit_becomes_stale(self):
        project = self._save_current_prompt(self._project("keyframe_i2v"))
        scene_id = project["scenes"][0]["scene_id"]
        candidate = copy.deepcopy(project)
        candidate["characters"][0]["outfit"] = "A white suit with antique-gold trim."
        changed = self.storage.save_project(project["project_id"], candidate)
        state = prompt_scene_state(changed, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "stale")
        self.assertNotEqual(
            state["source_fingerprint"],
            build_prompt_source_fingerprint(project, scene_id, "keyframe_i2v"),
        )

    def test_location_edit_stales_affected_scene(self):
        project = self._save_current_prompt(self._project("reference2video"))
        scene_id = project["scenes"][0]["scene_id"]
        candidate = copy.deepcopy(project)
        candidate["locations"][0]["description"] = "Flooded stone under severe amber light."
        changed = self.storage.save_project(project["project_id"], candidate)
        self.assertEqual(prompt_scene_state(changed, scene_id, storage=self.storage)["status"], "stale")

    def test_unrelated_entity_edit_leaves_unaffected_scene_current(self):
        project = self._project("reference2video", scene_count=2)
        scene_one = project["scenes"][0]["scene_id"]
        scene_two = project["scenes"][1]["scene_id"]
        project = self._save_current_prompt(project, scene_index=0)
        project = self._save_current_prompt(project, scene_index=1)
        self.assertEqual(prompt_scene_state(project, scene_one, storage=self.storage)["status"], "current")
        self.assertEqual(prompt_scene_state(project, scene_two, storage=self.storage)["status"], "current")
        # Editing Vesper stales only the scene that uses Vesper.
        vesper_edit = copy.deepcopy(project)
        vesper_edit["characters"][0]["appearance"] = "Silver hair and a sharper silhouette."
        changed = self.storage.save_project(project["project_id"], vesper_edit)
        self.assertEqual(prompt_scene_state(changed, scene_one, storage=self.storage)["status"], "stale")
        self.assertEqual(prompt_scene_state(changed, scene_two, storage=self.storage)["status"], "current")
        # Adding an unused entity stales no scene.
        unrelated = copy.deepcopy(project)
        unrelated["characters"].append({
            "character_id": str(uuid.uuid4()),
            "name": "Wren",
            "role": "extra",
            "appearance": "A hooded figure.",
            "outfit": "Grey robes.",
            "references": [],
        })
        unchanged = self.storage.save_project(project["project_id"], unrelated)
        self.assertEqual(prompt_scene_state(unchanged, scene_one, storage=self.storage)["status"], "current")
        self.assertEqual(prompt_scene_state(unchanged, scene_two, storage=self.storage)["status"], "current")

    def test_selected_reference_change_stales_ref2va_scene(self):
        project = self._save_current_prompt(self._project("reference2video"))
        scene_id = project["scenes"][0]["scene_id"]
        candidate = copy.deepcopy(project)
        selection = candidate["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        candidate["visuals"]["scenes"][0]["reference2video"]["selected_references"] = selection[:1]
        changed = self.storage.save_project(project["project_id"], candidate, allow_visuals_change=True)
        self.assertEqual(prompt_scene_state(changed, scene_id, storage=self.storage)["status"], "stale")

    def test_actual_keyframe_description_change_stales_i2v_scene(self):
        project = self._save_current_prompt(self._project("keyframe_i2v"))
        scene_id = project["scenes"][0]["scene_id"]
        candidate = copy.deepcopy(project)
        candidate["visuals"]["scenes"][0]["keyframe_i2v"]["actual_keyframe_description"] = (
            "Vesper now faces the chapel window."
        )
        changed = self.storage.save_project(project["project_id"], candidate, allow_visuals_change=True)
        self.assertEqual(prompt_scene_state(changed, scene_id, storage=self.storage)["status"], "stale")
        intended_only = copy.deepcopy(project)
        intended_only["visuals"]["scenes"][0]["keyframe_i2v"]["intended_keyframe_description"] = (
            "A non-authoritative rewrite."
        )
        unchanged = self.storage.save_project(project["project_id"], intended_only, allow_visuals_change=True)
        self.assertEqual(prompt_scene_state(unchanged, scene_id, storage=self.storage)["status"], "current")

    def test_action_camera_and_motion_changes_stale_scene(self):
        for field in ("action", "camera_direction", "motion_direction", "continuity_notes"):
            with self.subTest(field=field):
                project = self._save_current_prompt(self._project("reference2video"))
                scene_id = project["scenes"][0]["scene_id"]
                candidate = copy.deepcopy(project)
                candidate["storyboard"]["scenes"][0][field] = "A completely different direction."
                changed = self.storage.save_project(
                    project["project_id"],
                    candidate,
                    allow_storyboard_change=True,
                )
                self.assertEqual(prompt_scene_state(changed, scene_id, storage=self.storage)["status"], "stale")

    def test_old_request_becomes_stale_and_fresh_fingerprint_differs(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        old_request = build_prompt_relay_request(project, scene_id)
        candidate = copy.deepcopy(project)
        candidate["characters"][0]["appearance"] = "Changed appearance facts."
        changed = self.storage.save_project(project["project_id"], candidate)
        new_request = build_prompt_relay_request(changed, scene_id)
        self.assertNotEqual(old_request["request_fingerprint"], new_request["request_fingerprint"])
        self.assertEqual(
            new_request["request_fingerprint"],
            build_prompt_source_fingerprint(changed, scene_id, "keyframe_i2v"),
        )
        self._run_prompt_state(f"""
const staleItem = {{scene_id: {json.dumps(scene_id)}, generation_method: "keyframe_i2v", source_fingerprint: {json.dumps(new_request["request_fingerprint"])}}};
const staleRelay = {{request: {json.dumps(old_request)}, responseText: "{{}}", responseMessage: "", responseMessageState: "ready"}};
if (state.promptRelayRequestIsCurrent(staleRelay, staleItem)) throw new Error("old generated request must be stale");
if (state.canCopyPromptRelayRequest(staleRelay, staleItem)) throw new Error("stale request must not copy");
if (state.canApplyPromptRelayResponse(staleRelay, staleItem)) throw new Error("stale request must block apply");
""")

    def test_old_gpt_response_is_rejected_after_upstream_change(self):
        project = self._save_current_prompt(self._project("keyframe_i2v"))
        scene_id = project["scenes"][0]["scene_id"]
        old_compiled = compile_scene_prompt(project, scene_id)
        old_response = self._response(old_compiled, "A measured camera drift follows the performance.")
        candidate = copy.deepcopy(project)
        candidate["characters"][0]["appearance"] = "Changed appearance facts."
        changed = self.storage.save_project(project["project_id"], candidate)
        with self.assertRaisesRegex(PromptConflictError, "stale"):
            validate_prompt_relay_response(compile_scene_prompt(changed, scene_id), old_response)

    def test_save_project_never_stamps_prompt_provenance(self):
        project = self._save_current_prompt(self._project("reference2video"))
        scene_id = project["scenes"][0]["scene_id"]
        record_before = copy.deepcopy(project["prompts"]["scenes"][0]["reference2video"])
        candidate = copy.deepcopy(project)
        candidate["characters"][0]["appearance"] = "Changed hair."
        changed = self.storage.save_project(project["project_id"], candidate)
        resaved = self.storage.save_project(changed["project_id"], copy.deepcopy(changed))
        self.assertEqual(resaved["prompts"]["scenes"][0]["reference2video"], record_before)
        self.assertEqual(prompt_scene_state(resaved, scene_id, storage=self.storage)["status"], "stale")
        # A client document that attempts to stamp fresh provenance through an
        # ordinary project save must be ignored.
        hostile = copy.deepcopy(resaved)
        hostile["prompts"]["scenes"][0]["reference2video"] = {
            "final_prompt": record_before["final_prompt"],
            "source_fingerprint": prompt_scene_state(resaved, scene_id, storage=self.storage)["source_fingerprint"],
            "relay_fingerprint": prompt_scene_state(resaved, scene_id, storage=self.storage)["source_fingerprint"],
        }
        saved = self.storage.save_project(resaved["project_id"], hostile)
        self.assertEqual(saved["prompts"]["scenes"][0]["reference2video"], record_before)
        self.assertEqual(prompt_scene_state(saved, scene_id, storage=self.storage)["status"], "stale")

    def test_save_prompt_is_the_path_that_stamps_current_relay_provenance(self):
        project = self._project("reference2video")
        scene_id = project["scenes"][0]["scene_id"]
        project = self._save_current_prompt(project)
        record = project["prompts"]["scenes"][0]["reference2video"]
        current = build_prompt_source_fingerprint(project, scene_id, "reference2video")
        self.assertEqual(record["source_fingerprint"], current)
        self.assertEqual(record["relay_fingerprint"], current)
        state = prompt_scene_state(project, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "current")
        self.assertEqual(state["source_validity"], "current")
        self.assertEqual(state["relay_validity"], "current")

    # ------------------------------------------------------------------
    # Status model (round items 28-36)
    # ------------------------------------------------------------------

    def test_status_precedence_stale_overrides_unsaved_and_draft_is_retained(self):
        self._run_prompt_state("""
const currentFingerprint = "a".repeat(64);
const staleFingerprint = "b".repeat(64);
const saved = {
    status: "stale",
    source_fingerprint: currentFingerprint,
    saved_source_fingerprint: staleFingerprint,
    saved_relay_fingerprint: staleFingerprint,
    saved_final_prompt: "saved prompt",
    ready_for_render_prompt: false,
};
const staleUnsavedDraft = {text: "draft edit", sourceFingerprint: staleFingerprint, relayFingerprint: staleFingerprint, dirty: true};
if (state.promptDraftStatus(saved, staleUnsavedDraft) !== "stale") throw new Error("stale must override unsaved");
if (state.canSavePromptDraft(saved, staleUnsavedDraft)) throw new Error("stale draft save must be disabled");
if (!state.promptDraftIsStale(saved, staleUnsavedDraft)) throw new Error("draft provenance must be stale");
const freshRelayDraft = {text: "fresh relay prompt", sourceFingerprint: currentFingerprint, relayFingerprint: currentFingerprint, dirty: true};
if (state.promptDraftStatus(saved, freshRelayDraft) !== "unsaved") throw new Error("fresh relay draft must be unsaved");
if (!state.canSavePromptDraft(saved, freshRelayDraft)) throw new Error("fresh relay draft must save");
const current = {...saved, status: "current", saved_source_fingerprint: currentFingerprint, saved_relay_fingerprint: currentFingerprint, ready_for_render_prompt: true};
const cleanCurrent = {text: "saved prompt", sourceFingerprint: currentFingerprint, relayFingerprint: currentFingerprint, dirty: false};
if (state.promptDraftStatus(current, cleanCurrent) !== "current") throw new Error("clean current must stay current");
const needsGpt = {...saved, status: "needs_gpt", saved_final_prompt: "", saved_source_fingerprint: null, saved_relay_fingerprint: ""};
if (state.promptDraftStatus(needsGpt, {dirty: false}) !== "needs_gpt") throw new Error("missing prompt must need GPT");
""")

    def test_stale_unsaved_draft_text_is_retained_non_destructively(self):
        project = self._save_current_prompt(self._project("reference2video"))
        scene_id = project["scenes"][0]["scene_id"]
        original = project["prompts"]["scenes"][0]["reference2video"]["final_prompt"]
        candidate = copy.deepcopy(project)
        candidate["locations"][0]["description"] = "A flooded ruin under amber light."
        changed = self.storage.save_project(project["project_id"], candidate)
        reopened = self.storage.load_project(changed["project_id"])
        state = prompt_scene_state(reopened, scene_id, storage=self.storage)
        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["saved_final_prompt"], original)
        with self.assertRaises(PromptConflictError):
            save_scene_prompt(
                self.storage,
                reopened["project_id"],
                scene_id,
                {
                    "generation_method": "reference2video",
                    "final_prompt": original,
                    "source_fingerprint": state["saved_source_fingerprint"],
                    "relay_fingerprint": state["saved_relay_fingerprint"],
                },
            )

    def test_inline_stale_notice_and_badge_share_one_authority(self):
        update_controls = EXTENSION[
            EXTENSION.index("function updatePromptRelayControls(root, sceneId, generationMethod)"):
            EXTENSION.index("function promptCardFor(root, sceneId)")
        ]
        self.assertIn("promptRelayIsCurrent(relay, item)", update_controls)
        self.assertIn('relay.request && !requestCurrent', update_controls)
        self.assertIn('"Request is stale — generate a new request."', update_controls)
        render = EXTENSION[
            EXTENSION.index("function renderPromptState(root, options = {})"):
            EXTENSION.index("async function loadPromptCards(root, silent = false, options = {})")
        ]
        self.assertIn("promptDraftStatus(item, draft)", render)
        narrow = EXTENSION[
            EXTENSION.index("function updatePromptCardInPlace(root, card, item, blocked)"):
            EXTENSION.index("function updatePromptCardsInPlace(root, promptViewport)")
        ]
        self.assertIn("promptDraftStatus(item, draft)", narrow)
        self.assertIn("updatePromptRelayControls(root, item.scene_id, item.generation_method)", narrow)

    def test_collapsed_badge_and_expanded_card_use_same_state(self):
        self.assertIn('badge.dataset.mvbPromptBadge = "";', EXTENSION)
        narrow = EXTENSION[
            EXTENSION.index("function updatePromptCardInPlace(root, card, item, blocked)"):
            EXTENSION.index("function updatePromptCardsInPlace(root, promptViewport)")
        ]
        self.assertIn('card.querySelector("[data-mvb-prompt-badge]")', narrow)
        self.assertIn("card.dataset.mvbPromptReadyForRender = String(promptDraftReadyForRender(item, draft));", narrow)

    # ------------------------------------------------------------------
    # Persistence (round items 37-44)
    # ------------------------------------------------------------------

    def test_needs_gpt_and_stale_projects_save_and_reopen(self):
        needs_gpt = self._project("reference2video")
        saved = self.storage.save_project(needs_gpt["project_id"], copy.deepcopy(needs_gpt))
        reopened = self.storage.load_project(saved["project_id"])
        self.assertEqual(reopened["prompts"], saved["prompts"])
        self.assertEqual(
            prompt_scene_state(reopened, reopened["scenes"][0]["scene_id"], storage=self.storage)["status"],
            "needs_gpt",
        )

        stale = self._save_current_prompt(self._project("reference2video"))
        candidate = copy.deepcopy(stale)
        candidate["characters"][0]["outfit"] = "A changed outfit."
        stale_changed = self.storage.save_project(stale["project_id"], candidate)
        stale_saved = self.storage.save_project(stale_changed["project_id"], copy.deepcopy(stale_changed))
        stale_reopened = self.storage.load_project(stale_saved["project_id"])
        self.assertEqual(
            prompt_scene_state(stale_reopened, stale_reopened["scenes"][0]["scene_id"], storage=self.storage)["status"],
            "stale",
        )
        self.assertEqual(
            prompt_scene_state(stale_reopened, stale_reopened["scenes"][0]["scene_id"], storage=self.storage)["saved_final_prompt"],
            stale["prompts"]["scenes"][0]["reference2video"]["final_prompt"],
        )

    def test_incomplete_multiscene_project_durability_and_upstream_resave(self):
        project = self._project("reference2video", scene_count=3)
        project = self._save_current_prompt(project, scene_index=0)
        incomplete = copy.deepcopy(project)
        incomplete["characters"][0]["appearance"] = "Changed between saves."
        first = self.storage.save_project(project["project_id"], incomplete)
        second = self.storage.save_project(first["project_id"], copy.deepcopy(first))
        reopened = self.storage.load_project(second["project_id"])
        self.assertEqual(len(reopened["scenes"]), 3)
        self.assertEqual(reopened["prompts"], second["prompts"])
        statuses = [
            prompt_scene_state(reopened, scene["scene_id"], storage=self.storage)["status"]
            for scene in reopened["scenes"]
        ]
        self.assertEqual(statuses[0], "stale")
        self.assertEqual(statuses[1], "needs_gpt")
        self.assertEqual(statuses[2], "needs_gpt")

    def test_save_project_is_independent_of_render_readiness(self):
        project = self._project("keyframe_i2v")
        scene_id = project["scenes"][0]["scene_id"]
        listing = build_prompt_list(project, self.storage)
        self.assertFalse(listing["scenes"][0]["ready_for_render_prompt"])
        saved = self.storage.save_project(project["project_id"], copy.deepcopy(project))
        self.assertEqual(saved["project_id"], project["project_id"])
        self.assertFalse(
            prompt_scene_state(saved, scene_id, storage=self.storage)["ready_for_render_prompt"],
        )

    # ------------------------------------------------------------------
    # Flicker / DOM stability (round items 45-53)
    # ------------------------------------------------------------------

    def test_generate_apply_save_keep_prompts_stage_out_of_full_rebuilds(self):
        render_start = EXTENSION.index("function renderPromptState(root, options = {})")
        render = EXTENSION[
            render_start:
            EXTENSION.index("list.replaceChildren();", render_start)
        ]
        self.assertIn('builderState.promptListState === "ready" && promptCardsMatchStructure(root)', render)
        self.assertIn("updatePromptCardsInPlace(root, promptViewport);", render)
        self.assertIn('builderState.promptListState === "loading" && promptCardsMatchStructure(root)', render)
        generate = EXTENSION[
            EXTENSION.index("async function generatePromptRelayRequest(root, sceneId, generationMethod)"):
            EXTENSION.index("async function copyPromptRelayRequest(root, sceneId, generationMethod)")
        ]
        apply_handler = EXTENSION[
            EXTENSION.index("async function applyPromptRelayResponse(root, sceneId, generationMethod)"):
            EXTENSION.index("async function savePrompt(root, sceneId, generationMethod)")
        ]
        save_handler = EXTENSION[
            EXTENSION.index("async function savePrompt(root, sceneId, generationMethod)"):
            EXTENSION.index("function closeResourceDialogs(root)")
        ]
        for handler in (generate, apply_handler, save_handler):
            self.assertNotIn("replaceChildren", handler)
        self.assertNotIn("setCurrentProject(root, project", apply_handler)

    def test_silent_refresh_keeps_visible_cards_and_reconciles_items_in_place(self):
        load = EXTENSION[
            EXTENSION.index("async function loadPromptCards(root, silent = false, options = {})"):
            EXTENSION.index("async function copyPrompt(root, sceneId, generationMethod)")
        ]
        self.assertIn("const previousItems = new Map(", load)
        self.assertIn("Object.assign(previous, entry);", load)
        self.assertIn("builderState.promptDrafts[key].text = item.saved_final_prompt || \"\";", load)
        set_current = EXTENSION[
            EXTENSION.index("function setCurrentProject(root, project, options = {})"):
            EXTENSION.index("function cancelAutosave(root)")
        ]
        self.assertIn("builderState.promptCards = sameProject ? builderState.promptCards : [];", set_current)

    def test_status_refresh_updates_cards_without_recreating_unrelated_nodes(self):
        narrow = EXTENSION[
            EXTENSION.index("function updatePromptCardsInPlace(root, promptViewport)"):
            EXTENSION.index("function renderPromptState(root, options = {})")
        ]
        self.assertNotIn("replaceChildren", narrow)
        self.assertNotIn(".open =", narrow)
        self.assertIn("restorePromptViewport(root, promptViewport);", narrow)
        card_update = EXTENSION[
            EXTENSION.index("function updatePromptCardInPlace(root, card, item, blocked)"):
            EXTENSION.index("function updatePromptCardsInPlace(root, promptViewport)")
        ]
        self.assertIn("updatePromptRelayControls(root, item.scene_id, item.generation_method)", card_update)
        self.assertIn("updatePromptFinalSection(root, card, item, draft, blocked)", card_update)
        self.assertIn("updatePromptContextInPlace(card, item)", card_update)

    def test_active_textarea_focus_and_caret_are_preserved_on_narrow_updates(self):
        final_update = EXTENSION[
            EXTENSION.index("function updatePromptFinalSection(root, card, item, draft, blocked)"):
            EXTENSION.index("function updatePromptContextInPlace(card, item)")
        ]
        self.assertIn("document.activeElement !== editor && editor.value !== draft.text", final_update)
        relay_controls = EXTENSION[
            EXTENSION.index("function updatePromptRelayControls(root, sceneId, generationMethod)"):
            EXTENSION.index("function promptCardFor(root, sceneId)")
        ]
        self.assertIn("document.activeElement !== requestPreview", relay_controls)
        self.assertIn("document.activeElement !== responseInput", relay_controls)
        self.assertIn("function capturePromptViewport(root, anchorSceneId = null)", EXTENSION)
        self.assertIn("function restorePromptViewport(root, viewport)", EXTENSION)
        self.assertIn("selectionStart: focusedEditor.selectionStart", EXTENSION)

    def test_final_prompt_reveal_is_atomic_and_save_reconciles_authority(self):
        builder = EXTENSION[
            EXTENSION.index("function buildPromptFinalSection(root, card, item, draft, blocked)"):
            EXTENSION.index("function updatePromptFinalSection(root, card, item, draft, blocked)")
        ]
        self.assertIn('finalSection.dataset.mvbPromptFinal = "";', builder)
        self.assertIn("return finalSection;", builder)
        update_final = EXTENSION[
            EXTENSION.index("function updatePromptFinalSection(root, card, item, draft, blocked)"):
            EXTENSION.index("function updatePromptContextInPlace(card, item)")
        ]
        self.assertIn("body.insertBefore(finalSection, body.querySelector(\"[data-mvb-prompt-context]\") || null);", update_final)
        self.assertNotIn("replaceChildren", update_final)
        save_handler = EXTENSION[
            EXTENSION.index("async function savePrompt(root, sceneId, generationMethod)"):
            EXTENSION.index("function closeResourceDialogs(root)")
        ]
        self.assertIn("const savedRecord = project.prompts?.scenes", save_handler)
        self.assertIn('savedItem.status = "current";', save_handler)
        self.assertIn("savedItem.saved_final_prompt = savedRecord.final_prompt;", save_handler)

    def test_transition_flush_skips_unsavable_stale_drafts_without_deadlock(self):
        flush = EXTENSION[
            EXTENSION.index("async function flushPromptDrafts(root)"):
            EXTENSION.index("function scheduleAutosave(root)")
        ]
        self.assertIn('message.includes("Scene inputs changed")', flush)
        self.assertIn('message.includes("current Prompt Director response must be applied")', flush)
        self.assertIn("continue;", flush)
        self.assertIn('fetchJson(promptPath(projectId, sceneId)', flush)
        self.assertNotIn('promptItemFor(sceneId, generationMethod)', flush)

    def test_storyboard_mutations_reconcile_prompt_cards(self):
        apply_storyboard = EXTENSION[
            EXTENSION.index("async function applyStoryboard(root)"):
            EXTENSION.index("function openStoryboardClearDialog(root)")
        ]
        clear_storyboard = EXTENSION[
            EXTENSION.index("async function clearAppliedStoryboard(root)"):
            EXTENSION.index("async function flushPromptDrafts(root)")
        ]
        self.assertIn("void loadPromptCards(root, true);", apply_storyboard)
        self.assertIn("void loadPromptCards(root, true);", clear_storyboard)


if __name__ == "__main__":
    unittest.main()
