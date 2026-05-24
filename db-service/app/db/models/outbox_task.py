import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, String, Integer, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutboxTask(Base):
    """Task queue entry processed by workers (IMAP notify, SMTP send, etc.).

    task_key is the idempotency key (UNIQUE).  Callers should pass a
    deterministic key so that retried HTTP requests don't produce duplicate
    tasks.

    Claim protocol:
      1. dequeue() atomically flips status → PROCESSING and sets claim_token /
         claim_until inside a single transaction (FOR UPDATE SKIP LOCKED).
      2. Worker calls update_status() with the claim_token it received.
         Mismatched token → 409 Conflict (stale claim).
    """

    __tablename__ = "outbox_tasks"
    __table_args__ = (
        UniqueConstraint("task_key", name="uq_outbox_task_key"),
        Index("ix_outbox_status_retry", "status", "next_retry_at"),
        Index("ix_outbox_tasks_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    email_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("messages.message_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    task_key: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="NEW", index=True)
    # NEW | PROCESSING | RETRYING | DONE | FAILED

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    message: Mapped["Message | None"] = relationship(  # noqa: F821
        "Message", back_populates="outbox_tasks", lazy="noload"
    )
