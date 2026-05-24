from fastapi import APIRouter
from sqlalchemy import text

from ...dependencies import SessionDep
from ...logging_config import get_logger

log = get_logger("app.api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

CASCADE_MAP: dict[str, list[str]] = {
    "threads": ["messages", "deal_parsing_log", "deal_snapshots"],
    "messages": ["deal_parsing_log", "deal_snapshots"],
    "deal_parsing_log": ["deal_snapshots"],
    "approve_tasks": ["approve_deal_snapshots"],
    "deal_snapshots": [],
    "outbox_tasks": [],
    "approve_deal_snapshots": [],
}


@router.delete("/truncate/{table_name}")
async def truncate_table(
    table_name: str,
    session: SessionDep,
) -> dict:
    """Truncate a table with CASCADE. Returns list of affected tables."""
    if table_name not in CASCADE_MAP:
        tables = sorted(CASCADE_MAP.keys())
        return {"error": f"Unknown table: {table_name}", "available_tables": tables}

    log.warning("truncate.start", table=table_name)
    await session.execute(text(f"TRUNCATE {table_name} CASCADE"))
    await session.commit()

    cascaded = CASCADE_MAP[table_name]
    log.warning("truncate.done", table=table_name, cascaded=cascaded)
    return {
        "truncated": table_name,
        "cascaded": cascaded,
    }
