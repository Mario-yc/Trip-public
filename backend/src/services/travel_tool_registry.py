import json
import re
import sqlite3
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, get_args
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from src.api.schemas.itinerary_patches import ItineraryPatchOperation, ItineraryPatchOperationName
from src.api.schemas.maps import MapPoiResolveQueryRequest, MapPoiResolveRequest
from src.providers.travel_tools import ResilientAmapWeatherProvider, ResilientWebSearchProvider
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_service import ItineraryService
from src.services.agent_run_control import assert_session_run_active
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.poi_resolution_service import PoiResolutionService
from src.services.preference_service import PreferenceService
from src.services.tool_schema_compiler import STRICT_NULL_NUMBER_SENTINEL
from src.services.visit_duration_policy import VisitDurationPolicy
from src.services.weather_service import WeatherService
from src.services.trip_date_resolver import TripDateResolver
from src.services.web_search_evidence_service import WebSearchEvidenceService

PATCH_OPERATION_NAMES = list(get_args(ItineraryPatchOperationName))


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
OutputSummarizer = Callable[[dict[str, Any]], dict[str, Any]]
ToolEventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class TravelToolMetadata:
    read_only: bool
    mutates_itinerary: bool = False
    requires_network: bool = False
    concurrent_safe: bool = False
    max_output_chars: int = 12000
    provider_dependencies: tuple[str, ...] = ()
    truthfulness_policy: str = "Return explicit failure/fallback metadata; never fabricate live travel data."

    def to_event_metadata(self) -> dict[str, Any]:
        return {
            "readOnly": self.read_only,
            "mutatesItinerary": self.mutates_itinerary,
            "requiresNetwork": self.requires_network,
            "concurrentSafe": self.concurrent_safe,
            "providerDependencies": list(self.provider_dependencies),
            "truthfulnessPolicy": self.truthfulness_policy,
        }


@dataclass(frozen=True)
class TravelToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    metadata: TravelToolMetadata
    output_summarizer: Optional[OutputSummarizer] = None

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class WebSearchToolArgs(BaseModel):
    query: str
    count: int = Field(default=5, ge=1, le=10)
    freshness: str = "noLimit"


class AmapWeatherToolArgs(BaseModel):
    city: str
    date: Optional[str] = None
    purposeTags: list[str] = Field(default_factory=list)


class TicketLookupToolArgs(BaseModel):
    poiName: str
    city: str
    date: Optional[str] = None


class ResolvePoiToolArgs(BaseModel):
    city: Optional[str] = None
    queries: list[MapPoiResolveQueryRequest]


class PatchItineraryToolArgs(BaseModel):
    operations: list[ItineraryPatchOperation]
    baseVersionId: Optional[str] = None


class SavePreferenceMemoryToolArgs(BaseModel):
    memoryText: str
    structuredMemory: Optional[dict[str, Any]] = None
    autoUpdateEnabled: Optional[bool] = None


class GenerateComparisonToolArgs(BaseModel):
    city: Optional[str] = None


PATCH_ITINERARY_SEGMENT_POI_REQUIRED_FEEDBACK = (
    "fullItinerary.days[].segments[].poi is required. "
    "Do not put source/latitude/longitude/confidence directly on segment. "
    "Use segment.poi.name/source/sourceNote/confidence/latitude/longitude."
)

PATCH_ITINERARY_NUMERIC_FIELDS_FEEDBACK = (
    "patch_itinerary numeric fields must be JSON numbers or null, not strings. "
    "Use latitude=null and longitude=null for agent-text-timeline POIs; use numeric values for "
    "budgetEstimate, totalEstimatedCost, estimatedCost, confidence, and grounded AMap coordinates."
)


class TravelToolRegistry:
    def __init__(
        self,
        db: sqlite3.Connection,
        session: sqlite3.Row,
        request_context: dict[str, Any],
        source_turn_id: Optional[str] = None,
        event_sink: Optional[ToolEventSink] = None,
        allowed_tool_names: Optional[set[str]] = None,
    ):
        self.db = db
        self.session = session
        self.request_context = request_context
        self.source_turn_id = source_turn_id
        self.event_sink = event_sink
        self.allowed_tool_names = frozenset(allowed_tool_names) if allowed_tool_names is not None else None
        self.events: list[dict[str, Any]] = []
        self._resolved_amap_pois: dict[str, dict[str, Any]] = {}
        self._tool_defs = self._build_tool_defs()

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [tool_def.to_openai_tool() for tool_def in self._tool_defs.values()]

    def registered_tools(self) -> dict[str, TravelToolDef]:
        return dict(self._tool_defs)

    def tool_metadata(self, tool_name: str) -> Optional[TravelToolMetadata]:
        tool_def = self._tool_defs.get(tool_name)
        return tool_def.metadata if tool_def is not None else None

    def _build_tool_defs(self) -> dict[str, TravelToolDef]:
        metadata_by_name = {
            "web_search": TravelToolMetadata(
                read_only=True,
                requires_network=True,
                concurrent_safe=True,
                provider_dependencies=("web_search",),
            ),
            "amap_weather": TravelToolMetadata(
                read_only=True,
                requires_network=True,
                concurrent_safe=True,
                provider_dependencies=("amap_weather",),
            ),
            "ticket_lookup": TravelToolMetadata(
                read_only=True,
                requires_network=True,
                concurrent_safe=True,
                provider_dependencies=("ticket_lookup", "web_search"),
            ),
            "resolve_poi": TravelToolMetadata(
                read_only=False,
                requires_network=True,
                concurrent_safe=False,
                provider_dependencies=("amap_poi",),
            ),
            "read_itinerary": TravelToolMetadata(read_only=True, max_output_chars=20000),
            "patch_itinerary": TravelToolMetadata(read_only=False, mutates_itinerary=True, max_output_chars=16000),
            "read_preference_memory": TravelToolMetadata(read_only=True, concurrent_safe=True),
            "save_preference_memory": TravelToolMetadata(read_only=False, concurrent_safe=False),
            "generate_plan_comparison": TravelToolMetadata(read_only=True, max_output_chars=16000),
        }
        tool_defs: dict[str, TravelToolDef] = {}
        for definition in self._legacy_tool_definitions():
            function = definition["function"]
            name = str(function["name"])
            tool_defs[name] = TravelToolDef(
                name=name,
                description=str(function["description"]),
                parameters=function["parameters"],
                handler=lambda arguments, tool_name=name: self._execute(tool_name, arguments),
                metadata=metadata_by_name[name],
                output_summarizer=lambda output, tool_name=name: self._output_preview(tool_name, output),
            )
        return tool_defs

    def _nullable_schema(self, schema_type: str) -> dict[str, Any]:
        return {"anyOf": [{"type": schema_type}, {"type": "null"}]}

    def _full_itinerary_schema(self) -> dict[str, Any]:
        agent_text_poi_schema = {
            "type": "object",
            "description": (
                "Required on every fullItinerary day segment. For ungrounded model-chosen drafts, "
                "set source='agent-text-timeline', latitude=null, longitude=null, confidence<=0.45, "
                "and sourceNote set to a short Chinese note: 高德 POI 待校验."
            ),
            "properties": {
                "id": {"type": "string"},
                "amapId": self._nullable_schema("string"),
                "name": {"type": "string"},
                "type": {"type": "string"},
                "city": {"type": "string"},
                "district": {"type": "string"},
                "address": {"type": "string"},
                "category": {"type": "string"},
                "latitude": self._nullable_schema("number"),
                "longitude": self._nullable_schema("number"),
                "photoUrl": self._nullable_schema("string"),
                "source": {"type": "string"},
                "sourceNote": {"type": "string"},
                "sourceUrl": self._nullable_schema("string"),
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["name", "city", "category", "source", "sourceNote", "confidence", "latitude", "longitude"],
        }
        segment_schema = {
            "type": "object",
            "description": (
                "A timeline segment. Put POI evidence fields under segment.poi; do not put "
                "source/latitude/longitude/confidence directly on this segment object."
            ),
            "properties": {
                "id": {"type": "string"},
                "startTime": {"type": "string", "description": "HH:mm"},
                "endTime": {"type": "string", "description": "HH:mm"},
                "kind": {"type": "string"},
                "poi": agent_text_poi_schema,
                "transportMode": {"type": "string"},
                "estimatedCost": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["startTime", "endTime", "poi", "transportMode", "estimatedCost", "notes"],
        }
        return {
            "type": "object",
            "description": "Complete replacement itinerary used only with op='replace_itinerary'.",
            "properties": {
                "title": {"type": "string"},
                "city": {"type": "string"},
                "templateType": {"type": "string"},
                "budgetTarget": {
                    **self._nullable_schema("number"),
                    "description": (
                        "Optional numeric budget target. Do not write Chinese budget tiers such as 中等/低/高 here; "
                        "keep those user-facing budget-tier words in budgetDeltaExplanation, decisionRationale, or notes."
                    ),
                },
                "budgetEstimate": {"type": "number"},
                "budgetDeltaExplanation": {"type": "string"},
                "decisionRationale": {"type": "string"},
                "status": {"type": "string"},
                "days": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "dayNumber": {"type": "integer"},
                            "date": self._nullable_schema("string"),
                            "title": {"type": "string"},
                            "weatherSummary": {"type": "string"},
                            "riskSummary": {"type": "string"},
                            "totalEstimatedCost": {"type": "number"},
                            "segments": {"type": "array", "items": segment_schema},
                        },
                        "required": ["dayNumber", "title", "segments"],
                    },
                },
            },
            "required": ["title", "city", "days"],
        }

    def _amap_poi_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "A grounded AMap POI copied exactly from resolve_poi or a persisted selected candidate record. "
                "Invented ids, names, or coordinates are rejected by the provenance guard."
            ),
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "type": {"type": "string"},
                "city": {"type": "string"},
                "district": {"type": "string"},
                "address": {"type": "string"},
                "longitude": {"type": "number"},
                "latitude": {"type": "number"},
                "category": {"type": "string"},
                "source": {"type": "string", "enum": [AMAP_PLACE_SOURCE]},
                "sourceNote": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
                "photos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                        "required": ["title", "url"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "id",
                "name",
                "type",
                "city",
                "district",
                "address",
                "longitude",
                "latitude",
                "category",
                "source",
                "sourceNote",
                "confidence",
                "photos",
            ],
            "additionalProperties": False,
        }

    def _patch_operation_variants(self) -> list[dict[str, Any]]:
        string = {"type": "string"}
        segment_id = {"type": "string", "description": "Existing target timeline segment id."}
        day_id = {"type": "string", "description": "Existing target itinerary day id."}

        def variant(op: str, properties: dict[str, Any]) -> dict[str, Any]:
            all_properties = {"op": {"type": "string", "enum": [op]}, **properties}
            return {
                "type": "object",
                "properties": all_properties,
                "required": list(all_properties),
                "additionalProperties": False,
            }

        variants = [
            variant("replace_itinerary", {"fullItinerary": self._full_itinerary_schema()}),
            variant("replace_trip_title", {"value": string}),
            variant("replace_day_title", {"dayId": day_id, "value": string}),
            variant(
                "replace_segment_start_time",
                {"segmentId": segment_id, "startTime": {"type": "string", "description": "HH:mm"}},
            ),
            variant(
                "replace_segment_duration",
                {"segmentId": segment_id, "durationMinutes": {"type": "integer", "minimum": 1, "maximum": 720}},
            ),
            variant("replace_transport_mode", {"segmentId": segment_id, "value": string}),
            variant("add_day", {"title": string}),
            variant(
                "add_segment",
                {
                    "dayId": day_id,
                    "startTime": {"type": "string", "description": "HH:mm"},
                    "title": string,
                    "kind": {
                        "type": "string",
                        "enum": ["meal", "rest", "note", "buffer", "visit", "activity", "area_walk"],
                    },
                    "durationMinutes": {"type": "integer", "minimum": 15, "maximum": 720},
                    "estimatedCost": {"type": "number"},
                    "transportMode": string,
                    "amapPoi": self._amap_poi_schema(),
                },
            ),
            variant("reorder_segments", {"dayId": day_id, "orderedSegmentIds": {"type": "array", "items": string}}),
            variant("replace_segment_poi", {"segmentId": segment_id, "amapPoi": self._amap_poi_schema()}),
            variant(
                "replace_segment_poi_from_candidate",
                {"segmentId": segment_id, "candidateId": string, "amapPoi": self._amap_poi_schema()},
            ),
            variant(
                "expand_area_poi_candidates",
                {"segmentId": segment_id, "radius": {"type": "integer", "minimum": 50, "maximum": 5000}},
            ),
            variant(
                "expand_meal_poi_candidates",
                {"segmentId": segment_id, "radius": {"type": "integer", "minimum": 50, "maximum": 5000}},
            ),
            variant("confirm_poi_anchor", {"segmentId": segment_id}),
            variant("refresh_ticket_for_segment", {"segmentId": segment_id}),
            variant("refresh_routes_for_day", {"dayId": day_id}),
            variant("remove_segment", {"segmentId": segment_id}),
            variant(
                "move_segment",
                {
                    "segmentId": segment_id,
                    "targetDayId": day_id,
                    "startTime": {"type": "string", "description": "HH:mm"},
                },
            ),
            variant("update_segment_notes", {"segmentId": segment_id, "notes": string}),
        ]
        assert {item["properties"]["op"]["enum"][0] for item in variants} == set(PATCH_OPERATION_NAMES)
        return variants

    def _legacy_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "查询景点开放时间、预约规则、官方预约入口或公开补充来源。使用当前配置的联网搜索 provider，默认多源免费 HTML 搜索并按可信度排序。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "count": {"type": "integer", "minimum": 1, "maximum": 10},
                            "freshness": {
                                "type": "string",
                                "enum": ["noLimit", "oneYear", "oneMonth", "oneWeek", "oneDay"],
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "amap_weather",
                    "description": "查询城市天气，并判断天气对用户明确提及的拍照、户外、亲子、老人同行或步行强度等旅行目的的影响。purposeTags 只能包含用户消息或已保存偏好里明确出现的标签。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "date": {"type": "string"},
                            "purposeTags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ticket_lookup",
                    "description": "查询景点门票、预约、开放时间和外部入口。内部使用当前配置的联网搜索 provider。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "poiName": {"type": "string"},
                            "city": {"type": "string"},
                            "date": {"type": "string"},
                        },
                        "required": ["poiName", "city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_poi",
                    "description": "通过高德 POI 解析地点名称。resolved POI 可作为 amapPoi 写入；pending 必须停止写入并让用户选择，不得构造 AMap POI 或用文本草稿冒充该具体地点。仅 provider failure 且没有具体 pending selection group 时，才可按后续 patch_itinerary 约束保留通用 agent-text-timeline 草稿。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "queries": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "category": {"type": "string"},
                                        "near": {
                                            "type": "object",
                                            "properties": {
                                                "longitude": {"type": "number"},
                                                "latitude": {"type": "number"},
                                                "radius": {"type": "integer"},
                                            },
                                            "required": ["longitude", "latitude"],
                                        },
                                    },
                                    "required": ["name"],
                                },
                            },
                        },
                        "required": ["queries"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_itinerary",
                    "description": "读取当前 active itinerary、activeVersionId 和 timeline context。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "patch_itinerary",
                    "description": (
                        "提交结构化 itinerary patch。模型不能直接写数据库，必须调用这个工具。"
                        "生成全新时间轴时使用 replace_itinerary + fullItinerary。"
                        "resolve_poi 返回 pending 时必须停止写入并让用户选择；不得构造 AMap POI，也不得用文本草稿冒充该具体地点。"
                        "仅全新通用规划且没有具体 pending selection group 时，才可写入 agent-text-timeline 草稿 POI："
                        "source=agent-text-timeline，latitude/longitude=null，confidence<=0.45。"
                        "明确实体使用 exact_entity；大范围区域只能作为 routeable_anchor 并提示用户核对；"
                        "午餐/晚餐/夜景/休息/购物等功能型地点必须先拆成 PoiIntent（rawNeed/city/dayNumber/timeWindow/"
                        "intentType/specificity/searchQueries/preferredTypes/rejectedTypes/selectionRules/askUserOnlyIf），"
                        "再写入可 grounding 的草稿地点；服务端会用高德候选检索和 ranker 自动选择，不能把功能词当最终 POI。"
                        "区域/功能型 POI 可用 expand_area_poi_candidates 展开附近真实高德候选；"
                        "用户确认锚点时用 confirm_poi_anchor；从候选替换具体地点时用 replace_segment_poi_from_candidate 并传 candidateId；"
                        "单段票务重查用 refresh_ticket_for_segment；整日路线重查用 refresh_routes_for_day。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "baseVersionId": self._nullable_schema("string"),
                            "operations": {
                                "type": "array",
                                "items": {"anyOf": self._patch_operation_variants()},
                            },
                        },
                        "required": ["operations"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_preference_memory",
                    "description": "读取当前用户长期偏好摘要，只返回过滤后的实际偏好内容。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_preference_memory",
                    "description": "当用户明确修改、确认或补充长期偏好时，保存自由文本偏好摘要。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "memoryText": {"type": "string"},
                        },
                        "required": ["memoryText"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_plan_comparison",
                    "description": "基于当前 itinerary 状态生成低预算、拍照优先、轻松不赶路三个方案的比较摘要。",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            },
        ]

    def execute(self, tool_call_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert_session_run_active(str(self.session["id"]))
        tool_def = self._tool_defs.get(tool_name)
        if tool_def is None or (self.allowed_tool_names is not None and tool_name not in self.allowed_tool_names):
            reason = (
                f"Tool not permitted by this action: {tool_name}"
                if tool_def is not None
                else f"Unregistered tool: {tool_name}"
            )
            now = datetime.now(timezone.utc).isoformat()
            self._record_event(
                {
                    "id": tool_call_id or f"tool_{uuid4().hex[:12]}",
                    "toolName": tool_name,
                    "type": "tool",
                    "label": tool_name,
                    "status": "failed",
                    "inputSummary": self._input_summary(arguments),
                    "outputSummary": reason,
                    "providerName": "agent-tool-registry",
                    "fallbackUsed": False,
                    "failureReason": reason,
                    "startedAt": now,
                    "finishedAt": now,
                    "timestamp": now,
                    "detail": reason,
                    "metadata": {"toolMetadata": {}},
                }
            )
            return {"ok": False, "toolName": tool_name, "error": reason}
        arguments = self._normalize_tool_arguments(tool_name, arguments)
        deadline = self.request_context.get("runtimeDeadlineMonotonic")
        if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
            return {
                "ok": False,
                "toolName": tool_name,
                "error": "Agent run deadline exceeded before tool execution",
                "failureReason": "agent_run_deadline_exceeded",
            }
        started_at = datetime.now(timezone.utc)
        event = {
            "id": tool_call_id or f"tool_{uuid4().hex[:12]}",
            "toolName": tool_name,
            "type": "tool",
            "label": tool_name,
            "status": "running",
            "inputSummary": self._input_summary(arguments),
            "outputSummary": "",
            "providerName": "agent-tool-registry",
            "fallbackUsed": False,
            "failureReason": None,
            "startedAt": started_at.isoformat(),
            "finishedAt": None,
            "timestamp": started_at.isoformat(),
            "detail": "",
            "metadata": {"toolMetadata": tool_def.metadata.to_event_metadata()},
        }
        self._notify_event(dict(event))
        try:
            output = tool_def.handler(arguments)
            assert_session_run_active(str(self.session["id"]))
            max_tool_seconds = int((self.request_context.get("runtimeLimits") or {}).get("maxToolSeconds") or 0)
            if max_tool_seconds and (datetime.now(timezone.utc) - started_at).total_seconds() > max_tool_seconds:
                output = {
                    "providerName": "agent-tool-registry",
                    "fallbackUsed": False,
                    "failureReason": "tool_deadline_exceeded",
                    "userVisibleCaveat": "工具执行超时，未继续后续工具。",
                }
            failed_without_fallback = bool(output.get("failureReason")) and not bool(output.get("fallbackUsed"))
            status = "failed" if failed_without_fallback else "fallback" if output.get("fallbackUsed") else "succeeded"
            event.update(
                {
                    "status": status,
                    "providerName": output.get("providerName") or event["providerName"],
                    "fallbackUsed": bool(output.get("fallbackUsed")),
                    "failureReason": output.get("failureReason"),
                    "outputSummary": self._output_summary(tool_name, output),
                    "detail": self._output_summary(tool_name, output),
                    "metadata": {
                        "toolMetadata": tool_def.metadata.to_event_metadata(),
                        "inputPreview": self._bounded_preview(self._payload_preview(arguments)),
                        "resultPreview": self._bounded_preview(self._output_preview(tool_name, output)),
                    },
                }
            )
            result = {"ok": not failed_without_fallback, "toolName": tool_name, **output}
            if failed_without_fallback:
                result["error"] = str(output.get("failureReason") or "Tool failed.")
            return self._bounded_tool_result(tool_def, result)
        except Exception as error:
            reason = self._friendly_error(error)
            validation_feedback = self._patch_itinerary_validation_feedback(tool_name, arguments, error, reason)
            event.update(
                {
                    "status": "failed",
                    "failureReason": reason,
                    "outputSummary": reason,
                    "detail": reason,
                    "metadata": {
                        "toolMetadata": tool_def.metadata.to_event_metadata(),
                        "inputPreview": self._bounded_preview(self._payload_preview(arguments)),
                        "resultPreview": {
                            "ok": False,
                            "error": reason,
                            **({"validationFeedback": validation_feedback} if validation_feedback else {}),
                        },
                    },
                }
            )
            result = {"ok": False, "toolName": tool_name, "error": reason}
            if validation_feedback:
                result["validationFeedback"] = validation_feedback
            return result
        finally:
            event["finishedAt"] = datetime.now(timezone.utc).isoformat()
            event["timestamp"] = event["finishedAt"]
            self._record_event(event)

    def _filter_unneeded_expand_area_candidates(
        self,
        patch_service: ItineraryPatchService,
        operations: list[ItineraryPatchOperation],
    ) -> tuple[list[ItineraryPatchOperation], list[str]]:
        filtered: list[ItineraryPatchOperation] = []
        skipped_segment_ids: list[str] = []
        for operation in operations:
            if operation.op == "expand_area_poi_candidates" and operation.segment_id:
                segment = patch_service._segment_with_poi(self.session["active_plan_id"], operation.segment_id)
                if segment is not None and self._segment_is_concrete_for_expand_noop(patch_service, segment):
                    skipped_segment_ids.append(operation.segment_id)
                    continue
            filtered.append(operation)
        return filtered, skipped_segment_ids

    def _segment_is_concrete_for_expand_noop(self, patch_service: ItineraryPatchService, segment: Any) -> bool:
        metadata = patch_service._poi_grounding_metadata(segment)
        status = str(metadata.get("groundingStatus") or "")
        specificity = str(metadata.get("poiSpecificity") or "")
        if not bool(metadata.get("routeable")):
            return False
        if status in {"verified_amap", "agent_selected_candidate", "user_confirmed"}:
            return True
        return specificity == "exact_entity" and status not in {
            "draft_only",
            "waiting_for_poi_grounding",
            "routeable_anchor",
            "optional_waiting",
            "not_required",
            "area_poi",
            "functional_poi",
            "composite_poi",
            "area_unresolved",
            "provider_rate_limited",
        }

    def _record_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self._notify_event(event)

    def _notify_event(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event)
        except Exception:
            return

    def _bounded_tool_result(self, tool_def: TravelToolDef, result: dict[str, Any]) -> dict[str, Any]:
        max_output_chars = self._max_tool_output_chars(tool_def)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= max_output_chars:
            return result
        result_preview = (
            tool_def.output_summarizer(result)
            if tool_def.output_summarizer is not None
            else self._output_preview(tool_def.name, result)
        )
        bounded = {
            "ok": result.get("ok"),
            "toolName": result.get("toolName"),
            "providerName": result.get("providerName"),
            "fallbackUsed": bool(result.get("fallbackUsed")),
            "failureReason": result.get("failureReason"),
            "confidence": result.get("confidence"),
            "userVisibleCaveat": result.get("userVisibleCaveat"),
            "summary": self._output_summary(tool_def.name, result),
            "resultPreview": self._bounded_preview(result_preview, max_chars=max(1000, max_output_chars // 2)),
            "truncated": True,
            "originalLength": len(text),
        }
        for key in ("activeVersionId", "query", "city", "date"):
            if key in result:
                bounded[key] = result[key]
        if isinstance(result.get("results"), list):
            bounded["resultCount"] = len(result["results"])
        if "version" in result:
            with_version = {**bounded, "version": result["version"]}
            if len(json.dumps(with_version, ensure_ascii=False, default=str)) <= max_output_chars:
                bounded = with_version
        if len(json.dumps(bounded, ensure_ascii=False, default=str)) > max_output_chars:
            bounded["summary"] = str(bounded.get("summary") or "")[:240]
            bounded["userVisibleCaveat"] = str(bounded.get("userVisibleCaveat") or "")[:240]
            preview_text = json.dumps(result_preview, ensure_ascii=False, default=str)
            bounded["resultPreview"] = {
                "truncated": True,
                "preview": preview_text[: max(120, max_output_chars // 4)],
                "originalLength": len(preview_text),
            }
        return bounded

    def _max_tool_output_chars(self, tool_def: TravelToolDef) -> int:
        runtime_limits = self.request_context.get("runtimeLimits") if isinstance(self.request_context, dict) else None
        runtime_max = None
        if isinstance(runtime_limits, dict):
            try:
                runtime_max = int(runtime_limits.get("maxToolResultChars") or 0)
            except (TypeError, ValueError):
                runtime_max = None
        limits = [tool_def.metadata.max_output_chars]
        if runtime_max and runtime_max > 0:
            limits.append(runtime_max)
        return max(800, min(limits))

    def _execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "web_search":
            args = WebSearchToolArgs.model_validate(arguments)
            result = ResilientWebSearchProvider().search(args.query, count=args.count, freshness=args.freshness)
            return WebSearchEvidenceService.project(result, freshness=args.freshness)
        if tool_name == "amap_weather":
            args = AmapWeatherToolArgs.model_validate(arguments)
            requested_range = self._resolved_trip_date_range()
            weather_context = dict(self.request_context)
            if args.date:
                weather_context["resolvedTripDates"] = (
                    TripDateResolver()
                    .resolve(
                        args.date,
                        source="amapWeatherTool.date",
                    )
                    .to_camel_dict()
                )
            availability = WeatherService().availability_contract(weather_context)
            if availability["status"] == "outside_forecast_window":
                return {
                    "city": args.city,
                    "date": args.date,
                    "weather": "待预报窗口开放",
                    "temperatureRange": "待确认",
                    "wind": "待确认",
                    "humidity": None,
                    "riskLevel": "unknown",
                    "riskReason": "尚未进入该出行日期的天气预报窗口，未调用今日天气。",
                    "sourceName": None,
                    "queriedAt": None,
                    "confidence": 0.0,
                    "providerName": "local-date-guard",
                    "fallbackUsed": False,
                    "failureReason": None,
                    "userVisibleCaveat": f"预计从 {availability.get('forecastAvailableFrom') or '临近出发'} 起可查询真实预报。",
                    "requestedDate": args.date,
                    "requestedDateRange": requested_range,
                    "weatherStatus": "outside_forecast_window",
                    "weatherAvailability": availability,
                }
            result = ResilientAmapWeatherProvider().query(
                args.city,
                travel_date=args.date,
                purpose_tags=args.purposeTags,
                context=self.request_context,
            )
            return {
                "city": result.city,
                "date": result.date,
                "weather": result.weather,
                "temperatureRange": result.temperature_range,
                "wind": result.wind,
                "humidity": result.humidity,
                "riskLevel": result.risk_level,
                "riskReason": result.risk_reason,
                "sourceName": result.source_name,
                "queriedAt": result.queried_at.isoformat(),
                "confidence": result.confidence,
                "providerName": result.provider_name,
                "fallbackUsed": result.fallback_used,
                "failureReason": result.failure_reason,
                "userVisibleCaveat": result.user_visible_caveat,
                "requestedDate": args.date,
                "requestedDateRange": requested_range,
                "weatherStatus": result.failure_reason
                if result.failure_reason == "forecast_not_supported_yet"
                else None,
            }
        if tool_name == "ticket_lookup":
            args = TicketLookupToolArgs.model_validate(arguments)
            query = " ".join(
                item for item in [args.city, args.poiName, args.date or "", "开放时间 访客参观 官方预约入口"] if item
            )
            result = ResilientWebSearchProvider().search(query, count=5, freshness="oneYear")
            return {
                "poiName": args.poiName,
                "city": args.city,
                "date": args.date,
                "query": result.query,
                "results": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "sourceName": item.source_name,
                        "confidence": item.confidence,
                    }
                    for item in result.results
                ],
                "providerName": result.provider_name,
                "fallbackUsed": result.fallback_used,
                "failureReason": result.failure_reason,
                "userVisibleCaveat": result.user_visible_caveat,
                "confidence": result.confidence,
                "queriedAt": result.queried_at.isoformat(),
            }
        if tool_name == "resolve_poi":
            args = ResolvePoiToolArgs.model_validate(arguments)
            response = PoiResolutionService(self.db).resolve(
                MapPoiResolveRequest(
                    sessionId=self.session["id"],
                    turnId=self.source_turn_id,
                    city=args.city or self.session["city"],
                    queries=args.queries,
                ),
                commit=False,
            )
            for item in response.resolved:
                poi_payload = item.poi.model_dump(by_alias=True)
                amap_id = str(poi_payload.get("id") or poi_payload.get("amapId") or "").strip()
                if amap_id:
                    self._resolved_amap_pois[amap_id] = poi_payload
            return {
                "resolved": [item.model_dump(by_alias=True) for item in response.resolved],
                "pending": [item.model_dump(by_alias=True) for item in response.pending],
                "providerName": "amap-poi-resolution",
                "fallbackUsed": False,
                "failureReason": None,
                "userVisibleCaveat": "POI 未确认时保留结构化候选供后续选择；当前不会把未确认候选伪装成已解析地点。",
                "confidence": 0.9 if response.resolved else 0.55 if response.pending else 0.2,
            }
        if tool_name == "read_itinerary":
            return self._read_itinerary()
        if tool_name == "patch_itinerary":
            args = PatchItineraryToolArgs.model_validate(arguments)
            if self._pending_poi_requires_confirmation(args.operations):
                raise ValueError(
                    "pending_poi_requires_confirmation: resolve_poi returned ambiguous candidates; "
                    "use replace_segment_poi_from_candidate after an explicit selection and do not write a text draft"
                )
            self._validate_patch_amap_poi_provenance(args.operations)
            patch_service = ItineraryPatchService(self.db)
            operations, skipped_expand_segment_ids = self._filter_unneeded_expand_area_candidates(
                patch_service, args.operations
            )
            if skipped_expand_segment_ids and not operations:
                return {
                    "activeVersionId": self.session["active_version_id"],
                    "providerName": "sqlite-itinerary-patch",
                    "fallbackUsed": False,
                    "failureReason": None,
                    "userVisibleCaveat": "当前时间轴地点已是具体 POI，无需展开附近候选。",
                    "confidence": 0.95,
                    "skippedOperations": [
                        {
                            "op": "expand_area_poi_candidates",
                            "segmentId": segment_id,
                            "reason": "segment_already_concrete",
                        }
                        for segment_id in skipped_expand_segment_ids
                    ],
                }
            if skipped_expand_segment_ids:
                args = args.model_copy(update={"operations": operations})
            result = patch_service.apply_patch(
                self.session["active_plan_id"],
                args.operations,
                source_type="agent",
                base_version_id=args.baseVersionId or self.session["active_version_id"],
                source_turn_id=self.source_turn_id,
                preference_summary=str(self.request_context.get("currentPreferenceSummary") or ""),
                planning_context=self.request_context,
                server_route_decision_contract=self._server_route_decision_contract(),
            )
            self.session = self.db.execute(
                "SELECT * FROM conversation_sessions WHERE id = ?", (self.session["id"],)
            ).fetchone()
            return {
                "activeVersionId": result.version.id,
                "version": result.version.model_dump(by_alias=True),
                "itinerary": result.itinerary.model_dump(by_alias=True),
                "patch": result.patch.model_dump(by_alias=True),
                "changedSegmentIds": sorted(
                    {operation.segment_id for operation in args.operations if operation.segment_id}
                ),
                "providerName": "sqlite-itinerary-patch",
                "fallbackUsed": False,
                "failureReason": None,
                "userVisibleCaveat": "",
                "confidence": 0.95,
            }
        if tool_name == "read_preference_memory":
            memory = PreferenceService(self.db).get_memory(session_id=self.session["id"], commit=False)
            memory_text = PreferenceService.effective_memory_text(memory.memory_text)
            return {
                "memoryText": memory_text,
                "structuredMemory": memory.structured_memory,
                "compiledRules": memory.compiled_rules,
                "pendingConfirmations": memory.pending_confirmations,
                "autoUpdateEnabled": memory.auto_update_enabled,
                "updatedAt": memory.updated_at,
                "providerName": "sqlite-preference-memory",
                "fallbackUsed": False,
                "failureReason": None,
                "confidence": 0.9 if memory_text else 0.4,
            }
        if tool_name == "save_preference_memory":
            args = SavePreferenceMemoryToolArgs.model_validate(arguments)
            memory = PreferenceService(self.db).update_memory(
                memory_text=args.memoryText,
                structured_memory=args.structuredMemory,
                auto_update_enabled=args.autoUpdateEnabled if args.autoUpdateEnabled is not None else True,
                session_id=self.session["id"],
                commit=False,
            )
            return {
                "memoryText": PreferenceService.effective_memory_text(memory.memory_text),
                "structuredMemory": memory.structured_memory,
                "compiledRules": memory.compiled_rules,
                "pendingConfirmations": memory.pending_confirmations,
                "autoUpdateEnabled": memory.auto_update_enabled,
                "updatedAt": memory.updated_at,
                "providerName": "sqlite-preference-memory",
                "fallbackUsed": False,
                "failureReason": None,
                "confidence": 0.9,
            }
        if tool_name == "generate_plan_comparison":
            args = GenerateComparisonToolArgs.model_validate(arguments)
            current = self._read_itinerary()
            return {
                "city": args.city or self.session["city"],
                "templates": [
                    {
                        "template": "low_budget",
                        "label": "低预算",
                        "basis": "减少交通和高价景点，优先保留免费/低价 POI。",
                    },
                    {"template": "photo_first", "label": "拍照优先", "basis": "优先保留拍照点、天气窗口和白天路线。"},
                    {
                        "template": "relaxed_pace",
                        "label": "轻松不赶路",
                        "basis": "降低单日 POI 数和换乘强度，增加缓冲。",
                    },
                ],
                "currentItinerary": current.get("itinerary"),
                "providerName": "agent-comparison-tool",
                "fallbackUsed": False,
                "failureReason": None,
                "confidence": 0.72,
            }
        raise ValueError(f"Unregistered tool: {tool_name}")

    def _server_route_decision_contract(self) -> Optional[dict[str, Any]]:
        """Lift only the server-built request contract into the write seam.

        Tool arguments never contain planning context, so a model cannot
        provide or replace this value.  The public REST patch endpoint does
        not call this method and therefore cannot turn a client supplied
        ``sourceType`` into a route-policy bypass.
        """
        request_contract = self.request_context.get("requestIntentContract")
        if not isinstance(request_contract, dict):
            return None
        candidate = request_contract.get("routeDecisionContract")
        return deepcopy(candidate) if isinstance(candidate, dict) else None

    def _normalize_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(arguments)
        if tool_name in {"amap_weather", "ticket_lookup", "resolve_poi", "generate_plan_comparison"}:
            normalized["city"] = self._normalized_tool_city(normalized.get("city"))
        if tool_name in {"amap_weather", "ticket_lookup"}:
            date_value = self._context_supported_date(normalized.get("date"))
            if date_value:
                normalized["date"] = date_value
            elif tool_name == "amap_weather":
                resolved_date = self._resolved_trip_start_date()
                if resolved_date:
                    normalized["date"] = resolved_date
                else:
                    normalized.pop("date", None)
            else:
                normalized.pop("date", None)
        if tool_name == "amap_weather":
            normalized["purposeTags"] = self._user_stated_weather_purpose_tags(normalized.get("purposeTags") or [])
        if tool_name == "patch_itinerary":
            normalized = self._normalize_patch_itinerary_arguments(normalized)
        return normalized

    def _validate_patch_amap_poi_provenance(self, operations: list[ItineraryPatchOperation]) -> None:
        """Reject model-invented AMap facts before a versioned write can start."""
        allowed = self._allowed_amap_poi_sources()
        for operation in operations:
            if operation.amap_poi is not None:
                self._assert_amap_poi_matches_source(
                    operation.amap_poi.model_dump(by_alias=True),
                    allowed,
                    candidate_id=operation.candidate_id,
                )
            snapshot = operation.full_itinerary if operation.op == "replace_itinerary" else None
            if not isinstance(snapshot, dict):
                continue
            enforce_snapshot_provenance = bool(self._resolved_amap_pois) or bool(
                self.db.execute(
                    "SELECT 1 FROM amap_poi_candidates WHERE turn_id = ? AND session_id = ? LIMIT 1",
                    (self.source_turn_id, self.session["id"]),
                ).fetchone()
                if self.source_turn_id
                else False
            )
            if not enforce_snapshot_provenance:
                continue
            for day in snapshot.get("days") or []:
                if not isinstance(day, dict):
                    continue
                for segment in day.get("segments") or []:
                    poi = segment.get("poi") if isinstance(segment, dict) else None
                    if not isinstance(poi, dict) or poi.get("source") == "agent-text-timeline":
                        continue
                    self._assert_amap_poi_matches_source(poi, allowed)

    def _pending_poi_requires_confirmation(self, operations: list[ItineraryPatchOperation]) -> bool:
        if any(operation.candidate_id for operation in operations):
            return False
        if not self.source_turn_id:
            return False
        pending = self.db.execute(
            "SELECT 1 FROM amap_poi_candidates WHERE turn_id = ? AND session_id = ? AND status = 'pending' LIMIT 1",
            (self.source_turn_id, self.session["id"]),
        ).fetchone()
        return pending is not None

    def _allowed_amap_poi_sources(self) -> dict[str, dict[str, Any]]:
        allowed = dict(self._resolved_amap_pois)
        current = self._read_itinerary().get("itinerary")
        if isinstance(current, dict):
            for day in current.get("days") or []:
                if not isinstance(day, dict):
                    continue
                for segment in day.get("segments") or []:
                    poi = segment.get("poi") if isinstance(segment, dict) else None
                    if not isinstance(poi, dict):
                        continue
                    amap_id = str(poi.get("amapId") or poi.get("id") or "").strip()
                    if amap_id and poi.get("source") == AMAP_PLACE_SOURCE:
                        allowed.setdefault(amap_id, poi)
        return allowed

    def _assert_amap_poi_matches_source(
        self,
        poi: dict[str, Any],
        allowed: dict[str, dict[str, Any]],
        *,
        candidate_id: Optional[str] = None,
    ) -> None:
        amap_id = str(poi.get("id") or poi.get("amapId") or "").strip()
        source = allowed.get(amap_id)
        if candidate_id:
            candidate_source = self._candidate_poi_source(candidate_id, amap_id)
            if candidate_source is not None:
                source = candidate_source
        if source is None:
            raise ValueError(
                "amap_poi_provenance_error: AMap POI must come from this turn's resolved result, "
                "a persisted selected candidate, or the current itinerary"
            )
        source_name = str(source.get("name") or "").strip()
        if source_name and str(poi.get("name") or "").strip() != source_name:
            raise ValueError("amap_poi_provenance_error: AMap POI name does not match the grounded source")
        for key in ("longitude", "latitude"):
            expected = source.get(key)
            actual = poi.get(key)
            if expected is None:
                continue
            try:
                if abs(float(expected) - float(actual)) > 1e-6:
                    raise ValueError(f"amap_poi_provenance_error: AMap POI {key} does not match the grounded source")
            except (TypeError, ValueError) as error:
                if isinstance(error, ValueError) and str(error).startswith("amap_poi_provenance_error"):
                    raise
                raise ValueError(f"amap_poi_provenance_error: AMap POI {key} is invalid") from error

    def _candidate_poi_source(self, candidate_id: str, amap_id: str) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT candidates_json FROM amap_poi_candidates WHERE id = ? AND session_id = ? AND status = 'pending'",
            (candidate_id, self.session["id"]),
        ).fetchone()
        if row is None:
            return None
        try:
            candidates = json.loads(row["candidates_json"] or "[]")
        except json.JSONDecodeError:
            return None
        return next(
            (
                item
                for item in candidates
                if isinstance(item, dict) and str(item.get("id") or item.get("amapId") or "").strip() == amap_id
            ),
            None,
        )

    def _normalize_patch_itinerary_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        operations = arguments.get("operations")
        if not isinstance(operations, list):
            return arguments
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            self._normalize_operation_amap_poi(operation)
            if operation.get("op") != "replace_itinerary":
                continue
            snapshot = operation.get("fullItinerary")
            if not isinstance(snapshot, dict):
                continue
            self._normalize_full_itinerary_numbers(snapshot)
            self._normalize_flattened_full_itinerary_pois(snapshot)
        return arguments

    def _normalize_operation_amap_poi(self, operation: dict[str, Any]) -> None:
        amap_poi = operation.get("amapPoi")
        if not isinstance(amap_poi, dict):
            return
        source = str(amap_poi.get("source") or "").strip()
        if source == "agent-text-timeline":
            operation.pop("amapPoi", None)
            return
        if source in {"amap", "amap-place", "高德", "高德地图"}:
            amap_poi["source"] = AMAP_PLACE_SOURCE
        self._set_number_if_parseable(amap_poi, "latitude")
        self._set_number_if_parseable(amap_poi, "longitude")
        confidence = self._normalize_number(amap_poi.get("confidence"))
        if confidence is not None:
            amap_poi["confidence"] = confidence
        distance = self._normalize_number(amap_poi.get("distanceMeters"))
        if distance is not None:
            amap_poi["distanceMeters"] = distance

    def _normalize_full_itinerary_numbers(self, snapshot: dict[str, Any]) -> None:
        self._set_nullable_number(snapshot, "budgetTarget")
        self._set_number_if_parseable(snapshot, "budgetEstimate")
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = self._normalize_integer(day.get("dayNumber"))
            if day_number is not None:
                day["dayNumber"] = day_number
            self._set_number_if_parseable(day, "totalEstimatedCost")
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                self._set_number_if_parseable(segment, "estimatedCost")
                self._set_number_if_parseable(segment, "durationMinutes")
                for key in ("latitude", "longitude"):
                    if key in segment:
                        segment[key] = self._normalize_nullable_number(segment.get(key))
                self._set_number_if_parseable(segment, "confidence")
                poi = segment.get("poi")
                if isinstance(poi, dict):
                    for key in ("latitude", "longitude"):
                        if key in poi:
                            poi[key] = self._normalize_nullable_number(poi.get(key))
                    self._set_number_if_parseable(poi, "confidence")
                    self._set_number_if_parseable(poi, "distanceMeters")
                VisitDurationPolicy().normalize_segment_dict(segment, self.request_context)

    def _set_number_if_parseable(self, payload: dict[str, Any], key: str) -> None:
        if key not in payload:
            return
        normalized = self._normalize_number(payload.get(key))
        if normalized is not None:
            payload[key] = normalized

    def _set_nullable_number(self, payload: dict[str, Any], key: str) -> None:
        if key not in payload:
            return
        payload[key] = self._normalize_nullable_number(payload.get(key))

    def _normalize_number(self, value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "nan", "待确认", "未知", "无"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _normalize_integer(self, value: Any) -> Optional[int]:
        number = self._normalize_number(value)
        if number is None:
            return None
        return int(number)

    def _normalize_nullable_number(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == STRICT_NULL_NUMBER_SENTINEL:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "nan", "待确认", "未知", "无"}:
            return None
        return self._normalize_number(value)

    def _normalize_flattened_full_itinerary_pois(self, snapshot: dict[str, Any]) -> None:
        default_city = snapshot.get("city") or self.session["city"]
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict) or isinstance(segment.get("poi"), dict):
                    continue
                poi_name = segment.get("poiName") or segment.get("name") or segment.get("title")
                if not str(poi_name or "").strip():
                    continue
                poi: dict[str, Any] = {
                    "id": segment.get("poiId") or f"poi_{uuid4().hex[:12]}",
                    "name": str(poi_name).strip(),
                    "city": segment.get("city") or default_city,
                    "category": segment.get("category") or segment.get("type") or "",
                    "latitude": self._normalize_nullable_number(segment.get("latitude")),
                    "longitude": self._normalize_nullable_number(segment.get("longitude")),
                    "source": segment.get("source"),
                    "sourceNote": segment.get("sourceNote") or segment.get("source_note") or "",
                    "confidence": self._normalize_number(segment.get("confidence")),
                }
                if poi["confidence"] is None:
                    poi.pop("confidence")
                amap_id = segment.get("amapId") or segment.get("amap_id")
                if amap_id is not None:
                    poi["amapId"] = amap_id
                for key in ("district", "address", "photos", "photoUrl", "sourceUrl"):
                    if key in segment:
                        poi[key] = segment[key]
                segment["poi"] = poi
                for key in (
                    "poiId",
                    "poiName",
                    "city",
                    "category",
                    "latitude",
                    "longitude",
                    "source",
                    "sourceNote",
                    "source_note",
                    "confidence",
                    "amapId",
                    "amap_id",
                    "district",
                    "address",
                    "photos",
                    "photoUrl",
                    "sourceUrl",
                ):
                    segment.pop(key, None)

    def _normalized_tool_city(self, raw_city: Any) -> str:
        city = str(raw_city or "").strip()
        if not city:
            return str(self.session["city"] or "").strip()
        if city == self.session["city"] or self._context_supports_value(city):
            return city
        return str(self.session["city"] or "").strip()

    def _context_supported_date(self, raw_date: Any) -> Optional[str]:
        if raw_date is None:
            return None
        date_text = str(raw_date).strip()
        if not date_text:
            return None
        try:
            parsed = datetime.strptime(date_text[:10], "%Y-%m-%d").date()
        except ValueError:
            return date_text if self._context_supports_value(date_text) else None
        if parsed < date.today():
            return None
        tokens = {
            parsed.isoformat(),
            f"{parsed.month}月{parsed.day}日",
            f"{parsed.month}/{parsed.day}",
            f"{parsed.month}.{parsed.day}",
        }
        return parsed.isoformat() if any(self._context_supports_value(token) for token in tokens) else None

    def _resolved_trip_start_date(self) -> Optional[str]:
        resolved = self.request_context.get("resolvedTripDates")
        if not isinstance(resolved, dict) or resolved.get("status") != "resolved":
            return None
        value = resolved.get("startDate") or resolved.get("start_date")
        return str(value) if value else None

    def _resolved_trip_date_range(self) -> Optional[dict[str, Any]]:
        resolved = self.request_context.get("resolvedTripDates")
        if not isinstance(resolved, dict) or resolved.get("status") != "resolved":
            return None
        return {
            "startDate": resolved.get("startDate") or resolved.get("start_date"),
            "endDate": resolved.get("endDate") or resolved.get("end_date"),
            "dates": resolved.get("dates") or [],
            "datePrecision": resolved.get("datePrecision") or resolved.get("date_precision"),
            "holidayName": resolved.get("holidayName") or resolved.get("holiday_name"),
        }

    def _context_supports_value(self, value: str) -> bool:
        normalized_value = re.sub(r"\s+", "", str(value or ""))
        if not normalized_value:
            return False
        normalized_context = re.sub(r"\s+", "", self._tool_argument_context_text())
        return normalized_value in normalized_context

    def _tool_argument_context_text(self) -> str:
        values = [
            self.request_context.get("latestUserMessage"),
            self.request_context.get("currentUserMessage"),
            self.request_context.get("currentPreferenceSummary"),
            self.request_context.get("memoryText"),
            self.request_context.get("currentItinerarySnapshot"),
            self.request_context.get("timelineContext"),
            self.request_context.get("resolvedTripDates"),
        ]
        return "\n".join(json.dumps(value, ensure_ascii=False, default=str) for value in values if value)

    def _user_stated_weather_purpose_tags(self, requested_tags: list[Any]) -> list[str]:
        context_text = "\n".join(
            str(value or "")
            for value in [
                self.request_context.get("latestUserMessage"),
                self.request_context.get("currentUserMessage"),
                self.request_context.get("currentPreferenceSummary"),
                self.request_context.get("memoryText"),
            ]
        )
        tag_aliases = {
            "拍照": ["拍照", "拍照优先", "打卡", "出片", "摄影"],
            "拍照优先": ["拍照优先", "拍照", "打卡", "出片", "摄影"],
            "户外": ["户外", "室外", "露天", "徒步", "爬山"],
            "亲子": ["亲子", "带娃", "小朋友", "孩子", "儿童"],
            "老人": ["老人", "父母", "长辈", "老人同行"],
            "老人同行": ["老人同行", "老人", "父母", "长辈"],
            "步行": ["步行", "走路", "徒步", "步行强度"],
            "徒步": ["徒步", "爬山", "远足"],
            "夜景": ["夜景", "夜游", "晚上拍照"],
        }
        filtered: list[str] = []
        for raw_tag in requested_tags:
            tag = str(raw_tag or "").strip()
            if not tag or tag in filtered:
                continue
            aliases = tag_aliases.get(tag, [tag])
            if any(alias and self._has_affirmative_mention(context_text, alias) for alias in aliases):
                filtered.append(tag)
        return filtered

    def _has_affirmative_mention(self, text: str, alias: str) -> bool:
        index = text.find(alias)
        while index >= 0:
            prefix = text[max(0, index - 8) : index]
            if not any(marker in prefix for marker in ["没有", "不带", "不要", "不想", "无需", "非", "无"]):
                return True
            index = text.find(alias, index + len(alias))
        return False

    def _clear_same_turn_pending_poi_candidates(self, pending_items: list[Any]) -> None:
        if not pending_items:
            return
        candidate_ids = [
            item.candidate_record_id for item in pending_items if getattr(item, "candidate_record_id", None)
        ]
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            self.db.execute(
                f"DELETE FROM amap_poi_candidates WHERE id IN ({placeholders}) AND status = 'pending'",
                tuple(candidate_ids),
            )
            return
        if self.source_turn_id:
            self.db.execute(
                "DELETE FROM amap_poi_candidates WHERE turn_id = ? AND status = 'pending'",
                (self.source_turn_id,),
            )

    def _read_itinerary(self) -> dict[str, Any]:
        itinerary = None
        if self.session["active_version_id"]:
            itinerary = ItinerarySnapshotService(self.db).capture_snapshot(self.session["active_plan_id"])
        return {
            "activePlanId": self.session["active_plan_id"],
            "activeVersionId": self.session["active_version_id"],
            "city": self.session["city"],
            "itinerary": itinerary,
            "timelineContext": self.request_context.get("timelineContext") or itinerary,
            "providerName": "sqlite-itinerary",
            "fallbackUsed": False,
            "failureReason": None,
            "confidence": 0.9 if itinerary else 0.4,
        }

    def _friendly_error(self, error: Exception) -> str:
        if isinstance(error, ValidationError):
            return "工具参数不符合 schema：" + "; ".join(
                f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
                for item in error.errors()[:3]
            )
        if isinstance(error, HTTPException):
            return str(error.detail)
        return str(error)

    def _patch_itinerary_validation_feedback(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        error: Exception,
        reason: str,
    ) -> Optional[dict[str, Any]]:
        if tool_name != "patch_itinerary":
            return None
        validation_errors: list[str] = []
        if isinstance(error, HTTPException) and isinstance(error.detail, dict):
            raw_errors = error.detail.get("validationErrors")
            if isinstance(raw_errors, list):
                validation_errors = [str(item) for item in raw_errors]
        if isinstance(error, ValidationError) and self._has_numeric_validation_error(error):
            return {
                "message": PATCH_ITINERARY_NUMERIC_FIELDS_FEEDBACK,
                "requiredPath": "operations[].fullItinerary numeric fields and operations[].amapPoi coordinates",
                "retryInstruction": (
                    "Retry patch_itinerary with JSON numbers for costs/confidence/grounded coordinates. "
                    "For agent-text-timeline drafts, omit top-level amapPoi and put latitude=null, longitude=null "
                    "inside segment.poi."
                ),
                "validationErrors": [
                    f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
                    for item in error.errors()[:5]
                ],
            }
        error_text = " ".join([reason, *validation_errors])
        if any(marker in error_text for marker in ["must be a number", "valid number", "float_parsing"]):
            return {
                "message": PATCH_ITINERARY_NUMERIC_FIELDS_FEEDBACK,
                "requiredPath": "operations[].fullItinerary numeric fields and operations[].amapPoi coordinates",
                "retryInstruction": (
                    "Retry patch_itinerary with JSON numbers for costs/confidence/grounded coordinates. "
                    "For agent-text-timeline drafts, omit top-level amapPoi and put latitude=null, longitude=null "
                    "inside segment.poi."
                ),
                "validationErrors": validation_errors,
            }
        if isinstance(error, ValidationError):
            invalid_poi_fields = [
                ".".join(str(part) for part in item.get("loc", []))
                for item in error.errors()
                if re.search(r"operations\.\d+\.poi", ".".join(str(part) for part in item.get("loc", [])))
            ]
            if invalid_poi_fields:
                return {
                    "message": "patch_itinerary 不接受 operations[].poi 字段。",
                    "requiredPath": "operations[].amapPoi or operations[].candidateId",
                    "retryInstruction": (
                        "Retry patch_itinerary using amapPoi for a resolved AMap POI, or candidateId with "
                        "replace_segment_poi_from_candidate. Do not send poi at operation top level."
                    ),
                    "patchValidationDiagnostics": {
                        "operationCount": len(arguments.get("operations") or []),
                        "invalidFields": invalid_poi_fields[:8],
                        "schemaHint": "Use amapPoi or candidateId; poi is forbidden.",
                    },
                    "validationErrors": [
                        f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
                        for item in error.errors()[:5]
                    ],
                }
        if "replace_segment_poi requires resolved AMap POI" in error_text:
            return {
                "message": "replace_segment_poi 需要 resolved AMap POI。",
                "requiredPath": "operations[].amapPoi",
                "retryInstruction": "Use resolve_poi first, or use expand_*_poi_candidates then replace_segment_poi_from_candidate.",
                "patchValidationDiagnostics": {
                    "operationCount": len(arguments.get("operations") or []),
                    "invalidFields": ["operations[].amapPoi"],
                    "schemaHint": "replace_segment_poi requires amapPoi; candidateId requires replace_segment_poi_from_candidate.",
                },
                "validationErrors": validation_errors,
            }
        if not self._has_replace_itinerary_segment_without_poi(arguments) and not any(
            marker in error_text for marker in ["'poi'", "Segment POI is required"]
        ):
            return None
        return {
            "message": PATCH_ITINERARY_SEGMENT_POI_REQUIRED_FEEDBACK,
            "requiredPath": "operations[].fullItinerary.days[].segments[].poi",
            "retryInstruction": (
                "Retry patch_itinerary with replace_itinerary.fullItinerary where every segment has a poi object. "
                "For agent-text-timeline drafts use segment.poi.name/source/sourceNote/confidence/latitude/longitude, "
                "with latitude=null and longitude=null until AMap grounding succeeds."
            ),
            "validationErrors": validation_errors,
        }

    def _has_numeric_validation_error(self, error: ValidationError) -> bool:
        numeric_error_types = {"float_parsing", "float_type", "int_parsing", "int_type"}
        return any(str(item.get("type") or "") in numeric_error_types for item in error.errors())

    def _has_replace_itinerary_segment_without_poi(self, arguments: dict[str, Any]) -> bool:
        operations = arguments.get("operations")
        if not isinstance(operations, list):
            return False
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") != "replace_itinerary":
                continue
            snapshot = operation.get("fullItinerary")
            if not isinstance(snapshot, dict):
                continue
            for day in snapshot.get("days") or []:
                if not isinstance(day, dict):
                    continue
                for segment in day.get("segments") or []:
                    if isinstance(segment, dict) and not isinstance(segment.get("poi"), dict):
                        return True
        return False

    def _input_summary(self, arguments: dict[str, Any]) -> str:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
        return text[:240]

    def _payload_preview(self, payload: Any) -> Any:
        return self._trim_preview(payload)

    def _output_preview(self, tool_name: str, output: dict[str, Any]) -> dict[str, Any]:
        base = {
            "providerName": output.get("providerName"),
            "fallbackUsed": bool(output.get("fallbackUsed")),
            "failureReason": output.get("failureReason"),
            "confidence": output.get("confidence"),
        }
        if tool_name in {"web_search", "ticket_lookup"}:
            return {
                **base,
                "query": output.get("query"),
                "schemaVersion": output.get("schemaVersion"),
                "status": output.get("status"),
                "queryFingerprint": output.get("queryFingerprint"),
                "resultFingerprint": output.get("resultFingerprint"),
                "queriedAt": output.get("queriedAt"),
                "resultCount": len(output.get("results") or []),
                "results": [
                    {
                        "refId": item.get("refId"),
                        "sourceFingerprint": item.get("sourceFingerprint"),
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "snippet": item.get("snippet"),
                        "sourceName": item.get("sourceName"),
                        "confidence": item.get("confidence"),
                    }
                    for item in (output.get("results") or [])[:5]
                    if isinstance(item, dict)
                ],
            }
        if tool_name == "amap_weather":
            return {
                **base,
                "city": output.get("city"),
                "date": output.get("date"),
                "weather": output.get("weather"),
                "temperatureRange": output.get("temperatureRange"),
                "riskLevel": output.get("riskLevel"),
                "riskReason": output.get("riskReason"),
                "requestedDate": output.get("requestedDate"),
                "requestedDateRange": output.get("requestedDateRange"),
                "weatherStatus": output.get("weatherStatus"),
                "userVisibleCaveat": output.get("userVisibleCaveat"),
            }
        if tool_name == "resolve_poi":
            return {
                **base,
                "resolvedCount": len(output.get("resolved") or []),
                "pendingCount": len(output.get("pending") or []),
                "resolved": [
                    {
                        "query": item.get("query"),
                        "name": (item.get("poi") or {}).get("name") if isinstance(item.get("poi"), dict) else None,
                        "amapId": (item.get("poi") or {}).get("id") if isinstance(item.get("poi"), dict) else None,
                        "poi": self._preview_resolved_poi(item.get("poi")),
                    }
                    for item in (output.get("resolved") or [])[:5]
                    if isinstance(item, dict)
                ],
                "pending": [
                    {
                        "candidateId": item.get("candidateRecordId") or item.get("id"),
                        "query": item.get("query"),
                        "candidateCount": len(item.get("candidates") or []),
                    }
                    for item in (output.get("pending") or [])[:5]
                    if isinstance(item, dict)
                ],
            }
        if tool_name == "patch_itinerary":
            itinerary = output.get("itinerary") if isinstance(output.get("itinerary"), dict) else {}
            days = itinerary.get("days") if isinstance(itinerary, dict) else []
            segment_count = 0
            if isinstance(days, list):
                for day in days:
                    if isinstance(day, dict) and isinstance(day.get("segments"), list):
                        segment_count += len(day["segments"])
            return {
                **base,
                "activeVersionId": output.get("activeVersionId"),
                "title": itinerary.get("title") if isinstance(itinerary, dict) else None,
                "city": itinerary.get("city") if isinstance(itinerary, dict) else None,
                "dayCount": len(days) if isinstance(days, list) else 0,
                "segmentCount": segment_count,
                "changedSegmentIds": output.get("changedSegmentIds") or [],
                "routeImpact": ((output.get("patch") or {}).get("metadata") or {}).get("routeRefresh")
                if isinstance(output.get("patch"), dict)
                else None,
            }
        if tool_name == "read_itinerary":
            itinerary = output.get("itinerary") if isinstance(output.get("itinerary"), dict) else {}
            days = itinerary.get("days") if isinstance(itinerary, dict) else []
            return {
                **base,
                "activePlanId": output.get("activePlanId"),
                "activeVersionId": output.get("activeVersionId"),
                "city": output.get("city"),
                "title": itinerary.get("title") if isinstance(itinerary, dict) else None,
                "dayCount": len(days) if isinstance(days, list) else 0,
            }
        if tool_name in {"read_preference_memory", "save_preference_memory"}:
            memory_text = str(output.get("memoryText") or "")
            return {
                **base,
                "memoryText": memory_text[:800],
                "memoryLength": len(memory_text),
                "updatedAt": output.get("updatedAt"),
                "autoUpdateEnabled": output.get("autoUpdateEnabled"),
            }
        return self._payload_preview(output)

    def _preview_resolved_poi(self, poi: Any) -> Optional[dict[str, Any]]:
        if not isinstance(poi, dict):
            return None
        return {
            "id": poi.get("id"),
            "name": poi.get("name"),
            "source": poi.get("source"),
            "longitude": poi.get("longitude"),
            "latitude": poi.get("latitude"),
            "confidence": poi.get("confidence"),
        }

    def _bounded_preview(self, payload: Any, max_chars: int = 6000) -> Any:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return payload
        return {
            "truncated": True,
            "preview": text[:max_chars],
            "originalLength": len(text),
        }

    def _trim_preview(self, value: Any, depth: int = 0) -> Any:
        if depth >= 5:
            return "..."
        if isinstance(value, dict):
            return {
                str(key): self._trim_preview(item, depth + 1)
                for key, item in list(value.items())[:24]
                if str(key).lower() not in {"apikey", "api_key", "authorization", "token", "password", "secret"}
            }
        if isinstance(value, list):
            items = [self._trim_preview(item, depth + 1) for item in value[:8]]
            if len(value) > 8:
                items.append(f"... truncated {len(value) - 8} items")
            return items
        if isinstance(value, str):
            return value if len(value) <= 1000 else f"{value[:1000]}..."
        return value

    def _output_summary(self, tool_name: str, output: dict[str, Any]) -> str:
        if output.get("failureReason"):
            return str(output.get("failureReason"))
        if tool_name == "read_itinerary":
            version = output.get("activeVersionId")
            return (
                f"已读取当前 itinerary 版本 {version}。"
                if version
                else "已读取当前 itinerary；当前没有 active version。"
            )
        if tool_name == "patch_itinerary" and output.get("activeVersionId"):
            changed = output.get("changedSegmentIds") or []
            return f"已写入 itinerary version {output['activeVersionId']}，修改 {len(changed)} 个时间轴节点。"
        if "results" in output:
            return f"返回 {len(output.get('results') or [])} 条结果。"
        if "resolved" in output or "pending" in output:
            return f"POI resolved={len(output.get('resolved') or [])}, pending={len(output.get('pending') or [])}。"
        if output.get("memoryText") is not None:
            return "已读取/更新偏好摘要。"
        if output.get("weather"):
            return (
                f"{output.get('city')} {output.get('date')} {output.get('weather')}，风险 {output.get('riskLevel')}。"
            )
        return "工具执行完成。"


def parse_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], Optional[str]]:
    if raw_arguments in (None, ""):
        return {}, None
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    try:
        parsed = json.loads(str(raw_arguments))
    except json.JSONDecodeError as error:
        return {}, f"工具参数不是合法 JSON：{error.msg}"
    if not isinstance(parsed, dict):
        return {}, "工具参数 JSON 根节点必须是对象。"
    return parsed, None
