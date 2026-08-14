"""Deterministic, project-local H3 prompt compilation for Phase 7.

The compiler is a narrow fact assembler.  It does not inspect donor workflow
text, invent narrative canon, execute H3, or mutate project state.
"""

from __future__ import annotations

import hashlib
import json
import re

from .projects import ProjectNotFoundError, ProjectStorage, ProjectValidationError, validate_entity_id
from .visuals import GENERATION_METHODS, MAX_REF2VA_STILL_REFERENCES, derive_visual_readiness


REF2VA_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
PROMPT_MAX_LENGTH = 50_000
ENHANCED_DESCRIPTION_MAX_LENGTH = 4_000
PROMPT_RELAY_REQUEST_VERSION = 1
PROMPT_RELAY_RESPONSE_VERSION = 1
PROMPT_STATUS_NEEDS_GPT = "needs_gpt"
PROMPT_STATUS_CURRENT = "current"
PROMPT_STATUS_STALE = "stale"
_TAG_PATTERN = re.compile(r"<(Picture|Subject|Audio|Video)\s+(\d+)>")
_PICTURE_TAG_PATTERN = re.compile(r"<Picture\b", re.IGNORECASE)
_SUBJECT_TAG_PATTERN = re.compile(r"<Subject\s+(\d+)>")
_SUBJECT_TAG_ANY_PATTERN = re.compile(r"<Subject\b[^>]*>", re.IGNORECASE)
_AUDIO_TAG_PATTERN = re.compile(r"<Audio\b", re.IGNORECASE)
_VIDEO_TAG_PATTERN = re.compile(r"<Video\b", re.IGNORECASE)
_ANY_REFERENCE_TAG_PATTERN = re.compile(r"<(?:Picture|Subject|Audio|Video|Location)\b", re.IGNORECASE)
_LOCATION_TAG_PATTERN = re.compile(r"<Location\b", re.IGNORECASE)
_SHOT_PATTERN = re.compile(r"\[(?:Shot|shot)\s+(\d+)\]")
_DIALOGUE_TAG_PATTERN = re.compile(r"<d\b", re.IGNORECASE)
_RETENTION_MARKER_PATTERN = re.compile(
    r"\b(?:fully|partially)_(?:preserved|copy)\b|\b(?:attribute_transfer|weak_reference)\b",
    re.IGNORECASE,
)
_DURATION_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:milliseconds?|ms|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_INSTRUMENTAL_TEXT_PATTERN = re.compile(r"[\[(]?\s*(?:instrumental|music)\s*[\])]?$", re.IGNORECASE)
_WHOLE_PROMPT_MARKERS = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "integrated_multimodal_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
    "deterministic h3 prompt",
    "scene_facts",
    "reference_map",
    "scene id:",
    "timeline:",
)


class PromptCompilationError(ProjectValidationError):
    """The current project cannot provide a safe deterministic scene prompt."""


class PromptConflictError(ProjectValidationError):
    """The prompt source changed after the client preview was produced."""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sentence_text(value: object) -> str:
    return _text(value).strip().rstrip(".!?;: ")


def _find_scene(project: dict[str, object], scene_id: object) -> dict[str, object]:
    canonical_id = validate_entity_id(scene_id, "Scene ID")
    for scene in project.get("scenes", []):
        if scene.get("scene_id") == canonical_id:
            return scene
    raise ProjectNotFoundError("The requested scene was not found.")


def _find_visual_scene(project: dict[str, object], scene_id: str) -> dict[str, object]:
    visuals = project.get("visuals")
    if isinstance(visuals, dict):
        for visual_scene in visuals.get("scenes", []):
            if visual_scene.get("scene_id") == scene_id:
                return visual_scene
    raise ProjectNotFoundError("The requested Visuals scene was not found.")


def _find_storyboard_scene(project: dict[str, object], scene_id: str) -> dict[str, object] | None:
    storyboard = project.get("storyboard")
    if not isinstance(storyboard, dict):
        return None
    for storyboard_scene in storyboard.get("scenes", []):
        if storyboard_scene.get("scene_id") == scene_id:
            return storyboard_scene
    return None


def _find_reference(entity: dict[str, object], reference_id: str) -> dict[str, object]:
    for reference in entity.get("references", []):
        if reference.get("reference_id") == reference_id:
            return reference
    raise PromptCompilationError("A selected reference is not owned by its entity.")


def _entity_indexes(
    project: dict[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    characters = {
        character["character_id"]: character
        for character in project.get("characters", [])
        if isinstance(character, dict) and isinstance(character.get("character_id"), str)
    }
    locations = {
        location["location_id"]: location
        for location in project.get("locations", [])
        if isinstance(location, dict) and isinstance(location.get("location_id"), str)
    }
    return characters, locations


def build_ref2va_mapping(project: dict[str, object], scene_id: object) -> dict[str, object]:
    """Build the ordered Picture/Subject map from the stored Ref2V selection."""

    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    selected = visual_scene["reference2video"]["selected_references"]
    if len(selected) > MAX_REF2VA_STILL_REFERENCES:
        raise PromptCompilationError("Reference-to-Video cannot use more than 9 still references.")
    characters, locations = _entity_indexes(project)
    subject_numbers: dict[tuple[str, str], int] = {}
    subjects: list[dict[str, object]] = []
    pictures: list[dict[str, object]] = []

    for picture_number, selector in enumerate(selected, start=1):
        entity_type = selector.get("entity_type")
        entity_id = selector.get("entity_id")
        reference_id = selector.get("reference_id")
        entity = characters.get(entity_id) if entity_type == "character" else locations.get(entity_id)
        if entity is None or entity_type not in {"character", "location"}:
            raise PromptCompilationError("A selected reference identifies an unknown entity.")
        reference = _find_reference(entity, reference_id)
        owner_key = (entity_type, entity_id)
        if owner_key not in subject_numbers:
            subject_number = len(subject_numbers) + 1
            subject_numbers[owner_key] = subject_number
            subjects.append(
                {
                    "subject_number": subject_number,
                    "subject_tag": f"<Subject {subject_number}>",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "entity_name": entity["name"],
                    "picture_numbers": [],
                }
            )
        subject_number = subject_numbers[owner_key]
        subject = subjects[subject_number - 1]
        subject["picture_numbers"].append(picture_number)
        pictures.append(
            {
                "picture_number": picture_number,
                "picture_tag": f"<Picture {picture_number}>",
                "subject_number": subject_number,
                "subject_tag": f"<Subject {subject_number}>",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_name": entity["name"],
                "reference_id": reference_id,
                "original_name": reference["original_name"],
            }
        )

    return {
        "pictures": pictures,
        "subjects": subjects,
        "audio": {
            "audio_number": 1,
            "audio_tag": "<Audio 1>",
            "role": "authoritative_scene_audio",
        },
    }


def planned_reference_mapping(project: dict[str, object], scene_id: object) -> dict[str, object]:
    """Public Phase 7 mapping entry point, kept separate from workflow patching."""

    return build_ref2va_mapping(project, scene_id)


def _scene_context(
    project: dict[str, object],
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    characters, locations = _entity_indexes(project)
    character_values: list[dict[str, object]] = []
    for character_id in (storyboard_scene or {}).get("character_ids", []):
        character = characters.get(character_id)
        if character is None:
            raise PromptCompilationError("Storyboard references an unknown Character.")
        character_values.append(character)
    location_id = (storyboard_scene or {}).get("location_id")
    location = locations.get(location_id) if location_id else None
    if location_id and location is None:
        raise PromptCompilationError("Storyboard references an unknown Location.")
    return character_values, location


def _usable_performance_lyric(
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
) -> str | None:
    lyric = _text(scene.get("lyric")).strip()
    if not lyric or _text((storyboard_scene or {}).get("scene_type")) != "performance":
        return None
    if _text(scene.get("source_kind")).lower() != "lyric":
        return None
    if re.fullmatch(r"[\[(]?\s*(?:instrumental|music)\s*[\])]?", lyric, re.IGNORECASE):
        return None
    return lyric


def _is_instrumental_scene(scene: dict[str, object]) -> bool:
    if _text(scene.get("source_kind")).lower() == "instrumental":
        return True
    lyric = _text(scene.get("lyric")).strip()
    return bool(lyric and _INSTRUMENTAL_TEXT_PATTERN.fullmatch(lyric))


def _prompt_source_basis(
    project: dict[str, object],
    scene_id: object,
    generation_method: str | None = None,
) -> dict[str, object]:
    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    method = generation_method or visual_scene.get("generation_method")
    if method not in GENERATION_METHODS:
        raise PromptCompilationError("Visuals generation_method is invalid.")
    storyboard_scene = _find_storyboard_scene(project, scene["scene_id"])
    characters, location = _scene_context(project, scene, storyboard_scene)
    storyboard_fields = {
        field: storyboard_scene.get(field) if storyboard_scene is not None else None
        for field in (
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
    }
    basis: dict[str, object] = {
        "generation_method": method,
        "exact_duration_ms": scene["exact_duration_ms"],
        "source_kind": scene["source_kind"],
        "performance_lyric": _usable_performance_lyric(scene, storyboard_scene),
        "storyboard": storyboard_fields,
        "characters": [
            {
                "character_id": character["character_id"],
                "name": character["name"],
                "role": character["role"],
                "appearance": character["appearance"],
                "outfit": character["outfit"],
            }
            for character in characters
        ],
        "location": (
            {
                "location_id": location["location_id"],
                "name": location["name"],
                "description": location["description"],
            }
            if location is not None
            else None
        ),
    }
    if method == "keyframe_i2v":
        accepted = visual_scene["keyframe_i2v"]["accepted_keyframe"]
        basis["accepted_keyframe_asset_id"] = accepted["asset_id"] if accepted is not None else None
        basis["actual_keyframe_description"] = visual_scene["keyframe_i2v"]["actual_keyframe_description"]
    else:
        basis["reference_map"] = build_ref2va_mapping(project, scene["scene_id"])
    return basis


def build_prompt_source_fingerprint(
    project: dict[str, object],
    scene_id: object,
    generation_method: str | None = None,
) -> str:
    """Return the stable hash of the authoritative inputs for one method."""

    serialized = json.dumps(
        _prompt_source_basis(project, scene_id, generation_method),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _duration_sentence(scene: dict[str, object]) -> str:
    return f"{scene['exact_duration_ms'] / 1000:.3f}-second continuous shot"


def _terminated_sentence(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    return stripped if stripped[-1] in ".!?" else stripped + "."


def _machine_performance_lyric_line(
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
    characters: list[dict[str, object]],
) -> str | None:
    lyric = _usable_performance_lyric(scene, storyboard_scene)
    if lyric is None:
        return None
    speaker = next(
        (
            _text(character.get("name")).strip()
            for character in characters
            if _text(character.get("role")) in {"performer", "band_member"}
            and _text(character.get("name")).strip()
        ),
        None,
    )
    if speaker is None:
        return None
    return f"{speaker} (S1) sings: <d>[English] {lyric}</d>"


def _performance_lyric_lines(
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
    characters: list[dict[str, object]],
) -> list[str]:
    lyric_line = _machine_performance_lyric_line(scene, storyboard_scene, characters)
    if lyric_line is None:
        return []
    return [
        lyric_line,
        "Treat the supplied lyric as audio performance context, not visible on-screen text.",
    ]


def _i2v_opening_line(actual_description: str, accepted_present: bool) -> str:
    if not accepted_present:
        return "No accepted first frame is recorded; do not invent a visible opening image."
    if actual_description:
        return f"The accepted first frame establishes the opening: {_terminated_sentence(actual_description)}"
    return "The accepted first frame establishes the visible opening."


def _i2v_continuity_lines(
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
) -> list[str]:
    lines: list[str] = []
    for character in characters:
        name = _text(character.get("name")).strip()
        facts = [
            fact
            for fact in (
                _text(character.get("appearance")).strip(),
                _text(character.get("outfit")).strip(),
            )
            if fact
        ]
        if not name or not facts:
            continue
        lines.append(
            f"{name} remains consistent with the opening image: "
            + _terminated_sentence(" ".join(facts))
        )
    if location is not None:
        location_name = _text(location.get("name")).strip()
        description = _text(location.get("description")).strip()
        if location_name and description:
            lines.append(
                f"The {location_name} environment remains consistent with the opening image: "
                + _terminated_sentence(description)
            )
    return lines


def _i2v_continuation_lines(storyboard_scene: dict[str, object] | None) -> list[str]:
    if storyboard_scene is None:
        return ["The shot continues from the opening as one continuous take with coherent, restrained motion."]
    lines: list[str] = []
    for field, prefix in (
        ("action", "The performance continues: "),
        ("visual_instructions", "Visual treatment: "),
        ("camera_direction", "Camera: "),
        ("motion_direction", "Motion: "),
        ("continuity_notes", "Continuity guidance: "),
    ):
        value = _text(storyboard_scene.get(field)).strip()
        if value:
            lines.append(f"{prefix}{_terminated_sentence(value)}")
    if not lines:
        lines.append("The shot continues from the opening as one continuous take with coherent, restrained motion.")
    return lines


def _forward_intent_lines(storyboard_scene: dict[str, object] | None) -> list[str]:
    if storyboard_scene is None:
        return ["No applied storyboard allocation is recorded; keep subsequent motion coherent and restrained."]
    lines = [
        "Forward-generation intent applies after the opening state and must not rewrite the opening frame."
    ]
    if _text(storyboard_scene.get("scene_type")):
        lines.append(f"Storyboard scene type: {_text(storyboard_scene.get('scene_type'))}.")
    for label, field in (
        ("Action after the opening", "action"),
        ("Subsequent visual direction", "visual_instructions"),
        ("Camera behavior", "camera_direction"),
        ("Subsequent motion", "motion_direction"),
        ("Continuity guidance", "continuity_notes"),
    ):
        value = _text(storyboard_scene.get(field))
        if value:
            lines.append(f"{label}: {value}")
    return lines


_I2VA_FORBIDDEN_MARKERS = (
    "One continuous shot begins from the accepted first frame",
    "Opening state is defined exclusively by the accepted keyframe",
    "The target duration is exactly",
    "The accepted keyframe is <Picture 1>",
    "Actual accepted-keyframe description:",
    "Continuity context for",
    "Forward-generation intent",
    "Treat the supplied lyric as audio performance context",
    "machine-mapped ownership",
    "opening_state_rule",
    "source fingerprint",
)


def _i2v_components(
    scene: dict[str, object],
    visual_scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
) -> tuple[dict[str, object], list[str]]:
    keyframe = visual_scene["keyframe_i2v"]
    accepted = keyframe["accepted_keyframe"]
    actual_description = _text(keyframe.get("actual_keyframe_description")).strip()
    warnings: list[str] = []
    if accepted is None:
        warnings.append("No accepted keyframe is assigned for this scene.")
    elif not actual_description:
        warnings.append("The accepted keyframe has no actual_keyframe_description.")

    continuity_lines = _i2v_continuity_lines(characters, location)
    lyric_line = _machine_performance_lyric_line(scene, storyboard_scene, characters)
    integrated_description = [_i2v_opening_line(actual_description, accepted is not None)]
    integrated_description.extend(continuity_lines)
    if lyric_line is not None:
        integrated_description.append(lyric_line)
    components = {
        "method": "keyframe_i2v",
        "picture_reference": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> "
            "(from [Shot 1]) is fully referenced."
            if accepted is not None
            else "For the target video, no accepted first frame is assigned; do not invent a Picture 1 opening."
        ),
        "integrated_multimodal_description": integrated_description,
        "continuity_context": continuity_lines,
        "forward_intent": _i2v_continuation_lines(storyboard_scene),
        "performance_lyric": [lyric_line] if lyric_line is not None else [],
        "overall_soundscape": [
            "The supplied master-song scene audio is authoritative for timing and performance."
        ] + (["This is an instrumental scene; do not invent vocal words, lyrics, or dialogue."]
             if _is_instrumental_scene(scene) else []),
        "non_diegetic_music": [
            "The supplied master-song scene audio supplies the music and timing relationship for this shot."
        ],
    }
    return components, warnings


def _subject_definition_lines(
    project: dict[str, object],
    mapping: dict[str, object],
) -> list[str]:
    characters, locations = _entity_indexes(project)
    lines: list[str] = []
    for subject in mapping["subjects"]:
        entity = (
            characters.get(subject["entity_id"])
            if subject["entity_type"] == "character"
            else locations.get(subject["entity_id"])
        )
        if entity is None:
            raise PromptCompilationError("A planned reference subject is no longer present.")
        picture_tags = ", ".join(f"<Picture {number}>" for number in subject["picture_numbers"])
        name = _text(entity.get("name"))
        if subject["entity_type"] == "character":
            role = _text(entity.get("role")).replace("_", " ") or "character"
            definition = (
                f"{subject['subject_tag']} is {name}, the {role} represented by {picture_tags}"
            )
            continuity: list[str] = []
            appearance = _sentence_text(entity.get("appearance"))
            outfit = _sentence_text(entity.get("outfit"))
            if appearance:
                continuity.append(f"appearance continuity: {appearance}")
            if outfit:
                continuity.append(f"wardrobe continuity: {outfit}")
            if continuity:
                definition += ", with " + "; ".join(continuity)
            lines.append(definition + ".")
        else:
            definition = (
                f"{subject['subject_tag']} is {name}, the location represented by {picture_tags}"
            )
            description = _sentence_text(entity.get("description"))
            if description:
                definition += f", with environment continuity: {description}"
            lines.append(definition + ".")
    if not lines:
        lines.append("No Character or Location reference subjects are selected.")
    lines.append(
        "<Audio 1> is the authoritative master-song scene-audio segment supplied for the target video."
    )
    return lines


def _ref2v_style_opening(
    storyboard_scene: dict[str, object] | None,
    location: dict[str, object] | None,
) -> list[str]:
    visual_direction = _sentence_text((storyboard_scene or {}).get("visual_instructions"))
    if visual_direction:
        return [f"Overall visual direction: {visual_direction}."]
    if location is not None:
        description = _sentence_text(location.get("description"))
        if description:
            return [
                f"Overall visual treatment is grounded in the supplied {location['name']} environment: {description}."
            ]
    return [
        "Overall visual treatment is limited to the supplied reference Subjects and their established continuity."
    ]


def _ref2v_components(
    project: dict[str, object],
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
    mapping: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    if not mapping["pictures"]:
        warnings.append("No Ref2V still references are selected for this scene.")
    duration_seconds = f"{scene['exact_duration_ms'] / 1000:.3f}-second"
    character_tags = [
        subject["subject_tag"] for subject in mapping["subjects"] if subject["entity_type"] == "character"
    ]
    location_tags = [
        subject["subject_tag"] for subject in mapping["subjects"] if subject["entity_type"] == "location"
    ]
    subject_phrase = " and ".join(character_tags)
    if subject_phrase:
        subject_phrase = f" featuring {subject_phrase}"
    if location_tags:
        subject_phrase += f" within {' and '.join(location_tags)}"
    if not subject_phrase:
        subject_phrase = " using the supplied reference context"
    scene_type = _text((storyboard_scene or {}).get("scene_type")).replace("_", " ") or "music-video"
    subject_lines = _subject_definition_lines(project, mapping)
    retention_lines = [
        f"{subject['subject_tag']} (appears in [Shot 1]): fully_preserved - preserve the mapped "
        + (
            f"{_text(subject.get('entity_name'))} identity, appearance, and wardrobe continuity."
            if subject["entity_type"] == "character"
            else f"{_text(subject.get('entity_name'))} environment and spatial continuity."
        )
        for subject in mapping["subjects"]
    ]
    retention_lines.append(
        "<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track."
    )
    performance_lyric_lines = _performance_lyric_lines(scene, storyboard_scene, characters)
    style_opening = _ref2v_style_opening(storyboard_scene, location)
    detailed_lines = [
        *style_opening,
        "",
        "[Shot 1] One continuous shot; do not invent cuts or timed shot changes.",
        "Opening reference state: establish the selected Picture references in their machine-mapped ownership.",
        "Forward-generation intent follows the opening reference state and must not alter Picture or Subject ownership.",
        "Audio relationship: relate visible action and pacing to the supplied scene audio without inventing lyrics or dialogue.",
    ]
    if _is_instrumental_scene(scene):
        detailed_lines.append("This is an instrumental scene; do not invent vocal words, lyrics, or dialogue.")
    detailed_lines.extend(_forward_intent_lines(storyboard_scene))
    detailed_lines.extend(performance_lyric_lines)

    components = {
        "method": "reference2video",
        "sections": {
            "subject_definitions": subject_lines,
            "summary": [
                "[reference generation + audio reuse] "
                f"The target video is one coherent continuous {duration_seconds} {scene_type} shot"
                f"{subject_phrase}, while reusing <Audio 1> as the authoritative final scene audio."
            ],
            "retention_analysis": retention_lines,
            "detailed_description": detailed_lines,
            "overall_soundscape": [
                "No additional diegetic sound design is specified; keep incidental ambience and physical sounds restrained beneath the authoritative master-song audio."
            ],
            "non_diegetic_music": [
                "<Audio 1> is reused unchanged as the authoritative complete final audio track for this scene; do not invent or replace its music, lyrics, instrumentation, tempo, or arrangement."
            ],
        },
        "style_opening": style_opening,
        "performance_lyric": performance_lyric_lines,
    }
    return components, warnings


def _assemble_prompt(
    result_or_components: dict[str, object],
    enhanced_description: str | None = None,
) -> str:
    components = result_or_components.get("machine_components", result_or_components)
    method = components["method"]
    if method == "keyframe_i2v":
        integrated_lines = components["integrated_multimodal_description"]
        integrated_content = [*integrated_lines]
        if enhanced_description:
            integrated_content.append(enhanced_description)
        else:
            integrated_content.extend(components["forward_intent"])
        blocks = [
            components["picture_reference"],
            "\n".join(
                [
                    f"integrated_multimodal_description: [Shot 1] {integrated_content[0]}",
                    *integrated_content[1:],
                ]
            ),
            "\n".join(["overall_soundscape:", *components["overall_soundscape"]]),
            "\n".join(["non_diegetic_music:", *components["non_diegetic_music"]]),
        ]
        return "\n\n".join(blocks)

    sections = components["sections"]
    blocks: list[str] = []
    for section in REF2VA_SECTIONS:
        if section == "detailed_description" and enhanced_description:
            content = [
                *components["style_opening"],
                "",
                f"[Shot 1] {enhanced_description}",
                *components.get("performance_lyric", []),
            ]
        else:
            content = sections[section]
        blocks.append("\n".join([f"{section}:", *content]))
    return "\n\n".join(blocks)


def compile_scene_prompt(
    project: dict[str, object],
    scene_id: object,
    generation_method: str | None = None,
) -> dict[str, object]:
    """Compile one authoritative deterministic prompt without saving or executing."""

    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    method = generation_method or visual_scene.get("generation_method")
    if method not in GENERATION_METHODS:
        raise PromptCompilationError("Visuals generation_method is invalid.")
    storyboard_scene = _find_storyboard_scene(project, scene["scene_id"])
    characters, location = _scene_context(project, scene, storyboard_scene)
    if method == "reference2video":
        mapping = build_ref2va_mapping(project, scene["scene_id"])
        machine_components, warnings = _ref2v_components(
            project,
            scene,
            storyboard_scene,
            characters,
            location,
            mapping,
        )
    else:
        mapping = {"pictures": [], "subjects": [], "audio": None}
        machine_components, warnings = _i2v_components(
            scene,
            visual_scene,
            storyboard_scene,
            characters,
            location,
        )

    keyframe = visual_scene["keyframe_i2v"]
    storyboard_facts = {
        field: storyboard_scene.get(field) if storyboard_scene is not None else None
        for field in (
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
    }
    result = {
        "scene_id": scene["scene_id"],
        "generation_method": method,
        "source_fingerprint": build_prompt_source_fingerprint(project, scene["scene_id"], method),
        "scene_facts": {
            "timeline_start_ms": scene["timeline_start_ms"],
            "timeline_end_ms": scene["timeline_end_ms"],
            "exact_duration_ms": scene["exact_duration_ms"],
            "source_kind": scene["source_kind"],
            "lyric": scene.get("lyric"),
            "source_cue_numbers": scene["source_cue_numbers"],
            "accepted_keyframe_asset_id": (
                keyframe["accepted_keyframe"].get("asset_id")
                if keyframe.get("accepted_keyframe") is not None
                else None
            ),
            "actual_keyframe_description": keyframe["actual_keyframe_description"],
            "storyboard": storyboard_facts,
        },
        "reference_map": mapping,
        "machine_components": machine_components,
        "deterministic_prompt": "",
        "warnings": warnings,
    }
    result["deterministic_prompt"] = _assemble_prompt(result)
    return result


PROVENANCE_VALIDITY_CURRENT = "current"
PROVENANCE_VALIDITY_STALE = "stale"
PROVENANCE_VALIDITY_MISSING = "missing"


def _provenance_validity(saved_fingerprint: object, current_fingerprint: str) -> str:
    """One authoritative validity grade for a saved provenance fingerprint."""

    if not saved_fingerprint:
        return PROVENANCE_VALIDITY_MISSING
    if saved_fingerprint == current_fingerprint:
        return PROVENANCE_VALIDITY_CURRENT
    return PROVENANCE_VALIDITY_STALE


def derive_prompt_status(
    final_prompt: object,
    source_validity: str,
    relay_validity: str,
) -> str:
    """Derive the saved-prompt status from one authoritative validity pair.

    Source/relay validity (current/stale/missing) is the single input; editor
    state such as an unsaved draft is layered on top by the client using the
    same fingerprints.  STALE always outranks UNSAVED client-side because a
    stale draft cannot be saved against changed scene inputs.
    """

    if not final_prompt:
        return PROMPT_STATUS_NEEDS_GPT
    if source_validity != PROVENANCE_VALIDITY_CURRENT:
        return PROMPT_STATUS_STALE
    if relay_validity != PROVENANCE_VALIDITY_CURRENT:
        return PROMPT_STATUS_NEEDS_GPT
    return PROMPT_STATUS_CURRENT


def _find_prompt_scene(project: dict[str, object], scene_id: object) -> dict[str, object]:
    canonical_id = validate_entity_id(scene_id, "Scene ID")
    for prompt_scene in project.get("prompts", {}).get("scenes", []):
        if prompt_scene.get("scene_id") == canonical_id:
            return prompt_scene
    raise ProjectNotFoundError("The requested prompt scene was not found.")


def _prompt_record(project: dict[str, object], scene_id: object, generation_method: str) -> dict[str, object]:
    if generation_method not in GENERATION_METHODS:
        raise PromptCompilationError("Visuals generation_method is invalid.")
    return _find_prompt_scene(project, scene_id)[generation_method]


def _canonical_section_contents(
    text: str,
    sections: tuple[str, ...],
) -> dict[str, str]:
    section_pattern = re.compile(
        rf"(?m)^({'|'.join(re.escape(section) for section in sections)}):$"
    )
    matches = list(section_pattern.finditer(text))
    if [match.group(1) for match in matches] != list(sections):
        raise PromptCompilationError("Final prompt sections are missing, duplicated, or out of order.")
    contents: dict[str, str] = {}
    for index, match in enumerate(matches):
        if match.end() >= len(text) or text[match.end()] != "\n":
            raise PromptCompilationError("Final prompt section headings must occupy their own lines.")
        content_start = match.end() + 1
        if index + 1 < len(matches):
            next_start = matches[index + 1].start()
            if text[next_start - 2:next_start] != "\n\n" or (
                next_start >= 3 and text[next_start - 3] == "\n"
            ):
                raise PromptCompilationError("Final prompt sections must use one blank line between sections.")
            content_end = next_start - 2
        else:
            content_end = len(text)
        content = text[content_start:content_end]
        if not content.strip():
            raise PromptCompilationError("Final prompt sections cannot be empty.")
        contents[match.group(1)] = content
    return contents


def _canonical_i2v_contents(text: str) -> dict[str, str]:
    match = re.fullmatch(
        r"integrated_multimodal_description: (?P<integrated>.+?)"
        r"\n\noverall_soundscape:\n(?P<soundscape>.+?)"
        r"\n\nnon_diegetic_music:\n(?P<music>.+)",
        text,
        flags=re.DOTALL,
    )
    if match is None or any(not value.strip() for value in match.groupdict().values()):
        raise PromptCompilationError("Keyframe prompts must preserve the canonical three-field structure.")
    return {
        "integrated_multimodal_description": match.group("integrated"),
        "overall_soundscape": match.group("soundscape"),
        "non_diegetic_music": match.group("music"),
    }


def validate_final_prompt(result: dict[str, object], final_prompt: object) -> str:
    """Validate the user-authored final prompt without requiring deterministic equality."""

    if not isinstance(final_prompt, str):
        raise PromptCompilationError("Final prompt must be text.")
    normalized = final_prompt.strip()
    if not normalized:
        raise PromptCompilationError("Final prompt cannot be empty.")
    if len(normalized) > PROMPT_MAX_LENGTH:
        raise PromptCompilationError("Final prompt is too large.")
    if _DIALOGUE_TAG_PATTERN.search(normalized) and not _DIALOGUE_TAG_PATTERN.search(result["deterministic_prompt"]):
        raise PromptCompilationError("Final prompt cannot introduce dialogue markup for this scene.")

    if result["generation_method"] == "keyframe_i2v":
        picture_numbers = _tag_numbers(normalized, "Picture")
        if picture_numbers != {1}:
            raise PromptCompilationError("Keyframe prompts must preserve the machine-authored <Picture 1> opening reference.")
        if (
            _tag_numbers(normalized, "Subject")
            or _tag_numbers(normalized, "Audio")
            or _tag_numbers(normalized, "Video")
            or _LOCATION_TAG_PATTERN.search(normalized)
        ):
            raise PromptCompilationError("Keyframe prompts cannot contain Subject, Audio, Video, or Location tags.")
        official_opening = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> "
            "(from [Shot 1]) is fully referenced."
        )
        if not normalized.startswith(official_opening + "\n\n") or normalized.startswith(
            official_opening + "\n\n\n"
        ):
            raise PromptCompilationError("Keyframe prompts must preserve the official I2VA opening structure.")
        body = normalized[len(official_opening) + 2:]
        section_contents = _canonical_i2v_contents(body)
        if not section_contents["integrated_multimodal_description"].startswith("[Shot 1] "):
            raise PromptCompilationError("Keyframe prompts must preserve the machine-authored [Shot 1] wrapper.")
        shot_numbers = [int(number) for number in _SHOT_PATTERN.findall(normalized)]
        if shot_numbers != [1, 1] or section_contents["integrated_multimodal_description"].count("[Shot 1]") != 1:
            raise PromptCompilationError(
                "Keyframe prompts must preserve one [Shot 1] description wrapper plus its opening reference."
            )
        actual_description = _text(result.get("scene_facts", {}).get("actual_keyframe_description")).strip()
        opening_anchor = _i2v_opening_line(actual_description, True)
        if opening_anchor not in normalized:
            raise PromptCompilationError("Keyframe prompts must preserve the accepted-keyframe opening anchor.")
        if actual_description and actual_description not in normalized:
            raise PromptCompilationError("Keyframe prompts must preserve the actual accepted-keyframe description.")
        for marker in _I2VA_FORBIDDEN_MARKERS:
            if marker in normalized:
                raise PromptCompilationError("Keyframe prompts cannot contain internal compiler boilerplate.")
        return normalized

    mapping = result["reference_map"]
    expected_pictures = {item["picture_number"] for item in mapping["pictures"]}
    expected_subjects = {item["subject_number"] for item in mapping["pictures"]}
    picture_numbers = _tag_numbers(normalized, "Picture")
    subject_numbers = _tag_numbers(normalized, "Subject")
    audio_numbers = _tag_numbers(normalized, "Audio")
    if not picture_numbers.issubset(expected_pictures):
        raise PromptCompilationError("Final prompt contains an unknown Picture tag.")
    if not subject_numbers.issubset(expected_subjects):
        raise PromptCompilationError("Final prompt contains an unknown Subject tag.")
    if not audio_numbers.issubset({1}):
        raise PromptCompilationError("Final prompt contains an unknown Audio tag.")
    if picture_numbers != expected_pictures:
        raise PromptCompilationError("Final prompt must preserve every mapped Picture label.")
    if subject_numbers != expected_subjects:
        raise PromptCompilationError("Final prompt must preserve every mapped Subject label.")
    if audio_numbers != {1}:
        raise PromptCompilationError("Final prompt must preserve <Audio 1>.")
    if _LOCATION_TAG_PATTERN.search(normalized):
        raise PromptCompilationError("Final prompt cannot contain Location tags.")
    if not normalized.startswith("subject_definitions:"):
        raise PromptCompilationError("REF2VA prompts cannot contain a diagnostic preamble.")
    sections = _canonical_section_contents(normalized, REF2VA_SECTIONS)
    if not sections["summary"].startswith("[reference generation + audio reuse] "):
        raise PromptCompilationError("REF2VA summary must begin with the canonical task prefix.")
    subject_definitions = sections["subject_definitions"]
    if "Continuity details for" in subject_definitions:
        raise PromptCompilationError("REF2VA Subject continuity must be folded into each Subject definition.")
    if "fully_copy" in subject_definitions:
        raise PromptCompilationError("REF2VA Audio retention does not belong in subject_definitions.")
    if (
        "<Audio 1> is the authoritative master-song scene-audio segment supplied for the target video."
        not in subject_definitions
    ):
        raise PromptCompilationError("REF2VA subject_definitions must preserve the canonical Audio role.")
    detailed_description = sections["detailed_description"]
    shot_numbers = [int(number) for number in _SHOT_PATTERN.findall(normalized)]
    if any(number != 1 for number in shot_numbers):
        raise PromptCompilationError("REF2VA prompts cannot contain additional generated shots.")
    if (
        detailed_description.count("[Shot 1]") != 1
        or "\n\n[Shot 1] " not in detailed_description
        or not detailed_description.split("[Shot 1]", 1)[0].strip()
    ):
        raise PromptCompilationError("REF2VA prompts require a grounded style opening before [Shot 1].")
    for subject in mapping["subjects"]:
        retention_marker = f"{subject['subject_tag']} (appears in [Shot 1]): fully_preserved - "
        if retention_marker not in normalized:
            raise PromptCompilationError("REF2VA prompts must preserve machine-authored Subject retention.")
    if (
        "<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track."
        not in sections["retention_analysis"]
    ):
        raise PromptCompilationError("REF2VA prompts must preserve machine-authored Audio 1 retention.")
    if "— fully_" in sections["retention_analysis"] or "; fully_" in sections["retention_analysis"]:
        raise PromptCompilationError("REF2VA retention_analysis must use canonical colon and hyphen syntax.")
    return normalized


def prompt_scene_state(
    project: dict[str, object],
    scene_id: object,
    *,
    storage: ProjectStorage | None = None,
    generation_method: str | None = None,
) -> dict[str, object]:
    """Return one scene's computed prompt state without persisting derived status."""

    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    method = generation_method or visual_scene["generation_method"]
    result = compile_scene_prompt(project, scene["scene_id"], method)
    record = _prompt_record(project, scene["scene_id"], method)
    final_prompt = record["final_prompt"]
    current_fingerprint = result["source_fingerprint"]
    source_validity = _provenance_validity(record["source_fingerprint"], current_fingerprint)
    relay_validity = _provenance_validity(record["relay_fingerprint"], current_fingerprint)
    status = derive_prompt_status(final_prompt, source_validity, relay_validity)

    visual_readiness = derive_visual_readiness(project, scene["scene_id"], storage)
    return {
        "scene_id": scene["scene_id"],
        "sequence": project["scenes"].index(scene) + 1,
        "timeline_start_ms": scene["timeline_start_ms"],
        "timeline_end_ms": scene["timeline_end_ms"],
        "exact_duration_ms": scene["exact_duration_ms"],
        "source_kind": scene["source_kind"],
        "lyric": scene.get("lyric"),
        "generation_method": method,
        "status": status,
        "source_validity": source_validity,
        "relay_validity": relay_validity,
        "source_fingerprint": current_fingerprint,
        "saved_source_fingerprint": record["source_fingerprint"],
        "saved_relay_fingerprint": record["relay_fingerprint"],
        "saved_final_prompt": final_prompt,
        "deterministic_prompt": result["deterministic_prompt"],
        "reference_map": result["reference_map"],
        "visual_readiness": visual_readiness,
        "ready_for_render_prompt": status == PROMPT_STATUS_CURRENT and visual_readiness["ready"],
        "warnings": result["warnings"],
    }


def build_prompt_list(
    project: dict[str, object],
    storage: ProjectStorage | None = None,
) -> dict[str, object]:
    """Build deterministic, computed prompt cards for the current scene order."""

    scenes: list[dict[str, object]] = []
    for scene in project["scenes"]:
        try:
            scenes.append(prompt_scene_state(project, scene["scene_id"], storage=storage))
        except (PromptCompilationError, ProjectNotFoundError, ProjectValidationError) as error:
            visual_scene = _find_visual_scene(project, scene["scene_id"])
            scenes.append(
                {
                    "scene_id": scene["scene_id"],
                    "sequence": project["scenes"].index(scene) + 1,
                    "timeline_start_ms": scene["timeline_start_ms"],
                    "timeline_end_ms": scene["timeline_end_ms"],
                    "exact_duration_ms": scene["exact_duration_ms"],
                    "source_kind": scene["source_kind"],
                    "lyric": scene.get("lyric"),
                    "generation_method": visual_scene.get("generation_method"),
                    "status": "error",
                    "source_validity": PROVENANCE_VALIDITY_MISSING,
                    "relay_validity": PROVENANCE_VALIDITY_MISSING,
                    "source_fingerprint": None,
                    "saved_source_fingerprint": None,
                    "saved_relay_fingerprint": "",
                    "saved_final_prompt": "",
                    "deterministic_prompt": "",
                    "reference_map": {"pictures": [], "subjects": [], "audio": None},
                    "visual_readiness": {"ready": False, "missing": [str(error)]},
                    "ready_for_render_prompt": False,
                    "warnings": [str(error)],
                    "error": str(error),
                }
            )
    return {"project_id": project["project_id"], "schema_version": project["schema_version"], "scenes": scenes}


def save_scene_prompt(
    storage: ProjectStorage,
    project_id: object,
    scene_id: object,
    payload: object,
) -> dict[str, object]:
    """Persist only one method-scoped final prompt after a source conflict check."""

    if not isinstance(payload, dict) or set(payload) != {
        "generation_method",
        "final_prompt",
        "source_fingerprint",
        "relay_fingerprint",
    }:
        raise ProjectValidationError("Prompt save request has unsupported or missing fields.")
    method = payload.get("generation_method")
    if method not in GENERATION_METHODS:
        raise ProjectValidationError("Visuals generation_method is invalid.")
    client_fingerprint = payload.get("source_fingerprint")
    if not isinstance(client_fingerprint, str):
        raise ProjectValidationError("Prompt source_fingerprint is required.")
    project = storage.load_project(project_id)
    result = compile_scene_prompt(project, scene_id, method)
    if client_fingerprint != result["source_fingerprint"]:
        raise PromptConflictError("Scene inputs changed. Review this prompt before saving.")
    relay_fingerprint = payload.get("relay_fingerprint")
    if not isinstance(relay_fingerprint, str) or relay_fingerprint != result["source_fingerprint"]:
        raise PromptConflictError(
            "A current Prompt Director response must be applied before saving this Final Prompt."
        )
    final_prompt = validate_final_prompt(result, payload.get("final_prompt"))
    prompt_scene = _find_prompt_scene(project, scene_id)
    updated_prompt_scene = {
        **prompt_scene,
        method: {
            "final_prompt": final_prompt,
            "source_fingerprint": result["source_fingerprint"],
            "relay_fingerprint": relay_fingerprint,
        },
    }
    updated_prompt_scenes = [
        updated_prompt_scene if item["scene_id"] == prompt_scene["scene_id"] else item
        for item in project["prompts"]["scenes"]
    ]
    return storage.save_project(
        project["project_id"],
        {**project, "prompts": {"scenes": updated_prompt_scenes}},
        allow_prompts_change=True,
    )


def _tag_numbers(text: str, tag_type: str) -> set[int]:
    return {int(number) for kind, number in _TAG_PATTERN.findall(text) if kind == tag_type}


def _public_subject_context(
    project: dict[str, object],
    mapping: dict[str, object],
) -> list[dict[str, object]]:
    """Expose selected owner facts without IDs, filenames, or final prompt prose."""

    characters, locations = _entity_indexes(project)
    subjects: list[dict[str, object]] = []
    for subject in mapping["subjects"]:
        entity = (
            characters.get(subject["entity_id"])
            if subject["entity_type"] == "character"
            else locations.get(subject["entity_id"])
        )
        if entity is None:
            raise PromptCompilationError("A planned reference subject is no longer present.")
        public_subject: dict[str, object] = {
            "subject_tag": subject["subject_tag"],
            "entity_type": subject["entity_type"],
            "entity_name": _text(entity.get("name")),
        }
        if subject["entity_type"] == "character":
            public_subject.update(
                {
                    "appearance": _text(entity.get("appearance")),
                    "outfit": _text(entity.get("outfit")),
                    "role": _text(entity.get("role")),
                }
            )
        else:
            public_subject["description"] = _text(entity.get("description"))
        subjects.append(public_subject)
    return subjects


def _public_continuity_context(
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
) -> list[dict[str, object]]:
    context: list[dict[str, object]] = []
    for character in characters:
        context.append(
            {
                "entity_type": "character",
                "entity_name": _text(character.get("name")),
                "appearance": _text(character.get("appearance")),
                "outfit": _text(character.get("outfit")),
                "role": _text(character.get("role")),
            }
        )
    if location is not None:
        context.append(
            {
                "entity_type": "location",
                "entity_name": _text(location.get("name")),
                "description": _text(location.get("description")),
            }
        )
    return context


def build_prompt_relay_request(
    project: dict[str, object],
    scene_id: object,
    generation_method: str | None = None,
) -> dict[str, object]:
    """Build the deterministic, ephemeral request copied to the Custom GPT."""

    result = compile_scene_prompt(project, scene_id, generation_method)
    scene_facts = result["scene_facts"]
    duration_seconds = scene_facts["exact_duration_ms"] / 1000
    storyboard = scene_facts.get("storyboard") or {}
    creative_context = {
        field: storyboard.get(field) if isinstance(storyboard.get(field), str) else ""
        for field in (
            "scene_type",
            "action",
            "visual_instructions",
            "camera_direction",
            "motion_direction",
            "continuity_notes",
        )
    }
    creative_context["lyric_context"] = _usable_performance_lyric(
        _find_scene(project, result["scene_id"]),
        _find_storyboard_scene(project, result["scene_id"]),
    )
    characters, location = _scene_context(
        project,
        _find_scene(project, result["scene_id"]),
        _find_storyboard_scene(project, result["scene_id"]),
    )
    request: dict[str, object] = {
        "prompt_relay_request_version": PROMPT_RELAY_REQUEST_VERSION,
        "scene_id": result["scene_id"],
        "generation_method": result["generation_method"],
        "request_fingerprint": result["source_fingerprint"],
        "duration": {
            "seconds": duration_seconds,
            "description": _duration_sentence({"exact_duration_ms": scene_facts["exact_duration_ms"]}),
        },
        "creative_context": creative_context,
        "constraints": {
            "duration_is_exact": True,
            "one_continuous_shot_by_default": True,
            "source_audio_is_authoritative": True,
            "do_not_invent_dialogue_or_lyrics": True,
            "lyric_text_is_machine_owned": True,
            "lyric_context_is_read_only": True,
            "do_not_repeat_or_quote_lyric_text": True,
            "response_must_be_strict_json": True,
            "json_string_values_must_escape_embedded_double_quotes": True,
            "do_not_emit_shot_markers": True,
            "do_not_emit_section_headings": True,
            "do_not_emit_picture_audio_video_location_tags": True,
        },
        "response_contract": {
            "prompt_relay_response_version": PROMPT_RELAY_RESPONSE_VERSION,
            "required_fields": [
                "prompt_relay_response_version",
                "scene_id",
                "request_fingerprint",
                "enhanced_description",
            ],
            "additional_fields_allowed": False,
            "enhanced_description_max_length": ENHANCED_DESCRIPTION_MAX_LENGTH,
            "structural_facts_are_machine_owned": True,
        },
    }
    if result["generation_method"] == "keyframe_i2v":
        request["continuity_context"] = _public_continuity_context(characters, location)
        request["method_specific"] = {
            "accepted_keyframe_opening_state_is_authoritative": True,
            "actual_keyframe_description": scene_facts["actual_keyframe_description"],
            "accepted_keyframe_present": bool(scene_facts["accepted_keyframe_asset_id"]),
            "opening_state_rule": "The accepted keyframe and actual description define the visible opening; context only guides subsequent motion.",
        }
    else:
        mapping = result["reference_map"]
        request["subjects"] = _public_subject_context(project, mapping)
        request["reference_context"] = {
            "subject_ownership_is_machine_owned": True,
            "picture_mapping_summary": [
                {
                    "picture_number": picture["picture_number"],
                    "subject_tag": picture["subject_tag"],
                    "entity_name": picture["entity_name"],
                }
                for picture in mapping["pictures"]
            ],
        }
        request["constraints"].update(
            {
                "audio_1_is_machine_owned": True,
                "audio_1_role": "authoritative_scene_audio_segment; fully_copy",
                "may_use_supplied_subject_tags_for_reference2video": True,
                "detail_guidance": (
                    "For richly specified scenes, normally target approximately 350-500 English words; "
                    "for sparse scenes, expand only legitimate supplied detail without inventing new action."
                ),
            }
        )
        request["method_specific"] = {
            "picture_numbering_is_selected_order": True,
            "subject_numbering_is_first_distinct_owner_order": True,
            "location_owners_use_subject_tags": True,
        }
    return request


def _decode_prompt_relay_response(response: object) -> dict[str, object]:
    try:
        decoded = json.loads(response) if isinstance(response, str) else response
    except (TypeError, ValueError) as error:
        raise PromptCompilationError(
            "Custom GPT response is not valid JSON. Copy the raw JSON response again without Markdown or unescaped quotation marks."
        ) from error
    if not isinstance(decoded, dict):
        raise PromptCompilationError("Custom GPT response must be one JSON object.")
    if (
        "prompt_relay_request_version" in decoded
        and "prompt_relay_response_version" not in decoded
        and "enhanced_description" not in decoded
    ):
        raise PromptCompilationError(
            "Pasted JSON is a Prompt Director request, not a Prompt Director response."
        )
    expected = {
        "prompt_relay_response_version",
        "scene_id",
        "request_fingerprint",
        "enhanced_description",
    }
    if set(decoded) != expected:
        raise PromptCompilationError("Custom GPT response contains unsupported or missing fields.")
    if decoded.get("prompt_relay_response_version") != PROMPT_RELAY_RESPONSE_VERSION:
        raise PromptCompilationError("Custom GPT response version is unsupported.")
    if not isinstance(decoded.get("scene_id"), str):
        raise PromptCompilationError("Custom GPT response scene_id is required.")
    if not isinstance(decoded.get("request_fingerprint"), str):
        raise PromptCompilationError("Custom GPT response request_fingerprint is required.")
    return decoded


def validate_relay_description(result: dict[str, object], enhanced_description: object) -> str:
    """Validate only the bounded creative prose supplied by the Custom GPT."""

    if (
        not isinstance(enhanced_description, str)
        or not enhanced_description.strip()
        or len(enhanced_description) > ENHANCED_DESCRIPTION_MAX_LENGTH
    ):
        raise PromptCompilationError("Enhanced description is empty or too large.")
    enhanced_description = enhanced_description.strip()
    lowered = enhanced_description.lower()
    if any(marker in lowered for marker in _WHOLE_PROMPT_MARKERS):
        raise PromptCompilationError("Enhanced response must contain bounded creative prose only.")
    if _DIALOGUE_TAG_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot introduce dialogue control markup.")
    if _RETENTION_MARKER_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot author machine-owned retention relationships.")
    if _DURATION_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot alter the machine-authored duration.")
    if _SHOT_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot introduce Shot markers.")
    if re.search(
        r"(?im)^\s*(?:subject_definitions|summary|retention_analysis|detailed_description|"
        r"integrated_multimodal_description|overall_soundscape|non_diegetic_music)\s*:",
        enhanced_description,
    ):
        raise PromptCompilationError("Enhanced response cannot introduce prompt section headings.")

    performance_lyric = _text(result.get("scene_facts", {}).get("lyric")).strip()
    has_machine_owned_lyric = bool(result.get("machine_components", {}).get("performance_lyric"))
    if has_machine_owned_lyric and " ".join(performance_lyric.casefold().split()) in " ".join(
        enhanced_description.casefold().split()
    ):
        raise PromptCompilationError(
            "Enhanced response cannot repeat machine-owned lyric text; describe only its delivery."
        )

    mapping = result["reference_map"]
    if result["generation_method"] == "reference2video":
        expected_subjects = {item["subject_number"] for item in mapping["pictures"]}
        if _PICTURE_TAG_PATTERN.search(enhanced_description):
            raise PromptCompilationError("Enhanced REF2VA response cannot contain Picture tags.")
        if _AUDIO_TAG_PATTERN.search(enhanced_description):
            raise PromptCompilationError("Enhanced REF2VA response cannot contain Audio tags.")
        if _VIDEO_TAG_PATTERN.search(enhanced_description):
            raise PromptCompilationError("Enhanced REF2VA response cannot contain Video tags.")
        if _LOCATION_TAG_PATTERN.search(enhanced_description):
            raise PromptCompilationError("Enhanced REF2VA response cannot contain Location tags.")
        subject_tags = _SUBJECT_TAG_ANY_PATTERN.findall(enhanced_description)
        allowed_subject_tags = {f"<Subject {number}>" for number in expected_subjects}
        if any(tag not in allowed_subject_tags for tag in subject_tags):
            raise PromptCompilationError("Enhanced response contains an unknown Subject tag.")
        subject_numbers = _tag_numbers(enhanced_description, "Subject")
        if not subject_numbers.issubset(expected_subjects):
            raise PromptCompilationError("Enhanced response contains an unknown Subject tag.")
    elif _ANY_REFERENCE_TAG_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced I2V response cannot contain reference tags.")

    final_prompt = _assemble_prompt(result, enhanced_description)
    if len(final_prompt) > PROMPT_MAX_LENGTH:
        raise PromptCompilationError("Enhanced prompt is too large.")
    return final_prompt


def validate_prompt_relay_response(result: dict[str, object], response: object) -> dict[str, object]:
    """Validate a pasted response and reassemble a non-persisted prompt candidate."""

    document = _decode_prompt_relay_response(response)
    if document["scene_id"] != result["scene_id"]:
        raise PromptCompilationError("Custom GPT response belongs to a different scene.")
    if document["request_fingerprint"] != result["source_fingerprint"]:
        raise PromptConflictError("Custom GPT response is stale. Generate a new request first.")
    enhanced_description = validate_relay_description(result, document["enhanced_description"])
    return {
        "prompt_relay_response_version": PROMPT_RELAY_RESPONSE_VERSION,
        "scene_id": result["scene_id"],
        "generation_method": result["generation_method"],
        "request_fingerprint": result["source_fingerprint"],
        "enhanced_description": document["enhanced_description"].strip(),
        "enhanced_prompt": enhanced_description,
        "status": "validated",
    }
