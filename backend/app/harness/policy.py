"""Fail-closed HOST policy. Cloud providers and renderers are not yet connected."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from backend.app.harness.contracts import ErrorCode, FinalStatus, RenderRequest, ToolCall
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.registry import ToolDefinition


@dataclass(frozen=True)
class TurnPolicy:
    # Constructed by HOST, never parsed from LLM output or public chat requests.
    allowed_tools: frozenset[str] = frozenset()
    privacy: str = "unknown"
    output_format: str = "natural_language"


def denied():
    raise HarnessFailure(ErrorCode.POLICY_DENIED, FinalStatus.REJECTED)


def authorized_path(value: str | Path, roots: tuple[Path, ...]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        denied()
    path = path.resolve()
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        denied()
    return path


def validate_tool_policy(call: ToolCall, definition: ToolDefinition, policy: TurnPolicy,
                         arguments: dict) -> str | None:
    if not definition.enabled or call.tool not in policy.allowed_tools:
        denied()
    # The real Cloud Privacy Gate is a later increment. No early bypass.
    if definition.cloud or (policy.privacy != "public" and not definition.allow_private):
        denied()
    if definition.external_retrieval:
        from backend.app.tools.retrieval import public_topic
        try:
            if arguments.get("public_confirmed") is not True:
                denied()
            public_topic(arguments.get("query"))
        except ValueError:
            denied()
    backend = call.backend if call.backend is not None else definition.default_backend
    if backend is not None and backend not in definition.allowed_backends:
        denied()
    if definition.allowed_backends and backend is None:
        denied()

    def check(value, key=""):
        if isinstance(value, dict):
            for name, child in value.items():
                if name.lower() in {"endpoint", "command", "argv", "pid", "api_key", "secret",
                                    "credentials", "start_command", "stop_command"}:
                    denied()
                check(child, name.lower())
        elif isinstance(value, (list, tuple)):
            for child in value:
                check(child, key)
        elif value is not None:
            is_path = isinstance(value, Path) or key in {"path", "file", "directory"} or key.endswith(("_path", "_file", "_directory"))
            is_url = key == "url" or key.endswith("_url")
            if isinstance(value, str):
                is_path = is_path or value.startswith(("/", "../", "./", "~", "file:"))
                is_url = is_url or value.startswith(("http:", "https:", "ftp:"))
            if is_path:
                if not isinstance(value, (str, Path)):
                    denied()
                authorized_path(value, definition.allowed_roots)
            if is_url:
                if not isinstance(value, str):
                    denied()
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or value not in definition.allowed_urls:
                    denied()

    check(arguments)
    return backend


def present(request: RenderRequest) -> str:
    # Only direct presentation exists. Both OpenAI purposes remain unavailable.
    if request.renderer != "direct":
        denied()
    return request.content


def presentation_request(content: str, policy: TurnPolicy) -> RenderRequest:
    format_name = policy.output_format
    if format_name == "natural_language":
        try:
            json.loads(content)
            format_name = "json"
        except (ValueError, TypeError):
            if "```" in content:
                format_name = "code"
            elif re.search(r"(?m)^\s*[a-zA-Z_][\w-]*:\s", content):
                format_name = "yaml"
            elif re.search(r"(?m)^\s*(?:\$ |sudo |curl |bash |python[0-9]* |export )", content):
                format_name = "command"
    return RenderRequest(path="presentation" if format_name == "natural_language" else "machine",
                         format=format_name, content=content)
