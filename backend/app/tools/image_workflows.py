"""Pitch-chat-owned workflow builders and backend registry, independent of other UIs."""

import os
from pathlib import Path, PurePosixPath
import secrets
import tomllib
from typing import Literal

from pydantic import Field, model_validator

from backend.app.config import ROOT
from backend.app.harness.contracts import Contract
from backend.app.harness.image import ImageSpec


def model_file(value):
    if (not value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or "\\" in value or any(ord(char) < 32 for char in value)):
        raise ValueError("Model must be a relative ComfyUI catalog filename")
    return value


class ImageBackend(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    family: Literal["qwen-image", "sdxl", "flux2-klein"]
    model: str = Field(min_length=1, max_length=300)
    text_encoder: str | None = Field(default=None, max_length=300)
    vae: str | None = Field(default=None, max_length=300)
    steps: int = Field(default=20, ge=1, le=40)
    guidance: float = Field(default=4.0, ge=0, le=15, allow_inf_nan=False)
    resolution: int | None = Field(default=None, ge=512, le=1024, exclude_if=lambda v: v is None)

    @property
    def workflow(self):
        return {"qwen-image": "qwen_image_basic", "sdxl": "sdxl_basic", "flux2-klein": "flux2_klein_distilled"}[self.family]

    @model_validator(mode="after")
    def dependencies(self):
        for value in (self.model, self.text_encoder, self.vae):
            if value is not None:
                model_file(value)
        if self.family in {"qwen-image", "flux2-klein"} and (not self.text_encoder or not self.vae):
            raise ValueError("Qwen Image requires reviewed encoder and VAE")
        if self.family == "sdxl" and (self.text_encoder or self.vae):
            raise ValueError("SDXL workflow uses checkpoint-owned encoder/VAE")
        if self.family == "flux2-klein" and (self.steps != 4 or self.guidance != 1):
            raise ValueError("Reviewed Klein Distilled uses four steps and CFG one")
        if self.resolution is not None and self.resolution % 64:
            raise ValueError("Resolution must be a multiple of 64")
        return self

    def graph(self, spec: ImageSpec, count: int, prepared=None, reference=None):
        # Model-specific prompt and dimensions stay in this adapter boundary.
        sizes = {"1:1": (768, 768), "3:2": (960, 640), "2:3": (640, 960),
                 "16:9": (1024, 576), "9:16": (576, 1024)}
        width, height = sizes[spec.aspect_ratio]
        if self.resolution is not None:
            scale = self.resolution / 768
            width, height = round(width * scale / 16) * 16, round(height * scale / 16) * 16
        if self.family == "flux2-klein":
            from backend.app.tools.flux_klein import flux_klein_graph, flux_klein_edit_graph
            positive = prepared.positive if prepared else ", ".join((spec.subject, spec.style, spec.composition, spec.text_requirement))
            if reference:
                width, height = reference.width, reference.height
            builder = flux_klein_edit_graph if reference else flux_klein_graph
            return builder(**({"reference_name": reference.name} if reference else {}), model=self.model, text_encoder=self.text_encoder, vae=self.vae,
                positive=positive, seed=prepared.seed if prepared else secrets.randbits(63),
                width=width, height=height, count=count)
        if reference:
            raise ValueError("Reference editing requires the reviewed Klein backend")
        if self.family == "qwen-image":
            positive = (f"Create an image for {spec.purpose}. Main subject: {spec.subject}. "
                        f"Style: {spec.style}. Composition: {spec.composition}. "
                        f"Text requirement: {spec.text_requirement or 'no text'}. Quality: {spec.quality_preference}.")
            loaders = {
                "diffusion": {"class_type": "UNETLoader", "inputs": {"unet_name": self.model, "weight_dtype": "default"}},
                "encoder": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.text_encoder, "type": "qwen_image", "device": "default"}},
                "vae": {"class_type": "VAELoader", "inputs": {"vae_name": self.vae}},
                "sampling": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["diffusion", 0], "shift": 3.1}},
            }
            model, clip, vae, latent = ["sampling", 0], ["encoder", 0], ["vae", 0], "EmptySD3LatentImage"
        else:
            positive = ", ".join((spec.subject, spec.style, spec.composition, spec.purpose,
                                  f"text requirement: {spec.text_requirement or 'no text'}", spec.quality_preference))
            loaders = {"checkpoint": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.model}}}
            model, clip, vae, latent = ["checkpoint", 0], ["checkpoint", 1], ["checkpoint", 2], "EmptyLatentImage"
        return loaders | {
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prepared.positive if prepared else positive, "clip": clip}},
            "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": prepared.negative if prepared else ", ".join(spec.negative_constraints), "clip": clip}},
            "latent": {"class_type": latent, "inputs": {"width": width, "height": height, "batch_size": count}},
            "sample": {"class_type": "KSampler", "inputs": {"model": model, "positive": ["positive", 0], "negative": ["negative", 0],
                "latent_image": ["latent", 0], "seed": prepared.seed if prepared else secrets.randbits(63), "steps": self.steps, "cfg": self.guidance,
                "sampler_name": "euler", "scheduler": "simple" if self.family == "qwen-image" else "normal", "denoise": 1.0}},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": vae}},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "pitch_chat"}},
        }


class ImageWorkflowRegistry:
    def __init__(self, backends=()):
        self.backends = {entry.id: entry for entry in backends}
        if len(self.backends) != len(backends):
            raise ValueError("Duplicate image backend logical ID")

    def get(self, backend):
        return self.backends.get(backend)


def load_image_workflows(path: Path | None = None):
    path = path or Path(os.environ.get("PITCH_CHAT_IMAGES_CONFIG", ROOT / "config/images.toml"))
    with path.open("rb") as file:
        config = tomllib.load(file)
    if set(config) - {"backends"} or not isinstance(config.get("backends", {}), dict):
        raise ValueError("Invalid image registry configuration")
    entries = []
    for name, entry in config.get("backends", {}).items():
        if not isinstance(entry, dict) or "id" in entry:
            raise ValueError("Image backend ID must be the registry table name")
        entries.append(ImageBackend.model_validate({"id": name, **entry}))
    return ImageWorkflowRegistry(tuple(entries))
