"""Entry point for VisualFlow Studio."""

import uvicorn

from src.core.config import config

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False,
        log_level="info",
    )
