"""Phase 8E tests: candidate validation, audio authority, atomic promotion, and failure isolation."""

import uuid
from pathlib import Path

from backend.render_finalize import FINALIZATION_STATE_FINALIZED
from backend.render_production import (
    POSTPROCESS_SUCCEEDED,
    PostprocessJobStore,
    ProductionMediaAdapter,
    ProductionNotReady,
    RenderProductionError,
    extract_video_frames_for_postprocess,
    finalize_postprocess_job,
    new_postprocess_job_record,
    promote_production_scene,
    record_production_failure,
    summarize_production_scene,
    transition_postprocess_job,
    validate_postprocess_candidate,
)
try:
    from phase8e_base import (
        AUDIO_CHANNELS,
        AUDIO_SAMPLE_RATE,
        DEFAULT_DURATION_MS,
        DEFAULT_FRAME_COUNT,
        FakeProductionMediaAdapter,
        Phase8ETestBase,
        UPSCALE_HEIGHT,
        UPSCALE_WIDTH,
    )
except ImportError:
    from tests.phase8e_base import (
        AUDIO_CHANNELS,
        AUDIO_SAMPLE_RATE,
        DEFAULT_DURATION_MS,
        DEFAULT_FRAME_COUNT,
        FakeProductionMediaAdapter,
        Phase8ETestBase,
        UPSCALE_HEIGHT,
        UPSCALE_WIDTH,
    )


class TestPhase8ECandidateValidation(Phase8ETestBase):
    """Test candidate probe validation for spatial-only upscaling."""

    def test_valid_candidate_passes(self):
        adapter = FakeProductionMediaAdapter()
        probe = adapter.probe(Path("dummy.mp4"))
        validated = validate_postprocess_candidate(
            probe,
            method="rtx_vsr_fast",
            expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
            target_duration_ms=DEFAULT_DURATION_MS,
            expected_frame_count=DEFAULT_FRAME_COUNT,
            expected_audio_sample_rate=AUDIO_SAMPLE_RATE,
            expected_audio_channels=AUDIO_CHANNELS,
        )
        self.assertEqual(validated["method"], "rtx_vsr_fast")
        self.assertEqual(validated["width"], UPSCALE_WIDTH)
        self.assertEqual(validated["height"], UPSCALE_HEIGHT)
        self.assertEqual(validated["fps_numerator"], 24)
        self.assertEqual(validated["fps_denominator"], 1)

    def test_invalid_fps_fails(self):
        adapter = FakeProductionMediaAdapter(fps_numerator=30)
        probe = adapter.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_FPS_INVALID")

    def test_invalid_resolution_fails(self):
        adapter = FakeProductionMediaAdapter(width=1280, height=720)
        probe = adapter.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_RESOLUTION_INVALID")

    def test_duration_mismatch_fails(self):
        adapter = FakeProductionMediaAdapter(duration_ms=5000)
        probe = adapter.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_DURATION_INVALID")

    def test_frame_count_mismatch_fails(self):
        adapter = FakeProductionMediaAdapter(frame_count=120)
        probe = adapter.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_FRAME_COUNT_INVALID")

    def test_frame_count_exact_match_enforced_single_frame_drift_rejected(self):
        # Single frame dropped or added is strictly rejected (zero unexplained drift)
        for drift in (-1, +1):
            adapter = FakeProductionMediaAdapter(frame_count=DEFAULT_FRAME_COUNT + drift)
            probe = adapter.probe(Path("dummy.mp4"))
            with self.assertRaises(RenderProductionError) as ctx:
                validate_postprocess_candidate(
                    probe,
                    method="rtx_vsr_fast",
                    expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                    target_duration_ms=DEFAULT_DURATION_MS,
                    expected_frame_count=DEFAULT_FRAME_COUNT,
                )
            self.assertEqual(ctx.exception.code, "PRODUCTION_FRAME_COUNT_INVALID")

    def test_duration_tolerance_one_video_frame_boundary(self):
        # At 24 FPS, one video frame tolerance is ceil(1000/24) = 42 ms
        # Within 1 video frame tolerance (e.g. +40 ms) passes
        adapter_pass = FakeProductionMediaAdapter(duration_ms=DEFAULT_DURATION_MS + 40)
        probe_pass = adapter_pass.probe(Path("dummy.mp4"))
        validated = validate_postprocess_candidate(
            probe_pass,
            method="rtx_vsr_fast",
            expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
            target_duration_ms=DEFAULT_DURATION_MS,
            expected_frame_count=DEFAULT_FRAME_COUNT,
        )
        self.assertEqual(validated["duration_ms"], DEFAULT_DURATION_MS + 40)

        # Exceeding 1 video frame tolerance (e.g. +45 ms) fails
        adapter_fail = FakeProductionMediaAdapter(duration_ms=DEFAULT_DURATION_MS + 45)
        probe_fail = adapter_fail.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe_fail,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_DURATION_INVALID")

    def test_audio_codec_mismatch_fails(self):
        adapter = FakeProductionMediaAdapter(audio_codec="mp3")
        probe = adapter.probe(Path("dummy.mp4"))
        with self.assertRaises(RenderProductionError) as ctx:
            validate_postprocess_candidate(
                probe,
                method="rtx_vsr_fast",
                expected_dimensions=(UPSCALE_WIDTH, UPSCALE_HEIGHT),
                target_duration_ms=DEFAULT_DURATION_MS,
                expected_frame_count=DEFAULT_FRAME_COUNT,
            )
        self.assertEqual(ctx.exception.code, "PRODUCTION_AUDIO_CODEC_INVALID")


class TestPhase8EFinalizationAndPromotion(Phase8ETestBase):
    """Test finalizing a post-process job and atomic promotion."""

    def test_finalize_postprocess_job_success(self):
        self._setup_final_scene(self.scene_1_id)
        raw_upscale = self.output_root / "upscaled_raw.mp4"
        raw_upscale.write_bytes(b"UPSCALED_RAW_VIDEO")

        summary_pre = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        fingerprint = summary_pre["fingerprint"]

        store = PostprocessJobStore(self.storage)
        record = new_postprocess_job_record(self.project_id, self.scene_1_id, "rtx_vsr_fast", production_fingerprint=fingerprint)
        record = transition_postprocess_job(
            record,
            POSTPROCESS_SUCCEEDED,
            reason="done",
            updates={"output": {"local_path": str(raw_upscale), "filename": "upscaled_raw.mp4"}},
        )
        store.save(record)

        adapter = FakeProductionMediaAdapter()
        promoted = finalize_postprocess_job(
            self.storage,
            self.project_id,
            self.scene_1_id,
            record["postprocess_job_id"],
            media_adapter=adapter,
        )

        self.assertEqual(promoted["state"], "FINALIZED")
        self.assertEqual(promoted["method"], "rtx_vsr_fast")
        self.assertEqual(len(adapter.remux_calls), 1)

        # Check production scene summary is now CURRENT
        summary = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        self.assertEqual(summary["state"], "CURRENT")
        self.assertIsNotNone(summary["output"])

    def test_failure_isolation_leaves_final_scene_intact(self):
        self._setup_final_scene(self.scene_1_id, content=b"PROTECTED_FINAL_SCENE")
        raw_upscale = self.output_root / "upscaled_raw.mp4"
        raw_upscale.write_bytes(b"UPSCALED_RAW_VIDEO")

        summary_pre = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        fingerprint = summary_pre["fingerprint"]

        store = PostprocessJobStore(self.storage)
        record = new_postprocess_job_record(self.project_id, self.scene_1_id, "rtx_vsr_fast", production_fingerprint=fingerprint)
        record = transition_postprocess_job(
            record,
            POSTPROCESS_SUCCEEDED,
            reason="done",
            updates={"output": {"local_path": str(raw_upscale), "filename": "upscaled_raw.mp4"}},
        )
        store.save(record)

        # Remux fails
        adapter = FakeProductionMediaAdapter(fail_remux=True)
        with self.assertRaises(RenderProductionError):
            finalize_postprocess_job(
                self.storage,
                self.project_id,
                self.scene_1_id,
                record["postprocess_job_id"],
                media_adapter=adapter,
            )

        # Production summary is FAILED
        summary = summarize_production_scene(self.storage, self.project_id, self.scene_1_id, method="rtx_vsr_fast")
        self.assertEqual(summary["state"], "FAILED")
        self.assertIsNotNone(summary["failure"])

        # Final Scene is STILL valid and untouched!
        self.assertTrue(summary["final_scene_current"])
        root = self.storage.project_directory(self.project_id)
        final_file = root / "renders" / self.scene_1_id / "final" / "final_scene.mp4"
        self.assertTrue(final_file.is_file())
        self.assertEqual(final_file.read_bytes(), b"PROTECTED_FINAL_SCENE")


class TestPhase8EFrameExtractionSafety(Phase8ETestBase):
    """Test deterministic temporary frame extraction helper."""

    def test_extract_frames_success(self):
        video_path = self.projects_root / "sample.mp4"
        video_path.write_bytes(b"DUMMY_VIDEO")
        out_dir = self.projects_root / "temp_frames"

        def fake_runner(args, **kwargs):
            class Completed:
                returncode = 0
                stderr = ""
            # Create two dummy frame files
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "000001.png").write_bytes(b"FRAME1")
            (out_dir / "000002.png").write_bytes(b"FRAME2")
            return Completed()

        frames = extract_video_frames_for_postprocess(
            video_path,
            out_dir,
            ffmpeg_path="fake_ffmpeg",
            runner=fake_runner,
        )
        self.assertEqual(len(frames), 2)
        self.assertEqual([f.name for f in frames], ["000001.png", "000002.png"])
