"""Synthetic workflow and reviewed-edit tests. No model download or GPU use."""
import asyncio
import json
import httpx
import pytest
from pydantic import ValidationError
from backend.app.tools.image_workflows import ImageBackend
from backend.app.harness.image import ImageSpec,PreparedImagePrompt
from backend.app.media import MediaPipeline,MediaRequest,MediaPromptEdit
from test_image_jobs import setup,Compiler
from test_comfyui import Protocol


def flux():
    return ImageBackend(id='flux-klein',family='flux2-klein',model='synthetic_flux.safetensors',
        text_encoder='synthetic_encoder.safetensors',vae='synthetic_vae.safetensors',steps=4,guidance=1.,resolution=1024)


def test_official_distilled_shape_and_no_old_digest_drift():
    from backend.app.media import backend_digest
    old=ImageBackend(id='sdxl',family='sdxl',model='synthetic.safetensors')
    assert 'resolution' not in old.model_dump()
    spec=ImageSpec(purpose='image',subject='cat',style='photo',composition='centered',aspect_ratio='1:1',text_requirement='')
    prepared=PreparedImagePrompt(positive='A cat in a garden',negative='',seed=17,backend_sha256=backend_digest(flux()))
    g=flux().graph(spec,1,prepared)
    assert g['encoder']['inputs']['type']=='flux2'
    assert g['latent']['inputs']=={'width':1024,'height':1024,'batch_size':1}
    assert g['sigmas']['inputs']=={'steps':4,'width':1024,'height':1024}
    assert g['guider']['inputs']['cfg']==1 and g['negative']['class_type']=='ConditioningZeroOut'
    assert g['noise']['inputs']['noise_seed']==17 and g['sampler']['inputs']['sampler_name']=='euler'
    with pytest.raises(ValidationError):ImageBackend.model_validate(flux().model_dump()|{'steps':20})
    with pytest.raises(ValidationError):MediaPromptEdit(positive_prompt='cat',command='echo unsafe')


def test_saved_preview_edit_retains_parent_and_uses_child_without_llm(tmp_path):
    async def run():
        protocol=Protocol()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host,worker,jobs,gpu,_,_=setup(tmp_path,client,Compiler())
            worker.media_pipeline=MediaPipeline(host,client);worker.start()
            job=worker.submit_media(MediaRequest(prompt='original cat',backend='qwen-image',prompt_mode='direct',preview_only=True,seed=17))
            await worker.queue.join();parent=jobs.get(job['job_id'],private=True);original=parent['image_spec_json']
            assert jobs.get(job['job_id'])['media_phase']=='PROMPT_SAVED'
            child=worker.retry_media(job['job_id'],edit=MediaPromptEdit(positive_prompt='Human edited watercolor cat',negative_prompt='blur'))
            await worker.queue.join()
            assert jobs.get(child['job_id'])['status']=='SUCCESS'
            plan=json.loads(jobs.get(child['job_id'],private=True)['image_spec_json'])
            assert plan['human_edited'] and plan['parent_job_id']==job['job_id'] and plan['seed']==17
            assert protocol.graph['positive']['inputs']['text']=='Human edited watercolor cat'
            assert jobs.get(job['job_id'],private=True)['image_spec_json']==original
            with pytest.raises(ValueError):worker.retry_media(child['job_id'],edit=MediaPromptEdit(positive_prompt='edit completed job'))
            await worker.close();await gpu.close()
    asyncio.run(run())


def test_render_api_rejects_scope_fields_and_foreign_origin(tmp_path):
    from fastapi import FastAPI
    from backend.app.media import router
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(Protocol().handle)) as wire:
            host,worker,jobs,gpu,_,_=setup(tmp_path,wire,Compiler());worker.media_pipeline=MediaPipeline(host,wire);worker.start()
            app=FastAPI();app.include_router(router());app.state.worker=worker;app.state.jobs=jobs
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as c:
                r=await c.post('/api/media/jobs',json={'prompt':'cat','backend':'qwen-image','prompt_mode':'direct','preview_only':True});jid=r.json()['job_id'];await worker.queue.join()
                path='/api/media/jobs/'+jid+'/render'
                assert (await c.post(path,json={'positive_prompt':'cat'},headers={'Origin':'http://foreign.invalid'})).status_code==403
                for key,value in [('backend','sdxl'),('seed',99),('graph',{}),('command','unsafe')]:
                    assert (await c.post(path,json={'positive_prompt':'cat',key:value})).status_code==422
                assert (await c.post(path,json={'positive_prompt':'reviewed cat'})).status_code==202
                await worker.queue.join()
            await worker.close();await gpu.close()
    asyncio.run(run())
