from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.consultant_agent_log import ConsultantAgentLog
from ..logging_config import get_logger

log = get_logger("app.repo.consultant_agent_log")


class ConsultantAgentLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: str) -> ConsultantAgentLog | None:
        result = await self._session.execute(
            select(ConsultantAgentLog).where(ConsultantAgentLog.id == run_id)
        )
        return result.scalar_one_or_none()

    async def get_by_message_id(self, message_id: str) -> ConsultantAgentLog | None:
        result = await self._session.execute(
            select(ConsultantAgentLog).where(
                ConsultantAgentLog.message_id == message_id
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        id: str,
        session_id: str,
        message_id: str,
        user_id: str,
        question: str,
        agent_branch: str | None = None,
        agent_answer: str | None = None,
        question_dttm: datetime | None = None,
        answer_dttm: datetime | None = None,
        source_system: str = "support",
        run_id: str | None = None,
    ) -> tuple[ConsultantAgentLog, bool]:
        """Insert a log entry; return (entry, created). Idempotent on message_id."""
        log.debug("create.start", id=id, message_id=message_id, session_id=session_id)
        values: dict = dict(
            id=id,
            session_id=session_id,
            message_id=message_id,
            user_id=user_id,
            question=question,
            agent_branch=agent_branch,
            agent_answer=agent_answer,
            answer_dttm=answer_dttm,
            source_system=source_system,
            run_id=run_id,
        )
        if question_dttm is not None:
            values["question_dttm"] = question_dttm

        stmt = (
            pg_insert(ConsultantAgentLog)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(ConsultantAgentLog)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("create.conflict_existing", message_id=message_id)
            existing = await self.get_by_message_id(message_id)
            return existing, False  # type: ignore[return-value]

        log.debug("create.created", id=id, message_id=message_id)
        return row, True

    async def set_answer(
        self,
        run_id: str,
        *,
        agent_answer: str,
        answer_dttm: datetime | None = None,
        agent_branch: str | None = None,
    ) -> ConsultantAgentLog | None:
        """Fill in the answer for a previously created entry. Returns None if not found."""
        log.debug("set_answer.lookup", run_id=run_id)
        entry = await self.get(run_id)
        if entry is None:
            log.warning("set_answer.not_found", run_id=run_id)
            return None

        entry.agent_answer = agent_answer
        if answer_dttm is not None:
            entry.answer_dttm = answer_dttm
        if agent_branch is not None:
            entry.agent_branch = agent_branch

        await self._session.flush()
        log.debug("set_answer.updated", run_id=run_id)
        return entry

    async def list_filtered(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        source_system: str | None = None,
        agent_branch: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConsultantAgentLog]:
        """List log entries with optional filters, sorted by question_dttm DESC."""
        q = select(ConsultantAgentLog)
        if session_id is not None:
            q = q.where(ConsultantAgentLog.session_id == session_id)
        if user_id is not None:
            q = q.where(ConsultantAgentLog.user_id == user_id)
        if source_system is not None:
            q = q.where(ConsultantAgentLog.source_system == source_system)
        if agent_branch is not None:
            q = q.where(ConsultantAgentLog.agent_branch == agent_branch)
        q = q.order_by(ConsultantAgentLog.question_dttm.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())
