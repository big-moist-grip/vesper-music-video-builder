"""Narrow provisional production contracts for the two supported H3 methods."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .projects import ProjectValidationError
from .visuals import MAX_REF2VA_STILL_REFERENCES


WORKFLOW_DIRECTORY = Path(__file__).resolve().parent.parent / "workflows"
WORKFLOW_MANIFESTS = {
    "keyframe_i2v": {
        "generation_method": "keyframe_i2v",
        "workflow_id": "h3_music_video_i2v_v1",
        "workflow_file": "h3_music_video_i2v_api.json",
        "required_node_types": [
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "LoadImage",
            "LoadAudio",
            "MiniMaxH3ImageToVideo",
            "LTXVSeparateAVLatent",
            "VAEEncodeAudio",
            "SolidMask",
            "SetLatentNoiseMask",
            "LTXVConcatAVLatent",
            "BasicGuider",
            "KSamplerSelect",
            "BasicScheduler",
            "RandomNoise",
            "SamplerCustomAdvanced",
            "VAEDecode",
            "VAEDecodeAudio",
            "VHS_VideoCombine",
        ],
        "required_models": [
            {
                "role": "diffusion_model",
                "category": "diffusion_models",
                "filename": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                "node_id": "1",
                "input": "unet_name",
                "node_type": "UNETLoader",
            },
            {
                "role": "text_encoder",
                "category": "text_encoders",
                "filename": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "node_id": "2",
                "input": "clip_name",
                "node_type": "CLIPLoader",
            },
            {
                "role": "video_vae",
                "category": "vae",
                "filename": "minimax_h3_video_vae_fp16.safetensors",
                "node_id": "3",
                "input": "vae_name",
                "node_type": "VAELoader",
            },
            {
                "role": "audio_vae",
                "category": "vae",
                "filename": "minimax_h3_audio_vae_fp32.safetensors",
                "node_id": "4",
                "input": "vae_name",
                "node_type": "VAELoader",
            },
        ],
        "inputs": {
            "keyframe": {"node_id": "5", "input": "image", "placeholder": "__PHASE8_KEYFRAME_IMAGE__"},
            "audio": {
                "source": {"node_id": "6", "input": "audio", "placeholder": "__PHASE8_SCENE_AUDIO__"},
                "latent_encode": {"node_id": "9", "input": "audio"},
            },
            "prompt": {"node_id": "7", "input": "prompt", "placeholder": "__PHASE8_COMPLETED_PROMPT__"},
            "seed": {"node_id": "16", "input": "noise_seed"},
            "width": {"node_id": "7", "input": "width"},
            "height": {"node_id": "7", "input": "height"},
            "frame_count": {"node_id": "7", "input": "length"},
            "steps": {"node_id": "15", "input": "steps"},
            "sampler_name": {"node_id": "14", "input": "sampler_name"},
            "scheduler": {"node_id": "15", "input": "scheduler"},
            "denoise": {"node_id": "15", "input": "denoise"},
            "fps": {"node_id": "20", "input": "frame_rate"},
            "filename_prefix": {"node_id": "20", "input": "filename_prefix"},
            "output": {"node_id": "20", "input": "images"},
        },
        "provisional_settings": {
            "width": 960,
            "height": 544,
            "frame_count": 73,
            "fps": 24,
            "steps": 20,
            "sampler": "res_multistep",
            "scheduler": "simple",
            "denoise": 1,
            "seed": 446059383552270,
        },
        "structurally_frozen": [
            "workflow file and generation method",
            "node types and IDs",
            "keyframe, audio, prompt, sampler, decode, and output patch points",
            "source-audio latent replacement path",
            "24 fps output frame rate",
        ],
        "provisional_target_nvidia": [
            "quality-first INT8 model candidates",
            "resolution/frame profile",
            "step count and acceleration selection",
            "Draft/Final settings",
            "upscale selection",
        ],
    },
    "reference2video": {
        "generation_method": "reference2video",
        "workflow_id": "h3_music_video_ref2va_v1",
        "workflow_file": "h3_music_video_ref2va_api.json",
        "required_node_types": [
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "LoadImage",
            "LoadAudio",
            "MiniMaxH3ReferenceToVideo",
            "LTXVSeparateAVLatent",
            "VAEEncodeAudio",
            "SolidMask",
            "SetLatentNoiseMask",
            "LTXVConcatAVLatent",
            "BasicGuider",
            "KSamplerSelect",
            "BasicScheduler",
            "RandomNoise",
            "SamplerCustomAdvanced",
            "VAEDecode",
            "VAEDecodeAudio",
            "VHS_VideoCombine",
        ],
        "required_models": [
            {
                "role": "diffusion_model",
                "category": "diffusion_models",
                "filename": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                "node_id": "1",
                "input": "unet_name",
                "node_type": "UNETLoader",
            },
            {
                "role": "text_encoder",
                "category": "text_encoders",
                "filename": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "node_id": "2",
                "input": "clip_name",
                "node_type": "CLIPLoader",
            },
            {
                "role": "video_vae",
                "category": "vae",
                "filename": "minimax_h3_video_vae_fp16.safetensors",
                "node_id": "3",
                "input": "vae_name",
                "node_type": "VAELoader",
            },
            {
                "role": "audio_vae",
                "category": "vae",
                "filename": "minimax_h3_audio_vae_fp32.safetensors",
                "node_id": "4",
                "input": "vae_name",
                "node_type": "VAELoader",
            },
        ],
        "inputs": {
            "pictures": {
                "max_count": MAX_REF2VA_STILL_REFERENCES,
                "slots": [
                    {
                        "picture_number": number,
                        "loader_node_id": str(50 + number - 1),
                        "loader_input": "image",
                        "destination_node_id": "8",
                        "destination_input": f"ref_images.ref_image_{number - 1}",
                    }
                    for number in range(1, MAX_REF2VA_STILL_REFERENCES + 1)
                ],
                "active_slots_only": True,
            },
            "audio": {
                "source": {"node_id": "7", "input": "audio", "placeholder": "__PHASE8_SCENE_AUDIO__"},
                "conditioning": {"node_id": "8", "input": "ref_audios.ref_audio_0"},
                "latent_encode": {"node_id": "10", "input": "audio"},
            },
            "prompt": {"node_id": "8", "input": "prompt", "placeholder": "__PHASE8_COMPLETED_PROMPT__"},
            "seed": {"node_id": "17", "input": "noise_seed"},
            "width": {"node_id": "8", "input": "width"},
            "height": {"node_id": "8", "input": "height"},
            "frame_count": {"node_id": "8", "input": "length"},
            "steps": {"node_id": "16", "input": "steps"},
            "sampler_name": {"node_id": "15", "input": "sampler_name"},
            "scheduler": {"node_id": "16", "input": "scheduler"},
            "denoise": {"node_id": "16", "input": "denoise"},
            "fps": {"node_id": "21", "input": "frame_rate"},
            "filename_prefix": {"node_id": "21", "input": "filename_prefix"},
            "output": {"node_id": "21", "input": "images"},
            "ref_image_size": {"node_id": "8", "input": "ref_image_size"},
        },
        "provisional_settings": {
            "width": 960,
            "height": 544,
            "frame_count": 73,
            "fps": 24,
            "steps": 20,
            "sampler": "res_multistep",
            "scheduler": "simple",
            "denoise": 1,
            "seed": 446059383552270,
            "ref_image_size": "max",
        },
        "structurally_frozen": [
            "workflow file and generation method",
            "node types and IDs",
            "ordered still Picture slot mappings through the installed capacity",
            "source audio conditioning and latent replacement paths",
            "prompt, sampler, decode, and output patch points",
            "24 fps output frame rate",
        ],
        "provisional_target_nvidia": [
            "quality-first INT8 model candidates",
            "resolution/frame profile",
            "step count and acceleration selection",
            "Draft/Final settings",
            "upscale selection",
        ],
    },
}

FORBIDDEN_NODE_TYPES = frozenset(
    {
        "MiniMaxH3TurboLoRA",
        "MiniMaxH3TurboSampler",
        "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "MiniMaxH3TextToVideo",
        "MiniMaxH3Text2Video",
        "MinimaxTextToVideoNode",
        "MinimaxHailuo03TextToVideoNode",
        "SolAttnPatch",
        "SolAttentionPatch",
        "PathchSageAttentionKJ",
        "SpectrumApplyMiniMaxH3",
        "RTXVideoSuperResolution",
        "SeedVR2VideoUpscaler",
        "SeedVR2Conditioning",
        "SeedVR2PostProcessing",
        "SeedVR2Preprocess",
        "SeedVR2TemporalChunk",
        "SeedVR2TemporalMerge",
        "RIFE",
        "LTXRefine",
        "MiniMaxH3Easy",
        "MiniMaxH3EasyLoader",
        "MiniMaxH3EasyModelAdapter",
        "MiniMaxH3EasyOutput",
        "MiniMaxH3Director",
        "MiniMaxH3DirectorGuide",
        "MiniMaxH3TokenCounter",
        "LoraLoader",
        "LoraLoader|pysssss",
        "LoraLoaderModelOnly",
        "LoraLoaderBypass",
        "LoraLoaderBypassModelOnly",
        "LoraModelLoader",
        "Lora Loader Stack (rgthree)",
        "Power Lora Loader (rgthree)",
        "CR Load LoRA",
        "CR Load Scheduled LoRAs",
    }
)
FORBIDDEN_NODE_MARKERS = (
    "lora",
    "rgthree",
    "easyuse",
    "texttovideo",
    "text2video",
    "sage",
    "solattn",
    "spectrum",
    "rtxvideosuperresolution",
    "seedvr2",
    "rife",
)
FORBIDDEN_TEXT = (
    "ref_video",
    "ref_video",
    "motion_transfer",
    "motion-transfer",
    "ollama",
    "easycache",
    "spectrum",
    "sage attention",
    "sol attention",
    "rife",
    "ltx refine",
    "text-to-video",
    "text to video",
    "t2v",
    "lora",
    "rgthree",
    "easyuse",
)
_DYNAMIC_REF_IMAGE = re.compile(r"^ref_images\.ref_image_(\d+)$")


def production_manifest_registry() -> dict[str, dict[str, object]]:
    return deepcopy(WORKFLOW_MANIFESTS)


def _load_workflow(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    path = WORKFLOW_DIRECTORY / str(manifest["workflow_file"])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectValidationError(f"Production workflow could not be loaded: {path.name}.") from error
    if not isinstance(document, dict) or not document:
        raise ProjectValidationError("Production workflow must be a non-empty API node object.")
    for node_id, node in document.items():
        if not isinstance(node_id, str) or not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            raise ProjectValidationError("Production workflow contains an invalid API node.")
    return document


def _node(workflow: dict[str, dict[str, object]], node_id: object, label: str) -> dict[str, object]:
    node = workflow.get(str(node_id))
    if node is None:
        raise ProjectValidationError(f"{label} references missing node {node_id}.")
    return node


def _input_exists(node: dict[str, object], input_name: str) -> bool:
    if input_name in node["inputs"]:
        return True
    match = _DYNAMIC_REF_IMAGE.match(input_name)
    return bool(match and node["class_type"] == "MiniMaxH3ReferenceToVideo" and int(match.group(1)) < MAX_REF2VA_STILL_REFERENCES)


def _check_mapping(workflow: dict[str, dict[str, object]], mapping: dict[str, object], label: str, expected_type: str | None = None) -> dict[str, object]:
    if not isinstance(mapping, dict) or not isinstance(mapping.get("node_id"), str) or not isinstance(mapping.get("input"), str):
        raise ProjectValidationError(f"{label} mapping is malformed.")
    node = _node(workflow, mapping["node_id"], label)
    if expected_type is not None and node["class_type"] != expected_type:
        raise ProjectValidationError(f"{label} mapping targets {node['class_type']}, expected {expected_type}.")
    if not _input_exists(node, mapping["input"]):
        raise ProjectValidationError(f"{label} mapping input {mapping['input']} is not present.")
    return node


def _check_link(workflow: dict[str, dict[str, object]], node_id: str, input_name: str, source_node_id: str, source_output: int = 0) -> None:
    node = _node(workflow, node_id, "Connection")
    link = node["inputs"].get(input_name)
    if link != [source_node_id, source_output]:
        raise ProjectValidationError(f"Connection {node_id}.{input_name} must point to {source_node_id}:{source_output}.")


def _check_node_type(workflow: dict[str, dict[str, object]], node_id: str, expected_type: str, label: str) -> None:
    node = _node(workflow, node_id, label)
    if node["class_type"] != expected_type:
        raise ProjectValidationError(f"{label} must be {expected_type}, found {node['class_type']}.")


def _check_models(workflow: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    expected_loader_types = {
        "diffusion_model": "UNETLoader",
        "text_encoder": "CLIPLoader",
        "video_vae": "VAELoader",
        "audio_vae": "VAELoader",
    }
    for declaration in manifest["required_models"]:
        role = declaration.get("role")
        node_type = declaration.get("node_type")
        if role not in expected_loader_types or node_type != expected_loader_types[role]:
            raise ProjectValidationError(f"Model {role} has an invalid loader declaration.")
        node = _check_mapping(workflow, declaration, f"Model {role}", node_type)
        if node["inputs"].get(declaration["input"]) != declaration["filename"]:
            raise ProjectValidationError(f"Model {role} does not match its declared filename.")


def _check_provisional_settings(workflow: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    generation_node_type = (
        "MiniMaxH3ImageToVideo"
        if manifest["generation_method"] == "keyframe_i2v"
        else "MiniMaxH3ReferenceToVideo"
    )
    setting_mappings = {
        "width": ("width", generation_node_type),
        "height": ("height", generation_node_type),
        "frame_count": ("frame_count", generation_node_type),
        "steps": ("steps", "BasicScheduler"),
        "sampler": ("sampler_name", "KSamplerSelect"),
        "scheduler": ("scheduler", "BasicScheduler"),
        "denoise": ("denoise", "BasicScheduler"),
        "fps": ("fps", "VHS_VideoCombine"),
        "seed": ("seed", "RandomNoise"),
    }
    provisional = manifest["provisional_settings"]
    inputs = manifest["inputs"]
    for setting_name, (mapping_name, expected_type) in setting_mappings.items():
        if setting_name not in provisional or mapping_name not in inputs:
            raise ProjectValidationError(f"Manifest is missing provisional setting mapping: {setting_name}.")
        node = _check_mapping(workflow, inputs[mapping_name], setting_name, expected_type)
        input_name = inputs[mapping_name]["input"]
        if node["inputs"].get(input_name) != provisional[setting_name]:
            raise ProjectValidationError(f"Template setting {setting_name} does not match its provisional manifest value.")
    if provisional["fps"] != 24:
        raise ProjectValidationError("Production workflow fps must remain structurally fixed at 24.")


def _check_forbidden(workflow: dict[str, dict[str, object]]) -> None:
    serialized = json.dumps(workflow, ensure_ascii=False).lower()
    for node in workflow.values():
        class_type = node["class_type"]
        class_type_lower = class_type.casefold()
        if class_type in FORBIDDEN_NODE_TYPES or any(marker in class_type_lower for marker in FORBIDDEN_NODE_MARKERS):
            raise ProjectValidationError(f"Forbidden production node type is present: {node['class_type']}.")
    for forbidden in FORBIDDEN_TEXT:
        if forbidden in serialized:
            raise ProjectValidationError(f"Forbidden production branch text is present: {forbidden}.")


def _check_common(workflow: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    types = {node["class_type"] for node in workflow.values()}
    missing_types = [node_type for node_type in manifest["required_node_types"] if node_type not in types]
    if missing_types:
        raise ProjectValidationError(f"Production workflow is missing node types: {', '.join(missing_types)}.")
    _check_models(workflow, manifest)
    inputs = manifest["inputs"]
    _check_mapping(workflow, inputs["prompt"], "Prompt", "MiniMaxH3ImageToVideo" if manifest["generation_method"] == "keyframe_i2v" else "MiniMaxH3ReferenceToVideo")
    _check_mapping(workflow, inputs["seed"], "Seed", "RandomNoise")
    _check_mapping(workflow, inputs["width"], "Width")
    _check_mapping(workflow, inputs["height"], "Height")
    _check_mapping(workflow, inputs["frame_count"], "Frame count")
    _check_mapping(workflow, inputs["steps"], "Steps", "BasicScheduler")
    _check_mapping(workflow, inputs["sampler_name"], "Sampler name", "KSamplerSelect")
    _check_mapping(workflow, inputs["scheduler"], "Scheduler", "BasicScheduler")
    _check_mapping(workflow, inputs["denoise"], "Denoise", "BasicScheduler")
    _check_mapping(workflow, inputs["fps"], "Frame rate", "VHS_VideoCombine")
    _check_mapping(workflow, inputs["filename_prefix"], "Filename prefix", "VHS_VideoCombine")
    _check_mapping(workflow, inputs["output"], "Output images", "VHS_VideoCombine")
    _check_provisional_settings(workflow, manifest)
    _check_forbidden(workflow)


def _check_i2v(workflow: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    _check_node_types(
        workflow,
        {
            "1": "UNETLoader",
            "2": "CLIPLoader",
            "3": "VAELoader",
            "4": "VAELoader",
            "5": "LoadImage",
            "6": "LoadAudio",
            "7": "MiniMaxH3ImageToVideo",
            "8": "LTXVSeparateAVLatent",
            "9": "VAEEncodeAudio",
            "10": "SolidMask",
            "11": "SetLatentNoiseMask",
            "12": "LTXVConcatAVLatent",
            "13": "BasicGuider",
            "14": "KSamplerSelect",
            "15": "BasicScheduler",
            "16": "RandomNoise",
            "17": "SamplerCustomAdvanced",
            "18": "VAEDecode",
            "19": "VAEDecodeAudio",
            "20": "VHS_VideoCombine",
        },
    )
    inputs = manifest["inputs"]
    _check_mapping(workflow, inputs["keyframe"], "Keyframe", "LoadImage")
    audio = inputs["audio"]
    _check_mapping(workflow, audio["source"], "Source audio", "LoadAudio")
    _check_mapping(workflow, audio["latent_encode"], "Audio latent encode", "VAEEncodeAudio")
    _check_link(workflow, audio["latent_encode"]["node_id"], "audio", audio["source"]["node_id"])
    _check_link(workflow, "7", "clip", "2")
    _check_link(workflow, "7", "vae", "3")
    _check_link(workflow, "7", "first_frame", inputs["keyframe"]["node_id"])
    _check_link(workflow, "8", "av_latent", "7", 1)
    _check_link(workflow, "9", "vae", "4")
    _check_link(workflow, "11", "samples", "9")
    _check_link(workflow, "11", "mask", "10")
    _check_link(workflow, "12", "video_latent", "8")
    _check_link(workflow, "12", "audio_latent", "11")
    _check_link(workflow, "13", "model", "1")
    _check_link(workflow, "13", "conditioning", "7")
    _check_link(workflow, "15", "model", "1")
    _check_link(workflow, "17", "noise", "16")
    _check_link(workflow, "17", "guider", "13")
    _check_link(workflow, "17", "sampler", "14")
    _check_link(workflow, "17", "sigmas", "15")
    _check_link(workflow, "17", "latent_image", "12")
    _check_link(workflow, "18", "samples", "17")
    _check_link(workflow, "18", "vae", "3")
    _check_link(workflow, "19", "samples", "17")
    _check_link(workflow, "19", "vae", "4")
    _check_link(workflow, "20", "images", "18")
    _check_link(workflow, "20", "audio", "19")


def _check_ref2v(workflow: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    _check_node_types(
        workflow,
        {
            "1": "UNETLoader",
            "2": "CLIPLoader",
            "3": "VAELoader",
            "4": "VAELoader",
            "7": "LoadAudio",
            "8": "MiniMaxH3ReferenceToVideo",
            "9": "LTXVSeparateAVLatent",
            "10": "VAEEncodeAudio",
            "11": "SolidMask",
            "12": "SetLatentNoiseMask",
            "13": "LTXVConcatAVLatent",
            "14": "BasicGuider",
            "15": "KSamplerSelect",
            "16": "BasicScheduler",
            "17": "RandomNoise",
            "18": "SamplerCustomAdvanced",
            "19": "VAEDecode",
            "20": "VAEDecodeAudio",
            "21": "VHS_VideoCombine",
        },
    )
    inputs = manifest["inputs"]
    pictures = inputs["pictures"]
    slots = pictures["slots"]
    if pictures["max_count"] != MAX_REF2VA_STILL_REFERENCES or len(slots) != MAX_REF2VA_STILL_REFERENCES:
        raise ProjectValidationError("REF2VA picture mapping capacity does not match the installed contract.")
    seen_destinations: set[str] = set()
    for expected_number, slot in enumerate(slots, start=1):
        if slot["picture_number"] != expected_number:
            raise ProjectValidationError("REF2VA picture slots must be ordered from Picture 1.")
        if slot["destination_input"] in seen_destinations:
            raise ProjectValidationError("REF2VA picture destinations must be unique.")
        seen_destinations.add(slot["destination_input"])
        _check_mapping(workflow, {"node_id": slot["loader_node_id"], "input": slot["loader_input"]}, f"Picture {expected_number}", "LoadImage")
        _check_mapping(workflow, {"node_id": slot["destination_node_id"], "input": slot["destination_input"]}, f"Picture {expected_number} destination", "MiniMaxH3ReferenceToVideo")
        _check_link(workflow, slot["destination_node_id"], slot["destination_input"], slot["loader_node_id"])
    audio = inputs["audio"]
    _check_mapping(workflow, audio["source"], "Source audio", "LoadAudio")
    _check_mapping(workflow, audio["conditioning"], "REF2VA audio conditioning", "MiniMaxH3ReferenceToVideo")
    _check_mapping(workflow, audio["latent_encode"], "Audio latent encode", "VAEEncodeAudio")
    _check_link(workflow, audio["latent_encode"]["node_id"], "audio", audio["source"]["node_id"])
    _check_link(workflow, "8", "clip", "2")
    _check_link(workflow, "8", "vae", "3")
    _check_link(workflow, "8", "audio_vae", "4")
    _check_link(workflow, "8", "ref_audios.ref_audio_0", audio["source"]["node_id"])
    _check_link(workflow, "9", "av_latent", "8", 1)
    _check_link(workflow, "12", "samples", "10")
    _check_link(workflow, "12", "mask", "11")
    _check_link(workflow, "13", "video_latent", "9")
    _check_link(workflow, "13", "audio_latent", "12")
    _check_link(workflow, "14", "model", "1")
    _check_link(workflow, "14", "conditioning", "8")
    _check_link(workflow, "16", "model", "1")
    _check_link(workflow, "18", "noise", "17")
    _check_link(workflow, "18", "guider", "14")
    _check_link(workflow, "18", "sampler", "15")
    _check_link(workflow, "18", "sigmas", "16")
    _check_link(workflow, "18", "latent_image", "13")
    _check_link(workflow, "19", "samples", "18")
    _check_link(workflow, "19", "vae", "3")
    _check_link(workflow, "20", "samples", "18")
    _check_link(workflow, "20", "vae", "4")
    _check_link(workflow, "21", "images", "19")
    _check_link(workflow, "21", "audio", "20")
    ref_image_size = inputs["ref_image_size"]
    ref_image_size_node = _check_mapping(workflow, ref_image_size, "REF2VA reference image size", "MiniMaxH3ReferenceToVideo")
    if ref_image_size_node["inputs"].get(ref_image_size["input"]) != manifest["provisional_settings"]["ref_image_size"]:
        raise ProjectValidationError("REF2VA provisional ref_image_size must be max.")


def _check_node_types(workflow: dict[str, dict[str, object]], expected: dict[str, str]) -> None:
    for node_id, expected_type in expected.items():
        _check_node_type(workflow, node_id, expected_type, f"Node {node_id}")


def _validate_loaded_workflow(manifest: dict[str, object], workflow: dict[str, dict[str, object]]) -> None:
    _check_common(workflow, manifest)
    if manifest["generation_method"] == "keyframe_i2v":
        _check_i2v(workflow, manifest)
    elif manifest["generation_method"] == "reference2video":
        _check_ref2v(workflow, manifest)
    else:
        raise ProjectValidationError("Unsupported production generation method.")


def validate_production_workflow_contract() -> dict[str, dict[str, object]]:
    if set(WORKFLOW_MANIFESTS) != {"keyframe_i2v", "reference2video"}:
        raise ProjectValidationError("Production manifest registry must contain exactly the two supported methods.")
    validated: dict[str, dict[str, object]] = {}
    for generation_method, manifest in WORKFLOW_MANIFESTS.items():
        workflow = _load_workflow(manifest)
        _validate_loaded_workflow(manifest, workflow)
        validated[generation_method] = {
            "workflow_id": manifest["workflow_id"],
            "workflow_file": manifest["workflow_file"],
            "node_count": len(workflow),
            "required_node_types": list(manifest["required_node_types"]),
            "required_models": deepcopy(manifest["required_models"]),
            "ref2va_capacity": MAX_REF2VA_STILL_REFERENCES if generation_method == "reference2video" else None,
        }
    return validated


def patch_reference_picture_slots(workflow: dict[str, object], image_names: list[str]) -> dict[str, object]:
    """Patch active REF2VA still slots for deterministic Phase 8 use.

    This is a pure contract helper; it does not queue or execute a workflow.
    """
    if not isinstance(image_names, list) or not 1 <= len(image_names) <= MAX_REF2VA_STILL_REFERENCES:
        raise ProjectValidationError(f"REF2VA requires 1 to {MAX_REF2VA_STILL_REFERENCES} still references.")
    if any(not isinstance(name, str) or not name.strip() for name in image_names):
        raise ProjectValidationError("REF2VA picture filenames must be non-empty strings.")
    patched = deepcopy(workflow)
    nodes = patched.get("nodes") if isinstance(patched, dict) and isinstance(patched.get("nodes"), dict) else patched
    if not isinstance(nodes, dict):
        raise ProjectValidationError("Workflow patch target must contain API nodes.")
    h3_node = nodes.get("8")
    if not isinstance(h3_node, dict) or h3_node.get("class_type") != "MiniMaxH3ReferenceToVideo":
        raise ProjectValidationError("REF2VA workflow patch target is invalid.")
    for slot_number in range(1, MAX_REF2VA_STILL_REFERENCES + 1):
        loader_id = str(50 + slot_number - 1)
        loader = nodes.get(loader_id)
        if not isinstance(loader, dict) or loader.get("class_type") != "LoadImage":
            raise ProjectValidationError(f"REF2VA picture loader {loader_id} is missing.")
        input_name = f"ref_images.ref_image_{slot_number - 1}"
        h3_node["inputs"].pop(input_name, None)
        if slot_number <= len(image_names):
            loader["inputs"]["image"] = image_names[slot_number - 1]
            h3_node["inputs"][input_name] = [loader_id, 0]
        else:
            nodes.pop(loader_id, None)
    return patched


# ---------------------------------------------------------------------------
# Phase 8E — production post-processing (upscale) contracts
# ---------------------------------------------------------------------------
#
# Method identities are stable.  Labels are the authoritative user-facing
# production choices.  The graph evidence for each ComfyUI-backed method is
# extracted from the reviewed community donor workflows and minimized to the
# smallest stable production subgraph:
#
#   rtx_vsr_fast    <- minimaxH3EZTurboOptimalRTXUpscale_v35REMADE
#                      VHS_LoadVideo -> RTXVideoSuperResolution -> VHS_VideoCombine
#   seedvr2_quality <- minimaxH3WithSEEDVR2Upscaler_v4 (UPSCALE subgraph)
#                      VHS_LoadVideo -> SeedVR2LoadDiTModel/SeedVR2LoadVAEModel
#                      -> SeedVR2VideoUpscaler -> VHS_VideoCombine
#
# H3 generation, LTX refinement, RIFE/FILM interpolation, preview/debug
# branches, and donor audio paths are deliberately excluded.  Authoritative
# scene audio is remuxed by the Builder after picture post-processing and is
# never taken from the post-processing graph.

POSTPROCESS_METHOD_NONE = "none"
POSTPROCESS_METHOD_RTX_VSR_FAST = "rtx_vsr_fast"
POSTPROCESS_METHOD_SEEDVR2_QUALITY = "seedvr2_quality"

POSTPROCESS_METHOD_LABELS = {
    POSTPROCESS_METHOD_NONE: "None",
    POSTPROCESS_METHOD_RTX_VSR_FAST: "RTX VSR — Fast",
    POSTPROCESS_METHOD_SEEDVR2_QUALITY: "SeedVR2 — Quality",
}

PRODUCTION_POSTPROCESS_MANIFESTS = {
    POSTPROCESS_METHOD_RTX_VSR_FAST: {
        "method": POSTPROCESS_METHOD_RTX_VSR_FAST,
        "label": POSTPROCESS_METHOD_LABELS[POSTPROCESS_METHOD_RTX_VSR_FAST],
        "workflow_id": "production_upscale_rtx_vsr_fast_v1",
        "workflow_file": "upscale_rtx_vsr_fast_api.json",
        "donor_basis": ["minimaxH3EZTurboOptimalRTXUpscale_v35REMADE"],
        "required_node_types": [
            "VHS_LoadVideo",
            "RTXVideoSuperResolution",
            "VHS_VideoCombine",
        ],
        "required_models": [],
        "inputs": {
            "final_scene_video": {"node_id": "1", "input": "video", "placeholder": "__PRODUCTION_FINAL_SCENE__"},
            "scale_mode": {"node_id": "2", "input": "resize_type"},
            "scale": {"node_id": "2", "input": "scale"},
            "quality": {"node_id": "2", "input": "quality"},
            "fps": {"node_id": "3", "input": "frame_rate"},
            "filename_prefix": {"node_id": "3", "input": "filename_prefix", "placeholder": "__PRODUCTION_OUTPUT_PREFIX__"},
            "output": {"node_id": "3", "input": "images"},
        },
        "output_node_id": "3",
        "frozen_settings": {
            "resize_type": "scale by multiplier",
            "scale": 2.0,
            "quality": "ULTRA",
            "frame_rate": 24,
            "format": "video/nvenc_h264-mp4",
            "pix_fmt": "yuv420p",
            "bitrate": 15,
            "megabit": True,
            "save_metadata": False,
            "save_output": True,
        },
        "runtime_policy": {
            "execution": "RTX_REQUIRED",
            "note": "RTXVideoSuperResolution executes through NVIDIA nvvfx/CUDA; on runtimes without it the method is truthfully unavailable.",
        },
        "structurally_frozen": [
            "workflow file and method identity",
            "node types and IDs",
            "Final Scene video input patch point",
            "scale-by-multiplier 2.0 ULTRA upscaler settings",
            "24 fps nvenc H.264 output contract",
            "no audio input into the graph (authoritative audio remuxed by the Builder)",
        ],
    },
    POSTPROCESS_METHOD_SEEDVR2_QUALITY: {
        "method": POSTPROCESS_METHOD_SEEDVR2_QUALITY,
        "label": POSTPROCESS_METHOD_LABELS[POSTPROCESS_METHOD_SEEDVR2_QUALITY],
        "workflow_id": "production_upscale_seedvr2_quality_v1",
        "workflow_file": "upscale_seedvr2_quality_api.json",
        "donor_basis": ["minimaxH3WithSEEDVR2Upscaler_v4"],
        "required_node_types": [
            "VHS_LoadVideo",
            "SeedVR2LoadDiTModel",
            "SeedVR2LoadVAEModel",
            "SeedVR2VideoUpscaler",
            "VHS_VideoCombine",
        ],
        "required_models": [
            {
                "role": "seedvr2_dit",
                "category": "diffusion_models",
                "filename": "seedvr2_ema_7b-Q4_K_M.gguf",
                "node_id": "2",
                "input": "model",
                "node_type": "SeedVR2LoadDiTModel",
            },
            {
                "role": "seedvr2_vae",
                "category": "vae",
                "filename": "ema_vae_fp16.safetensors",
                "node_id": "3",
                "input": "model",
                "node_type": "SeedVR2LoadVAEModel",
            },
        ],
        "inputs": {
            "final_scene_video": {"node_id": "1", "input": "video", "placeholder": "__PRODUCTION_FINAL_SCENE__"},
            "resolution": {"node_id": "4", "input": "resolution"},
            "max_resolution": {"node_id": "4", "input": "max_resolution"},
            "seed": {"node_id": "4", "input": "seed"},
            "fps": {"node_id": "5", "input": "frame_rate"},
            "filename_prefix": {"node_id": "5", "input": "filename_prefix", "placeholder": "__PRODUCTION_OUTPUT_PREFIX__"},
            "output": {"node_id": "5", "input": "images"},
        },
        "output_node_id": "5",
        "frozen_settings": {
            "dit_device": "cuda:0",
            "dit_blocks_to_swap": 36,
            "dit_swap_io_components": True,
            "dit_offload_device": "cpu",
            "dit_cache_model": False,
            "dit_attention_mode": "sageattn_2",
            "vae_device": "cuda:0",
            "vae_encode_tiled": True,
            "vae_encode_tile_size": 512,
            "vae_encode_tile_overlap": 64,
            "vae_decode_tiled": True,
            "vae_decode_tile_size": 512,
            "vae_decode_tile_overlap": 64,
            "vae_offload_device": "cpu",
            "vae_cache_model": False,
            "seed": 0,
            "batch_size": 53,
            "uniform_batch_size": False,
            "color_correction": "lab",
            "temporal_overlap": 4,
            "prepend_frames": 0,
            "input_noise_scale": 0,
            "latent_noise_scale": 0,
            "upscaler_offload_device": "cpu",
            "enable_debug": False,
            "frame_rate": 24,
            "format": "video/nvenc_h264-mp4",
            "pix_fmt": "yuv420p",
            "bitrate": 15,
            "megabit": True,
            "save_metadata": False,
            "save_output": True,
        },
        "resource_policy": {
            "unload_before": "H3 models must be released/unloaded before SeedVR2 where the runtime requires it.",
            "release_after_scene": "cache_model is frozen false; per-scene temporary workspaces are cleaned by the Builder.",
            "qualification": "PENDING_RTX_LIVE on RTX 4080 SUPER 16 GB / 32 GB system RAM",
        },
        "runtime_policy": {
            "execution": "RTX_QUALIFICATION_PENDING",
            "note": "SeedVR2 remains structurally supported; live VRAM/quality qualification on the RTX production machine is required before production use.",
        },
        "structurally_frozen": [
            "workflow file and method identity",
            "node types and IDs",
            "Final Scene video input patch point",
            "donor DiT/VAE loader settings (Q4_K_M 7B GGUF, 36 block swaps, tiled VAE 512/64, CPU offload, sageattn_2)",
            "24 fps nvenc H.264 output contract",
            "no audio input into the graph (authoritative audio remuxed by the Builder)",
        ],
    },
}


def production_postprocess_manifest_registry() -> dict[str, dict[str, object]]:
    return deepcopy(PRODUCTION_POSTPROCESS_MANIFESTS)


def validate_postprocess_workflow_contract() -> dict[str, dict[str, object]]:
    """Validate the frozen production post-processing templates and manifests."""

    if set(PRODUCTION_POSTPROCESS_MANIFESTS) != {
        POSTPROCESS_METHOD_RTX_VSR_FAST,
        POSTPROCESS_METHOD_SEEDVR2_QUALITY,
    }:
        raise ProjectValidationError("Post-process manifest registry must contain exactly the two ComfyUI-backed upscale methods.")
    validated: dict[str, dict[str, object]] = {}
    for method, manifest in PRODUCTION_POSTPROCESS_MANIFESTS.items():
        workflow = _load_workflow(manifest)
        _validate_postprocess_workflow(manifest, workflow)
        validated[method] = {
            "workflow_id": manifest["workflow_id"],
            "workflow_file": manifest["workflow_file"],
            "node_count": len(workflow),
            "required_node_types": list(manifest["required_node_types"]),
            "required_models": deepcopy(manifest["required_models"]),
            "output_node_id": manifest["output_node_id"],
            "frozen_settings": deepcopy(manifest["frozen_settings"]),
        }
    return validated


def _validate_postprocess_workflow(manifest: dict[str, object], workflow: dict[str, dict[str, object]]) -> None:
    # Exact node inventory: no donor debug/preview/LTX/RIFE/generation branches.
    expected_types = {str(node_id): node_type for node_id, node_type in zip(
        [node_id for node_id in workflow],
        [node["class_type"] for node in workflow.values()],
    )}
    if sorted(expected_types.values()) != sorted(manifest["required_node_types"]):
        raise ProjectValidationError(f"{manifest['workflow_id']} node inventory does not match the frozen manifest.")

    for mapping in manifest["inputs"].values():
        _check_mapping(workflow, mapping, "Post-process input")

    # The video input comes from the materialized Final Scene only.
    video_mapping = manifest["inputs"]["final_scene_video"]
    video_node = _node(workflow, video_mapping["node_id"], "Final Scene input")
    if video_node["inputs"].get("video") != video_mapping["placeholder"]:
        raise ProjectValidationError("Final Scene video input must remain the owned placeholder.")

    # Output node contract.
    output_node = _node(workflow, manifest["output_node_id"], "Post-process output")
    if output_node["class_type"] != "VHS_VideoCombine":
        raise ProjectValidationError("Post-process output node must be VHS_VideoCombine.")
    frozen = manifest["frozen_settings"]
    output_inputs = output_node["inputs"]
    for input_name, expected in (
        ("frame_rate", frozen["frame_rate"]),
        ("format", frozen["format"]),
        ("pix_fmt", frozen["pix_fmt"]),
        ("bitrate", frozen["bitrate"]),
        ("megabit", frozen["megabit"]),
        ("save_metadata", frozen["save_metadata"]),
        ("save_output", frozen["save_output"]),
    ):
        if output_inputs.get(input_name) != expected:
            raise ProjectValidationError(f"Post-process output setting {input_name} is not frozen.")
    if output_inputs.get("filename_prefix") != manifest["inputs"]["filename_prefix"]["placeholder"]:
        raise ProjectValidationError("Post-process output prefix must remain the owned placeholder.")
    if "audio" in output_inputs:
        raise ProjectValidationError("Post-processing graphs must not carry audio; authoritative audio is remuxed by the Builder.")

    # Method-specific frozen cores.
    if manifest["method"] == POSTPROCESS_METHOD_RTX_VSR_FAST:
        upscaler = _node(workflow, "2", "RTX VSR upscaler")
        if upscaler["inputs"].get("images") != ["1", 0]:
            raise ProjectValidationError("RTX VSR must consume the Final Scene frame stream.")
        for input_name, expected in (
            ("resize_type", frozen["resize_type"]),
            ("scale", frozen["scale"]),
            ("quality", frozen["quality"]),
        ):
            if upscaler["inputs"].get(input_name) != expected:
                raise ProjectValidationError(f"RTX VSR setting {input_name} is not frozen.")
    elif manifest["method"] == POSTPROCESS_METHOD_SEEDVR2_QUALITY:
        upscaler = _node(workflow, "4", "SeedVR2 upscaler")
        if upscaler["inputs"].get("image") != ["1", 0]:
            raise ProjectValidationError("SeedVR2 must consume the Final Scene frame stream.")
        if upscaler["inputs"].get("dit") != ["2", 0] or upscaler["inputs"].get("vae") != ["3", 0]:
            raise ProjectValidationError("SeedVR2 must consume the frozen DiT/VAE loaders.")
        for input_name, expected in (
            ("seed", frozen["seed"]),
            ("batch_size", frozen["batch_size"]),
            ("uniform_batch_size", frozen["uniform_batch_size"]),
            ("color_correction", frozen["color_correction"]),
            ("temporal_overlap", frozen["temporal_overlap"]),
            ("offload_device", frozen["upscaler_offload_device"]),
            ("enable_debug", frozen["enable_debug"]),
        ):
            if upscaler["inputs"].get(input_name) != expected:
                raise ProjectValidationError(f"SeedVR2 setting {input_name} is not frozen.")
        dit = _node(workflow, "2", "SeedVR2 DiT loader")
        for input_name, expected in (
            ("model", manifest["required_models"][0]["filename"]),
            ("blocks_to_swap", frozen["dit_blocks_to_swap"]),
            ("swap_io_components", frozen["dit_swap_io_components"]),
            ("offload_device", frozen["dit_offload_device"]),
            ("cache_model", frozen["dit_cache_model"]),
            ("attention_mode", frozen["dit_attention_mode"]),
        ):
            if dit["inputs"].get(input_name) != expected:
                raise ProjectValidationError(f"SeedVR2 DiT loader setting {input_name} is not frozen.")
        vae = _node(workflow, "3", "SeedVR2 VAE loader")
        for input_name, expected in (
            ("model", manifest["required_models"][1]["filename"]),
            ("encode_tiled", frozen["vae_encode_tiled"]),
            ("encode_tile_size", frozen["vae_encode_tile_size"]),
            ("encode_tile_overlap", frozen["vae_encode_tile_overlap"]),
            ("decode_tiled", frozen["vae_decode_tiled"]),
            ("decode_tile_size", frozen["vae_decode_tile_size"]),
            ("decode_tile_overlap", frozen["vae_decode_tile_overlap"]),
            ("offload_device", frozen["vae_offload_device"]),
            ("cache_model", frozen["vae_cache_model"]),
        ):
            if vae["inputs"].get(input_name) != expected:
                raise ProjectValidationError(f"SeedVR2 VAE loader setting {input_name} is not frozen.")

    # Forbidden donor branches must never appear in production post-processing.
    forbidden_markers = ("ltx", "rife", "film", "interpol", "ollama", "lora", "rgthree", "preview", "markdown", "note")
    for node in workflow.values():
        class_type = str(node["class_type"]).lower()
        for marker in forbidden_markers:
            if marker in class_type:
                raise ProjectValidationError(f"Post-processing workflow contains forbidden node type {node['class_type']}.")
        for value in node["inputs"].values():
            if isinstance(value, str):
                lowered = value.lower()
                if "ltx" in lowered or "rife" in lowered or "ollama" in lowered:
                    raise ProjectValidationError("Post-processing workflow contains a forbidden donor branch reference.")
