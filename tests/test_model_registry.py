"""Model Registry tests: only synthetic endpoints and mock adapters/transports."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from backend.app.config import ModelSettings, load_settings
from backend.app.harness.audit import AuditTrail
from backend.app.harness.orchestrator import Harness
from backend.app.harness.contracts import ErrorCode, FinalStatus
from backend.app.harness.recovery import HarnessFailure
from backend.app.main import create_app
from backend.app.models.base import Message, ModelError, ModelNotConfigured
from backend.app.models.registry import ModelRegistry
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore


def config_file(tmp_path, extra="", legacy=True):
    path = tmp_path / "settings.toml"
    path.write_text('[storage]\ndatabase = "history.db"\n' + (
        '[model]\nadapter = "qwen"\nendpoint = ""\nname = "legacy-qwen"\n' if legacy else ""
    ) + extra)
    return path


def test_legacy_and_registry_configuration(tmp_path):
    settings = load_settings(config_file(tmp_path, '[models.bonsai]\nendpoint = "http://bonsai.test/chat"\n'
                                        'name = "configured-bonsai"\napi_key_env = "BONSAI_KEY"\n'
                                        'timeout_seconds = 12\n[chat]\ndefault_model = "bonsai"\n'))
    assert settings.model_name == "legacy-qwen"
    assert settings.endpoint == ""
    assert settings.default_model == "bonsai"
    assert settings.models[1] == ModelSettings("bonsai", "bonsai", "http://bonsai.test/chat", "configured-bonsai", "BONSAI_KEY", 12)


def test_registry_only_configuration(tmp_path):
    settings = load_settings(config_file(tmp_path, '[models.qwen]\nendpoint = "http://qwen.test/chat"\n', legacy=False))
    assert settings.endpoint == "http://qwen.test/chat"
    assert settings.default_model == "qwen"
    assert settings.models[1].endpoint == ""


@pytest.mark.parametrize("extra", [
    '[models.qwen]\nendpoint = ""\n', '[models.unknown]\nendpoint = ""\n',
    '[models.bonsai]\nadapter = "openai"\n', '[models.bonsai]\ncommand = "unsafe"\n',
    '[models.bonsai]\napi_key = "synthetic"\n', '[models.bonsai]\nendpoint = 123\n',
    '[models.bonsai]\nendpoint = "http://user:password@bonsai.test/chat"\n',
    '[models.bonsai]\nendpoint = "http://bonsai.test/chat?key=synthetic"\n',
    '[models.bonsai]\napi_key_env = "NOT AN ENV NAME"\n', '[models.bonsai]\nname = " "\n',
    '[models.bonsai]\ntimeout_seconds = nan\n', '[models.bonsai]\ntimeout_seconds = inf\n',
    '[models.bonsai]\ntimeout_seconds = true\n', '[models.bonsai]\ntimeout_seconds = 601\n',
    '[chat]\ndefault_model = "unknown"\n', '[chat]\ndefault_model = []\n',
    '[chat]\nendpoint = "http://unsafe.test"\n',
])
def test_invalid_registry_configuration_is_rejected(tmp_path, extra):
    with pytest.raises(ValueError):
        load_settings(config_file(tmp_path, extra))


def test_unavailable_and_unknown_models_never_connect(settings):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("must not connect"))) as client:
            registry = ModelRegistry(settings, client)
            for model_id in (None, "qwen", "bonsai", "http://unknown.test", ""):
                with pytest.raises(ModelNotConfigured) as error:
                    registry.resolve(model_id)
                assert "unknown.test" not in str(error.value)
            state = registry.public_state()
            assert [model["id"] for model in state["models"]] == ["qwen", "bonsai"]
            assert all(not model["available"] for model in state["models"])
            assert state["process_management"] is False
    asyncio.run(run())


def test_bonsai_protocol_and_private_configuration(settings, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_BONSAI_KEY", "test-secret")
    spec = ModelSettings("bonsai", "bonsai", "http://bonsai.test/chat", "private-wire-name", "SYNTHETIC_BONSAI_KEY", 2)
    def handle(request):
        assert str(request.url) == spec.endpoint
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert json.loads(request.content)["model"] == "private-wire-name"
        return httpx.Response(200, json={"choices": [{"message": {"content": "直接日本語"}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            registry = ModelRegistry(replace(settings, models=(spec,), default_model="bonsai"), client)
            assert await registry.resolve().adapter.generate([Message("user", "test")]) == "直接日本語"
            public = json.dumps(registry.public_state())
            for private in (spec.endpoint, spec.model_name, spec.api_key_env, "test-secret"):
                assert private not in public
    asyncio.run(run())


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def generate(self, messages):
        self.calls.append(list(messages))
        return self.reply


def test_selection_and_context_through_harness_api(settings):
    async def run():
        qwen, bonsai = FakeModel("Qwen reply"), FakeModel("BONSAI reply")
        app = create_app(settings, adapters={"qwen": qwen, "bonsai": bonsai})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                state = (await client.get("/api/models")).json()
                assert state["default_model"] == "qwen"
                assert all(model["available"] for model in state["models"])
                first = await client.post("/api/chat", json={"message": "first"})
                assert first.json()["reply"] == "Qwen reply"
                cid = first.json()["conversation_id"]
                second = await client.post("/api/chat", json={"message": "second", "model": "bonsai", "conversation_id": cid})
                assert second.json()["reply"] == "BONSAI reply"
                assert set(second.json()) == {"conversation_id", "reply", "model_selection", "renderer_used", "cloud_used"}
                assert not second.json()["renderer_used"] and not second.json()["cloud_used"]
                assert second.json()["model_selection"]["requested_model"] == "bonsai"
                assert second.json()["model_selection"]["actual_model_used"] == "bonsai"
                assert [m.content for m in bonsai.calls[0]] == ["first", "Qwen reply", "second"]
                assert len(qwen.calls) == 1
                with app.state.harness.audit.store.connection() as db:
                    events = [json.loads(row["metadata"]) for row in db.execute(
                        "SELECT metadata FROM harness_audit WHERE request_id = ?", (second.headers["X-Request-ID"],))]
                assert any(event["selected_model"] == "bonsai" for event in events)
                assert all(event["cloud_used"] is False and event["renderer_used"] is False for event in events)
    asyncio.run(run())


@pytest.mark.parametrize("model", ["unknown", "http://unsafe.test/chat", 123, {}, ""])
def test_request_cannot_select_infrastructure(settings, model):
    async def run():
        adapter = FakeModel("unused")
        app = create_app(settings, adapter)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                assert (await client.post("/api/chat", json={"message": "test", "model": model})).status_code == 422
                assert adapter.calls == []
    asyncio.run(run())


def test_unconfigured_selection_preserves_history(settings):
    async def run():
        adapter = FakeModel("first reply")
        app = create_app(settings, adapter)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                first = (await client.post("/api/chat", json={"message": "first"})).json()
                failed = await client.post("/api/chat", json={"message": "not saved", "model": "bonsai", "conversation_id": first["conversation_id"]})
                assert failed.status_code == 503
                assert len(adapter.calls) == 1
                messages = (await client.get(f'/api/conversations/{first["conversation_id"]}')).json()["messages"]
                assert len(messages) == 2
    asyncio.run(run())


def test_default_bonsai_and_health(settings):
    async def run():
        spec = ModelSettings("bonsai", "bonsai", "http://bonsai.test/chat", "wire-name", "", 1)
        app = create_app(replace(settings, models=(spec,), default_model="bonsai"), adapters={"bonsai": FakeModel("default reply")})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                assert (await client.get("/health")).json()["model"] == {"adapter": "bonsai", "configured": True}
                assert (await client.post("/api/chat", json={"message": "test"})).json()["reply"] == "default reply"
    asyncio.run(run())


def test_selected_model_keeps_resource_exclusion_and_logical_identity(settings):
    async def run():
        resources = ResourceCoordinator()
        entered, release = asyncio.Event(), asyncio.Event()
        class Blocking:
            async def generate(self, messages):
                entered.set()
                await release.wait()
                return "answer"
        store = HistoryStore(settings.database)
        store.initialize()
        async with httpx.AsyncClient() as client:
            registry = ModelRegistry(settings, client, {"qwen": FakeModel("other"), "bonsai": Blocking()})
            harness = Harness(None, resources, AuditTrail(store), models=registry)
            task = asyncio.create_task(harness.run([Message("user", "private")], model_id="bonsai"))
            await entered.wait()
            assert harness.resource_state().running_model == "bonsai"
            queued = asyncio.create_task(harness.run([Message("user", "second")], model_id="qwen"))
            await asyncio.sleep(0.01)
            assert harness.resource_state().waiting_jobs == 1
            release.set()
            assert [result.content for result in await asyncio.gather(task, queued)] == ["answer", "other"]
            assert harness.resource_state().active_job is None
    asyncio.run(asyncio.wait_for(run(), 5))


def test_bonsai_rejects_tool_calls(settings):
    async def run():
        spec = ModelSettings("bonsai", "bonsai", "http://bonsai.test/chat", "bonsai", "", 1)
        response = {"choices": [{"message": {"content": "unused", "tool_calls": [{}]}}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
            registry = ModelRegistry(replace(settings, models=(spec,)), client)
            with pytest.raises(ModelError):
                await registry.resolve("bonsai").adapter.generate([Message("user", "test")])
    asyncio.run(run())


def test_selected_model_uses_its_own_bounded_timeout(settings):
    async def run():
        class Slow:
            async def generate(self, messages):
                await asyncio.sleep(10)
                return "late"
        spec = ModelSettings("bonsai", "bonsai", "", "private-wire-name", "", 0.01)
        store = HistoryStore(settings.database)
        store.initialize()
        resources = ResourceCoordinator()
        async with httpx.AsyncClient() as client:
            registry = ModelRegistry(replace(settings, models=(spec,)), client, {"bonsai": Slow()})
            harness = Harness(None, resources, AuditTrail(store), models=registry)
            with pytest.raises(HarnessFailure) as error:
                await harness.run([Message("user", "test")], model_id="bonsai")
            assert error.value.code == ErrorCode.TIMEOUT
            assert error.value.status == FinalStatus.STALLED
            await asyncio.sleep(0)
            assert harness.resource_state().active_job is None
    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("change,overrides", [
    ({"default_model": "unknown"}, {}),
    ({}, {"unknown": FakeModel("unused")}),
    ({"models": (ModelSettings("bonsai", "bonsai", "", "test", "", 1),) * 2}, {}),
])
def test_invalid_host_registry_fails_before_execution(settings, change, overrides):
    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                ModelRegistry(replace(settings, **change), client, overrides)
    asyncio.run(run())
