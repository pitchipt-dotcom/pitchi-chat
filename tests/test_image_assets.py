"""Isolated image upload/history/edit protocol tests. No GPU or real LLM."""
import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from backend.app.image_assets import ImageAssetStore, ImageReference, decoded_image, router
from backend.app.harness.contracts import ToolCall
from backend.app.harness.recovery import HarnessFailure
from backend.app.media import MediaPipeline, MediaRequest, MediaPromptEdit
from backend.app.resources.media_memory import selected_backend, effective_profile
from backend.app.resources.manager import GPUUnsafe
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from test_flux_klein import flux
from test_image_jobs import setup, Compiler
from test_comfyui import Protocol, png, spec


class EditProtocol(Protocol):
    def __init__(self):
        super().__init__(); self.image = png(32, 32); self.upload_name = None

    def handle(self, request):
        path = request.url.path
        if path == "/upload/image":
            self.calls.append(request)
            import re
            self.upload_name = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
            return httpx.Response(200, json={"name": self.upload_name, "subfolder": "", "type": "input"})
        if path.startswith("/object_info/"):
            name = path.rsplit("/", 1)[-1]
            reference = ImageReference(id=uuid4(), sha256="0" * 64, width=32, height=32)
            graph = flux().graph(spec(), 1, reference=reference)
            node = next(n for n in graph.values() if n["class_type"] == name)
            inputs = {key: ["STRING"] for key in node["inputs"]}
            if name == "LoadImage": inputs["image"] = [[self.upload_name]]
            return httpx.Response(200, json={name: {"input": {"required": inputs}}})
        return super().handle(request)


def test_actual_worker_direct_then_strata_preview_edit_and_reedit_mock(tmp_path):
    async def run():
        protocol = EditProtocol()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, drivers, _ = setup(tmp_path, client, Compiler())
            definition = host.registry.get("image.generate")
            # Preserve normal service policies, add only a synthetic Klein backend.
            from backend.app.tools.registry import ToolRegistry
            definition.adapter.workflows = ImageWorkflowRegistry((flux(),))
            definition.adapter.generation_backends = frozenset({"flux-klein"})
            host.registry = ToolRegistry((replace(definition, allowed_backends=frozenset({"flux-klein"})),))
            store = ImageAssetStore(jobs.store, tmp_path / "images"); cid = store.create_conversation()
            original = store.save(png(32, 32), cid)
            definition.adapter.image_assets = store
            worker.media_pipeline = MediaPipeline(host, client, image_assets=store)
            calls = []
            async def rewrite(instruction):
                calls.append(instruction); return "Make the background blue. Preserve the subject."
            host.compile_image_edit = rewrite
            worker.start()
            direct = "背景を青に。被写体はそのまま。"
            request = MediaRequest(prompt=direct, prompt_mode="direct", backend="flux-klein", conversation_id=UUID(cid), reference_image_id=UUID(original["id"]))
            first = worker.submit_media(request); await worker.queue.join()
            row = jobs.get(first["job_id"], private=True)
            assert row["status"] == "SUCCESS", row["error"]
            assert protocol.graph["positive"]["inputs"]["text"] == direct and not calls
            assert protocol.graph["guider"]["inputs"]["negative"] == ["reference_negative", 0]
            result = json.loads(row["result_json"])
            generated = result["image_spec"]["conversation_images"][0]
            assert generated["parent_id"] == original["id"]
            second_request = request.model_copy(update={"prompt_mode": "strata", "preview_only": True,
                "prompt": "もう少し明るく。人物はそのまま。", "reference_image_id": UUID(generated["id"])})
            preview = worker.submit_media(second_request); await worker.queue.join()
            assert jobs.get(preview["job_id"])["status"] == "SUCCESS"
            assert calls == [second_request.prompt]
            before = jobs.get(preview["job_id"], private=True)["image_spec_json"]
            child = worker.retry_media(preview["job_id"], edit=MediaPromptEdit(positive_prompt="Brighten the image. Preserve the person."))
            await worker.queue.join()
            row = jobs.get(child["job_id"], private=True)
            assert row["status"] == "SUCCESS", row["error"]
            next_image = json.loads(row["result_json"])["image_spec"]["conversation_images"][0]
            assert next_image["parent_id"] == generated["id"]
            assert len(calls) == 1 and jobs.get(preview["job_id"], private=True)["image_spec_json"] == before
            assert store.timeline(cid)["images"][-1]["id"] == next_image["id"]
            with pytest.raises(HarnessFailure): await host.run_tool(ToolCall.model_validate_json(row["call_json"]))
            assert not gpu.loaded
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_edit_requires_distinct_memory_budget_before_model_switch(tmp_path):
    from test_comfy_memory import profile
    p = profile(tmp_path); manager = SimpleNamespace(drivers={"comfyui": SimpleNamespace(profile=p)})
    with pytest.raises(GPUUnsafe, match="backend_memory_budget_missing"):
        with selected_backend(manager, "comfyui", "flux-klein-edit"): pass
    p = replace(p, backend_ram_mib=p.backend_ram_mib + (("flux-klein-edit", 32768),))
    manager.drivers["comfyui"].profile = p
    with selected_backend(manager, "comfyui", "flux-klein-edit"):
        assert effective_profile(p).low_swap_start_ram_mib == 36864
    assert effective_profile(p) == p


def test_chat_image_dom():
    import subprocess
    result = subprocess.run(["node", "tests/frontend_chat_images.cjs"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_chat_gets_host_image_unavailable_notice_without_history_pollution(settings):
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from test_chat import FakeAdapter
    adapter = FakeAdapter()
    with TestClient(create_app(settings, adapter)) as client:
        cid = client.post("/api/images/conversations").json()["conversation_id"]
        assert client.post("/api/images/attachments?conversation_id=" + cid, content=png(32, 32), headers={"Content-Type": "image/png"}).status_code == 201
        response = client.post("/api/chat", json={"message": "画像は見えますか", "conversation_id": cid})
        assert response.status_code == 200, response.text
        assert adapter.calls[0][0].role == "system"
        assert "画像データを一切渡していません" in adapter.calls[0][0].content
        history = client.get("/api/conversations/" + cid).json()["messages"]
        assert len(history) == 2 and all(x["role"] != "system" for x in history)


@pytest.mark.parametrize("invalid", [False, True])
def test_strata_rewrite_actual_adapter_contract_is_text_only(tmp_path, invalid):
    async def run():
        from backend.app.config import Settings, ModelSettings
        from backend.app.models.registry import ModelRegistry
        from backend.app.models.strata import StrataAdapter
        from test_model_usage import Driver
        payloads = []
        def handle(request):
            payload = json.loads(request.content); payloads.append(payload)
            content = json.dumps({"prompt": "Brighten the image. Preserve the person."} if not invalid else {"prompt": "ok", "tool": "shell"})
            return httpx.Response(200, json={"model": "synthetic-strata", "choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            host, worker, jobs, gpu, drivers, inspector = setup(tmp_path, client, Compiler())
            drivers["strata"] = Driver(inspector, 12)
            gpu.drivers["strata"] = drivers["strata"]
            settings = Settings(tmp_path / "unused.db", "http://localhost/v1/chat/completions", "synthetic-strata", "", 3,
                models=(ModelSettings("strata", "strata", "http://localhost/v1/chat/completions", "synthetic-strata", "", 3),), default_model="strata")
            host.models = ModelRegistry(settings, client, {"strata": StrataAdapter(settings, client)}, frozenset(drivers))
            if invalid:
                with pytest.raises(HarnessFailure): await host.compile_image_edit("もう少し明るく。人物はそのまま。")
            else:
                assert await host.compile_image_edit("もう少し明るく。人物はそのまま。") == "Brighten the image. Preserve the person."
            assert len(payloads) == 1
            payload = payloads[0]
            assert payload["reasoning_effort"] == "none" and payload["reasoning_budget_tokens"] == 0
            assert payload["max_tokens"] == 4096 and payload["stream"] is False
            assert all(isinstance(m["content"], str) for m in payload["messages"])
            assert "CANNOT see" in payload["messages"][0]["content"]
            assert "tools" not in payload and "image_url" not in json.dumps(payload)
            assert drivers["strata"].events == ["start"]
            await gpu.close()
    asyncio.run(run())
