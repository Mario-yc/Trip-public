from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any

from src.services.spatial_geometry_service import SpatialGeometryService


class SpatialPreferenceService:
    """Compile natural-language spatial intent without inventing geography."""

    SCHEMA_VERSION = "spatial-preference-v1"
    _VAGUE_FOCUS = re.compile(
        r"(?P<source>(?:(?:在|位于|安排在|集中在)?[^，。；\n]{0,12})?"
        r"(?:市中心|中心城区|核心区)(?:附近|周边|一带|区域|内)?)"
    )
    _REQUIRED = re.compile(r"(?:必须|只在|仅在|不要超出|不能超出|限定在)")
    _PREFERRED = re.compile(r"(?:最好|尽量|优先|希望|尽可能)")

    @classmethod
    def from_request_text(cls, text: str) -> dict[str, Any] | None:
        match = cls._VAGUE_FOCUS.search(str(text or ""))
        if match is None:
            return None
        source_text = str(match.group("source") or "").strip()
        window_start = max(0, match.start() - 8)
        window = str(text or "")[window_start : match.end()]
        strength = "required" if cls._REQUIRED.search(window) else "preferred"
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "status": "unresolved",
            "strength": strength,
            "sourceText": source_text,
            "source": "user_request",
            "fingerprint": cls.fingerprint({"status": "unresolved", "strength": strength, "sourceText": source_text}),
        }

    @classmethod
    def valid_resolution_input(cls, value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        allowed = {
            "kind",
            "referenceText",
            "radiusMeters",
            "administrativeAreaText",
            "boundaryText",
            "containment",
            "mapSelectionFingerprint",
        }
        if not set(value).issubset(allowed):
            return False
        # Controller output must never mint Provider or coordinate identity.
        folded_keys = " ".join(str(key).casefold() for key in value)
        if any(marker in folded_keys for marker in ("amapid", "longitude", "latitude", "coordinate", "adcode")):
            return False
        kind = str(value.get("kind") or "")
        if kind == "reference_point":
            return set(value) == {"kind", "referenceText"} and cls._bounded_text(value.get("referenceText"))
        if kind == "reference_point_radius":
            return set(value) == {"kind", "referenceText", "radiusMeters"} and bool(
                cls._bounded_text(value.get("referenceText"))
                and isinstance(value.get("radiusMeters"), (int, float))
                and not isinstance(value.get("radiusMeters"), bool)
                and 100 <= float(value["radiusMeters"]) <= 50000
            )
        if kind == "administrative_area":
            return set(value) == {"kind", "administrativeAreaText"} and cls._bounded_text(
                value.get("administrativeAreaText")
            )
        if kind == "named_boundary":
            return bool(
                set(value) == {"kind", "boundaryText", "containment"}
                and cls._bounded_text(value.get("boundaryText"))
                and str(value.get("containment") or "") in {"inside", "outside"}
            )
        if kind == "map_selection":
            return set(value) == {"kind", "mapSelectionFingerprint"} and bool(
                re.fullmatch(r"[a-f0-9]{32,128}", str(value.get("mapSelectionFingerprint") or ""))
            )
        return False

    @classmethod
    def named_boundary_identity_text(cls, value: Any, containment: Any) -> str:
        """Remove only generic containment grammar from a boundary identity.

        Provider lookup needs the named feature itself, while ``inside`` or
        ``outside`` remains a separate typed field.  This is language grammar,
        not a city, road, landmark, or relation-id dictionary.
        """

        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        direction = str(containment or "").strip().casefold()
        suffixes = {
            "inside": ("范围以内", "范围之内", "以内", "之内", "范围内", "内", " within", " inside"),
            "outside": ("范围以外", "范围之外", "以外", "之外", "范围外", "外", " outside"),
        }.get(direction, ())
        folded = text.casefold()
        for suffix in suffixes:
            if folded.endswith(suffix.casefold()):
                candidate = text[: len(text) - len(suffix)].strip(" ，,。.;；:：")
                if candidate:
                    text = candidate
                    break
        prefix_pattern = r"^(?:在|位于)\s*" if re.search(r"[\u3400-\u9fff]", text) else r"^(?:within|inside|outside)\s+"
        candidate = re.sub(prefix_pattern, "", text, flags=re.IGNORECASE).strip()
        return candidate or text

    @staticmethod
    def is_executable_resolution(resolution: Any) -> bool:
        """Return whether Provider-grounded spatial data can constrain POI search.

        A syntactically valid answer is deliberately not equivalent to an
        executable range. Named boundaries require an accepted polygon and a
        reference point without a radius still requires a follow-up answer.
        """

        if not isinstance(resolution, dict):
            return False
        kind = str(resolution.get("kind") or "")
        if kind in {"reference_point_radius", "map_selection"}:
            place = resolution.get("referencePlace")
            return bool(
                isinstance(place, dict)
                and str(place.get("amapId") or "")
                and isinstance(resolution.get("radiusMeters"), (int, float))
                and float(resolution["radiusMeters"]) >= 100
            )
        if kind == "administrative_area":
            return bool(resolution.get("adcodes"))
        if kind == "named_boundary_polygon":
            polygon = resolution.get("polygonGcj02")
            if not isinstance(polygon, list) or not 4 <= len(polygon) <= 40:
                return False
            try:
                points = [(float(point[0]), float(point[1])) for point in polygon if len(point) == 2]
            except (TypeError, ValueError, KeyError, IndexError):
                return False
            return bool(
                str(resolution.get("boundaryEvidenceFingerprint") or "")
                and len(points) == len(polygon)
                and SpatialGeometryService.valid_closed_polygon(points)
                and str(resolution.get("containment") or "") in {"inside", "outside"}
            )
        return False

    @staticmethod
    def _bounded_text(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text and len(text) <= 80)

    @staticmethod
    def fingerprint(value: dict[str, Any]) -> str:
        canonical = json.dumps(copy.deepcopy(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
