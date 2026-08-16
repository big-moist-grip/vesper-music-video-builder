"""Phase 8D sequential execution contracts: one H3 scene at a time."""

from backend.render_batch import (
    BATCH_COMPLETED,
    ITEM_COMPLETE,
    ITEM_RENDERING,
    start_batch,
)
from backend.render_jobs import JobStore

from phase8d_base import Phase8DBase


class SequentialExecutionTests(Phase8DBase):
    def test_only_one_scene_submitted_at_a_time(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)
        rendering = [item for item in record["items"] if item["disposition"] == ITEM_RENDERING]
        self.assertEqual(len(rendering), 1)
        # While scene 1 executes, repeated orchestration ticks submit nothing new.
        for _ in range(5):
            record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)

    def test_scene_two_not_submitted_until_scene_one_terminal(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        scene_one = record["items"][0]
        job = JobStore(self.storage).load(self.project_id, scene_one["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.client.calls["submit"], 2)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)

    def test_no_queue_flood_at_batch_start(self):
        self.start()
        runner = self.make_runner()
        for _ in range(10):
            record = runner.tick()
            if record["state"] != "RUNNING":
                break
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertLessEqual(len(self.client.pending) + len(self.client.running), 1)

    def test_preparation_invoked_automatically_when_required(self):
        self.scene_overrides[self.scene_ids[0]] = {"preparation_status": "stale"}
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.preparer_calls, [self.scene_ids[0]])
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_RENDERING)

    def test_current_preparation_is_reused_without_repreparing(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.preparer_calls, [])
        self.assertEqual(self.client.calls["submit"], 1)

    def test_raw_current_scene_skips_h3_and_finalizes_only(self):
        existing_job = self.make_succeeded_job_with_final(self.scene_ids[0])
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.finalizer_calls, [existing_job["job_id"]])
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        # The next scene receives the only H3 submission so far.
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)

    def test_final_current_scene_skips_all_production_work(self):
        job = self.make_succeeded_job_with_final(self.scene_ids[0])
        self.finalize_job_real(job)
        self.start()
        record = self.latest_record()
        self.assertIsNone(self.item_by_scene(record, self.scene_ids[0]))
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertNotIn(self.scene_ids[0], self.finalizer_calls)

    def test_successful_raw_auto_finalizes_before_next_scene(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        scene_one = record["items"][0]
        job = JobStore(self.storage).load(self.project_id, scene_one["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertIn(job["job_id"], self.finalizer_calls)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)

    def test_full_batch_completes_all_scenes_in_order(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner)
        self.assertEqual(record["state"], BATCH_COMPLETED)
        self.assertEqual(self.client.calls["submit"], 3)
        for scene_id in self.scene_ids:
            self.assertEqual(self.item_by_scene(record, scene_id)["disposition"], ITEM_COMPLETE)
        self.assertEqual(len(self.finalizer_calls), 3)

    def test_submission_order_follows_canonical_scene_order(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner)
        submitted_scenes = []
        for item in record["items"]:
            job = JobStore(self.storage).load(self.project_id, item["job_id"])
            submitted_scenes.append(job["scene_id"])
        self.assertEqual(submitted_scenes, self.scene_ids)

    def test_browser_not_required_for_advancement(self):
        # The backend runner thread alone advances the batch; no frontend call
        # participates.  A self-completing fake runtime proves the loop.
        start_batch(
            self.storage,
            self.project_id,
            start_runner=True,
            poll_interval_seconds=0.02,
            **self.batch_seams(),
        )
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self.active_record() or self.latest_record()
            rendering = [item for item in record["items"] if item["disposition"] == ITEM_RENDERING]
            for item in rendering:
                job = JobStore(self.storage).load(self.project_id, item["job_id"])
                if job.get("comfy_prompt_id"):
                    self.complete_job_raw(job)
            if record["state"] == BATCH_COMPLETED:
                break
            time.sleep(0.02)
        from backend import render_batch as render_batch_module

        runner = render_batch_module._RUNNERS.get(self.project_id)
        if runner is not None and runner._thread is not None:
            runner._thread.join(timeout=5.0)
        record = self.latest_record()
        self.assertEqual(record["state"], BATCH_COMPLETED)
        self.assertEqual(self.client.calls["submit"], 3)
