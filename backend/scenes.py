"""Deterministic lyric/instrumental scene construction and validation."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path


MIN_SCENE_DURATION_MS = 2_000
MAX_SCENE_DURATION_MS = 10_000
SCENE_FIELDS = (
    "scene_id",
    "source_kind",
    "timeline_start_ms",
    "timeline_end_ms",
    "exact_duration_ms",
    "lyric",
    "source_cue_numbers",
    "split_index",
    "split_count",
)


class SceneValidationError(ValueError):
    """A scene list or scene source segment is invalid."""


class SceneConstructionError(SceneValidationError):
    """A project cannot produce a complete scene timeline."""


def _validate_integer(value: object, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SceneValidationError(f"{label} must be an integer.")
    if minimum is not None and value < minimum:
        raise SceneValidationError(f"{label} must be at least {minimum}.")
    return value


def _validate_scene_id(value: object) -> str:
    if not isinstance(value, str):
        raise SceneValidationError("Scene ID must be a UUID string.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise SceneValidationError("Scene ID must be a valid UUID.") from error
    canonical = str(parsed)
    if value != canonical:
        raise SceneValidationError("Scene ID must use canonical UUID form.")
    return canonical


def _validate_source_cue_numbers(value: object, source_kind: str) -> list[int]:
    if not isinstance(value, list):
        raise SceneValidationError("Scene source_cue_numbers must be an array.")

    cue_numbers: list[int] = []
    seen_numbers: set[int] = set()
    for cue_number in value:
        validated_number = _validate_integer(cue_number, "Scene source cue number", 1)
        if validated_number in seen_numbers:
            raise SceneValidationError("Scene source cue numbers must be unique.")
        seen_numbers.add(validated_number)
        cue_numbers.append(validated_number)

    if source_kind == "lyric" and not cue_numbers:
        raise SceneValidationError("Lyric scenes require source cue provenance.")
    if source_kind == "instrumental" and cue_numbers:
        raise SceneValidationError("Instrumental scenes cannot contain lyric provenance.")
    return cue_numbers


def validate_scene_list(scenes: object, audio_duration_ms: object) -> list[dict[str, object]]:
    if not isinstance(scenes, list):
        raise SceneValidationError("Scenes must be an array.")
    if isinstance(audio_duration_ms, bool) or not isinstance(audio_duration_ms, int) or audio_duration_ms <= 0:
        raise SceneValidationError("Master-audio duration must be a positive integer.")
    if not scenes:
        return scenes
    if audio_duration_ms < MIN_SCENE_DURATION_MS:
        raise SceneValidationError("Master audio is shorter than the 2 second scene minimum.")

    seen_ids: set[str] = set()
    previous_end: int | None = None
    previous_start: int | None = None

    for position, scene in enumerate(scenes):
        if not isinstance(scene, dict) or set(scene) != set(SCENE_FIELDS):
            raise SceneValidationError(f"Scene {position + 1} has unsupported or missing fields.")

        scene_id = _validate_scene_id(scene.get("scene_id"))
        if scene_id in seen_ids:
            raise SceneValidationError("Scene IDs must be unique.")
        seen_ids.add(scene_id)

        source_kind = scene.get("source_kind")
        if source_kind not in {"lyric", "instrumental"}:
            raise SceneValidationError("Scene source_kind is invalid.")

        start_ms = _validate_integer(scene.get("timeline_start_ms"), "Scene timeline_start_ms", 0)
        end_ms = _validate_integer(scene.get("timeline_end_ms"), "Scene timeline_end_ms", 1)
        exact_duration_ms = _validate_integer(scene.get("exact_duration_ms"), "Scene exact_duration_ms", 1)
        if end_ms <= start_ms:
            raise SceneValidationError("Scene timeline_end_ms must be greater than timeline_start_ms.")
        if exact_duration_ms != end_ms - start_ms:
            raise SceneValidationError("Scene exact_duration_ms must match its timeline range.")
        if exact_duration_ms < MIN_SCENE_DURATION_MS:
            raise SceneValidationError("Scene duration is shorter than the 2 second generation minimum.")
        if exact_duration_ms > MAX_SCENE_DURATION_MS:
            raise SceneValidationError("Scene duration exceeds the 10 second policy limit.")
        if end_ms > audio_duration_ms:
            raise SceneValidationError("Scene timeline exceeds the master-audio duration.")

        source_cue_numbers = _validate_source_cue_numbers(scene.get("source_cue_numbers"), source_kind)
        if source_kind == "lyric":
            lyric = scene.get("lyric")
            if not isinstance(lyric, str) or not lyric.strip():
                raise SceneValidationError("Lyric scenes require non-empty lyric text.")
        else:
            if scene.get("lyric") is not None or source_cue_numbers:
                raise SceneValidationError("Instrumental scenes cannot contain lyric metadata.")

        split_index = _validate_integer(scene.get("split_index"), "Scene split_index", 1)
        split_count = _validate_integer(scene.get("split_count"), "Scene split_count", 1)
        if split_index > split_count:
            raise SceneValidationError("Scene split_index cannot exceed split_count.")

        if position == 0:
            if start_ms != 0:
                raise SceneValidationError("The first scene must start at 0 ms.")
        else:
            if previous_start is not None and start_ms < previous_start:
                raise SceneValidationError("Scenes must be sorted by timeline_start_ms.")
            if previous_end != start_ms:
                raise SceneValidationError("Scenes must be exactly contiguous without gaps or overlaps.")

        previous_start = start_ms
        previous_end = end_ms

    if previous_end != audio_duration_ms:
        raise SceneValidationError("The final scene must end at the master-audio duration.")
    return scenes


def _split_range(start_ms: int, end_ms: int) -> list[tuple[int, int, int, int]]:
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        raise SceneConstructionError("Scene regions must have positive duration.")
    if duration_ms < MIN_SCENE_DURATION_MS:
        raise SceneConstructionError("A scene region is shorter than the 2 second generation minimum.")
    if duration_ms <= MAX_SCENE_DURATION_MS:
        return [(start_ms, end_ms, 1, 1)]

    split_count = (duration_ms + MAX_SCENE_DURATION_MS - 1) // MAX_SCENE_DURATION_MS
    base_duration, remainder = divmod(duration_ms, split_count)
    if base_duration < MIN_SCENE_DURATION_MS:
        raise SceneConstructionError("Scene splitting cannot satisfy the 2 second generation minimum.")

    pieces: list[tuple[int, int, int, int]] = []
    cursor = start_ms
    for split_index in range(1, split_count + 1):
        piece_duration = base_duration + (1 if split_index <= remainder else 0)
        piece_end = cursor + piece_duration
        pieces.append((cursor, piece_end, split_index, split_count))
        cursor = piece_end
    return pieces


def _validate_cues(cues: object, audio_duration_ms: int) -> list[dict[str, object]]:
    if not isinstance(cues, list):
        raise SceneConstructionError("Parsed SRT cues must be an array.")

    normalized: list[dict[str, object]] = []
    previous_start = -1
    previous_end = 0
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) != {"cue_number", "start_ms", "end_ms", "text"}:
            raise SceneConstructionError("Parsed SRT cue data is invalid.")
        cue_number = _validate_integer(cue.get("cue_number"), "SRT cue number", 1)
        start_ms = _validate_integer(cue.get("start_ms"), "SRT cue start_ms", 0)
        end_ms = _validate_integer(cue.get("end_ms"), "SRT cue end_ms", 1)
        text = cue.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SceneConstructionError("SRT cue text cannot be empty.")
        if start_ms >= end_ms:
            raise SceneConstructionError("SRT cue start must precede its end.")
        if start_ms < previous_start or start_ms < previous_end:
            raise SceneConstructionError("SRT cues must be chronological and non-overlapping.")
        if end_ms > audio_duration_ms:
            raise SceneConstructionError("SRT cue timing exceeds the master-audio duration.")
        normalized.append(
            {
                "cue_number": cue_number,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
        )
        previous_start = start_ms
        previous_end = end_ms
    return normalized


def _new_block(start_ms: int, end_ms: int, cues: list[dict[str, object]] | None = None) -> dict[str, object]:
    cue_list = list(cues or [])
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "cues": cue_list,
        "source_kind": "lyric" if cue_list else "instrumental",
    }


def _block_duration(block: dict[str, object]) -> int:
    return int(block["end_ms"]) - int(block["start_ms"])


def _lyric_run_ranges(blocks: list[dict[str, object]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(blocks):
        if not blocks[index]["cues"]:
            index += 1
            continue
        start = index
        while index < len(blocks) and blocks[index]["cues"]:
            index += 1
        ranges.append((start, index))
    return ranges


def _run_duration(blocks: list[dict[str, object]], start: int, end: int) -> int:
    return int(blocks[end - 1]["end_ms"]) - int(blocks[start]["start_ms"])


def _absorb_available_instrumental_time(
    blocks: list[dict[str, object]],
    start: int,
    end: int,
    needed_ms: int,
) -> int:
    absorbed_ms = 0
    for neighbor_index, direction in ((start - 1, "before"), (end, "after")):
        if absorbed_ms >= needed_ms or not (0 <= neighbor_index < len(blocks)):
            continue
        neighbor = blocks[neighbor_index]
        if neighbor["cues"]:
            continue

        available_ms = max(0, _block_duration(neighbor) - MIN_SCENE_DURATION_MS)
        take_ms = min(needed_ms - absorbed_ms, available_ms)
        if take_ms <= 0:
            continue
        if direction == "before":
            neighbor["end_ms"] = int(neighbor["end_ms"]) - take_ms
            blocks[start]["start_ms"] = int(blocks[start]["start_ms"]) - take_ms
        else:
            neighbor["start_ms"] = int(neighbor["start_ms"]) + take_ms
            blocks[end - 1]["end_ms"] = int(blocks[end - 1]["end_ms"]) + take_ms
        absorbed_ms += take_ms
    return absorbed_ms


def _remove_instrumental_boundary(blocks: list[dict[str, object]], start: int, end: int) -> bool:
    if start > 0 and not blocks[start - 1]["cues"]:
        instrumental = blocks[start - 1]
        blocks[start]["start_ms"] = int(instrumental["start_ms"])
        del blocks[start - 1]
        return True
    if end < len(blocks) and not blocks[end]["cues"]:
        instrumental = blocks[end]
        blocks[end - 1]["end_ms"] = int(instrumental["end_ms"])
        del blocks[end]
        return True
    return False


def _expand_short_lyric_runs(blocks: list[dict[str, object]]) -> None:
    while True:
        changed = False
        for start, end in _lyric_run_ranges(blocks):
            duration_ms = _run_duration(blocks, start, end)
            if duration_ms >= MIN_SCENE_DURATION_MS:
                continue

            needed_ms = MIN_SCENE_DURATION_MS - duration_ms
            _absorb_available_instrumental_time(blocks, start, end, needed_ms)
            if _run_duration(blocks, start, end) >= MIN_SCENE_DURATION_MS:
                changed = True
                break

            if not _remove_instrumental_boundary(blocks, start, end):
                raise SceneConstructionError("Short lyric material cannot satisfy the 2 second scene minimum.")
            changed = True
            break
        if not changed:
            return


def _group_region(cue_blocks: list[dict[str, object]]) -> dict[str, object]:
    if not cue_blocks:
        raise SceneConstructionError("Lyric scene groups cannot be empty.")
    cues: list[dict[str, object]] = []
    for block in cue_blocks:
        cues.extend(block["cues"])
    return _new_block(int(cue_blocks[0]["start_ms"]), int(cue_blocks[-1]["end_ms"]), cues)


def _partition_short_cue_run(cue_blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    if not cue_blocks:
        return []

    @lru_cache(maxsize=None)
    def solve(start_index: int) -> tuple[int, tuple[int, ...]] | None:
        if start_index == len(cue_blocks):
            return (0, ())

        best: tuple[int, tuple[int, ...]] | None = None
        for end_index in range(start_index + 1, len(cue_blocks) + 1):
            duration_ms = int(cue_blocks[end_index - 1]["end_ms"]) - int(cue_blocks[start_index]["start_ms"])
            if duration_ms < MIN_SCENE_DURATION_MS:
                continue
            if duration_ms > MAX_SCENE_DURATION_MS:
                break

            remainder = solve(end_index)
            if remainder is None:
                continue
            candidate = (remainder[0] + 1, (end_index,) + remainder[1])
            if best is None or candidate[0] > best[0]:
                best = candidate
        return best

    solution = solve(0)
    if solution is None:
        raise SceneConstructionError("Lyric cue run cannot satisfy the 2–10 second scene policy.")

    groups: list[dict[str, object]] = []
    start_index = 0
    for end_index in solution[1]:
        groups.append(_group_region(cue_blocks[start_index:end_index]))
        start_index = end_index
    return groups


def _partition_lyric_run_normal(cue_blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep valid cue boundaries and split only genuinely long individual cues."""

    regions: list[dict[str, object]] = []
    short_run_start = 0
    for index, block in enumerate(cue_blocks):
        if _block_duration(block) <= MAX_SCENE_DURATION_MS:
            continue

        regions.extend(_partition_short_cue_run(cue_blocks[short_run_start:index]))
        regions.append(block)
        short_run_start = index + 1
    regions.extend(_partition_short_cue_run(cue_blocks[short_run_start:]))
    return regions


def _partition_lyric_run_with_rebalancing(cue_blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rescue an otherwise-unsatisfiable short cue by borrowing an adjacent cue edge."""

    short_index = next(
        (index for index, block in enumerate(cue_blocks) if _block_duration(block) < MIN_SCENE_DURATION_MS),
        None,
    )
    if short_index is None:
        raise SceneConstructionError("Lyric cue run cannot satisfy the 2–10 second scene policy.")

    short_block = cue_blocks[short_index]
    following_duration = _block_duration(short_block)
    for following_index in range(short_index + 1, len(cue_blocks)):
        following_block = cue_blocks[following_index]
        needed_ms = MIN_SCENE_DURATION_MS - following_duration
        if needed_ms <= 0:
            break
        remaining_ms = _block_duration(following_block) - needed_ms
        if remaining_ms >= MIN_SCENE_DURATION_MS:
            prefix = _partition_lyric_run(cue_blocks[:short_index])
            mixed_cues: list[dict[str, object]] = []
            for block in cue_blocks[short_index : following_index + 1]:
                mixed_cues.extend(block["cues"])
            mixed_end = int(following_block["start_ms"]) + needed_ms
            mixed_block = _new_block(
                int(short_block["start_ms"]),
                mixed_end,
                mixed_cues,
            )
            suffix_blocks = [
                _new_block(mixed_end, int(following_block["end_ms"]), list(following_block["cues"]))
            ]
            suffix_blocks.extend(cue_blocks[following_index + 1 :])
            suffix = _partition_lyric_run(suffix_blocks)
            return prefix + [mixed_block] + suffix
        following_duration += _block_duration(following_block)

    preceding_duration = _block_duration(short_block)
    for preceding_index in range(short_index - 1, -1, -1):
        preceding_block = cue_blocks[preceding_index]
        needed_ms = MIN_SCENE_DURATION_MS - preceding_duration
        if needed_ms <= 0:
            break
        remaining_ms = _block_duration(preceding_block) - needed_ms
        if remaining_ms >= MIN_SCENE_DURATION_MS:
            mixed_cues: list[dict[str, object]] = []
            for block in cue_blocks[preceding_index : short_index + 1]:
                mixed_cues.extend(block["cues"])
            mixed_start = int(preceding_block["end_ms"]) - needed_ms
            prefix_blocks = list(cue_blocks[:preceding_index])
            prefix_blocks.append(
                _new_block(
                    int(preceding_block["start_ms"]),
                    mixed_start,
                    list(preceding_block["cues"]),
                )
            )
            prefix = _partition_lyric_run(prefix_blocks)
            mixed_block = _new_block(
                mixed_start,
                int(short_block["end_ms"]),
                mixed_cues,
            )
            suffix = _partition_lyric_run(cue_blocks[short_index + 1 :])
            return prefix + [mixed_block] + suffix
        preceding_duration += _block_duration(preceding_block)

    raise SceneConstructionError("Lyric cue run cannot satisfy the 2–10 second scene policy.")


def _partition_lyric_run(cue_blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    try:
        return _partition_lyric_run_normal(cue_blocks)
    except SceneConstructionError:
        return _partition_lyric_run_with_rebalancing(cue_blocks)


def _build_atomic_blocks(normalized_cues: list[dict[str, object]], audio_duration_ms: int) -> list[dict[str, object]]:
    """Build cue-aware regions without merging adjacent lyric cue boundaries."""

    blocks: list[dict[str, object]] = []
    cursor = 0
    for cue in normalized_cues:
        cue_start = int(cue["start_ms"])
        cue_end = int(cue["end_ms"])
        gap_ms = cue_start - cursor
        lyric_start = cue_start

        if gap_ms > 0:
            if gap_ms < MIN_SCENE_DURATION_MS:
                if blocks and blocks[-1]["cues"]:
                    # Only the gap is absorbed; the next cue remains a separate region.
                    blocks[-1]["end_ms"] = cue_start
                else:
                    # A short opening gap belongs to the first lyric region.
                    lyric_start = 0
            else:
                blocks.append(_new_block(cursor, cue_start))

        blocks.append(_new_block(lyric_start, cue_end, [cue]))
        cursor = cue_end

    if cursor < audio_duration_ms:
        closing_gap_ms = audio_duration_ms - cursor
        if closing_gap_ms < MIN_SCENE_DURATION_MS and blocks and blocks[-1]["cues"]:
            # Only the gap is absorbed; the final cue remains its own region.
            blocks[-1]["end_ms"] = audio_duration_ms
        else:
            blocks.append(_new_block(cursor, audio_duration_ms))

    if not blocks:
        blocks.append(_new_block(0, audio_duration_ms))
    return blocks


def _lyric_text(cues: list[dict[str, object]]) -> str | None:
    if not cues:
        return None
    return "\n".join(str(cue["text"]) for cue in cues)


def build_scenes(cues: object, audio_duration_ms: int) -> list[dict[str, object]]:
    if isinstance(audio_duration_ms, bool) or not isinstance(audio_duration_ms, int) or audio_duration_ms <= 0:
        raise SceneConstructionError("Master-audio duration must be a positive integer.")
    if audio_duration_ms < MIN_SCENE_DURATION_MS:
        raise SceneConstructionError("Master audio is shorter than the 2 second scene minimum.")

    normalized_cues = _validate_cues(cues, audio_duration_ms)
    blocks = _build_atomic_blocks(normalized_cues, audio_duration_ms)
    _expand_short_lyric_runs(blocks)

    regions: list[dict[str, object]] = []
    index = 0
    while index < len(blocks):
        if not blocks[index]["cues"]:
            regions.append(blocks[index])
            index += 1
            continue
        start = index
        while index < len(blocks) and blocks[index]["cues"]:
            index += 1
        regions.extend(_partition_lyric_run(blocks[start:index]))

    scenes: list[dict[str, object]] = []
    for region in regions:
        region_cues = list(region["cues"])
        cue_numbers = [int(cue["cue_number"]) for cue in region_cues]
        source_kind = "lyric" if cue_numbers else "instrumental"
        lyric = _lyric_text(region_cues)
        for piece_start, piece_end, split_index, split_count in _split_range(
            int(region["start_ms"]), int(region["end_ms"])
        ):
            scenes.append(
                {
                    "scene_id": str(uuid.uuid4()),
                    "source_kind": source_kind,
                    "timeline_start_ms": piece_start,
                    "timeline_end_ms": piece_end,
                    "exact_duration_ms": piece_end - piece_start,
                    "lyric": lyric,
                    "source_cue_numbers": list(cue_numbers),
                    "split_index": split_index,
                    "split_count": split_count,
                }
            )

    validate_scene_list(scenes, audio_duration_ms)
    return scenes


def build_project_scenes(storage, project_id: object, parse_srt: Callable[[str], list[dict[str, object]]]) -> dict[str, object]:
    """Build and atomically persist a complete scene list for one project."""

    project = storage.load_project(project_id)
    source = project["source"]
    master_audio = source["master_audio"]
    lyrics_srt = source["lyrics_srt"]
    if master_audio is None or lyrics_srt is None:
        raise SceneConstructionError("Import master audio and an SRT file before building scenes.")

    project_directory: Path = storage.project_directory(project_id)
    audio_path = project_directory / "source" / master_audio["stored_name"]
    lyrics_path = project_directory / "source" / lyrics_srt["stored_name"]
    if not audio_path.is_file() or not lyrics_path.is_file():
        raise SceneConstructionError("Accepted source metadata does not match project files.")

    try:
        lyrics_text = lyrics_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise SceneConstructionError("Accepted lyrics SRT could not be read as UTF-8.") from error

    try:
        cues = parse_srt(lyrics_text)
    except Exception as error:
        if isinstance(error, SceneConstructionError):
            raise
        raise SceneConstructionError(str(error)) from error
    if len(cues) != lyrics_srt["cue_count"]:
        raise SceneConstructionError("Accepted lyrics metadata does not match the project SRT file.")

    scenes = build_scenes(cues, master_audio["duration_ms"])
    candidate = {**project, "scenes": scenes}
    return storage.save_project(project_id, candidate)
