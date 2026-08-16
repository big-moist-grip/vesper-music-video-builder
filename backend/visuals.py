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
MAX_REF2VA_STILL_REFERENCES = 9
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
    *,
    allow_over_capacity: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ProjectValidationError("Visuals selected_references must be an array.")
    if len(value) > MAX_REF2VA_STILL_REFERENCES and not allow_over_capacity:
        raise ProjectValidationError(
            f"Visuals selected_references cannot exceed {MAX_REF2VA_STILL_REFERENCES} still references."
        )

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
    *,
    allow_over_capacity: bool = False,
    allow_over_capacity_scene_ids: set[str] | None = None,
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
                allow_over_capacity=(
                    allow_over_capacity
                    or (allow_over_capacity_scene_ids is not None and scene_id in allow_over_capacity_scene_ids)
                ),
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


def _reference_owner_key(selector: dict[str, object]) -> tuple[str, str]:
    return selector["entity_type"], selector["entity_id"]


def _entity_for_reference_owner(
    project: dict[str, object],
    owner_key: tuple[str, str],
) -> dict[str, object] | None:
    entity_type, entity_id = owner_key
    if entity_type == "character":
        collection = project.get("characters", [])
        id_field = "character_id"
    elif entity_type == "location":
        collection = project.get("locations", [])
        id_field = "location_id"
    else:
        return None
    return next(
        (entity for entity in collection if entity.get(id_field) == entity_id),
        None,
    )


def _reference_display_name(project: dict[str, object], selector: dict[str, object]) -> str:
    collections = {
        "character": project.get("characters", []),
        "location": project.get("locations", []),
    }
    entities = collections.get(selector.get("entity_type"), [])
    entity = next(
        (candidate for candidate in entities if candidate.get(f"{selector['entity_type']}_id") == selector.get("entity_id")),
        None,
    )
    if entity is not None:
        reference = _entity_reference(entity, str(selector.get("reference_id")))
        if reference is not None:
            return f"{entity['name']} · {reference['original_name']}"
        return f"{entity['name']} · {selector.get('reference_id', 'unknown')}"
    return f"{selector.get('entity_type', 'unknown')} · {selector.get('reference_id', 'unknown')}"


def _required_owner_order_for_scene(
    project: dict[str, object],
    scene_id: str,
) -> list[tuple[str, str]]:
    storyboard_scene = _storyboard_scene(project, scene_id)
    if storyboard_scene is None:
        return []
    owners: list[tuple[str, str]] = []
    seen_owners: set[tuple[str, str]] = set()
    for selector in storyboard_scene.get("required_references", []):
        owner_key = _reference_owner_key(selector)
        if owner_key in seen_owners:
            continue
        seen_owners.add(owner_key)
        owners.append(owner_key)
    return owners


def _required_owner_keys_for_scene(
    project: dict[str, object],
    scene_id: str,
) -> set[tuple[str, str]]:
    return set(_required_owner_order_for_scene(project, scene_id))


def _required_references_for_scene(
    project: dict[str, object],
    scene_id: str,
) -> list[dict[str, object]]:
    """Return every current reference owned by each required Storyboard owner.

    Storyboard ``required_references`` remains the compact owner/order marker.
    Requiredness is deliberately derived from that marker plus current entity
    metadata, so adding a second image to a required Character or Location does
    not require a second persisted required flag.
    """
    owners = _required_owner_order_for_scene(project, scene_id)

    required: list[dict[str, object]] = []
    for entity_type, entity_id in owners:
        entity = _entity_for_reference_owner(project, (entity_type, entity_id))
        if entity is None:
            continue
        for reference in entity.get("references", []):
            required.append(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "reference_id": reference["reference_id"],
                }
            )
    return required


def reconcile_scene_reference_selection(
    project: dict[str, object],
    scene_id: str,
    selected: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return the canonical selected-reference sequence for one scene.

    The applied Storyboard supplies required owner order. Current entity
    metadata supplies every image and its authoritative within-owner order.
    Existing selections from non-required owners are the only optional state,
    and their relative order is preserved after the complete required block.
    """
    if not isinstance(selected, list):
        return selected
    required = _required_references_for_scene(project, scene_id)
    required_keys = {_selected_key(selector) for selector in required}
    required_owner_keys = _required_owner_keys_for_scene(project, scene_id)
    extras: list[dict[str, str]] = []
    seen = set(required_keys)
    for selector in selected:
        if not isinstance(selector, dict):
            extras.append(selector)
            continue
        try:
            owner_key = _reference_owner_key(selector)
            key = _selected_key(selector)
        except (KeyError, TypeError):
            extras.append(selector)
            continue
        if owner_key in required_owner_keys:
            continue
        if key not in seen:
            extras.append(selector)
            seen.add(key)
    return [*required, *extras]


def reconcile_project_reference_selections(project: dict[str, object]) -> dict[str, object]:
    """Materialize canonical REF2VA selections for every current scene.

    This is the project-level lifecycle adapter around
    :func:`reconcile_scene_reference_selection`. It is intentionally pure: load,
    save, Storyboard Apply, resource mutation, and Visuals projection can all
    call it without creating a persistence or invalidation loop.
    """

    visuals = project.get("visuals")
    if not isinstance(visuals, dict) or not isinstance(visuals.get("scenes"), list):
        return project

    changed = False
    updated_scenes: list[dict[str, object]] = []
    for visual_scene in visuals["scenes"]:
        if not isinstance(visual_scene, dict):
            updated_scenes.append(visual_scene)
            continue
        reference2video = visual_scene.get("reference2video")
        selected = (
            reference2video.get("selected_references", [])
            if isinstance(reference2video, dict)
            else None
        )
        if not isinstance(selected, list):
            updated_scenes.append(visual_scene)
            continue

        reconciled = reconcile_scene_reference_selection(
            project,
            str(visual_scene.get("scene_id")),
            selected,
        )
        if reconciled == selected:
            updated_scenes.append(visual_scene)
            continue
        changed = True
        updated_scenes.append(
            {
                **visual_scene,
                "reference2video": {"selected_references": reconciled},
            }
        )

    if not changed:
        return project
    return {**project, "visuals": {**visuals, "scenes": updated_scenes}}


# Compatibility name retained for the existing Storyboard Apply route. The
# implementation is the same canonical project reconciliation path used by
# validation, load/materialization, and Visuals mutations.
synchronize_storyboard_required_references = reconcile_project_reference_selections


def _required_reference_file_is_resolvable(
    storage: ProjectStorage,
    project: dict[str, object],
    selector: dict[str, object],
) -> bool:
    from .entities import get_reference_path_for_project

    kind = "characters" if selector["entity_type"] == "character" else "locations"
    try:
        get_reference_path_for_project(
            storage,
            project,
            kind,
            selector["entity_id"],
            selector["reference_id"],
        )
    except (ProjectNotFoundError, ProjectValidationError, OSError, KeyError, StopIteration):
        return False
    return True


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
        required = _required_references_for_scene(project, visual_scene["scene_id"])
        if len(required) > MAX_REF2VA_STILL_REFERENCES:
            missing.append(
                "Storyboard-required references require "
                f"{len(required)} images, exceeding the Reference-to-Video maximum of "
                f"{MAX_REF2VA_STILL_REFERENCES}."
            )
        elif len(selected) > MAX_REF2VA_STILL_REFERENCES:
            missing.append(
                f"Reference-to-Video supports at most {MAX_REF2VA_STILL_REFERENCES} still references"
            )
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
            for required_selector in required:
                required_key = _selected_key(required_selector)
                if required_key not in selected_keys:
                    display_name = _reference_display_name(project, required_selector)
                    missing.append(
                        f"Required storyboard reference '{display_name}' is missing from synchronized Visuals selection."
                    )

            for marker in storyboard_scene["required_references"]:
                entity = _entity_for_reference_owner(project, _reference_owner_key(marker))
                if entity is None or _entity_reference(entity, marker["reference_id"]) is None:
                    display_name = _reference_display_name(project, marker)
                    missing.append(
                        f"Required storyboard reference '{display_name}' could not be resolved."
                    )
            if storage is not None:
                for required_selector in required:
                    display_name = _reference_display_name(project, required_selector)
                    if not _required_reference_file_is_resolvable(storage, project, required_selector):
                        missing.append(
                            f"Required storyboard reference '{display_name}' could not be resolved."
                        )

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
    for selector in _required_references_for_scene(project, canonical_scene_id):
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
    current = _visual_entry(project, scene_id)
    required = _required_references_for_scene(project, current["scene_id"])
    selected = _validate_selected_references(
        document.get("selected_references"),
        project["characters"],
        project["locations"],
        allow_over_capacity=len(required) > MAX_REF2VA_STILL_REFERENCES,
    )
    reconciled = reconcile_scene_reference_selection(project, current["scene_id"], selected)
    if len(reconciled) > MAX_REF2VA_STILL_REFERENCES and len(required) <= MAX_REF2VA_STILL_REFERENCES:
        raise ProjectValidationError(
            f"Visuals selected_references cannot exceed {MAX_REF2VA_STILL_REFERENCES} still references."
        )
    updated = {**current, "reference2video": {"selected_references": reconciled}}
    return storage.save_project(
        project["project_id"],
        _replace_visual_entry(project, current["scene_id"], updated),
        allow_visuals_change=True,
        allow_visual_over_capacity_scene_ids={current["scene_id"]}
        if len(reconciled) > MAX_REF2VA_STILL_REFERENCES
        else None,
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
    return get_keyframe_path_for_project(storage, project, scene_id)


def get_keyframe_path_for_project(
    storage: ProjectStorage,
    project: dict[str, object],
    scene_id: object,
) -> Path:
    """Resolve an accepted keyframe using an already loaded project."""

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
