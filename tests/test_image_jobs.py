"""Compiler/persistence/lifecycle mocks. External ComfyUI and models are not called."""

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.image import ImageRequest
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.store import JobStore
from backend.app.jobs.worker import JobWorker
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.tools.config import ServiceSettings
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.tools.services import service_registry
from test_comfyui import backend, spec, Protocol
from test_gpu_manager import manager


class Compiler:
    def __init__(self, result=None):
        self.result = spec().model_dump() if result is None else result
        self.calls = []
    async def generate_structured(self, messages, schema):
        self.calls.append(messages)
        assert schema["properties"]["backend_preference"]["enum"] == [None, "qwen-image"]
        return json.dumps(self.result)


def setup(tmp_path, client, compiler, *, enabled=True):
    gpu, drivers, inspector = manager(tmp_path)
    service = ServiceSettings("image.generate", api_base_url="http://localhost", enabled=enabled,
        resource_profile="image", host_lifecycle_only=True, image_protocol="comfyui")
    registry = service_registry((service,), client, tmp_path / "artifacts", frozenset(drivers),
        image_workflows=ImageWorkflowRegistry((backend(),)), owned_origins={"image": "http://localhost"})
    registry.get("image.generate").adapter.poll_interval = 0
    registry.get("image.generate").adapter.max_polls = 2
    harness = Harness(compiler, ResourceCoordinator(gpu), AuditTrail(gpu.store), model_name="qwen",
        registry=registry, policy=TurnPolicy(allowed_tools=frozenset({"image.generate"})))
    jobs = JobStore(gpu.store)
    return harness, JobWorker(jobs, harness), jobs, gpu, drivers, inspector


def test_spec_persisted_before_chat_stop_then_one_comfy_submit(tmp_path):
    async def run():
        protocol, compiler = Protocol(), Compiler()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, drivers, inspector = setup(tmp_path, client, compiler)
            checks = []
            old_stop = drivers["qwen"].stop
            async def stop():
                row = jobs.get(jobs.list()[0]["job_id"], private=True)
                assert row["image_spec_json"] and row["image_request_json"]
                assert json.loads(row["call_json"])["arguments"]["spec"]["subject"] == "cat"
                checks.append("persisted_before_stop")
                await old_stop()
            drivers["qwen"].stop = stop
            old_start = drivers["image"].start
            async def start():
                assert not drivers["qwen"].started and 10 not in inspector.pids
                assert checks == ["persisted_before_stop"]
                checks.append("gpu_released_before_image")
                await old_start()
            drivers["image"].start = start
            worker.start()
            job = worker.submit_image(ImageRequest(request="PRIVATE ORIGINAL INTENT", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "SUCCESS", row["error"]
            assert row["remote_run_id"] == "mock-run" and row["remote_phase"] == "TERMINAL"
            assert len(compiler.calls) == 1
            assert "PRIVATE ORIGINAL INTENT" not in json.dumps(protocol.graph)
            public = jobs.get(job["job_id"])
            assert "PRIVATE ORIGINAL INTENT" not in json.dumps(public) and "image_spec_json" not in public
            with gpu.store.connection() as db:
                audit = str([dict(r) for r in db.execute("SELECT * FROM harness_audit")])
            assert "PRIVATE ORIGINAL INTENT" not in audit and "watercolor" not in audit
            assert checks == ["persisted_before_stop", "gpu_released_before_image"]
            assert gpu.phase.value == "STOPPED" and not inspector.pids
            await worker.close(); await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("result", [spec().model_dump() | {"workflow": {}}, spec().model_dump() | {"backend_preference": "unknown"},
    spec().model_dump() | {"reference_image_required": True}, spec().model_dump() | {"aspect_ratio": "16:9"}])
def test_invalid_compiler_proposal_never_dispatches(tmp_path, result):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No ComfyUI request"))) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, Compiler(result))
            worker.start()
            job = worker.submit_image(ImageRequest(request="test", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "ERROR" and row["image_spec_json"] is None
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_preview_disabled_generation_and_raw_tool_bypass_rejected(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No generation"))) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, Compiler(), enabled=False)
            worker.start()
            with pytest.raises(HarnessFailure):
                worker.submit_image(ImageRequest(request="test", backend="qwen-image"))
            job = worker.submit_image(ImageRequest(request="test", backend="qwen-image", compile_only=True))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "SUCCESS" and row["image_phase"] == "SPEC_READY"
            assert json.loads(row["result_json"])["image_spec"]["subject"] == "cat"
            assert row["remote_run_id"] is None
            with pytest.raises(HarnessFailure):
                await host.run_tool(ToolCall(tool="image.generate", backend="qwen-image", arguments={"spec": spec().model_dump()}))
            jobs.recover()  # finished previews remain present
            assert jobs.get(job["job_id"])["status"] == "SUCCESS"
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_spec_ready_restart_is_preserved_not_replayed(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No remote call"))) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, Compiler(), enabled=False)
            request = ImageRequest(request="private", backend="qwen-image", compile_only=True)
            job_id = jobs.create_image(request)
            jobs.transition(job_id, "RUNNING")
            await host.compile_image(request, lambda spec, call: jobs.prepare_image(job_id, spec, call))
            restarted = JobStore(gpu.store); restarted.recover()
            row = restarted.get(job_id, private=True)
            assert row["status"] == "RECOVERY_REQUIRED" and row["image_spec_json"]
            assert row["image_phase"] == "SPEC_READY"
            await gpu.close()
    asyncio.run(run())


def test_failed_spec_persistence_stops_chat_without_image_dispatch(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No ComfyUI"))) as client:
            host, worker, jobs, gpu, _, inspector = setup(tmp_path, client, Compiler())
            def fail(*args):
                raise OSError("PRIVATE persistence failure")
            jobs.prepare_image = fail
            worker.start()
            job = worker.submit_image(ImageRequest(request="test", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"])
            assert row["status"] == "ERROR" and "PRIVATE" not in json.dumps(row)
            assert gpu.phase.value == "STOPPED" and not inspector.pids
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_local_comfy_timeout_stops_owned_process_but_retains_job_recovery(tmp_path):
    async def run():
        protocol = Protocol()
        protocol.history["mock-run"]["status"]["completed"] = False
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, _, inspector = setup(tmp_path, client, Compiler())
            worker.start()
            job = worker.submit_image(ImageRequest(request="test", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "RECOVERY_REQUIRED" and row["remote_run_id"] == "mock-run"
            assert row["image_spec_json"]
            assert gpu.phase.value == "STOPPED" and not inspector.pids
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_registry_never_reinterprets_legacy_origin_or_mismatched_process(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No request"))) as client:
            for protocol, origin in [("", "http://localhost"), ("comfyui", "http://localhost:2")]:
                service = ServiceSettings("image.generate", api_base_url="http://localhost", enabled=True,
                    resource_profile="image", host_lifecycle_only=True, image_protocol=protocol)
                registry = service_registry((service,), client, tmp_path, frozenset({"image"}),
                    image_workflows=ImageWorkflowRegistry((backend(),)), owned_origins={"image": origin})
                definition = registry.get("image.generate")
                assert not definition.enabled and definition.requires_image_compilation
    asyncio.run(run())


def test_image_api_202_preview_private_status_and_bypass_denial(settings, monkeypatch):
    from backend.app.main import create_app
    async def run():
        service = ServiceSettings("image.generate", api_base_url="http://localhost", image_protocol="comfyui")
        app = create_app(settings, adapter=Compiler(), services=(service,), image_workflows=ImageWorkflowRegistry((backend(),)))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                result = await client.post("/api/images/jobs", json={"request": "PRIVATE", "backend": "qwen-image", "compile_only": True})
                assert result.status_code == 202
                job_id = result.json()["job_id"]
                await asyncio.wait_for(app.state.worker.queue.join(), 2)
                status = await client.get(f"/api/jobs/{job_id}")
                assert status.json()["status"] == "SUCCESS" and "PRIVATE" not in status.text
                assert (await client.get(f"/api/jobs/{job_id}/result")).json()["image_spec"]["subject"] == "cat"
                assert (await client.post("/api/jobs", json={"tool": "image.generate", "arguments": {"spec": spec().model_dump()}})).status_code == 409
                assert (await client.post("/api/images/jobs", json={"request": "test", "backend": "unknown", "compile_only": True})).status_code == 403
    asyncio.run(run())


def test_confirmed_edited_preview_is_rendered_without_second_model_call(tmp_path):
    async def run():
        protocol, compiler = Protocol(), Compiler()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, drivers, inspector = setup(tmp_path, client, compiler)
            worker.start()
            try:
                preview=worker.submit_image(ImageRequest(request='original intent',backend='qwen-image',compile_only=True))
                await asyncio.wait_for(worker.queue.join(),2)
                assert jobs.get(preview['job_id'])['status']=='SUCCESS'
                changed=spec().model_copy(update={'subject':'USER EDITED SUBJECT'})
                rendered=worker.submit_image_render(preview['job_id'],changed)
                await asyncio.wait_for(worker.queue.join(),2)
                assert jobs.get(rendered['job_id'])['status']=='SUCCESS'
                assert len(compiler.calls)==1
                assert 'USER EDITED SUBJECT' in json.dumps(protocol.graph)
                assert jobs.get(preview['job_id'])['image_phase']=='SPEC_READY'
                with pytest.raises(ValueError):
                    worker.submit_image_render(rendered['job_id'],changed)
                with pytest.raises(ValueError):
                    worker.submit_image_render(preview['job_id'],changed.model_copy(update={'aspect_ratio':'16:9'}))
                with pytest.raises(ValueError):
                    worker.submit_image_render(preview['job_id'],changed.model_copy(update={'reference_image_required':True}))
                with pytest.raises(ValueError):
                    worker.submit(ToolCall(tool='image.generate',backend='qwen-image',arguments={'spec':changed.model_dump(),'count':1}))
            finally:
                await worker.close()
                await gpu.close()
    asyncio.run(run())
