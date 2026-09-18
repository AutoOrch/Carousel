"""Centralised logging for the agent runner.

Usage in any module::

    from log import get_logger
    logger = get_logger(__name__)

Call ``setup_logging()`` once at startup (in watcher.py / main.py / planner.py).
Logs go to both stdout and ``logs/agent.log``.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from config import DATA_ROOT

LOG_DIR = DATA_ROOT / "logs"


def redact(value: str) -> str:
    text = str(value or "")
    for pattern, replacement in (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
    ):
        text = re.sub(pattern, replacement, text)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure the root ``agent`` logger with console + file handlers."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(_RedactingFilter())

    file_handler = logging.FileHandler(
        LOG_DIR / "agent.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RedactingFilter())

    root = logging.getLogger("agent")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``agent`` namespace."""
    return logging.getLogger(f"agent.{name}")
