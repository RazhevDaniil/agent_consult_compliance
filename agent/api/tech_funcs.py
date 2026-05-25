import re
import time
import hashlib
import logging
from dataclasses import dataclass
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import List, Tuple, Dict, Any
from langchain_core.documents import Document

from .config import settings


_LOGGER = logging.getLogger(__name__)

_COMP_ALIASES = {
    "pricing": {
        "ets": [r"\bетс\b", r"\bets\b", r"един\w*\s+трансферт\w*", r"трансферт\w*\s+ставк"],
        "ets_rub": [r"\bетс\b.*\brub\b", r"\bets\b.*\brub\b", r"етс\s+в\s+руб"],
        "crl": [r"\bсрл\b", r"\bcrl\b", r"регуляторн\w*\s+ликвидн"],
        "nor": [r"\bнор\b", r"\bnor\b", r"норматив\w*\s+обязательн\w*\s+резерв"],
        "for_rate": [r"\bфор\b", r"\bfor\b", r"обязательн\w*\s+резерв", r"фонд\w*\s+обязательн\w*\s+резерв"],
        "funding_rate": [r"\bсф\b", r"\bsf\b", r"стоимост[ьи]?\s+фондирован", r"ставк\w*\s+фондирован", r"\bfunding\b"],
        "eva_rate": [r"\beva\b", r"\bева\b", r"фактическ\w*\s+eva", r"фактическ\w*\s+ева"],
        "break_even_rate": [r"безубыт", r"без\s+убыт", r"\bбезуб\b", r"\bbe\b"],
        "non_utilizing_rate": [r"не ?утилиз", r"неутил", r"\bнул\b", r"\bnul\b", r"non[ _-]?util"],
        "marginal_income": [r"маржин\w*\s+доход", r"\bмаржа\b", r"маржиналк"],
        "target_marginal_income": [r"целев\w*\s+маржин\w*\s+доход", r"target\s+marginal"],
    },
    "limits": {
        "Влияние на лимит при котировании": [r"при\s+котир", r"котировоч", r"\bquote\b"],
        "Влияние на лимит действующей сделки": [r"действующ\w*\s+сделк", r"текущ\w*\s+сделк", r"\bactive\b"],
        "Влияние на лимит КПК действующей сделки": [r"лимит\s+кпк", r"\bкпк\b"],
        "Влияние на лимит ЦА действующей сделки": [r"лимит\s+ца", r"\bца\b", r"центральн\w*\s+аппарат"],
        "График начисления лимита по сделке": [r"график", r"начислен\w*\s+лимит", r"граф\w*\s+лимит"],
    },
}

_COMP_CANONICAL_ALIASES = {
    "pricing": {
        "ets": ["етс", "ets", "единая трансфертная ставка", "трансфертная ставка"],
        "ets_rub": ["етс в rub", "етс rub", "ets rub", "етс в руб"],
        "crl": ["срл", "crl", "стоимость регуляторной ликвидности", "регуляторная ликвидность"],
        "nor": ["нор", "nor", "норматив обязательного резервирования"],
        "for_rate": ["фор", "for", "фонд обязательных резервов", "обязательные резервы", "обязательное резервирование"],
        "funding_rate": ["сф", "sf", "стоимость фондирования", "ставка фондирования", "фондирование"],
        "eva_rate": ["eva", "ева", "фактическая eva", "фактическая ева"],
        "break_even_rate": ["ставка безубыточности", "безубыточная ставка", "безубыточность", "безуб", "be"],
        "non_utilizing_rate": ["ставка не утилизирующая лимит", "неутилизирующая ставка", "не утилизирующая ставка", "неутилиз", "нул", "nul"],
        "marginal_income": ["маржинальный доход", "маржиналка", "маржа"],
        "target_marginal_income": ["целевой маржинальный доход", "целевая маржинальность", "target marginal income"],
    },
    "limits": {
        "Влияние на лимит при котировании": ["влияние на лимит при котировании", "лимит при котировании", "влияние при котировании", "котировочный лимит"],
        "Влияние на лимит действующей сделки": ["влияние на лимит действующей сделки", "действующая сделка", "текущая сделка", "активная сделка"],
        "Влияние на лимит КПК действующей сделки": ["влияние на лимит кпк", "лимит кпк", "кпк"],
        "Влияние на лимит ЦА действующей сделки": ["влияние на лимит ца", "лимит ца", "ца", "центральный аппарат"],
        "График начисления лимита по сделке": ["график дисконтирования", "график начисления лимита", "график лимита", "график по лимиту", "график"],
    },
}

_COMP_ALIASES_INVERSED = {
    'ets': "единая трансфертная ставка",
    'ets_rub': "ЕТС в RUB",
    'crl': "СРЛ",
    'nor': "НОР",
    'for_rate': "ФОР",
    'funding_rate': "Стоимость фондирования",
    'eva_rate': "Фактическая EVA по сделке",
    'break_even_rate': "Ставка безубыточности",
    'non_utilizing_rate': "Ставка не утилизирующая лимит",
    'marginal_income': "Маржинальный доход по сделке",
    'target_marginal_income': "Целевой маржинальный доход по сделке",
    "Влияние на лимит при котировании": "Влияние на лимит при котировании",
    "Влияние на лимит действующей сделки": "Влияние на лимит действующей сделки",
    "Влияние на лимит КПК действующей сделки": "Влияние на лимит КПК действующей сделки",
    "Влияние на лимит ЦА действующей сделки": "Влияние на лимит ЦА действующей сделки",
    "График начисления лимита по сделке": "График начисления лимита по сделке"   
}

_CYR_TO_LAT_MAP = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y",
    "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": "",
})

# любые синонимы ID сделки/расчёта
_ANY_ID_PAT = re.compile(
    r"""
    \b(?:                                   # начало «метки»
        deal[_\s-]?id|
        id[_\s-]?deal|
        calc[_\s-]?id|
        id[_\s-]?calc|
        id\s*расчет[ау]|
        id\s*сделк[аи]|
        айд[ий]шник|
        id
    )\b
    [\s:=#]*                                # разделитель
    (?P<val>[A-Za-z0-9_-]{6,}|[0-9]{3,})    # значение (допускаем цифры/буквы/подчёрк/дефис)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ID_PAT = re.compile(
    r"(?:deal[_\s-]?id|calc[_\s-]?id|id\s+(?:calc|deal)|id\s+сделки|id\s+расч[её]та|айди\w*|^id)\s*[:#=]?\s*(\d{3,})",
    re.IGNORECASE
)

_THIS_DEAL_PAT = re.compile(
    r"(по\s+эт[ао]й?\s+же\s+сд[её]лк\w*|"
    r"эта\s+же\s+сделк\w*|"
    r"той\s+же\s+сделк\w*|"
    r"предыдущ\w+\s+сделк\w*|"
    r"прошл\w+\s+сделк\w*|"
    r"эту\s+же\s+сделк\w*|"
    r"этой\s+сделк\w*|"
    r"сделке)",
    re.IGNORECASE
)

_NEW_DEAL_RESET_PAT = re.compile(
    r"\b(нов(ая|ую)|друг(ая|ую)|смен(им|ить)|переключ(им|ить)|по\s+другой)\b.*\bсделк", re.I
)


@dataclass
class ContextResolution:
    mode: str  # "CONTINUE", "NEW_WITH_ID", "NEW_WITH_PARAMS", "INSUFFICIENT_PARAMS"
    deal_id: str | None
    explicit_params: dict  # нормализованные параметры для селектора/поиска
    reason: str


@dataclass
class RequestedComponentsResolution:
    requested: List[str]
    confidence: float
    matched_by: str
    needs_clarification: bool = False
    clarification_text: str | None = None

def _normalize_selector_like(sel) -> dict:
    return {
        "deal_id": getattr(sel, "deal_id", None),
        "inn": getattr(sel, "inn", None),
        "product": getattr(sel, "product", None),
        "currency": getattr(sel, "currency", None),
        "amount": getattr(sel, "amount", None),
        "deal_dt": getattr(sel, "deal_dt", None),
        "maturity_dt": getattr(sel, "maturity_dt", None),
        "term": getattr(sel, "term", None),
        "interest_rate": getattr(sel, "interest_rate", None),
    }

def resolve_context(user_input: str, sel, state) -> ContextResolution:
    """Строго следует правилам из постановки. Читает last_selector из
    state, который чекпоинтер вытаскивает из предыдущего хода (Шаг 2.1)."""
    last_sel = (state.get("last_selector") or {}) if state else {}
    cur = _normalize_selector_like(sel)

    cur_id = cur.get("deal_id")
    last_id = (last_sel or {}).get("deal_id")

    # 1) если указан deal_id в запросе
    if cur_id:
        if last_id and str(cur_id) == str(last_id):
            # текущий контекст: продолжаем, используем сохранённые данные/расчёты
            return ContextResolution(
                mode="CONTINUE",
                deal_id=str(cur_id),
                explicit_params={},  # не нужно
                reason="same_deal_id_as_last"
            )
        else:
            # новый контекст: новый селектор от id
            return ContextResolution(
                mode="NEW_WITH_ID",
                deal_id=str(cur_id),
                explicit_params={"deal_id": str(cur_id)},
                reason="new_deal_id"
            )

    # 2) если deal_id НЕТ — проверяем параметры
    base = {
        "inn": cur.get("inn"),
        "product": cur.get("product"),
        "currency": cur.get("currency"),
        "amount": cur.get("amount"),
        "deal_dt": cur.get("deal_dt"),
        "interest_rate": cur.get("interest_rate"),
    }
    has_any_param = any(v not in (None, "", []) for v in base.values())

    if not has_any_param:
        # параметров нет: продолжаем текущий контекст (если есть)
        if last_id:
            return ContextResolution(
                mode="CONTINUE",
                deal_id=str(last_id),
                explicit_params={},  # используем снапшот
                reason="no_params_continue_last"
            )
        # нет и последней сделки → недостаточно данных
        return ContextResolution(
            mode="INSUFFICIENT_PARAMS",
            deal_id=None,
            explicit_params={},
            reason="no_params_and_no_last"
        )

    # параметры есть — проверяем достаточность (все 6 базовых)
    missing = [k for k, v in base.items() if v in (None, "", [])]
    if missing:
        return ContextResolution(
            mode="INSUFFICIENT_PARAMS",
            deal_id=None,
            explicit_params={"missing": missing},
            reason="partial_params"
        )
    else:
        # достаточно параметров → НОВЫЙ контекст
        return ContextResolution(
            mode="NEW_WITH_PARAMS",
            deal_id=None,
            explicit_params={**base, **({
                "maturity_dt": cur.get("maturity_dt"),
                "term": cur.get("term")
            })},
            reason="full_params_no_id"
        )


def _format_limit_schedule(label: str, val: Any) -> str:
    """
    Безопасное форматирование графика лимитов.
    Ожидает val: {'YYYY-MM-DD': {'metric_name': 123.45, ...}, ...}
    """
    try:
        # Базовая проверка
        if not val or not isinstance(val, dict):
            return f"- **{label}:** n/a (нет данных)"

        # Сортируем даты
        dates = sorted(val.keys())
        if not dates:
            return f"- **{label}:** n/a (пустой список дат)"

        # Берем первую запись для определения колонок
        first_valid_row = None
        for d in dates:
            if isinstance(val[d], dict):
                first_valid_row = val[d]
                break

        if not first_valid_row:
            return f"- **{label}:** n/a (неверный формат данных)"

        # Определяем заголовки колонок
        columns = sorted(list(first_valid_row.keys()))

        # Формируем Markdown таблицу
        # Заголовок
        lines = [f"\n**{label}:**\n"]
        header = f"| Дата | {' | '.join(columns)} |"
        lines.append(header)

        # Разделитель
        sep = f"|---|{'---|' * len(columns)}"
        lines.append(sep)

        # Тело таблицы
        # for d in dates:
        def _render_row(d: str) -> str:
            row = val[d]
            # Если вдруг для какой-то даты пришла ошибка (строка) или None - обрабатываем безопасно
            if not isinstance(row, dict):
                # Можно вывести пустую строку или пропустить
                row_vals = ["error"] * len(columns)
            else:
                row_vals = []
                for col in columns:
                    raw_val = row.get(col)
                    row_vals.append(_fmt6(raw_val))
            return f"| {d} | {' | '.join(row_vals)} |"

            # lines.append(f"| {d} | {' | '.join(row_vals)} |")
        if len(dates) > 4:
            for d in dates[:2]:
                lines.append(_render_row(d))
            lines.append(f"| ... | {' | '.join(['...'] * len(columns))} |")
            for d in dates[-2:]:
                lines.append(_render_row(d))
            lines.append(f"\n_Показаны первые 2 и последние 2 даты из {len(dates)}._")
        else:
            for d in dates:
                lines.append(_render_row(d))

        return "\n".join(lines)

    except Exception as e:
        # При возникновении ошибки лучше вернуть хотя бы текст ошибки для отладки, а не просто n/a
        return f"- **{label}:** Ошибка отображения ({str(e)})"



def _normalize_context_from_inputs(inp: Dict[str, Any]) -> Dict[str, Any]:
    d = inp or {}
    return {
        "deal_id": d.get("pass_through_calc_id")
                  or d.get("internal_order_cd")
                  or d.get("deal_id")
                  or d.get("calc_id"),
        "inn": d.get("inn")
               or d.get("inn_num"),
        "product": d.get("product")
                   or d.get("product_cd"),
        "ccy": d.get("ccy")
                or d.get("currency")
                or d.get("ccy_cd"),
        "amount": d.get("amount")
                  or d.get("deal_amt"),
        "deal_dt": d.get("deal_dt")
                   or d.get("date")
                   or d.get("start_dt")
                   or d.get("value_dt"),
        "maturity_dt": d.get("maturity_dt") or d.get("end_dt"),
        "term": d.get("term"),
        "interest_rate": d.get("interest_rate"),
    }


def compute_deal_signature(ctx: Dict[str, Any]) -> str:
    """Сигнатура сделки: id либо хэш 6 базовых полей."""
    if not ctx:
        return ""
    if ctx.get("deal_id"):
        return f"id:{ctx['deal_id']}"
    keys = ["inn","product","ccy","amount","deal_dt","interest_rate"]
    raw = "|".join(str(ctx.get(k) or "") for k in keys)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _context_ribbon(*, deal_id=None, tool=None, inputs=None, continued=False) -> str:
    """
    Короткая «ленточка контекста», чтобы пользователю было очевидно:
    с какой сделкой мы сейчас работаем.
    """
    bits = []
    if deal_id:
        bits.append(f"id={deal_id}")
    inp = inputs or {}
    # аккуратно подхватим самые полезные поля, если они есть
    prod = inp.get("product")
    ccy  = inp.get("ccy") or inp.get("currency")
    amt  = inp.get("amount")
    dt   = inp.get("deal_dt") or inp.get("value_dt")
    if prod: bits.append(f"продукт={prod}")
    if ccy:  bits.append(f"валюта={ccy}")
    if amt:  bits.append(f"сумма={amt}")
    if dt:   bits.append(f"дата={dt}")
    if tool: bits.append(f"инструмент={tool}")

    tail = " — эта же сделка" if continued else ""
    return f"(контекст: " + " · ".join(str(b) for b in bits) + f"){tail}"


def _deal_id_from_state(state) -> str | None:
    """
    Достаёт deal_id из последнего успешного расчётного payload'а
    или, на крайний случай, из последнего selector'а. Читает напрямую
    из state.last_tool_payload / state.last_selector (Шаг 2.1).
    """
    if not state:
        return None
    last_payload = state.get("last_tool_payload") or {}
    id_from_inputs = (last_payload.get("inputs_used") or {}).get("deal_id")
    if id_from_inputs:
        return str(id_from_inputs)
    for key in ("deal_id", "calc_id", "id"):
        if last_payload.get(key):
            return str(last_payload[key])

    last_sel = state.get("last_selector") or {}
    for key in ("deal_id", "calc_id", "id"):
        if last_sel.get(key):
            return str(last_sel[key])
    return None


def prefetched_row_from_payload(payload) -> dict | None:
    """
    Pure-функция: тот же снапшот, что строит storage.get_prefetched_row,
    но без обращения к store — работает с самим payload (Шаг 2.1).
    """
    if not payload:
        return None

    inputs_used = payload.get("inputs_used") or {}
    comps = payload.get("components") or {}

    indicators = {
        "ets_rate": comps.get("ets"),
        "ets_rate_rub": comps.get("ets_rub"),
        "crl": comps.get("crl"),
        "nor": comps.get("nor"),
        "option_price_rate": comps.get("option_price") or comps.get("option_price_rate"),
        "indicative_eva_rate": comps.get("indicative_eva"),
        "target_eva_rate": comps.get("target_eva"),
    }

    return {**inputs_used, **indicators}


def _has_new_deal_hints(text: str, sel) -> bool:
    t = (text or "").lower()
    # Явное указание "новая/другая"
    if re.search(r"\b(нов(ая|ую)|другая|иную|сменить|пересчитать для другой)\b", t):
        return True
    # Указательные местоимения — трактуем как "продолжить"
    if re.search(r"\b(эта|этой|по ней|по этой|прошлой|той)\b", t):
        return False
    # Считаем "новой", только если пришли новые базовые поля
    has_any_base = any([sel.deal_id, sel.inn, sel.product, sel.currency, sel.amount, sel.deal_dt])
    # и хотя бы ДВА разных базовых поля (чтобы случайный шум не срабатывал)
    fields = [sel.deal_id, sel.inn, sel.product, sel.currency, sel.amount, sel.deal_dt]
    base_count = sum(1 for v in fields if v not in (None, "", []))
    return base_count >= 2


# def _detect_requested_components(text: str, tool: str, available_keys: list[str]) -> list[str]:
#     t = (text or "").lower()
#     tool_map = _COMP_ALIASES["pricing" if tool=="pricing" else "limits"]

def _normalize_component_text(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9%]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def _latinize_component_text(text: str) -> str:
    return _normalize_component_text(text).translate(_CYR_TO_LAT_MAP)

def _compact_component_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")

def _score_alias_variant(text_norm: str, alias_norm: str) -> float:
    if not text_norm or not alias_norm:
        return 0.0

    text_compact = _compact_component_text(text_norm)
    alias_compact = _compact_component_text(alias_norm)

    if alias_norm in text_norm or (alias_compact and alias_compact in text_compact):
        return 1.0

    tokens = text_norm.split()
    alias_tokens = alias_norm.split()
    candidate_strings = {text_norm, text_compact, *tokens}

    if tokens:
        sizes = {
            max(1, len(alias_tokens) - 1),
            len(alias_tokens),
            min(len(tokens), len(alias_tokens) + 1),
            min(len(tokens), len(alias_tokens) + 2),
        }
        for size in sizes:
            if size <= 0 or size > len(tokens):
                continue
            for idx in range(len(tokens) - size + 1):
                window = " ".join(tokens[idx:idx + size])
                candidate_strings.add(window)
                candidate_strings.add(_compact_component_text(window))

    best = 0.0
    for candidate in candidate_strings:
        cand = candidate.strip()
        if not cand:
            continue
        best = max(
            best,
            SequenceMatcher(None, alias_norm, cand).ratio(),
            SequenceMatcher(None, alias_compact, _compact_component_text(cand)).ratio(),
        )
    return best

def _best_component_alias_score(text: str, aliases: list[str]) -> float:
    text_norm = _normalize_component_text(text)
    text_lat = _latinize_component_text(text)
    best = 0.0
    for alias in aliases:
        alias_norm = _normalize_component_text(alias)
        alias_lat = _latinize_component_text(alias)
        best = max(
            best,
            _score_alias_variant(text_norm, alias_norm),
            _score_alias_variant(text_lat, alias_lat),
        )
    return best

def _default_component_keys(tool: str, available_keys: list[str]) -> list[str]:
    if tool == "limits" and "Влияние на лимит при котировании" in available_keys:
        return ["Влияние на лимит при котировании"]
    if tool == "pricing" and "break_even_rate" in available_keys:
        return ["break_even_rate"]
    return []

def _clarify_requested_component(tool: str, candidates: list[str], available_keys: list[str]) -> str:
    labels = [_COMP_ALIASES_INVERSED.get(k, k) for k in candidates[:4]]
    defaults = [_COMP_ALIASES_INVERSED.get(k, k) for k in _default_component_keys(tool, available_keys)]
    variants = labels or defaults
    if not variants:
        variants = [_COMP_ALIASES_INVERSED.get(k, k) for k in available_keys[:4]]

    joined = ", ".join(dict.fromkeys(variants))
    return (
        "Не до конца понял, какой именно показатель нужно показать по сделке. "
        f"Уточните, пожалуйста, один из вариантов: {joined}."
    )

def resolve_requested_components(text: str, tool: str, available_keys: list[str]) -> RequestedComponentsResolution:
    tool_kind = "pricing" if tool == "pricing" else "limits"
    text_norm = _normalize_component_text(text)
    tool_map = _COMP_ALIASES[tool_kind]
    catalog = _COMP_CANONICAL_ALIASES[tool_kind]

    requested = []
    for key, patterns in tool_map.items():
        if key not in available_keys:  # на всякий
            continue
        # if any(re.search(p, t) for p in patterns):
        if any(re.search(pattern, text_norm) for pattern in patterns):
            requested.append(key)
    # # если явно просили «какой лимит/ставка безубыточности/…», можем подсветить дефолт:
    # if not requested:
    #     if tool == "limits" and "Влияние на лимит при котировании" in available_keys and re.search(r"лимит|котиров", t):
    #         requested = ["Влияние на лимит при котировании"]
    #     elif tool == "pricing" and "break_even_rate" in available_keys and re.search(r"безубыточ", t):
    #         requested = ["break_even_rate"]
    # return requested

    requested = list(dict.fromkeys(requested))
    if requested:
        fuzzy_addons = []
        for key, aliases in catalog.items():
            if key not in available_keys or key in requested:
                continue
            label = _COMP_ALIASES_INVERSED.get(key, key)
            score = _best_component_alias_score(text, list(dict.fromkeys([*aliases, label, key])))
            if score >= 0.88:
                fuzzy_addons.append(key)

        requested = list(dict.fromkeys(requested + fuzzy_addons))
        return RequestedComponentsResolution(
            requested=requested,
            confidence=1.0,
            matched_by="regex+fuzzy" if fuzzy_addons else "regex",
        )

    scored: Dict[str, float] = {}
    for key, aliases in catalog.items():
        if key not in available_keys:
            continue
        label = _COMP_ALIASES_INVERSED.get(key, key)
        scored[key] = _best_component_alias_score(text, list(dict.fromkeys([*aliases, label, key])))


    ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return RequestedComponentsResolution(requested=[], confidence=0.0, matched_by="none")

    top_key, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if top_score >= 0.93:
        winners = [key for key, score in ranked if score >= 0.93]
        return RequestedComponentsResolution(
            requested=list(dict.fromkeys(winners)),
            confidence=top_score,
            matched_by="fuzzy-strong",
        )

    if top_score >= 0.78 and (top_score - second_score) >= 0.07:
        return RequestedComponentsResolution(
            requested=[top_key],
            confidence=top_score,
            matched_by="fuzzy",
        )

    ambiguous = [key for key, score in ranked if score >= 0.64]
    if ambiguous:
        return RequestedComponentsResolution(
            requested=[],
            confidence=top_score,
            matched_by="ambiguous",
            needs_clarification=True,
            clarification_text=_clarify_requested_component(tool_kind, ambiguous, available_keys),
        )

    return RequestedComponentsResolution(requested=[], confidence=top_score, matched_by="default")

def _detect_requested_components(text: str, tool: str, available_keys: list[str]) -> list[str]:
    return resolve_requested_components(text, tool, available_keys).requested


def _extract_any_id(text: str) -> str | None:
    m = _ANY_ID_PAT.search(text)
    if m: return m.group("val")
    m2 = _ID_PAT.search(text)
    return m2.group(1) if m2 else None


def _mentions_this_deal(text: str) -> bool:
    return bool(_THIS_DEAL_PAT.search(text))


@contextmanager
def _trace_node(name: str, **fields):
    """Structured node-trace: start/done with duration_ms, failed on exception.

    Это **структурированный лог-обвес**, а не AEF span. `_trace_node` оставлен
    для out-of-band аудита через structlog: пишет `node_start` / `node_done` /
    `node_failed` с `duration_ms` в общий лог-стрим (Loki / OpenSearch), не
    пересекаясь с трейсами в AEF Manager.

    Используется на узлах с локальной логикой (parse / preprocess / finalize
    / generate / router). На узлах, сводящихся к внешнему HTTP-вызову
    (execute_tool, kpk_tool, deals_report), отдельный `_trace_node` не
    добавляется — нагрузку покрывают ручные `aef_custom_span` в HTTP-клиентах.
    """
    t0 = time.perf_counter()
    _LOGGER.info("node_start")
    try:
        yield
    except Exception as exc:
        _LOGGER.error(f"node_failed, {name}. Exc: {exc}")
        raise
    else:
        _LOGGER.info(f"node_done. {name}")


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return text
    limit = max_chars or settings.context_max_chars
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.2):]
    return f"{head}\n...\n{tail}"


def _format_docs(docs: List[Document]) -> Tuple[str, List[str]]:
    """Собирает человеко-читаемый контекст и список имён файлов."""
    parts = []
    srcs = []
    for doc in docs:
        fname = doc.metadata.get("filename", "N/A")
        srcs.append(fname)
        parts.append(f"Имя файла: {fname}\nСодержимое: {doc.page_content}")
    return _truncate("\n\n".join(parts), settings.context_max_chars), sorted(set(srcs))


def sanitize_formulas(text: str) -> str:
    """Убирает LaTeX-делимитеры/команды и сжимает лишние пробелы."""
    if not text:
        return text

    t = text

    # 1) снести $...$ и $$...$$ — оставив содержимое
    t = settings.latex_pattern.sub(lambda m: m.group(2), t)

    # 2) популярные команды → plain text
    repl = [
        (r'\\text\{([^}]*)\}', r'\1'),
        (r'\\cdot', '·'),
        (r'\\times', '×'),
        (r'\\min', 'min'),
        (r'\\max', 'max'),
        (r'\\leq', '≤'),
        (r'\\geq', '≥'),
        (r'\\pm', '±'),
        (r'\\%', '%'),
        (r'\\,', ' '),
        (r'\\;', ' '),
    ]
    for pat, rep in repl:
        t = re.sub(pat, rep, t)

    # 3) простые дроби \frac{a}{b} → (a / b)
    t = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'(\1 / \2)', t)

    # 4) удалить остаточные бэкслэши-команды \alpha, \something
    t = re.sub(r'\\[A-Za-z]+', '', t)

    # 5) прибрать лишние фигурные скобки
    t = t.replace('{', '').replace('}', '')

    # 6) избавляемся от « • » → markdown-списки
    t = t.replace(' • ', '\n- ')

    # 7) финальная чистка пробелов
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n{3,}', '\n\n', t)

    return t.strip()


def _fmt_grouped(x, digits: int = 6):
    try:
        return f"{float(x):,.{digits}f}".replace(",", " ")
    except Exception:
        return "n/a"

def _fmt6(x):
    return _fmt_grouped(x, digits=6)


def _group_large_numbers_in_text(text: str) -> str:
    if not text:
        return text

    pattern = re.compile(r"(?<![\d/-])-?\d{4,}(?:[.,]\d+)?(?![\d/-])")

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        sign = ""
        if token.startswith("-"):
            sign = "-"
            token = token[1:]

        if "." in token:
            int_part, frac_part = token.split(".", 1)
            sep = "."
        elif "," in token:
            int_part, frac_part = token.split(",", 1)
            sep = ","
        else:
            int_part, frac_part = token, None
            sep = ""

        grouped = f"{int(int_part):,}".replace(",", " ")
        if frac_part is None:
            return f"{sign}{grouped}"
        return f"{sign}{grouped}{sep}{frac_part}"

    return pattern.sub(_replace, text)


def _explain_targets_from_tool_payload(tp: Dict[str, Any]) -> str:
    """
    Возвращает строку с перечнем показателей, которые нужно объяснить текстом.
    Никаких чисел, только термины — чтобы RAG искал методологию.
    """
    if not tp:
        return ""
    comps = (tp.get("components") or {})
    # Карта «человекочитаемого» порядка для pricing
    pricing_keys = [
        ("ets", "ЕТС"), ("ets_rub", "ЕТС в RUB"), ("crl", "СРЛ"), ("nor", "НОР"),
        ("for_rate", "ФОР"), ("funding_rate", "стоимость фондирования"),
        ("eva_rate", "EVA"), ("break_even_rate", "ставка безубыточности"),
        ("non_utilizing_rate", "ставка неутилизирующая лимит"),
        ("marginal_income", "маржинальный доход"),
        ("target_marginal_income", "целевой маржинальный доход"),
    ]
    # Карта для лимитов
    limit_keys = [
        ("Влияние на лимит при котировании", "влияние на лимит при котировании"),
        ("Влияние на лимит действующей сделки", "влияние на лимит действующей сделки"),
        ("Влияние на лимит КПК действующей сделки", "влияние на лимит КПК"),
        ("Влияние на лимит ЦА действующей сделки", "влияние на лимит ЦА"),
        ("k_coef", "коэффициент K"), ("limit_discount_coef", "коэффициент дисконтирования лимита"),
        ("ca_coef", "доля ЦА"),
    ]

    # эвристика: если есть ключи лимитов — считаем кейсом лимитов
    is_limits = any(k in comps for k, _ in limit_keys)
    pairs = limit_keys if is_limits else pricing_keys

    names = [label for k, label in pairs if k in comps]
    # подстраховка: если пусто — хотя бы общие
    if not names:
        names = ["ЕТС", "ФОР", "СРЛ", "НОР", "EVA", "ставка безубыточности", "ставка неутилизирующая лимит"]
    return "; ".join(names)
