"""Bounded private HTTP transport with safe error classifications and no redirects."""

import json

import httpx

from backend.app.harness.contracts import ErrorCode, FinalStatus
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.config import validate_location


class ServiceHTTP:
    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout: float = 20, *, public_retrieval=False):
        if public_retrieval and base_url:
            from backend.app.tools.config import validate_public_retrieval_location
            validate_public_retrieval_location(base_url)
        else:
            validate_location(base_url)
        self.client = client
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    async def request(self, method: str, path: str, *, max_bytes: int = 2 * 1024 * 1024, **kwargs):
        if not self.base or not path.startswith("/") or path.startswith("//"):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        try:
            async with self.client.stream(method, self.base + path, timeout=self.timeout,
                                          follow_redirects=False, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise HarnessFailure(ErrorCode.TOOL_FAILED)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise HarnessFailure(ErrorCode.RESULT_INVALID)
                return response.status_code, response.headers.get("content-type", "").split(";")[0], bytes(data)
        except httpx.TimeoutException:
            raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED) from None
        except httpx.HTTPError:
            raise HarnessFailure(ErrorCode.TOOL_FAILED) from None

    async def json(self, method: str, path: str, **kwargs):
        status, content_type, data = await self.request(method, path, **kwargs)
        if content_type != "application/json":
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        try:
            return status, json.loads(data)
        except (ValueError, UnicodeError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
