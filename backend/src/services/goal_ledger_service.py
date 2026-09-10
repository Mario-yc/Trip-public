from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Iterable, Optional


_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class GoalCardinality:
    intent_type: str
    min_count: int
    preferred_count: int
    max_count: Optional[int]
    source: str = "explicit_user_request"
    distribution_policy: str = "spread_across_distinct_days"
    allowed_day_numbers: tuple[int, ...] = ()
    requested_count: Optional[int] = None
    limitation_reason: Optional[str] = None


@dataclass(frozen=True)
class GoalLedger:
    goals: tuple[GoalCardinality, ...]
    required_planning_day_numbers: tuple[int, ...] = ()
    explicit_rest_day_numbers: tuple[int, ...] = ()

    def goal(self, intent_type: str) -> GoalCardinality:
        for goal in self.goals:
            if goal.intent_type == intent_type:
                return goal
        return GoalCardinality(intent_type, 0, 0, 0, source="not_requested")


class GoalLedgerService:
    """Extracts the pre-search per-day occurrence contract from user intent."""

    _DAILY_CLAUSE_RE = re.compile(
        r"(?:每天|每日|每一天|两天都)(?P<body>[^。！？!?；;\n]{0,160})"
    )
    _ORDERED_DAILY_LIST_RE = re.compile(
        r"(?:严格\s*)?(?:依次\s*)?(?:按照|按)\s*[“\"']?"
        r"(?P<payload>[^”\"'。！？!?；;\n]{1,128}?)[”\"']?\s*"
        r"(?:的\s*)?(?:先后\s*)?顺序\s*(?:安排|规划|游览|走访|体验)"
    )
    _ORDERED_DAILY_INTENT_PATTERNS = {
        "campus_visit": re.compile(r"(?:985|211|高校|大学|校园)"),
        "meal": re.compile(r"(?:早餐|早饭|午餐|午饭|中饭|晚餐|晚饭|餐厅|饭店|美食|菜系|[\u4e00-\u9fff]{2,6}菜)"),
        "park": re.compile(r"公园"),
    }
    _COMPLETE_ITINERARY_RE = re.compile(
        r"(?:[一二两三四五六七八九十\d]+\s*(?:天|日)(?:游|旅行|行程)|"
        r"旅行|旅游|游玩|行程|规划|安排)"
    )
    _INTENT_PATTERNS = {
        "campus_visit": re.compile(r"(?:985|211|高校|大学|校园)"),
        "meal": re.compile(
            r"(?:早餐|早饭|午餐|午饭|中饭|晚餐|晚饭|餐厅|饭店|美食|菜系|"
            r"中午.{0,10}(?:品尝|尝试|吃))"
        ),
        "park": re.compile(r"公园"),
    }
    _MULTI_DAY_TEMPLATE_PATTERNS = {
        # An unqualified campus visit in a complete multi-day trip is the trip
        # theme, so it applies to every required planning day.
        "campus_visit": re.compile(r"(?:985|211|高校|大学|校园)"),
        # Meal and park mentions become daily templates only when the user
        # gives them a daypart.  A broad request such as ``路途中品尝当地美食``
        # remains one soft experience unless it is explicitly quantified.
        "meal": re.compile(
            r"(?:早餐|早饭|午餐|午饭|中饭|晚餐|晚饭|"
            r"(?:早上|上午|中午|下午|傍晚|晚上|夜间)[^。！？!?；;\n]{0,12}"
            r"(?:品尝|尝试|吃|用餐|就餐|美食|餐厅|饭店))"
        ),
        "park": re.compile(
            r"(?:(?:早上|上午|中午|下午|傍晚|晚上|夜间)[^。！？!?；;\n]{0,12}公园|"
            r"公园[^。！？!?；;\n]{0,12}(?:早上|上午|中午|下午|傍晚|晚上|夜间))"
        ),
    }
    _DAY_NUMBER_PATTERN = r"[一二两三四五六七八九十\d]+"
    _DAY_CLAUSE_RE = re.compile(
        rf"(?:第\s*(?P<zh>{_DAY_NUMBER_PATTERN})\s*天|Day\s*(?P<arabic>[1-9]\d*))"
        rf"(?P<body>.*?)(?=(?:第\s*{_DAY_NUMBER_PATTERN}\s*天|Day\s*[1-9]\d*)|[。！？!?；;\n]|$)",
        re.IGNORECASE,
    )

    @classmethod
    def explicit_every_day_ordered_intents(cls, text: str) -> frozenset[str]:
        """Return intents inside one explicit, sentence-bounded daily order.

        A request such as ``每天严格按“高校→午餐→公园”的顺序安排``
        quantifies every listed stop.  Keeping the scan inside the same clause
        prevents an unrelated ``每天 09:00-19:00`` sentence from multiplying a
        later one-day meal or park request.
        """

        matched_intents: set[str] = set()
        for match in cls._DAILY_CLAUSE_RE.finditer(str(text or "")):
            scope = str(match.group(0) or "")
            for ordered_match in cls._ORDERED_DAILY_LIST_RE.finditer(scope):
                payload = str(ordered_match.group("payload") or "")
                for intent_type, pattern in cls._ORDERED_DAILY_INTENT_PATTERNS.items():
                    if pattern.search(payload):
                        matched_intents.add(intent_type)
        return frozenset(matched_intents)

    @classmethod
    def authoritative_daily_template_intents(
        cls,
        text: str,
        *,
        day_count: int,
    ) -> frozenset[str]:
        """Return intents that authoritatively repeat on every planning day.

        Explicit ``每天`` language remains the strongest signal.  For a complete
        multi-day itinerary, an unqualified trip theme and daypart clauses are
        also daily templates unless the same intent is explicitly narrowed to
        one occurrence or one day.  This is deliberately limited to campus,
        meal and park; it does not turn every mentioned POI into daily demand.
        """

        value = str(text or "")
        bounded_day_count = max(1, int(day_count or 1))
        intents = set(cls.explicit_every_day_ordered_intents(value))
        for intent_type, pattern in cls._INTENT_PATTERNS.items():
            if cls._explicit_every_day_intent(value, intent_type=intent_type):
                intents.add(intent_type)
        # A same-request single-occurrence/day constraint is authoritative and
        # narrows even an earlier daily phrase.  Remove it before the early
        # return as well as before implicit multi-day expansion so callers do
        # not observe contradictory template and cardinality contracts.
        intents = {
            intent_type
            for intent_type in intents
            if not cls._intent_has_single_scope(value, intent_type=intent_type)
        }
        if bounded_day_count <= 1 or cls._COMPLETE_ITINERARY_RE.search(value) is None:
            return frozenset(intents)
        for intent_type, pattern in cls._MULTI_DAY_TEMPLATE_PATTERNS.items():
            explicit_days = cls._explicit_intent_day_numbers(
                value,
                intent_type=intent_type,
                day_count=bounded_day_count,
            )
            if (
                pattern.search(value)
                and not explicit_days
                and not cls._intent_has_single_scope(value, intent_type=intent_type)
            ):
                intents.add(intent_type)
        return frozenset(intents)

    def from_message(
        self,
        message: str,
        *,
        day_count: int = 1,
        trip_dates: Iterable[str] = (),
        eligible_evening_day_numbers: Iterable[int] | None = None,
        clarify_ambiguous_night: bool = True,
    ) -> GoalLedger:
        text = str(message or "")
        bounded_day_count = max(1, int(day_count or 1))
        all_day_numbers = tuple(range(1, bounded_day_count + 1))
        explicit_rest_days = self._explicit_rest_day_numbers(text, day_count=bounded_day_count)
        required_planning_days = tuple(day for day in all_day_numbers if day not in explicit_rest_days)
        allowed_days = required_planning_days
        daily_template_intents = self.authoritative_daily_template_intents(
            text,
            day_count=bounded_day_count,
        )
        campus_explicit_days = self._explicit_intent_day_numbers(
            text,
            intent_type="campus_visit",
            day_count=bounded_day_count,
        )
        meal_explicit_days = self._explicit_intent_day_numbers(
            text,
            intent_type="meal",
            day_count=bounded_day_count,
        )
        park_explicit_days = self._explicit_intent_day_numbers(
            text,
            intent_type="park",
            day_count=bounded_day_count,
        )
        evening_days = tuple(
            day
            for day in (
                eligible_evening_day_numbers
                if eligible_evening_day_numbers is not None
                else required_planning_days
            )
            if isinstance(day, int) and 1 <= day <= bounded_day_count
            and day in required_planning_days
        )
        goals: list[GoalCardinality] = []
        if re.search(r"985|211|高校|大学", text):
            daily_total = self._campus_daily_total(text, day_count=bounded_day_count)
            exact = self._campus_exact_count(text)
            optional = bool(re.search(r"最好|如果方便|有空|可选|不强求", text))
            if campus_explicit_days:
                target = len(campus_explicit_days)
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        0 if optional else target,
                        target,
                        target,
                        source="explicit_day_scope",
                        distribution_policy="every_allowed_day",
                        allowed_day_numbers=campus_explicit_days,
                    )
                )
            elif self._intent_has_single_scope(text, intent_type="campus_visit"):
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        0 if optional else 1,
                        1,
                        1,
                        source="explicit_single_scope",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=allowed_days,
                    )
                )
            elif daily_total is not None:
                daily_total = len(required_planning_days) * max(1, daily_total // bounded_day_count)
                minimum = 0 if optional else daily_total
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        minimum,
                        daily_total,
                        daily_total,
                        source="explicit_every_day",
                        distribution_policy="every_allowed_day",
                        allowed_day_numbers=allowed_days,
                    )
                )
            elif exact is not None:
                minimum = 0 if optional else exact
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        minimum,
                        exact,
                        exact,
                        allowed_day_numbers=allowed_days,
                    )
                )
            elif "campus_visit" in daily_template_intents:
                target = len(required_planning_days)
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        0 if optional else target,
                        target,
                        target,
                        source="multi_day_daily_template",
                        distribution_policy="every_allowed_day",
                        allowed_day_numbers=allowed_days,
                    )
                )
            else:
                single_day_trip = bounded_day_count == 1 and bool(
                    re.search(r"(?:一|1)\s*(?:天|日)(?:游|旅行|行程)", text)
                )
                goals.append(
                    GoalCardinality(
                        "campus_visit",
                        0 if optional else 1,
                        1,
                        1 if optional or single_day_trip else None,
                        allowed_day_numbers=allowed_days,
                    )
                )
        if re.search(r"博物馆|美术馆|艺术馆", text):
            goals.append(GoalCardinality("museum", 1, 1, None))
        if re.search(r"风土人情|当地文化|本地文化", text):
            goals.append(GoalCardinality("local_culture", 1, 1, None))
        if re.search(
            r"特色美食|当地[^，,。；;]{0,8}美食|本地[^，,。；;]{0,8}美食|地方饮食|"
            r"餐厅|午餐|午饭|中饭|中午.{0,10}(?:品尝|尝试|吃|美食)",
            text,
        ):
            explicit_every_day_meal = self._explicit_every_day_intent(text, intent_type="meal")
            single_scope_meal = self._intent_has_single_scope(text, intent_type="meal")
            every_day_meal = "meal" in daily_template_intents and not single_scope_meal
            target = len(meal_explicit_days) if meal_explicit_days else len(required_planning_days) if every_day_meal else 1
            meal_allowed_days = meal_explicit_days or allowed_days
            meal_day_scoped = bool(meal_explicit_days)
            is_single_day_explicit = bounded_day_count == 1 and bool(required_planning_days)
            goals.append(
                GoalCardinality(
                    "meal",
                    target if meal_day_scoped or every_day_meal or single_scope_meal or is_single_day_explicit else 0,
                    target,
                    target if meal_day_scoped or every_day_meal or single_scope_meal or is_single_day_explicit else 2,
                    source=(
                        "explicit_day_scope"
                        if meal_day_scoped
                        else "explicit_single_scope"
                        if single_scope_meal
                        else "explicit_every_day"
                        if explicit_every_day_meal
                        else "multi_day_daily_template"
                        if every_day_meal
                        else "explicit_user_request"
                    ),
                    distribution_policy=(
                        "every_allowed_day" if meal_day_scoped or every_day_meal else "spread_across_distinct_days"
                    ),
                    allowed_day_numbers=meal_allowed_days,
                )
            )
        if re.search(r"公园", text) and not re.search(r"(?:不要|不去|无需|取消).{0,6}公园", text):
            explicit_every_day_park = self._explicit_every_day_intent(text, intent_type="park")
            single_scope_park = self._intent_has_single_scope(text, intent_type="park")
            every_day_park = "park" in daily_template_intents and not single_scope_park
            target = len(park_explicit_days) if park_explicit_days else len(required_planning_days) if every_day_park else 1
            park_allowed_days = park_explicit_days or allowed_days
            park_day_scoped = bool(park_explicit_days)
            is_single_day_explicit = bounded_day_count == 1 and bool(required_planning_days)
            goals.append(
                GoalCardinality(
                    "park",
                    target if park_day_scoped or every_day_park or single_scope_park or is_single_day_explicit else 1,
                    target,
                    target if park_day_scoped or every_day_park or single_scope_park or is_single_day_explicit else None,
                    source=(
                        "explicit_day_scope"
                        if park_day_scoped
                        else "explicit_single_scope"
                        if single_scope_park
                        else "explicit_every_day"
                        if explicit_every_day_park
                        else "multi_day_daily_template"
                        if every_day_park
                        else "explicit_user_request"
                    ),
                    distribution_policy=(
                        "every_allowed_day" if park_day_scoped or every_day_park else "spread_across_distinct_days"
                    ),
                    allowed_day_numbers=park_allowed_days,
                )
            )
        if re.search(r"夜景|夜游|夜间观景|看灯光", text) and not re.search(
            r"(?:不要|不看|无需|取消).{0,4}(?:夜景|夜游|夜间观景|看灯光)", text
        ):
            optional = bool(re.search(r"最好|如果方便|有空|可选|不强求", text))
            exact = self._night_view_exact_count(text)
            explicit_day = self._night_view_explicit_day(
                text,
                trip_dates=tuple(str(item) for item in trip_dates),
                day_count=bounded_day_count,
            )
            explicit_single = bool(re.search(r"其中一晚|找一晚|只(?:看|安排)?(?:一|1)晚|安排(?:一|1)次", text))
            every_day = bool(
                re.search(
                    r"每天\s*晚上|每晚|每一晚|两晚都|两个晚上都|两天晚上都|"
                    r"(?:每个|每一个|所有)(?:可用(?:的)?)?\s*晚上|每天.{0,4}夜景",
                    text,
                )
            )
            if every_day:
                # Universal quantifiers are authoritative even when the phrase
                # also contains a Chinese numeral (for example, "两晚都").
                exact = None
            availability_ambiguous = bool(
                bounded_day_count > 1
                and re.search(r"(?:抵达|到达|落地|返程|离开|出发)", text)
                and explicit_day is None
                and exact is None
                and not explicit_single
                and not every_day
            )
            if optional:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        0,
                        1,
                        1,
                        source="soft_experience",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=evening_days,
                    )
                )
            elif availability_ambiguous or not evening_days:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        0,
                        0,
                        0,
                        source="clarification_required",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=evening_days,
                        limitation_reason=(
                            "night_view_evening_availability_ambiguous"
                            if availability_ambiguous
                            else "night_view_no_eligible_evening"
                        ),
                    )
                )
            elif explicit_day is not None:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        1,
                        1,
                        1,
                        source="explicit_day",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=(explicit_day,),
                        requested_count=1,
                    )
                )
            elif explicit_single:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        1,
                        1,
                        1,
                        source="explicit_single_evening",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=evening_days,
                        requested_count=1,
                    )
                )
            elif exact is not None:
                if exact > len(evening_days):
                    goals.append(
                        GoalCardinality(
                            "night_view",
                            0,
                            0,
                            0,
                            source="clarification_required",
                            distribution_policy="spread_across_distinct_days",
                            allowed_day_numbers=evening_days,
                            requested_count=exact,
                            limitation_reason="night_view_daily_max_one",
                        )
                    )
                else:
                    goals.append(
                        GoalCardinality(
                            "night_view",
                            exact,
                            exact,
                            exact,
                            source="explicit_count",
                            distribution_policy="spread_across_distinct_days",
                            allowed_day_numbers=evening_days,
                            requested_count=exact,
                        )
                    )
            elif every_day:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        len(evening_days),
                        len(evening_days),
                        len(evening_days),
                        source="explicit_every_day",
                        distribution_policy="every_allowed_day",
                        allowed_day_numbers=evening_days,
                    )
                )
            elif bounded_day_count > 1:
                # Preserve the keyword argument for compatibility, but never let
                # it turn one vague night-view wish into a per-evening mandate.
                # Only an explicit user answer may close this cardinality gap.
                _ = clarify_ambiguous_night
                goals.append(
                    GoalCardinality(
                        "night_view",
                        1,
                        len(evening_days),
                        len(evening_days),
                        source="clarification_required",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=evening_days,
                        requested_count=None,
                        limitation_reason="night_view_cardinality_ambiguous",
                    )
                )
            else:
                goals.append(
                    GoalCardinality(
                        "night_view",
                        1,
                        1,
                        1,
                        source="explicit_user_request",
                        distribution_policy="spread_across_distinct_days",
                        allowed_day_numbers=evening_days,
                    )
                )
        return GoalLedger(
            tuple(goals),
            required_planning_day_numbers=required_planning_days,
            explicit_rest_day_numbers=explicit_rest_days,
        )

    @classmethod
    def _explicit_every_day_intent(cls, text: str, *, intent_type: str) -> bool:
        if intent_type in cls.explicit_every_day_ordered_intents(text):
            return True
        pattern = cls._INTENT_PATTERNS[intent_type]
        return bool(
            re.search(r"(?:每天|每日|每一天|两天都)[^。！？!?；;\n]{0,64}" + pattern.pattern, text)
            or re.search(pattern.pattern + r"[^。！？!?；;\n]{0,32}(?:每天|每日|每一天|两天都)", text)
            # A repeated evening/night is also daily cardinality. Keep that
            # quantifier local to its activity, not a later comma-separated meal.
            or re.search(r"每(?:一)?(?:晚|夜)[^，,。！？!?；;\n]{0,32}" + pattern.pattern, text)
            or re.search(pattern.pattern + r"[^，,。！？!?；;\n]{0,16}每(?:一)?(?:晚|夜)", text)
        )

    @classmethod
    def _intent_has_single_scope(cls, text: str, *, intent_type: str) -> bool:
        patterns = {
            "campus_visit": (
                r"(?:只|仅).{0,8}(?:一|1)\s*所\s*(?:985|211)?\s*(?:高校|大学)",
                r"其中\s*(?:一|1)\s*天[^。！？!?；;\n]{0,32}(?:985|211|高校|大学|校园)",
            ),
            "meal": (
                r"(?:午餐|午饭|中饭|美食|中午)[^。！？!?；;\n]{0,16}(?:只|仅).{0,8}(?:一次|一顿)",
                r"(?:午餐|午饭|中饭|美食|中午)[^。！？!?；;\n]{0,16}(?:只|仅)\s*在?\s*其中\s*(?:一|1)\s*天",
                r"(?:只|仅).{0,16}(?:吃|品尝|安排)[^。！？!?；;\n]{0,8}(?:一次|一顿)",
                r"其中\s*(?:一|1)\s*天[^。！？!?；;\n]{0,32}(?:午餐|午饭|中饭|美食|中午)",
            ),
            "park": (
                r"公园[^，,。！？!?；;\n]{0,16}(?:只|仅).{0,8}(?:一次|一晚|一天)",
                r"(?:只|仅).{0,12}(?:去|逛|安排)[^。！？!?；;\n]{0,8}公园",
                r"其中\s*(?:一|1)\s*天[^。！？!?；;\n]{0,32}(?:晚上[^。！？!?；;\n]{0,12})?公园",
            ),
        }
        return any(re.search(pattern, text) for pattern in patterns[intent_type])

    @classmethod
    def _day_clauses(cls, text: str) -> tuple[tuple[int, str], ...]:
        clauses: list[tuple[int, str]] = []
        for match in cls._DAY_CLAUSE_RE.finditer(str(text or "")):
            token = str(match.group("zh") or match.group("arabic") or "")
            day_number = cls._number(token)
            if day_number > 0:
                clauses.append((day_number, str(match.group("body") or "")))
        return tuple(clauses)

    @classmethod
    def _explicit_intent_day_numbers(
        cls,
        text: str,
        *,
        intent_type: str,
        day_count: int,
    ) -> tuple[int, ...]:
        pattern = cls._INTENT_PATTERNS[intent_type]
        negative_pattern = re.compile(
            r"(?:不要|不去|不逛|不吃|无需|取消)[^。！？!?；;\n]{0,12}" + pattern.pattern
        )
        return tuple(
            sorted(
                {
                    day_number
                    for day_number, body in cls._day_clauses(text)
                    if 1 <= day_number <= day_count
                    and pattern.search(body)
                    and negative_pattern.search(body) is None
                }
            )
        )

    @classmethod
    def _explicit_rest_day_numbers(cls, text: str, *, day_count: int) -> tuple[int, ...]:
        rest_days: set[int] = set()

        def is_explicit_rest_body(body: str, *, arrival: bool = True, departure: bool = True) -> bool:
            explicit_rest = bool(
                re.search(r"(?<!不)(?:休息|自由活动)", body)
                and re.search(r"(?:不|无需|不用)(?:休息|自由活动)", body) is None
            )
            transit_markers = []
            if arrival:
                transit_markers.extend(("抵达", "到达"))
            if departure:
                transit_markers.extend(("返程", "离开"))
            explicit_transit_only = bool(
                transit_markers
                and re.search(
                    r"(?:只\s*安排|仅\s*安排|只|仅)\s*(?:"
                    + "|".join(transit_markers)
                    + r")",
                    body,
                )
            )
            return explicit_rest or explicit_transit_only

        for day_number, body in cls._day_clauses(text):
            if 1 <= day_number <= day_count and is_explicit_rest_body(body):
                rest_days.add(day_number)
        last_day_clause = re.search(
            r"(?:最后|末)\s*一天(?P<body>[^。！？!?；;\n]{0,24})",
            text,
        )
        if last_day_clause and is_explicit_rest_body(
            str(last_day_clause.group("body") or ""),
            arrival=False,
        ):
            rest_days.add(day_count)
        first_day_clause = re.search(r"首日(?P<body>[^。！？!?；;\n]{0,24})", text)
        if first_day_clause and is_explicit_rest_body(
            str(first_day_clause.group("body") or ""),
            departure=False,
        ):
            rest_days.add(1)
        return tuple(sorted(rest_days))

    @classmethod
    def _night_view_exact_count(cls, text: str) -> Optional[int]:
        night_span = re.search(
            r"([一二两三四五六\d]+)\s*晚\s*都?.{0,8}(?:夜景|夜游|夜间观景)",
            text,
        )
        if night_span:
            return cls._number(night_span.group(1))
        match = re.search(r"(?:看|安排|体验)?\s*([一二两三四五六\d]+)\s*(?:处|个|次)\s*(?:夜景|夜游|夜间观景)", text)
        return cls._number(match.group(1)) if match else None

    @classmethod
    def _night_view_explicit_day(
        cls,
        text: str,
        *,
        trip_dates: tuple[str, ...],
        day_count: int,
    ) -> Optional[int]:
        ordinal = re.search(
            r"(?:只在|安排在)?第\s*([一二两三四五六\d]+)\s*天(?:的)?\s*(?:晚上|夜间).{0,6}(?:夜景|夜游|看灯光)",
            text,
        )
        if ordinal:
            value = cls._number(ordinal.group(1))
            return value if 1 <= value <= day_count else None
        day_label = re.search(r"Day\s*([1-9]\d*).{0,8}(?:晚上|夜景|夜游)", text, re.IGNORECASE)
        if day_label:
            value = int(day_label.group(1))
            return value if 1 <= value <= day_count else None
        calendar = re.search(r"(\d{1,2})月(\d{1,2})日.{0,8}(?:晚上|夜景|夜游)", text)
        if not calendar:
            return None
        month, day = int(calendar.group(1)), int(calendar.group(2))
        for index, raw_date in enumerate(trip_dates, start=1):
            try:
                parsed = date.fromisoformat(raw_date[:10])
            except (TypeError, ValueError):
                continue
            if parsed.month == month and parsed.day == day:
                return index
        return None

    @classmethod
    def _campus_daily_total(cls, text: str, *, day_count: int) -> Optional[int]:
        if "campus_visit" in cls.explicit_every_day_ordered_intents(text):
            return max(1, int(day_count or 1))
        daily_count = re.search(
            r"(?:每天|每日).{0,10}?([一二两三四五六\d]+)\s*所\s*(?:985|211)?\s*(?:高校|大学)",
            text,
        )
        daily_campus = daily_count or re.search(
            r"(?:每天|每日).{0,10}?(?:985|211)?\s*(?:高校|大学|校园)",
            text,
        )
        if not daily_campus:
            return None
        per_day = cls._number(daily_count.group(1)) if daily_count else 1
        explicit_days = re.search(r"([一二两三四五六\d]+)\s*天.{0,20}?(?:每天|每日)", text)
        total_days = cls._number(explicit_days.group(1)) if explicit_days else max(1, int(day_count or 1))
        return total_days * per_day

    @classmethod
    def _campus_exact_count(cls, text: str) -> Optional[int]:
        daily = re.search(
            r"([一二两三四五六\d]+)天.*?(?:每天|各(?:天|日)?).*?([一二两三四五六\d]+)所",
            text,
        )
        if daily:
            return cls._number(daily.group(1)) * cls._number(daily.group(2))
        match = re.search(r"([一二两三四五六\d]+)所\s*(?:985|211)?\s*(?:高校|大学)", text)
        return cls._number(match.group(1)) if match else None

    @staticmethod
    def _number(value: str) -> int:
        return int(value) if value.isdigit() else _CHINESE_NUMBERS.get(value, 1)
