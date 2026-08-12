"""Visuals-state validation, deterministic helpers, and project-local keyframes."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    validate_entity_id,
    validate_entity_text,
    validate_original_name,
)
from .storyboard import request_fingerprint


LOGGER = logging.getLogger(__name__)

VISUALS_FIELDS = ("scenes",)
VISUAL_SCENE_FIELDS = (
    "scene_id",
    "generation_method",
    "keyframe_i2v",
    "reference2video",
)
KEYFRAME_FIELDS = (
    "keyframe_generation_prompt",
    "intended_keyframe_description",
    "accepted_keyframe",
    "actual_keyframe_description",
)
KEYFRAME_DETAIL_FIELDS = (
    "keyframe_generation_prompt",
    "intended_keyframe_description",
    "actual_keyframe_description",
)
REFERENCE2VIDEO_FIELDS = ("selected_references",)
ACCEPTED_KEYFRAME_FIELDS = ("asset_id", "stored_name", "original_name")
REFERENCE_SELECTOR_FIELDS = ("entity_type", "entity_id", "reference_id")
GENERATION_METHODS = ("keyframe_i2v", "reference2video")
VISUAL_TEXT_MAX_LENGTH = 8_000
KEYFRAME_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
}
KEYFRAME_EXTENSIONS = frozenset(KEYFRAME_FORMATS.values())
MAX_KEYFRAME_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
_SAFE_UPLOAD_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]+$")


class VisualSceneNotFoundError(ProjectNotFoundError):
    """The requested render scene has no current Visuals entry."""


class KeyframeNotFoundError(ProjectNotFoundError):
    """The requested scene has no accepted keyframe asset."""


def _require_exact_fields(value: object, fields: tuple[str, ...], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ProjectValidationError(f"{label} has unsupported or missing fields.")
    return value


def _empty_keyframe() -> dict[str, object]:
    return {
        "keyframe_generation_prompt": "",
        "intended_keyframe_description": "",
        "accepted_keyframe": None,
        "actual_keyframe_description": "",
    }


def _empty_reference2video() -> dict[str, object]:
    return {"selected_references": []}


def default_visual_entry(scene_id: object) -> dict[str, object]:
    canonical_scene_id = validate_entity_id(scene_id, "Visuals scene ID")
    return {
        "scene_id": canonical_scene_id,
        "generation_method": "keyframe_i2v",
        "keyframe_i2v": _empty_keyframe(),
        "reference2video": _empty_reference2video(),
    }


def default_visuals_for_scenes(scenes: list[dict[str, object]]) -> dict[str, object]:
    return {"scenes": [default_visual_entry(scene["scene_id"]) for scene in scenes]}


def _validate_visual_text(value: object, label: str) -> str:
    return validate_entity_text(value, label, VISUAL_TEXT_MAX_LENGTH)


def _validate_accepted_keyframe(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    document = _require_exact_fields(value, ACCEPTED_KEYFRAME_FIELDS, "Accepted keyframe")
    asset_id = validate_entity_id(document.get("asset_id"), "Accepted keyframe asset ID")
    stored_name = document.get("stored_name")
    if not isinstance(stored_name, str) or "/" in stored_name or "\\" in stored_name:
        raise ProjectValidationError("Accepted keyframe stored filename must be local.")
    extension = Path(stored_name).suffix.lower()
    if extension not in KEYFRAME_EXTENSIONS or stored_name != f"{asset_id}{extension}":
        raise ProjectValidationError("Accepted keyframe stored filename is invalid.")
    original_name = validate_original_name(document.get("original_name"))
    return {
        "asset_id": asset_id,
        "stored_name": stored_name,
        "original_name": original_name,
    }


def _entity_reference(entity: dict[str, object], reference_id: str) -> dict[str, object] | None:
    for reference in entity["references"]:
        if reference["reference_id"] == reference_id:
            return reference
    return None


def _validate_selected_references(
    value: object,
    characters: list[dict[str, object]],
    locations: list[dict[str, object]],
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ProjectValidationError("Visuals selected_references must be an array.")

    characters_by_id = {character["character_id"]: character for character in characters}
    locations_by_id = {location["location_id"]: location for location in locations}
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for selected in value:
        document = _require_exact_fields(selected, REFERENCE_SELECTOR_FIELDS, "Visuals reference selector")
        entity_type = document.get("entity_type")
        if entity_type not in {"character", "location"}:
            raise ProjectValidationError("Visuals reference entity_type is invalid.")
        entity_id = validate_entity_id(document.get("entity_id"), "Visuals reference entity ID")
        reference_id = validate_entity_id(document.get("reference_id"), "Visuals reference ID")
        selector_key = (entity_type, entity_id, reference_id)
        if selector_key in seen:
            raise ProjectValidationError("Visuals selected references must be unique.")
        seen.add(selector_key)

        entity = characters_by_id.get(entity_id) if entity_type == "character" else locations_by_id.get(entity_id)
        if entity is None:
            raise ProjectValidationError("Visuals reference identifies an unknown entity.")
        if _entity_reference(entity, reference_id) is None:
            raise ProjectValidationError("Visuals reference is not owned by the selected entity.")
        normalized.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "reference_id": reference_id,
            }
        )
    return normalized


def validate_visuals(
    value: object,
    scenes: list[dict[str, object]],
    characters: list[dict[str, object]],
    locations: list[dict[str, object]],
) -> dict[str, object]:
    document = _require_exact_fields(value, VISUALS_FIELDS, "Visuals")
    visual_scenes = document.get("scenes")
    if not isinstance(visual_scenes, list):
        raise ProjectValidationError("Visuals scenes must be an array.")
    if len(visual_scenes) != len(scenes):
        raise ProjectValidationError("Visuals must contain exactly one entry for every current scene.")

    normalized_scenes: list[dict[str, object]] = []
    seen_scene_ids: set[str] = set()
    for position, visual_scene in enumerate(visual_scenes):
        scene_document = _require_exact_fields(visual_scene, VISUAL_SCENE_FIELDS, "Visuals scene")
        scene_id = validate_entity_id(scene_document.get("scene_id"), "Visuals scene ID")
        if scene_id in seen_scene_ids:
            raise ProjectValidationError("Visuals scene IDs must be unique.")
        if position >= len(scenes) or scene_id != scenes[position]["scene_id"]:
            raise ProjectValidationError("Visuals scene order must match the current scene order.")
        seen_scene_ids.add(scene_id)

        generation_method = scene_document.get("generation_method")
        if generation_method not in GENERATION_METHODS:
            raise ProjectValidationError("Visuals generation_method is invalid.")

        keyframe_document = _require_exact_fields(scene_document.get("keyframe_i2v"), KEYFRAME_FIELDS, "Keyframe / Image-to-Video state")
        keyframe = {
            "keyframe_generation_prompt": _validate_visual_text(
                keyframe_document.get("keyframe_generation_prompt"),
                "Keyframe generation prompt",
            ),
            "intended_keyframe_description": _validate_visual_text(
                keyframe_document.get("intended_keyframe_description"),
                "Intended keyframe description",
            ),
            "accepted_keyframe": _validate_accepted_keyframe(keyframe_document.get("accepted_keyframe")),
            "actual_keyframe_description": _validate_visual_text(
                keyframe_document.get("actual_keyframe_description"),
                "Actual keyframe description",
            ),
        }

        reference_document = _require_exact_fields(
            scene_document.get("reference2video"),
            REFERENCE2VIDEO_FIELDS,
            "Reference-to-Video state",
        )
        reference2video = {
            "selected_references": _validate_selected_references(
                reference_document.get("selected_references"),
                characters,
                locations,
            )
        }
        normalized_scenes.append(
            {
                "scene_id": scene_id,
                "generation_method": generation_method,
                "keyframe_i2v": keyframe,
                "reference2video": reference2video,
            }
        )

    return {"scenes": normalized_scenes}


def _visual_entry(project: dict[str, object], scene_id: object) -> dict[str, object]:
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    for visual_scene in project["visuals"]["scenes"]:
        if visual_scene["scene_id"] == canonical_scene_id:
            return visual_scene
    raise VisualSceneNotFoundError("The requested scene was not found.")


def _replace_visual_entry(
    project: dict[str, object],
    scene_id: object,
    replacement: dict[str, object],
) -> dict[str, object]:
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    updated = []
    found = False
    for visual_scene in project["visuals"]["scenes"]:
        if visual_scene["scene_id"] == canonical_scene_id:
            updated.append(replacement)
            found = True
        else:
            updated.append(visual_scene)
    if not found:
        raise VisualSceneNotFoundError("The requested scene was not found.")
    return {**project, "visuals": {"scenes": updated}}


def _storyboard_scene(project: dict[str, object], scene_id: str) -> dict[str, object] | None:
    for storyboard_scene in project["storyboard"]["scenes"]:
        if storyboard_scene["scene_id"] == scene_id:
            return storyboard_scene
    return None


def _storyboard_dependency(project: dict[str, object]) -> tuple[str, list[str]]:
    if not project["storyboard"]["scenes"]:
        return "missing", ["Apply a storyboard before completing Visuals."]
    if project["storyboard"]["request_fingerprint"] != request_fingerprint(project):
        return "out_of_date", ["Storyboard is out of date."]
    return "current", []


def _selected_key(selector: dict[str, object]) -> tuple[str, str, str]:
    return selector["entity_type"], selector["entity_id"], selector["reference_id"]


def derive_visual_readiness(
    project: dict[str, object],
    scene_id: object,
    storage: ProjectStorage | None = None,
) -> dict[str, object]:
    visual_scene = _visual_entry(project, scene_id)
    scene = next(scene for scene in project["scenes"] if scene["scene_id"] == visual_scene["scene_id"])
    dependency_state, missing = _storyboard_dependency(project)
    storyboard_scene = _storyboard_scene(project, visual_scene["scene_id"])

    if dependency_state == "current" and storyboard_scene is None:
        missing = ["Current storyboard scene allocation is missing."]

    if visual_scene["generation_method"] == "keyframe_i2v":
        keyframe = visual_scene["keyframe_i2v"]
        if not keyframe["keyframe_generation_prompt"]:
            missing.append("Missing keyframe generation prompt")
        if not keyframe["intended_keyframe_description"]:
            missing.append("Missing intended keyframe description")
        accepted_keyframe = keyframe["accepted_keyframe"]
        if accepted_keyframe is None:
            missing.append("Missing accepted keyframe")
        elif storage is not None and not _keyframe_file_exists(storage, project["project_id"], visual_scene["scene_id"], accepted_keyframe):
            missing.append("Missing accepted keyframe")
        if not keyframe["actual_keyframe_description"]:
            missing.append("Missing actual image description")
    else:
        selected = visual_scene["reference2video"]["selected_references"]
        if not selected:
            missing.append("Select at least one still reference")
        if storyboard_scene is not None:
            assigned = {
                ("character", character_id, reference_id)
                for character_id in storyboard_scene["character_ids"]
                for character in project["characters"]
                if character["character_id"] == character_id
                for reference_id in {reference["reference_id"] for reference in character["references"]}
            }
            if storyboard_scene["location_id"] is not None:
                assigned_location = next(
                    location for location in project["locations"] if location["location_id"] == storyboard_scene["location_id"]
                )
                assigned.update(
                    ("location", assigned_location["location_id"], reference["reference_id"])
                    for reference in assigned_location["references"]
                )
            if any(_selected_key(selector) not in assigned for selector in selected):
                missing.append("A selected reference is not assigned to this scene")
            selected_keys = {_selected_key(selector) for selector in selected}
            for required in storyboard_scene["required_references"]:
                if _selected_key(required) not in selected_keys:
                    missing.append("A required storyboard reference is not selected")

    return {
        "scene_id": scene["scene_id"],
        "generation_method": visual_scene["generation_method"],
        "ready": not missing,
        "state": "ready" if not missing else "blocked",
        "missing": list(dict.fromkeys(missing)),
    }


def derive_visual_readiness_list(
    project: dict[str, object],
    storage: ProjectStorage | None = None,
) -> list[dict[str, object]]:
    return [derive_visual_readiness(project, scene["scene_id"], storage) for scene in project["scenes"]]


def _prompt_value(value: object, fallback: str = "Not specified") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def build_keyframe_prompt(project: dict[str, object], scene_id: object) -> str:
    visual_scene = _visual_entry(project, scene_id)
    canonical_scene_id = visual_scene["scene_id"]
    scene = next(scene for scene in project["scenes"] if scene["scene_id"] == canonical_scene_id)
    storyboard_scene = _storyboard_scene(project, canonical_scene_id)
    characters_by_id = {character["character_id"]: character for character in project["characters"]}
    locations_by_id = {location["location_id"]: location for location in project["locations"]}
    assigned_characters = [characters_by_id[character_id] for character_id in (storyboard_scene or {}).get("character_ids", [])]
    location = locations_by_id.get((storyboard_scene or {}).get("location_id"))
    required_reference_names: list[str] = []
    for selector in (storyboard_scene or {}).get("required_references", []):
        entity = characters_by_id.get(selector["entity_id"]) or locations_by_id.get(selector["entity_id"])
        if entity is not None:
            reference = _entity_reference(entity, selector["reference_id"])
            if reference is not None:
                required_reference_names.append(f"{entity['name']} — {reference['original_name']}")

    keyframe = visual_scene["keyframe_i2v"]
    intended_basis = keyframe["intended_keyframe_description"] or _prompt_value(
        (storyboard_scene or {}).get("visual_instructions") or (storyboard_scene or {}).get("action"),
        "Construct a coherent visual interpretation of the scene.",
    )
    lines = [
        "Create one cinematic still image for a music-video scene.",
        f"Scene {project['scenes'].index(scene) + 1} timeline: {scene['timeline_start_ms']}–{scene['timeline_end_ms']} ms ({scene['exact_duration_ms']} ms).",
        f"Source context: {'Lyric' if scene['source_kind'] == 'lyric' else 'Instrumental'}.",
        f"Lyric material: {_prompt_value(scene['lyric'], 'Instrumental passage')}",
        f"Scene type: {_prompt_value((storyboard_scene or {}).get('scene_type'))}",
        "Characters: " + ", ".join(character["name"] for character in assigned_characters) if assigned_characters else "Characters: None assigned",
        "Character continuity: " + "; ".join(
            f"{character['name']} — appearance: {_prompt_value(character['appearance'])}; outfit: {_prompt_value(character['outfit'])}"
            for character in assigned_characters
        ) if assigned_characters else "Character continuity: None assigned",
        f"Location: {_prompt_value(location['name'])}" if location else "Location: None assigned",
        f"Location description: {_prompt_value(location['description'])}" if location else "Location description: None assigned",
        f"Action: {_prompt_value((storyboard_scene or {}).get('action'))}",
        f"Visual instructions: {_prompt_value((storyboard_scene or {}).get('visual_instructions'))}",
        f"Camera direction: {_prompt_value((storyboard_scene or {}).get('camera_direction'))}",
        f"Motion direction: {_prompt_value((storyboard_scene or {}).get('motion_direction'))}",
        f"Continuity notes: {_prompt_value((storyboard_scene or {}).get('continuity_notes'))}",
        "Reference guidance: " + ", ".join(required_reference_names) if required_reference_names else "Reference guidance: No required still references.",
        f"Intended visual basis: {intended_basis}",
        "Keep the frame visually coherent, physically plausible, and consistent with the supplied character and location continuity. Do not add unrelated narrative elements.",
    ]
    return "\n".join(lines)


def planned_reference_mapping(project: dict[str, object], scene_id: object) -> list[dict[str, object]]:
    visual_scene = _visual_entry(project, scene_id)
    characters_by_id = {character["character_id"]: character for character in project["characters"]}
    locations_by_id = {location["location_id"]: location for location in project["locations"]}
    subject_numbers: dict[tuple[str, str], int] = {}
    mapping: list[dict[str, object]] = []
    for position, selector in enumerate(visual_scene["reference2video"]["selected_references"], start=1):
        entity = characters_by_id.get(selector["entity_id"]) if selector["entity_type"] == "character" else locations_by_id.get(selector["entity_id"])
        if entity is None:
            continue
        reference = _entity_reference(entity, selector["reference_id"])
        if reference is None:
            continue
        owner_key = (selector["entity_type"], selector["entity_id"])
        if owner_key not in subject_numbers:
            subject_numbers[owner_key] = len(subject_numbers) + 1
        subject_tag = f"<Subject {subject_numbers[owner_key]}>"
        mapping.append(
            {
                "picture_number": position,
                "picture_tag": f"<Picture {position}>",
                "entity_type": selector["entity_type"],
                "entity_id": selector["entity_id"],
                "entity_name": entity["name"],
                "reference_id": selector["reference_id"],
                "original_name": reference["original_name"],
                "subject_tag": subject_tag,
            }
        )
    return mapping


def set_all_generation_method(
    storage: ProjectStorage,
    project_id: object,
    generation_method: object,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    if generation_method not in GENERATION_METHODS:
        raise ProjectValidationError("Visuals generation_method is invalid.")
    updated_visuals = {
        "scenes": [
            {**visual_scene, "generation_method": generation_method}
            for visual_scene in project["visuals"]["scenes"]
        ]
    }
    if updated_visuals == project["visuals"]:
        return project
    return storage.save_project(
        project["project_id"],
        {**project, "visuals": updated_visuals},
        allow_visuals_change=True,
    )


def visuals_uses_reference(
    visuals: object,
    entity_type: str,
    entity_id: str,
    reference_id: str | None = None,
) -> bool:
    if not isinstance(visuals, dict) or not isinstance(visuals.get("scenes"), list):
        return False
    return any(
        selector["entity_type"] == entity_type
        and selector["entity_id"] == entity_id
        and (reference_id is None or selector["reference_id"] == reference_id)
        for visual_scene in visuals["scenes"]
        for selector in visual_scene.get("reference2video", {}).get("selected_references", [])
    )


def visuals_uses_entity(visuals: object, entity_type: str, entity_id: str) -> bool:
    return visuals_uses_reference(visuals, entity_type, entity_id)


def set_generation_method(storage: ProjectStorage, project_id: object, scene_id: object, generation_method: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    if generation_method not in GENERATION_METHODS:
        raise ProjectValidationError("Visuals generation_method is invalid.")
    current = _visual_entry(project, scene_id)
    updated = {**current, "generation_method": generation_method}
    return storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
    )


def save_keyframe_details(storage: ProjectStorage, project_id: object, scene_id: object, payload: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    document = _require_exact_fields(payload, KEYFRAME_DETAIL_FIELDS, "Keyframe details")
    details = {
        key: _validate_visual_text(document.get(key), label)
        for key, label in (
            ("keyframe_generation_prompt", "Keyframe generation prompt"),
            ("intended_keyframe_description", "Intended keyframe description"),
            ("actual_keyframe_description", "Actual keyframe description"),
        )
    }
    current = _visual_entry(project, scene_id)
    updated = {**current, "keyframe_i2v": {**current["keyframe_i2v"], **details}}
    return storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
    )


def generate_and_save_keyframe_prompt(storage: ProjectStorage, project_id: object, scene_id: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    current = _visual_entry(project, scene_id)
    prompt = build_keyframe_prompt(project, current["scene_id"])
    updated = {**current, "keyframe_i2v": {**current["keyframe_i2v"], "keyframe_generation_prompt": prompt}}
    return storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
    )


def save_reference_selection(storage: ProjectStorage, project_id: object, scene_id: object, payload: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    document = _require_exact_fields(payload, REFERENCE2VIDEO_FIELDS, "Reference-to-Video state")
    selected = _validate_selected_references(
        document.get("selected_references"),
        project["characters"],
        project["locations"],
    )
    current = _visual_entry(project, scene_id)
    updated = {**current, "reference2video": {"selected_references": selected}}
    return storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
    )


def _safe_upload_name(filename: object) -> str:
    if not isinstance(filename, str):
        raise ProjectValidationError("Accepted keyframe must include a filename.")
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not basename or basename in {".", ".."} or not _SAFE_UPLOAD_NAME_PATTERN.fullmatch(basename):
        raise ProjectValidationError("Accepted keyframe filename is invalid.")
    return validate_original_name(basename)


def _keyframe_directory(storage: ProjectStorage, project_id: object, scene_id: object, create: bool = False) -> Path:
    project_directory = storage.project_directory(project_id)
    canonical_scene_id = validate_entity_id(scene_id, "Scene ID")
    project_root = project_directory.resolve(strict=True)
    keyframe_root = project_directory / "keyframes"
    if keyframe_root.is_symlink():
        raise ProjectValidationError("Keyframe storage is unsafe.")
    if create:
        keyframe_root.mkdir(parents=True, exist_ok=True)
    scene_directory = keyframe_root / canonical_scene_id
    if scene_directory.is_symlink():
        raise ProjectValidationError("Keyframe scene storage is unsafe.")
    if create:
        scene_directory.mkdir(parents=True, exist_ok=True)
    resolved_scene = scene_directory.resolve(strict=False)
    if resolved_scene != project_root and project_root not in resolved_scene.parents:
        raise ProjectValidationError("Keyframe storage is outside the project.")
    return scene_directory


def _keyframe_path(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    metadata: dict[str, object],
) -> Path:
    directory = _keyframe_directory(storage, project_id, scene_id)
    path = directory / metadata["stored_name"]
    resolved = path.resolve(strict=False)
    project_root = storage.project_directory(project_id).resolve(strict=True)
    if resolved != project_root and project_root not in resolved.parents:
        raise ProjectValidationError("Accepted keyframe path is outside the project.")
    return path


def _keyframe_file_exists(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    metadata: dict[str, object],
) -> bool:
    try:
        path = _keyframe_path(storage, project_id, scene_id, metadata)
    except (ProjectNotFoundError, ProjectValidationError, OSError):
        return False
    return path.is_file() and not path.is_symlink()


async def _write_keyframe_upload(upload, destination_directory: Path) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_directory,
            prefix=".keyframe-upload-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            total_bytes = 0
            while True:
                chunk = await upload.read_chunk(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ProjectValidationError("Accepted keyframe data is invalid.")
                total_bytes += len(chunk)
                if total_bytes > MAX_KEYFRAME_BYTES:
                    raise ProjectValidationError("Accepted keyframe is too large.")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total_bytes == 0:
            raise ProjectValidationError("Accepted keyframe is empty.")
        return temporary_path
    except Exception:
        if temporary_path is not None:
            _remove_file(temporary_path)
        raise


def _decode_keyframe_format(candidate_path: Path) -> str:
    try:
        with Image.open(candidate_path) as image:
            image_format = image.format
            image.verify()
        with Image.open(candidate_path) as image:
            image.load()
            if getattr(image, "n_frames", 1) != 1:
                raise ProjectValidationError("Accepted keyframe must be a still image.")
    except ProjectValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ProjectValidationError("Accepted keyframe image is invalid.") from error
    if image_format not in KEYFRAME_FORMATS:
        raise ProjectValidationError("Accepted keyframe must be PNG, JPEG, or WEBP.")
    return image_format


async def assign_keyframe(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    original_filename: object,
    upload,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    current = _visual_entry(project, scene_id)
    original_name = _safe_upload_name(original_filename)
    try:
        scene_directory = _keyframe_directory(storage, project["project_id"], current["scene_id"], create=True)
        candidate_path = await _write_keyframe_upload(upload, scene_directory)
    except ProjectValidationError:
        raise
    except OSError as error:
        raise ProjectPersistenceError("Accepted keyframe could not be written.") from error
    installed_path: Path | None = None
    old_metadata = current["keyframe_i2v"]["accepted_keyframe"]
    try:
        image_format = _decode_keyframe_format(candidate_path)
        asset_id = str(uuid.uuid4())
        stored_name = f"{asset_id}{KEYFRAME_FORMATS[image_format]}"
        installed_path = scene_directory / stored_name
        try:
            os.replace(candidate_path, installed_path)
        except OSError as error:
            raise ProjectPersistenceError("Accepted keyframe could not be installed safely.") from error
        candidate_entry = {
            **current,
            "keyframe_i2v": {
                **current["keyframe_i2v"],
                "accepted_keyframe": {
                    "asset_id": asset_id,
                    "stored_name": stored_name,
                    "original_name": original_name,
                },
            },
        }
        try:
            saved = storage.save_project(
                project["project_id"],
                _replace_visual_entry(project, current["scene_id"], candidate_entry),
                allow_visuals_change=True,
            )
        except (ProjectPersistenceError, ProjectValidationError):
            _remove_file(installed_path)
            installed_path = None
            raise

        if old_metadata is not None:
            try:
                _remove_file(_keyframe_path(storage, project["project_id"], current["scene_id"], old_metadata))
            except (ProjectValidationError, OSError) as error:
                LOGGER.warning("Could not clean replaced keyframe: %s", error)
        return saved
    finally:
        if candidate_path.exists():
            _remove_file(candidate_path)


def remove_keyframe(storage: ProjectStorage, project_id: object, scene_id: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    current = _visual_entry(project, scene_id)
    accepted = current["keyframe_i2v"]["accepted_keyframe"]
    if accepted is None:
        raise KeyframeNotFoundError("The scene has no accepted keyframe.")
    updated = {
        **current,
        "keyframe_i2v": {**current["keyframe_i2v"], "accepted_keyframe": None},
    }
    saved = storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
    )
    try:
        _remove_file(_keyframe_path(storage, project["project_id"], current["scene_id"], accepted))
    except (ProjectValidationError, OSError) as error:
        LOGGER.warning("Could not clean removed keyframe: %s", error)
    return saved


def get_keyframe_path(storage: ProjectStorage, project_id: object, scene_id: object) -> Path:
    project = storage.load_project(project_id)
    current = _visual_entry(project, scene_id)
    accepted = current["keyframe_i2v"]["accepted_keyframe"]
    if accepted is None:
        raise KeyframeNotFoundError("The scene has no accepted keyframe.")
    path = _keyframe_path(storage, project["project_id"], current["scene_id"], accepted)
    if path.is_symlink() or not path.is_file():
        raise KeyframeNotFoundError("Accepted keyframe file was not found.")
    return path


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        LOGGER.warning("Could not clean keyframe file %s: %s", path.name, error)
