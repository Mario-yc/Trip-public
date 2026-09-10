import json
import sqlite3
from pathlib import Path, PurePath
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from src.api.schemas.inspirations import SourceMaterialInput
from src.core.database import PROJECT_ROOT
from src.models.source_material import SourceMaterial


UPLOAD_KINDS = {"screenshot", "map_screenshot", "scenery_photo", "image_set"}
SUPPORTED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class SourceMaterialService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create(self, payload: SourceMaterialInput) -> SourceMaterial:
        material = SourceMaterial(
            id=f"mat_{uuid4().hex[:12]}",
            inspiration_set_id=payload.inspiration_set_id,
            kind=payload.kind,
            raw_text=payload.raw_text,
            link_url=payload.link_url,
            thumbnail_path=payload.thumbnail_path,
            original_retention="long_term_opt_in" if payload.save_original else "temporary_cache",
        )
        self._insert(material)
        self.db.commit()
        return material

    async def create_uploaded_file(self, kind: str, file: UploadFile, save_original: bool) -> SourceMaterial:
        if kind not in UPLOAD_KINDS:
            raise HTTPException(status_code=400, detail=f"Unsupported source material kind: {kind}")
        if file.content_type not in SUPPORTED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Unsupported file type. Supported image types: image/jpeg, image/png, image/webp.",
            )

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="Uploaded file exceeds the 8 MB size limit")

        material_id = f"mat_{uuid4().hex[:12]}"
        original_retention = "long_term_opt_in" if save_original else "temporary_cache"
        safe_name = PurePath(file.filename or f"{material_id}.bin").name
        suffix = SUPPORTED_IMAGE_TYPES[file.content_type]
        base_dir = PROJECT_ROOT / "backend" / "data" / "uploads"
        original_dir = base_dir / ("originals" if save_original else "original-cache")
        thumbnail_dir = base_dir / "thumbnails"
        original_dir.mkdir(parents=True, exist_ok=True)
        thumbnail_dir.mkdir(parents=True, exist_ok=True)

        original_path = original_dir / f"{material_id}{suffix}"
        thumbnail_path = thumbnail_dir / f"{material_id}.placeholder.txt"
        original_path.write_bytes(content)
        self._write_thumbnail_placeholder(thumbnail_path, kind, safe_name, len(content), file.content_type)

        material = SourceMaterial(
            id=material_id,
            kind=kind,
            raw_text=safe_name,
            thumbnail_path=str(thumbnail_path.relative_to(PROJECT_ROOT)),
            original_path=str(original_path.relative_to(PROJECT_ROOT)),
            original_retention=original_retention,
        )
        self._insert(material)
        self.db.commit()
        return material

    def create_text_sources(self, inspiration_set_id: str, text_items: list[str], social_links: list[str]) -> list[str]:
        material_ids = []
        for text in text_items:
            if not text.strip():
                continue
            material = SourceMaterial(
                id=f"mat_{uuid4().hex[:12]}",
                inspiration_set_id=inspiration_set_id,
                kind="guide_text",
                raw_text=text.strip(),
                original_retention="structured_only",
                cache_status="not_applicable",
            )
            self._insert(material)
            material_ids.append(material.id)

        for link in social_links:
            if not link.strip():
                continue
            material = SourceMaterial(
                id=f"mat_{uuid4().hex[:12]}",
                inspiration_set_id=inspiration_set_id,
                kind="social_link",
                link_url=link.strip(),
                # A URL is an address, not extracted evidence. Public content
                # is populated only by SocialLinkIngestionService after a real
                # successful fetch; inaccessible links stay text-empty.
                raw_text=None,
                original_retention="structured_only",
                cache_status="not_applicable",
                metadata={
                    "originalUrl": link.strip(),
                    "canonicalUrl": None,
                    "fetchStatus": "needs_user_material",
                    "failureReason": "not_ingested_by_legacy_inspiration_flow",
                    "provider": "xiaohongshu_public_html",
                    "contentFingerprint": None,
                    "extractedAt": None,
                },
            )
            self._insert(material)
            material_ids.append(material.id)
        return material_ids

    def validate_material_ids(self, material_ids: list[str]) -> None:
        for material_id in material_ids:
            row = self.db.execute("SELECT id FROM source_materials WHERE id = ?", (material_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=400, detail=f"Source material not found: {material_id}")

    def bind_to_inspiration(
        self, material_ids: list[str], inspiration_set_id: str, save_original_images: bool = False
    ) -> list[str]:
        bound_ids = []
        for material_id in material_ids:
            if save_original_images:
                self.db.execute(
                    """
                    UPDATE source_materials
                    SET inspiration_set_id = ?, original_retention = ?
                    WHERE id = ?
                    """,
                    (inspiration_set_id, "long_term_opt_in", material_id),
                )
            else:
                self.db.execute(
                    "UPDATE source_materials SET inspiration_set_id = ? WHERE id = ?",
                    (inspiration_set_id, material_id),
                )
            bound_ids.append(material_id)
        return bound_ids

    def list_for_inspiration(self, inspiration_set_id: str) -> list[SourceMaterial]:
        rows = self.db.execute(
            """
            SELECT * FROM source_materials
            WHERE inspiration_set_id = ?
            ORDER BY created_at ASC
            """,
            (inspiration_set_id,),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def _insert(self, material: SourceMaterial) -> None:
        self.db.execute(
            """
            INSERT INTO source_materials (
                id, inspiration_set_id, kind, raw_text, link_url, thumbnail_path,
                original_path, original_retention, cache_status, created_at
                , metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material.id,
                material.inspiration_set_id,
                material.kind,
                material.raw_text,
                material.link_url,
                material.thumbnail_path,
                material.original_path,
                material.original_retention,
                material.cache_status,
                material.created_at.isoformat(),
                json.dumps(material.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _from_row(self, row: sqlite3.Row) -> SourceMaterial:
        metadata: dict = {}
        try:
            parsed_metadata = json.loads(row["metadata_json"] or "{}")
            if isinstance(parsed_metadata, dict):
                metadata = parsed_metadata
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return SourceMaterial(
            id=row["id"],
            inspiration_set_id=row["inspiration_set_id"],
            kind=row["kind"],
            raw_text=row["raw_text"],
            link_url=row["link_url"],
            thumbnail_path=row["thumbnail_path"],
            original_path=row["original_path"],
            original_retention=row["original_retention"],
            cache_status=row["cache_status"],
            metadata=metadata,
        )

    def _write_thumbnail_placeholder(
        self, thumbnail_path: Path, kind: str, filename: str, byte_count: int, content_type: str
    ) -> None:
        # Demo placeholder: replace this method with real image thumbnail generation when adding an image library.
        thumbnail_path.write_text(
            f"demo_thumbnail_placeholder=true\nkind={kind}\nfilename={filename}\n"
            f"bytes={byte_count}\ncontent_type={content_type}\n",
            encoding="utf-8",
        )
