from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.thread import Thread
from ..logging_config import get_logger

log = get_logger("app.repo.thread")


class ThreadRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, thread_id: str) -> Thread | None:
        result = await self._session.execute(
            select(Thread).where(Thread.thread_id == thread_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        thread_id: str,
        subject: str | None = None,
    ) -> Thread:
        thread = Thread(thread_id=thread_id, subject=subject)
        self._session.add(thread)
        await self._session.flush()
        return thread

    async def get_or_create(
        self,
        *,
        thread_id: str,
        subject: str | None = None,
    ) -> tuple[Thread, bool]:
        """Return (thread, created). Looks up by thread_id, creates if missing."""
        existing = await self.get(thread_id)
        if existing is not None:
            log.debug("get_or_create.existing", thread_id=thread_id)
            return existing, False
        thread = await self.create(thread_id=thread_id, subject=subject)
        log.debug("get_or_create.created", thread_id=thread_id, subject=subject)
        return thread, True
