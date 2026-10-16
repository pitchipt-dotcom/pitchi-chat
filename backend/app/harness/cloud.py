"""HOST-only fail-closed cloud policy; payload approval never comes from the LLM."""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol, Literal

from backend.app.models.base import Message


def payload_digest(messages: list[Message]) -> str:
    # Covers the entire exact payload, including history, not only the latest user text.
    payload = json.dumps([{"role": m.role, "content": m.content} for m in messages],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class CloudPolicy:
    enabled: bool = False
    purposes: frozenset[str] = frozenset()
    # HOST-only exact payloads independently reviewed as public. No request/LLM field
    # can set this. Default empty; absence of a regex match is NOT public approval.
    reviewed_public_payloads: frozenset[str] = frozenset()
    reviewed_render_pairs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CloudDecision:
    allowed: bool
    reason: Literal["allowed", "cloud_disabled", "purpose_denied", "local_only_mode", "private_or_unknown"]


class CloudPrivacyGate:
    def __init__(self, policy: CloudPolicy | None = None):
        self.policy = policy or CloudPolicy()

    def authorize(self, messages, *, mode, purpose):
        if not self.policy.enabled:
            return CloudDecision(False, "cloud_disabled")
        if purpose not in {"reasoning_fallback", "japanese_renderer"} or purpose not in self.policy.purposes:
            return CloudDecision(False, "purpose_denied")
        if mode != "chat":
            return CloudDecision(False, "local_only_mode")
        text = "\n".join(m.content for m in messages)
        # Defense in depth only; manual exact-payload approval is ALSO mandatory.
        if re.search(r"患者|氏名|住所|生年月日|カルテ|個人情報|credential|password|secret|token|api[_ -]?key|PRIVATE KEY|[\w.+-]+@[\w.-]+\.[a-z]+", text, re.I):
            return CloudDecision(False, "private_or_unknown")
        if payload_digest(messages) not in self.policy.reviewed_public_payloads:
            return CloudDecision(False, "private_or_unknown")
        return CloudDecision(True, "allowed")


class ReasoningFallbackProvider(Protocol):
    """HOST-injected boundary. Not a Model Registry target; never chosen by LLM."""
    purpose: Literal["reasoning_fallback"]
    async def generate(self, messages: list[Message]) -> str: ...
