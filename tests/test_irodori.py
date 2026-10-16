"""Local speech API mock tests; no real TTS service or GPU is started."""

import asyncio
import io
import wave

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.config import ServiceSettings
from backend.app.tools.generation import ArtifactStore
from backend.app.tools.http import ServiceHTTP
from backend.app.tools.irodori import IrodoriAdapter, TTSArguments
from backend.app.tools.services import service_registry


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 12)
    return buffer.getvalue()


@pytest.mark.parametrize("status,mime,data", [
    (500, "audio/wav", wav()), (200, "text/html", wav()),
    (200, "audio/wav", b"not audio"), (200, "audio/wav", wav()[:-2]),
])
def test_invalid_speech_is_failure(tmp_path, status, mime, data):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(status, headers={"content-type": mime}, content=data))) as client:
            adapter = IrodoriAdapter(ServiceHTTP(client, "http://localhost", 1), ArtifactStore(tmp_path), {"irodori": "synthetic-model"})
            with pytest.raises(HarnessFailure):
                await adapter.execute(TTSArguments(text="テスト"), "irodori")
            assert not list(tmp_path.glob("*.wav"))
    asyncio.run(run())


def test_tts_harness_resource_and_wave_validation(tmp_path):
    from test_gpu_manager import manager
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        store = gpu.store
        def handle(request):
            assert gpu.owner == "image" and gpu.phase.value == "BUSY"
            assert request.url.path == "/v1/audio/speech"
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=wav())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            settings = ServiceSettings("tts.generate", api_base_url="http://localhost", enabled=True,
                    backends=(("irodori", "synthetic-model"),), resource_profile="image", host_lifecycle_only=True)
            registry = service_registry((settings,), client, tmp_path / "artifacts", frozenset(drivers),
                                        owned_origins={"image": "http://localhost"})
            harness = Harness(None, ResourceCoordinator(gpu), AuditTrail(store), registry=registry,
                    policy=TurnPolicy(allowed_tools=frozenset({"tts.generate"})))
            result = await harness.run_tool(ToolCall(tool="tts.generate", arguments={"text": "テスト"}))
            assert result.status.value == "SUCCESS"
            assert result.tool_result.output["frames"] == 12
            assert gpu.phase.value == "STOPPED" and drivers["image"].events == ["start", "stop"]
        await gpu.close()
    asyncio.run(run())


def test_unmanaged_tts_never_dispatches(tmp_path):
    async def run():
        store = HistoryStore(tmp_path / "jobs.db")
        store.initialize()
        def handle(request):
            pytest.fail("Unmanaged TTS must not dispatch")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            settings = ServiceSettings("tts.generate", api_base_url="http://localhost", enabled=True,
                     backends=(("irodori", "synthetic-model"),))
            registry = service_registry((settings,), client, tmp_path)
            harness = Harness(None, ResourceCoordinator(), AuditTrail(store), registry=registry,
                     policy=TurnPolicy(allowed_tools=frozenset({"tts.generate"})))
            with pytest.raises(HarnessFailure):
                await harness.run_tool(ToolCall(tool="tts.generate", arguments={"text": "テスト"}))
    asyncio.run(run())


@pytest.mark.parametrize("url,origins", [
    ("http://localhost", {}),
    ("http://localhost", {"image": "http://localhost:9999"}),
    ("https://synthetic.ts.net", {"image": "https://synthetic.ts.net"}),
])
def test_tts_owned_gpu_origin_must_match(tmp_path, url, origins):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no dispatch"))) as client:
            s = ServiceSettings("tts.generate", api_base_url=url, enabled=True, resource_profile="image",
                                host_lifecycle_only=True, backends=(("irodori", "synthetic"),))
            registry = service_registry((s,), client, tmp_path, frozenset({"image"}), owned_origins=origins)
            assert not registry.get("tts.generate").enabled
    asyncio.run(run())


def test_missing_tts_key_fails_before_submit(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNTHETIC_TTS_KEY", raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no dispatch"))) as client:
            adapter = IrodoriAdapter(ServiceHTTP(client, "http://localhost"), ArtifactStore(tmp_path),
                                     {"irodori": "synthetic"}, "SYNTHETIC_TTS_KEY")
            with pytest.raises(HarnessFailure):
                await adapter.execute(TTSArguments(text="テスト"), "irodori")
    asyncio.run(run())


def test_tts_disabled_probe_never_submits():
    from scripts.test_tts_e2e import probe
    async def run():
        def handle(r):
            assert r.method == "GET" and r.url.path == "/api/tools"
            return httpx.Response(200, json={"tools": [{"id": "tts.generate", "enabled": False}]})
        async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
            result = await probe(client, generate=True)
            assert result["status"] == "SKIPPED" and not result["submitted"]
    asyncio.run(run())


def test_speech_job_persists_and_survives_worker_restart(tmp_path):
    from backend.app.jobs.worker import JobWorker
    from backend.app.jobs.store import JobStore
    from test_gpu_manager import manager
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:
                httpx.Response(200, headers={"content-type": "audio/wav"}, content=wav()))) as client:
            settings = ServiceSettings("tts.generate", api_base_url="http://localhost", enabled=True,
                backends=(("irodori", "synthetic"),), resource_profile="image", host_lifecycle_only=True)
            registry = service_registry((settings,), client, tmp_path / "artifacts", frozenset(drivers),
                                        owned_origins={"image": "http://localhost"})
            h = Harness(None, ResourceCoordinator(gpu), AuditTrail(gpu.store), registry=registry,
                        policy=TurnPolicy(allowed_tools=frozenset({"tts.generate"})))
            jobs = JobStore(gpu.store)
            worker = JobWorker(jobs, h)
            worker.start()
            row = worker.submit(ToolCall(tool="tts.generate", arguments={"text": "合成テスト"}))
            await asyncio.wait_for(worker.queue.join(), 2)
            assert jobs.get(row["job_id"])["status"] == "SUCCESS"
            assert jobs.get(row["job_id"])["remote_phase"] == "TERMINAL"
            await worker.close()
            jobs.recover()
            assert jobs.get(row["job_id"])["status"] == "SUCCESS"
            assert gpu.phase.value == "STOPPED" and drivers["image"].events == ["start", "stop"]
        await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("gpu", [
    {"state": "ERROR", "memory_used_mib": 0},
    {"state": "STOPPED", "memory_used_mib": None},
    {"state": "STOPPED", "memory_used_mib": 100, "external_conflict": "unknown"},
    {"state": "BUSY", "memory_used_mib": 100},
])
def test_tts_probe_gpu_uncertainty_never_submits(gpu):
    from scripts.test_tts_e2e import probe
    async def run():
        def handle(r):
            assert r.method == "GET"
            return httpx.Response(200, json={"tools": [{"id": "tts.generate", "enabled": True}]} if r.url.path == "/api/tools" else gpu)
        async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
            result = await probe(client, generate=True)
            assert result["status"] == "SKIPPED" and not result["submitted"]
    asyncio.run(run())
