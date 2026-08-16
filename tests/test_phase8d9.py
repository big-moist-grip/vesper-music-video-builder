"""Phase 8D Renders batch UI structural contracts.

These tests assert the production-facing Renders workspace contract directly
against the shipped frontend source: batch controls, temporary selection mode,
start confirmation, count-based batch progress, and the absence of internal
identifiers or fabricated estimates.
"""

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION_JS = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
BUILDER_CSS = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")


class BatchIdleControlsTests(unittest.TestCase):
    def test_render_all_ready_is_the_primary_idle_action(self):
        self.assertIn('data-mvb-batch-all', EXTENSION_JS)
        self.assertIn(">Render All Ready</button>", EXTENSION_JS)
        self.assertIn('data-mvb-batch-all type="button"', EXTENSION_JS)

    def test_select_scenes_control_is_present(self):
        self.assertIn('data-mvb-batch-select', EXTENSION_JS)
        self.assertIn(">Select Scenes</button>", EXTENSION_JS)

    def test_header_keeps_refresh_and_rescan_secondary_controls(self):
        self.assertIn(">Refresh Preflight</button>", EXTENSION_JS)
        self.assertIn(">Rescan Runtime</button>", EXTENSION_JS)
        self.assertIn('>Renders</h2>', EXTENSION_JS)

    def test_batch_controls_disabled_while_batch_executes(self):
        self.assertIn("batchExecuting", EXTENSION_JS)
        self.assertIn("batchIsExecuting", EXTENSION_JS)


class SelectionModeTests(unittest.TestCase):
    def test_checkboxes_exist_only_while_selection_mode_is_active(self):
        self.assertIn("mvb-render-select", EXTENSION_JS)
        # The static shell contains no persistent checkbox clutter.
        shell_start = EXTENSION_JS.index("<section class=\"mvb-render\"")
        shell_end = EXTENSION_JS.index("</section>", shell_start)
        shell = EXTENSION_JS[shell_start:shell_end]
        self.assertNotIn('type="checkbox"', shell)

    def test_blocked_scenes_cannot_be_selected(self):
        self.assertIn("checkbox.disabled = !selectionEligible", EXTENSION_JS)
        self.assertIn("BATCH_ELIGIBLE_ACTIONS.includes(selectionAction)", EXTENSION_JS)
        self.assertIn("Not ready for batch rendering", EXTENSION_JS)
        self.assertIn("Final scene already ready", EXTENSION_JS)

    def test_select_all_ready_and_clear_controls(self):
        self.assertIn('data-mvb-batch-select-all', EXTENSION_JS)
        self.assertIn('textContent = "Select All Ready"', EXTENSION_JS)
        self.assertIn('data-mvb-batch-clear', EXTENSION_JS)
        self.assertIn('textContent = "Clear"', EXTENSION_JS)

    def test_selected_count_and_render_selected_action(self):
        self.assertIn("selectedCount", EXTENSION_JS)
        self.assertIn("${selectedCount} selected", EXTENSION_JS)
        self.assertIn('data-mvb-batch-render-selected', EXTENSION_JS)
        self.assertIn('textContent = "Render Selected"', EXTENSION_JS)

    def test_cancel_selection_exits_cleanly(self):
        self.assertIn('data-mvb-batch-cancel-selection', EXTENSION_JS)
        self.assertIn('textContent = "Cancel Selection"', EXTENSION_JS)
        self.assertIn("function exitBatchSelectionMode(root)", EXTENSION_JS)


class StartConfirmationTests(unittest.TestCase):
    def test_confirmation_dialog_exists_and_starts_hidden(self):
        self.assertIn('data-mvb-batch-confirm hidden', EXTENSION_JS)
        self.assertIn('data-mvb-batch-confirm-title', EXTENSION_JS)
        self.assertIn(">Start Batch</button>", EXTENSION_JS)

    def test_confirmation_reports_scene_count_and_work_split(self):
        self.assertIn("Render ${counts.eligible} scene", EXTENSION_JS)
        self.assertIn("require H3 rendering", EXTENSION_JS)
        self.assertIn("require finalization only", EXTENSION_JS)

    def test_confirmation_explains_sequential_automatic_pipeline(self):
        self.assertIn("Scenes will be processed one at a time in scene order.", EXTENSION_JS)
        self.assertIn("Render inputs will be prepared automatically where needed", EXTENSION_JS)
        self.assertIn("successful raw renders will be finalized automatically", EXTENSION_JS)

    def test_no_submission_before_confirmation(self):
        # The start endpoint is reachable only through the confirmed flow.
        start_calls = EXTENSION_JS.count("fetchJson(renderBatchStartPath(projectId)")
        self.assertEqual(start_calls, 1)
        self.assertIn("async function startConfirmedBatch(root)", EXTENSION_JS)
        self.assertIn("builderState.renderBatchConfirm", EXTENSION_JS)

    def test_no_fake_render_time_or_cost_estimates(self):
        lowered = EXTENSION_JS.lower()
        for forbidden in (
            "estimated",
            "time remaining",
            "hours remaining",
            "minutes remaining",
            "completion time",
            "eta_",
            "etaseconds",
            "timeremaining",
        ):
            self.assertNotIn(forbidden, lowered)


class BatchPanelTests(unittest.TestCase):
    def test_running_panel_shows_count_based_progress(self):
        self.assertIn('data-mvb-batch-panel', EXTENSION_JS)
        self.assertIn("Batch render", EXTENSION_JS)
        self.assertIn("scenes processed", EXTENSION_JS)
        self.assertIn("Scenes processed", EXTENSION_JS)
        self.assertIn("Pause After Current", EXTENSION_JS)
        self.assertIn('data-mvb-batch-pause', EXTENSION_JS)

    def test_current_scene_label_reuses_per_scene_telemetry(self):
        self.assertIn("renderTelemetryPresentation(job)", EXTENSION_JS)
        self.assertIn("renderBatchStageLabel", EXTENSION_JS)
        self.assertIn("Preparing render inputs", EXTENSION_JS)
        self.assertIn("Finalizing scene", EXTENSION_JS)

    def test_paused_panel_offers_resume_and_end(self):
        self.assertIn('textContent = "Batch paused"', EXTENSION_JS)
        self.assertIn('textContent = "Resume Batch"', EXTENSION_JS)
        self.assertIn('data-mvb-batch-resume', EXTENSION_JS)
        self.assertIn('textContent = "End Batch"', EXTENSION_JS)
        self.assertIn('data-mvb-batch-end', EXTENSION_JS)

    def test_recovery_pause_is_presented_truthfully(self):
        self.assertIn('textContent = "Batch paused after restart"', EXTENSION_JS)

    def test_completed_panel_offers_retry_failed(self):
        self.assertIn('textContent = "Batch complete"', EXTENSION_JS)
        self.assertIn('textContent = "Retry Failed"', EXTENSION_JS)
        self.assertIn('data-mvb-batch-retry-failed', EXTENSION_JS)

    def test_progress_bar_is_scene_count_labeled_accessibility(self):
        self.assertIn('aria-valuetext", `${processed} of ${counts.total} scenes processed`', EXTENSION_JS)
        self.assertIn("mvb-batch-progress-track", EXTENSION_JS)
        self.assertIn("mvb-batch-progress-bar", BUILDER_CSS)

    def test_no_time_based_completion_percentage_claim(self):
        self.assertNotIn("Render % complete", EXTENSION_JS)
        self.assertNotIn("render-time", EXTENSION_JS)


class IdentifierAndPolishTests(unittest.TestCase):
    def test_no_batch_or_job_uuids_are_rendered(self):
        self.assertNotIn("batch_id", EXTENSION_JS)
        self.assertNotIn("batchId", EXTENSION_JS)
        # Batch item rows never render job identifiers.
        self.assertNotIn("item.job_id", EXTENSION_JS)

    def test_panel_updates_are_narrow_via_shell_key(self):
        self.assertIn('panel.dataset.shellKey === shellKey', EXTENSION_JS)

    def test_scene_cards_preserve_scroll_position(self):
        self.assertIn("content.scrollTop = contentScrollTop", EXTENSION_JS)

    def test_reduced_motion_respected_for_batch_progress(self):
        self.assertIn("prefers-reduced-motion", BUILDER_CSS)
        reduced = BUILDER_CSS[BUILDER_CSS.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn("mvb-batch-progress-bar", reduced)
        self.assertIn("transition: none", reduced)

    def test_manual_scene_actions_explain_batch_gating(self):
        self.assertIn("A batch render is active; manual scene renders are paused.", EXTENSION_JS)

    def test_no_debug_or_internal_batch_prose(self):
        for forbidden in (
            "accepted by local ComfyUI",
            "workflow validated",
            "numerical progress is not inferred",
            "batch_id",
            "prompt_id",
        ):
            self.assertNotIn(forbidden, EXTENSION_JS)
