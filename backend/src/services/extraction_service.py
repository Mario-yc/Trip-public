import json
import sqlite3
from uuid import uuid4
from typing import Optional

from src.api.schemas.inspirations import (
    CostItem,
    ExtractionResponse,
    ItineraryDay,
    ItineraryDraft,
    ItinerarySegment,
    PoiCandidate,
)
from src.core.config import get_settings
from src.models.extraction_result import ExtractionResult
from src.models.inspiration_set import InspirationSet
from src.providers.base.results import ProviderKind
from src.providers.default.registry import build_default_registry
from src.providers.mock.registry import build_mock_registry
from src.services.source_material_service import SourceMaterialService
from src.services.vision_service import VisionService


class ExtractionService:
    def __init__(self, db: sqlite3.Connection, vision_service: Optional[VisionService] = None):
        self.db = db
        self.vision_service = vision_service or VisionService()

    def extract(
        self,
        inspiration_set: InspirationSet,
        text_items: Optional[list[str]] = None,
        social_links: Optional[list[str]] = None,
    ) -> ExtractionResponse:
        settings = get_settings()
        materials = SourceMaterialService(self.db).list_for_inspiration(inspiration_set.id)
        fallback_text_items = text_items or []
        fallback_social_links = social_links or []
        if materials:
            fallback_text_items = []
            fallback_social_links = []

        default_registry = build_default_registry({ProviderKind.vision: settings.deepseek_api_key})
        mock_registry = build_mock_registry()
        extracted = self.vision_service.extract_from_materials(
            inspiration_set.city,
            fallback_text_items,
            fallback_social_links,
            materials=materials,
            provider_mode=settings.provider_mode,
            default_provider=default_registry[ProviderKind.vision],
            mock_provider=mock_registry[ProviderKind.vision],
        )
        itinerary_draft = self._build_itinerary_draft(extracted.city_candidates, extracted.poi_candidates)
        result = ExtractionResult(
            id=f"ext_{uuid4().hex[:12]}",
            inspiration_set_id=inspiration_set.id,
            city_candidates=extracted.city_candidates,
            poi_candidates=extracted.poi_candidates,
            style_tags=extracted.style_tags,
            budget_clues=extracted.budget_clues,
            route_clues=extracted.route_clues,
            confidence=extracted.confidence,
            needs_user_confirmation=extracted.needs_user_confirmation,
            source_links=extracted.source_links,
            provider_name=extracted.provider_name,
            fallback_used=extracted.fallback_used,
            provider_failure_reason=extracted.provider_failure_reason,
            user_visible_caveat=extracted.user_visible_caveat,
        )
        inspiration_set.status = "needs_confirmation" if extracted.needs_user_confirmation else "ready"
        self.db.execute(
            """
            INSERT INTO extraction_results (
                id, inspiration_set_id, city_candidates, poi_candidates, style_tags,
                budget_clues, route_clues, confidence, needs_user_confirmation,
                source_links, provider_name, fallback_used, provider_failure_reason,
                user_visible_caveat, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.id,
                result.inspiration_set_id,
                json.dumps(result.city_candidates, ensure_ascii=False),
                json.dumps(result.poi_candidates, ensure_ascii=False),
                json.dumps(result.style_tags, ensure_ascii=False),
                json.dumps(result.budget_clues, ensure_ascii=False),
                json.dumps(result.route_clues, ensure_ascii=False),
                result.confidence,
                1 if result.needs_user_confirmation else 0,
                json.dumps(result.source_links, ensure_ascii=False),
                result.provider_name,
                1 if result.fallback_used else 0,
                result.provider_failure_reason,
                result.user_visible_caveat,
                result.created_at.isoformat(),
            ),
        )
        self.db.execute(
            "UPDATE inspiration_sets SET status = ?, updated_at = ? WHERE id = ?",
            (inspiration_set.status, inspiration_set.updated_at.isoformat(), inspiration_set.id),
        )
        self.db.commit()

        return ExtractionResponse(
            inspirationSetId=inspiration_set.id,
            cityCandidates=extracted.city_candidates,
            poiCandidates=[
                PoiCandidate(name=item["name"], confidence=item["confidence"], sourceLinks=item.get("sourceLinks", []))
                for item in extracted.poi_candidates
            ],
            styleTags=extracted.style_tags,
            budgetClues=extracted.budget_clues,
            routeClues=extracted.route_clues,
            confidence=extracted.confidence,
            needsUserConfirmation=extracted.needs_user_confirmation,
            sourceLinks=extracted.source_links,
            providerName=extracted.provider_name,
            fallbackUsed=extracted.fallback_used,
            providerFailureReason=extracted.provider_failure_reason,
            userVisibleCaveat=extracted.user_visible_caveat,
            itineraryDraft=itinerary_draft,
        )

    def _build_itinerary_draft(self, city_candidates: list[str], poi_candidates: list[dict]) -> ItineraryDraft:
        city = city_candidates[0] if city_candidates else "待确认城市"
        segments = []
        for index, poi in enumerate(poi_candidates[:4], start=1):
            confidence = float(poi.get("confidence", 0.0))
            notes = ["门票/预约状态待票务查询确认"]
            if confidence < 0.6:
                notes.insert(0, "地点识别置信度较低，建议先由用户确认")
            segments.append(
                ItinerarySegment(
                    id=f"seg_{index}",
                    title=f"{poi['name']} 游览",
                    poiName=poi["name"],
                    startTime=f"{8 + index:02d}:30",
                    durationMinutes=120 if index == 1 else 90,
                    transportMode="公共交通/步行待确认",
                    costItems=[
                        CostItem(
                            label="景点门票或预约费用",
                            amountCny=0,
                            currency="CNY",
                            isEstimate=True,
                        )
                    ],
                    reservationNotes=notes,
                )
            )

        return ItineraryDraft(
            title=f"{city}灵感行程草案",
            editable=True,
            days=[ItineraryDay(dayNumber=1, title="Day 1 初版可编辑行程", segments=segments)],
        )
