from scripts.check_public_repo import content_issue, path_issue

import pytest


@pytest.mark.parametrize("path", [
    ".env", ".env.production", "config/settings.local.toml", "credentials.json", "tokens.json",
    "data/chat.db", "history.sqlite3-wal", "private/patients.csv", "datasets/private.csv",
    "models/model.gguf", "weights/model.safetensors", "output/generated.png", "audio.wav",
    "runtime.log", "backup.zip", "keys/private.pem", "id_ed25519",
])
def test_forbidden_public_paths(path):
    assert path_issue(path)


@pytest.mark.parametrize("path", [".env.example", "backend/app/models/qwen.py", "config/settings.toml", "docs/screenshots/chat.png",
                                  "frontend/icons/pitch-chat-192.png", "frontend/icons/pitch-chat-512.png"])
def test_allowed_source_and_documentation(path):
    assert path_issue(path) is None


def test_personal_paths_and_hosts_are_not_public():
    # Construct synthetic identifiers; no developer-specific identifier in source.
    personal_path = b"/home/" + b"example-user/app"
    tailnet_host = b"example-device" + b".example-tailnet" + b".ts.net"
    assert content_issue("README.md", personal_path)
    assert content_issue("README.md", tailnet_host)


def test_runtime_binary_and_oversize_are_blocked():
    assert content_issue("unexpected.txt", b"private\0binary")
    assert content_issue("oversize.txt", b"x" * (5 * 1024 * 1024 + 1))


def test_public_template_endpoint_must_be_empty():
    assert content_issue("config/settings.toml", b'[model]\nendpoint = ""\n') is None
    assert content_issue("config/settings.toml", b'[model]\nendpoint = "http://private.test/chat"\n')
    assert content_issue("config/settings.toml", b'[model]\nendpoint = ""\n[models.bonsai]\nendpoint = "http://private.test/chat"\n')
    assert content_issue("config/services.toml", b'[services."book.search"]\nui_url = ""\napi_base_url = ""\nenabled = false\n') is None
    assert content_issue("config/services.toml", b'[services."book.search"]\nui_url = "http://private.test"\n')


def test_tailnet_ip_not_committed():
    synthetic_ip = b"100." + b"100.200.201"
    assert content_issue("source.py", synthetic_ip)
    assert content_issue("network-policy.py", b'"100.64.0.0/10"') is None
