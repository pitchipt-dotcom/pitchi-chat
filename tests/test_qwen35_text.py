"""Synthetic transports/config only. Real SHA calls are recorded separately."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from backend.app.config import ModelSettings, load_settings
from backend.app.models.base import Message, ModelError
from backend.app.models.qwen35 import Qwen35Adapter
from backend.app.models.registry import ModelRegistry
from backend.app.main import create_app
from backend.app.resources.config import ResourceSettings
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.providers.config import CloudSettings
from backend.app.tools.work_config import WorkSettings
from test_model_registry import config_file


@pytest.mark.parametrize('failure', [None, 'length', 'tool', 'model', 'reasoning'])
def test_qwen35_text_payload_and_terminal_validation(settings, failure):
    def handle(request):
        p=json.loads(request.content)
        assert p['max_tokens']==3072 and not p['stream']
        assert p['chat_template_kwargs']=={'enable_thinking':False}
        assert p['response_format']['type']=='json_schema'
        assert all(isinstance(x['content'],str) for x in p['messages'])
        message={'content':'{"prompt":"Keep the subject."}'}
        if failure=='tool':message['tool_calls']=[{'name':'shell'}]
        if failure=='reasoning':message['content']='<think>unfinished'
        return httpx.Response(200,json={'model':'other' if failure=='model' else 'synthetic-qwen35',
            'choices':[{'finish_reason':'length' if failure=='length' else 'stop','message':message}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter=Qwen35Adapter(replace(settings,endpoint='http://localhost/v1/chat/completions',model_name='synthetic-qwen35'),client)
            with patch('backend.app.models.qwen35.check_stream_memory'),patch.object(adapter,'_oom',return_value=0):
                if failure:
                    with pytest.raises(ModelError):await adapter.generate_structured([Message('user','edit')],{'type':'object'})
                else:assert 'Keep the subject' in await adapter.generate_structured([Message('user','edit')],{'type':'object'})
    asyncio.run(run())


def test_qwen35_registry_config_and_owned_availability(tmp_path):
    settings=load_settings(config_file(tmp_path,'[models.vision]\nadapter="qwen"\nendpoint="http://localhost/v1/chat/completions"\nname="synthetic-qwen35"\n'))
    async def run():
        async with httpx.AsyncClient() as client:
            registry=ModelRegistry(settings,client,managed_profiles=frozenset({'vision'}))
            assert isinstance(registry.resolve('vision').adapter,Qwen35Adapter)
            entry=next(x for x in registry.public_state()['models'] if x['id']=='vision')
            assert entry['available'] and entry['label']=='Qwen3.5-9B-Uncensored'
            assert registry.default_model=='qwen'
    asyncio.run(run())


def test_durable_qwen35_chat_finishes_without_subscriber(settings):
    class Adapter:
        async def generate(self,messages):return '通常の日本語回答です。'
    async def run():
        app=create_app(replace(settings,models=(ModelSettings('vision','qwen','','synthetic-qwen35','',2),)),
            adapters={'vision':Adapter()},services=(),resource_settings=ResourceSettings(),
            image_workflows=ImageWorkflowRegistry(),cloud_settings=CloudSettings(),work_settings=WorkSettings())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.post('/api/chat/stream',json={'model':'vision','message':'こんにちは','client_request_id':str(uuid4())})
                assert response.status_code==200,response.text
                job=response.json()
                for _ in range(100):
                    state=(await client.get('/api/chat/stream/jobs/'+job['job_id'])).json()
                    if state['status']=='COMPLETED':break
                    await asyncio.sleep(.01)
                assert state['status']=='COMPLETED',state
                assert state['model']=='vision'
                assert state['answer']['model_selection']['actual_model_used']=='vision'
                assert state['answer']['reply']=='通常の日本語回答です。'
    asyncio.run(run())


def test_qwen35_prompt_plan_reuses_existing_compiler_and_backend():
    from types import SimpleNamespace
    from backend.app.media import MediaRequest, MediaPipeline
    from test_comfyui import backend, spec
    calls=[]
    async def compile_image(request,persist):
        assert request.model=='vision' and request.compile_only
        calls.append(request)
        return spec(),None
    definition=SimpleNamespace(adapter=SimpleNamespace(workflows=SimpleNamespace(get=lambda _:backend()),generation_backends={'qwen-image'}))
    host=SimpleNamespace(preflight_image=lambda request:None,registry=SimpleNamespace(get=lambda _:definition),
                         compile_image=compile_image,prepare_tool=lambda call:None)
    async def run():
        async with httpx.AsyncClient() as client:
            pipeline=MediaPipeline(host,client)
            plan,call=await pipeline.prepare(MediaRequest(prompt='猫の画像',backend='qwen-image',prompt_mode='qwen35',preview_only=True))
            assert plan['prompt_source']=='qwen35' and 'cat' in plan['positive_prompt']
            assert call.tool=='image.generate' and len(calls)==1
    asyncio.run(run())


def test_qwen35_edit_rewrite_is_text_only_and_explicit(settings):
    from backend.app.harness.audit import AuditTrail
    from backend.app.harness.orchestrator import Harness
    from backend.app.resources.gpu import ResourceCoordinator
    from backend.app.storage.sqlite import HistoryStore
    class Adapter:
        async def generate_structured(self,messages,schema):
            assert 'CANNOT see' in messages[0].content
            assert messages[-1].content=='背景だけ青く'
            return '{"prompt":"Change only the background to blue."}'
    async def run():
        store=HistoryStore(settings.database);store.initialize()
        async with httpx.AsyncClient() as client:
            registry=ModelRegistry(settings,client,{'vision':Adapter()})
            host=Harness(None,ResourceCoordinator(),AuditTrail(store),models=registry)
            assert await host.compile_image_edit('背景だけ青く',model_id='vision')=='Change only the background to blue.'
    asyncio.run(run())


def test_cancel_retains_resource_until_http_terminal_and_logs_only_metadata(settings, caplog):
    from backend.app.models.qwen35 import trace_request, trace_job
    from backend.app.resources.gpu import ResourceCoordinator
    from backend.app.harness.contracts import ResourceMode
    from backend.app.harness.recovery import bounded_execute, RecoveryPolicy
    caplog.set_level('INFO',logger='uvicorn.error')
    async def run():
        entered=asyncio.Event();finish=asyncio.Event();upstream_cancelled=[];resources=ResourceCoordinator()
        async def handle(request):
            entered.set()
            try:await finish.wait()
            except asyncio.CancelledError:upstream_cancelled.append(True);raise
            return httpx.Response(200,json={'model':'synthetic-qwen35','choices':[{'finish_reason':'stop','message':{'content':'SYNTHETIC_PRIVATE_ANSWER'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter=Qwen35Adapter(replace(settings,endpoint='http://localhost/chat',model_name='synthetic-qwen35'),client)
            async def operation():
                async with resources.job(ResourceMode.CHAT,model='vision'):
                    return await adapter.generate([Message('user','SYNTHETIC_PRIVATE_INPUT')])
            with patch('backend.app.models.qwen35.check_stream_memory'),patch.object(adapter,'_oom',return_value=0),trace_request('synthetic-request-id'),trace_job('synthetic-job-id'):
                task=asyncio.create_task(bounded_execute(operation,RecoveryPolicy(timeout_seconds=2),lambda:None))
                await entered.wait();task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
                await asyncio.sleep(.01)
                assert resources.snapshot()['active_job'] is not None
                assert not upstream_cancelled
                next_started=asyncio.Event()
                async def next_request():
                    async with resources.job(ResourceMode.CHAT,model='vision'):next_started.set()
                queued=asyncio.create_task(next_request());await asyncio.sleep(.01);assert not next_started.is_set()
                finish.set();await asyncio.wait_for(queued,1)
                assert resources.snapshot()['active_job'] is None
        assert not upstream_cancelled
    asyncio.run(run())
    assert 'cancel_drain_started' in caplog.text and 'cancel_drain_complete' in caplog.text
    assert 'synthetic-request-id' in caplog.text and 'synthetic-job-id' in caplog.text
    assert 'SYNTHETIC_PRIVATE' not in caplog.text


def test_qwen_http_deadline_is_finite_and_diagnostic(settings, caplog):
    from backend.app.models.base import ModelTimeout
    caplog.set_level('INFO',logger='uvicorn.error')
    async def run():
        async def handle(request):await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter=Qwen35Adapter(replace(settings,endpoint='http://localhost/chat',timeout_seconds=.03),client)
            with patch('backend.app.models.qwen35.check_stream_memory'),patch.object(adapter,'_oom',return_value=0):
                with pytest.raises(ModelTimeout):await asyncio.wait_for(adapter.generate([Message('user','synthetic')]),1)
    asyncio.run(run())
    assert '"cause": "timeout"' in caplog.text and 'ModelTimeout' in caplog.text



def test_engine_diagnostics_are_bounded_numeric_and_no_raw_content(caplog):
    from backend.app.models.qwen35_logs import EngineDiagnostics
    caplog.set_level('INFO',logger='uvicorn.error')
    log=EngineDiagnostics(123)
    log.feed(b'SYNTHETIC_PRIVATE_PROMPT\nslot print_timing: prompt eval ti')
    log.feed(b'me = 10.00 ms / 31 tokens (0.32 ms per token)\n')
    log.feed(b'slot release: stop processing: n_tokens = 42, truncated = 0\n')
    log.feed(b'SYNTHETIC_PRIVATE_ANSWER\n'+b'x'*9000)
    assert log.pending==''
    assert 'SYNTHETIC_PRIVATE' not in caplog.text
    assert '"tokens": 31' in caplog.text and '"kind": "slot_stop"' in caplog.text
