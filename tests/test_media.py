"""Isolated Host/Simple UI protocol tests; no real models or GPU calls."""

import asyncio
import json

import httpx
import pytest

from backend.app.harness.contracts import ToolCall
from backend.app.harness.recovery import HarnessFailure
from backend.app.media import MediaPipeline, MediaRequest
from test_comfyui import Protocol
from test_image_jobs import Compiler, setup


def test_saved_prompt_handoff_and_direct_generation(tmp_path):
    async def run():
        protocol, compiler = Protocol(), Compiler()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, drivers, inspector = setup(tmp_path, client, compiler)
            worker.media_pipeline = MediaPipeline(host, client)
            async with host.resources.job("CHAT", model="qwen", profile="qwen", keep_ready=True):
                pass
            original_stop = drivers["qwen"].stop
            async def stop():
                row = jobs.get(jobs.list()[0]["job_id"], private=True)
                plan = json.loads(row["image_spec_json"])
                assert plan["positive_prompt"] == "a quiet green garden"
                assert plan["seed"] == 17 and plan["graph"]["sample"]["inputs"]["seed"] == 17
                assert row["media_phase"] == "STRATA_STOPPING"
                await original_stop()
            drivers["qwen"].stop = stop
            worker.start()
            job = worker.submit_media(MediaRequest(prompt="a quiet green garden", prompt_mode="direct",
                                                   backend="qwen-image", seed=17))
            await asyncio.wait_for(worker.queue.join(), 3)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "SUCCESS", row["error"]
            assert not compiler.calls
            assert protocol.graph["positive"]["inputs"]["text"] == "a quiet green garden"
            assert [e["phase"] for e in jobs.media_events(job["job_id"])] == [
                "PROMPT_GENERATING", "PROMPT_SAVED", "STRATA_STOPPING", "RESOURCE_CHECK",
                "COMFY_STARTING", "GENERATING", "COMPLETED"]
            assert gpu.loaded is None and not inspector.pids
            # Explicit regeneration reuses the plan; no compiler/Strata request.
            again = worker.retry_media(job["job_id"])
            await asyncio.wait_for(worker.queue.join(), 3)
            assert jobs.get(again["job_id"])["status"] == "SUCCESS"
            assert not compiler.calls
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_strata_uses_existing_simple_ui_prompt_api_without_outer_gpu_lock(tmp_path):
    async def run():
        protocol = Protocol(); planned = []
        def route(request):
            if request.url.path == "/api/prompt-preview":
                assert not host.resources._lock.locked()
                planned.append(json.loads(request.content))
                return httpx.Response(200, json={"planning_method": "strata", "workflow": "qwen_image_basic",
                    "image_prompt": "watercolor of a cat in a quiet green garden", "negative_prompt": "watermark", "over_limit": False})
            return protocol.handle(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, Compiler())
            worker.media_pipeline = MediaPipeline(host, client, "http://localhost:19090")
            worker.start()
            job = worker.submit_media(MediaRequest(prompt="庭の猫を水彩で", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 3)
            assert jobs.get(job["job_id"])["status"] == "SUCCESS"
            assert len(planned) == 1 and planned[0]["prompt_mode"] == "strata"
            assert protocol.graph["negative"]["inputs"]["text"] == "watermark"
            await worker.close(); await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["unavailable", "wrong-provider", "wrong-workflow"])
def test_prompt_failure_never_starts_comfy(tmp_path, bad):
    async def run():
        def route(request):
            assert request.url.path == "/api/prompt-preview"
            return httpx.Response(502 if bad == "unavailable" else 200, json={
                "planning_method": "rules" if bad == "wrong-provider" else "strata", "workflow": "unknown"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            host, worker, jobs, gpu, drivers, _ = setup(tmp_path, client, Compiler())
            worker.media_pipeline = MediaPipeline(host, client, "http://localhost:19090")
            worker.start(); job = worker.submit_media(MediaRequest(prompt="a garden", backend="qwen-image"))
            await asyncio.wait_for(worker.queue.join(), 3)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "ERROR" and row["media_phase"] == "FAILED"
            assert not drivers["image"].started and row["image_spec_json"] is None
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_restart_and_unknown_submission_never_replay(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no HTTP"))) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, Compiler())
            pipeline = worker.media_pipeline = MediaPipeline(host, client)
            req = MediaRequest(prompt="a garden", backend="qwen-image", prompt_mode="direct")
            plan, call = await pipeline.prepare(req)
            job_id = jobs.create_media(req, plan, call); jobs.transition(job_id, "RUNNING")
            jobs.remote(job_id, "SUBMITTING", None); jobs.recover()
            assert jobs.get(job_id)["status"] == "RECOVERY_REQUIRED"
            assert jobs.get(job_id)["media_phase"] == "FAILED"
            with pytest.raises(ValueError): worker.retry_media(job_id)
            assert json.loads(jobs.get(job_id, private=True)["image_spec_json"]) == plan
            # Arbitrary prepared image tool calls still lack the worker capability.
            with pytest.raises(HarnessFailure): await host.run_tool(call)
            await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("origin", ["http://example.com", "http://localhost/x", "http://a:b@localhost", "https://localhost"])
def test_unreviewed_prompt_service_refused(origin):
    with pytest.raises(ValueError): MediaPipeline(None, None, origin)


def test_media_api_preview_response_contract(tmp_path):
    from fastapi import FastAPI
    from backend.app.media import router
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("preview must not render"))) as wire:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, wire, Compiler())
            worker.media_pipeline = MediaPipeline(host, wire)
            app = FastAPI(); app.include_router(router())
            app.state.worker = worker; app.state.jobs = jobs
            worker.start()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
                body = {"prompt": "a cat", "backend": "qwen-image", "prompt_mode": "direct", "preview_only": True}
                refused = await client.post("/api/media/jobs", json=body, headers={"Origin": "http://foreign.invalid"})
                assert refused.status_code == 403
                created = await client.post("/api/media/jobs", json=body)
                assert created.status_code == 202, created.text
                jid = created.json()["job_id"]
                await asyncio.wait_for(worker.queue.join(), 3)
                assert jobs.get(jid)["media_phase"] == "PROMPT_SAVED"
                saved = await client.get(f"/api/media/jobs/{jid}/plan")
                assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
                assert saved.json()["plan"]["positive_prompt"] == "a cat"
            await worker.close(); await gpu.close()
    asyncio.run(run())


def test_prompt_error_metadata_is_durable_without_private_body(tmp_path):
    async def run():
        def route(request):
            return httpx.Response(502,json={'detail':{'code':'strata_structured_output_failed',
                'field':'supporting_elements','message':'PRIVATE upstream body'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            host,worker,jobs,gpu,drivers,_=setup(tmp_path,client,Compiler())
            worker.media_pipeline=MediaPipeline(host,client,'http://localhost:19090');worker.start()
            job=worker.submit_media(MediaRequest(prompt='garden',backend='qwen-image'))
            await asyncio.wait_for(worker.queue.join(),3)
            events=jobs.media_events(job['job_id'])
            assert events[-1]['details']['prompt_details']=={'code':'strata_structured_output_failed',
                'field':'supporting_elements','status':502}
            assert 'PRIVATE' not in json.dumps(events)
            assert not drivers['image'].started
            await worker.close();await gpu.close()
    asyncio.run(run())
