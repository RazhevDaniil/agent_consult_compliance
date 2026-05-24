from typing import Awaitable, Callable, List, Optional
from langchain_core.messages import AIMessage, BaseMessage

from .prompts import summarization_prompt_template
from .graph_llm_wrappers import _ainvoke_text_with_default, _invoke_text_with_default


def format_messages_for_summary(messages: List[BaseMessage]) -> str:
    return "\n".join([f"{type(msg).__name__}: {msg.content}" for msg in messages])

def summarize_messages(messages_to_summarize: List[BaseMessage], llm) -> str:
    if not messages_to_summarize:
        return ""
    formatted_history = format_messages_for_summary(messages_to_summarize)
    chain = summarization_prompt_template | llm
    return _invoke_text_with_default(
        chain,
        {"chat_history": formatted_history},
        default_text="",
        node_name="summarize_history",
    )

async def summarize_messages_async(messages_to_summarize: List[BaseMessage], llm) -> str:
    """Асинхронная версия"""
    if not messages_to_summarize:
        return ""
    formatted_history = format_messages_for_summary(messages_to_summarize)
    chain = summarization_prompt_template | llm
    return await _ainvoke_text_with_default(
        chain,
        {"chat_history": formatted_history},
        default_text="",
        node_name="summarize_history",
    )


async def summarize_history_if_needed(
    messages: List[BaseMessage],
    threshold: int,
    window: int,
    summary_fn: Callable[[List[BaseMessage]], Awaitable[str]],
) -> List[BaseMessage]:
    """Pure helper extracted from InMemoryChatStore.append_exchange:
    if `messages` has reached `threshold`, compact the first `window`
    items into a single AIMessage summary; otherwise return as-is.

    `summary_fn(snapshot)` is an awaitable returning the summary text.
    Tests pass a fake; production passes `summarize_messages_async`
    bound to `utility_llm`.
    """
    if len(messages) < threshold:
        return messages
    snapshot = messages[:window]
    summary_text = await summary_fn(snapshot)
    head = AIMessage(content=f"Краткое содержание предыдущей части диалога: {summary_text}")
    return [head] + list(messages[window:])
