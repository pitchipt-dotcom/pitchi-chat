from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from backend.app.harness.contracts import ModelProposal


@dataclass(frozen=True)
class Message:
    role: str
    content: str


class ModelError(Exception):
    """Safe, user-facing adapter failure."""
    def __init__(self, message, *, http_status=None, classification=None):
        super().__init__(message)
        self.http_status = http_status
        self.classification = classification


class ModelNotConfigured(ModelError):
    pass


class ModelTimeout(ModelError):
    pass


class ModelAdapter(Protocol):
    async def generate(self, messages: Sequence[Message]) -> str: ...


class ProposalAdapter(Protocol):
    """Optional structured adapter. HOST must parse and revalidate every response."""
    async def propose(self, messages: Sequence[Message]) -> "ModelProposal | dict | str": ...
