"""HOST worker context; adapters can record remote progress, not manage GPU authority."""

from contextvars import ContextVar
from typing import Callable

remote_callback: ContextVar[Callable[[str, str | None], None] | None] = ContextVar("remote_callback", default=None)
current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)
prepared_image_call: ContextVar[str | None] = ContextVar("prepared_image_call", default=None)
media_callback: ContextVar[Callable | None] = ContextVar("media_callback", default=None)


def media_event(phase, **details):
    callback = media_callback.get()
    if callback:
        callback(phase, details)


def remote_event(phase: str, run_id: str | None = None):
    callback = remote_callback.get()
    if callback:
        callback(phase, run_id)
