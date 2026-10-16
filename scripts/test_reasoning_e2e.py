"""Opt-in real local-Qwen/HOST probe. Print metadata only, never raw thoughts."""
import argparse
import json
from pathlib import Path

import httpx

from backend.app.config import load_settings, load_server_settings
from backend.app.models.response import split_response
from backend.app.storage.sqlite import HistoryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model", choices=("qwen", "qwen80b"), default="qwen")
    parser.add_argument("--run", action="store_true", help="Submit two harmless greetings; never retry POST")
    parser.add_argument("--turns", type=int, choices=(1, 2), default=2)
    parser.add_argument("--message", default="こんにちは", help="Use harmless synthetic text only")
    args = parser.parse_args()
    if not args.run:
        print("SKIPPED: --run required; no model request submitted")
        return
    server = load_server_settings(args.config)
    host = f"[{server.host}]" if ":" in server.host else server.host
    base = f"http://{host}:{server.port}"
    settings = load_settings(args.config)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=610) as client:
        if client.get(base + "/health", timeout=5).status_code != 200:
            raise SystemExit("HOST unavailable; no POST submitted")
        cid = None; thoughts = []; final = []
        for message in (args.message, "こんにちは")[:args.turns]:
            response = client.post(base + "/api/chat", json={"message": message, "model": args.model,
                **({"conversation_id": cid} if cid else {})})
            if response.status_code != 200:
                raise SystemExit(f"E2E failed: HTTP {response.status_code}; no response/reasoning printed; no replay")
            result = response.json()
            if result.get("cloud_used") or result.get("renderer_used"):
                raise SystemExit("Not counted as local-only E2E")
            actual = (result.get("model_selection") or {}).get("actual_model_used", args.model)
            if actual != args.model:
                raise SystemExit("Requested model fallback; not counted as requested-model E2E")
            reply = result["reply"]
            if split_response(reply).reasoning_content or not reply.strip():
                raise SystemExit("Final/reasoning separation failed; content not printed")
            final.append(reply); thoughts.append(result.get("reasoning_content") or "")
            print(f"turn={len(final)}; model={actual}; final_only=true; thinking_detected={bool(thoughts[-1].strip())}", flush=True)
            cid = result["conversation_id"]
        history = client.get(base + f"/api/conversations/{cid}").json()["messages"]
        assert [row["content"] for row in history if row["role"] == "assistant"] == final
        with HistoryStore(settings.database).connection() as db:
            rows = [dict(row) for row in db.execute("SELECT role,content FROM messages WHERE conversation_id=?", (cid,))]
            audit = [dict(row) for row in db.execute("SELECT metadata FROM harness_audit")]
        for thought in filter(None, thoughts):
            assert thought not in json.dumps(rows, ensure_ascii=False)
            assert thought not in json.dumps(audit, ensure_ascii=False)
        detected = sum(bool(thought.strip()) for thought in thoughts)
        print(f"SHA reasoning E2E: model={actual}; final_only=true; turns={len(final)}; thinking_turns={detected}; "
              "history_final_only=true; raw_reasoning_persisted=false; content_not_logged=true")
        if not detected:
            print("Thinking-specific live case NOT VERIFIED: model returned no reasoning; mock coverage remains separate")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError, AssertionError):
        raise SystemExit("E2E transport/contract/storage failure; private content not printed; no automatic replay") from None
