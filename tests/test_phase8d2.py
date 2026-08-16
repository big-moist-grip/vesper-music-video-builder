"""Phase 8D failure isolation and systemic pause contracts."""

from unittest.mock import patch

from backend import render_batch as render_batch_module
from backend.render_batch import (
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_PAUSED,
    BATCH_RUNNING,
    ITEM_COMPLETE,
    ITEM_FAILED,
    ITEM_FINALIZATION_FAILED,
    ITEM_PENDING,
    ITEM_RENDERING,
    RenderBatchConflict,
)
from backend.render_finalize import finalize_render_job
from backend.render_jobs import ComfyUIClientError, JobStore

from phase8d_base import Phase8DBase


class FailureIsolationTests(Phase8DBase):
    def test_preparation_failure_is_isolated_and_batch_continues(self):
        self.scene_overrides[self.scene_ids[0]] = {"preparation_status": "stale"}
        self.preparer_fail = "PREP_MEDIA_FAILED"
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner)
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_FAILED)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[2])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.client.calls["submit"], 2)

    def test_submission_failure_is_isolated_and_batch_continues(self):
        def submit_fail_once(workflow):
            if self.client.calls["submit"] == 0:
                self.client.calls["submit"] += 1
                raise ComfyUIClientError(
                    "COMFYUI_VALIDATION_REJECTED",
                    "ComfyUI rejected the workflow during validation.",
                    details={"node_errors": {}},
                )
            return real_submit(workflow)

        real_submit = self.client.submit_prompt
        self.client.submit_prompt = submit_fail_once
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner)
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_FAILED)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[2])["disposition"], ITEM_COMPLETE)

    def test_execution_failure_is_isolated_and_later_scenes_run(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner, fail_scenes={self.scene_ids[1]})
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        failed_item = self.item_by_scene(record, self.scene_ids[1])
        self.assertEqual(failed_item["disposition"], ITEM_FAILED)
        self.assertIsNotNone(failed_item["failure"])
        self.assertEqual(self.item_by_scene(record, self.scene_ids[2])["disposition"], ITEM_COMPLETE)

    def test_invalid_raw_output_is_isolated_with_real_finalization(self):
        # A ComfyUI success without a discoverable production output leaves the
        # job SUCCEEDED with no raw association; finalization must fail closed
        # without stopping the batch.
        real_submit = self.client.submit_prompt

        def submit_without_output(workflow):
            result = real_submit(workflow)
            return result

        self.client.submit_prompt = submit_without_output

        def succeed_without_discovery(prompt_id):
            self.client.pending = [p for p in self.client.pending if p != prompt_id]
            self.client.running = [p for p in self.client.running if p != prompt_id]
            self.client.history[prompt_id] = {
                "prompt_id": prompt_id,
                "status": {"status_str": "success", "messages": []},
                "outputs": {},
            }

        self.start()
        runner = self.make_runner()
        runner.finalizer = finalize_render_job
        runner.media_adapter = self.media_adapter
        record = runner.tick()
        item = record["items"][0]
        job = JobStore(self.storage).load(self.project_id, item["job_id"])
        succeed_without_discovery(job["comfy_prompt_id"])
        record = runner.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_FINALIZATION_FAILED)
        # The next scene proceeds.
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)

    def test_finalization_failure_is_isolated_and_raw_preserved(self):
        self.finalizer_fail = "FINAL_MEDIA_INVALID"
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        raw_relative = self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_FINALIZATION_FAILED)
        self.assertTrue((self.raw_root / raw_relative).is_file())
        self.assertEqual(JobStore(self.storage).load(self.project_id, job["job_id"])["state"], "SUCCEEDED")
        # The batch continues with the next scene.
        self.assertEqual(self.item_by_scene(record, self.scene_ids[1])["disposition"], ITEM_RENDERING)

    def test_failure_counts_are_exact(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner, fail_scenes={self.scene_ids[1]})
        from backend.render_batch import batch_counts

        counts = batch_counts(record)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["complete"], 2)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["processed"], 3)
        self.assertEqual(counts["remaining"], 0)

    def test_one_new_h3_attempt_per_item_per_run(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner, fail_scenes={self.scene_ids[0], self.scene_ids[1], self.scene_ids[2]})
        self.assertEqual(self.client.calls["submit"], 3)
        # Extra orchestration ticks never resubmit failed scenes.
        for _ in range(5):
            runner.tick()
        self.assertEqual(self.client.calls["submit"], 3)
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)

    def test_no_automatic_retry_loop_after_failure(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner, fail_scenes={self.scene_ids[0]})
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self.assertEqual(self.client.calls["submit"], 3)
        failed_item = self.item_by_scene(record, self.scene_ids[0])
        self.assertEqual(failed_item["disposition"], ITEM_FAILED)


class SystemicPauseTests(Phase8DBase):
    def test_comfyui_unavailable_pauses_batch_without_failing_scene(self):
        self.client.submit_error = ComfyUIClientError(
            "COMFYUI_UNAVAILABLE", "The local ComfyUI API could not be reached.", transient=True
        )
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(record["attention"]["code"], "COMFYUI_UNAVAILABLE")
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_PENDING)
        self.assertEqual(self.client.calls["submit"], 0)

    def test_missing_global_runtime_requirement_pauses_batch(self):
        self.global_requirements_ready = False
        self.start()
        record = self.latest_record()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(record["attention"]["code"], "RUNTIME_REQUIREMENTS_CHANGED")
        self.assertTrue(all(item["disposition"] == ITEM_PENDING for item in record["items"]))

    def test_invalid_workflow_contract_pauses_batch(self):
        self.global_workflow_ready = False
        self.start()
        record = self.latest_record()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(record["attention"]["code"], "WORKFLOW_CONTRACT_INVALID")

    def test_ffmpeg_unavailable_pauses_before_finalization(self):
        with patch.object(render_batch_module, "find_ffmpeg", return_value=None):
            self.start()
            runner = self.make_runner()
            record = runner.tick()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(record["attention"]["code"], "MEDIA_TOOL_MISSING")
        self.assertEqual(self.client.calls["submit"], 0)

    def test_remaining_scenes_are_not_mass_failed(self):
        self.client.submit_error = ComfyUIClientError(
            "COMFYUI_UNAVAILABLE", "The local ComfyUI API could not be reached.", transient=True
        )
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        dispositions = [item["disposition"] for item in record["items"]]
        self.assertEqual(dispositions, [ITEM_PENDING, ITEM_PENDING, ITEM_PENDING])
        self.assertNotIn(ITEM_FAILED, dispositions)

    def test_resume_revalidates_runtime_and_blocks_while_broken(self):
        self.global_requirements_ready = False
        self.start()
        with self.assertRaises(RenderBatchConflict) as context:
            self.resume()
        self.assertEqual(context.exception.code, "RUNTIME_REQUIREMENTS_CHANGED")
        record = self.active_record()
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.global_requirements_ready = True
        result = self.resume()
        self.assertEqual(result["batch"]["state"], BATCH_RUNNING)

    def test_transient_comfyui_loss_during_wait_does_not_fail_scene(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.client.unavailable = True
        for _ in range(5):
            record = runner.tick()
        self.assertEqual(record["state"], BATCH_RUNNING)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_RENDERING)
        self.client.unavailable = False
        self.complete_job_raw(job)
        record = runner.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)

    def test_sustained_comfyui_loss_pauses_for_attention(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        self.client.unavailable = True
        for _ in range(render_batch_module.RECONCILIATION_FAILURE_PAUSE_THRESHOLD + 2):
            record = runner.tick()
            if record["state"] == BATCH_PAUSED:
                break
        self.assertEqual(record["state"], BATCH_PAUSED)
        self.assertEqual(record["attention"]["code"], "COMFYUI_UNAVAILABLE")
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_RENDERING)
