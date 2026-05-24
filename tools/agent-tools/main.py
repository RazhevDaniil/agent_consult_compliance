import os
import uvicorn

from src.app import app


HOST = os.getenv('AGENT_TOOLS_SERVICE_HOST', "127.0.0.1")

PORT = int(os.getenv('AGENT_TOOLS_SERVICE_PORT', "8080"))


def main(
        host: str,
        port: int
) -> None:
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main(HOST, PORT)
