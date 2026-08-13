"""Deterministic, project-local H3 prompt compilation for Phase 7.

The compiler is a narrow fact assembler.  It does not inspect donor workflow
text, invent narrative canon, execute H3, or mutate project state.
"""

from __future__ import annotations

import ipaddress
import json
import re
from copy import deepcopy
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .projects import ProjectNotFoundError, ProjectValidationError, validate_entity_id
from .visuals import GENERATION_METHODS, MAX_REF2VA_STILL_REFERENCES


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
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3:8b"
OLLAMA_TIMEOUT_SECONDS = 8.0
_TAG_PATTERN = re.compile(r"<(Picture|Subject|Audio)\s+(\d+)>")
_LOCATION_TAG_PATTERN = re.compile(r"<Location\b", re.IGNORECASE)
_SHOT_PATTERN = re.compile(r"\[(?:Shot|shot)\s+(\d+)\]")
_DIALOGUE_TAG_PATTERN = re.compile(r"<d\b", re.IGNORECASE)
_DURATION_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:milliseconds?|ms|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_WHOLE_PROMPT_MARKERS = (
    "subject_definitions:",
    "retention_analysis:",
    "detailed_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
    "deterministic h3 prompt",
    "scene_facts",
    "reference_map",
    "scene id:",
    "timeline:",
)
_GUARD_MARKER = "IMMUTABLE STRUCTURAL FACTS (DO NOT EDIT)"


class PromptCompilationError(ProjectValidationError):
    """The current project cannot provide a safe deterministic scene prompt."""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


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


def _duration_sentence(scene: dict[str, object]) -> str:
    return f"{scene['exact_duration_ms'] / 1000:.3f}-second continuous shot"


def _source_lines(
    scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
) -> list[str]:
    lines = [
        f"Duration: {_duration_sentence(scene)}.",
        "The supplied master-song scene audio is authoritative for timing and performance.",
    ]
    if _text(scene.get("source_kind")).lower() == "instrumental":
        lines.append("This is an instrumental scene; do not invent vocal words, lyrics, or dialogue.")
    lyric = _usable_performance_lyric(scene, storyboard_scene)
    if lyric is not None:
        lines.append(f"Supplied audio/lyric context (verbatim; do not render as visible text): {lyric}")
    return lines


def _entity_continuity_lines(
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
) -> list[str]:
    lines: list[str] = []
    for character in characters:
        details = [f"role: {_text(character.get('role'))}"]
        if _text(character.get("appearance")):
            details.append(f"appearance: {_text(character['appearance'])}")
        if _text(character.get("outfit")):
            details.append(f"outfit: {_text(character['outfit'])}")
        lines.append(
            f"Continuity context for {_text(character.get('name'))}: "
            + "; ".join(details)
            + "."
        )
    if location is not None:
        lines.append(
            f"Continuity context for {_text(location.get('name'))}: "
            f"environment: {_text(location.get('description'))}."
        )
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


def _i2v_components(
    scene: dict[str, object],
    visual_scene: dict[str, object],
    storyboard_scene: dict[str, object] | None,
    characters: list[dict[str, object]],
    location: dict[str, object] | None,
) -> tuple[dict[str, object], list[str]]:
    keyframe = visual_scene["keyframe_i2v"]
    accepted = keyframe["accepted_keyframe"]
    actual_description = _text(keyframe.get("actual_keyframe_description"))
    warnings: list[str] = []
    if accepted is None:
        warnings.append("No accepted keyframe is assigned for this scene.")
    elif not actual_description:
        warnings.append("The accepted keyframe has no actual_keyframe_description.")

    opening_state = [
        "Opening state is defined exclusively by the accepted keyframe and its actual description."
    ]
    if accepted is not None:
        opening_state.append("The accepted keyframe is the authoritative visible frame at the start of the shot.")
    else:
        opening_state.append("No accepted keyframe is recorded; do not invent a visible opening image.")
    if actual_description:
        opening_state.append(f"Actual accepted-keyframe description: {actual_description}")

    forward_lines = _forward_intent_lines(storyboard_scene)
    components = {
        "method": "keyframe_i2v",
        "opening_state": opening_state,
        "continuity_context": _entity_continuity_lines(characters, location),
        "forward_intent": forward_lines,
        "audio_lines": _source_lines(scene, storyboard_scene),
        "enhancement_brief": (
            "Write one concise paragraph containing only useful subsequent action, camera, "
            "motion, and continuity prose.  Do not restate or alter the authoritative opening "
            "state, duration, audio relationship, or structural metadata."
        ),
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
        entity_kind = "character" if subject["entity_type"] == "character" else "location"
        lines.append(
            f"{subject['subject_tag']} is {_text(entity.get('name'))}, the {entity_kind} "
            f"represented by {picture_tags}."
        )
        if subject["entity_type"] == "character":
            details = [f"role: {_text(entity.get('role'))}"]
            if _text(entity.get("appearance")):
                details.append(f"appearance: {_text(entity['appearance'])}")
            if _text(entity.get("outfit")):
                details.append(f"outfit: {_text(entity['outfit'])}")
            lines.append(f"Continuity details for {_text(entity.get('name'))}: " + "; ".join(details) + ".")
        elif _text(entity.get("description")):
            lines.append(
                f"Continuity details for {_text(entity.get('name'))}: "
                f"{_text(entity.get('description'))}."
            )
    if not lines:
        lines.append("No Character or Location reference subjects are selected.")
    lines.append("<Audio 1> is the authoritative scene-audio segment from the master song; fully_copy.")
    return lines


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
    subject_names = ", ".join(subject["entity_name"] for subject in mapping["subjects"])
    subject_context = f" ({subject_names})" if subject_names else ""
    duration_seconds = f"{scene['exact_duration_ms'] / 1000:.3f} seconds"
    environment = _text(location.get("name")) if location is not None else "the assigned performance environment"
    subject_lines = _subject_definition_lines(project, mapping)
    retention_lines = [
        f"{subject['subject_tag']} — preserve the mapped {_text(subject.get('entity_name'))} identity and appearance."
        for subject in mapping["subjects"]
    ]
    retention_lines.append("<Audio 1> — fully_copy; preserve the authoritative scene-audio relationship.")
    detailed_lines = [
        "[Shot 1] One continuous shot; do not invent cuts or timed shot changes.",
        "Opening reference state: establish the selected Picture references in their machine-mapped ownership.",
        "Forward-generation intent follows the opening reference state and must not alter Picture or Subject ownership.",
        "Audio relationship: relate visible action and pacing to the supplied scene audio without inventing lyrics or dialogue.",
    ]
    detailed_lines.extend(_entity_continuity_lines(characters, location))
    detailed_lines.extend(_forward_intent_lines(storyboard_scene))
    lyric = _usable_performance_lyric(scene, storyboard_scene)
    if lyric is not None:
        detailed_lines.append(f"Supplied audio/lyric context (verbatim; do not render as visible text): {lyric}")

    components = {
        "method": "reference2video",
        "sections": {
            "subject_definitions": subject_lines,
            "summary": [
                f"Create one coherent continuous H3 shot lasting exactly {duration_seconds} "
                f"with the mapped reference subjects{subject_context} "
                f"in {environment}; use the supplied scene audio as authoritative."
            ],
            "retention_analysis": retention_lines,
            "detailed_description": detailed_lines,
            "overall_soundscape": [
                "No additional diegetic or environmental sound requirement is supplied."
            ],
            "non_diegetic_music": [
                "<Audio 1> is the authoritative scene-audio segment from the master song and supplies the music and timing relationship."
            ],
        },
        "enhancement_brief": (
            "Write one concise paragraph containing only useful subsequent action, camera, "
            "motion, and continuity prose for this single continuous shot.  Do not restate or "
            "alter machine-authored Subject/Picture ownership, duration, audio, or section structure."
        ),
    }
    return components, warnings


def _assemble_prompt(
    result_or_components: dict[str, object],
    enhanced_description: str | None = None,
) -> str:
    components = result_or_components.get("machine_components", result_or_components)
    method = components["method"]
    if method == "keyframe_i2v":
        lines = [
            "KEYFRAME / IMAGE-TO-VIDEO H3 PROMPT",
            *components["audio_lines"],
            *components["opening_state"],
            "Continuity context for identity and preservation:",
            *components["continuity_context"],
        ]
        if enhanced_description:
            lines.extend(
                [
                    "Enhanced creative direction for action, camera, and motion after the opening state:",
                    enhanced_description,
                ]
            )
        else:
            lines.extend(
                [
                    "Creative direction for action, camera, and motion after the opening state:",
                    *components["forward_intent"],
                ]
            )
        lines.append("The accepted image remains the exclusive authority for the visible opening state.")
        return "\n".join(lines)

    sections = components["sections"]
    lines: list[str] = []
    for section in REF2VA_SECTIONS:
        lines.append(f"{section}:")
        lines.extend(sections[section])
        if section == "detailed_description" and enhanced_description:
            lines.extend(
                [
                    "Enhanced creative direction for subsequent action, camera, and motion:",
                    enhanced_description,
                ]
            )
    return "\n".join(lines)


def compile_scene_prompt(project: dict[str, object], scene_id: object) -> dict[str, object]:
    """Compile one authoritative deterministic prompt without saving or executing."""

    scene = _find_scene(project, scene_id)
    visual_scene = _find_visual_scene(project, scene["scene_id"])
    method = visual_scene.get("generation_method")
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


def build_immutable_structural_facts(result: dict[str, object]) -> str:
    """Serialize internal facts supplied to Ollama as input-only control data."""

    facts = {
        "scene_id": result["scene_id"],
        "generation_method": result["generation_method"],
        "scene_facts": deepcopy(result.get("scene_facts", {})),
        "reference_map": deepcopy(result["reference_map"]),
    }
    return _GUARD_MARKER + "\n" + json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tag_numbers(text: str, tag_type: str) -> set[int]:
    return {int(number) for kind, number in _TAG_PATTERN.findall(text) if kind == tag_type}


def _decode_bounded_response(response: object) -> dict[str, object]:
    try:
        decoded = json.loads(response) if isinstance(response, str) else response
    except (TypeError, ValueError) as error:
        raise PromptCompilationError("Ollama response is not valid JSON.") from error
    if not isinstance(decoded, dict):
        raise PromptCompilationError("Ollama response must be one JSON object.")
    if set(decoded) != {"enhanced_description"}:
        raise PromptCompilationError("Ollama response must contain only enhanced_description.")
    return decoded


def validate_enhanced_prompt(result: dict[str, object], response: object) -> str:
    """Validate bounded prose and reassemble it with machine-owned components."""

    document = _decode_bounded_response(response)
    enhanced_description = document["enhanced_description"]
    if (
        not isinstance(enhanced_description, str)
        or not enhanced_description.strip()
        or len(enhanced_description) > ENHANCED_DESCRIPTION_MAX_LENGTH
    ):
        raise PromptCompilationError("Enhanced description is empty or too large.")
    enhanced_description = enhanced_description.strip()
    lowered = enhanced_description.lower()
    if _GUARD_MARKER.lower() in lowered or any(marker in lowered for marker in _WHOLE_PROMPT_MARKERS):
        raise PromptCompilationError("Enhanced response must contain bounded creative prose only.")
    if _DIALOGUE_TAG_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot introduce dialogue control markup.")
    if _DURATION_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response cannot alter the machine-authored duration.")
    shot_numbers = {int(number) for number in _SHOT_PATTERN.findall(enhanced_description)}
    if any(number >= 2 for number in shot_numbers):
        raise PromptCompilationError("Enhanced response cannot introduce additional shots.")
    if _LOCATION_TAG_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced response contains an unsupported Location tag.")

    mapping = result["reference_map"]
    if result["generation_method"] == "reference2video":
        expected_pictures = {item["picture_number"] for item in mapping["pictures"]}
        expected_subjects = {item["subject_number"] for item in mapping["pictures"]}
        picture_numbers = _tag_numbers(enhanced_description, "Picture")
        subject_numbers = _tag_numbers(enhanced_description, "Subject")
        audio_numbers = _tag_numbers(enhanced_description, "Audio")
        if not picture_numbers.issubset(expected_pictures):
            raise PromptCompilationError("Enhanced response contains an unknown Picture tag.")
        if not subject_numbers.issubset(expected_subjects):
            raise PromptCompilationError("Enhanced response contains an unknown Subject tag.")
        if not audio_numbers.issubset({1}):
            raise PromptCompilationError("Enhanced response contains an unknown Audio tag.")
    elif _TAG_PATTERN.search(enhanced_description):
        raise PromptCompilationError("Enhanced I2V response contains REF2VA tags.")

    final_prompt = _assemble_prompt(result, enhanced_description)
    if len(final_prompt) > PROMPT_MAX_LENGTH:
        raise PromptCompilationError("Enhanced prompt is too large.")
    return final_prompt


def _loopback_url(base_url: str) -> bool:
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


def _fallback(result: dict[str, object], error_message: str, model: str) -> dict[str, object]:
    return {
        "status": "fallback",
        "prompt": result["deterministic_prompt"],
        "used_fallback": True,
        "model": model,
        "enhanced_description": None,
        "error": error_message,
    }


def enhance_prompt_with_ollama(
    result: dict[str, object],
    *,
    base_url: str = OLLAMA_BASE_URL,
    model: str = OLLAMA_MODEL,
    opener: Callable[..., Any] | None = None,
    timeout: float = OLLAMA_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Optionally enhance bounded prose through loopback Ollama."""

    requested_model = model if isinstance(model, str) else ""
    base_url = base_url.rstrip("/")
    if not _loopback_url(base_url):
        return _fallback(result, "Ollama enhancement requires a loopback endpoint.", requested_model)
    if not requested_model:
        return _fallback(result, "No local Ollama enhancement model is configured.", requested_model)

    guard = build_immutable_structural_facts(result)
    brief = result["machine_components"]["enhancement_brief"]
    instruction = (
        "Return exactly one JSON object with exactly one key, enhanced_description. "
        "Write only bounded creative/action/camera/motion prose for the current single shot. "
        "Do not return the deterministic prompt, section labels, JSON facts, tags, duration, "
        "IDs, filenames, dialogue markup, or the immutable guard."
    )
    input_prompt = f"{brief}\n\n{guard}\n\nInstruction: {instruction}"
    request = urllib_request.Request(
        f"{base_url}/api/generate",
        data=json.dumps(
            {
                "model": requested_model,
                "prompt": input_prompt,
                "stream": False,
                "format": "json",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    open_url = opener or urllib_request.urlopen
    try:
        response = open_url(request, timeout)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        outer = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if isinstance(outer, dict) and "response" in outer:
            response_value = outer["response"]
            bounded_response = json.loads(response_value) if isinstance(response_value, str) else response_value
        else:
            bounded_response = outer
        final_prompt = validate_enhanced_prompt(result, bounded_response)
        description = _decode_bounded_response(bounded_response)["enhanced_description"]
        return {
            "status": "enhanced",
            "prompt": final_prompt,
            "used_fallback": False,
            "model": requested_model,
            "enhanced_description": description,
            "error": None,
        }
    except (urllib_error.URLError, OSError, TimeoutError):
        return _fallback(result, "Local Ollama enhancement was unavailable.", requested_model)
    except (PromptCompilationError, UnicodeError, TypeError, ValueError, KeyError):
        return _fallback(result, "Ollama returned a response that failed prompt validation.", requested_model)
