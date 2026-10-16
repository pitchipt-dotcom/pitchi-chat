#!/usr/bin/env python3
"""Read-only administrator planning aid. No execution/swapoff option exists."""
import json
from pathlib import Path
import sys
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.resources.memory_recovery import linux_memory_details, recovery_assessment


def main():
    memory = linux_memory_details()
    opener = build_opener(ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/api/models/strata/preflight', timeout=10) as reply:
        admission = json.load(reply)
    with opener.open('http://127.0.0.1:8000/api/gpu', timeout=10) as reply:
        gpu = json.load(reply)
    with opener.open('http://127.0.0.1:8000/api/resources', timeout=10) as reply:
        resources = json.load(reply)
    state = {'ram_available_mib': memory['MemAvailable'],
             'swap_used_mib': memory['SwapTotal'] - memory['SwapFree']}
    assessment = recovery_assessment(state, admission['required_ram_mib'])
    idle = (gpu['state'] == 'STOPPED' and gpu['loaded_model'] is None
            and not gpu['queued'] and not gpu['retained_uses']
            and resources['active_job'] is None and not resources['waiting_jobs'])
    print(json.dumps({'memory_mib': memory, 'assessment': assessment,
                     'managed_gpu_jobs_idle': idle,
                     'active_swaps': Path('/proc/swaps').read_text(),
                     'memory_pressure': Path('/proc/pressure/memory').read_text(),
                     'overcommit_memory': Path('/proc/sys/vm/overcommit_memory').read_text().strip(),
                     'administrator_must_check_external_jobs': True,
                     'model_admission': admission}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
