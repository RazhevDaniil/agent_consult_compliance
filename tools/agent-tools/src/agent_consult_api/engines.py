from __future__ import annotations

import os
import logging
import numpy as np
import pandas as pd
from decimal import Decimal
from datetime import date, datetime

from ..utils.logger_config import configure_logging

configure_logging()
log = logging.getLogger("---Engines---")


def days_in_year_for_basis(d: date | str | None = None) -> int:
    """
    Вернет 365 или 366 для года, в к-й попадает дата d
    """
    if d is None:
        d = date.today()
    elif isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()

    y = d.year
    return 366 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 365


def Decimal_(value, default=Decimal('0')):
    """Безопасное преобразование в Decimal"""
    if value is None or value == 'None':
        return default
    try:
        # Заменяем запятые на точки и убираем пробелы
        if isinstance(value, str):
            value = value.replace(',', '.').strip()
        else:
            value = str(value)
            value = value.replace(',', '.').strip()
        return Decimal(str(value))
    except (ValueError, TypeError):
        return default


class PricingEngine:

    def __init__(
            self,
            data: dict,
            indicators
    ):
        self.data = data # снэпшот сделки или расчета

        self.option_price = Decimal_(str(indicators.option_price())) or Decimal("0.0") # стоимость опциона
        self.crl = Decimal_(str(indicators.crl())) or Decimal("0.0") # компонента СРЛ (стоимость регуляторной ликвидности)
        self.nor = Decimal_(str(indicators.nor())) or Decimal("0.045") # компонента НОР (норматив обязательного резервирования)

        self.target_eva = Decimal_(str(indicators.target_eva())) or Decimal("0.0") # целевая EVA
        self.indic_eva = Decimal_(str(indicators.indic_eva())) or Decimal("0.0") # индикативная EVA

        ets_dict = indicators.ets() or {'ets_zc': Decimal("0.0"), "ets_zc_rub": Decimal("0.0")} # компонента ЕТС (единая трансфертная ставка)
        self.ets = Decimal_(str(ets_dict['ets_zc'])) # ЕТС по параметрам расчета/сделки
        self.ets_rub = Decimal_(str(ets_dict['ets_zc_rub'])) # ЕТС RUB для расчета ФОР

        self.OPEX_COMPONENT = Decimal_("0.0") # на текущий момент компонента OPEX при ценообразовании пассивов равна 0
        self.ACB_COMPONENT = Decimal_("0.0048") # применима для клиентов из реестра МСП
        self.CHZK = Decimal_("0.0") # ЧЗЭК - чистые затраты на экономический капитал по Сделке при ценообразовании пассивов равна 0

        log.info('--- PricingEngine has been inited ---')


    # 1) ФОР
    def for_rate(self) -> Dict[str, Any]:
        """НОР * ЕТС rub в базисе расчета (даже для валютных сделок)"""
        log.info('--- START FOR ---')
        _for = self.ets_rub * self.nor
        log.info('--- END FOR ---')
        return {
            "for_rate": _for,
            "explain": f"ФОР({_for:.2f}) = ЕТС RUB({self.ets_rub:.2f}) * НОР({self.nor:.2f})"
        }


    # 2) Стоимость фондирования
    def funding_rate(self) -> Dict[str, Any]:
        """Стоимость фондирования: ЕТС в базисе расчета – ФОР – Стоимость опционов +  СРЛ"""
        log.info('--- START FundRate ---')
        for_rate = self.for_rate()['for_rate']
        fund_rate = self.ets - for_rate - self.option_price + self.crl

        is_equal_to_data = fund_rate == Decimal_(str(self.data['fund_rate']))
        log.info('--- END FundRate ---')
        return {
            "is_equal_to_data": is_equal_to_data,
            "funding_rate": float(fund_rate),
            "explain": f"СФ({fund_rate * 100:.5f}) = ЕТС({self.ets * 100:.5f}) - ФОР({for_rate * 100:.5f}) - Стоимость опционов({self.option_price * 100:.5f}) + СРЛ({self.crl * 100:.5f})"
        }


    # 3) Фактическая EVA по сделке, в % годовых
    def eva_rate(self) -> Dict[str, Any]:
        """Фактическая EVA по сделке: Стоимость фондирования - Ставка по сделке - АСВ - OPEX - ЧЗЭК"""

        log.info('--- START EVA RATE ---')
        for_rate = self.for_rate()['for_rate']
        asv = Decimal_(self.ACB_COMPONENT if self.data['msp_flg'] == 1 else 0.0)
        client_rate = Decimal_(str(self.data['interest_rate']))

        eva_rate = self.ets - for_rate - self.option_price + self.crl - client_rate - self.OPEX_COMPONENT - asv - self.CHZK

        is_equal_to_data = eva_rate == Decimal_(str(self.data['eva_rate']))
        log.info('--- END EVA RATE ---')
        return {
            "is_equal_to_data": is_equal_to_data,
            "eva_rate": float(eva_rate),
            "explain": f"Фактическая EVA по сделке({eva_rate * 100:.5f}) = ЕТС({self.ets * 100:.5f}) - ФОР({for_rate * 100:.5f}) - Стоимость опционов({self.option_price * 100:.5f}) + СРЛ({self.crl * 100:.5f}) - Ставка по сделке({client_rate*100:.5f}) - OPEX({self.OPEX_COMPONENT*100:.5f}) - АСВ({asv*100:.5f}) - ЧЗЭК({self.CHZK:.5f})"
        }


    # 4) Ставка безубыточности
    def break_even(self) -> Dict[str, Any]:
        """ЕТС в базисе расчета – ФОР – Стоимость опционов + СРЛ - OPEX – АСВ (для клиентов МСП)"""
        log.info('--- START BREAKEVEN RATE ---')
        for_rate = self.for_rate()['for_rate']
        asv = Decimal_(str((self.ACB_COMPONENT if self.data['msp_flg']==1 else 0.0)))

        be_rate = self.ets - for_rate - self.option_price + self.crl - self.OPEX_COMPONENT - asv
        log.info('--- END BREAKEVEN RATE ---')
        return {
            "break_even_rate": float(be_rate),
            "explain": f"BE({be_rate * 100:.5f}) = ЕТС({self.ets * 100:.5f}) - ФОР({for_rate * 100:.5f}) - Стоимость опционов({self.option_price * 100:.5f}) + СРЛ({self.crl * 100:.5f}) - OPEX({self.OPEX_COMPONENT * 100:.5f}) - АСВ({asv * 100:.5f})"
        }


    # 5) Ставка неутилизирующая лимит
    def non_utilizing(self) -> Dict[str, Any]:
        """ЕТС в базисе расчета – ФОР – Стоимость опционов + СРЛ - OPEX – АСВ (для клиентов МСП) – минимум из (Целевой/индикативной Eva)"""
        log.info('--- START NUL RATE ---')
        for_rate = self.for_rate()['for_rate']
        asv = Decimal_(str((self.ACB_COMPONENT if self.data['msp_flg']==1 else 0.0)))
        min_eva = min(self.target_eva, self.indic_eva)

        nu_rate = self.ets - for_rate - self.option_price + self.crl - self.OPEX_COMPONENT - asv - min_eva

        is_equal_to_data = nu_rate == Decimal_(str(self.data['max_rate']))
        log.info('--- END NUL RATE ---')
        return {
            "is_equal_to_data": is_equal_to_data,
            "non_utilizing_rate": float(nu_rate),
            "explain": f"NU({nu_rate * 100:.2f}) = ЕТС({self.ets * 100:.2f}) - ФОР({for_rate * 100:.2f}) - Стоимость опционов({self.option_price * 100:.2f}) + СРЛ({self.crl * 100:.2f}) - OPEX({self.OPEX_COMPONENT * 100:.2f}) - АСВ({asv * 100:.5f}) - min(Целевая EVA({self.target_eva * 100:.5f}); Индикативная EVA({self.indic_eva * 100:.5f}))"
        }


    # 6) Маржинальный доход
    def marginal_income(self) -> Dict[str, Any]:
        """Сумма сделки * Фактическая Eva (%) * количество дней сделки/кол-во дней в году"""
        log.info('--- START MARGINAL INCOME ---')
        if self.data['eva_rate'] is not None:
            eva_rate = Decimal_(str(self.data['eva_rate']))
        else:
            eva_rate = Decimal_(str(self.eva_rate()['eva_rate']))
        diy = int(days_in_year_for_basis(self.data['deal_dt']))
        mi = Decimal_(str(self.data['deal_amt'])) * eva_rate * int(self.data['term']) / diy
        log.info('--- END MARGINAL INCOME ---')
        return {
            "marginal_income": float(mi),
            "explain": f"Маржинальный доход({mi:.2f}) = Сумма({self.data['deal_amt']:.2f}) * Фактическая Eva({eva_rate * 100:.2f}) * Срок({self.data['term']:.2f}) / кол-во дней в году({diy})"
        }


    # 7) Целевой маржинальный доход
    def target_marginal_income(self) -> Dict[str, Any]:
        """Сумма сделки * Целевая Eva (%) * количество дней сделки/кол-во дней в году"""
        log.info('--- START TARGET MARGINAL INCOME ---')
        diy = int(days_in_year_for_basis(self.data['deal_dt']))
        tmi = Decimal_(str(self.data['deal_amt'])) * self.target_eva * int(self.data['term']) / diy
        log.info('--- END TARGET MARGINAL INCOME ---')
        return {
            "target_marginal_income": float(tmi),
            "explain": f"Целевой маржинальный доход({tmi:.2f}) = Сумма({self.data['deal_amt']:.2f}) * Целевая Eva({self.target_eva * 100:.2f}) * Срок({self.data['term']:.2f}) / кол-во дней в году({diy})"
        }


class LimitEngine:

    def __init__(
            self,
            data: dict,
            indicators
    ):
        self.data = data # снэпшот сделки/расчета

        self.diy = int(days_in_year_for_basis(self.data['deal_dt']))

        self.eva_rate = Decimal_(str(self.data['eva_rate'])) or Decimal("0.0") # фактическая EVA
        self.target_eva = Decimal_(str(indicators.target_eva())) or Decimal("0.0") # целевая EVA
        self.indic_eva = Decimal_(str(indicators.indic_eva())) or Decimal("0.0") # индикативная EVA
        self.eva_diff = Decimal_(str((self.eva_rate - min(self.target_eva, self.indic_eva))))

        self.LIMIT_COEF = Decimal_(os.getenv('LIMIT_COEF', "0.9924")) # коэффициент дисконтирования лимитов
        self.CA_LIMIT_DISCOUNT = Decimal_(os.getenv('CA_LIMIT_DISCOUNT', "0.1")) # доля ЦА

        self.K = 2 if (self.data['product_cd'] == "DEPO" and self.data['optionality_type'] in ("POP", "OTZ_POP")) else 1

        log.info('--- Limit Engine inited ---')

    # 1) Расчет влияния на лимит при котировании
    def limit_impact_quote(self) -> Dict[str, Any]:
        """
        для случаев если value_dt = deal_dt для DEPO и случаев если value_dt – deal_dt <= 1 для NSO:
        Сумма сделки * (Фактическая Eva (%)- минимум из (Целевой/индикативной Eva (%) ) * количество дней сделки/кол-во дней в году

        для случаев если value_dt – deal_dt > 0 для DEPO и случаев если value_dt - deal_dt > 1 для NSO:
        Сумма сделки * (Фактическая Eva (%) - минимум из (Целевой/индикативной Eva (%) ) * количество дней сделки/кол-во дней в году * 0.9924^ (deal_dt – value_dt)
        """
        log.info("--- START Limit Impact Quote ---")
        dd = (pd.to_datetime(self.data['value_dt']) - pd.to_datetime(self.data['deal_dt'])).days

        vol = Decimal_(str(self.data['deal_amt']))
        term = int(self.data['term'])

        if (self.data['product_cd'] == "DEPO" and dd == 0) or (self.data['product_cd'] == "NSO" and dd <= 1):
            limit_impact = self.K * vol * self.eva_diff * term / self.diy
            log.info("--- END Limit Impact Quote ---")
            return {
                "limit_impact": float(limit_impact),
                "condition": "для случаев если value_dt = deal_dt для DEPO и случаев если value_dt – deal_dt <= 1 для NSO",
                "explain": f"Влияние на лимит при котировании({limit_impact:.2f}) = k({self.K}) * Сумма({vol:.2f}) * (Фактическая EVA({self.eva_rate * 100:.5f})- min(Целевая EVA({self.target_eva * 100:5f}); Индикативная EVA({self.indic_eva * 100:.5f})) * Срок({term})/кол-во дней в году({self.diy})"
            }

        elif (self.data['product_cd'] == "DEPO" and dd > 0) or (self.data['product_cd'] == "NSO" and dd > 1):
            inverse_dd = (pd.to_datetime(self.data['deal_dt']) - pd.to_datetime(self.data['value_dt'])).days
            if self.data['product_cd'] == "NSO":
                inverse_dd += 1
            limit_impact = (self.K * vol * self.eva_diff * term / self.diy) * self.LIMIT_COEF**inverse_dd

            if limit_impact <= 0:
                log.info("--- END Limit Impact Quote ---")
                return {
                    "limit_impact": float(limit_impact),
                    "condition": "для случаев если value_dt – deal_dt > 0 для DEPO и случаев если value_dt - deal_dt > 1 для NSO:",
                    "explain": f"Влияние на лимит при котировании({limit_impact:.2f}) = (k({self.K}) * Сумма({vol:.2f}) * (Фактическая EVA({self.eva_rate * 100:.5f})- min(Целевая EVA({self.target_eva * 100:.5f}); Индикативная EVA({self.indic_eva * 100:.5f})) * Срок({term})/кол-во дней в году({self.diy})) * {self.LIMIT_COEF:.2f}^(deal_dt – value_dt ({inverse_dd}))"
                }
            else:
                limit_impact = self.K * vol * self.eva_diff * term / self.diy
                log.info("--- END Limit Impact Quote ---")
                return {
                    "limit_impact": float(limit_impact),
                    "condition": f"для случаев если value_dt – deal_dt > 0 для DEPO и случаев если value_dt - deal_dt > 1 для NSO и влияние на лимит ({limit_impact:.2f}) > 0",
                    "explain": f"Влияние на лимит при котировании({limit_impact:.2f}) = k({self.K}) * Сумма({vol:.2f}) * (Фактическая EVA({self.eva_rate * 100:.5f})- min(Целевая EVA({self.target_eva * 100:.5f}); Индикативная EVA({self.indic_eva * 100:.5f})) * Срок({term})/кол-во дней в году({self.diy})"
                }

        else:
            log.info("--- END Limit Impact Quote: No calculus! ---")
            return "Влияние на лимит не рассчитывается, так как входные параметры противоречат необходимым условиям расчета"


    # 2) Расчет ежедневного влияния на лимит уже заключенной сделки (расчета)
    def limit_impact_active_deal(self) -> Dict[str, Any]:

        log.info("--- START Limit Impact Active Deal ---")

        if "delta_limit_amt" not in self.data.keys():
            log.info("--- END Limit Impact Active Deal. No calculus! ---")
            return "Влияние на лимит не рассчитывается, так как входные параметры противоречат необходимым условиям расчета"

        data_delta_limit_amt = Decimal_(str(self.data['delta_limit_amt']))

        log.info(f"Влияние на лимит по сделке в БД: {data_delta_limit_amt:.2f}")

        if "original_delta_limit_amt" in self.data.keys():
            log.info(f"--- Get original_delta_limit_amt {self.data['original_delta_limit_amt']} from initial deal data ---")
            original_deltalimit = Decimal_(str(self.data['original_delta_limit_amt']))
        else:
            limit_quote_info = self.limit_impact_quote()
            if isinstance(limit_quote_info, dict):
                original_deltalimit = limit_quote_info['limit_impact']
                log.info(f"--- Calculate by ourselves original_delta_limit_amt {original_deltalimit} ---")
            else:
                log.info("--- END Limit Impact Active Deal. No calculus! ---")
                return "Влияние на лимит не рассчитывается, так как входные параметры противоречат необходимым условиям расчета"

        if "calc_limit_dt" in self.data.keys() and self.data['calc_limit_dt'] is not None:
            calc_dt = pd.to_datetime(self.data['calc_limit_dt'])
            if self.data['product_cd'] == 'DEPO':
                calc_dt += pd.Timedelta(days=5)
            else:
                calc_dt += pd.Timedelta(days=5)
        else:
            calc_dt = pd.to_datetime(date.today())

        dd = Decimal_(str((calc_dt - pd.to_datetime(self.data['value_dt'])).days))
        dd_deal = Decimal_(str((calc_dt - pd.to_datetime(self.data['deal_dt'])).days))
        maturity = Decimal_(str((pd.to_datetime(self.data['end_dt']) - pd.to_datetime(self.data['value_dt'])).days))

        if self.data['product_cd'] == 'NSO':
            dd += 1
            dd_deal += 1
            maturity += 1

        # Если сделка с положительным влиянием на лимит
        if original_deltalimit > 0:

            # А) Действующая сделка с положительным влиянием на лимит
            if calc_dt <= pd.to_datetime(self.data['end_dt']) and ('termination_dt' not in self.data.keys() or self.data['termination_dt']  is None):

                # 1) Влияние на лимит по сделке
                delta_limit = Decimal_(str((original_deltalimit * (self.LIMIT_COEF**dd) * (dd - 1) / maturity)))
                if calc_dt < pd.to_datetime(self.data['value_dt']):
                    delta_limit = 0.0

                log.info(f"--- Limit Impact on deal: {delta_limit:.2f} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}. Дата расчета лимита по сделке {calc_dt:.2f} <= дате окончания {self.data['value_dt']}",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd})) * (({dd}) - 1) / разница между датой окончания и датой валютирования ({maturity})"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

            # Б) Сделка с положительным влиянием на лимит после даты окончания
            if calc_dt > pd.to_datetime(self.data['end_dt']) and ('termination_dt' not in self.data.keys() or self.data['termination_dt']  is None):

                # 1) Влияние на лимит по сделке
                delta_limit = Decimal_(str((original_deltalimit * (self.LIMIT_COEF**dd))))

                log.info(f"--- Deal limit impact: {delta_limit:.2f} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}. Дата расчета лимита по сделке {calc_dt} > даты окончания {self.data['value_dt']}",
                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

            # B) Сделки и расчеты с положительным влиянием на лимит отозванные досрочно
            if 'termination_dt' in self.data.keys() and self.data['termination_dt'] is not None:
                termination_dt = pd.to_datetime(self.data['termination_dt'])

                # 1) Влияние на лимит по сделке
                delta_limit = original_deltalimit * ((termination_dt - pd.to_datetime(self.data['value_dt'])).days) / maturity * (self.LIMIT_COEF**dd)

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}, отозванные досрочно {self.data['termination_dt']}",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * разница между датой отзыва и датой валютирования ({(termination_dt - pd.to_datetime(self.data['value_dt'])).days}) / разница между датой окончания и датой валютирования ({maturity}:.2f) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

        # Если сделка с отрицательным влиянием на лимит
        elif original_deltalimit < 0:

            deal_value_dd = (pd.to_datetime(self.data['value_dt'])-pd.to_datetime(self.data['deal_dt'])).days

            # А) Сделка с отрицательным влиянием на лимит (без будущей даты валютирования)
            if (self.data['product_cd'] == 'DEPO' and deal_value_dd == 0) or (self.data['product_cd'] == 'NSO' and deal_value_dd <= 1):
                # 1) Влияние на лимит по сделке
                delta_limit = original_deltalimit * (self.LIMIT_COEF**dd)

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit# * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = 0.0 # delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с отрицательным влиянием на лимит: {original_deltalimit:.2f} без будущей даты валютирования",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f})"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Отрицательное влияние сделки на лимит не аллоцируется на ЦА."
                    }
                }

            # Б) Сделка с отрицательным влиянием на лимит (с будущей датой валютирования)
            elif (self.data['product_cd'] == 'DEPO' and deal_value_dd > 0) or (self.data['product_cd'] == 'NSO' and deal_value_dd > 1):
                vol = Decimal_(str(self.data['deal_amt']))
                term = int(self.data['term'])
                ts_to = (pd.to_datetime(self.data['deal_dt'])-pd.to_datetime(self.data['value_dt'])).days

                if self.data['product_cd'] == 'NSO':
                    ts_to += 1

                # 1) Влияние на лимит по сделке
                delta_limit = (self.K * self.eva_diff * vol * term / self.diy) * (self.LIMIT_COEF**ts_to) * (self.LIMIT_COEF**dd_deal)

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit# * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = 0.0#delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с отрицательным влиянием на лимит: {original_deltalimit:.2f} с будущей датой валютирования",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = (K (k = 2, по депозиту с опцией пополнения и delta_limit < 0, в остальных случаях = 1) * (Фактическая EVA ({self.eva_rate:.2f}) - min(Целевая EVA ({self.target_eva:.2f}), Индикативная EVA ({self.indic_eva:.2f}))) * Сумма ({vol:.2f}) * Срок ({term:.2f})/ К-во дней в году ({self.diy:.2f})) * коэф дисконтирования в степени разницы даты договора и даты валютирования (({self.LIMIT_COEF})^({ts_to})) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f})"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Отрицательное влияние сделки на лимит не аллоцируется на ЦА."
                    }
                }


    def limit_timetable(self):

        log.info("--- Start Limit Timetable ---")

        value_dt = pd.to_datetime(self.data['value_dt'])
        end_dt = pd.to_datetime(self.data['end_dt'])
        
        if value_dt <= end_dt:
            deal_dates = [pd.to_datetime(str(d).split(' ')[0]) for d in list(pd.date_range(value_dt, end_dt))]
        else:
            deal_dates = [value_dt]

        lim_graph_dict = {}

        for calc_dt in deal_dates:

            df_d = self.data.copy()
            if isinstance(df_d, dict):
                log.info(f"--- Get data for date {calc_dt} ---")
            else:
                df_d = df_d.to_dict(orient='records')
            df_d['calc_limit_dt'] = calc_dt

            res = self.limit_impact_active_deal_for_timetable(df_d)

            if isinstance(res, dict):
                lim_graph_dict[str(calc_dt).split(' ')[0]] = res
            else:
                log.info(f"--- FOR DATE {lim_graph_dict[str(calc_dt).split(' ')[0]]} calculation limits has been failed ---")
                pass

        l_dict = {}
        for k,v in lim_graph_dict.items():
            l_dict[str(k).split(' ')[0]] = {'Влияние на лимит действующей сделки': v['deltaLimitAmt']['value']}

        return l_dict


    # 2) Расчет ежедневного влияния на лимит уже заключенной сделки (расчета)
    def limit_impact_active_deal_for_timetable(self, df_d) -> Dict[str, Any]:

        if "delta_limit_amt" not in df_d.keys():
            log.info("--- END Limit Impact Active Deal. No calculus! ---")
            return "Влияние на лимит не рассчитывается, так как входные параметры противоречат необходимым условиям расчета"

        data_delta_limit_amt = Decimal_(str(df_d['delta_limit_amt']))

        log.info(f"--- Limit impact on deal in DB: {data_delta_limit_amt:.2f} ---")

        if "original_delta_limit_amt" in df_d.keys():
            log.info(f"--- Get original_delta_limit_amt {df_d['original_delta_limit_amt']:.2f} from initial deal data ---")
            original_deltalimit = Decimal_(str(df_d['original_delta_limit_amt']))
        else:
            log.info("--- Calculate by ourselves ---")
            limit_quote_info = Decimal_(str(self.limit_impact_quote()))
            if isinstance(limit_quote_info, dict):
                original_deltalimit = Decimal_(str(limit_quote_info['limit_impact']))
                log.info(f"--- Calculate by ourselves original_delta_limit_amt {original_deltalimit:.2f} ---")
            else:
                log.info("--- END Limit Impact Active Deal. No calculus! ---")
                return "Влияние на лимит не рассчитывается, так как входные параметры противоречат необходимым условиям расчета"

        if "calc_limit_dt" in df_d.keys() and df_d['calc_limit_dt'] is not None:
            calc_dt = pd.to_datetime(df_d['calc_limit_dt'])
            if df_d['product_cd'] == 'DEPO':
                
                calc_dt += pd.Timedelta(days=5)
            else:
                calc_dt += pd.Timedelta(days=5)
        else:
            calc_dt = pd.to_datetime(date.today())

        try:
            dd = (calc_dt - pd.to_datetime(df_d['value_dt'])).days
            dd_deal = (calc_dt - pd.to_datetime(df_d['deal_dt'])).days
            maturity = (pd.to_datetime(df_d['end_dt']) - pd.to_datetime(df_d['value_dt'])).days
            log.info(f"--- DD: {dd} ---")
        except Exception as e:
            log.error(e)

        if df_d['product_cd'] == 'NSO':
            dd += 1
            dd_deal += 1
            maturity += 1

        # Если сделка с положительным влиянием на лимит
        if original_deltalimit >= Decimal('0'):

            # А) Действующая сделка с положительным влиянием на лимит
            if calc_dt <= pd.to_datetime(df_d['end_dt']) and ('termination_dt' not in df_d.keys() or df_d['termination_dt'] is None):

                # 1) Влияние на лимит по сделке
                delta_limit = (original_deltalimit * (self.LIMIT_COEF**dd) * (dd - 1) / maturity)
                if calc_dt < pd.to_datetime(df_d['value_dt']):
                    delta_limit = 0.0

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}. Дата расчета лимита по сделке {calc_dt:.2f} <= дате окончания {df_d['value_dt']}",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd})) * (({dd}) - 1) / разница между датой окончания и датой валютирования ({maturity})"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

            # Б) Сделка с положительным влиянием на лимит после даты окончания
            if calc_dt > pd.to_datetime(df_d['end_dt']) and ('termination_dt' not in df_d.keys() or df_d['termination_dt'] is None):

                # 1) Влияние на лимит по сделке
                delta_limit = Decimal_(str((original_deltalimit * (self.LIMIT_COEF**dd))))

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}. Дата расчета лимита по сделке {calc_dt} > даты окончания {df_d['value_dt']}",
                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

            # B) Сделки и расчеты с положительным влиянием на лимит отозванные досрочно
            if 'termination_dt' in df_d.keys() and df_d['termination_dt'] is not None:
                termination_dt = pd.to_datetime(df_d['termination_dt'])

                # 1) Влияние на лимит по сделке
                delta_limit = Decimal_(str(original_deltalimit * ((termination_dt - pd.to_datetime(df_d['value_dt'])).days) / maturity * (self.LIMIT_COEF**dd)))

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с положительным влиянием на лимит: {original_deltalimit:.2f}, отозванные досрочно {df_d['termination_dt']}",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * разница между датой отзыва и датой валютирования ({(termination_dt - pd.to_datetime(df_d['value_dt'])).days}) / разница между датой окончания и датой валютирования ({maturity}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,9"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Влияние на лимит ЦА действующей сделки ({delta_limit_ca:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f}) * 0,1"
                    }
                }

        # Если сделка с отрицательным влиянием на лимит
        elif original_deltalimit < Decimal('0'):

            deal_value_dd = (pd.to_datetime(df_d['value_dt'])-pd.to_datetime(df_d['deal_dt'])).days

            # А) Сделка с отрицательным влиянием на лимит (без будущей даты валютирования)
            if (df_d['product_cd'] == 'DEPO' and deal_value_dd == 0) or (df_d['product_cd'] == 'NSO' and deal_value_dd <= 1):
                # 1) Влияние на лимит по сделке
                delta_limit = Decimal_(str(original_deltalimit * (self.LIMIT_COEF**dd)))

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit# * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = 0.0#delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с отрицательным влиянием на лимит: {original_deltalimit:.2f} без будущей даты валютирования",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = влиение на лимит при котировании ({original_deltalimit:.2f}) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f})"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Отрицательное влияние сделки на лимит не аллоцируется на ЦА."
                    }
                }

            # Б) Сделка с отрицательным влиянием на лимит (с будущей датой валютирования)
            elif (df_d['product_cd'] == 'DEPO' and deal_value_dd > 0) or (df_d['product_cd'] == 'NSO' and deal_value_dd > 1):
                vol = Decimal_(str(df_d['deal_amt']))
                term = int(df_d['term'])
                ts_to = (pd.to_datetime(df_d['deal_dt'])-pd.to_datetime(df_d['value_dt'])).days

                if df_d['product_cd'] == 'NSO':
                    ts_to += 1

                # 1) Влияние на лимит по сделке
                delta_limit = (self.K * self.eva_diff * vol * term / self.diy) * (self.LIMIT_COEF**ts_to) * (self.LIMIT_COEF**dd_deal)

                log.info(f"--- Deal limit impact: {delta_limit} ---")

                #                 if delta_limit != data_delta_limit_amt:
                #                     return "Рассчитанный лимит не равен тому, что указан в базе данных => по сделке изменилось влияние и необходим анализ на стороне специалиста"

                # 2) влияние сделки на лимит КПК
                delta_limit_kpk = delta_limit# * (1 - self.CA_LIMIT_DISCOUNT)

                # 3) влияние сделки на лимит ЦА
                delta_limit_ca = 0.0#delta_limit * self.CA_LIMIT_DISCOUNT

                log.info("--- END Limit Impact Active Deal ---")
                return {
                    "condition": f"Сделка с отрицательным влиянием на лимит: {original_deltalimit} с будущей датой валютирования",

                    "deltaLimitAmt" : {
                        "value": float(delta_limit),
                        "explanation" : f"Влияние на лимит действующей сделки ({delta_limit:.2f}) = (K (k = 2, по депозиту с опцией пополнения и delta_limit < 0, в остальных случаях = 1) * (Фактическая EVA ({self.eva_rate:.5f}) - min(Целевая EVA ({self.target_eva:.5f}), Индикативная EVA ({self.indic_eva:.5f}))) * Сумма ({vol:.5f}) * Срок ({term})/ К-во дней в году ({self.diy})) * коэф дисконтирования в степени разницы даты договора и даты валютирования (({self.LIMIT_COEF})^({ts_to})) * коэф дисконтирования в степени разницы даты расчета лимита и даты валютирования (({self.LIMIT_COEF})^({dd}))"
                    },
                    "deltaLimitAmtKpk" : {
                        "value": float(delta_limit_kpk),
                        "explanation" : f"Влияние на лимит КПК действующей сделки ({delta_limit_kpk:.2f}) = влияние на лимит действующей сделки ({delta_limit:.2f})"
                    },
                    "deltaLimitAmtCa" : {
                        "value": float(delta_limit_ca),
                        "explanation" : f"Отрицательное влияние сделки на лимит не аллоцируется на ЦА."
                    }
                }
