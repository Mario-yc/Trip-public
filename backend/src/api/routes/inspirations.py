from datetime import datetime, timezone
import sqlite3
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException

from src.api.schemas.inspirations import InspirationCreateRequest, InspirationCreateResponse, ExtractionResponse
from src.core.config import get_settings
from src.core.database import get_db
from src.models.inspiration_set import InspirationSet
from src.services.extraction_service import ExtractionService
from src.services.source_material_service import SourceMaterialService

router = APIRouter(prefix="/inspirations", tags=["inspirations"])


@router.post("", response_model=InspirationCreateResponse)
def create_inspiration(
    payload: InspirationCreateRequest, db: sqlite3.Connection = Depends(get_db)
) -> InspirationCreateResponse:
    settings = get_settings()
    material_service = SourceMaterialService(db)
    material_service.validate_material_ids(payload.source_material_ids)
    inspiration = InspirationSet(
        id=f"insp_{uuid4().hex[:12]}",
        user_id=settings.default_user_id,
        city=payload.city_hint,
        status="extracting",
    )
    try:
        db.execute(
            """
            INSERT INTO inspiration_sets (
                id, user_id, city, status, theme_summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inspiration.id,
                inspiration.user_id,
                inspiration.city,
                inspiration.status,
                inspiration.theme_summary,
                inspiration.created_at.isoformat(),
                inspiration.updated_at.isoformat(),
            ),
        )
        created_material_ids = material_service.create_text_sources(
            inspiration.id, payload.text_items, payload.social_links
        )
        bound_material_ids = material_service.bind_to_inspiration(
            payload.source_material_ids, inspiration.id, payload.save_original_images
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return InspirationCreateResponse(
        inspirationSetId=inspiration.id,
        status=inspiration.status,
        sourceMaterialIds=[*created_material_ids, *bound_material_ids],
    )


@router.post("/{inspiration_set_id}/extract", response_model=ExtractionResponse)
def extract_inspiration(
    inspiration_set_id: str,
    payload: Optional[InspirationCreateRequest] = Body(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> ExtractionResponse:
    payload = payload or InspirationCreateRequest()
    row = db.execute("SELECT * FROM inspiration_sets WHERE id = ?", (inspiration_set_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Inspiration set not found")
    inspiration = InspirationSet(
        id=row["id"],
        user_id=row["user_id"],
        city=row["city"],
        status=row["status"],
        theme_summary=row["theme_summary"],
        updated_at=datetime.now(timezone.utc),
    )
    return ExtractionService(db).extract(inspiration, payload.text_items, payload.social_links)
