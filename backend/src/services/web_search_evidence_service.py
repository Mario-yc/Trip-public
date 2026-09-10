"""Project web-search responses into bounded, attributable source references.

The search providers remain responsible for retrieval and ranking.  This
service only freezes the accepted snippets into opaque references that can be
carried through Agent traces without pretending Trip has fetched or stored the
full page body.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.providers.travel_tools import WebSearchResponse


class WebSearchEvidenceService:
    SCHEMA_VERSION = "trip-web-search-evidence-v1"

    @classmethod
    def project(cls, response: WebSearchResponse, *, freshness: str) -> dict[str, Any]:
        normalized_query = cls._normalize_text(response.query)
        query_fingerprint = cls._fingerprint(
            {
                "schemaVersion": cls.SCHEMA_VERSION,
                "query": normalized_query,
                "freshness": str(freshness or "noLimit"),
            }
        )
        results = [cls._source_ref(item) for item in response.results]
        status = (
            "search_results_ready"
            if results
            else "search_provider_failed"
            if response.failure_reason
            else "search_no_usable_results"
        )
        result_fingerprint = cls._fingerprint(
            {
                "schemaVersion": cls.SCHEMA_VERSION,
                "queryFingerprint": query_fingerprint,
                "sourceFingerprints": [item["sourceFingerprint"] for item in results],
                "providerName": response.provider_name,
                "queriedAt": response.queried_at.isoformat(),
                "fallbackUsed": bool(response.fallback_used),
                "failureReason": response.failure_reason,
                "status": status,
            }
        )
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "query": response.query,
            "queryFingerprint": query_fingerprint,
            "resultFingerprint": result_fingerprint,
            "status": status,
            "sourceRefIds": [item["refId"] for item in results],
            # Keep the existing `results` key for compatibility. Each result
            # is now also a self-contained SourceRef; there is no duplicated
            # full-page body or second evidence store in the model context.
            "results": results,
            "providerName": response.provider_name,
            "fallbackUsed": response.fallback_used,
            "failureReason": response.failure_reason,
            "userVisibleCaveat": response.user_visible_caveat,
            "confidence": response.confidence,
            "queriedAt": response.queried_at.isoformat(),
            "providerDiagnostics": response.provider_diagnostics,
            "attemptedProviders": response.attempted_providers,
            "successfulProviders": response.successful_providers,
            "failedProviders": response.failed_providers,
            "skippedProviders": response.skipped_providers,
            "acceptedSourceCount": len({item["sourceName"] for item in results if item["sourceName"]}),
        }

    @classmethod
    def _source_ref(cls, item: Any) -> dict[str, Any]:
        canonical_url = cls._canonical_url(str(item.url or ""))
        material = {
            "canonicalUrl": canonical_url,
            "title": cls._normalize_text(item.title),
            "snippet": cls._normalize_text(item.snippet),
            "sourceName": cls._normalize_text(item.source_name),
            "queriedAt": item.queried_at.isoformat(),
            "providerName": cls._normalize_text(item.provider_name),
            "confidence": float(item.confidence or 0.0),
            "authorityLevel": cls._normalize_text(item.credibility_rank) or "unknown",
            "publishedAt": item.published_at,
        }
        source_fingerprint = cls._fingerprint(material)
        return {
            "refId": f"webref_{source_fingerprint[:16]}",
            "sourceFingerprint": source_fingerprint,
            "sourceType": "web_search_snippet",
            "title": item.title,
            "url": canonical_url,
            "snippet": item.snippet,
            "sourceName": item.source_name,
            "queriedAt": item.queried_at.isoformat(),
            "confidence": item.confidence,
            "authorityLevel": item.credibility_rank,
            "providerName": item.provider_name,
            "fallbackUsed": item.fallback_used,
            "failureReason": item.failure_reason,
            "userVisibleCaveat": item.user_visible_caveat,
            "publishedAt": item.published_at,
        }

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _canonical_url(value: str) -> str:
        try:
            parsed = urlsplit(value.strip())
            scheme = parsed.scheme.casefold()
            hostname = (parsed.hostname or "").casefold()
            port = parsed.port
        except ValueError:
            return value.strip()
        if not parsed.scheme or not parsed.netloc:
            return value.strip()
        if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            hostname = f"{hostname}:{port}"
        path = parsed.path or "/"
        return urlunsplit((scheme, hostname, path, parsed.query, ""))

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
