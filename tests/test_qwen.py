import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from backend.app.config import load_settings
from backend.app.models.base import Message, ModelError
from backend.app.models.qwen import QwenAdapter


def test_qwen_wire_protocol(settings, monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    configured = replace(settings, endpoint="http://qwen.test/chat")

    def handle(request):
        assert str(request.url) == configured.endpoint
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert json.loads(request.content) == {
            "model": "test-qwen", "stream": False,
            "messages": [{"role": "user", "content": "hello"}],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "こんにちは"}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await QwenAdapter(configured, client).generate([Message("user", "hello")])

    assert asyncio.run(run()) == "こんにちは"


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="secret upstream body"),
    httpx.Response(200, json={}),
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": " "}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": "tool", "tool_calls": [{}]}}]}),
])
def test_qwen_bad_responses(settings, response):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            with pytest.raises(ModelError) as error:
                await QwenAdapter(replace(settings, endpoint="http://qwen.test/chat"), client).generate([])
            assert "secret" not in str(error.value)
            assert "qwen.test" not in str(error.value)

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_qwen_network_failures(settings, error_type):
    def fail(request):
        raise error_type("secret endpoint", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with pytest.raises(ModelError) as error:
                await QwenAdapter(replace(settings, endpoint="http://qwen.test/chat"), client).generate([])
            assert "secret" not in str(error.value)

    asyncio.run(run())


def test_config_file_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text('[storage]\ndatabase = "data/chat.db"\n[model]\nadapter = "qwen"\n'
                    'endpoint = "http://qwen.test/custom/chat"\nname = "custom-model"\n'
                    'api_key_env = "CUSTOM_KEY"\ntimeout_seconds = 12\n')
    monkeypatch.setenv("PITCH_CHAT_CONFIG", str(path))
    settings = load_settings()
    assert settings.database == tmp_path / "data/chat.db"
    assert settings.endpoint == "http://qwen.test/custom/chat"
    assert settings.model_name == "custom-model"
    assert settings.api_key_env == "CUSTOM_KEY"
    assert settings.timeout_seconds == 12


@pytest.mark.parametrize("endpoint", ["ftp://host/chat", "http://key:secret@host/chat", "http://host/chat?key=secret"])
def test_reject_endpoint_credentials(tmp_path, endpoint):
    path = tmp_path / "settings.toml"
    path.write_text('[storage]\ndatabase = "chat.db"\n[model]\nadapter = "qwen"\n'
                    f'endpoint = "{endpoint}"\nname = "test"\n')
    with pytest.raises(ValueError):
        load_settings(path)
