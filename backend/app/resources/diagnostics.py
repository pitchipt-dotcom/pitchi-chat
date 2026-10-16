"""Private, per-start Strata diagnostics. Never writes prompts or response bodies."""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


current_start_id: ContextVar[str | None] = ContextVar("strata_start_id", default=None)


def safety_category(reason: str | None) -> str:
    if reason in {"insufficient_ram", "runtime_ram_reserve"}:
        return "ram"
    if reason in {"insufficient_swap_reserve", "insufficient_swap"}:
        return "swap"
    if reason in {"insufficient_vram", "vram_release_unverified", "profile_memory_budget_exceeded"}:
        return "vram"
    if reason in {"telemetry_unavailable", "memory_telemetry_unavailable"}:
        return "telemetry"
    if reason in {"start_timeout", "owned_process_exited"}:
        return "engine_startup"
    return "ownership_or_lifecycle"


class StartDiagnostics:
    """Owner-only append-only JSONL records, one file per explicit start request."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _file(self, start_id: str | None = None) -> Path:
        start_id = start_id or current_start_id.get() or str(uuid.uuid4())
        # Never allow a caller-controlled path component.
        safe_id = "".join(ch for ch in start_id if ch.isalnum() or ch == "-")[:64] or str(uuid.uuid4())
        return self.directory / f"strata-start-{safe_id}.jsonl"

    def record(self, event: str, *, start_id: str | None = None, **fields):
        path = self._file(start_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.parent.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError("Strata diagnostics directory must be owner-only")
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, "start_id": path.stem.removeprefix("strata-start-"), **fields}
        data = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8", "replace")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        return path
