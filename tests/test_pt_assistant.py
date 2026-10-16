"""PT vertical slice mock tests: no patient data, real models, cloud or GPU."""

import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.pt import EvidencePlan, validate_plan, PTAnswer, generation_schema, PTEvidencePacket
from backend.app.harness.recovery import HarnessFailure
from backend.app.models.base import Message
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.config import ServiceSettings, load_services
from backend.app.tools.registry import ToolRegistry
from backend.app.tools.services import service_registry
from test_services import book_response


def paper_response():
    return {"query": "test", "limit": 1, "offset": 0, "paper_total": 1,
            "search_event_id": None, "journey_id": None, "papers": [{
                "title": "Synthetic paper", "abstract_snippet": "original paper evidence, not AI summary",
                "source_url": "https://example.org/paper/123", "journal": "Synthetic journal", "publication_year": 2024,
                "analysis": {"takeaway": "DO NOT USE AI SUMMARY"}, "localization": {"title": "DO NOT USE TRANSLATION"}}]}


def final(mode, ids=("E1", "E2")):
    keys = ("explanation", "clinical_meaning", "example", "misconceptions", "cautions") if mode == "pt_learning" else (
        "problem_summary", "hypotheses", "additional_assessment", "intervention_candidates", "missing_information")
    return {"source_claims": [{"evidence_id": id} for id in ids], "sections": {key: "仮説・要確認です。" for key in keys},
            "uncertainty": "原典と個別患者の安全評価が必要です。"}


class Model:
    def __init__(self, mode, response=None):
        self.inputs = []
        self.response = final(mode) if response is None else response

    async def generate_structured(self, messages, schema):
        self.inputs.append((messages, schema))
        return json.dumps({"query": "test"} if len(self.inputs) == 1 else self.response)


def host(tmp_path, client, model):
    store = HistoryStore(tmp_path / "private.db"); store.initialize()
    services = (ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),
                ServiceSettings("ptotsearch.search", api_base_url="https://example.org", enabled=True, public_retrieval=True))
    registry = service_registry(services, client, tmp_path)
    return Harness(model, ResourceCoordinator(), AuditTrail(store), registry=registry,
                   policy=TurnPolicy(allowed_tools=frozenset(s.tool for s in services)), evidence_limit=1, snippet_chars=160)


@pytest.mark.parametrize("mode", ["pt_learning", "clinical"])
def test_two_read_only_retrievals_sequential_original_grounding(tmp_path, mode):
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.path == "/search":
                return httpx.Response(200, json=book_response() | {"limit": 1})
            assert request.url.path == "/api/search" and request.headers["DNT"] == "1"
            assert request.url.params["domain"] == "evidence" and request.url.params["q"] == "test"
            return httpx.Response(200, json=paper_response())
        model = Model(mode)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            harness = host(tmp_path, client, model)
            result = await harness.run([Message("assistant", "PRIVATE HISTORY"), Message("user", "PRIVATE CASE")],
                pt_mode=mode, evidence_query="test", external_retrieval=True)
            answer = PTAnswer.model_validate(result.pt_answer)
            assert [r.url.path for r in calls] == ["/search", "/api/search"]
            assert len(model.inputs) == 2 and len(answer.evidence.items) == 2
            assert [r.reference.source_type for r in answer.evidence.items] == ["book", "paper"]
            assert answer.source_claims[1].quote == "original paper evidence, not AI summary"
            assert "Synthetic paper" in result.content and "PDFページ: 3" in result.content
            assert "AIによる整理・推論" in result.content and not result.cloud_used and not result.renderer_used
            assert "PRIVATE HISTORY" not in model.inputs[1][0][-1].content
            assert all("PRIVATE CASE" not in str(r.url) for r in calls)
            assert "DO NOT USE" not in model.inputs[1][0][-1].content
            with harness.audit.store.connection() as db:
                audit = str([dict(r) for r in db.execute("SELECT * FROM harness_audit")])
            assert "PRIVATE CASE" not in audit and "original paper evidence" not in audit
    asyncio.run(run())


def test_without_external_consent_local_book_only(tmp_path):
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=book_response() | {"limit": 1}))) as client:
            result = await host(tmp_path, client, Model("clinical", final("clinical", ("E1",)))).run(
                [Message("user", "private case")], pt_mode="clinical")
            assert len(calls) == 1 and calls[0].url.path == "/search"
            assert "公開検索語" in result.content
    asyncio.run(run())


@pytest.mark.parametrize("query", [None, "patient@example.org", "患者名 山田", "12345678", "Bearer abc", "x"*121])
def test_external_query_gate_before_model_or_network(tmp_path, query):
    async def run():
        model = Model("clinical")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No network expected"))) as client:
            with pytest.raises(HarnessFailure):
                await host(tmp_path, client, model).run([Message("user", "private case")], pt_mode="clinical",
                                                      evidence_query=query, external_retrieval=True)
            assert not model.inputs
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [{"query": "test"}, {"query": "patient@example.org", "public_confirmed": True}])
def test_direct_ptot_cannot_bypass_privacy_gate(tmp_path, arguments):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as client:
            with pytest.raises(HarnessFailure):
                await host(tmp_path, client, None).run_tool(ToolCall(tool="ptotsearch.search", arguments=arguments))
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"query": "wrong"}, {"limit": 2}, {"offset": True}, {"paper_total": 0},
                                   {"papers": [{}]}, {"journey_id": 123}, {"offset": False}])
def test_bad_ptot_response_rejected(tmp_path, change):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=paper_response() | change))) as client:
            with pytest.raises(HarnessFailure):
                await host(tmp_path, client, None).run_tool(ToolCall(tool="ptotsearch.search", arguments={"query": "test", "public_confirmed": True}))
    asyncio.run(run())


@pytest.mark.parametrize("mutate", ["unknown_id", "quote", "metadata", "extra_tool"])
def test_model_cannot_invent_source_or_loop(tmp_path, mutate):
    response = final("pt_learning")
    if mutate == "unknown_id": response["source_claims"] = [{"evidence_id": "E9"}]
    elif mutate == "quote": response["source_claims"][0]["quote"] = "invented"
    elif mutate == "metadata": response["source_claims"][0]["title"] = "invented"
    else: response["tool_call"] = {"tool": "web.search"}
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200,
            json=(book_response() | {"limit": 1}) if r.url.path == "/search" else paper_response()))) as client:
            with pytest.raises(HarnessFailure):
                await host(tmp_path, client, Model("pt_learning", response)).run([Message("user", "question")],
                    pt_mode="pt_learning", evidence_query="test", external_retrieval=True)
            assert len(calls) == 2
    asyncio.run(run())


def test_plan_budget_and_registry_capabilities(tmp_path):
    call = ToolCall(tool="book.search", arguments={"query": "test", "limit": 1})
    with pytest.raises(ValidationError): EvidencePlan(steps=[call]*4)
    with pytest.raises(ValidationError): EvidencePlan(steps=[call, call])
    async def run():
        async with httpx.AsyncClient() as client:
            harness = host(tmp_path, client, None)
            definition = harness.registry.get("book.search")
            for changes in ({"read_only_evidence": False}, {"requires_managed_gpu": True}, {"remote_job": True}, {"min_artifacts": 1}):
                harness.registry = ToolRegistry((replace(definition, **changes),))
                with pytest.raises(HarnessFailure): validate_plan(harness, EvidencePlan(steps=[call]))
    asyncio.run(run())


def test_public_config_is_explicit_and_restricted(tmp_path):
    from unittest.mock import patch
    file = tmp_path / "services.toml"; file.touch()
    for tool, url, public in [("image.generate", "https://example.org", True),
                              ("ptotsearch.search", "http://example.org", True),
                              ("ptotsearch.search", "https://example.org", False)]:
        with patch("backend.app.tools.config.tomllib.load", return_value={"services": {tool: {"api_base_url": url, "public_retrieval": public}}}):
            with pytest.raises(ValueError): load_services(file)


def test_empty_evidence_generation_forbids_claims():
    assert generation_schema("clinical", PTEvidencePacket(items=[]))["properties"]["source_claims"]["maxItems"] == 0


@pytest.mark.parametrize("mode", ["pt_learning", "clinical"])
def test_pt_chat_api_and_success_only_private_history(settings, monkeypatch, mode):
    from backend.app.main import create_app
    calls = []
    original = httpx.AsyncClient
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=(book_response() | {"limit": 1}) if request.url.path == "/search" else paper_response())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    app = create_app(replace(settings, library_evidence_limit=1), adapter=Model(mode), services=(
        ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),
        ServiceSettings("ptotsearch.search", api_base_url="https://example.org", enabled=True, public_retrieval=True)))
    async def run():
        async with app.router.lifespan_context(app):
            async with original(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
                denied = await client.post("/api/chat", json={"message": "private case", "mode": mode, "external_retrieval": True})
                assert denied.status_code == 502 and not calls
                ok = await client.post("/api/chat", json={"message": "private case", "mode": mode,
                    "evidence_query": "test", "external_retrieval": True})
                assert ok.status_code == 200 and len(calls) == 2
                answer = PTAnswer.model_validate(ok.json()["pt_answer"])
                assert answer.mode == mode and ok.headers["X-Request-ID"]
                store = HistoryStore(settings.database)
                assert len(store.list_conversations()) == 1
                assert len(store.history(ok.json()["conversation_id"])) == 2
                invalid = await client.post("/api/chat", json={"message": "test", "mode": mode,
                    "external_retrieval": "yes", "evidence_query": "test"})
                assert invalid.status_code == 422
                wrong_mode = await client.post("/api/chat", json={"message": "test", "mode": "chat", "evidence_query": "test"})
                assert wrong_mode.status_code == 422
    asyncio.run(run())


@pytest.mark.parametrize("url", ["file:///secret", "http://example.org/a", "https://user:pass@example.org/a", "https://example.org/a?token=private"])
def test_ptot_references_are_data_not_untrusted_fetches(tmp_path, url):
    raw = paper_response(); raw["papers"][0]["source_url"] = url
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=raw))) as client:
            with pytest.raises(HarnessFailure):
                await host(tmp_path, client, None).run_tool(ToolCall(tool="ptotsearch.search", arguments={"query": "test", "public_confirmed": True}))
            assert len(calls) == 1 and calls[0].url.path == "/api/search"
    asyncio.run(run())


def test_empty_packet_cannot_claim_unknown_source(tmp_path):
    from backend.app.harness.pt import validate_pt_answer
    with pytest.raises(HarnessFailure):
        validate_pt_answer(json.dumps(final("clinical", ("E1",))), "clinical", PTEvidencePacket(items=[]),
                           EvidencePlan(steps=[ToolCall(tool="book.search", arguments={"query": "test"})]), [])


@pytest.mark.parametrize("denied", [False, True])
def test_live_probe_mock_never_replays_or_prints_evidence(monkeypatch, capsys, denied):
    from types import SimpleNamespace
    from backend.app.harness.pt import PTEvidence, validate_pt_answer
    from backend.app.tools.retrieval import EvidenceReference
    from scripts import test_pt_assistant
    calls = []
    original = httpx.Client
    reference = EvidenceReference(source_type="paper", title="Synthetic private title", citation="Synthetic citation",
        locator="https://example.org/paper/123", reference="https://example.org/paper/123",
        text="Synthetic private evidence", provenance="original_abstract_snippet")
    packet = PTEvidencePacket(items=[PTEvidence(evidence_id="E1", tool="ptotsearch.search", reference=reference)])
    plan = EvidencePlan(steps=[ToolCall(tool="book.search", arguments={"query": "test"}),
                               ToolCall(tool="ptotsearch.search", arguments={"query": "test", "public_confirmed": True})])
    answer = validate_pt_answer(json.dumps(final("pt_learning", ("E1",))), "pt_learning", packet, plan, [])
    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["external_retrieval"] is True and payload["evidence_query"] == "test"
        return httpx.Response(403 if denied else 200, json={"pt_answer": answer.model_dump(mode="json"),
            "reply": "AIによる整理・推論 citation:"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(test_pt_assistant, "load_server_settings", lambda _: SimpleNamespace(host="localhost", port=1234))
    monkeypatch.setattr("sys.argv", ["pt-probe", "--query", "test"])
    if denied:
        with pytest.raises(SystemExit): test_pt_assistant.main()
    else:
        test_pt_assistant.main()
        output = capsys.readouterr().out
        assert "SUCCESS" in output and "Synthetic private" not in output
    assert len(calls) == 1
