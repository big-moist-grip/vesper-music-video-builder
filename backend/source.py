"""Local master-audio and SRT import helpers."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .projects import (
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    SUPPORTED_AUDIO_EXTENSIONS,
    validate_original_name,
)


LOGGER = logging.getLogger(__name__)
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_SRT_BYTES = 10 * 1024 * 1024
_SRT_TIMESTAMP_PATTERN = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3})$"
)


class SourceImportError(Exception):
    """Base class for expected source-import failures."""


class UnsupportedAudioError(SourceImportError):
    """The selected audio filename is outside the supported policy."""


class AudioProbeError(SourceImportError):
    """The selected audio could not be recognised or probed."""


class MediaProbeUnavailableError(SourceImportError):
    """No usable local duration-probe executable is available."""


class SrtValidationError(SourceImportError):
    """The selected SRT is not a valid supported subtitle file."""


def _safe_original_name(filename: object) -> str:
    if not isinstance(filename, str):
        raise SourceImportError("Uploaded file must include a filename.")
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        return validate_original_name(basename)
    except ProjectValidationError as error:
        raise SourceImportError(str(error)) from error


def _audio_extension(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise UnsupportedAudioError(f"Unsupported audio format. Use one of: {supported}.")
    return extension


def find_ffprobe() -> str | None:
    """Return the locally available ffprobe executable without embedding its path."""

    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def probe_audio_duration_ms(
    audio_path: str | Path,
    ffprobe_path: str | None = None,
    runner=subprocess.run,
) -> int:
    executable = ffprobe_path or find_ffprobe()
    if not executable:
        raise MediaProbeUnavailableError("No local ffprobe executable is available.")

    arguments = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        completed = runner(
            arguments,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise MediaProbeUnavailableError("The local ffprobe executable could not be started.") from error
    except OSError as error:
        raise MediaProbeUnavailableError("The local media probe could not be started.") from error
    except subprocess.TimeoutExpired as error:
        raise AudioProbeError("Audio duration probing timed out.") from error

    if completed.returncode != 0:
        raise AudioProbeError("The selected audio could not be recognised.")

    output = completed.stdout.strip() if isinstance(completed.stdout, str) else ""
    if not output or len(output.splitlines()) != 1:
        raise AudioProbeError("Audio duration output was invalid.")
    try:
        duration_seconds = Decimal(output)
    except InvalidOperation as error:
        raise AudioProbeError("Audio duration output was invalid.") from error
    if not duration_seconds.is_finite() or duration_seconds <= 0:
        raise AudioProbeError("Audio duration must be a positive finite value.")

    duration_ms = int((duration_seconds * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))
    if duration_ms <= 0:
        raise AudioProbeError("Audio duration is too short to represent in milliseconds.")
    return duration_ms


def _parse_srt_timestamp(value: str) -> int:
    match = _SRT_TIMESTAMP_PATTERN.fullmatch(value.strip())
    if not match:
        raise SrtValidationError("SRT timestamp format is invalid.")
    hours, minutes, seconds, milliseconds = (int(group) for group in match.groups())
    if minutes > 59 or seconds > 59:
        raise SrtValidationError("SRT timestamp contains an invalid minute or second.")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def parse_srt(text: str) -> list[dict[str, object]]:
    if not isinstance(text, str):
        raise SrtValidationError("SRT content must be UTF-8 text.")
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []

    blocks = re.split(r"\n[ \t]*\n", normalized.strip("\n"))
    cues: list[dict[str, object]] = []
    seen_numbers: set[int] = set()
    previous_start = -1
    previous_end = 0

    for block_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise SrtValidationError(f"SRT cue block {block_index} is incomplete.")

        cue_number_text = lines[0].strip()
        if not cue_number_text.isdigit():
            raise SrtValidationError(f"SRT cue block {block_index} has an invalid cue number.")
        cue_number = int(cue_number_text)
        if cue_number <= 0 or cue_number in seen_numbers:
            raise SrtValidationError(f"SRT cue block {block_index} has an invalid or duplicate cue number.")
        seen_numbers.add(cue_number)

        timing_line = lines[1].strip()
        if "-->" not in timing_line:
            raise SrtValidationError(f"SRT cue block {block_index} has an invalid timing line.")
        start_text, end_text = timing_line.split("-->", 1)
        start_ms = _parse_srt_timestamp(start_text)
        end_ms = _parse_srt_timestamp(end_text)
        if start_ms >= end_ms:
            raise SrtValidationError(f"SRT cue block {block_index} must have positive duration.")
        if start_ms < previous_start or start_ms < previous_end:
            raise SrtValidationError("SRT cues must be chronological and non-overlapping.")

        cue_text = "\n".join(lines[2:])
        if not cue_text.strip():
            raise SrtValidationError(f"SRT cue block {block_index} has empty lyric text.")

        cues.append(
            {
                "cue_number": cue_number,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": cue_text,
            }
        )
        previous_start = start_ms
        previous_end = end_ms

    return cues


async def _write_upload_to_temp(upload, source_directory: Path, suffix: str) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=source_directory,
            prefix=".source-upload-",
            suffix=suffix,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            total_bytes = 0
            while True:
                chunk = await upload.read_chunk(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise SourceImportError("Uploaded file data was invalid.")
                handle.write(chunk)
                total_bytes += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total_bytes == 0:
            raise SourceImportError("Uploaded file is empty.")
        return temporary_path
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


async def _read_upload_bytes(upload) -> bytes:
    chunks: list[bytes] = []
    total_bytes = 0
    while True:
        chunk = await upload.read_chunk(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise SourceImportError("Uploaded file data was invalid.")
        total_bytes += len(chunk)
        if total_bytes > MAX_SRT_BYTES:
            raise SrtValidationError("SRT file is too large.")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _backup_path(source_directory: Path, original_path: Path) -> Path:
    return source_directory / f".{original_path.name}.{uuid.uuid4().hex}.backup"


def _restore_source_files(backups: list[tuple[Path, Path]], target_path: Path, installed: bool) -> None:
    if installed:
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("Could not remove failed replacement source file: %s", error)
    for original_path, backup_path in reversed(backups):
        if not backup_path.exists():
            continue
        try:
            os.replace(backup_path, original_path)
        except OSError as error:
            LOGGER.warning("Could not restore prior source file %s: %s", original_path, error)


def _replace_source_file(
    storage: ProjectStorage,
    project_id: object,
    current_project: dict[str, object],
    source_key: str,
    metadata: dict[str, object],
    candidate_path: Path,
) -> dict[str, object]:
    project_directory = storage.project_directory(project_id)
    source_directory = project_directory / "source"
    source_directory.mkdir(parents=True, exist_ok=True)
    target_path = source_directory / metadata["stored_name"]
    previous_metadata = current_project["source"][source_key]
    paths_to_backup: list[Path] = [target_path]
    if previous_metadata is not None:
        paths_to_backup.append(source_directory / previous_metadata["stored_name"])

    backups: list[tuple[Path, Path]] = []
    installed = False
    try:
        seen: set[Path] = set()
        for original_path in paths_to_backup:
            if original_path in seen or not original_path.is_file():
                continue
            seen.add(original_path)
            backup_path = _backup_path(source_directory, original_path)
            os.replace(original_path, backup_path)
            backups.append((original_path, backup_path))

        os.replace(candidate_path, target_path)
        installed = True
        candidate_project = {
            **current_project,
            "source": {**current_project["source"], source_key: metadata},
            "scenes": [],
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        saved_project = storage.save_project(
            project_id,
            candidate_project,
            allow_storyboard_change=True,
        )
    except (ProjectPersistenceError, ProjectValidationError):
        _restore_source_files(backups, target_path, installed)
        raise
    except OSError as error:
        _restore_source_files(backups, target_path, installed)
        raise ProjectPersistenceError("Could not persist imported source file.") from error

    for _original_path, backup_path in backups:
        try:
            backup_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("Could not remove prior source backup %s: %s", backup_path, error)
    return saved_project


async def import_master_audio(
    storage: ProjectStorage,
    project_id: object,
    original_filename: object,
    upload,
    probe_duration=probe_audio_duration_ms,
) -> dict[str, object]:
    current_project = storage.load_project(project_id)
    original_name = _safe_original_name(original_filename)
    extension = _audio_extension(original_name)
    source_directory = storage.project_directory(project_id) / "source"
    source_directory.mkdir(parents=True, exist_ok=True)

    candidate_path = await _write_upload_to_temp(upload, source_directory, ".audio.tmp")
    try:
        duration_ms = probe_duration(candidate_path)
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise AudioProbeError("Audio duration must be a positive integer.")
        metadata = {
            "stored_name": f"master_audio{extension}",
            "original_name": original_name,
            "duration_ms": duration_ms,
        }
        return _replace_source_file(storage, project_id, current_project, "master_audio", metadata, candidate_path)
    except SourceImportError:
        raise
    except (ProjectPersistenceError, ProjectValidationError):
        raise
    except OSError as error:
        raise SourceImportError("Master audio could not be imported.") from error
    finally:
        try:
            candidate_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("Could not clean up temporary audio upload: %s", error)


async def import_lyrics_srt(
    storage: ProjectStorage,
    project_id: object,
    original_filename: object,
    upload,
) -> dict[str, object]:
    current_project = storage.load_project(project_id)
    original_name = _safe_original_name(original_filename)
    content = await _read_upload_bytes(upload)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SrtValidationError("SRT file must be UTF-8 text.") from error
    cues = parse_srt(text)

    source_directory = storage.project_directory(project_id) / "source"
    source_directory.mkdir(parents=True, exist_ok=True)
    candidate_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=source_directory,
            prefix=".source-upload-",
            suffix=".srt.tmp",
            delete=False,
        ) as handle:
            candidate_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        metadata = {
            "stored_name": "lyrics.srt",
            "original_name": original_name,
            "cue_count": len(cues),
        }
        return _replace_source_file(storage, project_id, current_project, "lyrics_srt", metadata, candidate_path)
    except (ProjectPersistenceError, ProjectValidationError):
        raise
    except OSError as error:
        raise SourceImportError("Lyrics SRT could not be imported.") from error
    finally:
        if candidate_path is not None:
            try:
                candidate_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                LOGGER.warning("Could not clean up temporary SRT upload: %s", error)
