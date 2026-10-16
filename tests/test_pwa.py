import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_static_routes_are_pwa_ready():
    manifest = json.loads((ROOT / "frontend/manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] and manifest["background_color"]
    icons = {icon["sizes"]: icon for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= icons.keys()
    for icon in icons.values():
        path = ROOT / "frontend" / icon["src"].removeprefix("/static/")
        assert path.is_file()
        assert icon["type"] == "image/png"
        assert "maskable" in icon["purpose"]
        png = path.read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", png[16:24])
        assert width == height == int(icon["sizes"].split("x")[0])
    app_source = (ROOT / "backend/app/main.py").read_text()
    assert '"/manifest.webmanifest"' in app_source
    assert '"/sw.js"' in app_source
    assert '"Service-Worker-Allowed": "/"' in app_source


def test_manifest_and_worker_routes_serve_static_files(tmp_path):
    from backend.app.config import Settings
    from backend.app.main import create_app
    from backend.app.providers.config import CloudSettings
    from backend.app.resources.config import ResourceSettings
    from backend.app.tools.image_workflows import ImageWorkflowRegistry
    from backend.app.tools.work_config import WorkSettings

    app = create_app(Settings(tmp_path / "history.sqlite3", "", "", "", 2), services=(),
                     resource_settings=ResourceSettings(), image_workflows=ImageWorkflowRegistry(),
                     cloud_settings=CloudSettings(), work_settings=WorkSettings())
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    manifest = routes["/manifest.webmanifest"]()
    worker = routes["/sw.js"]()
    assert Path(manifest.path) == ROOT / "frontend/manifest.webmanifest"
    assert manifest.media_type == "application/manifest+json"
    assert manifest.headers["cache-control"] == "no-cache"
    assert Path(worker.path) == ROOT / "frontend/sw.js"
    assert worker.headers["cache-control"] == "no-cache"
    assert worker.headers["service-worker-allowed"] == "/"


def test_mobile_layout_and_offline_notice_are_present():
    html = (ROOT / "frontend/index.html").read_text()
    css = (ROOT / "frontend/style.css").read_text()
    script = (ROOT / "frontend/chat.js").read_text()
    assert "viewport-fit=cover" in html and "interactive-widget=resizes-content" in html
    assert 'id="chat-mode"' in html and 'id="resource-panel"' in html
    assert 'id="chat-panel"' in html and 'id="management-menu"' in html
    assert html.index('id="chat-model"') < html.index('id="chat-panel"') < html.index('id="management-menu"')
    assert "Pitchi-chat HOSTへ接続できません" in html and "Pitchi-chat HOSTへ接続できません" in script
    assert "max-width: 550px" in css and "max-width: 380px" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "#chat-panel { min-height: 0; height: 100%; overflow: hidden; }" in css
    assert "#chat-form { flex: 0 0 auto;" in css
    assert "user-select: text" in css and "function renderMarkdown(root, source)" in script
    assert "function copyButton(value, label)" in script and 'id="management-menu"' in html
    assert "max-height: 55vh" not in css


def test_service_worker_mock_cache_isolation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Service Worker mock checks")
    result = subprocess.run([node, "tests/pwa_service_worker.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cache-isolation checks passed" in result.stdout


def test_versioned_shell_assets_match_worker_allowlist():
    from html.parser import HTMLParser

    class AssetParser(HTMLParser):
        assets = []
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag == "script":
                assert "defer" in attrs, "DOM handlers require a deferred app script"
                self.assets.append(attrs["src"])
            if tag == "link" and attrs.get("rel") == "stylesheet":
                self.assets.append(attrs["href"])

    parser = AssetParser()
    parser.feed((ROOT / "frontend/index.html").read_text())
    worker = (ROOT / "frontend/sw.js").read_text()
    assert len(parser.assets) == 8
    for asset in parser.assets:
        assert "?v=" in asset, "versioned assets bypass previously installed cache-first workers"
        assert f'"{asset}"' in worker, "offline shell must cache the exact URLs used by HTML"


def test_pwa_connection_state_matrix():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for connection state checks")
    result = subprocess.run([node, "tests/pwa_connection.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "connection state checks passed" in result.stdout


def test_pwa_in_isolated_real_chrome():
    node = shutil.which("node")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if node is None or chrome is None:
        pytest.skip("Node.js and Chrome are required for real browser PWA checks")
    result = subprocess.run([node, "tests/pwa_browser.cjs", chrome], cwd=ROOT, capture_output=True, text=True, timeout=75)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PWA browser checks passed" in result.stdout
