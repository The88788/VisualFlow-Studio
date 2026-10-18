"""Logging setup for ImageAPI."""

import logging
import os
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent.parent / "logs" / "imageapi"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("imageapi")
logger.setLevel(logging.DEBUG)

# Console handler
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(console)

# Rotating file handler (5 MB, 2 backups)
file_handler = RotatingFileHandler(
    LOG_DIR / "app.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=2,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
)
logger.addHandler(file_handler)

# 10-minute rotation handler
ten_min_dir = LOG_DIR / "10min"
ten_min_dir.mkdir(parents=True, exist_ok=True)
timed_handler = TimedRotatingFileHandler(
    ten_min_dir / "imageapi.log",
    when="M",
    interval=10,
    backupCount=6,
    encoding="utf-8",
)
timed_handler.setLevel(logging.DEBUG)
timed_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
)
logger.addHandler(timed_handler)
