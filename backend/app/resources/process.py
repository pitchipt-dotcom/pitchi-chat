"""Explicit HOST-owned child process profiles, no shell and no PID/name adoption."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
from urllib.parse import urlsplit

import uuid

import httpx

from backend.app.resources.manager import GPUUnsafe
from backend.app.resources.diagnostics import StartDiagnostics, current_start_id, safety_category


@dataclass(frozen=True)
class ProcessProfile:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    health_url: str
    start_timeout: float = 60
    stop_timeout: float = 10
    device: str = "gpu"
    required_vram_mib: int = 0
    required_ram_mib: int = 0
    min_swap_free_mib: int = 0
    runtime_ram_reserve_mib: int = 0
    manual_start: bool = False
    graceful_only: bool = False
    diagnostics_path: Path | None = None
    low_swap_start_ram_mib: int = 0
    backend_ram_mib: tuple[tuple[str, int], ...] = ()


class OwnedProcess:
    def __init__(self, profile: ProcessProfile, client: httpx.AsyncClient):
        self.profile, self.client = profile, client
        self.process: asyncio.subprocess.Process | None = None
        self.identity: str | None = None
        self.diagnostics = StartDiagnostics(profile.diagnostics_path) if profile.diagnostics_path else None
        self.gpu_reader = None
        self._diag_id: str | None = None
        self._capture_tasks = []
        self._sampler_task = None
        self._capture_error: str | None = None
        self._startup_capture = False

    @property
    def owned_pids(self):
        # Child group descendants also belong to the explicitly started session.
        if self.process is None:
            return frozenset()
        try:
            leader = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            if self.identity is not None and leader[19] != self.identity:
                return frozenset()  # A reused leader PID never confers ownership.
        except FileNotFoundError:
            pass  # Orphan descendants can still retain the owned session.
        except (OSError, ValueError, IndexError):
            raise GPUUnsafe("process_identity_unverified") from None
        pids = set()
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                # Include the whole owned session, even an orphan engine after
                # the HTTP server exits. Both group and session must match.
                if int(fields[2]) == self.process.pid and int(fields[3]) == self.process.pid:
                    pids.add(int(path.parent.name))
            except (OSError, ValueError, IndexError):
                continue
        return frozenset(pids)

    @property
    def process_tree_pids(self):
        if self.process is None or self.process.returncode is not None:
            return frozenset()
        parents = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                parents[int(path.parent.name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
        pids = {self.process.pid}
        changed = True
        while changed:
            descendants = {pid for pid, parent in parents.items() if parent in pids}
            changed = not descendants.issubset(pids)
            pids.update(descendants)
        return frozenset(pids)

    def _record(self, event, **fields):
        if self.diagnostics:
            try:
                self.diagnostics.record(event, start_id=self._diag_id, **fields)
            except OSError:
                self._capture_error = "diagnostics_write_failed"

    async def _capture(self, reader, stream):
        engine = None
        if self.profile.name == "vision":
            from backend.app.models.qwen35_logs import EngineDiagnostics
            engine = EngineDiagnostics(self.process.pid)
        while True:
            data = await reader.read(4096)
            if not data:
                return
            if self._startup_capture:
                self._record("server_" + stream, chunk=data.decode("utf-8", "replace"))
            elif engine is not None:
                engine.feed(data)

    @staticmethod
    def _proc_table():
        rows = {}
        for path in Path("/proc").glob("[0-9]*/status"):
            pid = int(path.parent.name)
            try:
                text = path.read_text()
                values = {}
                for line in text.splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        if key in {"Name", "PPid", "VmRSS", "VmSwap", "VmLck", "VmPin"}:
                            values[key] = value.strip()
                def kib(key):
                    return int(values.get(key, "0 kB").split()[0])
                stat = path.parent / "stat"
                fields = stat.read_text().rsplit(")", 1)[1].split()
                rows[pid] = {"pid": pid, "ppid": int(fields[1]), "start": fields[19],
                    "name": values.get("Name", "?"), "rss_kib": kib("VmRSS"), "swap_kib": kib("VmSwap"),
                    "vmlck_kib": kib("VmLck"), "vmpin_kib": kib("VmPin")}
            except (OSError, ValueError, IndexError):
                continue
        return rows

    @staticmethod
    def _vmstat():
        try:
            fields = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            return {key: int(fields[key]) for key in ("pswpin", "pswpout", "oom_kill") if key in fields}
        except (OSError, ValueError, KeyError):
            return None

    async def _sample_startup(self):
        previous_swaps = {}
        previous_vmstat = None
        while self.process is not None and self.process.returncode is None:
            rows = self._proc_table()
            pids = self.process_tree_pids
            owned = [row for pid, row in rows.items() if pid in pids]
            swap_deltas = []
            for pid, row in rows.items():
                if pid in pids:
                    continue
                identity = (pid, row["start"])
                before = previous_swaps.get(identity, row["swap_kib"])
                delta = row["swap_kib"] - before
                if delta > 0:
                    swap_deltas.append({"pid": pid, "name": row["name"], "delta_kib": delta,
                                        "swap_kib": row["swap_kib"], "rss_kib": row["rss_kib"]})
            previous_swaps = {(pid, row["start"]): row["swap_kib"] for pid, row in rows.items()}
            try:
                mem = {line.split(":", 1)[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line}
                memory = {"mem_available_kib": mem.get("MemAvailable"), "swap_free_kib": mem.get("SwapFree")}
            except (OSError, ValueError, IndexError):
                memory = {"mem_available_kib": None, "swap_free_kib": None}
            vmstat = self._vmstat()
            try:
                gpu = await self.gpu_reader() if self.gpu_reader else None
                gpu_data = None if gpu is None else {"used_mib": gpu.used_mib, "total_mib": gpu.total_mib,
                    "free_mib": gpu.total_mib - gpu.used_mib if gpu.total_mib is not None else None,
                    "processes": [{"pid": p.pid, "used_mib": p.used_mib} for p in gpu.processes]}
            except Exception as exc:
                gpu_data = {"error": getattr(exc, "args", ["telemetry_unavailable"])[0]}
            self._record("startup_sample", **memory,
                strata={"pids": owned, "rss_kib": sum(p["rss_kib"] for p in owned),
                    "swap_kib": sum(p["swap_kib"] for p in owned),
                    "vmlck_kib": sum(p["vmlck_kib"] for p in owned),
                    "vmpin_kib": sum(p["vmpin_kib"] for p in owned)},
                other_process_swap_increase=swap_deltas,
                vmstat=vmstat,
                vmstat_delta={key: vmstat[key] - previous_vmstat.get(key, vmstat[key]) for key in vmstat}
                    if vmstat is not None and previous_vmstat is not None else None,
                gpu=gpu_data)
            if self._capture_error:
                return
            previous_vmstat = vmstat
            await asyncio.sleep(1)

    async def start(self):
        self._diag_id = current_start_id.get() or str(uuid.uuid4())
        if self.process is not None and self.process.returncode is None:
            raise GPUUnsafe("owned_process_already_running")
        self._record("process_spawn_requested", argv=self.profile.argv, cwd=str(self.profile.cwd))
        if self._capture_error:
            raise GPUUnsafe("diagnostics_unavailable")
        # Existing healthy listener is external; never start/adopt it silently.
        try:
            response = await self.client.get(self.profile.health_url, timeout=1, follow_redirects=False)
            raise GPUUnsafe("endpoint_already_owned")
        except httpx.HTTPError:
            pass
        try:
            self.process = await asyncio.create_subprocess_exec(*self.profile.argv, cwd=self.profile.cwd,
                              start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                              env=os.environ | {"CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "none"}
                                  if self.profile.device == "cpu" else None)
        except OSError as error:
            self._record("process_spawn_failed", error_type=type(error).__name__, exit_code=None)
            raise GPUUnsafe("owned_process_spawn_failed") from None
        self.identity = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        self._record("host_process_started", pid=self.process.pid, start_ticks=self.identity)
        self._startup_capture = True
        self._capture_tasks = [asyncio.create_task(self._capture(self.process.stdout, "stdout")),
                               asyncio.create_task(self._capture(self.process.stderr, "stderr"))]
        self._sampler_task = asyncio.create_task(self._sample_startup())
        try:
            async with asyncio.timeout(self.profile.start_timeout):
                while self.process.returncode is None:
                    if self.profile.low_swap_start_ram_mib:
                        from backend.app.resources.memory import check_profile_memory
                        check_profile_memory(self.profile, stage='startup')
                    try:
                        response = await self.client.get(self.profile.health_url, timeout=1, follow_redirects=False)
                        if response.status_code == 200:
                            self._record("startup_ready", exit_code=None)
                            self._startup_capture = False
                            self._sampler_task.cancel()
                            await asyncio.gather(self._sampler_task, return_exceptions=True)
                            if self._capture_error:
                                raise GPUUnsafe(self._capture_error)
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                self._record("startup_failed", reason="owned_process_exited", category="engine_startup",
                             exit_code=self.process.returncode)
                raise GPUUnsafe("owned_process_exited")
        except TimeoutError:
            self._record("startup_failed", reason="start_timeout", category=safety_category("start_timeout"),
                         exit_code=self.process.returncode)
            raise GPUUnsafe("start_timeout") from None
        finally:
            if self._sampler_task and not self._sampler_task.done():
                self._sampler_task.cancel()
                await asyncio.gather(self._sampler_task, return_exceptions=True)
            if self.process.returncode is not None:
                self._record("host_process_exit", exit_code=self.process.returncode)

    async def healthy(self):
        if self.process is None or self.process.returncode is not None or self.process.pid not in self.owned_pids:
            return False
        try:
            response = await self.client.get(self.profile.health_url, timeout=1, follow_redirects=False)
            if response.status_code != 200:
                return False
            if self.profile.name == "strata":
                return response.json().get("loaded") is True
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def idle(self):
        if self.profile.name == 'vision' and self.process is None:
            # This driver never spawned/adopted a child. An occupied foreign
            # endpoint must not be queried as if it were our inference server.
            return True
        if self.profile.name not in {"strata", "comfyui", "vision"}:
            return True
        # A fully exited owned session cannot have a generation in flight.
        # An orphan child still prevents this shortcut.
        if self.process is not None and self.process.returncode is not None and not self.owned_pids:
            return True
        origin = urlsplit(self.profile.health_url)
        try:
            if self.profile.name == 'vision':
                response = await self.client.get(f"{origin.scheme}://{origin.netloc}/slots", timeout=2, follow_redirects=False)
                response.raise_for_status()
                slots = response.json()
                return isinstance(slots, list) and len(slots) == 1 and all(
                    isinstance(slot, dict) and slot.get('is_processing') is False for slot in slots)
            if self.profile.name == "comfyui":
                response = await self.client.get(f"{origin.scheme}://{origin.netloc}/queue",
                                                 timeout=1, follow_redirects=False)
                response.raise_for_status()
                queue = response.json()
                return (isinstance(queue, dict)
                        and all(isinstance(queue.get(key), list) and not queue[key]
                                for key in ("queue_running", "queue_pending")))
            response = await self.client.get(f"{origin.scheme}://{origin.netloc}/v1/status",
                                             timeout=1, follow_redirects=False)
            response.raise_for_status()
            count = response.json()["activity"]["in_flight"]
            return type(count) is int and count == 0
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return False

    def _signal(self, sig):
        if self.process is None:
            return
        try:
            identity = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            if identity != self.identity or os.getpgid(self.process.pid) != self.process.pid:
                raise GPUUnsafe("process_identity_changed")
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass

    async def unload_comfy(self):
        """Official async cache release, only under the owned idle GPU lock."""
        if not await self.idle():
            raise GPUUnsafe('model_in_use')
        origin = urlsplit(self.profile.health_url)
        base = f'{origin.scheme}://{origin.netloc}'
        try:
            response = await self.client.post(base + '/free', json={'unload_models': True, 'free_memory': True}, timeout=5, follow_redirects=False)
            response.raise_for_status()
            async with asyncio.timeout(min(self.profile.stop_timeout, 15)):
                while True:
                    if not await self.idle():
                        raise GPUUnsafe('model_in_use')
                    stats = await self.client.get(base + '/system_stats', timeout=2, follow_redirects=False)
                    stats.raise_for_status()
                    devices = stats.json()['devices']
                    used = sum(d['torch_vram_total'] - d['torch_vram_free'] for d in devices)
                    if not devices or used < 0:
                        raise ValueError('invalid memory telemetry')
                    if used <= 256 * 1024 * 1024:
                        from backend.app.resources.memory import memory_state
                        self.last_unload = dict(memory_state(), torch_vram_used_mib=used // (1024 * 1024), unload_requested=True)
                        return
                    await asyncio.sleep(.25)
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise GPUUnsafe('comfy_unload_unverified') from None

    async def stop(self):
        if self.process is None:
            return
        if self.profile.name == 'vision' and not await self.idle():
            raise GPUUnsafe('model_in_use')
        if self.profile.name == 'comfyui' and self.profile.low_swap_start_ram_mib and self.process.returncode is None:
            await self.unload_comfy()
        self._signal(signal.SIGINT if self.profile.graceful_only else signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(self.process.wait()), self.profile.stop_timeout)
        except TimeoutError:
            if self.profile.graceful_only:
                raise GPUUnsafe("graceful_stop_unconfirmed") from None
            self._signal(signal.SIGKILL)
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), self.profile.stop_timeout)
            except TimeoutError:
                raise GPUUnsafe("stop_timeout") from None
        # Parent exit alone cannot prove engine/RAM release. Wait for the owned
        # session to disappear; no force escalation after a graceful-only stop.
        try:
            async with asyncio.timeout(self.profile.stop_timeout):
                while self.owned_pids:
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise GPUUnsafe("process_tree_release_unconfirmed") from None

        self._record("host_process_exit", exit_code=self.process.returncode)
        if self._capture_tasks:
            await asyncio.gather(*self._capture_tasks, return_exceptions=True)
            self._capture_tasks = []
