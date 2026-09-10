import sqlite3
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.reminders import WeatherReminderResponse
from src.models.reminder_draft import ReminderDraft
from src.providers.mock.email_provider import SimulatedEmailProvider


class ReminderService:
    def __init__(self, db: sqlite3.Connection, email_provider: Optional[SimulatedEmailProvider] = None):
        self.db = db
        self.email_provider = email_provider or SimulatedEmailProvider()

    def create_weather_reminder(
        self, itinerary_plan_id: str, email_address: str, trigger_date: str
    ) -> WeatherReminderResponse:
        plan = self.db.execute("SELECT * FROM itinerary_plans WHERE id = ?", (itinerary_plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="Itinerary plan not found")

        weather = self.db.execute(
            "SELECT * FROM weather_signals WHERE plan_id = ? ORDER BY queried_at DESC LIMIT 1",
            (itinerary_plan_id,),
        ).fetchone()
        risk_text = weather["daily_summary"] if weather else "天气待查询"
        subject = f"{plan['city']}天气风险提醒"
        body = f"{trigger_date} 行程天气：{risk_text}。如天气影响旅行目的，请提前调整路线或出发时间。"
        provider_result = self.email_provider.schedule(email_address, subject, body)
        reminder = ReminderDraft(
            id=f"rem_{uuid4().hex[:12]}",
            itinerary_plan_id=itinerary_plan_id,
            email_address=email_address,
            trigger_date=trigger_date,
            subject=subject,
            body=body,
            simulated_status=provider_result.status,
        )
        self._insert(reminder)
        self.db.commit()
        return WeatherReminderResponse(
            reminder_id=reminder.id,
            simulated_status=reminder.simulated_status,
            subject=reminder.subject,
            body_preview=reminder.body,
            provider_name=self.email_provider.name,
        )

    def _insert(self, reminder: ReminderDraft) -> None:
        self.db.execute(
            """
            INSERT INTO reminder_drafts (
                id, inspiration_set_id, itinerary_plan_id, email_address,
                trigger_date, subject, body, simulated_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reminder.id,
                reminder.inspiration_set_id,
                reminder.itinerary_plan_id,
                reminder.email_address,
                reminder.trigger_date,
                reminder.subject,
                reminder.body,
                reminder.simulated_status,
                reminder.created_at.isoformat(),
            ),
        )
