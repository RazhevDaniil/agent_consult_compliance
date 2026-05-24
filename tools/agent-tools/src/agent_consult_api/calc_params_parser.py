import re
import logging
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, date

from ..utils.logger_config import configure_logging


configure_logging()
log = logging.getLogger("---CalcParamsParser---")


_CCY_NEAR = re.compile(r'\b(?:руб(?:\.|лей|ля)?|₽|rub|rur|usd|eur|\$|€)\b', re.I)
_AMOUNT_LEAD_LEFT = re.compile(r'(?:сумм[аы]|объ?е[мм]|объем|обьем|amount)\s*[:=]?\s*$', re.I)
_NOT_AMOUNT_NEAR = re.compile(r'(?:%|процент|ставк|дн|дней|срок|мес|год)', re.I)

_amount_re = re.compile(
    r'(?P<num>\d[\d\s.,]*)\s*(?P<u>'
    r'(?:млрд|миллиард(?:ов|а)?)|'      # сначала миллиарды
    r'(?:млн|миллион(?:ов|а)?)|'        # затем миллионы
    r'(?:тыс|тыся[ччи])|'               # тысячи
    r'k|'                               # англ. «k»
    r'м(?![а-я])'                       # одиночная «м», НЕ перед буквой (не схватит «млн/млрд»)
    r')',
    re.I
)

# множители для единиц
_MULT = {
    "млрд": 1_000_000_000, "миллиард": 1_000_000_000, "миллиарда": 1_000_000_000, "миллиардов": 1_000_000_000,
    "ярд": 1_000_000_000, "ярдов": 1_000_000_000,
    "млн": 1_000_000, "миллион": 1_000_000, "миллиона": 1_000_000, "миллионов": 1_000_000,
    "лямов": 1_000_000, "лимонов": 1_000_000, "лимон": 1_000_000, "лям": 1_000_000,
    "тыс": 1_000, "тысяча": 1_000, "тысячи": 1_000, "тысяч": 1_000, "к": 1_000, "касарей": 1_000, "кэсов": 1_000,
    "k": 1_000, "м": 1_000_000,  # «м» — только как самостоятельная единица, см. regex ниже
}

_CCY = {"rub":"RUB","руб":"RUB","₽":"RUB","rur":"RUB", "рублей":"RUB", "рубли":"RUB"}
_PROD = {"депо":"DEPO","depo":"DEPO","deposit":"DEPO", "депоз":"DEPO",
         "нсо":"NSO","nso":"NSO", "неснижаемый остаток":"NSO", "не снижаемый остаток":"NSO", "НСО":"NSO"}
_BOOL = {"да": True, "нет": False, "true": True, "false": False}

_rate_re   = re.compile(r'(?:ставка|ставки|ставкой)\s*(?P<rate>[\d.,]+)\s*%?', re.I)
_inn_re = re.compile(r'(?:(?:инн|inn)\s*[:=]?\s*)(\d{9}|\d{10}|\d{12})\b', re.I)
_date_re   = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}[./]\d{2}[./]\d{4})')
_term_re   = re.compile(r'на\s*(?P<n>\d+)\s*(?P<u>дн|дней|дня|нед|недел|мес|месяц|месяцев|год|года|лет)', re.I)


class DealSelector(BaseModel):
    deal_id: Optional[str]=None
    inn: Optional[str]=None
    term: Optional[int]=None
    currency: Optional[str]=None
    product: Optional[str]=None
    interest_rate: Optional[float]=None
    amount: Optional[float]=None
    deal_dt: Optional[str]=None
    maturity_dt: Optional[str]=None
    optionality: Optional[str]=None
    otz_type: Optional[str]=None


def _to_decimal(s: str) -> Decimal:
    # убираем тонкие пробелы и обычные
    s = s.replace('\u00A0', ' ').replace('\u202F', ' ').replace(' ', '')
    if ',' in s and '.' in s:
        # последний разделитель — десятичный
        last = max(s.rfind(','), s.rfind('.'))
        dec = s[last]
        thou = '.' if dec == ',' else ','
        s = s.replace(thou, '')
        s = s.replace(dec, '.')
    elif ',' in s and '.' not in s:
        # если после запятой ровно 3 цифры — это, скорее, разделитель тысяч
        s = s.replace(',', '') if re.search(r',\d{3}(?!\d)', s) else s.replace(',', '.')
    return Decimal(s)


def parse_amount(user_text: str) -> int | None:
    """
    Возвращает сумму в абсолютных единицах (int) или None.
    Отбрасывает «голые» числа, если рядом нет единиц измерения или валюты/маркера.
    """
    best = None

    for m in _amount_re.finditer(user_text):
        num_str = m.group('num')
        unit = (m.group('u') or '').lower().rstrip('.')
        has_unit = unit in _MULT

        # контекст вокруг совпадения
        left = max(0, m.start() - 24)
        right = min(len(user_text), m.end() + 24)
        ctx = user_text[left:right]
        left_ctx = user_text[left:m.start()]

        # отсечём явно не сумму (проценты/сроки) если нет единицы
        if not has_unit and _NOT_AMOUNT_NEAR.search(ctx):
            continue

        # требуем: есть единица ИЛИ рядом валюта ИЛИ слева маркер "сумма/объем"
        near_ccy = bool(_CCY_NEAR.search(ctx))
        lead_left = bool(_AMOUNT_LEAD_LEFT.search(left_ctx))
        if not (has_unit or near_ccy or lead_left):
            continue

        try:
            base = _to_decimal(num_str)
        except Exception:
            continue

        mult = _MULT.get(unit, 1)
        value = int(base * mult)

        # берём наиболее "уверенную" сумму: при равенстве — большую
        score = (has_unit * 2) + (near_ccy) + (lead_left)
        if best is None or score > best[0] or (score == best[0] and value > best[1]):
            best = (score, value)
    return None if best is None else best[1]


def _to_decimal_rate(s: str) -> Decimal:
    return Decimal(s.replace(' ', '').replace(',', '.'))

def _parse_date(s: str) -> date:
    if "-" in s:
        return datetime.strptime(s, "%Y-%m-%d").date()
    return datetime.strptime(s, "%d.%m.%Y").date()

def _guess_currency(text: str) -> Optional[str]:
    t = text.lower()
    for k,v in _CCY.items():
        if k in t: return v
    return None

def _guess_product(text: str) -> Optional[str]:
    t = text.lower()
    for k,v in _PROD.items():
        if k in t: return v
    return None

def parse_rate(text: str) -> Optional[Decimal]:
    m = _rate_re.search(text)
    return _to_decimal_rate(m.group('rate'))/Decimal('100') if m else None

def parse_dates(text: str) -> dict:
    ds = _date_re.findall(text)
    # простая эвристика: первая — value_dt, вторая — maturity_dt (или deal_dt если явно указано)
    out = {}
    if ds:
        out["value_dt"] = _parse_date(ds[0])
    if len(ds) >= 2:
        if _parse_date(ds[1]) > out["value_dt"]:
            out["maturity_dt"] = _parse_date(ds[1])
        else:
            out["value_dt"] = _parse_date(ds[1])
            out["maturity_dt"] = _parse_date(ds[0])
    return out

def parse_term_days(text: str, start_dt: Optional[date]) -> Optional[int]:
    m = _term_re.search(text)
    if not m: return None
    n = int(m.group('n')); u = m.group('u').lower()
    if 'дн' in u: return n
    if 'нед' in u: return n*7
    if 'мес' in u: return n*30  # без relativedelta
    if 'год' in u or 'лет' in u: return n*365
    return None

def extract_selector_and_overrides(user_text: str, explicit: dict) -> DealSelector:

    sel = DealSelector()

    deal_id = explicit.get("deal_id")
    if deal_id:
        sel.deal_id = deal_id
        log.info(f'--- Get deal_id from initial query - that is enough, finish parsing. DealSelector: {sel} ---')
        return sel
    else:
        log.info(f"--- Don't get deal_id from initial query, continue parsing ---")
        m = _inn_re.search(user_text)
        sel.inn = explicit.get("inn") or (m.group(1) if m else None)

        sel.currency = explicit.get("ccy") or _guess_currency(user_text)
        sel.product = explicit.get("product") or _guess_product(user_text)
        sel.interest_rate = explicit.get("interest_rate") or parse_rate(user_text)
        sel.amount = explicit.get("amount") or parse_amount(user_text)

        dates = parse_dates(user_text)
        sel.deal_dt = explicit.get("deal_dt") or dates.get("value_dt")
        sel.maturity_dt = explicit.get("maturity_dt") or dates.get("maturity_dt")

        if explicit.get("term"):
            sel.term = explicit['term']
        elif dates.get("maturity_dt") and dates.get("value_dt"):
            sel.term = (dates.get("maturity_dt") - dates.get("value_dt")).days
        elif dates.get("value_dt"):
            sel.term = parse_term_days(user_text, dates.get("value_dt"))
        else:
            sel.term = None
        log.info(f'--- Finish parsing. DealSelector has been cooked! ---')
        return sel
