"""File-based configuration; secrets are read only from the environment."""

import os
import math
import re
import tomllib
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelSettings:
    id: str
    adapter: str
    endpoint: str
    model_name: str
    api_key_env: str
    timeout_seconds: float


@dataclass(frozen=True)
class Settings:
    database: Path
    endpoint: str
    model_name: str
    api_key_env: str
    timeout_seconds: float
    models: tuple[ModelSettings, ...] = ()
    default_model: str = "qwen"
    library_evidence_limit: int = 3
    library_snippet_chars: int = 800


def read_config(path: Path | None = None) -> tuple[Path, dict]:
    path = (path or Path(os.environ.get("PITCH_CHAT_CONFIG", ROOT / "config/settings.toml"))).resolve()
    with path.open("rb") as file:
        config = tomllib.load(file)
    return path, config


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int


def load_server_settings(path: Path | None = None) -> ServerSettings:
    _, config = read_config(path)
    server = config.get("server", {})
    host = os.environ.get("PITCH_CHAT_HOST", server.get("host"))
    port = os.environ.get("PITCH_CHAT_PORT", server.get("port"))
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Set server.host in TOML or PITCH_CHAT_HOST in the environment")
    host = host.strip()
    if host != "localhost":
        address = ip_address(host)
        tailnet = ip_network("100.64.0.0/10") if address.version == 4 else ip_network("fd7a:115c:a1e0::/48")
        if not (address.is_loopback or address in tailnet):
            raise ValueError("server.host must be a loopback or Tailscale IP; wildcard/LAN/public binding is disabled")
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ValueError("Set server.port in TOML or PITCH_CHAT_PORT in the environment")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("server.port must be between 1 and 65535")
    return ServerSettings(host, port)


def load_settings(path: Path | None = None) -> Settings:
    path, config = read_config(path)
    models = config.get("models", {})
    if not isinstance(models, dict) or set(models) - {"qwen", "bonsai", "qwen80b", "strata", "vision"}:
        raise ValueError("Unknown HOST logical model ID")
    if "model" in config and "qwen" in models:
        raise ValueError("Configure qwen in either model or models.qwen, not both")
    qwen = parse_model_settings("qwen", config.get("model", models.get("qwen", {})))
    bonsai = parse_model_settings("bonsai", models.get("bonsai", {}))
    chat = config.get("chat", {})
    if not isinstance(chat, dict) or set(chat) - {"default_model"}:
        raise ValueError("Invalid chat configuration fields")
    default = chat.get("default_model", "qwen")
    if not isinstance(default, str) or default not in {"qwen", "bonsai", "qwen80b", "strata", "vision"}:
        raise ValueError("Unknown HOST default model ID")
    database = Path(config["storage"]["database"])
    library = config.get("library", {})
    if not isinstance(library, dict) or set(library) - {"evidence_limit", "snippet_chars"}:
        raise ValueError("Unknown library budget field")
    evidence_limit = library.get("evidence_limit", 3)
    snippet_chars = library.get("snippet_chars", 800)
    if type(evidence_limit) is not int or not 1 <= evidence_limit <= 3 or type(snippet_chars) is not int or not 80 <= snippet_chars <= 800:
        raise ValueError("Invalid library evidence budget")
    specs = (qwen, bonsai)
    if "qwen80b" in models or default == "qwen80b":
        specs += (parse_model_settings("qwen80b", models.get("qwen80b", {})),)
    if "strata" in models or default == "strata":
        specs += (parse_model_settings("strata", models.get("strata", {})),)
    if "vision" in models or default == "vision":
        specs += (parse_model_settings("vision", models.get("vision", {})),)
    return Settings((path.parent / database).resolve(), qwen.endpoint, qwen.model_name,
                    qwen.api_key_env, qwen.timeout_seconds, specs, default, evidence_limit, snippet_chars)


def parse_model_settings(model_id: str, model: dict) -> ModelSettings:
    if not isinstance(model, dict) or set(model) - {"adapter", "endpoint", "name", "api_key_env", "timeout_seconds"}:
        raise ValueError("Invalid model configuration fields")
    expected_adapter = "qwen" if model_id in {"qwen80b", "vision"} else model_id
    if model.get("adapter", expected_adapter) != expected_adapter:
        raise ValueError("Model adapter must match its HOST logical ID")
    endpoint = model.get("endpoint", "")
    name = model.get("name", f"{model_id}-local")
    key_env = model.get("api_key_env", "")
    if not all(isinstance(value, str) for value in (endpoint, name, key_env)):
        raise ValueError("Model endpoint, name and api_key_env must be strings")
    endpoint = endpoint.strip()
    parsed = urlsplit(endpoint)
    if endpoint and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                     or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("model.endpoint must be an HTTP(S) URL without embedded credentials or query parameters")
    raw_timeout = model.get("timeout_seconds", 60)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ValueError("Model timeout must be numeric")
    timeout = float(raw_timeout)
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError("model.timeout_seconds must be between 0 and 600")
    name = name.strip()
    if not name:
        raise ValueError("model.name must not be empty")
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("api_key_env must be an environment variable name")
    return ModelSettings(model_id, expected_adapter, endpoint, name, key_env, timeout)
