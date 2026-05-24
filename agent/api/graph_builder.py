import re
import logging

from langgraph.graph import StateGraph, END, START

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')

from .config import settings

from .tech_funcs import _deal_id_from_state, _mentions_this_deal

from .graph_state import GraphState

from .graph_nodes_rag import (route_query, retrieve_documents, generate_answer_rag,
critique_answer, refine_and_reretrieve, regenerate_answer, format_final_answer)

from .graph_nodes_tool import (execute_tool_node, after_execute_tool,
deals_report, retrieve_documents_for_explain, generate_explainer)

from .graph_nodes_kpk import (kpk_preprocess_input, kpk_parse_query,
kpk_call_tool, kpk_generate_answer, kpk_finalize)

from .graph_nodes_messages import append_response_node, summarize_history_node


# =====================================================================================
# УСЛОВНЫЕ РЁБРА
# =====================================================================================

def _route_after_kpk_parse(state: GraphState) -> str:
    """Если парсер не получил достаточно данных — сразу на финализацию."""
    if state.get("response_state") == "input-required":
        return "kpk_finalize"
    return "kpk_tool"


def route_branches(state):
    """Маршрутизатор, направляет на RAG, инструменты или КПК."""
    dest = (state.get("destination") or "").lower()
    
    if state.get("pending_kpk_date_choice"):
        return "kpk_preprocess"

    # Если маршрутизатор определил запрос как kpk_limits - идем в ветку КПК
    if dest == "kpk_limits":
        return "kpk_preprocess"

    # Если маршрутизатор определил запрос как unsupported_calculation - идем в инструменты
    if dest == "unsupported_calculation":
        return "execute_tool"

    text = (state.get("input") or "").lower()

    # Проверяем наличие контекста (активная сделка в памяти) — теперь
    # из state, который чекпоинтер сохраняет между ходами (Шаг 2.1).
    has_ctx = bool(_deal_id_from_state(state) or state.get("last_selector"))

    # Если Router отправил в Теорию (RAG), но у нас есть контекст...
    if dest == "rag_methodology" and has_ctx:
        calc_triggers = []
        calc_triggers.extend([
            r"посчитай", r"расчитай", r"значение", r"че?му равн", r"какая", r"какой",
            r"данн(ые|ых)", r"цифр(ы|а)", r"результат"
        ])

        is_calc_intent = any(re.search(p, text) for p in calc_triggers)
        is_referal = _mentions_this_deal(text)

        if is_calc_intent or is_referal:
            _LOGGER.info(f"--- REROUTE: Context present + calc keyword found -> execute_tool ---")
            audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"REROUTE: Context present + calc keyword found -> execute_tool"}})
            return "execute_tool"

    return "retrieve_documents"


def should_critique(state: GraphState):
    """Автокритика по порогу уверенности и лимиту регенераций."""
    if state.get("regen_attempts", 0) >= settings.max_retries:
        _LOGGER.info("--- КРИТИКА: Лимит регенераций. Пропускаем. ---")
        audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"КРИТИКА: Лимит регенераций. Пропускаем."}})
        return "no_critique_needed"
    thr = settings.auto_crit_conf_threshold or settings.self_confidence_treshold
    if state.get("confidence_score", 10) <= thr:
        _LOGGER.info(f"--- КРИТИКА: Уверенность {state['confidence_score']} ≤ {thr}. ---")
        return "critique_needed"
    return "no_critique_needed"


def after_critique(state: GraphState):
    """Решает, нужно ли регенерировать ответ после критики."""
    if state["critique"]["is_good"]:
        _LOGGER.info("--- КРИТИКА: Ответ признан хорошим. ---")
        return "end_critique"
    _LOGGER.info("--- КРИТИКА: Ответ плохой. Уточняем запрос и переизвлекаем контекст. ---")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"КРИТИКА: Ответ плохой. Уточняем запрос и переизвлекаем контекст."}})
    return "refine"


def check_refine_result(state: GraphState):
    """Проверка результатов Refine"""
    if state.get("is_context_identical", False):
        return "skip_regen"
    return "do_regen"


# =====================================================================================
# СБОРКА ГРАФА
# =====================================================================================

def build_graph(checkpointer=None):
    workflow = StateGraph(GraphState)

    # Узлы
    workflow.add_node("router", route_query)
    workflow.add_node("execute_tool", execute_tool_node)
    workflow.add_node("deals_report", deals_report)
    workflow.add_node("retrieve_explain", retrieve_documents_for_explain)
    workflow.add_node("generate_explainer", generate_explainer)
    workflow.add_node("retrieve_documents", retrieve_documents)
    workflow.add_node("generate_rag", generate_answer_rag)
    workflow.add_node("critique_rag", critique_answer)
    workflow.add_node("refine_and_reretrieve", refine_and_reretrieve)
    workflow.add_node("regenerate_answer", regenerate_answer)
    workflow.add_node("format_final_answer", format_final_answer)

    # Узлы КПК
    workflow.add_node("kpk_preprocess", kpk_preprocess_input)
    workflow.add_node("kpk_parse", kpk_parse_query)
    workflow.add_node("kpk_tool", kpk_call_tool)
    workflow.add_node("kpk_generate_answer", kpk_generate_answer)
    workflow.add_node("kpk_finalize", kpk_finalize)

    # Terminal message-management nodes (Шаг 3)
    workflow.add_node("append_response", append_response_node)
    workflow.add_node("summarize_history", summarize_history_node)

    # Рёбра
    workflow.add_edge(START, "router")

    workflow.add_conditional_edges(
        "execute_tool",
        after_execute_tool,
        {
            "explain": "retrieve_explain",
            "final": "format_final_answer",
            "deals_report": "deals_report"
        },
    )

    workflow.add_edge("retrieve_explain", "generate_explainer")
    workflow.add_edge("generate_explainer", "format_final_answer")

    workflow.add_conditional_edges(
        "router",
        route_branches,
        {
            "retrieve_documents": "retrieve_documents",
            "execute_tool": "execute_tool",
            "kpk_preprocess": "kpk_preprocess",
        },
    )

    # Цепочка КПК с условным переходом после парсинга
    workflow.add_edge("kpk_preprocess", "kpk_parse")

    workflow.add_conditional_edges(
        "kpk_parse",
        _route_after_kpk_parse,
        {
            "kpk_tool": "kpk_tool",
            "kpk_finalize": "kpk_finalize",
        },
    )

    workflow.add_edge("kpk_tool", "kpk_generate_answer")
    workflow.add_edge("kpk_generate_answer", "kpk_finalize")
    workflow.add_edge("kpk_finalize", "append_response")

    # Цепочка RAG
    workflow.add_edge("retrieve_documents", "generate_rag")
    workflow.add_conditional_edges(
        "generate_rag",
        should_critique,
        {"critique_needed": "critique_rag", "no_critique_needed": "format_final_answer"},
    )
    workflow.add_conditional_edges(
        "critique_rag",
        after_critique,
        {
            "refine": "refine_and_reretrieve",
            "end_critique": "format_final_answer"
            },
        )

    workflow.add_conditional_edges(
        "refine_and_reretrieve",
        check_refine_result,
        {
            "do_regen": "regenerate_answer",
            "skip_regen": "format_final_answer"
        }
    )

    workflow.add_edge("regenerate_answer", "format_final_answer")

    # Convergence: every successful branch funnels through the
    # message-management terminal pair.
    workflow.add_edge("deals_report", "append_response")
    workflow.add_edge("format_final_answer", "append_response")
    workflow.add_edge("append_response", "summarize_history")
    workflow.add_edge("summarize_history", END)

    compile_kwargs = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    app = workflow.compile(**compile_kwargs)
    _LOGGER.info(f"--- Graph compiled successfully! (checkpointer=%s). {type(checkpointer).__name__ if checkpointer is not None else 'None'}")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": "Graph compiled successfully!"}})
    return app
