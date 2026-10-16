"""Minimal adapter for an existing Qwen HTTP service. No process management."""

import os
from typing import Sequence

import httpx

from backend.app.config import Settings
from backend.app.models.base import Message, ModelError, ModelNotConfigured, ModelTimeout
from backend.app.models.response import ModelResponse, split_response


class QwenAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def generate(self, messages: Sequence[Message]) -> str:
        return (await self.generate_response(messages)).final_content

    async def generate_response(self, messages: Sequence[Message]) -> ModelResponse:
        return await self._generate(messages)

    async def generate_structured(self, messages: Sequence[Message], schema: dict) -> str:
        return (await self.generate_structured_response(messages, schema)).final_content

    async def generate_structured_response(self, messages: Sequence[Message], schema: dict) -> ModelResponse:
        return await self._generate(messages, schema)

    async def _generate(self, messages: Sequence[Message], schema: dict | None = None) -> ModelResponse:
        if not self.settings.endpoint:
            raise ModelNotConfigured("configファイルの model.endpoint を設定してください。")
        headers = {}
        key = os.environ.get(self.settings.api_key_env) if self.settings.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = None
            payload = {"model": self.settings.model_name, "stream": False,
                       "messages": [{"role": m.role, "content": m.content} for m in messages]}
            if schema is not None:
                payload |= {"response_format": {"type": "json_schema", "json_schema": {
                    "name": "pitch_chat_contract", "strict": True, "schema": schema}},
                    "max_tokens": 1024, "temperature": 0}
            response = await self.client.post(
                self.settings.endpoint,
                headers=headers,
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ModelError("v0.1ではtool callingを利用できません。", http_status=response.status_code,
                                 classification="unexpected_tool_call")
            final = message.get("final_content", message.get("final", message.get("content")))
            if final is None or final == "":
                raise ModelError("モデルのfinal answerが空です。", http_status=response.status_code, classification="empty_content")
            try:
                return split_response(message)
            except ModelError as error:
                error.http_status = response.status_code
                # Safe framing metadata only; never raw content in exceptions/audit.
                from backend.app.models.response import TOKEN
                tags = [m.group(0).lower() for m in TOKEN.finditer(final)] if isinstance(final, str) else []
                error.model_framing = ("close_only" if tags and tags[0].startswith("</") else
                                       "tagged" if tags else "field_only" if message.get("reasoning_content") else "plain")
                finish = response.json()["choices"][0].get("finish_reason")
                error.model_finish_reason = finish if finish in {"stop", "length"} else "other"
                raise
        except httpx.TimeoutException:
            raise ModelTimeout("モデルへの接続がタイムアウトしました。") from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            # Never expose upstream bodies, endpoint URLs or credentials.
            raise ModelError("モデルの接続または応答に問題があります。",
                             http_status=response.status_code if response is not None else None,
                             classification="http_error" if response is not None and response.status_code >= 400 else "invalid_response") from None
