from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .store import store
from .config import settings
from .card import build_agent_card
from .service import JsonRpcException, service
from .models import JsonRpcError, JsonRpcErrorResponse, JsonRpcRequest


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
TRACE_HEADER_NAME = "x-trace-id"


def _resolve_trace_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("x-trace-id must be a valid UUID v4") from exc
    if parsed.version != 4:
        raise ValueError("x-trace-id must be a UUID v4")
    return str(parsed)


def _json_response_with_trace(
    payload: JsonRpcErrorResponse,
    trace_id: str,
    x_session_id: str | None = None,
) -> JSONResponse:
    headers = {TRACE_HEADER_NAME: trace_id}
    if x_session_id:
        headers["x-session-id"] = x_session_id
    return JSONResponse(
        status_code=200,
        content=payload.model_dump(),
        headers=headers,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s v%s | downstream=%s",
        settings.app_name,
        settings.app_version,
        settings.downstream_depo_agent_url,
    )
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown %s", settings.app_name)


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.get('/health')
async def health() -> Dict[str, Any]:
    return {'status': 'ok', 'service': settings.app_name, 'version': settings.app_version}


@app.get('/api/v1/.well-known/agent-card.json')
async def agent_card() -> Dict[str, Any]:
    return build_agent_card().model_dump(by_alias=True, mode='json')


@app.post('/api/v1/orchestrator')
async def orchestrator_rpc(
    request: Request,
    response: Response,
    x_trace_id: str | None = Header(default=None, alias='x-trace-id'),
    x_session_id: str | None = Header(default=None, alias='x-session-id'),
):
    raw_body = await request.json()
    rpc_method = raw_body.get('method', 'unknown') if isinstance(raw_body, dict) else 'unknown'
    rpc_id = raw_body.get('id') if isinstance(raw_body, dict) else None
    try:
        trace_id = _resolve_trace_id(x_trace_id)
    except ValueError as exc:
        trace_id = str(uuid.uuid4())
        logger.warning(
            "Invalid x-trace-id | method=%s original_trace_id=%s error=%s",
            rpc_method,
            x_trace_id,
            exc,
        )
        error_payload = JsonRpcErrorResponse(
            id=rpc_id,
            error=JsonRpcError(code=-32602, message=str(exc)),
        )
        return _json_response_with_trace(error_payload, trace_id, x_session_id)

    logger.info(
        "RPC request | method=%s trace_id=%s session_id=%s",
        rpc_method,
        trace_id,
        x_session_id,
    )

    try:
        rpc_request = JsonRpcRequest.model_validate(raw_body)
        rpc_response = await service.handle_rpc(rpc_request, x_trace_id=trace_id, x_session_id=x_session_id)
    except JsonRpcException as exc:
        logger.warning(
            "RPC error | method=%s code=%d message=%s trace_id=%s",
            rpc_method,
            exc.code,
            exc.message,
            trace_id,
        )
        error_payload = JsonRpcErrorResponse(
            id=rpc_id,
            error=JsonRpcError(code=exc.code, message=exc.message),
        )
        return _json_response_with_trace(error_payload, trace_id, x_session_id)
    except ValidationError as exc:
        logger.warning(
            "Validation error | method=%s trace_id=%s errors=%s",
            rpc_method,
            trace_id,
            exc.error_count(),
        )
        error_payload = JsonRpcErrorResponse(
            id=rpc_id,
            error=JsonRpcError(code=-32602, message=f"Ошибка валидации: {exc.error_count()} нарушение(й)"),
        )
        return _json_response_with_trace(error_payload, trace_id, x_session_id)
    except Exception:
        logger.exception(
            "Unexpected error | method=%s trace_id=%s",
            rpc_method,
            trace_id,
        )
        error_payload = JsonRpcErrorResponse(
            id=rpc_id,
            error=JsonRpcError(code=-32603, message='Внутренняя ошибка proxy-сервиса'),
        )
        return _json_response_with_trace(error_payload, trace_id, x_session_id)

    logger.debug(
        "RPC response | method=%s trace_id=%s",
        rpc_method,
        trace_id,
    )
    response.headers[TRACE_HEADER_NAME] = trace_id
    result_context_id = None
    result = rpc_response.result
    if hasattr(result, 'contextId'):
        result_context_id = result.contextId
    elif hasattr(result, 'status') and getattr(result, 'status', None) and getattr(result.status, 'message', None):
        result_context_id = result.status.message.contextId
    if result_context_id:
        response.headers['x-session-id'] = result_context_id
    elif x_session_id:
        response.headers['x-session-id'] = x_session_id

    return rpc_response.model_dump(mode='json')


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        await store.cleanup(settings.task_ttl_seconds)
        logger.debug("Cleanup loop iteration completed")
