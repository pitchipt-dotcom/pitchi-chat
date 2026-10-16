"""Probe control-flow mocks; never real 80B, GPU, external services or cloud."""

import httpx
import pytest
from scripts.test_qwen80b_e2e import probe
from scripts.test_pt_assistant import run_e2e


def test_80b_probe_blocked_never_stops_or_requests_chat(capsys):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/gpu": return httpx.Response(200,json={"state":"STOPPED","loaded_model":None})
        if request.url.path == "/api/resources": return httpx.Response(200,json={"active_job":None})
        return httpx.Response(409,json={"detail":"resource unavailable"})
    with httpx.Client(transport=httpx.MockTransport(handle),base_url="http://host") as client:
        probe(client,"http://host","http://localhost/health","clinical","test")
    assert calls == [("GET","/api/gpu"),("GET","/api/resources"),("POST","/api/models/qwen80b/lifecycle")]
    assert "SKIPPED" in capsys.readouterr().out


def test_80b_probe_does_not_stop_existing_owned_model(capsys):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200,json={"state":"READY","loaded_model":"qwen80b"} if request.url.path=="/api/gpu" else {"active_job":None})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        probe(client,"http://host","http://localhost/health","clinical","test")
    assert calls == ["GET","GET"] and "SKIPPED" in capsys.readouterr().out


@pytest.mark.parametrize("selection", [{"actual_model_used":"qwen", "fallback_reason":"external_gpu_workload"}, {}])
def test_strict_requested_model_probe_rejects_cpu_success(selection):
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"model_selection":selection}))) as client:
        with pytest.raises(SystemExit,match="not counted"):
            run_e2e(client,"http://host","clinical","qwen80b","test",require_actual=True)


def test_80b_probe_failure_still_stops_owned_model(monkeypatch):
    from scripts import test_qwen80b_e2e
    requests = []
    gpu_count = 0
    def handle(request):
        nonlocal gpu_count
        requests.append((request.url.path,request.content))
        if request.url.path=="/api/gpu":
            gpu_count += 1
            return httpx.Response(200,json={"state":"STOPPED","loaded_model":None,"error":None})
        if request.url.path=="/api/resources":return httpx.Response(200,json={"active_job":None})
        if request.url.path=="/health":return httpx.Response(200,json={"status":"ok"})
        return httpx.Response(200,json={"model":"qwen80b","state":"READY" if b'start' in request.content else "STOPPED"})
    def fail(*args,**kwargs):raise SystemExit("synthetic failed E2E")
    monkeypatch.setattr(test_qwen80b_e2e,"run_e2e",fail)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SystemExit,match="failed E2E"):
            probe(client,"http://host","http://localhost/health","clinical","test")
    assert gpu_count == 2 and any(b'stop' in body for _,body in requests)


@pytest.mark.parametrize("stop_status", [409, 502])
def test_80b_probe_stop_failure_never_claims_release(monkeypatch, capsys, stop_status):
    from scripts import test_qwen80b_e2e
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == "/api/gpu": return httpx.Response(200,json={"state":"STOPPED","loaded_model":None})
        if request.url.path == "/api/resources": return httpx.Response(200,json={"active_job":None})
        if request.url.path == "/health": return httpx.Response(200,json={"status":"ok"})
        if b'stop' in request.content: return httpx.Response(stop_status,json={"detail":"stop rejected"})
        return httpx.Response(200,json={"model":"qwen80b","state":"READY"})
    monkeypatch.setattr(test_qwen80b_e2e,"run_e2e",lambda *a,**kw:None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError): probe(client,"http://host","http://localhost/health","clinical","test")
    assert len([r for r in calls if b'stop' in r.content]) == 1
    assert "VERIFIED" not in capsys.readouterr().out
