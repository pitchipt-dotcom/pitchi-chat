"""Mock vision inference and owned lifecycle; no real model or GPU."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
import httpx
import pytest
from backend.app.tools.vision import VisionAdapter, VisionArguments, VisionOutput
from backend.app.tools.http import ServiceHTTP
from backend.app.harness.contracts import Contract, ToolResult, ExecutionResult, FinalStatus, ToolCall
from backend.app.harness.recovery import HarnessFailure
from backend.app.image_assets import ImageAssetStore
from backend.app.jobs.store import JobStore
from backend.app.media import MediaRequest, MediaPipeline
from backend.app.resources.process import OwnedProcess, ProcessProfile
from backend.app.resources.manager import GPUUnsafe, GPUReading
from backend.app.storage.sqlite import HistoryStore
from test_comfyui import png


@pytest.mark.parametrize('bad', [None, 'truncated', 'model', 'tools', 'reasoning'])
def test_local_vision_payload_and_response(tmp_path, bad):
    async def run():
        history = HistoryStore(tmp_path/'history.db');history.initialize();assets = ImageAssetStore(history,tmp_path/'images');cid=assets.create_conversation()
        image=assets.save(png(32,32),cid);reference=assets.reference(image['id'],cid);seen=[]
        def handle(r):
            payload=json.loads(r.content);seen.append(payload)
            assert payload['chat_template_kwargs']=={'enable_thinking':False}
            assert payload['max_tokens']==1024 and payload['stream'] is False
            parts=payload['messages'][1]['content'];assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
            message={'content':'赤い円が白い背景の中央にあります。'}
            if bad=='tools':message['tool_calls']=[{'name':'shell'}]
            if bad=='reasoning':message={'content':'<think>unfinished'}
            return httpx.Response(200,json={'model':'other' if bad=='model' else 'synthetic-vision',
                'choices':[{'finish_reason':'length' if bad=='truncated' else 'stop','message':message}]})
        class Driver:
            profile=SimpleNamespace(required_vram_mib=10240)
            owned_pids=frozenset()
            async def idle(self):return True
        class Manager:
            drivers={'vision':Driver()}
            ownership_policy=SimpleNamespace(running_reason=lambda *a:None)
            async def inspect(self):return GPUReading(0,frozenset(),12288)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter=VisionAdapter(ServiceHTTP(client,'http://localhost'),{'qwen35-vision':'synthetic-vision'});adapter.image_assets=assets;adapter.manager=Manager()
            with patch('backend.app.tools.vision.check_profile_memory',return_value={}),patch.object(adapter,'_oom',return_value=0):
                args=VisionArguments(question='何が見えますか',reference_image=reference)
                if bad:
                    with pytest.raises(HarnessFailure):await adapter.execute(args,'qwen35-vision')
                else:
                    result=await adapter.execute(args,'qwen35-vision');assert result.output['image_id']==image['id'] and result.output['model']=='synthetic-vision'
            assert len(seen)==1
    asyncio.run(run())


@pytest.mark.parametrize('slots', [[{'is_processing':False}],[{'is_processing':True}],[],[{}],{'bad':'value'}])
def test_slot_idle_proof_and_no_signal_if_busy(tmp_path, slots):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=slots))) as c:
            p=ProcessProfile('vision',('/synthetic/server',),tmp_path,'http://localhost/health',graceful_only=True)
            driver=OwnedProcess(p,c);driver.process=SimpleNamespace(returncode=None)
            expected=slots==[{'is_processing':False}]
            assert await driver.idle() is expected
            if not expected:
                with patch.object(driver,'_signal',side_effect=AssertionError('must not signal')):
                    with pytest.raises(GPUUnsafe):await driver.stop()
    asyncio.run(run())


def test_atomic_observation_history_and_job_commit(tmp_path):
    history=HistoryStore(tmp_path/'history.db');history.initialize();jobs=JobStore(history);assets=ImageAssetStore(history,tmp_path/'images');cid=assets.create_conversation();image=assets.save(png(32,32),cid)
    request=MediaRequest(kind='vision',prompt='何が見えますか',prompt_mode='direct',backend='qwen35-vision',conversation_id=UUID(cid),reference_image_id=UUID(image['id']))
    job=jobs.create_media(request);jobs.transition(job,'RUNNING')
    from uuid import uuid4
    output=VisionOutput(answer='赤い円です。',image_id=image['id'],image_sha256=image['sha256'],model='synthetic-vision')
    result=ExecutionResult(request_id=uuid4(),status=FinalStatus.SUCCESS,tool_result=ToolResult(success=True,result_type='structured',output=output.model_dump(),http_status=200))
    jobs.transition(job,'SUCCESS',result=result)
    assert [r['content'] for r in history.history(cid)]==['何が見えますか','赤い円です。']
    with history.connection() as db:assert db.execute('select image_id from image_observations').fetchone()[0]==image['id']
    with pytest.raises(ValueError):jobs.transition(job,'SUCCESS',result=result)
    assert len(history.history(cid))==2


def test_foreign_endpoint_never_adopted_or_signalled(tmp_path):
    async def run():
        calls=[]
        def response(request):
            calls.append(request.url.path)
            return httpx.Response(404,json={'detail':'foreign endpoint'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            p=ProcessProfile('vision',('/synthetic/server',),tmp_path,'http://localhost/health',graceful_only=True)
            driver=OwnedProcess(p,c)
            with pytest.raises(GPUUnsafe,match='endpoint_already_owned'):await driver.start()
            assert driver.process is None and not driver.owned_pids
            assert await driver.idle() is True
            with patch.object(driver,'_signal',side_effect=AssertionError('must not signal')):await driver.stop()
            assert calls==['/health']
    asyncio.run(run())
