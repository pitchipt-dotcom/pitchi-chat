"""Private-safe persistent audit: classifications only, never request/response bodies."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field

from backend.app.harness.contracts import Contract, ErrorCode, FinalStatus, ModelSelection
from backend.app.storage.sqlite import HistoryStore


class AuditKind(str, Enum):
    REQUEST_NORMALIZED = "request_normalized"
    MODEL_SELECTED = "model_selected"
    TOOL_SELECTED = "tool_selected"
    PROPOSAL = "proposal"
    CONTRACT_VALIDATED = "contract_validated"
    POLICY_VALIDATED = "policy_validated"
    RESOURCE_VALIDATED = "resource_validated"
    RESOURCE_ACQUIRED = "resource_acquired"
    RESULT_VALIDATED = "result_validated"
    REJECTED = "rejected"
    RETRY = "retry"
    PRESENTATION = "presentation"
    FINAL = "final"
    PROCESS_START = "process_start"
    PROCESS_STOP = "process_stop"
    MODEL_FALLBACK = "model_fallback"
    CLOUD_GATE = "cloud_gate"


class AuditEvent(Contract):
    request_id: UUID
    event: AuditKind
    selected_model: str | None = None
    selected_tool: str | None = None
    proposal_kind: Literal["answer", "tool"] | None = None
    resource_job_id: UUID | None = None
    stage: Literal["proposal", "arguments", "policy", "resource", "result", "presentation"] | None = None
    reason: ErrorCode | None = None
    status: FinalStatus | None = None
    renderer_used: bool = False
    cloud_used: bool = False
    cloud_purpose: Literal["japanese_renderer", "reasoning_fallback"] | None = None
    model_http_status: int | None = None
    model_failure: Literal["http_error", "invalid_response", "unexpected_tool_call", "empty_content", "invalid_final"] | None = None
    model_framing: Literal["close_only", "tagged", "field_only", "plain"] | None = None
    model_frame_error: Literal["invalid_channel", "size_limit", "machine_embedding", "mismatched_close", "malformed_tag",
                               "unclosed_reasoning", "partial_tag", "empty_final"] | None = None
    model_finish_reason: Literal["stop", "length", "other"] | None = None
    reasoning_detected: bool = False
    reasoning_chars: int = Field(default=0, ge=0, le=1000000)
    contract_failure: Literal["invalid_json", "missing_fields", "extra_fields", "invalid_value"] | None = None
    response_shape: Literal["json_object", "json_array", "code_frame", "thinking_frame", "plain_text"] | None = None
    evidence_count: int | None = Field(default=None, ge=0, le=9)
    model_selection: ModelSelection | None = None
    cloud_decision: Literal["allowed", "cloud_disabled", "purpose_denied", "local_only_mode", "private_or_unknown", "provider_unavailable"] | None = None


class AuditTrail:
    def __init__(self, store: HistoryStore):
        self.store = store
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS harness_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                request_id TEXT NOT NULL,
                event TEXT NOT NULL,
                metadata TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS audit_request ON harness_audit(request_id, id)")

    def emit(self, event: AuditEvent):
        with self.store.connection() as db:
            db.execute("INSERT INTO harness_audit (created_at, request_id, event, metadata) VALUES (?, ?, ?, ?)",
                       (datetime.now(timezone.utc).isoformat(), str(event.request_id), event.event.value,
                        event.model_dump_json(exclude={"request_id", "event"})))
