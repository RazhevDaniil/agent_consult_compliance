"""
Узлы графа: RAG-пайплайн (маршрутизация, поиск документов, генерация,
критика, уточнение, регенерация, финальное форматирование).
"""
import re
import asyncio
import logging

from .config import settings

from .models import (RouteQuery, RagAnswerWithConfidence, Critique, MinimalContext,
RewrittenQuestion, RegenerateResult)

from .llm_setup import answer_llm, utility_llm

from .prompts import contextualize_q_prompt_template, regenerate_prompt_template

from .tech_funcs import sanitize_formulas, _format_docs, _trace_node

from .graph_state import (GraphState, retriever, router_chain,
rag_chain_with_confidence, critique_chain, rag_context_chain)

from .graph_llm_wrappers import (_ainvoke_structured_with_default, _ainvoke_with_retry,
_coerce_regenerate_result, _rerank_with_llm)

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')


async def route_query(state: GraphState):
    """--- УЗЕЛ: МАРШРУТИЗАЦИЯ ---"""
    _LOGGER.info("--- УЗЕЛ: МАРШРУТИЗАЦИЯ ---")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": "маршрутизация"}})
    route = await _ainvoke_structured_with_default(
        router_chain(),
        {"input": state["input"]},
        RouteQuery,
        {"destination": "rag_methodology"},
        "router"
    )
    _LOGGER.info(f"--- РЕШЕНИЕ: Направить в '{route.destination}' ---")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"--- РЕШЕНИЕ: Направить в '{route.destination}' ---"}})
    return {"destination": route.destination}


async def retrieve_documents(state: GraphState):
    """Первичный ретрив"""
    with _trace_node("retrieve_documents", history_len=len(state.get("messages", []))):
        q_orig = state["input"]

        try:
            rw = await _ainvoke_structured_with_default(
                rag_context_chain(),
                {"input": q_orig, "chat_history": state.get("messages", [])[-1:]},
                MinimalContext,
                {"is_referal": False, "minimal_context": []},
                "rag_context",
            )
            q_ctx = f"Контекст в понятиях: {rw.minimal_context}"
        except Exception:
            q_ctx = "Контекста нет"

        # AEF callback is attached once at graph invocation level in app.py.
        documents = await retriever.ainvoke(f"{q_ctx}\n{q_orig}")
        _LOGGER.info(f"vector_retrieve_done: {len(documents)}")

        documents = await asyncio.to_thread(_rerank_with_llm, q_orig, documents,
                                            top_n=max(6, settings.num_of_base_vectors))

        return {"context": documents}


async def generate_answer_rag(state: GraphState):
    """ГЕНЕРАЦИЯ ОТВЕТА"""
    with _trace_node("generate_answer_rag"):
        context_str, srcs = _format_docs(state.get("context") or [])
        resp = await _ainvoke_structured_with_default(
            rag_chain_with_confidence(),
            {"input": state["input"], "context": context_str, "chat_history": state.get("messages", [])},
            RagAnswerWithConfidence,
            {
                "answer_text": "В предоставленном контексте не нашлось достаточно данных для уверенного ответа.",
                "confidence_score": max(1, (settings.self_confidence_treshold or 7) - 1),
                "sources": srcs or [],
            },
            "rag"
        )
        return {
            "answer": resp.answer_text,
            "confidence_score": resp.confidence_score,
            "regen_attempts": state.get("regen_attempts", 0),
            "sources": resp.sources or srcs or [],
        }


async def critique_answer(state: GraphState):
    """Критика"""
    with _trace_node("critique_answer"):
        context_str = "\n\n".join([f"Содержимое: {doc.page_content}" for doc in state.get("context") or []])

        critique_result = await _ainvoke_structured_with_default(
            critique_chain(),
            {"input": state["input"], "context": context_str, "answer": state.get("answer", "")},
            Critique,
            {"is_good": True, "feedback": "LLM недоступен: пропускаю автокритику."},
            "critique"
        )
        return {"critique": critique_result.model_dump()}


async def refine_and_reretrieve(state: GraphState):
    """
    Перефраз и retrieve с проверкой дубликатов
    """
    with _trace_node("refine_and_reretrieve"):
        base_q = state["input"]
        payload = {"chat_history": state.get("messages", []), "input": base_q}

        # Асинхронный перефраз
        try:
            rewritten = await _ainvoke_structured_with_default(
                contextualize_q_prompt_template | utility_llm().with_structured_output(RewrittenQuestion),
                payload,
                RewrittenQuestion,
                {"question": base_q},
                "refine_contextualize",
            )
            aug_q = f"{base_q}\n\n(Уточненная формулировка для поиска: {rewritten.question})"
        except Exception as e:
            _LOGGER.warning(
                "[refine_and_reretrieve] Перефразирование запроса через utility_llm.with_structured_output"
                "(RewrittenQuestion) упало. Используем оригинальный запрос. "
                "Вероятно GigaChat вернул невалидную структуру: %s", e
            )
            aug_q = base_q

        fb = (state.get("critique") or {}).get("feedback")
        if fb:
            aug_q += f"\n\n(Учитывать замечания критика: {fb})"

        old_documents = state.get("context", [])

        # Асинхронный поиск + реранкинг.
        try:
            new_documents = await retriever.ainvoke(aug_q)
            _LOGGER.info(f"vector_retrieve_done: {len(new_documents)}")
            new_documents = await asyncio.to_thread(_rerank_with_llm, aug_q, new_documents,
                                                    top_n=max(6, settings.num_of_base_vectors))
        except Exception as e:
            _LOGGER.error(
                "[refine_and_reretrieve] Повторный ретрив/реранкинг упал. "
                "Оставляем старый контекст (%d документов): %s",
                len(old_documents), e
            )
            return {
                "context": old_documents,
                "is_context_identical": True
            }

        old_content_set = {doc.page_content for doc in old_documents}
        new_content_set = {doc.page_content for doc in new_documents}

        is_identical = (old_content_set == new_content_set)

        if is_identical:
            audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"REFINE: Документы не изменились. Пропускаем регенерацию."}})
            _LOGGER.info("--- REFINE: Документы не изменились. Пропускаем регенерацию. ---")
        else:
            audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"REFINE: Найдены новые документы. Регенерируем."}})
            _LOGGER.info("--- REFINE: Найдены новые документы. Регенерируем. ---")

        return {
            "context": new_documents,
            "is_context_identical": is_identical
        }


async def regenerate_answer(state: GraphState):
    """РЕГЕНЕРАЦИЯ ОТВЕТА"""
    with _trace_node("regenerate_answer"):
        context_str, srcs = _format_docs(state.get("context") or [])
        payload = {
            "input": state["input"],
            "context": context_str,
            "feedback": (state.get("critique") or {}).get("feedback", ""),
            "chat_history": state.get("messages", []),
        }

        try:
            raw = None
            try:
                raw = await _ainvoke_with_retry(
                    regenerate_prompt_template | answer_llm().with_structured_output(RegenerateResult),
                    payload,
                    node_name="regenerate_structured",
                )
            except Exception:
                pass

            if raw is None:
                raw = await _ainvoke_with_retry(
                    regenerate_prompt_template | answer_llm(),
                    payload,
                    node_name="regenerate_text",
                )

            out = _coerce_regenerate_result(raw)

            prev_answer = state.get("answer", "")
            prev_conf = state.get("confidence_score", 7)
            prev_srcs = state.get("sources", []) or []
            new_text = sanitize_formulas(out.answer_text or prev_answer).strip()

            if len(new_text) < 40 or re.fullmatch(r"(источники?:?\s*\[.*\]\s*)", new_text, re.I):
                new_text = prev_answer

            new_conf = int(out.confidence_score) if out.confidence_score is not None else prev_conf
            new_sources = (out.sources or []) or srcs or prev_srcs

            def _looks_like_source_echo(txt: str, sources: list[str]) -> bool:
                t = (txt or "").strip()
                t = re.sub(r"^[,;\s]+", "", t).strip()
                if not t:
                    return True
                if sources and any(t == s for s in sources):
                    return True
                if re.fullmatch(r"(?:[^,\n]+\.md(?:,\s*)?)+", t, flags=re.I):
                    return True
                return False

            if _looks_like_source_echo(new_text, list(sorted(set(new_sources)))):
                new_text = ("Я не могу ответить на этот вопрос по имеющимся данным. "
                            "Пожалуйста, обратитесь в поддержку ПЦП.")
                new_sources = []
                new_conf = 5

            return {
                "answer": new_text,
                "regen_attempts": state.get("regen_attempts", 0) + 1,
                "confidence_score": new_conf,
                "sources": list(sorted(set(new_sources))),
            }

        except Exception as e:
            _LOGGER.warning(f"[regenerate] error: {e}")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[regenerate] error: {e}"}})
            return {
                "answer": state.get("answer", ""),
                "regen_attempts": state.get("regen_attempts", 0) + 1,
                "confidence_score": state.get("confidence_score", 7),
                "sources": state.get("sources", []),
            }


async def format_final_answer(state: GraphState):
    """Финальное форматирование"""
    _LOGGER.info("--- ФОРМАТИРОВАНИЕ ФИНАЛА ---")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"ФОРМАТИРОВАНИЕ ФИНАЛА"}})
    try:
        final = sanitize_formulas(state["answer"])

        # двойные переносы между крупными блоками
        final = re.sub(r'\n{3,}', '\n\n', final).strip()

        if state.get("sources"):
            if "Источники:" not in final:
                final += f"\n\n**Источники:** {', '.join(sorted(set(state['sources'])))}"

        return {"final_answer": final}
    except Exception as e:
        _LOGGER.error(
            "[format_final_answer] Ошибка при sanitize_formulas или форматировании источников. "
            "answer длина=%d, кол-во sources=%d: %s",
            len(state.get("answer") or ""), len(state.get("sources") or []), e
        )
        return {"final_answer": state.get("answer", "Ошибка форматирования ответа.")}
