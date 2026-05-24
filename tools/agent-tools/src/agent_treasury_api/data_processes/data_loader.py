from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import Engine
from sqlalchemy.exc import NoSuchTableError, ProgrammingError

from .db import get_pg_schema

from .helpers import (
    get_term_label, get_term_label_crl, get_term_label_cbr_spreads,
    get_vol_labels_cbr_spreads, get_vol_label_clients,
    get_term_label_clients, normalize_inn,
    get_term_label_lim_rates, get_term_label_limit_rates,
)
from ..config import settings

logger = structlog.getLogger(__name__)

BASIS_COL_MAP = {
    "MONTH": "month",
    "QUARTAL": "quartal",
    "SEMIANNUAL": "semiannual",
    "ANNUAL": "annual",
}

CBR_SPREADS_BASIS_MAP = {
    "MONTH": "monthly",
    "QUARTAL": "quarterly"
}


def _read_table(engine: Engine, name: str) -> pd.DataFrame:
    """Читает таблицу целиком. Если её нет — возвращает пустой DataFrame."""
    try:
        return pd.read_sql_table(name, engine, schema=get_pg_schema())
    except (NoSuchTableError, ProgrammingError, ValueError):
        return pd.DataFrame()


class DataLoader:
    """Читает таблицы Postgres и фильтрует по параметрам запроса."""

    @classmethod
    def etc_for_basis(cls, engine: Engine, term: int, basis: str | None = None) -> float | None:
        try:
            df = _read_table(engine, "etc")
            if df.empty:
                logger.warning("etc_table_empty")
                return None
            term = term or 1
            col = BASIS_COL_MAP.get(basis, "zc") if basis else "zc"
            row = df[df["term_day_cnt"] == term]
            if row.empty:
                return None
            return np.round(float(row[f"ftp_for_{col}"].unique()[0]), 6)
        except Exception as e:
            logger.warning(f"etc_read_error: {e}")
            return None

    @classmethod
    def eva_pahom(
        cls, engine: Engine, *, product_cd: str, term: int, vol: float,
    ) -> float | None:
        try:
            df = _read_table(engine, "eva_pahom")
            if df.empty:
                logger.warning("eva_pahom_table_empty")
                return None
            filtered = df[
                (df["product_cd"] == product_cd)
                & (df["term_cd_eva"] == get_term_label(term, is_eva=True))
                & (df["min_amt"] <= vol)
                & (df["max_amt"] >= vol)
            ]
            if not filtered.empty:
                return np.round(float(filtered["eva_rate"].unique()[0]), 6)
            return None
        except Exception as e:
            logger.warning(f"eva_pahom_read_error: {e}")
            return None

    @classmethod
    def min_eva(
        cls,
        engine: Engine,
        *,
        segment: str,
        product_cd: str,
        term: int,
        vol: float,
        is_ip: int,
        is_sense_eva: int,
    ) -> float | None:
        try:
            df = _read_table(engine, "min_eva")
            if df.empty:
                logger.warning("min_eva_table_empty")
                return None
            filtered = df[
                (df["business_block_cd"] == segment)
                & (df["product_cd"] == product_cd)
                & (df["term_cd_eva"] == get_term_label(term, is_eva=True))
                & (df["min_amt"] <= vol)
                & (df["max_amt"] >= vol)
                & (df["ib_flg"] == is_ip)
                & (df["sens_flg"] == is_sense_eva)
            ]
            if not filtered.empty:
                return np.round(float(filtered["minEvaRate"].unique()[0]), 6)
            return None
        except Exception as e:
            logger.warning(f"min_eva_read_error: {e}")
            return None

    @classmethod
    def lim_rate(
        cls,
        engine: Engine,
        ccy_cd: str = 'CNY',
        product: str = 'D',
        term: int = 0,
        volume: float = 0.0,
        ib_flg: int = 0,
        optionality: str = "",
    ) -> float | None:
        try:
            df = _read_table(engine, "foreign_limit_rates")
            if df.empty:
                logger.warning("foreign_limit_rates_table_empty")
                return None
            filtered = df[
                (df['ccy_cd'] == ccy_cd) &
                (df['ib_flg'] == ib_flg) &
                (df['product'] == product) &
                (df['optionality'] == optionality) &
                (df['term_cd'] == get_term_label_lim_rates(term)) &
                (df['minAmt'] <= volume) &
                (df['maxAmt'] >= volume)
            ]
            if not filtered.empty:
                return np.round(float(filtered["rateValue"].unique()[0]), 4)
            return None
        except Exception as e:
            logger.warning(f"foreign_limit_rates_read_error: {e}")
            return None

    @classmethod
    def lim_rate_rub(
        cls,
        engine: Engine,
        product: str='DEPO',
        optionality: str='Max',
        volume: float=500e6,
        term: int = 1,
        ib_flg: int = 0
    ) -> float | None:
        optionality_map = {"OTZ": "Max_Otz", "POP": "Max_Pop"}

        if optionality == '' and product == 'NSO':
            option = 'NSO_Max'
        elif optionality == '' and product == 'DEPO':
            option = 'Max'
        else:
            option = optionality_map[optionality]

        if not term:
            term_bucket = '1-3D'
        else:
            term_bucket = get_term_label_limit_rates(term)

        if not ib_flg:
            ib_flg = 0

        try:
            df = _read_table(engine, "rub_limit_rates")
            if df.empty:
                return None
            df['max_rate'] = df['max_rate'].astype(float)
            df['min_amt'] = df['min_amt'].astype(float)
            df['max_amt'] = df['max_amt'].astype(float)
            df['min_amt'] = df['min_amt'].astype(int)
            df['max_amt'] = df['max_amt'].astype(int)
            mask = (
                (df['product_type_cd']==product) &
                (df['product_cd']==option) &
                (df['term_cd']==term_bucket) &
                (df['ib_flg']==int(ib_flg)) &
                (
                    (df['min_amt']<=float(volume)) &
                    (df['max_amt']>=float(volume))
                )
            )
            df_filtered = df[mask]
            if df_filtered.empty:
                logger.warning("lim_rate_rub_empty_initial_filters")
                df_filtered = df[
                    (df['product_type_cd']=='DEPO') &
                    (df['product_cd']=='Max') &
                    (df['term_cd']=='1-3D') &
                    (df['ib_flg']==int(0)) &
                    (
                        (df['min_amt']<=float(45e6)) &
                        (df['max_amt']>=float(45e6))
                    )
                ]

            return float(df_filtered['max_rate'].unique()[0])

        except Exception as e:
            logger.warning(f"lim_rate_rub_read_error: {e}")
            return None


    # ------------------------------------------------------------------
    # Тип B — компоненты с fallback: нет данных = подстановка + warning
    # ------------------------------------------------------------------

    @classmethod
    def option_price(
        cls, engine: Engine, *, product: str, term: int, vol: float, optionality: str,
    ) -> float:
        if optionality == "":
            return 0.0
        optionality_map = {"OTZ": "OTZ", "OTZ_POP": "OTZ", "POP": "POP"}
        try:
            table = "option_price_nso" if product == "NSO" else "option_price_depo"
            df = _read_table(engine, table)

            if df.empty:
                logger.warning(
                    "option_price_table_empty table=%s fallback=%s",
                    table,
                    settings.option_price_fallback,
                )
                return settings.option_price_fallback

            filtered = df[
                (df["min_amt"] <= vol)
                & (df["max_amt"] >= vol)
                & (df["term_cd"] == get_term_label_crl(term))
                & (df["product_type_cd"] == optionality_map[optionality])
            ]
            if not filtered.empty:
                return np.round(float(filtered["option_price"].unique()[0]), 6)
            logger.warning(
                "option_price_not_found optionality=%s term=%s vol=%s fallback=%s",
                optionality,
                term,
                vol,
                settings.option_price_fallback,
            )
            return settings.option_price_fallback
        except Exception as e:
            logger.warning(f"option_price_read_error: {e}. Use fallback: {settings.option_price_fallback}")
            return settings.option_price_fallback

    @classmethod
    def crl(
        cls, engine: Engine, *, product_cd: str, product: str, optionality: str, term: int,
    ) -> float:
        if optionality == "" and product == "DEPO":
            return 0.0
        try:
            df = _read_table(engine, "crl")

            if df.empty:
                logger.warning("crl_table_empty fallback=%s", settings.crl_fallback)
                return settings.crl_fallback

            filtered = df[
                (df["product_cd"] == product_cd)
                & (df["product_type_cd"] == optionality)
                & (df["term_cd"] == get_term_label_crl(term))
            ]
            if not filtered.empty:
                return np.round(float(filtered["crl_val"].unique()[0]), 6)
            logger.warning(
                "crl_not_found product_cd=%s optionality=%s term=%s fallback=%s",
                product_cd,
                optionality,
                term,
                settings.crl_fallback,
            )
            return settings.crl_fallback
        except Exception as e:
            logger.warning(f"crl_read_error: {e}. Use fallback: {settings.crl_fallback}")
            return settings.crl_fallback

    @classmethod
    def key_rate(cls, engine: Engine) -> float:
        try:
            df = _read_table(engine, "key_rate")
            if df.empty:
                logger.warning("key_rate_table_empty fallback=%s", settings.key_rate_fallback)
                return settings.key_rate_fallback
            return np.round(float(df["cbr_rate"].unique()[-1]), 6)
        except Exception as e:
            logger.warning(f"key_rate_read_error: {e}. Use fallback: {settings.key_rate_fallback}")
            return settings.key_rate_fallback

    @classmethod
    def key_rate_spreads(cls, engine: Engine, term: int, volume: float, basis: str) -> float:
        try:
            df = _read_table(engine, "treasury_float_rates_spread")
            if df.empty:
                logger.warning(
                    "key_rate_spreads_table_empty fallback=%s",
                    settings.key_rate_spreads_fallback,
                )
                return settings.key_rate_spreads_fallback
            term_bucket = get_term_label_cbr_spreads(term)
            vol_bucket = get_vol_labels_cbr_spreads(volume)
            filtered = float(df[df['term'] == term_bucket][f"{vol_bucket}"].unique()[0])
            if not filtered:
                filtered = 0.0
            if basis not in list(CBR_SPREADS_BASIS_MAP.keys()):
                filtered_basis = 0.0
            else:
                filtered_basis = float(df[df['term'] == term_bucket][f"{CBR_SPREADS_BASIS_MAP[basis]}"].unique()[0])
            return filtered + filtered_basis
        except Exception as e:
            logger.warning(f"key_rate_spreads_read_error: {e}. Use fallback: {settings.key_rate_spreads_fallback}")
            return settings.key_rate_spreads_fallback

    # ------------------------------------------------------------------
    # Тип C — исторический спред: нет данных = None
    # ------------------------------------------------------------------

    @classmethod
    def hist_spread(
        cls, engine: Engine, *, inn: str, term: int, vol: float,
    ) -> float | None:
        """Спред из исторических сделок клиента (таблица history_spreads)."""
        try:
            df = _read_table(engine, "history_ftp_spreads_matrix")
            if df.empty:
                logger.warning("history_spreads_empty")
                return None
            filtered = df[
                (df['client'] == normalize_inn(inn)) &
                (df['term_bucket'] == get_term_label_clients(term)) &
                (df['vol_bucket'] == get_vol_label_clients(vol))
            ]
            if not filtered.empty:
                return np.round(float(filtered["wmean_spread"].unique()[0]), 6)
            filtered = df[
                (df['term_bucket'] == get_term_label_clients(term)) &
                (df['vol_bucket'] == get_vol_label_clients(vol))
            ]
            if not filtered.empty:
                return np.round(float(filtered["wmean_spread"].mean()), 6)
            return None
        except Exception as e:
            logger.warning(f"history_spreads_error: {e}")
            return None
