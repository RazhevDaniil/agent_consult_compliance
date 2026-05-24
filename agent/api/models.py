from datetime import date
from typing import Literal, List, Optional, Annotated, Dict, Any, Union
from pydantic import BaseModel, Field, StringConstraints, conint, ConfigDict

# =====================================================================================
# классы Pydantic
# =====================================================================================


class _Rank(BaseModel):
    """Модель для реранкера"""
    score: conint(ge=0, le=3) = Field(description="0..3, где 3 — очень релевантно")


class RouteQuery(BaseModel):
    """Решение о маршрутизации запроса пользователя."""
    # destination_reasoning: str = Field(description="Рассуждения куда надо направить запрос")
    destination: Literal["rag_methodology", "unsupported_calculation", "kpk_limits"] = Field(
        description="Узел назначения для обработки запроса."
    )

class RagAnswerWithConfidence(BaseModel):
    """Ответ RAG с человекочитаемыми формулами/шагами."""
    answer_text: str = Field(description="Краткий ответ пользователю.")
    confidence_reasoning: str = Field(description="Рассуждение насколько агент уверен в корректности ответа")
    confidence_score: conint(ge=1, le=10) = Field(description="Оценка уверенности от 1 до 10, насколько ответ полон и точен СТРОГО на основе предоставленного контекста.")
    sources: List[str] = Field(description="Список имён файлов, использованных для ответа (без doc_id).")

class Critique(BaseModel):
    """Модель-критик"""
    is_good: bool = Field(description="True, если ответ соответствует всем правилам, иначе False.")
    feedback: str = Field(description="Конструктивная критика: что именно нужно исправить (краткость, ссылки, галлюцинации и т.д.).")

class RewrittenQuestion(BaseModel):
    """Переформулированный или оригинальный вопрос пользователя."""
    question: str = Field(
        description="Только вопрос, без ответов, пояснений или комментариев. Должен заканчиваться на '?'"
    )

class MinimalContext(BaseModel):
    """Переформулированный или оригинальный вопрос пользователя."""
    is_referal: bool = Field(description="Вопрос отсылается на прошлое сообщение?")
    minimal_context: List[str] = Field(description="К каким понятиям отсылается прошлый вопрос?")


# --- Tool routing ---
class ToolRoute(BaseModel):
    """Какой инструмент вызывать для вопроса пользователя"""
    tool: Literal["pricing", "limits", "deals_report", "none"] = Field(
        description="Какой инструмент вызывать для вопроса пользователя."
    )
    reason: str = Field(description="Короткое объяснение выбора.")

# --- Результат регенерации (ответ+уверенность+источники) ---
class RegenerateResult(BaseModel):
    """Результат регенерации (ответ+уверенность+источники)"""
    answer_text: Optional[str] = None
    confidence_score: Optional[int] = None
    sources: Optional[List[str]] = None



# ---------- Custom type ----------
HexUUID36 = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        min_length=36,
        max_length=36,
    ),
]

# Модели запросов/ответов
class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=32000,
        description="Текст запроса пользователя. Не может быть пустым."
    )
    chat_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Уникальный идентификатор чата (сессии)."
    )


class GeneratedReportMeta(BaseModel):
    kind: Literal["incorrect_deals_report"] = Field(
        ...,
        description="Тип автоматически подготовленного отчета."
    )
    report_dt: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Дата отчета в формате YYYY-MM-DD."
    )
    title: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Человекочитаемое название отчета."
    )


class ChatResponse(BaseModel):
    answer: str = Field(
        ...,
        description="Сгенерированный ответ."
    )
    destination: str = Field(
        ...,
        max_length=50,
        description="Маршрут, по которому пошел запрос (rag_methodology, unsupported_calculation и т.д.)."
    )
    confidence: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="Оценка уверенности модели от 0 до 10."
    )
    sources: List[str] = Field(
        default_factory=list,
        description="Список файлов-источников."
    )
    state: Literal["completed", "input-required", "failed"] = Field(
        default="completed",
        description=(
            "Статус задачи для A2A-протокола. "
            "completed — ответ готов, input-required — агент ждёт уточнения от пользователя, "
            "failed — ошибка обработки."
        ),
    )
    generated_report: Optional[GeneratedReportMeta] = Field(
        default=None,
        description="Метаданные автоматически подготовленного отчета для UI."
    )
    suggests: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Готовые варианты продолжения диалога для UI."
    )


# -------------------------------------------------------------------
# Модели API расчетного модуля на PSS
# -------------------------------------------------------------------

class UnifiedRequest(BaseModel):
    """
    Универсальный запрос для инструментов расчета
    Ограничения соответствуют финансовым данным (суммы > 0, ИНН определенной длины)
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

class IncorrectDealsReportRequest(BaseModel):
    report_dt: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    chat_id: str = Field(..., min_length=1, max_length=64)
