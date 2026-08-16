"""Phase 8D persistence, restart recovery, and runner locking contracts."""

import time

from backend import render_batch as render_batch_module
from backend.render_batch import (
    BATCH_COMPLETED_WITH_ISSUES,
    BATCH_PAUSED_RECOVERY,
    BATCH_RUNNING,
    ITEM_COMPLETE,
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_RENDERING,
    batch_status,
    ensure_batch_recovery,
    start_batch,
)
from backend.render_jobs import JobStore

from phase8d_base import Phase8DBase


class RecoveryTests(Phase8DBase):
    def _simulate_restart(self):
        with render_batch_module._RUNNERS_LOCK:
            runner = render_batch_module._RUNNERS.pop(self.project_id, None)
        if runner is not None:
            runner.stop()
            thread = runner._thread
            if thread is not None:
                thread.join(timeout=5.0)

    def test_backend_restart_recovers_active_job_without_duplicate_submission(self):
        self.start(start_runner=True, poll_interval_seconds=0.02)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self.active_record()
            if record and any(item["disposition"] == ITEM_RENDERING for item in record["items"]):
                break
            time.sleep(0.01)
        self._simulate_restart()
        ensure_batch_recovery(
            self.storage,
            self.project_id,
            poll_interval_seconds=0.02,
            **self.batch_seams(),
        )
        record = self.active_record()
        self.assertEqual(record["state"], BATCH_RUNNING)
        self.assertIsNotNone(record.get("recovered"))
        # The recovered job is tracked to completion; no second submission.
        item = next(entry for entry in record["items"] if entry["disposition"] == ITEM_RENDERING)
        job = JobStore(self.storage).load(self.project_id, item["job_id"])
        self.complete_job_raw(job)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self.active_record() or self.latest_record()
            if record["state"] == BATCH_PAUSED_RECOVERY:
                break
            time.sleep(0.01)
        self.assertEqual(record["state"], BATCH_PAUSED_RECOVERY)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.client.calls["submit"], 1)
        runner = render_batch_module._RUNNERS.get(self.project_id)
        if runner is not None and runner._thread is not None:
            runner._thread.join(timeout=5.0)

    def test_restart_without_active_job_enters_safe_recovery_pause(self):
        # Simulate a crash before any submission reached ComfyUI.
        start_batch(
            self.storage,
            self.project_id,
            start_runner=False,
            **self.batch_seams(),
        )
        self._simulate_restart()
        recovered = ensure_batch_recovery(
            self.storage,
            self.project_id,
            **self.batch_seams(),
        )
        self.assertEqual(recovered["state"], BATCH_PAUSED_RECOVERY)
        self.assertEqual(recovered["attention"]["code"], "RESTART_RECOVERY")
        self.assertEqual(self.client.calls["submit"], 0)
        self.assertTrue(all(item["disposition"] == ITEM_PENDING for item in recovered["items"]))

    def test_resume_after_recovery_dispatches_exactly_once(self):
        start_batch(
            self.storage,
            self.project_id,
            start_runner=False,
            **self.batch_seams(),
        )
        self._simulate_restart()
        ensure_batch_recovery(self.storage, self.project_id, **self.batch_seams())
        self.resume()
        runner = self.make_runner()
        record = runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_RENDERING)
        for _ in range(5):
            runner.tick()
        self.assertEqual(self.client.calls["submit"], 1)

    def test_completed_and_failed_entries_survive_restart(self):
        self.start()
        runner = self.make_runner()
        record = self.run_batch_to_completion(runner, fail_scenes={self.scene_ids[1]})
        self.assertEqual(record["state"], BATCH_COMPLETED_WITH_ISSUES)
        self._simulate_restart()
        observed = batch_status(
            self.storage,
            self.project_id,
            client=self.client,
            preflight_builder=self.preflight_builder,
            raw_output_root=self.raw_root,
        )
        batch_payload = observed["batch"]
        dispositions = {item["scene_id"]: item["disposition"] for item in batch_payload["items"]}
        self.assertEqual(dispositions[self.scene_ids[0]], ITEM_COMPLETE)
        self.assertEqual(dispositions[self.scene_ids[1]], ITEM_FAILED)
        self.assertEqual(dispositions[self.scene_ids[2]], ITEM_COMPLETE)
        self.assertEqual(batch_payload["counts"]["failed"], 1)
        self.assertEqual(batch_payload["retryable_count"], 1)

    def test_browser_reload_while_backend_alive_keeps_running_batch(self):
        self.start(start_runner=True, poll_interval_seconds=0.02)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self.active_record()
            if record and any(item["disposition"] == ITEM_RENDERING for item in record["items"]):
                break
            time.sleep(0.01)
        # Frontend reload only observes; the backend runner is untouched.
        observed = batch_status(
            self.storage,
            self.project_id,
            client=self.client,
            preflight_builder=self.preflight_builder,
            raw_output_root=self.raw_root,
        )
        self.assertEqual(observed["batch"]["state"], BATCH_RUNNING)
        item = next(entry for entry in self.active_record()["items"] if entry["disposition"] == ITEM_RENDERING)
        job = JobStore(self.storage).load(self.project_id, item["job_id"])
        self.complete_job_raw(job)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self.active_record() or self.latest_record()
            if record["state"] != BATCH_RUNNING:
                break
            time.sleep(0.01)
        runner = render_batch_module._RUNNERS.get(self.project_id)
        if runner is not None and runner._thread is not None:
            runner.stop()
            runner._thread.join(timeout=5.0)
        self.assertGreaterEqual(self.client.calls["submit"], 2)

    def test_no_duplicate_runner_is_created(self):
        self.start(start_runner=True, poll_interval_seconds=0.5)
        first = render_batch_module._RUNNERS.get(self.project_id)
        self.assertIsNotNone(first)
        # Repeated status reads and resume attempts never create a second runner.
        for _ in range(3):
            batch_status(
                self.storage,
                self.project_id,
                client=self.client,
                preflight_builder=self.preflight_builder,
                raw_output_root=self.raw_root,
            )
        second = render_batch_module._RUNNERS.get(self.project_id)
        self.assertIs(first, second)
        self.assertTrue(first.is_alive())
        # A second batch start is rejected outright.
        from backend.render_batch import RenderBatchConflict

        with self.assertRaises(RenderBatchConflict):
            self.start()
        first.stop()
        if first._thread is not None:
            first._thread.join(timeout=5.0)

    def test_mid_finalization_crash_recovers_by_finalizing_again(self):
        self.start()
        runner = self.make_runner()
        record = runner.tick()
        job = JobStore(self.storage).load(self.project_id, record["items"][0]["job_id"])
        self.complete_job_raw(job)
        # Drive the item to FINALIZING, then simulate a restart.
        test_case = self

        class FinalizingFinalizer:
            def __init__(self):
                self.calls = 0

            def __call__(self, storage, project_id, job_id, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    # Simulate a crash mid-finalization: metadata stays FINALIZING.
                    from backend.render_finalize import (
                        FINALIZATION_STATE_FINALIZING,
                        _write_finalization_metadata,
                    )

                    root = test_case.storage.project_directory(test_case.project_id).resolve(strict=True)
                    _write_finalization_metadata(
                        root,
                        test_case.scene_ids[0],
                        job_id,
                        {
                            "finalization_schema_version": 1,
                            "project_id": test_case.project_id,
                            "scene_id": test_case.scene_ids[0],
                            "job_id": job_id,
                            "state": FINALIZATION_STATE_FINALIZING,
                            "output": None,
                            "failure": None,
                        },
                    )
                    raise SystemExit("simulated crash")
                return {"state": "FINALIZED"}

        crash_finalizer = FinalizingFinalizer()
        runner.finalizer = crash_finalizer
        try:
            runner.tick()
        except SystemExit:
            pass
        # Persisted item remains FINALIZING; restart recovery pauses safely and
        # resume re-runs finalization on the same job without a new H3 render.
        record = self.active_record()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], "FINALIZING")
        self._simulate_restart()
        recovered = ensure_batch_recovery(self.storage, self.project_id, **self.batch_seams())
        self.assertEqual(recovered["state"], BATCH_PAUSED_RECOVERY)
        self.resume()
        runner2 = self.make_runner()
        record = runner2.tick()
        self.assertEqual(self.item_by_scene(record, self.scene_ids[0])["disposition"], ITEM_COMPLETE)
        self.assertEqual(self.client.calls["submit"], 2)
