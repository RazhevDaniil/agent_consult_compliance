from aef_tracing import (
    AEFBatchSpanProcessor,
    AEFHandler,
    AEFTracerProvider,
    aef_agent_start,
    aef_custom_span,
    aef_input_request,
    aef_kafka_consume,
    aef_kafka_produce,
    aef_observation,
)
from aef_tracing.exporters import AEFKafkaSender, AEFProtobufSenderExporter
from aef_tracing.span_processors import session_id_cvar

import logging

from .config import settings

# The AEF SDK itself writes the `sdk-list` Kafka header — its own entry for
# aef-tracing is added automatically (docs_for_SDK/instructions/tracing_usage.md
# §2.3). We do not enumerate langchain/langgraph here: those are informational
# inventory entries, not part of the obligatory contract for prototype agents.
# If ПСИ/ПРОМ audit requests them, add `sdk_list_headers=[SDKEntry(...), ...]`
# to the AEFKafkaSender(...) call.

logger = logging.getLogger(__name__)

_HANDLER: AEFHandler | None = None


def init_tracing() -> AEFHandler:
    """Build the tracer provider/sender/exporter/handler once.

    Idempotent — subsequent calls return the same handler. Must be invoked
    from the FastAPI lifespan startup so AEF spans surface for synthetic
    startup probes as well as production traffic.
    """
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER
        
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
    logger.info("AEF Tracing initialized successfully")
    return _HANDLER


def get_aef_handler() -> AEFHandler:
    """Return the singleton handler. Raises if `init_tracing()` wasn't called."""
    if _HANDLER is None:
        raise RuntimeError(
            "AEF tracing not initialised — call init_tracing() in lifespan first"
        )
    return _HANDLER


__all__ = [
    "init_tracing",
    "get_aef_handler",
    "aef_input_request",
    "aef_agent_start",
    "aef_kafka_produce",
    "aef_kafka_consume",
    "aef_custom_span",
    "aef_observation",
    "session_id_cvar",
]
