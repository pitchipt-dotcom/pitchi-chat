"""Opt-in actual 80B E2E. Never stop external services or accept CPU fallback."""

import argparse
from pathlib import Path

import httpx

from backend.app.config import load_server_settings
from backend.app.resources.config import load_resources
from backend.app.tools.retrieval import public_topic
from scripts.test_pt_assistant import run_e2e


def probe(client, base, health_url, mode, query):
    before = client.get(f"{base}/api/gpu"); before.raise_for_status()
    resources = client.get(f"{base}/api/resources"); resources.raise_for_status()
    if before.json().get("state") != "STOPPED" or before.json().get("loaded_model") is not None or resources.json().get("active_job") is not None:
        print("SHA 80B E2E: SKIPPED; existing owned model/job or recovery; no stop performed")
        return
    started = client.post(f"{base}/api/models/qwen80b/lifecycle", json={"action": "start"})
    if started.status_code == 409:
        print("SHA 80B E2E: SKIPPED; HOST rejected GPU admission; no external process stopped")
        return
    started.raise_for_status()
    try:
        if started.json() != {"model": "qwen80b", "state": "READY"}:
            raise SystemExit("80B readiness contract invalid; no external process operation")
        health = client.get(health_url); health.raise_for_status()
        run_e2e(client, base, mode, "qwen80b", query, require_actual=True)
    finally:
        # HOST alone verifies idle owner/session identity and release; never PID-kill here.
        stopped = client.post(f"{base}/api/models/qwen80b/lifecycle", json={"action": "stop"})
        stopped.raise_for_status()
        after = client.get(f"{base}/api/gpu"); after.raise_for_status()
        state = after.json()
        if state.get("state") != "STOPPED" or state.get("loaded_model") is not None or state.get("error"):
            raise SystemExit("80B owned stop/release unverified; not counted as successful GPU integration")
        print("SHA 80B owned stop/release: VERIFIED by HOST; external backgrounds retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--resources-config", type=Path)
    parser.add_argument("--mode", choices=("pt_learning", "clinical"), default="pt_learning")
    parser.add_argument("--query", default="脳卒中 歩行")
    args = parser.parse_args()
    query = public_topic(args.query)
    settings = load_server_settings(args.config)
    resources = load_resources(args.resources_config)
    profile = next((p for p in resources.profiles if p.name == "qwen80b" and p.device == "gpu"), None)
    if profile is None:
        raise SystemExit("80B HOST-owned GPU profile missing")
    from urllib.parse import urlsplit
    from ipaddress import ip_address
    hostname = urlsplit(profile.health_url).hostname
    if hostname != "localhost" and not ip_address(hostname).is_loopback:
        raise SystemExit("80B health must be a reviewed loopback endpoint")
    host = f"[{settings.host}]" if ":" in settings.host else settings.host
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=620) as client:
        probe(client, f"http://{host}:{settings.port}", profile.health_url, args.mode, query)


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError, AssertionError):
        raise SystemExit("80B E2E transport/contract failure; private content hidden; no automatic replay") from None
