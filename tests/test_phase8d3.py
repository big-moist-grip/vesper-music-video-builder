"""Phase 8D pause/cancel and project-edit currentness contracts."""

from backend.render_batch import (
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_PAUSED,
    BATCH_PAUSE_REQUESTED,
    BATCH_RUNNING,
    ITEM_CANCELLED,
    ITEM_COMPLETE,
    ITEM_RENDERING,
    ITEM_SKIPPED,
    ITEM_STALE_AFTER_RENDER,
    batch_status,
    pause_batch_after_current,
)
from backend.render_jobs import JobStore, cancel_render_job

from phase8d_base import FINGERPRINT, Phase8DBase


class ProjectEditCurrentnessTests(Phase8DBase):
    def test_queued_scene_stale_before_dispatch_is_skipped_not_rendered(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.scene_overrides[self.scene_ids[1]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "PROMPT_NOT_CURRENT", "message": "edited", "layer": "prompt"}],
        }
        self.complete_job_raw(job)
        record = runner.tick()
        skipped = self.item_by_scene(record, self.scene_ids[1])
        self.assertEqual(skipped["disposition"], ITEM_SKIPPED)
        self.assertEqual(skipped["reason_code"], "PROMPT_NOT_CURRENT")
        # The later scene still proceeds.
        self.assertEqual(self.item_by_scene(record, self.scene_ids[2])["disposition"], ITEM_RENDERING)
        self.assertEqual(self.client.calls["submit"], 2)

    def test_scene_blocked_before_dispatch_is_excluded_and_not_rendered(self):
        self.scene_overrides[self.scene_ids[0]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "VISUALS_INVALID", "message": "edited", "layer": "visuals"}],
        }
        self.start()
        record = self.latest_record()
        self.assertIsNone(self.item_by_scene(record, self.scene_ids[0]))
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)

    def test_batch_membership_does_not_grow_when_new_scene_becomes_ready(self):
        self.scene_overrides[self.scene_ids[2]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "CONTENT_NOT_READY", "message": "blocked", "layer": "content"}],
        }
        self.start()
        record = self.latest_record()
        self.assertEqual(len(record["items"]), 2)
        runner = self.make_runner()
        # Scene 3 becomes ready mid-batch; membership must stay deterministic.
        self.scene_overrides.pop(self.scene_ids[2])
        record = self.run_batch_to_completion(runner)
        self.assertEqual(record["state"], BATCH_COMPLETED)
        self.assertEqual(len(record["items"]), 2)
        self.assertIsNone(self.item_by_scene(record, self.scene_ids[2]))
        self.assertEqual(self.client.calls["submit"], 2)

    def test_scene_edited_during_render_keeps_historical_job_without_promotion(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        raw_relative = self.complete_job_raw(job)
        # The scene changes upstream while the raw render exists.
        self.scene_overrides[self.scene_ids[0]] = {
            "preparation_status": "stale",
            "preparation_fingerprint": "e" * 64,
        }
        record = runner.tick()
        item = self.item_by_scene(record, self.scene_ids[0])
        self.assertEqual(item["disposition"], ITEM_STALE_AFTER_RENDER)
        # Historical raw output retained, not finalized, not promoted.
        stored_job = JobStore(self.storage).load(self.project_id, job["job_id"])
        self.assertEqual(stored_job["state"], "SUCCEEDED")
        self.assertEqual(stored_job["output"]["relative_path"], raw_relative)
        self.assertTrue((self.raw_root / raw_relative).is_file())
        self.assertNotIn(job["job_id"], self.finalizer_calls)

    def test_edited_scene_is_not_automatically_rerendered_in_same_batch(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        self.scene_overrides[self.scene_ids[0]] = {
            "preparation_status": "stale",
            "preparation_fingerprint": "e" * 64,
        }
        record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 2)  # scene 2 only
        # Further ticks never resubmit scene 1.
        record = self.run_batch_to_completion(runner)
        self.assertEqual(self.client.calls["submit"], 3)
        scene_one_jobs = self.scene_jobs(self.scene_ids[0])
        self.assertEqual(len(scene_one_jobs), 1)


class PauseResumeTests(Phase8DBase):
    def test_pause_after_current_lets_active_scene_finish_then_pauses(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_RUNNING)
        paused = pause_batch_after_current(self.storage, self.project_id)
        self.assertEqual(paused["batch"]["state"], BATCH_PAUSE_REQUESTED)
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        # No next scene was started.
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], "PENDING")

    def test_pause_between_items_is_immediate(self):
        self.start()
        pause_batch_after_current(self.storage, self.project_id)
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(self.client.calls["submit"], 0)

    def test_resume_continues_with_next_unresolved_item(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        pause_batch_after_current(self.storage, self.project_id)
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        resumed = self.resume()
        self.assertEqual(resumed["batch"]["state"], BATCH_RUNNING)
        runner2 = self.make_runner()
        record = runner2.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)
        self.assertEqual(self.client.calls["submit"], 2)

    def test_browser_reconnect_observes_paused_state_without_unpausing(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        pause_batch_after_current(self.storage, self.project_id)
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        for _ in range(3):
            observed = batch_status(
                self.storage,
                self.project_id,
                client=self.client,
                preflight_builder=self.preflight_builder,
                raw_output_root=self.raw_root,
            )
            self.assertEqual(observed["batch"]["state"], BATCH_PAUSED)
        self.assertEqual(self.active_record()["state"], BATCH_PAUSED)

    def test_cancel_current_render_pauses_batch(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        cancel_render_job(self.storage, self.project_id, job["job_id"], client=self.client)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        item = self.item_by_scene(record, self.scene_ids[0])
        self.assertEqual(item["disposition"], ITEM_CANCELLED)
        self.assertEqual(record["attention"]["code"], "CANCELLED_CURRENT")

    def test_no_surprise_next_render_after_user_cancel(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        cancel_render_job(self.storage, self.project_id, job["job_id"], client=self.client)
        record = runner.tick()
        for _ in range(5):
            record = runner.tick()
            if record is None or record["state"] != BATCH_PAUSE_REQUESTED:
                break
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.active_record()["state"], BATCH_PAUSED)
        # Resume continues with scene 2, not a resubmission of scene 1.
        self.resume()
        runner2 = self.make_runner()
        record = runner2.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)
        self.assertEqual(self.client.calls["submit"], 2)

    def test_end_paused_batch_retains_history_without_further_work(self):
        from backend.render_batch import end_batch, BATCH_ENDED

        self.start()
        runner = self.make_runner()
        record = runner.tick()
        pause_batch_after_current(self.storage, self.project_id)
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        ended = end_batch(self.storage, self.project_id)
        self.assertEqual(ended["batch"]["state"], BATCH_ENDED)
        self.assertIsNone(self.active_record())
        stored = self.latest_record()
        self.assertEqual(stored["state"], BATCH_ENDED)
        # No additional production work happened.
        self.assertEqual(self.client.calls["submit"], 1)

    def test_running_batch_cannot_be_ended_directly(self):
        from backend.render_batch import RenderBatchConflict, end_batch

        self.start()
        with self.assertRaises(RenderBatchConflict):
            end_batch(self.storage, self.project_id)
