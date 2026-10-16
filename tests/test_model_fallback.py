"""HOST fallback and cloud refusal mocks; no OpenAI or real CUDA is called."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import json

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.cloud import CloudPolicy, CloudPrivacyGate, payload_digest
from backend.app.harness.orchestrator import Harness
from backend.app.harness.recovery import HarnessFailure
from backend.app.models.base import Message, ModelError
from backend.app.models.registry import ModelRegistry
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.resources.manager import GPUManager, GPUReading, GPUUnsafe
from backend.app.resources.ownership import ExternalIdentity, GPUProcess, Ownership, OwnershipPolicy, ExternalActivity
from backend.app.storage.sqlite import HistoryStore
from test_gpu_manager import Driver, Inspector


BLOCK = GPUProcess(99, 100, "C", 700, "python", "0::/image.service")
RULE = ExternalIdentity("external-image", 99, 700, "python", "0::/image.service", Ownership.EXTERNAL_BLOCKING, 12288)


class Adapter:
    def __init__(self, content="local answer", fail=False):
        self.calls = 0
        self.content, self.fail = content, fail
    async def generate(self, messages):
        self.calls += 1
        if self.fail: raise ModelError("synthetic private upstream error")
        return self.content


class Provider(Adapter):
    purpose = "reasoning_fallback"


def setup(tmp_path, settings, client, *, blocked=True, fail_cpu=False):
    store = HistoryStore(tmp_path / "private.db");store.initialize()
    inspector = Inspector()
    if blocked: inspector.pids.add(99)
    async def read():
        processes = tuple(BLOCK if pid == 99 else GPUProcess(pid, 1000, "C") for pid in inspector.pids)
        return GPUReading(sum(p.used_mib for p in processes), frozenset(inspector.pids), 12288, processes)
    inspector.read = read
    gpu = Driver(inspector)
    gpu.profile = SimpleNamespace(device="gpu", required_vram_mib=8000)
    cpu = Driver(inspector, 11)
    cpu.profile = SimpleNamespace(device="cpu")
    async def start_cpu():
        cpu.started = True;cpu.events.append("start")
    cpu.start = start_cpu
    manager = GPUManager(store, inspector, {"qwen80b": gpu, "qwen": cpu},
        ownership_policy=OwnershipPolicy((RULE,)), release_timeout=.02)
    manager.open()
    adapters = {"qwen80b": Adapter("80B answer"), "qwen": Adapter(fail=fail_cpu)}
    models = ModelRegistry(settings, client, adapters, frozenset({"qwen80b", "qwen"}))
    harness = Harness(None, ResourceCoordinator(manager), AuditTrail(store), models=models)
    return harness, manager, adapters, store


def test_external_blocking_falls_back_without_gpu_start(tmp_path, settings):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, adapters, store = setup(tmp_path, settings, client)
            result = await harness.run([Message("user", "synthetic question")], model_id="qwen80b")
            assert result.content == "local answer" and not result.cloud_used
            selection = result.model_selection
            assert selection.requested_model == "qwen80b" and selection.actual_model_used == "qwen"
            assert selection.fallback_reason == "external_gpu_workload"
            assert selection.gpu_blocking_owners == ("external-image",) and selection.models_used == ("qwen",)
            assert adapters["qwen80b"].calls == 0 and adapters["qwen"].calls == 1
            assert manager.drivers["qwen80b"].events == []
            assert manager.drivers["qwen"].events == ["start", "stop"]
            assert manager.phase.value == "STOPPED" and manager.reading.pids == {99}
            with store.connection() as db:
                audit = str([dict(row) for row in db.execute("SELECT * FROM harness_audit")])
            assert "synthetic question" not in audit and "image.service" not in audit and "python" not in audit
            assert "model_fallback" in audit
            await manager.close()
    asyncio.run(run())


def test_available_gpu_keeps_80b_priority(tmp_path, settings):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, adapters, _ = setup(tmp_path, settings, client, blocked=False)
            result = await harness.run([Message("user", "question")], model_id="qwen80b")
            assert result.content == "80B answer" and result.model_selection.actual_model_used == "qwen80b"
            assert result.model_selection.fallback_reason is None and adapters["qwen"].calls == 0
            assert manager.phase.value == "READY"
            await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unknown", "telemetry", "recovery", "wrong_cpu", "start_failure"])
def test_other_failures_do_not_bypass_gpu_safety(tmp_path, settings, failure):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, adapters, _ = setup(tmp_path, settings, client, blocked=failure != "start_failure")
            if failure == "unknown": manager.ownership_policy = OwnershipPolicy()
            elif failure == "telemetry":
                async def read(): raise GPUUnsafe("telemetry_unavailable")
                manager.inspector.read = read
            elif failure == "recovery": manager._state(type(manager.phase).ERROR, error="recovery_required")
            elif failure == "wrong_cpu": manager.drivers["qwen"].profile = SimpleNamespace(device="gpu")
            else: manager.drivers["qwen80b"].fail_start = True
            with pytest.raises(HarnessFailure): await harness.run([Message("user", "question")], model_id="qwen80b")
            assert adapters["qwen"].calls == 0
            await manager.close()
    asyncio.run(run())


def test_lease_race_falls_back_only_before_child_creation(tmp_path, settings):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, adapters, _ = setup(tmp_path, settings, client, blocked=False)
            original = manager.preflight
            calls = 0
            async def preflight(owner):
                nonlocal calls
                calls += 1
                if calls == 2: manager.inspector.pids.add(99)
                await original(owner)
            manager.preflight = preflight
            result = await harness.run([Message("user", "question")], model_id="qwen80b")
            assert result.model_selection.actual_model_used == "qwen" and adapters["qwen80b"].calls == 0
            await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["chat", "library", "pt_learning", "clinical"])
def test_cloud_default_denied_including_exact_public_text(mode):
    assert not CloudPrivacyGate().authorize([Message("user", "public science")], mode=mode, purpose="reasoning_fallback").allowed


@pytest.mark.parametrize("mode", ["library", "pt_learning", "clinical"])
def test_pt_and_evidence_never_cloud_even_when_reviewed(mode):
    messages = [Message("user", "public science")]
    gate = CloudPrivacyGate(CloudPolicy(True, frozenset({"reasoning_fallback"}), frozenset({payload_digest(messages)})))
    assert gate.authorize(messages, mode=mode, purpose="reasoning_fallback").reason == "local_only_mode"


@pytest.mark.parametrize("text", ["患者A", "API key synthetic", "someone@example.org", "PRIVATE KEY"])
def test_review_does_not_override_private_markers(text):
    messages = [Message("user", text)]
    gate = CloudPrivacyGate(CloudPolicy(True, frozenset({"reasoning_fallback"}), frozenset({payload_digest(messages)})))
    assert not gate.authorize(messages, mode="chat", purpose="reasoning_fallback").allowed


def test_entire_history_and_purpose_are_bound_to_review():
    messages = [Message("user", "public science")]
    gate = CloudPrivacyGate(CloudPolicy(True, frozenset({"reasoning_fallback"}), frozenset({payload_digest(messages)})))
    assert gate.authorize(messages, mode="chat", purpose="reasoning_fallback").allowed
    assert not gate.authorize([Message("assistant", "other history")] + messages, mode="chat", purpose="reasoning_fallback").allowed
    assert not gate.authorize(messages, mode="chat", purpose="japanese_renderer").allowed


@pytest.mark.parametrize("approved", [False, True])
def test_cpu_failure_cloud_candidate_must_pass_gate(tmp_path, settings, approved):
    async def run():
        messages = [Message("user", "public science")]
        async with httpx.AsyncClient() as client:
            harness, manager, _, store = setup(tmp_path, settings, client, fail_cpu=True)
            provider = Provider("synthetic cloud answer")
            harness.reasoning_provider = provider
            if approved:
                harness.cloud_gate = CloudPrivacyGate(CloudPolicy(True, frozenset({"reasoning_fallback"}), frozenset({payload_digest(messages)})))
                result = await harness.run(messages, model_id="qwen80b")
                assert result.cloud_used and result.model_selection.actual_model_used == "openai_reasoning_fallback"
                assert provider.calls == 1
            else:
                with pytest.raises(ModelError): await harness.run(messages, model_id="qwen80b")
                assert provider.calls == 0
            with store.connection() as db:
                audit = str([dict(row) for row in db.execute("SELECT * FROM harness_audit")])
            assert "public science" not in audit and "synthetic private upstream error" not in audit
            await manager.close()
    asyncio.run(run())


def test_idle_activity_does_not_grant_coexistence():
    policy = OwnershipPolicy((replace(RULE, activity=ExternalActivity.IDLE_REPORTED),))
    assert policy.external_reason(GPUReading(100, frozenset({99}), 12288, (BLOCK,)), frozenset()) == "external_gpu_workload"


@pytest.mark.parametrize("mode", ["pt_learning", "clinical"])
@pytest.mark.parametrize("final_failure", [False, True])
def test_pt_fallback_keeps_evidence_no_replay_or_cloud(tmp_path, settings, mode, final_failure):
    from test_pt_assistant import Model, paper_response
    from test_services import book_response
    from backend.app.tools.config import ServiceSettings
    from backend.app.tools.services import service_registry
    from backend.app.harness.policy import TurnPolicy
    from backend.app.harness.pt import PTAnswer
    async def run():
        calls = []
        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=book_response() | {"limit": 1} if request.url.path == "/search" else paper_response())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            harness, manager, adapters, _ = setup(tmp_path, settings, client)
            model = Model(mode)
            original = model.generate_structured
            async def generate(messages, schema):
                if final_failure and len(model.inputs) == 1: raise ModelError("synthetic synthesis failure")
                return await original(messages, schema)
            model.generate_structured = generate
            harness.models = ModelRegistry(settings, client, {"qwen80b": adapters["qwen80b"], "qwen": model})
            services = (ServiceSettings("book.search", api_base_url="http://localhost", enabled=True),
                ServiceSettings("ptotsearch.search", api_base_url="https://example.org", enabled=True, public_retrieval=True))
            harness.registry = service_registry(services, client, tmp_path)
            harness.policy = TurnPolicy(allowed_tools=frozenset(s.tool for s in services))
            harness.evidence_limit = 1
            provider = Provider("must not send")
            harness.reasoning_provider = provider
            if final_failure:
                with pytest.raises(ModelError): await harness.run([Message("user", "PRIVATE CASE")], model_id="qwen80b",
                    pt_mode=mode, evidence_query="test", external_retrieval=True)
            else:
                result = await harness.run([Message("user", "PRIVATE CASE")], model_id="qwen80b",
                    pt_mode=mode, evidence_query="test", external_retrieval=True)
                assert result.model_selection.actual_model_used == "qwen" and not result.cloud_used
                answer = PTAnswer.model_validate(result.pt_answer)
                assert len(answer.source_claims) == 2 and len(answer.evidence.items) == 2
            assert calls == ["/search", "/api/search"] and provider.calls == 0 and adapters["qwen80b"].calls == 0
            assert manager.drivers["qwen80b"].events == []
            await manager.close()
    asyncio.run(run())


def test_unknown_precedence_over_known_blocking_is_deterministic():
    unknown = replace(BLOCK, pid=100)
    policy = OwnershipPolicy((RULE,))
    value = GPUReading(200, frozenset({99,100}), 12288, (BLOCK,unknown))
    assert policy.external_reason(value, frozenset()) == "unknown_gpu_process"


def test_cpu_success_never_considers_cloud(tmp_path, settings):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, _, _ = setup(tmp_path, replace(settings, default_model="qwen80b"), client)
            class Gate:
                def authorize(self, *args, **kwargs): pytest.fail("CPU success must not consider cloud")
            harness.cloud_gate = Gate()
            result = await harness.run([Message("user", "PRIVATE CASE")])
            assert result.model_selection.requested_model == "qwen80b" and result.model_selection.actual_model_used == "qwen"
            await manager.close()
    asyncio.run(run())


def test_fallback_endpoint_policy_is_not_bypassed(tmp_path, settings):
    async def run():
        async with httpx.AsyncClient() as client:
            harness, manager, adapters, _ = setup(tmp_path, settings, client)
            resolve = harness.models.resolve
            def mismatched(model_id):
                candidate = resolve(model_id)
                return replace(candidate, injected=False, settings=replace(candidate.settings, endpoint="https://example.org/chat")) if model_id == "qwen" else candidate
            harness.models.resolve = mismatched
            manager.drivers["qwen"].profile = SimpleNamespace(device="cpu", health_url="http://localhost/health")
            with pytest.raises(HarnessFailure): await harness.run([Message("user", "private case")], model_id="qwen80b")
            assert adapters["qwen"].calls == 0 and not manager.drivers["qwen"].events
            await manager.close()
    asyncio.run(run())
