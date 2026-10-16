"""Isolated real HTTP Host + Chrome; temporary synthetic data, no inference."""
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.providers.config import CloudSettings
from backend.app.resources.config import ResourceSettings
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.tools.work_config import WorkSettings
from test_deletion import png, job


def test_mobile_real_host_deletion(tmp_path):
    chrome=shutil.which('google-chrome') or shutil.which('chromium');node=shutil.which('node')
    if not chrome or not node:pytest.skip('Chrome and Node required')
    app=create_app(Settings(tmp_path/'history.sqlite3','','fixture','TEST_KEY',1),services=(),
        resource_settings=ResourceSettings(),image_workflows=ImageWorkflowRegistry(),cloud_settings=CloudSettings(),work_settings=WorkSettings())
    original=app.router.lifespan_context
    records={}
    @asynccontextmanager
    async def lifespan(host):
        async with original(host):
            s=SimpleNamespace(history=host.state.jobs.store,deletions=host.state.deletions)
            images=host.state.image_assets;cid=images.create_conversation();keep=images.create_conversation()
            first=images.save(png(),cid);second=images.save(png(),cid)
            audio,ap=job(s,'tts.generate');video,vp=job(s,'media.video.generate')
            records.update(images=[first['id'],second['id']],batch=[cid,audio,video],keep=keep,paths=[ap,vp])
            (tmp_path/'fixtures.json').write_text(json.dumps({k:v for k,v in records.items() if k!='paths'}))
            yield
    app.router.lifespan_context=lifespan
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.05)
        assert server.started
        run=subprocess.run([node,'tests/deletion_browser.cjs',chrome,f'http://127.0.0.1:{port}',str(tmp_path/'fixtures.json')],capture_output=True,text=True,timeout=65)
        assert run.returncode==0,run.stdout+run.stderr
        assert 'PASS actual 390px Chrome' in run.stdout
        assert all(not p.exists() for p in records['paths'])
        assert not list((tmp_path/'image-assets').glob('*.png'))
        with app.state.jobs.store.connection() as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
