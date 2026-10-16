"""Synthetic memory admission; no swap, GPU or real models changed."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import asyncio
import sys
import httpx
import pytest
from backend.app.resources.memory import check_memory, check_profile_memory, physical_ram_safety, MemoryAdmissionFailure
from backend.app.resources.process import ProcessProfile, OwnedProcess
from backend.app.resources.config import load_resources

STATE = {'ram_available_mib': 52960, 'swap_free_mib': 0, 'swap_used_mib': 8192}

@pytest.mark.parametrize('free', [0, 301, 1023])
def test_reviewed_physical_ram_does_not_need_swap_reset(free):
    with patch('backend.app.resources.memory.memory_state', return_value=STATE | {'swap_free_mib': free}), patch('backend.app.resources.memory.physical_ram_safety', return_value=None) as guard:
        result = check_memory(43008, 1024, low_swap_ram_mib=49152)
        assert result['admission_mode'] == 'physical_ram'
        assert result['admission_ram_mib'] == 49152
        guard.assert_called_once_with(49152,pressure_window="avg10")

@pytest.mark.parametrize('available', [43008, 48000, 49151])
def test_low_swap_requires_larger_physical_reserve(available):
    with patch('backend.app.resources.memory.memory_state', return_value=STATE | {'ram_available_mib': available}):
        with pytest.raises(MemoryAdmissionFailure, match='insufficient_ram') as found:
            check_memory(43008, 1024, low_swap_ram_mib=49152)
        assert found.value.memory_details['required_ram_mib'] == 49152

@pytest.mark.parametrize('reason', ['memory_pressure','insufficient_cgroup_ram','memory_safety_telemetry_unavailable'])
def test_pressure_limits_and_missing_telemetry_fail_closed(reason):
    with patch('backend.app.resources.memory.memory_state', return_value=STATE), patch('backend.app.resources.memory.physical_ram_safety', return_value=reason):
        with pytest.raises(MemoryAdmissionFailure, match=reason):
            check_memory(43008, 1024, low_swap_ram_mib=49152)

def test_default_swap_gate_is_preserved():
    with patch('backend.app.resources.memory.memory_state', return_value=STATE):
        with pytest.raises(MemoryAdmissionFailure, match='insufficient_swap_reserve'):
            check_memory(43008, 1024)

@pytest.mark.parametrize('ram,ok', [(5120, True), (5119, False)])
def test_runtime_keeps_five_gib_reserve_even_with_zero_swap(ram, ok):
    profile=SimpleNamespace(low_swap_start_ram_mib=49152, runtime_ram_reserve_mib=5120)
    with patch('backend.app.resources.memory.memory_state', return_value=STATE | {'ram_available_mib':ram}), patch('backend.app.resources.memory.physical_ram_safety', return_value=None):
        if ok: assert check_profile_memory(profile,stage='runtime')['admission_ram_mib']==5120
        else:
            with pytest.raises(MemoryAdmissionFailure,match='insufficient_ram'):
                check_profile_memory(profile,stage='runtime')

@pytest.mark.parametrize('some,full,limit,used,expected', [(0,0,'max',0,None),(6,0,'max',0,'memory_pressure'),(0,2,'max',0,'memory_pressure'),(0,0,str(50*1024**3),3*1024**3,'insufficient_cgroup_ram'),(0,0,str(60*1024**3),3*1024**3,None)])
def test_pressure_and_ancestor_limits(some,full,limit,used,expected):
    def read(path,*args,**kwargs):
        if str(path)=='/proc/pressure/memory':return f'some avg10={some} avg60=0\nfull avg10={full} avg60=0\n'
        if str(path)=='/proc/self/cgroup':return '0::/parent/host\n'
        if path.name in {'memory.max','memory.high'}:return limit if path.parent.name=='parent' else 'max'
        if path.name=='memory.current':return str(used)
        raise OSError('synthetic telemetry failure')
    with patch.object(Path,'read_text',read):
        assert physical_ram_safety(49152)==expected

def test_missing_pressure_fails_closed():
    with patch.object(Path,'read_text',side_effect=OSError):
        assert physical_ram_safety(49152)=='memory_safety_telemetry_unavailable'

@pytest.mark.parametrize('name,budget,ok', [('strata',49152,True),('strata',49151,False),('strata',0,True),('strata',True,False),('other',49152,False)])
def test_reviewed_budget_config(tmp_path,name,budget,ok):
    config=tmp_path/'resources.toml'
    config.write_text(f'''[gpu]
inspect=true
[profiles.{name}]
argv=["/usr/bin/python3"]
cwd="/tmp"
health_url="http://127.0.0.1:18080/health"
required_vram_mib=10500
required_ram_mib=43008
min_swap_free_mib=1024
runtime_ram_reserve_mib=5120
manual_start=true
graceful_only=true
low_swap_start_ram_mib={str(budget).lower()}
''')
    if ok:assert load_resources(config).profiles[0].low_swap_start_ram_mib==budget
    else:
        with pytest.raises(ValueError):load_resources(config)

def test_startup_memory_guard_runs_before_readiness(tmp_path):
    async def run():
        def unavailable(request):raise httpx.ConnectError('synthetic unavailable')
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            profile=ProcessProfile('strata',(sys.executable,'-c','import time; time.sleep(30)'),tmp_path,'http://localhost/health',low_swap_start_ram_mib=49152,runtime_ram_reserve_mib=5120)
            driver=OwnedProcess(profile,client)
            with patch('backend.app.resources.memory.check_profile_memory', side_effect=MemoryAdmissionFailure('insufficient_ram',STATE,5120,256,'startup')) as guard:
                try:
                    with pytest.raises(MemoryAdmissionFailure):await driver.start()
                    assert driver.process is not None
                    guard.assert_called_once_with(profile,stage='startup')
                finally:await driver.stop()
                assert not driver.owned_pids
    asyncio.run(run())

@pytest.mark.parametrize('ram,ready', [(52960,True),(48000,False)])
def test_preflight_reports_effective_budget_without_spawn(settings,tmp_path,monkeypatch,ram,ready):
    from backend.app.main import create_app
    from backend.app.resources.config import ResourceSettings
    from backend.app.resources.manager import GPUManager,GPUReading
    async def inspect(manager):
        manager.reading=GPUReading(0,frozenset(),12288)
        return manager.reading
    async def forbidden_start(self):pytest.fail('Read-only preflight must not spawn')
    monkeypatch.setattr(GPUManager,'inspect',inspect)
    monkeypatch.setattr(OwnedProcess,'start',forbidden_start)
    monkeypatch.setattr('backend.app.resources.memory.memory_state',lambda:STATE | {'ram_available_mib':ram})
    monkeypatch.setattr('backend.app.main.memory_state',lambda:STATE | {'ram_available_mib':ram})
    monkeypatch.setattr('backend.app.resources.memory.physical_ram_safety',lambda budget,**kwargs:None)
    profile=ProcessProfile('strata',('/synthetic/python',),tmp_path,'http://127.0.0.1:18080/health',required_ram_mib=43008,min_swap_free_mib=1024,runtime_ram_reserve_mib=5120,manual_start=True,low_swap_start_ram_mib=49152)
    app=create_app(settings,services=(),resource_settings=ResourceSettings(inspect_gpu=True,profiles=(profile,)))
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.get('/api/models/strata/preflight')
                data=response.json()
                assert response.status_code==200 and data['ready_to_start'] is ready
                assert data['admission_ram_mib']==49152
                assert data['admission_mode']=='physical_ram'
                assert data['reason'] is None if ready else data['reason']=='insufficient_ram'
                assert ('sudo操作は不要' if ready else '49152 MiB') in data['message']
                assert app.state.gpu_manager.loaded is None
    asyncio.run(run())


def test_frontend_uses_effective_budget_and_skips_recovery_when_ready():
    js=Path('frontend/chat.js').read_text()
    assert 'data.admission_ram_mib ?? data.required_ram_mib' in js
    assert 'data.ready_to_start ? "" : data.recovery?.instructions' in js
    assert 'data.message || (data.ready_to_start' in js


def test_startup_pressure_uses_sustained_window_with_hard_ram_guard():
    profile=SimpleNamespace(low_swap_start_ram_mib=49152,runtime_ram_reserve_mib=5120)
    with patch('backend.app.resources.memory.memory_state',return_value=STATE), patch('backend.app.resources.memory.physical_ram_safety',return_value=None) as guard:
        check_profile_memory(profile,stage='startup')
        guard.assert_called_once_with(5120,pressure_window='avg60')
    with patch.object(Path,'read_text',side_effect=OSError):
        assert physical_ram_safety(5120,pressure_window='avg60')=='memory_safety_telemetry_unavailable'


@pytest.mark.parametrize('some,full,expected',[(0,0,None),(6,0,'memory_pressure'),(0,2,'memory_pressure')])
def test_sustained_pressure_guard(some,full,expected):
    def read(path,*args,**kwargs):
        if str(path)=='/proc/pressure/memory':return f'some avg10=10 avg60={some}\nfull avg10=10 avg60={full}\n'
        if str(path)=='/proc/self/cgroup':return '0::/host\n'
        if path.name in {'memory.max','memory.high'}:return 'max'
        raise OSError('synthetic failure')
    with patch.object(Path,'read_text',read):
        assert physical_ram_safety(5120,pressure_window='avg60')==expected

def test_startup_pressure_failure_explains_cause_without_claiming_exit():
    from backend.app.resources.memory_recovery import failure_detail
    detail=failure_detail('resource_unavailable',STATE | dict(reason='memory_pressure',stage='startup',required_ram_mib=5120,min_swap_free_mib=256))
    assert '起動中に継続的なメモリ圧迫' in detail['message']
    assert '停止しました' not in detail['message']
    assert '隔離状態' in detail['message']
