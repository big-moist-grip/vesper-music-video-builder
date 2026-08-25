"""Phase 8D batch production orchestration.

The batch system orchestrates the existing authoritative per-scene services:
Phase 8A readiness/preparation, Phase 8B queue/lifecycle/reconciliation, and
Phase 8C finalization/recovery.  It never reimplements per-scene rendering.

The batch runner is backend-owned.  Exactly ONE Builder batch H3 render may be
active or submitted at a time; scenes are processed sequentially in canonical
project scene order.  The frontend observes and controls the batch (start,
pause after current, resume, end, retry failed) but never advances it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import re
import threading
import time
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
from .render import (
    RenderPreparationBlocked,
    RenderPreparationError,
    build_render_preflight,
    find_ffmpeg,
    prepare_render_scene,
)
from .render_finalize import (
    FINALIZATION_STATE_FAILED,
    FINALIZATION_STATE_FINALIZED,
    FINALIZATION_STATE_RAW_READY,
    RenderFinalizationError,
    enrich_render_jobs_with_finalization,
    finalize_render_job,
)
from .render_jobs import (
    ACTIVE_STATES,
    CANCELLED,
    FAILED,
    INTERRUPTED,
    ORPHANED,
    SUCCEEDED,
    ComfyUIClient,
    ComfyUIClientError,
    JobStore,
    RenderExecutionBlocked,
    is_raw_output_usable,
    RenderJobConflict,
    RenderJobError,
    RenderJobSubmissionError,
    reconcile_project_jobs,
    sanitize_user_facing_message,
    submit_render_job,
)
from .render_production import (
    POSTPROCESS_ACTIVE_STATES,
    POSTPROCESS_CANCELLED,
    POSTPROCESS_FAILED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_SUCCEEDED,
    PRODUCTION_CURRENT,
    PostprocessJobConflict,
    PostprocessJobNotFound,
    PostprocessJobStore,
    ProductionMethodUnavailable,
    ProductionNotReady,
    RenderProductionError,
    finalize_postprocess_job,
    materialize_final_scene_for_postprocess,
    production_method_availability,
    reconcile_postprocess_job,
    resolve_project_production_method,
    submit_postprocess_job,
    summarize_production_scene,
    validate_production_method,
    validate_production_settings,
)


LOGGER = logging.getLogger(__name__)

BATCH_SCHEMA_VERSION = 1
BATCH_DIRECTORY = "batches"
BATCH_ACTIVE_POINTER_FILENAME = "batch_active.json"
BATCH_FILENAME_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$"
)

# Top-level batch lifecycle states (truthful and deterministic)
BATCH_RUNNING = "RUNNING"
BATCH_PAUSE_REQUESTED = "PAUSE_REQUESTED"
BATCH_PAUSED = "PAUSED"
BATCH_PAUSED_RECOVERY = "PAUSED_RECOVERY"
BATCH_COMPLETED = "COMPLETED"
BATCH_COMPLETED_WITH_ISSUES = "COMPLETED_WITH_ISSUES"
BATCH_ENDED = "ENDED"

BATCH_STATES = frozenset({
    BATCH_RUNNING,
    BATCH_PAUSE_REQUESTED,
    BATCH_PAUSED,
    BATCH_PAUSED_RECOVERY,
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_ENDED,
})
OPEN_BATCH_STATES = frozenset({
    BATCH_RUNNING,
    BATCH_PAUSE_REQUESTED,
    BATCH_PAUSED,
    BATCH_PAUSED_RECOVERY,
})
EXECUTING_BATCH_STATES = frozenset({BATCH_RUNNING, BATCH_PAUSE_REQUESTED})
PAUSED_BATCH_STATES = frozenset({
    BATCH_PAUSE_REQUESTED,
    BATCH_PAUSED,
    BATCH_PAUSED_RECOVERY,
})
TERMINAL_BATCH_STATES = frozenset({BATCH_COMPLETED, BATCH_COMPLETED_WITH_ISSUES, BATCH_ENDED})

# Authoritative planner actions: the minimum work each scene needs to reach a
# current production scene output.
ACTION_ALREADY_COMPLETE = "ALREADY_COMPLETE"
ACTION_POSTPROCESS_ONLY = "POSTPROCESS_ONLY"
ACTION_FINALIZE_AND_POSTPROCESS = "FINALIZE_AND_POSTPROCESS"
ACTION_FINALIZE_RAW = "FINALIZE_RAW"
ACTION_RENDER_PREPARED = "RENDER_PREPARED"
ACTION_PREPARE_AND_RENDER = "PREPARE_AND_RENDER"
ACTION_BLOCKED = "BLOCKED"

BATCH_ACTIONS = frozenset({
    ACTION_ALREADY_COMPLETE,
    ACTION_POSTPROCESS_ONLY,
    ACTION_FINALIZE_AND_POSTPROCESS,
    ACTION_FINALIZE_RAW,
    ACTION_RENDER_PREPARED,
    ACTION_PREPARE_AND_RENDER,
    ACTION_BLOCKED,
})
ELIGIBLE_BATCH_ACTIONS = frozenset({
    ACTION_POSTPROCESS_ONLY,
    ACTION_FINALIZE_AND_POSTPROCESS,
    ACTION_FINALIZE_RAW,
    ACTION_RENDER_PREPARED,
    ACTION_PREPARE_AND_RENDER,
})

# Per-item runtime dispositions.
ITEM_PENDING = "PENDING"
ITEM_PREPARING = "PREPARING"
ITEM_RENDERING = "RENDERING"
ITEM_FINALIZING = "FINALIZING"
ITEM_POSTPROCESSING = "POSTPROCESSING"
ITEM_COMPLETE = "COMPLETE"
ITEM_ALREADY_COMPLETE = "ALREADY_COMPLETE"
ITEM_FAILED = "FAILED"
ITEM_FINALIZATION_FAILED = "FINALIZATION_FAILED"
ITEM_POSTPROCESS_FAILED = "POSTPROCESS_FAILED"
ITEM_STALE_AFTER_RENDER = "STALE_AFTER_RENDER"
ITEM_CANCELLED = "CANCELLED"
ITEM_SKIPPED = "SKIPPED"

ITEM_DISPOSITIONS = frozenset({
    ITEM_PENDING,
    ITEM_PREPARING,
    ITEM_RENDERING,
    ITEM_FINALIZING,
    ITEM_POSTPROCESSING,
    ITEM_COMPLETE,
    ITEM_ALREADY_COMPLETE,
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_POSTPROCESS_FAILED,
    ITEM_STALE_AFTER_RENDER,
    ITEM_CANCELLED,
    ITEM_SKIPPED,
})
ACTIVE_ITEM_DISPOSITIONS = frozenset({ITEM_PREPARING, ITEM_RENDERING, ITEM_FINALIZING, ITEM_POSTPROCESSING})
# "Processed" is deterministic: any item that reached a stable disposition.
STABLE_ITEM_DISPOSITIONS = frozenset({
    ITEM_COMPLETE,
    ITEM_ALREADY_COMPLETE,
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_POSTPROCESS_FAILED,
    ITEM_STALE_AFTER_RENDER,
    ITEM_CANCELLED,
    ITEM_SKIPPED,
})
COMPLETE_ITEM_DISPOSITIONS = frozenset({ITEM_COMPLETE, ITEM_ALREADY_COMPLETE})
FAILED_ITEM_DISPOSITIONS = frozenset({
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_POSTPROCESS_FAILED,
    ITEM_STALE_AFTER_RENDER,
})
ISSUE_ITEM_DISPOSITIONS = frozenset({
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_POSTPROCESS_FAILED,
    ITEM_STALE_AFTER_RENDER,
    ITEM_CANCELLED,
})
RETRYABLE_ITEM_DISPOSITIONS = frozenset({
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_POSTPROCESS_FAILED,
    ITEM_STALE_AFTER_RENDER,
    ITEM_CANCELLED,
})

RECONCILIATION_FAILURE_PAUSE_THRESHOLD = 15
BATCH_TICK_TRANSITION_BUDGET = 64
SYSTEMIC_COMFYUI_CODES = frozenset({
    "COMFYUI_UNAVAILABLE",
    "COMFYUI_TRANSPORT_FAILED",
    "COMFYUI_RESPONSE_INVALID",
})
SYSTEMIC_RUNTIME_NODE_CODES = frozenset({
    "LIVE_NODE_CLASS_MISSING",
    "LIVE_NODE_CLASS_INVALID",
    "LIVE_NODE_CLASS_MISMATCH",
})

_LOCKS_LOCK = threading.Lock()
_BATCH_LOCKS: dict[str, threading.RLock] = {}
_RUNNERS_LOCK = threading.Lock()
_RUNNERS: dict[str, "BatchRunner"] = {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _batch_project_lock(project_id: str) -> threading.RLock:
    key = f"batch:{project_id}"
    with _LOCKS_LOCK:
        lock = _BATCH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _BATCH_LOCKS[key] = lock
        return lock


class RenderBatchError(Exception):
    """Base class for product-facing batch orchestration failures."""

    status = 422

    def __init__(self, code: str, message: str, *, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class RenderBatchConflict(RenderBatchError):
    status = 409


class RenderBatchNotFound(RenderBatchError):
    status = 404


def _validate_batch_id(batch_id: object) -> str:
    if not isinstance(batch_id, str):
        raise ProjectValidationError("Batch ID must be a UUID string.")
    try:
        parsed = uuid.UUID(batch_id)
    except (ValueError, AttributeError) as error:
        raise ProjectValidationError("Batch ID must be a valid UUID.") from error
    canonical = str(parsed)
    if canonical != batch_id:
        raise ProjectValidationError("Batch ID must use canonical UUID form.")
    return canonical


def _validate_batch_item(item: object, *, project_id: str) -> dict[str, object]:
    if not isinstance(item, Mapping):
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch item record is invalid.")
    result = deepcopy(dict(item))
    scene_id = validate_entity_id(result.get("scene_id"), "Scene ID")
    sequence = result.get("sequence")
    action = result.get("action")
    disposition = result.get("disposition")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch item sequence is invalid.")
    if action not in BATCH_ACTIONS:
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch item action is invalid.")
    if disposition not in ITEM_DISPOSITIONS:
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch item disposition is invalid.")
    result["scene_id"] = scene_id
    result["project_id"] = project_id
    return result


def _validate_batch_record(record: Mapping[str, object], *, project_id: str | None = None) -> dict[str, object]:
    if not isinstance(record, Mapping):
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch record is invalid.")
    result = deepcopy(dict(record))
    if result.get("batch_schema_version") != BATCH_SCHEMA_VERSION:
        raise RenderBatchError("BATCH_RECORD_VERSION_UNSUPPORTED", "The durable batch record version is unsupported.")
    canonical_batch_id = _validate_batch_id(result.get("batch_id"))
    canonical_project_id = validate_project_id(result.get("project_id"))
    if project_id is not None and canonical_project_id != project_id:
        raise RenderBatchError("BATCH_OWNERSHIP_MISMATCH", "The batch does not belong to this project.")
    if result.get("state") not in BATCH_STATES:
        raise RenderBatchError("BATCH_STATE_INVALID", "The durable batch state is invalid.")
    mode = result.get("mode")
    if mode not in {"all_ready", "selected"}:
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch mode is invalid.")
    items = result.get("items")
    if not isinstance(items, list):
        raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch item list is invalid.")
    seen_scene_ids: set[str] = set()
    normalized_items: list[dict[str, object]] = []
    for item in items:
        normalized = _validate_batch_item(item, project_id=canonical_project_id)
        if normalized["scene_id"] in seen_scene_ids:
            raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch contains duplicate scenes.")
        seen_scene_ids.add(normalized["scene_id"])
        normalized_items.append(normalized)
    result["items"] = normalized_items
    result["batch_id"] = canonical_batch_id
    result["project_id"] = canonical_project_id
    if "production_profile" in result and isinstance(result["production_profile"], Mapping):
        result["production_profile"] = validate_production_settings(result["production_profile"])
    else:
        result["production_profile"] = {"upscale_method": "none"}
    return result


def _read_batch_json(path: Path) -> object | None:
    """Read a durable batch document tolerating brief Windows sharing delays."""

    attempts = 0
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, OSError):
            attempts += 1
            if attempts >= 12:
                return None
            time.sleep(0.005 * attempts)
        except (UnicodeError, json.JSONDecodeError):
            return None


def _batch_directory(storage: ProjectStorage, project_id: str) -> Path:
    root = storage.project_directory(project_id).resolve(strict=True)
    renders = root / "renders"
    if renders.exists() and renders.is_symlink():
        raise RenderBatchError("BATCH_PATH_UNSAFE", "The project render directory is a symlink.")
    batches = renders / BATCH_DIRECTORY
    if batches.exists() and batches.is_symlink():
        raise RenderBatchError("BATCH_PATH_UNSAFE", "The batch directory is a symlink.")
    batches.mkdir(parents=True, exist_ok=True)
    return batches


def _batch_path(storage: ProjectStorage, project_id: str, batch_id: str) -> Path:
    directory = _batch_directory(storage, project_id)
    path = directory / f"{batch_id}.json"
    if path.is_symlink():
        raise RenderBatchError("BATCH_PATH_UNSAFE", "The batch record is a symlink.")
    return path


def _active_pointer_path(storage: ProjectStorage, project_id: str) -> Path:
    directory = _batch_directory(storage, project_id)
    path = directory / BATCH_ACTIVE_POINTER_FILENAME
    if path.is_symlink():
        raise RenderBatchError("BATCH_PATH_UNSAFE", "The batch active pointer is a symlink.")
    return path


def sanitize_batch_record_for_presentation(record: Mapping[str, object]) -> dict[str, object]:
    batch = deepcopy(dict(record))
    items = batch.get("items")
    if isinstance(items, list):
        sanitized_items = []
        for it in items:
            if isinstance(it, Mapping):
                it_copy = deepcopy(dict(it))
                fail = it_copy.get("failure")
                if isinstance(fail, Mapping) and "message" in fail:
                    fail_copy = deepcopy(dict(fail))
                    fail_copy["message"] = sanitize_user_facing_message(fail_copy.get("message"), fallback="Batch item failed.")
                    it_copy["failure"] = fail_copy
                sanitized_items.append(it_copy)
        batch["items"] = sanitized_items
    attention = batch.get("attention")
    if isinstance(attention, Mapping) and "message" in attention:
        att_copy = deepcopy(dict(attention))
        att_copy["message"] = sanitize_user_facing_message(att_copy.get("message"), fallback="Batch paused.")
        batch["attention"] = att_copy
    return batch


class BatchStore:
    """Durable project-owned batch state outside schema-v7 project JSON."""

    def __init__(self, storage: ProjectStorage):
        self.storage = storage

    def save(self, record: Mapping[str, object], *, update_pointer: bool = True) -> dict[str, object]:
        normalized = _validate_batch_record(record)
        normalized["updated_at"] = _timestamp()
        path = _batch_path(self.storage, normalized["project_id"], normalized["batch_id"])
        try:
            atomic_write_json(path, normalized)
        except ProjectPersistenceError:
            raise
        except OSError as error:
            raise ProjectPersistenceError("Could not persist the batch record atomically.") from error
        if update_pointer:
            if normalized["state"] in OPEN_BATCH_STATES:
                self._write_pointer(normalized["project_id"], normalized["batch_id"])
            else:
                self._clear_pointer(normalized["project_id"], normalized["batch_id"])
        return deepcopy(normalized)

    def _write_pointer(self, project_id: str, batch_id: str) -> None:
        pointer_path = _active_pointer_path(self.storage, project_id)
        # The pointer changes only when batch ownership changes; skipping
        # unchanged rewrites keeps the hot path quiet and avoids needless
        # file replacement while readers observe the batch.
        if pointer_path.is_file() and not pointer_path.is_symlink():
            existing = _read_batch_json(pointer_path)
            if isinstance(existing, Mapping) and existing.get("batch_id") == batch_id:
                return
        try:
            atomic_write_json(pointer_path, {"batch_id": batch_id, "updated_at": _timestamp()})
        except OSError as error:
            raise ProjectPersistenceError("Could not persist the batch active pointer atomically.") from error

    def _clear_pointer(self, project_id: str, batch_id: str) -> None:
        pointer_path = _active_pointer_path(self.storage, project_id)
        if not pointer_path.is_file():
            return
        document = _read_batch_json(pointer_path)
        if isinstance(document, Mapping) and document.get("batch_id") == batch_id:
            attempts = 0
            while True:
                try:
                    pointer_path.unlink()
                    break
                except PermissionError:
                    attempts += 1
                    if attempts >= 12:
                        LOGGER.warning("Could not remove the stale batch active pointer.")
                        break
                    time.sleep(0.005 * attempts)
                except OSError:
                    LOGGER.warning("Could not remove the stale batch active pointer.")
                    break

    def load(self, project_id: object, batch_id: object) -> dict[str, object]:
        canonical_project_id = validate_project_id(project_id)
        canonical_batch_id = _validate_batch_id(batch_id)
        path = _batch_path(self.storage, canonical_project_id, canonical_batch_id)
        if not path.is_file():
            raise RenderBatchNotFound("BATCH_NOT_FOUND", "Batch was not found.")
        document = _read_batch_json(path)
        if not isinstance(document, Mapping):
            raise RenderBatchError("BATCH_RECORD_INVALID", "The durable batch record could not be read.")
        validated = _validate_batch_record(document, project_id=canonical_project_id)
        return sanitize_batch_record_for_presentation(validated)

    def load_active(self, project_id: object) -> dict[str, object] | None:
        canonical_project_id = validate_project_id(project_id)
        pointer_path = _active_pointer_path(self.storage, canonical_project_id)
        if not pointer_path.is_file() or pointer_path.is_symlink():
            return None
        document = _read_batch_json(pointer_path)
        batch_id = document.get("batch_id") if isinstance(document, Mapping) else None
        if not isinstance(batch_id, str):
            return None
        try:
            record = self.load(canonical_project_id, batch_id)
        except RenderBatchError:
            return None
        if record["state"] not in OPEN_BATCH_STATES:
            return None
        return record

    def list_project(self, project_id: object) -> list[dict[str, object]]:
        canonical_project_id = validate_project_id(project_id)
        directory = _batch_directory(self.storage, canonical_project_id)
        records: list[dict[str, object]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.is_symlink() or not BATCH_FILENAME_PATTERN.fullmatch(path.name):
                continue
            document = _read_batch_json(path)
            if not isinstance(document, Mapping):
                LOGGER.warning("Skipping an unreadable durable batch record: %s", path.name)
                continue
            try:
                validated = _validate_batch_record(document, project_id=canonical_project_id)
                records.append(sanitize_batch_record_for_presentation(validated))
            except (RenderBatchError, ProjectValidationError):
                LOGGER.warning("Skipping an unreadable durable batch record: %s", path.name)
        return sorted(records, key=lambda item: str(item.get("created_at", "")))


def new_batch_record(
    project_id: str,
    *,
    mode: str,
    items: list[dict[str, object]],
    origin_batch_id: str | None = None,
    production_profile: Mapping[str, object] | None = None,
    now: str | None = None,
) -> dict[str, object]:
    timestamp = now or _timestamp()
    return {
        "batch_schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": str(uuid.uuid4()),
        "project_id": validate_project_id(project_id),
        "mode": mode,
        "state": BATCH_RUNNING,
        "production_profile": dict(production_profile or {"upscale_method": "none"}),
        "attention": None,
        "pause_requested": False,
        "created_at": timestamp,
        "started_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
        "origin_batch_id": origin_batch_id,
        "current_index": None,
        "recovered": None,
        "items": items,
    }


def batch_counts(record: Mapping[str, object]) -> dict[str, int]:
    """Deterministic scene-count progress semantics.

    ``processed`` counts items that reached a stable disposition (COMPLETE,
    ALREADY_COMPLETE, FAILED, FINALIZATION_FAILED, STALE_AFTER_RENDER,
    CANCELLED, SKIPPED).  The denominator is the batch's fixed scene set.
    No time or computation percentage is ever derived.
    """

    items = record.get("items") if isinstance(record.get("items"), list) else []
    dispositions = [
        item.get("disposition")
        for item in items
        if isinstance(item, Mapping)
    ]
    total = len(dispositions)
    complete = sum(1 for disposition in dispositions if disposition in COMPLETE_ITEM_DISPOSITIONS)
    failed = sum(1 for disposition in dispositions if disposition in FAILED_ITEM_DISPOSITIONS)
    cancelled = sum(1 for disposition in dispositions if disposition == ITEM_CANCELLED)
    skipped = sum(1 for disposition in dispositions if disposition == ITEM_SKIPPED)
    active = sum(1 for disposition in dispositions if disposition in ACTIVE_ITEM_DISPOSITIONS)
    processed = sum(1 for disposition in dispositions if disposition in STABLE_ITEM_DISPOSITIONS)
    return {
        "total": total,
        "processed": processed,
        "complete": complete,
        "failed": failed,
        "cancelled": cancelled,
        "skipped": skipped,
        "active": active,
        "remaining": total - processed,
    }


def batch_current_item(record: Mapping[str, object]) -> dict[str, object] | None:
    index = record.get("current_index")
    items = record.get("items")
    if isinstance(index, int) and not isinstance(index, bool) and isinstance(items, list) and 0 <= index < len(items):
        item = items[index]
        if isinstance(item, Mapping):
            return dict(item)
    return None


def batch_attention(attention: object) -> dict[str, str] | None:
    if isinstance(attention, Mapping) and isinstance(attention.get("code"), str) and isinstance(attention.get("message"), str):
        return {"code": attention["code"], "message": attention["message"]}
    return None


def public_batch_record(record: Mapping[str, object] | None) -> dict[str, object] | None:
    """Project-facing batch presentation without internal identifiers."""

    if record is None:
        return None
    items: list[dict[str, object]] = []
    for item in record.get("items", []) if isinstance(record.get("items"), list) else []:
        if not isinstance(item, Mapping):
            continue
        failure = item.get("failure")
        msg = failure.get("message") if isinstance(failure, Mapping) else None
        items.append({
            "scene_id": item.get("scene_id"),
            "sequence": item.get("sequence"),
            "action": item.get("action"),
            "disposition": item.get("disposition"),
            "reason_code": item.get("reason_code"),
            "failure": {
                "code": failure.get("code"),
                "message": sanitize_user_facing_message(msg, fallback="Batch item failed.") if msg else None,
            } if isinstance(failure, Mapping) else None,
        })
    current = batch_current_item(record)
    attention = batch_attention(record.get("attention"))
    if isinstance(attention, Mapping) and "message" in attention:
        att_copy = dict(attention)
        att_copy["message"] = sanitize_user_facing_message(att_copy.get("message"), fallback="Batch paused.")
        attention = att_copy
    result = {
        "state": record.get("state"),
        "mode": record.get("mode"),
        "production_profile": record.get("production_profile") or {"upscale_method": "none"},
        "attention": attention,
        "pause_requested": record.get("pause_requested") is True,
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "counts": batch_counts(record),
        "current": {
            "scene_id": current.get("scene_id"),
            "sequence": current.get("sequence"),
            "disposition": current.get("disposition"),
        } if current is not None else None,
        "retryable_count": sum(
            1
            for item in items
            if item.get("disposition") in RETRYABLE_ITEM_DISPOSITIONS
        ),
        "items": items,
    }
    return result


# ---------------------------------------------------------------------------
# Authoritative batch action planner
# ---------------------------------------------------------------------------

def _first_blocker_code(scene_preflight: Mapping[str, object]) -> str:
    blockers = scene_preflight.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if isinstance(blocker, Mapping) and isinstance(blocker.get("code"), str):
                return blocker["code"]
    if scene_preflight.get("runtime_requirements_ready") is not True:
        return "RUNTIME_REQUIREMENTS_NOT_READY"
    if scene_preflight.get("workflow_ready") is not True:
        return "WORKFLOW_CONTRACT_INVALID"
    return "PREPARATION_NOT_READY"


def plan_scene_action(
    scene_preflight: Mapping[str, object],
    scene_jobs: list[Mapping[str, object]],
    *,
    finalization_by_job_id: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[str, str | None]:
    """Resolve the minimum authoritative action for one scene.

    Reuses Phase 8B job states and Phase 8C finalization summaries; no
    batch-specific eligibility rules are introduced.
    """

    for job in scene_jobs:
        if job.get("state") in ACTIVE_STATES:
            return ACTION_BLOCKED, "SCENE_JOB_ACTIVE"
    succeeded_jobs = [job for job in scene_jobs if job.get("state") == SUCCEEDED]
    if succeeded_jobs:
        latest = succeeded_jobs[-1]
        finalization = None
        if finalization_by_job_id is not None:
            finalization = finalization_by_job_id.get(str(latest.get("job_id")))
        if isinstance(finalization, Mapping):
            state = finalization.get("state")
            raw_current = finalization.get("raw_current") is True
            eligible = finalization.get("eligible") is not False
            if state == FINALIZATION_STATE_FINALIZED and raw_current:
                return ACTION_ALREADY_COMPLETE, None
            if raw_current and eligible and state in {FINALIZATION_STATE_RAW_READY, FINALIZATION_STATE_FAILED}:
                return ACTION_FINALIZE_RAW, None
            # A stale, unavailable, or non-retryable finalization falls through
            # to the preparation branch: the scene may still need a new render.
    if scene_preflight.get("execution_eligibility", {}).get("eligible") is True and scene_preflight.get("preparation_current") is True:
        return ACTION_RENDER_PREPARED, None
    if scene_preflight.get("preparation_ready") is True:
        return ACTION_PREPARE_AND_RENDER, None
    return ACTION_BLOCKED, _first_blocker_code(scene_preflight)


def plan_batch(
    storage: ProjectStorage,
    project_id: object,
    *,
    scene_ids: list[str] | None = None,
    preflight: Mapping[str, object],
    jobs: list[Mapping[str, object]],
    raw_output_root: Path | None = None,
    hardware_supported: bool | None = None,
) -> dict[str, object]:
    """One authoritative planning pass for all requested scenes.

    The same planner is used by preview and start so the confirmed plan is the
    executed plan, revalidated at start.
    """

    canonical_project_id = validate_project_id(project_id)
    project = storage.load_project(canonical_project_id)
    upscale_method = resolve_project_production_method(project)
    if upscale_method != POSTPROCESS_METHOD_NONE:
        availability = production_method_availability(upscale_method, hardware_supported=hardware_supported)
        if availability.get("state") == "UNAVAILABLE":
            raise RenderBatchError(
                "PRODUCTION_METHOD_UNAVAILABLE",
                "Selected upscaler is unavailable on this runtime.",
                details=availability,
            )
        if availability.get("state") == "UNQUALIFIED":
            raise RenderBatchError(
                "UPSCALER_UNQUALIFIED",
                "Upscaler requires qualification before batch production.",
                details=availability,
            )
    scenes = project.get("scenes") if isinstance(project.get("scenes"), list) else []
    scene_entries = [
        scene
        for scene in scenes
        if isinstance(scene, Mapping) and isinstance(scene.get("scene_id"), str)
    ]
    owned_scene_ids = {scene["scene_id"] for scene in scene_entries}
    if scene_ids is not None:
        for requested in scene_ids:
            canonical = validate_entity_id(requested, "Scene ID")
            if canonical not in owned_scene_ids:
                raise RenderBatchError(
                    "SCENE_NOT_OWNED",
                    "A selected scene does not belong to this project.",
                    details={"rejected": True},
                )
        requested_set = {validate_entity_id(requested, "Scene ID") for requested in scene_ids}
    else:
        requested_set = None

    preflight_scenes = preflight.get("scenes") if isinstance(preflight.get("scenes"), list) else []
    preflight_by_scene = {
        entry.get("scene_id"): entry
        for entry in preflight_scenes
        if isinstance(entry, Mapping)
    }
    jobs_by_scene: dict[str, list[dict[str, object]]] = {}
    for job in jobs:
        if not isinstance(job, Mapping) or not isinstance(job.get("scene_id"), str):
            continue
        if job.get("state") not in ACTIVE_STATES and job.get("state") != SUCCEEDED:
            continue
        jobs_by_scene.setdefault(job["scene_id"], []).append(dict(job))
    enriched = enrich_render_jobs_with_finalization(
        storage,
        canonical_project_id,
        [job for job in jobs if isinstance(job, Mapping)],
        preflight=preflight,
        raw_output_root=raw_output_root,
    )
    finalization_by_job_id = {
        str(job.get("job_id")): job.get("finalization")
        for job in enriched
        if isinstance(job.get("finalization"), Mapping)
    }

    planned: list[dict[str, object]] = []
    selected_set = requested_set if requested_set is not None else owned_scene_ids
    for index, scene in enumerate(scene_entries):
        scene_id = scene["scene_id"]
        if scene_id not in selected_set:
            continue
        scene_preflight = preflight_by_scene.get(scene_id)
        if not isinstance(scene_preflight, Mapping):
            action, reason = ACTION_BLOCKED, "SCENE_PREFLIGHT_MISSING"
        else:
            action, reason = plan_scene_action(
                scene_preflight,
                jobs_by_scene.get(scene_id, []),
                finalization_by_job_id=finalization_by_job_id,
            )
            if upscale_method != POSTPROCESS_METHOD_NONE:
                if action == ACTION_ALREADY_COMPLETE:
                    summary = summarize_production_scene(
                        storage,
                        canonical_project_id,
                        scene_id,
                        method=upscale_method,
                        preflight=preflight,
                        raw_output_root=raw_output_root,
                    )
                    if summary.get("state") == PRODUCTION_CURRENT:
                        action = ACTION_ALREADY_COMPLETE
                        reason = None
                    else:
                        action = ACTION_POSTPROCESS_ONLY
                        reason = None
                elif action == ACTION_FINALIZE_RAW:
                    action = ACTION_FINALIZE_AND_POSTPROCESS
        planned.append({
            "scene_id": scene_id,
            "sequence": index + 1,
            "action": action,
            "reason_code": reason,
        })

    counts = {
        "selected": len(planned),
        "eligible": sum(1 for entry in planned if entry["action"] in ELIGIBLE_BATCH_ACTIONS),
        "needs_render": sum(
            1
            for entry in planned
            if entry["action"] in {ACTION_RENDER_PREPARED, ACTION_PREPARE_AND_RENDER}
        ),
        "finalize_only": sum(1 for entry in planned if entry["action"] in {ACTION_FINALIZE_RAW, ACTION_FINALIZE_AND_POSTPROCESS}),
        "postprocess_only": sum(1 for entry in planned if entry["action"] == ACTION_POSTPROCESS_ONLY),
        "already_complete": sum(1 for entry in planned if entry["action"] == ACTION_ALREADY_COMPLETE),
        "blocked": sum(1 for entry in planned if entry["action"] == ACTION_BLOCKED),
    }
    return {
        "project_id": canonical_project_id,
        "scenes": planned,
        "counts": counts,
    }


def build_batch_preview_payload(plan: Mapping[str, object], *, active_batch: Mapping[str, object] | None, active_job: Mapping[str, object] | None) -> dict[str, object]:
    scenes_payload = []
    for entry in plan.get("scenes", []) if isinstance(plan.get("scenes"), list) else []:
        if isinstance(entry, Mapping):
            scenes_payload.append({
                "scene_id": entry.get("scene_id"),
                "sequence": entry.get("sequence"),
                "action": entry.get("action"),
                "reason_code": entry.get("reason_code"),
            })
    return {
        "scenes": scenes_payload,
        "counts": dict(plan.get("counts", {})),
        "blockers": {
            "active_batch_state": active_batch.get("state") if isinstance(active_batch, Mapping) else None,
            "active_job_scene_id": active_job.get("scene_id") if isinstance(active_job, Mapping) else None,
        },
    }


# ---------------------------------------------------------------------------
# Backend-owned sequential batch runner
# ---------------------------------------------------------------------------

class BatchRunner:
    """One backend-owned sequential orchestration loop for one project.

    The runner never duplicates per-scene services: preparation, submission,
    reconciliation, cancellation observation, and finalization all call the
    existing authoritative Phase 8 functions.  Exactly one H3 submission is
    active at a time and at most one new H3 attempt is made per item per run.
    """

    def __init__(
        self,
        storage: ProjectStorage,
        project_id: str,
        *,
        client: object | None = None,
        preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
        package_loader: Callable | None = None,
        compatibility_validator: Callable | None = None,
        hardware_gate: object | None = None,
        media_adapter: object | None = None,
        raw_output_root: Path | None = None,
        finalizer: Callable[..., Mapping[str, object]] = finalize_render_job,
        preparer: Callable[..., Mapping[str, object]] = prepare_render_scene,
        poll_interval_seconds: float = 1.0,
    ):
        self.storage = storage
        self.project_id = validate_project_id(project_id)
        self.client = client
        self.preflight_builder = preflight_builder
        self.package_loader = package_loader
        self.compatibility_validator = compatibility_validator
        self.hardware_gate = hardware_gate
        self.media_adapter = media_adapter
        self.raw_output_root = raw_output_root
        self.finalizer = finalizer
        self.preparer = preparer
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconcile_failures = 0
        self._post_recovery_hold = False
        self._tick_requested = False
        self._last_tick_steps = 0

    # -- lifecycle ----------------------------------------------------------

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start_thread(self) -> None:
        with self.lock:
            if self.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            thread = threading.Thread(
                target=self._run_loop,
                name=f"mvb-batch-{self.project_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _run_loop(self) -> None:
        LOGGER.info("Batch runner started project_id=%s", self.project_id)
        while not self._stop.is_set():
            try:
                record = self.tick()
            except Exception:  # pragma: no cover - defensive runner boundary
                LOGGER.exception("Batch runner encountered an unexpected error project_id=%s", self.project_id)
                record = self._emergency_pause()
            if record is None or record.get("state") not in EXECUTING_BATCH_STATES:
                break
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()
        LOGGER.info("Batch runner stopped project_id=%s", self.project_id)

    def _emergency_pause(self) -> dict[str, object] | None:
        """Never leave a batch RUNNING without a live backend runner."""

        try:
            with self.lock:
                record = self._store().load_active(self.project_id)
                if record is None or record["state"] not in EXECUTING_BATCH_STATES:
                    return record
                return self._pause_for_attention(
                    record,
                    "BATCH_INTERNAL_ERROR",
                    "Batch paused: an unexpected orchestration error occurred.",
                )
        except Exception:  # pragma: no cover - defensive boundary
            LOGGER.exception("Batch emergency pause failed project_id=%s", self.project_id)
            return None

    # -- helpers ------------------------------------------------------------

    def _store(self) -> BatchStore:
        return BatchStore(self.storage)

    def _comfy(self) -> object:
        if self.client is not None:
            return self.client
        self.client = ComfyUIClient()
        return self.client

    def _submission_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {"client": self._comfy()}
        if self.package_loader is not None:
            kwargs["package_loader"] = self.package_loader
        if self.compatibility_validator is not None:
            kwargs["compatibility_validator"] = self.compatibility_validator
        if self.hardware_gate is not None:
            kwargs["hardware_gate"] = self.hardware_gate
        return kwargs

    def _current_preflight(self) -> dict[str, object]:
        project = self.storage.load_project(self.project_id)
        return dict(self.preflight_builder(project, self.storage))

    def _systemic_runtime_check(
        self,
        preflight: Mapping[str, object],
        *,
        scene_preflight: Mapping[str, object] | None = None,
        needs_finalization: bool,
        upscale_method: str = "none",
    ) -> tuple[str, str] | None:
        readiness = scene_preflight if isinstance(scene_preflight, Mapping) else preflight
        if readiness.get("workflow_ready") is not True:
            return ("WORKFLOW_CONTRACT_INVALID", "Batch paused: the selected scene workflow contract is not ready.")
        if readiness.get("runtime_requirements_ready") is not True:
            return ("RUNTIME_REQUIREMENTS_CHANGED", "Batch paused: the selected scene runtime requirements are not ready.")
        if needs_finalization and find_ffmpeg() is None:
            return ("MEDIA_TOOL_MISSING", "Batch paused: FFmpeg is not available for finalization.")
        if upscale_method != POSTPROCESS_METHOD_NONE:
            avail = production_method_availability(upscale_method)
            if avail.get("state") == "UNAVAILABLE":
                return ("PRODUCTION_METHOD_UNAVAILABLE", "Batch paused: the selected production upscaler is unavailable on this runtime.")
            if avail.get("state") == "UNQUALIFIED":
                return ("UPSCALER_UNQUALIFIED", "Batch paused: the selected upscaler requires qualification before batch production.")
        return None

    def _pause_for_attention(self, record: dict[str, object], code: str, message: str) -> dict[str, object]:
        record["state"] = BATCH_PAUSED
        record["attention"] = {"code": code, "message": message, "at": _timestamp()}
        record["current_index"] = record.get("current_index")
        LOGGER.warning("Batch paused for attention project_id=%s code=%s", self.project_id, code)
        return self._store().save(record)

    def _find_job_record(self, job_id: object) -> dict[str, object] | None:
        if not isinstance(job_id, str):
            return None
        try:
            return JobStore(self.storage).load(self.project_id, job_id)
        except (RenderJobError, ProjectValidationError, ProjectNotFoundError):
            return None

    def _scene_preflight_entry(self, preflight: Mapping[str, object], scene_id: str) -> Mapping[str, object] | None:
        for entry in preflight.get("scenes", []) if isinstance(preflight.get("scenes"), list) else []:
            if isinstance(entry, Mapping) and entry.get("scene_id") == scene_id:
                return entry
        return None

    def _reconcile_scene(self, scene_id: str) -> dict[str, list[dict[str, object]]]:
        try:
            reconciled = reconcile_project_jobs(
                self.storage,
                self.project_id,
                client=self._comfy(),
                output_root=self.raw_output_root,
            )
            jobs = [
                j for j in reconciled.get("jobs", [])
                if isinstance(j, Mapping) and j.get("scene_id") == scene_id
            ]
            return {"jobs": jobs}
        except (RenderJobError, ProjectPersistenceError):
            return {"jobs": []}

    # -- tick: one bounded orchestration step -------------------------------

    def tick(self) -> dict[str, object] | None:
        """Advance the batch through a bounded iterative transition drain.

        A stable item may request another local transition, but those requests
        are serviced by this loop rather than by recursive calls.  The runner
        thread will continue on the next poll when the per-tick budget is
        exhausted.
        """

        with self.lock:
            record: dict[str, object] | None = None
            self._last_tick_steps = 0
            for step in range(BATCH_TICK_TRANSITION_BUDGET):
                self._tick_requested = False
                record = self._tick_once()
                self._last_tick_steps = step + 1
                if not self._tick_requested:
                    break
            return record

    def _tick_once(self) -> dict[str, object] | None:
        store = self._store()
        record = store.load_active(self.project_id)
        if record is None or record["state"] not in EXECUTING_BATCH_STATES:
            return record
        if record.get("pause_requested") is True and not self._has_active_item(record):
            return self._enter_paused(record)
        item_index = self._current_active_item_index(record)
        if item_index is None:
            return self._select_and_dispatch_next(record)
        return self._advance_active_item(record, item_index)

    def _request_next_tick(self, record: dict[str, object]) -> dict[str, object]:
        self._tick_requested = True
        return record

    def _has_active_item(self, record: Mapping[str, object]) -> bool:
        if self._current_active_item_index(record) is not None:
            return True
        items = record.get("items")
        if not isinstance(items, list):
            return False
        return any(
            isinstance(item, Mapping) and item.get("disposition") in ACTIVE_ITEM_DISPOSITIONS
            for item in items
        )

    def _current_active_item_index(self, record: Mapping[str, object]) -> int | None:
        index = record.get("current_index")
        items = record.get("items")
        if not isinstance(index, int) or isinstance(index, bool) or not isinstance(items, list):
            return None
        if not 0 <= index < len(items):
            return None
        item = items[index]
        if isinstance(item, Mapping) and item.get("disposition") in ACTIVE_ITEM_DISPOSITIONS:
            return index
        return None

    def _enter_paused(self, record: dict[str, object]) -> dict[str, object]:
        record["state"] = BATCH_PAUSED
        record["pause_requested"] = False
        record["current_index"] = None
        LOGGER.info("Batch paused after current project_id=%s", self.project_id)
        return self._store().save(record)

    def _complete_batch(self, record: dict[str, object]) -> dict[str, object]:
        counts = batch_counts(record)
        has_issues = any(
            isinstance(item, Mapping) and item.get("disposition") in ISSUE_ITEM_DISPOSITIONS
            for item in record.get("items", [])
        )
        record["state"] = BATCH_COMPLETED_WITH_ISSUES if has_issues else BATCH_COMPLETED
        record["current_index"] = None
        record["completed_at"] = _timestamp()
        LOGGER.info(
            "Batch finished project_id=%s state=%s processed=%s total=%s",
            self.project_id,
            record["state"],
            counts["processed"],
            counts["total"],
        )
        return self._store().save(record)

    def _select_and_dispatch_next(self, record: dict[str, object]) -> dict[str, object]:
        store = self._store()
        items = record.get("items")

        # Recovery first: a restarted backend may leave an item mid-flight with
        # no current_index.  Resume it in place before selecting anything new.
        resume_index = None
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, Mapping) and item.get("disposition") in ACTIVE_ITEM_DISPOSITIONS:
                    resume_index = index
                    break
        if resume_index is not None:
            resume_item = dict(items[resume_index])
            disposition = resume_item.get("disposition")
            record["current_index"] = resume_index
            record = store.save(record)
            if disposition == ITEM_POSTPROCESSING:
                return self._advance_postprocessing_item(record, resume_index)
            if disposition == ITEM_FINALIZING:
                return self._finalize_item(record, resume_index, jobs=None, preflight=None)
            if disposition == ITEM_RENDERING:
                return self._advance_active_item(record, resume_index)
            # PREPARING is safely re-planned from the beginning.
            resume_item["disposition"] = ITEM_PENDING
            resume_item["updated_at"] = _timestamp()
            record["items"][resume_index] = resume_item
            record["current_index"] = None
            saved = store.save(record)
            return self._request_next_tick(saved) if saved["state"] in EXECUTING_BATCH_STATES else saved

        next_index = None
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, Mapping) and item.get("disposition") == ITEM_PENDING:
                    next_index = index
                    break
        if next_index is None:
            return self._complete_batch(record)

        if self._post_recovery_hold:
            self._post_recovery_hold = False
            record["state"] = BATCH_PAUSED_RECOVERY
            record["attention"] = {
                "code": "RESTART_RECOVERY",
                "message": "Batch paused after restart. Resume to continue production.",
                "at": _timestamp(),
            }
            LOGGER.info("Batch paused after restart recovery project_id=%s", self.project_id)
            return store.save(record)

        item = dict(items[next_index])
        scene_id = str(item["scene_id"])

        # Revalidate current scene state immediately before dispatch.  Batch
        # membership is fixed, but readiness/currentness is never assumed from
        # selection time.
        try:
            preflight = self._current_preflight()
        except (ProjectPersistenceError, ProjectNotFoundError, OSError):
            return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: project state could not be read.")
        scene_preflight = self._scene_preflight_entry(preflight, scene_id)
        if not isinstance(scene_preflight, Mapping):
            item["disposition"] = ITEM_SKIPPED
            item["reason_code"] = "SCENE_PREFLIGHT_MISSING"
            item["updated_at"] = _timestamp()
            items[next_index] = item
            record["items"] = items
            saved = store.save(record)
            return self._request_next_tick(saved) if saved["state"] in EXECUTING_BATCH_STATES else saved

        method = record.get("production_profile", {}).get("upscale_method", "none")
        systemic = self._systemic_runtime_check(
            preflight,
            scene_preflight=scene_preflight,
            needs_finalization=item.get("action") in {ACTION_FINALIZE_RAW, ACTION_RENDER_PREPARED, ACTION_PREPARE_AND_RENDER, ACTION_FINALIZE_AND_POSTPROCESS},
            upscale_method=method,
        )
        if systemic is not None:
            return self._pause_for_attention(record, systemic[0], systemic[1])

        reconciled = self._reconcile_scene(scene_id)
        jobs = reconciled["jobs"]
        finalization_map = self._finalization_map_for_scene(jobs, preflight)
        action, reason = plan_scene_action(
            scene_preflight,
            jobs,
            finalization_by_job_id=finalization_map,
        )
        method = record.get("production_profile", {}).get("upscale_method", "none")
        if method != POSTPROCESS_METHOD_NONE:
            if action == ACTION_ALREADY_COMPLETE:
                summary = summarize_production_scene(
                    self.storage,
                    self.project_id,
                    scene_id,
                    method=method,
                    preflight=preflight,
                    raw_output_root=self.raw_output_root,
                )
                if summary.get("state") == PRODUCTION_CURRENT:
                    action = ACTION_ALREADY_COMPLETE
                    reason = None
                else:
                    action = ACTION_POSTPROCESS_ONLY
                    reason = None
            elif action == ACTION_FINALIZE_RAW:
                action = ACTION_FINALIZE_AND_POSTPROCESS

        if action == ACTION_BLOCKED:
            item["disposition"] = ITEM_SKIPPED
            item["reason_code"] = reason or "BLOCKED"
            item["updated_at"] = _timestamp()
            items[next_index] = item
            record["items"] = items
            LOGGER.info(
                "Batch item skipped before dispatch project_id=%s scene_id=%s reason=%s",
                self.project_id,
                scene_id,
                reason,
            )
            saved = store.save(record)
            return self._request_next_tick(saved) if saved["state"] in EXECUTING_BATCH_STATES else saved

        if action == ACTION_ALREADY_COMPLETE:
            item["disposition"] = ITEM_ALREADY_COMPLETE
            item["reason_code"] = None
            item["updated_at"] = _timestamp()
            items[next_index] = item
            record["items"] = items
            saved = store.save(record)
            return self._request_next_tick(saved) if saved["state"] in EXECUTING_BATCH_STATES else saved

        record["current_index"] = next_index
        record = store.save(record)
        if action == ACTION_POSTPROCESS_ONLY:
            return self._postprocess_item(record, next_index, preflight=preflight)
        if action in {ACTION_FINALIZE_RAW, ACTION_FINALIZE_AND_POSTPROCESS}:
            return self._finalize_item(record, next_index, jobs=jobs, preflight=preflight)
        if action == ACTION_PREPARE_AND_RENDER:
            return self._prepare_item(record, next_index, preflight=preflight)
        return self._submit_item(record, next_index, preflight=preflight)

    def _finalization_map_for_scene(self, jobs: list[Mapping[str, object]], preflight: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        try:
            enriched = enrich_render_jobs_with_finalization(
                self.storage,
                self.project_id,
                [dict(job) for job in jobs],
                preflight=preflight,
                raw_output_root=self.raw_output_root,
            )
        except (RenderJobError, ProjectValidationError, OSError):
            return {}
        return {
            str(job.get("job_id")): job.get("finalization")
            for job in enriched
            if isinstance(job.get("finalization"), Mapping)
        }

    def _prepare_item(self, record: dict[str, object], index: int, *, preflight: Mapping[str, object]) -> dict[str, object]:
        store = self._store()
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        item["disposition"] = ITEM_PREPARING
        record["items"][index] = item
        record = store.save(record)
        LOGGER.info("Batch preparing scene project_id=%s scene_id=%s", self.project_id, scene_id)
        try:
            self.preparer(self.storage, self.project_id, scene_id)
        except RenderPreparationBlocked:
            item = dict(record["items"][index])
            item["disposition"] = ITEM_SKIPPED
            item["reason_code"] = "NOT_READY"
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            saved = store.save(record)
            return self._continue_or_finish(saved)
        except RenderPreparationError as error:
            return self._fail_item(record, index, "PREPARATION_FAILED", error.message)
        except (ProjectPersistenceError, OSError):
            return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: preparation state could not be persisted.")
        return self._submit_item(record, index, preflight=None)

    def _submit_item(self, record: dict[str, object], index: int, *, preflight: Mapping[str, object] | None) -> dict[str, object]:
        store = self._store()
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        if preflight is None:
            try:
                preflight = self._current_preflight()
            except (ProjectPersistenceError, ProjectNotFoundError, OSError):
                return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: project state could not be read.")
        try:
            job = submit_render_job(
                self.storage,
                self.project_id,
                scene_id,
                preflight=preflight,
                **self._submission_kwargs(),
            )
        except RenderJobConflict:
            return self._fail_item(record, index, "SCENE_JOB_ACTIVE", "This scene already has a live render job.")
        except RenderExecutionBlocked as error:
            item = dict(record["items"][index])
            item["disposition"] = ITEM_SKIPPED
            item["reason_code"] = error.code or "NOT_READY"
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            saved = store.save(record)
            return self._continue_or_finish(saved)
        except RenderJobSubmissionError as error:
            if error.code in SYSTEMIC_COMFYUI_CODES:
                return self._pause_for_attention(record, "COMFYUI_UNAVAILABLE", "Batch paused: ComfyUI is unavailable.")
            return self._fail_item(record, index, error.code, error.message)
        except ComfyUIClientError as error:
            if error.code in SYSTEMIC_COMFYUI_CODES:
                return self._pause_for_attention(record, "COMFYUI_UNAVAILABLE", "Batch paused: ComfyUI is unavailable.")
            if error.code in SYSTEMIC_RUNTIME_NODE_CODES:
                return self._pause_for_attention(record, "RUNTIME_REQUIREMENTS_CHANGED", "Batch paused: runtime requirements changed.")
            return self._fail_item(record, index, error.code, error.message)
        except (ProjectPersistenceError, OSError):
            return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: render job state could not be persisted.")
        item = dict(record["items"][index])
        item["disposition"] = ITEM_RENDERING
        item["job_id"] = str(job.get("job_id"))
        item["updated_at"] = _timestamp()
        record["items"][index] = item
        LOGGER.info(
            "Batch submitted scene project_id=%s scene_id=%s job_id=%s",
            self.project_id,
            scene_id,
            job.get("job_id"),
        )
        return store.save(record)

    def _advance_active_item(self, record: dict[str, object], index: int) -> dict[str, object]:
        item = dict(record["items"][index])
        disposition = item.get("disposition")
        if disposition == ITEM_POSTPROCESSING:
            return self._advance_postprocessing_item(record, index)
        if disposition == ITEM_FINALIZING:
            return self._finalize_item(record, index, jobs=None, preflight=None)
        if disposition == ITEM_PREPARING:
            # A crash between preparation start and completion leaves the item
            # here; revalidation re-plans it safely.
            record["current_index"] = None
            item["disposition"] = ITEM_PENDING
            record["items"][index] = item
            saved = self._store().save(record)
            return self._request_next_tick(saved) if saved["state"] in EXECUTING_BATCH_STATES else saved
        # ITEM_RENDERING: reconcile the owned job through Phase 8B authority.
        job_id = item.get("job_id")
        job = self._find_job_record(job_id)
        if job is None:
            return self._fail_item(record, index, "JOB_RECORD_MISSING", "The batch-owned render job record is missing.")
        if job.get("state") in ACTIVE_STATES:
            try:
                reconcile_project_jobs(self.storage, self.project_id, client=self._comfy(), output_root=self.raw_output_root)
            except ComfyUIClientError as error:
                self._reconcile_failures += 1
                if error.code in SYSTEMIC_COMFYUI_CODES and self._reconcile_failures >= RECONCILIATION_FAILURE_PAUSE_THRESHOLD:
                    return self._pause_for_attention(record, "COMFYUI_UNAVAILABLE", "Batch paused: ComfyUI is unavailable.")
                return self._store().load_active(self.project_id) or record
            except (ProjectPersistenceError, OSError):
                return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: render job state could not be persisted.")
            refreshed = self._find_job_record(job_id)
            if refreshed is None:
                return self._fail_item(record, index, "JOB_RECORD_MISSING", "The batch-owned render job record is missing.")
            if refreshed.get("state") in ACTIVE_STATES:
                # Reconciliation records transport failures as durable poll
                # errors without failing the scene.  A transient loss keeps the
                # job; a sustained loss beyond bounded reconciliation pauses
                # the batch for attention instead of mass-failing scenes.
                if isinstance(refreshed.get("last_poll_error"), Mapping):
                    self._reconcile_failures += 1
                    if self._reconcile_failures >= RECONCILIATION_FAILURE_PAUSE_THRESHOLD:
                        return self._pause_for_attention(record, "COMFYUI_UNAVAILABLE", "Batch paused: ComfyUI is unavailable.")
                else:
                    self._reconcile_failures = 0
                record = self._store().load_active(self.project_id) or record
                record["current_index"] = index
                return record
            job = refreshed

        self._reconcile_failures = 0
        state = job.get("state")
        if state == SUCCEEDED:
            return self._on_render_succeeded(record, index, job)
        if state == CANCELLED:
            return self._cancel_item(record, index, job)
        if state == INTERRUPTED and isinstance(job.get("cancel"), Mapping):
            return self._cancel_item(record, index, job)
        if state == ORPHANED:
            # Orphan recovery keeps the historical job; the batch never
            # automatically re-renders the same scene within the same run.
            return self._fail_item(record, index, "COMFYUI_JOB_ORPHANED", "The batch render is no longer available; retry it later.")
        return self._fail_item(
            record,
            index,
            (job.get("failure") or {}).get("code") if isinstance(job.get("failure"), Mapping) else "COMFYUI_EXECUTION_FAILED",
            (job.get("failure") or {}).get("message") if isinstance(job.get("failure"), Mapping) else "The batch render failed.",
        )

    def _on_render_succeeded(self, record: dict[str, object], index: int, job: Mapping[str, object]) -> dict[str, object]:
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        if not is_raw_output_usable(job, output_root=self.raw_output_root):
            return self._fail_item(
                record,
                index,
                "RAW_OUTPUT_UNAVAILABLE",
                "ComfyUI reported render success, but no usable Builder-owned raw H3 output is available.",
                disposition=ITEM_FINALIZATION_FAILED,
            )
        # Currentness before automatic finalization: a scene edited while its
        # H3 job ran keeps the historical raw output without false promotion.
        try:
            preflight = self._current_preflight()
        except (ProjectPersistenceError, ProjectNotFoundError, OSError):
            return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: project state could not be read.")
        scene_preflight = self._scene_preflight_entry(preflight, scene_id)
        current = (
            isinstance(scene_preflight, Mapping)
            and scene_preflight.get("preparation_status") == "current"
            and scene_preflight.get("preparation_fingerprint") is not None
            and scene_preflight.get("preparation_fingerprint") == job.get("preparation_fingerprint")
        )
        if not current:
            item["disposition"] = ITEM_STALE_AFTER_RENDER
            item["reason_code"] = "SCENE_CHANGED_DURING_RENDER"
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            LOGGER.info(
                "Batch scene changed during render; raw retained historically project_id=%s scene_id=%s job_id=%s",
                self.project_id,
                scene_id,
                job.get("job_id"),
            )
            saved = self._store().save(record)
            return self._continue_or_finish(saved)
        return self._finalize_item(record, index, jobs=None, preflight=preflight, job_id=str(job.get("job_id")))

    def _finalize_item(
        self,
        record: dict[str, object],
        index: int,
        *,
        jobs: list[Mapping[str, object]] | None,
        preflight: Mapping[str, object] | None,
        job_id: str | None = None,
    ) -> dict[str, object]:
        store = self._store()
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        if job_id is None:
            job_id = item.get("job_id") if isinstance(item.get("job_id"), str) else None
        if job_id is None:
            # FINALIZE_RAW dispatch: select the newest current successful raw job.
            scene_jobs = jobs if jobs is not None else JobStore(self.storage).list_scene(self.project_id, scene_id)
            succeeded = [
                job
                for job in scene_jobs
                if job.get("state") == SUCCEEDED
                and is_raw_output_usable(job, output_root=self.raw_output_root)
            ]
            if not succeeded:
                return self._fail_item(
                    record,
                    index,
                    "RAW_OUTPUT_UNAVAILABLE",
                    "No successful raw H3 render with a usable Builder-owned output is available to finalize.",
                    disposition=ITEM_FINALIZATION_FAILED,
                )
            job_id = str(succeeded[-1].get("job_id"))
        if find_ffmpeg() is None:
            return self._pause_for_attention(record, "MEDIA_TOOL_MISSING", "Batch paused: FFmpeg is not available for finalization.")
        item["disposition"] = ITEM_FINALIZING
        item["job_id"] = job_id
        item["updated_at"] = _timestamp()
        record["items"][index] = item
        record = store.save(record)
        LOGGER.info("Batch finalizing scene project_id=%s scene_id=%s job_id=%s", self.project_id, scene_id, job_id)
        try:
            self.finalizer(
                self.storage,
                self.project_id,
                job_id,
                preflight=preflight,
                media_adapter=self.media_adapter,
                raw_output_root=self.raw_output_root,
            )
        except RenderFinalizationError as error:
            if error.code == "MEDIA_TOOL_MISSING":
                return self._pause_for_attention(record, "MEDIA_TOOL_MISSING", "Batch paused: FFmpeg is not available for finalization.")
            if error.code in {"FINALIZATION_NOT_ELIGIBLE", "FINALIZATION_STALE", "PREPARATION_STALE", "RAW_OUTPUT_STALE"}:
                item = dict(record["items"][index])
                item["disposition"] = ITEM_STALE_AFTER_RENDER
                item["reason_code"] = error.code
                item["updated_at"] = _timestamp()
                record["items"][index] = item
                record["current_index"] = None
                saved = store.save(record)
                return self._continue_or_finish(saved)
            # Raw H3 output remains preserved; only finalization failed.
            return self._fail_item(record, index, error.code, error.message, disposition=ITEM_FINALIZATION_FAILED)
        except (ProjectPersistenceError, OSError):
            return self._pause_for_attention(record, "STORAGE_UNAVAILABLE", "Batch paused: finalization state could not be persisted.")

        method = record.get("production_profile", {}).get("upscale_method", "none")
        if method == POSTPROCESS_METHOD_NONE:
            item = dict(record["items"][index])
            item["disposition"] = ITEM_COMPLETE
            item["reason_code"] = None
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            LOGGER.info("Batch scene complete project_id=%s scene_id=%s", self.project_id, scene_id)
            saved = store.save(record)
            return self._continue_or_finish(saved)
        return self._postprocess_item(record, index, preflight=preflight)

    def _postprocess_item(
        self,
        record: dict[str, object],
        index: int,
        *,
        preflight: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        store = self._store()
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        method = record.get("production_profile", {}).get("upscale_method", "none")
        if method == POSTPROCESS_METHOD_NONE:
            item["disposition"] = ITEM_COMPLETE
            item["reason_code"] = None
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            saved = store.save(record)
            return self._continue_or_finish(saved)

        avail = production_method_availability(method)
        if avail.get("state") == "UNAVAILABLE":
            return self._pause_for_attention(
                record,
                "PRODUCTION_METHOD_UNAVAILABLE",
                "Batch paused: the selected production upscaler is unavailable on this runtime.",
            )
        if avail.get("state") == "UNQUALIFIED":
            return self._pause_for_attention(
                record,
                "UPSCALER_UNQUALIFIED",
                "Batch paused: the selected upscaler requires qualification before batch production.",
            )

        try:
            selector, staged_path = materialize_final_scene_for_postprocess(
                self.storage,
                self.project_id,
                scene_id,
                raw_output_root=self.raw_output_root,
                preflight=preflight,
            )
            output_prefix = f"mvb_{self.project_id}_{scene_id}_{uuid.uuid4().hex[:8]}"
            job = submit_postprocess_job(
                self.storage,
                self.project_id,
                scene_id,
                method=method,
                client=self._comfy(),
                final_scene_selector=selector,
                output_prefix=output_prefix,
                raw_output_root=self.raw_output_root,
                preflight=preflight,
            )
        except ProductionMethodUnavailable:
            return self._pause_for_attention(
                record,
                "PRODUCTION_METHOD_UNAVAILABLE",
                "Batch paused: the selected production upscaler is unavailable on this runtime.",
            )
        except PostprocessJobConflict:
            return self._fail_item(
                record,
                index,
                "POSTPROCESS_JOB_ACTIVE",
                "This scene already has an active post-process job.",
                disposition=ITEM_POSTPROCESS_FAILED,
            )
        except ProductionNotReady as error:
            return self._fail_item(record, index, error.code, error.message, disposition=ITEM_POSTPROCESS_FAILED)
        except RenderProductionError as error:
            return self._fail_item(record, index, error.code, error.message, disposition=ITEM_POSTPROCESS_FAILED)
        except Exception as error:
            return self._fail_item(
                record,
                index,
                "POSTPROCESS_SUBMISSION_FAILED",
                str(error),
                disposition=ITEM_POSTPROCESS_FAILED,
            )

        item = dict(record["items"][index])
        item["disposition"] = ITEM_POSTPROCESSING
        item["postprocess_job_id"] = str(job.get("postprocess_job_id"))
        item["updated_at"] = _timestamp()
        record["items"][index] = item
        record["current_index"] = index
        LOGGER.info(
            "Batch postprocessing scene project_id=%s scene_id=%s method=%s postprocess_job_id=%s",
            self.project_id,
            scene_id,
            method,
            job.get("postprocess_job_id"),
        )
        return store.save(record)

    def _advance_postprocessing_item(self, record: dict[str, object], index: int) -> dict[str, object]:
        item = dict(record["items"][index])
        scene_id = str(item["scene_id"])
        job_id = item.get("postprocess_job_id")
        try:
            refreshed = reconcile_postprocess_job(
                self.storage,
                self.project_id,
                scene_id,
                job_id,
                client=self._comfy(),
                output_root=self.raw_output_root,
            )
        except Exception:
            self._reconcile_failures += 1
            if self._reconcile_failures >= RECONCILIATION_FAILURE_PAUSE_THRESHOLD:
                return self._pause_for_attention(record, "COMFYUI_UNAVAILABLE", "Batch paused: ComfyUI is unavailable.")
            record = self._store().load_active(self.project_id) or record
            record["current_index"] = index
            return record

        state = refreshed.get("state")
        if state in POSTPROCESS_ACTIVE_STATES:
            record = self._store().load_active(self.project_id) or record
            record["current_index"] = index
            return record

        self._reconcile_failures = 0
        if state == POSTPROCESS_SUCCEEDED:
            try:
                promoted = finalize_postprocess_job(
                    self.storage,
                    self.project_id,
                    scene_id,
                    job_id,
                    media_adapter=self.media_adapter,
                    preflight=self._current_preflight(),
                    raw_output_root=self.raw_output_root,
                )
            except RenderProductionError as error:
                return self._fail_item(record, index, error.code, error.message, disposition=ITEM_POSTPROCESS_FAILED)
            except Exception as error:
                return self._fail_item(
                    record,
                    index,
                    "POSTPROCESS_FINALIZATION_FAILED",
                    str(error),
                    disposition=ITEM_POSTPROCESS_FAILED,
                )

            item = dict(record["items"][index])
            item["disposition"] = ITEM_COMPLETE
            item["reason_code"] = None
            item["updated_at"] = _timestamp()
            record["items"][index] = item
            record["current_index"] = None
            LOGGER.info("Batch scene production complete project_id=%s scene_id=%s", self.project_id, scene_id)
            saved = self._store().save(record)
            return self._continue_or_finish(saved)

        if state == POSTPROCESS_CANCELLED:
            return self._cancel_item(record, index, refreshed)

        failure = refreshed.get("failure") if isinstance(refreshed.get("failure"), Mapping) else {}
        return self._fail_item(
            record,
            index,
            failure.get("code") or "POSTPROCESS_FAILED",
            failure.get("message") or "Post-processing failed.",
            disposition=ITEM_POSTPROCESS_FAILED,
        )

    def _cancel_item(self, record: dict[str, object], index: int, job: Mapping[str, object]) -> dict[str, object]:
        item = dict(record["items"][index])
        item["disposition"] = ITEM_CANCELLED
        item["reason_code"] = "USER_CANCELLED"
        item["updated_at"] = _timestamp()
        record["items"][index] = item
        record["current_index"] = None
        # An explicit user cancellation never starts the next H3 scene; the
        # batch pauses and requires Resume Batch.
        record["state"] = BATCH_PAUSED
        record["pause_requested"] = False
        record["attention"] = {
            "code": "CANCELLED_CURRENT",
            "message": "Batch paused after the current render was cancelled.",
            "at": _timestamp(),
        }
        LOGGER.info("Batch paused after user cancellation project_id=%s job_id=%s", self.project_id, job.get("job_id"))
        return self._store().save(record)

    def _fail_item(
        self,
        record: dict[str, object],
        index: int,
        code: str | None,
        message: str | None,
        *,
        disposition: str = ITEM_FAILED,
    ) -> dict[str, object]:
        item = dict(record["items"][index])
        item["disposition"] = disposition
        item["failure"] = {
            "code": code or "BATCH_ITEM_FAILED",
            "message": message or "The batch item failed.",
            "at": _timestamp(),
        }
        item["updated_at"] = _timestamp()
        record["items"][index] = item
        record["current_index"] = None
        LOGGER.warning(
            "Batch item failed project_id=%s scene_id=%s code=%s disposition=%s",
            self.project_id,
            item.get("scene_id"),
            code,
            disposition,
        )
        saved = self._store().save(record)
        return self._continue_or_finish(saved)

    def _continue_or_finish(self, record: dict[str, object]) -> dict[str, object]:
        if record.get("state") not in EXECUTING_BATCH_STATES:
            return record
        if record.get("pause_requested") is True:
            return self._enter_paused(record)
        if self._stop.is_set():
            return record
        return self._request_next_tick(record)


# ---------------------------------------------------------------------------
# Runner registry and recovery
# ---------------------------------------------------------------------------

def _get_runner(storage: ProjectStorage, project_id: str, **seams: object) -> BatchRunner:
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(project_id)
        if runner is None:
            runner = BatchRunner(storage, project_id, **seams)
            _RUNNERS[project_id] = runner
        return runner


def _active_project_job(storage: ProjectStorage, project_id: str) -> dict[str, object] | None:
    try:
        records = JobStore(storage).list_project(project_id)
    except (RenderJobError, ProjectValidationError):
        return None
    for record in reversed(records):
        if record.get("state") in ACTIVE_STATES:
            return record
    return None


def ensure_batch_recovery(storage: ProjectStorage, project_id: object, **seams: object) -> dict[str, object] | None:
    """Restore durable batch authority after any process or browser transition.

    If the backend process stayed alive, the existing runner thread continues
    untouched.  After a full backend/runtime restart the batch is recovered:
    a persisted active owned job resumes tracking, while a batch with no
    recoverable active job enters PAUSED_RECOVERY instead of automatically
    submitting new H3 work.
    """

    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        store = BatchStore(storage)
        record = store.load_active(canonical_project_id)
        if record is None:
            return None
        if record["state"] not in EXECUTING_BATCH_STATES:
            return record
        runner = None
        with _RUNNERS_LOCK:
            candidate = _RUNNERS.get(canonical_project_id)
            if candidate is not None and candidate.is_alive():
                return record
            runner = candidate
        if runner is None:
            runner = _get_runner(storage, canonical_project_id, **seams)
        current = batch_current_item(record)
        recovered_job = None
        if current is not None and isinstance(current.get("job_id"), str):
            job = runner._find_job_record(current.get("job_id"))
            if job is not None and job.get("state") in ACTIVE_STATES:
                recovered_job = job
        if recovered_job is None:
            active_job = _active_project_job(storage, canonical_project_id)
            if active_job is not None and current is not None and active_job.get("scene_id") == current.get("scene_id"):
                recovered_job = active_job
        if recovered_job is not None:
            record["recovered"] = {
                "job_id": recovered_job.get("job_id"),
                "scene_id": recovered_job.get("scene_id"),
                "at": _timestamp(),
            }
            record = store.save(record)
            runner._post_recovery_hold = True
            runner.start_thread()
            LOGGER.info(
                "Batch recovered active job after restart project_id=%s job_id=%s",
                canonical_project_id,
                recovered_job.get("job_id"),
            )
            return record
        if current is not None and isinstance(current.get("postprocess_job_id"), str):
            try:
                pjob = PostprocessJobStore(storage).load(canonical_project_id, current["scene_id"], current["postprocess_job_id"])
                if pjob.get("state") in POSTPROCESS_ACTIVE_STATES:
                    record["recovered"] = {
                        "postprocess_job_id": pjob.get("postprocess_job_id"),
                        "scene_id": current.get("scene_id"),
                        "at": _timestamp(),
                    }
                    record = store.save(record)
                    runner._post_recovery_hold = True
                    runner.start_thread()
                    LOGGER.info(
                        "Batch recovered active postprocess job after restart project_id=%s job_id=%s",
                        canonical_project_id,
                        pjob.get("postprocess_job_id"),
                    )
                    return record
            except Exception:
                pass
        items = record.get("items")
        if isinstance(items, list):
            for index, item in enumerate(items):
                if not isinstance(item, Mapping):
                    continue
                disposition = item.get("disposition")
                if disposition == ITEM_PREPARING:
                    updated = dict(item)
                    updated["disposition"] = ITEM_PENDING
                    updated["reason_code"] = "BATCH_RESTARTED"
                    updated["updated_at"] = _timestamp()
                    items[index] = updated
                elif disposition == ITEM_RENDERING:
                    updated = dict(item)
                    job = runner._find_job_record(updated.get("job_id")) if isinstance(updated.get("job_id"), str) else None
                    if job is None:
                        updated["disposition"] = ITEM_PENDING
                        updated["reason_code"] = "BATCH_RESTARTED"
                    elif job.get("state") == SUCCEEDED:
                        # The finished raw result is finalized on resume; the
                        # finalizer enforces currentness authoritatively.
                        updated["disposition"] = ITEM_FINALIZING
                    elif job.get("state") == CANCELLED or (job.get("state") == INTERRUPTED and isinstance(job.get("cancel"), Mapping)):
                        updated["disposition"] = ITEM_CANCELLED
                        updated["reason_code"] = "USER_CANCELLED"
                    else:
                        failure = job.get("failure") if isinstance(job.get("failure"), Mapping) else {}
                        updated["disposition"] = ITEM_FAILED
                        updated["failure"] = {
                            "code": failure.get("code") or "COMFYUI_EXECUTION_FAILED",
                            "message": failure.get("message") or "The batch render failed.",
                            "at": _timestamp(),
                        }
                    updated["updated_at"] = _timestamp()
                    items[index] = updated
                elif disposition == ITEM_POSTPROCESSING:
                    updated = dict(item)
                    job_id = updated.get("postprocess_job_id")
                    pjob = None
                    if isinstance(job_id, str) and job_id:
                        try:
                            pjob = PostprocessJobStore(storage).load(canonical_project_id, updated["scene_id"], job_id)
                        except Exception:
                            pjob = None
                    if pjob is None:
                        updated["disposition"] = ITEM_PENDING
                        updated["reason_code"] = "BATCH_RESTARTED"
                    elif pjob.get("state") == POSTPROCESS_SUCCEEDED:
                        updated["disposition"] = ITEM_POSTPROCESSING
                    elif pjob.get("state") == POSTPROCESS_CANCELLED:
                        updated["disposition"] = ITEM_CANCELLED
                        updated["reason_code"] = "USER_CANCELLED"
                    else:
                        failure = pjob.get("failure") if isinstance(pjob.get("failure"), Mapping) else {}
                        updated["disposition"] = ITEM_POSTPROCESS_FAILED
                        updated["failure"] = {
                            "code": failure.get("code") or "POSTPROCESS_FAILED",
                            "message": failure.get("message") or "Post-processing failed before restart.",
                            "at": _timestamp(),
                        }
                    updated["updated_at"] = _timestamp()
                    items[index] = updated
        record["items"] = items
        record["state"] = BATCH_PAUSED_RECOVERY
        record["pause_requested"] = False
        record["current_index"] = None
        record["attention"] = {
            "code": "RESTART_RECOVERY",
            "message": "Batch paused after restart. Resume to continue production.",
            "at": _timestamp(),
        }
        LOGGER.info("Batch entered safe recovery pause after restart project_id=%s", canonical_project_id)
        return store.save(record)


# ---------------------------------------------------------------------------
# Public batch operations (route surface)
# ---------------------------------------------------------------------------

def _reconcile_for_preview(
    storage: ProjectStorage,
    project_id: str,
    client: object | None,
    raw_output_root: Path | None = None,
) -> dict[str, object]:
    result = reconcile_project_jobs(storage, project_id, client=client, output_root=raw_output_root)
    jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
    return {"jobs": jobs, "warnings": result.get("warnings", [])}


def preview_batch(
    storage: ProjectStorage,
    project_id: object,
    *,
    scene_ids: list[str] | None = None,
    client: object | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    raw_output_root: Path | None = None,
    hardware_supported: bool | None = None,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    ensure_batch_recovery(storage, canonical_project_id, client=client, preflight_builder=preflight_builder, raw_output_root=raw_output_root)
    project = storage.load_project(canonical_project_id)
    preflight = dict(preflight_builder(project, storage))
    reconciled = _reconcile_for_preview(storage, canonical_project_id, client, raw_output_root)
    plan = plan_batch(
        storage,
        canonical_project_id,
        scene_ids=scene_ids,
        preflight=preflight,
        jobs=reconciled["jobs"],
        raw_output_root=raw_output_root,
        hardware_supported=hardware_supported,
    )
    store = BatchStore(storage)
    active_batch = store.load_active(canonical_project_id)
    active_job = _active_project_job(storage, canonical_project_id)
    return build_batch_preview_payload(plan, active_batch=active_batch, active_job=active_job)


def start_batch(
    storage: ProjectStorage,
    project_id: object,
    *,
    scene_ids: list[str] | None = None,
    client: object | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    package_loader: Callable | None = None,
    compatibility_validator: Callable | None = None,
    hardware_gate: object | None = None,
    hardware_supported: bool | None = None,
    media_adapter: object | None = None,
    raw_output_root: Path | None = None,
    finalizer: Callable[..., Mapping[str, object]] = finalize_render_job,
    preparer: Callable[..., Mapping[str, object]] = prepare_render_scene,
    origin_batch_id: str | None = None,
    start_runner: bool = True,
    poll_interval_seconds: float = 1.0,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        ensure_batch_recovery(
            storage,
            canonical_project_id,
            client=client,
            preflight_builder=preflight_builder,
            package_loader=package_loader,
            compatibility_validator=compatibility_validator,
            hardware_gate=hardware_gate,
            media_adapter=media_adapter,
            raw_output_root=raw_output_root,
            finalizer=finalizer,
            preparer=preparer,
        )
        store = BatchStore(storage)
        active = store.load_active(canonical_project_id)
        if active is not None:
            raise RenderBatchConflict(
                "BATCH_ACTIVE",
                "A batch is already active for this project. Resume or end it before starting another.",
                details={"state": active["state"]},
            )
        active_job = _active_project_job(storage, canonical_project_id)
        if active_job is not None:
            raise RenderBatchConflict(
                "PROJECT_JOB_ACTIVE",
                "Wait for the active scene render to finish or cancel it before starting a batch.",
                details={"scene_id": active_job.get("scene_id")},
            )
        project = storage.load_project(canonical_project_id)
        preflight = dict(preflight_builder(project, storage))
        systemic = None
        reconciled = _reconcile_for_preview(storage, canonical_project_id, client, raw_output_root)
        plan = plan_batch(
            storage,
            canonical_project_id,
            scene_ids=scene_ids,
            preflight=preflight,
            jobs=reconciled["jobs"],
            raw_output_root=raw_output_root,
            hardware_supported=hardware_supported,
        )
        if scene_ids is None:
            if preflight.get("workflow_ready") is not True:
                systemic = ("WORKFLOW_CONTRACT_INVALID", "Batch paused: the production workflow contract is not ready.")
            elif preflight.get("runtime_requirements_ready") is not True:
                systemic = ("RUNTIME_REQUIREMENTS_CHANGED", "Batch paused: runtime requirements changed.")
        else:
            preflight_by_scene = {
                entry.get("scene_id"): entry
                for entry in preflight.get("scenes", [])
                if isinstance(entry, Mapping)
            }
            eligible_scene_ids = {
                entry.get("scene_id")
                for entry in plan.get("scenes", [])
                if isinstance(entry, Mapping) and entry.get("action") in ELIGIBLE_BATCH_ACTIONS
            }
            for selected_scene_id in eligible_scene_ids:
                scene_preflight = preflight_by_scene.get(selected_scene_id)
                if not isinstance(scene_preflight, Mapping) or scene_preflight.get("workflow_ready") is not True:
                    systemic = ("WORKFLOW_CONTRACT_INVALID", "Batch paused: the selected scene workflow contract is not ready.")
                    break
                if scene_preflight.get("runtime_requirements_ready") is not True:
                    systemic = ("RUNTIME_REQUIREMENTS_CHANGED", "Batch paused: the selected scene runtime requirements are not ready.")
                    break
        eligible_entries = [entry for entry in plan["scenes"] if entry["action"] in ELIGIBLE_BATCH_ACTIONS]
        if not eligible_entries:
            raise RenderBatchError(
                "BATCH_NO_ELIGIBLE_SCENES",
                "No scenes are currently ready for batch production.",
                details={"counts": plan["counts"]},
            )
        items = [
            {
                "scene_id": entry["scene_id"],
                "sequence": entry["sequence"],
                "action": entry["action"],
                "disposition": ITEM_PENDING,
                "reason_code": None,
                "job_id": None,
                "failure": None,
                "updated_at": _timestamp(),
            }
            for entry in eligible_entries
        ]
        upscale_method = resolve_project_production_method(project)
        production_profile = {"upscale_method": upscale_method}
        record = new_batch_record(
            canonical_project_id,
            mode="selected" if scene_ids is not None else "all_ready",
            items=items,
            origin_batch_id=origin_batch_id,
            production_profile=production_profile,
        )
        record = store.save(record)
        LOGGER.info(
            "Batch started project_id=%s batch_id=%s scene_count=%s mode=%s",
            canonical_project_id,
            record["batch_id"],
            len(items),
            record["mode"],
        )
        if systemic is not None:
            record["state"] = BATCH_PAUSED
            record["attention"] = {"code": systemic[0], "message": systemic[1], "at": _timestamp()}
            record = store.save(record)
        elif start_runner:
            runner = _get_runner(
                storage,
                canonical_project_id,
                client=client,
                preflight_builder=preflight_builder,
                package_loader=package_loader,
                compatibility_validator=compatibility_validator,
                hardware_gate=hardware_gate,
                media_adapter=media_adapter,
                raw_output_root=raw_output_root,
                finalizer=finalizer,
                preparer=preparer,
            )
            runner.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
            runner.start_thread()
            runner.wake()
        return {"batch": public_batch_record(record), "preview": build_batch_preview_payload(plan, active_batch=record, active_job=None)}


def batch_status(
    storage: ProjectStorage,
    project_id: object,
    *,
    client: object | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    raw_output_root: Path | None = None,
    include_terminal: bool = True,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    ensure_batch_recovery(
        storage,
        canonical_project_id,
        client=client,
        preflight_builder=preflight_builder,
        raw_output_root=raw_output_root,
    )
    store = BatchStore(storage)
    record = store.load_active(canonical_project_id)
    if record is None and include_terminal:
        records = store.list_project(canonical_project_id)
        record = records[-1] if records else None
    return {"batch": public_batch_record(record)}


def pause_batch_after_current(storage: ProjectStorage, project_id: object) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        store = BatchStore(storage)
        record = store.load_active(canonical_project_id)
        if record is None:
            raise RenderBatchNotFound("BATCH_NOT_FOUND", "No active batch was found.")
        if record["state"] not in EXECUTING_BATCH_STATES:
            raise RenderBatchConflict("BATCH_NOT_RUNNING", "Only a running batch can be paused.")
        record["state"] = BATCH_PAUSE_REQUESTED
        record["pause_requested"] = True
        record = store.save(record)
        LOGGER.info("Batch pause after current requested project_id=%s", canonical_project_id)
        return {"batch": public_batch_record(record)}


def resume_batch(
    storage: ProjectStorage,
    project_id: object,
    *,
    client: object | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    package_loader: Callable | None = None,
    compatibility_validator: Callable | None = None,
    hardware_gate: object | None = None,
    media_adapter: object | None = None,
    raw_output_root: Path | None = None,
    finalizer: Callable[..., Mapping[str, object]] = finalize_render_job,
    preparer: Callable[..., Mapping[str, object]] = prepare_render_scene,
    start_runner: bool = True,
    poll_interval_seconds: float = 1.0,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        ensure_batch_recovery(
            storage,
            canonical_project_id,
            client=client,
            preflight_builder=preflight_builder,
            package_loader=package_loader,
            compatibility_validator=compatibility_validator,
            hardware_gate=hardware_gate,
            media_adapter=media_adapter,
            raw_output_root=raw_output_root,
            finalizer=finalizer,
            preparer=preparer,
        )
        store = BatchStore(storage)
        record = store.load_active(canonical_project_id)
        if record is None:
            raise RenderBatchNotFound("BATCH_NOT_FOUND", "No active batch was found.")
        if record["state"] not in {BATCH_PAUSED, BATCH_PAUSED_RECOVERY}:
            raise RenderBatchConflict("BATCH_NOT_PAUSED", "Only a paused batch can be resumed.")
        # Revalidate runtime and project state before dispatching again.
        active_job = _active_project_job(storage, canonical_project_id)
        if active_job is not None:
            raise RenderBatchConflict(
                "PROJECT_JOB_ACTIVE",
                "Wait for the active scene render to finish or cancel it before resuming the batch.",
                details={"scene_id": active_job.get("scene_id")},
            )
        project = storage.load_project(canonical_project_id)
        preflight = dict(preflight_builder(project, storage))
        if preflight.get("workflow_ready") is not True:
            raise RenderBatchConflict("WORKFLOW_CONTRACT_INVALID", "Batch cannot resume: the production workflow contract is not ready.")
        if preflight.get("runtime_requirements_ready") is not True:
            raise RenderBatchConflict("RUNTIME_REQUIREMENTS_CHANGED", "Batch cannot resume: runtime requirements changed.")
        upscale_method = record.get("production_profile", {}).get("upscale_method", "none")
        if upscale_method != POSTPROCESS_METHOD_NONE:
            avail = production_method_availability(upscale_method)
            if avail.get("state") == "UNAVAILABLE":
                raise RenderBatchConflict(
                    "PRODUCTION_METHOD_UNAVAILABLE",
                    "Batch cannot resume: the selected production upscaler is unavailable on this runtime.",
                    details=avail,
                )
            if avail.get("state") == "UNQUALIFIED":
                raise RenderBatchConflict(
                    "UPSCALER_UNQUALIFIED",
                    "Batch cannot resume: the selected upscaler requires qualification before batch production.",
                    details=avail,
                )
        has_work = any(
            isinstance(item, Mapping)
            and (item.get("disposition") == ITEM_PENDING or item.get("disposition") in ACTIVE_ITEM_DISPOSITIONS)
            for item in record.get("items", [])
        )
        if not has_work:
            record = _finalize_terminal_state(record)
            record = store.save(record)
            return {"batch": public_batch_record(record)}
        record["state"] = BATCH_RUNNING
        record["pause_requested"] = False
        record["attention"] = None
        record = store.save(record)
        LOGGER.info("Batch resumed project_id=%s", canonical_project_id)
        if start_runner:
            runner = _get_runner(
                storage,
                canonical_project_id,
                client=client,
                preflight_builder=preflight_builder,
                package_loader=package_loader,
                compatibility_validator=compatibility_validator,
                hardware_gate=hardware_gate,
                media_adapter=media_adapter,
                raw_output_root=raw_output_root,
                finalizer=finalizer,
                preparer=preparer,
            )
            runner.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
            runner.start_thread()
            runner.wake()
        return {"batch": public_batch_record(record)}


def _finalize_terminal_state(record: dict[str, object]) -> dict[str, object]:
    counts = batch_counts(record)
    has_issues = counts["failed"] > 0 or counts["cancelled"] > 0
    record["state"] = BATCH_COMPLETED_WITH_ISSUES if has_issues else BATCH_COMPLETED
    record["current_index"] = None
    record["completed_at"] = _timestamp()
    return record


def end_batch(storage: ProjectStorage, project_id: object) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        store = BatchStore(storage)
        record = store.load_active(canonical_project_id)
        if record is None:
            raise RenderBatchNotFound("BATCH_NOT_FOUND", "No active batch was found.")
        if record["state"] not in {BATCH_PAUSED, BATCH_PAUSED_RECOVERY}:
            raise RenderBatchConflict("BATCH_NOT_PAUSED", "Only a paused batch can be ended.")
        record["state"] = BATCH_ENDED
        record["pause_requested"] = False
        record["current_index"] = None
        record["completed_at"] = _timestamp()
        record = store.save(record)
        LOGGER.info("Batch ended project_id=%s", canonical_project_id)
        return {"batch": public_batch_record(record)}


def retry_failed_batch(
    storage: ProjectStorage,
    project_id: object,
    *,
    client: object | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    package_loader: Callable | None = None,
    compatibility_validator: Callable | None = None,
    hardware_gate: object | None = None,
    media_adapter: object | None = None,
    raw_output_root: Path | None = None,
    finalizer: Callable[..., Mapping[str, object]] = finalize_render_job,
    preparer: Callable[..., Mapping[str, object]] = prepare_render_scene,
    start_runner: bool = True,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    with _batch_project_lock(canonical_project_id):
        ensure_batch_recovery(
            storage,
            canonical_project_id,
            client=client,
            preflight_builder=preflight_builder,
            package_loader=package_loader,
            compatibility_validator=compatibility_validator,
            hardware_gate=hardware_gate,
            media_adapter=media_adapter,
            raw_output_root=raw_output_root,
            finalizer=finalizer,
            preparer=preparer,
        )
        store = BatchStore(storage)
        active = store.load_active(canonical_project_id)
        if active is not None:
            raise RenderBatchConflict(
                "BATCH_ACTIVE",
                "A batch is already active for this project. Resume or end it before retrying failed scenes.",
                details={"state": active["state"]},
            )
        records = store.list_project(canonical_project_id)
        origin = None
        for candidate in reversed(records):
            if candidate["state"] in TERMINAL_BATCH_STATES and any(
                isinstance(item, Mapping) and item.get("disposition") in RETRYABLE_ITEM_DISPOSITIONS
                for item in candidate.get("items", [])
            ):
                origin = candidate
                break
        if origin is None:
            raise RenderBatchError("BATCH_RETRY_UNAVAILABLE", "No completed batch has retryable scenes.")
        retry_scene_ids = [
            str(item["scene_id"])
            for item in origin.get("items", [])
            if isinstance(item, Mapping) and item.get("disposition") in RETRYABLE_ITEM_DISPOSITIONS
        ]
        # The old batch history is never mutated; a new run revalidates every
        # scene against current project state and receives fresh Builder jobs.
        return start_batch(
            storage,
            canonical_project_id,
            scene_ids=retry_scene_ids,
            client=client,
            preflight_builder=preflight_builder,
            package_loader=package_loader,
            compatibility_validator=compatibility_validator,
            hardware_gate=hardware_gate,
            media_adapter=media_adapter,
            raw_output_root=raw_output_root,
            finalizer=finalizer,
            preparer=preparer,
            origin_batch_id=str(origin["batch_id"]),
            start_runner=start_runner,
        )


def manual_action_blocker(storage: ProjectStorage, project_id: object, *, check_upscaler: bool = False) -> str | None:
    """Truthful gate for manual per-scene production actions during a batch or unavailable upscaler."""

    canonical_project_id = validate_project_id(project_id)
    record = BatchStore(storage).load_active(canonical_project_id)
    if record is not None and record["state"] in EXECUTING_BATCH_STATES:
        return "A batch render is active. Pause or end the batch before rendering scenes manually."
    if check_upscaler:
        project = storage.load_project(canonical_project_id)
        method = resolve_project_production_method(project)
        if method != POSTPROCESS_METHOD_NONE:
            avail = production_method_availability(method)
            if avail.get("state") == "UNAVAILABLE":
                return "The selected production upscaler is unavailable on this runtime."
    return None
