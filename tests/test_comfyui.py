"""Independent image workflow/protocol mocks, never real GPU generation."""

import asyncio
import json
from pathlib import Path
import struct
import zlib

import httpx
import pytest
from pydantic import ValidationError

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.image import ImageSpec, ImageToolArguments
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.resources.gpu import ResourceCoordinator, ResourceMode
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.comfyui import ComfyImageAdapter
from backend.app.tools.generation import ArtifactStore, ImageOutput
from backend.app.tools.http import ServiceHTTP
from backend.app.tools.image_workflows import ImageBackend, ImageWorkflowRegistry, load_image_workflows
from backend.app.tools.png import validate_png
from backend.app.tools.registry import ToolDefinition, ToolRegistry


def spec():
    return ImageSpec(purpose="poster", subject="cat", style="watercolor", composition="centered",
                     aspect_ratio="1:1", text_requirement="CAT", backend_preference="qwen-image")


def backend():
    return ImageBackend(id="qwen-image", family="qwen-image", model="synthetic.safetensors",
                        text_encoder="synthetic_clip.safetensors", vae="synthetic_vae.safetensors")


def png(width=1, height=1):
    def chunk(kind, data):
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x11\x22\x33"*width)*height)) + chunk(b"IEND", b"")


class Protocol:
    def __init__(self):
        self.calls = []
        self.graph = None
        self.queue = {"queue_running": [], "queue_pending": []}
        self.history = {"mock-run": {"status": {"completed": True, "status_str": "success"},
            "outputs": {"save": {"images": [{"filename": "synthetic.png", "subfolder": "", "type": "output"}]}}}}
        self.image = png(768, 768)
        self.missing = False
        self.malformed = False

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.startswith("/object_info/"):
            name = path.rsplit("/", 1)[-1]
            if self.missing:
                return httpx.Response(200, json={})
            if self.malformed:
                return httpx.Response(200, json={name: {"input": []}})
            node = next(n for n in backend().graph(spec(), 1).values() if n["class_type"] == name)
            inputs = {key: [[value]] if isinstance(value, str) and key not in {"text", "filename_prefix"} else ["STRING"] for key, value in node["inputs"].items()}
            return httpx.Response(200, json={name: {"input": {"required": inputs}}})
        if path == "/queue":
            return httpx.Response(200, json=self.queue)
        if path == "/prompt":
            self.graph = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "mock-run", "node_errors": {}})
        if path == "/history/mock-run":
            return httpx.Response(200, json=self.history)
        if path == "/view":
            return httpx.Response(200, content=self.image, headers={"content-type": "image/png"})
        pytest.fail("Unexpected route: " + path)


def build_harness(tmp_path, client, *, max_polls=2):
    store = HistoryStore(tmp_path / "audit.db"); store.initialize()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = ComfyImageAdapter(ServiceHTTP(client, "http://localhost"), artifacts,
                               ImageWorkflowRegistry((backend(),)), poll_interval=0, max_polls=max_polls)
    definition = ToolDefinition("image.generate", adapter, ImageToolArguments, ImageOutput, "image", ResourceMode.IMAGE,
                                enabled=True, allowed_backends=frozenset({"qwen-image"}), allowed_roots=(artifacts.root,),
                                allow_private=True, requires_managed_gpu=False, requires_http_status=True, min_artifacts=1)
    return Harness(None, ResourceCoordinator(), AuditTrail(store), registry=ToolRegistry((definition,)),
                   policy=TurnPolicy(allowed_tools=frozenset({"image.generate"})))


def call():
    return ToolCall(tool="image.generate", backend="qwen-image", arguments={"spec": spec().model_dump(), "count": 1})


def test_independent_comfyui_harness_artifact_and_no_legacy_dependency(tmp_path):
    async def run():
        protocol = Protocol()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host = build_harness(tmp_path, client)
            result = await host.run_tool(call())
            assert result.status.value == "SUCCESS"
            assert validate_png(Path(result.tool_result.artifacts[0].path).read_bytes()) == (768, 768)
            assert len([r for r in protocol.calls if r.method == "POST"]) == 1
            assert all("/api/generations" not in r.url.path for r in protocol.calls)
            assert "CAT" in protocol.graph["positive"]["inputs"]["text"]
            assert not any(n["class_type"] == "LoraLoaderModelOnly" for n in protocol.graph.values())
            with host.audit.store.connection() as db:
                audit = str([dict(r) for r in db.execute("SELECT * FROM harness_audit")])
            assert "synthetic.safetensors" not in audit and "watercolor" not in audit
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing", "malformed", "busy", "empty", "error", "wrong_node", "wrong_count", "path", "control", "dimensions", "crc", "pending"])
def test_invalid_generation_never_success_and_no_global_interrupt(tmp_path, failure):
    async def run():
        protocol = Protocol()
        entry = protocol.history["mock-run"]
        if failure == "missing": protocol.missing = True
        elif failure == "malformed": protocol.malformed = True
        elif failure == "busy": protocol.queue["queue_running"] = [["foreign"]]
        elif failure == "empty": protocol.image = b""
        elif failure == "error": entry["status"]["status_str"] = "error"
        elif failure == "wrong_node": entry["outputs"]["foreign"] = entry["outputs"].pop("save")
        elif failure == "wrong_count": entry["outputs"]["save"]["images"] = []
        elif failure == "path": entry["outputs"]["save"]["images"][0]["subfolder"] = "../private"
        elif failure == "control": entry["outputs"]["save"]["images"][0]["filename"] = "bad\n.png"
        elif failure == "dimensions": protocol.image = png(1, 1)
        elif failure == "crc": protocol.image = protocol.image[:-1] + b"x"
        elif failure == "pending": entry["status"]["completed"] = False
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            with pytest.raises(HarnessFailure):
                await build_harness(tmp_path, client).run_tool(call())
            assert all(r.url.path not in {"/free", "/interrupt"} for r in protocol.calls)
            if failure in {"missing", "malformed", "busy"}:
                assert not any(r.method == "POST" for r in protocol.calls)
    asyncio.run(run())


@pytest.mark.parametrize("extra", [{"endpoint": "bad"}, {"workflow": {}}, {"command": "bad"}, {"subject": " "}, {"negative_constraints": ["x"*301]}])
def test_image_spec_rejects_infrastructure_and_invalid_intent(extra):
    with pytest.raises(ValidationError):
        ImageSpec.model_validate(spec().model_dump() | extra)


def test_workflow_config_empty_default_and_unsafe_models(tmp_path):
    assert not load_image_workflows().backends
    for filename in ["/absolute", "../model", "a/../b", "a\\b"]:
        with pytest.raises(ValueError):
            ImageBackend(id="example", family="sdxl", model=filename)
    with pytest.raises(ValidationError):
        ImageBackend(id="fast-h2", family="fast-h2", model="unknown")


def test_sdxl_graph_is_distinct_and_does_not_mutate_spec():
    original = spec().model_dump()
    graph = ImageBackend(id="sdxl", family="sdxl", model="synthetic.safetensors").graph(spec(), 2)
    assert graph["latent"]["class_type"] == "EmptyLatentImage"
    assert graph["latent"]["inputs"]["batch_size"] == 2
    assert "diffusion" not in graph and spec().model_dump() == original


def test_registry_table_name_cannot_be_overridden(tmp_path):
    from unittest.mock import patch
    config = tmp_path / "registry.toml"
    config.touch()
    for entries in ({"safe": {"id": "other", "family": "sdxl", "model": "synthetic"}}, {"safe": "invalid"}):
        with patch("backend.app.tools.image_workflows.tomllib.load", return_value={"backends": entries}):
            with pytest.raises(ValueError):
                load_image_workflows(config)


def test_reviewed_on_demand_catalog_and_compile_only_backend(tmp_path):
    async def run():
        def unavailable(request):
            raise httpx.ConnectError('offline',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            entries=ImageWorkflowRegistry((backend(),ImageBackend(id='sdxl',family='sdxl',model='test.safetensors')))
            adapter=ComfyImageAdapter(ServiceHTTP(client,'http://localhost'),ArtifactStore(tmp_path/'images'),entries,
                                     on_demand=True,generation_backends=frozenset({'sdxl'}))
            statuses={row['id']:row['status'] for row in await adapter.catalog()}
            assert statuses == {'qwen-image':'compile_only','sdxl':'on_demand'}
            with pytest.raises(HarnessFailure) as error:
                await adapter.execute(ImageToolArguments(spec=spec(),count=1),'qwen-image')
            assert error.value.code.value == 'policy_denied'
    asyncio.run(run())
