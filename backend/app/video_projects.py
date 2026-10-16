"""Conversation-owned clip lineage and bounded CPU assembly in the existing worker."""
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from backend.app.harness.contracts import Artifact, Contract, ExecutionResult, FinalStatus, ToolResult
from backend.app.harness.recovery import HarnessFailure
from backend.app.harness.policy import authorized_path
from backend.app.jobs.contracts import JobRecord
from backend.app.media import MediaRequest
from backend.app.image_assets import same_origin
from backend.app.tools.comfy_video import checked_process, validate_clip


class Extension(Contract):
    prompt: str = Field(min_length=1, max_length=4000)
    prompt_mode: str = Field(default='direct', pattern=r'^(direct|strata|qwen35)$')
    backend: str | None = Field(default=None, pattern=r'^(ltx-video|minimax-h3)$')
    seed: int | None = Field(default=None, ge=0, le=9223372036854775807, strict=True)


class Assembly(Contract):
    clip_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=1, max_length=60)


class VideoProjects:
    def __init__(self, history, images):
        self.history, self.images = history, images
        with history.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS video_projects (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            db.execute('''CREATE TABLE IF NOT EXISTS video_clips (
                clip_id TEXT PRIMARY KEY REFERENCES tool_jobs(job_id), project_id TEXT NOT NULL REFERENCES video_projects(id),
                previous_clip_id TEXT REFERENCES video_clips(clip_id), retry_of_clip_id TEXT REFERENCES video_clips(clip_id),
                start_frame INTEGER NOT NULL, end_frame INTEGER NOT NULL,
                last_frame_image_id TEXT REFERENCES image_assets(id), mp4_sha256 TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')

    def clip(self, ident):
        ident = str(UUID(str(ident)))
        with self.history.connection() as db:
            row = db.execute('''SELECT c.*, p.conversation_id, j.status,j.error,j.image_request_json,
                j.image_spec_json,j.result_json,j.media_phase FROM video_clips c JOIN video_projects p ON p.id=c.project_id
                JOIN tool_jobs j ON j.job_id=c.clip_id WHERE c.clip_id=?''', (ident,)).fetchone()
        if row is None:
            raise ValueError('clip_missing')
        return dict(row)

    def bind(self, request):
        if not request.conversation_id:
            if request.project_id or request.previous_clip_id:
                raise ValueError('video_conversation_required')
            return request  # Legacy non-conversation media clients retain their contract.
        cid = str(request.conversation_id)
        if self.history.history(cid) is None:
            raise ValueError('conversation_missing')
        pid = str(request.project_id) if request.project_id else str(uuid4())
        if request.previous_clip_id:
            parent = self.clip(request.previous_clip_id)
            if (parent['conversation_id'] != cid or parent['project_id'] != pid or parent['status'] != 'SUCCESS'
                    or not parent['last_frame_image_id'] or parent['end_frame'] >= 4320):
                raise ValueError('completed_owned_previous_clip_required')
            tail = UUID(parent['last_frame_image_id'])
            if request.reference_image_id and request.reference_image_id != tail:
                raise ValueError('continuation_reference_mismatch')
            request = request.model_copy(update={'reference_image_id': tail})
        with self.history.connection() as db:
            project = db.execute('SELECT conversation_id FROM video_projects WHERE id=?', (pid,)).fetchone()
            if project is None:
                if request.project_id:
                    raise ValueError('project_missing')
                db.execute('INSERT INTO video_projects(id,conversation_id) VALUES(?,?)', (pid,cid))
            elif project[0] != cid:
                raise ValueError('project_not_in_conversation')
            if db.execute('SELECT count(*) FROM video_clips WHERE project_id=?', (pid,)).fetchone()[0] >= 180:
                raise ValueError('project_attempt_limit')
        return request.model_copy(update={'project_id': UUID(pid)})

    def register(self, job_id, request, backend, *, retry_of=None):
        if not request.project_id:
            return
        previous = self.clip(request.previous_clip_id) if request.previous_clip_id else None
        start = previous['end_frame'] if previous else 0
        with self.history.connection() as db:
            db.execute('''INSERT INTO video_clips(clip_id,project_id,previous_clip_id,retry_of_clip_id,start_frame,end_frame)
                VALUES(?,?,?,?,?,?)''', (job_id,str(request.project_id),str(request.previous_clip_id) if previous else None,
                retry_of,start,start+backend.frames))

    def extension_request(self, clip_id, body):
        parent = self.clip(clip_id)
        original = MediaRequest.model_validate_json(parent['image_request_json'])
        return original.model_copy(update={'prompt': body.prompt, 'prompt_mode': body.prompt_mode,
            'backend': body.backend or original.backend, 'seed': body.seed, 'preview_only': False,
            'previous_clip_id': UUID(clip_id), 'reference_image_id': None})

    async def complete(self, job_id, result, definition):
        row = self.clip(job_id)
        if not result.tool_result or len(result.tool_result.artifacts) != 1:
            raise ValueError('one_clip_artifact_required')
        path = authorized_path(result.tool_result.artifacts[0].path, definition.allowed_roots)
        # Decode the exact last video frame. No client paths or URLs enter FFmpeg.
        plan = json.loads(row['image_spec_json'])
        raw = await checked_process('ffmpeg','-v','error','-i',str(path),'-vf',
            f"select=eq(n\\,{plan['frames']-1})",'-frames:v','1','-f','image2pipe','-vcodec','png','-')
        if not raw or len(raw) > 20*1024*1024:
            raise ValueError('last_frame_decode_failed')
        request = MediaRequest.model_validate_json(row['image_request_json'])
        image = await asyncio.to_thread(self.images.save, raw,row['conversation_id'],
            parent_id=request.reference_image_id,source_job_id=job_id,source_kind='video_frame')
        digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        with self.history.connection() as db:
            db.execute('UPDATE video_clips SET last_frame_image_id=?,mp4_sha256=? WHERE clip_id=?', (image['id'],digest,job_id))
        return {'clip_id':job_id,'project_id':row['project_id'],'previous_clip_id':row['previous_clip_id'],
                'start_frame':row['start_frame'],'end_frame':row['end_frame'],'last_frame_image':image}

    def public(self, row):
        request = json.loads(row['image_request_json']);plan = json.loads(row['image_spec_json']) if row['image_spec_json'] else {}
        with self.history.connection() as db:
            next_clips = [r[0] for r in db.execute('SELECT clip_id FROM video_clips WHERE previous_clip_id=? ORDER BY created_at,clip_id',(row['clip_id'],))]
            failure = db.execute("SELECT details_json FROM media_events WHERE job_id=? AND phase='FAILED' ORDER BY id DESC LIMIT 1",(row['clip_id'],)).fetchone()
        return {k:row[k] for k in ['clip_id','project_id','previous_clip_id','retry_of_clip_id','start_frame','end_frame',
            'last_frame_image_id','status','error','created_at']} | {
            'prompt':request['prompt'],'prompt_mode':request['prompt_mode'],'reference_image_id':request.get('reference_image_id'),
            'backend':request['backend'],'seed':plan.get('seed',request.get('seed')),
            'next_clip_ids':next_clips,'phase':row['media_phase'],'failure':json.loads(failure[0]) if failure else None,
            'settings':plan.get('generation_settings'), 'positive_prompt':plan.get('positive_prompt'),
            'video_url':f"/api/jobs/{row['clip_id']}/artifacts/0" if row['status']=='SUCCESS' else None}

    def timeline(self, cid):
        with self.history.connection() as db:
            projects = [dict(r) for r in db.execute('SELECT * FROM video_projects WHERE conversation_id=? ORDER BY created_at,id', (str(cid),))]
            ids = [r[0] for r in db.execute('''SELECT c.clip_id FROM video_clips c JOIN video_projects p ON p.id=c.project_id
                JOIN tool_jobs j ON j.job_id=c.clip_id WHERE p.conversation_id=? ORDER BY j.rowid''', (str(cid),))]
            assemblies = [dict(r) for r in db.execute('''SELECT j.job_id,j.status,j.error FROM tool_jobs j
                JOIN video_projects p ON json_extract(j.image_request_json,'$.project_id')=p.id
                WHERE p.conversation_id=? AND j.job_kind='video_concat' ORDER BY j.rowid''', (str(cid),))]
        with self.history.connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='long_video_members'").fetchone():
                long_ids={r[0] for r in db.execute('SELECT job_id FROM long_video_members')}
                ids=[i for i in ids if i not in long_ids]
        return {'projects':projects,'clips':[self.public(self.clip(i)) for i in ids if not self.history.is_deleted(i)],
                'assemblies':[r for r in assemblies if not self.history.is_deleted(r['job_id'])]}

    def assembly_clips(self, project_id, clip_ids, definition):
        if not definition or not definition.enabled or not 1 <= len(clip_ids) <= 60 or len(set(map(str,clip_ids))) != len(clip_ids):
            raise ValueError('invalid_assembly')
        rows = [self.clip(i) for i in clip_ids]
        paths = []
        for row in rows:
            if row['project_id'] != str(project_id) or row['status'] != 'SUCCESS' or not row['mp4_sha256']:
                raise ValueError('completed_project_clips_required')
            result = json.loads(row['result_json']);artifacts = result['tool_result']['artifacts']
            if len(artifacts) != 1:
                raise ValueError('one_clip_artifact_required')
            path = authorized_path(artifacts[0]['path'], definition.allowed_roots)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64*1024*1024:
                raise ValueError('invalid_clip_file')
            paths.append(path)
        return rows,paths

    async def concatenate(self, body, definition):
        rows,paths = self.assembly_clips(body['project_id'], body['clip_ids'], definition)
        # Verify immutable inputs again when the queued CPU operation actually executes.
        for row,path in zip(rows,paths):
            if await asyncio.to_thread(lambda p=path: hashlib.sha256(p.read_bytes()).hexdigest()) != row['mp4_sha256']:
                raise ValueError('clip_hash_changed')
        from backend.app.tools.comfy_video import checked_process
        store = definition.adapter.artifacts
        store.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        dest = store.root / (uuid4().hex+'.mp4')
        try:
            with tempfile.TemporaryDirectory(prefix='pitch-video-join-') as tmp:
                manifest = Path(tmp)/'concat.txt'
                manifest.write_text(''.join("file '"+str(p).replace("'", "'\\''")+"'\n" for p in paths))
                await checked_process('ffmpeg','-v','error','-f','concat','-safe','0','-i',str(manifest),
                    '-map','0:v:0','-an','-vf','scale=608:352,setsar=1','-r','24',
                    '-frames:v','4320','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p',str(dest), timeout=180)
            frames = min(4320,sum(json.loads(row['image_spec_json'])['frames'] for row in rows))
            await validate_clip(dest, frames=frames, require_audio=False)
            os.chmod(dest,0o600)
            return ExecutionResult(request_id=uuid4(),status=FinalStatus.SUCCESS,
                image_spec={'media_kind':'video_concat','project_id':str(body['project_id']),
                    'clip_ids':body['clip_ids'],'frames':frames,'fps':24,'audio':False},
                tool_result=ToolResult(success=True,result_type='run',http_status=200,run_status='completed',
                    run_id=uuid4().hex,output={'status':'completed','artifact_count':1},artifacts=(Artifact(path=str(dest),kind='file'),)))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise


def router():
    routes = APIRouter()

    @routes.get('/api/conversations/{cid}/videos')
    async def timeline(cid: UUID, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return request.app.state.video_projects.timeline(cid)

    @routes.post('/api/videos/clips/{clip_id}/extend',status_code=202,response_model=JobRecord)
    async def extend(clip_id: UUID, body: Extension, request: Request):
        same_origin(request)
        try:
            payload = request.app.state.video_projects.extension_request(str(clip_id),body)
            return JobRecord.model_validate(request.app.state.worker.submit_media(payload),strict=False)
        except (ValueError, HarnessFailure):
            raise HTTPException(409,'完了済みクリップ・参照画像・リソース条件を確認してください。') from None

    @routes.post('/api/videos/projects/{project_id}/concatenate',status_code=202,response_model=JobRecord)
    async def concatenate(project_id: UUID, body: Assembly, request: Request):
        same_origin(request)
        try:
            return JobRecord.model_validate(request.app.state.worker.submit_video_concat(str(project_id),list(map(str,body.clip_ids))),strict=False)
        except ValueError:
            raise HTTPException(409,'同じプロジェクトの完了済みクリップを順番に選んでください。') from None

    return routes
