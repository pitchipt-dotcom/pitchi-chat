"""Dedicated synthetic files/SQLite only: never connects to existing services."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.deletion import Deletions, Target, Refusal
from backend.app.storage.sqlite import HistoryStore
from backend.app.image_assets import ImageAssetStore
from backend.app.jobs.store import JobStore
from backend.app.jobs.chat_generations import ChatGenerationStore
from backend.app.video_projects import VideoProjects
from backend.app.main import create_app
from backend.app.config import Settings
from backend.app.providers.config import CloudSettings
from backend.app.resources.config import ResourceSettings
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.tools.work_config import WorkSettings


def png():
    out = io.BytesIO(); Image.new('RGB',(32,32),'red').save(out,format='PNG'); return out.getvalue()


@pytest.fixture
def storage(tmp_path):
    h = HistoryStore(tmp_path/'history.sqlite3'); h.initialize()
    j = JobStore(h); c = ChatGenerationStore(h); i = ImageAssetStore(h,tmp_path/'image-assets'); v = VideoProjects(h,i)
    d = Deletions(h)
    return SimpleNamespace(history=h,jobs=j,chat=c,images=i,videos=v,deletions=d)


def job(s, tool='tts.generate', cid=None, path=None, reference=None):
    jid=str(uuid4())
    path=path or s.deletions.roots['artifacts']/(uuid4().hex+('.wav' if tool=='tts.generate' else '.mp4' if 'video' in tool else '.png'))
    if not path.exists():path.write_bytes(b'synthetic-test-only')
    request={'conversation_id':cid, 'reference_image_id':reference}
    result={'tool_result':{'artifacts':[{'path':str(path),'kind':'audio' if tool=='tts.generate' else 'file'}]}}
    with s.history.connection() as db:
        db.execute('INSERT INTO tool_jobs(job_id,tool,status,call_json,requires_gpu,result_json,image_request_json) VALUES(?,?,\'SUCCESS\',\'{}\',0,?,?)',
            (jid,tool,json.dumps(result),json.dumps(request)))
    return jid,path


def erase(s, targets):
    p=s.deletions.preview([Target(kind=k,id=i) for k,i in targets]);return s.deletions.execute(p['operation_id'])


def test_upload_individual_physical_delete_and_idempotence(storage):
    s=storage;cid=s.images.create_conversation();image=s.images.save(png(),cid)
    paths=[s.images.path(image['id']),s.images.path(image['id'],reference=True)]
    preview=s.deletions.preview([Target(kind='image',id=image['id'])]);assert preview['scope']['files']==2
    result=s.deletions.execute(preview['operation_id']);assert result['status']=='COMPLETED'
    assert all(not p.exists() for p in paths)
    assert s.images.timeline(cid)['images']==[] and s.history.history(cid)==[]
    assert s.deletions.execute(preview['operation_id'])['status']=='COMPLETED'
    with pytest.raises(ValueError):s.images.get(image['id'])


@pytest.mark.parametrize('tool',['tts.generate','media.video.generate','image.generate'])
def test_audio_video_generated_artifact_deletion(storage,tool):
    s=storage;jid,path=job(s,tool)
    result=erase(s,[('job',jid)])
    assert result['status']=='COMPLETED' and not path.exists() and s.jobs.get(jid) is None
    with s.history.connection() as db:
        assert not db.execute('SELECT 1 FROM tool_jobs WHERE job_id=?',(jid,)).fetchone()


def test_generated_image_removes_all_host_copies(storage):
    s=storage;cid=s.images.create_conversation();jid,p=job(s,'image.generate',cid)
    image=s.images.save(png(),cid,source_job_id=jid)
    assert erase(s,[('image',image['id'])])['files_removed']==3
    assert not p.exists() and not s.images.timeline(cid)['images'] and s.jobs.get(jid) is None


def test_conversation_batch_erases_payloads_metadata_and_no_recreation(storage):
    s=storage;cid=s.images.create_conversation();s.history.save_turn(cid,'test secret needle','dedicated answer')
    image=s.images.save(png(),cid);jid,p=job(s,'image.generate',cid)
    client=str(uuid4());chat,_=s.chat.create(client,cid,{'message':'fixture'});s.chat.start(chat['job_id']);s.chat.fail(chat['job_id'],'fixture')
    other=s.images.create_conversation();other_image=s.images.save(png(),other)
    result=erase(s,[('conversation',cid),('image',other_image['id'])]);assert result['status']=='COMPLETED'
    assert s.history.history(cid) is None and s.history.history(other)==[] and not p.exists()
    with s.history.connection() as db:
        for table in ['messages','image_assets','chat_generation_jobs']:
            assert db.execute('SELECT count(*) FROM '+table+' WHERE conversation_id=?',(cid,)).fetchone()[0]==0
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('SELECT plan_json FROM deletion_operations').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM deletion_audit').fetchone()[0]==1
    assert b'test secret needle' not in s.history.path.read_bytes()
    with pytest.raises(Exception,match='conversation_deleted'):s.history.save_turn(cid,'resurrect','bad')
    with pytest.raises(Exception,match='conversation_deleted'):s.chat.create(client,cid,{'message':'replay'})


def test_shared_image_reference_rejected_until_dependent_job_selected(storage):
    s=storage;cid=s.images.create_conversation();image=s.images.save(png(),cid)
    jid,p=job(s,'media.video.generate',cid,reference=image['id'])
    with pytest.raises(Refusal,match='参照'):erase(s,[('image',image['id'])])
    assert p.exists() and s.images.path(image['id']).exists()
    result=erase(s,[('image',image['id']),('job',jid)])
    assert result['status']=='COMPLETED' and not p.exists()


def test_shared_file_protection(storage):
    s=storage;one,path=job(s);two,_=job(s,path=path)
    result=erase(s,[('job',one)])
    assert result['status']=='COMPLETED' and result['retained'] and path.exists() and s.jobs.get(two)
    assert erase(s,[('job',two)])['status']=='COMPLETED' and not path.exists()


def test_symlink_outside_root_hardlink_and_arbitrary_path_are_never_unlinked(storage,tmp_path):
    s=storage;outside=tmp_path/'external.wav';outside.write_bytes(b'foreign-service')
    for variant in ['symlink','outside','hardlink']:
        path=s.deletions.roots['artifacts']/(uuid4().hex+'.wav')
        if variant=='symlink':path.symlink_to(outside)
        elif variant=='outside':path=outside
        else:os.link(outside,path)
        jid,_=job(s,path=path)
        result=erase(s,[('job',jid)])
        assert result['status']=='PARTIAL' and result['remaining_files']==1 and outside.read_bytes()==b'foreign-service'
        assert s.jobs.get(jid) is None
        assert path.exists()


def test_partial_unlink_failure_retry_and_restart(storage,monkeypatch):
    s=storage;cid=s.images.create_conversation();image=s.images.save(png(),cid)
    real=s.deletions.unlink;n=[0]
    def fail_second(entry):
        n[0]+=1
        if n[0]==2:raise PermissionError('synthetic')
        real(entry)
    monkeypatch.setattr(s.deletions,'unlink',fail_second)
    p=s.deletions.preview([Target(kind='image',id=image['id'])]);result=s.deletions.execute(p['operation_id'])
    assert result['status']=='PARTIAL' and result['files_removed']==1 and s.images.timeline(cid)['images']==[]
    restarted=Deletions(s.history);result=restarted.execute(p['operation_id'])
    assert result['status']=='COMPLETED' and not list(s.images.root.glob('*.png'))
    assert restarted.catalog()['pending']==[]


def test_changed_file_or_reference_requires_new_confirmation(storage):
    s=storage;jid,path=job(s);p=s.deletions.preview([Target(kind='job',id=jid)])
    path.write_bytes(b'changed fixture')
    with pytest.raises(Refusal,match='変化'):s.deletions.execute(p['operation_id'])
    assert path.exists()


def test_running_and_queued_jobs_not_interrupted(storage):
    s=storage;jid,path=job(s)
    with s.history.connection() as db:db.execute("UPDATE tool_jobs SET status='RUNNING' WHERE job_id=?",(jid,))
    with pytest.raises(Refusal,match='生成中'):erase(s,[('job',jid)])
    assert path.exists() and s.jobs.get(jid)['status']=='RUNNING'


def test_api_validation_access_denial_same_origin_and_fresh_history(tmp_path):
    settings=Settings(tmp_path/'history.sqlite3','','fixture','TEST_KEY',1)
    app=create_app(settings,services=(),resource_settings=ResourceSettings(),cloud_settings=CloudSettings(),
        image_workflows=ImageWorkflowRegistry(),work_settings=WorkSettings())
    with TestClient(app) as c:
        cid=c.post('/api/images/conversations').json()['conversation_id']
        image=c.post('/api/images/attachments',params={'conversation_id':cid},content=png(),headers={'Content-Type':'image/png'}).json()
        for bad in [{'targets':[{'kind':'image','id':'../outside'}]}, {'targets':[{'kind':'image','id':image['id'],'path':'/etc/passwd'}]}]:
            assert c.post('/api/deletions/preview',json=bad).status_code==422
        body={'targets':[{'kind':'conversation','id':cid}]}
        assert c.post('/api/deletions/preview',json=body,headers={'Origin':'https://foreign.test'}).status_code==403
        response=c.post('/api/deletions/preview',json=body);assert response.status_code==200,response.text
        p=response.json();url=f"/api/deletions/{p['operation_id']}/execute"
        assert c.post(url,json={'confirm_original_files':False}).status_code==422
        done=c.post(url,json={'confirm_original_files':True});assert done.json()['status']=='COMPLETED',done.text
        assert done.headers['cache-control']=='no-store'
        assert c.get(image['url']).status_code==410
        assert c.post('/api/images/attachments',params={'conversation_id':cid},content=png(),headers={'Content-Type':'image/png'}).status_code==410
        assert c.get('/api/conversations/'+cid).status_code==410
        assert c.post('/api/chat/stream',json={'conversation_id':cid,'client_request_id':str(uuid4()),'message':'do not recreate','model':'strata'}).status_code==410
        assert cid not in [r['id'] for r in c.get('/api/conversations').json()['conversations']]
        assert c.post(url,json={'confirm_original_files':True}).json()['status']=='COMPLETED'


def test_video_delete_protects_input_and_tail_used_by_next_clip(storage):
    s=storage;cid=s.images.create_conversation();source=s.images.save(png(),cid)
    first,p1=job(s,'media.video.generate',cid,reference=source['id'])
    tail=s.images.save(png(),cid,parent_id=source['id'],source_job_id=first,source_kind='video_frame')
    second,p2=job(s,'media.video.generate',cid,reference=tail['id'])
    pid=str(uuid4())
    with s.history.connection() as db:
        db.execute('INSERT INTO video_projects(id,conversation_id) VALUES(?,?)',(pid,cid))
        db.execute('INSERT INTO video_clips(clip_id,project_id,start_frame,end_frame,last_frame_image_id) VALUES(?,?,0,73,?)',(first,pid,tail['id']))
        db.execute('INSERT INTO video_clips(clip_id,project_id,previous_clip_id,start_frame,end_frame) VALUES(?,?,?,73,146)',(second,pid,first))
    result=erase(s,[('job',first)])
    assert result['retained'] and not p1.exists() and p2.exists()
    assert s.images.path(source['id']).exists() and s.images.path(tail['id']).exists()
    with s.history.connection() as db:
        assert db.execute('SELECT previous_clip_id FROM video_clips WHERE clip_id=?',(second,)).fetchone()[0] is None
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert erase(s,[('conversation',cid)])['status']=='COMPLETED'
    assert not p2.exists() and not list(s.images.root.glob('*.png'))
    with s.history.connection() as db:assert db.execute('SELECT count(*) FROM video_projects').fetchone()[0]==0


def test_metadata_transaction_failure_is_durable_and_retryable(storage,monkeypatch):
    import sqlite3
    s=storage;jid,path=job(s);preview=s.deletions.preview([Target(kind='job',id=jid)])
    real=s.deletions.purge
    def fail(db,plan):raise sqlite3.OperationalError('synthetic fail')
    monkeypatch.setattr(s.deletions,'purge',fail)
    result=s.deletions.execute(preview['operation_id'])
    assert result['status']=='PARTIAL' and result['metadata_cleanup_pending'] and not path.exists()
    assert s.jobs.get(jid) is None
    monkeypatch.setattr(s.deletions,'purge',real)
    done=s.deletions.execute(preview['operation_id']);assert done['status']=='COMPLETED' and not done.get('metadata_cleanup_pending')


def test_unknown_remote_completion_and_invalid_id_refused(storage):
    s=storage;jid,path=job(s)
    with s.history.connection() as db:db.execute("UPDATE tool_jobs SET status='RECOVERY_REQUIRED',remote_phase='SUBMITTED' WHERE job_id=?",(jid,))
    with pytest.raises(Refusal,match='完了が不明'):erase(s,[('job',jid)])
    with pytest.raises(Refusal,match='見つからない'):erase(s,[('image',str(uuid4()))])
    assert path.exists()


def test_directory_replaced_by_symlink_does_not_delete_foreign_files(storage,tmp_path):
    s=storage;jid,path=job(s);preview=s.deletions.preview([Target(kind='job',id=jid)])
    root=s.deletions.roots['artifacts'];moved=tmp_path/'old-artifacts';root.rename(moved)
    foreign=tmp_path/'foreign';foreign.mkdir();(foreign/path.name).write_bytes(b'foreign')
    root.symlink_to(foreign,target_is_directory=True)
    with pytest.raises(Refusal,match='変化'):s.deletions.execute(preview['operation_id'])
    assert (foreign/path.name).read_bytes()==b'foreign' and (moved/path.name).exists()


def test_deletion_dom_protocol_and_confirmation():
    import subprocess
    result=subprocess.run(['node','tests/frontend_deletion.cjs'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr


def test_individual_file_in_legacy_multi_image_job_preserves_other_outputs(storage):
    from backend.app.deletion import artifact_id
    s=storage;jid,first=job(s,'image.generate');second=first.parent/(uuid4().hex+'.png');second.write_bytes(b'second image fixture')
    with s.history.connection() as db:
        r=json.loads(db.execute('SELECT result_json FROM tool_jobs WHERE job_id=?',(jid,)).fetchone()[0])
        r['tool_result']['artifacts'].append({'path':str(second),'kind':'image'})
        db.execute('UPDATE tool_jobs SET result_json=? WHERE job_id=?',(json.dumps(r),jid))
    assert len([x for x in s.deletions.catalog()['items'] if x['kind']=='artifact'])==2
    done=erase(s,[('artifact',artifact_id(jid,0))]);assert done['status']=='COMPLETED' and not first.exists() and second.exists()
    row=s.jobs.get(jid,private=True);r=json.loads(row['result_json'])
    assert r['tool_result']['artifacts'][0].get('deleted') and r['tool_result']['artifacts'][1]['path']==str(second)
    assert s.deletions.deleted(artifact_id(jid,0)) and not s.deletions.deleted(jid)
    assert erase(s,[('artifact',artifact_id(jid,1))])['status']=='COMPLETED'
    assert not second.exists() and s.jobs.get(jid) is None


def test_http_inflight_operation_and_file_stream_block_delete(tmp_path):
    settings=Settings(tmp_path/'history.sqlite3','','fixture','TEST_KEY',1)
    app=create_app(settings,services=(),resource_settings=ResourceSettings(),cloud_settings=CloudSettings(),image_workflows=ImageWorkflowRegistry(),work_settings=WorkSettings())
    with TestClient(app) as c:
        cid=c.post('/api/images/conversations').json()['conversation_id']
        body={'targets':[{'kind':'conversation','id':cid}]}
        app.state.deletion_streams=1
        assert c.post('/api/deletions/preview',json=body).status_code==409
        app.state.deletion_streams=0;app.state.deletion_mutations=1
        assert c.post('/api/deletions/preview',json=body).status_code==409
        app.state.deletion_mutations=0
        assert c.post('/api/deletions/preview',json=body).status_code==200
        assert c.get('/api/conversations/'+cid).status_code==200


def test_legacy_compiler_request_text_is_not_a_nested_conversation_object(storage):
    s=storage;jid,path=job(s,'image.generate')
    with s.history.connection() as db:
        db.execute('UPDATE tool_jobs SET image_request_json=? WHERE job_id=?',
            (json.dumps({'request':'synthetic legacy image request','conversation_id':None,'compile_only':False}),jid))
    item=next(x for x in s.deletions.catalog()['items'] if x['id']==jid)
    assert item['conversation_id'] is None
    cid=s.images.create_conversation()
    assert erase(s,[('conversation',cid)])['status']=='COMPLETED' and path.exists()
    assert erase(s,[('job',jid)])['status']=='COMPLETED' and not path.exists()


def test_pre_long_video_partial_manifest_can_still_complete(storage):
    s=storage;jid,path=job(s);p=s.deletions.preview([Target(kind='job',id=jid)])
    with s.history.connection() as db:
        plan=json.loads(db.execute('SELECT plan_json FROM deletion_operations WHERE id=?',(p['operation_id'],)).fetchone()[0]);plan.pop('long_videos',None)
        db.execute("UPDATE deletion_operations SET status='PARTIAL',plan_json=? WHERE id=?",(json.dumps(plan),p['operation_id']))
    assert s.deletions.execute(p['operation_id'])['status']=='COMPLETED' and not path.exists()
