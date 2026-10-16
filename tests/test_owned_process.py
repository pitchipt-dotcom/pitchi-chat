"""CPU-only real child lifecycle tests, not SHA GPU integration."""

import asyncio
import sys

import httpx
import pytest

from backend.app.resources.manager import GPUUnsafe
from backend.app.resources.process import OwnedProcess, ProcessProfile


def test_owned_cpu_child_safe_stop(tmp_path):
    async def run():
        calls = 0
        def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("synthetic unavailable", request=request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            driver = OwnedProcess(ProcessProfile("cpu-test", (sys.executable, "-c", "import time; time.sleep(30)"),
                                  tmp_path, "http://localhost/health", 1, 1), client)
            try:
                await driver.start()
                assert driver.process.pid in driver.owned_pids and driver.identity
            finally:
                await driver.stop()
            assert driver.process.returncode is not None and not driver.owned_pids
    asyncio.run(run())


@pytest.mark.parametrize("status", [200, 401, 503])
def test_existing_listener_is_never_adopted(tmp_path, status):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
            driver = OwnedProcess(ProcessProfile("cpu-test", (sys.executable, "-c", "raise RuntimeError"),
                                  tmp_path, "http://localhost/health"), client)
            with pytest.raises(GPUUnsafe):
                await driver.start()
            assert driver.process is None
    asyncio.run(run())

@pytest.mark.parametrize('queue,expected', [
    ({'queue_running':[], 'queue_pending':[]}, True),
    ({'queue_running':['active'], 'queue_pending':[]}, False),
    ({'queue_running':[], 'queue_pending':['waiting']}, False),
    ({'queue_running':0, 'queue_pending':[]}, False),
    ({'queue_running':[]}, False),
])
def test_owned_comfy_queue_is_required_before_stop(tmp_path, queue, expected):
    async def run():
        def handle(request):
            assert request.url.path == '/queue'
            return httpx.Response(200,json=queue)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            driver=OwnedProcess(ProcessProfile('comfyui',(sys.executable,),tmp_path,'http://localhost/system_stats'),client)
            assert await driver.idle() is expected
    asyncio.run(run())


def test_exited_owned_session_is_idle_but_orphan_is_not(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch, PropertyMock
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(503))) as client:
            driver=OwnedProcess(ProcessProfile('comfyui',(sys.executable,),tmp_path,'http://localhost/system_stats'),client)
            driver.process=SimpleNamespace(pid=99999999,returncode=1)
            assert await driver.idle() is True
            with patch.object(OwnedProcess,'owned_pids',new_callable=PropertyMock,return_value=frozenset({888})):
                assert await driver.idle() is False
    asyncio.run(run())
