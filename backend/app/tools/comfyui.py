"""Direct ComfyUI protocol; execution is always beneath Harness resource ownership."""

import asyncio
from pathlib import PurePosixPath
from uuid import uuid4
import zlib

from backend.app.harness.contracts import Artifact, ErrorCode, FinalStatus, ToolResult
from backend.app.harness.image import ImageSpec
from backend.app.harness.recovery import HarnessFailure
from backend.app.jobs.context import remote_event, media_event
from backend.app.tools.generation import ArtifactStore, ImageOutput, checked_id
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.tools.png import validate_png


class ComfyImageAdapter:
    save_node = "save"
    result_type = "image"
    reference_families = frozenset({'flux2-klein'})

    def output_node(self, graph):
        return self.save_node
    def __init__(self, transport, artifacts: ArtifactStore, workflows: ImageWorkflowRegistry,
                 *, poll_interval=1, max_polls=120, on_demand=False, generation_backends=None):
        self.http, self.artifacts, self.workflows = transport, artifacts, workflows
        if not 1 <= max_polls <= 600 or not 0 <= poll_interval <= 10:
            raise ValueError("Image polling must be bounded")
        self.poll_interval, self.max_polls = poll_interval, max_polls
        if type(on_demand) is not bool:
            raise ValueError("Invalid image lifecycle availability")
        self.on_demand = on_demand
        self.image_assets = None
        self.generation_backends = frozenset(workflows.backends if generation_backends is None else generation_backends)
        if self.generation_backends - workflows.backends.keys():
            raise ValueError("Unknown image generation backend")

    async def _ready(self, definition, graph=None):
        sample = ImageSpec(purpose="catalog", subject="catalog", style="catalog", composition="catalog",
                           aspect_ratio="1:1", text_requirement="")
        graph = graph or definition.graph(sample, 1)
        for node in graph.values():
            name = node["class_type"]
            _, raw = await self.http.json("GET", f"/object_info/{name}")
            info = raw.get(name) if isinstance(raw, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("input"), dict):
                return False
            required = info.get("input", {}).get("required", {})
            optional = info.get("input", {}).get("optional", {})
            if not isinstance(required, dict) or not isinstance(optional, dict) or set(required) - set(node["inputs"]):
                return False
            for key, value in node["inputs"].items():
                declaration = (required | optional).get(key)
                if not isinstance(declaration, list) or not declaration:
                    return False
                if isinstance(declaration[0], list) and value not in declaration[0]:
                    return False
        return True

    async def catalog(self):
        result = []
        for backend in self.workflows.backends.values():
            if backend.id not in self.generation_backends:
                result.append({"id": backend.id, "family": backend.family, "status": "compile_only"})
                continue
            try:
                async with asyncio.timeout(min(self.http.timeout, 20)):
                    ready = await self._ready(backend)
                status = "ready" if ready else "dependencies_missing"
            except (HarnessFailure, TimeoutError):
                status = "on_demand" if self.on_demand else "api_unavailable"
            result.append({"id": backend.id, "family": backend.family, "status": status})
        return result

    async def execute(self, arguments, backend):
        definition = self.workflows.get(backend)
        if (definition is None or backend not in self.generation_backends
                or arguments.spec.backend_preference not in {None, backend} or arguments.spec.reference_image_required):
            raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        if not await self._ready(definition):
            raise HarnessFailure(ErrorCode.RESOURCE_UNAVAILABLE, FinalStatus.REJECTED)
        _, queue = await self.http.json("GET", "/queue")
        if not isinstance(queue, dict) or any(not isinstance(queue.get(key), list) for key in ("queue_running", "queue_pending")):
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        if queue["queue_running"] or queue["queue_pending"]:
            raise HarnessFailure(ErrorCode.RESOURCE_BUSY, FinalStatus.REJECTED)
        if arguments.prepared_prompt is not None:
            from backend.app.media import backend_digest
            if arguments.prepared_prompt.backend_sha256 != backend_digest(definition):
                raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
        reference = arguments.reference_image
        if reference:
            if (not self.image_assets or definition.family not in self.reference_families or arguments.count != 1
                    or arguments.prepared_prompt is None or arguments.prepared_prompt.negative):
                raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)
            try:
                data = await asyncio.to_thread(self.image_assets.reference_bytes, reference)
            except (ValueError, OSError):
                raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
            # Only a Host-decoded image reaches the official local upload API.
            _, uploaded = await self.http.json("POST", "/upload/image",
                files={"image": (reference.name, data, "image/png")}, data={"type": "input", "overwrite": "false"})
            if (not isinstance(uploaded, dict) or uploaded.get("name") != reference.name
                    or uploaded.get("subfolder", "") != "" or uploaded.get("type") != "input"):
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            graph = definition.graph(arguments.spec, arguments.count, arguments.prepared_prompt, reference)
            if not await self._ready(definition, graph):
                raise HarnessFailure(ErrorCode.RESOURCE_UNAVAILABLE, FinalStatus.REJECTED)
        else:
            graph = definition.graph(arguments.spec, arguments.count, arguments.prepared_prompt)
        media_event("GENERATING")
        from backend.app.resources.media_memory import GenerationMemoryWatch
        watch = GenerationMemoryWatch()
        remote_event("SUBMITTING")
        status, raw = await self.http.json("POST", "/prompt", json={"prompt": graph, "client_id": uuid4().hex})
        if status != 200 or not isinstance(raw, dict) or raw.get("node_errors"):
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        run_id = checked_id(raw.get("prompt_id"))
        remote_event("SUBMITTED", run_id)
        for _ in range(self.max_polls):
            watch.sample()
            await watch.sample_gpu()
            _, history = await self.http.json("GET", f"/history/{run_id}")
            if not isinstance(history, dict):
                raise HarnessFailure(ErrorCode.RESULT_INVALID)
            entry = history.get(run_id)
            if entry is not None:
                if not isinstance(entry, dict) or not isinstance(entry.get("status"), dict):
                    raise HarnessFailure(ErrorCode.RESULT_INVALID)
                state = entry["status"]
                if state.get("status_str") == "error":
                    remote_event("TERMINAL", run_id)
                    watch.finish()
                    from backend.app.resources.media_memory import allocation_failure
                    allocation_failure(state)
                    raise HarnessFailure(ErrorCode.TOOL_FAILED)
                if state.get("completed") is True:
                    remote_event("TERMINAL", run_id)
                    watch.finish()
                    outputs = entry.get("outputs", {})
                    output_node = self.output_node(graph)
                    images = outputs.get(output_node, {}).get("images") if isinstance(outputs, dict) and isinstance(outputs.get(output_node), dict) else None
                    if state.get("status_str") != "success" or not isinstance(images, list) or len(images) != arguments.count:
                        raise HarnessFailure(ErrorCode.RESULT_INVALID)
                    artifacts = []
                    for image in images:
                        artifacts.append(await self._artifact(image, graph))
                    output = ImageOutput(run_id=run_id, status="completed", artifact_count=len(artifacts))
                    return ToolResult(success=True, result_type=self.result_type, output=output.model_dump(), artifacts=tuple(artifacts),
                                      http_status=200, run_id=run_id, run_status="completed")
            await asyncio.sleep(self.poll_interval)
        raise HarnessFailure(ErrorCode.TIMEOUT, FinalStatus.STALLED)

    def _output_params(self, image, suffix):
        if not isinstance(image, dict) or image.get("type") != "output":
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        filename, folder = image.get("filename"), image.get("subfolder", "")
        if (not isinstance(filename, str) or not filename.endswith(suffix) or PurePosixPath(filename).name != filename
                or not isinstance(folder, str) or PurePosixPath(folder).is_absolute()
                or ".." in PurePosixPath(folder).parts or any(t in filename + folder for t in ("\\", "%"))
                or any(ord(char) < 32 or ord(char) == 127 for char in filename + folder)):
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        return {"filename": filename, "subfolder": folder, "type": "output"}

    async def _artifact(self, image, graph):
        params = self._output_params(image, ".png")
        _, mime, data = await self.http.request("GET", "/view", params=params, max_bytes=20*1024*1024)
        try:
            if mime != "image/png":
                raise ValueError
            width, height = validate_png(data)
            if (width, height) != (graph["latent"]["inputs"]["width"], graph["latent"]["inputs"]["height"]):
                raise ValueError("Unexpected PNG dimensions")
        except (ValueError, zlib.error, IndexError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        return Artifact(path=self.artifacts.save(data, ".png"), kind="image")
