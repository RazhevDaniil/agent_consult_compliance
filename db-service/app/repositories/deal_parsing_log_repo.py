from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.deal_parsing_log import DealParsingLog
from ..logging_config import get_logger

log = get_logger("app.repo.deal_parsing_log")


def _is_sqlite(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "sqlite"


class DealParsingLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, log_id: str) -> DealParsingLog | None:
        result = await self._session.execute(
            select(DealParsingLog).where(DealParsingLog.id == log_id)
        )
        return result.scalar_one_or_none()

    async def get_by_message_fragment(
        self, message_id: str, fragment_idx: int
    ) -> DealParsingLog | None:
        result = await self._session.execute(
            select(DealParsingLog).where(
                DealParsingLog.message_id == message_id,
                DealParsingLog.fragment_idx == fragment_idx,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        message_id: str,
        fragment_idx: int,
        fragment_text: str,
        extraction_status: str = "empty",
        intent: str | None = None,
        deal_number: int | None = None,
        parsed_conditions: dict | None = None,
        error_details: str | None = None,
        warnings: dict | None = None,
        run_id: str | None = None,
    ) -> tuple[DealParsingLog, bool]:
        """Insert a parsing log entry; return (entry, created).

        Idempotent on (message_id, fragment_idx).
        """
        log.debug(
            "create.start",
            message_id=message_id,
            fragment_idx=fragment_idx,
        )
        values = dict(
            message_id=message_id,
            fragment_idx=fragment_idx,
            fragment_text=fragment_text,
            extraction_status=extraction_status,
            intent=intent,
            deal_number=deal_number,
            parsed_conditions=parsed_conditions,
            error_details=error_details,
            warnings=warnings,
            run_id=run_id,
        )

        if _is_sqlite(self._session):
            existing = await self.get_by_message_fragment(message_id, fragment_idx)
            if existing is not None:
                log.debug("create.conflict_existing", message_id=message_id, fragment_idx=fragment_idx)
                return existing, False

            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = (
                sqlite_insert(DealParsingLog)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["message_id", "fragment_idx"])
            )
            await self._session.execute(stmt)
            await self._session.flush()
            row = await self.get_by_message_fragment(message_id, fragment_idx)
            log.debug("create.created", id=row.id)  # type: ignore[union-attr]
            return row, True  # type: ignore[return-value]

        stmt = (
            pg_insert(DealParsingLog)
            .values(**values)
            .on_conflict_do_nothing(
                constraint="uq_dpl_message_fragment",
            )
            .returning(DealParsingLog)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("create.conflict_existing", message_id=message_id, fragment_idx=fragment_idx)
            existing = await self.get_by_message_fragment(message_id, fragment_idx)
            return existing, False  # type: ignore[return-value]

        log.debug("create.created", id=row.id)
        return row, True

    async def list_by_message(self, message_id: str) -> list[DealParsingLog]:
        result = await self._session.execute(
            select(DealParsingLog)
            .where(DealParsingLog.message_id == message_id)
            .order_by(DealParsingLog.fragment_idx.asc())
        )
        return list(result.scalars().all())
