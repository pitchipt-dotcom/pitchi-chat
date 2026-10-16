"""Read-only bounded API discovery. Never submit generation or print bodies/URLs."""

import argparse
import asyncio
import json
from pathlib import Path
import tomllib

import httpx


async def probe(path: Path):
    with path.open("rb") as file:
        services = tomllib.load(file)["services"]
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8) as client:
        async def one(name, service):
            records = []
            for suffix in ("", "openapi.json", "docs", "config"):
                try:
                    async with asyncio.timeout(8), client.stream("GET", service["ui_url"].rstrip("/") + "/" + suffix) as response:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 2 * 1024 * 1024:
                                break
                        record = {"path": "/" + suffix, "status": response.status_code,
                                  "type": response.headers.get("content-type", "").split(";")[0]}
                        if suffix == "openapi.json" and response.status_code == 200:
                            schema = json.loads(data)
                            record["routes"] = {route: list(methods) for route, methods in schema.get("paths", {}).items()}
                        records.append(record)
                except (httpx.HTTPError, ValueError, TimeoutError):
                    records.append({"path": "/" + suffix, "error": "unavailable_or_invalid"})
            return {"tool": name, "probes": records}
        for result in await asyncio.gather(*(one(name, service) for name, service in services.items())):
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/services.local.toml"))
    asyncio.run(probe(parser.parse_args().config))
