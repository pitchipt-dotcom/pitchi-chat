"""Model-free subprocess ownership tests; no GPU, model, socket or systemd."""
import asyncio
from pathlib import Path
import sys
import signal

import httpx
import pytest

from backend.app.resources.process import ProcessProfile, OwnedProcess
from backend.app.resources.manager import GPUUnsafe


def test_graceful_stop_waits_for_owned_child_and_parent(tmp_path):
    async def run():
        ready = tmp_path / "ready"
        code = (
            "import subprocess,signal,sys,pathlib,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "def stop(*args):\n child.wait(timeout=3)\n sys.exit(0)\n"
            "signal.signal(signal.SIGINT,stop)\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        def health(request):
            if not ready.exists(): raise httpx.ConnectError("synthetic not ready",request=request)
            return httpx.Response(200,json={"loaded":True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(health)) as client:
            profile = ProcessProfile("synthetic", (sys.executable,"-c",code), tmp_path,
                                     "http://127.0.0.1:18765/health", start_timeout=3,
                                     stop_timeout=3, graceful_only=True)
            driver = OwnedProcess(profile, client)
            try:
                await driver.start()
                assert int(ready.read_text()) in driver.owned_pids
                assert driver.process.pid in driver.owned_pids
            finally:
                await driver.stop()
            assert driver.process.returncode == 0
            assert not driver.owned_pids
            assert not Path('/proc', ready.read_text()).exists()
    asyncio.run(run())


def test_parent_exit_does_not_claim_child_release(tmp_path):
    class Process:
        returncode = 0
        async def wait(self): return 0
    class Unreleased(OwnedProcess):
        @property
        def owned_pids(self): return frozenset({123})
        def _signal(self, sig): pass
    async def run():
        async with httpx.AsyncClient() as client:
            profile=ProcessProfile("synthetic",("/synthetic/python",),tmp_path,
                "http://127.0.0.1:18765/health",stop_timeout=.01,graceful_only=True)
            driver=Unreleased(profile,client); driver.process=Process()
            with pytest.raises(GPUUnsafe,match="process_tree_release_unconfirmed"): await driver.stop()
    asyncio.run(run())


def test_direct_strata_activity_or_unknown_status_refuses_stop(tmp_path):
    async def run():
        for response in [httpx.Response(200,json={"activity":{"in_flight":1}}),
                         httpx.Response(200,json={"activity":{"in_flight":0}}),
                         httpx.Response(200,json={}),httpx.Response(503)]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:response)) as client:
                profile=ProcessProfile("strata",("/synthetic/python",),tmp_path,"http://127.0.0.1:18765/health")
                assert await OwnedProcess(profile,client).idle() is (response.status_code==200 and
                        response.json().get("activity",{}).get("in_flight")==0)
    asyncio.run(run())
