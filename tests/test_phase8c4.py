import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_phase8 import Phase8TestCase

from backend.projects import ProjectStorage
from backend.render import (
    PREPARATION_VERSION,
    MediaPreparationError,
    _copy_runtime_input_atomic,
    _queue_input_name,
    _resolve_comfyui_input_directory,
    _runtime_input_path,
    build_h3_timing_plan,
    compile_production_workflow,
    prepare_render_scene,
)
from backend.render_jobs import (
    LIVE_INPUT_NOT_DETERMINABLE,
    LIVE_INPUT_SUPPORTED,
    JobStore,
    RenderExecutionBlocked,
    submit_render_job,
    validate_live_workflow_compatibility,
)
from backend.workflows import production_manifest_registry


def loadimage_definition(options, *, upload=True):
    settings = {"image_upload": True} if upload else {}
    return {"input": {"required": {"image": [list(options), settings]}}}


def loadimage_workflow(selector, *, node_id="50"):
    return {node_id: {"class_type": "LoadImage", "inputs": {"image": selector}}}


class ContractClient:
    def __init__(self, definition):
        self.definition = definition
        self.info_calls = 0
        self.submit_calls = 0

    def get_object_info(self, class_type):
        self.info_calls += 1
        if class_type == "VHS_VideoCombine":
            return {"input": {"required": {
                "filename_prefix": ["STRING", {}],
                "format": [["video/h264-mp4"], {}],
            }}}
        if class_type != "LoadImage":
            raise AssertionError(f"Unexpected class {class_type}")
        return self.definition

    def submit_prompt(self, _workflow):
        self.submit_calls += 1
        return {"prompt_id": str(uuid.uuid4()), "queue_number": 1, "node_errors": {}}


class Phase8C4LoadImageContractTests(unittest.TestCase):
    nested_selector = "music_video_builder/project/scene/picture_01.png"

    def test_installed_style_loadimage_schema_is_an_upload_discovery_list(self):
        definition = loadimage_definition(["Vesper.png", "Chapel.png"])
        image_definition = definition["input"]["required"]["image"]
        self.assertEqual(image_definition[0], ["Vesper.png", "Chapel.png"])
        self.assertIs(image_definition[1]["image_upload"], True)

    def test_root_level_selector_in_discovery_list_is_supported(self):
        result = validate_live_workflow_compatibility(
            loadimage_workflow("Vesper.png"),
            ContractClient(loadimage_definition(["Vesper.png"])),
        )
        self.assertEqual(result["assessment_counts"][LIVE_INPUT_SUPPORTED], 1)
        self.assertEqual(result["not_determinable"], [])

    def test_recursive_runtime_listing_can_definitively_support_nested_selector(self):
        result = validate_live_workflow_compatibility(
            loadimage_workflow(self.nested_selector),
            ContractClient(loadimage_definition([self.nested_selector])),
        )
        self.assertTrue(result["compatible"])
        self.assertEqual(result["assessment_counts"][LIVE_INPUT_SUPPORTED], 1)

    def test_root_only_upload_discovery_does_not_falsely_reject_nested_selector(self):
        result = validate_live_workflow_compatibility(
            loadimage_workflow(self.nested_selector),
            ContractClient(loadimage_definition(["Vesper.png"])),
        )
        self.assertTrue(result["compatible"])
        self.assertEqual(result["assessment_counts"][LIVE_INPUT_NOT_DETERMINABLE], 1)
        self.assertEqual(result["not_determinable"][0]["input_name"], "image")
        self.assertIn("upload selector", result["not_determinable"][0]["reason"])
        self.assertEqual(result["final_authority"], "comfyui_prompt_validation")

    def test_closed_enum_still_rejects_nested_selector(self):
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(
                loadimage_workflow(self.nested_selector),
                ContractClient(loadimage_definition(["Vesper.png"], upload=False)),
            )
        self.assertIn("image", context.exception.message)
        self.assertIn("not accepted", context.exception.message)

    def test_file_existence_cannot_override_definitive_closed_enum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "picture_01.png"
            path.write_bytes(b"exists")
            self.assertTrue(path.is_file())
            with self.assertRaises(RenderExecutionBlocked):
                validate_live_workflow_compatibility(
                    loadimage_workflow(str(path)),
                    ContractClient(loadimage_definition(["Vesper.png"], upload=False)),
                )

    def test_model_and_other_genuine_enums_remain_fail_closed(self):
        definition = {"input": {"required": {"model_name": [["current.safetensors"]]}}}
        workflow = {"1": {"class_type": "ModelNode", "inputs": {"model_name": "old.safetensors"}}}
        client = ContractClient(definition)
        client.get_object_info = lambda _class_type: definition
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(workflow, client)
        self.assertIn("old.safetensors", context.exception.message)


class Phase8C4MaterializationTests(unittest.TestCase):
    setUp = Phase8TestCase.setUp
    _reference = staticmethod(Phase8TestCase._reference)
    _requirements = Phase8TestCase._requirements
    _project = Phase8TestCase._project
    _fake_media_adapter = Phase8TestCase._fake_media_adapter

    def _prepare(self, method="reference2video"):
        project = self._project(method=method)
        adapter, _calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
            comfyui_input_directory=self.comfyui_input_directory,
        )
        return project, result

    def test_preparation_v3_materializes_visual_inside_active_input_root(self):
        project, result = self._prepare()
        visual = result["preparation"]["visual_inputs"][0]
        destination = self.comfyui_input_directory.joinpath(*visual["queue_name"].split("/"))
        self.assertEqual(result["preparation"]["preparation_version"], 3)
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), b"reference still")
        self.assertIn(project["project_id"], visual["queue_name"])

    def test_selector_is_deterministic_project_scene_scoped_and_traceable(self):
        project, first = self._prepare()
        adapter, _calls = self._fake_media_adapter()
        second = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
            comfyui_input_directory=self.comfyui_input_directory,
        )
        self.assertTrue(second["reused"])
        self.assertEqual(
            first["preparation"]["visual_inputs"][0]["queue_name"],
            second["preparation"]["visual_inputs"][0]["queue_name"],
        )

    def test_different_project_scene_and_picture_indices_do_not_collide(self):
        project_a = str(uuid.uuid4())
        project_b = str(uuid.uuid4())
        scene_a = str(uuid.uuid4())
        scene_b = str(uuid.uuid4())
        selectors = {
            _queue_input_name(project_a, scene_a, "picture_01.png"),
            _queue_input_name(project_b, scene_a, "picture_01.png"),
            _queue_input_name(project_a, scene_b, "picture_01.png"),
            _queue_input_name(project_a, scene_a, "picture_02.png"),
        }
        self.assertEqual(len(selectors), 4)

    def test_source_asset_is_unchanged_and_materialized_content_matches(self):
        project = self._project(method="reference2video")
        root = self.storage.project_directory(project["project_id"])
        visual = project["visuals"]["scenes"][0]["reference2video"]["selected_references"][0]
        character = next(item for item in project["characters"] if item["character_id"] == visual["entity_id"])
        reference = next(item for item in character["references"] if item["reference_id"] == visual["reference_id"])
        source = root / "references" / "characters" / visual["entity_id"] / reference["stored_name"]
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        adapter, _calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
            comfyui_input_directory=self.comfyui_input_directory,
        )
        selector = result["preparation"]["visual_inputs"][0]["queue_name"]
        staged = self.comfyui_input_directory.joinpath(*selector.split("/"))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertEqual(staged.read_bytes(), source.read_bytes())

    def test_runtime_path_rejects_traversal_and_wrong_namespace(self):
        project_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        invalid = (
            f"music_video_builder/{project_id}/{scene_id}/../evil.png",
            f"other/{project_id}/{scene_id}/picture_01.png",
            "C:/outside/picture_01.png",
        )
        for selector in invalid:
            with self.subTest(selector=selector):
                with self.assertRaises(MediaPreparationError):
                    _runtime_input_path(self.comfyui_input_directory, selector, project_id, scene_id)

    def test_incorrect_input_root_is_rejected(self):
        not_directory = Path(self.temp_directory.name) / "not-a-directory"
        not_directory.write_bytes(b"file")
        with self.assertRaises(MediaPreparationError):
            _resolve_comfyui_input_directory(not_directory)

    def test_runtime_copy_is_atomic_and_leaves_no_temporary_file(self):
        project_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        root = self.storage.project_directory(self.storage.create_project("atomic")["project_id"])
        source = root / "references" / "source.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"exact visual bytes")
        selector = _queue_input_name(project_id, scene_id, "picture_01.png")
        destination = _copy_runtime_input_atomic(
            source,
            selector,
            project_root=root,
            input_root=self.comfyui_input_directory,
            project_id=project_id,
            scene_id=scene_id,
        )
        self.assertEqual(destination.read_bytes(), b"exact visual bytes")
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])
        self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_four_ref2va_picture_selectors_preserve_order_and_mapping(self):
        project = self._project(method="reference2video")
        scene_id = project["scenes"][0]["scene_id"]
        project_id = project["project_id"]
        visual_inputs = [
            {
                "picture_number": number,
                "picture_tag": f"<Picture {number}>",
                "subject_tag": f"<Subject {1 if number < 3 else number - 1}>",
                "queue_name": _queue_input_name(project_id, scene_id, f"picture_{number:02d}.png"),
            }
            for number in range(1, 5)
        ]
        original = copy.deepcopy(visual_inputs)
        workflow, validation = compile_production_workflow(
            project,
            scene_id,
            final_prompt="A current four-picture production prompt.",
            timing_plan=build_h3_timing_plan(4_000),
            visual_inputs=visual_inputs,
            scene_audio_name=_queue_input_name(project_id, scene_id, "scene_audio.wav"),
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(visual_inputs, original)
        for number in range(1, 5):
            loader_id = str(49 + number)
            self.assertEqual(workflow[loader_id]["inputs"]["image"], visual_inputs[number - 1]["queue_name"])
            self.assertEqual(workflow["8"]["inputs"][f"ref_images.ref_image_{number - 1}"], [loader_id, 0])
        self.assertEqual(len({workflow[str(49 + number)]["inputs"]["image"] for number in range(1, 5)}), 4)

    def test_four_ref2va_execution_copies_are_complete_ordered_and_collision_free(self):
        project = self.storage.create_project("four runtime pictures")
        root = self.storage.project_directory(project["project_id"])
        scene_id = str(uuid.uuid4())
        selectors = []
        for number in range(1, 5):
            source = root / "references" / f"source_{number:02d}.png"
            source.parent.mkdir(parents=True, exist_ok=True)
            expected = f"picture {number} exact bytes".encode("utf-8")
            source.write_bytes(expected)
            selector = _queue_input_name(project["project_id"], scene_id, f"picture_{number:02d}.png")
            destination = _copy_runtime_input_atomic(
                source,
                selector,
                project_root=root,
                input_root=self.comfyui_input_directory,
                project_id=project["project_id"],
                scene_id=scene_id,
            )
            selectors.append(selector)
            self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(
            [selector.rsplit("/", 1)[-1] for selector in selectors],
            ["picture_01.png", "picture_02.png", "picture_03.png", "picture_04.png"],
        )
        self.assertEqual(len(set(selectors)), 4)

    def test_i2v_keyframe_uses_same_nested_runtime_materialization_policy(self):
        project, result = self._prepare(method="keyframe_i2v")
        metadata = result["preparation"]
        self.assertEqual(len(metadata["visual_inputs"]), 1)
        visual = metadata["visual_inputs"][0]
        self.assertEqual(visual["role"], "keyframe")
        self.assertEqual(visual["picture_number"], 1)
        self.assertTrue(self.comfyui_input_directory.joinpath(*visual["queue_name"].split("/")).is_file())
        workflow_path = self.storage.project_directory(project["project_id"]) / metadata["workflow"]["relative_path"]
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        mapping = production_manifest_registry()["keyframe_i2v"]["inputs"]["keyframe"]
        self.assertEqual(workflow[mapping["node_id"]]["inputs"][mapping["input"]], visual["queue_name"])

    def test_audio_selector_contract_and_authoritative_source_remain_unchanged(self):
        project = self._project(method="reference2video")
        root = self.storage.project_directory(project["project_id"])
        source = root / "source" / "master_audio.wav"
        before = source.read_bytes()
        adapter, _calls = self._fake_media_adapter()
        result = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
            comfyui_input_directory=self.comfyui_input_directory,
        )
        audio = result["preparation"]["source_audio"]
        self.assertTrue(audio["queue_name"].endswith("/scene_audio.wav"))
        self.assertTrue(self.comfyui_input_directory.joinpath(*audio["queue_name"].split("/")).is_file())
        self.assertEqual(source.read_bytes(), before)

    def test_validator_only_fix_keeps_preparation_v3_and_existing_nested_package_current(self):
        project, first = self._prepare()
        adapter, _calls = self._fake_media_adapter()
        second = prepare_render_scene(
            self.storage,
            project["project_id"],
            project["scenes"][0]["scene_id"],
            requirements_report=self._requirements(),
            media_adapter=adapter,
            comfyui_input_directory=self.comfyui_input_directory,
        )
        self.assertEqual(PREPARATION_VERSION, 3)
        self.assertTrue(second["reused"])
        self.assertEqual(first["preparation_fingerprint"], second["preparation_fingerprint"])


class Phase8C4QueueBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8C.4 queue boundary")
        self.project_id = project["project_id"]
        self.scene_id = str(uuid.uuid4())
        self.selector = _queue_input_name(self.project_id, self.scene_id, "picture_01.png")
        self.fingerprint = "f" * 64
        self.preflight = {
            "project_id": self.project_id,
            "scenes": [{
                "scene_id": self.scene_id,
                "generation_method": "keyframe_i2v",
                "content_ready": True,
                "workflow_ready": True,
                "runtime_requirements_ready": True,
                "preparation_current": True,
                "preparation_fingerprint": self.fingerprint,
            }],
        }
        self.package = {
            "workflow": {
                **loadimage_workflow(self.selector, node_id="5"),
                "20": {"class_type": "VHS_VideoCombine", "inputs": {
                    "filename_prefix": "music_video_builder/render",
                    "format": "video/h264-mp4",
                }},
            },
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": self.fingerprint,
            "output_node_id": "20",
            "expected_filename_prefix": "music_video_builder/render",
            "output_format": "video/h264-mp4",
        }

    def test_current_nested_preparation_reaches_mock_queue_adapter(self):
        client = ContractClient(loadimage_definition(["Vesper.png"]))
        job = submit_render_job(
            self.storage,
            self.project_id,
            self.scene_id,
            client=client,
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
        )
        self.assertEqual(job["state"], "QUEUED")
        self.assertEqual(client.info_calls, 2)
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(job["runtime_compatibility"]["assessment_counts"][LIVE_INPUT_NOT_DETERMINABLE], 1)

    def test_definitive_closed_selector_failure_creates_no_phantom_job(self):
        client = ContractClient(loadimage_definition(["Vesper.png"], upload=False))
        with self.assertRaises(RenderExecutionBlocked):
            submit_render_job(
                self.storage,
                self.project_id,
                self.scene_id,
                client=client,
                preflight=self.preflight,
                package_loader=lambda *_args: self.package,
            )
        self.assertEqual(client.submit_calls, 0)
        self.assertEqual(JobStore(self.storage).list_scene(self.project_id, self.scene_id), [])


del Phase8TestCase


if __name__ == "__main__":
    unittest.main()
