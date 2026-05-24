from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.approve_deal_snapshot import ApproveDealSnapshot
from ..logging_config import get_logger

log = get_logger("app.repo.approve_snapshot")


class ApproveSnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, calc_id: str) -> ApproveDealSnapshot | None:
        result = await self._session.execute(
            select(ApproveDealSnapshot).where(
                ApproveDealSnapshot.calc_id == calc_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_task_id(self, task_id: str) -> ApproveDealSnapshot | None:
        result = await self._session.execute(
            select(ApproveDealSnapshot).where(
                ApproveDealSnapshot.task_id == task_id
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        calc_id: str,
        task_id: str,
        run_id: str | None = None,
        calc_dttm: datetime | None = None,
        # Financial
        policy_rate: Decimal | None = None,
        ets: Decimal | None = None,
        for_rate: Decimal | None = None,
        eva_target: Decimal | None = None,
        eva_indicative: Decimal | None = None,
        asv: Decimal | None = None,
        option_price: Decimal | None = None,
        crl: Decimal | None = None,
        cfc_so: Decimal | None = None,
        cfc_ets: Decimal | None = None,
        cfc_for_rate: Decimal | None = None,
        cfc_eva_target: Decimal | None = None,
        cfc_eva_indicative: Decimal | None = None,
        cfc_asv: Decimal | None = None,
        cfc_option_price: Decimal | None = None,
        cfc_k_liq_fund: Decimal | None = None,
        # Deal conditions
        inn: str | None = None,
        term_days: int | None = None,
        volume: float | None = None,
        currency: str | None = None,
        rate: float | None = None,
        rate_type: str | None = None,
        product: str | None = None,
        basis: str | None = None,
        optionality: str | None = None,
    ) -> tuple[ApproveDealSnapshot, bool]:
        """Insert a snapshot; return (snapshot, created). Idempotent on calc_id."""
        log.debug("create.start", calc_id=calc_id, task_id=task_id)
        values: dict = dict(
            calc_id=calc_id,
            task_id=task_id,
            run_id=run_id,
            policy_rate=policy_rate,
            ets=ets,
            for_rate=for_rate,
            eva_target=eva_target,
            eva_indicative=eva_indicative,
            asv=asv,
            option_price=option_price,
            crl=crl,
            cfc_so=cfc_so,
            cfc_ets=cfc_ets,
            cfc_for_rate=cfc_for_rate,
            cfc_eva_target=cfc_eva_target,
            cfc_eva_indicative=cfc_eva_indicative,
            cfc_asv=cfc_asv,
            cfc_option_price=cfc_option_price,
            cfc_k_liq_fund=cfc_k_liq_fund,
            inn=inn,
            term_days=term_days,
            volume=volume,
            currency=currency,
            rate=rate,
            rate_type=rate_type,
            product=product,
            basis=basis,
            optionality=optionality,
        )
        if calc_dttm is not None:
            values["calc_dttm"] = calc_dttm

        stmt = (
            pg_insert(ApproveDealSnapshot)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["calc_id"])
            .returning(ApproveDealSnapshot)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            log.debug("create.conflict_existing", calc_id=calc_id)
            existing = await self.get(calc_id)
            return existing, False  # type: ignore[return-value]

        log.debug("create.created", calc_id=calc_id)
        return row, True
