"""Harness unit/mock tests. No GPU, provider, process or external tool is called."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import Field, ValidationError

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import (
    Artifact, Contract, ErrorCode, FinalStatus, ModelProposal, RenderRequest, ToolResult,
)
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy, presentation_request, present
from backend.app.harness.recovery import HarnessFailure, RecoveryPolicy, RetryableExecutionError
from backend.app.models.base import Message
from backend.app.resources.gpu import ResourceCoordinator, ResourceMode
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.registry import ToolDefinition, ToolRegistry


class Args(Contract):
    prompt: str = Field(min_length=1)


class Paths(Contract):
    path: Path


class URLs(Contract):
    url: str


class Output(Contract):
    value: str


class MockTool:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or ToolResult(success=True, result_type="text", output={"value": "mock result"})
        self.error = error

    async def execute(self, arguments, backend):
        self.calls.append((arguments, backend))
        if self.error:
            raise self.error
        return self.result


class ProposalAdapter:
    def __init__(self, raw):
        self.raw = raw

    async def propose(self, messages):
        return self.raw


def definition(adapter, **kwargs):
    return replace(ToolDefinition(
        name="tts.generate", adapter=adapter, argument_schema=Args, output_schema=Output,
        result_type="text", mode=ResourceMode.TTS, enabled=True,
        allowed_backends=frozenset({"irodori"}), default_backend="irodori",
        requires_managed_gpu=False,  # Explicit unit/mock capability, not a real GPU integration.
    ), **kwargs)


def proposal(arguments=None, **kwargs):
    call = {"tool": "tts.generate", "arguments": arguments or {"prompt": "private argument"}, **kwargs}
    return {"kind": "tool", "tool_call": call}


def make_harness(tmp_path, raw, definitions=(), policy=None):
    store = HistoryStore(tmp_path / "history.db")
    store.initialize()
    return Harness(ProposalAdapter(raw), ResourceCoordinator(), AuditTrail(store),
                   registry=ToolRegistry(definitions),
                   policy=policy or TurnPolicy(allowed_tools=frozenset({"tts.generate"}), privacy="public"))


def execute(harness):
    return asyncio.run(harness.run([Message("user", "patient-private-request api-key-test")]))


def audit_rows(harness):
    with harness.audit.store.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM harness_audit ORDER BY id")]


def test_valid_tool_runs_only_through_harness(tmp_path):
    adapter = MockTool()
    harness = make_harness(tmp_path, json.dumps(proposal()), (definition(adapter),))
    result = execute(harness)
    assert result.status == FinalStatus.SUCCESS
    assert result.tool_result.output == {"value": "mock result"}
    assert len(adapter.calls) == 1
    assert isinstance(adapter.calls[0][0], Args)
    assert adapter.calls[0][1] == "irodori"
    assert harness.resource_state().active_job is None
    events = [row["event"] for row in audit_rows(harness)]
    assert events.index("contract_validated") < events.index("policy_validated")
    assert events.index("policy_validated") < events.index("result_validated")
    assert events[-1] == "final"


@pytest.mark.parametrize("raw", [
    "not json", '{"kind":"tool"',
    {"kind": "tool", "tool_call": {"tool": "tts.generate", "arguments": {}, "endpoint": "http://bad.test"}},
    {"kind": "tool", "tool_call": {"tool": "tts.generate"}, "retry": "forever"},
    {"kind": "answer", "content": " ", "tool_call": None},
    {"kind": "tool", "tool_calls": [{}, {}]},
    {"kind": "answer", "content": "answer", "tool_call": {"tool": "tts.generate"}},
    ModelProposal.model_construct(kind="tool", tool_call=None),
])
def test_bad_proposals_never_execute(tmp_path, raw):
    adapter = MockTool()
    harness = make_harness(tmp_path, raw, (definition(adapter),))
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.code == ErrorCode.SCHEMA_INVALID
    assert adapter.calls == []
    assert harness.resource_state().active_job is None


@pytest.mark.parametrize("change,call,policy", [
    ({"enabled": False}, {}, None),
    ({}, {"backend": "unapproved"}, None),
    ({}, {"tool": "missing"}, None),
    ({"cloud": True}, {}, None),
    ({}, {}, TurnPolicy()),
    ({}, {}, TurnPolicy(allowed_tools=frozenset({"tts.generate"}), privacy="private")),
])
def test_policy_rejection_never_executes(tmp_path, change, call, policy):
    adapter = MockTool()
    harness = make_harness(tmp_path, proposal(**call), (definition(adapter, **change),), policy)
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.code == ErrorCode.POLICY_DENIED
    assert adapter.calls == []


@pytest.mark.parametrize("arguments", [
    {}, {"prompt": 123}, {"prompt": ""},
    {"prompt": "text", "command": "shutdown"},
    {"prompt": "text", "endpoint": "http://bad.test"},
])
def test_argument_contracts_reject_extra_or_wrong_types(tmp_path, arguments):
    adapter = MockTool()
    raw = {"kind": "tool", "tool_call": {"tool": "tts.generate", "arguments": arguments}}
    harness = make_harness(tmp_path, raw, (definition(adapter),))
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.code == ErrorCode.SCHEMA_INVALID
    assert adapter.calls == []


def test_filesystem_allowlist_and_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "escape").symlink_to(tmp_path, target_is_directory=True)
    adapter = MockTool()
    spec = definition(adapter, argument_schema=Paths, allowed_roots=(allowed,))
    for path in [tmp_path / "outside", allowed / "escape/outside", Path("relative")]:
        harness = make_harness(tmp_path, proposal({"path": str(path)}), (spec,))
        with pytest.raises(HarnessFailure):
            execute(harness)
    assert adapter.calls == []
    harness = make_harness(tmp_path, proposal({"path": str(allowed / "output.wav")}), (spec,))
    assert execute(harness).status == FinalStatus.SUCCESS


def test_url_allowlist(tmp_path):
    adapter = MockTool()
    spec = definition(adapter, argument_schema=URLs, allowed_urls=frozenset({"https://allowed.test/input"}))
    for url in ["http://bad.test", "https://allowed.test/other", "https://user:secret@allowed.test/input"]:
        with pytest.raises(HarnessFailure):
            execute(make_harness(tmp_path, proposal({"url": url}), (spec,)))
    assert adapter.calls == []
    execute(make_harness(tmp_path, proposal({"url": "https://allowed.test/input"}), (spec,)))
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("result,changes", [
    ({"success": True, "result_type": "text", "output": {"value": 12}}, {}),
    ({"success": True, "result_type": "text", "output": {"value": "ok"}, "unexpected": "secret"}, {}),
    (ToolResult(success=False, result_type="text", output={"value": "ok"}), {}),
    (ToolResult(success=True, result_type="text", output={"value": "ok"}, exit_status=1), {}),
    (ToolResult(success=True, result_type="text", output={"value": "ok"}, http_status=500), {}),
    (ToolResult(success=True, result_type="text", output={"value": "ok"}), {"requires_exit_status": True}),
    (ToolResult(success=True, result_type="text", output={"value": "ok"}), {"requires_http_status": True}),
    (ToolResult(success=True, result_type="audio", output={"value": "ok"}), {"result_type": "audio"}),
    (ToolResult(success=True, result_type="image", output={"value": "ok"}), {"result_type": "image"}),
    (ToolResult(success=True, result_type="run", output={"value": "ok"}, run_status="completed"), {"result_type": "run"}),
    (ToolResult(success=True, result_type="run", output={"value": "ok"}, run_id="run1", run_status="running"), {"result_type": "run"}),
])
def test_success_flag_is_not_sufficient(tmp_path, result, changes):
    adapter = MockTool(result=result)
    harness = make_harness(tmp_path, proposal(), (definition(adapter, **changes),))
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.code == ErrorCode.RESULT_INVALID
    assert len(adapter.calls) == 1
    assert harness.resource_state().active_job is None


def test_audio_artifact_exists_nonempty_and_expected_type(tmp_path):
    path = tmp_path / "audio.wav"
    result = ToolResult(success=True, result_type="audio", output={"value": "ok"},
                        artifacts=(Artifact(path=str(path), kind="audio"),))
    adapter = MockTool(result=result)
    spec = definition(adapter, result_type="audio", allowed_roots=(tmp_path,))
    for data in [None, b"", b"not audio"]:
        if data is not None:
            path.write_bytes(data)
        with pytest.raises(HarnessFailure):
            execute(make_harness(tmp_path, proposal(), (spec,)))
    # Basic signature fixture, not an IRODORI integration or full audio decode test.
    path.write_bytes(b"RIFF\x10\x00\x00\x00WAVEfmt mock")
    assert execute(make_harness(tmp_path, proposal(), (spec,))).status == FinalStatus.SUCCESS


@pytest.mark.parametrize("kind,header", [
    ("image", b"\x89PNG\r\n\x1a\nmock-image"),
    ("image", b"\xff\xd8\xffmock-jpeg"),
])
def test_image_artifact_signature(tmp_path, kind, header):
    path = tmp_path / "artifact"
    path.write_bytes(header)
    result = ToolResult(success=True, result_type=kind, output={"value": "ok"},
                        artifacts=(Artifact(path=str(path), kind=kind),))
    adapter = MockTool(result=result)
    assert execute(make_harness(tmp_path, proposal(),
                   (definition(adapter, result_type=kind, allowed_roots=(tmp_path,)),))).status == FinalStatus.SUCCESS


@pytest.mark.parametrize("idempotent,retries,expected", [(True, 2, 3), (False, 2, 1), (True, 0, 1)])
def test_retry_is_host_bounded(tmp_path, idempotent, retries, expected):
    adapter = MockTool(error=RetryableExecutionError("private error text"))
    spec = definition(adapter, recovery=RecoveryPolicy(max_retries=retries, idempotent=idempotent))
    harness = make_harness(tmp_path, proposal(), (spec,))
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.code == ErrorCode.TOOL_FAILED
    assert len(adapter.calls) == expected
    assert len([row for row in audit_rows(harness) if row["event"] == "retry"]) == expected - 1
    assert "private error" not in str(audit_rows(harness))


def test_nonretryable_failure_is_not_repeated(tmp_path):
    adapter = MockTool(error=ValueError("secret"))
    harness = make_harness(tmp_path, proposal(),
                           (definition(adapter, recovery=RecoveryPolicy(max_retries=2, idempotent=True)),))
    with pytest.raises(HarnessFailure):
        execute(harness)
    assert len(adapter.calls) == 1


def test_timeout_is_stalled_and_releases_resource(tmp_path):
    class SlowTool(MockTool):
        async def execute(self, arguments, backend):
            self.calls.append((arguments, backend))
            await asyncio.Event().wait()

    adapter = SlowTool()
    spec = definition(adapter, recovery=RecoveryPolicy(timeout_seconds=0.01, max_retries=2, idempotent=True))
    harness = make_harness(tmp_path, proposal(), (spec,))
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.status == FinalStatus.STALLED
    assert len(adapter.calls) == 1
    assert harness.resource_state().active_job is None
    assert '"status":"STALLED"' in audit_rows(harness)[-1]["metadata"]


def test_tool_reported_timeout_is_stalled(tmp_path):
    adapter = MockTool(result=ToolResult(success=True, result_type="text", output={"value": "ok"}, timed_out=True))
    with pytest.raises(HarnessFailure) as error:
        execute(make_harness(tmp_path, proposal(), (definition(adapter),)))
    assert error.value.status == FinalStatus.STALLED


@pytest.mark.parametrize("format_name", ["json", "yaml", "code", "command", "api_payload", "tool_args", "config", "structured", "strict_numbers"])
@pytest.mark.parametrize("renderer", ["luna", "llmjp"])
def test_machine_formats_cannot_render(format_name, renderer):
    with pytest.raises(ValidationError):
        RenderRequest(path="presentation", format=format_name, content="secret", renderer=renderer)


def test_machine_path_and_unconnected_renderer_are_fail_closed():
    with pytest.raises(ValidationError):
        RenderRequest(path="machine", format="natural_language", content="machine", renderer="luna")
    with pytest.raises(HarnessFailure):
        present(RenderRequest(path="presentation", format="natural_language", content="hello", renderer="luna"))


@pytest.mark.parametrize("content,expected", [
    ('{"number": 12}', "json"), ("```python\nprint(1)\n```", "code"),
    ("setting: value", "yaml"), ("curl http://host.test", "command"), ("こんにちは", "natural_language"),
])
def test_host_separates_machine_and_human_output(content, expected):
    request = presentation_request(content, TurnPolicy())
    assert request.format == expected
    assert request.path == ("presentation" if expected == "natural_language" else "machine")
    assert present(request) == content


def test_legacy_json_response_never_executes_tool(tmp_path):
    adapter = MockTool()
    raw = json.dumps(proposal())
    harness = make_harness(tmp_path, None, (definition(adapter),))

    class Legacy:
        async def generate(self, messages):
            return raw

    harness.adapter = Legacy()
    assert execute(harness).content == raw
    assert adapter.calls == []


def test_one_tool_result_cannot_trigger_a_second_tool(tmp_path):
    adapter = MockTool(result=ToolResult(success=True, result_type="text", output={"value": json.dumps(proposal())}))
    result = execute(make_harness(tmp_path, proposal(), (definition(adapter),)))
    assert result.tool_result.output["value"] == json.dumps(proposal())
    assert len(adapter.calls) == 1


def test_audit_persists_only_metadata(tmp_path):
    adapter = MockTool(result=ToolResult(success=True, result_type="text", output={"value": "secret response"}))
    harness = make_harness(tmp_path, proposal(), (definition(adapter),))
    result = execute(harness)
    rows = audit_rows(harness)
    assert {row["request_id"] for row in rows} == {str(result.request_id)}
    assert not any(private in str(rows) for private in ["patient-private-request", "api-key-test", "private argument", "secret response"])
    assert all(json.loads(row["metadata"])["cloud_used"] is False for row in rows)
    assert all(json.loads(row["metadata"])["renderer_used"] is False for row in rows)


def test_registry_requires_closed_schema_and_unique_names():
    from pydantic import BaseModel

    class OpenArgs(BaseModel):
        prompt: str

    spec = definition(MockTool())
    with pytest.raises(ValueError):
        replace(spec, argument_schema=OpenArgs)
    with pytest.raises(ValueError):
        ToolRegistry((spec, spec))


def test_resource_lock_is_shared_by_harness_tools(tmp_path):
    async def run():
        active = 0
        peak = 0

        class ConcurrentTool(MockTool):
            async def execute(self, arguments, backend):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                assert resources.snapshot()["mode"] == "TTS"
                try:
                    await asyncio.sleep(0.01)
                    return self.result
                finally:
                    active -= 1

        resources = ResourceCoordinator()
        adapter = ConcurrentTool()
        first = make_harness(tmp_path, proposal(), (definition(adapter),))
        second = make_harness(tmp_path, proposal(), (definition(adapter),))
        first.resources = second.resources = resources
        results = await asyncio.gather(*[harness.run([Message("user", "hello")]) for harness in [first, second]])
        assert all(result.status == FinalStatus.SUCCESS for result in results)
        assert peak == 1
        assert resources.snapshot()["active_job"] is None

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_unverified_real_gpu_tool_is_blocked(tmp_path):
    adapter = MockTool()
    spec = definition(adapter, requires_managed_gpu=True)
    with pytest.raises(HarnessFailure) as error:
        execute(make_harness(tmp_path, proposal(), (spec,)))
    assert error.value.code == ErrorCode.RESOURCE_UNAVAILABLE
    assert adapter.calls == []


def test_active_job_conflict_prevents_tool_dispatch(tmp_path):
    from backend.app.harness.validator import validate_resource

    async def run():
        adapter = MockTool()
        harness = make_harness(tmp_path, proposal(), (definition(adapter),))
        async with harness.resources.job(ResourceMode.IMAGE):
            with pytest.raises(HarnessFailure) as error:
                validate_resource(harness.resource_state(), definition(adapter))
            assert error.value.code == ErrorCode.RESOURCE_BUSY
        assert adapter.calls == []

    asyncio.run(run())


def test_retry_can_succeed_within_limit(tmp_path):
    class OnceTransient(MockTool):
        async def execute(self, arguments, backend):
            self.calls.append((arguments, backend))
            if len(self.calls) == 1:
                raise RetryableExecutionError("temporary")
            return self.result

    adapter = OnceTransient()
    spec = definition(adapter, recovery=RecoveryPolicy(max_retries=1, idempotent=True))
    assert execute(make_harness(tmp_path, proposal(), (spec,))).status == FinalStatus.SUCCESS
    assert len(adapter.calls) == 2


def test_model_failure_is_host_classified_and_not_a_cloud_switch(tmp_path):
    from backend.app.models.base import ModelNotConfigured, ModelTimeout

    harness = make_harness(tmp_path, None)

    class Unavailable:
        async def generate(self, messages):
            raise ModelTimeout("secret endpoint")

    harness.adapter = Unavailable()
    with pytest.raises(HarnessFailure) as error:
        execute(harness)
    assert error.value.status == FinalStatus.STALLED
    assert "secret endpoint" not in str(audit_rows(harness))
    assert harness.resource_state().active_job is None

    class Missing:
        async def generate(self, messages):
            raise ModelNotConfigured("secret config detail")

    harness.adapter = Missing()
    with pytest.raises(ModelNotConfigured) as missing:
        execute(harness)
    assert "secret config" not in str(missing.value)
    assert "secret config" not in str(audit_rows(harness))


def test_unknown_tool_name_is_not_copied_to_audit(tmp_path):
    harness = make_harness(tmp_path, proposal(tool="patient-private-id"))
    with pytest.raises(HarnessFailure):
        execute(harness)
    assert "patient-private-id" not in str(audit_rows(harness))


def test_stalled_adapter_cannot_force_unlock_or_accept_late_result(tmp_path):
    async def run():
        release = asyncio.Event()

        class StubbornTool(MockTool):
            async def execute(self, arguments, backend):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Deliberately emulate a noncooperative adapter; no real process.
                    await release.wait()
                return self.result

        adapter = StubbornTool()
        spec = definition(adapter, recovery=RecoveryPolicy(timeout_seconds=0.01))
        harness = make_harness(tmp_path, proposal(), (spec,))
        try:
            with pytest.raises(HarnessFailure) as error:
                await harness.run([Message("user", "hello")])
            assert error.value.status == FinalStatus.STALLED
            assert harness.resource_state().active_job is not None
        finally:
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
                if harness.resource_state().active_job is None:
                    break
        assert harness.resource_state().active_job is None
        rows = audit_rows(harness)
        assert not any(row["event"] == "result_validated" for row in rows)
        assert '"status":"STALLED"' in rows[-1]["metadata"]

    asyncio.run(asyncio.wait_for(run(), timeout=5))
