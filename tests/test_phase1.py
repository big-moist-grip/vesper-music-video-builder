import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.projects import (
    ProjectPersistenceError,
    ProjectStorage,
    ProjectValidationError,
    parse_timestamp,
    validate_project_document,
)


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def test_schema_validation_accepts_new_project_and_rejects_invalid_values(self):
        project = self.storage.create_project("  Valid Project  ")
        self.assertEqual(project["name"], "Valid Project")
        self.assertEqual(project["schema_version"], 5)
        self.assertEqual(project["source"], {"master_audio": None, "lyrics_srt": None})
        self.assertEqual(project["scenes"], [])
        self.assertEqual(project["characters"], [])
        self.assertEqual(project["locations"], [])
        self.assertEqual(validate_project_document(project), project)

        malformed_uuid = {**project, "project_id": "not-a-uuid"}
        with self.assertRaises(ProjectValidationError):
            validate_project_document(malformed_uuid)

        unsupported_version = {**project, "schema_version": 99}
        with self.assertRaises(ProjectValidationError):
            validate_project_document(unsupported_version)

        empty_name = {**project, "name": "   "}
        with self.assertRaises(ProjectValidationError):
            validate_project_document(empty_name)

        mismatched_id = {**project, "project_id": str(uuid.uuid4())}
        with self.assertRaises(ProjectValidationError):
            validate_project_document(mismatched_id, expected_project_id=project["project_id"])

    def test_create_project_builds_fixed_directory_tree_and_valid_json(self):
        project = self.storage.create_project("Phase 1 Test")
        project_directory = self.projects_root / project["project_id"]

        self.assertTrue(project_directory.is_dir())
        for relative_directory in (
            "source",
            "references",
            "references/characters",
            "references/locations",
            "keyframes",
            "scene_audio",
            "renders",
            "export",
        ):
            self.assertTrue((project_directory / relative_directory).is_dir())

        project_file = project_directory / "project.json"
        self.assertTrue(project_file.is_file())
        persisted = json.loads(project_file.read_text(encoding="utf-8"))
        self.assertEqual(validate_project_document(persisted), project)
        self.assertEqual(self.storage.load_project(project["project_id"]), project)
        self.assertEqual(project["project_id"], str(uuid.UUID(project["project_id"])))

    def test_save_load_roundtrip_preserves_identity_and_creation_time(self):
        created = self.storage.create_project("Before Save")
        changed = {**created, "name": "After Save", "created_at": "2000-01-01T00:00:00Z"}

        saved = self.storage.save_project(created["project_id"], changed)
        loaded = self.storage.load_project(created["project_id"])

        self.assertEqual(saved, loaded)
        self.assertEqual(saved["name"], "After Save")
        self.assertEqual(saved["project_id"], created["project_id"])
        self.assertEqual(saved["created_at"], created["created_at"])
        self.assertGreaterEqual(
            parse_timestamp(saved["updated_at"]),
            parse_timestamp(created["updated_at"]),
        )

        changed_id = {**created, "project_id": str(uuid.uuid4()), "name": "Should Reject"}
        with self.assertRaises(ProjectValidationError):
            self.storage.save_project(created["project_id"], changed_id)

    def test_list_allows_duplicate_names_and_reports_malformed_directory(self):
        first = self.storage.create_project("Duplicate Test")
        second = self.storage.create_project("Duplicate Test")
        malformed_id = str(uuid.uuid4())
        malformed_directory = self.projects_root / malformed_id
        malformed_directory.mkdir()
        (malformed_directory / "project.json").write_text("{not valid json", encoding="utf-8")

        listed = self.storage.list_projects()
        listed_ids = {project["project_id"] for project in listed["projects"]}

        self.assertEqual(listed_ids, {first["project_id"], second["project_id"]})
        self.assertEqual(
            [project["name"] for project in listed["projects"]].count("Duplicate Test"),
            2,
        )
        self.assertEqual(len(listed["invalid_projects"]), 1)
        self.assertEqual(listed["invalid_projects"][0]["folder_name"], malformed_id)

    def test_atomic_replace_failure_keeps_original_bytes_and_cleans_temporary_file(self):
        project = self.storage.create_project("Atomic Project")
        project_file = self.projects_root / project["project_id"] / "project.json"
        original_bytes = project_file.read_bytes()

        with patch("backend.projects.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(ProjectPersistenceError):
                self.storage.save_project(project["project_id"], {**project, "name": "Changed"})

        self.assertEqual(project_file.read_bytes(), original_bytes)
        self.assertEqual(
            list(project_file.parent.glob(".project.json.*.tmp")),
            [],
        )

    def test_timestamp_validation_requires_timezone(self):
        project = self.storage.create_project("Timestamp Project")
        invalid_timestamp = {**project, "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}

        with self.assertRaises(ProjectValidationError):
            validate_project_document(invalid_timestamp)


if __name__ == "__main__":
    unittest.main()
