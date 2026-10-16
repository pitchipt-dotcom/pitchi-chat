"""Independent HOST cloud configuration. Empty/disabled defaults; no literal keys."""

import os
from pathlib import Path
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from backend.app.config import ROOT
from backend.app.harness.contracts import Contract


class ProviderSettings(Contract):
    enabled: bool = False
    endpoint: str = ""
    model: str = Field(default="", max_length=120)
    api_key_env: str = Field(default="", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*)?$")
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"

    @field_validator("endpoint")
    @classmethod
    def official_endpoint(cls, value):
        if value:
            p = urlsplit(value)
            if (p.scheme != "https" or p.hostname != "api.openai.com" or p.port not in {None, 443}
                    or p.path != "/v1/responses" or p.username or p.password or p.query or p.fragment):
                raise ValueError("Only an explicitly configured official Responses endpoint is supported")
        return value

    @model_validator(mode="after")
    def configured(self):
        if self.enabled and (not self.endpoint or not self.model.strip() or not self.api_key_env):
            raise ValueError("Enabled provider requires endpoint, model and credential env name")
        return self


class CloudSettings(Contract):
    enabled: bool = False
    reviewed_public_payloads: tuple[str, ...] = ()
    reviewed_render_pairs: tuple[str, ...] = ()
    reasoning_fallback: ProviderSettings = ProviderSettings()
    japanese_renderer: ProviderSettings = ProviderSettings()

    @field_validator("reviewed_public_payloads", "reviewed_render_pairs")
    @classmethod
    def digests(cls, values):
        import re
        if len(values) > 100 or any(not re.fullmatch(r"[a-f0-9]{64}", v) for v in values):
            raise ValueError("Only bounded HOST-reviewed SHA256 digests are accepted")
        return values

    @model_validator(mode="after")
    def renderer_effort(self):
        if self.japanese_renderer.reasoning_effort != "none":
            raise ValueError("Japanese Renderer is presentation only, reasoning must be none")
        return self


def load_cloud(path: Path | None = None):
    path = path or Path(os.environ.get("PITCH_CHAT_CLOUD_CONFIG", ROOT / "config/cloud.toml"))
    with path.open("rb") as file:
        data = tomllib.load(file)
    # TOML arrays become tuples; all other strict types/unknown fields remain checked.
    for name in ("reviewed_public_payloads", "reviewed_render_pairs"):
        if name in data and isinstance(data[name], list):
            data[name] = tuple(data[name])
    return CloudSettings.model_validate(data)
