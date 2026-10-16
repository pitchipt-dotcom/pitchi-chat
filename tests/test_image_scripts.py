"""Opt-in diagnostics are tested with mocks; no model, network or GPU access."""

import asyncio
from types import SimpleNamespace

import pytest

from scripts import probe_comfy_images, test_image_compiler


@pytest.mark.parametrize("outcome", ["success", "submission_denied", "recovery"])
def test_compiler_probe_never_generates_or_replays(monkeypatch, capsys, outcome):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json):
            calls.append(("POST", url, json))
            assert json["compile_only"] is True and "workflow" not in json
            return SimpleNamespace(status_code=403 if outcome == "submission_denied" else 202,
                                   json=lambda: {"job_id": "synthetic-job"})

        def get(self, url):
            calls.append(("GET", url, None))
            if url.endswith("/result"):
                data = {"image_spec": {"purpose": "poster", "subject": "synthetic cat", "style": "watercolor",
                        "composition": "centered", "aspect_ratio": "1:1", "text_requirement": ""},
                        "tool_result": None, "cloud_used": False}
            else:
                data = {"status": "RECOVERY_REQUIRED" if outcome == "recovery" else "SUCCESS",
                        "image_phase": "SPEC_READY", "remote_run_id": None}
            return SimpleNamespace(json=lambda: data)

    monkeypatch.setattr(test_image_compiler.httpx, "Client", Client)
    monkeypatch.setattr(test_image_compiler, "load_server_settings", lambda _: SimpleNamespace(host="localhost", port=1234))
    monkeypatch.setattr("sys.argv", ["compiler"])
    if outcome == "success":
        test_image_compiler.main()
        assert "persisted=SPEC_READY" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit):
            test_image_compiler.main()
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert all("/prompt" not in url for _, url, _ in calls)


def test_catalog_probe_rejects_legacy_ui_before_network(monkeypatch):
    service = SimpleNamespace(tool="image.generate", api_base_url="http://localhost", image_protocol="")
    monkeypatch.setattr(probe_comfy_images, "load_services", lambda _: (service,))
    with pytest.raises(SystemExit, match="legacy UI URLs are not inferred"):
        asyncio.run(probe_comfy_images.probe(None, None))
