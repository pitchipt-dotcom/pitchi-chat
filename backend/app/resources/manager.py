"""Single-GPU ownership with persistent quarantine and owned-process-only stops."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import time
from uuid import uuid4
from pathlib import Path
from typing import Protocol
import xml.etree.ElementTree as ET

from backend.app.resources.ownership import OwnershipPolicy, Ownership, process_identity
from backend.app.resources.diagnostics import StartDiagnostics, current_start_id, safety_category

from backend.app.storage.sqlite import HistoryStore


class GPUPhase(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class GPUUnsafe(Exception):
    """Classification only. Never expose commands, paths, process names or payloads."""


@dataclass(frozen=True)
class GPUReading:
    used_mib: int
    pids: frozenset[int]
    total_mib: int | None = None
    processes: tuple = ()


class Inspector(Protocol):
    async def read(self) -> GPUReading: ...


class Driver(Protocol):
    @property
    def owned_pids(self) -> frozenset[int]: ...
    async def start(self): ...
    async def stop(self): ...


class NvidiaInspector:
    def __init__(self, index: int = 0, timeout: float = 3):
        self.index, self.timeout = index, timeout

    async def _query(self, query: str) -> str:
        process = await asyncio.create_subprocess_exec("nvidia-smi", "-i", str(self.index), query,
                    "-x", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self.timeout)
            if process.returncode != 0 or len(output) > 262144:
                raise GPUUnsafe("telemetry_unavailable")
            return output.decode("ascii").strip()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def read(self) -> GPUReading:
        try:
            root = ET.fromstring(await self._query("-q"))
            if len(root.findall("gpu")) != 1:
                raise ValueError
            gpu = root.find("gpu")
            if gpu.find("processes") is None:
                raise ValueError
            def memory(text):
                if not text or not text.endswith(" MiB"):
                    raise ValueError
                return int(text.removesuffix(" MiB"))
            used = memory(gpu.findtext("fb_memory_usage/used"))
            total = memory(gpu.findtext("fb_memory_usage/total"))
            processes = tuple(process_identity(int(p.findtext("pid")), memory(p.findtext("used_memory")),
                p.findtext("type", "unknown")) for p in gpu.findall("processes/process_info"))
            reading = GPUReading(used, frozenset(p.pid for p in processes), total, processes)
            if (not 0 <= reading.used_mib <= total or total <= 0 or
                    any(p.pid <= 0 or p.used_mib < 0 for p in processes) or len(reading.pids) != len(processes)):
                raise ValueError
            return reading
        except (OSError, ValueError, TimeoutError, ET.ParseError, AttributeError, TypeError):
            raise GPUUnsafe("telemetry_unavailable") from None


class GPUManager:
    def __init__(self, store: HistoryStore, inspector: Inspector, drivers: dict[str, Driver] | None = None,
                 *, max_idle_mib: int = 256, release_timeout: float = 10, queue_timeout: float = 60,
                 host_lock_path: Path | None = None, ownership_policy: OwnershipPolicy | None = None,
                 idle_timeout: float = 600, clock=time.monotonic):
        self.store, self.inspector = store, inspector
        self.drivers = dict(drivers or {})
        self.diagnostics = {}
        for name, driver in self.drivers.items():
            path = getattr(getattr(driver, "profile", None), "diagnostics_path", None)
            if path is not None:
                diagnostics = StartDiagnostics(path)
                self.diagnostics[name] = diagnostics
                driver.diagnostics = diagnostics
                if hasattr(driver, "gpu_reader"):
                    driver.gpu_reader = self.inspect
        self.ownership_policy = ownership_policy or OwnershipPolicy()
        self.max_idle_mib, self.release_timeout, self.queue_timeout = max_idle_mib, release_timeout, queue_timeout
        self._lock = asyncio.Lock()
        self.idle_timeout, self.clock = idle_timeout, clock
        self._uses = {}
        self._last_use = clock()
        self._maintenance = None
        self.last_release = None
        self.waiting = 0
        self.reading: GPUReading | None = None
        self.loaded: str | None = None
        self.phase = GPUPhase.STOPPED
        self.owner: str | None = None
        self.job_id: str | None = None
        self.error: str | None = None
        self._file = None
        self.host_lock_path = host_lock_path
        self._global_file = None
        self.closed = False
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gpu_ownership (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT, phase TEXT NOT NULL,
                job_id TEXT, error TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("INSERT OR IGNORE INTO gpu_ownership(singleton,phase) VALUES(1,'STOPPED')")
            db.execute("""CREATE TABLE IF NOT EXISTS gpu_events (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                owner TEXT, phase TEXT NOT NULL, job_id TEXT, error TEXT)""")

    def open(self):
        """One live HOST per database. Recovered PIDs are never adopted or signalled."""
        path = self.store.path.with_suffix(".host.lock")
        self._file = path.open("a+b")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise GPUUnsafe("host_already_running") from None
        if self.host_lock_path is not None:
            try:
                self._global_file = self.host_lock_path.open("a+b")
                fcntl.flock(self._global_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                self._file.close()
                self._file = None
                if self._global_file is not None:
                    self._global_file.close()
                    self._global_file = None
                raise GPUUnsafe("gpu_host_already_running") from None
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM gpu_ownership WHERE singleton=1").fetchone()
        self.owner, self.job_id = row["owner"], row["job_id"]
        if row["phase"] != GPUPhase.STOPPED.value:
            self._state(GPUPhase.ERROR, self.owner, self.job_id, "recovery_required")

    def _state(self, phase: GPUPhase, owner: str | None = None, job_id: str | None = None, error: str | None = None):
        if self.closed:
            raise GPUUnsafe("host_closed")
        self.phase, self.owner, self.job_id, self.error = phase, owner, job_id, error
        with self.store.connection() as db:
            db.execute("UPDATE gpu_ownership SET owner=?,phase=?,job_id=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",
                       (owner, phase.value, job_id, error))
            db.execute("INSERT INTO gpu_events(owner,phase,job_id,error) VALUES(?,?,?,?)",
                       (owner, phase.value, job_id, error))

    async def inspect(self):
        self.reading = await self.inspector.read()
        return self.reading

    def retain(self, owner, *, ttl=None):
        """A queued request or a whole job retains use, independently of generation."""
        if self.closed or self._file is None or owner not in self.drivers:
            raise GPUUnsafe("profile_unmanaged")
        if ttl is not None and not 1 <= ttl <= 3600:
            raise GPUUnsafe("invalid_usage_ttl")
        self._expire_uses()
        if len(self._uses) >= 128:
            raise GPUUnsafe("usage_queue_full")
        token = str(uuid4())
        self._uses[token] = (owner, self.clock() + ttl if ttl is not None else None)
        return token

    def renew(self, token, ttl=120):
        self._expire_uses()
        if token not in self._uses or not 1 <= ttl <= 3600:
            raise GPUUnsafe("usage_expired")
        owner, expires = self._uses[token]
        if expires is None:
            raise GPUUnsafe("usage_not_renewable")
        self._uses[token] = (owner, self.clock() + ttl)

    def release_use(self, token):
        if self._uses.pop(token, None) is not None:
            self._last_use = self.clock()

    def _expire_uses(self):
        for token, (_, expiry) in list(self._uses.items()):
            if expiry is not None and expiry <= self.clock():
                self.release_use(token)

    def usage_count(self, owner=None):
        self._expire_uses()
        return sum(owner is None or model == owner for model, _ in self._uses.values())

    @asynccontextmanager
    async def usage(self, owner):
        # Internal scoped jobs never expire between phases. Cancellation releases
        # in finally; external clients must renew finite reservations instead.
        token = self.retain(owner)
        try:
            yield token
        finally:
            self.release_use(token)

    def start_maintenance(self):
        if self._maintenance is not None:
            raise GPUUnsafe("maintenance_already_started")
        self._maintenance = asyncio.create_task(self._maintain())

    async def _maintain(self):
        while True:
            await asyncio.sleep(min(5, self.idle_timeout / 4))
            try:
                await self.maintenance_once()
            except (GPUUnsafe, TimeoutError, OSError):
                # Failed release already quarantines ownership; never force-kill,
                # retry a quarantined model, or expose private exception text.
                pass

    async def maintenance_once(self):
        if self.closed or self._lock.locked() or self.waiting or self.usage_count():
            return
        if self.phase != GPUPhase.READY or self.loaded is None:
            return
        driver = self.drivers[self.loaded]
        process = getattr(driver, "process", None)
        if process is not None and process.returncode is not None:
            self._state(GPUPhase.ERROR, self.loaded, error="owned_process_exited")
            return
        if hasattr(driver, "healthy") and not await driver.healthy():
            self._state(GPUPhase.ERROR, self.loaded, error="owned_process_unhealthy")
            return
        # A request may arrive while health is awaited. stop_idle rechecks use.
        if self.clock() - self._last_use >= self.idle_timeout:
            await self.stop_idle(self.loaded)

    def _diagnostic(self, owner, event, reason=None, **fields):
        diagnostics = self.diagnostics.get(owner)
        attempt_id = current_start_id.get()
        if diagnostics is None or attempt_id is None:
            return
        if reason is not None:
            fields.update(reason=reason, category=safety_category(reason))
        try:
            diagnostics.record(event, start_id=attempt_id, **fields)
        except OSError:
            # OwnedProcess performs a strict write check before spawning; a failure
            # while recording an earlier refusal must not interrupt lease cleanup.
            return

    def record_start_request(self, owner):
        driver = self.drivers.get(owner)
        if driver is None:
            return
        profile = getattr(driver, "profile", None)
        if profile is not None:
            self._diagnostic(owner, "start_requested", argv=profile.argv, cwd=str(profile.cwd),
                required_ram_mib=profile.required_ram_mib, min_swap_free_mib=profile.min_swap_free_mib,
                runtime_ram_reserve_mib=profile.runtime_ram_reserve_mib,
                required_vram_mib=profile.required_vram_mib, vram_reserve_mib=self.ownership_policy.reserve_mib)

    def blocking_owners(self):
        """Reviewed logical labels only, never process name, command or PID."""
        if self.reading is None:
            return ()
        owned = frozenset().union(*(d.owned_pids for d in self.drivers.values()))
        classes = self.ownership_policy.classify(self.reading, owned)
        return tuple(sorted({rule.name for kind, rule in classes.values() if kind == Ownership.EXTERNAL_BLOCKING and rule}))

    def snapshot(self):
        owned = frozenset().union(*(driver.owned_pids for driver in self.drivers.values()))
        foreign = self.reading.pids - owned if self.reading else None
        classes = self.ownership_policy.classify(self.reading, owned) if self.reading else {}
        return {"state": self.phase.value, "current_owner": self.owner or ("external_unmanaged" if foreign else None),
                "process_classifications": {kind.value: sum(c == kind for c, _ in classes.values()) for kind in Ownership},
                "external_conflict": self.ownership_policy.external_reason(self.reading, owned) if self.reading else None,
                "blocking_owners": self.blocking_owners(),
                "loaded_model": self.loaded, "active_job": self.job_id, "queued": self.waiting,
                "retained_uses": self.usage_count(), "idle_timeout_seconds": self.idle_timeout,
                "idle_seconds": max(0, self.clock() - self._last_use), "last_release": self.last_release,
                "memory_used_mib": self.reading.used_mib if self.reading else None,
                "foreign_process_count": len(foreign) if foreign is not None else None,
                "error": self.error, "profiles": sorted(self.drivers), "exclusive_scope": "host_owned_only"}

    async def _quiescent(self, stopped_pids=frozenset()):
        async with asyncio.timeout(self.release_timeout):
            while True:
                reading = await self.inspect()
                if self.ownership_policy.released(reading, stopped_pids, self.max_idle_mib):
                    return
                await asyncio.sleep(0.05)

    async def _stop_loaded(self):
        if self.loaded is None:
            return
        driver = self.drivers[self.loaded]
        if hasattr(driver, "idle") and not await driver.idle():
            raise GPUUnsafe("model_in_use")
        self.last_release = None
        self._state(GPUPhase.STOPPING, self.loaded, self.job_id)
        stopped_pids = self.drivers[self.loaded].owned_pids
        await self.drivers[self.loaded].stop()
        await self._quiescent(stopped_pids)
        from backend.app.resources.memory import memory_state
        self.last_release = {"process_exit_confirmed": True, "vram_release_confirmed": True,
                             **memory_state()}
        if getattr(driver, 'last_unload', None):
            self.last_release['comfy_unload'] = driver.last_unload
        self.loaded = None

    async def preflight(self, owner):
        driver = self.drivers.get(owner)
        if driver is None:
            raise GPUUnsafe("profile_unmanaged")
        profile = getattr(driver, "profile", None)
        if self.loaded != owner and getattr(profile, "manual_start", False) and self.loaded is not None:
            self._diagnostic(owner, "safety_gate", gate="gpu_ownership", result="rejected", reason="other_model_loaded")
            raise GPUUnsafe("other_model_loaded")
        if self.loaded != owner and getattr(profile, "required_ram_mib", 0):
            from backend.app.resources.memory import check_profile_memory
            try:
                memory = check_profile_memory(profile)
                self._diagnostic(owner, "safety_gate", gate="ram_swap_preflight", result="passed", **memory,
                                 required_ram_mib=profile.required_ram_mib, min_swap_free_mib=profile.min_swap_free_mib)
            except GPUUnsafe as error:
                self._diagnostic(owner, "safety_gate", gate="ram_swap_preflight", result="rejected", reason=str(error),
                                 required_ram_mib=profile.required_ram_mib, min_swap_free_mib=profile.min_swap_free_mib)
                raise
        try:
            reading = await self.inspect()
        except GPUUnsafe as error:
            self._diagnostic(owner, "safety_gate", gate="vram_preflight", result="rejected", reason=str(error))
            raise
        owned = frozenset().union(*(d.owned_pids for d in self.drivers.values()))
        required = getattr(getattr(driver, "profile", None), "required_vram_mib", 0)
        reason = self.ownership_policy.admission_reason(reading, owned, required if self.loaded != owner else 0)
        # An already-loaded profile must still have a declared budget when sharing.
        if self.loaded == owner:
            reason = self.ownership_policy.running_reason(reading, owned, required)
        if reason:
            self._diagnostic(owner, "safety_gate", gate="vram_preflight", result="rejected", reason=reason,
                             vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib,
                             required_vram_mib=required, reserve_mib=self.ownership_policy.reserve_mib)
            raise GPUUnsafe(reason)
        if self.loaded is None and not self.ownership_policy.released(reading, frozenset(), self.max_idle_mib):
            self._diagnostic(owner, "safety_gate", gate="vram_release", result="rejected", reason="vram_release_unverified",
                             vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib)
            raise GPUUnsafe("vram_release_unverified")
        self._diagnostic(owner, "safety_gate", gate="vram_preflight", result="passed",
                         vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib,
                         required_vram_mib=required, reserve_mib=self.ownership_policy.reserve_mib)

    @asynccontextmanager
    async def cpu_lease(self, owner: str):
        """Explicit CPU-only model under Harness lock; never grants a GPU Tool lease."""
        driver = self.drivers.get(owner)
        if self._file is None or driver is None or driver.profile.device != "cpu":
            raise GPUUnsafe("cpu_profile_unmanaged")
        try:
            await driver.start()
            if driver.owned_pids & (await self.inspect()).pids:
                raise GPUUnsafe("cpu_profile_used_gpu")
            yield
            if driver.owned_pids & (await self.inspect()).pids:
                raise GPUUnsafe("cpu_profile_used_gpu")
        finally:
            await driver.stop()

    @asynccontextmanager
    async def lease(self, owner: str, job_id: str, *, remote: bool = False, keep_ready: bool = False, allow_start: bool = False):
        if self._file is None or owner not in self.drivers:
            raise GPUUnsafe("profile_unmanaged")
        if getattr(getattr(self.drivers[owner], "profile", None), "device", "gpu") != "gpu":
            raise GPUUnsafe("gpu_profile_required")
        profile = getattr(self.drivers[owner], "profile", None)
        if getattr(profile, "manual_start", False) and self.loaded != owner and not allow_start:
            self._diagnostic(owner, "safety_gate", gate="gpu_ownership", result="rejected", reason="manual_start_required")
            raise GPUUnsafe("manual_start_required")
        if getattr(profile, "manual_start", False) and self.loaded not in {None, owner}:
            self._diagnostic(owner, "safety_gate", gate="gpu_ownership", result="rejected", reason="other_model_loaded")
            raise GPUUnsafe("other_model_loaded")
        self.waiting += 1
        acquired = False
        try:
            async with asyncio.timeout(self.queue_timeout):
                await self._lock.acquire()
                acquired = True
        except TimeoutError:
            self._diagnostic(owner, "safety_gate", gate="gpu_ownership", result="rejected", reason="queue_timeout")
            raise GPUUnsafe("queue_timeout") from None
        finally:
            self.waiting -= 1
        # Admission rejection under the lock must not clean up another READY owner.
        if getattr(profile, "manual_start", False) and (
                self.loaded not in {None, owner} or self.loaded != owner and not allow_start):
            self._lock.release()
            reason = "other_model_loaded" if self.loaded is not None else "manual_start_required"
            self._diagnostic(owner, "safety_gate", gate="gpu_ownership", result="rejected", reason=reason)
            raise GPUUnsafe(reason)
        if self.loaded not in {None, owner} and self.usage_count(self.loaded):
            self._lock.release()
            raise GPUUnsafe("model_in_use")
        dispatched = False
        try:
            from backend.app.jobs.context import media_event
            media_event("STRATA_STOPPING")
            if self.phase == GPUPhase.ERROR:
                raise GPUUnsafe("recovery_required")
            if self.loaded is None:
                media_event("RESOURCE_CHECK")
                await self.preflight(owner)  # Refuse before creating a child or claiming ownership.
            if self.loaded != owner:
                await self._stop_loaded()
                await self._quiescent()
                media_event("RESOURCE_CHECK", **self.last_release if self.last_release else {})
                await self.preflight(owner)
                media_event("COMFY_STARTING")
                self._state(GPUPhase.STARTING, owner, job_id)
                self.loaded = owner  # Record ownership before a partially successful start.
                self._diagnostic(owner, "engine_start", result="begin")
                try:
                    await self.drivers[owner].start()
                except GPUUnsafe as error:
                    self._diagnostic(owner, "engine_start", result="failed", reason=str(error))
                    raise
                self._diagnostic(owner, "engine_start", result="ready")
            reading = await self.inspect()
            process = getattr(self.drivers[owner], "process", None)
            if process is not None and process.returncode is not None:
                self._state(GPUPhase.ERROR, owner, job_id, "owned_process_exited")
                raise GPUUnsafe("owned_process_exited")
            if hasattr(self.drivers[owner], "healthy") and not await self.drivers[owner].healthy():
                self._state(GPUPhase.ERROR, owner, job_id, "owned_process_unhealthy")
                raise GPUUnsafe("owned_process_unhealthy")
            required = getattr(getattr(self.drivers[owner], "profile", None), "required_vram_mib", 0)
            reason = self.ownership_policy.running_reason(reading, self.drivers[owner].owned_pids, required)
            if reason:
                self._diagnostic(owner, "safety_gate", gate="vram_runtime", result="rejected", reason=reason,
                                 vram_used_mib=reading.used_mib, vram_total_mib=reading.total_mib,
                                 required_vram_mib=required, reserve_mib=self.ownership_policy.reserve_mib)
                raise GPUUnsafe(reason)
            if getattr(profile, "runtime_ram_reserve_mib", 0):
                from backend.app.resources.memory import check_profile_memory
                try:
                    memory = check_profile_memory(profile, stage="runtime")
                    self._diagnostic(owner, "safety_gate", gate="ram_swap_runtime", result="passed", **memory,
                                     required_ram_mib=profile.runtime_ram_reserve_mib, min_swap_free_mib=256)
                except GPUUnsafe as error:
                    self._diagnostic(owner, "safety_gate", gate="ram_swap_runtime", result="rejected", reason=str(error),
                                     required_ram_mib=profile.runtime_ram_reserve_mib, min_swap_free_mib=256)
                    raise
            self._state(GPUPhase.READY, owner, job_id)
            self._state(GPUPhase.BUSY, owner, job_id)
            dispatched = True
            yield
            reason = self.ownership_policy.running_reason(await self.inspect(), self.drivers[owner].owned_pids, required)
            if reason:
                raise GPUUnsafe(reason)
            if keep_ready:
                self._last_use = self.clock()
                self._state(GPUPhase.READY, owner)
            else:
                await self._stop_loaded()
                await self._quiescent()
                self._state(GPUPhase.STOPPED)
        except BaseException as error:

            if isinstance(error, GPUUnsafe):
                self._diagnostic(owner, "safety_rejected", stage="lifecycle", reason=str(error))
            # Uncertain remote side effects are never erased by releasing a Python lock.
            if self.phase == GPUPhase.ERROR:
                raise
            if self.loaded is None and self.phase == GPUPhase.STOPPED:
                raise  # Admission rejection is not an uncertain executed job.
            if remote and dispatched:
                self._state(GPUPhase.ERROR, owner, job_id, "remote_recovery_required")
            elif owner == "strata" and dispatched and keep_ready and not isinstance(error, GPUUnsafe):
                # A cancelled/failed client closes its socket. Other retained jobs
                # still own use of the shared model; do not stop it underneath them.
                self._last_use = self.clock()
                self._state(GPUPhase.READY, owner)
            else:
                try:
                    await self._stop_loaded()
                    await self._quiescent()
                    self._state(GPUPhase.STOPPED)
                except BaseException:
                    self._state(GPUPhase.ERROR, owner, job_id, "release_unverified")
            raise
        finally:
            if acquired:
                self._lock.release()

    async def stop_idle(self, owner):
        """Explicit HOST action: never stop an active, foreign or recovered owner."""
        if (self._file is None or self.closed or owner not in self.drivers or self._lock.locked()
                or self.waiting or self.usage_count(owner)):
            raise GPUUnsafe("idle_stop_unavailable")
        async with self._lock:
            if self.waiting or self.usage_count(owner):
                raise GPUUnsafe("model_in_use")
            if self.phase == GPUPhase.STOPPED and self.loaded is None:
                return
            if self.phase != GPUPhase.READY or self.loaded != owner:
                raise GPUUnsafe("idle_owner_mismatch")
            driver = self.drivers[owner]
            if hasattr(driver, "idle") and not await driver.idle():
                raise GPUUnsafe("model_in_use")
            if self.waiting or self.usage_count(owner):
                raise GPUUnsafe("model_in_use")
            try:
                await self._stop_loaded()
                self._state(GPUPhase.STOPPED)
            except BaseException:
                self._state(GPUPhase.ERROR, owner, error="release_unverified")
                raise

    async def close(self):
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
            self._maintenance = None
        try:
            # Never stop unknown/recovered owners. Active uncertain jobs stay quarantined.
            if self.phase == GPUPhase.READY and not self.usage_count() and not self._lock.locked():
                await self._stop_loaded()
                self._state(GPUPhase.STOPPED)
            elif self.phase not in {GPUPhase.STOPPED, GPUPhase.ERROR}:
                self._state(GPUPhase.ERROR, self.owner, self.job_id, "recovery_required")
        finally:
            self.closed = True
            if self._file is not None:
                fcntl.flock(self._file, fcntl.LOCK_UN)
                self._file.close()
                self._file = None
            if self._global_file is not None:
                self._global_file.close()
                self._global_file = None

    async def recover_idle(self, owner):
        """Operator recovery only after use is absent and release is proven.

        This never adopts an old PID, clears uncertain remote work, or grants a
        new model start. A restart-lost owner needs quiescent telemetry first.
        """
        if (self._file is None or self.closed or self.phase != GPUPhase.ERROR or
                self.owner != owner or self._lock.locked() or self.waiting or self.usage_count()):
            raise GPUUnsafe("recovery_unavailable")
        if self.error == "remote_recovery_required":
            raise GPUUnsafe("remote_recovery_required")
        async with self._lock:
            try:
                if self.loaded is not None:
                    if self.loaded != owner:
                        raise GPUUnsafe("idle_owner_mismatch")
                    await self._stop_loaded()
                else:
                    # No in-memory owned child => no process adoption/signalling.
                    await self._quiescent()
                if self.usage_count() or self.waiting:
                    raise GPUUnsafe("model_in_use")
                self._state(GPUPhase.STOPPED)
            except BaseException:
                self._state(GPUPhase.ERROR, owner, error="release_unverified")
                raise
