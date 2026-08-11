"""Market research — the input side of the growth loop.

Every result carries provenance. When a source is not configured the
response is explicitly flagged as mock so the agent labels it UNKNOWN
rather than asserting it as fact. That flag is the anti-hallucination rule
enforced in code rather than left to the prompt.
"""

from __future__ import annotations

from typing import Any

from .base import Integration


class WebSearch(Integration):
    """Generic search provider (Brave/Serper/Google CSE shaped)."""

    def search(self, query: str, count: int = 5) -> dict[str, Any]:
        result = self.request(
            "GET",
            "/search",
            params={"q": query, "count": count},
            mock={"results": []},
        )
        return {
            "query": query,
            "verified": not result.get("_mocked", False),
            "results": result.get("results") or result.get("web", {}).get("results", []),
            "note": result.get("_reason", "live search results"),
        }


class ConstructionData(Integration):
    """Permits, tenders and project-award feeds.

    A signal is only useful once it names who controls the project and what
    Ashrah should do about it — that enrichment is the agent's job, but the
    shape below is what it has to fill in.
    """

    def permits(self, region: str, since_days: int = 30) -> dict[str, Any]:
        result = self.request(
            "GET",
            "/permits",
            params={"region": region, "since_days": since_days},
            mock={"permits": []},
        )
        return {
            "region": region,
            "verified": not result.get("_mocked", False),
            "permits": result.get("permits", []),
            "note": result.get("_reason", "live permit feed"),
        }

    def tenders(self, keywords: str = "painting", region: str = "") -> dict[str, Any]:
        result = self.request(
            "GET",
            "/tenders",
            params={"q": keywords, "region": region},
            mock={"tenders": []},
        )
        return {
            "keywords": keywords,
            "region": region,
            "verified": not result.get("_mocked", False),
            "tenders": result.get("tenders", []),
            "note": result.get("_reason", "live tender feed"),
        }

    def company_profile(self, company: str) -> dict[str, Any]:
        result = self.request(
            "GET",
            "/companies",
            params={"name": company},
            mock={"company": None},
        )
        return {
            "company": company,
            "verified": not result.get("_mocked", False),
            "profile": result.get("company"),
            "note": result.get("_reason", "live company record"),
        }
