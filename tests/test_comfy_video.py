"""Synthetic video contracts; real MP4/GPU checks are recorded separately."""
import asyncio
import hashlib
import json
from pathlib import Path
from dataclasses import replace

import httpx
import pytest

from backend.app.media import MediaPipeline, MediaRequest
from backend.app.tools.comfy_video import load_video_workflows
from backend.app.harness.policy import TurnPolicy
from test_image_jobs import setup, Compiler


def configure(tmp_path, monkeypatch):
    data = (Path(__file__).parent/'fixtures/media_video_workflow.json').read_bytes()
    source = tmp_path/'workflow.json'; source.write_bytes(data)
    config = tmp_path/'video.json'; config.write_text(json.dumps({'workflow_path':str(source),
        'workflow_sha256':hashlib.sha256(data).hexdigest()}))
    monkeypatch.setenv('PITCH_CHAT_VIDEO_WORKFLOW_CONFIG',str(config))
    return source


def test_pinned_workflow_read_only_and_deterministic_graph(tmp_path, monkeypatch):
    source = configure(tmp_path, monkeypatch)
    backend = load_video_workflows().get('minimax-h3')
    from backend.app.harness.image import ImageSpec, PreparedImagePrompt
    spec = ImageSpec(purpose='video', subject='moving cat', style='cinematic', composition='wide',
        aspect_ratio='16:9', text_requirement='')
    prepared = PreparedImagePrompt(positive='moving cat',negative='text', seed=17, backend_sha256='a'*64)
    graph = backend.graph(spec,1,prepared)
    assert graph['5']['inputs'] == {'clip':['2',0], 'vae':['3',0],
        'prompt':'moving cat\nAvoid: text','width':608,'height':352,'length':90}
    assert graph['7']['inputs']['noise_seed']==17 and graph['9']['inputs']['steps']==20
    assert graph['15']['inputs']['filename_prefix']=='video/pitch_chat'
    assert graph == backend.graph(spec,1,prepared)
    source.write_text(source.read_text()+' ')
    with pytest.raises(ValueError,match='source changed'):load_video_workflows()


@pytest.mark.parametrize('failure',['none','busy','corrupt','timeout'])
def test_video_shared_worker_protocol_and_retry(tmp_path, monkeypatch, failure):
    configure(tmp_path,monkeypatch)
    submitted=[]
    async def decoded(path):
        if failure=='corrupt':raise ValueError('synthetic corrupt video')
    monkeypatch.setattr('backend.app.tools.comfy_video.validate_video',decoded)
    def route(req):
        path=req.url.path
        if path.startswith('/object_info/'):
            name=path.rsplit('/',1)[-1]
            return httpx.Response(200,json={name:{'input':{'required':{},'optional':{
                k:['STRING'] for k in ['unet_name','weight_dtype','clip_name','type','device','vae_name','prompt',
                    'width','height','length','clip','vae','shift_video','shift_audio','model','noise_seed',
                    'sampler_name','scheduler','steps','denoise','conditioning','noise','guider','sampler','sigmas',
                    'latent_image','samples','images','audio','fps','video','filename_prefix','format','codec']}}}})
        if path=='/queue':return httpx.Response(200,json={'queue_running':[1] if failure=='busy' else [],'queue_pending':[]})
        if path=='/prompt':
            submitted.append(json.loads(req.content));return httpx.Response(200,json={'prompt_id':'synthetic-video','node_errors':{}})
        if path.startswith('/history/'):
            return httpx.Response(200,json={} if failure=='timeout' else {'synthetic-video':{
                'status':{'completed':True,'status_str':'success'},'outputs':{'15':{'images':[
                    {'filename':'synthetic.mp4','subfolder':'video','type':'output'}]}}}})
        if path=='/view':return httpx.Response(200,content=b'\x00\x00\x00\x20ftyp'+b'x'*32,headers={'content-type':'video/mp4'})
        pytest.fail(path)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            host,worker,jobs,gpu,drivers,_=setup(tmp_path,client,Compiler())
            host.policy=TurnPolicy(allowed_tools=frozenset({'image.generate','media.video.generate'}))
            adapter=host.registry.get('media.video.generate').adapter
            adapter.poll_interval=0;adapter.max_polls=2
            worker.media_pipeline=MediaPipeline(host,client)
            worker.start()
            job=worker.submit_media(MediaRequest(kind='video',prompt='moving cat',prompt_mode='direct',
                backend='minimax-h3',aspect_ratio='16:9',seed=17))
            await asyncio.wait_for(worker.queue.join(),3)
            row=jobs.get(job['job_id'],private=True)
            assert row['tool']=='media.video.generate'
            assert json.loads(row['image_spec_json'])['media_kind']=='video'
            if failure=='none':
                assert row['status']=='SUCCESS',row['error']
                child=worker.retry_media(job['job_id']);await asyncio.wait_for(worker.queue.join(),3)
                assert jobs.get(child['job_id'])['status']=='SUCCESS'
                assert len(submitted)==2 and submitted[0]['prompt']==submitted[1]['prompt']
            else:
                assert row['status']!='SUCCESS' and row['media_phase']=='FAILED'
                if failure=='timeout':
                    assert row['status']=='RECOVERY_REQUIRED'
                    with pytest.raises(ValueError):worker.retry_media(job['job_id'])
            await worker.close();await gpu.close()
    asyncio.run(run())
