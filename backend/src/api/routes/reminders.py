import sqlite3

from fastapi import APIRouter, Depends

from src.api.schemas.reminders import WeatherReminderRequest, WeatherReminderResponse
from src.core.database import get_db
from src.services.reminder_service import ReminderService


router = APIRouter(prefix="/reminders", tags=["reminders"])


@router.post("/weather", response_model=WeatherReminderResponse)
def create_weather_reminder(
    payload: WeatherReminderRequest, db: sqlite3.Connection = Depends(get_db)
) -> WeatherReminderResponse:
    return ReminderService(db).create_weather_reminder(
        payload.itinerary_plan_id,
        payload.email_address,
        payload.trigger_date,
    )
