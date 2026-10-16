"""Existing PTOTsearch GET search only; no consult/worker/AI-summary endpoints."""

from backend.app.harness.contracts import ErrorCode, ToolResult
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.retrieval import EvidenceReference, RetrievalOutput


class PTOTSearchAdapter:
    def __init__(self, transport):
        self.http = transport

    async def execute(self, arguments, backend):
        status, raw = await self.http.json("GET", "/api/search", params={"q": arguments.query,
            "limit": arguments.limit, "offset": 0, "domain": "evidence", "client": "api"}, headers={"DNT": "1"})
        try:
            if (status != 200 or not isinstance(raw, dict) or raw["query"] != arguments.query
                    or type(raw["limit"]) is not int or raw["limit"] != arguments.limit
                    or type(raw["offset"]) is not int or raw["offset"] != 0
                    or type(raw["paper_total"]) is not int or not isinstance(raw["papers"], list)
                    or len(raw["papers"]) > arguments.limit or raw["paper_total"] < len(raw["papers"])
                    or raw.get("search_event_id") is not None or raw.get("journey_id") is not None):
                raise ValueError
            references = []
            for row in raw["papers"]:
                # Select only original English title/abstract. AI analysis and
                # Japanese localization are deliberately excluded, even if VALIDATED.
                title, text, url = row["title"], row["abstract_snippet"], row["source_url"]
                year, journal = row["publication_year"], row["journal"]
                if (not isinstance(title, str) or not isinstance(text, str) or len(text) > 4000
                        or type(year) is not int or not 1900 <= year <= 2100 or not isinstance(journal, str)):
                    raise ValueError
                if not text.strip():
                    continue  # A title alone is not evidence of a clinical claim.
                references.append(EvidenceReference(source_type="paper", title=title,
                    citation=f"{title}. {journal} ({year}).", locator=url, text=text[:800], reference=url,
                    provenance="original_abstract_snippet"))
            output = RetrievalOutput(query=arguments.query, returned=len(references), references=references)
        except (ValueError, TypeError, KeyError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        return ToolResult(success=True, result_type="structured", output=output.model_dump(mode="json"), http_status=status)
