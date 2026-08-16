"""Phase 8C.10 Renders title and internal-identifier cleanup contracts."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "builder.css").read_text(encoding="utf-8")
RENDER = EXTENSION[
    EXTENSION.index("function renderRenderState") : EXTENSION.index("async function loadRenderJobs")
]
PREPARE = EXTENSION[
    EXTENSION.index("async function prepareRenderScene") : EXTENSION.index("async function copyPrompt")
]
RENDER_JOBS = (ROOT / "backend" / "render_jobs.py").read_text(encoding="utf-8")
TELEMETRY = (ROOT / "backend" / "render_telemetry.py").read_text(encoding="utf-8")


def css_rule(selector: str) -> str:
    start = CSS.index(f"{selector} {{")
    return CSS[start : CSS.index("}", start) + 1]


class Phase8C10TitleTests(unittest.TestCase):
    def test_01_visible_renders_title_exists(self):
        self.assertIn(
            '<h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>',
            EXTENSION,
        )
        self.assertIn('aria-labelledby="mvb-render-heading"', EXTENSION)

    def test_02_title_has_dedicated_page_title_style(self):
        rule = css_rule(".mvb-render-page-title")
        self.assertIn("font-size: clamp(21px, 1.5vw, 24px)", rule)
        self.assertIn("font-weight: 750", rule)
        self.assertIn("color: var(--mvb-text)", rule)

    def test_03_title_hierarchy_is_stronger_than_scene_label(self):
        title_rule = css_rule(".mvb-render-page-title")
        scene_rule = css_rule(".mvb-render-card-title h3")
        self.assertIn("24px", title_rule)
        self.assertIn("font-weight: 750", title_rule)
        self.assertIn("font-size: 11px", scene_rule)

    def test_04_renders_has_one_workspace_heading(self):
        headings = re.findall(r"<h[1-6][^>]*>Renders</h[1-6]>", EXTENSION)
        self.assertEqual(headings, ['<h2 class="mvb-render-page-title" id="mvb-render-heading">Renders</h2>'])
        self.assertNotIn("Render preparation", EXTENSION)
        self.assertNotIn("Scene readiness", EXTENSION)

    def test_05_neutral_header_summary_is_retained(self):
        self.assertIn("ready · ${notReadyCount} not ready", RENDER)
        self.assertIn('${activeJobCount} active', RENDER)
        self.assertIn(': "neutral";', RENDER)

    def test_06_direct_header_controls_are_retained(self):
        self.assertIn("data-mvb-render-refresh", EXTENSION)
        self.assertIn("data-mvb-render-rescan", EXTENSION)
        self.assertNotIn("mvb-render-advanced", EXTENSION)


class Phase8C10IdentifierLeakageTests(unittest.TestCase):
    def test_07_scene_uuid_is_not_rendered_in_card(self):
        self.assertIn("name.textContent = `Scene ${scene.sequence}`", RENDER)
        self.assertNotIn("name.textContent = scene.scene_id", RENDER)
        self.assertNotIn("title.textContent = scene.scene_id", RENDER)

    def test_08_scene_uuid_is_not_rendered_after_preparation(self):
        self.assertNotIn("result.preparation?.scene_id", PREPARE)
        self.assertNotIn("Prepared Scene", PREPARE)
        self.assertNotIn("${sceneId}", PREPARE)

    def test_09_job_id_is_not_assigned_to_visible_card_copy(self):
        self.assertNotIn("textContent = job.job_id", RENDER)
        self.assertNotIn("textContent = job?.job_id", RENDER)
        self.assertNotIn("`${job.job_id}`", RENDER)

    def test_10_prompt_id_is_not_assigned_to_visible_card_copy(self):
        self.assertNotIn("textContent = job.comfy_prompt_id", RENDER)
        self.assertNotIn("textContent = job?.comfy_prompt_id", RENDER)
        self.assertNotIn("prompt_id", RENDER)

    def test_11_preparation_fingerprint_is_not_visible(self):
        fingerprint_lines = [line for line in RENDER.splitlines() if "preparation_fingerprint" in line]
        self.assertEqual(len(fingerprint_lines), 1)
        self.assertTrue(all("textContent" not in line and "title" not in line for line in fingerprint_lines))

    def test_12_request_and_source_fingerprints_are_not_in_renders_dom(self):
        self.assertNotIn("request_fingerprint", RENDER)
        self.assertNotIn("source_fingerprint", RENDER)
        self.assertNotIn("saved_relay_fingerprint", RENDER)

    def test_13_internal_identifiers_remain_in_backend_records_and_logs(self):
        self.assertIn('"job_id": str(uuid.uuid4())', RENDER_JOBS)
        self.assertIn('"preparation_fingerprint": preparation_fingerprint', RENDER_JOBS)
        self.assertIn("Render job submitted job_id=%s prompt_id=%s scene_id=%s", RENDER_JOBS)
        self.assertIn("Accepted ComfyUI telemetry client_id=%s prompt_id=%s", TELEMETRY)

    def test_14_actionable_errors_remain_inside_scene_numbered_card(self):
        self.assertIn("name.textContent = `Scene ${scene.sequence}`", RENDER)
        self.assertIn('detail.textContent = job.failure?.message || "The render job failed."', RENDER)
        self.assertIn("detail.textContent = renderDiagnosticText(blockers[0])", RENDER)


class Phase8C10PreparationFeedbackTests(unittest.TestCase):
    def test_15_successful_preparation_refreshes_authoritative_tile(self):
        self.assertIn('if (result?.status === "prepared")', PREPARE)
        self.assertIn("await loadRenderPreflight(root, true)", PREPARE)
        self.assertIn('["Preparation", scene.preparation_status === "current" ? "PREPARED"', RENDER)

    def test_16_success_has_no_persistent_technical_banner(self):
        success = PREPARE[PREPARE.index('if (result?.status === "prepared")') : PREPARE.index("} else {")]
        self.assertIn('builderState.renderMessage = ""', success)
        self.assertIn('builderState.renderMessageState = "ready"', success)
        self.assertNotIn("timing?.", success)

    def test_17_no_transient_feedback_contains_uuid(self):
        self.assertNotIn("result.preparation?.scene_id", PREPARE)
        self.assertNotRegex(PREPARE, r"renderMessage\s*=\s*`[^`]*\$\{sceneId\}")

    def test_18_workflow_validation_debug_prose_is_absent(self):
        self.assertNotIn("workflow validated", EXTENSION.casefold())

    def test_19_product_tiles_retain_preparation_details(self):
        for label in ('["Duration",', '["H3 plan",', '["Preparation",'):
            self.assertIn(label, RENDER)

    def test_20_progress_component_is_unchanged_and_retained(self):
        self.assertIn("function renderTelemetryPresentation", EXTENSION)
        self.assertIn("mvb-render-telemetry-track-indeterminate", EXTENSION)
        self.assertIn("mvb-render-telemetry-track-determinate", EXTENSION)


if __name__ == "__main__":
    unittest.main()
