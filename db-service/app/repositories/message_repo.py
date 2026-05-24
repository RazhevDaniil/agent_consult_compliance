from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.message import Message
from ..logging_config import get_logger

log = get_logger("app.repo.message")


class MessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, message_id: str) -> Message | None:
        result = await self._session.execute(
            select(Message).where(Message.message_id == message_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        message_id: str,
        thread_id: str,
        author_email: str,
        body_text: str | None = None,
        body_html: str | None = None,
        headers_json: dict | None = None,
        run_id: str | None = None,
    ) -> tuple[Message, bool]:
        """Insert the message; return (message, created).

        On conflict (duplicate message_id) does nothing and returns the
        existing row — idempotent by design.
        """
        log.debug("upsert.start", message_id=message_id, thread_id=thread_id, author_email=author_email)
        stmt = (
            pg_insert(Message)
            .values(
                message_id=message_id,
                thread_id=thread_id,
                author_email=author_email,
                body_text=body_text,
                body_html=body_html,
                headers_json=headers_json,
                run_id=run_id,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(Message)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("upsert.conflict_existing", message_id=message_id)
            existing = await self.get(message_id)
            return existing, False  # type: ignore[return-value]

        log.debug("upsert.created", message_id=message_id)
        return row, True

    async def list_by_author(
        self,
        author_email: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Message]:
        result = await self._session.execute(
            select(Message)
            .where(Message.author_email == author_email)
            .order_by(Message.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_thread(self, thread_id: str) -> list[Message]:
        result = await self._session.execute(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
        return list(result.scalars().all())
