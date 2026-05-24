import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DealParsingLog(Base):
    """One message fragment sent for parsing."""

    __tablename__ = "deal_parsing_log"
    __table_args__ = (
        UniqueConstraint("message_id", "fragment_idx", name="uq_dpl_message_fragment"),
        Index("ix_dpl_message_id", "message_id"),
        Index("ix_dpl_extraction_status", "extraction_status"),
        Index(
            "ix_dpl_run_id",
            "run_id",
            postgresql_where="run_id IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    message_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    fragment_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_text: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_status: Mapped[str] = mapped_column(
        String, nullable=False, default="empty"
    )
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    deal_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_conditions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
