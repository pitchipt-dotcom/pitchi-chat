"""Official Klein 4B Distilled graph, with no lifecycle or external dependencies.

Reviewed from Comfy-Org/workflow_templates at
 e7cd011d4ded3411c2f481200544f0be6fdc962e,
 image_flux2_klein_text_to_image.json, Distilled subgraph. Caller owns config,
 validation, persistence, resource admission, execution and result verification.
"""


def flux_klein_graph(*, model, text_encoder, vae, positive, seed, width, height, count=1):
    return {
        "diffusion": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "encoder": {"class_type": "CLIPLoader", "inputs": {"clip_name": text_encoder, "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["encoder", 0]}},
        "negative": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["positive", 0]}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": count}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": 4, "width": width, "height": height}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["diffusion", 0], "positive": ["positive", 0], "negative": ["negative", 0], "cfg": 1.0}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0], "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "pitch_chat_flux"}},
    }


def flux_klein_edit_graph(*, reference_name, **settings):
    """Single-reference Distilled edit from the same pinned official template.

    The Host supplies a sanitized upload name and bounded, decoded dimensions.
    Both conditioning branches carry the same VAE latent, as the official edit
    subgraph does. This does not use text-to-image denoise as an edit substitute.
    """
    import re
    if not re.fullmatch(r"pitch_chat_[a-f0-9]{32}_[a-f0-9]{64}\.png", reference_name):
        raise ValueError("Invalid Host reference image name")
    if settings.get("count", 1) != 1:
        raise ValueError("Only a single-reference, single-output edit is reviewed")
    graph = flux_klein_graph(**settings)
    graph.update({
        "reference": {"class_type": "LoadImage", "inputs": {"image": reference_name}},
        "reference_encode": {"class_type": "VAEEncode", "inputs": {"pixels": ["reference", 0], "vae": ["vae", 0]}},
        "reference_positive": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["positive", 0], "latent": ["reference_encode", 0]}},
        "reference_negative": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["negative", 0], "latent": ["reference_encode", 0]}},
    })
    graph["guider"]["inputs"].update(positive=["reference_positive", 0], negative=["reference_negative", 0])
    graph["save"]["inputs"]["filename_prefix"] = "pitch_chat_flux_edit"
    return graph
