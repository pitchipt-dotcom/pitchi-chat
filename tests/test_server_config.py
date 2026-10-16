import pytest

from backend.app.config import ROOT, load_server_settings
from backend.app import serve


@pytest.fixture(autouse=True)
def clean_server_environment(monkeypatch):
    for name in ["PITCH_CHAT_HOST", "PITCH_CHAT_PORT", "PITCH_CHAT_CONFIG"]:
        monkeypatch.delenv(name, raising=False)


def test_default_preserves_localhost():
    settings = load_server_settings(ROOT / "config/settings.toml")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


def test_custom_file_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / "server.toml"
    path.write_text('[server]\nhost = "::1"\nport = 8002\n')
    monkeypatch.setenv("PITCH_CHAT_CONFIG", str(path))
    assert load_server_settings().host == "::1"
    assert load_server_settings().port == 8002
    monkeypatch.setenv("PITCH_CHAT_HOST", "127.0.0.2")
    monkeypatch.setenv("PITCH_CHAT_PORT", "8003")
    assert load_server_settings().host == "127.0.0.2"
    assert load_server_settings().port == 8003


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "8.8.8.8", "example.com", "", "::ffff:192.168.1.2"])
def test_reject_nonprivate_bindings(monkeypatch, host):
    monkeypatch.setenv("PITCH_CHAT_HOST", host)
    with pytest.raises(ValueError):
        load_server_settings()


@pytest.mark.parametrize("host", ["localhost", "::1", "100." + "64.0.1", "100." + "127.255.254", "fd7a:115c:a1e0::1"])
def test_loopback_and_tailnet_addresses(monkeypatch, host):
    monkeypatch.setenv("PITCH_CHAT_HOST", host)
    assert load_server_settings().host == host


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port", ""])
def test_reject_bad_ports(monkeypatch, port):
    monkeypatch.setenv("PITCH_CHAT_PORT", port)
    with pytest.raises(ValueError):
        load_server_settings()


def test_launcher_uses_configuration(monkeypatch):
    monkeypatch.setenv("PITCH_CHAT_HOST", "::1")
    monkeypatch.setenv("PITCH_CHAT_PORT", "8100")
    calls = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    serve.main()
    assert calls == [(("backend.app.main:create_app",),
                      {"factory": True, "host": "::1", "port": 8100, "workers": 1})]


@pytest.mark.parametrize("budget", [
    'evidence_limit = 0', 'evidence_limit = 4', 'evidence_limit = true',
    'snippet_chars = 79', 'snippet_chars = 801', 'snippet_chars = 160.0',
    'unknown = 1',
])
def test_library_evidence_budget_is_bounded(tmp_path, budget):
    from backend.app.config import load_settings
    path = tmp_path / "settings.toml"
    path.write_text('[storage]\ndatabase = "history.db"\n[library]\n' + budget)
    with pytest.raises(ValueError):
        load_settings(path)


def test_library_cpu_budget_configuration(tmp_path):
    from backend.app.config import load_settings
    path = tmp_path / "settings.toml"
    path.write_text('[storage]\ndatabase = "history.db"\n[library]\nevidence_limit = 1\nsnippet_chars = 160\n')
    settings = load_settings(path)
    assert settings.library_evidence_limit == 1
    assert settings.library_snippet_chars == 160
