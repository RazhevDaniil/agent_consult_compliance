from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, ConfigDict
from pydantic.alias_generators import to_camel


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str = Field(..., min_length=1)


MessagePart = TextPart


class A2AMessage(BaseModel):
    kind: Literal["message"] = "message"
    messageId: str = Field(..., min_length=1)
    contextId: Optional[str] = None
    taskId: Optional[str] = None
    role: Literal["user", "agent"]
    parts: List[MessagePart] = Field(..., min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class A2AArtifact(BaseModel):
    parts: List[MessagePart] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    completed = "completed"
    failed = "failed"


class TaskStatus(BaseModel):
    state: TaskState
    message: Optional[A2AMessage] = None


class A2ATask(BaseModel):
    kind: Literal["task"] = "task"
    id: str
    contextId: str
    status: TaskStatus
    history: List[A2AMessage] = Field(default_factory=list)
    artifacts: List[A2AArtifact] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class A2ARequestConfiguration(BaseModel):
    acceptedOutputModes: List[str] = Field(default_factory=list)
    blocking: bool = False
    historyLength: Optional[int] = None


class MessageSendParams(BaseModel):
    message: A2AMessage
    contextId: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    configuration: A2ARequestConfiguration = Field(default_factory=A2ARequestConfiguration)


class TasksGetParams(BaseModel):
    id: str
    contextId: Optional[str] = None
    configuration: A2ARequestConfiguration = Field(default_factory=A2ARequestConfiguration)


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Union[str, int]
    method: Literal["message/send", "tasks/get"]
    params: Dict[str, Any]


class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Union[str, int]
    result: Union[A2ATask, A2AMessage]


class JsonRpcErrorData(BaseModel):
    details: Optional[Any] = None


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: Optional[JsonRpcErrorData] = None


class JsonRpcErrorResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    error: JsonRpcError


class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=32000,
        description="Текст запроса пользователя. Не может быть пустым.",
    )
    chat_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Уникальный идентификатор чата (сессии).",
    )


class ChatResponse(BaseModel):
    answer: str = Field(..., description="Сгенерированный ответ. При state=input-required — текст уточняющего вопроса.")
    destination: str = Field(
        ...,
        max_length=50,
        description="Маршрут, по которому пошел запрос (rag_methodology, unsupported_calculation и т.д.).",
    )
    confidence: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="Оценка уверенности модели от 0 до 10.",
    )
    sources: List[str] = Field(default_factory=list, description="Список файлов-источников.")
    state: Literal["completed", "input-required", "failed"] = Field(
        default="completed",
        description="Итоговый статус задачи. completed — готово, input-required — агент ждёт уточнения, failed — ошибка.",
    )
    suggests: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Готовые варианты продолжения диалога для UI.",
    )


# ──────────────────────────────────────────────
# Agent Card models
# ──────────────────────────────────────────────


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)


class AgentCapabilities(BaseModel):
    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")

    model_config = ConfigDict(populate_by_name=True)


class AgentCard(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    protocol_version: str = "0.3.0"
    name: str
    description: str
    url: str
    version: str
    default_input_modes: List[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: List[str] = Field(default_factory=lambda: ["application/json"])
    capabilities: AgentCapabilities
    skills: List[AgentSkill]


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: A2ATask
    created_from_trace_id: str
    last_state: TaskState
    last_transition_at: float
