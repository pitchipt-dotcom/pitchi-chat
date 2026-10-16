"""Opt-in real HOST chat probe. Never starts/stops models or external services."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirect refused; use the reviewed HOST origin")


def request(base, route, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(base + route, data=data, headers={"Content-Type": "application/json"})
    # No environment proxy; no conversation goes to a third-party proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=600) as response:
        return json.load(response)


def stream(base, payload):
    started, first = time.perf_counter(), None
    req = urllib.request.Request(base + "/api/chat/stream", data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    event, provisional = "", []
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=600) as response:
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            if not line.startswith("data:"):
                continue
            data = json.loads(line[5:])
            if event == "delta":
                if first is None: first = time.perf_counter() - started
                provisional.append(data["content"])
            elif event == "error":
                raise RuntimeError(data.get("detail", "HOST model error"))
            elif event == "done":
                if not provisional or first is None:
                    raise RuntimeError("No visible streaming deltas")
                data["client_ttft_seconds"] = first
                return data
    raise RuntimeError("Stream ended without validated done event")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--evaluate", action="store_true", help="Send synthetic conversations to an already explicitly started Strata")
    parser.add_argument("--output-dir", type=Path, default=Path("data/strata-evaluation"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    from urllib.parse import urlsplit
    from ipaddress import ip_address
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        parser.error("Use a fixed HTTP(S) HOST origin without credentials, path or query")
    host = parsed.hostname
    if not (host == "localhost" or host and host.endswith(".ts.net")):
        try:
            if not ip_address(host).is_loopback: raise ValueError
        except ValueError: parser.error("Use the reviewed loopback HOST or existing HTTPS tailnet URL")
    if host.endswith(".ts.net") and urlsplit(base).scheme != "https": parser.error("Tailnet DNS requires HTTPS")
    preflight = request(base, "/api/models/strata/preflight")
    print(json.dumps(preflight, ensure_ascii=False))
    if not args.evaluate:
        print("READ_ONLY: no model start/stop or conversation POST. Use --evaluate only after approved manual startup.")
        return
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.output_dir / f"conversation-{int(time.time())}.jsonl"
    cases = [
        ("japanese", "日本語で短く挨拶してください。これからの会話では計画名『青空』、予算7000円を覚えてください。"),
        ("reasoning", "Aの後にBとCを並行実施し、両方完了したらDを行います。Aは2日、Bは3日、Cは5日、Dは1日です。最短日数と依存関係を簡潔に説明してください。"),
        ("instructions", "睡眠の基本を箇条書き3項目、各20字以内で書き、最後に『以上』だけを書いてください。"),
        ("coding", "Pythonで整数リストの平均を返し、空ならNoneを返す関数と3件のpytest例を示してください。"),
        ("long_context", "以下は架空の会議メモです。予算や最初の計画名は変更しません。要点を1文でまとめてください。\n" +
         "担当者は資料を整理し、期限を確認し、レビュー後に利用者へ説明する。資料の版、引用、検証結果を保存する。" * 40),
        ("context", "最初に指定した計画名と予算を、ほかの説明なしで1行だけ答えてください。"),
    ]
    cid = None
    with path.open("w", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        for category, prompt in cases:
            payload = {"model": "strata", "message": prompt, "conversation_id": cid,
                       "strata_options": {"temperature": 0.2, "max_tokens": 4096,
                                          "reasoning_effort": "low" if category == "reasoning" else "none",
                                          "reasoning_budget_tokens": 512}}
            result = stream(base, payload)
            cid = result["conversation_id"]
            # Do not persist raw thinking/debug. Final answer and safe metrics only.
            record = {"category": category, "conversation_id": cid, "prompt": prompt, "reply": result["reply"],
                      "generation_metrics": result.get("generation_metrics"), "client_ttft_seconds": result["client_ttft_seconds"]}
            output.write(json.dumps(record, ensure_ascii=False) + "\n"); output.flush()
            print(category, json.dumps(record["generation_metrics"], ensure_ascii=False))
            if category == "context" and not ("青空" in result["reply"] and "7000" in result["reply"]):
                raise RuntimeError("Context retention check failed; inspect private final answers")
    history = request(base, f"/api/conversations/{cid}")
    if len(history["messages"]) != 2 * len(cases): raise RuntimeError("Saved multi-turn history mismatch")
    print("PASS: real HOST streaming / Japanese final answers / multi-turn marker / saved history", path)
    print("Naturalness, reasoning and coding quality require reading the saved final answers; no automatic quality guarantee.")


if __name__ == "__main__":
    main()
