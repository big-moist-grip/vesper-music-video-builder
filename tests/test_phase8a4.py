import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend import requirements
from backend.requirements import (
    STATUS_AVAILABLE,
    build_requirements_report,
    clear_requirements_snapshot_cache,
    get_requirements_snapshot,
    requirements_snapshot_cache_info,
    scan_requirements,
)
from backend.workflows import production_manifest_registry


ROOT = Path(__file__).parents[1]


class Phase8A4RequirementsCacheTestCase(unittest.TestCase):
    def setUp(self):
        clear_requirements_snapshot_cache()

    def tearDown(self):
        clear_requirements_snapshot_cache()

    @staticmethod
    def _report(marker):
        return {
            "response_version": 1,
            "source": "test.runtime-cache",
            "methods": {"keyframe_i2v": {"ready": True, "marker": marker}},
            "shared": {"nodes": [], "models": []},
            "required_tools": {
                "ffmpeg": {"status": STATUS_AVAILABLE},
                "ffprobe": {"status": STATUS_AVAILABLE},
            },
            "required_ready": True,
            "optional": {},
        }

    def test_first_preflight_discovery_is_authoritative_and_warm_call_reuses_snapshot(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            return self._report(calls[-1])

        first = get_requirements_snapshot(scanner=scanner, cache_key="cache-test")
        second = get_requirements_snapshot(scanner=scanner, cache_key="cache-test")

        self.assertEqual(calls, [1])
        self.assertEqual(first.cache_status, "cold")
        self.assertEqual(second.cache_status, "warm")
        self.assertEqual(first.report_copy()["methods"]["keyframe_i2v"]["marker"], 1)
        self.assertEqual(second.report_copy()["methods"]["keyframe_i2v"]["marker"], 1)

    def test_cached_report_is_copied_and_does_not_expose_mutable_process_state(self):
        scanner = lambda: self._report("stable")
        first = get_requirements_snapshot(scanner=scanner, cache_key="copy-test")
        mutated = first.report_copy()
        mutated["methods"]["keyframe_i2v"]["marker"] = "changed"
        second = get_requirements_snapshot(scanner=scanner, cache_key="copy-test")

        self.assertEqual(second.report_copy()["methods"]["keyframe_i2v"]["marker"], "stable")

    def test_force_refresh_bypasses_cache_and_replaces_cached_snapshot(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            return self._report(calls[-1])

        get_requirements_snapshot(scanner=scanner, cache_key="force-test")
        rescanned = get_requirements_snapshot(scanner=scanner, cache_key="force-test", force_refresh=True)
        warm = get_requirements_snapshot(scanner=scanner, cache_key="force-test")

        self.assertEqual(calls, [1, 2])
        self.assertEqual(rescanned.cache_status, "rescan")
        self.assertEqual(warm.cache_status, "warm")
        self.assertEqual(warm.report_copy()["methods"]["keyframe_i2v"]["marker"], 2)

    def test_explicit_invalidation_and_contract_identity_change_force_new_scan(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            return self._report(calls[-1])

        get_requirements_snapshot(scanner=scanner, cache_key="contract-v1")
        get_requirements_snapshot(scanner=scanner, cache_key="contract-v1")
        get_requirements_snapshot(scanner=scanner, cache_key="contract-v2")
        self.assertEqual(calls, [1, 2])

        clear_requirements_snapshot_cache()
        get_requirements_snapshot(scanner=scanner, cache_key="contract-v2")
        self.assertEqual(calls, [1, 2, 3])

    def test_failed_scan_returns_structured_error_and_successful_retry_replaces_it(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise RuntimeError("temporary runtime inspection failure")
            return self._report("recovered")

        failed = get_requirements_snapshot(scanner=scanner, cache_key="retry-test")
        recovered = get_requirements_snapshot(scanner=scanner, cache_key="retry-test")
        warm = get_requirements_snapshot(scanner=scanner, cache_key="retry-test")

        self.assertEqual(calls, [1, 2])
        self.assertEqual(failed.source_state, "error")
        self.assertEqual(failed.report["required_ready"], False)
        self.assertIn("temporary runtime inspection failure", failed.report["error"])
        self.assertEqual(recovered.source_state, "success")
        self.assertEqual(recovered.cache_status, "cold")
        self.assertEqual(warm.cache_status, "warm")

    def test_identical_concurrent_scans_coalesce(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        results = []
        errors = []

        def scanner():
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(2))
            return self._report("coalesced")

        def worker():
            try:
                results.append(get_requirements_snapshot(scanner=scanner, cache_key="concurrent-test"))
            except Exception as error:  # pragma: no cover - assertion below reports unexpected thread errors
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.03)
        self.assertEqual(len(calls), 1)
        release.set()
        for thread in threads:
            thread.join(2)

        self.assertFalse(errors)
        self.assertEqual(len(results), len(threads))
        self.assertEqual({result.report_copy()["methods"]["keyframe_i2v"]["marker"] for result in results}, {"coalesced"})
        self.assertLessEqual(sum(result.cache_status == "cold" for result in results), 1)

    def test_build_report_uses_cache_and_force_rescan_without_second_requirements_system(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            return self._report(calls[-1])

        with patch.object(requirements, "scan_requirements", side_effect=scanner):
            first = build_requirements_report()
            second = build_requirements_report()
            forced = build_requirements_report(force_refresh=True)

        self.assertEqual(calls, [1, 2])
        self.assertEqual(first["methods"]["keyframe_i2v"]["marker"], 1)
        self.assertEqual(second["methods"]["keyframe_i2v"]["marker"], 1)
        self.assertEqual(forced["methods"]["keyframe_i2v"]["marker"], 2)

    def test_ffmpeg_ffprobe_and_node_model_discovery_are_reused_after_first_scan(self):
        registry = production_manifest_registry()
        node_types = {
            node_type
            for manifest in registry.values()
            for node_type in manifest["required_node_types"]
        }
        model_categories = {
            declaration["category"]
            for manifest in registry.values()
            for declaration in manifest["required_models"]
        }
        binary_calls = []

        class FolderPaths:
            folder_names_and_paths = {category: [] for category in model_categories}

            @staticmethod
            def get_full_path(_category, filename):
                return f"C:/cached-models/{filename}"

            @staticmethod
            def get_filename_list(_category):
                return []

        def binary_finder(name):
            binary_calls.append(name)
            return f"C:/cached-tools/{name}.exe"

        def scanner():
            return scan_requirements(
                node_types=node_types,
                folder_paths_module=FolderPaths(),
                binary_finder=binary_finder,
            )

        first = get_requirements_snapshot(scanner=scanner, cache_key="runtime-discovery-test")
        second = get_requirements_snapshot(scanner=scanner, cache_key="runtime-discovery-test")

        self.assertEqual(first.source_state, "success")
        self.assertEqual(second.cache_status, "warm")
        self.assertEqual(binary_calls, ["ffmpeg", "ffprobe"])

    def test_cache_does_not_write_project_or_schema_state(self):
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "project.json"
            sentinel.write_text('{"schema_version": 7}', encoding="utf-8")
            before = sentinel.read_bytes()
            get_requirements_snapshot(scanner=lambda: self._report("no-project-write"), cache_key="read-only-test")
            self.assertEqual(sentinel.read_bytes(), before)
            self.assertEqual(requirements_snapshot_cache_info()["status"], "cold")

    def test_frontend_exposes_force_rescan_loading_truth_and_stable_cards(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        routes = (ROOT / "backend" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("data-mvb-render-rescan", extension)
        self.assertIn("force=1", extension)
        self.assertIn("Inspecting runtime requirements…", extension)
        self.assertIn("if (builderState.renderState === \"loading\")", extension)
        self.assertIn("mvbRenderCard", extension)
        self.assertIn("force_requirements_refresh", routes)
        self.assertIn("get_requirements_snapshot", routes)

    def test_render_boundary_remains_no_queue_no_h3_and_eligibility_is_explicit(self):
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        self.assertNotIn("queue_prompt", render_source)
        self.assertNotIn("PromptServer", render_source)
        self.assertIn('"queue_submitted": False', render_source)
        self.assertIn("execution_eligibility", render_source)
        self.assertNotIn("Target H3 qualification deferred", extension)
        self.assertNotIn("Render deferred", extension)

    def test_snapshot_metadata_exposes_cache_provenance_and_duration(self):
        snapshot = get_requirements_snapshot(
            scanner=lambda: self._report("metadata"),
            cache_key="metadata-test",
        )

        metadata = snapshot.metadata()
        self.assertEqual(metadata["status"], "cold")
        self.assertEqual(metadata["source_state"], "success")
        self.assertEqual(metadata["cache_identity"], "metadata-test")
        self.assertGreaterEqual(metadata["scan_duration_ms"], 0)
        self.assertIsNone(metadata["error"])

    def test_cache_info_is_empty_after_process_invalidation(self):
        get_requirements_snapshot(scanner=lambda: self._report("invalidate"), cache_key="invalidate-test")
        self.assertEqual(requirements_snapshot_cache_info()["status"], "cold")

        clear_requirements_snapshot_cache()

        self.assertEqual(requirements_snapshot_cache_info(), {"status": "empty", "cache_identity": None})

    def test_cache_does_not_mutate_prompt_provenance_or_project_json(self):
        with TemporaryDirectory() as directory:
            project_file = Path(directory) / "project.json"
            original = {
                "schema_version": 7,
                "prompt_provenance": {"status": "CURRENT", "fingerprint": "sentinel"},
            }
            project_file.write_text(json.dumps(original), encoding="utf-8")
            before = project_file.read_bytes()

            get_requirements_snapshot(
                scanner=lambda: self._report("read-only"),
                cache_key="read-only-prompt-test",
            )

            self.assertEqual(project_file.read_bytes(), before)
            self.assertEqual(json.loads(project_file.read_text(encoding="utf-8")), original)

    def test_cache_metadata_is_isolated_from_project_schema(self):
        snapshot = get_requirements_snapshot(
            scanner=lambda: self._report("schema-isolated"),
            cache_key="schema-isolated-test",
        )

        self.assertNotIn("schema_version", snapshot.metadata())
        self.assertNotIn("project_id", snapshot.metadata())
        self.assertNotIn("schema_version", snapshot.report_copy())

    def test_manifest_change_changes_authoritative_cache_identity(self):
        with patch.object(requirements, "scan_requirements", return_value=self._report("manifest")):
            with patch.object(requirements, "production_manifest_registry", return_value={"contract": "one"}):
                first = get_requirements_snapshot()
            with patch.object(requirements, "production_manifest_registry", return_value={"contract": "two"}):
                second = get_requirements_snapshot()

        self.assertEqual(first.cache_status, "cold")
        self.assertEqual(second.cache_status, "cold")
        self.assertNotEqual(first.cache_identity, second.cache_identity)

    def test_force_rescan_reinvokes_authoritative_runtime_discovery(self):
        calls = []

        def scanner():
            calls.append(len(calls) + 1)
            return self._report(calls[-1])

        with patch.object(requirements, "scan_requirements", side_effect=scanner):
            first = get_requirements_snapshot()
            warm = get_requirements_snapshot()
            forced = get_requirements_snapshot(force_refresh=True)

        self.assertEqual(calls, [1, 2])
        self.assertEqual(first.report_copy()["methods"]["keyframe_i2v"]["marker"], 1)
        self.assertEqual(warm.cache_status, "warm")
        self.assertEqual(forced.cache_status, "rescan")
        self.assertEqual(forced.report_copy()["methods"]["keyframe_i2v"]["marker"], 2)

    def test_node_and_model_requirements_are_reused_from_valid_snapshot(self):
        calls = []

        def scanner():
            calls.append(1)
            report = self._report("node-model")
            report["shared"] = {"nodes": ["LoadVideo"], "models": ["h3.safetensors"]}
            return report

        first = get_requirements_snapshot(scanner=scanner, cache_key="node-model-test")
        second = get_requirements_snapshot(scanner=scanner, cache_key="node-model-test")

        self.assertEqual(len(calls), 1)
        self.assertEqual(second.cache_status, "warm")
        self.assertEqual(first.report_copy()["shared"], {"nodes": ["LoadVideo"], "models": ["h3.safetensors"]})
        self.assertEqual(second.report_copy()["shared"], first.report_copy()["shared"])

    def test_runtime_failure_is_fail_closed_with_truthful_discovery_status(self):
        snapshot = get_requirements_snapshot(
            scanner=lambda: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
            cache_key="truthful-runtime-error",
        )

        self.assertEqual(snapshot.source_state, "error")
        self.assertEqual(snapshot.report["discovery_status"], "ERROR")
        self.assertFalse(snapshot.report["required_ready"])
        self.assertIn("registry unavailable", snapshot.report["error"])

    def test_render_keeps_dynamic_scene_checks_outside_requirements_snapshot(self):
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")

        self.assertIn("prompt_scene_state(", render_source)
        self.assertIn("requirements_snapshot", render_source)
        self.assertNotIn("_PREFLIGHT_CACHE", render_source)

    def test_frontend_refresh_and_rescan_actions_share_duplicate_request_guard(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn('if (builderState.renderState === "loading")', extension)
        self.assertIn("refresh.disabled", extension)
        self.assertIn("rescan.disabled", extension)

    def test_frontend_cold_and_warm_refresh_messages_are_distinct(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("Inspecting runtime requirements…", extension)
        self.assertIn("Refreshing renders…", extension)
        self.assertIn('renderLoadingMode = forceRefresh || !builderState.renderRequirementsCache ? "runtime" : "scene"', extension)

    def test_frontend_warm_refresh_reconciles_existing_scene_cards_in_place(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("const existingCards = new Map", extension)
        self.assertIn("card.dataset.mvbRenderCard", extension)
        self.assertIn("card.replaceChildren(", extension)
        self.assertIn("telemetryPanel,", extension)
        self.assertNotIn("submissionDiagnostics", extension)
        self.assertIn("blockersList,", extension)
        self.assertIn("actions,", extension)

    def test_frontend_preserves_render_content_scroll_position(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("const contentScrollTop = content?.scrollTop ?? 0", extension)
        self.assertIn("content.scrollTop = contentScrollTop", extension)

    def test_frontend_does_not_add_queue_or_h3_execution_controls(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")

        self.assertNotIn("data-mvb-render-queue", extension)
        self.assertNotIn("queue_prompt", render_source)
        self.assertNotIn("PromptServer", render_source)

    def test_multi_gpu_execution_policy_is_informational_in_runtime_surface(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")

        self.assertIn("execution_eligibility", extension)
        self.assertNotIn("performance_preference", extension)
        self.assertIn("SUPPORTED_MULTI_GPU", render_source)
        self.assertIn("AMD Radeon RX 7900 XT", render_source)
        self.assertIn("NVIDIA RTX 4080 SUPER 16 GB", render_source)
        self.assertNotIn("DEFERRED_TARGET_NVIDIA", render_source)
        self.assertNotIn("TARGET_HARDWARE_STATUS", render_source)

    def test_workflow_contract_validation_remains_explicit_and_fail_closed(self):
        render_source = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")

        self.assertIn("validate_production_workflow_contract", render_source)
        self.assertIn("WORKFLOW_CONTRACT_INVALID", render_source)
        self.assertNotIn("_WORKFLOW_CONTRACT_CACHE", render_source)

    def test_changed_file_identity_is_rehashed_after_stat_signature_changes(self):
        from backend import render

        with TemporaryDirectory() as directory:
            media = Path(directory) / "media.bin"
            media.write_bytes(b"one")
            first_size, first_digest = render._cached_file_identity(media)
            media.write_bytes(b"a changed file")
            second_size, second_digest = render._cached_file_identity(media)

        self.assertNotEqual(first_size, second_size)
        self.assertNotEqual(first_digest, second_digest)


if __name__ == "__main__":
    unittest.main()
