import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.schemas.preferences import (
    PreferenceEnvelope,
    PreferenceExtractRequest,
    PreferenceMemoryResponse,
    PreferenceMemoryUpdateRequest,
    PreferenceUpdateRequest,
)
from src.core.database import get_db
from src.services.preference_service import PreferenceService


router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("/memory", response_model=PreferenceMemoryResponse)
def get_preference_memory(
    session_id: Optional[str] = Query(default=None, alias="sessionId"),
    db: sqlite3.Connection = Depends(get_db),
) -> PreferenceMemoryResponse:
    return PreferenceService(db).get_memory(session_id=session_id)


@router.patch("/memory", response_model=PreferenceMemoryResponse)
def update_preference_memory(
    payload: PreferenceMemoryUpdateRequest,
    session_id: Optional[str] = Query(default=None, alias="sessionId"),
    db: sqlite3.Connection = Depends(get_db),
) -> PreferenceMemoryResponse:
    return PreferenceService(db).update_memory(
        memory_text=payload.memory_text,
        structured_memory=payload.structured_memory,
        auto_update_enabled=payload.auto_update_enabled,
        session_id=session_id,
    )


@router.post("/memory/restore-default", response_model=PreferenceMemoryResponse)
def restore_preference_memory(
    session_id: Optional[str] = Query(default=None, alias="sessionId"),
    db: sqlite3.Connection = Depends(get_db),
) -> PreferenceMemoryResponse:
    return PreferenceService(db).restore_default_memory(session_id=session_id)


@router.post("/extract", response_model=PreferenceEnvelope)
def extract_preferences(
    payload: PreferenceExtractRequest, db: sqlite3.Connection = Depends(get_db)
) -> PreferenceEnvelope:
    card = PreferenceService(db).extract_from_text(payload.conversation_text)
    return PreferenceEnvelope(summary_card=card)


@router.patch("/{card_id}", response_model=PreferenceEnvelope)
def update_preferences(
    card_id: str, payload: PreferenceUpdateRequest, db: sqlite3.Connection = Depends(get_db)
) -> PreferenceEnvelope:
    card = PreferenceService(db).update_card(
        card_id,
        party_size=payload.party_size,
        traveler_types=payload.traveler_types,
        budget_range=payload.budget_range,
        pace_preference=payload.pace_preference,
        summary_text=payload.summary_text,
        items=payload.items,
    )
    return PreferenceEnvelope(summary_card=card)
