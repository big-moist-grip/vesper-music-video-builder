"""Deterministic storyboard request, response, and integrity helpers."""

from __future__ import annotations

import hashlib
import json
import re

from .projects import ProjectValidationError, validate_entity_id, validate_entity_text


STORYBOARD_REQUEST_VERSION = 1
STORYBOARD_RESPONSE_VERSION = 1
STORYBOARD_MODES = ("loose", "strict", "band_performance")
DEFAULT_STORYBOARD_MODE = "loose"
STORY_DIRECTION_FIELDS = ("storyboard_mode", "story_brief", "visual_notes")
PROVISIONAL_STORY_DIRECTION_FIELDS = ("story_brief", "visual_notes")
STORYBOARD_FIELDS = ("request_fingerprint", "scenes")
STORYBOARD_SCENE_FIELDS = (
    "scene_id",
    "scene_type",
    "character_ids",
    "location_id",
    "action",
    "visual_instructions",
    "camera_direction",
    "motion_direction",
    "continuity_notes",
    "required_references",
)
RESPONSE_FIELDS = (
    "storyboard_response_version",
    "project_id",
    "request_fingerprint",
    "scenes",
)
REFERENCE_SELECTOR_FIELDS = ("entity_type", "entity_id", "reference_id")
SCENE_TYPES = frozenset({"performance", "narrative", "hybrid", "atmospheric", "transition"})
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STORY_DIRECTION_MAX_LENGTH = 8_000
STORYBOARD_TEXT_MAX_LENGTH = 8_000


STORYBOARD_MODE_GUIDANCE = {
    "loose": "Interpret the song freely; lyrics guide emotion and structure rather than dictating each shot.",
    "strict": "Keep the visuals closely aligned to the lyrical content and sequence.",
    "band_performance": "Build a performance-only video with no narrative storyline.",
}


def empty_story_direction() -> dict[str, str]:
    return {
        "storyboard_mode": DEFAULT_STORYBOARD_MODE,
        "story_brief": "",
        "visual_notes": "",
    }


def empty_storyboard() -> dict[str, object]:
    return {"request_fingerprint": None, "scenes": []}


def _require_exact_fields(value: object, fields: tuple[str, ...], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ProjectValidationError(f"{label} has unsupported or missing fields.")
    return value


def validate_story_direction(value: object) -> dict[str, str]:
    if isinstance(value, dict) and set(value) == set(PROVISIONAL_STORY_DIRECTION_FIELDS):
        document = {
            "storyboard_mode": DEFAULT_STORYBOARD_MODE,
            **value,
        }
    else:
        document = _require_exact_fields(value, STORY_DIRECTION_FIELDS, "Story direction")

    storyboard_mode = document.get("storyboard_mode")
    if storyboard_mode not in STORYBOARD_MODES:
        raise ProjectValidationError("Storyboard mode is invalid.")
    return {
        "storyboard_mode": storyboard_mode,
        "story_brief": validate_entity_text(
            document.get("story_brief"),
            "Story brief",
            STORY_DIRECTION_MAX_LENGTH,
        ),
        "visual_notes": validate_entity_text(
            document.get("visual_notes"),
            "Visual notes",
            STORY_DIRECTION_MAX_LENGTH,
        ),
    }


def _validate_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise ProjectValidationError("Storyboard request fingerprint is malformed.")
    return value


def _validate_optional_storyboard_text(value: object, label: str) -> str:
    return validate_entity_text(value, label, STORYBOARD_TEXT_MAX_LENGTH)


def _reference_ids(entity: dict[str, object]) -> set[str]:
    return {reference["reference_id"] for reference in entity["references"]}


def _validate_storyboard_scene(
    value: object,
    scene_ids: set[str],
    characters_by_id: dict[str, dict[str, object]],
    locations_by_id: dict[str, dict[str, object]],
    required_scene_type: str | None = None,
) -> dict[str, object]:
    document = _require_exact_fields(value, STORYBOARD_SCENE_FIELDS, "Storyboard scene")
    scene_id = validate_entity_id(document.get("scene_id"), "Storyboard scene ID")
    if scene_id not in scene_ids:
        raise ProjectValidationError("Storyboard scene ID does not identify a current scene.")

    scene_type = document.get("scene_type")
    if scene_type not in SCENE_TYPES:
        raise ProjectValidationError("Storyboard scene_type is invalid.")
    if required_scene_type is not None and scene_type != required_scene_type:
        raise ProjectValidationError("Band Performance storyboards require performance scenes.")

    character_ids = document.get("character_ids")
    if not isinstance(character_ids, list):
        raise ProjectValidationError("Storyboard character_ids must be an array.")
    normalized_character_ids: list[str] = []
    seen_character_ids: set[str] = set()
    for character_id in character_ids:
        normalized_id = validate_entity_id(character_id, "Storyboard character ID")
        if normalized_id in seen_character_ids:
            raise ProjectValidationError("Storyboard character IDs must be unique.")
        if normalized_id not in characters_by_id:
            raise ProjectValidationError("Storyboard references an unknown character.")
        seen_character_ids.add(normalized_id)
        normalized_character_ids.append(normalized_id)

    location_id = document.get("location_id")
    normalized_location_id: str | None
    if location_id is None:
        normalized_location_id = None
    else:
        normalized_location_id = validate_entity_id(location_id, "Storyboard location ID")
        if normalized_location_id not in locations_by_id:
            raise ProjectValidationError("Storyboard references an unknown location.")

    action = _validate_optional_storyboard_text(document.get("action"), "Storyboard action")
    if not action.strip():
        raise ProjectValidationError("Storyboard action cannot be empty.")
    visual_instructions = _validate_optional_storyboard_text(
        document.get("visual_instructions"),
        "Storyboard visual_instructions",
    )
    if not visual_instructions.strip():
        raise ProjectValidationError("Storyboard visual_instructions cannot be empty.")
    camera_direction = _validate_optional_storyboard_text(
        document.get("camera_direction"),
        "Storyboard camera_direction",
    )
    motion_direction = _validate_optional_storyboard_text(
        document.get("motion_direction"),
        "Storyboard motion_direction",
    )
    continuity_notes = _validate_optional_storyboard_text(
        document.get("continuity_notes"),
        "Storyboard continuity_notes",
    )

    required_references = document.get("required_references")
    if not isinstance(required_references, list):
        raise ProjectValidationError("Storyboard required_references must be an array.")
    normalized_references: list[dict[str, str]] = []
    seen_references: set[tuple[str, str, str]] = set()
    for reference in required_references:
        selector = _require_exact_fields(reference, REFERENCE_SELECTOR_FIELDS, "Storyboard reference selector")
        entity_type = selector.get("entity_type")
        if entity_type not in {"character", "location"}:
            raise ProjectValidationError("Storyboard reference entity_type is invalid.")
        entity_id = validate_entity_id(selector.get("entity_id"), "Storyboard reference entity ID")
        reference_id = validate_entity_id(selector.get("reference_id"), "Storyboard reference ID")
        selector_key = (entity_type, entity_id, reference_id)
        if selector_key in seen_references:
            raise ProjectValidationError("Storyboard reference selectors must be unique.")
        seen_references.add(selector_key)

        if entity_type == "character":
            character = characters_by_id.get(entity_id)
            if character is None:
                raise ProjectValidationError("Storyboard reference identifies an unknown character.")
            if entity_id not in seen_character_ids:
                raise ProjectValidationError("Storyboard character references require that character in the scene.")
            if reference_id not in _reference_ids(character):
                raise ProjectValidationError("Storyboard character reference is not owned by that character.")
        else:
            location = locations_by_id.get(entity_id)
            if location is None:
                raise ProjectValidationError("Storyboard reference identifies an unknown location.")
            if normalized_location_id != entity_id:
                raise ProjectValidationError("Storyboard location references require that location in the scene.")
            if reference_id not in _reference_ids(location):
                raise ProjectValidationError("Storyboard location reference is not owned by that location.")

        normalized_references.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "reference_id": reference_id,
            }
        )

    return {
        "scene_id": scene_id,
        "scene_type": scene_type,
        "character_ids": normalized_character_ids,
        "location_id": normalized_location_id,
        "action": action,
        "visual_instructions": visual_instructions,
        "camera_direction": camera_direction,
        "motion_direction": motion_direction,
        "continuity_notes": continuity_notes,
        "required_references": normalized_references,
    }


def validate_applied_storyboard(
    value: object,
    scenes: list[dict[str, object]],
    characters: list[dict[str, object]],
    locations: list[dict[str, object]],
    required_scene_type: str | None = None,
) -> dict[str, object]:
    document = _require_exact_fields(value, STORYBOARD_FIELDS, "Storyboard")
    storyboard_scenes = document.get("scenes")
    if not isinstance(storyboard_scenes, list):
        raise ProjectValidationError("Storyboard scenes must be an array.")
    fingerprint = document.get("request_fingerprint")
    if not storyboard_scenes:
        if fingerprint is not None:
            raise ProjectValidationError("An empty storyboard cannot have a request fingerprint.")
        return empty_storyboard()

    normalized_fingerprint = _validate_fingerprint(fingerprint)
    if not scenes:
        raise ProjectValidationError("A storyboard cannot be applied before scenes exist.")
    if len(storyboard_scenes) != len(scenes):
        raise ProjectValidationError("Storyboard must contain exactly one entry for every current scene.")

    scene_ids = {scene["scene_id"] for scene in scenes}
    characters_by_id = {character["character_id"]: character for character in characters}
    locations_by_id = {location["location_id"]: location for location in locations}
    normalized_scenes: list[dict[str, object]] = []
    seen_scene_ids: set[str] = set()
    for position, storyboard_scene in enumerate(storyboard_scenes):
        normalized = _validate_storyboard_scene(
            storyboard_scene,
            scene_ids,
            characters_by_id,
            locations_by_id,
            required_scene_type,
        )
        scene_id = normalized["scene_id"]
        if scene_id in seen_scene_ids:
            raise ProjectValidationError("Storyboard scene IDs must be unique.")
        expected_scene_id = scenes[position]["scene_id"]
        if scene_id != expected_scene_id:
            raise ProjectValidationError("Storyboard scene order must match the current scene order.")
        seen_scene_ids.add(scene_id)
        normalized_scenes.append(normalized)

    return {
        "request_fingerprint": normalized_fingerprint,
        "scenes": normalized_scenes,
    }


def _request_character(character: dict[str, object]) -> dict[str, object]:
    return {
        "character_id": character["character_id"],
        "name": character["name"],
        "role": character["role"],
        "appearance": character["appearance"],
        "outfit": character["outfit"],
        "references": [
            {
                "reference_id": reference["reference_id"],
                "original_name": reference["original_name"],
            }
            for reference in character["references"]
        ],
    }


def _request_location(location: dict[str, object]) -> dict[str, object]:
    return {
        "location_id": location["location_id"],
        "name": location["name"],
        "description": location["description"],
        "references": [
            {
                "reference_id": reference["reference_id"],
                "original_name": reference["original_name"],
            }
            for reference in location["references"]
        ],
    }


def _request_scene(scene: dict[str, object], sequence: int) -> dict[str, object]:
    return {
        "scene_id": scene["scene_id"],
        "sequence": sequence,
        "timeline_start_ms": scene["timeline_start_ms"],
        "timeline_end_ms": scene["timeline_end_ms"],
        "exact_duration_ms": scene["exact_duration_ms"],
        "source_kind": scene["source_kind"],
        "lyric": scene["lyric"],
        "source_cue_numbers": list(scene["source_cue_numbers"]),
        "split_index": scene["split_index"],
        "split_count": scene["split_count"],
    }


def _request_basis(project: dict[str, object]) -> dict[str, object]:
    story_direction = validate_story_direction(project["story_direction"])
    return {
        "storyboard_request_version": STORYBOARD_REQUEST_VERSION,
        "project_id": project["project_id"],
        "story_direction": {
            "storyboard_mode": story_direction["storyboard_mode"],
            "story_brief": story_direction["story_brief"],
            "visual_notes": story_direction["visual_notes"],
        },
        "characters": [_request_character(character) for character in project["characters"]],
        "locations": [_request_location(location) for location in project["locations"]],
        "scenes": [_request_scene(scene, index + 1) for index, scene in enumerate(project["scenes"])],
    }


def canonical_request_json(basis: dict[str, object]) -> str:
    return json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_fingerprint(project: dict[str, object]) -> str:
    basis = _request_basis(project)
    return hashlib.sha256(canonical_request_json(basis).encode("utf-8")).hexdigest()


def build_storyboard_request(project: dict[str, object]) -> dict[str, object]:
    if not project["scenes"]:
        raise ProjectValidationError("Build scenes before generating a storyboard request.")

    basis = _request_basis(project)
    fingerprint = hashlib.sha256(canonical_request_json(basis).encode("utf-8")).hexdigest()
    return {
        **basis,
        "request_fingerprint": fingerprint,
        "response_contract": {
            "response_version": STORYBOARD_RESPONSE_VERSION,
            "top_level_fields": list(RESPONSE_FIELDS),
            "scene_fields": list(STORYBOARD_SCENE_FIELDS),
            "scene_type_values": ["performance", "narrative", "hybrid", "atmospheric", "transition"],
            "reference_selector_fields": list(REFERENCE_SELECTOR_FIELDS),
            "storyboard_mode": basis["story_direction"]["storyboard_mode"],
            "mode_guidance": STORYBOARD_MODE_GUIDANCE[basis["story_direction"]["storyboard_mode"]],
            "rules": [
                "Return JSON only.",
                "Return exactly one allocation for every supplied scene in supplied order.",
                "Preserve every supplied scene_id exactly.",
                "Use only supplied character, location, and reference IDs.",
                "Keep creative instructions generation-method neutral; this relay does not select a future visual method.",
                f"Follow the {basis['story_direction']['storyboard_mode']} storyboard mode guidance.",
            ],
        },
    }


def validate_storyboard_response(
    project: dict[str, object],
    response: object,
) -> dict[str, object]:
    document = _require_exact_fields(response, RESPONSE_FIELDS, "Storyboard response")
    version = document.get("storyboard_response_version")
    if isinstance(version, bool) or version != STORYBOARD_RESPONSE_VERSION:
        raise ProjectValidationError("Unsupported storyboard response version.")
    project_id = validate_entity_id(document.get("project_id"), "Storyboard response project ID")
    if project_id != project["project_id"]:
        raise ProjectValidationError("Storyboard response project ID does not match the current project.")

    fingerprint = _validate_fingerprint(document.get("request_fingerprint"))
    current_fingerprint = request_fingerprint(project)
    if fingerprint != current_fingerprint:
        raise ProjectValidationError("Storyboard response is out of date. Generate a new request and try again.")

    story_direction = validate_story_direction(project["story_direction"])
    required_scene_type = "performance" if story_direction["storyboard_mode"] == "band_performance" else None
    return validate_applied_storyboard(
        {
            "request_fingerprint": fingerprint,
            "scenes": document.get("scenes"),
        },
        project["scenes"],
        project["characters"],
        project["locations"],
        required_scene_type,
    )


def storyboard_uses_character(storyboard: object, character_id: str) -> bool:
    if not isinstance(storyboard, dict) or not isinstance(storyboard.get("scenes"), list):
        return False
    return any(character_id in scene.get("character_ids", []) for scene in storyboard["scenes"])


def storyboard_uses_location(storyboard: object, location_id: str) -> bool:
    if not isinstance(storyboard, dict) or not isinstance(storyboard.get("scenes"), list):
        return False
    return any(scene.get("location_id") == location_id for scene in storyboard["scenes"])


def storyboard_uses_reference(storyboard: object, entity_type: str, entity_id: str, reference_id: str) -> bool:
    if not isinstance(storyboard, dict) or not isinstance(storyboard.get("scenes"), list):
        return False
    return any(
        selector.get("entity_type") == entity_type
        and selector.get("entity_id") == entity_id
        and selector.get("reference_id") == reference_id
        for scene in storyboard["scenes"]
        for selector in scene.get("required_references", [])
    )
