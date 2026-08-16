import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import ProjectStorage, default_prompts_for_scenes
from backend.render import H3_FPS, H3_FRAME_STEP, H3_MINIMUM_FRAMES, TimingContractError, build_h3_timing_plan, build_preparation_fingerprint
from backend.render_finalize import (
    FINALIZATION_STATE_FAILED,
    FINALIZATION_STATE_FINALIZED,
    FINALIZATION_STATE_NOT_AVAILABLE,
    FINALIZATION_STATE_RAW_READY,
    FINALIZATION_STATE_STALE,
    FinalizationMediaAdapter,
    MediaProbeAdapter,
    RenderFinalizationError,
    _resolve_raw_path,
    finalize_render_job,
    summarize_render_job_finalization,
)
from backend.render_jobs import JobStore, SUCCEEDED, new_job_record, transition_job
from backend.scenes import build_scenes
from backend.visuals import default_visuals_for_scenes


ROOT = Path(__file__).parents[1]
FFMPEG = Path("D:/ffmpeg/bin/ffmpeg.exe") if Path("D:/ffmpeg/bin/ffmpeg.exe").is_file() else Path(shutil.which("ffmpeg") or "")
FFPROBE = Path("D:/ffmpeg/bin/ffprobe.exe") if Path("D:/ffmpeg/bin/ffprobe.exe").is_file() else Path(shutil.which("ffprobe") or "")
HAS_FFMPEG = FFMPEG.is_file() and FFPROBE.is_file()


class Phase8CFixture(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8C finalization")
        self.target_duration_ms = 6_000
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": self.target_duration_ms, "text": "No god in the house."}],
            self.target_duration_ms,
        )
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "source": {
                    "master_audio": {
                        "stored_name": "master_audio.wav",
                        "original_name": "master.wav",
                        "duration_ms": self.target_duration_ms,
                    },
                    "lyrics_srt": {"stored_name": "lyrics.srt", "original_name": "lyrics.srt", "cue_count": 1},
                },
                "scenes": scenes,
                "visuals": default_visuals_for_scenes(scenes),
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )
        self.project_id = project["project_id"]
        self.scene_id = scenes[0]["scene_id"]
        self.project_root = self.storage.project_directory(self.project_id)
        self.raw_root = Path(self.temp_directory.name) / "comfy-output"
        self.raw_relative = Path("music_video_builder") / "raw.mp4"
        self.raw_path = self.raw_root / self.raw_relative
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path = self.project_root / "renders" / self.scene_id / "inputs" / "scene_audio.wav"
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.preparation_fingerprint = "a" * 64
        workflow_path = self.project_root / "renders" / self.scene_id / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text("{}", encoding="utf-8")
        self.audio_relative = self.audio_path.relative_to(self.project_root).as_posix()
        self.workflow_relative = workflow_path.relative_to(self.project_root).as_posix()
        preparation = {
            "preparation_version": 2,
            "project_id": self.project_id,
            "scene_id": self.scene_id,
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": self.preparation_fingerprint,
            "source_audio": {
                "prepared_relative_path": self.audio_relative,
                "render_relative_path": self.audio_relative,
                "authoritative_duration_ms": self.target_duration_ms,
            },
            "visual_inputs": [],
            "timing": build_h3_timing_plan(self.target_duration_ms),
            "workflow": {"relative_path": self.workflow_relative},
        }
        preparation_path = self.project_root / "renders" / self.scene_id / "preparation.json"
        preparation_path.write_text(json.dumps(preparation), encoding="utf-8")
        self.preflight = {
            "project_id": self.project_id,
            "scenes": [{
                "scene_id": self.scene_id,
                "generation_method": "keyframe_i2v",
                "preparation_status": "current",
                "preparation_current": True,
                "preparation_fingerprint": self.preparation_fingerprint,
            }],
        }
        self.prompt_id = "synthetic-prompt-1"
        self.job = self._create_successful_job()

    def _create_successful_job(self):
        record = new_job_record(
            self.project_id,
            self.scene_id,
            "keyframe_i2v",
            self.preparation_fingerprint,
            output_node_id="20",
            expected_filename_prefix="music_video_builder",
            output_format="video/h264-mp4",
        )
        record = transition_job(record, "SUBMITTING", reason="test")
        record = transition_job(
            record,
            "QUEUED",
            reason="test",
            updates={"comfy_prompt_id": self.prompt_id},
        )
        record = transition_job(
            record,
            SUCCEEDED,
            reason="test",
            updates={
                "output": {
                    "prompt_id": self.prompt_id,
                    "output_node_id": "20",
                    "relative_path": self.raw_relative.as_posix(),
                    "filename": self.raw_path.name,
                    "subfolder": self.raw_relative.parent.as_posix(),
                    "format": "video/h264-mp4",
                    "raw_h3_output": True,
                },
            },
        )
        return JobStore(self.storage).save(record)

    def _run_ffmpeg(self, *arguments):
        completed = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *arguments],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _make_audio(self, duration_seconds=6):
        self._run_ffmpeg(
            "-f", "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=" + str(duration_seconds),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(self.audio_path),
        )

    def _make_raw(self, duration_seconds=8, *, with_audio=True, output=None):
        target = output or self.raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        arguments = [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x29314a:s=320x180:r=24:d={duration_seconds}",
        ]
        if with_audio:
            arguments.extend([
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=48000:duration={duration_seconds}",
            ])
        arguments.extend(["-t", str(duration_seconds), "-map", "0:v:0", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
        if with_audio:
            arguments.extend(["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"])
        else:
            arguments.append("-an")
        arguments.append(str(target))
        self._run_ffmpeg(*arguments)
        return target

    def _adapter(self):
        return FinalizationMediaAdapter(ffmpeg_path=str(FFMPEG), ffprobe_path=str(FFPROBE))


class FramePlanTests(unittest.TestCase):
    def test_planner_uses_installed_duration_semantics_and_covers_target(self):
        plan = build_h3_timing_plan(6_679)
        self.assertEqual(plan["contract_id"], "minimax_h3_24fps_5_plus_17n_covering_v2")
        self.assertEqual(plan["duration_formula"], "generated_frame_count / h3_fps")
        self.assertEqual(plan["generated_frame_count"], 175)
        self.assertEqual(plan["generated_duration_ms"], 7291.666667)
        self.assertTrue(plan["coverage_satisfied"])
        self.assertEqual(plan["generated_frame_count"] % H3_FRAME_STEP, H3_MINIMUM_FRAMES % H3_FRAME_STEP)

    def test_planner_selects_smallest_covering_count_at_boundaries(self):
        self.assertEqual(build_h3_timing_plan(1)["generated_frame_count"], H3_MINIMUM_FRAMES)
        self.assertEqual(build_h3_timing_plan(3_750)["generated_frame_count"], 90)
        self.assertEqual(build_h3_timing_plan(3_751)["generated_frame_count"], 107)
        self.assertEqual(build_h3_timing_plan(10_000)["generated_frame_count"], 243)

    def test_planner_fails_closed_above_maximum(self):
        with self.assertRaises(TimingContractError) as context:
            build_h3_timing_plan(150_000)
        self.assertEqual(context.exception.code, "TIMING_FRAME_LIMIT")

    def test_preparation_fingerprint_basis_changes_with_corrected_timing(self):
        project_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        project = {
            "project_id": project_id,
            "scenes": [{
                "scene_id": scene_id,
                "timeline_start_ms": 0,
                "timeline_end_ms": 6_679,
                "exact_duration_ms": 6_679,
            }],
        }
        common = {
            "prompt_state": {"generation_method": "keyframe_i2v", "saved_source_fingerprint": "x", "saved_relay_fingerprint": "y", "saved_final_prompt": "z", "source_fingerprint": "x"},
            "source_audio_identity": {"sha256": "a"},
            "visual_inputs": [],
            "workflow_identity": {"workflow_id": "w"},
        }
        old_plan = dict(build_h3_timing_plan(6_679), generated_frame_count=158, generated_duration_ms=6583.333333)
        old = build_preparation_fingerprint(project, scene_id, timing_plan=old_plan, **common)
        new = build_preparation_fingerprint(project, scene_id, timing_plan=build_h3_timing_plan(6_679), **common)
        self.assertNotEqual(old, new)


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg and ffprobe are required for synthetic media qualification")
class SyntheticFinalizationTests(Phase8CFixture):
    def test_raw_ready_state_has_no_finalize_requirement_until_raw_is_current(self):
        self._make_audio()
        self._make_raw()
        state = summarize_render_job_finalization(
            self.storage,
            self.job,
            preflight=self.preflight,
            raw_output_root=self.raw_root,
        )
        self.assertEqual(state["state"], FINALIZATION_STATE_RAW_READY)
        self.assertTrue(state["eligible"])

    def test_probe_captures_container_video_audio_dimensions_fps_and_duration(self):
        self._make_audio()
        self._make_raw()
        probe = MediaProbeAdapter(ffprobe_path=str(FFPROBE)).probe(self.raw_path)
        self.assertTrue(probe["format"]["format_name"])
        self.assertEqual(len(probe["video_streams"]), 1)
        self.assertEqual(probe["video"]["width"], 320)
        self.assertEqual(probe["video"]["height"], 180)
        self.assertEqual((probe["video"]["fps_numerator"], probe["video"]["fps_denominator"]), (24, 1))
        self.assertGreater(probe["video"]["duration_ms"], self.target_duration_ms)
        self.assertEqual(len(probe["audio_streams"]), 1)

    def test_long_raw_video_finalizes_with_authoritative_external_audio(self):
        self._make_audio()
        self._make_raw()
        raw_before = self.raw_path.read_bytes()
        result = finalize_render_job(
            self.storage,
            self.project_id,
            self.job["job_id"],
            preflight=self.preflight,
            media_adapter=self._adapter(),
            raw_output_root=self.raw_root,
        )
        self.assertEqual(result["state"], FINALIZATION_STATE_FINALIZED)
        self.assertEqual(result["target_duration_ms"], self.target_duration_ms)
        self.assertEqual(result["policy"]["video_codec"], "libx264")
        self.assertEqual(result["policy"]["audio_codec"], "aac")
        self.assertEqual(result["authoritative_audio"]["source"], "phase8a_prepared_scene_audio")
        final_path = self.project_root / result["output"]["relative_path"]
        self.assertTrue(final_path.is_file())
        self.assertEqual(final_path.suffix, ".mp4")
        self.assertEqual(self.raw_path.read_bytes(), raw_before)
        self.assertEqual(result["validation"]["width"], 320)
        self.assertEqual(result["validation"]["height"], 180)
        self.assertEqual(result["validation"]["video_stream_count"], 1)
        self.assertEqual(result["validation"]["audio_stream_count"], 1)
        self.assertEqual(result["validation"]["audio_codec"], "aac")
        self.assertLessEqual(
            abs(result["validation"]["container_duration_ms"] - self.target_duration_ms),
            result["timing_tolerances"]["container_duration_tolerance_ms"],
        )
        self.assertIn("1:a:0", result["ffmpeg_command"])
        self.assertNotIn("0:a:0", result["ffmpeg_command"])

    def test_finalization_is_idempotent_for_unchanged_inputs(self):
        self._make_audio()
        self._make_raw()
        first = finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        final_path = self.project_root / first["output"]["relative_path"]
        first_identity = final_path.stat().st_mtime_ns
        second = finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertTrue(second["reused"])
        self.assertEqual(second["finalization_fingerprint"], first["finalization_fingerprint"])
        self.assertEqual(final_path.stat().st_mtime_ns, first_identity)

    def test_raw_change_marks_finalization_stale(self):
        self._make_audio()
        self._make_raw()
        first = finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self._make_raw(duration_seconds=9)
        state = summarize_render_job_finalization(self.storage, self.job, preflight=self.preflight, raw_output_root=self.raw_root)
        self.assertEqual(state["state"], FINALIZATION_STATE_STALE)
        self.assertNotEqual(state["current_fingerprint"], first["finalization_fingerprint"])

    def test_authoritative_audio_change_marks_finalization_stale(self):
        self._make_audio()
        self._make_raw()
        finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:sample_rate=48000:duration=6",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(self.audio_path),
        )
        state = summarize_render_job_finalization(self.storage, self.job, preflight=self.preflight, raw_output_root=self.raw_root)
        self.assertEqual(state["state"], FINALIZATION_STATE_STALE)

    def test_stale_preparation_blocks_finalization_without_deleting_raw(self):
        self._make_audio()
        self._make_raw()
        changed_preflight = {
            **self.preflight,
            "scenes": [{**self.preflight["scenes"][0], "preparation_status": "stale", "preparation_current": False, "preparation_fingerprint": "b" * 64}],
        }
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=changed_preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(context.exception.code, "FINALIZATION_STALE")
        self.assertTrue(self.raw_path.is_file())
        state = summarize_render_job_finalization(
            self.storage,
            self.job,
            preflight=changed_preflight,
            raw_output_root=self.raw_root,
        )
        self.assertEqual(state["state"], FINALIZATION_STATE_NOT_AVAILABLE)
        self.assertFalse(state["eligible"])

    def test_short_raw_fails_closed_without_motion_alteration(self):
        self._make_audio()
        self._make_raw(duration_seconds=3)
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(context.exception.code, "RAW_VIDEO_TOO_SHORT")
        self.assertIn("Re-render this scene with a current timing plan", context.exception.message)
        self.assertFalse((self.project_root / "renders" / self.scene_id / "final" / f"{self.job['job_id']}.mp4").exists())
        state = summarize_render_job_finalization(self.storage, self.job, preflight=self.preflight, raw_output_root=self.raw_root)
        self.assertEqual(state["state"], FINALIZATION_STATE_FAILED)
        self.assertFalse(state["eligible"])

    def test_failed_replacement_retains_previous_valid_final_output(self):
        self._make_audio()
        self._make_raw()
        first = finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        final_path = self.project_root / first["output"]["relative_path"]
        previous_bytes = final_path.read_bytes()
        self._make_raw(duration_seconds=3)
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(context.exception.code, "RAW_VIDEO_TOO_SHORT")
        self.assertEqual(final_path.read_bytes(), previous_bytes)
        metadata_path = self.project_root / "renders" / self.scene_id / "final" / f"{self.job['job_id']}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["state"], FINALIZATION_STATE_FAILED)
        self.assertEqual(metadata["output"]["relative_path"], first["output"]["relative_path"])

    def test_final_output_deletion_marks_finalization_stale(self):
        self._make_audio()
        self._make_raw()
        result = finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        (self.project_root / result["output"]["relative_path"]).unlink()
        state = summarize_render_job_finalization(self.storage, self.job, preflight=self.preflight, raw_output_root=self.raw_root)
        self.assertEqual(state["state"], FINALIZATION_STATE_STALE)

    def test_no_video_raw_is_rejected_and_raw_is_not_deleted(self):
        self._make_audio()
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=6",
            "-c:a",
            "aac",
            "-vn",
            str(self.raw_path),
        )
        original = self.raw_path.read_bytes()
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(context.exception.code, "RAW_VIDEO_NO_VIDEO")
        self.assertEqual(self.raw_path.read_bytes(), original)

    def test_corrupt_raw_is_rejected_and_preserved(self):
        self._make_audio()
        self.raw_path.write_bytes(b"not a media file")
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertIn(context.exception.code, {"MEDIA_PROBE_FAILED", "MEDIA_PROBE_INVALID"})
        self.assertEqual(self.raw_path.read_bytes(), b"not a media file")

    def test_raw_output_traversal_is_rejected(self):
        unsafe_job = {
            **self.job,
            "output": {
                **self.job["output"],
                "subfolder": "..",
                "filename": "outside.mp4",
                "relative_path": "../outside.mp4",
            },
        }
        with self.assertRaises(RenderFinalizationError) as context:
            _resolve_raw_path(unsafe_job, output_root=self.raw_root)
        self.assertEqual(context.exception.code, "OUTPUT_PATH_UNSAFE")

    def test_missing_authoritative_audio_is_specific(self):
        self._make_raw()
        with self.assertRaises(RenderFinalizationError) as context:
            finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(context.exception.code, "AUTHORITATIVE_AUDIO_MISSING")
        self.assertEqual(context.exception.message, "Authoritative scene audio is missing.")

    def test_project_save_and_prompt_provenance_are_untouched(self):
        self._make_audio()
        self._make_raw()
        project_path = self.project_root / "project.json"
        before = project_path.read_bytes()
        finalize_render_job(self.storage, self.project_id, self.job["job_id"], preflight=self.preflight, media_adapter=self._adapter(), raw_output_root=self.raw_root)
        self.assertEqual(project_path.read_bytes(), before)

    def test_finalization_does_not_require_target_hardware_gate(self):
        source = (ROOT / "backend" / "render_finalize.py").read_text(encoding="utf-8")
        self.assertNotIn("TargetHardwareGate", source)
        self.assertNotIn("submit_prompt", source)

    def test_finalization_route_and_frontend_surface_are_separate_from_h3_gate(self):
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertIn("/render/jobs/{job_id}/finalize", routes)
        self.assertIn("renderJobFinalizePath", extension)
        self.assertIn("Finalize Scene", extension)
        self.assertIn("FINALIZED", extension)
        self.assertIn("H3 plan", extension)


class ProbeContractTests(unittest.TestCase):
    def test_probe_rejects_malformed_ffprobe_json(self):
        class Completed:
            returncode = 0
            stdout = "not json"

        with self.assertRaises(RenderFinalizationError) as context:
            MediaProbeAdapter(ffprobe_path="ffprobe", runner=lambda *_args, **_kwargs: Completed()).probe(Path(__file__))
        self.assertEqual(context.exception.code, "MEDIA_PROBE_INVALID")

    def test_probe_captures_rational_rates_and_frame_counts(self):
        class Completed:
            returncode = 0
            stdout = json.dumps({
                "format": {"format_name": "mov,mp4", "duration": "6.500"},
                "streams": [{
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 960,
                    "height": 544,
                    "avg_frame_rate": "24/1",
                    "time_base": "1/12288",
                    "duration": "6.500",
                    "nb_read_frames": "156",
                }],
            })

        probe = MediaProbeAdapter(ffprobe_path="ffprobe", runner=lambda *_args, **_kwargs: Completed()).probe(Path(__file__))
        self.assertEqual(probe["video"]["fps_numerator"], 24)
        self.assertEqual(probe["video"]["frame_count"], 156)
        self.assertEqual(probe["video"]["duration_ms"], 6500)
