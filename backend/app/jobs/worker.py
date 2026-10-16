"""Bounded HOST-owned work queue independent of HTTP/UI connection lifetimes."""

import asyncio

from backend.app.harness.contracts import ToolCall, ExecutionResult, FinalStatus
from backend.app.harness.image import ImageRequest, ImageSpec, ImageToolArguments
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.context import remote_callback, current_job_id, prepared_image_call
from uuid import uuid4
from backend.app.jobs.store import JobStore


class JobWorker:
    def __init__(self, store: JobStore, harness, *, capacity: int = 32, shutdown_timeout: float = 5, media_pipeline=None, video_projects=None):
        self.store, self.harness = store, harness
        self.queue = asyncio.Queue(maxsize=capacity)
        self.shutdown_timeout = shutdown_timeout
        self.task = None
        self.closing = False
        self.media_pipeline = media_pipeline
        self.video_projects = video_projects
        self.long_videos = None

    def submit_media(self, request, *, before_queue=None):
        if self.closing or self.queue.full() or self.media_pipeline is None:
            raise ValueError("job_queue_unavailable")
        self.media_pipeline.preflight(request)
        backend = None
        if request.kind == 'video' and self.video_projects:
            request = self.video_projects.bind(request)
            backend = self.media_pipeline.preflight(request)
        job_id = self.store.create_media(request)
        if backend:
            self.video_projects.register(job_id, request, backend)
        try:
            if before_queue:
                before_queue(job_id,request)
        except Exception:
            self.store.transition(job_id,'RUNNING')
            self.store.transition(job_id,'ERROR',error='parent_binding_failed')
            raise
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    def retry_media(self, parent_id, *, edit=None):
        import json
        from backend.app.media import MediaRequest, backend_digest
        parent = self.store.get(parent_id, private=True)
        if (not parent or parent["job_kind"] != "media" or parent["status"] not in {"ERROR", "SUCCESS"}
                or not parent["image_spec_json"] or parent["remote_phase"] in {"SUBMITTING", "SUBMITTED"}
                or self.closing or self.queue.full()):
            raise ValueError("saved_plan_required")
        request = MediaRequest.model_validate_json(parent["image_request_json"]).model_copy(update={"preview_only": False})
        backend = self.media_pipeline.preflight(request)
        call = ToolCall.model_validate_json(parent["call_json"])
        args = ImageToolArguments.model_validate(call.arguments)
        if args.prepared_prompt is None or args.prepared_prompt.backend_sha256 != backend_digest(backend):
            raise ValueError("saved_plan_settings_changed")
        self.harness.prepare_tool(call)
        plan = json.loads(parent["image_spec_json"]) | {"parent_job_id": parent_id}
        if edit is not None:
            if (request.kind != "image" or parent["status"] != "SUCCESS" or parent["media_phase"] != "PROMPT_SAVED"
                    or not MediaRequest.model_validate_json(parent["image_request_json"]).preview_only):
                raise ValueError("completed_preview_required")
            plan, call = self.media_pipeline.edit_plan(plan, call, backend, edit)
        if request.kind == 'video' and self.video_projects:
            request = self.video_projects.bind(request)
        job_id = self.store.create_media(request, plan, call)
        if request.kind == 'video' and self.video_projects:
            self.video_projects.register(job_id, request, backend, retry_of=parent_id)
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    def submit_video_concat(self, project_id, clip_ids):
        if self.closing or self.queue.full() or not self.video_projects:
            raise ValueError('job_queue_unavailable')
        definition = self.harness.registry.get('media.video.generate')
        self.video_projects.assembly_clips(project_id,clip_ids,definition)
        job_id = self.store.create_video_concat(project_id,clip_ids)
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    def start(self):
        self.store.recover()
        self.task = asyncio.create_task(self._work())

    def submit(self, call: ToolCall):
        if self.closing or self.queue.full():
            raise ValueError("job_queue_unavailable")
        definition, _, _ = self.harness.prepare_tool(call)
        if definition.requires_image_compilation:
            raise ValueError("image_compiler_required")
        job_id = self.store.create(call, definition.requires_managed_gpu)
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    def submit_image(self, request):
        if self.closing or self.queue.full():
            raise ValueError("job_queue_unavailable")
        self.harness.preflight_image(request)
        if request.conversation_id:
            assets = self.media_pipeline.image_assets if self.media_pipeline else None
            if not assets or assets.history.history(str(request.conversation_id)) is None or request.count != 1:
                raise ValueError('conversation_image_scope_invalid')
        job_id = self.store.create_image(request)
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    async def _save_compiled_conversation_image(self, job_id, request, result, call):
        if not request.conversation_id:
            return
        from backend.app.harness.policy import authorized_path
        from backend.app.image_assets import MAX_IMAGE_BYTES
        definition = self.harness.registry.get(call.tool)
        if request.count != 1 or len(result.tool_result.artifacts) != 1:
            raise ValueError('conversation_image_scope_invalid')
        artifact = result.tool_result.artifacts[0]
        path = authorized_path(artifact.path,definition.allowed_roots)
        if artifact.kind != 'image' or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError('conversation_image_scope_invalid')
        await asyncio.to_thread(self.media_pipeline.image_assets.save,path.read_bytes(),request.conversation_id,source_job_id=job_id)

    def submit_image_render(self, compiler_job_id, spec):
        if self.closing or self.queue.full():
            raise ValueError("job_queue_unavailable")
        parent = self.store.get(compiler_job_id, private=True)
        if (parent is None or parent["status"] != "SUCCESS" or parent["job_kind"] != "image"
                or parent["image_phase"] != "SPEC_READY" or not parent["image_spec_json"]):
            raise ValueError("completed_compiler_required")
        request = ImageRequest.model_validate_json(parent["image_request_json"])
        if not request.compile_only:
            raise ValueError("completed_compiler_required")
        spec = ImageSpec.model_validate(spec.model_dump(mode="json"), strict=True)
        if (spec.aspect_ratio != request.aspect_ratio or spec.reference_image_required
                or spec.backend_preference not in {None, request.backend}):
            raise ValueError("compiled_image_scope_changed")
        self.harness.preflight_image(request.model_copy(update={"compile_only": False}))
        definition = self.harness.registry.get("image.generate")
        allowed = getattr(definition.adapter, "generation_backends", None)
        if allowed is not None and request.backend not in allowed:
            raise ValueError("image_backend_unverified")
        call = ToolCall(tool="image.generate", backend=request.backend,
                        arguments=ImageToolArguments(spec=spec, count=request.count).model_dump(mode="json"))
        self.harness.prepare_tool(call)
        job_id = self.store.create_image_render(compiler_job_id, request, spec, call)
        self.queue.put_nowait(job_id)
        return self.store.get(job_id)

    async def _work(self):
        while True:
            job_id = await self.queue.get()
            try:
                self.store.transition(job_id, "RUNNING")
                row = self.store.get(job_id, private=True)
                token = remote_callback.set(lambda phase, run_id: self.store.remote(job_id, phase, run_id))
                job_token = current_job_id.set(job_id)
                image_token = prepared_image_call.set(None)
                from backend.app.jobs.context import media_callback
                media_token = media_callback.set(None)
                try:
                    if row['job_kind'] in {'long_plan','long_assembly'}:
                        import json
                        key = json.loads(row['call_json'])['arguments']['parent_id' if row['job_kind']=='long_plan' else 'long_parent_id']
                        result = await (self.long_videos.plan(key) if row['job_kind']=='long_plan' else self.long_videos.assembly(key))
                    elif row['job_kind'] == 'video_concat':
                        import json
                        result = await self.video_projects.concatenate(json.loads(row['image_request_json']),
                            self.harness.registry.get('media.video.generate'))
                    elif row["job_kind"] == "media":
                        import json
                        from backend.app.media import MediaRequest
                        media_request = MediaRequest.model_validate_json(row["image_request_json"])
                        if not row["image_spec_json"]:
                            self.store.media_phase(job_id, "PROMPT_GENERATING")
                            plan, call = await self.media_pipeline.prepare(media_request)
                            self.store.prepare_media(job_id, plan, call)
                        else:
                            plan = json.loads(row["image_spec_json"])
                            call = ToolCall.model_validate_json(row["call_json"])
                        if media_request.preview_only:
                            result = ExecutionResult(request_id=uuid4(), status=FinalStatus.SUCCESS, image_spec=plan)
                        else:
                            media_callback.set(lambda phase, details: self.store.media_phase(job_id, phase, details))
                            prepared_image_call.set(call.model_dump_json())
                            result = await self.harness.run_tool(call, queued=True)
                            if media_request.kind == 'video' and media_request.project_id and self.video_projects:
                                from backend.app.harness.contracts import ErrorCode
                                try:
                                    clip = await self.video_projects.complete(job_id,result,self.harness.registry.get(call.tool))
                                except (ValueError,OSError,TimeoutError):
                                    raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
                                plan = plan | {'clip':clip}
                            if media_request.conversation_id:
                                from backend.app.harness.policy import authorized_path
                                from backend.app.image_assets import MAX_IMAGE_BYTES
                                definition = self.harness.registry.get(call.tool)
                                images = []
                                for artifact in result.tool_result.artifacts:
                                    if artifact.kind != "image":
                                        continue
                                    path = authorized_path(artifact.path, definition.allowed_roots)
                                    if path.stat().st_size > MAX_IMAGE_BYTES:
                                        raise ValueError("image_size_limit")
                                    asset = await asyncio.to_thread(self.media_pipeline.image_assets.save,
                                        path.read_bytes(), media_request.conversation_id,
                                        parent_id=media_request.reference_image_id,
                                        source_job_id=job_id)
                                    images.append(asset)
                                plan = plan | {"conversation_images": images}
                            self.store.media_phase(job_id, "COMPLETED")
                            result = result.model_copy(update={"image_spec": plan})
                    elif row["job_kind"] == "image":
                        request = ImageRequest.model_validate_json(row["image_request_json"])
                        spec, _ = await self.harness.compile_image(request, lambda spec, call: self.store.prepare_image(job_id, spec, call))
                        if request.compile_only:
                            result = ExecutionResult(request_id=uuid4(), status=FinalStatus.SUCCESS, image_spec=spec.model_dump(mode="json"))
                        else:
                            row = self.store.get(job_id, private=True)
                            call = ToolCall.model_validate_json(row["call_json"])
                            prepared_image_call.set(call.model_dump_json())
                            self.store.generating_image(job_id)
                            result = await self.harness.run_tool(call, queued=True)
                            await self._save_compiled_conversation_image(job_id,request,result,call)
                    elif row["job_kind"] == "image_render":
                        spec = ImageSpec.model_validate_json(row["image_spec_json"])
                        call = ToolCall.model_validate_json(row["call_json"])
                        prepared_image_call.set(call.model_dump_json())
                        self.store.generating_image(job_id)
                        result = await self.harness.run_tool(call, queued=True)
                        import json
                        request = ImageRequest.model_validate(json.loads(row['image_request_json'])['request'])
                        await self._save_compiled_conversation_image(job_id,request,result,call)
                        result = result.model_copy(update={"image_spec": spec.model_dump(mode="json")})
                    else:
                        result = await self.harness.run_tool(ToolCall.model_validate_json(row["call_json"]), queued=True)
                    self.store.transition(job_id, "SUCCESS", result=result)
                except HarnessFailure as error:
                    if row["job_kind"] == "media":
                        self.store.media_phase(job_id, "FAILED", {"code": error.code.value,
                            "resource_details": getattr(error, "resource_details", None),
                            "prompt_details": getattr(error, "prompt_details", None)})
                    latest = self.store.get(job_id)
                    uncertain = latest["remote_phase"] in {"SUBMITTING", "SUBMITTED"} or error.status.value == "STALLED"
                    self.store.transition(job_id, "RECOVERY_REQUIRED" if uncertain else "ERROR", error=error.code.value)
                except asyncio.CancelledError:
                    if row["job_kind"] == "media":
                        self.store.media_phase(job_id, "FAILED", {"code": "host_shutdown"})
                    self.store.transition(job_id, "RECOVERY_REQUIRED", error="host_shutdown")
                    raise
                except Exception:
                    if row["job_kind"] == "media":
                        self.store.media_phase(job_id, "FAILED", {"code": "internal_error"})
                    self.store.transition(job_id, "ERROR" if row['job_kind'] in {'video_concat','long_assembly','long_plan'} else "RECOVERY_REQUIRED", error="long_plan_failed" if row['job_kind']=='long_plan' else "long_assembly_failed" if row['job_kind']=='long_assembly' else "video_assembly_failed" if row['job_kind']=='video_concat' else "internal_error")
                finally:
                    remote_callback.reset(token)
                    current_job_id.reset(job_token)
                    prepared_image_call.reset(image_token)
                    media_callback.reset(media_token)
            finally:
                if self.long_videos:
                    try:
                        await self.long_videos.completed(job_id)
                    except Exception:
                        # Keep the successful child; never regenerate on a hook fault.
                        self.long_videos.hook_failed(job_id)
                self.queue.task_done()

    async def close(self):
        self.closing = True
        try:
            await asyncio.wait_for(self.queue.join(), self.shutdown_timeout)
        except TimeoutError:
            pass
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, self.shutdown_timeout)
            except (asyncio.CancelledError, TimeoutError):
                pass
        self.store.recover()
