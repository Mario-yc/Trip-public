from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routes import agent, inspirations, itineraries, maps, preferences, providers, reminders, source_materials
from src.api.schemas.common import HealthResponse
from src.core.config import get_settings
from src.core.logging import RequestLoggingMiddleware
from src.core.schema import initialize_database


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(providers.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(inspirations.router, prefix="/api")
app.include_router(source_materials.router, prefix="/api")
app.include_router(itineraries.router, prefix="/api")
app.include_router(maps.router, prefix="/api")
app.include_router(preferences.router, prefix="/api")
app.include_router(reminders.router, prefix="/api")


def _http_error_code(status_code: int, detail) -> str:
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"])
    if status_code == 409 and isinstance(detail, str) and "stale" in detail.lower():
        return "STALE_BASE_VERSION"
    if isinstance(detail, dict) and detail.get("validationErrors"):
        return "PATCH_VALIDATION_FAILED"
    return f"HTTP_{status_code}"


def _http_error_message(status_code: int, detail) -> str:
    if isinstance(detail, dict):
        if detail.get("message"):
            return str(detail["message"])
        validation_errors = detail.get("validationErrors")
        if isinstance(validation_errors, list) and validation_errors:
            return str(validation_errors[0])
    if isinstance(detail, str):
        return detail
    return f"HTTP request failed with status {status_code}"


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    validation_errors = detail.get("validationErrors") if isinstance(detail, dict) else None
    body = {
        "code": _http_error_code(exc.status_code, detail),
        "message": _http_error_message(exc.status_code, detail),
        "details": detail if isinstance(detail, dict) else None,
        "validationErrors": validation_errors if isinstance(validation_errors, list) else [],
        "detail": detail,
    }
    return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(body), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    validation_errors = exc.errors()
    body = {
        "code": "REQUEST_VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": {"validationErrors": validation_errors},
        "validationErrors": validation_errors,
        "detail": {"validationErrors": validation_errors},
    }
    return JSONResponse(status_code=422, content=jsonable_encoder(body))


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", appName=settings.app_name, environment=settings.app_env)
