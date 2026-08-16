"""Phase 8C.8 live telemetry transport and production Renders UI contracts."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from backend.render_jobs import ComfyUIClient, _overlay_telemetry_jobs
from backend.render_telemetry import ComfyUITelemetryAdapter


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
TELEMETRY_SOURCE = (ROOT / "backend" / "render_telemetry.py").read_text(encoding="utf-8")
JOBS_SOURCE = (ROOT / "backend" / "render_jobs.py").read_text(encoding="utf-8")
UPDATE_SOURCE = EXTENSION[
    EXTENSION.index("function updateRenderJobTelemetry") : EXTENSION.index("function createRenderJobTelemetry")
]
PRESENTATION_SOURCE = EXTENSION[
    EXTENSION.index("function renderTelemetryPresentation") : EXTENSION.index("function updateRenderJobTelemetry")
]
RENDER_SOURCE = EXTENSION[
    EXTENSION.index("function renderRenderState") : EXTENSION.index("async function loadRenderJobs")
]
PROMPT_ID = "phase8c8-owned-prompt"
FOREIGN_PROMPT_ID = "phase8c8-foreign-prompt"
WORKFLOW = {
    "2": {"class_type": "UNETLoader", "inputs": {}},
    "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}},
    "18": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
    "21": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


def event(event_type: str, prompt_id: str = PROMPT_ID, **data):
    return {"type": event_type, "data": {"prompt_id": prompt_id, **data}}


class _WaitingSocket:
    def __init__(self):
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def recv(self):
        if self.closed:
            raise ConnectionError("closed")
        raise socket.timeout()

    def close(self):
        self.closed = True


class Phase8C8TelemetryPipelineTests(unittest.TestCase):
    def test_submission_preconnects_with_the_same_owned_client_identity(self):
        connected = threading.Event()
        observed_urls = []

        def factory(url):
            observed_urls.append(url)
            connected.set()
            return _WaitingSocket()

        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            websocket_factory=factory,
            client_id_factory=lambda: "phase8c8-client-a",
            reconnect_delays=(0.01,),
        )
        self.addCleanup(adapter.close)
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", telemetry=adapter)
        requests = []

        def request(method, path, payload):
            self.assertTrue(connected.is_set())
            self.assertEqual(adapter.connection_state, "connected")
            requests.append((method, path, payload))
            return {"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}}

        with mock.patch.object(client, "_request_json", side_effect=request):
            result = client.submit_prompt(WORKFLOW)
        websocket_client = parse_qs(urlsplit(observed_urls[0]).query)["clientId"][0]
        self.assertEqual(websocket_client, "phase8c8-client-a")
        self.assertEqual(requests[0][2]["client_id"], websocket_client)
        self.assertEqual(result["prompt_id"], PROMPT_ID)

    def test_telemetry_unavailability_does_not_gate_http_submission(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "phase8c8-client-b",
        )
        self.addCleanup(adapter.close)
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", telemetry=adapter)
        with mock.patch.object(adapter, "ensure_connected", return_value=False), mock.patch.object(
            client,
            "_request_json",
            return_value={"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}},
        ) as request:
            result = client.submit_prompt(WORKFLOW)
        self.assertEqual(result["prompt_id"], PROMPT_ID)
        self.assertEqual(request.call_args.args[1], "/prompt")

    def test_installed_progress_and_progress_state_fixtures_are_accepted(self):
        adapter = self._adapter()
        installed_progress = {
            "type": "progress",
            "data": {"value": 42, "max": 100, "prompt_id": PROMPT_ID, "node": "18"},
        }
        installed_progress_state = {
            "type": "progress_state",
            "data": {
                "prompt_id": PROMPT_ID,
                "nodes": {"18": {"value": 43, "max": 100, "state": "running", "node_id": "18"}},
            },
        }
        self.assertTrue(adapter.handle_message(json.dumps(installed_progress)))
        self.assertTrue(adapter.handle_message(installed_progress_state))
        self.assertEqual(adapter.snapshot(PROMPT_ID)["progress_percent"], 43)

    def test_prompt_id_correlation_accepts_owned_and_rejects_foreign_events(self):
        adapter = self._adapter()
        self.assertFalse(adapter.handle_message(event("progress", FOREIGN_PROMPT_ID, value=90, max=100, node="18")))
        self.assertTrue(adapter.handle_message(event("progress", value=12, max=100, node="18")))
        self.assertEqual(adapter.snapshot(PROMPT_ID)["progress_percent"], 12)

    def test_progress_enters_latest_volatile_snapshot(self):
        adapter = self._adapter()
        adapter.handle_message(event("executing", node="18"))
        adapter.handle_message(event("progress", value=37, max=100, node="18"))
        snapshot = adapter.snapshot(PROMPT_ID)
        self.assertTrue(snapshot["observed"])
        self.assertEqual(snapshot["current_stage"], "Generating video")
        self.assertEqual(snapshot["progress_value"], 37)
        self.assertEqual(snapshot["progress_max"], 100)
        self.assertIsNotNone(snapshot["updated_at"])

    def test_status_overlay_exposes_available_or_unavailable_telemetry(self):
        adapter = self._adapter()
        client = type("Client", (), {"telemetry": adapter, "telemetry_worker_enabled": False})()
        record = {"job_id": "job", "comfy_prompt_id": PROMPT_ID, "state": "RUNNING", "progress": None}
        unavailable = _overlay_telemetry_jobs([record], client)[0]["telemetry"]
        self.assertFalse(unavailable["available"])
        self.assertFalse(unavailable["observed"])
        adapter.handle_message(event("progress", value=51, max=100, node="18"))
        available = _overlay_telemetry_jobs([record], client)[0]
        self.assertEqual(available["progress"]["percent"], 51)
        self.assertEqual(available["telemetry"]["current_stage"], "Generating video")

    def test_frontend_reads_backend_telemetry_and_progress_fields(self):
        for field in ("job?.telemetry", "telemetry?.current_stage", "progress?.stage", "progress?.raw_value", "progress?.raw_max"):
            self.assertIn(field, PRESENTATION_SOURCE)

    def test_telemetry_only_change_uses_narrow_update_branch(self):
        self.assertIn("progress: null", EXTENSION)
        self.assertIn("telemetry: null", EXTENSION)
        self.assertIn("updateRenderJobTelemetry(card, job);", RENDER_SOURCE)
        self.assertIn("continue;", RENDER_SOURCE)

    def test_missing_numeric_telemetry_selects_indeterminate_mode(self):
        self.assertIn('mode: percent === null ? "indeterminate" : "determinate"', PRESENTATION_SOURCE)
        self.assertIn('bar.style.removeProperty("width")', UPDATE_SOURCE)
        self.assertIn('track.removeAttribute("aria-valuenow")', UPDATE_SOURCE)

    def test_numeric_telemetry_selects_determinate_mode(self):
        self.assertIn('bar.style.width = `${percent}%`', UPDATE_SOURCE)
        self.assertIn('track.setAttribute("aria-valuenow", String(percent))', UPDATE_SOURCE)
        self.assertIn("track.dataset.mode = mode", UPDATE_SOURCE)

    def test_frontend_never_fabricates_overall_percentage(self):
        self.assertNotIn("elapsed", PRESENTATION_SOURCE.casefold())
        self.assertNotIn("estimate", PRESENTATION_SOURCE.casefold())
        self.assertNotIn("overall", PRESENTATION_SOURCE.casefold())
        self.assertIn('progress?.kind === "telemetry"', PRESENTATION_SOURCE)

    def test_exact_root_cause_dependency_is_repaired(self):
        self.assertIn("from websockets.sync.client import connect", TELEMETRY_SOURCE)
        self.assertNotIn("import websocket  # type: ignore", TELEMETRY_SOURCE)
        self.assertIn("telemetry unavailable before submission; continuing without gating", JOBS_SOURCE)

    def _adapter(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "phase8c8-client-c",
        )
        adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.addCleanup(adapter.close)
        return adapter


class Phase8C8ProgressUiTests(unittest.TestCase):
    def test_running_job_always_has_activity_region(self):
        self.assertIn("createRenderJobTelemetry()", RENDER_SOURCE)
        self.assertIn("panel.hidden = !active", UPDATE_SOURCE)
        self.assertIn("renderJobIsActive(job)", PRESENTATION_SOURCE)

    def test_no_telemetry_uses_indeterminate_bar(self):
        self.assertIn(".mvb-render-telemetry-track-indeterminate", CSS)
        self.assertIn("mvb-render-progress", CSS)

    def test_numeric_telemetry_uses_determinate_bar(self):
        self.assertIn(".mvb-render-telemetry-track-determinate", CSS)
        self.assertIn("transition: width 120ms ease-out", CSS)

    def test_percentage_copy_is_concise_stage_progress(self):
        self.assertIn('`${stageLabel} · ${percent}%`', UPDATE_SOURCE)
        self.assertNotIn("STAGE PROGRESS", EXTENSION)

    def test_friendly_stage_label_is_displayed_without_node_id(self):
        self.assertIn("Preparing conditioning", TELEMETRY_SOURCE)
        self.assertIn("Generating video", TELEMETRY_SOURCE)
        self.assertIn("Encoding video", TELEMETRY_SOURCE)
        self.assertNotIn("current_node_id", UPDATE_SOURCE)

    def test_node_stage_change_resets_numeric_progress_truthfully(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188", worker_enabled=False, client_id_factory=lambda: "phase8c8-reset"
        )
        self.addCleanup(adapter.close)
        adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        adapter.handle_message(event("progress", value=90, max=100, node="18"))
        adapter.handle_message(event("executing", node="21"))
        snapshot = adapter.snapshot(PROMPT_ID)
        self.assertIsNone(snapshot["progress_percent"])
        self.assertEqual(snapshot["current_stage"], "Encoding video")

    def test_succeeded_state_removes_progress_activity(self):
        self.assertNotIn('"SUCCEEDED"', EXTENSION[EXTENSION.index("function renderJobIsActive"):EXTENSION.index("function renderFinalizationLabel")])
        self.assertIn("panel.hidden = !active", UPDATE_SOURCE)

    def test_cancelled_state_removes_progress_activity(self):
        active_source = EXTENSION[EXTENSION.index("function renderJobIsActive"):EXTENSION.index("function renderFinalizationLabel")]
        self.assertNotIn('"CANCELLED"', active_source)

    def test_failed_state_removes_progress_activity(self):
        active_source = EXTENSION[EXTENSION.index("function renderJobIsActive"):EXTENSION.index("function renderFinalizationLabel")]
        self.assertNotIn('"FAILED"', active_source)

    def test_foreign_prompt_cannot_update_activity_state(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188", worker_enabled=False, client_id_factory=lambda: "phase8c8-foreign"
        )
        self.addCleanup(adapter.close)
        adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.assertFalse(adapter.handle_message(event("progress", FOREIGN_PROMPT_ID, value=100, max=100, node="18")))
        self.assertFalse(adapter.snapshot(PROMPT_ID)["observed"])

    def test_high_frequency_events_are_coalesced_to_latest_snapshot(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188", worker_enabled=False, client_id_factory=lambda: "phase8c8-coalesce"
        )
        self.addCleanup(adapter.close)
        adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        for value in range(101):
            adapter.handle_message(event("progress", value=value, max=100, node="18"))
        self.assertEqual(adapter.snapshot(PROMPT_ID)["progress_percent"], 100)
        self.assertEqual(len(adapter._snapshots), 1)

    def test_scroll_position_is_retained(self):
        self.assertIn("const contentScrollTop", RENDER_SOURCE)
        self.assertIn("content.scrollTop = contentScrollTop", RENDER_SOURCE)

    def test_telemetry_update_does_not_replace_scene_card(self):
        narrow_branch = RENDER_SOURCE[
            RENDER_SOURCE.index("if (card.dataset.mvbRenderShellKey") : RENDER_SOURCE.index("card.className")
        ]
        self.assertIn("updateRenderJobTelemetry(card, job)", narrow_branch)
        self.assertIn("continue;", narrow_branch)
        self.assertNotIn("replaceChildren", narrow_branch)


class Phase8C8RendersCleanupTests(unittest.TestCase):
    def test_navigation_label_is_exactly_renders(self):
        self.assertIn('data-mvb-view="render" type="button" role="tab" aria-selected="false">Renders</button>', EXTENSION)
        self.assertIn('<h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>', EXTENSION)
        self.assertIn('aria-labelledby="mvb-render-heading"', EXTENSION)

    def test_render_preparation_heading_is_absent(self):
        self.assertNotIn("Render preparation", EXTENSION)

    def test_scene_readiness_heading_is_absent(self):
        self.assertNotIn("Scene readiness", EXTENSION)

    def test_scene_uuid_is_not_primary_title_copy(self):
        self.assertIn("name.textContent = `Scene ${scene.sequence}`", RENDER_SOURCE)
        self.assertNotIn("name.textContent = scene.scene_id", RENDER_SOURCE)

    def test_accepted_by_comfyui_uuid_banner_is_absent(self):
        self.assertNotIn("accepted by local ComfyUI", EXTENSION)
        self.assertNotIn("accepted by ComfyUI", EXTENSION)

    def test_numerical_progress_debug_sentence_is_absent(self):
        self.assertNotIn("Numerical progress is not inferred", EXTENSION)

    def test_rtx_performance_commentary_is_absent(self):
        self.assertNotIn("RTX 4080 SUPER is expected to render faster", EXTENSION)
        self.assertNotIn("Supported on AMD and RTX hardware", EXTENSION)

    def test_explanatory_render_intro_is_absent(self):
        self.assertNotIn("Prepare exact scene audio, current visual inputs, timing", EXTENSION)
        self.assertNotIn("mvb-render-intro", EXTENSION)

    def test_product_status_tiles_are_retained(self):
        for label in (
            "Duration", "H3 plan", "Trim", "Method", "Visuals", "Prompt", "Requirements",
            "Preparation", "Render job", "Raw H3", "Final scene",
        ):
            self.assertIn(f'["{label}"', RENDER_SOURCE)

    def test_actionable_failure_message_remains_user_visible(self):
        self.assertIn('job.failure?.message || "The render job failed."', RENDER_SOURCE)
        self.assertIn("renderDiagnosticText(blocker)", RENDER_SOURCE)

    def test_technical_submission_details_are_not_dumped_in_primary_ui(self):
        self.assertNotIn("Submission diagnostics", RENDER_SOURCE)
        self.assertNotIn("submitted_value", RENDER_SOURCE)
        self.assertNotIn("affected_nodes", RENDER_SOURCE)

    def test_structured_logs_retain_technical_identifiers(self):
        self.assertIn("client_id=%s prompt_id=%s event=%s node_id=%s", TELEMETRY_SOURCE)
        self.assertIn("job_id=%s prompt_id=%s scene_id=%s", JOBS_SOURCE)
        self.assertIn("websocket_url=%s", TELEMETRY_SOURCE)

    def test_active_render_and_prepare_controls_are_disabled(self):
        self.assertIn("Boolean(job && renderJobIsActive(job)) || state.renderPreparingSceneId", RENDER_SOURCE)
        self.assertIn("|| Boolean(job && renderJobIsActive(job))", RENDER_SOURCE)

    def test_active_cancel_control_is_retained(self):
        self.assertIn("cancelButton.dataset.mvbRenderCancel", RENDER_SOURCE)
        self.assertIn('cancelButton.textContent = job.state === "CANCEL_REQUESTED" ? "Cancellation requested" : "Cancel"', RENDER_SOURCE)

    def test_runtime_rescan_is_direct_and_retained(self):
        self.assertNotIn('<details class="mvb-render-advanced">', EXTENSION)
        self.assertIn("data-mvb-render-rescan", EXTENSION)


if __name__ == "__main__":
    unittest.main()
