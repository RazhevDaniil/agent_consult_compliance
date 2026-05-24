"""Ставка от ML-модели (API котировщика)."""

from __future__ import annotations

import os
import requests
import urllib3
import logging

from ...utils.logger_config import configure_logging


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

configure_logging()
logger = logging.getLogger("---ModelRateGetter---")


class ModelRateGetter:
    """Получает ставку от ML-модели через HTTP API.

    Формирует payload и отправляет POST-запрос к сервису котировщика.
    Возвращает ставку или None при ошибке / недоступности.
    """

    def __init__(
        self,
        *,
        term: int,
        ccy: str,
        inn: str,
        amount: float,
        product: str,
        limit_rate: float | None,
        max_rate: float | None,
        fund_rate: float | None,
    ) -> None:
        self.payload = {
            "Term": term,
            "CCY": ccy,
            "CRM_ID": None,
            "INN": inn,
            "Amount": amount,
            "ProdLevel2": product,       # "DEPO" / "NSO"
            "LimitRate": limit_rate,
            "MaxRate": max_rate,
            "FundRate": fund_rate,
        }

    def rate(self) -> float | None:
        """Отправляет запрос к ML API и возвращает ставку."""
        try:
            resp = requests.post(
                os.getenv("MODEL_INFERENCE_URL"),
                json=self.payload,
                timeout=float(os.getenv("MODEL_INFERENCE_TIMEOUT", "120")),
            )
            resp.raise_for_status()
            data = resp.json()
            rate_value = data.get("rate")
            if rate_value is not None:
                return round(rate_value * 100, 2)
            logger.warning(f"model_rate_no_rate_in_response: {data}")
            return None
        except Exception as e:
            logger.warning(f"model_rate_error: {e}")
            return None
