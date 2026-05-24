"""Калькулятор процентной ставки уровня согласования казначейства по условиям сделки"""

from __future__ import annotations

import logging
import calendar
import datetime
import numpy as np
from sqlalchemy import Engine

from .data_processes.data_loader import DataLoader
from .client_processes.client_service import ClientDataService

from ..utils.logger_config import configure_logging


configure_logging()
logger = logging.getLogger("---RateCalculator---")


class RateCalculator:

    MAP_PRODUCT_EVA = {"DEPO": "D", "NSO": "NSO"}
    MAP_PRODUCT_CRL = {"DEPO": "Depo", "NSO": "NSO"}

    FIN_ORG_SPEC_CRL_WEIGHT = 0.008

    def __init__(
        self,
        engine: Engine,
        *,
        inn: str,
        ccy: str = "RUB",
        product: str = "DEPO",
        term: int = 1,
        vol: float = 500e6,
        rate_type: str = "FIX",
        basis: str = "END",
        optionality: str = ""
    ) -> None:

        self.cl = ClientDataService(inn).get_client_info()
        logger.info("client_info_loaded")
        self.IS_IP = self.cl.is_ip
        self.IS_MSP = self.cl.is_msp
        self.SEGMENT = self.cl.segment
        self.IS_SENSE_EVA = self.cl.is_sense_eva
        self.IS_FIN_ORG = self.cl.is_fin_org
        self.IS_SUBJECT = self.cl.is_subject
        
        self.engine = engine

        self.CCY = ccy
        self.PRODUCT = product
        self.TERM = int(term)
        self.VOL = float(vol)
        self.RATE_TYPE = rate_type
        self.BASIS = basis
        self.OPTIONALITY = optionality
        
        self.SUBJECT_SPEC_CRL_WEIGHT = np.round(0.01 - self.FIN_ORG_SPEC_CRL_WEIGHT, 4)
        self.CRL_FINORG: float = self._set_crl_finorg()
        
        self.ASV: float = self._set_asv()

    # ------------------------------------------------------------------
    # Внутренние компоненты
    # ------------------------------------------------------------------

    _BASIS_N = {
        "MONTH": 1 / 12,
        "QUARTAL": 1 / 4,
        "SEMIANNUAL": 1 / 2,
        "ANNUAL": 1,
    }

    def _rate_to_basis(self, rate: float, term: int, basis: str) -> float:
        """Пересчёт ставки из месячного базиса в целевой (аналогично ETCBasisCalculator.etc_basis)."""
        if not rate:
            return 0.0
        if basis == "END":
            return rate
        if not term or term < 32:
            return rate
        today = str(datetime.date.today())
        dt = datetime.date.today()
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        days_in_year = 366 if calendar.isleap(dt.year) else 365
        t = days_in_month / days_in_year
        n = self._BASIS_N[basis]
        return np.round((((1 + rate * t) ** (n / t) - 1) / n), 6)

    def _set_asv(self) -> float:
        if self.IS_MSP == 1 or self.IS_IP == 1:
            return 0.0048
        return 0.0

    def _set_crl_finorg(self) -> float:
        if self.IS_FIN_ORG != 1 and self.IS_SUBJECT != 1:
            return 0.0

        if self.IS_FIN_ORG == 1:
            weight = self.FIN_ORG_SPEC_CRL_WEIGHT
        else:
            weight = self.SUBJECT_SPEC_CRL_WEIGHT

        if self.TERM < 30:
            return np.round(-weight, 4)
        return np.round(-weight / (self.TERM / 30), 4)

    # ------------------------------------------------------------------
    # Итоговые формулы
    # ------------------------------------------------------------------

    def fund_rate(self) -> float | None:
        try:
            etc_for = DataLoader.etc_for_basis(self.engine, term=self.TERM, basis=self.BASIS)
            option_price = DataLoader.option_price(
                self.engine, product=self.PRODUCT, term=self.TERM, vol=self.VOL, optionality=self.OPTIONALITY
            )
            crl = DataLoader.crl(
                self.engine,
                product_cd=self.MAP_PRODUCT_CRL[self.PRODUCT],
                product=self.PRODUCT,
                optionality=self.OPTIONALITY,
                term=self.TERM,
            )
            fr = etc_for - option_price + crl - self.ASV
            return np.round(float(fr), 6)
        except Exception:
            return None

    def nul_rate(self) -> float | None:
        try:
            min_eva = DataLoader.min_eva(
                self.engine,
                segment=self.SEGMENT,
                product_cd=self.MAP_PRODUCT_EVA[self.PRODUCT],
                term=self.TERM,
                vol=self.VOL,
                is_ip=self.IS_IP,
                is_sense_eva=self.IS_SENSE_EVA,
            )
            fr = self.fund_rate()
            if min_eva is not None and fr is not None:
                return np.round(float(fr - min_eva) * 100, 2)
            return None
        except Exception:
            return None

    def rate(self) -> float | None:
        """Основной метод — расчет итоговой ставки уровня согласования"""

        if self.CCY == "CNY":
            """макс {0,01% годовых; (Ставка уровня согл-я для CNY – АСВ –  спред)} + СРЛ спец."""
            cny_rate = DataLoader.lim_rate(self.engine, self.CCY, self.MAP_PRODUCT_EVA[self.PRODUCT], self.TERM, self.VOL, self.IS_IP, self.OPTIONALITY)
            if cny_rate is None:
                return None
            if self.BASIS != "END":
                cny_rate = self._rate_to_basis(cny_rate, self.TERM, self.BASIS)
            if self.OPTIONALITY == 'OTZ' or self.PRODUCT == 'NSO':
                spread = 0.015
            else:
                spread = 0.0
            rate_approve = max(0.0001, (cny_rate - self.ASV - spread)) + self.CRL_FINORG
            return round(float(rate_approve) * 100, 2)

        if self.CCY == "INR":
            """макс {0,01% годовых; (Ставка уровня согл-я для INR – АСВ –  спред)} + СРЛ спец."""
            inr_rate = DataLoader.lim_rate(self.engine, self.CCY, self.MAP_PRODUCT_EVA[self.PRODUCT], self.TERM, self.VOL, self.IS_IP, self.OPTIONALITY)
            if inr_rate is None:
                return None
            if self.BASIS != "END":
                inr_rate = self._rate_to_basis(inr_rate, self.TERM, self.BASIS)
            if self.OPTIONALITY == 'OTZ' or self.PRODUCT == 'NSO':
                spread = 0.10
            else:
                spread = 0.0
            rate_approve = max(0.0001, (inr_rate - self.ASV - spread)) + self.CRL_FINORG
            return round(float(rate_approve) * 100, 2)

        
        # -------------------------------------------------------------------------------
        # Расчет максимальной ставки согласования для сделок в рублях с плавающей ставкой
        # -------------------------------------------------------------------------------
        if self.CCY == "RUB" and self.RATE_TYPE == "FLOAT":
            if self.TERM < 61:
                # для срока менее 2 месяцев рассчитываем по фикс для альтернативы
                etc_for = DataLoader.etc_for_basis(self.engine, term=self.TERM, basis=self.BASIS)
                eva_pahom = DataLoader.eva_pahom(
                    self.engine,
                    product_cd=self.MAP_PRODUCT_EVA[self.PRODUCT],
                    term=self.TERM,
                    vol=self.VOL,
                )
                if etc_for is None or eva_pahom is None:
                    return None
                rate_approve = etc_for - eva_pahom - self.ASV + self.CRL_FINORG
                return round(float(rate_approve) * 100, 2)

            key_rate = DataLoader.key_rate(self.engine)
            spread = DataLoader.key_rate_spreads(self.engine, self.TERM, self.VOL, self.BASIS)

            rate_approve = key_rate + spread - self.ASV + self.CRL_FINORG

            if self.OPTIONALITY in ['OTZ', 'POP_OTZ']:
                # для опциона с отзывом дополнительно вычитаем 0.25%
                rate_approve -= 0.0025

            return round(float(rate_approve) * 100, 2)
                

        # -----------------------------------------------------------------------------------
        # Расчет максимальной ставки согласования для сделок в рублях с фиксированной ставкой 
        # -----------------------------------------------------------------------------------
        if self.CCY == "RUB" and self.RATE_TYPE == "FIX":
            etc_for = DataLoader.etc_for_basis(self.engine, term=self.TERM, basis=self.BASIS)
            eva_pahom = DataLoader.eva_pahom(
                self.engine,
                product_cd=self.MAP_PRODUCT_EVA[self.PRODUCT],
                term=self.TERM,
                vol=self.VOL,
            )
            if etc_for is None or eva_pahom is None:
                return None
            rate_approve = etc_for - eva_pahom - self.ASV + self.CRL_FINORG
            return round(float(rate_approve) * 100, 2)

        return None
