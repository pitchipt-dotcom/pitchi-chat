"""Opt-in SHA PT E2E with a public topic; never submit private patient data."""

import argparse
from pathlib import Path
import time

import httpx

from backend.app.config import load_server_settings
from backend.app.harness.pt import PTAnswer
from backend.app.tools.retrieval import public_topic


def run_e2e(client, base, mode, model, query, *, require_actual=False):
    started = time.monotonic()
    question = f"「{query}」について、取得原文の根拠とAIによる整理を分け、各欄を一文で簡潔に説明してください。患者個別情報はありません。"
    response = client.post(f"{base}/api/chat", json={"message": question,
        "model": model, "mode": mode, "evidence_query": query, "external_retrieval": True})
    if response.status_code != 200:
        raise SystemExit(f"PT E2E failed: HTTP {response.status_code}; private response not printed; no replay")
    payload = response.json()
    selection = payload.get("model_selection") or {}
    actual = selection.get("actual_model_used", "unknown")
    if require_actual and (actual != model or selection.get("fallback_reason") is not None):
        raise SystemExit("Requested-model E2E failed: fallback or missing attribution; not counted as real requested-model success")
    answer = PTAnswer.model_validate(payload["pt_answer"], strict=True)
    assert answer.mode == mode and [call.tool for call in answer.plan.steps] == ["book.search", "ptotsearch.search"]
    references = {item.evidence_id: item.reference for item in answer.evidence.items}
    assert any(ref.source_type == "paper" for ref in references.values())
    if not answer.source_claims:
        raise SystemExit("PT E2E abstained; not counted as grounded success")
    assert all(claim.quote == references[claim.evidence_id].text for claim in answer.source_claims)
    assert "AIによる整理・推論" in payload["reply"] and "citation:" in payload["reply"]
    print(f"SHA PT E2E: SUCCESS; mode={mode}; requested={model}; actual={actual}; "
          f"fallback={selection.get('fallback_reason')}; tools=2; evidence={len(references)}; "
          f"quotes={len(answer.source_claims)}; elapsed={time.monotonic()-started:.1f}s; content not logged")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mode", choices=("pt_learning", "clinical"), default="pt_learning")
    parser.add_argument("--model", choices=("qwen", "bonsai", "qwen80b"), default="qwen")
    parser.add_argument("--query", default="脳卒中 歩行")
    parser.add_argument("--require-actual-model", action="store_true", help="Reject fallback as requested-model success")
    args = parser.parse_args()
    query = public_topic(args.query)
    server = load_server_settings(args.config)
    host = f"[{server.host}]" if ":" in server.host else server.host
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=610) as client:
        run_e2e(client, f"http://{host}:{server.port}", args.mode, args.model, query, require_actual=args.require_actual_model)


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError):
        raise SystemExit("PT E2E transport/contract failure; response not printed; no automatic replay") from None
