"""Isolated image upload/history/edit protocol tests. No GPU or real LLM."""
import asyncio
import io
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from backend.app.image_assets import ImageAssetStore, ImageReference, decoded_image, router
from backend.app.storage.sqlite import HistoryStore
from test_comfyui import png


def assets(tmp_path):
    history = HistoryStore(tmp_path / "history.db"); history.initialize()
    store = ImageAssetStore(history, tmp_path / "images")
    return store, store.create_conversation()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_decode_normalize_and_keep_original_pixels(tmp_path, format):
    store, cid = assets(tmp_path)
    image = Image.new("RGB", (1500, 700), "red"); out = io.BytesIO()
    image.save(out, format=format, exif=Image.Exif())
    record = store.save(out.getvalue(), cid)
    ref = store.reference(record["id"], cid)
    assert (record["original_width"], record["original_height"]) == (1500, 700)
    assert (ref.width, ref.height) == (1024, 480)
    assert Image.open(io.BytesIO(store.reference_bytes(ref))).size == (1024, 480)
    assert store.path(ref.id).stat().st_mode & 0o777 == 0o600
    assert record["source_kind"] == "upload" and record["parent_id"] is None
    with pytest.raises(ValueError): store.reference(ref.id, str(uuid4()))
    bad = ref.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError): store.reference_bytes(bad)
    store.path(ref.id, reference=True).write_bytes(b"changed")
    with pytest.raises(ValueError): store.reference_bytes(ref)


@pytest.mark.parametrize("data", [b"<svg onload='bad'/>", b"", png(16, 16)[:50], png(1, 1), png(4100, 16)])
def test_reject_bad_images(data):
    with pytest.raises(ValueError): decoded_image(data)


def test_animated_rejected_and_metadata_removed(tmp_path):
    store, cid = assets(tmp_path)
    from PIL.PngImagePlugin import PngInfo
    metadata = PngInfo(); metadata.add_text("prompt", "private synthetic metadata")
    out = io.BytesIO(); Image.new("RGB", (32, 32)).save(out, "PNG", pnginfo=metadata)
    record = store.save(out.getvalue(), cid)
    assert "prompt" not in Image.open(store.path(record["id"])).info
    out = io.BytesIO(); Image.new("RGB", (32, 32), "red").save(out, "WEBP", save_all=True,
        append_images=[Image.new("RGB", (32, 32), "blue")])
    with pytest.raises(ValueError): decoded_image(out.getvalue())


def test_reference_lineage_and_idempotent_result_capture(tmp_path):
    store, cid = assets(tmp_path)
    assert store.text_only_notice(cid) is None
    a = store.save(png(32, 32), cid)
    assert "画像データを一切渡していません" in store.text_only_notice(cid)
    b = store.save(png(32, 32), cid, parent_id=a["id"], source_job_id="synthetic-first")
    c = store.save(png(32, 32), cid, parent_id=b["id"], source_job_id="synthetic-second")
    assert c["parent_id"] == b["id"] and b["parent_id"] == a["id"]
    assert a["source_kind"] == "upload" and c["source_kind"] == "generated"
    assert store.save(png(32, 32), cid, source_job_id="synthetic-first")["id"] == b["id"]
    with pytest.raises(ValueError): store.get("../../bad")
    path = store.path(a["id"]); path.unlink(); path.symlink_to(store.path(b["id"]))
    with pytest.raises(ValueError): store.path(a["id"])


def test_upload_api_and_vision_truth(tmp_path):
    async def run():
        store, cid = assets(tmp_path)
        app = FastAPI(); app.include_router(router()); app.state.image_assets = store; app.state.image_upload_lock = asyncio.Lock()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
            path = "/api/images/attachments?conversation_id=" + cid
            assert (await client.post(path, content=png(32, 32), headers={"Content-Type": "image/png", "Origin": "http://foreign.invalid"})).status_code == 403
            assert (await client.post(path, content=b"bad", headers={"Content-Type": "image/svg+xml"})).status_code == 415
            assert (await client.post(path, content=b"bad", headers={"Content-Type": "image/png"})).status_code == 422
            response = await client.post(path, content=png(32, 32), headers={"Content-Type": "image/png"})
            assert response.status_code == 201, response.text
            image = response.json()
            response = await client.get(image["url"])
            assert response.headers["cache-control"] == "no-store" and response.content.startswith(b"\x89PNG")
            assert (await client.get("/api/images/vision")).json()["available"] is False
    asyncio.run(run())
