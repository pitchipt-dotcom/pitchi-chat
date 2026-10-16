"""Confirmed external job protocols; production submission stays GPU-policy blocked.

Private helpers are adapter internals, not alternative HOST execution entrypoints.
Durable remote-job leases/cancellation must exist before real GPU dispatch is enabled.
"""

import asyncio
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, ValidationError

from backend.app.harness.contracts import Artifact, Contract, ErrorCode, FinalStatus, ToolResult
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.http import ServiceHTTP
from backend.app.jobs.context import remote_event


class ImageArguments(Contract):
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    aspect_ratio: Literal["1:1", "3:2", "2:3", "16:9", "9:16"] = "1:1"
    count: int = Field(default=1, ge=1, le=4)


class ImageOutput(Contract):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Literal["completed"]
    artifact_count: int = Field(ge=1, le=4)


class VideoArguments(Contract):
    title: str = Field(min_length=1, max_length=200)
    speech_text: str = Field(min_length=1, max_length=16000)
    subtitle_text: str = Field(min_length=1, max_length=16000)
    library_asset_ids: list[str] = Field(min_length=1, max_length=20)


class RemoteRunState(Contract):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Literal["running", "completed", "failed"]


class VideoOutput(Contract):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Literal["completed"]
    artifact_count: Literal[1]


def checked_id(value) -> str:
    try:
        return RemoteRunState(run_id=value, status="running").run_id
    except (ValidationError, TypeError):
        raise HarnessFailure(ErrorCode.RESULT_INVALID) from None


def relative_output(value: str) -> str:
    if not isinstance(value, str):
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or not value.startswith("/") or value.startswith("//") or ".." in value or "\\" in value or "%" in value
            or any(ord(char) < 32 for char in value)):
        raise HarnessFailure(ErrorCode.RESULT_INVALID)
    return value


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save(self, data: bytes, suffix: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / (uuid4().hex + suffix)
        with path.open("xb") as file:
            file.write(data)
        path.chmod(0o600)
        return str(path)


class ImageAdapter:
    def __init__(self, transport: ServiceHTTP, artifacts: ArtifactStore, backends: dict[str, str],
                 *, poll_interval: float = 1, max_polls: int = 120):
        self.http, self.artifacts, self.backends = transport, artifacts, dict(backends)
        self.poll_interval, self.max_polls = poll_interval, max_polls

    async def execute(self, arguments: ImageArguments, backend: str | None) -> ToolResult:
        wire_model = self.backends.get(backend)
        if wire_model is None:
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        remote_event("SUBMITTING")
        status, raw = await self.http.json("POST", "/api/generations",
                                          json=arguments.model_dump() | {"model": wire_model})
        if status != 202 or not isinstance(raw, dict):
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        run_id = checked_id(raw.get("id"))
        remote_event("SUBMITTED", run_id)
        artifacts = []
        for _ in range(self.max_polls):
            poll_status, raw = await self.http.json("GET", f"/api/generations/{run_id}")
            if poll_status != 200 or not isinstance(raw, dict) or raw.get("id") != run_id:
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            state = raw.get("status")
            if state in {"failed", "cancelled"}:
                remote_event("TERMINAL", run_id)
                raise HarnessFailure(ErrorCode.TOOL_FAILED)
            if state == "completed":
                remote_event("TERMINAL", run_id)
                images = raw.get("images")
                if not isinstance(images, list) or len(images) != arguments.count:
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
                for index in range(len(images)):
                    download_status, mime, data = await self.http.request("GET", f"/api/generations/{run_id}/images/{index}", max_bytes=20 * 1024 * 1024)
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime)
                    valid = (data.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png" else
                             data.startswith(b"\xff\xd8\xff") if suffix == ".jpg" else
                             data.startswith(b"RIFF") and data[8:12] == b"WEBP" if suffix == ".webp" else False)
                    if download_status != 200 or not valid or len(data) < 16:
                        raise HarnessFailure(ErrorCode.RESULT_INVALID)
                    artifacts.append(Artifact(path=self.artifacts.save(data, suffix), kind="image"))
                output = ImageOutput(run_id=run_id, status="completed", artifact_count=len(artifacts))
                return ToolResult(success=True, result_type="image", output=output.model_dump(),
                                  artifacts=tuple(artifacts), http_status=200, run_id=run_id, run_status="completed")
            if state not in {"queued", "planning", "submitting", "running"}:
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            await asyncio.sleep(self.poll_interval)
        raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)


class VideoAdapter:
    def __init__(self, transport: ServiceHTTP, artifacts: ArtifactStore, *, poll_interval: float = 3, max_polls: int = 120):
        self.http, self.artifacts = transport, artifacts
        self.poll_interval, self.max_polls = poll_interval, max_polls

    async def _submit(self, arguments: VideoArguments) -> RemoteRunState:
        for asset in arguments.library_asset_ids:
            checked_id(asset)
        fields = [(key, (None, value)) for key, value in arguments.model_dump(exclude={"library_asset_ids"}).items()]
        fields += [("library_asset_ids", (None, asset)) for asset in arguments.library_asset_ids]
        remote_event("SUBMITTING")
        _, raw = await self.http.json("POST", "/api/manual-video-jobs", files=fields,
                                      headers={"accept": "application/json", "x-requested-with": "manual-video-ui"})
        if not isinstance(raw, dict) or raw.get("ok") is not True:
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        run_id = checked_id(raw.get("job_id"))
        remote_event("SUBMITTED", run_id)
        return RemoteRunState(run_id=run_id, status="running")

    async def _poll(self, run_id: str):
        # The real UI polls the list; no unconfirmed single-job route is invented.
        checked_id(run_id)
        cursor = None
        for _ in range(5):
            params = {"limit": 20}
            if cursor:
                params["cursor"] = cursor
            poll_status, raw = await self.http.json("GET", "/api/manual-video-jobs", params=params)
            if poll_status != 200 or not isinstance(raw, dict) or raw.get("ok") is not True or not isinstance(raw.get("items"), list):
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            for item in raw["items"]:
                if not isinstance(item, dict):
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
                if item.get("job_id") == run_id:
                    status = item.get("status")
                    if status == "video_ready":
                        return RemoteRunState(run_id=run_id, status="completed"), relative_output(item.get("video_url"))
                    if status in {"failed", "cancelled"}:
                        return RemoteRunState(run_id=run_id, status="failed"), None
                    if item.get("is_stalled") is True:
                        raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)
                    # Nonterminal remote statuses remain untrusted progress, never success.
                    if not isinstance(status, str) or not status:
                        raise HarnessFailure(ErrorCode.RESULT_INVALID)
                    return RemoteRunState(run_id=run_id, status="running"), None
            cursor = raw.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or len(cursor) > 1000:
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
        raise HarnessFailure(ErrorCode.RESULT_INVALID)

    async def execute(self, arguments: VideoArguments, backend: str | None) -> ToolResult:
        state = await self._submit(arguments)
        for _ in range(self.max_polls):
            state, output_path = await self._poll(state.run_id)
            if state.status == "failed":
                remote_event("TERMINAL", state.run_id)
                raise HarnessFailure(ErrorCode.TOOL_FAILED)
            if state.status == "completed":
                remote_event("TERMINAL", state.run_id)
                # Fetch only this service's validated relative reference; never follow URLs/redirects.
                download_status, mime, data = await self.http.request("GET", output_path, max_bytes=100 * 1024 * 1024)
                if download_status != 200 or mime != "video/mp4" or len(data) < 16 or data[4:8] != b"ftyp":
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
                artifact = Artifact(path=self.artifacts.save(data, ".mp4"), kind="file")
                output = VideoOutput(run_id=state.run_id, status="completed", artifact_count=1)
                return ToolResult(success=True, result_type="run", output=output.model_dump(), artifacts=(artifact,),
                                  http_status=200, run_id=state.run_id, run_status="completed")
            await asyncio.sleep(self.poll_interval)
        raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)
