import copy
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectStorage, default_prompts_for_scenes
from backend.prompt_service import prompt_scene_state
from backend.render import (
    H3_FPS,
    H3_FRAME_STEP,
    H3_MINIMUM_FRAMES,
    MediaPreparationError,
    MediaToolAdapter,
    RenderPreparationBlocked,
    build_h3_timing_plan,
    build_preparation_fingerprint,
    build_render_preflight,
    compile_production_workflow,
    prepare_render_scene,
)
from backend.requirements import (
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_UNKNOWN,
    clear_requirements_snapshot_cache,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes
from backend.workflows import production_manifest_registry


ROOT = Path(__file__).parents[1]


class Phase8TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")

    @staticmethod
    def _reference(original_name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": original_name,
        }

    def _requirements(self, *, method_missing=None, tool_status=STATUS_AVAILABLE):
        registry = production_manifest_registry()
        methods = {}
        for method, manifest in registry.items():
            methods[method] = {
                "workflow_id": manifest["workflow_id"],
                "required_nodes": [
                    {"node_type": node_type, "status": STATUS_AVAILABLE}
                    for node_type in manifest["required_node_types"]
                ],
                "required_models": [
                    {
                        "role": declaration["role"],
                        "category": declaration["category"],
                        "filename": declaration["filename"],
                        "status": STATUS_MISSING if method == method_missing else STATUS_AVAILABLE,
                    }
                    for declaration in manifest["required_models"]
                ],
                "ready": method != method_missing,
            }
        tools = {
            name: {"name": name, "status": tool_status}
            for name in ("ffmpeg", "ffprobe")
        }
        return {
            "response_version": 1,
            "source": "test.injected",
            "methods": methods,
            "required_tools": tools,
            "required_ready": all(method_report["ready"] for method_report in methods.values())
            and tool_status == STATUS_AVAILABLE,
            "shared": {"nodes": [], "models": []},
            "optional": {},
        }

    def _project(self, method="keyframe_i2v", scene_count=1):
        project = self.storage.create_project("Phase 8 structural render")
        cues = [
            {
                "cue_number": index + 1,
                "start_ms": index * 4_000,
                "end_ms": (index + 1) * 4_000,
                "text": "No god in the house.",
            }
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
        locations = [{
            "location_id": location_id,
            "name": "Chapel",
            "description": "Cold stone walls, reflective water, and hard white side light.",
            "references": [location_reference],
        }]
        visuals = default_visuals_for_scenes(scenes)
        for visual in visuals["scenes"]:
            visual["generation_method"] = method
            if method == "reference2video":
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

        project_root = self.storage.project_directory(project["project_id"])
        (project_root / "source" / "master_audio.wav").write_bytes(b"authoritative master audio")
        for entity_type, entity_id, reference in (
            ("characters", character_id, character_reference),
            ("locations", location_id, location_reference),
        ):
            reference_directory = project_root / "references" / entity_type / entity_id
            reference_directory.mkdir(parents=True, exist_ok=True)
            (reference_directory / reference["stored_name"]).write_bytes(b"reference still")

        for scene in scenes:
            asset_id = str(uuid.uuid4())
            stored_name = f"{asset_id}.png"
            keyframe_directory = project_root / "keyframes" / scene["scene_id"]
            keyframe_directory.mkdir(parents=True, exist_ok=True)
            (keyframe_directory / stored_name).write_bytes(b"accepted keyframe")
            visual = next(entry for entry in visuals["scenes"] if entry["scene_id"] == scene["scene_id"])
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
                "character_ids": [character_id],
                "location_id": location_id,
                "action": "Vesper performs with controlled intensity.",
                "visual_instructions": "Cold gothic performance staging.",
                "camera_direction": "A slow lateral camera drift.",
                "motion_direction": "Restrained forward movement.",
                "continuity_notes": "Keep Vesper and the chapel consistent.",
                "required_references": [],
            }
            for scene in scenes
        ]
        storyboard = validate_storyboard_response(project, {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": storyboard_scenes,
        })
        project = self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )
        prompt_records = copy.deepcopy(project["prompts"])
        for scene in scenes:
            current = prompt_scene_state(project, scene["scene_id"], storage=self.storage, generation_method=method)
            for prompt_method in ("keyframe_i2v", "reference2video"):
                prompt_records["scenes"][scenes.index(scene)][prompt_method] = {
                    "final_prompt": "A current production prompt for this scene.",
                    "source_fingerprint": current["source_fingerprint"] if prompt_method == method else None,
                    "relay_fingerprint": current["source_fingerprint"] if prompt_method == method else "",
                }
        return self.storage.save_project(
            project["project_id"],
            {**project, "prompts": prompt_records},
            allow_prompts_change=True,
        )

    def _fake_media_adapter(self):
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            Path(arguments[-1]).write_bytes(b"prepared scene audio")
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

        adapter = MediaToolAdapter(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            runner=runner,
            duration_probe=lambda _path, *_args: 4_000,
        )
        return adapter, calls

    def test_render_stage_and_routes_are_real_preparation_surface(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('data-mvb-view="render"', extension)
        self.assertIn("data-mvb-render-refresh", extension)
        self.assertIn("Prepare Render Inputs", extension)
        self.assertNotIn("Render Scene", extension)
        self.assertIn("/music-video-builder/projects/{project_id}/render/preflight", routes)
        self.assertIn("/music-video-builder/projects/{project_id}/render/scenes/{scene_id}/prepare", routes)

    def test_preflight_is_read_only_and_reports_required_dimensions(self):
        project = self._project()
        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        before = project_file.read_bytes()
        result = build_render_preflight(project, self.storage, requirements_report=self._requirements())
        self.assertEqual(project_file.read_bytes(), before)
        scene = result["scenes"][0]
        for field in (
            "scene_id", "generation_method", "duration_ms", "prompt_status", "prompt_ready",
            "visuals_ready", "source_audio_ready", "workflow_contract_ready", "requirements_ready",
            "preparation_ready", "blockers", "warnings",
        ):
            self.assertIn(field, scene)
        self.assertTrue(scene["content_ready"])
        self.assertTrue(scene["workflow_ready"])
        self.assertTrue(scene["runtime_requirements_ready"])
        self.assertFalse(scene["target_hardware_qualified"])
        self.assertTrue(any(item["code"] == "TARGET_HARDWARE_DEFERRED" for item in scene["warnings"]))

    def test_prompt_status_current_needs_gpt_unsaved_and_stale_are_distinct_blockers(self):
        project = self._project()
        report = self._requirements()
        scene_id = project["scenes"][0]["scene_id"]
        current = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]
        self.assertEqual(current["prompt_status"], "current")
        self.assertTrue(current["prompt_ready"])

        needs_gpt_project = copy.deepcopy(project)
        needs_gpt_project["prompts"]["scenes"][0]["keyframe_i2v"] = {
            "final_prompt": "",
            "source_fingerprint": None,
            "relay_fingerprint": "",
        }
        needs_gpt = build_render_preflight(needs_gpt_project, self.storage, requirements_report=report)["scenes"][0]
        self.assertEqual(needs_gpt["prompt_status"], "needs_gpt")
        self.assertIn("PROMPT_MISSING", {item["code"] for item in needs_gpt["blockers"]})

        unsaved_state = prompt_scene_state(project, scene_id, storage=self.storage, generation_method="keyframe_i2v")
        unsaved = build_render_preflight(
            project,
            self.storage,
            requirements_report=report,
            prompt_drafts={f"{scene_id}:keyframe_i2v": {
                "dirty": True,
                "sourceFingerprint": unsaved_state["source_fingerprint"],
            }},
        )["scenes"][0]
        self.assertEqual(unsaved["prompt_status"], "unsaved")
        self.assertIn("PROMPT_UNSAVED", {item["code"] for item in unsaved["blockers"]})

        stale_project = copy.deepcopy(project)
        stale_project["prompts"]["scenes"][0]["keyframe_i2v"]["source_fingerprint"] = "0" * 64
        stale = build_render_preflight(stale_project, self.storage, requirements_report=report)["scenes"][0]
        self.assertEqual(stale["prompt_status"], "stale")
        self.assertIn("PROMPT_STALE", {item["code"] for item in stale["blockers"]})

    def test_missing_visual_source_and_audio_use_stable_blocker_codes(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        root = self.storage.project_directory(project["project_id"])
        keyframe = next((root / "keyframes" / scene_id).iterdir())
        keyframe.unlink()
        project["source"]["master_audio"] = None
        scene = build_render_preflight(project, self.storage, requirements_report=self._requirements())["scenes"][0]
        codes = {item["code"] for item in scene["blockers"]}
        self.assertIn("SOURCE_AUDIO_MISSING", codes)
        self.assertIn("VISUAL_KEYFRAME_MISSING", codes)
        self.assertTrue(all(set(item) >= {"code", "message", "layer"} for item in scene["blockers"]))

    def test_invalid_ref2va_mapping_is_blocked_without_dropping_order(self):
        project = self._project(method="reference2video")
        project["visuals"]["scenes"][0]["reference2video"]["selected_references"][0]["reference_id"] = str(uuid.uuid4())
        scene = build_render_preflight(project, self.storage, requirements_report=self._requirements())["scenes"][0]
        self.assertIn("REF2VA_MAPPING_INVALID", {item["code"] for item in scene["blockers"]})
        self.assertFalse(scene["visuals_ready"])

    def test_requirements_failure_is_separate_from_content_and_hardware(self):
        project = self._project()
        scene = build_render_preflight(
            project,
            self.storage,
            requirements_report=self._requirements(method_missing="keyframe_i2v"),
        )["scenes"][0]
        self.assertTrue(scene["content_ready"])
        self.assertTrue(scene["workflow_ready"])
        self.assertFalse(scene["runtime_requirements_ready"])
        self.assertFalse(scene["preparation_ready"])
        self.assertIn("REQUIREMENTS_MISSING", {item["code"] for item in scene["blockers"]})
        self.assertFalse(scene["target_hardware_qualified"])

    def test_one_incomplete_scene_does_not_gate_unrelated_scene(self):
        project = self._project(scene_count=2)
        project["prompts"]["scenes"][1]["keyframe_i2v"] = {
            "final_prompt": "",
            "source_fingerprint": None,
            "relay_fingerprint": "",
        }
        result = build_render_preflight(project, self.storage, requirements_report=self._requirements())
        self.assertTrue(result["scenes"][0]["preparation_ready"])
        self.assertFalse(result["scenes"][1]["preparation_ready"])
        self.assertEqual(result["preparation_ready_count"], 1)

    def test_h3_timing_plan_records_exact_audio_and_explicit_trim(self):
        plan = build_h3_timing_plan(4_000)
        self.assertEqual(plan["h3_fps"], H3_FPS)
        self.assertEqual(plan["frame_formula"], "5 + 17n")
        self.assertEqual(plan["target_duration_ms"], 4_000)
        self.assertEqual(plan["authoritative_audio_duration_ms"], 4_000)
        self.assertEqual(plan["generated_frame_count"] % H3_FRAME_STEP, 5 % H3_FRAME_STEP)
        self.assertEqual(plan["generated_frame_count"], 90)
        self.assertEqual(plan["generated_duration_ms"], 3750.0)
        self.assertTrue(plan["trim_required"])

    def test_h3_timing_plan_handles_short_edge_and_fails_closed(self):
        short_plan = build_h3_timing_plan(1)
        self.assertEqual(short_plan["generated_frame_count"], H3_MINIMUM_FRAMES)
        self.assertEqual(short_plan["authoritative_audio_duration_ms"], 1)
        with self.assertRaisesRegex(Exception, "approved 24 FPS"):
            build_h3_timing_plan(4_000, fps=25)
        with self.assertRaisesRegex(Exception, "positive integer"):
            build_h3_timing_plan(0)

    def test_media_adapter_uses_safe_arguments_exact_boundaries_and_atomic_replace(self):
        project = self._project()
        root = self.storage.project_directory(project["project_id"])
        adapter, calls = self._fake_media_adapter()
        destination = root / "scene_audio" / project["scenes"][0]["scene_id"] / "scene_audio.wav"
        duration = adapter.extract_scene_audio(
            root / "source" / "master_audio.wav",
            destination,
            project_root=root,
            start_ms=125,
            end_ms=4_125,
        )
        self.assertEqual(duration, 4_000)
        self.assertTrue(destination.is_file())
        arguments, kwargs = calls[0]
        self.assertEqual(arguments[arguments.index("-ss") + 1], "0.125")
        self.assertEqual(arguments[arguments.index("-t") + 1], "4.000")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 120)

    def test_media_failure_is_structured_and_unowned_path_is_rejected(self):
        project = self._project()
        root = self.storage.project_directory(project["project_id"])

        def failed_runner(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="ffmpeg diagnostic")

        failed = MediaToolAdapter(
            ffmpeg_path="ffmpeg.exe",
            runner=failed_runner,
            duration_probe=lambda *_args: 4_000,
        )
        with self.assertRaises(MediaPreparationError) as context:
            failed.extract_scene_audio(
                root / "source" / "master_audio.wav",
                root / "scene_audio" / "failed.wav",
                project_root=root,
                start_ms=0,
                end_ms=4_000,
            )
        self.assertEqual(context.exception.code, "MEDIA_EXTRACTION_FAILED")
        with self.assertRaises(MediaPreparationError) as context:
            failed.extract_scene_audio(
                Path(self.temp_directory.name) / "outside.wav",
                root / "scene_audio" / "unsafe.wav",
                project_root=root,
                start_ms=0,
                end_ms=4_000,
            )
        self.assertEqual(context.exception.code, "PATH_UNSAFE")

    def test_i2v_preparation_is_dry_deterministic_and_preserves_master_and_prompt_provenance(self):
        project = self._project(method="keyframe_i2v")
        project_id = project["project_id"]
        scene_id = project["scenes"][0]["scene_id"]
        project_file = self.storage.project_directory(project_id) / "project.json"
        before = project_file.read_bytes()
        adapter, calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project_id,
            scene_id,
            requirements_report=self._requirements(),
            media_adapter=adapter,
        )
        self.assertEqual(result["status"], "prepared")
        self.assertFalse(result["queue_submitted"])
        self.assertTrue(result["workflow_validated"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(project_file.read_bytes(), before)
        workflow = json.loads((self.storage.project_directory(project_id) / "renders" / scene_id / "workflow.json").read_text())
        self.assertEqual(workflow["7"]["inputs"]["prompt"], "A current production prompt for this scene.")
        self.assertEqual(workflow["7"]["inputs"]["length"], result["preparation"]["timing"]["generated_frame_count"])
        self.assertIn("music_video_builder", workflow["5"]["inputs"]["image"])
        self.assertIn("music_video_builder", workflow["6"]["inputs"]["audio"])
        self.assertTrue((self.storage.project_directory(project_id) / "scene_audio" / scene_id / "scene_audio.wav").is_file())
        self.assertTrue((self.storage.project_directory(project_id) / "renders" / scene_id / "preparation.json").is_file())
        repeated = prepare_render_scene(
            self.storage,
            project_id,
            scene_id,
            requirements_report=self._requirements(),
            media_adapter=adapter,
        )
        self.assertTrue(repeated["reused"])
        self.assertEqual(len(calls), 1)

    def test_ref2va_preparation_preserves_order_and_capacity(self):
        project = self._project(method="reference2video")
        scene_id = project["scenes"][0]["scene_id"]
        adapter, _calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project["project_id"],
            scene_id,
            requirements_report=self._requirements(),
            media_adapter=adapter,
        )
        root = self.storage.project_directory(project["project_id"])
        workflow = json.loads((root / "renders" / scene_id / "workflow.json").read_text())
        self.assertEqual(workflow["50"]["inputs"]["image"].split("/")[-1], "picture_01.png")
        self.assertEqual(workflow["51"]["inputs"]["image"].split("/")[-1], "picture_02.png")
        self.assertNotIn("52", workflow)
        self.assertNotIn("ref_images.ref_image_2", workflow["8"]["inputs"])
        self.assertEqual(result["preparation"]["visual_inputs"][0]["picture_number"], 1)
        self.assertEqual(result["preparation"]["visual_inputs"][1]["picture_number"], 2)

    def test_template_is_not_mutated_by_compilation(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        from backend.workflows import WORKFLOW_DIRECTORY

        template_path = WORKFLOW_DIRECTORY / "h3_music_video_i2v_api.json"
        before = template_path.read_bytes()
        timing = build_h3_timing_plan(4_000)
        visual_inputs = [{"queue_name": "music_video_builder/p/keyframe.png"}]
        workflow, _validation = compile_production_workflow(
            project,
            scene_id,
            final_prompt="A current production prompt for this scene.",
            timing_plan=timing,
            visual_inputs=visual_inputs,
            scene_audio_name="music_video_builder/p/scene_audio.wav",
        )
        self.assertEqual(template_path.read_bytes(), before)
        self.assertNotEqual(workflow["7"]["inputs"]["length"], 73)

    def test_prepare_requires_current_content_and_does_not_run_media_when_blocked(self):
        project = self._project()
        project["prompts"]["scenes"][0]["keyframe_i2v"]["source_fingerprint"] = "1" * 64
        project = self.storage.save_project(
            project["project_id"],
            project,
            allow_prompts_change=True,
        )
        adapter, calls = self._fake_media_adapter()
        with self.assertRaises(RenderPreparationBlocked) as context:
            prepare_render_scene(
                self.storage,
                project["project_id"],
                project["scenes"][0]["scene_id"],
                requirements_report=self._requirements(),
                media_adapter=adapter,
            )
        self.assertIn("PROMPT_STALE", {item["code"] for item in context.exception.preflight["blockers"]})
        self.assertEqual(calls, [])

    def test_preparation_fingerprint_changes_for_relevant_inputs_but_not_unrelated_scene(self):
        project = self._project(scene_count=2)
        report = self._requirements()
        first = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]
        original_fingerprint = first["preparation_fingerprint"]
        changed = copy.deepcopy(project)
        changed["prompts"]["scenes"][1]["keyframe_i2v"]["final_prompt"] = "Unrelated scene changed."
        unrelated = build_render_preflight(changed, self.storage, requirements_report=report)["scenes"][0]
        self.assertEqual(unrelated["preparation_fingerprint"], original_fingerprint)

        changed["prompts"]["scenes"][0]["keyframe_i2v"]["final_prompt"] = "Target scene changed."
        relevant = build_render_preflight(changed, self.storage, requirements_report=report)["scenes"][0]
        self.assertNotEqual(relevant["preparation_fingerprint"], original_fingerprint)

    def test_preparation_fingerprint_changes_for_file_identity_and_provenance(self):
        project = self._project()
        report = self._requirements()
        scene = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]
        original = scene["preparation_fingerprint"]
        root = self.storage.project_directory(project["project_id"])
        (root / "source" / "master_audio.wav").write_bytes(b"master audio changed")
        audio_changed = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]
        self.assertNotEqual(audio_changed["preparation_fingerprint"], original)

        provenance_changed = copy.deepcopy(project)
        provenance_changed["prompts"]["scenes"][0]["keyframe_i2v"]["relay_fingerprint"] = "2" * 64
        changed = build_render_preflight(provenance_changed, self.storage, requirements_report=report)["scenes"][0]
        self.assertNotEqual(changed["preparation_fingerprint"], original)

        keyframe = next((root / "keyframes" / project["scenes"][0]["scene_id"]).iterdir())
        keyframe.write_bytes(b"accepted keyframe changed")
        visual_changed = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]
        self.assertNotEqual(visual_changed["preparation_fingerprint"], original)

    def test_ref2va_reference_order_changes_preparation_identity(self):
        project = self._project(method="reference2video")
        report = self._requirements()
        original = build_render_preflight(project, self.storage, requirements_report=report)["scenes"][0]["preparation_fingerprint"]
        changed = copy.deepcopy(project)
        selected = changed["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        selected.reverse()
        changed["prompts"]["scenes"][0]["reference2video"]["source_fingerprint"] = None
        changed["prompts"]["scenes"][0]["reference2video"]["relay_fingerprint"] = ""
        changed["prompts"]["scenes"][0]["reference2video"]["final_prompt"] = ""
        # The visual mapping itself is sufficient to prove preparation identity
        # changes even though the now-stale prompt correctly blocks preparation.
        changed_scene = build_render_preflight(changed, self.storage, requirements_report=report)["scenes"][0]
        self.assertNotEqual(changed_scene["preparation_fingerprint"], original)

    def test_no_execution_boundary_is_present_in_phase8a_backend(self):
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")
        self.assertNotIn("/prompt", render_source)
        self.assertNotIn("queue_prompt", render_source)
        self.assertNotIn("PromptServer", render_source)
        self.assertNotIn("shell=True", render_source)
        self.assertIn('"queue_submitted": False', render_source)

    def test_preparation_artifacts_are_project_owned_and_schema_stays_v7(self):
        project = self._project()
        adapter, _calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
        )
        self.assertEqual(self.storage.load_project(project["project_id"])["schema_version"], 7)
        metadata = result["preparation"]
        root = self.storage.project_directory(project["project_id"]).resolve()
        self.assertTrue((root / metadata["workflow"]["relative_path"]).resolve().is_file())
        self.assertTrue((root / metadata["source_audio"]["prepared_relative_path"]).resolve().is_file())
        self.assertTrue(all(not Path(item["prepared_relative_path"]).is_absolute() for item in metadata["visual_inputs"]))

    def test_fingerprint_helper_is_stable_and_includes_workflow_contract(self):
        project = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        state = prompt_scene_state(project, scene_id, storage=self.storage, generation_method="keyframe_i2v")
        timing = build_h3_timing_plan(4_000)
        root = self.storage.project_directory(project["project_id"])
        source_identity = {
            "relative_path": "source/master_audio.wav",
            "size": (root / "source" / "master_audio.wav").stat().st_size,
            "sha256": "a" * 64,
        }
        visual_inputs = [{"role": "keyframe", "queue_name": "keyframe.png", "source_name": "keyframes/x/keyframe.png", "sha256": "b" * 64}]
        workflow = {"workflow_id": "one", "template_sha256": "c" * 64, "manifest_sha256": "d" * 64}
        first = build_preparation_fingerprint(
            project,
            scene_id,
            prompt_state=state,
            timing_plan=timing,
            source_audio_identity=source_identity,
            visual_inputs=visual_inputs,
            workflow_identity=workflow,
        )
        second = build_preparation_fingerprint(
            project,
            scene_id,
            prompt_state=state,
            timing_plan=timing,
            source_audio_identity=source_identity,
            visual_inputs=visual_inputs,
            workflow_identity=workflow,
        )
        self.assertEqual(first, second)
        workflow["template_sha256"] = "e" * 64
        self.assertNotEqual(
            first,
            build_preparation_fingerprint(
                project,
                scene_id,
                prompt_state=state,
                timing_plan=timing,
                source_audio_identity=source_identity,
                visual_inputs=visual_inputs,
                workflow_identity=workflow,
            ),
        )

    def test_cached_requirements_do_not_hide_prompt_status_changes(self):
        project = self._project()
        clear_requirements_snapshot_cache()
        calls = []

        def scanner():
            calls.append(1)
            return self._requirements()

        with patch("backend.requirements.scan_requirements", side_effect=scanner):
            first_result = build_render_preflight(project, self.storage)
            first = first_result["scenes"][0]
            changed = copy.deepcopy(project)
            changed["prompts"]["scenes"][0]["keyframe_i2v"]["source_fingerprint"] = "0" * 64
            second_result = build_render_preflight(changed, self.storage)
            second = second_result["scenes"][0]

        self.assertEqual(calls, [1])
        self.assertEqual(first["prompt_status"], "current")
        self.assertEqual(second["prompt_status"], "stale")
        self.assertEqual(second_result["requirements_cache"]["status"], "warm")

    def test_cached_requirements_do_not_hide_visuals_or_preparation_changes(self):
        project = self._project()
        clear_requirements_snapshot_cache()
        calls = []

        def scanner():
            calls.append(1)
            return self._requirements()

        with patch("backend.requirements.scan_requirements", side_effect=scanner):
            first = build_render_preflight(project, self.storage)["scenes"][0]
            changed = copy.deepcopy(project)
            changed["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"] = None
            second = build_render_preflight(changed, self.storage)["scenes"][0]

        self.assertEqual(calls, [1])
        self.assertTrue(first["visuals_ready"])
        self.assertFalse(second["visuals_ready"])
        self.assertNotEqual(first["preparation_fingerprint"], second["preparation_fingerprint"])


if __name__ == "__main__":
    unittest.main()
