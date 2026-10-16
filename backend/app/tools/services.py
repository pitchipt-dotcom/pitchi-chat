"""Build service adapters beneath Harness, never a direct executor/router."""

from pathlib import Path
from dataclasses import replace
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

from backend.app.harness.recovery import RecoveryPolicy
from backend.app.resources.gpu import ResourceMode
from backend.app.tools.book import BookAdapter, BookArguments, BookOutput
from backend.app.tools.config import ServiceSettings
from backend.app.tools.generation import ArtifactStore, ImageOutput, VideoAdapter, VideoArguments, VideoOutput
from backend.app.tools.comfyui import ComfyImageAdapter
from backend.app.tools.image_workflows import ImageWorkflowRegistry
from backend.app.harness.image import ImageToolArguments
from backend.app.tools.http import ServiceHTTP
from backend.app.tools.registry import ToolDefinition, ToolRegistry
from backend.app.tools.irodori import IrodoriAdapter, TTSArguments, TTSOutput
from backend.app.tools.ptotsearch import PTOTSearchAdapter
from backend.app.tools.retrieval import PTOTArguments, RetrievalOutput


def service_registry(settings: tuple[ServiceSettings, ...], client: httpx.AsyncClient, artifact_root: Path,
                     managed_profiles: frozenset[str] = frozenset(), *, image_workflows: ImageWorkflowRegistry | None = None,
                     owned_origins: dict[str, str] | None = None, work_settings=None) -> ToolRegistry:
    artifacts = ArtifactStore(artifact_root)
    definitions = []
    for service in settings:
        if not service.api_base_url and service.tool != "image.generate":
            continue
        transport = ServiceHTTP(client, service.api_base_url, service.timeout_seconds, public_retrieval=service.public_retrieval)
        common = dict(name=service.tool, requires_http_status=True,
                      recovery=RecoveryPolicy(timeout_seconds=service.timeout_seconds), allow_private=True)
        if service.tool == "book.search":
            definitions.append(ToolDefinition(**common, adapter=BookAdapter(transport), argument_schema=BookArguments,
                                               output_schema=BookOutput, result_type="structured", mode=ResourceMode.WORK,
                                               enabled=service.enabled, requires_managed_gpu=False, read_only_evidence=True))
        elif service.tool == "ptotsearch.search":
            definitions.append(ToolDefinition(**common, adapter=PTOTSearchAdapter(transport), argument_schema=PTOTArguments,
                output_schema=RetrievalOutput, result_type="structured", mode=ResourceMode.WORK, enabled=service.enabled,
                requires_managed_gpu=False, read_only_evidence=True, external_retrieval=service.public_retrieval))
        elif service.tool == "image.generate":
            workflows = image_workflows or ImageWorkflowRegistry()
            verified = service.image_protocol == "comfyui"
            transport = ServiceHTTP(client, service.api_base_url if verified else "", service.timeout_seconds)
            origin_matches = (owned_origins or {}).get(service.resource_profile) == service.api_base_url.rstrip("/")
            hostname = urlsplit(service.api_base_url).hostname
            try:
                local = hostname == "localhost" or ip_address(hostname).is_loopback
            except (ValueError, TypeError):
                local = False
            definitions.append(ToolDefinition(**common, adapter=ComfyImageAdapter(transport, artifacts, workflows,
                                               max_polls=min(480, max(1, int(service.timeout_seconds) - 60)),
                                               on_demand=bool(service.enabled and verified and origin_matches and local
                                                              and service.host_lifecycle_only
                                                              and service.resource_profile in managed_profiles),
                                               generation_backends=frozenset(service.generation_backends) if service.generation_backends else None),
                                               argument_schema=ImageToolArguments, output_schema=ImageOutput, result_type="image",
                                               mode=ResourceMode.IMAGE, allowed_backends=frozenset(workflows.backends),
                                               allowed_roots=(artifacts.root,), min_artifacts=1,
                                               enabled=bool(service.enabled and verified and workflows.backends and origin_matches and local
                                                            and service.host_lifecycle_only and service.resource_profile in managed_profiles),
                                               resource_profile=service.resource_profile, remote_job=True, requires_image_compilation=True,
                                               remote_resource=False))
        elif service.tool == "image.understand":
            from backend.app.tools.vision import VisionAdapter, VisionArguments, VisionOutput
            matched = (owned_origins or {}).get('vision') == service.api_base_url.rstrip('/')
            local = urlsplit(service.api_base_url).hostname in {'127.0.0.1', 'localhost', '::1'}
            definitions.append(ToolDefinition(**common, adapter=VisionAdapter(transport, service.backends),
                argument_schema=VisionArguments, output_schema=VisionOutput, result_type='structured', mode=ResourceMode.IMAGE,
                allowed_backends=frozenset(dict(service.backends)), resource_profile='vision',
                enabled=bool(service.enabled and service.resource_profile == 'vision' and service.host_lifecycle_only
                    and 'vision' in managed_profiles and matched and local and service.backends),
                remote_job=True, remote_resource=False, requires_image_compilation=True))
        elif service.tool == "video.generate":
            definitions.append(ToolDefinition(**common, adapter=VideoAdapter(transport, artifacts),
                                               argument_schema=VideoArguments, output_schema=VideoOutput, result_type="run",
                                               mode=ResourceMode.WORK, allowed_roots=(artifacts.root,), enabled=False, min_artifacts=1,
                                               resource_profile=service.resource_profile, remote_job=True))
        elif service.tool == "tts.generate":
            backends = dict(service.backends)
            origin_matches = (owned_origins or {}).get(service.resource_profile) == service.api_base_url.rstrip("/")
            hostname = urlsplit(service.api_base_url).hostname
            try:
                local = hostname == "localhost" or ip_address(hostname).is_loopback
            except (ValueError, TypeError):
                local = False
            definitions.append(ToolDefinition(**common, adapter=IrodoriAdapter(transport, artifacts, backends, service.api_key_env),
                                               argument_schema=TTSArguments, output_schema=TTSOutput, result_type="audio",
                                               mode=ResourceMode.TTS, allowed_roots=(artifacts.root,), min_artifacts=1,
                                               allowed_backends=frozenset(backends), default_backend="irodori" if "irodori" in backends else None,
                                               enabled=bool(service.enabled and backends and local and origin_matches
                                                            and service.host_lifecycle_only and service.resource_profile in managed_profiles),
                                               resource_profile=service.resource_profile, remote_job=True, remote_resource=False))
    image = next((d for d in definitions if d.name == "image.generate"), None)
    if image and image.enabled:
        from backend.app.tools.comfy_video import ComfyVideoAdapter, load_video_workflows
        video_workflows = load_video_workflows()
        if video_workflows.backends:
            adapter = ComfyVideoAdapter(image.adapter.http, artifacts, video_workflows,
                on_demand=True, max_polls=480, poll_interval=1)
            definitions.append(replace(image, name="media.video.generate", adapter=adapter,
                result_type="run", allowed_backends=frozenset(video_workflows.backends)))
    if work_settings is not None:
        from backend.app.tools.pitch_work import work_definition
        definitions.append(work_definition(work_settings, artifact_root.parent / "work", managed_profiles, owned_origins or {}))
    return ToolRegistry(tuple(definitions))


def public_services(settings: tuple[ServiceSettings, ...], registry: ToolRegistry) -> dict:
    def configured(service):
        return bool(service.api_base_url and (service.tool != "image.generate" or service.image_protocol == "comfyui"))
    return {"tools": [{"id": service.tool, "configured": configured(service),
                       "enabled": bool(registry.get(service.tool) and registry.get(service.tool).enabled),
                       "backends": sorted(registry.get(service.tool).allowed_backends) if registry.get(service.tool) else [],
                       "reason": ("api_unverified" if not configured(service) else
                                  None if registry.get(service.tool) and registry.get(service.tool).enabled else
                                  "lifecycle_ownership_unverified" if service.tool != "book.search" else
                                  None if service.enabled else "disabled")}
                      for service in settings]}
