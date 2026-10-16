"""All network calls use MockTransport; no API key or real cloud request required."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.app.harness.cloud import CloudPolicy, CloudPrivacyGate, payload_digest
from backend.app.harness.contracts import RenderRequest
from backend.app.harness.policy import presentation_request, TurnPolicy
from backend.app.harness.render import JapaneseRenderer, render_messages, render_pair_digest
from backend.app.models.base import Message, ModelError
from backend.app.providers.config import CloudSettings, ProviderSettings, load_cloud
from backend.app.providers.openai import OpenAIProvider


def settings(**values):
    return ProviderSettings(enabled=True, endpoint="https://api.openai.com/v1/responses", model="synthetic",
                            api_key_env="SYNTHETIC_CLOUD_KEY", **values)


def gate(messages, purpose="reasoning_fallback", pairs=()):
    return CloudPrivacyGate(CloudPolicy(True, frozenset({purpose}), frozenset({payload_digest(messages)}), frozenset(pairs)))


def response(text="こんにちは。", **values):
    return {"status": "completed", "output": [{"type": "message", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": text, "annotations": []}]}], **values}


def test_defaults_disabled_and_purposes_independent():
    c = load_cloud()
    assert not c.enabled and not c.reasoning_fallback.enabled and not c.japanese_renderer.enabled
    assert c.reasoning_fallback.api_key_env != c.japanese_renderer.api_key_env


@pytest.mark.parametrize("values", [
    {"endpoint": "http://api.openai.com/v1/responses"}, {"endpoint": "https://example.org/v1/responses"},
    {"endpoint": "https://api.openai.com/v1/responses?key=synthetic"}, {"api_key": "synthetic"},
    {"timeout_seconds": 0}, {"max_output_tokens": 100000}, {"enabled": "true"},
])
def test_invalid_provider_config(values):
    with pytest.raises((ValueError, ValidationError)):
        ProviderSettings.model_validate(values)


def test_renderer_must_not_reason():
    with pytest.raises(ValidationError):
        CloudSettings(japanese_renderer=ProviderSettings(reasoning_effort="high"))


@pytest.mark.parametrize("purpose", ["reasoning_fallback", "japanese_renderer"])
def test_gate_bound_actual_wire_schema(monkeypatch, purpose):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = render_messages("こんにちは。") if purpose == "japanese_renderer" else [Message("user", "こんにちは。")]
    def handle(r):
        body = json.loads(r.content)
        assert body["input"] == [{"role": m.role, "content": m.content} for m in messages]
        assert body["store"] is False and body["background"] is False
        assert body["tools"] == [] and body["tool_choice"] == "none" and not body["parallel_tool_calls"]
        assert body["truncation"] == "disabled" and body["max_output_tokens"] == 2048
        return httpx.Response(200, json=response())
    provider = OpenAIProvider(settings(), gate(messages, purpose), purpose, transport=httpx.MockTransport(handle))
    assert asyncio.run(provider.generate(messages)) == "こんにちは。"


@pytest.mark.parametrize("mode", ["library", "pt_learning", "clinical"])
@pytest.mark.parametrize("purpose", ["reasoning_fallback", "japanese_renderer"])
def test_local_only_even_exactly_reviewed(mode, purpose):
    messages = [Message("user", "公開一般語")]
    assert not gate(messages, purpose).authorize(messages, mode=mode, purpose=purpose).allowed


@pytest.mark.parametrize("content", ["患者", "api_key synthetic", "氏名 テスト", "person@example.org", "secret", "credential"])
def test_review_does_not_override_sensitive_content(content):
    messages = [Message("user", content)]
    assert not gate(messages).authorize(messages, mode="chat", purpose="reasoning_fallback").allowed


def test_direct_transport_cannot_bypass_gate(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    provider = OpenAIProvider(settings(), CloudPrivacyGate(), "reasoning_fallback",
                              transport=httpx.MockTransport(lambda r: pytest.fail("No dispatch")))
    with pytest.raises(ModelError):
        asyncio.run(provider.generate([Message("user", "未承認")]))


def test_history_mutation_invalidates_approval(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = [Message("user", "公開一般語")]
    provider = OpenAIProvider(settings(), gate(messages), "reasoning_fallback",
                              transport=httpx.MockTransport(lambda r: pytest.fail("No dispatch")))
    with pytest.raises(ModelError):
        asyncio.run(provider.generate([Message("assistant", "未承認履歴"), *messages]))


@pytest.mark.parametrize("body", [
    response(status="incomplete"), response(error={"message": "private upstream body"}),
    response(incomplete_details={"reason": "max_output_tokens"}), response(""),
    {"status": "completed", "output": [{"type": "function_call", "name": "unsafe"}]},
    {"status": "completed", "output": [{"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": None},
])
def test_bad_cloud_results_fail_closed(monkeypatch, body):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = [Message("user", "公開一般語")]
    p = OpenAIProvider(settings(), gate(messages), "reasoning_fallback",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(ModelError) as error:
        asyncio.run(p.generate(messages))
    assert "private upstream" not in str(error.value)


@pytest.mark.parametrize("code", [302, 401, 429, 500])
def test_cloud_http_failure_has_no_retry(monkeypatch, code):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = [Message("user", "公開一般語")]
    calls = []
    def handle(r):
        calls.append(r)
        return httpx.Response(code, headers={"location": "https://example.org/"}, json={"secret": "upstream"})
    p = OpenAIProvider(settings(), gate(messages), "reasoning_fallback", transport=httpx.MockTransport(handle))
    with pytest.raises(ModelError):
        asyncio.run(p.generate(messages))
    assert len(calls) == 1


@pytest.mark.parametrize("content", [
    '{"key": 1}', "```python\nprint(1)\n```", "foo: bar", "$ curl example", "`command`",
    "説明と {\"key\":1} の混在", "説明\nexport VAR=value", "https://example.org", "[citation]",
])
def test_machine_mixed_content_never_reaches_renderer(content):
    class Provider:
        purpose = "japanese_renderer"
        async def generate(self, messages): pytest.fail("Machine path cannot render")
    r = JapaneseRenderer(Provider(), gate(render_messages(content), "japanese_renderer"))
    result = asyncio.run(r.render(presentation_request(content, TurnPolicy())))
    assert result[:3] == (content, False, False)


@pytest.mark.parametrize("approved,candidate", [(False, "自然な表現です。"), (True, "自然な表現です。"), (True, "数字を2に改変。")])
def test_renderer_unreviewed_or_numeric_mutation_returns_original(approved, candidate):
    source = "表現を自然にします。"
    class Provider:
        purpose = "japanese_renderer"
        async def generate(self, messages): return candidate
    pairs = [render_pair_digest(source, candidate)] if approved else []
    r = JapaneseRenderer(Provider(), gate(render_messages(source), "japanese_renderer", pairs))
    result = asyncio.run(r.render(RenderRequest(path="presentation", format="natural_language", content=source)))
    valid = approved and "2" not in candidate
    assert result[:3] == (candidate if valid else source, valid, True)


def test_renderer_failure_restores_source_and_tracks_cloud():
    source = "元の自然文です。"
    class Provider:
        purpose = "japanese_renderer"
        async def generate(self, messages): raise ModelError("safe")
    r = JapaneseRenderer(Provider(), gate(render_messages(source), "japanese_renderer"))
    assert asyncio.run(r.render(presentation_request(source, TurnPolicy())))[:3] == (source, False, True)


def test_real_renderer_transport_rejects_approved_machine_payload(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = render_messages('{"key": 1}')
    p = OpenAIProvider(settings(), gate(messages, "japanese_renderer"), "japanese_renderer",
                       transport=httpx.MockTransport(lambda r: pytest.fail("No dispatch")))
    with pytest.raises(ModelError):
        asyncio.run(p.generate(messages))


def test_harness_renderer_only_final_answer_and_audit_has_no_text(tmp_path):
    from backend.app.harness.audit import AuditTrail
    from backend.app.harness.orchestrator import Harness
    from backend.app.resources.gpu import ResourceCoordinator
    from backend.app.storage.sqlite import HistoryStore
    source, candidate = "原文の自然な回答です。", "自然な日本語の回答です。"
    class Local:
        async def generate(self, messages): return source
    class Cloud:
        purpose = "japanese_renderer"
        async def generate(self, messages):
            assert messages == render_messages(source)
            return candidate
    g = gate(render_messages(source), "japanese_renderer", [render_pair_digest(source, candidate)])
    store = HistoryStore(tmp_path / "chat.db")
    store.initialize()
    h = Harness(Local(), ResourceCoordinator(), AuditTrail(store), japanese_renderer=JapaneseRenderer(Cloud(), g))
    result = asyncio.run(h.run([Message("user", "質問")]))
    assert result.content == candidate and result.renderer_used and result.cloud_used
    with store.connection() as db:
        events = [r[0] for r in db.execute("SELECT metadata FROM harness_audit")]
    assert any('"cloud_purpose":"japanese_renderer"' in e for e in events)
    assert not any(source in e or candidate in e for e in events)


@pytest.mark.parametrize("format_name", ["json", "yaml", "code", "command", "api_payload", "tool_args", "config", "structured", "strict_numbers"])
def test_explicit_machine_contract_never_renders(format_name):
    class Provider:
        purpose = "japanese_renderer"
        async def generate(self, messages): pytest.fail("No renderer on a machine contract")
    r = JapaneseRenderer(Provider(), CloudPrivacyGate())
    request = RenderRequest(path="machine", format=format_name, content="普通の文章でも型がmachineなら禁止。")
    assert asyncio.run(r.render(request))[:3] == (request.content, False, False)


def test_renderer_cancel_records_cloud_attempt_before_return(tmp_path):
    from backend.app.harness.audit import AuditTrail
    from backend.app.harness.orchestrator import Harness
    from backend.app.resources.gpu import ResourceCoordinator
    from backend.app.storage.sqlite import HistoryStore
    source = "元の自然文です。"
    async def run():
        entered = asyncio.Event()
        class Local:
            async def generate(self, messages): return source
        class Cloud:
            purpose = "japanese_renderer"
            async def generate(self, messages):
                entered.set()
                await asyncio.Event().wait()
        store = HistoryStore(tmp_path / "chat.db")
        store.initialize()
        g = gate(render_messages(source), "japanese_renderer")
        h = Harness(Local(), ResourceCoordinator(), AuditTrail(store), japanese_renderer=JapaneseRenderer(Cloud(), g))
        task = asyncio.create_task(h.run([Message("user", "公開質問")]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        with store.connection() as db:
            final = json.loads(db.execute("SELECT metadata FROM harness_audit WHERE event='final'").fetchone()[0])
        assert final["cloud_used"] is True and not final["renderer_used"]
    asyncio.run(run())


def test_cloud_transport_has_bounded_deadline(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_CLOUD_KEY", "synthetic-test-value")
    messages = [Message("user", "公開一般語")]
    async def handle(r):
        await asyncio.Event().wait()
    p = OpenAIProvider(settings(timeout_seconds=.01), gate(messages), "reasoning_fallback", transport=httpx.MockTransport(handle))
    with pytest.raises(ModelError):
        asyncio.run(p.generate(messages))
