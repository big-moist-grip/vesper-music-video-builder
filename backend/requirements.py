"""Deterministic local dependency checks for the approved H3 manifests.

This module deliberately reads only the production manifest registry.  Donor
workflow files are not a source of runtime requirements for the Builder.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import importlib
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .workflows import production_manifest_registry


LOGGER = logging.getLogger(__name__)

STATUS_AVAILABLE = "AVAILABLE"
STATUS_MISSING = "MISSING"
STATUS_UNKNOWN = "UNKNOWN"
REQUIREMENT_STATUSES = (STATUS_AVAILABLE, STATUS_MISSING, STATUS_UNKNOWN)
REQUIREMENTS_CACHE_VERSION = "phase8a4-runtime-requirements-v1"


@dataclass(frozen=True)
class RequirementsSnapshot:
    """Process-local stable runtime discovery result and its provenance."""

    report: dict[str, object]
    generated_at: float
    cache_identity: str
    source_state: str
    scan_duration_ms: float
    cache_status: str
    error: str | None = None

    def report_copy(self) -> dict[str, object]:
        return deepcopy(self.report)

    def metadata(self) -> dict[str, object]:
        return {
            "status": self.cache_status,
            "source_state": self.source_state,
            "generated_at": self.generated_at,
            "cache_identity": self.cache_identity,
            "scan_duration_ms": round(self.scan_duration_ms, 3),
            "error": self.error,
        }


@dataclass
class _RequirementsScan:
    identity: str
    generation: int
    event: threading.Event
    snapshot: RequirementsSnapshot | None = None


_CACHE_LOCK = threading.Lock()
_CACHE_GENERATION = 0
_CACHED_SNAPSHOT: RequirementsSnapshot | None = None
_IN_FLIGHT_SCAN: _RequirementsScan | None = None

OPTIONAL_NODE_TYPES = {
    "rtx_vsr": ("RTXVideoSuperResolution",),
    "seedvr2": (
        "SeedVR2VideoUpscaler",
        "SeedVR2Conditioning",
        "SeedVR2PostProcessing",
        "SeedVR2Preprocess",
        "SeedVR2TemporalChunk",
        "SeedVR2TemporalMerge",
    ),
}


def _status_item(status: str, **values: object) -> dict[str, object]:
    if status not in REQUIREMENT_STATUSES:
        raise ValueError("Unknown requirement status.")
    return {"status": status, **values}


def _requirements_cache_identity() -> str:
    """Return a cheap identity for the stable runtime contract inputs."""

    registry = production_manifest_registry()
    serialized = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{REQUIREMENTS_CACHE_VERSION}:{digest}"


def _failed_scan_report(message: str) -> dict[str, object]:
    return {
        "response_version": 1,
        "source": "backend.requirements",
        "methods": {},
        "shared": {"nodes": [], "models": []},
        "required_tools": {},
        "required_ready": False,
        "optional": {},
        "discovery_status": "ERROR",
        "error": message,
    }


def _run_requirements_scan(
    scanner: Callable[[], dict[str, object]],
    identity: str,
    cache_status: str,
) -> RequirementsSnapshot:
    started = time.perf_counter()
    error: str | None = None
    source_state = "success"
    try:
        report = scanner()
        if not isinstance(report, dict):
            raise TypeError("Requirements discovery returned an invalid report.")
    except Exception as scan_error:  # Runtime discovery must fail closed, not crash preflight.
        source_state = "error"
        error = str(scan_error) or scan_error.__class__.__name__
        LOGGER.warning("Runtime requirements discovery failed: %s", error)
        report = _failed_scan_report(error)
    return RequirementsSnapshot(
        report=deepcopy(report),
        generated_at=time.time(),
        cache_identity=identity,
        source_state=source_state,
        scan_duration_ms=(time.perf_counter() - started) * 1000,
        cache_status=cache_status,
        error=error,
    )


def get_requirements_snapshot(
    *,
    force_refresh: bool = False,
    scanner: Callable[[], dict[str, object]] | None = None,
    cache_key: str | None = None,
) -> RequirementsSnapshot:
    """Return stable runtime discovery, coalescing identical in-flight scans.

    The default scanner is the authoritative :func:`scan_requirements` path.
    ``scanner`` and ``cache_key`` are intentionally injectable for deterministic
    tests; project state is never part of this cache identity.
    """

    authoritative = scanner is None
    scan_callable = scan_requirements if scanner is None else scanner
    identity = cache_key or (
        _requirements_cache_identity()
        if authoritative
        else f"{REQUIREMENTS_CACHE_VERSION}:injected:{id(scan_callable)}"
    )

    global _IN_FLIGHT_SCAN, _CACHED_SNAPSHOT
    while True:
        with _CACHE_LOCK:
            cached = _CACHED_SNAPSHOT
            if (
                not force_refresh
                and cached is not None
                and cached.cache_identity == identity
                and cached.source_state == "success"
            ):
                return replace(cached, cache_status="warm")

            in_flight = _IN_FLIGHT_SCAN
            if in_flight is None or in_flight.identity != identity:
                in_flight = _RequirementsScan(
                    identity=identity,
                    generation=_CACHE_GENERATION,
                    event=threading.Event(),
                )
                _IN_FLIGHT_SCAN = in_flight
                owner = True
            else:
                owner = False

        if owner:
            break

        in_flight.event.wait()
        if in_flight.snapshot is not None:
            return replace(in_flight.snapshot, cache_status="coalesced")

    status = "rescan" if force_refresh else "cold"
    snapshot = _run_requirements_scan(scan_callable, identity, status)
    with _CACHE_LOCK:
        if snapshot.source_state == "success" and in_flight.generation == _CACHE_GENERATION:
            _CACHED_SNAPSHOT = snapshot
        in_flight.snapshot = snapshot
        if _IN_FLIGHT_SCAN is in_flight:
            _IN_FLIGHT_SCAN = None
        in_flight.event.set()
    return snapshot


def clear_requirements_snapshot_cache() -> None:
    """Invalidate process-local runtime discovery without touching projects."""

    global _CACHE_GENERATION, _CACHED_SNAPSHOT
    with _CACHE_LOCK:
        _CACHE_GENERATION += 1
        _CACHED_SNAPSHOT = None


def requirements_snapshot_cache_info() -> dict[str, object]:
    with _CACHE_LOCK:
        if _CACHED_SNAPSHOT is None:
            return {"status": "empty", "cache_identity": None}
        return _CACHED_SNAPSHOT.metadata()


def _load_active_node_types() -> tuple[set[str] | None, str]:
    """Read the node registry already loaded by the active ComfyUI process."""

    try:
        nodes = importlib.import_module("nodes")
        mapping = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
    except (ImportError, RuntimeError, OSError):
        return None, "ComfyUI node registry could not be imported."
    if not isinstance(mapping, dict) or not mapping:
        return None, "ComfyUI node registry is not available yet."
    return {name for name in mapping if isinstance(name, str)}, "nodes.NODE_CLASS_MAPPINGS"


def _node_status(
    node_type: str,
    active_node_types: set[str] | None,
    source: str,
) -> dict[str, object]:
    if active_node_types is None:
        return _status_item(STATUS_UNKNOWN, node_type=node_type, source=source)
    status = STATUS_AVAILABLE if node_type in active_node_types else STATUS_MISSING
    return _status_item(status, node_type=node_type, source=source)


def _folder_paths_module():
    try:
        return importlib.import_module("folder_paths")
    except (ImportError, RuntimeError, OSError):
        return None


def _known_model_category(folder_paths: object, category: str) -> bool | None:
    categories = getattr(folder_paths, "folder_names_and_paths", None)
    if isinstance(categories, dict):
        return category in categories
    return None


def _model_status(
    declaration: dict[str, object],
    folder_paths: object | None,
) -> dict[str, object]:
    category = declaration["category"]
    filename = declaration["filename"]
    result = {
        "category": category,
        "filename": filename,
    }
    if "role" in declaration:
        result["role"] = declaration["role"]
    if folder_paths is None:
        return _status_item(STATUS_UNKNOWN, **result, resolved_path=None)

    known_category = _known_model_category(folder_paths, category)
    if known_category is False:
        return _status_item(STATUS_UNKNOWN, **result, resolved_path=None)

    get_full_path = getattr(folder_paths, "get_full_path", None)
    get_filename_list = getattr(folder_paths, "get_filename_list", None)
    try:
        resolved = get_full_path(category, filename) if callable(get_full_path) else None
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        resolved = None
    if resolved:
        try:
            safe_path = str(Path(resolved).resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            safe_path = str(resolved)
        return _status_item(STATUS_AVAILABLE, **result, resolved_path=safe_path)

    if callable(get_filename_list):
        try:
            filenames = get_filename_list(category)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            filenames = None
        if filenames is not None and filename in filenames:
            return _status_item(STATUS_AVAILABLE, **result, resolved_path=None)
        if filenames is not None:
            return _status_item(STATUS_MISSING, **result, resolved_path=None)

    return _status_item(STATUS_UNKNOWN, **result, resolved_path=None)


def _binary_status(name: str, finder: Callable[[str], str | None] = shutil.which) -> dict[str, object]:
    try:
        executable = finder(name)
    except (OSError, RuntimeError, TypeError):
        return _status_item(STATUS_UNKNOWN, name=name, path=None, version=None)
    if not executable:
        return _status_item(STATUS_MISSING, name=name, path=None, version=None)

    version = None
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        output = (completed.stdout or completed.stderr or "").splitlines()
        if output:
            version = output[0].strip()[:240] or None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        version = None
    return _status_item(STATUS_AVAILABLE, name=name, path=str(Path(executable).resolve()), version=version)


def _optional_node_status(
    capability: str,
    active_node_types: set[str] | None,
    source: str,
) -> dict[str, object]:
    candidates = OPTIONAL_NODE_TYPES[capability]
    if active_node_types is None:
        return _status_item(
            STATUS_UNKNOWN,
            capability=capability,
            available_nodes=[],
            candidate_node_types=list(candidates),
            source=source,
            runtime_qualification="DEFERRED_TARGET_NVIDIA" if capability == "rtx_vsr" else "NOT_APPLICABLE",
        )
    available = [candidate for candidate in candidates if candidate in active_node_types]
    status = STATUS_AVAILABLE if available else STATUS_MISSING
    return _status_item(
        status,
        capability=capability,
        available_nodes=available,
        candidate_node_types=list(candidates),
        source=source,
        runtime_qualification="DEFERRED_TARGET_NVIDIA" if capability == "rtx_vsr" else "NOT_APPLICABLE",
    )


def _all_model_declarations(registry: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for method in registry:
        for declaration in registry[method]["required_models"]:
            key = (str(declaration["category"]), str(declaration["filename"]))
            unique.setdefault(
                key,
                {
                    "category": key[0],
                    "filename": key[1],
                    "methods": [],
                },
            )
            unique[key]["methods"].append(method)
    return [unique[key] for key in sorted(unique)]


def _all_node_declarations(registry: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[str, dict[str, object]] = {}
    for method in registry:
        for node_type in registry[method]["required_node_types"]:
            unique.setdefault(node_type, {"node_type": node_type, "methods": []})
            unique[node_type]["methods"].append(method)
    return [unique[node_type] for node_type in sorted(unique)]


def scan_requirements(
    *,
    node_types: set[str] | None = None,
    folder_paths_module: object | None = None,
    binary_finder: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    """Return a stable JSON-safe report for the two approved production methods."""

    registry = production_manifest_registry()
    if node_types is None:
        active_node_types, node_source = _load_active_node_types()
    else:
        active_node_types, node_source = set(node_types), "injected node registry"
    folder_paths = folder_paths_module if folder_paths_module is not None else _folder_paths_module()

    methods: dict[str, object] = {}
    for method, manifest in registry.items():
        nodes = [_node_status(node_type, active_node_types, node_source) for node_type in manifest["required_node_types"]]
        models = [_model_status(declaration, folder_paths) for declaration in manifest["required_models"]]
        methods[method] = {
            "workflow_id": manifest["workflow_id"],
            "required_nodes": nodes,
            "required_models": models,
            "ready": all(item["status"] == STATUS_AVAILABLE for item in [*nodes, *models]),
        }

    shared_models = []
    for declaration in _all_model_declarations(registry):
        status = _model_status(declaration, folder_paths)
        shared_models.append({**status, "methods": list(declaration["methods"])})
    shared_nodes = []
    for declaration in _all_node_declarations(registry):
        status = _node_status(declaration["node_type"], active_node_types, node_source)
        shared_nodes.append({**status, "methods": list(declaration["methods"])})

    required_tools = {
        "ffmpeg": _binary_status("ffmpeg", binary_finder),
        "ffprobe": _binary_status("ffprobe", binary_finder),
    }
    required_ready = all(
        method_report["ready"] for method_report in methods.values()
    ) and all(item["status"] == STATUS_AVAILABLE for item in required_tools.values())
    optional = {
        "rtx_vsr": _optional_node_status("rtx_vsr", active_node_types, node_source),
        "seedvr2": _optional_node_status("seedvr2", active_node_types, node_source),
    }
    return {
        "response_version": 1,
        "source": "backend.workflows.production_manifest_registry",
        "methods": methods,
        "shared": {"nodes": shared_nodes, "models": shared_models},
        "required_tools": required_tools,
        "required_ready": required_ready,
        "optional": optional,
    }


def build_requirements_report(
    *,
    force_refresh: bool = False,
    **kwargs: object,
) -> dict[str, object]:
    """Return the authoritative report, caching only stable runtime discovery.

    Injected scanner arguments remain an uncached direct scan for compatibility
    with the existing deterministic unit-test and diagnostic hooks.
    """

    if kwargs:
        return scan_requirements(**kwargs)
    return get_requirements_snapshot(force_refresh=force_refresh).report_copy()
