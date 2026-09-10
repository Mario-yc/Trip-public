from typing import Callable, Optional
from uuid import uuid4

from src.core.config import get_settings
from src.models.weather_signal import WeatherSignal
from src.providers.travel_tools import AMAP_WEATHER_URL, AmapWeatherProvider, ResilientAmapWeatherProvider
from src.services.trip_date_resolver import TripDateResolver


class WeatherService:
    def __init__(
        self,
        weather_provider_key: Optional[str] = None,
        timeout_seconds: float = 5.0,
        http_get: Optional[Callable[[str, float], dict]] = None,
        provider: Optional[ResilientAmapWeatherProvider] = None,
    ):
        settings = get_settings()
        if weather_provider_key is None:
            weather_provider_key = settings.weather_provider_key or settings.map_provider_key
        self.weather_provider_key = weather_provider_key
        self.timeout_seconds = settings.provider_timeout_seconds if timeout_seconds == 5.0 else timeout_seconds
        self.http_get = http_get
        self.provider = provider or ResilientAmapWeatherProvider(
            default_provider=AmapWeatherProvider(
                api_key=self.weather_provider_key,
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            )
        )

    def build_weather_signal(
        self,
        city: str,
        travel_purpose_tags: list[str],
        weather_context: Optional[dict] = None,
    ) -> WeatherSignal:
        weather_context = weather_context or {}
        availability = self.availability_contract(weather_context)
        if availability["status"] == "outside_forecast_window":
            return WeatherSignal(
                id=f"weather_{uuid4().hex[:10]}",
                city=city,
                date=str(availability.get("tripStartDate") or ""),
                hourly_forecast=[],
                daily_summary="已识别出行日期，尚未进入天气预报窗口；当前未使用今日天气推断未来行程。",
                risk_level="unknown",
                purpose_impact_reason="forecast_not_supported_yet: 等进入预报窗口后再查询真实天气。",
                source="weather-forecast-window",
                data_status="outside_forecast_window",
                confidence=0.0,
                failure_reason="forecast_not_supported_yet",
                source_url=None,
                user_visible_caveat=f"预计从 {availability.get('forecastAvailableFrom') or '临近出发'} 起可查询；当前没有远期天气数据。",
                provider_name="local-date-guard",
                fallback_used=False,
            )
        response = self.provider.query(
            city,
            travel_date=self._travel_start_date(weather_context),
            purpose_tags=self._purpose_tags(travel_purpose_tags, weather_context),
            context=weather_context,
        )
        hourly_forecast = self._forecast_rows(response.raw.get("casts") or [])
        if not hourly_forecast and isinstance(response.raw.get("forecast"), dict):
            hourly_forecast = self._forecast_rows(response.raw["forecast"].get("casts") or [])
        data_status = "fallback" if response.fallback_used else "degraded"
        if response.failure_reason == "forecast_not_supported_yet":
            data_status = "forecast_not_supported_yet"
        return WeatherSignal(
            id=f"weather_{uuid4().hex[:10]}",
            city=response.city,
            date=response.date,
            hourly_forecast=hourly_forecast,
            daily_summary=f"{response.weather}，{response.temperature_range}",
            risk_level=response.risk_level,
            purpose_impact_reason=response.risk_reason,
            source=response.source_name,
            data_status=data_status,
            confidence=response.confidence,
            failure_reason=response.failure_reason,
            source_url=AMAP_WEATHER_URL,
            user_visible_caveat=response.user_visible_caveat,
            provider_name=response.provider_name,
            fallback_used=response.fallback_used,
            queried_at=response.queried_at,
        )

    def availability_contract(self, weather_context: Optional[dict] = None) -> dict:
        context = weather_context or {}
        resolver = TripDateResolver()
        resolved = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else None
        if resolved is None:
            start = self._travel_start_date(context)
            resolved = resolver.resolve(str(start or ""), source="weatherContext").to_camel_dict()
        return resolver.weather_availability_contract(resolved)

    def _forecast_rows(self, casts: list[dict]) -> list[dict]:
        return [
            {
                "date": item.get("date"),
                "dayWeather": item.get("dayweather") or item.get("dayWeather"),
                "nightWeather": item.get("nightweather") or item.get("nightWeather"),
                "dayTempC": item.get("daytemp") or item.get("dayTempC"),
                "nightTempC": item.get("nighttemp") or item.get("nightTempC"),
                "dayWind": item.get("daywind") or item.get("dayWind"),
                "nightWind": item.get("nightwind") or item.get("nightWind"),
            }
            for item in casts
        ]

    def _travel_start_date(self, weather_context: dict) -> Optional[str]:
        resolved_dates = weather_context.get("resolvedTripDates")
        if isinstance(resolved_dates, dict):
            value = resolved_dates.get("startDate") or resolved_dates.get("start_date")
            if value:
                return str(value)
        date_range = weather_context.get("travelDateRange")
        if isinstance(date_range, dict):
            value = date_range.get("start") or date_range.get("date")
            if value:
                return str(value)
        value = weather_context.get("travelDate") or weather_context.get("date")
        if value:
            return str(value)
        requirements = weather_context.get("understoodRequirements")
        if isinstance(requirements, dict):
            fields = requirements.get("fields")
            if isinstance(fields, dict):
                value = fields.get("travelDate")
                if value and str(value) != "待确认":
                    return str(value)
        timeline_context = weather_context.get("timelineContext")
        if isinstance(timeline_context, dict):
            plan = timeline_context.get("itineraryPlan")
            if isinstance(plan, dict):
                for day in plan.get("days") or []:
                    if isinstance(day, dict) and day.get("date"):
                        return str(day["date"])
        return str(value) if value else None

    def _purpose_tags(self, travel_purpose_tags: list[str], weather_context: dict) -> list[str]:
        tags = [str(tag) for tag in travel_purpose_tags]
        for key in ("tripPurpose", "preferenceSummary", "weatherSensitivity"):
            value = weather_context.get(key)
            if value:
                tags.append(str(value))
        return tags
