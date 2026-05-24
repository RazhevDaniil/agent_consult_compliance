import uuid
import httpx

import asyncio
import numpy as np
import logging
from typing import Any, Optional
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi.openapi.utils import get_openapi
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, ORJSONResponse,Response
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.constants import START
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from . import startup_checkup
from .tracing import (
    aef_agent_start,
    aef_custom_span,
    aef_input_request,
    get_aef_handler,
    init_tracing,
    session_id_cvar,
)
from .graph import compiled_graph, checkpointer
from .models import ChatRequest, ChatResponse, IncorrectDealsReportRequest
from .kpk_tools import download_incorrect_deals_report
from .config import (
    settings,
    ERROR_TEXT,
    TTL_EXCEEDED_TEXT,
    GIGAPLATFORM_REJECTED_TEXT,
    DB_APP_URL,
    DB_APP_TIMEOUT_SEC,
    _TRACE_HEADER_NAME,
)
from .graph_llm_wrappers import (
    get_llm_stop_event,
    mark_llm_stop_event_if_needed,
    reset_llm_stop_event,
)


logger = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise AEF tracing first so synthetic startup probes also surface
    in AEF Manager. Then run SECURITY readiness probes and spawn the
    background re-check loop. Probe failures do NOT abort startup — the
    server stays up serving /health=200 while the loop waits for
    dependencies to recover. /ready stays 503 until all checks pass."""
    init_tracing()
    logger.info("consultant_app_starting")

    ok, failures = await startup_checkup.run_checks(source="startup")
    if ok:
        logger.info("consultant_app_ready")
    else:
        logger.warning(f"consultant_app_started_not_ready. failures: {failures}")

    recheck_task = asyncio.create_task(
        startup_checkup.recheck_loop(), name="readiness-recheck"
    )

    yield

    logger.info("consultant_app_shutting_down")
    recheck_task.cancel()
    try:
        await recheck_task
    except asyncio.CancelledError:
        pass
    logger.info("consultant_app_stopped")


app = FastAPI(
    title=settings.title,
    version=settings.version,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    default_response_class=ORJSONResponse,
    swagger_ui_parameters={
        "validatorUrl": None,
        "docExpansion": "none",
        "displayRequestDuration": True,
        "tryItOutEnabled": True,
    },
    lifespan=lifespan,
)

logger.info('---Start App AI-agent-system for depo pricing consultation---')

# настройки по работе с БД храним через контейнер таски (иначе GC может убить их до завершения)
_bg_tasks: set[asyncio.Task] = set()


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description="AI Agent for depo pricing consultation",
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


def _incorrect_report_cache_key(report_dt: str) -> str:
    return f"incorrect_deals_report:{report_dt}"


def _is_valid_cached_report(entry) -> bool:
    """A cached entry is reusable only if it's a successful download with
    non-empty content."""
    return (
        isinstance(entry, dict)
        and entry.get("status") == "success"
        and bool(entry.get("content"))
    )


async def _ensure_incorrect_deals_report_cached(
        chat_id: str,
        report_dt: str,
        auth_header: str | None = None,
        trace_id: str | None = None,
) -> dict:
    """Cache lives in checkpointed state.generated_reports; auth comes
    from the request, not from any store."""
    cache_key = _incorrect_report_cache_key(report_dt)
    config = {"configurable": {"thread_id": chat_id}}

    snap = await compiled_graph.aget_state(config)
    values = getattr(snap, "values", None) or {}
    reports = dict(values.get("generated_reports") or {})

    cached = reports.get(cache_key)
    if _is_valid_cached_report(cached):
        return cached

    result = await asyncio.to_thread(
        download_incorrect_deals_report,
        report_dt=report_dt,
        auth_header=auth_header,
        trace_id=trace_id,
    )
    reports[cache_key] = result
    # as_node=START anchors the write on threads that haven't yet run
    # the graph (e.g. a report fetched before any /chat call).
    await compiled_graph.aupdate_state(
        config, {"generated_reports": reports}, as_node=START,
    )
    return result


def gen_id(prefix: str = "") -> str:
    rand_part = "".join(np.random.choice(list("0123456789abcdef"), 16))
    return f"{prefix}{rand_part}"


def _resolve_trace_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError):
        raise HTTPException(status_code=400, detail="x-trace-id must be a valid UUID v4")
    if parsed.version != 4:
        raise HTTPException(status_code=400, detail="x-trace-id must be a UUID v4")
    return str(parsed)


# -------------------- Логика графа --------------------
async def invoke_graph_single_turn(
        chat_id: str,
        user_text: str,
        auth_header: str | None = None,
        trace_id: str | None = None,
        callbacks: list | None = None,
) -> dict:
    """
    Полный цикл шага:
    - кладём HumanMessage в state через add_messages reducer;
    - граф сам прибавит AIMessage (append_response_node) и при
      необходимости компактирует историю (summarize_history_node);
    - auth_header пробрасывается через configurable;
    - callbacks (AEFHandler) подмешиваются в config для SECURITY §20 трейса.
    """
    user_input = user_text.strip()
    reset_llm_stop_event()
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": chat_id,
            "auth_header": auth_header,
            "trace_id": trace_id,
        }
    }
    if callbacks:
        config["callbacks"] = callbacks

    try:
        final_state = await asyncio.wait_for(
            compiled_graph.ainvoke(
                {
                    "input": user_input,
                    "messages": [HumanMessage(content=user_input)],
                    "regen_attempts": 0,
                    "is_context_identical": False,
                },
                config=config,
            ),
            timeout=settings.graph_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.error(f"graph_timeout_exceeded: {chat_id}")
        return {
            "answer": TTL_EXCEEDED_TEXT,
            "destination": "error",
            "confidence": 0,
            "sources": [],
            "response_state": "failed",
            "stop_event": "ttl_exceeded",
            "state": {},
        }
    except Exception as e:
        stop_event = mark_llm_stop_event_if_needed(e) or get_llm_stop_event()
        if stop_event:
            logger.warning(
                "graph_ainvoke_stopped_by_llm_event: chat_id=%s input_len=%s stop_event=%s",
                chat_id,
                len(user_input),
                stop_event,
            )
            return {
                "answer": GIGAPLATFORM_REJECTED_TEXT,
                "destination": "error",
                "confidence": 0,
                "sources": [],
                "response_state": "failed",
                "stop_event": stop_event,
                "state": {},
            }
        logger.error(
            "graph_ainvoke_error: chat_id=%s input_len=%s error=%s",
            chat_id,
            len(user_input),
            e,
            exc_info=True,
        )
        return {
            "answer": ERROR_TEXT,
            "destination": "error",
            "confidence": 0,
            "sources": [],
            "response_state": "failed",
            "state": {},
        }

    stop_event = get_llm_stop_event()
    if stop_event:
        return {
            "answer": GIGAPLATFORM_REJECTED_TEXT,
            "destination": "error",
            "confidence": 0,
            "sources": [],
            "response_state": "failed",
            "stop_event": stop_event,
            "state": final_state,
        }

    answer_text = final_state.get(
        "final_answer",
        final_state.get("answer", "Извините, произошла непредвиденная ошибка. Ответ не был сформирован.")
    )

    logger.info(f"chat : {chat_id}; agent response: {answer_text}")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": "Успешно получили ответ агента"}})

    destination = final_state.get("destination", "rag_methodology")
    confidence = final_state.get("confidence_score", None)
    sources = sorted(set(final_state.get("sources") or []))
    response_state = final_state.get("response_state", "completed")
    generated_report = final_state.get("generated_report")

    logger.info(f"Processed chat request for chat_id: {chat_id}")

    return {
        "answer": answer_text,
        "destination": destination,
        "confidence": confidence,
        "sources": sources,
        "response_state": response_state,
        "generated_report": generated_report,
        "state": final_state,
    }


def _spawn_bg(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def _post_consultant_log(payload: dict) -> None:
    """Best-effort log to db_app. Никогда не бросает — отказ логирования не
    должен ломать чат.

    The httpx call is auto-instrumented (OpenTelemetry → `output_request`).
    `aef_custom_span` tags this as a mutating action (insert into the
    consultant_agent_log table) per SECURITY §20 (4).
    """
    trace_id = payload.get("trace_id")
    with aef_custom_span(span_attributes={
        "aef.kind": "other",
        "aef.action": "db_app.consultant_log",
        "aef.is_mutation": True,
        "aef.rollback_possible": False,
        "aef.trace_id": trace_id,
        _TRACE_HEADER_NAME: trace_id,
    }):
        try:
            json_payload = dict(payload)
            trace_id = json_payload.pop("trace_id", None)
            headers = {_TRACE_HEADER_NAME: trace_id} if trace_id else {}
            async with httpx.AsyncClient(timeout=DB_APP_TIMEOUT_SEC) as client:
                resp = await client.post(
                    f"{DB_APP_URL}/v1/consultant-agent/logs",
                    json=json_payload,
                    headers=headers,
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "consultant_log_post_failed status_code=%s body=%s",
                        resp.status_code,
                        resp.text[:500],
                    )
        except Exception as e:
            logger.warning(f"consultant_log_post_failed: {e}")


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest,
    raw_request: Request,
    raw_response: Response,
    authorization: Optional[str] = Header(default=None),
    x_trace_id: Optional[str] = Header(default=None, alias="x-trace-id"),
    agent_audience: Optional[str] = Header(default=None, alias="X-Agent-Audience"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_source_system: Optional[str] = Header(default=None, alias="X-Source-System"),
):
    user_message = (request.message or "").strip()
    if len(user_message) > settings.max_user_chars:
        logger.error("Too long input query")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {
            "object_name": f"Too long input query: {len(user_message)}. Max length is {settings.max_user_chars}."}
                    }
                   )
        raise HTTPException(
            status_code=413,
            detail=f"Too long input query: {len(user_message)}. Max length is {settings.max_user_chars}.",
        )

    if authorization:
        logger.info(f"auth_header_received")
        # auth_header is now passed per-request through LangGraph's
        # configurable (Шаг 2.4); not persisted in the store anymore.
    if agent_audience:
        logger.info("agent_audience_received")

    # `message_id` is the consultant_agent_log UNIQUE key; AEF SDK does not
    # know about it, so we still generate one locally. SDK provides
    # `trace_id` for cross-span correlation; we drop the legacy `run_id`.
    message_id = str(uuid.uuid4())
    question_dttm = datetime.now(timezone.utc)
    user_id = (
        x_user_id
        or getattr(request, "user_id", None)
        or request.chat_id  # fallback: один человек = один chat_id
    )
    source_system = x_source_system or getattr(request, "source_system", None) or "support"
    trace_id = _resolve_trace_id(x_trace_id)
    raw_response.headers[_TRACE_HEADER_NAME] = trace_id

    session_id_cvar.set(request.chat_id)

    input_body = {
        "chat_id": request.chat_id,
        "x_trace_id": trace_id,
        "message_len": len(request.message or ""),
        "source_system": source_system,
    }

    with aef_input_request(
        span_name="chat",
        headers=dict(raw_request.headers),
        body=input_body,
        path="/chat",
        method="POST",
    ) as input_req:
        try:
            with aef_agent_start(input=input_body) as agent_span:
                agent_span.add_span_attributes(**{
                    "aef.agent_uid": settings.aef_agent_id,
                    "aef.session_id": request.chat_id,
                    "aef.trace_id": trace_id,
                    _TRACE_HEADER_NAME: trace_id,
                    "aef.ttl": settings.graph_timeout_sec,
                    "aef.hops": None,
                    "aef.stop_event": None,
                })

                final_state = await invoke_graph_single_turn(
                    chat_id=request.chat_id,
                    user_text=request.message,
                    auth_header=authorization,
                    trace_id=trace_id,
                    callbacks=[get_aef_handler()],
                )

                response_state = final_state.get("response_state", "completed")
                stop_event = final_state.get("stop_event")
                if stop_event:
                    agent_span.add_span_attributes(**{"aef.stop_event": stop_event})
                elif response_state == "failed":
                    agent_span.add_span_attributes(**{"aef.stop_event": "phase_error"})

                generated_report = final_state.get("generated_report") or {}
                report_dt = generated_report.get("report_dt")
                if generated_report.get("kind") == "incorrect_deals_report" and report_dt:
                    try:
                        await _ensure_incorrect_deals_report_cached(
                            request.chat_id,
                            report_dt,
                            auth_header=authorization,
                            trace_id=trace_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "incorrect_deals_report_prefetch_failed chat_id=%s report_dt=%s error=%s",
                            request.chat_id,
                            report_dt,
                            e,
                        )

                answer_text = final_state.get("answer", "Извините, произошла непредвиденная ошибка.")
                destination = final_state.get("destination", "rag_methodology")

                agent_span.add_output_result(output={
                    "destination": destination,
                    "response_state": response_state,
                    "answer_len": len(answer_text or ""),
                })

            # Лог в db_app — в фоне, не блокирует ответ
            _spawn_bg(_post_consultant_log({
                "id": message_id,
                "session_id": request.chat_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "user_id": user_id,
                "question": request.message,
                "agent_branch": destination,
                "agent_answer": answer_text,
                "question_dttm": question_dttm.isoformat(),
                "answer_dttm": datetime.now(timezone.utc).isoformat(),
                "source_system": source_system,
            }))

            response_body = {
                "answer": answer_text,
                "destination": destination,
                "confidence": final_state.get("confidence", None),
                "sources": final_state.get("sources", []),
                "state": response_state,
                "generated_report": generated_report or None,
                "suggests": [],
            }
            input_req.add_response(
                headers={_TRACE_HEADER_NAME: trace_id},
                body=response_body,
                http_code=200,
            )
            return response_body

        except Exception as e:
            logger.error(f"chat_request_failed: {e}")
            # Логируем и упавшие запросы — иначе error rate в дашборде будет занижен
            _spawn_bg(_post_consultant_log({
                "id": message_id,
                "session_id": request.chat_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "user_id": user_id,
                "question": request.message,
                "agent_branch": "error",
                "agent_answer": f"Agent error: {str(e)}",
                "question_dttm": question_dttm.isoformat(),
                "answer_dttm": datetime.now(timezone.utc).isoformat(),
                "source_system": source_system,
            }))
            input_req.add_response(
                headers={_TRACE_HEADER_NAME: trace_id},
                body={"error": str(e)},
                http_code=500,
            )
            raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")


@app.post("/api/kpk/incorrect-deals-report")
async def incorrect_deals_report_endpoint(
        request: IncorrectDealsReportRequest,
        raw_response: Response,
        authorization: Optional[str] = Header(default=None),
        x_trace_id: Optional[str] = Header(default=None, alias="x-trace-id"),
):
    trace_id = _resolve_trace_id(x_trace_id)
    raw_response.headers[_TRACE_HEADER_NAME] = trace_id
    result = await _ensure_incorrect_deals_report_cached(
        chat_id=request.chat_id,
        report_dt=request.report_dt,
        auth_header=authorization,
        trace_id=trace_id,
    )
    if result.get("status") != "success":
        raise HTTPException(status_code=502, detail=result.get("error") or ERROR_TEXT)

    filename = result.get("filename") or f"new_deals_report_{request.report_dt.replace('-', '')}.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    headers[_TRACE_HEADER_NAME] = trace_id
    return Response(
        content=result.get("content") or b"",
        media_type=result.get("media_type") or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.get("/health")
async def health_check():
    """Liveness probe — always 200 while the process is up."""
    return {"status": "healthy", "timestamp": datetime.now()}


@app.get("/ready")
async def readiness_check():
    """Readiness probe — 200 only after startup checks pass; otherwise 503
    with the latest failure list so operators can see which dependency is
    keeping the pod out of rotation."""
    if startup_checkup.is_ready():
        return {"status": "ready", "timestamp": datetime.now()}
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "timestamp": datetime.now().isoformat(),
            "failures": startup_checkup.get_failures(),
        },
    )


@app.get("/chat/{chat_id}/history")
async def get_chat_history(chat_id: str):
    try:
        config = {"configurable": {"thread_id": chat_id}}
        snap = await compiled_graph.aget_state(config)
        values = getattr(snap, "values", None) or {}
        history = list(values.get("messages") or [])

        simple_history = []
        for msg in history:
            if hasattr(msg, 'type'):
                if msg.type == 'human':
                    simple_history.append({"role": "user", "content": msg.content})
                elif msg.type == 'ai':
                    simple_history.append({"role": "assistant", "content": msg.content})
            else:
                simple_history.append({"role": "unknown", "content": str(msg)})

        return {
            "chat_id": chat_id,
            "history": simple_history,
            "message_count": len(history)
        }
    except Exception as e:
        logger.error(f"get_chat_history_failed: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving history: {str(e)}")


@app.post("/chat/{chat_id}/reset")
async def reset_chat(chat_id: str):
    # Clear the entire checkpointed slice for this thread. For `messages`
    # we pipe through the add_messages reducer using a single tombstone
    # `RemoveMessage(id=REMOVE_ALL_MESSAGES)`, which clears the list.
    config = {"configurable": {"thread_id": chat_id}}
    await compiled_graph.aupdate_state(
        config,
        {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
            "last_tool_payload": None,
            "last_tool_kind": None,
            "last_selector": None,
            "generated_reports": {},
            "tool_cache": {},
            "pending_kpk_date_choice": None,
        },
        as_node=START,
    )
    return {"status": "success", "message": "Chat history reset"}
