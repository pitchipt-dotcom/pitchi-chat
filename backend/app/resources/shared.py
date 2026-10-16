"""Opt-in job clients use the existing HOST's model owner and generation lock.

No Work binding changes, arbitrary endpoints, tools, model selection or lifecycle
commands. A finite renewable reservation holds the model between job phases.
"""
import json
import os
import secrets
import re
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from backend.app.resources.gpu import ResourceMode
from backend.app.resources.manager import GPUUnsafe
from backend.app.models.base import ModelError
from backend.app.models.schema import normalized_format, validated_json


class JobMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=100000)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model: str
    messages: list[JobMessage] = Field(min_length=1, max_length=256)
    max_tokens: int = Field(default=2600, ge=1, le=8192, strict=True)
    temperature: float = Field(default=0, ge=0, le=2)
    stream: bool = False
    response_format: dict | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    reasoning_budget_tokens: int = Field(default=512, ge=0, le=4096, strict=True)


def router():
    routes = APIRouter(prefix="/api/models/strata")

    def authorized(request):
        key = os.environ.get("PITCH_CHAT_SHARED_MODEL_TOKEN", "")
        if len(key) < 32:
            raise HTTPException(503, "共有ジョブ接続は未設定です。")
        given = request.headers.get("authorization", "")
        if not secrets.compare_digest(given, "Bearer " + key):
            raise HTTPException(403, "共有ジョブ接続の認証が必要です。")
        return request.app.state.gpu_manager

    def binding_for(request):
        try:
            binding = request.app.state.models.resolve("strata")
            request.app.state.harness.validate_owned_model(binding)
            return binding
        except ModelError:
            raise HTTPException(503, "共有モデルの管理設定が未完了です。") from None

    @routes.post("/uses")
    async def retain(request: Request, response: Response):
        manager = authorized(request)
        try:
            token = manager.retain("strata", ttl=120)
        except GPUUnsafe:
            raise HTTPException(409, "モデル使用権を取得できません。") from None
        response.headers["Cache-Control"] = "no-store"
        # Reserving usage does not start a model. Start only on a generation.
        return {"use_id": token, "renew_within_seconds": 120}

    @routes.post("/uses/{use_id}/renew")
    async def renew(use_id: UUID, request: Request, response: Response):
        manager = authorized(request)
        try:
            manager.renew(str(use_id))
        except GPUUnsafe:
            raise HTTPException(409, "モデル使用権が失効しました。") from None
        response.headers["Cache-Control"] = "no-store"
        return {"renew_within_seconds": 120}

    @routes.delete("/uses/{use_id}")
    async def release(use_id: UUID, request: Request, response: Response):
        manager = authorized(request)
        manager.release_use(str(use_id))
        response.headers["Cache-Control"] = "no-store"
        return {"released": True}

    @routes.get("/v1/models")
    async def models(request: Request, response: Response):
        authorized(request)
        spec = binding_for(request).settings
        response.headers["Cache-Control"] = "no-store"
        return {"object": "list", "data": [{"id": spec.model_name, "object": "model"}]}

    @routes.post("/v1/chat/completions")
    async def completion(body: Completion, request: Request):
        manager = authorized(request)
        binding = binding_for(request)
        if body.model != binding.settings.model_name:
            raise HTTPException(422, "設定された共有モデルと一致しません。")
        payload = body.model_dump(exclude_none=True)
        try:
            output_format, schema = normalized_format(body.response_format)
        except ModelError:
            raise HTTPException(422, "JSON Schemaを安全に検証できません。") from None
        if output_format is not None:
            payload["response_format"] = output_format
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 262144:
            raise HTTPException(413, "モデル入力が上限を超えています。")
        if body.reasoning_effort != "none" and not 1 <= body.reasoning_budget_tokens <= body.max_tokens - 512:
            raise HTTPException(422, "有限の思考予算と回答予算が必要です。")
        payload["stream"] = False
        payload["reasoning_budget_tokens"] = 0 if body.reasoning_effort == "none" else body.reasoning_budget_tokens
        # Each call retains use before entering the shared coordinator queue;
        # optional /uses reservations independently span gaps between calls.
        try:
            async with request.app.state.resources.job(ResourceMode.WORK, model="strata", profile="strata",
                                                        keep_ready=True, allow_start=True):
                client = request.app.state.shared_model_client
                async with client.stream("POST", binding.settings.endpoint, json=payload,
                                         timeout=binding.settings.timeout_seconds, follow_redirects=False) as upstream:
                    data = bytearray()
                    async for part in upstream.aiter_bytes():
                        data.extend(part)
                        if len(data) > 1000000:
                            raise ValueError("response_too_large")
                    upstream.raise_for_status()
                result = json.loads(data)
                if result.get("model") != binding.settings.model_name or not result.get("choices"):
                    raise ValueError("invalid_model_response")
                if any(not isinstance(c.get("message"), dict) or c.get("finish_reason") not in {"stop", "length"}
                       for c in result["choices"]):
                    raise ValueError("invalid_completion")
                if schema is not None:
                    for choice in result["choices"]:
                        if choice["finish_reason"] != "stop":
                            raise ValueError("incomplete_structured_output")
                        validated_json(choice["message"]["content"], schema)
        except GPUUnsafe as error:
            details = getattr(error, "memory_details", None)
            if details is not None:
                from backend.app.resources.memory_recovery import failure_detail
                raise HTTPException(409, failure_detail("resource_unavailable", details)) from None
            raise HTTPException(409, "リソース安全条件またはモデル使用権を確認してください。") from None
        except httpx.TimeoutException:
            raise HTTPException(504, "共有モデルがタイムアウトしました。") from None
        except httpx.HTTPStatusError as error:
            detail = {"code": "strata_upstream_http", "upstream_status": error.response.status_code}
            # Preserve only finite error metadata. The upstream Schema error
            # message can contain generated text and must never be forwarded.
            try:
                upstream = json.loads(data).get("error", {})
                if upstream.get("code") == "structured_output_failed":
                    detail["code"] = "strata_structured_output_failed"
                    field = re.search(r"JSON Schema at /([a-z_]+)(?:/|:)", upstream.get("message", ""))
                    allowed = {"article_summary", "thumbnail_concept", "main_subject", "supporting_elements",
                               "composition", "mood", "style", "text_in_image", "image_prompt", "negative_prompt"}
                    if field and field[1] in allowed:
                        detail["field"] = field[1]
            except (ValueError, TypeError, AttributeError):
                pass
            raise HTTPException(502, detail) from None
        except ModelError as error:
            code = "strata_schema_validation_failed" if error.classification == "invalid_json_schema" else "strata_completion_invalid"
            raise HTTPException(502, {"code": code}) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "共有モデルの応答を完了できません。") from None
        if not body.stream:
            return Response(json.dumps(result, ensure_ascii=False), media_type="application/json",
                            headers={"Cache-Control": "no-store"})
        # Structured Strata output already buffers. Supply a complete SSE frame
        # for existing role transports; this route makes no incremental TTFT claim.
        event = {"id": result.get("id", str(uuid4())), "object": "chat.completion.chunk",
                 "model": result["model"], "usage": result.get("usage", {}),
                 "choices": [{"index": c.get("index", i), "delta": c["message"],
                              "finish_reason": c["finish_reason"]} for i, c in enumerate(result["choices"])]}
        return Response("data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n",
                        media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return routes
