"""Volatile, prompt-correlated telemetry for the local ComfyUI execution session.

ComfyUI's queue and history endpoints remain the durable lifecycle authority.
This module only retains the latest in-process WebSocket observation for owned
prompt IDs.  It is deliberately independent from project persistence so a
disconnect, process restart, or stale event cannot manufacture a failed job.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import math
import re
import socket
import threading
import time
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid


PROMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
MAX_PROGRESS_NODES = 512
MAX_NODE_HISTORY = 512
MAX_RECONNECT_DELAY_SECONDS = 5.0
TELEMETRY_FRESHNESS_SECONDS = 10.0

LOGGER = logging.getLogger(__name__)

_TERMINAL_EVENTS = frozenset({
    "execution_error",
    "execution_interrupted",
    "execution_success",
    "execution_complete",
    "execution_done",
})


def _safe_prompt_id(value: object) -> str | None:
    return value if isinstance(value, str) and PROMPT_ID_PATTERN.fullmatch(value) else None


def _safe_node_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and NODE_ID_PATTERN.fullmatch(value) else None


def _safe_node_type(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    return value


def _finite_number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_comfyui_progress(value: object, maximum: object) -> dict[str, object] | None:
    """Normalize a valid node-local ``value/max`` pair.

    ComfyUI progress is node-local.  The returned fraction is presentation
    data only and is never treated as whole-workflow completion.
    """

    numeric_value = _finite_number(value)
    numeric_maximum = _finite_number(maximum)
    if numeric_value is None or numeric_maximum is None or float(numeric_maximum) <= 0:
        return None
    fraction = _clamp(float(numeric_value) / float(numeric_maximum))
    return {
        "value": fraction,
        "max": 1.0,
        "fraction": fraction,
        "percent": int(round(fraction * 100)),
        "raw_value": numeric_value,
        "raw_max": numeric_maximum,
        "scope": "node",
        "node_local": True,
    }


def parse_comfyui_event(message: object) -> dict[str, object] | None:
    """Decode one installed ComfyUI JSON WebSocket event safely.

    Binary preview frames, malformed JSON, unknown wrappers, and non-object
    payloads are ignored.  Event-specific validation is performed by the
    adapter after prompt ownership has been checked.
    """

    if isinstance(message, bytes):
        return None
    value: object = message
    if isinstance(message, str):
        try:
            value = json.loads(message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if not isinstance(value, Mapping):
        return None
    event_type = value.get("type")
    data = value.get("data")
    if not isinstance(event_type, str) or not event_type or len(event_type) > 128 or not isinstance(data, Mapping):
        return None
    return {"type": event_type, "data": dict(data)}


def _node_stage(class_type: str | None) -> str:
    if class_type in {"UNETLoader", "CLIPLoader", "VAELoader"}:
        return "Loading models"
    if class_type in {"LoadImage", "LoadAudio"}:
        return "Loading inputs"
    if class_type in {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"}:
        return "Preparing conditioning"
    if class_type == "SamplerCustomAdvanced":
        return "Generating video"
    if class_type in {"BasicGuider", "BasicScheduler", "RandomNoise", "KSamplerSelect"}:
        return "Preparing conditioning"
    if class_type in {"LTXVSeparateAVLatent", "VAEEncodeAudio", "SolidMask", "SetLatentNoiseMask", "LTXVConcatAVLatent"}:
        return "Preparing conditioning"
    if class_type in {"VAEDecode", "VAEDecodeAudio"}:
        return "Decoding video"
    if class_type == "VHS_VideoCombine":
        return "Encoding video"
    return "Rendering"


def stage_for_node(class_type: object) -> str:
    """Return a compact stage label without exposing an arbitrary node ID."""

    return _node_stage(_safe_node_type(class_type))


def _workflow_node_index(workflow: object) -> dict[str, str]:
    if not isinstance(workflow, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_node_id, raw_node in list(workflow.items())[:MAX_PROGRESS_NODES]:
        node_id = _safe_node_id(raw_node_id)
        class_type = raw_node.get("class_type") if isinstance(raw_node, Mapping) else None
        class_type = _safe_node_type(class_type)
        if node_id is not None and class_type is not None:
            result[node_id] = class_type
    return result


def _local_websocket_url(base_url: str, client_id: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("ComfyUI telemetry is restricted to the local host.")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/ws"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["clientId"] = client_id
    return urlunsplit((scheme, parsed.netloc, path, urlencode(query), ""))


class _SyncWebSocketConnection:
    """Normalize the installed ``websockets.sync`` receive timeout contract."""

    def __init__(self, connection: object):
        self._connection = connection

    def recv(self) -> object:
        return getattr(self._connection, "recv")(timeout=1.0)

    def close(self) -> None:
        getattr(self._connection, "close")()


def _default_websocket_factory(url: str) -> object:
    """Create telemetry with the WebSocket client installed beside ComfyUI."""

    from websockets.sync.client import connect

    return _SyncWebSocketConnection(connect(url, open_timeout=1.0, close_timeout=1.0))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ComfyUITelemetryAdapter:
    """One reconnecting WebSocket session shared by Builder render jobs."""

    def __init__(
        self,
        base_url: str,
        *,
        websocket_factory: Callable[[str], object] | None = None,
        client_id_factory: Callable[[], str] | None = None,
        worker_enabled: bool = True,
        reconnect_delays: tuple[float, ...] | None = None,
    ):
        self.base_url = base_url
        self.client_id = (client_id_factory or (lambda: uuid.uuid4().hex))()
        if not isinstance(self.client_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", self.client_id):
            raise ValueError("ComfyUI telemetry client ID is invalid.")
        self.websocket_url = _local_websocket_url(base_url, self.client_id)
        self._websocket_factory = websocket_factory or _default_websocket_factory
        self._worker_enabled = worker_enabled
        self._reconnect_delays = tuple(
            max(0.0, min(float(delay), MAX_RECONNECT_DELAY_SECONDS))
            for delay in (reconnect_delays or (0.05, 0.1, 0.25, 1.0))
        ) or (0.1,)
        self._lock = threading.RLock()
        self._owned_prompts: set[str] = set()
        self._workflow_nodes: dict[str, dict[str, str]] = {}
        self._snapshots: dict[str, dict[str, object]] = {}
        self._connection: object | None = None
        self._connection_state = "disconnected"
        self._connection_count = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection_requested = threading.Event()
        self._connected = threading.Event()
        self._connection_failure_count = 0
        self._last_connection_error: str | None = None
        LOGGER.info(
            "ComfyUI telemetry session configured client_id=%s websocket_url=%s",
            self.client_id,
            self.websocket_url,
        )

    @property
    def connection_state(self) -> str:
        with self._lock:
            return self._connection_state

    @property
    def connection_count(self) -> int:
        with self._lock:
            return self._connection_count

    def _new_snapshot(self, prompt_id: str) -> dict[str, object]:
        return {
            "prompt_id": prompt_id,
            "state": "RUNNING",
            "observed": False,
            "current_node_id": None,
            "current_node_type": None,
            "stage": None,
            "progress": None,
            "progress_scope": None,
            "progress_reset": False,
            "cached_node_ids": [],
            "completed_node_ids": [],
            "last_event": None,
            "updated_at": None,
            "_last_event_monotonic": None,
        }

    def register_prompt(self, prompt_id: object, workflow: object = None, *, start: bool = True) -> bool:
        canonical = _safe_prompt_id(prompt_id)
        if canonical is None:
            return False
        with self._lock:
            is_new = canonical not in self._owned_prompts
            self._owned_prompts.add(canonical)
            if is_new or canonical not in self._snapshots:
                self._snapshots[canonical] = self._new_snapshot(canonical)
            if workflow is not None:
                self._workflow_nodes[canonical] = _workflow_node_index(workflow)
        if start:
            self.ensure_started()
        return True

    def clear_prompt(self, prompt_id: object) -> None:
        canonical = _safe_prompt_id(prompt_id)
        if canonical is None:
            return
        with self._lock:
            self._owned_prompts.discard(canonical)
            self._workflow_nodes.pop(canonical, None)
            self._snapshots.pop(canonical, None)

    def close(self) -> None:
        self._stop.set()
        self._connected.set()
        with self._lock:
            connection = self._connection
            self._connection = None
            self._connection_state = "disconnected"
            self._owned_prompts.clear()
            self._workflow_nodes.clear()
            self._snapshots.clear()
        _close_websocket(connection)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def ensure_started(self) -> None:
        if not self._worker_enabled or self._stop.is_set():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vesper-comfyui-telemetry", daemon=True)
            self._thread.start()

    def ensure_connected(self, timeout_seconds: float = 1.0) -> bool:
        """Request the owned session before submission without gating the POST."""

        timeout = max(0.0, min(float(timeout_seconds), 5.0))
        if not self._worker_enabled or self._stop.is_set():
            return False
        with self._lock:
            if self._connection_state == "connected":
                return True
        self._connected.clear()
        self._connection_requested.set()
        self.ensure_started()
        self._connected.wait(timeout)
        with self._lock:
            return self._connection_state == "connected"

    def _set_connection_state(self, state: str, connection: object | None = None) -> None:
        with self._lock:
            self._connection_state = state
            self._connection = connection
        if state == "connected":
            self._connected.set()
        else:
            self._connected.clear()

    def _wait_for_reconnect(self, attempt: int) -> None:
        delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
        self._stop.wait(delay)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            with self._lock:
                has_owned_prompts = bool(self._owned_prompts)
            if not has_owned_prompts and not self._connection_requested.is_set():
                self._stop.wait(0.1)
                continue
            self._set_connection_state("connecting")
            connection: object | None = None
            connection_error: Exception | None = None
            try:
                LOGGER.debug(
                    "Connecting ComfyUI telemetry client_id=%s websocket_url=%s",
                    self.client_id,
                    self.websocket_url,
                )
                connection = self._websocket_factory(self.websocket_url)
                _set_websocket_timeout(connection)
                with self._lock:
                    self._connection_count += 1
                self._set_connection_state("connected", connection)
                self._connection_requested.clear()
                LOGGER.info("ComfyUI telemetry connected client_id=%s", self.client_id)
                attempt = 0
                while not self._stop.is_set():
                    try:
                        raw = getattr(connection, "recv")()
                    except (TimeoutError, socket.timeout):
                        continue
                    except Exception as error:
                        connection_error = error
                        break
                    if raw in (None, "", b""):
                        break
                    self.handle_message(raw)
            except Exception as error:
                connection_error = error
            finally:
                _close_websocket(connection)
                self._set_connection_state("disconnected")
            if connection_error is not None:
                error_text = f"{connection_error.__class__.__name__}: {connection_error}"
                with self._lock:
                    self._connection_failure_count += 1
                    failure_count = self._connection_failure_count
                    changed = error_text != self._last_connection_error
                    self._last_connection_error = error_text
                log = LOGGER.warning if failure_count == 1 or changed else LOGGER.debug
                log(
                    "ComfyUI telemetry disconnected client_id=%s attempt=%s error=%s",
                    self.client_id,
                    failure_count,
                    error_text,
                )
            if not self._stop.is_set():
                self._wait_for_reconnect(attempt)
                attempt = min(attempt + 1, len(self._reconnect_delays) - 1)

    def _node_details(self, prompt_id: str, node_id: object, event_data: Mapping[str, object]) -> tuple[str | None, str]:
        canonical_node = _safe_node_id(node_id)
        node_type = None
        with self._lock:
            node_type = self._workflow_nodes.get(prompt_id, {}).get(canonical_node or "")
        if node_type is None:
            node_type = _safe_node_type(event_data.get("class_type"))
        return canonical_node, _node_stage(node_type)

    def _set_node(self, snapshot: dict[str, object], prompt_id: str, node_id: object, data: Mapping[str, object]) -> None:
        canonical_node, stage = self._node_details(prompt_id, node_id, data)
        snapshot["current_node_id"] = canonical_node
        node_type = None
        with self._lock:
            if canonical_node is not None:
                node_type = self._workflow_nodes.get(prompt_id, {}).get(canonical_node)
        if node_type is None:
            node_type = _safe_node_type(data.get("class_type"))
        snapshot["current_node_type"] = node_type
        snapshot["stage"] = stage if canonical_node is not None else None

    def _progress_for_node(self, node_id: object, node_data: Mapping[str, object]) -> dict[str, object] | None:
        progress = normalize_comfyui_progress(node_data.get("value"), node_data.get("max"))
        if progress is not None:
            canonical_node = _safe_node_id(node_id)
            if canonical_node is not None:
                progress["node_id"] = canonical_node
        return progress

    def _select_progress_state_node(self, snapshot: Mapping[str, object], nodes: Mapping[str, object]) -> tuple[str | None, Mapping[str, object] | None]:
        current = _safe_node_id(snapshot.get("current_node_id"))
        current_data = nodes.get(current) if current is not None else None
        if current is not None and isinstance(current_data, Mapping) and current_data.get("state") in {None, "running"}:
            return current, current_data
        for raw_node_id, raw_node in list(nodes.items())[:MAX_PROGRESS_NODES]:
            if isinstance(raw_node, Mapping) and raw_node.get("state") == "running":
                return _safe_node_id(raw_node_id), raw_node
        if current is not None and isinstance(current_data, Mapping):
            return current, current_data
        for raw_node_id, raw_node in list(nodes.items())[:MAX_PROGRESS_NODES]:
            if isinstance(raw_node, Mapping):
                return _safe_node_id(raw_node_id), raw_node
        return None, None

    def handle_message(self, message: object) -> bool:
        event = parse_comfyui_event(message)
        if event is None:
            return False
        event_type = str(event["type"])
        data = event["data"]
        prompt_id = _safe_prompt_id(data.get("prompt_id"))
        if prompt_id is None:
            # The installed status event has no prompt ID and is only useful
            # for queue reconciliation, which remains an HTTP responsibility.
            return False
        with self._lock:
            if prompt_id not in self._owned_prompts:
                return False
            LOGGER.debug(
                "Accepted ComfyUI telemetry client_id=%s prompt_id=%s event=%s node_id=%s",
                self.client_id,
                prompt_id,
                event_type,
                data.get("node", data.get("node_id")),
            )
            snapshot = self._snapshots.setdefault(prompt_id, self._new_snapshot(prompt_id))
            snapshot["last_event"] = event_type
            snapshot["observed"] = True
            snapshot["_last_event_monotonic"] = time.monotonic()
            snapshot["updated_at"] = _timestamp()
            if event_type == "execution_start":
                self._snapshots[prompt_id] = self._new_snapshot(prompt_id)
                snapshot = self._snapshots[prompt_id]
                snapshot["last_event"] = event_type
                snapshot["observed"] = True
                snapshot["_last_event_monotonic"] = time.monotonic()
                snapshot["updated_at"] = _timestamp()
                return True
            if event_type in _TERMINAL_EVENTS:
                self._owned_prompts.discard(prompt_id)
                self._workflow_nodes.pop(prompt_id, None)
                self._snapshots.pop(prompt_id, None)
                return True
            if event_type == "execution_cached":
                cached = [_safe_node_id(item) for item in data.get("nodes", [])] if isinstance(data.get("nodes"), list) else []
                snapshot["cached_node_ids"] = [item for item in cached[:MAX_NODE_HISTORY] if item is not None]
                snapshot["state"] = "RUNNING"
                snapshot["progress"] = None
                snapshot["progress_scope"] = None
                return True
            if event_type == "executing":
                self._set_node(snapshot, prompt_id, data.get("node"), data)
                snapshot["state"] = "RUNNING"
                snapshot["progress"] = None
                snapshot["progress_scope"] = None
                snapshot["progress_reset"] = True
                return True
            if event_type == "progress":
                node_id = data.get("node", data.get("node_id"))
                if _safe_node_id(node_id) is not None:
                    self._set_node(snapshot, prompt_id, node_id, data)
                snapshot["state"] = "RUNNING"
                snapshot["progress"] = self._progress_for_node(node_id, data)
                snapshot["progress_scope"] = "node" if snapshot["progress"] is not None else None
                snapshot["progress_reset"] = False
                return True
            if event_type == "progress_state":
                raw_nodes = data.get("nodes")
                nodes = raw_nodes if isinstance(raw_nodes, Mapping) else {}
                node_id, node_data = self._select_progress_state_node(snapshot, nodes)
                if node_id is not None and isinstance(node_data, Mapping):
                    self._set_node(snapshot, prompt_id, node_id, node_data)
                    snapshot["progress"] = self._progress_for_node(node_id, node_data)
                else:
                    snapshot["progress"] = None
                snapshot["progress_scope"] = "node" if snapshot["progress"] is not None else None
                snapshot["state"] = "RUNNING"
                snapshot["progress_reset"] = False
                return True
            if event_type == "executed":
                node_id = _safe_node_id(data.get("node", data.get("node_id")))
                if node_id is not None:
                    completed = snapshot.get("completed_node_ids")
                    completed = completed if isinstance(completed, list) else []
                    if node_id not in completed:
                        completed.append(node_id)
                    snapshot["completed_node_ids"] = completed[-MAX_NODE_HISTORY:]
                snapshot["progress"] = None
                snapshot["progress_scope"] = None
                snapshot["progress_reset"] = True
                snapshot["state"] = "RUNNING"
                return True
        return False

    def snapshot(self, prompt_id: object) -> dict[str, object] | None:
        canonical = _safe_prompt_id(prompt_id)
        if canonical is None:
            return None
        with self._lock:
            snapshot = self._snapshots.get(canonical)
            if snapshot is None or canonical not in self._owned_prompts:
                return None
            result = deepcopy(snapshot)
            result["connection_state"] = self._connection_state
            result["available"] = self._connection_state == "connected"
            result["connected"] = self._connection_state == "connected"
            result["current_stage"] = result.get("stage") or "Rendering"
            last_event_monotonic = result.pop("_last_event_monotonic", None)
            result["telemetry_stale"] = not result.get("observed") or not isinstance(last_event_monotonic, (int, float)) or (
                time.monotonic() - float(last_event_monotonic) > TELEMETRY_FRESHNESS_SECONDS
            )
            progress = result.get("progress")
            result["numeric_progress"] = isinstance(progress, Mapping) and isinstance(progress.get("percent"), int)
            result["progress_value"] = progress.get("raw_value") if isinstance(progress, Mapping) else None
            result["progress_max"] = progress.get("raw_max") if isinstance(progress, Mapping) else None
            result["progress_percent"] = progress.get("percent") if isinstance(progress, Mapping) else None
            return result


def _set_websocket_timeout(connection: object | None) -> None:
    setter = getattr(connection, "settimeout", None)
    if callable(setter):
        try:
            setter(1.0)
        except Exception:
            pass


def _close_websocket(connection: object | None) -> None:
    closer = getattr(connection, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


_SHARED_LOCK = threading.Lock()
_SHARED_ADAPTERS: dict[str, ComfyUITelemetryAdapter] = {}


def get_shared_telemetry_adapter(base_url: str) -> ComfyUITelemetryAdapter:
    with _SHARED_LOCK:
        adapter = _SHARED_ADAPTERS.get(base_url)
        if adapter is None:
            adapter = ComfyUITelemetryAdapter(base_url)
            _SHARED_ADAPTERS[base_url] = adapter
        return adapter


def reset_shared_telemetry_adapters() -> None:
    with _SHARED_LOCK:
        adapters = list(_SHARED_ADAPTERS.values())
        _SHARED_ADAPTERS.clear()
    for adapter in adapters:
        adapter.close()


__all__ = [
    "ComfyUITelemetryAdapter",
    "get_shared_telemetry_adapter",
    "normalize_comfyui_progress",
    "parse_comfyui_event",
    "reset_shared_telemetry_adapters",
    "stage_for_node",
]
