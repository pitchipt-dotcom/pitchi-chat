"""Opt-in real HOST/local-model/book E2E; print metadata, never private evidence."""

import argparse
from pathlib import Path
import time

import httpx

from backend.app.config import load_server_settings
from backend.app.harness.evidence import GroundedAnswer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model", choices=("qwen", "bonsai", "qwen80b"))
    parser.add_argument("--question", default="蔵書から「リハビリ」という語を検索し、見つかった抜粋を1つ原文引用して説明してください。")
    args = parser.parse_args()
    server = load_server_settings(args.config)
    host = f"[{server.host}]" if ":" in server.host else server.host
    base = f"http://{host}:{server.port}"
    start = time.monotonic()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=610) as client:
        # Retry readiness GET only; never replay an uncertain Chat/tool submission.
        for _ in range(30):
            try:
                if client.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise SystemExit("HOST unavailable; no Chat request submitted")
        try:
            response = client.post(base + "/api/chat", json={"message": args.question, "mode": "library",
                **({"model": args.model} if args.model else {})})
        except httpx.HTTPError:
            raise SystemExit("Library E2E transport failure; no automatic replay") from None
        if response.status_code != 200:
            raise SystemExit(f"Library E2E failed: HTTP {response.status_code}; private response not printed")
        result = response.json()
        answer = GroundedAnswer.model_validate(result["grounded_answer"], strict=True)
        refs = {item.evidence_id: item.reference for item in answer.evidence.items}
        if not answer.book_claims:
            raise SystemExit(f"Library E2E abstained; evidence={len(refs)}; not counted as grounded E2E success")
        assert all(claim.evidence_id in refs and claim.quote in refs[claim.evidence_id].snippet for claim in answer.book_claims)
        assert "蔵書に基づく内容" in result["reply"] and "AIによる整理・推論" in result["reply"]
        assert "citation:" in result["reply"] and "PDFページ:" in result["reply"]
        selection = result.get("model_selection") or {}
        print(f"SHA library E2E: SUCCESS; requested={selection.get('requested_model', 'unknown')}; "
              f"actual={selection.get('actual_model_used', 'unknown')}; fallback={selection.get('fallback_reason')}; "
              f"evidence={len(refs)}; verified_quotes={len(answer.book_claims)}; elapsed={time.monotonic()-start:.1f}s; content not logged")


if __name__ == "__main__":
    main()
