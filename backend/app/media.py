"""Saved image plans through existing Simple UI prompts and Host GPU tools."""

import hashlib
import json
import os
import secrets
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from backend.app.harness.contracts import Contract, ErrorCode, FinalStatus, ToolCall
from backend.app.harness.image import ImageSpec, ImageToolArguments, PreparedImagePrompt
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.contracts import JobRecord


class MediaRequest(Contract):
    kind: Literal["image", "video", "vision"] = "image"
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=4000)
    prompt_mode: Literal["direct", "strata", "qwen35"] = "strata"
    backend: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    aspect_ratio: Literal["1:1", "3:2", "2:3", "16:9", "9:16"] = "1:1"
    count: int = Field(default=1, ge=1, le=4)
    seed: int | None = Field(default=None, ge=0, le=9223372036854775807, strict=True)
    preview_only: bool = False
    conversation_id: UUID | None = Field(default=None, strict=False)
    reference_image_id: UUID | None = Field(default=None, strict=False)
    project_id: UUID | None = Field(default=None, strict=False, exclude_if=lambda v: v is None)
    previous_clip_id: UUID | None = Field(default=None, strict=False, exclude_if=lambda v: v is None)


class MediaPromptEdit(Contract):
    positive_prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)


def backend_digest(backend):
    return hashlib.sha256(json.dumps(backend.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class MediaPipeline:
    def __init__(self, harness, client, simple_ui_origin="", image_assets=None):
        self.harness, self.client = harness, client
        self.image_assets = image_assets
        if simple_ui_origin:
            p = urlsplit(simple_ui_origin)
            try:
                local = p.hostname == "localhost" or ip_address(p.hostname).is_loopback
            except (ValueError, TypeError):
                local = False
            if (not local or p.scheme != "http" or p.username or p.password or p.query
                    or p.fragment or p.path not in {"", "/"}):
                raise ValueError("Simple UI must use a configured loopback origin")
        self.origin = simple_ui_origin.rstrip("/")

    def preflight(self, request):
        if request.kind != 'video' and (request.project_id or request.previous_clip_id):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.kind == 'vision' and (not request.reference_image_id or not request.conversation_id
                or request.prompt_mode != 'direct' or request.preview_only or request.negative_prompt):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.conversation_id and request.count != 1:
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.reference_image_id:
            if (request.kind not in {"image", "vision", "video"} or not request.conversation_id or not self.image_assets
                    or request.count != 1 or request.negative_prompt):
                raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
            try:
                self.image_assets.reference_bytes(self.image_assets.reference(request.reference_image_id, request.conversation_id))
            except (ValueError, OSError):
                raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED) from None
        if request.conversation_id and (not self.image_assets or self.image_assets.history.history(str(request.conversation_id)) is None):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.kind == 'vision':
            definition = self.harness.registry.get('image.understand')
            if not definition or not definition.enabled or request.backend not in definition.allowed_backends:
                raise HarnessFailure(ErrorCode.RESOURCE_UNAVAILABLE, FinalStatus.REJECTED)
            return definition
        if request.kind == "video":
            definition = self.harness.registry.get("media.video.generate")
            if (not definition or not definition.enabled or request.count != 1 or request.aspect_ratio != "16:9"
                    or request.backend not in definition.allowed_backends
                    or request.negative_prompt):
                raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
            return definition.adapter.workflows.get(request.backend)
        from backend.app.harness.image import ImageRequest
        self.harness.preflight_image(ImageRequest(request=request.prompt, backend=request.backend,
            aspect_ratio=request.aspect_ratio, count=request.count, model="strata", compile_only=request.preview_only))
        definition = self.harness.registry.get("image.generate")
        adapter = definition.adapter
        backend = adapter.workflows.get(request.backend)
        if backend is None or (not request.preview_only and request.backend not in adapter.generation_backends):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if backend.family == "flux2-klein" and (request.count != 1 or request.negative_prompt):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.reference_image_id and backend.family != "flux2-klein":
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if request.prompt_mode == "strata" and not self.origin and not request.reference_image_id:
            raise HarnessFailure(ErrorCode.RESOURCE_UNAVAILABLE, FinalStatus.REJECTED)
        return backend

    async def prepare(self, request):
        backend = self.preflight(request)
        reference = self.image_assets.reference(request.reference_image_id, request.conversation_id) if request.reference_image_id else None
        if request.kind == 'video':
            from backend.app.harness.video import prepare_video
            return await prepare_video(self.harness, request, backend, reference)
        if request.kind == 'vision':
            from backend.app.tools.vision import VisionArguments
            call = ToolCall(tool='image.understand', backend=request.backend,
                arguments=VisionArguments(question=request.prompt, reference_image=reference).model_dump(mode='json'))
            self.harness.prepare_tool(call)
            return {'media_kind':'vision', 'question':request.prompt, 'reference_image':reference.model_dump(mode='json'),
                'conversation_id':str(request.conversation_id), 'backend':request.backend, 'vision_used':True}, call
        positive, negative, method = request.prompt, request.negative_prompt, "direct"
        workflow = ("minimax_h3_t2v_608x352_4s" if request.kind == "video" else
                    backend.workflow)
        if reference and request.prompt_mode in {"strata", "qwen35"}:
            model_id = "vision" if request.prompt_mode == "qwen35" else "strata"
            positive = await self.harness.compile_image_edit(request.prompt, **({"model_id": model_id} if model_id != "strata" else {}))
            method = "qwen35_text_only" if model_id == "vision" else "strata_text_only"
        elif request.prompt_mode == "qwen35":
            from backend.app.harness.image import ImageRequest
            intent, _ = await self.harness.compile_image(ImageRequest(request=request.prompt, model="vision",
                backend=request.backend, aspect_ratio=request.aspect_ratio, count=request.count, compile_only=True), lambda *args: None)
            template = backend.graph(intent, request.count)
            positive = template["positive"]["inputs"]["text"]
            negative = "" if backend.family == "flux2-klein" else template["negative"]["inputs"]["text"]
            if request.negative_prompt:
                negative = request.negative_prompt
            method = "qwen35"
        elif request.prompt_mode == "strata":
            # Do not hold a Host GPU lease while calling Simple UI: its planner
            # enters this same Host's authenticated shared Strata queue.
            try:
                response = await self.client.post(self.origin + "/api/prompt-preview", timeout=300,
                    json={"prompt": request.prompt, "model": backend.model,
                          "aspect_ratio": request.aspect_ratio, "prompt_mode": "strata", "media_kind": request.kind})
                response.raise_for_status()
                data = response.json()
                if data.get("planning_method") != "strata" or data.get("workflow") != workflow or data.get("over_limit"):
                    raise ValueError("invalid prompt provenance")
                positive, negative, method = data["image_prompt"], data["negative_prompt"], "strata"
                if backend.family == "flux2-klein":
                    negative = ""  # Official Distilled CFG=1 uses zero conditioning.
                if request.negative_prompt:
                    negative = request.negative_prompt
            except httpx.HTTPStatusError as error:
                failure = HarnessFailure(ErrorCode.MODEL_UNAVAILABLE)
                failure.prompt_details = {"code": "prompt_service_failed", "status": error.response.status_code}
                try:
                    detail = error.response.json().get("detail", {})
                    if detail.get("code") in {"strata_request_failed", "strata_structured_output_failed", "strata_schema_validation_failed", "strata_completion_invalid", "strata_upstream_http"}:
                        failure.prompt_details["code"] = detail["code"]
                        if detail.get("field") in {"article_summary", "thumbnail_concept", "main_subject", "supporting_elements", "composition", "mood", "style", "text_in_image", "image_prompt", "negative_prompt"}:
                            failure.prompt_details["field"] = detail["field"]
                except (ValueError, TypeError, AttributeError):
                    pass
                raise failure from None
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HarnessFailure(ErrorCode.MODEL_UNAVAILABLE) from None
        prepared = PreparedImagePrompt(positive=positive, negative=negative,
            seed=request.seed if request.seed is not None else secrets.randbits(63),
            backend_sha256=backend_digest(backend))
        spec = ImageSpec(purpose="user image", subject=request.prompt[:1200], style="specified in saved prompt",
            composition="specified in saved prompt", aspect_ratio=request.aspect_ratio,
            text_requirement="", backend_preference=request.backend)
        call = ToolCall(tool="media.video.generate" if request.kind == "video" else "image.generate", backend=request.backend,
            arguments=ImageToolArguments(spec=spec, count=request.count, prepared_prompt=prepared, reference_image=reference).model_dump(mode="json"))
        self.harness.prepare_tool(call)
        graph = backend.graph(spec, request.count, prepared, reference) if reference else backend.graph(spec, request.count, prepared)
        dimensions = graph["5"]["inputs"] if request.kind == "video" else graph["latent"]["inputs"]
        plan = {"media_kind": request.kind, "prompt_source": method, "positive_prompt": positive, "negative_prompt": negative,
                "workflow": workflow, "backend": request.backend, "generation_settings": backend.model_dump(mode="json"),
                "seed": prepared.seed, "aspect_ratio": request.aspect_ratio, "count": request.count,
                "width": dimensions["width"], "height": dimensions["height"],
                "graph": graph, "spec": spec.model_dump(mode="json")}
        if reference:
            plan.update(reference_image=reference.model_dump(mode="json"), workflow="flux2_klein_distilled_edit",
                        original_instruction=request.prompt, vision_used=False)
        if request.conversation_id:
            plan["conversation_id"] = str(request.conversation_id)
        return plan, call

    def edit_plan(self, plan, call, backend, edit):
        if not edit.positive_prompt.strip() or (backend.family == "flux2-klein" and edit.negative_prompt):
            raise ValueError("invalid_prepared_prompt")
        args = ImageToolArguments.model_validate(call.arguments)
        if args.prepared_prompt is None or args.prepared_prompt.backend_sha256 != backend_digest(backend):
            raise ValueError("saved_plan_settings_changed")
        prepared = args.prepared_prompt.model_copy(update={"positive": edit.positive_prompt, "negative": edit.negative_prompt})
        args = args.model_copy(update={"prepared_prompt": prepared})
        updated = call.model_copy(update={"arguments": args.model_dump(mode="json")})
        self.harness.prepare_tool(updated)
        graph = backend.graph(args.spec, args.count, prepared, args.reference_image) if args.reference_image else backend.graph(args.spec, args.count, prepared)
        return plan | {"positive_prompt": prepared.positive, "negative_prompt": prepared.negative,
                       "graph": graph, "human_edited": True}, updated


def router():
    routes = APIRouter(prefix="/api/media")

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "同じ画面から操作してください。")

    @routes.post("/jobs", status_code=202, response_model=JobRecord)
    async def submit(body: MediaRequest, request: Request):
        same_origin(request)
        try:
            return JobRecord.model_validate(request.app.state.worker.submit_media(body), strict=False)
        except HarnessFailure as error:
            raise HTTPException(409, error.code.value) from None
        except ValueError:
            raise HTTPException(429, "生成キューを利用できません。") from None

    @routes.get("/jobs/{job_id}/plan")
    async def plan(job_id: UUID, request: Request, response: Response):
        row = request.app.state.jobs.get(str(job_id), private=True)
        if row is None or row["job_kind"] != "media":
            raise HTTPException(404, "生成ジョブがありません。")
        response.headers["Cache-Control"] = "no-store"
        return {"job_id": str(job_id), "plan": json.loads(row["image_spec_json"]) if row["image_spec_json"] else None,
                "events": request.app.state.jobs.media_events(str(job_id))}

    @routes.post("/jobs/{job_id}/retry", status_code=202, response_model=JobRecord)
    async def retry(job_id: UUID, request: Request):
        same_origin(request)
        try:
            return JobRecord.model_validate(request.app.state.worker.retry_media(str(job_id)), strict=False)
        except (ValueError, HarnessFailure):
            raise HTTPException(409, "保存済み計画・設定・実行結果の確認が必要です。自動再送はしません。") from None

    @routes.post("/jobs/{job_id}/render", status_code=202, response_model=JobRecord)
    async def render(job_id: UUID, body: MediaPromptEdit, request: Request):
        same_origin(request)
        try:
            return JobRecord.model_validate(request.app.state.worker.retry_media(str(job_id), edit=body), strict=False)
        except (ValueError, HarnessFailure):
            raise HTTPException(409, "完了した画像プレビューと同じ設定でpromptだけ編集してください。") from None

    return routes
