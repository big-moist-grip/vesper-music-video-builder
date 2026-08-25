"""Phase 8D batch security and manual-action gating contracts."""

import inspect
from pathlib import Path

from backend.projects import ProjectStorage
from backend.routes import parse_batch_selection_payload
from backend.render_batch import (
    BATCH_PAUSED,
    BATCH_RUNNING,
    BATCH_TICK_TRANSITION_BUDGET,
    ITEM_RENDERING,
    BatchStore,
    RenderBatchConflict,
    RenderBatchError,
    batch_status,
    manual_action_blocker,
    pause_batch_after_current,
    plan_batch,
    preview_batch,
    resume_batch,
    retry_failed_batch,
    start_batch,
)

from phase8d_base import Phase8DBase


ROOT = Path(__file__).parents[1]
ROUTES_PY = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")


class BatchSecurityTests(Phase8DBase):
    def test_arbitrary_workflow_cannot_be_injected(self):
        for api in (start_batch, preview_batch, plan_batch):
            with self.assertRaises(TypeError):
                api(self.storage, self.project_id, workflow={"1": {"class_type": "X", "inputs": {}}})

    def test_arbitrary_prompt_id_cannot_be_injected(self):
        for api in (start_batch, preview_batch, resume_batch, retry_failed_batch):
            with self.assertRaises(TypeError):
                api(self.storage, self.project_id, prompt_id="attacker-prompt")

    def test_job_ids_from_other_projects_are_rejected(self):
        from backend.render_batch import _validate_batch_record

        self.start()
        record = self.latest_record()
        other_storage = ProjectStorage(Path(self.temp_directory.name) / "other-projects")
        other_project = other_storage.create_project("Other project")
        # Batch state is project-scoped on disk: another project cannot load it.
        with self.assertRaises(RenderBatchError):
            BatchStore(other_storage).load(other_project["project_id"], record["batch_id"])
        # Even a smuggled record fails ownership validation.
        with self.assertRaises(RenderBatchError) as context:
            _validate_batch_record(record, project_id=other_project["project_id"])
        self.assertEqual(context.exception.code, "BATCH_OWNERSHIP_MISMATCH")

    def test_arbitrary_output_paths_are_not_client_controllable(self):
        for api in (start_batch, resume_batch, retry_failed_batch):
            with self.assertRaises(TypeError):
                api(self.storage, self.project_id, output_path="../../escape.mp4")
            with self.assertRaises(TypeError):
                api(self.storage, self.project_id, output_root="C:/windows")
        # Routes never forward client output paths into batch operations.
        self.assertNotIn("output_path", ROUTES_PY)

    def test_arbitrary_preparation_paths_are_not_client_controllable(self):
        for api in (start_batch, resume_batch, retry_failed_batch):
            with self.assertRaises(TypeError):
                api(self.storage, self.project_id, preparation_path="../evil")
        self.assertIn("Render preparation does not accept filesystem paths or request fields.", ROUTES_PY)

    def test_scene_ids_resolve_against_owned_project_only(self):
        other_storage = ProjectStorage(Path(self.temp_directory.name) / "other-projects")
        other_project = other_storage.create_project("Other project")
        foreign_scene = other_project["scenes"][0]["scene_id"] if other_project.get("scenes") else "00000000-0000-4000-8000-000000000000"
        with self.assertRaises(RenderBatchError) as context:
            preview_batch(
                self.storage,
                self.project_id,
                scene_ids=[foreign_scene],
                client=self.client,
                preflight_builder=self.preflight_builder,
                raw_output_root=self.raw_root,
            )
        self.assertEqual(context.exception.code, "SCENE_NOT_OWNED")

    def test_backend_controls_dispatch_order_regardless_of_selection_order(self):
        reversed_selection = list(reversed(self.scene_ids))
        self.start(scene_ids=reversed_selection)
        record = self.latest_record()
        self.assertEqual([item["scene_id"] for item in record["items"]], self.scene_ids)
        self.assertEqual([item["sequence"] for item in record["items"]], [1, 2, 3])

    def test_frontend_cannot_force_two_simultaneous_batches(self):
        self.start()
        with self.assertRaises(RenderBatchConflict) as context:
            self.start()
        self.assertEqual(context.exception.code, "BATCH_ACTIVE")
        records = BatchStore(self.storage).list_project(self.project_id)
        self.assertEqual(len(records), 1)

    def test_routes_reject_arbitrary_batch_control_bodies(self):
        self.assertIn("Batch controls do not accept batch IDs, scene objects, or request fields.", ROUTES_PY)
        self.assertIn("Batch selection accepts only scene_ids.", ROUTES_PY)
        self.assertIn("scene_ids must be a non-empty list of scene identifiers.", ROUTES_PY)

    def test_routes_expose_no_generic_queue_execution_api(self):
        self.assertNotIn("/render/batch/execute", ROUTES_PY)
        self.assertNotIn("run_batch_scene", ROUTES_PY)
        for route in (
            "/render/batch/preview",
            "/render/batch/start",
            "/render/batch/pause",
            "/render/batch/resume",
            "/render/batch/end",
            "/render/batch/retry-failed",
        ):
            self.assertIn(route, ROUTES_PY)

    def test_batch_items_cannot_carry_client_supplied_identity(self):
        self.start()
        record = self.latest_record()
        for item in record["items"]:
            self.assertEqual(
                set(item),
                {
                    "scene_id",
                    "project_id",
                    "sequence",
                    "action",
                    "disposition",
                    "reason_code",
                    "job_id",
                    "failure",
                    "updated_at",
                },
            )


class V20BatchLifecycleTests(Phase8DBase):
    def test_selected_scene_uses_selected_readiness_not_unrelated_global_gate(self):
        self.global_workflow_ready = False
        self.scene_overrides[self.scene_ids[0]] = {"workflow_ready": True}
        self.scene_overrides[self.scene_ids[1]] = {"workflow_ready": False}
        self.start(scene_ids=[self.scene_ids[0]])
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_RUNNING)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_RENDERING)

    def test_tick_transition_drain_is_bounded_without_recursion(self):
        self.start(scene_ids=[self.scene_ids[0]])
        runner = self.make_runner()
        calls = []
        original_record = runner._store().load_active(self.project_id)

        def synthetic_tick_once():
            calls.append(len(calls))
            if len(calls) < BATCH_TICK_TRANSITION_BUDGET + 5:
                runner._request_next_tick(original_record)
            return original_record

        runner._tick_once = synthetic_tick_once
        result = runner.tick()
        self.assertIs(result, original_record)
        self.assertEqual(len(calls), BATCH_TICK_TRANSITION_BUDGET)
        self.assertEqual(runner._last_tick_steps, BATCH_TICK_TRANSITION_BUDGET)

    def test_malformed_batch_body_is_not_treated_as_all_ready(self):
        with self.assertRaises(ValueError):
            parse_batch_selection_payload(None, body_present=True)
        self.assertIsNone(parse_batch_selection_payload(None, body_present=False))
        self.assertIsNone(parse_batch_selection_payload({}, body_present=True))


class ManualActionGateTests(Phase8DBase):
    def test_manual_actions_blocked_while_batch_running(self):
        self.start()
        blocker = manual_action_blocker(self.storage, self.project_id)
        self.assertEqual(blocker, "A batch render is active. Pause or end the batch before rendering scenes manually.")

    def test_manual_actions_allowed_when_no_batch(self):
        self.assertIsNone(manual_action_blocker(self.storage, self.project_id))

    def test_manual_actions_allowed_again_after_pause(self):
        self.start()
        pause_batch_after_current(self.storage, self.project_id)
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertIsNone(manual_action_blocker(self.storage, self.project_id))

    def test_manual_actions_allowed_after_batch_ends(self):
        from backend.render_batch import end_batch

        self.start()
        pause_batch_after_current(self.storage, self.project_id)
        runner = self.make_runner()
        runner.tick()
        end_batch(self.storage, self.project_id)
        self.assertIsNone(manual_action_blocker(self.storage, self.project_id))

    def test_manual_submit_route_gate_reuses_the_same_truth(self):
        self.assertIn("manual_action_blocker", ROUTES_PY)
        # Prepare, submit, retry, and finalize are gated; cancel remains free.
        self.assertEqual(ROUTES_PY.count("blocker = await asyncio.to_thread(manual_action_blocker"), 4)
