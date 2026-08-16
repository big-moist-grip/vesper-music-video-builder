import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectStorage
from backend.render_jobs import (
    COMFYUI_DIAGNOSTIC_MAX_STRING,
    COMFYUI_ERROR_RESPONSE_MAX_BYTES,
    FAILED,
    JobStore,
    ComfyUIClient,
    ComfyUIClientError,
    RenderExecutionBlocked,
    RenderJobSubmissionError,
    reconcile_project_jobs,
    retry_render_job,
    submit_render_job,
    validate_live_workflow_compatibility,
)
from backend.workflows import WORKFLOW_DIRECTORY, production_manifest_registry


ROOT = Path(__file__).parents[1]


def validation_body(*, selector="music_video_builder/project/scene/scene_audio.wav"):
    return {
        "error": {
            "type": "prompt_outputs_failed_validation",
            "message": "Prompt outputs failed validation",
            "details": "",
            "extra_info": {},
        },
        "node_errors": {
            "7": {
                "errors": [{
                    "type": "custom_validation_failed",
                    "message": "Custom validation failed for node",
                    "details": f"audio - Invalid audio file: {selector}",
                    "extra_info": {"input_name": "audio"},
                }],
                "dependent_outputs": ["21"],
                "class_type": "LoadAudio",
            },
        },
    }


def api_workflow(selector="music_video_builder/project/scene/scene_audio.wav"):
    return {"7": {"class_type": "LoadAudio", "inputs": {"audio": selector}}}


def http_error(body, *, status=400):
    return urllib.error.HTTPError(
        "http://127.0.0.1:8188/prompt",
        status,
        "Bad Request",
        {},
        io.BytesIO(body),
    )


class ContractClient:
    def __init__(self, definitions):
        self.definitions = definitions
        self.calls = []

    def get_object_info(self, class_type):
        self.calls.append(class_type)
        definition = self.definitions.get(class_type)
        if definition is None:
            raise ComfyUIClientError(
                "LIVE_NODE_CLASS_MISSING",
                f"Required node class '{class_type}' is not registered in the active ComfyUI runtime.",
            )
        return definition


class SubmissionClient(ContractClient):
    def __init__(self, *, reject=True):
        super().__init__({
            "LoadImage": {"input": {"required": {"image": [["keyframe.png"]]}}},
            "VHS_VideoCombine": {"input": {"required": {
                "filename_prefix": ["STRING", {}],
                "format": [["video/h264-mp4"], {}],
            }}},
        })
        self.reject = reject
        self.submit_calls = 0
        self.queue_calls = 0
        self.history_calls = 0

    def submit_prompt(self, workflow):
        self.submit_calls += 1
        if self.reject:
            raise ComfyUIClientError(
                "COMFYUI_VALIDATION_REJECTED",
                "ComfyUI rejected the workflow: node 1 (LoadImage) rejected input 'image': Invalid image file: keyframe.png",
                details={
                    "http_status": 400,
                    "comfyui_error": validation_body()["error"],
                    "node_errors": validation_body()["node_errors"],
                    "affected_nodes": [{
                        "node_id": "1",
                        "class_type": "LoadImage",
                        "input_name": "image",
                        "submitted_value": "keyframe.png",
                        "reason": "Invalid image file: keyframe.png",
                    }],
                },
            )
        return {"prompt_id": str(uuid.uuid4()), "queue_number": 1, "node_errors": {}}

    def get_queue(self):
        self.queue_calls += 1
        return {"queue_running": [], "queue_pending": []}

    def get_history(self, _prompt_id):
        self.history_calls += 1
        return {}

    def cancellation_capabilities(self):
        return {"queued": True, "running": True, "running_scope": "test"}


class Phase8C2HttpAndPayloadTests(unittest.TestCase):
    def setUp(self):
        self.client = ComfyUIClient(base_url="http://127.0.0.1:8188")

    def test_http_400_json_error_and_node_errors_are_preserved_and_normalized(self):
        selector = "music_video_builder/p/s/scene_audio.wav"
        body = json.dumps(validation_body(selector=selector)).encode("utf-8")
        with patch("backend.render_jobs.urllib.request.urlopen", side_effect=http_error(body)):
            with self.assertRaises(ComfyUIClientError) as context:
                self.client.submit_prompt(api_workflow(selector))

        error = context.exception
        self.assertEqual(error.code, "COMFYUI_VALIDATION_REJECTED")
        self.assertEqual(error.details["http_status"], 400)
        self.assertEqual(error.details["response_body"]["error"]["type"], "prompt_outputs_failed_validation")
        self.assertIn("7", error.details["node_errors"])
        self.assertEqual(error.details["affected_nodes"][0]["class_type"], "LoadAudio")
        self.assertEqual(error.details["affected_nodes"][0]["input_name"], "audio")
        self.assertEqual(error.details["affected_nodes"][0]["submitted_value"], selector)
        self.assertIn("node 7 (LoadAudio)", error.message)
        self.assertIn("Invalid audio file", error.message)

    def test_malformed_non_json_400_is_safe_and_bounded(self):
        body = b"not-json:" + (b"x" * (COMFYUI_ERROR_RESPONSE_MAX_BYTES + 500))
        with patch("backend.render_jobs.urllib.request.urlopen", side_effect=http_error(body)):
            with self.assertRaises(ComfyUIClientError) as context:
                self.client.submit_prompt(api_workflow())
        error = context.exception
        self.assertEqual(error.code, "COMFYUI_HTTP_ERROR")
        self.assertTrue(error.details["response_truncated"])
        self.assertLessEqual(len(error.details["response_body"]), COMFYUI_DIAGNOSTIC_MAX_STRING)
        self.assertNotIn("node_errors", error.details)

    def test_network_error_remains_distinct_from_validation_rejection(self):
        with patch("backend.render_jobs.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(ComfyUIClientError) as context:
                self.client.submit_prompt(api_workflow())
        self.assertEqual(context.exception.code, "COMFYUI_UNAVAILABLE")
        self.assertTrue(context.exception.transient)
        self.assertEqual(context.exception.message, "The local ComfyUI API could not be reached.")

    def test_prompt_payload_is_exact_api_graph_and_json_encodable(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {"prompt_id": "prompt-1", "number": 3, "node_errors": {}}

        client = ComfyUIClient(base_url="http://127.0.0.1:8188", transport=transport)
        graph = api_workflow()
        client.submit_prompt(graph)
        self.assertEqual(calls, [("POST", "/prompt", {"prompt": graph, "client_id": client.telemetry.client_id})])
        self.assertIsInstance(calls[0][2]["prompt"], dict)
        self.assertEqual(set(calls[0][2]), {"prompt", "client_id"})
        json.dumps(calls[0][2], ensure_ascii=False, allow_nan=False)

    def test_ui_workflow_wrappers_and_non_api_nodes_are_rejected_before_transport(self):
        calls = []
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            transport=lambda *args: calls.append(args) or {},
        )
        invalid_workflows = [
            {"nodes": [], "links": []},
            {"workflow": api_workflow()},
            {"prompt": api_workflow()},
            {"1": {"type": "LoadImage", "widgets_values": ["image.png"]}},
            {"1": {"class_type": "LoadImage", "inputs": {"value": float("nan")}}},
        ]
        for workflow in invalid_workflows:
            with self.subTest(workflow=list(workflow)):
                with self.assertRaises(ComfyUIClientError):
                    client.submit_prompt(workflow)
        self.assertEqual(calls, [])


class Phase8C2LiveContractTests(unittest.TestCase):
    def _schema(self, *, enum_value="model.safetensors"):
        return {
            "input": {
                "required": {
                    "model_name": [[enum_value]],
                    "count": ["INT"],
                },
                "optional": {"prompt": ["STRING"]},
            },
        }

    def test_registered_classes_inputs_required_values_and_types_pass(self):
        workflow = {
            "1": {
                "class_type": "ModelNode",
                "inputs": {"model_name": "model.safetensors", "count": 5, "prompt": "current"},
            },
        }
        client = ContractClient({"ModelNode": self._schema()})
        result = validate_live_workflow_compatibility(workflow, client)
        self.assertTrue(result["compatible"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["checked_node_classes"], ["ModelNode"])

    def test_missing_live_node_class_blocks_before_submission(self):
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(
                {"1": {"class_type": "MissingNode", "inputs": {}}},
                ContractClient({}),
            )
        self.assertEqual(context.exception.code, "LIVE_WORKFLOW_INCOMPATIBLE")
        self.assertIn("not registered", context.exception.message)

    def test_invalid_input_name_is_detected(self):
        workflow = {"1": {"class_type": "ModelNode", "inputs": {
            "model_name": "model.safetensors", "count": 5, "old_input": True,
        }}}
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(workflow, ContractClient({"ModelNode": self._schema()}))
        self.assertIn("old_input", context.exception.message)

    def test_required_input_omission_is_detected(self):
        workflow = {"1": {"class_type": "ModelNode", "inputs": {"model_name": "model.safetensors"}}}
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(workflow, ContractClient({"ModelNode": self._schema()}))
        self.assertIn("count", context.exception.message)
        self.assertIn("required input is missing", context.exception.message)

    def test_invalid_enum_and_scalar_type_are_detected(self):
        for inputs, expected in (
            ({"model_name": "old-model.safetensors", "count": 5}, "not accepted"),
            ({"model_name": "model.safetensors", "count": "5"}, "integer"),
        ):
            with self.subTest(inputs=inputs):
                with self.assertRaises(RenderExecutionBlocked) as context:
                    validate_live_workflow_compatibility(
                        {"1": {"class_type": "ModelNode", "inputs": inputs}},
                        ContractClient({"ModelNode": self._schema()}),
                    )
                self.assertIn(expected, context.exception.message)

    def test_missing_image_selector_reproduces_actual_live_file_validation_defect(self):
        workflow = {"50": {"class_type": "LoadImage", "inputs": {
            "image": "music_video_builder/p/s/picture_01.png",
        }}}
        definition = {"input": {"required": {"image": [["some_other_file.png"]]}}}
        with self.assertRaises(RenderExecutionBlocked) as context:
            validate_live_workflow_compatibility(workflow, ContractClient({"LoadImage": definition}))
        self.assertIn("picture_01.png", context.exception.message)
        self.assertIn("not accepted", context.exception.message)

    def test_object_info_is_bounded_cached_and_read_only(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {"ModelNode": self._schema()}

        client = ComfyUIClient(base_url="http://127.0.0.1:8188", transport=transport)
        workflow = {
            "1": {"class_type": "ModelNode", "inputs": {"model_name": "model.safetensors", "count": 5}},
            "2": {"class_type": "ModelNode", "inputs": {"model_name": "model.safetensors", "count": 8}},
        }
        validate_live_workflow_compatibility(workflow, client)
        validate_live_workflow_compatibility(workflow, client)
        self.assertEqual(calls, [("GET", "/object_info/ModelNode", None)])
        self.assertNotIn("/prompt", {path for _method, path, _payload in calls})

    def test_oversized_object_info_is_rejected(self):
        huge = "x" * (2 * 1024 * 1024)
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            transport=lambda *_args: {"ModelNode": {"input": {}, "description": huge}},
        )
        with self.assertRaises(ComfyUIClientError) as context:
            client.get_object_info("ModelNode")
        self.assertEqual(context.exception.code, "COMFYUI_RESPONSE_TOO_LARGE")

    def test_all_production_template_node_classes_are_inspected(self):
        for method, manifest in production_manifest_registry().items():
            workflow = json.loads((WORKFLOW_DIRECTORY / manifest["workflow_file"]).read_text(encoding="utf-8"))
            definitions = {}
            for node in workflow.values():
                required = {}
                for name, value in node["inputs"].items():
                    if isinstance(value, bool):
                        required[name] = ["BOOLEAN"]
                    elif isinstance(value, int):
                        required[name] = ["INT"]
                    elif isinstance(value, float):
                        required[name] = ["FLOAT"]
                    elif isinstance(value, str):
                        required[name] = ["STRING"]
                    else:
                        required[name] = ["ANY"]
                definitions[node["class_type"]] = {"input": {"required": required}}
            client = ContractClient(definitions)
            result = validate_live_workflow_compatibility(workflow, client)
            self.assertEqual(set(result["checked_node_classes"]), {node["class_type"] for node in workflow.values()}, method)


class Phase8C2JobAndUiTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8C.2 submission diagnostics")
        self.project_id = project["project_id"]
        self.scene_id = str(uuid.uuid4())
        self.preflight = {
            "project_id": self.project_id,
            "scenes": [{
                "scene_id": self.scene_id,
                "generation_method": "keyframe_i2v",
                "content_ready": True,
                "workflow_ready": True,
                "runtime_requirements_ready": True,
                "preparation_current": True,
                "preparation_fingerprint": "f" * 64,
            }],
        }
        self.package = {
            "workflow": {
                "1": {"class_type": "LoadImage", "inputs": {"image": "keyframe.png"}},
                "20": {"class_type": "VHS_VideoCombine", "inputs": {
                    "filename_prefix": "music_video_builder/render",
                    "format": "video/h264-mp4",
                }},
            },
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": "f" * 64,
            "output_node_id": "20",
            "expected_filename_prefix": "music_video_builder/render",
            "output_format": "video/h264-mp4",
        }

    def _submit(self, client):
        return submit_render_job(
            self.storage,
            self.project_id,
            self.scene_id,
            client=client,
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
        )

    def test_400_submission_is_terminal_without_prompt_id_queue_state_or_polling(self):
        client = SubmissionClient(reject=True)
        with self.assertRaises(RenderJobSubmissionError) as context:
            self._submit(client)
        failed = JobStore(self.storage).load(self.project_id, context.exception.job_id)
        self.assertEqual(failed["state"], FAILED)
        self.assertEqual(failed["failure"]["category"], "submission_failed_validation")
        self.assertIsNone(failed["comfy_prompt_id"])
        self.assertIsNone(failed["submitted_at"])
        self.assertNotIn("QUEUED", {item["to"] for item in failed["transitions"]})
        self.assertNotIn("RUNNING", {item["to"] for item in failed["transitions"]})
        self.assertIn("affected_nodes", failed["failure"]["diagnostics"])

        reconcile_project_jobs(self.storage, self.project_id, client=client)
        self.assertEqual(client.queue_calls, 0)
        self.assertEqual(client.history_calls, 0)

    def test_retry_after_validation_rejection_creates_new_builder_job(self):
        rejecting = SubmissionClient(reject=True)
        with self.assertRaises(RenderJobSubmissionError) as context:
            self._submit(rejecting)
        accepted = SubmissionClient(reject=False)
        retried = retry_render_job(
            self.storage,
            self.project_id,
            context.exception.job_id,
            client=accepted,
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
        )
        self.assertNotEqual(retried["job_id"], context.exception.job_id)
        self.assertEqual(retried["state"], "QUEUED")
        self.assertIsNotNone(retried["comfy_prompt_id"])
        self.assertEqual(retried["retry_of_job_id"], context.exception.job_id)

    def test_stale_preparation_blocks_retry_before_object_info_or_prompt(self):
        rejecting = SubmissionClient(reject=True)
        with self.assertRaises(RenderJobSubmissionError) as context:
            self._submit(rejecting)
        stale = {**self.preflight, "scenes": [{**self.preflight["scenes"][0], "preparation_current": False}]}
        retry_client = SubmissionClient(reject=False)
        with self.assertRaises(RenderExecutionBlocked):
            retry_render_job(
                self.storage,
                self.project_id,
                context.exception.job_id,
                client=retry_client,
                preflight=stale,
                package_loader=lambda *_args: self.package,
            )
        self.assertEqual(retry_client.calls, [])
        self.assertEqual(retry_client.submit_calls, 0)

    def test_pre_submit_live_contract_failure_creates_no_job_and_never_calls_prompt(self):
        client = SubmissionClient(reject=False)
        client.definitions = {}
        with self.assertRaises(RenderExecutionBlocked):
            self._submit(client)
        self.assertEqual(JobStore(self.storage).list_scene(self.project_id, self.scene_id), [])
        self.assertEqual(client.submit_calls, 0)

    def test_ui_has_concise_failure_and_keeps_technical_details_out_of_primary_card(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
        jobs = (ROOT / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertIn("job.failure?.message", extension)
        self.assertNotIn("renderSubmissionDiagnostics", extension)
        self.assertNotIn("Submission diagnostics", extension)
        self.assertNotIn(".mvb-render-submission-diagnostics", css)
        self.assertNotIn("JSON.stringify(issue)", extension)
        self.assertIn("Render job submission failed job_id=%s scene_id=%s code=%s", jobs)

    def test_submission_validation_and_execution_failures_remain_distinct(self):
        jobs = (ROOT / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertIn('"submission_failed_validation"', jobs)
        self.assertIn('"category": "execution_failure"', jobs)
        self.assertIn("SUPPORTED_MULTI_GPU", jobs)
        self.assertNotIn("DEFERRED_TARGET_NVIDIA", jobs)


if __name__ == "__main__":
    unittest.main()
