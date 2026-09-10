import sqlite3

from fastapi import APIRouter, Depends, File, Form, UploadFile

from src.api.schemas.inspirations import (
    SocialLinkIngestRequest,
    SocialLinkIngestResponse,
    SourceMaterialCleanupResponse,
    SourceMaterialCreateResponse,
    SourceMaterialInput,
)
from src.core.database import get_db
from src.services.image_cleanup_service import ImageCleanupService
from src.services.source_material_service import SourceMaterialService
from src.services.social_link_ingestion_service import SocialLinkIngestionService

router = APIRouter(prefix="/source-materials", tags=["source-materials"])


@router.post("/social-link", response_model=SocialLinkIngestResponse)
def ingest_social_link(
    payload: SocialLinkIngestRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> SocialLinkIngestResponse:
    material = SocialLinkIngestionService(db).ingest(payload.url)
    return SocialLinkIngestResponse(
        sourceMaterialId=material.id,
        fetchStatus=str(material.metadata.get("fetchStatus") or "needs_user_material"),
        failureReason=material.metadata.get("failureReason"),
        canonicalUrl=material.metadata.get("canonicalUrl"),
        extractedText=material.raw_text,
    )


@router.post("", response_model=SourceMaterialCreateResponse)
def create_source_material(
    payload: SourceMaterialInput, db: sqlite3.Connection = Depends(get_db)
) -> SourceMaterialCreateResponse:
    material = SourceMaterialService(db).create(payload)
    return SourceMaterialCreateResponse(
        sourceMaterialId=material.id,
        kind=material.kind,
        thumbnailUrl=material.thumbnail_path,
        originalRetention=material.original_retention,
        cacheStatus=material.cache_status,
        metadata=material.metadata,
    )


@router.post("/cleanup-originals", response_model=SourceMaterialCleanupResponse)
def cleanup_original_images(db: sqlite3.Connection = Depends(get_db)) -> SourceMaterialCleanupResponse:
    result = ImageCleanupService(db).cleanup_temporary_originals()
    return SourceMaterialCleanupResponse(**result)


@router.post("/upload", response_model=SourceMaterialCreateResponse)
async def upload_source_material(
    kind: str = Form(...),
    save_original: bool = Form(False, alias="saveOriginal"),
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
) -> SourceMaterialCreateResponse:
    material = await SourceMaterialService(db).create_uploaded_file(kind, file, save_original)
    return SourceMaterialCreateResponse(
        sourceMaterialId=material.id,
        kind=material.kind,
        thumbnailUrl=material.thumbnail_path,
        originalRetention=material.original_retention,
        cacheStatus=material.cache_status,
        metadata=material.metadata,
    )
