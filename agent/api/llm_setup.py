import random
import logging

from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_gigachat.chat_models import GigaChat
from langchain_gigachat.embeddings.gigachat import GigaChatEmbeddings

from .config import giga_settings

_LOGGER = logging.getLogger(__name__)


class BatchedEmbeddings(Embeddings):
    def __init__(self, inner: Embeddings, batch_size: int):
        self.inner = inner
        self.batch_size = max(1, int(batch_size))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_result = self.inner.embed_documents(batch)
            results.extend(batch_result)
        return results

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(text)


GIGA_KWARGS = dict(
    base_url=giga_settings.base_url,
    verify_ssl_certs=giga_settings.verify_ssl_certs,
    cert_file=giga_settings.cert_file,
    key_file=giga_settings.key_file,
)


def _pick(main: str, preview: Optional[str]) -> tuple[str, str]:
    """Return (model_name, installation). `installation` ∈ {"main", "preview"}."""
    if preview and giga_settings.preview_ratio > 0.0 and random.random() < giga_settings.preview_ratio:
        return preview, "preview"
    return main, "main"


def _build(main: str, preview: Optional[str], temperature: float, timeout: int, max_tokens: int) -> GigaChat:
    """Per-call model pick + GigaChat construction.

    AEF spans for LLM calls are emitted by graph_llm_wrappers around the shared
    retry wrapper. Do not attach callbacks here: nested callbacks can conflict
    across async and threadpool boundaries.
    """
    model, installation = _pick(main, preview)
    _LOGGER.info(f"gigachat_installation_picked. installation: {installation}. model: {model}")
    return GigaChat(
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        profanity_check=giga_settings.profanity_check,
        **GIGA_KWARGS,
    )


# === Per-role factories — each call picks Main/PreView and returns a fresh GigaChat ===

def answer_llm() -> GigaChat:
    return _build(
        giga_settings.main_model,
        giga_settings.preview_main_model,
        giga_settings.main_model_temperature,
        giga_settings.main_model_timeout,
        giga_settings.answer_max_tokens,
    )


def logic_llm() -> GigaChat:
    return _build(
        giga_settings.main_model,
        giga_settings.preview_main_model,
        giga_settings.temperature,
        giga_settings.timeout,
        giga_settings.logic_max_tokens,
    )


def utility_llm() -> GigaChat:
    return _build(
        giga_settings.model,
        giga_settings.preview_model,
        giga_settings.temperature,
        giga_settings.timeout,
        giga_settings.utility_max_tokens,
    )


def kpk_parse_llm() -> GigaChat:
    return _build(
        giga_settings.limits_model,
        giga_settings.preview_limits_model,
        giga_settings.main_model_temperature,
        giga_settings.main_model_timeout,
        giga_settings.kpk_parse_max_tokens,
    )


def kpk_answer_llm() -> GigaChat:
    return _build(
        giga_settings.model,
        giga_settings.preview_model,
        giga_settings.temperature,
        giga_settings.timeout,
        giga_settings.kpk_answer_max_tokens,
    )


# backward-compat alias used by older imports
limits_llm = kpk_parse_llm


# === Embedder — single instance, shared (canary doesn't apply to embeddings) ===

raw_embedding_function: Optional[Embeddings] = None
embedding_function: Optional[Embeddings] = None

try:
    raw_embedding_function = GigaChatEmbeddings(
        **GIGA_KWARGS,
        timeout=giga_settings.main_model_timeout,
    )
    embedding_function = BatchedEmbeddings(
        inner=raw_embedding_function,
        batch_size=giga_settings.embed_batch_size,
    )
    _LOGGER.info("--- LLM factories are ready. Preview ratio: %.2f. ---", giga_settings.preview_ratio)
except Exception as e:
    _LOGGER.error("Error due initialization embedder: %s", e)
