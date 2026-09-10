"""Shared canonical physical identity rules for AMap parent/child records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class PoiPhysicalIdentityService:
    _AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")

    @staticmethod
    def _value(poi: Any, *names: str) -> Any:
        for name in names:
            value = poi.get(name) if isinstance(poi, Mapping) else getattr(poi, name, None)
            if value not in (None, ""):
                return value
        return ""

    @classmethod
    def normalized_amap_id(cls, poi: Any) -> str:
        """Return the exact AMap record identity in one comparison form."""

        return str(cls._value(poi, "amapId", "amap_id", "id") or "").strip().upper()

    @classmethod
    def canonical_amap_id(cls, poi: Any) -> str:
        amap_id = cls.normalized_amap_id(poi)
        parent_id = str(cls._value(poi, "parentPoiId", "parent_poi_id") or "").strip().upper()
        if parent_id and cls._AMAP_ID_RE.fullmatch(parent_id):
            return parent_id
        indoor_parent_id = str(
            cls._value(poi, "indoorParentPoiId", "indoor_parent_poi_id") or ""
        ).strip().upper()
        if indoor_parent_id and cls._AMAP_ID_RE.fullmatch(indoor_parent_id):
            return indoor_parent_id
        return amap_id

    physical_group_id = canonical_amap_id

    @classmethod
    def invalid_parent_id(cls, poi: Any) -> bool:
        parent_ids = (
            str(cls._value(poi, "parentPoiId", "parent_poi_id") or "").strip().upper(),
            str(cls._value(poi, "indoorParentPoiId", "indoor_parent_poi_id") or "").strip().upper(),
        )
        return any(value and not cls._AMAP_ID_RE.fullmatch(value) for value in parent_ids)
