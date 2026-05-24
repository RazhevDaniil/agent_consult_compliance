from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConsultantAgentLog(Base):
    """One question/answer round of the СЮЛ consultant agent.

    Standalone log table — no FK to other tables, since the consultant agent
    lives outside this repo and does not share thread/message identifiers.
    """

    __tablename__ = "consultant_agent_logs"
    __table_args__ = (
        Index("ix_cal_session_id", "session_id"),
        Index("ix_cal_user_id", "user_id"),
        Index("ix_cal_question_dttm", "question_dttm"),
        Index("ix_cal_source_system", "source_system"),
        Index("ix_cal_agent_branch", "agent_branch"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    question_dttm: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    agent_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_dttm: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_system: Mapped[str] = mapped_column(String, nullable=False, default="support")
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
