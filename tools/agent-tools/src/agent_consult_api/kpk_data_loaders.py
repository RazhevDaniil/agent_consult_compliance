import re
import os
import logging
import datetime
import urllib3
import requests
import pandas as pd
import numpy as np
from typing import List, Optional
from requests.exceptions import RequestException

from .kpk_limit_analysis import get_diff_limit_between_days
from ..trino.trino_client import run_trino
from ..utils.logger_config import configure_logging


configure_logging()
log = logging.getLogger("---KPK Data Loaders---")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Table names ──────────────────────────────────────────────────────────────
_SCHEMA = "custom_fin_palm_dataops"
_CATALOG = "con_hadoop_foton"
T_LIM = f"{_SCHEMA}.lb_division_limit_fct"
T_TRX = f"{_SCHEMA}.lb_transaction_limit_division_csp_fct"
T_DEALS = f"{_SCHEMA}.lb_deal_depo_fct"
T_LOGS = f"{_SCHEMA}.lb_cons_calculation_fct"

# ── Column lists ─────────────────────────────────────────────────────────────
LIM_COLS = (
    "CAST(load_dttm AS DATE) as upload_dt, report_dt, calc_limit_dt, division_cd, limit_amt, "
    "ccy_cd, business_block_cd, parent_division_cd, parent_division_nm"
)
TRX_COLS = (
    "report_dt, created_dttm as transaction_dttz, transaction_limit_amt, "
    "division_from_cd, division_to_cd, business_block_cd, author_nm, author_tab_num"
)
DEALS_COLS = (
    "CAST(load_dttm AS DATE) as upload_dt, value_dt, division_cd, "
    "source_system_cd, product_cd, delta_limit_amt, original_delta_limit_amt, "
    "internal_order_cd, inn_num, deal_dt, maturity_dt, top_up_option_flg" #group_id
)
LOGS_COLS = (
    "pass_through_calc_id as internal_order_cd, status_cd, calculation_dttm, "
    "inn_num, value_dt, delta_limit_amt, customer_nm as source_system_cd, product_cd" #group_id
)

# ── Empty templates ──────────────────────────────────────────────────────────
_EMPTY_LIM = pd.DataFrame(columns=[
    "upload_dt", "report_dt", "calc_limit_dt", "division_cd", "limit_amt",
    "ccy_cd", "business_block_cd", "parent_division_cd", "parent_division_nm",
])
_EMPTY_TRX = pd.DataFrame(columns=[
    "report_dt", "transaction_dttz", "transaction_limit_amt",
    "division_from_cd", "division_to_cd", "business_block_cd", "author_nm", "author_tab_num",
])
_EMPTY_DEALS = pd.DataFrame(columns=[
    "upload_dt", "value_dt", "division_cd", "source_system_cd", "product_cd",
    "delta_limit_amt", "original_delta_limit_amt", "internal_order_cd",
    "inn_num", "deal_dt", "maturity_dt", "top_up_option_flg", #group_id
])
_EMPTY_LOGS = pd.DataFrame(columns=[
    "internal_order_cd", "status_cd", "calculation_dttm",
    "inn_num", "value_dt", "delta_limit_amt", "source_system_cd", "product_cd", #group_id
])

# ── Validation ───────────────────────────────────────────────────────────────
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_DIGITS = re.compile(r"^\d+$")

def _validate_date(val: str) -> str:
    if not _RE_DATE.match(val):
        raise ValueError(f"Invalid date format: {val!r}")
    return val

def _effective_today_str(today_override: Optional[str] = None) -> str:
    if today_override:
        try:
            return datetime.date.fromisoformat(today_override).isoformat()
        except ValueError:
            pass
    return datetime.date.today().isoformat()

def _validate_digits(val: str, name: str) -> str:
    if not _RE_DIGITS.match(val):
        raise ValueError(f"{name} must be digits only, got: {val!r}")
    return val

def _quote_str_list(values: list) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _normalize_id_value(val):
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (int, np.integer)):
        return str(int(val))
    if isinstance(val, (float, np.floating)):
        return str(int(val)) if float(val).is_integer() else str(val).rstrip("0").rstrip(".")
    text = str(val).strip()
    if text.lower() in {"", "none", "nan", "null", "n/a"}:
        return None
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _normalize_id_columns(df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    for col in columns:
        if col in df.columns:
            df[col] = df[col].map(_normalize_id_value)
    return df


def _coerce_numeric_columns(df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _df_summary(
        df: Optional[pd.DataFrame],
        *,
        date_cols: Optional[List[str]] = None,
        id_cols: Optional[List[str]] = None,
        numeric_cols: Optional[List[str]] = None,
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
        date_cols: Optional[List[str]] = None,
        id_cols: Optional[List[str]] = None,
        numeric_cols: Optional[List[str]] = None,
) -> None:
    summary = _df_summary(
        df,
        date_cols=date_cols,
        id_cols=id_cols,
        numeric_cols=numeric_cols,
    )
    if summary.get("state") == "none" or summary.get("empty"):
        log.warning("[kpk_loader] %s: %s", label, summary)
    else:
        log.info("[kpk_loader] %s: %s", label, summary)


def _shift_date(date_str: str, days: int) -> str:
    return (pd.to_datetime(date_str) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")

# ── Helpers ──────────────────────────────────────────────────────────────────
def _configured_holidays() -> set[str]:
    holidays = []
    static_holidays = os.getenv("KPK_HOLIDAYS", "2026-05-11")
    for token in static_holidays.split(','):
        holidays.append(token)
    return holidays


def _configured_holiday_timestamps() -> set[pd.Timestamp]:
    holidays: set[pd.Timestamp] = set()
    for token in _configured_holidays():
        ts = pd.to_datetime(token, errors="coerce")
        if pd.notna(ts):
            holidays.add(ts.normalize())
    return holidays


def _is_allowed_snapshot_day(ts: pd.Timestamp, holidays: set[pd.Timestamp]) -> bool:
    normalized = pd.to_datetime(ts, errors="coerce")
    if pd.isna(normalized):
        return False
    normalized = normalized.normalize()
    return normalized.weekday() <= 4 and normalized not in holidays


def _roll_forward_to_allowed_snapshot_day(ts: pd.Timestamp | None) -> pd.Timestamp | None:
    normalized = pd.to_datetime(ts, errors="coerce")
    if pd.isna(normalized):
        return None
    normalized = normalized.normalize()
    holidays = _configured_holiday_timestamps()
    for _ in range(14):
        if _is_allowed_snapshot_day(normalized, holidays):
            return normalized
        normalized = normalized + pd.Timedelta(days=1)
    return normalized

def _holiday_sql_filter(column: str, holidays: set[str]) -> str:
    if not holidays:
        return ""
    holiday_dates = ", ".join(f"DATE '{h}'" for h in sorted(holidays))
    return f"AND CAST({column} AS DATE) NOT IN ({holiday_dates})"

def get_prev_date_trino(date: str) -> Optional[str]:
    _validate_date(date)
    holidays = _configured_holidays()
    holiday_filter = _holiday_sql_filter("load_dttm", holidays)
    df = run_trino(
        sql=f"""
        SELECT MAX(CAST(load_dttm as DATE)) as prev_date
        FROM {T_LIM}
        WHERE CAST(load_dttm as DATE) < DATE '{date}'
          AND day_of_week(CAST(load_dttm as DATE)) BETWEEN 1 AND 5
          {holiday_filter}
        """,
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    if df.empty or df.iloc[0, 0] is None:
        return None
    return str(pd.to_datetime(df.iloc[0, 0]).date())

def resolve_limit_change_date_trino(
        *,
        division_cd: str,
        date: Optional[str],
        today_override: Optional[str] = None,
) -> str:
    _validate_digits(division_cd, "division_cd")
    if date:
        return _validate_date(date)

    effective_date = _effective_today_str(today_override)
    df = run_trino(
        sql=f"""
        SELECT MAX(CAST(load_dttm as DATE)) as resolved_date
        FROM {T_LIM}
        WHERE division_cd = '{division_cd}'
        AND CAST(load_dttm as DATE) <= DATE '{effective_date}'
        """,
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    if df.empty or df.iloc[0, 0] is None:
        return effective_date
    return str(pd.to_datetime(df.iloc[0, 0]).date())

def _postprocess_deals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return _EMPTY_DEALS.copy()
    df = _normalize_id_columns(df, ["division_cd", "internal_order_cd", "inn_num"])
    df = _coerce_numeric_columns(df, ["delta_limit_amt", "original_delta_limit_amt", "top_up_option_flg"])
    if "upload_dt" in df.columns:
        df["upload_dt"] = pd.to_datetime(df["upload_dt"], errors="coerce")
    return df

def _postprocess_trx(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return _EMPTY_TRX.copy()
    df = _normalize_id_columns(df, ["division_from_cd", "division_to_cd", "author_nm"])
    df = _coerce_numeric_columns(df, ["transaction_limit_amt"])
    if "transaction_dttz" in df.columns:
        df["transaction_dttz"] = pd.to_datetime(df["transaction_dttz"], errors="coerce")
    return df

def _postprocess_logs(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return _EMPTY_LOGS.copy()
    df = _normalize_id_columns(df, ["internal_order_cd", "inn_num"])
    return _coerce_numeric_columns(df, ["delta_limit_amt"])

def _postprocess_lim(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return _EMPTY_LIM.copy()
    df = _normalize_id_columns(df, ["division_cd", "parent_division_cd"])
    df = _coerce_numeric_columns(df, ["limit_amt"])
    if "upload_dt" in df.columns:
        df["upload_dt"] = pd.to_datetime(df["upload_dt"], errors="coerce")
    if "report_dt" in df.columns:
        df["report_dt"] = pd.to_datetime(df["report_dt"], errors="coerce")
    if "calc_limit_dt" in df.columns:
        df["calc_limit_dt"] = pd.to_datetime(df["calc_limit_dt"], errors="coerce")
    return df

def _enrich_first_upload_dt(df_deals: pd.DataFrame) -> pd.DataFrame:
    if df_deals.empty or "internal_order_cd" not in df_deals.columns:
        if "first_upload_dt" not in df_deals.columns:
            df_deals["first_upload_dt"] = pd.Series(dtype="datetime64[ns]")
        return df_deals

    order_cds = df_deals["internal_order_cd"].dropna().unique().tolist()
    if not order_cds:
        df_deals["first_upload_dt"] = df_deals["upload_dt"]
        return df_deals

    first_map: dict = {}
    for chunk in _chunks(order_cds, 5000):
        in_list = _quote_str_list(chunk)
        df_first = run_trino(
            sql=f"""
                SELECT internal_order_cd, MIN(CAST(load_dttm AS DATE)) as first_upload_dt
                FROM {T_DEALS} WHERE internal_order_cd IN ({in_list})
                GROUP BY internal_order_cd""",
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
        if not df_first.empty:
            df_first["internal_order_cd"] = df_first["internal_order_cd"].astype(str)
            df_first["first_upload_dt"] = pd.to_datetime(df_first["first_upload_dt"], errors="coerce")
            df_first["first_upload_dt"] = df_first["first_upload_dt"].map(_roll_forward_to_allowed_snapshot_day)
            first_map.update(dict(zip(df_first["internal_order_cd"], df_first["first_upload_dt"])))

    df_deals["first_upload_dt"] = df_deals["internal_order_cd"].map(first_map)
    df_deals["first_upload_dt"] = df_deals["first_upload_dt"].fillna(df_deals["upload_dt"])
    return df_deals

def _load_logs_by_order_cds(order_cds: List[str]) -> pd.DataFrame:
    if not order_cds:
        return _EMPTY_LOGS.copy()
    parts: list[pd.DataFrame] = []
    for chunk in _chunks(order_cds, 5000):
        in_list = _quote_str_list(chunk)
        df = run_trino(
            sql=f"""
            SELECT {LOGS_COLS} FROM {T_LOGS}
            WHERE CAST(pass_through_calc_id AS VARCHAR) IN ({in_list})
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
        if not df.empty:
            parts.append(df)
    if not parts:
        return _EMPTY_LOGS.copy()
    return _postprocess_logs(pd.concat(parts, ignore_index=True))

def _flagged_kpks_from_limits(df_lim: pd.DataFrame, date: str) -> List[str]:
    """Сет кпк для анализа negative-report"""
    if df_lim is None or df_lim.empty:
        return []

    target_date = pd.to_datetime(date)
    dt_column = "upload_dt" if "upload_dt" in df_lim.columns else "report_dt"
    all_kpks = (
        df_lim.loc[df_lim[dt_column] == target_date, "division_cd"]
        .dropna()
        .unique()
        .tolist()
    )
    if not all_kpks:
        return []

    diffs, _, _ = get_diff_limit_between_days(
        df_lim,
        all_kpks,
        date=str(target_date).split(" ")[0],
        dt_column=dt_column,
    )
    if diffs.empty:
        return []

    is_negative = (diffs["limit_t"] < 0) | (diffs["dif_lim"] < -10000)
    is_anomalous_positive = (
            diffs["limit_prev"].notna()
            & (diffs["limit_prev"].abs() > 0)
            & (diffs["dif_lim"] > 0.15 * diffs["limit_prev"].abs())
    )

    flagged = diffs.loc[is_negative | is_anomalous_positive, "division_cd"]
    flagged_kpks = [_normalize_id_value(kpk) for kpk in flagged.dropna().tolist()]
    return sorted({kpk for kpk in flagged_kpks if kpk})

# ── Endpoint loaders ─────────────────────────────────────────────────────────
def load_for_limit_change(
        *,
        division_cd: str,
        date: Optional[str],
        date_from: Optional[str] = None,
        today_override: Optional[str] = None,
) -> dict:
    log.info(
        "[kpk_loader] load_for_limit_change start division_cd=%s requested_date=%s date_from=%s today_override=%s",
        division_cd,
        date,
        date_from,
        today_override,
    )

    _validate_digits(division_cd, "division_cd")

    date = resolve_limit_change_date_trino(
        division_cd=division_cd,
        date=date,
        today_override=today_override,
    )
    log.info(
        "[kpk_loader] resolved limit change date division_cd=%s resolved_date=%s",
        division_cd,
        date,
    )

    # Roll forward to the next available snapshot if date falls on a
    # weekend or holiday (snapshots exist only on working days).
    date_ts = pd.to_datetime(date)
    rolled = _roll_forward_to_allowed_snapshot_day(date_ts)
    if rolled is not None:
        rolled_str = rolled.strftime("%Y-%m-%d")
        if rolled_str != date:
            log.info(
                "[kpk_loader] rolled snapshot date %s -> %s (weekend or holiday)",
                date, rolled_str,
            )
            date = rolled_str

    if date_from:
        _validate_date(date_from)
        prev_date = date_from
    else:
        prev_date = get_prev_date_trino(date)
    log.info(
        "[kpk_loader] previous limit date division_cd=%s date=%s prev_date=%s source=%s",
        division_cd,
        date,
        prev_date,
        "request" if date_from else "trino",
    )

    if prev_date:
        df_lim = run_trino(
            sql=f"""
            SELECT {LIM_COLS} FROM {T_LIM}
            WHERE division_cd = '{division_cd}'
            AND CAST(load_dttm as DATE) IN (DATE '{date}', DATE '{prev_date}')
            AND ccy_cd = 'RUB'
            AND calc_type_cd = 'DEPO'
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
    else:
        df_lim = run_trino(
            sql=f"""
            SELECT {LIM_COLS} FROM {T_LIM}
            WHERE division_cd = '{division_cd}' AND CAST(load_dttm as DATE) = DATE '{date}'
            AND ccy_cd = 'RUB'
            AND calc_type_cd = 'DEPO'
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
    _log_df_state(
        "limit rows raw",
        df_lim,
        date_cols=["upload_dt", "report_dt", "calc_limit_dt"],
        id_cols=["division_cd", "parent_division_cd"],
        numeric_cols=["limit_amt"],
    )
    if df_lim.empty:
        log.warning(
            "[kpk_loader] no limit rows found; using empty limit template division_cd=%s date=%s prev_date=%s",
            division_cd,
            date,
            prev_date,
        )
        df_lim = _EMPTY_LIM.copy()
        dt_from_df_lim = str((pd.to_datetime(date) - pd.Timedelta(days=1))).split(" ")[0]
    else:
        df_lim = _postprocess_lim(df_lim)
        dt_from_df_lim = str(df_lim["report_dt"].max().split(" ")[0])
    _log_df_state(
        "limit rows postprocess",
        df_lim,
        date_cols=["upload_dt", "report_dt", "calc_limit_dt"],
        id_cols=["division_cd", "parent_division_cd"],
        numeric_cols=["limit_amt"],
    )
    log.info(
        "[kpk_loader] transaction lookup date division_cd=%s trx_report_dt=%s",
        division_cd,
        dt_from_df_lim,
    )

    df_trx = run_trino(
        sql=f"""
        SELECT {TRX_COLS} FROM {T_TRX}
        WHERE report_dt = DATE '{dt_from_df_lim}'
          AND (division_to_cd = '{division_cd}' OR division_from_cd = '{division_cd}')
        """,
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    df_trx = _postprocess_trx(df_trx)
    _log_df_state(
        "transactions postprocess",
        df_trx,
        date_cols=["report_dt", "transaction_dttz"],
        id_cols=["division_from_cd", "division_to_cd", "author_nm"],
        numeric_cols=["transaction_limit_amt"],
    )

    df_deals_t = run_trino(
        sql=f"""
        SELECT {DEALS_COLS} FROM {T_DEALS}
        WHERE division_cd = '{division_cd}'
        AND CAST(load_dttm AS DATE) = DATE '{date}'
        """,
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    df_deals_t = _postprocess_deals(df_deals_t)
    _log_df_state(
        "deals current date postprocess",
        df_deals_t,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )

    if prev_date:
        if not df_deals_t.empty:
            order_cds_t = df_deals_t["internal_order_cd"].dropna().unique().tolist()
            log.info(
                "[kpk_loader] current deals found; loading previous date by order ids division_cd=%s date=%s prev_date=%s order_count=%s",
                division_cd,
                date,
                prev_date,
                len(order_cds_t),
            )
            if order_cds_t:
                in_list = _quote_str_list(order_cds_t)
                df_deals_p = run_trino(
                    sql=f"""
                    SELECT {DEALS_COLS} FROM {T_DEALS}
                    WHERE CAST(load_dttm AS DATE) = DATE '{prev_date}'
                    AND (internal_order_cd IN ({in_list}) OR division_cd = '{division_cd}')""",
                    catalog=_CATALOG,
                    schema=_SCHEMA,
                    is_test=False
                )
            else:
                log.warning(
                    "[kpk_loader] current deals have no internal_order_cd; loading previous date by division division_cd=%s prev_date=%s",
                    division_cd,
                    prev_date,
                )
                df_deals_p = run_trino(
                    sql=f"""
                    SELECT {DEALS_COLS} FROM {T_DEALS}
                    WHERE CAST(load_dttm AS DATE) = DATE '{prev_date}'
                    AND division_cd = '{division_cd}'
                    """,
                    catalog=_CATALOG,
                    schema=_SCHEMA,
                    is_test=False
                )
        else:
            log.info(
                "[kpk_loader] no current deals; loading previous date by division only division_cd=%s prev_date=%s",
                division_cd,
                prev_date,
            )
            df_deals_p = run_trino(
                sql=f"""
                SELECT {DEALS_COLS} FROM {T_DEALS}
                WHERE CAST(load_dttm AS DATE) = DATE '{prev_date}'
                AND division_cd = '{division_cd}'
                    """,
                    catalog=_CATALOG,
                    schema=_SCHEMA,
                    is_test=False
                )
        df_deals_p = _postprocess_deals(df_deals_p)
        _log_df_state(
            "deals previous date postprocess",
            df_deals_p,
            date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
            id_cols=["division_cd", "internal_order_cd", "inn_num"],
            numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
        )
        df_deals = pd.concat([df_deals_t, df_deals_p], ignore_index=True).drop_duplicates()
    else:
        log.warning(
            "[kpk_loader] previous date is missing; using current deals only division_cd=%s date=%s",
            division_cd,
            date,
        )
        df_deals = df_deals_t

    _log_df_state(
        "deals combined before first_upload enrichment",
        df_deals,
        date_cols=["upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )
    df_deals = _enrich_first_upload_dt(df_deals)
    _log_df_state(
        "deals combined enriched",
        df_deals,
        date_cols=["upload_dt", "first_upload_dt", "value_dt", "deal_dt", "maturity_dt"],
        id_cols=["division_cd", "internal_order_cd", "inn_num"],
        numeric_cols=["delta_limit_amt", "original_delta_limit_amt"],
    )
    all_order_cds = df_deals["internal_order_cd"].dropna().unique().tolist()
    log.info(
        "[kpk_loader] loading calculation logs division_cd=%s order_count=%s",
        division_cd,
        len(all_order_cds),
    )
    df_calc_logs = _load_logs_by_order_cds(all_order_cds)
    _log_df_state(
        "calculation logs postprocess",
        df_calc_logs,
        date_cols=["calculation_dttm", "value_dt"],
        id_cols=["internal_order_cd", "inn_num", "status_cd"],
        numeric_cols=["delta_limit_amt"],
    )
    log.info(
        "[kpk_loader] load_for_limit_change done division_cd=%s date=%s prev_date=%s shapes=%s",
        division_cd,
        date,
        prev_date,
        {
            "df_lim": tuple(df_lim.shape),
            "df_trx": tuple(df_trx.shape),
            "df_deals": tuple(df_deals.shape),
            "df_calc_logs": tuple(df_calc_logs.shape),
        },
    )

    return {"df_lim": df_lim, "df_trx": df_trx, "df_deals": df_deals, "df_calc_logs": df_calc_logs}

def load_for_negative_report(*, date: str) -> dict:
    _validate_date(date)
    # Roll forward to the next available snapshot if date is a weekend or holiday.
    date_ts = pd.to_datetime(date)
    rolled = _roll_forward_to_allowed_snapshot_day(date_ts)
    if rolled is not None:
        rolled_str = rolled.strftime("%Y-%m-%d")
        if rolled_str != date:
            log.info(
                "[kpk_loader] load_for_negative_report rolled snapshot date %s -> %s",
                date, rolled_str,
            )
            date = rolled_str
    prev_date = get_prev_date_trino(date)

    if prev_date:
        df_lim = run_trino(
            sql=f"""
            SELECT {LIM_COLS}
            FROM {T_LIM}
            WHERE CAST(load_dttm AS DATE) IN (DATE '{date}', DATE '{prev_date}')
            AND ccy_cd = 'RUB'
            AND calc_type_cd = 'DEPO'
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
    else:
        df_lim = run_trino(
            f"""
            SELECT {LIM_COLS}
            FROM {T_LIM}
            WHERE CAST(load_dttm AS DATE) = DATE '{date}'
            AND ccy_cd = 'RUB'
            AND calc_type_cd = 'DEPO'
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
    if df_lim.empty:
        return {
            "df_lim": _EMPTY_LIM.copy(),
            "df_trx": _EMPTY_TRX.copy(),
            "df_deals": _EMPTY_DEALS.copy(),
            "df_calc_logs": _EMPTY_LOGS.copy()
        }
        dt_from_df_lim = str((pd.to_datetime(date) - pd.Timedelta(days=1))).split(" ")[0]
    else:
        df_lim = _postprocess_lim(df_lim)
        dt_from_df_lim = str(df_lim["report_dt"].max().split(" ")[0])

    flagged_kpks = _flagged_kpks_from_limits(df_lim, date)
    if not flagged_kpks:
        return {
            "df_lim": df_lim,
            "df_trx": _EMPTY_TRX.copy(),
            "df_deals": _EMPTY_DEALS.copy(),
            "df_calc_logs": _EMPTY_LOGS.copy()
        }

    trx_parts: list[pd.DataFrame] = []
    for chunk in _chunks(flagged_kpks, 5000):
        in_list = _quote_str_list(chunk)
        df_part = run_trino(
            sql=f"""
            SELECT {TRX_COLS} FROM {T_TRX}
            WHERE report_dt = DATE '{dt_from_df_lim}'
              AND (
                division_from_cd IN ({in_list})
                OR division_to_cd IN ({in_list})
              )
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
        if not df_part.empty:
            trx_parts.append(df_part)
    df_trx = (
        _postprocess_trx(pd.concat(trx_parts, ignore_index=True))
        if trx_parts
        else _EMPTY_TRX.copy()
    )

    deals_parts: list[pd.DataFrame] = []
    if prev_date:
        date_filter = f"CAST(load_dttm AS DATE) IN (DATE '{date}', DATE '{prev_date}')"
    else:
        date_filter = f"CAST(load_dttm AS DATE) = DATE '{date}'"

    for chunk in _chunks(flagged_kpks, 5000):
        in_list = _quote_str_list(chunk)
        df_part = run_trino(
            sql=f"""
            SELECT {DEALS_COLS} FROM {T_DEALS}
            WHERE {date_filter}
              AND division_cd IN ({in_list})
            """,
            catalog=_CATALOG,
            schema=_SCHEMA,
            is_test=False
        )
        if not df_part.empty:
            deals_parts.append(df_part)

    df_deals = (
        _postprocess_deals(pd.concat(deals_parts, ignore_index=True))
        if deals_parts
        else _EMPTY_DEALS.copy()
    )
    df_deals = _enrich_first_upload_dt(df_deals)

    all_order_cds = df_deals["internal_order_cd"].dropna().unique().tolist()
    df_calc_logs = _load_logs_by_order_cds(all_order_cds)

    return {"df_lim": df_lim, "df_trx": df_trx, "df_deals": df_deals, "df_calc_logs": df_calc_logs}

def load_for_find_deal(*, division_cd: str, date: str) -> dict:
    _validate_date(date)
    _validate_digits(division_cd, "division_cd")
    start_date = _shift_date(date, -7)

    df_deals = run_trino(
        sql=f"""SELECT {DEALS_COLS} FROM {T_DEALS}
            WHERE division_cd = '{division_cd}'
            AND CAST(load_dttm AS DATE) BETWEEN DATE '{start_date}' AND DATE '{date}'
            """,
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    return {"df_deals": _postprocess_deals(df_deals)}

def load_for_investigate_deal(
        *, inn_num: str, value_dt: str, delta_amt: Optional[float] = None,
        division_cd: Optional[str] = None, lookback_days: int = 0,
        as_of_dt: Optional[str] = None,
) -> dict:
    _validate_date(value_dt)
    _validate_digits(inn_num, "inn_num")
    if as_of_dt:
        _validate_date(as_of_dt)

    target_vdt = pd.to_datetime(value_dt)
    target_as_of = pd.to_datetime(as_of_dt or datetime.date.today().isoformat())
    if lookback_days > 0:
        deal_dt_from = (target_vdt - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        deal_dt_to = value_dt
    else:
        deal_dt_from = (target_vdt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        deal_dt_to = (target_vdt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    log_dt_from = deal_dt_from
    log_dt_to = max(target_vdt, target_as_of).strftime("%Y-%m-%d")

    df_calc_logs = run_trino(
        sql=f"""SELECT {LOGS_COLS} FROM {T_LOGS}
            WHERE inn_num = '{inn_num}'
            AND (
                value_dt BETWEEN DATE '{log_dt_from}' AND DATE '{log_dt_to}'
                OR calculation_dttm BETWEEN TIMESTAMP '{log_dt_from} 00:00:00' AND TIMESTAMP '{log_dt_to} 23:59:59'
            )""",
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    df_calc_logs = _postprocess_logs(df_calc_logs)

    div_filter = f"AND division_cd = '{_validate_digits(division_cd, 'division_cd')}' " if division_cd else ""
    df_deals = run_trino(
        sql=f"""SELECT {DEALS_COLS} FROM {T_DEALS}
            WHERE inn_num = '{inn_num}'
            AND value_dt BETWEEN DATE '{deal_dt_from}' AND DATE '{deal_dt_to}'
        {div_filter}""",
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    df_deals = _postprocess_deals(df_deals)
    if not df_deals.empty:
        df_deals = _enrich_first_upload_dt(df_deals)

    return {"df_deals": df_deals, "df_calc_logs": df_calc_logs}

def load_for_client_history(
        *,
        inn_num: str,
        division_cd: str,
        date: Optional[str],
        lookback_days: int = 7,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
) -> dict:
    end_date = date_to or date
    if not end_date:
        raise ValueError("date or date_to is required")
    _validate_date(end_date)
    _validate_digits(inn_num, "inn_num")
    _validate_digits(division_cd, "division_cd")

    if date_from:
        _validate_date(date_from)
        start_date = date_from
    else:
        start_date = (pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    df_deals = run_trino(
        sql=f"""SELECT {DEALS_COLS} FROM {T_DEALS}
            WHERE inn_num = '{inn_num}' AND division_cd = '{division_cd}'
            AND (
                CAST(load_dttm AS DATE) BETWEEN DATE '{start_date}' AND DATE '{end_date}'
                OR value_dt BETWEEN DATE '{start_date}' AND DATE '{end_date}'
            )""",
        catalog=_CATALOG,
        schema=_SCHEMA,
        is_test=False
    )
    df_deals = _postprocess_deals(df_deals)

    order_cds = df_deals["internal_order_cd"].dropna().unique().tolist() if not df_deals.empty else []
    df_calc_logs = _load_logs_by_order_cds(order_cds)

    return {"df_deals": df_deals, "df_calc_logs": df_calc_logs}
