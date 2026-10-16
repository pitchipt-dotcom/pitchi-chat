"""Read-only GPU admission report. Private identities appear ONLY with explicit opt-in."""

import argparse
import asyncio
import json
from pathlib import Path

from backend.app.resources.config import load_resources
from backend.app.resources.manager import NvidiaInspector, GPUUnsafe
from backend.app.resources.ownership import Ownership, OwnershipPolicy


async def inspect(config, private_details=False):
    settings = load_resources(config)
    if not settings.inspect_gpu:
        raise ValueError("Inspection must be explicitly enabled")
    reading = await NvidiaInspector().read()
    policy = OwnershipPolicy(settings.external_processes, settings.reserve_mib)
    classes = policy.classify(reading, frozenset())
    result = {"read_only": True, "total_mib": reading.total_mib, "used_mib": reading.used_mib,
              "classifications": {kind.value: sum(c == kind for c, _ in classes.values()) for kind in Ownership},
              "profiles": {p.name: policy.admission_reason(reading, frozenset(), p.required_vram_mib)
                           for p in settings.profiles if p.device == "gpu"}}
    if private_details:
        result["private_processes"] = []
        for p in reading.processes:
            try:
                fields = Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()
                parent = int(fields[1])
                parent_name = Path(f"/proc/{parent}/comm").read_text().strip()
            except (OSError, ValueError, IndexError):
                parent, parent_name = None, None
            result["private_processes"].append({"pid": p.pid, "comm": p.comm,
                "parent_pid": parent, "parent_name": parent_name, "memory_mib": p.used_mib,
                "kind": p.kind, "start_ticks": p.start_ticks, "cgroup": p.cgroup,
                "classification": classes[p.pid][0].value})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--private-details", action="store_true", help="Do not publish this output; never reads argv/environ")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(inspect(args.config, args.private_details)), ensure_ascii=False, indent=2))
    except (GPUUnsafe, ValueError, OSError):
        raise SystemExit("GPU inspection failed closed; no process was modified") from None


if __name__ == "__main__":
    main()
