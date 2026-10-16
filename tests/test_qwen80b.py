"""80B selection/lifecycle mocks, never proof of real 80B or CUDA inference."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.app.config import ModelSettings, load_settings
from backend.app.harness.audit import AuditTrail
from backend.app.harness.orchestrator import Harness
from backend.app.harness.recovery import HarnessFailure
from backend.app.models.registry import ModelRegistry
from backend.app.models.qwen import QwenAdapter
from backend.app.models.base import ModelNotConfigured
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.resources.manager import GPUManager
from backend.app.storage.sqlite import HistoryStore
from test_gpu_manager import Driver, Inspector


def test_qwen80b_separate_registry_and_owned_profile_requirement(settings):
    async def run():
        spec = ModelSettings("qwen80b", "qwen", "http://localhost:1234/v1/chat/completions", "synthetic-80b", "", 5)
        configured = replace(settings, models=(spec,))
        async with httpx.AsyncClient() as client:
            registry = ModelRegistry(configured, client, managed_profiles=frozenset())
            with pytest.raises(ModelNotConfigured): registry.resolve("qwen80b")
            registry = ModelRegistry(configured, client, managed_profiles=frozenset({"qwen80b"}))
            assert isinstance(registry.resolve("qwen80b").adapter, QwenAdapter)
            state = registry.public_state()
            assert "synthetic-80b" not in str(state) and "1234" not in str(state)
            assert next(s for s in state["models"] if s["id"] == "qwen80b")["connection_mode"] == "host_owned"
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["ready_stop", "foreign", "busy", "wrong_owner", "release_failure"])
def test_qwen80b_harness_gpu_lifecycle_boundaries(tmp_path, settings, scenario):
    async def run():
        store = HistoryStore(tmp_path / "private.db"); store.initialize()
        inspector = Inspector()
        driver = Driver(inspector)
        driver.profile = SimpleNamespace(device="gpu", start_timeout=1, stop_timeout=1)
        manager = GPUManager(store, inspector, {"qwen80b": driver}, release_timeout=.02, queue_timeout=.02)
        manager.open()
        async with httpx.AsyncClient() as client:
            registry = ModelRegistry(settings, client, {"qwen80b": QwenAdapter(settings, client)}, frozenset({"qwen80b"}))
            harness = Harness(None, ResourceCoordinator(manager), AuditTrail(store), models=registry)
            if scenario == "foreign":
                inspector.pids.add(999)
                with pytest.raises(HarnessFailure): await harness.model_lifecycle("qwen80b", "start")
                assert not driver.started and not driver.events
                inspector.pids.clear()
            else:
                assert (await harness.model_lifecycle("qwen80b", "start"))["state"] == "READY"
                if scenario == "busy":
                    async with harness.resources.job("CHAT"):
                        with pytest.raises(HarnessFailure): await harness.model_lifecycle("qwen80b", "stop")
                    assert driver.started
                elif scenario == "wrong_owner":
                    manager.drivers["other"] = Driver(inspector, 11)
                    from backend.app.resources.manager import GPUUnsafe
                    with pytest.raises(GPUUnsafe): await manager.stop_idle("other")
                    assert driver.started
                elif scenario == "release_failure":
                    driver.fail_stop = True
                    with pytest.raises(HarnessFailure): await harness.model_lifecycle("qwen80b", "stop")
                    assert manager.snapshot()["state"] == "ERROR"
                    driver.fail_stop = False
                    await driver.stop()  # mock cleanup only; real recovery never adopts a PID
                else:
                    assert (await harness.model_lifecycle("qwen80b", "stop"))["state"] == "STOPPED"
                    assert driver.events == ["start", "stop"]
            await manager.close()
    asyncio.run(run())


def test_qwen80b_config_preserves_cpu_qwen(tmp_path):
    from unittest.mock import patch
    path = tmp_path / "settings.toml"; path.touch()
    config = {"storage": {"database": "synthetic.db"}, "model": {"endpoint": "http://localhost:1234/chat", "name": "synthetic-cpu"},
              "models": {"qwen80b": {"adapter": "qwen", "endpoint": "http://localhost:1235/chat", "name": "synthetic-80b"}}}
    with patch("backend.app.config.tomllib.load", return_value=config):
        settings = load_settings(path)
    assert settings.default_model == "qwen" and settings.model_name == "synthetic-cpu"
    assert settings.models[-1].id == "qwen80b" and settings.models[-1].adapter == "qwen"


@pytest.mark.parametrize("endpoint", ["https://example.org/chat", "http://localhost:9999/chat"])
def test_owned_model_endpoint_cannot_diverge_from_lifecycle(tmp_path, settings, endpoint):
    async def run():
        store = HistoryStore(tmp_path / "audit.db"); store.initialize()
        profile = SimpleNamespace(health_url="http://localhost:1234/health")
        resource = ResourceCoordinator(SimpleNamespace(drivers={"qwen80b": SimpleNamespace(profile=profile)}))
        spec = ModelSettings("qwen80b", "qwen", endpoint, "synthetic", "", 1)
        async with httpx.AsyncClient() as client:
            models = ModelRegistry(replace(settings, models=(spec,)), client, managed_profiles=frozenset({"qwen80b"}))
            harness = Harness(None, resource, AuditTrail(store), models=models)
            with pytest.raises(HarnessFailure): harness.validate_owned_model(models.resolve("qwen80b"))
    asyncio.run(run())
