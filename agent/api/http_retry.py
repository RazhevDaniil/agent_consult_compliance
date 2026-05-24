import logging
from typing import Any, Callable, Mapping

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import TIMEOUT, settings

_LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


def _is_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return retryable_status(exc.response.status_code)
    return isinstance(exc, httpx.RequestError)


def _log_http_before_sleep(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    sleep = retry_state.next_action.sleep if retry_state.next_action else None
    _LOGGER.warning(
        "http_retry_wait. attempt=%s next_wait_sec=%s exc_type=%s exc=%s",
        retry_state.attempt_number,
        sleep,
        type(exc).__name__ if exc else None,
        exc,
    )


def _log_http_after(retry_state: RetryCallState) -> None:
    if not retry_state.outcome or not retry_state.outcome.failed:
        return
    exc = retry_state.outcome.exception()
    _LOGGER.warning(
        "http_attempt_error. attempt=%s exc_type=%s exc=%s",
        retry_state.attempt_number,
        type(exc).__name__ if exc else None,
        exc,
    )


def http_retrying() -> Retrying:
    return Retrying(
        retry=retry_if_exception(_is_retryable_http_error),
        stop=stop_after_attempt(settings.http_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.http_retry_base,
            max=settings.http_retry_max,
        ),
        after=_log_http_after,
        before_sleep=_log_http_before_sleep,
        reraise=True,
    )


def post_json_with_retry(
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        timeout: float = TIMEOUT,
        on_response: Callable[[httpx.Response], None] | None = None,
) -> httpx.Response:
    """POST JSON with bounded retry for transient transport/5xx errors only.

    RR-AI-5: retry is allowed for httpx.RequestError and HTTP
    500/502/503/504. 4xx, including 429, validation and business errors are
    non-retryable and are raised/handled by the caller after this helper.
    """
    response: httpx.Response | None = None
    for attempt in http_retrying():
        with attempt:
            _LOGGER.info("http_attempt_start. attempt=%s url=%s", attempt.retry_state.attempt_number, url)
            response = httpx.post(
                url,
                json=dict(json),
                headers=dict(headers or {}),
                timeout=timeout,
            )
            if on_response:
                try:
                    on_response(response)
                except Exception as hook_exc:
                    _LOGGER.warning("http_response_hook_failed. url=%s error=%s", url, hook_exc)
            if retryable_status(response.status_code):
                _LOGGER.warning(
                    "http_attempt_retryable_error. attempt=%s status_code=%s url=%s",
                    attempt.retry_state.attempt_number,
                    response.status_code,
                    url,
                )
                response.raise_for_status()
            if response.status_code >= 400:
                _LOGGER.warning(
                    "http_attempt_non_retryable_error. attempt=%s status_code=%s url=%s",
                    attempt.retry_state.attempt_number,
                    response.status_code,
                    url,
                )
                response.raise_for_status()
            _LOGGER.info(
                "http_attempt_success. attempt=%s status_code=%s url=%s",
                attempt.retry_state.attempt_number,
                response.status_code,
                url,
            )
    if response is None:
        raise RuntimeError("HTTP request did not execute")
    response.raise_for_status()
    return response


async def async_post_json_with_retry(
        client: httpx.AsyncClient,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        on_response: Callable[[httpx.Response], None] | None = None,
) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in http_retrying():
        with attempt:
            _LOGGER.info("http_attempt_start. attempt=%s url=%s", attempt.retry_state.attempt_number, url)
            response = await client.post(url, json=dict(json), headers=dict(headers or {}))
            if on_response:
                try:
                    on_response(response)
                except Exception as hook_exc:
                    _LOGGER.warning("http_response_hook_failed. url=%s error=%s", url, hook_exc)
            if retryable_status(response.status_code):
                _LOGGER.warning(
                    "http_attempt_retryable_error. attempt=%s status_code=%s url=%s",
                    attempt.retry_state.attempt_number,
                    response.status_code,
                    url,
                )
                response.raise_for_status()
            if response.status_code >= 400:
                _LOGGER.warning(
                    "http_attempt_non_retryable_error. attempt=%s status_code=%s url=%s",
                    attempt.retry_state.attempt_number,
                    response.status_code,
                    url,
                )
                response.raise_for_status()
            _LOGGER.info(
                "http_attempt_success. attempt=%s status_code=%s url=%s",
                attempt.retry_state.attempt_number,
                response.status_code,
                url,
            )
    if response is None:
        raise RuntimeError("HTTP request did not execute")
    response.raise_for_status()
    return response
