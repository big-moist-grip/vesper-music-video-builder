import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.projects import ProjectStorage, ProjectValidationError, validate_project_document
from backend.scenes import (
    MAX_SCENE_DURATION_MS,
    MIN_SCENE_DURATION_MS,
    SceneConstructionError,
    SceneValidationError,
    build_project_scenes,
    build_scenes,
    validate_scene_list,
)
from backend.source import (
    AudioProbeError,
    parse_srt,
    probe_audio_duration_ms,
    import_lyrics_srt,
    import_master_audio,
    SrtValidationError,
)


class FakeUpload:
    def __init__(self, data: bytes, chunk_size: int = 5):
        self.data = data
        self.chunk_size = chunk_size
        self.position = 0

    async def read_chunk(self, size: int) -> bytes:
        if self.position >= len(self.data):
            return b""
        end = min(self.position + min(size, self.chunk_size), len(self.data))
        chunk = self.data[self.position:end]
        self.position = end
        return chunk


def run_async(coroutine):
    return asyncio.run(coroutine)


class Phase2TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def _source_document(self, project, duration_ms=20_001, cue_count=0, scenes=None):
        return {
            **project,
            "source": {
                "master_audio": {
                    "stored_name": "master_audio.wav",
                    "original_name": "master.wav",
                    "duration_ms": duration_ms,
                },
                "lyrics_srt": {
                    "stored_name": "lyrics.srt",
                    "original_name": "lyrics.srt",
                    "cue_count": cue_count,
                },
            },
            "scenes": [] if scenes is None else scenes,
        }


class ProjectSchemaV2Tests(Phase2TestCase):
    def test_legacy_v1_normalizes_without_mutating_file(self):
        project = self.storage.create_project("Legacy Project")
        project_file = self.projects_root / project["project_id"] / "project.json"
        legacy = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        project_file.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = self.storage.load_project(project["project_id"])

        self.assertEqual(loaded["schema_version"], 2)
        self.assertEqual(loaded["project_id"], legacy["project_id"])
        self.assertEqual(loaded["created_at"], legacy["created_at"])
        self.assertEqual(loaded["updated_at"], legacy["updated_at"])
        self.assertEqual(loaded["source"], {"master_audio": None, "lyrics_srt": None})
        self.assertEqual(loaded["scenes"], [])
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), legacy)

    def test_legacy_save_upgrades_to_v2(self):
        project = self.storage.create_project("Legacy Save")
        project_file = self.projects_root / project["project_id"] / "project.json"
        legacy = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        project_file.write_text(json.dumps(legacy), encoding="utf-8")

        saved = self.storage.save_project(project["project_id"], {**legacy, "name": "Upgraded"})

        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["name"], "Upgraded")
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8"))["schema_version"], 2)

    def test_invalid_v2_source_and_scene_data_is_rejected(self):
        project = self.storage.create_project("Invalid v2")
        valid = self._source_document(project, duration_ms=10_001)
        valid["scenes"] = build_scenes([], 10_001)
        self.assertEqual(validate_project_document(valid), valid)

        invalid_source = self._source_document(project, duration_ms=0)
        with self.assertRaises(ProjectValidationError):
            validate_project_document(invalid_source)

        duplicate_ids = [dict(valid["scenes"][0]), dict(valid["scenes"][1])]
        duplicate_ids[1]["scene_id"] = duplicate_ids[0]["scene_id"]
        with self.assertRaises(ProjectValidationError):
            validate_project_document({**valid, "scenes": duplicate_ids})

        non_contiguous = [dict(valid["scenes"][0]), dict(valid["scenes"][1])]
        non_contiguous[1]["timeline_start_ms"] += 1
        with self.assertRaises(ProjectValidationError):
            validate_project_document({**valid, "scenes": non_contiguous})

        with self.assertRaises(ProjectValidationError):
            validate_project_document({**project, "schema_version": 99})


class SrtParserTests(unittest.TestCase):
    def test_crlf_lf_bom_and_multiline_text(self):
        text = "\ufeff1\r\n00:00:01,500 --> 00:00:03,000\r\nFirst line\r\nSecond line\r\n\r\n2\n00:00:04,000 --> 00:00:05,000\nNext"

        cues = parse_srt(text)

        self.assertEqual(
            cues,
            [
                {"cue_number": 1, "start_ms": 1500, "end_ms": 3000, "text": "First line\nSecond line"},
                {"cue_number": 2, "start_ms": 4000, "end_ms": 5000, "text": "Next"},
            ],
        )

    def test_empty_srt_returns_zero_cues(self):
        self.assertEqual(parse_srt("\ufeff \r\n\r\n"), [])

    def test_malformed_srt_is_rejected(self):
        invalid_inputs = [
            "1\nnot a timestamp\nLyric",
            "1\n00:00:03,000 --> 00:00:02,000\nLyric",
            "1\n00:00:00,000 --> 00:00:01,000\n   ",
            "2\n00:00:02,000 --> 00:00:03,000\nLater\n\n1\n00:00:01,000 --> 00:00:02,000\nEarlier",
            "1\n00:00:00,000 --> 00:00:02,000\nOverlap\n\n2\n00:00:01,000 --> 00:00:03,000\nOverlap",
        ]
        for invalid in invalid_inputs:
            with self.subTest(invalid=invalid):
                with self.assertRaises(SrtValidationError):
                    parse_srt(invalid)


class SceneConstructionTests(unittest.TestCase):
    def assert_valid_timeline(self, scenes, duration_ms):
        self.assertTrue(scenes)
        self.assertEqual(scenes[0]["timeline_start_ms"], 0)
        self.assertEqual(scenes[-1]["timeline_end_ms"], duration_ms)
        self.assertEqual(len({scene["scene_id"] for scene in scenes}), len(scenes))
        self.assertTrue(
            all(
                MIN_SCENE_DURATION_MS <= scene["exact_duration_ms"] <= MAX_SCENE_DURATION_MS
                for scene in scenes
            )
        )
        for previous, current in zip(scenes, scenes[1:]):
            self.assertEqual(previous["timeline_end_ms"], current["timeline_start_ms"])
        validate_scene_list(scenes, duration_ms)

    def test_equalised_splits_at_required_boundaries(self):
        for duration_ms, expected_count, expected_durations in (
            (10_000, 1, [10_000]),
            (10_001, 2, [5_001, 5_000]),
            (20_001, 3, [6_667, 6_667, 6_667]),
            (21_000, 3, [7_000, 7_000, 7_000]),
        ):
            with self.subTest(duration_ms=duration_ms):
                scenes = build_scenes([], duration_ms)
                durations = [scene["exact_duration_ms"] for scene in scenes]
                self.assertEqual(len(scenes), expected_count)
                self.assertEqual(durations, expected_durations)
                self.assertEqual(sum(durations), duration_ms)
                self.assertGreaterEqual(min(durations), MIN_SCENE_DURATION_MS)
                self.assertLessEqual(max(durations), MAX_SCENE_DURATION_MS)
                self.assertLessEqual(max(durations) - min(durations), 1)
                self.assertTrue(all(scene["source_cue_numbers"] == [] for scene in scenes))
                self.assert_valid_timeline(scenes, duration_ms)

    def test_audio_shorter_than_generation_minimum_is_rejected(self):
        with self.assertRaisesRegex(SceneConstructionError, "2 second"):
            build_scenes([], MIN_SCENE_DURATION_MS - 1)

    def test_micro_gaps_are_absorbed_and_exact_boundary_gaps_remain(self):
        micro_gap_scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 6_655, "text": "Cue A"},
                {"cue_number": 2, "start_ms": 6_679, "end_ms": 9_386, "text": "Cue B"},
            ],
            12_000,
        )
        self.assertFalse(
            any(
                scene["timeline_start_ms"] == 6_655 and scene["timeline_end_ms"] == 6_679
                for scene in micro_gap_scenes
            )
        )
        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in micro_gap_scenes
                if scene["source_kind"] == "lyric"
            ],
            [
                (0, 6_679, [1], "Cue A"),
                (6_679, 9_386, [2], "Cue B"),
            ],
        )
        self.assert_valid_timeline(micro_gap_scenes, 12_000)

        short_gap_scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 3_000, "text": "Cue A"},
                {"cue_number": 2, "start_ms": 4_999, "end_ms": 8_000, "text": "Cue B"},
            ],
            10_000,
        )
        self.assertFalse(any(scene["source_kind"] == "instrumental" for scene in short_gap_scenes[:-1]))
        self.assertEqual(
            [
                (scene["timeline_start_ms"], scene["timeline_end_ms"], scene["source_cue_numbers"])
                for scene in short_gap_scenes
                if scene["source_kind"] == "lyric"
            ],
            [(0, 4_999, [1]), (4_999, 8_000, [2])],
        )
        self.assert_valid_timeline(short_gap_scenes, 10_000)

        exact_gap_scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 3_000, "text": "Cue A"},
                {"cue_number": 2, "start_ms": 5_000, "end_ms": 8_000, "text": "Cue B"},
            ],
            10_000,
        )
        self.assertIn(
            (3_000, 5_000),
            {(scene["timeline_start_ms"], scene["timeline_end_ms"]) for scene in exact_gap_scenes},
        )
        exact_gap = next(
            scene
            for scene in exact_gap_scenes
            if scene["timeline_start_ms"] == 3_000 and scene["timeline_end_ms"] == 5_000
        )
        self.assertEqual(exact_gap["source_kind"], "instrumental")
        self.assertEqual(exact_gap["exact_duration_ms"], MIN_SCENE_DURATION_MS)
        self.assert_valid_timeline(exact_gap_scenes, 10_000)

    def test_valid_cues_separated_by_micro_gaps_remain_distinct(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "One"},
                {"cue_number": 2, "start_ms": 4_024, "end_ms": 8_000, "text": "Two"},
                {"cue_number": 3, "start_ms": 8_024, "end_ms": 12_000, "text": "Three"},
            ],
            12_000,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in scenes
            ],
            [
                (0, 4_024, [1], "One"),
                (4_024, 8_024, [2], "Two"),
                (8_024, 12_000, [3], "Three"),
            ],
        )
        self.assert_valid_timeline(scenes, 12_000)

    def test_many_valid_micro_gapped_cues_remain_one_scene_per_cue(self):
        cues = []
        cursor = 0
        for cue_number in range(1, 11):
            end_ms = cursor + 3_000
            cues.append(
                {
                    "cue_number": cue_number,
                    "start_ms": cursor,
                    "end_ms": end_ms,
                    "text": f"Cue {cue_number}",
                }
            )
            cursor = end_ms + 24
        audio_duration_ms = cues[-1]["end_ms"] + MIN_SCENE_DURATION_MS

        scenes = build_scenes(cues, audio_duration_ms)
        lyric_scenes = [scene for scene in scenes if scene["source_kind"] == "lyric"]

        self.assertEqual(len(lyric_scenes), len(cues))
        self.assertEqual(
            [scene["source_cue_numbers"] for scene in lyric_scenes],
            [[cue_number] for cue_number in range(1, 11)],
        )
        self.assertEqual(
            [scene["lyric"] for scene in lyric_scenes],
            [f"Cue {cue_number}" for cue_number in range(1, 11)],
        )
        self.assertTrue(all(scene["split_index"] == 1 and scene["split_count"] == 1 for scene in lyric_scenes))
        self.assert_valid_timeline(scenes, audio_duration_ms)

    def test_short_cue_merges_only_when_required(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "Short"},
                {"cue_number": 2, "start_ms": 1_200, "end_ms": 4_200, "text": "Valid"},
            ],
            4_200,
        )

        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["source_cue_numbers"], [1, 2])
        self.assertEqual(scenes[0]["lyric"], "Short\nValid")
        self.assertEqual(scenes[0]["exact_duration_ms"], 4_200)
        self.assert_valid_timeline(scenes, 4_200)

    def test_short_cue_before_exact_maximum_cue_rebalances_inside_following_cue(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "Short"},
                {"cue_number": 2, "start_ms": 1_200, "end_ms": 11_200, "text": "Ten second cue"},
            ],
            11_200,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in scenes
            ],
            [
                (0, 2_000, [1, 2], "Short\nTen second cue"),
                (2_000, 11_200, [2], "Ten second cue"),
            ],
        )
        self.assert_valid_timeline(scenes, 11_200)

    def test_exact_maximum_cue_before_short_cue_rebalances_inside_preceding_cue(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 10_000, "text": "Ten second cue"},
                {"cue_number": 2, "start_ms": 10_000, "end_ms": 11_200, "text": "Short"},
            ],
            11_200,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in scenes
            ],
            [
                (0, 9_200, [1], "Ten second cue"),
                (9_200, 11_200, [1, 2], "Ten second cue\nShort"),
            ],
        )
        self.assert_valid_timeline(scenes, 11_200)

    def test_short_cue_before_long_cue_rebalances_before_long_split(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "Short"},
                {"cue_number": 2, "start_ms": 1_200, "end_ms": 13_200, "text": "Long"},
            ],
            13_200,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in scenes
            ],
            [
                (0, 2_000, [1, 2], "Short\nLong"),
                (2_000, 7_600, [2], "Long"),
                (7_600, 13_200, [2], "Long"),
            ],
        )
        self.assert_valid_timeline(scenes, 13_200)

    def test_long_cue_before_short_cue_rebalances_after_long_split(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 12_000, "text": "Long"},
                {"cue_number": 2, "start_ms": 12_000, "end_ms": 13_200, "text": "Short"},
            ],
            13_200,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                )
                for scene in scenes
            ],
            [
                (0, 5_600, [1], "Long"),
                (5_600, 11_200, [1], "Long"),
                (11_200, 13_200, [1, 2], "Long\nShort"),
            ],
        )
        self.assert_valid_timeline(scenes, 13_200)

    def test_78_cue_micro_gapped_smoke_preserves_cue_local_scenes(self):
        cues = []
        cursor = 0
        for cue_number in range(1, 79):
            end_ms = cursor + 3_000
            cues.append(
                {
                    "cue_number": cue_number,
                    "start_ms": cursor,
                    "end_ms": end_ms,
                    "text": f"Cue {cue_number}",
                }
            )
            cursor = end_ms + 24
        audio_duration_ms = cues[-1]["end_ms"]

        scenes = build_scenes(cues, audio_duration_ms)
        lyric_scenes = [scene for scene in scenes if scene["source_kind"] == "lyric"]

        self.assertEqual(len(lyric_scenes), 78)
        self.assertEqual(
            [scene["source_cue_numbers"] for scene in lyric_scenes],
            [[cue_number] for cue_number in range(1, 79)],
        )
        self.assertEqual(
            [scene["lyric"] for scene in lyric_scenes],
            [f"Cue {cue_number}" for cue_number in range(1, 79)],
        )
        self.assertTrue(all(scene["split_index"] == 1 and scene["split_count"] == 1 for scene in lyric_scenes))
        self.assert_valid_timeline(scenes, audio_duration_ms)

    def test_valid_adjacent_cues_are_not_greedily_merged(self):
        scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 3_000, "text": "Three"},
                {"cue_number": 2, "start_ms": 3_000, "end_ms": 7_000, "text": "Four"},
                {"cue_number": 3, "start_ms": 7_000, "end_ms": 12_000, "text": "Five"},
            ],
            12_000,
        )

        self.assertEqual(
            [(scene["exact_duration_ms"], scene["source_cue_numbers"]) for scene in scenes],
            [(3_000, [1]), (4_000, [2]), (5_000, [3])],
        )
        self.assert_valid_timeline(scenes, 12_000)

    def test_long_single_cue_splits_with_narrow_provenance(self):
        scenes = build_scenes(
            [{"cue_number": 7, "start_ms": 0, "end_ms": 21_000, "text": "Long cue"}],
            21_000,
        )

        self.assertEqual(
            [
                (
                    scene["timeline_start_ms"],
                    scene["timeline_end_ms"],
                    scene["source_cue_numbers"],
                    scene["lyric"],
                    scene["split_index"],
                    scene["split_count"],
                )
                for scene in scenes
            ],
            [
                (0, 7_000, [7], "Long cue", 1, 3),
                (7_000, 14_000, [7], "Long cue", 2, 3),
                (14_000, 21_000, [7], "Long cue", 3, 3),
            ],
        )
        self.assert_valid_timeline(scenes, 21_000)

    def test_provenance_is_limited_to_the_cues_in_each_group(self):
        cues = [
            {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "One"},
            {"cue_number": 2, "start_ms": 1_200, "end_ms": 4_200, "text": "Two"},
            {"cue_number": 3, "start_ms": 4_200, "end_ms": 8_200, "text": "Three"},
            {"cue_number": 4, "start_ms": 10_200, "end_ms": 11_400, "text": "Four"},
            {"cue_number": 5, "start_ms": 11_400, "end_ms": 14_400, "text": "Five"},
        ]
        scenes = build_scenes(cues, 16_400)
        lyric_scenes = [scene for scene in scenes if scene["source_kind"] == "lyric"]

        self.assertEqual(
            [scene["source_cue_numbers"] for scene in lyric_scenes],
            [[1, 2], [3], [4, 5]],
        )
        self.assertEqual(
            [scene["lyric"] for scene in lyric_scenes],
            ["One\nTwo", "Three", "Four\nFive"],
        )
        self.assertTrue(all(scene["source_cue_numbers"] != [1, 2, 3, 4, 5] for scene in scenes))
        self.assert_valid_timeline(scenes, 16_400)

    def test_full_timeline_constraints_and_ordered_provenance(self):
        cues = [
            {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "One"},
            {"cue_number": 2, "start_ms": 1_200, "end_ms": 4_200, "text": "Two"},
            {"cue_number": 3, "start_ms": 4_224, "end_ms": 8_224, "text": "Three"},
            {"cue_number": 4, "start_ms": 10_224, "end_ms": 13_224, "text": "Four"},
        ]
        audio_duration_ms = 15_224

        scenes = build_scenes(cues, audio_duration_ms)
        self.assertEqual(scenes[0]["timeline_start_ms"], 0)
        self.assertEqual(scenes[-1]["timeline_end_ms"], audio_duration_ms)
        self.assertEqual(
            [cue_number for scene in scenes if scene["source_kind"] == "lyric" for cue_number in scene["source_cue_numbers"]],
            [1, 2, 3, 4],
        )
        self.assertTrue(
            all(
                MIN_SCENE_DURATION_MS <= scene["exact_duration_ms"] <= MAX_SCENE_DURATION_MS
                for scene in scenes
            )
        )
        self.assertTrue(
            all(
                previous["timeline_end_ms"] == current["timeline_start_ms"]
                for previous, current in zip(scenes, scenes[1:])
            )
        )
        self.assert_valid_timeline(scenes, audio_duration_ms)

    def test_opening_and_closing_gap_boundaries(self):
        opening_absorbed = build_scenes(
            [{"cue_number": 1, "start_ms": 1_999, "end_ms": 5_000, "text": "Opening"}],
            7_000,
        )
        self.assertEqual(opening_absorbed[0]["source_kind"], "lyric")
        self.assertEqual(opening_absorbed[0]["timeline_start_ms"], 0)
        self.assert_valid_timeline(opening_absorbed, 7_000)

        opening_preserved = build_scenes(
            [{"cue_number": 1, "start_ms": 2_000, "end_ms": 5_000, "text": "Opening"}],
            7_000,
        )
        self.assertEqual(opening_preserved[0]["source_kind"], "instrumental")
        self.assertEqual(opening_preserved[0]["exact_duration_ms"], MIN_SCENE_DURATION_MS)
        self.assert_valid_timeline(opening_preserved, 7_000)

        closing_absorbed = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 5_001, "text": "Closing"}],
            7_000,
        )
        self.assertEqual(closing_absorbed[-1]["source_kind"], "lyric")
        self.assertEqual(closing_absorbed[-1]["timeline_end_ms"], 7_000)
        self.assert_valid_timeline(closing_absorbed, 7_000)

        closing_preserved = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 5_000, "text": "Closing"}],
            7_000,
        )
        self.assertEqual(closing_preserved[-1]["source_kind"], "instrumental")
        self.assertEqual(closing_preserved[-1]["exact_duration_ms"], MIN_SCENE_DURATION_MS)
        self.assert_valid_timeline(closing_preserved, 7_000)

    def test_short_lyric_material_is_coalesced_with_ordered_text(self):
        one_short_cue = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 1_200, "text": "Short cue"},
                {"cue_number": 2, "start_ms": 1_200, "end_ms": 5_000, "text": "Adjacent lyric"},
            ],
            7_000,
        )
        lyric_scene = next(scene for scene in one_short_cue if scene["source_kind"] == "lyric")
        self.assertEqual(lyric_scene["source_cue_numbers"], [1, 2])
        self.assertEqual(lyric_scene["lyric"], "Short cue\nAdjacent lyric")
        self.assertGreaterEqual(lyric_scene["exact_duration_ms"], MIN_SCENE_DURATION_MS)
        self.assert_valid_timeline(one_short_cue, 7_000)

        several_short_cues = build_scenes(
            [
                {"cue_number": 10, "start_ms": 0, "end_ms": 1_200, "text": "One"},
                {"cue_number": 11, "start_ms": 1_300, "end_ms": 2_400, "text": "Two"},
                {"cue_number": 12, "start_ms": 2_500, "end_ms": 3_600, "text": "Three"},
            ],
            7_000,
        )
        lyric_scenes = [scene for scene in several_short_cues if scene["source_kind"] == "lyric"]
        self.assertEqual(len(lyric_scenes), 1)
        self.assertEqual(lyric_scenes[0]["source_cue_numbers"], [10, 11, 12])
        self.assertEqual(lyric_scenes[0]["lyric"], "One\nTwo\nThree")
        self.assert_valid_timeline(several_short_cues, 7_000)

    def test_current_scene_schema_requires_plural_provenance(self):
        scenes = build_scenes(
            [{"cue_number": 12, "start_ms": 0, "end_ms": 3_000, "text": "Lyric"}],
            5_000,
        )
        lyric_scene = next(scene for scene in scenes if scene["source_kind"] == "lyric")
        instrumental_scene = next(scene for scene in scenes if scene["source_kind"] == "instrumental")
        self.assertEqual(lyric_scene["source_cue_numbers"], [12])
        self.assertEqual(instrumental_scene["source_cue_numbers"], [])
        validate_scene_list(scenes, 5_000)

        singular = dict(lyric_scene)
        singular.pop("source_cue_numbers")
        singular["source_cue_number"] = 12
        with self.assertRaises(SceneValidationError):
            validate_scene_list([singular, instrumental_scene], 5_000)

        missing_provenance = dict(lyric_scene)
        missing_provenance["source_cue_numbers"] = []
        with self.assertRaises(SceneValidationError):
            validate_scene_list([missing_provenance, instrumental_scene], 5_000)

        duplicate_provenance = dict(lyric_scene)
        duplicate_provenance["source_cue_numbers"] = [12, 12]
        with self.assertRaises(SceneValidationError):
            validate_scene_list([duplicate_provenance, instrumental_scene], 5_000)

        malformed_provenance = dict(lyric_scene)
        malformed_provenance["source_cue_numbers"] = ["12"]
        with self.assertRaises(SceneValidationError):
            validate_scene_list([malformed_provenance, instrumental_scene], 5_000)

        bad_instrumental = dict(instrumental_scene)
        bad_instrumental["source_cue_numbers"] = [12]
        with self.assertRaises(SceneValidationError):
            validate_scene_list([lyric_scene, bad_instrumental], 5_000)

        self.assertNotIn("source_cue_number", lyric_scene)

    def test_full_timeline_has_lyric_and_instrumental_segments(self):
        cues = [
            {"cue_number": 1, "start_ms": 2_000, "end_ms": 4_000, "text": "Short lyric"},
            {"cue_number": 2, "start_ms": 15_000, "end_ms": 27_000, "text": "Long lyric"},
        ]

        scenes = build_scenes(cues, 40_000)

        self.assertEqual(scenes[0]["source_kind"], "instrumental")
        first_lyric = next(scene for scene in scenes if scene["source_kind"] == "lyric")
        self.assertEqual(first_lyric["source_cue_numbers"], [1])
        self.assertEqual(first_lyric["lyric"], "Short lyric")
        long_lyric_scenes = [scene for scene in scenes if scene["source_cue_numbers"] == [2]]
        self.assertGreater(len(long_lyric_scenes), 1)
        self.assertTrue(all(scene["lyric"] == "Long lyric" for scene in long_lyric_scenes))
        self.assertEqual(scenes[-1]["source_kind"], "instrumental")
        self.assert_valid_timeline(scenes, 40_000)

    def test_fully_instrumental_audio_is_split_evenly(self):
        scenes = build_scenes([], 20_001)

        self.assertEqual(len(scenes), 3)
        self.assertTrue(all(scene["source_kind"] == "instrumental" for scene in scenes))
        self.assertTrue(all(scene["lyric"] is None for scene in scenes))
        self.assertTrue(all(scene["source_cue_numbers"] == [] for scene in scenes))
        self.assertEqual(sum(scene["exact_duration_ms"] for scene in scenes), 20_001)
        self.assert_valid_timeline(scenes, 20_001)

    def test_invalid_timeline_is_rejected_before_scene_replacement(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        storage = ProjectStorage(Path(temporary_directory.name) / "projects")
        project = storage.create_project("Scene failure")
        project = storage.save_project(
            project["project_id"],
            self._project_with_sources(project, storage, "1\n00:00:00,000 --> 00:00:01,000\nValid"),
        )
        prior_scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 1_000, "text": "Valid"}],
            5_000,
        )
        project = storage.save_project(project["project_id"], {**project, "scenes": prior_scenes})
        lyrics_path = storage.project_directory(project["project_id"]) / "source" / "lyrics.srt"
        lyrics_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nValid\n\n2\n00:00:00,500 --> 00:00:02,000\nOverlap",
            encoding="utf-8",
        )

        with self.assertRaises(SceneConstructionError):
            build_project_scenes(storage, project["project_id"], parse_srt)

        with self.assertRaises(SceneConstructionError):
            build_scenes(
                [{"cue_number": 1, "start_ms": 0, "end_ms": 5_001, "text": "Beyond audio"}],
                5_000,
            )

        self.assertEqual(storage.load_project(project["project_id"])["scenes"], prior_scenes)

    @staticmethod
    def _project_with_sources(project, storage, srt_text):
        project_directory = storage.project_directory(project["project_id"])
        (project_directory / "source" / "master_audio.wav").write_bytes(b"audio")
        (project_directory / "source" / "lyrics.srt").write_text(srt_text, encoding="utf-8")
        return {
            **project,
            "source": {
                "master_audio": {
                    "stored_name": "master_audio.wav",
                    "original_name": "master.wav",
                    "duration_ms": 5_000,
                },
                "lyrics_srt": {
                    "stored_name": "lyrics.srt",
                    "original_name": "lyrics.srt",
                    "cue_count": 1,
                },
            },
            "scenes": [],
        }


class MediaProbeTests(unittest.TestCase):
    def test_probe_uses_argument_array_and_converts_seconds_to_milliseconds(self):
        calls = []

        def fake_runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(returncode=0, stdout="12.3456\n", stderr="")

        duration_ms = probe_audio_duration_ms("candidate.wav", ffprobe_path="ffprobe.exe", runner=fake_runner)

        self.assertEqual(duration_ms, 12_346)
        self.assertEqual(calls[0][0][-1], "candidate.wav")
        self.assertNotIn("shell", calls[0][1])

    def test_probe_rejects_invalid_output(self):
        def fake_runner(arguments, **kwargs):
            return SimpleNamespace(returncode=0, stdout="NaN\n", stderr="")

        with self.assertRaises(AudioProbeError):
            probe_audio_duration_ms("candidate.wav", ffprobe_path="ffprobe.exe", runner=fake_runner)


class SourceImportTests(Phase2TestCase):
    def test_source_replacement_clears_scenes_and_failed_candidates_preserve_state(self):
        project = self.storage.create_project("Source replacement")
        project = run_async(
            import_master_audio(
                self.storage,
                project["project_id"],
                "original.WAV",
                FakeUpload(b"first audio"),
                probe_duration=lambda path: 5_000,
            )
        )
        project = run_async(
            import_lyrics_srt(
                self.storage,
                project["project_id"],
                "lyrics-original.srt",
                FakeUpload(b"1\n00:00:00,000 --> 00:00:01,000\nFirst"),
            )
        )
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 1_000, "text": "First"}],
            5_000,
        )
        project = self.storage.save_project(project["project_id"], {**project, "scenes": scenes})
        old_audio_bytes = (self.projects_root / project["project_id"] / "source" / "master_audio.wav").read_bytes()
        old_srt_bytes = (self.projects_root / project["project_id"] / "source" / "lyrics.srt").read_bytes()

        replaced_audio = run_async(
            import_master_audio(
                self.storage,
                project["project_id"],
                "new.mp3",
                FakeUpload(b"second audio"),
                probe_duration=lambda path: 6_000,
            )
        )
        self.assertEqual(replaced_audio["source"]["master_audio"]["stored_name"], "master_audio.mp3")
        self.assertEqual(replaced_audio["scenes"], [])
        self.assertFalse((self.projects_root / project["project_id"] / "source" / "master_audio.wav").exists())

        self.storage.save_project(
            project["project_id"],
            {
                **replaced_audio,
                "scenes": build_scenes(
                    [{"cue_number": 1, "start_ms": 0, "end_ms": 1_000, "text": "First"}],
                    6_000,
                ),
            },
        )
        replaced_srt = run_async(
            import_lyrics_srt(
                self.storage,
                project["project_id"],
                "lyrics-replacement.srt",
                FakeUpload(b"1\n00:00:00,000 --> 00:00:01,000\nReplacement"),
            )
        )
        self.assertEqual(replaced_srt["source"]["lyrics_srt"]["original_name"], "lyrics-replacement.srt")
        self.assertEqual(replaced_srt["scenes"], [])
        old_srt_bytes = (self.projects_root / project["project_id"] / "source" / "lyrics.srt").read_bytes()

        with self.assertRaises(SrtValidationError):
            run_async(
                import_lyrics_srt(
                    self.storage,
                    project["project_id"],
                    "bad.srt",
                    FakeUpload(b"not an srt"),
                )
            )
        after_bad_srt = self.storage.load_project(project["project_id"])
        self.assertEqual(after_bad_srt["source"]["lyrics_srt"]["original_name"], "lyrics-replacement.srt")
        self.assertEqual(
            (self.projects_root / project["project_id"] / "source" / "lyrics.srt").read_bytes(),
            old_srt_bytes,
        )

        with self.assertRaises(AudioProbeError):
            run_async(
                import_master_audio(
                    self.storage,
                    project["project_id"],
                    "failed.flac",
                    FakeUpload(b"failed audio"),
                    probe_duration=lambda path: (_ for _ in ()).throw(AudioProbeError("bad audio")),
                )
            )
        after_bad_audio = self.storage.load_project(project["project_id"])
        self.assertEqual(after_bad_audio["source"]["master_audio"]["stored_name"], "master_audio.mp3")
        self.assertEqual(
            (self.projects_root / project["project_id"] / "source" / "master_audio.mp3").read_bytes(),
            b"second audio",
        )
        self.assertNotEqual(old_audio_bytes, b"second audio")


if __name__ == "__main__":
    unittest.main()
