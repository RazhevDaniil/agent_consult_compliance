from __future__ import annotations

import math
import logging
import datetime
import re
import os
import numpy as np
import pandas as pd
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger_config import configure_logging


configure_logging()
log = logging.getLogger("---KPK Limit Analysis---")


def _fmt_date(val, default: str = "Н/Д") -> str:
    """Форматирует дату как YYYY-MM-DD, отбрасывая время."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    try:
        return str(pd.to_datetime(val).date())
    except Exception:
        return str(val)


def _fmt_datetime(val, default: str = "Н/Д") -> str:
    """Форматирует дату+время как YYYY-MM-DD HH:MM."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(val)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Безопасно приводит значение к float, возвращая default для None/NaN/мусора."""
    if val is None:
        return default
    if isinstance(val, str):
        cleaned = val.replace(" ", "").replace(",", ".").strip()
        if cleaned.lower() in {"", "none", "nan", "null", "n/a"}:
            return default
        val = cleaned
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(num) or math.isinf(num)) else num


def _normalize_id_value(val: Any) -> Optional[str]:
    """Приводит идентификаторы к консистентному строковому виду."""
    try:
        if val is None or pd.isna(val):
            return None
    except TypeError:
        pass

    if isinstance(val, (np.integer, int)):
        return str(int(val))

    if isinstance(val, (np.floating, float)):
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return None
        return str(int(num)) if num.is_integer() else str(num).rstrip("0").rstrip(".")

    text = str(val).strip()
    if text.lower() in {"", "none", "nan", "null", "n/a"}:
        return None
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _normalize_id_columns(df: Optional[pd.DataFrame], columns: Sequence[str]) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return df
    existing = [col for col in columns if col in df.columns]
    if not existing:
        return df
    df = df.copy()
    for col in existing:
        df[col] = df[col].map(_normalize_id_value)
    return df


def _string_series(df: pd.DataFrame, col: str, default: str = "") -> pd.Series:
    if col in df.columns:
        series = df[col]
    else:
        series = pd.Series(default, index=df.index, dtype="object")
    return series.fillna(default).astype(str)


def _coerce_numeric_series(series: pd.Series) -> pd.Series:
    """Приводит Series к float, безопасно обрабатывая строковые числа."""
    if series.empty:
        return series.astype(float)
    return pd.to_numeric(series, errors="coerce").astype(float)


def _stable_sort(df: Optional[pd.DataFrame], columns: Sequence[str], *, ascending=True) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return df
    existing = [col for col in columns if col in df.columns]
    if not existing:
        return df
    return df.sort_values(existing, ascending=ascending, kind="mergesort", na_position="last")


def _df_summary(
        df: Optional[pd.DataFrame],
        *,
        date_cols: Optional[Sequence[str]] = None,
        id_cols: Optional[Sequence[str]] = None,
        numeric_cols: Optional[Sequence[str]] = None,
) -> dict:
    """Small log-friendly snapshot of a dataframe without row-level data."""
    if df is None:
        return {"state": "none"}

    summary = {
        "rows": int(len(df)),
        "cols": int(len(df.columns)),
        "empty": bool(df.empty),
        "columns": list(df.columns),
    }
    if df.empty:
        return summary

    date_stats = {}
    for col in date_cols or []:
        if col in df.columns:
            values = pd.to_datetime(df[col], errors="coerce").dropna()
            date_stats[col] = {
                "min": str(values.min().date()) if not values.empty else None,
                "max": str(values.max().date()) if not values.empty else None,
                "unique": int(values.dt.date.nunique()) if not values.empty else 0,
            }
    if date_stats:
        summary["dates"] = date_stats

    id_stats = {}
    for col in id_cols or []:
        if col in df.columns:
            id_stats[col] = int(df[col].dropna().nunique())
    if id_stats:
        summary["unique"] = id_stats

    numeric_stats = {}
    for col in numeric_cols or []:
        if col in df.columns:
            values = pd.to_numeric(df[col], errors="coerce").dropna()
            numeric_stats[col] = {
                "count": int(values.count()),
                "sum": float(values.sum()) if not values.empty else 0.0,
            }
    if numeric_stats:
        summary["numeric"] = numeric_stats

    return summary


def _log_df_state(
        label: str,
        df: Optional[pd.DataFrame],
        *,
        date_cols: Optional[Sequence[str]] = None,
        id_cols: Optional[Sequence[str]] = None,
        numeric_cols: Optional[Sequence[str]] = None,
) -> None:
    summary = _df_summary(
        df,
        date_cols=date_cols,
        id_cols=id_cols,
        numeric_cols=numeric_cols,
    )
    if summary.get("state") == "none" or summary.get("empty"):
        log.warning("[kpk_analysis] %s: %s", label, summary)
    else:
        log.info("[kpk_analysis] %s: %s", label, summary)


def _normalize_trx_for_analysis(
        df: Optional[pd.DataFrame],
        *,
        target_division_cd: Optional[str] = None,
) -> pd.DataFrame:
    """Приводит перераспределения к знаковому виду относительно КПК."""
    empty = pd.DataFrame(columns=DEFAULT_TRX_COLS)
    if df is None or df.empty:
        return empty

    df = _ensure_dt(df, "report_dt")
    df = _normalize_id_columns(df, ("division_cd", "division_from_cd", "division_to_cd", "author_nm"))
    target_division_cd = _normalize_id_value(target_division_cd)

    has_direction = ("division_from_cd" in df.columns) or ("division_to_cd" in df.columns)
    if not has_direction:
        if "division_cd" not in df.columns:
            return empty
        out = df.copy()
        out["transaction_limit_amt"] = pd.to_numeric(out["transaction_limit_amt"], errors="coerce").fillna(0.0)
        if target_division_cd:
            out = out[out["division_cd"] == target_division_cd].copy()
    else:
        base = df.copy()
        amount_abs = pd.to_numeric(base["transaction_limit_amt"], errors="coerce").fillna(0.0).abs()
        frames: List[pd.DataFrame] = []

        if target_division_cd:
            if "division_to_cd" in base.columns:
                incoming = base[base["division_to_cd"] == target_division_cd].copy()
                if not incoming.empty:
                    incoming["division_cd"] = target_division_cd
                    incoming["transaction_limit_amt"] = amount_abs.loc[incoming.index]
                    frames.append(incoming)
            if "division_from_cd" in base.columns:
                outgoing = base[base["division_from_cd"] == target_division_cd].copy()
                if not outgoing.empty:
                    outgoing["division_cd"] = target_division_cd
                    outgoing["transaction_limit_amt"] = -amount_abs.loc[outgoing.index]
                    frames.append(outgoing)
        else:
            if "division_to_cd" in base.columns:
                incoming = base.copy()
                incoming["division_cd"] = incoming["division_to_cd"]
                incoming["transaction_limit_amt"] = amount_abs
                incoming = incoming[incoming["division_cd"].notna()].copy()
                if not incoming.empty:
                    frames.append(incoming)
            if "division_from_cd" in base.columns:
                outgoing = base.copy()
                outgoing["division_cd"] = outgoing["division_from_cd"]
                outgoing["transaction_limit_amt"] = -amount_abs
                outgoing = outgoing[outgoing["division_cd"].notna()].copy()
                if not outgoing.empty:
                    frames.append(outgoing)

        if not frames:
            return empty
        out = pd.concat(frames, ignore_index=True)

    for col in DEFAULT_TRX_COLS:
        if col not in out.columns:
            out[col] = None

    out = out[DEFAULT_TRX_COLS].copy()
    if "transaction_dttz" in out.columns:
        out["transaction_dttz"] = pd.to_datetime(out["transaction_dttz"], errors="coerce")
    sorted_out = _stable_sort(out, ("report_dt", "transaction_dttz", "author_nm"), ascending=True)
    return sorted_out if sorted_out is not None else empty


# ─── Словарь сегмент → фамилии сотрудников ЦА (компенсация лимитов) ─────────
# Заполните списки реальными фамилиями.
SEGMENT_CA_NAMES: Dict[str, List[str]] = {
    "КСБ": ["Смирнов А.А."],
    "КФИ": ["Кузнецова М.В."],
    "РГС": ["Попов Д.И."],
}

# Обратный маппинг: фамилия (lowercase) → сегмент
_CA_NAME_TO_SEGMENT: Dict[str, str] = {}
for _seg, _names in SEGMENT_CA_NAMES.items():
    for _name in _names:
        normalized = _name.strip().lower()
        _CA_NAME_TO_SEGMENT[normalized] = _seg
        surname = normalized.split()[0]
        _CA_NAME_TO_SEGMENT[surname] = _seg


def _check_ca_compensation(author_nm: str) -> Optional[str]:
    """Возвращает название сегмента, если автор из ЦА, иначе None."""
    if not author_nm:
        return None
    normalized = author_nm.strip().lower()
    surname = normalized.split()[0]
    return _CA_NAME_TO_SEGMENT.get(normalized) or _CA_NAME_TO_SEGMENT.get(surname)


def sanitize_json(x):
    if x is None:
        return None
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(x, (np.integer, int)):
        return int(x)
    if isinstance(x, dict):
        return {k: sanitize_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [sanitize_json(v) for v in x]
    return x


DEFAULT_LIMIT_COLS = [
    "report_dt",
    "calc_limit_dt",
    "division_cd",
    # "division_nm",
    "limit_amt",
    "ccy_cd",
    "business_block_cd",
    "parent_division_cd",
    "parent_division_nm",
]

DEFAULT_TRX_COLS = [
    "report_dt",
    "transaction_dttz",
    "transaction_limit_amt",
    "division_cd",
    "division_from_cd",
    "division_to_cd",
    "business_block_cd",
    "author_nm",
    "author_tab_num",
]

DEFAULT_DEALS_COLS = [
    "upload_dt",
    "value_dt",
    "division_cd",
    "source_system_cd",
    "product_cd",
    "delta_limit_amt",
    "original_delta_limit_amt",
    "internal_order_cd",
]

NON_COMPLIANCE_RETURN_DAYS = 5  # рабочих дней

_DISAPPEARED_LOG_STATUSES = {"BreachDealClosed", "ClientDeclined", "BankDeclined"}

_STATUS_IMPACT_DAYS = {
    "BreachDealClosed": 5,      # рабочих дней
    "ClientDeclined": 1,  # рабочий день
    "BankDeclined": 0,    # сразу
    "EarlyTerminated": -1,  # влияние продолжается до даты погашения (не возвращается)
}

_DEAL_STATUS_EXPLANATIONS = {
    "auto_cotirovka": "автокотировка — влияние начнётся на утро 5 рабочего дня с даты валютирования",
    "km_cotirovka": "прокотирована КМ — должна отразиться на следующий рабочий день с даты валютирования",
    "group_deal": "групповая сделка — влияние начнётся на утро 5 рабочего дня с даты валютирования",
    "incorrect": "не прошла валидацию бизнес-правил",
}


def _ensure_dt(df: pd.DataFrame, col: str = "report_dt") -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if not pd.api.types.is_datetime64_any_dtype(df[col]):
        df = df.copy()
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _sort_calc_logs(logs: Optional[pd.DataFrame], *, ascending: bool = True) -> pd.DataFrame:
    """Сортирует расчётные логи по времени calculation_dttm."""
    if logs is None or logs.empty or "calculation_dttm" not in logs.columns:
        return pd.DataFrame() if logs is None else logs
    logs = _ensure_dt(logs, "calculation_dttm")
    return logs.sort_values("calculation_dttm", ascending=ascending, na_position="last")


def _resolve_date(df: pd.DataFrame, date: Optional[str], *, today_override: Optional[str] = None) -> str:
    """Если дата не задана — берём максимальную report_dt, не позднее today.

    Parameters
    ----------
    today_override : str | None
        Если задано (формат YYYY-MM-DD) — используется вместо реальной
        системной даты.  Пробрасывается из переменной окружения
        ``KPK_TODAY_OVERRIDE`` через вызывающий код.
    """
    if date:
        return str(pd.to_datetime(date).date())

    def _effective_today() -> datetime.date:
        if today_override:
            try:
                return datetime.date.fromisoformat(today_override)
            except ValueError:
                pass
        return datetime.date.today()

    effective = _effective_today()

    if df is None or df.empty:
        return effective.isoformat()

    df = _ensure_dt(df, "report_dt")
    today_ts = pd.to_datetime(effective)
    mx = df["report_dt"].max()
    if pd.isna(mx):
        return effective.isoformat()
    # если в данных есть даты будущего — ограничиваемся today
    chosen = mx if mx <= today_ts else today_ts
    return str(pd.to_datetime(chosen).date())



def _resolve_snapshot_date(
        df: pd.DataFrame,
        date: Optional[str],
        *,
        snapshot_column: str = "upload_dt",
        today_override: Optional[str] = None,
) -> str:
    """Если дата не задана — берём максимальную дату снимка, не позднее today."""
    if date:
        return str(pd.to_datetime(date).date())

    if df is None or df.empty or snapshot_column not in df.columns:
        return _resolve_date(df, date, today_override=today_override)

    def _effective_today() -> datetime.date:
        if today_override:
            try:
                return datetime.date.fromisoformat(today_override)
            except ValueError:
                pass
        return datetime.date.today()

    effective = _effective_today()
    df = _ensure_dt(df, snapshot_column)
    today_ts = pd.to_datetime(effective)
    mx = df[snapshot_column].max()
    if pd.isna(mx):
        return effective.isoformat()
    chosen = mx if mx <= today_ts else today_ts
    return str(pd.to_datetime(chosen).date())


def _snapshot_to_business_date(snapshot_date: Optional[str]) -> Optional[str]:
    if not snapshot_date:
        return None
    return str((pd.to_datetime(snapshot_date) - pd.Timedelta(days=1)).date())


def get_prev_date(df: pd.DataFrame, date: str, dt_column: str = "report_dt") -> Optional[str]:
    """Возвращает ближайшую предыдущую дату < date из df[dt_column]."""
    if df is None or df.empty:
        return None

    df = _ensure_dt(df, dt_column)
    d = pd.to_datetime(date)
    prev = df.loc[df[dt_column] < d, dt_column].max()
    if pd.isna(prev):
        return None
    return str(pd.to_datetime(prev).date())

def get_diff_limit_between_days(
        df: pd.DataFrame,
        kpk_list: Sequence[str],
        date: Optional[str] = None,
        date_from: Optional[str] = None,
        dt_column: str = "report_dt",
        value_column: str = "limit_amt",
        today_override: Optional[str] = None,
) -> Tuple[pd.DataFrame, Optional[str], str]:
    """Считает изменение лимита(t и t-1) по списку КПК

    Возвращает:
      - DataFrame: division_cd, dif_lim, limit_t, limit_prev
      - prev_date (str|None)
      - date (str)
    """

    log.info(
        "[kpk_analysis] get_diff_limit_between_days start date=%s date_from=%s kpk_count=%s",
        date,
        date_from,
        len(kpk_list or []),
    )
    if df is None or df.empty:
        resolved_date = _resolve_date(df, date, today_override=today_override)
        log.warning(
            "[kpk_analysis] get_diff_limit_between_days input limits are empty; returning empty diffs date=%s",
            resolved_date,
        )
        return pd.DataFrame(columns=["division_cd", "dif_lim", "limit_t", "limit_prev"]), None, resolved_date

    df = _ensure_dt(df, dt_column)
    df = _normalize_id_columns(df, ("division_cd",))
    date = _resolve_date(df, date, today_override=today_override)
    kpk_list = [_normalize_id_value(kpk) for kpk in kpk_list]
    kpk_list = [kpk for kpk in kpk_list if kpk]
    _log_df_state(
        "get_diff input limits",
        df,
        date_cols=[dt_column],
        id_cols=["division_cd"],
        numeric_cols=[value_column],
    )
    # Если указана начальная дата — используем её, иначе ищем ближайшую предыдущую
    if date_from:
        prev_date = str(pd.to_datetime(date_from).date())
    else:
        prev_date = get_prev_date(df, date, dt_column=dt_column)
    log.info(
        "[kpk_analysis] get_diff resolved dates date=%s prev_date=%s kpk_list=%s",
        date,
        prev_date,
        kpk_list,
    )

    # если не нашли предыдущую дату — достаём реальные limit_t, prev = t (delta=0)
    if prev_date is None:
        target = pd.to_datetime(date)
        sub_t = df[df["division_cd"].isin(list(kpk_list)) & (df[dt_column] == target)]
        if sub_t.empty:
            log.warning(
                "[kpk_analysis] get_diff no current limit rows date=%s kpk_list=%s; returning NaN limits and zero delta",
                date,
                kpk_list,
            )
            out = pd.DataFrame({
                "division_cd": list(kpk_list),
                "dif_lim": [0.0] * len(kpk_list),
                "limit_t": [math.nan] * len(kpk_list),
                "limit_prev": [math.nan] * len(kpk_list),
            })
            return out, None, date
        sub_t = _stable_sort(sub_t, [dt_column, "calc_limit_dt"])
        limit_map = (
            sub_t.drop_duplicates("division_cd", keep="last")
            .set_index("division_cd")[value_column]
        )
        out = pd.DataFrame({"division_cd": list(kpk_list)})
        out["limit_t"] = out["division_cd"].map(limit_map).astype(float)
        out["limit_prev"] = out["limit_t"]
        out["dif_lim"] = 0.0
        _log_df_state(
            "get_diff output without prev date",
            out,
            id_cols=["division_cd"],
            numeric_cols=["dif_lim", "limit_t", "limit_prev"],
        )
        return out, None, date

    dates = [pd.to_datetime(prev_date), pd.to_datetime(date)]
    sub = df[df["division_cd"].isin(list(kpk_list)) & df[dt_column].isin(dates)]
    if sub.empty:
        log.warning(
            "[kpk_analysis] get_diff no limit rows for requested dates date=%s prev_date=%s kpk_list=%s",
            date,
            prev_date,
            kpk_list,
        )
        out = pd.DataFrame(columns=["division_cd", "dif_lim", "limit_t", "limit_prev"])
        return out, prev_date, date
    sub = _stable_sort(sub, [dt_column, "calc_limit_dt"])
    _log_df_state(
        "get_diff matched limit rows",
        sub,
        date_cols=[dt_column],
        id_cols=["division_cd"],
        numeric_cols=[value_column],
    )

    piv = (
        sub[["division_cd", dt_column, value_column]]
        .dropna(subset=[dt_column])
        .drop_duplicates(["division_cd", dt_column], keep="last")
        .set_index(["division_cd", dt_column])[value_column]
        .unstack(dt_column)
    )

    prev_col = pd.to_datetime(prev_date)
    curr_col = pd.to_datetime(date)
    if prev_col not in piv.columns:
        piv[prev_col] = math.nan
    if curr_col not in piv.columns:
        piv[curr_col] = math.nan

    piv = piv[[prev_col, curr_col]]

    out = piv.reset_index().rename(columns={prev_col: "limit_prev", curr_col: "limit_t"})
    out["limit_prev"] = _coerce_numeric_series(out["limit_prev"])
    out["limit_t"] = _coerce_numeric_series(out["limit_t"])
    out["dif_lim"] = out["limit_t"] - out["limit_prev"]

    out = out[["division_cd", "dif_lim", "limit_t", "limit_prev"]]
    _log_df_state(
        "get_diff output",
        out,
        id_cols=["division_cd"],
        numeric_cols=["dif_lim", "limit_t", "limit_prev"],
    )
    return out, prev_date, date


def get_distributed_limits(
        df: pd.DataFrame,
        date: Optional[str],
        kpk_list: Sequence[str],
        default_columns: Sequence[str] = DEFAULT_TRX_COLS,
        today_override: Optional[str] = None,
) -> pd.DataFrame:
    """Считает суммарное влияние перераспределений на КПК на дату."""

    if df is None or df.empty:
        return pd.DataFrame(columns=["division_cd", "distributed_amt"])

    df = _normalize_trx_for_analysis(df)
    date = _resolve_date(df, date, today_override=today_override)

    kpk_list = list(kpk_list) if isinstance(kpk_list, (list, tuple, set)) else [kpk_list]
    kpk_list = [_normalize_id_value(kpk) for kpk in kpk_list]
    kpk_list = [kpk for kpk in kpk_list if kpk]
    if not kpk_list:
        return pd.DataFrame(columns=["division_cd", "distributed_amt"])

    author_series = _string_series(df, "author_nm")
    sub = df[
        (df["report_dt"] == pd.to_datetime(date))
        & (df["division_cd"].isin(kpk_list))
        & (~author_series.isin(["", "AUTO"]))
        & (pd.to_numeric(df["transaction_limit_amt"], errors="coerce") != 0)
        ]

    if sub.empty:
        return pd.DataFrame({"division_cd": kpk_list, "distributed_amt": [0.0] * len(kpk_list)})

    g = sub.groupby("division_cd", as_index=False)["transaction_limit_amt"].sum()
    g = g.rename(columns={"transaction_limit_amt": "distributed_amt"})
    return g


def _decision_from_delta_and_dist(
        delta: Optional[float],
        dist: Optional[float],
        *,
        tol_abs: float = 1.0,
        tol_rel: float = 0.05,
) -> str:
    """Простой классификатор причины изменения"""

    if delta is None or (isinstance(delta, float) and math.isnan(delta)):
        return "unknown"

    dist = dist or 0.0

    # сравниваем по модулю
    d = abs(float(delta))
    r = abs(float(dist))

    tol = max(tol_abs, d * tol_rel)

    if d < tol_abs and r < tol_abs:
        return "no_change"

    if abs(d - r) <= tol:
        return "redistribution"

    if d > r + tol:
        return "needs_support"

    return "other"


def _get_workdays_diff(val_dt: Any, upl_dt: Any) -> int:
    """
    Считает разницу в рабочих днях с учетом кастомных бизнес-правил:
    - Валютирование Сб -> Загрузка Пн: 0 дней
    - Валютирование [Пн, Вт, Ср] -> Загрузка Пн: 5 дней
    - Валютирование Чт -> Загрузка Вт: 5 дней
    - Валютирование Пт -> Загрузка Ср: 5 дней

    ВАЖНО: Для учета праздников РФ (производственный календарь) необходимо
    передать список праздников в параметр holidays:
    np.busday_count(v, u, holidays=['2023-01-01', ...])
    """
    if pd.isna(val_dt) or pd.isna(upl_dt):
        return -999 # маркер ошибки дат
    try:
        # Преобразуем входные данные для удобной работы с днями недели
        v_date = pd.to_datetime(val_dt)
        u_date = pd.to_datetime(upl_dt)
        u_norm = u_date.normalize()
        # Получаем дни недели: 0 - Пн, 1 - Вт
        v_wd = v_date.weekday()
        # Вычисляем разницу в календарных днях для защиты от обратного порядка дат
        # или совпадения дней (например, Пн и тот же Пн)
        days_diff = (u_date.date() - v_date.date()).days
        # Применяем кастомную логику только если загрузка была ПОСЛЕ валютирования
        if days_diff > 0:
            # 1. Сб -> следующий допустимый снимок после понедельника
            if v_wd == 5:
                expected = _roll_forward_to_allowed_snapshot_day(v_date + pd.Timedelta(days=2))
                if expected is not None and u_norm == expected:
                    return 0
            # 2. [Пн, Вт, Ср] -> следующий допустимый снимок после понедельника
            elif v_wd in [0, 1, 2]:
                expected = _roll_forward_to_allowed_snapshot_day(v_date + pd.Timedelta(days=7 - v_wd))
                if expected is not None and u_norm == expected:
                    return 5
            # 3. Чт -> следующий допустимый снимок после вторника
            elif v_wd == 3:
                expected = _roll_forward_to_allowed_snapshot_day(v_date + pd.Timedelta(days=5))
                if expected is not None and u_norm == expected:
                    return 5
            # 4. Пт -> следующий допустимый снимок после среды
            elif v_wd == 4:
                expected = _roll_forward_to_allowed_snapshot_day(v_date + pd.Timedelta(days=5))
                if expected is not None and u_norm == expected:
                    return 5

        # Фоллбэк: стандартный расчет для всех остальных случаев (и для совпадения дней)
        holidays = [
            holiday.date().isoformat()
            for holiday in sorted(_configured_holiday_timestamps())
        ]
        v = np.datetime64(v_date.date(), 'D')
        u = np.datetime64(u_date.date(), 'D')
        return int(np.busday_count(v, u, holidays=holidays))

    except Exception:
        return -999


def _configured_holiday_timestamps() -> set[pd.Timestamp]:
    holidays: set[pd.Timestamp] = set()
    raw = os.getenv("KPK_HOLIDAYS", "2026-05-11")
    for token in raw.split(","):
        ts = pd.to_datetime(token.strip(), errors="coerce")
        if pd.notna(ts):
            holidays.add(ts.normalize())
    return holidays


def _roll_forward_to_allowed_snapshot_day(ts: Any) -> pd.Timestamp | None:
    normalized = pd.to_datetime(ts, errors="coerce")
    if pd.isna(normalized):
        return None
    normalized = normalized.normalize()
    holidays = _configured_holiday_timestamps()
    for _ in range(14):
        if normalized.weekday() <= 4 and normalized not in holidays:
            return normalized
        normalized = normalized + pd.Timedelta(days=1)
    return normalized


def _resolve_status_effective_date(base_dt: Any, target_days: int) -> Optional[pd.Timestamp]:
    """
    Возвращает первую рабочую дату, на которую срабатывает статусное правило.

    Использует ту же кастомную бизнес-логику, что и _get_workdays_diff, чтобы
    даты прекращения/возврата лимита совпадали с датами появления влияния.
    """
    if pd.isna(base_dt):
        return None
    try:
        base = pd.to_datetime(base_dt).normalize()
    except Exception:
        return None

    if target_days <= 0:
        return _roll_forward_to_allowed_snapshot_day(base)

    # Для правил "утро 5-го рабочего дня" в проекте используется
    # особая бизнес-логика:
    # Пн/Вт/Ср -> следующий понедельник,
    # Чт -> следующий вторник,
    # Пт -> следующая среда,
    # затем результат сдвигается на ближайший допустимый снимок
    # (если дата попала на выходной/праздник).
    if target_days == 5:
        weekday = base.weekday()
        if weekday in (0, 1, 2):
            candidate = base + pd.Timedelta(days=7 - weekday)
        elif weekday == 3:
            candidate = base + pd.Timedelta(days=5)
        elif weekday == 4:
            candidate = base + pd.Timedelta(days=5)
        elif weekday == 5:
            candidate = base + pd.Timedelta(days=2)
        else:
            candidate = base + pd.Timedelta(days=1)
        return _roll_forward_to_allowed_snapshot_day(candidate)

    search_horizon = max(14, target_days + 10)
    for offset in range(0, search_horizon + 1):
        candidate = base + pd.Timedelta(days=offset)
        if candidate.weekday() >= 5:
            continue
        if _get_workdays_diff(base, candidate) >= target_days:
            return candidate.normalize()
    return None

def _classify_deal(row: pd.Series) -> str:
    """
    Классифицирует сделку как 'correct', 'incorrect' или 'unknown'
    на основе бизнес-правил продукта и системы-источника.
    """
    # Используем first_upload_dt (дату первого появления в системе) для классификации,
    # чтобы переведённые между КПК сделки сохраняли свою корректную классификацию
    upload_for_classify = row.get("first_upload_dt")
    if upload_for_classify is None or (hasattr(upload_for_classify, '__class__') and pd.isna(upload_for_classify)):
        upload_for_classify = row.get("upload_dt")
    diff_days = _get_workdays_diff(row.get("value_dt"), upload_for_classify)

    if diff_days == -999:
        return "incorrect" # Нет дат для анализа

    src = str(row.get("source_system_cd", "")).lower().strip()
    prod = str(row.get("product_cd", "")).upper().strip()

    # Правило 1: прокотированные КМ
    if src == 'urn:sbrfsystems:99-ufs-sr':
        if prod == 'DEPO' and diff_days == 1:
            return "km_cotirovka"
        if prod == 'NSO' and diff_days == 0:
            return "km_cotirovka"

    # Правило 2: автокотирование (на утро 5-го дня = 5 рабочих дней разницы)
    elif src in ['urn:sbrfsystems:99-ufs-depositsweb', 'urn:sbrfsystems:99-ufs-sct']:
        if diff_days == 5:
            # Правило 3: групповые сделки (source=sct, group_id не пустой)
            group_id = str(row.get("group_id", "")).strip()
            if src == 'urn:sbrfsystems:99-ufs-sct' and group_id:
                return "group_deal"
            return "auto_cotirovka"

    return "incorrect"

def analyze_deals_for_kpk(
        df_deals: pd.DataFrame,
        division_cd: str,
        date: str,
        delta_limit_prev_day: float,
        include_details: bool = False,
        df_calc_logs: Optional[pd.DataFrame] = None,
        df_deals_full: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Анализ таблицы сделок для КПК. Возвращает суммы и топ договоров."""
    log.info(
        "[kpk_analysis] analyze_deals_for_kpk start division_cd=%s date=%s delta_limit_prev_day=%s include_details=%s",
        division_cd,
        date,
        delta_limit_prev_day,
        include_details,
    )
    if df_deals is None or df_deals.empty:
        log.warning(
            "[kpk_analysis] analyze_deals_for_kpk no data division_cd=%s date=%s",
            division_cd,
            date,
        )
        return {"status": "no_data"}

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    _log_df_state(
        "analyze_deals input",
        df_deals,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )
    target_date = pd.to_datetime(date)
    sub = df_deals[
        (df_deals["division_cd"] == division_cd) &
        (df_deals["upload_dt"] == target_date)
        ].copy()

    if sub.empty:
        log.warning(
            "[kpk_analysis] analyze_deals_for_kpk no deals on date division_cd=%s date=%s",
            division_cd,
            date,
        )
        return {"status": "no_deals_on_date"}
    _log_df_state(
        "analyze_deals filtered date",
        sub,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )

    sub["delta_limit_amt"] = pd.to_numeric(sub["delta_limit_amt"], errors="coerce").fillna(0.0)
    # Обогащаем first_upload_dt для корректной классификации переведённых сделок
    if df_deals_full is not None:
        sub = _enrich_first_upload_dt(sub, df_deals_full)
    sub["deal_status"] = sub.apply(_classify_deal, axis=1)
    all_new_deals = _build_all_new_deals_list(sub, df_deals_full)

    # Отбираем только корректные сделки (все статусы кроме "incorrect")
    correct_df = sub[sub["deal_status"] != "incorrect"].copy()
    incorrect_df = sub[sub["deal_status"] == "incorrect"].copy()

    # 2. Считаем общее влияние положительных и отрицательных корректных сделок
    correct_pos_df = correct_df[correct_df["delta_limit_amt"] > 0]
    correct_neg_df = correct_df[correct_df["delta_limit_amt"] < 0]

    impact_correct_pos = correct_pos_df["delta_limit_amt"].sum()
    impact_correct_neg = correct_neg_df["delta_limit_amt"].sum()

    # 4. Выводим топ N корректных сделок отрицательно повлиявших на лимит (>= 90%)
    top_negative_deals = []
    if not correct_neg_df.empty:
        # Сортируем по возрастанию (так как числа отрицательные, самыми весомыми будут наименьшие значения)
        correct_neg_df = correct_neg_df.sort_values(by="delta_limit_amt", ascending=True)
        target_impact = _safe_float(delta_limit_prev_day, 0.0) * 0.90 # например, -100 * 0.9 = -90
        use_threshold = target_impact < 0

        cumulative = 0.0
        for _, row in correct_neg_df.iterrows():
            v_dt = pd.to_datetime(row.get("value_dt"))
            u_dt = pd.to_datetime(row.get("upload_dt"))
            d_dt = pd.to_datetime(row.get("deal_dt"))
            # Забираем нужные поля для таблицы
            top_negative_deals.append({
                "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
                "product_cd": row.get("product_cd", "Н/Д"),
                "source_system_cd": row.get("source_system_cd", "Н/Д"),
                "maturity_dt": _fmt_date(row.get("maturity_dt")),
                "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
                "inn": row.get("inn_num", "Н/Д"),
                "value_dt": _fmt_date(row.get("value_dt")),
                "deal_dt": _fmt_date(row.get("deal_dt")),
                "first_upload_dt": _fmt_date(row.get("first_upload_dt") or row.get("upload_dt")),
                "prev_division": row.get("prev_day_division_cd", "Н/Д"),
                "v_dt_weekday": v_dt.weekday(), # 5 - суббота
                "days_diff": _get_workdays_diff(row.get("value_dt"), row.get("first_upload_dt") or row.get("upload_dt")),
                "deal_status": row.get("deal_status", "incorrect"),
                "value_dt_after_upload": bool(pd.notna(v_dt) and pd.notna(u_dt) and v_dt > u_dt),
                "value_dt_after_deal": bool(pd.notna(v_dt) and pd.notna(d_dt) and v_dt > d_dt),
                "deal_to_value_days": int((v_dt.normalize() - d_dt.normalize()).days) if pd.notna(v_dt) and pd.notna(d_dt) and v_dt > d_dt else 0,
            })
            cumulative += row["delta_limit_amt"]
            # Если накопленная сумма стала меньше или равна таргету (т.к. мы в минусе), останавливаемся
            if use_threshold and cumulative <= target_impact:
                break

    # Обогащаем top_negative_deals статусами из логов (BreachDealClosed, EarlyTerminated)
    top_order_cds = set()
    if df_calc_logs is not None:
        for deal in top_negative_deals:
            order_cd = deal.get("internal_order_cd", "")
            enrichment = _enrich_deal_with_calc_log(order_cd, deal.get("value_dt"), df_calc_logs)
            deal.update(enrichment)
            if enrichment.get("calc_log_status"):
                top_order_cds.add(order_cd)

    # BreachDealClosed сделки, не попавшие в top_negative_deals
    non_compliance_deals = []
    if df_calc_logs is not None and not sub.empty:
        non_compliance_deals = check_non_compliance_deals(sub, df_calc_logs, exclude_order_cds=top_order_cds)

    # Формируем список некорректных сделок для отображения
    incorrect_deals = []
    for _, row in incorrect_df.iterrows():
        incorrect_deals.append({
            "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "source_system_cd": row.get("source_system_cd", "Н/Д"),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "original_delta_limit_amt": round(_safe_float(row.get("original_delta_limit_amt")), 2),
            "inn": row.get("inn_num", "Н/Д"),
            "value_dt": _fmt_date(row.get("value_dt")),
            "deal_dt": _fmt_date(row.get("deal_dt")),
            "first_upload_dt": _fmt_date(row.get("first_upload_dt") or row.get("upload_dt")),
            "deal_status": "incorrect",
        })

    res = {
        "status": "analyzed",
        "deals_impact_total": round(float(sub["delta_limit_amt"].sum()), 2),
        "deals_impact_correct_pos": round(float(impact_correct_pos), 2),
        "deals_impact_correct_neg": round(float(impact_correct_neg), 2),
        "all_new_deals": all_new_deals,
        "all_new_deals_count": len(all_new_deals),
        "top_negative_deals": top_negative_deals,
        "non_compliance_deals": non_compliance_deals,
        "incorrect_deals": incorrect_deals,
        "incorrect_deals_count": len(incorrect_deals),
        "deals_impact_incorrect": round(float(incorrect_df["delta_limit_amt"].sum()), 2),
    }
    log.info(
        "[kpk_analysis] analyze_deals_for_kpk done division_cd=%s date=%s summary=%s",
        division_cd,
        date,
        {
            "status": res["status"],
            "deals_impact_total": res["deals_impact_total"],
            "deals_impact_correct_pos": res["deals_impact_correct_pos"],
            "deals_impact_correct_neg": res["deals_impact_correct_neg"],
            "all_new_deals_count": res["all_new_deals_count"],
            "top_negative_count": len(top_negative_deals),
            "incorrect_deals_count": res["incorrect_deals_count"],
            "non_compliance_count": len(non_compliance_deals),
        },
    )

    return res


def _build_all_new_deals_list(
        sub: Optional[pd.DataFrame],
        df_deals_full: Optional[pd.DataFrame] = None,
) -> List[Dict[str, Any]]:
    if sub is None or sub.empty:
        return []

    report_df = _stable_sort(
        sub.copy(),
        ["internal_order_cd", "upload_dt", "value_dt", "deal_dt", "delta_limit_amt", "product_cd", "source_system_cd"],
    )
    first_appearances = report_df.drop_duplicates(["internal_order_cd"], keep="first").copy()

    if df_deals_full is not None:
        first_appearances = _enrich_first_upload_dt(first_appearances, df_deals_full)
    if "deal_status" not in first_appearances.columns:
        first_appearances["deal_status"] = first_appearances.apply(_classify_deal, axis=1)

    first_appearances = _stable_sort(
        first_appearances,
        ["upload_dt", "internal_order_cd"],
        ascending=[False, True],
    )

    deals_list: List[Dict[str, Any]] = []
    for _, row in first_appearances.iterrows():
        deals_list.append({
            "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "source_system_cd": row.get("source_system_cd", "Н/Д"),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "original_delta_limit_amt": round(_safe_float(row.get("original_delta_limit_amt")), 2),
            "inn": row.get("inn_num", "Н/Д"),
            "inn_num": row.get("inn_num", "Н/Д"),
            "value_dt": _fmt_date(row.get("value_dt")),
            "deal_dt": _fmt_date(row.get("deal_dt")),
            "maturity_dt": _fmt_date(row.get("maturity_dt")),
            "upload_dt": _fmt_date(row.get("upload_dt")),
            "first_upload_dt": _fmt_date(row.get("first_upload_dt") or row.get("upload_dt")),
            "deal_status": row.get("deal_status", "incorrect"),
            "group_id": row.get("group_id"),
            "prev_division": row.get("prev_day_division_cd"),
        })

    return deals_list


def filter_new_deals(df_deals: pd.DataFrame, target_date: str, dt_column: str = "upload_dt", date_from: Optional[str] = None, division_cd: Optional[str] = None) -> pd.DataFrame:
    log.info(
        "[kpk_analysis] filter_new_deals start target_date=%s date_from=%s division_cd=%s",
        target_date,
        date_from,
        division_cd,
    )
    if df_deals is None or df_deals.empty:
        log.warning(
            "[kpk_analysis] filter_new_deals input deals are empty target_date=%s division_cd=%s",
            target_date,
            division_cd,
        )
        return pd.DataFrame()

    df_deals = _ensure_dt(df_deals, dt_column)
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    t_date = pd.to_datetime(target_date)

    # Если указана начальная дата — используем её для определения новизны
    if date_from:
        prev_date_str = str(pd.to_datetime(date_from).date())
    else:
        # Предполагается, что get_prev_date импортирована/определена выше
        prev_date_str = get_prev_date(df_deals, str(t_date.date()), dt_column=dt_column)

    df_t = df_deals[df_deals[dt_column] == t_date].copy()
    # Если задан division_cd, фильтруем df_t по КПК, но df_prev оставляем полным
    # чтобы корректно находить prev_day_division_cd для переведённых сделок
    if division_cd:
        df_t = df_t[df_t["division_cd"] == division_cd].copy()
    if df_t.empty:
        log.warning(
            "[kpk_analysis] filter_new_deals no current date deals target_date=%s division_cd=%s",
            target_date,
            division_cd,
        )
        return df_t

    # Если предыдущей даты в принципе нет
    if not prev_date_str:
        df_t["prev_day_division_cd"] = None
        log.warning(
            "[kpk_analysis] filter_new_deals prev date not found; treating all current deals as new target_date=%s division_cd=%s current_rows=%s",
            target_date,
            division_cd,
            len(df_t),
        )
        return df_t

    prev_date = pd.to_datetime(prev_date_str)
    df_prev = df_deals[df_deals[dt_column] == prev_date].copy()

    # ИСПРАВЛЕНИЕ: Если за предыдущий день нет данных, все сделки текущего дня "новые"
    if df_prev.empty:
        df_t["prev_day_division_cd"] = None
        log.warning(
            "[kpk_analysis] filter_new_deals previous date deals are empty; treating all current deals as new target_date=%s prev_date=%s division_cd=%s current_rows=%s",
            target_date,
            prev_date_str,
            division_cd,
            len(df_t),
        )
        return df_t

    # Убираем дублирующиеся колонки (могут появиться после merge/join)
    if df_prev.columns.duplicated().any():
        df_prev = df_prev.loc[:, ~df_prev.columns.duplicated()]
    if df_t.columns.duplicated().any():
        df_t = df_t.loc[:, ~df_t.columns.duplicated()]

    # Ключевые поля для идентификации сделки (без КПК)
    key_cols = ['internal_order_cd', 'deal_dt', 'value_dt', 'inn_num']
    key_cols = [col for col in key_cols if col in df_t.columns and col in df_prev.columns]

    # Создаем мапу: ключ сделки -> division_cd в день t-1
    # ОПТИМИЗАЦИЯ: Используем agg вместо apply для безопасного и быстрого объединения колонок
    df_prev['deal_key'] = df_prev[key_cols].fillna("").astype(str).agg('|'.join, axis=1)
    prev_division_map = df_prev.set_index('deal_key')['division_cd'].to_dict()

    # Присваиваем каждой сделке текущего дня её КПК из прошлого дня
    df_t['deal_key'] = df_t[key_cols].fillna("").astype(str).agg('|'.join, axis=1)
    df_t['prev_day_division_cd'] = df_t['deal_key'].map(prev_division_map)

    # Сделка "новая" для КПК, если:
    # 1. Её вообще не было в системе в день t-1
    # 2. Или она была в системе, но числилась за ДРУГИМ КПК
    df_new = df_t[
        (df_t['prev_day_division_cd'].isna()) |
        (df_t['prev_day_division_cd'] != df_t['division_cd'])
        ].copy()

    log.info(
        "[kpk_analysis] filter_new_deals done target_date=%s prev_date=%s division_cd=%s current_rows=%s prev_rows=%s new_rows=%s",
        target_date,
        prev_date_str,
        division_cd,
        len(df_t),
        len(df_prev),
        len(df_new),
    )
    return df_new


def _enrich_first_upload_dt(df_target: pd.DataFrame, df_full: pd.DataFrame) -> pd.DataFrame:
    """Добавляет колонку first_upload_dt — самый ранний upload_dt для каждого
    internal_order_cd по ВСЕМ КПК в df_full.

    Для переведённых сделок это даёт исходную дату появления в системе,
    чтобы _classify_deal использовал корректный diff_days.

    Если колонка first_upload_dt уже предрассчитана (например на уровне адаптера),
    пропускаем пересчёт.
    """
    if df_target.empty:
        if "first_upload_dt" not in df_target.columns:
            df_target["first_upload_dt"] = pd.Series(dtype="datetime64[ns]")
        return df_target
    # Если колонка уже заполнена (предрассчитана в адаптере) — ничего не делаем
    if "first_upload_dt" in df_target.columns and df_target["first_upload_dt"].notna().any():
        return df_target
    if df_full is None or df_full.empty:
        df_target = df_target.copy()
        df_target["first_upload_dt"] = df_target["upload_dt"]
        return df_target

    df_full = _ensure_dt(df_full, "upload_dt")
    first_upload_map = df_full.groupby("internal_order_cd")["upload_dt"].min().to_dict()
    first_upload_map = {
        order_cd: _roll_forward_to_allowed_snapshot_day(first_upload_dt)
        for order_cd, first_upload_dt in first_upload_map.items()
    }
    df_target = df_target.copy()
    df_target["first_upload_dt"] = df_target["internal_order_cd"].map(first_upload_map)
    df_target["first_upload_dt"] = df_target["first_upload_dt"].fillna(df_target["upload_dt"])
    return df_target


def analyze_discounting_impact(
        df_deals: pd.DataFrame,
        division_cd: str,
        date: str,
        prev_date: Optional[str],
) -> Dict[str, Any]:
    """Анализ влияния дисконтирования: сделки, которые были на обе даты,
    но delta_limit_amt изменился из-за домножения на коэффициент дисконтирования."""
    if df_deals is None or df_deals.empty or not prev_date:
        return {"status": "no_data", "discount_impact_total": 0.0, "significant_deals": []}

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    t_date = pd.to_datetime(date)
    p_date = pd.to_datetime(prev_date)

    df_t = df_deals[(df_deals["upload_dt"] == t_date) & (df_deals["division_cd"] == division_cd)].copy()
    df_p = df_deals[(df_deals["upload_dt"] == p_date) & (df_deals["division_cd"] == division_cd)].copy()

    if df_t.empty or df_p.empty:
        return {"status": "no_data", "discount_impact_total": 0.0, "significant_deals": []}

    # Ключ для матчинга сделок между датами
    key_cols = ['internal_order_cd', 'deal_dt', 'value_dt', 'inn_num']
    key_cols = [col for col in key_cols if col in df_t.columns and col in df_p.columns]

    if not key_cols:
        return {"status": "no_data", "discount_impact_total": 0.0, "significant_deals": []}

    df_t["deal_key"] = df_t[key_cols].fillna("").astype(str).agg("|".join, axis=1)
    df_p["deal_key"] = df_p[key_cols].fillna("").astype(str).agg("|".join, axis=1)

    df_t["delta_limit_amt"] = pd.to_numeric(df_t["delta_limit_amt"], errors="coerce").fillna(0.0)
    df_p["delta_limit_amt"] = pd.to_numeric(df_p["delta_limit_amt"], errors="coerce").fillna(0.0)

    has_top_up_flag = "top_up_option_flg" in df_t.columns and "top_up_option_flg" in df_p.columns
    if has_top_up_flag:
        df_t["top_up_option_flg"] = pd.to_numeric(df_t["top_up_option_flg"], errors="coerce").fillna(0).astype(int)
        df_p["top_up_option_flg"] = pd.to_numeric(df_p["top_up_option_flg"], errors="coerce").fillna(0).astype(int)

    # Сделки, присутствующие на обе даты в том же КПК (НЕ новые)
    common_keys = set(df_t["deal_key"]) & set(df_p["deal_key"])
    if not common_keys:
        return {"status": "no_common_deals", "discount_impact_total": 0.0, "significant_deals": []}

    # Строим маппинг: deal_key -> delta_limit_amt на t-1
    prev_map = df_p.set_index("deal_key")["delta_limit_amt"].to_dict()
    prev_top_up_map = (
        df_p.set_index("deal_key")["top_up_option_flg"].to_dict()
        if has_top_up_flag
        else {}
    )

    df_common = df_t[df_t["deal_key"].isin(common_keys)].copy()
    df_common["delta_limit_amt_prev"] = df_common["deal_key"].map(prev_map)
    if has_top_up_flag:
        df_common["top_up_flg_prev"] = df_common["deal_key"].map(prev_top_up_map)
        df_common = df_common[
            ~(
                    (df_common["top_up_flg_prev"] == 0)
                    & (df_common["top_up_option_flg"] == 1)
            )
        ].copy()

    if df_common.empty:
        return {"status": "analyzed", "discount_impact_total": 0.0, "significant_deals": []}

    df_common["discount_change"] = df_common["delta_limit_amt"] - df_common["delta_limit_amt_prev"]

    # Общее влияние дисконтирования
    discount_total = df_common["discount_change"].sum()

    # Порог: 20% от max(abs(sum_positive_changes), abs(sum_negative_changes))
    sum_pos = df_common.loc[df_common["discount_change"] > 0, "discount_change"].sum()
    sum_neg = df_common.loc[df_common["discount_change"] < 0, "discount_change"].sum()
    threshold_base = max(abs(sum_pos), abs(sum_neg))
    threshold = threshold_base * 0.20 if threshold_base > 0 else 0.0

    significant = df_common[df_common["discount_change"].abs() > threshold].copy()
    significant = significant.sort_values("discount_change", key=abs, ascending=False)

    significant_deals = []
    for _, row in significant.iterrows():
        significant_deals.append({
            "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "inn": row.get("inn_num", "Н/Д"),
            "original_delta_limit_amt": row.get("original_delta_limit_amt", "Н/Д"),
            "delta_limit_amt_prev": round(_safe_float(row.get("delta_limit_amt_prev")), 2),
            "delta_limit_amt_t": round(_safe_float(row.get("delta_limit_amt")), 2),
            "discount_change": round(_safe_float(row.get("discount_change")), 2),
        })

    return {
        "status": "analyzed",
        "discount_impact_total": discount_total,
        "significant_deals": significant_deals,
    }


def analyze_top_up_option_changes(
        df_deals: pd.DataFrame,
        division_cd: str,
        date: str,
        prev_date: Optional[str],
) -> Dict[str, Any]:
    """Выявляет сделки, у которых активировалась опция пополнения (top_up_option_flg: 0 → 1)
    при отрицательном влиянии на лимит (delta_limit_amt < 0)."""
    empty = {"status": "no_data", "top_up_total_impact": 0.0, "top_up_deals": []}
    if df_deals is None or df_deals.empty or not prev_date:
        return empty

    if "top_up_option_flg" not in df_deals.columns:
        return empty

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    t_date = pd.to_datetime(date)
    p_date = pd.to_datetime(prev_date)

    df_t = df_deals[(df_deals["upload_dt"] == t_date) & (df_deals["division_cd"] == division_cd)].copy()
    df_p = df_deals[(df_deals["upload_dt"] == p_date) & (df_deals["division_cd"] == division_cd)].copy()

    if df_t.empty or df_p.empty:
        return empty

    key_cols = ['internal_order_cd', 'deal_dt', 'value_dt', 'inn_num']
    key_cols = [col for col in key_cols if col in df_t.columns and col in df_p.columns]
    if not key_cols:
        return empty

    df_t["deal_key"] = df_t[key_cols].fillna("").astype(str).agg("|".join, axis=1)
    df_p["deal_key"] = df_p[key_cols].fillna("").astype(str).agg("|".join, axis=1)

    df_t["delta_limit_amt"] = pd.to_numeric(df_t["delta_limit_amt"], errors="coerce").fillna(0.0)
    df_p["delta_limit_amt"] = pd.to_numeric(df_p["delta_limit_amt"], errors="coerce").fillna(0.0)
    df_t["top_up_option_flg"] = pd.to_numeric(df_t["top_up_option_flg"], errors="coerce").fillna(0).astype(int)
    df_p["top_up_option_flg"] = pd.to_numeric(df_p["top_up_option_flg"], errors="coerce").fillna(0).astype(int)

    common_keys = set(df_t["deal_key"]) & set(df_p["deal_key"])
    if not common_keys:
        return empty

    prev_flg_map = df_p.set_index("deal_key")["top_up_option_flg"].to_dict()
    prev_delta_map = df_p.set_index("deal_key")["delta_limit_amt"].to_dict()

    df_common = df_t[df_t["deal_key"].isin(common_keys)].copy()
    df_common["top_up_flg_prev"] = df_common["deal_key"].map(prev_flg_map)
    df_common["delta_limit_amt_prev"] = df_common["deal_key"].map(prev_delta_map)

    # Фильтр: флаг был 0, стал 1 И delta_limit_amt < 0
    mask = (
            (df_common["top_up_flg_prev"] == 0)
            & (df_common["top_up_option_flg"] == 1)
            & (df_common["delta_limit_amt"] < 0)
    )
    matched = df_common[mask]

    if matched.empty:
        return {"status": "analyzed", "top_up_total_impact": 0.0, "top_up_deals": []}

    matched = matched.copy()
    matched["delta_limit_change"] = matched["delta_limit_amt"] - matched["delta_limit_amt_prev"]
    top_up_total = matched["delta_limit_change"].sum()

    top_up_deals = []
    for _, row in matched.iterrows():
        top_up_deals.append({
            "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "inn": row.get("inn_num", "Н/Д"),
            "delta_limit_amt_prev": round(_safe_float(row.get("delta_limit_amt_prev")), 2),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "delta_limit_change": round(_safe_float(row.get("delta_limit_change")), 2),
            "original_delta_limit_amt": row.get("original_delta_limit_amt", "Н/Д"),
            "value_dt": _fmt_date(row.get("value_dt")),
            "deal_dt": _fmt_date(row.get("deal_dt")),
        })

    return {
        "status": "analyzed",
        "top_up_total_impact": top_up_total,
        "top_up_deals": top_up_deals,
    }


def _get_calc_log_entries(
        df_calc_logs: pd.DataFrame,
        internal_order_cd: str,
        statuses: Optional[set] = None,
) -> pd.DataFrame:
    """Возвращает строки логов cons_calc для сделки (опционально фильтрует по статусам)."""
    if df_calc_logs is None or df_calc_logs.empty:
        return pd.DataFrame()
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd",))
    internal_order_cd = _normalize_id_value(internal_order_cd)
    if not internal_order_cd:
        return pd.DataFrame(columns=df_calc_logs.columns)
    mask = df_calc_logs["internal_order_cd"] == internal_order_cd
    sub = df_calc_logs[mask]
    if statuses:
        sub = sub[sub["status_cd"].isin(statuses)]
    return sub


def _enrich_deal_with_calc_log(
        order_cd: str,
        value_dt,
        df_calc_logs: pd.DataFrame,
) -> Dict[str, Any]:
    """Проверяет сделку по логам cons_calc и возвращает доп. поля для обогащения.

    Проверяет статусы: BreachDealClosed (лимит вернётся) и EarlyTerminated
    (досрочное закрытие, лимит продолжает влиять).
    """
    info: Dict[str, Any] = {"calc_log_status": None, "calc_log_note": None}
    if df_calc_logs is None or df_calc_logs.empty or not order_cd or order_cd == "nan":
        return info

    # BreachDealClosed
    nc_logs = _get_calc_log_entries(df_calc_logs, order_cd, statuses={"BreachDealClosed"})
    if not nc_logs.empty:
        info["calc_log_status"] = "BreachDealClosed"
        note = "несоблюдение условий — лимит вернется на утро 5 рабочего дня после value_dt"
        if value_dt is not None:
            try:
                return_dt = _resolve_status_effective_date(value_dt, NON_COMPLIANCE_RETURN_DAYS)
                if return_dt is None:
                    raise ValueError("return_dt is None")
                return_date_str = return_dt.strftime("%d.%m.%Y")
                note = f"несоблюдение условий — лимит вернется на утро {return_date_str}"
                info["return_date"] = return_date_str
            except Exception:
                pass
        info["calc_log_note"] = note
        return info

    # EarlyTerminated
    te_logs = _get_calc_log_entries(df_calc_logs, order_cd, statuses={"EarlyTerminated"})
    if not te_logs.empty:
        info["calc_log_status"] = "EarlyTerminated"
        info["calc_log_note"] = "сделка закрыта досрочно, но продолжает влиять на лимит"
        return info

    return info


def check_non_compliance_deals(
        sub_df: pd.DataFrame,
        df_calc_logs: pd.DataFrame,
        exclude_order_cds: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Проверяет все сделки на дату по логам: возвращает список сделок со статусом BreachDealClosed.

    exclude_order_cds — сделки, уже показанные в top_negative_deals (чтобы не дублировать).
    """
    if sub_df is None or sub_df.empty or df_calc_logs is None or df_calc_logs.empty:
        return []

    exclude = exclude_order_cds or set()
    result = []
    for _, row in sub_df.iterrows():
        order_cd = str(row.get("internal_order_cd", ""))
        if not order_cd or order_cd == "nan" or order_cd in exclude:
            continue
        logs = _get_calc_log_entries(df_calc_logs, order_cd, statuses={"BreachDealClosed"})
        if logs.empty:
            continue

        value_dt = row.get("value_dt")
        return_date_str = None
        note = "лимит вернется на утро 5 рабочего дня после value_dt"
        if value_dt is not None:
            try:
                return_dt = _resolve_status_effective_date(value_dt, NON_COMPLIANCE_RETURN_DAYS)
                if return_dt is None:
                    raise ValueError("return_dt is None")
                return_date_str = return_dt.strftime("%d.%m.%Y")
                note = f"лимит вернется на утро {return_date_str}"
            except Exception:
                pass

        result.append({
            "internal_order_cd": order_cd,
            "value_dt": _fmt_date(value_dt),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "return_date": return_date_str,
            "note": note,
        })

    return result


def find_disappeared_deals_with_logs(
        df_deals: pd.DataFrame,
        division_cd: str,
        date: str,
        prev_date: Optional[str],
        df_calc_logs: pd.DataFrame,
) -> List[Dict[str, Any]]:
    """Находит сделки, пропавшие из df_deals между prev_date и date для данного КПК,
    и обогащает их статусами из расчётных логов.

    При исчезновении сделки её original_delta_limit_amt возвращается в лимит.
    Возвращает только сделки, у которых есть лог с одним из статусов:
    BreachDealClosed, ClientDeclined, BankDeclined.
    """
    if df_deals is None or df_deals.empty or not prev_date:
        return []

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    t_date = pd.to_datetime(date)
    p_date = pd.to_datetime(prev_date)

    df_t = df_deals[(df_deals["upload_dt"] == t_date) & (df_deals["division_cd"] == division_cd)]
    df_p = df_deals[(df_deals["upload_dt"] == p_date) & (df_deals["division_cd"] == division_cd)]

    if df_p.empty:
        return []

    present_today = set(df_t["internal_order_cd"].dropna().astype(str))
    df_p = df_p[df_p["internal_order_cd"].notna()].copy()
    df_p["internal_order_cd"] = df_p["internal_order_cd"].astype(str)
    disappeared = df_p[~df_p["internal_order_cd"].isin(present_today)]

    if disappeared.empty:
        return []

    result = []
    for _, row in disappeared.iterrows():
        order_cd = str(row["internal_order_cd"])
        logs = _get_calc_log_entries(df_calc_logs, order_cd, statuses=_DISAPPEARED_LOG_STATUSES)
        if logs.empty:
            continue

        log_cols = [c for c in ["status_cd", "calculation_dttm", "inn_num", "value_dt"] if c in logs.columns]
        log_entries = logs[log_cols].to_dict(orient="records")
        for le in log_entries:
            if "calculation_dttm" in le:
                le["calculation_dttm"] = _fmt_datetime(le["calculation_dttm"])
            if "value_dt" in le:
                le["value_dt"] = _fmt_date(le["value_dt"])

        result.append({
            "internal_order_cd": order_cd,
            "value_dt": _fmt_date(row.get("value_dt")),
            "original_delta_limit_amt": row.get("original_delta_limit_amt"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "inn_num": row.get("inn_num", "Н/Д"),
            "log_entries": log_entries,
        })

    return result


def analyze_single_kpk(
        *,
        df_lim: pd.DataFrame,
        df_trx: pd.DataFrame,
        df_deals: pd.DataFrame,
        df_calc_logs: Optional[pd.DataFrame] = None,
        division_cd: str,
        date: Optional[str] = None,
        date_from: Optional[str] = None,
        delta_hint: Optional[float] = None,
        include_details: bool = False,
        tol_abs: float = 1.0,
        tol_rel: float = 0.05,
        limit_cols: Sequence[str] = DEFAULT_LIMIT_COLS,
        today_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Анализ по одному КПК на дату с фолбэком на df_deals."""

    log.info(
        "[kpk_analysis] analyze_single_kpk start division_cd=%s date=%s date_from=%s delta_hint=%s include_details=%s today_override=%s",
        division_cd,
        date,
        date_from,
        delta_hint,
        include_details,
        today_override,
    )
    division_cd = _normalize_id_value(division_cd)
    if not division_cd:
        log.warning("[kpk_analysis] analyze_single_kpk missing division_cd")
        return {"status": "error", "error": "division_cd is required for single mode"}

    df_lim = _ensure_dt(df_lim, "report_dt")
    if df_lim is not None and "upload_dt" in df_lim.columns:
        df_lim = _ensure_dt(df_lim, "upload_dt")
    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_lim = _normalize_id_columns(df_lim, ("division_cd",))
    df_trx = _normalize_trx_for_analysis(df_trx, target_division_cd=division_cd)
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd", "inn_num"))
    _log_df_state(
        "single limits normalized",
        df_lim,
        date_cols=["report_dt", "calc_limit_dt"],
        id_cols=["division_cd"],
        numeric_cols=["limit_amt"],
    )
    _log_df_state(
        "single transactions normalized",
        df_trx,
        date_cols=["report_dt", "transaction_dttz"],
        id_cols=["division_cd", "division_from_cd", "division_to_cd", "author_nm"],
        numeric_cols=["transaction_limit_amt"],
    )
    _log_df_state(
        "single deals normalized",
        df_deals,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )
    _log_df_state(
        "single calculation logs normalized",
        df_calc_logs,
        date_cols=["calculation_dttm", "value_dt"],
        id_cols=["internal_order_cd", "inn_num", "status_cd"],
        numeric_cols=["delta_limit_amt"],
    )


    date = _resolve_snapshot_date(df_lim, date, today_override=today_override)
    prev_date = (
        get_prev_date(df_lim, date, dt_column="upload_dt")
        if not date_from else str(pd.to_datetime(date_from).date())
    )
    business_date = _snapshot_to_business_date(date)
    prev_business_date = _snapshot_to_business_date(prev_date)
    log.info(
        "[kpk_analysis] analyze_single_kpk resolved dates division_cd=%s snapshot_date=%s prev_snapshot_date=%s business_date=%s prev_business_date=%s",
        division_cd,
        date,
        prev_date,
        business_date,
        prev_business_date,
    )

    # Для сделок используем snapshot_date, для перераспределений — business_date.
    deal_date = date
    deal_date_from = prev_date
    log.info(
        "[kpk_analysis] analyze_single_kpk deal snapshot dates division_cd=%s deal_date=%s deal_date_from=%s",
        division_cd,
        deal_date,
        deal_date_from,
    )
    df_deals_full = df_deals.copy()
    df_deals = filter_new_deals(df_deals, deal_date, date_from=deal_date_from, division_cd=division_cd)
    _log_df_state(
        "single new deals filtered",
        df_deals,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )

    # 1. Анализ перераспределений
    author_series = _string_series(df_trx, "author_nm")
    trx_date_mask = df_trx["report_dt"] == pd.to_datetime(business_date) if business_date else pd.Series(False, index=df_trx.index)
    if date_from and prev_business_date:
        trx_date_mask = (
                (df_trx["report_dt"] >= pd.to_datetime(prev_business_date))
                & (df_trx["report_dt"] <= pd.to_datetime(business_date))
        )
    trx_nonzero_mask = pd.to_numeric(df_trx["transaction_limit_amt"], errors="coerce") != 0
    trx_sub = df_trx[
        trx_date_mask
        & (df_trx["division_cd"] == division_cd)
        & (~author_series.isin(["", "AUTO"]))
        & trx_nonzero_mask
        ]

    redistribution_by_author = []
    dist_amt = 0.0
    if not trx_sub.empty:
        trx_sub = trx_sub.copy()
        trx_sub["transaction_limit_amt"] = pd.to_numeric(
            trx_sub["transaction_limit_amt"], errors="coerce"
        ).fillna(0.0).round(2)
        redistribution_by_author = trx_sub.to_dict(orient="records")
        dist_amt = float(trx_sub["transaction_limit_amt"].sum())
    log.info(
        "[kpk_analysis] analyze_single_kpk redistribution summary division_cd=%s date=%s rows=%s total=%s",
        division_cd,
        date,
        len(trx_sub),
        dist_amt,
    )

    # Аннотируем перераспределения: компенсация от ЦА
    for rec in redistribution_by_author:
        seg = _check_ca_compensation(rec.get("author_nm", ""))
        rec["is_ca_compensation"] = seg is not None
        rec["ca_segment"] = seg

    # Получаем общую дельту
    diffs, _, _ = get_diff_limit_between_days(
        df_lim,
        [division_cd],
        date=date,
        date_from=date_from,
        dt_column="upload_dt",
    )
    _log_df_state(
        "single limit diffs",
        diffs,
        id_cols=["division_cd"],
        numeric_cols=["dif_lim", "limit_t", "limit_prev"],
    )
    if diffs.empty:
        log.warning(
            "[kpk_analysis] analyze_single_kpk empty limit diffs; payload will contain delta=0 and null limits division_cd=%s date=%s prev_date=%s",
            division_cd,
            date,
            prev_date,
        )
    delta = float(diffs["dif_lim"].iloc[0]) if (not diffs.empty and pd.notna(diffs["dif_lim"].iloc[0])) else 0.0
    limit_t = float(diffs["limit_t"].iloc[0]) if not diffs.empty else None
    limit_prev = float(diffs["limit_prev"].iloc[0]) if not diffs.empty else None
    log.info(
        "[kpk_analysis] analyze_single_kpk limit summary division_cd=%s limit_t=%s limit_prev=%s delta=%s",
        division_cd,
        limit_t,
        limit_prev,
        delta,
    )

    # Анализ сделок
    deals_analysis = analyze_deals_for_kpk(
        df_deals, division_cd, deal_date,
        delta_limit_prev_day=delta,
        include_details=include_details,
        df_calc_logs=df_calc_logs,
        df_deals_full=df_deals_full,
    )

    # Анализ дисконтирования
    discounting_analysis = analyze_discounting_impact(df_deals_full, division_cd, deal_date, deal_date_from)

    # Анализ активации опции пополнения
    top_up_analysis = analyze_top_up_option_changes(df_deals_full, division_cd, deal_date, deal_date_from)

    # Анализ пропавших сделок
    disappeared_deals = find_disappeared_deals_with_logs(
        df_deals_full, division_cd, deal_date, deal_date_from, df_calc_logs
    )
    log.info(
        "[kpk_analysis] analyze_single_kpk component summary division_cd=%s summary=%s",
        division_cd,
        {
            "deals_status": deals_analysis.get("status") if isinstance(deals_analysis, dict) else None,
            "deals_impact_total": deals_analysis.get("deals_impact_total") if isinstance(deals_analysis, dict) else None,
            "discounting_status": discounting_analysis.get("status") if isinstance(discounting_analysis, dict) else None,
            "discount_impact_total": discounting_analysis.get("discount_impact_total") if isinstance(discounting_analysis, dict) else None,
            "top_up_status": top_up_analysis.get("status") if isinstance(top_up_analysis, dict) else None,
            "top_up_total_impact": top_up_analysis.get("top_up_total_impact") if isinstance(top_up_analysis, dict) else None,
            "disappeared_count": len(disappeared_deals),
        },
    )

    # Собираем payload
    payload = {
        "mode": "single",
        "division_cd": division_cd,
        "report_dt": date,
        "prev_report_dt": prev_date,
        "limit_amt": limit_t,
        "prev_limit_amt": limit_prev,
        "delta_limit_amt": delta,
        "redistribution_total": dist_amt,
        "redistribution_by_author": redistribution_by_author,
        "deals_analysis": deals_analysis,
        "discounting_analysis": discounting_analysis,
        "top_up_option_analysis": top_up_analysis,
        "disappeared_deals_analysis": disappeared_deals,
    }
    log.info(
        "[kpk_analysis] analyze_single_kpk done division_cd=%s payload_summary=%s",
        division_cd,
        {
            "report_dt": payload["report_dt"],
            "prev_report_dt": payload["prev_report_dt"],
            "limit_amt": payload["limit_amt"],
            "prev_limit_amt": payload["prev_limit_amt"],
            "delta_limit_amt": payload["delta_limit_amt"],
            "redistribution_total": payload["redistribution_total"],
            "deals_status": deals_analysis.get("status") if isinstance(deals_analysis, dict) else None,
        },
    )

    return {"status": "success", "data": sanitize_json(payload)}


def analyze_negative_report(
        *,
        df_lim: pd.DataFrame,
        df_trx: pd.DataFrame,
        df_deals: pd.DataFrame,
        df_calc_logs: Optional[pd.DataFrame] = None,
        date: Optional[str] = None,
        include_details: bool = False,
        tol_abs: float = 1.0,
        tol_rel: float = 0.05,
        today_override: Optional[str] = None,
) -> Dict[str, Any]:

    df_lim = _ensure_dt(df_lim, "report_dt")
    if df_lim is not None and "upload_dt" in df_lim.columns:
        df_lim = _ensure_dt(df_lim, "upload_dt")
    df_lim = _normalize_id_columns(df_lim, ("division_cd",))
    df_trx = _normalize_trx_for_analysis(df_trx)
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd", "inn_num"))
    date = _resolve_snapshot_date(df_lim, date, today_override=today_override)
    target_date = pd.to_datetime(date)
    business_date = _snapshot_to_business_date(date)

    # 1. Получаем список всех КПК, по которым есть данные на текущую дату
    all_kpks = df_lim.loc[df_lim["upload_dt"] == target_date, "division_cd"].dropna().unique().tolist()

    if not all_kpks:
        return {"status": "success", "data": {"mode": "all", "report_dt": date, "negative_count": 0, "rows": []}}

    # 2. Считаем дельту и лимиты для ВСЕХ КПК через get_diff_limit_between_days
    diffs, prev_date, _ = get_diff_limit_between_days(df_lim, all_kpks, date=date, dt_column="upload_dt")
    prev_business_date = _snapshot_to_business_date(prev_date)

    # Фильтр: отрицательные лимиты / большое падение / аномальный рост (>15% от модуля лимита прошлого дня)
    is_negative = (diffs["limit_t"] < 0) | (diffs["dif_lim"] < -10000)
    is_anomalous_positive = (
            diffs["limit_prev"].notna()
            & (diffs["limit_prev"].abs() > 0)
            & (diffs["dif_lim"] > 0.15 * diffs["limit_prev"].abs())
    )
    flagged_diffs = diffs[is_negative | is_anomalous_positive].copy()
    flagged_diffs["anomaly_type"] = "negative"
    flagged_diffs.loc[is_anomalous_positive & ~is_negative, "anomaly_type"] = "anomalous_positive"

    # Если проблемных КПК нет — возвращаем пустой отчет, не фильтруя сделки
    if flagged_diffs.empty:
        return {
            "status": "success",
            "data": {"mode": "all", "report_dt": date, "prev_report_dt": prev_date, "negative_count": 0, "rows": []}
        }

    # 4. Фильтруем сделки только теперь, когда мы уверены, что есть что анализировать
    deal_date = date
    deal_date_from = prev_date
    df_deals_full = df_deals.copy()  # сохраняем полный df для дисконтирования
    df_deals = filter_new_deals(df_deals, date)

    rows = []
    # Запускаем цикл анализа по каждому отфильтрованному КПК,
    # итерируясь прямо по датафрейму с посчитанными дельтами
    for _, kpk_row in flagged_diffs.iterrows():
        kpk = str(kpk_row["division_cd"])
        delta = float(kpk_row["dif_lim"]) if pd.notna(kpk_row["dif_lim"]) else 0.0
        limit_t = float(kpk_row["limit_t"]) if pd.notna(kpk_row["limit_t"]) else 0.0
        limit_prev = float(kpk_row["limit_prev"]) if pd.notna(kpk_row["limit_prev"]) else 0.0

        # 5. Перераспределения по КМ
        author_series = _string_series(df_trx, "author_nm")
        trx_sub = df_trx[
            (df_trx["report_dt"] == pd.to_datetime(business_date))
            & (df_trx["division_cd"] == kpk)
            & (~author_series.isin(["", "AUTO"]))
            & (pd.to_numeric(df_trx["transaction_limit_amt"], errors="coerce") != 0)
            ]

        redistribution_by_author = []
        dist_amt = 0.0
        if not trx_sub.empty:
            trx_sub = trx_sub.copy()
            trx_sub["transaction_limit_amt"] = pd.to_numeric(
                trx_sub["transaction_limit_amt"], errors="coerce"
            ).fillna(0.0).round(2)
            redistribution_by_author = trx_sub.to_dict(orient="records")
            dist_amt = float(trx_sub["transaction_limit_amt"].sum())

        # Аннотируем перераспределения: компенсация от ЦА
        for rec in redistribution_by_author:
            seg = _check_ca_compensation(rec.get("author_nm", ""))
            rec["is_ca_compensation"] = seg is not None
            rec["ca_segment"] = seg

        # 6. Сделки (топ 90%)
        deals_analysis = analyze_deals_for_kpk(
            df_deals, kpk, deal_date,
            delta_limit_prev_day=delta,
            include_details=include_details,
            df_calc_logs=df_calc_logs,
            df_deals_full=df_deals_full,
        )

        # 7. Определяем главную причину
        decision = _decision_from_delta_and_dist(delta, dist_amt, tol_abs=tol_abs, tol_rel=tol_rel)

        # 8. Дисконтирование
        discounting_analysis = analyze_discounting_impact(df_deals_full, kpk, deal_date, deal_date_from)

        # 9. Активация опции пополнения
        top_up_analysis = analyze_top_up_option_changes(df_deals_full, kpk, deal_date, deal_date_from)

        # 10. Пропавшие сделки
        disappeared_deals = find_disappeared_deals_with_logs(
            df_deals_full, kpk, deal_date, deal_date_from, df_calc_logs
        )

        # Собираем данные по конкретному КПК
        rows.append({
            "division_cd": kpk,
            "limit_amt": limit_t,
            "prev_limit_amt": limit_prev,
            "delta_limit_amt": delta,
            "redistribution_total": dist_amt,
            "redistribution_by_author": redistribution_by_author,
            "deals_analysis": deals_analysis,
            "discounting_analysis": discounting_analysis,
            "top_up_option_analysis": top_up_analysis,
            "disappeared_deals_analysis": disappeared_deals,
            "decision": decision,
            "anomaly_type": kpk_row.get("anomaly_type", "negative"),
        })

    payload = {
        "mode": "all",
        "report_dt": date,
        "prev_report_dt": prev_date,
        "negative_count": len(rows),
        "rows": rows,
    }

    return {"status": "success", "data": sanitize_json(payload)}


def analyze_new_deals_report(
        *,
        df_lim: pd.DataFrame,
        df_trx: pd.DataFrame,
        df_deals: pd.DataFrame,
        df_calc_logs: Optional[pd.DataFrame] = None,
        date: Optional[str] = None,
        include_details: bool = False,
        today_override: Optional[str] = None,
) -> Dict[str, Any]:

    df_lim = _ensure_dt(df_lim, "report_dt")
    if df_lim is not None and "upload_dt" in df_lim.columns:
        df_lim = _ensure_dt(df_lim, "upload_dt")
    df_lim = _normalize_id_columns(df_lim, ("division_cd",))
    df_trx = _normalize_trx_for_analysis(df_trx)
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd", "inn_num"))
    date = _resolve_snapshot_date(df_lim, date, today_override=today_override)
    target_date = pd.to_datetime(date)

    all_kpks = df_lim.loc[df_lim["upload_dt"] == target_date, "division_cd"].dropna().unique().tolist()
    if not all_kpks:
        return {"status": "success", "data": {"mode": "new_deals_report", "report_dt": date, "rows": []}}

    diffs, prev_date, _ = get_diff_limit_between_days(df_lim, all_kpks, date=date, dt_column="upload_dt")
    diffs = _normalize_id_columns(diffs, ("division_cd",))

    df_deals_full = df_deals.copy()
    df_new_deals = filter_new_deals(df_deals, date)

    report_kpks = {
        _normalize_id_value(kpk)
        for kpk in df_new_deals["division_cd"].dropna().unique().tolist()
        if _normalize_id_value(kpk)
    }
    if "top_up_option_flg" in df_deals_full.columns:
        df_top_up_current = _ensure_dt(df_deals_full, "upload_dt")
        df_top_up_current = _normalize_id_columns(df_top_up_current, ("division_cd",))
        top_up_mask = (
                (df_top_up_current["upload_dt"] == target_date)
                & (pd.to_numeric(df_top_up_current["top_up_option_flg"], errors="coerce").fillna(0).astype(int) == 1)
        )
        report_kpks.update({
            _normalize_id_value(kpk)
            for kpk in df_top_up_current.loc[top_up_mask, "division_cd"].dropna().unique().tolist()
            if _normalize_id_value(kpk)
        })

    report_kpks = sorted(report_kpks)
    if not report_kpks:
        return {
            "status": "success",
            "data": {"mode": "new_deals_report", "report_dt": date, "prev_report_dt": prev_date, "rows": []},
        }
    diff_map = {
        _normalize_id_value(row.get("division_cd")): row
        for _, row in diffs.iterrows()
        if _normalize_id_value(row.get("division_cd"))
    }

    rows = []
    for kpk in report_kpks:
        diff_row = diff_map.get(kpk)
        delta = float(diff_row.get("dif_lim")) if diff_row is not None and pd.notna(diff_row.get("dif_lim")) else 0.0
        limit_t = float(diff_row.get("limit_t")) if diff_row is not None and pd.notna(diff_row.get("limit_t")) else 0.0
        limit_prev = (
            float(diff_row.get("limit_prev"))
            if diff_row is not None and pd.notna(diff_row.get("limit_prev"))
            else 0.0
        )

        if limit_t >= 0:
            continue

        deals_analysis = analyze_deals_for_kpk(
            df_new_deals,
            kpk,
            date,
            delta_limit_prev_day=delta,
            include_details=include_details,
            df_calc_logs=df_calc_logs,
            df_deals_full=df_deals_full,
        )
        if deals_analysis.get("status") == "no_deals_on_date":
            deals_analysis = {
                "status": "analyzed",
                "deals_impact_total": 0.0,
                "deals_impact_correct_pos": 0.0,
                "deals_impact_correct_neg": 0.0,
                "all_new_deals": [],
                "all_new_deals_count": 0,
                "top_negative_deals": [],
                "non_compliance_deals": [],
                "incorrect_deals": [],
                "incorrect_deals_count": 0,
                "deals_impact_incorrect": 0.0,
            }

        top_up_analysis = analyze_top_up_option_changes(df_deals_full, kpk, date, prev_date)
        has_new_deals = bool(deals_analysis.get("all_new_deals"))
        has_top_up_deals = bool((top_up_analysis or {}).get("top_up_deals"))
        if not has_new_deals and not has_top_up_deals:
            continue

        rows.append({
            "division_cd": kpk,
            "limit_amt": limit_t,
            "prev_limit_amt": limit_prev,
            "delta_limit_amt": delta,
            "is_negative_limit": bool(limit_t < 0),
            "deals_analysis": deals_analysis,
            "top_up_option_analysis": top_up_analysis,
        })

    payload = {
        "mode": "new_deals_report",
        "report_dt": date,
        "prev_report_dt": prev_date,
        "rows": rows,
    }

    return {"status": "success", "data": sanitize_json(payload)}


def find_deal_by_amount(
        *,
        df_deals: pd.DataFrame,
        division_cd: str,
        date: str,
        target_amount: float,
        tolerance: float = 0.05,
) -> Dict[str, Any]:
    """Поиск сделок с похожим влиянием на лимит (delta_limit_amt) с погрешностью 5%."""
    if df_deals is None or df_deals.empty:
        return {"status": "success", "data": {"mode": "find_deal", "deals": []}}

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    if not division_cd:
        return {"status": "error", "error": "division_cd is required for find_deal mode"}
    target_date = pd.to_datetime(date)
    sub = df_deals[df_deals["division_cd"] == division_cd].copy()
    if sub.empty:
        return {"status": "success", "data": {"mode": "find_deal", "deals": []}}

    exact = sub[sub["upload_dt"] == target_date].copy()
    if exact.empty:
        historic = sub[sub["upload_dt"] <= target_date].copy()
        if not historic.empty:
            fallback_dt = historic["upload_dt"].max()
            sub = historic[historic["upload_dt"] == fallback_dt].copy()
        else:
            fallback_dt = sub["upload_dt"].max()
            sub = sub[sub["upload_dt"] == fallback_dt].copy()
    else:
        sub = exact

    if sub.empty:
        return {"status": "success", "data": {"mode": "find_deal", "deals": []}}

    sub["delta_limit_amt"] = pd.to_numeric(sub["delta_limit_amt"], errors="coerce").fillna(0.0)

    abs_tol = max(abs(target_amount) * tolerance, 10.0)
    matches = sub[
        (sub["delta_limit_amt"] >= target_amount - abs_tol)
        & (sub["delta_limit_amt"] <= target_amount + abs_tol)
        ].copy()

    if matches.empty:
        return {"status": "success", "data": {"mode": "find_deal", "deals": []}}
    matches["amount_distance"] = (matches["delta_limit_amt"] - target_amount).abs()
    matches = _stable_sort(matches, ["amount_distance", "upload_dt", "internal_order_cd"], ascending=[True, False, True])
    if "internal_order_cd" in matches.columns:
        matches = matches.drop_duplicates(["internal_order_cd"], keep="first")

    result_deals = []
    for _, row in matches.iterrows():
        result_deals.append({
            "internal_order_cd": row.get("internal_order_cd", "Н/Д"),
            "product_cd": row.get("product_cd", "Н/Д"),
            "source_system_cd": row.get("source_system_cd", "Н/Д"),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "inn": row.get("inn_num", "Н/Д"),
            "value_dt": _fmt_date(row.get("value_dt")),
            "deal_dt": _fmt_date(row.get("deal_dt")),
        })

    return {"status": "success", "data": sanitize_json({"mode": "find_deal", "deals": result_deals})}


def investigate_deal(
        *,
        df_deals: pd.DataFrame,
        df_calc_logs: pd.DataFrame,
        inn_num: str,
        value_dt: str,
        as_of_dt: Optional[str] = None,
        delta_amt: Optional[float] = None,
        division_cd: Optional[str] = None,
        lookback_days: int = 0,
) -> Dict[str, Any]:
    """Расследование сделки: почему менеджер не видит влияния на лимит.

    Шаг 1: проверка логов на статусы отмены/несоответствия.
    Шаг 2: поиск в таблице сделок (последний upload_dt).
    Шаг 3: если в логах без плохого статуса — объяснение по classify.

    Parameters
    ----------
    lookback_days : int
        Если > 0 — поиск сделки ведётся в окне [value_dt - lookback_days, value_dt].
        Если == 0 — поиск сделки ведётся в окне ±1 день от value_dt (допуск на ошибку КМ).
    as_of_dt : str | None
        Дата, на которую КМ смотрит результат. Логи статусов ищутся в окне
        [deal_dt_from, as_of_dt], чтобы находить события после value_dt.
    """
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    inn_num = _normalize_id_value(inn_num) or str(inn_num)
    target_vdt = pd.to_datetime(value_dt).normalize()
    target_as_of = pd.to_datetime(as_of_dt).normalize() if as_of_dt else None
    if target_as_of is None:
        candidate_dates: List[pd.Timestamp] = []
        if df_calc_logs is not None and not df_calc_logs.empty:
            logs_probe = df_calc_logs.copy()
            if "calculation_dttm" in logs_probe.columns:
                calc_dates = pd.to_datetime(logs_probe["calculation_dttm"], errors="coerce").dropna()
                if not calc_dates.empty:
                    candidate_dates.append(calc_dates.max().normalize())
            if "value_dt" in logs_probe.columns:
                log_value_dates = pd.to_datetime(logs_probe["value_dt"], errors="coerce").dropna()
                if not log_value_dates.empty:
                    candidate_dates.append(log_value_dates.max().normalize())
        if df_deals is not None and not df_deals.empty and "upload_dt" in df_deals.columns:
            deal_upload_dates = pd.to_datetime(df_deals["upload_dt"], errors="coerce").dropna()
            if not deal_upload_dates.empty:
                candidate_dates.append(deal_upload_dates.max().normalize())
        target_as_of = max(candidate_dates) if candidate_dates else target_vdt
    target_as_of = max(target_vdt, target_as_of)
    if lookback_days > 0:
        deal_dt_from = target_vdt - pd.Timedelta(days=lookback_days)
        deal_dt_to = target_vdt
    else:
        # ±1 день — допуск на ошибку КМ в дате
        deal_dt_from = target_vdt - pd.Timedelta(days=1)
        deal_dt_to = target_vdt + pd.Timedelta(days=1)
    log_dt_from = deal_dt_from
    log_dt_to = target_as_of

    log_findings: List[Dict[str, Any]] = []
    deal_matches: List[Dict[str, Any]] = []
    pending_explanation: Optional[str] = None
    verdict = "not_found"

    # ── Шаг 1: логи с "плохими" статусами ──
    if df_calc_logs is not None and not df_calc_logs.empty:
        logs = df_calc_logs.copy()
        logs["inn_num"] = logs["inn_num"].astype(str)
        logs["calculation_dttm"] = pd.to_datetime(logs["calculation_dttm"], errors="coerce")
        logs["calculation_day"] = logs["calculation_dttm"].dt.normalize()
        if "value_dt" in logs.columns:
            logs["log_value_day"] = pd.to_datetime(logs["value_dt"], errors="coerce").dt.normalize()
        else:
            logs["log_value_day"] = logs["calculation_dttm"].dt.normalize()
        mask = (
                (logs["inn_num"] == str(inn_num))
                & (logs["log_value_day"] >= deal_dt_from)
                & (logs["log_value_day"] <= deal_dt_to)
                & (logs["calculation_day"] <= log_dt_to)
        )
        bad_mask = mask & logs["status_cd"].isin(_STATUS_IMPACT_DAYS.keys())
        if delta_amt is not None and "delta_limit_amt" in logs.columns:
            logs["delta_limit_amt_num"] = pd.to_numeric(logs["delta_limit_amt"], errors="coerce")
            abs_tol = max(abs(delta_amt) * 0.05, 10.0)
            delta_match_mask = logs["delta_limit_amt_num"].between(delta_amt - abs_tol, delta_amt + abs_tol)
            bad = _sort_calc_logs(logs[bad_mask & delta_match_mask], ascending=False)
            if bad.empty:
                # В статусных логах delta_limit_amt может отсутствовать или не совпадать с витриной.
                # В этом случае не отбрасываем статус целиком, а используем delta_amt только как мягкое уточнение.
                bad = _sort_calc_logs(logs[bad_mask], ascending=False)
        else:
            bad = _sort_calc_logs(logs[bad_mask], ascending=False)

        for _, row in bad.iterrows():
            status = row["status_cd"]
            days = _STATUS_IMPACT_DAYS[status]
            note = None
            if days < 0:
                # EarlyTerminated — влияние продолжается до плановой даты погашения
                note = "досрочное закрытие — влияние продолжается"
            elif days == 0:
                note = "влияние прекратилось сразу"
            elif days > 0:
                if days == 1:
                    note = "со следующего дня"
                else:
                    note = f"через {days} раб. дн."
            log_findings.append({
                "internal_order_cd": str(row.get("internal_order_cd", "Н/Д")),
                "status_cd": status,
                "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
                "calculation_dttm": _fmt_datetime(row.get("calculation_dttm")),
                "value_dt": _fmt_date(row.get("value_dt")),
                "impact_end_note": note,
            })

        if log_findings:
            verdict = "stopped_by_status"

    # ── Шаг 2: поиск в таблице сделок (последний upload_dt) ──
    if df_deals is not None and not df_deals.empty:
        df_d = _ensure_dt(df_deals, "upload_dt")
        if "value_dt" in df_d.columns:
            df_d["value_dt"] = pd.to_datetime(df_d["value_dt"], errors="coerce")
        if "deal_dt" in df_d.columns:
            df_d["deal_dt"] = pd.to_datetime(df_d["deal_dt"], errors="coerce")
        df_d = _stable_sort(df_d, ["internal_order_cd", "upload_dt", "value_dt", "deal_dt"])
        if "internal_order_cd" in df_d.columns:
            sub = df_d.drop_duplicates(["internal_order_cd"], keep="last").copy()
        else:
            sub = df_d.copy()
        sub["inn_num"] = sub["inn_num"].astype(str)

        mask = (
                (sub["inn_num"] == str(inn_num))
                & (sub["value_dt"] >= deal_dt_from)
                & (sub["value_dt"] <= deal_dt_to)
        )
        if division_cd:
            sub["division_cd"] = sub["division_cd"].astype(str)
            mask = mask & (sub["division_cd"] == str(division_cd))
        if delta_amt is not None:
            sub["delta_limit_amt"] = pd.to_numeric(sub["delta_limit_amt"], errors="coerce").fillna(0.0)
            abs_tol = max(abs(delta_amt) * 0.05, 10.0)
            mask = mask & (sub["delta_limit_amt"] >= delta_amt - abs_tol) & (sub["delta_limit_amt"] <= delta_amt + abs_tol)

        found = sub[mask]
        if not found.empty:
            found = _enrich_first_upload_dt(found, df_deals)
        for _, row in found.iterrows():
            deal_status = _classify_deal(row)
            enrichment = _enrich_deal_with_calc_log(str(row.get("internal_order_cd", "")), row.get("value_dt"), df_calc_logs)
            deal_status_explanation = enrichment.get("calc_log_note") or _DEAL_STATUS_EXPLANATIONS.get(deal_status, "")
            deal_matches.append({
                "internal_order_cd": str(row.get("internal_order_cd", "Н/Д")),
                "division_cd": str(row.get("division_cd", "Н/Д")),
                "upload_dt": _fmt_date(row.get("upload_dt")),
                "value_dt": _fmt_date(row.get("value_dt")),
                "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
                "product_cd": str(row.get("product_cd", "Н/Д")),
                "source_system_cd": str(row.get("source_system_cd", "Н/Д")),
                "deal_dt": _fmt_date(row.get("deal_dt")),
                "deal_status": deal_status,
                "deal_status_explanation": deal_status_explanation,
            })

        if deal_matches and verdict != "stopped_by_status":
            verdict = "found_in_deals"

    # ── Шаг 3: логи без плохого статуса → pending_impact ──
    if verdict == "not_found" and df_calc_logs is not None and not df_calc_logs.empty:
        logs = df_calc_logs.copy()
        logs["inn_num"] = logs["inn_num"].astype(str)
        logs["calculation_dttm"] = pd.to_datetime(logs["calculation_dttm"], errors="coerce")
        logs["calculation_day"] = logs["calculation_dttm"].dt.normalize()
        if "value_dt" in logs.columns:
            logs["log_value_day"] = pd.to_datetime(logs["value_dt"], errors="coerce").dt.normalize()
        else:
            logs["log_value_day"] = logs["calculation_dttm"].dt.normalize()
        any_logs = _sort_calc_logs(
            logs[
                (logs["inn_num"] == str(inn_num))
                & (logs["log_value_day"] >= deal_dt_from)
                & (logs["log_value_day"] <= deal_dt_to)
                & (logs["calculation_day"] <= log_dt_to)
                ],
            ascending=False,
        )
        if delta_amt is not None and not any_logs.empty and "delta_limit_amt" in any_logs.columns:
            any_logs["delta_limit_amt_num"] = pd.to_numeric(any_logs["delta_limit_amt"], errors="coerce")
            abs_tol = max(abs(delta_amt) * 0.05, 10.0)
            narrowed_logs = any_logs[
                any_logs["delta_limit_amt_num"].between(delta_amt - abs_tol, delta_amt + abs_tol)
            ]
            if not narrowed_logs.empty:
                any_logs = narrowed_logs

        if not any_logs.empty:
            verdict = "pending_impact"
            row0 = any_logs.iloc[0]
            log_status = str(row0.get("status_cd", ""))
            log_order_cd = str(row0.get("internal_order_cd", ""))

            # Пытаемся определить тип по данным из лога
            pseudo_row = pd.Series({
                "value_dt": row0.get("value_dt"),
                "upload_dt": row0.get("calculation_dttm"),
                "source_system_cd": str(row0.get("source_system_cd", "")),
                "product_cd": str(row0.get("product_cd", "")),
                "group_id": str(row0.get("group_id", "")),
            })
            pending_explanation = (
                f"Сделка {log_order_cd} зарегистрирована в системе (статус: {log_status}). "
                "Ожидайте отражения в лимите. Сроки зависят от типа сделки: "
                "прокотированная КМ (DEPO) — на следующий рабочий день после даты валютирования; "
                "прокотированная КМ (НСО) — в тот же день; "
                "автокотировка / групповая сделка — на утро 5-го рабочего дня с даты валютирования."
            )

    return {
        "status": "success",
        "data": sanitize_json({
            "mode": "investigate_deal",
            "verdict": verdict,
            "inn_num": inn_num,
            "value_dt": value_dt,
            "as_of_dt": _fmt_date(target_as_of),
            "log_findings": log_findings,
            "deal_matches": deal_matches,
            "pending_explanation": pending_explanation,
        }),
    }


def analyze_client_history(
        *,
        df_deals: pd.DataFrame,
        df_calc_logs: Optional[pd.DataFrame],
        inn_num: str,
        division_cd: str,
        date: Optional[str],
        lookback_days: int = 7,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Анализ истории сделок клиента (ИНН) по КПК за последние N дней.

    Для каждой сделки берётся только первое появление (min upload_dt).
    Для каждого internal_order_cd из логов берётся последняя запись.
    """
    lookback_days = max(1, min(lookback_days, 31))
    end_date_str = date_to or date
    if not end_date_str:
        raise ValueError("date or date_to is required")

    explicit_range = bool(date_from)
    end_date = pd.to_datetime(end_date_str)
    start_date = pd.to_datetime(date_from) if explicit_range else end_date - pd.Timedelta(days=lookback_days)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    response_data = {
        "mode": "client_history",
        "inn_num": inn_num,
        "division_cd": division_cd,
        "date": end_date.strftime("%Y-%m-%d"),
        "lookback_days": lookback_days,
        "deals": [],
        "total_positive": 0.0,
        "total_negative": 0.0,
    }
    if explicit_range:
        response_data["date_from"] = start_date.strftime("%Y-%m-%d")
        response_data["date_to"] = end_date.strftime("%Y-%m-%d")

    empty_result = {
        "status": "success",
        "data": sanitize_json(response_data),
    }

    if df_deals is None or df_deals.empty:
        return empty_result

    df_deals = _ensure_dt(df_deals, "upload_dt")
    df_deals = _normalize_id_columns(df_deals, ("division_cd", "internal_order_cd", "inn_num"))
    df_calc_logs = _normalize_id_columns(df_calc_logs, ("internal_order_cd", "inn_num"))
    division_cd = _normalize_id_value(division_cd)
    inn_num = _normalize_id_value(inn_num) or str(inn_num)

    # Фильтр: ИНН + КПК + диапазон дат
    df_deals["inn_num"] = df_deals["inn_num"].astype(str)
    if "value_dt" in df_deals.columns:
        df_deals["value_dt"] = pd.to_datetime(df_deals["value_dt"], errors="coerce")
        value_window_mask = (df_deals["value_dt"] >= start_date) & (df_deals["value_dt"] <= end_date)
    else:
        value_window_mask = pd.Series(False, index=df_deals.index)
    upload_window_mask = (df_deals["upload_dt"] >= start_date) & (df_deals["upload_dt"] <= end_date)
    sub = df_deals[
        (df_deals["inn_num"] == str(inn_num))
        & (df_deals["division_cd"] == division_cd)
        & (upload_window_mask | value_window_mask)
        ].copy()

    if sub.empty:
        return empty_result

    sub["delta_limit_amt"] = pd.to_numeric(sub["delta_limit_amt"], errors="coerce").fillna(0.0)

    # Берём только первое появление каждой сделки, но детерминированно при дублях.
    sub = _stable_sort(
        sub,
        ["internal_order_cd", "upload_dt", "value_dt", "deal_dt", "delta_limit_amt", "product_cd", "source_system_cd"],
    )
    first_appearances = sub.drop_duplicates(["internal_order_cd"], keep="first").copy()
    first_appearances = _enrich_first_upload_dt(first_appearances, df_deals)
    first_appearances["deal_status"] = first_appearances.apply(_classify_deal, axis=1)

    # Обогащаем последним логом для каждого internal_order_cd
    def _get_last_log(order_cd: str) -> Optional[Dict[str, Any]]:
        if df_calc_logs is None or df_calc_logs.empty:
            return None
        logs = _get_calc_log_entries(df_calc_logs, order_cd)
        if logs.empty:
            return None
        logs = _ensure_dt(logs, "calculation_dttm")
        last = logs.sort_values("calculation_dttm", ascending=False).iloc[0]
        return {
            "status_cd": str(last.get("status_cd", "Н/Д")),
            "calculation_dttm": str(last.get("calculation_dttm", "Н/Д")),
        }

    total_pos = first_appearances.loc[first_appearances["delta_limit_amt"] > 0, "delta_limit_amt"].sum()
    total_neg = first_appearances.loc[first_appearances["delta_limit_amt"] < 0, "delta_limit_amt"].sum()

    deals_list = []
    for _, row in first_appearances.sort_values("upload_dt", ascending=False).iterrows():
        order_cd = str(row.get("internal_order_cd", ""))
        last_log = _get_last_log(order_cd)
        deals_list.append({
            "internal_order_cd": order_cd if order_cd else "Н/Д",
            "product_cd": row.get("product_cd", "Н/Д"),
            "source_system_cd": row.get("source_system_cd", "Н/Д"),
            "delta_limit_amt": round(_safe_float(row.get("delta_limit_amt")), 2),
            "value_dt": _fmt_date(row.get("value_dt")),
            "deal_dt": _fmt_date(row.get("deal_dt")),
            "maturity_dt": _fmt_date(row.get("maturity_dt")),
            "upload_dt": _fmt_date(row.get("upload_dt")),
            "deal_status": row.get("deal_status", "incorrect"),
            "last_log_status": last_log.get("status_cd") if last_log else None,
            "last_log_date": _fmt_datetime(last_log.get("calculation_dttm")) if last_log else None,
        })

    return {
        "status": "success",
        "data": sanitize_json({
            **response_data,
            "deals": deals_list,
            "total_positive": round(float(total_pos), 2),
            "total_negative": round(float(total_neg), 2),
        }),
    }
