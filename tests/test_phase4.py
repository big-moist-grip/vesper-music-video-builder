import json
import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.entities import (
    create_character,
    create_location,
    delete_character,
    delete_location,
    remove_reference,
    update_character,
    update_location,
)
from backend.projects import ProjectStorage, ProjectValidationError, default_prompts_for_scenes, validate_project_document
from backend.scenes import build_project_scenes, build_scenes
from backend.storyboard import (
    STORYBOARD_MODE_GUIDANCE,
    STORYBOARD_MODES,
    build_storyboard_request,
    empty_story_direction,
    empty_storyboard,
    request_fingerprint,
    validate_storyboard_response,
)
from backend.source import AudioProbeError, import_master_audio
from backend.visuals import default_visuals_for_scenes


class FakeUpload:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    async def read_chunk(self, _size: int) -> bytes:
        if self.position >= len(self.data):
            return b""
        chunk = self.data[self.position:]
        self.position = len(self.data)
        return chunk


class Phase4TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def _project_with_scene(self):
        project = self.storage.create_project("Storyboard Project")
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "A lyric"}],
            4_000,
        )
        return self.storage.save_project(
            project["project_id"],
            {
                **project,
                "source": {
                    "master_audio": {
                        "stored_name": "master_audio.wav",
                        "original_name": "master.wav",
                        "duration_ms": 4_000,
                    },
                    "lyrics_srt": {
                        "stored_name": "lyrics.srt",
                        "original_name": "lyrics.srt",
                        "cue_count": 1,
                    },
                },
                "scenes": scenes,
                "visuals": default_visuals_for_scenes(scenes),
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )

    def _reference(self, reference_id=None, extension=".png", original_name="ref.png"):
        reference_id = reference_id or str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}{extension}",
            "original_name": original_name,
        }

    def _project_with_resources(self):
        project = self._project_with_scene()
        project = create_character(
            self.storage,
            project["project_id"],
            {
                "name": "Vesper",
                "role": "performer",
                "appearance": "Copper hair.",
                "outfit": "Black jacket.",
            },
        )
        project = create_location(
            self.storage,
            project["project_id"],
            {"name": "Warehouse", "description": "Industrial interior."},
        )
        character_reference = self._reference(original_name="vesper.png")
        character_reference_extra = self._reference(original_name="vesper-extra.png")
        location_reference = self._reference(original_name="warehouse.png")
        location_reference_extra = self._reference(original_name="warehouse-extra.png")
        character = {
            **project["characters"][0],
            "references": [character_reference, character_reference_extra],
        }
        location = {
            **project["locations"][0],
            "references": [location_reference, location_reference_extra],
        }
        project = self.storage.save_project(
            project["project_id"],
            {**project, "characters": [character], "locations": [location]},
        )
        return project

    def _response(self, project, fingerprint=None, character_ids=None, location_id=None, references=None):
        response_scenes = []
        for scene in project["scenes"]:
            response_scenes.append(
                {
                    "scene_id": scene["scene_id"],
                    "scene_type": "performance",
                    "character_ids": character_ids or [],
                    "location_id": location_id,
                    "action": "Performer crosses the warehouse floor.",
                    "visual_instructions": "Warm practical light and restrained handheld framing.",
                    "camera_direction": "Slow lateral tracking shot.",
                    "motion_direction": "Measured forward movement.",
                    "continuity_notes": "Keep the jacket silhouette consistent.",
                    "required_references": references or [],
                }
            )
        return {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": fingerprint or request_fingerprint(project),
            "scenes": response_scenes,
        }

    def _apply(self, project, response):
        storyboard = validate_storyboard_response(project, response)
        return self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )

    def test_new_project_and_legacy_documents_normalize_to_v4(self):
        project = self.storage.create_project("Schema v4")
        self.assertEqual(project["schema_version"], 7)
        self.assertEqual(project["story_direction"], empty_story_direction())
        self.assertEqual(project["storyboard"], empty_storyboard())

        v1 = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        v2 = {
            **v1,
            "schema_version": 2,
            "source": {"master_audio": None, "lyrics_srt": None},
            "scenes": [],
        }
        v3 = {**project, "schema_version": 3}
        v3.pop("story_direction")
        v3.pop("storyboard")
        v3.pop("visuals")
        v3.pop("prompts")

        for legacy in (v1, v2, v3):
            normalized = validate_project_document(legacy)
            self.assertEqual(normalized["schema_version"], 7)
            self.assertEqual(normalized["story_direction"], empty_story_direction())
            self.assertEqual(normalized["storyboard"], empty_storyboard())

    def test_provisional_v4_story_direction_normalizes_without_load_mutation(self):
        project = self.storage.create_project("Provisional v4")
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "loose",
                    "story_brief": "A provisional brief.",
                    "visual_notes": "A provisional visual language.",
                },
            },
        )
        project_file = self.projects_root / project["project_id"] / "project.json"
        provisional = {
            **project,
            "schema_version": 4,
            "story_direction": {
                "story_brief": "A provisional brief.",
                "visual_notes": "A provisional visual language.",
            },
        }
        provisional.pop("visuals")
        provisional.pop("prompts")
        provisional_bytes = (json.dumps(provisional, indent=2) + "\n").encode("utf-8")
        project_file.write_bytes(provisional_bytes)

        loaded = self.storage.load_project(project["project_id"])
        self.assertEqual(
            loaded["story_direction"],
            {
                "storyboard_mode": "loose",
                "story_brief": "A provisional brief.",
                "visual_notes": "A provisional visual language.",
            },
        )
        listing = self.storage.list_projects()
        self.assertEqual([item["project_id"] for item in listing["projects"]], [project["project_id"]])
        self.assertEqual(project_file.read_bytes(), provisional_bytes)

        saved = self.storage.save_project(project["project_id"], provisional)
        self.assertEqual(saved["story_direction"]["storyboard_mode"], "loose")
        self.assertEqual(
            json.loads(project_file.read_text(encoding="utf-8"))["story_direction"],
            saved["story_direction"],
        )

    def test_legacy_file_is_not_mutated_by_load(self):
        project = self.storage.create_project("Legacy file")
        project_file = self.projects_root / project["project_id"] / "project.json"
        legacy = {
            "schema_version": 3,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "source": {"master_audio": None, "lyrics_srt": None},
            "scenes": [],
            "characters": [],
            "locations": [],
        }
        project_file.write_text(json.dumps(legacy), encoding="utf-8")
        self.storage.load_project(project["project_id"])
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), legacy)

    def test_story_direction_and_storyboard_shape_are_strict(self):
        project = self.storage.create_project("Shape")
        invalid_documents = (
            {**project, "story_direction": {"storyboard_mode": "loose", "story_brief": "only"}},
            {**project, "story_direction": {"storyboard_mode": "loose", "story_brief": "", "visual_notes": "", "mood": "x"}},
            {**project, "story_direction": {"storyboard_mode": "invalid", "story_brief": "", "visual_notes": ""}},
            {**project, "story_direction": {"storyboard_mode": "loose", "story_brief": 1, "visual_notes": ""}},
            {**project, "storyboard": {"request_fingerprint": "a" * 64, "scenes": []}},
            {**project, "storyboard": {"request_fingerprint": None, "scenes": [{}]}},
        )
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_project_document(invalid)

    def test_storyboard_modes_are_valid_and_current_shape_is_exact(self):
        project = self._project_with_scene()
        for mode in STORYBOARD_MODES:
            with self.subTest(mode=mode):
                saved = self.storage.save_project(
                    project["project_id"],
                    {
                        **project,
                        "story_direction": {
                            "storyboard_mode": mode,
                            "story_brief": "",
                            "visual_notes": "",
                        },
                    },
                )
                self.assertEqual(saved["story_direction"]["storyboard_mode"], mode)

    def test_request_is_deterministic_and_contains_only_relay_data(self):
        project = self._project_with_resources()
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "loose",
                    "story_brief": "A performer finds courage.",
                    "visual_notes": "Obsidian spaces and antique gold highlights.",
                },
            },
        )
        first = build_storyboard_request(project)
        second = build_storyboard_request(self.storage.load_project(project["project_id"]))
        self.assertEqual(first, second)
        self.assertEqual(first["request_fingerprint"], request_fingerprint(project))
        self.assertEqual(
            first["request_fingerprint"],
            request_fingerprint({**project, "updated_at": "2099-01-01T00:00:00Z"}),
        )
        serialized = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertNotIn("stored_name", serialized)
        self.assertNotIn("generation_method", serialized)
        self.assertNotIn(str(self.projects_root), serialized)
        self.assertIn("response_contract", first)
        self.assertEqual(first["story_direction"]["storyboard_mode"], "loose")
        self.assertEqual(first["response_contract"]["storyboard_mode"], "loose")
        self.assertEqual(first["response_contract"]["mode_guidance"], STORYBOARD_MODE_GUIDANCE["loose"])
        self.assertEqual(len(first["scenes"]), len(project["scenes"]))
        self.assertEqual(first["scenes"][0]["scene_id"], project["scenes"][0]["scene_id"])

        changed_documents = [
            {
                "storyboard_mode": "loose",
                "story_brief": "A different story.",
                "visual_notes": project["story_direction"]["visual_notes"],
            },
            {
                "storyboard_mode": "loose",
                "story_brief": project["story_direction"]["story_brief"],
                "visual_notes": "A different look.",
            },
        ]
        character = project["characters"][0]
        changed_documents.append(
            {
                "storyboard_mode": "loose",
                "story_brief": project["story_direction"]["story_brief"],
                "visual_notes": project["story_direction"]["visual_notes"],
                "characters": [{**character, "appearance": "A changed appearance."}],
            }
        )
        for changed in changed_documents[:2]:
            changed_project = self.storage.save_project(
                project["project_id"],
                {**project, "story_direction": changed},
            )
            self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_project))

        for mode in ("strict", "band_performance"):
            changed_project = self.storage.save_project(
                project["project_id"],
                {
                    **project,
                    "story_direction": {
                        "storyboard_mode": mode,
                        "story_brief": project["story_direction"]["story_brief"],
                        "visual_notes": project["story_direction"]["visual_notes"],
                    },
                },
            )
            self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_project))

        changed_character_project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "characters": [changed_documents[2]["characters"][0]],
            },
        )
        self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_character_project))
        changed_reference = self._reference(original_name="changed.png")
        changed_reference_project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "characters": [{**character, "references": [changed_reference]}],
            },
        )
        self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_reference_project))

        changed_location = {**project["locations"][0], "description": "A changed location."}
        changed_location_project = self.storage.save_project(
            project["project_id"],
            {**project, "locations": [changed_location]},
        )
        self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_location_project))
        scene = project["scenes"][0]
        changed_scene_project = {
            **project,
            "scenes": [{**scene, "timeline_end_ms": 3_999, "exact_duration_ms": 3_999}],
        }
        self.assertNotEqual(first["request_fingerprint"], request_fingerprint(changed_scene_project))

    def test_valid_response_normalizes_and_validation_is_non_mutating(self):
        project = self._project_with_resources()
        character = project["characters"][0]
        location = project["locations"][0]
        response = self._response(
            project,
            character_ids=[character["character_id"]],
            location_id=location["location_id"],
            references=[
                {
                    "entity_type": "character",
                    "entity_id": character["character_id"],
                    "reference_id": character["references"][0]["reference_id"],
                },
                {
                    "entity_type": "location",
                    "entity_id": location["location_id"],
                    "reference_id": location["references"][0]["reference_id"],
                },
            ],
        )
        project_file = self.projects_root / project["project_id"] / "project.json"
        original_bytes = project_file.read_bytes()
        preview = validate_storyboard_response(project, response)
        self.assertEqual(preview["request_fingerprint"], response["request_fingerprint"])
        self.assertEqual(preview["scenes"][0]["scene_id"], project["scenes"][0]["scene_id"])
        self.assertEqual(project_file.read_bytes(), original_bytes)

    def test_multi_scene_response_requires_complete_current_scene_order(self):
        project = self._project_with_scene()
        new_scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 2_000, "text": "First"},
                {"cue_number": 2, "start_ms": 2_000, "end_ms": 4_000, "text": "Second"},
            ],
            4_000,
        )
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "scenes": new_scenes,
                "visuals": default_visuals_for_scenes(new_scenes),
                "prompts": default_prompts_for_scenes(new_scenes),
            },
            allow_visuals_change=True,
        )
        response = self._response(project)
        preview = validate_storyboard_response(project, response)
        self.assertEqual(
            [scene["scene_id"] for scene in preview["scenes"]],
            [scene["scene_id"] for scene in project["scenes"]],
        )
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(project, {**response, "scenes": list(reversed(response["scenes"]))})
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(project, {**response, "scenes": response["scenes"][:1]})

    def test_response_validation_rejects_stale_and_invalid_data(self):
        project = self._project_with_resources()
        valid = self._response(project)
        invalid_responses = (
            {**valid, "storyboard_response_version": 2},
            {**valid, "project_id": str(uuid.uuid4())},
            {**valid, "request_fingerprint": "b" * 64},
            {**valid, "unexpected": True},
            {**valid, "scenes": []},
            {**valid, "scenes": [{**valid["scenes"][0], "scene_type": "unknown"}]},
            {**valid, "scenes": [{**valid["scenes"][0], "action": "   "}]},
            {**valid, "scenes": [{**valid["scenes"][0], "visual_instructions": ""}]},
            {**valid, "scenes": [{**valid["scenes"][0], "character_ids": [str(uuid.uuid4())]}]},
        )
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_storyboard_response(project, invalid)

        changed = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "strict",
                    "story_brief": "Changed",
                    "visual_notes": "",
                },
            },
        )
        with self.assertRaisesRegex(ProjectValidationError, "out of date"):
            validate_storyboard_response(changed, valid)

        band_performance = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "band_performance",
                    "story_brief": "",
                    "visual_notes": "",
                },
            },
        )
        performance_response = self._response(band_performance)
        self.assertEqual(validate_storyboard_response(band_performance, performance_response)["scenes"][0]["scene_type"], "performance")
        narrative_response = {
            **performance_response,
            "scenes": [{**performance_response["scenes"][0], "scene_type": "narrative"}],
        }
        with self.assertRaisesRegex(ProjectValidationError, "Band Performance"):
            validate_storyboard_response(band_performance, narrative_response)

    def test_reference_rules_and_exact_fields_are_enforced(self):
        project = self._project_with_resources()
        character = project["characters"][0]
        location = project["locations"][0]
        base = self._response(project, character_ids=[character["character_id"]])
        bad_reference = {
            "entity_type": "character",
            "entity_id": character["character_id"],
            "reference_id": location["references"][0]["reference_id"],
        }
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(
                project,
                {**base, "scenes": [{**base["scenes"][0], "required_references": [bad_reference]}]},
            )
        location_reference = {
            "entity_type": "location",
            "entity_id": location["location_id"],
            "reference_id": location["references"][0]["reference_id"],
        }
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(
                project,
                {**base, "scenes": [{**base["scenes"][0], "required_references": [location_reference]}]},
            )

        duplicate_character = {**base["scenes"][0], "character_ids": [character["character_id"], character["character_id"]]}
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(project, {**base, "scenes": [duplicate_character]})

        extra_scene_field = {**base["scenes"][0], "extra": True}
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(project, {**base, "scenes": [extra_scene_field]})
        wrong_order = {**base, "scenes": [{**base["scenes"][0], "scene_id": str(uuid.uuid4())}]}
        with self.assertRaises(ProjectValidationError):
            validate_storyboard_response(project, wrong_order)

    def test_apply_clear_and_generic_save_protect_applied_storyboard(self):
        project = self._project_with_resources()
        applied = self._apply(project, self._response(project))
        project_file = self.projects_root / project["project_id"] / "project.json"
        self.assertTrue(applied["storyboard"]["scenes"])
        stale_browser_save = self.storage.save_project(
            applied["project_id"],
            {**applied, "name": "Renamed", "storyboard": empty_storyboard()},
        )
        self.assertEqual(stale_browser_save["name"], "Renamed")
        self.assertEqual(stale_browser_save["storyboard"], applied["storyboard"])
        self.assertEqual(self.storage.load_project(project["project_id"])["storyboard"], applied["storyboard"])

        cleared = self.storage.save_project(
            project["project_id"],
            {**stale_browser_save, "storyboard": empty_storyboard()},
            allow_storyboard_change=True,
        )
        self.assertEqual(cleared["storyboard"], empty_storyboard())
        self.assertEqual(cleared["characters"], project["characters"])
        self.assertEqual(cleared["locations"], project["locations"])
        self.assertEqual(cleared["scenes"], project["scenes"])
        self.assertTrue(project_file.is_file())

    def test_applied_storyboard_blocks_assigned_resource_deletion_only(self):
        project = self._project_with_resources()
        unrelated_character = create_character(
            self.storage,
            project["project_id"],
            {
                "name": "Unrelated",
                "role": "extra",
                "appearance": "Unrelated.",
                "outfit": "Unrelated.",
            },
        )
        unrelated_location = create_location(
            self.storage,
            project["project_id"],
            {"name": "Unrelated Place", "description": "Unrelated."},
        )
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply(
            self.storage.load_project(project["project_id"]),
            self._response(
                self.storage.load_project(project["project_id"]),
                character_ids=[character["character_id"]],
                location_id=location["location_id"],
                references=[
                    {
                        "entity_type": "character",
                        "entity_id": character["character_id"],
                        "reference_id": character["references"][0]["reference_id"],
                    },
                    {
                        "entity_type": "location",
                        "entity_id": location["location_id"],
                        "reference_id": location["references"][0]["reference_id"],
                    },
                ],
            ),
        )
        with self.assertRaises(ProjectValidationError):
            delete_character(self.storage, project["project_id"], character["character_id"])
        with self.assertRaises(ProjectValidationError):
            delete_location(self.storage, project["project_id"], location["location_id"])
        with self.assertRaises(ProjectValidationError):
            remove_reference(
                self.storage,
                project["project_id"],
                "characters",
                character["character_id"],
                character["references"][0]["reference_id"],
            )
        with self.assertRaises(ProjectValidationError):
            remove_reference(
                self.storage,
                project["project_id"],
                "locations",
                location["location_id"],
                location["references"][0]["reference_id"],
            )

        deleted_character_project = delete_character(
            self.storage,
            project["project_id"],
            unrelated_character["characters"][-1]["character_id"],
        )
        deleted_location_project = delete_location(
            self.storage,
            project["project_id"],
            unrelated_location["locations"][-1]["location_id"],
        )
        self.assertEqual(len(deleted_character_project["characters"]), 1)
        self.assertEqual(len(deleted_location_project["locations"]), 1)

    def test_editing_assigned_resources_remains_allowed_and_preserves_storyboard(self):
        project = self._project_with_resources()
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply(
            project,
            self._response(
                project,
                character_ids=[character["character_id"]],
                location_id=location["location_id"],
            ),
        )
        updated_character = update_character(
            self.storage,
            project["project_id"],
            character["character_id"],
            {
                "name": "Vesper Renamed",
                "role": character["role"],
                "appearance": character["appearance"],
                "outfit": character["outfit"],
            },
        )
        updated_location = update_location(
            self.storage,
            project["project_id"],
            location["location_id"],
            {"name": "Warehouse Renamed", "description": location["description"]},
        )
        self.assertTrue(updated_character["storyboard"]["scenes"])
        self.assertEqual(updated_location["storyboard"], updated_character["storyboard"])

    def test_rebuilding_scenes_clears_storyboard_but_preserves_direction_and_resources(self):
        project = self._project_with_resources()
        project = self._apply(project, self._response(project))
        project_directory = self.storage.project_directory(project["project_id"])
        (project_directory / "source" / "master_audio.wav").write_bytes(b"audio")
        (project_directory / "source" / "lyrics.srt").write_text("lyrics", encoding="utf-8")
        before_direction = project["story_direction"]
        before_characters = project["characters"]
        before_locations = project["locations"]
        rebuilt = build_project_scenes(
            self.storage,
            project["project_id"],
            lambda _text: [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "A lyric"}],
        )
        self.assertEqual(rebuilt["storyboard"], empty_storyboard())
        self.assertEqual(rebuilt["story_direction"], before_direction)
        self.assertEqual(rebuilt["characters"], before_characters)
        self.assertEqual(rebuilt["locations"], before_locations)
        self.assertEqual(len(rebuilt["scenes"]), 1)

    def test_source_replacement_clears_storyboard_but_failed_import_preserves_it(self):
        project = self._project_with_resources()
        project = self._apply(project, self._response(project))
        prior_storyboard = project["storyboard"]
        replaced = asyncio.run(
            import_master_audio(
                self.storage,
                project["project_id"],
                "replacement.mp3",
                FakeUpload(b"replacement audio"),
                probe_duration=lambda _path: 4_000,
            )
        )
        self.assertEqual(replaced["storyboard"], empty_storyboard())
        replacement_scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "A lyric"}],
            4_000,
        )
        replaced = self.storage.save_project(
            replaced["project_id"],
            {
                **replaced,
                "scenes": replacement_scenes,
                "visuals": default_visuals_for_scenes(replacement_scenes),
                "prompts": default_prompts_for_scenes(replacement_scenes),
            },
            allow_visuals_change=True,
        )
        replaced = self._apply(replaced, self._response(replaced))
        prior_reapplied_storyboard = replaced["storyboard"]
        with self.assertRaises(AudioProbeError):
            asyncio.run(
                import_master_audio(
                    self.storage,
                    project["project_id"],
                    "failed.mp3",
                    FakeUpload(b"failed"),
                    probe_duration=lambda _path: (_ for _ in ()).throw(AudioProbeError("probe failed")),
                )
            )
        self.assertEqual(self.storage.load_project(project["project_id"])["storyboard"], prior_reapplied_storyboard)
        self.assertNotEqual(prior_storyboard, prior_reapplied_storyboard)

    def test_frontend_and_routes_preserve_phase4_relay_contract(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        routes = Path("backend/routes.py").read_text(encoding="utf-8")
        for marker in (
            "data-mvb-story-brief",
            "data-mvb-visual-notes",
            "data-mvb-generate-request",
            "data-mvb-copy-request",
            "data-mvb-response-json",
            "data-mvb-validate-response",
            "data-mvb-storyboard-preview",
            "data-mvb-apply-storyboard",
            "data-mvb-clear-storyboard",
            "storyboardRequestPath",
        ):
            self.assertIn(marker, extension)
        for route in (
            "/storyboard/request",
            "/storyboard/validate",
            "/storyboard/apply",
            "/storyboard",
        ):
            self.assertIn(route, routes)
        self.assertIn("generation_method", extension)
        self.assertIn("generation-method", routes)
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertIn('data-mvb-view="prompts"', extension)
        self.assertIn('data-mvb-view="render"', extension)

    def test_frontend_relay_freshness_and_status_guards(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        styles = Path("web/builder.css").read_text(encoding="utf-8")
        self.assertIn("function invalidateStoryboardRequestForDirectionEdit()", extension)
        story_brief_handler = extension[extension.index('storyBrief.addEventListener("input"'):extension.index('visualNotes.addEventListener("input"')]
        visual_notes_handler = extension[extension.index('visualNotes.addEventListener("input"'):extension.index('responseInput.addEventListener("input"')]
        self.assertIn("invalidateStoryboardRequestForDirectionEdit();", story_brief_handler)
        self.assertIn("invalidateStoryboardRequestForDirectionEdit();", visual_notes_handler)
        self.assertIn("builderState.storyboardRequest = null;", extension)
        self.assertIn("builderState.storyboardPreview = null;", extension)
        self.assertIn("copyButton.disabled = !canCopyStoryboardRequest(", extension)
        self.assertIn("builderState.storyboardRequestStale = false;", extension)
        self.assertIn("storyboardRequestJson(builderState.storyboardRequest)", extension)

        open_project = extension[extension.index("async function openProject(root, projectId)"):]
        self.assertIn("void loadStoryboardRequest(root, true);", open_project)
        request_loader = extension[extension.index("async function loadStoryboardRequest(root, silent = false)"):extension.index("async function generateStoryboardRequest(root)")]
        self.assertIn("builderState.currentProject?.project_id !== projectId", request_loader)

        self.assertIn("? builderState.storyboardRelayState", extension)
        self.assertNotIn('status.dataset.state = relayMessage\n        ? "error"', extension)
        self.assertIn('.mvb-storyboard-relay-status[data-state="success"]', styles)
        self.assertIn('setStoryboardRelayMessage("Request ready to copy.");', extension)
        self.assertIn('setStoryboardRelayMessage("Storyboard applied.");', extension)
        self.assertIn('setStoryboardRelayMessage("Response JSON is not valid JSON.", "error");', extension)
        self.assertIn('setStoryboardRelayMessage("Save the current project before applying the storyboard.", "error");', extension)
        self.assertIn('setStoryboardRelayMessage(error instanceof Error', extension)

    def test_frontend_storyboard_mode_and_resource_layout_contract(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        styles = Path("web/builder.css").read_text(encoding="utf-8")
        for marker in (
            "data-mvb-storyboard-mode",
            "data-mvb-storyboard-mode-help",
            'value="loose"',
            'value="strict"',
            'value="band_performance"',
            "STORYBOARD_MODE_HELP",
        ):
            self.assertIn(marker, extension)
        self.assertLess(extension.index('<option value="loose">Loose</option>'), extension.index('<option value="strict">Strict</option>'))
        self.assertIn('storyboard_mode: "loose"', extension)

        mode_handler = extension[extension.index('storyboardMode.addEventListener("change"'):extension.index('storyBrief.addEventListener("input"')]
        for marker in ("story_direction.storyboard_mode", "editRevision += 1", "saveState = \"dirty\"", "scheduleAutosave(root)", "invalidateStoryboardRequestForDirectionEdit()"):
            self.assertIn(marker, mode_handler)
        self.assertIn("Object.keys(storyDirection).length === 3", extension)
        self.assertIn("storyDirection.storyboard_mode", extension)

        resource_grid = styles[styles.index(".mvb-storyboard-grid {"):styles.index(".mvb-storyboard-relay {")]
        self.assertIn("flex: 0 0 auto;", resource_grid)
        self.assertIn("min-height: auto;", resource_grid)
        self.assertNotIn("min-height: 0;", resource_grid)
        resource_panel = styles[styles.index(".mvb-resource-panel {"):styles.index(".mvb-resource-panel-heading {")]
        resource_list = styles[styles.index(".mvb-resource-list {"):styles.index(".mvb-entity-card {")]
        self.assertIn("min-height: auto;", resource_panel)
        self.assertIn("min-height: auto;", resource_list)
        self.assertNotRegex(resource_grid + resource_panel + resource_list, r"(?m)^\s+height:")


if __name__ == "__main__":
    unittest.main()
