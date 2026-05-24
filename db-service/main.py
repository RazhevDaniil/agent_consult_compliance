from contextlib import asynccontextmanager
from typing import AsyncGenerator
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from structlog.contextvars import bind_contextvars, unbind_contextvars

from app.api.v1.router import router as v1_router
from app.config import get_settings
from app.db.session import engine
from app.logging_config import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
log = get_logger("app")
TRACE_HEADER_NAME = "x-trace-id"


def _resolve_trace_id(value: str | None) -> tuple[str, bool, bool]:
    if not value:
        return str(uuid.uuid4()), True, False
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError):
        return str(uuid.uuid4()), False, True
    if parsed.version != 4:
        return str(uuid.uuid4()), False, True
    return str(parsed), False, False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    log.info("app.started", port=settings.port)
    yield
    await engine.dispose()
    log.info("app.stopped")


app = FastAPI(title="app", lifespan=lifespan)


@app.middleware("http")
async def trace_context_middleware(request: Request, call_next):
    """Bind trace headers to contextvars so db_app logs are correlated with
    the upstream operation."""
    bound: dict[str, str] = {}
    trace_id, missing, invalid = _resolve_trace_id(request.headers.get(TRACE_HEADER_NAME))
    bound["trace_id"] = trace_id
    if run_id := request.headers.get("X-Run-Id"):
        bound["run_id"] = run_id
    if thread_id := request.headers.get("X-Thread-Id"):
        bound["thread_id"] = thread_id
    bind_contextvars(**bound)
    try:
        if missing or invalid:
            log.warning(
                "trace_id_generated",
                trace_id=trace_id,
                trace_id_missing=missing,
                trace_id_invalid=invalid,
                path=request.url.path,
            )
        response = await call_next(request)
        response.headers[TRACE_HEADER_NAME] = trace_id
        return response
    finally:
        unbind_contextvars(*bound.keys())


app.include_router(v1_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    log.warning(
        "validation_error",
        path=request.url.path,
        method=request.method,
        errors=errors,
        body=exc.body,
    )
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok"}
