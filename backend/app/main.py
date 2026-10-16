import asyncio
import json
from contextlib import suppress, nullcontext
import os
from pathlib import Path
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from typing import Literal, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.config import ROOT, Settings, load_settings
from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ExecutionResult, ResourceState, ToolCall, ModelSelection
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.harness.orchestrator import Harness
from backend.app.models.base import Message, ModelAdapter, ModelError, ModelNotConfigured, ProposalAdapter
from backend.app.models.registry import ModelRegistry
from backend.app.models.strata import StrataOptions
from backend.app.resources.memory import memory_state
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.config import ServiceSettings, load_services
from backend.app.tools.services import public_services, service_registry
from backend.app.resources.manager import GPUManager, NvidiaInspector, GPUUnsafe
from backend.app.resources.ownership import OwnershipPolicy
from backend.app.resources.process import OwnedProcess
from backend.app.resources.config import ResourceSettings, load_resources
from backend.app.jobs.store import JobStore
from backend.app.jobs.chat_generations import ChatGenerationStore
from backend.app.jobs.worker import JobWorker
from backend.app.jobs.contracts import JobRecord
from backend.app.harness.image import ImageRequest, ImageSpec
from backend.app.tools.image_workflows import ImageWorkflowRegistry, load_image_workflows
from urllib.parse import urlsplit
from backend.app.providers.config import CloudSettings, load_cloud
from backend.app.providers.openai import OpenAIProvider
from backend.app.harness.cloud import CloudPolicy, CloudPrivacyGate
from backend.app.harness.render import JapaneseRenderer
from backend.app.tools.work_config import WorkSettings, load_work
from backend.app.work_explanation import HumanExplanationLayer


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)
    conversation_id: UUID | None = None
    client_request_id: UUID | None = None
    model: Literal["qwen", "bonsai", "qwen80b", "strata", "vision"] | None = None
    strata_options: StrataOptions | None = None
    mode: Literal["chat", "library", "pt_learning", "clinical"] = "chat"
    evidence_query: str | None = Field(default=None, min_length=2, max_length=120)
    external_retrieval: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def evidence_options(self):
        if self.strata_options is not None and self.model != "strata":
            raise ValueError("Strata設定はStrata選択時だけ指定できます。")
        if self.model in {"strata", "vision"} and self.mode != "chat":
            raise ValueError("選択モデルは通常Chatで利用してください。")
        if self.mode not in {"pt_learning", "clinical"} and (self.evidence_query is not None or self.external_retrieval):
            raise ValueError("Evidence options require an explicit PT mode")
        if self.evidence_query is not None and not self.evidence_query.strip():
            raise ValueError("Evidence query must not be blank")
        return self

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str):
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ChatResponse(BaseModel):
    conversation_id: UUID
    reply: str
    reasoning_content: str | None = Field(default=None, repr=False)
    grounded_answer: dict | None = None
    pt_answer: dict | None = None
    model_selection: ModelSelection | None = None
    renderer_used: bool = False
    cloud_used: bool = False
    generation_metrics: dict | None = None


class ConversationRating(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["japanese", "reasoning", "instructions", "coding", "context", "speed"]
    score: int = Field(ge=1, le=5, strict=True)
    note: str = Field(default="", max_length=2000)


class ModelLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop", "recover"]


def create_app(settings: Settings | None = None, adapter: ModelAdapter | ProposalAdapter | None = None,
               *, adapters: Mapping[str, ModelAdapter | ProposalAdapter] | None = None,
               services: tuple[ServiceSettings, ...] | None = None,
               resource_settings: ResourceSettings | None = None, image_workflows: ImageWorkflowRegistry | None = None,
               cloud_settings: CloudSettings | None = None, work_settings: WorkSettings | None = None) -> FastAPI:
    settings = settings or load_settings()
    services = load_services() if services is None else services
    store = HistoryStore(settings.database)
    resource_settings = resource_settings or load_resources()
    image_workflows = load_image_workflows() if image_workflows is None else image_workflows
    cloud_settings = load_cloud() if cloud_settings is None else cloud_settings
    work_settings = load_work() if work_settings is None else work_settings
    work_explainer = HumanExplanationLayer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        # v0.1 runs with one worker; serialize turns to preserve history ordering.
        app.state.turn_lock = asyncio.Lock()
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            drivers = {profile.name: OwnedProcess(profile, client) for profile in resource_settings.profiles}
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
            manager = GPUManager(store, NvidiaInspector(), drivers, max_idle_mib=resource_settings.max_idle_mib,
                                 host_lock_path=runtime / f"pitch-chat-{os.getuid()}-gpu0.host.lock",
                                 release_timeout=resource_settings.release_timeout, queue_timeout=resource_settings.queue_timeout,
                                 idle_timeout=resource_settings.idle_timeout_seconds,
                                 ownership_policy=OwnershipPolicy(resource_settings.external_processes, resource_settings.reserve_mib))
            manager.open()
            worker = None
            try:
                app.state.gpu_manager = manager
                app.state.shared_model_client = client
                manager.start_maintenance()
                app.state.resources = ResourceCoordinator(manager)
                overrides = dict(adapters or {})
                if adapter is not None:
                    if "qwen" in overrides:
                        raise ValueError("Duplicate qwen adapter override")
                    overrides["qwen"] = adapter
                app.state.models = ModelRegistry(settings, client, overrides, frozenset(drivers))
                gpu_profiles = frozenset(profile.name for profile in resource_settings.profiles if profile.device == "gpu")
                origins = {profile.name: f"{urlsplit(profile.health_url).scheme}://{urlsplit(profile.health_url).netloc}"
                           for profile in resource_settings.profiles if profile.device == "gpu"}
                registry = service_registry(services, client, settings.database.parent / "artifacts", gpu_profiles,
                                            image_workflows=image_workflows, owned_origins=origins, work_settings=work_settings)
                policy = TurnPolicy(allowed_tools=frozenset(service.tool for service in services if service.enabled)
                                    | (frozenset({"media.video.generate"}) if registry.get("media.video.generate") else frozenset())
                                    | (frozenset({"pitch_work.run"}) if work_settings.enabled else frozenset()))
                purposes = frozenset(p for p in ("reasoning_fallback", "japanese_renderer") if getattr(cloud_settings, p).enabled)
                gate = CloudPrivacyGate(CloudPolicy(cloud_settings.enabled, purposes,
                    frozenset(cloud_settings.reviewed_public_payloads), frozenset(cloud_settings.reviewed_render_pairs)))
                reasoning = OpenAIProvider(cloud_settings.reasoning_fallback, gate, "reasoning_fallback") if "reasoning_fallback" in purposes else None
                renderer = JapaneseRenderer(OpenAIProvider(cloud_settings.japanese_renderer, gate, "japanese_renderer"), gate) if "japanese_renderer" in purposes else None
                app.state.harness = Harness(adapter, app.state.resources, AuditTrail(store),
                                            model_name=settings.model_name, timeout_seconds=settings.timeout_seconds,
                                            models=app.state.models, registry=registry, policy=policy,
                                            evidence_limit=settings.library_evidence_limit, snippet_chars=settings.library_snippet_chars,
                                            cloud_gate=gate, reasoning_provider=reasoning, japanese_renderer=renderer)
                if os.environ.get('PITCH_CHAT_WORK_EXPLANATION_LOCAL') == '1' and any(
                    profile.name == 'qwen' and profile.device == 'cpu' for profile in resource_settings.profiles
                ):
                    # Existing managed CPU model, shared resource boundary; no tools,
                    # cloud provider, Work data or authority are given to this Harness.
                    wording = Harness(None, app.state.resources, AuditTrail(store),
                                      models=app.state.models, policy=TurnPolicy(allowed_tools=frozenset()))
                    async def select_wording(variants):
                        async with app.state.turn_lock:
                            value = await wording.run([
                                Message('system', '説明文の言い回しを選んでください。判断や事実の追加は禁止です。返答は0または1だけ。'),
                                Message('user', '\n'.join(f'{i}: {s}' for i, s in enumerate(variants))),
                            ], model_id='qwen')
                            return value.content
                    work_explainer.selector = select_wording
                app.state.jobs = JobStore(store)
                from backend.app.image_assets import ImageAssetStore
                app.state.image_assets = ImageAssetStore(store, settings.database.parent / "image-assets")
                app.state.image_upload_lock = asyncio.Lock()
                image_definition = registry.get("image.generate")
                if image_definition:
                    image_definition.adapter.image_assets = app.state.image_assets
                video_definition = registry.get('media.video.generate')
                if video_definition:
                    video_definition.adapter.image_assets = app.state.image_assets
                from backend.app.video_projects import VideoProjects
                app.state.video_projects = VideoProjects(store, app.state.image_assets)
                vision_definition = registry.get('image.understand')
                if vision_definition:
                    vision_definition.adapter.image_assets = app.state.image_assets
                    vision_definition.adapter.manager = manager
                app.state.chat_generations = ChatGenerationStore(store)
                from backend.app.deletion import Deletions
                app.state.deletions = Deletions(store)
                app.state.chat_generation_tasks = {}
                from backend.app.media import MediaPipeline
                media_pipeline = MediaPipeline(app.state.harness, client, os.environ.get("PITCH_CHAT_SIMPLE_UI_ORIGIN", ""), app.state.image_assets)
                worker = app.state.worker = JobWorker(app.state.jobs, app.state.harness, media_pipeline=media_pipeline,
                                                     video_projects=app.state.video_projects)
                from backend.app.long_videos import LongVideos
                app.state.long_videos = LongVideos(store, worker)
                worker.start()
                yield
            finally:
                try:
                    generation_tasks = dict(getattr(app.state, "chat_generation_tasks", {}))
                    for task in generation_tasks.values():
                        task.cancel()
                    if generation_tasks:
                        await asyncio.gather(*generation_tasks.values(), return_exceptions=True)
                        for job_id in generation_tasks:
                            app.state.chat_generations.interrupted(job_id)
                    if worker is not None:
                        await worker.close()
                finally:
                    await manager.close()

    app = FastAPI(title="Pitch-chat", version="0.1.0", lifespan=lifespan)
    from backend.app.deletion import DeletionBoundary, router as deletion_router
    app.add_middleware(DeletionBoundary, host=app)
    app.include_router(deletion_router())
    from backend.app.long_videos import router as long_video_router
    app.include_router(long_video_router())
    from backend.app.media import router as media_router
    app.include_router(media_router())
    from backend.app.image_assets import router as image_assets_router
    app.include_router(image_assets_router())
    from backend.app.video_projects import router as video_projects_router
    app.include_router(video_projects_router())
    from backend.app.resources.shared import router as shared_model_router
    app.include_router(shared_model_router())
    from backend.app.work_bridge import WorkClient, router as work_router
    app.include_router(work_router(store, WorkClient(), work_explainer))

    @app.get("/health")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        model_state = app.state.models.public_state()
        default = next(model for model in model_state["models"] if model["id"] == model_state["default_model"])
        return {"status": "ok", "version": "0.1.0", "storage": "ok",
                "model": {"adapter": default["id"], "configured": default["configured"]}}

    @asynccontextmanager
    async def chat_turn():
        try:
            await asyncio.wait_for(app.state.turn_lock.acquire(), app.state.gpu_manager.queue_timeout)
        except TimeoutError:
            raise HTTPException(504, "会話の生成待ちがタイムアウトしました。") from None
        try:
            yield
        finally:
            app.state.turn_lock.release()

    async def execute_chat(request: ChatRequest, on_delta=None, *, reserved_conversation=False, generation_job_id=None):
        selected = request.model or app.state.models.default_model
        manager = app.state.gpu_manager
        retained = manager.usage(selected) if selected in manager.drivers else nullcontext()
        async with retained, chat_turn():
            if generation_job_id and not app.state.chat_generations.start(generation_job_id):
                raise asyncio.CancelledError
            conversation_id = str(request.conversation_id or uuid4())
            history = store.history(conversation_id) if request.conversation_id else []
            if history is None and reserved_conversation:
                history = []
            if history is None:
                raise HTTPException(404, "会話が見つかりません。")
            messages = [Message(row["role"], row["content"]) for row in history]
            notice = app.state.image_assets.text_only_notice(conversation_id)
            messages.append(Message("user", request.message))
            try:
                result = await app.state.harness.run(messages, model_id=request.model, library=request.mode == "library",
                    pt_mode=request.mode if request.mode in {"pt_learning", "clinical"} else None,
                    evidence_query=request.evidence_query, external_retrieval=request.external_retrieval,
                    generation_options=request.strata_options.model_dump() if request.strata_options else None, on_delta=on_delta,
                    **({"images_not_provided": True} if notice else {}))
                if result.content is None:
                    raise ModelError("このチャット経路ではToolを利用できません。")
            except ModelNotConfigured as error:
                raise HTTPException(503, str(error)) from None
            except ModelError as error:
                if isinstance(error, HarnessFailure) and error.resource_details is not None:
                    from backend.app.resources.memory_recovery import failure_detail
                    raise HTTPException(409, failure_detail(error.code.value, error.resource_details)) from None
                raise HTTPException(502, str(error)) from None
            except HarnessFailure as error:
                code = 504 if error.status.value == "STALLED" else 409 if error.code.value.startswith("resource_") else 502
                raise HTTPException(code, "モデルの実行を完了できません。起動状態とリソース安全条件を確認してください。") from None
            metrics = result.generation_metrics.model_dump() if result.generation_metrics else None
            metadata = ({"model": "strata", "reasoning_effort": (request.strata_options or StrataOptions()).reasoning_effort,
                         **metrics} if metrics else None)
            answer = ChatResponse(conversation_id=UUID(conversation_id), reply=result.content,
                grounded_answer=result.grounded_answer, reasoning_content=result.reasoning_content,
                pt_answer=result.pt_answer, model_selection=result.model_selection, generation_metrics=metrics,
                renderer_used=result.renderer_used, cloud_used=result.cloud_used)
            if generation_job_id:
                safe_answer = answer.model_dump(mode="json", exclude_none=True)
                safe_answer.pop("reasoning_content", None)
                app.state.chat_generations.complete_turn(generation_job_id, conversation_id, request.message,
                    result.content, result.request_id, metadata, safe_answer)
            else:
                store.save_turn(conversation_id, request.message, result.content, request_id=result.request_id,
                    metadata=metadata)
            return result, answer

    @app.post("/api/chat", response_model=ChatResponse, response_model_exclude_none=True)
    async def chat(request: ChatRequest, response: Response):
        result, answer = await execute_chat(request)
        response.headers["X-Request-ID"] = str(result.request_id)
        return answer

    def chat_generation_response(row):
        return app.state.chat_generations.public(row)

    async def run_chat_generation(job_id, request_data):
        jobs = app.state.chat_generations
        try:
            row = jobs.get(job_id)
            if row is None or row["status"] != "QUEUED":
                return
            payload = dict(request_data)
            payload.pop("client_request_id", None)
            chat_request = ChatRequest.model_validate(payload)
            pending = []
            async def persist_delta(text):
                pending.append(text)
            async def flush_partial():
                while True:
                    await asyncio.sleep(0.25)
                    if pending:
                        batch = "".join(pending); pending.clear()
                        jobs.append(job_id, batch)
            flush_task = asyncio.create_task(flush_partial())
            from backend.app.models.qwen35 import trace_job
            with trace_job(job_id):
                _, answer = await execute_chat(chat_request, persist_delta, reserved_conversation=True, generation_job_id=job_id)
        except asyncio.CancelledError:
            if 'pending' in locals() and pending:
                with suppress(asyncio.CancelledError):
                    jobs.append(job_id, "".join(pending))
                pending.clear()
            jobs.cancelled(job_id)
            raise
        except HTTPException as error:
            if 'pending' in locals() and pending:
                with suppress(asyncio.CancelledError):
                    jobs.append(job_id, "".join(pending))
            import logging
            logging.getLogger('uvicorn.error').warning('chat_generation_failed job_id=%s http_status=%s exception_type=%s',
                job_id, error.status_code, type(error).__name__)
            detail = error.detail
            if (isinstance(detail, dict) and isinstance(detail.get("resource"), dict)
                    and detail["resource"].get("reason") in {"insufficient_ram", "insufficient_swap_reserve", "memory_pressure", "insufficient_cgroup_ram", "memory_safety_telemetry_unavailable"}):
                jobs.fail(job_id, detail["message"])
            else:
                jobs.fail(job_id, f"http_{error.status_code}")
        except Exception as error:
            if 'pending' in locals() and pending:
                with suppress(asyncio.CancelledError):
                    jobs.append(job_id, "".join(pending))
            import logging
            logging.getLogger('uvicorn.error').error('chat_generation_failed job_id=%s exception_type=%s', job_id, type(error).__name__)
            jobs.fail(job_id, type(error).__name__)
        finally:
            if 'flush_task' in locals():
                flush_task.cancel()
                with suppress(asyncio.CancelledError):
                    await flush_task
            app.state.chat_generation_tasks.pop(job_id, None)

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest, response: Response):
        if request.model not in {"strata", "vision"} or request.mode != "chat" or request.client_request_id is None:
            raise HTTPException(422, "ストリーミングはStrataまたはQwen3.5の通常Chatで利用してください。")
        existing = app.state.chat_generations.by_client_request(str(request.client_request_id))
        conversation_id = str(request.conversation_id or (existing["conversation_id"] if existing else uuid4()))
        if request.conversation_id and store.history(conversation_id) is None:
            raise HTTPException(404, "会話が見つかりません。")
        bound = request.model_copy(update={"conversation_id": UUID(conversation_id)})
        payload = bound.model_dump(mode="json")
        try:
            row, created = app.state.chat_generations.create(str(request.client_request_id), conversation_id, payload)
        except ValueError as error:
            detail = "別のStrata生成が進行中です。" if str(error) == "generation_busy" else "この送信IDは別の内容に使用されています。"
            raise HTTPException(409, detail) from None
        if created:
            task = asyncio.create_task(run_chat_generation(row["job_id"], payload))
            app.state.chat_generation_tasks[row["job_id"]] = task
        response.headers["Cache-Control"] = "no-store"
        return chat_generation_response(row)

    @app.get("/api/chat/stream/by-client/{client_request_id}")
    async def resume_chat_generation(client_request_id: UUID, response: Response):
        row = app.state.chat_generations.by_client_request(str(client_request_id))
        if row is None:
            raise HTTPException(404, "復元できる生成はありません。")
        response.headers["Cache-Control"] = "no-store"
        return chat_generation_response(row)

    @app.get("/api/chat/stream/jobs/{job_id}")
    async def get_chat_generation(job_id: UUID, response: Response):
        row = app.state.chat_generations.get(str(job_id))
        if row is None:
            raise HTTPException(404, "生成が見つかりません。")
        response.headers["Cache-Control"] = "no-store"
        return chat_generation_response(row)

    @app.get("/api/chat/stream/jobs/{job_id}/events")
    async def chat_generation_events(job_id: UUID):
        row = app.state.chat_generations.get(str(job_id))
        if row is None:
            raise HTTPException(404, "生成が見つかりません。")
        body = f"event: snapshot\ndata: {json.dumps({'reply': row['reply'], 'revision': row['revision']}, ensure_ascii=False)}\n\n"
        if row["status"] == "COMPLETED":
            body += f"event: done\ndata: {json.dumps(json.loads(row['answer_json']), ensure_ascii=False)}\n\n"
        elif row["status"] in {"ERROR", "CANCELLED", "INTERRUPTED"}:
            detail = "生成を停止しました。" if row["status"] == "CANCELLED" else "生成を完了できませんでした。"
            body += f"event: error\ndata: {json.dumps({'detail': detail + ' 参照ID: ' + str(job_id), 'status': row['status']}, ensure_ascii=False)}\n\n"
        return Response(body, media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/stream/jobs/{job_id}/cancel")
    async def cancel_chat_generation(job_id: UUID, response: Response):
        key = str(job_id)
        if app.state.chat_generations.get(key) is None:
            raise HTTPException(404, "生成が見つかりません。")
        if app.state.chat_generations.request_cancel(key):
            task = app.state.chat_generation_tasks.get(key)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            app.state.chat_generations.cancelled(key)
        response.headers["Cache-Control"] = "no-store"
        return chat_generation_response(app.state.chat_generations.get(key))

    @app.get("/api/models/strata/preflight")
    async def strata_preflight(response: Response):
        response.headers["Cache-Control"] = "no-store"
        manager = app.state.gpu_manager
        driver = manager.drivers.get("strata")
        state = memory_state()
        reason = None
        memory_detail = None
        # Show current GPU/candidates even when RAM or Swap admission fails first.
        # This read grants no permission to start, stop or adopt a process.
        try:
            await manager.inspect()
        except GPUUnsafe as error:
            reason = str(error)
        if driver is None:
            reason = "profile_unmanaged"
        elif reason is None:
            try:
                await manager.preflight("strata")
            except GPUUnsafe as error:
                reason = str(error)
                memory_detail = getattr(error, 'memory_details', None)
        from backend.app.resources.memory_recovery import admission_message, recovery_assessment
        ram = driver.profile.required_ram_mib if driver else 0
        swap = driver.profile.min_swap_free_mib if driver else 0
        physical = getattr(driver.profile, 'low_swap_start_ram_mib', 0) if driver else 0
        admission_ram = max(ram, physical) if physical and state['swap_free_mib'] < swap else ram
        if manager.loaded == 'strata' and driver:
            admission_ram = driver.profile.runtime_ram_reserve_mib
        if memory_detail:
            state.update(memory_detail)
        elif physical:
            state.update(admission_mode='already_loaded' if manager.loaded == 'strata' else ('physical_ram' if state['swap_free_mib'] < swap else 'swap_reserve'),
                         admission_ram_mib=admission_ram)
        reading = manager.reading
        return state | {"message": admission_message(reason, state, admission_ram, swap),
                        "recovery": recovery_assessment(state, ram)} | {"ready_to_start": reason is None, "reason": reason,
            "blocking_owners": manager.blocking_owners(),
            "vram_free_mib": reading.total_mib - reading.used_mib if reading and reading.total_mib else None,
            "low_swap_start_ram_mib": physical,
            "required_ram_mib": driver.profile.required_ram_mib if driver else None,
            "required_vram_mib": driver.profile.required_vram_mib if driver else None,
            "min_swap_free_mib": driver.profile.min_swap_free_mib if driver else None,
            "existing_services_will_not_be_stopped": True}

    @app.get("/api/resources", response_model=ResourceState)
    async def resources():
        # Read on the same event loop as the coordinator; no hardware side effects.
        return app.state.harness.resource_state()

    @app.get("/api/models")
    async def models():
        state = app.state.models.public_state()
        devices = {profile.name: profile.device for profile in resource_settings.profiles}
        for model in state["models"]:
            model["device"] = devices.get(model["id"])
        return state

    @app.post("/api/models/{model_id}/lifecycle")
    async def model_lifecycle(model_id: Literal["qwen", "bonsai", "qwen80b", "strata", "vision"], request: ModelLifecycleRequest):
        try:
            return await app.state.harness.model_lifecycle(model_id, request.action)
        except ModelNotConfigured:
            raise HTTPException(503, "HOST-owned model profileが未設定です。") from None
        except HarnessFailure as error:
            detail = error.resource_details
            if detail is not None:
                from backend.app.resources.memory_recovery import failure_detail
                raise HTTPException(409, failure_detail(error.code.value, detail)) from None
            raise HTTPException(409, "GPU所有権・実行中job・lifecycle条件を確認してください。") from None

    @app.get("/api/tools")
    async def tools():
        state = public_services(services, app.state.harness.registry)
        definition = app.state.harness.registry.get("pitch_work.run")
        state["tools"].append({"id": "pitch_work.run", "configured": bool(work_settings.source_root),
            "enabled": definition.enabled, "backends": [], "projects": sorted(work_settings.projects),
            "reason": None if definition.enabled else "lifecycle_ownership_unverified"})
        return state

    @app.get("/api/gpu")
    async def gpu():
        manager = app.state.gpu_manager
        if resource_settings.inspect_gpu:
            try:
                await manager.inspect()
            except GPUUnsafe:
                return manager.snapshot() | {"telemetry_error": "unavailable"}
        return manager.snapshot()

    @app.post("/api/jobs", status_code=202, response_model=JobRecord)
    async def submit_job(call: ToolCall):
        if call.tool == "image.generate":
            raise HTTPException(409, "画像はImage Prompt Compilerを通す /api/images/jobs へsubmitしてください。")
        try:
            return JobRecord.model_validate(app.state.worker.submit(call), strict=False)
        except HarnessFailure as error:
            raise HTTPException(403 if error.code.value == "policy_denied" else 422, str(error)) from None
        except ValueError:
            raise HTTPException(429, "HOST job queueを利用できません。") from None

    @app.get("/api/images/backends")
    async def image_backends():
        definition = app.state.harness.registry.get("image.generate")
        if definition is None:
            return {"backends": [], "generation_enabled": False}
        return {"backends": await definition.adapter.catalog(), "generation_enabled": definition.enabled,
                "prompt_modes": {"direct": True, "strata": bool(app.state.worker.media_pipeline.origin)}}

    @app.get("/api/media/backends")
    async def media_backends():
        definition = app.state.harness.registry.get("media.video.generate")
        return {"video": await definition.adapter.catalog() if definition and definition.enabled else [],
                "prompt_modes": ["direct", "strata", "qwen35"], "default_video_backend": "ltx-video",
                "default_prompt_mode": "direct"}

    @app.post("/api/images/jobs", status_code=202, response_model=JobRecord)
    async def image_job(request: ImageRequest):
        try:
            return JobRecord.model_validate(app.state.worker.submit_image(request), strict=False)
        except HarnessFailure as error:
            raise HTTPException(403, str(error)) from None
        except ValueError:
            raise HTTPException(429, "HOST image queueを利用できません。") from None

    @app.post("/api/images/jobs/{job_id}/render", status_code=202, response_model=JobRecord)
    async def render_image(job_id: UUID, spec: ImageSpec):
        try:
            return JobRecord.model_validate(app.state.worker.submit_image_render(str(job_id), spec), strict=False)
        except HarnessFailure as error:
            raise HTTPException(403, str(error)) from None
        except ValueError as error:
            status = 429 if str(error) == "job_queue_unavailable" else 409
            raise HTTPException(status, "保存済みプロンプト案と画像生成の条件を確認してください。") from None

    @app.get("/api/jobs")
    async def jobs():
        return {"jobs": app.state.jobs.list()}

    @app.get("/api/jobs/{job_id}", response_model=JobRecord)
    async def job(job_id: UUID):
        record = app.state.jobs.get(str(job_id))
        if record is None:
            raise HTTPException(404, "jobが見つかりません。")
        return JobRecord.model_validate(record, strict=False)

    @app.get("/api/jobs/{job_id}/result")
    async def job_result(job_id: UUID):
        import json
        record = app.state.jobs.get(str(job_id), private=True)
        if record is None:
            raise HTTPException(404, "jobが見つかりません。")
        if record["status"] != "SUCCESS":
            raise HTTPException(409, "jobは完了していません。")
        result = json.loads(record["result_json"])
        if result.get("tool_result"):
            visible = []
            for index, artifact in enumerate(result["tool_result"]["artifacts"]):
                if artifact.get('deleted'):
                    continue
                artifact["path"] = f"/api/jobs/{job_id}/artifacts/{index}"
                visible.append(artifact)
            result["tool_result"]["artifacts"] = visible
        return result

    @app.get("/api/jobs/{job_id}/artifacts/{index}")
    async def job_artifact(job_id: UUID, index: int):
        import json
        from backend.app.harness.policy import authorized_path
        record = app.state.jobs.get(str(job_id), private=True)
        if record is None or record["status"] != "SUCCESS":
            raise HTTPException(404, "artifactが見つかりません。")
        result = json.loads(record["result_json"])
        artifacts = result.get("tool_result", {}).get("artifacts", [])
        if not 0 <= index < len(artifacts):
            raise HTTPException(404, "artifactが見つかりません。")
        if artifacts[index].get("deleted"):
            raise HTTPException(410, "削除済みのartifactです。")
        definition = app.state.harness.registry.get(record["tool"])
        if definition is None:
            raise HTTPException(409, "artifact policyが未設定です。")
        try:
            path = authorized_path(artifacts[index]["path"], definition.allowed_roots)
        except HarnessFailure:
            raise HTTPException(403, "artifactへのアクセスは許可されていません。") from None
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(404, "artifactが見つかりません。")
        return FileResponse(path)

    @app.post("/api/tools/run", response_model=ExecutionResult)
    async def run_tool(call: ToolCall):
        try:
            definition, _, _ = app.state.harness.prepare_tool(call)
            if definition.remote_job:
                raise HTTPException(409, "このToolは /api/jobs へsubmitしてください。")
            return await app.state.harness.run_tool(call)
        except HarnessFailure as error:
            code = 504 if error.status.value == "STALLED" else 403 if error.code.value == "policy_denied" else 409 if error.code.value.startswith("resource_") else 422 if error.code.value == "schema_invalid" else 502
            raise HTTPException(code, str(error)) from None

    @app.get("/api/conversations")
    def conversations():
        return {"conversations": store.list_conversations()}

    @app.get("/api/conversations/{conversation_id}")
    def history(conversation_id: UUID):
        messages = store.history(str(conversation_id))
        if messages is None:
            raise HTTPException(404, "会話が見つかりません。")
        return {"conversation_id": str(conversation_id), "messages": messages}

    @app.get("/api/conversations/{conversation_id}/evaluation")
    def evaluation(conversation_id: UUID, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if store.history(str(conversation_id)) is None:
            raise HTTPException(404, "会話が見つかりません。")
        return store.evaluation(str(conversation_id))

    @app.post("/api/conversations/{conversation_id}/evaluation")
    def rate(conversation_id: UUID, rating: ConversationRating, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if store.history(str(conversation_id)) is None:
            raise HTTPException(404, "会話が見つかりません。")
        store.rate(str(conversation_id), rating.category, rating.score, rating.note)
        return store.evaluation(str(conversation_id))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "frontend/index.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def web_manifest():
        return FileResponse(ROOT / "frontend/manifest.webmanifest", media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(ROOT / "frontend/sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")
    from backend.app.resources.control import resource_routes
    app.include_router(resource_routes(app))
    return app
