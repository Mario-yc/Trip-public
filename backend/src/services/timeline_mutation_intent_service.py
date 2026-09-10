from __future__ import annotations

import re
from typing import Optional

from src.services.timeline_mutation_models import (
    TimelineMutationIntent,
    TimelineMutationReplacement,
    TimelineMutationSelector,
)


_DAY_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("museum", re.compile(r"美术馆|艺术博物馆|博物馆|博物院|画院|艺术馆")),
    ("campus_visit", re.compile(r"大学|学院|高校|校园|校区")),
    ("meal", re.compile(r"餐厅|饭店|餐饮|午餐|晚餐|早餐|美食")),
    ("night_view", re.compile(r"夜景|夜游|观景台|天际线")),
    ("park", re.compile(r"公园|园林|植物园|湿地")),
)


class TimelineMutationIntentExtractor:
    """Extracts high-confidence semantic edits without accepting internal IDs."""

    def extract(self, text: str, *, has_active_timeline: bool) -> Optional[TimelineMutationIntent]:
        source = str(text or "").strip()
        if not source or not has_active_timeline:
            return None
        day_number = self._day_number(source)

        transport = re.fullmatch(
            r"(?:把)?(?:第?[一二三四五六七八九十\d]+天)?\s*(?P<from>.+?)到(?P<to>.+?)改为(?P<mode>公交地铁|公共交通|地铁|公交|步行|驾车|自驾|打车|出租车)",
            source,
        )
        if transport:
            return self._intent(
                "set_transport_mode",
                source,
                day_number,
                current_text=transport.group("to").strip(),
                from_text=transport.group("from").strip(),
                to_text=transport.group("to").strip(),
                transport_mode=self._transport_mode(transport.group("mode")),
            )

        duration = re.fullmatch(
            r"(?:把)?(?:第?[一二三四五六七八九十\d]+天(?:的)?)?\s*(?P<target>.+?)\s*停留(?:时间)?改为\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>小时|分钟)",
            source,
        )
        if duration:
            value = float(duration.group("value"))
            minutes = round(value * 60) if duration.group("unit") == "小时" else round(value)
            return self._intent(
                "set_duration", source, day_number, current_text=duration.group("target").strip(), duration_minutes=minutes
            )

        start_time = re.fullmatch(
            r"(?:把)?(?:第?[一二三四五六七八九十\d]+天(?:的)?)?\s*(?P<target>.+?)\s*(?:改到|调整到|开始时间改为)\s*(?P<time>[0-2]?\d:[0-5]\d)",
            source,
        )
        if start_time:
            normalized = self._clock(start_time.group("time"))
            return self._intent(
                "set_start_time", source, day_number, current_text=start_time.group("target").strip(), start_time=normalized
            )

        remove = re.fullmatch(
            r"(?:删除|移除|去掉)\s*(?:第?[一二三四五六七八九十\d]+天(?:的)?)?\s*(?P<target>.+)", source
        )
        if remove:
            return self._intent("remove_segment", source, day_number, current_text=remove.group("target").strip())

        replacement = re.fullmatch(
            r"(?:把)?(?:第?[一二三四五六七八九十\d]+天(?:的)?)?\s*(?P<target>.+?)\s*(?:改为|替换为|换成)\s*(?P<replacement>.+)",
            source,
        )
        if replacement:
            current = replacement.group("target").strip()
            query = replacement.group("replacement").strip()
            generic_replacement = re.fullmatch(r"(?:真实|具体|合适|别的|其他)(?:的)?地点", query)
            if (
                current
                and query
                and "不是具体" not in current
                and generic_replacement is None
                and not re.fullmatch(r"\d+(?:\.\d+)?\s*(?:小时|分钟)", query)
            ):
                return self._intent("replace_poi", source, day_number, current_text=current, poi_query=query)
        return None

    def _intent(self, operation: str, source: str, day_number: Optional[int], **values) -> TimelineMutationIntent:
        current_text = values.get("current_text")
        return TimelineMutationIntent(
            operation=operation,
            selector=TimelineMutationSelector(
                dayNumber=day_number,
                intentType=self._intent_type(" ".join(filter(None, [current_text, values.get("to_text")]))),
                currentText=current_text,
                fromText=values.get("from_text"),
                toText=values.get("to_text"),
            ),
            replacement=TimelineMutationReplacement(
                poiQuery=values.get("poi_query"),
                startTime=values.get("start_time"),
                durationMinutes=values.get("duration_minutes"),
                transportMode=values.get("transport_mode"),
            ),
            confidence=0.98,
            source="deterministic_parser",
            sourceText=source,
        )

    @staticmethod
    def _day_number(text: str) -> Optional[int]:
        match = re.search(r"第?([一二三四五六七八九十\d]+)天", text)
        if not match:
            return None
        value = match.group(1)
        if value.isdigit():
            return int(value)
        if value == "十":
            return 10
        if value.startswith("十"):
            return 10 + _DAY_DIGITS.get(value[1:], 0)
        if value.endswith("十"):
            return _DAY_DIGITS.get(value[:-1], 0) * 10
        return _DAY_DIGITS.get(value)

    @staticmethod
    def _intent_type(text: str) -> Optional[str]:
        for intent_type, pattern in _INTENT_PATTERNS:
            if pattern.search(text or ""):
                return intent_type
        return None

    @staticmethod
    def _clock(value: str) -> str:
        hour, minute = value.split(":", 1)
        return f"{int(hour):02d}:{int(minute):02d}"

    @staticmethod
    def _transport_mode(value: str) -> str:
        if value in {"公交地铁", "公共交通", "地铁", "公交"}:
            return "transit"
        if value in {"驾车", "自驾"}:
            return "driving"
        if value in {"打车", "出租车"}:
            return "taxi"
        return "walking"
