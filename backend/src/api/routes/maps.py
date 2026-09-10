import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api.schemas.maps import MapConfigResponse, MapPoiResolveRequest, MapPoiResolveResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.core.database import get_db
from src.services.map_poi_service import MapPoiService
from src.services.poi_resolution_service import PoiResolutionService


router = APIRouter(prefix="/map", tags=["map"])


@router.get("/config", response_model=MapConfigResponse)
def get_map_config() -> MapConfigResponse:
    settings = get_settings()
    if not settings.map_js_api_key:
        raise HTTPException(status_code=400, detail="MAP_JS_API_KEY or MAP_PROVIDER_KEY is not configured")

    return MapConfigResponse(
        provider="amap",
        enabled=True,
        js_api_key=settings.map_js_api_key,
        security_js_code=settings.map_provider_security_js_code or None,
    )


@router.get("/pois", response_model=MapPoiSearchResponse)
def search_map_pois(city: str, keyword: str = "", category: str = "all") -> MapPoiSearchResponse:
    return MapPoiService().search(city=city, keyword=keyword, category=category)


@router.get("/pois/nearby", response_model=MapPoiSearchResponse)
def search_nearby_map_pois(
    city: str,
    longitude: float,
    latitude: float,
    keyword: str,
    category: str = "all",
    radius: int = 1500,
) -> MapPoiSearchResponse:
    return MapPoiService().search_nearby(
        city=city,
        longitude=longitude,
        latitude=latitude,
        keyword=keyword,
        category=category,
        radius=radius,
    )


@router.post("/pois/resolve", response_model=MapPoiResolveResponse)
def resolve_map_pois(
    payload: MapPoiResolveRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> MapPoiResolveResponse:
    return PoiResolutionService(db).resolve(payload)
