from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from backend.app.storage.sqlite import HistoryStore
from backend.app.work_bridge import router,WorkClient
from backend.app.tools.guideline_work import PROJECT

class Tool:
    enabled=True
    def __init__(self):self.calls=[]
    async def call(self,payload,publication=False):
        self.calls.append((payload,publication))
        return {'run_id':'gl-'+'a'*32,'status':'RUNNING','tool_task':True,'message':'隔離CLI検証を進めています。'}
class Client:
    config={'enabled':True}
    def __init__(self):self.guidelines=Tool();self.calls=[]
    async def call(self,payload):
        self.calls.append(payload)
        return await self.guidelines.call(payload)
@pytest.fixture
def fixture(tmp_path):
    store=HistoryStore(tmp_path/'chat.sqlite');store.initialize();client=Client();app=FastAPI();app.include_router(router(store,client))
    with TestClient(app) as api:yield api,client

def submit(api):return api.post('/api/work/missions',json={'message':'KDIGO CKD 2024を登録して','project':PROJECT,'operation':'create'}).json()
def approval():return {'sha256':'b'*64,'challenge':'c'*32,'confirmation':'本番反映を承認 '+'b'*64}

def test_natural_request_and_state(fixture):
    api,client=fixture;r=submit(api);assert r['status']=='RUNNING'
    assert client.calls[-1]['project']==PROJECT
    assert 'approve' not in client.calls[-1]
    assert api.get('/api/work/missions/'+r['run_id']).json()['tool_task']

def test_approval_requires_origin_and_explicit_header(fixture):
    api,client=fixture;r=submit(api);url='/api/work/missions/'+r['run_id']+'/approve';a=approval()
    assert api.post(url,json=a).status_code==403
    assert api.post(url,json=a,headers={'Origin':'https://evil.test','X-Pitchi-Human-Approval':a['challenge']}).status_code==403
    assert api.post(url,json=a,headers={'Origin':'http://testserver','X-Pitchi-Human-Approval':'wrong'}).status_code==403
    assert not any(publication for _,publication in client.guidelines.calls)

def test_explicit_approval_is_separate_tool_action(fixture):
    api,client=fixture;r=submit(api);a=approval()
    response=api.post('/api/work/missions/'+r['run_id']+'/approve',json=a,headers={'Origin':'http://testserver','X-Pitchi-Human-Approval':a['challenge']})
    assert response.status_code==202
    assert client.guidelines.calls[-1]==({'run_id':r['run_id'],**a},True)

def test_no_shell_or_cross_run_access(fixture):
    api,client=fixture
    assert api.post('/api/work/missions',json={'message':'x','project':PROJECT,'shell':'arbitrary'}).status_code==422
    assert api.post('/api/work/missions/gl-'+ 'd'*32+'/approve',json=approval(),headers={'Origin':'http://testserver'}).status_code==404

def test_search_quality_uses_existing_chat_work_contract(fixture):
    api,client=fixture
    r=api.post('/api/work/missions',json={'message':'大動脈解離 リハビリテーションの検索結果を改善して','project':'ptotsearch-quality','operation':'repair'})
    assert r.status_code==202
    data=r.json();assert client.calls[-1]['project']=='ptotsearch-quality'
    assert client.calls[-1]['operation']=='create'
    assert api.get('/api/work/missions/'+data['run_id']).status_code==200
    assert api.get('/api/work/missions/'+data['run_id']+'/explanation').status_code==200


def test_natural_search_request_selects_quality_capability(fixture):
    api,client=fixture
    response=api.post('/api/work/missions',json={'message':'大動脈解離 リハビリテーションの検索結果を改善して','project':PROJECT})
    assert response.status_code==202
    assert client.calls[-1]['project']=='ptotsearch-quality'


def test_show_search_diff_reads_subject_without_new_mission(fixture):
    api,client=fixture
    first=api.post('/api/work/missions',json={'message':'大動脈解離 リハビリテーションの検索結果を改善して','project':'ptotsearch-quality'}).json()
    count=len([x for x in client.calls if x['action']=='submit'])
    response=api.post('/api/work/missions',json={'message':'検索改善の変更差分とテスト結果を見せて','conversation_id':first['conversation_id'],'project':'ptotsearch-quality'}).json()
    assert response['kind']=='explanation'
    assert response['subject_run_id']==first['run_id']
    assert len([x for x in client.calls if x['action']=='submit'])==count


def test_saved_success_reuses_known_conversation_without_duplicate_run(fixture):
    api,client=fixture
    first=submit(api)
    original=client.call
    async def reuse(payload):
        if payload['action']=='submit':
            return {'run_id':first['run_id'],'status':'NEEDS_HUMAN','tool_task':True,
                    'reused_result':True,'new_llm_calls':0,'message':'保存済み結果'}
        return await original(payload)
    client.call=reuse
    for _ in range(2):
        response=api.post('/api/work/missions',json={'message':'大動脈解離 リハビリテーションの検索結果を改善して','project':PROJECT})
        assert response.status_code==202
        data=response.json()
        assert data['run_id']==first['run_id']
        assert data['conversation_id']==first['conversation_id']
        assert data['reused_result'] and data['new_llm_calls']==0
    assert api.get('/api/work/missions/'+first['run_id']).status_code==200


def test_unbound_saved_run_cannot_be_exposed(fixture):
    api,client=fixture
    async def reuse(payload):
        return {'run_id':'gl-'+'f'*32,'status':'NEEDS_HUMAN','reused_result':True}
    client.call=reuse
    response=api.post('/api/work/missions',json={'message':'大動脈解離 リハビリテーションの検索結果を改善して','project':PROJECT})
    assert response.status_code==404
