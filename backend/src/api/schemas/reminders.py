from pydantic import BaseModel, Field


class WeatherReminderRequest(BaseModel):
    itinerary_plan_id: str = Field(alias="itineraryPlanId")
    email_address: str = Field(alias="emailAddress")
    trigger_date: str = Field(alias="triggerDate")

    model_config = {"populate_by_name": True}


class WeatherReminderResponse(BaseModel):
    reminder_id: str = Field(alias="reminderId")
    simulated_status: str = Field(alias="simulatedStatus")
    subject: str
    body_preview: str = Field(alias="bodyPreview")
    provider_name: str = Field(alias="providerName")

    model_config = {"populate_by_name": True}
