import os
import uuid
import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from .vault import vault_finorgs, vault_subjects

from ...trino.trino_client import run_trino
from ...trino.sql_queries_bank import SENS_FLG_BY_INN

from ...utils.logger_config import configure_logging


configure_logging()
logger = logging.getLogger("---ClientInfoService---")


@dataclass
class ClientInfo:
    is_ip: int = 0
    is_msp: int = 0
    segment: str = "KSB"
    is_sense_eva: int = 0
    is_fin_org: int = 0
    is_subject: int = 0
    name: str = ""


def normalize_inn(inn: str) -> str:
    """Нормализация ИНН: дополнение нулями до 10 или 12 символов."""
    inn = str(inn).strip()
    if len(inn) < 10:
        inn = inn.zfill(10)
    elif len(inn) == 11:
        inn = inn.zfill(12)
    return inn


def get_rqtm():
    return datetime.now(timezone(timedelta(hours=3))).isoformat(timespec='milliseconds')


class CLIENT_INFO:

    def __init__(
        self,
        inn: str
    ):
        if not inn:
            return None

        self.INN = normalize_inn(inn)
        
        self.CLIENT = self.clientInfoByInn()

        if self.CLIENT is None:
            self.IS_IP = int(len(inn) > 11)
            self.IS_MSP = 0
            self.SEGMENT = "KSB"
            self.NAME = "UnknownOrganization"
            return

        if 'isIp' in self.CLIENT.keys():
            self.IS_IP = int(self.CLIENT['isIp'])
        else:
            self.IS_IP = int(len(inn) > 11)

        if 'isMsp' in self.CLIENT.keys():
            self.IS_MSP = int(self.CLIENT['isMsp'])
        else:
            self.IS_MSP = 0

        if 'segment' in self.CLIENT.keys():
            self.SEGMENT = self.CLIENT['segment']
        else:
            self.SEGMENT = "KSB"

        if "name" in self.CLIENT.keys():
            self.NAME = str(self.CLIENT['name'])
        else:
            self.NAME = "UnknownOrganization"


    def clientInfoByInn(self):
        
        headers_clients = {
            "RqUID": str(uuid.uuid4()),
            "RqTm": get_rqtm(),
            "SpName": "urn:sbrfsystems:99-fcalc",
            "SystemId": "urn:sbrfsystems:99-fcalc",
            "CalcType": "DEPO",
        }
        
        body_clients = {
            "strategy": "INN",
            "searchBy": [
                {
                    "filterType": "inn",
                    "filterValue": self.INN
                }
            ]
        }
        try:
            response = requests.post(
                os.getenv("CLIENTS_DATA_SERVICE_URL"),
                json=body_clients,
                headers=headers_clients,
                verify=False,
                timeout=float(os.getenv("CLIENT_SERVICE_TIMEOUT", "60"))
            )
            response.raise_for_status()
            clients_info = response.json()
            return clients_info["clients"][0]['clientInfo']

        except Exception as e:
            logger.warning(f"client_info_request_failed: {e}")
            return None


class ClientDataService:
    """Получение клиентских атрибутов по ИНН"""

    def __init__(
        self,
        inn: str
    ):

        self.INN = inn
        self.CLIENT_INFO = CLIENT_INFO(self.INN)

    def get_client_info(self) -> ClientInfo:
        
        is_fin_org = vault_finorgs.check(self.INN)
        is_subject = vault_subjects.check(self.INN)

        return ClientInfo(
            is_ip=self.CLIENT_INFO.IS_IP,
            is_msp=self.CLIENT_INFO.IS_MSP,
            segment=self.CLIENT_INFO.SEGMENT,
            is_sense_eva=self._get_is_sense_eva(self.INN),
            is_fin_org=int(is_fin_org),
            is_subject=int(is_subject),
            name=self.CLIENT_INFO.NAME,
        )

    def _get_is_sense_eva(self, inn: str) -> int:
        """Получение флага чувствительности по ИНН
        """
        try:
            sens_flg_df = run_trino(
                SENS_FLG_BY_INN.format(inn),
                "con_hadoop_foton",
                "custom_fin_palm_dataops"
            )
            if not sens_flg_df.empty:
                return sens_flg_df['sens_flg'].unique()[-1]
            logger.warning("sens_flg_empty_dataset")
            return 0
        except Exception as e:
            logger.warning(f"sens_flg_request_failed: {e}")
            return 0
