from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from src.services.travel_guide_advice_service import GUIDE_RESTAURANT_NAME_SUFFIXES


class GuidePoiIdentityService:
    """Bind one source mention to a bounded, server-fetched AMap result set.

    City, address and taxonomy corroborate an explicit identity relation; they
    cannot manufacture an alias. Only MapPoiService's retained provider_aliases
    may supply aliases. Fingerprints detect changed material; they are
    not authentication or a substitute for the server's source/capability checks.
    """

    PROVIDER = "amap-place-search"
    MAX_CANDIDATES = 5
    _SUBENTITY = re.compile(r"分馆|附属|分校|校区|分店|支店|停车场|出入口|售票|服务中心|[东南西北正侧后前]门")

    @staticmethod
    def normalize_name(value: str) -> str:
        return "".join(char for char in unicodedata.normalize("NFKC", str(value or "")).casefold() if char.isalnum())

    @classmethod
    def name_method(cls, hint: dict[str, Any], name: str) -> str:
        mention_text = str(hint.get("mentionText") or "").strip()
        name = str(name or "").strip()
        mention = cls.normalize_name(mention_text)
        if not mention or not name or max(len(mention_text), len(name)) > 180:
            return ""
        if mention_text == name:
            return "provider_name_exact"
        if mention == cls.normalize_name(name):
            return "unicode_name_equivalent"
        # Retain the established complete restaurant-brand policy. Branches
        # still participate separately in result-set ambiguity detection.
        if hint.get("intentType") in {"meal", "food_experience"}:
            brand = re.sub(r"[（(][^（）()]{1,40}店[）)]$", "", name).strip()
            canonical_brand = cls.normalize_name(brand)
            if canonical_brand == mention or any(
                canonical_brand == mention + cls.normalize_name(suffix) for suffix in GUIDE_RESTAURANT_NAME_SUFFIXES
            ):
                return "provider_restaurant_brand"
            return ""
        return ""

    @staticmethod
    def _field(candidate: Any, field: str, alias: str = "") -> Any:
        if isinstance(candidate, dict):
            return candidate.get(alias or field, candidate.get(field))
        return getattr(candidate, field, None)

    @classmethod
    def candidate_material(cls, candidate: Any) -> dict[str, Any]:
        identity = cls._field(candidate, "amap_id", "amapId") or cls._field(candidate, "id")
        material = {"amapPoiId": str(identity or "").strip().upper()}
        for field, alias in (
            ("name", "name"),
            ("city", "city"),
            ("district", "district"),
            ("address", "address"),
            ("type", "type"),
            ("provider_type_code", "providerTypeCode"),
            ("source", "source"),
            ("parent_poi_id", "parentPoiId"),
            ("indoor_parent_poi_id", "indoorParentPoiId"),
            ("provider_query_receipt_fingerprint", "providerQueryReceiptFingerprint"),
        ):
            material[alias] = str(cls._field(candidate, field, alias) or "")
        for field in ("latitude", "longitude"):
            value = cls._field(candidate, field)
            material[field] = float(value) if value is not None else None
        aliases = cls._field(candidate, "provider_aliases", "providerAliases")
        material["providerAliases"] = (
            sorted({str(value).strip() for value in aliases if isinstance(value, str) and value.strip()})
            if isinstance(aliases, list)
            else []
        )
        return material

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @staticmethod
    def _source_binding(hint: dict[str, Any]) -> dict[str, Any]:
        return {
            "mentionText": str(hint.get("mentionText") or ""),
            "intentType": str(hint.get("intentType") or ""),
            "sourceRefIds": [str(value) for value in hint.get("sourceRefIds") or []],
            "sourceFingerprints": [str(value) for value in hint.get("sourceFingerprints") or []],
            "guideEvidenceFingerprint": str(hint.get("guideEvidenceFingerprint") or ""),
            **(
                {"sourceDocumentFingerprints": list(hint["sourceDocumentFingerprints"])}
                if isinstance(hint.get("sourceDocumentFingerprints"), list)
                else {}
            ),
            **({"sourceExcerpt": str(hint["sourceExcerpt"])} if "sourceExcerpt" in hint else {}),
        }

    @classmethod
    def candidate_method(cls, hint: dict[str, Any], candidate: Any, city: str) -> str:
        material = cls.candidate_material(candidate)
        if material["source"] != cls.PROVIDER or not re.fullmatch(r"B[0-9A-Z]{8,31}", material["amapPoiId"]):
            return ""
        if city and cls.normalize_name(material["city"]).removesuffix("市") != cls.normalize_name(city).removesuffix(
            "市"
        ):
            return ""
        method = cls.name_method(hint, material["name"])
        if not method and cls._matched_alias(hint, material):
            method = "provider_alias_exact"
        has_external_parent = any(
            str(material[key]).strip().upper() not in {"", material["amapPoiId"]}
            for key in ("parentPoiId", "indoorParentPoiId")
        )
        # A museum obligation refers to the museum entity, so a same-name
        # attached POI cannot replace it under a different match method.
        # Meal branches and explicitly named campuses retain their existing
        # downstream semantic/qualification gates; parentage alone does not
        # make a restaurant inside a mall ineligible.
        if method and hint.get("intentType") == "museum" and has_external_parent:
            return ""
        if method == "provider_alias_exact" and (
            (has_external_parent and hint.get("intentType") not in {"meal", "food_experience"})
            or cls._SUBENTITY.search(material["name"])
            or not material["address"].strip()
            or not material["type"].strip()
            or not re.fullmatch(r"[0-9a-f]{64}", material["providerQueryReceiptFingerprint"])
        ):
            return ""
        return method

    @classmethod
    def _matched_alias(cls, hint: dict[str, Any], material: dict[str, Any]) -> str:
        mention = cls.normalize_name(str(hint.get("mentionText") or ""))
        aliases = material["providerAliases"]
        if not mention or len(aliases) > 16:
            return ""
        return next((alias for alias in aliases if 1 <= len(alias) <= 256 and cls.normalize_name(alias) == mention), "")

    @classmethod
    def resolve(cls, *, hint: dict[str, Any], candidates: list[Any], city: str) -> dict[str, Any]:
        rows = []
        for candidate in candidates[: cls.MAX_CANDIDATES]:
            material = cls.candidate_material(candidate)
            # Count provider identity matches before city, semantic or novelty
            # filtering. Those later gates must not turn ambiguity into proof.
            rows.append((material, cls._digest(material), cls.candidate_method(hint, candidate, "")))
        candidate_fingerprints = sorted({fingerprint for _, fingerprint, _ in rows})
        set_fingerprint = cls._digest(candidate_fingerprints)
        matches: dict[str, dict[str, Any]] = {}
        conflicting_ids = {
            material["amapPoiId"]
            for material, fingerprint, _ in rows
            if any(
                other["amapPoiId"] == material["amapPoiId"] and other_fingerprint != fingerprint
                for other, other_fingerprint, _ in rows
            )
        }
        for material, fingerprint, method in rows:
            if not method:
                continue
            identity = material["amapPoiId"]
            proof = {
                "schemaVersion": "guide-poi-identity-v1",
                **cls._source_binding(hint),
                "amapPoiId": identity,
                "matchMethod": method,
                "sourceField": "alias" if method == "provider_alias_exact" else "name",
                "canonicalName": material["name"],
                "matchedAlias": cls._matched_alias(hint, material) if method == "provider_alias_exact" else None,
                "providerName": cls.PROVIDER,
                "providerQueryReceiptFingerprint": material["providerQueryReceiptFingerprint"],
                "candidateFingerprint": fingerprint,
                "candidateSetFingerprint": set_fingerprint,
                "candidateFingerprints": candidate_fingerprints,
            }
            matches[identity] = proof
        matched_ids = sorted(matches)
        ambiguous = len(matched_ids) > 1 or bool(set(matched_ids) & conflicting_ids)
        for proof in matches.values():
            proof["matchedCandidateIds"] = matched_ids
            proof["ambiguous"] = ambiguous
            proof["evidenceFingerprint"] = cls._digest(proof)
        return {
            "matches": matches,
            "matchedCandidateIds": matched_ids,
            "ambiguous": ambiguous,
            "candidateSetFingerprint": set_fingerprint,
        }

    @classmethod
    def validate_evidence(cls, *, hint: dict[str, Any], candidate: Any, evidence: Any) -> bool:
        if not isinstance(evidence, dict):
            return False
        try:
            material = cls.candidate_material(candidate)
            fingerprint = cls._digest(material)
            method = cls.candidate_method(hint, candidate, material["city"])
            candidate_fingerprints = evidence.get("candidateFingerprints")
            return bool(
                method
                and evidence.get("schemaVersion") == "guide-poi-identity-v1"
                and evidence.get("providerName") == cls.PROVIDER
                and evidence.get("sourceField") == ("alias" if method == "provider_alias_exact" else "name")
                and evidence.get("matchMethod") == method
                and evidence.get("canonicalName") == material["name"]
                and evidence.get("matchedAlias")
                == (cls._matched_alias(hint, material) if method == "provider_alias_exact" else None)
                and evidence.get("ambiguous") is False
                and evidence.get("amapPoiId") == material["amapPoiId"]
                and evidence.get("matchedCandidateIds") == [material["amapPoiId"]]
                and evidence.get("candidateFingerprint") == fingerprint
                and evidence.get("providerQueryReceiptFingerprint") == material["providerQueryReceiptFingerprint"]
                and isinstance(candidate_fingerprints, list)
                and 1 <= len(candidate_fingerprints) <= cls.MAX_CANDIDATES
                and fingerprint in candidate_fingerprints
                and evidence.get("candidateSetFingerprint") == cls._digest(candidate_fingerprints)
                and all(evidence.get(key) == value for key, value in cls._source_binding(hint).items())
                and evidence.get("evidenceFingerprint")
                == cls._digest({key: value for key, value in evidence.items() if key != "evidenceFingerprint"})
            )
        except (TypeError, ValueError, OverflowError):
            return False
