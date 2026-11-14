import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import router as auth_router
from app.api.environments import router as environments_router
from app.api.evaluation import router as evaluation_router
from app.api.events import router as events_router
from app.api.experiments import router as experiments_router
from app.api.flags import router as flags_router
from app.api.health import router as health_router
from app.api.projects import router as projects_router
from app.core.config import settings
from app.core.logging_config import configure_logging, request_id_context
from app.core.rate_limit import rate_limit_auth_requests
from app.web.dashboard import router as dashboard_router

configure_logging()
logger = logging.getLogger("parakh.http")

app = FastAPI(title="Parakh", version="0.1.0")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)
app.middleware("http")(rate_limit_auth_requests)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict) or "code" not in detail or "message" not in detail:
        detail = {"code": "http_error", "message": str(detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": jsonable_encoder(detail)},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "errors": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")
    safe_request_id = (
        supplied_request_id
        if len(supplied_request_id) <= 128
        and all(character.isalnum() or character in "-_" for character in supplied_request_id)
        else ""
    )
    request_id = safe_request_id or str(uuid4())
    token = request_id_context.set(request_id)
    started_at = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request error")
        response = JSONResponse(
            status_code=500,
            content={
                "detail": {"code": "internal_error", "message": "An unexpected error occurred."}
            },
        )
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
        },
    )
    request_id_context.reset(token)
    return response


app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(environments_router)
app.include_router(evaluation_router)
app.include_router(events_router)
app.include_router(experiments_router)
app.include_router(flags_router)
app.include_router(health_router)
app.include_router(dashboard_router)
