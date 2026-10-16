"""GPU/process definitions are trusted local HOST configuration, never model proposals."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import math
import tomllib

from backend.app.config import ROOT
from backend.app.resources.process import ProcessProfile
from backend.app.resources.ownership import ExternalIdentity, Ownership
from backend.app.tools.config import validate_location


@dataclass(frozen=True)
class ResourceSettings:
    inspect_gpu: bool = False
    max_idle_mib: int = 256
    profiles: tuple[ProcessProfile, ...] = ()
    release_timeout: float = 10
    queue_timeout: float = 60
    external_processes: tuple[ExternalIdentity, ...] = ()
    reserve_mib: int = 512
    idle_timeout_seconds: float = 600


def load_resources(path: Path | None = None) -> ResourceSettings:
    path = path or Path(os.environ.get("PITCH_CHAT_RESOURCES_CONFIG", ROOT / "config/resources.toml"))
    with path.open("rb") as file:
        config = tomllib.load(file)
    gpu = config.get("gpu", {})
    if set(config) - {"gpu", "profiles", "external_processes"} or set(gpu) - {"inspect", "max_idle_mib", "release_timeout", "queue_timeout", "reserve_mib", "idle_timeout_seconds"}:
        raise ValueError("Unknown resource policy field")
    def timeout(value):
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 600:
            raise ValueError("Resource timeout must be bounded")
        return float(value)
    release = timeout(gpu.get("release_timeout", 10))
    queue = timeout(gpu.get("queue_timeout", 60))
    idle = gpu.get("idle_timeout_seconds", 600)
    if type(idle) not in {int, float} or not math.isfinite(idle) or not 1 <= idle <= 86400:
        raise ValueError("Idle timeout must be between 1 and 86400 seconds")
    enabled, maximum = gpu.get("inspect", False), gpu.get("max_idle_mib", 256)
    if type(enabled) is not bool or type(maximum) is not int or not 0 <= maximum <= 1024:
        raise ValueError("Invalid GPU policy")
    profiles = []
    reserve = gpu.get("reserve_mib", 512)
    if type(reserve) is not int or not 256 <= reserve <= 4096:
        raise ValueError("Invalid GPU safety reserve")
    identities = []
    for entry in config.get("external_processes", []):
        if set(entry) != {"name", "pid", "start_ticks", "comm", "cgroup", "classification", "max_memory_mib"}:
            raise ValueError("External identity requires all reviewed fields")
        if not isinstance(entry["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", entry["name"]):
            raise ValueError("Invalid external logical ID")
        if any(type(entry[key]) is not int or entry[key] <= 0 for key in ("pid", "start_ticks", "max_memory_mib")):
            raise ValueError("Invalid external identity numbers")
        if any(not isinstance(entry[key], str) or not entry[key] for key in ("comm", "cgroup")):
            raise ValueError("Invalid external identity text")
        classification = Ownership(entry["classification"])
        if classification not in {Ownership.EXTERNAL_SHARED, Ownership.EXTERNAL_BLOCKING}:
            raise ValueError("External config cannot confer ownership")
        if entry["max_memory_mib"] > (1024 if classification == Ownership.EXTERNAL_SHARED else 12288):
            raise ValueError("External memory cap exceeds policy bound")
        identities.append(ExternalIdentity(**(entry | {"classification": classification})))
    if len(identities) > 64 or len({p.pid for p in identities}) != len(identities) or len({p.name for p in identities}) != len(identities):
        raise ValueError("Duplicate external identity")
    for name, entry in config.get("profiles", {}).items():
        if set(entry) - {"argv", "cwd", "health_url", "start_timeout", "stop_timeout", "device", "required_vram_mib", "required_ram_mib", "min_swap_free_mib", "runtime_ram_reserve_mib", "manual_start", "graceful_only", "diagnostics_path", "low_swap_start_ram_mib", "backend_ram_mib"}:
            raise ValueError("Unknown process profile field")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name):
            raise ValueError("Invalid profile logical ID")
        argv = entry["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise ValueError("Profile requires explicit nonempty argv")
        cwd = Path(entry["cwd"])
        if not cwd.is_absolute() or not Path(argv[0]).is_absolute():
            raise ValueError("Profile executable/cwd must be explicit absolute paths in private config")
        health = entry["health_url"]
        from urllib.parse import urlsplit
        parsed = urlsplit(health)
        validate_location(f"{parsed.scheme}://{parsed.netloc}")
        if not parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Profile health URL must have a fixed route")
        device = entry.get("device", "gpu")
        if device not in {"cpu", "gpu"}:
            raise ValueError("Invalid resource device")
        required = entry.get("required_vram_mib", 0)
        if type(required) is not int or not 0 <= required <= 12288 or (device == "cpu" and required):
            raise ValueError("Invalid profile VRAM budget")
        memory_fields = {k: entry.get(k, 0) for k in ("required_ram_mib", "min_swap_free_mib", "runtime_ram_reserve_mib")}
        if any(type(v) is not int or not 0 <= v <= 1048576 for v in memory_fields.values()):
            raise ValueError("Invalid memory budget")
        manual, graceful = entry.get("manual_start", False), entry.get("graceful_only", False)
        if type(manual) is not bool or type(graceful) is not bool:
            raise ValueError("Invalid process safety options")
        if name == "strata" and (device != "gpu" or not manual or not graceful or
                memory_fields["required_ram_mib"] < 40960 or memory_fields["min_swap_free_mib"] < 512 or
                memory_fields["runtime_ram_reserve_mib"] < 5120 or not required):
            raise ValueError("Strata requires explicit manual start and reviewed GPU/RAM/Swap safety budgets")
        physical = entry.get('low_swap_start_ram_mib', 0)
        if type(physical) is not int or not 0 <= physical <= 1048576:
            raise ValueError('Invalid physical RAM budget')
        if physical and (name not in {'strata', 'comfyui', 'vision'} or (name == 'strata' and physical < 49152) or
                         physical < memory_fields['required_ram_mib'] + memory_fields['runtime_ram_reserve_mib']):
            raise ValueError('Physical RAM admission requires reviewed Strata peak plus runtime reserve')
        if name == 'vision' and (not physical or not graceful or device != 'gpu' or required < 10240
                or memory_fields['required_ram_mib'] < 16384 or memory_fields['runtime_ram_reserve_mib'] < 2048):
            raise ValueError('Vision requires explicit conservative GPU/RAM budgets and graceful stop')
        diagnostics_path = Path(entry["diagnostics_path"]) if "diagnostics_path" in entry else None
        if diagnostics_path is not None and (name != "strata" or not diagnostics_path.is_absolute()):
            raise ValueError("Diagnostics are allowed only at an absolute private Strata path")
        budgets = entry.get('backend_ram_mib', {})
        if (not isinstance(budgets, dict) or budgets and (name != 'comfyui' or not physical or not graceful or device != 'gpu')
                or any(k not in {'sdxl', 'qwen-image', 'flux-klein', 'flux-klein-edit', 'minimax-h3', 'ltx-video', 'minimax-h3-i2v'} or type(v) is not int or not 8192 <= v <= 1048576 for k, v in budgets.items())
                or any(budgets.get(k,43008) < 43008 for k in ('ltx-video','minimax-h3-i2v'))):
            raise ValueError('Invalid reviewed ComfyUI backend memory budgets')
        profiles.append(ProcessProfile(name, tuple(argv), cwd, health,
                        timeout(entry.get("start_timeout", 60)), timeout(entry.get("stop_timeout", 10)), device, required, **memory_fields, manual_start=manual, graceful_only=graceful,
                        diagnostics_path=diagnostics_path, low_swap_start_ram_mib=physical, backend_ram_mib=tuple(sorted(budgets.items()))))
    if (profiles or identities) and not enabled:
        raise ValueError("Profiles require GPU inspection")
    return ResourceSettings(enabled, maximum, tuple(profiles), release, queue, tuple(identities), reserve, float(idle))
