from __future__ import annotations

import calendar
import datetime
import numpy as np
import pandas as pd

from .helpers import get_term_label

# ---------------------------------------------------------------------------
# Калькулятор пересчета ставки ЕТС из месячного в другие базисы (квартальный, полугодовой, годовой, в конце срока)
# ---------------------------------------------------------------------------
class ETCBasisCalculator:

    def __init__(self, last_data_dt: str | None = None) -> None:
        self.date = last_data_dt
        self.round_const = 6
        self.n = {
            "MONTH": 1 / 12,
            "QUARTAL": 1 / 4,
            "SEMIANNUAL": 1 / 2,
            "ANNUAL": 1,
        }

    def days_in_current_month(self, value_dt: str) -> int:
        dt = pd.to_datetime(value_dt)
        return calendar.monthrange(dt.year, dt.month)[1]

    def days_in_current_year(self, value_dt: str) -> int:
        return 366 if calendar.isleap(pd.to_datetime(value_dt).year) else 365

    def set_t(self, value_dt: str | None = None) -> float:
        if value_dt:
            m = self.days_in_current_month(value_dt)
            n = self.days_in_current_year(value_dt)
            return m / n
        return 1 / 12

    def set_n(self, term: int, term_type: str = "days", basis: str | None = None) -> float:
        if not basis:
            return term / 365 if term_type == "days" else term / 12
        return self.n[basis]

    def etc_basis(self, etc: float, term: int, term_type: str = "days", basis: str | None = None) -> float:
        if not etc:
            return 0.0
        etc = float(etc)
        if not basis and (not term or term < 32):
            return etc
        value_dt = self.date or str(datetime.date.today())
        t = self.set_t(value_dt)
        n = self.set_n(term, term_type=term_type, basis=basis)
        return np.round((((1 + etc * t) ** (n / t) - 1) * 1 / n), self.round_const)


def calc_for(etc_value: float, nor_value: float = 0.045) -> float:
    if not etc_value:
        return 0.0
    if not nor_value:
        nor_value = 0.045
    if nor_value > 1:
        nor_value /= 100
    return etc_value * nor_value


def etc_rate_preprocessing(etc: pd.DataFrame, date: str, nor_rate: float = 0.045) -> pd.DataFrame:
    """Интерполяция ETC на все сроки [1..1096] + расчёт FOR по всем базисам."""
    etc = etc.copy()
    etc["term_day_cnt"] = etc["term_day_cnt"].astype(int)
    etc["ftp_rate"] = etc["ftp_rate"].astype(float) / 100

    base_terms = list(etc["term_day_cnt"].unique())
    all_terms = list(range(1, max(base_terms) + 1))
    etc_values = etc["ftp_rate"]
    interpolated = [np.interp(i, base_terms, etc_values) for i in all_terms]

    etc_int = pd.DataFrame()
    etc_int["term_day_cnt"] = all_terms
    etc_int["term_cd"] = etc_int["term_day_cnt"].apply(get_term_label)
    etc_int["ftp_month"] = interpolated

    calc = ETCBasisCalculator(last_data_dt=date)

    etc_int["ftp_zc"] = etc_int.apply(lambda r: calc.etc_basis(r["ftp_month"], r["term_day_cnt"]), axis=1)
    etc_int["ftp_quartal"] = etc_int.apply(lambda r: calc.etc_basis(r["ftp_month"], r["term_day_cnt"], basis="QUARTAL"), axis=1)
    etc_int["ftp_semiannual"] = etc_int.apply(lambda r: calc.etc_basis(r["ftp_month"], r["term_day_cnt"], basis="SEMIANNUAL"), axis=1)
    etc_int["ftp_annual"] = etc_int.apply(lambda r: calc.etc_basis(r["ftp_month"], r["term_day_cnt"], basis="ANNUAL"), axis=1)

    for basis in ("month", "zc", "quartal", "semiannual", "annual"):
        etc_int[f"for_{basis}"] = etc_int[f"ftp_{basis}"].apply(lambda x: calc_for(x, nor_rate))
        etc_int[f"ftp_for_{basis}"] = etc_int[f"ftp_{basis}"] - etc_int[f"for_{basis}"]

    return etc_int
