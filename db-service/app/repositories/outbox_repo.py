import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.outbox_task import OutboxTask
from ..logging_config import get_logger

log = get_logger("app.repo.outbox")


def _is_sqlite(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "sqlite"


class OutboxRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_type: str,
        task_key: str,
        email_id: str | None = None,
        payload_json: dict | None = None,
        run_id: str | None = None,
    ) -> tuple[OutboxTask, bool]:
        """Insert a task; return (task, created).

        On conflict on task_key does nothing — idempotent.
        """
        log.debug("create.start", task_type=task_type, task_key=task_key, email_id=email_id)
        values = dict(
            task_type=task_type,
            task_key=task_key,
            email_id=email_id,
            payload_json=payload_json,
            status="NEW",
            attempt=0,
            run_id=run_id,
        )

        if _is_sqlite(self._session):
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            existing = await self._session.execute(
                select(OutboxTask).where(OutboxTask.task_key == task_key)
            )
            if (task := existing.scalar_one_or_none()) is not None:
                log.debug("create.conflict_existing", task_key=task_key, task_id=task.id)
                return task, False

            stmt = (
                sqlite_insert(OutboxTask)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["task_key"])
            )
            await self._session.execute(stmt)
            await self._session.flush()

            result = await self._session.execute(
                select(OutboxTask).where(OutboxTask.task_key == task_key)
            )
            task = result.scalar_one()
            log.debug("create.created", task_key=task_key, task_id=task.id)
            return task, True

        stmt = (
            pg_insert(OutboxTask)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["task_key"])
            .returning(OutboxTask)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            log.debug("create.created", task_key=task_key, task_id=row.id)
            return row, True

        existing_result = await self._session.execute(
            select(OutboxTask).where(OutboxTask.task_key == task_key)
        )
        task = existing_result.scalar_one()
        log.debug("create.conflict_existing", task_key=task_key, task_id=task.id)
        return task, False

    async def dequeue(
        self,
        *,
        task_type: str | None = None,
        limit: int = 10,
        claim_ttl_seconds: int = 300,
    ) -> list[OutboxTask]:
        """Claim up to `limit` ready tasks and flip them to PROCESSING.

        Uses SELECT FOR UPDATE SKIP LOCKED on PostgreSQL.
        """
        log.debug("dequeue.query", task_type=task_type, limit=limit)
        now = datetime.now(timezone.utc)

        q = (
            select(OutboxTask)
            .where(OutboxTask.status.in_(["NEW", "RETRYING"]))
            .where(
                (OutboxTask.next_retry_at == None)  # noqa: E711
                | (OutboxTask.next_retry_at <= now)
            )
            .order_by(OutboxTask.created_at.asc())
            .limit(limit)
        )
        if not _is_sqlite(self._session):
            q = q.with_for_update(skip_locked=True)
        if task_type is not None:
            q = q.where(OutboxTask.task_type == task_type)

        result = await self._session.execute(q)
        tasks = list(result.scalars().all())

        if not tasks:
            log.debug("dequeue.empty", task_type=task_type)
            return []

        claim_until = datetime.fromtimestamp(
            now.timestamp() + claim_ttl_seconds, tz=timezone.utc
        )
        ids = [t.id for t in tasks]

        claim_token_value = (
            str(uuid.uuid4())
            if _is_sqlite(self._session)
            else func.gen_random_uuid().cast(OutboxTask.claim_token.type)
        )

        await self._session.execute(
            update(OutboxTask)
            .where(OutboxTask.id.in_(ids))
            .values(
                status="PROCESSING",
                claim_token=claim_token_value,
                claim_until=claim_until,
                attempt=OutboxTask.attempt + 1,
                updated_at=now,
            )
        )

        await self._session.flush()

        refreshed = await self._session.execute(
            select(OutboxTask).where(OutboxTask.id.in_(ids))
        )
        claimed = list(refreshed.scalars().all())
        log.debug("dequeue.claimed", count=len(claimed), task_ids=ids)
        return claimed

    async def update_status(
        self,
        task_id: str,
        *,
        status: str,
        claim_token: str,
        error: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> OutboxTask | None:
        """Update status only when claim_token matches.

        Returns None if the task is not found or the token is stale.
        """
        log.debug("update_status.lookup", task_id=task_id, expected_status=status)
        result = await self._session.execute(
            select(OutboxTask).where(OutboxTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            log.warning("update_status.not_found", task_id=task_id)
            return None
        if task.claim_token != claim_token:
            log.warning(
                "update_status.stale_token",
                task_id=task_id,
                expected=claim_token,
                actual=task.claim_token,
            )
            return None

        old_status = task.status
        task.status = status
        task.next_retry_at = next_retry_at
        task.updated_at = datetime.now(timezone.utc)
        if error is not None:
            payload = dict(task.payload_json or {})
            payload["_last_error"] = error
            task.payload_json = payload

        await self._session.flush()
        log.debug(
            "update_status.updated",
            task_id=task_id,
            old_status=old_status,
            new_status=status,
            has_error=error is not None,
        )
        return task
