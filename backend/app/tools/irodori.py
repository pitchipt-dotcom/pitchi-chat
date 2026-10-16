"""Existing local IRODORI speech API. No cloud SDK, process launch or voice upload."""

import io
import os
import wave
from typing import Literal

from pydantic import Field

from backend.app.harness.contracts import Artifact, Contract, ErrorCode, FinalStatus, ToolResult
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.context import remote_event
from backend.app.tools.generation import ArtifactStore
from backend.app.tools.http import ServiceHTTP


class TTSArguments(Contract):
    text: str = Field(min_length=1, max_length=4096)
    speed: float = Field(default=1.0, ge=0.25, le=4, allow_inf_nan=False)


class TTSOutput(Contract):
    format: Literal["wav"]
    frames: int = Field(gt=0)
    sample_rate: int = Field(gt=0)
    artifact_count: int = Field(ge=1, le=1)


class IrodoriAdapter:
    def __init__(self, transport: ServiceHTTP, artifacts: ArtifactStore, models: dict[str, str], key_env: str = ""):
        self.http, self.artifacts, self.models, self.key_env = transport, artifacts, models, key_env

    async def execute(self, arguments: TTSArguments, backend: str | None):
        model = self.models.get(backend)
        if not model or not arguments.text.strip():
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        key = os.environ.get(self.key_env) if self.key_env else None
        if self.key_env and not key:
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        remote_event("SUBMITTING")
        status, mime, data = await self.http.request("POST", "/v1/audio/speech", max_bytes=30 * 1024 * 1024,
                           json={"model": model, "input": arguments.text, "voice": None,
                                 "response_format": "wav", "speed": arguments.speed}, headers=headers)
        if status != 200 or mime not in {"audio/wav", "audio/x-wav", "audio/wave"}:
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        try:
            with wave.open(io.BytesIO(data)) as audio:
                frames, rate = audio.getnframes(), audio.getframerate()
                if not 8000 <= rate <= 192000 or not 1 <= audio.getnchannels() <= 2 or audio.getsampwidth() not in {1, 2, 3, 4}:
                    raise ValueError
                decoded = audio.readframes(frames)
                if len(decoded) != frames * audio.getnchannels() * audio.getsampwidth() or not decoded:
                    raise ValueError
        except (wave.Error, EOFError, ValueError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        output = TTSOutput(format="wav", frames=frames, sample_rate=rate, artifact_count=1)
        artifact = Artifact(path=self.artifacts.save(data, ".wav"), kind="audio")
        remote_event("TERMINAL")
        return ToolResult(success=True, result_type="audio", output=output.model_dump(), http_status=status,
                          artifacts=(artifact,))
