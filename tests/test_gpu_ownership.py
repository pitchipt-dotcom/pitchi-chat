"""Synthetic ownership/admission tests, not real GPU integration."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from backend.app.resources.manager import GPUReading, GPUUnsafe, NvidiaInspector
from backend.app.resources.ownership import ExternalIdentity, GPUProcess, Ownership, OwnershipPolicy, process_identity
from backend.app.resources.config import load_resources
from tests.test_gpu_manager import manager


SHARED = GPUProcess(99, 100, "C", 700, "desktop", "0::/display.scope")
RULE = ExternalIdentity("desktop", 99, 700, "desktop", "0::/display.scope", Ownership.EXTERNAL_SHARED, 256)


def reading(processes=(SHARED,), used=150, total=12288):
    return GPUReading(used, frozenset(p.pid for p in processes), total, tuple(processes))


def test_classification_does_not_grant_ownership():
    policy = OwnershipPolicy((RULE,))
    assert policy.classify(reading(), frozenset())[99][0] == Ownership.EXTERNAL_SHARED
    assert policy.classify(reading(), frozenset({99}))[99][0] == Ownership.HOST_OWNED
    assert policy.classify(reading(), frozenset())[99][0] != Ownership.HOST_OWNED


@pytest.mark.parametrize("changes", [{"start_ticks": 701}, {"comm": "other"}, {"cgroup": "0::/other"}, {"pid": 98}, {"start_ticks": 0}])
def test_identity_mismatch_fails_closed(changes):
    assert OwnershipPolicy((RULE,)).external_reason(reading((replace(SHARED, **changes),)), frozenset()) == "unknown_gpu_process"


@pytest.mark.parametrize("memory", [-1, 257])
def test_shared_memory_bound(memory):
    assert OwnershipPolicy((RULE,)).external_reason(reading((replace(SHARED, used_mib=memory),)), frozenset()) == "shared_memory_cap_exceeded"


def test_external_workload_blocks_even_when_idle():
    rule = replace(RULE, classification=Ownership.EXTERNAL_BLOCKING)
    assert OwnershipPolicy((rule,)).admission_reason(reading(), frozenset(), 8000) == "external_gpu_workload"


@pytest.mark.parametrize("used,total,budget,expected", [
    (150, 12288, 8000, None), (150, 12288, 0, "profile_memory_budget_missing"),
    (150, None, 8000, "memory_capacity_unknown"), (5000, 12288, 8000, "insufficient_vram"),
    (150, 8700, 8000, "insufficient_vram")])
def test_admission_reserves_caps_and_headroom(used, total, budget, expected):
    assert OwnershipPolicy((RULE,)).admission_reason(reading(used=used, total=total), frozenset(), budget) == expected


def test_release_allows_only_verified_background():
    policy = OwnershipPolicy((RULE,))
    assert policy.released(reading(), frozenset({10}), 256)
    assert not policy.released(reading(used=800), frozenset({10}), 256)
    assert not policy.released(reading(), frozenset({99}), 256)
    assert not OwnershipPolicy().released(reading(), frozenset(), 256)


def test_process_identity_read_failure_is_unknown(tmp_path):
    assert process_identity(99, 100, "C", tmp_path).start_ticks == 0


def test_running_budget_and_reserve():
    owned = GPUProcess(10, 8100, "C")
    policy = OwnershipPolicy((RULE,))
    assert policy.running_reason(reading((SHARED, owned), used=8250), frozenset({10}), 8000) == "profile_memory_budget_exceeded"
    assert policy.running_reason(reading((SHARED, owned), used=12000), frozenset({10}), 8500) == "insufficient_vram"


def test_shared_context_survives_owned_start_and_stop(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        inspector.pids.add(99)
        inspector.used = 150
        async def read():
            processes = (SHARED,) + tuple(GPUProcess(pid, 1000, "C") for pid in inspector.pids if pid != 99)
            return reading(processes, used=150 + (1000 if drivers["qwen"].started else 0))
        inspector.read = read
        gpu.ownership_policy = OwnershipPolicy((RULE,))
        drivers["qwen"].profile = SimpleNamespace(device="gpu", required_vram_mib=8000)
        await gpu.preflight("qwen")
        async with gpu.lease("qwen", "job"):
            assert inspector.pids == {10, 99}
        assert inspector.pids == {99} and gpu.phase.value == "STOPPED"
        snapshot = str(gpu.snapshot())
        assert "display.scope" not in snapshot and "desktop" not in snapshot
        assert gpu.snapshot()["process_classifications"]["EXTERNAL_SHARED"] == 1
        await gpu.close()
    asyncio.run(run())


def test_new_external_workload_rejects_final_result_and_stops_only_owned(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        with pytest.raises(GPUUnsafe):
            async with gpu.lease("qwen", "job", keep_ready=True):
                inspector.pids.add(999)
        assert not drivers["qwen"].started and inspector.pids == {999}
        assert gpu.phase.value == "ERROR"  # foreign workload prevents release proof
        await gpu.close()
    asyncio.run(run())


def test_nvidia_xml_includes_graphics_and_compute(monkeypatch):
    inspector = NvidiaInspector()
    async def query(_):
        return '''<nvidia_smi_log><gpu><fb_memory_usage><used>150 MiB</used><total>12288 MiB</total></fb_memory_usage><processes>
        <process_info><pid>99</pid><type>G</type><used_memory>100 MiB</used_memory></process_info>
        <process_info><pid>10</pid><type>C</type><used_memory>50 MiB</used_memory></process_info>
        </processes></gpu></nvidia_smi_log>'''
    monkeypatch.setattr(inspector, "_query", query)
    value = asyncio.run(inspector.read())
    assert value.pids == {99, 10} and value.total_mib == 12288
    assert [p.kind for p in value.processes] == ["G", "C"]


@pytest.mark.parametrize("xml", ["bad", "<gpu/>", "<nvidia_smi_log><gpu><fb_memory_usage><used>N/A</used></fb_memory_usage></gpu></nvidia_smi_log>"])
def test_invalid_telemetry_is_not_empty_gpu(monkeypatch, xml):
    inspector = NvidiaInspector()
    async def query(_): return xml
    monkeypatch.setattr(inspector, "_query", query)
    with pytest.raises(GPUUnsafe): asyncio.run(inspector.read())


@pytest.mark.parametrize("field,value", [("classification", '"HOST_OWNED"'), ("pid", "true"), ("start_ticks", "0"), ("max_memory_mib", "-1")])
def test_config_cannot_grant_ownership_or_invalid_pin(tmp_path, field, value):
    fields = dict(name='"background"', pid="99", start_ticks="700", comm='"desktop"', cgroup='"0::/display.scope"', classification='"EXTERNAL_SHARED"', max_memory_mib="256")
    fields[field] = value
    path = tmp_path / "resources.toml"
    path.write_text('[gpu]\ninspect=true\n[[external_processes]]\n' + '\n'.join(f'{k}={v}' for k,v in fields.items()))
    with pytest.raises(ValueError): load_resources(path)


def test_valid_private_identity_config(tmp_path):
    path = tmp_path / "resources.toml"
    path.write_text('''[gpu]
inspect=true
reserve_mib=768
[[external_processes]]
name="background"
pid=99
start_ticks=700
comm="desktop"
cgroup="0::/display.scope"
classification="EXTERNAL_SHARED"
max_memory_mib=256
''')
    settings = load_resources(path)
    assert settings.external_processes == (replace(RULE, name="background"),)
    assert settings.reserve_mib == 768


def test_diagnostic_default_does_not_expose_identity(monkeypatch):
    from scripts import inspect_gpu
    from backend.app.resources.config import ResourceSettings
    monkeypatch.setattr(inspect_gpu, "load_resources", lambda _: ResourceSettings(inspect_gpu=True, external_processes=(RULE,)))
    async def read(_): return reading()
    monkeypatch.setattr(inspect_gpu.NvidiaInspector, "read", read)
    result = asyncio.run(inspect_gpu.inspect(None))
    assert result["classifications"]["EXTERNAL_SHARED"] == 1
    assert "private_processes" not in result
    assert "display.scope" not in str(result) and "desktop" not in str(result)


def test_diagnostic_requires_opt_in(monkeypatch):
    from scripts import inspect_gpu
    from backend.app.resources.config import ResourceSettings
    monkeypatch.setattr(inspect_gpu, "load_resources", lambda _: ResourceSettings())
    with pytest.raises(ValueError): asyncio.run(inspect_gpu.inspect(None))


def test_blocking_preflight_has_no_process_or_persistence_side_effect(tmp_path):
    async def run():
        gpu, drivers, inspector = manager(tmp_path)
        inspector.pids.add(99)
        async def read(): return reading()
        inspector.read = read
        gpu.ownership_policy = OwnershipPolicy((replace(RULE, classification=Ownership.EXTERNAL_BLOCKING),))
        with pytest.raises(GPUUnsafe, match="external_gpu_workload"):
            async with gpu.lease("qwen", "rejected"):
                pytest.fail("must not dispatch")
        assert gpu.phase.value == "STOPPED" and gpu.loaded is None
        assert all(not driver.events for driver in drivers.values())
        with gpu.store.connection() as db:
            assert db.execute("SELECT count(*) FROM gpu_events").fetchone()[0] == 0
        await gpu.close()
    asyncio.run(run())
