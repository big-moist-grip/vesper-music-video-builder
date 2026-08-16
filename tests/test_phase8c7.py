"""Phase 8C.7 orphaned-job recovery and deterministic output fallback contracts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid

from backend.projects import ProjectStorage
from backend.render_jobs import (
    ORPHANED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    UNKNOWN,
    ComfyUIClientError,
    JobStore,
    RenderOutputDiscoveryError,
    TargetHardwareGate,
    TargetHardwareQualification,
    discover_raw_output_fallback,
    new_job_record,
    reconcile_project_jobs,
    retry_render_job,
    submit_render_job,
    transition_job,
)


ROOT = Path(__file__).parents[1]


class FakeComfyClient:
    def __init__(self, prompt_ids: list[str] | None = None):
        self.prompt_ids = list(prompt_ids or [])
        self.pending: list[str] = []
        self.running: list[str] = []
        self.history: dict[str, dict[str, object]] = {}
        self.submitted: list[dict[str, object]] = []
        self.queue_error: ComfyUIClientError | None = None
        self.history_error: ComfyUIClientError | None = None
        self.calls = {"queue": 0, "history": 0, "submit": 0}

    def get_queue(self) -> dict[str, object]:
        self.calls["queue"] += 1
        if self.queue_error is not None:
            raise self.queue_error
        return {
            "queue_running": [[0, prompt_id] for prompt_id in self.running],
            "queue_pending": [[0, prompt_id] for prompt_id in self.pending],
        }

    def get_history(self, prompt_id: str) -> dict[str, object]:
        self.calls["history"] += 1
        if self.history_error is not None:
            raise self.history_error
        return self.history.get(prompt_id, {})

    def submit_prompt(self, workflow: dict[str, object]) -> dict[str, object]:
        self.calls["submit"] += 1
        prompt_id = self.prompt_ids.pop(0) if self.prompt_ids else f"prompt-8c7-{self.calls['submit']}"
        self.submitted.append(deepcopy(workflow))
        self.pending.append(prompt_id)
        return {"prompt_id": prompt_id, "queue_number": self.calls["submit"], "node_errors": {}}

    def cancellation_capabilities(self) -> dict[str, object]:
        return {"queued": True, "running": True, "running_scope": "global_engine_interrupt_targeted_by_prompt_id"}


class Phase8C7OrphanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8C.7 orphan recovery")
        self.project_id = project["project_id"]
        self.scene_id = str(uuid.uuid4())
        self.output_root = Path(self.temp_directory.name) / "comfy-output"
        self.output_root.mkdir()
        self.base_prefix = f"music_video_builder/{self.project_id}/{self.scene_id}/render"
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
                "20": {
                    "class_type": "VHS_VideoCombine",
                    "inputs": {"filename_prefix": self.base_prefix, "format": "video/h264-mp4"},
                },
                "21": {
                    "class_type": "VHS_VideoCombine",
                    "inputs": {"filename_prefix": self.base_prefix, "format": "video/h264-mp4"},
                },
            },
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": "f" * 64,
            "output_node_id": "20",
            "expected_filename_prefix": self.base_prefix,
            "output_format": "video/h264-mp4",
        }
        self.client = FakeComfyClient()

    @staticmethod
    def _good_probe(_path: Path) -> dict[str, object]:
        return {
            "video_streams": [{"codec_type": "video"}],
            "video": {"duration_ms": 1000, "width": 320, "height": 180, "frame_count": 24},
        }

    @staticmethod
    def _invalid_probe(_path: Path) -> dict[str, object]:
        return {"video_streams": [], "video": {"duration_ms": 0, "width": 0, "height": 0}}

    def _gate(self) -> TargetHardwareGate:
        return TargetHardwareGate(lambda: TargetHardwareQualification(
            True,
            "SUPPORTED_MULTI_GPU",
            "Both supported H3 execution platforms are valid.",
            "test-fixture",
        ))

    def _job(self, *, state: str = QUEUED, prompt_id: str = "prompt-8c7-old") -> dict[str, object]:
        record = new_job_record(
            self.project_id,
            self.scene_id,
            "keyframe_i2v",
            "f" * 64,
            output_node_id="20",
            expected_filename_prefix=self.base_prefix,
            output_format="video/h264-mp4",
        )
        record["production_output"]["filename_prefix"] = f"{self.base_prefix}/{record['job_id']}"
        record["production_output"]["ownership"] = {
            "kind": "builder_job_id",
            "job_id": record["job_id"],
            "prefix": record["production_output"]["filename_prefix"],
        }
        record = transition_job(record, "SUBMITTING", reason="test_submit", updates={"comfy_prompt_id": prompt_id})
        record = transition_job(record, QUEUED, reason="test_queue")
        if state == RUNNING:
            record = transition_job(record, RUNNING, reason="test_running")
        elif state == UNKNOWN:
            record = transition_job(record, UNKNOWN, reason="test_unknown")
        elif state == ORPHANED:
            record = transition_job(record, ORPHANED, reason="test_orphaned", updates={
                "failure": {
                    "category": "job_orphaned",
                    "code": "COMFYUI_JOB_ORPHANED",
                    "message": "The previous ComfyUI job is no longer available.",
                },
            })
        JobStore(self.storage).save(record)
        return record

    def _reconcile(self, *, media_probe=None) -> dict[str, object]:
        return reconcile_project_jobs(
            self.storage,
            self.project_id,
            client=self.client,
            output_root=self.output_root,
            media_probe=media_probe or self._good_probe,
        )

    def _write_candidates(self, record: dict[str, object], names: list[str]) -> list[Path]:
        prefix = str(record["production_output"]["filename_prefix"])
        parts = prefix.replace("\\", "/").split("/")
        parent = self.output_root.joinpath(*parts[:-1])
        parent.mkdir(parents=True, exist_ok=True)
        targets = []
        for name in names:
            target = parent / name
            target.write_bytes(b"raw h3 output")
            targets.append(target)
        return targets

    def _submit(self, *, client: FakeComfyClient | None = None) -> dict[str, object]:
        return submit_render_job(
            self.storage,
            self.project_id,
            self.scene_id,
            client=client or self.client,
            hardware_gate=self._gate(),
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
            compatibility_validator=lambda *_args: {"compatible": True, "source": "test"},
        )

    def test_one_absence_confirmation_is_transient_reconciliation(self):
        record = self._job()
        result = self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["code"], "RECONCILIATION_REQUIRED")
        self.assertEqual(job["reconciliation"]["absence_confirmations"], 1)
        self.assertEqual(result["jobs"][0]["state"], UNKNOWN)

    def test_second_confirmed_absence_becomes_terminal_orphaned_and_stops_polling(self):
        record = self._job()
        self._reconcile()
        self.assertEqual(JobStore(self.storage).load(self.project_id, record["job_id"])["state"], UNKNOWN)
        self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], ORPHANED)
        self.assertEqual(job["failure"]["code"], "COMFYUI_JOB_ORPHANED")
        self.assertEqual(job["reconciliation"]["state"], "ORPHANED")
        queue_calls = self.client.calls["queue"]
        self._reconcile()
        self.assertEqual(self.client.calls["queue"], queue_calls)

    def test_queue_transport_failure_never_counts_as_absence(self):
        record = self._job()
        self.client.queue_error = ComfyUIClientError("COMFYUI_UNAVAILABLE", "temporary", transient=True)
        result = self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], QUEUED)
        self.assertIsNone(job["reconciliation"])
        self.assertEqual(result["warnings"][0]["code"], "COMFYUI_UNAVAILABLE")

    def test_history_transport_failure_never_counts_as_absence(self):
        record = self._job()
        self.client.history_error = ComfyUIClientError("COMFYUI_UNAVAILABLE", "temporary", transient=True)
        self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], QUEUED)
        self.assertIsNone(job["reconciliation"])
        self.assertEqual(job["last_poll_error"]["code"], "COMFYUI_UNAVAILABLE")

    def test_prompt_returning_to_queue_releases_transient_reconciliation(self):
        record = self._job()
        self._reconcile()
        self.client.pending.append(record["comfy_prompt_id"])
        self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], QUEUED)
        self.assertIsNone(job["reconciliation"])
        self.assertIsNone(job["failure"])

    def test_history_independent_fallback_recovers_one_owned_complete_video(self):
        record = self._job()
        filename = f"{record['job_id']}_00001_.mp4"
        self._write_candidates(record, [filename])
        result = self._reconcile()
        job = result["jobs"][0]

        self.assertEqual(job["state"], SUCCEEDED)
        self.assertEqual(job["output"]["discovery_source"], "deterministic_job_output_prefix")
        self.assertTrue(job["output"]["media_validation"]["validated"])
        self.assertFalse(job["output"]["history_verified"])
        self.assertTrue(job["output"]["relative_path"].endswith(f"/{filename}"))

    def test_fallback_requires_media_validation(self):
        record = self._job()
        self._write_candidates(record, [f"{record['job_id']}_00001_.mp4"])
        self._reconcile(media_probe=self._invalid_probe)
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["details"]["fallback"]["code"], "OUTPUT_FALLBACK_MEDIA_INVALID")

    def test_ambiguous_fallback_fails_closed_then_orphans(self):
        record = self._job()
        self._write_candidates(record, [
            f"{record['job_id']}_00001_.mp4",
            f"{record['job_id']}_00002_.mp4",
        ])
        self._reconcile()
        first = JobStore(self.storage).load(self.project_id, record["job_id"])
        self._reconcile()
        second = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(first["state"], UNKNOWN)
        self.assertEqual(first["failure"]["details"]["fallback"]["code"], "OUTPUT_FALLBACK_AMBIGUOUS")
        self.assertEqual(second["state"], ORPHANED)
        self.assertIsNone(second["output"])

    def test_corrupt_fallback_never_becomes_success(self):
        record = self._job()
        self._write_candidates(record, [f"{record['job_id']}_00001_.mp4"])
        self._reconcile(media_probe=self._invalid_probe)
        self._reconcile(media_probe=self._invalid_probe)
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], ORPHANED)
        self.assertNotEqual(job["state"], SUCCEEDED)

    def test_fallback_rejects_unsafe_prefix_before_scanning(self):
        record = self._job()
        record["production_output"]["filename_prefix"] = "../outside/job"

        with self.assertRaises(RenderOutputDiscoveryError) as context:
            discover_raw_output_fallback(record, output_root=self.output_root, media_probe=self._good_probe)
        self.assertEqual(context.exception.code, "OUTPUT_PATH_UNSAFE")

    def test_fallback_ignores_non_video_candidates_and_reports_truthful_failure(self):
        record = self._job()
        self._write_candidates(record, [f"{record['job_id']}_00001_.txt"])
        self._reconcile()
        job = JobStore(self.storage).load(self.project_id, record["job_id"])

        self.assertEqual(job["state"], UNKNOWN)
        self.assertEqual(job["failure"]["details"]["fallback"]["code"], "OUTPUT_FALLBACK_NOT_FOUND")
        self.assertNotIn("not selected", job["failure"]["message"].lower())

    def test_orphaned_job_can_be_retried_without_mutating_old_job(self):
        old = self._job(state=ORPHANED, prompt_id="prompt-8c7-old")
        old_before = deepcopy(old)
        self.client.prompt_ids = ["prompt-8c7-new"]

        new = retry_render_job(
            self.storage,
            self.project_id,
            old["job_id"],
            client=self.client,
            hardware_gate=self._gate(),
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
            compatibility_validator=lambda *_args: {"compatible": True, "source": "test"},
        )
        old_after = JobStore(self.storage).load(self.project_id, old["job_id"])

        self.assertEqual(old_after, old_before)
        self.assertNotEqual(new["job_id"], old["job_id"])
        self.assertEqual(new["retry_of_job_id"], old["job_id"])
        self.assertEqual(new["comfy_prompt_id"], "prompt-8c7-new")
        self.assertEqual(new["state"], QUEUED)

    def test_submission_uses_job_specific_namespace_and_leaves_preparation_unchanged(self):
        package_before = deepcopy(self.package)
        submitted = self._submit()
        workflow = self.client.submitted[0]
        execution_prefix = submitted["production_output"]["filename_prefix"]

        self.assertTrue(execution_prefix.startswith(f"{self.base_prefix}/"))
        self.assertTrue(execution_prefix.endswith(submitted["job_id"]))
        self.assertEqual(submitted["production_output"]["prepared_filename_prefix"], self.base_prefix)
        self.assertEqual(submitted["preparation_fingerprint"], "f" * 64)
        self.assertEqual(self.package, package_before)
        self.assertEqual(workflow["20"]["inputs"]["filename_prefix"], execution_prefix)
        self.assertEqual(workflow["21"]["inputs"]["filename_prefix"], self.base_prefix)

    def test_retry_execution_prefix_is_distinct_while_fingerprint_is_stable(self):
        old = self._job(state=ORPHANED, prompt_id="prompt-8c7-old")
        self.client.prompt_ids = ["prompt-8c7-new"]
        new = retry_render_job(
            self.storage,
            self.project_id,
            old["job_id"],
            client=self.client,
            hardware_gate=self._gate(),
            preflight=self.preflight,
            package_loader=lambda *_args: self.package,
            compatibility_validator=lambda *_args: {"compatible": True},
        )

        self.assertNotEqual(old["production_output"]["filename_prefix"], new["production_output"]["filename_prefix"])
        self.assertEqual(old["preparation_fingerprint"], new["preparation_fingerprint"])
        self.assertEqual(new["production_output"]["ownership"]["job_id"], new["job_id"])
        self.assertIsNone(new["comfy_instance_id"])

    def test_execution_prefix_patch_is_fail_closed_when_manifest_output_node_is_missing(self):
        package = deepcopy(self.package)
        package["workflow"].pop("20")

        with self.assertRaises(Exception) as context:
            submit_render_job(
                self.storage,
                self.project_id,
                self.scene_id,
                client=self.client,
                hardware_gate=self._gate(),
                preflight=self.preflight,
                package_loader=lambda *_args: package,
                compatibility_validator=lambda *_args: {"compatible": True},
            )
        self.assertIn("production output node", str(context.exception).lower())

    def test_orphaned_state_is_not_active_in_frontend_and_retry_is_available(self):
        source = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn('ORPHANED: "ORPHANED"', source)
        self.assertIn("Boolean(job && renderJobIsActive(job))", source)
        self.assertIn('["FAILED", "CANCELLED", "INTERRUPTED", "ORPHANED"]', source)
        self.assertIn('"UNKNOWN"].includes(job?.state)', source)

    def test_frontend_reconciliation_shell_excludes_volatile_poll_fields(self):
        source = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("progress: null", source)
        self.assertIn("telemetry: null", source)
        self.assertIn("updated_at: null", source)
        self.assertIn("last_reconciled_at: null", source)
        self.assertIn("last_poll_error: null", source)

    def test_frontend_uses_input_readiness_wording_for_top_badge(self):
        source = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn('stateBadge.textContent = job && renderJobIsActive(job) ? renderJobLabel(job.state) : ready ? "INPUTS READY" : "BLOCKED"', source)
        self.assertNotIn('stateBadge.textContent = ready ? "READY" : "BLOCKED"', source)


if __name__ == "__main__":
    unittest.main()
