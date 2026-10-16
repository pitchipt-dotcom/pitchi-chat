"""Grounded Chat E2E mocks. No live model, book dataset or GPU is used."""

import asyncio
import json

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ErrorCode
from backend.app.harness.evidence import AnswerProposal, EvidencePacket, evidence_from_result, validate_answer, parse_contract
from backend.app.harness.evidence import SearchProposal
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.models.base import Message
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.tools.config import ServiceSettings
from backend.app.tools.services import service_registry
from backend.app.storage.sqlite import HistoryStore
from test_services import book_response


def answer():
    return {"book_claims": [{"evidence_id": "E1", "quote": "synthetic evidence"}],
            "analysis": "AIによる整理です。", "limitations": "検索抜粋の範囲のみ。"}


class Model:
    def __init__(self, final=None):
        self.inputs = []
        self.final = answer() if final is None else final
    async def generate(self, messages):
        self.inputs.append(messages)
        return json.dumps({"query": "test"} if len(self.inputs) == 1 else self.final)


def harness(tmp_path, client, model):
    store = HistoryStore(tmp_path / "history.db")
    store.initialize()
    registry = service_registry((ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),), client, tmp_path)
    return Harness(model, ResourceCoordinator(), AuditTrail(store), registry=registry,
                   policy=TurnPolicy(allowed_tools=frozenset({"book.search"})))


def test_one_tool_two_models_evidence_host_citations_and_private_audit(tmp_path):
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=book_response() | {"limit": 3})
        model = Model()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            host = harness(tmp_path, client, model)
            result = await host.run([Message("assistant", "OLD PRIVATE CLAIM"), Message("user", "test question")], library=True)
            assert len(calls) == 1 and len(model.inputs) == 2
            packet = json.loads(model.inputs[1][-1].content)["evidence"]
            assert packet["items"][0]["reference"]["title"] == "Synthetic book"
            assert packet["items"][0]["reference"]["pdf_page"] == 3
            assert "OLD PRIVATE CLAIM" not in model.inputs[1][-1].content
            assert "RAW OCR" not in model.inputs[1][-1].content
            assert "蔵書に基づく内容" in result.content and "AIによる整理・推論" in result.content
            assert "PDFページ: 3" in result.content and "citation:" in result.content
            assert result.grounded_answer["grounding"] == "exact_quotes_not_semantic_entailment"
            with host.audit.store.connection() as db:
                audit = json.dumps([dict(row) for row in db.execute("SELECT * FROM harness_audit")])
            assert "Synthetic book" not in audit and "test question" not in audit
            assert not result.renderer_used and not result.cloud_used
    asyncio.run(run())


@pytest.mark.parametrize("final", [
    answer() | {"book_claims": [{"evidence_id": "E2", "quote": "synthetic evidence"}]},
    answer() | {"book_claims": [{"evidence_id": "E1", "quote": "invented statement"}]},
    answer() | {"book_claims": [{"evidence_id": "E1", "quote": "synthetic evidence", "page": 999}]},
    answer() | {"tool_call": {"tool": "book.search"}},
    answer() | {"citation": "invented book"},
])
def test_invented_grounding_and_second_tool_rejected(tmp_path, final):
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=book_response() | {"limit": 3})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(HarnessFailure):
                await harness(tmp_path, client, Model(final)).run([Message("user", "test")], library=True)
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("quote_field", [{}, {"quote": None}])
def test_id_only_selection_materializes_verbatim_host_evidence(tmp_path, quote_field):
    async def run():
        final = answer() | {"book_claims": [{"evidence_id": "E1", **quote_field}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=book_response() | {"limit": 3}))) as client:
            result = await harness(tmp_path, client, Model(final)).run([Message("user", "test")], library=True)
            grounded = result.grounded_answer
            assert grounded["book_claims"][0]["quote"] == grounded["evidence"]["items"][0]["reference"]["snippet"]
            assert "citation:" in result.content
    asyncio.run(run())


def test_unknown_id_only_selection_rejected(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=book_response() | {"limit": 3}))) as client:
            with pytest.raises(HarnessFailure):
                await harness(tmp_path, client, Model(answer() | {"book_claims": [{"evidence_id": "E2"}]})).run([Message("user", "test")], library=True)
    asyncio.run(run())


def test_local_generation_contract_cannot_generate_quote(tmp_path):
    class StructuredModel(Model):
        async def generate_structured(self, messages, schema):
            self.inputs.append(messages)
            if len(self.inputs) == 1:
                return json.dumps({"query": "test"})
            selection = schema["$defs"]["EvidenceID"]
            assert set(selection["properties"]) == {"evidence_id"}
            assert selection["additionalProperties"] is False
            assert selection["properties"]["evidence_id"]["enum"] == ["E1"]
            return json.dumps(answer() | {"book_claims": [{"evidence_id": "E1"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=book_response() | {"limit": 3}))) as client:
            result = await harness(tmp_path, client, StructuredModel()).run([Message("user", "test")], library=True)
            assert result.grounded_answer["book_claims"][0]["quote"] == "synthetic evidence"
    asyncio.run(run())


def test_empty_evidence_generation_cannot_claim_a_book():
    from backend.app.harness.evidence import answer_generation_schema
    schema = answer_generation_schema(EvidencePacket(items=[]))
    assert schema["properties"]["book_claims"]["maxItems"] == 0


@pytest.mark.parametrize("question,term", [('「topic」を検索', 'topic'), ('『 topic 』の本', 'topic'),
                                          ('topicの本', None), ('「one」と「two」', None), ('「 」', None)])
def test_explicit_single_search_term_narrows_host_generation_contract(question, term):
    from backend.app.harness.evidence import search_generation_schema
    allowed = search_generation_schema(question)["properties"]["query"].get("enum")
    assert allowed == ([term] if term else None)


def test_model_cannot_override_explicit_user_search_term(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("No tool dispatch"))) as client:
            with pytest.raises(HarnessFailure):
                await harness(tmp_path, client, Model()).run([Message("user", "「other」を検索")], library=True)
    asyncio.run(run())


def test_no_evidence_cannot_claim_a_book_statement(tmp_path):
    async def run():
        empty = {"query": "test", "count": 0, "offset": 0, "limit": 3, "returned": 0, "has_more": False, "results": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=empty))) as client:
            with pytest.raises(HarnessFailure):
                await harness(tmp_path, client, Model()).run([Message("user", "test")], library=True)
            result = await harness(tmp_path, client, Model(answer() | {"book_claims": []})).run([Message("user", "test")], library=True)
            assert "本の記載として回答できません" in result.content
    asyncio.run(run())


@pytest.mark.parametrize("raw", ['{"query":"a","query":"b"}', '{"query":"a","endpoint":"bad"}', 'not JSON', '{"query":"   "}'])
def test_invalid_search_proposal_before_tool(tmp_path, raw):
    class BadModel:
        async def generate(self, messages):
            return raw
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("No execution"))) as client:
            with pytest.raises(HarnessFailure):
                await harness(tmp_path, client, BadModel()).run([Message("user", "test")], library=True)
    asyncio.run(run())


@pytest.mark.parametrize("raw", ['<think>unclosed {"query":"bad"}', '<think><think>x</think>{"query":"bad"}',
                                  'prefix {"query":"bad"}', '```json\n{"query":"bad"}\n``` extra'])
def test_unrecognized_model_envelopes_fail_closed(raw):
    with pytest.raises(HarnessFailure):
        parse_contract(raw, SearchProposal)


def test_framing_never_executes_thinking_proposals():
    raw = '<think>{"tool":"image.generate","arguments":{}}</think>\n```json\n{"query":"test"}\n```'
    assert parse_contract(raw, SearchProposal).query == "test"


@pytest.mark.parametrize("raw,category", [
    ('PRIVATE not JSON', 'invalid_json'), ('{}', 'missing_fields'),
    ('{"query":"test","PRIVATE":1}', 'extra_fields'), ('{"query":123}', 'invalid_value'),
])
def test_contract_failure_audits_classifications_not_payload(tmp_path, raw, category):
    class InvalidModel:
        async def generate(self, messages):
            return raw
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("No execution"))) as client:
            host = harness(tmp_path, client, InvalidModel())
            with pytest.raises(HarnessFailure):
                await host.run([Message("user", "PRIVATE QUESTION")], library=True)
            with host.audit.store.connection() as db:
                audit = json.dumps([dict(row) for row in db.execute("SELECT * FROM harness_audit")])
                final = json.loads(db.execute("SELECT metadata FROM harness_audit ORDER BY id DESC LIMIT 1").fetchone()["metadata"])
            assert final["contract_failure"] == category
            assert "PRIVATE" not in audit
    asyncio.run(run())


def test_library_api_success_history_and_failed_turn_not_saved(settings, monkeypatch):
    from backend.app.main import create_app
    from backend.app.tools.book import BookAdapter
    from backend.app.harness.contracts import ToolResult
    from backend.app.tools.book import BookReference
    raw = book_response()["results"][0]
    async def fake(self, arguments, backend):
        ref = {"book_id": raw["book_id"], "title": raw["book_title"], "pdf_page": raw["pdf_page"],
               "book_pdf_page": raw["book_pdf_page"], "citation_id": raw["citation_id"], "citation": raw["citation"],
               "snippet": raw["snippet"], "source_ref": raw["source_url"], "viewer_ref": raw["viewer_url"]}
        return ToolResult(success=True, result_type="structured", http_status=200, output={
            "count": 1, "offset": 0, "limit": 3, "returned": 1, "has_more": False, "references": [ref]})
    monkeypatch.setattr(BookAdapter, "execute", fake)
    model = Model()
    async def run():
        app = create_app(settings, adapter=model, services=(ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                result = await client.post("/api/chat", json={"message": "test", "mode": "library"})
                assert result.status_code == 200 and result.json()["grounded_answer"]
                conversation = result.json()["conversation_id"]
                model.final = answer() | {"book_claims": [{"evidence_id": "E1", "quote": "invented"}]}
                model.inputs.clear()
                failure = await client.post("/api/chat", json={"message": "test", "mode": "library", "conversation_id": conversation})
                assert failure.status_code == 502
                assert len(app.state.harness.audit.store.history(conversation)) == 2
    asyncio.run(run())


def test_structured_local_adapter_wire_contract(settings):
    from dataclasses import replace
    from backend.app.models.qwen import QwenAdapter
    async def run():
        def handle(request):
            payload = json.loads(request.content)
            assert payload["response_format"]["type"] == "json_schema"
            assert payload["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
            assert payload["max_tokens"] == 1024 and payload["temperature"] == 0
            assert "tools" not in payload
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer())}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            model = QwenAdapter(replace(settings, endpoint="http://localhost/v1/chat/completions"), client)
            raw = await model.generate_structured([Message("user", "synthetic")], AnswerProposal.model_json_schema())
            assert parse_contract(raw, AnswerProposal).analysis
    asyncio.run(run())


def test_library_cannot_send_evidence_to_public_model_endpoint(settings, tmp_path):
    from dataclasses import replace
    from backend.app.models.registry import ModelRegistry
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("No cloud/model dispatch"))) as client:
            host = harness(tmp_path, client, None)
            host.models = ModelRegistry(replace(settings, endpoint="https://public.test/v1/chat/completions"), client)
            with pytest.raises(HarnessFailure) as failure:
                await host.run([Message("user", "test")], library=True)
            assert failure.value.code == ErrorCode.POLICY_DENIED
    asyncio.run(run())


def test_library_smoke_reports_metadata_only(monkeypatch, capsys):
    import sys
    from scripts import test_library_chat
    from backend.app.harness.contracts import ToolResult
    from backend.app.tools.book import BookReference
    row = book_response()["results"][0]
    ref = {"book_id": row["book_id"], "title": row["book_title"], "pdf_page": row["pdf_page"],
           "book_pdf_page": row["book_pdf_page"], "citation_id": row["citation_id"], "citation": row["citation"],
           "snippet": row["snippet"], "source_ref": row["source_url"], "viewer_ref": row["viewer_url"]}
    packet = evidence_from_result(ToolResult(success=True, result_type="structured", http_status=200,
             output={"count": 1, "offset": 0, "limit": 3, "returned": 1, "has_more": False, "references": [ref]}))
    validated = validate_answer(json.dumps(answer()), packet)
    calls = []
    original = httpx.Client
    def handle(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"grounded_answer": validated.model_dump(mode="json"),
                                       "reply": "蔵書に基づく内容 AIによる整理・推論 citation: PDFページ:"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(sys, "argv", ["test_library_chat"])
    test_library_chat.main()
    output = capsys.readouterr().out
    assert "SUCCESS" in output and "Synthetic book" not in output and "synthetic evidence" not in output
    assert calls == ["GET", "POST"]


def test_empty_model_content_audits_only_safe_classification(settings, tmp_path):
    from dataclasses import replace
    from backend.app.models.registry import ModelRegistry
    async def run():
        def handle(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "PRIVATE MODEL BODY"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            host = harness(tmp_path, client, None)
            host.models = ModelRegistry(replace(settings, endpoint="http://localhost/v1/chat/completions"), client)
            with pytest.raises(Exception):
                await host.run([Message("user", "PRIVATE QUESTION")], library=True)
            with host.audit.store.connection() as db:
                text = json.dumps([dict(row) for row in db.execute("SELECT * FROM harness_audit")])
                final = json.loads(db.execute("SELECT metadata FROM harness_audit ORDER BY id DESC LIMIT 1").fetchone()["metadata"])
            assert final["model_http_status"] == 200 and final["model_failure"] == "empty_content"
            assert "PRIVATE MODEL BODY" not in text and "PRIVATE QUESTION" not in text
    asyncio.run(run())
