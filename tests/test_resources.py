import asyncio

import pytest
import httpx

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models.base import ModelError
from backend.app.storage.sqlite import HistoryStore


def test_resource_api_reports_chat_then_idle(tmp_path):
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        class Adapter:
            async def generate(self, messages):
                entered.set()
                await release.wait()
                return "mock reply"

        settings = Settings(tmp_path / "chat.db", "", "test-model", "", 1)
        app = create_app(settings, Adapter())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                request = asyncio.create_task(client.post("/api/chat", json={"message": "hello"}))
                await entered.wait()
                active = (await client.get("/api/resources")).json()
                assert active["mode"] == "CHAT"
                assert active["running_model"] == "qwen"  # HOST logical ID, not the private wire model name.
                assert active["active_job"]["id"]
                assert active["gpu_telemetry"] == "unavailable"
                assert active["process_management"] is False
                release.set()
                assert (await request).status_code == 200
                idle = (await client.get("/api/resources")).json()
                assert idle["active_job"] is None
                assert idle["lifecycle"] == "IDLE"

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_resource_api_failure_and_retry(tmp_path):
    async def run():
        class Adapter:
            failed = False

            async def generate(self, messages):
                if not self.failed:
                    self.failed = True
                    raise ModelError("mock unavailable")
                return "retry success"

        settings = Settings(tmp_path / "chat.db", "", "test", "", 1)
        app = create_app(settings, Adapter())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                assert (await client.post("/api/chat", json={"message": "hello"})).status_code == 502
                assert (await client.get("/api/resources")).json()["lifecycle"] == "FAILED"
                assert HistoryStore(settings.database).list_conversations() == []
                assert (await client.post("/api/chat", json={"message": "retry"})).status_code == 200
                assert (await client.get("/api/resources")).json()["lifecycle"] == "IDLE"

    asyncio.run(asyncio.wait_for(run(), timeout=5))
