"""Gate-bound Responses transport. No tools, remote state, redirect, proxy or retry."""

import asyncio
import json
import os

import httpx

from backend.app.harness.cloud import CloudPrivacyGate
from backend.app.models.base import Message, ModelError
from backend.app.providers.config import ProviderSettings
from backend.app.harness.contracts import RenderRequest


class OpenAIProvider:
    def __init__(self, settings: ProviderSettings, gate: CloudPrivacyGate, purpose: str,
                 *, transport: httpx.AsyncBaseTransport | None = None):
        if purpose not in {"reasoning_fallback", "japanese_renderer"}:
            raise ValueError("Unsupported cloud purpose")
        self.settings, self.gate, self.purpose, self.transport = settings, gate, purpose, transport

    async def generate(self, messages: list[Message]) -> str:
        # Snapshot input BEFORE authorization; a caller cannot mutate it mid-dispatch.
        messages = [Message(m.role, m.content) for m in messages]
        if self.purpose == "japanese_renderer":
            from backend.app.harness.render import RENDER_INSTRUCTION, eligible
            if (len(messages) != 2 or messages[0] != Message("system", RENDER_INSTRUCTION)
                    or messages[1].role != "user" or not eligible(RenderRequest(
                        path="presentation", format="natural_language", content=messages[1].content))):
                raise ModelError("Renderer accepts only HOST natural-language presentation.")
        if (not self.settings.enabled or not messages or len(messages) > 100
                or any(m.role not in {"system", "user", "assistant"} or not isinstance(m.content, str) for m in messages)
                or sum(len(m.content) for m in messages) > 32000
                or not self.gate.authorize(messages, mode="chat", purpose=self.purpose).allowed):
            raise ModelError("Cloud policy denied the exact payload.")
        key = os.environ.get(self.settings.api_key_env)
        if not key or any(c in key for c in "\r\n"):
            raise ModelError("Cloud credential is unavailable.")
        body = {"model": self.settings.model, "input": [{"role": m.role, "content": m.content} for m in messages],
                "store": False, "background": False, "stream": False, "tools": [], "tool_choice": "none",
                "parallel_tool_calls": False, "truncation": "disabled",
                "reasoning": {"effort": self.settings.reasoning_effort},
                "max_output_tokens": self.settings.max_output_tokens}
        try:
            async with asyncio.timeout(self.settings.timeout_seconds), httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, transport=self.transport) as client:
                async with client.stream("POST", self.settings.endpoint, json=body,
                                         headers={"Authorization": f"Bearer {key}"}, timeout=self.settings.timeout_seconds) as response:
                    if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise ModelError("Cloud HTTP response failed.")
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > 1024 * 1024:
                            raise ModelError("Cloud response exceeds HOST limit.")
            result = json.loads(data)
            if result.get("status") != "completed" or result.get("error") or result.get("incomplete_details"):
                raise ValueError
            parts = []
            for item in result["output"]:
                if item.get("type") == "reasoning" and self.purpose == "reasoning_fallback":
                    continue  # Never present hidden reasoning; no execution authority.
                if item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed":
                    raise ValueError
                for part in item["content"]:
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str) or part.get("annotations"):
                        raise ValueError
                    parts.append(part["text"])
            text = "\n".join(parts)
            if not text.strip() or len(text) > 16000:
                raise ValueError
            return text
        except ModelError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            raise ModelError("Cloud transport or response validation failed.") from None
