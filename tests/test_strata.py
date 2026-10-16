"""Synthetic Strata SSE/API tests; these do not load a model or stop services."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from backend.app.config import ModelSettings, load_settings
from backend.app.main import create_app
from backend.app.models.base import Message, ModelError, ModelNotConfigured
from backend.app.models.strata import StrataAdapter, StrataOptions
from backend.app.resources.memory import memory_state, check_memory
from backend.app.resources.manager import GPUUnsafe
from backend.app.resources.config import ResourceSettings, load_resources


def sse(chunks, done=True):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")


def chunk(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def adapter(settings, transport):
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    return StrataAdapter(replace(settings, endpoint="http://127.0.0.1:18765/v1/chat/completions"), client)


def test_stream_separates_reasoning_and_split_tags(settings):
    payloads, deltas = [], []
    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, text=sse([
            chunk({"reasoning_content": "synthetic hidden thought"}),
            chunk({"content": "<thi"}), chunk({"content": "nk>internal</think>日本"}),
            chunk({"content": "語です"}, "stop"), {"usage": {"completion_tokens": 5}},
        ]))
    async def run():
        a = adapter(settings, respond)
        async def emit(text): deltas.append(text)
        try:
            response = await a.generate_chat([Message("user", "挨拶"), Message("assistant", "以前の回答"),
                                              Message("user", "続けて")], on_delta=emit)
            assert response.final_content == "日本語です"
            assert "synthetic" in response.reasoning_content
            assert "internal" not in "".join(deltas)
            assert "".join(deltas) == "日本語です"
            assert response.metadata["completion_tokens"] == 5
            assert response.metadata["time_to_first_token_seconds"] >= 0
            p = payloads[0]
            assert p["reasoning_effort"] == "none" and p["reasoning_budget_tokens"] == 0
            assert p["messages"][-2] == {"role": "assistant", "content": "以前の回答"}
            assert p["stream"] is True and "tools" not in p
        finally: await a.client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    sse([chunk({"reasoning_content": "hidden"}, "length")]),
    sse([chunk({"content": "<think>unfinished"}, "length")]),
    sse([chunk({"content": "partial"}, "stop")], done=False),
    sse([chunk({"tool_calls": [{"function": {"name": "unsafe"}}]}, "tool_calls")]),
    sse([chunk({"content": "answer"}, "cancel")]),
])
def test_incomplete_and_tool_streams_fail_closed(settings, body):
    async def run():
        a = adapter(settings, lambda r: httpx.Response(200, text=body))
        try:
            with pytest.raises(ModelError): await a.generate_chat([Message("user", "test")])
        finally: await a.client.aclose()
    asyncio.run(run())


def test_stopped_service_is_safe_error(settings):
    def fail(request): raise httpx.ConnectError("synthetic private endpoint", request=request)
    async def run():
        a = adapter(settings, fail)
        try:
            with pytest.raises(ModelNotConfigured) as error: await a.generate_chat([Message("user", "test")])
            assert "停止中" in str(error.value) and "private" not in str(error.value)
        finally: await a.client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('valid',[True,False])
def test_structured_response_is_independently_schema_validated(settings,valid):
    async def run():
        content='{"count":3}' if valid else '{"count":true}'
        a=adapter(settings,lambda r:httpx.Response(200,json={
            'model':settings.model_name,'choices':[{'message':{'content':content},'finish_reason':'stop'}]}))
        schema={'type':'object','properties':{'count':{'type':'integer'}},'required':['count'],'additionalProperties':False}
        try:
            if valid:assert (await a.generate_structured_response([Message('user','JSON')],schema)).final_content==content
            else:
                with pytest.raises(ModelError):await a.generate_structured_response([Message('user','JSON')],schema)
        finally:await a.client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("options", [
    {"temperature": float("nan")}, {"temperature": 3.0}, {"max_tokens": True},
    {"reasoning_effort": "unknown"}, {"reasoning_effort": "high", "max_tokens": 512},
    {"reasoning_effort": "low", "reasoning_budget_tokens": 0}, {"endpoint": "untrusted"},
])
def test_invalid_options_rejected(options):
    with pytest.raises(ValidationError): StrataOptions.model_validate(options)


def test_low_effort_is_forwarded_with_answer_reserve(settings):
    async def run():
        def respond(r):
            p = json.loads(r.content)
            assert p["reasoning_effort"] == "low" and p["reasoning_budget_tokens"] == 512
            assert p["max_tokens"] == 2048 and p["temperature"] == 0.5
            return httpx.Response(200, text=sse([chunk({"content": "答え"}, "stop")]))
        a = adapter(settings, respond)
        try: await a.generate_chat([Message("user", "test")], {"temperature": 0.5, "max_tokens": 2048, "reasoning_effort": "low"})
        finally: await a.client.aclose()
    asyncio.run(run())


def test_strata_config_is_additive_and_requires_safety_profile(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text('[storage]\ndatabase="history.db"\n[models.strata]\nendpoint="http://127.0.0.1:18765/v1/chat/completions"\nname="synthetic-strata"\n')
    settings = load_settings(cfg)
    assert settings.default_model == "qwen" and settings.models[-1].id == "strata"
    resource = tmp_path / "resources.toml"
    resource.write_text('[gpu]\ninspect=true\n[profiles.strata]\nargv=["/synthetic/python"]\ncwd="/synthetic"\nhealth_url="http://127.0.0.1:18765/health"\n')
    with pytest.raises(ValueError, match="Strata requires"): load_resources(resource)


def test_memory_reader_and_low_memory_refusal(tmp_path, monkeypatch):
    path = tmp_path / "meminfo"
    path.write_text('MemAvailable: 2097152 kB\nSwapFree: 131072 kB\nSwapTotal: 8388608 kB\n')
    state = memory_state(path)
    assert state == {"ram_available_mib": 2048, "swap_free_mib": 128, "swap_used_mib": 8064}
    monkeypatch.setattr('backend.app.resources.memory.memory_state', lambda: state)
    with pytest.raises(GPUUnsafe, match="insufficient_ram"): check_memory(40960, 512)
    with pytest.raises(GPUUnsafe, match="insufficient_swap"): check_memory(1024, 512)


def test_api_stream_multiturn_evaluation_and_failed_turn_not_saved(settings):
    calls = []
    def respond(request):
        p = json.loads(request.content); calls.append(p)
        if p['messages'][-1]['content'] == '不正応答':
            return httpx.Response(200, text=sse([chunk({"content": "<think>unclosed"}, "length")]))
        return httpx.Response(200, text=sse([chunk({"content": "日本語"}), chunk({"content": "の回答"}, "stop"),
                                           {"usage": {"completion_tokens": 6}}]))
    async def run():
        a = adapter(settings, respond)
        spec = ModelSettings('strata', 'strata', a.settings.endpoint, 'synthetic-strata', '', 5)
        app = create_app(replace(settings, models=(spec,)), adapters={'strata': a}, services=(), resource_settings=ResourceSettings())
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    request_id = str(uuid4())
                    first = await client.post('/api/chat/stream', json={'model':'strata','message':'覚えて','client_request_id':request_id})
                    assert first.status_code == 200 and first.headers['cache-control'] == 'no-store'
                    accepted = first.json(); assert accepted['status'] in {'QUEUED','RUNNING'}
                    duplicate = await client.post('/api/chat/stream', json={'model':'strata','message':'覚えて','client_request_id':request_id})
                    assert duplicate.json()['job_id'] == accepted['job_id'], 'retry must attach to the same generation'
                    for _ in range(50):
                        state = (await client.get(f"/api/chat/stream/jobs/{accepted['job_id']}")).json()
                        if state['status'] in {'COMPLETED','ERROR'}: break
                        await asyncio.sleep(.01)
                    events = await client.get(f"/api/chat/stream/jobs/{accepted['job_id']}/events")
                    assert 'event: snapshot' in events.text, events.text
                    assert 'event: done\ndata: ' in events.text, events.text
                    event = events.text.split('event: done\ndata: ')[1].split('\n\n')[0]
                    data = json.loads(event); cid=data['conversation_id']
                    assert data['reply']=='日本語の回答' and data['generation_metrics']['completion_tokens']==6
                    first_history = (await client.get(f'/api/conversations/{cid}')).json()['messages']
                    assert [(row['role'],row['content']) for row in first_history] == [
                        ('user','覚えて'), ('assistant','日本語の回答')]
                    completed_retry = await client.post('/api/chat/stream', json={'model':'strata','message':'覚えて','client_request_id':request_id})
                    assert completed_retry.json()['job_id'] == accepted['job_id']
                    assert len([call for call in calls if call['messages'][-1]['content'] == '覚えて']) == 1
                    conflict = await client.post('/api/chat/stream', json={'model':'strata','message':'別の内容','client_request_id':request_id})
                    assert conflict.status_code == 409, 'a request ID remains bound to its original payload after completion'
                    second = await client.post('/api/chat', json={'model':'strata','message':'続けて','conversation_id':cid})
                    assert second.status_code==200
                    assert [m['role'] for m in calls[-1]['messages']]==['system','user','assistant','user']
                    assert calls[-1]['messages'][2]['content']=='日本語の回答'
                    failed_start = await client.post('/api/chat/stream', json={'model':'strata','message':'不正応答','conversation_id':cid,
                        'client_request_id':str(uuid4())})
                    failed_id = failed_start.json()['job_id']
                    for _ in range(50):
                        state = (await client.get(f"/api/chat/stream/jobs/{failed_id}")).json()
                        if state['status'] in {'COMPLETED','ERROR'}: break
                        await asyncio.sleep(.01)
                    failed_events = await client.get(f"/api/chat/stream/jobs/{failed_id}/events")
                    assert 'event: error' in failed_events.text and 'event: done' not in failed_events.text
                    history=(await client.get('/api/conversations/'+cid)).json()
                    assert len(history['messages'])==4 and 'unclosed' not in json.dumps(history)
                    review=await client.post('/api/conversations/'+cid+'/evaluation',json={'category':'context','score':4,'note':'synthetic test'})
                    assert review.status_code==200 and len(review.json()['metrics'])==2
                    assert review.json()['ratings'][0]['score']==4
                    assert (await client.post('/api/chat/stream',json={'model':'qwen','message':'test','client_request_id':str(uuid4())})).status_code==422
                    assert (await client.post('/api/chat',json={'model':'strata','mode':'library','message':'test'})).status_code==422
        finally: await a.client.aclose()
    asyncio.run(run())


def test_strata_generation_survives_unsubscribed_client_and_can_resume_or_cancel(settings):
    calls = []
    stopped = asyncio.Event()
    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = json.dumps(chunk({"content": "途中の回答"})).encode()
            yield b"data: " + data + b"\n\n"
            await asyncio.Event().wait()
        async def aclose(self):
            stopped.set()
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, stream=PausedStream())

    async def run():
        a = adapter(settings, respond)
        spec = ModelSettings('strata', 'strata', a.settings.endpoint, 'synthetic-strata', '', 5)
        app = create_app(replace(settings, models=(spec,)), adapters={'strata': a}, services=(), resource_settings=ResourceSettings())
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    client_id = str(uuid4())
                    accepted = await client.post('/api/chat/stream', json={'model':'strata','message':'長い回答を続けて','client_request_id':client_id})
                    job_id = accepted.json()["job_id"]
                    # No event subscriber is attached here, matching a sleeping/backgrounded browser.
                    partial = None
                    for _ in range(100):
                        partial = (await client.get(f'/api/chat/stream/jobs/{job_id}')).json()
                        if partial['reply']:
                            break
                        await asyncio.sleep(.02)
                    assert partial['reply'] == '途中の回答'
                    assert partial['status'] == 'RUNNING'
                    resumed = await client.get(f'/api/chat/stream/by-client/{client_id}')
                    assert resumed.json()['job_id'] == job_id
                    assert resumed.json()['message'] == '長い回答を続けて'
                    cancelled = await client.post(f'/api/chat/stream/jobs/{job_id}/cancel')
                    assert cancelled.json()['status'] == 'CANCELLED'
                    await asyncio.wait_for(stopped.wait(), 2)
                    assert len(calls) == 1, 'resume/cancel must not replay inference'
                    assert (await client.get(f"/api/conversations/{partial['conversation_id']}")).status_code == 404
                    assert 'event: error' in (await client.get(f'/api/chat/stream/jobs/{job_id}/events')).text
        finally:
            await a.client.aclose()
    asyncio.run(run())


def test_manual_start_never_stops_another_owned_model(tmp_path):
    from backend.app.resources.manager import GPUManager, GPUReading, GPUPhase
    from backend.app.resources.process import ProcessProfile
    from backend.app.storage.sqlite import HistoryStore
    class Inspector:
        async def read(self): return GPUReading(0, frozenset())
    class Driver:
        def __init__(self, name, manual=False):
            self.profile = ProcessProfile(name, ("/synthetic/python",), tmp_path, "http://localhost/health", manual_start=manual)
            self.owned_pids = frozenset(); self.events=[]
        async def start(self): self.events.append("start")
        async def stop(self): self.events.append("stop")
    async def run():
        store=HistoryStore(tmp_path/"manual.db"); store.initialize()
        drivers={"strata":Driver("strata",True),"other":Driver("other")}
        manager=GPUManager(store,Inspector(),drivers); manager.open()
        try:
            with pytest.raises(GPUUnsafe,match="manual_start_required"):
                async with manager.lease("strata","test"): pass
            assert not drivers["strata"].events
            # Simulate a queued explicit start racing with another model load.
            await manager._lock.acquire()
            async def enter():
                async with manager.lease("strata","test",allow_start=True): pass
            task=asyncio.create_task(enter()); await asyncio.sleep(0)
            manager.loaded="other"; manager._state(GPUPhase.READY,"other")
            manager._lock.release()
            with pytest.raises(GPUUnsafe,match="other_model_loaded"): await task
            assert manager.loaded=="other" and not drivers["other"].events
        finally:
            manager.loaded=None; manager._state(GPUPhase.STOPPED); await manager.close()
    asyncio.run(run())


def test_graceful_only_stop_never_force_kills(tmp_path):
    from backend.app.resources.process import OwnedProcess, ProcessProfile
    import signal
    class Process:
        async def wait(self): await asyncio.sleep(1)
    async def run():
        async with httpx.AsyncClient() as client:
            profile=ProcessProfile("strata",("/synthetic/python",),tmp_path,"http://localhost/health",stop_timeout=0.01,graceful_only=True)
            driver=OwnedProcess(profile,client); driver.process=Process()
            signals=[]; driver._signal=signals.append
            with pytest.raises(GPUUnsafe,match="graceful_stop_unconfirmed"): await driver.stop()
            assert signals==[signal.SIGINT]
    asyncio.run(run())


def test_selection_does_not_start_and_unsafe_on_demand_send_is_refused(settings, tmp_path, monkeypatch):
    from backend.app.resources.process import ProcessProfile, OwnedProcess
    async def forbidden_start(self): pytest.fail("No implicit model startup is allowed")
    monkeypatch.setattr(OwnedProcess, "start", forbidden_start)
    from backend.app.resources.manager import GPUManager
    async def unsafe_preflight(self, owner): raise GPUUnsafe("insufficient_swap_reserve")
    monkeypatch.setattr(GPUManager, "preflight", unsafe_preflight)
    profile=ProcessProfile("strata",("/synthetic/python",),tmp_path,"http://127.0.0.1:18765/health",
        required_vram_mib=10500, required_ram_mib=43008, min_swap_free_mib=1024,
        runtime_ram_reserve_mib=5120, manual_start=True, graceful_only=True)
    spec=ModelSettings("strata","strata","http://127.0.0.1:18765/v1/chat/completions","synthetic-strata","",5)
    app=create_app(replace(settings,models=(spec,)),services=(),resource_settings=ResourceSettings(inspect_gpu=True,profiles=(profile,)))
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
                models=(await client.get("/api/models")).json()
                assert next(m for m in models["models"] if m["id"]=="strata")["available"]
                result=await client.post("/api/chat",json={"model":"strata","message":"こんにちは"})
                assert result.status_code==502
                assert app.state.gpu_manager.loaded is None
                assert (await client.get("/api/conversations")).json()["conversations"]==[]
    asyncio.run(run())


def test_preflight_shows_vram_even_when_swap_admission_fails(settings, tmp_path, monkeypatch):
    from backend.app.resources.process import ProcessProfile
    from backend.app.resources.manager import GPUManager, GPUReading
    async def inspect(manager):
        manager.reading = GPUReading(1000, frozenset(), 12288)
        return manager.reading
    async def preflight(manager, owner):
        raise GPUUnsafe("insufficient_swap_reserve")
    monkeypatch.setattr(GPUManager, "inspect", inspect)
    monkeypatch.setattr(GPUManager, "preflight", preflight)
    profile = ProcessProfile("strata", ("/synthetic/python",), tmp_path,
        "http://127.0.0.1:18765/health", required_ram_mib=43008, min_swap_free_mib=1024)
    app = create_app(settings, services=(), resource_settings=ResourceSettings(inspect_gpu=True, profiles=(profile,)))
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/models/strata/preflight")
                data = response.json()
                assert data["reason"] == "insufficient_swap_reserve" and not data["ready_to_start"]
                assert data["vram_free_mib"] == 11288
                assert response.headers["cache-control"] == "no-store"
                assert app.state.gpu_manager.loaded is None
    asyncio.run(run())


def test_memory_refusal_is_explained_without_spawning_or_losing_cause(settings, tmp_path, monkeypatch):
    from backend.app.resources.process import ProcessProfile, OwnedProcess
    from backend.app.resources.manager import GPUManager, GPUReading
    async def forbidden_start(self): pytest.fail('Memory gate must reject before spawning')
    async def inspect(manager):
        manager.reading = GPUReading(392, frozenset(), 12288)
        return manager.reading
    monkeypatch.setattr(OwnedProcess, 'start', forbidden_start)
    monkeypatch.setattr(GPUManager, 'inspect', inspect)
    monkeypatch.setattr('backend.app.resources.memory.memory_state', lambda: {
        'ram_available_mib':52900, 'swap_free_mib':301, 'swap_used_mib':7891})
    profile=ProcessProfile('strata',('/synthetic/python',),tmp_path,'http://127.0.0.1:18765/health',
        required_ram_mib=43008,min_swap_free_mib=1024,manual_start=True)
    spec=ModelSettings('strata','strata','http://127.0.0.1:18765/v1/chat/completions','synthetic-strata','',5)
    app=create_app(replace(settings,models=(spec,)),services=(),resource_settings=ResourceSettings(inspect_gpu=True,profiles=(profile,)))
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.post('/api/models/strata/lifecycle',json={'action':'start'})
                assert response.status_code==409
                detail=response.json()['detail']
                assert detail['resource']['stage']=='preflight'
                assert detail['resource']['reason']=='insufficient_swap_reserve'
                assert '301 MiB' in detail['message'] and '1024 MiB' in detail['message']
                response=await client.post('/api/chat',json={'model':'strata','message':'synthetic'})
                assert response.status_code==409
                assert response.json()['detail']['resource']['stage']=='preflight'
                assert app.state.gpu_manager.loaded is None
                assert (await client.get('/api/conversations')).json()['conversations']==[]
    asyncio.run(run())
