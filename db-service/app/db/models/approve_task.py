from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApproveTask(Base):
    """One approval/review task for a deal."""

    __tablename__ = "approve_tasks"
    __table_args__ = (
        Index("ix_at_task_status", "task_status"),
        Index("ix_at_updated_at", "updated_at"),
        Index(
            "ix_at_run_id",
            "run_id",
            postgresql_where="run_id IS NOT NULL",
        ),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    calculation_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    task_dttm: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    task_status: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    deal_status: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
