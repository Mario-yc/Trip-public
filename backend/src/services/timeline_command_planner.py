import re
from dataclasses import dataclass, field
from typing import Any, Optional


CHINESE_DAY_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
}


@dataclass
class TimelineEditCommand:
    intent: str
    scope: str
    operation: str
    day_number: Optional[int] = None
    segment_kind: Optional[str] = None
    time_window: Optional[str] = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "scope": self.scope,
            "operation": self.operation,
            "dayNumber": self.day_number,
            "segmentKind": self.segment_kind,
            "timeWindow": self.time_window,
            "constraints": self.constraints,
        }


class TimelineCommandPlanner:
    def compile(self, directive: Any, observation: Any) -> Optional[TimelineEditCommand]:
        """Compile an accepted PatchDirective; never infer the primary action."""
        if hasattr(directive, "model_dump"):
            payload = directive.model_dump(by_alias=True, exclude_none=True)
        elif isinstance(directive, dict):
            payload = dict(directive)
        else:
            return None
        if payload.get("type") != "patch_itinerary":
            return None
        operation = str(payload.get("operationIntent") or "")
        allowed = {
            "replace_segment_start_time",
            "replace_segment_poi",
            "replace_segment_poi_from_candidate",
            "remove_segment",
            "move_segment",
            "add_segment",
        }
        if operation not in allowed:
            return None
        segment_ids = [str(item) for item in payload.get("targetSegmentIds") or [] if str(item)]
        if not segment_ids:
            return None
        return TimelineEditCommand(
            intent="directive_patch",
            scope="segment",
            operation=operation,
            constraints={
                "baseVersionId": payload.get("baseVersionId"),
                "targetSegmentIds": segment_ids,
                "requestedOutcome": payload.get("requestedOutcome"),
                "preserve": list(payload.get("preserve") or []),
                "maxChangedSegmentCount": int(payload.get("maxChangedSegmentCount") or 1),
                "startTime": payload.get("startTime"),
                "candidateId": payload.get("candidateId"),
                "amapPoiId": payload.get("amapPoiId"),
            },
        )

    def parse(self, message: str, *, has_active_timeline: bool) -> Optional[TimelineEditCommand]:
        if not has_active_timeline:
            return None
        text = str(message or "").strip()
        if not text:
            return None
        return (
            self._parse_campus_replace(text)
            or self._parse_exact_place_replace(text)
            or self._parse_night_view_options(text)
            or self._parse_night_view_replace(text)
            or self._parse_meal_option_choice(text)
            or self._parse_meal_options(text)
            or self._parse_night_view_option_choice(text)
            or self._parse_remove_meal(text)
            or self._parse_meal_edit(text)
            or self._parse_fill_dinner(text)
            or self._parse_cost_display(text)
            or self._parse_transport_preference(text)
        )

    def _parse_remove_meal(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(删除|删掉|移除|取消)", text):
            return None
        if not re.search(r"(早餐|早饭|午餐|午饭|中餐|中饭|中午|晚餐|晚饭|餐饮|吃饭)", text):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="meal",
            operation="remove",
            day_number=self._day_number(text),
            segment_kind="meal",
            time_window=self._meal_time_window(text),
            constraints={
                "keepExistingAnchors": True,
                "avoidGlobalReplan": True,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
            },
        )

    def _parse_meal_options(self, text: str) -> Optional[TimelineEditCommand]:
        if not self._looks_like_meal_edit(text):
            return None
        institutional_choice_needed = bool(
            re.search(r"(大学|高校|校园|学校).{0,12}(食堂|餐厅)|(食堂|餐厅).{0,12}(大学|高校|校园|学校)", text)
        )
        if not institutional_choice_needed and not re.search(
            r"(先看|看看|候选|推荐|找几个|给我|选项|再决定|再选)", text
        ):
            return None
        food_intent = self._food_intent(text, default="当地特色美食")
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="meal",
            operation="offer_local_poi_options",
            day_number=self._day_number(text),
            segment_kind="meal",
            time_window=self._meal_time_window(text),
            constraints={
                "intentType": "meal",
                "foodIntent": food_intent,
                "targetMealText": self._target_meal_text(text, food_intent),
                "areaIntent": self._meal_area_intent(text),
                "containmentMode": "inside_or_same_complex" if self._meal_area_intent(text) else "nearby",
                "routeAwareOptions": True,
                "optionCount": 3,
                "includeCustomOption": True,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
                "avoidGlobalReplan": True,
            },
        )

    def _parse_meal_option_choice(self, text: str) -> Optional[TimelineEditCommand]:
        selected_index = self._selected_option_index(text)
        if selected_index is None or not re.search(r"(餐|饭|吃|酸菜鱼|寿司|火锅|小吃|美食|候选|选项)", text):
            return None
        selected_text = ""
        match = re.search(r"(?:第\s*[1-4一二三四]\s*个)?\s*[:：]\s*([^，,。；;]+)", text)
        if match:
            selected_text = re.sub(r"（餐饮候选）$", "", match.group(1)).strip()
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="meal",
            operation="choose_previous_local_poi_option",
            constraints={
                "selectedOptionIndex": selected_index,
                "selectedOptionText": selected_text,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
            },
        )

    def _parse_meal_edit(self, text: str) -> Optional[TimelineEditCommand]:
        if not self._looks_like_meal_edit(text):
            return None
        day_number = self._day_number(text)
        time_window = self._meal_time_window(text)
        food_intent = self._food_intent(text, default="当地特色美食")
        area_intent = self._meal_area_intent(text)
        constraints = {
            "foodIntent": food_intent,
            "targetMealText": self._target_meal_text(text, food_intent),
            "keepExistingAnchors": True,
            "preferNearby": True,
            "avoidGlobalReplan": True,
        }
        excluded_families = self._excluded_dish_families(text)
        if excluded_families:
            constraints["excludeDishFamilies"] = excluded_families
        if area_intent:
            # A shopping complex is a search anchor, never the meal POI itself.
            constraints.update({"areaIntent": area_intent, "containmentMode": "inside_or_same_complex"})
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="meal",
            operation="replace_or_fill",
            day_number=day_number,
            segment_kind="meal",
            time_window=time_window,
            constraints=constraints,
        )

    def _parse_campus_replace(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(大学|高校|学院|校园|校区)", text):
            return None
        if not re.search(r"(换|换掉|替换|不要|不想去|别去|保留)", text):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="campus",
            operation="replace_kind",
            day_number=self._day_number(text),
            segment_kind="visit",
            time_window=self._daypart_time_window(text),
            constraints={
                "intentType": "campus_visit",
                "excludeCurrentSameKind": True,
                "keepUserNamedAnchors": bool(re.search(r"(保留|留下|不要动)", text)),
                "keepMealsAndNightViews": True,
                "avoidGlobalReplan": True,
                "excludeNames": self._excluded_names(text),
                "keepNames": self._kept_names(text),
            },
        )

    def _parse_exact_place_replace(self, text: str) -> Optional[TimelineEditCommand]:
        query = self._positive_place_target(text)
        if not query:
            return None
        if not re.search(r"(直接去|单纯去|改为去|改成去|换成去|换去)\s*[^，,。；;]{1,30}", text):
            return None
        if not re.search(r"(夜景|观景|晚上|夜晚|灯光)", text, re.IGNORECASE):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="night_view",
            operation="ground_or_replace_specific_poi",
            day_number=self._day_number(text),
            segment_kind="visit",
            time_window="evening",
            constraints={
                "intentType": "exact_place",
                "query": query,
                "excludeCurrentSameKind": True,
                "excludeNames": self._excluded_names(text),
                "requireConcreteAmapPoi": True,
                "rejectGenericPlaceholder": True,
                "rejectCompositePoi": True,
                "avoidWeakSubentity": True,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
                "allowHighRouteCostIfUserExplicit": True,
                "avoidGlobalReplan": True,
            },
        )

    def _parse_night_view_options(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(夜景|夜游|观景|晚上|夜晚|灯光|城市夜景)", text):
            return None
        if re.search(r"(大学|高校|校园|学院)", text) and re.search(
            r"(保留|留下|不要动).{0,8}(夜景)|夜景.{0,8}(保留|留下|不要动)", text
        ):
            return None
        if not re.search(r"(先看|看看|候选|推荐|找几个|给我|选项)", text):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="night_view",
            operation="offer_local_poi_options",
            day_number=self._day_number(text),
            segment_kind="visit",
            time_window="evening",
            constraints={
                "intentType": "night_view",
                "theme": "城市夜景" if "城市夜景" in text else "夜景",
                "requireConcreteAmapPoi": True,
                "routeAwareOptions": True,
                "optionCount": 3,
                "includeCustomOption": True,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
            },
        )

    def _parse_night_view_option_choice(self, text: str) -> Optional[TimelineEditCommand]:
        selected_index = self._selected_option_index(text)
        selected_text = self._night_view_selected_text(text)
        if selected_index is None and not selected_text:
            return None
        if selected_index is None and not re.search(
            r"(夜景|夜游|观景|晚上|夜晚|我选|选择|就选|那就)",
            text,
        ):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="night_view",
            operation="choose_previous_local_poi_option",
            constraints={
                "selectedOptionText": selected_text,
                "selectedOptionIndex": selected_index,
                "requireConcreteAmapPoi": True,
                "avoidTicketLookup": True,
                "avoidWebSearch": True,
            },
        )

    def _parse_night_view_replace(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(夜景|夜游|观景|晚上|夜晚|灯光|拍照)", text):
            return None
        if not re.search(
            r"(换|换掉|替换|不要|不想去|别去|更适合|拍照|改|改成|改为|具体|真实|高德|地点|细化|落成)", text
        ):
            return None
        query = self._night_view_query(text)
        require_concrete_amap_poi = bool(
            re.search(r"(不是具体|具体|真实|高德|地图地点|真实地点|真实的地点|落成具体|细化成|改成真实|改为真实)", text)
            or query
        )
        constraints = {
            "intentType": "exact_place" if self._is_exact_place_query(query, text) else "night_view",
            "excludeCurrentSameKind": True,
            "keepMealsAndCampuses": True,
            "avoidWeakSubentity": True,
            "avoidGlobalReplan": True,
            "excludeNames": self._excluded_names(text),
        }
        if require_concrete_amap_poi:
            constraints.update(
                {
                    "requireConcreteAmapPoi": True,
                    "rejectGenericPlaceholder": True,
                    "rejectCompositePoi": True,
                    "avoidTicketLookup": True,
                    "avoidWebSearch": True,
                }
            )
        if query:
            constraints.update(
                {
                    "query": query,
                    "allowHighRouteCostIfUserExplicit": True,
                }
            )
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="night_view",
            operation="ground_or_replace_specific_poi" if require_concrete_amap_poi else "replace_kind",
            day_number=self._day_number(text),
            segment_kind="visit",
            time_window="evening",
            constraints=constraints,
        )

    def _parse_fill_dinner(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(补上|补全|补|加上|添加|增加|安排|放在)", text):
            return None
        if not re.search(r"(晚餐|晚饭|dinner)", text, re.IGNORECASE):
            return None
        day_number = self._day_number(text)
        constraints = {
            "keepExistingAnchors": bool(re.search(r"(别|不要|不用|不必).{0,4}重排|只(补|改|调整)|不动|保留", text)),
            "foodIntent": self._food_intent(text, default="当地特色美食"),
            "placementHint": self._placement_hint(text),
        }
        anchor = self._nearby_anchor(text)
        if anchor:
            constraints["nearbyAnchor"] = anchor
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="meal",
            operation="replace_or_fill",
            day_number=day_number,
            segment_kind="meal",
            time_window="dinner",
            constraints=constraints,
        )

    def _parse_cost_display(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(人均|按人均|费用|预算)", text):
            return None
        if not re.search(r"(显示|表达|展示|改成|按)", text):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="budget",
            operation="update_display_policy",
            constraints={"costDisplay": "per_person"},
        )

    def _parse_transport_preference(self, text: str) -> Optional[TimelineEditCommand]:
        if not re.search(r"(不要|别|不用).{0,4}(骑行|自行车|单车)|只要.{0,6}(公交|地铁)|公交地铁", text):
            return None
        return TimelineEditCommand(
            intent="timeline_edit",
            scope="route",
            operation="update_transport_preference",
            constraints={
                "transportMode": "transit",
                "avoidModes": ["bicycling"],
                "refreshRoutesOnly": True,
                "requiresCaveatIfFallback": True,
            },
        )

    def _looks_like_meal_edit(self, text: str) -> bool:
        meal_marker = re.search(
            r"(早餐|早饭|午餐|午饭|中餐|中饭|中午|晚餐|晚饭|餐饮|餐厅|餐馆|食堂|吃|美食|小吃|菜|咖啡|下午茶|面|鱼|饭)",
            text,
        )
        edit_marker = re.search(
            r"(想吃|想换|换一种|换成|换到|换一个|换掉|补一个|补上|补全|改成|改为|改到|只改|安排|附近|当地特色|地方特色|太远|太贵|不好吃|不吃|不要)",
            text,
        )
        return bool(meal_marker and edit_marker)

    def _meal_time_window(self, text: str) -> str:
        if re.search(r"(早餐|早饭)", text):
            return "breakfast"
        if re.search(r"(午餐|午饭|中餐|中饭|中午)", text):
            return "lunch"
        if re.search(r"(晚餐|晚饭|dinner)", text, re.IGNORECASE):
            return "dinner"
        return "auto"

    def _daypart_time_window(self, text: str) -> Optional[str]:
        if re.search(r"(上午|早上|上午段|morning)", text, re.IGNORECASE):
            return "morning"
        if re.search(r"(下午|午后|afternoon)", text, re.IGNORECASE):
            return "afternoon"
        if re.search(r"(晚上|夜晚|傍晚|evening|night)", text, re.IGNORECASE):
            return "evening"
        return None

    def _food_intent(self, text: str, *, default: str) -> str:
        cleaned = re.sub(r"第\s*[一二两三四五六七\d]+\s*[天日]", "", text)
        cleaned = re.sub(
            r"(早餐|早饭|午餐|午饭|中餐|中饭|晚餐|晚饭|餐饮|餐厅|附近|当地特色|只改|不要重排|不重排|景点|其他内容|其它内容)",
            " ",
            cleaned,
        )
        match = re.search(r"(?:想吃|换成|改成|补一个|安排|吃)\s*([^，,。；;]{1,20})", cleaned)
        if match:
            value = self._clean_food_intent(match.group(1))
            if value:
                return value
        for pattern in [
            r"([^，,。；;]{2,12})(?:太远|太贵|不好吃|不想吃|换掉)",
            r"(本地菜|当地菜|地方菜|特色餐厅|特色美食|小吃|咖啡|下午茶)",
            r"附近的?([^，,。；;]{1,12})",
        ]:
            match = re.search(pattern, text)
            if match:
                value = self._clean_food_intent(match.group(1) if match.lastindex else match.group(0))
                if value:
                    return value
        return default

    def _excluded_dish_families(self, text: str) -> list[str]:
        excluded: list[str] = []
        for match in re.finditer(r"(?:不要|不吃|避开|排除)\s*([^，,。；;]{1,12})", text):
            value = self._clean_food_intent(match.group(1))
            if value and value not in excluded:
                excluded.append(value)
        return excluded

    def _target_meal_text(self, text: str, fallback: str) -> str:
        match = re.search(r"(?:的|原来)?([^，,。；;]{2,16})(?:太远|太贵|不好吃|不想吃|换掉)", text)
        if not match:
            return fallback
        value = re.sub(r"^(第\s*[一二两三四五六七\d]+\s*[天日])?的?", "", match.group(1)).strip()
        return self._clean_food_intent(value) or fallback

    def _meal_area_intent(self, text: str) -> str:
        match = re.search(
            r"(?:改到|换到|放到|安排到)\s*([^，,。；;]{2,30}?)(?:里面|内|里)(?:的)?(?:店|餐厅|饭店)?", text
        )
        if not match:
            return ""
        value = re.sub(r"^(第\s*[一二两三四五六七\d]+\s*[天日])?的?", "", match.group(1)).strip()
        return re.sub(r"(?:的)?(?:店|餐厅|饭店)$", "", value).strip()[:30]

    def _clean_food_intent(self, value: str) -> str:
        value = re.sub(r"^(?:第\s*[一二两三四五六七\d]+\s*[天日])?的?", "", str(value or "").strip())
        value = re.sub(r"(吧|一下|一点|附近|餐厅|饭店|美食)$", "", str(value or "").strip())
        value = re.sub(r"(不要|别|不用).*$", "", value).strip()
        return value[:30]

    def _excluded_names(self, text: str) -> list[str]:
        names: list[str] = []
        for pattern in [r"(?:不要|不去|别去|不想去)\s*([^，,。；;]+)", r"([^，,。；;]+?)\s*换掉"]:
            for match in re.finditer(pattern, text):
                names.extend(self._split_names(match.group(1)))
        return names[:12]

    def _kept_names(self, text: str) -> list[str]:
        names: list[str] = []
        for match in re.finditer(r"保留\s*([^，,。；;]+)", text):
            names.extend(self._split_names(match.group(1)))
        return names[:12]

    def _split_names(self, value: str) -> list[str]:
        parts = re.split(r"[、/和与及\s]+", str(value or ""))
        cleaned: list[str] = []
        for part in parts:
            name = re.sub(r"(这些|几所|几个|其他|其它|大学|高校|学院|夜景|地方|地点)$", "", part.strip())
            if len(name) >= 2:
                cleaned.append(name)
        return cleaned

    def _selected_option_index(self, text: str) -> Optional[int]:
        match = re.search(r"(?:选|选择|我选|就选)\s*([1-4一二三四])", text)
        if not match:
            match = re.search(r"第\s*([1-4一二三四])\s*个", text)
        if not match:
            return None
        value = match.group(1)
        if value.isdigit():
            return int(value)
        return CHINESE_DAY_NUMBERS.get(value)

    def _night_view_selected_text(self, text: str) -> str:
        cleaned = str(text or "").strip()
        positive_target = self._positive_place_target(cleaned)
        if positive_target:
            return positive_target
        cleaned = re.sub(r"^(那就|就|我选|选择|选|去|我想去|还是|就去)", "", cleaned)
        cleaned = re.sub(r"(吧|就行|好了|可以|看看|看夜景|夜景)$", "", cleaned).strip(" ：:，,。；;")
        choice_match = re.search(
            r"(?:第\s*[1-4一二三四]\s*个|(?:我)?(?:选择|选)\s*[1-4一二三四])\s*[：:，,]\s*(.+)$",
            text,
        )
        if choice_match:
            value = re.sub(
                r"^(?:我自己填写具体地点|我自己填写|自定义|手动输入)\s*[：:，,]?",
                "",
                choice_match.group(1).strip(),
            )
            value = re.sub(r"(吧|就行|好了|可以|看看|看夜景|夜景)$", "", value).strip(" ：:，,。；;")
            return value[:30] if len(value) >= 2 else ""
        if self._selected_option_index(text) is not None:
            return ""
        return cleaned[:30] if len(cleaned) >= 2 else ""

    def _night_view_query(self, text: str) -> str:
        positive_target = self._positive_place_target(text)
        if positive_target:
            return positive_target
        patterns = [
            r"(?:夜景|晚上|夜晚).{0,10}(?:改成|改为|换成|去)\s*([^，,。；;]{2,30})",
            r"(?:改成|改为|换成|去)\s*([^，,。；;]{2,30})(?:看夜景|夜景)?",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = re.sub(r"(看夜景|夜景|真实地点|具体地点|真实的地点|吧|一下)$", "", match.group(1).strip())
            if value and not re.search(r"^(真实|真实的|具体|地点|高德|地图|夜景观景点|城市夜景)$", value):
                return value[:30]
        return ""

    def _positive_place_target(self, text: str) -> str:
        matches: list[str] = []
        for pattern in [
            r"(?:直接去|单纯去|就去|改为去|改成去|换成去|换去)\s*([^，,。；;]{1,30})",
            r"(?:改成|改为|换成)\s*([^，,。；;]{1,30})(?:看夜景|夜景|观景)?",
        ]:
            for match in re.finditer(pattern, text):
                value = self._clean_place_target(match.group(1))
                if value:
                    matches.append(value)
        return matches[-1] if matches else ""

    def _clean_place_target(self, value: str) -> str:
        cleaned = str(value or "").strip(" ：:，,。；;")
        cleaned = re.sub(r"(这个|该)?\s*(POI|poi|地点|地方)$", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"(看夜景|夜景|观景|吧|就行|好了|可以|一下)$", "", cleaned).strip(" ：:，,。；;")
        cleaned = re.sub(r"^(不是这个|不要|不去|别去|不想去)\s*", "", cleaned).strip()
        if re.fullmatch(r"(真实|真实的|具体|具体的|高德|地图)", cleaned):
            return ""
        return cleaned[:30] if len(cleaned) >= 2 else ""

    def _is_exact_place_query(self, query: str, text: str) -> bool:
        return bool(query and re.search(r"(直接|单纯|就行|不是这个|不去|不要)", text))

    def _day_number(self, text: str) -> Optional[int]:
        patterns = [
            r"(?:Day|day)\s*(\d+)",
            r"第\s*(\d+)\s*[天日]",
            r"第\s*([一二两三四五六七])\s*[天日]",
            r"(\d+)\s*[天日]",
            r"([一二两三四五六七])\s*[天日]",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = match.group(1)
            if value.isdigit():
                return int(value)
            return CHINESE_DAY_NUMBERS.get(value)
        return None

    def _placement_hint(self, text: str) -> str:
        if re.search(r"(之间|中间)", text) and re.search(r"(夜景|晚上)", text):
            return "before_night_view"
        if re.search(r"(下午|高校|校园|大学)", text) and re.search(r"(之后|后面|后)", text):
            return "after_afternoon_anchor"
        return ""

    def _nearby_anchor(self, text: str) -> str:
        match = re.search(r"([^\s，,。；;]{2,20})附近", text)
        if not match:
            return ""
        return match.group(1).strip()
