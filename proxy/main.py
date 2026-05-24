import logging

_LOGGER = logging.getLogger(__name__)

import uvicorn

from app.main import app
from app.config import settings


if __name__ == '__main__':
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port
    )
