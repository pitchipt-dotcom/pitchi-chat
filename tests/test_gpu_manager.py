"""GPU lifecycle mock tests; no real GPU processes are changed."""

import asyncio
import json
import pytest

from backend.app.resources.manager import GPUManager, GPUReading, GPUUnsafe
from backend.app.storage.sqlite import HistoryStore


class Inspector:
    def __init__(self):
        self.pids = set()
        self.used = 0
    async def read(self):
        return GPUReading(self.used, frozenset(self.pids))


class Driver:
    def __init__(self, inspector, pid=10):
        self.inspector, self.pid = inspector, pid
        self.started = False
        self.events = []
        self.fail_start = False
        self.fail_stop = False
    @property
    def owned_pids(self):
        return frozenset({self.pid}) if self.started else frozenset()
    async def start(self):
        self.started = True
        self.inspector.pids.add(self.pid)
        self.events.append("start")
        if self.fail_start:
            raise GPUUnsafe("start_failed")
    async def stop(self):
        self.events.append("stop")
        if self.fail_stop:
            raise GPUUnsafe("stop_failed")
        self.inspector.pids.discard(self.pid)
        self.started = False


def manager(tmp_path):
    store = HistoryStore(tmp_path / "jobs.db")
    store.initialize()
    inspector = Inspector()
    drivers = {"qwen": Driver(inspector), "image": Driver(inspector, 11)}
    instance = GPUManager(store, inspector, drivers, release_timeout=0.03, queue_timeout=0.03)
    instance.open()
    return instance, drivers, inspector


def test_chat_ready_to_tool_stop_release_start_and_exclusion(tmp_path):
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        async with gpu.lease("qwen", "job-a", keep_ready=True):
            assert gpu.snapshot()["state"] == "BUSY"
            assert gpu.snapshot()["current_owner"] == "qwen"
        assert gpu.snapshot()["state"] == "READY"
        async with gpu.lease("image", "job-b"):
            assert not drivers["qwen"].started and drivers["image"].started
            assert gpu.snapshot()["state"] == "BUSY"
        assert gpu.snapshot()["state"] == "STOPPED"
        assert drivers["qwen"].events == ["start", "stop"]
        assert drivers["image"].events == ["start", "stop"]
        await gpu.close()
    asyncio.run(run())


def test_finite_queue_wait_and_owner_kept(tmp_path):
    async def run():
        gpu, _, _ = manager(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def active():
            async with gpu.lease("qwen", "first"):
                entered.set()
                await release.wait()
        task = asyncio.create_task(active())
        await entered.wait()
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("image", "second"):
                pytest.fail("queue timeout must not dispatch")
        assert gpu.owner == "qwen" and gpu.waiting == 0
        release.set()
        await task
        await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["start", "execution"])
def test_owned_failure_stops_and_confirms_release(tmp_path, failure):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        drivers["image"].fail_start = failure == "start"
        with pytest.raises((GPUUnsafe, ValueError)):
            async with gpu.lease("image", "job"):
                raise ValueError("private exception")
        assert not inspector.pids
        assert gpu.snapshot()["state"] == "STOPPED"
        assert "private exception" not in json.dumps(gpu.snapshot())
        await gpu.close()
    asyncio.run(run())


def test_stop_failure_retains_quarantine(tmp_path):
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        drivers["image"].fail_stop = True
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("image", "job"):
                pass
        assert gpu.snapshot()["state"] == "ERROR"
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("qwen", "next"):
                pass
        assert not drivers["qwen"].started
        await gpu.close()
    asyncio.run(run())


def test_foreign_gpu_owner_is_never_stopped(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        inspector.pids.add(999)
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("image", "job"):
                pass
        assert inspector.pids == {999}
        assert all(not driver.events for driver in drivers.values())
        assert gpu.phase.value == "STOPPED"
        await gpu.close()
    asyncio.run(run())


def test_unreleased_vram_blocks_new_start(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        inspector.used = 500
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("image", "job"):
                pass
        assert not drivers["image"].started
        await gpu.close()
    asyncio.run(run())


def test_remote_timeout_retains_persistent_owner_across_restart(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        with pytest.raises(TimeoutError):
            async with gpu.lease("image", "job", remote=True):
                raise TimeoutError("remote unknown")
        assert gpu.snapshot()["state"] == "ERROR"
        assert drivers["image"].started
        await gpu.close()
        restarted = GPUManager(gpu.store, inspector, drivers, release_timeout=0.01)
        restarted.open()
        assert restarted.error == "recovery_required"
        assert restarted.owner == "image"
        with pytest.raises(GPUUnsafe):
            async with restarted.lease("qwen", "other"):
                pass
        assert drivers["image"].events == ["start"]
        await restarted.close()
    asyncio.run(run())


def test_duplicate_host_same_database_cannot_adopt(tmp_path):
    async def run():
        gpu, _, inspector = manager(tmp_path)
        other = GPUManager(gpu.store, inspector)
        with pytest.raises(GPUUnsafe):
            other.open()
        await gpu.close()
    asyncio.run(run())


def test_late_completion_cannot_clear_closed_manager(tmp_path):
    async def run():
        gpu, _, _ = manager(tmp_path)
        await gpu.close()
        with pytest.raises(GPUUnsafe):
            gpu._state(gpu.phase)
    asyncio.run(run())


def test_gpu_host_lock_spans_different_databases(tmp_path):
    async def run():
        first = HistoryStore(tmp_path / "first.db")
        second = HistoryStore(tmp_path / "second.db")
        first.initialize()
        second.initialize()
        lock = tmp_path / "shared.host.lock"
        gpu = GPUManager(first, Inspector(), host_lock_path=lock)
        other = GPUManager(second, Inspector(), host_lock_path=lock)
        gpu.open()
        with pytest.raises(GPUUnsafe):
            other.open()
        await gpu.close()
        other.open()
        await other.close()
    asyncio.run(run())


def test_cpu_chat_does_not_grant_gpu_ownership(tmp_path):
    from types import SimpleNamespace
    from backend.app.resources.gpu import ResourceCoordinator, ResourceMode
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        cpu = drivers["qwen"]
        cpu.profile = SimpleNamespace(device="cpu")
        async def start():
            cpu.started = True
            cpu.events.append("start")
        cpu.start = start
        inspector.pids.add(999)
        coordinator = ResourceCoordinator(gpu)
        async with coordinator.job(ResourceMode.CHAT, profile="qwen"):
            assert gpu.owner is None and gpu.phase.value == "STOPPED"
            assert inspector.pids == {999}
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("qwen", "cannot-use-cpu-for-gpu"):
                pass
        with pytest.raises(GPUUnsafe):
            async with coordinator.job(ResourceMode.IMAGE, profile="qwen"):
                pass
        assert cpu.events == ["start", "stop"]
        await gpu.close()
    asyncio.run(run())


def test_cpu_claim_that_uses_gpu_is_stopped_before_model(tmp_path):
    from types import SimpleNamespace
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        drivers["qwen"].profile = SimpleNamespace(device="cpu")
        with pytest.raises(GPUUnsafe):
            async with gpu.cpu_lease("qwen"):
                pytest.fail("GPU usage cannot bypass policy")
        assert not inspector.pids and drivers["qwen"].events == ["start", "stop"]
        await gpu.close()
    asyncio.run(run())
