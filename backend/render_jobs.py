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
from pathlib import Path, PurePosixPath
import re
import socket
import threading
import time
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
    build_execution_eligibility,
    build_render_preflight,
    validate_compiled_workflow_contract,
)
from .render_telemetry import (
    ComfyUITelemetryAdapter,
    get_shared_telemetry_adapter,
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
ORPHANED = "ORPHANED"

ORPHAN_CONFIRMATION_ATTEMPTS = 2

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
    ORPHANED,
)
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, ORPHANED})
ACTIVE_STATES = frozenset({READY_TO_SUBMIT, SUBMITTING, QUEUED, RUNNING, CANCEL_REQUESTED, UNKNOWN})

LEGAL_TRANSITIONS = {
    READY_TO_SUBMIT: frozenset({SUBMITTING, UNKNOWN}),
    SUBMITTING: frozenset({QUEUED, FAILED, CANCEL_REQUESTED, UNKNOWN}),
    QUEUED: frozenset({RUNNING, SUCCEEDED, FAILED, INTERRUPTED, CANCEL_REQUESTED, UNKNOWN, ORPHANED}),
    RUNNING: frozenset({SUCCEEDED, FAILED, INTERRUPTED, CANCEL_REQUESTED, UNKNOWN, ORPHANED}),
    CANCEL_REQUESTED: frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, UNKNOWN, QUEUED, RUNNING, ORPHANED}),
    UNKNOWN: frozenset({QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, ORPHANED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    INTERRUPTED: frozenset(),
    ORPHANED: frozenset(),
}

LOCAL_API_DEFAULT_HOST = "127.0.0.1"
LOCAL_API_DEFAULT_PORT = 8188
LOCAL_API_TIMEOUT_MAX_SECONDS = 60.0
OUTPUT_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})
COMFYUI_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
COMFYUI_ERROR_RESPONSE_MAX_BYTES = 64 * 1024
COMFYUI_OBJECT_INFO_MAX_BYTES = 2 * 1024 * 1024
COMFYUI_OBJECT_INFO_CACHE_SECONDS = 30.0
COMFYUI_OBJECT_INFO_CACHE_MAX_ENTRIES = 128
COMFYUI_DIAGNOSTIC_MAX_ITEMS = 100
COMFYUI_DIAGNOSTIC_MAX_STRING = 2_048
LIVE_INPUT_SUPPORTED = "SUPPORTED"
LIVE_INPUT_UNSUPPORTED = "UNSUPPORTED"
LIVE_INPUT_NOT_DETERMINABLE = "NOT_DETERMINABLE"

_LOCKS_LOCK = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}


def _bounded_json_value(value: object, *, depth: int = 0) -> object:
    if depth >= 6:
        return "[detail depth limit]"
    if isinstance(value, str):
        return value[:COMFYUI_DIAGNOSTIC_MAX_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= COMFYUI_DIAGNOSTIC_MAX_ITEMS:
                result["_truncated"] = True
                break
            result[str(key)[:256]] = _bounded_json_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded_json_value(item, depth=depth + 1) for item in value[:COMFYUI_DIAGNOSTIC_MAX_ITEMS]]
        if len(value) > COMFYUI_DIAGNOSTIC_MAX_ITEMS:
            result.append("[items truncated]")
        return result
    return str(value)[:COMFYUI_DIAGNOSTIC_MAX_STRING]


def _bounded_stream_read(stream: object, limit: int) -> tuple[bytes, bool]:
    reader = getattr(stream, "read", None)
    if not callable(reader):
        return b"", False
    raw = reader(limit + 1)
    if not isinstance(raw, bytes):
        raw = bytes(raw or b"")
    return raw[:limit], len(raw) > limit


def _response_size_limit(path: str) -> int:
    return COMFYUI_OBJECT_INFO_MAX_BYTES if path.startswith("/object_info/") else COMFYUI_RESPONSE_MAX_BYTES


def _validate_api_prompt_shape(workflow: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(workflow, Mapping) or not workflow:
        raise ComfyUIClientError("WORKFLOW_INVALID", "Only a prepared production API workflow may be submitted.")
    wrapper_keys = {"nodes", "links", "workflow", "prompt"}.intersection(workflow)
    if wrapper_keys:
        raise ComfyUIClientError(
            "WORKFLOW_FORMAT_INVALID",
            "The prepared workflow must be a ComfyUI API-format node graph, not a UI workflow or wrapper.",
        )
    graph: dict[str, object] = {}
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not node_id or not isinstance(node, Mapping):
            raise ComfyUIClientError("WORKFLOW_FORMAT_INVALID", "The prepared API workflow contains an invalid node.")
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not class_type or not isinstance(inputs, Mapping):
            raise ComfyUIClientError(
                "WORKFLOW_FORMAT_INVALID",
                f"Prepared node {node_id} is missing API-format class_type or inputs data.",
            )
        graph[node_id] = deepcopy(dict(node))
    try:
        json.dumps(graph, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ComfyUIClientError("WORKFLOW_JSON_INVALID", "The prepared API workflow is not valid JSON data.") from error
    return graph


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
    """Legacy compatibility error; current execution is not vendor-deferred."""

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
    """Return informational support for both valid H3 execution platforms."""

    return TargetHardwareQualification(
        qualified=True,
        status="SUPPORTED_MULTI_GPU",
        message="H3 execution is supported on AMD Radeon RX 7900 XT and NVIDIA RTX 4080 SUPER 16 GB.",
        evidence="multi_gpu_execution_supported",
    )


class TargetHardwareGate:
    """Compatibility wrapper around the real scene-execution eligibility gate.

    The qualification provider remains available as informational metadata for
    callers that already use this API.  Its preference or qualification result
    never blocks an otherwise eligible scene.
    """

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
        eligibility = build_execution_eligibility(scene)
        qualification = self.qualification()
        return {
            "allowed": eligibility["eligible"],
            "status": eligibility["status"],
            "message": eligibility["message"],
            "blockers": eligibility["blockers"],
            "execution_eligibility": eligibility,
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
        or re.fullmatch(r"/object_info/[A-Za-z0-9._-]{1,128}", path)
    )


class ComfyUIClient:
    """Small, local-only adapter for the installed ComfyUI core API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: Callable[[str, str, object | None], Mapping[str, object]] | None = None,
        telemetry: ComfyUITelemetryAdapter | None = None,
    ):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ComfyUIClientError("COMFYUI_TIMEOUT_INVALID", "ComfyUI request timeout is invalid.")
        if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= LOCAL_API_TIMEOUT_MAX_SECONDS:
            raise ComfyUIClientError("COMFYUI_TIMEOUT_INVALID", "ComfyUI request timeout must be bounded.")
        self.base_url = _resolve_local_comfyui_url(base_url)
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport
        self.telemetry = telemetry or get_shared_telemetry_adapter(self.base_url)
        self.telemetry_worker_enabled = transport is None
        self.request_log: list[tuple[str, str, object | None]] = []
        self._object_info_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._object_info_lock = threading.Lock()

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
            try:
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise ComfyUIClientError("COMFYUI_RESPONSE_INVALID", "ComfyUI returned an invalid JSON response.") from error
            if len(encoded) > _response_size_limit(path):
                raise ComfyUIClientError("COMFYUI_RESPONSE_TOO_LARGE", "ComfyUI returned an oversized JSON response.")
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
                raw, truncated = _bounded_stream_read(response, _response_size_limit(path))
                if truncated:
                    raise ComfyUIClientError("COMFYUI_RESPONSE_TOO_LARGE", "ComfyUI returned an oversized JSON response.")
        except urllib.error.HTTPError as error:
            transient = error.code >= 500 or error.code == 429
            raw_error, truncated = _bounded_stream_read(error, COMFYUI_ERROR_RESPONSE_MAX_BYTES)
            response_text = raw_error.decode("utf-8", errors="replace")
            response_body: object = response_text
            try:
                response_body = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError:
                pass
            bounded_body = _bounded_json_value(response_body)
            details: dict[str, object] = {
                "http_status": error.code,
                "response_body": bounded_body,
                "response_truncated": truncated,
            }
            if isinstance(response_body, Mapping):
                if "error" in response_body:
                    details["comfyui_error"] = _bounded_json_value(response_body.get("error"))
                if "node_errors" in response_body:
                    details["node_errors"] = _bounded_json_value(response_body.get("node_errors"))
            validation_rejection = error.code == 400 and (
                isinstance(details.get("comfyui_error"), Mapping)
                or isinstance(details.get("node_errors"), Mapping)
            )
            raise ComfyUIClientError(
                "COMFYUI_VALIDATION_REJECTED" if validation_rejection else "COMFYUI_HTTP_ERROR",
                "ComfyUI rejected the workflow during validation."
                if validation_rejection
                else f"ComfyUI returned HTTP {error.code}.",
                transient=transient,
                details=details,
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
        graph = _validate_api_prompt_shape(workflow)
        if self.transport is None:
            # The installed ComfyUI server copies this top-level value into
            # execution extra_data and uses it to route prompt events to the
            # initiating /ws session.  The ID is generated by the shared
            # adapter; callers cannot supply an arbitrary session ID.
            telemetry_connected = self.telemetry.ensure_connected(timeout_seconds=min(1.0, self.timeout_seconds))
            if telemetry_connected:
                LOGGER.info(
                    "ComfyUI telemetry ready before submission client_id=%s",
                    self.telemetry.client_id,
                )
            else:
                LOGGER.warning(
                    "ComfyUI telemetry unavailable before submission; continuing without gating client_id=%s websocket_url=%s",
                    self.telemetry.client_id,
                    self.telemetry.websocket_url,
                )
        LOGGER.info("Submitting ComfyUI prompt client_id=%s", self.telemetry.client_id)
        try:
            response = self._request_json(
                "POST",
                "/prompt",
                {"prompt": graph, "client_id": self.telemetry.client_id},
            )
        except ComfyUIClientError as error:
            if error.code == "COMFYUI_VALIDATION_REJECTED" or isinstance(error.details.get("node_errors"), Mapping):
                raise _normalize_submission_error(graph, error) from error
            raise
        prompt_id = _canonical_prompt_id(response.get("prompt_id"))
        self.telemetry.register_prompt(prompt_id, graph, start=self.transport is None)
        LOGGER.info(
            "ComfyUI prompt accepted client_id=%s prompt_id=%s",
            self.telemetry.client_id,
            prompt_id,
        )
        return {
            "prompt_id": prompt_id,
            "queue_number": response.get("number"),
            "node_errors": deepcopy(response.get("node_errors", {})),
        }

    def get_object_info(self, class_type: str) -> dict[str, object]:
        if not isinstance(class_type, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", class_type):
            raise ComfyUIClientError("LIVE_NODE_CLASS_INVALID", "A live ComfyUI node class name is invalid.")
        now = time.monotonic()
        with self._object_info_lock:
            cached = self._object_info_cache.get(class_type)
            if cached is not None and now - cached[0] <= COMFYUI_OBJECT_INFO_CACHE_SECONDS:
                return deepcopy(cached[1])
        response = self._request_json("GET", f"/object_info/{class_type}")
        definition = response.get(class_type)
        if not isinstance(definition, Mapping):
            raise ComfyUIClientError(
                "LIVE_NODE_CLASS_MISSING",
                f"Required node class '{class_type}' is not registered in the active ComfyUI runtime.",
            )
        result = dict(definition)
        with self._object_info_lock:
            if len(self._object_info_cache) >= COMFYUI_OBJECT_INFO_CACHE_MAX_ENTRIES:
                oldest = min(self._object_info_cache, key=lambda key: self._object_info_cache[key][0])
                self._object_info_cache.pop(oldest, None)
            self._object_info_cache[class_type] = (now, deepcopy(result))
        return result

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


def _telemetry_adapter(client: object) -> ComfyUITelemetryAdapter | None:
    adapter = getattr(client, "telemetry", None)
    return adapter if isinstance(adapter, ComfyUITelemetryAdapter) else None


def _register_telemetry_prompt(client: object, prompt_id: object, workflow: object = None) -> None:
    adapter = _telemetry_adapter(client)
    if adapter is None:
        return
    adapter.register_prompt(
        prompt_id,
        workflow,
        start=bool(getattr(client, "telemetry_worker_enabled", True)),
    )


def _clear_telemetry_prompt(client: object, prompt_id: object) -> None:
    adapter = _telemetry_adapter(client)
    if adapter is not None:
        adapter.clear_prompt(prompt_id)


def _overlay_telemetry_jobs(records: list[Mapping[str, object]], client: object) -> list[dict[str, object]]:
    """Overlay volatile WebSocket observations without persisting progress ticks."""

    adapter = _telemetry_adapter(client)
    result: list[dict[str, object]] = []
    for record in records:
        item = deepcopy(dict(record))
        prompt_id = item.get("comfy_prompt_id")
        # Telemetry may enrich a durable queued/running job only.  In
        # particular, an UNKNOWN/reconciliation-required job must never be
        # presented as RUNNING merely because re-registration created a
        # synthetic in-memory snapshot before any WebSocket event arrived.
        if adapter is None or item.get("state") not in {QUEUED, RUNNING} or not isinstance(prompt_id, str):
            result.append(item)
            continue
        # A job can have been submitted before this process was restarted. It
        # becomes owned by the current shared session for future telemetry;
        # HTTP queue/history still decides whether it is actually running.
        adapter.register_prompt(
            prompt_id,
            start=bool(getattr(client, "telemetry_worker_enabled", True)),
        )
        snapshot = adapter.snapshot(prompt_id)
        if snapshot is not None:
            item["telemetry"] = snapshot
            if snapshot.get("observed") is True and snapshot.get("telemetry_stale") is not True:
                progress = snapshot.get("progress")
                if isinstance(progress, Mapping):
                    item["progress"] = deepcopy(dict(progress, kind="telemetry", label=snapshot.get("stage") or "Running"))
                else:
                    item["progress"] = {
                        "kind": "telemetry",
                        "value": None,
                        "label": snapshot.get("current_stage") or "Rendering",
                        "stage": snapshot.get("current_stage"),
                        "node_id": snapshot.get("current_node_id"),
                        "node_type": snapshot.get("current_node_type"),
                        "scope": "node",
                        "node_local": True,
                        "progress_reset": snapshot.get("progress_reset") is True,
                    }
        result.append(item)
    return result


def _diagnostic_text(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return " ".join(value.split())[:COMFYUI_DIAGNOSTIC_MAX_STRING]


def _normalize_submission_error(
    workflow: Mapping[str, object],
    error: ComfyUIClientError,
) -> ComfyUIClientError:
    details = deepcopy(error.details)
    node_errors = details.get("node_errors")
    affected: list[dict[str, object]] = []
    if isinstance(node_errors, Mapping):
        for node_id, node_error in node_errors.items():
            if len(affected) >= COMFYUI_DIAGNOSTIC_MAX_ITEMS:
                break
            workflow_node = workflow.get(str(node_id))
            class_type = workflow_node.get("class_type") if isinstance(workflow_node, Mapping) else None
            if not isinstance(class_type, str) and isinstance(node_error, Mapping):
                class_type = node_error.get("class_type")
            errors = node_error.get("errors") if isinstance(node_error, Mapping) else None
            if not isinstance(errors, list) or not errors:
                errors = [node_error]
            for item in errors:
                if len(affected) >= COMFYUI_DIAGNOSTIC_MAX_ITEMS:
                    break
                extra_info = item.get("extra_info") if isinstance(item, Mapping) else None
                input_name = extra_info.get("input_name") if isinstance(extra_info, Mapping) else None
                detail = item.get("details") if isinstance(item, Mapping) else None
                reason = _diagnostic_text(detail, "ComfyUI rejected this node input.")
                if isinstance(input_name, str) and reason.startswith(f"{input_name} - "):
                    reason = reason[len(input_name) + 3:]
                elif isinstance(input_name, str) and reason.startswith(f"{input_name}: "):
                    reason = reason[len(input_name) + 2:]
                submitted_value = None
                if isinstance(input_name, str) and isinstance(workflow_node, Mapping):
                    inputs = workflow_node.get("inputs")
                    if isinstance(inputs, Mapping) and input_name in inputs:
                        submitted_value = _bounded_json_value(inputs[input_name])
                affected.append({
                    "node_id": str(node_id),
                    "class_type": class_type if isinstance(class_type, str) else "unknown",
                    "input_name": input_name if isinstance(input_name, str) else None,
                    "submitted_value": submitted_value,
                    "reason": reason,
                })
    details["affected_nodes"] = affected
    if affected:
        primary = affected[0]
        node_label = f"node {primary['node_id']} ({primary['class_type']})"
        input_label = f" input '{primary['input_name']}'" if primary.get("input_name") else ""
        message = f"ComfyUI rejected the workflow: {node_label} rejected{input_label}: {primary['reason']}"
    else:
        comfy_error = details.get("comfyui_error")
        comfy_message = comfy_error.get("message") if isinstance(comfy_error, Mapping) else None
        message = f"ComfyUI rejected the workflow: {_diagnostic_text(comfy_message, error.message)}"
    return ComfyUIClientError(
        error.code,
        message[:1_000],
        transient=error.transient,
        details=_bounded_json_value(details) if isinstance(details, Mapping) else {},
    )


def _is_workflow_link(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _parse_dynamic_widget_definitions(value: object, *, depth: int = 0) -> tuple[dict[str, object], bool]:
    """Parse selected-option widget descriptors without assuming a VHS class or node ID."""

    if depth >= 6:
        return {}, False
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and isinstance(value[0], str) and isinstance(value[1], (str, list, tuple)):
            return {value[0]: list(value[1:])}, True
        definitions: dict[str, object] = {}
        complete = True
        for item in value:
            nested, nested_complete = _parse_dynamic_widget_definitions(item, depth=depth + 1)
            definitions.update(nested)
            complete = complete and nested_complete
        return definitions, complete
    if isinstance(value, Mapping):
        definitions: dict[str, object] = {}
        complete = True
        for item in value.values():
            nested, nested_complete = _parse_dynamic_widget_definitions(item, depth=depth + 1)
            definitions.update(nested)
            complete = complete and nested_complete
        return definitions, complete
    return {}, False


def _selected_option_dynamic_inputs(
    selector_definition: object,
    selected_value: object,
) -> tuple[dict[str, object], bool, bool]:
    """Return dynamic inputs, whether metadata declares them, and completeness."""

    if (
        not isinstance(selector_definition, (list, tuple))
        or len(selector_definition) < 2
        or not isinstance(selector_definition[0], (list, tuple))
        or not isinstance(selector_definition[1], Mapping)
    ):
        return {}, False, True
    option_values = {item for item in selector_definition[0] if isinstance(item, str)}
    containers: list[Mapping[str, object]] = []

    def collect(value: object, depth: int = 0) -> None:
        if depth >= 6:
            return
        if isinstance(value, Mapping):
            keys = {key for key in value if isinstance(key, str)}
            if keys and keys.issubset(option_values):
                containers.append(value)
                return
            for item in value.values():
                collect(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, depth + 1)

    collect(selector_definition[1])
    if not containers:
        return {}, False, True
    if not isinstance(selected_value, str) or selected_value not in option_values:
        return {}, True, False
    definitions: dict[str, object] = {}
    complete = True
    for container in containers:
        selected_schema = container.get(selected_value, [])
        parsed, parsed_complete = _parse_dynamic_widget_definitions(selected_schema)
        definitions.update(parsed)
        complete = complete and parsed_complete
    return definitions, True, complete


def resolve_live_node_input_schema(
    definition: Mapping[str, object],
    node_inputs: Mapping[str, object],
) -> dict[str, object]:
    """Resolve static plus selected-option dynamic inputs for one node configuration."""

    input_groups = definition.get("input")
    if not isinstance(input_groups, Mapping):
        return {
            "static": {},
            "dynamic": {},
            "required": {},
            "determinable": False,
            "dynamic_selectors": [],
        }
    required = input_groups.get("required") if isinstance(input_groups.get("required"), Mapping) else {}
    optional = input_groups.get("optional") if isinstance(input_groups.get("optional"), Mapping) else {}
    hidden = input_groups.get("hidden") if isinstance(input_groups.get("hidden"), Mapping) else {}
    static_inputs = {**required, **optional, **hidden}
    dynamic_inputs: dict[str, object] = {}
    dynamic_selectors: list[dict[str, object]] = []
    determinable = True
    for selector_name, selector_definition in static_inputs.items():
        selected_value = node_inputs.get(selector_name)
        selected_inputs, declares_dynamic, complete = _selected_option_dynamic_inputs(
            selector_definition,
            selected_value,
        )
        if declares_dynamic:
            dynamic_selectors.append({
                "input_name": str(selector_name),
                "selected_value": _bounded_json_value(selected_value),
                "resolved_inputs": sorted(selected_inputs),
                "complete": complete,
            })
            dynamic_inputs.update(selected_inputs)
            determinable = determinable and complete
    return {
        "static": static_inputs,
        "dynamic": dynamic_inputs,
        "required": required,
        "determinable": determinable,
        "dynamic_selectors": dynamic_selectors,
    }


def _resolved_input_definition(
    resolved_schema: Mapping[str, object],
    input_name: str,
) -> tuple[str, object | None, str]:
    static_inputs = resolved_schema.get("static")
    dynamic_inputs = resolved_schema.get("dynamic")
    if isinstance(static_inputs, Mapping):
        if input_name in static_inputs:
            return LIVE_INPUT_SUPPORTED, static_inputs[input_name], "static"
        if "." in input_name:
            base_name = input_name.split(".", 1)[0]
            if base_name in static_inputs:
                return LIVE_INPUT_SUPPORTED, static_inputs[base_name], "static_autogrow"
    if isinstance(dynamic_inputs, Mapping) and input_name in dynamic_inputs:
        return LIVE_INPUT_SUPPORTED, dynamic_inputs[input_name], "selected_option_dynamic"
    if resolved_schema.get("determinable") is not True:
        return LIVE_INPUT_NOT_DETERMINABLE, None, "dynamic schema could not be resolved completely"
    return LIVE_INPUT_UNSUPPORTED, None, "input is absent from the resolved live schema"


def _live_input_value_assessment(input_name: str, value: object, definition: object) -> tuple[str, str | None]:
    if _is_workflow_link(value):
        return LIVE_INPUT_SUPPORTED, None
    if not isinstance(definition, (list, tuple)) or not definition:
        return LIVE_INPUT_NOT_DETERMINABLE, "live value contract is not structured"
    declared_type = definition[0]
    if isinstance(declared_type, (list, tuple)):
        if value not in declared_type:
            settings = definition[1] if len(definition) > 1 and isinstance(definition[1], Mapping) else {}
            is_upload_selector = any(
                isinstance(key, str) and key.endswith("_upload") and enabled is True
                for key, enabled in settings.items()
            )
            if is_upload_selector:
                return (
                    LIVE_INPUT_NOT_DETERMINABLE,
                    "value is outside the live discovery list for an upload selector; ComfyUI validation is authoritative",
                )
            return LIVE_INPUT_UNSUPPORTED, f"value {value!r} is not accepted by the active runtime"
        return LIVE_INPUT_SUPPORTED, None
    if declared_type == "INT" and (isinstance(value, bool) or not isinstance(value, int)):
        return LIVE_INPUT_UNSUPPORTED, "value must be an integer"
    if declared_type == "FLOAT" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return LIVE_INPUT_UNSUPPORTED, "value must be numeric"
    if declared_type == "BOOLEAN" and not isinstance(value, bool):
        return LIVE_INPUT_UNSUPPORTED, "value must be a boolean"
    if declared_type == "STRING" and not isinstance(value, str):
        return LIVE_INPUT_UNSUPPORTED, "value must be a string"
    if declared_type in {"INT", "FLOAT"}:
        settings = definition[1] if len(definition) > 1 and isinstance(definition[1], Mapping) else {}
        minimum = settings.get("min")
        maximum = settings.get("max")
        if isinstance(minimum, (int, float)) and value < minimum:
            return LIVE_INPUT_UNSUPPORTED, f"value must be at least {minimum}"
        if isinstance(maximum, (int, float)) and value > maximum:
            return LIVE_INPUT_UNSUPPORTED, f"value must be at most {maximum}"
        return LIVE_INPUT_SUPPORTED, None
    if declared_type in {"BOOLEAN", "STRING"}:
        return LIVE_INPUT_SUPPORTED, None
    return LIVE_INPUT_NOT_DETERMINABLE, f"value contract for {input_name!r} is runtime-defined"


def validate_live_workflow_compatibility(
    workflow: Mapping[str, object],
    client: ComfyUIClient,
) -> dict[str, object]:
    """Inspect object_info only; never use /prompt as a validation surrogate."""

    try:
        graph = _validate_api_prompt_shape(workflow)
    except ComfyUIClientError as error:
        raise RenderExecutionBlocked(error.code, error.message, details=error.details) from error
    definitions: dict[str, dict[str, object]] = {}
    issues: list[dict[str, object]] = []
    not_determinable: list[dict[str, object]] = []
    dynamic_schemas: list[dict[str, object]] = []
    assessment_counts = {
        LIVE_INPUT_SUPPORTED: 0,
        LIVE_INPUT_UNSUPPORTED: 0,
        LIVE_INPUT_NOT_DETERMINABLE: 0,
    }
    assessed_inputs = 0
    for node_id, node in graph.items():
        class_type = str(node["class_type"])
        if class_type not in definitions:
            try:
                definitions[class_type] = client.get_object_info(class_type)
            except ComfyUIClientError as error:
                if error.code in {"LIVE_NODE_CLASS_MISSING", "LIVE_NODE_CLASS_INVALID"}:
                    issues.append({
                        "node_id": node_id,
                        "class_type": class_type,
                        "input_name": None,
                        "status": LIVE_INPUT_UNSUPPORTED,
                        "reason": error.message,
                    })
                    continue
                raise RenderExecutionBlocked(
                    "LIVE_RUNTIME_INSPECTION_FAILED",
                    "The active ComfyUI node contracts could not be inspected safely.",
                    details={"cause": error.code, **error.details},
                ) from error
        definition = definitions.get(class_type)
        if not isinstance(definition, Mapping):
            continue
        node_inputs = node["inputs"]
        resolved_schema = resolve_live_node_input_schema(definition, node_inputs)
        if resolved_schema.get("dynamic_selectors"):
            dynamic_schemas.append({
                "node_id": node_id,
                "class_type": class_type,
                "selectors": resolved_schema["dynamic_selectors"],
            })
        if resolved_schema.get("determinable") is not True and not resolved_schema.get("static"):
            not_determinable.append({
                "node_id": node_id,
                "class_type": class_type,
                "input_name": None,
                "status": LIVE_INPUT_NOT_DETERMINABLE,
                "reason": "active object_info input contract could not be resolved",
            })
            continue
        required = resolved_schema.get("required")
        if not isinstance(required, Mapping):
            required = {}
        for input_name in required:
            has_direct = input_name in node_inputs
            has_autogrow = any(str(candidate).startswith(f"{input_name}.") for candidate in node_inputs)
            if not has_direct and not has_autogrow:
                assessment_counts[LIVE_INPUT_UNSUPPORTED] += 1
                issues.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input_name": input_name,
                    "status": LIVE_INPUT_UNSUPPORTED,
                    "reason": "required input is missing",
                })
        for input_name, value in node_inputs.items():
            assessed_inputs += 1
            input_status, live_definition, source = _resolved_input_definition(
                resolved_schema,
                str(input_name),
            )
            if input_status == LIVE_INPUT_UNSUPPORTED:
                assessment_counts[input_status] += 1
                issues.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input_name": str(input_name),
                    "submitted_value": _bounded_json_value(value),
                    "status": input_status,
                    "source": source,
                    "reason": "input is not present in the selected live node configuration",
                })
                continue
            if input_status == LIVE_INPUT_NOT_DETERMINABLE:
                assessment_counts[input_status] += 1
                not_determinable.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input_name": str(input_name),
                    "submitted_value": _bounded_json_value(value),
                    "status": input_status,
                    "source": source,
                    "reason": "input support could not be determined from active object_info",
                })
                continue
            value_status, mismatch = _live_input_value_assessment(
                str(input_name),
                value,
                live_definition,
            )
            assessment_counts[value_status] += 1
            if value_status == LIVE_INPUT_UNSUPPORTED:
                issues.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input_name": str(input_name),
                    "submitted_value": _bounded_json_value(value),
                    "status": value_status,
                    "source": source,
                    "reason": mismatch,
                })
            elif value_status == LIVE_INPUT_NOT_DETERMINABLE:
                not_determinable.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input_name": str(input_name),
                    "submitted_value": _bounded_json_value(value),
                    "status": value_status,
                    "source": source,
                    "reason": mismatch,
                })
    if issues:
        primary = issues[0]
        input_label = f" input '{primary['input_name']}'" if primary.get("input_name") else ""
        raise RenderExecutionBlocked(
            "LIVE_WORKFLOW_INCOMPATIBLE",
            f"Active ComfyUI is incompatible with prepared node {primary['node_id']} ({primary['class_type']}){input_label}: {primary['reason']}.",
            details={
                "issues": _bounded_json_value(issues),
                "not_determinable": _bounded_json_value(not_determinable),
                "dynamic_schemas": _bounded_json_value(dynamic_schemas),
            },
        )
    return {
        "compatible": True,
        "inspection": "object_info",
        "read_only": True,
        "checked_nodes": len(graph),
        "checked_node_classes": sorted(definitions),
        "assessed_inputs": assessed_inputs,
        "assessment_counts": assessment_counts,
        "not_determinable": not_determinable,
        "dynamic_schemas": dynamic_schemas,
        "final_authority": "comfyui_prompt_validation",
        "dynamic_schema_cache": "resolved_per_node_configuration",
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
            validated = _validate_job_record(record, project_id=canonical_project_id)
            return sanitize_job_record_for_presentation(validated)
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
                validated = _validate_job_record(document, project_id=canonical_project_id, scene_id=canonical_scene_id)
                records.append(sanitize_job_record_for_presentation(validated))
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
        "output_discovery": None,
        "cancel": None,
        "last_poll_error": None,
        "last_reconciled_at": None,
        "reconciliation": None,
        "comfy_instance_id": None,
        "production_output": {
            "node_id": output_node_id,
            "filename_prefix": expected_filename_prefix,
            "prepared_filename_prefix": expected_filename_prefix,
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


def _job_execution_output_prefix(prepared_prefix: object, job_id: object) -> str:
    if not isinstance(prepared_prefix, str) or not prepared_prefix.strip():
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared production output prefix is invalid.")
    if not isinstance(job_id, str) or not PROMPT_ID_PATTERN.fullmatch(job_id):
        raise RenderExecutionBlocked("JOB_RECORD_INVALID", "The Builder job identity is invalid.")
    normalized = prepared_prefix.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or any(not part or part in {".", ".."} for part in parts)
        or any(any(character in part for character in "*?[]") for part in parts)
    ):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared production output prefix is unsafe.")
    return f"{normalized}/{job_id}"


def _patch_job_execution_output_prefix(
    workflow: Mapping[str, object],
    generation_method: object,
    execution_prefix: str,
) -> dict[str, object]:
    """Patch only the manifest-owned VHS output prefix on an execution copy."""

    if not isinstance(generation_method, str):
        raise RenderExecutionBlocked("GENERATION_METHOD_INVALID", "The prepared generation method is invalid.")
    manifest = production_manifest_registry().get(generation_method)
    if not isinstance(manifest, Mapping):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The production manifest is unavailable.")
    inputs = manifest.get("inputs")
    mapping = inputs.get("filename_prefix") if isinstance(inputs, Mapping) else None
    if not isinstance(mapping, Mapping):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The production output prefix mapping is unavailable.")
    node_id = mapping.get("node_id")
    input_name = mapping.get("input")
    if not isinstance(node_id, str) or not isinstance(input_name, str):
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The production output prefix mapping is invalid.")
    execution_workflow = deepcopy(dict(workflow))
    node = execution_workflow.get(node_id)
    if not isinstance(node, Mapping) or node.get("class_type") != "VHS_VideoCombine":
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared production output node is unavailable.")
    node_inputs = node.get("inputs")
    if not isinstance(node_inputs, Mapping) or input_name not in node_inputs:
        raise RenderExecutionBlocked("WORKFLOW_CONTRACT_INVALID", "The prepared production output prefix input is unavailable.")
    execution_node = dict(node)
    execution_node["inputs"] = {**dict(node_inputs), input_name: execution_prefix}
    execution_workflow[node_id] = execution_node
    return execution_workflow


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
    compatibility_validator: Callable[[Mapping[str, object], ComfyUIClient], Mapping[str, object]] = validate_live_workflow_compatibility,
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
        comfy = client or ComfyUIClient()
        prepared_prefix = package.get("expected_filename_prefix")
        job = new_job_record(
            canonical_project_id,
            canonical_scene_id,
            str(package["generation_method"]),
            fingerprint,
            output_node_id=str(package["output_node_id"]),
            expected_filename_prefix=str(prepared_prefix),
            output_format=package.get("output_format") if isinstance(package.get("output_format"), str) else None,
            retry_of_job_id=retry_of_job_id,
        )
        execution_prefix = _job_execution_output_prefix(prepared_prefix, str(job["job_id"]))
        production_output = job.get("production_output")
        if not isinstance(production_output, dict):
            raise RenderExecutionBlocked("JOB_RECORD_INVALID", "The Builder job production output metadata is invalid.")
        production_output["prepared_filename_prefix"] = str(prepared_prefix)
        production_output["filename_prefix"] = execution_prefix
        production_output["ownership"] = {
            "kind": "builder_job_id",
            "job_id": job["job_id"],
            "prefix": execution_prefix,
        }
        execution_workflow = _patch_job_execution_output_prefix(
            package["workflow"],
            package["generation_method"],
            execution_prefix,
        )
        compatibility = dict(compatibility_validator(execution_workflow, comfy))
        job["execution_gate"] = gate
        job["runtime_compatibility"] = compatibility
        job = store.save(job)
        job = transition_job(job, SUBMITTING, reason="submission_started")
        store.save(job)
        try:
            response = comfy.submit_prompt(execution_workflow)
            prompt_id = _canonical_prompt_id(response.get("prompt_id"))
            if any(item.get("comfy_prompt_id") == prompt_id for item in records):
                raise ComfyUIClientError("PROMPT_ID_REUSED", "ComfyUI returned a prompt ID already owned by another Builder job.")
            _register_telemetry_prompt(comfy, prompt_id, package["workflow"])
            LOGGER.info(
                "Render job submitted job_id=%s prompt_id=%s scene_id=%s",
                job.get("job_id"),
                prompt_id,
                canonical_scene_id,
            )
        except ComfyUIClientError as error:
            LOGGER.warning(
                "Render job submission failed job_id=%s scene_id=%s code=%s",
                job.get("job_id"),
                canonical_scene_id,
                error.code,
            )
            failure = {
                "category": "submission_failed_validation"
                if error.code == "COMFYUI_VALIDATION_REJECTED"
                else "submission_failure",
                "code": error.code,
                "message": error.message,
                "transient": error.transient,
            }
            if error.details:
                failure["diagnostics"] = _bounded_json_value(error.details)
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


UNSAFE_DIAGNOSTIC_SUBSTRINGS = (
    "prompt_id",
    "node_id",
    "node_type",
    "status_str",
    "executed",
    "Traceback (most recent call last):",
    "exception_type",
    "exception_message",
)


def sanitize_user_facing_message(
    value: object,
    fallback: str = "The render job failed.",
) -> str:
    """Centralized user-facing error and diagnostic sanitizer.

    Guarantees that no raw Python dict reprs, JSON payloads, internal prompt IDs,
    node IDs, telemetry arrays, timestamps, or execution tracebacks ever enter
    product UI presentation text.
    """
    if value is None:
        return fallback

    if isinstance(value, Exception):
        return sanitize_user_facing_message(str(value), fallback=fallback)

    if isinstance(value, Mapping):
        exc = value.get("exception_message") or value.get("message") or value.get("error") or value.get("reason")
        if exc is not None and exc != value:
            return sanitize_user_facing_message(exc, fallback=fallback)
        return fallback

    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[0] == "execution_error":
                return sanitize_user_facing_message(item[1], fallback=fallback)
        for item in value:
            if isinstance(item, Mapping) and ("exception_message" in item or "message" in item):
                return sanitize_user_facing_message(item, fallback=fallback)
        return fallback

    if not isinstance(value, str):
        return fallback

    text = value.strip()
    if not text:
        return fallback

    # Check for raw Python dict repr or JSON object/array
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, Mapping):
                return sanitize_user_facing_message(parsed, fallback=fallback)
        except Exception:
            pass
        return fallback

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        for line in reversed(lines):
            if any(token in line for token in UNSAFE_DIAGNOSTIC_SUBSTRINGS):
                continue
            if line.startswith(("{", "[", "'", '"', "(")):
                continue
            if ":" in line:
                exc_type, _, exc_msg = line.partition(":")
                if (exc_type.endswith("Error") or exc_type.endswith("Exception")) and exc_msg.strip():
                    return line
        last = lines[-1]
        if not any(token in last for token in UNSAFE_DIAGNOSTIC_SUBSTRINGS) and not last.startswith(("{", "[")):
            return last
        return fallback

    has_unsafe = any(token in text for token in UNSAFE_DIAGNOSTIC_SUBSTRINGS)
    if has_unsafe or "{'" in text or '{"' in text or "['" in text or '["' in text:
        if ":" in text:
            exc_type, _, exc_msg = text.partition(":")
            if (exc_type.endswith("Error") or exc_type.endswith("Exception")) and exc_msg.strip():
                return text
        return fallback

    return text


def sanitize_job_record_for_presentation(record: Mapping[str, object]) -> dict[str, object]:
    """Non-destructively sanitize a job record's user-facing messages for UI presentation."""
    if not isinstance(record, Mapping):
        return {}
    job = deepcopy(dict(record))
    failure = job.get("failure")
    if isinstance(failure, Mapping):
        f_copy = deepcopy(dict(failure))
        if "message" in f_copy:
            f_copy["message"] = sanitize_user_facing_message(f_copy.get("message"), fallback="The render job failed.")
        job["failure"] = f_copy
    output_disc = job.get("output_discovery")
    if isinstance(output_disc, Mapping) and isinstance(output_disc.get("failure"), Mapping):
        disc_copy = deepcopy(dict(output_disc))
        disc_fail = deepcopy(dict(disc_copy["failure"]))
        if "message" in disc_fail:
            disc_fail["message"] = sanitize_user_facing_message(disc_fail.get("message"), fallback="Output discovery failed.")
        disc_copy["failure"] = disc_fail
        job["output_discovery"] = disc_copy
    finalization = job.get("finalization")
    if isinstance(finalization, Mapping) and isinstance(finalization.get("failure"), Mapping):
        fin_copy = deepcopy(dict(finalization))
        fin_fail = deepcopy(dict(fin_copy["failure"]))
        if "message" in fin_fail:
            fin_fail["message"] = sanitize_user_facing_message(fin_fail.get("message"), fallback="Final scene finalization failed; retry is available if inputs remain current.")
        fin_copy["failure"] = fin_fail
        job["finalization"] = fin_copy
    return job


def _history_status(entry: Mapping[str, object]) -> tuple[str | None, str | None]:
    status = entry.get("status")
    if not isinstance(status, Mapping):
        return None, None
    status_name = status.get("status_str")
    messages = status.get("messages")
    message = None
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, (list, tuple)) and len(item) > 1:
                event_type, detail_val = item[0], item[1]
                if event_type == "execution_error":
                    if isinstance(detail_val, Mapping):
                        exc = detail_val.get("exception_message") or detail_val.get("message")
                        if isinstance(exc, str) and exc.strip():
                            clean_exc = exc.strip().splitlines()[-1] if "\n" in exc else exc.strip()
                            message = sanitize_user_facing_message(clean_exc, fallback="ComfyUI reported an execution error.")
                            break
                    elif isinstance(detail_val, str) and detail_val.strip():
                        message = sanitize_user_facing_message(detail_val, fallback="ComfyUI reported an execution error.")
                        break
        if not message:
            for item in messages:
                if isinstance(item, (list, tuple)) and len(item) > 1:
                    detail_val = item[1]
                    if isinstance(detail_val, Mapping):
                        exc = detail_val.get("exception_message") or detail_val.get("message")
                        if isinstance(exc, str) and exc.strip():
                            clean_exc = exc.strip().splitlines()[-1] if "\n" in exc else exc.strip()
                            message = sanitize_user_facing_message(clean_exc, fallback="ComfyUI reported an execution error.")
                            break
                    elif isinstance(detail_val, str) and detail_val.strip():
                        clean = sanitize_user_facing_message(detail_val, fallback="ComfyUI reported an execution error.")
                        if clean != "The render job failed.":
                            message = clean
                            break
        if not message and messages:
            message = "ComfyUI reported an execution error."
    return status_name.casefold() if isinstance(status_name, str) else None, message


def _history_terminal_state(entry: Mapping[str, object]) -> tuple[str | None, str | None, str | None]:
    """Return the durable lifecycle result proved by one history entry."""

    status_name, status_message = _history_status(entry)
    state = {
        "success": SUCCEEDED,
        "error": FAILED,
        "interrupted": INTERRUPTED,
        "cancelled": CANCELLED,
        "canceled": CANCELLED,
    }.get(status_name)
    return state, status_name, status_message


def _history_error_is_not_found(error: ComfyUIClientError) -> bool:
    """Treat an HTTP history miss as absence, while preserving other failures."""

    return error.details.get("http_status") == 404 or error.code == "COMFYUI_HISTORY_NOT_FOUND"


def _poll_error_document(error: ComfyUIClientError) -> dict[str, object]:
    return {
        "code": error.code,
        "message": error.message,
        "transient": error.transient,
        "at": _timestamp(),
    }


def _set_poll_error(record: dict[str, object], error: ComfyUIClientError) -> dict[str, object]:
    updated = deepcopy(record)
    updated["last_poll_error"] = _poll_error_document(error)
    updated["updated_at"] = _timestamp()
    updated["last_reconciled_at"] = _timestamp()
    return updated


def _absence_confirmation_count(record: Mapping[str, object]) -> int:
    reconciliation = record.get("reconciliation")
    if isinstance(reconciliation, Mapping):
        value = reconciliation.get("absence_confirmations")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    transitions = record.get("transitions")
    if isinstance(transitions, list) and transitions:
        last = transitions[-1]
        if isinstance(last, Mapping) and last.get("reason") == "prompt_missing_from_queue_and_history":
            return 1
    return 0


def _reconciliation_document(*, attempts: int, fallback: Mapping[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "state": "RECONCILIATION_REQUIRED",
        "absence_confirmations": attempts,
        "required_confirmations": ORPHAN_CONFIRMATION_ATTEMPTS,
        "last_evidence": "queue_absent_history_absent",
    }
    if fallback is not None:
        result["fallback"] = deepcopy(dict(fallback))
    return result


def _fallback_failure_document(error: RenderOutputDiscoveryError) -> dict[str, object]:
    return {
        "category": "deterministic_output_fallback_failure",
        "code": error.code,
        "message": error.message,
        "details": _bounded_json_value(error.details),
    }


def reconcile_project_jobs(
    storage: ProjectStorage,
    project_id: object,
    *,
    client: ComfyUIClient | None = None,
    output_root: Path | None = None,
    media_probe: Callable[[Path], Mapping[str, object]] | None = None,
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
                    "jobs": _overlay_telemetry_jobs(records, comfy),
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
                        "last_poll_error": None,
                        "last_reconciled_at": _timestamp(),
                    },
                )
                store.save(updated)
                continue
            history_error: ComfyUIClientError | None = None
            history_missing = False
            entry: dict[str, object] | None = None
            try:
                history_payload = comfy.get_history(prompt_id)
            except ComfyUIClientError as error:
                history_error = error
                history_missing = _history_error_is_not_found(error)
                if not history_missing:
                    warnings.append({"job_id": record["job_id"], "code": error.code, "message": error.message, "transient": error.transient})
            else:
                entry = _history_entry(history_payload, prompt_id)
            if entry is not None:
                history_state, status_name, status_message = _history_terminal_state(entry)
                if history_state == SUCCEEDED:
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
                            "category": "output_discovery_failure",
                            "code": error.code,
                            "message": error.message,
                        }
                        updated = transition_job(
                            record,
                            SUCCEEDED,
                            reason="history_success_output_discovery_failed",
                            updates={
                                "output": None,
                                "output_discovery": {"state": "FAILED", "failure": failure},
                                "failure": None,
                                "progress": {"kind": "lifecycle", "value": None, "label": "Succeeded"},
                                "reconciliation": None,
                                "last_poll_error": None,
                                "last_reconciled_at": _timestamp(),
                            },
                        )
                        store.save(updated)
                        _clear_telemetry_prompt(comfy, prompt_id)
                    else:
                        updated = transition_job(
                            record,
                            SUCCEEDED,
                            reason="history_success_and_output_discovered",
                            updates={
                                "output": output,
                                "output_discovery": {"state": "AVAILABLE", "failure": None},
                                "failure": None,
                                "progress": {"kind": "lifecycle", "value": None, "label": "Succeeded"},
                                "reconciliation": None,
                                "last_poll_error": None,
                                "last_reconciled_at": _timestamp(),
                            },
                        )
                        store.save(updated)
                        _clear_telemetry_prompt(comfy, prompt_id)
                    continue
                if history_state in {FAILED, INTERRUPTED, CANCELLED}:
                    failure = {
                        "category": "execution_failure",
                        "code": "COMFYUI_EXECUTION_FAILED",
                        "message": status_message or f"ComfyUI reported {status_name or 'an execution'} failure.",
                    }
                    if history_state == INTERRUPTED:
                        failure["code"] = "COMFYUI_EXECUTION_INTERRUPTED"
                    elif history_state == CANCELLED:
                        failure["category"] = "cancellation"
                        failure["code"] = "COMFYUI_EXECUTION_CANCELLED"
                    updated = transition_job(
                        record,
                        history_state,
                        reason="history_terminal_result",
                        updates={
                            "failure": failure if history_state != CANCELLED else None,
                            "output_discovery": None,
                            "progress": None,
                            "reconciliation": None,
                            "last_poll_error": None,
                            "last_reconciled_at": _timestamp(),
                        },
                    )
                    store.save(updated)
                    _clear_telemetry_prompt(comfy, prompt_id)
                    continue
            if prompt_id in running_ids:
                target_state = CANCEL_REQUESTED if record.get("state") == CANCEL_REQUESTED else RUNNING
                updated = transition_job(
                    record,
                    target_state,
                    reason="queue_running",
                    updates={
                        "progress": {"kind": "lifecycle", "value": None, "label": "Cancellation requested" if target_state == CANCEL_REQUESTED else "Running"},
                        "failure": None,
                        "reconciliation": None,
                        "last_poll_error": _poll_error_document(history_error) if history_error and not history_missing else None,
                        "last_reconciled_at": _timestamp(),
                    },
                )
                store.save(updated)
                continue
            if prompt_id in pending_ids:
                target_state = CANCEL_REQUESTED if record.get("state") == CANCEL_REQUESTED else QUEUED
                updated = transition_job(
                    record,
                    target_state,
                    reason="queue_pending",
                    updates={
                        "progress": {"kind": "lifecycle", "value": None, "label": "Cancellation requested" if target_state == CANCEL_REQUESTED else "Queued"},
                        "failure": None,
                        "reconciliation": None,
                        "last_poll_error": _poll_error_document(history_error) if history_error and not history_missing else None,
                        "last_reconciled_at": _timestamp(),
                    },
                )
                store.save(updated)
                continue
            if history_error is not None and not history_missing:
                # Queue was reachable but history was not.  Preserve the last
                # durable lifecycle state and retry; a transport failure is not
                # proof that the prompt disappeared.
                updated = _set_poll_error(record, history_error)
                store.save(updated)
                continue
            cancel = record.get("cancel")
            if record.get("state") == CANCEL_REQUESTED and isinstance(cancel, Mapping) and cancel.get("api_acknowledged") is True:
                target_state = CANCELLED if cancel.get("mode") == "queued_delete" else INTERRUPTED
                updated = transition_job(
                    record,
                    target_state,
                    reason="cancellation_confirmed_by_absence",
                    updates={
                        "progress": {"kind": "lifecycle", "value": None, "label": target_state.title()},
                        "reconciliation": None,
                        "last_reconciled_at": _timestamp(),
                    },
                )
                _clear_telemetry_prompt(comfy, prompt_id)
            else:
                absence_attempts = _absence_confirmation_count(record) + 1
                try:
                    fallback_output = discover_raw_output_fallback(
                        record,
                        output_root=output_root,
                        media_probe=media_probe,
                    )
                except RenderOutputDiscoveryError as error:
                    fallback_failure = _fallback_failure_document(error)
                    if absence_attempts >= ORPHAN_CONFIRMATION_ATTEMPTS:
                        updated = transition_job(
                            record,
                            ORPHANED,
                            reason="prompt_absent_after_bounded_reconciliation",
                            updates={
                                "failure": {
                                    "category": "job_orphaned",
                                    "code": "COMFYUI_JOB_ORPHANED",
                                    "message": "The previous ComfyUI job is no longer available. It was marked orphaned; you may render the scene again.",
                                    "details": {
                                        "absence_confirmations": absence_attempts,
                                        "required_confirmations": ORPHAN_CONFIRMATION_ATTEMPTS,
                                        "fallback": fallback_failure,
                                    },
                                },
                                "output": None,
                                "output_discovery": {"state": "FAILED", "failure": fallback_failure},
                                "reconciliation": {
                                    "state": "ORPHANED",
                                    "absence_confirmations": absence_attempts,
                                    "last_evidence": "queue_absent_history_absent",
                                },
                                "progress": None,
                                "last_poll_error": None,
                                "last_reconciled_at": _timestamp(),
                            },
                        )
                        _clear_telemetry_prompt(comfy, prompt_id)
                    else:
                        updated = transition_job(
                            record,
                            UNKNOWN,
                            reason="prompt_missing_from_queue_and_history",
                            updates={
                                "failure": {
                                    "category": "reconciliation_failure",
                                    "code": "RECONCILIATION_REQUIRED",
                                    "message": "Checking the previous ComfyUI job; confirming whether it is still known by ComfyUI.",
                                    "details": {
                                        "absence_confirmations": absence_attempts,
                                        "required_confirmations": ORPHAN_CONFIRMATION_ATTEMPTS,
                                        "fallback": fallback_failure,
                                    },
                                },
                                "output_discovery": {"state": "FAILED", "failure": fallback_failure},
                                "reconciliation": _reconciliation_document(
                                    attempts=absence_attempts,
                                    fallback=fallback_failure,
                                ),
                                "progress": None,
                                "last_poll_error": None,
                                "last_reconciled_at": _timestamp(),
                            },
                        )
                else:
                    updated = transition_job(
                        record,
                        SUCCEEDED,
                        reason="deterministic_output_fallback_recovered",
                        updates={
                            "failure": None,
                            "output": fallback_output,
                            "output_discovery": {
                                "state": "AVAILABLE",
                                "failure": None,
                                "source": "deterministic_job_output_prefix",
                            },
                            "reconciliation": {
                                "state": "RECOVERED",
                                "absence_confirmations": absence_attempts,
                                "last_evidence": "deterministic_output_prefix_and_media_validation",
                            },
                            "progress": {"kind": "lifecycle", "value": None, "label": "Succeeded"},
                            "last_poll_error": None,
                            "last_reconciled_at": _timestamp(),
                        },
                    )
                    _clear_telemetry_prompt(comfy, prompt_id)
            store.save(updated)
        current_records = store.list_project(canonical_project_id)
        return {
            "project_id": canonical_project_id,
            "jobs": _overlay_telemetry_jobs(current_records, comfy),
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
        _clear_telemetry_prompt(comfy, prompt_id)
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
    compatibility_validator: Callable[[Mapping[str, object], ComfyUIClient], Mapping[str, object]] = validate_live_workflow_compatibility,
) -> dict[str, object]:
    canonical_project_id = validate_project_id(project_id)
    canonical_job_id = _validate_job_id(job_id)
    old = JobStore(storage).find(canonical_project_id, canonical_job_id)
    if old["state"] not in {FAILED, CANCELLED, INTERRUPTED, ORPHANED}:
        raise RenderJobStateError("RETRY_NOT_ALLOWED", "Only a reconciled failed, cancelled, interrupted, or orphaned job may be retried.")
    return submit_render_job(
        storage,
        canonical_project_id,
        old["scene_id"],
        client=client,
        hardware_gate=hardware_gate,
        preflight=preflight,
        preflight_builder=preflight_builder,
        package_loader=package_loader,
        compatibility_validator=compatibility_validator,
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


def _normalise_output_prefix(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value.strip():
        raise RenderOutputDiscoveryError("OUTPUT_PREFIX_INVALID", "The deterministic production output prefix is missing.")
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    parts = tuple(normalized.split("/"))
    if (
        not normalized
        or path.is_absolute()
        or ":" in parts[0]
        or any(not part or part in {".", ".."} for part in parts)
        or any(any(character in part for character in "*?[]") for part in parts)
    ):
        raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The deterministic production output prefix is unsafe.")
    return normalized, parts


def _fallback_media_summary(
    path: Path,
    *,
    media_probe: Callable[[Path], Mapping[str, object]] | None,
) -> dict[str, object]:
    if media_probe is None:
        try:
            from .render_finalize import MediaProbeAdapter, RenderFinalizationError

            probe = MediaProbeAdapter().probe(path)
        except RenderFinalizationError as error:
            raise RenderOutputDiscoveryError(
                "OUTPUT_FALLBACK_MEDIA_INVALID",
                f"The deterministic raw output could not be validated: {error.message}",
                details={"cause": error.code},
            ) from error
    else:
        try:
            probe = media_probe(path)
        except Exception as error:  # pragma: no cover - defensive adapter boundary
            raise RenderOutputDiscoveryError(
                "OUTPUT_FALLBACK_MEDIA_INVALID",
                "The deterministic raw output could not be validated.",
            ) from error
    if not isinstance(probe, Mapping):
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INVALID", "The deterministic raw output probe returned invalid metadata.")
    video_streams = probe.get("video_streams")
    video = probe.get("video")
    if not isinstance(video_streams, list) or not video_streams or not isinstance(video, Mapping):
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INVALID", "The deterministic raw output has no valid video stream.")
    duration_ms = video.get("duration_ms")
    if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool) or not math.isfinite(float(duration_ms)) or duration_ms <= 0:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INCOMPLETE", "The deterministic raw output has no positive duration.")
    width = video.get("width")
    height = video.get("height")
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0 or not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INCOMPLETE", "The deterministic raw output has no valid video dimensions.")
    frame_count = video.get("frame_count")
    if frame_count is not None and (not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0):
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INCOMPLETE", "The deterministic raw output has invalid frame coverage.")
    try:
        stat_before = path.stat()
        stat_after = path.stat()
    except OSError as error:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INVALID", "The deterministic raw output could not be inspected safely.") from error
    if stat_before.st_size <= 0 or stat_before.st_size != stat_after.st_size or stat_before.st_mtime_ns != stat_after.st_mtime_ns:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_MEDIA_INCOMPLETE", "The deterministic raw output is empty or still changing.")
    return {
        "validated": True,
        "duration_ms": int(round(float(duration_ms))),
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "video_stream_count": len(video_streams),
    }


def discover_raw_output_fallback(
    record: Mapping[str, object],
    *,
    output_root: Path | None = None,
    media_probe: Callable[[Path], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Recover one complete raw video using the job-owned output prefix only."""

    if not isinstance(record, Mapping):
        raise RenderOutputDiscoveryError("OUTPUT_RECORD_INVALID", "The render job record is invalid.")
    prompt_id = record.get("comfy_prompt_id")
    canonical_prompt_id = _canonical_prompt_id(prompt_id)
    job_id = _validate_job_id(record.get("job_id"))
    expected = record.get("production_output")
    if not isinstance(expected, Mapping) or expected.get("raw_h3_only") is not True:
        raise RenderOutputDiscoveryError("OUTPUT_RECORD_INVALID", "The render job has no raw H3 output contract.")
    output_node_id = expected.get("node_id")
    if not isinstance(output_node_id, str) or not output_node_id:
        raise RenderOutputDiscoveryError("OUTPUT_NODE_MISSING", "The render job has no known production output node.")
    normalized_prefix, prefix_parts = _normalise_output_prefix(expected.get("filename_prefix"))
    try:
        root = Path(output_root) if output_root is not None else _default_comfy_output_root()
        if root.is_symlink() or not root.is_dir():
            raise RenderOutputDiscoveryError("OUTPUT_ROOT_INVALID", "The ComfyUI output root is unavailable or unsafe.")
        root_resolved = root.resolve(strict=True)
    except RenderOutputDiscoveryError:
        raise
    except (OSError, RuntimeError) as error:
        raise RenderOutputDiscoveryError("OUTPUT_ROOT_INVALID", "The ComfyUI output root is unavailable or unsafe.") from error
    prefix_path = root.joinpath(*prefix_parts)
    parent = prefix_path.parent
    current = root
    for part in prefix_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The deterministic output namespace contains a symlink.")
    if not parent.is_dir() or parent.is_symlink():
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_NOT_FOUND", "No deterministic raw H3 output matched the owning job prefix.")
    candidates: list[tuple[Path, str]] = []
    prefix_name = prefix_path.name
    try:
        entries = list(parent.iterdir())
    except OSError as error:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_NOT_FOUND", "The deterministic raw H3 output namespace could not be read.") from error
    for candidate in entries:
        if candidate.is_symlink() or not candidate.is_file() or not candidate.name.startswith(prefix_name):
            continue
        if candidate.suffix.lower() not in OUTPUT_VIDEO_SUFFIXES:
            continue
        relative_parent = "/".join(prefix_parts[:-1])
        resolved, relative_path = _safe_relative_output(root, relative_parent, candidate.name)
        if root_resolved not in resolved.parents:
            raise RenderOutputDiscoveryError("OUTPUT_PATH_UNSAFE", "The deterministic raw output is outside the ComfyUI output root.")
        candidates.append((resolved, relative_path))
    if not candidates:
        raise RenderOutputDiscoveryError("OUTPUT_FALLBACK_NOT_FOUND", "No deterministic raw H3 output matched the owning job prefix.")
    if len(candidates) != 1:
        raise RenderOutputDiscoveryError(
            "OUTPUT_FALLBACK_AMBIGUOUS",
            "More than one deterministic raw H3 output matched the owning job prefix.",
            details={"candidate_count": len(candidates), "prefix": normalized_prefix},
        )
    resolved, relative_path = candidates[0]
    media = _fallback_media_summary(resolved, media_probe=media_probe)
    relative = PurePosixPath(relative_path)
    return {
        "prompt_id": canonical_prompt_id,
        "job_id": job_id,
        "output_node_id": output_node_id,
        "relative_path": relative_path,
        "filename": relative.name,
        "subfolder": relative.parent.as_posix() if relative.parent.as_posix() != "." else "",
        "format": expected.get("format") or "video",
        "raw_h3_output": True,
        "history_verified": False,
        "discovery_source": "deterministic_job_output_prefix",
        "media_validation": media,
        "discovered_at": _timestamp(),
    }


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
    "ComfyUITelemetryAdapter",
    "FAILED",
    "INTERRUPTED",
    "ORPHANED",
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
    "discover_raw_output_fallback",
    "new_job_record",
    "reconcile_project_jobs",
    "retry_render_job",
    "submit_render_job",
    "transition_job",
]
