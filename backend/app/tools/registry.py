"""HOST-owned tool definitions. The model supplies only a logical proposal."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from backend.app.harness.contracts import ResultType, ToolResult
from backend.app.harness.recovery import RecoveryPolicy
from backend.app.resources.gpu import ResourceMode


class ToolAdapter(Protocol):
    async def execute(self, arguments: BaseModel, backend: str | None) -> ToolResult: ...


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    adapter: ToolAdapter
    argument_schema: type[BaseModel]
    output_schema: type[BaseModel]
    result_type: ResultType
    mode: ResourceMode
    enabled: bool = False
    allowed_backends: frozenset[str] = frozenset()
    default_backend: str | None = None
    allowed_roots: tuple[Path, ...] = ()
    allowed_urls: frozenset[str] = frozenset()
    allow_private: bool = False
    cloud: bool = False
    requires_managed_gpu: bool = True
    requires_exit_status: bool = False
    requires_http_status: bool = False
    min_artifacts: int = 0
    resource_profile: str | None = None
    remote_job: bool = False
    requires_image_compilation: bool = False
    remote_resource: bool | None = None
    read_only_evidence: bool = False
    external_retrieval: bool = False
    recovery: RecoveryPolicy = RecoveryPolicy()

    def __post_init__(self):
        if type(self.min_artifacts) is not int or not 0 <= self.min_artifacts <= 20:
            raise ValueError("Invalid artifact requirement")
        for name in [self.name, *self.allowed_backends]:
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name):
                raise ValueError("registry names must be logical identifiers")
        if self.default_backend is not None and self.default_backend not in self.allowed_backends:
            raise ValueError("default backend must be allowed")
        if self.argument_schema.model_config.get("extra") != "forbid":
            raise ValueError("tool argument schemas must forbid extra fields")
        if self.output_schema.model_config.get("extra") != "forbid":
            raise ValueError("tool output schemas must forbid extra fields")
        if self.mode == ResourceMode.IDLE:
            raise ValueError("tool must declare a non-idle resource mode")


class ToolRegistry:
    def __init__(self, definitions: tuple[ToolDefinition, ...] = ()):
        self._definitions = {definition.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("duplicate tool registration")

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)
