"""Synthetic model lifecycle tests: no models, GPU or systemd services used."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.app.resources.config import ResourceSettings, load_resources
from backend.app.resources.gpu import ResourceCoordinator, ResourceMode
from backend.app.resources.manager import GPUManager, GPUReading, GPUUnsafe, GPUPhase
from backend.app.storage.sqlite import HistoryStore
from backend.app.jobs.chat_generations import ChatGenerationStore


class Inspector:
    def __init__(self):
        self.pids = set()
        self.used = 0
    async def read(self):
        return GPUReading(self.used, frozenset(self.pids), 12288)


class Driver:
    def __init__(self, inspector, pid, manual=False):
        self.inspector, self.pid = inspector, pid
        self.profile = SimpleNamespace(device="gpu", manual_start=manual, required_vram_mib=0,
                                       runtime_ram_reserve_mib=0)
        self.events = []
        self.started = False
        self.start_gate = None
        self.stop_gate = None
        self.fail_start = False
    @property
    def owned_pids(self):
        return frozenset({self.pid}) if self.started else frozenset()
    async def start(self):
        self.events.append("start")
        if self.start_gate:
            await self.start_gate.wait()
        self.started = True
        self.inspector.pids.add(self.pid)
        if self.fail_start:
            raise GPUUnsafe("start_failed")
    async def stop(self):
        self.events.append("stop")
        if self.stop_gate:
            await self.stop_gate.wait()
        self.started = False
        self.inspector.pids.discard(self.pid)


def setup(tmp_path):
    store = HistoryStore(tmp_path / "usage.db"); store.initialize()
    inspector = Inspector()
    drivers = {"strata": Driver(inspector, 10, True), "image": Driver(inspector, 11)}
    now = [0.]
    manager = GPUManager(store, inspector, drivers, idle_timeout=600, clock=lambda: now[0],
                         queue_timeout=.1, release_timeout=.02)
    manager.open()
    return manager, drivers, inspector, now


async def generate(manager):
    async with manager.usage("strata"):
        async with manager.lease("strata", str(uuid4()), allow_start=True, keep_ready=True):
            await asyncio.sleep(0)


def test_on_demand_then_ten_minute_idle_release(tmp_path):
    async def run():
        manager, drivers, _, now = setup(tmp_path)
        assert not drivers["strata"].events
        await generate(manager)
        now[0] = 599
        await manager.maintenance_once()
        assert drivers["strata"].events == ["start"]
        now[0] = 600
        await manager.maintenance_once()
        assert drivers["strata"].events == ["start", "stop"]
        assert manager.loaded is None and manager.phase == GPUPhase.STOPPED
        assert manager.last_release["process_exit_confirmed"] and manager.last_release["vram_release_confirmed"]
        assert "ram_available_mib" in manager.last_release
        await manager.close()
    asyncio.run(run())


def test_starting_requests_share_single_start_and_serial_generation(tmp_path):
    async def run():
        manager, drivers, _, _ = setup(tmp_path)
        gate = drivers["strata"].start_gate = asyncio.Event()
        active = 0; peak = 0
        async def request():
            nonlocal active, peak
            async with manager.usage("strata"):
                async with manager.lease("strata", str(uuid4()), allow_start=True, keep_ready=True):
                    active += 1; peak = max(peak, active)
                    await asyncio.sleep(.005)
                    active -= 1
        tasks = [asyncio.create_task(request()) for _ in range(3)]
        await asyncio.sleep(.005)
        assert manager.phase == GPUPhase.STARTING
        assert manager.usage_count() == 3 and manager.waiting == 2
        gate.set(); await asyncio.gather(*tasks)
        assert peak == 1 and drivers["strata"].events == ["start"]
        assert manager.usage_count() == 0
        await manager.close()
    asyncio.run(run())


def test_job_retains_between_phases_blocks_manual_stop_and_switch(tmp_path):
    async def run():
        manager, drivers, _, now = setup(tmp_path)
        async with manager.usage("strata"):
            await generate(manager)
            now[0] = 10000
            await manager.maintenance_once()
            with pytest.raises(GPUUnsafe): await manager.stop_idle("strata")
            with pytest.raises(GPUUnsafe, match="model_in_use"):
                async with manager.lease("image", "image"): pass
            assert not drivers["image"].events
        now[0] += 600
        await manager.maintenance_once()
        async with manager.lease("image", "image"): pass
        assert drivers["image"].events == ["start", "stop"]
        await manager.close()
    asyncio.run(run())


def test_active_and_coordinator_queued_requests_prohibit_idle_stop(tmp_path):
    async def run():
        manager, drivers, _, now = setup(tmp_path)
        coordinator = ResourceCoordinator(manager)
        entered, release = asyncio.Event(), asyncio.Event()
        async def first():
            async with coordinator.job(ResourceMode.CHAT, profile="strata", allow_start=True, keep_ready=True):
                entered.set(); await release.wait()
        async def second():
            async with coordinator.job(ResourceMode.CHAT, profile="strata", allow_start=True, keep_ready=True): pass
        task = asyncio.create_task(first()); await entered.wait()
        waiter = asyncio.create_task(second()); await asyncio.sleep(0)
        now[0] = 1000; await manager.maintenance_once()
        assert manager.usage_count() == 2
        with pytest.raises(GPUUnsafe): await manager.stop_idle("strata")
        release.set(); await asyncio.gather(task, waiter)
        assert drivers["strata"].events == ["start"]
        await manager.close()
    asyncio.run(run())


def test_request_during_stop_waits_then_starts_once(tmp_path):
    async def run():
        manager, drivers, _, _ = setup(tmp_path)
        await generate(manager)
        gate = drivers["strata"].stop_gate = asyncio.Event()
        stopping = asyncio.create_task(manager.stop_idle("strata")); await asyncio.sleep(0)
        assert manager.phase == GPUPhase.STOPPING
        request = asyncio.create_task(generate(manager)); await asyncio.sleep(0)
        gate.set(); await asyncio.gather(stopping, request)
        assert drivers["strata"].events == ["start", "stop", "start"]
        await manager.close()
    asyncio.run(run())


def test_abandoned_renewable_job_reservation_expires_then_idle_clock_starts(tmp_path):
    async def run():
        manager, _, _, now = setup(tmp_path)
        await generate(manager)
        token = manager.retain("strata", ttl=120)
        now[0] = 100; manager.renew(token)
        now[0] = 219; await manager.maintenance_once()
        assert manager.usage_count() == 1
        now[0] = 220; await manager.maintenance_once()
        assert manager.loaded == "strata" and manager.usage_count() == 0
        with pytest.raises(GPUUnsafe, match="usage_expired"): manager.renew(token)
        now[0] = 820; await manager.maintenance_once()
        assert manager.loaded is None
        await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancel", "timeout", "error"])
def test_client_failure_releases_use_without_stopping_shared_job(tmp_path, failure):
    async def run():
        manager, drivers, _, _ = setup(tmp_path)
        held = manager.retain("strata", ttl=120)
        error = {"cancel": asyncio.CancelledError, "timeout": TimeoutError, "error": ValueError}[failure]
        with pytest.raises(error):
            async with manager.usage("strata"):
                async with manager.lease("strata", "client", allow_start=True, keep_ready=True): raise error()
        assert manager.usage_count() == 1
        assert manager.phase == GPUPhase.READY and drivers["strata"].events == ["start"]
        manager.release_use(held); await manager.close()
    asyncio.run(run())


def test_start_failure_cleanup_and_unreleased_vram_quarantine(tmp_path):
    async def run():
        manager, drivers, inspector, _ = setup(tmp_path)
        drivers["strata"].fail_start = True
        with pytest.raises(GPUUnsafe): await generate(manager)
        assert manager.usage_count() == 0 and manager.phase == GPUPhase.STOPPED
        drivers["strata"].fail_start = False
        await generate(manager)
        inspector.used = 500
        with pytest.raises(TimeoutError): await manager.stop_idle("strata")
        assert manager.phase == GPUPhase.ERROR and manager.last_release is None
        with pytest.raises(GPUUnsafe, match="recovery_required"): await generate(manager)
        await manager.close()
    asyncio.run(run())


def test_unhealthy_loaded_engine_is_quarantined_without_auto_restart(tmp_path):
    async def run():
        manager, drivers, _, _ = setup(tmp_path)
        await generate(manager)
        async def unhealthy(): return False
        drivers["strata"].healthy = unhealthy
        await manager.maintenance_once()
        assert manager.phase == GPUPhase.ERROR and manager.error == "owned_process_unhealthy"
        with pytest.raises(GPUUnsafe): await generate(manager)
        assert drivers["strata"].events == ["start"]
        await manager.close()
    asyncio.run(run())


def test_foreign_comfy_workload_never_signalled(tmp_path):
    async def run():
        manager, drivers, inspector, _ = setup(tmp_path)
        inspector.pids.add(999)
        with pytest.raises(GPUUnsafe): await generate(manager)
        assert inspector.pids == {999} and not drivers["strata"].events
        await manager.close()
    asyncio.run(run())


def test_chat_queue_is_bounded_and_same_conversation_cannot_race(tmp_path):
    store = HistoryStore(tmp_path / "queue.db"); store.initialize()
    jobs = ChatGenerationStore(store)
    cid = str(uuid4())
    jobs.create(str(uuid4()), cid, {"message": "one"})
    with pytest.raises(ValueError, match="generation_busy"):
        jobs.create(str(uuid4()), cid, {"message": "two"})
    for _ in range(31): jobs.create(str(uuid4()), str(uuid4()), {"message": "queued"})
    with pytest.raises(ValueError, match="generation_busy"):
        jobs.create(str(uuid4()), str(uuid4()), {"message": "full"})


@pytest.mark.parametrize("value", [0, -1, True, '600', float('inf'), 86401])
def test_invalid_idle_timeout_rejected(tmp_path, value):
    cfg = tmp_path / "resources.toml"
    value = '"600"' if value == '600' else str(value).lower()
    cfg.write_text('[gpu]\nidle_timeout_seconds=' + value + '\n')
    with pytest.raises(ValueError): load_resources(cfg)


def test_custom_idle_timeout(tmp_path):
    cfg = tmp_path / "resources.toml"; cfg.write_text('[gpu]\nidle_timeout_seconds=1200\n')
    assert load_resources(cfg).idle_timeout_seconds == 1200


def test_background_maintenance_performs_idle_stop_without_ui_polling(tmp_path):
    import time
    async def run():
        manager,drivers,_,_=setup(tmp_path)
        manager.clock=time.monotonic;manager._last_use=time.monotonic();manager.idle_timeout=1
        manager.start_maintenance()
        await generate(manager)
        for _ in range(150):
            if manager.loaded is None:break
            await asyncio.sleep(.01)
        assert manager.loaded is None and drivers['strata'].events==['start','stop']
        await manager.close()
        assert manager._maintenance is None
    asyncio.run(run())


def test_recover_releases_owned_quarantine_but_never_adopts_foreign_process(tmp_path):
    async def run():
        manager,drivers,inspector,_=setup(tmp_path)
        await generate(manager)
        manager._state(GPUPhase.ERROR,'strata',error='owned_process_unhealthy')
        token=manager.retain('strata',ttl=120)
        with pytest.raises(GPUUnsafe):await manager.recover_idle('strata')
        manager.release_use(token)
        await manager.recover_idle('strata')
        assert manager.phase==GPUPhase.STOPPED and drivers['strata'].events==['start','stop']
        manager._state(GPUPhase.ERROR,'strata',error='recovery_required')
        inspector.pids.add(999)
        with pytest.raises(TimeoutError):await manager.recover_idle('strata')
        assert inspector.pids=={999} and manager.phase==GPUPhase.ERROR
        await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("resource", ["ram", "swap", "vram"])
def test_original_memory_gates_refuse_start_without_any_process_stop(tmp_path,monkeypatch,resource):
    async def run():
        manager,drivers,inspector,_=setup(tmp_path)
        profile=drivers["strata"].profile
        profile.required_ram_mib=43008; profile.min_swap_free_mib=1024
        profile.required_vram_mib=10500
        state={"ram_available_mib":40000 if resource=="ram" else 50000,
               "swap_free_mib":512 if resource=="swap" else 2048,"swap_used_mib":0}
        monkeypatch.setattr('backend.app.resources.memory.memory_state',lambda:state)
        inspector.used=2000 if resource=="vram" else 0
        with pytest.raises(GPUUnsafe):await generate(manager)
        assert not drivers["strata"].events and manager.usage_count()==0
        await manager.close()
    asyncio.run(run())
