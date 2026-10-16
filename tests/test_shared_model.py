"""ASGI and synthetic upstream tests; no real model inference or services."""
import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from backend.app.main import create_app
from backend.app.config import ModelSettings
from backend.app.models.response import ModelResponse
from backend.app.resources.config import ResourceSettings
from backend.app.resources.manager import GPUPhase
from tests.test_model_usage import Inspector, Driver


class Adapter:
    async def generate_chat(self, messages, options=None, on_delta=None, **kwargs):
        if on_delta: await on_delta("synthetic answer")
        return ModelResponse(final_content="synthetic answer", metadata={"elapsed_seconds": .01, "finish_reason": "stop"})


def app_for(settings):
    spec = ModelSettings("strata", "strata", "http://127.0.0.1:18765/v1/chat/completions", "synthetic-model", "", 2)
    return create_app(replace(settings, models=(spec,)), adapters={"strata": Adapter()},
                      services=(), resource_settings=ResourceSettings())


def configure(app):
    inspector = Inspector()
    driver = Driver(inspector, 10, True)
    app.state.gpu_manager.inspector = inspector
    app.state.gpu_manager.drivers = {"strata": driver}
    return driver


def test_on_demand_chat_does_not_start_at_host_boot_or_catalog(settings):
    async def run():
        app = app_for(settings)
        async with app.router.lifespan_context(app):
            driver = configure(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                await client.get("/api/models")
                assert driver.events == []
                result = await client.post("/api/chat", json={"model": "strata", "message": "hello"})
                assert result.status_code == 200 and result.json()["reply"] == "synthetic answer"
                assert driver.events == ["start"]
                assert app.state.gpu_manager.usage_count() == 0
    asyncio.run(run())


def test_queued_mobile_jobs_survive_without_subscribers_and_cancel_releases_usage(settings):
    async def run():
        app = app_for(settings)
        async with app.router.lifespan_context(app):
            driver = configure(app)
            manager = app.state.gpu_manager
            await app.state.turn_lock.acquire()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                key1, key2 = str(uuid4()), str(uuid4())
                first = (await client.post("/api/chat/stream", json={"model":"strata", "message":"one", "client_request_id":key1})).json()
                second = (await client.post("/api/chat/stream", json={"model":"strata", "message":"two", "client_request_id":key2})).json()
                await asyncio.sleep(.01)
                assert manager.usage_count() == 2
                status = (await client.get(f'/api/chat/stream/jobs/{second["job_id"]}')).json()
                assert status["status"] == "QUEUED"
                cancelled = await client.post(f'/api/chat/stream/jobs/{second["job_id"]}/cancel')
                assert cancelled.json()["status"] == "CANCELLED"
                assert manager.usage_count() == 1
                app.state.turn_lock.release()
                for _ in range(100):
                    row = (await client.get(f'/api/chat/stream/by-client/{key1}')).json()
                    if row["status"] == "COMPLETED": break
                    await asyncio.sleep(.005)
                assert row["status"] == "COMPLETED" and row["answer"]["reply"] == "synthetic answer"
                assert manager.usage_count() == 0 and driver.events == ["start"]
                cid = row["conversation_id"]
                assert len((await client.get(f'/api/conversations/{cid}')).json()["messages"]) == 2
    asyncio.run(run())


def test_shared_job_auth_usage_stream_and_fixed_endpoint(settings, monkeypatch):
    token = "synthetic-test-only-job-token-0123456789"
    monkeypatch.setenv("PITCH_CHAT_SHARED_MODEL_TOKEN", token)
    calls = []
    def respond(request):
        assert str(request.url) == "http://127.0.0.1:18765/v1/chat/completions"
        body = json.loads(request.content); calls.append(body)
        assert body["stream"] is False and body["reasoning_effort"] == "none"
        return httpx.Response(200, json={"model":"synthetic-model", "choices":[
            {"message":{"role":"assistant","content":'{"ok":true}'},"finish_reason":"stop"}],
            "usage":{"completion_tokens":4}})
    async def run():
        app = app_for(settings)
        async with app.router.lifespan_context(app):
            driver = configure(app)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
                app.state.shared_model_client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                    assert (await client.post("/api/models/strata/uses")).status_code == 403
                    headers = {"Authorization":"Bearer " + token}
                    lease = (await client.post("/api/models/strata/uses", headers=headers)).json()["use_id"]
                    assert driver.events == []
                    assert (await client.post(f"/api/models/strata/uses/{lease}/renew",headers=headers)).status_code == 200
                    body = {"model":"synthetic-model","messages":[{"role":"user","content":"JSON"}],"stream":True}
                    result = await client.post("/api/models/strata/v1/chat/completions", json=body, headers=headers)
                    assert result.status_code == 200 and "[DONE]" in result.text
                    assert '"finish_reason": "stop"' in result.text
                    assert app.state.gpu_manager.usage_count() == 1
                    assert driver.events == ["start"]
                    await client.delete(f"/api/models/strata/uses/{lease}",headers=headers)
                    assert app.state.gpu_manager.usage_count() == 0
                    bad = await client.post("/api/models/strata/v1/chat/completions",json=body|{"model":"other"},headers=headers)
                    assert bad.status_code == 422 and len(calls) == 1
                    bad = await client.post("/api/models/strata/v1/chat/completions",json=body|{"tools":[]},headers=headers)
                    assert bad.status_code == 422 and len(calls) == 1
                    assert (await client.get("/api/models/strata/v1/models",headers=headers)).json()["data"][0]["id"] == "synthetic-model"
    asyncio.run(run())


def test_shared_job_gateway_disabled_by_default(settings, monkeypatch):
    monkeypatch.delenv("PITCH_CHAT_SHARED_MODEL_TOKEN", raising=False)
    async def run():
        app = app_for(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                assert (await client.post("/api/models/strata/uses")).status_code == 503
    asyncio.run(run())


def test_conversation_queue_timeout_releases_usage_without_start(settings):
    async def run():
        app=app_for(settings)
        async with app.router.lifespan_context(app):
            driver=configure(app)
            app.state.gpu_manager.queue_timeout=.01
            await app.state.turn_lock.acquire()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
                    result=await client.post('/api/chat',json={"model":"strata","message":"queued"})
                    assert result.status_code==504
                assert app.state.gpu_manager.usage_count()==0 and not driver.events
            finally:app.state.turn_lock.release()
    asyncio.run(run())


def test_upstream_schema_failure_preserves_only_safe_metadata(settings, monkeypatch):
    token = 'synthetic-test-only-job-token-0123456789'
    monkeypatch.setenv('PITCH_CHAT_SHARED_MODEL_TOKEN', token)
    async def run():
        app = app_for(settings)
        async with app.router.lifespan_context(app):
            configure(app)
            def response(request):
                return httpx.Response(502, json={'error': {'code': 'structured_output_failed',
                    'message': 'model output failed the JSON Schema at /supporting_elements: PRIVATE contents'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as upstream:
                app.state.shared_model_client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                    result = await client.post('/api/models/strata/v1/chat/completions',
                        headers={'Authorization':'Bearer '+token}, json={'model':'synthetic-model',
                        'messages':[{'role':'user','content':'JSON'}]})
                    assert result.status_code == 502
                    assert result.json()['detail'] == {'code':'strata_structured_output_failed',
                        'upstream_status':502, 'field':'supporting_elements'}
                    assert 'PRIVATE' not in result.text
                    assert app.state.gpu_manager.usage_count() == 0
    asyncio.run(run())
