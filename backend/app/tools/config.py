"""Private, configuration-only service locations. No automatic UI-to-API guessing."""

from dataclasses import dataclass
from ipaddress import ip_address, ip_network
import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from backend.app.config import ROOT

TOOL_IDS = ("tts.generate", "image.generate", "video.generate", "book.search", "ptotsearch.search", "image.understand")


@dataclass(frozen=True)
class ServiceSettings:
    tool: str
    ui_url: str = ""
    api_base_url: str = ""
    enabled: bool = False
    timeout_seconds: float = 20.0
    backends: tuple[tuple[str, str], ...] = ()
    resource_profile: str | None = None
    host_lifecycle_only: bool = False
    api_key_env: str = ""
    image_protocol: str = ""
    public_retrieval: bool = False
    generation_backends: tuple[str, ...] = ()


def validate_public_retrieval_location(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or "." not in parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port not in {None, 443}):
        raise ValueError("Public retrieval requires an explicit HTTPS origin")


def validate_location(value: str):
    if not isinstance(value, str):
        raise ValueError("Service location must be a string")
    if not value:
        return
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Service base URL must be an origin without credentials or query")
    try:
        address = ip_address(parsed.hostname)
        allowed = address.is_loopback or address in ip_network("100.64.0.0/10") or address in ip_network("fd7a:115c:a1e0::/48")
    except ValueError:
        allowed = parsed.hostname == "localhost" or parsed.hostname.endswith(".ts.net")
    if not allowed or (parsed.hostname.endswith(".ts.net") and parsed.scheme != "https"):
        raise ValueError("Service location must be loopback or private tailnet (HTTPS for DNS)")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid service port") from None


def load_services(path: Path | None = None) -> tuple[ServiceSettings, ...]:
    path = path or Path(os.environ.get("PITCH_CHAT_SERVICES_CONFIG", ROOT / "config/services.toml"))
    with path.open("rb") as file:
        config = tomllib.load(file)
    services = config.get("services", {})
    if not isinstance(services, dict) or set(services) - set(TOOL_IDS):
        raise ValueError("Unknown logical service ID")
    result = []
    for tool in TOOL_IDS:
        entry = services.get(tool, {})
        if not isinstance(entry, dict) or set(entry) - {"ui_url", "api_base_url", "enabled", "timeout_seconds", "backends", "resource_profile", "host_lifecycle_only", "api_key_env", "image_protocol", "public_retrieval", "generation_backends"}:
            raise ValueError("Invalid service configuration fields")
        ui_url, api_url = entry.get("ui_url", ""), entry.get("api_base_url", "")
        public = entry.get("public_retrieval", False)
        if type(public) is not bool or public and tool != "ptotsearch.search":
            raise ValueError("Public retrieval is restricted to explicit PTOTsearch configuration")
        for location in (ui_url, api_url):
            if public and location:
                validate_public_retrieval_location(location)
            else:
                validate_location(location)
        enabled, timeout = entry.get("enabled", False), entry.get("timeout_seconds", 20.0)
        if type(enabled) is not bool or type(timeout) not in {float, int} or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("Invalid service enable/timeout setting")
        profile = entry.get("resource_profile")
        reviewed = entry.get("host_lifecycle_only", False)
        key_env = entry.get("api_key_env", "")
        protocol = entry.get("image_protocol", "")
        if protocol not in {"", "comfyui"} or protocol and tool != "image.generate":
            raise ValueError("Image protocol must be explicitly verified ComfyUI")
        if enabled and tool == "image.generate" and protocol != "comfyui":
            raise ValueError("Legacy UI origin cannot be assumed to be ComfyUI")
        if (profile is not None and (not isinstance(profile, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", profile))) or type(reviewed) is not bool:
            raise ValueError("Invalid lifecycle contract")
        if not isinstance(key_env, str) or key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ValueError("api_key_env must name an environment variable")
        if enabled and (not api_url or (tool not in {"book.search", "ptotsearch.search"} and (tool == "video.generate" or not profile or not reviewed))):
            raise ValueError("GPU enable requires reviewed HOST-owned lifecycle; video remains blocked")
        backends = entry.get("backends", {})
        if not isinstance(backends, dict) or any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", key)
                or not isinstance(value, str) or not value.strip() or any(token in value for token in ("/", "\\", ".."))
                for key, value in backends.items()):
            raise ValueError("Invalid HOST backend mapping")
        generation = entry.get("generation_backends", [])
        if (not isinstance(generation, list) or any(not isinstance(name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name) for name in generation)
                or len(generation) != len(set(generation)) or generation and tool != "image.generate"
                or "generation_backends" in entry and not generation):
            raise ValueError("Invalid image generation allowlist")
        result.append(ServiceSettings(tool, ui_url, api_url, enabled, float(timeout), tuple(backends.items()), profile, reviewed, key_env, protocol, public, tuple(generation)))
    return tuple(result)
