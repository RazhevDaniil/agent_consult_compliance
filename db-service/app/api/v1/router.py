from fastapi import APIRouter

from ...api.v1.cleanup import router as cleanup_router
from ...api.v1.approve import router as approve_router
from ...api.v1.consultant_agent import router as consultant_agent_router
from ...api.v1.deals import router as deals_router
from ...api.v1.messages import router as messages_router
from ...api.v1.outbox import router as outbox_router

router = APIRouter(prefix="/v1")
router.include_router(messages_router)
router.include_router(outbox_router)
router.include_router(approve_router)
router.include_router(deals_router)
router.include_router(consultant_agent_router)
router.include_router(cleanup_router)
