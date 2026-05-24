import time
import asyncio
import logging
from typing import Dict, Optional

from .models import TaskEnvelope, TaskState


logger = logging.getLogger(__name__)


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: Dict[str, TaskEnvelope] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, envelope: TaskEnvelope) -> None:
        async with self._lock:
            existing = self._tasks.get(envelope.task.id)
            state_changed = existing is None or existing.last_state != envelope.last_state
            self._tasks[envelope.task.id] = envelope
            event = self._events.setdefault(envelope.task.id, asyncio.Event())
            if state_changed:
                old_state = existing.last_state.value if existing else None
                logger.debug(
                    "Task state changed | task_id=%s %s -> %s",
                    envelope.task.id,
                    old_state,
                    envelope.last_state.value,
                )
                event.set()
            else:
                logger.debug("Task upserted (no state change) | task_id=%s state=%s", envelope.task.id, envelope.last_state.value)

    async def get(self, task_id: str) -> Optional[TaskEnvelope]:
        async with self._lock:
            envelope = self._tasks.get(task_id)
            logger.debug("Task get | task_id=%s found=%s", task_id, envelope is not None)
            return envelope

    async def claim_for_resume(self, task_id: str, expected_state: TaskState) -> Optional[TaskEnvelope]:
        """Атомарно проверяет состояние задачи и переводит в working (защита от concurrent resume)."""
        async with self._lock:
            envelope = self._tasks.get(task_id)
            if envelope is None:
                return None
            if envelope.last_state != expected_state:
                return None
            import time as _time
            claimed = envelope.model_copy(update={"last_state": TaskState.working, "last_transition_at": _time.time()})
            self._tasks[task_id] = claimed
            return claimed

    async def wait_for_change(
        self,
        task_id: str,
        previous_state: Optional[TaskState],
        timeout_seconds: float,
    ) -> Optional[TaskEnvelope]:
        async with self._lock:
            envelope = self._tasks.get(task_id)
            if envelope is None:
                logger.debug("wait_for_change: task not found | task_id=%s", task_id)
                return None
            if previous_state is None or envelope.last_state != previous_state:
                logger.debug(
                    "wait_for_change: state already changed | task_id=%s state=%s",
                    task_id,
                    envelope.last_state.value,
                )
                return envelope
            # Создаём новый Event под lock — upsert увидит его и сделает set()
            event = asyncio.Event()
            self._events[task_id] = event

        logger.debug(
            "wait_for_change: waiting | task_id=%s previous_state=%s timeout=%.1fs",
            task_id,
            previous_state.value if previous_state else None,
            timeout_seconds,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            logger.debug("wait_for_change: event received | task_id=%s", task_id)
        except asyncio.TimeoutError:
            logger.debug(
                "wait_for_change: timeout | task_id=%s after=%.1fs",
                task_id,
                timeout_seconds,
            )

        async with self._lock:
            return self._tasks.get(task_id)

    async def cleanup(self, ttl_seconds: int) -> None:
        cutoff = time.time() - ttl_seconds
        async with self._lock:
            expired_ids = [
                task_id
                for task_id, envelope in self._tasks.items()
                if envelope.last_transition_at < cutoff
            ]
            for task_id in expired_ids:
                self._tasks.pop(task_id, None)
                self._events.pop(task_id, None)

        if expired_ids:
            logger.info("Cleanup: removed %d expired tasks | ttl=%ds", len(expired_ids), ttl_seconds)
        else:
            logger.debug("Cleanup: no expired tasks | ttl=%ds", ttl_seconds)


store = InMemoryTaskStore()
