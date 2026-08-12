"""Small, fixed-root JSON persistence for Music Video Builder projects."""

from __future__ import annotations

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

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
PROJECT_FILENAME = "project.json"
DEFAULT_PROJECTS_ROOT = Path(
    r"D:\User Folders\Documents\Projects\vesper-music-video-builder\projects"
)
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
PROJECT_FIELDS = (
    "schema_version",
    "project_id",
    "name",
    "created_at",
    "updated_at",
    "source",
    "scenes",
)
SOURCE_FIELDS = ("master_audio", "lyrics_srt")
MASTER_AUDIO_FIELDS = ("stored_name", "original_name", "duration_ms")
LYRICS_SRT_FIELDS = ("stored_name", "original_name", "cue_count")
_MASTER_AUDIO_NAME_PATTERN = re.compile(r"^master_audio\.[a-z0-9]+$")


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


def validate_project_document(
    document: object,
    expected_project_id: str | None = None,
) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ProjectValidationError("Project document must be a JSON object.")

    schema_version = document.get("schema_version")
    if schema_version == LEGACY_SCHEMA_VERSION:
        normalized = _validate_legacy_project(document)
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
        normalized = {
            "schema_version": SCHEMA_VERSION,
            **base,
            "source": source,
            "scenes": scenes,
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

    def __init__(self, projects_root: str | Path = DEFAULT_PROJECTS_ROOT):
        self.projects_root = Path(projects_root)

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
        projects: list[dict[str, object]] = []
        invalid_projects: list[dict[str, str]] = []

        if not self.projects_root.exists():
            return {"projects": projects, "invalid_projects": invalid_projects}

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
                invalid_projects.append({"folder_name": candidate.name, "error": str(error)})
                continue

            projects.append(
                {
                    "project_id": project["project_id"],
                    "name": project["name"],
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                }
            )

        return {"projects": projects, "invalid_projects": invalid_projects}

    def load_project(self, project_id: object) -> dict[str, object]:
        canonical_id = validate_project_id(project_id)
        project_directory = self.project_directory(canonical_id)
        return self._read_project(project_directory, canonical_id)

    def save_project(self, project_id: object, document: object) -> dict[str, object]:
        canonical_id = validate_project_id(project_id)
        project_directory = self.project_directory(canonical_id)
        current = self._read_project(project_directory, canonical_id)
        if (
            isinstance(document, dict)
            and document.get("schema_version") == LEGACY_SCHEMA_VERSION
            and (current["source"]["master_audio"] is not None or current["source"]["lyrics_srt"] is not None or current["scenes"])
        ):
            raise ProjectValidationError("Legacy project data cannot overwrite Phase 2 project state.")

        candidate = validate_project_document(document, expected_project_id=canonical_id)
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
