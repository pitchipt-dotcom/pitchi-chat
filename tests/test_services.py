"""Service unit/mock tests. Live service integration is an explicit separate script."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from backend.app.harness.audit import AuditTrail
from backend.app.harness.contracts import ErrorCode, FinalStatus, ToolCall, ToolResult
from backend.app.harness.orchestrator import Harness
from backend.app.harness.policy import TurnPolicy
from backend.app.harness.recovery import HarnessFailure
from backend.app.resources.gpu import ResourceCoordinator
from backend.app.storage.sqlite import HistoryStore
from backend.app.tools.book import BookArguments
from backend.app.tools.config import ServiceSettings, load_services, validate_location
from backend.app.tools.generation import VideoArguments
from backend.app.tools.services import public_services, service_registry
from backend.app.harness.validator import validate_result


def book_response():
    return {"query": "test", "count": 1, "offset": 0, "limit": 5, "returned": 1, "has_more": False,
            "results": [{"book_id": "book-1", "book_title": "Synthetic book", "pdf_page": 3, "book_pdf_page": 13,
                         "citation_id": "book-1:3", "citation": "Synthetic book, PDF p.3", "snippet": "synthetic evidence",
                         "source_url": "/pdf/example.pdf#page=3", "viewer_url": "/viewer.html?file=%2Fpdf%2Fexample.pdf&page=3",
                         "ocr_text": "RAW OCR MUST NOT COPY", "source_pdf": "/private/source/example.pdf"}]}


def make_harness(tmp_path, client, service, *, mock_gpu=False, polling=False):
    registry = service_registry((service,), client, tmp_path / "artifacts")
    definition = registry.get(service.tool)
    if mock_gpu:
        definition = replace(definition, enabled=True, requires_managed_gpu=False)
        if service.tool == "image.generate":
            # Retain the old protocol solely as isolated migration coverage.
            # Production service_registry never registers this adapter.
            from backend.app.tools.generation import ArtifactStore, ImageAdapter, ImageArguments
            from backend.app.tools.http import ServiceHTTP
            definition = replace(definition, adapter=ImageAdapter(ServiceHTTP(client, service.api_base_url),
                ArtifactStore(tmp_path / "artifacts"), dict(service.backends)), argument_schema=ImageArguments,
                allowed_backends=frozenset(dict(service.backends)), requires_image_compilation=False)
        from backend.app.tools.registry import ToolRegistry
        registry = ToolRegistry((definition,))
    if polling:
        definition.adapter.poll_interval = 0
        definition.adapter.max_polls = 2
    store = HistoryStore(tmp_path / "audit.db")
    store.initialize()
    return Harness(None, ResourceCoordinator(), AuditTrail(store), registry=registry,
                   policy=TurnPolicy(allowed_tools=frozenset({service.tool})))


def test_book_result_citations_and_private_audit(tmp_path):
    def handle(request):
        assert request.method == "GET" and request.url.path == "/search"
        assert request.url.params["q"] == "test"
        return httpx.Response(200, json=book_response())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            result = await harness.run_tool(ToolCall(tool="book.search", arguments={"query": "test"}))
            assert result.status == FinalStatus.SUCCESS
            ref = result.tool_result.output["references"][0]
            assert ref["title"] == "Synthetic book" and ref["pdf_page"] == 3 and ref["book_pdf_page"] == 13
            assert "RAW OCR" not in result.model_dump_json() and "private/source" not in result.model_dump_json()
            with harness.audit.store.connection() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM harness_audit")]
            assert "synthetic evidence" not in json.dumps(rows) and "Synthetic book" not in json.dumps(rows)
            assert rows[-1]["event"] == "final"
            assert any(row["event"] == "result_validated" for row in rows)
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"count": "1"}, {"returned": 0}, {"has_more": True}, {"query": "other"}, {"results": []},
    {"limit": 6}, {"offset": 1}, {"results": [{}]},
])
def test_bad_search_response_is_not_success(tmp_path, change):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=book_response() | change))) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            with pytest.raises(HarnessFailure) as error:
                await harness.run_tool(ToolCall(tool="book.search", arguments={"query": "test"}))
            assert error.value.code == ErrorCode.RESULT_INVALID
    asyncio.run(run())


@pytest.mark.parametrize("source", ["https://external.test/private", "//external.test/pdf/a", "/pdf/../secret", "/pdf/%2e%2e/secret", "/unknown"])
def test_citation_references_cannot_escape(tmp_path, source):
    raw = book_response()
    raw["results"][0]["source_url"] = source
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            with pytest.raises(HarnessFailure):
                await harness.run_tool(ToolCall(tool="book.search", arguments={"query": "test"}))
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [{"query": " "}, {"query": "test", "limit": 21}, {"query": "test", "endpoint": "http://unsafe.test"}, {"query": "test", "offset": -1}])
def test_tool_argument_validation_before_network(tmp_path, arguments):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("network must not run"))) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            with pytest.raises(HarnessFailure):
                await harness.run_tool(ToolCall(tool="book.search", arguments=arguments))
    asyncio.run(run())


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://external.test"}), httpx.Response(500, text="secret"),
                                        httpx.Response(200, text="HTML UI"), httpx.Response(200, headers={"content-type": "application/json"}, content=b"invalid")])
def test_http_failures_are_safe(tmp_path, response):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            with pytest.raises(HarnessFailure) as error:
                await harness.run_tool(ToolCall(tool="book.search", arguments={"query": "test"}))
            assert "secret" not in str(error.value) and "external.test" not in str(error.value)
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://public.test/", "http://host.ts.net/", "http://user:pass@localhost/", "http://localhost/?secret=x", "http://localhost/path", "file:///tmp/x", "http://0.0.0.0/"])
def test_service_location_policy(url):
    with pytest.raises(ValueError):
        validate_location(url)


def test_local_config_and_ui_is_not_api(tmp_path):
    path = tmp_path / "services.local.toml"
    path.write_text('[services."book.search"]\nui_url="http://localhost:1234/"\n')
    settings = load_services(path)
    async def run():
        async with httpx.AsyncClient() as client:
            registry = service_registry(settings, client, tmp_path / "artifacts")
            assert registry.get("book.search") is None
            assert all(not item["enabled"] for item in public_services(settings, registry)["tools"])
    asyncio.run(run())


@pytest.mark.parametrize("entry", ['[services."image.generate"]\nenabled=true\napi_base_url="http://localhost/"',
                                  '[services."book.search"]\nenabled=true', '[services."book.search"]\napi_key="synthetic"',
                                  '[services."book.search"]\ntimeout_seconds=nan', '[services.unknown]\nenabled=false'])
def test_invalid_service_config_fails_closed(tmp_path, entry):
    path = tmp_path / "services.local.toml"
    path.write_text(entry)
    with pytest.raises(ValueError):
        load_services(path)


def test_image_adapter_mock_submission_poll_and_artifact(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["model"] == "configured-wire-model"
            assert payload["prompt"] == "synthetic prompt"
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if request.url.path.endswith("images/0"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG\r\n\x1a\n" + b"mock-media" * 4)
        return httpx.Response(200, json={"id": "job-1", "status": "completed", "images": [{"index": 0}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            service = ServiceSettings("image.generate", api_base_url="http://localhost", backends=(("qwen-image", "configured-wire-model"),))
            harness = make_harness(tmp_path, client, service, mock_gpu=True, polling=True)
            result = await harness.run_tool(ToolCall(tool="image.generate", backend="qwen-image", arguments={"prompt": "synthetic prompt"}))
            assert result.tool_result.result_type == "image"
            assert Path(result.tool_result.artifacts[0].path).stat().st_size > 0
            assert calls == ["/api/generations", "/api/generations/job-1", "/api/generations/job-1/images/0"]
    asyncio.run(run())


def test_real_gpu_generation_is_blocked_before_http(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("must not submit"))) as client:
            service = ServiceSettings("image.generate", api_base_url="http://localhost", enabled=True, backends=(("fast-h2", "wire-model"),))
            harness = make_harness(tmp_path, client, service)
            with pytest.raises(HarnessFailure) as error:
                await harness.run_tool(ToolCall(tool="image.generate", backend="fast-h2", arguments={"prompt": "test"}))
            assert error.value.code == ErrorCode.POLICY_DENIED
    asyncio.run(run())


def test_video_adapter_multipart_polling_and_final_artifact(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            assert "multipart/form-data" in request.headers["content-type"]
            assert request.headers["x-requested-with"] == "manual-video-ui"
            assert b'library_asset_ids' in request.content
            return httpx.Response(200, json={"ok": True, "job_id": "run-1"})
        if request.url.path == "/media/result.mp4":
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\x00\x00\x00\x18ftypisom" + b"mock-video" * 4)
        return httpx.Response(200, json={"ok": True, "items": [{"job_id": "run-1", "status": "video_ready", "video_url": "/media/result.mp4"}], "next_cursor": None})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("video.generate", api_base_url="http://localhost"), mock_gpu=True, polling=True)
            result = await harness.run_tool(ToolCall(tool="video.generate", arguments={"title": "test", "speech_text": "test", "subtitle_text": "test", "library_asset_ids": ["asset-1"]}))
            assert result.tool_result.run_id == "run-1" and result.tool_result.run_status == "completed"
            assert len(result.tool_result.artifacts) == 1
            assert calls == ["POST", "GET", "GET"]
    asyncio.run(run())


def test_tool_api_discovery_and_denial_without_model(settings):
    from backend.app.main import create_app
    async def run():
        services = (ServiceSettings("book.search", ui_url="http://localhost:1234"),)
        app = create_app(settings, services=services)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://host.test") as client:
                response = await client.get("/api/tools")
                assert response.status_code == 200
                assert "localhost" not in response.text
                assert (await client.post("/api/tools/run", json={"tool": "book.search", "arguments": {"query": "test"}})).status_code == 403
                assert (await client.post("/api/tools/run", json={"tool": "book.search", "arguments": {}, "endpoint": "http://unsafe.test"})).status_code == 422
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_images", "bad_mime", "never_finishes", "wrong_id"])
def test_image_failure_is_not_success(tmp_path, failure):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job-1"})
        if request.url.path.endswith("images/0"):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"fake success")
        return httpx.Response(200, json={"id": "other-job" if failure == "wrong_id" else "job-1",
            "status": "running" if failure == "never_finishes" else "completed",
            "images": [] if failure == "missing_images" else [{}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            service = ServiceSettings("image.generate", api_base_url="http://localhost", backends=(("qwen-image", "wire-name"),))
            harness = make_harness(tmp_path, client, service, mock_gpu=True, polling=True)
            with pytest.raises(HarnessFailure) as error:
                await harness.run_tool(ToolCall(tool="image.generate", backend="qwen-image", arguments={"prompt": "test"}))
            assert error.value.code == (ErrorCode.TIMEOUT if failure == "never_finishes" else ErrorCode.RESULT_INVALID)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["external_url", "empty_file", "failed", "stalled", "running", "missing_job"])
def test_video_failure_and_bounded_poll(tmp_path, failure):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "job_id": "run-1"})
        if request.url.path == "/media/output.mp4":
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"")
        item = {"job_id": "run-1", "status": "video_ready", "video_url": "/media/output.mp4"}
        if failure == "external_url": item["video_url"] = "https://external.test/private"
        if failure in {"failed", "running"}: item["status"] = failure
        if failure == "stalled": item |= {"status": "running", "is_stalled": True}
        return httpx.Response(200, json={"ok": True, "items": [] if failure == "missing_job" else [item], "next_cursor": None})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("video.generate", api_base_url="http://localhost"), mock_gpu=True, polling=True)
            with pytest.raises(HarnessFailure):
                await harness.run_tool(ToolCall(tool="video.generate", arguments={"title": "test", "speech_text": "test", "subtitle_text": "test", "library_asset_ids": ["asset-1"]}))
            assert len(calls) <= 3
    asyncio.run(run())


def test_video_success_without_artifact_is_rejected(tmp_path):
    async def run():
        async with httpx.AsyncClient() as client:
            registry = service_registry((ServiceSettings("video.generate", api_base_url="http://localhost"),), client, tmp_path)
            result = ToolResult(success=True, result_type="run", output={"run_id": "run-1", "status": "completed", "artifact_count": 1},
                                http_status=200, run_id="run-1", run_status="completed")
            with pytest.raises(HarnessFailure) as error:
                validate_result(result, registry.get("video.generate"))
            assert error.value.code == ErrorCode.RESULT_INVALID
    asyncio.run(run())


def test_http_timeout_stalled_and_size_limit(tmp_path):
    from backend.app.tools.http import ServiceHTTP
    def fail(request):
        raise httpx.ReadTimeout("synthetic private detail", request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            harness = make_harness(tmp_path, client, ServiceSettings("book.search", api_base_url="http://localhost", enabled=True))
            with pytest.raises(HarnessFailure) as error:
                await harness.run_tool(ToolCall(tool="book.search", arguments={"query": "test"}))
            assert error.value.status == FinalStatus.STALLED and "private detail" not in str(error.value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"oversize"))) as client:
            with pytest.raises(HarnessFailure):
                await ServiceHTTP(client, "http://localhost").request("GET", "/test", max_bytes=2)
    asyncio.run(run())
