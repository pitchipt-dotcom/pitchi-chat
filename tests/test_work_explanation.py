"""Presentation tests, including opt-in read-only actual saved-run replay."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from backend.app.work_explanation import HumanExplanationLayer, explanation


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def dom(result):
    shown = {**result, 'human_explanation': explanation(result)}
    process = subprocess.run(['node', 'tests/work_explanation_dom.cjs'], input=json.dumps(shown),
                             text=True, capture_output=True, timeout=10)
    assert process.returncode == 0, process.stdout + process.stderr


@pytest.mark.parametrize('status', ['RUNNING', 'COMPLETED', 'FAILED', 'NEEDS_HUMAN', 'NEEDS_CAPABILITY'])
def test_all_states(status):
    result = {'run_id': '20260101_000000_abcdef', 'status': status, 'native_status': status,
              'message': '{"validator":"private_internal_failure"}', 'failure_class': 'INTERNAL_FAILURE'}
    before = copy.deepcopy(result)
    dom(result)
    assert result == before


def test_registered_document_and_saved_search_ui():
    dom({'run_id':'gl-'+'a'*32,'status':'COMPLETED','tool_task':True,
         'result_details':{'documents':[{'title':'Synthetic guideline','issuer':'Synthetic society',
             'edition':'1','revision_year':2024,'official_url':'https://example.org/guideline'}],
             'change_count':0,'duplicate_check':'same metadata; no register','diff':'変更なし',
             'verification':{'passed':True}}})
    dom({'run_id':'gl-'+'b'*32,'status':'NEEDS_HUMAN','tool_task':True,'reused_result':True,
         'approval':{'sha256':'c'*64,'challenge':'d'*32,'execution_enabled':False,'plan':{
             'kind':'search_quality','documents':[],'change_count':1,'database_changes':0,
             'duplicate_check':'already improved','files':{'fixture.php':{}},'model':'Synthetic',
             'host_run_id':'synthetic-host','search_summary':{'before_after':{
                 'query':'Synthetic query','before_ids':['a'],'after_ids':[]}},'diff':'Synthetic diff',
             'publication_block_reason':'Already improved; no apply','backup':{'status':'not needed'},
             'rollback':'No writes','isolated_verification':{'passed':True,'checks':20}}}})


def test_unmet_guideline_discovery_display_is_not_success_or_object_string():
    dom({'run_id':'gl-'+'e'*32,'status':'NEEDS_HUMAN','tool_task':True,
         'original_request':'イレウスの国内ガイドラインを探してください',
         'result_details':{'kind':'guideline_discovery','documents':[], 'change_count':0,
             'duplicate_check':{}, 'diff':{}, 'verification':{'production_writes':0},
             'discovery':{'conditions':{'disease_groups':[['イレウス','腸閉塞']], 'country':'JP',
                 'language_required':None,'language_preference':['ja'],'unresolved_subject':False},
                 'objective':{'processing_success':True,'goal_met':False,'confirmed_count':0,
                     'unregistered_count':0,'unconfirmed_count':0},
                 'discovered_count':30,'relevant_candidate_count':0,
                 'searched_sources':[{'id':'synthetic','index_url':'https://example.jp/list','requests':2,'pages':{}}],
                 'result_origin':'fresh_official_list_scan','limitations':['未対応の情報源は巡回していません。']}}})


@pytest.mark.anyio
async def test_local_model_cannot_add_facts_or_rejudge():
    result = {'status': 'FAILED', 'failure_class': 'HOST_PROFILE_BROWSER_INTERACTION_FAIL'}
    calls = []
    async def local(variants):
        calls.append(variants)
        return '{"status":"COMPLETED","cause":"network broken"}'
    layer = HumanExplanationLayer(local)
    assert await layer.explain(result) == explanation(result)
    assert await layer.explain(result) == explanation(result)
    assert len(calls) == 1
    assert all('画面操作' in text for text in calls[0])
    async def valid(variants): return '1'
    answer = await HumanExplanationLayer(valid).explain(result)
    assert answer['source'] == 'local_wording_selection'
    assert answer['message'] == '状況をお知らせします。' + explanation(result)['message']


@pytest.mark.anyio
async def test_qwen_stopped_timeout_and_default_fallback():
    result = {'status': 'NEEDS_HUMAN', 'human_confirmation': {'reason': 'internal error'}}
    async def stopped(_): raise ConnectionRefusedError()
    async def slow(_): await asyncio.sleep(10)
    for selector in (None, stopped, slow):
        assert await HumanExplanationLayer(selector, timeout=.01).explain(result) == explanation(result)
    assert 'internal error' not in explanation(result)['message']


def test_explicit_human_question_not_error_reason():
    result = {'status': 'NEEDS_HUMAN', 'human_confirmation': {
        'question': '元の成果物を残して別版を作りますか？', 'choices': ['別版を作る', '今回はやめる'],
        'reason': 'validator internal'}}
    assert explanation(result)['choices'] == result['human_confirmation']['choices']
    assert explanation(result)['question'] == result['human_confirmation']['question']
    result['human_confirmation']['question'] = '{"validator":"ERROR"}'
    assert 'question' not in explanation(result)


def test_actual_saved_failed_and_capability_results(monkeypatch):
    root = os.environ.get('PITCH_CHAT_WORK_SAVED_HOME')
    if not root: pytest.skip('Actual saved Work home not supplied; no synthetic substitution')
    home = Path(root)
    monkeypatch.syspath_prepend(str(home / 'src'))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    from pitchi_work.headless import view
    from pitchi_work.store import RunStore
    ids = ['20261008_194717_2bdd9f', '20261008_200415_2b4496',
           '20261008_200236_af4d41', '20261008_195637_572a38']
    for identifier in ids:
        run = RunStore(home / 'runs' / identifier)
        files = [run.path / 'state.json', *sorted((run.path / 'artifacts').rglob('*.json')),
                 *sorted((run.path / 'logs').rglob('*'))]
        def digest():
            return {str(p.relative_to(run.path)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in files if p.is_file()}
        before = digest()
        result = view(run)
        raw = copy.deepcopy(result)
        dom(result)
        human = explanation(result)
        if result['status'] == 'FAILED':
            assert '画面操作の確認' in human['message']
        if result['status'] == 'NEEDS_CAPABILITY':
            assert 'Pythonプログラム' in human['message']
        assert raw == result and digest() == before
