from fastapi import APIRouter

from ...api.v1.schemas import (
    DealParsingLogIn,
    DealParsingLogOut,
    DealSnapshotIn,
    DealSnapshotOut,
)
from ...dependencies import SessionDep
from ...logging_config import get_logger
from ...repositories.deal_parsing_log_repo import DealParsingLogRepo
from ...repositories.deal_snapshot_repo import DealSnapshotRepo

log = get_logger("app.api.deals")

router = APIRouter(prefix="/deals", tags=["deals"])


@router.post("/parsing-log", response_model=DealParsingLogOut, status_code=201)
async def create_parsing_log(
    body: DealParsingLogIn,
    session: SessionDep,
) -> DealParsingLogOut:
    """Create a parsing log entry for a message fragment."""
    log.info(
        "create_parsing_log.start",
        message_id=body.message_id,
        fragment_idx=body.fragment_idx,
    )
    repo = DealParsingLogRepo(session)
    entry, created = await repo.create(**body.model_dump())
    await session.commit()
    log.info("create_parsing_log.done", id=entry.id, created=created)
    return DealParsingLogOut.model_validate(entry)


@router.post("/snapshots", response_model=DealSnapshotOut, status_code=201)
async def create_deal_snapshot(
    body: DealSnapshotIn,
    session: SessionDep,
) -> DealSnapshotOut:
    """Create a deal negotiation snapshot."""
    log.info(
        "create_deal_snapshot.start",
        thread_id=body.thread_id,
        deal_number=body.deal_number,
        iteration=body.iteration,
    )
    repo = DealSnapshotRepo(session)
    snapshot, created = await repo.create(**body.model_dump())
    await session.commit()
    log.info("create_deal_snapshot.done", id=snapshot.id, created=created)
    return DealSnapshotOut.model_validate(snapshot)
