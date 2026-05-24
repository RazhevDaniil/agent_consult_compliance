"""
Узлы графа: KPK-пайплайн (анализ лимитов подразделений).
Препроцессинг, парсинг запроса, вызов инструмента, генерация ответа, финализация.
"""
import re
import asyncio
import datetime
import logging
import pandas as pd
from typing import Any, Dict

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')

from .config import settings, ERROR_TEXT

from .kpk_tools import execute_kpk_limits_tool

from .kpk_table_formatters import (
    generate_kpk_report_markdown,
    generate_all_kpk_report_markdown,
    generate_find_deal_markdown,
    generate_investigate_deal_markdown,
    generate_client_history_markdown,
)

from .graph_state import GraphState, _kpk_parse_chain, _kpk_answer_chain

from .graph_llm_wrappers import _kpk_safe_structured_invoke, _kpk_safe_text_invoke


# =====================================================================================
# КОНСТАНТЫ
# =====================================================================================

_KPK_DATE_ALIASES = {
    "позавчера": 2,
    "вчера": 1,
    "сегодня": 0,
    "завтра": -1,
}

_RUSSIAN_MONTH_MAP = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}

_RUSSIAN_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_RUSSIAN_MONTH_MAP.keys()) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
    )

_FULL_DOT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
_SHORT_DOT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?!\.?\d)")

_RUSSIAN_ORDINAL_DAY_MAP = {
    "первого": 1,
    "второго": 2,
    "третьего": 3,
    "четвертого": 4,
    "четвёртого": 4,
    "пятого": 5,
    "шестого": 6,
    "седьмого": 7,
    "восьмого": 8,
    "девятого": 9,
    "десятого": 10,
    "одиннадцатого": 11,
    "двенадцатого": 12,
    "тринадцатого": 13,
    "четырнадцатого": 14,
    "пятнадцатого": 15,
    "шестнадцатого": 16,
    "семнадцатого": 17,
    "восемнадцатого": 18,
    "девятнадцатого": 19,
    "двадцатого": 20,
    "двадцать первого": 21,
    "двадцать второго": 22,
    "двадцать третьего": 23,
    "двадцать четвертого": 24,
    "двадцать четвёртого": 24,
    "двадцать пятого": 25,
    "двадцать шестого": 26,
    "двадцать седьмого": 27,
    "двадцать восьмого": 28,
    "двадцать девятого": 29,
    "тридцатого": 30,
    "тридцать первого": 31,
}

_RUSSIAN_WORD_DATE_RE = re.compile(
    r"\b("
    + "|".join(re.escape(key) for key in sorted(_RUSSIAN_ORDINAL_DAY_MAP, key=len, reverse=True))
    + r")\s+("
    + "|".join(_RUSSIAN_MONTH_MAP.keys())
    + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
    )


# =====================================================================================
# ХЕЛПЕРЫ
# =====================================================================================

def _kpk_today() -> datetime.date:
    override = settings.kpk_today_override
    if override:
        try:
            return datetime.date.fromisoformat(override)
        except ValueError:
            _LOGGER.warning(f"[kpk] Invalid KPK_TODAY_OVERRIDE='{override}', using real today")
    return datetime.date.today()


def _kpk_today_iso() -> str:
    return _kpk_today().isoformat()


def _kpk_parse_iso_date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None

def _kpk_shift_iso_date(value: Any, days: int) -> str | None:
    dt = _kpk_parse_iso_date(value)
    if not dt:
        return None
    return (dt + datetime.timedelta(days=days)).isoformat()

def _kpk_morning_date_label(tool_payload: dict) -> str:
    as_of_dt = tool_payload.get("as_of_morning_dt")
    if as_of_dt:
        return f"УТРО {as_of_dt}"
    return str(tool_payload.get("report_dt") or "Н/Д")

def _kpk_detect_date_basis_choice(text: str) -> str | None:
    text_lower = (text or "").lower()

    include_day_patterns = [
        r"с\s+уч[её]том",
        r"уч[её]сть\s+сдел",
        r"уч[её]сть\s+(?:этот|указанн\w+)\s+день",
        r"учитыва(?:й|ем|ть)?\w*\s+сдел",
        r"включи\w*\s+сдел",
        r"за\s+этот\s+день",
        r"за\s+указанн\w+\s+день",
        r"на\s+утро\s+следующ",
        r"следующ\w+\s+дн",
    ]
    if any(re.search(pattern, text_lower) for pattern in include_day_patterns):
        return "include_day"

    morning_patterns = [
        r"на\s+утро(?!\s+следующ)",
        r"утро\s+указанн",
        r"без\s+уч[её]та",
        r"не\s+учитыва",
        r"до\s+сдел",
    ]
    if any(re.search(pattern, text_lower) for pattern in morning_patterns):
        return "morning"

    return None

def _kpk_uses_morning_analysis_dates(data: dict) -> bool:
    # investigate_deal treats report_dt as value_dt of a deal, not as a
    # limit/deals showcase partition. client_history has its own period logic.
    return data.get("intent") not in {"investigate_deal", "client_history"}

def _kpk_date_clarification_message(requested_dt: str) -> str:
    next_morning_dt = _kpk_shift_iso_date(requested_dt, 1) or requested_dt
    return (
        f"Уточните, пожалуйста: подготовить анализ **на УТРО {requested_dt}** "
        f"или **с учетом сделок за {requested_dt}** "
        f"(то есть на УТРО {next_morning_dt})?"
    )

def _kpk_apply_morning_dates(
        data: dict,
        text_input: str,
        *,
        explicit_date: bool | None = None,
        date_basis_choice: str | None = None,
) -> tuple[dict, dict | None, str | None]:
    """Resolve user-facing morning date and technical tool report date.

    Returns: (updated_data, pending_context, clarification_message).
    """
    data = dict(data or {})
    if not _kpk_uses_morning_analysis_dates(data):
        return data, None, None

    today = _kpk_today()
    if explicit_date is None:
        explicit_date = _text_contains_explicit_date_hint(text_input)

    requested_dt = _kpk_parse_iso_date(data.get("report_dt")) if explicit_date else today
    if not requested_dt:
        requested_dt = today

    requested_iso = requested_dt.isoformat()
    data["requested_dt"] = requested_iso
    data["report_dt"] = requested_iso

    if not explicit_date:
        data["report_dt_from"] = None

    report_dt_from = data.get("report_dt_from")
    requested_from_dt = _kpk_parse_iso_date(report_dt_from)
    if requested_from_dt:
        data["requested_dt_from"] = requested_from_dt.isoformat()

    if date_basis_choice == "include_day" and requested_dt >= today:
        return data, {"kind": "kpk_data_unavailable"}, (
            f"Данные с учетом сделок за {requested_iso} пока недоступны. "
            f"Снапшот лимита на утро следующего рабочего дня ещё не сформирован."
        )

    if explicit_date and requested_dt < today and date_basis_choice is None:
        pending = {
            "kind": "kpk_date_basis",
            "parsed": data,
            "requested_dt": requested_iso,
        }
        return data, pending, _kpk_date_clarification_message(requested_iso)

    if date_basis_choice == "include_day":
        as_of_morning_dt = requested_dt + datetime.timedelta(days=1)
        tool_report_dt = as_of_morning_dt
        from_shift_days = 1
        basis = "include_day"
    else:
        as_of_morning_dt = requested_dt
        tool_report_dt = requested_dt
        from_shift_days = 0
        basis = "morning"

    data["as_of_morning_dt"] = as_of_morning_dt.isoformat()
    data["tool_report_dt"] = tool_report_dt.isoformat()
    data["data_report_dt"] = tool_report_dt.isoformat()
    data["date_basis"] = basis

    if requested_from_dt:
        data["tool_report_dt_from"] = (
                requested_from_dt + datetime.timedelta(days=from_shift_days)
        ).isoformat()
        data["data_report_dt_from"] = data["tool_report_dt_from"]

    return data, None, None


def _investigate_found_in_deals_intro(tool_payload: Dict[str, Any]) -> str:
    """Текст для found_in_deals с акцентом на появление сделки сегодня."""
    deal_matches = tool_payload.get("deal_matches") or []
    if not deal_matches:
        return "Сделка найдена в расчете лимита."

    upload_dt = str(deal_matches[0].get("upload_dt") or "").strip()
    today_iso = _kpk_today_iso()
    if upload_dt == today_iso:
        today_label = pd.Timestamp(today_iso).strftime("%d.%m.%Y")
        return f"Сделка появилась в расчете лимита только сегодня, {today_label}."

    return "Сделка найдена в расчете лимита."


def _kpk_fmt(val) -> str:
    try:
        return f"{float(val):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(val) if val is not None else "н/д"


def _format_code_number(val) -> str:
    try:
        num = float(str(val).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return str(val) if val is not None else ""

    if num.is_integer():
        return f"{int(num):,}".replace(",", " ")
    return f"{num:,.2f}".replace(",", " ")


def _kpk_safe_int(val, default: int = 7) -> int:
    try:
        if val is None:
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _redistribution_summary_label(amount) -> str:
    amt = _safe_float_like(amount)
    if amt < 0:
        return "Перераспределения с данного КПК на другие КПК"
    if amt > 0:
        return "Перераспределения с других КПК на данный КПК"
    return "Перераспределения по КПК"


def _safe_float_like(val, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(str(val).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _should_skip_number_sanitizing(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 24):start].lower()
    after = text[end:min(len(text), end + 24)].lower()
    context = f"{before}{after}"

    if start > 0 and text[start - 1] in ".-/" and end < len(text) and text[end:end + 1] in ".-/":
        return True
    if any(token in context for token in ("инн", "кпк", "division_cd", "id сделки", "internal_order_cd", "order_cd")):
        return True
    return False


def _sanitize_code_generated_numbers(text: str) -> str:
    if not text:
        return text

    pattern = re.compile(r"(?<![\d ])-?\d{5,}(?:[.,]\d+)?")

    def _replace(match: re.Match) -> str:
        start, end = match.span()
        if _should_skip_number_sanitizing(text, start, end):
            return match.group(0)
        return _format_code_number(match.group(0))

    return pattern.sub(_replace, text)


def _kpk_build_all_intro(tool_payload: dict) -> str:
    count = tool_payload.get("negative_count", 0)
    dt = tool_payload.get("as_of_morning_dt") or tool_payload.get("report_dt")
    return (
        f"На утро {dt} выявлено **{count}** КПК с отрицательным лимитом или значительным падением.\n\n"
        "<TABLES_GO_HERE>"
    )


def _build_incorrect_deals_report_meta(state: GraphState, tool_payload: dict) -> dict | None:
    parsed = state.get("parsed") or {}
    report_dt = tool_payload.get("report_dt") or parsed.get("report_dt")
    if tool_payload.get("mode") != "all" or not report_dt:
        return None
    as_of_dt = tool_payload.get("as_of_morning_dt") or parsed.get("as_of_morning_dt")
    title = "Отчет по новым сделкам по КПК с отрицательным лимитом"
    if as_of_dt:
        title = f"{title} на утро {as_of_dt}"
    return {
        "kind": "incorrect_deals_report",
        "report_dt": report_dt,
        "title": title,
    }


def _append_generated_report_notice(text: str) -> str:
    note = (
        "Также сформирован отчет по новым сделкам по КПК с отрицательным лимитом. "
        "Его можно выгрузить по кнопке под ответом."
    )
    placeholder_re = re.compile(r"\n*\s*<?\bTABLES[\s_-]*GO[\s_-]*HERE\b>?", re.IGNORECASE)
    if placeholder_re.search(text):
        return placeholder_re.sub(f"\n\n{note}\n\n<TABLES_GO_HERE>", text, count=1)
    return text.rstrip() + "\n\n" + note


def _kpk_limit_effect_label(amount) -> str:
    amt = _safe_float_like(amount)
    if amt < 0:
        return "возврат в лимит"
    if amt > 0:
        return "изъятие из лимита"
    return "изменение лимита"


def _kpk_disappeared_effect_totals(tool_payload: dict) -> tuple[float, float]:
    disappeared = tool_payload.get("disappeared_deals_analysis") or []
    returned_total = 0.0
    withdrawn_total = 0.0

    for deal in disappeared:
        amt = _safe_float_like(deal.get("original_delta_limit_amt"))
        if amt < 0:
            returned_total += abs(amt)
        elif amt > 0:
            withdrawn_total += abs(amt)

    return returned_total, withdrawn_total


def _kpk_build_single_summary_input(tool_payload: dict) -> str:
    deals_analysis = tool_payload.get("deals_analysis", {}) or {}
    discounting = tool_payload.get("discounting_analysis", {}) or {}
    top_up = tool_payload.get("top_up_option_analysis", {}) or {}
    report_dt = tool_payload.get("report_dt", "Н/Д")
    as_of_morning_dt = tool_payload.get("as_of_morning_dt")
    data_report_dt = tool_payload.get("data_report_dt") or report_dt
    delta = _kpk_fmt(tool_payload.get("delta_limit_amt"))
    redist_total = tool_payload.get("redistribution_total")
    redist = _kpk_fmt(redist_total)
    deals_pos = _kpk_fmt(deals_analysis.get("deals_impact_correct_pos"))
    deals_neg = _kpk_fmt(deals_analysis.get("deals_impact_correct_neg"))
    deals_incorrect_total = _safe_float_like(deals_analysis.get("deals_impact_incorrect"))
    discount_impact = _kpk_fmt(discounting.get("discount_impact_total"))
    top_up_total = _safe_float_like(top_up.get("top_up_total_impact"))
    redistribution_label = _redistribution_summary_label(redist_total)

    lines = [
        f"Анализ на утро {as_of_morning_dt}" if as_of_morning_dt else f"Дата: {report_dt}",
        f"Изменение лимита КПК: {delta} руб.",
        f"{redistribution_label}: {redist} руб.",
        f"Положительное влияние сделок: {deals_pos} руб.",
        f"Отрицательное влияние сделок: {deals_neg} руб.",
        f"Влияние дисконтирования: {discount_impact} руб.",
    ]
    if as_of_morning_dt and data_report_dt:
        lines.insert(1, f"Дата данных витрины: {data_report_dt}")
    if deals_incorrect_total != 0:
        lines.append(
            f"Влияние сделок, требующих ручной проверки: {_kpk_fmt(deals_incorrect_total)} руб."
        )
    if top_up_total != 0:
        lines.append(f"Влияние активации опции пополнения: {_kpk_fmt(top_up_total)} руб.")

    returned_total, withdrawn_total = _kpk_disappeared_effect_totals(tool_payload)
    if returned_total > 0:
        lines.append(f"Возврат в лимит {_kpk_fmt(returned_total)} руб. по отозванным сделкам.")
    if withdrawn_total > 0:
        lines.append(f"Изъятие из лимита {_kpk_fmt(withdrawn_total)} руб. по отозванным сделкам.")

    return "\n".join(lines)


def _kpk_disappeared_summary_sentence(tool_payload: dict) -> str:
    returned_total, withdrawn_total = _kpk_disappeared_effect_totals(tool_payload)
    parts = []
    if returned_total > 0:
        parts.append(f"возврат в лимит {_kpk_fmt(returned_total)} руб. по отозванным сделкам")
    if withdrawn_total > 0:
        parts.append(f"изъятие из лимита {_kpk_fmt(withdrawn_total)} руб. по отозванным сделкам")
    if not parts:
        return ""
    return "Также был " + " и ".join(parts) + "."


def _insert_summary_before_tables(text: str, extra_sentence: str) -> str:
    if not extra_sentence:
        return text
    placeholder_re = re.compile(r"\n*\s*<?\bTABLES[\s_-]*GO[\s_-]*HERE\b>?", re.IGNORECASE)
    if placeholder_re.search(text):
        return placeholder_re.sub(f"\n\n{extra_sentence}\n\n<TABLES_GO_HERE>", text, count=1)
    return text.rstrip() + "\n\n" + extra_sentence


def _extract_amount_from_text(text: str):
    normalized = re.sub(
        r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D\u00AD]",
        "-",
        text,
    )
    scrubbed = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", normalized)
    scrubbed = re.sub(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", " ", scrubbed)

    match = re.search(
        r"(?:влияни\w*|на\s+сумму)\s+(-?\s*\d[\d\s]*(?:[.,]\d+)?)",
        scrubbed,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(-\s*\d[\d\s]*(?:[.,]\d+)?)\s*(?:руб|\u20bd)",
            scrubbed,
            re.IGNORECASE,
        )
    if not match:
        negative_match = re.search(
            r"(?:уменьшил\w*|упал\w*|снизил\w*|падени\w*).*?(?:на|примерно\s+на)\s+"
            r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:руб|\u20bd)?",
            scrubbed,
            re.IGNORECASE,
        )
        if negative_match:
            raw = negative_match.group(1)
            raw = re.sub(r"(?<=\d)\s+(?=\d)", "", raw).strip().replace(",", ".")
            try:
                return -abs(float(raw))
            except ValueError:
                return None
    if not match:
        match = re.search(
            r"сделк\w*\s+(?:с\s+)?(-\s*\d[\d\s]*(?:[.,]\d+)?)",
            scrubbed,
            re.IGNORECASE,
        )
    if not match:
        match = re.search(r"(?<![\d-])(-\s*\d[\d\s]{3,}(?:[.,]\d+)?)(?!-\d)", scrubbed)
    if not match:
        return None

    raw = re.sub(r"(?<=\d)\s+(?=\d)", "", match.group(1))
    raw = raw.strip().replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _infer_intent_from_text(text: str, parsed: dict):
    text_lower = text.lower()
    inn_num = parsed.get("inn_num")

    if inn_num and re.search(
            r"(истори\w*\s+сдел|что\s+делал\s+клиент|какие\s+сделки\s+были\s+у\s+клиента|"
            r"почему\s+перестал\s+пополнять\s+лимит|раньше\s+клиент.*пополнял\s+лимит)",
            text_lower,
    ):
        return "client_history"

    if inn_num and re.search(
            r"(где\s+сделк|не\s+вижу\s+влияни|не\s+отразил|не\s+отразила|не\s+отразилась|"
            r"перестал\w*\s+влиять|перестала\s+влиять|не\s+влияет|не\s+вернул|"
            r"куда\s+пропал\w*\s+влияни|досрочно\s+закрыл)",
            text_lower,
    ):
        return "investigate_deal"

    if re.search(r"(что\s+повлиял|за\s+сч[её]т\s+чего|фактор\w*)", text_lower):
        return "factors"

    if re.search(r"перераспредел", text_lower):
        return "redistribution"

    if not inn_num and parsed.get("delta_amt") not in (None, 0) and re.search(
            r"(какая\s+сделк|что\s+за\s+сделк|кто\s+повлиял|найди\s+сделк)",
            text_lower,
    ):
        return "find_deal"

    return None


def _extract_investigate_value_date(text: str):
    match = re.search(r"\bот\s+(\d{4}-\d{2}-\d{2})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)

    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if len(dates) == 1:
        return dates[0]
    return None


def _text_contains_explicit_date_hint(text: str) -> bool:
    text_lower = text.lower()
    return bool(
        re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
        or _FULL_DOT_DATE_RE.search(text)
        or _SHORT_DOT_DATE_RE.search(text)
        or _RUSSIAN_DATE_RE.search(text)
        or _RUSSIAN_WORD_DATE_RE.search(text)
        or any(alias in text_lower for alias in _KPK_DATE_ALIASES)
    )


def _is_early_termination_investigation(text: str) -> bool:
    return bool(
        re.search(
            r"(досрочно\s+закрыл|закрыт\w*\s+досрочно|не\s+вернул\w*\s+лимит|"
            r"лимит\s+вс[её]\s+ещ[её]\s+занят)",
            text.lower(),
        )
    )


def _extract_iso_date_range(text: str):
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    unique_dates = []
    for date_str in dates:
        if date_str not in unique_dates:
            unique_dates.append(date_str)
    if len(unique_dates) >= 2:
        return unique_dates[0], unique_dates[-1]
    return None


# =====================================================================================
# САНИТИЗАЦИЯ CHAT HISTORY
# =====================================================================================

def _sanitize_chat_history_for_parse(messages, max_msgs=None, max_chars_per_msg=None):
    if max_msgs is None:
        max_msgs = settings.max_chat_history_for_parse
    if max_chars_per_msg is None:
        max_chars_per_msg = settings.max_chars_per_history_msg
    sanitized = []
    for msg in messages[-max_msgs:]:
        content = getattr(msg, 'content', str(msg))
        content = re.sub(r'^\|.*$', '', content, flags=re.MULTILINE)
        content = re.sub(r'\n{3,}', '\n\n', content)
        if len(content) > max_chars_per_msg:
            content = content[:max_chars_per_msg] + '...'
        sanitized.append(msg.__class__(content=content))
    return sanitized


# =====================================================================================
# УЗЛЫ ГРАФА
# =====================================================================================

async def kpk_preprocess_input(state: GraphState):
    """КПК: нормализация входного текста (замена русских дат, КПК -> division_cd)."""
    try:
        text = state["input"]

        text = re.sub(
            r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D\u00AD]",
            "-", text,
        )

        # Нормализация названий сегментов в канонический вид (до подстановки division_cd)
        _SEGMENT_ALIASES = {
            "кфи": "CIB", "КФИ": "CIB", "cib": "CIB",
            "ММБ": "MMB","ммб": "MMB", "mmb": "MMB",
            "КСБ": "KSB", "ксб": "KSB", "ksb": "KSB",
            "РГС": "RGS", "ргс": "RGS", "rgs": "RGS",
            "СКМ": "SKM", "скм": "SKM", "skm": "SKM",
            "ДГР": "DGR", "дгр": "DGR", "dgr": "DGR",
        }
        _segment_pattern = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in _SEGMENT_ALIASES) + r")\b",
            re.IGNORECASE,
            )
        text = _segment_pattern.sub(lambda m: _SEGMENT_ALIASES[m.group(1).lower()], text)

        text = re.sub(
            r"\b(?:КПК|подразделени[яе])\s+(\d+)",
            r"division_cd=\1",
            text,
            flags=re.IGNORECASE,
        )
        # Голое «КПК» без числового кода не заменяем на «division_cd»,
        # чтобы LLM не копировала служебное слово в поле division_cd.
        # Сегменты (CIB, MMB и т.д.) уже нормализованы выше, но НЕ подставляются
        # в division_cd — только числовые коды.

        today = _kpk_today()

        def _replace_russian_date(m: re.Match) -> str:
            day = int(m.group(1))
            month = _RUSSIAN_MONTH_MAP[m.group(2).lower()]
            year = int(m.group(3)) if m.group(3) else today.year
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                return m.group(0)

        def _replace_word_date(m: re.Match) -> str:
            day = _RUSSIAN_ORDINAL_DAY_MAP[m.group(1).lower()]
            month = _RUSSIAN_MONTH_MAP[m.group(2).lower()]
            year = int(m.group(3)) if m.group(3) else today.year
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                return m.group(0)

        def _replace_full_dot_date(m: re.Match) -> str:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if year < 100:
                year += 2000
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                return m.group(0)

        text = _FULL_DOT_DATE_RE.sub(_replace_full_dot_date, text)
        text = _RUSSIAN_DATE_RE.sub(_replace_russian_date, text)
        text = _RUSSIAN_WORD_DATE_RE.sub(_replace_word_date, text)

        def _replace_short_dot_date(m: re.Match) -> str:
            day, month = int(m.group(1)), int(m.group(2))
            try:
                return datetime.date(today.year, month, day).isoformat()
            except ValueError:
                return m.group(0)

        text = _SHORT_DOT_DATE_RE.sub(_replace_short_dot_date, text)

        for alias, days_back in _KPK_DATE_ALIASES.items():
            if alias in text.lower():
                resolved = (today - datetime.timedelta(days=days_back)).isoformat()
                text = re.sub(alias, resolved, text, flags=re.IGNORECASE)
                break

        return {"input": text}
    except Exception as e:
        _LOGGER.error(
            "[kpk_preprocess_input] Нормализация дат упала. Входной текст: '%.200s...'. "
            "Скорее всего регулярка _RUSSIAN_DATE_RE или _SHORT_DOT_DATE_RE "
            f"не смогла распарсить дату: {state.get('input', '')}, {e}"
        )
        return {"input": state["input"]}


async def kpk_parse_query(state: GraphState):
    """КПК: парсинг запроса через LLM -> KpkLimitParsedQuery."""
    text_input = state["input"]
    pending_date_choice = state.get("pending_kpk_date_choice") or {}
    if pending_date_choice.get("kind") == "kpk_date_basis":
        choice = _kpk_detect_date_basis_choice(text_input)
        if not choice:
            requested_dt = pending_date_choice.get("requested_dt") or (
                    pending_date_choice.get("parsed") or {}
            ).get("report_dt") or _kpk_today_iso()
            return {
                "destination": "kpk_limits",
                "parsed": pending_date_choice.get("parsed") or {},
                "confidence_score": 7,
                "response_state": "input-required",
                "final_answer": _kpk_date_clarification_message(requested_dt),
                "pending_kpk_date_choice": pending_date_choice,
            }

        data, _, _ = _kpk_apply_morning_dates(
            pending_date_choice.get("parsed") or {},
            text_input,
            explicit_date=True,
            date_basis_choice=choice,
            )
        _LOGGER.info(f"kpk_parse_resolved_pending_date: {choice}, {data}")
        return {
            "destination": "kpk_limits",
            "parsed": data,
            "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
            "pending_kpk_date_choice": None,
            "response_state": "completed",
        }
    
    with_today = {
        "input": text_input,
        "chat_history": _sanitize_chat_history_for_parse(state.get("messages", [])),
        "today": _kpk_today_iso(),
    }

    parsed = await asyncio.to_thread(
        _kpk_safe_structured_invoke,
        _kpk_parse_chain(),
        with_today,
        default={
            "mode": "single",
            "division_cd": None,
            "report_dt": _kpk_today_iso(),
            "report_dt_from": None,
            "delta_amt": None,
            "limit_t": None,
            "limit_prev": None,
            "intent": "other",
            "confidence": 5,
        },
    )

    data = parsed.model_dump()

    # -- Пост-валидация: LLM иногда путает division_cd и delta_amt --

    _div_from_text = re.search(r"division_cd=(\d+)", text_input)
    if _div_from_text:
        expected_div = _div_from_text.group(1)
        if not data.get("division_cd"):
            data["division_cd"] = expected_div
            data["mode"] = "single"
        if data.get("delta_amt") is not None and str(int(data["delta_amt"])) == expected_div:
            _LOGGER.warning(f"[kpk_parse] LLM confused division_cd with delta_amt, fixing")
            data["division_cd"] = expected_div
            data["delta_amt"] = None
            data["mode"] = "single"

    extracted_amt = _extract_amount_from_text(text_input)
    if extracted_amt is not None:
        if not data.get("delta_amt") or data["delta_amt"] == 0:
            data["delta_amt"] = extracted_amt
            _LOGGER.info(f"[kpk_parse] overrode delta_amt from text: {extracted_amt}")
        elif extracted_amt < 0 and data.get("delta_amt", 0) > 0 and abs(data["delta_amt"]) == abs(extracted_amt):
            data["delta_amt"] = extracted_amt
            _LOGGER.info(f"[kpk_parse] fixed delta_amt sign: {extracted_amt}")

    # --- division_cd: допускаем ТОЛЬКО числовой код ---
    # Любые нечисловые значения (названия, сегменты, мусор) — обнуляем.
    _div_raw = data.get("division_cd")
    if _div_raw is not None:
        _div_stripped = str(_div_raw).strip()
        _GARBAGE_LITERALS = {"division_cd", "null", "none", "n/a", "нет", ""}
        _is_numeric = bool(re.fullmatch(r"\d+", _div_stripped))
        if _div_stripped.lower() in _GARBAGE_LITERALS or not _is_numeric:
            if _div_stripped and _div_stripped.lower() not in _GARBAGE_LITERALS:
                _LOGGER.info(
                    "[kpk_parse] division_cd='%s' не является числовым кодом КПК. "
                    "Вероятно LLM записал название подразделения или сегмент. Обнуляем.",
                    _div_stripped
                )
            data["division_cd"] = None

    # --- Fallback: если LLM не извлёк division_cd, ищем числовой код в тексте ---
    # Сначала собираем все числа, входящие в даты (ISO и dd.mm), чтобы их исключить
    _date_nums = set()
    for _dm in re.finditer(r"\d{4}-\d{2}-\d{2}", text_input):
        _date_nums.update(_dm.group().replace("-", " ").split())
    for _dm in re.finditer(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", text_input):
        _date_nums.update(re.findall(r"\d+", _dm.group()))

    if not data.get("division_cd"):
        for _m in re.finditer(r"\b(\d{4,10})\b", text_input):
            _num = _m.group(1)
            _start, _end = _m.span(1)
            _prev_char = text_input[_start - 1] if _start > 0 else ""
            _next_char = text_input[_end] if _end < len(text_input) else ""
            if _prev_char in {".", ","} or _next_char in {".", ","}:
                continue
            # Пропускаем если это часть даты
            if _num in _date_nums:
                continue
            # Пропускаем если это delta_amt
            if data.get("delta_amt") and str(int(abs(data["delta_amt"]))) == _num:
                continue
            # Пропускаем если это inn_num
            if data.get("inn_num") and data["inn_num"] == _num:
                continue
            data["division_cd"] = _num
            data["mode"] = "single"
            _LOGGER.info(f"[kpk_parse] Fallback: извлечён КПК-код из текста: {_num}")
            break

    # --- mode=all допускается ТОЛЬКО при ЯВНОМ запросе списка/отчёта по ВСЕМ КПК ---
    # Триггеры должны содержать "все/всех/всем КПК" или явный запрос перечня:
    # "какие/каких/у каких КПК...", "какие подразделения..." и т.п.
    # НЕ триггерят: "отрицательный лимит", "отчет о движении лимита",
    # "перераспределение лимита" — это всё single (по конкретному КПК).
    if data.get("mode") == "all":
        _text_lower = text_input.lower()
        _all_mode_triggers = [
            r"вс[еёх]+\s+кпк",                          # "все КПК", "всех КПК"
            r"по\s+всем\s+кпк",                          # "по всем КПК"
            r"по\s+всем\s+подразделени",                  # "по всем подразделениям"
            r"(?:отч[её]т|сводк\w*|аналитик\w*)\s+(?:по\s+)?вс[еёх]+",  # "отчет по всем", "сводка всех"
            r"(?:отч[её]т|сводк\w*)\s+(?:по\s+)?отрицательн\w+\s+кпк",  # "отчет по отрицательным КПК"
            r"как[ие]+\s+кпк\s+(?:с\s+)?отрицательн",     # "какие КПК с отрицательным"
            r"каких\s+кпк\s+(?:с\s+)?отрицательн",        # "каких КПК с отрицательным"
            r"у\s+каких\s+кпк\s+отрицательн",             # "у каких КПК отрицательный..."
            r"каки[еёх]\s+подразделени\w*\s+(?:с\s+)?отрицательн",  # "какие подразделения с отрицательным"
            r"у\s+каких\s+подразделени\w*\s+отрицательн", # "у каких подразделений отрицательный..."
            r"покажи\s+вс[еёх]+\s+(?:подразделени|кпк)",   # "покажи все подразделения/КПК"
            r"вс[еёх]+\s+подразделени\w*\s+с\s+отрицательн", # "все подразделения с отрицательным"
            r"список\s+(?:кпк|подразделени)\w*\s+(?:с\s+)?отрицательн", # "список КПК с отрицательным"
        ]
        _is_all_mode_request = any(re.search(p, _text_lower) for p in _all_mode_triggers)
        if not _is_all_mode_request:
            _LOGGER.info(
                "[kpk_parse] LLM выбрал mode=all, но в тексте нет явных ключевых слов "
                "отчёта по ВСЕМ КПК (ожидаются: 'все КПК', 'по всем подразделениям', "
                "'отчет по всем' и т.п.). Переключаем на mode=single. Текст: '%.200s...'",
                text_input
            )
            data["mode"] = "single"

    if data.get("mode") == "all":
        data["division_cd"] = None

    inferred_intent = _infer_intent_from_text(text_input, data)
    if inferred_intent and inferred_intent != data.get("intent"):
        _LOGGER.info(f"[kpk_parse] overriding intent based on text heuristics {data.get('intent')}, {inferred_intent}")
        data["intent"] = inferred_intent

    if data.get("intent") == "investigate_deal" and data.get("inn_num"):
        inferred_date = _extract_investigate_value_date(text_input)
        if inferred_date and data.get("report_dt") != inferred_date:
            _LOGGER.info(f"[kpk_parse] overriding report_dt for investigate_deal {data.get('report_dt')}, {inferred_date}")
            data["report_dt"] = inferred_date
        elif not _text_contains_explicit_date_hint(text_input):
            today_dt = _kpk_today_iso()
            if data.get("report_dt") != today_dt:
                _LOGGER.info(
                    "[kpk_parse] overriding hallucinated report_dt for investigate_deal from %s to %s "
                    "because the user did not specify a date",
                    data.get("report_dt"),
                    today_dt,
                )
                data["report_dt"] = today_dt

    if data.get("intent") == "client_history":
        inferred_range = _extract_iso_date_range(text_input)
        if inferred_range:
            range_from, range_to = inferred_range
            if (data.get("report_dt_from"), data.get("report_dt")) != (range_from, range_to):
                _LOGGER.info(
                    "[kpk_parse] overriding client_history date range from (%s, %s) to (%s, %s)",
                    data.get("report_dt_from"),
                    data.get("report_dt"),
                    range_from,
                    range_to,
                )
            data["report_dt_from"] = range_from
            data["report_dt"] = range_to
            data["lookback_days"] = None

    if data.get("report_dt_from") and data.get("report_dt") and data["report_dt_from"] > data["report_dt"]:
        data["report_dt_from"], data["report_dt"] = data["report_dt"], data["report_dt_from"]

    # --- Если single, но нет числового кода КПК — запрос уточнения ---
    _div = data.get("division_cd")
    _div_is_valid = _div is not None and bool(re.fullmatch(r"\d+", str(_div).strip()))

    if data.get("intent") == "find_deal" and not _div_is_valid:
        return {
            "destination": "kpk_limits",
            "parsed": data,
            "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
            "response_state": "input-required",
            "final_answer": (
                "Для поиска сделки по влиянию на лимит нужен числовой код КПК "
                "(например, 10253085)."
            ),
        }

    if data.get("mode") == "single" and not _div_is_valid:
        division_name = _div if _div else None

        if division_name:
            msg = (
                f"Вы указали подразделение «{division_name}», но для анализа "
                f"требуется числовой код КПК (например, 10253085). "
                f"Пожалуйста, уточните код."
            )
        else:
            msg = (
                "Для ответа на Ваш вопрос по изменению лимита "
                "требуется указать числовой код КПК (например, 10253085)."
            )

        _LOGGER.info(f"[kpk_parse] input-required: division_cd={_div}, returning clarification")
        return {
            "destination": "kpk_limits",
            "parsed": data,
            "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
            "response_state": "input-required",
            "final_answer": msg,
        }

    data, pending_date_choice, date_clarification = _kpk_apply_morning_dates(
        data,
        text_input,
        date_basis_choice=_kpk_detect_date_basis_choice(text_input),
    )
    if pending_date_choice:
        if pending_date_choice.get("kind") == "kpk_data_unavailable":
            _LOGGER.info(
                "[kpk_parse] data unavailable: requested_dt=%s include_day not available yet",
                data.get("requested_dt"),
            )
            return {
                "destination": "kpk_limits",
                "parsed": data,
                "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
                "response_state": "input-required",
                "final_answer": date_clarification,
            }
        _LOGGER.info(
            "[kpk_parse] input-required: requested_dt=%s needs morning/include_day choice",
            pending_date_choice.get("requested_dt"),
        )
        return {
            "destination": "kpk_limits",
            "parsed": data,
            "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
            "response_state": "input-required",
            "final_answer": date_clarification,
            "pending_kpk_date_choice": pending_date_choice,
        }

    _LOGGER.info(f"[kpk_parse] parsed: {data}")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"[kpk_parse] parsed: {data}"}})

    return {
        "destination": "kpk_limits",
        "parsed": data,
        "confidence_score": _kpk_safe_int(data.get("confidence", 7)),
        "pending_kpk_date_choice": None,
        "response_state": "completed",
    }


async def kpk_call_tool(state: GraphState, config=None):
    configurable = (config or {}).get("configurable") or {}
    auth_header = configurable.get("auth_header")
    trace_id = configurable.get("trace_id")
    """КПК: вызов инструмента анализа лимитов."""
    parsed = state.get("parsed") or {}
    mode = parsed.get("mode", "single")
    intent = parsed.get("intent", "other")

    if intent == "find_deal" and parsed.get("delta_amt") is not None and parsed["delta_amt"] != 0:
        mode = "find_deal"

    if intent == "investigate_deal" and parsed.get("inn_num"):
        mode = "investigate_deal"
        if not parsed.get("lookback_days"):
            parsed["lookback_days"] = 0 if _text_contains_explicit_date_hint(state.get("input", "")) else 7
        if (
                _is_early_termination_investigation(state.get("input", ""))
                and not _text_contains_explicit_date_hint(state.get("input", ""))
        ):
            expanded_lookback = max(_kpk_safe_int(parsed.get("lookback_days"), 7), 14)
            if parsed.get("lookback_days") != expanded_lookback:
                _LOGGER.info(
                    "[kpk_tool] expanding investigate_deal lookback_days from %s to %s "
                    "for early-termination query without explicit date",
                    parsed.get("lookback_days"),
                    expanded_lookback,
                )
            parsed["lookback_days"] = expanded_lookback

    if intent == "client_history" and parsed.get("inn_num") and parsed.get("division_cd"):
        mode = "client_history"
        if not parsed.get("report_dt_from") and not parsed.get("lookback_days"):
            parsed["lookback_days"] = 7

    tool_report_dt = parsed.get("tool_report_dt") or parsed.get("report_dt")
    tool_report_dt_from = parsed.get("tool_report_dt_from") or parsed.get("report_dt_from")
    if mode in {"investigate_deal", "client_history"}:
        tool_report_dt = parsed.get("report_dt")
        tool_report_dt_from = parsed.get("report_dt_from")

    _LOGGER.info(f"[kpk_tool] mode={mode}")
    audit.info({"code": "C3_SERVICE_ACTION", "params": {"object_name": f"[kpk_tool] mode={mode}"}})

    try:
        res = await asyncio.to_thread(
            execute_kpk_limits_tool,
            mode=mode,
            report_dt=tool_report_dt,
            report_dt_from=tool_report_dt_from,
            as_of_dt=_kpk_today_iso() if mode == "investigate_deal" else None,
            division_cd=parsed.get("division_cd"),
            delta_amt=parsed.get("delta_amt"),
            inn_num=parsed.get("inn_num"),
            include_details=(intent in {"redistribution", "factors"}),
            lookback_days=parsed.get("lookback_days"),
            auth_header=auth_header,
            trace_id=trace_id,
        )
    except Exception as e:
        _LOGGER.error(
            "[kpk_call_tool] Вызов execute_kpk_limits_tool упал. mode=%s, division_cd=%s, "
            "report_dt=%s, intent=%s. Вероятные причины: API недоступен, "
            "невалидные параметры запроса, или таймаут сетевого вызова: %s",
            mode, parsed.get("division_cd"), tool_report_dt, intent, e
        )
        return {
            "tool_payload": {},
            "answer": ERROR_TEXT,
            "confidence_score": 5,
        }

    if not isinstance(res, dict) or res.get("status") != "success":
        err = (res or {}).get("error") or ERROR_TEXT
        _LOGGER.warning(f"[kpk_tool] error: {err}")
        audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"[kpk_tool] error: {err}"}})
        return {
            "tool_payload": {},
            "answer": ERROR_TEXT,
            "confidence_score": 5,
        }

    # Проверка: если данных за дату нет (все нули) — переспрашиваем
    _data = res.get("data") or {}
    # Use backend's actual snapshot date as the authoritative display date
    # (backend may have rolled it forward past a weekend or holiday).
    if _data.get("report_dt"):
        _data["as_of_morning_dt"] = _data["report_dt"]
    for _key in (
            "requested_dt",
            "requested_dt_from",
            "tool_report_dt",
            "tool_report_dt_from",
            "date_basis",
    ):
        if parsed.get(_key):
            _data[_key] = parsed.get(_key)

    if (
            _data.get("mode") == "single"
            and _data.get("limit_amt") == 0
            and _data.get("delta_limit_amt") == 0
            and _data.get("prev_limit_amt") == 0
    ):
        _report_dt = _data.get("report_dt", parsed.get("report_dt", ""))
        _as_of_dt = _data.get("as_morning_dt")
        _LOGGER.info(f"[kpk_tool] Нет данных за дату {_report_dt}, переспрашиваем")
        if _as_of_dt:
            answer = (
                f"На УТРО {_as_of_dt} нет данных по лимиту КПК "
                f"(дата данных витрины: {_report_dt}). "
                "Пожалуйста, укажите корректную рабочую дату."
            )
        else:
            answer = (
                f"За дату {_report_dt} нет данных по лимиту КПК. "
                "Пожалуйста, укажите корректную рабочую дату."
            )
            
        return {
            "tool_payload": {},
            "answer": answer,
            "confidence_score": 5,
            "response_state": "input-required",
        }

    return {"tool_payload": _data, "confidence_score": 9}


async def kpk_generate_answer(state: GraphState):
    """КПК: генерация текстового ответа."""
    tool_payload = state.get("tool_payload") or {}
    mode = tool_payload.get("mode")
    division_cd = tool_payload.get("division_cd", "Н/Д")
    report_dt = tool_payload.get("report_dt", "Н/Д")
    morning_label = _kpk_morning_date_label(tool_payload)

    try:
        if mode == "all":
            return {"answer": _sanitize_code_generated_numbers(_kpk_build_all_intro(tool_payload))}

        if mode == "single":
            dt = tool_payload.get("as_of_morning_dt") or report_dt
            input_formatted = _kpk_build_single_summary_input(tool_payload)

            text = await asyncio.to_thread(
                _kpk_safe_text_invoke,
                _kpk_answer_chain(),
                {"input_formatted": input_formatted},
                default_text=f"Анализ лимита на утро {dt} завершён.\n\n<TABLES_GO_HERE>",
            )
            if not re.search(r"возврат в лимит|изъятие из лимита", text, re.IGNORECASE):
                text = _insert_summary_before_tables(text, _kpk_disappeared_summary_sentence(tool_payload))
            return {"answer": text}

        if mode == "find_deal":
            deals = tool_payload.get("deals", [])
            if not deals:
                return {"answer": f"На {morning_label} сделка с указанным влиянием на лимит не найдена."}
            return {
                "answer": _sanitize_code_generated_numbers(
                    f"Найдено **{len(deals)}** сделок с похожим влиянием на лимит.\n\n<TABLES_GO_HERE>"
                )
            }

        if mode == "investigate_deal":
            verdict = tool_payload.get("verdict", "not_found")
            verdict_texts = {
                "stopped_by_status": "<TABLES_GO_HERE>",
                "found_in_deals": _investigate_found_in_deals_intro(tool_payload),
                "pending_impact": "Сделка зарегистрирована в системе, но **ещё не начала влиять** на лимит.",
                "not_found": "Сделка с указанными параметрами **не найдена** ни в таблице сделок, ни в логах расчётов.",
            }
            text = verdict_texts.get(verdict, verdict_texts["not_found"])
            if "<TABLES_GO_HERE>" in text:
                return {"answer": text}
            return {"answer": f"{text}\n\n<TABLES_GO_HERE>"}

        if mode == "client_history":
            deals = tool_payload.get("deals", [])
            inn = tool_payload.get("inn_num", "Н/Д")
            div = division_cd
            client_name = str(tool_payload.get("client_name") or "").strip()
            days = tool_payload.get("lookback_days", 7)
            date_from = tool_payload.get("date_from")
            date_to = tool_payload.get("date_to") or tool_payload.get("date")
            client_label = (
                f"клиента **{client_name}** (ИНН **{inn}**)"
                if client_name
                else f"клиента с ИНН **{inn}**"
            )
            if not deals:
                if date_from and date_to:
                    return {
                        "answer": _sanitize_code_generated_numbers(
                            f"За период с **{date_from}** по **{date_to}** сделок {client_label} по КПК **{div}** не найдено."
                        )
                    }
                return {
                    "answer": _sanitize_code_generated_numbers(
                        f"За последние **{days}** дней сделок {client_label} по КПК **{div}** не найдено."
                    )
                }
            if date_from and date_to:
                return {
                    "answer": _sanitize_code_generated_numbers(
                        f"Найдено **{len(deals)}** сделок {client_label} по КПК **{div}** за период с **{date_from}** по **{date_to}**.\n\n<TABLES_GO_HERE>"
                    )
                }
            return {
                "answer": _sanitize_code_generated_numbers(
                    f"На {morning_label} найдено **{len(deals)}** сделок {client_label} по КПК **{div}** за последние **{days}** дней.\n\n<TABLES_GO_HERE>"
                )
            }

        return {"answer": "Анализ завершен. Детали приведены ниже:\n\n<TABLES_GO_HERE>"}

    except Exception as e:
        _LOGGER.error(
            "[kpk_generate_answer] Генерация текста ответа упала. mode=%s, division_cd=%s, "
            "report_dt=%s. Вероятные причины: tool_payload пришёл пустой или с неожиданной "
            "структурой (ожидались ключи 'deals_analysis', 'discounting_analysis' и т.д.), "
            "либо LLM-вызов _kpk_safe_text_invoke не смог сформировать текст: %s",
            mode, division_cd, report_dt, e
        )
        return {"answer": ERROR_TEXT}


async def kpk_finalize(state: GraphState):
    """КПК: инъекция markdown-таблиц в ответ."""
    # Если ответ уже сформирован на этапе парсинга (запрос уточнения)
    if state.get("response_state") == "input-required":
        return {
            "final_answer": state.get("final_answer", ""),
            "response_state": "input-required",
        }

    if state.get("answer") == ERROR_TEXT:
        return {"final_answer": ERROR_TEXT}

    final = (state.get("answer") or "").strip()
    tool_payload = state.get("tool_payload") or {}
    mode = tool_payload.get("mode")

    try:
        md_tables = ERROR_TEXT
        if mode == "single":
            md_tables = generate_kpk_report_markdown(tool_payload)
        elif mode == "all":
            md_tables = generate_all_kpk_report_markdown(tool_payload)
        elif mode == "find_deal":
            md_tables = generate_find_deal_markdown(tool_payload)
        elif mode == "investigate_deal":
            md_tables = generate_investigate_deal_markdown(tool_payload)
        elif mode == "client_history":
            md_tables = generate_client_history_markdown(tool_payload)
    except Exception as e:
        _LOGGER.error(
            "[kpk_finalize] Форматирование markdown-таблиц упало. mode=%s, division_cd=%s, "
            "ключи tool_payload=%s. Скорее всего pandas получил DataFrame с неожиданным "
            "набором колонок или пустые данные в data.get('deals_analysis')/"
            "data.get('redistribution_by_author'): %s",
            mode, tool_payload.get("division_cd"),
            list(tool_payload.keys())[:20], e
        )
        return {"final_answer": ERROR_TEXT}

    if md_tables == ERROR_TEXT:
        return {"final_answer": ERROR_TEXT}

    generated_report = _build_incorrect_deals_report_meta(state, tool_payload)
    placeholder_re = re.compile(r"<?\bTABLES[\s_-]*GO[\s_-]*HERE\b>?", re.IGNORECASE)
    replacement = f"\n\n{md_tables}\n\n"
    if generated_report:
        replacement = f"\n\n{_append_generated_report_notice('<TABLES_GO_HERE>').replace('<TABLES_GO_HERE>', '').strip()}\n\n{md_tables}\n\n"

    if placeholder_re.search(final):
        final = placeholder_re.sub(replacement, final)
    else:
        final = final + replacement

    final = re.sub(r"\n{3,}", "\n\n", final)
    final = _sanitize_code_generated_numbers(final)

    result = {"final_answer": final}
    if generated_report:
        result["generated_report"] = generated_report
    return result
