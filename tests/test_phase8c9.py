"""Phase 8C.9 Renders header and progress-component remediation contracts."""

from __future__ import annotations

from pathlib import Path
import unittest

from backend.render_telemetry import ComfyUITelemetryAdapter, normalize_comfyui_progress


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
PRESENTATION = EXTENSION[
    EXTENSION.index("function renderTelemetryPresentation") : EXTENSION.index("function updateRenderJobTelemetry")
]
UPDATE = EXTENSION[
    EXTENSION.index("function updateRenderJobTelemetry") : EXTENSION.index("function createRenderJobTelemetry")
]
RENDER = EXTENSION[
    EXTENSION.index("function renderRenderState") : EXTENSION.index("async function loadRenderJobs")
]
ACTIVE_STATES = EXTENSION[
    EXTENSION.index("function renderJobIsActive") : EXTENSION.index("function renderFinalizationLabel")
]
TELEMETRY_CSS = CSS[
    CSS.index(".mvb-render-telemetry {") : CSS.index(".mvb-render-blockers {")
]
PROMPT_ID = "phase8c9-prompt"
WORKFLOW = {
    "2": {"class_type": "UNETLoader", "inputs": {}},
    "18": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
    "21": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


def event(event_type: str, **data):
    return {"type": event_type, "data": {"prompt_id": PROMPT_ID, **data}}


class Phase8C9HeaderTests(unittest.TestCase):
    def test_01_navigation_label_is_renders(self):
        self.assertIn('data-mvb-view="render" type="button" role="tab" aria-selected="false">Renders</button>', EXTENSION)

    def test_02_page_title_renders_exists(self):
        self.assertIn('<h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>', EXTENSION)
        self.assertIn('aria-labelledby="mvb-render-heading"', EXTENSION)

    def test_03_redundant_legacy_headings_are_absent(self):
        self.assertNotIn("Render preparation", EXTENSION)
        self.assertNotIn("Scene readiness", EXTENSION)

    def test_04_aggregate_summary_is_present_and_concise(self):
        self.assertIn('`${preflight.preparation_ready_count} ready · ${notReadyCount} not ready', RENDER)
        self.assertIn('${activeJobCount} active', RENDER)

    def test_05_ordinary_aggregate_summary_is_neutral(self):
        self.assertIn(': "neutral";', RENDER)
        self.assertNotIn('notReadyCount ? "warning"', RENDER)

    def test_06_refresh_preflight_is_a_direct_action(self):
        self.assertIn('<button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-render-refresh', EXTENSION)

    def test_07_rescan_runtime_is_a_direct_action(self):
        self.assertIn('<button class="mvb-button mvb-button-secondary mvb-button-small" data-mvb-render-rescan', EXTENSION)

    def test_08_advanced_wrapper_is_absent(self):
        self.assertNotIn("mvb-render-advanced", EXTENSION)
        self.assertNotIn("mvb-render-advanced", CSS)


class Phase8C9ProgressModeTests(unittest.TestCase):
    def test_09_running_without_numeric_telemetry_is_indeterminate(self):
        self.assertIn('mode: percent === null ? "indeterminate" : "determinate"', PRESENTATION)
        self.assertIn('progressMax > 0', PRESENTATION)

    def test_10_indeterminate_mode_has_no_percentage(self):
        self.assertIn('stage.textContent = percent === null ? stageLabel', UPDATE)
        self.assertIn('track.removeAttribute("aria-valuenow")', UPDATE)

    def test_11_indeterminate_mode_has_no_fixed_completion_width(self):
        self.assertNotIn('bar.style.width = "32%"', UPDATE)
        self.assertIn('bar.style.removeProperty("width")', UPDATE)
        self.assertIn("background: transparent", TELEMETRY_CSS)

    def test_12_indeterminate_animation_class_is_applied(self):
        self.assertIn('track.classList.toggle("mvb-render-telemetry-track-indeterminate", mode === "indeterminate")', UPDATE)
        self.assertIn(".mvb-render-telemetry-track-indeterminate .mvb-render-telemetry-bar::after", CSS)

    def test_13_valid_value_max_selects_determinate_mode(self):
        self.assertIn('job?.state === "RUNNING"', PRESENTATION)
        self.assertIn('progress?.kind === "telemetry"', PRESENTATION)
        self.assertIn('progressValue !== null', PRESENTATION)

    def test_14_determinate_width_uses_exact_normalized_fraction(self):
        self.assertIn('Math.round(Math.max(0, Math.min(1, progressValue / progressMax)) * 100)', PRESENTATION)
        self.assertIn('bar.style.width = `${percent}%`', UPDATE)

    def test_15_percentage_copy_is_concise_and_stage_local(self):
        self.assertIn('`${stageLabel} · ${percent}%`', UPDATE)
        self.assertNotIn("Overall render", UPDATE)

    def test_16_nonpositive_maximum_is_not_numeric_progress(self):
        self.assertIsNone(normalize_comfyui_progress(10, 0))
        self.assertIsNone(normalize_comfyui_progress(10, -1))

    def test_17_invalid_telemetry_falls_back_to_indeterminate(self):
        self.assertIsNone(normalize_comfyui_progress("10", 100))
        self.assertIn('progressMax > 0', PRESENTATION)
        self.assertIn('mode: percent === null ? "indeterminate" : "determinate"', PRESENTATION)


class Phase8C9StageTransitionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "phase8c9-client",
        )
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.addCleanup(self.adapter.close)

    def test_18_nonnumeric_loading_models_is_indeterminate_input(self):
        self.adapter.handle_message(event("executing", node="2"))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["current_stage"], "Loading models")
        self.assertIsNone(snapshot["progress_percent"])

    def test_19_generating_twenty_five_percent_is_determinate_input(self):
        self.adapter.handle_message(event("progress", node="18", value=25, max=100))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["current_stage"], "Generating video")
        self.assertEqual(snapshot["progress_percent"], 25)

    def test_20_numeric_stage_to_nonnumeric_stage_becomes_indeterminate_input(self):
        self.adapter.handle_message(event("progress", node="18", value=80, max=100))
        self.adapter.handle_message(event("executing", node="21"))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID)["progress_percent"])

    def test_21_old_eighty_percent_is_not_retained(self):
        self.adapter.handle_message(event("progress", node="18", value=80, max=100))
        self.adapter.handle_message(event("executing", node="21"))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertIsNone(snapshot["progress_value"])
        self.assertIsNone(snapshot["progress_max"])

    def test_22_stage_label_changes_with_node(self):
        self.adapter.handle_message(event("executing", node="18"))
        self.assertEqual(self.adapter.snapshot(PROMPT_ID)["current_stage"], "Generating video")
        self.adapter.handle_message(event("executing", node="21"))
        self.assertEqual(self.adapter.snapshot(PROMPT_ID)["current_stage"], "Encoding video")

    def test_23_terminal_success_removes_progress(self):
        self.adapter.handle_message(event("progress", node="18", value=80, max=100))
        self.adapter.handle_message(event("execution_success"))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))
        self.assertIn("panel.hidden = !active", UPDATE)

    def test_24_terminal_failure_removes_progress(self):
        self.adapter.handle_message(event("progress", node="18", value=80, max=100))
        self.adapter.handle_message(event("execution_error", node_id="18"))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))
        self.assertNotIn('"FAILED"', ACTIVE_STATES)

    def test_25_cancelled_state_removes_progress(self):
        self.assertNotIn('"CANCELLED"', ACTIVE_STATES)
        self.assertIn('panel.dataset.state = active ? mode : "idle"', UPDATE)


class Phase8C9CssDomTests(unittest.TestCase):
    def test_26_progress_mode_classes_are_mutually_exclusive(self):
        self.assertIn('mode === "indeterminate"', UPDATE)
        self.assertIn('mode === "determinate"', UPDATE)
        self.assertIn('track.classList.remove("mvb-render-telemetry-track-indeterminate", "mvb-render-telemetry-track-determinate")', UPDATE)

    def test_27_progress_component_persists_without_card_replacement(self):
        branch = RENDER[RENDER.index("if (card.dataset.mvbRenderShellKey") : RENDER.index("card.className")]
        self.assertIn("updateRenderJobTelemetry(card, job)", branch)
        self.assertNotIn("replaceChildren", branch)

    def test_28_indeterminate_sweep_animation_is_defined(self):
        self.assertIn("@keyframes mvb-render-progress", CSS)
        self.assertIn("animation: mvb-render-progress 1.35s ease-in-out infinite", CSS)

    def test_29_reduced_motion_uses_nonpositional_pulse(self):
        reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn("mvb-render-progress-pulse", reduced)
        self.assertIn("will-change: opacity", reduced)
        self.assertNotIn("translate3d", reduced)

    def test_30_header_layout_avoids_horizontal_overflow(self):
        header_css = CSS[CSS.index(".mvb-render-heading {") : CSS.index(".mvb-render-status[data-state")]
        self.assertIn("minmax(140px, 1fr)", header_css)
        self.assertIn("min-width: 0", header_css)
        self.assertIn("white-space: nowrap", header_css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", CSS)

    def test_31_progress_palette_remains_gold_and_neutral(self):
        self.assertIn("var(--mvb-gold)", TELEMETRY_CSS)
        self.assertIn("var(--mvb-amber)", TELEMETRY_CSS)
        for forbidden in ("blue", "green", "cyan", "purple"):
            self.assertNotIn(forbidden, TELEMETRY_CSS.casefold())


class Phase8C9UiCopyAndActionsTests(unittest.TestCase):
    def test_32_stage_progress_label_is_removed(self):
        self.assertNotIn("STAGE PROGRESS", EXTENSION)

    def test_33_render_is_active_prose_is_removed(self):
        self.assertNotIn("Render is active.", EXTENSION)

    def test_34_rtx_commentary_remains_absent(self):
        self.assertNotIn("RTX 4080 SUPER is expected to render faster", EXTENSION)

    def test_35_scene_uuid_remains_absent_from_title(self):
        self.assertIn("name.textContent = `Scene ${scene.sequence}`", RENDER)
        self.assertNotIn("name.textContent = scene.scene_id", RENDER)

    def test_36_comfyui_acceptance_banner_remains_absent(self):
        self.assertNotIn("accepted by local ComfyUI", EXTENSION)

    def test_37_telemetry_debug_prose_remains_absent(self):
        self.assertNotIn("Numerical progress is not inferred", EXTENSION)
        self.assertNotIn("current_node_id", UPDATE)

    def test_38_aggregate_uses_not_ready_instead_of_blocked(self):
        summary_start = RENDER.index("const notReadyCount")
        summary_end = RENDER.index("status.dataset.state", summary_start)
        summary = RENDER[summary_start:summary_end]
        self.assertIn("not ready", summary)
        self.assertNotIn("blocked", summary.casefold())

    def test_39_orphaned_copy_is_concise(self):
        self.assertIn("Previous render is unavailable and can be retried.", RENDER)
        self.assertNotIn("marked orphaned", RENDER.casefold())

    def test_40_retryable_terminal_job_exposes_only_retry_path(self):
        self.assertIn("const retryableJob", RENDER)
        self.assertIn("if (!retryableJob)", RENDER)
        self.assertIn("if (retryableJob)", RENDER)
        self.assertIn("retryButton.dataset.mvbRenderRetry", RENDER)


if __name__ == "__main__":
    unittest.main()
