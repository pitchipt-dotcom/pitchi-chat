"""Real local HTTP/Chrome/CPU files; mock all models and GPU services."""
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.deletion import Deletions
from backend.app.providers.config import CloudSettings
from backend.app.resources.config import ResourceSettings
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.tools.work_config import WorkSettings
from backend.app.storage.sqlite import HistoryStore
from test_long_videos import service,mp4


def test_real_browser_long_video_reconnect_and_delete(tmp_path,monkeypatch,mp4):
    chrome=shutil.which('google-chrome') or shutil.which('chromium');node=shutil.which('node')
    if not chrome or not node:pytest.skip('Chrome and Node required')
    long,v=service(tmp_path,monkeypatch,mp4);host,worker,jobs,gpu,client,projects,images,cid,calls=v
    ui_path=tmp_path/'ui.db'
    app=create_app(Settings(ui_path,'','fixture','TEST_KEY',1),services=(),resource_settings=ResourceSettings(),
        image_workflows=ImageWorkflowRegistry(),cloud_settings=CloudSettings(),work_settings=WorkSettings())
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            from backend.app.tools.registry import ToolRegistry
            work_definition=application.state.harness.registry.get('pitch_work.run')
            host.registry=ToolRegistry(tuple(host.registry._definitions.values())+(work_definition,))
            with HistoryStore(ui_path).connection() as db:db.execute('INSERT INTO conversations(id,title) VALUES(?,?)',(cid,'Synthetic long video test'))
            for name,value in [('harness',host),('worker',worker),('jobs',jobs),('gpu_manager',gpu),('resources',host.resources),
                ('models',host.models),('image_assets',images),('video_projects',projects),('long_videos',long),('deletions',Deletions(jobs.store))]:
                setattr(application.state,name,value)
            worker.start()
            try:yield
            finally:await worker.close();await gpu.close();await client.aclose()
    app.router.lifespan_context=lifespan
    fixture=tmp_path/'browser-fixture.json';fixture.write_text(json.dumps({'cid':cid}))
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.05)
        assert server.started
        result=subprocess.run([node,'tests/long_video_browser.cjs',chrome,f'http://127.0.0.1:{port}',str(fixture)],capture_output=True,text=True,timeout=70)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'PASS actual 390px Chrome/mock GPU' in result.stdout
        assert len(calls)==5,'reconnect never repeats generation'
    finally:server.should_exit=True;thread.join(timeout=10);sock.close()
