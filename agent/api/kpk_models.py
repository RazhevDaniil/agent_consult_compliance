from typing import Literal, List, Optional, Dict, Any
from pydantic import BaseModel, Field, conint

# -------------------------------------------------------------------
# Модели для помощника по изменению лимитов КПК
# -------------------------------------------------------------------

class KpkLimitParsedQuery(BaseModel):
    """Результат разбора пользовательского вопроса про лимит КПК"""

    mode: Literal["single", "all"] = Field(
        description="single — указан конкретный КПК; all — запрос по всем КПК"
    )
    division_cd: Optional[str] = Field(
        default=None,
        description="Код КПК (подразделения) Если mode=all — None"
    )
    report_dt: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Дата анализа в формате YYYY-MM-DD (t, конечная дата)"
    )
    report_dt_from: Optional[str] = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Начальная дата для сравнения двух произвольных дат (t-from). Если не указана — используется t-1"
    )

    # Опционально — если пользователь указал явное изменение/разницу
    delta_amt: Optional[float] = Field(
        default=None,
        description="На какую сумму, по мнению пользователя, изменился лимит (delta=t-(t-1))"
    )

    # Опционально — если пользователь сравнивает 2 значения лимита (вчера/сегодня)
    limit_t: Optional[float] = Field(
        default=None,
        description="Значение лимита на дату t (если явно указано в вопросе)"
    )
    limit_prev: Optional[float] = Field(
        default=None,
        description="Значение лимита на дату t-1 (если явно указано в вопросе)"
    )

    inn_num: Optional[str] = Field(
        default=None,
        description="ИНН клиента (если пользователь указал в вопросе, например при расследовании сделки)"
    )

    lookback_days: Optional[conint(ge=1, le=31)] = Field(
        default=None,
        description="Количество дней для анализа (1-31). Используется при intent=client_history и investigate_deal"
    )

    intent: Literal["factors", "redistribution", "delta_reason", "negative_report", "find_deal", "investigate_deal", "client_history", "other"] = Field(
        default="other",
        description="Тип вопроса: причины/перераспределения/почему на изменение/отчет по отрицательным/расследование сделки/история клиента/прочее"
    )

    confidence: conint(ge=1, le=10) = Field(
        default=7,
        description="Оценка уверенности в корректности разбора (1..10)"
    )


class KpkLimitToolRequest(BaseModel):
    """Запрос в fastapi-tool для анализа лимитов КПК"""

    report_dt: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    report_dt_from: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    division_cd: Optional[str] = None
    delta_amt: Optional[float] = None
    inn_num: Optional[str] = None
    include_details: bool = False
    today_override: Optional[str] = None


class KpkLimitToolResponse(BaseModel):
    """Унифицированный ответ fastapi-tool для анализа лимитов КПК"""

    status: str = Field(..., pattern="^(success|error)$")
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None