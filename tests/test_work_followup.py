"""Saved-run conversation regression; no missions or evidence edits."""
import json
import os
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app.storage.sqlite import HistoryStore
from backend.app.work_bridge import router
from backend.app.work_followup import intent

ORIGINAL = '20261008_203253_71b180'
SECOND = '20261008_203444_fcb509'


@pytest.fixture
def saved(tmp_path):
    home = os.environ.get('PITCH_CHAT_WORK_SAVED_HOME')
    if not home: pytest.skip('Set saved Work home for real-run regression')
    results = {}
    for identifier in (ORIGINAL, SECOND):
        folder = Path(home)/'runs'/identifier
        state = json.loads((folder/'state.json').read_text())
        results[identifier] = {'run_id':identifier, 'status':'FAILED' if identifier == ORIGINAL else 'NEEDS_CAPABILITY',
                              'native_status':state['status'], 'failure_class':state['failure_class'],
                              'message':state['stop_reason'], 'artifact':None,
                              'original_request':json.loads((folder/'root_goal.json').read_text())['request']}
    class Client:
        calls = []
        config = {'enabled':True}
        async def call(self, payload):
            self.calls.append(payload)
            if payload['action'] in {'continue','submit'}: return {**results[ORIGINAL], 'run_id':'20260101_000000_000001','status':'RUNNING'}
            if payload['action'] == 'advice': return {'subject_run_id':payload['run_id'],'advisor_run_id':'a'*32,'status':'RUNNING'}
            return results[payload['run_id']]
    remote = Client(); remote.calls = []
    store = HistoryStore(tmp_path/'chat.sqlite3'); store.initialize()
    conversation = '4b317a11-2374-4378-a09a-88d1305cef0d'
    store.save_turn(conversation, '足し算アプリ作って', '完成できませんでした')
    app = FastAPI(); app.include_router(router(store,remote))
    with TestClient(app) as api:
        api.get('/api/work/conversations/'+conversation)
        with store.connection() as db:
            db.execute('INSERT INTO work_context(conversation_id,run_id) VALUES (?,?)',(conversation,SECOND))
            db.executemany('INSERT INTO work_runs VALUES (?,?)',[(ORIGINAL,conversation),(SECOND,conversation)])
        yield api,remote,conversation


def test_saved_original_not_advisor_failure(saved):
    api,remote,conversation = saved
    for text in ('なぜ失敗したか教えて','詳しく教えて','何がダメだった？','さっきのエラーは？'):
        value = api.post('/api/work/missions',json={'message':text,'conversation_id':conversation}).json()
        assert value['kind'] == 'explanation' and value['subject_run_id'] == ORIGINAL
        assert '登録済み' in value['human_explanation']['message']
        assert 'workspace_file_listing' not in value['human_explanation']['message']
        assert 'registered_profile_boundary' not in value['human_explanation']['message']
    assert all(c['action']=='result' for c in remote.calls)
    assert api.get('/api/work/conversations/'+conversation).json()['runs'] == [ORIGINAL,SECOND]


def test_repair_child_targets_original(saved):
    api,remote,conversation = saved
    value = api.post('/api/work/missions',json={'message':'じゃあ直して','conversation_id':conversation})
    assert value.status_code == 202
    assert remote.calls[-1] == {'action':'continue','run_id':ORIGINAL,'request':'じゃあ直して'}


def test_advisor_subject_explicit(saved):
    api,remote,conversation = saved
    value = api.post('/api/work/missions',json={'message':'何を追加すればできる？','conversation_id':conversation}).json()
    assert value['kind'] == 'advisor'
    assert value['subject_run_id'] == ORIGINAL
    assert remote.calls[-1] == {'action':'advice','run_id':ORIGINAL}


def test_explanation_overrides_leftover_operation(saved):
    api,remote,conversation = saved
    value = api.post('/api/work/missions',json={'message':'なぜ失敗した？','operation':'repair','conversation_id':conversation}).json()
    assert value['kind'] == 'explanation'
    assert all(c['action']=='result' for c in remote.calls)


def test_completed_followup_uses_current_artifact(saved):
    api,remote,conversation = saved
    original_call = remote.call
    async def call(payload):
        result = await original_call(payload)
        if payload['action']=='result' and payload['run_id']==ORIGINAL:
            return {**result,'status':'COMPLETED','artifact':{'artifact_id':'current-artifact'}}
        return result
    remote.call = call
    api.post('/api/work/missions',json={'message':'これを変更して','conversation_id':conversation})
    assert remote.calls[-1]['action'] == 'submit'
    assert remote.calls[-1]['operation'] == 'modify'
    assert remote.calls[-1]['artifact_id'] == 'current-artifact'


def test_ambiguous_change_does_not_dispatch(saved):
    api,remote,conversation = saved
    value = api.post('/api/work/missions',json={'message':'両方を削除して','conversation_id':conversation}).json()
    assert value['kind'] == 'confirmation'
    assert all(c['action']=='result' for c in remote.calls)


@pytest.mark.parametrize('text', ['なぜ失敗した？','何がダメだった？','どうして完成しなかった？','詳しく教えて'])
def test_finite_explanation_intent(text): assert intent(text)=='explain'


def test_new_work_is_not_explanation():
    assert intent('足し算アプリ作って') == 'new'
    assert intent('これを変更して') == 'modify'
