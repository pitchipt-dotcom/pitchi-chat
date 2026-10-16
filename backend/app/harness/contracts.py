"""Explicit machine contracts. No infrastructure authority comes from model output."""

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from backend.app.resources.gpu import Lifecycle, ResourceMode

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolCall(Contract):
    tool: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    backend: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ModelProposal(Contract):
    kind: Literal["answer", "tool"]
    content: str | None = Field(default=None, min_length=1, max_length=1000000)
    tool_call: ToolCall | None = None

    @model_validator(mode="after")
    def exclusive_branch(self):
        if self.kind == "answer":
            if not self.content or not self.content.strip() or self.tool_call is not None:
                raise ValueError("answer requires only nonblank content")
        elif self.tool_call is None or self.content is not None:
            raise ValueError("tool requires only a tool_call")
        return self


ResultType = Literal["text", "structured", "audio", "image", "run"]


class Artifact(Contract):
    path: str = Field(min_length=1, max_length=4096)
    kind: Literal["audio", "image", "file"]


class ToolResult(Contract):
    success: bool
    result_type: ResultType
    output: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    exit_status: int | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    timed_out: bool = False
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_status: Literal["completed", "failed", "running"] | None = None


class RenderRequest(Contract):
    path: Literal["machine", "presentation"]
    format: Literal["natural_language", "json", "yaml", "code", "command", "api_payload",
                    "tool_args", "config", "structured", "strict_numbers"]
    content: str
    renderer: Literal["direct", "luna", "llmjp"] = "direct"

    @model_validator(mode="after")
    def machine_never_renders(self):
        if (self.path == "machine" or self.format != "natural_language") and self.renderer != "direct":
            raise ValueError("machine/structured content cannot use a renderer")
        return self


class ResourceJob(Contract):
    id: str
    mode: str
    model: str | None
    tool: str | None
    started_at: str


class ResourceState(Contract):
    mode: ResourceMode
    lifecycle: Lifecycle
    active_job: ResourceJob | None
    waiting_jobs: int = Field(ge=0)
    running_model: str | None
    running_tool: str | None
    loaded_model: str | None
    last_failure: str | None
    scope: Literal["host_single_worker"]
    gpu_telemetry: Literal["unavailable", "available"]
    process_management: bool
    external_gpu_workloads: Literal["unmanaged", "managed"]


class FinalStatus(str, Enum):
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    ERROR = "ERROR"
    STALLED = "STALLED"
    CANCELLED = "CANCELLED"


class ErrorCode(str, Enum):
    SCHEMA_INVALID = "schema_invalid"
    POLICY_DENIED = "policy_denied"
    RESOURCE_BUSY = "resource_busy"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    RESULT_INVALID = "result_invalid"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    TOOL_FAILED = "tool_failed"
    INTERNAL_ERROR = "internal_error"


class ModelSelection(Contract):
    requested_model: Literal["qwen", "bonsai", "qwen80b", "strata", "vision"]
    actual_model_used: Literal["qwen", "bonsai", "qwen80b", "strata", "vision", "openai_reasoning_fallback"]
    fallback_reason: Literal["external_gpu_workload", "local_model_failure"] | None = None
    gpu_blocking_owners: tuple[str, ...] = ()
    models_used: tuple[Literal["qwen", "bonsai", "qwen80b", "strata", "vision", "openai_reasoning_fallback"], ...] = Field(default=(), max_length=3)


class GenerationMetrics(Contract):
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    time_to_first_token_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    completion_tokens: int | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    finish_reason: Literal["stop", "length"]


class ExecutionResult(Contract):
    request_id: UUID
    status: FinalStatus
    content: str | None = None
    reasoning_content: str | None = Field(default=None, repr=False, exclude=True)
    tool_result: ToolResult | None = None
    error_code: ErrorCode | None = None
    renderer_used: bool = False
    cloud_used: bool = False
    grounded_answer: dict[str, JsonValue] | None = None
    image_spec: dict[str, JsonValue] | None = None
    pt_answer: dict[str, JsonValue] | None = None
    model_selection: ModelSelection | None = None
    generation_metrics: GenerationMetrics | None = None


class ProcessLifecycleContract(Contract):
    """HOST-only definition; deliberately absent from ModelProposal."""
    logical_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    start_argv: tuple[str, ...] = Field(min_length=1)
    stop_strategy: Literal["owned_process_terminate"]
    health_check: Literal["http"]
    process_identification: Literal["owned_child_pid"]
    endpoint: str
    timeout_seconds: float = Field(gt=0, le=600)


class NormalizedMessage(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000000)
