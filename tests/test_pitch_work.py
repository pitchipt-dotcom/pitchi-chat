"""External agent mock jobs; never execute the existing agent or touch its runs."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.context import current_job_id
from backend.app.jobs.store import JobStore
from backend.app.jobs.worker import JobWorker
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.tools.pitch_work import EVENT_PREFIX, WorkArguments, PitchWorkAdapter, work_definition
from backend.app.tools.registry import ToolRegistry
from backend.app.tools.work_config import WorkSettings, source_digest, load_work


def configure(tmp_path, **values):
    source = tmp_path / "external"
    package = source / "src/pitchi_work"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""synthetic external module"""')
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "notes.md").write_text("synthetic project")
    data = dict(enabled=True, source_root=str(source), python_executable="/usr/bin/python3",
                reviewed_source_sha256=source_digest(source), model_profile="image", model_endpoint="http://localhost/v1",
                model_name="synthetic", projects={"demo": str(project)}, timeout_seconds=1.0)
    data.update(values)
    return WorkSettings(**data)


def fake_spawn(*, final_status="COMPLETED", exit_status=0, mutation=None, events=None):
    async def spawn(*argv, **kwargs):
        assert kwargs["start_new_session"] is True
        assert not any("KEY" in k or "TOKEN" in k or "PROXY" in k for k in kwargs["env"])
        assert "instruction" not in str(argv)
        class Process:
            returncode = exit_status
            pid = 999999999
            stdout = asyncio.StreamReader()
            async def wait(self): return exit_status
        process = Process()
        class Input:
            def write(self, data):
                payload = json.loads(data)
                home = Path(payload["home"])
                run_id = "synthetic-run"
                run = home / "runs" / run_id
                run.mkdir(parents=True)
                artifacts = run / "artifacts"
                artifacts.mkdir()
                workspace = home / "workspaces" / run_id
                workspace.mkdir(parents=True)
                (workspace / "notes.md").write_text("result evidence")
                root = run / "root_goal.json"
                root.write_text(json.dumps({"request": payload["instruction"]}))
                sha = hashlib.sha256(root.read_bytes()).hexdigest()
                state = {"run_id": run_id, "status": final_status, "root_goal_sha256": sha, "active_processes": {}}
                completion = {"status": final_status, "root_goal_sha256": sha, "host_evidence_hashes_verified": True,
                              "criteria": [{"id": "R0", "met": True}]}
                receipt = artifacts / "receipt.json"
                receipt.write_text(json.dumps({"passed": True}))
                evidence = [{"passed": True, "reference": "artifacts/receipt.json", "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                             "artifact_hashes": {"notes.md": hashlib.sha256((workspace / "notes.md").read_bytes()).hexdigest()}}]
                if mutation: mutation(state, completion, evidence)
                (run / "state.json").write_text(json.dumps(state))
                (artifacts / "request_completion_gate.json").write_text(json.dumps(completion))
                (artifacts / "root_evidence.json").write_text(json.dumps(evidence))
                rows = events if events is not None else [
                    {"phase": "SUBMITTED", "run_id": run_id},
                    {"phase": "TERMINAL", "run_id": run_id, "status": final_status}]
                for row in rows:
                    process.stdout.feed_data((EVENT_PREFIX + json.dumps(row) + "\n").encode())
                process.stdout.feed_eof()
            async def drain(self): pass
            def close(self): pass
        process.stdin = Input()
        return process
    return spawn


def test_work_default_disabled():
    assert not load_work().enabled


@pytest.mark.parametrize("values", [{"enabled": "true"}, {"enabled": True}, {"model_endpoint": "http://example.org/v1"},
    {"projects": {"bad": "relative"}}, {"timeout_seconds": 10000}, {"command": "arbitrary"}])
def test_work_invalid_config(values):
    with pytest.raises((ValueError, ValidationError)):
        WorkSettings.model_validate(values)


@pytest.mark.parametrize("origins,profiles", [({}, frozenset({"image"})), ({"image": "http://localhost:9999"}, frozenset({"image"})),
                                                       ({"image": "http://localhost"}, frozenset())])
def test_work_cannot_reuse_foreign_model_owner(tmp_path, origins, profiles):
    settings = configure(tmp_path)
    assert not work_definition(settings, tmp_path / "private", profiles, origins).enabled


def test_work_job_persists_remote_run_and_validated_result(tmp_path):
    from test_gpu_manager import manager
    async def run():
        gpu, drivers, _ = manager(tmp_path)
        definition = work_definition(configure(tmp_path), tmp_path / "private", frozenset(drivers), {"image": "http://localhost"})
        definition.adapter.spawn = fake_spawn()
        h = Harness(None, ResourceCoordinator(gpu), AuditTrail(gpu.store), registry=ToolRegistry((definition,)),
                    policy=TurnPolicy(allowed_tools=frozenset({"pitch_work.run"})))
        jobs = JobStore(gpu.store)
        worker = JobWorker(jobs, h)
        worker.start()
        row = worker.submit(ToolCall(tool="pitch_work.run", arguments={"instruction": "Inspect only", "project": "demo"}))
        await asyncio.wait_for(worker.queue.join(), 2)
        result = jobs.get(row["job_id"], private=True)
        assert result["status"] == "SUCCESS" and result["remote_run_id"] == "synthetic-run"
        assert result["remote_phase"] == "TERMINAL" and result["result_reference"]
        output = json.loads(result["result_json"])["tool_result"]
        assert output["exit_status"] == 0 and output["output"]["deployed"] is False
        assert drivers["image"].events == ["start", "stop"] and gpu.phase.value == "STOPPED"
        await worker.close()
        assert jobs.get(row["job_id"])["status"] == "SUCCESS"
        with gpu.store.connection() as db:
            assert "Inspect only" not in str([r[0] for r in db.execute("SELECT metadata FROM harness_audit")])
        await gpu.close()
    asyncio.run(run())


@pytest.mark.parametrize("spawn", [
    fake_spawn(final_status="PARTIAL"), fake_spawn(exit_status=2),
    fake_spawn(mutation=lambda s, c, e: c.update(host_evidence_hashes_verified=False)),
    fake_spawn(mutation=lambda s, c, e: c.update(criteria=[])),
    fake_spawn(mutation=lambda s, c, e: c.update(criteria=[{"id": "M1", "met": True}])),
    fake_spawn(mutation=lambda s, c, e: s.update(active_processes={"synthetic": {"active": True}})),
    fake_spawn(mutation=lambda s, c, e: e[0].update(sha256="tampered")),
    fake_spawn(mutation=lambda s, c, e: e[0].update(reference="../outside.json")),
    fake_spawn(mutation=lambda s, c, e: e[0].update(artifact_hashes={"notes.md": "tampered"})),
    fake_spawn(events=[]), fake_spawn(events=[{"phase": "SUBMITTED", "run_id": "../outside"}]),
    fake_spawn(events=[{"phase": "SUBMITTED", "run_id": "first"}, {"phase": "TERMINAL", "run_id": "second"}]),
])
def test_external_success_without_contract_evidence_is_failure(tmp_path, spawn):
    async def run():
        adapter = PitchWorkAdapter(configure(tmp_path), tmp_path / "private", spawn=spawn)
        token = current_job_id.set("00000000-0000-0000-0000-000000000001")
        try:
            with pytest.raises(HarnessFailure):
                await adapter.execute(WorkArguments(instruction="Inspect", project="demo"), None)
        finally:
            current_job_id.reset(token)
    asyncio.run(run())


@pytest.mark.parametrize("condition", ["no_job", "unknown_project", "changed_source"])
def test_work_preflight_rejects_before_child(tmp_path, condition):
    async def run():
        settings = configure(tmp_path)
        if condition == "changed_source":
            (Path(settings.source_root) / "src/pitchi_work/__init__.py").write_text("changed")
        async def spawn(*args, **kwargs): pytest.fail("No execution allowed")
        adapter = PitchWorkAdapter(settings, tmp_path / "private", spawn=spawn)
        token = current_job_id.set(None if condition == "no_job" else "00000000-0000-0000-0000-000000000001")
        try:
            with pytest.raises(HarnessFailure):
                await adapter.execute(WorkArguments(instruction="Inspect", project="unknown" if condition == "unknown_project" else "demo"), None)
        finally:
            current_job_id.reset(token)
    asyncio.run(run())


def test_unknown_project_rejected_at_contract_before_gpu(tmp_path):
    from backend.app.harness.validator import validate_arguments
    definition = work_definition(configure(tmp_path), tmp_path / "private", frozenset({"image"}), {"image": "http://localhost"})
    with pytest.raises(HarnessFailure):
        validate_arguments({"instruction": "Inspect", "project": "unregistered"}, definition)


def test_work_timeout_records_uncertainty_and_signals_only_its_child(tmp_path, monkeypatch):
    import signal
    from backend.app.jobs.context import remote_callback
    async def run():
        settings = configure(tmp_path, timeout_seconds=.01)
        class Process:
            pid = 999999999
            returncode = None
            stdout = asyncio.StreamReader()
            class Input:
                def write(self, data): pass
                async def drain(self): pass
                def close(self): pass
            stdin = Input()
            async def wait(self):
                assert self.returncode == -15
                return self.returncode
        process = Process()
        signals, events = [], []
        def killpg(pid, sig):
            signals.append((pid, sig))
            assert pid == process.pid
            process.returncode = -15
        monkeypatch.setattr("backend.app.tools.pitch_work.os.killpg", killpg)
        async def spawn(*a, **kw): return process
        adapter = PitchWorkAdapter(settings, tmp_path / "private", spawn=spawn)
        token = current_job_id.set("00000000-0000-0000-0000-000000000001")
        callback = remote_callback.set(lambda phase, run_id: events.append(phase))
        try:
            with pytest.raises(HarnessFailure) as error:
                await adapter.execute(WorkArguments(instruction="Inspect", project="demo"), None)
            assert error.value.status.value == "STALLED"
            assert events == ["SUBMITTING"] and signals == [(process.pid, signal.SIGTERM)]
        finally:
            current_job_id.reset(token)
            remote_callback.reset(callback)
    asyncio.run(run())
