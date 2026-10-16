"""Opt-in HOST speech job probe. Never starts/stops an existing external service."""

import argparse
import asyncio
import io
import json
from pathlib import Path
import wave

import httpx

from backend.app.config import load_server_settings


async def probe(client, *, generate=False, timeout=600):
    response = await client.get("/api/tools")
    response.raise_for_status()
    tools = response.json()["tools"]
    tool = next((t for t in tools if t["id"] == "tts.generate"), None)
    if not tool or not tool["enabled"]:
        return {"status": "SKIPPED", "reason": "tts_lifecycle_or_api_unavailable", "submitted": False}
    gpu_response = await client.get("/api/gpu")
    gpu_response.raise_for_status()
    gpu = gpu_response.json()
    if gpu.get("state") != "STOPPED" or gpu.get("external_conflict") or gpu.get("memory_used_mib") is None or gpu.get("active_job"):
        return {"status": "SKIPPED", "reason": "gpu_unavailable", "submitted": False}
    if not generate:
        return {"status": "READY_FOR_OPT_IN", "submitted": False}
    response = await client.post("/api/jobs", json={"tool": "tts.generate", "arguments": {"text": "こんにちは。音声の確認です。"}})
    if response.status_code == 409:
        return {"status": "SKIPPED", "reason": "resource_rejected", "submitted": False}
    response.raise_for_status()
    job_id = response.json()["job_id"]
    async with asyncio.timeout(timeout):
        while True:
            response = await client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            if job["status"] not in {"QUEUED", "RUNNING"}:
                break
            await asyncio.sleep(.5)
    if job["status"] != "SUCCESS":
        return {"status": job["status"], "error": job["error"], "job_id": job_id, "submitted": True}
    response = await client.get(f"/api/jobs/{job_id}/result")
    response.raise_for_status()
    assert response.json()["tool_result"]["result_type"] == "audio"
    response = await client.get(f"/api/jobs/{job_id}/artifacts/0")
    response.raise_for_status()
    with wave.open(io.BytesIO(response.content)) as audio:
        assert audio.getnframes() > 0
        assert len(audio.readframes(audio.getnframes())) == audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
    return {"status": "SUCCESS", "job_id": job_id, "wav_bytes": len(response.content), "submitted": True}


async def main(args):
    settings = load_server_settings(args.config)
    async with httpx.AsyncClient(base_url=f"http://{settings.host}:{settings.port}", trust_env=False,
                                 follow_redirects=False, timeout=10) as client:
        print(json.dumps(await probe(client, generate=args.generate), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--generate", action="store_true")
    asyncio.run(main(parser.parse_args()))
