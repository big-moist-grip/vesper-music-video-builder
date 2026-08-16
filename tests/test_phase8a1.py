import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from backend.projects import ProjectStorage, default_prompts_for_scenes, ProjectValidationError
from backend.prompt_service import prompt_scene_state
from backend.render import build_render_preflight
from backend.requirements import STATUS_AVAILABLE
from backend.scenes import build_scenes
from backend.storyboard import request_fingerprint, validate_storyboard_response
from backend.entities import update_resource_draft
from backend.visuals import (
    MAX_REF2VA_STILL_REFERENCES,
    build_keyframe_prompt,
    derive_visual_readiness,
    planned_reference_mapping,
    reconcile_project_reference_selections,
    reconcile_scene_reference_selection,
    save_reference_selection,
    synchronize_storyboard_required_references,
)
from backend.workflows import production_manifest_registry


ROOT = Path(__file__).parents[1]


class Phase8A1TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.storage = ProjectStorage(Path(self.temp_directory.name) / "projects")

    @staticmethod
    def _reference(original_name):
        reference_id = str(uuid.uuid4())
        return {
            "reference_id": reference_id,
            "stored_name": f"{reference_id}.png",
            "original_name": original_name,
        }

    @staticmethod
    def _selector(entity_type, entity, reference):
        id_field = "character_id" if entity_type == "character" else "location_id"
        return {
            "entity_type": entity_type,
            "entity_id": entity[id_field],
            "reference_id": reference["reference_id"],
        }

    def _project(self, *, method="reference2video", selected=None, extra_character_references=0):
        project = self.storage.create_project("Phase 8A.1 synchronization")
        scenes = build_scenes(
            [{"cue_number": 1, "start_ms": 0, "end_ms": 4_000, "text": "No god in the house."}],
            4_000,
        )
        character_id = str(uuid.uuid4())
        secondary_character_id = str(uuid.uuid4())
        location_id = str(uuid.uuid4())
        character_references = [self._reference("Vesper.png"), self._reference("Vesper close.png")]
        character_references.extend(
            self._reference(f"Vesper extra {index}.png")
            for index in range(2, 2 + extra_character_references)
        )
        secondary_character_references = [
            self._reference("Bassist.png"),
            self._reference("Bassist close.png"),
        ]
        location_references = [self._reference("The House After God.png"), self._reference("Chapel wide.png")]
        character = {
            "character_id": character_id,
            "name": "Vesper",
            "role": "performer",
            "appearance": "Copper hair and a slim build.",
            "outfit": "Black jacket and dark boots.",
            "references": character_references,
        }
        secondary_character = {
            "character_id": secondary_character_id,
            "name": "Bassist",
            "role": "band_member",
            "appearance": "A restrained silhouette with a bass guitar.",
            "outfit": "Charcoal shirt and worn boots.",
            "references": secondary_character_references,
        }
        location = {
            "location_id": location_id,
            "name": "Chapel",
            "description": "Cold stone walls and reflective water.",
            "references": location_references,
        }
        visuals = {"scenes": [{
            "scene_id": scenes[0]["scene_id"],
            "generation_method": method,
            "keyframe_i2v": {
                "keyframe_generation_prompt": "",
                "intended_keyframe_description": "",
                "accepted_keyframe": None,
                "actual_keyframe_description": "",
            },
            "reference2video": {"selected_references": list(selected or [])},
        }]}
        root = self.storage.project_directory(project["project_id"])
        (root / "source").mkdir(parents=True, exist_ok=True)
        (root / "source" / "master_audio.wav").write_bytes(b"authoritative master audio")
        for collection, entity_id, references in (
            ("characters", character_id, character_references),
            ("characters", secondary_character_id, secondary_character_references),
            ("locations", location_id, location_references),
        ):
            reference_directory = root / "references" / collection / entity_id
            reference_directory.mkdir(parents=True, exist_ok=True)
            for reference in references:
                (reference_directory / reference["stored_name"]).write_bytes(b"reference still")

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
                "characters": [character, secondary_character],
                "locations": [location],
                "visuals": visuals,
                "prompts": default_prompts_for_scenes(scenes),
            },
            allow_visuals_change=True,
        )

    def _storyboard_response(self, project, required, *, character_ids=None):
        scene = project["scenes"][0]
        character = project["characters"][0]
        location = project["locations"][0]
        return {
            "storyboard_response_version": 1,
            "project_id": project["project_id"],
            "request_fingerprint": request_fingerprint(project),
            "scenes": [{
                "scene_id": scene["scene_id"],
                "scene_type": "performance",
                "character_ids": character_ids or [character["character_id"]],
                "location_id": location["location_id"],
                "action": "Vesper holds the frame.",
                "visual_instructions": "Cold gothic performance staging.",
                "camera_direction": "Slow lateral drift.",
                "motion_direction": "Restrained forward movement.",
                "continuity_notes": "Keep the performer and chapel consistent.",
                "required_references": required,
            }],
        }

    def _apply_storyboard(self, project, required, *, character_ids=None):
        storyboard = validate_storyboard_response(
            project,
            self._storyboard_response(project, required, character_ids=character_ids),
        )
        candidate = synchronize_storyboard_required_references({**project, "storyboard": storyboard})
        over_capacity_scene_ids = {
            visual_scene["scene_id"]
            for visual_scene in candidate["visuals"]["scenes"]
            if len(visual_scene["reference2video"]["selected_references"]) > MAX_REF2VA_STILL_REFERENCES
        }
        return self.storage.save_project(
            project["project_id"],
            candidate,
            allow_storyboard_change=True,
            allow_visuals_change=True,
            allow_visual_over_capacity_scene_ids=over_capacity_scene_ids,
        )

    def _requirements(self):
        registry = production_manifest_registry()
        methods = {}
        for method, manifest in registry.items():
            methods[method] = {
                "workflow_id": manifest["workflow_id"],
                "required_nodes": [
                    {"node_type": node_type, "status": STATUS_AVAILABLE}
                    for node_type in manifest["required_node_types"]
                ],
                "required_models": [
                    {
                        "role": declaration["role"],
                        "category": declaration["category"],
                        "filename": declaration["filename"],
                        "status": STATUS_AVAILABLE,
                    }
                    for declaration in manifest["required_models"]
                ],
                "ready": True,
            }
        return {
            "response_version": 1,
            "source": "test.phase8a1",
            "methods": methods,
            "required_tools": {
                name: {"name": name, "status": STATUS_AVAILABLE}
                for name in ("ffmpeg", "ffprobe")
            },
            "required_ready": True,
            "shared": {"nodes": [], "models": []},
            "optional": {},
        }

    def _save_current_ref2va_prompt(self, project):
        scene_id = project["scenes"][0]["scene_id"]
        state = prompt_scene_state(
            project,
            scene_id,
            storage=self.storage,
            generation_method="reference2video",
        )
        prompts = copy.deepcopy(project["prompts"])
        prompts["scenes"][0]["reference2video"] = {
            "final_prompt": "A current production prompt.",
            "source_fingerprint": state["source_fingerprint"],
            "relay_fingerprint": state["source_fingerprint"],
        }
        return self.storage.save_project(
            project["project_id"],
            {**project, "prompts": prompts},
            allow_prompts_change=True,
        )

    def test_apply_seeds_required_references_in_storyboard_order_and_is_idempotent(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        storyboard_markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        optional = [
            self._selector("character", secondary_character, secondary_character["references"][1]),
            self._selector("character", secondary_character, secondary_character["references"][0]),
        ]
        project = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": {"scenes": [{
                **project["visuals"]["scenes"][0],
                "reference2video": {"selected_references": [optional[0], optional[1]]},
            }]}},
            allow_visuals_change=True,
        )
        applied = self._apply_storyboard(
            project,
            storyboard_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        selected = applied["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(selected, [*required, *optional])
        self.assertEqual(
            len({(item["entity_type"], item["entity_id"], item["reference_id"]) for item in selected}),
            len(selected),
        )
        reapplied = self._apply_storyboard(
            applied,
            storyboard_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        self.assertEqual(reapplied["visuals"], applied["visuals"])
        self.assertEqual(
            applied["storyboard"]["request_fingerprint"],
            request_fingerprint(applied),
        )

    def test_reapply_preserves_extras_and_old_required_references_as_optional(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        first_markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": {"scenes": [{
                **project["visuals"]["scenes"][0],
                "reference2video": {
                    "selected_references": [
                        self._selector("character", secondary_character, secondary_character["references"][1]),
                        self._selector("character", secondary_character, secondary_character["references"][0]),
                    ],
                },
            }]}},
            allow_visuals_change=True,
        )
        project = self._apply_storyboard(
            project,
            first_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        first_selected = project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        second_markers = [
            self._selector("location", location, location["references"][0]),
        ]
        reapplied = self._apply_storyboard(
            project,
            second_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        selected = reapplied["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(
            selected,
            [
                self._selector("location", location, reference)
                for reference in location["references"]
            ] + [
                selector
                for selector in first_selected
                if selector["entity_id"] != location["location_id"]
            ],
        )

    def test_required_and_optional_reference_guardrails(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        optional = [
            self._selector("character", secondary_character, secondary_character["references"][0]),
            self._selector("character", secondary_character, secondary_character["references"][1]),
        ]
        project = self._apply_storyboard(
            project,
            markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        scene_id = project["scenes"][0]["scene_id"]
        selected = [*required, *optional]
        guarded_remove = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": selected[1:]},
        )
        self.assertEqual(
            guarded_remove["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            selected,
        )
        guarded_reorder = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": [selected[1], selected[0], *selected[2:]]},
        )
        self.assertEqual(
            guarded_reorder["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            selected,
        )
        reordered = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": [*required, optional[1], optional[0]]},
        )
        self.assertEqual(
            reordered["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            [*required, optional[1], optional[0]],
        )
        removed = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": [*required, optional[1]]},
        )
        self.assertEqual(
            removed["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            [*required, optional[1]],
        )
        crossed = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": [optional[0], *required, optional[1]]},
        )
        self.assertEqual(
            crossed["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            [*required, optional[0], optional[1]],
        )

    def test_legacy_missing_required_image_is_reconciled_when_visuals_are_saved(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self._apply_storyboard(project, markers)
        scene_id = project["scenes"][0]["scene_id"]
        incomplete = [
            self._selector("character", character, character["references"][0]),
            *[
                self._selector("location", location, reference)
                for reference in location["references"]
            ],
        ]
        repaired = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": incomplete},
        )
        self.assertEqual(
            repaired["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            [
                self._selector("character", character, reference)
                for reference in character["references"]
            ] + [
                self._selector("location", location, reference)
                for reference in location["references"]
            ],
        )

    def test_dynamic_image_on_required_owner_is_added_on_reapply_after_resource_change(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self._apply_storyboard(project, markers)
        added_reference = self._reference("Vesper newly added.png")
        reference_directory = (
            self.storage.project_directory(project["project_id"])
            / "references"
            / "characters"
            / character["character_id"]
        )
        (reference_directory / added_reference["stored_name"]).write_bytes(b"reference still")
        changed_character = {**character, "references": [*character["references"], added_reference]}
        stale = self.storage.save_project(
            project["project_id"],
            {**project, "characters": [changed_character, project["characters"][1]]},
        )
        self.assertEqual(
            stale["visuals"]["scenes"][0]["reference2video"]["selected_references"][2]["reference_id"],
            added_reference["reference_id"],
        )
        reapplied = self._apply_storyboard(stale, markers)
        selected = reapplied["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(selected[2]["reference_id"], added_reference["reference_id"])
        self.assertEqual(reapplied["storyboard"]["request_fingerprint"], request_fingerprint(reapplied))

    def test_resource_image_removal_clears_visual_selector_without_ghost(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self._apply_storyboard(project, markers)
        removed_reference = character["references"][1]
        project = update_resource_draft(
            self.storage,
            project["project_id"],
            "characters",
            character["character_id"],
            {
                "name": character["name"],
                "role": character["role"],
                "appearance": character["appearance"],
                "outfit": character["outfit"],
                "reference_ids": [character["references"][0]["reference_id"]],
            },
        )
        selected_ids = {
            selector["reference_id"]
            for selector in project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        }
        self.assertNotIn(removed_reference["reference_id"], selected_ids)

    def test_required_owner_context_includes_all_current_images_for_i2v_prompt(self):
        project = self._project(method="keyframe_i2v")
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        )
        prompt = build_keyframe_prompt(project, project["scenes"][0]["scene_id"])
        self.assertIn("Vesper — Vesper close.png", prompt)
        self.assertIn("Chapel — Chapel wide.png", prompt)

    def test_required_over_capacity_has_render_specific_blocker_without_truncation(self):
        project = self._project(extra_character_references=8)
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        )
        report = build_render_preflight(
            project,
            self.storage,
            requirements_report=self._requirements(),
        )
        scene = report["scenes"][0]
        self.assertIn("REF2VA_REQUIRED_REFERENCE_LIMIT", {item["code"] for item in scene["blockers"]})
        self.assertEqual(len(project["visuals"]["scenes"][0]["reference2video"]["selected_references"]), 12)

    def test_synchronization_does_not_add_schema_or_per_image_required_state(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        applied = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        )
        self.assertEqual(applied["schema_version"], project["schema_version"])
        self.assertTrue(
            all(set(selector) == {"entity_type", "entity_id", "reference_id"}
                for selector in applied["visuals"]["scenes"][0]["reference2video"]["selected_references"])
        )

    def test_save_reopen_preserves_synchronized_visual_state(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        applied = self._apply_storyboard(project, markers)
        reopened = self.storage.load_project(applied["project_id"])
        self.assertEqual(reopened["visuals"], applied["visuals"])
        self.assertEqual(
            reopened["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            required,
        )

    def test_over_capacity_sync_preserves_every_required_reference_and_blocks_truthfully(self):
        project = self._project(extra_character_references=8)
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        applied = self._apply_storyboard(project, markers)
        selected = applied["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected, required)
        readiness = derive_visual_readiness(applied, applied["scenes"][0]["scene_id"], self.storage)
        self.assertFalse(readiness["ready"])
        self.assertIn(
            "Storyboard-required references require 12 images, exceeding the Reference-to-Video maximum of 9.",
            readiness["missing"],
        )

    def test_unresolved_required_asset_has_specific_blocker(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        required = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        applied = self._apply_storyboard(project, required)
        missing_reference = location["references"][0]
        missing_path = (
            self.storage.project_directory(applied["project_id"])
            / "references"
            / "locations"
            / location["location_id"]
            / missing_reference["stored_name"]
        )
        missing_path.unlink()
        readiness = derive_visual_readiness(applied, applied["scenes"][0]["scene_id"], self.storage)
        self.assertIn(
            "Required storyboard reference 'Chapel · The House After God.png' could not be resolved.",
            readiness["missing"],
        )

    def test_visuals_only_change_keeps_storyboard_current_but_changes_prompt_freshness(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        project = self._apply_storyboard(
            project,
            markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        project = self._save_current_ref2va_prompt(project)
        storyboard_fingerprint = project["storyboard"]["request_fingerprint"]
        scene_id = project["scenes"][0]["scene_id"]
        before_prompt = prompt_scene_state(project, scene_id, storage=self.storage, generation_method="reference2video")
        optional = self._selector("character", secondary_character, secondary_character["references"][0])
        changed = save_reference_selection(
            self.storage,
            project["project_id"],
            scene_id,
            {"selected_references": [*required, optional]},
        )
        after_prompt = prompt_scene_state(changed, scene_id, storage=self.storage, generation_method="reference2video")
        self.assertEqual(changed["storyboard"]["request_fingerprint"], storyboard_fingerprint)
        self.assertNotEqual(before_prompt["source_fingerprint"], after_prompt["source_fingerprint"])
        render_scene = build_render_preflight(
            changed,
            self.storage,
            requirements_report=self._requirements(),
        )["scenes"][0]
        self.assertIn("PROMPT_STALE", {item["code"] for item in render_scene["blockers"]})
        self.assertFalse(render_scene["preparation_ready"])

    def test_render_preflight_accepts_auto_synchronized_reference_scene(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        project = self._apply_storyboard(project, markers)
        project = self._save_current_ref2va_prompt(project)
        selected = project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(selected, required)
        scene = build_render_preflight(
            project,
            self.storage,
            requirements_report=self._requirements(),
        )["scenes"][0]
        self.assertTrue(scene["visuals_ready"])
        self.assertTrue(scene["preparation_ready"])

    def test_picture_numbering_and_subject_ownership_follow_synchronized_order(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        project = self._apply_storyboard(project, markers)
        mapping = planned_reference_mapping(project, project["scenes"][0]["scene_id"])
        self.assertEqual([item["picture_tag"] for item in mapping], ["<Picture 1>", "<Picture 2>", "<Picture 3>", "<Picture 4>"])
        self.assertEqual([item["subject_tag"] for item in mapping], ["<Subject 1>", "<Subject 1>", "<Subject 2>", "<Subject 2>"])

    def test_i2v_keeps_keyframe_authority_while_syncing_reference_context(self):
        project = self._project(method="keyframe_i2v")
        character = project["characters"][0]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        required = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        project = self._apply_storyboard(project, markers)
        visual = project["visuals"]["scenes"][0]
        self.assertEqual(visual["reference2video"]["selected_references"], required)
        self.assertIsNone(visual["keyframe_i2v"]["accepted_keyframe"])
        prompt = build_keyframe_prompt(project, project["scenes"][0]["scene_id"])
        self.assertIn("Reference guidance", prompt)
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")
        self.assertIn('"role": "keyframe"', render_source)
        self.assertNotIn('"role": "reference"', render_source.split('if method == "keyframe_i2v":', 1)[1].split('try:', 1)[0])

    def test_authoritative_scene_reconciler_repairs_missing_late_duplicate_and_promoted_images(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("character", secondary_character, secondary_character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self._apply_storyboard(
            project,
            markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        malformed = [
            self._selector("character", character, character["references"][0]),
            self._selector("character", secondary_character, secondary_character["references"][0]),
            self._selector("location", location, location["references"][0]),
            self._selector("character", character, character["references"][1]),
            self._selector("character", character, character["references"][0]),
        ]
        canonical = reconcile_scene_reference_selection(
            project,
            project["scenes"][0]["scene_id"],
            malformed,
        )
        expected = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("character", secondary_character, reference)
            for reference in secondary_character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        self.assertEqual(canonical, expected)

    def test_project_load_self_heals_legacy_misorder_missing_image_and_duplicate(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        project = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("character", secondary_character, secondary_character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        canonical = project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        malformed = copy.deepcopy(project)
        malformed["visuals"]["scenes"][0]["reference2video"]["selected_references"] = [
            canonical[0],
            canonical[2],
            canonical[4],
            canonical[1],
            canonical[0],
        ]
        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        project_file.write_text(json.dumps(malformed), encoding="utf-8")

        loaded = self.storage.load_project(project["project_id"])
        loaded_selected = loaded["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        self.assertEqual(loaded_selected, canonical)
        reopened = self.storage.load_project(project["project_id"])
        self.assertEqual(
            reopened["visuals"]["scenes"][0]["reference2video"]["selected_references"],
            canonical,
        )
        self.assertEqual(reopened["updated_at"], loaded["updated_at"])

    def test_reconciliation_change_stales_prompt_but_not_storyboard(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        )
        canonical = project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        malformed = copy.deepcopy(project)
        malformed["visuals"]["scenes"][0]["reference2video"]["selected_references"] = [
            canonical[2], canonical[0], canonical[1], canonical[3],
        ]
        malformed_prompt = prompt_scene_state(
            malformed,
            project["scenes"][0]["scene_id"],
            storage=self.storage,
            generation_method="reference2video",
        )
        malformed["prompts"]["scenes"][0]["reference2video"] = {
            "final_prompt": "Prompt saved against the legacy ordering.",
            "source_fingerprint": malformed_prompt["source_fingerprint"],
            "relay_fingerprint": malformed_prompt["source_fingerprint"],
        }
        project_file = self.storage.project_directory(project["project_id"]) / "project.json"
        project_file.write_text(json.dumps(malformed), encoding="utf-8")

        loaded = self.storage.load_project(project["project_id"])
        state = prompt_scene_state(
            loaded,
            loaded["scenes"][0]["scene_id"],
            storage=self.storage,
            generation_method="reference2video",
        )
        self.assertEqual(state["status"], "stale")
        self.assertEqual(loaded["storyboard"]["request_fingerprint"], request_fingerprint(loaded))

    def test_owner_becoming_required_promotes_all_current_images_once(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        first_markers = [
            self._selector("character", character, character["references"][0]),
            self._selector("location", location, location["references"][0]),
        ]
        project = self.storage.save_project(
            project["project_id"],
            {**project, "visuals": {"scenes": [{
                **project["visuals"]["scenes"][0],
                "reference2video": {
                    "selected_references": [
                        self._selector("character", secondary_character, secondary_character["references"][0]),
                    ],
                },
            }]}},
            allow_visuals_change=True,
        )
        project = self._apply_storyboard(
            project,
            first_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        second_markers = [
            *first_markers[:1],
            self._selector("character", secondary_character, secondary_character["references"][0]),
            first_markers[1],
        ]
        project = self._apply_storyboard(
            project,
            second_markers,
            character_ids=[character["character_id"], secondary_character["character_id"]],
        )
        selected = project["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        expected = [
            self._selector("character", character, reference)
            for reference in character["references"]
        ] + [
            self._selector("character", secondary_character, reference)
            for reference in secondary_character["references"]
        ] + [
            self._selector("location", location, reference)
            for reference in location["references"]
        ]
        self.assertEqual(selected, expected)

    def test_scene_specific_required_owner_blocks_are_isolated(self):
        project = self._project()
        character = project["characters"][0]
        secondary_character = project["characters"][1]
        location = project["locations"][0]
        scene_one_id = project["scenes"][0]["scene_id"]
        scene_two_id = str(uuid.uuid4())
        scene_one = {
            "scene_id": scene_one_id,
            "character_ids": [character["character_id"], secondary_character["character_id"]],
            "location_id": location["location_id"],
            "required_references": [
                self._selector("character", character, character["references"][0]),
                self._selector("character", secondary_character, secondary_character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        }
        scene_two = {
            "scene_id": scene_two_id,
            "character_ids": [character["character_id"], secondary_character["character_id"]],
            "location_id": location["location_id"],
            "required_references": [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        }
        project = {
            **project,
            "storyboard": {"request_fingerprint": request_fingerprint(project), "scenes": [scene_one, scene_two]},
            "visuals": {"scenes": [
                project["visuals"]["scenes"][0],
                {
                    **project["visuals"]["scenes"][0],
                    "scene_id": scene_two_id,
                    "reference2video": {
                        "selected_references": [
                            self._selector("character", secondary_character, secondary_character["references"][0]),
                        ],
                    },
                },
            ]},
        }
        reconciled = reconcile_project_reference_selections(project)
        scene_one_selected = reconciled["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        scene_two_selected = reconciled["visuals"]["scenes"][1]["reference2video"]["selected_references"]
        self.assertEqual(
            [selector["entity_id"] for selector in scene_one_selected],
            [character["character_id"]] * 2
            + [secondary_character["character_id"]] * 2
            + [location["location_id"]] * 2,
        )
        self.assertEqual(
            scene_two_selected,
            [
                self._selector("character", character, reference)
                for reference in character["references"]
            ] + [
                self._selector("location", location, reference)
                for reference in location["references"]
            ] + [
                self._selector("character", secondary_character, secondary_character["references"][0]),
            ],
        )

    def test_visuals_and_render_do_not_repair_state_during_preflight(self):
        project = self._project()
        character = project["characters"][0]
        location = project["locations"][0]
        project = self._apply_storyboard(
            project,
            [
                self._selector("character", character, character["references"][0]),
                self._selector("location", location, location["references"][0]),
            ],
        )
        malformed = copy.deepcopy(project)
        selected = malformed["visuals"]["scenes"][0]["reference2video"]["selected_references"]
        malformed["visuals"]["scenes"][0]["reference2video"]["selected_references"] = [
            selected[2], selected[0], selected[1], selected[3],
        ]
        before = copy.deepcopy(malformed["visuals"])
        build_render_preflight(
            malformed,
            self.storage,
            requirements_report=self._requirements(),
        )
        self.assertEqual(malformed["visuals"], before)

    def test_canonical_reconciliation_is_the_only_required_order_path(self):
        visuals_source = (ROOT / "backend" / "visuals.py").read_text(encoding="utf-8")
        projects_source = (ROOT / "backend" / "projects.py").read_text(encoding="utf-8")
        extension_source = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertIn("def reconcile_scene_reference_selection", visuals_source)
        self.assertIn("reconcile_project_reference_selections", projects_source)
        self.assertNotIn("_reconcile_required_reference_selection", visuals_source)
        self.assertNotIn("Required Storyboard references must remain first in Storyboard order.", visuals_source)
        self.assertNotIn("Required Storyboard references must remain first in Storyboard order.", extension_source)
        self.assertIn("reconcile_scene_reference_selection", visuals_source)

    def test_apply_route_and_frontend_expose_the_guarded_synchronization_contract(self):
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
        self.assertIn("synchronize_storyboard_required_references", routes)
        self.assertIn("allow_visual_over_capacity_scene_ids", routes)
        self.assertIn("mvb-visual-required-badge", extension)
        self.assertIn("disabled || isRequired", extension)
        self.assertNotIn("A required storyboard reference is not selected", extension)
        self.assertIn("mvb-visual-selected-reference-required", styles)

    def test_phase8a_keeps_no_queue_boundary(self):
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")
        self.assertNotIn("queue_prompt", render_source)
        self.assertNotIn("PromptServer", render_source)
        self.assertIn('"queue_submitted": False', render_source)


if __name__ == "__main__":
    unittest.main()
