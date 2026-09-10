from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


PLANNER_VERSION = "explicit-planner-p1-v1"


@dataclass(frozen=True)
class AgentPlanItem:
    goal: str
    successCriteria: str
    requiresTool: str | None = None


@dataclass(frozen=True)
class AgentPlan:
    plannerVersion: str
    intent: str
    taskType: str
    taskRoute: str
    writeIntent: str
    requiresPoiResolution: bool
    requiresPatch: bool
    requiresRollback: bool
    requiresRealtimeInfo: bool
    readOnly: bool
    allowedTools: list[str]
    toolStrategy: dict[str, Any]
    qualityFocus: list[str]
    hasCurrentItinerary: bool
    hasPendingPoiCandidates: bool
    expectedHarnessStages: list[str]
    riskControls: list[str]
    items: list[AgentPlanItem]

    def to_context(self) -> dict[str, Any]:
        return asdict(self)


class AgentPlannerService:
    def plan(self, latest_message: str, request_context: dict[str, Any]) -> dict[str, Any]:
        planning_message = str(request_context.get("effectiveUserMessage") or latest_message or "")
        text = planning_message.lower()
        latest_text = str(latest_message or request_context.get("latestUserMessage") or "").lower()
        observation = request_context.get("agentObservation") if isinstance(request_context.get("agentObservation"), dict) else {}
        itinerary_observation = observation.get("itinerary") if isinstance(observation.get("itinerary"), dict) else {}
        has_itinerary = bool(
            itinerary_observation.get("activeVersionId")
            and int(itinerary_observation.get("meaningfulSegmentCount") or 0) > 0
        )
        has_pending_pois = bool(request_context.get("pendingAmapPoiCandidates"))
        has_selected_poi = bool(request_context.get("selectedMapPoi") or request_context.get("selectedMapPoiId"))
        understood = request_context.get("understoodRequirements") or {}
        needs_clarification = understood.get("isCompleteEnoughToPlan") is False or bool(understood.get("missingFields"))
        explicit_no_write = self._has_any(
            text, ["不要改", "先不要改", "先不改", "别改", "不要调整", "不修改", "先不要应用"]
        )
        local_edit_text = latest_text or text
        patch_intent = (not explicit_no_write) and self._has_any(
            text, ["改", "调整", "移动", "删除", "添加", "加入", "时间", "标题", "放到", "换成", "patch"]
        )
        latest_patch_intent = (not explicit_no_write) and self._has_any(
            local_edit_text, ["改", "改成", "改为", "调整", "移动", "删除", "添加", "加入", "时间", "标题", "放到", "换成", "替换", "换掉"]
        )
        rollback_intent = self._has_any(text, ["历史", "编辑之前", "回滚", "撤回", "重新生成", "上一轮"])
        source_intent = self._has_any(
            latest_text or text,
            [
                "来源",
                "官方",
                "查一下",
                "联网",
                "核验",
                "是否营业",
                "票务",
                "预约",
                "开放时间",
                "门票",
                "搜索",
                "天气",
                "风险",
                "预警",
                "国庆",
                "十一",
                "黄金周",
                "节假日",
                "限流",
                "闭园",
                "临时闭园",
                "施工",
                "交通管制",
                "拥挤",
                "人流",
                "安全提示",
                "公告",
                "景区公告",
            ],
        )
        poi_intent = self._has_any(
            text, ["poi", "景点", "地点", "高德", "地图", "坐标", "餐厅", "博物馆", "故宫", "胡同"]
        )
        latest_poi_intent = self._has_any(
            local_edit_text, ["poi", "景点", "地点", "高德", "地图", "坐标", "餐厅", "博物馆", "故宫", "胡同", "夜景", "观景", "大学", "高校"]
        )
        if has_itinerary:
            patch_intent = patch_intent or latest_patch_intent
            poi_intent = poi_intent or latest_poi_intent
        local_poi_grounding_request = (
            has_itinerary
            and latest_patch_intent
            and latest_poi_intent
            and bool(re.search(r"(具体|真实|高德|地图地点|不是具体|改成|改为|替换|换掉|地点)", local_edit_text))
        )
        explicit_realtime_latest = bool(
            re.search(r"(查|搜索|官方|门票|预约|开放时间|营业|公告|风险|天气|核验|联网|限流|闭园|管制)", local_edit_text)
        )
        if local_poi_grounding_request and not explicit_realtime_latest:
            source_intent = False
        route_intent = bool(
            re.search(r"(优化.*路线|路线.*优化|调整交通|换成.*(?:地铁|公交)|少换乘|重排路线)", latest_text or text)
        )
        comparison_intent = self._has_any(
            text, ["三方案", "3方案", "三个方案", "方案比较", "对比方案", "比较", "plan comparison"]
        )
        initial_arrange_intent = self._has_any(text, ["安排", "规划", "生成", "做个行程", "帮我做", "帮我安排"])
        if not has_itinerary and not needs_clarification and re.search(
            r"(旅行|行程|\d+\s*日游|[一二三四五六七八九十两]+日游|参观|游览|体验)",
            text,
        ):
            # A complete trip request remains a write task even when holiday,
            # weather, reservation, or risk words also require later online
            # enrichment.
            initial_arrange_intent = True
        task_type = self._task_type(
            has_itinerary=has_itinerary,
            has_pending_pois=has_pending_pois,
            needs_clarification=needs_clarification,
            rollback_intent=rollback_intent,
            comparison_intent=comparison_intent,
            source_intent=source_intent,
            route_intent=route_intent,
            poi_intent=poi_intent,
            patch_intent=patch_intent,
            initial_arrange_intent=initial_arrange_intent,
        )

        intent = self._legacy_intent(task_type, has_itinerary or has_pending_pois, patch_intent)
        requires_poi_resolution = bool(poi_intent or has_selected_poi or has_pending_pois)
        read_only = task_type in {"clarification", "plan_comparison", "ticket_source_lookup"} and not patch_intent
        requires_patch = (
            task_type in {"initial_planning", "local_modification", "poi_grounding", "route_optimization"}
            and not has_pending_pois
            and not needs_clarification
        )
        requires_realtime_info = bool(source_intent)
        task_route = self._task_route(task_type, requires_patch, requires_poi_resolution, requires_realtime_info)
        expected_stages = self._expected_stages(
            task_type, requires_patch, requires_poi_resolution, requires_realtime_info
        )
        risk_controls = ["patch_before_write", "active_version_verifier", "planning_quality_contract"]
        if task_type == "plan_comparison":
            risk_controls.append("read_only_comparison")
        if task_type == "local_modification":
            risk_controls.append("minimal_patch")
        if task_type == "route_optimization":
            risk_controls.append("route_feasibility_soft_check")
        if requires_poi_resolution:
            risk_controls.append("amap_poi_grounding")
        if has_pending_pois:
            risk_controls.append("pending_poi_state_machine")
        if rollback_intent:
            risk_controls.append("rollback_supersession")
        if source_intent:
            risk_controls.append("source_transparency")
        items = self._items(
            requires_poi_resolution=requires_poi_resolution,
            requires_patch=requires_patch,
            has_pending_pois=has_pending_pois,
            rollback_intent=rollback_intent,
            source_intent=source_intent,
            needs_clarification=needs_clarification and not rollback_intent,
            comparison_intent=comparison_intent,
            route_intent=route_intent,
        )

        return AgentPlan(
            plannerVersion=PLANNER_VERSION,
            intent=intent,
            taskType=task_type,
            taskRoute=task_route,
            writeIntent=self._write_intent(requires_patch, read_only, task_type),
            requiresPoiResolution=requires_poi_resolution,
            requiresPatch=requires_patch,
            requiresRollback=rollback_intent,
            requiresRealtimeInfo=requires_realtime_info,
            readOnly=read_only,
            allowedTools=self._allowed_tools(
                task_type, requires_patch, requires_poi_resolution, requires_realtime_info
            ),
            toolStrategy=self._tool_strategy(task_type, requires_patch, requires_realtime_info),
            qualityFocus=self._quality_focus(task_type, text),
            hasCurrentItinerary=has_itinerary,
            hasPendingPoiCandidates=has_pending_pois,
            expectedHarnessStages=expected_stages,
            riskControls=risk_controls,
            items=items,
        ).to_context()

    def _has_any(self, text: str, keywords: list[str]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def _task_type(
        self,
        has_itinerary: bool,
        has_pending_pois: bool,
        needs_clarification: bool,
        rollback_intent: bool,
        comparison_intent: bool,
        source_intent: bool,
        route_intent: bool,
        poi_intent: bool,
        patch_intent: bool,
        initial_arrange_intent: bool = False,
    ) -> str:
        if rollback_intent:
            return "rollback"
        if needs_clarification:
            return "clarification"
        if comparison_intent:
            return "plan_comparison"
        if has_pending_pois:
            return "local_modification"
        if route_intent and has_itinerary:
            return "route_optimization"
        if poi_intent and has_itinerary and patch_intent:
            return "poi_grounding"
        if patch_intent:
            return "local_modification"
        if initial_arrange_intent and not has_itinerary:
            return "initial_planning"
        if source_intent:
            return "ticket_source_lookup"
        if has_itinerary:
            return "local_modification"
        return "initial_planning"

    def _legacy_intent(self, task_type: str, has_itinerary: bool, patch_intent: bool) -> str:
        if task_type == "clarification":
            return "clarification"
        if task_type == "rollback":
            return "rollback"
        if task_type == "initial_planning":
            return "draft_itinerary"
        if task_type == "plan_comparison":
            return "comparison"
        if task_type == "ticket_source_lookup":
            return "source_lookup"
        return "patch" if has_itinerary or patch_intent else "draft_itinerary"

    def _task_route(
        self, task_type: str, requires_patch: bool, requires_poi_resolution: bool, requires_realtime_info: bool
    ) -> str:
        route = [task_type]
        if requires_realtime_info:
            route.append("ground_realtime_facts")
        if requires_poi_resolution:
            route.append("ground_poi")
        if requires_patch:
            route.append("versioned_patch")
            route.append("basic_verifier")
        elif task_type == "plan_comparison":
            route.append("read_only_compare")
        return " -> ".join(route)

    def _expected_stages(
        self,
        task_type: str,
        requires_patch: bool,
        requires_poi_resolution: bool,
        requires_realtime_info: bool,
    ) -> list[str]:
        if task_type == "clarification":
            return ["plan", "clarify", "respond"]
        if task_type == "rollback":
            return ["plan", "restore_version", "verify", "respond"]
        stages = ["plan"]
        if task_type in {"local_modification", "route_optimization", "plan_comparison", "ticket_source_lookup"}:
            stages.append("read_itinerary")
        if requires_realtime_info:
            stages.append("ground_sources")
        if requires_poi_resolution:
            stages.append("resolve_poi")
        if task_type == "plan_comparison":
            stages.extend(["compare", "respond"])
            return stages
        if requires_patch:
            stages.extend(["apply_patch", "verify"])
        stages.append("respond")
        return stages

    def _write_intent(self, requires_patch: bool, read_only: bool, task_type: str) -> str:
        if requires_patch:
            return "versioned_patch"
        if task_type == "clarification":
            return "no_write_until_clarified"
        if read_only:
            return "read_only_comparison" if task_type == "plan_comparison" else "read_only_lookup"
        return "no_write_until_clarified"

    def _allowed_tools(
        self,
        task_type: str,
        requires_patch: bool,
        requires_poi_resolution: bool,
        requires_realtime_info: bool,
    ) -> list[str]:
        tools: list[str] = []
        if task_type in {"local_modification", "route_optimization", "plan_comparison", "ticket_source_lookup"}:
            tools.append("read_itinerary")
        if requires_realtime_info:
            tools.extend(["web_search", "ticket_lookup", "amap_weather"])
        if requires_poi_resolution:
            tools.append("resolve_poi")
        if task_type == "plan_comparison":
            tools.append("generate_plan_comparison")
        if requires_patch:
            tools.append("patch_itinerary")
        return list(dict.fromkeys(tools))

    def _tool_strategy(self, task_type: str, requires_patch: bool, requires_realtime_info: bool) -> dict[str, Any]:
        return {
            "mode": "read_only"
            if task_type in {"plan_comparison", "ticket_source_lookup"} and not requires_patch
            else "write_if_needed",
            "mustPersistItinerary": requires_patch,
            "initialPlanningUsesReplaceItinerary": task_type == "initial_planning",
            "modificationsUseMinimalPatch": task_type in {"local_modification", "poi_grounding", "route_optimization"},
            "comparisonsAreReadOnlyByDefault": task_type == "plan_comparison",
            "realtimeFactsRequireTools": requires_realtime_info,
            "fallbackFactsNeedVerificationNotes": True,
        }

    def _quality_focus(self, task_type: str, text: str) -> list[str]:
        focus = ["day_theme", "practical_notes", "transport_cost_duration"]
        if any(marker in text for marker in ("轻松", "不赶", "慢", "亲子", "老人")):
            focus.extend(["relaxed_pace", "rest_buffers"])
        if task_type in {"initial_planning", "route_optimization"}:
            focus.append("avoid_overpacking")
        if task_type == "ticket_source_lookup":
            focus.append("reservation_risk_transparency")
        return list(dict.fromkeys(focus))

    def _items(
        self,
        requires_poi_resolution: bool,
        requires_patch: bool,
        has_pending_pois: bool,
        rollback_intent: bool,
        source_intent: bool,
        needs_clarification: bool,
        comparison_intent: bool,
        route_intent: bool,
    ) -> list[AgentPlanItem]:
        items = [
            AgentPlanItem(
                goal="Build request context from active conversation, itinerary, preference memory, and selected skills.",
                successCriteria="Context includes active turns only, current itinerary snapshot, memoryText, selectedSkills, and supported patch schema.",
                requiresTool=None,
            )
        ]
        if needs_clarification:
            items.append(
                AgentPlanItem(
                    goal="Clarify missing trip requirements before itinerary write.",
                    successCriteria="Assistant asks focused questions and does not create itinerary_patches or itinerary_versions.",
                    requiresTool=None,
                )
            )
        if rollback_intent:
            items.append(
                AgentPlanItem(
                    goal="Restore the single active branch before regenerating.",
                    successCriteria="Superseded turns are excluded and restored itinerary version is used as the generation base.",
                    requiresTool="conversation_service",
                )
            )
        if has_pending_pois:
            items.append(
                AgentPlanItem(
                    goal="Ask the user to confirm pending AMap POI candidates before writing final itinerary.",
                    successCriteria="No final itinerary segment is created from unresolved candidates.",
                    requiresTool="poi_resolution_service",
                )
            )
        elif requires_poi_resolution:
            items.append(
                AgentPlanItem(
                    goal="Ground all final POIs through AMap before itinerary write.",
                    successCriteria="Every persisted segment POI has amapId, coordinates, source, and confidence.",
                    requiresTool="poi_resolution_service",
                )
            )
        if requires_patch:
            items.append(
                AgentPlanItem(
                    goal="Apply itinerary changes only through a versioned patch.",
                    successCriteria="Accepted write creates itinerary_patches and itinerary_versions with activeVersionId updated.",
                    requiresTool="itinerary_patch_service",
                )
            )
        if source_intent:
            items.append(
                AgentPlanItem(
                    goal="Check ticket, reservation, weather, or source transparency before advising.",
                    successCriteria="Provider failures are surfaced with fallbackUsed/failureReason and no fake official source.",
                    requiresTool="travel_tool_registry",
                )
            )
        if comparison_intent:
            items.append(
                AgentPlanItem(
                    goal="Generate itinerary comparison as a read-only recommendation unless the user explicitly asks to apply one option.",
                    successCriteria="No itinerary patch/version is created by comparison-only requests.",
                    requiresTool="plan_comparison_service",
                )
            )
        if route_intent:
            items.append(
                AgentPlanItem(
                    goal="Check route pace and transport feasibility before changing the timeline.",
                    successCriteria="Route optimization keeps feasible visit count, transport preference, and rest buffers visible.",
                    requiresTool="route_service",
                )
            )
        items.append(
            AgentPlanItem(
                goal="Verify hard safety constraints before returning the assistant response.",
                successCriteria="Verifier passes AMap grounding, pending POI exclusion, patch/version invariant, and source transparency checks.",
                requiresTool="agent_verifier_service",
            )
        )
        return items
