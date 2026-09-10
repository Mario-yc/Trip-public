from __future__ import annotations

import re
from typing import Any, Mapping

from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService


CAMPUS_WEAK_RE = re.compile(
    r"(成人教育|老年大学|企业大学|银行大学|建行大学|培训|职业培训|继续教育|网络教育|开放大学|广播电视大学|"
    r"校友会|招生办|附属中学|附属小学|食堂|宿舍|办公室|服务中心)"
)
CAMPUS_SUBENTITY_RE = re.compile(
    r"(国际学院|材料学院|环境学院|继续教育学院|招生办|办公室|服务中心|体育部|食堂|宿舍|"
    r"教学楼|实验楼|办公楼|综合楼|为民楼|实验室)"
)
CAMPUS_REMOTE_BRANCH_RE = re.compile(r"(远郊|分校区|新校区|校区东区|校区西区|校区南区|校区北区)")
CAMPUS_REMOTE_USER_REQUEST_RE = re.compile(r"(远郊|分校区|新校区|东区|西区|南区|北区|校区)")
CAMPUS_STRONG_RE = re.compile(r"(大学|学院|高等院校|学校|校区|科教文化服务)")


class CampusCandidatePolicy:
    def canonical_hints(self, city: str, tier: str) -> list[str]:
        if str(tier or "").strip() != "985":
            return []
        return EntityQualificationEvidenceService.canonical_hints(
            locality=city,
            scheme="moe_project_classification",
            value="985",
        )

    def reject_reason(
        self,
        candidate: Any,
        raw_need: str = "",
        *,
        qualification_binding: Mapping[str, Any] | None = None,
        expected_entity: str = "",
    ) -> str:
        text = self._candidate_text(candidate)
        if CAMPUS_WEAK_RE.search(text):
            return "weak_campus_entity"
        if CAMPUS_SUBENTITY_RE.search(text) and not self._looks_like_named_college_subject(candidate):
            return "campus_affiliated_subentity"
        name = self._value(candidate, "name")
        if CAMPUS_REMOTE_BRANCH_RE.search(name) and not CAMPUS_REMOTE_USER_REQUEST_RE.search(str(raw_need or "")):
            return "campus_remote_branch"
        if not CAMPUS_STRONG_RE.search(text):
            return "campus_type_missing"
        bound_985 = bool(
            isinstance(qualification_binding, Mapping)
            and str(qualification_binding.get("qualificationScheme") or "") == "moe_project_classification"
            and str(qualification_binding.get("qualificationValue") or "") == "985"
        )
        if ("985" in str(raw_need or "") or bound_985) and not self.matches_tier(
            candidate,
            "985",
            qualification_binding=qualification_binding,
            expected_entity=expected_entity,
        ):
            return "campus_tier_985_mismatch"
        return ""

    def matches_tier(
        self,
        candidate: Any,
        tier: str,
        *,
        qualification_binding: Mapping[str, Any] | None = None,
        expected_entity: str = "",
    ) -> bool:
        if str(tier or "").strip() != "985":
            return True
        if isinstance(qualification_binding, Mapping):
            canonical_name = str(qualification_binding.get("canonicalName") or "").strip()
            binding_reason = EntityQualificationEvidenceService.validate_binding(
                qualification_binding,
                expected_canonical_name=expected_entity,
            )
            return bool(
                not binding_reason
                and str(qualification_binding.get("qualificationScheme") or "")
                == "moe_project_classification"
                and str(qualification_binding.get("qualificationValue") or "") == "985"
                and self._binding_locality_matches_candidate(qualification_binding, candidate)
                and self._binding_identity_matches_candidate(canonical_name, candidate)
            )
        return EntityQualificationEvidenceService.qualifies(
            candidate,
            scheme="moe_project_classification",
            value="985",
        )

    @staticmethod
    def _binding_identity_matches_candidate(canonical_name: str, candidate: Any) -> bool:
        canonical = EntityQualificationEvidenceService._normalize_entity(canonical_name)
        candidate_name = CampusCandidatePolicy._value(candidate, "parentInstitutionName") or CampusCandidatePolicy._value(
            candidate, "parent_institution_name"
        )
        if not candidate_name:
            candidate_name = CampusCandidatePolicy._value(candidate, "name")
        actual = EntityQualificationEvidenceService._normalize_entity(candidate_name)
        if not canonical or not actual:
            return False
        if actual == canonical:
            return True
        if not actual.startswith(canonical):
            return False
        suffix = actual[len(canonical) :]
        return bool(re.fullmatch(r"(?:.+校区|主校区|[东西南北]区)", suffix))

    @staticmethod
    def _binding_locality_matches_candidate(binding: Mapping[str, Any], candidate: Any) -> bool:
        evidence_locality = EntityQualificationEvidenceService._normalize_locality(binding.get("locality"))
        candidate_locality = EntityQualificationEvidenceService._normalize_locality(
            CampusCandidatePolicy._value(candidate, "city")
            or CampusCandidatePolicy._value(candidate, "locality")
            or CampusCandidatePolicy._value(candidate, "province")
        )
        candidate_address = EntityQualificationEvidenceService._normalize_locality(
            CampusCandidatePolicy._value(candidate, "address")
        )
        return bool(
            evidence_locality
            and (
                candidate_locality == evidence_locality
                or (not candidate_locality and evidence_locality in candidate_address)
            )
        )

    def _looks_like_named_college_subject(self, candidate: Any) -> bool:
        name = self._value(candidate, "name")
        return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,12}学院(?:\(.+\)|（.+）)?", name))

    def _candidate_text(self, candidate: Any) -> str:
        return " ".join(
            self._value(candidate, field_name)
            for field_name in ("name", "type", "category", "address", "district", "source_note")
        )

    @staticmethod
    def _value(candidate: Any, field_name: str) -> str:
        if isinstance(candidate, Mapping):
            return str(candidate.get(field_name) or "")
        return str(getattr(candidate, field_name, "") or "")
