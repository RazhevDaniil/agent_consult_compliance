import json
import hashlib
import uuid
import httpx
import dataclasses
import logging
from decimal import Decimal
from datetime import date, datetime
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from typing import Literal, Tuple, Dict, Any, Optional

from .config import ADAPTERS_API_BASE_URL, TIMEOUT, ERROR_TEXT, _AUTH_HEADER_NAME, _TRACE_HEADER_NAME, settings
from .tracing import (
    aef_custom_span,
    get_hops,
    record_hop,
    safe_add_output_result,
    safe_add_span_attributes,
    safe_trace_payload,
)

from .tech_funcs import compute_deal_signature, _normalize_context_from_inputs

from .models import UnifiedRequest, PipelineResponse, ReportRequest, ComponentsResponse


_LOGGER = logging.getLogger(__name__)

ToolName = Literal["pricing", "limits", "both"]


def _response_body_preview(response: httpx.Response, max_chars: int = 1000) -> str:
    return (response.text or "")[:max_chars].replace("\n", "\\n")


def _payload_summary(payload) -> dict:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}

    data = payload.get("data")
    data_summary = {"type": type(data).__name__}
    if isinstance(data, dict):
        components = data.get("components")
        explain_map = data.get("explain_map")
        inputs_used = data.get("inputs_used")
        data_summary.update({
            "keys": sorted(data.keys()),
            "components_count": len(components or {}) if isinstance(components, dict) else None,
            "component_keys": sorted((components or {}).keys()) if isinstance(components, dict) else None,
            "explain_count": len(explain_map or {}) if isinstance(explain_map, dict) else None,
            "inputs_used_keys": sorted((inputs_used or {}).keys()) if isinstance(inputs_used, dict) else None,
        })
    elif isinstance(data, str):
        data_summary["len"] = len(data)

    return {
        "type": "dict",
        "keys": sorted(payload.keys()),
        "status": payload.get("status"),
        "error": payload.get("error"),
        "data": data_summary,
    }


def _dump_response_diag(response: httpx.Response, *, url: str, log_prefix: str) -> None:
    content_attr = getattr(response, "_content", None)
    if content_attr is None:
        content_state = "None"
    else:
        content_state = f"len={len(content_attr)} preview={content_attr[:200]!r}"

    _LOGGER.error(
        "%s_response_diag. url=%s encoding=%s _content=%s "
        "is_closed=%s is_stream_consumed=%s headers=%s",
        log_prefix,
        url,
        response.encoding,
        content_state,
        response.is_closed,
        response.is_stream_consumed,
        dict(response.headers),
    )


def _decode_json_response(response: httpx.Response, *, url: str, log_prefix: str):
    preview = _response_body_preview(response)
    _LOGGER.info(
        "%s_http_response. status=%s content_type=%s content_length=%s url=%s body_preview=%r",
        log_prefix,
        response.status_code,
        response.headers.get("content-type"),
        response.headers.get("content-length"),
        url,
        preview,
    )
    try:
        payload = response.json()
    except ValueError as e:
        _LOGGER.error(
            "%s_non_json_response. status=%s content_type=%s url=%s body_preview=%r error=%s",
            log_prefix,
            response.status_code,
            response.headers.get("content-type"),
            url,
            preview,
            e,
        )
        _dump_response_diag(response, url=url, log_prefix=log_prefix)
        raise

    _LOGGER.info("%s_json_payload. summary=%s", log_prefix, _payload_summary(payload))
    return payload


def _retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _log_http_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    _LOGGER.info(f"http_retry. attempt={retry_state.attempt_number}. next_wait_sec={retry_state.next_action.sleep}. exc={exc}")


def _http_retrying() -> Retrying:
    return Retrying(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(settings.http_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.http_retry_base,
            max=settings.http_retry_max,
        ),
        before_sleep=_log_http_retry,
        reraise=True,
    )


def _auth_headers(auth_header: str | None, trace_id: str | None = None) -> Dict[str, str]:
    """Build the auth headers dict from the request-scoped header.
    auth_header comes from LangGraph's configurable.auth_header
    (Шаг 2.4) and is supplied per ainvoke().
    """
    headers: Dict[str, str] = {}
    if auth_header:
        headers[_AUTH_HEADER_NAME] = auth_header
    if trace_id:
        headers[_TRACE_HEADER_NAME] = trace_id
    return headers


def _json_default(obj):
    """Безопасная сериализация для кеш-ключа."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, set):
        return sorted(list(obj))
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def _key_for_cache(tool: str, user_text: str, explicit: dict | None, prefetched_row: dict | None = None) -> str:
    signature = ""
    if explicit:
        signature = compute_deal_signature(explicit)
    if not signature and prefetched_row:
        signature = compute_deal_signature(_normalize_context_from_inputs(prefetched_row))
    payload = {"tool": tool, "signature": signature, "explicit": explicit or {}}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _selector_to_dict(sel):
    try:
        return {
            "deal_id": getattr(sel, "deal_id", None),
            "inn": getattr(sel, "inn", None),
            "product": getattr(sel, "product", None),
            "currency": getattr(sel, "currency", None),
            "amount": getattr(sel, "amount", None),
            "deal_dt": str(getattr(sel, "deal_dt", None)),
            "maturity_dt": str(getattr(sel, "maturity_dt", None)),
            "term": getattr(sel, "term", None),
            "interest_rate": getattr(sel, "interest_rate", None),
        }
    except Exception:
        return {}

def execute_tool(
        tool: ToolName,
        user_text: str,
        explicit: Dict[str, Any] | None = None,
        *,
        prebuilt_selector=None,
        prefetched_row: Optional[dict] = None,
        auth_header: str | None = None,
        trace_id: str | None = None,
        operation_uid: str | None = None,
        tool_cache: Optional[Dict[str, dict]] = None,
) -> Tuple[dict, bool, Dict[str, dict]]:
    """Шаг 2.5: stateless w.r.t. InMemoryChatStore.

    `tool_cache` (a dict, defaults to empty) is the prior cache slice
    pulled from GraphState. Returns (res, from_cache, new_cache) where
    new_cache contains the prior entries plus the just-computed one
    (if the call succeeded). The caller is responsible for persisting
    new_cache back into state.
    """
    cache_in: Dict[str, dict] = dict(tool_cache or {})
    key = _key_for_cache(tool, user_text, explicit, prefetched_row)
    cached = cache_in.get(key)
    if cached:
        return cached, True, cache_in

    if tool == "pricing":
        endpoint = "/api/pricing/from-selector"
    elif tool == "limits":
        endpoint = "/api/limits/from-selector"
    elif tool == "both":
        endpoint = "/api/both/from-selector"
    else:
        return {"status": "error", "error": f"Unknown tool: {tool}"}, False, cache_in

    if ADAPTERS_API_BASE_URL is None:
        return {"status": "error", "error": ERROR_TEXT}, False, cache_in

    url = f"{ADAPTERS_API_BASE_URL.rstrip('/')}{endpoint}"

    try:
        # 1. Подготовка данных
        selector_data = _selector_to_dict(prebuilt_selector) if prebuilt_selector else {}

        # Мержим explicit, если он есть
        raw_data = {**selector_data, **(explicit or {})}

        headers = _auth_headers(auth_header, trace_id)
        if not headers.get(_AUTH_HEADER_NAME):
            _LOGGER.info("pss_request_no_auth")

        # 2. Создание Pydantic модели запроса
        request_model = UnifiedRequest(
            **raw_data,
            prefetched_row=prefetched_row
        )

        body = request_model.model_dump(exclude_none=True)
        _LOGGER.info(f"pss_request. req = {body}")

        service_operation_uid = str(uuid.uuid4())
        with aef_custom_span(span_attributes={
            "aef.kind": "service_call",
            "aef.call_type": "api_call",
            "aef.action": f"pss.{tool}",
            "aef.target_name": "PALM.Security/PSS",
            "aef.is_mutation": False,
            "aef.rollback_possible": None,
            "aef.trace_id": trace_id,
            "aef.operation_uid": service_operation_uid,
            "aef.parent_operation_uid": operation_uid or trace_id,
            _TRACE_HEADER_NAME: trace_id,
            "http.method": "POST",
            "http.url": url,
            "aef.request_payload": safe_trace_payload(body),
        }) as span:
            # 3. Отправка запроса (SECURITY §22: retry on 5xx/429/network)
            for attempt in _http_retrying():
                with attempt:
                    record_hop(trace_id)
                    response = httpx.post(
                        url,
                        json=body,
                        headers=headers,
                        timeout=TIMEOUT
                    )
                    safe_add_span_attributes(
                        span,
                        **{
                            "http.status_code": response.status_code,
                            "aef.hops_used": get_hops(trace_id),
                        },
                    )
                    if _retryable_status(response.status_code):
                        response.raise_for_status()
            response.raise_for_status()

            # 4. Валидация ответа через PipelineResponse
            payload = _decode_json_response(response, url=url, log_prefix="pss")
            safe_add_span_attributes(span, **{"aef.response_payload": safe_trace_payload(_payload_summary(payload))})
            safe_add_output_result(span, output=_payload_summary(payload))
            try:
                api_response = PipelineResponse(**payload)
            except Exception as e:
                _LOGGER.error(
                    "pss_response_validation_failed. error=%s payload_summary=%s",
                    e,
                    _payload_summary(payload),
                )
                safe_add_span_attributes(span, **{"aef.error_message": str(e)})
                raise

        res = api_response.model_dump()
        _LOGGER.info(f"pss_response_ok. status = {res.get('status')}")

    except Exception as e:
        _LOGGER.error(f"pss_request_failed. error: {e}")
        return {
            "status": "error",
            "error": ERROR_TEXT
        }, False, cache_in

    cache_out = dict(cache_in)
    if res.get("status") == "success":
        cache_out[key] = res

    return res, False, cache_out


def execute_report_tool(
        inns: Optional[list] = None,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        auth_header: str | None = None,
        trace_id: str | None = None,
        operation_uid: str | None = None,
):
    if ADAPTERS_API_BASE_URL is None:
        return {"status": "error", "error": ERROR_TEXT}

    url = ADAPTERS_API_BASE_URL.rstrip('/') + "/api/report"

    try:
        # 1. Создаем валидную модель запроса
        request_model = ReportRequest(
            period_start=period_start,
            period_end=period_end,
            inns=inns
        )

        body = request_model.model_dump(exclude_none=True)
        _LOGGER.info(f"report_request. req = {body}")

        headers = _auth_headers(auth_header, trace_id)
        if not headers.get(_AUTH_HEADER_NAME):
            _LOGGER.info("report_request_no_auth")

        service_operation_uid = str(uuid.uuid4())
        with aef_custom_span(span_attributes={
            "aef.kind": "service_call",
            "aef.call_type": "api_call",
            "aef.action": "pss.report",
            "aef.target_name": "PALM.Security/PSS",
            "aef.is_mutation": False,
            "aef.rollback_possible": None,
            "aef.trace_id": trace_id,
            "aef.operation_uid": service_operation_uid,
            "aef.parent_operation_uid": operation_uid or trace_id,
            _TRACE_HEADER_NAME: trace_id,
            "http.method": "POST",
            "http.url": url,
            "aef.request_payload": safe_trace_payload(body),
        }) as span:
            # 2. Отправляем (SECURITY §22: retry on 5xx/429/network)
            for attempt in _http_retrying():
                with attempt:
                    record_hop(trace_id)
                    response = httpx.post(
                        url,
                        json=body,
                        headers=headers,
                        timeout=TIMEOUT
                    )
                    safe_add_span_attributes(
                        span,
                        **{
                            "http.status_code": response.status_code,
                            "aef.hops_used": get_hops(trace_id),
                        },
                    )
                    if _retryable_status(response.status_code):
                        response.raise_for_status()
            response.raise_for_status()

            # 3. Валидируем ответ через общий PipelineResponse
            payload = _decode_json_response(response, url=url, log_prefix="report")
            safe_add_span_attributes(span, **{"aef.response_payload": safe_trace_payload(_payload_summary(payload))})
            safe_add_output_result(span, output=_payload_summary(payload))
            try:
                api_response = PipelineResponse(**payload)
            except Exception as e:
                _LOGGER.error(
                    "report_response_validation_failed. error=%s payload_summary=%s",
                    e,
                    _payload_summary(payload),
                )
                safe_add_span_attributes(span, **{"aef.error_message": str(e)})
                raise
        res = api_response.model_dump()

        _LOGGER.info(f"report_response. status = {res.get('status')}")

    except Exception as e:
        _LOGGER.error(f"report_request_failed. Error: {e}")
        return {"status": "error", "error": ERROR_TEXT}

    if res.get("status") != "success":
        _LOGGER.error(f"report_response_not_success. error: {res.get('error')}")
        return {
            "status": "error",
            "error": res.get("error", "Unknown error")
        }

    return {
        "status": "success",
        "result": res["data"]
    }
