"""Phase 8C.6 durable job reconciliation and telemetry-deadlock contracts."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
import uuid

from backend.projects import ProjectStorage
from backend.render_jobs import (
    CANCEL_REQUESTED,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    UNKNOWN,
    ComfyUIClientError,
    JobStore,
    _overlay_telemetry_jobs,
    new_job_record,
    reconcile_project_jobs,
    transition_job,
)
from backend.render_telemetry import ComfyUITelemetryAdapter, reset_shared_telemetry_adapters


ROOT = Path(__file__).parents[1]
PROMPT_ID = "prompt-reconcile-8c6"
WORKFLOW = {
    "18": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
    "21": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


def event(event_type: str, prompt_id: str = PROMPT_ID, **data: object) -> dict[str, object]:
    return {"type": event_type, "data": {"prompt_id": prompt_id, **data}}


class FakeComfyClient:
    def __init__(self, adapter: ComfyUITelemetryAdapter):
        self.telemetry = adapter
        self.telemetry_worker_enabled = False
        self.running: list[str] = []
        self.pending: list[str] = []
        self.history: dict[str, dict[str, object]] = {}
        self.history_error: ComfyUIClientError | None = None
        self.calls = {"queue": 0, "history": 0}

    def get_queue(self):
        self.calls["queue"] += 1
        return {
            "queue_running": [[0, prompt_id] for prompt_id in self.running],
            "queue_pending": [[0, prompt_id] for prompt_id in self.pending],
        }

    def get_history(self, prompt_id: str):
        self.calls["history"] += 1
        if self.history_error is not None:
            raise self.history_error
        return self.history.get(prompt_id, {})

    def cancellation_capabilities(self):
        return {"queued": True, "running": True, "running_scope": "global_engine_interrupt_targeted_by_prompt_id"}


class Phase8C6ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8C.6 reconciliation")
        self.project_id = project["project_id"]
        self.scene_id = str(uuid.uuid4())
        self.output_prefix = f"music_video_builder/{self.project_id}/{self.scene_id}/render"
        self.adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "test-reconcile-8c6",
        )
        self.addCleanup(self.adapter.close)
        self.client = FakeComfyClient(self.adapter)

    def tearDown(self):
        reset_shared_telemetry_adapters()

    def _job(self, state: str = QUEUED, prompt_id: str = PROMPT_ID) -> dict[str, object]:
        record = new_job_record(
            self.project_id,
            self.scene_id,
            "keyframe_i2v",
            "f" * 64,
            output_node_id="21",
            expected_filename_prefix=self.output_prefix,
            output_format="video/h264-mp4",
        )
        record = transition_job(record, "SUBMITTING", reason="test_submit", updates={"comfy_prompt_id": prompt_id})
        record = transition_job(record, QUEUED, reason="test_queue")
        if state == RUNNING:
            record = transition_job(record, RUNNING, reason="test_running")
        elif state == UNKNOWN:
            record = transition_job(record, UNKNOWN, reason="test_unknown")
        elif state == CANCEL_REQUESTED:
            record = transition_job(record, CANCEL_REQUESTED, reason="test_cancel_requested", updates={"cancel": {"api_acknowledged": False}})
        JobStore(self.storage).save(record)
        return record

    def _success_history(self, prompt_id: str = PROMPT_ID) -> dict[str, object]:
        return {
            "prompt_id": prompt_id,
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {
                "21": {
                    "gifs": [{
                        "filename": "render_00001_.mp4",
                        "subfolder": self.output_prefix,
                        "type": "output",
                    }],
                },
            },
        }

    def _output_root(self) -> Path:
        root = Path(self.temp_directory.name) / "comfy-output"
        target = root / self.output_prefix / "render_00001_.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"raw h3 output")
        return root

    def _reconcile(self, *, output_root: Path | None = None) -> dict[str, object]:
        return reconcile_project_jobs(
            self.storage,
            self.project_id,
            client=self.client,
            output_root=output_root,
        )

    def test_queue_running_wins_without_any_websocket_event(self):
        record = self._job()
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)

        result = self._reconcile()
        job = next(item for item in result["jobs"] if item["job_id"] == record["job_id"])

        self.assertEqual(job["state"], RUNNING)
        self.assertNotEqual(job["state"], UNKNOWN)
        self.assertFalse(job["telemetry"]["available"])
        self.assertFalse(job["telemetry"]["observed"])
        self.assertEqual(job["progress"]["kind"], "lifecycle")
        self.assertNotEqual(job["failure"], {"code": "RECONCILIATION_REQUIRED"})

    def test_history_404_does_not_hide_confirmed_running_prompt(self):
        self._job()
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)
        self.client.history_error = ComfyUIClientError(
            "COMFYUI_HTTP_ERROR",
            "history entry is not available yet",
            details={"http_status": 404},
        )

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], RUNNING)
        self.assertFalse(result["warnings"])

    def test_history_transport_failure_does_not_override_confirmed_queue_state(self):
        self._job()
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)
        self.client.history_error = ComfyUIClientError(
            "COMFYUI_UNAVAILABLE",
            "history temporarily unavailable",
            transient=True,
            details={"http_status": 503},
        )

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], RUNNING)
        self.assertEqual(result["warnings"][0]["code"], "COMFYUI_UNAVAILABLE")
        self.assertEqual(job["last_poll_error"]["code"], "COMFYUI_UNAVAILABLE")

    def test_pending_queue_is_truthful_without_telemetry_and_keeps_duplicate_submit_blocked(self):
        self._job()
        self.client.pending.append(PROMPT_ID)

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], QUEUED)
        self.assertFalse(job["telemetry"]["available"])
        self.assertFalse(job["telemetry"]["observed"])
        self.assertEqual(job["progress"]["label"], "Queued")
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertIn("Boolean(job && renderJobIsActive(job))", extension)

    def test_missing_queue_and_empty_history_becomes_unknown_without_telemetry_panel(self):
        self._job()

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["code"], "RECONCILIATION_REQUIRED")
        self.assertNotIn("telemetry", job)

    def test_history_transport_failure_without_queue_preserves_last_state_for_retry(self):
        self._job(state=RUNNING)
        self.client.history_error = ComfyUIClientError("COMFYUI_UNAVAILABLE", "temporary", transient=True)

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], RUNNING)
        self.assertNotEqual(job["failure"], {"code": "RECONCILIATION_REQUIRED"})
        self.assertEqual(job["last_poll_error"]["code"], "COMFYUI_UNAVAILABLE")

    def test_missed_completion_history_success_overrides_stale_running_telemetry(self):
        record = self._job()
        self.client.pending.clear()
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("executing", node="18"))
        self.adapter.handle_message(event("progress", value=73, max=100, node="18"))
        self.client.history[PROMPT_ID] = self._success_history()
        output_root = self._output_root()

        result = self._reconcile(output_root=output_root)
        job = next(item for item in result["jobs"] if item["job_id"] == record["job_id"])

        self.assertEqual(job["state"], SUCCEEDED)
        self.assertEqual(job["output"]["relative_path"], f"{self.output_prefix}/render_00001_.mp4")
        self.assertEqual(job["output_discovery"]["state"], "AVAILABLE")
        self.assertNotIn("telemetry", job)
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))

    def test_history_failure_overrides_stale_running_telemetry(self):
        self._job()
        self.client.pending.clear()
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("progress", value=80, max=100, node="18"))
        self.client.history[PROMPT_ID] = {
            "prompt_id": PROMPT_ID,
            "status": {"status_str": "error", "completed": True, "messages": [["execution", "node failed"]]},
            "outputs": {},
        }

        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], FAILED)
        self.assertEqual(job["failure"]["code"], "COMFYUI_EXECUTION_FAILED")
        self.assertNotIn("telemetry", job)
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))

    def test_history_interruption_maps_to_terminal_interrupted_state(self):
        self._job()
        self.client.pending.clear()
        self.client.history[PROMPT_ID] = {
            "prompt_id": PROMPT_ID,
            "status": {"status_str": "interrupted", "completed": True, "messages": []},
            "outputs": {},
        }

        result = self._reconcile()

        self.assertEqual(result["jobs"][0]["state"], INTERRUPTED)
        self.assertEqual(result["jobs"][0]["failure"]["code"], "COMFYUI_EXECUTION_INTERRUPTED")

    def test_history_success_with_missing_raw_file_stays_comfyui_succeeded(self):
        self._job()
        self.client.pending.clear()
        self.client.history[PROMPT_ID] = self._success_history()

        result = self._reconcile(output_root=Path(self.temp_directory.name) / "missing-output")
        job = result["jobs"][0]

        self.assertEqual(job["state"], SUCCEEDED)
        self.assertIsNone(job["failure"])
        self.assertIsNone(job["output"])
        self.assertEqual(job["output_discovery"]["state"], "FAILED")
        self.assertEqual(job["output_discovery"]["failure"]["category"], "output_discovery_failure")

    def test_cancel_requested_remains_cancel_requested_while_queue_confirms_active(self):
        self._job(state=CANCEL_REQUESTED)
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)

        result = self._reconcile()

        self.assertEqual(result["jobs"][0]["state"], CANCEL_REQUESTED)
        self.assertEqual(result["jobs"][0]["progress"]["label"], "Cancellation requested")

    def test_persisted_running_job_reconciles_after_store_reopen(self):
        record = self._job(state=RUNNING)
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)
        reopened_storage = ProjectStorage(Path(self.temp_directory.name) / "projects")

        result = reconcile_project_jobs(reopened_storage, self.project_id, client=self.client)
        persisted = JobStore(reopened_storage).load(self.project_id, record["job_id"])

        self.assertEqual(result["jobs"][0]["state"], RUNNING)
        self.assertEqual(persisted["state"], RUNNING)
        self.assertGreaterEqual(self.client.calls["queue"], 1)
        self.assertGreaterEqual(self.client.calls["history"], 1)

    def test_reconciliation_reads_queue_on_every_active_request_after_telemetry_reconnect(self):
        self._job()
        self.client.pending.clear()
        self.client.running.append(PROMPT_ID)
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("progress", value=10, max=100, node="18"))
        first = self._reconcile()
        first_queue_calls = self.client.calls["queue"]
        self.adapter.clear_prompt(PROMPT_ID)
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        second = self._reconcile()

        self.assertEqual(first["jobs"][0]["state"], RUNNING)
        self.assertEqual(second["jobs"][0]["state"], RUNNING)
        self.assertGreater(self.client.calls["queue"], first_queue_calls)
        self.assertNotEqual(second["jobs"][0]["state"], UNKNOWN)

    def test_overlay_exposes_unavailable_snapshot_without_promoting_lifecycle_progress(self):
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        record = {"job_id": "job", "comfy_prompt_id": PROMPT_ID, "state": RUNNING, "progress": {"kind": "lifecycle"}}

        overlay = _overlay_telemetry_jobs([record], self.client)[0]

        self.assertFalse(overlay["telemetry"]["observed"])
        self.assertFalse(overlay["telemetry"]["available"])
        self.assertEqual(overlay["progress"]["kind"], "lifecycle")

    def test_observed_telemetry_is_volatile_and_clears_after_terminal_http_reconciliation(self):
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("progress", value=33, max=100, node="18"))
        record = {"job_id": "job", "comfy_prompt_id": PROMPT_ID, "state": RUNNING, "progress": {"kind": "lifecycle"}}

        overlay = _overlay_telemetry_jobs([record], self.client)[0]

        self.assertEqual(overlay["telemetry"]["observed"], True)
        self.assertEqual(overlay["progress"]["percent"], 33)
        self.assertNotIn("telemetry", record)

    def test_stale_progress_is_not_represented_as_current_numeric_progress(self):
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("progress", value=33, max=100, node="18"))
        with self.adapter._lock:
            self.adapter._snapshots[PROMPT_ID]["_last_event_monotonic"] = time.monotonic() - 11.0
        record = {"job_id": "job", "comfy_prompt_id": PROMPT_ID, "state": RUNNING, "progress": {"kind": "lifecycle"}}

        overlay = _overlay_telemetry_jobs([record], self.client)[0]

        self.assertTrue(overlay["telemetry"]["telemetry_stale"])
        self.assertEqual(overlay["progress"]["kind"], "lifecycle")


class Phase8C6FrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

    def test_frontend_has_one_http_poll_coordinator_and_no_websocket_client(self):
        self.assertEqual(self.extension.count("function loadRenderJobs("), 1)
        self.assertEqual(self.extension.count("function scheduleRenderJobPoll("), 1)
        self.assertNotIn("new WebSocket", self.extension)
        self.assertIn("void loadRenderJobs(root, true)", self.extension)
        self.assertIn("if (!jobs.some(renderJobIsActive))", self.extension)

    def test_telemetry_is_observation_only_and_unknown_does_not_show_running_panel(self):
        start = self.extension.index("function renderTelemetryPresentation")
        end = self.extension.index("function createRenderJobTelemetry")
        telemetry = self.extension[start:end]
        self.assertIn('renderJobIsActive(job) && job.state !== "UNKNOWN"', telemetry)
        self.assertIn('telemetry?.telemetry_stale !== true', telemetry)
        self.assertIn("panel.hidden = !active", telemetry)
        self.assertNotIn("telemetry?.observed === true", telemetry)

    def test_unknown_state_is_presented_as_reconciliation_checking_not_running(self):
        self.assertIn("Checking ComfyUI queue/history", self.extension)
        self.assertNotIn("ComfyUI no longer reports this prompt; reconcile before retrying.", self.extension)
        self.assertIn("Boolean(job && renderJobIsActive(job))", self.extension)

    def test_completion_projection_drives_raw_output_surface(self):
        self.assertIn("function renderCompletionProjection(job)", self.extension)
        self.assertIn("completion?.finalization_allowed === true", self.extension)
        self.assertIn("renderRawOutputAssociationState(job)", self.extension)
        self.assertIn("renderCompletionProjectionIsValid(job)", self.extension)
        self.assertIn("Render lifecycle projection was invalid", self.extension)
        self.assertNotIn("job.output_discovery", self.extension)
        self.assertNotIn("job?.output", self.extension)
        self.assertIn('job?.state === "UNKNOWN"', self.extension)


if __name__ == "__main__":
    unittest.main()
