"""Reviewed media budgets beneath the existing Host lock, never model input."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from backend.app.resources.manager import GPUUnsafe

_current = ContextVar('reviewed_media_memory', default=None)


def effective_profile(profile):
    selected = _current.get()
    if selected and profile.name == 'comfyui':
        budgets = dict(getattr(profile, 'backend_ram_mib', ()))
        if budgets:
            if selected[1] not in budgets:
                raise GPUUnsafe('backend_memory_budget_missing')
            ram = budgets[selected[1]]
            return replace(profile, required_ram_mib=ram,
                           low_swap_start_ram_mib=ram + profile.runtime_ram_reserve_mib)
    return profile


@contextmanager
def selected_backend(manager, owner, backend, *, count=1):
    profile = getattr(getattr(manager, 'drivers', {}).get(owner), 'profile', None)
    if profile and owner == 'comfyui' and getattr(profile, 'backend_ram_mib', ()) and count != 1:
        from backend.app.resources.memory import MemoryAdmissionFailure, memory_state
        raise MemoryAdmissionFailure('media_memory_shape_unreviewed', memory_state(), profile.required_ram_mib, profile.min_swap_free_mib, 'preflight')
    token = _current.set((profile, backend, manager) if profile and owner == 'comfyui' else None)
    try:
        if profile and owner == 'comfyui':
            effective_profile(profile)
        yield
    finally:
        _current.reset(token)


class GenerationMemoryWatch:
    """Retain safety failure until remote terminal; never interrupt a running graph."""
    def __init__(self):
        selected = _current.get()
        self.profile = effective_profile(selected[0]) if selected else None
        if selected and getattr(selected[2], 'loaded', None) == 'comfyui':
            selected[2].active_backend = selected[1]
        self.enabled = bool(self.profile and getattr(self.profile, 'low_swap_start_ram_mib', 0))
        self.manager = selected[2] if selected else None
        self.failure = None
        self.oom = self._oom() if self.enabled else None
        self.sample()
        self.finish()  # No remote submit yet: reject immediately if unsafe.

    @staticmethod
    def _oom():
        try:
            return int(dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())['oom_kill'])
        except (OSError, ValueError, KeyError):
            raise GPUUnsafe('memory_safety_telemetry_unavailable') from None

    def sample(self):
        if self.enabled and self.failure is None:
            try:
                from backend.app.resources.memory import check_profile_memory, MemoryAdmissionFailure
                state = check_profile_memory(self.profile, stage='generation')
                if self._oom() != self.oom:
                    raise MemoryAdmissionFailure('oom_observed', state, self.profile.runtime_ram_reserve_mib, 256, 'generation')
            except GPUUnsafe as error:
                self.failure = error

    async def sample_gpu(self):
        if not self.enabled or self.failure is not None:
            return
        try:
            reading = await self.manager.inspect()
            owned = self.manager.drivers['comfyui'].owned_pids
            reason = self.manager.ownership_policy.running_reason(reading, owned, self.profile.required_vram_mib)
            if reason:
                from backend.app.resources.memory import MemoryAdmissionFailure, memory_state
                state = dict(memory_state(), vram_used_mib=reading.used_mib,
                             vram_total_mib=reading.total_mib,
                             required_vram_mib=self.profile.required_vram_mib,
                             owned_vram_used_mib=sum(p.used_mib for p in reading.processes if p.pid in owned))
                raise MemoryAdmissionFailure(reason, state, self.profile.runtime_ram_reserve_mib, 256, 'generation')
        except GPUUnsafe as error:
            self.failure = error

    def finish(self):
        if self.failure is not None:
            raise self.failure


def allocation_failure(state):
    """Classify only stable exception type tags, never return raw server errors."""
    for entry in state.get('messages', []):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2 or entry[0] != 'execution_error' or not isinstance(entry[1], dict):
            continue
        kind = entry[1].get('exception_type')
        reason = 'gpu_allocation_failed' if kind in {'torch.OutOfMemoryError','torch.cuda.OutOfMemoryError','comfy.model_management.OOM_EXCEPTION'} else 'allocation_failed' if kind in {'MemoryError','builtins.MemoryError'} else None
        if reason:
            from backend.app.resources.memory import MemoryAdmissionFailure, memory_state
            selected = _current.get()
            reserve = selected[0].runtime_ram_reserve_mib if selected else 0
            raise MemoryAdmissionFailure(reason, memory_state(), reserve, 256, 'generation')
