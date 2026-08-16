"""Phase 8C.5 installed ComfyUI progress telemetry contracts."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest

from backend.render_jobs import ComfyUIClient, _overlay_telemetry_jobs
from backend.render_telemetry import (
    ComfyUITelemetryAdapter,
    get_shared_telemetry_adapter,
    normalize_comfyui_progress,
    parse_comfyui_event,
    reset_shared_telemetry_adapters,
)


ROOT = Path(__file__).parents[1]
PROMPT_ID = "prompt-telemetry-1"
OTHER_PROMPT_ID = "prompt-telemetry-2"
WORKFLOW = {
    "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}},
    "18": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
    "21": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


# These wrappers and fields are copied from the installed ComfyUI runtime:
# main.py emits progress, comfy_execution/progress.py emits progress_state,
# and execution.py emits execution_start/executing/executed/execution_success.
INSTALLED_PROGRESS = {
    "type": "progress",
    "data": {"value": 17, "max": 100, "prompt_id": PROMPT_ID, "node": "18"},
}
INSTALLED_PROGRESS_STATE = {
    "type": "progress_state",
    "data": {
        "prompt_id": PROMPT_ID,
        "nodes": {
            "18": {
                "value": 17,
                "max": 100,
                "state": "running",
                "node_id": "18",
                "prompt_id": PROMPT_ID,
                "display_node_id": "18",
                "parent_node_id": None,
                "real_node_id": "18",
            },
        },
    },
}


def event(event_type, prompt_id=PROMPT_ID, **data):
    return {"type": event_type, "data": {"prompt_id": prompt_id, **data}}


class Phase8C5AdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "test-client-8c5",
        )
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.addCleanup(self.adapter.close)

    def test_exact_installed_progress_wrapper_is_parsed_and_normalized(self):
        self.assertEqual(parse_comfyui_event(json.dumps(INSTALLED_PROGRESS))["type"], "progress")
        self.assertTrue(self.adapter.handle_message(INSTALLED_PROGRESS))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["current_node_id"], "18")
        self.assertEqual(snapshot["current_node_type"], "SamplerCustomAdvanced")
        self.assertEqual(snapshot["stage"], "Generating video")
        self.assertEqual(snapshot["progress"]["percent"], 17)
        self.assertEqual(snapshot["progress"]["scope"], "node")
        self.assertTrue(snapshot["progress"]["node_local"])

    def test_exact_installed_progress_state_fixture_is_node_local(self):
        self.assertTrue(self.adapter.handle_message(INSTALLED_PROGRESS_STATE))
        progress = self.adapter.snapshot(PROMPT_ID)["progress"]
        self.assertEqual(progress["percent"], 17)
        self.assertEqual(progress["scope"], "node")
        self.assertNotIn("total_percent", progress)

    def test_normalization_rejects_malformed_or_non_positive_maximum(self):
        self.assertIsNone(normalize_comfyui_progress("17", 100))
        self.assertIsNone(normalize_comfyui_progress(17, 0))
        self.assertIsNone(normalize_comfyui_progress(17, float("nan")))
        self.assertEqual(normalize_comfyui_progress(150, 100)["percent"], 100)
        self.assertEqual(normalize_comfyui_progress(-5, 100)["percent"], 0)

    def test_malformed_websocket_messages_are_ignored(self):
        for message in (b"preview", "not json", {}, {"type": "progress"}, {"type": "progress", "data": []}):
            self.assertFalse(self.adapter.handle_message(message))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID)["progress"])

    def test_prompt_correlation_ignores_foreign_prompt(self):
        self.assertFalse(self.adapter.handle_message(event("progress", OTHER_PROMPT_ID, value=90, max=100, node="18")))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID)["progress"])
        self.assertIsNone(self.adapter.snapshot(OTHER_PROMPT_ID))

    def test_execution_start_and_executing_identify_current_node_and_stage(self):
        self.assertTrue(self.adapter.handle_message(event("execution_start")))
        self.assertTrue(self.adapter.handle_message(event("executing", node="8")))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["current_node_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(snapshot["stage"], "Preparing conditioning")
        self.assertEqual(snapshot["state"], "RUNNING")

    def test_executing_new_node_resets_previous_node_progress_truthfully(self):
        self.adapter.handle_message(event("executing", node="18"))
        self.adapter.handle_message(event("progress", value=80, max=100, node="18"))
        self.adapter.handle_message(event("executing", node="21"))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertIsNone(snapshot["progress"])
        self.assertTrue(snapshot["progress_reset"])
        self.assertEqual(snapshot["stage"], "Encoding video")

    def test_progress_state_without_reliable_pair_remains_running(self):
        payload = event("progress_state", nodes={"18": {"state": "running", "value": 1, "max": 0}})
        self.assertTrue(self.adapter.handle_message(payload))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["state"], "RUNNING")
        self.assertIsNone(snapshot["progress"])

    def test_executed_marks_node_and_clears_stale_node_progress(self):
        self.adapter.handle_message(event("executing", node="18"))
        self.adapter.handle_message(event("progress", value=100, max=100, node="18"))
        self.adapter.handle_message(event("executed", node="18"))
        snapshot = self.adapter.snapshot(PROMPT_ID)
        self.assertEqual(snapshot["completed_node_ids"], ["18"])
        self.assertIsNone(snapshot["progress"])

    def test_error_terminal_event_wins_and_clears_telemetry(self):
        self.adapter.handle_message(event("executing", node="18"))
        self.adapter.handle_message(event("progress", value=40, max=100, node="18"))
        self.assertTrue(self.adapter.handle_message(event("execution_error", node_id="18")))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))
        self.assertFalse(self.adapter.handle_message(event("execution_success")))

    def test_success_and_explicit_cancel_clear_telemetry(self):
        self.adapter.handle_message(event("execution_success"))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))
        self.adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("executing", node="18"))
        self.adapter.clear_prompt(PROMPT_ID)
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID))

    def test_multiple_owned_jobs_are_isolated(self):
        self.adapter.register_prompt(OTHER_PROMPT_ID, WORKFLOW, start=False)
        self.adapter.handle_message(event("executing", PROMPT_ID, node="18"))
        self.adapter.handle_message(event("executing", OTHER_PROMPT_ID, node="8"))
        self.adapter.handle_message(event("progress", PROMPT_ID, value=10, max=100, node="18"))
        self.adapter.handle_message(event("progress", OTHER_PROMPT_ID, value=90, max=100, node="8"))
        self.assertEqual(self.adapter.snapshot(PROMPT_ID)["progress"]["percent"], 10)
        self.assertEqual(self.adapter.snapshot(OTHER_PROMPT_ID)["progress"]["percent"], 90)

    def test_stale_retry_prompt_cannot_update_new_prompt(self):
        self.adapter.handle_message(event("execution_success"))
        new_prompt = "prompt-telemetry-retry"
        self.adapter.register_prompt(new_prompt, WORKFLOW, start=False)
        self.assertFalse(self.adapter.handle_message(event("progress", PROMPT_ID, value=80, max=100, node="18")))
        self.assertTrue(self.adapter.handle_message(event("progress", new_prompt, value=20, max=100, node="18")))
        self.assertEqual(self.adapter.snapshot(new_prompt)["progress"]["percent"], 20)

    def test_status_without_prompt_id_is_not_job_telemetry(self):
        self.assertFalse(self.adapter.handle_message({"type": "status", "data": {"status": {"exec_info": {}}}}))
        self.assertIsNone(self.adapter.snapshot(PROMPT_ID)["progress"])


class _ScriptedSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionError("test disconnect")

    def close(self):
        self.closed = True


class Phase8C5ConnectionTests(unittest.TestCase):
    def tearDown(self):
        reset_shared_telemetry_adapters()

    def test_reconnect_keeps_running_state_and_disconnect_is_not_failure(self):
        connected = threading.Event()
        sockets = [
            _ScriptedSocket([event("execution_start"), event("executing", node="18"), INSTALLED_PROGRESS]),
            _ScriptedSocket([event("progress", value=55, max=100, node="18")]),
        ]

        def factory(_url):
            connected.set()
            return sockets.pop(0) if sockets else _ScriptedSocket([])

        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            websocket_factory=factory,
            client_id_factory=lambda: "test-reconnect-8c5",
            reconnect_delays=(0.01,),
        )
        self.addCleanup(adapter.close)
        adapter.register_prompt(PROMPT_ID, WORKFLOW)
        self.assertTrue(connected.wait(1.0))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and adapter.connection_count < 2:
            time.sleep(0.01)
        snapshot = adapter.snapshot(PROMPT_ID)
        self.assertGreaterEqual(adapter.connection_count, 2)
        self.assertEqual(snapshot["state"], "RUNNING")
        self.assertNotEqual(snapshot["state"], "FAILED")

    def test_shared_adapter_has_one_session_identity_for_multiple_clients(self):
        first = get_shared_telemetry_adapter("http://127.0.0.1:8188")
        second = get_shared_telemetry_adapter("http://127.0.0.1:8188")
        self.assertIs(first, second)
        self.assertEqual(first.client_id, second.client_id)
        self.assertIn("clientId=", first.websocket_url)

    def test_http_submission_keeps_prompt_correlated_client_id(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}}

        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            transport=transport,
            telemetry=ComfyUITelemetryAdapter(
                "http://127.0.0.1:8188",
                worker_enabled=False,
                client_id_factory=lambda: "test-http-8c5",
            ),
        )
        result = client.submit_prompt(WORKFLOW)
        self.assertEqual(result["prompt_id"], PROMPT_ID)
        self.assertEqual(calls[0][2]["client_id"], "test-http-8c5")
        self.assertEqual(calls[0][2]["prompt"], WORKFLOW)

    def test_overlay_is_volatile_and_does_not_mutate_durable_job_record(self):
        adapter = ComfyUITelemetryAdapter(
            "http://127.0.0.1:8188",
            worker_enabled=False,
            client_id_factory=lambda: "test-overlay-8c5",
        )
        self.addCleanup(adapter.close)
        adapter.register_prompt(PROMPT_ID, WORKFLOW, start=False)
        adapter.handle_message(event("progress", value=33, max=100, node="18"))
        client = type("Client", (), {"telemetry": adapter, "telemetry_worker_enabled": False})()
        record = {"job_id": "job", "comfy_prompt_id": PROMPT_ID, "state": "RUNNING", "progress": {"kind": "lifecycle", "value": None}}
        original = dict(record)
        overlay = _overlay_telemetry_jobs([record], client)[0]
        self.assertEqual(overlay["progress"]["kind"], "telemetry")
        self.assertEqual(overlay["progress"]["percent"], 33)
        self.assertNotIn("telemetry", record)
        self.assertEqual(record, original)


class Phase8C5FrontendContractTests(unittest.TestCase):
    def test_render_ui_has_narrow_live_telemetry_surface(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
        self.assertIn("createRenderJobTelemetry", extension)
        self.assertIn("updateRenderJobTelemetry", extension)
        self.assertIn("data-mvb-render-telemetry", extension)
        self.assertIn("renderJobShellKey", extension)
        self.assertIn("contentScrollTop", extension)
        self.assertIn('panel.dataset.state = active ? mode : "idle"', extension)
        self.assertNotIn("STAGE PROGRESS", extension)
        self.assertIn("mvb-render-telemetry", css)

    def test_ui_never_labels_node_progress_as_total_progress(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        telemetry_start = extension.index("function updateRenderJobTelemetry")
        telemetry_end = extension.index("function createRenderJobTelemetry")
        telemetry = extension[telemetry_start:telemetry_end]
        self.assertNotIn("total", telemetry.casefold())
        self.assertIn("stage progress", telemetry.casefold())

    def test_ui_keeps_single_shared_poll_path_and_no_render_card_listener(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertEqual(extension.count("function loadRenderJobs("), 1)
        self.assertEqual(extension.count("function createRenderJobTelemetry("), 1)
        self.assertNotIn("new WebSocket", extension)


if __name__ == "__main__":
    unittest.main()
