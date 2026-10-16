"""Synthetic/mocked resource checks; no actual GPU or models."""
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import httpx
import pytest
from backend.app.resources.process import ProcessProfile, OwnedProcess
from backend.app.resources.manager import GPUReading, GPUUnsafe
from backend.app.resources.memory import check_profile_memory,MemoryAdmissionFailure
from backend.app.resources.media_memory import selected_backend,effective_profile,GenerationMemoryWatch
from backend.app.main import create_app
from backend.app.resources.config import ResourceSettings,load_resources


def profile(tmp_path):
 return ProcessProfile('comfyui',('/synthetic/python',),tmp_path,'http://127.0.0.1:18188/system_stats',required_ram_mib=43008,min_swap_free_mib=1024,runtime_ram_reserve_mib=4096,required_vram_mib=10500,graceful_only=True,low_swap_start_ram_mib=47104,backend_ram_mib=(('sdxl',16384),('flux-klein',20480),('qwen-image',43008),('minimax-h3',32768)))

@pytest.mark.parametrize('backend,ram',[('sdxl',20480),('flux-klein',24576),('qwen-image',47104),('minimax-h3',36864)])
def test_each_backend_gets_its_reviewed_low_swap_budget(tmp_path,backend,ram):
 p=profile(tmp_path);manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p)})
 with patch('backend.app.resources.memory.memory_state',return_value={'ram_available_mib':53000,'swap_free_mib':0,'swap_used_mib':8192}),patch('backend.app.resources.memory.physical_ram_safety',return_value=None):
  with selected_backend(manager,'comfyui',backend):
   assert check_profile_memory(p)['admission_ram_mib']==ram
  assert effective_profile(p)==p


def test_unknown_backend_and_low_ram_refuse(tmp_path):
 p=profile(tmp_path);manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p)})
 with pytest.raises(GPUUnsafe,match='backend_memory_budget_missing'):
  with selected_backend(manager,'comfyui','unknown'):pass
 with selected_backend(manager,'comfyui','sdxl'),patch('backend.app.resources.memory.memory_state',return_value={'ram_available_mib':18000,'swap_free_mib':0,'swap_used_mib':8192}):
  with pytest.raises(MemoryAdmissionFailure,match='insufficient_ram'):check_profile_memory(p)


def test_runtime_failure_is_held_until_terminal_not_remote_interrupted(tmp_path):
 p=profile(tmp_path);manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p)})
 with selected_backend(manager,'comfyui','sdxl'),patch.object(GenerationMemoryWatch,'_oom',side_effect=[0,0,1]),patch('backend.app.resources.memory.check_profile_memory',return_value={'ram_available_mib':5000,'swap_free_mib':0,'swap_used_mib':8192}):
  watch=GenerationMemoryWatch();watch.sample() # records, never interrupts remote generation
  with pytest.raises(MemoryAdmissionFailure,match='oom_observed'):watch.finish()

@pytest.mark.parametrize('busy',[False,True])
def test_official_unload_never_clears_or_interrupts_queue(tmp_path,busy):
 async def run():
  calls=[]
  def handle(r):
   calls.append((r.method,r.url.path))
   if r.url.path=='/queue':return httpx.Response(200,json={'queue_running':['existing'] if busy else [],'queue_pending':[]})
   if r.url.path=='/free':return httpx.Response(200)
   if r.url.path=='/system_stats':return httpx.Response(200,json={'devices':[{'torch_vram_total':1000,'torch_vram_free':1000}]})
   pytest.fail('Unexpected control route')
  async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
   driver=OwnedProcess(profile(tmp_path),client)
   if busy:
    with pytest.raises(GPUUnsafe,match='model_in_use'):await driver.unload_comfy()
    assert ('POST','/free') not in calls
   else:
    await driver.unload_comfy();assert driver.last_unload['unload_requested']
    assert ('POST','/free') in calls
 asyncio.run(run())


def test_unload_failure_does_not_signal_process(tmp_path):
 async def run():
  def handle(r):
   if r.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
   return httpx.Response(503)
  async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
   driver=OwnedProcess(profile(tmp_path),client);driver.process=SimpleNamespace(returncode=None)
   with patch.object(driver,'_signal',side_effect=AssertionError('must not signal')):
    with pytest.raises(GPUUnsafe,match='comfy_unload_unverified'):await driver.stop()
 asyncio.run(run())


def test_status_and_queued_job_stop_protection(settings,tmp_path,monkeypatch):
 async def inspect(manager):manager.reading=GPUReading(0,frozenset(),12288);return manager.reading
 from backend.app.resources.manager import GPUManager
 monkeypatch.setattr(GPUManager,'inspect',inspect)
 monkeypatch.setattr('backend.app.resources.memory.physical_ram_safety',lambda *a,**kw:None)
 monkeypatch.setattr('backend.app.resources.memory.memory_state',lambda:{'ram_available_mib':53000,'swap_free_mib':0,'swap_used_mib':8192})
 monkeypatch.setattr('backend.app.resources.control.memory_state',lambda:{'ram_available_mib':53000,'swap_free_mib':0,'swap_used_mib':8192})
 app=create_app(settings,services=(),resource_settings=ResourceSettings(inspect_gpu=True,profiles=(profile(tmp_path),)))
 async def run():
  async with app.router.lifespan_context(app):
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
    r=await client.get('/api/resources/memory?target=comfyui&backend=sdxl');assert r.status_code==200
    d=r.json();assert d['ready_to_start'] and d['required_ram_mib']==20480 and d['swap_free_mib']==0
    assert r.headers['cache-control']=='no-store'
    # A preparation job before any GPU lease must protect release too.
    with app.state.jobs.store.connection() as db:
     db.execute("INSERT INTO tool_jobs (job_id,tool,status,call_json,requires_gpu) VALUES ('synthetic','image.generate','QUEUED','{}',1)")
    r=await client.post('/api/resources/release',json={'target':'comfyui'});assert r.status_code==409
    assert r.json()['detail']['reason']=='model_in_use'
    with app.state.jobs.store.connection() as db:db.execute("UPDATE tool_jobs SET status='ERROR' WHERE job_id='synthetic'")
    assert (await client.post('/api/resources/release',json={'target':'comfyui'})).status_code==200
 asyncio.run(run())


def test_comfy_config_preserves_strata_specific_floor(tmp_path):
 path=tmp_path/'resources.toml'
 path.write_text('''[gpu]
inspect=true
[profiles.comfyui]
argv=["/synthetic/python"]
cwd="/tmp"
health_url="http://127.0.0.1:18188/system_stats"
required_ram_mib=43008
runtime_ram_reserve_mib=4096
low_swap_start_ram_mib=47104
min_swap_free_mib=1024
graceful_only=true
[profiles.comfyui.backend_ram_mib]
sdxl=16384
''')
 assert load_resources(path).profiles[0].backend_ram_mib==(('sdxl',16384),)
 path.write_text(path.read_text().replace('sdxl=16384','sdxl=true'))
 with pytest.raises(ValueError):load_resources(path)


def test_memory_management_dom():
 import shutil,subprocess
 if not shutil.which('node'):pytest.skip('Node not installed')
 subprocess.run(['node','tests/frontend_memory.cjs'],check=True)


@pytest.mark.parametrize('kind,reason',[('MemoryError','allocation_failed'),('torch.OutOfMemoryError','gpu_allocation_failed')])
def test_actual_allocator_errors_are_distinct_from_swap_refusal(kind,reason):
 from backend.app.resources.media_memory import allocation_failure
 with pytest.raises(MemoryAdmissionFailure,match=reason) as error:
  allocation_failure({'messages':[['execution_error',{'exception_type':kind,'exception_message':'private synthetic payload'}]]})
 assert 'private synthetic payload' not in str(error.value.memory_details)


def test_loaded_backend_is_recorded_only_under_owned_dispatch(tmp_path):
 p=profile(tmp_path);manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p)},loaded='comfyui')
 with selected_backend(manager,'comfyui','sdxl'),patch.object(GenerationMemoryWatch,'_oom',return_value=0),patch('backend.app.resources.memory.check_profile_memory',return_value={}):
  GenerationMemoryWatch()
 assert manager.active_backend=='sdxl'


def test_gpu_violation_waits_for_terminal_without_stopping_owned_job(tmp_path):
 p=profile(tmp_path)
 async def inspect():return GPUReading(12000,frozenset({123}),12288)
 policy=SimpleNamespace(running_reason=lambda *args:'insufficient_vram')
 manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p,owned_pids=frozenset({123}))},loaded='comfyui',inspect=inspect,ownership_policy=policy)
 with selected_backend(manager,'comfyui','sdxl'),patch.object(GenerationMemoryWatch,'_oom',return_value=0),patch('backend.app.resources.memory.check_profile_memory',return_value={}):
  watch=GenerationMemoryWatch();asyncio.run(watch.sample_gpu())
  with pytest.raises(MemoryAdmissionFailure,match='insufficient_vram') as found:watch.finish()
  assert found.value.memory_details['stage']=='generation'
  assert found.value.memory_details['vram_used_mib']==12000


def test_unmeasured_batch_shape_rejected_before_any_switch(tmp_path):
 p=profile(tmp_path);manager=SimpleNamespace(drivers={'comfyui':SimpleNamespace(profile=p)})
 with pytest.raises(MemoryAdmissionFailure,match='media_memory_shape_unreviewed'):
  with selected_backend(manager,'comfyui','sdxl',count=4):pytest.fail('must reject before resource acquisition')


def test_strata_stream_uses_same_reviewed_runtime_profile_and_clears_scope(tmp_path):
 from backend.app.resources.memory import scoped_stream_memory,check_stream_memory
 p=ProcessProfile('strata',('/synthetic/python',),tmp_path,'http://127.0.0.1:18080/health',required_ram_mib=43008,min_swap_free_mib=1024,runtime_ram_reserve_mib=5120,low_swap_start_ram_mib=49152)
 state={'ram_available_mib':17000,'swap_free_mib':0,'swap_used_mib':8192}
 with patch('backend.app.resources.memory.memory_state',return_value=state),patch('backend.app.resources.memory.physical_ram_safety',return_value=None):
  with scoped_stream_memory(p):assert check_stream_memory(5120)['admission_mode']=='physical_ram'
  with pytest.raises(MemoryAdmissionFailure,match='insufficient_swap_reserve'):check_stream_memory(5120)
 state['ram_available_mib']=5119
 with scoped_stream_memory(p),patch('backend.app.resources.memory.memory_state',return_value=state):
  with pytest.raises(MemoryAdmissionFailure,match='insufficient_ram'):check_stream_memory(5120)


def test_real_sse_adapter_accepts_zero_swap_only_under_host_profile(settings,tmp_path):
 from tests.test_strata import adapter,sse,chunk
 from backend.app.models.base import Message
 from backend.app.resources.memory import scoped_stream_memory
 p=ProcessProfile('strata',('/synthetic/python',),tmp_path,'http://127.0.0.1:18080/health',required_ram_mib=43008,min_swap_free_mib=1024,runtime_ram_reserve_mib=5120,low_swap_start_ram_mib=49152)
 async def run():
  a=adapter(settings,lambda request:httpx.Response(200,text=sse([chunk({'content':'確認できました'},'stop')])))
  try:
   with scoped_stream_memory(p),patch('backend.app.resources.memory.memory_state',return_value={'ram_available_mib':17000,'swap_free_mib':0,'swap_used_mib':8192}),patch('backend.app.resources.memory.physical_ram_safety',return_value=None):
    response=await a.generate_chat([Message('user','synthetic')],memory_reserve_mib=5120)
    assert response.final_content=='確認できました'
  finally:await a.client.aclose()
 asyncio.run(run())
