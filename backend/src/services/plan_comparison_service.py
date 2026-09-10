import json
import sqlite3
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.itineraries import PlanComparisonResponse
from src.models.plan_comparison import PlanComparison
from src.services.itinerary_service import ItineraryService
from src.services.planning_run_service import PlanningRunService
from src.services.ticket_service import TicketService


COMPARISON_TEMPLATES = ["low_budget", "photo_first", "relaxed_pace"]
TICKET_FALLBACK_CAVEAT = "票务/预约联网查询未返回真实结果，请以官方渠道确认为准。"
ROUTE_PENDING_CAVEAT = (
    "路线状态为 pending_provider_verification：固定模板尚未经过统一路线合同与 Provider 插入矩阵终判。"
)


class PlanComparisonService:
    def __init__(self, db: sqlite3.Connection, ticket_service: Optional[TicketService] = None):
        self.db = db
        self.ticket_service = ticket_service or TicketService(db)

    def generate(
        self,
        inspiration_set_id: str,
        city: str,
        date_range: Optional[dict] = None,
        preference_profile_id: Optional[str] = None,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
    ) -> PlanComparisonResponse:
        inspiration = self.db.execute("SELECT * FROM inspiration_sets WHERE id = ?", (inspiration_set_id,)).fetchone()
        if inspiration is None:
            raise HTTPException(status_code=404, detail="Inspiration set not found")

        itinerary_service = ItineraryService(self.db)
        plans = []
        plan_ids = []
        fallback_used = False
        provider_name = "default-ticket-provider"
        for template in COMPARISON_TEMPLATES:
            plan = itinerary_service.generate_for_inspiration(
                inspiration_set_id,
                city,
                template_type=template,
                preference_profile_id=preference_profile_id,
                preference_summary=preference_summary,
                planning_context=planning_context,
                date_range=date_range,
            )
            self.ticket_service.refresh_for_plan(plan.id)
            refreshed = itinerary_service.get_plan(plan.id)
            plans.append(refreshed)
            plan_ids.append(refreshed.id)
            if refreshed.ticket_lookup_results:
                fallback_used = fallback_used or any(result.fallback_used for result in refreshed.ticket_lookup_results)
                provider_name = refreshed.ticket_lookup_results[0].provider_name

        comparison = PlanComparison(
            id=f"cmp_{uuid4().hex[:12]}",
            inspiration_set_id=inspiration_set_id,
            city=city or inspiration["city"] or "北京",
            plan_ids=plan_ids,
            provider_name=provider_name,
            fallback_used=fallback_used,
            user_visible_caveat=" ".join(
                item
                for item in (
                    TICKET_FALLBACK_CAVEAT if fallback_used else "票务来源已按可信度排序。",
                    ROUTE_PENDING_CAVEAT,
                )
                if item
            ),
        )
        self._persist(comparison)
        primary_plan = plans[0] if plans else None
        planning_run = PlanningRunService(self.db).create_run(
            "plan_comparison",
            user_input=str((planning_context or {}).get("currentUserMessage") or ""),
            preference_summary=preference_summary or "",
            itinerary=primary_plan,
            final_summary=(
                f"已生成 {len(plans)} 个固定模板方案，并按票务/来源透明度展示取舍；"
                "路线仍待统一合同与 Provider 矩阵核验，不宣称已完成可行性检查。"
            ),
        )
        self.db.commit()

        return PlanComparisonResponse(
            comparison_id=comparison.id,
            provider_name=comparison.provider_name,
            fallback_used=comparison.fallback_used,
            user_visible_caveat=comparison.user_visible_caveat,
            plans=plans,
            planningRun=planning_run,
        )

    def _persist(self, comparison: PlanComparison) -> None:
        self.db.execute(
            """
            INSERT INTO plan_comparisons (
                id, inspiration_set_id, city, plan_ids, provider_name,
                fallback_used, user_visible_caveat, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                comparison.id,
                comparison.inspiration_set_id,
                comparison.city,
                json.dumps(comparison.plan_ids, ensure_ascii=False),
                comparison.provider_name,
                1 if comparison.fallback_used else 0,
                comparison.user_visible_caveat,
                comparison.created_at.isoformat(),
            ),
        )
