import asyncio

import pytest

from backend.app.resources.gpu import ResourceCoordinator, ResourceMode


def test_shared_lock_serializes_different_gpu_modes():
    async def run():
        resources = ResourceCoordinator()
        entered = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def chat():
            async with resources.job(ResourceMode.CHAT, model="test-model"):
                order.append("chat")
                entered.set()
                await release.wait()

        async def tts():
            async with resources.job(ResourceMode.TTS, tool="tts.generate"):
                order.append("tts")
                state = resources.snapshot()
                assert state["running_model"] is None
                assert state["running_tool"] == "tts.generate"

        first = asyncio.create_task(chat())
        await entered.wait()
        second = asyncio.create_task(tts())
        await asyncio.sleep(0)
        state = resources.snapshot()
        assert state["mode"] == "CHAT"
        assert state["lifecycle"] == "RUNNING"
        assert state["waiting_jobs"] == 1
        assert order == ["chat"]
        release.set()
        await asyncio.gather(first, second)
        assert order == ["chat", "tts"]
        assert resources.snapshot()["lifecycle"] == "IDLE"

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_failure_releases_lock_and_redacts_error():
    async def run():
        resources = ResourceCoordinator()
        with pytest.raises(RuntimeError):
            async with resources.job(ResourceMode.IMAGE):
                raise RuntimeError("secret api key")
        state = resources.snapshot()
        assert state["lifecycle"] == "FAILED"
        assert state["active_job"] is None
        assert "secret" not in str(state)
        async with resources.job(ResourceMode.CHAT):
            assert resources.snapshot()["last_failure"] is None
        assert resources.snapshot()["lifecycle"] == "IDLE"

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_cancel_active_and_queued_jobs():
    async def run():
        resources = ResourceCoordinator()
        entered = asyncio.Event()

        async def active():
            async with resources.job(ResourceMode.CHAT):
                entered.set()
                await asyncio.Event().wait()

        async def queued():
            async with resources.job(ResourceMode.TTS):
                pytest.fail("queued task acquired the lock before cancellation")

        first = asyncio.create_task(active())
        await entered.wait()
        second = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert resources.snapshot()["waiting_jobs"] == 1
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert resources.snapshot()["waiting_jobs"] == 0
        assert resources.snapshot()["active_job"] is not None
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert resources.snapshot()["lifecycle"] == "IDLE"
        async with resources.job(ResourceMode.WORK):
            assert resources.snapshot()["mode"] == "WORK"

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_idle_mode_cannot_be_a_job():
    async def run():
        resources = ResourceCoordinator()
        with pytest.raises(ValueError):
            async with resources.job(ResourceMode.IDLE):
                pass
        assert resources.snapshot()["waiting_jobs"] == 0

    asyncio.run(run())
