import asyncio
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.entities import delete_character, delete_location, remove_reference
from backend.projects import (
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    default_prompts_for_scenes,
    validate_project_document,
)
from backend.scenes import build_scenes
from backend.storyboard import (
    build_storyboard_request,
    request_fingerprint,
    validate_storyboard_response,
)
from backend.source import import_master_audio
from backend.visuals import (
    GENERATION_METHODS,
    assign_keyframe,
    build_keyframe_prompt,
    default_visuals_for_scenes,
    derive_visual_readiness,
    generate_and_save_keyframe_prompt,
    get_keyframe_path,
    planned_reference_mapping,
    remove_keyframe,
    save_keyframe_details,
    save_reference_selection,
    set_all_generation_method,
    set_generation_method,
)


class FakeUpload:
    def __init__(self, data: bytes, chunk_size: int = 11):
        self.data = data
        self.chunk_size = chunk_size
        self.position = 0

    async def read_chunk(self, _size: int) -> bytes:
        if self.position >= len(self.data):
            return b""
        end = min(self.position + self.chunk_size, len(self.data))
        chunk = self.data[self.position:end]
        self.position = end
        return chunk


def run_async(coroutine):
    return asyncio.run(coroutine)


def image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (5, 3), color=(179, 139, 77))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class Phase5TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def _project_with_scene(self, name="Visuals Project"):
        project = self.storage.create_project(name)
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

    @staticmethod
    def _reference(original_name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": original_name,
        }

    def _project_with_references(self):
        project = self._project_with_scene("Reference Project")
        character_id = str(uuid.uuid4())
        location_id = str(uuid.uuid4())
        character_reference = self._reference("performer.png")
        second_character_reference = self._reference("performer-close.png")
        location_reference = self._reference("warehouse.png")
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "characters": [
                    {
                        "character_id": character_id,
                        "name": "Vesper",
                        "role": "performer",
                        "appearance": "Copper hair.",
                        "outfit": "Black jacket.",
                        "references": [character_reference, second_character_reference],
                    }
                ],
                "locations": [
                    {
                        "location_id": location_id,
                        "name": "Warehouse",
                        "description": "An industrial performance space.",
                        "references": [location_reference],
                    }
                ],
            },
        )
        project_root = self.storage.project_directory(project["project_id"])
        for collection, entity_id, references in (
            ("characters", character_id, [character_reference, second_character_reference]),
            ("locations", location_id, [location_reference]),
        ):
            reference_directory = project_root / "references" / collection / entity_id
            reference_directory.mkdir(parents=True, exist_ok=True)
            for reference in references:
                (reference_directory / reference["stored_name"]).write_bytes(b"reference still")
        return project, character_reference, second_character_reference, location_reference

    def _apply_storyboard(self, project, required_references=None):
        required_references = required_references or []
        response = {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [
                {
                    "scene_id": project["scenes"][0]["scene_id"],
                    "scene_type": "performance",
                    "character_ids": [character["character_id"] for character in project["characters"]],
                    "location_id": project["locations"][0]["location_id"] if project["locations"] else None,
                    "action": "Performer holds the frame.",
                    "visual_instructions": "Warm practical light and restrained contrast.",
                    "camera_direction": "Slow lateral tracking.",
                    "motion_direction": "Measured movement.",
                    "continuity_notes": "Keep the jacket and warehouse consistent.",
                    "required_references": required_references,
                }
            ],
        }
        storyboard = validate_storyboard_response(project, response)
        return self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )

    def test_new_project_is_schema_v7_with_exact_empty_visuals_and_prompts(self):
        project = self.storage.create_project("Schema v6")
        self.assertEqual(project["schema_version"], 7)
        self.assertEqual(set(project["visuals"]), {"scenes"})
        self.assertEqual(project["visuals"]["scenes"], [])
        self.assertEqual(validate_project_document(project), project)

    def test_v1_v2_v3_and_provisional_v4_normalize_without_mutation(self):
        project = self.storage.create_project("Legacy Visuals")
        project_file = self.projects_root / project["project_id"] / "project.json"
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
        v3 = {
            **v2,
            "schema_version": 3,
            "characters": [],
            "locations": [],
        }
        v4 = {
            **v3,
            "schema_version": 4,
            "story_direction": {"story_brief": "Brief", "visual_notes": "Notes"},
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        for legacy in (v1, v2, v3, v4):
            project_file.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = self.storage.load_project(project["project_id"])
            self.assertEqual(loaded["schema_version"], 7)
            self.assertEqual(loaded["story_direction"]["storyboard_mode"], "loose")
            self.assertEqual(loaded["visuals"]["scenes"], [])
            self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), legacy)

    def test_provisional_v4_save_upgrades_to_exact_current_shape(self):
        project = self.storage.create_project("Upgrade Visuals")
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "loose",
                    "story_brief": "Brief",
                    "visual_notes": "Notes",
                },
            },
        )
        provisional = {
            **project,
            "schema_version": 4,
            "story_direction": {"story_brief": "Brief", "visual_notes": "Notes"},
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        provisional.pop("visuals")
        provisional.pop("prompts")
        project_file = self.projects_root / project["project_id"] / "project.json"
        project_file.write_text(json.dumps(provisional), encoding="utf-8")
        saved = self.storage.save_project(project["project_id"], {**provisional, "name": "Upgraded"})
        self.assertEqual(saved["schema_version"], 7)
        self.assertEqual(set(saved["story_direction"]), {"storyboard_mode", "story_brief", "visual_notes"})
        self.assertEqual(set(saved["visuals"]), {"scenes"})
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8"))["schema_version"], 7)

    def _non_default_direction_project(self):
        project = self.storage.create_project("Story Direction Guard")
        return self.storage.save_project(
            project["project_id"],
            {
                **project,
                "story_direction": {
                    "storyboard_mode": "strict",
                    "story_brief": "Current brief",
                    "visual_notes": "Current notes",
                },
            },
        )

    def test_v1_stale_save_cannot_erase_non_default_story_direction(self):
        project = self._non_default_direction_project()
        stale = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        with self.assertRaisesRegex(ProjectValidationError, "story direction"):
            self.storage.save_project(project["project_id"], stale)
        self.assertEqual(self.storage.load_project(project["project_id"])["story_direction"], project["story_direction"])

    def test_v2_stale_save_cannot_erase_non_default_story_direction(self):
        project = self._non_default_direction_project()
        stale = {
            "schema_version": 2,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "source": {"master_audio": None, "lyrics_srt": None},
            "scenes": [],
        }
        with self.assertRaisesRegex(ProjectValidationError, "story direction"):
            self.storage.save_project(project["project_id"], stale)
        self.assertEqual(self.storage.load_project(project["project_id"])["story_direction"], project["story_direction"])

    def test_v3_stale_save_cannot_erase_non_default_story_direction(self):
        project = self._non_default_direction_project()
        stale = {
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
        with self.assertRaisesRegex(ProjectValidationError, "story direction"):
            self.storage.save_project(project["project_id"], stale)
        self.assertEqual(self.storage.load_project(project["project_id"])["story_direction"], project["story_direction"])

    def test_mismatched_v4_save_cannot_overwrite_current_story_direction(self):
        project = self._non_default_direction_project()
        stale = {
            "schema_version": 4,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "source": {"master_audio": None, "lyrics_srt": None},
            "scenes": [],
            "characters": [],
            "locations": [],
            "story_direction": {"story_brief": "Stale brief", "visual_notes": "Stale notes"},
            "storyboard": {"request_fingerprint": None, "scenes": []},
        }
        with self.assertRaisesRegex(ProjectValidationError, "story direction"):
            self.storage.save_project(project["project_id"], stale)
        self.assertEqual(self.storage.load_project(project["project_id"])["story_direction"], project["story_direction"])

    def test_visuals_validation_rejects_unknown_schema_method_shape_and_order(self):
        project = self._project_with_scene()
        valid = project["visuals"]
        invalid_documents = (
            {**project, "schema_version": 8},
            {**project, "visuals": {"scenes": [{**valid["scenes"][0], "generation_method": "t2v"}]}},
            {**project, "visuals": {"scenes": [{**valid["scenes"][0], "extra": True}]}},
            {**project, "visuals": {"scenes": []}},
            {**project, "visuals": {"scenes": [{**valid["scenes"][0], "scene_id": str(uuid.uuid4())}]}},
        )
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_project_document(invalid)

    def test_visual_methods_are_exact_and_switch_without_losing_other_state(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        for method in GENERATION_METHODS:
            project = set_generation_method(self.storage, project["project_id"], scene_id, method)
            self.assertEqual(project["visuals"]["scenes"][0]["generation_method"], method)
        with self.assertRaises(ProjectValidationError):
            set_generation_method(self.storage, project["project_id"], scene_id, "t2v")
        with self.assertRaises(ProjectValidationError):
            set_generation_method(self.storage, project["project_id"], scene_id, "reference_to_video")

    def test_generic_save_preserves_visuals_until_explicit_visual_mutation(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        changed = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        stale = self.storage.save_project(
            changed["project_id"],
            {**changed, "name": "Renamed", "visuals": default_visuals_for_scenes(changed["scenes"])},
        )
        self.assertEqual(stale["visuals"], changed["visuals"])
        self.assertEqual(stale["name"], "Renamed")

    def test_keyframe_details_are_exact_and_prompt_is_deterministic_local(self):
        project, _, _, _ = self._project_with_references()
        project = self._apply_storyboard(project)
        scene_id = project["scenes"][0]["scene_id"]
        prompt_one = build_keyframe_prompt(project, scene_id)
        prompt_two = build_keyframe_prompt(self.storage.load_project(project["project_id"]), scene_id)
        self.assertEqual(prompt_one, prompt_two)
        self.assertIn("Scene 1 timeline", prompt_one)
        self.assertIn("Vesper", prompt_one)
        self.assertNotIn(str(self.projects_root), prompt_one)
        self.assertNotIn("keyframe_i2v", prompt_one)
        project = generate_and_save_keyframe_prompt(self.storage, project["project_id"], scene_id)
        self.assertEqual(project["visuals"]["scenes"][0]["keyframe_i2v"]["keyframe_generation_prompt"], prompt_one)
        details = {
            "keyframe_generation_prompt": "A saved cinematic prompt.",
            "intended_keyframe_description": "The intended image.",
            "actual_keyframe_description": "The accepted image.",
        }
        project = save_keyframe_details(self.storage, project["project_id"], scene_id, details)
        self.assertEqual(project["visuals"]["scenes"][0]["keyframe_i2v"]["intended_keyframe_description"], details["intended_keyframe_description"])
        with self.assertRaises(ProjectValidationError):
            save_keyframe_details(self.storage, project["project_id"], scene_id, {**details, "extra": "no"})

    def test_keyframe_upload_accepts_png_jpeg_webp_and_uses_project_local_metadata(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        project_directory = self.projects_root / project["project_id"]
        asset_ids = []
        for image_format, filename in (("PNG", "first.png"), ("JPEG", "replace.jpg"), ("WEBP", "final.webp")):
            project = run_async(assign_keyframe(
                self.storage,
                project["project_id"],
                scene_id,
                filename,
                FakeUpload(image_bytes(image_format)),
            ))
            accepted = project["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"]
            asset_ids.append(accepted["asset_id"])
            self.assertEqual(accepted["original_name"], filename)
            self.assertNotIn(str(project_directory), json.dumps(accepted))
            path = get_keyframe_path(self.storage, project["project_id"], scene_id)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, project_directory / "keyframes" / scene_id)
        self.assertEqual(list((project_directory / "keyframes" / scene_id).glob(".keyframe-upload-*.tmp")), [])
        self.assertEqual(len(set(asset_ids)), 3)

    def test_invalid_keyframe_upload_and_replace_failure_preserve_prior_asset(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        project = run_async(assign_keyframe(
            self.storage,
            project["project_id"],
            scene_id,
            "original.png",
            FakeUpload(image_bytes("PNG")),
        ))
        original_metadata = project["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"]
        original_path = get_keyframe_path(self.storage, project["project_id"], scene_id)
        original_bytes = original_path.read_bytes()
        with self.assertRaises(ProjectValidationError):
            run_async(assign_keyframe(
                self.storage,
                project["project_id"],
                scene_id,
                "bad.png",
                FakeUpload(b"not an image"),
            ))
        self.assertEqual(self.storage.load_project(project["project_id"])["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"], original_metadata)
        with patch("backend.visuals.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(ProjectPersistenceError):
                run_async(assign_keyframe(
                    self.storage,
                    project["project_id"],
                    scene_id,
                    "failed.png",
                    FakeUpload(image_bytes("PNG")),
                ))
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.storage.load_project(project["project_id"])["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"], original_metadata)
        self.assertEqual(list(original_path.parent.glob(".keyframe-upload-*.tmp")), [])

    def test_remove_keyframe_clears_metadata_and_file(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        project = run_async(assign_keyframe(
            self.storage,
            project["project_id"],
            scene_id,
            "remove.png",
            FakeUpload(image_bytes("PNG")),
        ))
        path = get_keyframe_path(self.storage, project["project_id"], scene_id)
        project = remove_keyframe(self.storage, project["project_id"], scene_id)
        self.assertIsNone(project["visuals"]["scenes"][0]["keyframe_i2v"]["accepted_keyframe"])
        self.assertFalse(path.exists())

    def test_reference_selection_preserves_order_and_mapping_rules(self):
        project, character_reference, second_character_reference, location_reference = self._project_with_references()
        character = project["characters"][0]
        location = project["locations"][0]
        required = [
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": character_reference["reference_id"]},
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": location_reference["reference_id"]},
        ]
        project = self._apply_storyboard(project, required)
        scene_id = project["scenes"][0]["scene_id"]
        selected = [
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": character_reference["reference_id"]},
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": second_character_reference["reference_id"]},
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": location_reference["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        self.assertEqual(project["visuals"]["scenes"][0]["reference2video"]["selected_references"], selected)
        mapping = planned_reference_mapping(project, scene_id)
        self.assertEqual([item["picture_tag"] for item in mapping], ["<Picture 1>", "<Picture 2>", "<Picture 3>"])
        self.assertEqual(mapping[0]["subject_tag"], "<Subject 1>")
        self.assertEqual(mapping[1]["subject_tag"], "<Subject 1>")
        self.assertEqual(mapping[2]["subject_tag"], "<Subject 2>")

    def test_reference_mapping_numbers_distinct_character_and_location_owners_in_order(self):
        project, character_reference, second_character_reference, location_reference = self._project_with_references()
        location = project["locations"][0]
        second_location_reference = self._reference("warehouse-wide.png")
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "locations": [
                    {**location, "references": [location_reference, second_location_reference]},
                ],
            },
        )
        scene_id = project["scenes"][0]["scene_id"]
        selected = [
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": location_reference["reference_id"]},
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": second_location_reference["reference_id"]},
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": character_reference["reference_id"]},
            {"entity_type": "character", "entity_id": project["characters"][0]["character_id"], "reference_id": second_character_reference["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": selected})
        mapping = planned_reference_mapping(project, scene_id)
        self.assertEqual([item["picture_tag"] for item in mapping], ["<Picture 1>", "<Picture 2>", "<Picture 3>", "<Picture 4>"])
        self.assertEqual([item["subject_tag"] for item in mapping], ["<Subject 1>", "<Subject 1>", "<Subject 2>", "<Subject 2>"])

        reordered = [selected[2], selected[0], selected[3], selected[1]]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": reordered})
        reordered_mapping = planned_reference_mapping(project, scene_id)
        self.assertEqual([item["picture_tag"] for item in reordered_mapping], ["<Picture 1>", "<Picture 2>", "<Picture 3>", "<Picture 4>"])
        self.assertEqual([item["subject_tag"] for item in reordered_mapping], ["<Subject 1>", "<Subject 2>", "<Subject 1>", "<Subject 2>"])

    def test_reference_selection_rejects_duplicates_unknown_and_unowned_references(self):
        project, character_reference, _, location_reference = self._project_with_references()
        scene_id = project["scenes"][0]["scene_id"]
        character = project["characters"][0]
        valid = {"entity_type": "character", "entity_id": character["character_id"], "reference_id": character_reference["reference_id"]}
        with self.assertRaises(ProjectValidationError):
            save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": [valid, valid]})
        with self.assertRaises(ProjectValidationError):
            save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": [{**valid, "reference_id": location_reference["reference_id"]}]})
        with self.assertRaises(ProjectValidationError):
            save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": [{**valid, "entity_id": str(uuid.uuid4())}]})
        with self.assertRaises(ProjectValidationError):
            save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": [{**valid, "extra": True}]})

    def test_visual_reference_selection_blocks_entity_and_reference_deletion(self):
        project, character_reference, _, location_reference = self._project_with_references()
        character = project["characters"][0]
        location = project["locations"][0]
        selected = [
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": character_reference["reference_id"]},
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": location_reference["reference_id"]},
        ]
        project = save_reference_selection(self.storage, project["project_id"], project["scenes"][0]["scene_id"], {"selected_references": selected})
        with self.assertRaisesRegex(ProjectValidationError, "Visuals"):
            remove_reference(self.storage, project["project_id"], "characters", character["character_id"], character_reference["reference_id"])
        with self.assertRaisesRegex(ProjectValidationError, "Visuals"):
            delete_character(self.storage, project["project_id"], character["character_id"])
        project = save_reference_selection(self.storage, project["project_id"], project["scenes"][0]["scene_id"], {"selected_references": []})
        project = delete_location(self.storage, project["project_id"], location["location_id"])
        self.assertEqual(project["locations"], [])

    def test_readiness_requires_storyboard_and_method_specific_inputs(self):
        project, character_reference, _, location_reference = self._project_with_references()
        scene_id = project["scenes"][0]["scene_id"]
        missing_storyboard = derive_visual_readiness(project, scene_id, self.storage)
        self.assertFalse(missing_storyboard["ready"])
        self.assertIn("Apply a storyboard before completing Visuals.", missing_storyboard["missing"])
        character = project["characters"][0]
        location = project["locations"][0]
        required = [
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": character_reference["reference_id"]},
            {"entity_type": "location", "entity_id": location["location_id"], "reference_id": location_reference["reference_id"]},
        ]
        project = self._apply_storyboard(project, required)
        blocked_keyframe = derive_visual_readiness(project, scene_id, self.storage)
        self.assertFalse(blocked_keyframe["ready"])
        self.assertIn("Missing accepted keyframe", blocked_keyframe["missing"])
        project = generate_and_save_keyframe_prompt(self.storage, project["project_id"], scene_id)
        project = save_keyframe_details(
            self.storage,
            project["project_id"],
            scene_id,
            {
                "keyframe_generation_prompt": project["visuals"]["scenes"][0]["keyframe_i2v"]["keyframe_generation_prompt"],
                "intended_keyframe_description": "A complete intended still.",
                "actual_keyframe_description": "The accepted still.",
            },
        )
        project = run_async(assign_keyframe(self.storage, project["project_id"], scene_id, "ready.png", FakeUpload(image_bytes("PNG"))))
        self.assertTrue(derive_visual_readiness(project, scene_id, self.storage)["ready"])
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        visual_selected = [
            *required[:1],
            {"entity_type": "character", "entity_id": character["character_id"], "reference_id": project["characters"][0]["references"][1]["reference_id"]},
            required[1],
        ]
        project = save_reference_selection(self.storage, project["project_id"], scene_id, {"selected_references": visual_selected})
        self.assertTrue(derive_visual_readiness(project, scene_id, self.storage)["ready"])

    def test_readiness_becomes_blocked_when_applied_storyboard_is_out_of_date(self):
        project = self._project_with_scene()
        project = self._apply_storyboard(project)
        scene_id = project["scenes"][0]["scene_id"]
        project = self.storage.save_project(
            project["project_id"],
            {**project, "story_direction": {"storyboard_mode": "strict", "story_brief": "Changed", "visual_notes": ""}},
        )
        readiness = derive_visual_readiness(project, scene_id)
        self.assertFalse(readiness["ready"])
        self.assertIn("Storyboard is out of date.", readiness["missing"])

    def test_scene_and_source_replacement_reset_visuals(self):
        project = self._project_with_scene()
        scene_id = project["scenes"][0]["scene_id"]
        project = set_generation_method(self.storage, project["project_id"], scene_id, "reference2video")
        project_directory = self.storage.project_directory(project["project_id"])
        (project_directory / "source" / "master_audio.wav").write_bytes(b"audio")
        (project_directory / "source" / "lyrics.srt").write_text("1\n00:00:00,000 --> 00:00:04,000\nA lyric", encoding="utf-8")
        from backend.scenes import build_project_scenes

        rebuilt = build_project_scenes(self.storage, project["project_id"], lambda text: [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "A lyric"}])
        self.assertEqual(len(rebuilt["visuals"]["scenes"]), 1)
        self.assertEqual(rebuilt["visuals"]["scenes"][0]["generation_method"], "keyframe_i2v")
        project = run_async(import_master_audio(
            self.storage,
            rebuilt["project_id"],
            "replacement.wav",
            FakeUpload(b"replacement"),
            probe_duration=lambda _path: 4_000,
        ))
        self.assertEqual(project["scenes"], [])
        self.assertEqual(project["visuals"]["scenes"], [])

    def test_bulk_generation_method_changes_only_methods_and_preserves_both_branches(self):
        project, character_reference, _, location_reference = self._project_with_references()
        two_scenes = build_scenes(
            [
                {"cue_number": 1, "start_ms": 0, "end_ms": 2_000, "text": "First"},
                {"cue_number": 2, "start_ms": 2_000, "end_ms": 4_000, "text": "Second"},
            ],
            4_000,
        )
        character = project["characters"][0]
        location = project["locations"][0]
        selected = [{
            "entity_type": "character",
            "entity_id": character["character_id"],
            "reference_id": character_reference["reference_id"],
        }]
        visuals = default_visuals_for_scenes(two_scenes)
        visuals["scenes"][0]["keyframe_i2v"]["intended_keyframe_description"] = "Keep this branch."
        visuals["scenes"][1]["reference2video"]["selected_references"] = selected
        project = self.storage.save_project(
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
                        "cue_count": 2,
                    },
                },
                "scenes": two_scenes,
                "visuals": visuals,
                "prompts": default_prompts_for_scenes(two_scenes),
            },
            allow_visuals_change=True,
        )
        before = project["visuals"]
        switched = set_all_generation_method(self.storage, project["project_id"], "reference2video")
        self.assertEqual([scene["generation_method"] for scene in switched["visuals"]["scenes"]], ["reference2video", "reference2video"])
        for before_scene, after_scene in zip(before["scenes"], switched["visuals"]["scenes"]):
            self.assertEqual(after_scene["scene_id"], before_scene["scene_id"])
            self.assertEqual(after_scene["keyframe_i2v"], before_scene["keyframe_i2v"])
            self.assertEqual(after_scene["reference2video"], before_scene["reference2video"])
        switched_back = set_all_generation_method(self.storage, project["project_id"], "keyframe_i2v")
        self.assertEqual([scene["generation_method"] for scene in switched_back["visuals"]["scenes"]], ["keyframe_i2v", "keyframe_i2v"])
        self.assertEqual(switched_back["visuals"]["scenes"][1]["reference2video"]["selected_references"], selected)
        with self.assertRaises(ProjectValidationError):
            set_all_generation_method(self.storage, project["project_id"], "t2v")

    def test_request_contains_no_visuals_method_and_fingerprint_changes_only_with_direction(self):
        project = self._project_with_scene()
        first = build_storyboard_request(project)
        changed = self.storage.save_project(
            project["project_id"],
            {**project, "story_direction": {"storyboard_mode": "band_performance", "story_brief": "", "visual_notes": ""}},
        )
        second = build_storyboard_request(changed)
        self.assertIn("storyboard_mode", first["story_direction"])
        self.assertNotIn("generation_method", json.dumps(first))
        self.assertNotIn("H3", json.dumps(first))
        self.assertNotEqual(first["request_fingerprint"], second["request_fingerprint"])
        self.assertEqual(second["request_fingerprint"], request_fingerprint(changed))

    def test_frontend_and_routes_expose_only_phase5_visual_contract(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        styles = Path("web/builder.css").read_text(encoding="utf-8")
        routes = Path("backend/routes.py").read_text(encoding="utf-8")
        for marker in (
            'data-mvb-view="visuals"',
            "data-mvb-visual-scenes",
            "runVisualMutation",
            "keyframe_generation_prompt",
            "intended_keyframe_description",
            "actual_keyframe_description",
            "selected_references",
            "<Picture ${index + 1}>",
            "const ownerKey = `${selector.entity_type}:${selector.entity_id}`",
            "<Subject ${subjectNumbers.get(ownerKey)}>",
            "Keyframe / Image-to-Video",
            "Reference-to-Video",
            "Generation Method for All Scenes",
            "Apply to All Scenes",
            "captureVisualViewport(root)",
            "restoreVisualViewport(root, viewport)",
            "?v=${encodeURIComponent(accepted.asset_id)}",
        ):
            self.assertIn(marker, extension)
        for route in (
            "/visuals/scenes/{scene_id}/generation-method",
            "/visuals/generation-method",
            "/visuals/scenes/{scene_id}/keyframe-prompt",
            "/visuals/scenes/{scene_id}/keyframe-details",
            "/visuals/scenes/{scene_id}/keyframe",
            "/visuals/scenes/{scene_id}/keyframe/image",
            "/visuals/scenes/{scene_id}/reference2video",
        ):
            self.assertIn(route, routes)
        self.assertIn(".mvb-visual-scenes {", styles)
        self.assertIn("align-content: start;", styles)
        self.assertIn(".mvb-visual-bulk {", styles)
        self.assertIn('headers={"Cache-Control": "no-store"}', routes)
        self.assertIn("set_all_generation_method", routes)
        self.assertIn("visualBulkMethod", extension)
        self.assertIn("visual-bulk-method", extension)
        self.assertIn("setCurrentProject(root, project, options)", extension)
        self.assertIn("renderProjectState(root, { visualViewport })", extension)
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertIn('data-mvb-view="prompts"', extension)
        self.assertIn('data-mvb-view="render"', extension)
        self.assertNotIn("OpenAI", extension)

    def test_frontend_preserves_visual_drafts_and_scene_expansion_state(self):
        extension = Path("web/extension.js").read_text(encoding="utf-8")
        for marker in (
            "function reconcileVisualDrafts",
            "function reconcileVisualExpansion",
            "options.reconciledVisualSceneId",
            "builderState.visualExpandedScenes[scene.scene_id] = card.open",
            "Object.keys(builderState.visualExpandedScenes)",
            "nextSceneIds.has(sceneId)",
            "setCurrentProject(root, project, options)",
        ):
            self.assertIn(marker, extension)
        self.assertNotIn("card.open = index === 0;", extension)
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)


if __name__ == "__main__":
    unittest.main()
