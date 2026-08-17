"""Phase 8E tests: resolution policy, method availability, and input materialization."""

from pathlib import Path
from unittest.mock import patch

from backend.projects import ProjectValidationError
from backend.render_production import (
    METHOD_AVAILABLE,
    METHOD_NOT_DETERMINABLE,
    METHOD_UNAVAILABLE,
    METHOD_UNQUALIFIED,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    ProductionNotReady,
    RenderProductionError,
    materialize_final_scene_for_postprocess,
    method_runtime_requirements,
    native_source_dimensions,
    production_method_availability,
    production_resolution_policy,
    production_target_dimensions,
    rtx_vsr_dimension,
)
try:
    from phase8e_base import NATIVE_HEIGHT, NATIVE_WIDTH, Phase8ETestBase
except ImportError:
    from tests.phase8e_base import NATIVE_HEIGHT, NATIVE_WIDTH, Phase8ETestBase


class TestPhase8EResolutionPolicy(Phase8ETestBase):
    """Test the authoritative resolution policy and calculations."""

    def test_native_source_dimensions(self):
        w, h = native_source_dimensions()
        self.assertEqual(w, 960)
        self.assertEqual(h, 544)

    def test_rtx_vsr_dimension_multiple_of_8(self):
        self.assertEqual(rtx_vsr_dimension(1920.0), 1920)
        self.assertEqual(rtx_vsr_dimension(1088.0), 1088)
        self.assertEqual(rtx_vsr_dimension(1085.0), 1088)
        self.assertEqual(rtx_vsr_dimension(0.0), 8)

    def test_production_target_dimensions_none(self):
        target = production_target_dimensions("none", 960, 544)
        self.assertIsNone(target)

    def test_production_target_dimensions_rtx_vsr(self):
        target = production_target_dimensions("rtx_vsr_fast", 960, 544)
        self.assertEqual(target, (1920, 1088))

    def test_production_target_dimensions_seedvr2(self):
        target = production_target_dimensions("seedvr2_quality", 960, 544)
        self.assertEqual(target, (1920, 1088))

    def test_production_target_dimensions_invalid_inputs(self):
        with self.assertRaises(RenderProductionError):
            production_target_dimensions("rtx_vsr_fast", -1, 544)
        with self.assertRaises(RenderProductionError):
            production_target_dimensions("rtx_vsr_fast", 960, 0)
        with self.assertRaises(ProjectValidationError):
            production_target_dimensions("invalid", 960, 544)

    def test_production_resolution_policy_document(self):
        policy = production_resolution_policy()
        self.assertEqual(policy["native_source"], {"width": 960, "height": 544, "fps": 24})
        self.assertEqual(policy["source_aspect_ratio"], "30:17")
        self.assertEqual(policy["spatial_multiplier"], 2.0)
        self.assertTrue(policy["mod_8_safe"])
        self.assertTrue(policy["aspect_ratio_preserved"])
        self.assertTrue(policy["no_crop_no_stretch"])
        methods = policy["methods"]
        self.assertIsNone(methods["none"]["target"])
        self.assertEqual(methods["rtx_vsr_fast"]["target"], (1920, 1088))
        self.assertEqual(methods["seedvr2_quality"]["target"], (1920, 1088))

    def test_aspect_ratio_semantics_30_17_not_16_9(self):
        # 960x544 is mathematically 30:17, NOT literal 16:9 (960*9=8640 != 544*16=8704)
        w_native, h_native = native_source_dimensions()
        self.assertEqual(w_native, 960)
        self.assertEqual(h_native, 544)
        self.assertEqual(w_native * 17, h_native * 30)
        self.assertNotEqual(w_native * 9, h_native * 16)

        # 1920x1088 is mathematically 30:17, NOT literal 16:9 (1920*9=17280 != 1088*16=17408)
        w_target, h_target = 1920, 1088
        self.assertEqual(w_target * 17, h_target * 30)
        self.assertNotEqual(w_target * 9, h_target * 16)

        # Source aspect ratio is exactly preserved without crop or stretch
        self.assertEqual(w_native * h_target, h_native * w_target)

        # Exact 2x spatial multiplier and mod 8 safe
        self.assertEqual(w_target, 2 * w_native)
        self.assertEqual(h_target, 2 * h_native)
        self.assertEqual(w_target % 8, 0)
        self.assertEqual(h_target % 8, 0)


class TestPhase8ERuntimeAvailability(Phase8ETestBase):
    """Test method-specific lazy capability probing and hardware policy."""

    def test_method_runtime_requirements(self):
        none_reqs = method_runtime_requirements("none")
        self.assertEqual(none_reqs["node_types"], [])
        self.assertEqual(none_reqs["models"], [])

        rtx_reqs = method_runtime_requirements("rtx_vsr_fast")
        self.assertEqual(
            set(rtx_reqs["node_types"]),
            {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"},
        )
        self.assertEqual(rtx_reqs["models"], [])

        seed_reqs = method_runtime_requirements("seedvr2_quality")
        self.assertEqual(
            set(seed_reqs["node_types"]),
            {"VHS_LoadVideo", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel", "SeedVR2VideoUpscaler", "VHS_VideoCombine"},
        )
        self.assertEqual(len(seed_reqs["models"]), 2)

    def test_none_method_always_available(self):
        # Even with no node types or models, None is always AVAILABLE
        avail = production_method_availability("none", node_types=set(), model_status={})
        self.assertEqual(avail["state"], METHOD_AVAILABLE)
        self.assertEqual(avail["missing_nodes"], [])
        self.assertEqual(avail["missing_models"], [])

        avail_none_types = production_method_availability("none", node_types=None)
        self.assertEqual(avail_none_types["state"], METHOD_AVAILABLE)

    def test_rtx_vsr_availability(self):
        # Node types not determinable
        avail = production_method_availability("rtx_vsr_fast", node_types=None)
        self.assertEqual(avail["state"], METHOD_NOT_DETERMINABLE)

        # Missing RTXVideoSuperResolution
        avail = production_method_availability("rtx_vsr_fast", node_types={"VHS_LoadVideo", "VHS_VideoCombine"})
        self.assertEqual(avail["state"], METHOD_UNAVAILABLE)
        self.assertEqual(avail["missing_nodes"], ["RTXVideoSuperResolution"])

        # All nodes present
        avail = production_method_availability(
            "rtx_vsr_fast",
            node_types={"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"},
        )
        self.assertEqual(avail["state"], METHOD_AVAILABLE)
        self.assertEqual(avail["missing_nodes"], [])

    def test_seedvr2_availability(self):
        all_seed_nodes = {"VHS_LoadVideo", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel", "SeedVR2VideoUpscaler", "VHS_VideoCombine"}
        all_seed_models = {"seedvr2_ema_7b-Q4_K_M.gguf": "AVAILABLE", "ema_vae_fp16.safetensors": "AVAILABLE"}

        # Missing node
        avail = production_method_availability("seedvr2_quality", node_types={"VHS_LoadVideo"}, model_status=all_seed_models)
        self.assertEqual(avail["state"], METHOD_UNAVAILABLE)
        self.assertIn("SeedVR2VideoUpscaler", avail["missing_nodes"])

        # Missing model
        avail = production_method_availability(
            "seedvr2_quality",
            node_types=all_seed_nodes,
            model_status={"seedvr2_ema_7b-Q4_K_M.gguf": "AVAILABLE", "ema_vae_fp16.safetensors": "MISSING"},
        )
        self.assertEqual(avail["state"], METHOD_UNAVAILABLE)
        self.assertEqual(avail["missing_models"], ["ema_vae_fp16.safetensors"])

        # All present -> UNQUALIFIED (pending live RTX testing)
        avail = production_method_availability("seedvr2_quality", node_types=all_seed_nodes, model_status=all_seed_models)
        self.assertEqual(avail["state"], METHOD_UNQUALIFIED)

    def test_amd_hardware_not_globally_blocked(self):
        # On AMD machine where RTXVideoSuperResolution is missing, None is still available
        avail_none = production_method_availability("none", node_types={"LoadImage", "LoadAudio"})
        self.assertEqual(avail_none["state"], METHOD_AVAILABLE)


class TestPhase8EInputMaterialization(Phase8ETestBase):
    """Test deterministic Final Scene input staging for ComfyUI post-processing."""

    def test_materialize_final_scene_success(self):
        self._setup_final_scene(self.scene_1_id, content=b"FINAL_SCENE_CONTENT_FOR_STAGING")
        selector, staged_path = materialize_final_scene_for_postprocess(
            self.storage,
            self.project_id,
            self.scene_1_id,
            input_root=self.input_root,
        )
        self.assertEqual(
            selector,
            f"music_video_builder/{self.project_id}/{self.scene_1_id}/final_scene.mp4",
        )
        self.assertTrue(staged_path.is_file())
        self.assertEqual(staged_path.read_bytes(), b"FINAL_SCENE_CONTENT_FOR_STAGING")

    def test_materialize_final_scene_fails_when_scene_not_finalized(self):
        with self.assertRaises(ProductionNotReady):
            materialize_final_scene_for_postprocess(
                self.storage,
                self.project_id,
                self.scene_1_id,
                input_root=self.input_root,
            )
