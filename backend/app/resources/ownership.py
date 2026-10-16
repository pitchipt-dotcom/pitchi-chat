"""Reviewed external identities confer coexistence, NEVER process ownership."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Ownership(str, Enum):
    HOST_OWNED = "HOST_OWNED"
    EXTERNAL_SHARED = "EXTERNAL_SHARED"
    EXTERNAL_BLOCKING = "EXTERNAL_BLOCKING"
    STALE_OR_UNKNOWN = "STALE_OR_UNKNOWN"


class ExternalActivity(str, Enum):
    """Separate from ownership. An idle report does NOT grant a cooperative lease."""
    UNVERIFIED = "UNVERIFIED"
    IDLE_REPORTED = "IDLE_REPORTED"
    ACTIVE_REPORTED = "ACTIVE_REPORTED"


@dataclass(frozen=True)
class GPUProcess:
    pid: int
    used_mib: int
    kind: str
    start_ticks: int = 0
    comm: str = ""
    cgroup: str = ""


@dataclass(frozen=True)
class ExternalIdentity:
    name: str
    pid: int
    start_ticks: int
    comm: str
    cgroup: str
    classification: Ownership
    max_memory_mib: int
    activity: ExternalActivity = ExternalActivity.UNVERIFIED

    def matches(self, process):
        return (self.pid, self.start_ticks, self.comm, self.cgroup) == (
            process.pid, process.start_ticks, process.comm, process.cgroup)


def process_identity(pid: int, used_mib: int, kind: str, proc_root: Path = Path("/proc")):
    """Do not read argv/environ: no private content enters GPU telemetry/audit."""
    try:
        root = proc_root / str(pid)
        before = root.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
        comm = root.joinpath("comm").read_text().strip()
        cgroup = root.joinpath("cgroup").read_text().strip()
        after = root.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
        if before != after:
            raise ValueError
        return GPUProcess(pid, used_mib, kind, int(before), comm, cgroup)
    except (OSError, ValueError, IndexError):
        return GPUProcess(pid, used_mib, kind)


class OwnershipPolicy:
    def __init__(self, identities=(), reserve_mib=512):
        self.identities = tuple(identities)
        self.reserve_mib = reserve_mib

    def classify(self, reading, owned):
        processes = {p.pid: p for p in reading.processes}
        result = {}
        for pid in reading.pids:
            if pid in owned:
                result[pid] = (Ownership.HOST_OWNED, None)
                continue
            process = processes.get(pid)
            match = next((rule for rule in self.identities if process and rule.matches(process)), None)
            result[pid] = (match.classification, match) if match else (Ownership.STALE_OR_UNKNOWN, None)
        return result

    def external_reason(self, reading, owned):
        processes = {p.pid: p for p in reading.processes}
        blocked = False
        for pid, (classification, rule) in self.classify(reading, owned).items():
            if classification == Ownership.HOST_OWNED:
                continue
            if classification == Ownership.STALE_OR_UNKNOWN:
                return "unknown_gpu_process"
            if classification == Ownership.EXTERNAL_BLOCKING:
                blocked = True
                continue
            if processes[pid].used_mib < 0 or processes[pid].used_mib > rule.max_memory_mib:
                return "shared_memory_cap_exceeded"
        return "external_gpu_workload" if blocked else None

    def admission_reason(self, reading, owned, required_mib):
        reason = self.external_reason(reading, owned)
        if reason:
            return reason
        shared = reading.pids - owned
        if shared and not required_mib:
            return "profile_memory_budget_missing"
        if required_mib:
            if reading.total_mib is None:
                return "memory_capacity_unknown"
            # Reserved shared caps account for predictable growth, not just the current sample.
            growth = sum(rule.max_memory_mib - p.used_mib
                for p in reading.processes
                for classification, rule in [self.classify(reading, owned).get(p.pid, (None, None))]
                if classification == Ownership.EXTERNAL_SHARED)
            if reading.total_mib - reading.used_mib < required_mib + self.reserve_mib + growth:
                return "insufficient_vram"
        return None

    def running_reason(self, reading, owned, required_mib):
        reason = self.external_reason(reading, owned)
        if reason:
            return reason
        if reading.pids - owned and not required_mib:
            return "profile_memory_budget_missing"
        if required_mib:
            if reading.total_mib is None:
                return "memory_capacity_unknown"
            if reading.total_mib - reading.used_mib < self.reserve_mib:
                return "insufficient_vram"
            if sum(p.used_mib for p in reading.processes if p.pid in owned) > required_mib:
                return "profile_memory_budget_exceeded"
        return None

    def released(self, reading, owned, idle_mib):
        if reading.pids & owned or self.external_reason(reading, frozenset()):
            return False
        background = sum(p.used_mib for p in reading.processes)
        return reading.used_mib <= idle_mib + background
