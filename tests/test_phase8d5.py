"""Phase 8D Retry Failed and deterministic batch progress contracts."""

import json

from backend.render_batch import (
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_RUNNING,
    ITEM_COMPLETE,
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_RENDERING,
    ITEM_SKIPPED,
    RenderBatchError,
    batch_counts,
    batch_status,
    public_batch_record,
    retry_failed_batch,
)
from backend.render_jobs import JobStore

from phase8d_base import Phase8DBase


class RetryFailedTests(Phase8DBase):
    def _completed_batch_with_failures(self, fail_scenes):
        self.start()
        runner = self.make_runner()
        return self.run_batch_to_completion(runner, fail_scenes=fail_scenes)

    def test_failed_items_are_collected_for_retry(self):
        record = self._completed_batch_with_failures({self.scene_ids[1]})
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        retryable = [item for item in record["items"] if item["disposition"] in {"FAILED", "FINALIZATION_FAILED", "STALE_AFTER_RENDER", "CANCELLED"}]
        self.assertEqual([item["scene_id"] for item in retryable], [self.scene_ids[1]])

    def test_retry_creates_a_new_batch_and_preserves_old_history(self):
        record = self._completed_batch_with_failures({self.scene_ids[1]})
        old_batch_id = record["batch_id"]
        old_path = self.project_root / "renders" / "batches" / f"{old_batch_id}.json"
        old_bytes = old_path.read_bytes()
        result = retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
        new_record = self.active_record()
        self.assertNotEqual(new_record["batch_id"], old_batch_id)
        self.assertEqual(new_record["origin_batch_id"], old_batch_id)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(result["batch"]["counts"]["total"], 1)

    def test_retry_revalidates_scenes_and_skips_now_blocked_scene(self):
        self._completed_batch_with_failures({self.scene_ids[1]})
        # The failed scene becomes blocked before retry.
        self.scene_overrides[self.scene_ids[1]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "PROMPT_NOT_CURRENT", "message": "stale", "layer": "prompt"}],
        }
        with self.assertRaises(RenderBatchError) as context:
            retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
        self.assertEqual(context.exception.code, "BATCH_NO_ELIGIBLE_SCENES")
        self.assertIsNone(self.active_record())

    def test_retry_rerenders_failed_scene_with_new_job_and_prompt_id(self):
        record = self._completed_batch_with_failures({self.scene_ids[1]})
        failed_item = self.item_by_scene(record, self.scene_ids[1])
        old_job = JobStore(self.storage).load(self.project_id, failed_item["job_id"])
        old_prompt_id = old_job["comfy_prompt_id"]
        retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
        runner = self.make_runner()
        new_record = runner.tick()
        retried = self.item_by_scene(new_record, self.scene_ids[1])
        self.assertEqual(retried["disposition"], ITEM_RENDERING)
        new_job = JobStore(self.storage).load(self.project_id, retried["job_id"])
        self.assertNotEqual(new_job["job_id"], old_job["job_id"])
        self.client.begin_running(new_job["comfy_prompt_id"])
        self.assertNotEqual(new_job["comfy_prompt_id"], old_prompt_id)
        self.assertEqual(self.client.calls["submit"], 4)  # 3 original + 1 retry

    def test_finalization_failure_retries_finalization_without_new_h3(self):
        self.finalizer_fail = "FINAL_MEDIA_INVALID"
        self.start(scene_ids=[self.scene_ids[0]])
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_FINALIZATION_FAILED)
        submits_before_retry = self.client.calls["submit"]
        self.finalizer_fail = None
        retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
        new_record = self.active_record()
        retried_item = new_record["items"][0]
        # The raw output is still current, so the retry plans finalization only.
        self.assertEqual(retried_item["action"], "FINALIZE_RAW")
        runner2 = self.make_runner()
        finished = runner2.tick()
        self.assertEqual(self.item_by_scene(finished, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.client.calls["submit"], submits_before_retry)

    def test_retry_unavailable_without_retryable_items(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner)
        self.assertEqual(record["state"], BATCH_COMPLETED)
        with self.assertRaises(RenderBatchError) as context:
            retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())
        self.assertEqual(context.exception.code, "BATCH_RETRY_UNAVAILABLE")

    def test_retry_blocked_while_batch_active(self):
        from backend.render_batch import RenderBatchConflict

        self.start()
        with self.assertRaises(RenderBatchConflict):
            retry_failed_batch(self.storage, self.project_id, start_runner=False, **self.batch_seams())


class BatchProgressTests(Phase8DBase):
    def test_counts_are_exact_through_a_mixed_batch(self):
        self.scene_overrides[self.scene_ids[0]] = {"preparation_status": "stale"}
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        counts = batch_counts(record)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["processed"], 0)
        self.assertEqual(counts["remaining"], 3)
        # Complete scene 1, fail scene 2, then let scene 3 finish.
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        scene_two_item = self.item_by_scene(record, self.scene_ids[1])
        self.assertEqual(scene_two_item["disposition"], ITEM_RENDERING)
        job_two = JobStore(self.storage).load(self.project_id, scene_two_item["job_id"])
        self.client.fail(job_two["comfy_prompt_id"])
        record = runner.tick()
        scene_three_item = self.item_by_scene(record, self.scene_ids[2])
        self.assertEqual(scene_three_item["disposition"], ITEM_RENDERING)
        job_three = JobStore(self.storage).load(self.project_id, scene_three_item["job_id"])
        self.complete_job_raw(job_three)
        record = runner.tick()
        counts = batch_counts(record)
        self.assertEqual(counts["complete"], 2)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["processed"], 3)
        self.assertEqual(counts["remaining"], 0)
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)

    def test_processed_semantics_documented_in_public_payload(self):
        record = self._run_full_batch()
        payload = public_batch_record(record)
        self.assertEqual(payload["counts"]["total"], 3)
        self.assertEqual(payload["counts"]["processed"], 3)
        # No time/computation estimation is ever exposed.
        for key in ("eta", "estimated_completion", "time_remaining", "percent_complete", "render_percent"):
            self.assertNotIn(key, payload)
            self.assertNotIn(key, payload["counts"])

    def test_skipped_count_is_exact(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        # Scene 2 becomes blocked before dispatch; it is skipped, not rendered.
        self.scene_overrides[self.scene_ids[1]] = {
            "content_ready": False,
            "preparation_ready": False,
            "blockers": [{"code": "CONTENT_NOT_READY", "message": "x", "layer": "content"}],
        }
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        record = runner.tick()
        counts = batch_counts(record)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_SKIPPED)

    def test_current_scene_exposed_without_internal_identifiers(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        payload = public_batch_record(record)
        self.assertIsNotNone(payload["current"])
        self.assertEqual(payload["current"]["sequence"], 1)
        self.assertNotIn("job_id", payload["current"])
        self.assertNotIn("prompt_id", payload["current"])
        self.assertNotIn("batch_id", payload)

    def test_batch_progress_is_scene_count_based_not_time_based(self):
        record = self._run_full_batch()
        payload = public_batch_record(record)
        self.assertEqual(payload["counts"]["processed"], payload["counts"]["total"])
        # The payload exposes counts only; nothing derives a time estimate.
        self.assertEqual(
            set(payload["counts"]),
            {"total", "processed", "complete", "failed", "cancelled", "skipped", "active", "remaining"},
        )

    def test_status_route_reports_running_batch(self):
        self.start()
        runner = self.make_runner()
        runner.tick()
        observed = batch_status(
            self.storage,
            self.project_id,
            client=self.client,
            preflight_builder=self.preflight_builder,
            raw_output_root=self.raw_root,
        )
        self.assertEqual(observed["batch"]["state"], BATCH_RUNNING)
        self.assertEqual(observed["batch"]["counts"]["total"], 3)
        # A status read after a simulated backend restart restores tracking;
        # stop the restored runner before teardown.
        from backend import render_batch as render_batch_module

        restored = render_batch_module._RUNNERS.get(self.project_id)
        if restored is not None:
            restored.stop()
            if restored._thread is not None:
                restored._thread.join(timeout=5.0)

    def _run_full_batch(self):
        self.start()
        runner = self.make_runner()
        return self.run_batch_to_completion(runner)
