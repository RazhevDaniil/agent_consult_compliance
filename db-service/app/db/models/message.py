from datetime import datetime, timezone

from sqlalchemy import JSON, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Message(Base):
    """One email message (incoming or outgoing).

    message_id is the RFC Message-ID normalised by mail_app — it acts as both
    PK and idempotency key (UNIQUE enforced by the PK constraint itself).
    app never generates IDs, it only stores what mail_app sends.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_author_email", "author_email"),
        Index("ix_messages_thread_id", "thread_id"),
        Index("ix_messages_run_id", "run_id"),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("threads.thread_id", ondelete="CASCADE"),
        nullable=False,
    )
    author_email: Mapped[str] = mapped_column(String, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # All raw headers: To, Cc, Bcc, In-Reply-To, References, Subject, etc.
    headers_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    thread: Mapped["Thread"] = relationship(  # noqa: F821
        "Thread", lazy="noload"
    )
    outbox_tasks: Mapped[list["OutboxTask"]] = relationship(  # noqa: F821
        "OutboxTask", back_populates="message", lazy="noload"
    )
