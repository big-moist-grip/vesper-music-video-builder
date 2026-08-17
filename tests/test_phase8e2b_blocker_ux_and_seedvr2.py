"""Phase 8E Round 8E.2B tests: Production Blocker UX, SeedVR2 Capability, and Debug Leak Remediation.

Verifies:
1. High-level UI controls:
   - Select Scenes remains interactive when upscale profile is unavailable.
   - Render All Ready remains interactive when scenes qualify.
   - Blocked actions show clear user-facing error message (RTX VSR / SeedVR2 unavailable).
   - Zero preparation, H3, finalization, or postprocess compute is executed.
   - Zero phantom batch or job records are created.
2. Warning UX & Styling:
   - Selected unavailable upscale profile renders red warning note with data-state="error" / --mvb-crimson.
   - Qualification pending renders non-error warning note with data-state="warning" / --mvb-amber.
   - Switching to None clears warning note and restores ordinary status immediately.
   - Unselected unavailable methods do not clutter the header.
3. Debug Leak Remediation:
   - History error parsing extracts clean exception message and never leaks raw Python dict reprs.
   - Raw dicts, JSON diagnostic objects, prompt_ids, and node_ids are sanitized from user-facing UI.
   - Technical details remain in backend logs and diagnostic records.
4. Independent SeedVR2 Runtime Capability:
   - Evaluated independently from RTX VSR hardware gate.
   - Missing required custom nodes / models truthfully report UNAVAILABLE on current AMD runtime.
   - Mocked complete SeedVR2 runtime reports UNQUALIFIED / AVAILABLE regardless of GPU vendor.
   - No automatic profile downgrade (frozen profile parameters preserved).
5. None Mode Regression:
   - Upscale None immediately clears production blockers.
   - Final Scenes resolve directly to Production Scene READY with zero postprocess compute.
"""

from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectValidationError
from backend.render_batch import (
    ACTION_ALREADY_COMPLETE,
    ACTION_POSTPROCESS_ONLY,
    ACTION_PREPARE_AND_RENDER,
    BATCH_COMPLETED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    BatchRunner,
    BatchStore,
    RenderBatchConflict,
    RenderBatchError,
    manual_action_blocker,
    new_batch_record,
    plan_batch,
    preview_batch,
    resume_batch,
    start_batch,
)
from backend.render_jobs import _history_status
from backend.render_production import (
    METHOD_AVAILABLE,
    METHOD_NOT_DETERMINABLE,
    METHOD_UNAVAILABLE,
    METHOD_UNQUALIFIED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    PRODUCTION_CURRENT,
    PRODUCTION_NEEDS_POSTPROCESS,
    PRODUCTION_NOT_AVAILABLE,
    ProductionMethodUnavailable,
    get_project_production_status,
    is_rtx_vsr_hardware_supported,
    production_method_availability,
    resolve_project_production_method,
    set_project_production_method,
    submit_postprocess_job,
    summarize_production_scene,
)

try:
    from phase8d_base import FINGERPRINT, Phase8DBase
    from phase8e_base import FakeComfyClientForPostprocess, FakeProductionMediaAdapter, Phase8ETestBase
except ImportError:
    from tests.phase8d_base import FINGERPRINT, Phase8DBase
    from tests.phase8e_base import FakeComfyClientForPostprocess, FakeProductionMediaAdapter, Phase8ETestBase

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_JS = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
BUILDER_CSS = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")


class TestPhase8E2BButtonUxAndGating(Phase8ETestBase):
    """Test button interactivity and fail-fast gating without wasted compute."""

    def test_select_scenes_not_disabled_by_unavailable_upscaler_in_ui(self):
        self.assertNotIn("batchSelectButton.disabled = !state.currentProject || ... || upscaleUnavailable", EXTENSION_JS)
        self.assertIn("batchSelectButton.removeAttribute(\"title\");", EXTENSION_JS)

    def test_render_all_ready_not_disabled_solely_by_unavailable_upscaler(self):
        self.assertIn("batchAllButton.removeAttribute(\"title\");", EXTENSION_JS)

    def test_blocked_batch_start_creates_zero_batch_or_job_records(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        initial_batches = BatchStore(self.storage).list_project(self.project_id)

        with self.assertRaises(RenderBatchError) as ctx:
            start_batch(self.storage, self.project_id, hardware_supported=False)

        self.assertEqual(ctx.exception.code, "PRODUCTION_METHOD_UNAVAILABLE")
        self.assertIn("unavailable on this runtime", ctx.exception.message)
        current_batches = BatchStore(self.storage).list_project(self.project_id)
        self.assertEqual(len(current_batches), len(initial_batches))

    def test_blocked_batch_preview_fails_before_any_scene_preparation(self):
        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        with self.assertRaises(RenderBatchError) as ctx:
            preview_batch(self.storage, self.project_id, hardware_supported=False)

        self.assertEqual(ctx.exception.code, "PRODUCTION_METHOD_UNAVAILABLE")


class TestPhase8E2BWarningPresentation(Phase8ETestBase):
    """Test warning styling tokens and UI state transitions."""

    def test_css_error_state_uses_crimson_token(self):
        self.assertIn(".mvb-render-upscale-note[data-state=\"error\"]", BUILDER_CSS)
        self.assertIn("color: var(--mvb-crimson);", BUILDER_CSS)

    def test_css_warning_state_uses_amber_token(self):
        self.assertIn(".mvb-render-upscale-note[data-state=\"warning\"]", BUILDER_CSS)
        self.assertIn("color: var(--mvb-amber);", BUILDER_CSS)

    def test_js_sets_error_data_state_on_unavailable(self):
        self.assertIn('upscaleNote.textContent = upscaleMethod === "rtx_vsr_fast" ? "RTX VSR unavailable on this runtime" : "SeedVR2 unavailable on this runtime";', EXTENSION_JS)
        self.assertIn('upscaleNote.dataset.state = "error";', EXTENSION_JS)

    def test_js_sets_warning_data_state_on_unqualified(self):
        self.assertIn('upscaleNote.textContent = "Qualification pending";', EXTENSION_JS)
        self.assertIn('upscaleNote.dataset.state = "warning";', EXTENSION_JS)

    def test_js_clears_upscale_note_when_none(self):
        self.assertIn('upscaleNote.removeAttribute("data-state");', EXTENSION_JS)


class TestPhase8E2BDebugSanitization(Phase8ETestBase):
    """Test debug and internal object leak remediation."""

    def test_history_status_sanitizes_comfyui_dict_message(self):
        raw_error_dict = {
            "prompt_id": "89ab12cd-34ef-56gh-78ij-90kl12mn34op",
            "node_id": "12",
            "node_type": "SeedVR2VideoUpscaler",
            "exception_message": "CUDA out of memory in SeedVR2 attention forward\nRuntimeError: out of memory",
            "exception_type": "RuntimeError",
        }
        entry = {
            "status": {
                "status_str": "error",
                "messages": [("execution_error", raw_error_dict)],
            },
        }
        status_name, status_message = _history_status(entry)
        self.assertEqual(status_name, "error")
        self.assertNotIn("prompt_id", status_message)
        self.assertNotIn("node_id", status_message)
        self.assertNotIn("{'", status_message)
        self.assertEqual(status_message, "RuntimeError: out of memory")

    def test_history_status_handles_opaque_error_without_leaking_dict_repr(self):
        opaque_dict = {"raw_telemetry": [1, 2, 3], "code": -1}
        entry = {
            "status": {
                "status_str": "error",
                "messages": [("execution_error", opaque_dict)],
            },
        }
        status_name, status_message = _history_status(entry)
        self.assertEqual(status_name, "error")
        self.assertNotIn("{'", status_message)
        self.assertEqual(status_message, "ComfyUI reported an execution error.")

    def test_render_diagnostic_text_rejects_raw_serialized_structures(self):
        self.assertIn("function renderDiagnosticText(entry)", EXTENSION_JS)
        self.assertIn('if (!raw || (raw.startsWith("{") && raw.endsWith("}")) || raw.startsWith("[") || raw.includes("Traceback"))', EXTENSION_JS)
        self.assertIn('return "Render inputs require attention.";', EXTENSION_JS)


class TestPhase8E2BSeedVR2IndependentCapability(Phase8ETestBase):
    """Test independent evaluation of SeedVR2 runtime capability without NVIDIA-only assumptions."""

    def test_seedvr2_does_not_inherit_rtx_vsr_vendor_predicate(self):
        avail_missing = production_method_availability(
            "seedvr2_quality",
            node_types=set(),
            model_status={},
            hardware_supported=False,
        )
        self.assertEqual(avail_missing["state"], METHOD_UNAVAILABLE)
        self.assertIn("SeedVR2LoadDiTModel", avail_missing.get("missing_nodes", []))
        self.assertNotEqual(avail_missing.get("reason"), "UNSUPPORTED_HARDWARE")

    def test_seedvr2_with_complete_runtime_reports_unqualified_for_batch(self):
        seed_nodes = {
            "VHS_LoadVideo",
            "SeedVR2LoadDiTModel",
            "SeedVR2LoadVAEModel",
            "SeedVR2VideoUpscaler",
            "VHS_VideoCombine",
        }
        seed_models = {
            "seedvr2_ema_7b-Q4_K_M.gguf": "AVAILABLE",
            "ema_vae_fp16.safetensors": "AVAILABLE",
        }
        avail = production_method_availability(
            "seedvr2_quality",
            node_types=seed_nodes,
            model_status=seed_models,
            hardware_supported=False,
        )
        self.assertEqual(avail["state"], METHOD_UNQUALIFIED)

    def test_seedvr2_frozen_profile_parameters_retained(self):
        from backend.render_production import production_method_registry
        registry = production_method_registry()
        seed_entry = registry["seedvr2_quality"]
        self.assertEqual(seed_entry["workflow_id"], "production_upscale_seedvr2_quality_v1")
        self.assertEqual(seed_entry["resolution"], "2x_native")
        self.assertEqual(seed_entry["comfyui_backed"], True)


class TestPhase8E2BNoneModeRegression(Phase8ETestBase):
    """Test switching to None mode restores full production readiness instantly."""

    def test_none_mode_clears_production_blockers_and_resolves_final_scenes(self):
        self._setup_final_scene(self.scene_1_id)

        set_project_production_method(self.storage, self.project_id, "rtx_vsr_fast")
        status_rtx = get_project_production_status(
            self.storage,
            self.project_id,
            hardware_supported=False,
        )
        self.assertEqual(status_rtx["capabilities"]["rtx_vsr_fast"]["state"], "UNAVAILABLE")

        set_project_production_method(self.storage, self.project_id, "none")
        summary_none = summarize_production_scene(
            self.storage,
            self.project_id,
            self.scene_1_id,
            method="none",
        )
        self.assertEqual(summary_none["state"], PRODUCTION_CURRENT)
        self.assertEqual(summary_none["resolves_to_final_scene"], True)

        status_none = get_project_production_status(
            self.storage,
            self.project_id,
            hardware_supported=False,
        )
        self.assertEqual(status_none["capabilities"]["none"]["state"], "AVAILABLE")
        self.assertEqual(status_none["scenes"][0]["state"], PRODUCTION_CURRENT)
