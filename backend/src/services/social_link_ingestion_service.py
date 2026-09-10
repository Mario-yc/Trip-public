"""Persist bounded public XHS note text, without retaining share credentials."""

import hashlib
import json
import socket
import sqlite3
from typing import Callable, Optional

import httpx
from fastapi import HTTPException

from src.models.source_material import SourceMaterial
from src.services.public_source_reader import _ReadFailure, _public_ip, _validate_peer
from src.services.source_material_service import SourceMaterialService
from src.services.xiaohongshu_public_reader import XiaohongshuPublicReader


class _InjectedClientTransport(httpx.BaseTransport):
    """Compatibility test seam; shared reader peer checks remain mandatory."""

    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        return self.client.send(request, stream=True, follow_redirects=False)

    def close(self):
        # One injected client can serve several redirect hops; ingest owns it.
        pass


class SocialLinkIngestionService:
    ALLOWED_DOMAINS = ("xhslink.com", "xhslink.cn", "xiaohongshu.com")
    MAX_REDIRECTS = XiaohongshuPublicReader.MAX_REDIRECTS
    MAX_BYTES = XiaohongshuPublicReader.MAX_BYTES
    MAX_TEXT_CHARS = XiaohongshuPublicReader.MAX_TEXT_CHARS
    TOTAL_SECONDS = XiaohongshuPublicReader.TOTAL_SECONDS
    PARSER_VERSION = XiaohongshuPublicReader.PARSER_VERSION

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        client: Optional[httpx.Client] = None,
        resolver: Optional[Callable[[str, int], set[str]]] = None,
        reader=None,
    ):
        self.db = db
        self.client = client
        self.resolver = resolver or self._resolve_addresses
        self.reader = reader or XiaohongshuPublicReader(
            resolver=self.resolver,
            transport_factory=(lambda **_kwargs: _InjectedClientTransport(client)) if client is not None else None,
        )

    @staticmethod
    def _policy_error(reason):
        code = {
            "xhs_domain_not_allowed": "social_link_domain_not_allowed",
            "xhs_port_not_allowed": "social_link_port_not_allowed",
            "non_public_address": "social_link_private_target",
            "dns_failed": "social_link_dns_failed",
        }.get(reason, "social_link_invalid")
        return HTTPException(status_code=422, detail={"code": code, "message": "分享链接未通过公开来源安全校验。"})

    def ingest(self, url: str, *, deadline: Optional[float] = None) -> SourceMaterial:
        original_url = str(url or "").strip()
        try:
            normalized, _, _ = XiaohongshuPublicReader._url(original_url)
        except _ReadFailure as exc:
            raise self._policy_error(exc.reason) from None
        public_original_url = XiaohongshuPublicReader._public_url(normalized)
        try:
            result = self.reader.read(original_url, deadline=deadline)
        finally:
            if self.client is not None:
                self.client.close()
        if result.get("reason") in {
            "non_public_address",
            "dns_failed",
            "xhs_domain_not_allowed",
            "xhs_port_not_allowed",
        }:
            raise self._policy_error(result["reason"])
        succeeded = result.get("status") == "succeeded"
        extracted_text = result.get("bodyText") if succeeded else None
        fingerprint = hashlib.sha256(extracted_text.encode("utf-8")).hexdigest() if extracted_text else None
        canonical_url = result.get("canonicalUrl")
        if canonical_url:
            canonical_url = XiaohongshuPublicReader._public_url(canonical_url)
        material = SourceMaterial(
            id=self._material_id(canonical_url or public_original_url, fingerprint),
            kind="social_link",
            raw_text=extracted_text,
            link_url=public_original_url,
            original_retention="structured_only",
            cache_status="not_applicable",
            metadata={
                "originalUrl": public_original_url,
                "canonicalUrl": canonical_url,
                "fetchStatus": "succeeded" if succeeded else "needs_user_material",
                "failureReason": None if succeeded else result.get("reason"),
                "provider": "xiaohongshu_public_html",
                "contentFingerprint": fingerprint,
                "extractedAt": result.get("fetchedAt"),
                "parserVersion": self.PARSER_VERSION,
                "title": result.get("title"),
                "noteId": result.get("noteId"),
                "images": result.get("images") or [],
                "imageContentStatus": "not_read",
            },
        )
        try:
            SourceMaterialService(self.db)._insert(material)
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.execute(
                """UPDATE source_materials SET raw_text = ?, link_url = ?, original_retention = ?,
                    cache_status = ?, metadata_json = ?, created_at = ? WHERE id = ?""",
                (
                    material.raw_text,
                    material.link_url,
                    material.original_retention,
                    material.cache_status,
                    json.dumps(material.metadata, ensure_ascii=False, sort_keys=True),
                    material.created_at.isoformat(),
                    material.id,
                ),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM source_materials WHERE id = ?", (material.id,)).fetchone()
            if row is not None:
                return SourceMaterialService(self.db)._from_row(row)
            raise
        return material

    @classmethod
    def _validate_public_xhs_url(cls, url, *, resolver=None):
        """Compatibility helper; production DNS runs under the shared deadline."""
        try:
            _, host, port = XiaohongshuPublicReader._url(url)
            raw = (resolver or cls._resolve_addresses)(host, port)
            if not raw:
                raise _ReadFailure("blocked", "dns_failed")
            return {_public_ip(str(address)) for address in raw}
        except _ReadFailure as exc:
            raise cls._policy_error(exc.reason) from None
        except socket.gaierror:
            raise cls._policy_error("dns_failed") from None

    @staticmethod
    def _validate_connected_peer(response, resolved_addresses):
        try:
            _validate_peer(response.extensions.get("network_stream"), resolved_addresses)
        except _ReadFailure as exc:
            raise ValueError(exc.reason) from None

    @staticmethod
    def _resolve_addresses(hostname, port):
        return {item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}

    @staticmethod
    def _material_id(url, fingerprint):
        identity = hashlib.sha256(f"{url}\n{fingerprint or 'unavailable'}".encode("utf-8")).hexdigest()[:18]
        return f"mat_xhs_{identity}"
