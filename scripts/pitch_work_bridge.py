"""Private external-agent child; writes only new HOST-selected home, never external runs."""

import hashlib
import json
from pathlib import Path
import signal
import sys

PREFIX = "PITCH_CHAT_WORK_EVENT "


def main():
    request = json.loads(sys.stdin.buffer.read(32769))
    source = Path(request["source_root"])
    digest = hashlib.sha256()
    for path in sorted((source / "src/pitchi_work").glob("*.py")):
        if path.is_symlink():
            raise ValueError("Source identity changed")
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    if digest.hexdigest() != request["reviewed_source_sha256"]:
        raise ValueError("Source identity changed")
    sys.path.insert(0, str(source / "src"))
    from pitchi_work.model import health
    from pitchi_work.mission_controller import create_mission, MissionController
    health(request["endpoint"], request["model"])
    run = create_mission(request["home"], request["project"], request["instruction"], request["endpoint"], request["model"])
    print(PREFIX + json.dumps({"phase": "SUBMITTED", "run_id": run.run_id}), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: run.request_cancel())
    state = MissionController(run).execute()
    print(PREFIX + json.dumps({"phase": "TERMINAL", "run_id": run.run_id, "status": state["status"]}), flush=True)
    return 0 if state["status"] == "COMPLETED" and not state.get("active_processes") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Parent classifies failure without receiving private request/exception data.
        raise SystemExit(2) from None
