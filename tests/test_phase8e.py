"""Phase 8E tests: production upscale method contracts, manifests, and validation."""

import json
from unittest.mock import patch

from backend.projects import ProjectValidationError
from backend.render_production import (
    POSTPROCESS_METHOD_LABELS,
    POSTPROCESS_METHOD_NONE,
    POSTPROCESS_METHOD_RTX_VSR_FAST,
    POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    PRODUCTION_METHODS,
    default_production_settings,
    production_method_registry,
    resolve_project_production_method,
    validate_production_method,
    validate_production_settings,
)
from backend.workflows import (
    PRODUCTION_POSTPROCESS_MANIFESTS,
    production_postprocess_manifest_registry,
    validate_postprocess_workflow_contract,
)
try:
    from phase8e_base import Phase8ETestBase
except ImportError:
    from tests.phase8e_base import Phase8ETestBase


class TestPhase8EMethodContracts(Phase8ETestBase):
    """Test the authoritative production post-processing options and validation."""

    def test_authoritative_options_exact(self):
        self.assertEqual(
            set(PRODUCTION_METHODS),
            {"none", "rtx_vsr_fast", "seedvr2_quality"},
        )
        self.assertEqual(
            POSTPROCESS_METHOD_LABELS,
            {
                "none": "None",
                "rtx_vsr_fast": "RTX VSR — Fast",
                "seedvr2_quality": "SeedVR2 — Quality",
            },
        )

    def test_production_method_registry(self):
        registry = production_method_registry()
        self.assertEqual(set(registry), {"none", "rtx_vsr_fast", "seedvr2_quality"})
        self.assertFalse(registry["none"]["comfyui_backed"])
        self.assertTrue(registry["rtx_vsr_fast"]["comfyui_backed"])
        self.assertTrue(registry["seedvr2_quality"]["comfyui_backed"])
        self.assertIsNone(registry["none"]["workflow_id"])
        self.assertIsNotNone(registry["rtx_vsr_fast"]["workflow_id"])
        self.assertIsNotNone(registry["seedvr2_quality"]["workflow_id"])

    def test_validate_production_method_success(self):
        for method in PRODUCTION_METHODS:
            self.assertEqual(validate_production_method(method), method)

    def test_validate_production_method_rejects_unknown(self):
        for forbidden in (
            "real_esrgan",
            "esrgan",
            "topaz",
            "rife",
            "film",
            "ltx_refine",
            "generic_ffmpeg",
            "upscale_2x",
            "4k",
            123,
            None,
            "",
        ):
            with self.assertRaises(ProjectValidationError):
                validate_production_method(forbidden)

    def test_default_production_settings(self):
        settings = default_production_settings()
        self.assertEqual(settings, {"upscale_method": "none"})

    def test_validate_production_settings(self):
        valid = validate_production_settings({"upscale_method": "rtx_vsr_fast"})
        self.assertEqual(valid, {"upscale_method": "rtx_vsr_fast"})

        with self.assertRaises(ProjectValidationError):
            validate_production_settings({"upscale_method": "invalid_method"})
        with self.assertRaises(ProjectValidationError):
            validate_production_settings({"upscale_method": "none", "extra": True})
        with self.assertRaises(ProjectValidationError):
            validate_production_settings("not a dict")

    def test_resolve_project_production_method_migration_safe(self):
        # Empty project document -> defaults to none
        self.assertEqual(resolve_project_production_method({}), "none")
        # None production section -> defaults to none
        self.assertEqual(resolve_project_production_method({"production": None}), "none")
        # Missing upscale_method -> defaults to none
        self.assertEqual(resolve_project_production_method({"production": {}}), "none")
        # Unknown method -> defaults to none
        self.assertEqual(resolve_project_production_method({"production": {"upscale_method": "unknown"}}), "none")
        # Valid methods
        self.assertEqual(resolve_project_production_method({"production": {"upscale_method": "rtx_vsr_fast"}}), "rtx_vsr_fast")
        self.assertEqual(resolve_project_production_method({"production": {"upscale_method": "seedvr2_quality"}}), "seedvr2_quality")
        self.assertEqual(resolve_project_production_method({"production": {"upscale_method": "none"}}), "none")


class TestPhase8EWorkflowManifests(Phase8ETestBase):
    """Test the frozen workflow manifests and post-processing templates."""

    def test_manifest_registry_exact(self):
        registry = production_postprocess_manifest_registry()
        self.assertEqual(set(registry), {"rtx_vsr_fast", "seedvr2_quality"})

    def test_validate_postprocess_workflow_contract_passes(self):
        validated = validate_postprocess_workflow_contract()
        self.assertEqual(set(validated), {"rtx_vsr_fast", "seedvr2_quality"})

        rtx = validated["rtx_vsr_fast"]
        self.assertEqual(rtx["node_count"], 3)
        self.assertEqual(
            set(rtx["required_node_types"]),
            {"VHS_LoadVideo", "RTXVideoSuperResolution", "VHS_VideoCombine"},
        )
        self.assertEqual(rtx["required_models"], [])
        self.assertEqual(rtx["output_node_id"], "3")

        seed = validated["seedvr2_quality"]
        self.assertEqual(seed["node_count"], 5)
        self.assertEqual(
            set(seed["required_node_types"]),
            {"VHS_LoadVideo", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel", "SeedVR2VideoUpscaler", "VHS_VideoCombine"},
        )
        self.assertEqual(len(seed["required_models"]), 2)
        self.assertEqual(seed["output_node_id"], "5")

    def test_frozen_settings_rtx_vsr(self):
        manifest = PRODUCTION_POSTPROCESS_MANIFESTS["rtx_vsr_fast"]
        frozen = manifest["frozen_settings"]
        self.assertEqual(frozen["resize_type"], "scale by multiplier")
        self.assertEqual(frozen["scale"], 2.0)
        self.assertEqual(frozen["quality"], "ULTRA")
        self.assertEqual(frozen["frame_rate"], 24)
        self.assertEqual(frozen["format"], "video/nvenc_h264-mp4")
        self.assertEqual(frozen["pix_fmt"], "yuv420p")
        self.assertEqual(frozen["bitrate"], 15)
        self.assertTrue(frozen["megabit"])
        self.assertFalse(frozen["save_metadata"])
        self.assertTrue(frozen["save_output"])

    def test_frozen_settings_seedvr2(self):
        manifest = PRODUCTION_POSTPROCESS_MANIFESTS["seedvr2_quality"]
        frozen = manifest["frozen_settings"]
        self.assertEqual(frozen["dit_blocks_to_swap"], 36)
        self.assertTrue(frozen["dit_swap_io_components"])
        self.assertEqual(frozen["dit_offload_device"], "cpu")
        self.assertFalse(frozen["dit_cache_model"])
        self.assertEqual(frozen["dit_attention_mode"], "sageattn_2")
        self.assertTrue(frozen["vae_encode_tiled"])
        self.assertEqual(frozen["vae_encode_tile_size"], 512)
        self.assertEqual(frozen["vae_encode_tile_overlap"], 64)
        self.assertTrue(frozen["vae_decode_tiled"])
        self.assertEqual(frozen["vae_decode_tile_size"], 512)
        self.assertEqual(frozen["vae_decode_tile_overlap"], 64)
        self.assertEqual(frozen["vae_offload_device"], "cpu")
        self.assertFalse(frozen["vae_cache_model"])
        self.assertEqual(frozen["batch_size"], 53)
        self.assertFalse(frozen["uniform_batch_size"])
        self.assertEqual(frozen["color_correction"], "lab")
        self.assertEqual(frozen["temporal_overlap"], 4)
        self.assertEqual(frozen["upscaler_offload_device"], "cpu")
        self.assertFalse(frozen["enable_debug"])
        self.assertEqual(frozen["frame_rate"], 24)
        self.assertEqual(frozen["format"], "video/nvenc_h264-mp4")

    def test_manifest_validation_rejects_forbidden_nodes(self):
        from backend.workflows import _validate_postprocess_workflow

        manifest = production_postprocess_manifest_registry()["rtx_vsr_fast"]
        bad_workflow = {
            "1": {"class_type": "VHS_LoadVideo", "inputs": {"video": "__PRODUCTION_FINAL_SCENE__"}},
            "2": {"class_type": "RTXVideoSuperResolution", "inputs": {"images": ["1", 0], "resize_type": "scale by multiplier", "scale": 2.0, "quality": "ULTRA"}},
            "3": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["2", 0], "frame_rate": 24, "format": "video/nvenc_h264-mp4", "pix_fmt": "yuv420p", "bitrate": 15, "megabit": True, "save_metadata": False, "save_output": True, "filename_prefix": "__PRODUCTION_OUTPUT_PREFIX__"}},
            "4": {"class_type": "LTXRefine", "inputs": {}},
        }
        with self.assertRaises(ProjectValidationError):
            _validate_postprocess_workflow(manifest, bad_workflow)
