"""Phase 8A render preflight and dry preparation.

This module deliberately stops at a durable, validated preparation package.  It
does not know how to submit a ComfyUI prompt and it does not import the queue
or execution APIs.  The project document remains the source of truth for all
scene, prompt, Visuals, and source-audio inputs.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
from typing import Callable, Mapping

from .entities import get_reference_path_for_project
from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    atomic_write_json,
    validate_entity_id,
    validate_project_id,
)
from .prompt_service import (
    PROMPT_STATUS_CURRENT,
    PROMPT_STATUS_NEEDS_GPT,
    PROMPT_STATUS_STALE,
    PromptCompilationError,
    build_ref2va_mapping,
    prompt_scene_state,
)
from .requirements import (
    RequirementsSnapshot,
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_UNKNOWN,
    get_requirements_snapshot,
)
from .source import AudioProbeError, MediaProbeUnavailableError, probe_audio_duration_ms
from .visuals import (
    GENERATION_METHODS,
    MAX_REF2VA_STILL_REFERENCES,
    derive_visual_readiness,
    get_keyframe_path_for_project,
)
from .workflows import (
    WORKFLOW_DIRECTORY,
    _load_workflow,
    _validate_loaded_workflow,
    patch_reference_picture_slots,
    production_manifest_registry,
    validate_production_workflow_contract,
)


LOGGER = logging.getLogger(__name__)

RENDER_RESPONSE_VERSION = 1
PREPARATION_VERSION = 1
H3_FPS = 24
H3_MINIMUM_FRAMES = 5
H3_FRAME_STEP = 17
H3_MAX_FRAMES = 3600
H3_TIMING_CONTRACT_ID = "minimax_h3_24fps_5_plus_17n_v1"
TARGET_HARDWARE_STATUS = "DEFERRED_TARGET_NVIDIA"
TARGET_HARDWARE_MESSAGE = (
    "RTX 4080 SUPER H3 qualification is deferred; Phase 8A performs structural preparation only."
)
PREPARATION_FILENAME = "preparation.json"
WORKFLOW_FILENAME = "workflow.json"
SCENE_AUDIO_FILENAME = "scene_audio.wav"
RENDER_INPUT_DIRECTORY = "inputs"


_FILE_IDENTITY_CACHE_LOCK = threading.Lock()
_FILE_IDENTITY_CACHE: dict[str, tuple[int, int, int, str]] = {}


def _diagnostic(code: str, message: str, layer: str) -> dict[str, str]:
    return {"code": code, "message": message, "layer": layer}


class RenderError(Exception):
    """Base class for expected render-boundary failures."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RenderPreparationError(RenderError):
    """A stable, product-facing preparation failure."""

    def __init__(self, code: str, message: str, *, preflight: dict[str, object] | None = None):
        super().__init__(code, message)
        self.preflight = preflight


class RenderPreparationBlocked(RenderPreparationError):
    """The current scene has content or runtime blockers."""

    def __init__(self, preflight: dict[str, object]):
        super().__init__(
            "PREPARATION_BLOCKED",
            "Render inputs are not ready for this scene.",
            preflight=preflight,
        )


class TimingContractError(RenderPreparationError):
    """The installed H3 timing contract cannot safely plan this scene."""


class MediaPreparationError(RenderPreparationError):
    """Audio or visual media preparation failed at a known layer."""


class WorkflowCompilationError(RenderPreparationError):
    """The immutable production workflow could not be compiled safely."""


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cached_file_identity(path: Path) -> tuple[int, str]:
    """Hash a file once per unchanged stat signature within this process."""

    stat = path.stat()
    cache_key = str(path)
    signature = (stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0))
    with _FILE_IDENTITY_CACHE_LOCK:
        cached = _FILE_IDENTITY_CACHE.get(cache_key)
        if cached is not None and cached[:3] == signature:
            return cached[0], cached[3]

    digest = _file_sha256(path)
    with _FILE_IDENTITY_CACHE_LOCK:
        _FILE_IDENTITY_CACHE[cache_key] = (*signature, digest)
    return stat.st_size, digest


def _json_hash(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def find_ffmpeg() -> str | None:
    """Find the already-installed media executable used by the application."""

    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def _root_path(storage: ProjectStorage, project_id: object) -> tuple[str, Path]:
    canonical_project_id = validate_project_id(project_id)
    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    return canonical_project_id, root


def _owned_path(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    """Resolve a project-owned path while rejecting symlinks and traversal."""

    root_resolved = root.resolve(strict=True)
    lexical_path = path.absolute()
    try:
        relative_parts = lexical_path.relative_to(root_resolved).parts
    except ValueError as error:
        raise RenderPreparationError("PATH_UNSAFE", "A render path is not inside the project workspace.") from error
    current = root_resolved
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise RenderPreparationError("PATH_UNSAFE", "A project-owned render path is a symlink.")
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as error:
        raise RenderPreparationError("PATH_UNSAFE", "A project-owned render path could not be resolved.") from error
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise RenderPreparationError("PATH_UNSAFE", "A render path resolved outside the project workspace.")
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise RenderPreparationError("PATH_UNSAFE", "A required project-owned file is unavailable.")
    return resolved


def _relative_path(path: Path, root: Path) -> str:
    resolved = _owned_path(path, root)
    return resolved.relative_to(root.resolve(strict=True)).as_posix()


def _find_scene(project: dict[str, object], scene_id: object) -> dict[str, object]:
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    for scene in project.get("scenes", []):
        if isinstance(scene, dict) and scene.get("scene_id") == canonical_scene_id:
            return scene
    raise ProjectNotFoundError("The requested scene was not found.")


def _find_visual_scene(project: dict[str, object], scene_id: str) -> dict[str, object]:
    for visual_scene in project.get("visuals", {}).get("scenes", []):
        if isinstance(visual_scene, dict) and visual_scene.get("scene_id") == scene_id:
            return visual_scene
    raise ProjectNotFoundError("The requested Visuals scene was not found.")


def build_h3_timing_plan(
    exact_duration_ms: object,
    *,
    fps: int = H3_FPS,
    minimum_frames: int = H3_MINIMUM_FRAMES,
    frame_step: int = H3_FRAME_STEP,
    max_frames: int = H3_MAX_FRAMES,
) -> dict[str, object]:
    """Calculate the one accepted H3 frame plan without changing audio timing."""

    if fps != H3_FPS or minimum_frames != H3_MINIMUM_FRAMES or frame_step != H3_FRAME_STEP:
        raise TimingContractError(
            "TIMING_CONTRACT_UNSUPPORTED",
            "The installed H3 timing contract is not the approved 24 FPS 5 + 17n grid.",
        )
    if not isinstance(exact_duration_ms, int) or isinstance(exact_duration_ms, bool) or exact_duration_ms <= 0:
        raise TimingContractError("TIMING_DURATION_INVALID", "The exact scene duration must be a positive integer in milliseconds.")
    if max_frames < minimum_frames:
        raise TimingContractError("TIMING_CONTRACT_UNSUPPORTED", "The installed H3 frame limit is invalid.")

    requested_frame_count = exact_duration_ms * fps / 1000
    block_count = max(0, round((requested_frame_count - minimum_frames) / frame_step))
    generated_frame_count = block_count * frame_step + minimum_frames
    if generated_frame_count > max_frames:
        raise TimingContractError("TIMING_FRAME_LIMIT", "The exact scene duration exceeds the installed H3 frame limit.")

    generated_duration_ms = generated_frame_count * 1000 / fps
    return {
        "contract_id": H3_TIMING_CONTRACT_ID,
        "frame_formula": "5 + 17n",
        "minimum_frames": minimum_frames,
        "frame_step": frame_step,
        "max_frames": max_frames,
        "target_duration_ms": exact_duration_ms,
        "authoritative_audio_duration_ms": exact_duration_ms,
        "h3_fps": fps,
        "requested_frame_count": requested_frame_count,
        "generated_frame_count": generated_frame_count,
        "generated_duration_ms": round(generated_duration_ms, 6),
        "trim_required": generated_frame_count * 1000 != exact_duration_ms * fps,
    }


def _source_audio_state(
    project: dict[str, object],
    root: Path,
    scene: dict[str, object],
) -> tuple[bool, Path | None, list[dict[str, str]]]:
    source = project.get("source")
    master_audio = source.get("master_audio") if isinstance(source, dict) else None
    blockers: list[dict[str, str]] = []
    if not isinstance(master_audio, dict):
        return False, None, [_diagnostic("SOURCE_AUDIO_MISSING", "Master audio is not imported.", "source_audio")]

    stored_name = master_audio.get("stored_name")
    if not isinstance(stored_name, str) or not stored_name or Path(stored_name).name != stored_name:
        return False, None, [_diagnostic("SOURCE_AUDIO_PATH_UNSAFE", "Master audio metadata does not name a project-local file.", "source_audio")]

    path = root / "source" / stored_name
    try:
        owned = _owned_path(path, root, must_exist=True)
    except RenderPreparationError as error:
        code = "SOURCE_AUDIO_FILE_MISSING" if not path.exists() else error.code
        message = "Master audio file cannot be resolved." if code == "SOURCE_AUDIO_FILE_MISSING" else error.message
        return False, None, [_diagnostic(code, message, "source_audio")]

    duration_ms = master_audio.get("duration_ms")
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms <= 0:
        blockers.append(_diagnostic("SOURCE_AUDIO_METADATA_INVALID", "Master audio duration metadata is invalid.", "source_audio"))
    start_ms = scene.get("timeline_start_ms")
    end_ms = scene.get("timeline_end_ms")
    exact_duration_ms = scene.get("exact_duration_ms")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start_ms, end_ms, exact_duration_ms)):
        blockers.append(_diagnostic("SOURCE_AUDIO_BOUNDS_INVALID", "Scene audio boundaries are not exact integer milliseconds.", "source_audio"))
    elif start_ms < 0 or end_ms <= start_ms or end_ms - start_ms != exact_duration_ms or end_ms > duration_ms:
        blockers.append(_diagnostic("SOURCE_AUDIO_BOUNDS_INVALID", "Scene audio boundaries fall outside the authoritative master audio.", "source_audio"))
    return not blockers, owned, blockers


def _visual_diagnostics(
    project: dict[str, object],
    scene_id: str,
    method: str,
    storage: ProjectStorage,
) -> tuple[bool, dict[str, object], list[dict[str, str]]]:
    try:
        readiness = derive_visual_readiness(project, scene_id, storage)
    except (ProjectNotFoundError, ProjectValidationError, KeyError, StopIteration):
        readiness = {
            "scene_id": scene_id,
            "generation_method": method,
            "ready": False,
            "state": "blocked",
            "missing": [],
        }
        return False, readiness, [
            _diagnostic("VISUALS_CONTRACT_INVALID", "Visuals state could not be resolved for this scene.", "visuals")
        ]

    blockers: list[dict[str, str]] = []
    if readiness.get("ready"):
        return True, readiness, blockers

    visual_scene = _find_visual_scene(project, scene_id)
    selected_count = len(visual_scene.get("reference2video", {}).get("selected_references", []))
    for missing in readiness.get("missing", []):
        text = str(missing)
        lowered = text.casefold()
        if "accepted keyframe" in lowered:
            diagnostic = _diagnostic("VISUAL_KEYFRAME_MISSING", "Accepted keyframe is missing.", "visuals")
        elif "storyboard-required references require" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_REQUIRED_REFERENCE_LIMIT",
                text,
                "visuals",
            )
        elif "supports at most" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_REFERENCE_LIMIT",
                f"Reference-to-Video has {selected_count} selected references; maximum supported is {MAX_REF2VA_STILL_REFERENCES}.",
                "visuals",
            )
        elif "required storyboard reference" in lowered and "could not be resolved" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_REQUIRED_REFERENCE_UNRESOLVED",
                text,
                "visuals",
            )
        elif "missing from synchronized visuals selection" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_REQUIRED_REFERENCE_MISSING",
                text,
                "visuals",
            )
        elif "selected reference" in lowered or "required storyboard reference" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_MAPPING_INVALID",
                "Reference-to-Video selected references do not satisfy the current scene mapping.",
                "visuals",
            )
        elif "still reference" in lowered:
            diagnostic = _diagnostic(
                "REF2VA_REFERENCES_MISSING",
                "Reference-to-Video requires at least one selected still reference.",
                "visuals",
            )
        elif "storyboard" in lowered:
            diagnostic = _diagnostic("VISUALS_STORYBOARD_NOT_READY", "Visuals depends on a current applied storyboard.", "visuals")
        else:
            diagnostic = _diagnostic("VISUALS_INCOMPLETE", "Visual inputs are not ready for this scene.", "visuals")
        if diagnostic["code"] not in {entry["code"] for entry in blockers}:
            blockers.append(diagnostic)
    return False, readiness, blockers


def _draft_for(
    prompt_drafts: Mapping[object, object] | None,
    scene_id: str,
    method: str,
) -> Mapping[str, object] | None:
    if not isinstance(prompt_drafts, Mapping):
        return None
    candidates: tuple[object, ...] = ((scene_id, method), f"{scene_id}:{method}", scene_id)
    for key in candidates:
        value = prompt_drafts.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def _prompt_diagnostics(
    project: dict[str, object],
    scene_id: str,
    method: str,
    storage: ProjectStorage,
    prompt_drafts: Mapping[object, object] | None,
) -> tuple[dict[str, object] | None, str, bool, list[dict[str, str]]]:
    try:
        state = prompt_scene_state(
            project,
            scene_id,
            storage=storage,
            generation_method=method,
            include_visual_readiness=False,
        )
    except (PromptCompilationError, ProjectNotFoundError, ProjectValidationError, KeyError, StopIteration):
        return None, "error", False, [
            _diagnostic("PROMPT_STATE_INVALID", "Prompt state could not be resolved for this scene.", "prompt")
        ]

    status = state["status"]
    prompt_ready = False
    blockers: list[dict[str, str]] = []
    if status == PROMPT_STATUS_CURRENT:
        prompt_ready = True
    elif status == PROMPT_STATUS_STALE:
        blockers.append(_diagnostic("PROMPT_STALE", "Prompt is stale. Generate and save a fresh Prompt Director result.", "prompt"))
    elif status == PROMPT_STATUS_NEEDS_GPT:
        if state.get("saved_final_prompt"):
            blockers.append(_diagnostic("PROMPT_NEEDS_GPT", "Prompt provenance is not current. Apply and save a fresh Prompt Director response.", "prompt"))
        else:
            blockers.append(_diagnostic("PROMPT_MISSING", "Final Prompt is missing. Generate and save a Prompt Director result.", "prompt"))
    else:
        blockers.append(_diagnostic("PROMPT_STATE_INVALID", "Prompt state is not renderable.", "prompt"))

    draft = _draft_for(prompt_drafts, scene_id, method)
    if isinstance(draft, Mapping) and draft.get("dirty"):
        draft_fingerprint = draft.get("sourceFingerprint", draft.get("source_fingerprint"))
        if draft_fingerprint and draft_fingerprint != state.get("source_fingerprint"):
            status = PROMPT_STATUS_STALE
            prompt_ready = False
            blockers = [entry for entry in blockers if entry["code"] != "PROMPT_UNSAVED"]
            if not any(entry["code"] == "PROMPT_STALE" for entry in blockers):
                blockers.append(_diagnostic("PROMPT_STALE", "Prompt draft is stale. Generate and save a fresh Prompt Director result.", "prompt"))
        elif status == PROMPT_STATUS_CURRENT:
            status = "unsaved"
            prompt_ready = False
            blockers.append(_diagnostic("PROMPT_UNSAVED", "Prompt has unsaved edits. Save the current Final Prompt before preparation.", "prompt"))
    return state, status, prompt_ready, blockers


def _requirements_state(
    report: object,
    method: str,
) -> tuple[bool, dict[str, object], list[dict[str, str]]]:
    if not isinstance(report, Mapping):
        state = {"method_ready": False, "tools_ready": False, "status": STATUS_UNKNOWN}
        return False, state, [_diagnostic("REQUIREMENTS_UNKNOWN", "Production requirements could not be determined.", "requirements")]

    methods = report.get("methods")
    method_report = methods.get(method) if isinstance(methods, Mapping) else None
    tools = report.get("required_tools")
    if not isinstance(method_report, Mapping) or not isinstance(tools, Mapping):
        state = {"method_ready": False, "tools_ready": False, "status": STATUS_UNKNOWN}
        return False, state, [_diagnostic("REQUIREMENTS_UNKNOWN", "Production requirements report is incomplete.", "requirements")]

    method_ready = method_report.get("ready") is True
    tool_states = [tools.get("ffmpeg"), tools.get("ffprobe")]
    tools_ready = all(isinstance(item, Mapping) and item.get("status") == STATUS_AVAILABLE for item in tool_states)
    blockers: list[dict[str, str]] = []
    if not method_ready:
        missing_names: list[str] = []
        unknown_names: list[str] = []
        for collection_name in ("required_nodes", "required_models"):
            for item in method_report.get(collection_name, []) if isinstance(method_report.get(collection_name), list) else []:
                name = item.get("node_type") or item.get("filename") if isinstance(item, Mapping) else None
                if not name:
                    continue
                if item.get("status") == STATUS_MISSING:
                    missing_names.append(str(name))
                elif item.get("status") == STATUS_UNKNOWN:
                    unknown_names.append(str(name))
        if missing_names:
            blockers.append(_diagnostic(
                "REQUIREMENTS_MISSING",
                f"Required production inputs are missing for {method}: {', '.join(sorted(missing_names))}.",
                "requirements",
            ))
        if unknown_names:
            blockers.append(_diagnostic(
                "REQUIREMENTS_UNKNOWN",
                f"Required production inputs are not yet discoverable for {method}: {', '.join(sorted(unknown_names))}.",
                "requirements",
            ))
        if not blockers:
            blockers.append(_diagnostic("REQUIREMENTS_MISSING", f"Required production inputs are not ready for {method}.", "requirements"))
    if not tools_ready:
        missing_tools = [
            str(item.get("name"))
            for item in tool_states
            if isinstance(item, Mapping) and item.get("status") != STATUS_AVAILABLE
        ]
        if any(isinstance(item, Mapping) and item.get("status") == STATUS_UNKNOWN for item in tool_states):
            code = "REQUIREMENTS_UNKNOWN"
            message = "Required media tools are not yet discoverable."
        else:
            code = "MEDIA_TOOL_MISSING"
            message = f"Required media tool(s) are missing: {', '.join(missing_tools)}."
        blockers.append(_diagnostic(code, message, "requirements"))

    state = {
        "method_ready": method_ready,
        "tools_ready": tools_ready,
        "status": STATUS_AVAILABLE if method_ready and tools_ready else (
            STATUS_UNKNOWN if any(
                (isinstance(item, Mapping) and item.get("status") == STATUS_UNKNOWN)
                for item in [*method_report.get("required_nodes", []), *method_report.get("required_models", []), *tool_states]
            ) else STATUS_MISSING
        ),
    }
    return method_ready and tools_ready, state, blockers


def _workflow_state(contract_report: object, method: str) -> tuple[bool, list[dict[str, str]]]:
    if isinstance(contract_report, Mapping) and method in contract_report:
        return True, []
    return False, [_diagnostic(
        "WORKFLOW_CONTRACT_INVALID",
        "The production workflow contract is unavailable for this generation method.",
        "workflow",
    )]


def _external_file_identity(path: Path) -> dict[str, object]:
    try:
        size, digest = _cached_file_identity(path)
        return {"filename": path.name, "size": size, "sha256": digest}
    except (OSError, ValueError) as error:
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The production workflow template could not be fingerprinted.") from error


def _workflow_identity(method: str) -> dict[str, object]:
    registry = production_manifest_registry()
    manifest = registry.get(method)
    if not isinstance(manifest, dict):
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The generation method has no production workflow manifest.")
    template_path = WORKFLOW_DIRECTORY / str(manifest["workflow_file"])
    if not template_path.is_file() or template_path.is_symlink():
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The production workflow template is unavailable.")
    file_identity = _external_file_identity(template_path)
    return {
        "workflow_id": manifest["workflow_id"],
        "workflow_file": manifest["workflow_file"],
        "template_sha256": file_identity["sha256"],
        "manifest_sha256": _json_hash(manifest),
    }


def _project_file_identity(path: Path, root: Path) -> dict[str, object]:
    owned = _owned_path(path, root, must_exist=True)
    size, digest = _cached_file_identity(owned)
    return {
        "relative_path": _relative_path(owned, root),
        "size": size,
        "sha256": digest,
    }


def _queue_input_name(project_id: str, scene_id: str, filename: str) -> str:
    return f"music_video_builder/{project_id}/{scene_id}/{filename}"


def _resolve_visual_sources(
    project: dict[str, object],
    scene_id: str,
    method: str,
    storage: ProjectStorage,
    root: Path,
) -> list[dict[str, object]]:
    visual_scene = _find_visual_scene(project, scene_id)
    if method == "keyframe_i2v":
        try:
            path = get_keyframe_path_for_project(storage, project, scene_id)
            path = _owned_path(path, root, must_exist=True)
        except (ProjectNotFoundError, ProjectValidationError, RenderPreparationError, OSError) as error:
            raise MediaPreparationError("VISUAL_KEYFRAME_MISSING", "Accepted keyframe is missing.") from error
        suffix = path.suffix.lower() or ".png"
        return [{
            "role": "keyframe",
            "picture_number": 1,
            "source_path": path,
            "source_name": _relative_path(path, root),
            "source_identity": _project_file_identity(path, root),
            "queue_name": _queue_input_name(project["project_id"], scene_id, f"keyframe{suffix}"),
            "target_filename": f"keyframe{suffix}",
        }]

    try:
        mapping = build_ref2va_mapping(project, scene_id)
    except (ProjectNotFoundError, ProjectValidationError, PromptCompilationError, KeyError, StopIteration) as error:
        raise MediaPreparationError("REF2VA_MAPPING_INVALID", "Reference-to-Video selected references do not satisfy the current scene mapping.") from error
    pictures = mapping.get("pictures") if isinstance(mapping, Mapping) else None
    if not isinstance(pictures, list) or not 1 <= len(pictures) <= MAX_REF2VA_STILL_REFERENCES:
        raise MediaPreparationError("REF2VA_MAPPING_INVALID", "Reference-to-Video requires 1 to 9 ordered still references.")

    resolved: list[dict[str, object]] = []
    for picture in pictures:
        if not isinstance(picture, Mapping):
            raise MediaPreparationError("REF2VA_MAPPING_INVALID", "Reference-to-Video picture mapping is invalid.")
        entity_type = picture.get("entity_type")
        kind = "characters" if entity_type == "character" else "locations" if entity_type == "location" else None
        if kind is None:
            raise MediaPreparationError("REF2VA_MAPPING_INVALID", "Reference-to-Video picture ownership is invalid.")
        try:
            path = get_reference_path_for_project(
                storage,
                project,
                kind,
                picture["entity_id"],
                picture["reference_id"],
            )
            path = _owned_path(path, root, must_exist=True)
        except (ProjectNotFoundError, ProjectValidationError, RenderPreparationError, OSError) as error:
            raise MediaPreparationError("REF2VA_MAPPING_INVALID", "A selected Reference-to-Video still cannot be resolved.") from error
        picture_number = picture.get("picture_number")
        suffix = path.suffix.lower() or ".png"
        resolved.append({
            "role": "reference",
            "picture_number": picture_number,
            "picture_tag": picture.get("picture_tag"),
            "subject_tag": picture.get("subject_tag"),
            "entity_type": entity_type,
            "entity_id": picture.get("entity_id"),
            "entity_name": picture.get("entity_name"),
            "reference_id": picture.get("reference_id"),
            "source_path": path,
            "source_name": _relative_path(path, root),
            "source_identity": _project_file_identity(path, root),
            "queue_name": _queue_input_name(project["project_id"], scene_id, f"picture_{int(picture_number):02d}{suffix}"),
            "target_filename": f"picture_{int(picture_number):02d}{suffix}",
        })
    return resolved


def build_preparation_fingerprint(
    project: dict[str, object],
    scene_id: object,
    *,
    prompt_state: Mapping[str, object],
    timing_plan: Mapping[str, object],
    source_audio_identity: Mapping[str, object],
    visual_inputs: list[Mapping[str, object]],
    workflow_identity: Mapping[str, object],
) -> str:
    """Hash only the target scene's authoritative render inputs."""

    scene = _find_scene(project, scene_id)
    method = prompt_state.get("generation_method")
    visual_identity = []
    for item in visual_inputs:
        visual_identity.append({
            key: value
            for key, value in item.items()
            if key not in {"source_path"}
        })
    basis = {
        "preparation_version": PREPARATION_VERSION,
        "project_id": project["project_id"],
        "scene_id": scene["scene_id"],
        "generation_method": method,
        "scene_boundaries": {
            "timeline_start_ms": scene["timeline_start_ms"],
            "timeline_end_ms": scene["timeline_end_ms"],
            "exact_duration_ms": scene["exact_duration_ms"],
        },
        "prompt": {
            "source_fingerprint": prompt_state.get("saved_source_fingerprint"),
            "relay_fingerprint": prompt_state.get("saved_relay_fingerprint"),
            "final_prompt": prompt_state.get("saved_final_prompt"),
            "current_source_fingerprint": prompt_state.get("source_fingerprint"),
        },
        "visual_inputs": visual_identity,
        "source_audio": dict(source_audio_identity),
        "timing": dict(timing_plan),
        "workflow": dict(workflow_identity),
    }
    return _json_hash(basis)


def _preparation_metadata_path(root: Path, scene_id: str) -> Path:
    return root / "renders" / scene_id / PREPARATION_FILENAME


def _artifact_path(root: Path, relative_path: object) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        return None
    candidate = root / Path(relative_path)
    if any(part in {"", ".", ".."} for part in Path(relative_path).parts):
        return None
    try:
        return _owned_path(candidate, root, must_exist=True)
    except RenderPreparationError:
        return None


def _read_preparation_metadata(root: Path, scene_id: str) -> dict[str, object] | None:
    path = _preparation_metadata_path(root, scene_id)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _preparation_artifacts_current(root: Path, metadata: Mapping[str, object]) -> bool:
    workflow = metadata.get("workflow")
    source_audio = metadata.get("source_audio")
    if not isinstance(workflow, Mapping) or not isinstance(source_audio, Mapping):
        return False
    if _artifact_path(root, workflow.get("relative_path")) is None:
        return False
    if _artifact_path(root, source_audio.get("prepared_relative_path")) is None:
        return False
    if _artifact_path(root, source_audio.get("render_relative_path")) is None:
        return False
    visual_inputs = metadata.get("visual_inputs")
    return isinstance(visual_inputs, list) and all(
        isinstance(item, Mapping) and _artifact_path(root, item.get("prepared_relative_path")) is not None
        for item in visual_inputs
    )


def _scene_preflight(
    project: dict[str, object],
    scene: dict[str, object],
    storage: ProjectStorage,
    requirements_report: object,
    workflow_contract_report: object,
    prompt_drafts: Mapping[object, object] | None,
    root: Path,
) -> dict[str, object]:
    scene_id = scene["scene_id"]
    visual_scene = _find_visual_scene(project, scene_id)
    method = visual_scene.get("generation_method")
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if method not in GENERATION_METHODS:
        method = "unknown"
        blockers.append(_diagnostic("GENERATION_METHOD_INVALID", "Scene generation method is not supported.", "content"))

    source_ready, source_path, source_blockers = _source_audio_state(project, root, scene)
    blockers.extend(source_blockers)
    prompt_state: dict[str, object] | None = None
    prompt_status = "error"
    prompt_ready = False
    if method in GENERATION_METHODS:
        prompt_state, prompt_status, prompt_ready, prompt_blockers = _prompt_diagnostics(
            project,
            scene_id,
            method,
            storage,
            prompt_drafts,
        )
        blockers.extend(prompt_blockers)
    visuals_ready = False
    visual_readiness: dict[str, object] = {"scene_id": scene_id, "generation_method": method, "ready": False}
    if method in GENERATION_METHODS:
        visuals_ready, visual_readiness, visual_blockers = _visual_diagnostics(project, scene_id, method, storage)
        blockers.extend(visual_blockers)

    requirements_ready = False
    requirement_state: dict[str, object] = {"method_ready": False, "tools_ready": False, "status": STATUS_UNKNOWN}
    if method in GENERATION_METHODS:
        requirements_ready, requirement_state, requirement_blockers = _requirements_state(requirements_report, method)
        blockers.extend(requirement_blockers)

    workflow_ready, workflow_blockers = _workflow_state(workflow_contract_report, method)
    blockers.extend(workflow_blockers)
    content_ready = prompt_ready and visuals_ready and source_ready
    workflow_dimension_ready = workflow_ready
    runtime_requirements_ready = requirements_ready
    warnings.append(_diagnostic("TARGET_HARDWARE_DEFERRED", TARGET_HARDWARE_MESSAGE, "target_hardware"))

    timing_plan: dict[str, object] | None = None
    visual_inputs: list[dict[str, object]] = []
    source_identity: dict[str, object] | None = None
    workflow_identity: dict[str, object] | None = None
    candidate_fingerprint: str | None = None
    if source_ready and source_path is not None and prompt_state is not None and visuals_ready and workflow_ready:
        try:
            timing_plan = build_h3_timing_plan(scene["exact_duration_ms"])
            visual_inputs = _resolve_visual_sources(project, scene_id, method, storage, root)
            source_identity = _project_file_identity(source_path, root)
            workflow_identity = _workflow_identity(method)
            candidate_fingerprint = build_preparation_fingerprint(
                project,
                scene_id,
                prompt_state=prompt_state,
                timing_plan=timing_plan,
                source_audio_identity=source_identity,
                visual_inputs=visual_inputs,
                workflow_identity=workflow_identity,
            )
        except TimingContractError as error:
            blockers.append(_diagnostic(error.code, error.message, "timing"))
        except (MediaPreparationError, WorkflowCompilationError) as error:
            blockers.append(_diagnostic(error.code, error.message, "inputs"))

    metadata = _read_preparation_metadata(root, scene_id)
    if metadata is None:
        preparation_status = "not_prepared"
    elif candidate_fingerprint and metadata.get("preparation_fingerprint") == candidate_fingerprint and _preparation_artifacts_current(root, metadata):
        preparation_status = "current"
    else:
        preparation_status = "stale"
        warnings.append(_diagnostic("PREPARATION_STALE", "Existing preparation inputs are stale; prepare this scene again.", "preparation"))

    preparation_ready = content_ready and workflow_dimension_ready and runtime_requirements_ready and not any(
        entry["layer"] in {"content", "source_audio", "visuals", "prompt", "workflow", "requirements", "timing", "inputs"}
        for entry in blockers
    )
    # A stale or absent package is exactly what the Prepare action is allowed to
    # repair; it is not a content blocker for a new preparation.
    return {
        "scene_id": scene_id,
        "sequence": project["scenes"].index(scene) + 1,
        "timeline_start_ms": scene["timeline_start_ms"],
        "timeline_end_ms": scene["timeline_end_ms"],
        "duration_ms": scene["exact_duration_ms"],
        "duration": {"start_ms": scene["timeline_start_ms"], "end_ms": scene["timeline_end_ms"], "exact_duration_ms": scene["exact_duration_ms"]},
        "generation_method": method,
        "prompt_status": prompt_status,
        "prompt_status_label": prompt_status.upper(),
        "prompt_ready": prompt_ready,
        "visuals_ready": visuals_ready,
        "visual_readiness": visual_readiness,
        "source_audio_ready": source_ready,
        "workflow_contract_ready": workflow_ready,
        "requirements_ready": requirements_ready,
        "requirements_state": requirement_state,
        "content_ready": content_ready,
        "workflow_ready": workflow_dimension_ready,
        "runtime_requirements_ready": runtime_requirements_ready,
        "target_hardware_qualified": False,
        "preparation_ready": preparation_ready,
        "preparation_status": preparation_status,
        "preparation_current": preparation_status == "current",
        "preparation_fingerprint": candidate_fingerprint,
        "timing_plan": timing_plan,
        "blockers": blockers,
        "warnings": warnings,
    }


def build_render_preflight(
    project: dict[str, object],
    storage: ProjectStorage,
    *,
    requirements_report: object | None = None,
    requirements_snapshot: RequirementsSnapshot | None = None,
    force_requirements_refresh: bool = False,
    prompt_drafts: Mapping[object, object] | None = None,
) -> dict[str, object]:
    """Build the one read-only machine result for all current scenes."""

    canonical_project_id, root = _root_path(storage, project["project_id"])
    if requirements_snapshot is not None:
        report = requirements_snapshot.report_copy()
        requirements_cache = requirements_snapshot.metadata()
    elif requirements_report is not None:
        report = requirements_report
        requirements_cache = {
            "status": "provided",
            "source_state": "provided",
            "generated_at": None,
            "cache_identity": None,
            "scan_duration_ms": None,
            "error": None,
        }
    else:
        snapshot = get_requirements_snapshot(force_refresh=force_requirements_refresh)
        report = snapshot.report_copy()
        requirements_cache = snapshot.metadata()
    try:
        workflow_contract_report = validate_production_workflow_contract()
    except (ProjectValidationError, OSError, UnicodeError, ValueError):
        workflow_contract_report = {}

    scenes = [
        _scene_preflight(
            project,
            scene,
            storage,
            report,
            workflow_contract_report,
            prompt_drafts,
            root,
        )
        for scene in project.get("scenes", [])
    ]
    requirements_required_ready = isinstance(report, Mapping) and report.get("required_ready") is True
    content_ready = all(scene["content_ready"] for scene in scenes) if scenes else False
    workflow_ready = all(scene["workflow_ready"] for scene in scenes) if scenes else False
    runtime_requirements_ready = all(scene["runtime_requirements_ready"] for scene in scenes) if scenes else requirements_required_ready
    preparation_ready_count = sum(1 for scene in scenes if scene["preparation_ready"])
    return {
        "response_version": RENDER_RESPONSE_VERSION,
        "project_id": canonical_project_id,
        "project_name": project.get("name"),
        "requirements": report,
        "requirements_cache": requirements_cache,
        "requirements_required_ready": requirements_required_ready,
        "content_ready": content_ready,
        "workflow_ready": workflow_ready,
        "runtime_requirements_ready": runtime_requirements_ready,
        "target_hardware_qualified": False,
        "target_hardware_status": TARGET_HARDWARE_STATUS,
        "target_hardware_message": TARGET_HARDWARE_MESSAGE,
        "scene_count": len(scenes),
        "preparation_ready_count": preparation_ready_count,
        "blocked_scene_count": sum(1 for scene in scenes if scene["blockers"]),
        "scenes": scenes,
    }


def _patch_declared_input(
    workflow: dict[str, dict[str, object]],
    mapping: object,
    value: object,
    label: str,
    *,
    expected_type: str | None = None,
) -> None:
    if not isinstance(mapping, Mapping) or not isinstance(mapping.get("node_id"), str) or not isinstance(mapping.get("input"), str):
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", f"Production mapping for {label} is invalid.")
    node = workflow.get(mapping["node_id"])
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", f"Production mapping for {label} targets a missing node.")
    if expected_type is not None and node.get("class_type") != expected_type:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", f"Production mapping for {label} targets the wrong node type.")
    input_name = mapping["input"]
    if input_name not in node["inputs"]:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", f"Production mapping for {label} targets a missing input.")
    node["inputs"][input_name] = value


def _compiled_contract_check(
    method: str,
    workflow: dict[str, dict[str, object]],
    manifest: dict[str, object],
    template: dict[str, dict[str, object]],
    timing_plan: Mapping[str, object],
    *,
    visual_inputs: list[Mapping[str, object]],
    queue_audio_name: str,
    prompt_value: str,
    queue_prefix: str,
) -> dict[str, object]:
    """Validate patched values while delegating topology qualification to Phase 6."""

    try:
        _validate_loaded_workflow(manifest, template)
    except (ProjectValidationError, KeyError, TypeError, ValueError) as error:
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The immutable production workflow contract is invalid.") from error

    expected_generation_type = "MiniMaxH3ImageToVideo" if method == "keyframe_i2v" else "MiniMaxH3ReferenceToVideo"
    generation_mapping = manifest["inputs"]["prompt"]
    generation_node = workflow.get(generation_mapping["node_id"])
    if not isinstance(generation_node, dict) or generation_node.get("class_type") != expected_generation_type:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "Generation method mapping targets the wrong H3 node.")
    if generation_node["inputs"].get(generation_mapping["input"]) != prompt_value:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "Final Prompt was not inserted at the declared mapping.")
    audio_mapping = manifest["inputs"]["audio"]["source"]
    audio_node = workflow.get(audio_mapping["node_id"])
    if not isinstance(audio_node, dict) or audio_node["inputs"].get(audio_mapping["input"]) != queue_audio_name:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "Scene audio was not inserted at the declared mapping.")
    frame_mapping = manifest["inputs"]["frame_count"]
    if generation_node["inputs"].get(frame_mapping["input"]) != timing_plan["generated_frame_count"]:
        raise WorkflowCompilationError("TIMING_MAPPING_INVALID", "The H3 frame plan was not inserted at the declared mapping.")
    fps_mapping = manifest["inputs"]["fps"]
    fps_node = workflow.get(fps_mapping["node_id"])
    if not isinstance(fps_node, dict) or fps_node["inputs"].get(fps_mapping["input"]) != H3_FPS:
        raise WorkflowCompilationError("TIMING_MAPPING_INVALID", "The approved H3 FPS was not retained.")
    prefix_mapping = manifest["inputs"]["filename_prefix"]
    prefix_node = workflow.get(prefix_mapping["node_id"])
    if not isinstance(prefix_node, dict) or prefix_node["inputs"].get(prefix_mapping["input"]) != queue_prefix:
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "The deterministic output prefix was not inserted.")

    provisional = manifest["provisional_settings"]
    for setting_name, mapping_name in (
        ("width", "width"),
        ("height", "height"),
        ("steps", "steps"),
        ("sampler", "sampler_name"),
        ("scheduler", "scheduler"),
        ("denoise", "denoise"),
        ("seed", "seed"),
    ):
        mapping = manifest["inputs"][mapping_name]
        node = workflow.get(mapping["node_id"])
        if not isinstance(node, dict) or node["inputs"].get(mapping["input"]) != provisional[setting_name]:
            raise WorkflowCompilationError("WORKFLOW_SETTINGS_CHANGED", f"Approved production setting {setting_name} was changed.")

    if method == "keyframe_i2v":
        if len(visual_inputs) != 1:
            raise WorkflowCompilationError("I2V_INPUT_INVALID", "Keyframe Image-to-Video requires exactly one accepted keyframe.")
        keyframe_mapping = manifest["inputs"]["keyframe"]
        keyframe_node = workflow.get(keyframe_mapping["node_id"])
        if not isinstance(keyframe_node, dict) or keyframe_node["inputs"].get(keyframe_mapping["input"]) != visual_inputs[0]["queue_name"]:
            raise WorkflowCompilationError("I2V_INPUT_INVALID", "The accepted keyframe was not inserted at the declared mapping.")
    else:
        picture_names = [str(item["queue_name"]) for item in visual_inputs]
        if not 1 <= len(picture_names) <= MAX_REF2VA_STILL_REFERENCES:
            raise WorkflowCompilationError("REF2VA_INPUT_INVALID", "Reference-to-Video requires 1 to 9 ordered still references.")
        h3_node = workflow.get("8")
        if not isinstance(h3_node, dict):
            raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "REF2VA generation node is missing.")
        for number in range(1, MAX_REF2VA_STILL_REFERENCES + 1):
            loader_id = str(50 + number - 1)
            destination = f"ref_images.ref_image_{number - 1}"
            if number <= len(picture_names):
                loader = workflow.get(loader_id)
                if not isinstance(loader, dict) or loader.get("class_type") != "LoadImage" or loader["inputs"].get("image") != picture_names[number - 1]:
                    raise WorkflowCompilationError("REF2VA_INPUT_INVALID", "Ordered Reference-to-Video still inputs are invalid.")
                if h3_node["inputs"].get(destination) != [loader_id, 0]:
                    raise WorkflowCompilationError("REF2VA_INPUT_INVALID", "Reference-to-Video picture mapping is not ordered.")
            elif loader_id in workflow or destination in h3_node["inputs"]:
                raise WorkflowCompilationError("REF2VA_INPUT_INVALID", "Inactive Reference-to-Video slots were not removed safely.")

    original_ids = set(template)
    compiled_ids = set(workflow)
    if not compiled_ids.issubset(original_ids):
        raise WorkflowCompilationError("WORKFLOW_MAPPING_INVALID", "Compiled workflow introduced undeclared nodes.")
    return {
        "valid": True,
        "workflow_id": manifest["workflow_id"],
        "generation_method": method,
        "template_node_count": len(template),
        "compiled_node_count": len(workflow),
        "quality_settings_preserved": True,
        "phase6_template_contract_validated": True,
    }


def validate_compiled_workflow_contract(
    generation_method: object,
    workflow: dict[str, dict[str, object]],
    *,
    timing_plan: Mapping[str, object],
    visual_inputs: list[Mapping[str, object]],
    queue_audio_name: str,
    prompt_value: str,
    queue_prefix: str,
) -> dict[str, object]:
    """Public validator for a Phase 8A compiled API workflow."""

    if generation_method not in GENERATION_METHODS:
        raise WorkflowCompilationError("GENERATION_METHOD_INVALID", "The generation method is not supported.")
    registry = production_manifest_registry()
    manifest = registry[generation_method]
    try:
        template = _load_workflow(manifest)
    except (ProjectValidationError, OSError, UnicodeError, ValueError) as error:
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The production workflow template could not be loaded.") from error
    return _compiled_contract_check(
        generation_method,
        workflow,
        manifest,
        template,
        timing_plan,
        visual_inputs=visual_inputs,
        queue_audio_name=queue_audio_name,
        prompt_value=prompt_value,
        queue_prefix=queue_prefix,
    )


def compile_production_workflow(
    project: dict[str, object],
    scene_id: object,
    *,
    final_prompt: str,
    timing_plan: Mapping[str, object],
    visual_inputs: list[Mapping[str, object]],
    scene_audio_name: str,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Materialize one complete API workflow from an immutable Phase 6 template."""

    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    method = visual_scene.get("generation_method")
    if method not in GENERATION_METHODS:
        raise WorkflowCompilationError("GENERATION_METHOD_INVALID", "The generation method is not supported.")
    if not isinstance(final_prompt, str) or not final_prompt.strip():
        raise WorkflowCompilationError("PROMPT_MISSING", "A saved Final Prompt is required for workflow preparation.")
    registry = production_manifest_registry()
    manifest = registry[method]
    try:
        template = _load_workflow(manifest)
    except (ProjectValidationError, OSError, UnicodeError, ValueError) as error:
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "The production workflow template could not be loaded.") from error
    workflow = deepcopy(template)
    expected_type = "MiniMaxH3ImageToVideo" if method == "keyframe_i2v" else "MiniMaxH3ReferenceToVideo"
    inputs = manifest["inputs"]
    _patch_declared_input(workflow, inputs["prompt"], final_prompt, "Final Prompt", expected_type=expected_type)
    _patch_declared_input(workflow, inputs["audio"]["source"], scene_audio_name, "scene audio", expected_type="LoadAudio")
    _patch_declared_input(workflow, inputs["frame_count"], timing_plan["generated_frame_count"], "frame count", expected_type=expected_type)
    _patch_declared_input(workflow, inputs["fps"], timing_plan["h3_fps"], "frame rate", expected_type="VHS_VideoCombine")

    prefix = _queue_input_name(project["project_id"], scene["scene_id"], "render")
    _patch_declared_input(workflow, inputs["filename_prefix"], prefix, "output prefix", expected_type="VHS_VideoCombine")
    if method == "keyframe_i2v":
        _patch_declared_input(workflow, inputs["keyframe"], visual_inputs[0]["queue_name"], "accepted keyframe", expected_type="LoadImage")
    else:
        workflow = patch_reference_picture_slots(workflow, [str(item["queue_name"]) for item in visual_inputs])

    try:
        validation = validate_compiled_workflow_contract(
            method,
            workflow,
            timing_plan=timing_plan,
            visual_inputs=visual_inputs,
            queue_audio_name=scene_audio_name,
            prompt_value=final_prompt,
            queue_prefix=prefix,
        )
    except RenderPreparationError:
        raise
    except (ProjectValidationError, KeyError, TypeError, ValueError) as error:
        raise WorkflowCompilationError("WORKFLOW_CONTRACT_INVALID", "Compiled workflow failed production contract validation.") from error
    return workflow, validation


class MediaToolAdapter:
    """Safe FFmpeg adapter for exact project-owned scene-audio preparation."""

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        duration_probe: Callable[..., int] = probe_audio_duration_ms,
        timeout_seconds: int = 120,
    ):
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg()
        self.ffprobe_path = ffprobe_path
        self.runner = runner
        self.duration_probe = duration_probe
        self.timeout_seconds = timeout_seconds

    def extract_scene_audio(
        self,
        source_path: Path,
        destination_path: Path,
        *,
        project_root: Path,
        start_ms: int,
        end_ms: int,
    ) -> int:
        if not self.ffmpeg_path:
            raise MediaPreparationError("MEDIA_TOOL_MISSING", "FFmpeg is not available for scene-audio preparation.")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int) or start_ms < 0 or end_ms <= start_ms:
            raise MediaPreparationError("SOURCE_AUDIO_BOUNDS_INVALID", "Scene audio boundaries are invalid.")
        try:
            source = _owned_path(source_path, project_root, must_exist=True)
            destination = _owned_path(destination_path, project_root)
        except RenderPreparationError as error:
            raise MediaPreparationError(error.code, error.message) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the media suffix on the temporary path so FFmpeg can select the
        # intended container before the file is atomically installed.
        temporary_path = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix or '.wav'}"
        )
        arguments = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(temporary_path),
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
            raise MediaPreparationError("MEDIA_EXTRACTION_TIMEOUT", "Scene-audio preparation timed out.") from error
        except (FileNotFoundError, OSError) as error:
            raise MediaPreparationError("MEDIA_TOOL_UNAVAILABLE", "FFmpeg could not be started.") from error

        stderr = getattr(completed, "stderr", "")
        if getattr(completed, "returncode", 1) != 0:
            if isinstance(stderr, str) and stderr.strip():
                LOGGER.warning("FFmpeg scene-audio preparation failed: %s", stderr.strip()[:500])
            raise MediaPreparationError("MEDIA_EXTRACTION_FAILED", "FFmpeg could not prepare the authoritative scene-audio segment.")
        if not temporary_path.is_file() or temporary_path.is_symlink():
            raise MediaPreparationError("MEDIA_OUTPUT_INVALID", "FFmpeg did not produce a valid scene-audio segment.")
        try:
            try:
                prepared_duration_ms = self.duration_probe(temporary_path, self.ffprobe_path)
            except TypeError:
                prepared_duration_ms = self.duration_probe(temporary_path)
            if isinstance(prepared_duration_ms, bool) or not isinstance(prepared_duration_ms, int) or prepared_duration_ms <= 0:
                raise MediaPreparationError("MEDIA_OUTPUT_INVALID", "Prepared scene audio has no valid duration.")
            os.replace(temporary_path, destination)
            return prepared_duration_ms
        except MediaProbeUnavailableError as error:
            raise MediaPreparationError("MEDIA_TOOL_MISSING", "FFprobe is not available to validate prepared scene audio.") from error
        except AudioProbeError as error:
            raise MediaPreparationError("MEDIA_OUTPUT_INVALID", "Prepared scene audio could not be validated.") from error
        except RenderPreparationError:
            raise
        except (OSError, ValueError) as error:
            raise MediaPreparationError("MEDIA_OUTPUT_INVALID", "Prepared scene audio could not be installed atomically.") from error
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Could not clean temporary scene-audio file.")


def _copy_asset_atomic(source_path: Path, destination_path: Path, project_root: Path) -> None:
    source = _owned_path(source_path, project_root, must_exist=True)
    destination = _owned_path(destination_path, project_root)
    if destination.exists() and destination.is_symlink():
        raise MediaPreparationError("PATH_UNSAFE", "A render input destination is a symlink.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as source_handle, temporary_path.open("wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
            destination_handle.flush()
            if hasattr(os, "fsync"):
                os.fsync(destination_handle.fileno())
        os.replace(temporary_path, destination)
    except OSError as error:
        raise MediaPreparationError("MEDIA_INPUT_COPY_FAILED", "A project-owned visual input could not be materialized safely.") from error
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.warning("Could not clean temporary visual-input file.")


def _preparation_response(
    metadata: dict[str, object],
    *,
    reused: bool,
    preflight_scene: dict[str, object],
) -> dict[str, object]:
    return {
        "response_version": RENDER_RESPONSE_VERSION,
        "status": "prepared",
        "reused": reused,
        "project_id": metadata["project_id"],
        "scene_id": metadata["scene_id"],
        "generation_method": metadata["generation_method"],
        "preparation_fingerprint": metadata["preparation_fingerprint"],
        "preparation": metadata,
        "preflight": preflight_scene,
        "workflow_validated": True,
        "queue_submitted": False,
    }


def prepare_render_scene(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    requirements_report: object | None = None,
    media_adapter: MediaToolAdapter | None = None,
) -> dict[str, object]:
    """Prepare one eligible scene and stop before queue submission."""

    canonical_project_id, root = _root_path(storage, project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    project = storage.load_project(canonical_project_id)
    preflight = build_render_preflight(project, storage, requirements_report=requirements_report)
    preflight_scene = next((scene for scene in preflight["scenes"] if scene["scene_id"] == canonical_scene_id), None)
    if not isinstance(preflight_scene, dict):
        raise ProjectNotFoundError("The requested scene was not found.")
    if not preflight_scene["preparation_ready"]:
        raise RenderPreparationBlocked(preflight_scene)

    scene = _find_scene(project, canonical_scene_id)
    method = preflight_scene["generation_method"]
    source_ready, source_path, source_blockers = _source_audio_state(project, root, scene)
    if not source_ready or source_path is None:
        raise RenderPreparationError("SOURCE_AUDIO_UNAVAILABLE", "Master audio file cannot be resolved.")
    try:
        prompt_state = prompt_scene_state(project, canonical_scene_id, storage=storage, generation_method=method)
        timing_plan = build_h3_timing_plan(scene["exact_duration_ms"])
        visual_inputs = _resolve_visual_sources(project, canonical_scene_id, method, storage, root)
        source_identity = _project_file_identity(source_path, root)
        workflow_identity = _workflow_identity(method)
        preparation_fingerprint = build_preparation_fingerprint(
            project,
            canonical_scene_id,
            prompt_state=prompt_state,
            timing_plan=timing_plan,
            source_audio_identity=source_identity,
            visual_inputs=visual_inputs,
            workflow_identity=workflow_identity,
        )
    except TimingContractError:
        raise
    except RenderPreparationError:
        raise
    except (PromptCompilationError, ProjectNotFoundError, ProjectValidationError, KeyError, StopIteration) as error:
        raise RenderPreparationError("PREPARATION_INPUTS_INVALID", "Current scene inputs could not be resolved safely.") from error

    metadata_path = _preparation_metadata_path(root, canonical_scene_id)
    existing = _read_preparation_metadata(root, canonical_scene_id)
    if (
        isinstance(existing, dict)
        and existing.get("preparation_fingerprint") == preparation_fingerprint
        and _preparation_artifacts_current(root, existing)
    ):
        return _preparation_response(existing, reused=True, preflight_scene=preflight_scene)

    scene_audio_directory = root / "scene_audio" / canonical_scene_id
    render_directory = root / "renders" / canonical_scene_id
    inputs_directory = render_directory / RENDER_INPUT_DIRECTORY
    scene_audio_path = scene_audio_directory / SCENE_AUDIO_FILENAME
    render_audio_path = inputs_directory / SCENE_AUDIO_FILENAME
    for directory in (scene_audio_directory, render_directory, inputs_directory):
        directory.mkdir(parents=True, exist_ok=True)
        _owned_path(directory, root)

    adapter = media_adapter or MediaToolAdapter()
    prepared_duration_ms = adapter.extract_scene_audio(
        source_path,
        scene_audio_path,
        project_root=root,
        start_ms=scene["timeline_start_ms"],
        end_ms=scene["timeline_end_ms"],
    )
    _copy_asset_atomic(scene_audio_path, render_audio_path, root)

    prepared_visual_inputs: list[dict[str, object]] = []
    for visual_input in visual_inputs:
        target = inputs_directory / str(visual_input["target_filename"])
        _copy_asset_atomic(visual_input["source_path"], target, root)
        prepared_visual_inputs.append({
            key: value
            for key, value in visual_input.items()
            if key != "source_path"
        } | {"prepared_relative_path": _relative_path(target, root)})

    workflow, validation = compile_production_workflow(
        project,
        canonical_scene_id,
        final_prompt=prompt_state["saved_final_prompt"],
        timing_plan=timing_plan,
        visual_inputs=visual_inputs,
        scene_audio_name=_queue_input_name(canonical_project_id, canonical_scene_id, SCENE_AUDIO_FILENAME),
    )
    workflow_path = render_directory / WORKFLOW_FILENAME
    workflow_relative_path = _relative_path(workflow_path, root)
    metadata = {
        "preparation_version": PREPARATION_VERSION,
        "project_id": canonical_project_id,
        "scene_id": canonical_scene_id,
        "generation_method": method,
        "preparation_fingerprint": preparation_fingerprint,
        "source_audio": {
            **source_identity,
            "start_ms": scene["timeline_start_ms"],
            "end_ms": scene["timeline_end_ms"],
            "authoritative_duration_ms": scene["exact_duration_ms"],
            "prepared_duration_ms": prepared_duration_ms,
            "prepared_relative_path": _relative_path(scene_audio_path, root),
            "render_relative_path": _relative_path(render_audio_path, root),
            "queue_name": _queue_input_name(canonical_project_id, canonical_scene_id, SCENE_AUDIO_FILENAME),
        },
        "visual_inputs": prepared_visual_inputs,
        "timing": timing_plan,
        "workflow": {
            **workflow_identity,
            "relative_path": workflow_relative_path,
            "contract_validation": validation,
        },
        "queue_submitted": False,
    }
    try:
        atomic_write_json(workflow_path, workflow)
        atomic_write_json(metadata_path, metadata)
    except ProjectPersistenceError as error:
        raise RenderPreparationError("PREPARATION_ARTIFACT_WRITE_FAILED", "Preparation artifacts could not be written atomically.") from error
    return _preparation_response(metadata, reused=False, preflight_scene=preflight_scene)


# Descriptive aliases keep the route/service vocabulary discoverable without
# creating a second implementation.
render_preflight = build_render_preflight
prepare_render_inputs = prepare_render_scene
