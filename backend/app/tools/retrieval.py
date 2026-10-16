"""Shared bounded Evidence Tool interface; reference URLs are never auto-fetched."""

import re
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from backend.app.harness.contracts import Contract, ToolResult


def public_topic(value):
    # Defense in depth, NOT an anonymizer. External use also requires explicit
    # user confirmation and a separately supplied public topic, never case text.
    if (not isinstance(value, str) or not 2 <= len(value.strip()) <= 120
            or len(value.split()) > 15 or any(ord(c) < 32 for c in value)
            or re.search(r"@|https?://|\d{6,}|氏名|患者名|住所|電話|生年月日|credentials?|password|token|secret|api[_ -]?key|Bearer|sk-", value, re.I)):
        raise ValueError("Use a short, non-private public topic")
    return value.strip()


class EvidenceReference(Contract):
    source_type: Literal["book", "paper", "guideline", "web"]
    title: str = Field(min_length=1, max_length=1000)
    citation: str = Field(min_length=1, max_length=2000)
    locator: str = Field(min_length=1, max_length=2000)
    text: str = Field(min_length=1, max_length=800)
    reference: str = Field(min_length=1, max_length=4000)
    provenance: Literal["original_book_snippet", "original_abstract_snippet", "original_web_snippet"]

    @field_validator("reference")
    @classmethod
    def safe_reference(cls, value):
        parsed = urlsplit(value)
        if any(ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError("Invalid evidence reference")
        if value.startswith("/") and not value.startswith("//") and not parsed.netloc and not parsed.scheme:
            from backend.app.tools.book import BookReference
            return BookReference.same_service_reference(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.port not in {None, 443}):
            raise ValueError("Evidence URLs must be credential-free HTTPS references")
        return value

    @model_validator(mode="after")
    def original_kind(self):
        kinds = {"book": "original_book_snippet", "paper": "original_abstract_snippet",
                 "guideline": "original_web_snippet", "web": "original_web_snippet"}
        if self.provenance != kinds[self.source_type] or not self.text.strip():
            raise ValueError("Evidence must retain its original provenance")
        return self


class RetrievalOutput(Contract):
    query: str = Field(min_length=1, max_length=500)
    returned: int = Field(ge=0, le=3)
    references: list[EvidenceReference] = Field(max_length=3)

    @model_validator(mode="after")
    def count(self):
        if self.returned != len(self.references):
            raise ValueError("Evidence count mismatch")
        return self


class EvidenceTool(Protocol):
    async def execute(self, arguments: Contract, backend: str | None) -> ToolResult: ...


class PTOTArguments(Contract):
    query: str = Field(min_length=2, max_length=120)
    limit: int = Field(default=1, ge=1, le=3)
    public_confirmed: bool = False

    @field_validator("query")
    @classmethod
    def topic(cls, value):
        return public_topic(value)
