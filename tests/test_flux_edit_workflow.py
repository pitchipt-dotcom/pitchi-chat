"""Pure graph checks; no models, HTTP, GPU or image files."""
import pytest
from backend.app.tools.flux_klein import flux_klein_graph, flux_klein_edit_graph


def settings():
    return dict(model="synthetic_flux.safetensors", text_encoder="synthetic_encoder.safetensors",
                vae="synthetic_vae.safetensors", positive="Change the background to blue.",
                seed=17, width=1024, height=768, count=1)


def test_official_edit_keeps_distilled_settings_and_both_reference_branches():
    name = "pitch_chat_" + "a" * 32 + "_" + "b" * 64 + ".png"
    graph = flux_klein_edit_graph(reference_name=name, **settings())
    plain = flux_klein_graph(**settings())
    for node in ("diffusion", "encoder", "vae", "positive", "negative", "latent", "sigmas", "noise", "sampler", "sample", "decode"):
        assert graph[node] == plain[node]
    assert graph["reference"]["inputs"]["image"] == name
    assert graph["reference_encode"]["inputs"] == {"pixels": ["reference", 0], "vae": ["vae", 0]}
    for branch in ("positive", "negative"):
        assert graph["reference_" + branch] == {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": [branch, 0], "latent": ["reference_encode", 0]}}
        assert graph["guider"]["inputs"][branch] == ["reference_" + branch, 0]
    assert graph["guider"]["inputs"]["cfg"] == 1 and graph["sigmas"]["inputs"]["steps"] == 4


@pytest.mark.parametrize("name", ["../image.png", "https://foreign.invalid/image.png", "image.png", "input/test.png", "image%2f.png"])
def test_refuses_non_host_names(name):
    with pytest.raises(ValueError): flux_klein_edit_graph(reference_name=name, **settings())


def test_refuses_unreviewed_batch():
    with pytest.raises(ValueError):
        flux_klein_edit_graph(reference_name="pitch_chat_" + "a" * 32 + "_" + "b" * 64 + ".png", **(settings() | {"count": 2}))
