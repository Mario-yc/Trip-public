from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Optional

from src.services.campus_candidate_policy import CampusCandidatePolicy

from src.services.experience_search_profile_compiler import (
    CREATIVE_OPTIONAL_FAMILY_INTENTS,
)

_MUSEUM_STRONG_NAME = re.compile(r"(美术馆|艺术博物馆|博物馆|博物院|画院|当代艺术)", re.IGNORECASE)
_MUSEUM_WEAK_NAME = re.compile(r"(展览馆|展馆|艺术中心|文化艺术中心|艺术空间|文化艺术)", re.IGNORECASE)
_MUSEUM_PROVIDER = re.compile(
    r"(美术馆|博物馆|博物院|展览馆|文化艺术场馆|科教文化服务[^;；]*(?:博物馆|博物院|展览馆|文化艺术))",
    re.IGNORECASE,
)
_MUSEUM_PROVIDER_NEGATIVE = re.compile(
    r"(交通设施服务|通行设施|停车设施|停车场|地下车库|停车楼|停车库|出入口|入口|出口|(?:^|[;；])门(?:[;；]|$))",
    re.IGNORECASE,
)
_MUSEUM_NEGATIVE = re.compile(
    r"(风景名胜|公园|花海|溪谷|景区|田园|乐园|观景点|广场|商场|购物|餐饮|住宅|小区|学校|高等院校|停车场|出入口)",
    re.IGNORECASE,
)
_MUSEUM_SUBORDINATE_NAME = re.compile(
    r"(停车场|地下车库|停车楼|停车库|(?:东|南|西|北|正|侧|后|前)门|出入口|入口|出口)$",
    re.IGNORECASE,
)
_MUSEUM_ENTITY_GENERIC = re.compile(
    r"(艺术博物馆|文化艺术中心|文化艺术|当代艺术|美术馆|博物馆|博物院|展览馆|艺术中心|艺术空间|画院|展馆|大学|学院|校园|校区)",
    re.IGNORECASE,
)
_BROAD_ENTITY_MODIFIER = re.compile(r"(当地|附近|周边|任意|一家|一个|某个|体验|参观)", re.IGNORECASE)

_CAMPUS_NAME = re.compile(r"(大学|学院|校园|校区)", re.IGNORECASE)
_CAMPUS_PROVIDER = re.compile(r"(高等院校|科教文化服务[^;；]*学校|学校;高等院校)", re.IGNORECASE)
_CAMPUS_NEGATIVE = re.compile(
    r"(公交车站|公交站|地铁站|道路名|交通地名|住宅|小区|公司|停车场|风景名胜|公园|寺庙道观)",
    re.IGNORECASE,
)

_AREA_WALK_POSITIVE = re.compile(
    r"(历史街区|文化街区|特色街区|商业街区|街区|步行街|街巷|胡同|老街|古街|文化园区|创意园区|"
    r"艺术区|艺术街区|艺术园区|市集|市场|商圈|滨水步道|河岸步道|城市步道|社区|城市公园|滨水公园|公园)",
    re.IGNORECASE,
)
_AREA_WALK_PROVIDER = re.compile(
    r"(风景名胜[^;；]*(?:街区|步行街|胡同|老街|古街|文化园区|创意园区|市集|市场)|"
    r"购物服务[^;；]*(?:步行街|商业街|市场|商圈)|道路附属设施[^;；]*步行道)",
    re.IGNORECASE,
)
_AREA_WALK_NEGATIVE_PROVIDER = re.compile(
    r"(动物园|水族馆|海底世界|主题乐园|游乐园|植物园|森林公园|湿地公园|城市公园|"
    r"公园广场;动物园|公园广场;水族馆|公园广场;植物园|酒店|宾馆|度假村)",
    re.IGNORECASE,
)
_AREA_WALK_NON_EXPERIENCE_ENTITY = re.compile(
    r"(住宅|小区|居民区|公寓|房地产|公司|企业|写字楼|办公楼|停车场|停车库|地下车库|车库|出入口|入口|出口|酒店|宾馆|度假村)",
    re.IGNORECASE,
)
_PARK_RELAX_NAME = re.compile(
    r"(城市公园|滨水公园|森林公园|湿地公园|口袋公园|公园|园林|绿地)",
    re.IGNORECASE,
)
_PARK_RELAX_PROVIDER = re.compile(
    r"(公园广场|城市公园|森林公园|湿地公园|公园|园林|绿地)",
    re.IGNORECASE,
)
_STRICT_AREA_WALK_FAMILIES = {
    "heritage_walk",
    "local_life",
    "market_walk",
    "art_walk",
    "park_relax",
}
_AREA_WALK_FAMILY_SIGNALS: dict[str, re.Pattern[str]] = {
    "heritage_walk": re.compile(
        r"(历史文化街区|历史街区|文化街区|胡同|老街|古街|古城|古镇)",
        re.IGNORECASE,
    ),
    "local_life": re.compile(
        r"(社区|居民区|里弄|本地生活|生活街区|菜市场|农贸市场|社区市场|胡同)",
        re.IGNORECASE,
    ),
    "market_walk": re.compile(
        r"(菜市场|农贸市场|传统市场|市井市场|市场|市集|集市|夜市|商业街|步行街|商圈)",
        re.IGNORECASE,
    ),
    "art_walk": re.compile(
        r"(艺术区|艺术街区|艺术园区|创意园区|文化园区|艺术中心|艺术空间)",
        re.IGNORECASE,
    ),
}

_INTENT_SIGNALS: dict[str, re.Pattern[str]] = {
    "park": re.compile(r"(公园|园林|植物园|森林公园|湿地公园|绿地)", re.IGNORECASE),
    "night_view": re.compile(r"(夜景|夜游|观景台|观景平台|天际线|灯光|亮化|塔|CBD|滨水)", re.IGNORECASE),
    "campus_visit": re.compile(r"(大学|学院|高等院校|学校|校园|校区)", re.IGNORECASE),
    "local_culture": re.compile(r"(文化|民俗|非遗|胡同|历史街区|博物馆|纪念馆|文化馆)", re.IGNORECASE),
    "meal": re.compile(r"(餐饮|餐厅|饭店|小吃|美食|菜馆|咖啡|茶馆)", re.IGNORECASE),
    "landmark": re.compile(r"(地标|名胜|景点|纪念碑|广场|塔|城楼)", re.IGNORECASE),
}


@dataclass(frozen=True)
class IntentSemanticDecision:
    intent_type: str
    passed: bool
    confidence: float
    reason_code: str
    positive_signals: list[str] = field(default_factory=list)
    negative_signals: list[str] = field(default_factory=list)

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "intentType": self.intent_type,
            "passed": self.passed,
            "confidence": self.confidence,
            "reasonCode": self.reason_code,
            "positiveSignals": list(self.positive_signals),
            "negativeSignals": list(self.negative_signals),
        }


class IntentCandidateSemanticPolicy:
    """Validates that a real map candidate also satisfies the requested intent.

    Provider identity and coordinates prove that a place exists.  They do not
    prove that the place is a museum, campus, meal, or another requested
    experience.  This policy is shared by candidate admission and persisted
    snapshot verification so those two boundaries cannot drift.
    """

    def evaluate(
        self,
        intent_type: str,
        candidate: Any,
        *,
        raw_need: str = "",
        exact_entity: Optional[str] = None,
        optional_experience_family: str = "",
        qualification_binding: Mapping[str, Any] | None = None,
    ) -> IntentSemanticDecision:
        intent = str(intent_type or "").strip()
        family = str(optional_experience_family or "").strip().casefold()
        name = self._value(candidate, "name")
        provider_type = " ".join(self._value(candidate, key) for key in ("type", "category")).strip()
        address_text = self._value(candidate, "address").strip()
        context_text = " ".join(
            self._value(candidate, key) for key in ("address", "source_note", "sourceNote")
        ).strip()
        all_text = f"{name} {provider_type} {context_text}".strip()

        expected_entity = str(exact_entity or "").strip()
        if expected_entity and not self._same_entity(expected_entity, name):
            return IntentSemanticDecision(intent, False, 0.0, "exact_entity_mismatch", [], [name])

        if family and not expected_entity:
            allowed_intents = CREATIVE_OPTIONAL_FAMILY_INTENTS.get(family)
            if allowed_intents is None:
                return IntentSemanticDecision(
                    intent,
                    False,
                    0.0,
                    "creative_optional_family_unregistered",
                    [],
                    [family],
                )
            if intent not in allowed_intents:
                return IntentSemanticDecision(
                    intent,
                    False,
                    0.0,
                    "creative_optional_intent_unregistered",
                    [],
                    [intent],
                )

        if intent == "museum":
            return self._evaluate_museum(name, provider_type, all_text, raw_need=raw_need)
        if intent == "campus_visit":
            semantic = self._evaluate_campus(name, provider_type, all_text)
            if not semantic.passed:
                return semantic
            campus_reason = CampusCandidatePolicy().reject_reason(
                candidate,
                raw_need,
                qualification_binding=qualification_binding,
                expected_entity=expected_entity,
            )
            if campus_reason:
                return IntentSemanticDecision(
                    "campus_visit",
                    False,
                    0.0,
                    campus_reason,
                    list(semantic.positive_signals),
                    [name],
                )
            return semantic
        if intent in {"area_walk", "experience"} and family in _STRICT_AREA_WALK_FAMILIES:
            invalid_entities = list(
                dict.fromkeys(
                    match.group(0)
                    for match in _AREA_WALK_NON_EXPERIENCE_ENTITY.finditer(
                        f"{name} {provider_type}"
                    )
                )
            )
            if invalid_entities:
                return IntentSemanticDecision(
                    "area_walk",
                    False,
                    0.0,
                    "area_walk_non_experience_entity",
                    [],
                    invalid_entities,
                )
            if family == "park_relax":
                return self._evaluate_park_relax(name, provider_type)
            return self._evaluate_area_walk(
                name,
                provider_type,
                all_text,
                optional_experience_family=family,
                family_evidence_text=f"{name} {provider_type} {address_text}".strip(),
            )
        if family == "park_relax" and intent == "park":
            return self._evaluate_park_relax(
                name, provider_type, intent_type=intent
            )

        signal = _INTENT_SIGNALS.get(intent)
        if signal is None:
            return IntentSemanticDecision(intent, True, 0.5, "semantic_policy_not_required")
        matches = list(dict.fromkeys(match.group(0) for match in signal.finditer(all_text)))
        if matches:
            return IntentSemanticDecision(intent, True, 0.82, f"{intent}_semantic_match", matches)
        # Museum is the P0 hard gate.  Other intent families already have
        # dedicated production policies (campus, night view, meal trust and
        # route insertion).  Emit a diagnostic here without creating a second
        # conflicting hard reject until each family is migrated to this
        # registry with its fixtures.
        return IntentSemanticDecision(intent, True, 0.45, f"{intent}_semantic_policy_delegated")

    @staticmethod
    def _evaluate_area_walk(
        name: str,
        provider_type: str,
        all_text: str,
        *,
        optional_experience_family: str,
        family_evidence_text: str,
    ) -> IntentSemanticDecision:
        negative = list(dict.fromkeys(match.group(0) for match in _AREA_WALK_NEGATIVE_PROVIDER.finditer(all_text)))
        positive = list(dict.fromkeys(match.group(0) for match in _AREA_WALK_POSITIVE.finditer(name)))
        provider = list(dict.fromkeys(match.group(0) for match in _AREA_WALK_PROVIDER.finditer(provider_type)))
        if negative:
            return IntentSemanticDecision(
                "area_walk",
                False,
                0.0,
                "area_walk_provider_type_mismatch",
                positive + provider,
                negative,
            )
        if positive or provider:
            family_signal = _AREA_WALK_FAMILY_SIGNALS.get(optional_experience_family)
            family_matches = (
                list(
                    dict.fromkeys(
                        match.group(0)
                        for match in family_signal.finditer(family_evidence_text)
                    )
                )
                if family_signal is not None
                else []
            )
            if not family_matches:
                return IntentSemanticDecision(
                    "area_walk",
                    False,
                    0.0,
                    "area_walk_family_mismatch",
                    positive + provider,
                    [optional_experience_family],
                )
            return IntentSemanticDecision(
                "area_walk",
                True,
                0.9 if positive and provider else 0.82,
                "area_walk_structured_provider_match",
                positive + provider + family_matches,
                [],
            )
        return IntentSemanticDecision(
            "area_walk",
            False,
            0.0,
            "area_walk_semantic_evidence_missing",
            [],
            [provider_type[:160] or name[:160]],
        )

    @staticmethod
    def _evaluate_park_relax(
        name: str,
        provider_type: str,
        *,
        intent_type: str = "area_walk",
    ) -> IntentSemanticDecision:
        name_signals = list(
            dict.fromkeys(match.group(0) for match in _PARK_RELAX_NAME.finditer(name))
        )
        provider_signals = list(
            dict.fromkeys(
                match.group(0)
                for match in _PARK_RELAX_PROVIDER.finditer(provider_type)
            )
        )
        if name_signals and provider_signals:
            return IntentSemanticDecision(
                intent_type,
                True,
                0.9,
                "park_relax_structured_provider_match",
                name_signals + provider_signals,
                [],
            )
        return IntentSemanticDecision(
            intent_type,
            False,
            0.0,
            "park_relax_semantic_evidence_missing",
            name_signals + provider_signals,
            [provider_type[:160] or name[:160]],
        )

    def _evaluate_museum(
        self,
        name: str,
        provider_type: str,
        all_text: str,
        *,
        raw_need: str,
    ) -> IntentSemanticDecision:
        strong = list(dict.fromkeys(match.group(0) for match in _MUSEUM_STRONG_NAME.finditer(name)))
        provider = list(dict.fromkeys(match.group(0) for match in _MUSEUM_PROVIDER.finditer(provider_type)))
        provider_negative = list(
            dict.fromkeys(match.group(0) for match in _MUSEUM_PROVIDER_NEGATIVE.finditer(provider_type))
        )
        weak = list(dict.fromkeys(match.group(0) for match in _MUSEUM_WEAK_NAME.finditer(name)))
        negative = list(dict.fromkeys(match.group(0) for match in _MUSEUM_NEGATIVE.finditer(all_text)))
        name_negative = list(dict.fromkeys(match.group(0) for match in _MUSEUM_SUBORDINATE_NAME.finditer(name)))
        if strong and name_negative:
            return IntentSemanticDecision(
                "museum",
                False,
                0.0,
                "museum_subordinate_or_wrong_category",
                strong + weak + provider,
                name_negative,
            )
        if not self._museum_entity_compatible(raw_need, name):
            return IntentSemanticDecision(
                "museum",
                False,
                0.0,
                "museum_entity_mismatch",
                strong + weak + provider,
                [name],
            )
        if strong and (not provider or provider_negative):
            return IntentSemanticDecision(
                "museum",
                False,
                0.0,
                "museum_provider_type_mismatch",
                strong + provider,
                provider_negative or [provider_type[:160]],
            )
        if strong:
            return IntentSemanticDecision("museum", True, 0.96, "museum_strong_name_match", strong + provider, [])
        if weak and provider:
            return IntentSemanticDecision("museum", True, 0.86, "museum_name_and_provider_type_match", weak + provider, [])
        return IntentSemanticDecision("museum", False, 0.0, "museum_semantic_mismatch", weak + provider, [all_text[:160]])

    @staticmethod
    def _museum_entity_compatible(raw_need: str, candidate_name: str) -> bool:
        requested = str(raw_need or "").strip()
        if not requested or _BROAD_ENTITY_MODIFIER.search(requested):
            return True

        def normalize(value: str) -> str:
            return re.sub(
                r"[^0-9a-z\u4e00-\u9fff]",
                "",
                _MUSEUM_ENTITY_GENERIC.sub("", str(value or "")).casefold(),
            )
        requested_core = normalize(requested)
        if len(requested_core) < 2:
            return True
        candidate_core = normalize(candidate_name)
        return bool(candidate_core and (requested_core in candidate_core or candidate_core in requested_core))

    def _evaluate_campus(self, name: str, provider_type: str, all_text: str) -> IntentSemanticDecision:
        name_signals = list(dict.fromkeys(match.group(0) for match in _CAMPUS_NAME.finditer(name)))
        provider_signals = list(dict.fromkeys(match.group(0) for match in _CAMPUS_PROVIDER.finditer(provider_type)))
        negative = list(dict.fromkeys(match.group(0) for match in _CAMPUS_NEGATIVE.finditer(all_text)))
        if name_signals and provider_signals and not negative:
            return IntentSemanticDecision(
                "campus_visit",
                True,
                0.92,
                "campus_visit_name_and_provider_type_match",
                name_signals + provider_signals,
                [],
            )
        return IntentSemanticDecision(
            "campus_visit",
            False,
            0.0,
            "campus_visit_semantic_mismatch",
            name_signals + provider_signals,
            negative or [all_text[:160]],
        )

    @staticmethod
    def _value(candidate: Any, key: str) -> str:
        if isinstance(candidate, Mapping):
            return str(candidate.get(key) or "")
        return str(getattr(candidate, key, "") or "")

    @staticmethod
    def _same_entity(expected: str, actual: str) -> bool:
        normalize = lambda value: re.sub(r"[\s（()）·\-—_]", "", str(value or "")).casefold()
        left = normalize(expected)
        right = normalize(actual)
        return bool(left and right and (left == right or left in right or right in left))
