from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.approve_task import ApproveTask
from ..logging_config import get_logger

log = get_logger("app.repo.approve_task")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApproveTaskRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, task_id: str) -> ApproveTask | None:
        result = await self._session.execute(
            select(ApproveTask).where(ApproveTask.task_id == task_id)
        )
        return result.scalar_one_or_none()

    async def get_by_calculation_id(
        self, calculation_id: str
    ) -> ApproveTask | None:
        result = await self._session.execute(
            select(ApproveTask).where(
                ApproveTask.calculation_id == calculation_id
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        task_id: str,
        calculation_id: str,
        task_status: str,
        task_dttm: datetime | None = None,
        attempt_count: int = 0,
        agent_answer: str | None = None,
        deal_status: str | None = None,
        error_message: str | None = None,
        run_id: str | None = None,
    ) -> tuple[ApproveTask, bool]:
        """Insert a task; return (task, created). Idempotent on task_id."""
        log.debug("create.start", task_id=task_id, calculation_id=calculation_id)
        values: dict = dict(
            task_id=task_id,
            calculation_id=calculation_id,
            task_status=task_status,
            attempt_count=attempt_count,
            agent_answer=agent_answer,
            deal_status=deal_status,
            error_message=error_message,
            run_id=run_id,
        )
        if task_dttm is not None:
            values["task_dttm"] = task_dttm

        stmt = (
            pg_insert(ApproveTask)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["task_id"])
            .returning(ApproveTask)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("create.conflict_existing", task_id=task_id)
            existing = await self.get(task_id)
            return existing, False  # type: ignore[return-value]

        log.debug("create.created", task_id=task_id)
        return row, True

    async def update_status(
        self,
        task_id: str,
        *,
        task_status: str,
        deal_status: str | None = None,
        agent_answer: str | None = None,
        attempt_count: int | None = None,
        error_message: str | None = None,
        run_id: str | None = None,
    ) -> ApproveTask | None:
        """Update task fields. Returns None if task not found."""
        log.debug("update_status.lookup", task_id=task_id)
        task = await self.get(task_id)
        if task is None:
            log.warning("update_status.not_found", task_id=task_id)
            return None

        old_status = task.task_status
        task.task_status = task_status
        task.updated_at = _now()
        if deal_status is not None:
            task.deal_status = deal_status
        if agent_answer is not None:
            task.agent_answer = agent_answer
        if attempt_count is not None:
            task.attempt_count = attempt_count
        if error_message is not None:
            task.error_message = error_message
        if run_id is not None:
            task.run_id = run_id

        await self._session.flush()
        log.debug(
            "update_status.updated",
            task_id=task_id,
            old_status=old_status,
            new_status=task_status,
        )
        return task

    async def list_by_status(self, task_status: str) -> list[ApproveTask]:
        result = await self._session.execute(
            select(ApproveTask)
            .where(ApproveTask.task_status == task_status)
            .order_by(ApproveTask.task_dttm.asc())
        )
        return list(result.scalars().all())

    async def list_filtered(
        self,
        *,
        task_status: str | None = None,
        calculation_id: str | None = None,
        older_than_minutes: int | None = None,
    ) -> list[ApproveTask]:
        """List tasks with optional filters, sorted by task_dttm ASC.

        older_than_minutes: only return tasks whose updated_at is older than N minutes.
        """
        from datetime import timedelta

        q = select(ApproveTask)
        if task_status is not None:
            q = q.where(ApproveTask.task_status == task_status)
        if calculation_id is not None:
            q = q.where(ApproveTask.calculation_id == calculation_id)
        if older_than_minutes is not None:
            cutoff = _now() - timedelta(minutes=older_than_minutes)
            q = q.where(ApproveTask.updated_at < cutoff)
        q = q.order_by(ApproveTask.task_dttm.asc())
        result = await self._session.execute(q)
        return list(result.scalars().all())
