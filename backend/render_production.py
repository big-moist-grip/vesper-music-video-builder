"""Phase 8E production post-processing / Production Scene foundation.

Production pipeline position:

    Raw H3 -> Final Scene -> Production Scene

The Production Scene is the selected post-processing result intended for the
later Resolve handoff.  Three authoritative user-facing methods exist:

    none             -> Production Scene resolves to the current Final Scene
    rtx_vsr_fast     -> RTX Video Super Resolution (donor-frozen graph)
    seedvr2_quality  -> SeedVR2 quality upscale (donor-frozen graph)

This module orchestrates the existing authoritative services only: Phase 8C
finalization metadata supplies the current Final Scene identity, the central
ComfyUI client executes ComfyUI-backed methods, and deterministic output
ownership/atomic promotion follow the Phase 8B/8C durability philosophy.
Upscale failure never invalidates the Raw H3 or Final Scene.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
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
from .render import H3_FPS
from .render_finalize import (
    FINALIZATION_STATE_FINALIZED,
    MediaProbeAdapter,
    RenderFinalizationError,
    _audio_tolerance_ms,
    _video_tolerance_ms,
    summarize_render_job_finalization,
)
from .render_jobs import (
    ACTIVE_STATES,
    CANCELLED,
    FAILED,
    INTERRUPTED,
    SUCCEEDED,
    ComfyUIClient,
    ComfyUIClientError,
    discover_raw_output,
    RenderOutputDiscoveryError,
    sanitize_user_facing_message,
)
from .workflows import (
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    POSTPROCESS_METHOD_LABELS,
    production_manifest_registry,
    production_postprocess_manifest_registry,
)


LOGGER = logging.getLogger(__name__)

PRODUCTION_SCHEMA_VERSION = 1
PRODUCTION_DIRECTORY = "production"
PRODUCTION_CURRENT_FILENAME = "production.json"
POSTPROCESS_JOB_DIRECTORY = "postprocess_jobs"
POSTPROCESS_JOB_FILENAME_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$"
)

# Stable method identities.  Labels are authoritative user-facing text.
PRODUCTION_METHODS = (
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
)

# Production Scene summary states.
PRODUCTION_NOT_AVAILABLE = "NOT_AVAILABLE"
PRODUCTION_CURRENT = "CURRENT"
PRODUCTION_NEEDS_POSTPROCESS = "NEEDS_POSTPROCESS"
PRODUCTION_STALE = "STALE"
PRODUCTION_FAILED = "FAILED"
PRODUCTION_STATES = frozenset({
    PRODUCTION_NOT_AVAILABLE,
    PRODUCTION_CURRENT,
    PRODUCTION_NEEDS_POSTPROCESS,
    PRODUCTION_STALE,
    PRODUCTION_FAILED,
})

# Production action planning constants (minimum-work semantics).
ACTION_PRODUCTION_COMPLETE = "PRODUCTION_COMPLETE"
ACTION_POSTPROCESS_ONLY = "POSTPROCESS_ONLY"
ACTION_FINALIZE_AND_POSTPROCESS = "FINALIZE_AND_POSTPROCESS"
ACTION_RENDER_FINALIZE_POSTPROCESS = "RENDER_FINALIZE_POSTPROCESS"
ACTION_PREPARE_RENDER_FINALIZE_POSTPROCESS = "PREPARE_RENDER_FINALIZE_POSTPROCESS"
ACTION_PRODUCTION_BLOCKED = "PRODUCTION_BLOCKED"

PRODUCTION_ACTIONS = frozenset({
    ACTION_PRODUCTION_COMPLETE,
    ACTION_POSTPROCESS_ONLY,
    ACTION_FINALIZE_AND_POSTPROCESS,
    ACTION_RENDER_FINALIZE_POSTPROCESS,
    ACTION_PREPARE_RENDER_FINALIZE_POSTPROCESS,
    ACTION_PRODUCTION_BLOCKED,
})

# Post-process job lifecycle (intentionally separate from H3 render jobs).
POSTPROCESS_READY = "READY"
POSTPROCESS_SUBMITTING = "SUBMITTING"
POSTPROCESS_SUBMITTED = "SUBMITTED"
POSTPROCESS_ACTIVE = "ACTIVE"
POSTPROCESS_SUCCEEDED = "SUCCEEDED"
POSTPROCESS_FAILED = "FAILED"
POSTPROCESS_CANCELLED = "CANCELLED"
POSTPROCESS_JOB_STATES = frozenset({
    POSTPROCESS_READY,
    POSTPROCESS_SUBMITTING,
    POSTPROCESS_SUBMITTED,
    POSTPROCESS_ACTIVE,
    POSTPROCESS_SUCCEEDED,
    POSTPROCESS_FAILED,
    POSTPROCESS_CANCELLED,
})
POSTPROCESS_ACTIVE_STATES = frozenset({POSTPROCESS_SUBMITTING, POSTPROCESS_SUBMITTED, POSTPROCESS_ACTIVE})
POSTPROCESS_TERMINAL_STATES = frozenset({POSTPROCESS_SUCCEEDED, POSTPROCESS_FAILED, POSTPROCESS_CANCELLED})

# Method availability states.
METHOD_AVAILABLE = "AVAILABLE"
METHOD_UNAVAILABLE = "UNAVAILABLE"
METHOD_NOT_DETERMINABLE = "NOT_DETERMINABLE"
METHOD_UNQUALIFIED = "UNQUALIFIED"

# Frozen resolution policy evidence.  H3 generation is natively 960x544 for
# both production methods (manifest provisional settings).  The donor contracts
# upscale by exactly 2.0x: RTX VSR through "scale by multiplier" 2.0 with the
# node's own round-to-multiple-of-8 normalization; SeedVR2 through resolution
# targets of 2x the native dimensions (1088 short side / 1920 long side).
RTX_VSR_SCALE = 2.0
RTX_VSR_DIMENSION_MULTIPLE = 8
UPSCALE_TARGET_SCALE = 2.0


class RenderProductionError(Exception):
    """Base class for product-facing production post-processing failures."""

    status = 422

    def __init__(self, code: str, message: str, *, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class ProductionMethodUnavailable(RenderProductionError):
    status = 422


class ProductionNotReady(RenderProductionError):
    status = 422


class PostprocessJobNotFound(RenderProductionError):
    status = 404


class PostprocessJobConflict(RenderProductionError):
    status = 409


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Method registry / project setting
# ---------------------------------------------------------------------------

def production_method_registry() -> dict[str, dict[str, object]]:
    """The three authoritative production upscale choices."""

    manifests = production_postprocess_manifest_registry()
    return {
        POSTPROCESS_METHOD_NONE: {
            "method": POSTPROCESS_METHOD_NONE,
            "label": POSTPROCESS_METHOD_LABELS[POSTPROCESS_METHOD_NONE],
            "comfyui_backed": False,
            "workflow_id": None,
            "resolution": "native_final_scene",
        },
        POSTPROCESS_METHOD_RTX_VSR_FAST: {
            "method": POSTPROCESS_METHOD_RTX_VSR_FAST,
            "label": POSTPROCESS_METHOD_LABELS[POSTPROCESS_METHOD_RTX_VSR_FAST],
            "comfyui_backed": True,
            "workflow_id": manifests[POSTPROCESS_METHOD_RTX_VSR_FAST]["workflow_id"],
            "resolution": "2x_native_multiple_8",
        },
        POSTPROCESS_METHOD_SEEDVR2_QUALITY: {
            "method": POSTPROCESS_METHOD_SEEDVR2_QUALITY,
            "label": POSTPROCESS_METHOD_LABELS[POSTPROCESS_METHOD_SEEDVR2_QUALITY],
            "comfyui_backed": True,
            "workflow_id": manifests[POSTPROCESS_METHOD_SEEDVR2_QUALITY]["workflow_id"],
            "resolution": "2x_native",
        },
    }


def validate_production_method(value: object) -> str:
    if not isinstance(value, str) or value not in PRODUCTION_METHODS:
        raise ProjectValidationError("Production upscale method is not one of the authoritative options.")
    return value


def default_production_settings() -> dict[str, object]:
    return {"upscale_method": POSTPROCESS_METHOD_NONE}


def validate_production_settings(document: object) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise ProjectValidationError("Production settings must be a JSON object.")
    if set(document) != {"upscale_method"}:
        raise ProjectValidationError("Production settings support only upscale_method.")
    return {"upscale_method": validate_production_method(document.get("upscale_method"))}


def resolve_project_production_method(project: Mapping[str, object]) -> str:
    """Migration-safe resolution: unknown/absent settings resolve to None."""

    production = project.get("production") if isinstance(project, Mapping) else None
    if not isinstance(production, Mapping):
        return POSTPROCESS_METHOD_NONE
    method = production.get("upscale_method")
    if not isinstance(method, str) or method not in PRODUCTION_METHODS:
        return POSTPROCESS_METHOD_NONE
    return method


# ---------------------------------------------------------------------------
# Resolution policy (donor-derived, frozen)
# ---------------------------------------------------------------------------

def native_source_dimensions() -> tuple[int, int]:
    """Authoritative H3 generation dimensions from the production manifests."""

    registry = production_manifest_registry()
    dimensions = set()
    for manifest in registry.values():
        settings = manifest.get("provisional_settings")
        if isinstance(settings, Mapping):
            width = settings.get("width")
            height = settings.get("height")
            if isinstance(width, int) and isinstance(height, int):
                dimensions.add((width, height))
    if len(dimensions) != 1:
        raise RenderProductionError("RESOLUTION_POLICY_INVALID", "The H3 source resolution policy is not unique.")
    return next(iter(dimensions))


def rtx_vsr_dimension(value: float) -> int:
    """The installed RTXVideoSuperResolution normalization: multiple of 8, min 8."""

    return max(RTX_VSR_DIMENSION_MULTIPLE, round(value / RTX_VSR_DIMENSION_MULTIPLE) * RTX_VSR_DIMENSION_MULTIPLE)


def production_target_dimensions(method: str, source_width: int, source_height: int) -> tuple[int, int] | None:
    """Frozen deterministic upscale target for a method, or None for native."""

    canonical = validate_production_method(method)
    if not isinstance(source_width, int) or not isinstance(source_height, int) or source_width <= 0 or source_height <= 0:
        raise RenderProductionError("RESOLUTION_SOURCE_INVALID", "The Production Scene source dimensions are invalid.")
    if canonical == POSTPROCESS_METHOD_NONE:
        return None
    if canonical == POSTPROCESS_METHOD_RTX_VSR_FAST:
        return (
            rtx_vsr_dimension(source_width * RTX_VSR_SCALE),
            rtx_vsr_dimension(source_height * RTX_VSR_SCALE),
        )
    # SeedVR2: exact 2x targets; live qualification confirms node alignment.
    return (
        int(round(source_width * UPSCALE_TARGET_SCALE)),
        int(round(source_height * UPSCALE_TARGET_SCALE)),
    )


def production_resolution_policy() -> dict[str, object]:
    width, height = native_source_dimensions()
    return {
        "policy_version": "phase8e_donor_frozen_v1",
        "source": "donor contracts + installed RTXVideoSuperResolution node semantics",
        "native_source": {"width": width, "height": height, "fps": H3_FPS},
        "source_aspect_ratio": "30:17",
        "spatial_multiplier": 2.0,
        "mod_8_safe": True,
        "methods": {
            POSTPROCESS_METHOD_NONE: {"target": None, "rule": "native Final Scene resolution; no re-encode"},
            POSTPROCESS_METHOD_RTX_VSR_FAST: {
                "target": production_target_dimensions(POSTPROCESS_METHOD_RTX_VSR_FAST, width, height),
                "rule": "scale by multiplier 2.0, ULTRA; node rounds to multiples of 8 (1920x1088)",
            },
            POSTPROCESS_METHOD_SEEDVR2_QUALITY: {
                "target": production_target_dimensions(POSTPROCESS_METHOD_SEEDVR2_QUALITY, width, height),
                "rule": "2x native dimensions (1920x1088); donor resolution targets; live qualification pending",
            },
        },
        "aspect_ratio_preserved": True,
        "no_crop_no_stretch": True,
    }


# ---------------------------------------------------------------------------
# Method availability (method-specific; never a global Builder gate)
# ---------------------------------------------------------------------------

def method_runtime_requirements(method: str) -> dict[str, object]:
    canonical = validate_production_method(method)
    if canonical == POSTPROCESS_METHOD_NONE:
        return {"node_types": [], "models": [], "note": "None requires only a current Final Scene."}
    manifest = production_postprocess_manifest_registry()[canonical]
    return {
        "node_types": list(manifest["required_node_types"]),
        "models": deepcopy(manifest["required_models"]),
    }


def is_rtx_vsr_hardware_supported() -> bool:
    """Check if the current runtime environment supports NVIDIA RTX VSR execution.

    Scoped exclusively to rtx_vsr_fast. Checks for NVIDIA CUDA runtime
    (torch.cuda with valid torch.version.cuda and non-HIP/non-ROCm backend).
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        if getattr(torch.version, "cuda", None) is None:
            return False
        if getattr(torch.version, "hip", None) is not None:
            return False
        return True
    except Exception:
        return False


def production_method_availability(
    method: str,
    *,
    node_types: set[str] | None = None,
    model_status: Mapping[str, str] | None = None,
    hardware_supported: bool | None = None,
) -> dict[str, object]:
    """Truthful method availability.  Detection is lazy and method-specific.

    RTX VSR is verified against actual runtime CUDA/NVIDIA hardware capability.
    SeedVR2 is evaluated independently on its own requirements.
    None mode is always available.
    """

    canonical = validate_production_method(method)
    if canonical == POSTPROCESS_METHOD_NONE:
        return {"method": canonical, "state": METHOD_AVAILABLE, "missing_nodes": [], "missing_models": []}

    if canonical == POSTPROCESS_METHOD_RTX_VSR_FAST:
        hw_ok = is_rtx_vsr_hardware_supported() if hardware_supported is None else bool(hardware_supported)
        if not hw_ok:
            return {
                "method": canonical,
                "state": METHOD_UNAVAILABLE,
                "reason": "UNSUPPORTED_HARDWARE",
                "missing_nodes": [],
                "missing_models": [],
            }

    if node_types is None:
        try:
            from .requirements import _load_active_node_types
            active_nodes, _ = _load_active_node_types()
            if active_nodes is not None:
                node_types = active_nodes
        except Exception:
            pass

    if node_types is None:
        return {"method": canonical, "state": METHOD_NOT_DETERMINABLE, "missing_nodes": [], "missing_models": []}
    requirements = method_runtime_requirements(canonical)
    missing_nodes = [node_type for node_type in requirements["node_types"] if node_type not in node_types]
    missing_models: list[str] = []
    for declaration in requirements["models"]:
        filename = str(declaration["filename"])
        status = (model_status or {}).get(filename)
        if status != "AVAILABLE":
            missing_models.append(filename)
    if missing_nodes or missing_models:
        return {"method": canonical, "state": METHOD_UNAVAILABLE, "missing_nodes": missing_nodes, "missing_models": missing_models}
    if canonical == POSTPROCESS_METHOD_SEEDVR2_QUALITY:
        # Structurally available; live VRAM/quality qualification on the RTX
        # production machine remains pending and is never implied here.
        return {"method": canonical, "state": METHOD_UNQUALIFIED, "missing_nodes": [], "missing_models": []}
    return {"method": canonical, "state": METHOD_AVAILABLE, "missing_nodes": [], "missing_models": []}


# ---------------------------------------------------------------------------
# Workflow compilation (manifest-owned patch points only)
# ---------------------------------------------------------------------------

def compile_postprocess_workflow(
    method: str,
    *,
    final_scene_selector: str,
    output_prefix: str,
) -> dict[str, object]:
    """Patch the frozen template with owned input/output selectors.

    No client-provided workflow JSON is ever accepted; only the two owned
    placeholders are replaced.
    """

    canonical = validate_production_method(method)
    if canonical == POSTPROCESS_METHOD_NONE:
        raise RenderProductionError("POSTPROCESS_WORKFLOW_NOT_APPLICABLE", "The None method performs no post-processing.")
    if not isinstance(final_scene_selector, str) or not final_scene_selector or ".." in final_scene_selector:
        raise RenderProductionError("POSTPROCESS_INPUT_UNSAFE", "The materialized Final Scene selector is invalid.")
    if not isinstance(output_prefix, str) or not output_prefix or ".." in output_prefix:
        raise RenderProductionError("POSTPROCESS_OUTPUT_UNSAFE", "The production output prefix is invalid.")
    manifest = production_postprocess_manifest_registry()[canonical]
    from .workflows import _load_workflow

    workflow = _load_workflow(manifest)
    video_mapping = manifest["inputs"]["final_scene_video"]
    prefix_mapping = manifest["inputs"]["filename_prefix"]
    video_node = workflow[str(video_mapping["node_id"])]
    if video_node["inputs"].get(video_mapping["input"]) != video_mapping["placeholder"]:
        raise RenderProductionError("POSTPROCESS_WORKFLOW_INVALID", "The post-processing template input placeholder is invalid.")
    video_node["inputs"][video_mapping["input"]] = final_scene_selector
    output_node = workflow[str(prefix_mapping["node_id"])]
    if output_node["inputs"].get(prefix_mapping["input"]) != prefix_mapping["placeholder"]:
        raise RenderProductionError("POSTPROCESS_WORKFLOW_INVALID", "The post-processing template output placeholder is invalid.")
    output_node["inputs"][prefix_mapping["input"]] = output_prefix
    return workflow


# ---------------------------------------------------------------------------
# Production fingerprint / currentness
# ---------------------------------------------------------------------------

def build_production_fingerprint(
    *,
    final_scene_identity: Mapping[str, object],
    method: str,
) -> str:
    """Deterministic Production Scene fingerprint.

    Depends only on the current Final Scene identity and the selected method's
    frozen contract.  Changing the method invalidates Production only; Prompt,
    Preparation, H3 jobs, Raw H3, and Final Scene are never touched.
    """

    canonical = validate_production_method(method)
    if not isinstance(final_scene_identity, Mapping):
        raise RenderProductionError("PRODUCTION_FINGERPRINT_INVALID", "The Final Scene identity is unavailable.")
    basis: dict[str, object] = {
        "production_schema_version": PRODUCTION_SCHEMA_VERSION,
        "method": canonical,
        "final_scene_identity": {
            key: final_scene_identity.get(key)
            for key in ("relative_path", "size", "sha256")
        },
    }
    if canonical != POSTPROCESS_METHOD_NONE:
        manifest = production_postprocess_manifest_registry()[canonical]
        basis["workflow_id"] = manifest["workflow_id"]
        basis["frozen_settings"] = manifest["frozen_settings"]
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _production_directory(root: Path, scene_id: str, *, create: bool = True) -> Path:
    render_directory = root / "renders" / scene_id
    production = render_directory / PRODUCTION_DIRECTORY
    if not create:
        return production
    for directory in (render_directory, production):
        if directory.exists() and directory.is_symlink():
            raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The production scene directory is a symlink.")
        directory.mkdir(parents=True, exist_ok=True)
    return production


def _production_current_path(root: Path, scene_id: str) -> Path:
    path = _production_directory(root, scene_id) / PRODUCTION_CURRENT_FILENAME
    if path.is_symlink():
        raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The production current record is a symlink.")
    return path


def _postprocess_job_directory(root: Path, scene_id: str, *, create: bool = True) -> Path:
    production = _production_directory(root, scene_id, create=create)
    jobs = production / POSTPROCESS_JOB_DIRECTORY
    if not create:
        return jobs
    if jobs.exists() and jobs.is_symlink():
        raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The post-process job directory is a symlink.")
    jobs.mkdir(parents=True, exist_ok=True)
    return jobs


def _read_production_current(root: Path, scene_id: str) -> dict[str, object] | None:
    path = _production_current_path(root, scene_id)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or document.get("production_schema_version") != PRODUCTION_SCHEMA_VERSION:
        return None
    return dict(document)


def _current_final_scene(
    storage: ProjectStorage,
    project_id: str,
    scene_id: str,
    *,
    preflight: Mapping[str, object] | None,
    raw_output_root: Path | None = None,
):
    """Return (finalization_summary, job) for the newest SUCCEEDED scene job."""

    from .render_jobs import JobStore

    jobs = JobStore(storage).list_scene(project_id, scene_id)
    succeeded = [job for job in jobs if job.get("state") == SUCCEEDED]
    if not succeeded:
        return None, None
    latest = succeeded[-1]
    finalization = latest.get("finalization")
    if isinstance(finalization, Mapping) and finalization.get("state") == FINALIZATION_STATE_FINALIZED and finalization.get("raw_current") is True:
        return finalization, latest
    summary = summarize_render_job_finalization(storage, latest, preflight=preflight, raw_output_root=raw_output_root)
    return summary, latest


def summarize_production_scene(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    method: str,
    preflight: Mapping[str, object] | None = None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    """Authoritative Production Scene state for one scene and method.

    For ``none`` the Production Scene resolves directly to the current Final
    Scene artifact without any re-encode.  For upscale methods the per-method
    production record is compared against the deterministic fingerprint of the
    current Final Scene identity plus the frozen method contract.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    canonical = validate_production_method(method)
    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    finalization, job = _current_final_scene(
        storage,
        canonical_project_id,
        canonical_scene_id,
        preflight=preflight,
        raw_output_root=raw_output_root,
    )
    final_current = (
        isinstance(finalization, Mapping)
        and finalization.get("state") == FINALIZATION_STATE_FINALIZED
        and finalization.get("raw_current") is True
        and isinstance(finalization.get("output"), Mapping)
    )
    result: dict[str, object] = {
        "production_schema_version": PRODUCTION_SCHEMA_VERSION,
        "project_id": canonical_project_id,
        "scene_id": canonical_scene_id,
        "method": canonical,
        "state": PRODUCTION_NOT_AVAILABLE,
        "final_scene_current": final_current,
        "fingerprint": None,
        "output": None,
        "postprocess_job_id": None,
        "failure": None,
    }
    if not final_current:
        return result
    final_output = finalization.get("output")
    identity = final_output.get("identity") if isinstance(final_output, Mapping) else None
    fingerprint = build_production_fingerprint(final_scene_identity=identity, method=canonical)
    result["fingerprint"] = fingerprint
    if canonical == POSTPROCESS_METHOD_NONE:
        result["state"] = PRODUCTION_CURRENT
        result["output"] = deepcopy(dict(final_output))
        result["resolves_to_final_scene"] = True
        return result
    current = _read_production_current(root, canonical_scene_id)
    if not isinstance(current, Mapping) or current.get("method") != canonical:
        result["state"] = PRODUCTION_NEEDS_POSTPROCESS
        return result
    if current.get("state") == PRODUCTION_FAILED:
        result["state"] = PRODUCTION_FAILED
        failure_doc = deepcopy(current.get("failure")) if isinstance(current.get("failure"), Mapping) else None
        if failure_doc and "message" in failure_doc:
            failure_doc["message"] = sanitize_user_facing_message(failure_doc.get("message"), fallback="Production upscale failed; retry is available.")
        result["failure"] = failure_doc
        result["postprocess_job_id"] = current.get("postprocess_job_id")
        return result
    if current.get("fingerprint") != fingerprint:
        result["state"] = PRODUCTION_STALE
        result["postprocess_job_id"] = current.get("postprocess_job_id")
        return result
    output = current.get("output")
    if not isinstance(output, Mapping) or not isinstance(output.get("relative_path"), str):
        result["state"] = PRODUCTION_NEEDS_POSTPROCESS
        return result
    output_path = root / str(output["relative_path"])
    if output_path.is_symlink() or not output_path.is_file():
        result["state"] = PRODUCTION_NEEDS_POSTPROCESS
        return result
    result["state"] = PRODUCTION_CURRENT
    result["output"] = deepcopy(dict(output))
    result["postprocess_job_id"] = current.get("postprocess_job_id")
    return result


# ---------------------------------------------------------------------------
# Post-process job model
# ---------------------------------------------------------------------------

def new_postprocess_job_record(
    project_id: str,
    scene_id: str,
    method: str,
    *,
    production_fingerprint: str,
    now: str | None = None,
) -> dict[str, object]:
    canonical = validate_production_method(method)
    if canonical == POSTPROCESS_METHOD_NONE:
        raise RenderProductionError("POSTPROCESS_JOB_NOT_APPLICABLE", "The None method performs no post-processing.")
    timestamp = now or _timestamp()
    return {
        "production_schema_version": PRODUCTION_SCHEMA_VERSION,
        "postprocess_job_id": str(uuid.uuid4()),
        "project_id": validate_project_id(project_id),
        "scene_id": validate_entity_id(scene_id, "Scene ID"),
        "method": canonical,
        "state": POSTPROCESS_READY,
        "production_fingerprint": production_fingerprint,
        "comfy_prompt_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "submitted_at": None,
        "output": None,
        "failure": None,
        "cancel": None,
        "transitions": [],
    }


def transition_postprocess_job(
    record: Mapping[str, object],
    new_state: str,
    *,
    reason: str,
    updates: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized = deepcopy(dict(record))
    old_state = normalized.get("state")
    if new_state not in POSTPROCESS_JOB_STATES:
        raise RenderProductionError("POSTPROCESS_STATE_INVALID", "The requested post-process job state is invalid.")
    if old_state in POSTPROCESS_TERMINAL_STATES and new_state != old_state:
        raise RenderProductionError("POSTPROCESS_TERMINAL_IMMUTABLE", "A terminal post-process job cannot change state.")
    normalized["state"] = new_state
    normalized["updated_at"] = _timestamp()
    if updates:
        normalized.update(deepcopy(dict(updates)))
    transitions = normalized.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions.append({"from": old_state, "to": new_state, "at": normalized["updated_at"], "reason": reason})
    normalized["transitions"] = transitions[-50:]
    return normalized


def sanitize_postprocess_job_record_for_presentation(record: Mapping[str, object]) -> dict[str, object]:
    pjob = deepcopy(dict(record))
    failure = pjob.get("failure")
    if isinstance(failure, Mapping):
        f_copy = deepcopy(dict(failure))
        if "message" in f_copy:
            f_copy["message"] = sanitize_user_facing_message(f_copy.get("message"), fallback="Production upscale failed; retry is available.")
        pjob["failure"] = f_copy
    return pjob


class PostprocessJobStore:
    """Durable per-scene post-process job records under project-owned runtime."""

    def __init__(self, storage: ProjectStorage):
        self.storage = storage

    def _root(self, project_id: str) -> Path:
        return self.storage.project_directory(project_id).resolve(strict=True)

    def save(self, record: Mapping[str, object]) -> dict[str, object]:
        project_id = validate_project_id(record.get("project_id"))
        scene_id = validate_entity_id(record.get("scene_id"), "Scene ID")
        job_id = record.get("postprocess_job_id")
        if not isinstance(job_id, str) or not POSTPROCESS_JOB_FILENAME_PATTERN.fullmatch(f"{job_id}.json"):
            raise RenderProductionError("POSTPROCESS_JOB_INVALID", "The post-process job identity is invalid.")
        if record.get("state") not in POSTPROCESS_JOB_STATES:
            raise RenderProductionError("POSTPROCESS_JOB_INVALID", "The post-process job state is invalid.")
        directory = _postprocess_job_directory(self._root(project_id), scene_id)
        path = directory / f"{job_id}.json"
        if path.is_symlink():
            raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The post-process job record is a symlink.")
        try:
            atomic_write_json(path, dict(record))
        except ProjectPersistenceError:
            raise
        except OSError as error:
            raise ProjectPersistenceError("Could not persist the post-process job record atomically.") from error
        return deepcopy(dict(record))

    def load(self, project_id: object, scene_id: object, job_id: object) -> dict[str, object]:
        canonical_project_id = validate_project_id(project_id)
        canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
        if not isinstance(job_id, str) or not POSTPROCESS_JOB_FILENAME_PATTERN.fullmatch(f"{job_id}.json"):
            raise PostprocessJobNotFound("POSTPROCESS_JOB_NOT_FOUND", "Post-process job was not found.")
        path = _postprocess_job_directory(self._root(canonical_project_id), canonical_scene_id, create=False) / f"{job_id}.json"
        if not path.is_file() or path.is_symlink():
            raise PostprocessJobNotFound("POSTPROCESS_JOB_NOT_FOUND", "Post-process job was not found.")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RenderProductionError("POSTPROCESS_JOB_INVALID", "The post-process job record could not be read.") from error
        if not isinstance(document, Mapping):
            raise RenderProductionError("POSTPROCESS_JOB_INVALID", "The post-process job record is invalid.")
        return sanitize_postprocess_job_record_for_presentation(dict(document))

    def list_scene(self, project_id: object, scene_id: object) -> list[dict[str, object]]:
        canonical_project_id = validate_project_id(project_id)
        canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
        directory = _postprocess_job_directory(self._root(canonical_project_id), canonical_scene_id, create=False)
        if not directory.is_dir():
            return []
        records: list[dict[str, object]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.is_symlink() or not POSTPROCESS_JOB_FILENAME_PATTERN.fullmatch(path.name):
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(document, Mapping):
                records.append(sanitize_postprocess_job_record_for_presentation(dict(document)))
        return sorted(records, key=lambda item: str(item.get("created_at", "")))


def _active_postprocess_job(records: list[Mapping[str, object]]) -> Mapping[str, object] | None:
    for record in reversed(records):
        if record.get("state") in POSTPROCESS_ACTIVE_STATES:
            return record
    return None


# ---------------------------------------------------------------------------
# Media adapter: probe + authoritative audio remux
# ---------------------------------------------------------------------------

class ProductionMediaAdapter:
    """FFprobe validation plus lossless authoritative-audio remux."""

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        timeout_seconds: int = 300,
    ):
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        self.probe_adapter = MediaProbeAdapter(ffprobe_path=ffprobe_path, runner=runner, timeout_seconds=min(timeout_seconds, 120))
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> dict[str, object]:
        return self.probe_adapter.probe(path)

    def remux_authoritative_audio(self, upscaled_video: Path, final_scene_video: Path, candidate: Path) -> list[str]:
        """Stream-copy the upscaled picture with the Final Scene's audio.

        The picture stream is copied without re-encode; the authoritative
        source-song scene audio restored by Phase 8C is copied without
        normalization, gain, or any creative processing.
        """

        if not self.ffmpeg_path:
            raise RenderFinalizationError("MEDIA_TOOL_MISSING", "FFmpeg is not available for production remux.")
        arguments = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(upscaled_video),
            "-i", str(final_scene_video),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "copy",
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
            raise RenderFinalizationError("PRODUCTION_REMUX_TIMEOUT", "Production remux timed out.") from error
        except (FileNotFoundError, OSError) as error:
            raise RenderFinalizationError("MEDIA_TOOL_UNAVAILABLE", "FFmpeg could not be started.") from error
        if getattr(completed, "returncode", 1) != 0:
            stderr = (getattr(completed, "stderr", "") or "")[:500]
            raise RenderFinalizationError("PRODUCTION_REMUX_FAILED", "Production remux failed.", details={"stderr": stderr})
        return arguments


# ---------------------------------------------------------------------------
# Candidate validation + atomic promotion
# ---------------------------------------------------------------------------

def validate_postprocess_candidate(
    probe: Mapping[str, object],
    *,
    method: str,
    expected_dimensions: tuple[int, int],
    target_duration_ms: int,
    expected_frame_count: int | None,
    expected_audio_sample_rate: int | None = None,
    expected_audio_channels: int | None = None,
) -> dict[str, object]:
    """Spatial-only validation: 24 FPS, exact timing, method resolution, audio."""

    canonical = validate_production_method(method)
    video = probe.get("video")
    audio = probe.get("audio")
    if not isinstance(video, Mapping):
        raise RenderProductionError("PRODUCTION_VIDEO_MISSING", "The production candidate has no video stream.")
    if not isinstance(audio, Mapping):
        raise RenderProductionError("PRODUCTION_AUDIO_MISSING", "The production candidate has no authoritative audio stream.")
    numerator = video.get("fps_numerator")
    denominator = video.get("fps_denominator")
    if numerator != H3_FPS or denominator != 1:
        raise RenderProductionError("PRODUCTION_FPS_INVALID", "The production candidate must remain 24 FPS.")
    width = video.get("width")
    height = video.get("height")
    expected_width, expected_height = expected_dimensions
    if width != expected_width or height != expected_height:
        raise RenderProductionError(
            "PRODUCTION_RESOLUTION_INVALID",
            "The production candidate does not match the selected method's resolution contract.",
            details={"expected": [expected_width, expected_height], "actual": [width, height]},
        )
    duration_ms = video.get("duration_ms")
    tolerance_ms = _video_tolerance_ms()
    if not isinstance(duration_ms, int) or abs(duration_ms - target_duration_ms) > tolerance_ms:
        raise RenderProductionError(
            "PRODUCTION_DURATION_INVALID",
            "The production candidate does not preserve the authoritative scene duration.",
            details={"duration_ms": duration_ms, "target_duration_ms": target_duration_ms, "tolerance_ms": tolerance_ms},
        )
    frame_count = video.get("frame_count")
    if expected_frame_count is not None:
        if not isinstance(frame_count, int) or frame_count != expected_frame_count:
            raise RenderProductionError(
                "PRODUCTION_FRAME_COUNT_INVALID",
                "The production candidate changed the authoritative frame count.",
                details={"frame_count": frame_count, "expected_frame_count": expected_frame_count},
            )
    audio_duration = audio.get("duration_ms")
    audio_tolerance = _audio_tolerance_ms(expected_audio_sample_rate, final=True) if expected_audio_sample_rate else _video_tolerance_ms()
    if not isinstance(audio_duration, int) or abs(audio_duration - target_duration_ms) > audio_tolerance:
        raise RenderProductionError(
            "PRODUCTION_AUDIO_DURATION_INVALID",
            "The production candidate audio does not preserve the authoritative duration.",
            details={"audio_duration_ms": audio_duration, "target_duration_ms": target_duration_ms},
        )
    if expected_audio_sample_rate is not None and audio.get("sample_rate") != expected_audio_sample_rate:
        raise RenderProductionError("PRODUCTION_AUDIO_RATE_INVALID", "The production candidate audio sample rate changed.")
    if expected_audio_channels is not None and audio.get("channels") != expected_audio_channels:
        raise RenderProductionError("PRODUCTION_AUDIO_CHANNELS_INVALID", "The production candidate audio channels changed.")
    if isinstance(audio.get("codec_name"), str) and audio["codec_name"] != "aac":
        raise RenderProductionError("PRODUCTION_AUDIO_CODEC_INVALID", "The production candidate audio is not the authoritative AAC stream.")
    return {
        "method": canonical,
        "width": width,
        "height": height,
        "fps_numerator": numerator,
        "fps_denominator": denominator,
        "duration_ms": duration_ms,
        "frame_count": frame_count,
        "audio_duration_ms": audio_duration,
    }


def _write_production_current(root: Path, scene_id: str, document: Mapping[str, object]) -> dict[str, object]:
    path = _production_current_path(root, scene_id)
    try:
        atomic_write_json(path, dict(document))
    except OSError as error:
        raise ProjectPersistenceError("Could not persist the production current record atomically.") from error
    return dict(document)


def promote_production_scene(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    method: str,
    fingerprint: str,
    postprocess_job_id: str,
    candidate_path: Path,
    relative_path: str,
    validation: Mapping[str, object],
) -> dict[str, object]:
    """Atomically promote a validated candidate to the canonical Production Scene.

    A failed replacement preserves the previous valid Production Scene record.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    canonical = validate_production_method(method)
    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    canonical_path = root / relative_path
    if canonical_path.is_symlink():
        raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The production scene output path is unsafe.")
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = canonical_path.with_name(f".{canonical_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with candidate_path.open("rb") as source, temporary_path.open("xb") as destination:
            shutil.copyfileobj(source, destination, 1024 * 1024)
            destination.flush()
            if hasattr(os, "fsync"):
                os.fsync(destination.fileno())
        os.replace(temporary_path, canonical_path)
    except OSError as error:
        try:
            temporary_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        raise RenderProductionError("PRODUCTION_PROMOTION_FAILED", "The production scene candidate could not be promoted safely.") from error
    size = canonical_path.stat().st_size
    sha256 = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
    previous = _read_production_current(root, canonical_scene_id)
    document = {
        "production_schema_version": PRODUCTION_SCHEMA_VERSION,
        "project_id": canonical_project_id,
        "scene_id": canonical_scene_id,
        "method": canonical,
        "state": "FINALIZED",
        "fingerprint": fingerprint,
        "postprocess_job_id": postprocess_job_id,
        "updated_at": _timestamp(),
        "output": {
            "relative_path": relative_path,
            "identity": {"relative_path": relative_path, "size": size, "sha256": sha256},
            "format": "mp4",
        },
        "validation": dict(validation),
        "previous_valid_output": previous.get("output") if isinstance(previous, Mapping) and previous.get("method") == canonical else None,
        "failure": None,
    }
    return _write_production_current(root, canonical_scene_id, document)


def record_production_failure(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    method: str,
    fingerprint: str,
    postprocess_job_id: str,
    failure: Mapping[str, object],
) -> dict[str, object]:
    """Record a post-processing failure without touching Raw H3/Final Scene.

    The previous valid Production Scene (if any) remains current.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    canonical = validate_production_method(method)
    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    previous = _read_production_current(root, canonical_scene_id)
    document = {
        "production_schema_version": PRODUCTION_SCHEMA_VERSION,
        "project_id": canonical_project_id,
        "scene_id": canonical_scene_id,
        "method": canonical,
        "state": "FAILED",
        "fingerprint": fingerprint,
        "postprocess_job_id": postprocess_job_id,
        "updated_at": _timestamp(),
        "output": previous.get("output") if isinstance(previous, Mapping) and previous.get("method") == canonical else None,
        "validation": None,
        "previous_valid_output": previous.get("output") if isinstance(previous, Mapping) and previous.get("method") == canonical else None,
        "failure": dict(failure),
    }
    return _write_production_current(root, canonical_scene_id, document)


# ---------------------------------------------------------------------------
# Execution (ComfyUI-backed methods), reusing the central client
# ---------------------------------------------------------------------------

def _probe_node_types(client: object | None, required_nodes: list[str]) -> set[str] | None:
    if client is None:
        return None
    if hasattr(client, "get_object_info") and callable(client.get_object_info):
        probed: set[str] = set()
        for nt in required_nodes:
            try:
                info = client.get_object_info(nt)
                if isinstance(info, dict) and info:
                    probed.add(nt)
            except Exception:
                pass
        return probed
    if hasattr(client, "node_types") and isinstance(getattr(client, "node_types"), (set, list, tuple)):
        return set(getattr(client, "node_types"))
    return set(required_nodes)


def submit_postprocess_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    method: str,
    client: ComfyUIClient | None = None,
    final_scene_selector: str,
    output_prefix: str,
    node_types: set[str] | None = None,
    model_status: Mapping[str, str] | None = None,
    hardware_supported: bool | None = None,
    raw_output_root: Path | None = None,
    preflight: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Create and submit one post-process job through the central ComfyUI client.

    At most one new H3/upscale submission happens per call; there is no retry
    loop here.  Availability is enforced truthfully before any submission.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    canonical = validate_production_method(method)
    if canonical == POSTPROCESS_METHOD_NONE:
        raise RenderProductionError("POSTPROCESS_JOB_NOT_APPLICABLE", "The None method performs no post-processing.")
    if node_types is None and client is not None:
        node_types = _probe_node_types(client, method_runtime_requirements(canonical)["node_types"])
    availability = production_method_availability(
        canonical,
        node_types=node_types,
        model_status=model_status,
        hardware_supported=hardware_supported,
    )
    if availability["state"] not in {METHOD_AVAILABLE, METHOD_UNQUALIFIED}:
        raise ProductionMethodUnavailable(
            "POSTPROCESS_METHOD_UNAVAILABLE",
            f"The selected production method is not available in this runtime.",
            details=availability,
        )
    store = PostprocessJobStore(storage)
    existing = store.list_scene(canonical_project_id, canonical_scene_id)
    active = _active_postprocess_job(existing)
    if active is not None:
        raise PostprocessJobConflict(
            "POSTPROCESS_JOB_ACTIVE",
            "This scene already has an active post-process job.",
            details={"postprocess_job_id": active.get("postprocess_job_id")},
        )
    summary = summarize_production_scene(
        storage,
        canonical_project_id,
        canonical_scene_id,
        method=canonical,
        raw_output_root=raw_output_root,
        preflight=preflight,
    )
    if summary["final_scene_current"] is not True:
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "A current Final Scene is required before post-processing.")
    fingerprint = str(summary["fingerprint"])
    comfy = client or ComfyUIClient()
    workflow = compile_postprocess_workflow(canonical, final_scene_selector=final_scene_selector, output_prefix=output_prefix)
    record = new_postprocess_job_record(canonical_project_id, canonical_scene_id, canonical, production_fingerprint=fingerprint)
    record = transition_postprocess_job(record, POSTPROCESS_SUBMITTING, reason="submission_started")
    store.save(record)
    try:
        response = comfy.submit_prompt(workflow)
    except ComfyUIClientError as error:
        failed = transition_postprocess_job(
            record,
            POSTPROCESS_FAILED,
            reason="submission_failed",
            updates={"failure": {"category": "postprocess_submission_failure", "code": error.code, "message": error.message, "transient": error.transient}},
        )
        store.save(failed)
        raise RenderProductionError(error.code, error.message, details={"postprocess_job_id": record["postprocess_job_id"]}) from error
    prompt_id = response.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        failed = transition_postprocess_job(
            record,
            POSTPROCESS_FAILED,
            reason="submission_response_invalid",
            updates={"failure": {"category": "postprocess_submission_failure", "code": "COMFYUI_RESPONSE_INVALID", "message": "ComfyUI returned an invalid submission response."}},
        )
        store.save(failed)
        raise RenderProductionError("COMFYUI_RESPONSE_INVALID", "ComfyUI returned an invalid submission response.")
    submitted = transition_postprocess_job(
        record,
        POSTPROCESS_SUBMITTED,
        reason="comfyui_accepted_prompt",
        updates={"comfy_prompt_id": prompt_id, "submitted_at": _timestamp(), "output_prefix": output_prefix},
    )
    return store.save(submitted)


def reconcile_postprocess_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    job_id: object,
    *,
    client: ComfyUIClient | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Reconcile one post-process job against ComfyUI queue/history.

    Output discovery uses only the deterministic job-owned prefix; ambiguity
    fails closed.  Foreign ComfyUI prompts are never adopted.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    store = PostprocessJobStore(storage)
    record = store.load(canonical_project_id, canonical_scene_id, job_id)
    if record.get("state") in POSTPROCESS_TERMINAL_STATES:
        return record
    prompt_id = record.get("comfy_prompt_id")
    if not isinstance(prompt_id, str):
        return store.save(transition_postprocess_job(
            record,
            POSTPROCESS_FAILED,
            reason="prompt_id_missing",
            updates={"failure": {"category": "postprocess_reconciliation_failure", "code": "PROMPT_ID_MISSING", "message": "The post-process job has no ComfyUI prompt ID."}},
        ))
    comfy = client or ComfyUIClient()
    queue = comfy.get_queue()
    pending_ids = _prompt_ids_in_queue(queue, "queue_pending")
    running_ids = _prompt_ids_in_queue(queue, "queue_running")
    try:
        history = comfy.get_history(prompt_id)
    except ComfyUIClientError as error:
        if error.details.get("http_status") != 404:
            return record
        history = {}
    from .render_jobs import _history_entry
    entry = _history_entry(history, prompt_id) if isinstance(history, Mapping) else None
    if isinstance(entry, Mapping):
        status = entry.get("status")
        status_str = status.get("status_str") if isinstance(status, Mapping) else None
        if isinstance(status_str, str):
            state_map = {
                "success": POSTPROCESS_SUCCEEDED,
                "error": POSTPROCESS_FAILED,
                "interrupted": POSTPROCESS_CANCELLED,
                "cancelled": POSTPROCESS_CANCELLED,
                "canceled": POSTPROCESS_CANCELLED,
            }
            terminal = state_map.get(status_str.casefold())
            if terminal is not None:
                updates: dict[str, object] = {}
                if terminal == POSTPROCESS_SUCCEEDED:
                    manifest = production_postprocess_manifest_registry()[str(record.get("method"))]
                    owned_prefix = record.get("output_prefix")
                    try:
                        output = discover_raw_output(
                            entry,
                            prompt_id=prompt_id,
                            output_node_id=str(manifest["output_node_id"]),
                            expected_filename_prefix=str(owned_prefix) if isinstance(owned_prefix, str) and owned_prefix else None,
                            output_root=output_root,
                        )
                    except RenderOutputDiscoveryError as error:
                        updates["output"] = None
                        updates["failure"] = {"category": "postprocess_output_discovery_failure", "code": error.code, "message": error.message}
                        terminal = POSTPROCESS_FAILED
                    else:
                        updates["output"] = output
                elif terminal == POSTPROCESS_FAILED:
                    updates["failure"] = {"category": "postprocess_execution_failure", "code": "COMFYUI_EXECUTION_FAILED", "message": "ComfyUI reported a post-processing execution failure."}
                return store.save(transition_postprocess_job(record, terminal, reason="history_terminal_result", updates=updates))
    if prompt_id in running_ids:
        return store.save(transition_postprocess_job(record, POSTPROCESS_ACTIVE, reason="queue_running"))
    if prompt_id in pending_ids:
        return store.save(transition_postprocess_job(record, POSTPROCESS_SUBMITTED, reason="queue_pending"))
    return record


def _prompt_ids_in_queue(queue: Mapping[str, object], key: str) -> set[str]:
    values = queue.get(key)
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for item in values:
        candidate = None
        if isinstance(item, Mapping):
            candidate = item.get("prompt_id")
        elif isinstance(item, (list, tuple)) and len(item) > 1:
            candidate = item[1]
        if isinstance(candidate, str) and candidate:
            result.add(candidate)
    return result


# ---------------------------------------------------------------------------
# Production Planning (minimum-work semantics)
# ---------------------------------------------------------------------------

def plan_production_scene(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    method: str | None = None,
    preflight: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Authoritative minimum-work action plan for one scene to reach Production Scene READY.

    When method is None, resolves the project's production upscale setting.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    project = storage.load_project(canonical_project_id)
    resolved_method = resolve_project_production_method(project) if method is None else validate_production_method(method)

    summary = summarize_production_scene(
        storage,
        canonical_project_id,
        canonical_scene_id,
        method=resolved_method,
        preflight=preflight,
    )

    from .render_jobs import JobStore
    from .render_batch import (
        ACTION_ALREADY_COMPLETE as PHASE8D_COMPLETE,
        ACTION_BLOCKED as PHASE8D_BLOCKED,
        ACTION_FINALIZE_RAW as PHASE8D_FINALIZE,
        ACTION_PREPARE_AND_RENDER as PHASE8D_PREPARE,
        ACTION_RENDER_PREPARED as PHASE8D_RENDER,
        plan_scene_action,
    )
    from .render import build_render_preflight

    if preflight is None:
        try:
            preflight = build_render_preflight(project, storage)
        except Exception:
            preflight = {}

    preflight_scenes = preflight.get("scenes") if isinstance(preflight, Mapping) and isinstance(preflight.get("scenes"), list) else []
    scene_preflight = next((entry for entry in preflight_scenes if isinstance(entry, Mapping) and entry.get("scene_id") == canonical_scene_id), {})
    scene_jobs = JobStore(storage).list_scene(canonical_project_id, canonical_scene_id)

    finalization_by_job_id = {}
    for job in scene_jobs:
        job_id = str(job.get("job_id"))
        if isinstance(job.get("finalization"), Mapping):
            finalization_by_job_id[job_id] = job["finalization"]

    upstream_action, blocker = plan_scene_action(
        scene_preflight,
        scene_jobs,
        finalization_by_job_id=finalization_by_job_id or None,
    )

    blockers = [blocker] if blocker else []

    if upstream_action == PHASE8D_BLOCKED:
        return {
            "project_id": canonical_project_id,
            "scene_id": canonical_scene_id,
            "method": resolved_method,
            "action": ACTION_PRODUCTION_BLOCKED,
            "summary": summary,
            "blockers": blockers,
        }
    if upstream_action == PHASE8D_PREPARE:
        return {
            "project_id": canonical_project_id,
            "scene_id": canonical_scene_id,
            "method": resolved_method,
            "action": ACTION_PREPARE_RENDER_FINALIZE_POSTPROCESS,
            "summary": summary,
            "blockers": [],
        }
    if upstream_action == PHASE8D_RENDER:
        return {
            "project_id": canonical_project_id,
            "scene_id": canonical_scene_id,
            "method": resolved_method,
            "action": ACTION_RENDER_FINALIZE_POSTPROCESS,
            "summary": summary,
            "blockers": [],
        }
    if upstream_action == PHASE8D_FINALIZE:
        return {
            "project_id": canonical_project_id,
            "scene_id": canonical_scene_id,
            "method": resolved_method,
            "action": ACTION_FINALIZE_AND_POSTPROCESS,
            "summary": summary,
            "blockers": [],
        }
    # Upstream Final Scene is current
    if summary.get("state") == PRODUCTION_CURRENT:
        return {
            "project_id": canonical_project_id,
            "scene_id": canonical_scene_id,
            "method": resolved_method,
            "action": ACTION_PRODUCTION_COMPLETE,
            "summary": summary,
            "blockers": [],
        }
    return {
        "project_id": canonical_project_id,
        "scene_id": canonical_scene_id,
        "method": resolved_method,
        "action": ACTION_POSTPROCESS_ONLY,
        "summary": summary,
        "blockers": [],
    }


# ---------------------------------------------------------------------------
# Input Materialization & Frame Safety
# ---------------------------------------------------------------------------

def materialize_final_scene_for_postprocess(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    input_root: Path | None = None,
    raw_output_root: Path | None = None,
    preflight: Mapping[str, object] | None = None,
) -> tuple[str, Path]:
    """Atomically stage the current Final Scene in the ComfyUI input directory.

    Returns (relative_selector, staged_path).
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    if input_root is None:
        try:
            import folder_paths  # type: ignore
            resolved_input_root = Path(folder_paths.get_input_directory())
        except Exception:
            resolved_input_root = storage.project_directory(canonical_project_id) / "input"
    else:
        resolved_input_root = input_root
    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    summary = summarize_production_scene(
        storage,
        canonical_project_id,
        canonical_scene_id,
        method=POSTPROCESS_METHOD_NONE,
        raw_output_root=raw_output_root,
        preflight=preflight,
    )
    if summary.get("state") != PRODUCTION_CURRENT or not isinstance(summary.get("output"), Mapping):
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "A current Final Scene is required before post-processing.")
    output = summary["output"]
    relative_path = output.get("relative_path")
    if not isinstance(relative_path, str):
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "Final Scene output path is invalid.")
    final_scene_path = root / relative_path
    if not final_scene_path.is_file() or final_scene_path.is_symlink():
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "Final Scene output file does not exist.")

    selector = f"music_video_builder/{canonical_project_id}/{canonical_scene_id}/final_scene.mp4"
    staged_path = resolved_input_root / "music_video_builder" / canonical_project_id / canonical_scene_id / "final_scene.mp4"
    if staged_path.is_symlink():
        raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "The staged input path is a symlink.")
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = staged_path.with_name(f".{staged_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with final_scene_path.open("rb") as source, temporary_path.open("wb") as destination:
            shutil.copyfileobj(source, destination, 1024 * 1024)
            destination.flush()
            if hasattr(os, "fsync"):
                os.fsync(destination.fileno())
        os.replace(temporary_path, staged_path)
    except OSError as error:
        try:
            temporary_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        raise RenderProductionError("POSTPROCESS_MATERIALIZATION_FAILED", "Could not materialize the Final Scene input.") from error
    return selector, staged_path


def extract_video_frames_for_postprocess(
    video_path: Path,
    output_directory: Path,
    *,
    ffmpeg_path: str | None = None,
    runner: Callable[..., object] = subprocess.run,
    fps: int = H3_FPS,
) -> list[Path]:
    """Deterministic 24 FPS frame extraction into an isolated temporary workspace."""

    if not video_path.is_file() or video_path.is_symlink():
        raise RenderProductionError("POSTPROCESS_INPUT_INVALID", "Video source for frame extraction does not exist.")
    if output_directory.is_symlink():
        raise RenderProductionError("PRODUCTION_PATH_UNSAFE", "Temporary frame directory is a symlink.")
    output_directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise RenderFinalizationError("MEDIA_TOOL_MISSING", "FFmpeg is not available for frame extraction.")
    pattern = output_directory / "%06d.png"
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        str(pattern),
    ]
    try:
        completed = runner(
            arguments,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise RenderProductionError("POSTPROCESS_FRAME_EXTRACTION_TIMEOUT", "Frame extraction timed out.") from error
    except OSError as error:
        raise RenderFinalizationError("MEDIA_TOOL_UNAVAILABLE", "FFmpeg could not be started.") from error
    if getattr(completed, "returncode", 1) != 0:
        stderr = (getattr(completed, "stderr", "") or "")[:500]
        raise RenderProductionError("POSTPROCESS_FRAME_EXTRACTION_FAILED", "Frame extraction failed.", details={"stderr": stderr})
    frames = sorted(
        [p for p in output_directory.iterdir() if p.is_file() and p.suffix.lower() == ".png" and not p.is_symlink()],
        key=lambda p: p.name,
    )
    if not frames:
        raise RenderProductionError("POSTPROCESS_FRAME_EXTRACTION_FAILED", "No frames were extracted.")
    return frames


# ---------------------------------------------------------------------------
# Postprocess Job Finalization (Remux + Validate + Atomically Promote)
# ---------------------------------------------------------------------------

def finalize_postprocess_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    job_id: object,
    *,
    media_adapter: ProductionMediaAdapter | None = None,
    preflight: Mapping[str, object] | None = None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    """Finalize a SUCCEEDED post-process job: remux authoritative audio, validate candidate, and atomically promote.

    Upscale failures are isolated to Production Scene; Raw H3 and Final Scene are never modified.
    """

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    store = PostprocessJobStore(storage)
    record = store.load(canonical_project_id, canonical_scene_id, job_id)
    if record.get("state") != POSTPROCESS_SUCCEEDED:
        raise RenderProductionError("POSTPROCESS_JOB_NOT_READY", "Only succeeded post-process jobs can be finalized.")
    method = str(record.get("method"))
    fingerprint = str(record.get("production_fingerprint"))
    output = record.get("output")
    if not isinstance(output, Mapping):
        raise RenderProductionError("POSTPROCESS_OUTPUT_MISSING", "The succeeded post-process job has no discovered output.")
    raw_video_path = None
    if isinstance(output.get("local_path"), str) and Path(str(output.get("local_path"))).is_file():
        raw_video_path = Path(str(output["local_path"]))
    elif isinstance(output.get("relative_path"), str):
        try:
            from .render_jobs import _default_comfy_output_root
            output_root = raw_output_root or _default_comfy_output_root()
        except Exception:
            output_root = raw_output_root
        if output_root is not None:
            raw_video_path = output_root / str(output["relative_path"])
    if raw_video_path is None or not raw_video_path.is_file():
        raise RenderProductionError("POSTPROCESS_OUTPUT_MISSING", "The upscaled output file does not exist.")

    root = storage.project_directory(canonical_project_id).resolve(strict=True)
    summary, final_job = _current_final_scene(
        storage,
        canonical_project_id,
        canonical_scene_id,
        preflight=preflight,
        raw_output_root=raw_output_root,
    )
    if not isinstance(summary, Mapping) or summary.get("state") != FINALIZATION_STATE_FINALIZED or not isinstance(summary.get("output"), Mapping):
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "The Final Scene is not current for finalization.")
    final_output = summary["output"]
    final_relative = final_output.get("relative_path")
    if not isinstance(final_relative, str):
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "Final Scene output path is missing.")
    final_scene_path = root / final_relative
    if not final_scene_path.is_file():
        raise ProductionNotReady("POSTPROCESS_SOURCE_NOT_READY", "Final Scene output file is missing.")

    adapter = media_adapter or ProductionMediaAdapter()
    final_probe = adapter.probe(final_scene_path)
    final_video = final_probe.get("video", {})
    final_audio = final_probe.get("audio", {})
    source_width = final_video.get("width")
    source_height = final_video.get("height")
    target_duration_ms = final_video.get("duration_ms")
    expected_frame_count = final_video.get("frame_count")
    expected_sample_rate = final_audio.get("sample_rate")
    expected_channels = final_audio.get("channels")

    if not isinstance(source_width, int) or not isinstance(source_height, int) or not isinstance(target_duration_ms, int):
        raise RenderProductionError("PRODUCTION_SOURCE_PROBE_FAILED", "Could not probe Final Scene properties.")

    expected_dims = production_target_dimensions(method, source_width, source_height)
    if expected_dims is None:
        expected_dims = (source_width, source_height)

    # Remux candidate
    production_dir = _production_directory(root, canonical_scene_id)
    candidate_path = production_dir / f".candidate.{uuid.uuid4().hex}.mp4"
    try:
        adapter.remux_authoritative_audio(raw_video_path, final_scene_path, candidate_path)
        candidate_probe = adapter.probe(candidate_path)
        validation = validate_postprocess_candidate(
            candidate_probe,
            method=method,
            expected_dimensions=expected_dims,
            target_duration_ms=target_duration_ms,
            expected_frame_count=expected_frame_count,
            expected_audio_sample_rate=expected_sample_rate,
            expected_audio_channels=expected_channels,
        )
        canonical_relative = f"renders/{canonical_scene_id}/{PRODUCTION_DIRECTORY}/production_{method}.mp4"
        promoted = promote_production_scene(
            storage,
            canonical_project_id,
            canonical_scene_id,
            method=method,
            fingerprint=fingerprint,
            postprocess_job_id=str(record["postprocess_job_id"]),
            candidate_path=candidate_path,
            relative_path=canonical_relative,
            validation=validation,
        )
        return promoted
    except (RenderProductionError, RenderFinalizationError) as error:
        failure = {
            "category": "postprocess_finalization_failure",
            "code": getattr(error, "code", "FINALIZATION_FAILED"),
            "message": str(error),
        }
        record_production_failure(
            storage,
            canonical_project_id,
            canonical_scene_id,
            method=method,
            fingerprint=fingerprint,
            postprocess_job_id=str(record["postprocess_job_id"]),
            failure=failure,
        )
        if isinstance(error, RenderProductionError):
            raise
        raise RenderProductionError(getattr(error, "code", "FINALIZATION_FAILED"), str(error)) from error
    finally:
        try:
            if candidate_path.exists():
                candidate_path.unlink()
        except OSError:
            pass


def cancel_postprocess_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    job_id: object | None = None,
    *,
    client: ComfyUIClient | None = None,
) -> dict[str, object]:
    """Cancel an active post-process job. Preserves Raw H3 and Final Scene intact."""

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    store = PostprocessJobStore(storage)
    if job_id is not None:
        record = store.load(canonical_project_id, canonical_scene_id, job_id)
    else:
        existing = store.list_scene(canonical_project_id, canonical_scene_id)
        record = _active_postprocess_job(existing)
        if record is None:
            raise RenderProductionError("POSTPROCESS_JOB_NOT_ACTIVE", "No active post-process job found to cancel.")

    current_state = record.get("state")
    if current_state not in POSTPROCESS_ACTIVE_STATES:
        return record

    prompt_id = record.get("comfy_prompt_id")
    comfy = client or ComfyUIClient()
    if isinstance(prompt_id, str) and prompt_id:
        try:
            comfy.interrupt(prompt_id)
        except Exception:
            LOGGER.warning("ComfyUI interrupt failed for postprocess prompt_id=%s", prompt_id)

    cancelled = transition_postprocess_job(
        record,
        POSTPROCESS_CANCELLED,
        reason="user_cancelled",
        updates={"cancelled_at": _timestamp()},
    )
    return store.save(cancelled)


def retry_postprocess_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    client: ComfyUIClient | None = None,
    final_scene_selector: str,
    output_prefix: str,
    node_types: set[str] | None = None,
    model_status: Mapping[str, str] | None = None,
    hardware_supported: bool | None = None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    """Retry post-processing for a scene without rerendering H3 or refinalizing Final Scene."""

    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    project = storage.load_project(canonical_project_id)
    method = resolve_project_production_method(project)
    if method == POSTPROCESS_METHOD_NONE:
        raise RenderProductionError("POSTPROCESS_JOB_NOT_APPLICABLE", "The None method performs no post-processing.")
    return submit_postprocess_job(
        storage,
        canonical_project_id,
        canonical_scene_id,
        method=method,
        client=client,
        final_scene_selector=final_scene_selector,
        output_prefix=output_prefix,
        node_types=node_types,
        model_status=model_status,
        hardware_supported=hardware_supported,
        raw_output_root=raw_output_root,
    )


def set_project_production_method(
    storage: ProjectStorage,
    project_id: object,
    method: object,
) -> dict[str, object]:
    """Persist the project production upscale method, rejecting mutations if a batch is active."""

    canonical_project_id = validate_project_id(project_id)
    canonical_method = validate_production_method(method)

    from .render_batch import BatchStore, OPEN_BATCH_STATES
    batch = BatchStore(storage).load_active(canonical_project_id)
    if batch is not None and batch.get("state") in OPEN_BATCH_STATES:
        raise RenderProductionError(
            "PROJECT_BATCH_ACTIVE_MUTATION_BLOCKED",
            "Cannot change project upscale method while a batch is active.",
        )

    project = storage.load_project(canonical_project_id)
    current_production = project.get("production") if isinstance(project.get("production"), Mapping) else {}
    updated_production = {**current_production, "upscale_method": canonical_method}
    updated_project = {**project, "production": updated_production}
    saved = storage.save_project(canonical_project_id, updated_project)
    return {"project_id": canonical_project_id, "production": saved.get("production")}


def get_project_production_status(
    storage: ProjectStorage,
    project_id: object,
    *,
    node_types: set[str] | None = None,
    model_status: Mapping[str, str] | None = None,
    hardware_supported: bool | None = None,
) -> dict[str, object]:
    """Aggregate project production settings, runtime method capabilities, and per-scene summaries."""

    canonical_project_id = validate_project_id(project_id)
    project = storage.load_project(canonical_project_id)
    resolved_method = resolve_project_production_method(project)

    if node_types is None:
        try:
            from .requirements import _load_active_node_types
            active_nodes, _ = _load_active_node_types()
            if active_nodes is not None:
                node_types = active_nodes
        except Exception:
            pass

    capabilities = {}
    for m in PRODUCTION_METHODS:
        capabilities[m] = production_method_availability(
            m,
            node_types=node_types,
            model_status=model_status,
            hardware_supported=hardware_supported,
        )

    scenes = project.get("scenes") if isinstance(project.get("scenes"), list) else []
    scene_summaries = []
    for scene in scenes:
        if isinstance(scene, Mapping) and isinstance(scene.get("scene_id"), str):
            summary = summarize_production_scene(storage, canonical_project_id, scene["scene_id"], method=resolved_method)
            scene_summaries.append(summary)

    return {
        "project_id": canonical_project_id,
        "upscale_method": resolved_method,
        "capabilities": capabilities,
        "scenes": scene_summaries,
    }
