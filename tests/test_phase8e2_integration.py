"""Phase 8E Round 8E.2 tests: Production Scene UI and Batch Integration.

Covers:
1. Project production settings persistence and mutation gating during active batches.
2. Production status reporting and runtime capability discovery.
3. Manual per-scene postprocess lifecycle (start, cancel, retry, isolation).
4. Batch planning for 'none' vs upscale methods (rtx_vsr_fast, seedvr2_quality).
5. Sequential batch execution advancing seamlessly from H3 -> Finalize -> Postprocess -> Production Scene.
6. Postprocess failure isolation (Final Scene remains valid, batch advances to next scene).
7. Retry failed minimum-work execution (resumes as POSTPROCESS_ONLY without rerendering H3).
8. Restart recovery of postprocessing items into PAUSED_RECOVERY or active tracking.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectValidationError
from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_FINALIZE_AND_POSTPROCESS,
    ACTION_FINALIZE_RAW,
    ACTION_POSTPROCESS_ONLY,
    ACTION_PREPARE_AND_RENDER,
    ACTION_RENDER_PREPARED,
    BATCH_COMPLETED,
    BATCH_PAUSED_RECOVERY,
    BATCH_RUNNING,
    ITEM_ALREADY_COMPLETE,
    ITEM_CANCELLED,
    ITEM_COMPLETE,
    ITEM_FINALIZING,
    ITEM_PENDING,
    ITEM_POSTPROCESSING,
    ITEM_POSTPROCESS_FAILED,
    BatchRunner,
    BatchStore,
    ensure_batch_recovery,
    new_batch_record,
    plan_batch,
    preview_batch,
    retry_failed_batch,
    start_batch,
)
from backend.render_finalize import summarize_render_job_finalization
from backend.render_jobs import SUCCEEDED, JobStore, new_job_record
from backend.render_production import (
    POSTPROCESS_ACTIVE,
    POSTPROCESS_CANCELLED,
    POSTPROCESS_FAILED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    POSTPROCESS_SUBMITTED,
    POSTPROCESS_SUCCEEDED,
    PRODUCTION_CURRENT,
    PRODUCTION_FAILED,
    PRODUCTION_NEEDS_POSTPROCESS,
    PostprocessJobStore,
    cancel_postprocess_job,
    get_project_production_status,
    new_postprocess_job_record,
    resolve_project_production_method,
    retry_postprocess_job,
    set_project_production_method,
    submit_postprocess_job,
    summarize_production_scene,
)

try:
    from phase8d_base import FINGERPRINT, Phase8DBase
    from phase8e_base import FakeComfyClientForPostprocess, FakeProductionMediaAdapter, Phase8ETestBase
except ImportError:
    from tests.phase8d_base import FINGERPRINT, Phase8DBase
    from tests.phase8e_base import FakeComfyClientForPostprocess, FakeProductionMediaAdapter, Phase8ETestBase


class TestPhase8E2ProductionSettingsAndStatus(Phase8ETestBase):
    """Test project upscale method persistence, batch mutual exclusion, and status."""

    def test_set_and_get_production_method(self):
        result = set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        self.assertEqual(result["production"]["upscale_method"], "rtx_vsr_fast")

        project = self.storage.load_project(self.project_id)
        self.assertEqual(project["production"]["upscale_method"], "rtx_vsr_fast")
        self.assertEqual(resolve_project_production_method(project), "rtx_vsr_fast")

        result2 = set_project_production_method(self.storage, self.project_id, "seedvr2_quality")
        self.assertEqual(result2["production"]["upscale_method"], "seedvr2_quality")

    def test_set_production_method_rejects_invalid(self):
        with self.assertRaises(ProjectValidationError):
            set_project_production_method(self.storage, self.project_id, "esrgan_invalid")

    def test_set_production_method_blocked_when_batch_active(self):
        store = BatchStore(self.storage)
        record = new_batch_record(self.project_id, mode="all_ready", items=[])
        record["state"] = BATCH_RUNNING
        store.save(record)

        from backend.render_production import RenderProductionError

        with self.assertRaises(RenderProductionError) as ctx:
            set_project_production_method(self.storage, self.project_id, "seedvr2_quality")
        self.assertEqual(ctx.exception.code, "PROJECT_BATCH_ACTIVE_MUTATION_BLOCKED")

    def test_get_project_production_status(self):
        self._setup_final_scene(self.scene_1_id)
        status = get_project_production_status(self.storage, self.project_id)
        self.assertEqual(status["project_id"], self.project_id)
        self.assertEqual(status["upscale_method"], "none")
        self.assertIn("capabilities", status)
        self.assertIn("none", status["capabilities"])
        self.assertIn("rtx_vsr_fast", status["capabilities"])
        self.assertIn("seedvr2_quality", status["capabilities"])
        self.assertEqual(len(status["scenes"]), 2)


class TestPhase8E2ManualPostprocessLifecycle(Phase8ETestBase):
    """Test manual per-scene postprocess start, cancel, retry, and candidate finalization."""

    def test_manual_postprocess_submit_cancel_retry(self):
        self._setup_final_scene(self.scene_1_id)
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        client = FakeComfyClientForPostprocess(prompt_id="prompt-p1-123")
        node_types = {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"}

        # Submit postprocess job
        job = submit_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            final_scene_selector="renders/test/final_scene.mp4",
            output_prefix=f"mvb_{self.project_id}_{self.scene_1_id}",
            client=client,
            node_types=node_types,
            hardware_supported=True,
        )
        self.assertEqual(job["state"], POSTPROCESS_SUBMITTED)
        self.assertEqual(job["method"], "rtx_vsr_fast")

        # Cancel job
        with patch.object(client, "interrupt", return_value=True, create=True):
            cancelled = cancel_postprocess_job(self.storage, self.project_id, self.scene_1_id, client=client)
            self.assertEqual(cancelled["state"], POSTPROCESS_CANCELLED)

        # Retry job
        client2 = FakeComfyClientForPostprocess(prompt_id="prompt-p1-retry")
        retried = retry_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            final_scene_selector="renders/test/final_scene.mp4",
            output_prefix=f"mvb_{self.project_id}_{self.scene_1_id}",
            client=client2,
            node_types=node_types,
            hardware_supported=True,
        )
        self.assertEqual(retried["state"], POSTPROCESS_SUBMITTED)
        self.assertEqual(retried["comfy_prompt_id"], "prompt-p1-retry")


class TestPhase8E2BatchPlanningAndExecution(Phase8DBase):
    """Test batch production planning and end-to-end execution with postprocessing using Phase8DBase."""

    def _make_finalized_job(self, scene_id):
        job = self.make_succeeded_job_with_final(scene_id)
        self.finalize_job_real(job)
        return JobStore(self.storage).load(self.project_id, job["job_id"])

    def test_batch_planning_none_vs_upscale_methods(self):
        self._make_finalized_job(self.scene_ids[0])

        # For method none:
        set_project_production_method(self.storage, self.project_id, "none")
        preview_none = preview_batch(
            self.storage,
            self.project_id,
            preflight_builder=self.preflight_builder,
            client=self.client,
            raw_output_root=self.raw_root,
        )
        scenes_none = {s["scene_id"]: s for s in preview_none["scenes"]}
        self.assertEqual(scenes_none[self.scene_ids[0]]["action"], ACTION_ALREADY_COMPLETE)

        # For method rtx_vsr_fast:
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with patch("backend.render_batch.production_method_availability", return_value={"method": "rtx_vsr_fast", "state": "AVAILABLE"}):
            preview_rtx = preview_batch(
                self.storage,
                self.project_id,
                preflight_builder=self.preflight_builder,
                client=self.client,
                raw_output_root=self.raw_root,
            )
            scenes_rtx = {s["scene_id"]: s for s in preview_rtx["scenes"]}
            self.assertEqual(scenes_rtx[self.scene_ids[0]]["action"], ACTION_POSTPROCESS_ONLY)

    def test_batch_postprocess_only_execution_to_completion(self):
        self._make_finalized_job(self.scene_ids[0])
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")

        items = [
            {
                "scene_id": self.scene_ids[0],
                "sequence": 1,
                "action": ACTION_POSTPROCESS_ONLY,
                "disposition": ITEM_PENDING,
                "reason_code": None,
                "job_id": None,
                "updated_at": "2026-08-17T00:00:00.000Z",
            }
        ]
        store = BatchStore(self.storage)
        record = new_batch_record(
            self.project_id,
            mode="selected",
            items=items,
            production_profile={"upscale_method": "rtx_vsr_fast"},
        )
        store.save(record)

        runner = self.make_runner()

        # Tick 1: Dispatches postprocess job -> ITEM_POSTPROCESSING
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            rec1 = runner.tick()
        self.assertEqual(rec1["items"][0]["disposition"], ITEM_POSTPROCESSING)
        pjob_id = rec1["items"][0].get("postprocess_job_id")
        self.assertIsNotNone(pjob_id)

        # Set up fake output for postprocess job
        pjob = PostprocessJobStore(self.storage).load(self.project_id, self.scene_ids[0], pjob_id)
        prompt_id = pjob["comfy_prompt_id"]
        output_prefix = pjob["output_prefix"]
        filename = f"{output_prefix}_00001.mp4"
        output_file = self.raw_root / filename
        output_file.write_bytes(b"FAKE_UPSCALED_FRAMES")
        self.client.succeed(prompt_id, "3", filename)

        # Tick 2: Observes succeeded postprocess job, finalizes candidate with authoritative audio -> ITEM_COMPLETE
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            rec2 = runner.tick()
        self.assertEqual(rec2["items"][0]["disposition"], ITEM_COMPLETE)
        self.assertEqual(rec2["state"], BATCH_COMPLETED)

        # Verify Production Scene is now CURRENT
        summary = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_ids[0],
            method="rtx_vsr_fast",
            raw_output_root=self.raw_root,
            preflight=self.preflight_builder(self.storage.load_project(self.project_id), self.storage),
        )
        self.assertEqual(summary["state"], PRODUCTION_CURRENT)
        self.assertIsNotNone(summary["output"])

    def test_batch_postprocess_failure_isolates_and_advances_to_next_scene(self):
        fjob0 = self._make_finalized_job(self.scene_ids[0])
        self._make_finalized_job(self.scene_ids[1])
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")

        items = [
            {
                "scene_id": self.scene_ids[0],
                "sequence": 1,
                "action": ACTION_POSTPROCESS_ONLY,
                "disposition": ITEM_PENDING,
                "reason_code": None,
                "job_id": None,
                "updated_at": "2026-08-17T00:00:00.000Z",
            },
            {
                "scene_id": self.scene_ids[1],
                "sequence": 2,
                "action": ACTION_POSTPROCESS_ONLY,
                "disposition": ITEM_PENDING,
                "reason_code": None,
                "job_id": None,
                "updated_at": "2026-08-17T00:00:00.000Z",
            },
        ]
        store = BatchStore(self.storage)
        record = new_batch_record(
            self.project_id,
            mode="selected",
            items=items,
            production_profile={"upscale_method": "rtx_vsr_fast"},
        )
        store.save(record)

        runner = self.make_runner()

        # Tick 1: Submit Scene 1 postprocess -> ITEM_POSTPROCESSING
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            rec1 = runner.tick()
        self.assertEqual(rec1["items"][0]["disposition"], ITEM_POSTPROCESSING)
        pjob_id = rec1["items"][0]["postprocess_job_id"]
        pjob = PostprocessJobStore(self.storage).load(self.project_id, self.scene_ids[0], pjob_id)
        prompt_id = pjob["comfy_prompt_id"]

        # Fail the ComfyUI execution for prompt_id
        self.client.fail(prompt_id)

        # Tick 2: Reconcile Scene 1 failure -> records ITEM_POSTPROCESS_FAILED, advances to Scene 2
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            rec2 = runner.tick()
        self.assertEqual(rec2["items"][0]["disposition"], ITEM_POSTPROCESS_FAILED)
        # Final Scene 1 is STILL valid!
        final_summary = summarize_render_job_finalization(self.storage, fjob0, raw_output_root=self.raw_root)
        final_file = final_summary["output"]["relative_path"]
        self.assertTrue((self.storage.project_directory(self.project_id) / final_file).exists())
        # Scene 2 was dispatched
        self.assertEqual(rec2["items"][1]["disposition"], ITEM_POSTPROCESSING)

    def test_retry_failed_minimum_work_plans_postprocess_only(self):
        self._make_finalized_job(self.scene_ids[0])
        set_project_production_method(self.storage, self.project_id, "seedvr2_quality")

        items = [
            {
                "scene_id": self.scene_ids[0],
                "sequence": 1,
                "action": ACTION_POSTPROCESS_ONLY,
                "disposition": ITEM_POSTPROCESS_FAILED,
                "reason_code": None,
                "job_id": None,
                "failure": {"code": "POSTPROCESS_FAILED", "message": "OOM", "at": "2026-08-17T00:00:00.000Z"},
                "updated_at": "2026-08-17T00:00:00.000Z",
            }
        ]
        store = BatchStore(self.storage)
        record = new_batch_record(
            self.project_id,
            mode="selected",
            items=items,
            production_profile={"upscale_method": "seedvr2_quality"},
        )
        record["state"] = BATCH_COMPLETED
        store.save(record)

        # Retry Failed
        result = retry_failed_batch(
            self.storage,
            self.project_id,
            start_runner=False,
            **self.batch_seams(),
        )
        batch = result["batch"]
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["scene_id"], self.scene_ids[0])
        # Plans as POSTPROCESS_ONLY (no H3 re-render!)
        self.assertEqual(batch["items"][0]["action"], ACTION_POSTPROCESS_ONLY)

    def test_restart_recovery_recovers_active_postprocess_job(self):
        self._make_finalized_job(self.scene_ids[0])
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")

        # Create active postprocess job
        pjob_store = PostprocessJobStore(self.storage)
        pjob = new_postprocess_job_record(
            self.project_id,
            self.scene_ids[0],
            "rtx_vsr_fast",
            production_fingerprint="a" * 64,
        )
        pjob["state"] = POSTPROCESS_ACTIVE
        pjob_store.save(pjob)

        items = [
            {
                "scene_id": self.scene_ids[0],
                "sequence": 1,
                "action": ACTION_POSTPROCESS_ONLY,
                "disposition": ITEM_POSTPROCESSING,
                "postprocess_job_id": pjob["postprocess_job_id"],
                "reason_code": None,
                "job_id": None,
                "updated_at": "2026-08-17T00:00:00.000Z",
            }
        ]
        store = BatchStore(self.storage)
        record = new_batch_record(
            self.project_id,
            mode="selected",
            items=items,
            production_profile={"upscale_method": "rtx_vsr_fast"},
        )
        record["current_index"] = 0
        store.save(record)

        recovered = ensure_batch_recovery(self.storage, self.project_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["state"], BATCH_RUNNING)
        self.assertIn("recovered", recovered)
        self.assertEqual(recovered["recovered"]["postprocess_job_id"], pjob["postprocess_job_id"])
