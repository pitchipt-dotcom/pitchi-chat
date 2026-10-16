"""Opt-in read-only ComfyUI catalog probe; no submit/start/stop or private payload output."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

import httpx

from backend.app.tools.config import load_services
from backend.app.tools.comfyui import ComfyImageAdapter
from backend.app.tools.generation import ArtifactStore
from backend.app.tools.http import ServiceHTTP
from backend.app.tools.image_workflows import load_image_workflows


async def probe(services_path, workflows_path):
    service = next(service for service in load_services(services_path) if service.tool == "image.generate")
    if not service.api_base_url or service.image_protocol != "comfyui":
        raise SystemExit("A verified direct ComfyUI origin is required; legacy UI URLs are not inferred")
    workflows = load_image_workflows(workflows_path)
    with tempfile.TemporaryDirectory(prefix="pitch-chat-catalog-") as directory:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            adapter = ComfyImageAdapter(ServiceHTTP(client, service.api_base_url, min(service.timeout_seconds, 8)),
                                       ArtifactStore(Path(directory)), workflows)
            print(json.dumps({"backends": await adapter.catalog(), "generation_submitted": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services-config", type=Path)
    parser.add_argument("--images-config", type=Path)
    args = parser.parse_args()
    asyncio.run(probe(args.services_config, args.images_config))


if __name__ == "__main__":
    main()
