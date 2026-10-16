"""ID-only, resumable physical erasure of Host-owned conversation/media copies.

No recursive unlink, service calls, shell, or client-supplied paths. A failed
operation keeps its manifest and blocks reads/reuse until explicitly retried.
"""
import hashlib
import json
import os
import re
import stat
import sqlite3
import errno
from urllib.parse import parse_qs
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from typing import Literal

from backend.app.harness.contracts import Contract
from backend.app.image_assets import same_origin


class Target(Contract):
    kind: Literal['conversation', 'image', 'job', 'artifact', 'long_video']
    id: UUID = Field(strict=False)


class Selection(Contract):
    targets: list[Target] = Field(min_length=1, max_length=100)


class Confirmation(Contract):
    confirm_original_files: Literal[True]


class Refusal(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def ids(value):
    """Only exact UUID values, not substrings of prompts or arbitrary URLs."""
    if isinstance(value, dict):
        for item in value.values():
            yield from ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from ids(item)
    elif isinstance(value, str):
        try:
            yield str(UUID(value))
        except ValueError:
            pass


def payload(row):
    return json.loads(row.get('image_request_json') or '{}')


def conversation_id(row):
    value = payload(row)
    nested = value.get('request')
    return value.get('conversation_id') or (nested.get('conversation_id') if isinstance(nested,dict) else None)


def artifact_id(job_id, index):
    return str(uuid5(UUID(job_id), 'pitch-chat-artifact:'+str(index)))


def indexed_artifacts(row):
    values = (json.loads(row.get('result_json') or '{}').get('tool_result') or {}).get('artifacts', [])
    return [(n,a) for n,a in enumerate(values) if a and not a.get('deleted')]


def artifacts(row):
    return [a for _,a in indexed_artifacts(row)]


EXCLUSIONS = [
    'ComfyUI・音声/動画APIなど別サービス側の原本、入力コピー、履歴（正式なファイル削除APIが未確認）',
    '端末に保存したファイル、OS/外部バックアップ、既存の検証報告書のコピー',
    'Pitchi-workのrun・成果物、蔵書検索の資料、モデル・workflow・設定',
    '本文を含まない既存の運用監査と、今回の削除ID・状態・件数の監査記録',
]


class Deletions:
    def __init__(self, history):
        self.history = history
        self.roots = {name: history.path.parent / name for name in ('artifacts', 'image-assets')}
        self.identity = {}
        for name, root in self.roots.items():
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Pin the actual flat directory, never resolve an unexpected link.
            if root.is_symlink() or root.parent.is_symlink():
                raise RuntimeError('unsafe_media_root')
            st = root.stat()
            self.identity[name] = (st.st_dev, st.st_ino)
        with history.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS deletion_operations (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, plan_json TEXT,
                    report_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS deletion_tombstones (
                    kind TEXT NOT NULL, id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS deletion_audit (
                    id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, status TEXT NOT NULL,
                    file_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TRIGGER IF NOT EXISTS deleted_conversation_no_recreate BEFORE INSERT ON conversations
                    WHEN EXISTS(SELECT 1 FROM deletion_tombstones WHERE kind='conversation' AND id=NEW.id)
                    BEGIN SELECT RAISE(ABORT,'conversation_deleted'); END;
                CREATE TRIGGER IF NOT EXISTS deleted_chat_no_replay BEFORE INSERT ON chat_generation_jobs
                    WHEN EXISTS(SELECT 1 FROM deletion_tombstones WHERE
                        (kind='conversation' AND id=NEW.conversation_id) OR (kind='client' AND id=NEW.client_request_id))
                    BEGIN SELECT RAISE(ABORT,'conversation_deleted'); END;
            ''')
            # Process death after marking/unlink is an explicit retry, never a replay.
            db.execute("UPDATE deletion_operations SET status='PARTIAL' WHERE status='DELETING'")

    def snapshot(self, db):
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: [dict(r) for r in db.execute('SELECT * FROM '+name)] if name in names else []
                for name in ('conversations','image_assets','tool_jobs','video_projects','video_clips',
                             'chat_generation_jobs','work_context','work_runs','long_video_jobs','long_video_members','long_video_temp_files')}

    def deleted(self, ident):
        with self.history.connection() as db:
            return bool(db.execute('SELECT 1 FROM deletion_tombstones WHERE id=?', (ident,)).fetchone())

    def catalog(self):
        with self.history.connection() as db:
            data = self.snapshot(db)
            hidden = {r[0] for r in db.execute('SELECT id FROM deletion_tombstones')}
            pending = [self.public(dict(r)) for r in db.execute(
                "SELECT * FROM deletion_operations WHERE status IN ('PARTIAL','DELETING') ORDER BY created_at")]
        items = []
        for r in data['conversations']:
            items.append(dict(kind='conversation', id=r['id'], label=r['title'], created_at=r['created_at'], conversation_id=r['id']))
        mapped = {r['source_job_id'] for r in data['image_assets'] if r['source_kind'] != 'video_frame'}
        for r in data['image_assets']:
            items.append(dict(kind='image', id=r['id'], label={'upload':'添付画像','generated':'生成画像','video_frame':'動画の最終フレーム'}.get(r['source_kind'],'画像'),
                conversation_id=r['conversation_id'], created_at=r['created_at'], url=f"/api/images/attachments/{r['id']}/content"))
        long_children={m['job_id'] for m in data['long_video_members']}
        for r in data['tool_jobs']:
            if r['job_id'] in long_children or r['job_id'] in mapped or r['tool'] not in {'image.generate','media.video.generate','video.generate','tts.generate','image.understand'}:
                continue
            media = {'image.generate':'画像・指示案','media.video.generate':'動画','video.generate':'動画','tts.generate':'音声','image.understand':'画像理解'}[r['tool']]
            cid = conversation_id(r)
            outputs = indexed_artifacts(r)
            if len(outputs)>1:
                for index,artifact in outputs:
                    items.append(dict(kind='artifact',id=artifact_id(r['job_id'],index),
                        label=f"{media} {index+1}/{len(outputs)}",conversation_id=cid,created_at=r['created_at'],
                        url=f"/api/jobs/{r['job_id']}/artifacts/{index}",media_type='audio' if artifact['kind']=='audio' else 'video' if 'video' in r['tool'] else 'image'))
            else:
                index = outputs[0][0] if outputs else 0
                items.append(dict(kind='job',id=r['job_id'],label=f"{media} / {r['status']}",conversation_id=cid,
                    created_at=r['created_at'],url=f"/api/jobs/{r['job_id']}/artifacts/{index}" if outputs else None,
                    media_type='audio' if r['tool']=='tts.generate' else 'video' if 'video' in r['tool'] else 'image'))
        # Parent choices expose final-only versus dedicated intermediate cleanup.
        for parent in data['long_video_jobs']:
            items.append(dict(kind='long_video',id=parent['id'],label='長尺動画 / '+parent['status'],
                conversation_id=parent['conversation_id'],created_at=parent['created_at'],media_type='video',
                final_job_id=parent['final_job_id'],dedicated_intermediates=True))
        return {'items':[i for i in items if i['id'] not in hidden], 'pending':pending, 'exclusions':EXCLUSIONS}

    def idle(self, db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='long_video_jobs'").fetchone() and db.execute("SELECT 1 FROM long_video_jobs WHERE status IN ('PLANNING','RUNNING','PAUSE_REQUESTED','CANCEL_REQUESTED') LIMIT 1").fetchone():
            raise Refusal('長尺動画の実行工程があります。クリップ境界で一時停止してから削除してください。')
        if db.execute("SELECT 1 FROM tool_jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone():
            raise Refusal('生成中・キュー待ちの処理があります。完了後に再試行してください。')
        if db.execute("SELECT 1 FROM chat_generation_jobs WHERE status IN ('QUEUED','RUNNING','CANCELLING') LIMIT 1").fetchone():
            raise Refusal('会話を生成中です。完了後に再試行してください。')

    def plan(self, targets, db):
        data = self.snapshot(db)
        convs = {r['id']:r for r in data['conversations']}
        images = {r['id']:r for r in data['image_assets']}
        jobs = {r['job_id']:r for r in data['tool_jobs']}
        projects = {r['id']:r for r in data['video_projects']}
        cset, iset, jset = set(), set(), set()
        slots = {}
        lset=set()
        longs={r['id']:r for r in data['long_video_jobs']}
        known_slots = {artifact_id(jid,n):(jid,n) for jid,r in jobs.items()
            if r['tool'] in {'image.generate','media.video.generate','video.generate','tts.generate'}
            for n,a in indexed_artifacts(r)}
        for target in targets:
            kind, key = target['kind'], str(target['id'])
            if kind == 'conversation' and key in convs:
                cset.add(key)
            elif kind == 'long_video' and key in longs:
                lset.add(key)
            elif kind == 'image' and key in images:
                iset.add(key)
            elif kind == 'artifact' and key in known_slots:
                slots[key] = known_slots[key]
            elif kind == 'job' and key in jobs and jobs[key]['tool'] in {
                    'image.generate','media.video.generate','video.generate','tts.generate','image.understand'}:
                jset.add(key)
            else:
                raise Refusal('対象が見つからないか、このHostの削除対象ではありません。')
        lset.update(k for k,r in longs.items() if r['conversation_id'] in cset)
        jset.update(m['job_id'] for m in data['long_video_members'] if m['parent_id'] in lset)
        for r in images.values():
            if r['conversation_id'] in cset:
                iset.add(r['id'])
        for r in jobs.values():
            p = payload(r)
            cid = conversation_id(r)
            if cid in cset or projects.get(p.get('project_id'),{}).get('conversation_id') in cset:
                if r['tool'] not in {'image.generate','media.video.generate','video.generate','tts.generate','image.understand','video.long.plan'}:
                    raise Refusal('別サービスのジョブが関連付いています。管理者による範囲確認が必要です。')
                jset.add(r['job_id'])
        for jid,r in jobs.items():
            if indexed_artifacts(r) and all(artifact_id(jid,n) in slots for n,a in indexed_artifacts(r)):
                jset.add(jid)
        # Generated images and their source job are one Host artifact, video inputs are not.
        for key in list(iset):
            r = images[key]
            if r['source_kind'] != 'video_frame' and r['source_job_id'] in jobs:
                jset.add(r['source_job_id'])
        for member in data['long_video_members']:
            if member['job_id'] in jset and member['parent_id'] not in lset and member['role']!='assembly':
                raise Refusal('長尺動画の再開に必要な中間クリップです。親の「完成動画＋中間クリップ」を選択してください。')
        selected_job_ids = jset | {jid for jid,n in slots.values()}
        if any(jobs[k]['remote_phase'] in {'SUBMITTING','SUBMITTED'} and jobs[k]['status']=='RECOVERY_REQUIRED' for k in selected_job_ids):
            raise Refusal('外部生成の完了が不明です。正式な状態照合が完了するまで削除できません。')
        retained = []
        for r in images.values():
            if r['source_job_id'] in jset and r['source_kind'] != 'video_frame':
                iset.add(r['id'])
        # A video owns its tail frame, except a tail still used by another output.
        tails = {r['id'] for r in images.values() if r['source_kind']=='video_frame' and r['source_job_id'] in jset}
        def references(key):
            used = [r['job_id'] for r in jobs.values() if r['job_id'] not in jset and key in set(ids(payload(r))) |
                    set(ids(json.loads(r.get('image_spec_json') or '{}'))) |
                    set(ids(json.loads(r.get('call_json') or '{}')))]
            used += [r['id'] for r in images.values() if r['id'] not in iset | tails and r['parent_id']==key]
            used += [r['clip_id'] for r in data['video_clips'] if r['clip_id'] not in jset and r['last_frame_image_id']==key]
            used += [r['id'] for r in longs.values() if r['id'] not in lset and key in set(ids(json.loads(r['request_json']))) | {r['anchor_image_id']}]
            return used
        tails.update(r['anchor_image_id'] for k,r in longs.items() if k in lset and r['anchor_image_id'])
        for key in tails - iset:
            if references(key):
                retained.append({'kind':'image','id':key,'reason':'続きの動画・他の成果物が参照する最終フレームを保護'})
            else:
                iset.add(key)
        tails = iset.copy()
        for key in iset:
            if references(key):
                raise Refusal('選択画像を他の成果物・保存済み生成計画が参照しています。関連成果物も選択するか、先に削除してください。')
        # Work authority/files stay external. Unknown/live Work is a deletion blocker.
        for r in data['work_context']:
            if r['conversation_id'] in cset and r.get('latest_run_status') not in {'COMPLETED','FAILED','CANCELLED','ERROR','NEEDS_HUMAN','NEEDS_CAPABILITY'}:
                raise Refusal('Pitchi-workの実行状態を確認できない会話です。Workの完了確認が必要です。')
        files = {}
        def add(path, owner):
            p = Path(path)
            entry = files.setdefault(str(p), {'path':str(p), 'owners':[],
                'location':'Host編集参照PNG' if p.name.endswith('.input.png') else
                           'Host保存画像PNG' if p.parent==self.roots['image-assets'] else 'Host成果物コピー'})
            if owner not in entry['owners']:
                entry['owners'].append(owner)
        for receipt in data['long_video_temp_files']:
            if receipt['parent_id'] in lset:
                add(self.roots['artifacts']/receipt['name'],{'kind':'long_video','id':receipt['parent_id']})
        for key in iset:
            for suffix in ('.png','.input.png'):
                add(self.roots['image-assets'] / (UUID(key).hex+suffix), {'kind':'image','id':key})
        for jid in jset:
            for n,a in indexed_artifacts(jobs[jid]):
                slots[artifact_id(jid,n)] = (jid,n)
        selected_paths = {a['path'] for k,r in jobs.items() for n,a in indexed_artifacts(r) if artifact_id(k,n) in slots}
        retained_paths = {a['path'] for k,r in jobs.items() for n,a in indexed_artifacts(r) if artifact_id(k,n) not in slots}
        # Result JSON is not the only file reference: saved calls/requests may share files.
        def strings(value):
            if isinstance(value,dict):
                for v in value.values(): yield from strings(v)
            elif isinstance(value,list):
                for v in value: yield from strings(v)
            elif isinstance(value,str): yield value
        for k,r in jobs.items():
            if k not in jset:
                for column in ('call_json','image_request_json','image_spec_json'):
                    retained_paths.update(strings(json.loads(r.get(column) or '{}')))
        for path in selected_paths:
            if path in retained_paths:
                retained.append({'kind':'shared_file','reason':'別成果物が参照するHostファイルを保護'})
            else:
                for ident,(jid,n) in sorted(slots.items()):
                    if dict(indexed_artifacts(jobs[jid]))[n]['path']==path:
                        add(path, {'kind':'artifact','id':ident})
        for f in files.values():
            try:
                f['identity'] = self.file_identity(f['path'])
            except (OSError, Refusal):
                f['identity'] = 'unsafe'
        clients = [r['client_request_id'] for r in data['chat_generation_jobs'] if r['conversation_id'] in cset]
        project_ids = {r['project_id'] for r in data['video_clips'] if r['clip_id'] in jset}
        project_ids.update(payload(jobs[k]).get('project_id') for k in jset if payload(jobs[k]).get('project_id'))
        plan = {'long_videos':sorted(lset),'projects':sorted(project_ids),'targets':targets,'conversations':sorted(cset),'images':sorted(iset),'jobs':sorted(jset),
            'artifact_slots':[{'id':i,'job_id':j,'index':n} for i,(j,n) in sorted(slots.items())],
            'artifacts':sorted(slots),'clients':clients,'files':sorted(files.values(),key=lambda f:f['path']),'retained':retained}
        # Snapshot whole metadata rows being purged, not just file identities.
        plan['revision'] = digest({k:[r for r in data[k] if
            (r.get('id') in cset | iset or r.get('job_id') in selected_job_ids or r.get('conversation_id') in cset)]
            for k in data})
        return plan

    def directory(self, path):
        path = Path(path)
        name = next((n for n,r in self.roots.items() if path.parent == r),None)
        if name is None or not re.fullmatch(r'[a-f0-9]{32}(?:\.input)?\.(png|jpg|jpeg|webp|wav|mp4|txt)',path.name):
            raise Refusal('許可領域外・未確認のファイル名（削除しません）')
        fd = os.open(self.roots[name], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if (st.st_dev,st.st_ino) != self.identity[name]:
            os.close(fd)
            raise Refusal('保存領域の同一性が変化（削除しません）')
        return fd

    def file_identity(self, path):
        fd = self.directory(path)
        try:
            try:
                st = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise Refusal('リンク・非通常ファイルは削除しません')
            return [st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns]
        finally:
            os.close(fd)

    def unlink(self, entry):
        current = self.file_identity(entry['path'])
        if current is None:
            return  # Missing is idempotent, including crash after unlink before commit.
        if entry['identity'] == 'unsafe' or current != entry['identity']:
            raise Refusal('ファイル同一性不一致・安全な削除不可')
        fd = self.directory(entry['path'])
        try:
            os.unlink(Path(entry['path']).name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)

    def public(self, row):
        return {'operation_id':row['id'],'status':row['status'],**json.loads(row['report_json'])}

    def preview(self, targets):
        targets = sorted({(t.kind,str(t.id)) for t in targets})
        targets = [dict(kind=k,id=i) for k,i in targets]
        with self.history.connection() as db:
            self.idle(db)
            plan = self.plan(targets, db)
            key = str(uuid4())
            report = {'targets':targets,'scope':{k:len(plan[k]) for k in ('conversations','images','jobs','files')},
                'retained':plan['retained'],'exclusions':EXCLUSIONS,
                'warning':'選択した履歴・関連メタデータとHost管理の元ファイルも削除されます。元に戻せません。',
                'unsafe_files':sum(f['identity']=='unsafe' for f in plan['files'])}
            db.execute('INSERT INTO deletion_operations(id,status,plan_json,report_json) VALUES(?,?,?,?)',
                (key,'PREVIEW',encoded(plan),encoded(report)))
            # Discard abandoned preview manifests; no actual media/history removal.
            db.execute("DELETE FROM deletion_operations WHERE status='PREVIEW' AND created_at < datetime('now','-1 day')")
        return {'operation_id':key,'status':'PREVIEW',**report}

    def execute(self, key):
        with self.history.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM deletion_operations WHERE id=?',(key,)).fetchone()
            if not row:
                raise Refusal('削除操作が見つかりません。')
            row = dict(row)
            if row['status']=='COMPLETED':
                return self.public(row)
            self.idle(db)
            plan = json.loads(row['plan_json'])
            if row['status']=='PREVIEW' and digest(self.plan(plan['targets'],db)) != digest(plan):
                raise Refusal('参照やファイルが変化しました。削除範囲を再確認してください。')
            for kind, group in [('conversation','conversations'),('image','images'),('job','jobs'),('artifact','artifacts'),('long_video','long_videos'),('client','clients')]:
                for ident in plan.get(group,[]):
                    prior = db.execute('SELECT operation_id FROM deletion_tombstones WHERE kind=? AND id=?',(kind,ident)).fetchone()
                    if prior and prior[0]!=key:
                        raise Refusal('同じ対象を別の削除操作が処理中です。元の操作を再試行してください。')
                    db.execute('INSERT OR IGNORE INTO deletion_tombstones VALUES(?,?,?)',(kind,ident,key))
            db.execute("UPDATE deletion_operations SET status='DELETING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(key,))
        failures, removed = [], 0
        for entry in plan['files']:
            try:
                self.unlink(entry)
                removed += 1
            except (OSError, Refusal) as error:
                reason = str(error) if isinstance(error,Refusal) else (
                    'ファイルの削除権限がありません' if error.errno in {errno.EACCES,errno.EPERM} else
                    '保存領域が読み取り専用です' if error.errno==errno.EROFS else 'ファイルI/Oが失敗しました')
                failures.append({'code':'file_protected_or_io_error','owners':entry['owners'],
                    'location':entry['location'],'reason':reason+'。残存を保護し、管理者確認後に再試行できます。'})
        report = json.loads(row['report_json'])
        report.pop('sqlite_cleanup_pending',None)
        report.pop('metadata_cleanup_pending',None)
        report.update(files_removed=removed, failures=failures, remaining_files=len(failures))
        state = 'PARTIAL' if failures else 'COMPLETED'
        try:
            with self.history.connection() as db:
                db.execute('PRAGMA secure_delete=ON')
                if not failures:
                    self.purge(db,plan)
                db.execute('UPDATE deletion_operations SET status=?,plan_json=?,report_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                    (state,None if not failures else encoded(plan),encoded(report),key))
                db.execute('INSERT INTO deletion_audit(operation_id,status,file_count) VALUES(?,?,?)',(key,state,removed))
        except sqlite3.Error:
            # Files can be gone while the metadata transaction rolled back. Hide
            # them, retain the manifest, and finish the transaction on retry.
            state = 'PARTIAL'
            report['metadata_cleanup_pending'] = True
            with self.history.connection() as db:
                db.execute("UPDATE deletion_operations SET status='PARTIAL',report_json=? WHERE id=?",(encoded(report),key))
                db.execute('INSERT INTO deletion_audit(operation_id,status,file_count) VALUES(?,?,?)',(key,state,removed))
        # Secure-delete erases freed SQLite cells; truncate historic WAL frames too.
        with self.history.connection() as db:
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            report['sqlite_cleanup_pending'] = True
            # Purge already succeeded. Keep manifest for a checkpoint-only retry.
            with self.history.connection() as db:
                db.execute("UPDATE deletion_operations SET status='PARTIAL',plan_json=?,report_json=? WHERE id=?",
                    (encoded(plan),encoded(report),key))
            state = 'PARTIAL'
        return {'operation_id':key,'status':state,**report}

    def purge(self, db, plan):
        def delete(table, column, keys):
            for key in keys:
                db.execute('DELETE FROM '+table+' WHERE '+column+'=?',(key,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='long_video_jobs'").fetchone():
            for jid in plan['jobs']:
                db.execute("UPDATE long_video_jobs SET final_job_id=NULL,status='OUTPUT_DELETED' WHERE final_job_id=?",(jid,))
            delete('long_video_members','job_id',plan['jobs'])
            delete('long_video_members','parent_id',plan.get('long_videos',[]))
            delete('long_video_temp_files','parent_id',plan.get('long_videos',[]))
            delete('long_video_jobs','id',plan.get('long_videos',[]))
        # Preserve stable URL indices for independently selected legacy outputs.
        # Empty redacted slots retain no file metadata; only the other outputs
        # still share the parent generation prompt/settings.
        for slot in plan['artifact_slots']:
            if slot['job_id'] in plan['jobs']:
                continue
            row = db.execute('SELECT result_json FROM tool_jobs WHERE job_id=?',(slot['job_id'],)).fetchone()
            if row:
                result = json.loads(row[0]); values = result['tool_result']['artifacts']
                values[slot['index']] = {'path':'','kind':'file','deleted':True}
                output = result['tool_result'].get('output')
                if isinstance(output,dict) and 'artifact_count' in output:
                    output['artifact_count'] = sum(not a.get('deleted') for a in values)
                db.execute('UPDATE tool_jobs SET result_json=? WHERE job_id=?',(encoded(result),slot['job_id']))
        # Null lineage pointers without erasing independently owned child outputs.
        for jid in plan['jobs']:
            db.execute('UPDATE video_clips SET previous_clip_id=NULL WHERE previous_clip_id=?',(jid,))
            db.execute('UPDATE video_clips SET retry_of_clip_id=NULL WHERE retry_of_clip_id=?',(jid,))
        delete('video_clips','clip_id',plan['jobs'])
        for iid in plan['images']:
            db.execute('UPDATE image_assets SET parent_id=NULL WHERE parent_id=?',(iid,))
            db.execute('UPDATE video_clips SET last_frame_image_id=NULL WHERE last_frame_image_id=?',(iid,))
        delete('image_observations','image_id',plan['images'])
        delete('image_observations','job_id',plan['jobs'])
        delete('image_assets','id',plan['images'])
        # Preserved shared tails lose only the deleted source-job binding.
        for jid in plan['jobs']:
            db.execute('UPDATE image_assets SET source_job_id=NULL WHERE source_job_id=?',(jid,))
        delete('media_events','job_id',plan['jobs'])
        delete('tool_jobs','job_id',plan['jobs'])
        for pid in plan['projects']:
            if not db.execute('SELECT 1 FROM video_clips WHERE project_id=?',(pid,)).fetchone() and not db.execute(
                    "SELECT 1 FROM tool_jobs WHERE json_extract(image_request_json,'$.project_id')=?",(pid,)).fetchone():
                delete('video_projects','id',[pid])
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for cid in plan['conversations']:
            for table in ('chat_generation_jobs','image_observations','chat_turn_metrics','conversation_ratings',
                          'messages','video_projects','work_context','work_runs'):
                if table in tables:
                    delete(table,'conversation_id',[cid])
            delete('conversations','id',[cid])


def router():
    routes = APIRouter(prefix='/api/deletions')

    def ready(request):
        # All mutating requests are tracked from ASGI receive through completion,
        # including uploads awaiting body and Work submissions awaiting the gateway.
        state = request.app.state
        snap = state.resources.snapshot()
        if (state.deletion_mutations > 1 or state.deletion_streams or state.turn_lock.locked()
                or snap['active_job'] or snap['waiting_jobs']):
            raise HTTPException(409,'通信・生成処理の完了後に再試行してください。実行中の処理は停止しません。')
        return state.deletions

    @routes.get('/catalog')
    async def catalog(request: Request):
        return request.app.state.deletions.catalog()

    @routes.post('/preview')
    async def preview(body: Selection, request: Request):
        same_origin(request)
        try:
            return ready(request).preview(body.targets)
        except Refusal as error:
            raise HTTPException(409,str(error)) from None

    @routes.post('/{operation_id}/execute')
    async def execute(operation_id: UUID, body: Confirmation, request: Request):
        same_origin(request)
        try:
            return ready(request).execute(str(operation_id))
        except Refusal as error:
            raise HTTPException(409,str(error)) from None

    @routes.get('/{operation_id}')
    async def status(operation_id: UUID, request: Request):
        with request.app.state.deletions.history.connection() as db:
            row = db.execute('SELECT * FROM deletion_operations WHERE id=?',(str(operation_id),)).fetchone()
        if row is None:
            raise HTTPException(404,'削除操作が見つかりません。')
        return request.app.state.deletions.public(dict(row))

    return routes


class DeletionBoundary:
    """Outer ASGI gate: no cached body consumption, private payload logging or replay."""
    def __init__(self, app, host):
        self.app,self.host = app,host
        host.state.deletion_mutations = host.state.deletion_streams = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not hasattr(self.host.state,'deletions'):
            return await self.app(scope,receive,send)
        path, method = scope['path'],scope['method']
        deletion_route = path.startswith('/api/deletions')
        mutation = path.startswith('/api/') and method not in {'GET','HEAD','OPTIONS'}
        stream = not deletion_route and ('/artifacts/' in path or path.endswith('/content'))
        self.host.state.deletion_mutations += int(mutation)
        self.host.state.deletion_streams += int(stream)
        buffer = []
        async def guarded_receive():
            if buffer:
                return buffer.pop(0)
            return await receive()
        try:
            # URL UUIDs also cover client_request_id, jobs, image and conversation IDs.
            artifact_route = re.fullmatch(r'/api/jobs/([a-fA-F0-9-]{36})/artifacts/([0-9]+)',path)
            if artifact_route and self.host.state.deletions.deleted(artifact_id(artifact_route[1],int(artifact_route[2]))):
                return await JSONResponse({'detail':'削除済みの成果物です。'},status_code=410)(scope,receive,send)
            query_values = list(parse_qs(scope.get('query_string',b'').decode('utf-8',errors='replace')).values())
            if not deletion_route and any(self.host.state.deletions.deleted(i) for i in ids([path.split('/'),query_values])):
                return await JSONResponse({'detail':'削除済み、または完全削除の再試行待ちです。'},status_code=410)(scope,receive,send)
            content_type = dict(scope.get('headers',[])).get(b'content-type',b'').lower().split(b';')[0].strip()
            if mutation and not deletion_route and (content_type in {b'',b'application/json'} or content_type.endswith(b'+json')):
                chunks, total = [], 0
                while True:
                    message = await receive()
                    buffer.append(message)
                    if message['type'] == 'http.disconnect':
                        break
                    chunks.append(message.get('body',b'')); total += len(chunks[-1])
                    if total > 2*1024*1024:
                        return await JSONResponse({'detail':'JSON入力が大きすぎます。'},status_code=413)(scope,receive,send)
                    if not message.get('more_body'):
                        break
                try:
                    body = json.loads(b''.join(chunks))
                except (ValueError, UnicodeError):
                    body = None  # Existing route validation owns malformed JSON.
                if any(self.host.state.deletions.deleted(i) for i in ids(body)):
                    return await JSONResponse({'detail':'削除済みの対象は再利用できません。'},status_code=410)(scope,receive,send)
            async def private_send(message):
                if message['type']=='http.response.start' and path.startswith('/api/deletions'):
                    message['headers'] = [(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control'] + [(b'cache-control',b'no-store')]
                await send(message)
            await self.app(scope,guarded_receive,private_send)
        finally:
            self.host.state.deletion_mutations -= int(mutation)
            self.host.state.deletion_streams -= int(stream)
