import json
import tempfile
from copy import deepcopy
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectStorage
from backend.render_jobs import (
    CANCELLED,
    CANCEL_REQUESTED,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    SUBMITTING,
    SUCCEEDED,
    UNKNOWN,
    ComfyUIClient,
    ComfyUIClientError,
    JobStore,
    RenderExecutionBlocked,
    RenderExecutionDeferred,
    RenderJobConflict,
    RenderJobStateError,
    RenderJobSubmissionError,
    RenderOutputDiscoveryError,
    TargetHardwareGate,
    TargetHardwareQualification,
    cancel_render_job,
    discover_raw_output,
    evaluate_raw_output_association,
    is_raw_output_usable,
    new_job_record,
    render_completion_summary,
    reconcile_project_jobs,
    retry_render_job,
    submit_render_job,
    transition_job,
)


class FakeComfyClient:
    def __init__(self, *, prompt_ids=None):
        self.submitted = []
        self.pending = []
        self.running = []
        self.history = {}
        self.calls = {"submit": 0, "queue": 0, "history": 0, "delete": 0, "interrupt": 0, "object_info": 0}
        self.prompt_ids = list(prompt_ids or [])
        self.queue_error = None
        self.history_error = None

    def get_object_info(self, class_type):
        self.calls["object_info"] += 1
        if class_type == "LoadImage":
            return {"input": {"required": {"image": [["keyframe.png"]]}}}
        if class_type == "VHS_VideoCombine":
            return {
                "input": {
                    "required": {
                        "filename_prefix": ["STRING", {}],
                        "format": [["video/h264-mp4"], {}],
                    }
                }
            }
        raise ComfyUIClientError("LIVE_NODE_CLASS_MISSING", f"Missing node class {class_type}.")

    def submit_prompt(self, workflow):
        self.calls["submit"] += 1
        self.submitted.append(workflow)
        if self.prompt_ids:
            prompt_id = self.prompt_ids.pop(0)
        else:
            prompt_id = str(uuid.uuid4())
        self.pending.append(prompt_id)
        return {"prompt_id": prompt_id, "queue_number": self.calls["submit"], "node_errors": {}}

    def get_queue(self):
        self.calls["queue"] += 1
        if self.queue_error is not None:
            raise self.queue_error
        return {
            "queue_running": [[0, prompt_id] for prompt_id in self.running],
            "queue_pending": [[0, prompt_id] for prompt_id in self.pending],
        }

    def get_history(self, prompt_id):
        self.calls["history"] += 1
        if self.history_error is not None:
            raise self.history_error
        return self.history.get(prompt_id, {})

    def delete_queued(self, prompt_id):
        self.calls["delete"] += 1
        self.pending = [item for item in self.pending if item != prompt_id]
        return {"deleted": [prompt_id]}

    def interrupt_running(self, prompt_id):
        self.calls["interrupt"] += 1
        self.running = [item for item in self.running if item != prompt_id]
        return {"interrupted": True}

    def cancellation_capabilities(self):
        return {
            "queued": True,
            "running": True,
            "running_scope": "global_engine_interrupt_targeted_by_prompt_id",
        }


class Phase8BTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8B queue lifecycle")
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
                "20": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": f"music_video_builder/{self.project_id}/{self.scene_id}/render", "format": "video/h264-mp4"}},
                "21": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": f"music_video_builder/{self.project_id}/{self.scene_id}/render", "format": "video/h264-mp4"}},
            },
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": "f" * 64,
            "output_node_id": "20",
            "expected_filename_prefix": f"music_video_builder/{self.project_id}/{self.scene_id}/render",
            "output_format": "video/h264-mp4",
        }
        self.client = FakeComfyClient()

    def _qualified_gate(self):
        return TargetHardwareGate(lambda: TargetHardwareQualification(
            True,
            "SUPPORTED_MULTI_GPU",
            "Both supported H3 execution platforms are valid.",
            "test-fixture",
        ))

    def _submit(self, *, client=None, gate=None, preflight=None, retry_of_job_id=None):
        return submit_render_job(
            self.storage,
            self.project_id,
            self.scene_id,
            client=client or self.client,
            hardware_gate=gate or self._qualified_gate(),
            preflight=preflight or self.preflight,
            package_loader=lambda *_args: self.package,
            retry_of_job_id=retry_of_job_id,
        )

    def test_client_uses_only_installed_core_api_paths_and_prepared_payload(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            if path == "/prompt":
                return {"prompt_id": str(uuid.uuid4()), "number": 4, "node_errors": {}}
            if path == "/queue":
                return {"queue_running": [], "queue_pending": []}
            return {"outputs": {}}

        client = ComfyUIClient(base_url="http://127.0.0.1:8188", transport=transport)
        workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "prepared.png"}}}
        client.submit_prompt(workflow)
        client.get_queue()
        self.assertEqual(calls[0], ("POST", "/prompt", {"prompt": workflow, "client_id": client.telemetry.client_id}))
        self.assertEqual(calls[1][1], "/queue")
        self.assertNotIn("workflow", calls[0][2])

    def test_client_rejects_external_hosts_and_unbounded_timeouts(self):
        with self.assertRaises(ComfyUIClientError):
            ComfyUIClient(base_url="https://example.com")
        with self.assertRaises(ComfyUIClientError):
            ComfyUIClient(base_url="http://127.0.0.1:8188", timeout_seconds=61)
        with self.assertRaises(ComfyUIClientError):
            ComfyUIClient(base_url="http://127.0.0.1:8188", timeout_seconds=0)

    def test_default_multi_gpu_policy_allows_ready_scene_to_reach_queue_adapter(self):
        result = self._submit(gate=TargetHardwareGate())
        self.assertEqual(result["state"], QUEUED)
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertTrue(result["execution_gate"]["allowed"])
        self.assertTrue(result["execution_gate"]["target_hardware"]["qualified"])

    def test_hardware_preference_never_blocks_ready_scene(self):
        preference_only = TargetHardwareGate(lambda: TargetHardwareQualification(
            False,
            "PERFORMANCE_PREFERENCE_ONLY",
            "RTX is preferred for throughput but is not required.",
            "test-preference-only",
        ))
        inspection = preference_only.inspect(self.preflight["scenes"][0])
        self.assertTrue(inspection["allowed"])
        self.assertTrue(inspection["execution_eligibility"]["eligible"])
        self.assertFalse(inspection["target_hardware"]["qualified"])
        result = self._submit(gate=preference_only)
        self.assertEqual(result["state"], QUEUED)
        self.assertEqual(self.client.calls["submit"], 1)

    def test_readiness_gate_blocks_before_prompt_when_preparation_is_stale(self):
        blocked = dict(self.preflight)
        blocked["scenes"] = [dict(self.preflight["scenes"][0], preparation_current=False)]
        with self.assertRaises(RenderExecutionBlocked):
            self._submit(preflight=blocked)
        self.assertEqual(self.client.calls["submit"], 0)

    def test_hardware_dimension_is_separate_from_content_readiness(self):
        scene = dict(self.preflight["scenes"][0], content_ready=False)
        gate = TargetHardwareGate()
        inspection = gate.inspect(scene)
        self.assertFalse(inspection["allowed"])
        self.assertTrue(inspection["target_hardware"]["qualified"])
        self.assertFalse(inspection["execution_eligibility"]["eligible"])
        self.assertEqual(inspection["blockers"][0]["code"], "CONTENT_NOT_READY")

    def test_package_fingerprint_mismatch_is_rejected_before_prompt(self):
        stale_package = dict(self.package, preparation_fingerprint="0" * 64)
        with self.assertRaises(RenderExecutionBlocked) as context:
            submit_render_job(
                self.storage,
                self.project_id,
                self.scene_id,
                client=self.client,
                hardware_gate=self._qualified_gate(),
                preflight=self.preflight,
                package_loader=lambda *_args: stale_package,
            )
        self.assertEqual(context.exception.code, "PREPARATION_STALE")
        self.assertEqual(self.client.calls["submit"], 0)

    def test_malformed_prompt_response_becomes_submission_failure(self):
        malformed = FakeComfyClient()
        malformed.submit_prompt = lambda _workflow: {}
        with self.assertRaises(RenderJobSubmissionError) as context:
            self._submit(client=malformed)
        failed = JobStore(self.storage).load(self.project_id, context.exception.job_id)
        self.assertEqual(failed["state"], FAILED)
        self.assertEqual(failed["failure"]["code"], "COMFYUI_PROMPT_ID_INVALID")

    def test_submission_creates_builder_job_and_sends_only_prepared_workflow(self):
        result = self._submit()
        self.assertEqual(result["state"], QUEUED)
        self.assertTrue(result["job_id"])
        self.assertTrue(result["comfy_prompt_id"])
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(set(self.client.submitted[0]), set(self.package["workflow"]))
        self.assertEqual(self.client.submitted[0]["1"], self.package["workflow"]["1"])
        self.assertNotEqual(
            self.client.submitted[0]["20"]["inputs"]["filename_prefix"],
            self.package["workflow"]["20"]["inputs"]["filename_prefix"],
        )
        self.assertEqual(result["production_output"]["node_id"], "20")

    def test_one_active_job_prevents_duplicate_scene_submission(self):
        self._submit()
        with self.assertRaises(RenderJobConflict):
            self._submit()
        self.assertEqual(self.client.calls["submit"], 1)

    def test_job_metadata_is_atomic_project_owned_and_survives_reopen(self):
        result = self._submit()
        job_path = self.storage.project_directory(self.project_id) / "renders" / self.scene_id / "jobs" / f"{result['job_id']}.json"
        self.assertTrue(job_path.is_file())
        persisted = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["job_schema_version"], 1)
        self.assertNotIn("workflow", persisted)
        reopened = JobStore(ProjectStorage(Path(self.temp_directory.name) / "projects"))
        self.assertEqual(reopened.load(self.project_id, result["job_id"])["state"], QUEUED)
        self.assertEqual(self.storage.load_project(self.project_id)["schema_version"], 7)

    def test_submission_failure_is_distinct_and_durable(self):
        failing = FakeComfyClient()
        failing.submit_prompt = lambda _workflow: (_ for _ in ()).throw(
            ComfyUIClientError("COMFYUI_UNAVAILABLE", "ComfyUI is unavailable.", transient=True)
        )
        with self.assertRaises(RenderJobSubmissionError) as context:
            self._submit(client=failing)
        failed = JobStore(self.storage).load(self.project_id, context.exception.job_id)
        self.assertEqual(failed["state"], FAILED)
        self.assertEqual(failed["failure"]["category"], "submission_failure")
        self.assertTrue(failed["failure"]["transient"])

    def test_state_machine_rejects_illegal_transition_and_freezes_terminal_state(self):
        record = new_job_record(self.project_id, self.scene_id, "keyframe_i2v", "f" * 64, output_node_id="20", expected_filename_prefix="prefix")
        with self.assertRaises(RenderJobStateError):
            transition_job(record, RUNNING, reason="illegal")
        record = transition_job(record, SUBMITTING, reason="submit")
        record = transition_job(record, QUEUED, reason="accepted")
        record = transition_job(record, RUNNING, reason="running")
        record = transition_job(record, SUCCEEDED, reason="success")
        with self.assertRaises(RenderJobStateError):
            transition_job(record, FAILED, reason="terminal mutation")

    def test_retry_creates_new_job_and_new_prompt_id(self):
        first = self._submit()
        failed = transition_job(first, FAILED, reason="test execution failure")
        JobStore(self.storage).save(failed)
        second = retry_render_job(
            self.storage,
            self.project_id,
            first["job_id"],
            client=self.client,
            hardware_gate=self._qualified_gate(),
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
        )
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertNotEqual(second["comfy_prompt_id"], first["comfy_prompt_id"])
        self.assertEqual(second["retry_of_job_id"], first["job_id"])

    def test_reused_prompt_id_is_rejected_and_not_claimed(self):
        fixed_prompt = str(uuid.uuid4())
        first_client = FakeComfyClient(prompt_ids=[fixed_prompt])
        first = self._submit(client=first_client)
        failed = transition_job(first, FAILED, reason="test retry")
        JobStore(self.storage).save(failed)
        reuse_client = FakeComfyClient(prompt_ids=[fixed_prompt])
        with self.assertRaises(RenderJobSubmissionError):
            retry_render_job(
                self.storage,
                self.project_id,
                first["job_id"],
                client=reuse_client,
                hardware_gate=self._qualified_gate(),
                preflight=self.preflight,
                package_loader=lambda *_args: self.package,
            )
        jobs = JobStore(self.storage).list_scene(self.project_id, self.scene_id)
        self.assertEqual(sum(item.get("comfy_prompt_id") == fixed_prompt for item in jobs), 1)

    def test_reconcile_pending_job_stays_queued_without_fake_progress(self):
        submitted = self._submit()
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = next(item for item in result["jobs"] if item["job_id"] == submitted["job_id"])
        self.assertEqual(job["state"], QUEUED)
        self.assertEqual(job["progress"]["value"], None)
        self.assertEqual(job["progress"]["label"], "Queued")

    def test_reconcile_running_job_uses_queue_lifecycle_not_numeric_progress(self):
        submitted = self._submit()
        prompt_id = submitted["comfy_prompt_id"]
        self.client.pending.remove(prompt_id)
        self.client.running.append(prompt_id)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = next(item for item in result["jobs"] if item["job_id"] == submitted["job_id"])
        self.assertEqual(job["state"], RUNNING)
        self.assertIsNone(job["progress"]["value"])

    def test_missing_prompt_id_reconciles_to_unknown_not_success(self):
        record = new_job_record(self.project_id, self.scene_id, "keyframe_i2v", "f" * 64, output_node_id="20", expected_filename_prefix="prefix")
        JobStore(self.storage).save(record)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = result["jobs"][0]
        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["code"], "PROMPT_ID_MISSING")
        self.assertIsNone(job["output"])

    def test_prompt_missing_from_queue_and_history_is_reconciliation_required(self):
        submitted = self._submit()
        self.client.pending.remove(submitted["comfy_prompt_id"])
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = result["jobs"][0]
        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["code"], "RECONCILIATION_REQUIRED")

    def test_transient_poll_error_preserves_last_state(self):
        submitted = self._submit()
        self.client.queue_error = ComfyUIClientError("COMFYUI_UNAVAILABLE", "temporary", transient=True)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = result["jobs"][0]
        self.assertEqual(job["state"], QUEUED)
        self.assertEqual(result["warnings"][0]["code"], "COMFYUI_UNAVAILABLE")
        self.assertTrue(job["last_poll_error"]["transient"])

    def _write_output(self, relative="music_video_builder/output/render_00001_.mp4"):
        output_root = Path(self.temp_directory.name) / "comfy-output"
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"raw h3 video")
        return output_root, target

    def _success_history(self, prompt_id, output_node="20", relative="music_video_builder/output/render_00001_.mp4"):
        return {
            "prompt_id": prompt_id,
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {
                output_node: {
                    "gifs": [{"filename": Path(relative).name, "subfolder": str(Path(relative).parent).replace("\\", "/"), "type": "output"}],
                },
            },
        }

    def test_success_requires_prompt_owned_known_output_node_and_persists_raw_output(self):
        submitted = self._submit()
        relative = f"{submitted['production_output']['filename_prefix']}_00001_.mp4"
        output_root, _target = self._write_output(relative)
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = self._success_history(submitted["comfy_prompt_id"], relative=relative)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=output_root)
        job = result["jobs"][0]
        self.assertEqual(job["state"], SUCCEEDED)
        self.assertEqual(job["output"]["relative_path"], relative)
        self.assertTrue(job["output"]["raw_h3_output"])
        self.assertEqual(job["output"]["prompt_id"], submitted["comfy_prompt_id"])

    def test_historical_raw_output_is_retained_when_current_preparation_changes(self):
        submitted = self._submit()
        relative = f"{submitted['production_output']['filename_prefix']}_00001_.mp4"
        output_root, _target = self._write_output(relative)
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = self._success_history(submitted["comfy_prompt_id"], relative=relative)
        reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=output_root)
        historical = JobStore(self.storage).load(self.project_id, submitted["job_id"])
        current_fingerprint = "c" * 64
        self.assertNotEqual(historical["preparation_fingerprint"], current_fingerprint)
        self.assertEqual(historical["state"], SUCCEEDED)
        self.assertEqual(historical["output"]["relative_path"], relative)
        extension = (Path(__file__).parents[1] / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertIn("HISTORICAL", extension)
        self.assertIn("Historical raw H3 output retained", extension)

    def test_execution_failure_is_not_submission_failure(self):
        submitted = self._submit()
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = {
            "prompt_id": submitted["comfy_prompt_id"],
            "status": {"status_str": "error", "completed": True, "messages": [["execution", "node failed"]]},
            "outputs": {},
        }
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = result["jobs"][0]
        self.assertEqual(job["state"], FAILED)
        self.assertEqual(job["failure"]["category"], "execution_failure")
        self.assertNotEqual(job["failure"]["category"], "submission_failure")

    def test_success_without_output_fails_closed(self):
        submitted = self._submit()
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = {
            "prompt_id": submitted["comfy_prompt_id"],
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {},
        }
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=Path(self.temp_directory.name))
        job = result["jobs"][0]
        self.assertEqual(job["state"], SUCCEEDED)
        self.assertIsNone(job["failure"])
        self.assertEqual(job["output_discovery"]["state"], "FAILED")
        self.assertEqual(job["output_discovery"]["failure"]["code"], "OUTPUT_NODE_MISSING")
        self.assertEqual(job["completion"]["state"], "SUCCEEDED_OUTPUT_UNAVAILABLE")
        self.assertEqual(job["completion"]["association_state"], "MISSING")
        self.assertEqual(job["completion"]["raw_output"]["state"], "MISSING")
        self.assertFalse(job["completion"]["finalization_allowed"])
        self.assertFalse(is_raw_output_usable(job, output_root=Path(self.temp_directory.name)))

    def test_discovered_output_becomes_stale_when_file_is_removed(self):
        submitted = self._submit()
        relative = f"{submitted['production_output']['filename_prefix']}_00001_.mp4"
        output_root, target = self._write_output(relative)
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = self._success_history(submitted["comfy_prompt_id"], relative=relative)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=output_root)
        job = result["jobs"][0]
        self.assertEqual(evaluate_raw_output_association(job, output_root=output_root)["state"], "AVAILABLE")
        target.unlink()
        association = evaluate_raw_output_association(job, output_root=output_root)
        self.assertEqual(association["state"], "STALE")
        self.assertFalse(association["usable"])

    def test_association_projection_preserves_required_failure_states(self):
        root = Path(self.temp_directory.name)
        base = {
            "state": SUCCEEDED,
            "comfy_prompt_id": "owned-prompt",
            "production_output": {"node_id": "20"},
            "output_discovery": {"state": "AVAILABLE", "failure": None},
            "output": {
                "raw_h3_output": True,
                "prompt_id": "owned-prompt",
                "output_node_id": "20",
                "filename": "owned.mp4",
                "subfolder": "",
                "relative_path": "owned.mp4",
            },
        }
        cases = {
            "MISSING": {"output_discovery": {"state": "FAILED", "failure": {"code": "OUTPUT_NODE_MISSING", "message": "missing"}}},
            "STALE": {},
            "UNSAFE": {"output": {"subfolder": ".."}},
            "FOREIGN": {"output": {"prompt_id": "foreign-prompt"}},
            "AMBIGUOUS": {"output_discovery": {"state": "FAILED", "failure": {"code": "OUTPUT_AMBIGUOUS", "message": "ambiguous"}}},
            "UNKNOWN": {"output_discovery": None},
        }
        for expected, patch in cases.items():
            job = deepcopy(base)
            if "output_discovery" in patch:
                job["output_discovery"] = patch["output_discovery"]
            if "output" in patch:
                job["output"].update(patch["output"])
            association = evaluate_raw_output_association(job, output_root=root)
            self.assertEqual(association["state"], expected, expected)
            self.assertFalse(association["usable"])

    def test_default_output_root_is_checked_when_callers_omit_it(self):
        submitted = self._submit()
        relative = f"{submitted['production_output']['filename_prefix']}_00001_.mp4"
        output_root, target = self._write_output(relative)
        self.client.pending.remove(submitted["comfy_prompt_id"])
        self.client.history[submitted["comfy_prompt_id"]] = self._success_history(submitted["comfy_prompt_id"], relative=relative)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=output_root)
        job = result["jobs"][0]
        with patch("backend.render_jobs._default_comfy_output_root", return_value=output_root):
            self.assertTrue(is_raw_output_usable(job))
            target.unlink()
            self.assertFalse(is_raw_output_usable(job))

    def test_queued_cancel_uses_owned_prompt_id_and_reconciles_cancelled(self):
        submitted = self._submit()
        cancelled = cancel_render_job(self.storage, self.project_id, submitted["job_id"], client=self.client)
        self.assertEqual(cancelled["state"], CANCEL_REQUESTED)
        self.assertEqual(self.client.calls["delete"], 1)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        self.assertEqual(result["jobs"][0]["state"], CANCELLED)
        self.assertEqual(result["jobs"][0]["cancel"]["api_scope"], "queued_delete")

    def test_running_cancel_records_global_interrupt_scope_truthfully(self):
        submitted = self._submit()
        prompt_id = submitted["comfy_prompt_id"]
        self.client.pending.remove(prompt_id)
        self.client.running.append(prompt_id)
        cancelled = cancel_render_job(self.storage, self.project_id, submitted["job_id"], client=self.client)
        self.assertEqual(cancelled["state"], CANCEL_REQUESTED)
        self.assertEqual(self.client.calls["interrupt"], 1)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        job = result["jobs"][0]
        self.assertEqual(job["state"], INTERRUPTED)
        self.assertEqual(job["cancel"]["api_scope"], "global_engine_interrupt_targeted_by_prompt_id")

    def test_cancel_is_by_builder_job_id_not_caller_prompt_id(self):
        submitted = self._submit()
        with self.assertRaises(Exception):
            cancel_render_job(self.storage, self.project_id, str(uuid.uuid4()), client=self.client)
        self.assertEqual(self.client.calls["delete"], 0)
        self.assertTrue(submitted["job_id"])

    def test_output_discovery_rejects_unknown_node_and_temp_output(self):
        output_root, _target = self._write_output()
        history = self._success_history(str(uuid.uuid4()), output_node="99")
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)

    def test_output_discovery_rejects_directory_and_unsupported_extension(self):
        output_root = Path(self.temp_directory.name) / "comfy-output"
        directory = output_root / "music_video_builder" / "output" / "render_00001_.mp4"
        directory.mkdir(parents=True, exist_ok=True)
        history = self._success_history(str(uuid.uuid4()))
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)
        history = self._success_history(str(uuid.uuid4()))
        history["outputs"]["20"]["gifs"][0]["filename"] = "render_00001_.png"
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)
        history = self._success_history(str(uuid.uuid4()))
        history["outputs"]["20"]["gifs"][0]["type"] = "temp"
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)

    def test_output_discovery_rejects_traversal_ambiguous_and_prompt_mismatch(self):
        output_root, _target = self._write_output()
        history = self._success_history(str(uuid.uuid4()))
        history["outputs"]["20"]["gifs"][0]["filename"] = "..\\escape.mp4"
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)
        first = output_root / "music_video_builder" / "output" / "render_00002_.mp4"
        first.write_bytes(b"second")
        history = self._success_history(str(uuid.uuid4()))
        history["outputs"]["20"]["gifs"].append({"filename": first.name, "subfolder": "music_video_builder/output", "type": "output"})
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=history["prompt_id"], output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)
        with self.assertRaises(RenderOutputDiscoveryError):
            discover_raw_output(history, prompt_id=str(uuid.uuid4()), output_node_id="20", expected_filename_prefix="music_video_builder/output", output_root=output_root)

    def test_project_coordinator_uses_one_queue_snapshot_for_multiple_scene_jobs(self):
        second_scene = str(uuid.uuid4())
        store = JobStore(self.storage)
        store.save(new_job_record(self.project_id, self.scene_id, "keyframe_i2v", "f" * 64, output_node_id="20", expected_filename_prefix="a"))
        store.save(new_job_record(self.project_id, second_scene, "reference2video", "e" * 64, output_node_id="21", expected_filename_prefix="b"))
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        self.assertEqual(self.client.calls["queue"], 1)
        self.assertEqual(len(result["jobs"]), 2)
        self.assertTrue(all(item["state"] == UNKNOWN for item in result["jobs"]))

    def test_retry_is_blocked_for_unreconciled_unknown_job(self):
        record = new_job_record(self.project_id, self.scene_id, "keyframe_i2v", "f" * 64, output_node_id="20", expected_filename_prefix="prefix")
        unknown = transition_job(record, UNKNOWN, reason="missing prompt")
        JobStore(self.storage).save(unknown)
        with self.assertRaises(RenderJobStateError):
            retry_render_job(self.storage, self.project_id, unknown["job_id"], client=self.client, preflight=self.preflight, package_loader=lambda *_args: self.package)

    def test_output_association_does_not_apply_post_processing(self):
        source = (Path(__file__).parents[1] / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertNotIn("ffmpeg", source.lower())
        self.assertNotIn("remux", source.lower())
        self.assertNotIn("upscale", source.lower())
        self.assertIn("raw_h3_output", source)

    def test_i2v_submission_keeps_prepared_package_as_single_workflow_input(self):
        result = self._submit()
        self.assertEqual(result["generation_method"], "keyframe_i2v")
        self.assertEqual(result["production_output"]["raw_h3_only"], True)
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(set(self.client.submitted[0]), {"1", "20", "21"})

    def test_reference2video_job_identity_preserves_canonical_output_node(self):
        package = dict(self.package, generation_method="reference2video", output_node_id="21")
        result = submit_render_job(
            self.storage,
            self.project_id,
            self.scene_id,
            client=self.client,
            hardware_gate=self._qualified_gate(),
            preflight=dict(self.preflight, scenes=[dict(self.preflight["scenes"][0], generation_method="reference2video")]),
            package_loader=lambda *_args: package,
        )
        self.assertEqual(result["generation_method"], "reference2video")
        self.assertEqual(result["production_output"]["node_id"], "21")

    def test_route_and_frontend_surface_is_job_owned_and_guarded(self):
        root = Path(__file__).parents[1]
        routes = (root / "backend" / "routes.py").read_text(encoding="utf-8")
        extension = (root / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertIn("/render/jobs", routes)
        self.assertIn("/render/scenes/{scene_id}/submit", routes)
        self.assertIn("/cancel", routes)
        self.assertIn("/retry", routes)
        self.assertIn("renderJobsPath", extension)
        self.assertIn("data-mvb-render-cancel", extension)
        self.assertIn("renderTelemetryPresentation", extension)
        self.assertIn("Render Scene", extension)
        self.assertNotIn("Render deferred", extension)
        self.assertIn("execution_eligibility", extension)
        self.assertIn('<h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>', extension)

    def test_phase8a_dry_boundary_remains_separate_from_queue_adapter(self):
        root = Path(__file__).parents[1]
        render_source = (root / "backend" / "render.py").read_text(encoding="utf-8")
        self.assertNotIn("/prompt", render_source)
        self.assertNotIn("queue_prompt", render_source)
        jobs_source = (root / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertNotIn("queue_prompt", jobs_source)
        self.assertNotIn("shell=True", jobs_source)

    def test_no_quality_override_or_amd_downgrade_is_in_execution_module(self):
        source = (Path(__file__).parents[1] / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertNotIn("quality_settings", source)
        self.assertNotIn("DEFERRED_TARGET_NVIDIA", source)
        self.assertIn("SUPPORTED_MULTI_GPU", source)
        self.assertIn("build_execution_eligibility", source)

    def test_job_store_rejects_wrong_project_ownership(self):
        result = self._submit()
        with self.assertRaises(Exception):
            JobStore(self.storage).load(str(uuid.uuid4()), result["job_id"])

    def test_terminal_records_are_returned_without_polling(self):
        submitted = self._submit()
        success = transition_job(submitted, SUCCEEDED, reason="test terminal")
        JobStore(self.storage).save(success)
        self.client.queue_error = ComfyUIClientError("should_not_poll", "should not poll", transient=True)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        self.assertEqual(result["jobs"][0]["state"], SUCCEEDED)
        self.assertEqual(self.client.calls["queue"], 0)

    def test_cancellation_of_terminal_job_is_idempotent(self):
        submitted = self._submit()
        success = transition_job(submitted, SUCCEEDED, reason="test terminal")
        JobStore(self.storage).save(success)
        returned = cancel_render_job(self.storage, self.project_id, submitted["job_id"], client=self.client)
        self.assertEqual(returned["state"], SUCCEEDED)
        self.assertEqual(self.client.calls["queue"], 0)

    def test_history_error_preserves_state_and_is_reported_per_job(self):
        submitted = self._submit()
        self.client.history_error = ComfyUIClientError("HISTORY_TEMPORARY", "history temporarily unavailable", transient=True)
        result = reconcile_project_jobs(self.storage, self.project_id, client=self.client)
        self.assertEqual(result["jobs"][0]["state"], QUEUED)
        self.assertEqual(result["warnings"][0]["job_id"], submitted["job_id"])

    def test_output_prefix_is_a_prompt_owned_filter_not_newest_file_scan(self):
        source = (Path(__file__).parents[1] / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertIn("expected_filename_prefix", source)
        self.assertNotIn("max(.*mtime", source)
        self.assertNotIn("newest", source.lower())

    def test_submission_does_not_accept_caller_supplied_workflow_in_service_signature(self):
        source = (Path(__file__).parents[1] / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        self.assertIn("package_loader", source)
        self.assertNotIn("request_workflow", source)
        self.assertNotIn("workflow_path", source.split("def submit_render_job", 1)[1].split("def _history_entry", 1)[0])


if __name__ == "__main__":
    unittest.main()
