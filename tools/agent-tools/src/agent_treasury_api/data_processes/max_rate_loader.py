import os
import uuid
import logging
import requests
import pandas as pd
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from .helpers import get_term_label_lim_rates

from ...utils.logger_config import configure_logging


configure_logging()
logger = logging.getLogger("---ForeignLimitRatesLoader---")


# Маппинг продуктов с опциональностью
PRODUCT_OPTIONALITY_MAP: dict[str, str] = {
    "D_OTZ": "OTZ",
    "D_POP": "POP",
    "D_POP_OTZ": "POP_OTZ",
}

CCY_LIST: list[str] = ["INR", "CNY"]
CCY_START_DATE: dict[str, str] = {
    "CNY": "2026-05-07",
    "INR": "2025-08-07",
}
IB_FLG_LIST: list[bool] = [False, True]


def _build_session(retries: int = 3, backoff_factor: float = 0.5) -> requests.Session:
    """Создаёт сессию с retry"""
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class ForeignLimitRatesLoader:

    def __init__(self) -> None:
        self.session: requests.Session = _build_session()

    
    @staticmethod
    def _prepare_dataset(
        rates_response: dict,
        ccy_cd: str,
        ib_flg: int,
    ) -> pd.DataFrame:

        rates_df = pd.DataFrame(rates_response["rates"])

        rates_df["startDate"] = pd.to_datetime(rates_df["startDate"])
        rates_df["term"] = rates_df["term"].astype(int)
        rates_df["minAmt"] = rates_df["minAmt"].astype(float)
        rates_df["maxAmt"] = rates_df["maxAmt"].astype(float)
        rates_df["rateValue"] = rates_df["rateValue"].astype(float) / 100

        rates_df["term_cd"] = rates_df["term"].apply(get_term_label_lim_rates)
        rates_df["ccy_cd"] = ccy_cd
        rates_df["ib_flg"] = ib_flg

        mask = rates_df["product"].isin(PRODUCT_OPTIONALITY_MAP)
        rates_df.loc[mask, "optionality"] = rates_df["product"].map(
            PRODUCT_OPTIONALITY_MAP
        )
        rates_df.loc[mask, "product"] = "D"

        rates_df = rates_df[rates_df["startDate"] == rates_df["startDate"].max()]

        rates_df = rates_df.drop(columns=["createDate"], errors="ignore")
        rates_df = rates_df.sort_values(by="term").reset_index(drop=True)
        return rates_df

    
    def _fetch_rates(self, ccy_cd: str, ib_flg: bool) -> dict:
        headers = {
            "sberpdi": "PALM_PSS_RMK_SERVICE_RATE_CONSUMER",
            "RqUID": uuid.uuid4().hex,
        }
        body = {
            "filter": {
                "rateType": "MAX",
                "date": CCY_START_DATE[ccy_cd],
                "currency": ccy_cd,
                "ibFlg": ib_flg,
            },
            "sort": {
                "sortBy": "term",
                "sortType": "ASC",
            },
        }

        response = self.session.post(
            os.getenv("RMK_RATES_SERVICE_URL"),
            json=body,
            headers=headers,
            verify=False,
            timeout=int(os.getenv("RMK_RATES_SERVICE_TIMEOUT")),
        )
        response.raise_for_status()
        return response.json()

    def load(self) -> pd.DataFrame:

        frames: list[pd.DataFrame] = []

        for ccy_cd in CCY_LIST:
            for ib_flg in IB_FLG_LIST:
                try:
                    raw = self._fetch_rates(ccy_cd, ib_flg)
                    df = self._prepare_dataset(raw, ccy_cd=ccy_cd, ib_flg=int(ib_flg))
                    frames.append(df)
                except requests.RequestException as exc:
                    logger.error(
                        "Ошибка при загрузке ставок ccy=%s ib_flg=%s: %s",
                        ccy_cd,
                        ib_flg,
                        exc,
                    )

        if not frames:
            logger.warning("Не удалось загрузить ни одного набора ставок")
            return pd.DataFrame()

        return pd.concat(frames, ignore_index=True)
