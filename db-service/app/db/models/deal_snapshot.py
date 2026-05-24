import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DealSnapshot(Base):
    """Deal state after one negotiation round."""

    __tablename__ = "deal_snapshots"
    __table_args__ = (
        UniqueConstraint("parsing_log_id", name="uq_ds_parsing_log_id"),
        Index("ix_ds_thread_id", "thread_id"),
        Index("ix_ds_deal_number", "thread_id", "deal_number"),
        Index("ix_ds_parsing_log_id", "parsing_log_id"),
        Index("ix_ds_status", "status"),
        Index(
            "ix_ds_run_id",
            "run_id",
            postgresql_where="run_id IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    thread_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("threads.thread_id", ondelete="CASCADE"),
        nullable=False,
    )
    deal_number: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    incoming_conditions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    limit_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    avg_hist_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    model_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    policy_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    final_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    proposed_conditions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsing_log_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("deal_parsing_log.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    utilizes_limit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    agent_text: Mapped[str] = mapped_column(Text, nullable=False)
