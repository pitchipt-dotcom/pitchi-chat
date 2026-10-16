"""Public HOST job contract: payloads and local filesystem paths stay private."""

from typing import Literal
from uuid import UUID

from backend.app.harness.contracts import Contract


class JobRecord(Contract):
    job_id: UUID
    tool: str
    status: Literal["QUEUED", "RUNNING", "SUCCESS", "ERROR", "RECOVERY_REQUIRED"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result_reference: str | None = None
    error: str | None = None
    remote_run_id: str | None = None
    remote_phase: Literal["SUBMITTING", "SUBMITTED", "TERMINAL"] | None = None
    image_phase: Literal["COMPILING", "SPEC_READY", "GENERATING"] | None = None
    media_phase: Literal["PROMPT_GENERATING", "PROMPT_SAVED", "STRATA_STOPPING", "RESOURCE_CHECK", "COMFY_STARTING", "GENERATING", "COMPLETED", "FAILED"] | None = None
