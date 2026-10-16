"""Read-only existing Pitchi-work CLI probe. Never creates/resumes/cancels a run."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from backend.app.tools.work_config import load_work, source_digest


def snapshot(source):
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (source / "runs").glob("*/state.json") if p.is_file() and not p.is_symlink()}


async def probe(settings):
    if not settings.source_root or not settings.python_executable:
        return {"status": "SKIPPED", "reason": "external_source_unconfigured", "submitted": False}
    source = Path(settings.source_root)
    fingerprint, states = source_digest(source), snapshot(source)
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(source / "src")}
    process = await asyncio.create_subprocess_exec(settings.python_executable, "-B", "-m", "pitchi_work.cli",
        "--home", str(source), "status", cwd=str(source), env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(10):
            data = await process.stdout.read(2 * 1024 * 1024 + 1)
            await process.wait()
        if (process.returncode != 0 or len(data) > 2 * 1024 * 1024
                or fingerprint != source_digest(source) or states != snapshot(source)):
            raise ValueError
        rows = json.loads(data)
        if not isinstance(rows, list):
            raise ValueError
        return {"status": "CLI_READ_ONLY_OK", "existing_run_count": len(rows),
                "status_counts": dict(Counter(row.get("status", "UNKNOWN") for row in rows)),
                "source_pin_matches": fingerprint == settings.reviewed_source_sha256,
                "run_submitted": False, "integration": "status_only_not_agent_execution"}
    except (ValueError, TypeError, TimeoutError):
        return {"status": "FAILED", "submitted": False}
    finally:
        if process.returncode is None:
            process.terminate()  # This read-only probe child only, never an external agent run.
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    print(json.dumps(asyncio.run(probe(load_work(parser.parse_args().config))), ensure_ascii=False))
