import json
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path

from backend.projects import ProjectStorage, ProjectValidationError, default_prompts_for_scenes
from backend.scenes import build_scenes
from backend.visuals import (
    MAX_REF2VA_STILL_REFERENCES,
    default_visuals_for_scenes,
    derive_visual_readiness,
    save_reference_selection,
    set_generation_method,
)
from backend.workflows import (
    WORKFLOW_DIRECTORY,
    _validate_loaded_workflow,
    patch_reference_picture_slots,
    production_manifest_registry,
    validate_production_workflow_contract,
)


class Phase6ContractTestCase(unittest.TestCase):
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

    def _project_with_references(self, count=10, scene_count=1):
        project = self.storage.create_project("Capacity Project")
        scenes = build_scenes(
            [
                {
                    "cue_number": index + 1,
                    "start_ms": index * 4_000,
                    "end_ms": (index + 1) * 4_000,
                    "text": f"Lyric {index + 1}",
                }
                for index in range(scene_count)
            ],
            scene_count * 4_000,
        )
        references = [self._reference(f"reference-{index}.png") for index in range(count)]
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
                        "cue_count": 1,
                    },
                },
                "scenes": scenes,
                "characters": [
                    {
                        "character_id": str(uuid.uuid4()),
                        "name": "Vesper",
                        "role": "performer",
                        "appearance": "Copper hair.",
                        "outfit": "Black jacket.",
                        "references": references,
                    }
                ],
                "visuals": default_visuals_for_scenes(scenes),
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )
        selectors = [
            {
                "entity_type": "character",
                "entity_id": project["characters"][0]["character_id"],
                "reference_id": reference["reference_id"],
            }
            for reference in references
        ]
        return project, selectors

    def test_registry_has_exact_supported_methods_and_contracts_validate(self):
        registry = production_manifest_registry()
        self.assertEqual(set(registry), {"keyframe_i2v", "reference2video"})
        validated = validate_production_workflow_contract()
        self.assertEqual(set(validated), set(registry))
        self.assertEqual(validated["keyframe_i2v"]["node_count"], 20)
        self.assertEqual(validated["reference2video"]["node_count"], 28)
        self.assertEqual(validated["reference2video"]["ref2va_capacity"], 9)

    def test_templates_parse_and_are_immutable_under_manifest_load(self):
        registry = production_manifest_registry()
        original = json.dumps(registry, sort_keys=True)
        registry["reference2video"]["inputs"]["pictures"]["max_count"] = 1
        self.assertEqual(json.dumps(production_manifest_registry(), sort_keys=True), original)
        for manifest in production_manifest_registry().values():
            path = WORKFLOW_DIRECTORY / manifest["workflow_file"]
            self.assertTrue(path.is_file())
            parsed = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(parsed, dict)
            self.assertTrue(parsed)

    def test_required_model_categories_and_filenames_are_declarative(self):
        for method, manifest in production_manifest_registry().items():
            self.assertEqual(manifest["generation_method"], method)
            for model in manifest["required_models"]:
                self.assertIn(model["category"], {"diffusion_models", "text_encoders", "vae"})
                self.assertNotIn("\\", model["filename"])
                self.assertNotIn("/", model["filename"])
                self.assertFalse(Path(model["filename"]).is_absolute())
        reference_models = [model["filename"] for model in production_manifest_registry()["reference2video"]["required_models"]]
        self.assertNotIn("minimax_h3_ref2va_pruned_w4a8_mixed.safetensors", reference_models)

    def test_forbidden_generation_branches_and_acceleration_are_absent(self):
        validated = validate_production_workflow_contract()
        self.assertEqual(set(validated), {"keyframe_i2v", "reference2video"})
        serialized = "".join(
            (WORKFLOW_DIRECTORY / manifest["workflow_file"]).read_text(encoding="utf-8").lower()
            for manifest in production_manifest_registry().values()
        )
        for forbidden in (
            "t2v",
            "ref_video",
            "motion-transfer",
            "rife",
            "ltx refine",
            "ollama",
            "spectrum",
            "easycache",
            "sage",
            "sol attention",
            "lora",
            "rgthree",
            "easyuse",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_ref2va_picture_slots_support_one_through_installed_capacity(self):
        workflow = json.loads(
            (WORKFLOW_DIRECTORY / "h3_music_video_ref2va_api.json").read_text(encoding="utf-8")
        )
        original = json.dumps(workflow, sort_keys=True)
        self.assertEqual(MAX_REF2VA_STILL_REFERENCES, 9)
        for count in range(1, MAX_REF2VA_STILL_REFERENCES + 1):
            patched = patch_reference_picture_slots(workflow, [f"reference-{index}.png" for index in range(count)])
            picture_inputs = [key for key in patched["8"]["inputs"] if key.startswith("ref_images.ref_image_")]
            self.assertEqual(picture_inputs, [f"ref_images.ref_image_{index}" for index in range(count)])
            self.assertEqual(
                [patched[str(50 + index)]["inputs"]["image"] for index in range(count)],
                [f"reference-{index}.png" for index in range(count)],
            )
            self.assertEqual(
                sorted(
                    node_id
                    for node_id, node in patched.items()
                    if node.get("class_type") == "LoadImage"
                ),
                [str(50 + index) for index in range(count)],
            )
        self.assertEqual(json.dumps(workflow, sort_keys=True), original)
        with self.assertRaises(ProjectValidationError):
            patch_reference_picture_slots(workflow, [])
        with self.assertRaises(ProjectValidationError):
            patch_reference_picture_slots(workflow, [f"reference-{index}.png" for index in range(10)])

    def test_phase5_reference_selection_enforces_capacity_and_preserves_over_capacity_legacy_state(self):
        project, selectors = self._project_with_references()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        with self.assertRaises(ProjectValidationError):
            save_reference_selection(
                self.storage,
                project["project_id"],
                scene_id,
                {"selected_references": selectors},
            )

        valid = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": selectors[:MAX_REF2VA_STILL_REFERENCES]},
        )
        self.assertEqual(
            len(valid["visuals"]["scenes"][0]["reference2video"]["selected_references"]),
            MAX_REF2VA_STILL_REFERENCES,
        )

        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        raw = json.loads(project_file.read_text(encoding="utf-8"))
        raw["visuals"]["scenes"][0]["reference2video"]["selected_references"] = selectors
        project_file.write_text(json.dumps(raw), encoding="utf-8")
        loaded = self.storage.load_project(project["project_id"])
        self.assertEqual(len(loaded["visuals"]["scenes"][0]["reference2video"]["selected_references"]), 10)
        readiness = derive_visual_readiness(loaded, scene_id, self.storage)
        self.assertFalse(readiness["ready"])
        self.assertTrue(any("at most 9" in message for message in readiness["missing"]))
        saved = self.storage.save_project(loaded["project_id"], loaded, allow_visuals_change=True)
        self.assertEqual(len(saved["visuals"]["scenes"][0]["reference2video"]["selected_references"]), 10)

    def test_legacy_over_capacity_is_preserved_and_corrected_per_scene(self):
        project, selectors = self._project_with_references(scene_count=2)
        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        raw = json.loads(project_file.read_text(encoding="utf-8"))
        for visual_scene in raw["visuals"]["scenes"]:
            visual_scene["generation_method"] = "reference2video"
            visual_scene["reference2video"]["selected_references"] = selectors
        project_file.write_text(json.dumps(raw), encoding="utf-8")

        loaded = self.storage.load_project(project["project_id"])
        unchanged = self.storage.save_project(
            loaded["project_id"],
            {**loaded, "name": "Unrelated save"},
            allow_visuals_change=True,
        )
        self.assertEqual(
            [
                len(scene["reference2video"]["selected_references"])
                for scene in unchanged["visuals"]["scenes"]
            ],
            [10, 10],
        )

        first_scene_id = unchanged["visuals"]["scenes"][0]["scene_id"]
        second_scene_id = unchanged["visuals"]["scenes"][1]["scene_id"]
        corrected = deepcopy(unchanged)
        corrected["visuals"]["scenes"][0]["reference2video"]["selected_references"] = selectors[:9]
        saved = self.storage.save_project(
            unchanged["project_id"],
            corrected,
            allow_visuals_change=True,
        )
        self.assertEqual(saved["visuals"]["scenes"][0]["scene_id"], first_scene_id)
        self.assertEqual(saved["visuals"]["scenes"][1]["scene_id"], second_scene_id)
        self.assertEqual(
            [
                len(scene["reference2video"]["selected_references"])
                for scene in saved["visuals"]["scenes"]
            ],
            [9, 10],
        )
        readiness = derive_visual_readiness(saved, second_scene_id, self.storage)
        self.assertFalse(readiness["ready"])
        self.assertTrue(any("at most 9" in message for message in readiness["missing"]))

        different_legacy = deepcopy(saved)
        different_legacy["visuals"]["scenes"][1]["reference2video"]["selected_references"] = list(reversed(selectors))
        with self.assertRaises(ProjectValidationError):
            self.storage.save_project(
                saved["project_id"],
                different_legacy,
                allow_visuals_change=True,
            )

        new_over_capacity = deepcopy(saved)
        new_over_capacity["visuals"]["scenes"][0]["reference2video"]["selected_references"] = selectors
        with self.assertRaises(ProjectValidationError):
            self.storage.save_project(
                saved["project_id"],
                new_over_capacity,
                allow_visuals_change=True,
            )

    def test_production_topology_rejects_corrupted_critical_edges(self):
        edge_cases = {
            "keyframe_i2v": (
                ("8", "av_latent", ["7", 1]),
                ("12", "audio_latent", ["11", 0]),
                ("17", "sigmas", ["15", 0]),
                ("20", "audio", ["19", 0]),
            ),
            "reference2video": (
                ("9", "av_latent", ["8", 1]),
                ("13", "audio_latent", ["12", 0]),
                ("18", "latent_image", ["13", 0]),
                ("21", "images", ["19", 0]),
                ("8", "ref_images.ref_image_0", ["50", 0]),
            ),
        }
        for method, cases in edge_cases.items():
            manifest = production_manifest_registry()[method]
            template = json.loads(
                (WORKFLOW_DIRECTORY / manifest["workflow_file"]).read_text(encoding="utf-8")
            )
            _validate_loaded_workflow(manifest, template)
            for node_id, input_name, expected_link in cases:
                corrupted = deepcopy(template)
                corrupted[node_id]["inputs"][input_name] = ["999", 0]
                with self.subTest(method=method, node_id=node_id, input_name=input_name):
                    with self.assertRaises(ProjectValidationError):
                        _validate_loaded_workflow(manifest, corrupted)

    def test_model_loader_types_and_declared_inputs_are_validated(self):
        for method, source_manifest in production_manifest_registry().items():
            template = json.loads(
                (WORKFLOW_DIRECTORY / source_manifest["workflow_file"]).read_text(encoding="utf-8")
            )
            for index, declaration in enumerate(source_manifest["required_models"]):
                manifest = deepcopy(source_manifest)
                manifest["required_models"][index]["node_type"] = "WrongLoader"
                with self.subTest(method=method, role=declaration["role"]):
                    with self.assertRaises(ProjectValidationError):
                        _validate_loaded_workflow(manifest, template)

    def test_manifest_sampler_settings_are_mapped_and_template_consistent(self):
        expected_mappings = {
            "sampler": ("sampler_name", "KSamplerSelect"),
            "scheduler": ("scheduler", "BasicScheduler"),
            "denoise": ("denoise", "BasicScheduler"),
        }
        for method, source_manifest in production_manifest_registry().items():
            template = json.loads(
                (WORKFLOW_DIRECTORY / source_manifest["workflow_file"]).read_text(encoding="utf-8")
            )
            for setting, (input_name, _) in expected_mappings.items():
                self.assertEqual(source_manifest["inputs"][input_name]["input"], input_name)
                self.assertEqual(
                    template[source_manifest["inputs"][input_name]["node_id"]]["class_type"],
                    expected_mappings[setting][1],
                )
            self.assertEqual(source_manifest["inputs"]["fps"]["input"], "frame_rate")
            self.assertEqual(source_manifest["provisional_settings"]["fps"], 24)
            _validate_loaded_workflow(source_manifest, template)

            mismatch = deepcopy(source_manifest)
            mismatch["provisional_settings"]["steps"] += 1
            with self.subTest(method=method):
                with self.assertRaises(ProjectValidationError):
                    _validate_loaded_workflow(mismatch, template)

    def test_known_forbidden_nodes_are_rejected(self):
        forbidden_types = (
            "MinimaxTextToVideoNode",
            "PathchSageAttentionKJ",
            "MiniMaxH3MemoryEfficientSageAttentionPatch",
            "SpectrumApplyMiniMaxH3",
            "RTXVideoSuperResolution",
            "SeedVR2Conditioning",
            "LoraLoader",
            "Any Switch (rgthree)",
            "EasyCache",
        )
        manifest = production_manifest_registry()["keyframe_i2v"]
        template = json.loads(
            (WORKFLOW_DIRECTORY / manifest["workflow_file"]).read_text(encoding="utf-8")
        )
        for index, class_type in enumerate(forbidden_types, start=100):
            corrupted = deepcopy(template)
            corrupted[str(index)] = {"class_type": class_type, "inputs": {}}
            with self.subTest(class_type=class_type):
                with self.assertRaises(ProjectValidationError):
                    _validate_loaded_workflow(manifest, corrupted)

    def test_frontend_capacity_guard_matches_installed_contract(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        self.assertIn("const MAX_REF2VA_STILL_REFERENCES = 9;", extension)
        self.assertIn("selected.length >= MAX_REF2VA_STILL_REFERENCES", extension)
        self.assertIn("selected.length > MAX_REF2VA_STILL_REFERENCES", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertNotIn("localStorage", extension)

    def test_schema_is_v6_with_prompt_persistence(self):
        project = self.storage.create_project("Schema v5 contract")
        self.assertEqual(project["schema_version"], 7)


if __name__ == "__main__":
    unittest.main()
