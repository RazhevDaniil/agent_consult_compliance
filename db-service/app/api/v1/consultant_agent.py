from fastapi import APIRouter, HTTPException, Query

from ...api.v1.schemas import (
    ConsultantAgentLogAnswerIn,
    ConsultantAgentLogIn,
    ConsultantAgentLogOut,
)
from ...dependencies import SessionDep
from ...logging_config import get_logger
from ...repositories.consultant_agent_log_repo import ConsultantAgentLogRepo

log = get_logger("app.api.consultant_agent")

router = APIRouter(prefix="/consultant-agent", tags=["consultant-agent"])


@router.post("/logs", response_model=ConsultantAgentLogOut, status_code=201)
async def create_log(
    body: ConsultantAgentLogIn,
    session: SessionDep,
) -> ConsultantAgentLogOut:
    """Create a Q/A log entry. Idempotent on message_id."""
    log.info(
        "create_log.start",
        id=body.id,
        message_id=body.message_id,
        session_id=body.session_id,
    )
    repo = ConsultantAgentLogRepo(session)
    entry, created = await repo.create(**body.model_dump())
    await session.commit()
    log.info("create_log.done", id=entry.id, created=created)
    return ConsultantAgentLogOut.model_validate(entry)


@router.get("/logs/{run_id}", response_model=ConsultantAgentLogOut)
async def get_log(
    run_id: str,
    session: SessionDep,
) -> ConsultantAgentLogOut:
    """Fetch one log entry by run_id (== consultant_agent_logs.id)."""
    log.info("get_log.start", run_id=run_id)
    repo = ConsultantAgentLogRepo(session)
    entry = await repo.get(run_id)
    if entry is None:
        log.warning("get_log.not_found", run_id=run_id)
        raise HTTPException(status_code=404, detail="Log entry not found")
    return ConsultantAgentLogOut.model_validate(entry)


@router.get("/logs", response_model=list[ConsultantAgentLogOut])
async def list_logs(
    session: SessionDep,
    session_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    source_system: str | None = Query(default=None),
    agent_branch: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ConsultantAgentLogOut]:
    """List log entries with optional filters, sorted by question_dttm DESC."""
    log.info(
        "list_logs.start",
        session_id=session_id,
        user_id=user_id,
        source_system=source_system,
        agent_branch=agent_branch,
        limit=limit,
        offset=offset,
    )
    repo = ConsultantAgentLogRepo(session)
    entries = await repo.list_filtered(
        session_id=session_id,
        user_id=user_id,
        source_system=source_system,
        agent_branch=agent_branch,
        limit=limit,
        offset=offset,
    )
    log.info("list_logs.done", count=len(entries))
    return [ConsultantAgentLogOut.model_validate(e) for e in entries]


@router.patch("/logs/{run_id}/answer", response_model=ConsultantAgentLogOut)
async def set_log_answer(
    run_id: str,
    body: ConsultantAgentLogAnswerIn,
    session: SessionDep,
) -> ConsultantAgentLogOut:
    """Fill in the answer for a previously created log entry."""
    log.info("set_log_answer.start", run_id=run_id)
    repo = ConsultantAgentLogRepo(session)
    entry = await repo.set_answer(
        run_id,
        agent_answer=body.agent_answer,
        answer_dttm=body.answer_dttm,
        agent_branch=body.agent_branch,
    )
    if entry is None:
        log.warning("set_log_answer.not_found", run_id=run_id)
        raise HTTPException(status_code=404, detail="Log entry not found")
    await session.commit()
    log.info("set_log_answer.done", run_id=run_id)
    return ConsultantAgentLogOut.model_validate(entry)
