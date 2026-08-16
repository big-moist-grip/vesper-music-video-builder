"""Phase 8B queue, lifecycle, recovery, and raw-output boundary.

The Render preparation package remains the only source of a production
workflow.  This module adds the smallest execution boundary around that
package: a local ComfyUI API adapter, Builder-owned durable job records, a
truthful lifecycle state machine, reconciliation, cancellation, and output
discovery.  It deliberately does not contain H3 execution code, quality
tuning, later media transformation, or export assembly.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
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
from .prompt_service import prompt_scene_state
from .render import (
    RenderPreparationError,
    _artifact_path,
    _preparation_artifacts_current,
    _read_preparation_metadata,
    _root_path,
    build_render_preflight,
    validate_compiled_workflow_contract,
)
from .workflows import production_manifest_registry


LOGGER = logging.getLogger(__name__)

JOB_SCHEMA_VERSION = 1
JOB_DIRECTORY = "jobs"
JOB_FILENAME_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$")
PROMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

READY_TO_SUBMIT = "READY_TO_SUBMIT"
SUBMITTING = "SUBMITTING"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCELLED = "CANCELLED"
INTERRUPTED = "INTERRUPTED"
UNKNOWN = "UNKNOWN"

JOB_STATES = (
    READY_TO_SUBMIT,
    SUBMITTING,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    FAILED,
    CANCEL_REQUESTED,
    CANCELLED,
    INTERRUPTED,
    UNKNOWN,
)
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED})
ACTIVE_STATES = frozenset({READY_TO_SUBMIT, SUBMITTING, QUEUED, RUNNING, CANCEL_REQUESTED, UNKNOWN})

LEGAL_TRANSITIONS = {
    READY_TO_SUBMIT: frozenset({SUBMITTING, UNKNOWN}),
    SUBMITTING: frozenset({QUEUED, FAILED, CANCEL_REQUESTED, UNKNOWN}),
    QUEUED: frozenset({RUNNING, SUCCEEDED, FAILED, CANCEL_REQUESTED, UNKNOWN}),
    RUNNING: frozenset({SUCCEEDED, FAILED, CANCEL_REQUESTED, UNKNOWN}),
    CANCEL_REQUESTED: frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, UNKNOWN, QUEUED, RUNNING}),
    UNKNOWN: frozenset({QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    INTERRUPTED: frozenset(),
}

LOCAL_API_DEFAULT_HOST = "127.0.0.1"
LOCAL_API_DEFAULT_PORT = 8188
LOCAL_API_TIMEOUT_MAX_SECONDS = 60.0
OUTPUT_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})

_LOCKS_LOCK = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scene_lock(project_id: str, scene_id: str) -> threading.RLock:
    key = f"{project_id}:{scene_id}"
    with _LOCKS_LOCK:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _project_lock(project_id: str) -> threading.RLock:
    key = f"project:{project_id}"
    with _LOCKS_LOCK:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[key] = lock
        return lock


class RenderJobError(Exception):
    """Base class for product-facing queue and lifecycle failures."""

    status = 422

    def __init__(self, code: str, message: str, *, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class RenderExecutionBlocked(RenderJobError):
    """The prepared scene is not currently eligible for execution."""

    status = 422


class RenderExecutionDeferred(RenderJobError):
    """Execution is intentionally deferred by target-hardware policy."""

    status = 409


class RenderJobConflict(RenderJobError):
    """A scene already has a live or unreconciled Builder job."""

    status = 409


class RenderJobNotFound(RenderJobError):
    status = 404


class RenderJobStateError(RenderJobError):
    status = 409


class RenderJobSubmissionError(RenderJobError):
    status = 502

    def __init__(self, code: str, message: str, *, job_id: str, details: Mapping[str, object] | None = None):
        super().__init__(code, message, details={"job_id": job_id, **dict(details or {})})
        self.job_id = job_id


class RenderOutputDiscoveryError(RenderJobError):
    status = 502


class ComfyUIClientError(RenderJobError):
    status = 502

    def __init__(self, code: str, message: str, *, transient: bool = False, details: Mapping[str, object] | None = None):
        super().__init__(code, message, details=details)
        self.transient = transient


@dataclass(frozen=True)
class TargetHardwareQualification:
    """The separate target-runtime qualification dimension."""

    qualified: bool
    status: str
    message: str
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "qualified": self.qualified,
            "status": self.status,
            "message": self.message,
            "evidence": self.evidence,
        }


def default_target_hardware_qualification() -> TargetHardwareQualification:
    """Return the safe default until the target production host is qualified."""

    return TargetHardwareQualification(
        qualified=False,
        status="DEFERRED_TARGET_NVIDIA",
        message="Render execution is deferred until the RTX 4080 SUPER target is qualified.",
        evidence="target_runtime_qualification_deferred",
    )


class TargetHardwareGate:
    """Central execution gate; no ComfyUI request is made before it passes."""

    def __init__(self, qualification_provider: Callable[[], TargetHardwareQualification | Mapping[str, object]] | None = None):
        self.qualification_provider = qualification_provider or default_target_hardware_qualification

    def qualification(self) -> TargetHardwareQualification:
        value = self.qualification_provider()
        if isinstance(value, TargetHardwareQualification):
            return value
        if isinstance(value, Mapping):
            qualified = value.get("qualified") is True
            status = value.get("status") if isinstance(value.get("status"), str) else "UNKNOWN"
            message = value.get("message") if isinstance(value.get("message"), str) else "Target hardware qualification is unavailable."
            evidence = value.get("evidence") if isinstance(value.get("evidence"), str) else "unavailable"
            return TargetHardwareQualification(qualified, status, message, evidence)
        raise RenderExecutionBlocked("TARGET_HARDWARE_RESULT_INVALID", "Target hardware qualification returned an invalid result.")

    def inspect(self, scene: Mapping[str, object]) -> dict[str, object]:
        blockers: list[dict[str, str]] = []
        dimensions = (
            ("content_ready", "CONTENT_NOT_READY", "Content and prompt inputs are not ready."),
            ("workflow_ready", "WORKFLOW_NOT_READY", "The production workflow contract is not ready."),
            ("runtime_requirements_ready", "RUNTIME_REQUIREMENTS_NOT_READY", "Runtime requirements are not ready."),
            ("preparation_current", "PREPARATION_NOT_CURRENT", "Current render preparation is required before execution."),
        )
        for field, code, message in dimensions:
            if scene.get(field) is not True:
                blockers.append({"code": code, "message": message, "layer": "execution"})
        qualification = self.qualification()
        allowed = not blockers and qualification.qualified
        status = "READY" if allowed else "BLOCKED" if blockers else qualification.status
        message = "Target H3 execution is ready." if allowed else (
            "Render execution is blocked by current scene readiness." if blockers else qualification.message
        )
        return {
            "allowed": allowed,
            "status": status,
            "message": message,
            "blockers": blockers,
            "target_hardware": qualification.as_dict(),
        }

    def require(self, scene: Mapping[str, object]) -> dict[str, object]:
        gate = self.inspect(scene)
        if gate["blockers"]:
            raise RenderExecutionBlocked(
                "RENDER_EXECUTION_BLOCKED",
                "Render execution is blocked by current scene readiness.",
                details={"execution_gate": gate},
            )
        if not gate["target_hardware"]["qualified"]:
            raise RenderExecutionDeferred(
                "TARGET_HARDWARE_DEFERRED",
                gate["message"],
                details={"execution_gate": gate},
            )
        return gate


def _canonical_prompt_id(value: object) -> str:
    if not isinstance(value, str) or not PROMPT_ID_PATTERN.fullmatch(value):
        raise ComfyUIClientError("COMFYUI_PROMPT_ID_INVALID", "ComfyUI returned an invalid prompt ID.")
    return value


def _normalise_loopback_address(address: str) -> str:
    value = address.strip().strip("[]")
    if value in {"0.0.0.0", "::", ""}:
        return LOCAL_API_DEFAULT_HOST
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        if value.lower() == "localhost":
            return LOCAL_API_DEFAULT_HOST
        raise ComfyUIClientError("COMFYUI_HOST_NOT_LOCAL", "Only the local ComfyUI API is permitted.")
    if not parsed.is_loopback:
        raise ComfyUIClientError("COMFYUI_HOST_NOT_LOCAL", "Only the local ComfyUI API is permitted.")
    return value


def _resolve_local_comfyui_url(base_url: str | None) -> str:
    if base_url is None:
        address = LOCAL_API_DEFAULT_HOST
        port = LOCAL_API_DEFAULT_PORT
        try:
            from server import PromptServer  # type: ignore

            instance = getattr(PromptServer, "instance", None)
            address = str(getattr(instance, "address", address) or address)
            port = int(getattr(instance, "port", port) or port)
        except (ImportError, AttributeError, TypeError, ValueError):
            try:
                from comfy.cli_args import args  # type: ignore

                address = str(getattr(args, "listen", address) or address)
                port = int(getattr(args, "port", port) or port)
            except (ImportError, AttributeError, TypeError, ValueError):
                pass
        address = _normalise_loopback_address(address)
        return f"http://{address}:{port}"

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ComfyUIClientError("COMFYUI_URL_INVALID", "The ComfyUI API URL is invalid.")
    if parsed.query or parsed.fragment:
        raise ComfyUIClientError("COMFYUI_URL_INVALID", "The ComfyUI API URL may not include a query or fragment.")
    host = _normalise_loopback_address(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ComfyUIClientError("COMFYUI_URL_INVALID", "The ComfyUI API port is invalid.")
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _allowed_api_path(path: str) -> bool:
    return path in {"/prompt", "/queue", "/interrupt", "/history"} or bool(
        re.fullmatch(r"/history/[A-Za-z0-9._-]{1,128}", path)
    )


class ComfyUIClient:
    """Small, local-only adapter for the installed ComfyUI core API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: Callable[[str, str, object | None], Mapping[str, object]] | None = None,
    ):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ComfyUIClientError("COMFYUI_TIMEOUT_INVALID", "ComfyUI request timeout is invalid.")
        if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= LOCAL_API_TIMEOUT_MAX_SECONDS:
            raise ComfyUIClientError("COMFYUI_TIMEOUT_INVALID", "ComfyUI request timeout must be bounded.")
        self.base_url = _resolve_local_comfyui_url(base_url)
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport
        self.request_log: list[tuple[str, str, object | None]] = []

    def _request_json(self, method: str, path: str, payload: object | None = None) -> dict[str, object]:
        if not _allowed_api_path(path):
            raise ComfyUIClientError("COMFYUI_API_PATH_INVALID", "The requested ComfyUI API path is not supported.")
        self.request_log.append((method, path, deepcopy(payload)))
        if self.transport is not None:
            try:
                result = self.transport(method, path, deepcopy(payload))
            except ComfyUIClientError:
                raise
            except Exception as error:  # pragma: no cover - defensive transport boundary
                raise ComfyUIClientError("COMFYUI_TRANSPORT_FAILED", "The local ComfyUI request failed.", transient=True) from error
            if not isinstance(result, Mapping):
                raise ComfyUIClientError("COMFYUI_RESPONSE_INVALID", "ComfyUI returned an invalid JSON response.")
            return dict(result)

        url = f"{self.base_url}{path}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            transient = error.code >= 500 or error.code == 429
            raise ComfyUIClientError(
                "COMFYUI_HTTP_ERROR",
                f"ComfyUI returned HTTP {error.code}.",
                transient=transient,
                details={"http_status": error.code},
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            raise ComfyUIClientError("COMFYUI_UNAVAILABLE", "The local ComfyUI API could not be reached.", transient=True) from error
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ComfyUIClientError("COMFYUI_RESPONSE_INVALID", "ComfyUI returned invalid JSON.") from error
        if not isinstance(document, dict):
            raise ComfyUIClientError("COMFYUI_RESPONSE_INVALID", "ComfyUI returned an invalid JSON object.")
        return document

    def submit_prompt(self, workflow: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(workflow, Mapping) or not workflow:
            raise ComfyUIClientError("WORKFLOW_INVALID", "Only a prepared production workflow may be submitted.")
        response = self._request_json("POST", "/prompt", {"prompt": deepcopy(dict(workflow))})
        prompt_id = _canonical_prompt_id(response.get("prompt_id"))
        return {
            "prompt_id": prompt_id,
            "queue_number": response.get("number"),
            "node_errors": deepcopy(response.get("node_errors", {})),
        }

    def get_queue(self) -> dict[str, object]:
        return self._request_json("GET", "/queue")

    def get_history(self, prompt_id: str) -> dict[str, object]:
        canonical = _canonical_prompt_id(prompt_id)
        return self._request_json("GET", f"/history/{canonical}")

    def delete_queued(self, prompt_id: str) -> dict[str, object]:
        canonical = _canonical_prompt_id(prompt_id)
        return self._request_json("POST", "/queue", {"delete": [canonical]})

    def interrupt_running(self, prompt_id: str) -> dict[str, object]:
        canonical = _canonical_prompt_id(prompt_id)
        return self._request_json("POST", "/interrupt", {"prompt_id": canonical})

    def cancellation_capabilities(self) -> dict[str, object]:
        return {
            "queued": True,
            "running": True,
            "running_scope": "global_engine_interrupt_targeted_by_prompt_id",
            "meaning": "A running cancellation targets the owned prompt ID through ComfyUI's global engine interrupt API.",
        }


def _validate_job_id(job_id: object) -> str:
    if not isinstance(job_id, str):
        raise ProjectValidationError("Job ID must be a UUID string.")
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, AttributeError) as error:
        raise ProjectValidationError("Job ID must be a valid UUID.") from error
    canonical = str(parsed)
    if canonical != job_id:
        raise ProjectValidationError("Job ID must use canonical UUID form.")
    return canonical


def _job_directory(storage: ProjectStorage, project_id: str, scene_id: str) -> Path:
    root = storage.project_directory(project_id).resolve(strict=True)
    render_directory = root / "renders" / scene_id
    jobs = render_directory / JOB_DIRECTORY
    if render_directory.exists() and render_directory.is_symlink():
        raise RenderJobError("JOB_PATH_UNSAFE", "The scene render directory is a symlink.")
    if jobs.exists() and jobs.is_symlink():
        raise RenderJobError("JOB_PATH_UNSAFE", "The scene job directory is a symlink.")
    jobs.mkdir(parents=True, exist_ok=True)
    return jobs


def _job_path(storage: ProjectStorage, project_id: str, scene_id: str, job_id: str) -> Path:
    directory = _job_directory(storage, project_id, scene_id)
    path = directory / f"{job_id}.json"
    if path.is_symlink():
        raise RenderJobError("JOB_PATH_UNSAFE", "The job record is a symlink.")
    return path


def _validate_job_record(record: Mapping[str, object], *, project_id: str | None = None, scene_id: str | None = None) -> dict[str, object]:
    if not isinstance(record, Mapping):
        raise RenderJobError("JOB_RECORD_INVALID", "The durable render job record is invalid.")
    result = deepcopy(dict(record))
    if result.get("job_schema_version") != JOB_SCHEMA_VERSION:
        raise RenderJobError("JOB_RECORD_VERSION_UNSUPPORTED", "The durable render job record version is unsupported.")
    canonical_job_id = _validate_job_id(result.get("job_id"))
    canonical_project_id = validate_project_id(result.get("project_id"))
    canonical_scene_id = validate_entity_id(result.get("scene_id"), "Scene ID")
    if project_id is not None and canonical_project_id != project_id:
        raise RenderJobError("JOB_OWNERSHIP_MISMATCH", "The render job does not belong to this project.")
    if scene_id is not None and canonical_scene_id != scene_id:
        raise RenderJobError("JOB_OWNERSHIP_MISMATCH", "The render job does not belong to this scene.")
    if result.get("state") not in JOB_STATES:
        raise RenderJobError("JOB_STATE_INVALID", "The durable render job state is invalid.")
    if not isinstance(result.get("generation_method"), str) or not isinstance(result.get("preparation_fingerprint"), str):
        raise RenderJobError("JOB_RECORD_INVALID", "The durable render job identity is incomplete.")
    result["job_id"] = canonical_job_id
    result["project_id"] = canonical_project_id
    result["scene_id"] = canonical_scene_id
    return result


class JobStore:
    """Durable project-owned job metadata outside schema-v7 project JSON."""

    def __init__(self, storage: ProjectStorage):
        self.storage = storage

    def save(self, record: Mapping[str, object]) -> dict[str, object]:
        normalized = _validate_job_record(record)
        path = _job_path(self.storage, normalized["project_id"], normalized["scene_id"], normalized["job_id"])
        if path.is_file() and not path.is_symlink():
            try:
                existing_document = json.loads(path.read_text(encoding="utf-8"))
                existing = _validate_job_record(existing_document)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RenderJobError("JOB_RECORD_INVALID", "The existing durable render job record could not be read.") from error
            if existing["state"] in TERMINAL_STATES and normalized != existing:
                raise RenderJobStateError("JOB_TERMINAL_IMMUTABLE", "A terminal render job record cannot be mutated.")
        try:
            atomic_write_json(path, normalized)
        except ProjectPersistenceError:
            raise
        except OSError as error:
            raise ProjectPersistenceError("Could not persist the render job record atomically.") from error
        return deepcopy(normalized)

    def load(self, project_id: object, job_id: object) -> dict[str, object]:
        canonical_project_id = validate_project_id(project_id)
        canonical_job_id = _validate_job_id(job_id)
        root = self.storage.project_directory(canonical_project_id).resolve(strict=True)
        renders = root / "renders"
        if not renders.is_dir() or renders.is_symlink():
            raise RenderJobNotFound("JOB_NOT_FOUND", "Render job was not found.")
        for scene_directory in renders.iterdir():
            if not scene_directory.is_dir() or scene_directory.is_symlink():
                continue
            path = scene_directory / JOB_DIRECTORY / f"{canonical_job_id}.json"
            if not path.is_file() or path.is_symlink():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RenderJobError("JOB_RECORD_INVALID", "The durable render job record could not be read.") from error
            return _validate_job_record(record, project_id=canonical_project_id)
        raise RenderJobNotFound("JOB_NOT_FOUND", "Render job was not found.")

    def list_scene(self, project_id: object, scene_id: object) -> list[dict[str, object]]:
        canonical_project_id = validate_project_id(project_id)
        canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
        directory = _job_directory(self.storage, canonical_project_id, canonical_scene_id)
        records: list[dict[str, object]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.is_symlink() or not JOB_FILENAME_PATTERN.fullmatch(path.name):
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                records.append(_validate_job_record(document, project_id=canonical_project_id, scene_id=canonical_scene_id))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RenderJobError("JOB_RECORD_INVALID", "A durable render job record could not be read.") from error
        return sorted(records, key=lambda item: (str(item.get("created_at", "")), str(item["job_id"])))

    def list_project(self, project_id: object) -> list[dict[str, object]]:
        canonical_project_id = validate_project_id(project_id)
        root = self.storage.project_directory(canonical_project_id).resolve(strict=True)
        renders = root / "renders"
        if not renders.is_dir() or renders.is_symlink():
            return []
        records: list[dict[str, object]] = []
        for scene_directory in sorted(renders.iterdir(), key=lambda item: item.name):
            if not scene_directory.is_dir() or scene_directory.is_symlink():
                continue
            jobs = scene_directory / JOB_DIRECTORY
            if not jobs.is_dir() or jobs.is_symlink():
                continue
            records.extend(self.list_scene(canonical_project_id, scene_directory.name))
        return sorted(records, key=lambda item: (str(item.get("created_at", "")), str(item["job_id"])))

    def find(self, project_id: object, job_id: object) -> dict[str, object]:
        return self.load(project_id, job_id)


def new_job_record(
    project_id: str,
    scene_id: str,
    generation_method: str,
    preparation_fingerprint: str,
    *,
    output_node_id: str,
    expected_filename_prefix: str,
    output_format: str | None = None,
    retry_of_job_id: str | None = None,
    now: str | None = None,
) -> dict[str, object]:
    timestamp = now or _timestamp()
    return {
        "job_schema_version": JOB_SCHEMA_VERSION,
        "job_id": str(uuid.uuid4()),
        "project_id": validate_project_id(project_id),
        "scene_id": validate_entity_id(scene_id, "Scene ID"),
        "generation_method": generation_method,
        "preparation_fingerprint": preparation_fingerprint,
        "state": READY_TO_SUBMIT,
        "created_at": timestamp,
        "updated_at": timestamp,
        "submitted_at": None,
        "comfy_prompt_id": None,
        "queue_number": None,
        "progress": None,
        "failure": None,
        "output": None,
        "cancel": None,
        "last_poll_error": None,
        "last_reconciled_at": None,
        "production_output": {
            "node_id": output_node_id,
            "filename_prefix": expected_filename_prefix,
            "format": output_format,
            "raw_h3_only": True,
        },
        "retry_of_job_id": retry_of_job_id,
        "transitions": [],
    }


def transition_job(
    record: dict[str, object],
    new_state: str,
    *,
    reason: str,
    now: str | None = None,
    updates: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized = _validate_job_record(record)
    old_state = normalized["state"]
    if new_state not in JOB_STATES:
        raise RenderJobStateError("JOB_STATE_INVALID", "The requested render job state is invalid.")
    if old_state == new_state:
        if old_state in TERMINAL_STATES and updates:
            raise RenderJobStateError("JOB_TERMINAL_IMMUTABLE", "A terminal render job record cannot be mutated.")
        if updates:
            normalized.update(deepcopy(dict(updates)))
        return normalized
    if new_state not in LEGAL_TRANSITIONS[old_state]:
        raise RenderJobStateError(
            "JOB_TRANSITION_INVALID",
            f"Render job cannot transition from {old_state} to {new_state}.",
            details={"from": old_state, "to": new_state},
        )
    timestamp = now or _timestamp()
    normalized["state"] = new_state
    normalized["updated_at"] = timestamp
    if updates:
        normalized.update(deepcopy(dict(updates)))
    transitions = normalized.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions.append({"from": old_state, "to": new_state, "at": timestamp, "reason": reason})
    normalized["transitions"] = transitions[-50:]
    return normalized


def _scene_from_preflight(preflight: Mapping[str, object], scene_id: str) -> dict[str, object]:
    scenes = preflight.get("scenes") if isinstance(preflight, Mapping) else None
    if isinstance(scenes, list):
        for scene in scenes:
            if isinstance(scene, Mapping) and scene.get("scene_id") == scene_id:
                return dict(scene)
    if preflight.get("scene_id") == scene_id:
        return dict(preflight)
    raise RenderExecutionBlocked("SCENE_PREFLIGHT_MISSING", "Current scene readiness could not be evaluated.")


def _load_prepared_package(
    storage: ProjectStorage,
    project: dict[str, object],
    scene_id: str,
    scene_preflight: Mapping[str, object],
) -> dict[str, object]:
    _, root = _root_path(storage, project["project_id"])
    metadata = _read_preparation_metadata(root, scene_id)
    if metadata is None or not _preparation_artifacts_current(root, metadata):
        raise RenderExecutionBlocked("PREPARATION_NOT_CURRENT", "Current render preparation is required before execution.")
    expected_fingerprint = scene_preflight.get("preparation_fingerprint")
    if not isinstance(expected_fingerprint, str) or metadata.get("preparation_fingerprint") != expected_fingerprint:
        raise RenderExecutionBlocked("PREPARATION_STALE", "Render preparation no longer matches current scene inputs.")
    method = metadata.get("generation_method")
    if not isinstance(method, str):
        raise RenderExecutionBlocked("PREPARATION_INVALID", "Prepared generation method is invalid.")
    workflow_metadata = metadata.get("workflow")
    timing = metadata.get("timing")
    visual_inputs = metadata.get("visual_inputs")
    source_audio = metadata.get("source_audio")
    if not isinstance(workflow_metadata, Mapping) or not isinstance(timing, Mapping) or not isinstance(visual_inputs, list) or not isinstance(source_audio, Mapping):
        raise RenderExecutionBlocked("PREPARATION_INVALID", "Prepared render metadata is incomplete.")
    workflow_path = _artifact_path(root, workflow_metadata.get("relative_path"))
    if workflow_path is None:
        raise RenderExecutionBlocked("PREPARATION_WORKFLOW_MISSING", "The prepared production workflow is missing.")
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RenderExecutionBlocked("PREPARATION_WORKFLOW_INVALID", "The prepared production workflow could not be read.") from error
    if not isinstance(workflow, dict):
        raise RenderExecutionBlocked("PREPARATION_WORKFLOW_INVALID", "The prepared production workflow is invalid.")
    try:
        prompt_state = prompt_scene_state(
            project,
            scene_id,
            storage=storage,
            generation_method=method,
            include_visual_readiness=False,
        )
    except (ProjectValidationError, ProjectNotFoundError, RenderPreparationError) as error:
        raise RenderExecutionBlocked("PROMPT_STATE_INVALID", "Current Final Prompt state could not be verified.") from error
    if prompt_state.get("status") != "current" or not isinstance(prompt_state.get("saved_final_prompt"), str):
        raise RenderExecutionBlocked("PROMPT_NOT_CURRENT", "CURRENT Final Prompt provenance is required before execution.")
    registry = production_manifest_registry()
    manifest = registry.get(method)
    if not isinstance(manifest, Mapping):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared generation method has no production manifest.")
    output_mapping = manifest.get("inputs", {}).get("output") if isinstance(manifest.get("inputs"), Mapping) else None
    prefix_mapping = manifest.get("inputs", {}).get("filename_prefix") if isinstance(manifest.get("inputs"), Mapping) else None
    if not isinstance(output_mapping, Mapping) or not isinstance(prefix_mapping, Mapping):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The production output mapping is unavailable.")
    output_node_id = output_mapping.get("node_id")
    prefix_input = prefix_mapping.get("input")
    output_node = workflow.get(output_node_id) if isinstance(output_node_id, str) else None
    if not isinstance(output_node, Mapping) or not isinstance(output_node.get("inputs"), Mapping) or not isinstance(prefix_input, str):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared production output node is unavailable.")
    expected_prefix = output_node["inputs"].get(prefix_input)
    queue_audio_name = source_audio.get("queue_name")
    if not isinstance(expected_prefix, str) or not isinstance(queue_audio_name, str) or not all(isinstance(item, Mapping) for item in visual_inputs):
        raise RenderExecutionBlocked("PREPARATION_INVALID", "Prepared production inputs are incomplete.")
    try:
        validation = validate_compiled_workflow_contract(
            method,
            workflow,
            timing_plan=timing,
            visual_inputs=[dict(item) for item in visual_inputs],
            queue_audio_name=queue_audio_name,
            prompt_value=prompt_state["saved_final_prompt"],
            queue_prefix=expected_prefix,
        )
    except RenderPreparationError as error:
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", error.message) from error
    return {
        "workflow": workflow,
        "metadata": metadata,
        "generation_method": method,
        "preparation_fingerprint": metadata["preparation_fingerprint"],
        "output_node_id": str(output_node_id),
        "expected_filename_prefix": expected_prefix,
        "output_format": output_node["inputs"].get("format"),
        "contract_validation": validation,
    }


def _active_job(records: list[Mapping[str, object]]) -> Mapping[str, object] | None:
    for record in records:
        if record.get("state") in ACTIVE_STATES:
            return record
    return None


def submit_render_job(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    *,
    client: ComfyUIClient | None = None,
    hardware_gate: TargetHardwareGate | None = None,
    preflight: Mapping[str, object] | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    package_loader: Callable[[ProjectStorage, dict[str, object], str, Mapping[str, object]], Mapping[str, object]] = _load_prepared_package,
    retry_of_job_id: str | None = None,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    project = storage.load_project(canonical_project_id)
    with _scene_lock(canonical_project_id, canonical_scene_id):
        store = JobStore(storage)
        records = store.list_scene(canonical_project_id, canonical_scene_id)
        active = _active_job(records)
        if active is not None:
            raise RenderJobConflict(
                "SCENE_JOB_ACTIVE",
                "This scene already has a live or unreconciled render job.",
                details={"job_id": active["job_id"], "state": active["state"]},
            )
        current_preflight = preflight or preflight_builder(project, storage)
        scene_preflight = _scene_from_preflight(current_preflight, canonical_scene_id)
        gate = (hardware_gate or TargetHardwareGate()).require(scene_preflight)
        package = dict(package_loader(storage, project, canonical_scene_id, scene_preflight))
        fingerprint = package.get("preparation_fingerprint")
        if not isinstance(fingerprint, str):
            raise RenderExecutionBlocked("PREPARATION_INVALID", "Prepared render package has no fingerprint.")
        if fingerprint != scene_preflight.get("preparation_fingerprint"):
            raise RenderExecutionBlocked("PREPARATION_STALE", "Render preparation no longer matches current scene inputs.")
        job = new_job_record(
            canonical_project_id,
            canonical_scene_id,
            str(package["generation_method"]),
            fingerprint,
            output_node_id=str(package["output_node_id"]),
            expected_filename_prefix=str(package["expected_filename_prefix"]),
            output_format=package.get("output_format") if isinstance(package.get("output_format"), str) else None,
            retry_of_job_id=retry_of_job_id,
        )
        job["execution_gate"] = gate
        job = store.save(job)
        job = transition_job(job, SUBMITTING, reason="submission_started")
        store.save(job)
        comfy = client or ComfyUIClient()
        try:
            response = comfy.submit_prompt(package["workflow"])
            prompt_id = _canonical_prompt_id(response.get("prompt_id"))
            if any(item.get("comfy_prompt_id") == prompt_id for item in records):
                raise ComfyUIClientError("PROMPT_ID_REUSED", "ComfyUI returned a prompt ID already owned by another Builder job.")
        except ComfyUIClientError as error:
            failure = {
                "category": "submission_failure",
                "code": error.code,
                "message": error.message,
                "transient": error.transient,
            }
            job = transition_job(job, FAILED, reason="submission_failed", updates={"failure": failure, "progress": None})
            store.save(job)
            raise RenderJobSubmissionError(
                error.code,
                error.message,
                job_id=job["job_id"],
                details={"failure": failure},
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            failure = {
                "category": "submission_failure",
                "code": "COMFYUI_RESPONSE_INVALID",
                "message": "ComfyUI returned an invalid submission response.",
                "transient": False,
            }
            job = transition_job(job, FAILED, reason="submission_response_invalid", updates={"failure": failure, "progress": None})
            store.save(job)
            raise RenderJobSubmissionError(failure["code"], failure["message"], job_id=job["job_id"], details={"failure": failure}) from error
        timestamp = _timestamp()
        job = transition_job(
            job,
            QUEUED,
            reason="comfyui_accepted_prompt",
            now=timestamp,
            updates={
                "submitted_at": timestamp,
                "comfy_prompt_id": prompt_id,
                "queue_number": response.get("queue_number"),
                "progress": {"kind": "lifecycle", "value": None, "label": "Queued"},
                "failure": None,
            },
        )
        return store.save(job)


def _history_entry(history: Mapping[str, object], prompt_id: str) -> dict[str, object] | None:
    candidate = history.get(prompt_id)
    if isinstance(candidate, Mapping):
        return dict(candidate)
    if history.get("prompt_id") == prompt_id and isinstance(history.get("outputs"), Mapping):
        return dict(history)
    return None


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
        if isinstance(candidate, str) and PROMPT_ID_PATTERN.fullmatch(candidate):
            result.add(candidate)
    return result


def _history_status(entry: Mapping[str, object]) -> tuple[str | None, str | None]:
    status = entry.get("status")
    if not isinstance(status, Mapping):
        return None, None
    status_name = status.get("status_str")
    messages = status.get("messages")
    message = None
    if isinstance(messages, list):
        text_messages = []
        for item in messages:
            if isinstance(item, (list, tuple)) and len(item) > 1:
                text_messages.append(str(item[1]))
            elif isinstance(item, str):
                text_messages.append(item)
        if text_messages:
            message = "; ".join(text_messages)[-1000:]
    return status_name if isinstance(status_name, str) else None, message


def _set_poll_error(record: dict[str, object], error: ComfyUIClientError) -> dict[str, object]:
    updated = deepcopy(record)
    updated["last_poll_error"] = {"code": error.code, "message": error.message, "transient": error.transient, "at": _timestamp()}
    updated["updated_at"] = _timestamp()
    return updated


def reconcile_project_jobs(
    storage: ProjectStorage,
    project_id: object,
    *,
    client: ComfyUIClient | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    storage.load_project(canonical_project_id)
    with _project_lock(canonical_project_id):
        store = JobStore(storage)
        records = store.list_project(canonical_project_id)
        active = [record for record in records if record.get("state") in ACTIVE_STATES]
        warnings: list[dict[str, object]] = []
        comfy = client or ComfyUIClient()
        queue: dict[str, object] = {}
        if active:
            try:
                queue = comfy.get_queue()
            except ComfyUIClientError as error:
                warnings.append({"code": error.code, "message": error.message, "transient": error.transient})
                for record in active:
                    updated = _set_poll_error(record, error)
                    store.save(updated)
                records = store.list_project(canonical_project_id)
                return {
                    "project_id": canonical_project_id,
                    "jobs": records,
                    "warnings": warnings,
                    "capabilities": comfy.cancellation_capabilities(),
                }
        running_ids = _prompt_ids_in_queue(queue, "queue_running")
        pending_ids = _prompt_ids_in_queue(queue, "queue_pending")
        for record in active:
            prompt_id = record.get("comfy_prompt_id")
            if not isinstance(prompt_id, str):
                updated = transition_job(
                    record,
                    UNKNOWN,
                    reason="prompt_id_missing_during_reconciliation",
                    updates={
                        "failure": {
                            "category": "reconciliation_failure",
                            "code": "PROMPT_ID_MISSING",
                            "message": "The Builder job has no ComfyUI prompt ID; success cannot be inferred.",
                        },
                        "progress": None,
                    },
                )
                store.save(updated)
                continue
            try:
                history_payload = comfy.get_history(prompt_id)
            except ComfyUIClientError as error:
                warnings.append({"job_id": record["job_id"], "code": error.code, "message": error.message, "transient": error.transient})
                store.save(_set_poll_error(record, error))
                continue
            entry = _history_entry(history_payload, prompt_id)
            if entry is not None:
                status_name, status_message = _history_status(entry)
                if status_name == "success":
                    expected = record.get("production_output")
                    try:
                        output = discover_raw_output(
                            entry,
                            prompt_id=prompt_id,
                            output_node_id=str(expected.get("node_id")) if isinstance(expected, Mapping) else None,
                            expected_filename_prefix=str(expected.get("filename_prefix")) if isinstance(expected, Mapping) else None,
                            output_root=output_root,
                            output_format=str(expected.get("format")) if isinstance(expected, Mapping) and expected.get("format") else None,
                        )
                    except RenderOutputDiscoveryError as error:
                        failure = {
                            "category": "execution_failure",
                            "code": error.code,
                            "message": error.message,
                        }
                        updated = transition_job(record, FAILED, reason="output_discovery_failed", updates={"failure": failure, "progress": None})
                        store.save(updated)
                    else:
                        updated = transition_job(
                            record,
                            SUCCEEDED,
                            reason="history_success_and_output_discovered",
                            updates={"output": output, "failure": None, "progress": {"kind": "lifecycle", "value": None, "label": "Succeeded"}},
                        )
                        store.save(updated)
                    continue
                if status_name == "error":
                    failure = {
                        "category": "execution_failure",
                        "code": "COMFYUI_EXECUTION_FAILED",
                        "message": status_message or "ComfyUI reported an execution failure.",
                    }
                    updated = transition_job(record, FAILED, reason="history_execution_error", updates={"failure": failure, "progress": None})
                    store.save(updated)
                    continue
            if prompt_id in running_ids:
                if record.get("state") == CANCEL_REQUESTED:
                    continue
                updated = transition_job(record, RUNNING, reason="queue_running", updates={"progress": {"kind": "lifecycle", "value": None, "label": "Running"}, "last_poll_error": None})
                store.save(updated)
                continue
            if prompt_id in pending_ids:
                if record.get("state") == CANCEL_REQUESTED:
                    continue
                updated = transition_job(record, QUEUED, reason="queue_pending", updates={"progress": {"kind": "lifecycle", "value": None, "label": "Queued"}, "last_poll_error": None})
                store.save(updated)
                continue
            cancel = record.get("cancel")
            if record.get("state") == CANCEL_REQUESTED and isinstance(cancel, Mapping) and cancel.get("api_acknowledged") is True:
                target_state = CANCELLED if cancel.get("mode") == "queued_delete" else INTERRUPTED
                updated = transition_job(record, target_state, reason="cancellation_confirmed_by_absence", updates={"progress": {"kind": "lifecycle", "value": None, "label": target_state.title()}})
            else:
                updated = transition_job(
                    record,
                    UNKNOWN,
                    reason="prompt_missing_from_queue_and_history",
                    updates={
                        "failure": {
                            "category": "reconciliation_failure",
                            "code": "RECONCILIATION_REQUIRED",
                            "message": "ComfyUI no longer reports this prompt; the Builder cannot infer success.",
                        },
                        "progress": None,
                    },
                )
            store.save(updated)
        return {
            "project_id": canonical_project_id,
            "jobs": store.list_project(canonical_project_id),
            "warnings": warnings,
            "capabilities": comfy.cancellation_capabilities(),
        }


def cancel_render_job(
    storage: ProjectStorage,
    project_id: object,
    job_id: object,
    *,
    client: ComfyUIClient | None = None,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    canonical_job_id = _validate_job_id(job_id)
    with _project_lock(canonical_project_id):
        store = JobStore(storage)
        record = store.find(canonical_project_id, canonical_job_id)
        if record["state"] in TERMINAL_STATES:
            return record
        prompt_id = record.get("comfy_prompt_id")
        if not isinstance(prompt_id, str):
            raise RenderJobStateError("CANCEL_UNAVAILABLE", "This job has no owned ComfyUI prompt ID to cancel.")
        comfy = client or ComfyUIClient()
        queue = comfy.get_queue()
        running_ids = _prompt_ids_in_queue(queue, "queue_running")
        pending_ids = _prompt_ids_in_queue(queue, "queue_pending")
        if prompt_id in pending_ids:
            comfy.delete_queued(prompt_id)
            mode = "queued_delete"
        elif prompt_id in running_ids:
            comfy.interrupt_running(prompt_id)
            mode = "running_interrupt"
        else:
            raise RenderJobStateError("CANCEL_NOT_ACTIVE", "The owned ComfyUI prompt is no longer queued or running; reconcile before cancelling.")
        updated = transition_job(
            record,
            CANCEL_REQUESTED,
            reason=f"{mode}_requested",
            updates={
                "cancel": {
                    "requested_at": _timestamp(),
                    "mode": mode,
                    "api_acknowledged": True,
                    "api_scope": "global_engine_interrupt_targeted_by_prompt_id" if mode == "running_interrupt" else "queued_delete",
                },
                "progress": {"kind": "lifecycle", "value": None, "label": "Cancellation requested"},
            },
        )
        return store.save(updated)


def retry_render_job(
    storage: ProjectStorage,
    project_id: object,
    job_id: object,
    *,
    client: ComfyUIClient | None = None,
    hardware_gate: TargetHardwareGate | None = None,
    preflight: Mapping[str, object] | None = None,
    preflight_builder: Callable[..., Mapping[str, object]] = build_render_preflight,
    package_loader: Callable[[ProjectStorage, dict[str, object], str, Mapping[str, object]], Mapping[str, object]] = _load_prepared_package,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    canonical_job_id = _validate_job_id(job_id)
    old = JobStore(storage).find(canonical_project_id, canonical_job_id)
    if old["state"] not in {FAILED, CANCELLED, INTERRUPTED}:
        raise RenderJobStateError("RETRY_NOT_ALLOWED", "Only a reconciled failed or cancelled job may be retried.")
    return submit_render_job(
        storage,
        canonical_project_id,
        old["scene_id"],
        client=client,
        hardware_gate=hardware_gate,
        preflight=preflight,
        preflight_builder=preflight_builder,
        package_loader=package_loader,
        retry_of_job_id=canonical_job_id,
    )


def _safe_relative_output(root: Path, subfolder: object, filename: object) -> tuple[Path, str]:
    if not isinstance(subfolder, str) or not isinstance(filename, str) or not filename:
        raise RenderOutputDiscoveryError("OUTPUT_RECORD_INVALID", "ComfyUI returned an invalid production output record.")
    relative = Path(subfolder) / Path(filename)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The discovered output path is unsafe.")
    if relative.suffix.lower() not in OUTPUT_VIDEO_SUFFIXES:
        raise RenderOutputDiscoveryError("OUTPUT_TYPE_INVALID", "The discovered production output is not a raw video file.")
    if root.is_symlink() or not root.is_dir():
        raise RenderOutputDiscoveryError("OUTPUT_ROOT_INVALID", "The ComfyUI output root is unavailable or unsafe.")
    root_resolved = root.resolve(strict=True)
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The discovered output path contains a symlink.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RenderOutputDiscoveryError("OUTPUT_MISSING", "The production output file is not available.") from error
    if resolved.is_symlink() or not resolved.is_file() or (resolved != root_resolved and root_resolved not in resolved.parents):
        raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The discovered output is outside the ComfyUI output root.")
    return resolved, relative.as_posix()


def _default_comfy_output_root() -> Path:
    try:
        import folder_paths  # type: ignore

        root = folder_paths.get_directory_by_type("output")
    except (ImportError, AttributeError, OSError, TypeError):
        try:
            import folder_paths  # type: ignore

            root = folder_paths.get_output_directory()
        except (ImportError, AttributeError, OSError, TypeError) as error:
            raise RenderOutputDiscoveryError("OUTPUT_ROOT_UNAVAILABLE", "The ComfyUI output root could not be resolved safely.") from error
    if not isinstance(root, (str, os.PathLike)):
        raise RenderOutputDiscoveryError("OUTPUT_ROOT_UNAVAILABLE", "The ComfyUI output root could not be resolved safely.")
    return Path(root)


def discover_raw_output(
    history_entry: Mapping[str, object],
    *,
    prompt_id: str,
    output_node_id: str | None,
    expected_filename_prefix: str | None,
    output_root: Path | None = None,
    output_format: str | None = None,
) -> dict[str, object]:
    canonical_prompt_id = _canonical_prompt_id(prompt_id)
    if not isinstance(history_entry, Mapping) or history_entry.get("prompt_id", canonical_prompt_id) != canonical_prompt_id:
        raise RenderOutputDiscoveryError("OUTPUT_PROMPT_MISMATCH", "The discovered output is not owned by this ComfyUI prompt.")
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, Mapping) or not isinstance(output_node_id, str) or not output_node_id:
        raise RenderOutputDiscoveryError("OUTPUT_NODE_MISSING", "The known production output node is missing from ComfyUI history.")
    node_output = outputs.get(output_node_id)
    if not isinstance(node_output, Mapping):
        raise RenderOutputDiscoveryError("OUTPUT_NODE_MISSING", "The known production output node is missing from ComfyUI history.")
    candidates: dict[str, dict[str, object]] = {}
    for item_type in ("gifs", "videos", "images", "audio"):
        items = node_output.get(item_type)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") not in {None, "output"}:
                continue
            filename = item.get("filename")
            subfolder = item.get("subfolder", "")
            if not isinstance(filename, str) or Path(filename).suffix.lower() not in OUTPUT_VIDEO_SUFFIXES:
                continue
            root = output_root or _default_comfy_output_root()
            resolved, relative_path = _safe_relative_output(root, subfolder, filename)
            if expected_filename_prefix:
                normalized_prefix = expected_filename_prefix.replace("\\", "/").strip("/")
                if not relative_path.startswith(normalized_prefix):
                    continue
            candidates[str(resolved)] = {
                "prompt_id": canonical_prompt_id,
                "output_node_id": output_node_id,
                "relative_path": relative_path,
                "filename": filename,
                "subfolder": subfolder,
                "format": output_format or item.get("format") or item_type,
                "raw_h3_output": True,
                "discovered_at": _timestamp(),
            }
    if len(candidates) != 1:
        if not candidates:
            raise RenderOutputDiscoveryError("OUTPUT_NOT_FOUND", "The prompt-owned raw H3 video output could not be discovered.")
        raise RenderOutputDiscoveryError("OUTPUT_AMBIGUOUS", "More than one prompt-owned raw H3 video output matched the production node.")
    return next(iter(candidates.values()))


__all__ = [
    "ACTIVE_STATES",
    "CANCELLED",
    "CANCEL_REQUESTED",
    "ComfyUIClient",
    "ComfyUIClientError",
    "FAILED",
    "INTERRUPTED",
    "JobStore",
    "QUEUED",
    "READY_TO_SUBMIT",
    "RenderExecutionBlocked",
    "RenderExecutionDeferred",
    "RenderJobConflict",
    "RenderJobError",
    "RenderJobNotFound",
    "RenderJobStateError",
    "RenderJobSubmissionError",
    "RenderOutputDiscoveryError",
    "RUNNING",
    "SUBMITTING",
    "SUCCEEDED",
    "TERMINAL_STATES",
    "TargetHardwareGate",
    "TargetHardwareQualification",
    "UNKNOWN",
    "cancel_render_job",
    "default_target_hardware_qualification",
    "discover_raw_output",
    "new_job_record",
    "reconcile_project_jobs",
    "retry_render_job",
    "submit_render_job",
    "transition_job",
]
