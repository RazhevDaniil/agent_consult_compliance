"""Ставка на основе исторических сделок клиента."""

from __future__ import annotations

import logging
from sqlalchemy import Engine

from .data_loader import DataLoader

from ...utils.logger_config import configure_logging


configure_logging()
logger = logging.getLogger("---HistRateGetter---")


class HistRateGetter:
    """Рассчитывает ставку по историческому спреду клиента.

    Делегирует чтение и фильтрацию DataLoader.hist_spread() —
    по аналогии с тем, как RateCalculator работает с DataLoader.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        inn: str,
        ccy: str,
        term: int,
        vol: float,
        fund_rate: float,
    ) -> None:
        self.engine = engine
        self.inn = inn
        self.ccy = ccy
        self.term = term
        self.vol = vol
        self.fund_rate = fund_rate

    def rate(self) -> float | None:
        """Возвращает ставку = (fundRate - wmean_spread) * 100, или None."""
        try:
            spread = DataLoader.hist_spread(
                self.engine,
                inn=self.inn,
                term=self.term,
                vol=self.vol,
            )
            if spread is None:
                logger.info("hist_rate_no_spread")
                return None
            rate = self.fund_rate - spread
            return round(rate * 100, 2)
        except Exception as e:
            logger.warning(f"hist_rate_error: {e}")
            return None
