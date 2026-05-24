from datetime import date
from typing import Optional, List, Dict, Any, Union, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

# -------------------------------------------------------------------
# Модели API
# -------------------------------------------------------------------
class UnifiedRequest(BaseModel):
    """
    Универсальный запрос для инструментов расчета
    """
    model_config = ConfigDict(extra="ignore")

    deal_id: Optional[str] = Field(
        None,
        max_length=100,
        description="Идентификатор сделки."
    )

    prefetched_row: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Предзагруженные данные (кэш)."
    )

class ReportRequest(BaseModel):
    period_start: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Начало периода в формате YYYY-MM-DD."
    )
    period_end: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Конец периода в формате YYYY-MM-DD."
    )
    inns: Optional[List[str]] = Field(
        None,
        max_length=100,  # Максимум 100 ИНН за раз
        description="Список ИНН для фильтрации."
    )

class PipelineResponse(BaseModel):
    status: str = Field(..., pattern="^(success|error)$")
    data: Optional[Union[Dict[str, Any], str]] = None
    error: Optional[str] = None

class ComponentsResponse(BaseModel):
    components: Dict[str, Any]
    explain_map: Dict[str, Any]
    inputs_used: Dict[str, Any]
    found_deal: Dict[str, Any]


class GetRateRequest(BaseModel):
    """Входные параметры для расчёта процентной ставки."""

    inn: str = Field(
        ...,
        min_length=9,
        max_length=12,
        description="ИНН контрагента (от 9 до 12 символов)",
    )
    ccy: Literal["RUB", "CNY", "INR"] = Field(
        "RUB",
        description="Валюта",
    )
    product: Literal["DEPO", "NSO"] = Field(
        "DEPO",
        description="Тип продукта",
    )
    term: int = Field(
        ...,
        ge=1,
        description="Срок сделки в днях",
    )
    vol: float = Field(
        ...,
        gt=0,
        description="Объем сделки",
    )
    rate_type: Literal["FIX", "FLOAT"] = Field(
        "FIX",
        description="Тип ставки: фиксированная / плавающая",
    )
    basis: Literal["MONTH", "QUARTAL", "SEMIANNUAL", "ANNUAL", "END"] = Field(
        "END",
        description="Базис начисления процентов",
    )
    optionality: Literal["", "OTZ", "POP", "POP_OTZ"] = Field(
        "",
        description="Опциональность: без опционов / OTZ / POP / POP_OTZ",
    )

    @model_validator(mode="after")
    def validate_per_currency(self) -> "GetRateRequest":
        """Per-currency term and volume limits."""
        match self.ccy:
            case "RUB":
                if not (1 <= self.term <= 1096):
                    raise ValueError(f"RUB: срок {self.term} вне диапазона 1–1096")
                if not (500e6 <= self.vol <= 15e9):
                    raise ValueError(f"RUB: объём {self.vol} вне диапазона 500M–15B")
            case "CNY":
                if not (1 <= self.term <= 732):
                    raise ValueError(f"CNY: срок {self.term} вне диапазона 1–732")
                if not (30e3 <= self.vol <= 500e6):
                    raise ValueError(f"CNY: объём {self.vol} вне диапазона 30K–500M")
            case "INR":
                if not (1 <= self.term <= 366):
                    raise ValueError(f"INR: срок {self.term} вне диапазона 1–366")
                if not (1e6 <= self.vol <= 4_999_999_999):
                    raise ValueError(f"INR: объём {self.vol} вне диапазона 1M–4999M")
        return self


class GetRateResponse(BaseModel):
    """Результат расчёта ставки.

    rates — словарь источников ставок, отсортированный по возрастанию.
    Для RUB: до трёх ключей (calc_rate, hist_rate, model_rate).
    Для CNY/INR: единственный ключ calc_rate.
    """

    rates: list[tuple[str, float]] = Field(
        ...,
        description="Ставки по источникам, отсортированные по возрастанию (% годовых). "
                    "Каждый элемент — (source_key, rate_value).",
    )


class KpkRequest(BaseModel):
    report_dt: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    report_dt_from: str | None = None
    division_cd: str | None = None
    delta_amt: float | None = None
    include_details: bool = False
    today_override: str | None = None

class InvestigateDealRequest(BaseModel):
    inn_num: str
    value_dt: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    as_of_dt: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    delta_amt: float | None = None
    division_cd: str | None = None
    lookback_days: int = 0

class ClientHistoryRequest(BaseModel):
    inn_num: str
    division_cd: str
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    lookback_days: int = Field(default=7, ge=1, le=31)

    @model_validator(mode="after")
    def validate_dates(self) -> "ClientHistoryRequest":
        if not self.date and not self.date_to:
            raise ValueError("Either date or date_to must be provided")
        return self
