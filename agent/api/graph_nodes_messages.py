import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage

from .config import settings

from .llm_setup import utility_llm

from .summarizer import summarize_messages_async


_LOGGER = logging.getLogger(__name__)


def append_response_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Emit `{"messages": [AIMessage(answer)]}`. The reducer appends
    it to state.messages; nothing else is touched."""
    answer = state.get("final_answer") or state.get("answer") or ""
    if not answer:
        return {}
    return {"messages": [AIMessage(content=answer)]}


async def summarize_history_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Compact the oldest `summarization_window` messages once the
    list crosses `summarization_treshold`. Returns a list of
    RemoveMessage tombstones for the old IDs plus a single AIMessage
    holding the summary text; `add_messages` applies the removes
    in-place and appends the new entry.

    On error or empty snapshot, returns {} (no-op).
    """
    messages: List[BaseMessage] = state.get("messages") or []
    threshold = settings.summarization_treshold
    window = settings.summarization_window

    if len(messages) < threshold:
        return {}

    snapshot = messages[:window]
    if not snapshot:
        return {}

    try:
        summary_text = await summarize_messages_async(snapshot, utility_llm())
    except Exception as e:
        _LOGGER.error(f"summarize_history_node_failed, error: {e}")
        return {}

    summary_msg = AIMessage(
        content=f"Краткое содержание предыдущей части диалога: {summary_text}"
    )

    # RemoveMessage requires .id; LangChain auto-assigns one on
    # construction, but defensive code can't hurt.
    removes: List[BaseMessage] = []
    for m in snapshot:
        mid = getattr(m, "id", None)
        if mid:
            removes.append(RemoveMessage(id=mid))

    return {"messages": removes + [summary_msg]}
