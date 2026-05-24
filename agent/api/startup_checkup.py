"""SECURITY §19 — readiness probes for the consultant agent.

Defines three startup checks (GigaChat / RAG / missing-data),
runs them sequentially with fail-fast semantics, and exposes a background
re-check loop that refreshes the ready flag every N seconds so the service
recovers from transient dependency outages without a pod restart.

The lifespan in `app.py` is the only caller; routes read state via the
`is_ready()` / `get_failures()` accessors.
"""

import asyncio
import uuid
from typing import Awaitable, Callable

import logging
from langchain_core.messages import HumanMessage

from .config import settings

from .graph import compiled_graph

from .llm_setup import utility_llm


logger = logging.getLogger(__name__)


# --- Module state (private; routes read via accessors below) -----------------
_is_ready: bool = False
_failures: list[dict] = []


def is_ready() -> bool:
    return _is_ready


def get_failures() -> list[dict]:
    return list(_failures)


# --- Individual probes -------------------------------------------------------
async def _check_gigachat() -> None:
    """Synthetic LLM ping. Raises on timeout or empty content."""
    response = await asyncio.wait_for(
        utility_llm().ainvoke("Ответь одним словом: OK"),
        timeout=settings.readiness_gigachat_timeout_sec,
    )
    content = getattr(response, "content", None) or ""
    if not str(content).strip():
        raise RuntimeError("gigachat returned empty content")


async def _check_rag() -> None:
    """Three reference methodology questions routed through the full graph
    (router → retrieve_documents → generate_rag → format_final_answer).
    Each must return a non-empty final_answer with at least one source."""
    questions = [
        "Что такое NSO?",
        "Что такое стоимость фондирования и как она рассчитывается?",
        "Что такое лимит КПК и как он рассчитывается?",
    ]
    for q in questions:
        chat_id = f"_readiness_rag_{uuid.uuid4().hex}"
        config = {"configurable": {"thread_id": chat_id, "auth_header": None}}
        state = await asyncio.wait_for(
            compiled_graph.ainvoke(
                {
                    "input": q,
                    "messages": [HumanMessage(content=q)],
                    "regen_attempts": 0,
                    "is_context_identical": False,
                },
                config=config,
            ),
            timeout=settings.readiness_rag_timeout_sec,
        )
        final = (state.get("final_answer") or state.get("answer") or "").strip()
        sources = state.get("sources") or []
        if not final:
            raise RuntimeError(f"RAG returned empty answer for: {q!r}")
        if not sources:
            raise RuntimeError(f"RAG returned no sources for: {q!r}")


async def _check_missing_data() -> None:
    """Pricing-style request without enough parameters. The graph must
    return a non-empty answer (asking for clarification) instead of crashing."""
    chat_id = f"_readiness_missing_{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": chat_id, "auth_header": None}}
    state = await asyncio.wait_for(
        compiled_graph.ainvoke(
            {
                "input": "Посчитай pricing",
                "messages": [HumanMessage(content="Посчитай pricing")],
                "regen_attempts": 0,
                "is_context_identical": False,
            },
            config=config,
        ),
        timeout=settings.readiness_missing_data_timeout_sec,
    )
    final = (state.get("final_answer") or state.get("answer") or "").strip()
    if not final:
        raise RuntimeError("missing-data probe returned empty answer")


CHECKS: list[tuple[str, Callable[[], Awaitable[None]]]] = [
    ("gigachat", _check_gigachat),
    ("rag", _check_rag),
    ("missing_data", _check_missing_data),
]


# --- Orchestration -----------------------------------------------------------
async def run_checks(*, source: str) -> tuple[bool, list[dict]]:
    """Run all probes sequentially with fail-fast semantics. Updates module
    state and returns (ok, failures). `source` (`startup`/`recheck`) labels
    logs so operators can tell boot probes from background refreshes."""
    global _is_ready, _failures

    failures: list[dict] = []
    for name, fn in CHECKS:
        try:
            await fn()
            logger.info(f"readiness_check_ok, check={name}, source={source}")
        except Exception as e:
            failures.append({"check": name, "error": str(e), "exc_type": type(e).__name__})
            logger.error(f"startup_check_failed, check={name}, source={source}, error={e}")
            for skipped, _ in CHECKS[len(failures):]:
                logger.info(f"readiness_check_skipped, check={skipped}, source={source}")
            _is_ready = False
            _failures = failures
            return False, failures

    _is_ready = True
    _failures = []
    return True, []


async def recheck_loop() -> None:
    """Background loop: re-runs the checks every
    `settings.readiness_recheck_interval_sec` and flips ready state on
    transition. Cancellation-safe."""
    while True:
        try:
            await asyncio.sleep(settings.readiness_recheck_interval_sec)
            prev = _is_ready
            ok, failures = await run_checks(source="recheck")
            if ok and not prev:
                logger.info("readiness_recheck_recovered")
            elif not ok and prev:
                logger.warning(f"readiness_recheck_degraded, failures={failures}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"readiness_recheck_loop_error. error={e}")
