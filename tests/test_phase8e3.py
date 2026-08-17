"""Phase 8E tests: post-process job lifecycle, ComfyUI compilation, submission, and reconciliation."""

import uuid
from pathlib import Path

from backend.render_production import (
    POSTPROCESS_ACTIVE,
    POSTPROCESS_CANCELLED,
    POSTPROCESS_FAILED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    POSTPROCESS_READY,
    POSTPROCESS_SUBMITTED,
    POSTPROCESS_SUBMITTING,
    POSTPROCESS_SUCCEEDED,
    PostprocessJobConflict,
    PostprocessJobNotFound,
    PostprocessJobStore,
    ProductionMethodUnavailable,
    ProductionNotReady,
    RenderProductionError,
    compile_postprocess_workflow,
    new_postprocess_job_record,
    reconcile_postprocess_job,
    submit_postprocess_job,
    transition_postprocess_job,
)
try:
    from phase8e_base import FakeComfyClientForPostprocess, Phase8ETestBase
except ImportError:
    from tests.phase8e_base import FakeComfyClientForPostprocess, Phase8ETestBase


class TestPhase8EJobLifecycleAndStore(Phase8ETestBase):
    """Test post-process job model, transitions, and persistence."""

    def test_new_job_record(self):
        record = new_postprocess_job_record(
            self.project_id,
            self.scene_1_id,
            "rtx_vsr_fast",
            production_fingerprint="f" * 64,
        )
        self.assertEqual(record["state"], POSTPROCESS_READY)
        self.assertEqual(record["project_id"], self.project_id)
        self.assertEqual(record["scene_id"], self.scene_1_id)
        self.assertEqual(record["method"], "rtx_vsr_fast")
        self.assertEqual(record["production_fingerprint"], "f" * 64)
        self.assertIsNone(record["comfy_prompt_id"])
        self.assertIsNone(record["output"])

    def test_new_job_record_rejects_none_method(self):
        with self.assertRaises(RenderProductionError):
            new_postprocess_job_record(
                self.project_id,
                self.scene_1_id,
                "none",
                production_fingerprint="f" * 64,
            )

    def test_transition_postprocess_job(self):
        record = new_postprocess_job_record(
            self.project_id,
            self.scene_1_id,
            "rtx_vsr_fast",
            production_fingerprint="f" * 64,
        )
        submitting = transition_postprocess_job(record, POSTPROCESS_SUBMITTING, reason="submitting")
        self.assertEqual(submitting["state"], POSTPROCESS_SUBMITTING)
        self.assertEqual(len(submitting["transitions"]), 1)

        submitted = transition_postprocess_job(
            submitting,
            POSTPROCESS_SUBMITTED,
            reason="accepted",
            updates={"comfy_prompt_id": "prompt-123"},
        )
        self.assertEqual(submitted["state"], POSTPROCESS_SUBMITTED)
        self.assertEqual(submitted["comfy_prompt_id"], "prompt-123")

        succeeded = transition_postprocess_job(submitted, POSTPROCESS_SUCCEEDED, reason="done")
        self.assertEqual(succeeded["state"], POSTPROCESS_SUCCEEDED)

        # Terminal state cannot transition to another state
        with self.assertRaises(RenderProductionError):
            transition_postprocess_job(succeeded, POSTPROCESS_ACTIVE, reason="invalid")

    def test_job_store_save_load_list(self):
        store = PostprocessJobStore(self.storage)
        record = new_postprocess_job_record(
            self.project_id,
            self.scene_1_id,
            "rtx_vsr_fast",
            production_fingerprint="f" * 64,
        )
        saved = store.save(record)
        self.assertEqual(saved["postprocess_job_id"], record["postprocess_job_id"])

        loaded = store.load(self.project_id, self.scene_1_id, record["postprocess_job_id"])
        self.assertEqual(loaded["postprocess_job_id"], record["postprocess_job_id"])

        listed = store.list_scene(self.project_id, self.scene_1_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["postprocess_job_id"], record["postprocess_job_id"])

    def test_job_store_not_found(self):
        store = PostprocessJobStore(self.storage)
        with self.assertRaises(PostprocessJobNotFound):
            store.load(self.project_id, self.scene_1_id, str(uuid.uuid4()))


class TestPhase8ECompilationAndSubmission(Phase8ETestBase):
    """Test workflow template compilation and prompt submission."""

    def test_compile_postprocess_workflow_rtx_vsr(self):
        compiled = compile_postprocess_workflow(
            "rtx_vsr_fast",
            final_scene_selector="music_video_builder/p1/s1/final_scene.mp4",
            output_prefix="prod_postprocess_s1",
        )
        self.assertEqual(compiled["1"]["inputs"]["video"], "music_video_builder/p1/s1/final_scene.mp4")
        self.assertEqual(compiled["3"]["inputs"]["filename_prefix"], "prod_postprocess_s1")

    def test_compile_postprocess_workflow_seedvr2(self):
        compiled = compile_postprocess_workflow(
            "seedvr2_quality",
            final_scene_selector="music_video_builder/p1/s1/final_scene.mp4",
            output_prefix="prod_postprocess_seed_s1",
        )
        self.assertEqual(compiled["1"]["inputs"]["video"], "music_video_builder/p1/s1/final_scene.mp4")
        self.assertEqual(compiled["5"]["inputs"]["filename_prefix"], "prod_postprocess_seed_s1")

    def test_compile_postprocess_workflow_rejects_none(self):
        with self.assertRaises(RenderProductionError):
            compile_postprocess_workflow(
                "none",
                final_scene_selector="foo",
                output_prefix="bar",
            )

    def test_submit_postprocess_job_success(self):
        self._setup_final_scene(self.scene_1_id)
        client = FakeComfyClientForPostprocess(prompt_id="prompt-test-456")
        node_types = {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"}

        submitted = submit_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            client=client,
            final_scene_selector="music_video_builder/p/s/final_scene.mp4",
            output_prefix="prefix_s1",
            node_types=node_types,
            hardware_supported=True,
        )
        self.assertEqual(submitted["state"], POSTPROCESS_SUBMITTED)
        self.assertEqual(submitted["comfy_prompt_id"], "prompt-test-456")
        self.assertEqual(len(client.submitted_prompts), 1)

    def test_submit_postprocess_job_rejects_when_unavailable(self):
        self._setup_final_scene(self.scene_1_id)
        client = FakeComfyClientForPostprocess()
        # Missing RTXVideoSuperResolution
        with self.assertRaises(ProductionMethodUnavailable):
            submit_postprocess_job(
                self.storage,
                self.project_id,
                self.scene_1_id,
                method="rtx_vsr_fast",
                client=client,
                final_scene_selector="video.mp4",
                output_prefix="prefix",
                node_types={"VHS_LoadVideo", "VHS_VideoCombine"},
                hardware_supported=True,
            )

    def test_submit_postprocess_job_conflict_when_active(self):
        self._setup_final_scene(self.scene_1_id)
        client = FakeComfyClientForPostprocess(prompt_id="prompt-1")
        node_types = {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"}

        submit_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            client=client,
            final_scene_selector="video.mp4",
            output_prefix="prefix_1",
            node_types=node_types,
            hardware_supported=True,
        )

        # Second submission while first is still SUBMITTED -> Conflict
        with self.assertRaises(PostprocessJobConflict):
            submit_postprocess_job(
                self.storage,
                self.project_id,
                self.scene_1_id,
                method="rtx_vsr_fast",
                client=client,
                final_scene_selector="video.mp4",
                output_prefix="prefix_2",
                node_types=node_types,
                hardware_supported=True,
            )


class TestPhase8EReconciliation(Phase8ETestBase):
    """Test post-process job reconciliation against queue and history."""

    def test_reconcile_running_and_succeeded(self):
        self._setup_final_scene(self.scene_1_id)
        client = FakeComfyClientForPostprocess(prompt_id="prompt-rec-1")
        node_types = {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"}

        job = submit_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            client=client,
            final_scene_selector="video.mp4",
            output_prefix="owned_prefix_s1",
            node_types=node_types,
            hardware_supported=True,
        )
        job_id = job["postprocess_job_id"]

        # 1. Running in queue
        client.queue_running = [{"prompt_id": "prompt-rec-1"}]
        reconciled = reconcile_postprocess_job(self.storage, self.project_id, self.scene_1_id, job_id, client=client)
        self.assertEqual(reconciled["state"], POSTPROCESS_ACTIVE)

        # 2. Succeeded in history with output
        raw_video = self.output_root / "owned_prefix_s1_00001_.mp4"
        raw_video.write_bytes(b"RAW_UPSCALED_VIDEO_FROM_COMFY")

        client.queue_running = []
        client.history_records["prompt-rec-1"] = {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "gifs": [
                        {
                            "filename": "owned_prefix_s1_00001_.mp4",
                            "subfolder": "",
                            "type": "output",
                            "format": "video/nvenc_h264-mp4",
                        }
                    ]
                }
            },
        }

        reconciled_done = reconcile_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            job_id,
            client=client,
            output_root=self.output_root,
        )
        self.assertEqual(reconciled_done["state"], POSTPROCESS_SUCCEEDED)
        self.assertIsNotNone(reconciled_done["output"])
        self.assertEqual(reconciled_done["output"]["filename"], "owned_prefix_s1_00001_.mp4")

    def test_reconcile_failure_in_history(self):
        self._setup_final_scene(self.scene_1_id)
        client = FakeComfyClientForPostprocess(prompt_id="prompt-fail-1")
        node_types = {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"}

        job = submit_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            client=client,
            final_scene_selector="video.mp4",
            output_prefix="prefix",
            node_types=node_types,
            hardware_supported=True,
        )
        job_id = job["postprocess_job_id"]

        client.history_records["prompt-fail-1"] = {
            "status": {"status_str": "error", "completed": True, "messages": ["Execution failed"]},
        }

        reconciled = reconcile_postprocess_job(self.storage, self.project_id, self.scene_1_id, job_id, client=client)
        self.assertEqual(reconciled["state"], POSTPROCESS_FAILED)
        self.assertEqual(reconciled["failure"]["code"], "COMFYUI_EXECUTION_FAILED")
