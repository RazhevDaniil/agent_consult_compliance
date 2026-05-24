"""
Состояние графа (GraphState), инициализация ретривера и LLM-цепочек.
Все узлы графа импортируют отсюда общее состояние и цепочки.
"""
from typing import List, Dict, Any, Annotated, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from .models import (RouteQuery, RagAnswerWithConfidence, Critique,
                     RewrittenQuestion, MinimalContext, ToolRoute)

from .llm_setup import answer_llm, logic_llm, utility_llm, limits_llm, kpk_parse_llm, kpk_answer_llm

from .prompts import (
    router_prompt_template,
    qa_prompt_template,
    critique_prompt_template,
    contextualize_q_prompt_template,
    explainer_prompt_template,
    tool_router_prompt_template,
    theory_from_tool_prompt,
    theory_from_user_prompt,
    KPK_PARSE_PROMPT,
    KPK_ANSWER_PROMPT,
)

from .document_processor import initialize_vector_db

from .kpk_models import KpkLimitParsedQuery


# =====================================================================================
# СОСТОЯНИЕ ГРАФА
# =====================================================================================
class GraphState(TypedDict):
    input: str
    # Шаг 3: conversation history uses LangGraph's add_messages reducer.
    # Каждый возврат `{"messages": [...]}` из узла или ainvoke-входа
    # ДОБАВЛЯЕТСЯ к существующему списку, а не заменяет его. Для
    # компактизации (саммаризации) или очистки используется RemoveMessage
    # (см. summarize_history_node и /chat/{id}/reset endpoint).
    messages: Annotated[List[BaseMessage], add_messages]
    destination: str
    context: List[Document]
    answer: str
    confidence_score: int
    critique: Dict[str, Any]
    regen_attempts: int
    final_answer: str
    sources: List[str]
    tool_payload: Dict[str, Any]
    theory_inputs: Dict[str, Any]
    theory_question: str
    requested_components: str
    payload_components: List[str]
    skip_explain: bool
    is_context_identical: bool
    response_state: str  # "completed" | "input-required" | "failed"
    parsed: Dict[str, Any]
    generated_report: Dict[str, Any]
    # Cross-turn fields (Шаги 2.1 / 2.3 / 2.5): persisted by the
    # checkpointer between ainvoke() calls with the same thread_id.
    # Replace the dicts that used to live in InMemoryChatStore.
    last_tool_payload: Dict[str, Any]
    last_tool_kind: str
    last_selector: Dict[str, Any]
    generated_reports: Dict[str, Any]
    # Шаг 2.5: per-chat tool-result cache, keyed by the deal signature.
    # Replaces InMemoryChatStore._tool_cache.
    tool_cache: Dict[str, Any]
    # pending_kpk_date_choce
    pending_kpk_date_choice: Dict[str, Any]


# Инициализация ретривера один раз при старте
retriever = initialize_vector_db()


# =====================================================================================
# ЦЕПОЧКИ — factories: each call picks Main/PreView LLM (SECURITY §26).
# Mirrors treasurer's `get_llm_with_config()` pattern — chain is built fresh
# per invocation so the `_pick(...)` call inside the *_llm() factory runs on
# every node hop, not once at module load.
# =====================================================================================
def router_chain():
    return router_prompt_template | logic_llm().with_structured_output(RouteQuery)


def rag_chain_with_confidence():
    return qa_prompt_template | answer_llm().with_structured_output(RagAnswerWithConfidence)


def critique_chain():
    return critique_prompt_template | logic_llm().with_structured_output(Critique)


def explainer_chain():
    return explainer_prompt_template | answer_llm()


def tool_router_chain():
    return tool_router_prompt_template | logic_llm().with_structured_output(ToolRoute)


def rag_context_chain():
    return contextualize_q_prompt_template | utility_llm().with_structured_output(MinimalContext)


def tool2theory_chain():
    return theory_from_tool_prompt | utility_llm().with_structured_output(RewrittenQuestion)


def user2theory_chain():
    return theory_from_user_prompt | utility_llm().with_structured_output(RewrittenQuestion)


def _kpk_parse_chain():
    return KPK_PARSE_PROMPT | kpk_parse_llm().with_structured_output(KpkLimitParsedQuery)


def _kpk_answer_chain():
    return KPK_ANSWER_PROMPT | kpk_answer_llm()
