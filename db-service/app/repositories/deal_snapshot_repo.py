from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.deal_snapshot import DealSnapshot
from ..logging_config import get_logger

log = get_logger("app.repo.deal_snapshot")


def _is_sqlite(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "sqlite"


class DealSnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, snapshot_id: str) -> DealSnapshot | None:
        result = await self._session.execute(
            select(DealSnapshot).where(DealSnapshot.id == snapshot_id)
        )
        return result.scalar_one_or_none()

    async def get_by_parsing_log(self, parsing_log_id: str) -> DealSnapshot | None:
        result = await self._session.execute(
            select(DealSnapshot).where(
                DealSnapshot.parsing_log_id == parsing_log_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        thread_id: str,
        deal_number: int,
        iteration: int,
        limit_rate: Decimal,
        policy_rate: Decimal,
        final_rate: Decimal,
        status: str,
        parsing_log_id: str,
        utilizes_limit: bool,
        agent_text: str,
        incoming_conditions: dict | None = None,
        request_rate: Decimal | None = None,
        avg_hist_rate: Decimal | None = None,
        model_rate: Decimal | None = None,
        proposed_conditions: dict | None = None,
        escalation_reason: str | None = None,
        run_id: str | None = None,
    ) -> tuple[DealSnapshot, bool]:
        """Insert a deal snapshot; return (snapshot, created).

        Idempotent on parsing_log_id.
        """
        log.debug(
            "create.start",
            thread_id=thread_id,
            deal_number=deal_number,
            iteration=iteration,
            parsing_log_id=parsing_log_id,
        )
        values = dict(
            thread_id=thread_id,
            deal_number=deal_number,
            iteration=iteration,
            incoming_conditions=incoming_conditions,
            request_rate=request_rate,
            limit_rate=limit_rate,
            avg_hist_rate=avg_hist_rate,
            model_rate=model_rate,
            policy_rate=policy_rate,
            final_rate=final_rate,
            proposed_conditions=proposed_conditions,
            status=status,
            escalation_reason=escalation_reason,
            parsing_log_id=parsing_log_id,
            utilizes_limit=utilizes_limit,
            agent_text=agent_text,
            run_id=run_id,
        )

        if _is_sqlite(self._session):
            existing = await self.get_by_parsing_log(parsing_log_id)
            if existing is not None:
                log.debug("create.conflict_existing", parsing_log_id=parsing_log_id)
                return existing, False

            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = (
                sqlite_insert(DealSnapshot)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["parsing_log_id"])
            )
            await self._session.execute(stmt)
            await self._session.flush()
            row = await self.get_by_parsing_log(parsing_log_id)
            log.debug("create.created", id=row.id)  # type: ignore[union-attr]
            return row, True  # type: ignore[return-value]

        stmt = (
            pg_insert(DealSnapshot)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_ds_parsing_log_id")
            .returning(DealSnapshot)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("create.conflict_existing", parsing_log_id=parsing_log_id)
            existing = await self.get_by_parsing_log(parsing_log_id)
            return existing, False  # type: ignore[return-value]

        log.debug("create.created", id=row.id)
        return row, True

    async def list_by_thread(self, thread_id: str) -> list[DealSnapshot]:
        result = await self._session.execute(
            select(DealSnapshot)
            .where(DealSnapshot.thread_id == thread_id)
            .order_by(DealSnapshot.created_at.asc())
        )
        return list(result.scalars().all())
