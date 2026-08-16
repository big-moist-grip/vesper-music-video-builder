"""Phase 8C.1 AMD/RTX eligibility and frame-plan presentation contracts."""

from pathlib import Path
import unittest

from backend.render import build_execution_eligibility, build_h3_timing_plan


ROOT = Path(__file__).parents[1]


class Phase8C1TestCase(unittest.TestCase):
    def test_6679_ms_plan_keeps_exact_internal_timing_and_explicit_trim(self):
        plan = build_h3_timing_plan(6_679)

        self.assertEqual(plan["generated_frame_count"], 175)
        self.assertEqual(plan["generated_duration_ms"], 7291.666667)
        self.assertEqual(plan["trim_duration_ms"], 612.666667)
        self.assertEqual(plan["authoritative_audio_duration_ms"], 6_679)
        self.assertTrue(plan["coverage_satisfied"])
        self.assertTrue(plan["trim_required"])

    def test_scene_execution_eligibility_uses_real_dimensions_only(self):
        ready = {
            "content_ready": True,
            "workflow_ready": True,
            "runtime_requirements_ready": True,
            "preparation_current": True,
        }
        eligible = build_execution_eligibility(ready)
        self.assertTrue(eligible["eligible"])
        self.assertEqual(eligible["status"], "READY")

        blocked = build_execution_eligibility(dict(ready, runtime_requirements_ready=False))
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["blockers"][0]["code"], "RUNTIME_REQUIREMENTS_NOT_READY")

    def test_render_card_uses_concise_seconds_and_trim_labels(self):
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("toFixed(3)} s", extension)
        self.assertIn("Required · ${Math.round", extension)
        self.assertNotIn("${timingPlan.generated_duration_ms} ms nominal", extension)
        self.assertIn('renderButton.textContent = "Render Scene"', extension)
        self.assertNotIn("Render deferred", extension)

    def test_hardware_policy_is_informational_and_shared_quality_is_retained(self):
        render = (ROOT / "backend" / "render.py").read_text(encoding="utf-8")
        jobs = (ROOT / "backend" / "render_jobs.py").read_text(encoding="utf-8")
        extension = (ROOT / "web" / "extension.js").read_text(encoding="utf-8")

        self.assertIn("SUPPORTED_MULTI_GPU", render)
        self.assertIn("AMD Radeon RX 7900 XT", render)
        self.assertIn("NVIDIA RTX 4080 SUPER 16 GB", render)
        self.assertIn("same production workflow and quality settings", render)
        self.assertIn("build_execution_eligibility", jobs)
        self.assertNotIn("DEFERRED_TARGET_NVIDIA", jobs)
        self.assertNotIn("TARGET_HARDWARE_DEFERRED", render)
        self.assertNotIn("performance_preference", extension)
        self.assertIn("execution_eligibility", extension)


if __name__ == "__main__":
    unittest.main()
