import asyncio
import base64
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.entities import (
    ReferenceNotFoundError,
    add_reference,
    create_character,
    create_location,
    update_resource_draft,
)
from backend.projects import ProjectStorage, default_prompts_for_scenes
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.visuals import default_visuals_for_scenes, save_reference_selection


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT.joinpath("web", "extension.js").read_text(encoding="utf-8")
LAUNCHER = ROOT.joinpath("web", "gpt_launcher.js").read_bytes()


class FakeUpload:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    async def read_chunk(self, size: int) -> bytes:
        if self.position >= len(self.data):
            return b""
        end = min(self.position + size, len(self.data))
        chunk = self.data[self.position:end]
        self.position = end
        return chunk


def image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), color=(117, 87, 45))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class Phase7C4ResourceDraftTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.projects_root = Path(self.temp_directory.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    @staticmethod
    def character_payload(name="Vesper"):
        return {
            "name": name,
            "role": "performer",
            "appearance": "Copper hair and a focused expression.",
            "outfit": "Black jacket with antique-gold trim.",
        }

    @staticmethod
    def location_payload(name="Chapel"):
        return {"name": name, "description": "An obsidian chapel with antique-gold light."}

    def add_reference(self, project_id, kind, entity_id, filename):
        return asyncio.run(
            add_reference(
                self.storage,
                project_id,
                kind,
                entity_id,
                filename,
                FakeUpload(image_bytes()),
            )
        )

    def test_character_draft_removes_existing_and_keeps_retained_ids_order_and_unrelated_files(self):
        project = self.storage.create_project("Character draft")
        project = create_character(self.storage, project["project_id"], self.character_payload())
        project = create_character(self.storage, project["project_id"], self.character_payload("Crystal"))
        character_id = project["characters"][0]["character_id"]
        other_id = project["characters"][1]["character_id"]
        project = self.add_reference(project["project_id"], "characters", character_id, "first.png")
        project = self.add_reference(project["project_id"], "characters", character_id, "second.png")
        project = self.add_reference(project["project_id"], "characters", other_id, "other.png")
        first, second = project["characters"][0]["references"]
        other = project["characters"][1]["references"][0]
        first_path = self.projects_root / project["project_id"] / "references" / "characters" / character_id / first["stored_name"]
        other_path = self.projects_root / project["project_id"] / "references" / "characters" / other_id / other["stored_name"]

        saved = update_resource_draft(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            {**self.character_payload("Vesper Renamed"), "reference_ids": [second["reference_id"]]},
        )

        self.assertEqual(saved["characters"][0]["name"], "Vesper Renamed")
        self.assertEqual(saved["characters"][0]["references"], [second])
        self.assertEqual(saved["characters"][1]["references"], [other])
        self.assertFalse(first_path.exists())
        self.assertTrue(other_path.is_file())
        self.assertEqual(saved["schema_version"], 7)

    def test_location_draft_can_remove_final_reference_to_placeholder_state(self):
        project = self.storage.create_project("Location draft")
        project = create_location(self.storage, project["project_id"], self.location_payload())
        location_id = project["locations"][0]["location_id"]
        project = self.add_reference(project["project_id"], "locations", location_id, "chapel.png")
        reference = project["locations"][0]["references"][0]
        reference_path = self.projects_root / project["project_id"] / "references" / "locations" / location_id / reference["stored_name"]

        saved = update_resource_draft(
            self.storage,
            project["project_id"],
            "locations",
            location_id,
            {**self.location_payload(), "reference_ids": []},
        )

        self.assertEqual(saved["locations"][0]["references"], [])
        self.assertFalse(reference_path.exists())

    def test_existing_and_new_references_commit_as_one_ordered_resource_set(self):
        project = self.storage.create_project("Mixed draft")
        project = create_character(self.storage, project["project_id"], self.character_payload())
        character_id = project["characters"][0]["character_id"]
        project = self.add_reference(project["project_id"], "characters", character_id, "existing-a.png")
        project = self.add_reference(project["project_id"], "characters", character_id, "existing-b.png")
        existing_a, existing_b = project["characters"][0]["references"]
        project = self.add_reference(project["project_id"], "characters", character_id, "staged-new.png")
        staged_new = project["characters"][0]["references"][2]

        saved = update_resource_draft(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            {
                **self.character_payload(),
                "reference_ids": [existing_b["reference_id"], staged_new["reference_id"]],
            },
        )

        self.assertEqual(saved["characters"][0]["references"], [existing_b, staged_new])
        self.assertNotIn(existing_a, saved["characters"][0]["references"])
        with self.assertRaises(ReferenceNotFoundError):
            update_resource_draft(
                self.storage,
                project["project_id"],
                "characters",
                character_id,
                {**self.character_payload(), "reference_ids": [existing_a["reference_id"]]},
            )

    def test_saved_reference_removal_cleans_only_matching_selectors_and_stales_storyboard(self):
        project = self.storage.create_project("Reference invalidation")
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "Sing the line"}],
            4_000,
        )
        project = self.storage.save_project(
            project["project_id"],
            {
                **project,
                "source": {
                    "master_audio": {"stored_name": "master_audio.wav", "original_name": "master.wav", "duration_ms": 4_000},
                    "lyrics_srt": {"stored_name": "lyrics.srt", "original_name": "lyrics.srt", "cue_count": 1},
                },
                "scenes": scenes,
                "visuals": default_visuals_for_scenes(scenes),
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )
        project = create_character(self.storage, project["project_id"], self.character_payload())
        character_id = project["characters"][0]["character_id"]
        project = self.add_reference(project["project_id"], "characters", character_id, "required.png")
        project = self.add_reference(project["project_id"], "characters", character_id, "retained.png")
        removed, retained = project["characters"][0]["references"]
        selector = {"entity_type": "character", "entity_id": character_id, "reference_id": removed["reference_id"]}
        retained_selector = {"entity_type": "character", "entity_id": character_id, "reference_id": retained["reference_id"]}
        response = {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [{
                "scene_id": scenes[0]["scene_id"],
                "scene_type": "performance",
                "character_ids": [character_id],
                "location_id": None,
                "action": "The performer sings.",
                "visual_instructions": "Obsidian stage.",
                "camera_direction": "Slow push in.",
                "motion_direction": "Measured performance.",
                "continuity_notes": "Retain identity.",
                "required_references": [selector, retained_selector],
            }],
        }
        storyboard = validate_storyboard_response(project, response)
        project = self.storage.save_project(
            project["project_id"],
            {**project, "storyboard": storyboard},
            allow_storyboard_change=True,
        )
        project = save_reference_selection(
            self.storage,
            project["project_id"],
            scenes[0]["scene_id"],
            {"selected_references": [selector, retained_selector]},
        )
        original_storyboard_fingerprint = project["storyboard"]["request_fingerprint"]

        saved = update_resource_draft(
            self.storage,
            project["project_id"],
            "characters",
            character_id,
            {**self.character_payload(), "reference_ids": [retained["reference_id"]]},
        )

        self.assertEqual(saved["storyboard"]["scenes"][0]["required_references"], [retained_selector])
        self.assertEqual(saved["visuals"]["scenes"][0]["reference2video"]["selected_references"], [retained_selector])
        self.assertEqual(saved["storyboard"]["request_fingerprint"], original_storyboard_fingerprint)
        self.assertNotEqual(saved["storyboard"]["request_fingerprint"], request_fingerprint(saved))


class Phase7C4FrontendContractTestCase(unittest.TestCase):
    def run_launcher_script(self, script):
        module_url = "data:text/javascript;base64," + base64.b64encode(LAUNCHER).decode("ascii")
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", f"const launcher = await import('{module_url}');\n{script}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    @staticmethod
    def function_body(name, next_name):
        start = EXTENSION.index(name)
        return EXTENSION[start:EXTENSION.index(next_name, start)]

    def test_gpt_launcher_uses_exact_allowlisted_targets_and_one_attempt(self):
        self.run_launcher_script(
            """
const expected = {
  storyboard: "https://chatgpt.com/g/g-6a7c2ee4c49c81919ff52e70d0e6f58a-storyboard-director",
  prompts: "https://chatgpt.com/g/g-6a7dd81828588191955a0ef1ac196da7-prompt-director",
};
for (const [director, url] of Object.entries(expected)) {
  const path = launcher.gptLaunchPath(director);
  if (path !== `/music-video-builder/gpt/${director}/open`) throw new Error(director + " path");
  const result = { director, url, opened: true };
  if (!launcher.isSuccessfulGptLaunch(result, director)) throw new Error(director + " success contract");
}
if (launcher.gptLaunchPath("https://example.invalid") !== null) throw new Error("allowlist contract");
if (launcher.isSuccessfulGptLaunch(null, "storyboard")) throw new Error("null success");
if (launcher.isSuccessfulGptLaunch({ director: "storyboard", url: expected.storyboard, opened: false }, "storyboard")) throw new Error("closed success");
if (launcher.isSuccessfulGptLaunch({ director: "prompts", url: expected.prompts, opened: true }, "storyboard")) throw new Error("cross-director success");
"""
        )

    def test_open_gpt_has_one_registered_path_and_no_fallback_or_state_mutation(self):
        helper = self.function_body("function openDedicatedGpt(root, director)", "async function closeBuilder")
        self.assertEqual(helper.count("fetchJson(path"), 1)
        self.assertNotIn("window.open", helper)
        self.assertNotIn("document.createElement", helper)
        self.assertNotIn(".click()", helper)
        self.assertNotIn("setCurrentProject", helper)
        self.assertNotIn("storyboardRequest", helper)
        self.assertNotIn("promptDraft", helper)
        self.assertIn("if (!isSuccessfulGptLaunch(result, director))", helper)
        self.assertIn("clearGptLauncherErrors();", helper)
        self.assertIn("builderState.gptLaunchInFlight", helper)
        self.assertIn("GPT_LAUNCH_FAILURE_MESSAGE", helper)
        self.assertNotIn("allow pop-ups", helper)
        self.assertEqual(EXTENSION.count('openDedicatedGpt(root, "storyboard")'), 1)
        self.assertEqual(EXTENSION.count('openDedicatedGpt(root, "prompts")'), 1)

    def test_edit_modal_stages_existing_and_new_references_until_save_or_cancel(self):
        render = self.function_body("function appendReferenceList(root, parent, kind, entity)", "function appendDeleteControls")
        save = self.function_body("async function saveExistingResourceDraft", "async function saveCharacter")
        close = self.function_body("function closeResourceDialogs", "function openCharacterDialog")
        self.assertIn("setPendingResourceReferenceIds", render)
        self.assertIn("setPendingResourceFiles", render)
        self.assertNotIn("fetchJson", render)
        self.assertNotIn("removeReference", render)
        self.assertIn("reference_ids: [...retainedReferenceIds, ...uploadedReferenceIds]", save)
        self.assertIn("rollbackUploadedResourceReferences", save)
        self.assertIn("characterDialogReferenceIds = []", close)
        self.assertIn("locationDialogReferenceIds = []", close)
        self.assertIn("No reference images.", render)

    def test_compact_cards_and_preview_order_contract_remain(self):
        character_card = self.function_body("function appendCharacterCard(root, character)", "function appendLocationCard")
        location_card = self.function_body("function appendLocationCard(root, location)", "function findCharacter")
        for card in (character_card, location_card):
            self.assertIn("appendEntityPreview(root, card", card)
            self.assertNotIn("appendReferenceList", card)
        preview = self.function_body("function appendEntityPreview", "function renderResourceDialogReferences")
        self.assertIn("const reference = entity.references[0];", preview)
        self.assertIn('preview.textContent = "No image";', preview)


if __name__ == "__main__":
    unittest.main()
