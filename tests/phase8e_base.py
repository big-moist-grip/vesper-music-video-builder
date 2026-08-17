"""Shared deterministic fixtures for Phase 8E production post-processing tests.

These fixtures provide fake ComfyUI clients, media adapters, and project setups
to test the production post-processing lifecycle, method contracts, resolution
policies, output ownership, audio authority, and failure isolation without
running live GPU models or live FFmpeg processes.
"""

from copy import deepcopy
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Mapping

from backend.projects import (
    ProjectStorage,
    atomic_write_json,
    default_prompts_for_scenes,
)
from backend.render import H3_FPS
from backend.render_finalize import (
    FINALIZATION_STATE_FINALIZED,
    RenderFinalizationError,
)
from backend.render_jobs import (
    JobStore,
    SUCCEEDED,
    new_job_record,
    transition_job,
)
from backend.render_production import (
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    POSTPROCESS_SUCCEEDED,
    PostprocessJobStore,
    ProductionMediaAdapter,
    new_postprocess_job_record,
    transition_postprocess_job,
)
from backend.scenes import build_scenes
from backend.visuals import default_visuals_for_scenes


NATIVE_WIDTH = 960
NATIVE_HEIGHT = 544
UPSCALE_WIDTH = 1920
UPSCALE_HEIGHT = 1088
DEFAULT_DURATION_MS = 3000
DEFAULT_FRAME_COUNT = 72
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2


class FakeProductionMediaAdapter:
    """Deterministic MediaProbeAdapter stand-in for production post-processing."""

    def __init__(
        self,
        *,
        fail_remux: bool = False,
        fail_probe: bool = False,
        fps_numerator: int = 24,
        fps_denominator: int = 1,
        width: int = UPSCALE_WIDTH,
        height: int = UPSCALE_HEIGHT,
        duration_ms: int = DEFAULT_DURATION_MS,
        frame_count: int = DEFAULT_FRAME_COUNT,
        audio_duration_ms: int = DEFAULT_DURATION_MS,
        audio_sample_rate: int = AUDIO_SAMPLE_RATE,
        audio_channels: int = AUDIO_CHANNELS,
        audio_codec: str = "aac",
    ):
        self.fail_remux = fail_remux
        self.fail_probe = fail_probe
        self.fps_numerator = fps_numerator
        self.fps_denominator = fps_denominator
        self.width = width
        self.height = height
        self.duration_ms = duration_ms
        self.frame_count = frame_count
        self.audio_duration_ms = audio_duration_ms
        self.audio_sample_rate = audio_sample_rate
        self.audio_channels = audio_channels
        self.audio_codec = audio_codec
        self.remux_calls: list[tuple[Path, Path, Path]] = []
        self.probe_calls: list[Path] = []

    def probe(self, path: Path) -> dict[str, object]:
        self.probe_calls.append(path)
        if self.fail_probe:
            raise RenderFinalizationError("MEDIA_PROBE_FAILED", "Media probe failed.")
        # If probing native final scene, use native width/height if path indicates final_scene
        w = NATIVE_WIDTH if "final_scene" in str(path) and "production" not in str(path) else self.width
        h = NATIVE_HEIGHT if "final_scene" in str(path) and "production" not in str(path) else self.height
        return {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration_ms": self.duration_ms,
            "video": {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": w,
                "height": h,
                "fps_numerator": self.fps_numerator,
                "fps_denominator": self.fps_denominator,
                "fps": float(self.fps_numerator) / float(self.fps_denominator),
                "time_base": f"1/{self.fps_numerator}",
                "duration_ms": self.duration_ms,
                "frame_count": self.frame_count,
                "pix_fmt": "yuv420p",
            },
            "audio": {
                "index": 1,
                "codec_type": "audio",
                "codec_name": self.audio_codec,
                "duration_ms": self.audio_duration_ms,
                "channels": self.audio_channels,
                "sample_rate": self.audio_sample_rate,
            },
        }

    def remux_authoritative_audio(self, upscaled_video: Path, final_scene_video: Path, candidate: Path) -> list[str]:
        self.remux_calls.append((upscaled_video, final_scene_video, candidate))
        if self.fail_remux:
            raise RenderFinalizationError("PRODUCTION_REMUX_FAILED", "Simulated remux failure.")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"PRODUCTION_CANDIDATE_WITH_AUTHORITATIVE_AUDIO")
        return ["ffmpeg", "-i", str(upscaled_video), "-i", str(final_scene_video), "-c", "copy", str(candidate)]


class FakeComfyClientForPostprocess:
    """Deterministic ComfyUIClient stand-in for post-process prompt submission & history."""

    def __init__(self, *, fail_submit: bool = False, prompt_id: str | None = None):
        self.fail_submit = fail_submit
        self.prompt_id = prompt_id or str(uuid.uuid4())
        self.submitted_prompts: list[dict[str, object]] = []
        self.history_records: dict[str, dict[str, object]] = {}
        self.queue_pending: list[object] = []
        self.queue_running: list[object] = []

    def submit_prompt(self, workflow: dict[str, object]) -> dict[str, object]:
        if self.fail_submit:
            from backend.render_jobs import ComfyUIClientError
            raise ComfyUIClientError("COMFYUI_UNAVAILABLE", "ComfyUI is unavailable.", transient=True)
        self.submitted_prompts.append(deepcopy(workflow))
        return {"prompt_id": self.prompt_id, "number": 1}

    def get_queue(self) -> dict[str, object]:
        return {
            "queue_pending": deepcopy(self.queue_pending),
            "queue_running": deepcopy(self.queue_running),
        }

    def get_history(self, prompt_id: str | None = None) -> dict[str, object]:
        if prompt_id is not None:
            entry = self.history_records.get(prompt_id)
            return {prompt_id: deepcopy(entry)} if entry is not None else {}
        return deepcopy(self.history_records)


class Phase8ETestBase(unittest.TestCase):
    """Base class providing a temp project with initialized scenes and Final Scene artifacts."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.projects_root = Path(self.temp_dir.name) / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.storage = ProjectStorage(self.projects_root)
        self.project_id = str(uuid.uuid4())
        self.input_root = Path(self.temp_dir.name) / "comfy_input"
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.output_root = Path(self.temp_dir.name) / "comfy_output"
        self.output_root.mkdir(parents=True, exist_ok=True)

        # Build basic 2-scene project
        self.master_audio = {
            "stored_name": "master_audio.wav",
            "original_name": "song.wav",
            "duration_ms": 6000,
        }
        self.lyrics_srt = {
            "stored_name": "lyrics.srt",
            "original_name": "lyrics.srt",
            "cue_count": 2,
        }
        cues = [
            {"cue_number": 1, "start_ms": 0, "end_ms": 3000, "text": "First scene"},
            {"cue_number": 2, "start_ms": 3000, "end_ms": 6000, "text": "Second scene"},
        ]
        self.scenes = build_scenes(cues, self.master_audio["duration_ms"])
        self.scene_1_id = self.scenes[0]["scene_id"]
        self.scene_2_id = self.scenes[1]["scene_id"]

        project = self.storage.create_project("Phase 8E Test Project")
        self.project_id = project["project_id"]
        self.project_data = self.storage.save_project(
            self.project_id,
            {
                **project,
                "source": {"master_audio": self.master_audio, "lyrics_srt": self.lyrics_srt},
                "scenes": self.scenes,
                "visuals": default_visuals_for_scenes(self.scenes),
                "prompts": default_prompts_for_scenes(self.scenes),
            },
            allow_visuals_change=True,
        )

    def _setup_final_scene(
        self,
        scene_id: str,
        *,
        duration_ms: int = DEFAULT_DURATION_MS,
        content: bytes = b"FINAL_SCENE_H264_AAC_VIDEO",
    ) -> Path:
        """Create a completed H3 job with finalized Final Scene artifact."""
        root = self.storage.project_directory(self.project_id)
        scene_renders = root / "renders" / scene_id
        final_dir = scene_renders / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        final_file = final_dir / "final_scene.mp4"
        final_file.write_bytes(content)
        size = len(content)
        sha256 = hashlib.sha256(content).hexdigest()
        relative_path = f"renders/{scene_id}/final/final_scene.mp4"

        job_store = JobStore(self.storage)
        job = new_job_record(
            self.project_id,
            scene_id,
            "keyframe_i2v",
            "a" * 64,
            output_node_id="20",
            expected_filename_prefix=f"test_{scene_id}",
        )
        job["state"] = SUCCEEDED
        job["raw_output"] = {
            "local_path": str(final_file),
            "relative_path": relative_path,
            "size": size,
            "sha256": sha256,
        }
        job["finalization"] = {
            "state": FINALIZATION_STATE_FINALIZED,
            "raw_current": True,
            "output": {
                "relative_path": relative_path,
                "identity": {"relative_path": relative_path, "size": size, "sha256": sha256},
                "format": "mp4",
            },
        }
        job_store.save(job)
        return final_file
