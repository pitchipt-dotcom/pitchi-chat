"""Synthetic reasoning channels only; no real patient/private reasoning fixtures."""
import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.models.base import Message, ModelError
from backend.app.models.qwen import QwenAdapter
from backend.app.models.response import ReasoningParser, split_response
from backend.app.storage.sqlite import HistoryStore
from backend.app.harness.contracts import RenderRequest


@pytest.mark.parametrize("tag", ["think", "reasoning", "analysis", "THINK"])
def test_tags_separate(tag):
    response = split_response(f"<{tag}>secret reasoning</{tag}>こんにちは")
    assert response.final_content == "こんにちは"
    assert response.reasoning_content == "secret reasoning"
    assert "secret reasoning" not in repr(response)
    assert "secret reasoning" not in response.model_dump_json()


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking", "analysis"])
def test_backend_fields(field):
    result = split_response({field: "secret reasoning", "content": "final"})
    assert result.final_content == "final" and result.reasoning_content == "secret reasoning"


@pytest.mark.parametrize("raw", ["<think>secret reasoning</think>", "<think>secret reasoning",
    "<think>secret</reasoning>final", "</think>secret", "<th", "<think bad>secret", "<think bad",
    {"reasoning_content": "secret reasoning", "content": None}])
def test_no_final_never_falls_back(raw):
    with pytest.raises(ModelError) as exc:
        split_response(raw)
    assert "secret" not in str(exc.value)


def test_plain_text_unchanged():
    response = split_response("  こんにちは\n")
    assert response.final_content == "  こんにちは\n" and response.reasoning_content is None


@pytest.mark.parametrize("width", range(1, 15))
def test_stream_split_tags_never_emit_reasoning(width):
    text = "<think>secret reasoning</think>こんにちは"
    parser = ReasoningParser(); visible = []; debug = []
    for start in range(0, len(text), width):
        final, thinking = parser.feed(text[start:start + width])
        visible.append(final); debug.append(thinking)
        assert "secret" not in "".join(visible)
    assert "".join(visible) == "こんにちは"
    assert "".join(debug) == "secret reasoning"
    assert parser.finish().final_content == "こんにちは"


def test_stream_explicit_channels_and_unclosed():
    parser = ReasoningParser()
    assert parser.feed("secret", "reasoning") == ("", "secret")
    assert parser.feed("final", "final") == ("final", "")
    assert parser.finish().reasoning_content == "secret"
    parser = ReasoningParser()
    assert parser.feed("<think>secret") == ("", "secret")
    with pytest.raises(ModelError):
        parser.finish()


def test_qwen_field_and_tag_response(settings):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                "choices": [{"message": {"reasoning_content": "first ", "content": "<think>second</think>final"}}]}))) as client:
            adapter = QwenAdapter(replace(settings, endpoint="http://qwen.test/chat"), client)
            result = await adapter.generate_response([Message("user", "hello")])
            assert result.final_content == "final" and "second" in result.reasoning_content
            assert await adapter.generate([]) == "final"
    asyncio.run(run())


def test_chat_debug_transient_history_and_continuation(settings):
    class Model:
        def __init__(self): self.calls = []
        async def generate(self, messages):
            self.calls.append(messages)
            return "<think>synthetic private thought</think>こんにちは"
    model = Model()
    with TestClient(create_app(settings, model)) as client:
        first = client.post("/api/chat", json={"message": "hello"})
        assert first.status_code == 200
        result = first.json(); cid = result["conversation_id"]
        assert result["reply"] == "こんにちは"
        assert result["reasoning_content"] == "synthetic private thought"
        assert client.post("/api/chat", json={"message": "continue", "conversation_id": cid}).status_code == 200
        assert model.calls[1][1] == Message("assistant", "こんにちは")
        assert "synthetic private thought" not in json.dumps(client.get(f"/api/conversations/{cid}").json())
    with HistoryStore(settings.database).connection() as db:
        for table in ("messages", "harness_audit"):
            assert "synthetic private thought" not in json.dumps([dict(row) for row in db.execute(f"SELECT * FROM {table}")])


def test_legacy_history_sanitized_without_destructive_migration(settings):
    store = HistoryStore(settings.database); store.initialize(); cid = str(uuid4())
    store.save_turn(cid, "hello", "final")
    with store.connection() as db:
        db.execute("UPDATE messages SET content=? WHERE role='assistant'", ("<think>old thought</think>old final",))
    assert store.history(cid)[1]["content"] == "old final"
    store.save_turn(cid, "next", "<think>new thought</think>new final")
    with store.connection() as db:
        assert db.execute("SELECT content FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0] == "new final"


@pytest.mark.parametrize("mode", ["library", "pt_learning", "clinical"])
def test_structured_evidence_synthesis_thoughts_do_not_enter_answer(tmp_path, mode):
    from test_services import book_response
    from test_evidence_chat import harness, Model as LibraryModel
    from test_pt_assistant import host, Model as PTModel, paper_response
    class Library(LibraryModel):
        async def generate(self, messages):
            return "<reasoning>synthetic thought</reasoning>" + await super().generate(messages)
    class PT(PTModel):
        async def generate_structured(self, messages, schema):
            return "<think>synthetic thought</think>" + await super().generate_structured(messages, schema)
    async def run():
        def handle(r):
            return httpx.Response(200, json=book_response() | {"limit": 3 if mode == "library" else 1}) if r.url.path == "/search" else httpx.Response(200, json=paper_response())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            if mode == "library":
                result = await harness(tmp_path, client, Library()).run([Message("user", "test")], library=True)
            else:
                result = await host(tmp_path, client, PT(mode)).run([Message("user", "test")], pt_mode=mode,
                    evidence_query="test", external_retrieval=True)
            assert "synthetic thought" not in result.model_dump_json()
            assert "synthetic thought" in result.reasoning_content
            assert "synthetic thought" not in result.content
    asyncio.run(run())


def test_renderer_receives_final_only(settings):
    from backend.app.harness.orchestrator import Harness
    from backend.app.harness.audit import AuditTrail
    from backend.app.resources.gpu import ResourceCoordinator
    class Model:
        async def generate(self, messages): return "<think>synthetic thought</think>こんにちは"
    class Renderer:
        async def render(self, request, **kwargs):
            assert request.content == "こんにちは"
            return request.content, False, False, "cloud_disabled"
    store = HistoryStore(settings.database); store.initialize()
    host = Harness(Model(), ResourceCoordinator(), AuditTrail(store), japanese_renderer=Renderer())
    assert asyncio.run(host.run([Message("user", "hello")])).content == "こんにちは"


def test_machine_contracts_reject_embedded_or_escaped_thinking():
    from backend.app.harness.evidence import parse_contract, SearchProposal
    from backend.app.harness.recovery import HarnessFailure
    for text in ('{"query":"<think>secret</think>test"}', '{"query":"\\u003cthink>secret"}'):
        with pytest.raises((HarnessFailure, ModelError)):
            parse_contract(text, SearchProposal)


@pytest.mark.parametrize("raw", ["<think>synthetic only</think>", "<think>synthetic unclosed"])
def test_reasoning_only_route_is_502_and_saves_no_turn(settings, raw):
    class Model:
        async def generate(self, messages): return raw
    with TestClient(create_app(settings, Model())) as client:
        result = client.post("/api/chat", json={"message": "hello"})
        assert result.status_code == 502
        assert "synthetic" not in result.text
        assert client.get("/api/conversations").json()["conversations"] == []


def test_tool_and_work_instruction_reject_reasoning(tmp_path):
    from test_harness import make_harness, definition, MockTool, proposal
    from backend.app.harness.recovery import HarnessFailure
    from backend.app.harness.validator import validate_arguments
    from backend.app.tools.pitch_work import WorkArguments
    from types import SimpleNamespace
    tool = MockTool()
    host = make_harness(tmp_path, proposal({"prompt": "<think>synthetic thought</think>final"}), (definition(tool),))
    with pytest.raises(HarnessFailure):
        asyncio.run(host.run([Message("user", "hello")]))
    assert tool.calls == []
    with pytest.raises(HarnessFailure):
        validate_arguments({"project": "demo", "instruction": "<reasoning>synthetic</reasoning>"},
                           SimpleNamespace(argument_schema=WorkArguments))


def test_image_compiler_thinking_never_persists_in_job(tmp_path):
    from test_image_jobs import Compiler, setup
    from test_comfyui import Protocol
    from backend.app.harness.image import ImageRequest
    class ThinkingCompiler(Compiler):
        async def generate_structured(self, messages, schema):
            return "<think>synthetic compiler thought</think>" + await super().generate_structured(messages, schema)
    async def run():
        protocol = Protocol()
        async with httpx.AsyncClient(transport=httpx.MockTransport(protocol.handle)) as client:
            host, worker, jobs, gpu, _, _ = setup(tmp_path, client, ThinkingCompiler())
            worker.start()
            job = worker.submit_image(ImageRequest(request="cat", backend="qwen-image", compile_only=True))
            await asyncio.wait_for(worker.queue.join(), 2)
            row = jobs.get(job["job_id"], private=True)
            assert row["status"] == "SUCCESS"
            assert "synthetic compiler thought" not in json.dumps(row)
            assert not protocol.graph
            await worker.close(); await gpu.close()
    asyncio.run(run())
