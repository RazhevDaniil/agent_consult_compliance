import json
import logging
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from .config import settings

logger = logging.getLogger(__name__)

try:
    from aef_tracing import (
        AEFBatchSpanProcessor,
        AEFHandler,
        AEFTracerProvider,
        aef_agent_start as _raw_aef_agent_start,
        aef_custom_span as _raw_aef_custom_span,
        aef_input_request as _raw_aef_input_request,
        aef_kafka_consume as _raw_aef_kafka_consume,
        aef_kafka_produce as _raw_aef_kafka_produce,
        aef_observation as _raw_aef_observation,
    )
    from aef_tracing.exporters import AEFKafkaSender, AEFProtobufSenderExporter
    from aef_tracing.span_processors import session_id_cvar
except Exception as e:
    AEFBatchSpanProcessor = None
    AEFHandler = None
    AEFTracerProvider = None
    AEFKafkaSender = None
    AEFProtobufSenderExporter = None
    _raw_aef_agent_start = None
    _raw_aef_custom_span = None
    _raw_aef_input_request = None
    _raw_aef_kafka_consume = None
    _raw_aef_kafka_produce = None
    _raw_aef_observation = None
    session_id_cvar = ContextVar("session_id", default=None)
    _AEF_IMPORT_ERROR: Exception | None = e
else:
    _AEF_IMPORT_ERROR = None


class _NoopSpan:
    def add_span_attributes(self, **attrs) -> None:
        return None

    def add_output_result(self, output=None, **kwargs) -> None:
        return None

    def add_response(self, headers=None, body=None, http_code=None) -> None:
        return None


_HANDLER: Any | None = None
_TRACING_DISABLED = _AEF_IMPORT_ERROR is not None
_HOP_COUNTS: dict[str, int] = {}
_HOP_LOCK = threading.Lock()
_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar("aef_current_trace_id", default=None)
_CURRENT_OPERATION_UID: ContextVar[str | None] = ContextVar("aef_current_operation_uid", default=None)


def reset_hops(trace_id: str | None) -> None:
    if not trace_id:
        return
    with _HOP_LOCK:
        _HOP_COUNTS[trace_id] = 0


def record_hop(trace_id: str | None) -> int:
    if not trace_id:
        return 0
    with _HOP_LOCK:
        _HOP_COUNTS[trace_id] = _HOP_COUNTS.get(trace_id, 0) + 1
        return _HOP_COUNTS[trace_id]


def get_hops(trace_id: str | None) -> int:
    if not trace_id:
        return 0
    with _HOP_LOCK:
        return _HOP_COUNTS.get(trace_id, 0)


def current_trace_id() -> str | None:
    return _CURRENT_TRACE_ID.get()


def current_operation_uid() -> str | None:
    return _CURRENT_OPERATION_UID.get()


@contextmanager
def trace_operation_context(trace_id: str | None = None, operation_uid: str | None = None):
    trace_token = _CURRENT_TRACE_ID.set(trace_id)
    operation_token = _CURRENT_OPERATION_UID.set(operation_uid)
    try:
        yield
    finally:
        _CURRENT_OPERATION_UID.reset(operation_token)
        _CURRENT_TRACE_ID.reset(trace_token)


def safe_trace_payload(value: Any, max_chars: int | None = None) -> Any:
    """Serialize trace payloads without raising and without sending huge blobs."""
    if value is None:
        return None
    limit = max_chars or getattr(settings, "tracing_max_payload_size", 10000)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary len={len(value)}>"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        try:
            return json.loads(text)
        except Exception:
            return text
    return text[:limit] + "...<truncated>"


def safe_add_span_attributes(span: Any, **attrs) -> None:
    try:
        cleaned = _sanitize_span_attributes(attrs)
        if cleaned and hasattr(span, "add_span_attributes"):
            span.add_span_attributes(**cleaned)
    except Exception as e:
        logger.warning("aef_add_span_attributes_failed: %s", e)


def safe_add_output_result(span: Any, output: Any) -> None:
    try:
        if hasattr(span, "add_output_result"):
            span.add_output_result(output=safe_trace_payload(output))
    except Exception as e:
        logger.warning("aef_add_output_result_failed: %s", e)


def _sanitize_span_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set, bytes, bytearray, memoryview)):
            cleaned[key] = json.dumps(safe_trace_payload(value), ensure_ascii=False, default=str)
        else:
            cleaned[key] = value
    return cleaned


def _sanitize_context_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if "span_attributes" not in kwargs or not isinstance(kwargs["span_attributes"], dict):
        return kwargs
    patched = dict(kwargs)
    patched["span_attributes"] = _sanitize_span_attributes(kwargs["span_attributes"])
    return patched


@contextmanager
def _safe_aef_context(raw_factory: Callable | None, context_name: str, *args, **kwargs):
    if _TRACING_DISABLED or raw_factory is None:
        yield _NoopSpan()
        return

    cm = None
    try:
        kwargs = _sanitize_context_kwargs(kwargs)
        cm = raw_factory(*args, **kwargs)
        span = cm.__enter__()
    except Exception as e:
        logger.warning("aef_%s_enter_failed: %s", context_name, e)
        yield _NoopSpan()
        return

    try:
        yield span or _NoopSpan()
    except BaseException:
        exc_info = sys.exc_info()
        try:
            suppress = bool(cm.__exit__(*exc_info))
        except Exception as e:
            logger.warning("aef_%s_exit_failed: %s", context_name, e)
            suppress = False
        if not suppress:
            raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:
            logger.warning("aef_%s_exit_failed: %s", context_name, e)


def aef_input_request(*args, **kwargs):
    return _safe_aef_context(_raw_aef_input_request, "input_request", *args, **kwargs)


def aef_agent_start(*args, **kwargs):
    return _safe_aef_context(_raw_aef_agent_start, "agent_start", *args, **kwargs)


def aef_custom_span(*args, **kwargs):
    return _safe_aef_context(_raw_aef_custom_span, "custom_span", *args, **kwargs)


def aef_kafka_produce(*args, **kwargs):
    return _safe_aef_context(_raw_aef_kafka_produce, "kafka_produce", *args, **kwargs)


def aef_kafka_consume(*args, **kwargs):
    return _safe_aef_context(_raw_aef_kafka_consume, "kafka_consume", *args, **kwargs)


def aef_observation(*args, **kwargs):
    return _safe_aef_context(_raw_aef_observation, "observation", *args, **kwargs)


def init_tracing() -> Any | None:
    """Build the tracer provider/sender/exporter/handler once.

    Tracing is best-effort: SDK/Kafka failures never prevent the agent from
    starting or serving requests. In that case span context managers degrade
    to no-op spans and LangChain callbacks are omitted.
    """
    global _HANDLER, _TRACING_DISABLED
    if _HANDLER is not None:
        return _HANDLER
    if _AEF_IMPORT_ERROR is not None:
        logger.warning("aef_tracing_import_failed: %s", _AEF_IMPORT_ERROR)
        _TRACING_DISABLED = True
        return None

    try:
        logger.info(
            "aef_tracing_config. kafka_hosts=%r outbox_topic=%r "
            "security_protocol=%r max_request_size=%d "
            "agent_id=%r cluster_id=%r namespace=%r distributive=%r",
            settings.kafka_hosts,
            settings.tracing_service_kafka_outbox_topic,
            settings.aef_kafka_security_protocol,
            settings.aef_kafka_max_request_size,
            settings.aef_agent_id,
            settings.aef_cluster_id,
            settings.aef_namespace,
            settings.aef_distributive,
        )

        sender = AEFKafkaSender(
            outbox_topic=settings.tracing_service_kafka_outbox_topic,
            kafka_producer_config={
                "bootstrap_servers": settings.kafka_hosts,
                "security_protocol": settings.aef_kafka_security_protocol,
                "max_request_size": settings.aef_kafka_max_request_size,
            },
            headers={
                "agent-id": settings.aef_agent_id,
                "cluster-id": settings.aef_cluster_id,
                "namespace": settings.aef_namespace,
                "distributive": settings.aef_distributive,
            },
        )
        provider = AEFTracerProvider()
        provider.add_span_processor(
            AEFBatchSpanProcessor(AEFProtobufSenderExporter(senders=[sender]))
        )
        _HANDLER = AEFHandler(tracer=provider.get_tracer(__name__))
        _TRACING_DISABLED = False
        if not settings.aef_langchain_callbacks_enabled:
            logger.info("AEF LangChain callbacks disabled; manual AEF spans remain active")
        logger.info("AEF Tracing initialized successfully")
        return _HANDLER
    except Exception as e:
        _HANDLER = None
        _TRACING_DISABLED = True
        logger.warning("aef_tracing_init_failed_best_effort: %s", e, exc_info=True)
        return None


def get_aef_handler() -> Any | None:
    return _HANDLER


def get_aef_callbacks() -> list[Any]:
    if not settings.aef_langchain_callbacks_enabled:
        return []
    return [_HANDLER] if _HANDLER is not None else []


__all__ = [
    "init_tracing",
    "get_aef_handler",
    "get_aef_callbacks",
    "aef_input_request",
    "aef_agent_start",
    "aef_kafka_produce",
    "aef_kafka_consume",
    "aef_custom_span",
    "aef_observation",
    "session_id_cvar",
    "reset_hops",
    "record_hop",
    "get_hops",
    "current_trace_id",
    "current_operation_uid",
    "trace_operation_context",
    "safe_trace_payload",
    "safe_add_span_attributes",
    "safe_add_output_result",
]
