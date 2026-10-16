"""Read-only book search with citations, without copying raw OCR or server paths."""

from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import Field, ValidationError, field_validator

from backend.app.harness.contracts import Contract, ErrorCode, ToolResult
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.http import ServiceHTTP


class BookArguments(Contract):
    query: str = Field(min_length=1, max_length=500)
    book_id: str | None = Field(default=None, min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Search query must not be blank")
        return value


class BookReference(Contract):
    book_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1000)
    pdf_page: int = Field(ge=1)
    book_pdf_page: int = Field(ge=1)
    citation_id: str = Field(min_length=1, max_length=500)
    citation: str = Field(min_length=1, max_length=2000)
    snippet: str = Field(max_length=4000)
    source_ref: str = Field(max_length=2000)
    viewer_ref: str = Field(max_length=4000)

    @field_validator("source_ref", "viewer_ref")
    @classmethod
    def same_service_reference(cls, value):
        parsed = urlsplit(value)
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//")
                or "\\" in value or any(ord(char) < 32 for char in value)
                or ".." in decoded.split("/")
                or not (parsed.path.startswith("/pdf/") or parsed.path == "/viewer.html")):
            raise ValueError("Invalid citation reference")
        if parsed.path == "/viewer.html":
            query = parse_qs(parsed.query)
            file = query.get("file", [""])[0]
            if (set(query) != {"file", "page"} or any(len(values) != 1 for values in query.values())
                    or not file.startswith("/pdf/") or ".." in unquote(file).split("/") or "\\" in file
                    or urlsplit(file).netloc or not query["page"][0].isdigit()):
                raise ValueError("Invalid viewer reference")
        elif parsed.query or not parsed.fragment.startswith("page=") or not parsed.fragment[5:].isdigit():
            raise ValueError("Invalid PDF reference")
        return value


class BookOutput(Contract):
    count: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=20)
    returned: int = Field(ge=0, le=20)
    has_more: bool
    references: list[BookReference] = Field(max_length=20)


class BookAdapter:
    def __init__(self, transport: ServiceHTTP):
        self.http = transport

    async def execute(self, arguments: BookArguments, backend: str | None) -> ToolResult:
        params = {"q": arguments.query, "offset": arguments.offset, "limit": arguments.limit}
        if arguments.book_id is not None:
            params["book_id"] = arguments.book_id
        status, raw = await self.http.json("GET", "/search", params=params)
        if status != 200:
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        try:
            references = [BookReference.model_validate({
                "book_id": row["book_id"], "title": row["book_title"], "pdf_page": row["pdf_page"],
                "book_pdf_page": row["book_pdf_page"], "citation_id": row["citation_id"],
                "citation": row["citation"], "snippet": row["snippet"],
                "source_ref": row["source_url"], "viewer_ref": row["viewer_url"],
            }) for row in raw["results"]]
            output = BookOutput.model_validate({key: raw[key] for key in ("count", "offset", "limit", "returned", "has_more")}
                                               | {"references": references})
            if (raw["query"] != arguments.query or output.offset != arguments.offset or output.limit != arguments.limit
                    or output.returned != len(references) or output.returned > arguments.limit
                    or output.count < output.offset + output.returned and output.returned > 0
                    or output.has_more != (output.offset + output.returned < output.count)
                    or (arguments.book_id and any(ref.book_id != arguments.book_id for ref in references))):
                raise ValueError("Inconsistent search result")
        except (KeyError, TypeError, ValueError, ValidationError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        return ToolResult(success=True, result_type="structured", output=output.model_dump(mode="json"), http_status=status)
