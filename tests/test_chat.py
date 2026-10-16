import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.models.base import ModelError


class FakeAdapter:
    def __init__(self):
        self.calls = []

    async def generate(self, messages):
        self.calls.append(list(messages))
        await asyncio.sleep(0)
        return "返答: " + messages[-1].content


def test_chat_context_and_persistence(settings):
    adapter = FakeAdapter()
    with TestClient(create_app(settings, adapter)) as client:
        assert client.get("/health").json()["storage"] == "ok"
        assert client.get("/").status_code == 200
        assert client.get("/static/chat.js").status_code == 200
        first = client.post("/api/chat", json={"message": "こんにちは"})
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]
        second = client.post("/api/chat", json={"message": "続き", "conversation_id": conversation_id})
        assert second.status_code == 200
        assert [m.role for m in adapter.calls[1]] == ["user", "assistant", "user"]
        assert adapter.calls[1][0].content == "こんにちは"
        other = client.post("/api/chat", json={"message": "別の会話"})
        assert other.json()["conversation_id"] != conversation_id
        assert len(adapter.calls[2]) == 1
    with TestClient(create_app(settings, FakeAdapter())) as restarted:
        history = restarted.get(f"/api/conversations/{conversation_id}").json()["messages"]
        assert [m["content"] for m in history] == ["こんにちは", "返答: こんにちは", "続き", "返答: 続き"]
        assert len(restarted.get("/api/conversations").json()["conversations"]) == 2


@pytest.mark.parametrize("payload", [
    {"message": " "}, {"message": ""}, {"message": "x" * 16001},
    {"message": "hi", "conversation_id": "invalid"},
    {"message": "hi", "tools": []}, {},
])
def test_validation(settings, payload):
    adapter = FakeAdapter()
    with TestClient(create_app(settings, adapter)) as client:
        assert client.post("/api/chat", json=payload).status_code == 422
        assert adapter.calls == []
        assert client.get("/api/conversations").json() == {"conversations": []}


def test_unknown_conversation(settings):
    with TestClient(create_app(settings, FakeAdapter())) as client:
        unknown = str(uuid4())
        assert client.get(f"/api/conversations/{unknown}").status_code == 404
        assert client.post("/api/chat", json={"message": "hi", "conversation_id": unknown}).status_code == 404


def test_unconfigured_model(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["model"]["configured"] is False
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 503
        assert client.get("/api/conversations").json()["conversations"] == []


def test_failure_does_not_save_partial_turn(settings):
    adapter = FakeAdapter()
    with TestClient(create_app(settings, adapter)) as client:
        conversation_id = client.post("/api/chat", json={"message": "first"}).json()["conversation_id"]

        async def fail(messages):
            raise ModelError("接続エラー")

        adapter.generate = fail
        assert client.post("/api/chat", json={"message": "failed", "conversation_id": conversation_id}).status_code == 502
        assert client.post("/api/chat", json={"message": "new failed"}).status_code == 502
        assert len(client.get(f"/api/conversations/{conversation_id}").json()["messages"]) == 2
        assert len(client.get("/api/conversations").json()["conversations"]) == 1


def test_concurrent_turns_preserve_context(settings):
    adapter = FakeAdapter()
    app = create_app(settings, adapter)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                first = await client.post("/api/chat", json={"message": "first"})
                conversation_id = first.json()["conversation_id"]
                responses = await asyncio.gather(*[
                    client.post("/api/chat", json={"message": text, "conversation_id": conversation_id})
                    for text in ["second", "third"]
                ])
                assert all(response.status_code == 200 for response in responses)
                assert [len(call) for call in adapter.calls] == [1, 3, 5]
                history = (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"]
                assert [message["role"] for message in history] == ["user", "assistant"] * 3

    asyncio.run(run())


def test_chat_api_preserves_payload_and_returns_audit_request_id(tmp_path):
    import httpx
    from backend.app.config import Settings
    from backend.app.main import create_app

    class Legacy:
        async def generate(self, messages):
            return '{"unchanged":12}'

    async def run():
        app = create_app(Settings(tmp_path / "api.db", "", "test-model", "", 1), Legacy())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                response = await client.post("/api/chat", json={"message": "private request"})
                assert response.status_code == 200
                assert set(response.json()) == {"conversation_id", "reply", "model_selection", "renderer_used", "cloud_used"}
                assert not response.json()["renderer_used"] and not response.json()["cloud_used"]
                assert response.json()["model_selection"]["actual_model_used"] == "qwen"
                assert response.json()["reply"] == '{"unchanged":12}'
                request_id = response.headers["X-Request-ID"]
                with app.state.harness.audit.store.connection() as db:
                    rows = db.execute("SELECT metadata FROM harness_audit WHERE request_id = ?", (request_id,)).fetchall()
                assert rows
                assert "private request" not in str([row["metadata"] for row in rows])

    asyncio.run(run())
