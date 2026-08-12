import asyncio
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.entities import (
    EntityNotFoundError,
    ReferenceImportError,
    ReferenceNotFoundError,
    add_reference,
    create_character,
    create_location,
    delete_character,
    delete_location,
    get_reference_path,
    remove_reference,
    update_character,
    update_location,
)
from backend.projects import (
    ProjectNotFoundError,
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    validate_project_document,
)


class FakeUpload:
    def __init__(self, data: bytes, chunk_size: int = 7):
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
    image = Image.new("RGB", (3, 2), color=(180, 130, 60))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class Phase3TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def _character_payload(self, name="Vesper", role="performer"):
        return {
            "name": name,
            "role": role,
            "appearance": "Copper hair and a focused expression.",
            "outfit": "Black jacket with antique-gold trim.",
        }

    def _location_payload(self, name="Warehouse"):
        return {
            "name": name,
            "description": "A dark industrial space with high windows.",
        }

    def _v2_document(self, project):
        return {
            "schema_version": 2,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "source": {
                "master_audio": {
                    "stored_name": "master_audio.wav",
                    "original_name": "master.wav",
                    "duration_ms": 4_000,
                },
                "lyrics_srt": {
                    "stored_name": "lyrics.srt",
                    "original_name": "lyrics.srt",
                    "cue_count": 0,
                },
            },
            "scenes": [],
        }

    def test_new_projects_use_strict_schema_v4(self):
        project = self.storage.create_project("Phase 3 Project")
        self.assertEqual(project["schema_version"], 4)
        self.assertEqual(project["characters"], [])
        self.assertEqual(project["locations"], [])
        self.assertEqual(validate_project_document(project), project)

    def test_exact_v1_and_v2_normalize_without_mutating_files(self):
        project = self.storage.create_project("Legacy Project")
        project_file = self.projects_root / project["project_id"] / "project.json"
        v1 = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        project_file.write_text(json.dumps(v1), encoding="utf-8")
        loaded_v1 = self.storage.load_project(project["project_id"])
        self.assertEqual(loaded_v1["schema_version"], 4)
        self.assertEqual(loaded_v1["characters"], [])
        self.assertEqual(loaded_v1["locations"], [])
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), v1)

        v2 = self._v2_document(project)
        project_file.write_text(json.dumps(v2), encoding="utf-8")
        loaded_v2 = self.storage.load_project(project["project_id"])
        self.assertEqual(loaded_v2["schema_version"], 4)
        self.assertEqual(loaded_v2["source"], v2["source"])
        self.assertEqual(loaded_v2["scenes"], v2["scenes"])
        self.assertEqual(loaded_v2["characters"], [])
        self.assertEqual(loaded_v2["locations"], [])
        self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), v2)

    def test_legacy_save_upgrades_to_v4_but_cannot_erase_entities(self):
        project = self.storage.create_project("Legacy Save")
        project_file = self.projects_root / project["project_id"] / "project.json"
        v1 = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "name": project["name"],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        project_file.write_text(json.dumps(v1), encoding="utf-8")
        saved = self.storage.save_project(project["project_id"], {**v1, "name": "Upgraded"})
        self.assertEqual(saved["schema_version"], 4)
        self.assertEqual(saved["name"], "Upgraded")

        character_project = create_character(self.storage, project["project_id"], self._character_payload())
        current_file = self.projects_root / project["project_id"] / "project.json"
        with self.assertRaises(ProjectValidationError):
            self.storage.save_project(character_project["project_id"], v1)
        with self.assertRaises(ProjectValidationError):
            self.storage.save_project(character_project["project_id"], self._v2_document(character_project))
        self.assertEqual(self.storage.load_project(character_project["project_id"])["characters"], character_project["characters"])
        self.assertEqual(json.loads(current_file.read_text(encoding="utf-8"))["schema_version"], 4)

    def test_schema_v3_rejects_invalid_entities_and_references(self):
        project = self.storage.create_project("Validation")
        character = {
            "character_id": str(uuid.uuid4()),
            **self._character_payload(),
            "references": [],
        }
        location = {
            "location_id": str(uuid.uuid4()),
            **self._location_payload(),
            "references": [],
        }

        for invalid in (
            {**project, "characters": [character, {**character, "character_id": character["character_id"]}]},
            {**project, "locations": [location, {**location, "location_id": location["location_id"]}]},
            {**project, "characters": [{**character, "character_id": "not-a-uuid"}]},
            {**project, "characters": [{**character, "role": "singer"}]},
            {**project, "characters": [{**character, "references": [{
                "reference_id": str(uuid.uuid4()),
                "stored_name": "not-a-path.png",
                "original_name": "ref.png",
            }]}]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectValidationError):
                    validate_project_document(invalid)

        reference_id = str(uuid.uuid4())
        valid_reference = {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": "ref.png",
        }
        duplicate_references = [valid_reference, dict(valid_reference)]
        with self.assertRaises(ProjectValidationError):
            validate_project_document({**project, "characters": [{**character, "references": duplicate_references}]})
        with self.assertRaises(ProjectValidationError):
            validate_project_document({**project, "characters": [{
                **character,
                "references": [{**valid_reference, "stored_name": f"{reference_id}..png"}],
            }]})

        with self.assertRaises(ProjectValidationError):
            validate_project_document({**project, "schema_version": 99})

    def test_character_crud_preserves_ids_and_allows_duplicate_names(self):
        project = self.storage.create_project("Characters")
        first = create_character(self.storage, project["project_id"], self._character_payload())
        first_id = first["characters"][0]["character_id"]
        second = create_character(self.storage, project["project_id"], self._character_payload())
        self.assertEqual(len(second["characters"]), 2)
        self.assertNotEqual(second["characters"][0]["character_id"], second["characters"][1]["character_id"])

        updated = update_character(
            self.storage,
            project["project_id"],
            first_id,
            {**self._character_payload(), "name": "Renamed", "role": "band_member"},
        )
        self.assertEqual(updated["characters"][0]["character_id"], first_id)
        self.assertEqual(updated["characters"][0]["name"], "Renamed")
        self.assertEqual(updated["characters"][0]["role"], "band_member")

        deleted = delete_character(self.storage, project["project_id"], first_id)
        self.assertEqual(len(deleted["characters"]), 1)
        self.assertEqual(deleted["characters"][0]["name"], "Vesper")
        with self.assertRaises(EntityNotFoundError):
            delete_character(self.storage, project["project_id"], first_id)

    def test_location_crud_preserves_ids_and_unrelated_locations(self):
        project = self.storage.create_project("Locations")
        first = create_location(self.storage, project["project_id"], self._location_payload())
        first_id = first["locations"][0]["location_id"]
        second = create_location(self.storage, project["project_id"], self._location_payload("Street"))
        updated = update_location(
            self.storage,
            project["project_id"],
            first_id,
            {"name": "Renamed Warehouse", "description": "Updated description."},
        )
        self.assertEqual(updated["locations"][0]["location_id"], first_id)
        self.assertEqual(updated["locations"][0]["description"], "Updated description.")
        deleted = delete_location(self.storage, project["project_id"], first_id)
        self.assertEqual([item["location_id"] for item in deleted["locations"]], [second["locations"][1]["location_id"]])

    def test_real_image_validation_storage_preview_and_isolation(self):
        project = self.storage.create_project("References")
        character_project = create_character(self.storage, project["project_id"], self._character_payload())
        location_project = create_location(self.storage, project["project_id"], self._location_payload())
        character_id = character_project["characters"][0]["character_id"]
        location_id = location_project["locations"][0]["location_id"]

        with self.assertRaises(ReferenceImportError):
            run_async(add_reference(
                self.storage,
                project["project_id"],
                "characters",
                character_id,
                "fake.png",
                FakeUpload(b"not an image"),
            ))
        self.assertEqual(self.storage.load_project(project["project_id"])["characters"][0]["references"], [])

        added_png = run_async(add_reference(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            r"C:\\uploads\\singer.png",
            FakeUpload(image_bytes("PNG")),
        ))
        added_jpeg = run_async(add_reference(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            "portrait.jpeg",
            FakeUpload(image_bytes("JPEG")),
        ))
        added_webp = run_async(add_reference(
            self.storage,
            project["project_id"],
            "locations",
            location_id,
            "warehouse.webp",
            FakeUpload(image_bytes("WEBP")),
        ))

        character = added_jpeg["characters"][0]
        location = added_webp["locations"][0]
        self.assertEqual(len(character["references"]), 2)
        self.assertEqual(character["references"][0]["original_name"], "singer.png")
        self.assertTrue(character["references"][0]["stored_name"].endswith(".png"))
        self.assertTrue(character["references"][1]["stored_name"].endswith(".jpg"))
        self.assertTrue(location["references"][0]["stored_name"].endswith(".webp"))
        for entity_kind, entity_id, entity in (
            ("characters", character_id, character),
            ("locations", location_id, location),
        ):
            for reference in entity["references"]:
                self.assertNotIn("/", reference["stored_name"])
                self.assertNotIn("\\", reference["stored_name"])
                self.assertEqual(reference["stored_name"].split(".", 1)[0], reference["reference_id"])
                stored_path = self.projects_root / project["project_id"] / "references" / entity_kind / entity_id / reference["stored_name"]
                self.assertTrue(stored_path.is_file())
                self.assertEqual(get_reference_path(self.storage, project["project_id"], entity_kind, entity_id, reference["reference_id"]), stored_path)

        with self.assertRaises(ReferenceNotFoundError):
            get_reference_path(self.storage, project["project_id"], "locations", location_id, character["references"][0]["reference_id"])
        with self.assertRaises(ReferenceNotFoundError):
            get_reference_path(self.storage, project["project_id"], "characters", character_id, location["references"][0]["reference_id"])

        removed_reference = character["references"][0]
        removed_path = self.projects_root / project["project_id"] / "references" / "characters" / character_id / removed_reference["stored_name"]
        after_remove = remove_reference(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            removed_reference["reference_id"],
        )
        self.assertEqual(len(after_remove["characters"][0]["references"]), 1)
        self.assertFalse(removed_path.exists())

    def test_reference_install_is_cleaned_when_project_save_fails(self):
        project = self.storage.create_project("Reference Failure")
        character_project = create_character(self.storage, project["project_id"], self._character_payload())
        character_id = character_project["characters"][0]["character_id"]
        entity_directory = self.projects_root / project["project_id"] / "references" / "characters" / character_id
        with patch.object(self.storage, "save_project", side_effect=ProjectPersistenceError("injected")):
            with self.assertRaises(ProjectPersistenceError):
                run_async(add_reference(
                    self.storage,
                    project["project_id"],
                    "characters",
                    character_id,
                    "failure.png",
                    FakeUpload(image_bytes("PNG")),
                ))
        self.assertEqual(list(entity_directory.iterdir()), [])
        self.assertEqual(self.storage.load_project(project["project_id"])["characters"][0]["references"], [])

    def test_entity_deletion_cleans_only_its_reference_directory(self):
        project = self.storage.create_project("Cleanup")
        first = create_character(self.storage, project["project_id"], self._character_payload("First"))
        second = create_character(self.storage, project["project_id"], self._character_payload("Second"))
        first_id = first["characters"][0]["character_id"]
        second_id = second["characters"][1]["character_id"]
        updated = run_async(add_reference(
            self.storage,
            project["project_id"],
            "characters",
            second_id,
            "second.png",
            FakeUpload(image_bytes("PNG")),
        ))
        second_reference_path = self.projects_root / project["project_id"] / "references" / "characters" / second_id
        self.assertTrue(second_reference_path.is_dir())
        deleted = delete_character(self.storage, project["project_id"], first_id)
        self.assertEqual([item["character_id"] for item in deleted["characters"]], [second_id])
        self.assertFalse((self.projects_root / project["project_id"] / "references" / "characters" / first_id).exists())
        self.assertTrue(second_reference_path.is_dir())
        self.assertEqual(len(updated["characters"]), 2)

    def test_phase3_routes_are_explicit_and_id_scoped(self):
        routes_path = Path(__file__).parents[1] / "backend" / "routes.py"
        routes = routes_path.read_text(encoding="utf-8")
        required_routes = (
            "/characters",
            "/characters/{character_id}",
            "/characters/{character_id}/references",
            "/characters/{character_id}/references/{reference_id}",
            "/locations",
            "/locations/{location_id}",
            "/locations/{location_id}/references",
            "/locations/{location_id}/references/{reference_id}",
        )
        for route in required_routes:
            with self.subTest(route=route):
                self.assertIn(route, routes)
        self.assertIn('/music-video-builder/projects/invalid/ignore', routes)
        self.assertIn('@PromptServer.instance.routes.delete("/music-video-builder/projects/{project_id}")', routes)
        self.assertNotIn("path=", routes)
        self.assertIn("web.FileResponse(reference_path)", routes)

    def test_project_deletion_is_validated_and_fixed_root_scoped(self):
        target = self.storage.create_project("Delete Target")
        unrelated = self.storage.create_project("Keep Target")
        unrelated_marker = self.projects_root / unrelated["project_id"] / "source" / "keep.txt"
        unrelated_marker.write_text("keep", encoding="utf-8")

        result = self.storage.delete_project(target["project_id"])

        self.assertEqual(result, {"project_id": target["project_id"], "deleted": True})
        self.assertFalse((self.projects_root / target["project_id"]).exists())
        self.assertTrue(unrelated_marker.exists())
        self.assertEqual(
            [project["project_id"] for project in self.storage.list_projects()["projects"]],
            [unrelated["project_id"]],
        )

        for invalid_id in ("not-a-uuid", "..", str(self.projects_root), f"{unrelated['project_id']}\\.."):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ProjectValidationError):
                self.storage.delete_project(invalid_id)
        with self.assertRaises(ProjectNotFoundError):
            self.storage.delete_project(str(uuid.uuid4()))

        outside = Path(self.temp_directory.name) / "outside"
        outside.mkdir()
        symlink_id = str(uuid.uuid4())
        symlink_path = self.projects_root / symlink_id
        try:
            os.symlink(outside, symlink_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            symlink_path = None
        if symlink_path is not None:
            with self.assertRaises(ProjectValidationError):
                self.storage.delete_project(symlink_id)
            self.assertTrue(outside.exists())

    def test_invalid_project_ignore_is_persistent_signature_scoped_and_fail_safe(self):
        valid = self.storage.create_project("Still usable")
        invalid_directory = self.projects_root / str(uuid.uuid4())
        invalid_directory.mkdir()
        invalid_file = invalid_directory / "project.json"
        invalid_file.write_text("{", encoding="utf-8")

        first_listing = self.storage.list_projects()
        self.assertEqual([project["project_id"] for project in first_listing["projects"]], [valid["project_id"]])
        self.assertEqual(len(first_listing["invalid_projects"]), 1)
        first_invalid = first_listing["invalid_projects"][0]
        self.assertIn("signature", first_invalid)

        ignored = self.storage.ignore_invalid_project(first_invalid["folder_name"], first_invalid["signature"])
        self.assertTrue(ignored["ignored"])
        self.assertEqual(self.storage.list_projects()["invalid_projects"], [])

        fresh_storage = ProjectStorage(self.projects_root, self.storage.state_root)
        self.assertEqual(fresh_storage.list_projects()["invalid_projects"], [])
        self.assertTrue((self.storage.state_root / "ignored_invalid_projects.json").exists())
        self.assertEqual(
            [project["project_id"] for project in fresh_storage.list_projects()["projects"]],
            [valid["project_id"]],
        )

        invalid_file.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        changed_listing = fresh_storage.list_projects()
        self.assertEqual(len(changed_listing["invalid_projects"]), 1)
        self.assertNotEqual(changed_listing["invalid_projects"][0]["signature"], first_invalid["signature"])

        (self.storage.state_root / "ignored_invalid_projects.json").write_text("not json", encoding="utf-8")
        malformed_registry_listing = fresh_storage.list_projects()
        self.assertEqual(len(malformed_registry_listing["invalid_projects"]), 1)
        self.assertEqual(
            [project["project_id"] for project in malformed_registry_listing["projects"]],
            [valid["project_id"]],
        )

    def test_project_management_frontend_contract_has_delete_and_persistent_ignore(self):
        extension = (Path(__file__).parents[1] / "web" / "extension.js").read_text(encoding="utf-8")
        deletion = extension.split("async function deleteProject", 1)[1].split("function uploadFormData", 1)[0]

        self.assertIn("data-mvb-delete-dialog", extension)
        self.assertIn("Delete Project", extension)
        self.assertIn("openProjectDeleteDialog(root, project)", extension)
        self.assertIn('method: "DELETE"', deletion)
        self.assertIn("data-mvb-confirm-delete", extension)
        self.assertIn("data-mvb-cancel-delete", extension)
        self.assertIn("builderState.currentProject", extension.split("function openProjectDeleteDialog", 1)[1].split("async function deleteProject", 1)[0])
        self.assertIn("/invalid/ignore", extension)
        self.assertIn('ignoreButton.textContent = "Ignore"', extension)
        self.assertIn("data-mvb-invalid-warning", extension)
        self.assertNotIn("Dismiss", extension)
        self.assertNotIn("localStorage", extension)
        self.assertNotIn("sessionStorage", extension)
        self.assertNotIn("data-mvb-project-delete", extension)

    def test_modal_save_failures_surface_inside_character_and_location_forms(self):
        extension = (Path(__file__).parents[1] / "web" / "extension.js").read_text(encoding="utf-8")
        character_save = extension.split("async function saveCharacter", 1)[1].split("async function saveLocation", 1)[0]
        location_save = extension.split("async function saveLocation", 1)[1].split("async function uploadReference", 1)[0]

        for save_function, kind in ((character_save, "character"), (location_save, "location")):
            with self.subTest(kind=kind):
                self.assertIn(f'data-mvb-{kind}-error', save_function)
                self.assertIn("errorElement.textContent = builderState.storyboardMessage ||", save_function)
                self.assertIn("errorElement.hidden = false;", save_function)
                self.assertIn("if (!saved && isActive(root))", save_function)


if __name__ == "__main__":
    unittest.main()
