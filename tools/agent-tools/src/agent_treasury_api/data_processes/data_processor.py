from __future__ import annotations

import os
import math
import logging
import pandas as pd
from sqlalchemy import Engine, text

from .db import truncate_table, get_pg_schema

from ..config import settings

from .etc_rate_helpers import etc_rate_preprocessing

from .max_rate_loader import ForeignLimitRatesLoader

from ...utils.logger_config import configure_logging

from .helpers import get_term_label, get_term_label_crl, normalize_inn

from ...trino.trino_client import run_trino
from ...trino.sql_queries_bank import (
        NOR, ETC, OPTION_PRICE_NSO, OPTION_PRICE_DEPO,
        COST_LIQUIDITY_RISK, EVA_PAHOM, EVA_INDIC, EVA_TARGET,
        KEY_RATE, LIMIT_RATE, LIMIT_RATE_IB,
        AVG_CLIENTS_SPREAD, KEY_WNO_RATE
    )


configure_logging()
logger = logging.getLogger("---DataProcessor---")


class DataProcessor:
    """Качает данные из Trino, предобрабатывает и сохраняет в Postgres.
    На каждом запуске таблица очищается и наполняется заново.
    DDL остаётся в Liquibase, приложение делает только DML/TRUNCATE.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:

        self.engine = engine
        self.schema = get_pg_schema()
        logger.info("data_processor_start")

        self._etc_for()
        self._option_price_nso()
        self._option_price_depo()
        self._crl()
        self._nor()
        self._eva_pahom()
        self._min_eva()
        self._key_rate()
        self._foreign_limit_rates()
        self._limit_rate()
        self._history_spreads()

        logger.info("data_processor_complete")


    @staticmethod
    def _normalize_rub_limit_rate(raw_value: object) -> tuple[float, bool, list[float]]:
        """
        Приводит max_rate к доле

        Обычный кейс источника: значение приходит в процентах, поэтому делим на 100.
        Если после этого ставка всё ещё >= 100%, считаем, что значение
        дополнительно задублировало scale, и продолжаем делить на 100,
        пока не получим правдоподобную долю.
        """
        value = float(raw_value)
        if math.isfinite(value) and abs(value) >= 1:
            normalized = value / 100.0
            history = [value, normalized]
        else:
            normalized = value
            history = [value, normalized]
        corrected = False

        while math.isfinite(normalized) and abs(normalized) >= 1:
            normalized /= 100.0
            history.append(normalized)
            corrected = True

            if len(history) >= 8:
                break

        return normalized, corrected, history


    def _clear_table(self, table_name: str) -> None:
        if self.engine.dialect.name == "sqlite":
            return
        truncate_table(table_name, self.engine)

    def _replace_table_data(self, table_name: str, df: pd.DataFrame) -> None:
        if self.engine.dialect.name == "sqlite":
            df.to_sql(table_name, self.engine, if_exists="replace", index=False)
            return

        schema = get_pg_schema()
        with self.engine.begin() as conn:
            conn.execute(text(f'TRUNCATE TABLE "{schema}"."{table_name}"'))
            df.to_sql(
                table_name,
                conn,
                schema=schema,
                if_exists="append",
                index=False,
            )

    # ------------------------------------------------------------------
    # NOR
    # ------------------------------------------------------------------
    def _nor(self) -> float:
        try:
            df_nor = run_trino(NOR, "con_pg_dm", "dm_ddt")
            if not df_nor.empty:
                df_nor["nor"] = df_nor["nor"].astype(float)
                nor_rate = df_nor["nor"].unique()[0]
                if nor_rate > 1:
                    nor_rate /= 100
                return nor_rate
            logger.warning(f"nor_empty. Use fallback: {settings.nor_fallback}")
            return settings.nor_fallback
        except Exception as e:
            logger.warning(f"nor_error: {e}. Use fallback: {settings.nor_fallback}")
            return settings.nor_fallback

    # ------------------------------------------------------------------
    # ETC-FOR
    # ------------------------------------------------------------------
    def _etc_for(self) -> None:
        try:
            df_etc = run_trino(ETC, "con_hadoop_foton", "custom_fin_palm_rates")

            if df_etc.empty:
                logger.warning("etc_empty")
                self._clear_table("etc")
                return

            df_etc["start_dt"] = pd.to_datetime(df_etc["start_dt"])
            last_dt = str(df_etc["start_dt"].max()).split(" ")[0]

            nor_rate = self._nor()
            ftp_data = etc_rate_preprocessing(df_etc, last_dt, nor_rate=nor_rate)

            self._replace_table_data("etc", ftp_data)
            logger.info("etc_saved")
        except Exception as e:
            logger.warning(f"etc_error: {e}")
            self._clear_table("etc")

    # ------------------------------------------------------------------
    # Option Price NSO
    # ------------------------------------------------------------------
    def _option_price_nso(self) -> None:
        try:
            df = run_trino(OPTION_PRICE_NSO, "con_hadoop_foton", "custom_fin_palm_rates")

            if df.empty:
                logger.warning("option_price_nso_empty")
                self._clear_table("option_price_nso")
                return

            df["term_day_cnt"] = df["term_day_cnt"].astype(int)
            df["min_amt"] = df["min_amt"].astype(float).astype(int)
            df["max_amt"] = df["max_amt"].astype(float).astype(int)
            df["option_price"] = df["option_price"].astype(float) / 100
            df["term_cd"] = df["term_day_cnt"].apply(get_term_label_crl)

            self._replace_table_data("option_price_nso", df)
            logger.info("option_price_nso_saved")
        except Exception as e:
            logger.warning(f"option_price_nso_error: {e}")
            self._clear_table("option_price_nso")

    # ------------------------------------------------------------------
    # Option Price DEPO
    # ------------------------------------------------------------------
    def _option_price_depo(self) -> None:
        try:
            df = run_trino(OPTION_PRICE_DEPO, "con_hadoop_foton", "custom_fin_palm_rates")

            if df.empty:
                logger.warning("option_price_depo_empty")
                self._clear_table("option_price_depo")
                return

            df["term_day_cnt"] = df["term_day_cnt"].astype(int)
            df["min_amt"] = df["min_amt"].astype(float).astype(int)
            df["max_amt"] = df["max_amt"].astype(float).astype(int)
            df["option_price"] = df["option_price"].astype(float) / 100
            df["term_cd"] = df["term_day_cnt"].apply(get_term_label_crl)

            self._replace_table_data("option_price_depo", df)
            logger.info("option_price_depo_saved")
        except Exception as e:
            logger.warning(f"option_price_depo_error: {e}")
            self._clear_table("option_price_depo")

    # ------------------------------------------------------------------
    # CRL
    # ------------------------------------------------------------------
    def _crl(self) -> None:
        try:
            df = run_trino(COST_LIQUIDITY_RISK, "con_hadoop_foton", "custom_fin_palm_rates")
            if df.empty:
                logger.warning("crl_empty")
                self._clear_table("crl")
                return

            df["term_day_cnt"] = df["term_day_cnt"].astype(int)
            df["crl_val"] = df["crl_val"].astype(float) / 100

            self._replace_table_data("crl", df)
            logger.info("crl_saved")
        except Exception as e:
            logger.warning(f"crl_error: {e}")
            self._clear_table("crl")

    # ------------------------------------------------------------------
    # LIMIT_RATE
    # ------------------------------------------------------------------
    def _limit_rate(self) -> None:
        try:
            df = run_trino(
                LIMIT_RATE,
                "con_hadoop_foton",
                "custom_fin_palm_rates",
            )
            df['ib_flg'] = 0

            df_ib = run_trino(
                LIMIT_RATE_IB,
                "con_hadoop_foton",
                "custom_fin_palm_rates",
            )
            df_ib['ib_flg'] = 1

            lim_rate_df = pd.concat([df, df_ib]).reset_index(drop=True)

            if lim_rate_df.empty:
                logger.warning("limit_rate_empty")
                self._clear_table("rub_limit_rates")
                return

            lim_rate_df.loc[lim_rate_df['product_type_cd'] == 'Dep', 'product_type_cd'] = 'DEPO'
            lim_rate_df['term_day_cnt'] = lim_rate_df['term_day_cnt'].astype(int)
            # lim_rate_df['max_rate'] = lim_rate_df['max_rate'].astype(float) / 100
            lim_rate_df['min_amt'] = lim_rate_df['min_amt'].astype(float).astype(int)
            lim_rate_df['max_amt'] = lim_rate_df['max_amt'].astype(float).astype(int)
            lim_rate_df['ib_flg'] = lim_rate_df['ib_flg'].astype(int)

            normalized_rates = lim_rate_df['max_rate'].astype(float).apply(self._normalize_rub_limit_rate)
            lim_rate_df['max_rate'] = normalized_rates.apply(lambda item: item[0])

            corrected_rows = lim_rate_df[
                normalized_rates.apply(lambda item: item[1])
            ].copy()
            if not corrected_rows.empty:
                corrected_rows['max_rate_fix_history'] = normalized_rates[
                    normalized_rates.apply(lambda item: item[1])
                ].apply(lambda item: item[2])
                logger.warning(
                    "rub_limit_rates_max_rate_scale_fix count=%s rows=%s",
                    len(corrected_rows),
                    corrected_rows[
                        [
                            'product_type_cd',
                            'product_cd',
                            'min_amt',
                            'max_amt',
                            'term_cd',
                            'term_day_cnt',
                            'ib_flg',
                            'max_rate_fix_history',
                        ]
                    ].to_dict(orient='records'),
                )

            lim_rate_df = lim_rate_df.drop(['start_dt', 'ccy_cd'], axis=1)

            self._replace_table_data("rub_limit_rates", lim_rate_df)
            logger.info("lim_rates_saved")
        except Exception as e:
            logger.warning(f"lim_rates_error: {e}")
            self._clear_table("rub_limit_rates")


    # ------------------------------------------------------------------
    # History Spreads
    # ------------------------------------------------------------------
    def _history_spreads(self) -> None:
        try:
            df = run_trino(
                AVG_CLIENTS_SPREAD.format(int(os.getenv("HIST_DEALS_N_MEAN", 10))),
                "con_hadoop_foton",
                "custom_fin_palm_dataops"
            )

            if df.empty:
                logger.warning("history_spreads_empty")
                self._clear_table("history_ftp_spreads_matrix")
                return

            df["n_deals"] = df["n_deals"].astype(int)
            df["n_from_own_bucket"] = df["n_from_own_bucket"].astype(int)
            df["n_from_nearest_buckets"] = df["n_from_nearest_buckets"].astype(int)
            df["wmean_spread"] = df["wmean_spread"].astype(float)
            df["avg_volume"] = df["avg_volume"].astype(float)
            df["client"] = df["inn_num"].apply(normalize_inn)
            df['label'] = df['term_bucket'] + '-' + df['vol_bucket']
            df = df.drop(["inn_num"], axis=1)
            df = df[["client", "label", "term_bucket", "vol_bucket", "wmean_spread",
                     "n_deals","avg_volume", "n_from_own_bucket", "n_from_nearest_buckets"]]

            self._replace_table_data("history_ftp_spreads_matrix", df)
            logger.info("history_spreads_saved")
        except Exception as e:
            logger.warning(f"history_spreads_error: {e}")
            self._clear_table("history_ftp_spreads_matrix")

    # ------------------------------------------------------------------
    # EVA Pahom
    # ------------------------------------------------------------------
    def _eva_pahom(self) -> None:
        try:
            df = run_trino(EVA_PAHOM, "con_hadoop_foton", "custom_fin_palm_dataops")

            if df.empty:
                logger.warning("eva_pahom_empty")
                self._clear_table("eva_pahom")
                return

            df["term_cnt"] = df["term_cnt"].astype(int)
            df["min_amt"] = df["min_amt"].astype(float).astype(int)
            df["max_amt"] = df["max_amt"].astype(float).astype(int)
            df["eva_rate"] = df["eva_rate"].astype(float) / 100
            df["term_cd_eva"] = df["term_cnt"].apply(lambda x: get_term_label(x, is_eva=True))

            self._replace_table_data("eva_pahom", df)
            logger.info("eva_pahom_saved")
        except Exception as e:
            logger.warning(f"eva_pahom_error: {e}")
            self._clear_table("eva_pahom")

    # ------------------------------------------------------------------
    # Min EVA (Indic + Target)
    # ------------------------------------------------------------------
    def _min_eva(self) -> None:
        try:
            df_indic = run_trino(EVA_INDIC, "con_hadoop_foton", "custom_fin_palm_dataops")
            df_target = run_trino(EVA_TARGET, "con_hadoop_foton", "custom_fin_palm_dataops")
        except Exception as e:
            logger.warning(f"min_eva_load_error: {e}")
            self._clear_table("min_eva")
            return

        if df_indic.empty and df_target.empty:
            logger.warning("min_eva_both_empty")
            self._clear_table("min_eva")
            return

        if df_indic.empty and not df_target.empty:
            df_target["TargetEVA"] = df_target["TargetEVA"].astype(float) / 100
            df_target["term_cd_eva"] = df_target["term_cnt"].apply(lambda x: get_term_label(x, is_eva=True))
            result = df_target.rename(columns={"TargetEVA": "minEvaRate"})
            self._replace_table_data("min_eva", result)
            logger.info("min_eva_saved")
            return

        if not df_indic.empty and df_target.empty:
            df_indic["IndicEVA"] = df_indic["IndicEVA"].astype(float) / 100
            df_indic["term_cd_eva"] = df_indic["term_cnt"].apply(lambda x: get_term_label(x, is_eva=True))
            result = df_indic.rename(columns={"IndicEVA": "minEvaRate"})
            self._replace_table_data("min_eva", result)
            logger.info("min_eva_saved")
            return

        # Обе таблицы непустые — merge
        for df in (df_indic, df_target):
            df["term_cnt"] = df["term_cnt"].astype(int)
            df["min_amt"] = df["min_amt"].astype(float).astype(int)
            df["max_amt"] = df["max_amt"].astype(float).astype(int)

        df_indic["IndicEVA"] = df_indic["IndicEVA"].astype(float) / 100
        df_target["TargetEVA"] = df_target["TargetEVA"].astype(float) / 100

        merge_keys = [
            "business_block_cd", "product_cd", "subproduct_cd",
            "term_cnt", "min_amt", "max_amt", "ib_flg", "sens_flg",
        ]
        full_eva = df_indic.merge(df_target, how="outer", on=merge_keys)

        full_eva["sens_flg"] = full_eva["sens_flg"].astype(int)
        full_eva["ib_flg"] = full_eva["ib_flg"].astype(int)
        full_eva["max_amt"] = full_eva["max_amt"].astype(float).astype(int)
        full_eva["min_amt"] = full_eva["min_amt"].astype(float).astype(int)
        full_eva["term_cnt"] = full_eva["term_cnt"].astype(int)
        full_eva["TargetEVA"] = full_eva["TargetEVA"].astype(float)
        full_eva["IndicEVA"] = full_eva["IndicEVA"].astype(float)

        full_eva["IndicEVA"] = full_eva["IndicEVA"].fillna(full_eva["TargetEVA"])
        full_eva["TargetEVA"] = full_eva["TargetEVA"].fillna(full_eva["IndicEVA"])

        full_eva = full_eva.dropna(subset=["IndicEVA", "TargetEVA"])

        full_eva["minEvaRate"] = full_eva[["TargetEVA", "IndicEVA"]].min(axis=1)
        full_eva["term_cd_eva"] = full_eva["term_cnt"].apply(lambda x: get_term_label(x, is_eva=True))

        self._replace_table_data("min_eva", full_eva)
        logger.info("min_eva_saved")


    # ------------------------------------------------------------------
    # KEY RATE
    # ------------------------------------------------------------------
    def _key_rate(self) -> None:
        try:
            try:
                df = run_trino(KEY_RATE, "con_pg_dm", "dm_csp")
            except Exception as e:
                logger.warning(f"key_rate_load_error: {e}")
                df = pd.DataFrame()
            try:
                logger.info("key_rate_load_wno_rate_columns")
                df = run_trino(KEY_WNO_RATE, "con_pg_dm", "dm_csp")
            except Exception as e:
                logger.warning(f"key_rate_load_wno_rate_columns_error: {e}")
                df = pd.DataFrame()

            if df.empty:
                logger.warning("key_rate_empty")
                self._clear_table("key_rate")
                return

            if "cbr_rate" not in df.columns:
                df['cbr_rate'] = float(os.getenv("CBR_RATE", "15"))

            df["start_dt"] = pd.to_datetime(df["start_dt"])
            df["cbr_rate"] = df["cbr_rate"].astype(float) / 100

            self._replace_table_data("key_rate", df)
            logger.info("key_rate_saved")
        except Exception as e:
            logger.warning(f"key_rate_error: {e}")
            self._clear_table("key_rate")

    # ------------------------------------------------------------------
    # FOREIGN LIMIT RATES
    # ------------------------------------------------------------------
    def _foreign_limit_rates(self) -> None:
        try:
            loader = ForeignLimitRatesLoader()
            df = loader.load()
            if df.empty:
                logger.warning("foreign_limit_rates_empty")
                self._clear_table("foreign_limit_rates")
                return
            self._replace_table_data("foreign_limit_rates", df)
            logger.info("foreign_limit_rates_saved")
        except Exception as e:
            logger.warning(f"foreign_limit_rates_error: {e}")
            self._clear_table("foreign_limit_rates")
