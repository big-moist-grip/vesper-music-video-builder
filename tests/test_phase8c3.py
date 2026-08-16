import json
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path

from backend.projects import ProjectStorage
from backend.render import PREPARATION_VERSION
from backend.render_jobs import (
    LIVE_INPUT_NOT_DETERMINABLE,
    LIVE_INPUT_SUPPORTED,
    ComfyUIClient,
    RenderExecutionBlocked,
    resolve_live_node_input_schema,
    submit_render_job,
    validate_live_workflow_compatibility,
)


ROOT = Path(__file__).parents[1]


def vhs_definition(*, nested=False):
    formats = {
        "video/h264-mp4": [
            ["pix_fmt", ["yuv420p", "yuv420p10le"]],
            ["crf", "INT", {"default": 19, "min": 0, "max": 100, "step": 1}],
            ["save_metadata", "BOOLEAN", {"default": True}],
            ["trim_to_audio", "BOOLEAN", {"default": False}],
        ],
        "video/h265-mp4": [
            ["pix_fmt", ["yuv420p10le", "yuv420p"]],
            ["crf", "INT", {"default": 19, "min": 0, "max": 100, "step": 1}],
            ["save_metadata", "BOOLEAN", {"default": True}],
        ],
        "video/av1-webm": [
            ["input_color_depth", ["8bit", "16bit"]],
            ["crf", "INT", {"default": 23, "min": 0, "max": 63, "step": 1}],
        ],
    }
    format_metadata = {"dynamic": {"formats": formats}} if nested else {"formats": formats}
    return {
        "input": {
            "required": {
                "images": ["IMAGE"],
                "frame_rate": ["FLOAT", {"default": 8.0, "min": 1.0}],
                "loop_count": ["INT", {"default": 0, "min": 0}],
                "filename_prefix": ["STRING"],
                "format": [["video/h264-mp4", "video/h265-mp4", "video/av1-webm"], format_metadata],
                "pingpong": ["BOOLEAN"],
                "save_output": ["BOOLEAN"],
            },
            "optional": {"audio": ["AUDIO"]},
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        },
    }


def production_node_21():
    workflow = json.loads((ROOT / "workflows" / "h3_music_video_ref2va_api.json").read_text(encoding="utf-8"))
    node = deepcopy(workflow["21"])
    node["inputs"]["images"] = ["19", 0]
    node["inputs"]["audio"] = ["20", 0]
    return node


class ContractClient:
    def __init__(self, definition=None):
        self.definition = definition or vhs_definition()
        self.info_calls = 0
        self.submit_calls = 0

    def get_object_info(self, class_type):
        self.info_calls += 1
        if class_type != "VHS_VideoCombine":
            raise AssertionError(f"Unexpected class {class_type}")
        return self.definition

    def submit_prompt(self, workflow):
        self.submit_calls += 1
        return {"prompt_id": str(uuid.uuid4()), "queue_number": 1, "node_errors": {}}


class Phase8C3DynamicSchemaTests(unittest.TestCase):
    def test_static_required_optional_and_hidden_inputs_resolve(self):
        node = production_node_21()
        schema = resolve_live_node_input_schema(vhs_definition(), node["inputs"])
        self.assertIn("images", schema["static"])
        self.assertIn("audio", schema["static"])
        self.assertIn("prompt", schema["static"])
        self.assertTrue(schema["determinable"])

    def test_exact_h264_selection_resolves_runtime_dynamic_widgets(self):
        node = production_node_21()
        schema = resolve_live_node_input_schema(vhs_definition(), node["inputs"])
        self.assertEqual(
            sorted(schema["dynamic"]),
            ["crf", "pix_fmt", "save_metadata", "trim_to_audio"],
        )
        self.assertEqual(schema["dynamic"]["pix_fmt"][0], ["yuv420p", "yuv420p10le"])

    def test_nested_dynamic_metadata_is_parsed_generically(self):
        node = production_node_21()
        schema = resolve_live_node_input_schema(vhs_definition(nested=True), node["inputs"])
        self.assertEqual(schema["dynamic_selectors"][0]["input_name"], "format")
        self.assertIn("pix_fmt", schema["dynamic"])

    def test_exact_production_node_21_is_fully_assessed_and_accepted(self):
        node = production_node_21()
        result = validate_live_workflow_compatibility(
            {"21": node},
            ContractClient(),
        )
        self.assertTrue(result["compatible"])
        self.assertEqual(result["assessed_inputs"], len(node["inputs"]))
        self.assertEqual(result["assessment_counts"][LIVE_INPUT_SUPPORTED], len(node["inputs"]))
        self.assertEqual(result["not_determinable"], [])

    def test_production_pix_fmt_crf_and_booleans_are_accepted(self):
        node = production_node_21()
        node["inputs"].update({
            "pix_fmt": "yuv420p",
            "crf": 16,
            "save_metadata": False,
            "trim_to_audio": False,
        })
        result = validate_live_workflow_compatibility({"999": node}, ContractClient())
        selector = result["dynamic_schemas"][0]["selectors"][0]
        self.assertEqual(selector["selected_value"], "video/h264-mp4")
        self.assertEqual(selector["resolved_inputs"], ["crf", "pix_fmt", "save_metadata", "trim_to_audio"])

    def test_invalid_dynamic_enum_is_definitively_rejected(self):
        node = production_node_21()
        node["inputs"]["pix_fmt"] = "rgb24"
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility({"21": node}, ContractClient())
        issue = context.exception.details["issues"][0]
        self.assertEqual(issue["input_name"], "pix_fmt")
        self.assertEqual(issue["source"], "selected_option_dynamic")
        self.assertIn("not accepted", issue["reason"])

    def test_invalid_dynamic_numeric_type_and_bounds_are_rejected(self):
        for crf, expected in (("16", "integer"), (101, "at most 100")):
            with self.subTest(crf=crf):
                node = production_node_21()
                node["inputs"]["crf"] = crf
                with self.assertRaises(RenderExecutionBlocked) as context:
                    validate_live_workflow_compatibility({"21": node}, ContractClient())
                self.assertIn(expected, context.exception.message)

    def test_input_for_another_selected_format_is_rejected(self):
        node = production_node_21()
        node["inputs"]["input_color_depth"] = "8bit"
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility({"21": node}, ContractClient())
        self.assertIn("input_color_depth", context.exception.message)
        self.assertIn("selected live node configuration", context.exception.message)

    def test_same_class_can_resolve_different_per_node_input_sets(self):
        h264 = production_node_21()
        h265 = production_node_21()
        h265["inputs"].update({"format": "video/h265-mp4"})
        h265["inputs"].pop("trim_to_audio")
        client = ContractClient()
        result = validate_live_workflow_compatibility({"20": h264, "21": h265}, client)
        resolved_sets = [item["selectors"][0]["resolved_inputs"] for item in result["dynamic_schemas"]]
        self.assertEqual(client.info_calls, 1)
        self.assertIn(["crf", "pix_fmt", "save_metadata", "trim_to_audio"], resolved_sets)
        self.assertIn(["crf", "pix_fmt", "save_metadata"], resolved_sets)

    def test_malformed_dynamic_metadata_is_not_determinable_and_does_not_false_block(self):
        definition = vhs_definition()
        definition["input"]["required"]["format"][1]["formats"]["video/h264-mp4"] = "malformed"
        result = validate_live_workflow_compatibility({"21": production_node_21()}, ContractClient(definition))
        self.assertTrue(result["compatible"])
        self.assertGreater(result["assessment_counts"][LIVE_INPUT_NOT_DETERMINABLE], 0)
        self.assertTrue(any(item["input_name"] == "pix_fmt" for item in result["not_determinable"]))

    def test_missing_entire_input_contract_is_not_determinable_and_does_not_false_block(self):
        result = validate_live_workflow_compatibility(
            {"21": production_node_21()},
            ContractClient({"display_name": "Video Combine"}),
        )
        self.assertTrue(result["compatible"])
        self.assertEqual(result["not_determinable"][0]["input_name"], None)

    def test_unknown_input_against_complete_static_schema_still_blocks(self):
        node = {"class_type": "VHS_VideoCombine", "inputs": {"images": ["1", 0], "bogus": True}}
        definition = {"input": {"required": {"images": ["IMAGE"]}}}
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility({"7": node}, ContractClient(definition))
        self.assertIn("bogus", context.exception.message)

    def test_object_info_remains_read_only_and_prompt_is_final_authority(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {"VHS_VideoCombine": vhs_definition()}

        client = ComfyUIClient(transport=transport)
        result = validate_live_workflow_compatibility({"21": production_node_21()}, client)
        self.assertEqual(calls, [("GET", "/object_info/VHS_VideoCombine", None)])
        self.assertEqual(result["final_authority"], "comfyui_prompt_validation")
        self.assertEqual(result["dynamic_schema_cache"], "resolved_per_node_configuration")

    def test_raw_class_contract_is_cached_but_selected_option_is_resolved_each_time(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {"VHS_VideoCombine": vhs_definition()}

        client = ComfyUIClient(transport=transport)
        h264 = production_node_21()
        h265 = production_node_21()
        h265["inputs"]["format"] = "video/h265-mp4"
        h265["inputs"].pop("trim_to_audio")
        first = validate_live_workflow_compatibility({"21": h264}, client)
        second = validate_live_workflow_compatibility({"21": h265}, client)
        self.assertEqual(len(calls), 1)
        self.assertIn("trim_to_audio", first["dynamic_schemas"][0]["selectors"][0]["resolved_inputs"])
        self.assertNotIn("trim_to_audio", second["dynamic_schemas"][0]["selectors"][0]["resolved_inputs"])

    def test_validator_only_remediation_keeps_preparation_v3_and_workflow_values(self):
        node = production_node_21()
        self.assertEqual(PREPARATION_VERSION, 3)
        self.assertEqual(node["inputs"]["format"], "video/h264-mp4")
        self.assertEqual(node["inputs"]["pix_fmt"], "yuv420p")
        self.assertEqual(node["inputs"]["crf"], 16)


class Phase8C3SubmissionBoundaryTests(unittest.TestCase):
    def test_exact_node_21_passes_live_inspection_then_reaches_prompt_once(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = ProjectStorage(Path(directory) / "projects")
            project = storage.create_project("Phase 8C.3 dynamic live schema")
            project_id = project["project_id"]
            scene_id = str(uuid.uuid4())
            fingerprint = "f" * 64
            preflight = {
                "project_id": project_id,
                "scenes": [{
                    "scene_id": scene_id,
                    "generation_method": "reference2video",
                    "content_ready": True,
                    "workflow_ready": True,
                    "runtime_requirements_ready": True,
                    "preparation_current": True,
                    "preparation_fingerprint": fingerprint,
                }],
            }
            package = {
                "workflow": {"21": production_node_21()},
                "generation_method": "reference2video",
                "preparation_fingerprint": fingerprint,
                "output_node_id": "21",
                "expected_filename_prefix": "music_video_builder/render",
                "output_format": "video/h264-mp4",
            }
            client = ContractClient()

            job = submit_render_job(
                storage,
                project_id,
                scene_id,
                client=client,
                preflight=preflight,
                package_loader=lambda *_args: package,
            )

            self.assertEqual(job["state"], "QUEUED")
            self.assertEqual(client.info_calls, 1)
            self.assertEqual(client.submit_calls, 1)
            self.assertTrue(job["runtime_compatibility"]["compatible"])


if __name__ == "__main__":
    unittest.main()
