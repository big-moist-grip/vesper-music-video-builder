import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.projects as projects_module
from backend.projects import ProjectPersistenceError, ProjectStorage


class ProjectStoragePortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.temp_root = Path(self.temp_directory.name)

    def test_missing_legacy_drive_uses_portable_default(self):
        portable_root = self.temp_root / "portable" / "projects"
        portable_state = portable_root.parent / "state"
        with (
            patch.object(projects_module, "DEFAULT_PROJECTS_ROOT", portable_root),
            patch.object(projects_module, "DEFAULT_STATE_ROOT", portable_state),
            patch.object(projects_module, "_legacy_default_root_is_available", return_value=False),
        ):
            storage = ProjectStorage()

        self.assertEqual(storage.projects_root, portable_root)
        self.assertEqual(storage.state_root, portable_state)
        self.assertNotEqual(storage.projects_root, projects_module.LEGACY_DEFAULT_PROJECTS_ROOT)

    def test_default_project_root_creates_project_without_legacy_drive(self):
        portable_root = self.temp_root / "portable" / "projects"
        portable_state = portable_root.parent / "state"
        with (
            patch.object(projects_module, "DEFAULT_PROJECTS_ROOT", portable_root),
            patch.object(projects_module, "DEFAULT_STATE_ROOT", portable_state),
            patch.object(projects_module, "_legacy_default_root_is_available", return_value=False),
        ):
            storage = ProjectStorage()
            project = storage.create_project("Portable Default")

        self.assertTrue((portable_root / project["project_id"] / "project.json").is_file())
        self.assertEqual(storage.list_projects()["projects"][0]["project_id"], project["project_id"])

    def test_explicit_valid_custom_root_remains_authoritative(self):
        custom_root = self.temp_root / "configured" / "projects"
        storage = ProjectStorage(custom_root)
        project = storage.create_project("Configured Root")

        self.assertEqual(storage.projects_root, custom_root)
        self.assertEqual(storage.state_root, custom_root.parent / "state")
        self.assertTrue((custom_root / project["project_id"] / "project.json").is_file())

    def test_explicit_unavailable_root_fails_with_path_and_cause(self):
        blocking_file = self.temp_root / "not-a-directory"
        blocking_file.write_text("blocking parent", encoding="utf-8")
        unavailable_root = blocking_file / "projects"
        storage = ProjectStorage(unavailable_root)

        with self.assertRaises(ProjectPersistenceError) as context:
            storage.create_project("Unavailable Root")

        error = context.exception
        self.assertIn(str(unavailable_root), str(error))
        self.assertIsInstance(error.__cause__, OSError)

    def test_existing_custom_root_projects_remain_compatible(self):
        custom_root = self.temp_root / "existing" / "projects"
        original = ProjectStorage(custom_root)
        project = original.create_project("Existing Project")

        reopened = ProjectStorage(custom_root)

        self.assertEqual(reopened.load_project(project["project_id"]), project)
        self.assertEqual(reopened.list_projects()["projects"][0]["project_id"], project["project_id"])

    def test_existing_legacy_default_projects_remain_discoverable(self):
        legacy_root = self.temp_root / "legacy" / "projects"
        legacy_state = legacy_root.parent / "state"
        original = ProjectStorage(legacy_root, legacy_state)
        project = original.create_project("Legacy Project")

        with (
            patch.object(projects_module, "LEGACY_DEFAULT_PROJECTS_ROOT", legacy_root),
            patch.object(projects_module, "LEGACY_DEFAULT_STATE_ROOT", legacy_state),
        ):
            reopened = ProjectStorage()

        self.assertEqual(reopened.projects_root, legacy_root)
        self.assertEqual(reopened.state_root, legacy_state)
        self.assertEqual(reopened.load_project(project["project_id"]), project)

    def test_project_creation_route_logs_storage_path_and_underlying_cause(self):
        routes_source = Path(__file__).parents[1].joinpath("backend", "routes.py").read_text(encoding="utf-8")

        self.assertIn("projects_root=%s", routes_source)
        self.assertIn("state_root=%s", routes_source)
        self.assertIn("error.__cause__", routes_source)


if __name__ == "__main__":
    unittest.main()
