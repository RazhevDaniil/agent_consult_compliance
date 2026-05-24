from fastapi import APIRouter, HTTPException, Query

from ...api.v1.schemas import (
    ApproveDealSnapshotIn,
    ApproveDealSnapshotOut,
    ApproveTaskIn,
    ApproveTaskOut,
    UpdateApproveTaskIn,
)
from ...dependencies import SessionDep
from ...logging_config import get_logger
from ...repositories.approve_snapshot_repo import ApproveSnapshotRepo
from ...repositories.approve_task_repo import ApproveTaskRepo

log = get_logger("app.api.approve")

router = APIRouter(prefix="/approve", tags=["approve"])


# ─── Snapshots ───────────────────────────────────────────────────────────────


@router.post("/snapshots", response_model=ApproveDealSnapshotOut, status_code=201)
async def create_snapshot(
    body: ApproveDealSnapshotIn,
    session: SessionDep,
) -> ApproveDealSnapshotOut:
    """Create a rate calculation snapshot bound to an existing approve_task."""
    log.info("create_snapshot.start", calc_id=body.calc_id, task_id=body.task_id)
    repo = ApproveSnapshotRepo(session)
    snapshot, created = await repo.create(**body.model_dump())
    await session.commit()
    log.info("create_snapshot.done", calc_id=snapshot.calc_id, created=created)
    return ApproveDealSnapshotOut.model_validate(snapshot)


@router.get(
    "/snapshots/by-task/{task_id}", response_model=ApproveDealSnapshotOut
)
async def get_snapshot_by_task(
    task_id: str,
    session: SessionDep,
) -> ApproveDealSnapshotOut:
    """Get the snapshot bound to a task (1:1 via UNIQUE FK)."""
    log.info("get_snapshot_by_task.start", task_id=task_id)
    repo = ApproveSnapshotRepo(session)
    snapshot = await repo.get_by_task_id(task_id)
    if snapshot is None:
        log.warning("get_snapshot_by_task.not_found", task_id=task_id)
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return ApproveDealSnapshotOut.model_validate(snapshot)


# ─── Tasks ───────────────────────────────────────────────────────────────────


@router.post("/tasks", response_model=ApproveTaskOut, status_code=201)
async def create_task(
    body: ApproveTaskIn,
    session: SessionDep,
) -> ApproveTaskOut:
    """Create an approval task. Idempotent on task_id."""
    log.info(
        "create_task.start",
        task_id=body.task_id,
        calculation_id=body.calculation_id,
    )
    repo = ApproveTaskRepo(session)
    task, created = await repo.create(**body.model_dump())
    await session.commit()
    log.info("create_task.done", task_id=task.task_id, created=created)
    return ApproveTaskOut.model_validate(task)


@router.get("/tasks/{task_id}", response_model=ApproveTaskOut)
async def get_task(
    task_id: str,
    session: SessionDep,
) -> ApproveTaskOut:
    """Get an approval task by task_id (UUID)."""
    log.info("get_task.start", task_id=task_id)
    repo = ApproveTaskRepo(session)
    task = await repo.get(task_id)
    if task is None:
        log.warning("get_task.not_found", task_id=task_id)
        raise HTTPException(status_code=404, detail="Task not found")
    return ApproveTaskOut.model_validate(task)


@router.get("/tasks/by-calculation/{calculation_id}", response_model=ApproveTaskOut)
async def get_task_by_calculation_id(
    calculation_id: str,
    session: SessionDep,
) -> ApproveTaskOut:
    """Get an approval task by external calculation_id (deduplication lookup)."""
    log.info("get_task_by_calc.start", calculation_id=calculation_id)
    repo = ApproveTaskRepo(session)
    task = await repo.get_by_calculation_id(calculation_id)
    if task is None:
        log.warning("get_task_by_calc.not_found", calculation_id=calculation_id)
        raise HTTPException(status_code=404, detail="Task not found")
    return ApproveTaskOut.model_validate(task)


@router.get("/tasks", response_model=list[ApproveTaskOut])
async def list_tasks(
    session: SessionDep,
    task_status: str | None = Query(default=None),
    calculation_id: str | None = Query(default=None),
    older_than_minutes: int | None = Query(
        default=None,
        description="Return only tasks whose updated_at is older than N minutes",
    ),
) -> list[ApproveTaskOut]:
    """List approval tasks with optional filters, sorted by task_dttm ASC."""
    log.info(
        "list_tasks.start",
        task_status=task_status,
        calculation_id=calculation_id,
        older_than_minutes=older_than_minutes,
    )
    repo = ApproveTaskRepo(session)
    tasks = await repo.list_filtered(
        task_status=task_status,
        calculation_id=calculation_id,
        older_than_minutes=older_than_minutes,
    )
    log.info("list_tasks.done", count=len(tasks))
    return [ApproveTaskOut.model_validate(t) for t in tasks]


@router.patch("/tasks/{task_id}/status", response_model=ApproveTaskOut)
async def update_task_status(
    task_id: str,
    body: UpdateApproveTaskIn,
    session: SessionDep,
) -> ApproveTaskOut:
    """Update task status, attempt counter, agent answer and error message."""
    log.info(
        "update_task_status.start",
        task_id=task_id,
        task_status=body.task_status,
        deal_status=body.deal_status,
        attempt_count=body.attempt_count,
    )
    repo = ApproveTaskRepo(session)
    task = await repo.update_status(
        task_id,
        task_status=body.task_status,
        deal_status=body.deal_status,
        agent_answer=body.agent_answer,
        attempt_count=body.attempt_count,
        error_message=body.error_message,
        run_id=body.run_id,
    )
    if task is None:
        log.warning("update_task_status.not_found", task_id=task_id)
        raise HTTPException(status_code=404, detail="Task not found")
    await session.commit()
    log.info("update_task_status.done", task_id=task_id, task_status=body.task_status)
    return ApproveTaskOut.model_validate(task)
