"""Centralised logging for the agent runner.

Usage in any module::

    from log import get_logger
    logger = get_logger(__name__)

Call ``setup_logging()`` once at startup (in watcher.py / main.py / planner.py).
Logs go to both stdout and ``logs/agent.log``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging(level: str = "INFO") -> None:
    """Configure the root ``agent`` logger with console + file handlers."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(
        LOG_DIR / "agent.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger("agent")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``agent`` namespace."""
    return logging.getLogger(f"agent.{name}")
