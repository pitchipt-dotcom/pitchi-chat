"""Model output channels. Raw reasoning is transient, never a history/prompt payload.

The incremental parser withholds possible tag prefixes across chunk boundaries.
Consumers must treat stream output as provisional until finish validates it; no
streaming HTTP endpoint is currently exposed by HOST.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.models.base import ModelError


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    final_content: str
    reasoning_content: str | None = Field(default=None, repr=False, exclude=True)
    metadata: dict[str, str | int | bool | float | None] = Field(default_factory=dict)


TAGS = ("think", "reasoning", "analysis")
TOKEN = re.compile(r"</?(think|reasoning|analysis)\s*>", re.I)
PREFIX = re.compile(r"^</?(?:think|reasoning|analysis)?\s*$", re.I)


def invalid(reason="invalid_channel"):
    error = ModelError("モデルのfinal answerを安全に取得できませんでした。", classification="invalid_final")
    error.model_frame_error = reason
    raise error


class ReasoningParser:
    """feed returns (final_delta, thinking_delta); unknown/unclosed tags fail closed."""
    def __init__(self):
        self.pending = ""
        self.stack = []
        self.final = []
        self.reasoning = []
        self.formats = set()
        self.total_chars = 0

    def feed(self, text: str, channel: Literal["content", "reasoning", "final"] = "content"):
        if not isinstance(text, str) or channel not in {"content", "reasoning", "final"}:
            invalid()
        self.total_chars += len(text)
        if self.total_chars > 1000000:
            invalid("size_limit")
        if channel == "reasoning":
            self.formats.add("field")
            self.reasoning.append(text)
            return "", text
        if channel == "final":
            # A protocol's explicit final channel can terminate a thinking channel.
            self.reasoning.append(self.pending)
            self.pending = ""
            self.stack.clear()
        self.pending += text
        final, thoughts = [], []
        while self.pending:
            match = TOKEN.search(self.pending)
            if match:
                self._append(self.pending[:match.start()], final, thoughts)
                token, name = match.group(0), match.group(1).lower()
                if not self.stack and "".join(self.final).lstrip().startswith(("{", "[", "```")):
                    invalid("machine_embedding")  # Never silently rewrite machine fields.
                if token.startswith("</"):
                    if not self.stack or self.stack[-1] != name:
                        invalid("mismatched_close")
                    self.stack.pop()
                else:
                    self.stack.append(name)
                    self.formats.add(name)
                self.pending = self.pending[match.end():]
                continue
            # Hold an incomplete '<th' or '</reasoning' rather than leaking it.
            start = self.pending.rfind("<")
            suffix = self.pending[start:] if start >= 0 else ""
            suspect = re.match(r"</?(?:think|reasoning|analysis)\b", suffix, re.I)
            if suspect:
                if ">" in suffix:
                    invalid("malformed_tag")
                partial = True
            else:
                partial = False
            partial = partial or suffix and any(token.lower().startswith(suffix.lower()) for name in TAGS
                                     for token in (f"<{name}>", f"</{name}>"))
            partial = partial or bool(suffix and PREFIX.fullmatch(suffix))
            if partial:
                self._append(self.pending[:start], final, thoughts)
                self.pending = suffix
            else:
                self._append(self.pending, final, thoughts)
                self.pending = ""
            break
        return "".join(final), "".join(thoughts)

    def _append(self, text, final, thoughts):
        if self.stack:
            self.reasoning.append(text)
            thoughts.append(text)
        else:
            self.final.append(text)
            final.append(text)

    def finish(self):
        if self.stack:
            invalid("unclosed_reasoning")
        if self.pending:
            invalid("partial_tag")
        final = "".join(self.final)
        if not final.strip():
            invalid("empty_final")
        reasoning = "".join(self.reasoning) or None
        return ModelResponse(final_content=final.strip() if self.formats else final,
                             reasoning_content=reasoning, metadata={
            "reasoning_detected": bool(self.formats), "reasoning_chars": len(reasoning or ""),
            "reasoning_format": "+".join(sorted(self.formats)) or "none"})


def split_response(raw) -> ModelResponse:
    # Revalidate even a typed adapter response. Never trust model_construct.
    if isinstance(raw, ModelResponse):
        parsed = split_response({"content": raw.final_content, "reasoning_content": raw.reasoning_content})
        return parsed
    parser = ReasoningParser()
    if isinstance(raw, str):
        parser.feed(raw)
    elif isinstance(raw, dict):
        # Explicit, known backend fields only; never concatenate arbitrary response fields.
        for field in ("reasoning_content", "reasoning", "thinking", "analysis"):
            value = raw.get(field)
            if value is not None:
                parser.feed(value, "reasoning")
        content = raw.get("final_content", raw.get("final", raw.get("content")))
        if content is not None:
            parser.feed(content)
    else:
        invalid()
    return parser.finish()


def history_final(content: str) -> str:
    """Sanitize legacy contaminated assistant rows without rewriting user history."""
    try:
        return split_response(content).final_content
    except ModelError:
        return "[過去のモデル応答はfinal answerを確認できないため非表示です]"


def reject_reasoning_fields(value):
    """Machine contracts may not carry reasoning tags even as escaped JSON values."""
    if isinstance(value, str) and re.search(r"</?(?:think|reasoning|analysis)\b", value, re.I):
        raise ValueError("Reasoning is forbidden in machine/presentation fields")
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"reasoning_content", "thinking", "analysis_content"}:
                raise ValueError("Reasoning field is forbidden")
            reject_reasoning_fields(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            reject_reasoning_fields(child)


async def model_response(adapter, messages, schema=None) -> ModelResponse:
    if schema is None and hasattr(adapter, "generate_response"):
        raw = await adapter.generate_response(messages)
    elif schema is not None and hasattr(adapter, "generate_structured_response"):
        raw = await adapter.generate_structured_response(messages, schema)
    elif schema is not None and hasattr(adapter, "generate_structured"):
        raw = await adapter.generate_structured(messages, schema)
    else:
        raw = await adapter.generate(messages)
    return split_response(raw)
