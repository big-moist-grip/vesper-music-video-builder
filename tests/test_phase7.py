import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.prompt_service import (
    build_immutable_structural_facts,
    build_ref2va_mapping,
    compile_scene_prompt,
    enhance_prompt_with_ollama,
    validate_enhanced_prompt,
)
from backend.projects import ProjectStorage, ProjectValidationError
from backend.requirements import (
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_UNKNOWN,
    OLLAMA_MODEL,
    probe_ollama,
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


class _FakeResponse:
    def __init__(self, document):
        self.document = document
        self.closed = False

    def read(self):
        return json.dumps(self.document).encode("utf-8")

    def close(self):
        self.closed = True


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
            ollama_probe=lambda: {"status": STATUS_MISSING, "model": OLLAMA_MODEL},
        )
        report_two = scan_requirements(
            node_types=node_types,
            folder_paths_module=_FakeFolderPaths,
            binary_finder=lambda name: f"C:/tools/{name}.exe",
            ollama_probe=lambda: {"status": STATUS_MISSING, "model": OLLAMA_MODEL},
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
            ollama_probe=lambda: {"status": STATUS_MISSING, "model": OLLAMA_MODEL},
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
            ollama_probe=lambda: {"status": STATUS_MISSING, "model": OLLAMA_MODEL},
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
                ollama_probe=lambda: {"status": STATUS_MISSING, "model": OLLAMA_MODEL},
            )
        self.assertEqual(report["methods"]["keyframe_i2v"]["required_nodes"][0]["status"], STATUS_UNKNOWN)

    def test_ollama_probe_is_loopback_only_and_reports_model(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return _FakeResponse({"models": [{"name": OLLAMA_MODEL}]})

        report = probe_ollama(opener=opener)
        self.assertEqual(report["status"], STATUS_AVAILABLE)
        self.assertEqual(report["model_status"], STATUS_AVAILABLE)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/api/tags"))
        blocked = probe_ollama("https://example.invalid", opener=opener)
        self.assertEqual(blocked["status"], STATUS_UNKNOWN)
        self.assertEqual(len(calls), 1)

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
        self.assertNotIn("<Picture", prompt)
        self.assertNotIn("<Subject", prompt)
        self.assertNotIn("<Audio", prompt)
        self.assertNotIn(scene_id, prompt)
        self.assertNotIn(character_id, prompt)
        self.assertNotIn(location_id, prompt)
        self.assertNotIn(asset_id, prompt)
        self.assertNotIn("source_cue_numbers", prompt)
        self.assertNotIn("accepted.png", prompt)
        self.assertIn("Opening state is defined exclusively by the accepted keyframe", prompt)
        self.assertIn("Continuity context for identity and preservation", prompt)
        self.assertIn("Forward-generation intent applies after the opening state", prompt)
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
        self.assertTrue(all(section + ":" in prompt for section in ("subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music")))
        self.assertLess(prompt.index("subject_definitions:"), prompt.index("summary:"))
        self.assertLess(prompt.index("summary:"), prompt.index("retention_analysis:"))
        self.assertIn("<Picture 1>", prompt)
        self.assertIn("<Subject 1>", prompt)
        self.assertIn("<Subject 2>", prompt)
        self.assertIn("<Audio 1>", prompt)
        self.assertNotIn("<Location", prompt)
        self.assertIn("The lyric remains verbatim.", prompt)
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
        self.assertIn("The lyric remains verbatim.", performance_prompt)
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

    def test_bounded_enhancement_keeps_guard_input_only_and_reassembles(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)
        guard = build_immutable_structural_facts(result)
        self.assertIn('"exact_duration_ms":4000', guard)
        self.assertNotIn(guard, result["deterministic_prompt"])
        captured = {}

        def opener(request, _timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(
                {
                    "response": json.dumps(
                        {"enhanced_description": "The performer turns toward the warm light."}
                    )
                }
            )

        enhanced = enhance_prompt_with_ollama(result, opener=opener)
        self.assertFalse(enhanced["used_fallback"], enhanced)
        self.assertEqual(enhanced["enhanced_description"], "The performer turns toward the warm light.")
        self.assertIn(guard, captured["payload"]["prompt"])
        self.assertEqual(captured["payload"]["format"], "json")
        self.assertNotIn(guard, enhanced["prompt"])
        self.assertIn("Duration: 4.000-second continuous shot.", enhanced["prompt"])
        self.assertIn("The performer turns toward the warm light.", enhanced["prompt"])
        self.assertNotIn("subject_definitions:", enhanced["prompt"])

    def test_bounded_enhancement_rejects_tags_shots_dialogue_and_duration(self):
        project, _, _ = self._project()
        result = compile_scene_prompt(project, project["scenes"][0]["scene_id"])
        for description in (
            "<Picture 1>",
            "<Subject 1>",
            "<Audio 1>",
            "<Location 1>",
            "[Shot 2]",
            "<d>spoken words</d>",
            "Change the timing to 10 seconds.",
        ):
            with self.subTest(description=description):
                with self.assertRaises(ProjectValidationError):
                    validate_enhanced_prompt(result, {"enhanced_description": description})

    def test_malformed_bounded_response_falls_back_without_repair(self):
        project, _, _ = self._project()
        result = compile_scene_prompt(project, project["scenes"][0]["scene_id"])

        response = enhance_prompt_with_ollama(
            result,
            opener=lambda _request, _timeout: _FakeResponse({"response": "not JSON"}),
        )
        self.assertTrue(response["used_fallback"])
        self.assertEqual(response["prompt"], result["deterministic_prompt"])

    def test_ollama_invalid_response_falls_back_without_repair(self):
        project, _, _ = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        result = compile_scene_prompt(project, scene_id)

        def opener(_request, _timeout):
            return _FakeResponse({"response": "corrupted output"})

        enhanced = enhance_prompt_with_ollama(result, opener=opener)
        self.assertTrue(enhanced["used_fallback"])
        self.assertEqual(enhanced["prompt"], result["deterministic_prompt"])

    def test_ref2va_enhancement_reassembles_machine_sections_and_rejects_unknown_tags(self):
        project, character_refs, location_refs = self._project()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        selected = [
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_refs[0]["reference_id"]},
            {"entity_type": "location", "entity_id": project["locations"][0]["location_id"], "reference_id": location_refs[0]["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        result = compile_scene_prompt(project, scene_id)
        structural = result["deterministic_prompt"]
        valid = enhance_prompt_with_ollama(
            result,
            opener=lambda _request, _timeout: _FakeResponse(
                {
                    "response": json.dumps(
                        {
                            "enhanced_description": (
                                "The camera drifts through the warm chapel while the performer "
                                "moves with measured intensity."
                            )
                        }
                    )
                }
            ),
        )
        self.assertFalse(valid["used_fallback"], valid)
        self.assertNotIn(build_immutable_structural_facts(result), valid["prompt"])
        self.assertEqual(valid["prompt"].count("subject_definitions:"), 1)
        self.assertEqual(valid["prompt"].count("summary:"), 1)
        self.assertLess(valid["prompt"].index("subject_definitions:"), valid["prompt"].index("summary:"))
        self.assertLess(valid["prompt"].index("summary:"), valid["prompt"].index("retention_analysis:"))
        self.assertIn("<Subject 1> is Vesper", valid["prompt"])
        self.assertIn("<Subject 2> is Chapel", valid["prompt"])
        self.assertIn("<Audio 1> — fully_copy", valid["prompt"])
        self.assertIn("lasting exactly 4.000 seconds", valid["prompt"])
        self.assertEqual(structural.split("detailed_description:", 1)[0], valid["prompt"].split("detailed_description:", 1)[0])

        for description in ("<Picture 99>", "<Subject 99>", "<Audio 2>", "<Location 1>"):
            invalid = enhance_prompt_with_ollama(
                result,
                opener=lambda _request, _timeout, description=description: _FakeResponse(
                    {"response": json.dumps({"enhanced_description": description})}
                ),
            )
            with self.subTest(description=description):
                self.assertTrue(invalid["used_fallback"])

    def test_custom_model_is_reported_by_fallback(self):
        project, _, _ = self._project()
        result = compile_scene_prompt(project, project["scenes"][0]["scene_id"])
        response = enhance_prompt_with_ollama(
            result,
            model="custom-local-model",
            opener=lambda _request, _timeout: _FakeResponse({"response": "malformed"}),
        )
        self.assertTrue(response["used_fallback"])
        self.assertEqual(response["model"], "custom-local-model")

    def test_ollama_timeout_falls_back_to_deterministic_prompt(self):
        project, _, _ = self._project()
        result = compile_scene_prompt(project, project["scenes"][0]["scene_id"])

        def timeout_opener(_request, timeout):
            raise TimeoutError()

        response = enhance_prompt_with_ollama(result, opener=timeout_opener)
        self.assertTrue(response["used_fallback"])
        self.assertEqual(response["prompt"], result["deterministic_prompt"])

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

    def test_phase7_does_not_change_schema_or_add_phase8_surface(self):
        self.assertEqual(production_manifest_registry(), WORKFLOW_MANIFESTS)
        project = self.storage.create_project("Schema check")
        self.assertEqual(project["schema_version"], 5)
        extension = Path(__file__).parents[1].joinpath("web", "extension.js").read_text(encoding="utf-8")
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertNotIn("/prompt", extension)
        self.assertNotIn("/render", extension)


if __name__ == "__main__":
    unittest.main()
