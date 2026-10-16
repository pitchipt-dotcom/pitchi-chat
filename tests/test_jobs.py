"""Owned lifecycle, durable jobs and recovery tests. GPU/process/HTTP are mocked."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.jobs.store import JobStore
from backend.app.jobs.worker import JobWorker
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.resources.manager import GPUManager, GPUReading, GPUUnsafe
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.config import ServiceSettings
from backend.app.tools.services import service_registry


def test_job_restart_preserves_and_classifies_existing_jobs(tmp_path):
    store = HistoryStore(tmp_path / "jobs.db")
    store.initialize()
    jobs = JobStore(store)
    call = ToolCall(tool="image.generate", arguments={"prompt": "private input"})
    job_id = jobs.create(call, True)
    jobs.transition(job_id, "RUNNING")
    jobs.remote(job_id, "SUBMITTED", "remote-id")
    JobStore(store).recover()
    job = jobs.get(job_id)
    assert job["status"] == "RECOVERY_REQUIRED" and job["remote_run_id"] == "remote-id"
    assert "private input" not in json.dumps(job)
    assert job["created_at"] and job["started_at"] and job["finished_at"]


@pytest.mark.parametrize("transition", ["SUCCESS", "ERROR", "UNKNOWN"])
def test_job_state_machine_rejects_illegal_transition(tmp_path, transition):
    store = HistoryStore(tmp_path / "jobs.db")
    store.initialize()
    jobs = JobStore(store)
    job_id = jobs.create(ToolCall(tool="book.search"), False)
    with pytest.raises(ValueError):
        jobs.transition(job_id, transition)


def test_persistent_worker_runs_harness_and_redacts_status(tmp_path):
    async def run():
        store = HistoryStore(tmp_path / "jobs.db")
        store.initialize()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                "query": "private query", "count": 0, "offset": 0, "limit": 5, "returned": 0, "has_more": False, "results": []}))) as client:
            registry = service_registry((ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),), client, tmp_path)
            harness = Harness(None, ResourceCoordinator(), AuditTrail(store), registry=registry,
                              policy=TurnPolicy(allowed_tools=frozenset({"book.search"})))
            jobs = JobStore(store)
            worker = JobWorker(jobs, harness)
            worker.start()
            job = worker.submit(ToolCall(tool="book.search", arguments={"query": "private query"}))
            assert job["status"] == "QUEUED"
            await asyncio.wait_for(worker.queue.join(), 1)
            completed = jobs.get(job["job_id"])
            assert completed["status"] == "SUCCESS" and completed["result_reference"]
            assert "private query" not in json.dumps(completed)
            await worker.close()
    asyncio.run(run())


def test_legacy_image_persistent_job_uses_gpu_owner_and_returns_verified_artifact(tmp_path):
    from test_gpu_manager import manager
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        jobs = JobStore(gpu.store)
        job_ids = []
        def handle(request):
            assert gpu.owner == "image" and gpu.phase.value == "BUSY"
            assert gpu.job_id == job_ids[0]
            if request.method == "POST":
                assert json.loads(request.content)["model"] == "synthetic-model"
                return httpx.Response(202, json={"id": "mock-image"})
            if request.url.path.endswith("/images/0"):
                return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG\r\n\x1a\n" + b"mockdata")
            return httpx.Response(200, json={"id": "mock-image", "status": "completed", "images": [{}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            service = ServiceSettings("image.generate", api_base_url="http://localhost", enabled=True,
                      backends=(("qwen-image", "synthetic-model"),), resource_profile="image", host_lifecycle_only=True)
            registry = service_registry((service,), client, tmp_path / "artifacts", frozenset(drivers))
            from dataclasses import replace
            from backend.app.tools.generation import ArtifactStore, ImageAdapter, ImageArguments
            from backend.app.tools.http import ServiceHTTP
            from backend.app.tools.registry import ToolRegistry
            legacy = replace(registry.get("image.generate"), enabled=True, requires_image_compilation=False,
                argument_schema=ImageArguments, allowed_backends=frozenset({"qwen-image"}), remote_resource=None,
                adapter=ImageAdapter(ServiceHTTP(client, service.api_base_url), ArtifactStore(tmp_path / "artifacts"), dict(service.backends)))
            registry = ToolRegistry((legacy,))
            harness = Harness(None, ResourceCoordinator(gpu), AuditTrail(gpu.store), registry=registry,
                      policy=TurnPolicy(allowed_tools=frozenset({"image.generate"})))
            worker = JobWorker(jobs, harness)
            worker.start()
            job = worker.submit(ToolCall(tool="image.generate", backend="qwen-image", arguments={"prompt": "mock"}))
            job_ids.append(job["job_id"])
            await asyncio.wait_for(worker.queue.join(), 1)
            completed = jobs.get(job["job_id"], private=True)
            assert completed["status"] == "SUCCESS" and completed["remote_run_id"] == "mock-image"
            artifact = json.loads(completed["result_json"])["tool_result"]["artifacts"][0]
            assert Path(artifact["path"]).stat().st_size > 0
            assert gpu.phase.value == "STOPPED"
            await worker.close()
        await gpu.close()
    asyncio.run(run())


def test_persistent_video_remote_id_before_poll_and_restart(tmp_path):
    from dataclasses import replace
    from backend.app.tools.registry import ToolRegistry
    async def run():
        store = HistoryStore(tmp_path / "jobs.db")
        store.initialize()
        polling = asyncio.Event()
        async def handle(request):
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "job_id": "remote-1"})
            polling.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            registry = service_registry((ServiceSettings("video.generate", api_base_url="http://localhost"),), client, tmp_path)
            definition = replace(registry.get("video.generate"), enabled=True, requires_managed_gpu=False)
            harness = Harness(None, ResourceCoordinator(), AuditTrail(store), registry=ToolRegistry((definition,)),
                              policy=TurnPolicy(allowed_tools=frozenset({"video.generate"})))
            jobs = JobStore(store)
            worker = JobWorker(jobs, harness, shutdown_timeout=0.02)
            worker.start()
            job = worker.submit(ToolCall(tool="video.generate", arguments={"title": "test", "speech_text": "test",
                                          "subtitle_text": "test", "library_asset_ids": ["asset-1"]}))
            await asyncio.wait_for(polling.wait(), 1)
            assert jobs.get(job["job_id"])["remote_run_id"] == "remote-1"
            await worker.close()
            recovered = jobs.get(job["job_id"])
            assert recovered["status"] == "RECOVERY_REQUIRED" and recovered["remote_run_id"] == "remote-1"
    asyncio.run(run())


def test_job_api_submit_poll_result_and_restart(settings, monkeypatch):
    from backend.app.main import create_app
    from backend.app.tools.book import BookAdapter
    from backend.app.harness.contracts import ToolResult
    async def fake(self, arguments, backend):
        return ToolResult(success=True, result_type="structured", http_status=200,
                 output={"count": 0, "offset": 0, "limit": 5, "returned": 0, "has_more": False, "references": []})
    monkeypatch.setattr(BookAdapter, "execute", fake)
    async def run():
        services = (ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),)
        app = create_app(settings, services=services)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                response = await client.post("/api/jobs", json={"tool": "book.search", "arguments": {"query": "private query"}})
                assert response.status_code == 202
                job_id = response.json()["job_id"]
                assert "private query" not in response.text
                await asyncio.wait_for(app.state.worker.queue.join(), 1)
                assert (await client.get(f"/api/jobs/{job_id}")).json()["status"] == "SUCCESS"
                assert (await client.get(f"/api/jobs/{job_id}/result")).json()["tool_result"]["result_type"] == "structured"
                assert (await client.get("/api/gpu")).json()["state"] == "STOPPED"
        restarted = create_app(settings, services=services)
        async with restarted.router.lifespan_context(restarted):
            assert restarted.state.jobs.get(job_id)["status"] == "SUCCESS"
    asyncio.run(run())
