"""Phase 8C raw H3 output finalization.

This module consumes an already-successful Phase 8B raw output and produces a
project-owned, validated per-scene MP4.  It never submits a ComfyUI prompt and
does not mutate the project document or Phase 7 prompt provenance.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Callable, Mapping

from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    atomic_write_json,
    validate_entity_id,
    validate_project_id,
)
from .render import (
    H3_FPS,
    _artifact_path,
    _preparation_artifacts_current,
    _read_preparation_metadata,
    build_render_preflight,
)
from .render_jobs import (
    SUCCEEDED,
    JobStore,
    RenderOutputDiscoveryError,
    evaluate_raw_output_association,
    _default_comfy_output_root,
    _safe_relative_output,
    _scene_lock,
)


LOGGER = logging.getLogger(__name__)

FINALIZATION_SCHEMA_VERSION = 1
FINALIZATION_POLICY_VERSION = "phase8c-final-scene-mp4-v1"
FINALIZATION_DIRECTORY = "final"
FINALIZATION_STATE_NOT_AVAILABLE = "NOT_AVAILABLE"
FINALIZATION_STATE_RAW_READY = "RAW_READY"
FINALIZATION_STATE_FINALIZING = "FINALIZING"
FINALIZATION_STATE_FINALIZED = "FINALIZED"
FINALIZATION_STATE_STALE = "STALE"
FINALIZATION_STATE_FAILED = "FAILED"
FINALIZATION_STATES = frozenset({
    FINALIZATION_STATE_NOT_AVAILABLE,
    FINALIZATION_STATE_RAW_READY,
    FINALIZATION_STATE_FINALIZING,
    FINALIZATION_STATE_FINALIZED,
    FINALIZATION_STATE_STALE,
    FINALIZATION_STATE_FAILED,
})

FINAL_OUTPUT_SUFFIX = ".mp4"
FINAL_VIDEO_CODEC = "libx264"
FINAL_VIDEO_PRESET = "medium"
FINAL_VIDEO_CRF = 18
FINAL_VIDEO_PIXEL_FORMAT = "yuv420p"
FINAL_AUDIO_CODEC = "aac"
FINAL_AUDIO_BITRATE = "256k"
FINALIZATION_TIMEOUT_SECONDS = 300
VIDEO_COVERAGE_TOLERANCE_FRAMES = 1
AAC_FRAME_SAMPLES = 1024
AAC_TOLERANCE_FRAMES = 2
NON_RETRYABLE_RAW_FAILURE_CODES = frozenset({
    "RAW_VIDEO_TOO_SHORT",
    "RAW_VIDEO_NO_VIDEO",
    "RAW_VIDEO_INVALID",
    "RAW_VIDEO_FPS_UNSUPPORTED",
    "RAW_VIDEO_DURATION_UNKNOWN",
})
FINALIZATION_RETRYABLE_ERROR_CODES = frozenset({
    "FINALIZATION_OUTPUT_FAILED",
    "MEDIA_PROBE_FAILED",
    "MEDIA_PROBE_TIMEOUT",
    "MEDIA_TOOL_MISSING",
    "MEDIA_TOOL_UNAVAILABLE",
    "FINALIZATION_TIMEOUT",
    "FINALIZATION_ENCODE_FAILED",
    "FINAL_METADATA_WRITE_FAILED",
})


class RenderFinalizationError(Exception):
    """A stable product-facing Phase 8C failure."""

    status = 422

    def __init__(self, code: str, message: str, *, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class FinalizationInProgressError(RenderFinalizationError):
    status = 409


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _duration_ms(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not seconds.is_finite() or seconds < 0:
        return None
    result = int((seconds * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))
    return result if result >= 0 else None


def _fraction(value: object) -> Fraction | None:
    if not isinstance(value, str) or not value or value in {"0/0", "N/A"}:
        return None
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def _integer(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _stream_record(stream: Mapping[str, object]) -> dict[str, object]:
    rate = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
    frame_count = _integer(stream.get("nb_read_frames"))
    if frame_count is None:
        frame_count = _integer(stream.get("nb_frames"))
    record: dict[str, object] = {
        "index": _integer(stream.get("index")),
        "codec_type": stream.get("codec_type"),
        "codec_name": stream.get("codec_name"),
        "width": _integer(stream.get("width")),
        "height": _integer(stream.get("height")),
        "fps_numerator": rate.numerator if rate else None,
        "fps_denominator": rate.denominator if rate else None,
        "fps": float(rate) if rate else None,
        "time_base": stream.get("time_base"),
        "duration_ms": _duration_ms(stream.get("duration")),
        "frame_count": frame_count,
        "channels": _integer(stream.get("channels")),
        "sample_rate": _integer(stream.get("sample_rate")),
        "pix_fmt": stream.get("pix_fmt"),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
    }
    return record


class MediaProbeAdapter:
    """Bounded ffprobe JSON inspection for project-owned media paths."""

    def __init__(
        self,
        *,
        ffprobe_path: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        timeout_seconds: int = 60,
    ):
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> dict[str, object]:
        if not self.ffprobe_path:
            raise RenderFinalizationError("MEDIA_TOOL_MISSING", "FFprobe is not available for media validation.")
        if not path.is_file() or path.is_symlink():
            raise RenderFinalizationError("MEDIA_FILE_MISSING", "The media file is missing or unsafe.")
        arguments = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            "-count_frames",
            str(path),
        ]
        try:
            completed = self.runner(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RenderFinalizationError("MEDIA_PROBE_TIMEOUT", "Media inspection timed out.") from error
        except (FileNotFoundError, OSError) as error:
            raise RenderFinalizationError("MEDIA_TOOL_UNAVAILABLE", "FFprobe could not be started.") from error
        if getattr(completed, "returncode", 1) != 0:
            raise RenderFinalizationError("MEDIA_PROBE_FAILED", "The media file could not be inspected.")
        stdout = getattr(completed, "stdout", "")
        try:
            document = json.loads(stdout)
        except (TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise RenderFinalizationError("MEDIA_PROBE_INVALID", "FFprobe returned invalid media metadata.") from error
        if not isinstance(document, Mapping):
            raise RenderFinalizationError("MEDIA_PROBE_INVALID", "FFprobe returned invalid media metadata.")
        streams = document.get("streams")
        if not isinstance(streams, list):
            streams = []
        stream_records = [_stream_record(item) for item in streams if isinstance(item, Mapping)]
        format_record = document.get("format")
        if not isinstance(format_record, Mapping):
            format_record = {}
        video_streams = [item for item in stream_records if item.get("codec_type") == "video"]
        audio_streams = [item for item in stream_records if item.get("codec_type") == "audio"]
        return {
            "format": {
                "format_name": format_record.get("format_name"),
                "duration_ms": _duration_ms(format_record.get("duration")),
                "size": _integer(format_record.get("size")),
                "start_time": format_record.get("start_time"),
            },
            "streams": stream_records,
            "video_streams": video_streams,
            "audio_streams": audio_streams,
            "video": video_streams[0] if video_streams else None,
            "audio": audio_streams[0] if audio_streams else None,
        }


class FinalizationMediaAdapter:
    """Safe FFmpeg encode plus ffprobe validation for final scene media."""

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        timeout_seconds: int = FINALIZATION_TIMEOUT_SECONDS,
    ):
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        self.probe_adapter = MediaProbeAdapter(
            ffprobe_path=ffprobe_path,
            runner=runner,
            timeout_seconds=min(timeout_seconds, 120),
        )
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> dict[str, object]:
        return self.probe_adapter.probe(path)

    def encode(
        self,
        raw_video: Path,
        authoritative_audio: Path,
        candidate: Path,
        *,
        target_duration_ms: int,
    ) -> list[str]:
        if not self.ffmpeg_path:
            raise RenderFinalizationError("MEDIA_TOOL_MISSING", "FFmpeg is not available for final scene encoding.")
        target_seconds = f"{target_duration_ms / 1000:.6f}"
        arguments = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw_video),
            "-i",
            str(authoritative_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-t",
            target_seconds,
            "-c:v",
            FINAL_VIDEO_CODEC,
            "-preset",
            FINAL_VIDEO_PRESET,
            "-crf",
            str(FINAL_VIDEO_CRF),
            "-pix_fmt",
            FINAL_VIDEO_PIXEL_FORMAT,
            "-fps_mode",
            "cfr",
            "-r",
            str(H3_FPS),
            "-c:a",
            FINAL_AUDIO_CODEC,
            "-b:a",
            FINAL_AUDIO_BITRATE,
            "-movflags",
            "+faststart",
            "-map_metadata",
            "0",
            str(candidate),
        ]
        try:
            completed = self.runner(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RenderFinalizationError("FINALIZATION_TIMEOUT", "Final scene encoding timed out.") from error
        except (FileNotFoundError, OSError) as error:
            raise RenderFinalizationError("MEDIA_TOOL_UNAVAILABLE", "FFmpeg could not be started for final scene encoding.") from error
        if getattr(completed, "returncode", 1) != 0 or not candidate.is_file() or candidate.is_symlink():
            stderr = getattr(completed, "stderr", "")
            details = {"stderr": stderr.strip()[-1000:]} if isinstance(stderr, str) and stderr.strip() else {}
            raise RenderFinalizationError("FINALIZATION_ENCODE_FAILED", "Final scene encoding failed.", details=details)
        return arguments


def _file_identity(path: Path, *, relative_path: str | None = None) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise RenderFinalizationError("MEDIA_FILE_MISSING", "A required media artifact is missing or unsafe.")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    stat = path.stat()
    result: dict[str, object] = {
        "size": stat.st_size,
        "sha256": digest.hexdigest(),
    }
    if relative_path is not None:
        result["relative_path"] = relative_path
    return result


def _final_directory(root: Path, scene_id: str, *, create: bool = True) -> Path:
    render_directory = root / "renders" / scene_id
    final_directory = render_directory / FINALIZATION_DIRECTORY
    if not create:
        if render_directory.is_symlink() or final_directory.is_symlink():
            raise RenderFinalizationError("FINAL_PATH_UNSAFE", "The final scene output directory is a symlink.")
        return final_directory
    for directory in (render_directory, final_directory):
        if directory.exists() and directory.is_symlink():
            raise RenderFinalizationError("FINAL_PATH_UNSAFE", "The final scene output directory is a symlink.")
        directory.mkdir(parents=True, exist_ok=True)
    return final_directory


def _finalization_metadata_path(root: Path, scene_id: str, job_id: str, *, create: bool = True) -> Path:
    path = _final_directory(root, scene_id, create=create) / f"{job_id}.json"
    if path.is_symlink():
        raise RenderFinalizationError("FINAL_PATH_UNSAFE", "Finalization metadata path is a symlink.")
    return path


def _final_output_path(root: Path, scene_id: str, job_id: str) -> Path:
    path = _final_directory(root, scene_id) / f"{job_id}{FINAL_OUTPUT_SUFFIX}"
    if path.is_symlink():
        raise RenderFinalizationError("FINAL_PATH_UNSAFE", "Final scene output path is a symlink.")
    return path


def _read_finalization_metadata(root: Path, scene_id: str, job_id: str) -> dict[str, object] | None:
    try:
        path = _finalization_metadata_path(root, scene_id, job_id, create=False)
    except RenderFinalizationError:
        return None
    if path.is_symlink() or not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("finalization_schema_version") != FINALIZATION_SCHEMA_VERSION:
        return None
    if document.get("state") not in FINALIZATION_STATES:
        return None
    return document


def _write_finalization_metadata(root: Path, scene_id: str, job_id: str, document: Mapping[str, object]) -> dict[str, object]:
    path = _finalization_metadata_path(root, scene_id, job_id)
    normalized = deepcopy(dict(document))
    try:
        atomic_write_json(path, normalized)
    except ProjectPersistenceError:
        raise
    except OSError as error:
        raise RenderFinalizationError("FINAL_METADATA_WRITE_FAILED", "Finalization metadata could not be persisted.") from error
    return normalized


def _relative_project_path(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as error:
        raise RenderFinalizationError("FINAL_PATH_UNSAFE", "The final scene output is outside the project workspace.") from error


def _structured_finalization_error(
    job: Mapping[str, object],
    code: str,
    message: str,
    *,
    output_root: Path | None,
    details: Mapping[str, object] | None = None,
) -> RenderFinalizationError:
    error = RenderFinalizationError(code, message, details=dict(details or {}))
    error.details.update(finalization_error_details(job, error, output_root=output_root))
    return error


def _resolve_raw_path(job: Mapping[str, object], *, output_root: Path | None) -> tuple[Path, str]:
    association = evaluate_raw_output_association(job, output_root=output_root)
    if association.get("usable") is not True:
        failure = association.get("failure") if isinstance(association.get("failure"), Mapping) else {}
        association_state = association.get("state")
        failure_code = failure.get("code") if isinstance(failure.get("code"), str) else "RAW_OUTPUT_UNAVAILABLE"
        code = failure_code if association_state in {"UNSAFE", "FOREIGN", "AMBIGUOUS"} else "RAW_OUTPUT_UNAVAILABLE"
        message = failure.get("message") if isinstance(failure.get("message"), str) else "A usable Builder-owned raw H3 output is required before finalization."
        raise _structured_finalization_error(
            job,
            code,
            message,
            output_root=output_root,
            details={"output_association": association},
        )
    output = job.get("output")
    prompt_id = job.get("comfy_prompt_id")
    if not isinstance(output, Mapping) or output.get("raw_h3_output") is not True or not isinstance(prompt_id, str):
        raise _structured_finalization_error(
            job,
            "RAW_OUTPUT_UNAVAILABLE",
            "A successful raw H3 output is required before finalization.",
            output_root=output_root,
            details={"output_association": association},
        )
    if output.get("prompt_id") != prompt_id:
        raise _structured_finalization_error(
            job,
            "RAW_OUTPUT_PROMPT_MISMATCH",
            "The raw output is not associated with the owning render job.",
            output_root=output_root,
            details={"output_association": association},
        )
    production_output = job.get("production_output")
    expected_node_id = production_output.get("node_id") if isinstance(production_output, Mapping) else None
    if isinstance(expected_node_id, str) and output.get("output_node_id") != expected_node_id:
        raise _structured_finalization_error(
            job,
            "RAW_OUTPUT_NODE_MISMATCH",
            "The raw output is not associated with the prepared production output node.",
            output_root=output_root,
            details={"output_association": association},
        )
    filename = output.get("filename")
    subfolder = output.get("subfolder", "")
    recorded_relative = output.get("relative_path")
    if not isinstance(filename, str) or not isinstance(subfolder, str) or not isinstance(recorded_relative, str):
        raise _structured_finalization_error(
            job,
            "RAW_OUTPUT_RECORD_INVALID",
            "The durable raw output association is incomplete.",
            output_root=output_root,
            details={"output_association": association},
        )
    root = output_root or _default_comfy_output_root()
    try:
        resolved, relative = _safe_relative_output(root, subfolder, filename)
    except RenderOutputDiscoveryError as error:
        raise _structured_finalization_error(
            job,
            error.code,
            error.message,
            output_root=output_root,
            details={"output_association": association},
        ) from error
    if relative != recorded_relative.replace("\\", "/"):
        raise _structured_finalization_error(
            job,
            "RAW_OUTPUT_RECORD_INVALID",
            "The durable raw output association does not match ComfyUI history.",
            output_root=output_root,
            details={"output_association": association},
        )
    return resolved, relative


def finalization_error_details(
    job: Mapping[str, object] | None,
    error: RenderFinalizationError,
    *,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Build a stable route-facing finalization failure projection."""

    record = job if isinstance(job, Mapping) else {}
    association = error.details.get("output_association") if isinstance(error.details, Mapping) else None
    if not isinstance(association, Mapping):
        association = evaluate_raw_output_association(record, output_root=output_root)
    association_state = association.get("state") if isinstance(association.get("state"), str) else "UNKNOWN"
    retry_available = association_state == "AVAILABLE" and error.code in FINALIZATION_RETRYABLE_ERROR_CODES
    return {
        "job_id": record.get("job_id"),
        "scene_id": record.get("scene_id"),
        "association_state": association_state,
        "output_association": deepcopy(dict(association)),
        "failure_reason": {
            "code": error.code,
            "message": error.message,
            "details": deepcopy(dict(error.details)),
        },
        "retry_available": retry_available,
        "retry_action": "RETRY_FINALIZATION" if retry_available else ("RENDER_SCENE" if association_state != "AVAILABLE" else None),
    }


def _scene_preflight(preflight: Mapping[str, object], scene_id: str) -> dict[str, object]:
    scenes = preflight.get("scenes")
    if isinstance(scenes, list):
        for item in scenes:
            if isinstance(item, Mapping) and item.get("scene_id") == scene_id:
                return dict(item)
    if preflight.get("scene_id") == scene_id:
        return dict(preflight)
    raise RenderFinalizationError("FINALIZATION_SCENE_MISSING", "Current scene readiness could not be evaluated.")


def _current_inputs(
    storage: ProjectStorage,
    project: dict[str, object],
    job: Mapping[str, object],
    scene_preflight: Mapping[str, object],
    *,
    raw_output_root: Path | None,
) -> dict[str, object]:
    project_id = validate_project_id(project.get("project_id"))
    scene_id = validate_entity_id(job.get("scene_id"), "Scene ID")
    root = storage.project_directory(project_id).resolve(strict=True)
    job_fingerprint = job.get("preparation_fingerprint")
    current_fingerprint = scene_preflight.get("preparation_fingerprint")
    if scene_preflight.get("preparation_status") != "current" and scene_preflight.get("preparation_current") is not True:
        raise RenderFinalizationError("FINALIZATION_STALE", "Current render preparation is required before finalization.")
    if not isinstance(job_fingerprint, str) or job_fingerprint != current_fingerprint:
        raise RenderFinalizationError("FINALIZATION_STALE", "This raw render belongs to an older scene preparation and is historical only.")
    preparation = _read_preparation_metadata(root, scene_id)
    if not isinstance(preparation, Mapping):
        raise RenderFinalizationError("FINALIZATION_STALE", "Current render preparation metadata is unavailable.")
    source_audio = preparation.get("source_audio")
    if not isinstance(source_audio, Mapping):
        raise RenderFinalizationError("AUTHORITATIVE_AUDIO_MISSING", "Authoritative scene audio is missing.")
    audio_path = _artifact_path(root, source_audio.get("prepared_relative_path"))
    if audio_path is None:
        raise RenderFinalizationError("AUTHORITATIVE_AUDIO_MISSING", "Authoritative scene audio is missing.")
    if not _preparation_artifacts_current(root, preparation):
        raise RenderFinalizationError("FINALIZATION_STALE", "Current render preparation artifacts are unavailable.")
    if preparation.get("preparation_fingerprint") != job_fingerprint:
        raise RenderFinalizationError("FINALIZATION_STALE", "The raw render does not match the current preparation fingerprint.")
    raw_path, raw_relative = _resolve_raw_path(job, output_root=raw_output_root)
    scene = next((item for item in project.get("scenes", []) if isinstance(item, Mapping) and item.get("scene_id") == scene_id), None)
    if not isinstance(scene, Mapping):
        raise ProjectNotFoundError("The requested scene was not found.")
    target_duration_ms = scene.get("exact_duration_ms")
    if not isinstance(target_duration_ms, int) or isinstance(target_duration_ms, bool) or target_duration_ms <= 0:
        raise RenderFinalizationError("TARGET_DURATION_INVALID", "The authoritative scene duration is invalid.")
    return {
        "root": root,
        "scene_id": scene_id,
        "scene": dict(scene),
        "preparation": preparation,
        "audio_path": audio_path,
        "raw_path": raw_path,
        "raw_relative_path": raw_relative,
        "target_duration_ms": target_duration_ms,
        "preparation_fingerprint": job_fingerprint,
        "prompt_id": job.get("comfy_prompt_id"),
    }


def _video_duration_ms(video: Mapping[str, object]) -> int | None:
    duration = video.get("duration_ms")
    if isinstance(duration, int) and duration >= 0:
        return duration
    frame_count = video.get("frame_count")
    numerator = video.get("fps_numerator")
    denominator = video.get("fps_denominator")
    if all(isinstance(item, int) and item > 0 for item in (frame_count, numerator, denominator)):
        return int((Decimal(frame_count) * Decimal(1000) * Decimal(denominator) / Decimal(numerator)).to_integral_value(rounding=ROUND_HALF_UP))
    return None


def _video_tolerance_ms(fps: int = H3_FPS) -> int:
    return _ceil_div(1000 * VIDEO_COVERAGE_TOLERANCE_FRAMES, fps)


def _audio_tolerance_ms(sample_rate: int | None, *, final: bool) -> int:
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        return 50 if final else 2
    samples = AAC_FRAME_SAMPLES * AAC_TOLERANCE_FRAMES if final else 1
    return max(1, _ceil_div(1000 * samples, sample_rate))


def _timing_tolerances(sample_rate: int | None) -> dict[str, object]:
    video_ms = _video_tolerance_ms()
    audio_source_ms = _audio_tolerance_ms(sample_rate, final=False)
    audio_final_ms = _audio_tolerance_ms(sample_rate, final=True)
    return {
        "video_coverage_frames": VIDEO_COVERAGE_TOLERANCE_FRAMES,
        "video_end_tolerance_ms": video_ms,
        "audio_source_tolerance_ms": audio_source_ms,
        "audio_final_tolerance_samples": AAC_FRAME_SAMPLES * AAC_TOLERANCE_FRAMES,
        "audio_final_tolerance_ms": audio_final_ms,
        "container_duration_tolerance_ms": max(video_ms, audio_final_ms),
        "rationale": "One H3 video frame for video/container boundaries; one source sample for prepared PCM; two AAC frames for encoded MP4 priming/padding.",
    }


def _validate_raw_video(probe: Mapping[str, object], target_duration_ms: int) -> dict[str, object]:
    video = probe.get("video")
    if not isinstance(video, Mapping):
        raise RenderFinalizationError("RAW_VIDEO_NO_VIDEO", "Raw render does not contain a video stream.")
    width = video.get("width")
    height = video.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise RenderFinalizationError("RAW_VIDEO_INVALID", "Raw render video dimensions are invalid.")
    numerator = video.get("fps_numerator")
    denominator = video.get("fps_denominator")
    if numerator != H3_FPS or denominator != 1:
        raise RenderFinalizationError("RAW_VIDEO_FPS_UNSUPPORTED", "Raw H3 output does not use the authoritative 24 FPS video rate.")
    duration_ms = _video_duration_ms(video)
    if duration_ms is None:
        raise RenderFinalizationError("RAW_VIDEO_DURATION_UNKNOWN", "Raw H3 video duration could not be determined safely.")
    tolerance_ms = _video_tolerance_ms()
    if duration_ms + tolerance_ms < target_duration_ms:
        raise RenderFinalizationError(
            "RAW_VIDEO_TOO_SHORT",
            "Raw H3 output is shorter than the authoritative scene duration. Re-render this scene with a current timing plan.",
            details={"raw_video_duration_ms": duration_ms, "target_duration_ms": target_duration_ms, "tolerance_ms": tolerance_ms},
        )
    return {
        "duration_ms": duration_ms,
        "width": width,
        "height": height,
        "fps_numerator": numerator,
        "fps_denominator": denominator,
        "codec_name": video.get("codec_name"),
        "frame_count": video.get("frame_count"),
        "time_base": video.get("time_base"),
        "pix_fmt": video.get("pix_fmt"),
    }


def _validate_authoritative_audio(probe: Mapping[str, object], target_duration_ms: int) -> dict[str, object]:
    audio = probe.get("audio")
    if not isinstance(audio, Mapping):
        raise RenderFinalizationError("AUTHORITATIVE_AUDIO_MISSING", "Authoritative scene audio is missing.")
    sample_rate = audio.get("sample_rate")
    channels = audio.get("channels")
    duration_ms = audio.get("duration_ms")
    if not isinstance(sample_rate, int) or sample_rate <= 0 or not isinstance(channels, int) or channels <= 0 or not isinstance(duration_ms, int):
        raise RenderFinalizationError("AUTHORITATIVE_AUDIO_INVALID", "Authoritative scene audio could not be validated.")
    tolerance_ms = _audio_tolerance_ms(sample_rate, final=False)
    if abs(duration_ms - target_duration_ms) > tolerance_ms:
        raise RenderFinalizationError(
            "AUTHORITATIVE_AUDIO_DURATION_INVALID",
            "Authoritative scene audio does not match the exact scene duration.",
            details={"audio_duration_ms": duration_ms, "target_duration_ms": target_duration_ms, "tolerance_ms": tolerance_ms},
        )
    return {
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": channels,
        "codec_name": audio.get("codec_name"),
    }


def _validate_final_media(
    probe: Mapping[str, object],
    *,
    target_duration_ms: int,
    expected_width: int,
    expected_height: int,
    sample_rate: int,
    channels: int,
) -> dict[str, object]:
    format_name = probe.get("format", {}).get("format_name") if isinstance(probe.get("format"), Mapping) else None
    if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
        raise RenderFinalizationError("FINAL_MEDIA_CONTAINER_INVALID", "Final scene output is not an MP4 container.")
    videos = probe.get("video_streams")
    audios = probe.get("audio_streams")
    if not isinstance(videos, list) or len(videos) != 1:
        raise RenderFinalizationError("FINAL_MEDIA_VIDEO_INVALID", "Final scene output must contain exactly one video stream.")
    if not isinstance(audios, list) or len(audios) != 1:
        raise RenderFinalizationError("FINAL_MEDIA_AUDIO_INVALID", "Final scene output must contain exactly one authoritative audio stream.")
    video = videos[0]
    audio = audios[0]
    if video.get("codec_name") != "h264":
        raise RenderFinalizationError("FINAL_MEDIA_VIDEO_CODEC_INVALID", "Final scene output is not encoded with the approved H.264 policy.")
    if audio.get("codec_name") != FINAL_AUDIO_CODEC:
        raise RenderFinalizationError("FINAL_MEDIA_AUDIO_CODEC_INVALID", "Final scene output is not encoded with the approved AAC policy.")
    if video.get("width") != expected_width or video.get("height") != expected_height:
        raise RenderFinalizationError("FINAL_MEDIA_DIMENSIONS_INVALID", "Final scene output changed the prepared video resolution.")
    if video.get("fps_numerator") != H3_FPS or video.get("fps_denominator") != 1:
        raise RenderFinalizationError("FINAL_MEDIA_FPS_INVALID", "Final scene output changed the authoritative 24 FPS rate.")
    if not isinstance(audio.get("channels"), int) or audio.get("channels") <= 0 or audio.get("channels") != channels or not isinstance(audio.get("sample_rate"), int) or audio.get("sample_rate") <= 0 or audio.get("sample_rate") != sample_rate:
        raise RenderFinalizationError("FINAL_MEDIA_AUDIO_INVALID", "Final scene output audio metadata is invalid.")
    video_duration_ms = _video_duration_ms(video)
    audio_duration_ms = audio.get("duration_ms")
    container = probe.get("format")
    container_duration_ms = container.get("duration_ms") if isinstance(container, Mapping) else None
    video_tolerance_ms = _video_tolerance_ms()
    audio_tolerance_ms = _audio_tolerance_ms(sample_rate, final=True)
    if video_duration_ms is None or video_duration_ms + video_tolerance_ms < target_duration_ms or video_duration_ms - video_tolerance_ms > target_duration_ms:
        raise RenderFinalizationError("FINAL_MEDIA_DURATION_INVALID", "Final scene video duration is outside the technical frame-boundary tolerance.")
    if not isinstance(audio_duration_ms, int) or abs(audio_duration_ms - target_duration_ms) > audio_tolerance_ms:
        raise RenderFinalizationError("FINAL_MEDIA_AUDIO_DURATION_INVALID", "Final scene audio duration is outside the technical AAC tolerance.")
    container_tolerance_ms = max(video_tolerance_ms, audio_tolerance_ms)
    if not isinstance(container_duration_ms, int) or abs(container_duration_ms - target_duration_ms) > container_tolerance_ms:
        raise RenderFinalizationError("FINAL_MEDIA_CONTAINER_DURATION_INVALID", "Final scene container duration is outside the technical tolerance.")
    return {
        "container": format_name,
        "video_duration_ms": video_duration_ms,
        "audio_duration_ms": audio_duration_ms,
        "container_duration_ms": container_duration_ms,
        "width": video.get("width"),
        "height": video.get("height"),
        "fps_numerator": video.get("fps_numerator"),
        "fps_denominator": video.get("fps_denominator"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_channels": audio.get("channels"),
        "audio_sample_rate": audio.get("sample_rate"),
        "audio_stream_count": len(audios),
        "video_stream_count": len(videos),
    }


def _policy() -> dict[str, object]:
    return {
        "version": FINALIZATION_POLICY_VERSION,
        "video_codec": FINAL_VIDEO_CODEC,
        "video_preset": FINAL_VIDEO_PRESET,
        "video_crf": FINAL_VIDEO_CRF,
        "video_pixel_format": FINAL_VIDEO_PIXEL_FORMAT,
        "video_fps": H3_FPS,
        "audio_codec": FINAL_AUDIO_CODEC,
        "audio_bitrate": FINAL_AUDIO_BITRATE,
        "audio_processing": "none",
        "scaling": "none",
        "interpolation": "none",
        "output_container": "mp4",
    }


def _basis(
    job: Mapping[str, object],
    current: Mapping[str, object],
    raw_identity: Mapping[str, object],
    audio_identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        "policy_version": FINALIZATION_POLICY_VERSION,
        "job_id": job.get("job_id"),
        "prompt_id": job.get("comfy_prompt_id"),
        "raw_output_identity": dict(raw_identity),
        "preparation_fingerprint": current["preparation_fingerprint"],
        "target_duration_ms": current["target_duration_ms"],
        "authoritative_audio_identity": dict(audio_identity),
        "encode_policy": _policy(),
    }


def _hash_document(document: Mapping[str, object]) -> str:
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _output_identity(root: Path, output: Mapping[str, object] | None) -> tuple[Path, dict[str, object]] | None:
    if not isinstance(output, Mapping) or not isinstance(output.get("relative_path"), str):
        return None
    path = _artifact_path(root, output["relative_path"])
    if path is None:
        return None
    try:
        return path, _file_identity(path, relative_path=output["relative_path"])
    except RenderFinalizationError:
        return None


def _identity_matches(expected: object, actual: Mapping[str, object]) -> bool:
    if not isinstance(expected, Mapping):
        return False
    return expected.get("size") == actual.get("size") and expected.get("sha256") == actual.get("sha256")


def _base_document(
    job: Mapping[str, object],
    current: Mapping[str, object],
    *,
    fingerprint: str | None,
    raw_identity: Mapping[str, object] | None,
    audio_identity: Mapping[str, object] | None,
    raw_probe: Mapping[str, object] | None = None,
    audio_probe: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "finalization_schema_version": FINALIZATION_SCHEMA_VERSION,
        "project_id": job["project_id"],
        "scene_id": job["scene_id"],
        "job_id": job["job_id"],
        "comfy_prompt_id": job.get("comfy_prompt_id"),
        "state": FINALIZATION_STATE_RAW_READY,
        "created_at": _timestamp(),
        "updated_at": _timestamp(),
        "finalization_fingerprint": fingerprint,
        "preparation_fingerprint": current.get("preparation_fingerprint"),
        "target_duration_ms": current.get("target_duration_ms"),
        "raw_output": {
            "relative_path": current.get("raw_relative_path"),
            "identity": dict(raw_identity or {}),
            "probe": deepcopy(dict(raw_probe or {})),
            "raw_h3_output": True,
        },
        "authoritative_audio": {
            "relative_path": _relative_project_path(current["audio_path"], current["root"]),
            "identity": dict(audio_identity or {}),
            "probe": deepcopy(dict(audio_probe or {})),
            "source": "phase8a_prepared_scene_audio",
        },
        "timing_tolerances": _timing_tolerances(
            (audio_probe or {}).get("audio", {}).get("sample_rate") if isinstance((audio_probe or {}).get("audio"), Mapping) else None
        ),
        "policy": _policy(),
        "output": None,
        "previous_valid_output": None,
        "failure": None,
        "reused": False,
    }


def _failure_document(
    base: Mapping[str, object],
    error: RenderFinalizationError,
    *,
    previous_output: Mapping[str, object] | None,
) -> dict[str, object]:
    document = deepcopy(dict(base))
    document["state"] = FINALIZATION_STATE_FAILED
    document["updated_at"] = _timestamp()
    document["failure"] = {"code": error.code, "message": error.message, **error.details}
    document["output"] = deepcopy(dict(previous_output)) if isinstance(previous_output, Mapping) else None
    document["previous_valid_output"] = deepcopy(dict(previous_output)) if isinstance(previous_output, Mapping) else None
    return document


def _current_finalization_state(
    root: Path,
    job: Mapping[str, object],
    current: Mapping[str, object] | None,
    *,
    raw_output_root: Path | None,
) -> dict[str, object]:
    existing = _read_finalization_metadata(root, str(job["scene_id"]), str(job["job_id"]))
    response = deepcopy(existing) if existing else {
        "finalization_schema_version": FINALIZATION_SCHEMA_VERSION,
        "project_id": job.get("project_id"),
        "scene_id": job.get("scene_id"),
        "job_id": job.get("job_id"),
        "state": FINALIZATION_STATE_NOT_AVAILABLE,
        "output": None,
        "failure": None,
    }
    association = evaluate_raw_output_association(job, output_root=raw_output_root)
    response["output_association"] = deepcopy(dict(association))
    response["association_state"] = association.get("state", "UNKNOWN")
    response["retry_available"] = False
    response["finalization_allowed"] = False
    response["raw_current"] = False
    response["eligible"] = False
    if job.get("state") != SUCCEEDED or not isinstance(current, Mapping):
        response["state"] = FINALIZATION_STATE_NOT_AVAILABLE
        return response
    if current.get("preparation_status") != "current" and current.get("preparation_current") is not True:
        response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
        return response
    try:
        resolved_raw, raw_relative = _resolve_raw_path(job, output_root=raw_output_root)
        raw_identity = _file_identity(resolved_raw, relative_path=raw_relative)
        preparation = _read_preparation_metadata(root, str(job["scene_id"]))
        if not isinstance(preparation, Mapping) or not _preparation_artifacts_current(root, preparation):
            response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
            return response
        if preparation.get("preparation_fingerprint") != current.get("preparation_fingerprint"):
            response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
            return response
        source_audio = preparation.get("source_audio")
        audio_relative = source_audio.get("prepared_relative_path") if isinstance(source_audio, Mapping) else None
        audio_path = _artifact_path(root, audio_relative)
        if audio_path is None:
            response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
            return response
        audio_identity = _file_identity(audio_path, relative_path=audio_relative)
        if current.get("preparation_fingerprint") != job.get("preparation_fingerprint"):
            response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
            return response
        response["raw_current"] = True
        response["eligible"] = True
        response["finalization_allowed"] = True
        timing = preparation.get("timing") if isinstance(preparation, Mapping) else None
        target_duration_ms = current.get("target_duration_ms", current.get("duration_ms"))
        if not isinstance(target_duration_ms, int) and isinstance(timing, Mapping):
            target_duration_ms = timing.get("target_duration_ms")
        current_for_basis = {
            **current,
            "raw_relative_path": raw_relative,
            "target_duration_ms": target_duration_ms,
        }
        basis = _basis(job, current_for_basis, raw_identity, audio_identity)
        current_fingerprint = _hash_document(basis)
        if existing and existing.get("finalization_fingerprint") != current_fingerprint:
            response["state"] = FINALIZATION_STATE_STALE
        elif existing and existing.get("state") in {FINALIZATION_STATE_FINALIZED, FINALIZATION_STATE_FINALIZING, FINALIZATION_STATE_FAILED}:
            response["state"] = existing["state"]
        else:
            response["state"] = FINALIZATION_STATE_RAW_READY
        response["current_fingerprint"] = current_fingerprint
        if response["state"] == FINALIZATION_STATE_FINALIZED:
            output_identity = _output_identity(root, existing.get("output") if existing else None)
            expected_identity = existing.get("output", {}).get("identity") if isinstance(existing, Mapping) and isinstance(existing.get("output"), Mapping) else None
            if output_identity is None or not _identity_matches(expected_identity, output_identity[1]):
                response["state"] = FINALIZATION_STATE_STALE
        failure = response.get("failure")
        if response["state"] == FINALIZATION_STATE_FAILED and isinstance(failure, Mapping) and failure.get("code") in NON_RETRYABLE_RAW_FAILURE_CODES:
            response["eligible"] = False
            response["finalization_allowed"] = False
    except RenderFinalizationError as error:
        association = evaluate_raw_output_association(job, output_root=raw_output_root)
        response["output_association"] = deepcopy(dict(association))
        response["association_state"] = association.get("state", "UNKNOWN")
        response["failure"] = {
            "code": error.code,
            "message": error.message,
            **deepcopy(dict(error.details)),
        }
        response["retry_available"] = finalization_error_details(job, error, output_root=raw_output_root)["retry_available"]
        response["finalization_allowed"] = False
        response["state"] = FINALIZATION_STATE_STALE if existing else FINALIZATION_STATE_NOT_AVAILABLE
    return response


def summarize_render_job_finalization(
    storage: ProjectStorage,
    job: Mapping[str, object],
    *,
    preflight: Mapping[str, object] | None = None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    """Return current raw/final state without changing project or prompt state."""

    project_id = validate_project_id(job.get("project_id"))
    scene_id = validate_entity_id(job.get("scene_id"), "Scene ID")
    root = storage.project_directory(project_id).resolve(strict=True)
    current: Mapping[str, object] | None = None
    if preflight is None:
        try:
            preflight = build_render_preflight(storage.load_project(project_id), storage)
        except Exception:
            preflight = None
    if isinstance(preflight, Mapping):
        try:
            current = _scene_preflight(preflight, scene_id)
        except RenderFinalizationError:
            current = None
    return _current_finalization_state(root, job, current, raw_output_root=raw_output_root)


def enrich_render_jobs_with_finalization(
    storage: ProjectStorage,
    project_id: object,
    jobs: list[Mapping[str, object]],
    *,
    preflight: Mapping[str, object] | None = None,
    raw_output_root: Path | None = None,
) -> list[dict[str, object]]:
    canonical_project_id = validate_project_id(project_id)
    project = storage.load_project(canonical_project_id)
    if preflight is None:
        try:
            preflight = build_render_preflight(project, storage)
        except Exception:
            preflight = None
    result: list[dict[str, object]] = []
    for job in jobs:
        item = deepcopy(dict(job))
        item["finalization"] = summarize_render_job_finalization(
            storage,
            item,
            preflight=preflight,
            raw_output_root=raw_output_root,
        )
        result.append(item)
    return result


def finalize_render_job(
    storage: ProjectStorage,
    project_id: object,
    job_id: object,
    *,
    preflight: Mapping[str, object] | None = None,
    media_adapter: FinalizationMediaAdapter | None = None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    """Finalize one current successful raw job into a validated scene MP4."""

    canonical_project_id = validate_project_id(project_id)
    canonical_job_id = str(job_id)
    store = JobStore(storage)
    job = store.find(canonical_project_id, canonical_job_id)
    scene_id = validate_entity_id(job.get("scene_id"), "Scene ID")
    project = storage.load_project(canonical_project_id)
    with _scene_lock(canonical_project_id, scene_id):
        job = store.find(canonical_project_id, canonical_job_id)
        if job.get("state") != SUCCEEDED:
            raise RenderFinalizationError("FINALIZATION_NOT_ELIGIBLE", "A successful raw H3 render is required before finalization.")
        root = storage.project_directory(canonical_project_id).resolve(strict=True)
        existing = _read_finalization_metadata(root, scene_id, canonical_job_id)
        if existing and existing.get("state") == FINALIZATION_STATE_FINALIZING:
            existing = deepcopy(existing)
            existing["state"] = FINALIZATION_STATE_FAILED
            existing["updated_at"] = _timestamp()
            existing["failure"] = {
                "code": "FINALIZATION_INTERRUPTED",
                "message": "Finalization was interrupted before completion; retry is available.",
            }
        if preflight is None:
            preflight = build_render_preflight(project, storage)
        current = _scene_preflight(preflight, scene_id)
        current_inputs = _current_inputs(storage, project, job, current, raw_output_root=raw_output_root)
        adapter = media_adapter or FinalizationMediaAdapter()
        raw_identity = _file_identity(current_inputs["raw_path"], relative_path=current_inputs["raw_relative_path"])
        audio_identity = _file_identity(
            current_inputs["audio_path"],
            relative_path=_relative_project_path(current_inputs["audio_path"], root),
        )
        final_path = _final_output_path(root, scene_id, canonical_job_id)
        previous_valid_output = None
        if existing:
            candidate_previous = _output_identity(root, existing.get("output"))
            if candidate_previous is not None and existing.get("state") in {FINALIZATION_STATE_FINALIZED, FINALIZATION_STATE_FAILED, FINALIZATION_STATE_FINALIZING}:
                previous_valid_output = deepcopy(dict(existing["output"]))
        raw_probe: dict[str, object] = {}
        audio_probe: dict[str, object] = {}
        try:
            raw_probe = adapter.probe(current_inputs["raw_path"])
            raw_video = _validate_raw_video(raw_probe, current_inputs["target_duration_ms"])
            audio_probe = adapter.probe(current_inputs["audio_path"])
            authoritative_audio = _validate_authoritative_audio(audio_probe, current_inputs["target_duration_ms"])
        except RenderFinalizationError as error:
            failed_base = _base_document(
                job,
                current_inputs,
                fingerprint=_hash_document(_basis(job, current_inputs, raw_identity, audio_identity)),
                raw_identity=raw_identity,
                audio_identity=audio_identity,
                raw_probe=raw_probe,
                audio_probe=audio_probe,
            )
            try:
                _write_finalization_metadata(
                    root,
                    scene_id,
                    canonical_job_id,
                    _failure_document(failed_base, error, previous_output=previous_valid_output),
                )
            except RenderFinalizationError:
                LOGGER.exception("Could not persist early finalization failure metadata.")
            raise
        current_inputs["raw_video"] = raw_video
        current_inputs["authoritative_audio"] = authoritative_audio
        basis = _basis(job, current_inputs, raw_identity, audio_identity)
        fingerprint = _hash_document(basis)
        base = _base_document(
            job,
            current_inputs,
            fingerprint=fingerprint,
            raw_identity=raw_identity,
            audio_identity=audio_identity,
            raw_probe=raw_probe,
            audio_probe=audio_probe,
        )
        base["raw_video"] = raw_video
        base["authoritative_audio"].update(authoritative_audio)
        if existing and existing.get("finalization_fingerprint") == fingerprint and existing.get("state") == FINALIZATION_STATE_FINALIZED:
            output_identity = _output_identity(root, existing.get("output"))
            if output_identity is not None:
                try:
                    final_probe = adapter.probe(output_identity[0])
                    final_validation = _validate_final_media(
                        final_probe,
                        target_duration_ms=current_inputs["target_duration_ms"],
                        expected_width=raw_video["width"],
                        expected_height=raw_video["height"],
                        sample_rate=authoritative_audio["sample_rate"],
                        channels=authoritative_audio["channels"],
                    )
                except RenderFinalizationError:
                    pass
                else:
                    reused = deepcopy(existing)
                    reused["reused"] = True
                    reused["current"] = True
                    reused["validation"] = final_validation
                    return reused
        finalizing = deepcopy(base)
        finalizing["state"] = FINALIZATION_STATE_FINALIZING
        finalizing["updated_at"] = _timestamp()
        finalizing["output"] = previous_valid_output
        finalizing["previous_valid_output"] = previous_valid_output
        _write_finalization_metadata(root, scene_id, canonical_job_id, finalizing)
        candidate = final_path.with_name(f".{canonical_job_id}.{uuid.uuid4().hex}.candidate.mp4")
        try:
            command = adapter.encode(
                current_inputs["raw_path"],
                current_inputs["audio_path"],
                candidate,
                target_duration_ms=current_inputs["target_duration_ms"],
            )
            final_probe = adapter.probe(candidate)
            final_validation = _validate_final_media(
                final_probe,
                target_duration_ms=current_inputs["target_duration_ms"],
                expected_width=raw_video["width"],
                expected_height=raw_video["height"],
                sample_rate=authoritative_audio["sample_rate"],
                channels=authoritative_audio["channels"],
            )
            with candidate.open("r+b") as handle:
                if hasattr(os, "fsync"):
                    os.fsync(handle.fileno())
            os.replace(candidate, final_path)
            final_identity = _file_identity(final_path, relative_path=_relative_project_path(final_path, root))
            completed = deepcopy(base)
            completed["state"] = FINALIZATION_STATE_FINALIZED
            completed["updated_at"] = _timestamp()
            completed["output"] = {
                "relative_path": _relative_project_path(final_path, root),
                "identity": final_identity,
                "format": "mp4",
                "raw_h3_output": False,
            }
            completed["previous_valid_output"] = previous_valid_output
            completed["validation"] = final_validation
            completed["ffmpeg_command"] = command
            completed["current"] = True
            completed["reused"] = False
            completed["failure"] = None
            return _write_finalization_metadata(root, scene_id, canonical_job_id, completed)
        except RenderFinalizationError as error:
            failed = _failure_document(base, error, previous_output=previous_valid_output)
            try:
                _write_finalization_metadata(root, scene_id, canonical_job_id, failed)
            except RenderFinalizationError:
                LOGGER.exception("Could not persist finalization failure metadata.")
            raise
        except (OSError, ValueError, TypeError) as error:
            failure = RenderFinalizationError("FINALIZATION_OUTPUT_FAILED", "Final scene output could not be installed safely.")
            failed = _failure_document(base, failure, previous_output=previous_valid_output)
            try:
                _write_finalization_metadata(root, scene_id, canonical_job_id, failed)
            except RenderFinalizationError:
                LOGGER.exception("Could not persist finalization failure metadata.")
            raise failure from error
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Could not clean finalization candidate file.")


__all__ = [
    "FINALIZATION_POLICY_VERSION",
    "FINALIZATION_RETRYABLE_ERROR_CODES",
    "FINALIZATION_SCHEMA_VERSION",
    "FINALIZATION_STATE_FAILED",
    "FINALIZATION_STATE_FINALIZED",
    "FINALIZATION_STATE_FINALIZING",
    "FINALIZATION_STATE_NOT_AVAILABLE",
    "FINALIZATION_STATE_RAW_READY",
    "FINALIZATION_STATE_STALE",
    "FINALIZATION_STATES",
    "FinalizationInProgressError",
    "FinalizationMediaAdapter",
    "MediaProbeAdapter",
    "RenderFinalizationError",
    "enrich_render_jobs_with_finalization",
    "finalization_error_details",
    "finalize_render_job",
    "summarize_render_job_finalization",
]
