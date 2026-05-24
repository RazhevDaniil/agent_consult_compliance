import re
import uuid
import asyncio
import logging
from typing import Dict, Any
from datetime import date, timedelta

from .config import settings
from .calc_params_parser import DealSelector, extract_selector_and_overrides as parse_selector
from .tech_funcs import (sanitize_formulas, _fmt6, _trace_node, _format_docs,
                         resolve_requested_components, _explain_targets_from_tool_payload, _extract_any_id, _mentions_this_deal,
                         _context_ribbon, _normalize_context_from_inputs, _format_limit_schedule,
                         resolve_context, _group_large_numbers_in_text,
                         prefetched_row_from_payload)
from .tools import execute_tool, execute_report_tool
from .graph_state import (GraphState, retriever, tool_router_chain,
                          explainer_chain, user2theory_chain, tool2theory_chain)
from .graph_llm_wrappers import _rerank_with_llm

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')


def _make_theory_question_from_user(user_input: str, tool: str | None = None, requested_components: str | None = None) -> str:
    if not user_input:
        return ""
    t = user_input.strip().lower()

    first_comp = ""
    if requested_components:
        first_comp = (requested_components.split(";")[0] or "").strip()

    canon_by_label = {
        "лимит кпк": "Что такое лимит кпк и как он рассчитывается?",
        "стоимость фондирования":"что такое стоимость фондирования и как рассчитывается?",
        "eva по сделке":"что такое фактическая eva и как она рассчитывается?",
        "целевая eva":"что такое целевая eva и как она рассчитывается?",
        "индикативная eva":"что такое индикативная eva и зачем она нужна?",
        "влияние при котировании": "Что такое лимит при котировании и как рассчитывается влияние сделки на лимит при котировании?",
        "график начисления лимита по сделке": "Как формируется график начисления лимита по сделке и по каким правилам он строится?",
        "ставка безубыточности": "Что такое ставка безубыточности и как она рассчитывается?",
        "ставка не утилизирующая лимит": "Что такое ставка не утилизирующая лимит и как она рассчитывается?",
        "ставка неутилизирующая лимит": "Что такое ставка неутилизирующая лимит и как она рассчитывается?",
        "ставка НУЛ": "Что такое ставка НУЛ и как она рассчитывается?",
        "ставка НУЛ": "Что такое ставка неутилизирующая лимит и как она рассчитывается?",
        "ставка нул": "Что такое ставка неутилизирующая лимит и как она рассчитывается?",
        "ставка НУЛ": "Что такое ставка не утилизирующая лимит и как она рассчитывается?",
        "ставка нул": "Что такое ставка не утилизирующая лимит и как она рассчитывается?",
        "ставка нул": "Что такое ставка нул и как она рассчитывается?",
        "етс": "Что такое етс?",
        "фор": "Что такое фор?",
    }

    if first_comp:
        q = canon_by_label.get(first_comp.lower())
        if q:
            return q if q.endswith("?") else q + "?"

    if ("лимит" in t) and ("котир" in t):
        return "Что такое лимит при котировании и как он рассчитывается?"
    if ("график" in t) and ("лимит" in t):
        return "Как формируется график начисления лимита по сделке и по каким правилам он строится?"
    if ("безубыточ" in t) and ("ставк" in t):
        return "Что такое ставка безубыточности и как она рассчитывается?"

    if tool == "pricing" and "ставк" in t:
        return "Из каких компонент состоит ставка по банковскому продукту и как она рассчитывается?"
    if tool == "limits" and "лимит" in t:
        return "Как рассчитывается влияние сделки на лимит при котировании?"

    return ""


def _should_attach_methodology_block(context_str: str, srcs: list[str], llm_text: str) -> bool:
    """
    Не приклеиваем методологию, если ретрив ничего не дал
    или LLM явно ответил, что в контексте нет нужного описания.
    """
    if not (context_str or "").strip() or not srcs:
        return False

    cleaned = re.sub(r"\s+", " ", (llm_text or "")).strip().lower()
    if not cleaned:
        return False

    negative_prefixes = (
        "в предоставленном контенте нет описания",
        "в предоставленном контексте нет описания",
        "в предоставленном контенте отсутствует",
        "в предоставленном контексте отсутствует",
        "в предоставленном контенте не найдено",
        "в предоставленном контексте не найдено",
        "в данный момент пояснение методологии недоступно",
    )
    if any(cleaned.startswith(prefix) for prefix in negative_prefixes):
        return False

    weak_answer_pattern = (
        r"(не найден[аоы]?|не нашл[аиоы]?|нет описани|нет информац|нет данных|"
        r"недостаточно данных|не удалось найти|отсутствует описани|отсутствует информац)"
    )
    if len(cleaned) <= 220 and re.search(weak_answer_pattern, cleaned):
        return False

    return True


async def execute_tool_node(state: GraphState, config=None):
    """
    Вызов инструмента: execute_tool внутри синхронный
    Оборачиваем в to_thread, чтобы не блокировать Event Loop.
    auth_header читается из RunnableConfig.configurable (Шаг 2.4);
    tool_cache живёт в state (Шаг 2.5).
    """
    configurable = (config or {}).get("configurable") or {}
    auth_header = configurable.get("auth_header")
    trace_id = configurable.get("trace_id")
    operation_uid = configurable.get("operation_uid")
    chat_id = configurable.get("thread_id") or "default"
    with _trace_node("execute_tool"):
        route = await tool_router_chain().ainvoke({"input": state["input"], "chat_history": state.get("messages", [])})

        tool = route.tool  # "pricing", "limits", "deals_report" или "none"

        if tool == "deals_report":
            return {
                "destination": "deals_report",
                "tool_payload": None,
                "skip_explain": True
            }

        txt = (state["input"] or "").lower()

        if any(p in txt for p in ["оба", "всё сразу", "все сразу", "и лимит", "и компонент", "компоненты и лимит"]):
            tool = "both"
        if tool == "none":
            t = txt
            tool = "limits" if ("лимит" in t or "limit" in t) else "pricing"

        explicit_for_another_input: Dict[str, Any] = {}
        any_id_for_another_input = _extract_any_id(state["input"])
        if any_id_for_another_input:
            explicit_for_another_input["deal_id"] = any_id_for_another_input
        else:
            audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"DEAL ID not found. Chat ID: {chat_id}"}})
            _LOGGER.info(f"--- DEAL ID not found. Chat ID: {chat_id} ---")

        # 1) парсер селектора
        sel = parse_selector(state["input"], explicit_for_another_input)

        # 2) строгое разрешение контекста (читает state.last_selector,
        # сохранённый в чекпоинте на предыдущем ходу)
        ctx = resolve_context(state["input"], sel, state)

        # 3) prefetched_row только при CONTINUE
        prefetched_row = None
        if ctx.mode == "CONTINUE":
            snap = prefetched_row_from_payload(state.get("last_tool_payload"))
            if snap:
                prefetched_row = snap

        # 4) готовим explicit по решению резолвера
        explicit_call = {}
        if ctx.mode == "NEW_WITH_ID":
            explicit_call = ctx.explicit_params
        elif ctx.mode == "NEW_WITH_PARAMS":
            explicit_call = ctx.explicit_params
        elif ctx.mode == "CONTINUE":
            sel_dict = state.get("last_selector") or {}
            explicit_call = sel_dict
            sel = DealSelector(**sel_dict)

        if ctx.mode == "INSUFFICIENT_PARAMS":
            miss = ctx.explicit_params.get("missing") if isinstance(ctx.explicit_params, dict) else []
            rus = {"inn":"ИНН","product":"продукт","currency":"валюта","amount":"сумма",
                   "deal_dt":"дата сделки (YYYY-MM-DD)","interest_rate":"ставка, %"}
            miss_human = ", ".join(rus.get(m, m) for m in miss) if miss else "не указаны параметры сделки"
            hint = ("Например: ИНН=7705..., продукт=NSO, валюта=RUB, "
                    "сумма=1.2 млрд, дата=2025-08-05, ставка=16.63% — или укажите id сделки/расчёта.")
            return {
                "answer": f"Недостаточно данных для идентификации сделки: {miss_human}. {hint}",
                "sources": [],
                "confidence_score": 6,
                "tool_payload": None,
                "requested_components": [],
                "response_state": "input-required",
            }

        # 5) запуск инструмента
        loop = asyncio.get_running_loop()
        tool_cache_in = state.get("tool_cache") or {}

        def _run_tool():
            return execute_tool(
                tool,
                user_text=state["input"],
                explicit=explicit_call or None,
                prebuilt_selector=sel,
                prefetched_row=prefetched_row,
                auth_header=auth_header,
                trace_id=trace_id,
                operation_uid=operation_uid,
                tool_cache=tool_cache_in,
            )

        res, from_cache, tool_cache_out = await loop.run_in_executor(None, _run_tool)

        if res.get("status") != "success":
            err = res.get("error", "Расчёт не выполнен.")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"ERROR due calculation. Chat ID: {chat_id} . Error: {err}"}})
            _LOGGER.info(f"--- ERROR due calculation. Chat ID: {chat_id} . Error: {err} ---")
            return {
                "answer": f"Кажется я сломался, но скоро все починим! Если необходима помощь, обратитесь в поддержку FCBusinessSupportTeam@sberbank.ru.",
                "sources": [],
                "confidence_score": 5,
                "tool_payload": None,
                "requested_components": [],
                "response_state": "failed",
            }

        data = res["data"]

        # 6) СОХРАНЕНИЕ ПАМЯТИ — собираем final_selector. Все cross-turn
        # поля летят в state через return ниже.
        inputs_used = data.get("inputs_used") or {}
        normalized = _normalize_context_from_inputs(inputs_used)
        final_selector = {
            "deal_id": normalized.get("deal_id") or normalized.get("internal_order_cd") or f"local-{uuid.uuid4().hex[:8]}",
            "product": normalized.get("product"),
            "currency": normalized.get("ccy"),
            "amount": normalized.get("amount"),
            "deal_dt": normalized.get("deal_dt"),
            "maturity_dt": normalized.get("maturity_dt"),
            "term": normalized.get("term"),
            "interest_rate": normalized.get("interest_rate"),
        }
        for k in ("inn","product","currency","amount","deal_dt","maturity_dt","term","interest_rate"):
            if not final_selector.get(k):
                final_selector[k] = getattr(sel, k, None) or (explicit_call.get(k) if isinstance(explicit_call, dict) else None)

        # 7) текущий вывод
        comps = data.get("components", {}) or {}
        explain_map = data.get("explain_map", {}) or {}
        available_keys = list(comps.keys())
        # requested = _detect_requested_components(state["input"], tool, available_keys)

        resolution = None
        requested = []
        if tool != "both":
            resolution = resolve_requested_components(state["input"], tool, available_keys)
            requested = resolution.requested
            _LOGGER.info(
                "--- Requested component resolution: tool=%s matched_by=%s confidence=%.2f requested=%s clarification=%s ---",
                tool,
                resolution.matched_by,
                resolution.confidence,
                requested,
                resolution.needs_clarification,
            )
            if resolution.needs_clarification:
                return {
                    "answer": resolution.clarification_text,
                    "sources": [],
                    "confidence_score": 7,
                    "tool_payload": None,
                    "requested_components": [],
                    "response_state": "input-required",
                }

        def _fmt_component_value(key: str, value):
            if key.startswith("Влияние"):
                try:
                    return f"{float(value):,.2f}".replace(",", " ")
                except Exception:
                    return "n/a"
            return _fmt6(value)

        def _lines_for(keys: list[str]) -> list[str]:
            lines = []
            ex = explain_map

            for k in keys:
                if k not in comps:
                    continue
                label = label_map.get(k, k)
                val = comps[k]
                expl = ex.get(k)

                if k == "График начисления лимита по сделке":
                    lines.append(_format_limit_schedule(label, val))
                else:
                    val_fmt = _fmt_component_value(k, val)
                    block = f"- **{label}:** {val_fmt}"
                    if expl:
                        block += f"\n\n  _Краткое описание:_ {_group_large_numbers_in_text(str(expl))}"
                    lines.append(block)
            return lines

        if tool == "both":
            keys_to_show = [
                "break_even_rate",
                "non_utilizing_rate",
                "Влияние на лимит при котировании",
                "Влияние на лимит действующей сделки",
            ]
        else:
            default_keys = (["break_even_rate"] if tool=="pricing" else ["Влияние на лимит при котировании"])
            keys_to_show = requested or default_keys
        if tool != "pricing" and "график" in (state.get("input","").lower()):
            keys_to_show = list(dict.fromkeys(keys_to_show + ["График начисления лимита по сделке"]))

        pricing_labels = {
                "ets":"ЕТС","for_rate":"ФОР","funding_rate":"Стоимость фондирования",
                "eva_rate":"фактическая EVA по сделке","break_even_rate":"Ставка безубыточности",
                "non_utilizing_rate":"Ставка не утилизирующая лимит",
                "marginal_income":"Маржинальный доход","target_marginal_income":"Целевой маржинальный доход",
            }

        limits_labels = {
                "Влияние на лимит при котировании":"Влияние при котировании",
                "Влияние на лимит действующей сделки":"Влияние на лимит действующей сделки",
                "Влияние на лимит КПК действующей сделки":"Влияние на лимит КПК по действующей сделке",
                "Влияние на лимит ЦА действующей сделки":"Влияние на лимит ЦА по действующей сделке",
                "График начисления лимита по сделке":"График начисления лимита по сделке",
            }

        if tool == "pricing":
            label_map = pricing_labels
        elif tool == "limits":
            label_map = limits_labels
        else:  # both
            label_map = {**pricing_labels, **limits_labels}

        requested_labels = [label_map.get(k, k) for k in keys_to_show]
        requested_human = "; ".join(requested_labels)

        components_keys = ", ".join(sorted(comps.keys()))
        short_explains = "; ".join(f"{k}: {str(v)[:80]}" for k, v in explain_map.items())
        targets = _explain_targets_from_tool_payload(data)

        ribbon = _context_ribbon(
            deal_id=final_selector.get("deal_id"),
            tool=tool,
            inputs=(data.get("inputs_used") or {}),
            continued=(ctx.mode == "CONTINUE")
        )

        body_lines = _lines_for(keys_to_show)
        requested_block = f"**Выведены компоненты:** {requested_human}" if requested_human else ""

        answer_md = "\n".join([
            f"### Итог расчёта {ribbon}",
            requested_block,
            "",
            *body_lines
        ])

        return {
            "tool_payload": data,
            "answer": answer_md.strip(),
            "confidence_score": 9,
            "sources": [],
            "requested_components": requested_human,
            "payload_components": targets,
            "theory_inputs": {
                "tool": tool,
                "components_keys": components_keys,
                "short_explains": short_explains,
                "user_input": state["input"],
            },
            "skip_explain": (tool == "both"),
            # Cross-turn slice persisted by the checkpointer so the
            # next ainvoke with the same thread_id sees this context.
            "last_tool_payload": data,
            "last_tool_kind": tool,
            "last_selector": final_selector,
            "tool_cache": tool_cache_out,
        }


def after_execute_tool(state: GraphState):
    """
    Решает, куда идти после execute_tool:
    1. Если это был отчет -> идем в deals_report
    2. Если skip_explain -> финалим
    3. Иначе -> идем объяснять методологию
    """
    if state.get("destination") == "deals_report":
        return "deals_report"

    if state.get("skip_explain"):
        return "final"

    return "explain" if state.get("tool_payload") else "final"


async def deals_report(state: GraphState, config=None):
    """УЗЕЛ: ОТЧЕТ ПО СДЕЛКАМ"""
    configurable = (config or {}).get("configurable") or {}
    auth_header = configurable.get("auth_header")
    trace_id = configurable.get("trace_id")
    operation_uid = configurable.get("operation_uid")
    chat_id = configurable.get("thread_id") or "default"
    with _trace_node("deals_report"):
        current_date = date.today().isoformat()
        days_before_30 = (date.today() - timedelta(days=30)).isoformat()
        inn_list = re.findall(r'\b\d{10}\b|\b\d{12}\b', state["input"])

        if not inn_list:
            return {
                "final_answer": "Чтобы сформировать отчет по сделкам, укажите ИНН клиента(ов) (10/12 цифр)",
                "response_state": "input-required",
            }

        loop = asyncio.get_running_loop()

        def _run_report():
            return execute_report_tool(
                period_start=days_before_30,
                period_end=current_date,
                inns=inn_list,
                auth_header=auth_header,
                trace_id=trace_id,
                operation_uid=operation_uid,
            )

        try:
            res = await loop.run_in_executor(None, _run_report)
        except Exception as e:
            _LOGGER.error(f"Report tool failed: {e}")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"Report tool failed: {e}"}})
            return {"final_answer": f"Ошибка при формировании отчета: {e}", "response_state": "failed"}

        if res.get("status") != "success":
            err = res.get("error", "Расчёт не выполнен.")
            _LOGGER.info(f"--- ERROR due report. Chat ID: {chat_id} . Error: {err} ---")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"ERROR due report. Chat ID: {chat_id} . Error: {err}"}})
            return {"final_answer": f"Кажется, я сломался! Но скоро все починим! Если необходима помощь, то советую обратиться к команде бизнес-поддержки ПЦП. e-mail: FCBusinessSupportTeam@sberbank.ru",
                    "response_state": "failed"}

        final_answer = res.get("result")
        if not final_answer:
            _LOGGER.info(f"--- ERROR due report. Chat ID: {chat_id} . Error: No result")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"ERROR due report. Chat ID: {chat_id} . Error: No result"}})
            final_answer = 'Кажется, я сломался! Но скоро все починим! Если необходима помощь, то советую обратиться к команде бизнес-поддержки ПЦП. e-mail: FCBusinessSupportTeam@sberbank.ru'

        m = re.search(r"Общее количество сделок:\s*(\d+)", final_answer)
        if m and int(m.group(1)) == 0:
            if inn_list:
                return {"final_answer": (
                    f"Сделки не найдены по ИНН: {', '.join(inn_list)} (за период в 30 дней)."
                )}
            _LOGGER.info(f"--- ERROR due report. Chat ID: {chat_id} . Error: No deals found")
            audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"ERROR due report. Chat ID: {chat_id} . Error: No deals found"}})
            return {"final_answer": "Сделки не найдены за период в 30 дней."}

        return {"final_answer": final_answer}


async def retrieve_documents_for_explain(state: GraphState):
    """RAG для пояснения"""
    with _trace_node("retrieve_documents_for_explain"):
        ti = state.get("theory_inputs") or {}
        tool = ti.get("tool") or ("pricing" if "ЕТС" in (state.get("answer") or "") else "limits")

        try:
            payload = {
                "tool": tool,
                "components_keys": ti.get("components_keys", ""),
                "short_explains": ti.get("short_explains", ""),
                "user_input": ti.get("user_input", state.get("input", "")),
            }

            user_input = ti.get("user_input", state.get("input", ""))
            requested_components = state.get("requested_components") or ""

            theory_q = _make_theory_question_from_user(user_input, tool=tool, requested_components=requested_components)

            if not theory_q:
                rq = await user2theory_chain().ainvoke({
                    "tool": tool,
                    "user_input": user_input,
                    "requested_components": requested_components,
                })
                theory_q = (rq.question or "").strip()

            if not theory_q:
                rq = await tool2theory_chain().ainvoke(payload)
                theory_q = (rq.question or "").strip()

            # AEFHandler автоматически создаёт `retriever` span для FAISS.ainvoke.
            documents = await retriever.ainvoke(theory_q)
            _LOGGER.info("vector_retrieve_done")

            documents = await asyncio.to_thread(
                _rerank_with_llm,
                theory_q,
                documents,
                top_n=max(6, settings.num_of_base_vectors)
            )
        except Exception as e:
            _LOGGER.warning(
                "retrieve_documents_for_explain_failed. Returning calculation answer without methodology. error=%s",
                e,
                exc_info=True,
            )
            return {
                "context": [],
                "theory_question": "",
                "theory_inputs": {**ti, "explain_error": str(e)},
            }

        ti_out = {**ti, "last_theory_question": theory_q}

        return {
            "context": documents,
            "theory_question": theory_q,
            "theory_inputs": ti_out,
        }


async def generate_explainer(state: GraphState):
    """Генерация объяснения"""
    with _trace_node("generate_explainer"):
        context_str, srcs = _format_docs(state.get("context") or [])
        base_answer = (state.get("answer") or "").strip()
        theory_q = state.get("theory_question") or \
                   (state.get("theory_inputs") or {}).get("last_theory_question") or ""

        default_text = "В данный момент пояснение методологии недоступно. Привожу итог расчёта без расширенных пояснений."

        if not context_str.strip() or not srcs:
            _LOGGER.info("--- Skip methodology block: no relevant RAG context for explainer ---")
            return {
                "answer": base_answer,
                "sources": list(sorted(set(state.get("sources") or []))),
                "confidence_score": state.get("confidence_score", 0),
                "theory_question": theory_q,
            }

        try:
            expl = await explainer_chain().ainvoke(
                {
                    "explain_targets": theory_q or "Общее пояснение по методологии",
                    "context": context_str,
                    "chat_history": state.get("messages", [])
                }
            )
            llm_text = sanitize_formulas(getattr(expl, "content", str(expl)))
        except Exception:
            llm_text = default_text

        if not _should_attach_methodology_block(context_str, srcs, llm_text):
            _LOGGER.info("--- Skip methodology block: explainer returned no useful methodology ---")
            return {
                "answer": base_answer,
                "sources": list(sorted(set(state.get("sources") or []))),
                "confidence_score": state.get("confidence_score", 0),
                "theory_question": theory_q,
            }


        merged = "\n\n".join([
            # state.get("answer", "").strip(),
            base_answer,
            f"### Методология (кратко)",
            f"*Вопрос для пояснения:* {theory_q}" if theory_q else "",
            llm_text.strip()
        ]).strip()

        return {
            "answer": merged,
            "sources": list(sorted(set((state.get("sources") or []) + (srcs or [])))),
            "confidence_score": max(state.get("confidence_score", 0), 8),
            "theory_question": theory_q,
        }
