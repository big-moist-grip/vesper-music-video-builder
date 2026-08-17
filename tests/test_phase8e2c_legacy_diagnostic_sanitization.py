"""Phase 8E.2C legacy diagnostic sanitization and presentation boundary test suite.

Validates that:
1. sanitize_user_facing_message handles strings, dicts, lists, tuples, Exceptions, None.
2. Raw execution telemetry, prompt_ids, node_ids, and Python dict reprs are never exposed.
3. Historical persisted job records on disk with legacy raw diagnostic strings load safely.
4. Product-facing error messages remain clean, actionable, and non-empty.
5. Postprocess jobs and batch records sanitize failure messages at presentation boundaries.
6. Frontend presentation in extension.js fails safe against raw technical payloads.
7. Blocker UX and 8E.2B behaviors remain fully intact.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from backend.projects import ProjectStorage, atomic_write_json
from backend.render_batch import (
    BatchStore,
    public_batch_record,
    sanitize_batch_record_for_presentation,
)
from backend.render_jobs import (
    JobStore,
    _history_status,
    _history_terminal_state,
    new_job_record,
    reconcile_project_jobs,
    sanitize_job_record_for_presentation,
    sanitize_user_facing_message,
)
from backend.render_production import (
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    PostprocessJobStore,
    sanitize_postprocess_job_record_for_presentation,
    summarize_production_scene,
)

try:
    from phase8e_base import Phase8ETestBase
except ImportError:
    from tests.phase8e_base import Phase8ETestBase


ROOT = Path(__file__).parents[1]
EXTENSION_JS = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")


class Phase8E2CSanitizerUnitTests(unittest.TestCase):
    def test_01_sanitizer_handles_none_and_empty(self):
        self.assertEqual(sanitize_user_facing_message(None), "The render job failed.")
        self.assertEqual(sanitize_user_facing_message(""), "The render job failed.")
        self.assertEqual(sanitize_user_facing_message("   "), "The render job failed.")
        self.assertEqual(sanitize_user_facing_message(None, fallback="Custom fallback."), "Custom fallback.")

    def test_02_sanitizer_preserves_legitimate_product_messages(self):
        legitimate = [
            "Render failed.",
            "Render was interrupted.",
            "ComfyUI is unavailable.",
            "Required model is missing.",
            "Production scene validation failed.",
            "Selected upscaler is unavailable on this runtime.",
            "Preparation is stale.",
            "Final scene ready.",
        ]
        for msg in legitimate:
            self.assertEqual(sanitize_user_facing_message(msg), msg)

    def test_03_sanitizer_rejects_raw_python_dict_repr(self):
        raw_dict_repr = "{'prompt_id': 'cb3cd975-1234', 'timestamp': 1786938969625, 'nodes': []}"
        self.assertEqual(
            sanitize_user_facing_message(raw_dict_repr),
            "The render job failed.",
        )

    def test_04_sanitizer_rejects_concatenated_comfyui_history_messages(self):
        live_defect_string = (
            "{'prompt_id': 'cb3cd975-0000', 'timestamp': 123, 'nodes': []}, "
            "prompt_id: cb3cd975, timestamp: 123, "
            "{'prompt_id': 'cb3cd975', 'node_id': '11', 'node_type': 'SolidMask', 'executed': [4, '3'], 'timestamp': 124}"
        )
        self.assertEqual(
            sanitize_user_facing_message(live_defect_string),
            "The render job failed.",
        )

    def test_05_sanitizer_extracts_clean_exception_from_dict(self):
        dict_payload = {
            "prompt_id": "cb3cd975-5678",
            "node_id": "12",
            "node_type": "SeedVR2VideoUpscaler",
            "exception_message": "CUDA out of memory in SeedVR2 attention forward\nRuntimeError: out of memory",
        }
        self.assertEqual(
            sanitize_user_facing_message(dict_payload),
            "RuntimeError: out of memory",
        )

    def test_06_sanitizer_handles_exception_objects(self):
        exc = RuntimeError("CUDA device unavailable")
        self.assertEqual(
            sanitize_user_facing_message(exc),
            "CUDA device unavailable",
        )

    def test_07_sanitizer_handles_history_messages_tuple_list(self):
        history_messages = [
            ("execution_start", {"prompt_id": "12345", "timestamp": 100, "nodes": []}),
            ("execution_cached", {"prompt_id": "12345", "timestamp": 101, "nodes": ["1", "2"]}),
            ("executed", {"prompt_id": "12345", "node_id": "11", "node_type": "SolidMask", "executed": [4, "3"], "timestamp": 102}),
            ("execution_error", {"prompt_id": "12345", "node_id": "12", "node_type": "KSampler", "exception_message": "ValueError: Invalid seed value"}),
        ]
        self.assertEqual(
            sanitize_user_facing_message(history_messages),
            "ValueError: Invalid seed value",
        )

    def test_08_history_status_extracts_only_clean_error(self):
        entry = {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_start", {"prompt_id": "cb3cd975", "timestamp": 123, "nodes": []}],
                    ["executed", {"prompt_id": "cb3cd975", "node_id": "11", "node_type": "SolidMask", "executed": [4, "3"], "timestamp": 124}],
                    ["execution_error", {"prompt_id": "cb3cd975", "node_id": "12", "exception_message": "RuntimeError: CUDA out of memory"}],
                ],
            }
        }
        status_name, status_message = _history_status(entry)
        self.assertEqual(status_name, "error")
        self.assertEqual(status_message, "RuntimeError: CUDA out of memory")

    def test_09_history_status_without_error_messages_falls_back_cleanly(self):
        entry = {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_start", {"prompt_id": "cb3cd975", "timestamp": 123, "nodes": []}],
                    ["executed", {"prompt_id": "cb3cd975", "node_id": "11", "node_type": "SolidMask", "executed": [4, "3"], "timestamp": 124}],
                ],
            }
        }
        status_name, status_message = _history_status(entry)
        self.assertEqual(status_name, "error")
        self.assertEqual(status_message, "ComfyUI reported an execution error.")


class Phase8E2CLegacyPersistedJobTests(Phase8ETestBase):
    def test_10_legacy_persisted_job_with_raw_dict_loads_sanitized(self):
        job = new_job_record(
            self.project_id,
            self.scene_1_id,
            "keyframe_i2v",
            "fake-fingerprint",
            output_node_id="10",
            expected_filename_prefix="test_h3",
        )
        job["state"] = "FAILED"
        raw_defect_string = (
            "{'prompt_id': 'cb3cd975-0000', 'timestamp': 123, 'nodes': []}, "
            "prompt_id: cb3cd975, timestamp: 123, "
            "{'prompt_id': 'cb3cd975', 'node_id': '11', 'node_type': 'SolidMask', 'executed': [4, '3'], 'timestamp': 124}"
        )
        job["failure"] = {
            "category": "execution_failure",
            "code": "COMFYUI_EXECUTION_FAILED",
            "message": raw_defect_string,
        }

        # Write directly to disk as if created by an older version of the builder
        root = self.storage.project_directory(self.project_id)
        job_file = root / "renders" / self.scene_1_id / "jobs" / f"{job['job_id']}.json"
        job_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(job_file, job)

        # 1. Verify disk file retains raw text for forensic preservation
        persisted_raw = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted_raw["failure"]["message"], raw_defect_string)

        # 2. Verify JobStore.load sanitizes the message on read
        store = JobStore(self.storage)
        loaded = store.load(self.project_id, job["job_id"])
        self.assertEqual(loaded["failure"]["message"], "The render job failed.")

        # 3. Verify JobStore.list_scene sanitizes the message on read
        scene_jobs = store.list_scene(self.project_id, self.scene_1_id)
        self.assertEqual(len(scene_jobs), 1)
        self.assertEqual(scene_jobs[0]["failure"]["message"], "The render job failed.")

        # 4. Verify JobStore.list_project sanitizes the message on read
        project_jobs = store.list_project(self.project_id)
        self.assertEqual(len(project_jobs), 1)
        self.assertEqual(project_jobs[0]["failure"]["message"], "The render job failed.")

    def test_11_reconciliation_returns_sanitized_jobs_for_legacy_records(self):
        job = new_job_record(
            self.project_id,
            self.scene_1_id,
            "keyframe_i2v",
            "fake-fingerprint",
            output_node_id="10",
            expected_filename_prefix="test_h3",
        )
        job["state"] = "FAILED"
        job["failure"] = {
            "category": "execution_failure",
            "code": "COMFYUI_EXECUTION_FAILED",
            "message": "{'prompt_id': 'cb3cd975-0000', 'nodes': []}",
        }
        root = self.storage.project_directory(self.project_id)
        job_file = root / "renders" / self.scene_1_id / "jobs" / f"{job['job_id']}.json"
        job_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(job_file, job)

        reconciled = reconcile_project_jobs(self.storage, self.project_id)
        reconciled_job = reconciled["jobs"][0]
        self.assertEqual(reconciled_job["failure"]["message"], "The render job failed.")


class Phase8E2CProductionAndBatchSanitizationTests(Phase8ETestBase):
    def test_12_postprocess_job_with_legacy_message_loads_sanitized(self):
        pjob_store = PostprocessJobStore(self.storage)
        raw_msg = "{'prompt_id': 'post-1234', 'node_id': '99', 'executed': []}"
        pjob = {
            "project_id": self.project_id,
            "scene_id": self.scene_1_id,
            "postprocess_job_id": "33333333-3333-4333-8333-333333333333",
            "state": "FAILED",
            "method": POSTPROCESS_METHOD_SEEDVR2_QUALITY,
            "failure": {
                "code": "POSTPROCESS_FAILED",
                "message": raw_msg,
            },
            "created_at": "2026-08-17T12:00:00Z",
        }
        pjob_store.save(pjob)

        loaded = pjob_store.load(self.project_id, self.scene_1_id, pjob["postprocess_job_id"])
        self.assertEqual(loaded["failure"]["message"], "Production upscale failed; retry is available.")

        scene_pjobs = pjob_store.list_scene(self.project_id, self.scene_1_id)
        self.assertEqual(scene_pjobs[0]["failure"]["message"], "Production upscale failed; retry is available.")

    def test_13_batch_record_with_legacy_message_sanitizes_presentation(self):
        batch_store = BatchStore(self.storage)
        raw_msg = "{'prompt_id': 'batch-prompt', 'node_type': 'SolidMask'}"
        batch = {
            "batch_schema_version": 1,
            "project_id": self.project_id,
            "batch_id": "44444444-4444-4444-8444-444444444444",
            "state": "COMPLETED_WITH_ISSUES",
            "mode": "all_ready",
            "production_profile": {"upscale_method": "none"},
            "created_at": "2026-08-17T12:00:00Z",
            "started_at": "2026-08-17T12:00:01Z",
            "completed_at": "2026-08-17T12:00:02Z",
            "items": [
                {
                    "scene_id": self.scene_1_id,
                    "sequence": 1,
                    "action": "PREPARE_AND_RENDER",
                    "disposition": "FAILED",
                    "reason_code": "RENDER_FAILED",
                    "failure": {"code": "COMFYUI_EXECUTION_FAILED", "message": raw_msg},
                }
            ],
            "attention": {"code": "BATCH_ERROR", "message": raw_msg},
        }
        batch_store.save(batch)

        loaded = batch_store.load(self.project_id, batch["batch_id"])
        self.assertEqual(loaded["items"][0]["failure"]["message"], "Batch item failed.")
        self.assertEqual(loaded["attention"]["message"], "Batch paused.")

        public = public_batch_record(loaded)
        self.assertEqual(public["items"][0]["failure"]["message"], "Batch item failed.")
        self.assertEqual(public["attention"]["message"], "Batch paused.")


class Phase8E2CFrontendPresentationContracts(unittest.TestCase):
    def test_14_extension_js_has_robust_diagnostic_filter(self):
        self.assertIn('function renderDiagnosticText(entry)', EXTENSION_JS)
        self.assertIn('raw.includes("status_str")', EXTENSION_JS)
        self.assertIn('raw.includes("node_type")', EXTENSION_JS)
        self.assertIn('raw.includes("executed")', EXTENSION_JS)
        self.assertIn('"Render inputs require attention."', EXTENSION_JS)

    def test_15_extension_js_never_exposes_forbidden_prompt_id_identifier(self):
        self.assertNotIn("prompt_id", EXTENSION_JS)
        self.assertNotIn("batch_id", EXTENSION_JS)


if __name__ == "__main__":
    unittest.main()
