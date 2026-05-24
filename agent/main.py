import logging

_LOGGER = logging.getLogger(__name__)

try:
    import pysqlite3
    import sys
    sys.modules["sqlite3"] = pysqlite3
    sys.modules["sqlite"] = pysqlite3
except Exception as e:
    _LOGGER.error(f"[sqlite_patch] skip patch: {e}")

import uvicorn

from api.config import settings
from api.app import app


if __name__ == '__main__':
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port
    )
