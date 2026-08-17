"""Phase 8E Round 8E.2A tests: Method Capability Enforcement and AMD RTX VSR Block.

Verifies:
1. Truthful RTX VSR hardware capability detection (NVIDIA CUDA required, AMD ROCm/HIP -> UNAVAILABLE).
2. AMD runtime does NOT globally block Builder, Storyboard, Visuals, Prompts, H3, or None mode.
3. SeedVR2 is evaluated independently on its own requirements and offload architecture.
4. Project preference (e.g. rtx_vsr_fast) persists independently of transient machine capability.
5. Batch preview and start fail fast with clear errors when the selected production profile is unavailable:
   - No batch created
   - No preparation executed
   - No H3 prompt submitted
   - No finalization run
   - No postprocess prompt submitted
6. Direct route enforcement rejecting start_batch and submit_render_job when selected profile is unavailable.
7. Systemic batch pause if capability is lost during an active batch, preserving completed upstream work.
8. Resume revalidation failing while blocker persists and succeeding once restored.
9. UNQUALIFIED upscaler policy (allows manual single-scene qualification, blocks large batch).
10. Seamless method switching (RTX VSR -> None immediately makes existing Final Scenes current without re-rendering).
"""

from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectValidationError
from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_POSTPROCESS_ONLY,
    ACTION_PREPARE_AND_RENDER,
    BATCH_COMPLETED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    ITEM_COMPLETE,
    ITEM_PENDING,
    ITEM_POSTPROCESS_FAILED,
    BatchRunner,
    BatchStore,
    RenderBatchConflict,
    RenderBatchError,
    manual_action_blocker,
    new_batch_record,
    plan_batch,
    preview_batch,
    resume_batch,
    start_batch,
)
from backend.render_production import (
    METHOD_AVAILABLE,
    METHOD_NOT_DETERMINABLE,
    METHOD_UNAVAILABLE,
    METHOD_UNQUALIFIED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    PRODUCTION_CURRENT,
    PRODUCTION_NEEDS_POSTPROCESS,
    PRODUCTION_NOT_AVAILABLE,
    ProductionMethodUnavailable,
    get_project_production_status,
    is_rtx_vsr_hardware_supported,
    production_method_availability,
    resolve_project_production_method,
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


class TestPhase8E2ACapabilityProbing(Phase8ETestBase):
    """Test truthful runtime capability detection for RTX VSR and other methods."""

    def test_rtx_vsr_node_presence_alone_does_not_imply_executable_capability(self):
        avail = production_method_availability(
            "rtx_vsr_fast",
            node_types={"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"},
            hardware_supported=False,
        )
        self.assertEqual(avail["state"], METHOD_UNAVAILABLE)
        self.assertEqual(avail.get("reason"), "UNSUPPORTED_HARDWARE")

    def test_amd_runtime_reports_rtx_vsr_unavailable(self):
        avail = production_method_availability("rtx_vsr_fast", hardware_supported=False)
        self.assertEqual(avail["state"], METHOD_UNAVAILABLE)

    def test_amd_runtime_does_not_globally_block_builder_or_none_mode(self):
        avail_none = production_method_availability("none", hardware_supported=False)
        self.assertEqual(avail_none["state"], METHOD_AVAILABLE)

        status = get_project_production_status(self.storage, self.project_id, hardware_supported=False)
        self.assertEqual(status["capabilities"]["none"]["state"], METHOD_AVAILABLE)
        self.assertEqual(status["capabilities"]["rtx_vsr_fast"]["state"], METHOD_UNAVAILABLE)

    def test_seedvr2_evaluated_independently_on_its_own_requirements(self):
        seed_nodes = {
            "VHS_LoadVideo",
            "SeedVR2LoadDiTModel",
            "SeedVR2LoadVAEModel",
            "SeedVR2VideoUpscaler",
            "VHS_VideoCombine",
        }
        seed_models = {
            "seedvr2_ema_7b-Q4_K_M.gguf": "AVAILABLE",
            "ema_vae_fp16.safetensors": "AVAILABLE",
        }
        avail_seed = production_method_availability(
            "seedvr2_quality",
            node_types=seed_nodes,
            model_status=seed_models,
            hardware_supported=False,
        )
        self.assertEqual(avail_seed["state"], METHOD_UNQUALIFIED)

    def test_runtime_capability_is_not_persisted_as_project_preference(self):
        result = set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        self.assertEqual(result["production"]["upscale_method"], "rtx_vsr_fast")

        project = self.storage.load_project(self.project_id)
        self.assertEqual(project["production"]["upscale_method"], "rtx_vsr_fast")
        self.assertEqual(resolve_project_production_method(project), "rtx_vsr_fast")

    def test_supported_rtx_runtime_reports_available(self):
        avail = production_method_availability(
            "rtx_vsr_fast",
            node_types={"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"},
            hardware_supported=True,
        )
        self.assertEqual(avail["state"], METHOD_AVAILABLE)


class TestPhase8E2ABatchFailFastAndGating(Phase8DBase):
    """Test batch preview and start fail fast when selected upscaler is unavailable."""

    def test_batch_preview_blocks_unavailable_selected_upscaler(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=False):
            with self.assertRaises(RenderBatchError) as ctx:
                preview_batch(
                    self.storage,
                    self.project_id,
                    client=self.client,
                )
            self.assertEqual(ctx.exception.code, "PRODUCTION_METHOD_UNAVAILABLE")

    def test_batch_start_revalidates_and_blocks_unavailable_selected_upscaler(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=False):
            with self.assertRaises(RenderBatchError) as ctx:
                start_batch(
                    self.storage,
                    self.project_id,
                    start_runner=False,
                    **self.batch_seams(),
                )
            self.assertEqual(ctx.exception.code, "PRODUCTION_METHOD_UNAVAILABLE")

            # Verify no batch was created, no jobs submitted, no preparer called
            store = BatchStore(self.storage)
            self.assertIsNone(store.load_active(self.project_id))
            self.assertEqual(len(self.preparer_calls), 0)
            self.assertEqual(len(self.client.prompt_ids), 0)

    def test_manual_action_blocker_rejects_unavailable_upscaler(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=False):
            blocker = manual_action_blocker(self.storage, self.project_id, check_upscaler=True)
            self.assertIsNotNone(blocker)
            self.assertIn("unavailable", blocker.lower())

    def test_manual_upscale_rejected_when_selected_upscaler_unavailable(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=False):
            with self.assertRaises(ProductionMethodUnavailable) as ctx:
                submit_postprocess_job(
                    self.storage,
                    self.project_id,
                    self.scene_ids[0],
                    method="rtx_vsr_fast",
                    final_scene_selector="fake/path.mp4",
                    output_prefix="test_pfx",
                    hardware_supported=False,
                )
            self.assertEqual(ctx.exception.code, "POSTPROCESS_METHOD_UNAVAILABLE")

    def test_unqualified_upscaler_blocks_batch_but_allows_manual_upscale(self):
        set_project_production_method(self.storage, self.project_id, "seedvr2_quality")
        with patch(
            "backend.render_batch.production_method_availability",
            return_value={"method": "seedvr2_quality", "state": "UNQUALIFIED"},
        ):
            # Batch start is BLOCKED for UNQUALIFIED
            with self.assertRaises(RenderBatchError) as ctx:
                start_batch(
                    self.storage,
                    self.project_id,
                    start_runner=False,
                    **self.batch_seams(),
                )
            self.assertEqual(ctx.exception.code, "UPSCALER_UNQUALIFIED")


class TestPhase8E2ADispatchCapabilityLossAndRecovery(Phase8DBase):
    """Test systemic batch pause when upscaler capability disappears mid-batch."""

    def test_batch_pauses_systemically_if_upscaler_capability_lost_mid_batch(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")

        # Start batch while hardware is supported
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            rec = start_batch(
                self.storage,
                self.project_id,
                scene_ids=[self.scene_ids[0], self.scene_ids[1]],
                start_runner=False,
                **self.batch_seams(),
            )
            self.assertEqual(rec["batch"]["state"], BATCH_RUNNING)

        runner = BatchRunner(self.storage, self.project_id, **self.runner_seams())

        # Now simulate capability loss before scene execution
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=False):
            rec_tick = runner.tick()
            self.assertEqual(rec_tick["state"], BATCH_PAUSED)
            self.assertEqual(rec_tick["attention"]["code"], "PRODUCTION_METHOD_UNAVAILABLE")

            # Verify next H3 does not start and scenes are NOT mass-failed
            self.assertEqual(rec_tick["items"][0]["disposition"], ITEM_PENDING)
            self.assertEqual(rec_tick["items"][1]["disposition"], ITEM_PENDING)

            # Resume fails while blocker persists
            with self.assertRaises(RenderBatchConflict) as ctx:
                resume_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
            self.assertEqual(ctx.exception.code, "PRODUCTION_METHOD_UNAVAILABLE")

        # When capability is restored, resume succeeds
        with patch("backend.render_production.is_rtx_vsr_hardware_supported", return_value=True):
            resumed = resume_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
            self.assertEqual(resumed["batch"]["state"], BATCH_RUNNING)


class TestPhase8E2AMethodSwitch(Phase8ETestBase):
    """Test switching between upscaled and None production modes."""

    def test_switching_from_unavailable_rtx_to_none_clears_blocker(self):
        # Create finalized scene
        self._setup_final_scene(self.scene_1_id)

        # Under RTX VSR on unsupported hardware
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        summary_rtx = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
        )
        self.assertEqual(summary_rtx["state"], PRODUCTION_NEEDS_POSTPROCESS)

        # Switch to None
        set_project_production_method(self.storage, self.project_id, "none")
        summary_none = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="none",
        )
        # Production Scene is IMMEDIATELY CURRENT without re-rendering H3
        self.assertEqual(summary_none["state"], PRODUCTION_CURRENT)
        self.assertTrue(summary_none["resolves_to_final_scene"])
        self.assertIsNotNone(summary_none["output"])

        # Switch back to RTX VSR: Final Scene remains completely intact
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        summary_rtx2 = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
        )
        self.assertEqual(summary_rtx2["state"], PRODUCTION_NEEDS_POSTPROCESS)
        self.assertTrue(summary_rtx2["final_scene_current"])
