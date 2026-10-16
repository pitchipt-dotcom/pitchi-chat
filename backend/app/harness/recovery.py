"""HOST-selected timeout and retry limits; never consume model retry instructions."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import Field

from backend.app.harness.contracts import Contract, ErrorCode, FinalStatus
from backend.app.models.base import ModelError

T = TypeVar("T")
_running_operations: set[asyncio.Task] = set()


def _finished(task: asyncio.Task):
    _running_operations.discard(task)
    if not task.cancelled():
        task.exception()  # Consume late failures; never log their private contents.


class HarnessFailure(ModelError):
    def __init__(self, code: ErrorCode, status: FinalStatus = FinalStatus.ERROR, *, resource_details=None):
        self.code = code
        self.status = status
        self.resource_details = resource_details
        super().__init__(f"HOSTの検証または実行に失敗しました ({code.value})。")


class RetryableExecutionError(Exception):
    """An adapter-classified transient failure, not a model instruction."""


class RecoveryPolicy(Contract):
    timeout_seconds: float = Field(default=60.0, gt=0, le=600, allow_inf_nan=False)
    max_retries: int = Field(default=0, ge=0, le=2)
    idempotent: bool = False


async def bounded_execute(operation: Callable[[], Awaitable[T]], policy: RecoveryPolicy,
                          on_retry: Callable[[], None]) -> T:
    retries = policy.max_retries if policy.idempotent else 0
    for attempt in range(retries + 1):
        task = asyncio.create_task(operation())
        _running_operations.add(task)
        task.add_done_callback(_finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=policy.timeout_seconds)
            if not done:
                # Return STALLED even if an adapter ignores cancellation. Its operation
                # retains the resource lease until it actually exits; never force-unlock.
                task.cancel()
                raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise
        except TimeoutError:
            raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED) from None
        except RetryableExecutionError:
            if attempt == retries:
                raise HarnessFailure(ErrorCode.TOOL_FAILED) from None
            on_retry()
    raise AssertionError("unreachable bounded loop")
