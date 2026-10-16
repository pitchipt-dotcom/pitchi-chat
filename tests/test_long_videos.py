"""Synthetic GPU/LLM, actual CPU decode/assembly and durable SQLite checkpoints."""
import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.long_videos import LongVideos,LongRequest,Control,Story,Scene
from backend.app.long_video_assembly import clip_count,frame_difference
from backend.app.deletion import Deletions,Target,Refusal
from test_ltx_video import make_host,mp4,Planner


def service(tmp_path,monkeypatch,mp4,planner=None):
    values=make_host(tmp_path,monkeypatch,mp4,planner or Planner())
    host,worker,jobs,gpu,client,projects,images,cid,calls=values
    images.root=tmp_path/'image-assets';images.root.mkdir()
    from backend.app.jobs.chat_generations import ChatGenerationStore
    ChatGenerationStore(jobs.store)
    long=LongVideos(jobs.store,worker)
    return long,values


@pytest.mark.parametrize('seconds',[15,30,60,90,120,180])
@pytest.mark.parametrize('frames',[73,90])
@pytest.mark.parametrize('fade',[0,3])
def test_exact_target_capacity_and_budget(seconds,frames,fade):
    count=clip_count(seconds,frames,fade)
    assert 1<=count<=64
    assert frames+(count-1)*(frames-1-fade)>=seconds*24
    assert count==1 or frames+(count-2)*(frames-1-fade)<seconds*24


def test_first_clip_gate_resume_and_exact_real_cpu_assembly(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start()
        p=long.create(LongRequest(conversation_id=cid,prompt='A red ball slowly moves right.',seconds=15))
        await asyncio.wait_for(worker.queue.join(),30)
        row=long.public(p['id']);assert row['status']=='AWAITING_ESTIMATE_APPROVAL' and len(calls)==1
        assert row['completed_clips']==1 and row['estimate']['required_disk_bytes']>0
        with pytest.raises(ValueError):long.control(p['id'],Control(action='approve',estimate_sha256='0'*64))
        long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']))
        await asyncio.wait_for(worker.queue.join(),60)
        row=long.public(p['id']);assert row['status']=='COMPLETED',row
        assert row['completed_clips']==5 and len(calls)==5 and row['video_url']
        assert row['completed_seconds']==15 and all('FIXED SETTINGS:' in c['prompt']['3']['inputs']['text'] for c in calls)
        assert all('14' in c['prompt'] for c in calls[1:]),'I2V continuation required'
        final=jobs.get(row['final_job_id'],private=True);result=json.loads(final['result_json'])
        assert result['image_spec']['fps']==24 and result['image_spec']['seconds']==15
        assert result['image_spec']['width']==576 and result['image_spec']['height']==324
        assert not result['image_spec']['audio']
        assert all('long_metrics' in json.loads(jobs.get(m['job_id'],private=True)['image_spec_json']) for m in long.successful(p['id']))
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_pause_cancel_and_restart_keep_completed_clip(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball rolls.',seconds=180))
        await asyncio.wait_for(worker.queue.join(),20);first=long.successful(p['id'])[0]['job_id']
        row=long.public(p['id'])
        with pytest.raises(ValueError):long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']))
        long.control(p['id'],Control(action='pause'));assert long.public(p['id'])['status']=='PAUSED'
        restarted=LongVideos(jobs.store,worker);restarted.control(p['id'],Control(action='resume'))
        assert restarted.public(p['id'])['status']=='AWAITING_ESTIMATE_APPROVAL' and len(calls)==1
        restarted.control(p['id'],Control(action='cancel'))
        assert restarted.public(p['id'])['status']=='CANCELLED' and jobs.get(first)['status']=='SUCCESS'
        assert len(calls)==1,'180 seconds never runs an unapproved continuation'
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_delete_parent_protects_user_input_and_removes_dedicated_files(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        from test_deletion import png
        source=images.save(png(),cid)
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball rolls.',reference_image_id=source['id']))
        await asyncio.wait_for(worker.queue.join(),20)
        d=Deletions(jobs.store)
        with pytest.raises(Refusal):d.preview([Target(kind='image',id=source['id'])])
        child=long.successful(p['id'])[0];path=Path(json.loads(jobs.get(child['job_id'],private=True)['result_json'])['tool_result']['artifacts'][0]['path'])
        preview=d.preview([Target(kind='long_video',id=p['id'])]);done=d.execute(preview['operation_id'])
        assert done['status']=='COMPLETED' and not path.exists() and images.path(source['id']).exists()
        with jobs.store.connection() as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('aspect,fade',[('9:16',0),('16:9',3)])
def test_portrait_and_short_crossfade_keep_exact_duration(tmp_path,monkeypatch,mp4,aspect,fade):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.',aspect_ratio=aspect,crossfade_frames=fade,reset_interval=2))
        await asyncio.wait_for(worker.queue.join(),20);row=long.public(p['id'])
        long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']))
        await asyncio.wait_for(worker.queue.join(),60)
        row=long.public(p['id']);assert row['status']=='COMPLETED',row
        assert row['completed_seconds']==15 and row['completed_clips']==clip_count(15,73,fade)
        result=json.loads(jobs.get(row['final_job_id'],private=True)['result_json'])
        assert (result['image_spec']['width'],result['image_spec']['height'])==((324,576) if aspect=='9:16' else (576,324))
        # Every second clip explicitly resets from the immutable anchor, not the last tail.
        reqs=[json.loads(jobs.get(m['job_id'],private=True)['image_request_json']) for m in long.successful(p['id'])]
        assert reqs[2]['reference_image_id']==long.get(p['id'])['anchor_image_id'] and reqs[2].get('previous_clip_id') is None
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


class StoryPlanner:
    async def generate_structured(self,messages,schema):
        assert 'character' in schema['properties'] and 'NO image pixels' in messages[0].content
        return Story(story='A ball moves.',character='A red ball',clothing='none',background='a white room',style='realistic',camera='locked',
            scenes=[Scene(motion_steps=['move right'],camera='locked')]).model_dump_json()


@pytest.mark.parametrize('planner',['strata','qwen35','manual'])
def test_schema_plan_uses_selected_provider_or_manual_only(tmp_path,monkeypatch,mp4,planner):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4,StoryPlanner());host,worker,jobs,gpu,client,projects,images,cid,calls=v
        manual=Story(story='fixed story',character='ball',clothing='none',background='room',style='realistic',camera='locked',
            scenes=[Scene(motion_steps=['move'],camera='locked')]) if planner=='manual' else None
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A red ball moves.',planner=planner,manual_story=manual))
        await asyncio.wait_for(worker.queue.join(),20);row=long.public(p['id'])
        assert row['status']=='AWAITING_ESTIMATE_APPROVAL' and row['plan']['story']['character'] in {'ball','A red ball'}
        assert len(calls)==1
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_failed_clip_has_finite_explicit_retry_and_unknown_remote_never_replays(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.',retry_limit=0))
        await asyncio.wait_for(worker.queue.join(),20)
        # Simulate an uncertain child persisted over a restart, without touching models.
        with jobs.store.connection() as db:
            child=long.successful(p['id'])[0]['job_id'];db.execute("UPDATE tool_jobs SET status='RECOVERY_REQUIRED',remote_phase='SUBMITTED' WHERE job_id=?",(child,))
        long.update(p['id'],status='RECOVERY_REQUIRED',active_job_id=child)
        with pytest.raises(ValueError,match='uncertain_child'):await long.reconcile(p['id'])
        assert len(calls)==1
        long.update(p['id'],status='FAILED',active_job_id=None)
        with pytest.raises(ValueError):long.control(p['id'],Control(action='retry'))
        assert len(calls)==1
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_pause_at_clip_boundary_and_new_worker_resume_without_repeating_completed(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.'))
        await asyncio.wait_for(worker.queue.join(),20);first=long.successful(p['id'])[0]['job_id'];row=long.public(p['id'])
        original=host.run_tool;entered=asyncio.Event();release=asyncio.Event()
        async def held(*args,**kwargs):entered.set();await release.wait();return await original(*args,**kwargs)
        host.run_tool=held
        long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']))
        await asyncio.wait_for(entered.wait(),5);long.control(p['id'],Control(action='pause'))
        assert long.get(p['id'])['status']=='PAUSE_REQUESTED';release.set()
        await asyncio.wait_for(worker.queue.join(),20);assert long.get(p['id'])['status']=='PAUSED' and len(calls)==2
        await worker.close();host.run_tool=original
        from backend.app.jobs.worker import JobWorker
        fresh_worker=JobWorker(jobs,host,media_pipeline=worker.media_pipeline,video_projects=projects)
        fresh=LongVideos(jobs.store,fresh_worker);fresh_worker.start();fresh.control(p['id'],Control(action='resume'))
        await asyncio.wait_for(fresh_worker.queue.join(),60)
        assert fresh.public(p['id'])['status']=='COMPLETED' and len(calls)==5
        assert fresh.successful(p['id'])[0]['job_id']==first
        await fresh_worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_final_only_deletion_keeps_clips_then_full_delete_cleans_anchor(tmp_path,monkeypatch,mp4):
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.'));await worker.queue.join()
        row=long.public(p['id']);long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']));await worker.queue.join()
        row=long.public(p['id']);d=Deletions(jobs.store)
        preview=d.preview([Target(kind='job',id=row['final_job_id'])]);assert d.execute(preview['operation_id'])['status']=='COMPLETED'
        assert long.public(p['id'])['status']=='OUTPUT_DELETED' and len(long.successful(p['id']))==5
        long.control(p['id'],Control(action='reassemble'));await worker.queue.join();assert len(calls)==5
        assert long.public(p['id'])['status']=='COMPLETED'
        preview=d.preview([Target(kind='long_video',id=p['id'])]);done=d.execute(preview['operation_id']);assert done['status']=='COMPLETED',done
        assert not list(images.root.glob('*.png'))
        with jobs.store.connection() as db:
            assert not db.execute('SELECT 1 FROM long_video_members').fetchone()
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_audio_is_preserved_with_frame_trim_and_exact_target(tmp_path):
    import hashlib,subprocess
    from backend.app.long_video_assembly import assemble
    path=tmp_path/'audio-native.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=608x352:r=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
        '-frames:v','90','-t','3.75','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True,timeout=20)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    result=asyncio.run(assemble([path]*5,(tmp_path,),tmp_path/'artifacts',seconds=15,aspect='16:9',expected_hashes=[digest]*5))
    assert result.image_spec['audio'] and result.image_spec['seconds']==15
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',result.tool_result.artifacts[0].path]))
    audio=next(s for s in info['streams'] if s['codec_type']=='audio');video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert abs(float(audio['duration'])-float(video['duration']))<.1


def test_owned_staging_files_are_included_in_parent_delete(tmp_path,monkeypatch,mp4):
    from uuid import uuid4
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.'));await worker.queue.join()
        leftovers=[]
        for suffix in ['.mp4','.txt']:
            name=uuid4().hex+suffix;path=tmp_path/'artifacts'/name;path.write_bytes(b'dedicated unfinished assembly fixture')
            long.temp_receipt(p['id'],name);leftovers.append(path)
        d=Deletions(jobs.store);preview=d.preview([Target(kind='long_video',id=p['id'])]);result=d.execute(preview['operation_id'])
        assert result['status']=='COMPLETED' and all(not p.exists() for p in leftovers)
        with jobs.store.connection() as db:assert not db.execute('SELECT 1 FROM long_video_temp_files').fetchone()
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())


def test_disk_and_ram_reject_without_using_swap_or_losing_completed_clips(tmp_path,monkeypatch,mp4):
    import shutil
    async def run():
        long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
        worker.start();p=long.create(LongRequest(conversation_id=cid,prompt='A ball moves.'));await worker.queue.join();row=long.public(p['id'])
        monkeypatch.setattr('backend.app.long_videos.shutil.disk_usage',lambda p:shutil._ntuple_diskusage(100,100,0))
        with pytest.raises(ValueError,match='disk_reserve'):long.control(p['id'],Control(action='approve',estimate_sha256=row['estimate']['sha256']))
        assert len(calls)==1 and len(long.successful(p['id']))==1
        await worker.close();await gpu.close();await client.aclose()
    asyncio.run(run())
