import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.prompt_service import (
    build_ref2va_mapping,
    build_prompt_relay_request,
    compile_scene_prompt,
    validate_prompt_relay_response,
    validate_relay_description,
)
from backend.projects import ProjectStorage, ProjectValidationError, default_prompts_for_scenes
from backend.requirements import (
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_UNKNOWN,
    scan_requirements,
)
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import (
    default_visuals_for_scenes,
    save_reference_selection,
    set_generation_method,
)
from backend.workflows import WORKFLOW_MANIFESTS, production_manifest_registry


class _FakeFolderPaths:
    folder_names_and_paths = {
        "diffusion_models": ([], set()),
        "text_encoders": ([], set()),
        "vae": ([], set()),
    }

    @staticmethod
    def get_full_path(category, filename):
        return f"C:/ComfyUI/models/{category}/{filename}"


class Phase7TestCase(unittest.TestCase):
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

    def _project(self):
        project = self.storage.create_project("Phase 7 Prompt Project")
        scene = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "The lyric remains verbatim."}],
            4_000,
        )
        character_id = str(uuid.uuid4())
        location_id = str(uuid.uuid4())
        character_refs = [self._reference("vesper-wide.png"), self._reference("vesper-close.png")]
        location_refs = [self._reference("chapel-wide.png"), self._reference("chapel-detail.png")]
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
                "scenes": scene,
                "characters": [
                    {
                        "character_id": character_id,
                        "name": "Vesper",
                        "role": "performer",
                        "appearance": "Copper hair.",
                        "outfit": "Black jacket.",
                        "references": character_refs,
                    }
                ],
                "locations": [
                    {
                        "location_id": location_id,
                        "name": "Chapel",
                        "description": "Stone interior with warm practical light.",
                        "references": location_refs,
                    }
                ],
                "visuals": default_visuals_for_scenes(scene),
                "prompts": default_prompts_for_scenes(scene),
            },
            allow_visuals_change=True,
        )
        storyboard_response = {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [
                {
                    "scene_id": scene[0]["scene_id"],
                    "scene_type": "performance",
                    "character_ids": [character_id],
                    "location_id": location_id,
                    "action": "Vesper holds the frame.",
                    "visual_instructions": "Warm light on the stone interior.",
                    "camera_direction": "Slow lateral tracking.",
                    "motion_direction": "Measured movement.",
                    "continuity_notes": "Keep the jacket and chapel consistent.",
                    "required_references": [],
                }
            ],
        }
        storyboard = validate_storyboard_response(project, storyboard_response)
        project = self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )
        return project, character_refs, location_refs

    def test_requirements_are_manifest_driven_and_deterministic(self):
        node_types = {
            node_type
            for manifest in WORKFLOW_MANIFESTS.values()
            for node_type in manifest["required_node_types"]
        }
        report_one = scan_requirements(
            node_types=node_types,
            folder_paths_module=_FakeFolderPaths,
            binary_finder=lambda name: f"C:/tools/{name}.exe",
        )
        report_two = scan_requirements(
            node_types=node_types,
            folder_paths_module=_FakeFolderPaths,
            binary_finder=lambda name: f"C:/tools/{name}.exe",
        )
        self.assertEqual(report_one, report_two)
        self.assertEqual(report_one["source"], "backend.workflows.production_manifest_registry")
        self.assertTrue(report_one["required_ready"])
        self.assertEqual(set(report_one["methods"]), {"keyframe_i2v", "reference2video"})
        self.assertTrue(all(item["status"] == STATUS_AVAILABLE for item in report_one["shared"]["models"]))
        self.assertTrue(all(item["status"] == STATUS_AVAILABLE for item in report_one["shared"]["nodes"]))
        self.assertEqual(len({item["node_type"] for item in report_one["shared"]["nodes"]}), len(report_one["shared"]["nodes"]))
        self.assertIn("ffmpeg", report_one["required_tools"])
        self.assertIn("version", report_one["required_tools"]["ffmpeg"])
        shared_model_keys = {(item["category"], item["filename"]) for item in report_one["shared"]["models"]}
        self.assertEqual(len(shared_model_keys), len(report_one["shared"]["models"]))

    def test_requirements_distinguish_missing_and_unknown_and_optional_absence(self):
        report = scan_requirements(
            node_types={"UNETLoader"},
            folder_paths_module=None,
            binary_finder=lambda _name: None,
        )
        self.assertFalse(report["required_ready"])
        self.assertEqual(report["methods"]["keyframe_i2v"]["required_nodes"][0]["status"], STATUS_AVAILABLE)
        self.assertEqual(report["methods"]["keyframe_i2v"]["required_nodes"][1]["status"], STATUS_MISSING)
        self.assertTrue(all(item["status"] == STATUS_UNKNOWN for item in report["methods"]["keyframe_i2v"]["required_models"]))
        self.assertEqual(report["optional"]["rtx_vsr"]["status"], STATUS_MISSING)
        self.assertEqual(report["optional"]["seedvr2"]["status"], STATUS_MISSING)

    def test_requirements_exclude_donor_only_nodes_and_report_missing_models(self):
        class MissingFolderPaths(_FakeFolderPaths):
            @staticmethod
            def get_full_path(_category, _filename):
                return None

            @staticmethod
            def get_filename_list(_category):
                return []

        report = scan_requirements(
            node_types={"UNETLoader", "CLIPLoader", "VAELoader"},
            folder_paths_module=MissingFolderPaths,
            binary_finder=lambda _name: None,
                )
        required_nodes = {item["node_type"] for item in report["shared"]["nodes"]}
        self.assertNotIn("MiniMaxH3TurboSampler", required_nodes)
        self.assertNotIn("RTXVideoSuperResolution", required_nodes)
        self.assertFalse(report["required_ready"])
        self.assertTrue(all(item["status"] == STATUS_MISSING for item in report["shared"]["models"]))

    def test_node_scan_keeps_unknown_distinct_from_missing(self):
        with patch("backend.requirements._load_active_node_types", return_value=(None, "registry unavailable")):
            report = scan_requirements(
                folder_paths_module=None,
                binary_finder=lambda _name: None,
            )
        self.assertEqual(report["methods"]["keyframe_i2v"]["required_nodes"][0]["status"], STATUS_UNKNOWN)

    def test_requirements_have_no_local_lm_optional_capability(self):
        report = scan_requirements(
            node_types={"UNETLoader"},
            folder_paths_module=None,
            binary_finder=lambda _name: None,
        )
        self.assertNotIn("ollama", report["optional"])

    def test_ref2va_mapping_numbers_all_entity_owners_in_selection_order(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        character_id = project["characters"][0]["character_id"]
        location_id = project["locations"][0]["location_id"]
        selected = [
            {"entity_type": "character", "entity_id": character_id, "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "character", "entity_id": character_id, "reference_id": character_refs[1]["reference_id"]},
            {"entity_type": "location", "entity_id": location_id, "reference_id": location_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": location_id, "reference_id": location_refs[1]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        mapping = build_ref2va_mapping(project, scene_id)
        self.assertEqual([item["picture_tag"] for item in mapping["pictures"]], ["<Picture 1>", "<Picture 2>", "<Picture 3>", "<Picture 4>"])
        self.assertEqual([item["subject_tag"] for item in mapping["pictures"]], ["<Subject 1>", "<Subject 1>", "<Subject 2>", "<Subject 2>"])
        reordered = [selected[2], selected[0], selected[3]]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": reordered})
        reordered_mapping = build_ref2va_mapping(project, scene_id)
        self.assertEqual([item["subject_tag"] for item in reordered_mapping["pictures"]], ["<Subject 1>", "<Subject 2>", "<Subject 1>"])

    def test_ref2va_mapping_supports_nine_pictures_and_rejects_unknown_selection_data(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        character_id = project["characters"][0]["character_id"]
        location_id = project["locations"][0]["location_id"]
        extra_character_refs = [self._reference(f"vesper-extra-{index}.png") for index in range(6)]
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "characters": [{**project["characters"][0], "references": [*character_refs, *extra_character_refs]}],
            },
        )
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": character_id, "reference_id": reference["reference_id"]}
            for reference in [*character_refs, *extra_character_refs]
        ]
        selected.append({"entity_type": "location", "entity_id": location_id, "reference_id": location_refs[0]["reference_id"]})
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        mapping = build_ref2va_mapping(project, scene_id)
        self.assertEqual(len(mapping["pictures"]), 9)
        self.assertEqual(mapping["pictures"][-1]["picture_tag"], "<Picture 9>")
        self.assertEqual(mapping["pictures"][-1]["subject_tag"], "<Subject 2>")
        corrupted = {**project, "visuals": {"scenes": [{**project["visuals"]["scenes"][0], "reference2video": {"selected_references": [{**selected[0], "entity_id": str(uuid.uuid4())}]}}]}}
        with self.assertRaises(ProjectValidationError):
            build_ref2va_mapping(corrupted, scene_id)

    def test_i2v_compiler_uses_actual_keyframe_and_forbids_ref2va_tags(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        character_id = project["characters"][0]["character_id"]
        location_id = project["locations"][0]["location_id"]
        visual = project["visuals"]["scenes"][0]
        asset_id = str(uuid.uuid4())
        visual = {
            **visual,
            "keyframe_i2v": {
                **visual["keyframe_i2v"],
                "intended_keyframe_description": "This must not become authoritative.",
                "actual_keyframe_description": "Accepted image shows Vesper in warm chapel light.",
                "accepted_keyframe": {
                    "asset_id": asset_id,
                    "stored_name": f"{asset_id}.png",
                    "original_name": "accepted.png",
                },
            },
        }
        project = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": {"scenes": [visual]}},
            allow_visuals_change=True,
        )
        result = compile_scene_prompt(project, scene_id)
        prompt = result["deterministic_prompt"]
        self.assertIn("Accepted image shows Vesper", prompt)
        self.assertNotIn("This must not become authoritative", prompt)
        self.assertIn("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.", prompt)
        self.assertNotIn("<Subject", prompt)
        self.assertNotIn("<Audio", prompt)
        self.assertIn("integrated_multimodal_description:", prompt)
        self.assertIn("overall_soundscape:", prompt)
        self.assertIn("non_diegetic_music:", prompt)
        self.assertLess(prompt.index("<Picture 1> (from [Shot 1])"), prompt.index("integrated_multimodal_description:"))
        opening = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
        self.assertTrue(prompt.startswith(opening + "\n\nintegrated_multimodal_description: [Shot 1]"))
        self.assertIn("integrated_multimodal_description: [Shot 1]", prompt)
        self.assertLess(prompt.index("integrated_multimodal_description:"), prompt.index("overall_soundscape:"))
        self.assertLess(prompt.index("overall_soundscape:"), prompt.index("non_diegetic_music:"))
        self.assertNotIn(scene_id, prompt)
        self.assertNotIn(character_id, prompt)
        self.assertNotIn(location_id, prompt)
        self.assertNotIn(asset_id, prompt)
        self.assertNotIn("source_cue_numbers", prompt)
        self.assertNotIn("accepted.png", prompt)
        self.assertIn(
            "The accepted first frame establishes the opening: Accepted image shows Vesper in warm chapel light.",
            prompt,
        )
        self.assertIn("Vesper remains consistent with the opening image", prompt)
        self.assertIn("The Chapel environment remains consistent with the opening image", prompt)
        for boilerplate in (
            "Opening state is defined exclusively",
            "The target duration is exactly",
            "Actual accepted-keyframe description:",
            "Continuity context for",
            "Forward-generation intent",
            "Treat the supplied lyric as",
        ):
            self.assertNotIn(boilerplate, prompt)
        self.assertEqual(result["reference_map"]["pictures"], [])

    def test_compiler_is_byte_deterministic_and_instrumental_does_not_invent_vocals(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        first = compile_scene_prompt(project, scene_id)
        second = compile_scene_prompt(project, scene_id)
        self.assertEqual(first["deterministic_prompt"], second["deterministic_prompt"])
        instrumental = {**project, "scenes": [{**project["scenes"][0], "source_kind": "instrumental", "lyric": None, "source_cue_numbers": []}]}
        prompt = compile_scene_prompt(instrumental, scene_id)["deterministic_prompt"]
        self.assertIn("instrumental scene", prompt)
        self.assertNotIn("singing", prompt.lower())

    def test_missing_camera_and_optional_descriptions_do_not_invent_movement(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        storyboard_scene = {
            **project["storyboard"]["scenes"][0],
            "camera_direction": "",
            "motion_direction": "",
        }
        project = {
            **project,
            "characters": [{**project["characters"][0], "appearance": "", "outfit": ""}],
            "storyboard": {**project["storyboard"], "scenes": [storyboard_scene]},
        }
        prompt = compile_scene_prompt(project, scene_id)["deterministic_prompt"]
        self.assertNotIn("tracking", prompt.lower())
        self.assertNotIn("push in", prompt.lower())
        self.assertNotIn("appearance:", prompt.lower())

    def test_ref2va_compiler_has_required_sections_and_local_provenance(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": project["locations"][0]["location_id"], "reference_id": location_refs[0]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        result = compile_scene_prompt(project, scene_id)
        prompt = result["deterministic_prompt"]
        self.assertTrue(prompt.startswith("subject_definitions:"))
        self.assertTrue(all(section + ":" in prompt for section in ("subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music")))
        self.assertLess(prompt.index("subject_definitions:"), prompt.index("summary:"))
        self.assertLess(prompt.index("summary:"), prompt.index("retention_analysis:"))
        self.assertIn("<Picture 1>", prompt)
        self.assertIn("<Subject 1>", prompt)
        self.assertIn("<Subject 2>", prompt)
        self.assertIn("<Audio 1>", prompt)
        self.assertIn("<Audio 1>: fully_copy -", prompt)
        self.assertEqual(prompt.count("fully_preserved"), 2)
        self.assertNotIn("<Location", prompt)
        self.assertIn("Vesper (S1) sings: <d>[English] The lyric remains verbatim.</d>", prompt)
        self.assertIn("<Subject 1> (appears in [Shot 1]): fully_preserved -", prompt)
        self.assertIn("<Subject 2> (appears in [Shot 1]): fully_preserved -", prompt)
        self.assertIn("<Audio 1>: fully_copy -", prompt)
        section_order = [
            prompt.index(f"{section}:")
            for section in (
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            )
        ]
        self.assertEqual(section_order, sorted(section_order))
        self.assertNotIn(project["characters"][0]["character_id"], prompt)
        self.assertNotIn(project["locations"][0]["location_id"], prompt)
        self.assertNotIn(character_refs[0]["original_name"], prompt)
        self.assertNotIn(location_refs[0]["original_name"], prompt)
        self.assertNotIn("source_cue_numbers", prompt)
        self.assertNotIn("timeline_start_ms", prompt)

    def test_lyric_is_contextual_not_unconditional(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        performance_prompt = compile_scene_prompt(project, scene_id)["deterministic_prompt"]
        self.assertIn("Vesper (S1) sings: <d>[English] The lyric remains verbatim.</d>", performance_prompt)
        narrative_storyboard = {
            **project["storyboard"],
            "scenes": [{**project["storyboard"]["scenes"][0], "scene_type": "narrative"}],
        }
        narrative = {**project, "storyboard": narrative_storyboard}
        narrative_prompt = compile_scene_prompt(narrative, scene_id)["deterministic_prompt"]
        self.assertNotIn("The lyric remains verbatim.", narrative_prompt)
        instrumental = {
            **project,
            "scenes": [
                {
                    **project["scenes"][0],
                    "source_kind": "instrumental",
                    "lyric": "Instrumental",
                    "source_cue_numbers": [],
                }
            ],
        }
        instrumental_prompt = compile_scene_prompt(instrumental, scene_id)["deterministic_prompt"]
        self.assertNotIn("Instrumental", instrumental_prompt)
        self.assertIn("do not invent vocal words", instrumental_prompt)
        lyric_marker = {
            **project,
            "scenes": [{
                **project["scenes"][0],
                "source_kind": "lyric",
                "lyric": "Instrumental",
            }],
        }
        marker_prompt = compile_scene_prompt(lyric_marker, scene_id)["deterministic_prompt"]
        self.assertNotIn("<d>[English] Instrumental</d>", marker_prompt)
        self.assertIn("do not invent vocal words", marker_prompt)

    def test_custom_gpt_request_is_deterministic_and_method_specific(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": project["locations"][0]["location_id"], "reference_id": location_refs[0]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        first = build_prompt_relay_request(project, scene_id)
        second = build_prompt_relay_request(project, scene_id)
        self.assertEqual(first, second)
        self.assertEqual(first["prompt_relay_request_version"], 1)
        self.assertEqual(first["request_fingerprint"], compile_scene_prompt(project, scene_id)["source_fingerprint"])
        self.assertEqual(first["generation_method"], "reference2video")
        self.assertNotIn(project["project_id"], json.dumps(first))
        self.assertNotIn(project["characters"][0]["character_id"], json.dumps(first))
        self.assertNotIn("current_deterministic_prompt", first)
        self.assertNotIn("creative_components", first)
        self.assertNotIn("subject_definitions:", json.dumps(first))
        self.assertNotIn("retention_analysis:", json.dumps(first))
        self.assertNotIn("detailed_description:", json.dumps(first))
        self.assertNotIn("overall_soundscape:", json.dumps(first))
        self.assertNotIn("non_diegetic_music:", json.dumps(first))
        self.assertNotIn("stored_name", json.dumps(first))
        self.assertNotIn("original_name", json.dumps(first))
        expected_mapping = [
            {
                "picture_number": picture["picture_number"],
                "subject_tag": picture["subject_tag"],
                "entity_name": picture["entity_name"],
            }
            for picture in build_ref2va_mapping(project, scene_id)["pictures"]
        ]
        self.assertEqual(first["reference_context"]["picture_mapping_summary"], expected_mapping)
        self.assertEqual(first["subjects"][0]["subject_tag"], "<Subject 1>")
        self.assertEqual(first["subjects"][1]["subject_tag"], "<Subject 2>")
        self.assertTrue(first["reference_context"]["subject_ownership_is_machine_owned"])
        self.assertTrue(first["constraints"]["may_use_supplied_subject_tags_for_reference2video"])
        self.assertIn("350-500 English words", first["constraints"]["detail_guidance"])

        keyframe_request = build_prompt_relay_request(
            project,
            scene_id,
            "keyframe_i2v",
        )
        self.assertEqual(keyframe_request["generation_method"], "keyframe_i2v")
        self.assertIn("actual_keyframe_description", keyframe_request["method_specific"])
        self.assertNotIn("opening_reference", keyframe_request["method_specific"])
        self.assertNotIn("<Picture 1>", json.dumps(keyframe_request))
        self.assertNotIn("integrated_multimodal_description:", json.dumps(keyframe_request))
        self.assertNotIn("<Subject", json.dumps(keyframe_request))
        self.assertNotIn("<Audio", json.dumps(keyframe_request))
        changed_project = {
            **project,
            "storyboard": {
                **project["storyboard"],
                "scenes": [{
                    **project["storyboard"]["scenes"][0],
                    "action": "Vesper turns toward the lens.",
                }],
            },
        }
        changed = build_prompt_relay_request(changed_project, scene_id)
        self.assertNotEqual(first["request_fingerprint"], changed["request_fingerprint"])
        self.assertNotEqual(first, changed)
        renamed_project = {**project, "name": "Relay payload rename only"}
        self.assertEqual(first, build_prompt_relay_request(renamed_project, scene_id))

    def test_ref2va_relay_accepts_supplied_subject_tags_and_reassembles_shot_once(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": project["locations"][0]["location_id"], "reference_id": location_refs[0]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        result = compile_scene_prompt(project, scene_id)
        enhanced = (
            "<Subject 1> holds a measured performance position within <Subject 2>, "
            "with restrained movement, warm side light, and a slow lateral camera drift."
        )
        response = validate_prompt_relay_response(result, {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": result["source_fingerprint"],
            "enhanced_description": enhanced,
        })
        prompt = response["enhanced_prompt"]
        self.assertIn(f"\n\n[Shot 1] {enhanced}", prompt)
        detailed = prompt.split("detailed_description:\n", 1)[1].split("\n\noverall_soundscape:", 1)[0]
        self.assertEqual(detailed.count("[Shot 1]"), 1)
        self.assertNotIn("[Shot 2]", prompt)
        self.assertNotIn("Opening reference state:", prompt)
        self.assertNotIn("Forward-generation intent follows", prompt)
        self.assertEqual(
            [prompt.index(f"{section}:") for section in (
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            )],
            sorted(prompt.index(f"{section}:") for section in (
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            )),
        )
        for description in (
            "<Subject 3> is not supplied.",
            "<Picture 1> appears here.",
            "<Audio 1> is heard.",
            "<Video 1> appears here.",
            "<Location 1> appears here.",
            "[Shot 1] duplicate wrapper.",
            "retention_analysis: fully_preserved",
        ):
            with self.subTest(description=description):
                with self.assertRaises(ProjectValidationError):
                    validate_relay_description(result, description)

    def test_i2v_relay_accepts_plain_prose_and_rejects_all_machine_wrapper_content(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        valid = "A restrained camera drift follows the accepted opening into the warm practical light."
        self.assertIn(valid, validate_relay_description(result, valid))
        for description in (
            "<Subject 1> moves.",
            "<Picture 1> moves.",
            "<Audio 1> moves.",
            "[Shot 1] moves.",
            "integrated_multimodal_description: [Shot 1] moves.",
        ):
            with self.subTest(description=description):
                with self.assertRaises(ProjectValidationError):
                    validate_relay_description(result, description)

    def test_custom_gpt_response_is_strict_and_machine_reassembled(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": project["locations"][0]["location_id"], "reference_id": location_refs[0]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        result = compile_scene_prompt(project, scene_id)
        before = (self.storage.project_directory(project["project_id"]) / "project.json").read_bytes()
        response = {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": result["source_fingerprint"],
            "enhanced_description": "The camera drifts through the warm chapel while the performer moves with measured intensity.",
        }
        validated = validate_prompt_relay_response(result, response)
        self.assertEqual(validated["status"], "validated")
        self.assertIn("subject_definitions:", validated["enhanced_prompt"])
        self.assertIn("summary:", validated["enhanced_prompt"])
        self.assertIn("retention_analysis:", validated["enhanced_prompt"])
        self.assertIn("<Subject 1> is Vesper", validated["enhanced_prompt"])
        self.assertIn("<Subject 2> is Chapel", validated["enhanced_prompt"])
        self.assertIn("<Audio 1>: fully_copy -", validated["enhanced_prompt"])
        self.assertIn("continuous 4.000-second performance shot", validated["enhanced_prompt"])
        self.assertIn(response["enhanced_description"], validated["enhanced_prompt"])
        after = (self.storage.project_directory(project["project_id"]) / "project.json").read_bytes()
        self.assertEqual(before, after)

    def test_custom_gpt_response_rejects_malformed_stale_and_structural_content(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        base = {
            "prompt_relay_response_version": 1,
            "scene_id": scene_id,
            "request_fingerprint": result["source_fingerprint"],
            "enhanced_description": "A measured camera drift follows the performer.",
        }
        for invalid in (
            "not JSON",
            {**base, "prompt_relay_response_version": 2},
            {**base, "scene_id": str(uuid.uuid4())},
            {**base, "request_fingerprint": "0" * 64},
            {key: value for key, value in base.items() if key != "enhanced_description"},
            {**base, "enhanced_description": ""},
            {**base, "extra": True},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_prompt_relay_response(result, invalid)

        for description in (
            "<Picture 1>",
            "<Subject 1>",
            "<Audio 1>",
            "<Location 1>",
            "[Shot 2] cut to a new angle.",
            "<d>spoken words</d>",
            "<Subject 1> fully_preserved; keep this relationship.",
            "Change the timing to 10 seconds.",
        ):
            with self.subTest(description=description):
                with self.assertRaises(ProjectValidationError):
                    validate_relay_description(result, description)

    def test_preview_is_read_only_and_route_contract_is_present(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        before = (self.storage.project_directory(project["project_id"]) / "project.json").read_bytes()
        result = compile_scene_prompt(self.storage.load_project(project["project_id"]), scene_id)
        after = (self.storage.project_directory(project["project_id"]) / "project.json").read_bytes()
        self.assertEqual(result["scene_id"], scene_id)
        self.assertEqual(before, after)
        routes = Path(__file__).parents[1].joinpath("backend", "routes.py").read_text(encoding="utf-8")
        self.assertIn('/music-video-builder/requirements', routes)
        self.assertIn('await asyncio.to_thread(build_requirements_report)', routes)
        self.assertIn('/music-video-builder/projects/{project_id}/scenes/{scene_id}/prompt/preview', routes)

    def test_phase7_keeps_schema_v7_with_additive_phase8_render_surface(self):
        self.assertEqual(production_manifest_registry(), WORKFLOW_MANIFESTS)
        project = self.storage.create_project("Schema check")
        self.assertEqual(project["schema_version"], 7)
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertIn("/prompt", extension)
        self.assertIn('data-mvb-view="prompts"', extension)
        self.assertIn("render/preflight", extension)


if __name__ == "__main__":
    unittest.main()
