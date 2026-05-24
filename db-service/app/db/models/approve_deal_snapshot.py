from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ...db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApproveDealSnapshot(Base):
    """One rate calculation with full parameter set from both services."""

    __tablename__ = "approve_deal_snapshots"
    __table_args__ = (
        Index("ix_ads_task_id", "task_id"),
        Index(
            "ix_ads_run_id",
            "run_id",
            postgresql_where="run_id IS NOT NULL",
        ),
    )

    # (a) Identification
    calc_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("approve_tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    calc_dttm: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # (b) Parameters from agent_tools_app
    policy_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    ets: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    for_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    eva_target: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    eva_indicative: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    asv: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    option_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    crl: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # (c) Parameters from cfc-service
    cfc_so: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_ets: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_for_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_eva_target: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_eva_indicative: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_asv: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_option_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    cfc_k_liq_fund: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # (d) Deal conditions from external system
    inn: Mapped[str | None] = mapped_column(String, nullable=True)
    term_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume: Mapped[float | None] = mapped_column(nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    rate: Mapped[float | None] = mapped_column(nullable=True)
    rate_type: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    basis: Mapped[str | None] = mapped_column(String, nullable=True)
    optionality: Mapped[str | None] = mapped_column(String, nullable=True)
