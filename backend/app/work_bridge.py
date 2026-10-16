"""UI-only asynchronous client of Pitchi-work's official finite JSON gateway."""
import asyncio
import json
import os
from pathlib import Path
import tomllib
from uuid import UUID, uuid4
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from backend.app.tools.work_config import source_digest
from backend.app.tools.guideline_work import GuidelineClient, PROJECT as GUIDELINE_PROJECT
from backend.app.work_explanation import HumanExplanationLayer, explanation
from backend.app.work_followup import intent


class MissionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conversation_id: UUID | None = None
    message: str = Field(min_length=1, max_length=4000)
    project: str = Field(default='', max_length=80)
    operation: Literal['create', 'repair', 'modify', 'clone', 'continue'] = 'create'


class WorkClient:
    def __init__(self, config=None):
        if config is None:
            path = os.environ.get('PITCH_CHAT_HEADLESS_CONFIG')
            config = tomllib.loads(Path(path).read_text()) if path else {}
        self.config = config
        self.guidelines = GuidelineClient()

    async def call(self, payload):
        cfg = self.config
        if self.guidelines.enabled:
            if payload.get('project') in (GUIDELINE_PROJECT,'ptotsearch-quality','ptotsearch-national') or str(payload.get('run_id','')).startswith('gl-'):
                return await self.guidelines.call(payload)
            if payload.get('action') == 'catalog':
                tool = await self.guidelines.call({'action':'catalog'})
                legacy_ok = cfg.get('enabled') and source_digest(Path(cfg['source_root'])) == cfg['reviewed_source_sha256']
                if not legacy_ok:
                    return {**tool,'legacy_available':False,'legacy_source_review_required':bool(cfg.get('enabled'))}
                old = await self._legacy_call(payload)
                return {**old,'projects':list(dict.fromkeys([*old['projects'],*tool['projects']])),'guideline_tool':tool}
        return await self._legacy_call(payload)

    async def _legacy_call(self,payload):
        cfg = self.config
        if not cfg.get('enabled'): raise HTTPException(503, 'Pitchi-work接続は無効です。')
        source = Path(cfg['source_root'])
        if source_digest(source) != cfg['reviewed_source_sha256']:
            raise HTTPException(503, {'code': 'PITCHI_WORK_SOURCE_REAPPROVAL_REQUIRED',
                                      'message': '新しいPitchi-workソースの再承認が必要です。'})
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'HOME', 'XDG_RUNTIME_DIR') if key in os.environ}
        env.update(PYTHONPATH=str(source / 'src'), PYTHONDONTWRITEBYTECODE='1')
        process = await asyncio.create_subprocess_exec(
            cfg['python_executable'], '-B', '-m', 'pitchi_work.headless', '--config', cfg['gateway_config'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError: process.kill(); await process.wait()
            raise HTTPException(504, '短時間受付がtimeoutしました。再送前に状態を確認してください。') from None
        if len(output) > 1024 * 1024: raise HTTPException(502, 'Work response budget exceeded')
        try: response = json.loads(output)
        except ValueError: raise HTTPException(502, 'Work protocol error') from None
        if not response.get('ok'): raise HTTPException(409, response.get('error', 'Work rejected request'))
        return response['result']


def router(store, client, explainer=None):
    explainer = explainer or HumanExplanationLayer()
    def presented(result):
        return {**result, 'human_explanation': explanation(result)}
    def private_response(response: Response): response.headers['Cache-Control'] = 'no-store'
    routes = APIRouter(prefix='/api/work', dependencies=[Depends(private_response)])

    def initialize():
        with store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS work_context (
                conversation_id TEXT PRIMARY KEY, artifact_id TEXT, run_id TEXT);
                CREATE TABLE IF NOT EXISTS work_runs (
                run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL);''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(work_context)')}
            for column in ('subject_run_id', 'latest_run_status', 'latest_operation', 'result_reference'):
                if column not in columns:
                    db.execute('ALTER TABLE work_context ADD COLUMN ' + column + ' TEXT')

    def same_origin(request):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, 'Cross-origin mission submission denied')

    def context(identifier):
        initialize()
        with store.connection() as db:
            row = db.execute('SELECT * FROM work_context WHERE conversation_id=?', (identifier,)).fetchone()
            return dict(row) if row else {}

    def known(run_id):
        initialize()
        with store.connection() as db:
            row = db.execute('SELECT conversation_id FROM work_runs WHERE run_id=?', (run_id,)).fetchone()
        if not row: raise HTTPException(404, 'Chatから送信したrunではありません。')
        return row['conversation_id']

    async def subject(conversation, current):
        identifier = current.get('subject_run_id')
        if identifier and known(identifier) == conversation:
            return await client.call({'action': 'result', 'run_id': identifier})
        with store.connection() as db:
            ids = [r['run_id'] for r in db.execute(
                'SELECT run_id FROM work_runs WHERE conversation_id=? ORDER BY rowid DESC LIMIT 8', (conversation,))]
        for identifier in ids:
            result = await client.call({'action': 'result', 'run_id': identifier})
            # Recover pre-fix conversations without treating an explanation mission as the work subject.
            if intent(result.get('original_request') or '') in {'explain', 'advice'}:
                continue
            with store.connection() as db:
                db.execute('UPDATE work_context SET subject_run_id=? WHERE conversation_id=?', (identifier, conversation))
            return result
        return None

    @routes.get('/catalog')
    async def catalog():
        if not client.config.get('enabled') and not getattr(getattr(client,'guidelines',None),'enabled',False): return {'enabled': False, 'projects': []}
        return {'enabled': True, **await client.call({'action': 'catalog'}),
                'source_digest': client.config.get('reviewed_source_sha256', '')}

    @routes.post('/missions', status_code=202)
    async def submit(body: MissionInput, request: Request):
        same_origin(request)
        if not body.message.strip(): raise HTTPException(422, 'Blank mission')
        conversation = str(body.conversation_id or uuid4())
        if body.conversation_id and store.history(conversation) is None:
            raise HTTPException(404, 'Unknown conversation')
        current = context(conversation)
        national_request = any(word in body.message for word in ('全国カバレッジ', '全国収集'))
        if national_request and body.project not in ('', GUIDELINE_PROJECT, 'ptotsearch-national'):
            raise HTTPException(409, '全国カバレッジの依頼と選択能力が一致しません。実行前に停止しました。')
        if national_request and body.project in ('', GUIDELINE_PROJECT):
            body = body.model_copy(update={'project':'ptotsearch-national'})
        quality_request = intent(body.message)=='new' and '検索' in body.message and any(word in body.message for word in ('改善','無関係','調べて'))
        if quality_request and not national_request and body.project in ('',GUIDELINE_PROJECT):
            body = body.model_copy(update={'project':'ptotsearch-quality'})
        followup = 'new' if quality_request or national_request else intent(body.message)
        target = await subject(conversation, current) if current and followup != 'new' else None
        if followup in {'explain', 'confirm', 'advice'} or (followup in {'repair', 'modify'} and not target):
            if followup == 'explain' and target:
                human = await explainer.explain(target)
                kind = 'explanation'
            elif followup == 'advice' and target:
                advice = target.get('capability_advice')
                if not advice:
                    advisor = await client.call({'action': 'advice', 'run_id': target['run_id']})
                    human = {'message': '元の仕事の失敗を対象に、必要な能力と対応案を分析しています。元の仕事は変更しません。'}
                    store.save_turn(conversation, body.message, human['message'])
                    return {'kind': 'advisor', 'conversation_id': conversation,
                            'subject_run_id': target['run_id'], 'advisor': advisor,
                            'human_explanation': human, 'source_result': target}
                human = {'message': '元の仕事の対応案を示します。提案やコマンドは自動実行しません。' if advice else
                         'この仕事の能力分析はまだ保存されていません。元の失敗を対象とした分析が必要です。修正を依頼する場合は「直して」と伝えてください。'}
                kind = 'advice'
            else:
                human = {'message': '対象の仕事・成果物と、希望する操作を確認させてください。どれを対象にしますか？'}
                kind = 'confirmation'
            store.save_turn(conversation, body.message, human['message'])
            return {'kind': kind, 'conversation_id': conversation, 'subject_run_id': target['run_id'] if target else None,
                    'human_explanation': human, 'source_result': target}
        operation = 'create' if body.project in (GUIDELINE_PROJECT,'ptotsearch-quality','ptotsearch-national') else body.operation
        if target and followup in {'repair', 'modify'}:
            if target['status'] == 'RUNNING':
                raise HTTPException(409, '対象の仕事は実行中です。停止または完了後に変更してください。')
            operation = followup if target.get('artifact') else 'continue'
            current = {**current, 'run_id': target['run_id'],
                       'artifact_id': (target.get('artifact') or {}).get('artifact_id')}
        payload = {'action': 'submit', 'request': body.message, 'project': body.project, 'operation': operation}
        if operation == 'continue':
            if not current.get('run_id'): raise HTTPException(409, '継続対象runがありません。')
            payload = {'action': 'continue', 'request': body.message, 'run_id': current['run_id']}
        elif operation != 'create':
            if not current.get('artifact_id'): raise HTTPException(409, '対象Artifactがありません。')
            payload['artifact_id'] = current['artifact_id']
        result = await client.call(payload)
        if result.get('reused_result'):
            # Work selects an already-known verified run. Reuse its conversation
            # binding; never insert the same run ID or launch a replacement job.
            conversation=known(result['run_id'])
            store.save_turn(conversation,body.message,'保存済みの成功結果を参照しました。新規Host実行・LLM呼出しはありません。')
            return {'conversation_id':conversation,**presented(result)}
        store.save_turn(conversation, body.message, '仕事を受け付けました。進行状況をお知らせします。')
        with store.connection() as db:
            db.execute('INSERT INTO work_runs VALUES (?,?)', (result['run_id'], conversation))
            db.execute('INSERT INTO work_context (conversation_id,artifact_id,run_id) VALUES (?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET run_id=excluded.run_id',
                       (conversation, current.get('artifact_id'), result['run_id']))
            db.execute('UPDATE work_context SET subject_run_id=?,latest_run_status=?,latest_operation=?,result_reference=? WHERE conversation_id=?',
                       (result['run_id'], result['status'], operation, '/api/work/missions/'+result['run_id']+'/result', conversation))
        return {'conversation_id': conversation, **presented(result)}

    @routes.get('/missions/{run_id}')
    @routes.get('/missions/{run_id}/result')
    async def status(run_id: str):
        conversation = known(run_id)
        result = await client.call({'action': 'result', 'run_id': run_id})
        with store.connection() as db:
            db.execute('UPDATE work_context SET latest_run_status=? WHERE conversation_id=? AND run_id=?',
                       (result['status'], conversation, run_id))
        if result['status'] == 'COMPLETED' and result.get('artifact'):
            with store.connection() as db:
                db.execute('UPDATE work_context SET artifact_id=? WHERE conversation_id=? AND run_id=?',
                           (result['artifact']['artifact_id'], conversation, run_id))
        return {'conversation_id': conversation, **presented(result)}

    class ApprovalInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
        challenge: str = Field(pattern=r'^[a-f0-9]{32}$')
        confirmation: str = Field(min_length=1,max_length=120)

    @routes.post('/missions/{run_id}/approve',status_code=202)
    async def approve(run_id: str,body: ApprovalInput,request: Request):
        same_origin(request); known(run_id)
        if not request.headers.get('origin') or request.headers.get('x-pitchi-human-approval') != body.challenge:
            raise HTTPException(403,'Explicit same-origin human approval header required')
        if not run_id.startswith('gl-') or not getattr(client,'guidelines',None):raise HTTPException(409,'Not a guideline publication task')
        result=await client.guidelines.call({'run_id':run_id,**body.model_dump()},publication=True)
        return presented(result)

    @routes.get('/missions/{run_id}/explanation')
    async def human_explanation(run_id: str):
        known(run_id)
        result = await client.call({'action': 'result', 'run_id': run_id})
        return {'run_id': run_id, 'status': result['status'],
                'human_explanation': await explainer.explain(result)}

    @routes.get('/missions/{run_id}/advice/{advisor_id}')
    async def advisor_result(run_id: str, advisor_id: str):
        known(run_id)
        return await client.call({'action': 'advice_result', 'run_id': run_id, 'advisor_run_id': advisor_id})

    @routes.post('/missions/{run_id}/cancel')
    async def cancel(run_id: str, request: Request):
        same_origin(request); known(run_id)
        return presented(await client.call({'action': 'cancel', 'run_id': run_id}))

    @routes.get('/conversations/{conversation_id}')
    async def history(conversation_id: UUID):
        identifier = str(conversation_id)
        initialize()
        with store.connection() as db:
            runs = [row['run_id'] for row in db.execute('SELECT run_id FROM work_runs WHERE conversation_id=? ORDER BY rowid', (identifier,))]
        return {'context': context(identifier), 'runs': runs}

    return routes
