"""Finite gateway/UI boundary; never uses an LLM or evaluates generated code."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app.storage.sqlite import HistoryStore
from backend.app.work_bridge import router
from backend.app.work_bridge import WorkClient


class Client:
    config = {'enabled': True}
    def __init__(self): self.calls = []; self.status = 'RUNNING'; self.sequence = 0
    async def call(self, payload):
        self.calls.append(payload)
        if payload['action'] == 'catalog': return {'projects': ['demo']}
        if payload['action'] in ('submit', 'continue'): self.sequence += 1
        return {'run_id': payload.get('run_id') if payload['action'] not in ('submit', 'continue') else f'20260101_000000_{self.sequence:06x}', 'status': self.status, 'native_status': self.status,
                'message': 'ok', 'artifact': {'artifact_id': 'host-id', 'type': 'static_web_app',
                                             'preview_url': 'http://localhost/artifacts/host-id/'} if self.status == 'COMPLETED' else None}


@pytest.fixture
def bridge(tmp_path):
    store = HistoryStore(tmp_path / 'chat.sqlite3'); store.initialize()
    remote = Client(); app = FastAPI(); app.include_router(router(store, remote))
    with TestClient(app) as api: yield api, remote


def test_async_run_and_artifact_context(bridge):
    api, remote = bridge
    result = api.post('/api/work/missions', json={'message': 'make app', 'project': 'demo'})
    assert result.status_code == 202
    data = result.json(); assert data['status'] == 'RUNNING'
    remote.status = 'COMPLETED'
    assert api.get('/api/work/missions/' + data['run_id']).json()['artifact']['artifact_id'] == 'host-id'
    context = api.get('/api/work/conversations/' + data['conversation_id']).json()
    assert context['context']['artifact_id'] == 'host-id'
    remote.status = 'RUNNING'
    api.post('/api/work/missions', json={'conversation_id': data['conversation_id'], 'message': 'repair', 'operation': 'repair'})
    assert remote.calls[-1]['artifact_id'] == 'host-id'


def test_no_shell_or_unbound_target(bridge):
    api, remote = bridge
    assert api.post('/api/work/missions', json={'message': 'x', 'shell': 'anything'}).status_code == 422
    assert api.post('/api/work/missions', json={'message': 'x', 'operation': 'clone'}).status_code == 409
    assert api.post('/api/work/missions', headers={'Origin': 'https://evil.test'}, json={'message': 'x'}).status_code == 403
    assert api.get('/api/work/missions/unknown').status_code == 404
    assert remote.calls == []


def test_native_capability_and_human_not_inferred(bridge):
    api, remote = bridge
    data = api.post('/api/work/missions', json={'message': 'x'}).json()
    for state in ('NEEDS_CAPABILITY', 'NEEDS_HUMAN', 'FAILED'):
        remote.status = state
        assert api.get('/api/work/missions/' + data['run_id']).json()['status'] == state
    remote.status = 'RUNNING'
    api.post('/api/work/missions', json={'conversation_id': data['conversation_id'], 'message': 'answer', 'operation': 'continue'})
    assert remote.calls[-1] == {'action': 'continue', 'run_id': data['run_id'], 'request': 'answer'}


def test_explanation_is_additive_and_private(bridge):
    api, remote = bridge
    data = api.post('/api/work/missions', json={'message': 'x'}).json()
    remote.status = 'FAILED'
    response = api.get('/api/work/missions/' + data['run_id'])
    result = response.json()
    assert result['message'] == 'ok' and result['status'] == 'FAILED'
    assert '完了できません' in result['human_explanation']['message']
    human = api.get('/api/work/missions/' + data['run_id'] + '/explanation')
    assert human.headers['cache-control'] == 'no-store'
    assert human.json()['human_explanation'] == result['human_explanation']
    assert api.get('/api/work/missions/unknown/explanation').status_code == 404


def test_source_pin_mismatch_has_stable_reapproval_code(tmp_path):
    source = tmp_path / 'source'; (source / 'src/pitchi_work').mkdir(parents=True)
    (source / 'src/pitchi_work/__init__.py').write_text('VALUE = 1\n')
    client = WorkClient({'enabled': True, 'source_root': str(source), 'reviewed_source_sha256': '0' * 64})
    app = FastAPI(); app.include_router(router(HistoryStore(tmp_path / 'chat.sqlite3'), client))
    with TestClient(app) as api:
        response = api.get('/api/work/catalog')
    assert response.status_code == 503
    assert response.json()['detail'] == {
        'code': 'PITCHI_WORK_SOURCE_REAPPROVAL_REQUIRED',
        'message': '新しいPitchi-workソースの再承認が必要です。',
    }


def test_catalog_reports_digest_that_passed_pin_gate(bridge):
    api, remote = bridge
    remote.config = {**remote.config, 'reviewed_source_sha256': 'a' * 64}
    response = api.get('/api/work/catalog')
    assert response.status_code == 200
    assert response.json()['source_digest'] == 'a' * 64


def test_national_coverage_natural_request_uses_existing_work_endpoint(bridge):
    api, remote = bridge
    for text in ('全国カバレッジの収集状態を調べて', '全国収集が止まった原因を調べて', '承認済みの全国収集ジョブを途中再開して'):
        response = api.post('/api/work/missions', json={'message':text})
        assert response.status_code == 202
        assert remote.calls[-1]['project'] == 'ptotsearch-national'
        assert remote.calls[-1]['operation'] == 'create'
        assert remote.calls[-1]['request'] == text
        assert not any(call.get('action') == 'approve' for call in remote.calls)


def test_national_request_cannot_dispatch_to_search_quality(bridge):
    api, remote = bridge
    result = api.post('/api/work/missions',json={'message':'全国収集の停止原因を調べて','project':'ptotsearch-quality'})
    assert result.status_code == 409
    assert not remote.calls
