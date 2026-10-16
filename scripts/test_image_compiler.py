"""Opt-in SHA local compiler preview: persistent job/Spec, never image generation."""

import argparse
from pathlib import Path
import time

import httpx

from backend.app.config import load_server_settings
from backend.app.harness.image import ImageSpec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--backend", default="qwen-image")
    args = parser.parse_args()
    server = load_server_settings(args.config)
    host = f"[{server.host}]" if ":" in server.host else server.host
    base = f"http://{host}:{server.port}"
    start = time.monotonic()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
        response = client.post(base + "/api/images/jobs", json={
            "request": "中央に猫がいる、文字なしの水彩イラストを作りたい。", "backend": args.backend,
            "aspect_ratio": "1:1", "compile_only": True})
        if response.status_code != 202:
            raise SystemExit(f"Compiler submission failed: HTTP {response.status_code}; body not printed")
        job_id = response.json()["job_id"]
        for _ in range(400):
            status = client.get(base + f"/api/jobs/{job_id}").json()
            if status["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(1)
        if status["status"] != "SUCCESS":
            raise SystemExit(f"Compiler job: {status['status']}; no automatic replay")
        result = client.get(base + f"/api/jobs/{job_id}/result").json()
        ImageSpec.model_validate(result["image_spec"], strict=True)
        assert status["image_phase"] == "SPEC_READY" and status["remote_run_id"] is None
        assert result["tool_result"] is None and not result["cloud_used"]
        print(f"SHA Image Compiler: SUCCESS; persisted=SPEC_READY; no image submitted; elapsed={time.monotonic()-start:.1f}s; content not logged")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError):
        raise SystemExit("Compiler probe transport/contract failure; private response not printed; no automatic replay") from None
