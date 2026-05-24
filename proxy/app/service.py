import time
import uuid
import asyncio
import logging
from typing import Optional, Union

import httpx
from fastapi import HTTPException

from .store import store
from .config import settings
from .models import (
    A2AArtifact,
    A2AMessage,
    A2ATask,
    ChatRequest,
    ChatResponse,
    JsonRpcError,
    JsonRpcErrorData,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcSuccess,
    MessageSendParams,
    TaskEnvelope,
    TaskState,
    TaskStatus,
    TasksGetParams,
    TextPart,
)


logger = logging.getLogger(__name__)


class JsonRpcException(Exception):
    def __init__(self, code: int, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class A2AProxyService:
    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()

    async def handle_rpc(
        self,
        request: JsonRpcRequest,
        x_trace_id: Optional[str],
        x_session_id: Optional[str],
    ) -> Union[JsonRpcSuccess, JsonRpcErrorResponse]:
        rpc_id = str(request.id)
        trace_id = x_trace_id or str(uuid.uuid4())

        logger.debug(
            "handle_rpc | method=%s rpc_id=%s trace_id=%s",
            request.method,
            rpc_id,
            trace_id,
        )

        if request.method == "message/send":
            params = MessageSendParams.model_validate(request.params)
            return await self._handle_message_send(request, params, x_session_id, trace_id)

        if request.method == "tasks/get":
            params = TasksGetParams.model_validate(request.params)
            return await self._handle_tasks_get(request, params, x_session_id)

        logger.warning("Unsupported method | method=%s rpc_id=%s", request.method, rpc_id)
        raise JsonRpcException(code=-32601, message=f"Метод {request.method} не поддерживается")

    async def _handle_message_send(
        self,
        request: JsonRpcRequest,
        params: MessageSendParams,
        x_session_id: Optional[str],
        trace_id: str,
    ) -> JsonRpcSuccess:
        user_message = params.message
        incoming_task_id = user_message.taskId

        existing_history: list[A2AMessage] = []

        if incoming_task_id:
            # Resume after input-required
            envelope = await store.get(incoming_task_id)
            if envelope is None:
                logger.warning("Resume failed: task not found | task_id=%s", incoming_task_id)
                raise JsonRpcException(
                    code=-32004,
                    message=f"Task {incoming_task_id} не найдена",
                )
            if envelope.last_state != TaskState.input_required:
                logger.warning(
                    "Resume failed: task not in input-required state | task_id=%s state=%s",
                    incoming_task_id,
                    envelope.last_state.value,
                )
                raise JsonRpcException(
                    code=-32009,
                    message=(
                        f"Задача {incoming_task_id} не ожидает уточнения "
                        f"(текущий статус: {envelope.last_state.value})"
                    ),
                )
            # защита от concurrent resume
            claimed = await store.claim_for_resume(incoming_task_id, TaskState.input_required)
            if claimed is None:
                raise JsonRpcException(
                    code=-32009,
                    message=f"Задача {incoming_task_id} уже обрабатывается другим запросом",
                )
            task_id = incoming_task_id
            context_id = claimed.task.contextId
            existing_history = list(claimed.task.history)
            logger.info(
                "Resuming task | task_id=%s context_id=%s history_len=%d",
                task_id,
                context_id,
                len(existing_history),
            )
        else:
            # New task
            task_id = f"task-{uuid.uuid4()}"
            context_id = self._resolve_context_id(params, x_session_id)
            logger.info("New task | task_id=%s context_id=%s", task_id, context_id)

        normalized_message = A2AMessage(
            kind="message",
            messageId=user_message.messageId,
            contextId=context_id,
            taskId=task_id,
            role="user",
            parts=user_message.parts,
            metadata=user_message.metadata,
        )

        history = existing_history + [normalized_message]

        initial_task = self._build_task(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.submitted,
            history=history,
            status_text="Задача принята в обработку.",
        )
        await store.upsert(
            TaskEnvelope(
                task=initial_task,
                created_from_trace_id=trace_id,
                last_state=TaskState.submitted,
                last_transition_at=time.time(),
            )
        )

        task_runner = asyncio.create_task(
            self._execute_downstream_call(
                rpc_id=str(request.id),
                trace_id=trace_id,
                context_id=context_id,
                task_id=task_id,
                user_message=normalized_message,
                history=history,
            ),
            name=f"downstream-{task_id}",
        )
        self._background_tasks.add(task_runner)
        task_runner.add_done_callback(self._background_tasks.discard)

        if params.configuration.blocking:
            logger.debug("Blocking wait started | task_id=%s timeout=%.1fs", task_id, settings.blocking_wait_timeout_seconds)
            try:
                await asyncio.wait_for(task_runner, timeout=settings.blocking_wait_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning("Blocking wait timeout | task_id=%s", task_id)
            envelope = await store.get(task_id)
            if envelope is None:
                raise JsonRpcException(code=-32004, message="Задача не найдена после запуска")
            logger.info(
                "message/send blocking complete | task_id=%s state=%s",
                task_id,
                envelope.last_state.value,
            )
            return JsonRpcSuccess(id=request.id, result=self._trim_history(envelope.task, params.configuration.historyLength))

        envelope = await store.get(task_id)
        if envelope is None:
            raise JsonRpcException(code=-32004, message="Задача не найдена после запуска")
        logger.debug("message/send non-blocking | task_id=%s state=%s", task_id, envelope.last_state.value)
        return JsonRpcSuccess(id=request.id, result=self._trim_history(envelope.task, params.configuration.historyLength))

    async def _handle_tasks_get(
        self,
        request: JsonRpcRequest,
        params: TasksGetParams,
        x_session_id: Optional[str],
    ) -> JsonRpcSuccess:
        logger.debug(
            "tasks/get | task_id=%s blocking=%s",
            params.id,
            params.configuration.blocking,
        )
        envelope = await store.get(params.id)
        if envelope is None:
            logger.warning("tasks/get: task not found | task_id=%s", params.id)
            raise JsonRpcException(code=-32004, message=f"Task {params.id} не найдена")

        if params.contextId and envelope.task.contextId != params.contextId:
            raise JsonRpcException(
                code=-32602,
                message="Передан неверный contextId для tasks/get",
                details={
                    "expected": envelope.task.contextId,
                    "got": params.contextId,
                },
            )

        if x_session_id and envelope.task.contextId != x_session_id:
            logger.warning(
                "x-session-id does not match stored contextId: x-session-id=%s contextId=%s",
                x_session_id,
                envelope.task.contextId,
            )

        if params.configuration.blocking and envelope.last_state not in {
            TaskState.completed,
            TaskState.failed,
            TaskState.input_required,
        }:
            logger.debug(
                "tasks/get long-poll started | task_id=%s current_state=%s timeout=%.1fs",
                params.id,
                envelope.last_state.value,
                settings.tasks_get_long_poll_seconds,
            )
            envelope = await store.wait_for_change(
                task_id=params.id,
                previous_state=envelope.last_state,
                timeout_seconds=settings.tasks_get_long_poll_seconds,
            )
            if envelope is None:
                raise JsonRpcException(code=-32004, message=f"Task {params.id} не найдена")
            logger.debug(
                "tasks/get long-poll resolved | task_id=%s new_state=%s",
                params.id,
                envelope.last_state.value,
            )

        logger.debug("tasks/get result | task_id=%s state=%s", params.id, envelope.last_state.value)

        return JsonRpcSuccess(id=request.id, result=self._trim_history(envelope.task, params.configuration.historyLength))

    async def _execute_downstream_call(
        self,
        rpc_id: str,
        trace_id: str,
        context_id: str,
        task_id: str,
        user_message: A2AMessage,
        history: list[A2AMessage],
    ) -> None:
        working_task = self._build_task(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.working,
            history=history,
            status_text="Идет обработка запроса внешним агентом.",
            metadata={
                "currentAgent": settings.downstream_depo_agent_url,
                "progressPercent": 25,
                "responseMode": "fast",
            },
        )
        await store.upsert(
            TaskEnvelope(
                task=working_task,
                created_from_trace_id=trace_id,
                last_state=TaskState.working,
                last_transition_at=time.time(),
            )
        )

        user_text = "\n".join(part.text for part in user_message.parts if part.kind == "text").strip()
        if not user_text:
            logger.warning("Empty user text | task_id=%s context_id=%s", task_id, context_id)
            await self._save_failed_task(
                rpc_id=rpc_id,
                trace_id=trace_id,
                task_id=task_id,
                context_id=context_id,
                history=history,
                error_text="Пустой текст запроса пользователя.",
            )
            return

        chat_request = ChatRequest(message=user_text, chat_id=context_id)

        logger.info(
            "Calling downstream agent | task_id=%s url=%s",
            task_id,
            settings.downstream_depo_agent_url,
        )

        try:
            async with httpx.AsyncClient(timeout=settings.downstream_timeout_seconds) as client:
                response = await client.post(
                    url=f"{settings.downstream_depo_agent_url}/chat",
                    json=chat_request.model_dump(),
                    headers={
                        "x-trace-id": trace_id,
                        "X-Agent-Audience": "km_from_fox",
                        "X-Source-System": "orchestrator-proxy",
                    },
                )
                response.raise_for_status()
                agent_response = ChatResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Downstream HTTP error | task_id=%s status=%d body=%.200s",
                task_id,
                exc.response.status_code,
                exc.response.text,
            )
            await self._save_failed_task(
                rpc_id=rpc_id,
                trace_id=trace_id,
                task_id=task_id,
                context_id=context_id,
                history=history,
                error_text=f"Внутренний агент вернул HTTP {exc.response.status_code}.",
                error_details=exc.response.text,
            )
            return
        except (httpx.RequestError, ValueError) as exc:
            logger.error(
                "Downstream request error | task_id=%s error=%s",
                task_id,
                exc,
            )
            await self._save_failed_task(
                rpc_id=rpc_id,
                trace_id=trace_id,
                task_id=task_id,
                context_id=context_id,
                history=history,
                error_text="Не удалось получить корректный ответ от внутреннего агента.",
                error_details=str(exc),
            )
            return

        logger.info(
            "Downstream response | task_id=%s state=%s destination=%s",
            task_id,
            agent_response.state,
            agent_response.destination,
        )

        # Append agent reply to history regardless of state
        agent_message = A2AMessage(
            kind="message",
            messageId=f"msg-{uuid.uuid4()}",
            contextId=context_id,
            taskId=task_id,
            role="agent",
            parts=[TextPart(text=agent_response.answer)],
            metadata={},
        )
        full_history = history + [agent_message]

        if agent_response.state == "input-required":
            logger.info("Task requires clarification | task_id=%s context_id=%s", task_id, context_id)
            input_required_task = self._build_task(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.input_required,
                history=full_history,
                status_text=agent_response.answer,
            )
            await store.upsert(
                TaskEnvelope(
                    task=input_required_task,
                    created_from_trace_id=trace_id,
                    last_state=TaskState.input_required,
                    last_transition_at=time.time(),
                )
            )

        elif agent_response.state == "failed":
            logger.error(
                "Downstream agent returned failed state | task_id=%s answer=%.200s",
                task_id,
                agent_response.answer,
            )
            await self._save_failed_task(
                rpc_id=rpc_id,
                trace_id=trace_id,
                task_id=task_id,
                context_id=context_id,
                history=full_history,
                error_text=agent_response.answer,
            )

        else:
            logger.info(
                "Task completed | task_id=%s context_id=%s destination=%s confidence=%s",
                task_id,
                context_id,
                agent_response.destination,
                agent_response.confidence,
            )
            final_task = self._build_task(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.completed,
                history=full_history,
                status_text="Запрос выполнен.",
                artifacts=[
                    A2AArtifact(
                        parts=[TextPart(text=agent_response.answer)],
                        metadata={
                            "destination": agent_response.destination,
                            "confidence": agent_response.confidence,
                            "sources": agent_response.sources,
                            "suggests": agent_response.suggests,
                        },
                    )
                ],
                metadata={
                    "currentAgent": settings.downstream_depo_agent_url,
                    "progressPercent": 100,
                    "responseMode": "fast",
                    "destination": agent_response.destination,
                    "confidence": agent_response.confidence,
                    "sources": agent_response.sources,
                    "suggests": agent_response.suggests,
                },
            )
            await store.upsert(
                TaskEnvelope(
                    task=final_task,
                    created_from_trace_id=trace_id,
                    last_state=TaskState.completed,
                    last_transition_at=time.time(),
                )
            )

    async def _save_failed_task(
        self,
        rpc_id: str,
        trace_id: str,
        task_id: str,
        context_id: str,
        history: list[A2AMessage],
        error_text: str,
        error_details: Optional[str] = None,
    ) -> None:
        logger.error(
            "Task failed | task_id=%s context_id=%s error=%s details=%.200s",
            task_id,
            context_id,
            error_text,
            error_details,
        )
        failed_task = self._build_task(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.failed,
            history=history,
            status_text=error_text,
            artifacts=[],
            metadata={
                "currentAgent": settings.downstream_depo_agent_url,
                "progressPercent": 100,
                "errorDetails": error_details,
            },
        )
        await store.upsert(
            TaskEnvelope(
                task=failed_task,
                created_from_trace_id=trace_id,
                last_state=TaskState.failed,
                last_transition_at=time.time(),
            )
        )

    def _resolve_context_id(self, params: MessageSendParams, x_session_id: Optional[str]) -> str:
        explicit_context_id = params.message.contextId or params.contextId
        if explicit_context_id and x_session_id and explicit_context_id != x_session_id:
            logger.warning(
                "Context mismatch, using A2A contextId as source of truth: body=%s x-session-id=%s",
                explicit_context_id,
                x_session_id,
            )
            return explicit_context_id
        if explicit_context_id:
            return explicit_context_id
        if x_session_id:
            return x_session_id
        return str(uuid.uuid4())

    def _build_task(
        self,
        task_id: str,
        context_id: str,
        state: TaskState,
        history: list[A2AMessage],
        status_text: str,
        artifacts: Optional[list[A2AArtifact]] = None,
        metadata: Optional[dict] = None,
    ) -> A2ATask:
        status_message = A2AMessage(
            kind="message",
            messageId=f"msg-{uuid.uuid4()}",
            contextId=context_id,
            taskId=task_id,
            role="agent",
            parts=[TextPart(text=status_text)],
            metadata={},
        )
        return A2ATask(
            id=task_id,
            contextId=context_id,
            status=TaskStatus(state=state, message=status_message),
            history=history,
            artifacts=artifacts or [],
            metadata=metadata or {},
        )

    @staticmethod
    def _trim_history(task: A2ATask, history_length: Optional[int]) -> A2ATask:
        if history_length is not None and len(task.history) > history_length:
            return task.model_copy(update={"history": task.history[-history_length:]})
        return task

    @staticmethod
    def to_http_exception(exc: JsonRpcException, rpc_id: object | None) -> HTTPException:
        payload = JsonRpcErrorResponse(
            id=rpc_id,
            error=JsonRpcError(
                code=exc.code,
                message=exc.message,
                data=JsonRpcErrorData(details=exc.details) if exc.details is not None else None,
            ),
        )
        return HTTPException(status_code=400, detail=payload.model_dump())


service = A2AProxyService()
