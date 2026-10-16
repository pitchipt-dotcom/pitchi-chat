"""Read-only Linux RAM/Swap admission; no cache dropping, swapoff or process control."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from backend.app.resources.manager import GPUUnsafe


class MemoryAdmissionFailure(GPUUnsafe):
    def __init__(self, reason, state, required_ram_mib, min_swap_free_mib, stage):
        super().__init__(reason)
        logging.getLogger(__name__).warning(
            "memory_admission_rejected stage=%s reason=%s MemAvailable=%sMiB SwapFree=%sMiB required_ram=%sMiB min_swap_free=%sMiB",
            stage, reason, state['ram_available_mib'], state['swap_free_mib'], required_ram_mib, min_swap_free_mib)
        self.memory_details = dict(state, reason=reason, stage=stage,
                                   required_ram_mib=required_ram_mib,
                                   min_swap_free_mib=min_swap_free_mib)


def memory_state(path=Path("/proc/meminfo")):
    try:
        fields = {line.split(":", 1)[0]: int(line.split()[1]) // 1024
                  for line in path.read_text().splitlines() if ":" in line}
        return {"ram_available_mib": fields["MemAvailable"], "swap_free_mib": fields["SwapFree"],
                "swap_used_mib": fields["SwapTotal"] - fields["SwapFree"]}
    except (OSError, KeyError, ValueError, IndexError):
        raise GPUUnsafe("memory_telemetry_unavailable") from None


def physical_ram_safety(required_ram_mib, *, pressure_window="avg10"):
    """Fail closed on pressure or finite ancestor cgroup headroom; no privilege."""
    try:
        pressure = {}
        for line in Path('/proc/pressure/memory').read_text().splitlines():
            parts = line.split()
            pressure[parts[0]] = float(dict(part.split('=') for part in parts[1:])[pressure_window])
        if set(pressure) != {'some', 'full'} or any(not 0 <= v <= 100 for v in pressure.values()):
            raise ValueError('invalid pressure')
        if pressure['some'] > 5 or pressure['full'] > 1:
            return 'memory_pressure'
        entries = Path('/proc/self/cgroup').read_text().splitlines()
        unified = next(line[3:] for line in entries if line.startswith('0::'))
        base = Path('/sys/fs/cgroup')
        current = base / unified.lstrip('/')
        if '..' in Path(unified).parts or not current.is_relative_to(base):
            raise ValueError('invalid cgroup')
        while True:
            for limit_name in ('memory.max', 'memory.high'):
                limit_file = current / limit_name
                # The hierarchy root may omit these; child controllers may not.
                if current == base and not limit_file.exists():
                    continue
                limit = limit_file.read_text().strip()
                if limit != 'max':
                    used = int((current / 'memory.current').read_text())
                    if int(limit) - used < required_ram_mib * 1024 * 1024:
                        return 'insufficient_cgroup_ram'
            if current == base:
                break
            current = current.parent
        return None
    except (OSError, ValueError, KeyError, StopIteration, IndexError):
        return 'memory_safety_telemetry_unavailable'


def check_memory(required_ram_mib=0, min_swap_free_mib=0, *, stage="preflight", low_swap_ram_mib=0):
    state = memory_state()
    if state["ram_available_mib"] < required_ram_mib:
        raise MemoryAdmissionFailure("insufficient_ram", state, required_ram_mib, min_swap_free_mib, stage)
    low_swap = state['swap_free_mib'] < min_swap_free_mib
    if low_swap and not low_swap_ram_mib:
        raise MemoryAdmissionFailure("insufficient_swap_reserve", state, required_ram_mib, min_swap_free_mib, stage)
    if low_swap_ram_mib:
        budget = max(required_ram_mib, low_swap_ram_mib) if low_swap else required_ram_mib
        state = dict(state, admission_mode='physical_ram' if low_swap else 'swap_reserve',
                     admission_ram_mib=budget)
        if state['ram_available_mib'] < budget:
            raise MemoryAdmissionFailure('insufficient_ram', state, budget, min_swap_free_mib, stage)
        reason = physical_ram_safety(budget, pressure_window="avg10" if stage == "preflight" else "avg60")
        if reason:
            raise MemoryAdmissionFailure(reason, state, budget, min_swap_free_mib, stage)
    return state


def check_profile_memory(profile, *, stage='preflight'):
    from backend.app.resources.media_memory import effective_profile
    profile = effective_profile(profile)
    reviewed = getattr(profile, 'low_swap_start_ram_mib', 0)
    if stage == 'preflight':
        return check_memory(profile.required_ram_mib, profile.min_swap_free_mib,
                            stage=stage, low_swap_ram_mib=reviewed)
    reserve = profile.runtime_ram_reserve_mib
    return check_memory(reserve, 256, stage=stage,
                        low_swap_ram_mib=reserve if reviewed else 0)


_stream_profile = ContextVar('host_stream_memory_profile', default=None)


@contextmanager
def scoped_stream_memory(profile):
    token = _stream_profile.set(profile)
    try:
        yield
    finally:
        _stream_profile.reset(token)


def check_stream_memory(required_ram_mib):
    profile = _stream_profile.get()
    if profile is not None:
        return check_profile_memory(profile, stage='generation')
    # Non-managed adapter users retain strict swap admission, correctly staged.
    return check_memory(required_ram_mib, 256, stage='generation')
