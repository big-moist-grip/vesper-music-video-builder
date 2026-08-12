"""Narrow character, location, and project-local reference persistence."""

from __future__ import annotations

import logging
import os
import re
import shutil
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
    validate_entity_name,
    validate_entity_text,
    validate_original_name,
)


LOGGER = logging.getLogger(__name__)
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_REFERENCE_BYTES = 25 * 1024 * 1024
ENTITY_KINDS = frozenset({"characters", "locations"})
REFERENCE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
}
EDITABLE_CHARACTER_FIELDS = ("name", "role", "appearance", "outfit")
EDITABLE_LOCATION_FIELDS = ("name", "description")
_SAFE_UPLOAD_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]+$")


class EntityNotFoundError(ProjectNotFoundError):
    """The requested character or location does not exist."""


class ReferenceNotFoundError(ProjectNotFoundError):
    """The requested reference metadata or file does not exist."""


class ReferenceImportError(ProjectValidationError):
    """A reference upload is invalid or could not be installed safely."""


def _collection_for_kind(kind: str) -> str:
    if kind not in ENTITY_KINDS:
        raise ProjectValidationError("Entity type is invalid.")
    return kind


def _entity_directory(storage: ProjectStorage, project_id: object, kind: str, entity_id: object) -> Path:
    collection = _collection_for_kind(kind)
    canonical_entity_id = validate_entity_id(entity_id, f"{collection[:-1].capitalize()} ID")
    project_directory = storage.project_directory(project_id)
    return project_directory / "references" / collection / canonical_entity_id


def _find_entity(project: dict[str, object], kind: str, entity_id: object) -> dict[str, object]:
    collection = _collection_for_kind(kind)
    canonical_entity_id = validate_entity_id(entity_id, f"{collection[:-1].capitalize()} ID")
    for entity in project[collection]:
        key = "character_id" if collection == "characters" else "location_id"
        if entity[key] == canonical_entity_id:
            return entity
    raise EntityNotFoundError("The requested entity was not found.")


def _character_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != set(EDITABLE_CHARACTER_FIELDS):
        raise ProjectValidationError("Character request must contain name, role, appearance, and outfit.")
    role = payload.get("role")
    if role not in {"performer", "band_member", "extra"}:
        raise ProjectValidationError("Character role is invalid.")
    return {
        "name": validate_entity_name(payload.get("name"), "Character name"),
        "role": role,
        "appearance": validate_entity_text(payload.get("appearance"), "Character appearance"),
        "outfit": validate_entity_text(payload.get("outfit"), "Character outfit"),
    }


def _location_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != set(EDITABLE_LOCATION_FIELDS):
        raise ProjectValidationError("Location request must contain name and description.")
    return {
        "name": validate_entity_name(payload.get("name"), "Location name"),
        "description": validate_entity_text(payload.get("description"), "Location description"),
    }


def create_character(storage: ProjectStorage, project_id: object, payload: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    values = _character_payload(payload)
    character = {
        "character_id": str(uuid.uuid4()),
        **values,
        "references": [],
    }
    candidate = {**project, "characters": [*project["characters"], character]}
    return storage.save_project(project_id, candidate)


def update_character(
    storage: ProjectStorage,
    project_id: object,
    character_id: object,
    payload: object,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    canonical_id = validate_entity_id(character_id, "Character ID")
    values = _character_payload(payload)
    updated = False
    characters: list[dict[str, object]] = []
    for character in project["characters"]:
        if character["character_id"] == canonical_id:
            characters.append({**character, **values})
            updated = True
        else:
            characters.append(character)
    if not updated:
        raise EntityNotFoundError("The requested character was not found.")
    return storage.save_project(project_id, {**project, "characters": characters})


def delete_character(storage: ProjectStorage, project_id: object, character_id: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    canonical_id = validate_entity_id(character_id, "Character ID")
    from .storyboard import storyboard_uses_character

    if storyboard_uses_character(project["storyboard"], canonical_id):
        raise ProjectValidationError("Character is used by the applied storyboard.")
    characters = [
        character for character in project["characters"] if character["character_id"] != canonical_id
    ]
    if len(characters) == len(project["characters"]):
        raise EntityNotFoundError("The requested character was not found.")
    saved = storage.save_project(project_id, {**project, "characters": characters})
    _remove_entity_directory(storage, project_id, "characters", canonical_id)
    return saved


def create_location(storage: ProjectStorage, project_id: object, payload: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    values = _location_payload(payload)
    location = {
        "location_id": str(uuid.uuid4()),
        **values,
        "references": [],
    }
    candidate = {**project, "locations": [*project["locations"], location]}
    return storage.save_project(project_id, candidate)


def update_location(
    storage: ProjectStorage,
    project_id: object,
    location_id: object,
    payload: object,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    canonical_id = validate_entity_id(location_id, "Location ID")
    values = _location_payload(payload)
    updated = False
    locations: list[dict[str, object]] = []
    for location in project["locations"]:
        if location["location_id"] == canonical_id:
            locations.append({**location, **values})
            updated = True
        else:
            locations.append(location)
    if not updated:
        raise EntityNotFoundError("The requested location was not found.")
    return storage.save_project(project_id, {**project, "locations": locations})


def delete_location(storage: ProjectStorage, project_id: object, location_id: object) -> dict[str, object]:
    project = storage.load_project(project_id)
    canonical_id = validate_entity_id(location_id, "Location ID")
    from .storyboard import storyboard_uses_location

    if storyboard_uses_location(project["storyboard"], canonical_id):
        raise ProjectValidationError("Location is used by the applied storyboard.")
    locations = [
        location for location in project["locations"] if location["location_id"] != canonical_id
    ]
    if len(locations) == len(project["locations"]):
        raise EntityNotFoundError("The requested location was not found.")
    saved = storage.save_project(project_id, {**project, "locations": locations})
    _remove_entity_directory(storage, project_id, "locations", canonical_id)
    return saved


def _safe_upload_name(filename: object) -> str:
    if not isinstance(filename, str):
        raise ReferenceImportError("Uploaded reference must include a filename.")
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not basename or basename in {".", ".."} or not _SAFE_UPLOAD_NAME_PATTERN.fullmatch(basename):
        raise ReferenceImportError("Reference filename is invalid.")
    try:
        return validate_original_name(basename)
    except ProjectValidationError as error:
        raise ReferenceImportError(str(error)) from error


async def _write_reference_upload(upload, destination_directory: Path) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_directory,
            prefix=".reference-upload-",
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
                    raise ReferenceImportError("Uploaded reference data was invalid.")
                total_bytes += len(chunk)
                if total_bytes > MAX_REFERENCE_BYTES:
                    raise ReferenceImportError("Reference image is too large.")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total_bytes == 0:
            raise ReferenceImportError("Reference image is empty.")
        return temporary_path
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _decode_reference_format(candidate_path: Path) -> str:
    try:
        with Image.open(candidate_path) as image:
            image_format = image.format
            image.verify()
        with Image.open(candidate_path) as image:
            image.load()
            if getattr(image, "n_frames", 1) != 1:
                raise ReferenceImportError("Reference image must be a still image.")
    except ReferenceImportError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ReferenceImportError("Reference image is invalid.") from error

    if image_format not in REFERENCE_FORMATS:
        raise ReferenceImportError("Reference image must be PNG, JPEG, or WEBP.")
    return image_format


def _reference_metadata(entity: dict[str, object], reference_id: str) -> dict[str, object]:
    for reference in entity["references"]:
        if reference["reference_id"] == reference_id:
            return reference
    raise ReferenceNotFoundError("The requested reference was not found.")


async def add_reference(
    storage: ProjectStorage,
    project_id: object,
    kind: str,
    entity_id: object,
    original_filename: object,
    upload,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    entity = _find_entity(project, kind, entity_id)
    canonical_entity_id = validate_entity_id(
        entity_id,
        f"{'Character' if kind == 'characters' else 'Location'} ID",
    )
    original_name = _safe_upload_name(original_filename)
    entity_directory = _entity_directory(storage, project_id, kind, canonical_entity_id)
    entity_directory.mkdir(parents=True, exist_ok=True)
    candidate_path = await _write_reference_upload(upload, entity_directory)
    installed_path: Path | None = None
    try:
        image_format = _decode_reference_format(candidate_path)
        reference_id = str(uuid.uuid4())
        stored_name = f"{reference_id}{REFERENCE_FORMATS[image_format]}"
        installed_path = entity_directory / stored_name
        if installed_path.exists():
            raise ReferenceImportError("Reference ID collision prevented safe installation.")
        os.replace(candidate_path, installed_path)

        reference = {
            "reference_id": reference_id,
            "stored_name": stored_name,
            "original_name": original_name,
        }
        updated_entity = {**entity, "references": [*entity["references"], reference]}
        collection = _collection_for_kind(kind)
        key = "character_id" if collection == "characters" else "location_id"
        updated_entities = [
            updated_entity if current[key] == canonical_entity_id else current
            for current in project[collection]
        ]
        candidate_project = {**project, collection: updated_entities}
        try:
            return storage.save_project(project_id, candidate_project)
        except (ProjectPersistenceError, ProjectValidationError):
            _remove_file(installed_path)
            installed_path = None
            raise
    except ReferenceImportError:
        raise
    except OSError as error:
        if installed_path is not None:
            _remove_file(installed_path)
        raise ReferenceImportError("Reference image could not be installed safely.") from error
    finally:
        if candidate_path.exists():
            _remove_file(candidate_path)


def get_reference_path(
    storage: ProjectStorage,
    project_id: object,
    kind: str,
    entity_id: object,
    reference_id: object,
) -> Path:
    project = storage.load_project(project_id)
    canonical_reference_id = validate_entity_id(reference_id, "Reference ID")
    entity = _find_entity(project, kind, entity_id)
    metadata = _reference_metadata(entity, canonical_reference_id)
    entity_directory = _entity_directory(storage, project_id, kind, entity_id)
    reference_path = entity_directory / metadata["stored_name"]
    if reference_path.is_symlink() or not reference_path.is_file():
        raise ReferenceNotFoundError("Reference file is missing.")
    return reference_path


def remove_reference(
    storage: ProjectStorage,
    project_id: object,
    kind: str,
    entity_id: object,
    reference_id: object,
) -> dict[str, object]:
    project = storage.load_project(project_id)
    canonical_entity_id = validate_entity_id(
        entity_id,
        f"{'Character' if kind == 'characters' else 'Location'} ID",
    )
    canonical_reference_id = validate_entity_id(reference_id, "Reference ID")
    entity = _find_entity(project, kind, canonical_entity_id)
    metadata = _reference_metadata(entity, canonical_reference_id)
    from .storyboard import storyboard_uses_reference

    entity_type = "character" if kind == "characters" else "location"
    if storyboard_uses_reference(
        project["storyboard"],
        entity_type,
        canonical_entity_id,
        canonical_reference_id,
    ):
        raise ProjectValidationError("Reference is required by the applied storyboard.")
    updated_entity = {
        **entity,
        "references": [
            reference
            for reference in entity["references"]
            if reference["reference_id"] != canonical_reference_id
        ],
    }
    collection = _collection_for_kind(kind)
    key = "character_id" if collection == "characters" else "location_id"
    updated_entities = [
        updated_entity if current[key] == canonical_entity_id else current
        for current in project[collection]
    ]
    saved = storage.save_project(project_id, {**project, collection: updated_entities})
    _remove_file(_entity_directory(storage, project_id, collection, canonical_entity_id) / metadata["stored_name"])
    return saved


def _remove_entity_directory(storage: ProjectStorage, project_id: object, kind: str, entity_id: object) -> None:
    entity_directory = _entity_directory(storage, project_id, kind, entity_id)
    try:
        if entity_directory.is_symlink():
            entity_directory.unlink()
        elif entity_directory.exists():
            shutil.rmtree(entity_directory)
    except FileNotFoundError:
        pass
    except OSError as error:
        LOGGER.warning("Could not clean removed %s reference directory: %s", kind[:-1], error)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        LOGGER.warning("Could not clean reference file %s: %s", path.name, error)
