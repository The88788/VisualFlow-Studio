"""Logging configuration for VeoAPI."""

import logging.handlers
import sys
from pathlib import Path

from .config import DATA_DIR, BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
ROTATING_LOG_DIR = LOG_DIR / "10min"
ROTATING_LOG_DIR.mkdir(exist_ok=True)


def setup_logger(name: str = "veoapi") -> logging.Logger:
    """Setup and return a logger instance."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_format)

    # Main File handler (app.log) - with size rotation for low disk
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB max
        backupCount=2,  # Keep 2 backups (15 MB total max)
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)

    # Rotating File handler (every 10 minutes) - limited backups
    rotating_handler = logging.handlers.TimedRotatingFileHandler(
        ROTATING_LOG_DIR / "app.log",
        when="m",
        interval=10,
        backupCount=6,  # Keep only 6 files (1 hour of logs)
        encoding="utf-8",
    )
    rotating_handler.setLevel(logging.DEBUG)
    rotating_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(rotating_handler)

    return logger


logger = setup_logger()
