import json
import re
import httpx
import logging
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import ADAPTERS_API_BASE_URL, TIMEOUT, ERROR_TEXT, _AUTH_HEADER_NAME, _TRACE_HEADER_NAME, settings

from .kpk_models import KpkLimitToolRequest, KpkLimitToolResponse

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')


def _response_body_preview(response: httpx.Response, max_chars: int = 1000) -> str:
    return (response.text or "")[:max_chars].replace("\n", "\\n")


def _payload_summary(payload) -> dict:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}

    data = payload.get("data")
    data_summary = {"type": type(data).__name__}
    if isinstance(data, dict):
        data_summary.update({
            "keys": sorted(data.keys()),
            "mode": data.get("mode"),
            "division_cd": data.get("division_cd"),
            "report_dt": data.get("report_dt"),
            "prev_report_dt": data.get("prev_report_dt"),
            "limit_amt": data.get("limit_amt"),
            "prev_limit_amt": data.get("prev_limit_amt"),
            "delta_limit_amt": data.get("delta_limit_amt"),
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


def _auth_headers(auth_header: str | None, trace_id: str | None = None) -> dict:
    """Build the auth headers dict from a request-scoped header
    supplied through LangGraph's configurable (Шаг 2.4)."""
    headers = {}
    if auth_header:
        headers[_AUTH_HEADER_NAME] = auth_header
    if trace_id:
        headers[_TRACE_HEADER_NAME] = trace_id
    return headers


def _retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _log_http_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    _LOGGER.info(f"--- HTTP RETRY: {retry_state.attempt_number}. Exc: {exc}")


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


# -------------------------------------------------------------------
# Tool: анализ изменения лимитов КПК
# -------------------------------------------------------------------

def execute_kpk_limits_tool(
        *,
        mode: str,
        report_dt: str,
        report_dt_from: str | None = None,
        as_of_dt: str | None = None,
        division_cd: str | None = None,
        delta_amt: float | None = None,
        inn_num: str | None = None,
        include_details: bool = False,
        lookback_days: int | None = None,
        auth_header: str | None = None,
        trace_id: str | None = None,
) -> dict:
    """Вызывает tool для анализа лимитов КПК

    Ожидаемые эндпоинты
      - POST {ADAPTERS_API_BASE_URL}/api/kpk/limit-change       (mode=single)
      - POST {ADAPTERS_API_BASE_URL}/api/kpk/negative-report     (mode=all)
      - POST {ADAPTERS_API_BASE_URL}/api/kpk/find-deal           (mode=find_deal)
      - POST {ADAPTERS_API_BASE_URL}/api/kpk/investigate-deal    (mode=investigate_deal)
      - POST {ADAPTERS_API_BASE_URL}/api/kpk/client-history      (mode=client_history)

    Возвращает dict в формате PipelineResponse (status/data/error).
    """

    if ADAPTERS_API_BASE_URL is None:
        return {"status": "error", "error": ERROR_TEXT}

    if mode not in {"single", "all", "find_deal", "investigate_deal", "client_history"}:
        return {"status": "error", "error": f"Unknown mode: {mode}"}

    endpoint_map = {
        "single": "/api/kpk/limit-change",
        "all": "/api/kpk/negative-report",
        "find_deal": "/api/kpk/find-deal",
        "investigate_deal": "/api/kpk/investigate-deal",
        "client_history": "/api/kpk/client-history",
    }
    url = f"{ADAPTERS_API_BASE_URL.rstrip('/')}{endpoint_map[mode]}"

    try:
        # investigate_deal использует отдельную request-модель
        if mode == "investigate_deal":
            body = {"inn_num": inn_num, "value_dt": report_dt}
            resolved_as_of_dt = as_of_dt or settings.kpk_today_override
            if resolved_as_of_dt:
                body["as_of_dt"] = resolved_as_of_dt
            if delta_amt is not None:
                body["delta_amt"] = delta_amt
            if division_cd:
                body["division_cd"] = division_cd
            if lookback_days is not None and lookback_days > 0:
                body["lookback_days"] = lookback_days
        elif mode == "client_history":
            body = {
                "inn_num": inn_num,
                "division_cd": division_cd,
                "date": report_dt,
            }
            if report_dt_from:
                body["date_from"] = report_dt_from
                body["date_to"] = report_dt
            else:
                body["lookback_days"] = lookback_days or 7
        else:
            req = KpkLimitToolRequest(
                report_dt=report_dt,
                report_dt_from=report_dt_from,
                division_cd=division_cd,
                delta_amt=delta_amt,
                inn_num=inn_num,
                include_details=include_details,
                today_override=settings.kpk_today_override,
            )
            body = req.model_dump(exclude_none=True)

        headers = _auth_headers(auth_header, trace_id)

        # SECURITY §22: retry on 5xx/429/network; 4xx propagates.
        for attempt in _http_retrying():
            with attempt:
                resp = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT)
                if _retryable_status(resp.status_code):
                    resp.raise_for_status()
        resp.raise_for_status()

        # Валидируем
        payload = _decode_json_response(resp, url=url, log_prefix="kpk_tool")
        try:
            parsed = KpkLimitToolResponse(**payload)
        except Exception as e:
            _LOGGER.error(
                "kpk_response_validation_failed. error=%s payload_summary=%s",
                e,
                _payload_summary(payload),
            )
            raise
        _LOGGER.info(f"kpk_response_ok: {parsed.status}")
        return parsed.model_dump()

    except httpx.HTTPError as e:
        _LOGGER.error(f"--- ERROR in KPK LIMITS TOOL API (network). {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"KPK LIMITS TOOL network error: {e}"}})
        return {"status": "error", "error": ERROR_TEXT}
    except Exception as e:
        _LOGGER.error(f"--- ERROR in KPK LIMITS TOOL API. {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"KPK LIMITS TOOL error: {e}"}})
        return {"status": "error", "error": ERROR_TEXT}


def download_incorrect_deals_report(
        *,
        report_dt: str,
        auth_header: str | None = None,
        trace_id: str | None = None,
) -> dict:
    if ADAPTERS_API_BASE_URL is None:
        return {"status": "error", "error": ERROR_TEXT}

    url = f"{ADAPTERS_API_BASE_URL.rstrip('/')}/api/kpk/incorrect-deals-report"
    body = {"report_dt": report_dt}
    if settings.kpk_today_override:
        body["today_override"] = settings.kpk_today_override

    try:
        headers = _auth_headers(auth_header, trace_id)
        # SECURITY §22: retry on 5xx/429/network; 4xx propagates.
        for attempt in _http_retrying():
            with attempt:
                resp = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT)
                if _retryable_status(resp.status_code):
                    resp.raise_for_status()
        resp.raise_for_status()

        filename = _extract_filename(resp.headers.get("Content-Disposition"))
        if not filename:
            filename = f"new_deals_report_{report_dt.replace('-', '')}.xlsx"

        _LOGGER.info(f"incorrect_deals_report_ok. filename: {filename}")
        return {
            "status": "success",
            "content": resp.content,
            "filename": filename,
            "media_type": resp.headers.get("Content-Type"),
        }
    except httpx.HTTPError as e:
        _LOGGER.error(f"--- ERROR in INCORRECT DEALS REPORT API (network). {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"Incorrect deals report network error: {e}"}})
        return {"status": "error", "error": ERROR_TEXT}
    except Exception as e:
        _LOGGER.error(f"--- ERROR in INCORRECT DEALS REPORT API. {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"Incorrect deals report error: {e}"}})
        return {"status": "error", "error": ERROR_TEXT}


def _extract_filename(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    return match.group(1) if match else None
