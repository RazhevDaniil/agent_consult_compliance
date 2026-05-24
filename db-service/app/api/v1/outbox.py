from fastapi import APIRouter, HTTPException

from ...api.v1.schemas import DequeueIn, OutboxTaskOut, UpdateStatusIn
from ...dependencies import SessionDep
from ...logging_config import get_logger
from ...repositories.outbox_repo import OutboxRepo

log = get_logger("app.api.outbox")

router = APIRouter(prefix="/outbox", tags=["outbox"])


@router.post("/dequeue", response_model=list[OutboxTaskOut])
async def dequeue(
    body: DequeueIn,
    session: SessionDep,
) -> list[OutboxTaskOut]:
    """Claim up to `limit` ready tasks and return them with their claim tokens."""
    log.info("dequeue.start", task_type=body.task_type, limit=body.limit, claim_ttl=body.claim_ttl_seconds)
    repo = OutboxRepo(session)
    tasks = await repo.dequeue(
        task_type=body.task_type,
        limit=body.limit,
        claim_ttl_seconds=body.claim_ttl_seconds,
    )
    await session.commit()
    if tasks:
        log.info("dequeue.done", claimed=len(tasks), task_ids=[t.id for t in tasks])
    else:
        log.debug("dequeue.done", claimed=0, task_type=body.task_type)
    return [OutboxTaskOut.model_validate(t) for t in tasks]


@router.patch("/{task_id}/status", response_model=OutboxTaskOut)
async def update_status(
    task_id: str,
    body: UpdateStatusIn,
    session: SessionDep,
) -> OutboxTaskOut:
    """Update a task's status. Returns 409 if claim_token is stale or mismatched."""
    log.info(
        "update_status.start",
        task_id=task_id,
        status=body.status,
        claim_token=body.claim_token,
        error=body.error,
        next_retry_at=str(body.next_retry_at) if body.next_retry_at else None,
    )
    allowed = {"DONE", "RETRYING", "FAILED"}
    if body.status not in allowed:
        log.warning(
            "update_status.invalid_status",
            task_id=task_id,
            status=body.status,
            allowed=sorted(allowed),
        )
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(allowed)}",
        )

    repo = OutboxRepo(session)
    task = await repo.update_status(
        task_id,
        status=body.status,
        claim_token=body.claim_token,
        error=body.error,
        next_retry_at=body.next_retry_at,
    )
    if task is None:
        log.warning("update_status.conflict", task_id=task_id, claim_token=body.claim_token)
        raise HTTPException(
            status_code=409,
            detail="Task not found or claim_token is stale",
        )
    await session.commit()
    log.info("update_status.done", task_id=task_id, status=body.status)
    return OutboxTaskOut.model_validate(task)
