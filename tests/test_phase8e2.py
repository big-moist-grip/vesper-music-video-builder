"""Phase 8E tests: None mode, production fingerprinting/currentness, and action planning."""

import hashlib
import json
from pathlib import Path

from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_BLOCKED,
    ACTION_FINALIZE_RAW,
    ACTION_PREPARE_AND_RENDER,
    ACTION_RENDER_PREPARED,
)
from backend.render_production import (
    ACTION_FINALIZE_AND_POSTPROCESS,
    ACTION_POSTPROCESS_ONLY,
    ACTION_PREPARE_RENDER_FINALIZE_POSTPROCESS,
    ACTION_PRODUCTION_BLOCKED,
    ACTION_PRODUCTION_COMPLETE,
    ACTION_RENDER_FINALIZE_POSTPROCESS,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    PRODUCTION_CURRENT,
    PRODUCTION_FAILED,
    PRODUCTION_NEEDS_POSTPROCESS,
    PRODUCTION_NOT_AVAILABLE,
    PRODUCTION_STALE,
    build_production_fingerprint,
    plan_production_scene,
    promote_production_scene,
    record_production_failure,
    summarize_production_scene,
)
try:
    from phase8e_base import Phase8ETestBase
except ImportError:
    from tests.phase8e_base import Phase8ETestBase


class TestPhase8ENoneModeAndCurrentness(Phase8ETestBase):
    """Test None/Native mode resolution and production currentness."""

    def test_none_mode_resolves_directly_to_final_scene(self):
        self._setup_final_scene(self.scene_1_id, content=b"FINAL_SCENE_PICTURE")
        summary = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="none",
        )
        self.assertEqual(summary["state"], PRODUCTION_CURRENT)
        self.assertTrue(summary["resolves_to_final_scene"])
        self.assertEqual(summary["output"]["relative_path"], f"renders/{self.scene_1_id}/final/final_scene.mp4")
        self.assertIsNotNone(summary["fingerprint"])

    def test_none_mode_not_available_when_final_scene_missing(self):
        summary = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="none",
        )
        self.assertEqual(summary["state"], PRODUCTION_NOT_AVAILABLE)
        self.assertFalse(summary["final_scene_current"])
        self.assertIsNone(summary["output"])

    def test_upscale_method_needs_postprocess_when_not_yet_run(self):
        self._setup_final_scene(self.scene_1_id, content=b"FINAL_SCENE_PICTURE")
        summary_rtx = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
        )
        self.assertEqual(summary_rtx["state"], PRODUCTION_NEEDS_POSTPROCESS)
        self.assertTrue(summary_rtx["final_scene_current"])
        self.assertIsNone(summary_rtx["output"])

        summary_seed = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="seedvr2_quality",
        )
        self.assertEqual(summary_seed["state"], PRODUCTION_NEEDS_POSTPROCESS)

    def test_production_fingerprint_changes_with_method(self):
        identity = {"relative_path": "renders/s1/final/final_scene.mp4", "size": 1000, "sha256": "a" * 64}
        fp_none = build_production_fingerprint(final_scene_identity=identity, method="none")
        fp_rtx = build_production_fingerprint(final_scene_identity=identity, method="rtx_vsr_fast")
        fp_seed = build_production_fingerprint(final_scene_identity=identity, method="seedvr2_quality")

        self.assertNotEqual(fp_none, fp_rtx)
        self.assertNotEqual(fp_rtx, fp_seed)
        self.assertNotEqual(fp_none, fp_seed)

        # Deterministic
        self.assertEqual(fp_rtx, build_production_fingerprint(final_scene_identity=identity, method="rtx_vsr_fast"))

    def test_changing_final_scene_makes_upscale_stale(self):
        final_file = self._setup_final_scene(self.scene_1_id, content=b"ORIGINAL_FINAL_SCENE")
        root = self.storage.project_directory(self.project_id)
        summary = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        fingerprint = summary["fingerprint"]

        # Promote a production scene
        candidate = root / "temp_candidate.mp4"
        candidate.write_bytes(b"UPSCALED_OUTPUT")
        promote_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="rtx_vsr_fast",
            fingerprint=fingerprint,
            postprocess_job_id="00000000-0000-0000-0000-000000000001",
            candidate_path=candidate,
            relative_path=f"renders/{self.scene_1_id}/production/production_rtx_vsr_fast.mp4",
            validation={"width": 1920, "height": 1088},
        )
        candidate.unlink()

        # Now production is CURRENT
        current_summary = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        self.assertEqual(current_summary["state"], PRODUCTION_CURRENT)

        # Now update the Final Scene (new content / new H3 job)
        self._setup_final_scene(self.scene_1_id, content=b"NEW_FINAL_SCENE_CONTENT")

        # Production is now STALE
        stale_summary = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        self.assertEqual(stale_summary["state"], PRODUCTION_STALE)


class TestPhase8EActionPlanning(Phase8ETestBase):
    """Test the compositional minimum-work action planning (plan_production_scene)."""

    def test_plan_production_complete_when_current(self):
        self._setup_final_scene(self.scene_1_id)
        plan = plan_production_scene(self.storage, self.project_id, self.scene_1_id, method="none")
        self.assertEqual(plan["action"], ACTION_PRODUCTION_COMPLETE)
        self.assertEqual(plan["blockers"], [])

    def test_plan_postprocess_only_when_final_current_but_upscale_missing(self):
        self._setup_final_scene(self.scene_1_id)
        plan = plan_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        self.assertEqual(plan["action"], ACTION_POSTPROCESS_ONLY)

    def test_plan_production_blocked_when_unprepared(self):
        # Scene has no keyframe or preparation -> blocked
        plan = plan_production_scene(self.storage, self.project_id, self.scene_1_id, method="none")
        self.assertIn(plan["action"], {ACTION_PRODUCTION_BLOCKED, ACTION_PREPARE_RENDER_FINALIZE_POSTPROCESS})
