"""HOST-owned model bindings. UI selects logical IDs, never endpoints or commands."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import httpx

from backend.app.config import ModelSettings, Settings, parse_model_settings
from backend.app.models.base import ModelAdapter, ModelNotConfigured, ProposalAdapter
from backend.app.models.bonsai import BonsaiAdapter
from backend.app.models.qwen import QwenAdapter
from backend.app.models.strata import StrataAdapter
from backend.app.models.qwen35 import Qwen35Adapter

LABELS = MappingProxyType({"qwen": "Qwen", "bonsai": "BONSAI", "qwen80b": "Qwen3-Next-80B-A3B", "strata": "Strata IQ2_XS", "vision": "Qwen3.5-9B-Uncensored"})


@dataclass(frozen=True)
class ModelBinding:
    settings: ModelSettings
    adapter: ModelAdapter | ProposalAdapter
    available: bool
    injected: bool = False


class ModelRegistry:
    def __init__(self, settings: Settings, client: httpx.AsyncClient,
                 overrides: Mapping[str, ModelAdapter | ProposalAdapter] | None = None,
                 managed_profiles: frozenset[str] | None = None):
        overrides = dict(overrides or {})
        if set(overrides) - LABELS.keys() or settings.default_model not in LABELS:
            raise ValueError("Unknown HOST model ID")
        specs = {spec.id: spec for spec in settings.models}
        if len(specs) != len(settings.models) or set(specs) - LABELS.keys():
            raise ValueError("Invalid HOST model registry")
        # Preserve all five original Settings fields for existing v0.1 callers.
        specs.setdefault("qwen", ModelSettings("qwen", "qwen", settings.endpoint, settings.model_name,
                                               settings.api_key_env, settings.timeout_seconds))
        specs.setdefault("bonsai", parse_model_settings("bonsai", {}))
        if "qwen80b" in overrides or settings.default_model == "qwen80b":
            specs.setdefault("qwen80b", parse_model_settings("qwen80b", {}))
        if "strata" in overrides or settings.default_model == "strata":
            specs.setdefault("strata", parse_model_settings("strata", {}))
        if "vision" in overrides or settings.default_model == "vision":
            specs.setdefault("vision", parse_model_settings("vision", {}))
        bindings = {}
        for model_id, spec in specs.items():
            spec = parse_model_settings(model_id, {"adapter": spec.adapter, "endpoint": spec.endpoint,
                                                   "name": spec.model_name, "api_key_env": spec.api_key_env,
                                                   "timeout_seconds": spec.timeout_seconds})
            adapter_settings = Settings(settings.database, spec.endpoint, spec.model_name,
                                        spec.api_key_env, spec.timeout_seconds)
            factory = Qwen35Adapter if model_id == "vision" else StrataAdapter if model_id == "strata" else BonsaiAdapter if model_id == "bonsai" else QwenAdapter
            bindings[model_id] = ModelBinding(spec, overrides.get(model_id) or factory(adapter_settings, client),
                                               model_id in overrides or bool(spec.endpoint) and
                                               (managed_profiles is None or model_id in managed_profiles), model_id in overrides)
        self._bindings = MappingProxyType(bindings)
        self.default_model = settings.default_model
        self.managed_profiles = managed_profiles or frozenset()

    def resolve(self, model_id: str | None = None) -> ModelBinding:
        binding = self._bindings.get(self.default_model if model_id is None else model_id)
        if binding is None or not binding.available:
            # No untrusted IDs, endpoint URLs, model names or credentials in errors.
            raise ModelNotConfigured("選択モデルの接続先が未設定、または利用できません。")
        return binding

    def public_state(self) -> dict:
        return {"default_model": self.default_model, "process_management": bool(self.managed_profiles),
                "models": [{"id": model_id, "label": LABELS[model_id],
                            "configured": bool(self._bindings[model_id].settings.endpoint),
                            "available": self._bindings[model_id].available,
                            "connection_mode": "host_owned" if model_id in self.managed_profiles else "external"}
                           for model_id in LABELS if model_id in self._bindings]}
