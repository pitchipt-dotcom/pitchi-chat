"""Strata's local OpenAI SSE transport; final-only, bounded, no retry/tools."""

import json
import os
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.models.base import Message, ModelError, ModelNotConfigured, ModelTimeout
from backend.app.models.response import ReasoningParser
from backend.app.resources.memory import check_stream_memory


class StrataOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=128, le=8192)
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    reasoning_budget_tokens: int = Field(default=512, ge=0, le=4096)

    @model_validator(mode="after")
    def answer_reserve(self):
        if self.reasoning_effort != "none" and (self.reasoning_budget_tokens < 1 or
                self.reasoning_budget_tokens > self.max_tokens - 512):
            raise ValueError("思考予算とは別に回答用512トークン以上を確保してください。")
        return self


class StrataAdapter:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def generate(self, messages):
        return (await self.generate_response(messages)).final_content

    async def generate_response(self, messages):
        return await self.generate_chat(messages)

    async def generate_structured_response(self, messages, schema):
        from backend.app.models.schema import validator_for, validated_json
        from backend.app.models.response import split_response
        validator_for(schema)
        payload = {"model": self.settings.model_name,
                   "messages": [{"role": m.role, "content": m.content} for m in messages],
                   "stream": False, "temperature": 0, "max_tokens": 4096,
                   "reasoning_effort": "none", "reasoning_budget_tokens": 0,
                   "response_format": {"type": "json_schema", "json_schema": {
                       "name": "host_contract", "strict": True, "schema": schema}}}
        try:
            response = await self.client.post(self.settings.endpoint, json=payload,
                                             timeout=self.settings.timeout_seconds, follow_redirects=False)
            response.raise_for_status()
            data = response.json(); choice = data["choices"][0]
            if choice.get("finish_reason") != "stop" or data.get("model") != self.settings.model_name:
                raise ModelError("構造化応答が完了していません。", classification="invalid_response")
            if choice["message"].get("tool_calls") or choice["message"].get("function_call"):
                raise ModelError("構造化応答に未許可Tool呼出しがあります。", classification="unexpected_tool_call")
            result = split_response(choice["message"])
            validated_json(result.final_content, schema)
            return result
        except httpx.TimeoutException:
            raise ModelTimeout("Strata構造化生成がタイムアウトしました。") from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            raise ModelError("Strata構造化応答を取得できません。", classification="invalid_response") from None

    async def generate_chat(self, messages: list[Message], options=None, on_delta=None, *, memory_reserve_mib=0):
        options = StrataOptions.model_validate(options or {})
        if not self.settings.endpoint:
            raise ModelNotConfigured("Strataの接続先が未設定です。")
        # Strata supports Japanese direct replies; old assistant thoughts are never replayed.
        wire = [{"role": "system", "content": "あなたは日本語で自然に回答するアシスタントです。ユーザーの指示に従い、不確かな点を明示してください。"}]
        wire += [{"role": m.role, "content": m.content} for m in messages]
        payload = options.model_dump() | {"model": self.settings.model_name, "messages": wire,
            "stream": True, "stream_options": {"include_usage": True}}
        if options.reasoning_effort == "none":
            payload["reasoning_budget_tokens"] = 0
        key = os.environ.get(self.settings.api_key_env) if self.settings.api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        parser = ReasoningParser()
        started, first = time.perf_counter(), None
        usage, finish, done, size = {}, None, False, 0
        try:
            async with self.client.stream("POST", self.settings.endpoint, json=payload, headers=headers,
                                          timeout=self.settings.timeout_seconds, follow_redirects=False) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if memory_reserve_mib:
                        check_stream_memory(memory_reserve_mib)
                    size += len(line)
                    if size > 1000000:
                        raise ModelError("Strataの応答が上限を超えました。")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise ModelError("Strataが応答を完了できませんでした。")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("tool_calls") or delta.get("function_call"):
                        raise ModelError("Strataの会話経路ではTool呼出しを利用しません。")
                    for key in ("reasoning_content", "reasoning", "thinking", "analysis"):
                        if delta.get(key) is not None:
                            parser.feed(delta[key], "reasoning")
                    final, _ = parser.feed(delta.get("content") or "")
                    if final:
                        if first is None:
                            first = time.perf_counter() - started
                        if on_delta is not None:
                            await on_delta(final)  # Provisional until parser.finish and Harness validation.
                    if choice.get("finish_reason") is not None:
                        finish = choice["finish_reason"]
            if not done or finish not in {"stop", "length"}:
                raise ModelError("Strataのストリームが途中で終了しました。", classification="invalid_response")
            result = parser.finish()
            elapsed = time.perf_counter() - started
            tokens = usage.get("completion_tokens")
            if type(tokens) is not int or tokens < 0:
                tokens = None
            return result.model_copy(update={"metadata": result.metadata | {
                "elapsed_seconds": elapsed, "time_to_first_token_seconds": first,
                "completion_tokens": tokens, "tokens_per_second": tokens / elapsed if tokens else None,
                "finish_reason": finish}})
        except httpx.TimeoutException:
            raise ModelTimeout("Strataへの接続がタイムアウトしました。") from None
        except httpx.ConnectError:
            raise ModelNotConfigured("Strataは停止中、または接続できません。起動前確認後に明示的に起動してください。") from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ModelError("Strataの接続または応答に問題があります。会話が長すぎる場合は新しい会話を開始してください。") from None
