import asyncio
import re
import json
import httpx
import requests
import logging
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import List, Tuple

from langchain_core.documents import Document
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')

from .config import settings

GIGAPLATFORM_STOP_EVENT = "gigaplatform_rejected"
GIGAPLATFORM_UNAVAILABLE_MESSAGE = "The service is temporarily unavailable due to technical reasons."

_llm_stop_event_cvar: ContextVar[str | None] = ContextVar("llm_stop_event", default=None)


def reset_llm_stop_event() -> None:
    _llm_stop_event_cvar.set(None)


def get_llm_stop_event() -> str | None:
    return _llm_stop_event_cvar.get()


def _extract_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    if status is None:
        text = str(exc)
        if re.search(r'"status"\s*:\s*403\b', text) or re.search(r"\b403\b", text):
            status = 403
    return status if isinstance(status, int) else None


def _extract_error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    if response is not None:
        text = getattr(response, "text", None)
        if text:
            parts.append(str(text))
        content = getattr(response, "content", None)
        if content:
            try:
                parts.append(content.decode("utf-8", errors="ignore"))
            except Exception:
                pass
        try:
            payload = response.json()
            parts.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(part for part in parts if part)


def _is_gigaplatform_rejection(exc: BaseException) -> bool:
    return (
        _extract_status_code(exc) == 403
        and GIGAPLATFORM_UNAVAILABLE_MESSAGE in _extract_error_text(exc)
    )


def mark_llm_stop_event_if_needed(exc: BaseException) -> str | None:
    if _is_gigaplatform_rejection(exc):
        _llm_stop_event_cvar.set(GIGAPLATFORM_STOP_EVENT)
        return GIGAPLATFORM_STOP_EVENT
    return None


# SECURITY §23: map any GigaChat / transport exception to a typed audit event.
def _classify_gigachat_error(exc: BaseException) -> str:
    status = _extract_status_code(exc)

    if _is_gigaplatform_rejection(exc):
        return "gigachat_flow_disabled"
    if status == 429:
        return "gigachat_rate_limited"
    if isinstance(status, int) and 500 <= status < 600:
        return "gigachat_5xx_failed"
    if isinstance(status, int) and 400 <= status < 500:
        return "gigachat_response_error"

    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError,
                        requests.exceptions.Timeout)):
        return "gigachat_timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, ConnectionError,
                        OSError, requests.exceptions.ConnectionError)):
        return "gigachat_transport_error"

    return "gigachat_unknown_error"


# RR-AI-5: retry only transient 5xx/timeout/transport errors. 4xx,
# including 429, validation and business errors fall through without retry.
_RETRYABLE_EVENTS = {
    "gigachat_5xx_failed",
    "gigachat_timeout",
    "gigachat_transport_error",
}


def _should_retry_llm(exc: BaseException) -> bool:
    return _classify_gigachat_error(exc) in _RETRYABLE_EVENTS


def _log_llm_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    event = _classify_gigachat_error(exc) if exc else "gigachat_unknown_error"
    _LOGGER.warning(f"[{retry_state.fn.__name__}] LLM failed, retrying. Event: {event}")


def _log_llm_exhausted(exc: BaseException, *, node_name: str) -> None:
    """Emit a typed §23 audit event after retries are exhausted, before fallback."""
    event = _classify_gigachat_error(exc)
    mark_llm_stop_event_if_needed(exc)
    _LOGGER.error(f"[{node_name}] LLM failed, exhausted retries. Event: {event}")


def _llm_retrying_sync() -> Retrying:
    return Retrying(
        retry=retry_if_exception(_should_retry_llm),
        stop=stop_after_attempt(settings.llm_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.llm_retry_base,
            max=settings.llm_retry_max,
        ),
        before_sleep=_log_llm_retry,
        reraise=True,
    )


def _llm_retrying_async() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception(_should_retry_llm),
        stop=stop_after_attempt(settings.llm_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.llm_retry_base,
            max=settings.llm_retry_max,
        ),
        before_sleep=_log_llm_retry,
        reraise=True,
    )

from .models import RegenerateResult, _Rank

from .llm_setup import utility_llm

from .kpk_models import KpkLimitParsedQuery


def _invoke_with_retry(runnable, payload, *, node_name: str):
    for attempt in _llm_retrying_sync():
        with attempt:
            return runnable.invoke(payload)
    raise RuntimeError(f"{node_name} LLM retry loop did not execute")


async def _ainvoke_with_retry(runnable, payload, *, node_name: str):
    async for attempt in _llm_retrying_async():
        with attempt:
            return await runnable.ainvoke(payload)
    raise RuntimeError(f"{node_name} LLM retry loop did not execute")


def _invoke_structured_with_default(runnable, payload, model_cls, default_dict, node_name: str):
    """Безопасный инвок: если LLM упал — вернём валидный объект со значениями по умолчанию.

    AEF callback is attached once at graph invocation level in app.py.
    """
    try:
        result = _invoke_with_retry(runnable, payload, node_name=node_name)
        return result
    except Exception as e:
        _log_llm_exhausted(e, node_name=node_name)
        _LOGGER.warning(f"[{node_name}] LLM failed, fallback to default: {e}", exc_info=True)
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[{node_name}] LLM failed, fallback to default: {e}"}})
        return model_cls(**default_dict)


def _invoke_text_with_default(runnable, payload, *, default_text: str, node_name: str) -> str:
    """Безопасный invoke для неструктурированного ответа"""
    try:
        out = _invoke_with_retry(runnable, payload, node_name=node_name)
        return getattr(out, "content", str(out)) if out is not None else default_text
    except Exception as e:
        _log_llm_exhausted(e, node_name=node_name)
        _LOGGER.warning(f"[{node_name}] LLM failed, fallback to default: {e}", exc_info=True)
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[{node_name}] LLM failed, fallback to default: {e}"}})
        return default_text


def _coerce_regenerate_result(raw) -> "RegenerateResult":
    """
    Преобразует все в RegenerateResult
    """
    if isinstance(raw, RegenerateResult):
        return raw
    if isinstance(raw, dict):
        return RegenerateResult(**raw)

    text = getattr(raw, "content", None) or str(raw) or ""
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if isinstance(data, dict):
        return RegenerateResult(**data)

    return RegenerateResult(answer_text=text, confidence_score=None, sources=[])


def _rerank_with_llm(question: str, docs: List[Document], top_n: int = 6) -> List[Document]:
    if not docs:
        return docs
    scored: List[Tuple[int, Document]] = []
    for d in docs:
        prompt = (
            "Дай только целое число JSON со схемой {\"score\": 0..3} — без пояснений.\n"
            "0=совсем не относится; 1=слабая связь; 2=полезно; 3=очень полезно.\n\n"
            f"Вопрос: {question}\n\nФрагмент:\n{d.page_content[:1200]}"
        )
        try:
            rank = _invoke_with_retry(
                utility_llm().with_structured_output(_Rank),
                prompt,
                node_name="rerank",
            )
            score = int(rank.score)
        except Exception as e:
            mark_llm_stop_event_if_needed(e)
            score = 1
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:top_n]]


async def _ainvoke_structured_with_default(runnable, payload, model_cls, default_dict, node_name: str):
    try:
        result = await _ainvoke_with_retry(runnable, payload, node_name=node_name)
        return result
    except Exception as e:
        _log_llm_exhausted(e, node_name=node_name)
        _LOGGER.warning(f"[{node_name}] LLM failed, fallback to default: {e}", exc_info=True)
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[{node_name}] LLM failed, fallback to default: {e}"}})
        return model_cls(**default_dict)


async def _ainvoke_text_with_default(runnable, payload, *, default_text: str, node_name: str) -> str:
    try:
        out = await _ainvoke_with_retry(runnable, payload, node_name=node_name)
        return getattr(out, "content", str(out)) if out is not None else default_text
    except Exception as e:
        _log_llm_exhausted(e, node_name=node_name)
        _LOGGER.warning(f"[{node_name}] LLM failed, fallback to default: {e}", exc_info=True)
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[{node_name}] LLM failed, fallback to default: {e}"}})
        return default_text


_KPK_PARSE_TIMEOUT = 120  # секунд — макс. время ожидания structured output от LLM


def _kpk_safe_structured_invoke(chain, payload, *, default: dict) -> KpkLimitParsedQuery:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(chain.invoke, payload)
    try:
        result = future.result(timeout=_KPK_PARSE_TIMEOUT)
    except FuturesTimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        _LOGGER.error(f"[kpk_parse] LLM timed out after {_KPK_PARSE_TIMEOUT} seconds")
        return KpkLimitParsedQuery(**default)
    except Exception as e:
        executor.shutdown(wait=False, cancel_futures=True)
        _log_llm_exhausted(e, node_name="kpk_parse")
        _LOGGER.warning(f"[kpk_parse] LLM failed, fallback to default: {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[kpk_parse] LLM failed: {e}"}})
        return KpkLimitParsedQuery(**default)
    else:
        executor.shutdown(wait=False)
        if result is None:
            _LOGGER.warning("kpk_parse_returned_none")
            return KpkLimitParsedQuery(**default)
        return result


def _kpk_safe_text_invoke(chain, payload, *, default_text: str) -> str:
    try:
        for attempt in _llm_retrying_sync():
            with attempt:
                out = chain.invoke(payload)
        return getattr(out, "content", str(out)) if out is not None else default_text
    except Exception as e:
        _log_llm_exhausted(e, node_name="kpk_answer")
        _LOGGER.warning(f"[kpk_answer] LLM failed, fallback to default: {e}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[kpk_answer] LLM failed: {e}"}})
        return default_text
