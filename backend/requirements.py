"""Deterministic local dependency checks for the approved H3 manifests.

This module deliberately reads only the production manifest registry.  Donor
workflow files are not a source of runtime requirements for the Builder.
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .workflows import production_manifest_registry


STATUS_AVAILABLE = "AVAILABLE"
STATUS_MISSING = "MISSING"
STATUS_UNKNOWN = "UNKNOWN"
REQUIREMENT_STATUSES = (STATUS_AVAILABLE, STATUS_MISSING, STATUS_UNKNOWN)

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3:8b"
OLLAMA_TIMEOUT_SECONDS = 0.75

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


def _is_loopback_url(base_url: str) -> bool:
    try:
        parsed = urllib_parse.urlparse(base_url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme != "http" or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def probe_ollama(
    base_url: str = OLLAMA_BASE_URL,
    model: str = OLLAMA_MODEL,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = OLLAMA_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Probe only a loopback Ollama endpoint; never start or install anything."""

    base_url = base_url.rstrip("/")
    report = {"base_url": base_url, "model": model}
    if not _is_loopback_url(base_url):
        return _status_item(STATUS_UNKNOWN, **report, model_status=STATUS_UNKNOWN, detail="Ollama endpoint is not loopback-only.")
    if not isinstance(model, str) or not model:
        return _status_item(STATUS_UNKNOWN, **report, model_status=STATUS_UNKNOWN, detail="No local Ollama model is configured.")

    open_url = opener or urllib_request.urlopen
    request = urllib_request.Request(f"{base_url}/api/tags", headers={"Accept": "application/json"})
    try:
        response = open_url(request, timeout=timeout)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        document = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        models = document.get("models") if isinstance(document, dict) else None
        names = {
            item.get("name")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        } if isinstance(models, list) else set()
        model_status = STATUS_AVAILABLE if model in names else STATUS_MISSING
        return _status_item(STATUS_AVAILABLE, **report, model_status=model_status, detail=None)
    except (urllib_error.URLError, OSError, TimeoutError):
        return _status_item(STATUS_MISSING, **report, model_status=STATUS_MISSING, detail="Local Ollama is unavailable.")
    except (UnicodeError, TypeError, ValueError, KeyError):
        return _status_item(STATUS_UNKNOWN, **report, model_status=STATUS_UNKNOWN, detail="Local Ollama returned an invalid response.")


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
    ollama_probe: Callable[[], dict[str, object]] | None = None,
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
    if ollama_probe is None:
        optional_ollama = probe_ollama()
    else:
        optional_ollama = ollama_probe()
    optional = {
        "rtx_vsr": _optional_node_status("rtx_vsr", active_node_types, node_source),
        "seedvr2": _optional_node_status("seedvr2", active_node_types, node_source),
        "ollama": optional_ollama,
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


def build_requirements_report(**kwargs: object) -> dict[str, object]:
    """Named alias used by route callers and tests."""

    return scan_requirements(**kwargs)
