"""Phase 8D batch planner and batch start contracts."""

from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_BLOCKED,
    ACTION_FINALIZE_RAW,
    ACTION_PREPARE_AND_RENDER,
    ACTION_RENDER_PREPARED,
    BATCH_RUNNING,
    BatchStore,
    RenderBatchConflict,
    RenderBatchError,
    plan_batch,
    preview_batch,
    start_batch,
)

from phase8d_base import FINGERPRINT, Phase8DBase


class BatchPlannerTests(Phase8DBase):
    def _plan(self, scene_ids=None):
        preflight = self.preflight_builder(None, self.storage)
        return plan_batch(
            self.storage,
            self.project_id,
            scene_ids=scene_ids,
            preflight=preflight,
            jobs=[],
            raw_output_root=self.raw_root,
        )

    def test_final_current_scene_requires_no_work(self):
        job = self.make_succeeded_job_with_final(self.scene_ids[0])
        self.finalize_job_real(job)
        preflight = self.preflight_builder(None, self.storage)
        from backend.render_jobs import JobStore

        plan = plan_batch(
            self.storage,
            self.project_id,
            preflight=preflight,
            jobs=JobStore(self.storage).list_project(self.project_id),
            raw_output_root=self.raw_root,
        )
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[0])
        self.assertEqual(entry["action"], ACTION_ALREADY_COMPLETE)
        self.assertEqual(plan["counts"]["already_complete"], 1)

    def test_raw_current_scene_plans_finalization_only(self):
        self.make_succeeded_job_with_final(self.scene_ids[0])
        preflight = self.preflight_builder(None, self.storage)
        from backend.render_jobs import JobStore

        plan = plan_batch(
            self.storage,
            self.project_id,
            preflight=preflight,
            jobs=JobStore(self.storage).list_project(self.project_id),
            raw_output_root=self.raw_root,
        )
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[0])
        self.assertEqual(entry["action"], ACTION_FINALIZE_RAW)
        self.assertEqual(plan["counts"]["finalize_only"], 1)
        self.assertEqual(plan["counts"]["needs_render"], 2)

    def test_preparation_current_scene_plans_direct_render(self):
        plan = self._plan()
        for entry in plan["scenes"]:
            self.assertEqual(entry["action"], ACTION_RENDER_PREPARED)
        self.assertEqual(plan["counts"]["needs_render"], 3)
        self.assertEqual(plan["counts"]["finalize_only"], 0)

    def test_content_ready_unprepared_scene_plans_prepare_and_render(self):
        self.scene_overrides[self.scene_ids[1]] = {"preparation_status": "stale"}
        plan = self._plan()
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[1])
        self.assertEqual(entry["action"], ACTION_PREPARE_AND_RENDER)

    def test_stale_prompt_blocks_scene(self):
        self.scene_overrides[self.scene_ids[0]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "PROMPT_NOT_CURRENT", "message": "Prompt is stale.", "layer": "prompt"}],
        }
        plan = self._plan()
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[0])
        self.assertEqual(entry["action"], ACTION_BLOCKED)
        self.assertEqual(entry["reason_code"], "PROMPT_NOT_CURRENT")

    def test_invalid_visuals_block_scene(self):
        self.scene_overrides[self.scene_ids[0]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "VISUALS_INVALID", "message": "Visuals are invalid.", "layer": "visuals"}],
        }
        plan = self._plan()
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[0])
        self.assertEqual(entry["action"], ACTION_BLOCKED)
        self.assertEqual(entry["reason_code"], "VISUALS_INVALID")

    def test_missing_required_references_block_scene(self):
        self.scene_overrides[self.scene_ids[2]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "REQUIRED_REFERENCE_MISSING", "message": "A required reference is missing.", "layer": "visuals"}],
        }
        plan = self._plan()
        entry = next(item for item in plan["scenes"] if item["scene_id"] == self.scene_ids[2])
        self.assertEqual(entry["action"], ACTION_BLOCKED)
        self.assertEqual(entry["reason_code"], "REQUIRED_REFERENCE_MISSING")

    def test_canonical_scene_order_preserved(self):
        plan = self._plan()
        self.assertEqual([entry["scene_id"] for entry in plan["scenes"]], self.scene_ids)
        self.assertEqual([entry["sequence"] for entry in plan["scenes"]], [1, 2, 3])

    def test_selected_subset_preserves_canonical_order(self):
        reversed_selection = [self.scene_ids[2], self.scene_ids[0]]
        plan = self._plan(scene_ids=reversed_selection)
        self.assertEqual([entry["scene_id"] for entry in plan["scenes"]], [self.scene_ids[0], self.scene_ids[2]])

    def test_preview_and_start_share_the_same_planner(self):
        preview = preview_batch(self.storage, self.project_id, client=self.client, preflight_builder=self.preflight_builder, raw_output_root=self.raw_root)
        started = self.start()
        batch_actions = {item["scene_id"]: item["action"] for item in self.latest_record()["items"]}
        preview_actions = {entry["scene_id"]: entry["action"] for entry in preview["scenes"]}
        self.assertEqual(batch_actions, preview_actions)
        self.assertEqual(started["batch"]["counts"]["total"], len(preview["scenes"]))


class BatchStartTests(Phase8DBase):
    def test_all_ready_selects_only_eligible_scenes(self):
        self.scene_overrides[self.scene_ids[1]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "PROMPT_NOT_CURRENT", "message": "stale", "layer": "prompt"}],
        }
        result = self.start()
        record = self.latest_record()
        selected = [item["scene_id"] for item in record["items"]]
        self.assertEqual(selected, [self.scene_ids[0], self.scene_ids[2]])
        self.assertEqual(result["batch"]["counts"]["total"], 2)

    def test_final_current_scenes_are_not_rerendered(self):
        job = self.make_succeeded_job_with_final(self.scene_ids[0])
        self.finalize_job_real(job)
        result = self.start()
        record = self.latest_record()
        self.assertNotIn(self.scene_ids[0], [item["scene_id"] for item in record["items"]])
        self.assertEqual(result["batch"]["counts"]["total"], 2)

    def test_blocked_scenes_are_excluded(self):
        for scene_id in self.scene_ids:
            self.scene_overrides[scene_id] = {
                "content_ready": False,
                "preparation_ready": False,
                "blockers": [{"code": "CONTENT_NOT_READY", "message": "blocked", "layer": "content"}],
            }
        with self.assertRaises(RenderBatchError) as context:
            self.start()
        self.assertEqual(context.exception.code, "BATCH_NO_ELIGIBLE_SCENES")
        self.assertIsNone(self.active_record())

    def test_selected_mode_validates_owned_scene_ids(self):
        result = self.start(scene_ids=[self.scene_ids[1]])
        record = self.latest_record()
        self.assertEqual([item["scene_id"] for item in record["items"]], [self.scene_ids[1]])
        self.assertEqual(record["mode"], "selected")
        self.assertEqual(result["batch"]["counts"]["total"], 1)

    def test_foreign_scene_id_is_rejected(self):
        import uuid as _uuid

        foreign = str(_uuid.uuid4())
        with self.assertRaises(RenderBatchError) as context:
            self.start(scene_ids=[foreign])
        self.assertEqual(context.exception.code, "SCENE_NOT_OWNED")
        self.assertIsNone(self.active_record())

    def test_active_project_job_blocks_batch_start(self):
        job = self.submit_single(self.scene_ids[0])
        self.assertIn(job["state"], {"QUEUED", "RUNNING"})
        with self.assertRaises(RenderBatchConflict) as context:
            self.start()
        self.assertEqual(context.exception.code, "PROJECT_JOB_ACTIVE")

    def test_active_batch_blocks_second_batch(self):
        self.start()
        with self.assertRaises(RenderBatchConflict) as context:
            self.start()
        self.assertEqual(context.exception.code, "BATCH_ACTIVE")
        records = BatchStore(self.storage).list_project(self.project_id)
        self.assertEqual(len(records), 1)

    def test_confirmation_preview_reports_truthful_counts(self):
        self.scene_overrides[self.scene_ids[0]] = {"preparation_status": "stale"}
        job = self.make_succeeded_job_with_final(self.scene_ids[2])
        preview = preview_batch(self.storage, self.project_id, client=self.client, preflight_builder=self.preflight_builder, raw_output_root=self.raw_root)
        counts = preview["counts"]
        self.assertEqual(counts["selected"], 3)
        self.assertEqual(counts["eligible"], 3)
        self.assertEqual(counts["needs_render"], 2)
        self.assertEqual(counts["finalize_only"], 1)
        self.assertEqual(counts["blocked"], 0)

    def test_start_revalidates_scene_state_again(self):
        preview = preview_batch(self.storage, self.project_id, client=self.client, preflight_builder=self.preflight_builder, raw_output_root=self.raw_root)
        self.assertEqual(preview["counts"]["eligible"], 3)
        self.scene_overrides[self.scene_ids[0]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "PROMPT_NOT_CURRENT", "message": "stale", "layer": "prompt"}],
        }
        result = self.start()
        self.assertEqual(result["batch"]["counts"]["total"], 2)
        self.assertNotIn(self.scene_ids[0], [item["scene_id"] for item in self.latest_record()["items"]])

    def test_batch_state_is_durable_project_owned_runtime_state(self):
        self.start()
        record = self.latest_record()
        batch_file = self.project_root / "renders" / "batches" / f"{record['batch_id']}.json"
        self.assertTrue(batch_file.is_file())
        self.assertTrue((self.project_root / "renders" / "batches" / "batch_active.json").is_file())
        project_json = self.storage.load_project(self.project_id)
        self.assertNotIn("batch", project_json)

    def test_public_batch_payload_exposes_no_batch_uuid(self):
        result = self.start()
        payload = result["batch"]
        self.assertNotIn("batch_id", payload)
        for item in payload["items"]:
            self.assertNotIn("job_id", item)
