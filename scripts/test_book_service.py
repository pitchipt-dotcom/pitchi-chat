"""Opt-in SHA integration: real read-only search through Harness, no raw results printed."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

import httpx

from backend.app.harness.audit import AuditEvent, AuditTrail
from backend.app.harness.contracts import ToolCall
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.config import load_services
from backend.app.tools.services import service_registry


async def run(path: Path, query: str):
    services = load_services(path)
    with tempfile.TemporaryDirectory(prefix="pitch-chat-book-test-") as directory:
        store = HistoryStore(Path(directory) / "audit.sqlite3")
        store.initialize()
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            registry = service_registry(services, client, Path(directory) / "artifacts")
            harness = Harness(None, ResourceCoordinator(), AuditTrail(store), registry=registry,
                              policy=TurnPolicy(allowed_tools=frozenset({"book.search"})))
            result = await harness.run_tool(ToolCall(tool="book.search", arguments={"query": query, "limit": 1}))
            output = result.tool_result.output
            with store.connection() as db:
                audit = [json.loads(row["metadata"]) for row in db.execute("SELECT metadata FROM harness_audit")]
            assert all(set(row) <= set(AuditEvent.model_fields) and row.get("selected_tool") in {None, "book.search"}
                       for row in audit)
            print(f"book.search SHA integration: {result.status.value}; returned={output['returned']}; citations validated; audit content-free")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/services.local.toml"))
    parser.add_argument("--query", default="リハビリ")
    args = parser.parse_args()
    asyncio.run(run(args.config, args.query))


if __name__ == "__main__":
    main()
