"""Same-origin read-only memory status and owned idle release; no swap controls."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from backend.app.resources.manager import GPUUnsafe
from backend.app.resources.memory import memory_state, check_profile_memory
from backend.app.resources.memory_recovery import admission_message, failure_detail
from backend.app.resources.media_memory import selected_backend, effective_profile


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['strata', 'comfyui', 'vision']
    action: Literal['stop', 'recover'] = 'stop'


def resource_routes(app):
    routes = APIRouter()

    @routes.get('/api/resources/memory')
    async def status(response: Response, target: Literal['strata','comfyui','vision']='comfyui', backend: Literal["sdxl","qwen-image","flux-klein","flux-klein-edit","minimax-h3"] | None=None):
        response.headers['Cache-Control']='no-store'
        manager=app.state.gpu_manager
        reason=None; detail=None; state={}; profile=None
        try:
            state=memory_state()
            # Numeric host data only; no process identities, endpoints or payloads.
            from backend.app.resources.memory_recovery import linux_memory_details
            extra=linux_memory_details()
            state.update(ram_total_mib=extra['MemTotal'],ram_used_mib=extra['MemTotal']-extra['MemAvailable'])
            await manager.inspect()
            driver=manager.drivers.get(target)
            if driver is None:raise GPUUnsafe('profile_unmanaged')
            if manager.phase.value == 'ERROR':raise GPUUnsafe('recovery_required')
            if manager.phase.value in {'BUSY','STARTING','STOPPING'} or manager.waiting or manager.usage_count():
                raise GPUUnsafe('model_in_use')
            with selected_backend(manager,target,backend):
                profile=effective_profile(driver.profile)
                if manager.loaded not in {None,target}:raise GPUUnsafe('release_required')
                await manager.preflight(target)
                detail=check_profile_memory(profile,stage='runtime' if manager.loaded==target else 'preflight')
                state.update(detail)
        except GPUUnsafe as error:
            reason=str(error);detail=getattr(error,'memory_details',None)
            if detail:state.update(detail)
        except (OSError,ValueError):
            reason='memory_safety_telemetry_unavailable'
        reading=manager.reading
        required=state.get('admission_ram_mib',getattr(profile,'required_ram_mib',None))
        message=admission_message(reason,state,required or 0,getattr(profile,'min_swap_free_mib',0)) if state else None
        messages={'model_in_use':'生成中・起動停止中または待機中の要求があります。完了を待って再確認してください。','release_required':'現在の管理対象モデルを安全に停止し、解放後に再確認します。生成要求では既存の排他管理が切り替えます。',
                  'backend_memory_budget_missing':'このバックエンドの実測済みメモリ予算がありません。起動を見合わせます。',
                  'recovery_required':'所有状態が隔離されています。実行状態を確認してから復旧してください。'}
        return state | dict(target=target,backend=backend,ready_to_start=reason is None,reason=reason,
            message=message or messages.get(reason,reason),required_ram_mib=required,
            required_vram_mib=getattr(profile,'required_vram_mib',None),
            vram_used_mib=reading.used_mib if reading else None,
            vram_free_mib=reading.total_mib-reading.used_mib if reading and reading.total_mib else None,
            loaded_model=manager.loaded,loaded_backend=getattr(manager,'active_backend',None) if manager.loaded=='comfyui' else None,queued=manager.waiting,retained_uses=manager.usage_count(),
            state=manager.phase.value,last_release=manager.last_release,automatic_swap_reset=False)

    @routes.post('/api/resources/release')
    async def release(body: ReleaseRequest, response: Response):
        response.headers['Cache-Control']='no-store'
        manager=app.state.gpu_manager
        # Check persistent queues, including jobs still preparing prompts.
        store=app.state.jobs.store
        with store.connection() as db:
            active=sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN ('QUEUED','RUNNING','CANCELLING')").fetchone()[0]
                       for table in ('tool_jobs','chat_generation_jobs'))
        resource=app.state.harness.resource_state()
        if active or resource.active_job or resource.waiting_jobs:
            raise HTTPException(409,{'reason':'model_in_use','message':'生成中または待機中の要求があります。完了を待ってから再確認してください。'})
        if body.target not in manager.drivers:
            raise HTTPException(409,{'reason':'profile_unmanaged'})
        try:
            if body.action=='recover':await manager.recover_idle(body.target)
            else:await manager.stop_idle(body.target)
        except GPUUnsafe as error:
            detail=getattr(error,'memory_details',None)
            raise HTTPException(409,failure_detail('resource_unavailable',detail) if detail else
                {'reason':str(error),'message':'モデルの使用権・実行状態・所有権を確認できないため停止していません。'}) from None
        return {'state':manager.phase.value,'last_release':manager.last_release}
    return routes
