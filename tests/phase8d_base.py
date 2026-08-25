"""Shared deterministic fixtures for Phase 8D batch orchestration tests.

These tests drive the backend-owned batch runner against fake ComfyUI,
preparation, and finalization services so that sequential orchestration,
failure isolation, pause/resume, and recovery are all observable without a
live GPU render.  No H3 work is submitted.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectStorage
from backend.render import (
    RenderPreparationBlocked,
    RenderPreparationError,
)
from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_BLOCKED,
    ACTION_FINALIZE_RAW,
    ACTION_PREPARE_AND_RENDER,
    ACTION_RENDER_PREPARED,
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_ENDED,
    BATCH_PAUSED,
    BATCH_PAUSED_RECOVERY,
    BATCH_PAUSE_REQUESTED,
    BATCH_RUNNING,
    BatchRunner,
    BatchStore,
    RenderBatchConflict,
    RenderBatchError,
    batch_status,
    end_batch,
    manual_action_blocker,
    pause_batch_after_current,
    plan_batch,
    plan_scene_action,
    preview_batch,
    retry_failed_batch,
    resume_batch,
    start_batch,
)
from backend.render import build_h3_timing_plan
from backend.render_finalize import RenderFinalizationError, finalize_render_job
from backend.render_jobs import (
    ComfyUIClientError,
    JobStore,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    new_job_record,
    transition_job,
)
from backend.scenes import build_scenes
from backend.visuals import default_visuals_for_scenes
from backend.projects import default_prompts_for_scenes


FINGERPRINT = "f" * 64
SCENE_DURATION_MS = 6000
VIDEO_WIDTH = 320
VIDEO_HEIGHT = 180
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2


class FakeMediaAdapter:
    """Deterministic FFmpeg/ffprobe stand-in for finalization validation."""

    def __init__(self, *, fail_encode=False):
        self.fail_encode = fail_encode
        self.encodes = []

    def _video_stream(self):
        return {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "fps_numerator": 24,
            "fps_denominator": 1,
            "fps": 24.0,
            "time_base": "1/24",
            "duration_ms": SCENE_DURATION_MS,
            "frame_count": 144,
            "channels": None,
            "sample_rate": None,
            "pix_fmt": "yuv420p",
            "color_space": None,
            "color_transfer": None,
            "color_primaries": None,
        }

    def _audio_stream(self):
        return {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "width": None,
            "height": None,
            "fps_numerator": None,
            "fps_denominator": None,
            "fps": None,
            "time_base": "1/48000",
            "duration_ms": SCENE_DURATION_MS,
            "frame_count": None,
            "channels": AUDIO_CHANNELS,
            "sample_rate": AUDIO_SAMPLE_RATE,
            "pix_fmt": None,
            "color_space": None,
            "color_transfer": None,
            "color_primaries": None,
        }

    def probe(self, path):
        p = Path(path)
        name = p.name
        if name == "scene_audio.wav":
            audio = self._audio_stream()
            audio["codec_name"] = "pcm_s16le"
            return {
                "format": {"format_name": "wav", "duration_ms": SCENE_DURATION_MS, "size": 1, "start_time": "0"},
                "streams": [audio],
                "video_streams": [],
                "audio_streams": [audio],
                "video": None,
                "audio": audio,
            }
        video = self._video_stream()
        audio = self._audio_stream()
        if p.parent.name == "production" or name.startswith("production_") or (name.startswith("mvb_") and "final" not in p.parts):
            video["width"] = int(VIDEO_WIDTH * 2)
            video["height"] = int(VIDEO_HEIGHT * 2)
        else:
            video["width"] = VIDEO_WIDTH
            video["height"] = VIDEO_HEIGHT
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration_ms": SCENE_DURATION_MS, "size": 1, "start_time": "0"},
            "streams": [video, audio],
            "video_streams": [video],
            "audio_streams": [audio],
            "video": video,
            "audio": audio,
        }

    def encode(self, raw_video, authoritative_audio, candidate, *, target_duration_ms):
        if self.fail_encode:
            raise RenderFinalizationError("FINAL_MEDIA_ENCODE_FAILED", "Simulated encode failure.")
        self.encodes.append((str(raw_video), str(authoritative_audio), str(candidate), target_duration_ms))
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"final-scene-mp4")
        return ["fake-ffmpeg", "-i", str(raw_video), str(candidate)]

    def remux_authoritative_audio(self, upscaled_video, final_scene_video, candidate):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"production-candidate-mp4")
        return ["fake-ffmpeg", "-i", str(upscaled_video), "-i", str(final_scene_video), "-c", "copy", str(candidate)]


class FakeComfyClient:
    """In-memory ComfyUI adapter with controllable availability and history."""

    def __init__(self, *, prompt_ids=None):
        self.submitted = []
        self.pending = []
        self.running = []
        self.history = {}
        self.calls = {"submit": 0, "queue": 0, "history": 0, "delete": 0, "interrupt": 0}
        self.prompt_ids = list(prompt_ids or [])
        self.unavailable = False
        self.submit_error = None

    def submit_prompt(self, workflow):
        if self.submit_error is not None:
            raise self.submit_error
        self.calls["submit"] += 1
        self.submitted.append(workflow)
        prompt_id = self.prompt_ids.pop(0) if self.prompt_ids else str(uuid.uuid4())
        self.pending.append(prompt_id)
        return {"prompt_id": prompt_id, "queue_number": self.calls["submit"], "node_errors": {}}

    def get_queue(self):
        self.calls["queue"] += 1
        if self.unavailable:
            raise ComfyUIClientError("COMFYUI_UNAVAILABLE", "The local ComfyUI API could not be reached.", transient=True)
        return {
            "queue_running": [[0, pid] for pid in self.running],
            "queue_pending": [[0, pid] for pid in self.pending],
        }

    def get_history(self, prompt_id):
        self.calls["history"] += 1
        if self.unavailable:
            raise ComfyUIClientError("COMFYUI_UNAVAILABLE", "The local ComfyUI API could not be reached.", transient=True)
        return self.history.get(prompt_id, {})

    def delete_queued(self, prompt_id):
        self.calls["delete"] += 1
        self.pending = [p for p in self.pending if p != prompt_id]
        return {"deleted": [prompt_id]}

    def interrupt_running(self, prompt_id):
        self.calls["interrupt"] += 1
        self.running = [p for p in self.running if p != prompt_id]
        return {"interrupted": True}

    def cancellation_capabilities(self):
        return {"queued": True, "running": True, "running_scope": "global_engine_interrupt_targeted_by_prompt_id"}

    # -- test controls ------------------------------------------------------

    def begin_running(self, prompt_id):
        self.pending = [p for p in self.pending if p != prompt_id]
        if prompt_id not in self.running:
            self.running.append(prompt_id)

    def succeed(self, prompt_id, node_id, relative_path):
        self.pending = [p for p in self.pending if p != prompt_id]
        self.running = [p for p in self.running if p != prompt_id]
        filename = Path(relative_path).name
        subfolder = Path(relative_path).parent.as_posix()
        self.history[prompt_id] = {
            "prompt_id": prompt_id,
            "status": {"status_str": "success", "messages": []},
            "outputs": {node_id: {"videos": [{"filename": filename, "subfolder": subfolder, "type": "output"}]}},
        }

    def fail(self, prompt_id):
        self.pending = [p for p in self.pending if p != prompt_id]
        self.running = [p for p in self.running if p != prompt_id]
        self.history[prompt_id] = {
            "prompt_id": prompt_id,
            "status": {"status_str": "error", "messages": [["error", "simulated execution failure"]]},
            "outputs": {},
        }


class Phase8DBase(unittest.TestCase):
    SCENE_COUNT = 3

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")
        project = self.storage.create_project("Phase 8D batch")
        self.project_id = project["project_id"]
        duration = 6000
        cues = [
            {"cue_number": i + 1, "start_ms": i * duration, "end_ms": (i + 1) * duration, "text": f"Scene {i + 1}"}
            for i in range(self.SCENE_COUNT)
        ]
        scenes = build_scenes(cues, self.SCENE_COUNT * duration)
        project = self.storage.save_project(
            self.project_id,
            {
                **project,
                "source": {
                    "master_audio": {
                        "stored_name": "master_audio.wav",
                        "original_name": "master.wav",
                        "duration_ms": self.SCENE_COUNT * duration,
                    },
                    "lyrics_srt": {"stored_name": "lyrics.srt", "original_name": "lyrics.srt", "cue_count": self.SCENE_COUNT},
                },
                "scenes": scenes,
                "visuals": default_visuals_for_scenes(scenes),
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )
        self.scene_ids = [scene["scene_id"] for scene in scenes]
        self.project_root = self.storage.project_directory(self.project_id)
        self.raw_root = Path(self.temp_directory.name) / "comfy-output"
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.media_adapter = FakeMediaAdapter()
        for scene_id in self.scene_ids:
            self._write_preparation_artifacts(scene_id)

        self.client = FakeComfyClient()
        self.global_workflow_ready = True
        self.global_requirements_ready = True
        self.scene_overrides = {}
        self.preparer_calls = []
        self.preparer_fail = None
        self.preparer_block = False
        self.finalizer_calls = []
        self.finalizer_fail = None

        self._ffmpeg_patch = patch("backend.render_batch.find_ffmpeg", return_value="/fake/ffmpeg")
        self.addCleanup(self._ffmpeg_patch.stop)
        self._ffmpeg_patch.start()

    def _write_preparation_artifacts(self, scene_id, fingerprint=FINGERPRINT):
        render_dir = self.project_root / "renders" / scene_id
        inputs = render_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        audio_path = inputs / "scene_audio.wav"
        audio_path.write_bytes(b"prepared-scene-audio")
        workflow_path = render_dir / "workflow.json"
        workflow_path.write_text("{}", encoding="utf-8")
        audio_rel = audio_path.relative_to(self.project_root).as_posix()
        workflow_rel = workflow_path.relative_to(self.project_root).as_posix()
        preparation = {
            "preparation_version": 2,
            "project_id": self.project_id,
            "scene_id": scene_id,
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": fingerprint,
            "source_audio": {
                "prepared_relative_path": audio_rel,
                "render_relative_path": audio_rel,
                "authoritative_duration_ms": SCENE_DURATION_MS,
            },
            "visual_inputs": [],
            "timing": build_h3_timing_plan(SCENE_DURATION_MS),
            "workflow": {"relative_path": workflow_rel},
        }
        (render_dir / "preparation.json").write_text(json.dumps(preparation), encoding="utf-8")

    def finalize_job_real(self, job):
        preflight = {
            "project_id": self.project_id,
            "scenes": [self.scene_preflight_entry(job["scene_id"], 1)],
        }
        return finalize_render_job(
            self.storage,
            self.project_id,
            job["job_id"],
            preflight=preflight,
            media_adapter=self.media_adapter,
            raw_output_root=self.raw_root,
        )

    # -- controlled readiness ------------------------------------------------

    def scene_preflight_entry(self, scene_id, sequence):
        override = self.scene_overrides.get(scene_id, {})
        prep_status = override.get("preparation_status", "current")
        content = override.get("content_ready", True)
        workflow = override.get("workflow_ready", True)
        runtime = override.get("runtime_requirements_ready", True)
        prep_ready = override.get("preparation_ready", True)
        eligible = content and workflow and runtime and prep_status == "current"
        blockers = override.get("blockers", [])
        return {
            "scene_id": scene_id,
            "sequence": sequence,
            "generation_method": "keyframe_i2v",
            "content_ready": content,
            "workflow_ready": workflow,
            "runtime_requirements_ready": runtime,
            "preparation_current": prep_status == "current",
            "preparation_status": prep_status,
            "preparation_ready": prep_ready,
            "preparation_fingerprint": override.get("preparation_fingerprint", FINGERPRINT),
            "duration_ms": 6000,
            "exact_duration_ms": 6000,
            "target_duration_ms": 6000,
            "blockers": blockers,
            "execution_eligibility": {
                "eligible": eligible,
                "allowed": eligible,
                "status": "READY" if eligible else "BLOCKED",
                "message": "ready" if eligible else "blocked",
                "blockers": blockers,
            },
        }

    def preflight_builder(self, project, storage):
        return {
            "response_version": 1,
            "project_id": self.project_id,
            "workflow_ready": self.global_workflow_ready,
            "runtime_requirements_ready": self.global_requirements_ready,
            "scene_count": len(self.scene_ids),
            "scenes": [
                self.scene_preflight_entry(scene_id, index + 1)
                for index, scene_id in enumerate(self.scene_ids)
            ],
        }

    def package_loader(self, storage, project, scene_id, scene_preflight):
        prefix = f"music_video_builder/{self.project_id}/{scene_id}/render"
        return {
            "workflow": {
                "1": {"class_type": "LoadImage", "inputs": {"image": f"{scene_id}.png"}},
                "20": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": prefix, "format": "video/h264-mp4"}},
            },
            "generation_method": "keyframe_i2v",
            "preparation_fingerprint": scene_preflight.get("preparation_fingerprint", FINGERPRINT),
            "output_node_id": "20",
            "expected_filename_prefix": prefix,
            "output_format": "video/h264-mp4",
        }

    def compat_ok(self, workflow, client):
        return {"state": "COMPATIBLE", "checked": True}

    def preparer(self, storage, project_id, scene_id):
        self.preparer_calls.append(scene_id)
        if self.preparer_block:
            entry = self.scene_preflight_entry(scene_id, 1)
            raise RenderPreparationBlocked(entry)
        if self.preparer_fail is not None:
            raise RenderPreparationError(self.preparer_fail, "Simulated preparation failure.")
        self.scene_overrides.setdefault(scene_id, {})["preparation_status"] = "current"
        return {"status": "prepared", "scene_id": scene_id}

    def finalizer(self, storage, project_id, job_id, *, preflight=None, media_adapter=None, raw_output_root=None):
        self.finalizer_calls.append(job_id)
        if self.finalizer_fail is not None:
            raise RenderFinalizationError(self.finalizer_fail, "Simulated finalization failure.")
        return {"state": "FINALIZED", "job_id": job_id}

    # -- runner / API helpers ------------------------------------------------

    def batch_seams(self):
        return {
            "client": self.client,
            "preflight_builder": self.preflight_builder,
            "package_loader": self.package_loader,
            "compatibility_validator": self.compat_ok,
            "raw_output_root": self.raw_root,
            "finalizer": self.finalizer,
            "preparer": self.preparer,
            "media_adapter": self.media_adapter,
        }

    def runner_seams(self):
        return {**self.batch_seams(), "poll_interval_seconds": 0.0}

    def make_runner(self):
        return BatchRunner(self.storage, self.project_id, **self.runner_seams())

    def start(self, scene_ids=None, *, start_runner=False, poll_interval_seconds=1.0):
        return start_batch(
            self.storage,
            self.project_id,
            scene_ids=scene_ids,
            start_runner=start_runner,
            poll_interval_seconds=poll_interval_seconds,
            **self.batch_seams(),
        )

    def resume(self, *, start_runner=False):
        return resume_batch(
            self.storage,
            self.project_id,
            start_runner=start_runner,
            **self.batch_seams(),
        )

    def pump(self, runner, max_steps=400):
        record = None
        for _ in range(max_steps):
            record = runner.tick()
            if record is None or record["state"] not in {BATCH_RUNNING, BATCH_PAUSE_REQUESTED}:
                return record
        return record

    def run_batch_to_completion(self, runner, *, fail_scenes=(), max_ticks=400):
        """Drive a batch deterministically: complete each submitted job in the
        fake runtime until the batch reaches a terminal or paused state."""

        from backend.render_batch import ITEM_RENDERING

        record = runner.tick()
        ticks = 0
        while record is not None and record["state"] in {BATCH_RUNNING, BATCH_PAUSE_REQUESTED} and ticks < max_ticks:
            rendering = [item for item in record["items"] if item["disposition"] == ITEM_RENDERING]
            if not rendering:
                record = runner.tick()
                ticks += 1
                continue
            item = rendering[0]
            job = JobStore(self.storage).load(self.project_id, item["job_id"])
            if item["scene_id"] in fail_scenes:
                self.client.fail(job["comfy_prompt_id"])
            else:
                self.complete_job_raw(job)
            record = runner.tick()
            ticks += 1
        return record

    def active_record(self):
        return BatchStore(self.storage).load_active(self.project_id)

    def latest_record(self):
        records = BatchStore(self.storage).list_project(self.project_id)
        return records[-1] if records else None

    def item_by_scene(self, record, scene_id):
        for item in record.get("items", []):
            if item.get("scene_id") == scene_id:
                return item
        return None

    # -- job lifecycle helpers ------------------------------------------------

    def submit_single(self, scene_id):
        """Submit one scene through the authoritative per-scene service."""
        from backend.render_jobs import submit_render_job

        preflight = {"project_id": self.project_id, "scenes": [self.scene_preflight_entry(scene_id, 1)]}
        return submit_render_job(
            self.storage,
            self.project_id,
            scene_id,
            client=self.client,
            preflight=preflight,
            package_loader=self.package_loader,
            compatibility_validator=self.compat_ok,
        )

    def complete_job_raw(self, job):
        """Write the deterministic raw output file and mark ComfyUI success."""
        production = job["production_output"]
        prefix = production["filename_prefix"]
        node_id = production["node_id"]
        relative = f"{prefix}.mp4"
        path = self.raw_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"raw-h3-output")
        self.client.succeed(job["comfy_prompt_id"], node_id, relative)
        return relative

    def reconcile(self):
        from backend.render_jobs import reconcile_project_jobs

        return reconcile_project_jobs(self.storage, self.project_id, client=self.client, output_root=self.raw_root)

    def scene_jobs(self, scene_id):
        return JobStore(self.storage).list_scene(self.project_id, scene_id)

    def make_succeeded_job_with_final(self, scene_id, *, finalized=True):
        """Create a SUCCEEDED job with current raw output (and optional final)."""
        prefix = f"music_video_builder/{self.project_id}/{scene_id}/render"
        record = new_job_record(
            self.project_id,
            scene_id,
            "keyframe_i2v",
            FINGERPRINT,
            output_node_id="20",
            expected_filename_prefix=prefix,
            output_format="video/h264-mp4",
        )
        record = transition_job(record, "SUBMITTING", reason="test")
        prompt_id = str(uuid.uuid4())
        record = transition_job(record, QUEUED, reason="test", updates={"comfy_prompt_id": prompt_id})
        relative = f"{prefix}/{record['job_id']}.mp4"
        path = self.raw_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"raw-h3-output")
        record = transition_job(
            record,
            SUCCEEDED,
            reason="test",
            updates={
                "output": {
                    "prompt_id": prompt_id,
                    "output_node_id": "20",
                    "relative_path": relative,
                    "filename": Path(relative).name,
                    "subfolder": Path(relative).parent.as_posix(),
                    "format": "video/h264-mp4",
                    "raw_h3_output": True,
                },
                "output_discovery": {"state": "AVAILABLE", "failure": None},
            },
        )
        record["production_output"]["filename_prefix"] = f"{prefix}/{record['job_id']}"
        record = JobStore(self.storage).save(record)
        return record
