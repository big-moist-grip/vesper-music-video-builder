"""Small, fixed-root JSON persistence for Music Video Builder projects."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .scenes import SceneValidationError, validate_scene_list


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 4
LEGACY_SCHEMA_VERSION = 1
LEGACY_SCHEMA_VERSION_2 = 2
LEGACY_SCHEMA_VERSION_3 = 3
PROJECT_FILENAME = "project.json"
DEFAULT_PROJECTS_ROOT = Path(
    r"D:\User Folders\Documents\Projects\vesper-music-video-builder\projects"
)
DEFAULT_STATE_ROOT = Path(
    r"D:\User Folders\Documents\Projects\vesper-music-video-builder\state"
)
INVALID_PROJECT_STATE_FILENAME = "ignored_invalid_projects.json"
INVALID_PROJECT_STATE_VERSION = 1
REQUIRED_PROJECT_DIRECTORIES = (
    "source",
    "references",
    "references/characters",
    "references/locations",
    "keyframes",
    "scene_audio",
    "renders",
    "export",
)
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus"})
LEGACY_PROJECT_FIELDS = ("schema_version", "project_id", "name", "created_at", "updated_at")
V2_PROJECT_FIELDS = LEGACY_PROJECT_FIELDS + ("source", "scenes")
V3_PROJECT_FIELDS = (
    "schema_version",
    "project_id",
    "name",
    "created_at",
    "updated_at",
    "source",
    "scenes",
    "characters",
    "locations",
)
PROJECT_FIELDS = V3_PROJECT_FIELDS + ("story_direction", "storyboard")
SOURCE_FIELDS = ("master_audio", "lyrics_srt")
MASTER_AUDIO_FIELDS = ("stored_name", "original_name", "duration_ms")
LYRICS_SRT_FIELDS = ("stored_name", "original_name", "cue_count")
REFERENCE_FIELDS = ("reference_id", "stored_name", "original_name")
CHARACTER_FIELDS = ("character_id", "name", "role", "appearance", "outfit", "references")
LOCATION_FIELDS = ("location_id", "name", "description", "references")
CHARACTER_ROLES = frozenset({"performer", "band_member", "extra"})
_MASTER_AUDIO_NAME_PATTERN = re.compile(r"^master_audio\.[a-z0-9]+$")
_REFERENCE_EXTENSIONS = frozenset({".png", ".jpg", ".webp"})
_INVALID_PROJECT_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _empty_story_direction() -> dict[str, str]:
    return {
        "storyboard_mode": "loose",
        "story_brief": "",
        "visual_notes": "",
    }


class ProjectError(Exception):
    """Base class for expected project-storage failures."""


class ProjectValidationError(ProjectError):
    """The project or an input value does not satisfy the project schema."""


class ProjectNotFoundError(ProjectError):
    """The requested project does not exist beneath the fixed root."""


class ProjectPersistenceError(ProjectError):
    """Filesystem or atomic-replacement failure."""


def validate_project_id(project_id: object) -> str:
    if not isinstance(project_id, str):
        raise ProjectValidationError("Project ID must be a UUID string.")

    try:
        parsed = uuid.UUID(project_id)
    except (ValueError, AttributeError) as error:
        raise ProjectValidationError("Project ID must be a valid UUID.") from error

    canonical = str(parsed)
    if project_id != canonical:
        raise ProjectValidationError("Project ID must use canonical UUID form.")
    return canonical


def validate_invalid_project_folder_name(folder_name: object) -> str:
    if not isinstance(folder_name, str) or not folder_name:
        raise ProjectValidationError("Invalid project folder identifier is required.")
    if folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
        raise ProjectValidationError("Invalid project folder identifier is unsafe.")
    if any(ord(character) < 32 or ord(character) == 127 for character in folder_name):
        raise ProjectValidationError("Invalid project folder identifier is unsafe.")
    return folder_name


def validate_invalid_project_signature(signature: object) -> str:
    if not isinstance(signature, str) or not _INVALID_PROJECT_SIGNATURE_PATTERN.fullmatch(signature):
        raise ProjectValidationError("Invalid project signature is malformed.")
    return signature


def invalid_project_signature(folder_name: object, error: object) -> str:
    validated_folder_name = validate_invalid_project_folder_name(folder_name)
    if not isinstance(error, str) or not error:
        raise ProjectValidationError("Invalid project error is required.")
    canonical = json.dumps(
        [validated_folder_name, error],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_entity_id(entity_id: object, label: str = "Entity ID") -> str:
    if not isinstance(entity_id, str):
        raise ProjectValidationError(f"{label} must be a UUID string.")

    try:
        parsed = uuid.UUID(entity_id)
    except (ValueError, AttributeError) as error:
        raise ProjectValidationError(f"{label} must be a valid UUID.") from error

    canonical = str(parsed)
    if entity_id != canonical:
        raise ProjectValidationError(f"{label} must use canonical UUID form.")
    return canonical


def validate_project_name(name: object) -> str:
    if not isinstance(name, str):
        raise ProjectValidationError("Project name must be a string.")

    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ProjectValidationError("Project name contains unsupported control characters.")

    trimmed = name.strip()
    if not trimmed:
        raise ProjectValidationError("Project name cannot be empty.")
    if len(trimmed) > 200:
        raise ProjectValidationError("Project name is too long.")
    return trimmed


def validate_entity_name(name: object, label: str) -> str:
    if not isinstance(name, str):
        raise ProjectValidationError(f"{label} must be a string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ProjectValidationError(f"{label} contains unsupported control characters.")
    trimmed = name.strip()
    if not trimmed:
        raise ProjectValidationError(f"{label} cannot be empty.")
    if len(trimmed) > 200:
        raise ProjectValidationError(f"{label} is too long.")
    return trimmed


def validate_entity_text(value: object, label: str, maximum_length: int = 5_000) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be a string.")
    if any(ord(character) < 32 and character not in "\n\r\t" or ord(character) == 127 for character in value):
        raise ProjectValidationError(f"{label} contains unsupported control characters.")
    if len(value) > maximum_length:
        raise ProjectValidationError(f"{label} is too long.")
    return value


def validate_original_name(name: object) -> str:
    if not isinstance(name, str):
        raise ProjectValidationError("Original filename must be a string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ProjectValidationError("Original filename contains unsupported control characters.")
    if not name or len(name) > 255 or "/" in name or "\\" in name:
        raise ProjectValidationError("Original filename must be a local filename.")
    return name


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProjectValidationError("Project timestamps must be ISO-8601 strings.")

    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ProjectValidationError("Project timestamps must be valid ISO-8601 values.") from error

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectValidationError("Project timestamps must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    timestamp = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_base_project(document: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    if set(document) != set(fields):
        raise ProjectValidationError("Project document has unsupported or missing fields.")

    project_id = validate_project_id(document.get("project_id"))
    name = validate_project_name(document.get("name"))
    created_at = document.get("created_at")
    updated_at = document.get("updated_at")
    created_datetime = parse_timestamp(created_at)
    updated_datetime = parse_timestamp(updated_at)
    if updated_datetime < created_datetime:
        raise ProjectValidationError("Project updated_at cannot precede created_at.")

    return {
        "project_id": project_id,
        "name": name,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _validate_legacy_project(document: dict[str, object]) -> dict[str, object]:
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != LEGACY_SCHEMA_VERSION:
        raise ProjectValidationError("Unsupported project schema version.")

    base = _validate_base_project(document, LEGACY_PROJECT_FIELDS)
    return {
        "schema_version": SCHEMA_VERSION,
        **base,
        "source": {"master_audio": None, "lyrics_srt": None},
        "scenes": [],
        "characters": [],
        "locations": [],
        "story_direction": _empty_story_direction(),
        "storyboard": {"request_fingerprint": None, "scenes": []},
    }


def _validate_v2_project(document: dict[str, object]) -> dict[str, object]:
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != LEGACY_SCHEMA_VERSION_2:
        raise ProjectValidationError("Unsupported project schema version.")

    base = _validate_base_project(document, V2_PROJECT_FIELDS)
    source = _validate_source(document.get("source"))
    scenes = document.get("scenes")
    if not isinstance(scenes, list):
        raise ProjectValidationError("Project scenes must be an array.")
    if scenes:
        if source["master_audio"] is None or source["lyrics_srt"] is None:
            raise ProjectValidationError("Built scenes require master audio and lyrics SRT metadata.")
        try:
            validate_scene_list(scenes, source["master_audio"]["duration_ms"])
        except SceneValidationError as error:
            raise ProjectValidationError(str(error)) from error
    return {
        "schema_version": SCHEMA_VERSION,
        **base,
        "source": source,
        "scenes": scenes,
        "characters": [],
        "locations": [],
        "story_direction": _empty_story_direction(),
        "storyboard": {"request_fingerprint": None, "scenes": []},
    }


def _validate_v3_project(document: dict[str, object]) -> dict[str, object]:
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != LEGACY_SCHEMA_VERSION_3:
        raise ProjectValidationError("Unsupported project schema version.")

    base = _validate_base_project(document, V3_PROJECT_FIELDS)
    source = _validate_source(document.get("source"))
    scenes = document.get("scenes")
    if not isinstance(scenes, list):
        raise ProjectValidationError("Project scenes must be an array.")
    if scenes:
        if source["master_audio"] is None or source["lyrics_srt"] is None:
            raise ProjectValidationError("Built scenes require master audio and lyrics SRT metadata.")
        try:
            validate_scene_list(scenes, source["master_audio"]["duration_ms"])
        except SceneValidationError as error:
            raise ProjectValidationError(str(error)) from error
    characters, locations = _validate_entity_lists(
        document.get("characters"),
        document.get("locations"),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        **base,
        "source": source,
        "scenes": scenes,
        "characters": characters,
        "locations": locations,
        "story_direction": _empty_story_direction(),
        "storyboard": {"request_fingerprint": None, "scenes": []},
    }


def _validate_master_audio(metadata: object) -> dict[str, object] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict) or set(metadata) != set(MASTER_AUDIO_FIELDS):
        raise ProjectValidationError("Master-audio metadata is invalid.")

    stored_name = metadata.get("stored_name")
    if (
        not isinstance(stored_name, str)
        or stored_name != stored_name.lower()
        or not _MASTER_AUDIO_NAME_PATTERN.fullmatch(stored_name)
        or Path(stored_name).suffix not in SUPPORTED_AUDIO_EXTENSIONS
    ):
        raise ProjectValidationError("Master-audio stored filename is invalid.")

    original_name = validate_original_name(metadata.get("original_name"))
    duration_ms = metadata.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ProjectValidationError("Master-audio duration_ms must be a positive integer.")

    return {
        "stored_name": stored_name,
        "original_name": original_name,
        "duration_ms": duration_ms,
    }


def _validate_lyrics_srt(metadata: object) -> dict[str, object] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict) or set(metadata) != set(LYRICS_SRT_FIELDS):
        raise ProjectValidationError("Lyrics SRT metadata is invalid.")

    if metadata.get("stored_name") != "lyrics.srt":
        raise ProjectValidationError("Lyrics SRT stored filename is invalid.")
    original_name = validate_original_name(metadata.get("original_name"))
    cue_count = metadata.get("cue_count")
    if isinstance(cue_count, bool) or not isinstance(cue_count, int) or cue_count < 0:
        raise ProjectValidationError("Lyrics SRT cue_count must be a non-negative integer.")

    return {
        "stored_name": "lyrics.srt",
        "original_name": original_name,
        "cue_count": cue_count,
    }


def _validate_source(source: object) -> dict[str, object]:
    if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS):
        raise ProjectValidationError("Project source state is invalid.")
    return {
        "master_audio": _validate_master_audio(source.get("master_audio")),
        "lyrics_srt": _validate_lyrics_srt(source.get("lyrics_srt")),
    }


def _validate_reference_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict) or set(metadata) != set(REFERENCE_FIELDS):
        raise ProjectValidationError("Reference metadata is invalid.")

    reference_id = validate_entity_id(metadata.get("reference_id"), "Reference ID")
    stored_name = metadata.get("stored_name")
    if not isinstance(stored_name, str) or "/" in stored_name or "\\" in stored_name:
        raise ProjectValidationError("Reference stored filename must be a local filename.")
    extension = Path(stored_name).suffix.lower()
    if extension not in _REFERENCE_EXTENSIONS or stored_name != f"{reference_id}{extension}":
        raise ProjectValidationError("Reference stored filename is invalid.")

    original_name = validate_original_name(metadata.get("original_name"))
    if original_name in {".", ".."}:
        raise ProjectValidationError("Reference original filename is invalid.")
    return {
        "reference_id": reference_id,
        "stored_name": stored_name,
        "original_name": original_name,
    }


def _validate_references(references: object) -> list[dict[str, object]]:
    if not isinstance(references, list):
        raise ProjectValidationError("Entity references must be an array.")

    validated: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for reference in references:
        normalized = _validate_reference_metadata(reference)
        reference_id = normalized["reference_id"]
        if reference_id in seen_ids:
            raise ProjectValidationError("Entity reference IDs must be unique.")
        seen_ids.add(reference_id)
        validated.append(normalized)
    return validated


def _validate_character(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != set(CHARACTER_FIELDS):
        raise ProjectValidationError("Character data has unsupported or missing fields.")

    role = document.get("role")
    if role not in CHARACTER_ROLES:
        raise ProjectValidationError("Character role is invalid.")
    return {
        "character_id": validate_entity_id(document.get("character_id"), "Character ID"),
        "name": validate_entity_name(document.get("name"), "Character name"),
        "role": role,
        "appearance": validate_entity_text(document.get("appearance"), "Character appearance"),
        "outfit": validate_entity_text(document.get("outfit"), "Character outfit"),
        "references": _validate_references(document.get("references")),
    }


def _validate_locations(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != set(LOCATION_FIELDS):
        raise ProjectValidationError("Location data has unsupported or missing fields.")
    return {
        "location_id": validate_entity_id(document.get("location_id"), "Location ID"),
        "name": validate_entity_name(document.get("name"), "Location name"),
        "description": validate_entity_text(document.get("description"), "Location description"),
        "references": _validate_references(document.get("references")),
    }


def _validate_entity_lists(
    characters: object,
    locations: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(characters, list):
        raise ProjectValidationError("Project characters must be an array.")
    if not isinstance(locations, list):
        raise ProjectValidationError("Project locations must be an array.")

    validated_characters: list[dict[str, object]] = []
    character_ids: set[str] = set()
    for character in characters:
        normalized = _validate_character(character)
        character_id = normalized["character_id"]
        if character_id in character_ids:
            raise ProjectValidationError("Character IDs must be unique.")
        character_ids.add(character_id)
        validated_characters.append(normalized)

    validated_locations: list[dict[str, object]] = []
    location_ids: set[str] = set()
    for location in locations:
        normalized = _validate_locations(location)
        location_id = normalized["location_id"]
        if location_id in location_ids:
            raise ProjectValidationError("Location IDs must be unique.")
        location_ids.add(location_id)
        validated_locations.append(normalized)

    return validated_characters, validated_locations


def validate_project_document(
    document: object,
    expected_project_id: str | None = None,
) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ProjectValidationError("Project document must be a JSON object.")

    schema_version = document.get("schema_version")
    if schema_version == LEGACY_SCHEMA_VERSION:
        normalized = _validate_legacy_project(document)
    elif schema_version == LEGACY_SCHEMA_VERSION_2:
        normalized = _validate_v2_project(document)
    elif schema_version == LEGACY_SCHEMA_VERSION_3:
        normalized = _validate_v3_project(document)
    elif schema_version == SCHEMA_VERSION:
        base = _validate_base_project(document, PROJECT_FIELDS)
        source = _validate_source(document.get("source"))
        scenes = document.get("scenes")
        if not isinstance(scenes, list):
            raise ProjectValidationError("Project scenes must be an array.")
        if scenes:
            if source["master_audio"] is None or source["lyrics_srt"] is None:
                raise ProjectValidationError("Built scenes require master audio and lyrics SRT metadata.")
            try:
                validate_scene_list(scenes, source["master_audio"]["duration_ms"])
            except SceneValidationError as error:
                raise ProjectValidationError(str(error)) from error
        characters, locations = _validate_entity_lists(
            document.get("characters"),
            document.get("locations"),
        )
        from .storyboard import validate_applied_storyboard, validate_story_direction

        story_direction = validate_story_direction(document.get("story_direction"))
        storyboard = validate_applied_storyboard(
            document.get("storyboard"),
            scenes,
            characters,
            locations,
        )
        normalized = {
            "schema_version": SCHEMA_VERSION,
            **base,
            "source": source,
            "scenes": scenes,
            "characters": characters,
            "locations": locations,
            "story_direction": story_direction,
            "storyboard": storyboard,
        }
    else:
        raise ProjectValidationError("Unsupported project schema version.")

    if expected_project_id is not None:
        expected = validate_project_id(expected_project_id)
        if normalized["project_id"] != expected:
            raise ProjectValidationError("Project ID does not match the requested project.")
    return normalized


def atomic_write_json(destination: Path, document: dict[str, object]) -> None:
    """Replace a JSON file atomically using a temporary file in its directory."""

    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            if hasattr(os, "fsync"):
                os.fsync(handle.fileno())

        os.replace(temporary_path, destination)
    except Exception as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                LOGGER.warning("Could not clean up project temporary file: %s", cleanup_error)
        raise ProjectPersistenceError("Could not persist project JSON atomically.") from error


class ProjectStorage:
    """Persistence operations rooted at one fixed project directory."""

    def __init__(
        self,
        projects_root: str | Path = DEFAULT_PROJECTS_ROOT,
        state_root: str | Path | None = None,
    ):
        self.projects_root = Path(projects_root)
        if state_root is not None:
            self.state_root = Path(state_root)
        elif self.projects_root == DEFAULT_PROJECTS_ROOT:
            self.state_root = DEFAULT_STATE_ROOT
        else:
            self.state_root = self.projects_root.parent / "state"

    def create_project(self, name: object) -> dict[str, object]:
        validated_name = validate_project_name(name)
        project_id = str(uuid.uuid4())
        created_at = utc_timestamp()
        project = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "name": validated_name,
            "created_at": created_at,
            "updated_at": created_at,
            "source": {"master_audio": None, "lyrics_srt": None},
            "scenes": [],
            "characters": [],
            "locations": [],
            "story_direction": _empty_story_direction(),
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        project_directory = self.projects_root / project_id
        project_file = project_directory / PROJECT_FILENAME
        created_directory = False

        try:
            self.projects_root.mkdir(parents=True, exist_ok=True)
            project_directory.mkdir(exist_ok=False)
            created_directory = True
            for relative_directory in REQUIRED_PROJECT_DIRECTORIES:
                (project_directory / relative_directory).mkdir()
            atomic_write_json(project_file, project)
            return project
        except ProjectPersistenceError:
            self._clean_incomplete_directory(project_directory, project_file, created_directory)
            raise
        except OSError as error:
            self._clean_incomplete_directory(project_directory, project_file, created_directory)
            raise ProjectPersistenceError("Could not create project storage.") from error

    def project_directory(self, project_id: object) -> Path:
        canonical_id = validate_project_id(project_id)
        project_directory = self.projects_root / canonical_id
        if not project_directory.is_dir() or project_directory.is_symlink():
            raise ProjectNotFoundError("Project was not found.")
        return project_directory

    def list_projects(self) -> dict[str, list[dict[str, object]]]:
        projects, invalid_projects = self._scan_projects()
        ignored_signatures = self._load_ignored_invalid_signatures()
        visible_invalid_projects = [
            invalid_project
            for invalid_project in invalid_projects
            if invalid_project["signature"] not in ignored_signatures
        ]
        return {"projects": projects, "invalid_projects": visible_invalid_projects}

    def delete_project(self, project_id: object) -> dict[str, object]:
        canonical_id = validate_project_id(project_id)
        candidate_directory = self.projects_root / canonical_id
        if self.projects_root.is_symlink() or candidate_directory.is_symlink():
            raise ProjectValidationError("Project deletion target is unsafe.")
        project_directory = self.project_directory(canonical_id)

        # Confirm the target is a valid project before allowing recursive removal.
        self._read_project(project_directory, canonical_id)

        try:
            projects_root = self.projects_root.resolve(strict=True)
            target_directory = project_directory.resolve(strict=True)
        except OSError as error:
            raise ProjectPersistenceError("Project deletion target could not be verified.") from error

        if target_directory == projects_root or projects_root not in target_directory.parents:
            raise ProjectValidationError("Project deletion target is unsafe.")

        try:
            shutil.rmtree(project_directory)
        except OSError as error:
            raise ProjectPersistenceError("Project could not be deleted.") from error
        return {"project_id": canonical_id, "deleted": True}

    def ignore_invalid_project(self, folder_name: object, signature: object) -> dict[str, object]:
        validated_folder_name = validate_invalid_project_folder_name(folder_name)
        validated_signature = validate_invalid_project_signature(signature)
        _projects, invalid_projects = self._scan_projects()
        current_entry = next(
            (
                invalid_project
                for invalid_project in invalid_projects
                if invalid_project["folder_name"] == validated_folder_name
            ),
            None,
        )
        if current_entry is None:
            raise ProjectNotFoundError("Invalid project entry was not found.")
        if current_entry["signature"] != validated_signature:
            raise ProjectValidationError("Invalid project entry changed; refresh and retry.")

        ignored_signatures = self._load_ignored_invalid_signatures()
        ignored_signatures.add(validated_signature)
        self._save_ignored_invalid_signatures(ignored_signatures)
        return {
            "ignored": True,
            "folder_name": validated_folder_name,
            "signature": validated_signature,
        }

    def load_project(self, project_id: object) -> dict[str, object]:
        canonical_id = validate_project_id(project_id)
        project_directory = self.project_directory(canonical_id)
        return self._read_project(project_directory, canonical_id)

    def save_project(
        self,
        project_id: object,
        document: object,
        *,
        allow_storyboard_change: bool = False,
    ) -> dict[str, object]:
        canonical_id = validate_project_id(project_id)
        project_directory = self.project_directory(canonical_id)
        current = self._read_project(project_directory, canonical_id)
        if isinstance(document, dict) and document.get("schema_version") in {
            LEGACY_SCHEMA_VERSION,
            LEGACY_SCHEMA_VERSION_2,
            LEGACY_SCHEMA_VERSION_3,
        }:
            current_has_state = (
                current["source"]["master_audio"] is not None
                or current["source"]["lyrics_srt"] is not None
                or current["scenes"]
                or current["characters"]
                or current["locations"]
                or current["story_direction"] != _empty_story_direction()
                or current["storyboard"]["scenes"]
            )
            if current_has_state:
                raise ProjectValidationError("Legacy project data cannot overwrite current project state.")

        candidate = validate_project_document(document, expected_project_id=canonical_id)
        if not allow_storyboard_change:
            candidate["storyboard"] = current["storyboard"]
        candidate["created_at"] = current["created_at"]

        current_updated_at = parse_timestamp(current["updated_at"])
        now = datetime.now(timezone.utc)
        candidate["updated_at"] = utc_timestamp(max(current_updated_at, now))
        atomic_write_json(project_directory / PROJECT_FILENAME, candidate)
        return candidate

    def _read_project(self, project_directory: Path, expected_project_id: str) -> dict[str, object]:
        project_file = project_directory / PROJECT_FILENAME
        try:
            raw = project_file.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ProjectValidationError("Project is missing project.json.") from error
        except (OSError, UnicodeError) as error:
            raise ProjectPersistenceError("Project could not be read.") from error

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProjectValidationError("Project JSON is malformed.") from error
        return validate_project_document(document, expected_project_id=expected_project_id)

    def _scan_projects(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        projects: list[dict[str, object]] = []
        invalid_projects: list[dict[str, str]] = []

        if not self.projects_root.exists():
            return projects, invalid_projects

        try:
            candidates = sorted(self.projects_root.iterdir(), key=lambda path: path.name.casefold())
        except OSError as error:
            raise ProjectPersistenceError("Could not list projects.") from error

        for candidate in candidates:
            if not candidate.is_dir() or candidate.is_symlink():
                continue

            try:
                project_id = validate_project_id(candidate.name)
                project = self._read_project(candidate, project_id)
            except ProjectError as error:
                safe_error = str(error)
                invalid_projects.append(
                    {
                        "folder_name": candidate.name,
                        "error": safe_error,
                        "signature": invalid_project_signature(candidate.name, safe_error),
                    }
                )
                continue

            projects.append(
                {
                    "project_id": project["project_id"],
                    "name": project["name"],
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                }
            )

        return projects, invalid_projects

    def _load_ignored_invalid_signatures(self) -> set[str]:
        state_file = self.state_root / INVALID_PROJECT_STATE_FILENAME
        try:
            raw = state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeError) as error:
            LOGGER.warning("Could not read ignored invalid-project state: %s", error)
            return set()

        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            LOGGER.warning("Ignored invalid-project state is malformed: %s", error)
            return set()

        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "ignored_signatures"}
            or document.get("schema_version") != INVALID_PROJECT_STATE_VERSION
            or not isinstance(document.get("ignored_signatures"), list)
        ):
            LOGGER.warning("Ignored invalid-project state has an unsupported shape.")
            return set()

        signatures = document["ignored_signatures"]
        if any(
            not isinstance(signature, str)
            or _INVALID_PROJECT_SIGNATURE_PATTERN.fullmatch(signature) is None
            for signature in signatures
        ):
            LOGGER.warning("Ignored invalid-project state contains malformed signatures.")
            return set()
        return set(signatures)

    def _save_ignored_invalid_signatures(self, signatures: set[str]) -> None:
        document = {
            "schema_version": INVALID_PROJECT_STATE_VERSION,
            "ignored_signatures": sorted(signatures),
        }
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.state_root / INVALID_PROJECT_STATE_FILENAME, document)
        except ProjectPersistenceError:
            raise
        except OSError as error:
            raise ProjectPersistenceError("Could not save ignored invalid-project state.") from error

    @staticmethod
    def _clean_incomplete_directory(
        project_directory: Path,
        project_file: Path,
        created_directory: bool,
    ) -> None:
        if not created_directory or project_file.exists():
            return
        try:
            shutil.rmtree(project_directory)
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("Could not clean incomplete project directory: %s", error)
