"""Parse and validate before execution; verify evidence after execution."""

import json
from pathlib import Path

from pydantic import ValidationError

from backend.app.harness.contracts import ErrorCode, FinalStatus, ModelProposal, ResourceState, ToolResult
from backend.app.harness.policy import authorized_path
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.registry import ToolDefinition


def reject(code=ErrorCode.SCHEMA_INVALID):
    raise HarnessFailure(code, FinalStatus.REJECTED)


def parse_proposal(raw) -> ModelProposal:
    try:
        # Revalidate even typed input; never trust model_construct or an adapter claim.
        if isinstance(raw, ModelProposal):
            raw = raw.model_dump(mode="json")
        if isinstance(raw, str):
            if len(raw) > 1000000:
                reject()
            return ModelProposal.model_validate_json(raw)
        return ModelProposal.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        reject()


def validate_arguments(raw: dict, definition: ToolDefinition):
    try:
        from backend.app.models.response import reject_reasoning_fields
        reject_reasoning_fields(raw)
        return definition.argument_schema.model_validate_json(json.dumps(raw, allow_nan=False), strict=True)
    except (ValidationError, ValueError, TypeError):
        reject()


def validate_resource(state: ResourceState, definition: ToolDefinition):
    if state.active_job is not None:
        raise HarnessFailure(ErrorCode.RESOURCE_BUSY, FinalStatus.REJECTED)
    if definition.requires_managed_gpu and (
        state.gpu_telemetry != "available" or not state.process_management
        or state.external_gpu_workloads != "managed"
    ):
        raise HarnessFailure(ErrorCode.RESOURCE_UNAVAILABLE, FinalStatus.REJECTED)


def validate_result(raw, definition: ToolDefinition) -> ToolResult:
    try:
        if isinstance(raw, ToolResult):
            raw = raw.model_dump(mode="json")
        result = ToolResult.model_validate_json(json.dumps(raw, allow_nan=False))
        definition.output_schema.model_validate(result.output, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
    if result.timed_out:
        raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)
    if (not result.success or result.result_type != definition.result_type
            or (definition.requires_exit_status and result.exit_status is None)
            or (result.exit_status is not None and result.exit_status != 0)
            or (definition.requires_http_status and result.http_status is None)
            or (result.http_status is not None and not 200 <= result.http_status < 300)):
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    if result.result_type == "run" and (not result.run_id or result.run_status != "completed" or not result.output):
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    if result.result_type in {"audio", "image"} and not result.artifacts:
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    if (len(result.artifacts) < definition.min_artifacts
            or ("artifact_count" in result.output and result.output["artifact_count"] != len(result.artifacts))):
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    for artifact in result.artifacts:
        try:
            path = authorized_path(artifact.path, definition.allowed_roots)
            if not path.is_file() or path.stat().st_size == 0:
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            if result.result_type in {"audio", "image"}:
                if artifact.kind != result.result_type:
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
                with path.open("rb") as file:
                    header = file.read(16)
                if artifact.kind == "audio":
                    valid = header.startswith(b"RIFF") and header[8:12] == b"WAVE"
                else:
                    valid = (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")
                             or (header.startswith(b"RIFF") and header[8:12] == b"WEBP"))
                if not valid:
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
        except (OSError, HarnessFailure):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
    return result
