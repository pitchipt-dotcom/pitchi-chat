"""Harness coordination; real GPU/process authority delegates to GPUManager."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class ResourceMode(str, Enum):
    IDLE = "IDLE"
    CHAT = "CHAT"
    IMAGE = "IMAGE"
    TTS = "TTS"
    WORK = "WORK"
    JP_REWRITE = "JP_REWRITE"


class Lifecycle(str, Enum):
    IDLE = "IDLE"
    QUEUED = "QUEUED"
    LOADING = "LOADING"
    RUNNING = "RUNNING"
    UNLOADING = "UNLOADING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ActiveJob:
    id: str
    mode: str
    model: str | None
    tool: str | None
    started_at: str


class ResourceCoordinator:
    def __init__(self, manager=None):
        self.manager = manager
        self._lock = asyncio.Lock()
        self._active: ActiveJob | None = None
        self._waiting = 0
        self._last_failure: str | None = None

    def snapshot(self) -> dict:
        active = self._active
        reading = self.manager.reading if self.manager else None
        lifecycle = (Lifecycle.RUNNING if active else Lifecycle.QUEUED if self._waiting
                     else Lifecycle.FAILED if self._last_failure else Lifecycle.IDLE)
        return {
            "mode": active.mode if active else ResourceMode.IDLE.value,
            "lifecycle": lifecycle.value,
            "active_job": asdict(active) if active else None,
            "waiting_jobs": self._waiting,
            "running_model": active.model if active else None,
            "running_tool": active.tool if active else None,
            "loaded_model": self.manager.loaded if self.manager else None,
            "last_failure": self._last_failure,
            "scope": "host_single_worker",
            "gpu_telemetry": "available" if reading else "unavailable",
            "process_management": bool(self.manager and self.manager.drivers),
            "external_gpu_workloads": "unmanaged",
        }

    @asynccontextmanager
    async def job(self, mode: ResourceMode, *, model: str | None = None, tool: str | None = None,
                  profile: str | None = None, remote: bool = False, keep_ready: bool = False, allow_start: bool = False):
        retained = self.manager.usage(profile) if self.manager and profile in self.manager.drivers else nullcontext()
        async with retained:
            async with self._job(mode, model=model, tool=tool, profile=profile, remote=remote,
                                 keep_ready=keep_ready, allow_start=allow_start) as job:
                yield job

    @asynccontextmanager
    async def _job(self, mode: ResourceMode, *, model: str | None = None, tool: str | None = None,
                   profile: str | None = None, remote: bool = False, keep_ready: bool = False, allow_start: bool = False):
        mode = ResourceMode(mode)
        if mode is ResourceMode.IDLE:
            raise ValueError("IDLE cannot acquire an active job")
        self._waiting += 1
        try:
            if self.manager:
                try:
                    await asyncio.wait_for(self._lock.acquire(), self.manager.queue_timeout)
                except TimeoutError:
                    from backend.app.resources.manager import GPUUnsafe
                    raise GPUUnsafe("queue_timeout") from None
            else:
                await self._lock.acquire()
        finally:
            # Includes cancellation while queued; no lock is released by a waiter.
            self._waiting -= 1
        self._last_failure = None
        try:
            from backend.app.jobs.context import current_job_id
            self._active = ActiveJob(current_job_id.get() or str(uuid4()), mode.value, model, tool,
                                     datetime.now(timezone.utc).isoformat())
            if profile:
                from backend.app.resources.manager import GPUUnsafe
                if self.manager is None:
                    raise GPUUnsafe("profile_unmanaged")
                driver = self.manager.drivers.get(profile)
                cpu = getattr(getattr(driver, "profile", None), "device", "gpu") == "cpu"
                if cpu and mode is not ResourceMode.CHAT:
                    raise GPUUnsafe("gpu_profile_required")
                lease = self.manager.cpu_lease(profile) if cpu else self.manager.lease(
                    profile, self._active.id, remote=remote, keep_ready=keep_ready, allow_start=allow_start)
                async with lease:
                    yield self._active
            else:
                yield self._active
        except asyncio.CancelledError:
            # Explicit task cancellation is distinct from UI disconnect policy.
            raise
        except Exception:
            # Store a classification only, never model payloads/credentials/errors.
            self._last_failure = "job_failed"
            raise
        finally:
            self._active = None
            self._lock.release()
