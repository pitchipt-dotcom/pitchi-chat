"""External-agent bridge controlled by Harness GPU lease and persistent job worker."""

import asyncio
import json
import hashlib
import os
from pathlib import Path
import re
import signal
from typing import Literal
from uuid import UUID

from pydantic import Field, create_model

from backend.app.config import ROOT
from backend.app.harness.contracts import Contract, ErrorCode, FinalStatus, ToolResult
from backend.app.harness.recovery import HarnessFailure, RecoveryPolicy
from backend.app.jobs.context import current_job_id, remote_event
from backend.app.resources.gpu import ResourceMode
from backend.app.tools.registry import ToolDefinition
from backend.app.tools.work_config import WorkSettings, source_digest

EVENT_PREFIX = "PITCH_CHAT_WORK_EVENT "


class WorkArguments(Contract):
    instruction: str = Field(min_length=1, max_length=4000)
    project: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")


class WorkOutput(Contract):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    status: Literal["COMPLETED"]
    result_reference: str
    host_evidence_hashes_verified: Literal[True]
    deployed: Literal[False] = False


class PitchWorkAdapter:
    def __init__(self, settings: WorkSettings, runtime_root: Path, *, spawn=asyncio.create_subprocess_exec):
        self.settings, self.root, self.spawn = settings, runtime_root.resolve(), spawn

    async def execute(self, arguments: WorkArguments, backend: str | None):
        settings = self.settings
        job_id = current_job_id.get()
        try:
            UUID(job_id)
            if (not settings.enabled or backend is not None or arguments.project not in settings.projects
                    or not arguments.instruction.strip() or len(arguments.instruction.encode()) > 8000
                    or source_digest(Path(settings.source_root)) != settings.reviewed_source_sha256):
                raise ValueError
            source = Path(settings.projects[arguments.project])
            project = source.resolve()
            if (source.is_symlink() or not project.is_dir() or project == Path("/") or self.root.is_relative_to(project)
                    or project == Path(settings.source_root).resolve()):
                raise ValueError
        except (TypeError, ValueError, OSError):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED) from None
        home = self.root / job_id
        home.mkdir(parents=True, exist_ok=False, mode=0o700)
        payload = {"source_root": settings.source_root, "reviewed_source_sha256": settings.reviewed_source_sha256,
                   "home": str(home), "project": str(project), "instruction": arguments.instruction,
                   "endpoint": settings.model_endpoint, "model": settings.model_name}
        # No inherited keys, cloud env, proxies, shell initialization or .env loading.
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        remote_event("SUBMITTING")
        process = await self.spawn(settings.python_executable, "-B", str(ROOT / "scripts/pitch_work_bridge.py"),
                                   cwd=str(home), env=env, stdin=asyncio.subprocess.PIPE,
                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                   start_new_session=True, limit=65536)
        run_id, final, submitted = None, None, False
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
            await process.stdin.drain()
            process.stdin.close()
            seen = 0
            async with asyncio.timeout(settings.timeout_seconds):
                while line := await process.stdout.readline():
                    seen += len(line)
                    if seen > 2 * 1024 * 1024:
                        raise ValueError
                    if not line.startswith(EVENT_PREFIX.encode()):
                        continue  # External progress may contain private text; never log it.
                    event = json.loads(line[len(EVENT_PREFIX):])
                    identifier = event.get("run_id")
                    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
                        raise ValueError
                    if run_id is not None and identifier != run_id:
                        raise ValueError
                    run_id = identifier
                    if event.get("phase") == "SUBMITTED" and not submitted and final is None:
                        submitted = True
                        remote_event("SUBMITTED", run_id)
                    elif event.get("phase") == "TERMINAL" and submitted and final is None:
                        final = event
                    else:
                        raise ValueError
                exit_code = await process.wait()
            # A process returning exit0 is not an accepted mission.
            if final is None or run_id is None:
                raise ValueError
            remote_event("TERMINAL", run_id)
            if exit_code != 0 or final.get("status") != "COMPLETED":
                raise HarnessFailure(ErrorCode.TOOL_FAILED)
            run = home / "runs" / run_id
            state = self._json(run / "state.json")
            completion_path = run / "artifacts/request_completion_gate.json"
            completion = self._json(completion_path)
            criteria = completion.get("criteria")
            if (state.get("run_id") != run_id or state.get("status") != "COMPLETED" or state.get("active_processes")
                    or completion.get("status") != "COMPLETED" or completion.get("host_evidence_hashes_verified") is not True
                    or not isinstance(criteria, list) or not criteria or not all(isinstance(c, dict) and c.get("met") is True for c in criteria)
                    or "R0" not in {c.get("id") for c in criteria}
                    or completion.get("root_goal_sha256") != state.get("root_goal_sha256")
                    or self._sha(run / "root_goal.json") != state.get("root_goal_sha256")):
                raise ValueError
            workspace = home / "workspaces" / run_id
            receipts = self._json(run / "artifacts/root_evidence.json")
            if not isinstance(receipts, list) or not receipts:
                raise ValueError
            for receipt in receipts:
                if (not isinstance(receipt, dict) or receipt.get("passed") is not True
                        or self._sha(self._relative(run, receipt["reference"])) != receipt["sha256"]
                        or not isinstance(receipt.get("artifact_hashes"), dict) or not receipt["artifact_hashes"]):
                    raise ValueError
                for name, sha in receipt["artifact_hashes"].items():
                    if self._sha(self._relative(workspace, name)) != sha:
                        raise ValueError
            output = WorkOutput(run_id=run_id, status="COMPLETED", host_evidence_hashes_verified=True,
                                result_reference=f"runs/{run_id}/artifacts/request_completion_gate.json")
            return ToolResult(success=True, result_type="run", output=output.model_dump(), exit_status=exit_code,
                              run_id=run_id, run_status="completed")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED) from None
        except (ValueError, TypeError, KeyError, OSError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        finally:
            if process.returncode is None:
                # Only the child session just created here; never foreign PIDs/run IDs.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 5)
                except ProcessLookupError:
                    pass
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), 2)

    def _json(self, path):
        self._safe_file(path)
        if path.stat().st_size > 512 * 1024:
            raise ValueError
        return json.loads(path.read_bytes())

    def _relative(self, root, name):
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError
        return path

    def _sha(self, path):
        self._safe_file(path)
        if path.stat().st_size > 50_000_000:
            raise ValueError
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while part := file.read(65536):
                digest.update(part)
        return digest.hexdigest()

    def _safe_file(self, path):
        if (not path.resolve().is_relative_to(self.root) or not path.is_file()
                or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(self.root))):
            raise ValueError


def work_definition(settings, runtime_root, gpu_profiles, origins):
    from urllib.parse import urlsplit
    p = urlsplit(settings.model_endpoint)
    matches = origins.get(settings.model_profile) == f"{p.scheme}://{p.netloc}"
    schema = create_model("RegisteredWorkArguments", __base__=WorkArguments,
        project=(Literal[tuple(settings.projects)], Field())) if settings.projects else WorkArguments
    return ToolDefinition(name="pitch_work.run", adapter=PitchWorkAdapter(settings, runtime_root),
        argument_schema=schema, output_schema=WorkOutput, result_type="run", mode=ResourceMode.WORK,
        enabled=bool(settings.enabled and matches and settings.model_profile in gpu_profiles), allow_private=True,
        requires_managed_gpu=True, requires_exit_status=True, resource_profile=settings.model_profile or None,
        remote_job=True, remote_resource=True, recovery=RecoveryPolicy(timeout_seconds=settings.timeout_seconds))
