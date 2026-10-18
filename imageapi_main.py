"""Entry point for ImageAPI."""

import uvicorn
from imageapi.core.config import config

if __name__ == "__main__":
    uvicorn.run(
        "imageapi.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False,
        log_level="info",
    )
