"""Pinned finite Work binding; no arbitrary arguments or shell. Work owns model execution."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tomllib
from fastapi import HTTPException

PROJECT='ptotsearch-guidelines'

def tool_digest(root):
    base=root/'src/ptotsearch_tools';paths=sorted(p for p in base.iterdir() if p.is_file() and p.suffix in ('.py','.php','.patch','.mjs'))
    collector=root/'src/pitchi_work/search_probe.php'
    h=hashlib.sha256()
    for p in [*paths,collector]:
        if p.is_symlink() or p.stat().st_size>1024*1024:raise ValueError('Unsafe tool source')
        h.update(p.name.encode());h.update(p.read_bytes())
    return h.hexdigest()

class GuidelineClient:
    def __init__(self,config=None):
        if config is None:
            name=os.environ.get('PITCH_CHAT_GUIDELINES_CONFIG');config=tomllib.loads(Path(name).read_text()) if name else {}
        self.config=config
    @property
    def enabled(self):return self.config.get('enabled',False)
    async def call(self,payload,publication=False):
        c=self.config
        if not self.enabled:raise HTTPException(503,'ガイドライン登録ツールは無効です。')
        source=Path(c['source_root'])
        if tool_digest(source)!=c['reviewed_asset_sha256']:raise HTTPException(503,{'code':'GUIDELINE_TOOL_SOURCE_REVIEW_REQUIRED','message':'登録ツールのソース確認が必要です。'})
        env={k:os.environ[k] for k in ('PATH','HOME','LANG','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
        env.update(PYTHONPATH=str(source/'src'),PYTHONDONTWRITEBYTECODE='1')
        module='ptotsearch_tools.publication' if publication else 'ptotsearch_tools.workflow'
        p=await asyncio.create_subprocess_exec(c['python_executable'],'-B','-m',module,'--config',c['tool_config'],
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,start_new_session=True)
        try:output,_=await asyncio.wait_for(p.communicate(json.dumps(payload).encode()),20)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            p.terminate();await p.wait();raise HTTPException(504,'ツール受付が不明です。再送せず作業状態を確認してください。') from None
        if len(output)>1024*1024:raise HTTPException(502,'Tool response budget exceeded')
        try:result=json.loads(output)
        except ValueError:raise HTTPException(502,'Tool protocol error') from None
        if not result.get('ok'):raise HTTPException(409,result.get('error','Tool rejected request'))
        return result['result']
