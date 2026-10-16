"""Synthetic GPU/LLM protocol; real CPU MP4 decode/lineage, not a GPU test."""
import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from backend.app.harness.image import ImageSpec, PreparedImagePrompt
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.video import VideoScene
from backend.app.image_assets import ImageAssetStore
from backend.app.media import MediaPipeline, MediaRequest
from backend.app.models.registry import ModelRegistry
from backend.app.config import Settings
from backend.app.tools.ltx_video import LTXVideoBackend, SIGMAS
from backend.app.video_projects import VideoProjects
from test_comfy_video import configure
from test_image_jobs import setup




@pytest.fixture
def mp4(tmp_path):
    path = tmp_path/'synthetic.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=608x352:r=24',
        '-frames:v','73','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,timeout=20)
    return path.read_bytes()


class Planner:
    def __init__(self,bad=False): self.calls=[];self.bad=bad
    async def generate_structured(self,messages,schema):
        self.calls.append((messages,schema))
        assert 'action' in schema['properties'] and 'NO image pixels' in messages[0].content
        if self.bad: return '{"subject":"cat", "shell":"bad"}'
        return json.dumps(dict(subject='a cat',action='walks slowly',setting='a garden',camera='locked',style='realistic',continuity='preserve fur'))


def make_host(tmp_path,monkeypatch,mp4,planner):
    configure(tmp_path,monkeypatch)
    cfg=Path(__import__('os').environ['PITCH_CHAT_VIDEO_WORKFLOW_CONFIG']);data=json.loads(cfg.read_text());data['ltx']={};cfg.write_text(json.dumps(data))
    calls=[]
    def route(req):
        path=req.url.path
        if path.startswith('/object_info/'):
            name=path.rsplit('/',1)[-1]
            keys=['ckpt_name','clip_name','type','device','clip','text','positive','negative','frame_rate','width','height','length','batch_size',
                'sampler_name','sigmas','model','add_noise','noise_seed','cfg','sampler','latent_image','samples','vae','tile_size',
                'overlap','temporal_size','temporal_overlap','images','fps','video','filename_prefix','format','codec','image','strength']
            return httpx.Response(200,json={name:{'input':{'required':{},'optional':{k:['STRING'] for k in keys}}}})
        if path=='/queue': return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if path=='/upload/image':
            filename=re.search(br'filename="([^"]+)"',req.content).group(1).decode()
            return httpx.Response(200,json={'name':filename,'subfolder':'','type':'input'})
        if path=='/prompt':
            calls.append(json.loads(req.content));return httpx.Response(200,json={'prompt_id':'synthetic-video','node_errors':{}})
        if path.startswith('/history/'):
            return httpx.Response(200,json={'synthetic-video':{'status':{'completed':True,'status_str':'success'},
                'outputs':{'12':{'images':[{'filename':'synthetic.mp4','subfolder':'video','type':'output'}]}}}})
        if path=='/view':return httpx.Response(200,content=mp4,headers={'content-type':'video/mp4'})
        pytest.fail(path)
    client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    host,worker,jobs,gpu,drivers,_=setup(tmp_path,client,planner)
    host.policy=TurnPolicy(allowed_tools=frozenset({'image.generate','media.video.generate'}))
    host.models=ModelRegistry(Settings(tmp_path/'unused.sqlite3','','mock','KEY',2),client,overrides={'strata':planner,'vision':planner})
    images=ImageAssetStore(gpu.store,tmp_path/'images');cid=images.create_conversation()
    projects=VideoProjects(gpu.store,images)
    worker.media_pipeline=MediaPipeline(host,client,image_assets=images);worker.video_projects=projects
    adapter=host.registry.get('media.video.generate').adapter;adapter.image_assets=images;adapter.poll_interval=0;adapter.max_polls=2
    return host,worker,jobs,gpu,client,projects,images,cid,calls


@pytest.mark.parametrize('mode',['direct','strata','qwen35'])
def test_three_prompt_routes_and_real_cpu_mp4_decode(tmp_path,monkeypatch,mp4,mode):
    async def run():
        planner=Planner();host,worker,jobs,gpu,client,projects,images,cid,calls=make_host(tmp_path,monkeypatch,mp4,planner)
        worker.start()
        request=MediaRequest(kind='video',prompt='猫が歩く',backend='ltx-video',aspect_ratio='16:9',prompt_mode=mode,conversation_id=cid)
        job=worker.submit_media(request);await asyncio.wait_for(worker.queue.join(),10)
        row=jobs.get(job['job_id'],private=True);assert row['status']=='SUCCESS',row
        clip=projects.clip(job['job_id']);assert clip['last_frame_image_id'] and clip['mp4_sha256']
        assert images.get(clip['last_frame_image_id'])['source_kind']=='video_frame'
        assert len(planner.calls)==(0 if mode=='direct' else 1) and len(calls)==1
        assert json.loads(row['image_spec_json'])['prompt_source']==mode
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_clip_extension_retry_is_immutable_and_concat_is_durable(tmp_path,monkeypatch,mp4):
    from backend.app.video_projects import Extension
    async def run():
        host,worker,jobs,gpu,client,projects,images,cid,calls=make_host(tmp_path,monkeypatch,mp4,Planner())
        worker.start();job=worker.submit_media(MediaRequest(kind='video',prompt='cat',backend='ltx-video',aspect_ratio='16:9',prompt_mode='direct',conversation_id=cid,seed=17))
        await asyncio.wait_for(worker.queue.join(),10);first=projects.clip(job['job_id'])
        child=worker.submit_media(projects.extension_request(first['clip_id'],Extension(prompt='cat continues')))
        await asyncio.wait_for(worker.queue.join(),10);second=projects.clip(child['job_id'])
        assert second['previous_clip_id']==first['clip_id'] and second['start_frame']==73 and second['end_frame']==146
        assert calls[1]['prompt']['13']['inputs']['image'].startswith('pitch_chat_')
        retry=worker.retry_media(second['clip_id']);await asyncio.wait_for(worker.queue.join(),10)
        assert projects.clip(retry['job_id'])['retry_of_clip_id']==second['clip_id']
        assert projects.clip(second['clip_id'])==second
        joined=worker.submit_video_concat(first['project_id'],[first['clip_id'],second['clip_id']])
        await asyncio.wait_for(worker.queue.join(),10);row=jobs.get(joined['job_id'],private=True)
        assert row['status']=='SUCCESS',row
        result=json.loads(row['result_json']);assert result['image_spec']['frames']==146
        assert Path(result['tool_result']['artifacts'][0]['path']).is_file()
        # Cross-project joins, duplicates, foreign conversations and tampered hashes fail closed.
        with pytest.raises(ValueError):worker.submit_video_concat(first['project_id'],[first['clip_id'],first['clip_id']])
        foreign=images.create_conversation()
        with pytest.raises(ValueError):projects.bind(MediaRequest(kind='video',prompt='cat',backend='ltx-video',aspect_ratio='16:9',conversation_id=foreign,project_id=first['project_id']))
        path=Path(json.loads(jobs.get(first['clip_id'],private=True)['result_json'])['tool_result']['artifacts'][0]['path']);path.write_bytes(b'changed')
        broken=worker.submit_video_concat(first['project_id'],[first['clip_id']]);await asyncio.wait_for(worker.queue.join(),10)
        assert jobs.get(broken['job_id'])['status']=='ERROR'
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['strata','qwen35'])
def test_bad_scene_never_falls_back_or_submits_comfy(tmp_path,monkeypatch,mp4,mode):
    async def run():
        planner=Planner(True);host,worker,jobs,gpu,client,projects,images,cid,calls=make_host(tmp_path,monkeypatch,mp4,planner)
        worker.start();job=worker.submit_media(MediaRequest(kind='video',prompt='cat',backend='ltx-video',aspect_ratio='16:9',prompt_mode=mode,conversation_id=cid))
        await asyncio.wait_for(worker.queue.join(),5)
        assert jobs.get(job['job_id'])['status']=='ERROR' and not calls and len(planner.calls)==1
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_video_dom_mock():
    import shutil
    node=shutil.which('node')
    if node is None:pytest.skip('Node required for isolated DOM check')
    result=subprocess.run([node,'tests/frontend_videos.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'checks passed' in result.stdout


def test_owned_video_api_extension_and_assembly_uuid_contract(tmp_path,monkeypatch,mp4):
    from fastapi import FastAPI
    from backend.app.media import router as media_router
    from backend.app.video_projects import router,Assembly
    assert isinstance(Assembly.model_validate({'clip_ids':['11111111-1111-4111-8111-111111111111']}).clip_ids[0],UUID)
    async def run():
        host,worker,jobs,gpu,wire,projects,images,cid,calls=make_host(tmp_path,monkeypatch,mp4,Planner())
        app=FastAPI();app.include_router(media_router());app.include_router(router())
        app.state.worker=worker;app.state.jobs=jobs;app.state.video_projects=projects
        worker.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as client:
            first=await client.post('/api/media/jobs',json={'kind':'video','prompt':'cat','backend':'ltx-video',
                'aspect_ratio':'16:9','prompt_mode':'direct','conversation_id':cid});assert first.status_code==202,first.text
            await asyncio.wait_for(worker.queue.join(),10);clip=projects.clip(first.json()['job_id'])
            refused=await client.post(f"/api/videos/clips/{clip['clip_id']}/extend",json={'prompt':'continues'},headers={'Origin':'http://foreign.invalid'})
            assert refused.status_code==403
            child=await client.post(f"/api/videos/clips/{clip['clip_id']}/extend",json={'prompt':'continues'});assert child.status_code==202,child.text
            await asyncio.wait_for(worker.queue.join(),10)
            joined=await client.post(f"/api/videos/projects/{clip['project_id']}/concatenate",json={'clip_ids':[clip['clip_id'],child.json()['job_id']]})
            assert joined.status_code==202,joined.text;await asyncio.wait_for(worker.queue.join(),10)
            assert jobs.get(joined.json()['job_id'])['status']=='SUCCESS'
            data=await client.get(f'/api/conversations/{cid}/videos');assert data.headers['cache-control']=='no-store'
            assert len(data.json()['clips'])==2 and len(data.json()['assemblies'])==1
        await worker.close();await gpu.close();await wire.aclose()
    asyncio.run(run())


def test_compiler_generated_qwen_image_is_a_conversation_video_reference(tmp_path):
    from backend.app.harness.image import ImageRequest,ImageSpec
    from test_comfyui import Protocol
    from test_image_jobs import Compiler
    async def run():
        protocol=Protocol()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host,worker,jobs,gpu,drivers,_=setup(tmp_path,client,Compiler())
            images=ImageAssetStore(gpu.store,tmp_path/'images');cid=images.create_conversation()
            worker.media_pipeline=MediaPipeline(host,client,image_assets=images)
            worker.start();preview=worker.submit_image(ImageRequest(request='cat',backend='qwen-image',compile_only=True,conversation_id=cid))
            await asyncio.wait_for(worker.queue.join(),5)
            parent=jobs.get(preview['job_id'],private=True);assert parent['status']=='SUCCESS'
            child=worker.submit_image_render(preview['job_id'],ImageSpec.model_validate_json(parent['image_spec_json']))
            await asyncio.wait_for(worker.queue.join(),5);assert jobs.get(child['job_id'])['status']=='SUCCESS'
            saved=images.timeline(cid)['images'];assert len(saved)==1 and saved[0]['source_job_id']==child['job_id']
            assert images.reference_bytes(images.reference(saved[0]['id'],cid)).startswith(b'\x89PNG')
            await worker.close();await gpu.close()
    asyncio.run(run())
