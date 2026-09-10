import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.itineraries import (
    ItineraryExportJsonResponse,
    SavedItineraryVersionResponse,
)
from src.services.itinerary_service import ItineraryService


class ItineraryExportService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def save_version(self, plan_id: str, version_id: str) -> SavedItineraryVersionResponse:
        version = self.db.execute(
            "SELECT * FROM itinerary_versions WHERE id = ? AND plan_id = ?",
            (version_id, plan_id),
        ).fetchone()
        if version is None:
            raise HTTPException(status_code=404, detail="Itinerary version not found")
        plan = ItineraryService(self.db).get_plan(plan_id)
        existing = self.db.execute(
            """
            SELECT * FROM saved_itinerary_versions
            WHERE session_id = ? AND plan_id = ? AND version_id = ?
            """,
            (version["session_id"], plan_id, version_id),
        ).fetchone()
        if existing is not None:
            return self._saved_response(existing)
        now = datetime.now(timezone.utc).isoformat()
        saved_id = f"saved_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO saved_itinerary_versions (
                id, session_id, plan_id, version_id, title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (saved_id, version["session_id"], plan_id, version_id, plan.title, now),
        )
        self.db.commit()
        row = self.db.execute("SELECT * FROM saved_itinerary_versions WHERE id = ?", (saved_id,)).fetchone()
        return self._saved_response(row)

    def list_saved_versions(self, plan_id: str) -> list[SavedItineraryVersionResponse]:
        rows = self.db.execute(
            """
            SELECT * FROM saved_itinerary_versions
            WHERE plan_id = ?
            ORDER BY created_at DESC
            """,
            (plan_id,),
        ).fetchall()
        return [self._saved_response(row) for row in rows]

    def export_json(self, plan_id: str) -> ItineraryExportJsonResponse:
        plan = ItineraryService(self.db).get_plan(plan_id)
        active_version_id = self._active_version_id(plan_id)
        return ItineraryExportJsonResponse(
            exportedAt=datetime.now(timezone.utc).isoformat(),
            activeVersionId=active_version_id,
            planId=plan_id,
            itineraryPlan=plan,
            routeOptions=plan.route_options,
            riskSignals={
                "weatherSignals": [item.model_dump(by_alias=True) for item in plan.weather_signals],
                "trafficCrowdingSignals": [item.model_dump(by_alias=True) for item in plan.traffic_crowding_signals],
                "poiRiskAlerts": [item.model_dump(by_alias=True) for item in plan.poi_risk_alerts],
            },
        )

    def export_markdown(self, plan_id: str) -> str:
        payload = self.export_json(plan_id)
        plan = payload.itinerary_plan
        budget = plan.budget_breakdown
        budget_lines = (
            [
                "- 预算口径：后端统一预算明细",
                f"- 已知活动费用：¥{round(budget.known_activity_cost)}",
                f"- 已知餐饮费用：¥{round(budget.known_meal_cost)}",
                f"- 已知交通费用：¥{round(budget.known_transport_cost)}",
                f"- 已知合计：¥{round(budget.known_total)}",
                (
                    f"- 当前可估（暂估）：¥{round(budget.provisional_preferred)}"
                    f"（范围 ¥{round(budget.provisional_min)}–¥{round(budget.provisional_max)}）"
                ),
                f"- 未知项：{'；'.join(budget.unknown_items) if budget.unknown_items else '无'}",
                f"- 预算是否完整：{'是' if budget.is_complete else '否'}",
            ]
            if budget
            else [f"- 预算估算：¥{round(plan.budget_estimate)}"]
        )
        lines = [
            f"# {plan.title}",
            "",
            f"- 城市：{plan.city}",
            f"- Plan ID：{payload.plan_id}",
            f"- Active Version：{payload.active_version_id or '无'}",
            f"- 导出时间：{payload.exported_at}",
            *budget_lines,
            "",
        ]
        for day in plan.days:
            day_title = f"Day {day.day_number}"
            if day.date:
                day_title += f" · {day.date}"
            if day.title:
                day_title += f" · {day.title}"
            lines.extend([f"## {day_title}", ""])
            for segment in day.segments:
                poi_name = segment.poi.name if segment.poi else "待定地点"
                cost = f" · ¥{round(segment.estimated_cost)}" if segment.estimated_cost else ""
                notes = f" · {segment.notes}" if segment.notes else ""
                lines.append(f"- {segment.start_time}-{segment.end_time} {segment.kind}：{poi_name}{cost}{notes}")
            lines.append("")
        if plan.route_options:
            lines.extend(["## 路线候选", ""])
            for route in plan.route_options:
                selected = "（当前）" if route.is_selected else ""
                lines.append(
                    f"- {route.label}{selected}：{round(route.distance_meters / 1000, 1)} km，{route.duration_minutes} 分钟，¥{round(route.cost_amount)}"
                )
            lines.append("")
        risk_summary = {
            "weatherCount": len(plan.weather_signals),
            "trafficCount": len(plan.traffic_crowding_signals),
            "poiRiskCount": len(plan.poi_risk_alerts),
        }
        lines.extend(["## 风险信号", "", f"```json\n{json.dumps(risk_summary, ensure_ascii=False, indent=2)}\n```", ""])
        return "\n".join(lines).strip() + "\n"

    def _active_version_id(self, plan_id: str) -> Optional[str]:
        row = self.db.execute(
            """
            SELECT active_version_id FROM conversation_sessions
            WHERE active_plan_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if row is not None and row["active_version_id"]:
            return str(row["active_version_id"])
        latest = self.db.execute(
            """
            SELECT id FROM itinerary_versions
            WHERE plan_id = ?
            ORDER BY version_number DESC, created_at DESC
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        return str(latest["id"]) if latest is not None else None

    def _saved_response(self, row: Any) -> SavedItineraryVersionResponse:
        return SavedItineraryVersionResponse(
            id=row["id"],
            sessionId=row["session_id"],
            planId=row["plan_id"],
            versionId=row["version_id"],
            title=row["title"],
            createdAt=row["created_at"],
        )
